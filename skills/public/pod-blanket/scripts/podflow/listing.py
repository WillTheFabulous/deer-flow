"""上架状态：选中的款在「选中的款」表里由人标「上架状态」（待上架 / 已上架 / 不上了），程序只读，
用来回答「这个款上过没有」，任务进度里也多一步「上架 已上架/选中」。

- 三列（feishu_table.LISTING_FIELDS）：上架状态（单选）、上架日期、上架备注。开着 listing.enabled 时新表建表时就有，
  旧表在表格同步时补上，旧行一次性补「待上架」（只改空的格子）。新行由程序写「待上架」，之后程序不再写这一列。
- 回读：巡检拉起的 `podctl table poll`（和候选表控制行兜底一起）每 listing.read_minutes 分钟分页读一次这三列，
  经 table_rows 对到素材，写进 listing_marks；状态变了记事件 listing_changed。没读到的（表里删了、还没进表）按待上架算。
- `podctl listing set` 由人（或 POD 对话）改：等表格同步的租约，写飞书那一格，再写业务库。
- 不决定归档：任务归档只看选款和套图（shelf.py）。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import feishu, feishu_table, lease, store, table_registry, trace
from .context import Ctx
from .db import log_event
from .stages.common import StageError, task_label
from .stages.plan import asset_label_of
from .util import dumps, now_iso, parse_iso, utcnow

STATE_PENDING = feishu_table.LISTING_PENDING
STATE_LISTED = feishu_table.LISTING_LISTED
STATE_SKIPPED = feishu_table.LISTING_SKIPPED
STATES = feishu_table.LISTING_STATES
READABLE = (*feishu_table.LISTING_STATES, *feishu_table.LISTING_AUTO_STATES)
READ_META_KEY = "listing.read_at"
FIELD_NAMES = (
    feishu_table.LISTING_STATE_FIELD,
    feishu_table.LISTING_DATE_FIELD,
    feishu_table.LISTING_NOTE_FIELD,
)
NOTE_CHARS = 200
_PAGE = 500
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)
# 选中、在「选中的款」里的款（和选款表同一个口径）
_APPROVED_SQL = (
    "SELECT a.asset_id, f.seed_id, t.record_id, m.state, m.listed_on, m.note, m.first_listed_at, m.read_at"
    " FROM assets a JOIN variants v ON v.variant_id = a.variant_id"
    " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
    " LEFT JOIN table_rows t ON t.asset_id = a.asset_id"
    " LEFT JOIN listing_marks m ON m.asset_id = a.asset_id"
    " WHERE a.human_decision = 'approved'"
)


def enabled(ctx: Ctx) -> bool:
    return feishu_table.listing_enabled(ctx)


def ready(ctx: Ctx) -> bool:
    """开着上架状态，而且「选中的款」已经有那一列。"""
    return enabled(ctx) and feishu_table.listing_ready(feishu_table.base(ctx))


def due(ctx: Ctx) -> bool:
    """到了该回读的时候（listing.read_minutes，缺省 10 分钟，留 30 秒余量）。"""
    if not ready(ctx):
        return False
    last = ctx.db.get_meta(READ_META_KEY)
    minutes = float(ctx.setting("listing.read_minutes", 10))
    return not last or utcnow() - parse_iso(last) >= dt.timedelta(
        minutes=minutes, seconds=-30
    )


def _tz(ctx: Ctx) -> ZoneInfo:
    return ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))


def _text(cell: Any) -> str | None:
    """文本 / 单选格子的值：飞书返回字符串，或 [{"text": …}] 分段，或 {"text": …}。"""
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell.strip() or None
    if isinstance(cell, dict):
        return _text(cell.get("text") or cell.get("name"))
    if isinstance(cell, list):
        joined = "".join(_text(part) or "" for part in cell)
        return joined.strip() or None
    return str(cell).strip() or None


def _day(ctx: Ctx, cell: Any) -> str | None:
    """日期格子（毫秒时间戳）→ 当地日期 2026-10-25。"""
    if cell in (None, "", []):
        return None
    try:
        ms = int(cell)
    except (TypeError, ValueError):
        return None
    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)
        .astimezone(_tz(ctx))
        .strftime("%Y-%m-%d")
    )


def read_cells(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any]
) -> dict[str, dict[str, str | None]]:
    """分页读「选中的款」的三列：{record_id: {state, listed_on, note}}；没填的上架状态按待上架。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    out: dict[str, dict[str, str | None]] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "page_size": _PAGE,
            "field_names": dumps(list(FIELD_NAMES)),
        }
        if token:
            params["page_token"] = token
        data = api.call("GET", path, params=params)
        for item in data.get("items") or []:
            fields = item.get("fields") or {}
            state = _text(fields.get(feishu_table.LISTING_STATE_FIELD))
            out[item["record_id"]] = {
                # 程序写的上架中 / 上架失败照原样记（2026-10-30 前读成待上架）
                "state": state if state in READABLE else STATE_PENDING,
                "listed_on": _day(ctx, fields.get(feishu_table.LISTING_DATE_FIELD)),
                "note": _text(fields.get(feishu_table.LISTING_NOTE_FIELD)),
            }
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return out


