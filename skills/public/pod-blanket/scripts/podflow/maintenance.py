"""存量数据迁移与清理：流程调整后把旧数据收拾到新流程上，或彻底删掉不再用的旧数据。可重复执行，第二次起不改任何东西。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from . import candidates, feishu, feishu_table, lease, packages, scenes, state, store
from .context import Ctx
from .db import log_event
from .stages.common import StageError, get_job
from .stages.job import step_s09, step_s10
from .util import dumps, loads, now_iso

# IN 查询每次最多带多少个编号
_CHUNK = 500


def retire_dedup(ctx: Ctx, *, by: str) -> dict[str, Any]:
    """近重复判定下线：关掉未处理的「疑似重复」待审，素材改回「质检通过」。

    已结束任务里的这些素材当场预检、归档并开选款（都是程序步骤，不调模型）；
    还没结束的任务由 worker 按新流程接着推进。
    """
    worker = state.worker_status(ctx.db)
    if worker and worker["alive"]:
        raise StageError("worker 正在运行：等它结束再执行，免得两边同时改同一个任务")
    now = now_iso()
    with ctx.db.tx() as conn:
        reviews = [
            r["review_id"]
            for r in conn.execute(
                "SELECT review_id FROM reviews WHERE subject_type = 'asset_dup' AND status = 'open'"
                " ORDER BY review_id"
            ).fetchall()
        ]
        conn.execute(
            "UPDATE reviews SET status = 'dismissed', decided_by = ?, decided_at = ?, note = ?"
            " WHERE subject_type = 'asset_dup' AND status = 'open'",
            (by, now, "近重复判定已下线：素材直接进选款"),
        )
        assets = [
            dict(r)
            for r in conn.execute(
                "SELECT asset_id, job_id FROM assets WHERE status = 'review_dup' ORDER BY asset_id"
            ).fetchall()
        ]
        for asset in assets:
            conn.execute(
                "UPDATE assets SET status = 'qc_pass', updated_at = ? WHERE asset_id = ?",
                (now, asset["asset_id"]),
            )
            log_event(
                conn,
                asset["asset_id"],
                "asset_updated",
                {"status": "qc_pass", "reason": "近重复判定下线", "by": by},
            )
    processed, left = [], []
    for job_id in dict.fromkeys(a["job_id"] for a in assets):
        job = get_job(ctx, job_id)
        if job["status"] in ("complete", "failed"):
            step_s09(ctx, job)
            step_s10(ctx, job)
            processed.append(job_id)
        else:
            left.append(job_id)
    return {
        "dismissed_reviews": reviews,
        "assets_to_selection": [a["asset_id"] for a in assets],
        "processed_jobs": processed,
        "left_to_worker": left,
    }


def restore_rejected(
    ctx: Ctx, *, batches: Iterable[str] = (), by: str, dry_run: bool = False
) -> dict[str, Any]:
    """把质检淘汰的图放回选款（质检暂停后用）：标「未经质检」，原来的判定留在 previous。

    已结束的任务当场预检、归档、开选款（都是程序步骤，不调模型）；还没结束的任务由 worker 接着推进。
    batches 为空时放回全部批次的。可重复执行，第二次起什么都不改。
    """
    wanted = set(batches)
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT a.asset_id, a.job_id, a.qc_json, j.batch_id FROM assets a"
            " JOIN jobs j ON j.job_id = a.job_id WHERE a.status = 'rejected' ORDER BY a.asset_id"
        )
        if not wanted or r["batch_id"] in wanted
    ]
    summary = {
        "assets": [r["asset_id"] for r in rows],
        "jobs": sorted({r["job_id"] for r in rows}),
        "batches": sorted({r["batch_id"] for r in rows}),
    }
    if dry_run:
        return {"dry_run": True, **summary}
    now = now_iso()
    with ctx.db.tx() as conn:
        for row in rows:
            previous = loads(row["qc_json"], {}) or {}
            why = previous.get("override") or previous.get("reason") or "质检判淘汰"
            qc = {
                "decision": "unchecked",
                "flagged": True,
                "reason": f"质检暂停后放回选款（原判淘汰：{why}）",
                "file_checks": previous.get("file_checks"),
                "previous": {
                    k: previous[k]
                    for k in ("decision", "override", "result")
                    if k in previous
                },
            }
            conn.execute(
                "UPDATE assets SET status = 'qc_pass', qc_json = ?, updated_at = ?"
                " WHERE asset_id = ? AND status = 'rejected'",
                (dumps(qc), now, row["asset_id"]),
            )
            log_event(
                conn,
                row["asset_id"],
                "asset_updated",
                {"status": "qc_pass", "reason": "质检暂停后放回选款", "by": by},
            )
    processed, left = [], []
    for job_id in summary["jobs"]:
        job = get_job(ctx, job_id)
        if job["status"] in ("complete", "failed"):
            step_s09(ctx, job)
            step_s10(ctx, job)
            processed.append(job_id)
        else:
            left.append(job_id)
    return {
        "dry_run": False,
        **summary,
        "processed_jobs": processed,
        "left_to_worker": left,
    }


# ---------------------------------------------------------------- 旧候选清出选款

# 清出选款的待审：选款与质检存疑（都是等人看图的）
_DISMISS_TYPES = ("asset_selection", "asset_qc")
DISMISS_NOTE = "清出选款（没看过，不算人工没选；图片和记录留着）"


def dismiss_selection(
    ctx: Ctx,
    *,
    batches: Iterable[str],
    by: str,
    note: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把这些批次还没处理的候选清出选款：待审关掉（dismissed），图片、方向记录和选中过的款都不动。

    不记成人工「不要」，下次策划和批次反馈都不把它们当成被否掉的方向；批次里还有任务在出图时拒绝。
    可重复执行，第二次起什么都不改。
    """
    wanted = sorted(set(batches))
    if not wanted:
        raise StageError("要写清楚清哪几个批次（--batch B006，可重复写）")
    for batch_id in wanted:
        if not ctx.db.q1("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)):
            raise StageError(f"没有批次 {batch_id}")
        busy = ctx.db.scalar(
            "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status NOT IN ('complete', 'failed')",
            (batch_id,),
        )
        if busy:
            raise StageError(
                f"{batch_id} 还有 {busy} 个任务没出完：出完再清，免得新出的图又进选款"
            )
    marks = ",".join("?" for _ in wanted)
    types = ",".join("?" for _ in _DISMISS_TYPES)
    rows = [
        dict(r)
        for r in ctx.db.q(
            f"SELECT review_id, subject_id, subject_type, batch_id FROM reviews WHERE status = 'open'"
            f" AND subject_type IN ({types}) AND batch_id IN ({marks}) ORDER BY review_id",
            (*_DISMISS_TYPES, *wanted),
        )
    ]
    summary = {
        "batches": wanted,
        "reviews": [r["review_id"] for r in rows],
        "assets": [r["subject_id"] for r in rows],
    }
    if dry_run:
        return {"dry_run": True, **summary}
    now = now_iso()
    with ctx.db.tx() as conn:
        for row in rows:
            conn.execute(
                "UPDATE reviews SET status = 'dismissed', decided_by = ?, decided_at = ?, note = ?"
                " WHERE review_id = ? AND status = 'open'",
                (by, now, note or DISMISS_NOTE, row["review_id"]),
            )
        for batch_id in wanted:
            count = sum(1 for r in rows if r["batch_id"] == batch_id)
            if count:
                log_event(
                    conn,
                    batch_id,
                    "selection_dismissed",
                    {"reviews": count, "by": by, "note": note or DISMISS_NOTE},
                )
    # 候选表上这些行标「已清出」
    feishu_table.kick(ctx)
    return {"dry_run": False, **summary}


def relabel_assets(ctx: Ctx, *, by: str, dry_run: bool = False) -> dict[str, Any]:
    """把选款表「选中的款」和各候选表里已有行的素材名字改成「编号_方向短语」（之后新进表的行直接是新名字）：
    「素材」列、缩略图附件（按新名字重传）、下载原图（按新名字重签；候选表只管待选 / 已选中的行）。

    持表格同步租约（有同步在跑就等它放开）；逐行出错只记下来，可以重复执行。
    """
    if not feishu_table.enabled(ctx):
        raise StageError("table.enabled 为 false：表格没有打开")
    counts = {
        "selection_rows": int(
            ctx.db.scalar("SELECT COUNT(*) FROM table_rows WHERE record_id IS NOT NULL")
            or 0
        ),
        "candidate_rows": int(
            ctx.db.scalar(
                "SELECT COUNT(*) FROM candidate_rows WHERE record_id IS NOT NULL"
            )
            or 0
        ),
    }
    info = feishu_table.base(ctx)
    if dry_run or not info:
        return {"dry_run": dry_run, **counts}
    candidates.take_lease(ctx, busy="表格正在同步，过一会儿再补改名字")
    errors: list[dict[str, str]] = []
    try:
        with feishu.Client(ctx) as api:
            selection = feishu_table.relabel_rows(ctx, api, info, errors)
            cand = candidates.relabel_rows(ctx, api, info, errors)
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "maintenance",
            "assets_relabeled",
            {
                "by": by,
                "selection": selection,
                "candidates": cand,
                "failed": len(errors),
            },
        )
    return {
        "dry_run": False,
        "selection": selection,
        "candidates": cand,
        "failed": len(errors),
        "errors": errors[:10],
    }


