"""每周选款表（table.weekly，2026-10-29）：知识库根目录放一本锁住修改权限的「POD 选款表模板（勿改）」，每周从它复制一本
「POD 选款表 W44（10.26–11.01）」；周表格里每天一个文件夹「10-26 周一」，放那天上传的任务的候选表、当天的
「10-26 选中的款」和「10-26 已归档」子文件夹；「已上架的款」每本周表格一张（按上架日期归周）。

- 归属（seed_places）：任务按上传日期（timezone）归到那一天，第一次用到时定下来，之后不变（搬家由 migrate_week 改）。
  上传日期早于 table.weekly_since（缺省 2026-10-26）的，或开关打开时旧表格里已经有它的候选表 / 选中的款的，留在旧表格
  （「POD 毛毯选款表」，feishu_table.base()）。
- 模板表格（meta books.template）：三张模板表「选中的款模板」「候选表模板」「已上架的款模板」，照程序建这三种表的样子建。
  人在画册里设封面效果、卡片样式（接口改不了：两张模板的「看图」、候选表模板的「全部画册」「已选中」「待选」），周表格
  整本从它复制，这些设置跟着走。候选表模板的默认视图没跟上 candidates.VIEWS_VERSION 时，模板表格和各本周表格里的那一张
  在表格同步开头就地改（upgrade_candidate_templates，2026-10-30 视图版本 5、6）。锁法（lock_template）：开高级
  权限，「编辑者」角色所有表只读（Base v3 advperm / roles，权限 base:app:update、base:role:read / update）；可管理的人照样能改。
- 周表格（table_books）：知识库节点复制（POST /wiki/v2/spaces/:space/nodes/:node/copy，带 title），复制件带过来的高级权限关掉
  （v1 PUT apps/:app {"is_advanced": false}），三张模板表挪进「模板」文件夹，订阅这本表格的记录变更事件。
- 天（table_days）：「10-26 周一」文件夹 +「10-26 已归档」子文件夹（块接口 parent_id）+「10-26 选中的款」（从这本周表格的
  「选中的款模板」复制、挪进文件夹）。info_json 是那天「选中的款」的表格信息，口径同 feishu_table.base()（多了 day / book_id /
  folder_id / archive_folder_id），feishu_table 按份同步（旧表格一份 + 每天一份，scoped() 里只挑这一份的行）。
  「选中的款模板」的默认视图没跟上 feishu_table.SELECTED_VIEWS_VERSION 时，模板表格和各本周表格里的那一张在表格同步开头
  就地改（upgrade_selected_templates，2026-10-30 视图版本 2：全部 · 看图 · 待上架 · 上架中 · 已上架），新的一天复制出来
  就是这一套。
- 每周表格里程序复制、新建的表都登记在 table_places（table_registry 按它拼接口路径）。
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import feishu, feishu_table, packages, store, table_registry, views
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso, parse_iso, utcnow

TEMPLATE_META_KEY = "books.template"
TEMPLATE_TITLE = "POD 选款表模板（勿改）"
SELECTED_TEMPLATE = "选中的款模板"
CANDIDATE_TEMPLATE = "候选表模板"
LISTED_TEMPLATE = "已上架的款模板"
TEMPLATE_NAMES = {
    "selected": SELECTED_TEMPLATE,
    "candidate": CANDIDATE_TEMPLATE,
    "listed": LISTED_TEMPLATE,
}
TEMPLATE_FOLDER = "模板"
DEFAULT_SINCE = "2026-10-26"
WEEKDAYS = "一二三四五六日"
# 建模板表格 / 周表格 / 天、锁模板、订阅事件失败后隔这么久再试（table sync --retry 马上试）
RETRY_MINUTES = 60
# 搬一周的任务（maintenance migrate-week）时的租约：别的进程看到它就先不回读、不处理记录变更事件
MIGRATE_LEASE = "books.migrate"
MIGRATE_SECONDS = 3600
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)


# ---------------------------------------------------------------- 开关与命名


def enabled(ctx: Ctx) -> bool:
    """开着每周选款表：table.weekly，还要 table.enabled 和知识库（周表格都在知识库根目录）。"""
    return (
        feishu_table.enabled(ctx)
        and bool(ctx.setting("table.weekly", False))
        and bool(feishu_table.wiki_space_id(ctx))
    )


def since(ctx: Ctx) -> dt.date:
    return dt.date.fromisoformat(str(ctx.setting("table.weekly_since") or DEFAULT_SINCE))


def _tz(ctx: Ctx) -> ZoneInfo:
    return ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))


def _date(day: str | dt.date) -> dt.date:
    return day if isinstance(day, dt.date) else dt.date.fromisoformat(str(day))


def week_start(day: str | dt.date) -> dt.date:
    """这一周的周一（一周从周一到周日）。"""
    when = _date(day)
    return when - dt.timedelta(days=when.weekday())


def book_id(day: str | dt.date) -> str:
    year, week, _ = _date(day).isocalendar()
    return f"{year}-W{week:02d}"


def week_monday(bid: str) -> dt.date:
    """book_id（2026-W44）那一周的周一。"""
    year, week = bid.split("-W")
    return dt.date.fromisocalendar(int(year), int(week), 1)


def book_title(day: str | dt.date) -> str:
    """周表格的名字：POD 选款表 W44（10.26–11.01）。"""
    monday = week_start(day)
    sunday = monday + dt.timedelta(days=6)
    _, week, _ = monday.isocalendar()
    return f"POD 选款表 W{week:02d}（{monday:%m.%d}–{sunday:%m.%d}）"


def folder_name(day: str | dt.date) -> str:
    when = _date(day)
    return f"{when:%m-%d} 周{WEEKDAYS[when.weekday()]}"


def selected_name(day: str | dt.date) -> str:
    return f"{_date(day):%m-%d} 选中的款"


def archive_name(day: str | dt.date) -> str:
    return f"{_date(day):%m-%d} 已归档"


def local_day(ctx: Ctx, ts: str | None) -> dt.date | None:
    when = parse_iso(ts)
    return when.astimezone(_tz(ctx)).date() if when else None


# ---------------------------------------------------------------- 归属


def upload_day(ctx: Ctx, seed_id: str) -> dt.date | None:
    """任务（参考图）的上传日期（timezone）。"""
    row = ctx.db.q1("SELECT created_at FROM seeds WHERE seed_id = ?", (seed_id,))
    return local_day(ctx, row["created_at"]) if row else None


def _in_legacy(ctx: Ctx, seed_id: str) -> bool:
    """旧表格里已经有这个任务的候选表，或「选中的款」里已经有它的款。"""
    if ctx.db.q1("SELECT 1 FROM candidate_tables WHERE seed_id = ?", (seed_id,)):
        return True
    return bool(
        ctx.db.q1(
            "SELECT 1 FROM table_rows t JOIN assets a ON a.asset_id = t.asset_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " WHERE f.seed_id = ? AND t.record_id IS NOT NULL LIMIT 1",
            (seed_id,),
        )
    )


def place(ctx: Ctx, seed_id: str | None) -> str | None:
    """这个任务归到哪一天（"2026-10-26"）；None = 旧表格。第一次用到时（开着每周选款表）定下来记进 seed_places。"""
    if not seed_id:
        return None
    row = ctx.db.q1("SELECT day FROM seed_places WHERE seed_id = ?", (seed_id,))
    if row:
        return row["day"]
    if not enabled(ctx):
        return None
    when = upload_day(ctx, seed_id)
    day = None
    if when and when >= since(ctx) and not _in_legacy(ctx, seed_id):
        day = when.isoformat()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO seed_places(seed_id, day, placed_at) VALUES(?, ?, ?)"
            " ON CONFLICT(seed_id) DO NOTHING",
            (seed_id, day, now_iso()),
        )
    return place_of(ctx, seed_id)


def place_of(ctx: Ctx, seed_id: str) -> str | None:
    """只读：记下来的归属（没记过为 None）。"""
    row = ctx.db.q1("SELECT day FROM seed_places WHERE seed_id = ?", (seed_id,))
    return row["day"] if row else None


def set_place(ctx: Ctx, seed_id: str, day: str | None) -> None:
    """改归属（搬家用）。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO seed_places(seed_id, day, placed_at) VALUES(?, ?, ?)"
            " ON CONFLICT(seed_id) DO UPDATE SET day = excluded.day, placed_at = excluded.placed_at",
            (seed_id, day, now_iso()),
        )


