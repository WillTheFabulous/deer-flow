"""裂变方向挑选：策划出的候选方向由人勾选，确认后只给选中的建任务，并在后台一直出到这些方向全部出完。

- 勾选状态存在批次的 plan_json.picked；确认时选中的建任务与出图槽位，其余记「人工没选」，下次策划时作为排除项；
- 确认过的方向在开始出图前可以停掉（cancel）：任务删掉，方向记「已停掉」，照样算做过的方向；
- 系列还在待认可时，确认方向即一并认可（只审一次）；
- 开着 batch.auto_confirm_directions 时，人发起的每轮策划一出候选就由程序全部确认（auto_confirm），不等人挑；
- 选中的方向不看连续运行窗口：出图 worker 有活一直跑（多条工位，每次领取都现查确认过的方向），
  worker 交接、巡检、恢复运行时接上，出完（或中途停下）才发一张卡片。
"""

from __future__ import annotations

import re
from typing import Any

from . import bg, feishu_table, fixes, state
from .config import ConfigError
from .context import Ctx
from .db import log_event, next_id
from .stages import seed as seedmod
from .stages.common import (
    StageError,
    fanout_slots,
    get_family,
    subject_label,
    variant_modes,
)
from .stages.job import backlog_count
from .stages.plan import (
    STATE_LABELS,
    direction_records,
    field_based,
    legacy_reason,
    planning_family,
    proposed_batches,
)
from .util import dumps, loads, now_iso

SELECTED = "selected"
REJECTED = "rejected"
# 确认过、还没开始出图就停掉的方向（任务删掉，方向留着记录）
CANCELLED = "cancelled"
# 程序自动确认时记的确认人（动态、挑选卡上显示「自动」）
AUTO_BY = "system:auto"

_DONE_TEXT = {
    "planned": "确认过",
    "running": "确认过",
    "done": "出完了",
    "rejected": "都没选中",
    "superseded": "被新一轮策划取代",
    "empty": "没有候选方向",
    "failed": "策划失败",
    "planning": "还在策划",
    "cancelled": "全部停掉了",
}