def _scene_image_tables(ctx: Ctx) -> list[dict[str, Any]]:
    """有「场景图」列要删的表：旧表格的「选中的款」、每周表格每天的「选中的款」、模板表格和每本周表格里的「选中的款模板」。
    [{label, app_token, table_id, kind（legacy / day / template / book）, key（day / book_id）}]"""
    from . import books  # 每周选款表依赖表格模块，放到用时再导入

    out: list[dict[str, Any]] = []
    legacy = feishu_table.base(ctx)
    if legacy:
        out.append(
            {"label": "旧表格「选中的款」", "app_token": legacy["app_token"], "table_id": legacy["table_id"],
             "kind": "legacy", "key": None}
        )
    for day in books.days(ctx):
        if day.get("table_id"):
            out.append(
                {"label": f"「{day.get('table_name') or day['day']}」", "app_token": day["app_token"],
                 "table_id": day["table_id"], "kind": "day", "key": day["day"]}
            )
    tpl = books.template(ctx) or {}
    selected = (tpl.get("tables") or {}).get("selected") or {}
    if tpl.get("app_token") and selected.get("table_id"):
        out.append(
            {"label": "模板表格「选中的款模板」", "app_token": tpl["app_token"], "table_id": selected["table_id"],
             "kind": "template", "key": None}
        )
    for book in books.books(ctx):
        selected = (book.get("templates") or {}).get("selected") or {}
        if selected.get("table_id"):
            out.append(
                {"label": f"{book['book_id']}「选中的款模板」", "app_token": book["app_token"],
                 "table_id": selected["table_id"], "kind": "book", "key": book["book_id"]}
            )
    return out


