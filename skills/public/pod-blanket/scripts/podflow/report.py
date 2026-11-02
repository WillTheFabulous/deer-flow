"""状态快照、批次报告与日报。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import budget, labels, state
from .context import Ctx
from .stages.common import subject_label, task_label
from .stages.feedback import batch_metrics
from .stages.job import BACKLOG_TYPES, backlog_count
from .util import local_day, loads, parse_iso, utcnow


def trial_window(ctx: Ctx) -> dict[str, Any]:
    started = ctx.db.get_meta("trial_started_at")
    ends = ctx.db.get_meta("trial_ends_at")
    active = bool(started and ends and parse_iso(started) <= utcnow() < parse_iso(ends))
    return {
        "started_at": started,
        "ends_at": ends,
        "by": ctx.db.get_meta("trial_by"),
        "active": active,
    }


def _counts(ctx: Ctx, sql: str) -> dict[str, int]:
    return {r[0]: r[1] for r in ctx.db.q(sql)}


def snapshot(ctx: Ctx) -> dict[str, Any]:
    db = ctx.db
    open_reviews = _counts(
        ctx,
        "SELECT subject_type, COUNT(*) FROM reviews WHERE status = 'open' GROUP BY subject_type",
    )
    blocked = [
        dict(r)
        for r in db.q(
            "SELECT job_id, stage, blocked_reason, resume_condition FROM jobs WHERE status = 'blocked' ORDER BY job_id LIMIT 20"
        )
    ]
    failed_jobs = [
        dict(r)
        for r in db.q(
            "SELECT job_id, stage, last_error FROM jobs WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 10"
        )
    ]
    from . import shelf  # 归档模块依赖任务视图，放到用时再导入

    # 已归档任务的系列不列（工作台「系列」一行、耗尽告警）；它们在「历史任务」里
    archived = shelf.archived_seeds(ctx)
    families = [
        {**dict(r), "task": task_label(ctx, r["seed_id"])}
        for r in db.q(
            "SELECT family_id, family_version, seed_id, status, exhaustion_reason FROM families f WHERE family_version = ("
            " SELECT MAX(family_version) FROM families f2 WHERE f2.family_id = f.family_id) ORDER BY family_id"
        )
        if r["seed_id"] not in archived
    ]
    job_names = labels.jobs(ctx, [j["job_id"] for j in blocked + failed_jobs])
    for job in blocked + failed_jobs:
        job["label"] = job_names[job["job_id"]]
    budget_info = budget.summary(db, ctx.cfg)
    alerts: list[str] = []
    if budget_info["unknown_calls"]:
        alerts.append(
            f"{budget_info['unknown_calls']} 个调用结果未知，需对账（podctl review list --type call_reconcile）"
        )
    notes: list[str] = []
    if not budget_info["enforced"]:
        notes.append(
            "预算控制已关闭（budget.enforce: false）：不设上限、不计费用，只记调用次数与用量"
        )
    elif (
        budget_info["daily_cap"] <= 0
        or budget_info["total_cap"] <= 0
        or budget_info["per_task_cap"] <= 0
    ):
        alerts.append(
            "预算上限为 0：不会发起任何付费调用（在 podflow.yaml 填写 budget）"
        )
    backlog = backlog_count(ctx)
    limit = int(ctx.setting("batch.review_backlog_limit", 50))
    if backlog >= limit:
        alerts.append(f"待人工审核积压 {backlog} ≥ 上限 {limit}：已停止新增生成")
    for fam in families:
        if fam["status"] == "exhausted":
            alerts.append(
                f"系列 {subject_label(ctx, fam['family_id'])} 已耗尽：{fam['exhaustion_reason']}"
            )
    if failed_jobs:
        alerts.append(f"最近有 {len(failed_jobs)} 个任务失败")
    return {
        "notes": notes,
        "paused": db.get_meta("paused") == "1",
        "pause_reason": db.get_meta("pause_reason"),
        "trial": trial_window(ctx),
        "worker": state.worker_status(db),
        "seeds": _counts(ctx, "SELECT status, COUNT(*) FROM seeds GROUP BY status"),
        "archived_tasks": len(archived),
        "families": families,
        "batches": [
            {**dict(r), "task": labels.task(ctx, r["batch_id"])}
            for r in db.q(
                "SELECT batch_id, family_id, status, size FROM batches ORDER BY batch_id DESC LIMIT 5"
            )
        ],
        "jobs": _counts(ctx, "SELECT status, COUNT(*) FROM jobs GROUP BY status"),
        "job_stages": _counts(
            ctx,
            "SELECT stage, COUNT(*) FROM jobs WHERE status NOT IN ('complete', 'failed') GROUP BY stage",
        ),
        "assets": _counts(ctx, "SELECT status, COUNT(*) FROM assets GROUP BY status"),
        "open_reviews": open_reviews,
        "review_backlog": {
            "count": backlog,
            "limit": limit,
            "counted_types": list(BACKLOG_TYPES),
        },
        "blocked_jobs": blocked,
        "failed_jobs": failed_jobs,
        "budget": budget_info,
        "alerts": alerts,
    }


def batch_report(ctx: Ctx, batch_id: str) -> dict[str, Any]:
    batch = ctx.db.q1("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
    if not batch:
        raise ValueError(f"没有批次 {batch_id}")
    report = loads(batch["report_json"], {}) or {}
    return {
        "batch_id": batch_id,
        "family": f"{batch['family_id']}@v{batch['family_version']}",
        "status": batch["status"],
        "size": batch["size"],
        "plan": loads(batch["plan_json"], {}),
        "metrics": batch_metrics(ctx, batch_id),
        "feedback": report.get("p11"),
    }


def daily(ctx: Ctx, day: str | None = None) -> dict[str, Any]:
    day = day or local_day(ctx.setting("timezone", "Asia/Shanghai"))
    calls = [
        dict(r) for r in ctx.db.q("SELECT * FROM provider_calls WHERE day = ?", (day,))
    ]
    by_model: dict[str, dict[str, Any]] = {}
    for c in calls:
        entry = by_model.setdefault(
            c["model"],
            {"calls": 0, "succeeded": 0, "failed": 0, "unknown": 0, "cost": 0.0},
        )
        entry["calls"] += 1
        entry[c["status"]] = entry.get(c["status"], 0) + 1
        if c["cost_status"] in ("settled", "estimated", "reconciled"):
            entry["cost"] = round(entry["cost"] + (c["actual_cost"] or 0), 6)
        elif c["cost_status"] in ("reserved", "unknown"):
            entry["cost"] = round(entry["cost"] + c["reserved_cost"], 6)
    assets_today = ctx.db.q(
        "SELECT status FROM assets WHERE substr(created_at, 1, 10) >= ?", (day,)
    )
    return {
        "day": day,
        "budget": budget.summary(ctx.db, ctx.cfg),
        "calls_by_model": by_model,
        "assets_created_since_day": dict(Counter(r["status"] for r in assets_today)),
        "open_reviews": _counts(
            ctx,
            "SELECT subject_type, COUNT(*) FROM reviews WHERE status = 'open' GROUP BY subject_type",
        ),
    }


def text_summary(snap: dict[str, Any]) -> str:
    """给飞书通知用的简短中文摘要。"""
    b = snap["budget"]
    if b["enforced"]:
        money = f"预算：今日已占用 {b['today_committed']}/{b['daily_cap']} {b['currency']}，累计 {b['total_committed']}/{b['total_cap']}"
    else:
        money = f"预算控制已关闭（不计费）；今日调用 {b['calls_today']} 次"
    lines = [
        f"POD 毛毯流水线｜{b['day']}",
        money,
        f"任务：{snap['jobs'] or '无'}；素材：{snap['assets'] or '无'}",
        f"待审：{snap['review_backlog']['count']}（上限 {snap['review_backlog']['limit']}）",
    ]
    if snap["alerts"]:
        lines.append("异常：" + "；".join(snap["alerts"]))
    return "\n".join(lines)
