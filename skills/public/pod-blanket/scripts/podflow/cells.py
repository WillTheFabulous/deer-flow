"""人在飞书表里改的值全部落库（2026-10-30，feature-plans/pod-listing-autopilot）。

- table_cells：每一格上次读到的值（按 表 / 记录 / 列）；table_cell_log：人改了哪一格（旧值、新值、修改人、修改时间、
  程序怎么处理的 outcome：recorded 只记下 / accepted 照做了 / rejected 做不了 / reverted 程序列会改回 / deleted 行没了）。
  table_row_snapshots：程序删行、清格之前的整行快照。
- `observe` 拿一次回读（带 automatic_fields 的 last_modified_by / last_modified_time）和上次读到的比：第一次读到的格子只记下；
  之后值变了、又不是程序刚写的（`program` 给出程序上次写的值），就记一条人改的日志。读的是整张表时（full），上次还在、
  这次没了的行记一条 deleted。
- 三处回读：「选中的款」（`read_selected`，巡检 table poll 里每 table.cells_read_minutes 分钟）、「已上架的款」
  （listed_table 回读时）、候选表（candidate_stores 巡检时）。指令列（上架标题）的改动交给 title_sync。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from typing import Any

from . import feishu, store, table_registry, trace
from .context import Ctx
from .util import dumps, loads, now_iso, parse_iso, utcnow

KIND_SELECTED = "selected"
KIND_LISTED = "listed"
KIND_CANDIDATE = "candidate"
OUTCOME_RECORDED = "recorded"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected"
OUTCOME_REVERTED = "reverted"
OUTCOME_DELETED = "deleted"
ROW_FIELD = "__row__"
READ_META_KEY = "cells.selected_read_at"
MISSING = object()
_PAGE = 500
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, KeyError)

# 「选中的款」里表格同步写、又没记着写了什么的列（分不清是不是人改的，不看）：其余的（备注、上架几列、下载确认、人加的列）都看
SELECTED_IGNORE = frozenset(
    {
        "素材",
        "缩略图",
        "原图",
        "原图编号",
        "方向",
        "改了什么",
        "不变",
        "方位",
        "类型",
        "模型",
        "出图方式",
        "批次",
        "系列",
        "尺寸",
        "选中时间",
        "选中人",
        "修改自",
        "下载原图",
        "链接到期",
        "套图状态",
        "场景图张数",
        "下载套图",
        "排序",
    }
)
# 「选中的款」里上架那几列是程序写的（temu_rows 记着上次写的值）：人改了记 reverted，下次写表改回
SELECTED_REVERT = frozenset(
    {
        "店铺",
        "货号",
        "上架状态",
        "上架备注",
        "上架日期",
        "当前环节",
        "环节开始",
        "妙手 ID",
        "标题同步",
    }
)


def normalize(value: Any) -> Any:
    """飞书格子的值换成能比较、能存的样子：文本（含分段）→ 字符串，勾选框 → 布尔，链接 → 网址，附件 → 文件 token 列表，
    人员 → id 列表，多选 → 排好序的列表；空的一律 None。"""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        return text or None
    if isinstance(value, dict):
        if "link" in value:
            return value.get("link") or None
        if "text" in value or "name" in value:
            return normalize(value.get("text") or value.get("name"))
        return dumps(value)
    if isinstance(value, list):
        if all(isinstance(v, dict) and "file_token" in v for v in value):
            return [v["file_token"] for v in value]
        if all(
            isinstance(v, dict)
            and ("text" in v)
            and v.get("type") in (None, "text", "url", "mention")
            for v in value
        ):
            joined = "".join(str(v.get("text") or "") for v in value).strip()
            return joined or None
        if all(isinstance(v, dict) and "id" in v for v in value):
            return [v["id"] for v in value]
        parts = [normalize(v) for v in value]
        parts = [p for p in parts if p is not None]
        if all(isinstance(p, str) for p in parts):
            return sorted(parts) if len(parts) > 1 else (parts[0] if parts else None)
        return parts or None
    return str(value)


def modifier(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """一条记录（带 automatic_fields）的最后修改人（feishu:ou_… 才算人）和最后修改时间（ISO）。"""
    who = (item.get("last_modified_by") or {}).get("id")
    by = f"feishu:{who}" if isinstance(who, str) and who.startswith("ou_") else None
    ms = item.get("last_modified_time")
    at = None
    if isinstance(ms, int | float) or (isinstance(ms, str) and ms.isdigit()):
        at = dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return by, at


def read_table(
    api: feishu.Client, info: dict[str, Any], table_id: str
) -> dict[str, dict[str, Any]]:
    """分页读一整张表（全部列，带最后修改人 / 时间）：{record_id: {fields, by, at}}。"""
    path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/records"
    out: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "page_size": _PAGE,
            "user_id_type": "open_id",
            "automatic_fields": "true",
        }
        if token:
            params["page_token"] = token
        data = api.call("GET", path, params=params)
        for item in data.get("items") or []:
            by, at = modifier(item)
            out[item["record_id"]] = {
                "fields": item.get("fields") or {},
                "by": by,
                "at": at,
            }
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return out


def observe(
    ctx: Ctx,
    *,
    kind: str,
    table_id: str,
    records: dict[str, dict[str, Any]],
    refs: dict[str, dict[str, Any]],
    ignore: Iterable[str] = (),
    watch: Iterable[str] | None = None,
    program: Callable[[str, str], Any] | None = None,
    revert: Iterable[str] = (),
    full: bool = False,
    via: str = "poll",
) -> list[dict[str, Any]]:
    """比一次回读和上次读到的，人改了的格子记日志（返回这些改动：{log_id, record_id, asset_id, code, field, old, new, by, at}）。
    refs：{record_id: {asset_id, code}}，只看这些行（程序认得的行）；watch 给了就只看这几列，否则看 ignore 以外的全部列。
    program(record_id, 列名) 给程序上次写进这一格的值（MISSING = 程序不写这一格）：新值和它一样就是程序写的，不记。
    revert 里的列是程序列：人改了记 reverted（下次同步改回）。full：records 是整张表，上次有这次没有的行记 deleted。"""
    ignored = set(ignore)
    watched = set(watch) if watch is not None else None
    reverting = set(revert)
    old: dict[str, dict[str, dict[str, Any]]] = {}
    for r in ctx.db.q("SELECT * FROM table_cells WHERE table_id = ?", (table_id,)):
        old.setdefault(r["record_id"], {})[r["field"]] = dict(r)
    now = now_iso()
    upserts: list[tuple[Any, ...]] = []
    logs: list[dict[str, Any]] = []
    for record_id, item in records.items():
        ref = refs.get(record_id)
        if ref is None:
            continue
        fields = item.get("fields") or {}
        before = old.get(record_id) or {}
        names = {*fields, *(f for f in before if f != ROW_FIELD)}
        if watched is not None:
            names &= watched
        names -= ignored
        for name in sorted(names):
            new = normalize(fields.get(name))
            seen = before.get(name)
            new_json = dumps(new)
            if seen is not None and seen.get("value_json") == new_json:
                continue
            upserts.append(
                (
                    table_id,
                    record_id,
                    name,
                    kind,
                    ref.get("asset_id"),
                    ref.get("code"),
                    new_json,
                    item.get("by"),
                    item.get("at"),
                    now,
                )
            )
            written = program(record_id, name) if program is not None else MISSING
            if written is not MISSING and dumps(normalize(written)) == new_json:
                continue
            if seen is None and written is MISSING:
                # 第一次读到、程序也不写这一格：只记下（之后再变才算人改）
                continue
            logs.append(
                {
                    "record_id": record_id,
                    "asset_id": ref.get("asset_id"),
                    "code": ref.get("code"),
                    "field": name,
                    "old": loads(seen.get("value_json"), None)
                    if seen
                    else normalize(written),
                    "new": new,
                    "by": item.get("by"),
                    "at": item.get("at"),
                    "outcome": OUTCOME_REVERTED
                    if name in reverting
                    else OUTCOME_RECORDED,
                }
            )
        if ROW_FIELD not in before:
            upserts.append(
                (
                    table_id,
                    record_id,
                    ROW_FIELD,
                    kind,
                    ref.get("asset_id"),
                    ref.get("code"),
                    dumps("present"),
                    item.get("by"),
                    item.get("at"),
                    now,
                )
            )
    if full:
        for record_id, before in old.items():
            row = before.get(ROW_FIELD)
            if (
                record_id in records
                or not row
                or loads(row.get("value_json"), None) != "present"
            ):
                continue
            upserts.append(
                (
                    table_id,
                    record_id,
                    ROW_FIELD,
                    kind,
                    row.get("asset_id"),
                    row.get("code"),
                    dumps("deleted"),
                    None,
                    None,
                    now,
                )
            )
            logs.append(
                {
                    "record_id": record_id,
                    "asset_id": row.get("asset_id"),
                    "code": row.get("code"),
                    "field": ROW_FIELD,
                    "old": "present",
                    "new": "deleted",
                    "by": None,
                    "at": None,
                    "outcome": OUTCOME_DELETED,
                }
            )
    if not upserts and not logs:
        return []
    with ctx.db.tx() as conn:
        for values in upserts:
            conn.execute(
                "INSERT INTO table_cells(table_id, record_id, field, kind, asset_id, code, value_json, modified_by,"
                " modified_at, read_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(table_id, record_id, field)"
                " DO UPDATE SET kind = excluded.kind, asset_id = excluded.asset_id, code = excluded.code,"
                " value_json = excluded.value_json, modified_by = excluded.modified_by,"
                " modified_at = excluded.modified_at, read_at = excluded.read_at",
                values,
            )
        for entry in logs:
            conn.execute(
                "INSERT INTO table_cell_log(ts, kind, table_id, record_id, asset_id, code, field, old_json, new_json,"
                " editor, edited_at, via, outcome) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    kind,
                    table_id,
                    entry["record_id"],
                    entry["asset_id"],
                    entry["code"],
                    entry["field"],
                    dumps(entry["old"]),
                    dumps(entry["new"]),
                    entry["by"],
                    entry["at"],
                    via,
                    entry["outcome"],
                ),
            )
            entry["log_id"] = conn.execute(
                "SELECT MAX(log_id) FROM table_cell_log"
            ).fetchone()[0]
            if entry["asset_id"]:
                trace.record(
                    conn,
                    entry["asset_id"],
                    "cell",
                    frm=None if entry["old"] is None else str(entry["old"])[:100],
                    to=None if entry["new"] is None else str(entry["new"])[:100],
                    code=entry["code"],
                    actor=entry["by"],
                    source=f"{kind}·{entry['field']}",
                    reason=entry["outcome"],
                )
    return logs


def set_outcome(ctx: Ctx, log_id: int, outcome: str, note: str | None = None) -> None:
    """程序处理完一条人改的格子后改它的结果（accepted / rejected …）。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE table_cell_log SET outcome = ?, note = ? WHERE log_id = ?",
            (outcome, (note or "")[:300] or None, log_id),
        )