def _forget_scene_images(ctx: Ctx, item: dict[str, Any]) -> None:
    """列删掉以后，表格信息 / 模板记录里的 extra_fields 也去掉「场景图」（新的一天照模板记录抄 extra_fields）。"""
    from . import books  # 同上

    name = feishu_table.DROPPED_SCENE_FIELD

    def drop(meta: dict[str, Any]) -> bool:
        extra = list(meta.get("extra_fields") or [])
        if name not in extra:
            return False
        meta["extra_fields"] = [f for f in extra if f != name]
        return True

    if item["kind"] == "legacy":
        info = feishu_table.base(ctx) or {}
        if drop(info):
            feishu_table._save_base(ctx, info)
    elif item["kind"] == "day":
        info = books.day_info(ctx, item["key"]) or {}
        if drop(info):
            books.save_day(ctx, info)
    elif item["kind"] == "template":
        tpl = books.template(ctx) or {}
        if drop((tpl.get("tables") or {}).get("selected") or {}):
            books._save_template(ctx, tpl)
    else:
        book = books.book(ctx, item["key"]) or {}
        if drop((book.get("templates") or {}).get("selected") or {}):
            books.save_book(ctx, book)


def drop_scene_images(ctx: Ctx, *, by: str, dry_run: bool = False) -> dict[str, Any]:
    """「选中的款」不再放套图缩略图（2026-10-29）：删掉各张「选中的款」和「选中的款模板」里的附件列「场景图」。
    dry_run 只列哪些表还有这一列。持表格同步租约（有同步在跑就等它放开）；可以重复执行，没有这一列的表不动。"""
    from . import views  # 视图模块依赖表格模块，放到用时再导入

    if not feishu_table.enabled(ctx):
        raise StageError("table.enabled 为 false：表格没有打开")
    name = feishu_table.DROPPED_SCENE_FIELD
    tables = _scene_image_tables(ctx)
    out: dict[str, Any] = {"dry_run": dry_run, "tables": [], "dropped": [], "errors": []}
    if not dry_run:
        candidates.take_lease(ctx, busy="表格正在同步，过一会儿再删「场景图」列")
    try:
        with feishu.Client(ctx) as api:
            for item in tables:
                where = {"app_token": item["app_token"]}
                try:
                    field = next(
                        (
                            f
                            for f in views.list_fields(api, where, item["table_id"])
                            if f.get("field_name") == name
                        ),
                        None,
                    )
                    out["tables"].append({"table": item["label"], "has_column": bool(field)})
                    if dry_run:
                        continue
                    if not field:
                        _forget_scene_images(ctx, item)
                        continue
                    api.call(
                        "DELETE",
                        f"/bitable/v1/apps/{item['app_token']}/tables/{item['table_id']}/fields/{field['field_id']}",
                    )
                except feishu.FeishuError as exc:
                    out["errors"].append({"table": item["label"], "error": f"{exc}"[:300]})
                    continue
                _forget_scene_images(ctx, item)
                out["dropped"].append(item["label"])
                if not dry_run:
                    lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    finally:
        if not dry_run:
            lease.drop(ctx, feishu_table.LEASE_KEY)
    if out["dropped"]:
        with ctx.db.tx() as conn:
            log_event(
                conn, "maintenance", "scene_images_dropped", {"by": by, "tables": out["dropped"]}
            )
    return out


# ---------------------------------------------------------------- 方向按新规则重出

REDO_NOTE = "方向按新规则重出"
# 关掉的待审：选款与质检存疑（都是等人看这些旧图的）
_REDO_REVIEWS = ("asset_selection", "asset_qc")


def _redo_targets(ctx: Ctx, ids: Iterable[str]) -> list[str]:
    """J 编号原样；方向的 V 编号换成它的任务。"""
    jobs: list[str] = []
    for raw in ids:
        ident = str(raw).strip()
        if re.fullmatch(r"V\d+", ident):
            row = ctx.db.q1("SELECT job_id FROM jobs WHERE variant_id = ?", (ident,))
            if not row:
                raise StageError(f"方向 {ident} 还没有任务（没确认出图）")
            ident = row["job_id"]
        elif not re.fullmatch(r"J\d+", ident):
            raise StageError(f"编号不对：{raw}（任务像 J0076，方向像 V0107）")
        if ident not in jobs:
            jobs.append(ident)
    if not jobs:
        raise StageError("要写清楚重出哪几个任务（J 编号或方向的 V 编号）")
    return jobs


