"""P06 容错：只做记录的字段由程序归位 / 补默认值、一段输出里有几个 JSON 时取对的那个、
退回的原文留在 logs/llm-rejects/、几次重答都没过时任务放回队列新开对话重写一次。"""

from __future__ import annotations

import json

import pytest

from conftest import default_result, envelope
from podflow import llm, maintenance
from podflow.stages import job as jobmod
from podflow.util import loads
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_variant_rules import _chat_messages, _text

RECORD_FIELDS = (
    "prompt_summary",
    "avoid_instructions",
    "generation_parameters",
    "text_overlay",
    "checks",
)


def _render(ctx) -> dict:
    return loads(
        ctx.db.scalar("SELECT render_spec_json FROM jobs WHERE job_id = 'J0001'")
    )


def _events(ctx, name: str) -> list[dict]:
    return [
        loads(r["payload_json"])
        for r in ctx.db.q(
            "SELECT payload_json FROM events WHERE event = ? ORDER BY event_id", (name,)
        )
    ]


def test_missing_record_fields_are_filled_without_asking_again(
    ctx, fake, seed_image
) -> None:
    """线上 2026-10-26 的样子：提示词都写了，漏了 5 个只做记录的字段。程序补上，不再重答。"""
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        result = default_result("P06", payload, fake)
        for key in RECORD_FIELDS:
            result.pop(key)
        return json.dumps(envelope("P06", result), ensure_ascii=False)

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == 1
    assert ctx.db.scalar("SELECT status FROM jobs WHERE job_id = 'J0001'") == "complete"
    render = _render(ctx)
    result = render["result"]
    assert "程序按方向标题补" in result["prompt_summary"]
    assert result["avoid_instructions"] == [] and result["checks"] == []
    assert result["generation_parameters"] == {}
    assert result["text_overlay"]["enabled"] is False
    assert any("程序补了默认值" in n for n in render["notes"])
    (normalized,) = _events(ctx, "stage_output_normalized")
    assert normalized["stage"] == "P06" and "checks" in normalized["notes"][0]
    assert _events(ctx, "stage_output_rejected") == []


def test_misplaced_fields_are_moved_back_into_result(ctx, fake, seed_image) -> None:
    """字段写在外壳顶层，或写进了 i2i_prompts 里：挪回 result，内容照模型写的。"""
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        result = default_result("P06", payload, fake)
        summary = result.pop("prompt_summary")
        checks = result.pop("checks")
        avoid = result.pop("avoid_instructions")
        result.setdefault("i2i_prompts", {})["avoid_instructions"] = avoid
        out = envelope("P06", result)
        out["prompt_summary"] = summary
        out["checks"] = checks
        return json.dumps(out, ensure_ascii=False)

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == 1
    render = _render(ctx)
    result = render["result"]
    assert result["prompt_summary"] == "三只幼态动物坐在枝条上，米白底，水彩"
    assert result["checks"] == ["右侧主体为目标物种"]
    assert result["avoid_instructions"] == ["商品摆拍", "尺寸线"]
    assert "avoid_instructions" not in (result.get("i2i_prompts") or {})
    assert any("写错了位置" in n for n in render["notes"])


def test_missing_prompts_are_still_sent_back(ctx, fake, seed_image) -> None:
    """真正要用的提示词缺了照样退回重答；同时漏的记录字段不算进错误里。"""
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        result = default_result("P06", payload, fake)
        if fake.stage_counts["P06"] == 1:
            result["i2i_prompts"].pop("gpt", None)
            result["prompts"].pop("gpt", None)
            for key in RECORD_FIELDS:
                result.pop(key)
        return json.dumps(envelope("P06", result), ensure_ascii=False)

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == 2
    (rejected,) = _events(ctx, "stage_output_rejected")
    assert all("缺少模型家族 gpt" in e for e in rejected["errors"])
    assert not any("required property" in e for e in rejected["errors"])
    assert "缺少模型家族 gpt" in _text(_chat_messages(fake, "P06")[1][-1]["content"])
    assert ctx.db.scalar("SELECT status FROM jobs WHERE job_id = 'J0001'") == "complete"


@pytest.mark.parametrize("shape", ["note_first", "duplicated"])
def test_two_json_objects_in_one_answer(ctx, fake, seed_image, shape) -> None:
    """一段输出里有两个 JSON（线上报 Extra data）：取带外壳、能通过校验的那个，不重答。"""
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        full = json.dumps(
            envelope("P06", default_result("P06", payload, fake)), ensure_ascii=False
        )
        if shape == "note_first":
            return '{"schema_version": "0.1", "status": "complete"}\n' + full
        return full + full

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == 1
    assert ctx.db.scalar("SELECT status FROM jobs WHERE job_id = 'J0001'") == "complete"
    (repaired,) = _events(ctx, "stage_output_repaired")
    assert repaired["objects"] == 2
    assert repaired["picked"] == (1 if shape == "note_first" else 0)


def test_rejected_answers_are_saved_for_diagnosis(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        if fake.stage_counts["P06"] == 1:
            return "这不是 JSON，写不出来"
        return json.dumps(
            envelope("P06", default_result("P06", payload, fake)), ensure_ascii=False
        )

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == 2
    (rejected,) = _events(ctx, "stage_output_rejected")
    assert rejected["raw"].startswith("logs/llm-rejects/P06-J0001-")
    assert (ctx.home / rejected["raw"]).read_text(
        encoding="utf-8"
    ) == "这不是 JSON，写不出来"


def test_feedback_says_where_missing_fields_go() -> None:
    text = llm._feedback(["$.result: 'checks' is a required property"])
    assert "result 里缺 checks" in text and "result 这一层" in text
    assert "result 这一层" not in llm._feedback(["无法解析为 JSON 对象：x"])


def test_json_objects_parsing() -> None:
    assert llm.json_objects('{"a": 1}') == [{"a": 1}]
    assert llm.json_objects('说明文字 {"a": 1} 结尾') == [{"a": 1}]
    assert llm.json_objects('```json\n{"a": 1}\n```') == [{"a": 1}]
    assert llm.json_objects('{"a": 1}{"b": 2}') == [{"a": 1}, {"b": 2}]
    with pytest.raises(json.JSONDecodeError):
        llm.json_objects('{"a": [1, 2')


def test_s06_is_requeued_once_then_fails(ctx, fake, seed_image) -> None:
    """P06 几次重答都没过：放回队列新开对话重写一次，还不行才判失败；重出之后重新计数。"""
    bootstrap(ctx, seed_image, limit=1)
    fake.overrides["P06"] = lambda payload: "写不出来"
    out = Worker(ctx, max_minutes=5).run()
    retries = int(ctx.setting("llm.max_format_retries", 2)) + 1
    assert fake.stage_counts["P06"] == 2 * retries
    assert [j["status"] for j in out["jobs"]] == ["ready", "failed"]
    assert out["jobs"][0]["retry"] == "S06"
    # 第二次是新开的对话：只有 system + user 两条
    assert len(_chat_messages(fake, "P06")[retries]) == 2
    assert len(_events(ctx, "s06_output_retry")) == 1
    job = ctx.db.q1("SELECT status, last_error FROM jobs WHERE job_id = 'J0001'")
    assert job["status"] == "failed" and "多次未通过校验" in job["last_error"]

    maintenance.redo_jobs(ctx, ["J0001"], by="tester")
    exc = llm.StageOutputInvalid("P06", ["x"])
    assert jobmod._retry_s06(ctx, "J0001", exc) is True
    assert jobmod._retry_s06(ctx, "J0001", exc) is False