def _batch_row(conn, batch_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not row:
        raise StageError(f"没有批次 {batch_id}")
    batch = dict(row)
    batch["plan"] = loads(batch.pop("plan_json"), {}) or {}
    return batch


def _require_proposed(batch: dict[str, Any]) -> None:
    if batch["status"] != "proposed":
        raise StageError(
            f"{batch['batch_id']} {_DONE_TEXT.get(batch['status'], batch['status'])}，不能再挑"
        )


def _candidates(conn, batch_id: str) -> list[dict[str, Any]]:
    out = []
    rows = conn.execute(
        "SELECT variant_id, axis, plan_json FROM variants WHERE batch_id = ? ORDER BY variant_id",
        (batch_id,),
    ).fetchall()
    for i, row in enumerate(rows, 1):
        plan = loads(row["plan_json"], {}) or {}
        out.append(
            {
                "index": i,
                "variant_id": row["variant_id"],
                "title": plan.get("title") or row["axis"] or row["variant_id"],
                "task_type": plan.get("task_type"),
                "kind": plan.get("kind"),
                "aspects": plan.get("aspects") or [],
                "brief": plan.get("brief") or [],
                "keep": plan.get("keep") or "",
                "axis": row["axis"],
                "selection": (plan.get("selection") or {}).get("status") or "pending",
            }
        )
    return out


def parse_refs(text: str | None) -> list[str]:
    """「1,3 5、V0012」→ ["1", "3", "5", "V0012"]。"""
    return [p for p in re.split(r"[\s,，、;；]+", text or "") if p]


# 「全选」：这一批的全部方向（挑选卡的全选 / 全不选按钮、POD 对话里的「B006 全选」）
ALL_REFS = frozenset({"ALL", "全部", "全选"})


def _resolve(cands: list[dict[str, Any]], refs: list[str]) -> list[str]:
    """序号（从 1 起）、V 编号或 all（全部）→ variant_id；按候选顺序、去重。"""
    by_id = {c["variant_id"] for c in cands}
    chosen: set[str] = set()
    for ref in refs:
        ref = str(ref).strip().upper()
        if ref in ALL_REFS:
            chosen |= by_id
        elif ref.isdigit() and 1 <= int(ref) <= len(cands):
            chosen.add(cands[int(ref) - 1]["variant_id"])
        elif ref in by_id:
            chosen.add(ref)
        else:
            raise StageError(
                f"这一批没有方向 {ref}：用序号 1–{len(cands)}、V 编号或 all（全部）"
            )
    return [c["variant_id"] for c in cands if c["variant_id"] in chosen]


def show(ctx: Ctx, batch_id: str) -> dict[str, Any]:
    conn = ctx.db.conn()
    batch = _batch_row(conn, batch_id)
    cands = _candidates(conn, batch_id)
    if batch["status"] == "proposed":
        picked = [
            v
            for v in batch["plan"].get("picked") or []
            if any(c["variant_id"] == v for c in cands)
        ]
    else:
        picked = [c["variant_id"] for c in cands if c["selection"] == SELECTED]
    jobs = {
        r["variant_id"]: (r["job_id"], r["status"])
        for r in ctx.db.q(
            "SELECT variant_id, job_id, status FROM jobs WHERE batch_id = ?",
            (batch_id,),
        )
    }
    for c in cands:
        c["picked"] = c["variant_id"] in picked
        job = jobs.get(c["variant_id"])
        c["job_id"], c["job_status"] = job if job else (None, None)
    return {
        "batch_id": batch_id,
        "family_id": batch["family_id"],
        "family_version": batch["family_version"],
        "status": batch["status"],
        "hint": batch["plan"].get("hint"),
        "picked": picked,
        "selection": batch["plan"].get("selection"),
        "candidates": cands,
    }


def pick(ctx: Ctx, batch_id: str, ref: str, *, on: bool = True) -> dict[str, Any]:
    """勾选 / 取消一个方向（序号或 V 编号），all 是全部勾上 / 全部取消；确认前可以反复改。"""
    with ctx.db.tx() as conn:
        batch = _batch_row(conn, batch_id)
        _require_proposed(batch)
        cands = _candidates(conn, batch_id)
        chosen = set(_resolve(cands, [ref]))
        picked = [v for v in batch["plan"].get("picked") or [] if v not in chosen]
        if on:
            picked += list(chosen)
        order = [c["variant_id"] for c in cands]
        batch["plan"]["picked"] = sorted(set(picked), key=order.index)
        conn.execute(
            "UPDATE batches SET plan_json = ?, updated_at = ? WHERE batch_id = ?",
            (dumps(batch["plan"]), now_iso(), batch_id),
        )
    return show(ctx, batch_id)


def _close_review(conn, batch_id: str, decision: str, by: str, note: str) -> None:
    conn.execute(
        "UPDATE reviews SET status = 'decided', decision = ?, decided_by = ?, decided_at = ?, note = ?"
        " WHERE subject_type = 'plan_selection' AND subject_id = ? AND status = 'open'",
        (decision, by, now_iso(), note, batch_id),
    )


def confirm(
    ctx: Ctx,
    batch_id: str,
    *,
    by: str,
    pick: list[str] | None = None,
    start: bool = False,
) -> dict[str, Any]:
    """按勾选（或 pick 给的序号 / V 编号）确认：选中的建任务出图，其余记人工没选；系列待认可时一并认可。"""
    conn = ctx.db.conn()
    batch = _batch_row(conn, batch_id)
    _require_proposed(batch)
    cands = _candidates(conn, batch_id)
    if pick:
        chosen = _resolve(cands, pick)
    else:
        chosen = [
            c["variant_id"]
            for c in cands
            if c["variant_id"] in (batch["plan"].get("picked") or [])
        ]
    if not chosen:
        raise StageError(
            "还没选方向：先勾选要出图的方向（或用 --pick 1,3）；都不想要就「换一批」"
        )
    if not fanout_slots(ctx):
        raise StageError("image_generation.fanout 没有启用的模型")
    family_id, version = batch["family_id"], batch["family_version"]
    fam = get_family(ctx, family_id, version)
    if fam["status"] not in ("approved", "pending"):
        raise StageError(
            f"这批方向基于 {family_id} v{version}，这个版本已经「{fam['status']}」：请重新策划"
        )
    family_approved = False
    if fam["status"] == "pending":
        seedmod.decide_family(
            ctx,
            family_id,
            approve=True,
            by=by,
            note=("自动" if by == AUTO_BY else "") + "确认裂变方向时一并认可",
        )
        family_approved = True
    jobs: list[str] = []
    rejected: list[str] = []
    images: dict[str, int] = {}
    with ctx.db.tx() as conn:
        row = _batch_row(conn, batch_id)
        _require_proposed(row)
        for c in cands:
            vrow = conn.execute(
                "SELECT plan_json FROM variants WHERE variant_id = ?",
                (c["variant_id"],),
            ).fetchone()
            vplan = loads(vrow["plan_json"], {}) or {}
            status = SELECTED if c["variant_id"] in chosen else REJECTED
            vplan["selection"] = {"status": status, "by": by, "at": now_iso()}
            conn.execute(
                "UPDATE variants SET plan_json = ? WHERE variant_id = ?",
                (dumps(vplan), c["variant_id"]),
            )
            if status == REJECTED:
                rejected.append(c["variant_id"])
                continue
            job_id = next_id(conn, "job")
            conn.execute(
                "INSERT INTO jobs(job_id, variant_id, batch_id, stage, status, pipeline_version, created_at, updated_at)"
                " VALUES(?, ?, ?, 'S06', 'ready', ?, ?, ?)",
                (
                    job_id,
                    c["variant_id"],
                    batch_id,
                    ctx.pipeline_version,
                    now_iso(),
                    now_iso(),
                ),
            )
            # 小变化只做图生图（保住构图），大变化按 image_generation.modes；S06 开工前还会按当时的配置再核对
            slots = fanout_slots(ctx, variant_modes(ctx, vplan))
            for slot_id, model, _ in slots:
                conn.execute(
                    "INSERT INTO job_slots(job_id, slot_id, model, status, updated_at) VALUES(?, ?, ?, 'pending', ?)",
                    (job_id, slot_id, model, now_iso()),
                )
            images[c["variant_id"]] = len(slots)
            jobs.append(job_id)
        row["plan"]["picked"] = chosen
        row["plan"]["selection"] = {
            "by": by,
            "at": now_iso(),
            "picked": chosen,
            "rejected": rejected,
        }
        conn.execute(
            "UPDATE batches SET status = 'planned', size = ?, plan_json = ?, updated_at = ? WHERE batch_id = ?",
            (len(chosen), dumps(row["plan"]), now_iso(), batch_id),
        )
        titles = [c["title"] for c in cands if c["variant_id"] in chosen]
        _close_review(
            conn,
            batch_id,
            "approved",
            by,
            f"选了 {len(chosen)} 个方向：{'、'.join(titles)}",
        )
        log_event(
            conn,
            batch_id,
            "directions_selected",
            {
                "family_id": family_id,
                "picked": chosen,
                "rejected": len(rejected),
                "by": by,
            },
        )
    result: dict[str, Any] = {
        "batch_id": batch_id,
        "family_id": family_id,
        "family_version": version,
        "family_approved": family_approved,
        "picked": chosen,
        "rejected": rejected,
        "jobs": jobs,
        "images_per_direction": images,
        "images": sum(images.values()),
    }
    # 候选表按出图槽位预留「未生成」的行（table.reserve_rows）
    feishu_table.kick(ctx)
    if start:
        result.update(start_runs(ctx))
    return result


def auto_confirm_enabled(ctx: Ctx) -> bool:
    """策划完是否由程序全部确认并开始出图（batch.auto_confirm_directions）。"""
    return bool(ctx.setting("batch.auto_confirm_directions", False))


def auto_confirm(ctx: Ctx, plan: dict[str, Any]) -> dict[str, Any]:
    """开关开着时，把 plan_batch 刚策划出的候选方向全部确认并开始出图（系列待认可时一并认可）。

    没开、这一轮没出方向时返回 {}。确认没成功只记原因：批次留在待挑选，卡片退回「去挑选」，
    拆解 / 策划本身照样算成功。
    """
    if not auto_confirm_enabled(ctx) or plan.get("status") != "proposed":
        return {}
    try:
        result = confirm(ctx, plan["batch_id"], by=AUTO_BY, pick=["all"], start=True)
    except (StageError, ConfigError) as exc:
        return {"auto_confirm_error": str(exc)[:300]}
    limit = int(ctx.setting("batch.review_backlog_limit", 50))
    backlog = backlog_count(ctx)
    if backlog >= limit:
        # 待审积压到上限时新任务出图前会停下，选款降下来后巡检自动接上：卡片上要说清楚
        result["backlog"] = {"count": backlog, "limit": limit}
    return {"auto_confirm": result}


def cancel(
    ctx: Ctx,
    batch_id: str,
    refs: list[str] | None = None,
    *,
    kinds: list[str] | None = None,
    by: str,
) -> dict[str, Any]:
    """停掉确认过、还没开始出图的方向（序号 / V 编号 / all，可按类型 small / big / combo 筛）。

    只停排队中、没有图、没发过任何调用的任务：任务和出图槽位删掉，方向记「已停掉」；已经开始的跳过并说明。
    一个任务都不剩的批次记「全部停掉了」（不再等它出反馈报告）。
    """
    with ctx.db.tx() as conn:
        batch = _batch_row(conn, batch_id)
        if batch["status"] == "proposed":
            raise StageError(
                f"{batch_id} 还没确认：不勾这些方向就行，或点「都不满意，换一批」"
            )
        if batch["status"] not in ("planned", "running"):
            raise StageError(
                f"{batch_id} {_DONE_TEXT.get(batch['status'], batch['status'])}，没有排队中的任务可停"
            )
        cands = _candidates(conn, batch_id)
        chosen = (
            set(_resolve(cands, refs)) if refs else {c["variant_id"] for c in cands}
        )
        targets = [
            c
            for c in cands
            if c["variant_id"] in chosen
            and c["selection"] == SELECTED
            and (not kinds or c["kind"] in kinds)
        ]
        cancelled, jobs, skipped = [], [], []
        for c in targets:
            job = conn.execute(
                "SELECT job_id, status FROM jobs WHERE variant_id = ?",
                (c["variant_id"],),
            ).fetchone()
            if job is None:
                skipped.append(
                    {"variant_id": c["variant_id"], "reason": "没有出图任务"}
                )
                continue
            # 先按「排队中」锁住这一行：worker 同时在领这个任务时，谁先提交谁算
            locked = conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'ready'",
                (now_iso(), job["job_id"]),
            ).rowcount
            started = (
                locked != 1
                or conn.execute(
                    "SELECT 1 FROM assets WHERE job_id = ?", (job["job_id"],)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM provider_calls WHERE scope_id = ?", (job["job_id"],)
                ).fetchone()
            )
            if started:
                skipped.append(
                    {
                        "variant_id": c["variant_id"],
                        "reason": f"{job['job_id']} 已经开始出图",
                    }
                )
                continue
            conn.execute("DELETE FROM job_slots WHERE job_id = ?", (job["job_id"],))
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job["job_id"],))
            row = conn.execute(
                "SELECT plan_json FROM variants WHERE variant_id = ?",
                (c["variant_id"],),
            ).fetchone()
            vplan = loads(row["plan_json"], {}) or {}
            vplan["selection"] = {"status": CANCELLED, "by": by, "at": now_iso()}
            conn.execute(
                "UPDATE variants SET plan_json = ? WHERE variant_id = ?",
                (dumps(vplan), c["variant_id"]),
            )
            cancelled.append(c["variant_id"])
            jobs.append(job["job_id"])
        remaining = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
        )
        if cancelled:
            plan = batch["plan"]
            plan["picked"] = [v for v in plan.get("picked") or [] if v not in cancelled]
            plan["cancelled"] = (plan.get("cancelled") or []) + [
                {"variant_id": v, "job_id": j, "by": by, "at": now_iso()}
                for v, j in zip(cancelled, jobs)
            ]
            conn.execute(
                "UPDATE batches SET status = ?, size = ?, plan_json = ?, updated_at = ? WHERE batch_id = ?",
                (
                    batch["status"] if remaining else CANCELLED,
                    remaining,
                    dumps(plan),
                    now_iso(),
                    batch_id,
                ),
            )
            log_event(
                conn,
                batch_id,
                "directions_cancelled",
                {"variants": cancelled, "jobs": jobs, "by": by},
            )
    if cancelled:
        # 停掉的方向在候选表里预留的行删掉
        feishu_table.kick(ctx)
    return {
        "batch_id": batch_id,
        "cancelled": cancelled,
        "jobs": jobs,
        "skipped": skipped,
        "remaining_jobs": remaining,
    }