def redo_jobs(
    ctx: Ctx, ids: Iterable[str], *, by: str, dry_run: bool = False
) -> dict[str, Any]:
    """方向按新规则重出：作废这些任务已出的图，回到 S06 按现在的提示词与画面描述重新写生成指令、重新出图。

    - 图（含修复版）标「已被新版本取代」，文件与出图记录留着；选款 / 质检待审关掉（不算人工没选），候选表里的行删掉；
    - 出图槽位重置，之前的出图调用标作废（不计尝试次数，幂等键接着编号），新图是新编号；
    - 已出完（done）的批次放回 planned，选中的方向接着出，出完重新生成批次反馈。
    拒绝：任务还没结束（排队、在出、阻断）、有图已经被选中、有结果未知的出图请求。
    """
    job_ids = _redo_targets(ctx, ids)
    plans: list[dict[str, Any]] = []
    for job_id in job_ids:
        job = get_job(ctx, job_id)
        if job["status"] not in ("complete", "failed"):
            raise StageError(f"{job_id} 还没出完（{job['status']}）：等它结束再重出")
        assets = [
            dict(r)
            for r in ctx.db.q(
                "SELECT asset_id, status, human_decision FROM assets WHERE job_id = ? ORDER BY asset_id",
                (job_id,),
            )
        ]
        approved = [a["asset_id"] for a in assets if a["human_decision"] == "approved"]
        if approved:
            raise StageError(
                f"{job_id} 的 {'、'.join(approved)} 已经选中：选中的款不能作废，这个方向不重出"
            )
        calls = [
            dict(r)
            for r in ctx.db.q(
                "SELECT call_id, stage, status, request_meta_json FROM provider_calls"
                " WHERE scope_id = ? AND kind = 'image' AND stage IN ('T01', 'T01R') ORDER BY call_id",
                (job_id,),
            )
        ]
        unknown = [
            c["call_id"]
            for c in calls
            if c["status"] in ("reserved", "submitted", "unknown")
        ]
        if unknown:
            raise StageError(
                f"{job_id} 有结果未知的出图请求 {'、'.join(unknown)}：先用 podctl reconcile 对账"
            )
        live = [a["asset_id"] for a in assets if a["status"] != "superseded"]
        reviews = _rows_in(
            ctx.db,
            "SELECT review_id FROM reviews WHERE status = 'open'"
            f" AND subject_type IN ('{_REDO_REVIEWS[0]}', '{_REDO_REVIEWS[1]}')"
            " AND subject_id IN ({in}) ORDER BY review_id",
            live,
        )
        plans.append(
            {
                "job_id": job_id,
                "batch_id": job["batch_id"],
                "variant_id": job["variant_id"],
                "assets": live,
                "reviews": [r["review_id"] for r in reviews],
                "calls": [
                    c["call_id"]
                    for c in calls
                    if c["stage"] == "T01"
                    and not (loads(c["request_meta_json"], {}) or {}).get("voided")
                ],
            }
        )
    batches = sorted({p["batch_id"] for p in plans})
    summary = {
        "jobs": job_ids,
        "variants": [p["variant_id"] for p in plans],
        "assets": [a for p in plans for a in p["assets"]],
        "reviews": sum(len(p["reviews"]) for p in plans),
        "voided_calls": sum(len(p["calls"]) for p in plans),
        "batches": batches,
    }
    if dry_run:
        return {"dry_run": True, **summary}
    cand = candidates.delete_rows(ctx, summary["assets"])
    now = now_iso()
    reopened: list[str] = []
    with ctx.db.tx() as conn:
        for p in plans:
            for asset_id in p["assets"]:
                conn.execute(
                    "UPDATE assets SET status = 'superseded', updated_at = ? WHERE asset_id = ?",
                    (now, asset_id),
                )
            for review_id in p["reviews"]:
                conn.execute(
                    "UPDATE reviews SET status = 'dismissed', decided_by = ?, decided_at = ?, note = ?"
                    " WHERE review_id = ? AND status = 'open'",
                    (by, now, REDO_NOTE, review_id),
                )
            for call_id in p["calls"]:
                row = conn.execute(
                    "SELECT request_meta_json FROM provider_calls WHERE call_id = ?",
                    (call_id,),
                ).fetchone()
                meta = loads(row["request_meta_json"], {}) or {}
                meta.update({"voided": True, "void_reason": REDO_NOTE, "voided_by": by})
                conn.execute(
                    "UPDATE provider_calls SET request_meta_json = ? WHERE call_id = ?",
                    (dumps(meta), call_id),
                )
            conn.execute(
                "UPDATE job_slots SET status = 'pending', attempts = 0, asset_id = NULL, last_error = NULL,"
                " updated_at = ? WHERE job_id = ?",
                (now, p["job_id"]),
            )
            conn.execute(
                "UPDATE jobs SET status = 'ready', stage = 'S06', render_spec_json = NULL, attempt = 0,"
                " blocked_reason = NULL, resume_condition = NULL, last_error = NULL, lease_owner = NULL,"
                " lease_until = NULL, updated_at = ? WHERE job_id = ?",
                (now, p["job_id"]),
            )
            log_event(
                conn,
                p["job_id"],
                "job_redo",
                {
                    "by": by,
                    "assets": p["assets"],
                    "reviews": len(p["reviews"]),
                    "voided_calls": len(p["calls"]),
                },
            )
        for batch_id in batches:
            row = conn.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row and row["status"] == "done":
                conn.execute(
                    "UPDATE batches SET status = 'planned', updated_at = ? WHERE batch_id = ?",
                    (now, batch_id),
                )
                log_event(
                    conn,
                    batch_id,
                    "batch_reopened",
                    {
                        "by": by,
                        "reason": REDO_NOTE,
                        "jobs": [
                            p["job_id"] for p in plans if p["batch_id"] == batch_id
                        ],
                    },
                )
                reopened.append(batch_id)
    return {
        "dry_run": False,
        **summary,
        "batches_reopened": reopened,
        "candidates": cand,
    }


