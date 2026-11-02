"""候选表的店铺列（2026-10-28，开着 temu.enabled 时）：每家店一列勾选框，勾哪列就上哪家店，勾了任意一列就算选中；
旁边的「货号」由程序写（选完店铺就向 mooya 领号）。

- 列名是店铺的「label name」（如「3店 CozWeave H」）。新建的候选表建表时就带着（放在「选中」后面），之前建的表在同步时补上
  （加在最后，默认视图里按 layout() 摆到前面），补好的记在 meta candidates.stores：{"done": {table_id: [列名…]},
  "layout": {table_id: [列名…]}, "attempts": {table_id: 次数}, "error": …}。
- 「选中」列照旧在（聊天卡片里定的、不上这几家店的款还用得上），开着店铺列时默认视图里藏起来；按钮行勾任意一个勾选框都算点了按钮。
- 验收 / 提前出套图回读勾选时一起读店铺列：勾了任意一列的行算勾上；定为选中后记下勾的店铺（stores.choose，只加不减），
  当场领货号。
- 已经选中的款（2026-10-29）：在它的原图行或表顶的「套图 · IMG…」行勾店铺列，记录变更事件一到就记店铺、领货号、
  拉起上架准备（apply_ticks，不用再点按钮；巡检每 POLL_MINUTES 分钟回读一次兜底）。最近一套套图出完了、图位校验却过不了
  （10-26 前的老格式）的不收：取消这一行的店铺勾、备注写原因。
- 选中的行和它的套图行：店铺列和「货号」照店铺记录写（只写变了的，candidate_codes / candidate_scene_codes 记着上次写的）；
  没定店铺、也没写过的行不碰（可能是人刚勾的）。没选中的行在 candidates._sync_states 里连「选中」一起取消。
- 全自动（2026-10-30，`temu.auto.enabled`）：还没选的行勾了开着全自动的店，不用点按钮——等 `temu.auto.tick_grace_minutes`
  （缺省 5 分钟，犹豫期；记在 store_tick_pending）回读仍勾着，就定为选中（确认人是勾的人）、记店铺、领货号、交 mooya 出套图、
  由程序确认上架；勾的只有没开全自动的店时等「选款完成」。已经选中的款勾店铺同上（当场处理，没出过套图的也交出去）。
  犹豫期里点了「选款完成」的，按点的那一刻勾没勾定下来，撤销窗口里的记录跟着删掉（forget_pending）。
  处理完以后人取消店铺勾（最后修改人是人）：这家店还没开始建箱就去掉（货号不回收），已经建箱 / 上了的把勾写回去。
- 巡检（每 POLL_MINUTES 分钟）整张读开着的候选表：补漏掉的勾、处理到点的撤销窗口、认出取消的勾，人改的格子记日志（cells.py）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from . import bg, feishu, feishu_table, lease, store, stores, table_registry, trace
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, iso, loads, now_iso, parse_iso, utcnow

META_KEY = "candidates.stores"
CODE_FIELD = "货号"
STORE_HELP = (
    "要上这家店就在这一列打勾（可以勾几家）。还没选的款：勾了 5 分钟后自动定为选中，按勾的店铺领货号、出套图、上架，"
    "5 分钟内取消勾算撤销（没开全自动的店要等点「选款完成」）。已经选中的款（这一行或表顶它的「套图」行）：勾了当场领货号、"
    "上这家店，还没开始建箱时取消勾就去掉这家店。10-26 前的老格式套图不能自动上架，勾了会被取消。"
)
# 已经选中的款勾店铺：巡检多久回读一次兜底（事件漏掉的勾）
POLL_MINUTES = 30
TICKS_META = "candidates.store_ticks"
# 全自动时勾店铺定为选中写的备注（review 的 note）
AUTO_TICK_NOTE = "候选表里勾了店铺（全自动上架）"
# 撤销窗口里的勾：后台 `table ticks` 最多守这么久（窗口一般 5 分钟）
TICKS_WATCH_MINUTES = 20
OLD_FORMAT_NOTE = "这套套图是 10-26 前的老格式（没有交付清单，不是点缀版 10 张），不能自动上架"
CODE_HELP = "程序写：选完店铺后 mooya 按店铺前缀续的货号（如 FLow0006 / Deer0006），也是套图交付包的名字。"
# 按钮行的说明里「选中」换成这个（开着店铺列时「选中」藏起来了）
ANY_CHECK = "任意一个勾选框"
_PAGE = 500
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)


def enabled(ctx: Ctx) -> bool:
    return stores.enabled(ctx)


def column_map(ctx: Ctx) -> dict[str, stores.Store]:
    """列名 → 店铺（关着时为空）。"""
    return stores.column_stores(ctx) if enabled(ctx) else {}


def specs(ctx: Ctx) -> list[dict[str, Any]]:
    """店铺列和「货号」列（关着时为空）。"""
    cols = list(column_map(ctx))
    if not cols:
        return []
    return [
        *(
            {"field_name": col, "type": 7, "description": {"disable_sync": True, "text": STORE_HELP}}
            for col in cols
        ),
        {"field_name": CODE_FIELD, "type": 1, "description": {"disable_sync": True, "text": CODE_HELP}},
    ]


def _meta(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(META_KEY), None) or {}


def _save_meta(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, META_KEY, dumps(meta))


def mark_done(ctx: Ctx, table_id: str, names: list[str]) -> None:
    """这张候选表已经有这几列（建表时带着，或补上了）。"""
    meta = _meta(ctx)
    done = meta.setdefault("done", {})
    done[table_id] = sorted({*(done.get(table_id) or []), *names})
    attempts = meta.get("attempts") or {}
    attempts.pop(table_id, None)
    meta["attempts"] = attempts
    if not attempts:
        meta.pop("error", None)
    _save_meta(ctx, meta)


def present(ctx: Ctx, table_id: str) -> list[str]:
    """这张表里已经有的店铺列（按店铺配置的先后）；关着时为空。"""
    have = set((_meta(ctx).get("done") or {}).get(table_id) or [])
    return [c for c in column_map(ctx) if c in have]


def has_code_column(ctx: Ctx, table_id: str) -> bool:
    return enabled(ctx) and CODE_FIELD in set((_meta(ctx).get("done") or {}).get(table_id) or [])


def pending_tables(ctx: Ctx) -> list[dict[str, str]]:
    """开着店铺列时还缺店铺列 / 货号列的候选表（失败满上限的不再试）：[{seed_id, table_id}]。"""
    names = {s["field_name"] for s in specs(ctx)}
    if not names:
        return []
    meta = _meta(ctx)
    done = meta.get("done") or {}
    attempts = meta.get("attempts") or {}
    return [
        {"seed_id": r["seed_id"], "table_id": r["table_id"]}
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id")
        if not names <= set(done.get(r["table_id"]) or [])
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    ]


def sync_columns(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """给之前建的候选表补上店铺列和「货号」（加在最后）；失败记次数，满上限不再试。返回补了几张。"""
    from . import views  # 视图模块依赖表格模块，放到用时再导入

    fixed = 0
    wanted = specs(ctx)
    for item in pending_tables(ctx):
        table_id = item["table_id"]
        path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/fields"
        try:
            names = {f.get("field_name") for f in views.list_fields(api, info, table_id)}
            added = [s["field_name"] for s in wanted if s["field_name"] not in names]
            for spec in wanted:
                if spec["field_name"] in added:
                    api.call("POST", path, json=spec)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:300]
            errors.append({"seed_id": item["seed_id"], "error": reason})
            meta = _meta(ctx)
            attempts = meta.setdefault("attempts", {})
            attempts[table_id] = int(attempts.get(table_id, 0)) + 1
            meta["error"] = f"{item['seed_id']}：{reason}"
            _save_meta(ctx, meta)
            continue
        mark_done(ctx, table_id, [s["field_name"] for s in wanted])
        with ctx.db.tx() as conn:
            log_event(conn, item["seed_id"], "candidate_store_columns_added", {"table_id": table_id, "added": added})
        fixed += 1
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return fixed


# ---------------------------------------------------------------- 视图与按钮行


def layout(ctx: Ctx, table_id: str, base: Any, *, check_field: str) -> Any:
    """这张表默认视图的列先后：有店铺列时店铺列和「货号」排在缩略图后面，「选中」藏起来；三个画册的卡片放店铺列
    （画册「全部画册」「已选中」再放「货号」）。"""
    from . import views  # 同上

    cols = present(ctx, table_id)
    if not cols:
        return base
    code = (CODE_FIELD,) if has_code_column(ctx, table_id) else ()
    front = tuple(n for n in base.front if n != check_field)

    def cards(names: tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(n for n in names or base.gallery or () if n != check_field)

    gallery, picked, whole = cards(base.gallery), cards(base.picked), cards(base.whole)
    return views.Layout(
        front=(*front[:2], *cols, *code, *front[2:]),
        known=frozenset({*base.known, *cols, CODE_FIELD}),
        hidden=(*base.hidden, check_field),
        gallery=(*gallery[:1], *cols, *gallery[1:]),
        cover=base.cover,
        picked=(*picked[:1], *cols, *code, *picked[1:]),
        whole=(*whole[:1], *cols, *code, *whole[1:]),
    )


def layout_pending(ctx: Ctx, table_id: str) -> bool:
    """有店铺列、默认视图还没按店铺列摆过（或店铺变了）。"""
    cols = present(ctx, table_id)
    return bool(cols) and (_meta(ctx).get("layout") or {}).get(table_id) != cols


def layout_done(ctx: Ctx, table_id: str) -> None:
    meta = _meta(ctx)
    meta.setdefault("layout", {})[table_id] = present(ctx, table_id)
    _save_meta(ctx, meta)


def control_text(ctx: Ctx, text: str) -> str:
    """按钮行 / 交付包行的文字：开着店铺列时「选中」藏起来了，改成勾任意一个勾选框。"""
    if not column_map(ctx):
        return text
    return text.replace("「选中」", ANY_CHECK)


def checked(ctx: Ctx, table_id: str, values: dict[str, Any], check_field: str) -> bool:
    """一行的「选中」或任意一个店铺列勾着。"""
    from .downloads import _ticked

    return any(_ticked(values.get(name)) for name in (check_field, *present(ctx, table_id)))


def cleared(ctx: Ctx, table_id: str, check_field: str) -> dict[str, bool]:
    """把「选中」和这张表的店铺列都取消的格子。"""
    return {name: False for name in (check_field, *present(ctx, table_id))}


def picks(ctx: Ctx, table_id: str, values: dict[str, Any]) -> list[str]:
    """一行勾了哪几家店（店铺 key，按配置先后）。"""
    from .downloads import _ticked

    by_column = column_map(ctx)
    return [by_column[name].key for name in present(ctx, table_id) if _ticked(values.get(name))]


# ---------------------------------------------------------------- 选店铺、领货号


def apply(ctx: Ctx, seed_id: str, picked: dict[str, list[str]], *, by: str) -> dict[str, Any]:
    """验收 / 提前出套图之后：这个任务里选中的行勾了哪几家店就记下（只加不减），再领货号。
    picked = {record_id: [店铺 key]}。返回 {chosen: {asset_id: [新加的店铺]}, codes}。"""
    if not enabled(ctx) or not picked:
        return {}
    rows = ctx.db.q(
        "SELECT c.asset_id, c.record_id FROM candidate_rows c JOIN assets a ON a.asset_id = c.asset_id"
        " WHERE c.seed_id = ? AND c.record_id IS NOT NULL AND a.human_decision = 'approved'",
        (seed_id,),
    )
    chosen: dict[str, list[str]] = {}
    for row in rows:
        keys = picked.get(row["record_id"]) or []
        if not keys:
            continue
        added = stores.choose(ctx, row["asset_id"], keys, by=by, source="table")
        if added:
            chosen[row["asset_id"]] = added
    out: dict[str, Any] = {"chosen": chosen}
    if chosen:
        out["codes"] = stores.allocate_codes(ctx, asset_ids=list(chosen))
    return out


# ---------------------------------------------------------------- 选中行的店铺列与货号


def _cells(ctx: Ctx, table_id: str, asset_id: str, keys: set[str]) -> dict[str, Any]:
    by_column = column_map(ctx)
    cells: dict[str, Any] = {name: by_column[name].key in keys for name in present(ctx, table_id)}
    if has_code_column(ctx, table_id):
        cells[CODE_FIELD] = stores.codes_text(ctx, asset_id) if keys else ""
    return cells


# 店铺列 / 货号照店铺记录写的两种行：原图行和表顶的「套图 · IMG…」行，各自记着上次写的值
_MIRRORS = (("candidate_rows", "candidate_codes"), ("candidate_scene_rows", "candidate_scene_codes"))


def _changes(ctx: Ctx) -> dict[str, list[dict[str, Any]]]:
    """选中的款的原图行和套图行里，店铺列 / 货号和该有的不一样的：
    {table_id: [{asset_id, seed_id, record_id, cells, value, cache}]}（cache 是记上次写的那张表）。"""
    if not enabled(ctx):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for source, cache_table in _MIRRORS:
        rows = ctx.db.q(
            f"SELECT c.asset_id, c.seed_id, c.record_id, t.table_id FROM {source} c"
            " JOIN candidate_tables t ON t.seed_id = c.seed_id"
            " JOIN assets a ON a.asset_id = c.asset_id"
            " WHERE c.record_id IS NOT NULL AND a.human_decision = 'approved'"
        )
        groups = stores.by_asset(ctx, [r["asset_id"] for r in rows])
        cache = {r["asset_id"]: dict(r) for r in ctx.db.q(f"SELECT * FROM {cache_table}")}
        for row in rows:
            if not present(ctx, row["table_id"]):
                continue
            keys = {r["store_key"] for r in groups.get(row["asset_id"]) or []}
            old = cache.get(row["asset_id"]) or {}
            same = old.get("record_id") == row["record_id"]
            if not keys and not same:
                # 没定店铺、也没写过：不碰（可能是人刚勾的，事件或下次点按钮时读）
                continue
            cells = _cells(ctx, row["table_id"], row["asset_id"], keys)
            value = dumps(cells)
            if same and old.get("value") == value:
                continue
            out.setdefault(row["table_id"], []).append(
                {**dict(row), "cells": cells, "value": value, "cache": cache_table}
            )
    return out


def has_work(ctx: Ctx) -> bool:
    return bool(pending_tables(ctx)) or bool(_changes(ctx))


def sync_cells(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """选中的款的原图行和套图行：店铺列和「货号」写成店铺记录的样子（调用方持有选款表的同步租约）。返回写了几行。"""
    written = 0
    for table_id, items in _changes(ctx).items():
        path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/records"
        done: list[dict[str, Any]] = []
        for i in range(0, len(items), _PAGE):
            chunk = items[i : i + _PAGE]
            try:
                api.call(
                    "POST",
                    f"{path}/batch_update",
                    params={"user_id_type": "open_id"},
                    json={"records": [{"record_id": c["record_id"], "fields": c["cells"]} for c in chunk]},
                )
                done += chunk
            except _FAILURES:
                for change in chunk:
                    try:
                        api.call(
                            "PUT",
                            f"{path}/{change['record_id']}",
                            params={"user_id_type": "open_id"},
                            json={"fields": change["cells"]},
                        )
                        done.append(change)
                    except _FAILURES as exc:
                        errors.append({"asset_id": change["asset_id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
        now = now_iso()
        with ctx.db.tx() as conn:
            for change in done:
                conn.execute(
                    f"INSERT INTO {change['cache']}(asset_id, record_id, value, updated_at) VALUES(?, ?, ?, ?)"
                    " ON CONFLICT(asset_id) DO UPDATE SET record_id = excluded.record_id, value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (change["asset_id"], change["record_id"], change["value"], now),
                )
        written += len(done)
    return written


# ---------------------------------------------------------------- 已经选中的款：表里勾店铺当场生效

_BATCH_GET = 100


def auto_on(ctx: Ctx) -> bool:
    """开着全自动（至少一家店）：还没选的行勾店铺也当场生效。"""
    from . import temu  # 上架模块依赖本模块，用时再导入

    return temu.auto_on(ctx)


def grace_minutes(ctx: Ctx) -> float:
    try:
        return max(0.0, float(ctx.setting("temu.auto.tick_grace_minutes", 5) or 0))
    except (TypeError, ValueError):
        return 5.0


def tick_targets(ctx: Ctx, table_id: str, record_ids: list[str]) -> list[dict[str, str]]:
    """这几条记录里，哪些是这张候选表上已经选中的款的原图行或套图行，以及（开着全自动时）还没选、在选款里的原图行：
    [{asset_id, record_id, row}]（row 是 design / scene / pending）。关着店铺列、这张表还没有店铺列时为空。"""
    ids = list(dict.fromkeys(r for r in record_ids if r))
    if not ids or not present(ctx, table_id):
        return []
    marks = ",".join("?" * len(ids))
    out: list[dict[str, str]] = []
    for source, kind in (("candidate_rows", "design"), ("candidate_scene_rows", "scene")):
        for r in ctx.db.q(
            f"SELECT c.asset_id, c.record_id FROM {source} c"
            " JOIN candidate_tables t ON t.seed_id = c.seed_id"
            " JOIN assets a ON a.asset_id = c.asset_id"
            f" WHERE t.table_id = ? AND a.human_decision = 'approved' AND c.record_id IN ({marks})",
            (table_id, *ids),
        ):
            out.append({"asset_id": r["asset_id"], "record_id": r["record_id"], "row": kind})
    if auto_on(ctx):
        for r in ctx.db.q(
            "SELECT c.asset_id, c.record_id FROM candidate_rows c"
            " JOIN candidate_tables t ON t.seed_id = c.seed_id"
            " JOIN reviews v ON v.subject_type = 'asset_selection' AND v.subject_id = c.asset_id AND v.status = 'open'"
            f" WHERE t.table_id = ? AND c.record_id IN ({marks})",
            (table_id, *ids),
        ):
            out.append({"asset_id": r["asset_id"], "record_id": r["record_id"], "row": "pending"})
    return out


def _read_rows(
    api: feishu.Client, info: dict[str, Any], table_id: str, record_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """回读几行：{record_id: {"fields", "by"}}（by 是最后修改人 feishu:ou_…）；表里已经没有的行不在结果里。"""
    path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/records/batch_get"
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(record_ids), _BATCH_GET):
        data = api.call(
            "POST",
            path,
            json={
                "record_ids": record_ids[i : i + _BATCH_GET],
                "user_id_type": "open_id",
                "automatic_fields": True,
            },
        )
        for item in data.get("records") or []:
            modifier = (item.get("last_modified_by") or {}).get("id")
            out[item["record_id"]] = {
                "fields": item.get("fields") or {},
                "by": f"feishu:{modifier}"
                if isinstance(modifier, str) and modifier.startswith("ou_")
                else None,
            }
    return out


def _refusal(ctx: Ctx, asset_id: str) -> str | None:
    """这一款为什么不能在表里勾店铺：最近一套套图出完了，图位却对不上（老格式）；能勾为 None。
    套图还没出完、在等人工、没出成的照样收（记店铺、领号，上架备注写在等什么）。"""
    from . import scenes, temu_assets  # 都依赖本模块之外的表，用时再导入

    row = scenes.latest_sets(ctx, [asset_id]).get(asset_id)
    if row is None or row["status"] not in scenes.OK_FINAL:
        return None
    problems = temu_assets.slot_check(ctx, row["set_id"])
    if not problems:
        return None
    if any("交付清单是 0 张" in p for p in problems):
        return OLD_FORMAT_NOTE
    return f"套图图位对不上（{problems[0]}），不能自动上架"


def _enqueue_scenes(ctx: Ctx, asset_ids: list[str], *, by: str) -> dict[str, Any]:
    """定了店铺的款里还没出过套图的交给 mooya（出过、在出的跳过；没出成的不在这里重出，交给套图自愈）。"""
    from . import scenes  # 套图模块依赖本模块之外的很多表，用时再导入

    if not scenes.enabled(ctx) or not asset_ids:
        return {"enabled": scenes.enabled(ctx), "queued": 0}
    try:
        got = scenes.enqueue(ctx, by=by, asset_ids=asset_ids, retry_failed=False)
    except StageError as exc:
        return {"error": str(exc)[:300]}
    task = scenes.kick(ctx) if got["queued"] else None
    return {
        "queued": len(got["queued"]),
        "skipped": len(got["skipped"]),
        "sync_task": (task or {}).get("task_id"),
    }


def _apply(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    picked: dict[str, dict[str, Any]],
    *,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """picked = {asset_id: {"keys": {店铺 key}, "records": [record_id], "by": 人}}：不能上的取消勾写备注，
    能上的记店铺（只加不减）、领货号、还没出过套图的交给 mooya、拉起上架准备（全自动的店由程序确认上架），
    再把两行和「选中的款」写成店铺记录的样子。"""
    from . import temu  # 上架模块依赖本模块，用时再导入
    from .candidates import NOTE_FIELD

    out: dict[str, Any] = {"chosen": {}, "refused": {}}
    path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/records"
    for asset_id, item in picked.items():
        reason = _refusal(ctx, asset_id)
        if reason:
            for record_id in item["records"]:
                try:
                    api.call(
                        "PUT",
                        f"{path}/{record_id}",
                        params={"user_id_type": "open_id"},
                        json={"fields": {**{c: False for c in present(ctx, table_id)}, NOTE_FIELD: reason}},
                    )
                except _FAILURES as exc:
                    errors.append({"asset_id": asset_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
            out["refused"][asset_id] = reason
            with ctx.db.tx() as conn:
                log_event(conn, asset_id, "store_tick_refused", {"by": item["by"], "reason": reason})
                trace.record(conn, asset_id, "tick", to="refused", actor=item["by"], source="table", reason=reason)
            continue
        added = stores.choose(ctx, asset_id, sorted(item["keys"]), by=item["by"], source="table")
        if added:
            out["chosen"][asset_id] = added
    if out["chosen"]:
        ids = list(out["chosen"])
        out["codes"] = stores.allocate_codes(ctx, asset_ids=ids)
        by = next(iter(picked.values()))["by"]
        out["scenes"] = _enqueue_scenes(ctx, ids, by=by)
        confirmed = temu.auto_confirm(ctx, ids)
        if confirmed.get("queued"):
            out["confirmed"] = confirmed["queued"]
        # 「选中的款」先写上店铺和货号；上架图和标题由后台 temu sync 准备，好了再写上架标题
        out["selected"] = temu.write_now(ctx, ids)
        out["prepare"] = bool(temu.kick(ctx))
    # 两行的店铺列和货号：拿得到选款表同步租约当场写，拿不到交给下一次表格同步
    if lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS):
        try:
            out["cells"] = sync_cells(ctx, api, info, errors)
        finally:
            lease.drop(ctx, feishu_table.LEASE_KEY)
    else:
        feishu_table.kick(ctx)
        out["cells"] = "等下一次表格同步"
    return out


def _select(ctx: Ctx, picked: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """还没选的行勾了店铺（全自动）：定为选中，确认人是勾的人。返回（定下来的那些，没定成的 [{asset_id, reason}]）。
    已经被别处定成选中的照样往下走；已经定了不要 / 正在修改的不动。"""
    from . import review  # 审核模块依赖表格模块，用时再导入

    done: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, str]] = []
    for asset_id, item in picked.items():
        review_id = ctx.db.scalar(
            "SELECT review_id FROM reviews WHERE subject_type = 'asset_selection' AND subject_id = ? AND status = 'open'",
            (asset_id,),
        )
        if not review_id:
            if ctx.db.scalar("SELECT human_decision FROM assets WHERE asset_id = ?", (asset_id,)) == "approved":
                done[asset_id] = item
            else:
                failed.append({"asset_id": asset_id, "reason": "这张已经定了（不要 / 在修改），勾店铺不算"})
            continue
        try:
            review.decide(ctx, review_id, "approved", by=item["by"], note=AUTO_TICK_NOTE)
        except review.ReviewError as exc:
            failed.append({"asset_id": asset_id, "reason": str(exc)[:200]})
            continue
        done[asset_id] = item
    return done, failed


def _pending_picks(
    ctx: Ctx, table_id: str, targets: list[dict[str, str]], seen: dict[str, dict[str, Any]], by: str
) -> dict[str, dict[str, Any]]:
    """还没选的行里勾了开着全自动的店的：{asset_id: {keys, records, by}}（keys 是勾的全部店，没开全自动的也一起记）。"""
    from . import temu  # 上架模块依赖本模块，用时再导入

    auto_keys = temu.auto_stores(ctx)
    out: dict[str, dict[str, Any]] = {}
    for target in targets:
        if target["row"] != "pending":
            continue
        got = seen.get(target["record_id"]) or {}
        ticked = set(picks(ctx, table_id, got.get("fields") or {}))
        if not ticked & auto_keys:
            continue
        item = out.setdefault(target["asset_id"], {"keys": set(), "records": [], "by": got.get("by") or by})
        item["keys"].update(ticked)
        item["records"].append(target["record_id"])
    return out


def _select_and_apply(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    pending: dict[str, dict[str, Any]],
    *,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    selected, failed = _select(ctx, pending)
    out = _apply(ctx, api, info, table_id, selected, errors=errors) if selected else {"chosen": {}, "refused": {}}
    out["selected_rows"] = sorted(selected)
    if failed:
        out["not_selected"] = failed
    return out


def apply_ticks(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    targets: list[dict[str, str]],
    *,
    by: str,
) -> dict[str, Any]:
    """记录变更事件里勾上店铺列的行（tick_targets 认出来的）：回读确认勾着，按款处理。已经选中的款：新勾的店当场记下
    （_apply）；还没选的款（全自动）：撤销窗口开着时记下等到点（store_tick_pending + 后台 `table ticks`），没开就当场定为选中再处理。
    已经记下的店铺不再处理（程序自己写这两行的勾也会发事件）。"""
    seen = _read_rows(api, info, table_id, [t["record_id"] for t in targets])
    have = {
        a: {r["store_key"] for r in items}
        for a, items in stores.by_asset(ctx, [t["asset_id"] for t in targets]).items()
    }
    picked: dict[str, dict[str, Any]] = {}
    for target in targets:
        if target["row"] == "pending":
            continue
        got = seen.get(target["record_id"])
        keys = set(picks(ctx, table_id, (got or {}).get("fields") or {})) - have.get(target["asset_id"], set())
        if not keys:
            continue
        item = picked.setdefault(target["asset_id"], {"keys": set(), "records": [], "by": by})
        item["keys"].update(keys)
        item["records"].append(target["record_id"])
    pending = _pending_picks(ctx, table_id, targets, seen, by)
    if not picked and not pending:
        return {"ignored": "店铺列没有新勾的店"}
    errors: list[dict[str, str]] = []
    out = _apply(ctx, api, info, table_id, picked, errors=errors) if picked else {"chosen": {}, "refused": {}}
    if pending:
        if grace_minutes(ctx) > 0:
            out["pending"] = _remember_pending(ctx, table_id, pending)
            task = kick_ticks(ctx)
            if task:
                out["ticks_task"] = task["task_id"]
        else:
            part = _select_and_apply(ctx, api, info, table_id, pending, errors=errors)
            out["chosen"].update(part["chosen"])
            out["refused"].update(part["refused"])
            out["selected_rows"] = part["selected_rows"]
            if part.get("not_selected"):
                out["not_selected"] = part["not_selected"]
    if errors:
        out["errors"] = errors
    return out


# ---------------------------------------------------------------- 撤销窗口（store_tick_pending）


def _remember_pending(ctx: Ctx, table_id: str, pending: dict[str, dict[str, Any]]) -> list[str]:
    """勾了店铺、还在撤销窗口里的行记下来（再勾一次不延后到点时间，换了店铺照新的记）。返回这些款。"""
    now = utcnow()
    due = iso(now + dt.timedelta(minutes=grace_minutes(ctx)))
    with ctx.db.tx() as conn:
        for asset_id, item in pending.items():
            for record_id in item["records"]:
                conn.execute(
                    "INSERT INTO store_tick_pending(table_id, record_id, asset_id, keys_json, ticked_by, first_seen_at,"
                    " due_at, attempts) VALUES(?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(table_id, record_id) DO UPDATE SET"
                    " keys_json = excluded.keys_json, ticked_by = excluded.ticked_by",
                    (table_id, record_id, asset_id, dumps(sorted(item["keys"])), item["by"], iso(now), due),
                )
            trace.record(
                conn,
                asset_id,
                "tick",
                to="pending",
                actor=item["by"],
                source="table",
                payload={"stores": sorted(item["keys"]), "due_at": due},
            )
    return sorted(pending)


def has_pending(ctx: Ctx) -> bool:
    return bool(ctx.db.scalar("SELECT COUNT(*) FROM store_tick_pending"))


def forget_pending(ctx: Ctx, asset_ids: list[str]) -> int:
    """这几款已经定了（犹豫期里点了「选款完成」）：撤销窗口里的记录删掉，到点不再处理。返回删了几条。"""
    ids = sorted(set(asset_ids))
    if not ids:
        return 0
    removed = 0
    with ctx.db.tx() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            cur = conn.execute(
                f"DELETE FROM store_tick_pending WHERE asset_id IN ({','.join('?' * len(chunk))})", tuple(chunk)
            )
            removed += max(0, int(getattr(cur, "rowcount", 0) or 0))
    return removed


def pending_due(ctx: Ctx) -> bool:
    return bool(ctx.db.scalar("SELECT COUNT(*) FROM store_tick_pending WHERE due_at <= ?", (now_iso(),)))


def kick_ticks(ctx: Ctx) -> dict[str, Any] | None:
    """有撤销窗口里的勾、又没有守着的后台进程时，拉起 `podctl table ticks`（到点回读、处理）。"""
    if not has_pending(ctx) or any(t["kind"] == "ticks" for t in bg.running(ctx)):
        return None
    return bg.spawn(ctx, ["table", "ticks"], kind="ticks")


def process_pending(ctx: Ctx) -> dict[str, Any]:
    """到点的勾：回读仍勾着（还勾着开着全自动的店）就定为选中、处理；取消了的丢掉（记一条轨迹）。"""
    rows = [dict(r) for r in ctx.db.q("SELECT * FROM store_tick_pending WHERE due_at <= ? ORDER BY due_at", (now_iso(),))]
    out: dict[str, Any] = {"due": len(rows), "selected": [], "withdrawn": [], "errors": []}
    if not rows:
        return out
    info = feishu_table.base(ctx) or {}
    if not info:
        return out
    by_table: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_table.setdefault(row["table_id"], []).append(row)
    with feishu.Client(ctx) as api:
        for table_id, items in by_table.items():
            try:
                seen = _read_rows(api, info, table_id, [r["record_id"] for r in items])
            except _FAILURES as exc:
                out["errors"].append({"table_id": table_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
                _bump_pending(ctx, items, str(exc))
                continue
            targets = [{"asset_id": r["asset_id"], "record_id": r["record_id"], "row": "pending"} for r in items]
            by = {r["asset_id"]: r.get("ticked_by") or "feishu:选款表" for r in items}
            pending = {
                a: {**item, "by": by.get(a) or item["by"]}
                for a, item in _pending_picks(ctx, table_id, targets, seen, "feishu:选款表").items()
            }
            withdrawn = sorted({r["asset_id"] for r in items} - set(pending))
            errors: list[dict[str, str]] = []
            if pending:
                part = _select_and_apply(ctx, api, info, table_id, pending, errors=errors)
                out["selected"] += part["selected_rows"]
                out["errors"] += errors
                out.setdefault("chosen", {}).update(part["chosen"])
            with ctx.db.tx() as conn:
                for r in items:
                    conn.execute(
                        "DELETE FROM store_tick_pending WHERE table_id = ? AND record_id = ?",
                        (r["table_id"], r["record_id"]),
                    )
                for asset_id in withdrawn:
                    trace.record(conn, asset_id, "tick", to="withdrawn", source="table", reason="撤销窗口里取消了店铺勾")
            out["withdrawn"] += withdrawn
    return out


def _bump_pending(ctx: Ctx, items: list[dict[str, Any]], error: str) -> None:
    """回读失败：一会儿再试；试了好几次还不行就丢掉（巡检整张表读时还会认出来）。"""
    with ctx.db.tx() as conn:
        for r in items:
            if int(r.get("attempts") or 0) + 1 >= feishu_table.MAX_ATTEMPTS:
                conn.execute(
                    "DELETE FROM store_tick_pending WHERE table_id = ? AND record_id = ?",
                    (r["table_id"], r["record_id"]),
                )
                continue
            conn.execute(
                "UPDATE store_tick_pending SET attempts = attempts + 1, last_error = ?, due_at = ?"
                " WHERE table_id = ? AND record_id = ?",
                (error[:300], iso(utcnow() + dt.timedelta(minutes=1)), r["table_id"], r["record_id"]),
            )


def watch_pending(ctx: Ctx, *, max_minutes: float = TICKS_WATCH_MINUTES) -> dict[str, Any]:
    """后台 `table ticks`：守着撤销窗口，到点处理，没有了就退出（最多守 max_minutes）。"""
    import time

    deadline = time.monotonic() + max_minutes * 60
    total: dict[str, Any] = {"selected": [], "withdrawn": [], "errors": [], "rounds": 0}
    while time.monotonic() < deadline:
        part = process_pending(ctx)
        total["rounds"] += 1
        for key in ("selected", "withdrawn", "errors"):
            total[key] += part.get(key) or []
        if not has_pending(ctx):
            break
        bg.step(ctx, "等撤销窗口到点")
        nxt = parse_iso(ctx.db.scalar("SELECT MIN(due_at) FROM store_tick_pending"))
        wait = 5.0 if nxt is None else max(1.0, min(30.0, (nxt - utcnow()).total_seconds() + 1))
        _sleep(wait)
    return total


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


# ---------------------------------------------------------------- 巡检兜底


def poll_due(ctx: Ctx) -> bool:
    """到了回读候选表店铺列的时候（事件漏掉的勾、取消的勾、人改的格子），或有到点的撤销窗口。"""
    if not enabled(ctx):
        return False
    if pending_due(ctx):
        return True
    at = parse_iso((loads(ctx.db.get_meta(TICKS_META), None) or {}).get("polled_at"))
    return at is None or utcnow() - at >= dt.timedelta(minutes=POLL_MINUTES)


def _open_tables(ctx: Ctx) -> list[dict[str, str]]:
    """开着的候选表（任务没归档、表里有店铺列）：[{seed_id, table_id}]。"""
    archived = {
        r["seed_id"] for r in ctx.db.q("SELECT seed_id FROM seed_shelf WHERE state = 'archived'")
    }
    return [
        {"seed_id": r["seed_id"], "table_id": r["table_id"]}
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id")
        if r["seed_id"] not in archived and present(ctx, r["table_id"])
    ]


def _candidate_ignore() -> set[str]:
    """候选表里程序写的列（不看人改）：选中、店铺列、没选原因、备注和人加的列以外的。"""
    from . import candidates  # 候选表模块依赖本模块，用时再导入

    names = {f["field_name"] for f in candidates.FIELDS}
    names -= {candidates.CHECK_FIELD, candidates.REASON_FIELD, candidates.NOTE_FIELD}
    return names | {candidates.PACKAGE_FIELD, CODE_FIELD}


def _candidate_program(ctx: Ctx, table_id: str) -> Any:
    """候选表里程序上次写进各格的值：「选中」按选款结果，店铺列按 candidate_codes / candidate_scene_codes。"""
    from . import candidates, cells  # 都依赖本模块，用时再导入

    states = {
        r["record_id"]: r["state"]
        for r in ctx.db.q(
            "SELECT c.record_id, c.state FROM candidate_rows c JOIN candidate_tables t ON t.seed_id = c.seed_id"
            " WHERE t.table_id = ? AND c.record_id IS NOT NULL",
            (table_id,),
        )
    }
    written: dict[str, dict[str, Any]] = {}
    for cache in ("candidate_codes", "candidate_scene_codes"):
        for r in ctx.db.q(f"SELECT record_id, value FROM {cache}"):
            written[r["record_id"]] = loads(r["value"], {}) or {}

    def program(record_id: str, name: str) -> Any:
        if name == candidates.CHECK_FIELD and record_id in states:
            state = states[record_id]
            if state == candidates.STATE_APPROVED:
                return True
            if state in (candidates.STATE_REJECTED,):
                return None
            return cells.MISSING
        cached = written.get(record_id) or {}
        if name in cached:
            return cached[name]
        return cells.MISSING

    return program


def _unticks(
    ctx: Ctx,
    table_id: str,
    rows: list[dict[str, Any]],
    seen: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """全自动时人在候选表取消了店铺勾（程序上次写的是勾着、最后修改人是人）：这家店还没开始建箱就去掉，
    已经建箱 / 上了的勾写回去（删掉写过的记录，下次同步重写）。返回 {removed: {asset: [店]}, reticked: [asset]}。"""
    by_column = column_map(ctx)
    cached: dict[str, dict[str, Any]] = {}
    for cache in ("candidate_codes", "candidate_scene_codes"):
        for r in ctx.db.q(f"SELECT asset_id, record_id, value FROM {cache}"):
            cached[r["record_id"]] = {"asset_id": r["asset_id"], "cache": cache, "cells": loads(r["value"], {}) or {}}
    groups = stores.by_asset(ctx, sorted({r["asset_id"] for r in rows}))
    out: dict[str, Any] = {"removed": {}, "reticked": []}
    for row in rows:
        got = seen.get(row["record_id"])
        mine = cached.get(row["record_id"])
        if not got or not got.get("by") or not mine:
            continue
        ticked = set(picks(ctx, table_id, got.get("fields") or {}))
        for column, value in mine["cells"].items():
            store_ = by_column.get(column)
            if store_ is None or value is not True or store_.key in ticked:
                continue
            listing = next((r for r in groups.get(row["asset_id"]) or [] if r["store_key"] == store_.key), None)
            if listing is None:
                continue
            if listing["state"] in stores.UNSTARTED:
                removed = stores.remove(
                    ctx,
                    row["asset_id"],
                    [store_.key],
                    by=got["by"],
                    source="table",
                    reason="候选表里取消了店铺勾",
                    allow_queued=True,
                )
                if removed:
                    out["removed"].setdefault(row["asset_id"], []).extend(removed)
            else:
                with ctx.db.tx() as conn:
                    conn.execute(f"DELETE FROM {mine['cache']} WHERE record_id = ?", (row["record_id"],))
                    trace.record(
                        conn,
                        row["asset_id"],
                        "tick",
                        to="reticked",
                        store=store_.key,
                        code=listing.get("code"),
                        actor=got["by"],
                        source="table",
                        reason="这家店已经建进妙手 / 上架了，取消不了，勾写回去",
                    )
                out["reticked"].append(row["asset_id"])
    return out


def poll(ctx: Ctx) -> dict[str, Any] | None:
    """巡检兜底（到了 POLL_MINUTES 或有到点的撤销窗口才读）：整张读开着的候选表，
    已经选中的款勾着、却还没记成店铺的照样处理；全自动时还没选的行勾了店铺的定为选中（漏掉的事件），取消的勾按环节去店 / 写回；
    人改的格子记日志（cells.observe）。返回做了什么；没到时候为 None。"""
    from . import cells  # 人改格子的日志依赖本模块之外的表，用时再导入

    if not poll_due(ctx):
        return None
    out: dict[str, Any] = {"checked": 0, "chosen": {}, "refused": {}, "errors": []}
    if pending_due(ctx):
        out["pending"] = process_pending(ctx)
    at = parse_iso((loads(ctx.db.get_meta(TICKS_META), None) or {}).get("polled_at"))
    if at is not None and utcnow() - at < dt.timedelta(minutes=POLL_MINUTES):
        return out
    info = feishu_table.base(ctx) or {}
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, TICKS_META, dumps({"polled_at": now_iso()}))
    if not info:
        return out
    auto = auto_on(ctx)
    ignore = _candidate_ignore()
    with feishu.Client(ctx) as api:
        for table in _open_tables(ctx):
            table_id = table["table_id"]
            try:
                records = cells.read_table(api, info, table_id)
            except _FAILURES as exc:
                out["errors"].append({"table_id": table_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            approved = [
                dict(r)
                for source in ("candidate_rows", "candidate_scene_rows")
                for r in ctx.db.q(
                    f"SELECT c.asset_id, c.record_id FROM {source} c"
                    " JOIN assets a ON a.asset_id = c.asset_id"
                    " WHERE c.seed_id = ? AND c.record_id IS NOT NULL AND a.human_decision = 'approved'",
                    (table["seed_id"],),
                )
                if r["record_id"] in records
            ]
            out["checked"] += len(records)
            have = {
                a: {r["store_key"] for r in items}
                for a, items in stores.by_asset(ctx, [r["asset_id"] for r in approved]).items()
            }
            picked: dict[str, dict[str, Any]] = {}
            for row in approved:
                got = records.get(row["record_id"]) or {}
                keys = set(picks(ctx, table_id, got.get("fields") or {})) - have.get(row["asset_id"], set())
                if not keys:
                    continue
                item = picked.setdefault(
                    row["asset_id"], {"keys": set(), "records": [], "by": got.get("by") or "feishu:选款表"}
                )
                item["keys"].update(keys)
                item["records"].append(row["record_id"])
            if picked:
                done = _apply(ctx, api, info, table_id, picked, errors=out["errors"])
                out["chosen"].update(done["chosen"])
                out["refused"].update(done["refused"])
            if auto:
                pending_rows = [
                    {"asset_id": r["asset_id"], "record_id": r["record_id"], "row": "pending"}
                    for r in ctx.db.q(
                        "SELECT c.asset_id, c.record_id FROM candidate_rows c"
                        " JOIN reviews v ON v.subject_type = 'asset_selection' AND v.subject_id = c.asset_id"
                        " AND v.status = 'open' WHERE c.seed_id = ? AND c.record_id IS NOT NULL",
                        (table["seed_id"],),
                    )
                    if r["record_id"] in records
                ]
                pending = _pending_picks(ctx, table_id, pending_rows, records, "feishu:选款表")
                if pending:
                    part = _select_and_apply(ctx, api, info, table_id, pending, errors=out["errors"])
                    out["chosen"].update(part["chosen"])
                    out.setdefault("selected_rows", []).extend(part["selected_rows"])
                gone = _unticks(ctx, table_id, approved, records)
                if gone["removed"] or gone["reticked"]:
                    out.setdefault("unticked", []).append(gone)
            refs = {
                r["record_id"]: {"asset_id": r["asset_id"]}
                for source in ("candidate_rows", "candidate_scene_rows")
                for r in ctx.db.q(
                    f"SELECT asset_id, record_id FROM {source} WHERE seed_id = ? AND record_id IS NOT NULL",
                    (table["seed_id"],),
                )
            }
            edits = cells.observe(
                ctx,
                kind=cells.KIND_CANDIDATE,
                table_id=table_id,
                records=records,
                refs=refs,
                ignore=ignore,
                program=_candidate_program(ctx, table_id),
                full=True,
            )
            if edits:
                out["edits"] = out.get("edits", 0) + len(edits)
    if out.get("unticked"):
        from . import temu  # 上架模块依赖本模块，用时再导入

        removed = sorted({a for g in out["unticked"] for a in g["removed"]})
        if removed:
            temu.write_now(ctx, removed)
        feishu_table.kick(ctx)
    return out


def status(ctx: Ctx) -> dict[str, Any]:
    meta = _meta(ctx)
    return {
        "enabled": enabled(ctx),
        "columns": list(column_map(ctx)),
        "tables_done": len(meta.get("done") or {}),
        "tables_pending": len(pending_tables(ctx)),
        "cells_pending": sum(len(v) for v in _changes(ctx).values()),
        "ticks_polled_at": (loads(ctx.db.get_meta(TICKS_META), None) or {}).get("polled_at"),
        "auto": auto_on(ctx),
        "tick_grace_minutes": grace_minutes(ctx),
        "ticks_pending": [
            dict(r)
            for r in ctx.db.q(
                "SELECT asset_id, keys_json, ticked_by, first_seen_at, due_at, attempts FROM store_tick_pending ORDER BY due_at"
            )
        ],
        "error": meta.get("error"),
    }
