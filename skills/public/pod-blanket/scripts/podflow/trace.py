"""单款流水线轨迹与当前环节（2026-10-30，feature-plans/pod-listing-autopilot）。

- trace_events：每次状态迁移一条（只追加）：哪一款、哪家店、哪个环节、从什么到什么、谁 / 哪个来源、为什么。
  选款、店铺、货号、套图、上架素材、标题、确认 / 暂停、建箱、品牌、发布、已上架、人改格子都往这里记（`record` / `log`）。
- pipeline_status：每（款, 店）现在在哪一环、从什么时候起、卡在哪、从什么时候起卡。`refresh` 按库里的现状算
  （不靠各处自己维护），环节或卡点变了写一条 trace（stage = pipeline）。「选中的款」的「当前环节」「环节开始」读它。
- `timeline` 把一款的轨迹、事件、审核、调用账本、人改格子和各表的时间戳合成一条时间线（`podctl asset IMG… --timeline`）。
"""

from __future__ import annotations

from typing import Any

from .context import Ctx
from .util import dumps, loads, now_iso

# 环节（顺序 = 流水线先后；「当前环节」单选的选项照这个顺序）
STAGES: tuple[tuple[str, str], ...] = (
    ("code", "领货号"),
    ("scenes_wait", "等出套图"),
    ("scenes", "出套图中"),
    ("scenes_human", "套图等人工"),
    ("scenes_paused", "套图暂停"),
    ("scenes_failed", "套图没出成"),
    ("assets", "准备上架图"),
    ("confirm", "待确认上架"),
    ("hold", "已暂停上架"),
    ("queued", "排队上架"),
    ("box", "建箱中"),
    ("brand", "写品牌"),
    ("publish_wait", "等发布"),
    ("publishing", "发布中"),
    ("listed", "已上架"),
    ("failed", "上架失败"),
    ("cancelled", "不上了"),
)
LABELS = dict(STAGES)
ORDER = {key: i for i, (key, _) in enumerate(STAGES)}
# 一款几家店汇总成一个环节时先看这几个（要人看的排前面）
ATTENTION = ("failed", "scenes_failed", "hold", "scenes_human")
FINAL = frozenset({"listed", "cancelled"})
STAGE_PIPELINE = "pipeline"
_REASON_CHARS = 300


def record(
    conn: Any,
    asset_id: str,
    stage: str,
    *,
    to: str | None = None,
    frm: str | None = None,
    store: str | None = None,
    code: str | None = None,
    set_id: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    payload: Any = None,
) -> None:
    """在调用方的事务里记一条轨迹。"""
    conn.execute(
        "INSERT INTO trace_events(ts, asset_id, store_key, code, set_id, stage, from_state, to_state, actor,"
        " source, reason, payload_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            asset_id,
            store,
            code,
            set_id,
            stage,
            frm,
            to,
            actor,
            source,
            (reason or "")[:_REASON_CHARS] or None,
            None if payload is None else dumps(payload),
        ),
    )


def log(ctx: Ctx, asset_id: str, stage: str, **kwargs: Any) -> None:
    """自己开一个事务记一条轨迹。"""
    with ctx.db.tx() as conn:
        record(conn, asset_id, stage, **kwargs)


# ---------------------------------------------------------------- 当前环节


def _scene_stage(latest: dict[str, Any] | None) -> tuple[str, str | None] | None:
    """套图那一段的环节；套图出完（done / partial）返回 None，接着看上架素材。"""
    if latest is None or latest["status"] == "queued":
        return "scenes_wait", None
    status = latest["status"]
    if status in ("submitted", "running"):
        return "scenes", latest.get("step_label")
    if status == "waiting_human":
        return "scenes_human", latest.get("step_label") or "mooya 等人工"
    if status == "paused":
        return "scenes_paused", latest.get("last_error")
    if status in ("failed", "cancelled"):
        return "scenes_failed", latest.get("last_error")
    return None


