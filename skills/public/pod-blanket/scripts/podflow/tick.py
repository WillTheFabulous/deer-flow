"""S00 / P00 运行控制（程序化）：定时巡检的唯一入口。

按方案 P00 的规则做决定：先核对停止条件、试运行窗口、预算、积压与 worker 存活，
只在满足前置时拉起一个后台 worker；空队列不等于失败，系列耗尽时提出待批准的新方向。
返回值的 decision 取 dispatch / plan / wait / needs_review / stop，与 P00 输出约定一致。
"""

from __future__ import annotations

from typing import Any

from . import bg, budget, directions, fixes, report, state
from .context import Ctx
from .db import log_event
from .stages.common import subject_label
from .stages.job import backlog_count
from .util import dumps, utcnow
from .worker import is_paused, min_call_estimate, plannable_family


def spawn_worker(
    ctx: Ctx, *, allow_generation: bool = True, source: str = "tick"
) -> dict[str, Any]:
    argv = ["worker", "--source", source]
    if not allow_generation:
        argv.append("--no-generation")
    return bg.spawn(ctx, argv, kind="worker")


def tick(ctx: Ctx, *, spawn: bool = True) -> dict[str, Any]:
    alerts: list[str] = []
    jobs: list[dict[str, Any]] = []
    decision, reason, resume = "wait", "", ""
    # 人在选款里要求的修改、人选中的裂变方向都不看试运行窗口（选中本身就是授权）
    fix = fixes.resume_waiting(ctx) if spawn else {}
    chosen = (
        directions.resume_waiting(ctx) if spawn and not fix.get("fix_started") else {}
    )
    for recovered in (fix.get("recovered"), chosen.get("recovered")):
        if any((recovered or {}).values()):
            alerts.append(
                f"回收了上次中断的遗留：{ {k: len(v) for k, v in recovered.items() if v} }"
            )
    worker = state.worker_status(ctx.db)
    trial = report.trial_window(ctx)
    require_window = bool(ctx.setting("trial.require_window_for_tick", True))
    ready = int(ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'ready'"))
    blocked = int(ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'blocked'"))
    limit = int(ctx.setting("batch.review_backlog_limit", 50))
    backlog = backlog_count(ctx)

    if is_paused(ctx):
        decision, reason, resume = (
            "stop",
            f"已暂停：{ctx.db.get_meta('pause_reason') or '未说明'}",
            "人工 podctl resume",
        )
    elif fix.get("fix_started"):
        decision = "dispatch"
        reason = f"接着改选款里要求修改的 {'、'.join(fix['assets'])}"
        jobs.append({"task_id": fix["fix_started"], "kind": "fix"})
    elif chosen.get("directions_started"):
        decision = "dispatch"
        reason = f"接着出已选的裂变方向（{len(chosen['jobs'])} 个任务）"
        jobs.append({"task_id": chosen["directions_started"], "kind": "worker"})
    elif require_window and not trial["active"]:
        decision, reason, resume = (
            "stop",
            "试运行窗口未开启或已结束",
            "人工授权后 podctl trial start",
        )
    elif worker and worker["alive"]:
        decision, reason, resume = (
            "wait",
            f"worker 正在运行（{worker['owner']}）",
            "worker 结束后下一次巡检",
        )
    else:
        recovered = state.recover_stale(ctx.db, ctx.owner)
        if any(recovered.values()):
            alerts.append(
                f"回收了上次中断的遗留：{ {k: len(v) for k, v in recovered.items() if v} }"
            )
        estimate = min_call_estimate(ctx)
        budget_ok = (
            estimate is not None and budget.can_afford(ctx.db, ctx.cfg, estimate)[0]
        )
        family_id = (
            plannable_family(ctx) if ctx.setting("batch.auto_plan", False) else None
        )
        if estimate is None:
            decision, reason, resume = (
                "stop",
                "模型单价未填写，所有付费调用都会被阻断",
                "在 podflow.yaml 的 prices 填写单价",
            )
        elif not budget_ok:
            decision, reason, resume = (
                "stop",
                "剩余预算不足以发起下一次调用",
                "预算日界线过后或人工调整上限",
            )
        elif backlog >= limit:
            # 积压时只停新增生成，已出的图仍可继续质检、预检、归档
            has_post_work = ctx.db.scalar(
                "SELECT COUNT(*) FROM assets WHERE status IN ('generated', 'qc_pass', 'distinct', 'preview_ready')"
            )
            if has_post_work and spawn:
                jobs.append(spawn_worker(ctx, allow_generation=False))
                decision = "dispatch"
            else:
                decision = "needs_review"
            reason, resume = (
                f"待人工审核积压 {backlog} ≥ {limit}，停止新增生成",
                "人工处理审核项",
            )
        elif ready or blocked:
            decision, reason = "dispatch", f"{ready} 个就绪任务、{blocked} 个可恢复任务"
            if spawn:
                jobs.append(spawn_worker(ctx))
        elif family_id:
            decision, reason = (
                "plan",
                f"队列已空，为已批准系列 {subject_label(ctx, family_id)} 补充一批",
            )
            if spawn:
                jobs.append(spawn_worker(ctx))
        else:
            exhausted = ctx.db.q(
                "SELECT family_id FROM families WHERE status = 'exhausted'"
            )
            if exhausted:
                decision = "needs_review"
                names = "、".join(subject_label(ctx, r["family_id"]) for r in exhausted)
                reason = f"系列 {names} 已耗尽，等待人工批准新方向"
                resume = "人工扩大系列范围或新增母版"
            else:
                decision, reason, resume = (
                    "wait",
                    "队列为空",
                    "新增母版 / 批准系列 / 手动 podctl plan",
                )
    snap = report.snapshot(ctx)
    result = {
        "decision": decision,
        "reason": reason,
        "resume_condition": resume,
        "jobs": jobs,
        "alerts": alerts + snap["alerts"],
        "summary": report.text_summary(snap),
        "snapshot": {
            k: snap[k]
            for k in (
                "jobs",
                "assets",
                "open_reviews",
                "budget",
                "trial",
                "worker",
                "paused",
            )
        },
    }
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "tick",
            "tick",
            {"decision": decision, "reason": reason, "spawned": len(jobs)},
        )
        ctx.db.set_meta(
            conn,
            "last_tick",
            dumps(
                {
                    "at": utcnow().isoformat(timespec="seconds"),
                    "decision": decision,
                    "reason": reason,
                }
            ),
        )
    return result