# ---------------------------------------------------------------- 彻底删掉旧数据


def _chunks(ids: Iterable[Any]) -> list[list[Any]]:
    items = list(ids)
    return [items[i : i + _CHUNK] for i in range(0, len(items), _CHUNK)]


def _rows_in(db: Any, sql: str, ids: Iterable[Any]) -> list[dict[str, Any]]:
    """sql 里的 {in} 换成占位符，分批查。"""
    out: list[dict[str, Any]] = []
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        out += [dict(r) for r in db.q(sql.format(**{"in": marks}), tuple(chunk))]
    return out


def _delete_in(conn: Any, table: str, column: str, ids: Iterable[Any]) -> None:
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", tuple(chunk))


def _old_analyses(db: Any, seed_id: str, keep: set[str]) -> tuple[set[str], set[str]]:
    """这张参考图上没人再用的拆解与它们的假设：不被 keep 里的拆解覆盖，也不是参考图的最新指针。"""
    seed = db.q1(
        "SELECT latest_analysis_id, latest_hypothesis_set_id FROM seeds WHERE seed_id = ?",
        (seed_id,),
    )
    analyses: set[str] = set()
    hypotheses: set[str] = set()
    for row in db.q("SELECT analysis_id FROM analyses WHERE seed_id = ?", (seed_id,)):
        aid = row["analysis_id"]
        if aid in keep or aid == seed["latest_analysis_id"]:
            continue
        hyps = {
            h["hypothesis_set_id"]
            for h in db.q(
                "SELECT hypothesis_set_id FROM hypotheses WHERE analysis_id = ?", (aid,)
            )
        }
        if seed["latest_hypothesis_set_id"] in hyps:
            continue
        analyses.add(aid)
        hypotheses |= hyps
    return analyses, hypotheses