def stage_of(
    ctx: Ctx, row: dict[str, Any], latest: dict[str, Any] | None
) -> tuple[str, str | None]:
    """一（款, 店）现在的环节与卡点。row 是 store_listings 那一行，latest 是这一款最近一套套图（没有为 None）。"""
    from . import temu  # 上架模块依赖本模块，放到用时再导入

    state = row["state"]
    if state == "cancelled":
        return "cancelled", None
    if state == temu.STATE_LISTED:
        return "listed", None
    if state == temu.STATE_FAILED:
        return "failed", row.get("last_error")
    if state in (temu.STATE_CREATING, temu.STATE_UNKNOWN):
        return "box", "建箱结果未知，核对中" if state == temu.STATE_UNKNOWN else None
    if state == temu.STATE_BOX:
        note = temu.brand_note(ctx, row)
        if note:
            return "brand", note
        if temu.held(ctx, row["asset_id"]) and temu.mode_for(ctx, row["store_key"]) == temu.MODE_AUTO:
            return "hold", "人取消了「确认上架」：进了采集箱先不发布，再勾接着发布"
        return "publish_wait", temu.publish_wait_reason(ctx, row)
    if state in (temu.STATE_PUBLISHING, temu.STATE_PUBLISH_UNKNOWN):
        return (
            "publishing",
            "发布请求结果未知，等妙手的结果"
            if state == temu.STATE_PUBLISH_UNKNOWN
            else None,
        )
    if not row.get("code"):
        return "code", row.get("code_error")
    scene = _scene_stage(latest)
    if scene:
        return scene
    reason = temu.blocker(ctx, row)
    if reason:
        return "assets", reason
    if state == temu.STATE_CHOSEN:
        if temu.held(ctx, row["asset_id"]):
            return "hold", "人取消了「确认上架」，再勾就接着上"
        if row.get("error_kind") == temu.ERROR_REJECTED:
            return "confirm", row.get("last_error")
        if temu.auto_confirm_on(ctx, row["store_key"]):
            return "queued", None
        return "confirm", None
    return "queued", None


