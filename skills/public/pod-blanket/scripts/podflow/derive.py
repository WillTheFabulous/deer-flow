"""二次大变化的触发（2026-10-27）：一批小变化出完后，以换了主体的小变化出的图为底图再策划一轮单字段大改。

- 开关 plan.derived.enabled（代码与样例缺省关）；个数 plan.derived.count、plan.derived.per_parent，策划本身在
  stages/plan.py 的 plan_derived。
- 只自动接按换主体规则策划的批次（plan_json.small_swap，2026-10-27 起小变化分组开着时才有），本身是二次批次的
  不再派生；表 derived_rounds（主键 source_batch_id）记每个原批次派生过没有，一个原批次自动只派生一次。
  手动 `plan --derive-from B…` 不看这张表（会补一行记录），同一个底图上一样的大改由计划指纹去重。
- due()：原批次 planned / running / done、小变化任务全部结束（complete / failed）且至少一个出完、还没有记录、
  任务没归档、流水线没暂停；kick()：有 due、没有 kind=derive 的后台在跑时拉起 `plan --derive-due`（单实例），
  巡检（cli._auto_tick）和出图 worker 的反馈循环都调。
- run_due() 逐个认领（先插记录行，插不进说明别的进程认领了）→ plan_derived → 开着方向自动确认时全部确认、开始出图。
"""

from __future__ import annotations

from typing import Any

from . import bg, directions
from .config import ConfigError
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .stages.plan import SMALL, derived_settings, plan_derived, preview_derived
from .util import dumps, loads, now_iso

KIND = "derive"
AUTO_BY = "system:auto"
# 原批次还在这几种状态时才看（proposed / failed / empty / superseded / cancelled 的不派生）
SOURCE_STATUSES = ("planned", "running", "done")


def enabled(ctx: Ctx) -> bool:
    return derived_settings(ctx)["enabled"]


def _archived(ctx: Ctx) -> set[str]:
    from . import shelf  # 归档模块依赖任务视图，放到用时再导入

    return shelf.archived_families(ctx)


def _small_jobs(ctx: Ctx, batch_id: str) -> list[str]:
    """原批次里小变化任务的状态（没建任务的候选不算）。"""
    statuses = []
    for row in ctx.db.q(
        "SELECT v.plan_json, j.status FROM variants v JOIN jobs j ON j.variant_id = v.variant_id"
        " WHERE v.batch_id = ?",
        (batch_id,),
    ):
        if (loads(row["plan_json"], {}) or {}).get("kind") == SMALL:
            statuses.append(row["status"])
    return statuses


def due(ctx: Ctx) -> list[str]:
    """该自动派生二次大变化的原批次（按批次号）。开关关着、暂停时为空。"""
    if not enabled(ctx) or ctx.db.get_meta("paused") == "1":
        return []
    marks = ",".join("?" for _ in SOURCE_STATUSES)
    rows = ctx.db.q(
        f"SELECT b.batch_id, b.family_id, b.plan_json FROM batches b WHERE b.status IN ({marks})"
        " AND NOT EXISTS (SELECT 1 FROM derived_rounds d WHERE d.source_batch_id = b.batch_id)"
        " ORDER BY b.batch_id",
        SOURCE_STATUSES,
    )
    archived = _archived(ctx)
    out = []
    for row in rows:
        plan = loads(row["plan_json"], {}) or {}
        if not plan.get("small_swap") or plan.get("derived_from"):
            continue
        if row["family_id"] in archived:
            continue
        statuses = _small_jobs(ctx, row["batch_id"])
        if not statuses or "complete" not in statuses:
            continue
        if all(s in ("complete", "failed") for s in statuses):
            out.append(row["batch_id"])
    return out


def kick(ctx: Ctx) -> dict[str, Any] | None:
    """有该派生的批次、又没有派生进程在跑时，拉起一个后台 `plan --derive-due`。"""
    try:
        if not due(ctx):
            return None
    except (ConfigError, StageError):
        return None
    if any(t["kind"] == KIND for t in bg.running(ctx)):
        return None
    notify = str(ctx.setting("notify.style", "full") or "full") == "full"
    return bg.spawn(ctx, ["plan", "--derive-due"], kind=KIND, notify=notify)