def _purge_scope(
    ctx: Ctx, family_id: str | None, keep_version: int | None, seeds: list[str]
) -> dict[str, Any]:
    """要删的全部编号（只读）。"""
    db = ctx.db
    batches: set[str] = set()
    families: list[tuple[str, int]] = []
    analyses: set[str] = set()
    hypotheses: set[str] = set()
    kept_seed = None
    if family_id:
        versions = [
            r["family_version"]
            for r in db.q(
                "SELECT family_version FROM families WHERE family_id = ? ORDER BY family_version",
                (family_id,),
            )
        ]
        if not versions:
            raise StageError(f"没有系列 {family_id}")
        if keep_version != versions[-1]:
            raise StageError(
                f"{family_id} 最新是 v{versions[-1]}：--keep-version 要写最新版本，只删比它旧的"
            )
        families += [(family_id, v) for v in versions if v < keep_version]
        batches |= {
            r["batch_id"]
            for r in db.q(
                "SELECT batch_id FROM batches WHERE family_id = ? AND family_version < ?",
                (family_id, keep_version),
            )
        }
        kept_seed = db.q1(
            "SELECT seed_id FROM families WHERE family_id = ? AND family_version = ?",
            (family_id, keep_version),
        )["seed_id"]
        keep = {
            r["analysis_id"]
            for r in db.q(
                "SELECT analysis_id FROM families WHERE seed_id = ?"
                " AND NOT (family_id = ? AND family_version < ?)",
                (kept_seed, family_id, keep_version),
            )
        }
        analyses, hypotheses = _old_analyses(db, kept_seed, keep)
    missing = [
        s for s in seeds if not db.q1("SELECT 1 FROM seeds WHERE seed_id = ?", (s,))
    ]
    seeds = [s for s in seeds if s not in missing]
    for seed_id in seeds:
        if seed_id == kept_seed:
            raise StageError(
                f"{seed_id} 是要保留的系列 {family_id} 的参考图，不能整张删"
            )
        fams = db.q(
            "SELECT family_id, family_version FROM families WHERE seed_id = ?",
            (seed_id,),
        )
        families += [(f["family_id"], f["family_version"]) for f in fams]
        for fid in {f["family_id"] for f in fams}:
            batches |= {
                r["batch_id"]
                for r in db.q(
                    "SELECT batch_id FROM batches WHERE family_id = ?", (fid,)
                )
            }
        aids = {
            r["analysis_id"]
            for r in db.q(
                "SELECT analysis_id FROM analyses WHERE seed_id = ?", (seed_id,)
            )
        }
        analyses |= aids
        hypotheses |= {
            r["hypothesis_set_id"]
            for r in _rows_in(
                db,
                "SELECT hypothesis_set_id FROM hypotheses WHERE analysis_id IN ({in})",
                aids,
            )
        }
    variants = _rows_in(
        db,
        "SELECT variant_id, family_id FROM variants WHERE batch_id IN ({in})",
        batches,
    )
    jobs = _rows_in(
        db, "SELECT job_id, status FROM jobs WHERE batch_id IN ({in})", batches
    )
    job_ids = {j["job_id"] for j in jobs}
    assets = _rows_in(
        db,
        "SELECT asset_id, human_decision FROM assets WHERE job_id IN ({in})",
        job_ids,
    )
    asset_ids = {a["asset_id"] for a in assets}
    variant_ids = {v["variant_id"] for v in variants}
    family_labels = {f"{f}@v{v}" for f, v in families}
    subjects = asset_ids | variant_ids | job_ids | batches | set(seeds)
    reviews = {
        r["review_id"]: r["status"]
        for r in _rows_in(
            db,
            "SELECT review_id, status FROM reviews WHERE batch_id IN ({in})",
            batches,
        )
        + _rows_in(
            db,
            "SELECT review_id, status FROM reviews WHERE subject_id IN ({in})",
            subjects,
        )
        + _rows_in(
            db,
            "SELECT review_id, status FROM reviews WHERE subject_type = 'family' AND subject_id IN ({in})",
            family_labels,
        )
    }
    feedback = {
        r["feedback_id"]
        for r in _rows_in(
            db, "SELECT feedback_id FROM feedback WHERE batch_id IN ({in})", batches
        )
        + _rows_in(
            db, "SELECT feedback_id FROM feedback WHERE asset_id IN ({in})", asset_ids
        )
    }
    table_rows = {
        r["asset_id"]
        for r in _rows_in(
            db, "SELECT asset_id FROM table_rows WHERE asset_id IN ({in})", asset_ids
        )
    }
    candidate_rows = {
        r["asset_id"]
        for r in _rows_in(
            db,
            "SELECT asset_id FROM candidate_rows WHERE asset_id IN ({in})",
            asset_ids,
        )
    }
    bg_tasks = {
        r["task_id"]: r["status"]
        for r in _rows_in(
            db,
            "SELECT task_id, status FROM bg_tasks WHERE subject IN ({in})",
            batches | set(seeds) | job_ids,
        )
    }
    # 这些图出过的套图：编号、图片目录、压缩包；这些批次 / 参考图的交付包
    scene = scenes.purge_scope(ctx, sorted(asset_ids))
    bundle = packages.purge_scope(ctx, list(seeds), sorted(batches))
    scopes = subjects | set(reviews) | set(bg_tasks) | analyses | set(scene["sets"])
    events = {
        r["event_id"]
        for r in _rows_in(
            db, "SELECT event_id FROM events WHERE scope IN ({in})", scopes
        )
    }
    # 保留系列上旧版本的动态（提出、认可）与保留参考图上旧拆解的动态
    old_versions = {v for f, v in families if f == family_id}
    for scope, key, values in (
        (family_id, "version", old_versions),
        (kept_seed, "analysis_id", analyses),
    ):
        if not scope or not values:
            continue
        for r in db.q(
            "SELECT event_id, payload_json FROM events WHERE scope = ?", (scope,)
        ):
            if (loads(r["payload_json"], {}) or {}).get(key) in values:
                events.add(r["event_id"])
    prefixes = sorted(
        {f"assets/families/{v['family_id']}/{v['variant_id']}/" for v in variants}
        | {f"assets/seeds/{s}/" for s in seeds}
        | set(scene["prefixes"])
    )
    local_files = [f"cache/thumbs/seed-{s}.jpg" for s in seeds]
    # 平面图案的缩略图按图案文件缓存（seed-S01-pattern-1.jpg）
    thumbs = ctx.home / "cache" / "thumbs"
    if seeds and thumbs.is_dir():
        local_files += sorted(
            f"cache/thumbs/{p.name}"
            for s in seeds
            for p in thumbs.glob(f"seed-{s}-*.jpg")
        )
    reports = ctx.home / "reports"
    if batches and reports.is_dir():
        pattern = re.compile(
            "(?<![A-Za-z0-9])(" + "|".join(sorted(batches)) + ")(?![0-9])"
        )
        local_files += sorted(
            f"reports/{p.name}"
            for p in reports.iterdir()
            if p.is_file() and pattern.search(p.name)
        )
    return {
        "family_id": family_id,
        "keep_version": keep_version,
        "batches": sorted(batches),
        "families": sorted(families),
        "seeds": list(seeds),
        "missing_seeds": missing,
        "variants": sorted(variant_ids),
        "jobs": sorted(job_ids),
        "running_jobs": sorted(j["job_id"] for j in jobs if j["status"] == "running"),
        "assets": sorted(asset_ids),
        "approved_assets": sorted(
            a["asset_id"] for a in assets if a["human_decision"] == "approved"
        ),
        "reviews": reviews,
        "feedback": sorted(feedback),
        "table_rows": sorted(table_rows),
        "candidate_rows": sorted(candidate_rows),
        "scene_sets": scene["sets"],
        "scene_active": scene["active"],
        "scene_exports": scene["exports"],
        "scene_packages": bundle["packages"],
        "package_rows": bundle["rows"],
        "package_exports": bundle["exports"],
        "bg_tasks": bg_tasks,
        "events": sorted(events),
        "analyses": sorted(analyses),
        "hypotheses": sorted(hypotheses),
        "prefixes": prefixes,
        "local_files": local_files,
    }