def reject(
    ctx: Ctx, batch_id: str, *, by: str, replan: bool = False, notify: bool = True
) -> dict[str, Any]:
    """这一批都不要：全部记人工没选（下次策划时排除）；replan 时在后台马上再策划一批。"""
    with ctx.db.tx() as conn:
        batch = _batch_row(conn, batch_id)
        _require_proposed(batch)
        rows = conn.execute(
            "SELECT variant_id, plan_json FROM variants WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        for row in rows:
            vplan = loads(row["plan_json"], {}) or {}
            vplan["selection"] = {"status": REJECTED, "by": by, "at": now_iso()}
            conn.execute(
                "UPDATE variants SET plan_json = ? WHERE variant_id = ?",
                (dumps(vplan), row["variant_id"]),
            )
        batch["plan"]["rejected_by"] = {"by": by, "at": now_iso()}
        conn.execute(
            "UPDATE batches SET status = 'rejected', plan_json = ?, updated_at = ? WHERE batch_id = ?",
            (dumps(batch["plan"]), now_iso(), batch_id),
        )
        _close_review(
            conn, batch_id, "rejected", by, "都不满意" + ("，换一批" if replan else "")
        )
        log_event(
            conn,
            batch_id,
            "directions_rejected",
            {"family_id": batch["family_id"], "by": by, "replan": replan},
        )
    family_id = batch["family_id"]
    result: dict[str, Any] = {
        "batch_id": batch_id,
        "family_id": family_id,
        "rejected": len(rows),
    }
    if replan:
        if any(
            t["kind"] == "plan" and t["subject"] == family_id for t in bg.running(ctx)
        ):
            result["replan_skipped"] = f"{family_id} 正在策划，完成后会通知"
        else:
            argv = ["plan", "--family", family_id]
            # 换一批沿用这一轮的要求（写成一个参数，要求以「-」开头也不会被当成选项）
            if batch["plan"].get("hint"):
                argv.append(f"--hint={batch['plan']['hint']}")
            # 只策划部分类型的一轮，换一批也只策划这几类
            if batch["plan"].get("kinds"):
                argv.append("--kinds=" + ",".join(batch["plan"]["kinds"]))
            task = bg.spawn(ctx, argv, kind="plan", subject=family_id, notify=notify)
            result["replan_started"] = task["task_id"]
    return result


# ---------------------------------------------------------------- 方向记录


def plan_availability(
    ctx: Ctx, family_id: str, running: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """这个系列现在能不能再策划一轮：有待挑选的批次、正在策划、或者系列不可用时说明原因。

    之前的方向还在出图时也可以策划：选中的新方向会排在后面接着出。
    running 是调用方已经查过的 bg.running(ctx)，一次列出多个系列时不必反复查。
    """
    waiting = proposed_batches(ctx, family_id)
    if waiting:
        return {
            "ok": False,
            "pending_batch": waiting[-1]["batch_id"],
            "reason": f"还有一批方向待挑选（{waiting[-1]['batch_id']}）",
        }
    try:
        fam = planning_family(ctx, family_id)
    except StageError as exc:
        return {"ok": False, "reason": str(exc)}
    if not field_based(fam["spec"]):
        return {
            "ok": False,
            "reason": legacy_reason(fam["seed_id"]),
            "needs_reanalyze": fam["seed_id"],
        }
    for task in bg.running(ctx) if running is None else running:
        if (task["kind"] in ("plan", "revise") and task["subject"] == family_id) or (
            task["kind"] == "analyze" and task["subject"] == fam["seed_id"]
        ):
            return {"ok": False, "reason": "正在策划，完成后会通知你"}
    return {"ok": True}


def overview(ctx: Ctx, family_id: str) -> dict[str, Any]:
    """这个系列做过和提过的全部方向：按批次从新到旧分组，带状态与统计（方向记录卡、POD 对话共用）。"""
    fam = get_family(ctx, family_id)
    records = direction_records(ctx, family_id)
    by_batch: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_batch.setdefault(record["batch_id"], []).append(record)
    batches = []
    for row in ctx.db.q(
        "SELECT batch_id, status, plan_json, created_at FROM batches WHERE family_id = ? ORDER BY batch_id DESC",
        (family_id,),
    ):
        items = by_batch.get(row["batch_id"])
        if not items:
            continue
        plan = loads(row["plan_json"], {}) or {}
        batches.append(
            {
                "batch_id": row["batch_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "hint": plan.get("hint"),
                # 二次大变化批次：底图来自哪个原批次
                "derived_from": plan.get("derived_from"),
                "picked": sum(1 for r in items if r["job_status"] is not None),
                "directions": [
                    {
                        "index": i,
                        **{
                            k: r[k]
                            for k in (
                                "variant_id",
                                "title",
                                "task_type",
                                "kind",
                                "aspects",
                                "brief",
                                "keep",
                                "state",
                                "state_text",
                                "images",
                                "approved",
                            )
                        },
                    }
                    for i, r in enumerate(items, 1)
                ],
            }
        )
    counts = {key: 0 for key in STATE_LABELS}
    for record in records:
        counts[record["state"]] += 1
    return {
        "family_id": family_id,
        "family_version": fam["family_version"],
        "family_status": fam["status"],
        "seed_id": fam["seed_id"],
        "total": len(records),
        "counts": counts,
        "approved_images": sum(r["approved"] for r in records),
        "batches": batches,
        "availability": plan_availability(ctx, family_id),
    }


# ---------------------------------------------------------------- 选中的方向出到全部出完


def chosen_batches(ctx: Ctx) -> list[str]:
    """人确认过方向、还没出完的批次（旧批次没有挑选记录，不在其列）。"""
    rows = ctx.db.q(
        "SELECT batch_id, plan_json FROM batches WHERE status IN ('planned', 'running') ORDER BY batch_id"
    )
    return [
        r["batch_id"]
        for r in rows
        if (loads(r["plan_json"], {}) or {}).get("selection")
    ]


def _jobs_with_status(ctx: Ctx, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    batches = chosen_batches(ctx)
    if not batches:
        return []
    marks = ",".join("?" for _ in batches)
    smarks = ",".join("?" for _ in statuses)
    return [
        dict(r)
        for r in ctx.db.q(
            f"SELECT job_id, batch_id, blocked_reason FROM jobs WHERE batch_id IN ({marks}) AND status IN ({smarks})"
            " ORDER BY created_at, job_id",
            (*batches, *statuses),
        )
    ]


def _resumable_blocked(ctx: Ctx) -> list[dict[str, Any]]:
    """人确认过的方向里，因待审积压、预算等可恢复原因阻断、现在条件已经恢复的任务。"""
    from .worker import blocked_resumable  # worker 依赖本模块，放到用时再导入

    blocked = [
        j
        for j in _jobs_with_status(ctx, ("blocked",))
        if j["blocked_reason"] in state.RESUMABLE_BLOCKS
    ]
    return blocked if blocked and blocked_resumable(ctx) else []


def _pending(ctx: Ctx) -> list[dict[str, Any]]:
    return _jobs_with_status(ctx, ("ready",)) + _resumable_blocked(ctx)


def pending_jobs(ctx: Ctx) -> list[str]:
    """人确认过的方向里还等着出图的任务（含条件已恢复的阻断任务）。"""
    return [j["job_id"] for j in _pending(ctx)]


def start_runs(ctx: Ctx) -> dict[str, Any]:
    """没有出图在跑时，拉起只推进「人确认过的方向」（和选款里要求的修改）的后台出图；
    有 worker 在跑就只排队：它每次领取都现查确认过的方向，有空的工位就接着出。

    拉起的 worker 不带 --job 列表，之后新确认的批次也由它接着领。
    待审积压降回上限以下（人在选款里处理了、或清出了旧候选）时，之前因积压阻断的任务也一起接上。
    """
    pending = _pending(ctx)
    if not pending:
        return {}
    jobs = [j["job_id"] for j in pending]
    if ctx.db.get_meta("paused") == "1":
        return {"directions_queued": jobs, "reason": "流水线已暂停"}
    if fixes.busy(ctx):
        return {"directions_queued": jobs}
    task = bg.spawn(
        ctx,
        ["worker", "--source", "directions"],
        kind="worker",
        subject=pending[0]["batch_id"],
        notify=True,
    )
    return {"directions_started": task["task_id"], "jobs": jobs}


def resume_waiting(ctx: Ctx) -> dict[str, Any]:
    """巡检兜底：选中方向的出图被打断（任务卡在 running）或没人接时接着出；不看试运行窗口。"""
    if ctx.db.get_meta("paused") == "1" or fixes.busy(ctx):
        return {}
    out: dict[str, Any] = {}
    if _jobs_with_status(ctx, ("running",)):
        out["recovered"] = state.recover_stale(ctx.db, ctx.owner)
    out.update(start_runs(ctx))
    return out


def progress_lines(ctx: Ctx, batch_ids: list[str] | None = None) -> list[str]:
    """完成卡片用：人确认过的批次出到哪了。"""
    lines = []
    rows = ctx.db.q(
        "SELECT batch_id, status, plan_json FROM batches WHERE status IN ('planned', 'running', 'done') ORDER BY batch_id"
    )
    for row in rows:
        if batch_ids is not None and row["batch_id"] not in batch_ids:
            continue
        if not (loads(row["plan_json"], {}) or {}).get("selection"):
            continue
        counts = {
            r[0]: r[1]
            for r in ctx.db.q(
                "SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY status",
                (row["batch_id"],),
            )
        }
        total = sum(counts.values())
        done = counts.get("complete", 0) + counts.get("failed", 0)
        if batch_ids is None and done == total:
            continue
        text = f"{subject_label(ctx, row['batch_id'])} 选中的 {total} 个方向：已出完 {done} 个"
        if counts.get("blocked"):
            text += f"，{counts['blocked']} 个在等（待审积压或预算，先处理选款）"
        rest = counts.get("ready", 0) + counts.get("running", 0)
        if rest:
            text += f"，还剩 {rest} 个"
        lines.append(text)
    return lines
