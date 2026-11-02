"""选款时人工要求的修改：交给出图 worker，修改排在最前面。

有 worker 在跑时只排队：它每次领取都现查要修改的任务（state.claim_job 让修改先领），有空的工位就接着改；
没有就拉起一个 worker（来源 fix），它改完修改也顺带接着出确认过的方向。改好一张就发一张卡片。
"""

from __future__ import annotations

from typing import Any

from . import bg, state
from .context import Ctx


def pending(ctx: Ctx) -> list[dict[str, Any]]:
    """等着改的素材（任务已放回队列）：[{asset_id, job_id}]。"""
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT a.asset_id, a.job_id FROM assets a JOIN jobs j ON j.job_id = a.job_id"
            " WHERE a.status = 'repair_pending' AND a.human_decision = 'needs_changes'"
            " AND j.status = 'ready' ORDER BY a.asset_id"
        )
    ]


def busy(ctx: Ctx) -> bool:
    """有出图在跑（worker 租约活着，或别的 worker / fix 后台任务在跑）。"""
    worker = state.worker_status(ctx.db)
    if worker and worker["alive"]:
        return True
    return any(
        t["kind"] in ("worker", "fix") and t["task_id"] != bg.current()
        for t in bg.running(ctx)
    )


def start(ctx: Ctx, subject: str | None = None) -> dict[str, Any]:
    """没有出图在跑时，拉起一个先做修改的后台 worker；有就排队，由正在跑的 worker 接着领。"""
    items = pending(ctx)
    if not items:
        return {}
    if ctx.db.get_meta("paused") == "1":
        return {"fix_queued": [i["asset_id"] for i in items], "reason": "流水线已暂停"}
    if busy(ctx):
        return {"fix_queued": [i["asset_id"] for i in items]}
    task = bg.spawn(
        ctx,
        ["worker", "--source", "fix"],
        kind="fix",
        subject=subject or items[0]["asset_id"],
        notify=True,
    )
    return {"fix_started": task["task_id"], "assets": [i["asset_id"] for i in items]}


def resume_waiting(ctx: Ctx) -> dict[str, Any]:
    """巡检兜底：修改被打断（任务卡在 running）或排队后没人接时接着改；不看试运行窗口和积压。"""
    if ctx.db.get_meta("paused") == "1" or busy(ctx):
        return {}
    out: dict[str, Any] = {}
    if ctx.db.scalar(
        "SELECT COUNT(*) FROM assets a JOIN jobs j ON j.job_id = a.job_id"
        " WHERE a.status = 'repair_pending' AND a.human_decision = 'needs_changes'"
        " AND j.status = 'running'"
    ):
        out["recovered"] = state.recover_stale(ctx.db, ctx.owner)
    out.update(start(ctx))
    return out