def _purge_summary(plan: dict[str, Any]) -> dict[str, Any]:
    counts = {
        key: len(plan[key])
        for key in (
            "batches",
            "variants",
            "jobs",
            "assets",
            "approved_assets",
            "reviews",
            "feedback",
            "table_rows",
            "candidate_rows",
            "scene_sets",
            "scene_packages",
            "bg_tasks",
            "events",
            "families",
            "analyses",
            "hypotheses",
            "seeds",
            "prefixes",
            "local_files",
        )
    }
    counts["open_reviews"] = sum(1 for s in plan["reviews"].values() if s == "open")
    return {
        "counts": counts,
        "batches": plan["batches"],
        "family_versions": [f"{f}@v{v}" for f, v in plan["families"]],
        "seeds": plan["seeds"],
        "missing_seeds": plan["missing_seeds"],
        "analyses": plan["analyses"],
        "approved_assets": plan["approved_assets"],
        "local_files": plan["local_files"],
    }


def _purge_guard(ctx: Ctx, plan: dict[str, Any]) -> None:
    if plan["running_jobs"]:
        raise StageError(f"{'、'.join(plan['running_jobs'])} 正在出图：等它停下再删")
    worker = state.worker_status(ctx.db)
    if worker and worker["alive"]:
        only = set((worker.get("info") or {}).get("only_jobs") or [])
        if not only:
            raise StageError(
                "worker 正在跑全量出图，可能会领到要删的任务：等它结束再删"
            )
        if only & set(plan["jobs"]):
            raise StageError(
                f"worker 正在推进要删的任务 {'、'.join(sorted(only & set(plan['jobs'])))}：等它结束再删"
            )
    busy = sorted(t for t, status in plan["bg_tasks"].items() if status == "running")
    if busy:
        raise StageError(f"后台任务 {'、'.join(busy)} 还在跑：等它结束再删")
    if plan["scene_active"]:
        raise StageError(
            f"套图 {'、'.join(plan['scene_active'])} 还在出：先 podctl scenes cancel 或等它出完再删"
        )
    from . import stores  # 店铺上架依赖本模块之外的表格，放到用时再导入

    held = stores.purge_blockers(ctx, plan["assets"])
    if held:
        raise StageError(
            "这些款领了货号、在上架或已上架（Temu 上还在用，设计图和图包不能删）："
            + "、".join(held[:20])
        )