def asset_seeds(ctx: Ctx, asset_ids: list[str] | None = None) -> dict[str, str]:
    """素材 → 任务（参考图）。"""
    sql = (
        "SELECT a.asset_id, f.seed_id FROM assets a"
        " JOIN variants v ON v.variant_id = a.variant_id"
        " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
    )
    if asset_ids is None:
        return {r["asset_id"]: r["seed_id"] for r in ctx.db.q(sql)}
    out: dict[str, str] = {}
    for i in range(0, len(asset_ids), 500):
        chunk = asset_ids[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        for r in ctx.db.q(f"{sql} WHERE a.asset_id IN ({marks})", tuple(chunk)):
            out[r["asset_id"]] = r["seed_id"]
    return out


def asset_day(ctx: Ctx, asset_id: str) -> str | None:
    seed = asset_seeds(ctx, [asset_id]).get(asset_id)
    return place(ctx, seed) if seed else None


@contextmanager
def scoped(ctx: Ctx, info: dict[str, Any] | None) -> Iterator[None]:
    """这一份「选中的款」（旧表格，或某一天）的同步：里面 table_registry.mine(素材) 只认归到这一份的款。"""
    day = (info or {}).get("day")
    seeds = asset_seeds(ctx)
    cache: dict[str, str | None] = {}

    def resolve(asset_id: str) -> str | None:
        if asset_id not in cache:
            seed = seeds.get(asset_id) or asset_seeds(ctx, [asset_id]).get(asset_id)
            cache[asset_id] = place(ctx, seed) if seed else None
        return cache[asset_id]

    with table_registry.scope(day, resolve):
        yield


# ---------------------------------------------------------------- 登记与小工具


def register(
    ctx: Ctx, table_id: str, app_token: str, kind: str, day: str | None = None
) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO table_places(table_id, app_token, kind, day, created_at) VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(table_id) DO UPDATE SET app_token = excluded.app_token,"
            " kind = excluded.kind, day = excluded.day",
            (table_id, app_token, kind, day, now_iso()),
        )
    table_registry.register(table_id, app_token)


def unregister(ctx: Ctx, table_id: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM table_places WHERE table_id = ?", (table_id,))
    table_registry.forget(table_id)


def _wiki_url(ctx: Ctx, node_token: str) -> str:
    return feishu_table._wiki_url((feishu_table.base(ctx) or {}).get("url"), node_token)


def _space(ctx: Ctx) -> str:
    space = feishu_table.wiki_space_id(ctx)
    if not space:
        raise StageError("每周选款表要配 table.wiki_space_id（周表格放在知识库根目录）")
    return space


def _tables(api: feishu.Client, app_token: str) -> dict[str, str]:
    """这本表格里的数据表：{表名: table_id}。"""
    out: dict[str, str] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if token:
            params["page_token"] = token
        data = api.call("GET", f"/bitable/v1/apps/{app_token}/tables", params=params)
        for item in data.get("items") or []:
            out[str(item.get("name") or "")] = item["table_id"]
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return out


def _folder(
    api: feishu.Client, app_token: str, name: str, parent_id: str | None = None
) -> str:
    body: dict[str, Any] = {"type": "folder", "name": name}
    if parent_id:
        body["parent_id"] = parent_id
    return str(api.call("POST", f"/base/v3/bases/{app_token}/blocks", json=body)["id"])


def _copy_table(
    api: feishu.Client, app_token: str, table_id: str, name: str
) -> tuple[str, str]:
    """复制一张表（只复制结构）：返回 (新表 id, 表名)；同名就加上时间再复制一次。"""
    path = f"/base/v3/bases/{app_token}/tables/{table_id}/copy"
    try:
        data = api.call("POST", path, json={"name": name, "range": "schema"})
    except feishu.FeishuError as exc:
        if exc.code != 800010102:
            raise
        name = f"{name} {utcnow():%m%d%H%M}"
        data = api.call("POST", path, json={"name": name, "range": "schema"})
    table = data.get("table") or {}
    if data.get("state") != "success" or not table.get("id"):
        raise KeyError(f"复制没有马上完成：{dumps(data)[:200]}")
    return str(table["id"]), str(table.get("name") or name)


def _views_by_name(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    names: dict[str, tuple[str, str]],
) -> dict[str, str]:
    """按视图名和类型找视图 id：names = {键: (视图名, 类型)}；键 gallery（「选中的款」的看图）只按类型认第一个画册。"""
    found: dict[str, str] = {}
    for view in views.list_views(api, info, table_id):
        for key, (name, kind) in names.items():
            if view.get("view_type") != kind:
                continue
            if key == "gallery":
                found.setdefault(key, view["view_id"])
            elif view.get("view_name") == name:
                found[key] = view["view_id"]
    return found


_SELECTED_VIEWS = {
    "pending": (views.TO_LIST_VIEW, "grid"),
    "in_progress": (views.IN_PROGRESS_VIEW, "grid"),
    "listed": (views.LISTED_VIEW, "grid"),
    "all": (views.ALL_VIEW, "grid"),
    "gallery": (views.GALLERY_VIEW, "gallery"),
}
# 候选表视图版本 6：表格「全部」+ 画册「全部画册」「已选中」「待选」（三个画册按名字分）
_CANDIDATE_VIEWS = {
    "all": (views.ALL_VIEW, "grid"),
    "all_gallery": (views.ALL_GALLERY_VIEW, "gallery"),
    "picked": (views.PICKED_VIEW, "gallery"),
    "pending": (views.PENDING_VIEW, "gallery"),
}


# ---------------------------------------------------------------- 模板表格


def template(ctx: Ctx) -> dict[str, Any] | None:
    return loads(ctx.db.get_meta(TEMPLATE_META_KEY), None) or None


def _save_template(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, TEMPLATE_META_KEY, dumps(meta))


def _make_selected(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any]
) -> dict[str, Any]:
    """模板里的「选中的款模板」：列和视图照「选中的款」建（上架状态、下载确认开着时带上那几列）。"""
    from . import downloads  # 下载确认依赖表格模块，放到用时再导入

    listing_on = feishu_table.listing_enabled(ctx)
    extra = (feishu_table.LISTING_FIELDS if listing_on else []) + (
        feishu_table.DOWNLOAD_FIELDS if downloads.enabled(ctx) else []
    )
    table_id = api.call(
        "POST",
        f"/bitable/v1/apps/{info['app_token']}/tables",
        json={
            "table": {
                "name": SELECTED_TEMPLATE,
                "default_view_name": views.ALL_VIEW,
                "fields": feishu_table.FIELDS + extra,
            }
        },
    )["table_id"]
    order = feishu_table.DOWNLOAD_ORDER_FIELD if downloads.enabled(ctx) else None
    ids: dict[str, str] | None = None
    if views.enabled(ctx) and listing_on:
        ids = feishu_table.build_selected_views(
            ctx,
            api,
            {**info, "table_id": table_id},
            layout=feishu_table.selected_layout(False),
            order_field=order,
        )
    else:
        api.call(
            "POST",
            f"/bitable/v1/apps/{info['app_token']}/tables/{table_id}/views",
            json={"view_name": views.GALLERY_VIEW, "view_type": "gallery"},
        )
    return {
        "table_id": table_id,
        "views": ids or {},
        "extra_fields": sorted(
            f["field_name"] for f in feishu_table.SCENE_FIELDS + extra
        ),
        "views_layout": feishu_table.LAYOUT_VERSION if ids else 0,
        "views_sorted": bool(ids and order),
        "views_temu": False,
        "views_version": feishu_table.SELECTED_VIEWS_VERSION if ids else 0,
    }


