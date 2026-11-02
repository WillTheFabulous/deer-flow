"""Temu 上架交给 mooya（2026-10-30，mooya feature-plans/temu-listing-unify）：`temu.engine: mooya` 时本机配的店（3店 / 4店，
2026-11-02 起 1店 / 2店也在，mooya 租户 ai_temu_deerflow 里 key 相同、前缀 FLRP / JUHP）不再直连妙手，
定了店铺、确认上架的（款, 店）交给 mooya 的上架引擎（服务接口 /api/service/v1/temu/*，mooya `services/temu_listing/`），
取图、贴字标、写标题、按样板建箱、写品牌、发布、查发布结果、上架图链接重签都在 mooya 做，这里只做：

- 提交（`submit`）：确认了、领到货号、套图出完（`temu_assets.readiness` 照旧判）的交 `POST /temu/listings`（ref 就是领货号
  用的那个，同一 ref mooya 只建一条）：套图 job、店铺、货号、确认人、暂停没有、素材 / 任务标签；本机已经有标题的一起交
  （模型写的或人改的），mooya 不再写。同一款几家店：先交一家，它的标题写好了再带着标题交其余的店（一款一个标题）。
  mooya 预检没过（409：图位对不上、店铺停用着…）不算失败，原因写在「上架备注」接着等；mooya 里没上成的再确认 = 重试。
- 跟进（`poll`）：交出去的按 mooya 上架单写回 store_listings（排队 / 准备 / 待建箱 → queued，建箱 → creating /
  create_unknown，在箱 → in_box，发布 → publishing / publish_unknown，已上架 → listed，没上成 → failed），妙手 ID、
  各环节时间、等待原因 / 品牌 / 出错原因一起写；mooya 写的标题记进本机（「选中的款」的上架标题照旧）。
  查到已上架走 `temu._listed`（上架状态、下载确认、归档照旧）；底账由 mooya 记，这里拉（listed_table.pull）。
- 轨迹（`events`）：mooya 每一步按 id 增量拉进 trace_events（stage = mooya），`podctl asset --timeline` 看得到。
- 暂停以本机 listing_holds 为准：每轮对一下 mooya 的 held，不一样就 hold / release。取消（还没建箱）转 mooya 的 cancel。
- 改标题（`plan_title` / `process_titles`）：没交的提交时带上；排队 / 待建箱 / 在箱 / 没上成的转 mooya 的 title 动作
  （在箱的 mooya 存回采集箱）；已上架的走 mooya 底账的改标题（mooya 接 Temu 接口，没接上记「要人去 Temu 后台改」）。
- 搬单（`export`）：切换那一刻把本机已经进妙手的（建箱中 … 已上架、没上成但有妙手 ID 的）原样搬进 mooya
  （`/temu/listings/import`，mooya 接着推进）；已上架的连底账（快照、人填的列、时间序列、标题同步）一起搬。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import labels, lease, mooya, stores, temu, temu_assets, temu_title, trace
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso, parse_iso, utcnow

ENGINE_LOCAL = "local"
ENGINE_MOOYA = "mooya"
EVENTS_META_KEY = "mooya_listing.events_after"
POLLED_META_KEY = "mooya_listing.polled_at"
# mooya 上架单状态 → store_listings 的状态
STATE_MAP = {
    "queued": temu.STATE_QUEUED,
    "preparing": temu.STATE_QUEUED,
    "titled": temu.STATE_QUEUED,
    "creating": temu.STATE_CREATING,
    "create_unknown": temu.STATE_UNKNOWN,
    "in_box": temu.STATE_BOX,
    "publishing": temu.STATE_PUBLISHING,
    "publish_unknown": temu.STATE_PUBLISH_UNKNOWN,
    "published": temu.STATE_LISTED,
    "failed": temu.STATE_FAILED,
    "cancelled": temu.STATE_CHOSEN,
}
QUEUE_TEXT = {
    "queued": "mooya 排队中",
    "preparing": "mooya 在准备上架图与标题",
    "titled": "mooya 待建箱",
}
REMOTE_DONE = frozenset({"published", "cancelled"})
# 能转 mooya 改标题动作的状态（建箱中 / 发布中等结果，已上架走底账）
TITLE_ACTION_STATES = frozenset({"queued", "preparing", "titled", "in_box", "failed"})
TITLE_WAIT_STATES = frozenset(
    {"creating", "create_unknown", "publishing", "publish_unknown"}
)
# 从本机搬进 mooya 的状态（chosen / queued 不搬：套图出完时走提交）
EXPORT_STATES = (
    temu.STATE_CREATING,
    temu.STATE_UNKNOWN,
    temu.STATE_BOX,
    temu.STATE_PUBLISHING,
    temu.STATE_PUBLISH_UNKNOWN,
    temu.STATE_LISTED,
    temu.STATE_FAILED,
)
EXPORT_BATCH = 50
LIST_BATCH = 200


def enabled(ctx: Ctx) -> bool:
    """上架引擎在 mooya（temu.engine: mooya）。"""
    return str(ctx.setting("temu.engine") or ENGINE_LOCAL) == ENGINE_MOOYA


def client_name(ctx: Ctx) -> str:
    """交上架单用哪个 mooya 客户端（缺省交套图的那个，绑 ai_temu_deerflow）。"""
    return str(ctx.setting("temu.mooya_client") or mooya.submit_client(ctx))


# ---------------------------------------------------------------- 对照表


def mapping(ctx: Ctx, asset_id: str, store_key: str) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT * FROM mooya_listings WHERE asset_id = ? AND store_key = ?",
        (asset_id, store_key),
    )
    if not row:
        return None
    out = dict(row)
    out["payload"] = loads(out.pop("payload_json"), {}) or {}
    return out


def mappings(ctx: Ctx, asset_ids: list[str] | None = None) -> list[dict[str, Any]]:
    rows = ctx.db.q("SELECT * FROM mooya_listings ORDER BY listing_id")
    out = []
    wanted = set(asset_ids) if asset_ids is not None else None
    for r in rows:
        if wanted is not None and r["asset_id"] not in wanted:
            continue
        item = dict(r)
        item["payload"] = loads(item.pop("payload_json"), {}) or {}
        out.append(item)
    return out


def payload(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    """这一（款, 店）在 mooya 的上架单（最近一次读到的）；没交给 mooya 为空。"""
    found = mapping(ctx, row["asset_id"], row["store_key"])
    return (found or {}).get("payload") or {}


def _save_mapping(
    ctx: Ctx,
    row: dict[str, Any],
    client: str,
    listing: dict[str, Any],
    *,
    submitted: bool = False,
) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO mooya_listings(asset_id, store_key, client, listing_id, ref, state, held, payload_json,"
            " submitted_at, checked_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(asset_id, store_key) DO UPDATE SET client = excluded.client, listing_id = excluded.listing_id,"
            " ref = excluded.ref, state = excluded.state, held = excluded.held, payload_json = excluded.payload_json,"
            " checked_at = excluded.checked_at, updated_at = excluded.updated_at"
            + (", submitted_at = excluded.submitted_at" if submitted else ""),
            (
                row["asset_id"],
                row["store_key"],
                client,
                int(listing["id"]),
                str(
                    listing.get("ref")
                    or stores.code_ref(ctx, row["asset_id"], row["store_key"])
                ),
                listing.get("state"),
                1 if listing.get("held") else 0,
                dumps(listing),
                now,
                now,
                now,
            ),
        )


# ---------------------------------------------------------------- 状态与说明（temu.py 在 mooya 模式下调）


def _local_blocker(ctx: Ctx, row: dict[str, Any]) -> str | None:
    """交 mooya 之前本机要等的：货号、套图（出完、不是旧租户、图位对得上）。"""
    if not row.get("code"):
        if row.get("code_error"):
            return f"货号没领到：{row['code_error']}"[:200]
        return "领货号中"
    return temu_assets.readiness(ctx, row["asset_id"])


def blocker(ctx: Ctx, row: dict[str, Any]) -> str | None:
    """这一（款, 店）现在还不能往下走的原因：货号、套图（本机判）；交出去之后看 mooya 排到哪了。None = 能交 / 在走。"""
    reason = _local_blocker(ctx, row)
    if reason:
        return reason
    found = payload(ctx, row)
    state = found.get("state")
    if state in QUEUE_TEXT:
        extra = found.get("lastError") if state != "failed" else None
        return QUEUE_TEXT[state] + (f"（{extra}）" if extra else "")
    if not found and row["state"] == temu.STATE_QUEUED and row.get("last_error"):
        return row["last_error"][:200]
    return None


def brand_note(ctx: Ctx, row: dict[str, Any]) -> str | None:
    return payload(ctx, row).get("brandNote") or None


def wait_reason(ctx: Ctx, row: dict[str, Any]) -> str | None:
    found = payload(ctx, row)
    if found.get("held"):
        return "已暂停（人取消了「确认上架」），再勾「确认上架」接着发布"
    return found.get("waitReason") or None


# ---------------------------------------------------------------- 提交


def _labels(ctx: Ctx, row: dict[str, Any], set_id: str | None) -> dict[str, Any]:
    return {
        "asset": labels.asset(ctx, row["asset_id"]),
        "task": labels.task(ctx, row["seed_id"]) or row["seed_id"],
        "asset_id": row["asset_id"],
        "seed_id": row["seed_id"],
        "set_id": set_id,
    }


def _title_fields(ctx: Ctx, asset_id: str) -> dict[str, Any]:
    title = temu_title.get(ctx, asset_id)
    if not title or not title.get("title"):
        return {}
    meta = {
        k: v
        for k, v in (title.get("meta") or {}).items()
        if k in ("is_custom", "lead_type", "holiday", "gift_led", "theme")
    }
    return {
        "title": title["title"],
        "title_meta": meta,
        "title_source": "human"
        if title.get("source") == temu_title.SOURCE_HUMAN
        else "model",
    }


def _body(ctx: Ctx, row: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    body = {
        "job_id": int(current["mooya_job_id"]),
        "store": row["store_key"],
        "code": row["code"],
        "ref": stores.code_ref(ctx, row["asset_id"], row["store_key"]),
        "held": temu.held(ctx, row["asset_id"]),
        "labels": _labels(ctx, row, current.get("set_id")),
        "actor": row.get("confirmed_by") or temu.BY_AUTO,
    }
    body.update(_title_fields(ctx, row["asset_id"]))
    return body


def _sibling_pending(ctx: Ctx, row: dict[str, Any]) -> bool:
    """同一款别的店已经交了、mooya 还没写好标题（本机也没有）：等它（一款一个标题）。"""
    if temu_title.get(ctx, row["asset_id"]):
        return False
    for other in mappings(ctx, [row["asset_id"]]):
        if other["store_key"] != row["store_key"] and other["state"] not in (
            "cancelled",
            "failed",
        ):
            return True
    return False


def submit(ctx: Ctx, api: mooya.Client, out: dict[str, Any], touched: set[str]) -> None:
    """确认了的交 mooya；交过的、mooya 里没上成又被重新确认的转 mooya 的 retry。"""
    queued = temu._rows(ctx, (temu.STATE_QUEUED,))
    if any(not r["code"] for r in queued):
        stores.allocate_codes(
            ctx, asset_ids=[r["asset_id"] for r in queued if not r["code"]]
        )
        queued = temu._rows(ctx, (temu.STATE_QUEUED,))
    for row in queued:
        lease.take(ctx, temu.LEASE_KEY, temu.LEASE_SECONDS)
        asset_id = row["asset_id"]
        found = mapping(ctx, asset_id, row["store_key"])
        remote = (found or {}).get("state")
        if found and remote == "failed":
            _retry(ctx, api, row, found, out, touched)
            continue
        if found and remote != "cancelled":
            continue
        reason = _local_blocker(ctx, row)
        if reason:
            out["waiting"].append(
                {"asset_id": asset_id, "store": row["store_key"], "reason": reason}
            )
            continue
        if _sibling_pending(ctx, row):
            out["waiting"].append(
                {
                    "asset_id": asset_id,
                    "store": row["store_key"],
                    "reason": "等同一款另一家店的标题",
                }
            )
            continue
        current = temu_assets.latest_ok_set(ctx, asset_id)
        if not current or not current.get("mooya_job_id"):
            out["waiting"].append(
                {
                    "asset_id": asset_id,
                    "store": row["store_key"],
                    "reason": "还没出套图",
                }
            )
            continue
        try:
            result = api.temu_create_listing(_body(ctx, row, current))
        except mooya.MooyaError as exc:
            if exc.status == 409:
                note = "；".join(exc.problems[:4]) or str(exc)
                temu._update(
                    ctx,
                    row,
                    last_error=f"mooya 还不能上：{note}"[:500],
                    error_kind=None,
                )
                out["waiting"].append(
                    {"asset_id": asset_id, "store": row["store_key"], "reason": note}
                )
                touched.add(asset_id)
                continue
            if exc.status in (400, 404):
                temu._fail(ctx, row, f"mooya 不收这一单：{exc}")
                out["failed"].append(asset_id)
                touched.add(asset_id)
                continue
            raise
        listing = result["listing"]
        _save_mapping(ctx, row, api.name, listing, submitted=True)
        trace.log(
            ctx,
            asset_id,
            "listing",
            to="submitted",
            store=row["store_key"],
            code=row["code"],
            actor=row.get("confirmed_by"),
            source="mooya",
            payload={"listing": listing["id"], "created": bool(result.get("created"))},
        )
        out.setdefault("submitted", []).append(row["code"])
        _apply(ctx, row, listing, out, touched)


def _retry(
    ctx: Ctx,
    api: mooya.Client,
    row: dict[str, Any],
    found: dict[str, Any],
    out: dict[str, Any],
    touched: set[str],
) -> None:
    try:
        listing = api.temu_action(
            int(found["listing_id"]),
            "retry",
            actor=row.get("confirmed_by") or temu.BY_AUTO,
        )
    except mooya.MooyaError as exc:
        if exc.status != 409:
            raise
        out["errors"].append(f"{row['code']} mooya 重试没成：{exc}"[:300])
        return
    _save_mapping(ctx, row, api.name, listing)
    out.setdefault("retried", []).append(row["code"])
    _apply(ctx, row, listing, out, touched)


# ---------------------------------------------------------------- 跟进


def _times(listing: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, column in (
        ("createSubmittedAt", "create_submitted_at"),
        ("boxedAt", "boxed_at"),
        ("publishSubmittedAt", "publish_submitted_at"),
    ):
        if listing.get(key):
            out[column] = listing[key]
    return out


def _note(listing: dict[str, Any]) -> str | None:
    state = listing.get("state")
    if state in QUEUE_TEXT:
        return listing.get("lastError") or None
    if state == "failed":
        return listing.get("lastError") or "mooya 里没上成"
    return (
        listing.get("waitReason")
        or listing.get("brandNote")
        or listing.get("lastError")
        or None
    )


def _mirror_title(ctx: Ctx, asset_id: str, listing: dict[str, Any]) -> bool:
    """mooya 模型写的标题记进本机（「选中的款」照旧显示）；本机已经有人改过的不盖。"""
    text = (listing.get("title") or "").strip()
    if not text:
        return False
    local = temu_title.get(ctx, asset_id) or {}
    if (local.get("title") or "").strip() == text:
        return False
    if local and local.get("source") == temu_title.SOURCE_HUMAN:
        return False
    meta = {**(local.get("meta") or {}), **(listing.get("titleMeta") or {})}
    temu_title._save(
        ctx,
        asset_id,
        text,
        source=temu_title.SOURCE_AUTO,
        meta=meta,
        model="mooya",
        via="mooya",
    )
    return True


def _apply(
    ctx: Ctx,
    row: dict[str, Any],
    listing: dict[str, Any],
    out: dict[str, Any],
    touched: set[str],
) -> None:
    """mooya 上架单 → 本机 store_listings（状态、妙手 ID、时间、原因）；变成已上架的走 temu._listed。"""
    asset_id = row["asset_id"]
    state = STATE_MAP.get(str(listing.get("state") or ""), row["state"])
    if listing.get("state") == "cancelled" and row["state"] != temu.STATE_QUEUED:
        state = row["state"]
    values: dict[str, Any] = {**_times(listing)}
    for key, column in (("detailId", "detail_id"), ("shopId", "shop_id")):
        if listing.get(key) and listing.get(key) != row.get(column):
            values[column] = str(listing[key])
    if listing.get("title") and listing["title"] != row.get("title"):
        values["title"] = listing["title"]
    note = _note(listing)
    if state == temu.STATE_FAILED:
        values["error_kind"] = listing.get("errorKind") or "mooya"
    elif row.get("error_kind") and state != row["state"]:
        values["error_kind"] = None
    if (note or None) != (row.get("last_error") or None):
        values["last_error"] = (note or None) and note[:500]
    titled = _mirror_title(ctx, asset_id, listing)
    _settle_titles(ctx, row, listing)
    if state == temu.STATE_LISTED and row["state"] != temu.STATE_LISTED:
        if values:
            temu._update(ctx, row, **values)
        temu._listed(ctx, row, listed_at=listing.get("publishedAt"))
        out["listed"].append(row["code"])
        touched.add(asset_id)
        return
    if state != row["state"]:
        values["state"] = state
        if state == temu.STATE_FAILED:
            values["attempts"] = int(row.get("attempts") or 0) + 1
            out["failed"].append(asset_id)
        elif state == temu.STATE_BOX:
            out["boxed"].append(row["code"])
        elif state == temu.STATE_PUBLISHING:
            out["published"].append(row["code"])
    if values:
        temu._update(
            ctx, row, _actor=listing.get("actor") or None, _source="mooya", **values
        )
        touched.add(asset_id)
    elif titled:
        touched.add(asset_id)


def _watched(ctx: Ctx, found: dict[str, Any], row: dict[str, Any] | None) -> bool:
    """还要问 mooya 的：没走完的、已上架但本机还没记上的、没上成在 7 天里的（人在妙手修好重发也能认出来）。"""
    if row is None:
        return False
    state = found.get("state")
    if state == "cancelled":
        return False
    if state == "published":
        return row["state"] != temu.STATE_LISTED
    if state == "failed":
        at = parse_iso(found.get("updated_at"))
        days = float(ctx.setting("temu.watch_days", 7))
        return (
            bool(at and utcnow() - at < dt.timedelta(days=days))
            or row["state"] != temu.STATE_FAILED
        )
    return True


def poll(ctx: Ctx, api: mooya.Client, out: dict[str, Any], touched: set[str]) -> None:
    rows = {(r["asset_id"], r["store_key"]): r for r in stores.rows(ctx, active=False)}
    targets = [
        m
        for m in mappings(ctx)
        if _watched(ctx, m, rows.get((m["asset_id"], m["store_key"])))
    ]
    by_id = {int(m["listing_id"]): m for m in targets}
    ids = sorted(by_id)
    for i in range(0, len(ids), LIST_BATCH):
        for listing in api.temu_listings(ids=ids[i : i + LIST_BATCH]):
            found = by_id.get(int(listing["id"]))
            row = rows.get((found["asset_id"], found["store_key"])) if found else None
            if row is None:
                continue
            if listing.get("updatedAt") != found["payload"].get(
                "updatedAt"
            ) or listing.get("state") != found.get("state"):
                _save_mapping(ctx, row, found["client"], listing)
            else:
                with ctx.db.tx() as conn:
                    conn.execute(
                        "UPDATE mooya_listings SET checked_at = ? WHERE asset_id = ? AND store_key = ?",
                        (now_iso(), found["asset_id"], found["store_key"]),
                    )
            _apply(ctx, row, listing, out, touched)
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, POLLED_META_KEY, now_iso())


def poll_due(ctx: Ctx) -> bool:
    last = parse_iso(ctx.db.get_meta(POLLED_META_KEY))
    minutes = float(ctx.setting("temu.poll_minutes", 5))
    if last is None or utcnow() - last >= dt.timedelta(minutes=minutes, seconds=-15):
        return True
    # 刚交的、在途的看得勤一点（mooya 后台 20 秒一轮）
    return any(
        m["state"] not in REMOTE_DONE and m["state"] != "failed" for m in mappings(ctx)
    ) and (utcnow() - last >= dt.timedelta(seconds=55))


def events(ctx: Ctx, api: mooya.Client) -> int:
    """mooya 的轨迹按 id 增量拉进 trace_events（stage = mooya）。返回这次记了几条。"""
    after = int(ctx.db.get_meta(EVENTS_META_KEY) or 0)
    by_listing = {int(m["listing_id"]): m for m in mappings(ctx)}
    count = 0
    while True:
        data = api.temu_events(after, 500)
        items = data.get("events") or []
        with ctx.db.tx() as conn:
            for event in items:
                found = by_listing.get(int(event.get("listingId") or 0))
                if found:
                    trace.record(
                        conn,
                        found["asset_id"],
                        "mooya",
                        frm=event.get("from"),
                        to=event.get("to"),
                        store=found["store_key"],
                        code=event.get("code"),
                        actor=event.get("actor"),
                        source=f"mooya:{event.get('source') or ''}"[:40],
                        reason=event.get("reason"),
                        payload={
                            "stage": event.get("stage"),
                            "at": event.get("at"),
                            "event": event.get("id"),
                        },
                    )
                    count += 1
            after = int(data.get("last") or after)
            ctx.db.set_meta(conn, EVENTS_META_KEY, str(after))
        if len(items) < 500:
            break
    return count


def reconcile_holds(
    ctx: Ctx, api: mooya.Client, out: dict[str, Any], touched: set[str]
) -> None:
    """暂停以本机为准（人取消 / 再勾「确认上架」）：和 mooya 的 held 不一样就 hold / release。"""
    for found in mappings(ctx):
        state = found.get("state")
        if state in REMOTE_DONE:
            continue
        local = temu.held(ctx, found["asset_id"])
        remote = bool(found["payload"].get("held"))
        actions = found["payload"].get("actions") or {}
        if local == remote:
            continue
        action = "hold" if local else "release"
        if not actions.get(action):
            continue
        hold_row = ctx.db.q1(
            "SELECT held_by FROM listing_holds WHERE asset_id = ?", (found["asset_id"],)
        )
        try:
            listing = api.temu_action(
                int(found["listing_id"]),
                action,
                actor=(hold_row["held_by"] if hold_row else None) or temu.BY_AUTO,
                reason="「选中的款」取消了「确认上架」"
                if local
                else "「选中的款」又勾上了「确认上架」",
            )
        except mooya.MooyaError as exc:
            if exc.status != 409:
                raise
            continue
        row = next(
            (
                r
                for r in stores.rows(ctx, asset_ids=[found["asset_id"]])
                if r["store_key"] == found["store_key"]
            ),
            None,
        )
        if row is not None:
            _save_mapping(ctx, row, found["client"], listing)
            _apply(ctx, row, listing, out, touched)
        out.setdefault("holds", []).append(
            {"code": listing.get("code"), "action": action}
        )


def cancel(ctx: Ctx, row: dict[str, Any], *, by: str) -> bool:
    """取消确认（还没进妙手的）：交给了 mooya 的先在 mooya 取消。取消不了（已经建箱）返回 False。"""
    found = mapping(ctx, row["asset_id"], row["store_key"])
    if not found or found.get("state") in ("cancelled",):
        return True
    if not (found["payload"].get("actions") or {}).get("cancel"):
        return False
    try:
        with mooya.Client(ctx, client=found["client"]) as api:
            listing = api.temu_action(
                int(found["listing_id"]), "cancel", actor=by, reason="取消了确认上架"
            )
    except mooya.MooyaError:
        return False
    _save_mapping(ctx, row, found["client"], listing)
    return True


# ---------------------------------------------------------------- 改标题（title_sync 在 mooya 模式下调）


def plan_title(ctx: Ctx, row: dict[str, Any]) -> tuple[str, str | None]:
    """一（款, 店）收到一个新标题：(title_sync 状态, 目标)。目标 mooya = 转 mooya 的 title 动作；listed = mooya 底账改标题。"""
    from . import title_sync  # 标题同步依赖本模块，用时再导入

    found = mapping(ctx, row["asset_id"], row["store_key"])
    if not found or found.get("state") == "cancelled":
        return title_sync.STATE_DONE, None
    state = found.get("state")
    if state == "published":
        return title_sync.STATE_PENDING, "listed"
    return title_sync.STATE_PENDING, "mooya"


def process_titles(
    ctx: Ctx, api: mooya.Client, out: dict[str, Any], touched: set[str]
) -> None:
    """把要同步的标题交给 mooya：建箱中 / 发布中的等结果；在箱的 mooya 存回采集箱，读回 titleDirty 清了算生效。"""
    from . import title_sync  # 标题同步依赖本模块，用时再导入

    for item in title_sync.status(ctx):
        if item["state"] != title_sync.STATE_PENDING or item.get("target") not in (
            "mooya",
            "listed",
        ):
            continue
        found = mapping(ctx, item["asset_id"], item["store_key"])
        if not found or found.get("state") == "cancelled":
            title_sync.mark(
                ctx,
                item["asset_id"],
                item["store_key"],
                title_sync.STATE_DONE,
                last_error=None,
            )
            touched.add(item["asset_id"])
            continue
        state = found.get("state")
        if state in TITLE_WAIT_STATES:
            continue
        if item.get("submitted_at") and item.get("target") == "mooya":
            continue
        try:
            if state == "published":
                result = api.temu_listed_title(
                    found["payload"].get("code") or "",
                    item["title"],
                    editor=item.get("made_by") or "deerflow",
                    via="deerflow",
                    store=item["store_key"],
                )
                sync = (result.get("item") or {}).get("titleSync") or {}
                new = {
                    "waiting_temu": title_sync.STATE_WAITING_TEMU,
                    "temu_submitted": title_sync.STATE_TEMU_SUBMITTED,
                    "done": title_sync.STATE_DONE,
                }.get(
                    sync.get("state") or result.get("state"), title_sync.STATE_PENDING
                )
                title_sync.mark(
                    ctx,
                    item["asset_id"],
                    item["store_key"],
                    new,
                    target=title_sync.TARGET_TEMU,
                    last_error=sync.get("error"),
                    submitted_at=now_iso(),
                )
            elif state in TITLE_ACTION_STATES:
                listing = api.temu_action(
                    int(found["listing_id"]),
                    "title",
                    title=item["title"],
                    actor="deerflow",
                )
                row = next(
                    (
                        r
                        for r in stores.rows(ctx, asset_ids=[item["asset_id"]])
                        if r["store_key"] == item["store_key"]
                    ),
                    None,
                )
                if row is not None:
                    _save_mapping(ctx, row, found["client"], listing)
                if state == "in_box" and listing.get("titleDirty"):
                    title_sync.mark(
                        ctx,
                        item["asset_id"],
                        item["store_key"],
                        title_sync.STATE_PENDING,
                        target=title_sync.TARGET_BOX,
                        submitted_at=now_iso(),
                        last_error=None,
                    )
                else:
                    title_sync.mark(
                        ctx,
                        item["asset_id"],
                        item["store_key"],
                        title_sync.STATE_DONE,
                        target=title_sync.TARGET_BOX if state == "in_box" else None,
                        last_error=None,
                    )
        except mooya.MooyaError as exc:
            if exc.status not in (400, 409):
                raise
            problems = exc.problems
            title_sync.mark(
                ctx,
                item["asset_id"],
                item["store_key"],
                title_sync.STATE_INVALID
                if any("不合规" in p or "GSM" in p for p in problems)
                else title_sync.STATE_FAILED,
                last_error=("；".join(problems[:4]) or str(exc))[:300],
            )
        out.setdefault("titles_saved", []).append(item["asset_id"])
        touched.add(item["asset_id"])


def _settle_titles(ctx: Ctx, row: dict[str, Any], listing: dict[str, Any]) -> None:
    """在箱的标题交给 mooya 存回之后：mooya 读回 titleDirty 清了、标题对上了就算生效。"""
    from . import title_sync  # 标题同步依赖本模块，用时再导入

    for item in title_sync.status(ctx, row["asset_id"]):
        if (
            item["store_key"] != row["store_key"]
            or item["state"] != title_sync.STATE_PENDING
        ):
            continue
        if item.get("target") != title_sync.TARGET_BOX or not item.get("submitted_at"):
            continue
        if (
            not listing.get("titleDirty")
            and (listing.get("title") or "").strip() == item["title"].strip()
        ):
            title_sync.mark(
                ctx,
                row["asset_id"],
                row["store_key"],
                title_sync.STATE_DONE,
                last_error=None,
            )


# ---------------------------------------------------------------- 一轮


def has_work(ctx: Ctx) -> bool:
    from . import title_sync  # 标题同步依赖本模块，用时再导入

    if temu._auto_due(ctx):
        return True
    known = {(m["asset_id"], m["store_key"]): m for m in mappings(ctx)}
    for row in temu._rows(ctx, (temu.STATE_QUEUED,)):
        found = known.get((row["asset_id"], row["store_key"]))
        if found is None or found.get("state") in ("cancelled", "failed"):
            return True
    for found in known.values():
        if (
            temu.held(ctx, found["asset_id"]) != bool(found["payload"].get("held"))
            and found.get("state") not in REMOTE_DONE
        ):
            return True
    if any(
        i["state"] == title_sync.STATE_PENDING
        and i.get("target") in ("mooya", "listed")
        and not i.get("submitted_at")
        for i in title_sync.status(ctx)
    ):
        return True
    if not poll_due(ctx):
        return False
    rows = {(r["asset_id"], r["store_key"]): r for r in stores.rows(ctx, active=False)}
    return any(_watched(ctx, m, rows.get(key)) for key, m in known.items())


def sync(ctx: Ctx) -> dict[str, Any]:
    """mooya 模式的一轮：程序确认 → 交 mooya / 重试 → 对暂停 → 跟进状态 → 交标题 → 拉轨迹，最后写表。"""
    from . import listed_table  # 已上架的款依赖本模块，放到用时再导入

    if not lease.take(ctx, temu.LEASE_KEY, temu.LEASE_SECONDS):
        return {"skipped": True, "reason": "另一个上架进程正在运行"}
    out: dict[str, Any] = {
        "engine": ENGINE_MOOYA,
        "prepared": [],
        "boxed": [],
        "published": [],
        "listed": [],
        "failed": [],
        "waiting": [],
        "errors": [],
    }
    touched: set[str] = set()
    try:
        confirmed = temu.auto_confirm(ctx)
        if confirmed.get("queued"):
            out["auto_confirmed"] = confirmed["queued"]
            touched |= set(confirmed["queued"])
        try:
            with mooya.Client(ctx, client=client_name(ctx)) as api:
                submit(ctx, api, out, touched)
                reconcile_holds(ctx, api, out, touched)
                poll(ctx, api, out, touched)
                process_titles(ctx, api, out, touched)
                recorded = events(ctx, api)
                if recorded:
                    out["events"] = recorded
            temu._clear_service_error(ctx)
        except mooya.MooyaError as exc:
            if exc.service:
                temu._record_service_error(ctx, exc)
            out["stopped"] = f"mooya 暂时不通：{exc}"[:300]
    finally:
        lease.drop(ctx, temu.LEASE_KEY)
    if touched or out["waiting"]:
        out["table"] = temu.write_now(
            ctx, sorted(touched | {w["asset_id"] for w in out["waiting"]})
        )
    if out["listed"]:
        out["listed_table"] = (
            listed_table.pull_now(ctx)
            if listed_table.source_mooya(ctx)
            else listed_table.sync_now(ctx)
        )
        shelved = temu._sweep_listed(ctx, out["listed"])
        if shelved:
            out["shelf"] = shelved
        from . import feishu_table  # 表格模块依赖本模块之外的很多表，用时再导入

        feishu_table.kick(ctx)
    if out["failed"]:
        out["alerted"] = temu.notify_failed(ctx, out["failed"])
    return out


# ---------------------------------------------------------------- 搬单


def _listing_asset(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    if row.get("set_id"):
        found = temu_assets.get(ctx, row["asset_id"], row["set_id"])
        if found:
            return found
    return temu_assets.current(ctx, row["asset_id"]) or {}


def _brand(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    found = ctx.db.q1(
        "SELECT * FROM temu_brands WHERE asset_id = ? AND store_key = ?",
        (row["asset_id"], row["store_key"]),
    )
    if not found or str(found["detail_id"]) != str(row.get("detail_id") or ""):
        return {}
    return {
        "done_at": found["done_at"],
        "saved": bool(found["saved"]),
        "attempts": int(found["attempts"] or 0),
        "last_error": found["last_error"],
    }


def _listed_block(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any] | None:
    product = ctx.db.q1("SELECT * FROM listed_products WHERE code = ?", (row["code"],))
    if not product:
        return None
    sync = ctx.db.q1(
        "SELECT * FROM title_sync WHERE asset_id = ? AND store_key = ?",
        (row["asset_id"], row["store_key"]),
    )
    human = {
        k: product[k]
        for k in (
            "temu_status",
            "temu_product_id",
            "product_url",
            "sales_total",
            "sales_7d",
            "revenue_total",
            "data_updated_on",
            "note",
        )
    }
    return {
        "title": product["title"],
        "detail_id": product["detail_id"],
        "listed_at": product["listed_at"],
        "snapshot": loads(product["snapshot_json"], {}) or {},
        "human": human,
        "metrics": [
            {
                "observed_on": m["observed_on"],
                "temu_status": m["temu_status"],
                "sales_total": m["sales_total"],
                "sales_7d": m["sales_7d"],
                "revenue_total": m["revenue_total"],
                "extra": loads(m["extra_json"], None),
                "source": m["source"],
                "read_at": m["read_at"],
            }
            for m in ctx.db.q(
                "SELECT * FROM listed_metrics WHERE code = ? ORDER BY metric_id",
                (row["code"],),
            )
        ],
        "title_sync": dict(sync) if sync else {},
        "human_read_at": product["human_read_at"],
    }


def export_item(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    """一（款, 店）搬进 mooya 的那一条（mooya service.import_listing 的入参）。"""
    asset = _listing_asset(ctx, row)
    title = temu_title.get(ctx, row["asset_id"]) or {}
    set_id = row.get("set_id") or asset.get("set_id")
    scene = (
        ctx.db.q1("SELECT mooya_job_id FROM scene_sets WHERE set_id = ?", (set_id,))
        if set_id
        else None
    )
    item: dict[str, Any] = {
        "ref": stores.code_ref(ctx, row["asset_id"], row["store_key"]),
        "store": row["store_key"],
        "code": row["code"],
        "state": row["state"],
        "job_id": scene["mooya_job_id"] if scene and scene["mooya_job_id"] else None,
        "package_name": row["code"],
        "detail_id": row.get("detail_id"),
        "shop_id": row.get("shop_id"),
        "template_detail_id": row.get("template_detail_id"),
        "title": row.get("title") or title.get("title"),
        "title_source": title.get("source"),
        "title_meta": {
            k: v
            for k, v in (title.get("meta") or {}).items()
            if k in ("is_custom", "lead_type", "holiday", "gift_led", "theme")
        },
        "images": [
            {
                k: f.get(k)
                for k in (
                    "n",
                    "image_id",
                    "plan_id",
                    "role",
                    "title",
                    "key",
                    "sha256",
                    "url",
                )
            }
            for f in asset.get("files") or []
        ],
        "links_expire_at": asset.get("links_expire_at"),
        "confirmed_at": row.get("confirmed_at"),
        "create_submitted_at": row.get("create_submitted_at"),
        "boxed_at": row.get("boxed_at"),
        "publish_submitted_at": row.get("publish_submitted_at"),
        "listed_at": row.get("listed_at"),
        "brand": _brand(ctx, row),
        "held": temu.held(ctx, row["asset_id"]),
        "labels": _labels(ctx, row, set_id),
        "actor": row.get("confirmed_by"),
        "attempts": int(row.get("attempts") or 0),
        "last_error": row.get("last_error"),
        "error_kind": row.get("error_kind"),
        "title_versions": [
            {
                "title": v["title"],
                "source": v["source"],
                "made_by": v.get("made_by"),
                "via": v.get("via"),
                "problems": v.get("problems") or None,
                "at": v.get("at"),
            }
            for v in temu_title.versions(ctx, row["asset_id"])
        ],
    }
    if row["state"] == temu.STATE_LISTED:
        block = _listed_block(ctx, row)
        if block:
            item["listed"] = block
    return item


def export(
    ctx: Ctx,
    *,
    codes: list[str] | None = None,
    listed_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把本机进了妙手的（款, 店）搬进 mooya（同一 ref 不重复建）：listed_only = 只搬已上架的（底账推送用）。
    dry_run 只列出要搬的。返回 {pushed, errors, skipped}。"""
    rows = [
        r
        for r in stores.rows(ctx, active=False)
        if r.get("code")
        and r["state"] in EXPORT_STATES
        and (r["state"] != temu.STATE_FAILED or r.get("detail_id"))
        and (not listed_only or r["state"] == temu.STATE_LISTED)
        and (codes is None or r["code"] in set(codes))
    ]
    known = {(m["asset_id"], m["store_key"]) for m in mappings(ctx)}
    todo = [
        r for r in rows if listed_only or (r["asset_id"], r["store_key"]) not in known
    ]
    out: dict[str, Any] = {
        "pushed": [],
        "errors": [],
        "skipped": [r["code"] for r in rows if r not in todo],
    }
    if dry_run:
        return {
            **out,
            "would_push": [{"code": r["code"], "state": r["state"]} for r in todo],
        }
    if not todo:
        return out
    items = {r["code"]: (r, export_item(ctx, r)) for r in todo}
    codes_left = list(items)
    with mooya.Client(ctx, client=client_name(ctx)) as api:
        for i in range(0, len(codes_left), EXPORT_BATCH):
            chunk = codes_left[i : i + EXPORT_BATCH]
            results = api.temu_import([items[c][1] for c in chunk])
            for code, result in zip(chunk, results):
                row = items[code][0]
                if not result.get("ok"):
                    out["errors"].append(
                        {"code": code, "error": str(result.get("error") or "")[:300]}
                    )
                    continue
                listing = {
                    "id": result["listingId"],
                    "ref": items[code][1]["ref"],
                    "state": result.get("state"),
                    "code": code,
                    "held": items[code][1]["held"],
                }
                if (row["asset_id"], row["store_key"]) not in known:
                    _save_mapping(ctx, row, api.name, listing, submitted=True)
                out["pushed"].append(code)
    if out["pushed"]:
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "temu",
                "mooya_export",
                {"pushed": out["pushed"][:50], "errors": len(out["errors"])},
            )
    return out


