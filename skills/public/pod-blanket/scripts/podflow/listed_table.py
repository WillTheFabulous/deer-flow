"""已上架的款（2026-10-28）：每个上了 Temu 的（款, 店）一条，业务库 listed_products 是底账，飞书「已上架的款」是给人看、填数据的副本。

- 进账：temu.py 查到妙手采集箱状态 published 时 record() 记一条（货号为主键），记下当时的快照：标题、店铺、站点、尺码价格、
  10 张上架图（OSS 键 + 场景）、主图类型、套图、样板、妙手 ID。以后改不了的东西都在快照里。
- 飞书表（和「选中的款」同一个多维表格，第一次有已上架的款时建）：程序列（货号、主图、店铺、标题、上架日期、素材、任务、
  主图类型、图位、尺码与申报价、上架图包、妙手 ID）库 → 飞书，只在变了时写；人填列（Temu 状态、Temu 商品 ID、商品链接、
  累计销量、近 7 天销量、累计销售额、数据更新日期、备注）飞书 → 库，每 `temu.listed.read_minutes` 分钟（缺省 30）在巡检里回读。
  回读时发现程序列被改了 → 下次同步改回来；行被删了 → 下次同步按库里的程序列 + 人填列重建。表里人自己加的行不管。
- 视图：「全部」（默认）、每家店一个、「看图」（画册，封面主图）。上架图包链接快到期时跟着续签。
- 同步在表格同步进程里做（table sync，和「选中的款」共用租约），回读在巡检进程（table poll）里做。
- 2026-10-30（pod-listing-autopilot）：回读时每一格和上次读到的比，人改的记 table_cell_log（cells.py，含旧值 / 新值 /
  修改人 / 时间）；人填的数据变了（或这一天还没记过）记一条 listed_metrics（销量、销售额、Temu 状态的时间序列）。
  「上架标题」改成指令列：人改了交给 title_sync（复核、按环节同步到妙手采集箱或 Temu），程序不再改回；新加一列「标题同步」。
  开着 Temu 官方接口（temu_open）时「Temu 状态」「Temu 商品 ID」「商品链接」改由程序按接口写（人原来填的值留在日志里）。
- 每周选款表（table.weekly，books.py）：每本周表格一张「已上架的款」（从那本的「已上架的款模板」复制、挪到根目录，
  「看图」的封面效果跟着走），一条已上架的款按上架日期（timezone）归到那一周；上架日期早于 table.weekly_since、或开关打开
  之前已经写进旧表格的，留在旧表格那一张。归到哪一张在第一次写飞书时定下来（listed_books），之后不变。
- 2026-10-30 四家店统一底账（`temu.listed.source: mooya`，mooya feature-plans/temu-listing-unify）：底账在 mooya
  （`temu_listed_products`，含 1店 / 2店交付工作台上的、回填的人工款），这里按 `temu.listed.clients` 里的客户端增量拉
  （`pull`，每 `temu.listed.pull_minutes` 分钟在巡检里拉一次）镜像进 listed_rows，飞书「已上架的款」照旧按周写：
  店铺列 / 每家店的视图加上 mooya 那边的店，主图从 mooya 取（妙手图链接或带令牌的图片接口），上架图包链接由 mooya 打包、
  续签。人填的列读回后写回 mooya（`/temu/listed/<code>/human`，mooya 记改值日志与时间序列），改「上架标题」交给 mooya
  复核并同步（`/temu/listed/<code>/title`），这里不再记 listed_metrics。本机引擎上的款（`temu.engine: local`）查到已上架时
  照旧记 listed_products，再推给 mooya（`push_pending`，没推成下一轮接着推）。`source: local` 时一切照旧。
- 2026-10-31 精作款周表格（`temu.listed.crafted`，crafted.py）：mooya 上做的款（1店 / 2店）不进这里，按上架日期进「精作款」
  周表格里那一天的表（`place()` 给 `crafted:<day>`，表记录在 crafted_days）；写行、回读、视图、店铺选项都是这一套代码，
  精作款的表只有精作款那几家店，「已上架的款」不再有它们的视图。模板没锁上之前精作款的新行先不写。
- 2026-11-02 这条流水线也上 1店 / 2店（本机 `temu.stores` 配 s1 / s2，货号 FLRP / JUHP，mooya 租户 ai_temu_deerflow）：
  这些款走 deerflow 客户端，照样进「已上架的款」，1店 / 2店的选项和视图回到这里（`local_columns`）；精作款仍按客户端分到
  「精作款」。listed_rows 以货号为主键，两边的货号前缀不能一样。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import (
    feishu,
    feishu_table,
    http,
    images,
    labels,
    lease,
    listing,
    mooya,
    store,
    stores,
    temu_assets,
    temu_title,
    trace,
    views,
)
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso, parse_iso, sha256_text, utcnow

TABLE_NAME = "已上架的款"
META_KEY = "listed_table"
READ_META_KEY = "listed_table.read_at"
PULL_META_KEY = "listed_table.pull"
PULLED_META_KEY = "listed_table.pulled_at"
STORES_META_KEY = "listed_table.mooya_stores"
_PAGE = 500
_PULL_PAGE = 200
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError, mooya.MooyaError)
# 取主图时这几种 = 图没了，不带主图建行（别的错照常计次重试）
GONE_STATUSES = (403, 404, 410)
SOURCE_LOCAL = "local"
SOURCE_MOOYA = "mooya"
# listed_rows 里从 mooya 镜像过来的列（改了才算变）
MIRROR_COLUMNS = (
    "client",
    "store_key",
    "mooya_id",
    "asset_id",
    "seed_id",
    "title",
    "detail_id",
    "listed_at",
    "snapshot_json",
    "ledger_json",
    "temu_status",
    "temu_product_id",
    "product_url",
    "sales_total",
    "sales_7d",
    "revenue_total",
    "data_updated_on",
    "note",
    "mooya_updated_at",
)

F_CODE = "货号"
F_THUMB = "主图"
F_STORE = "店铺"
F_TITLE = "上架标题"
F_DATE = "上架日期"
F_ASSET = "素材"
F_TASK = "任务"
F_MAIN = "主图类型"
F_ORDER = "图位"
F_PRICES = "尺码与申报价"
F_PACKAGE = "上架图包"
F_DETAIL = "妙手 ID"
F_STATUS = "Temu 状态"
F_PRODUCT = "Temu 商品 ID"
F_URL = "商品链接"
F_SALES = "累计销量"
F_SALES_7D = "近 7 天销量"
F_REVENUE = "累计销售额"
F_DATA_ON = "数据更新日期"
F_NOTE = "备注"
F_TITLE_SYNC = "标题同步"
TEMU_STATUSES = ("在售", "审核中", "驳回", "已下架", "已删除")
PROGRAM_HELP = "程序写，以业务库为准：手改会在下次同步时改回来。"
HUMAN_HELP = "你来填（Temu 后台看到的）；程序每隔一阵读回业务库，用来统计。"
TITLE_HELP = "可以直接改：程序按 Temu 标题规则复核，合规的同步到店铺（还在妙手采集箱的存回去，已上架的走 Temu 接口），结果看「标题同步」。"
TITLE_SYNC_HELP = "程序写：改了「上架标题」之后同步到哪了，不合规写原因。"
API_HELP = "接上 Temu 官方接口后程序按接口写（以 Temu 为准）；没接上时你来填。"
# 人填列：飞书列名 → (库里的列, 类型)
HUMAN_COLUMNS: dict[str, tuple[str, str]] = {
    F_STATUS: ("temu_status", "text"),
    F_PRODUCT: ("temu_product_id", "text"),
    F_URL: ("product_url", "link"),
    F_SALES: ("sales_total", "int"),
    F_SALES_7D: ("sales_7d", "int"),
    F_REVENUE: ("revenue_total", "float"),
    F_DATA_ON: ("data_updated_on", "date"),
    F_NOTE: ("note", "text"),
}
# 接上 Temu 官方接口（temu_open）后改由程序写的列
API_COLUMNS = (F_STATUS, F_PRODUCT, F_URL)
# 回读时核对的程序列（人改了要改回来的）；「上架标题」2026-10-30 起是指令列（人改了就同步到店铺）
CHECKED_PROGRAM = (F_CODE, F_STORE, F_DETAIL)
# 列的版本：2 = 加了「标题同步」（之前建的表在同步时补上）
FIELDS_VERSION = 2


def api_owned(ctx: Ctx) -> bool:
    """「Temu 状态」「Temu 商品 ID」「商品链接」由程序按 Temu 接口写（开着 temu_open）。"""
    from . import temu_open  # Temu 接口依赖本模块，用时再导入

    return temu_open.enabled(ctx)


def human_columns(ctx: Ctx) -> dict[str, tuple[str, str]]:
    """现在由人填、要回读进库的列。"""
    if api_owned(ctx):
        return {k: v for k, v in HUMAN_COLUMNS.items() if k not in API_COLUMNS}
    return dict(HUMAN_COLUMNS)


# ---------------------------------------------------------------- 底账来源（本机 / mooya）


def source_mooya(ctx: Ctx) -> bool:
    """「已上架的款」读 mooya 的四家店底账（temu.listed.source: mooya）。"""
    return str(ctx.setting("temu.listed.source") or SOURCE_LOCAL) == SOURCE_MOOYA


def _table(ctx: Ctx) -> str:
    return "listed_rows" if source_mooya(ctx) else "listed_products"


def ledger_clients(ctx: Ctx) -> list[str]:
    """拉底账用哪几个 mooya 客户端（各绑一个租户：deerflow = 这条流水线上的款，四家店都有；ai_temu = 1店 / 2店的精作款）；
    没配用交套图的那个。"""
    names = [str(n) for n in ctx.setting("temu.listed.clients") or [] if n]
    return names or [mooya.submit_client(ctx)]


def mooya_stores(ctx: Ctx) -> dict[str, dict[str, Any]]:
    """上次拉底账时 mooya 报的店铺：{客户端: {店铺 key: {column, temuOpen, …}}}。"""
    return loads(ctx.db.get_meta(STORES_META_KEY), None) or {}


def local_columns(ctx: Ctx) -> list[str]:
    """本机配的店（这条流水线上架的店）的列名：这里上的款都进「已上架的款」，和精作款的店同名也照样有选项和视图。"""
    return [s.column for s in stores.all_stores(ctx)] if stores.enabled(ctx) else []


def store_columns(ctx: Ctx) -> list[str]:
    """「已上架的款」店铺列的选项（= 每家店一个视图）：本机配的店在前，mooya 那边多出来的店接在后面；开着精作款周表格时
    只有精作款客户端有的店不在这里（它们在「精作款」里）。2026-11-02 起本机也上 1店 / 2店（货号 FLRP / JUHP），这两家
    两边都有：精作款进「精作款」，这里上的进「已上架的款」（按 mooya 客户端分，见 crafted.is_crafted）。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    out = local_columns(ctx)
    if source_mooya(ctx):
        skip = set(crafted.store_columns(ctx)) if crafted.enabled(ctx) else set()
        for by_key in mooya_stores(ctx).values():
            for spec in sorted(by_key.values(), key=lambda s: str(s.get("key") or "")):
                column = str(spec.get("column") or "")
                if column and column not in out and column not in skip:
                    out.append(column)
    return out