def _store_columns(ctx: Ctx) -> list[str]:
    from . import candidate_stores  # 店铺列依赖表格模块，放到用时再导入

    return [s["field_name"] for s in candidate_stores.specs(ctx)]


def _make_candidate(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any]
) -> dict[str, Any]:
    """模板里的「候选表模板」：照新建候选表的样子建（列、店铺列、默认视图：表格「全部」、画册「全部画册」「已选中」「待选」）。"""
    from . import candidates  # 候选表依赖本模块，放到用时再导入

    table_id, _ = candidates.create_fresh(ctx, api, info, CANDIDATE_TEMPLATE)
    view_set = candidates.prepare_new(ctx, api, info, table_id)
    return {
        "table_id": table_id,
        "views": view_set or {},
        "fields_version": candidates.FIELDS_VERSION,
        "package": packages.enabled(ctx),
        "stores": _store_columns(ctx),
        "views_version": candidates.VIEWS_VERSION if view_set else 0,
    }


def _make_listed(ctx: Ctx, api: feishu.Client, info: dict[str, Any]) -> dict[str, Any]:
    """模板里的「已上架的款模板」：列照「已上架的款」，视图「全部」+ 画册「看图」（封面主图）；每家店的视图到周表格里再补。"""
    from . import listed_table  # 已上架的款依赖表格模块，放到用时再导入

    specs = listed_table.field_specs(ctx)
    table_id = api.call(
        "POST",
        f"/bitable/v1/apps/{info['app_token']}/tables",
        json={
            "table": {
                "name": LISTED_TEMPLATE,
                "default_view_name": views.ALL_VIEW,
                "fields": specs,
            }
        },
    )["table_id"]
    gallery = views._create(api, info, table_id, views.GALLERY_VIEW, "gallery")
    return _adopt_listed(ctx, api, info, table_id, gallery)


def _adopt_listed(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    gallery: str | None = None,
) -> dict[str, Any]:
    """「已上架的款模板」的视图：第一个表格视图「全部」、画册「看图」（没有就建），封面设成主图。"""
    from . import listed_table  # 已上架的款依赖表格模块，放到用时再导入

    existing = views.list_views(api, info, table_id)
    first = next(v["view_id"] for v in existing if v.get("view_type") == "grid")
    gallery = (
        gallery
        or next((v["view_id"] for v in existing if v.get("view_type") == "gallery"), None)
        or views._create(api, info, table_id, views.GALLERY_VIEW, "gallery")
    )
    cover = next(
        (
            f["field_id"]
            for f in views.list_fields(api, info, table_id)
            if f.get("field_name") == listed_table.F_THUMB
        ),
        None,
    )
    if cover:
        # 刚建的视图 v3 接口过一两秒才查得到
        _settle(lambda: views.set_cover(api, info, table_id, gallery, cover))
    return {
        "table_id": table_id,
        "views": {"all": first, "stores": {}, "gallery": gallery},
    }


NOT_FOUND = 800030501
_SETTLE_SECONDS = (1, 2, 4, 8)
# 整本复制（知识库节点复制）是异步的：复制完之前这本表格的接口报 1254036「Bitable is copying」（2026-10-29 线上实测）
COPYING = 1254036
_COPY_WAIT_SECONDS = (2, 3, 5, 8, 10, 15, 20, 30)
_sleep = time.sleep


def _settle(fn: Any) -> Any:
    """刚建出来的视图、表，v3 接口过一会儿才查得到（800030501 not_found）：等一等再试。"""
    for delay in (*_SETTLE_SECONDS, None):
        try:
            return fn()
        except feishu.FeishuError as exc:
            if exc.code != NOT_FOUND or delay is None:
                raise
            _sleep(delay)
    return None


def _adopt(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], kind: str, table_id: str
) -> dict[str, Any]:
    """上一次建到一半、表已经在模板表格里了：按表名认回来，补上视图 id（不再建一张）。"""
    from . import candidates, downloads  # 都依赖本模块之外的表格模块，放到用时再导入

    if kind == "listed":
        return _adopt_listed(ctx, api, info, table_id)
    if kind == "candidate":
        return {
            "table_id": table_id,
            "views": _views_by_name(api, info, table_id, _CANDIDATE_VIEWS),
            "fields_version": candidates.FIELDS_VERSION,
            "package": packages.enabled(ctx),
            "stores": _store_columns(ctx),
            "views_version": candidates.VIEWS_VERSION,
        }
    listing_on = feishu_table.listing_enabled(ctx)
    extra = (feishu_table.LISTING_FIELDS if listing_on else []) + (
        feishu_table.DOWNLOAD_FIELDS if downloads.enabled(ctx) else []
    )
    ids = _views_by_name(api, info, table_id, _SELECTED_VIEWS)
    # 五个都在才算建完（只认到一部分的，之后由 upgrade_selected_templates 接着建）
    complete = len(ids) == len(_SELECTED_VIEWS)
    return {
        "table_id": table_id,
        "views": ids,
        "extra_fields": sorted(
            f["field_name"] for f in feishu_table.SCENE_FIELDS + extra
        ),
        "views_layout": feishu_table.LAYOUT_VERSION if complete else 0,
        "views_sorted": complete and downloads.enabled(ctx),
        "views_temu": False,
        "views_version": feishu_table.SELECTED_VIEWS_VERSION if complete else 0,
    }


