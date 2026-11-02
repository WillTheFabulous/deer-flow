"""结构化阶段调用：G00 + Pxx → LiteLLM chat/completions（gpt-6.1-sol）→ JSON 外壳 + result 的 schema 校验。

- 每一次 HTTP 请求都是账本里的一行：先原子预留、标记已提交，再发请求；
- 429 / 5xx / 连不上：释放预留后退避重试（尊重 Retry-After），次数受 CFG12 限制；
- 已发出却超时：标记结果未知、保留预留额，不自动重付费；
- 输出不合 schema：把校验错误回给模型重答，次数受 llm.max_format_retries 限制；
  只做记录的字段由各阶段的 normalize 先归位、补默认值，不为它们重答；退回的原文留在 logs/llm-rejects/。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import budget, http, limits, prompts, schemas
from .context import Ctx
from .db import log_event
from .util import sha256_text

TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_sleep: Callable[[float], None] = time.sleep
# 退回的模型原文留在数据目录 logs/llm-rejects/，超过这么多天的删掉
REJECT_DIR = "llm-rejects"
REJECT_KEEP_DAYS = 14
_rejects_pruned = False
_REQUIRED = re.compile(r"^\$\.result: '([^']+)' is a required property$")


class LLMError(RuntimeError):
    pass


class LLMPermanentError(LLMError):
    pass


class LLMStreamError(LLMError):
    """流式响应中途报错（上游断了、代理没法再换线路），按瞬时错误重试。"""


class LLMResultUnknown(LLMError):
    def __init__(self, call_id: str, message: str):
        super().__init__(message)
        self.call_id = call_id


class StageOutputInvalid(LLMError):
    def __init__(self, stage: str, errors: list[str]):
        super().__init__(f"{stage} 输出多次未通过校验：{errors[:5]}")
        self.stage = stage
        self.errors = errors


@dataclass
class StageOutput:
    stage: str
    envelope: dict[str, Any]
    model: str
    prompt_version: str
    call_ids: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    # 程序替模型补齐 / 归位了哪些字段（normalize 给的说明）
    notes: list[str] = field(default_factory=list)

    @property
    def result(self) -> dict[str, Any]:
        return self.envelope["result"]


def stage_model(ctx: Ctx, stage: str) -> str:
    return (ctx.setting("llm.stage_models", {}) or {}).get(stage) or ctx.setting(
        "llm.default_model"
    )


def project_constraints(ctx: Ctx) -> dict[str, Any]:
    project = ctx.setting("project", {}) or {}
    return {
        "market": project.get("market"),
        "user_constraints": project.get("user_constraints") or [],
        "default_positioning": project.get("default_positioning"),
        "publish": "本流水线止于候选与预览文件，禁止上架、下单或写入外部商城",
        "paid_generation": "只有程序完成预算预留后才会生图；模型不得自行决定付费、提高预算或放宽标准",
        "print_template": "未配置供应商 SKU 模板，只能产出 preview_ready"
        if not ctx.setting("print.sku_templates")
        else "已配置 SKU 模板",
    }


def json_objects(text: str) -> list[Any]:
    """文本里的 JSON：整段是一个 JSON 就只有它；否则从第一个「{」起依次解出相连的完整对象。

    模型偶尔在一段输出里写两个对象（先写一小段，再写完整外壳；或把外壳原样重复一遍），
    整段解析会报 Extra data；前后夹了说明文字的也能解出来。一个完整对象都解不出时抛出原来的解析错误。
    """
    body = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        return [json.loads(body)]
    except json.JSONDecodeError as exc:
        first_error = exc
    decoder = json.JSONDecoder()
    found: list[Any] = []
    pos = body.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(body, pos)
        except json.JSONDecodeError:
            break
        found.append(obj)
        pos = body.find("{", end)
    if not found:
        raise first_error
    return found


def extract_json_object(text: str) -> Any:
    """第一个完整的 JSON 值（兼容旧调用）。"""
    return json_objects(text)[0]


def _check_envelope(
    obj: Any,
    stage: str,
    job_id: str,
    normalize: Callable[[dict[str, Any]], list[str]] | None,
    extra_validate: Callable[[dict[str, Any]], list[str]] | None,
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    if not isinstance(obj, dict):
        return None, ["顶层必须是 JSON 对象（公共外壳）"], []
    if "result" not in obj:
        return (
            None,
            [
                "缺少公共外壳：必须包含 schema_version / job_id / stage / status / result / unknowns / evidence_refs / next_action"
            ],
            [],
        )
    # 外壳里的标识由运行层决定，模型写错也以程序值为准
    obj["schema_version"] = prompts.SCHEMA_VERSION
    obj["job_id"] = job_id
    obj["stage"] = stage
    obj.setdefault("unknowns", [])
    obj.setdefault("evidence_refs", [])
    obj.setdefault("next_action", "")
    notes: list[str] = []
    if normalize and isinstance(obj.get("result"), dict):
        notes = normalize(obj)
    errors = schemas.validate_envelope(obj)
    if isinstance(obj.get("result"), dict):
        errors += schemas.validate_result(stage, obj["result"])
    if not errors and extra_validate:
        errors = extra_validate(obj["result"])
    return obj, errors, notes


def parse_envelope(
    text: str,
    stage: str,
    job_id: str,
    *,
    normalize: Callable[[dict[str, Any]], list[str]] | None = None,
    extra_validate: Callable[[dict[str, Any]], list[str]] | None = None,
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any]]:
    """解析并校验外壳与 result，返回 (外壳, 错误, 说明)。

    一段输出里有几个 JSON 对象时，取第一个整份通过校验的；都不通过时报错误最少的那个（优先带 result 的）。
    说明：objects（解出几个对象）、picked（用的第几个）、notes（normalize 补齐 / 归位了什么）。
    """
    try:
        objects = json_objects(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"无法解析为 JSON 对象：{exc}"], {}
    best: tuple[dict[str, Any] | None, list[str], dict[str, Any]] | None = None
    for index, obj in enumerate(objects):
        envelope, errors, notes = _check_envelope(
            obj, stage, job_id, normalize, extra_validate
        )
        info = {"objects": len(objects), "picked": index, "notes": notes}
        if envelope is not None and not errors:
            return envelope, errors, info
        if best is None or (best[0] is None, len(best[1])) > (
            envelope is None,
            len(errors),
        ):
            best = (envelope, errors, info)
    assert best is not None
    return best


def _save_reject(
    ctx: Ctx, stage: str, scope_id: str, call_id: str, text: str
) -> str | None:
    """把退回的模型原文写进 logs/llm-rejects/，返回相对数据目录的路径；写不了不影响重答。"""
    global _rejects_pruned
    folder = ctx.home / "logs" / REJECT_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not _rejects_pruned:
            _rejects_pruned = True
            cutoff = time.time() - REJECT_KEEP_DAYS * 86400
            for old in folder.glob("*.txt"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{stage}-{scope_id}-{call_id}")
        path = folder / f"{safe}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path.relative_to(ctx.home))
    except OSError:
        return None


def _feedback(errors: list[str]) -> str:
    lines = ["上次输出未通过程序校验：", *(f"- {e}" for e in errors[:12])]
    missing = [m.group(1) for e in errors if (m := _REQUIRED.match(e))]
    if missing:
        lines.append(
            f"result 里缺 {'、'.join(missing)}：这些字段都写在 result 这一层（和 variant_id 同级），"
            "不要写进外壳顶层、prompts / i2i_prompts 或别的对象里。"
        )
    return "\n".join(lines) + "\n请修正后重新输出完整的 JSON 对象（只输出 JSON）。"


def _message_text(data: dict[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"响应缺少 choices[0].message.content：{str(data)[:300]}"
        ) from exc
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return content or ""


def _read_stream(resp: httpx.Response) -> dict[str, Any]:
    """把流式响应（SSE）拼回非流式响应的样子：choices[0].message.content、usage、id、model。"""
    parts: list[str] = []
    usage: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for line in resp.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LLMStreamError(f"流式响应有一段不是 JSON：{data[:200]}") from exc
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            raise LLMStreamError(f"流式响应中途报错：{str(chunk['error'])[:300]}")
        for key in ("id", "model"):
            if chunk.get(key) and not meta.get(key):
                meta[key] = chunk[key]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str):
                parts.append(content)
    return {
        **meta,
        "choices": [{"message": {"role": "assistant", "content": "".join(parts)}}],
        "usage": usage,
    }


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        value = resp.headers.get("retry-after")
        if value:
            try:
                return min(float(value), 120.0)
            except ValueError:
                pass
    return min(5.0 * (2**attempt), 60.0)


@dataclass
class _Attempt:
    """一次 HTTP 尝试的结果：拿到回答，或要退避后重试（delay），或关掉 json_mode 马上重发。"""

    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    retry_delay: float | None = None
    json_off: bool = False


def _chat_once(
    ctx: Ctx,
    *,
    stage: str,
    scope_id: str,
    model: str,
    messages: list[dict[str, Any]],
    estimated_cost: float,
    json_mode: bool,
    asset_id: str | None,
) -> tuple[str, dict[str, Any], str, bool]:
    key_name = ctx.setting("litellm.api_key_name", "LITELLM_API_KEY")
    if not ctx.secrets.get(key_name):
        raise LLMPermanentError(f"secrets.env 缺少 {key_name}")
    stream = bool(ctx.setting("llm.stream", True))
    retry_limit = int(ctx.setting("batch.transient_retry_limit", 3))
    max_tokens = ctx.setting("llm.max_tokens")
    attempt = 0
    while True:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        # 在 worker 里时每次 HTTP 尝试占一个 LLM 名额；退避等待在名额外面
        with limits.llm_slot(ctx):
            outcome = _chat_attempt(
                ctx,
                stage=stage,
                scope_id=scope_id,
                model=model,
                body=json.dumps(payload, ensure_ascii=False),
                messages=len(messages),
                estimated_cost=estimated_cost,
                json_mode=json_mode,
                stream=stream,
                asset_id=asset_id,
                attempt=attempt,
                retry_limit=retry_limit,
            )
        if outcome.json_off:
            json_mode = False
            continue
        if outcome.retry_delay is not None:
            _sleep(outcome.retry_delay)
            attempt += 1
            continue
        return outcome.text, outcome.usage, outcome.call_id, json_mode


def _chat_attempt(
    ctx: Ctx,
    *,
    stage: str,
    scope_id: str,
    model: str,
    body: str,
    messages: int,
    estimated_cost: float,
    json_mode: bool,
    stream: bool,
    asset_id: str | None,
    attempt: int,
    retry_limit: int,
) -> _Attempt:
    base_url = ctx.setting("litellm.base_url").rstrip("/")
    api_key = ctx.secrets.get(ctx.setting("litellm.api_key_name", "LITELLM_API_KEY"))
    # 流式时 timeout 只管两段输出之间的间隔；非流式时它就是整次调用的上限，代理侧还有 600 秒
    timeout = float(ctx.setting("llm.timeout_seconds", 480))
    reservation = budget.reserve(
        ctx.db,
        ctx.cfg,
        kind="llm",
        stage=stage,
        scope_id=scope_id,
        model=model,
        idempotency_key=f"llm:{scope_id}:{stage}:{uuid.uuid4().hex}",
        estimated_cost=estimated_cost,
        request_hash=sha256_text(body),
        request_meta={"json_mode": json_mode, "messages": messages},
        owner=ctx.owner,
        asset_id=asset_id,
    )
    budget.mark_submitted(ctx.db, reservation.call_id)
    # 结构化阶段一次要几十秒到几分钟：等回答期间不占数据库连接
    limits.release_db(ctx)
    started = time.monotonic()
    resp: httpx.Response | None = None
    data: dict[str, Any] | None = None
    try:
        with http.make_client(timeout) as client:
            with client.stream(
                "POST",
                f"{base_url}/v1/chat/completions",
                content=body.encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                sse = "text/event-stream" in resp.headers.get("content-type", "")
                if resp.status_code < 400 and stream and sse:
                    data = _read_stream(resp)
                else:
                    resp.read()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        budget.settle_failure(ctx.db, reservation.call_id, error=f"连接失败：{exc}")
        if attempt >= retry_limit:
            raise LLMError(f"{stage} 连接 LiteLLM 失败：{exc}") from exc
        return _Attempt(retry_delay=_retry_delay(None, attempt))
    except httpx.TransportError as exc:
        budget.mark_unknown(
            ctx.db, reservation.call_id, error=f"已提交但未拿到结果：{exc}"
        )
        raise LLMResultUnknown(
            reservation.call_id, f"{stage} 调用结果未知（{type(exc).__name__}）"
        ) from exc
    except LLMStreamError as exc:
        budget.settle_failure(ctx.db, reservation.call_id, error=str(exc))
        if attempt >= retry_limit:
            raise LLMError(f"{stage} {exc}") from exc
        return _Attempt(retry_delay=_retry_delay(None, attempt))
    latency = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        text = resp.text[:1500]
        budget.settle_failure(
            ctx.db,
            reservation.call_id,
            error=text,
            http_status=resp.status_code,
            latency_ms=latency,
        )
        if resp.status_code == 400 and json_mode:
            return _Attempt(json_off=True)
        if resp.status_code in TRANSIENT_STATUS and attempt < retry_limit:
            return _Attempt(retry_delay=_retry_delay(resp, attempt))
        raise LLMPermanentError(f"{stage} HTTP {resp.status_code}：{text[:300]}")
    if data is None:
        data = resp.json()
    usage = data.get("usage") or {}
    actual = budget.actual_llm_cost(ctx.cfg, model, usage)
    budget.settle_success(
        ctx.db,
        reservation.call_id,
        actual_cost=actual,
        usage=usage,
        response_meta={"id": data.get("id"), "model": data.get("model")},
        latency_ms=latency,
    )
    return _Attempt(text=_message_text(data), usage=usage, call_id=reservation.call_id)


def run_stage(
    ctx: Ctx,
    stage: str,
    *,
    scope_id: str,
    job_id: str,
    variables: dict[str, Any],
    images: dict[str, list[prompts.ImageInput]] | None = None,
    asset_id: str | None = None,
    extra_validate: Callable[[dict[str, Any]], list[str]] | None = None,
    normalize: Callable[[dict[str, Any]], list[str]] | None = None,
) -> StageOutput:
    """调一次结构化阶段：校验不过带错误重答。

    normalize(外壳) 在 schema 校验前执行，可以就地把只做记录的字段归位、补默认值，返回说明（写进 notes）。
    """
    model = stage_model(ctx, stage)
    estimated = budget.estimate_llm_cost(ctx.cfg, model, stage)
    full_vars = {
        "project_constraints": project_constraints(ctx),
        "schema_version": prompts.SCHEMA_VERSION,
        "job_context": {
            "job_id": job_id,
            "stage": stage,
            "scope_id": scope_id,
            "pipeline_version": ctx.pipeline_version,
        },
        **variables,
    }
    built = prompts.build(
        stage,
        job_id=job_id,
        variables=full_vars,
        images=images,
        result_schema=schemas.schema_text(stage),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": built.system},
        {"role": "user", "content": built.user_content},
    ]
    json_mode = bool(ctx.setting("llm.json_mode", True))
    max_format_retries = int(ctx.setting("llm.max_format_retries", 2))
    call_ids: list[str] = []
    usage_total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    errors: list[str] = []
    for _ in range(max_format_retries + 1):
        text, usage, call_id, json_mode = _chat_once(
            ctx,
            stage=stage,
            scope_id=scope_id,
            model=model,
            messages=messages,
            estimated_cost=estimated,
            json_mode=json_mode,
            asset_id=asset_id,
        )
        call_ids.append(call_id)
        for key in usage_total:
            usage_total[key] += int(usage.get(key) or 0)
        envelope, errors, info = parse_envelope(
            text,
            stage,
            job_id,
            normalize=normalize,
            extra_validate=extra_validate,
        )
        if envelope is not None and not errors:
            notes = list(info.get("notes") or [])
            if notes or info.get("objects", 1) > 1:
                with ctx.db.tx() as conn:
                    if notes:
                        log_event(
                            conn,
                            scope_id,
                            "stage_output_normalized",
                            {"stage": stage, "call_id": call_id, "notes": notes},
                        )
                    if info.get("objects", 1) > 1:
                        log_event(
                            conn,
                            scope_id,
                            "stage_output_repaired",
                            {
                                "stage": stage,
                                "call_id": call_id,
                                "objects": info["objects"],
                                "picked": info["picked"],
                            },
                        )
            return StageOutput(
                stage=stage,
                envelope=envelope,
                model=model,
                prompt_version=built.prompt_version,
                call_ids=call_ids,
                usage=usage_total,
                notes=notes,
            )
        raw = _save_reject(ctx, stage, scope_id, call_id, text)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                scope_id,
                "stage_output_rejected",
                {"stage": stage, "call_id": call_id, "errors": errors[:12], "raw": raw},
            )
        messages = messages + [
            {"role": "assistant", "content": text[:20000]},
            {"role": "user", "content": _feedback(errors)},
        ]
    raise StageOutputInvalid(stage, errors)