def _label(bid: str | None) -> str:
    """报错时这一张表叫什么：已上架的款 / 已上架的款（2026-W44）/ 精作款（2026-10-26）。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    if crafted.is_key(bid):
        return f"{crafted.ROOT_TITLE}（{crafted.day_of(str(bid))}）"
    return TABLE_NAME if not bid else f"{TABLE_NAME}（{bid}）"


def _columns_for(ctx: Ctx, bid: str | None) -> list[str]:
    """这一张表的店铺列选项：精作款的表只有精作款那几家，「已上架的款」是别的店。"""
    from . import crafted  # 同上

    return crafted.store_columns(ctx) if crafted.is_key(bid) else store_columns(ctx)


def _api_owned_row(ctx: Ctx, item: dict[str, Any]) -> bool:
    """这一行的 Temu 状态 / 商品 ID / 链接由程序按 Temu 接口写：本机看 temu_open，mooya 底账看 mooya 那家店接没接接口。"""
    if not source_mooya(ctx):
        return api_owned(ctx)
    spec = (mooya_stores(ctx).get(item.get("client") or "") or {}).get(item.get("store_key") or "") or {}
    return bool(spec.get("temuOpen"))


def _human_columns_row(ctx: Ctx, item: dict[str, Any]) -> dict[str, tuple[str, str]]:
    if _api_owned_row(ctx, item):
        return {k: v for k, v in HUMAN_COLUMNS.items() if k not in API_COLUMNS}
    return dict(HUMAN_COLUMNS)


def _help(text: str) -> dict[str, Any]:
    return {"disable_sync": True, "text": text}


def enabled(ctx: Ctx) -> bool:
    if not (feishu_table.enabled(ctx) and bool(ctx.setting("temu.listed.enabled", True))):
        return False
    # 读 mooya 底账时本机没开上架（没配店）也照样写四家店的表
    return stores.enabled(ctx) or source_mooya(ctx)


def field_specs(ctx: Ctx) -> list[dict[str, Any]]:
    """「已上架的款」的列（第一列货号是主键）。"""
    program = _help(PROGRAM_HELP)
    human = _help(HUMAN_HELP)
    return [
        {"field_name": F_CODE, "type": 1},
        {"field_name": F_THUMB, "type": 17, "description": program},
        {
            "field_name": F_STORE,
            "type": 3,
            "property": {"options": [{"name": c} for c in store_columns(ctx)]},
            "description": program,
        },
        {"field_name": F_TITLE, "type": 1, "description": _help(TITLE_HELP)},
        {"field_name": F_DATE, "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}, "description": program},
        {"field_name": F_ASSET, "type": 1, "description": program},
        {"field_name": F_TASK, "type": 1, "description": program},
        {
            "field_name": F_MAIN,
            "type": 3,
            "property": {"options": [{"name": n} for n in (*temu_assets.MAIN_KINDS.values(), "其他")]},
            "description": program,
        },
        {"field_name": F_ORDER, "type": 1, "description": program},
        {"field_name": F_PRICES, "type": 1, "description": program},
        {"field_name": F_PACKAGE, "type": 15, "description": program},
        {"field_name": F_DETAIL, "type": 1, "description": program},
        {
            "field_name": F_STATUS,
            "type": 3,
            "property": {"options": [{"name": n} for n in TEMU_STATUSES]},
            "description": human,
        },
        {"field_name": F_PRODUCT, "type": 1, "description": human},
        {"field_name": F_URL, "type": 15, "description": human},
        {"field_name": F_SALES, "type": 2, "property": {"formatter": "0"}, "description": human},
        {"field_name": F_SALES_7D, "type": 2, "property": {"formatter": "0"}, "description": human},
        {"field_name": F_REVENUE, "type": 2, "property": {"formatter": "0.00"}, "description": human},
        {"field_name": F_DATA_ON, "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}, "description": human},
        {"field_name": F_NOTE, "type": 1, "description": human},
        {"field_name": F_TITLE_SYNC, "type": 1, "description": _help(TITLE_SYNC_HELP)},
    ]


def _meta(ctx: Ctx, bid: str | None = None) -> dict[str, Any]:
    """「已上架的款」那张表的记录：旧表格的在 meta listed_table，周表格的在那本周表格的 listed 里；
    `crafted:<day>` 是精作款那一天的表（crafted_days）。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    if crafted.is_key(bid):
        return crafted.table_meta(ctx, str(bid))
    if bid:
        from . import books  # 每周选款表依赖表格模块，放到用时再导入

        return dict((books.book(ctx, bid) or {}).get("listed") or {})
    return loads(ctx.db.get_meta(META_KEY), None) or {}


def _save_meta(ctx: Ctx, meta: dict[str, Any], bid: str | None = None) -> None:
    from . import crafted  # 同上

    if crafted.is_key(bid):
        crafted.save_table_meta(ctx, meta, str(bid))
        return
    if bid:
        from . import books  # 同上

        found = books.book(ctx, bid) or {}
        found["listed"] = meta
        books.save_book(ctx, found)
        return
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, META_KEY, dumps(meta))


def _places(ctx: Ctx) -> list[tuple[str | None, dict[str, Any], dict[str, Any]]]:
    """建好了的每一张「已上架的款」：[(book_id 或 None = 旧表格, 表格信息, 表的记录)]；还有精作款每一天的表（crafted:<day>）。"""
    from . import books, crafted  # 同上

    out: list[tuple[str | None, dict[str, Any], dict[str, Any]]] = []
    info = feishu_table.base(ctx)
    meta = _meta(ctx)
    if info and meta.get("table_id"):
        out.append((None, info, meta))
    for found in books.books(ctx):
        listed = found.get("listed") or {}
        if listed.get("table_id"):
            out.append(
                (found["book_id"], {"app_token": found["app_token"], "url": found.get("url")}, listed)
            )
    out += crafted.places(ctx)
    return out