def status(ctx: Ctx) -> dict[str, Any]:
    by_state: dict[str, int] = {}
    for found in mappings(ctx):
        by_state[str(found.get("state"))] = by_state.get(str(found.get("state")), 0) + 1
    return {
        "engine": ENGINE_MOOYA if enabled(ctx) else ENGINE_LOCAL,
        "client": client_name(ctx) if enabled(ctx) else None,
        "listings": by_state,
        "polled_at": ctx.db.get_meta(POLLED_META_KEY),
        "events_after": ctx.db.get_meta(EVENTS_META_KEY),
    }


def doctor_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """doctor 里的一组：mooya 模式时 mooya 的店铺开没开、只走服务接口；没开 mooya 模式时一项 ok。"""
    if not enabled(ctx):
        return [
            {
                "check": "Temu 上架引擎",
                "ok": True,
                "level": "ok",
                "detail": "本机直连妙手（temu.engine: local）",
            }
        ]
    problems = []
    try:
        with mooya.Client(ctx, client=client_name(ctx)) as api:
            found = {s["key"]: s for s in api.temu_stores().get("stores") or []}
    except (mooya.MooyaError, StageError) as exc:
        return [
            {
                "check": "Temu 上架引擎（mooya）",
                "ok": False,
                "level": "error",
                "detail": f"mooya 连不上：{exc}"[:300],
            }
        ]
    for store_ in stores.all_stores(ctx):
        spec = found.get(store_.key)
        if spec is None:
            problems.append(f"mooya 里没有 {store_.column}（{store_.key}）")
            continue
        if not spec.get("enabled"):
            problems.append(f"{store_.column} 在 mooya 里停用着（切换完要打开）")
        if spec.get("prefix") != store_.prefix:
            problems.append(
                f"{store_.column} 的货号前缀两边不一样（本机 {store_.prefix} / mooya {spec.get('prefix')}）"
            )
        # 「已上架的款」的店铺列写的是 mooya 底账里的列名，和本机的选项、视图要一字不差
        if spec.get("column") and spec["column"] != store_.column:
            problems.append(f"{store_.key} 的店名两边不一样（本机「{store_.column}」/ mooya「{spec['column']}」）")
        if spec.get("ui", True):
            problems.append(
                f"{store_.column} 在 mooya 交付工作台也能点上架（rules_json.ui 要关）"
            )
    return [
        {
            "check": "Temu 上架引擎（mooya）",
            "ok": not problems,
            "level": "warn" if problems else "ok",
            "detail": "；".join(problems)
            or f"交 mooya 上架（客户端 {client_name(ctx)}），店铺配置两边一致",
        }
    ]
