"""任务归档：做完的任务从任务列表收起。界面上叫「归档 / 历史任务 / 调出」，代码里叫 shelf，
免得和素材的「已入库」（assets.status = archived，S10）混在一起。

- 状态记在 seed_shelf，一张参考图一行：archived = 已归档（任务列表不列，工作台「可策划」和 worker 补充策划都跳过），
  active = 调出来了。归档不删任何数据：方向记录、系列规则、图、候选表的行都留着，调出后继续裂变照样避开做过的方向。
- 自动归档（shelf.auto，缺省 false）：选款完成过、没有待选、至少选中 1 款、选中的款最近一套套图都出完（出完或缺图），
  任务的出图也都出完；开着批次交付包（scenes.batch_packages）时，每个有选中款的批次最新的交付包还要标了已下载
  （packages.mark_downloaded，任务到「套图已下载」）；而且这次完成发生在上一次归档 / 调出之后
  （调出后要等新一轮选款完成、套图出完或标已下载才会再收起）。
  选中的款都定了店铺时（2026-10-30）换成：点过「选款完成」、选中的款每家店都已上架（Temu 上架后台每上一款扫一遍）。
  套图进程收尾（scenes.sync）和巡检（podctl tick --auto）各扫一遍（sweep），标完已下载只对那个任务扫一遍（after_download）。
- 手动归档（archive）：任务没有在做的事（拆解、策划、待挑选的批次、没出完的出图、在出的套图、正在验收）就能归档；
  还有待选时要 force：剩下的待选清出选款（dismissed，不算不要）。一款没选、套图没出成、授权受限、拆解失败、
  已耗尽的任务只能手动归档。
- 调出（restore）：手动调出；或者有新动静时自动调出：继续裂变、重拆、修订、出套图、重出由命令入口调 touch，
  巡检再兜底一遍（有在出的图、新的待选、在出的套图、和它有关的后台任务）。
- 候选表在飞书里的位置由表格同步按 folder_state 挪（candidates._sync_shelf）：归档 → 「已归档」文件夹，调出 → 根目录；
  只在状态变化的那一刻挪一次，人手动拖过的不再拖回去。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import feishu_table, labels
from .context import Ctx
from .db import log_event
from .stages.common import StageError, task_label
from .util import dumps, loads, now_iso

STATE_ARCHIVED = "archived"
STATE_ACTIVE = "active"
FOLDER_ROOT = "root"
FOLDER_IN = "folder"
AUTO_BY = "system:auto"
MODES = ("auto", "manual", "force")
MODE_LABELS = {
    "auto": "自动归档",
    "manual": "手动归档",
    "force": "手动归档（清出待选）",
}
DISMISS_NOTE = "任务归档时清出选款（没看过，不算人工没选；图片和记录留着）"
# 任务还有事在做（或在等人挑方向）：不能归档；已归档的任务出现这些状态就自动调出
BUSY_STATUSES = frozenset(
    {
        "picking",
        "blocked",
        "analyzing",
        "planning",
        "paused",
        "generating",
        "queued",
        "start_needed",
    }
)
# 和这张参考图（S）或它的系列（F）有关的后台任务
_SEED_KINDS = ("analyze", "extract", "describe", "accept", "dispatch")
_FAMILY_KINDS = ("plan", "revise")


def enabled(ctx: Ctx) -> bool:
    return bool(ctx.setting("shelf.enabled", True))


def auto_enabled(ctx: Ctx) -> bool:
    return enabled(ctx) and bool(ctx.setting("shelf.auto", False))


def _num(ident: str | None) -> int:
    digits = "".join(ch for ch in str(ident or "") if ch.isdigit())
    return int(digits) if digits else 0


# ---------------------------------------------------------------- 查询


def rows(ctx: Ctx) -> dict[str, dict[str, Any]]:
    """每张参考图的归档记录（没归档过的不在里面）；关着归档时为空，任务一律照常列。"""
    if not enabled(ctx):
        return {}
    return {r["seed_id"]: dict(r) for r in ctx.db.q("SELECT * FROM seed_shelf")}


def get(ctx: Ctx, seed_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT * FROM seed_shelf WHERE seed_id = ?", (seed_id,))
    return dict(row) if row else None


def archived_seeds(ctx: Ctx) -> set[str]:
    return {s for s, r in rows(ctx).items() if r["state"] == STATE_ARCHIVED}


def archived_families(ctx: Ctx) -> set[str]:
    seeds = archived_seeds(ctx)
    if not seeds:
        return set()
    return {
        r["family_id"]
        for r in ctx.db.q("SELECT DISTINCT family_id, seed_id FROM families")
        if r["seed_id"] in seeds
    }


def is_archived(ctx: Ctx, seed_id: str | None) -> bool:
    if not seed_id or not enabled(ctx):
        return False
    row = get(ctx, seed_id)
    return bool(row and row["state"] == STATE_ARCHIVED)


def view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """任务视图里的归档信息：没归档过为 None。"""
    if not row:
        return None
    return {
        "state": row["state"],
        "archived": row["state"] == STATE_ARCHIVED,
        "mode": row["mode"],
        "mode_label": MODE_LABELS.get(row["mode"] or "", row["mode"]),
        "archived_at": row["archived_at"],
        "archived_by": row["archived_by"],
        "note": row["note"],
        "restored_at": row["restored_at"],
        "restored_by": row["restored_by"],
        "restore_reason": row["restore_reason"],
        "cycles": int(row["cycles"] or 0),
        "folder_state": row["folder_state"] or FOLDER_ROOT,
        "folder_error": row["folder_error"],
        "snapshot": loads(row["snapshot_json"], {}) or {},
    }


def _facts(ctx: Ctx) -> dict[str, dict[str, str | None]]:
    """每张参考图最近一次选款验收、最近一次定下选款、最近一套套图结束、最近一次标交付包已下载的时间
    （判断「这次完成」用）。"""
    out: dict[str, dict[str, str | None]] = defaultdict(
        lambda: {
            "accepted_at": None,
            "decided_at": None,
            "scenes_at": None,
            "downloaded_at": None,
        }
    )
    for r in ctx.db.q("SELECT seed_id, accepted_at FROM candidate_tables"):
        out[r["seed_id"]]["accepted_at"] = r["accepted_at"]
    for r in ctx.db.q(
        "SELECT f.seed_id, MAX(r.decided_at) FROM reviews r"
        " JOIN assets a ON a.asset_id = r.subject_id"
        " JOIN variants v ON v.variant_id = a.variant_id"
        " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " WHERE r.subject_type = 'asset_selection' AND r.status = 'decided'"
        " GROUP BY f.seed_id"
    ):
        out[r[0]]["decided_at"] = r[1]
    for r in ctx.db.q(
        "SELECT seed_id, MAX(finished_at) FROM scene_sets WHERE finished_at IS NOT NULL"
        " GROUP BY seed_id"
    ):
        out[r[0]]["scenes_at"] = r[1]
    for r in ctx.db.q(
        "SELECT seed_id, MAX(marked_at) FROM package_downloads GROUP BY seed_id"
    ):
        out[r[0]]["downloaded_at"] = r[1]
    for r in ctx.db.q(
        "SELECT f.seed_id, MAX(s.listed_at) FROM store_listings s"
        " JOIN assets a ON a.asset_id = s.asset_id"
        " JOIN variants v ON v.variant_id = a.variant_id"
        " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " WHERE s.listed_at IS NOT NULL GROUP BY f.seed_id"
    ):
        out[r[0]]["listed_at"] = r[1]
    return out


def _listing_facts(ctx: Ctx) -> dict[str, dict[str, int]]:
    """每张参考图选中的款上架到哪了（去掉的店不算）：{seed_id: {approved, with_stores, listed, failed}}。
    listed = 每家店都已上架的款数，failed = 有店上架没成的款数。"""
    out: dict[str, dict[str, int]] = defaultdict(
        lambda: {"approved": 0, "with_stores": 0, "listed": 0, "failed": 0}
    )
    seed_of: dict[str, str] = {}
    states: dict[str, list[str]] = defaultdict(list)
    for r in ctx.db.q(
        "SELECT f.seed_id, a.asset_id, s.state FROM assets a"
        " JOIN variants v ON v.variant_id = a.variant_id"
        " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " LEFT JOIN store_listings s ON s.asset_id = a.asset_id AND s.state <> 'cancelled'"
        " WHERE a.human_decision = 'approved'"
    ):
        seed_of[r["asset_id"]] = r["seed_id"]
        if r["state"]:
            states[r["asset_id"]].append(r["state"])
    for asset_id, seed_id in seed_of.items():
        item = out[seed_id]
        item["approved"] += 1
        got = states.get(asset_id) or []
        if got:
            item["with_stores"] += 1
            item["listed"] += int(all(s == "listed" for s in got))
            item["failed"] += int("failed" in got)
    return out


def _completed_at(fact: dict[str, str | None] | None) -> str | None:
    values = [v for v in (fact or {}).values() if v]
    return max(values) if values else None


def _last_change(row: dict[str, Any] | None) -> str | None:
    values = [
        v for v in ((row or {}).get(k) for k in ("archived_at", "restored_at")) if v
    ]
    return max(values) if values else None


def _open_selection(ctx: Ctx, seed_id: str) -> list[tuple[str, str]]:
    """这张参考图还开着的选款待审：[(review_id, batch_id)]。"""
    return [
        (r["review_id"], r["batch_id"])
        for r in ctx.db.q(
            "SELECT r.review_id, v.batch_id FROM reviews r"
            " JOIN assets a ON a.asset_id = r.subject_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " WHERE r.subject_type = 'asset_selection' AND r.status = 'open' AND f.seed_id = ?"
            " ORDER BY r.review_id",
            (seed_id,),
        )
    ]


# ---------------------------------------------------------------- 判断（纯函数，任务视图共用）


def _scenes_text(counts: dict[str, Any], *, manual: bool = False) -> str:
    """「还有 n 款套图在出」；其中停在 mooya 等人工的写明（手动归档时再说怎么处理）。"""
    active = int(counts.get("scenes_active") or 0)
    parked = int(counts.get("scenes_parked") or 0)
    text = f"还有 {active} 款套图在出"
    if parked:
        text += f"（其中 {parked} 款在 mooya 等人工"
        if manual:
            text += "：去 mooya 图案管线处理，或 podctl scenes cancel 停掉"
        text += "）"
    return text


def _running_for(
    task: dict[str, Any], running: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seed, family = task["seed_id"], task.get("family_id")
    return [
        t
        for t in running
        if (t["kind"] in _SEED_KINDS and t["subject"] == seed)
        or (t["kind"] in _FAMILY_KINDS and family and t["subject"] == family)
    ]


def blockers(task: dict[str, Any], running: list[dict[str, Any]]) -> list[str]:
    """挡着手动归档的事（空 = 能归档）。待选不在这里：加 force 可以清出。"""
    from . import bg  # 后台任务模块依赖通知，放到用时再导入

    out = []
    status = task["status"]
    if status == "picking":
        out.append(
            f"{task.get('pending_batch') or '有一批'}的方向还在待挑选：先挑完，或点「都不满意，换一批」"
        )
    elif status in ("analyzing", "planning"):
        out.append(f"{task['status_label']}，做完再归档")
    elif status in BUSY_STATUSES:
        out.append(f"还有方向没出完（{task['status_label']}）")
    counts = task.get("counts") or {}
    if counts.get("scenes_active"):
        out.append(_scenes_text(counts, manual=True))
    for t in _running_for(task, running):
        out.append(
            f"后台正在{bg.KIND_LABELS.get(t['kind'], t['kind'])}（{t['task_id']}）"
        )
    return out


def activity(task: dict[str, Any], running: list[dict[str, Any]]) -> str | None:
    """已归档的任务有了新动静（这时自动调出来）：有在做的事、新的待选、在出的套图、和它有关的后台任务。"""
    from . import bg

    if task["status"] in BUSY_STATUSES:
        return task["status_label"]
    counts = task.get("counts") or {}
    if counts.get("pending_selection"):
        return f"有 {counts['pending_selection']} 张新图进选款"
    if counts.get("scenes_active"):
        return f"有 {counts['scenes_active']} 款套图在出"
    found = _running_for(task, running)
    if found:
        return f"后台正在{bg.KIND_LABELS.get(found[0]['kind'], found[0]['kind'])}"
    return None


def auto_block(
    task: dict[str, Any],
    fact: dict[str, str | None] | None,
    row: dict[str, Any] | None,
    *,
    scenes_on: bool,
    packages_on: bool = False,
    listing: dict[str, int] | None = None,
) -> str | None:
    """不能自动归档的原因（None = 能）：选款完成过、没有待选、至少选中 1 款，而且这次完成在上一次归档 / 调出之后；
    选中的款都定了店铺时（2026-10-30）：点过「选款完成」、选中的款每家店都已上架就能收起（listing 是 _listing_facts 那一项）；
    否则照旧：选中的款套图都出完、出图都出完，开着批次交付包时每个批次最新的交付包都标了已下载（任务视图的 downloads）。"""
    if task["status"] != "done":
        return f"任务还没完成（{task['status_label']}）"
    counts = task.get("counts") or {}
    if counts.get("pending_selection"):
        return f"还有 {counts['pending_selection']} 张待选"
    fact = fact or {}
    if not (fact.get("accepted_at") or fact.get("decided_at")):
        return "还没选过款"
    if not counts.get("approved"):
        return "一款都没选中（要收起就手动归档）"
    listing = listing or {}
    if listing.get("approved") and listing.get("with_stores") == listing["approved"]:
        if not fact.get("accepted_at"):
            return "还没点「选款完成」"
        left = listing["approved"] - listing.get("listed", 0)
        if left:
            failed = f"，其中 {listing['failed']} 款上架没成" if listing.get("failed") else ""
            return f"选中的款还有 {left} 款没上架完{failed}"
        return _after_change(row, fact)
    if not scenes_on:
        return "套图没打开（要收起就手动归档）"
    if counts.get("scenes_active"):
        return _scenes_text(counts)
    if counts.get("scenes_missing"):
        return (
            f"有 {counts['scenes_missing']} 款套图没出成或还没出"
            "（重出完会自动归档，或者手动归档）"
        )
    if not counts.get("scenes_done"):
        return "选中的款还没有出完的套图"
    if packages_on:
        downloads = task.get("downloads") or {}
        pending = downloads.get("pending") or []
        if pending:
            text = f"交付包还没标已下载：{'、'.join(pending)}"
            outdated = downloads.get("outdated") or []
            if outdated:
                text += f"（{'、'.join(outdated)} 重打过，下载新包后再标）"
            return text
        if not downloads.get("batches"):
            return "交付包还没打好"
    return _after_change(row, fact)


def _after_change(row: dict[str, Any] | None, fact: dict[str, str | None]) -> str | None:
    """这次完成要在上一次归档 / 调出之后（调出后要等新一轮选款完成、套图出完、交付包下载或上架）。"""
    last = _last_change(row)
    completed = _completed_at(fact)
    if last and (not completed or completed <= last):
        return "调出后还没有新的选款完成、套图出完、交付包下载或上架"
    return None


# ---------------------------------------------------------------- 归档 / 调出


def _snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    """归档那一刻的进度（历史任务里显示「选中 6 · 套图 6/6」用）。"""
    keys = (
        "batches",
        "directions",
        "jobs",
        "images",
        "approved",
        "scenes_done",
        "listing_listed",
        "listing_skipped",
    )
    total: dict[str, int] = {k: 0 for k in keys}
    for task in items:
        for key in keys:
            total[key] += int((task.get("counts") or {}).get(key) or 0)
    return total


def archive(
    ctx: Ctx,
    seed_id: str,
    *,
    by: str,
    force: bool = False,
    note: str | None = None,
    mode: str | None = None,
    dry_run: bool = False,
    items: list[dict[str, Any]] | None = None,
    running: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """归档一个任务（参考图）：从任务列表收起，候选表交给表格同步挪进「已归档」文件夹。

    有在做的事就拒绝；还有待选时要 force（剩下的待选清出选款，不算不要）。已经归档的直接返回 already。
    items / running 是调用方已经算好的任务视图和后台任务（sweep 一次扫很多张图时不必反复算）。
    """
    from . import bg, tasks as tasksmod  # 任务视图依赖本模块，放到用时再导入

    if not enabled(ctx):
        raise StageError("shelf.enabled 为 false：任务归档关着")
    if not ctx.db.q1("SELECT 1 FROM seeds WHERE seed_id = ?", (seed_id,)):
        raise StageError(f"没有参考图 {seed_id}")
    label = task_label(ctx, seed_id)
    current = get(ctx, seed_id)
    if current and current["state"] == STATE_ARCHIVED:
        return {
            "seed_id": seed_id,
            "label": label,
            "already": True,
            "archived_at": current["archived_at"],
        }
    if items is None:
        items = [
            t for t in tasksmod.summaries(ctx, view="all") if t["seed_id"] == seed_id
        ]
    if running is None:
        running = bg.running(ctx)
    blocked = list(dict.fromkeys(r for t in items for r in blockers(t, running)))
    if blocked:
        raise StageError(f"{label} 现在不能归档：" + "；".join(blocked))
    pending = _open_selection(ctx, seed_id)
    if pending and not force:
        raise StageError(
            f"{label} 还有 {len(pending)} 张待选：要归档就加 --force，"
            "剩下的待选清出选款（不算不要，调出后也不会回到选款）"
        )
    mode = mode or ("force" if pending else "manual")
    if mode not in MODES:
        raise StageError(f"归档方式只能是 {list(MODES)}")
    if dry_run:
        return {
            "dry_run": True,
            "seed_id": seed_id,
            "label": label,
            "mode": mode,
            "mode_label": MODE_LABELS[mode],
            "dismiss": len(pending),
        }
    dismissed = 0
    if pending:
        from . import maintenance  # 运维模块依赖表格同步，放到用时再导入

        out = maintenance.dismiss_selection(
            ctx, batches=sorted({b for _, b in pending}), by=by, note=DISMISS_NOTE
        )
        dismissed = len(out["reviews"])
    snapshot = _snapshot(items)
    now = now_iso()
    with ctx.db.tx() as conn:
        row = conn.execute(
            "SELECT state, cycles FROM seed_shelf WHERE seed_id = ?", (seed_id,)
        ).fetchone()
        if row and row[0] == STATE_ARCHIVED:
            return {"seed_id": seed_id, "label": label, "already": True}
        cycles = int(row[1] or 0) + 1 if row else 1
        conn.execute(
            "INSERT INTO seed_shelf(seed_id, state, mode, archived_at, archived_by, note, cycles,"
            " snapshot_json, folder_attempts, folder_error, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)"
            " ON CONFLICT(seed_id) DO UPDATE SET state = excluded.state, mode = excluded.mode,"
            " archived_at = excluded.archived_at, archived_by = excluded.archived_by,"
            " note = excluded.note, cycles = excluded.cycles, snapshot_json = excluded.snapshot_json,"
            " folder_attempts = 0, folder_error = NULL, updated_at = excluded.updated_at",
            (
                seed_id,
                STATE_ARCHIVED,
                mode,
                now,
                by,
                note,
                cycles,
                dumps(snapshot),
                now,
                now,
            ),
        )
        log_event(
            conn,
            seed_id,
            "task_archived",
            {"by": by, "mode": mode, "dismissed": dismissed, "note": note},
        )
    # 候选表挪进「已归档」文件夹、表名跟上，由表格同步来做（共用租约）
    feishu_table.kick(ctx)
    return {
        "seed_id": seed_id,
        "label": label,
        "archived": True,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "dismissed": dismissed,
        "cycles": cycles,
        "snapshot": snapshot,
    }


def restore(
    ctx: Ctx, seed_id: str, *, by: str, reason: str = "手动调出"
) -> dict[str, Any]:
    """调出一个已归档的任务：回到任务列表，候选表交给表格同步挪回根目录。没归档的直接返回 already_active。"""
    if not enabled(ctx):
        raise StageError("shelf.enabled 为 false：任务归档关着")
    if not ctx.db.q1("SELECT 1 FROM seeds WHERE seed_id = ?", (seed_id,)):
        raise StageError(f"没有参考图 {seed_id}")
    label = task_label(ctx, seed_id)
    now = now_iso()
    with ctx.db.tx() as conn:
        changed = conn.execute(
            "UPDATE seed_shelf SET state = ?, restored_at = ?, restored_by = ?, restore_reason = ?,"
            " folder_attempts = 0, folder_error = NULL, updated_at = ? WHERE seed_id = ? AND state = ?",
            (STATE_ACTIVE, now, by, reason, now, seed_id, STATE_ARCHIVED),
        ).rowcount
        if changed:
            log_event(conn, seed_id, "task_restored", {"by": by, "reason": reason})
    if not changed:
        return {"seed_id": seed_id, "label": label, "already_active": True}
    feishu_table.kick(ctx)
    return {"seed_id": seed_id, "label": label, "restored": True, "reason": reason}


def touch(
    ctx: Ctx, ref: str | None, *, reason: str, by: str = AUTO_BY
) -> dict[str, Any]:
    """有新动静（继续裂变、重拆、修订、出套图、重出）：这个任务归档着就调出来。返回 restore 的结果，没调出时为 {}。"""
    if not enabled(ctx):
        return {}
    seed_id = labels.seed_of(ctx, ref)
    if not is_archived(ctx, seed_id):
        return {}
    return restore(ctx, seed_id, by=by, reason=reason)


def sweep(
    ctx: Ctx,
    *,
    source: str,
    dry_run: bool = False,
    seeds: set[str] | None = None,
) -> dict[str, Any]:
    """扫一遍：先把有新动静的已归档任务调出来，再在开着 shelf.auto 时归档满足条件的任务。

    dry_run 只列出会调出 / 能自动归档的任务和其余任务不能归档的原因，什么都不改；seeds 只扫这几个任务。
    """
    from . import bg, packages, scenes, tasks as tasksmod

    if not enabled(ctx):
        return {"skipped": "shelf.enabled 为 false"}
    by_seed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasksmod.summaries(ctx, view="all"):
        if seeds is None or task["seed_id"] in seeds:
            by_seed[task["seed_id"]].append(task)
    running = bg.running(ctx)
    shelf_rows = rows(ctx)
    facts = _facts(ctx)
    listing = _listing_facts(ctx)
    scenes_on = scenes.enabled(ctx)
    packages_on = packages.enabled(ctx)
    auto = auto_enabled(ctx)
    out: dict[str, Any] = {
        "source": source,
        "auto": auto,
        "dry_run": dry_run,
        "reopened": [],
        "archived": [],
        "eligible": [],
        "blocked": [],
    }
    for seed_id in sorted(by_seed, key=_num):
        items = by_seed[seed_id]
        row = shelf_rows.get(seed_id)
        entry = {"seed_id": seed_id, "label": task_label(ctx, seed_id)}
        if row and row["state"] == STATE_ARCHIVED:
            reason = next((r for r in (activity(t, running) for t in items) if r), None)
            if reason:
                if not dry_run:
                    restore(ctx, seed_id, by=AUTO_BY, reason=f"有新动静：{reason}")
                out["reopened"].append({**entry, "reason": reason})
            continue
        reason = next(
            (
                r
                for r in (
                    auto_block(
                        t,
                        facts.get(seed_id),
                        row,
                        scenes_on=scenes_on,
                        packages_on=packages_on,
                        listing=listing.get(seed_id),
                    )
                    for t in items
                )
                if r
            ),
            None,
        )
        if reason:
            out["blocked"].append({**entry, "reason": reason})
            continue
        if dry_run or not auto:
            out["eligible"].append(entry)
            continue
        try:
            done = archive(
                ctx, seed_id, by=AUTO_BY, mode="auto", items=items, running=running
            )
        except StageError as exc:
            out["blocked"].append({**entry, "reason": str(exc)})
            continue
        if done.get("archived"):
            out["archived"].append({**entry, "snapshot": done["snapshot"]})
    return out


def after_download(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    """标完交付包已下载：开着 shelf.auto 时只对这个任务扫一遍，满足条件当场归档并发卡片。
    返回 {archived, notified}；没归档时 {shelf_block: 还差什么}；关着自动归档时 {}。"""
    if not auto_enabled(ctx):
        return {}
    try:
        swept = sweep(ctx, source="download", seeds={seed_id})
    except Exception as exc:  # noqa: BLE001 - 归档出错不挡标记
        return {"shelf_error": f"{type(exc).__name__}: {exc}"[:300]}
    archived = swept.get("archived") or []
    if archived:
        return {"archived": archived, "notified": announce(ctx, archived)}
    blocked = next((b["reason"] for b in swept.get("blocked") or []), None)
    return {"shelf_block": blocked} if blocked else {}


def announce(ctx: Ctx, archived: list[dict[str, Any]]) -> dict[str, Any] | None:
    """巡检这一轮（或标完已下载时）自动归档的任务合并发一张卡片（套图进程收尾时归档的写在「套图出完」卡片里，不走这里）。
    精简通知时不发：状态总结「今日已完成」里写归档了几个。"""
    if not archived:
        return None
    from . import digest, notify, packages, ui  # 通知依赖视图模块，放到用时再导入

    if digest.enabled(ctx):
        return {"sent": False, "reason": "精简通知：归档写进状态总结"}
    done = (
        "选款完成、套图都出完、交付包都标了已下载"
        if packages.enabled(ctx)
        else "选款完成、选中的款套图都出完了"
    )
    lines = [
        f"**{len(archived)}** 个任务{done}，已经从任务列表收起：",
        *(f"- {a['label']}" for a in archived[:10]),
    ]
    if len(archived) > 10:
        lines.append(f"- 还有 {len(archived) - 10} 个")
    lines.append(
        "交付包、候选表（挪进了「已归档」文件夹）和选中的款都还在；"
        "任务列表点「历史任务」能找到，要接着裂变点「调出」或直接写一句要求「继续裂变」"
    )
    buttons: list[tuple[str, dict[str, Any], str]] = [
        ("历史任务", {"action": ui.ACTION_SHELF}, "primary")
    ]
    if len(archived) == 1:
        buttons.append(
            (
                "调出",
                {"action": ui.ACTION_RESTORE, "seed": archived[0]["seed_id"]},
                "default",
            )
        )
    buttons.append(("任务列表", {"action": ui.ACTION_TASKS}, "default"))
    title = (
        f"任务已归档｜{archived[0]['label']}"
        if len(archived) == 1
        else f"任务已归档 {len(archived)} 个"
    )
    card = notify.pod_card(title, lines, buttons=buttons)
    try:
        return notify.send_pod_card(ctx, card, "\n".join([title, *lines]))
    except notify.NotifyError as exc:
        return {"sent": False, "error": str(exc)}


# ---------------------------------------------------------------- 候选表挪进 / 挪出文件夹（表格同步用）


def pending_moves(ctx: Ctx) -> list[dict[str, Any]]:
    """候选表要挪的：归档了还在根目录的、调出了还在文件夹里的（失败满上限的不再试）。"""
    if not enabled(ctx):
        return []
    out = []
    for r in ctx.db.q(
        "SELECT s.seed_id, s.state, s.folder_state, s.folder_attempts, t.table_id"
        " FROM seed_shelf s JOIN candidate_tables t ON t.seed_id = s.seed_id ORDER BY s.seed_id"
    ):
        want = FOLDER_IN if r["state"] == STATE_ARCHIVED else FOLDER_ROOT
        if (
            want != (r["folder_state"] or FOLDER_ROOT)
            and int(r["folder_attempts"] or 0) < feishu_table.MAX_ATTEMPTS
        ):
            out.append(
                {"seed_id": r["seed_id"], "table_id": r["table_id"], "want": want}
            )
    return out


def mark_folder(
    ctx: Ctx, seed_id: str, *, state: str | None = None, error: str | None = None
) -> None:
    """记下候选表挪到了哪（state），或者这次没挪成（error，失败次数加一）。"""
    with ctx.db.tx() as conn:
        if state:
            conn.execute(
                "UPDATE seed_shelf SET folder_state = ?, folder_attempts = 0, folder_error = NULL,"
                " updated_at = ? WHERE seed_id = ?",
                (state, now_iso(), seed_id),
            )
            log_event(conn, seed_id, "candidate_table_moved", {"to": state})
        else:
            conn.execute(
                "UPDATE seed_shelf SET folder_attempts = folder_attempts + 1, folder_error = ?,"
                " updated_at = ? WHERE seed_id = ?",
                ((error or "")[:300], now_iso(), seed_id),
            )


def reset_folder_attempts(ctx: Ctx) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE seed_shelf SET folder_attempts = 0 WHERE folder_attempts > 0"
        )


def status(ctx: Ctx) -> dict[str, Any]:
    """归档开关、文件夹、已归档几个、候选表还要挪几张和挪不动的原因、最近几次归档 / 调出。"""
    data = rows(ctx)
    archived = sorted(
        (r for r in data.values() if r["state"] == STATE_ARCHIVED),
        key=lambda r: r["archived_at"] or "",
        reverse=True,
    )
    errors = [
        {
            "seed_id": r["seed_id"],
            "error": r["folder_error"],
            "attempts": int(r["folder_attempts"] or 0),
        }
        for r in data.values()
        if r["folder_error"]
    ]
    return {
        "enabled": enabled(ctx),
        "auto": auto_enabled(ctx),
        "folder": (feishu_table.base(ctx) or {}).get("shelf_folder"),
        "archived": len(archived),
        "recent": [
            {
                "seed_id": r["seed_id"],
                "label": task_label(ctx, r["seed_id"]),
                "mode": r["mode"],
                "archived_at": r["archived_at"],
                "folder_state": r["folder_state"] or FOLDER_ROOT,
            }
            for r in archived[:10]
        ],
        "moves_pending": len(pending_moves(ctx)),
        "folder_errors": errors,
    }