def _claim(ctx: Ctx, source: str, family_id: str, by: str) -> bool:
    """先插一行记录认领这个原批次；已经有记录（别的进程认领过或派生过）返回 False。"""
    with ctx.db.tx() as conn:
        if conn.execute(
            "SELECT 1 FROM derived_rounds WHERE source_batch_id = ?", (source,)
        ).fetchone():
            return False
        conn.execute(
            "INSERT INTO derived_rounds(source_batch_id, family_id, status, created_by, created_at, updated_at)"
            " VALUES(?, ?, 'planning', ?, ?, ?)",
            (source, family_id, by, now_iso(), now_iso()),
        )
    return True


def _finish(
    ctx: Ctx,
    source: str,
    family_id: str,
    status: str,
    *,
    by: str,
    batch_id: str | None = None,
    parents: list[str] | None = None,
    reason: str | None = None,
) -> None:
    with ctx.db.tx() as conn:
        updated = conn.execute(
            "UPDATE derived_rounds SET status = ?, batch_id = ?, parents_json = ?, reason = ?, updated_at = ?"
            " WHERE source_batch_id = ?",
            (status, batch_id, dumps(parents or []), reason, now_iso(), source),
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO derived_rounds(source_batch_id, family_id, status, batch_id, parents_json, reason,"
                " created_by, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    family_id,
                    status,
                    batch_id,
                    dumps(parents or []),
                    reason,
                    by,
                    now_iso(),
                    now_iso(),
                ),
            )
        log_event(
            conn,
            source,
            "derived_planned" if status == "planned" else "derived_skipped",
            {"batch_id": batch_id, "status": status, "reason": reason, "by": by},
        )


def run_one(
    ctx: Ctx,
    source: str,
    *,
    by: str,
    parents: list[str] | None = None,
    count: int | None = None,
    claimed: bool = False,
) -> dict[str, Any]:
    """给一个原批次策划二次大变化，开着方向自动确认时全部确认、开始出图；结果写进 derived_rounds。"""
    row = ctx.db.q1("SELECT family_id FROM batches WHERE batch_id = ?", (source,))
    if not row:
        raise StageError(f"没有批次 {source}")
    family_id = row["family_id"]
    if not claimed:
        # 手动派生：已经归档的任务先调出来，任务列表里马上能看到它在策划
        from . import shelf

        shelf.touch(ctx, source, reason="二次大变化")
    try:
        plan = plan_derived(ctx, source, parents=parents, count=count)
    except StageError as exc:
        _finish(ctx, source, family_id, "skipped", by=by, reason=str(exc)[:300])
        raise
    except Exception as exc:
        _finish(
            ctx,
            source,
            family_id,
            "failed",
            by=by,
            reason=f"{type(exc).__name__}：{exc}"[:300],
        )
        raise
    _finish(
        ctx,
        source,
        family_id,
        "planned" if plan["status"] == "proposed" else "skipped",
        by=by,
        batch_id=plan["batch_id"],
        parents=plan.get("parents"),
        reason=None if plan["status"] == "proposed" else "这一轮没出方向",
    )
    plan.update(directions.auto_confirm(ctx, plan))
    return plan


def run_due(ctx: Ctx, *, by: str = AUTO_BY) -> dict[str, Any]:
    """把该派生的原批次逐个派生（认领到的才做）：{planned: [结果], skipped: [{source, reason}]}。"""
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for source in due(ctx):
        row = ctx.db.q1("SELECT family_id FROM batches WHERE batch_id = ?", (source,))
        if not row or not _claim(ctx, source, row["family_id"], by):
            continue
        bg.step(ctx, f"二次大变化：{source}")
        try:
            planned.append(run_one(ctx, source, by=by, claimed=True))
        except StageError as exc:
            skipped.append({"source": source, "reason": str(exc)[:300]})
    return {"planned": planned, "skipped": skipped}


def preview(
    ctx: Ctx,
    source: str,
    *,
    parents: list[str] | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    return preview_derived(ctx, source, parents=parents, count=count)


def status(ctx: Ctx) -> dict[str, Any]:
    """派生记录与现在该派生的批次（doctor / 命令输出用）。"""
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT source_batch_id, family_id, status, batch_id, reason, updated_at FROM derived_rounds"
            " ORDER BY source_batch_id DESC LIMIT 20"
        )
    ]
    return {
        "enabled": enabled(ctx),
        "settings": derived_settings(ctx),
        "due": due(ctx),
        "rounds": rows,
    }