def ensure_template(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    """模板表格：还没有就在知识库根目录建一本（三张模板表），返回 meta。做到一半失败的，下一次接着做（已经建出来的表按
    表名认回来）。建好后人要在三个「看图」里设封面效果，再 lock_template。"""
    meta = template(ctx)
    if template_ready(meta) and meta.get("cleaned"):
        # 上一次没建成时记的原因不再算数
        stale = [k for k in ("error", "failed_at") if k in meta]
        for key in stale:
            meta.pop(key)
        if stale:
            _save_template(ctx, meta)
        return meta
    if not meta or not meta.get("app_token"):
        space = _space(ctx)
        node = (
            api.call(
                "POST",
                f"/wiki/v2/spaces/{space}/nodes",
                json={
                    "obj_type": "bitable",
                    "node_type": "origin",
                    "title": TEMPLATE_TITLE,
                },
            ).get("node")
            or {}
        )
        meta = {
            "app_token": str(node["obj_token"]),
            "node_token": node["node_token"],
            "url": _wiki_url(ctx, str(node["node_token"])),
            "created_at": now_iso(),
            "tables": {},
        }
        # 先记下来：中途失败了下一次接着建，不会再建一本
        _save_template(ctx, meta)
    app_token = meta["app_token"]
    info = {"app_token": app_token, "url": meta["url"]}
    names = _tables(api, app_token)
    makers = {
        "selected": _make_selected,
        "candidate": _make_candidate,
        "listed": _make_listed,
    }
    for kind, maker in makers.items():
        if (meta["tables"] or {}).get(kind):
            continue
        name = TEMPLATE_NAMES[kind]
        meta["tables"][kind] = (
            _adopt(ctx, api, info, kind, names[name])
            if name in names
            else maker(ctx, api, info)
        )
        _save_template(ctx, meta)
    if not meta.get("cleaned"):
        # 新建的多维表格自带一张空表：删掉（只在刚建好时做一次，之后人加的表不动）
        for name, table_id in _tables(api, app_token).items():
            if name not in TEMPLATE_NAMES.values():
                try:
                    api.call(
                        "DELETE", f"/bitable/v1/apps/{app_token}/tables/{table_id}"
                    )
                except feishu.FeishuError:
                    pass
        meta["cleaned"] = True
        meta.pop("error", None)
        meta.pop("failed_at", None)
        _save_template(ctx, meta)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "table",
                "book_template_created",
                {"app_token": app_token, "url": meta["url"]},
            )
    return meta


def template_ready(meta: dict[str, Any] | None) -> bool:
    return bool(
        meta and all((meta.get("tables") or {}).get(k) for k in TEMPLATE_NAMES)
    )


# 只读的表权限：perm 不是 no_perm 时视图、记录、字段三条规则都要给全，少一条接口报 code 1（2026-10-29 线上实测）
READ_ONLY_RULE: dict[str, Any] = {
    "perm": "read_only",
    "view_rule": {"allow_edit": False, "visibility": {"all_visible": True}},
    "record_rule": {"record_operations": [], "other_record_all_read": True},
    "field_rule": {"field_perm_mode": "all_read", "allow_edit_and_modify_option_fields": []},
}


def _v3(data: Any) -> Any:
    """Base v3 角色接口的返回：data 是一段 JSON 字符串，列表里每一项也是 JSON 字符串（2026-10-29 线上实测）。"""
    if isinstance(data, dict) and isinstance(data.get("data"), str):
        data = loads(data["data"], {})
    if isinstance(data, str):
        data = loads(data, {})
    return data or {}


def _roles(api: feishu.Client, app: str) -> list[dict[str, Any]]:
    data = _v3(api.call("GET", f"/base/v3/bases/{app}/roles"))
    items = data.get("base_roles") or data.get("items") or []
    return [_v3(r) for r in items]


def _editor(api: feishu.Client, app: str) -> dict[str, Any]:
    """「Editor」角色（编辑者）的全部规则。"""
    found = next((r for r in _roles(api, app) if str(r.get("role_type")) == "editor"), None)
    if not found or not found.get("role_id"):
        raise KeyError("开了高级权限也没找到编辑者（Editor）角色")
    return _v3(api.call("GET", f"/base/v3/bases/{app}/roles/{found['role_id']}"))


def lock_tables(api: feishu.Client, app: str, tables: Iterable[str]) -> None:
    """开高级权限，「编辑者」角色对这几张表（连同角色里已经有规则的表）只读，改完读回来核对；没锁上报 KeyError。"""
    api.call("PUT", f"/base/v3/bases/{app}/advperm/enable", params={"enable": "true"})
    role = _editor(api, app)
    names = {*(role.get("table_rule_map") or {}), *tables}
    # 增量合并：role_name、role_type、base_rule_map 必带（照原样），只改各表的权限
    body = {
        "role_name": role.get("role_name"),
        "role_type": role.get("role_type") or "editor",
        "base_rule_map": role.get("base_rule_map") or {"copy": False, "download": False},
        "table_rule_map": {name: dict(READ_ONLY_RULE) for name in sorted(names)},
    }
    api.call("PUT", f"/base/v3/bases/{app}/roles/{role['role_id']}", json=body)
    rules = _editor(api, app).get("table_rule_map") or {}
    loose = sorted(n for n in names if (rules.get(n) or {}).get("perm") != "read_only")
    if loose:
        raise KeyError(f"编辑者对这几张表还不是只读：{'、'.join(loose)}")