def table_url(ctx: Ctx) -> str | None:
    """「已上架的款」的链接：开着每周选款表时是这一周周表格里的那一张（还没建时是旧表格的）。"""
    from . import books  # 同上

    current = (books.current_book(ctx) if books.enabled(ctx) else None) or {}
    meta = current.get("listed") or {}
    url = current.get("url")
    if not meta.get("table_id"):
        meta = _meta(ctx)
        url = (feishu_table.base(ctx) or {}).get("url")
    if not meta.get("table_id") or not url:
        return None
    return views.link(url, table_id=meta["table_id"], view_id=(meta.get("views") or {}).get("all"))


def place(ctx: Ctx, item: dict[str, Any], *, freeze: bool = True) -> str | None:
    """这条已上架的款写进哪一张「已上架的款」：周表格的 book_id，None = 旧表格那一张。开着每周选款表时第一次用到就定下来
    （listed_books，旧表格记空串），之后不变；已经写进旧表格的（有 record_id、没记过）留在旧表格。
    开着精作款周表格时精作款的行是 `crafted:<day>`（crafted.place）；还在「已上架的款」里、没迁过的照旧在那边。"""
    from . import books, crafted  # 同上

    if crafted.is_crafted(ctx, item):
        found = crafted.place(ctx, item, freeze=freeze)
        if found:
            return found
    row = ctx.db.q1("SELECT book_id FROM listed_books WHERE code = ?", (item["code"],))
    if row:
        return row["book_id"] or None
    if item.get("record_id") or not books.enabled(ctx):
        return None
    day = books.local_day(ctx, item.get("listed_at"))
    bid = books.book_id(day) if day and day >= books.since(ctx) else None
    if freeze:
        with ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO listed_books(code, book_id, placed_at) VALUES(?, ?, ?)"
                " ON CONFLICT(code) DO NOTHING",
                (item["code"], bid or "", now_iso()),
            )
    return bid


def _tz(ctx: Ctx) -> ZoneInfo:
    return ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))


def _day_ms(ctx: Ctx, ts: str | None) -> int | None:
    at = parse_iso(ts)
    if not at:
        return None
    local = at.astimezone(_tz(ctx))
    return int(dt.datetime(local.year, local.month, local.day, tzinfo=_tz(ctx)).timestamp() * 1000)


# ---------------------------------------------------------------- 进账


def record(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    """一个（款, 店）上了 Temu：记进 listed_products（已有就只更新快照，人填的不动）。row 是 store_listings 那一行。"""
    from . import temu  # 上架模块依赖本模块，放到用时再导入

    store_ = stores.get(ctx, row["store_key"])
    item = temu_assets.get(ctx, row["asset_id"], row["set_id"]) if row.get("set_id") else None
    item = item or temu_assets.current(ctx, row["asset_id"]) or {}
    title = row.get("title") or (temu_title.get(ctx, row["asset_id"]) or {}).get("title")
    tpl = temu.cached_template(ctx, store_)
    old = ctx.db.q1("SELECT snapshot_json FROM listed_products WHERE code = ?", (row["code"],))
    snapshot = {
        "label": labels.asset(ctx, row["asset_id"]),
        "task": labels.task(ctx, row["seed_id"]) or row["seed_id"],
        "store": store_.column,
        "store_name": store_.name,
        "shop_id": row.get("shop_id") or store_.shop_id,
        "sites": next(iter(temu.shop_sites(store_, tpl).values()), []) if tpl else [],
        "prices": temu.prices(tpl) if tpl else [],
        "set_id": row.get("set_id") or item.get("set_id"),
        "fingerprint": item.get("fingerprint"),
        "images": [
            {"n": f["n"], "plan_id": f.get("plan_id"), "title": f.get("title"), "key": f.get("key"), "path": f.get("path")}
            for f in item.get("files") or []
        ],
        "main_kind": temu_assets.main_kind(item) if item else None,
        "image_order": temu_assets.image_order_text(item) if item else None,
        "detail_id": row.get("detail_id"),
        "template_detail_id": row.get("template_detail_id"),
        "title_source": (temu_title.get(ctx, row["asset_id"]) or {}).get("source"),
        "package": (loads(old["snapshot_json"], {}) or {}).get("package") if old else None,
    }
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO listed_products(code, asset_id, store_key, seed_id, title, detail_id, listed_at, snapshot_json,"
            " attempts, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)"
            " ON CONFLICT(code) DO UPDATE SET title = excluded.title, detail_id = excluded.detail_id,"
            " listed_at = excluded.listed_at, snapshot_json = excluded.snapshot_json, written_hash = NULL,"
            " updated_at = excluded.updated_at",
            (
                row["code"],
                row["asset_id"],
                row["store_key"],
                row["seed_id"],
                title,
                row.get("detail_id"),
                row.get("listed_at") or now,
                dumps(snapshot),
                now,
                now,
            ),
        )
        log_event(conn, row["asset_id"], "listed_product_recorded", {"code": row["code"], "store": row["store_key"]})
        trace.record(
            conn,
            row["asset_id"],
            "listed",
            to="recorded",
            store=row["store_key"],
            code=row["code"],
            payload={"detail_id": row.get("detail_id"), "title": title},
        )
    if source_mooya(ctx):
        # 底账在 mooya：本机引擎上的款也推过去（没推成的 push_pending 下一轮接着推）
        push_pending(ctx, codes=[row["code"]])
    return snapshot


def rows(ctx: Ctx, *, store_key: str | None = None) -> list[dict[str, Any]]:
    """要写飞书的那些行：本机底账 listed_products，或读 mooya 底账时的镜像 listed_rows（多 client / ledger）。"""
    sql = f"SELECT * FROM {_table(ctx)}"
    params: tuple = ()
    if store_key:
        sql += " WHERE store_key = ?"
        params = (store_key,)
    out = []
    for r in ctx.db.q(sql + " ORDER BY listed_at, code", params):
        item = dict(r)
        item["snapshot"] = loads(item.pop("snapshot_json"), {}) or {}
        if "ledger_json" in item:
            item["ledger"] = loads(item.pop("ledger_json"), {}) or {}
        out.append(item)
    return out


# ---------------------------------------------------------------- mooya 底账（temu.listed.source: mooya）


def pull_due(ctx: Ctx) -> bool:
    if not source_mooya(ctx) or not enabled(ctx):
        return False
    last = parse_iso(ctx.db.get_meta(PULLED_META_KEY))
    minutes = float(ctx.setting("temu.listed.pull_minutes", 2) or 0)
    return last is None or utcnow() - last >= dt.timedelta(minutes=minutes, seconds=-15)


def _mirror_values(client: str, item: dict[str, Any]) -> dict[str, Any]:
    """mooya 底账的一行 → listed_rows 的镜像列（快照里补上店铺列名与上架图包，program_cells 照旧用它们）。"""
    snap = dict(item.get("snapshot") or {})
    store_ = item.get("store") or {}
    snap["store"] = snap.get("store") or store_.get("column") or store_.get("key")
    package = item.get("package") or None
    if package and package.get("url"):
        snap["package"] = {"url": package["url"], "expires_at": package.get("expiresAt"), "filename": package.get("filename")}
    else:
        snap.pop("package", None)
    origin = snap.get("origin") or {}
    human = item.get("human") or {}
    ledger = {
        k: item.get(k)
        for k in (
            "id", "tenant", "store", "source", "listingId", "packageId", "mainImage", "package", "titleSync",
            "temuGoods", "humanUpdatedAt", "humanUpdatedBy", "updatedAt",
        )
    }
    return {
        "client": client,
        "store_key": str(store_.get("key") or ""),
        "mooya_id": item.get("id"),
        "asset_id": origin.get("asset_id") or None,
        "seed_id": origin.get("seed_id") or None,
        "title": item.get("title"),
        "detail_id": item.get("detailId"),
        "listed_at": item.get("listedAt") or item.get("updatedAt") or now_iso(),
        "snapshot_json": dumps(snap),
        "ledger_json": dumps(ledger),
        **{column: human.get(column) for column, _kind in HUMAN_COLUMNS.values()},
        "mooya_updated_at": item.get("updatedAt"),
    }


def _mirror(ctx: Ctx, client: str, item: dict[str, Any]) -> bool:
    """镜像一行；新行如果本机旧底账（listed_products）已经写过飞书，接着用那一行（不重建）。返回镜像列有没有变。"""
    values = _mirror_values(client, item)
    code = str(item["code"])
    old = ctx.db.q1("SELECT * FROM listed_rows WHERE code = ?", (code,))
    now = now_iso()
    with ctx.db.tx() as conn:
        if old:
            if all(old[c] == values[c] for c in MIRROR_COLUMNS):
                return False
            sets = ", ".join(f"{c} = ?" for c in MIRROR_COLUMNS)
            conn.execute(
                f"UPDATE listed_rows SET {sets}, updated_at = ? WHERE code = ?",
                (*(values[c] for c in MIRROR_COLUMNS), now, code),
            )
            return True
        legacy = conn.execute("SELECT record_id FROM listed_products WHERE code = ?", (code,)).fetchone()
        cols = ", ".join(MIRROR_COLUMNS)
        marks = ", ".join("?" for _ in MIRROR_COLUMNS)
        conn.execute(
            f"INSERT INTO listed_rows(code, {cols}, record_id, attempts, created_at, updated_at)"
            f" VALUES(?, {marks}, ?, 0, ?, ?)",
            (code, *(values[c] for c in MIRROR_COLUMNS), legacy["record_id"] if legacy else None, now, now),
        )
    return True


def pull(ctx: Ctx) -> dict[str, Any]:
    """按客户端把 mooya 底账增量拉进 listed_rows（按 (updated_at, id) 翻页，游标记在 meta），顺带记下 mooya 报的店铺，
    再把本机引擎上、还没进 mooya 的推过去。有变化就拉起表格同步。"""
    out: dict[str, Any] = {"pulled": 0, "changed": [], "errors": []}
    if not source_mooya(ctx):
        return {**out, "skipped": "temu.listed.source 不是 mooya"}
    cursors = loads(ctx.db.get_meta(PULL_META_KEY), None) or {}
    known = mooya_stores(ctx)
    stores_changed = False
    with mooya.Pool(ctx) as pool:
        for client in ledger_clients(ctx):
            try:
                api = pool.get(client)
                found = {str(s["key"]): s for s in (api.temu_stores().get("stores") or []) if s.get("key")}
                if found != known.get(client):
                    known[client] = found
                    stores_changed = True
                cur = dict(cursors.get(client) or {})
                while True:
                    page = api.temu_listed(
                        changed_since=cur.get("changed_since"), after_id=int(cur.get("after_id") or 0), limit=_PULL_PAGE
                    )
                    items = page.get("items") or []
                    for item in items:
                        out["pulled"] += 1
                        if _mirror(ctx, client, item):
                            out["changed"].append(item["code"])
                    if items:
                        cur = {"changed_since": items[-1]["updatedAt"], "after_id": int(items[-1]["id"])}
                    if not page.get("next") or not items:
                        break
                cursors[client] = cur
            except mooya.MooyaError as exc:
                out["errors"].append({"client": client, "error": str(exc)[:300]})
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, PULL_META_KEY, dumps(cursors))
        ctx.db.set_meta(conn, PULLED_META_KEY, now_iso())
        if stores_changed:
            ctx.db.set_meta(conn, STORES_META_KEY, dumps(known))
    pushed = push_pending(ctx)
    if pushed.get("pushed"):
        out["pushed"] = pushed["pushed"]
    if pushed.get("errors"):
        out["errors"] += pushed["errors"]
    if out["changed"] or stores_changed:
        feishu_table.kick(ctx)
    return out