def purge_old(
    ctx: Ctx,
    *,
    family_id: str | None = None,
    keep_version: int | None = None,
    seeds: Iterable[str] = (),
    by: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """彻底删掉不再用的旧数据，不可恢复（先 dry_run 看清单）。

    - 系列 family_id 里低于 keep_version（必须是最新版本）的批次：方向、任务、出图槽位、图（含选中的款）、
      待审、反馈、选款表与候选表里的行（整张参考图删掉时连候选表和验收压缩包）、这些编号的动态与后台任务，以及这些旧版本与没人再用的拆解、假设；
    - seeds：整张参考图连同它的系列、批次、拆解；
    - 库里删完再删文件：OSS 对象、登记与本机副本（按方向 / 参考图目录），以及文件名带这些批次号的 reports 文件。
    调用账本（provider_calls）和后台日志文件不删，编号计数器不回收。
    """
    seeds = list(dict.fromkeys(seeds))
    if not family_id and not seeds:
        raise StageError("至少给 --family（配 --keep-version）或 --seed")
    if family_id and keep_version is None:
        raise StageError("给了 --family 就要写 --keep-version（要保留的最新版本）")
    plan = _purge_scope(ctx, family_id, keep_version, seeds)
    summary = _purge_summary(plan)
    if dry_run:
        return {"dry_run": True, **summary}
    _purge_guard(ctx, plan)
    table = feishu_table.delete_rows(ctx, plan["assets"])
    # 候选表里的行（整张参考图删掉时连表和压缩包）
    cand = candidates.delete_rows(ctx, plan["assets"], seeds=plan["seeds"])
    with ctx.db.tx() as conn:
        _delete_in(conn, "table_rows", "asset_id", plan["assets"])
        _delete_in(conn, "listing_marks", "asset_id", plan["assets"])
        _delete_in(conn, "selection_reasons", "asset_id", plan["assets"])
        _delete_in(conn, "selected_downloads", "asset_id", plan["assets"])
        _delete_in(conn, "selected_rows", "asset_id", plan["assets"])
        _delete_in(conn, "candidate_rows", "asset_id", plan["assets"])
        _delete_in(conn, "candidate_scene_rows", "asset_id", plan["assets"])
        _delete_in(conn, "candidate_scene_rows", "seed_id", plan["seeds"])
        _delete_in(conn, "candidate_controls", "seed_id", plan["seeds"])
        _delete_in(conn, "candidate_actions", "seed_id", plan["seeds"])
        _delete_in(conn, "candidate_orders", "seed_id", plan["seeds"])
        _delete_in(conn, "candidate_slots", "seed_id", plan["seeds"])
        _delete_in(conn, "candidate_slots", "job_id", plan["jobs"])
        _delete_in(conn, "store_listings", "asset_id", plan["assets"])
        _delete_in(conn, "listing_titles", "asset_id", plan["assets"])
        _delete_in(conn, "listing_assets", "asset_id", plan["assets"])
        _delete_in(conn, "temu_rows", "asset_id", plan["assets"])
        _delete_in(conn, "temu_brands", "asset_id", plan["assets"])
        _delete_in(conn, "candidate_codes", "asset_id", plan["assets"])
        _delete_in(conn, "candidate_scene_codes", "asset_id", plan["assets"])
        # 全自动上架与留痕（2026-10-30）的表：这些款的轨迹、当前环节、人改格子、暂停、标题版本与同步、撤销窗口
        for name in (
            "trace_events",
            "pipeline_status",
            "table_cell_log",
            "table_row_snapshots",
            "listing_holds",
            "listing_title_versions",
            "title_sync",
            "store_tick_pending",
            "scene_set_flags",
            "temu_publish_records",
        ):
            _delete_in(conn, name, "asset_id", plan["assets"])
        _delete_in(conn, "table_cells", "asset_id", plan["assets"])
        _delete_in(conn, "scene_images", "set_id", plan["scene_sets"])
        _delete_in(conn, "scene_files", "set_id", plan["scene_sets"])
        _delete_in(conn, "scene_set_clients", "set_id", plan["scene_sets"])
        _delete_in(conn, "scene_sets", "set_id", plan["scene_sets"])
        for seed_id, batch_id in plan["package_rows"]:
            for name in ("scene_packages", "package_downloads"):
                conn.execute(
                    f"DELETE FROM {name} WHERE seed_id = ? AND batch_id = ?",
                    (seed_id, batch_id),
                )
        _delete_in(conn, "package_downloads", "seed_id", plan["seeds"])
        _delete_in(conn, "job_slots", "job_id", plan["jobs"])
        _delete_in(conn, "feedback", "feedback_id", plan["feedback"])
        _delete_in(conn, "reviews", "review_id", plan["reviews"])
        _delete_in(conn, "events", "event_id", plan["events"])
        _delete_in(conn, "bg_tasks", "task_id", plan["bg_tasks"])
        _delete_in(conn, "assets", "asset_id", plan["assets"])
        _delete_in(conn, "jobs", "job_id", plan["jobs"])
        _delete_in(conn, "variants", "variant_id", plan["variants"])
        _delete_in(conn, "derived_rounds", "source_batch_id", plan["batches"])
        _delete_in(conn, "derived_rounds", "batch_id", plan["batches"])
        _delete_in(conn, "batches", "batch_id", plan["batches"])
        for fid, version in plan["families"]:
            conn.execute(
                "DELETE FROM families WHERE family_id = ? AND family_version = ?",
                (fid, version),
            )
        _delete_in(conn, "hypotheses", "hypothesis_set_id", plan["hypotheses"])
        _delete_in(conn, "seed_descriptions", "analysis_id", plan["analyses"])
        _delete_in(conn, "seed_descriptions", "seed_id", plan["seeds"])
        _delete_in(conn, "analyses", "analysis_id", plan["analyses"])
        _delete_in(conn, "seed_patterns", "seed_id", plan["seeds"])
        _delete_in(conn, "seed_names", "seed_id", plan["seeds"])
        _delete_in(conn, "seed_shelf", "seed_id", plan["seeds"])
        _delete_in(conn, "seeds", "seed_id", plan["seeds"])
        log_event(
            conn,
            "maintenance",
            "purge_old",
            {"by": by, "family_id": family_id, "keep_version": keep_version, **summary},
        )
    files: dict[str, Any] = {"objects": 0, "local_files": 0, "errors": []}
    for prefix in plan["prefixes"]:
        out = store.delete_tree(ctx, prefix)
        files["objects"] += out["objects"]
        files["local_files"] += out["local_files"]
        files["errors"] += out["errors"]
    for stored in plan["local_files"]:
        path = store.resolve(ctx.home, stored)
        if path.is_file():
            path.unlink()
            files["local_files"] += 1
    # 套图压缩包、交付包没登记在 storage_objects 里，单独删
    files["errors"] += scenes.delete_exports(ctx, plan["scene_exports"])
    files["errors"] += scenes.delete_exports(ctx, plan["package_exports"])
    return {
        "dry_run": False,
        **summary,
        "table": table,
        "candidates": cand,
        "files": files,
    }