def lock_template(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    """锁模板：开高级权限，「编辑者」角色所有表只读（可管理的人照样能改），改完读回来核对。
    要 base:app:update、base:role:read / update。"""
    meta = template(ctx)
    if not template_ready(meta):
        raise StageError("模板表格还没建好：先 podctl table sync")
    app = meta["app_token"]
    try:
        lock_tables(api, app, TEMPLATE_NAMES.values())
    except _FAILURES as exc:
        meta["lock_error"] = f"{type(exc).__name__}: {exc}"[:300]
        meta["lock_failed_at"] = now_iso()
        _save_template(ctx, meta)
        return {"locked": False, "error": meta["lock_error"]}
    meta["locked_at"] = now_iso()
    meta.pop("lock_error", None)
    meta.pop("lock_failed_at", None)
    _save_template(ctx, meta)
    with ctx.db.tx() as conn:
        log_event(conn, "table", "book_template_locked", {"app_token": app})
    return {"locked": True}


# ---------------------------------------------------------------- 周表格


def book(ctx: Ctx, bid: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT info_json FROM table_books WHERE book_id = ?", (bid,))
    return (loads(row["info_json"], None) or None) if row else None


def books(ctx: Ctx) -> list[dict[str, Any]]:
    return [
        loads(r["info_json"], {}) or {}
        for r in ctx.db.q("SELECT info_json FROM table_books ORDER BY book_id")
    ]


def save_book(ctx: Ctx, info: dict[str, Any]) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO table_books(book_id, app_token, week_start, info_json, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(book_id) DO UPDATE SET"
            " app_token = excluded.app_token, info_json = excluded.info_json,"
            " updated_at = excluded.updated_at",
            (
                info["book_id"],
                info["app_token"],
                info["week_start"],
                dumps(info),
                info.get("created_at") or now,
                now,
            ),
        )


def _advanced(api: feishu.Client, app_token: str) -> bool:
    data = api.call("GET", f"/bitable/v1/apps/{app_token}")
    return bool((data.get("app") or {}).get("is_advanced"))


def _copied(api: feishu.Client, app_token: str) -> bool:
    """等整本复制完（最多一分半钟），返回复制件开没开高级权限；还没复制完报 FeishuError（下一次同步接着做）。"""
    for delay in (*_COPY_WAIT_SECONDS, None):
        try:
            return _advanced(api, app_token)
        except feishu.FeishuError as exc:
            if exc.code != COPYING or delay is None:
                raise
            _sleep(delay)
    return False


def ensure_book(ctx: Ctx, api: feishu.Client, day: str | dt.date) -> dict[str, Any]:
    """这一天所在那一周的周表格：还没有就从模板表格复制一本到知识库根目录（关掉带过来的高级权限，三张模板表挪进
    「模板」文件夹，订阅记录变更事件）。返回周表格信息。"""
    bid = book_id(day)
    found = book(ctx, bid)
    if found and found.get("templates"):
        return found
    tpl = ensure_template(ctx, api)
    if not template_ready(tpl):
        raise StageError("模板表格还没建好")
    if not found:
        space = _space(ctx)
        node = (
            api.call(
                "POST",
                f"/wiki/v2/spaces/{space}/nodes/{tpl['node_token']}/copy",
                json={"target_space_id": space, "title": book_title(day)},
            ).get("node")
            or {}
        )
        found = {
            "book_id": bid,
            "app_token": str(node["obj_token"]),
            "node_token": node["node_token"],
            "url": _wiki_url(ctx, str(node["node_token"])),
            "title": book_title(day),
            "week_start": week_start(day).isoformat(),
            "created_at": now_iso(),
        }
        # 先记下来：后面哪一步失败了，下一次接着做，不会再复制一本
        save_book(ctx, found)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "table",
                "book_created",
                {"book_id": bid, "app_token": found["app_token"], "url": found["url"]},
            )
    app = found["app_token"]
    info = {"app_token": app, "url": found["url"]}
    if _copied(api, app):
        # 模板锁着（高级权限），复制件也带着：周表格要人能改，关掉
        api.call("PUT", f"/bitable/v1/apps/{app}", json={"is_advanced": False})
        found["advanced_off_at"] = now_iso()
    names = _tables(api, app)
    templates: dict[str, Any] = {}
    for kind, name in TEMPLATE_NAMES.items():
        if name not in names:
            raise KeyError(f"周表格里没有「{name}」")
        templates[kind] = {**tpl["tables"][kind], "table_id": names[name]}
    templates["selected"]["views"] = _views_by_name(
        api, info, names[SELECTED_TEMPLATE], _SELECTED_VIEWS
    )
    templates["candidate"]["views"] = _views_by_name(
        api, info, names[CANDIDATE_TEMPLATE], _CANDIDATE_VIEWS
    )
    folder = found.get("template_folder_id") or _folder(api, app, TEMPLATE_FOLDER)
    found["template_folder_id"] = folder
    save_book(ctx, found)
    for kind in TEMPLATE_NAMES:
        feishu_table.move_table(api, info, templates[kind]["table_id"], folder)
    found["templates"] = templates
    save_book(ctx, found)
    subscribe(ctx, api, found)
    return found


def subscribe(ctx: Ctx, api: feishu.Client, info: dict[str, Any]) -> dict[str, Any]:
    """订阅这本周表格的记录变更事件（勾选几秒内就处理）；失败只记原因，一小时后再试。"""
    try:
        api.call(
            "POST",
            f"/drive/v1/files/{info['app_token']}/subscribe",
            params={"file_type": "bitable"},
        )
    except _FAILURES as exc:
        info["events"] = {
            "error": feishu_table._events_failure_text(exc),
            "error_at": now_iso(),
        }
        save_book(ctx, info)
        return {"subscribed": False, "error": info["events"]["error"]}
    info["events"] = {"subscribed_at": now_iso()}
    save_book(ctx, info)
    return {"subscribed": True}


def events_pending(ctx: Ctx) -> list[dict[str, Any]]:
    """还没订阅上事件（上次失败已过冷却）的周表格。"""
    if not feishu_table.events_wanted(ctx):
        return []
    out = []
    for info in books(ctx):
        events = info.get("events") or {}
        if events.get("subscribed_at") or not info.get("templates"):
            continue
        failed = parse_iso(events.get("error_at"))
        if failed and utcnow() - failed < dt.timedelta(minutes=RETRY_MINUTES):
            continue
        out.append(info)
    return out


def book_of(ctx: Ctx, app_token: str | None) -> dict[str, Any] | None:
    """按 app_token 找周表格（记录变更事件里的 file_token）。"""
    if not app_token:
        return None
    return next((b for b in books(ctx) if b.get("app_token") == app_token), None)


# ---------------------------------------------------------------- 天


def day_info(ctx: Ctx, day: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT info_json FROM table_days WHERE day = ?", (day,))
    return (loads(row["info_json"], None) or None) if row else None


def days(ctx: Ctx) -> list[dict[str, Any]]:
    return [
        loads(r["info_json"], {}) or {}
        for r in ctx.db.q("SELECT info_json FROM table_days ORDER BY day")
    ]


def save_day(ctx: Ctx, info: dict[str, Any]) -> None:
    """存一天的表格信息（feishu_table._save_base 遇到带 day 的 info 时转到这里）。"""
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO table_days(day, book_id, info_json, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET"
            " book_id = excluded.book_id, info_json = excluded.info_json,"
            " updated_at = excluded.updated_at",
            (info["day"], info["book_id"], dumps(info), info.get("created_at") or now, now),
        )


def ensure_day(ctx: Ctx, api: feishu.Client, day: str) -> dict[str, Any]:
    """这一天的文件夹：周表格里建「10-26 周一」文件夹、里面的「10-26 已归档」子文件夹，复制「10-26 选中的款」挪进去。
    返回那天「选中的款」的表格信息（口径同 feishu_table.base()）。做到一半失败的，下一次接着做。"""
    found = day_info(ctx, day)
    if found and found.get("table_id"):
        return found
    if not found:
        # 先记一行：建周表格、文件夹哪一步失败了，失败原因记在这一天上
        found = {"day": day, "book_id": book_id(day), "created_at": now_iso()}
        save_day(ctx, found)
    info_book = ensure_book(ctx, api, day)
    app = info_book["app_token"]
    found.update(app_token=app, url=info_book["url"], book_id=info_book["book_id"])
    if not found.get("folder_id"):
        found["folder_id"] = _folder(api, app, folder_name(day))
        save_day(ctx, found)
    if not found.get("archive_folder_id"):
        found["archive_folder_id"] = _folder(
            api, app, archive_name(day), parent_id=found["folder_id"]
        )
        save_day(ctx, found)
    tpl = info_book["templates"]["selected"]
    if not found.get("copied_table_id"):
        table_id, name = _copy_table(api, app, tpl["table_id"], selected_name(day))
        register(ctx, table_id, app, "selected", day)
        # 先记下来：挪文件夹失败了下一次接着挪，不会再复制一张
        found.update(copied_table_id=table_id, table_name=name)
        save_day(ctx, found)
    table_id = found["copied_table_id"]
    feishu_table.move_table(
        api, {"app_token": app}, table_id, found["folder_id"]
    )
    found.update(
        table_id=table_id,
        table_name=found.get("table_name") or selected_name(day),
        # 复制件的视图 id 和模板里的一样
        views=tpl.get("views") or None,
        extra_fields=tpl.get("extra_fields") or [],
        views_layout=tpl.get("views_layout") or 0,
        views_sorted=bool(tpl.get("views_sorted")),
        views_temu=bool(tpl.get("views_temu")),
        views_version=tpl.get("views_version") or 0,
        listing_backfilled=True,
        seed_files={},
        shared_with=[],
    )
    # 上一次没建成时记的原因不再算数
    found.pop("error", None)
    found.pop("failed_at", None)
    save_day(ctx, found)
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "table",
            "book_day_created",
            {"day": day, "book_id": info_book["book_id"], "table_id": table_id},
        )
    return found


def ready_day(ctx: Ctx, api: feishu.Client, day: str) -> dict[str, Any]:
    """候选表要进这一天：那天还没建好就建（ensure_day）。失败记在那一天上，RETRY_MINUTES 内不再试；这时报 KeyError，
    调用方照别的写表失败处理（这一行先跳过，下一次同步再来）。"""
    found = day_info(ctx, day) or {}
    if found.get("table_id"):
        return found
    if failed_recently(found):
        raise KeyError(f"{day} 那天的文件夹上次没建成（{found.get('error')}），过一会儿再试")
    try:
        return ensure_day(ctx, api, day)
    except (*_FAILURES, StageError) as exc:
        failed = {
            **(day_info(ctx, day) or {"day": day, "book_id": book_id(day)}),
            "error": _reason(exc),
            "failed_at": now_iso(),
        }
        save_day(ctx, failed)
        raise KeyError(f"{day} 那天的文件夹没建成：{failed['error']}") from exc


def candidate_template(ctx: Ctx, where: dict[str, Any]) -> dict[str, Any]:
    """那一天所在周表格里的「候选表模板」：table_id 是周表格里的那一张，列 / 视图的版本记的是模板表格建它时的。"""
    found = book(ctx, str(where.get("book_id"))) or {}
    tpl = (found.get("templates") or {}).get("candidate") or {}
    if not tpl.get("table_id"):
        raise KeyError(f"周表格 {where.get('book_id')} 里还没认到「{CANDIDATE_TEMPLATE}」")
    return tpl


def _candidate_stores_behind(ctx: Ctx, meta: dict[str, Any]) -> bool:
    """这张「候选表模板」还没有店铺配置里后加的店铺列（或「货号」）。"""
    return not set(_store_columns(ctx)) <= set(meta.get("stores") or [])


def _candidate_behind(ctx: Ctx, meta: dict[str, Any] | None) -> bool:
    """这张「候选表模板」（模板表格的，或某本周表格里的）默认视图没跟上 candidates.VIEWS_VERSION，或缺店铺配置里后加的
    店铺列（失败满上限的不再试）。"""
    from . import candidates  # 候选表依赖本模块，放到用时再导入

    if not (meta and meta.get("table_id")):
        return False
    if int(meta.get("views_attempts") or 0) >= feishu_table.MAX_ATTEMPTS:
        return False
    stale_views = views.enabled(ctx) and int(meta.get("views_version") or 0) < candidates.VIEWS_VERSION
    return stale_views or _candidate_stores_behind(ctx, meta)


def _candidate_templates(ctx: Ctx) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """默认视图或店铺列没跟上的「候选表模板」：[(模板表格 template / 周表格编号, 那本表格的 info, 那本表格的 meta)]。"""
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    tpl = template(ctx) or {}
    if tpl.get("app_token") and _candidate_behind(
        ctx, (tpl.get("tables") or {}).get("candidate")
    ):
        out.append(("template", {"app_token": tpl["app_token"], "url": tpl.get("url")}, tpl))
    for found in books(ctx):
        if _candidate_behind(ctx, (found.get("templates") or {}).get("candidate")):
            out.append(
                (found["book_id"], {"app_token": found["app_token"], "url": found.get("url")}, found)
            )
    return out


def upgrade_candidate_templates(
    ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]
) -> list[str]:
    """模板表格和各本周表格里的「候选表模板」，默认视图跟上 candidates.VIEWS_VERSION（就地改，见
    views.ensure_candidate_views：已有画册依次改名挪位置，人设过的封面效果跟着视图走，最后新建的「待选」要人在飞书里设一次）；
    店铺配置多了店时先补上店铺列和「货号」，再按新的店铺列重摆默认视图（2026-11-02，1店 / 2店）。之后从它复制的候选表
    就是这一版。失败记次数（views_attempts），满上限不再试。返回改了哪几张（template / 周表格编号）。"""
    from . import candidate_stores, candidates, table_template  # 都依赖本模块，放到用时再导入

    done: list[str] = []
    for label, info, holder in _candidate_templates(ctx):
        meta = holder["tables" if label == "template" else "templates"]["candidate"]
        try:
            if _candidate_stores_behind(ctx, meta):
                meta["stores"] = table_template._add_store_columns(ctx, api, info, meta["table_id"])
            # 默认视图按模板里的店铺列摆（周表格里整本带过来的模板表没登记过店铺列）
            if meta.get("stores"):
                candidate_stores.mark_done(ctx, meta["table_id"], list(meta["stores"]))
            if views.enabled(ctx):
                meta["views"] = candidates.ensure_views(
                    api,
                    info,
                    meta["table_id"],
                    candidates._layout_for(ctx, meta["table_id"]),
                    resort=False,
                )
                meta["views_version"] = candidates.VIEWS_VERSION
            meta.pop("views_attempts", None)
            meta.pop("views_error", None)
            done.append(label)
        except _FAILURES as exc:
            meta["views_attempts"] = int(meta.get("views_attempts") or 0) + 1
            meta["views_error"] = _reason(exc)
            errors.append({"table": f"{label}·{CANDIDATE_TEMPLATE}", "error": meta["views_error"]})
        if label == "template":
            _save_template(ctx, holder)
        else:
            save_book(ctx, holder)
    if done:
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "table",
                "candidate_templates_upgraded",
                {"tables": done, "views_version": candidates.VIEWS_VERSION, "stores": _store_columns(ctx)},
            )
    return done