def pull_now(ctx: Ctx) -> dict[str, Any] | None:
    """到点才拉（巡检里调）；拉不成只记原因。"""
    if not pull_due(ctx):
        return None
    try:
        return pull(ctx)
    except _FAILURES as exc:
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, PULLED_META_KEY, now_iso())
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def push_pending(ctx: Ctx, codes: list[str] | None = None) -> dict[str, Any]:
    """本机底账（listed_products）里有、mooya 镜像里还没有的款推给 mooya（`/temu/listings/import`，同一 ref 不重复建）。
    推成功的下一次拉底账就会出现在 listed_rows 里。"""
    from . import mooya_listing  # 搬单要读上架的很多表，用时再导入

    if not source_mooya(ctx):
        return {}
    sql = "SELECT p.code FROM listed_products p LEFT JOIN listed_rows r ON r.code = p.code WHERE r.code IS NULL"
    pending = [r["code"] for r in ctx.db.q(sql)]
    if codes is not None:
        pending = [c for c in pending if c in set(codes)]
    if not pending:
        return {}
    try:
        return mooya_listing.export(ctx, codes=pending, listed_only=True)
    except (mooya.MooyaError, StageError) as exc:
        return {"errors": [{"push": f"{type(exc).__name__}: {exc}"[:300]}]}


# ---------------------------------------------------------------- 程序列


def _package(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any] | None:
    """上架图包（没有或快到期时打 / 续签），记在快照里。读 mooya 底账时由 mooya 打包、续签，这里只用它给的链接。"""
    if source_mooya(ctx):
        return item["snapshot"].get("package") or None
    snap = item["snapshot"]
    pkg = snap.get("package") or {}
    expires = parse_iso(pkg.get("expires_at"))
    refresh = utcnow() + dt.timedelta(days=int(ctx.setting("table.link_refresh_days", 15)))
    if pkg.get("url") and expires and expires > refresh:
        return pkg
    filename = f"{item['code']}_上架图包.zip"
    if pkg.get("key"):
        fresh = temu_assets.resign(ctx, pkg["key"], filename=filename, fallback=f"{item['code']}.zip")
        pkg = {**pkg, **fresh}
    else:
        listing_item = temu_assets.get(ctx, item["asset_id"], snap.get("set_id") or "")
        if not listing_item or not listing_item.get("files"):
            return None
        pkg = temu_assets.package(ctx, listing_item, item["code"])
    snap["package"] = pkg
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE listed_products SET snapshot_json = ?, updated_at = ? WHERE code = ?",
            (dumps(snap), now_iso(), item["code"]),
        )
    return pkg


def program_cells(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any]:
    """一条已上架的款在飞书里的程序列（不含主图附件：附件只在建行时传一次）。开着 Temu 接口时连 Temu 状态等三列。"""
    from . import title_sync  # 标题同步依赖本模块，用时再导入

    snap = item["snapshot"]
    pkg = snap.get("package") or {}
    prices = "；".join(
        f"{p['sku']} {p['price']}{(' ' + p['currency']) if p.get('currency') else ''}".strip()
        for p in snap.get("prices") or []
    )
    cells: dict[str, Any] = {
        F_CODE: item["code"],
        F_STORE: snap.get("store"),
        F_TITLE: item.get("title"),
        F_DATE: _day_ms(ctx, item["listed_at"]),
        F_ASSET: snap.get("label"),
        F_TASK: snap.get("task"),
        F_MAIN: snap.get("main_kind"),
        F_ORDER: snap.get("image_order"),
        F_PRICES: prices or None,
        F_DETAIL: item.get("detail_id"),
    }
    if pkg.get("url"):
        cells[F_PACKAGE] = {"link": pkg["url"], "text": f"{item['code']}_上架图包.zip"}
    if _has_title_sync(ctx, item):
        if source_mooya(ctx):
            cells[F_TITLE_SYNC] = ((item.get("ledger") or {}).get("titleSync") or {}).get("text")
        else:
            cells[F_TITLE_SYNC] = title_sync.cell_text(ctx, item["asset_id"])
    if _api_owned_row(ctx, item):
        cells[F_STATUS] = item.get("temu_status")
        cells[F_PRODUCT] = item.get("temu_product_id")
        if item.get("product_url"):
            cells[F_URL] = {"link": item["product_url"], "text": item["product_url"]}
    return {k: v for k, v in cells.items() if v not in (None, "")}


def _has_title_sync(ctx: Ctx, item: dict[str, Any]) -> bool:
    """这一条的表已经有「标题同步」这一列（新建的表建表时就有，旧表补过）。"""
    bid = place(ctx, item, freeze=False)
    return int((_meta(ctx, bid) or {}).get("fields_version") or 1) >= FIELDS_VERSION


def _hash(cells: dict[str, Any]) -> str:
    return sha256_text(dumps(cells))[:16]


