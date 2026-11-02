"""每周选款表迁移（podctl maintenance migrate-week，2026-10-29）：把一周里还在旧表格的任务搬进那一周的周表格。

- 搬哪些：上传日期（timezone）在这一周、不早于 table.weekly_since、旧表格里有候选表或「选中的款」行的任务；搬过去
  以后归属（seed_places）定成上传那天，之后照每周选款表的规矩走。
- 业务库是底账，新表的行照业务库重写：
  1. 先把旧表里人填的读回业务库（上架状态、没选原因），旧表的记录整份导出 JSON 备份，业务库相关的行也一起导出。
  2. 建周表格和那几天的文件夹（books.ensure_day）。人自己加的列缺省不搬（值留在 JSON 备份里，2026-10-29 定的）；
     keep_columns 时照旧表的定义在新表里建上再搬。
  3. 每个任务：业务库里指向旧表行的记录清掉（候选表、行、预留行、表顶行、排序、套图行、「选中的款」的行…；选款结论、
     下载确认、上架状态这些底账不动），从周表格的「候选表模板」复制新候选表进那天的文件夹，旧表里有的行都照业务库
     重写（定了的先按待选写，同步时改成定了的样子），没出成 / 未生成的预留行照原样重建；再跑表格同步（「选中的款」
     进那天的那张；选款结论、链接、表顶行、套图列、下载确认、排序都由同步写）。
  4. 人填的格子按素材 / 槽位对上新行搬过去：候选表待选行的「选中」、「没选原因」「备注」；「选中的款」的「备注」、
     上架状态三列（keep_columns 时还有人加的列）。按钮行的备注从业务库写回。
  5. 逐格核对：旧表的每一行都有新行，搬过去的格子新旧一致，选款结论、下载确认新旧一致；不一致就停在这里，旧表不删。
  6. 删掉旧表格里这几个任务的候选表、「选中的款」里它们的行。
- 跑的时候持有选款表的同步租约和迁移租约（books.MIGRATE_LEASE）：同步进程拿不到租约，巡检回读、记录变更事件先不处理。
  每一步做完记在 meta books.migrate.<周>，中途失败了重跑接着做：不重复建表、不重复写行。
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from . import books, candidates, feishu, feishu_table, lease, listing, store, table_registry, views
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso, utcnow

STATE_PREFIX = "books.migrate."
WEEK_RE = re.compile(r"\d{4}-W\d{2}")
_PAGE = 500
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)

# 候选表里人填的列（「选中」只搬还在待选的行：定了的由同步照选款结论写）
CANDIDATE_HUMAN = (candidates.REASON_FIELD, candidates.NOTE_FIELD)
# 「选中的款」里人填的列（上架状态三列程序只在建行时写「待上架」，之后归人）
SELECTED_HUMAN = (
    "备注",
    feishu_table.LISTING_STATE_FIELD,
    feishu_table.LISTING_DATE_FIELD,
    feishu_table.LISTING_NOTE_FIELD,
)
# 程序照业务库写的列：新旧应当一致，不一致说明同步还没写完（先不删旧表）
CANDIDATE_CHECKED = (candidates.STATE_FIELD,)
SELECTED_CHECKED = (feishu_table.DOWNLOAD_CONFIRM_FIELD,)
# 不搬的列类型：附件（file_token 跨表格用不了）、关联、查找引用、公式、创建 / 修改时间和人、自动编号
_NOT_COPIED = {17, 18, 19, 20, 21, 1001, 1002, 1003, 1004, 1005}

# 备份里业务库的哪些表：按任务、按素材
_SEED_TABLES = (
    "candidate_tables",
    "candidate_rows",
    "candidate_slots",
    "candidate_actions",
    "candidate_scene_rows",
    "candidate_orders",
    "candidate_controls",
    "selection_reasons",
    "scene_packages",
    "seed_names",
    "seed_shelf",
    "seed_places",
)
_ASSET_TABLES = (
    "table_rows",
    "selected_rows",
    "selected_downloads",
    "temu_rows",
    "candidate_codes",
    "listing_marks",
    "scene_sets",
)


# ---------------------------------------------------------------- 状态


def _key(week: str) -> str:
    return STATE_PREFIX + week


def state(ctx: Ctx, week: str) -> dict[str, Any]:
    return loads(ctx.db.get_meta(_key(week)), None) or {}


def _save(ctx: Ctx, week: str, value: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, _key(week), dumps(value))


def _check_week(week: str) -> None:
    if not WEEK_RE.fullmatch(week or ""):
        raise StageError(f"周不对：{week}（应当像 2026-W44）")


def _days_of(week: str) -> list[dt.date]:
    monday = books.week_monday(week)
    return [monday + dt.timedelta(days=i) for i in range(7)]


def _renew(ctx: Ctx) -> None:
    lease.take(ctx, books.MIGRATE_LEASE, books.MIGRATE_SECONDS)
    lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)


# ---------------------------------------------------------------- 搬哪些


def _assets(ctx: Ctx, seed_id: str) -> list[str]:
    return [
        r["asset_id"]
        for r in ctx.db.q(
            "SELECT a.asset_id FROM assets a JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " WHERE f.seed_id = ? ORDER BY a.asset_id",
            (seed_id,),
        )
    ]


def _rows_in(ctx: Ctx, table: str, column: str, ids: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), _PAGE):
        chunk = ids[i : i + _PAGE]
        marks = ",".join("?" for _ in chunk)
        out += [
            dict(r)
            for r in ctx.db.q(f"SELECT * FROM {table} WHERE {column} IN ({marks})", tuple(chunk))
        ]
    return out


def _seeds(ctx: Ctx, week: str, current: dict[str, Any]) -> dict[str, str]:
    """要搬的任务：{seed_id: 归到的那一天}。迁移状态里已经有的照旧算（重跑接着做）。"""
    days = {d.isoformat() for d in _days_of(week)}
    first = books.since(ctx)
    out = {s: e["day"] for s, e in (current.get("seeds") or {}).items()}
    for row in ctx.db.q("SELECT seed_id, created_at FROM seeds ORDER BY seed_id"):
        seed_id = row["seed_id"]
        if seed_id in out:
            continue
        day = books.local_day(ctx, row["created_at"])
        if not day or day.isoformat() not in days or day < first:
            continue
        # 已经在每周表格里的不搬；旧表格里什么都没有的，以后照每周选款表的规矩建
        if books.place_of(ctx, seed_id) or not books._in_legacy(ctx, seed_id):
            continue
        out[seed_id] = day.isoformat()
    return dict(sorted(out.items()))


def _snapshot(ctx: Ctx, seed_id: str, day: str) -> dict[str, Any]:
    """清掉业务库记录之前，记下旧表里每一行是什么：{旧记录号: 素材 / 槽位 / 表顶行}。"""
    table = candidates.table_info(ctx, seed_id) or {}
    q = ctx.db.q
    assets = _assets(ctx, seed_id)
    return {
        "day": day,
        "old_table": table.get("table_id"),
        "rows": {
            r["record_id"]: r["asset_id"]
            for r in q(
                "SELECT asset_id, record_id FROM candidate_rows WHERE seed_id = ? AND record_id IS NOT NULL",
                (seed_id,),
            )
        },
        "slots": {
            r["record_id"]: [r["job_id"], r["slot_id"], r["state"]]
            for r in q(
                "SELECT job_id, slot_id, state, record_id FROM candidate_slots"
                " WHERE seed_id = ? AND record_id IS NOT NULL AND asset_id IS NULL",
                (seed_id,),
            )
        },
        "actions": {
            r["record_id"]: [r["kind"], r["batch_id"]]
            for r in q(
                "SELECT kind, batch_id, record_id FROM candidate_actions"
                " WHERE seed_id = ? AND record_id IS NOT NULL",
                (seed_id,),
            )
        },
        "scene_rows": {
            r["record_id"]: r["asset_id"]
            for r in q(
                "SELECT asset_id, record_id FROM candidate_scene_rows WHERE seed_id = ? AND record_id IS NOT NULL",
                (seed_id,),
            )
        },
        "selected": {
            r["record_id"]: r["asset_id"]
            for r in _rows_in(ctx, "table_rows", "asset_id", assets)
            if r["record_id"]
        },
        "stash": {
            k: table.get(k)
            for k in (
                "accepted_at",
                "export_key",
                "export_path",
                "export_url",
                "export_expires_at",
                "export_count",
                "export_bytes",
            )
        },
        "stage": "planned",
    }


def _open_assets(ctx: Ctx, assets: list[str]) -> set[str]:
    """还在待选（选款待审开着）的素材。"""
    out: set[str] = set()
    for i in range(0, len(assets), _PAGE):
        chunk = assets[i : i + _PAGE]
        marks = ",".join("?" for _ in chunk)
        out |= {
            r["subject_id"]
            for r in ctx.db.q(
                "SELECT subject_id FROM reviews WHERE subject_type = 'asset_selection' AND status = 'open'"
                f" AND subject_id IN ({marks})",
                tuple(chunk),
            )
        }
    return out


# ---------------------------------------------------------------- 飞书小工具


def _read_all(api: feishu.Client, app_token: str, table_id: str) -> dict[str, dict[str, Any]]:
    """整张表的记录：{record_id: 字段}（不带 field_names 的列表接口：多选是选项列表、勾选框是 true / false、日期是毫秒）。"""
    path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    out: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": _PAGE, "user_id_type": "open_id"}
        if token:
            params["page_token"] = token
        data = api.call("GET", path, params=params)
        for item in data.get("items") or []:
            out[item["record_id"]] = item.get("fields") or {}
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return out


def _fields(api: feishu.Client, app_token: str, table_id: str) -> dict[str, dict[str, Any]]:
    return {
        f["field_name"]: f
        for f in views.list_fields(api, {"app_token": app_token}, table_id)
    }


def _norm(value: Any) -> Any:
    """比较用的样子：空值、没勾的勾选框都算空；文本分段拼起来；多选、人员排好序；链接只看地址。"""
    if value is None or value is False or value == "" or value == []:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return sorted(v.strip() for v in value if v.strip()) or None
        if all(isinstance(v, dict) and v.get("id") and "text" not in v for v in value):
            return sorted(str(v["id"]) for v in value)
        if all(isinstance(v, dict) for v in value):
            return "".join(str(v.get("text") or "") for v in value).strip() or None
        return dumps(value)
    if isinstance(value, dict):
        if "link" in value:
            return value.get("link") or None
        if "text" in value:
            return str(value.get("text") or "").strip() or None
        return dumps(value)
    return value


def _writable(value: Any, field_type: int) -> Any:
    """旧格子的值照新表那一列的类型写回去的样子；空的、写不了的（附件这类）返回 None。"""
    norm = _norm(value)
    if norm is None or field_type in _NOT_COPIED:
        return None
    if field_type == 15:
        if isinstance(value, dict):
            return {"link": value.get("link"), "text": value.get("text") or value.get("link")}
        return {"link": norm, "text": norm}
    if field_type == 11:
        return [{"id": v["id"]} for v in value if isinstance(v, dict) and v.get("id")] or None
    if field_type == 4:
        return norm if isinstance(norm, list) else [norm]
    if field_type == 7:
        return True
    return norm


def _user_specs(
    old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]], known: set[str]
) -> list[dict[str, Any]]:
    """旧表里有、新表里没有、程序也不建的列（人自己加的）：照旧的定义（类型、选项）在新表里建的样子。"""
    out = []
    for name, field in old.items():
        if name in new or name in known or field.get("type") in _NOT_COPIED:
            continue
        spec: dict[str, Any] = {"field_name": name, "type": field["type"]}
        prop = field.get("property")
        if prop:
            if prop.get("options"):
                prop = {
                    **prop,
                    "options": [
                        {k: o[k] for k in ("name", "color") if k in o} for o in prop["options"]
                    ],
                }
            spec["property"] = prop
        out.append(spec)
    return out


def _selected_known(ctx: Ctx) -> set[str]:
    """「选中的款」里程序建的列（不算人加的）。"""
    from . import temu  # 上架模块依赖表格模块，放到用时再导入

    try:
        extra = temu.fields(ctx)
    except (StageError, KeyError, ValueError):
        extra = []
    return {
        f["field_name"]
        for f in feishu_table.FIELDS
        + feishu_table.LISTING_FIELDS
        + feishu_table.DOWNLOAD_FIELDS
        + extra
    }


def _candidate_known(ctx: Ctx) -> set[str]:
    """候选表里程序建的列（不算人加的；改名前的「状态」也算）。"""
    return {f["field_name"] for f in candidates.table_fields(ctx)} | {
        candidates.PACKAGE_FIELD,
        candidates.OLD_STATE_FIELD,
    }


def _write_cells(
    new_fields: dict[str, dict[str, Any]], cells: dict[str, Any], wanted: list[str]
) -> dict[str, Any]:
    """旧行里这几列有值的，照新表的列类型写成要写的样子（新表没有这一列的跳过）。"""
    out = {}
    for name in wanted:
        field = new_fields.get(name)
        if not field:
            continue
        value = _writable(cells.get(name), int(field["type"]))
        if value is not None:
            out[name] = value
    return out


def _load_backups(current: dict[str, Any]) -> dict[str, Any]:
    """各任务记着的备份文件合在一起：{candidates: {seed_id: {...}}, selected: {record_id: 字段}}。"""
    merged: dict[str, Any] = {"candidates": {}, "selected": {}}
    for path in sorted({e["backup"] for e in current["seeds"].values() if e.get("backup")}):
        data = loads(Path(path).read_text(encoding="utf-8"), {}) or {}
        merged["candidates"].update(data.get("candidates") or {})
        merged["selected"].update(data.get("selected") or {})
    return merged


def _batch_update(
    api: feishu.Client, app_token: str, table_id: str, updates: list[tuple[str, dict[str, Any]]]
) -> None:
    path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    for i in range(0, len(updates), _PAGE):
        api.call(
            "POST",
            path,
            params={"user_id_type": "open_id"},
            json={
                "records": [
                    {"record_id": rid, "fields": fields} for rid, fields in updates[i : i + _PAGE]
                ]
            },
        )


# ---------------------------------------------------------------- 清单（dry-run）


def plan(ctx: Ctx, week: str) -> dict[str, Any]:
    """只读：按天列出要搬的任务、要建的表、要重写的行、要搬的人填格子、要删的旧东西。"""
    _check_week(week)
    legacy = feishu_table.base(ctx)
    if not legacy:
        raise StageError("旧表格还没建：没有要搬的")
    current = state(ctx, week)
    seeds = _seeds(ctx, week, current)
    monday = books.week_monday(week)
    found = books.book(ctx, week) or {}
    out: dict[str, Any] = {
        "week": week,
        "book": {"title": books.book_title(monday), "url": found.get("url"), "exists": bool(found)},
        "state": {
            **{
                k: current.get(k)
                for k in ("started_at", "verified_at", "done_at", "problems")
                if current.get(k)
            },
            **(
                {"stages": {s: e["stage"] for s, e in current["seeds"].items()}}
                if current.get("seeds")
                else {}
            ),
        },
        "days": {},
        "missing_tables": [],
        "unknown_rows": {},
        "human_cells": {"candidates": {}, "selected": {}},
        "user_columns": {"selected": [], "candidates": {}},
        "user_columns_note": "人自己加的列缺省不搬（值留在 JSON 备份里）；要搬加 --keep-columns",
        "user_cells": {},
        "delete": {"tables": [], "selected_rows": 0},
    }
    if not seeds:
        return out
    app = legacy["app_token"]
    cand_cells: Counter[str] = Counter()
    sel_cells: Counter[str] = Counter()
    user_cells: Counter[str] = Counter()
    with feishu.Client(ctx) as api:
        existing = books._tables(api, app)
        ids = set(existing.values())
        names = {v: k for k, v in existing.items()}
        selected_records = _read_all(api, app, legacy["table_id"])
        old_selected_fields = _fields(api, app, legacy["table_id"])
        user_selected = _user_specs(old_selected_fields, {}, _selected_known(ctx))
        out["user_columns"]["selected"] = [s["field_name"] for s in user_selected]
        for seed_id, day in seeds.items():
            entry = (current.get("seeds") or {}).get(seed_id) or _snapshot(ctx, seed_id, day)
            info = out["days"].setdefault(
                day,
                {
                    "folder": books.folder_name(day),
                    "selected_table": books.selected_name(day),
                    "seeds": [],
                    "candidate_rows": 0,
                    "slot_rows": 0,
                    "top_rows": 0,
                    "selected_rows": 0,
                },
            )
            info["seeds"].append(seed_id)
            info["candidate_rows"] += len(entry["rows"])
            info["slot_rows"] += len(entry["slots"])
            info["top_rows"] += len(entry["actions"]) + len(entry["scene_rows"])
            info["selected_rows"] += len(entry["selected"])
            tid = entry["old_table"]
            if tid and tid in ids:
                out["delete"]["tables"].append({"seed_id": seed_id, "table_id": tid, "name": names[tid]})
                records = _read_all(api, app, tid)
                known = set(entry["rows"]) | set(entry["slots"]) | set(entry["actions"]) | set(entry["scene_rows"])
                unknown = sorted(set(records) - known)
                if unknown:
                    out["unknown_rows"][seed_id] = len(unknown)
                user = _user_specs(_fields(api, app, tid), {}, _candidate_known(ctx))
                if user:
                    out["user_columns"]["candidates"][seed_id] = [s["field_name"] for s in user]
                pending = _open_assets(ctx, list(entry["rows"].values()))
                for rid, asset in entry["rows"].items():
                    cells = records.get(rid) or {}
                    for name in CANDIDATE_HUMAN:
                        if _norm(cells.get(name)) is not None:
                            cand_cells[name] += 1
                    for spec in user:
                        if _norm(cells.get(spec["field_name"])) is not None:
                            user_cells[f"{seed_id}·{spec['field_name']}"] += 1
                    if asset in pending and _norm(cells.get(candidates.CHECK_FIELD)):
                        cand_cells[candidates.CHECK_FIELD] += 1
            else:
                out["missing_tables"].append(seed_id)
            out["delete"]["selected_rows"] += len(entry["selected"])
            for rid in entry["selected"]:
                cells = selected_records.get(rid) or {}
                for name in SELECTED_HUMAN:
                    if _norm(cells.get(name)) is not None:
                        sel_cells[name] += 1
                for name in out["user_columns"]["selected"]:
                    if _norm(cells.get(name)) is not None:
                        user_cells[f"选中的款·{name}"] += 1
    out["human_cells"] = {"candidates": dict(cand_cells), "selected": dict(sel_cells)}
    out["user_cells"] = dict(user_cells)
    return out


# ---------------------------------------------------------------- 正式跑


def run(
    ctx: Ctx,
    week: str,
    *,
    by: str,
    backup_dir: str | None = None,
    keep_columns: bool = False,
) -> dict[str, Any]:
    """正式搬（见模块说明）。持有迁移租约和选款表的同步租约；核对不过时停下来、旧表都还在，返回 problems。
    keep_columns：人自己加的列也在新表里建上、搬过去（缺省不搬）。"""
    _check_week(week)
    if not feishu_table.enabled(ctx):
        raise StageError("table.enabled 为 false：先在 podflow.yaml 打开 table.enabled")
    legacy = feishu_table.base(ctx)
    if not legacy:
        raise StageError("旧表格还没建：没有要搬的")
    if not books.template_ready(books.template(ctx)):
        raise StageError("模板表格还没建好：先 podctl table books 看一眼")
    if not lease.take(ctx, books.MIGRATE_LEASE, books.MIGRATE_SECONDS):
        raise StageError("另一个迁移正在跑")
    try:
        candidates.take_lease(ctx, busy="表格正在同步：等一两分钟再迁移")
        with feishu.Client(ctx) as api:
            return _run(ctx, api, week, legacy, by, backup_dir, keep_columns)
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
        lease.drop(ctx, books.MIGRATE_LEASE)


def _run(
    ctx: Ctx,
    api: feishu.Client,
    week: str,
    legacy: dict[str, Any],
    by: str,
    backup_dir: str | None,
    keep_columns: bool,
) -> dict[str, Any]:
    current = state(ctx, week)
    seeds = _seeds(ctx, week, current)
    if not seeds:
        raise StageError(f"{week} 没有要搬的任务")
    fresh = [s for s in seeds if s not in (current.get("seeds") or {})]
    if current.get("done_at") and not fresh:
        return {"week": week, "done": True, "already": True, "done_at": current["done_at"]}
    errors: list[dict[str, str]] = []
    if fresh:
        # 第一次跑，或上次搬完之后又有这一周的任务留在了旧表格：这几个先读回、另做一份备份
        _pre_read(ctx, api, legacy, fresh)
        current = current or {
            "week": week,
            "by": by,
            "started_at": now_iso(),
            "seeds": {},
            "keep_columns": keep_columns,
        }
        current.pop("done_at", None)
        current.pop("verified_at", None)
        added = {s: _snapshot(ctx, s, seeds[s]) for s in fresh}
        path = _backup(ctx, api, legacy, week, added, backup_dir)
        for entry in added.values():
            entry["backup"] = path
        current["seeds"].update(added)
        _save(ctx, week, current)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "table",
                "week_migration_started",
                {"week": week, "seeds": fresh, "backup": path, "by": by},
            )
    backup = _load_backups(current)
    _renew(ctx)
    # 周表格和那几天（要搬人加的列时，那几天的「选中的款」先补上这几列）
    for day in sorted({e["day"] for e in current["seeds"].values()}):
        info = books.ensure_day(ctx, api, day)
        if current.get("keep_columns"):
            _add_selected_columns(ctx, api, legacy, info, current)
    _save(ctx, week, current)
    for seed_id, entry in current["seeds"].items():
        if entry["stage"] == "planned":
            _detach(ctx, seed_id, entry)
            entry["stage"] = "detached"
            _save(ctx, week, current)
    for seed_id, entry in current["seeds"].items():
        # 重建过的也再过一遍：上次有一批没建成（飞书偶发 RpcError）的行补上，建齐了的什么都不做
        if entry["stage"] in ("detached", "rebuilt"):
            _rebuild(ctx, api, legacy, seed_id, entry, errors, bool(current.get("keep_columns")))
            entry["stage"] = "rebuilt"
            _save(ctx, week, current)
            _renew(ctx)
    synced = _sync(ctx, api, legacy, errors)
    dropped = _drop_orphans(ctx, api, legacy, current)
    if dropped:
        current["orphans_dropped"] = int(current.get("orphans_dropped") or 0) + dropped
        _save(ctx, week, current)
    expected = _copy(ctx, api, legacy, current, backup)
    current["copied_at"] = now_iso()
    _save(ctx, week, current)
    _renew(ctx)
    problems = _verify(ctx, api, legacy, current, backup, expected)
    backups = sorted({e["backup"] for e in current["seeds"].values() if e.get("backup")})
    if problems:
        current["problems"] = problems[:200]
        _save(ctx, week, current)
        return {
            "week": week,
            "verified": False,
            "problems": problems[:30],
            "problem_count": len(problems),
            "errors": errors[:10],
            "backups": backups,
            "next": "旧表都还在：看清原因、处理好后重跑同一条命令（接着做，不会重复建表）",
        }
    current["verified_at"] = now_iso()
    current.pop("problems", None)
    _save(ctx, week, current)
    deleted = _delete_old(ctx, api, legacy, week, current, errors)
    current.update(deleted=deleted, done_at=now_iso())
    _save(ctx, week, current)
    # 新行上的上架状态、没选原因读回业务库（记到新记录号上）
    listing.refresh(ctx, api)
    for seed_id in current["seeds"]:
        try:
            candidates.refresh_reasons(ctx, seed_id)
        except (StageError, *_FAILURES) as exc:
            errors.append({"seed_id": seed_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "table",
            "week_migrated",
            {"week": week, "seeds": list(current["seeds"]), "deleted": deleted, "by": by},
        )
    found = books.book(ctx, week) or {}
    return {
        "week": week,
        "verified": True,
        "book": found.get("url"),
        "days": {
            day: (books.day_info(ctx, day) or {}).get("table_id")
            for day in sorted({e["day"] for e in current["seeds"].values()})
        },
        "seeds": {s: e["day"] for s, e in current["seeds"].items()},
        "deleted": deleted,
        "synced": synced,
        "errors": errors[:10],
        "backups": backups,
    }


def _pre_read(ctx: Ctx, api: feishu.Client, legacy: dict[str, Any], seeds: list[str]) -> None:
    """搬之前先把旧表里人填的读回业务库：上架状态（「选中的款」）、没选原因（各候选表）。"""
    listing.refresh(ctx, api, legacy)
    for seed_id in seeds:
        if not candidates.table_info(ctx, seed_id):
            continue
        try:
            candidates.refresh_reasons(ctx, seed_id)
        except (StageError, *_FAILURES):
            # 旧候选表已经没了（被人删了）：没得读
            continue


def _backup(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    week: str,
    entries: dict[str, dict[str, Any]],
    backup_dir: str | None,
) -> str:
    """旧表里这几个任务的记录（整行所有列）+ 业务库相关的行，写成一个 JSON 文件；顺便记下旧候选表还在不在。"""
    app = legacy["app_token"]
    existing = set(books._tables(api, app).values())
    data: dict[str, Any] = {
        "week": week,
        "created_at": now_iso(),
        "legacy": {k: legacy.get(k) for k in ("app_token", "table_id", "url")},
        "candidates": {},
        "selected": {},
        "db": {},
    }
    for seed_id, entry in entries.items():
        tid = entry["old_table"]
        entry["old_exists"] = bool(tid and tid in existing)
        if entry["old_exists"]:
            data["candidates"][seed_id] = {
                "table_id": tid,
                "fields": _fields(api, app, tid),
                "records": _read_all(api, app, tid),
            }
    wanted = {rid for e in entries.values() for rid in e["selected"]}
    data["selected"] = {
        rid: cells
        for rid, cells in _read_all(api, app, legacy["table_id"]).items()
        if rid in wanted
    }
    data["selected_fields"] = _fields(api, app, legacy["table_id"])
    seeds = list(entries)
    marks = ",".join("?" for _ in seeds)
    for table in _SEED_TABLES:
        data["db"][table] = [
            dict(r)
            for r in ctx.db.q(f"SELECT * FROM {table} WHERE seed_id IN ({marks})", tuple(seeds))
        ]
    assets = [a for s in seeds for a in _assets(ctx, s)]
    for table in _ASSET_TABLES:
        data["db"][table] = _rows_in(ctx, table, "asset_id", assets)
    folder = Path(backup_dir) if backup_dir else ctx.home / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"migrate-{week}-{utcnow():%Y%m%d-%H%M%S}.json"
    target.write_text(dumps(data), encoding="utf-8")
    return str(target)


def _add_selected_columns(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    day: dict[str, Any],
    current: dict[str, Any],
) -> None:
    """那一天的「选中的款」照旧表补上人自己加的列（使用情况、下载情况这类）。"""
    old = _fields(api, legacy["app_token"], legacy["table_id"])
    new = _fields(api, day["app_token"], day["table_id"])
    specs = _user_specs(old, new, _selected_known(ctx))
    path = f"/bitable/v1/apps/{day['app_token']}/tables/{day['table_id']}/fields"
    for spec in specs:
        api.call("POST", path, json=spec)
    added = current.setdefault("selected_columns", {})
    added[day["day"]] = sorted({*(added.get(day["day"]) or []), *[s["field_name"] for s in specs]})


def _detach(ctx: Ctx, seed_id: str, entry: dict[str, Any]) -> None:
    """业务库里指向旧表行的记录清掉、归属改到那一天；选款结论、下载确认、上架状态这些底账不动。"""
    assets = _assets(ctx, seed_id)
    with ctx.db.tx() as conn:
        for sql in (
            "DELETE FROM candidate_tables WHERE seed_id = ?",
            "DELETE FROM candidate_rows WHERE seed_id = ?",
            "UPDATE candidate_slots SET record_id = NULL, attempts = 0, last_error = NULL WHERE seed_id = ?",
            "UPDATE candidate_actions SET record_id = NULL, state_key = NULL, attempts = 0, last_error = NULL"
            " WHERE seed_id = ?",
            "DELETE FROM candidate_scene_rows WHERE seed_id = ?",
            "DELETE FROM candidate_orders WHERE seed_id = ?",
            "DELETE FROM candidate_controls WHERE seed_id = ?",
            "UPDATE selection_reasons SET record_id = NULL WHERE seed_id = ?",
            "UPDATE scene_packages SET table_state = NULL, table_attempts = 0, table_error = NULL WHERE seed_id = ?",
            "UPDATE seed_names SET table_name = NULL, table_attempts = 0, table_error = NULL WHERE seed_id = ?",
            "UPDATE seed_shelf SET folder_state = NULL, folder_attempts = 0, folder_error = NULL WHERE seed_id = ?",
        ):
            conn.execute(sql, (seed_id,))
        for i in range(0, len(assets), _PAGE):
            chunk = tuple(assets[i : i + _PAGE])
            marks = ",".join("?" for _ in chunk)
            for sql in (
                f"DELETE FROM candidate_codes WHERE asset_id IN ({marks})",
                f"DELETE FROM table_rows WHERE asset_id IN ({marks})",
                f"DELETE FROM selected_rows WHERE asset_id IN ({marks})",
                f"DELETE FROM temu_rows WHERE asset_id IN ({marks})",
                f"UPDATE listing_marks SET record_id = NULL WHERE asset_id IN ({marks})",
                f"UPDATE scene_sets SET table_state = NULL, table_attempts = 0, table_error = NULL"
                f" WHERE asset_id IN ({marks})",
            ):
                conn.execute(sql, chunk)
        conn.execute(
            "INSERT INTO seed_places(seed_id, day, placed_at) VALUES(?, ?, ?)"
            " ON CONFLICT(seed_id) DO UPDATE SET day = excluded.day, placed_at = excluded.placed_at",
            (seed_id, entry["day"], now_iso()),
        )
        log_event(
            conn,
            seed_id,
            "week_migration_detached",
            {"day": entry["day"], "old_table": entry["old_table"]},
        )


def _rebuild(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    seed_id: str,
    entry: dict[str, Any],
    errors: list[dict[str, str]],
    keep_columns: bool,
) -> None:
    """新候选表（从周表格的模板复制进那天的文件夹）+ 旧表里有的行照业务库重写 + 预留行重建。"""
    table = candidates._ensure_table(ctx, api, legacy, seed_id)
    table_id = table["table_id"]
    stash = {k: v for k, v in (entry.get("stash") or {}).items() if v is not None}
    with ctx.db.tx() as conn:
        if stash:
            sets = ", ".join(f"{k} = ?" for k in stash)
            conn.execute(
                f"UPDATE candidate_tables SET {sets} WHERE seed_id = ?", (*stash.values(), seed_id)
            )
        conn.execute(
            "UPDATE candidate_actions SET table_id = ? WHERE seed_id = ?", (table_id, seed_id)
        )
    if keep_columns and entry.get("old_exists") and entry["old_table"]:
        app = table_registry.app_for(legacy, table_id)
        old = _fields(api, legacy["app_token"], entry["old_table"])
        new = _fields(api, app, table_id)
        specs = _user_specs(old, new, _candidate_known(ctx))
        for spec in specs:
            api.call("POST", f"/bitable/v1/apps/{app}/tables/{table_id}/fields", json=spec)
        entry["user_columns"] = [s["field_name"] for s in specs]
    have = {
        r["asset_id"]
        for r in ctx.db.q(
            "SELECT asset_id FROM candidate_rows WHERE seed_id = ? AND record_id IS NOT NULL",
            (seed_id,),
        )
    }
    wanted = set(entry["rows"].values()) - have
    items = [
        dict(r)
        for r in ctx.db.q(
            "SELECT a.asset_id, f.seed_id FROM assets a JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " WHERE f.seed_id = ? ORDER BY v.batch_id, a.variant_id, a.asset_id",
            (seed_id,),
        )
        if r["asset_id"] in wanted
    ]
    candidates._add_rows(ctx, api, legacy, errors, items)
    _rebuild_slots(ctx, api, legacy, seed_id, table_id, entry)
    with ctx.db.tx() as conn:
        # 补上过图的槽位记着那一行（新记录号）
        conn.execute(
            "UPDATE candidate_slots SET record_id = (SELECT c.record_id FROM candidate_rows c"
            " WHERE c.asset_id = candidate_slots.asset_id) WHERE seed_id = ? AND asset_id IS NOT NULL",
            (seed_id,),
        )


def _rebuild_slots(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    seed_id: str,
    table_id: str,
    entry: dict[str, Any],
) -> int:
    """旧表里的预留行（没出成 / 未生成，还没补上图）照原样建回来。"""
    wanted = {(job, slot): slot_state for job, slot, slot_state in entry["slots"].values()}
    if not wanted:
        return 0
    taken = {
        (r["job_id"], r["slot_id"])
        for r in ctx.db.q(
            "SELECT job_id, slot_id FROM candidate_slots WHERE seed_id = ?"
            " AND (record_id IS NOT NULL OR asset_id IS NOT NULL)",
            (seed_id,),
        )
    }
    keys = [k for k in wanted if k not in taken]
    details = candidates._slot_details(ctx, keys)
    keys = [k for k in keys if k in details]
    if not keys:
        return 0
    records = [candidates._filled(candidates._slot_fields(details[k], wanted[k])) for k in keys]
    ids = candidates._batch_create(api, legacy, table_id, records)
    for (job_id, slot_id), record_id in zip(keys, ids):
        candidates._save_slot(
            ctx,
            job_id,
            slot_id,
            seed_id,
            record_id=record_id,
            state=wanted[(job_id, slot_id)],
            attempts=0,
            last_error=None,
        )
    return len(ids)


def _sync(
    ctx: Ctx, api: feishu.Client, legacy: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """持有租约跑表格同步（选款结论、链接、表名、表顶行、套图、排序；「选中的款」进那天的那张），没事做了或三遍为止。"""
    out: dict[str, Any] = {}
    for _ in range(3):
        out["candidates"] = candidates._sync_locked(ctx, api, legacy, errors)
        out["selected"] = feishu_table.sync_locked(ctx, api, errors)
        _renew(ctx)
        if not (candidates.has_work(ctx) or feishu_table._has_work(ctx)):
            break
    return out


def _known_records(ctx: Ctx, seed_id: str) -> set[str]:
    """业务库里记着的这个任务候选表的行：图、预留行、表顶的按钮 / 交付包行、套图行。"""
    out: set[str] = set()
    for table in ("candidate_rows", "candidate_slots", "candidate_actions", "candidate_scene_rows"):
        out |= {
            r["record_id"]
            for r in ctx.db.q(
                f"SELECT record_id FROM {table} WHERE seed_id = ? AND record_id IS NOT NULL", (seed_id,)
            )
        }
    return out


def _drop_orphans(
    ctx: Ctx, api: feishu.Client, legacy: dict[str, Any], current: dict[str, Any]
) -> int:
    """新候选表里业务库不认识的行删掉：一批建行报错（或进程被停）时飞书那边可能已经建了，重建时又建了一份。
    新表是这次迁移刚建的，人还没动过。返回删了几行。"""
    dropped = 0
    for seed_id in current["seeds"]:
        table = candidates.table_info(ctx, seed_id)
        if not table:
            continue
        app = table_registry.app_for(legacy, table["table_id"])
        orphans = sorted(set(_read_all(api, app, table["table_id"])) - _known_records(ctx, seed_id))
        if orphans:
            candidates._delete_records(api, {"app_token": app}, table["table_id"], orphans)
            dropped += len(orphans)
    return dropped


def _copy(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    current: dict[str, Any],
    backup: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    """人填的格子搬到新行上（重跑时照样整份再写一遍）。返回该有的值：{table_id: {record_id: {列: 值}}}，核对用。"""
    expected: dict[str, dict[str, dict[str, Any]]] = {}
    for seed_id, entry in current["seeds"].items():
        table = candidates.table_info(ctx, seed_id)
        if not table:
            continue
        table_id = table["table_id"]
        app = table_registry.app_for(legacy, table_id)
        old = (backup.get("candidates", {}).get(seed_id) or {}).get("records") or {}
        new_fields = _fields(api, app, table_id)
        names = [*CANDIDATE_HUMAN, *(entry.get("user_columns") or [])]
        rows = {
            r["asset_id"]: r["record_id"]
            for r in ctx.db.q(
                "SELECT asset_id, record_id FROM candidate_rows WHERE seed_id = ? AND record_id IS NOT NULL",
                (seed_id,),
            )
        }
        slots = {
            (r["job_id"], r["slot_id"]): r["record_id"]
            for r in ctx.db.q(
                "SELECT job_id, slot_id, record_id FROM candidate_slots"
                " WHERE seed_id = ? AND record_id IS NOT NULL AND asset_id IS NULL",
                (seed_id,),
            )
        }
        pending = _open_assets(ctx, list(entry["rows"].values()))
        todo: list[tuple[str | None, dict[str, Any] | None, list[str]]] = []
        for old_rid, asset_id in entry["rows"].items():
            wanted = names + ([candidates.CHECK_FIELD] if asset_id in pending else [])
            todo.append((rows.get(asset_id), old.get(old_rid), wanted))
        for old_rid, (job_id, slot_id, _) in entry["slots"].items():
            todo.append(
                (
                    slots.get((job_id, slot_id)),
                    old.get(old_rid),
                    [candidates.CHECK_FIELD, candidates.REASON_FIELD],
                )
            )
        # 按钮行的备注（点了以后写的）从业务库写回
        for r in ctx.db.q(
            "SELECT kind, record_id, note FROM candidate_actions WHERE seed_id = ? AND record_id IS NOT NULL",
            (seed_id,),
        ):
            if r["kind"] in (candidates.KIND_DISPATCH, candidates.KIND_ACCEPT) and r["note"]:
                todo.append((r["record_id"], {candidates.NOTE_FIELD: r["note"]}, [candidates.NOTE_FIELD]))
        updates: list[tuple[str, dict[str, Any]]] = []
        for record_id, cells, wanted in todo:
            if not record_id or cells is None:
                continue
            write = _write_cells(new_fields, cells, wanted)
            if write:
                updates.append((record_id, write))
                expected.setdefault(table_id, {})[record_id] = {k: _norm(v) for k, v in write.items()}
        if updates:
            _batch_update(api, app, table_id, updates)
        _renew(ctx)
    # 「选中的款」：按素材对上那一天新表里的行
    old_selected = backup.get("selected") or {}
    by_day: dict[str, list[tuple[str, str]]] = {}
    for entry in current["seeds"].values():
        for old_rid, asset_id in entry["selected"].items():
            by_day.setdefault(entry["day"], []).append((old_rid, asset_id))
    for day, pairs in sorted(by_day.items()):
        info = books.day_info(ctx, day) or {}
        if not info.get("table_id"):
            continue
        new_fields = _fields(api, info["app_token"], info["table_id"])
        names = [*SELECTED_HUMAN, *((current.get("selected_columns") or {}).get(day) or [])]
        records = {
            r["asset_id"]: r["record_id"]
            for r in _rows_in(ctx, "table_rows", "asset_id", [a for _, a in pairs])
            if r["record_id"]
        }
        updates = []
        for old_rid, asset_id in pairs:
            cells = old_selected.get(old_rid)
            record_id = records.get(asset_id)
            if cells is None or not record_id:
                continue
            write = _write_cells(new_fields, cells, names)
            if write:
                updates.append((record_id, write))
                expected.setdefault(info["table_id"], {})[record_id] = {
                    k: _norm(v) for k, v in write.items()
                }
        if updates:
            _batch_update(api, info["app_token"], info["table_id"], updates)
        _renew(ctx)
    return expected


def _verify(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    current: dict[str, Any],
    backup: dict[str, Any],
    expected: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    """逐格核对：旧表每一行都有新行；搬过去的格子、选款结论、下载确认新旧一致。返回不一致的地方。"""
    problems: list[str] = []
    read: dict[str, dict[str, dict[str, Any]]] = {}
    for seed_id, entry in current["seeds"].items():
        table = candidates.table_info(ctx, seed_id)
        if not table:
            problems.append(f"{seed_id}：新候选表没建成")
            continue
        table_id = table["table_id"]
        records = read[table_id] = _read_all(
            api, table_registry.app_for(legacy, table_id), table_id
        )
        old = (backup.get("candidates", {}).get(seed_id) or {}).get("records") or {}
        rows = {
            r["asset_id"]: r["record_id"]
            for r in ctx.db.q(
                "SELECT asset_id, record_id FROM candidate_rows WHERE seed_id = ? AND record_id IS NOT NULL",
                (seed_id,),
            )
        }
        for old_rid, asset_id in entry["rows"].items():
            record_id = rows.get(asset_id)
            if not record_id or record_id not in records:
                problems.append(f"{seed_id}：{asset_id} 在新候选表里没有行")
                continue
            if old_rid not in old:
                continue
            for name in CANDIDATE_CHECKED:
                was, now = _norm(old[old_rid].get(name)), _norm(records[record_id].get(name))
                if was != now:
                    problems.append(f"{seed_id}：{asset_id} 的「{name}」旧 {was} / 新 {now}")
        slots = {
            (r["job_id"], r["slot_id"]): r["record_id"]
            for r in ctx.db.q(
                "SELECT job_id, slot_id, record_id FROM candidate_slots"
                " WHERE seed_id = ? AND record_id IS NOT NULL AND asset_id IS NULL",
                (seed_id,),
            )
        }
        filled = {
            (r["job_id"], r["slot_id"])
            for r in ctx.db.q(
                "SELECT job_id, slot_id FROM candidate_slots WHERE seed_id = ? AND asset_id IS NOT NULL",
                (seed_id,),
            )
        }
        for job_id, slot_id, _ in entry["slots"].values():
            key = (job_id, slot_id)
            if key in filled:
                continue
            if slots.get(key) not in records:
                problems.append(f"{seed_id}：预留行 {job_id}/{slot_id} 在新候选表里没有")
    for entry in current["seeds"].values():
        info = books.day_info(ctx, entry["day"]) or {}
        if not info.get("table_id"):
            problems.append(f"{entry['day']}：那天的「选中的款」没建成")
            continue
        if info["table_id"] not in read:
            read[info["table_id"]] = _read_all(api, info["app_token"], info["table_id"])
        records = read[info["table_id"]]
        new = {
            r["asset_id"]: r["record_id"]
            for r in _rows_in(ctx, "table_rows", "asset_id", list(entry["selected"].values()))
            if r["record_id"]
        }
        for old_rid, asset_id in entry["selected"].items():
            record_id = new.get(asset_id)
            if not record_id or record_id not in records:
                problems.append(f"{entry['day']}：{asset_id} 在那天的「选中的款」里没有行")
                continue
            cells = (backup.get("selected") or {}).get(old_rid)
            if cells is None:
                continue
            for name in SELECTED_CHECKED:
                was, now = _norm(cells.get(name)), _norm(records[record_id].get(name))
                if was != now:
                    problems.append(f"{entry['day']}：{asset_id} 的「{name}」旧 {was} / 新 {now}")
    for table_id, cells_by_record in expected.items():
        records = read.get(table_id) or {}
        for record_id, cells in cells_by_record.items():
            got = records.get(record_id)
            if got is None:
                problems.append(f"{table_id}：{record_id} 不见了")
                continue
            for name, want in cells.items():
                now = _norm(got.get(name))
                if now != want:
                    problems.append(f"{table_id}/{record_id} 的「{name}」该是 {want}，现在是 {now}")
    return problems


def _delete_old(
    ctx: Ctx,
    api: feishu.Client,
    legacy: dict[str, Any],
    week: str,
    current: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """删掉旧表格里这几个任务的候选表、「选中的款」里它们的行（删过的记下来，重跑不再删）。"""
    app = legacy["app_token"]
    tables = []
    for seed_id, entry in current["seeds"].items():
        table_id = entry.get("old_table")
        if not table_id or not entry.get("old_exists") or entry.get("old_deleted"):
            continue
        try:
            api.call("DELETE", f"/bitable/v1/apps/{app}/tables/{table_id}")
        except feishu.FeishuError as exc:
            errors.append({"seed_id": seed_id, "error": f"删旧候选表没成：{exc}"[:300]})
            continue
        entry["old_deleted"] = True
        tables.append(table_id)
        _save(ctx, week, current)
    done = set(current.get("selected_deleted") or [])
    wanted = sorted({rid for e in current["seeds"].values() for rid in e["selected"]} - done)
    path = f"/bitable/v1/apps/{app}/tables/{legacy['table_id']}/records"
    for i in range(0, len(wanted), _PAGE):
        chunk = wanted[i : i + _PAGE]
        try:
            api.call("POST", f"{path}/batch_delete", json={"records": chunk})
            done |= set(chunk)
        except feishu.FeishuError:
            # 有的行已经被人删了时整批会失败：逐行删，删不掉的跳过
            for record_id in chunk:
                try:
                    api.call("DELETE", f"{path}/{record_id}")
                    done.add(record_id)
                except feishu.FeishuError as exc:
                    errors.append({"record_id": record_id, "error": f"删旧行没成：{exc}"[:300]})
        current["selected_deleted"] = sorted(done)
        _save(ctx, week, current)
    return {"tables": tables, "selected_rows": len(done)}