def _selected_views_behind(ctx: Ctx, meta: dict[str, Any] | None) -> bool:
    """这张「选中的款模板」（模板表格的，或某本周表格里的）默认视图没跟上 feishu_table.SELECTED_VIEWS_VERSION（要开着默认
    视图、模板里有上架状态那一列；失败满上限的不再试）。"""
    return bool(
        meta
        and meta.get("table_id")
        and views.enabled(ctx)
        and feishu_table.listing_enabled(ctx)
        and feishu_table.listing_ready(meta)
        and int(meta.get("views_version") or 0) < feishu_table.SELECTED_VIEWS_VERSION
        and int(meta.get("views_attempts") or 0) < feishu_table.MAX_ATTEMPTS
    )


def _selected_templates(ctx: Ctx) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """默认视图没跟上的「选中的款模板」：[(模板表格 template / 周表格编号, 那本表格的 info, 那本表格的 meta)]。"""
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    tpl = template(ctx) or {}
    if tpl.get("app_token") and _selected_views_behind(
        ctx, (tpl.get("tables") or {}).get("selected")
    ):
        out.append(
            ("template", {"app_token": tpl["app_token"], "url": tpl.get("url")}, tpl)
        )
    for found in books(ctx):
        if _selected_views_behind(ctx, (found.get("templates") or {}).get("selected")):
            out.append(
                (
                    found["book_id"],
                    {"app_token": found["app_token"], "url": found.get("url")},
                    found,
                )
            )
    return out


