"""结构化阶段调用走流式：拼回内容与用量、可关、中途断流记结果未知、流里报错按瞬时错误重试。"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from conftest import reopen, set_config
from podflow import http, llm

MODEL = "gpt-6.1-sol-high"


def _call(ctx) -> tuple[str, dict, str, bool]:
    return llm._chat_once(
        ctx,
        stage="PROBE",
        scope_id="probe",
        model=MODEL,
        messages=[
            {"role": "system", "content": "ping"},
            {"role": "user", "content": "ping"},
        ],
        estimated_cost=0.0,
        json_mode=True,
        asset_id=None,
    )


def _last_chat_body(fake) -> dict:
    bodies = [
        json.loads(r["body"])
        for r in fake.requests
        if r["path"] == "/v1/chat/completions"
    ]
    return bodies[-1]


def _use_handler(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        http,
        "make_client",
        lambda timeout: httpx.Client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )


def _call_row(ctx, call_id: str):
    return ctx.db.q1(
        "SELECT status, usage_json, error FROM provider_calls WHERE call_id = ?",
        (call_id,),
    )


def test_stage_calls_stream_and_keep_content_and_usage(ctx, fake):
    text, usage, call_id, json_mode = _call(ctx)

    assert json.loads(text) == {"ok": True}
    assert usage == {"prompt_tokens": 2000, "completion_tokens": 800}
    assert json_mode is True
    body = _last_chat_body(fake)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    row = _call_row(ctx, call_id)
    assert row["status"] == "succeeded"
    assert json.loads(row["usage_json"])["completion_tokens"] == 800


def test_stream_can_be_turned_off(home, fake):
    set_config(home, "llm.stream", False)
    ctx = reopen(home)
    try:
        text, usage, _, _ = _call(ctx)
    finally:
        ctx.db.close()

    assert json.loads(text) == {"ok": True}
    assert usage["completion_tokens"] == 800
    body = _last_chat_body(fake)
    assert "stream" not in body and "stream_options" not in body


def test_plain_json_reply_to_a_stream_request_still_works(ctx, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-plain",
                "model": MODEL,
                "choices": [
                    {"message": {"role": "assistant", "content": '{"ok": true}'}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    _use_handler(monkeypatch, handler)
    text, usage, _, _ = _call(ctx)

    assert json.loads(text) == {"ok": True}
    assert usage["completion_tokens"] == 5


class _CutStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b'data: {"choices": [{"delta": {"content": "{\\"ok\\""}}]}\n\n'
        raise httpx.ReadTimeout("模拟输出到一半断流")


def test_stream_cut_midway_is_result_unknown(ctx, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_CutStream()
        )

    _use_handler(monkeypatch, handler)
    with pytest.raises(llm.LLMResultUnknown) as exc:
        _call(ctx)

    assert _call_row(ctx, exc.value.call_id)["status"] == "unknown"


ERROR_BODY = (
    'data: {"choices": [{"delta": {"content": "{"}}]}\n\n'
    'data: {"error": {"message": "upstream exploded"}}\n\n'
    "data: [DONE]\n\n"
)


def _error_response() -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=ERROR_BODY.encode()
    )


def _statuses(ctx) -> list[str]:
    rows = ctx.db.q("SELECT status FROM provider_calls ORDER BY call_id")
    return [r["status"] for r in rows]


def test_error_inside_stream_is_retried_like_a_5xx(ctx, fake, monkeypatch):
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return _error_response() if len(seen) == 1 else fake.handler(request)

    _use_handler(monkeypatch, handler)
    text, _, _, _ = _call(ctx)

    assert json.loads(text) == {"ok": True}
    assert _statuses(ctx) == ["failed", "succeeded"]


def test_error_inside_stream_gives_up_after_the_retry_limit(ctx, monkeypatch):
    _use_handler(monkeypatch, lambda request: _error_response())
    with pytest.raises(llm.LLMError, match="upstream exploded") as exc:
        _call(ctx)

    assert not isinstance(exc.value, llm.LLMResultUnknown)
    assert _statuses(ctx) == ["failed"] * 4