def snapshot(
    ctx: Ctx,
    *,
    kind: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
    reason: str,
    asset_id: str | None = None,
    code: str | None = None,
) -> None:
    """程序删行 / 清格之前，把这一行原样存一份（人填的备注、人加的列都在里面）。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO table_row_snapshots(ts, kind, table_id, record_id, asset_id, code, reason, fields_json)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                kind,
                table_id,
                record_id,
                asset_id,
                code,
                reason,
                dumps({k: normalize(v) for k, v in fields.items()}),
            ),
        )


def snapshot_records(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    record_ids: list[str],
    *,
    kind: str,
    reason: str,
    refs: dict[str, dict[str, Any]] | None = None,
) -> int:
    """删 / 清这些行之前先读回来存快照（读不到的跳过，不挡删除）。返回存了几行。"""
    if not record_ids:
        return 0
    path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/records/batch_get"
    saved = 0
    for i in range(0, len(record_ids), 100):
        try:
            data = api.call(
                "POST",
                path,
                json={"record_ids": record_ids[i : i + 100], "user_id_type": "open_id"},
            )
        except _FAILURES:
            continue
        for item in data.get("records") or []:
            ref = (refs or {}).get(item["record_id"]) or {}
            fields = item.get("fields") or {}
            if not any(normalize(v) is not None for v in fields.values()):
                continue
            snapshot(
                ctx,
                kind=kind,
                table_id=table_id,
                record_id=item["record_id"],
                fields=fields,
                reason=reason,
                asset_id=ref.get("asset_id"),
                code=ref.get("code"),
            )
            saved += 1
    return saved


def history(
    ctx: Ctx, *, asset_id: str | None = None, code: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """人改格子的日志（新的在前）。"""
    sql = "SELECT * FROM table_cell_log WHERE 1 = 1"
    params: list[Any] = []
    if asset_id:
        sql += " AND asset_id = ?"
        params.append(asset_id)
    if code:
        sql += " AND code = ?"
        params.append(code)
    out = []
    for r in ctx.db.q(sql + " ORDER BY log_id DESC LIMIT ?", (*params, limit)):
        item = dict(r)
        item["old"] = loads(item.pop("old_json"), None)
        item["new"] = loads(item.pop("new_json"), None)
        out.append(item)
    return out


# ---------------------------------------------------------------- 「选中的款」


def due(ctx: Ctx) -> bool:
    from . import feishu_table  # 表格模块依赖很多模块，用时再导入

    if not feishu_table.enabled(ctx) or not ctx.setting("table.cells_enabled", True):
        return False
    minutes = float(ctx.setting("table.cells_read_minutes", 10) or 0)
    if minutes <= 0:
        return False
    last = parse_iso(ctx.db.get_meta(READ_META_KEY))
    return last is None or utcnow() - last >= dt.timedelta(minutes=minutes, seconds=-30)


def _selected_program(ctx: Ctx, asset_of: dict[str, str]) -> Callable[[str, str], Any]:
    """「选中的款」里程序上次写进各格的值：上架几列看 temu_rows，下载确认看 selected_rows。"""
    temu_cache = {r["asset_id"]: dict(r) for r in ctx.db.q("SELECT * FROM temu_rows")}
    downloads_cache = {
        r["asset_id"]: dict(r) for r in ctx.db.q("SELECT * FROM selected_rows")
    }

    def program(record_id: str, name: str) -> Any:
        from . import feishu_table  # 同上

        asset_id = asset_of.get(record_id)
        if not asset_id:
            return MISSING
        row = temu_cache.get(asset_id) or {}
        if row.get("record_id") == record_id:
            if (
                name == feishu_table.TEMU_CONFIRM_FIELD
                and row.get("confirm_written") is not None
            ):
                return bool(row["confirm_written"])
            cells = loads(row.get("state_key"), {}) or {}
            if name in cells:
                return cells[name]
        down = downloads_cache.get(asset_id) or {}
        if (
            name == feishu_table.DOWNLOAD_CONFIRM_FIELD
            and down.get("record_id") == record_id
            and down.get("checked") is not None
        ):
            return bool(down["checked"])
        return MISSING

    return program


def read_selected(ctx: Ctx, *, force: bool = False) -> dict[str, Any] | None:
    """回读每一份「选中的款」，人改的格子记日志；改了「上架标题」的交给 title_sync。没到时候为 None。"""
    from . import (
        books,
        feishu_table,
        stores,
        title_sync,
    )  # 都依赖本模块之外的很多表，用时再导入

    if not force and not due(ctx):
        return None
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, READ_META_KEY, now_iso())
    out: dict[str, Any] = {"read": 0, "changed": 0, "titles": [], "errors": []}
    rows = {
        r["record_id"]: r["asset_id"]
        for r in ctx.db.q(
            "SELECT asset_id, record_id FROM table_rows WHERE record_id IS NOT NULL"
        )
    }
    codes = {}
    for r in stores.rows(ctx):
        if r.get("code"):
            codes.setdefault(r["asset_id"], r["code"])
    refs = {rid: {"asset_id": a, "code": codes.get(a)} for rid, a in rows.items()}
    program = _selected_program(ctx, rows)
    try:
        with feishu.Client(ctx) as api:
            for info in books.scopes(ctx):
                if not info.get("table_id"):
                    continue
                try:
                    records = read_table(api, info, info["table_id"])
                except _FAILURES as exc:
                    out["errors"].append(
                        {
                            "table_id": info["table_id"],
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    )
                    continue
                out["read"] += len(records)
                mine = {rid: ref for rid, ref in refs.items() if rid in records}
                changes = observe(
                    ctx,
                    kind=KIND_SELECTED,
                    table_id=info["table_id"],
                    records=records,
                    refs=mine,
                    ignore=SELECTED_IGNORE,
                    program=program,
                    revert=SELECTED_REVERT,
                    full=True,
                )
                out["changed"] += len(changes)
                out["reverted"] = out.get("reverted", []) + _forget_written(
                    ctx, [c for c in changes if c["outcome"] == OUTCOME_REVERTED]
                )
                for change in changes:
                    if (
                        change["field"] != feishu_table.TEMU_TITLE_FIELD
                        or not change["asset_id"]
                    ):
                        continue
                    result = title_sync.handle_edit(
                        ctx,
                        change["asset_id"],
                        str(change["new"] or ""),
                        by=change["by"] or "feishu:选中的款",
                        via=KIND_SELECTED,
                    )
                    if result.get("same"):
                        continue
                    set_outcome(
                        ctx,
                        change["log_id"],
                        OUTCOME_REJECTED
                        if result.get("rejected")
                        else OUTCOME_ACCEPTED,
                        result.get("rejected"),
                    )
                    out["titles"].append({"asset_id": change["asset_id"], **result})
    except _FAILURES as exc:
        out["errors"].append({"error": f"{type(exc).__name__}: {exc}"[:300]})
    touched = {t["asset_id"] for t in out["titles"]} | set(out.get("reverted") or [])
    if touched:
        from . import temu  # 上架模块依赖本模块，用时再导入

        out["table"] = temu.write_now(ctx, sorted(touched))
    return out


def _forget_written(ctx: Ctx, changes: list[dict[str, Any]]) -> list[str]:
    """人改了程序写的上架列：从 temu_rows 里删掉这几格上次写的值，下次写表就会改回。返回涉及的款。"""
    fields: dict[str, set[str]] = {}
    for change in changes:
        if change.get("asset_id"):
            fields.setdefault(change["asset_id"], set()).add(change["field"])
    if not fields:
        return []
    kept: dict[str, str] = {}
    for asset_id, names in fields.items():
        row = ctx.db.q1(
            "SELECT state_key FROM temu_rows WHERE asset_id = ?", (asset_id,)
        )
        if row:
            cells = loads(row["state_key"], {}) or {}
            kept[asset_id] = dumps({k: v for k, v in cells.items() if k not in names})
    with ctx.db.tx() as conn:
        for asset_id, state_key in kept.items():
            conn.execute(
                "UPDATE temu_rows SET state_key = ?, updated_at = ? WHERE asset_id = ?",
                (state_key, now_iso(), asset_id),
            )
    return sorted(fields)