def _save(
    conn: Any,
    asset_id: str,
    record_id: str | None,
    values: dict[str, str | None],
    old: dict[str, Any] | None,
    *,
    source: str,
    by: str | None = None,
) -> bool:
    """写一款的上架状态；状态变了记事件并返回 True。"""
    now = now_iso()
    before = (old or {}).get("state") or STATE_PENDING
    first = (old or {}).get("first_listed_at") or (
        now if values["state"] == STATE_LISTED else None
    )
    conn.execute(
        "INSERT INTO listing_marks(asset_id, record_id, state, listed_on, note, first_listed_at, read_at, updated_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET"
        " record_id = excluded.record_id, state = excluded.state, listed_on = excluded.listed_on,"
        " note = excluded.note, first_listed_at = excluded.first_listed_at,"
        " read_at = excluded.read_at, updated_at = excluded.updated_at",
        (
            asset_id,
            record_id,
            values["state"],
            values.get("listed_on"),
            values.get("note"),
            first,
            now,
            now,
        ),
    )
    if before == values["state"]:
        return False
    log_event(
        conn,
        asset_id,
        "listing_changed",
        {"from": before, "to": values["state"], "source": source, "by": by},
    )
    trace.record(
        conn,
        asset_id,
        "listing_mark",
        frm=before,
        to=values["state"],
        actor=by,
        source=source,
        payload={"listed_on": values.get("listed_on"), "note": values.get("note")},
    )
    return True


def refresh(
    ctx: Ctx, api: feishu.Client | None = None, info: dict[str, Any] | None = None
) -> dict[str, Any]:
    """回读「选中的款」的上架状态写进 listing_marks：{read, rows, changed[]}。没开、还没这一列时跳过。
    没给 info 时旧表格和每周表格各天的「选中的款」都读一遍。"""
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    if not ready(ctx):
        return {"skipped": "上架状态没打开，或「选中的款」还没有这一列"}
    scopes = [info] if info else books.scopes(ctx)
    total: dict[str, Any] = {"read": 0, "rows": 0, "changed": []}

    def run(client: feishu.Client) -> None:
        for scope in scopes:
            if not feishu_table.listing_ready(scope):
                continue
            with books.scoped(ctx, scope):
                part = _refresh_scope(ctx, client, scope)
            total["read"] += part["read"]
            total["rows"] += part["rows"]
            total["changed"] += part["changed"]

    if api is None:
        with feishu.Client(ctx) as client:
            run(client)
    else:
        run(api)
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, READ_META_KEY, now_iso())
    return total


def _refresh_scope(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any]
) -> dict[str, Any]:
    cells = read_cells(ctx, api, info)
    marks = {r["asset_id"]: dict(r) for r in ctx.db.q("SELECT * FROM listing_marks")}
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT asset_id, record_id FROM table_rows WHERE record_id IS NOT NULL"
        )
        if table_registry.mine(r["asset_id"])
    ]
    changed = []
    with ctx.db.tx() as conn:
        for row in rows:
            values = cells.get(row["record_id"])
            if values is None:
                continue
            if _save(
                conn,
                row["asset_id"],
                row["record_id"],
                values,
                marks.get(row["asset_id"]),
                source="table",
            ):
                changed.append({"asset_id": row["asset_id"], "state": values["state"]})
    return {"read": len(cells), "rows": len(rows), "changed": changed}


def refresh_due(ctx: Ctx) -> dict[str, Any] | None:
    """巡检兜底（table poll）用：到点才回读，读不成只记原因，不挡控制行的回读。"""
    if not due(ctx):
        return None
    try:
        return refresh(ctx)
    except _FAILURES as exc:
        with ctx.db.tx() as conn:
            # 下一次巡检再试，不要每分钟重试
            ctx.db.set_meta(conn, READ_META_KEY, now_iso())
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _local_ms(ctx: Ctx, day: str) -> int:
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", day.strip())
    if not match:
        raise StageError(f"日期写成 2026-10-25 这样：{day}")
    try:
        local = dt.datetime(*(int(x) for x in match.groups()), tzinfo=_tz(ctx))
    except ValueError as exc:
        raise StageError(f"日期不对：{day}") from exc
    return int(local.timestamp() * 1000)


