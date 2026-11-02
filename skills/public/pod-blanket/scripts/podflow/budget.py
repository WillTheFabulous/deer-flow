"""费用账本：调用前原子预留、调用后按实际结算，未知价格与零预算一律阻断。

一行 provider_calls 就是一笔账：
- reserved / unknown：按 reserved_cost 计入已占用（结果未知时保守计入，直到人工对账）；
- settled / estimated / reconciled：按 actual_cost 计入；
- released：不计入（调用确定没有发生或确定失败）；
- untracked：`budget.enforce: false` 时不设上限、不计费用，调用照样落账（幂等与结果未知对账仍然生效）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import config as cfgmod
from .db import DB, next_id
from .util import dumps, local_day, now_iso

COMMITTED_SQL = """
COALESCE(SUM(CASE
  WHEN cost_status IN ('settled', 'estimated', 'reconciled') THEN COALESCE(actual_cost, 0)
  WHEN cost_status IN ('reserved', 'unknown') THEN reserved_cost
  ELSE 0 END), 0)
"""


class BudgetBlocked(RuntimeError):
    """预算或价格条件不满足，不得发起付费调用。"""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Reservation:
    call_id: str
    reserved_cost: float
    day: str


def base_model(model: str) -> str:
    return model[: -len("-edit")] if model.endswith("-edit") else model


def enforced(cfg: dict[str, Any]) -> bool:
    """预算控制总开关；关闭时不查上限、不要求单价，费用记为 untracked。"""
    return bool(cfgmod.get(cfg, "budget.enforce", True))


def _price_entry(cfg: dict[str, Any], kind: str, model: str) -> dict[str, Any]:
    table = cfgmod.get(cfg, f"prices.{kind}", {}) or {}
    entry = table.get(base_model(model))
    if not isinstance(entry, dict):
        raise BudgetBlocked("price_unknown", f"{model} 没有配置单价")
    return entry


def estimate_llm_cost(cfg: dict[str, Any], model: str, stage: str) -> float:
    if not enforced(cfg):
        return 0.0
    entry = _price_entry(cfg, "chat", model)
    pin, pout = entry.get("input_per_mtok"), entry.get("output_per_mtok")
    if pin is None or pout is None:
        raise BudgetBlocked("price_unknown", f"{model} 的 token 单价未填写")
    estimates = cfgmod.get(cfg, "prices.estimate_tokens", {}) or {}
    est = (
        estimates.get(stage)
        or estimates.get("default")
        or {"input": 12000, "output": 6000}
    )
    return round(est["input"] / 1e6 * float(pin) + est["output"] / 1e6 * float(pout), 6)


def actual_llm_cost(
    cfg: dict[str, Any], model: str, usage: dict[str, Any] | None
) -> float | None:
    if not usage or not enforced(cfg):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None or completion is None:
        return None
    entry = _price_entry(cfg, "chat", model)
    return round(
        prompt / 1e6 * float(entry["input_per_mtok"])
        + completion / 1e6 * float(entry["output_per_mtok"]),
        6,
    )


def estimate_image_cost(cfg: dict[str, Any], model: str, n: int = 1) -> float:
    if not enforced(cfg):
        return 0.0
    entry = _price_entry(cfg, "image", model)
    per_image = entry.get("per_image")
    if per_image is None:
        raise BudgetBlocked("price_unknown", f"{base_model(model)} 的单张价格未填写")
    return round(float(per_image) * n, 6)


def _committed(
    conn: sqlite3.Connection, where: str = "1=1", params: tuple = ()
) -> float:
    return float(
        conn.execute(
            f"SELECT {COMMITTED_SQL} FROM provider_calls WHERE {where}", params
        ).fetchone()[0]
    )


def reserve(
    db: DB,
    cfg: dict[str, Any],
    *,
    kind: str,
    stage: str,
    scope_id: str,
    model: str,
    idempotency_key: str,
    estimated_cost: float,
    request_hash: str | None = None,
    request_meta: dict[str, Any] | None = None,
    owner: str | None = None,
    asset_id: str | None = None,
) -> Reservation:
    """在一个 BEGIN IMMEDIATE 事务里核对三道上限并写入预留行：并发调用不会合计超限。"""
    budget = cfgmod.get(cfg, "budget", {}) or {}
    currency = budget.get("currency") or "CNY"
    daily_cap = float(budget.get("daily_cap") or 0)
    total_cap = float(budget.get("total_cap") or 0)
    per_task_cap = float(budget.get("per_task_cap") or 0)
    day = local_day(cfgmod.get(cfg, "timezone", "Asia/Shanghai"))
    enforce = enforced(cfg)
    with db.tx() as conn:
        if conn.execute(
            "SELECT 1 FROM provider_calls WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone():
            raise BudgetBlocked(
                "duplicate_request", f"幂等键 {idempotency_key} 已存在，不再重复提交"
            )
        if not enforce:
            pass
        elif daily_cap <= 0 or total_cap <= 0 or per_task_cap <= 0:
            raise BudgetBlocked("budget_zero", "预算上限为 0，不接收新的付费调用")
        else:
            today = _committed(conn, "day = ?", (day,))
            if today + estimated_cost > daily_cap:
                raise BudgetBlocked(
                    "daily_cap",
                    f"今日已占用 {today:.4f} + 预估 {estimated_cost:.4f} > 日上限 {daily_cap}",
                )
            total = _committed(conn)
            if total + estimated_cost > total_cap:
                raise BudgetBlocked(
                    "total_cap",
                    f"累计已占用 {total:.4f} + 预估 {estimated_cost:.4f} > 累计上限 {total_cap}",
                )
            scope_used = _committed(conn, "scope_id = ?", (scope_id,))
            if scope_used + estimated_cost > per_task_cap:
                raise BudgetBlocked(
                    "per_task_cap",
                    f"{scope_id} 已占用 {scope_used:.4f} + 预估 {estimated_cost:.4f} > 单任务上限 {per_task_cap}",
                )
        call_id = next_id(conn, "call")
        conn.execute(
            "INSERT INTO provider_calls(call_id, kind, stage, scope_id, asset_id, model, idempotency_key, request_hash,"
            " request_meta_json, status, owner, day, currency, reserved_cost, cost_status, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                kind,
                stage,
                scope_id,
                asset_id,
                model,
                idempotency_key,
                request_hash,
                dumps(request_meta or {}),
                owner,
                day,
                currency,
                estimated_cost,
                "reserved" if enforce else "untracked",
                now_iso(),
            ),
        )
    return Reservation(call_id=call_id, reserved_cost=estimated_cost, day=day)


def mark_submitted(db: DB, call_id: str) -> None:
    """请求发出前先落库：之后任何中断都能识别为「已提交、结果未知」。"""
    with db.tx() as conn:
        conn.execute(
            "UPDATE provider_calls SET status = 'submitted', submitted_at = ? WHERE call_id = ?",
            (now_iso(), call_id),
        )


def settle_success(
    db: DB,
    call_id: str,
    *,
    actual_cost: float | None,
    usage: dict[str, Any] | None = None,
    http_status: int | None = 200,
    response_meta: dict[str, Any] | None = None,
    latency_ms: int | None = None,
) -> None:
    with db.tx() as conn:
        row = conn.execute(
            "SELECT reserved_cost, cost_status FROM provider_calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        if row["cost_status"] == "untracked":
            cost_status, cost = "untracked", None
        else:
            cost_status = "settled" if actual_cost is not None else "estimated"
            cost = actual_cost if actual_cost is not None else row["reserved_cost"]
        conn.execute(
            "UPDATE provider_calls SET status = 'succeeded', http_status = ?, actual_cost = ?, cost_status = ?,"
            " usage_json = ?, response_meta_json = ?, finished_at = ?, latency_ms = ? WHERE call_id = ?",
            (
                http_status,
                cost,
                cost_status,
                dumps(usage or {}),
                dumps(response_meta or {}),
                now_iso(),
                latency_ms,
                call_id,
            ),
        )


def settle_failure(
    db: DB,
    call_id: str,
    *,
    error: str,
    http_status: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """上游明确返回失败：视为未计费，释放预留。"""
    with db.tx() as conn:
        conn.execute(
            "UPDATE provider_calls SET status = 'failed', http_status = ?, error = ?, actual_cost = 0,"
            " cost_status = 'released', finished_at = ?, latency_ms = ? WHERE call_id = ?",
            (http_status, error[:2000], now_iso(), latency_ms, call_id),
        )


def mark_unknown(db: DB, call_id: str, *, error: str) -> None:
    """已提交但拿不到确定结果：保留预留额，等人工对账，不自动重付费。"""
    with db.tx() as conn:
        conn.execute(
            "UPDATE provider_calls SET status = 'unknown', cost_status = 'unknown', error = ?, finished_at = ?"
            " WHERE call_id = ?",
            (error[:2000], now_iso(), call_id),
        )


def release_unsubmitted(db: DB, call_id: str, *, error: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE provider_calls SET status = 'failed', error = ?, actual_cost = 0, cost_status = 'released',"
            " finished_at = ? WHERE call_id = ? AND status = 'reserved'",
            (error[:2000], now_iso(), call_id),
        )


def summary(db: DB, cfg: dict[str, Any]) -> dict[str, Any]:
    budget = cfgmod.get(cfg, "budget", {}) or {}
    day = local_day(cfgmod.get(cfg, "timezone", "Asia/Shanghai"))
    conn = db.conn()

    def committed(where: str = "1=1", params: tuple = ()) -> float:
        return round(_committed(conn, where, params), 6)

    def by_status(where: str, params: tuple = ()) -> dict[str, float]:
        rows = conn.execute(
            f"SELECT cost_status, COALESCE(SUM(CASE WHEN cost_status IN ('settled','estimated','reconciled')"
            f" THEN COALESCE(actual_cost,0) ELSE reserved_cost END), 0) AS amount, COUNT(*) AS n"
            f" FROM provider_calls WHERE {where} GROUP BY cost_status",
            params,
        ).fetchall()
        return {
            r["cost_status"]: {"amount": round(r["amount"], 6), "calls": r["n"]}
            for r in rows
        }

    daily_cap = float(budget.get("daily_cap") or 0)
    total_cap = float(budget.get("total_cap") or 0)
    today_used = committed("day = ?", (day,))
    total_used = committed()
    return {
        "enforced": enforced(cfg),
        "currency": budget.get("currency") or "CNY",
        "day": day,
        "daily_cap": daily_cap,
        "total_cap": total_cap,
        "per_task_cap": float(budget.get("per_task_cap") or 0),
        "today_committed": today_used,
        "total_committed": total_used,
        "today_remaining": round(max(daily_cap - today_used, 0), 6),
        "total_remaining": round(max(total_cap - total_used, 0), 6),
        "today_by_status": by_status("day = ?", (day,)),
        "unknown_calls": conn.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE status = 'unknown'"
        ).fetchone()[0],
        "calls_today": conn.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE day = ?", (day,)
        ).fetchone()[0],
    }


def can_afford(db: DB, cfg: dict[str, Any], amount: float) -> tuple[bool, str]:
    """tick 用的粗检查：剩余额度是否还够一次最小调用（真正的硬限制在 reserve 里）。"""
    if not enforced(cfg):
        return True, "not_enforced"
    s = summary(db, cfg)
    if s["daily_cap"] <= 0 or s["total_cap"] <= 0 or s["per_task_cap"] <= 0:
        return False, "budget_zero"
    if s["today_remaining"] < amount:
        return False, "daily_cap"
    if s["total_remaining"] < amount:
        return False, "total_cap"
    return True, "ok"