def upgrade_selected_templates(
    ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]
) -> list[str]:
    """模板表格和各本周表格里的「选中的款模板」，默认视图跟上 feishu_table.SELECTED_VIEWS_VERSION（就地改，见
    views.ensure_selected_views：画册「看图」留着、人设的封面效果在；先给上架状态补齐五个选项）；之后从它复制的「选中的款」
    就是这一版。失败记次数（views_attempts），满上限不再试。返回改了哪几张（template / 周表格编号）。"""
    from . import downloads  # 下载确认依赖表格模块，放到用时再导入

    done: list[str] = []
    for label, info, holder in _selected_templates(ctx):
        meta = holder["tables" if label == "template" else "templates"]["selected"]
        has_order = feishu_table.DOWNLOAD_ORDER_FIELD in (
            meta.get("extra_fields") or []
        )
        order = (
            feishu_table.DOWNLOAD_ORDER_FIELD
            if downloads.enabled(ctx) and has_order
            else None
        )
        try:
            meta["views"] = feishu_table.build_selected_views(
                ctx,
                api,
                {**info, "table_id": meta["table_id"]},
                layout=feishu_table.selected_layout(False),
                order_field=order,
                resort=not meta.get("views_sorted"),
            )
            meta.update(
                views_version=feishu_table.SELECTED_VIEWS_VERSION,
                views_layout=feishu_table.LAYOUT_VERSION,
                views_sorted=bool(order),
            )
            meta.pop("views_attempts", None)
            meta.pop("views_error", None)
            done.append(label)
        except _FAILURES as exc:
            meta["views_attempts"] = int(meta.get("views_attempts") or 0) + 1
            meta["views_error"] = _reason(exc)
            errors.append(
                {"table": f"{label}·{SELECTED_TEMPLATE}", "error": meta["views_error"]}
            )
        if label == "template":
            _save_template(ctx, holder)
        else:
            save_book(ctx, holder)
    if done:
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "table",
                "selected_templates_upgraded",
                {"tables": done, "views_version": feishu_table.SELECTED_VIEWS_VERSION},
            )
    return done


def copy_candidate(
    ctx: Ctx,
    api: feishu.Client,
    where: dict[str, Any],
    tpl: dict[str, Any],
    name: str,
) -> tuple[str, str]:
    """从周表格的「候选表模板」复制一张候选表（复制件落在「模板」文件夹里）、登记，挪进那一天的文件夹；挪不成的记在
    那一天的 unmoved 上，下一次同步再挪（表已经能用）。返回 (table_id, 表名)。"""
    app = where["app_token"]
    table_id, name = _copy_table(api, app, tpl["table_id"], name)
    register(ctx, table_id, app, "candidate", where["day"])
    try:
        feishu_table.move_table(api, where, table_id, where["folder_id"])
    except _FAILURES:
        found = day_info(ctx, where["day"]) or dict(where)
        found["unmoved"] = sorted({*(found.get("unmoved") or []), table_id})
        save_day(ctx, found)
    return table_id, name


def _move_unmoved(
    ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]
) -> list[str]:
    """把上次没挪进当天文件夹的候选表挪进去。"""
    moved = []
    for found in days(ctx):
        left = list(found.get("unmoved") or [])
        if not left:
            continue
        for table_id in list(left):
            try:
                feishu_table.move_table(api, found, table_id, found["folder_id"])
            except _FAILURES as exc:
                errors.append({"day": found["day"], "error": _reason(exc)})
                continue
            left.remove(table_id)
            moved.append(table_id)
        found["unmoved"] = left
        if not left:
            found.pop("unmoved")
        save_day(ctx, found)
    return moved


def table_day(ctx: Ctx, table_id: str | None) -> dict[str, Any] | None:
    """每周表格里的这张表归哪一天（那天的信息）；旧表格里的表为 None。"""
    if not table_id:
        return None
    row = ctx.db.q1("SELECT day FROM table_places WHERE table_id = ?", (table_id,))
    return day_info(ctx, row["day"]) if row and row["day"] else None


def scopes(ctx: Ctx) -> list[dict[str, Any]]:
    """「选中的款」的每一份：旧表格（建好了的话）+ 每天建好了的。"""
    out = []
    legacy = feishu_table.base(ctx)
    if legacy:
        out.append(legacy)
    out += [d for d in days(ctx) if d.get("table_id")]
    return out


def scope_of_table(ctx: Ctx, table_id: str | None) -> dict[str, Any] | None:
    """这张表是哪一份「选中的款」（旧表格的或某一天的）；不是「选中的款」返回 None。"""
    if not table_id:
        return None
    return next((s for s in scopes(ctx) if s.get("table_id") == table_id), None)