def _human_cells(item: dict[str, Any], columns: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """重建被删掉的行时把人填过的值一起写回去。"""
    out: dict[str, Any] = {}
    for name, (column, kind) in (columns or HUMAN_COLUMNS).items():
        value = item.get(column)
        if value in (None, ""):
            continue
        if kind == "link":
            out[name] = {"link": value, "text": value}
        elif kind == "date":
            try:
                local = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            except ValueError:
                continue
            out[name] = int(local.timestamp() * 1000)
        else:
            out[name] = value
    return out


# ---------------------------------------------------------------- 飞书表


def _ensure_table(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], bid: str | None = None
) -> dict[str, Any]:
    """「已上架的款」数据表，还没有就建（同一个多维表格里），建好视图：全部 / 每家店 / 看图。
    周表格的从那本的「已上架的款模板」复制（复制件落在「模板」文件夹里，挪回根目录）。
    精作款那一天的表由 crafted.ready_day 建（sync_table 先建好再进来）。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    meta = _meta(ctx, bid)
    if meta.get("table_id"):
        if meta.get("unmoved"):
            feishu_table.move_table(api, info, meta["table_id"], None)
            meta.pop("unmoved")
            _save_meta(ctx, meta, bid)
        return meta
    if crafted.is_key(bid):
        raise KeyError(f"精作款 {crafted.day_of(str(bid))} 那天的表还没建好")
    if bid:
        return _copy_table(ctx, api, info, bid)
    path = f"/bitable/v1/apps/{info['app_token']}/tables"
    body = {"table": {"name": TABLE_NAME, "default_view_name": "全部", "fields": field_specs(ctx)}}
    try:
        table_id = api.call("POST", path, json=body)["table_id"]
    except feishu.FeishuError:
        # 多半是同名的表已经有了（库重建过）：换个名字另建一张
        body["table"]["name"] = f"{TABLE_NAME} {utcnow():%m%d%H%M}"
        table_id = api.call("POST", path, json=body)["table_id"]
    meta = {
        "table_id": table_id,
        "name": body["table"]["name"],
        "created_at": now_iso(),
        "fields_version": FIELDS_VERSION,
    }
    _save_meta(ctx, meta)
    with ctx.db.tx() as conn:
        log_event(conn, "table", "listed_table_created", {"table_id": table_id})
    return meta


def _ensure_fields(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], meta: dict[str, Any], bid: str | None = None
) -> dict[str, Any]:
    """之前建的表（或从模板复制的）补上新版本加的列（「标题同步」），补过记 fields_version。"""
    if int(meta.get("fields_version") or 1) >= FIELDS_VERSION:
        return meta
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{meta['table_id']}/fields"
    have = {f.get("field_name") for f in views.list_fields(api, info, meta["table_id"])}
    added = []
    for spec in field_specs(ctx):
        if spec["field_name"] not in have:
            api.call("POST", path, json=spec)
            added.append(spec["field_name"])
    meta["fields_version"] = FIELDS_VERSION
    _save_meta(ctx, meta, bid)
    if added:
        with ctx.db.tx() as conn:
            log_event(conn, "table", "listed_table_fields_added", {"table_id": meta["table_id"], "added": added})
    return meta


def _copy_table(ctx: Ctx, api: feishu.Client, info: dict[str, Any], bid: str) -> dict[str, Any]:
    from . import books  # 每周选款表依赖表格模块，放到用时再导入

    tpl = ((books.book(ctx, bid) or {}).get("templates") or {}).get("listed") or {}
    if not tpl.get("table_id"):
        raise KeyError(f"周表格 {bid} 里还没认到「{books.LISTED_TEMPLATE}」")
    table_id, name = books._copy_table(api, info["app_token"], tpl["table_id"], TABLE_NAME)
    books.register(ctx, table_id, info["app_token"], "listed")
    # 复制件的视图 id 和模板里的一样（全部 / 看图；每家店的视图后面补）
    meta = {
        "table_id": table_id,
        "name": name,
        "created_at": now_iso(),
        "views": dict(tpl.get("views") or {}),
        "unmoved": True,
    }
    _save_meta(ctx, meta, bid)
    with ctx.db.tx() as conn:
        log_event(conn, "table", "listed_table_created", {"table_id": table_id, "book_id": bid})
    feishu_table.move_table(api, info, table_id, None)
    meta.pop("unmoved")
    _save_meta(ctx, meta, bid)
    return meta


def _ensure_views(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    meta: dict[str, Any],
    bid: str | None = None,
) -> dict[str, Any]:
    """每家店一个表格视图（按店铺筛）、画册「看图」（封面主图）；新加的店补一个视图。"""
    table_id = meta["table_id"]
    want = _columns_for(ctx, bid)
    have = meta.get("views") or {}
    if have.get("all") and all(c in (have.get("stores") or {}) for c in want) and have.get("gallery"):
        return meta
    fields = views.list_fields(api, info, table_id)
    existing = views.list_views(api, info, table_id)
    # 记下的视图 id 以表里现在有的为准（整本复制过来的表，模板里记的 id 可能对不上）
    present = {v["view_id"] for v in existing}
    first = next(v for v in existing if v.get("view_type") == "grid")["view_id"]
    store_field = next(f for f in fields if f.get("field_name") == F_STORE)
    by_store = {c: v for c, v in (have.get("stores") or {}).items() if v in present}
    for column in want:
        if column in by_store:
            continue
        view_id = next(
            (v["view_id"] for v in existing if v.get("view_type") == "grid" and v.get("view_name") == column),
            None,
        ) or views._create(api, info, table_id, column, "grid")
        views._patch(api, info, table_id, view_id, filter_info=views.is_option(store_field, column))
        by_store[column] = view_id
    gallery = (have.get("gallery") if have.get("gallery") in present else None) or next(
        (v["view_id"] for v in existing if v.get("view_type") == "gallery"), None
    ) or views._create(api, info, table_id, views.GALLERY_VIEW, "gallery")
    cover = next((f["field_id"] for f in fields if f.get("field_name") == F_THUMB), None)
    if cover:
        views.set_cover(api, info, table_id, gallery, cover)
    meta["views"] = {"all": first, "stores": by_store, "gallery": gallery}
    _save_meta(ctx, meta, bid)
    return meta


def _sync_options(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    meta: dict[str, Any],
    bid: str | None = None,
) -> None:
    """店铺列补上新加的店铺（全量覆盖：原有选项带 id 回写）；读 mooya 底账时连 mooya 那边的店（1店 / 2店）。"""
    columns = _columns_for(ctx, bid)
    if meta.get("store_options") == columns:
        return
    table_id = meta["table_id"]
    field = next(f for f in views.list_fields(api, info, table_id) if f.get("field_name") == F_STORE)
    current = list((field.get("property") or {}).get("options") or [])
    have = {o.get("name") for o in current}
    missing = [c for c in columns if c not in have]
    if missing:
        api.call(
            "PUT",
            f"/bitable/v1/apps/{info['app_token']}/tables/{table_id}/fields/{field['field_id']}",
            json={
                "field_name": F_STORE,
                "type": field["type"],
                "property": {
                    **(field.get("property") or {}),
                    "options": [{k: o[k] for k in ("id", "name", "color") if k in o} for o in current]
                    + [{"name": c} for c in missing],
                },
                "description": _help(PROGRAM_HELP),
            },
        )
    meta["store_options"] = columns
    _save_meta(ctx, meta, bid)


def _pending(ctx: Ctx) -> list[dict[str, Any]]:
    """要写飞书的：还没有行（或行被删了）、程序列变了、上架图包链接快到期。
    读 mooya 底账时上架图包由 mooya 打包续签（链接换了程序列就变了），人工款没有图包也不算要写。
    精作款模板还没锁上时，要进精作款的行先不写（哪里都不写）。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    mooya_mode = source_mooya(ctx)
    hold_crafted = crafted.enabled(ctx) and not crafted.ready(ctx)
    refresh = utcnow() + dt.timedelta(days=int(ctx.setting("table.link_refresh_days", 15)))
    out = []
    for item in rows(ctx):
        if int(item["attempts"] or 0) >= feishu_table.MAX_ATTEMPTS:
            continue
        if hold_crafted and crafted.is_crafted(ctx, item) and crafted.place(ctx, item, freeze=False):
            continue
        if mooya_mode:
            if not item["record_id"] or item["written_hash"] != _hash(program_cells(ctx, item)):
                out.append(item)
            continue
        pkg = item["snapshot"].get("package") or {}
        expires = parse_iso(pkg.get("expires_at"))
        if (
            not item["record_id"]
            or item["written_hash"] != _hash(program_cells(ctx, item))
            or not pkg.get("url")
            or (expires and expires < refresh)
        ):
            out.append(item)
    return out