def set_state(
    ctx: Ctx,
    asset_id: str,
    state: str,
    *,
    by: str,
    date: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """改一款的上架状态（POD 对话里「IMG00325 已上架」）：写飞书「选中的款」那一格，再写业务库。"""
    if not ready(ctx):
        raise StageError(
            "上架状态没打开，或「选中的款」还没有这一列：先 podctl table sync"
        )
    if state not in STATES:
        raise StageError(f"上架状态只能是 {list(STATES)}")
    if note is not None and len(note) > NOTE_CHARS:
        raise StageError(f"备注最多 {NOTE_CHARS} 个字")
    row = ctx.db.q1(
        "SELECT t.record_id, a.human_decision FROM assets a"
        " LEFT JOIN table_rows t ON t.asset_id = a.asset_id WHERE a.asset_id = ?",
        (asset_id,),
    )
    if not row:
        raise StageError(f"没有素材 {asset_id}")
    if row["human_decision"] != "approved":
        raise StageError(f"{asset_id} 不是选中的款：只有选中的款才标上架状态")
    if not row["record_id"]:
        raise StageError(f"{asset_id} 还没进「选中的款」：等表格同步一两分钟再标")
    fields: dict[str, Any] = {feishu_table.LISTING_STATE_FIELD: state}
    values: dict[str, str | None] = {"state": state, "listed_on": None, "note": note}
    if date:
        fields[feishu_table.LISTING_DATE_FIELD] = _local_ms(ctx, date)
        values["listed_on"] = date.strip()
    if note is not None:
        fields[feishu_table.LISTING_NOTE_FIELD] = note
    old = ctx.db.q1("SELECT * FROM listing_marks WHERE asset_id = ?", (asset_id,))
    old = dict(old) if old else None
    if old:
        values["listed_on"] = values["listed_on"] or old.get("listed_on")
        values["note"] = note if note is not None else old.get("note")
    from . import candidates  # 候选表模块依赖本模块，放到用时再导入

    candidates.take_lease(ctx, busy="表格正在同步，过一会儿再标上架状态")
    try:
        from . import books  # 每周选款表依赖本模块，放到用时再导入

        # 这一款的行在它那一份「选中的款」里（旧表格，或每周表格的那一天）
        info = books.scope_of_asset(ctx, asset_id) or feishu_table.base(ctx) or {}
        with feishu.Client(ctx) as api:
            api.call(
                "PUT",
                f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records/{row['record_id']}",
                params={"user_id_type": "open_id"},
                json={"fields": fields},
            )
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    with ctx.db.tx() as conn:
        _save(conn, asset_id, row["record_id"], values, old, source="chat", by=by)
    return {
        "asset_id": asset_id,
        "label": asset_label_of(ctx, asset_id),
        "state": state,
        "listed_on": values["listed_on"],
        "note": values["note"],
    }


def seed_counts(ctx: Ctx) -> dict[str, dict[str, int]]:
    """每个任务（参考图）选中的款里已上架 / 不上了 / 待上架几款（没标的按待上架）。"""
    out: dict[str, dict[str, int]] = {}
    for row in ctx.db.q(_APPROVED_SQL):
        counts = out.setdefault(
            row["seed_id"], {"approved": 0, "listed": 0, "skipped": 0, "pending": 0}
        )
        counts["approved"] += 1
        state = row["state"] or STATE_PENDING
        key = {STATE_LISTED: "listed", STATE_SKIPPED: "skipped"}.get(state, "pending")
        counts[key] += 1
    return out


def status(
    ctx: Ctx,
    *,
    seed_id: str | None = None,
    asset_ids: list[str] | None = None,
    refresh_first: bool = False,
) -> dict[str, Any]:
    """选中的款的上架状态：每款一条（名字、任务、状态、上架日期、备注），和各状态款数；refresh_first 先回读一次。"""
    refreshed: dict[str, Any] | None = None
    if refresh_first and ready(ctx):
        try:
            refreshed = refresh(ctx)
        except _FAILURES as exc:
            refreshed = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    sql = _APPROVED_SQL
    params: list[Any] = []
    if seed_id:
        sql += " AND f.seed_id = ?"
        params.append(seed_id)
    if asset_ids:
        sql += f" AND a.asset_id IN ({','.join('?' for _ in asset_ids)})"
        params += list(asset_ids)
    items = []
    counts = {"listed": 0, "skipped": 0, "pending": 0}
    for row in ctx.db.q(sql + " ORDER BY a.asset_id", tuple(params)):
        state = row["state"] or STATE_PENDING
        counts[
            {STATE_LISTED: "listed", STATE_SKIPPED: "skipped"}.get(state, "pending")
        ] += 1
        items.append(
            {
                "asset_id": row["asset_id"],
                "label": asset_label_of(ctx, row["asset_id"]),
                "seed_id": row["seed_id"],
                "task": task_label(ctx, row["seed_id"]),
                "state": state,
                "listed_on": row["listed_on"],
                "note": row["note"],
                "first_listed_at": row["first_listed_at"],
                "in_table": bool(row["record_id"]),
            }
        )
    missing = sorted(set(asset_ids or []) - {i["asset_id"] for i in items})
    return {
        "enabled": enabled(ctx),
        "ready": ready(ctx),
        "read_at": ctx.db.get_meta(READ_META_KEY),
        "refreshed": refreshed,
        "counts": counts,
        "items": items,
        "not_selected": missing,
        "table_url": feishu_table.table_url(ctx),
    }
