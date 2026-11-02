"""T01 生图适配：mooya LiteLLM `/v1/images/generations`，按模型族映射参数。

- Gemini（nano-banana 2.1）：vendor_params.imageSize / aspectRatio；
- gpt-image-2.5（flare / sunburst）：vendor_params.size / quality / format；
- 输入图（图生图、修复）：加 vendor_params.images（裸 base64 列表），代理见到输入图会按 `<模型>-edit` 的配置转到编辑接口；
  图生图另有一路直接用 `<模型>-edit` 名（image_generation.i2i_edit），参数与家族按原模型算，代理里两者走同一条编辑路由；
- 退役模型（image_generation.replaced_models）在发请求前换成接替模型（current_model）；
- 在 worker 里时，预留到结算这一段占一个生图名额（limits.image_slot，batch.image_concurrency 个工位共用）。

不发 X-Billing-Context：那会把费用记进 mooya 生产库的租户账本；成本只记在本引擎的账本里。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import budget, http, limits
from .context import Ctx
from .util import sha256_text

TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# LiteLLM 会把上游的 4xx 参数错误包成 500 返回：这类错误重试也不会好，按永久失败处理
PERMANENT_MARKERS = (
    "Unknown parameter",
    "HTTP 400",
    "HTTP 422",
    "invalid_request_error",
    "content_policy",
    "moderation",
)
# 带输入图时走编辑接口，它不认 format：2026-10-08 M1 实测 anyaigc 2.5 返回 Unknown parameter: 'format'
EDIT_UNSUPPORTED_PARAMS = frozenset({"format"})


def is_permanent_error(status_code: int, text: str) -> bool:
    if 400 <= status_code < 500 and status_code not in TRANSIENT_STATUS:
        return True
    return any(marker in text for marker in PERMANENT_MARKERS)


@dataclass
class GenResult:
    status: str  # ok | failed | unknown
    call_id: str | None
    data: bytes | None = None
    error: str | None = None
    transient: bool = False
    http_status: int | None = None


def model_family(model: str) -> str:
    return "gemini" if budget.base_model(model).startswith("gemini-") else "gpt"


def current_model(ctx: Ctx, model: str) -> str:
    """退役模型换成接替模型（image_generation.replaced_models），-edit 后缀跟着保留。"""
    replaced = ctx.setting("image_generation.replaced_models") or {}
    base = budget.base_model(model)
    target = replaced.get(base)
    return target + model[len(base) :] if target else model


def vendor_params(ctx: Ctx, model: str) -> dict[str, Any]:
    profile_name = ctx.setting("image_generation.format_profile", "blanket_preview")
    profile = (ctx.setting("format_profiles", {}) or {}).get(profile_name)
    if not profile:
        raise ValueError(f"format_profiles 缺少 {profile_name}")
    return dict(
        profile["gemini"] if model_family(model) == "gemini" else profile["gpt"]
    )


def build_payload(
    ctx: Ctx, model: str, prompt: str, input_images: list[bytes] | None = None
) -> dict[str, Any]:
    params = vendor_params(ctx, model)
    if input_images:
        for key in EDIT_UNSUPPORTED_PARAMS:
            params.pop(key, None)
        params["images"] = [
            base64.b64encode(img).decode("ascii") for img in input_images
        ]
    return {"model": model, "prompt": prompt, "vendor_params": params}


def _extract_image(client: httpx.Client, data: dict[str, Any]) -> bytes | None:
    items = data.get("data") or []
    if not items:
        return None
    first = items[0] or {}
    if first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if first.get("url"):
        resp = client.get(first["url"])
        resp.raise_for_status()
        return resp.content
    return None


def generate(
    ctx: Ctx,
    *,
    scope_id: str,
    stage: str,
    model: str,
    prompt: str,
    idempotency_key: str,
    slot_id: str,
    input_images: list[bytes] | None = None,
    asset_hint: str | None = None,
) -> GenResult:
    """一次付费出图。预算不足 / 价格未知时抛 budget.BudgetBlocked，不会发请求。"""
    with limits.image_slot(ctx):
        return _generate(
            ctx,
            scope_id=scope_id,
            stage=stage,
            model=model,
            prompt=prompt,
            idempotency_key=idempotency_key,
            slot_id=slot_id,
            input_images=input_images,
            asset_hint=asset_hint,
        )


def _generate(
    ctx: Ctx,
    *,
    scope_id: str,
    stage: str,
    model: str,
    prompt: str,
    idempotency_key: str,
    slot_id: str,
    input_images: list[bytes] | None,
    asset_hint: str | None,
) -> GenResult:
    estimated = budget.estimate_image_cost(ctx.cfg, model)
    payload = build_payload(ctx, model, prompt, input_images)
    shown_params = {k: v for k, v in payload["vendor_params"].items() if k != "images"}
    reservation = budget.reserve(
        ctx.db,
        ctx.cfg,
        kind="image",
        stage=stage,
        scope_id=scope_id,
        model=model,
        idempotency_key=idempotency_key,
        estimated_cost=estimated,
        request_hash=sha256_text(
            f"{model}\n{prompt}\n{sorted(shown_params.items())}\n{len(input_images or [])}"
        ),
        request_meta={
            "slot_id": slot_id,
            "prompt": prompt[:6000],
            "vendor_params": shown_params,
            "input_images": len(input_images or []),
            "asset_hint": asset_hint,
        },
        owner=ctx.owner,
        asset_id=asset_hint,
    )
    budget.mark_submitted(ctx.db, reservation.call_id)
    base_url = ctx.setting("litellm.base_url").rstrip("/")
    api_key = ctx.secrets.get(ctx.setting("litellm.api_key_name", "LITELLM_API_KEY"))
    if not api_key:
        budget.settle_failure(
            ctx.db, reservation.call_id, error="secrets.env 缺少 LiteLLM key"
        )
        return GenResult(
            status="failed",
            call_id=reservation.call_id,
            error="secrets.env 缺少 LiteLLM key",
        )
    timeout = float(ctx.setting("image_generation.timeout_seconds", 840))
    # 出一张图要一两分钟：等结果期间不占数据库连接
    limits.release_db(ctx)
    started = time.monotonic()
    try:
        with http.make_client(timeout) as client:
            resp = client.post(
                f"{base_url}/v1/images/generations",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
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
                return GenResult(
                    status="failed",
                    call_id=reservation.call_id,
                    error=f"HTTP {resp.status_code}: {text[:300]}",
                    transient=resp.status_code in TRANSIENT_STATUS
                    and not is_permanent_error(resp.status_code, text),
                    http_status=resp.status_code,
                )
            body = resp.json()
            try:
                image = _extract_image(client, body)
            except httpx.HTTPError as exc:
                # 上游已出图并计费，只是取图失败：按结果未知处理，等人工对账
                budget.mark_unknown(
                    ctx.db, reservation.call_id, error=f"出图成功但下载失败：{exc}"
                )
                return GenResult(
                    status="unknown", call_id=reservation.call_id, error=str(exc)
                )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        budget.settle_failure(ctx.db, reservation.call_id, error=f"连接失败：{exc}")
        return GenResult(
            status="failed",
            call_id=reservation.call_id,
            error=f"连接失败：{exc}",
            transient=True,
        )
    except httpx.TransportError as exc:
        budget.mark_unknown(
            ctx.db,
            reservation.call_id,
            error=f"已提交但未拿到结果：{type(exc).__name__}: {exc}",
        )
        return GenResult(
            status="unknown",
            call_id=reservation.call_id,
            error=f"{type(exc).__name__}: {exc}",
        )
    if not image:
        budget.settle_failure(
            ctx.db,
            reservation.call_id,
            error=f"响应中没有图片：{str(body)[:500]}",
            http_status=200,
        )
        return GenResult(
            status="failed",
            call_id=reservation.call_id,
            error="响应中没有图片",
            http_status=200,
        )
    budget.settle_success(
        ctx.db,
        reservation.call_id,
        actual_cost=estimated,
        usage={"images": 1},
        response_meta={"created": body.get("created"), "model": body.get("model")},
        latency_ms=latency,
    )
    return GenResult(
        status="ok", call_id=reservation.call_id, data=image, http_status=200
    )