def has_work(ctx: Ctx) -> bool:
    from . import crafted  # 精作款依赖本模块，用时再导入

    if not enabled(ctx) or not feishu_table.base(ctx):
        return False
    return bool(_pending(ctx)) or crafted.has_work(ctx)


def sync_table(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    errors: list[dict[str, str]],
    *,
    batch: int | None = None,
) -> dict[str, Any]:
    """表格同步时把已上架的款写进飞书（调用方持有选款表的同步租约）：建表、补视图和店铺选项、建行（带主图）、改程序列。
    info 是旧表格的；归到周表格的写进那本周表格的「已上架的款」（那本还没有就先建），精作款写进那一天的表（那天还没有就先建）。
    batch 是这一次最多写几行（缺省 temu.listed.sync_batch）。"""
    from . import books, crafted  # 每周选款表、精作款依赖表格模块，放到用时再导入

    if not enabled(ctx) or not info:
        return {}
    prepared = crafted.prepare(ctx, api, errors)
    pending = _pending(ctx)
    if not pending:
        return {"crafted": prepared} if prepared else {}
    if source_mooya(ctx):
        # 第一次补 1店 / 2店上千款：每次同步只写一批（先补已有行的改动，再按上架时间建新行），剩下的下次接着写
        size = int(batch if batch is not None else ctx.setting("temu.listed.sync_batch", 80) or 0)
        if size > 0 and len(pending) > size:
            pending = sorted(pending, key=lambda i: (not i["record_id"], i["listed_at"] or ""))[:size]
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for item in pending:
        groups.setdefault(place(ctx, item), []).append(item)
    out: dict[str, Any] = {"created": 0, "updated": 0}
    if prepared:
        out["crafted"] = prepared
    for bid in sorted(groups, key=lambda b: b or ""):
        where = info
        if crafted.is_key(bid):
            day = crafted.day_of(str(bid))
            try:
                found = crafted.ready_day(ctx, api, day)
            except KeyError as exc:
                reason = str(exc)[:300]
                errors.append({"table": f"精作款（{day}）", "error": reason})
                out["error"] = reason
                continue
            where = {"app_token": found["app_token"], "url": found.get("url")}
        elif bid:
            try:
                found = books.ensure_book(ctx, api, books.week_monday(bid))
            except (*_FAILURES, StageError) as exc:
                reason = f"{type(exc).__name__}: {exc}"[:300]
                errors.append({"table": f"{TABLE_NAME}（{bid}）", "error": reason})
                out["error"] = reason
                continue
            where = {"app_token": found["app_token"], "url": found.get("url")}
        part = _sync_place(ctx, api, where, bid, groups[bid], errors)
        out["created"] += part.get("created", 0)
        out["updated"] += part.get("updated", 0)
        if part.get("error"):
            out["error"] = part["error"]
    return out


def _sync_place(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    bid: str | None,
    pending: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    out = {"created": 0, "updated": 0}
    try:
        meta = _ensure_table(ctx, api, info, bid)
        meta = _ensure_fields(ctx, api, info, meta, bid)
        _sync_options(ctx, api, info, meta, bid)
        _ensure_views(ctx, api, info, meta, bid)
    except _FAILURES as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        errors.append({"table": _label(bid), "error": reason})
        return {"error": reason}
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{meta['table_id']}/records"
    table = _table(ctx)
    for item in pending:
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
        try:
            _package(ctx, item)
            item = next(r for r in rows(ctx) if r["code"] == item["code"])
            cells = program_cells(ctx, item)
            if item["record_id"]:
                api.call(
                    "PUT", f"{path}/{item['record_id']}", params={"user_id_type": "open_id"}, json={"fields": cells}
                )
                record_id = item["record_id"]
                out["updated"] += 1
            else:
                fields = {**cells, **_human_cells(item, _human_columns_row(ctx, item))}
                data = _main_image(ctx, item)
                if data:
                    fields[F_THUMB] = [
                        {"file_token": feishu_table.upload_image(api, info["app_token"], f"{item['code']}.jpg", data)}
                    ]
                record_id = api.call("POST", path, params={"user_id_type": "open_id"}, json={"fields": fields})[
                    "record"
                ]["record_id"]
                out["created"] += 1
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:300]
            errors.append({"code": item["code"], "error": reason})
            with ctx.db.tx() as conn:
                conn.execute(
                    f"UPDATE {table} SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE code = ?",
                    (reason, now_iso(), item["code"]),
                )
            continue
        with ctx.db.tx() as conn:
            conn.execute(
                f"UPDATE {table} SET record_id = ?, written_hash = ?, attempts = 0, last_error = NULL,"
                " updated_at = ? WHERE code = ?",
                (record_id, _hash(cells), now_iso(), item["code"]),
            )
    return out


def _main_image(ctx: Ctx, item: dict[str, Any]) -> bytes | None:
    """建行时传一次的主图缩略图：本机底账用本机的上架图；mooya 底账要么是妙手图链接（人工款），要么走带令牌的图片接口。"""
    if not source_mooya(ctx):
        first = (item["snapshot"].get("images") or [{}])[0]
        return feishu_table.thumb_bytes(ctx, None, first["path"]) if first.get("path") else None
    found = (item.get("ledger") or {}).get("mainImage") or {}
    # 图没了（妙手会清图片空间，人工款的链接大多 404；mooya 也可能找不到）就不带主图建行，网络错照常重试
    if found.get("url"):
        with http.make_client(30) as client:
            resp = client.get(found["url"])
            if resp.status_code in GONE_STATUSES:
                return None
            resp.raise_for_status()
            data = resp.content
    elif found.get("path"):
        try:
            with mooya.Client(ctx, client=item["client"]) as api:
                data = api.temu_listed_image(int(item["mooya_id"]))
        except mooya.MooyaError as exc:
            if exc.status in GONE_STATUSES:
                return None
            raise
    else:
        return None
    return images.thumbnail_jpeg(data)


def sync_now(ctx: Ctx, *, batch: int | None = None) -> dict[str, Any]:
    """刚记了已上架的款：拿选款表的同步租约当场写（拿不到就交给下一次表格同步）。batch 是这一次最多写几行。"""
    if not enabled(ctx):
        return {}
    info = feishu_table.base(ctx)
    if not info:
        return {}
    if not lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS):
        feishu_table.kick(ctx)
        return {"deferred": "表格正在同步"}
    errors: list[dict[str, str]] = []
    try:
        with feishu.Client(ctx) as api:
            out = sync_table(ctx, api, info, errors, batch=batch)
    except _FAILURES as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    return {**out, "errors": errors[:5]} if errors else out


# ---------------------------------------------------------------- 回读人填的列


def _value(kind: str, cell: Any, ctx: Ctx) -> Any:
    if cell in (None, "", []):
        return None
    if kind == "link":
        if isinstance(cell, dict):
            return (cell.get("link") or cell.get("text") or "").strip() or None
        return listing._text(cell)
    if kind in ("int", "float"):
        try:
            number = float(listing._text(cell) if not isinstance(cell, (int, float)) else cell)
        except (TypeError, ValueError):
            return None
        return int(number) if kind == "int" else round(number, 2)
    if kind == "date":
        return listing._day(ctx, cell)
    return listing._text(cell)


def read_back(ctx: Ctx, api: feishu.Client | None = None) -> dict[str, Any]:
    """回读「已上架的款」：人填列写进库（变了才写）；程序列被改了、行被删了的标记下次同步改回 / 重建。
    旧表格和各本周表格的「已上架的款」、精作款每一天的表各读各的，每张只认归到它的那些条。"""
    places = _places(ctx)
    if not places:
        return {"skipped": "还没有「已上架的款」表"}
    if api is None:
        with feishu.Client(ctx) as client:
            return read_back(ctx, client)
    out: dict[str, Any] = {
        "read": 0, "changed": [], "restore": [], "rewrite": [], "unknown_rows": 0, "edits": 0, "titles": []
    }
    items = rows(ctx)
    # 每条归到哪一张只算一次（精作款几十张表，按表逐条算太慢）
    by_place: dict[str | None, list[dict[str, Any]]] = {}
    for item in items:
        by_place.setdefault(place(ctx, item, freeze=False), []).append(item)
    for bid, info, meta in places:
        part = _read_place(ctx, api, info, meta, by_place.get(bid) or [])
        for key in ("read", "unknown_rows", "edits", "changed", "restore", "rewrite", "titles"):
            out[key] += part[key]
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, READ_META_KEY, now_iso())
    if out["titles"]:
        from . import temu  # 上架模块依赖本模块，用时再导入

        # 「选中的款」的上架标题、两张表的「标题同步」马上跟着写
        temu.write_now(ctx, sorted({t["asset_id"] for t in out["titles"]}))
    if out["restore"] or out["rewrite"] or out["titles"]:
        feishu_table.kick(ctx)
    return out