def _current(
    ctx: Ctx, asset_ids: list[str] | None
) -> dict[tuple[str, str], dict[str, Any]]:
    if asset_ids is None:
        rows = ctx.db.q("SELECT * FROM pipeline_status")
    else:
        rows = []
        ids = sorted(set(asset_ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows += ctx.db.q(
                f"SELECT * FROM pipeline_status WHERE asset_id IN ({','.join('?' * len(chunk))})",
                tuple(chunk),
            )
    return {(r["asset_id"], r["store_key"]): dict(r) for r in rows}


def refresh(ctx: Ctx, asset_ids: list[str] | None = None) -> dict[str, Any]:
    """按库里的现状重算这些款（缺省所有定了店铺的款）每家店的环节；变了的记一条轨迹。返回 {checked, changed}。"""
    from . import scenes, stores  # 都依赖本模块之外的很多表，用时再导入

    items = stores.rows(ctx, asset_ids=asset_ids, active=False)
    if not items:
        return {"checked": 0, "changed": 0}
    latest = scenes.latest_sets(ctx, sorted({r["asset_id"] for r in items}))
    old = _current(ctx, sorted({r["asset_id"] for r in items}))
    now = now_iso()
    changes: list[dict[str, Any]] = []
    for row in items:
        key = (row["asset_id"], row["store_key"])
        before = old.get(key)
        if before and before["stage"] == "cancelled" and row["state"] == "cancelled":
            continue
        stage, blocker = stage_of(ctx, row, latest.get(row["asset_id"]))
        blocker = (blocker or "")[:_REASON_CHARS] or None
        if (
            before
            and before["stage"] == stage
            and (before.get("blocker") or None) == blocker
        ):
            continue
        changes.append(
            {
                "row": row,
                "stage": stage,
                "blocker": blocker,
                "stage_since": before["stage_since"]
                if before and before["stage"] == stage
                else now,
                "blocker_since": (
                    (
                        before.get("blocker_since")
                        if before and before.get("blocker") == blocker
                        else now
                    )
                    if blocker
                    else None
                ),
                "before": before,
            }
        )
    if not changes:
        return {"checked": len(items), "changed": 0}
    with ctx.db.tx() as conn:
        for change in changes:
            row = change["row"]
            conn.execute(
                "INSERT INTO pipeline_status(asset_id, store_key, stage, stage_since, blocker, blocker_since,"
                " detail_json, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(asset_id, store_key)"
                " DO UPDATE SET stage = excluded.stage, stage_since = excluded.stage_since,"
                " blocker = excluded.blocker, blocker_since = excluded.blocker_since,"
                " detail_json = excluded.detail_json, updated_at = excluded.updated_at",
                (
                    row["asset_id"],
                    row["store_key"],
                    change["stage"],
                    change["stage_since"],
                    change["blocker"],
                    change["blocker_since"],
                    dumps(
                        {
                            "state": row["state"],
                            "code": row.get("code"),
                            "detail_id": row.get("detail_id"),
                        }
                    ),
                    now,
                ),
            )
            before = change["before"] or {}
            record(
                conn,
                row["asset_id"],
                STAGE_PIPELINE,
                frm=before.get("stage"),
                to=change["stage"],
                store=row["store_key"],
                code=row.get("code"),
                reason=change["blocker"],
                source="refresh",
            )
    return {"checked": len(items), "changed": len(changes)}


def rows(ctx: Ctx, asset_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM pipeline_status WHERE asset_id = ? ORDER BY store_key",
            (asset_id,),
        )
    ]


def aggregate(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """一款几家店汇总成一个环节：要人看的（失败 / 暂停 / 等人工）先，其余取最落后的那一家。没有为 None。"""
    live = [r for r in items if r["stage"] != "cancelled"] or items
    if not live:
        return None
    attention = [r for r in live if r["stage"] in ATTENTION]
    if attention:
        pick = min(attention, key=lambda r: ATTENTION.index(r["stage"]))
    else:
        pick = min(live, key=lambda r: (ORDER.get(r["stage"], 99), r["stage_since"]))
    return {
        "stage": pick["stage"],
        "label": LABELS.get(pick["stage"], pick["stage"]),
        "since": pick["stage_since"],
        "blocker": pick.get("blocker"),
        "blocker_since": pick.get("blocker_since"),
    }


def summary(ctx: Ctx, asset_id: str) -> dict[str, Any] | None:
    return aggregate(rows(ctx, asset_id))


# ---------------------------------------------------------------- 时间线


def _when(value: Any) -> str | None:
    return str(value) if value else None


def timeline(ctx: Ctx, asset_id: str, *, limit: int = 400) -> dict[str, Any]:
    """一款的时间线：轨迹、事件、审核、出图调用、人改格子，加上各表里记着的时间点。按时间排。"""
    out: list[dict[str, Any]] = []

    def add(ts: Any, kind: str, what: str, **detail: Any) -> None:
        when = _when(ts)
        if when:
            out.append(
                {
                    "ts": when,
                    "source": kind,
                    "what": what,
                    **{k: v for k, v in detail.items() if v not in (None, "", [], {})},
                }
            )

    for r in ctx.db.q(
        "SELECT * FROM trace_events WHERE asset_id = ? ORDER BY trace_id", (asset_id,)
    ):
        add(
            r["ts"],
            "trace",
            r["stage"],
            store=r["store_key"],
            code=r["code"],
            set_id=r["set_id"],
            frm=r["from_state"],
            to=r["to_state"],
            actor=r["actor"],
            via=r["source"],
            reason=r["reason"],
            payload=loads(r["payload_json"], None),
        )
    sets = [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM scene_sets WHERE asset_id = ? ORDER BY created_at",
            (asset_id,),
        )
    ]
    scopes = [asset_id, *(s["set_id"] for s in sets)]
    for r in ctx.db.q(
        f"SELECT ts, scope, event, payload_json FROM events WHERE scope IN ({','.join('?' * len(scopes))})"
        " ORDER BY event_id",
        tuple(scopes),
    ):
        add(
            r["ts"],
            "event",
            r["event"],
            scope=r["scope"],
            payload=loads(r["payload_json"], None),
        )
    for r in ctx.db.q(
        "SELECT review_id, subject_type, status, decision, decided_by, decided_at, created_at FROM reviews"
        " WHERE subject_id = ? ORDER BY created_at",
        (asset_id,),
    ):
        add(
            r["created_at"],
            "review",
            f"{r['subject_type']} 开审",
            review_id=r["review_id"],
        )
        if r["decided_at"]:
            add(
                r["decided_at"],
                "review",
                f"{r['subject_type']} {r['decision']}",
                review_id=r["review_id"],
                actor=r["decided_by"],
            )
    for r in ctx.db.q(
        "SELECT call_id, stage, model, status, created_at, finished_at FROM provider_calls WHERE asset_id = ?"
        " ORDER BY created_at",
        (asset_id,),
    ):
        add(
            r["finished_at"] or r["created_at"],
            "call",
            f"{r['stage']} {r['status']}",
            call_id=r["call_id"],
            model=r["model"],
        )
    for s in sets:
        add(
            s["created_at"],
            "scenes",
            "套图入队",
            set_id=s["set_id"],
            actor=s["requested_by"],
        )
        add(
            s["submitted_at"],
            "scenes",
            "交给 mooya",
            set_id=s["set_id"],
            mooya_job_id=s["mooya_job_id"],
        )
        add(
            s["finished_at"],
            "scenes",
            f"套图 {s['status']}",
            set_id=s["set_id"],
            error=s["last_error"],
        )
    for r in ctx.db.q("SELECT * FROM store_listings WHERE asset_id = ?", (asset_id,)):
        for column, what in (
            ("chosen_at", "选了店铺"),
            ("code_at", "领到货号"),
            ("confirmed_at", "确认上架"),
            ("create_submitted_at", "提交建箱"),
            ("boxed_at", "进了采集箱"),
            ("publish_submitted_at", "提交发布"),
            ("listed_at", "已上架（妙手 published）"),
        ):
            add(
                r[column],
                "listing",
                what,
                store=r["store_key"],
                code=r["code"],
                detail_id=r["detail_id"]
                if column in ("boxed_at", "listed_at")
                else None,
            )
    for r in ctx.db.q("SELECT * FROM listing_assets WHERE asset_id = ?", (asset_id,)):
        add(r["prepared_at"], "listing", "上架图准备好", set_id=r["set_id"])
    for r in ctx.db.q(
        "SELECT * FROM listing_title_versions WHERE asset_id = ? ORDER BY version_id",
        (asset_id,),
    ):
        add(
            r["at"],
            "title",
            "标题版本" + ("" if r["check_ok"] else "（不合规）"),
            title=r["title"],
            origin=r["source"],
            actor=r["made_by"],
            via=r["via"],
            problems=loads(r["problems_json"], None),
        )
    for r in ctx.db.q(
        "SELECT * FROM table_cell_log WHERE asset_id = ? ORDER BY log_id", (asset_id,)
    ):
        add(
            r["edited_at"] or r["ts"],
            "cell",
            f"{r['kind']}·{r['field']} {r['outcome']}",
            old=loads(r["old_json"], None),
            new=loads(r["new_json"], None),
            actor=r["editor"],
            note=r["note"],
        )
    seen_codes: set[str] = set()
    for r in ctx.db.q(
        "SELECT code, store_key, listed_at FROM listed_products WHERE asset_id = ?",
        (asset_id,),
    ):
        seen_codes.add(r["code"])
        add(
            r["listed_at"],
            "listed",
            "记进已上架的款",
            code=r["code"],
            store=r["store_key"],
        )
    # 四家店统一底账（temu.listed.source: mooya）：mooya 底账的镜像
    for r in ctx.db.q(
        "SELECT code, store_key, listed_at FROM listed_rows WHERE asset_id = ?",
        (asset_id,),
    ):
        if r["code"] in seen_codes:
            continue
        add(
            r["listed_at"],
            "listed",
            "记进已上架的款（mooya 底账）",
            code=r["code"],
            store=r["store_key"],
        )
    out.sort(key=lambda e: e["ts"])
    return {
        "asset_id": asset_id,
        "pipeline": rows(ctx, asset_id),
        "summary": summary(ctx, asset_id),
        "count": len(out),
        "timeline": out[-limit:] if limit else out,
    }