def scope_of_asset(ctx: Ctx, asset_id: str) -> dict[str, Any] | None:
    """这一款的「选中的款」在哪一份（旧表格或它那一天的）；那天还没建时返回 None。"""
    day = asset_day(ctx, asset_id)
    return day_info(ctx, day) if day else feishu_table.base(ctx)


def needed_days(ctx: Ctx) -> list[str]:
    """有款要进「选中的款」、归到的那一天还没建的。"""
    if not enabled(ctx):
        return []
    rows = ctx.db.q(
        "SELECT a.asset_id FROM assets a LEFT JOIN table_rows t ON t.asset_id = a.asset_id"
        " WHERE a.human_decision = 'approved' AND t.record_id IS NULL"
    )
    seeds = asset_seeds(ctx, [r["asset_id"] for r in rows])
    out = set()
    for seed in set(seeds.values()):
        day = place(ctx, seed)
        if day and not (day_info(ctx, day) or {}).get("table_id"):
            out.add(day)
    return sorted(out)


def failed_recently(info: dict[str, Any], key: str = "failed_at") -> bool:
    failed = parse_iso(info.get(key))
    return bool(failed) and utcnow() - failed < dt.timedelta(minutes=RETRY_MINUTES)


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def prepare(
    ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]
) -> dict[str, Any]:
    """表格同步开头调（持有选款表的同步租约）：开着每周选款表时，备好模板表格（并锁上），有款要进的那一天还没建就建
    （周表格、文件夹、当天的「选中的款」），补订阅没订上的周表格。出错只记下来、这一天的款这一轮先不写，一小时后再试。
    返回这次做了什么，没事做为 {}。"""
    if not enabled(ctx):
        return {}
    out: dict[str, Any] = {}
    tpl = template(ctx) or {}
    if not (template_ready(tpl) and tpl.get("cleaned")):
        if failed_recently(tpl):
            return {}
        try:
            tpl = ensure_template(ctx, api)
            out["template"] = tpl.get("url")
        except _FAILURES as exc:
            tpl = {**(template(ctx) or {}), "error": _reason(exc), "failed_at": now_iso()}
            _save_template(ctx, tpl)
            errors.append({"table": TEMPLATE_TITLE, "error": tpl["error"]})
            return {"template_error": tpl["error"]}
    if not tpl.get("locked_at") and not failed_recently(tpl, "lock_failed_at"):
        locked = lock_template(ctx, api)
        out["template_lock"] = locked
    # 新的一天 / 新的一周从模板复制之前，先让「候选表模板」的默认视图和店铺列跟上这一版
    upgraded = upgrade_candidate_templates(ctx, api, errors)
    if upgraded:
        out["candidate_templates"] = upgraded
    # 「选中的款模板」也一样（新的一天从它复制「选中的款」）
    selected = upgrade_selected_templates(ctx, api, errors)
    if selected:
        out["selected_templates"] = selected
    made = []
    for day in needed_days(ctx):
        if failed_recently(day_info(ctx, day) or {}):
            continue
        try:
            ready_day(ctx, api, day)
            made.append(day)
        except KeyError:
            errors.append({"day": day, "error": (day_info(ctx, day) or {}).get("error") or ""})
    if made:
        out["days"] = made
    moved = _move_unmoved(ctx, api, errors)
    if moved:
        out["moved"] = moved
    for info in events_pending(ctx):
        subscribe(ctx, api, info)
    return out


def has_work(ctx: Ctx) -> bool:
    """模板表格还没建好 / 没锁上，「候选表模板」「选中的款模板」的默认视图没跟上（候选表模板还看店铺列），有款要进的
    那一天还没建，或有周表格没订阅上事件（失败后一小时内不算）。"""
    if not enabled(ctx):
        return False
    tpl = template(ctx) or {}
    if not (template_ready(tpl) and tpl.get("cleaned")):
        return not failed_recently(tpl)
    if not tpl.get("locked_at") and not failed_recently(tpl, "lock_failed_at"):
        return True
    if _candidate_templates(ctx):
        return True
    if _selected_templates(ctx):
        return True
    if any(not failed_recently(day_info(ctx, d) or {}) for d in needed_days(ctx)):
        return True
    if any(d.get("unmoved") for d in days(ctx)):
        return True
    return bool(events_pending(ctx))


def reset_attempts(ctx: Ctx) -> None:
    """table sync --retry：模板、各天、各本周表格的失败都不等那一小时，「选中的款模板」改视图的失败次数清零。"""
    tpl = template(ctx)
    if tpl:
        failed = tpl.pop("failed_at", None)
        lock_failed = tpl.pop("lock_failed_at", None)
        selected = (tpl.get("tables") or {}).get("selected") or {}
        views_failed = selected.pop("views_attempts", None) is not None
        if failed or lock_failed or views_failed:
            _save_template(ctx, tpl)
    for info in days(ctx):
        if info.pop("failed_at", None) is not None or info.get("views_attempts") or info.get(
            "listing_attempts"
        ):
            info["views_attempts"] = info["listing_attempts"] = 0
            save_day(ctx, info)
    for info in books(ctx):
        events = info.get("events") or {}
        selected = (info.get("templates") or {}).get("selected") or {}
        changed = selected.pop("views_attempts", None) is not None
        if events.pop("error_at", None) is not None:
            info["events"] = events
            changed = True
        if changed:
            save_book(ctx, info)


def migrating(ctx: Ctx) -> bool:
    """别的进程正在搬一周的任务（持有 MIGRATE_LEASE）：巡检回读、记录变更事件先不处理，搬完再来。"""
    held = loads(ctx.db.get_meta(MIGRATE_LEASE), {}) or {}
    until = parse_iso(held.get("until"))
    return bool(until and until > utcnow() and held.get("owner") != ctx.owner)


def current_book(ctx: Ctx) -> dict[str, Any] | None:
    """这一周的周表格（还没建为 None）。"""
    today = utcnow().astimezone(_tz(ctx)).date()
    return book(ctx, book_id(today))


def status(ctx: Ctx) -> dict[str, Any]:
    """给 table status：开关、模板表格（链接、锁没锁）、各本周表格和各天。"""
    tpl = template(ctx) or {}
    return {
        "enabled": enabled(ctx),
        "since": since(ctx).isoformat(),
        "template": {
            "url": tpl.get("url"),
            "ready": template_ready(tpl),
            "error": tpl.get("error"),
            "locked_at": tpl.get("locked_at"),
            "lock_error": tpl.get("lock_error"),
        },
        "books": [
            {
                "book_id": b["book_id"],
                "url": b.get("url"),
                "events": b.get("events"),
                "error": b.get("error"),
            }
            for b in books(ctx)
        ],
        "days": [
            {
                "day": d["day"],
                "book_id": d.get("book_id"),
                "table_id": d.get("table_id"),
                "error": d.get("error"),
            }
            for d in days(ctx)
        ],
    }