def _read_place(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    meta: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    from . import cells as cellmod, title_sync  # 都依赖本模块之外的很多表，用时再导入

    records = cellmod.read_table(api, info, meta["table_id"])
    changed: list[str] = []
    restore: list[str] = []
    rewrite: list[str] = []
    by_code = {listing._text(r["fields"].get(F_CODE)): rid for rid, r in records.items()}
    known = set()
    now = now_iso()
    mooya_mode = source_mooya(ctx)
    table = _table(ctx)
    refs: dict[str, dict[str, Any]] = {}
    wants: dict[str, dict[str, Any]] = {}
    metrics: list[dict[str, Any]] = []
    # 读 mooya 底账时人填的值不直接写本机，先攒着、按货号写回 mooya（回来的那一行再镜像）
    human_pushes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    row_columns: dict[str, dict[str, tuple[str, str]]] = {}
    with ctx.db.tx() as conn:
        for item in items:
            record_id = item["record_id"]
            if record_id not in records:
                # 行被删了（或换了 id：按货号找回来）
                found = by_code.get(item["code"])
                if found:
                    conn.execute(
                        f"UPDATE {table} SET record_id = ?, written_hash = NULL, updated_at = ? WHERE code = ?",
                        (found, now, item["code"]),
                    )
                    record_id = found
                elif record_id:
                    conn.execute(
                        f"UPDATE {table} SET record_id = NULL, written_hash = NULL, updated_at = ? WHERE code = ?",
                        (now, item["code"]),
                    )
                    restore.append(item["code"])
                    continue
                else:
                    continue
            known.add(record_id)
            refs[record_id] = {"asset_id": item["asset_id"], "code": item["code"]}
            cells = records[record_id]["fields"]
            columns = _human_columns_row(ctx, item)
            row_columns[record_id] = columns
            checked = (*CHECKED_PROGRAM, *(API_COLUMNS if _api_owned_row(ctx, item) else ()))
            values = {column: _value(kind, cells.get(name), ctx) for name, (column, kind) in columns.items()}
            diff = {c: v for c, v in values.items() if v != item.get(c)}
            if diff:
                if mooya_mode:
                    human_pushes[item["code"]] = (item, diff)
                else:
                    sets = ", ".join(f"{c} = ?" for c in values)
                    conn.execute(
                        f"UPDATE listed_products SET {sets}, human_read_at = ?, updated_at = ? WHERE code = ?",
                        (*values.values(), now, now, item["code"]),
                    )
                changed.append(item["code"])
            # 每天一条（值变了另记一条）：人填的销量 / 状态的时间序列（读 mooya 底账时由 mooya 记）
            if not mooya_mode:
                metrics.append({**item, **values})
            want = program_cells(ctx, item)
            wants[record_id] = want
            if any(
                listing._text(cells.get(name)) != listing._text(want.get(name))
                for name in checked
            ):
                conn.execute(
                    f"UPDATE {table} SET written_hash = NULL, updated_at = ? WHERE code = ?",
                    (now, item["code"]),
                )
                rewrite.append(item["code"])
        if changed or restore or rewrite:
            log_event(
                conn, "table", "listed_table_read", {"changed": changed[:20], "restore": restore, "rewrite": rewrite}
            )
    for item in metrics:
        record_metrics(ctx, item, source="human")
    # 人改的每一格记日志；「上架标题」是指令列：交给 title_sync（读 mooya 底账时交给 mooya）
    columns = human_columns(ctx) if not mooya_mode else dict(HUMAN_COLUMNS)
    program_names = set(want_names()) - set(columns)

    def program(record_id: str, name: str) -> Any:
        want = wants.get(record_id) or {}
        if name in program_names:
            return want.get(name)
        return cellmod.MISSING

    edits = cellmod.observe(
        ctx,
        kind=cellmod.KIND_LISTED,
        table_id=meta["table_id"],
        records=records,
        refs=refs,
        ignore=(F_THUMB, F_PACKAGE, F_TITLE_SYNC),
        program=program,
        revert=set(program_names) - {F_TITLE},
        full=True,
    )
    pushed_errors: list[dict[str, str]] = []
    if mooya_mode and human_pushes:
        by_code_edit: dict[str, dict[str, Any]] = {}
        for edit in edits:
            if edit.get("code") and edit["field"] in columns:
                by_code_edit[edit["code"]] = edit
        pushed_errors = _push_human(ctx, human_pushes, by_code_edit)
    titles = []
    for edit in edits:
        if edit["field"] != F_TITLE:
            continue
        if mooya_mode:
            item = next((i for i in items if i["code"] == edit["code"]), None)
            if item is None:
                continue
            result = _title_to_mooya(ctx, item, str(edit["new"] or ""), by=edit["by"] or "feishu:已上架的款")
        elif not edit["asset_id"]:
            continue
        else:
            result = title_sync.handle_edit(
                ctx, edit["asset_id"], str(edit["new"] or ""), by=edit["by"] or "feishu:已上架的款", via=cellmod.KIND_LISTED
            )
        if result.get("same"):
            continue
        cellmod.set_outcome(
            ctx, edit["log_id"], cellmod.OUTCOME_REJECTED if result.get("rejected") else cellmod.OUTCOME_ACCEPTED, result.get("rejected")
        )
        titles.append({"asset_id": edit["asset_id"], "code": edit["code"], **result})
        if not result.get("rejected") and not mooya_mode:
            _retitle(ctx, edit["asset_id"])
    out = {
        "read": len(records),
        "changed": changed,
        "restore": restore,
        "rewrite": rewrite,
        "unknown_rows": len(set(records) - known),
        "edits": len(edits),
        "titles": titles,
    }
    if pushed_errors:
        out["push_errors"] = pushed_errors
    return out


def _push_human(
    ctx: Ctx,
    pushes: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    edits: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """人在飞书改的人填列写回 mooya（修改人 / 时间取这一行最近一次改动的那一格），回来的那一行镜像进 listed_rows。
    没写成的不动本机镜像：下次回读还是对不上，接着写。"""
    errors = []
    with mooya.Pool(ctx) as pool:
        for code, (item, diff) in pushes.items():
            edit = edits.get(code) or {}
            try:
                result = pool.get(item["client"]).temu_listed_human(
                    code, diff, editor=edit.get("by") or "feishu:已上架的款", edited_at=edit.get("at"),
                    via="feishu", store=item.get("store_key"),
                )
            except mooya.MooyaError as exc:
                errors.append({"code": code, "error": str(exc)[:300]})
                continue
            if result.get("item"):
                _mirror(ctx, item["client"], result["item"])
            with ctx.db.tx() as conn:
                conn.execute("UPDATE listed_rows SET human_read_at = ? WHERE code = ?", (now_iso(), code))
    return errors


def _title_to_mooya(ctx: Ctx, item: dict[str, Any], title: str, *, by: str) -> dict[str, Any]:
    """「已上架的款」改了上架标题（读 mooya 底账时）：交 mooya 复核并同步（没接 Temu 接口时记「要人去 Temu 改」）。
    合规的、这一款是本机的（有 asset_id）也记进本机标题，「选中的款」跟着换；返回 {same | rejected | saved, state}。"""
    text = (title or "").strip()
    if not text or text == (item.get("title") or "").strip():
        return {"same": True}
    try:
        with mooya.Client(ctx, client=item["client"]) as api:
            result = api.temu_listed_title(item["code"], text, editor=by, via="feishu", store=item.get("store_key"))
    except mooya.MooyaError as exc:
        if exc.status == 409 and isinstance(exc.body, dict):
            if exc.body.get("item"):
                _mirror(ctx, item["client"], exc.body["item"])
            return {"rejected": "；".join(exc.problems[:4]) or str(exc), "state": exc.body.get("state")}
        return {"rejected": f"没交给 mooya：{exc}"[:300]}
    if result.get("item"):
        _mirror(ctx, item["client"], result["item"])
    if result.get("same"):
        return {"same": True}
    if item.get("asset_id"):
        try:
            temu_title.save_human(ctx, item["asset_id"], text, by=by, via="listed")
        except StageError:
            pass
    return {"saved": True, "state": result.get("state")}


def want_names() -> tuple[str, ...]:
    """程序会写的列名（主图、上架图包这种附件 / 链接列除外）。"""
    return (F_CODE, F_STORE, F_TITLE, F_DATE, F_ASSET, F_TASK, F_MAIN, F_ORDER, F_PRICES, F_DETAIL, F_STATUS, F_PRODUCT, F_URL)


def _retitle(ctx: Ctx, asset_id: str) -> None:
    """人在「已上架的款」改的标题存下之后：这一款各店的已上架记录跟着换标题（下次同步写回飞书两张表）。
    读 mooya 底账时标题以 mooya 为准（改标题交给 mooya，拉回来就是新的），这里不改。"""
    if source_mooya(ctx):
        return
    title = (temu_title.get(ctx, asset_id) or {}).get("title")
    if not title:
        return
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE listed_products SET title = ?, written_hash = NULL, updated_at = ? WHERE asset_id = ?",
            (title, now_iso(), asset_id),
        )


def record_metrics(ctx: Ctx, item: dict[str, Any], *, source: str, extra: dict[str, Any] | None = None) -> bool:
    """记一条已上架的款的数据（时间序列）：和上一条一样、又是同一天的不记。返回记了没有。
    读 mooya 底账时数据的时间序列在 mooya（人改的值写回 mooya 时记），本机不再记。"""
    if source_mooya(ctx):
        return False
    day = listing._day(ctx, int(utcnow().timestamp() * 1000))
    values = (
        item.get("temu_status"),
        item.get("sales_total"),
        item.get("sales_7d"),
        item.get("revenue_total"),
    )
    last = ctx.db.q1(
        "SELECT * FROM listed_metrics WHERE code = ? AND source = ? ORDER BY metric_id DESC LIMIT 1",
        (item["code"], source),
    )
    if last and last["observed_on"] == day and (
        last["temu_status"],
        last["sales_total"],
        last["sales_7d"],
        last["revenue_total"],
    ) == values:
        return False
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO listed_metrics(code, store_key, observed_on, temu_status, sales_total, sales_7d, revenue_total,"
            " extra_json, source, read_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["code"], item.get("store_key"), day, *values, dumps(extra) if extra else None, source, now_iso()),
        )
    return True


def metrics(ctx: Ctx, code: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in ctx.db.q("SELECT * FROM listed_metrics WHERE code = ? ORDER BY metric_id", (code,))
    ]


def due(ctx: Ctx) -> bool:
    if not enabled(ctx) or not _places(ctx):
        return False
    last = parse_iso(ctx.db.get_meta(READ_META_KEY))
    minutes = float(ctx.setting("temu.listed.read_minutes", 30))
    return last is None or utcnow() - last >= dt.timedelta(minutes=minutes, seconds=-30)


def read_due(ctx: Ctx) -> dict[str, Any] | None:
    """巡检兜底（table poll）用：读 mooya 底账时先到点拉一次底账；到点才回读，读不成只记原因，下一次巡检再试（不每分钟重试）。
    精作款正在迁移（那边的行正在删）时先不回读，迁完再来。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    pulled = pull_now(ctx)
    if not due(ctx) or crafted.migrating(ctx):
        return {"pull": pulled} if pulled else None
    try:
        out = read_back(ctx)
    except _FAILURES as exc:
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, READ_META_KEY, now_iso())
        out = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return {**out, "pull": pulled} if pulled else out


# ---------------------------------------------------------------- 状态、核对、导出


def status(ctx: Ctx) -> dict[str, Any]:
    from . import crafted  # 精作款依赖本模块，用时再导入

    items = rows(ctx)
    by_store: dict[str, int] = {}
    for item in items:
        # 读 mooya 底账时同一个店铺 key 两个客户端都有（1店 / 2店的精作款和这里上的），按「客户端/店铺」分开数
        key = f"{item['client']}/{item['store_key']}" if item.get("client") else item["store_key"]
        by_store[key] = by_store.get(key, 0) + 1
    extra = {"crafted": crafted.status(ctx)} if crafted.enabled(ctx) else {}
    return {
        **extra,
        "enabled": enabled(ctx),
        "source": SOURCE_MOOYA if source_mooya(ctx) else SOURCE_LOCAL,
        "url": table_url(ctx),
        "rows": len(items),
        "by_store": by_store,
        "pending": len(_pending(ctx)) if enabled(ctx) else 0,
        "read_at": ctx.db.get_meta(READ_META_KEY),
        "pulled_at": ctx.db.get_meta(PULLED_META_KEY) if source_mooya(ctx) else None,
        "not_pushed": [
            r["code"] for r in ctx.db.q(
                "SELECT p.code FROM listed_products p LEFT JOIN listed_rows r ON r.code = p.code WHERE r.code IS NULL"
            )
        ] if source_mooya(ctx) else [],
        "failed": [
            {"code": i["code"], "error": i["last_error"]}
            for i in items
            if int(i["attempts"] or 0) >= feishu_table.MAX_ATTEMPTS
        ],
    }


def check(ctx: Ctx) -> dict[str, Any]:
    """核对库和飞书：业务库里有、上架记录（store_listings）却不是 listed 的，上架了却没进库的，飞书里少的行；
    开着精作款时还有还在「已上架的款」里、没迁过去的精作款。"""
    from . import crafted  # 精作款依赖本模块，用时再导入

    listed = {
        r["code"]: r
        for r in stores.rows(ctx, active=False)
        if r["state"] == "listed" and r["code"]
    }
    items = {r["code"]: r for r in rows(ctx)}
    mine = {c: r for c, r in items.items() if not source_mooya(ctx) or r.get("asset_id")}
    out = {
        "missing_in_db": sorted(set(listed) - set(items)),
        "not_listed_anymore": sorted(set(mine) - set(listed)),
        "without_record": sorted(c for c, r in items.items() if not r["record_id"]),
    }
    if crafted.enabled(ctx):
        out["crafted_not_migrated"] = sorted(i["code"] for i in crafted.not_migrated(ctx))
    out["ok"] = not any(out.values())
    return out


EXPORT_COLUMNS = (
    "code",
    "store_key",
    "asset_id",
    "seed_id",
    "title",
    "listed_at",
    "detail_id",
    "temu_status",
    "temu_product_id",
    "product_url",
    "sales_total",
    "sales_7d",
    "revenue_total",
    "data_updated_on",
    "note",
)


def export_csv(ctx: Ctx, path: Path | None = None) -> dict[str, Any]:
    """导出全部已上架的款（含主图类型、图位、尺码价格），给统计分析用。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([*EXPORT_COLUMNS, "store", "main_kind", "image_order", "prices", "package_url"])
    items = rows(ctx)
    for item in items:
        snap = item["snapshot"]
        writer.writerow(
            [
                *(item.get(c) for c in EXPORT_COLUMNS),
                snap.get("store"),
                snap.get("main_kind"),
                snap.get("image_order"),
                dumps(snap.get("prices") or []),
                (snap.get("package") or {}).get("url"),
            ]
        )
    target = path or ctx.home / "exports" / f"listed-{utcnow():%Y%m%d%H%M}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(buf.getvalue(), encoding="utf-8-sig")
    return {"path": str(target), "rows": len(items)}


def backfill(ctx: Ctx) -> dict[str, Any]:
    """已经 listed、还没进 listed_products 的补记（record 当时失败、或老版本没记）；读 mooya 底账时再把没推过去的推给 mooya。"""
    if not stores.enabled(ctx):
        raise StageError("上架没打开")
    if source_mooya(ctx):
        local = {r["code"] for r in ctx.db.q("SELECT code FROM listed_products")}
        missing = sorted(
            r["code"] for r in stores.rows(ctx, active=False) if r["state"] == "listed" and r["code"] and r["code"] not in local
        )
    else:
        missing = check(ctx)["missing_in_db"]
    done = []
    for row in stores.rows(ctx, active=False):
        if row["code"] in missing:
            record(ctx, row)
            done.append(row["code"])
    out: dict[str, Any] = {"recorded": done}
    if source_mooya(ctx):
        pushed = push_pending(ctx)
        if pushed:
            out["pushed"] = pushed
    if done:
        feishu_table.kick(ctx)
    return out
