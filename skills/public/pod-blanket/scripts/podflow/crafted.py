"""精作款周表格（2026-10-31，`temu.listed.crafted`）：mooya 上做的款（1店 / 2店：mooya 底账里 `temu.listed.crafted.clients`
那几个客户端的行）不进「已上架的款」，按上架日期（timezone）进单独的周表格。

- 知识库根目录一个「精作款」节点，下面放锁住的「精作款模板（勿改）」和每周一本「精作款 W44（10.26–11.01）」；周表格里每天
  一张数据表「10-26 周一」，从那本的「精作款模板」复制（列、视图、「看图」的封面效果跟着走），挪到根目录。
- 模板（meta crafted.template）：「精作款模板」的列照「已上架的款」（店铺只有精作款那几家），视图「全部」+ 每家店一个 +
  「看图」（封面主图）。人在「看图」里设封面效果（接口改不了），再 `crafted lock`（编辑者只读）；锁上之前精作款的新行哪里都
  不写（不然先复制出来的周表格没有这个设置）。
- 归属（crafted_places）：一条第一次写进精作款（或迁移）时按上架日期定到那一天，之后不变。还在「已上架的款」里的（有行、
  没记过归属）照旧在那边，`migrate` 删掉那边的行后再写过来。
- 周表格（crafted_books）：知识库节点复制到「精作款」下面，关掉带过来的高级权限，「精作款模板」挪进「模板」文件夹。
- 天（crafted_days）：info_json 口径同 listed_table._meta（table_id / views / fields_version / store_options），另有
  day / book_id / app_token / url；表都登记在 table_places（kind crafted）。
- 迁移（migrate）：删掉精作款在「已上架的款」里的行（500 一批，整批失败逐行删），旧表格那张只剩精作款时整张删掉，周表格里
  那张删掉精作款店铺的视图；再按日期先后建好各周、各天。写行交给表格同步（`temu listed sync --batch N`）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from . import books, feishu, feishu_table, lease, listed_table, store, views
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso

ROOT_TITLE = "精作款"
TEMPLATE_TITLE = "精作款模板（勿改）"
TEMPLATE_TABLE = "精作款模板"
ROOT_META_KEY = "crafted.root"
TEMPLATE_META_KEY = "crafted.template"
KEY_PREFIX = "crafted:"
KIND = "crafted"
# 迁移时的租约：拿着它时巡检不回读「已上架的款」（那边的行正在删）
MIGRATE_LEASE = "crafted.migrate"
MIGRATE_SECONDS = 1800
_PAGE = 500
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)


# ---------------------------------------------------------------- 开关与命名


def enabled(ctx: Ctx) -> bool:
    """开着精作款周表格：`temu.listed.crafted.enabled`，还要读 mooya 底账、开着「已上架的款」、配了知识库。"""
    return (
        bool(ctx.setting("temu.listed.crafted.enabled", False))
        and listed_table.source_mooya(ctx)
        and listed_table.enabled(ctx)
        and bool(feishu_table.wiki_space_id(ctx))
    )


def clients(ctx: Ctx) -> list[str]:
    return [str(n) for n in ctx.setting("temu.listed.crafted.clients") or [] if n]


def is_crafted(ctx: Ctx, item: dict[str, Any]) -> bool:
    """这一条是精作款（mooya 底账里精作款客户端的行）。"""
    return enabled(ctx) and str(item.get("client") or "") in clients(ctx)


def key(day: str) -> str:
    return f"{KEY_PREFIX}{day}"


def is_key(bid: str | None) -> bool:
    return bool(bid) and str(bid).startswith(KEY_PREFIX)


def day_of(bid: str) -> str:
    return str(bid)[len(KEY_PREFIX) :]


def book_title(day: str | dt.date) -> str:
    """周表格的名字：精作款 W44（10.26–11.01）。"""
    monday = books.week_start(day)
    sunday = monday + dt.timedelta(days=6)
    _, week, _ = monday.isocalendar()
    return f"{ROOT_TITLE} W{week:02d}（{monday:%m.%d}–{sunday:%m.%d}）"


def table_name(day: str | dt.date) -> str:
    """那一天的表名：10-26 周一。"""
    return books.folder_name(day)


def store_columns(ctx: Ctx) -> list[str]:
    """精作款的店铺列选项（= 每家店一个视图）：上次拉底账时精作款客户端报的店。"""
    by_client = listed_table.mooya_stores(ctx)
    out: list[str] = []
    for client in clients(ctx):
        for spec in sorted((by_client.get(client) or {}).values(), key=lambda s: str(s.get("key") or "")):
            column = str(spec.get("column") or "")
            if column and column not in out:
                out.append(column)
    return out


# ---------------------------------------------------------------- 归属


def placed(ctx: Ctx, code: str) -> str | None:
    row = ctx.db.q1("SELECT day FROM crafted_places WHERE code = ?", (code,))
    return row["day"] if row else None


def place(ctx: Ctx, item: dict[str, Any], *, freeze: bool = True) -> str | None:
    """精作款的这一条写进哪一天（`crafted:<day>`）。还在「已上架的款」里的（有行、没记过归属）返回 None：照旧在那边，等迁移。"""
    day = placed(ctx, item["code"])
    if day:
        return key(day)
    if item.get("record_id"):
        return None
    when = books.local_day(ctx, item.get("listed_at"))
    if not when:
        return None
    if freeze:
        _set_place(ctx, item["code"], when.isoformat())
    return key(when.isoformat())


def _set_place(ctx: Ctx, code: str, day: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO crafted_places(code, day, placed_at) VALUES(?, ?, ?) ON CONFLICT(code) DO NOTHING",
            (code, day, now_iso()),
        )


def _rows(ctx: Ctx) -> list[dict[str, Any]]:
    return [i for i in listed_table.rows(ctx) if is_crafted(ctx, i)]


def not_migrated(ctx: Ctx) -> list[dict[str, Any]]:
    """精作款里还在「已上架的款」那边有行的（有 record_id、没记过精作款归属）。"""
    done = {r["code"] for r in ctx.db.q("SELECT code FROM crafted_places")}
    return [i for i in _rows(ctx) if i.get("record_id") and i["code"] not in done]


# ---------------------------------------------------------------- 「精作款」节点与模板


def root(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(ROOT_META_KEY), None) or {}


def ensure_root(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    """知识库根目录的「精作款」节点（一篇文档，只当目录用）：模板和各周表格都放在它下面。"""
    found = root(ctx)
    if found.get("node_token"):
        return found
    space = books._space(ctx)
    node = (
        api.call(
            "POST",
            f"/wiki/v2/spaces/{space}/nodes",
            json={"obj_type": "docx", "node_type": "origin", "title": ROOT_TITLE},
        ).get("node")
        or {}
    )
    found = {
        "node_token": node["node_token"],
        "obj_token": node.get("obj_token"),
        "url": books._wiki_url(ctx, str(node["node_token"])),
        "created_at": now_iso(),
    }
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, ROOT_META_KEY, dumps(found))
        log_event(conn, "table", "crafted_root_created", {"url": found["url"]})
    return found


def template(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(TEMPLATE_META_KEY), None) or {}


def _save_template(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, TEMPLATE_META_KEY, dumps(meta))


def template_ready(meta: dict[str, Any] | None) -> bool:
    return bool(meta and meta.get("app_token") and (meta.get("table") or {}).get("table_id") and meta.get("cleaned"))


def ready(ctx: Ctx) -> bool:
    """精作款的行能写了：开着、模板建好了、锁上了（人已经在「看图」里设好封面效果）。"""
    meta = template(ctx)
    return enabled(ctx) and template_ready(meta) and bool(meta.get("locked_at"))


def _specs(ctx: Ctx) -> list[dict[str, Any]]:
    """「精作款模板」的列：照「已上架的款」，店铺只列精作款那几家。"""
    specs = listed_table.field_specs(ctx)
    for spec in specs:
        if spec["field_name"] == listed_table.F_STORE:
            spec["property"] = {"options": [{"name": c} for c in store_columns(ctx)]}
    return specs


def _table_views(ctx: Ctx, api: feishu.Client, info: dict[str, Any], table_id: str) -> dict[str, Any]:
    """这张表的视图：第一个表格视图「全部」、每家店一个表格视图（按店铺筛）、画册「看图」（封面主图）；缺的补上。"""
    existing = views.list_views(api, info, table_id)
    fields = views.list_fields(api, info, table_id)
    grids = [v for v in existing if v.get("view_type") == "grid"]
    first = next((v["view_id"] for v in grids if v.get("view_name") == views.ALL_VIEW), None) or grids[0]["view_id"]
    store_field = next(f for f in fields if f.get("field_name") == listed_table.F_STORE)
    by_store: dict[str, str] = {}
    for column in store_columns(ctx):
        view_id = next((v["view_id"] for v in grids if v.get("view_name") == column), None)
        if not view_id:
            view_id = views._create(api, info, table_id, column, "grid")
            views._patch(api, info, table_id, view_id, filter_info=views.is_option(store_field, column))
        by_store[column] = view_id
    gallery = next((v["view_id"] for v in existing if v.get("view_type") == "gallery"), None)
    if not gallery:
        gallery = views._create(api, info, table_id, views.GALLERY_VIEW, "gallery")
        cover = next((f["field_id"] for f in fields if f.get("field_name") == listed_table.F_THUMB), None)
        if cover:
            # 刚建的视图 v3 接口过一两秒才查得到
            books._settle(lambda: views.set_cover(api, info, table_id, gallery, cover))
    return {"all": first, "stores": by_store, "gallery": gallery}


def ensure_template(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    """「精作款」节点下的模板表格：还没有就建（一张「精作款模板」），返回 meta。做到一半失败的，下一次接着做。
    建好后人要在「看图」里设封面效果，再 lock_template；锁上之前精作款的行不写。"""
    meta = template(ctx)
    if template_ready(meta):
        if meta.pop("error", None) is not None or meta.pop("failed_at", None) is not None:
            _save_template(ctx, meta)
        return meta
    if not meta.get("app_token"):
        parent = ensure_root(ctx, api)
        space = books._space(ctx)
        node = (
            api.call(
                "POST",
                f"/wiki/v2/spaces/{space}/nodes",
                json={
                    "obj_type": "bitable",
                    "node_type": "origin",
                    "parent_node_token": parent["node_token"],
                    "title": TEMPLATE_TITLE,
                },
            ).get("node")
            or {}
        )
        meta = {
            "app_token": str(node["obj_token"]),
            "node_token": node["node_token"],
            "url": books._wiki_url(ctx, str(node["node_token"])),
            "created_at": now_iso(),
        }
        # 先记下来：中途失败了下一次接着建，不会再建一本
        _save_template(ctx, meta)
    app = meta["app_token"]
    info = {"app_token": app, "url": meta["url"]}
    if not (meta.get("table") or {}).get("table_id"):
        names = books._tables(api, app)
        table_id = names.get(TEMPLATE_TABLE)
        if not table_id:
            table_id = api.call(
                "POST",
                f"/bitable/v1/apps/{app}/tables",
                json={"table": {"name": TEMPLATE_TABLE, "default_view_name": views.ALL_VIEW, "fields": _specs(ctx)}},
            )["table_id"]
            meta["table"] = {"table_id": table_id}
            _save_template(ctx, meta)
        meta["table"] = {
            "table_id": table_id,
            "views": _table_views(ctx, api, info, table_id),
            "fields_version": listed_table.FIELDS_VERSION,
            "store_options": store_columns(ctx),
        }
        _save_template(ctx, meta)
    elif not (meta["table"].get("views") or {}).get("gallery"):
        meta["table"]["views"] = _table_views(ctx, api, info, meta["table"]["table_id"])
        meta["table"].setdefault("fields_version", listed_table.FIELDS_VERSION)
        meta["table"].setdefault("store_options", store_columns(ctx))
        _save_template(ctx, meta)
    # 新建的多维表格自带一张空表：删掉（只在刚建好时做一次）
    for name, table_id in books._tables(api, app).items():
        if name != TEMPLATE_TABLE:
            try:
                api.call("DELETE", f"/bitable/v1/apps/{app}/tables/{table_id}")
            except feishu.FeishuError:
                pass
    meta["cleaned"] = True
    meta.pop("error", None)
    meta.pop("failed_at", None)
    _save_template(ctx, meta)
    with ctx.db.tx() as conn:
        log_event(conn, "table", "crafted_template_created", {"app_token": app, "url": meta["url"]})
    return meta


def lock_template(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    """锁模板（人已经在「看图」里设好封面效果）：编辑者只读，之后精作款的行才开始写、周表格才开始复制。"""
    meta = template(ctx)
    if not template_ready(meta):
        raise StageError("精作款模板还没建好：先 podctl temu listed crafted template")
    try:
        books.lock_tables(api, meta["app_token"], [TEMPLATE_TABLE])
    except _FAILURES as exc:
        meta["lock_error"] = books._reason(exc)
        meta["lock_failed_at"] = now_iso()
        _save_template(ctx, meta)
        return {"locked": False, "error": meta["lock_error"]}
    meta["locked_at"] = now_iso()
    meta.pop("lock_error", None)
    meta.pop("lock_failed_at", None)
    _save_template(ctx, meta)
    with ctx.db.tx() as conn:
        log_event(conn, "table", "crafted_template_locked", {"app_token": meta["app_token"]})
    feishu_table.kick(ctx)
    return {"locked": True, "url": meta.get("url")}


# ---------------------------------------------------------------- 周表格与天


def book(ctx: Ctx, bid: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT info_json FROM crafted_books WHERE book_id = ?", (bid,))
    return (loads(row["info_json"], None) or None) if row else None


def all_books(ctx: Ctx) -> list[dict[str, Any]]:
    return [loads(r["info_json"], {}) or {} for r in ctx.db.q("SELECT info_json FROM crafted_books ORDER BY book_id")]


def save_book(ctx: Ctx, info: dict[str, Any]) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO crafted_books(book_id, app_token, week_start, info_json, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(book_id) DO UPDATE SET"
            " app_token = excluded.app_token, info_json = excluded.info_json, updated_at = excluded.updated_at",
            (info["book_id"], info["app_token"], info["week_start"], dumps(info), info.get("created_at") or now, now),
        )


def ensure_book(ctx: Ctx, api: feishu.Client, day: str | dt.date) -> dict[str, Any]:
    """这一天所在那一周的精作款周表格：还没有就从模板整本复制到「精作款」下面（关掉带过来的高级权限，「精作款模板」挪进
    「模板」文件夹）。模板没锁上时报 StageError。返回周表格信息。"""
    bid = books.book_id(day)
    found = book(ctx, bid)
    if found and (found.get("template") or {}).get("table_id"):
        return found
    tpl = template(ctx)
    if not (template_ready(tpl) and tpl.get("locked_at")):
        raise StageError("精作款模板还没锁上：先在模板的「看图」里设好封面效果，再 podctl temu listed crafted lock")
    if not found:
        parent = ensure_root(ctx, api)
        space = books._space(ctx)
        node = (
            api.call(
                "POST",
                f"/wiki/v2/spaces/{space}/nodes/{tpl['node_token']}/copy",
                json={"target_space_id": space, "target_parent_token": parent["node_token"], "title": book_title(day)},
            ).get("node")
            or {}
        )
        found = {
            "book_id": bid,
            "app_token": str(node["obj_token"]),
            "node_token": node["node_token"],
            "url": books._wiki_url(ctx, str(node["node_token"])),
            "title": book_title(day),
            "week_start": books.week_start(day).isoformat(),
            "created_at": now_iso(),
        }
        # 先记下来：后面哪一步失败了，下一次接着做，不会再复制一本
        save_book(ctx, found)
        with ctx.db.tx() as conn:
            log_event(conn, "table", "crafted_book_created", {"book_id": bid, "url": found["url"]})
    app = found["app_token"]
    info = {"app_token": app, "url": found["url"]}
    if books._copied(api, app):
        # 模板锁着（高级权限），复制件也带着：周表格要人能填数据，关掉
        api.call("PUT", f"/bitable/v1/apps/{app}", json={"is_advanced": False})
        found["advanced_off_at"] = now_iso()
    names = books._tables(api, app)
    if TEMPLATE_TABLE not in names:
        raise KeyError(f"精作款周表格里没有「{TEMPLATE_TABLE}」")
    table_id = names[TEMPLATE_TABLE]
    folder = found.get("template_folder_id") or books._folder(api, app, books.TEMPLATE_FOLDER)
    found["template_folder_id"] = folder
    save_book(ctx, found)
    feishu_table.move_table(api, info, table_id, folder)
    # 整本复制后视图换了 id：按名字认回来
    found["template"] = {**(tpl.get("table") or {}), "table_id": table_id, "views": _table_views(ctx, api, info, table_id)}
    save_book(ctx, found)
    return found


def day_info(ctx: Ctx, day: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT info_json FROM crafted_days WHERE day = ?", (day,))
    return (loads(row["info_json"], None) or None) if row else None


def days(ctx: Ctx) -> list[dict[str, Any]]:
    return [loads(r["info_json"], {}) or {} for r in ctx.db.q("SELECT info_json FROM crafted_days ORDER BY day")]


def save_day(ctx: Ctx, info: dict[str, Any]) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO crafted_days(day, book_id, info_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET book_id = excluded.book_id, info_json = excluded.info_json,"
            " updated_at = excluded.updated_at",
            (info["day"], info["book_id"], dumps(info), info.get("created_at") or now, now),
        )


def ensure_day(ctx: Ctx, api: feishu.Client, day: str) -> dict[str, Any]:
    """这一天的表：从那一周周表格的「精作款模板」复制出「10-26 周一」（复制件落在「模板」文件夹里），登记、挪到根目录。
    做到一半失败的，下一次接着做。返回那一天的表信息。"""
    found = day_info(ctx, day)
    if found and found.get("table_id"):
        return found
    if not found:
        found = {"day": day, "book_id": books.book_id(day), "created_at": now_iso()}
        save_day(ctx, found)
    info_book = ensure_book(ctx, api, day)
    app = info_book["app_token"]
    found.update(app_token=app, url=info_book["url"], book_id=info_book["book_id"])
    tpl = info_book["template"]
    if not found.get("copied_table_id"):
        table_id, name = books._copy_table(api, app, tpl["table_id"], table_name(day))
        books.register(ctx, table_id, app, KIND, day)
        # 先记下来：挪到根目录失败了下一次接着挪，不会再复制一张
        found.update(copied_table_id=table_id, name=name)
        save_day(ctx, found)
    table_id = found["copied_table_id"]
    feishu_table.move_table(api, {"app_token": app}, table_id, None)
    found.update(
        table_id=table_id,
        name=found.get("name") or table_name(day),
        # 同一本表格里复制的表视图 id 和模板里的一样
        views=dict(tpl.get("views") or {}),
        fields_version=int(tpl.get("fields_version") or listed_table.FIELDS_VERSION),
        store_options=list(tpl.get("store_options") or []),
    )
    found.pop("error", None)
    found.pop("failed_at", None)
    save_day(ctx, found)
    with ctx.db.tx() as conn:
        log_event(conn, "table", "crafted_day_created", {"day": day, "book_id": found["book_id"], "table_id": table_id})
    return found


def ready_day(ctx: Ctx, api: feishu.Client, day: str) -> dict[str, Any]:
    """有精作款要进这一天：那天的表还没建就建。失败记在那一天上，一小时内不再试，这时报 KeyError（这一天的行先跳过）。"""
    found = day_info(ctx, day) or {}
    if found.get("table_id"):
        return found
    if books.failed_recently(found):
        raise KeyError(f"精作款 {day} 那天的表上次没建成（{found.get('error')}），过一会儿再试")
    try:
        return ensure_day(ctx, api, day)
    except (*_FAILURES, StageError) as exc:
        failed = {
            **(day_info(ctx, day) or {"day": day, "book_id": books.book_id(day)}),
            "error": books._reason(exc),
            "failed_at": now_iso(),
        }
        save_day(ctx, failed)
        raise KeyError(f"精作款 {day} 那天的表没建成：{failed['error']}") from exc


# ---------------------------------------------------------------- 给 listed_table 用


def table_meta(ctx: Ctx, bid: str) -> dict[str, Any]:
    """`crafted:<day>` 那一天的表记录（口径同 listed_table._meta）。"""
    return dict(day_info(ctx, day_of(bid)) or {})


def save_table_meta(ctx: Ctx, meta: dict[str, Any], bid: str) -> None:
    day = day_of(bid)
    found = day_info(ctx, day) or {"day": day, "book_id": books.book_id(day), "created_at": now_iso()}
    found.update(meta)
    save_day(ctx, found)


def places(ctx: Ctx) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """建好了的每一天：[(crafted:<day>, 那本周表格的 info, 那天的表记录)]。"""
    return [
        (key(d["day"]), {"app_token": d["app_token"], "url": d.get("url")}, d)
        for d in days(ctx)
        if d.get("table_id")
    ]


def prepare(ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]) -> dict[str, Any]:
    """表格同步开头：开着精作款时先备好「精作款」节点和模板（没建就建，失败一小时后再试）。锁要人设完封面效果后自己锁。"""
    if not enabled(ctx):
        return {}
    meta = template(ctx)
    if template_ready(meta) or books.failed_recently(meta):
        return {}
    try:
        meta = ensure_template(ctx, api)
    except (*_FAILURES, StageError) as exc:
        failed = {**template(ctx), "error": books._reason(exc), "failed_at": now_iso()}
        _save_template(ctx, failed)
        errors.append({"table": TEMPLATE_TITLE, "error": failed["error"]})
        return {"template_error": failed["error"]}
    return {"template": meta.get("url")}


def has_work(ctx: Ctx) -> bool:
    """模板还没建好（上次失败已过一小时）。"""
    if not enabled(ctx):
        return False
    meta = template(ctx)
    return not template_ready(meta) and not books.failed_recently(meta)


def migrating(ctx: Ctx) -> bool:
    return lease.active(ctx, MIGRATE_LEASE)


# ---------------------------------------------------------------- 迁移


def _old_places(ctx: Ctx) -> dict[str | None, list[dict[str, Any]]]:
    """还在「已上架的款」里的精作款，按在哪一张分组（None = 旧表格那一张，否则是周表格编号）。"""
    out: dict[str | None, list[dict[str, Any]]] = {}
    for item in not_migrated(ctx):
        out.setdefault(listed_table.place(ctx, item, freeze=False), []).append(item)
    return out


def _old_where(ctx: Ctx, bid: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """「已上架的款」那一张：(表格 info, 表记录)。"""
    if bid:
        found = books.book(ctx, bid) or {}
        return {"app_token": found.get("app_token"), "url": found.get("url")}, listed_table._meta(ctx, bid)
    return feishu_table.base(ctx) or {}, listed_table._meta(ctx)


def _days_needed(ctx: Ctx) -> list[str]:
    out = set()
    for item in _rows(ctx):
        day = placed(ctx, item["code"])
        if not day:
            when = books.local_day(ctx, item.get("listed_at"))
            day = when.isoformat() if when else None
        if day:
            out.add(day)
    return sorted(out)


def _store_views(ctx: Ctx) -> list[tuple[str, dict[str, Any], dict[str, Any], dict[str, str]]]:
    """周表格「已上架的款」里精作款店铺的视图：[(周表格编号, info, 表记录, {店铺列: 视图 id})]。本机也配了的店（这条流水线
    也上 1店 / 2店）不算：那边要留着它们的视图。"""
    wanted = set(store_columns(ctx)) - set(listed_table.local_columns(ctx))
    out = []
    for found in books.books(ctx):
        meta = found.get("listed") or {}
        extra = {c: v for c, v in ((meta.get("views") or {}).get("stores") or {}).items() if c in wanted}
        if meta.get("table_id") and extra:
            out.append((found["book_id"], {"app_token": found["app_token"], "url": found.get("url")}, meta, extra))
    return out


def plan(ctx: Ctx) -> dict[str, Any]:
    """迁移要做什么（不调飞书）：删哪几张「已上架的款」里的多少行、旧表格那张删不删、删哪些视图、要建哪几周哪几天。"""
    groups = _old_places(ctx)
    legacy_meta = listed_table._meta(ctx)
    others_in_legacy = [
        i
        for i in listed_table.rows(ctx)
        if i.get("record_id") and not is_crafted(ctx, i) and listed_table.place(ctx, i, freeze=False) is None
    ]
    needed = _days_needed(ctx)
    have_days = {d["day"] for d in days(ctx) if d.get("table_id")}
    have_books = {b["book_id"] for b in all_books(ctx) if (b.get("template") or {}).get("table_id")}
    weeks = sorted({books.book_id(d) for d in needed})
    return {
        "rows": len(_rows(ctx)),
        "delete": [
            {
                "table": listed_table.TABLE_NAME if not bid else f"{listed_table.TABLE_NAME}（{bid}）",
                "book_id": bid,
                "rows": len(group),
            }
            for bid, group in sorted(groups.items(), key=lambda kv: kv[0] or "")
        ],
        "legacy_table": {
            "table_id": legacy_meta.get("table_id"),
            "drop": bool(legacy_meta.get("table_id")) and not others_in_legacy,
            "others": len(others_in_legacy),
        },
        "views": [{"book_id": bid, "views": sorted(extra)} for bid, _info, _meta, extra in _store_views(ctx)],
        "books": [
            {"book_id": w, "title": book_title(books.week_monday(w)), "exists": w in have_books} for w in weeks
        ],
        "days": len(needed),
        "new_days": len([d for d in needed if d not in have_days]),
    }


def _delete_rows(
    ctx: Ctx, api: feishu.Client, bid: str | None, items: list[dict[str, Any]], errors: list[dict[str, str]]
) -> int:
    """删掉这些精作款在「已上架的款」那一张里的行，删掉的清掉本机的行号、记下精作款的归属。返回删了几行。"""
    info, meta = _old_where(ctx, bid)
    if not info.get("app_token") or not meta.get("table_id"):
        errors.append({"table": f"{listed_table.TABLE_NAME}（{bid or '旧表格'}）", "error": "找不到那一张表"})
        return 0
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{meta['table_id']}/records"
    done = 0
    for i in range(0, len(items), _PAGE):
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
        lease.take(ctx, MIGRATE_LEASE, MIGRATE_SECONDS)
        chunk = items[i : i + _PAGE]
        gone: list[dict[str, Any]] = []
        try:
            api.call("POST", f"{path}/batch_delete", json={"records": [r["record_id"] for r in chunk]})
            gone = chunk
        except feishu.FeishuError:
            # 有的行已经被人删了时整批会失败：逐行删，删不掉的跳过（还算在那边，下次重跑再删）
            for item in chunk:
                try:
                    api.call("DELETE", f"{path}/{item['record_id']}")
                    gone.append(item)
                except feishu.FeishuError as exc:
                    if _missing(exc):
                        gone.append(item)
                    else:
                        errors.append({"code": item["code"], "error": f"删「已上架的款」里的行没成：{exc}"[:300]})
        _moved(ctx, gone)
        done += len(gone)
    return done


def _missing(exc: feishu.FeishuError) -> bool:
    """那一行已经不在了（人删过）。"""
    text = f"{exc.msg} {exc}".lower()
    return "notfound" in text.replace("_", "").replace(" ", "") or "not found" in text


def _moved(ctx: Ctx, items: list[dict[str, Any]]) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        for item in items:
            when = books.local_day(ctx, item.get("listed_at"))
            conn.execute(
                "UPDATE listed_rows SET record_id = NULL, written_hash = NULL, attempts = 0, last_error = NULL,"
                " updated_at = ? WHERE code = ?",
                (now, item["code"]),
            )
            conn.execute("DELETE FROM listed_books WHERE code = ?", (item["code"],))
            if when:
                conn.execute(
                    "INSERT INTO crafted_places(code, day, placed_at) VALUES(?, ?, ?) ON CONFLICT(code) DO NOTHING",
                    (item["code"], when.isoformat(), now),
                )


def _drop_legacy(ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]) -> dict[str, Any]:
    """旧表格那张「已上架的款」只剩精作款（都删完了、人也没在里面加行）时整张删掉、清掉记录。"""
    from . import cells  # 回读格子依赖本模块之外的很多表，用时再导入

    meta = listed_table._meta(ctx)
    info = feishu_table.base(ctx) or {}
    if not meta.get("table_id") or not info.get("app_token"):
        return {}
    if any(
        i.get("record_id") and listed_table.place(ctx, i, freeze=False) is None for i in listed_table.rows(ctx)
    ):
        return {"kept": "还有别的店的款在那一张"}
    try:
        left = cells.read_table(api, info, meta["table_id"])
        if left:
            return {"kept": f"那一张里还有 {len(left)} 行（人加的或没删掉的），先不删"}
        api.call("DELETE", f"/bitable/v1/apps/{info['app_token']}/tables/{meta['table_id']}")
    except _FAILURES as exc:
        errors.append({"table": listed_table.TABLE_NAME, "error": books._reason(exc)})
        return {"error": books._reason(exc)}
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, listed_table.META_KEY)
        log_event(conn, "table", "listed_table_dropped", {"table_id": meta["table_id"], "why": "crafted"})
    return {"dropped": meta["table_id"]}


def _drop_store_views(ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    """周表格「已上架的款」里删掉只有精作款才上的店铺的视图（本机也上的店留着）。"""
    out = []
    for bid, info, meta, extra in _store_views(ctx):
        removed = []
        stores_ = dict((meta.get("views") or {}).get("stores") or {})
        for column, view_id in extra.items():
            try:
                views._delete(api, info, meta["table_id"], view_id)
            except feishu.FeishuError as exc:
                if not _missing(exc) and exc.code != 1254003:
                    errors.append({"table": f"{listed_table.TABLE_NAME}（{bid}）", "error": books._reason(exc)})
                    continue
            stores_.pop(column, None)
            removed.append(column)
        meta["views"] = {**(meta.get("views") or {}), "stores": stores_}
        listed_table._save_meta(ctx, meta, bid)
        out.append({"book_id": bid, "views": removed})
    return out


def migrate(ctx: Ctx, *, dry_run: bool = False) -> dict[str, Any]:
    """把精作款从「已上架的款」挪过来：删那边的行（旧表格那张只剩精作款时整张删）、删周表格那张的精作款店铺视图，按日期先后
    建好各周、各天的表；写行交给表格同步。拿着选款表的同步租约做（表格同步这时先不跑），重跑接着做、不重复删。"""
    if not enabled(ctx):
        raise StageError("精作款没打开（temu.listed.crafted.enabled，还要 temu.listed.source: mooya 和 table.wiki_space_id）")
    todo = plan(ctx)
    if dry_run:
        return {**todo, "dry_run": True}
    if not ready(ctx):
        raise StageError("先建好、锁上精作款模板：crafted template → 在「看图」里设封面效果 → crafted lock")
    if not lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS):
        raise StageError("表格正在同步（table sync 持有租约），等这一轮结束再迁")
    lease.take(ctx, MIGRATE_LEASE, MIGRATE_SECONDS)
    errors: list[dict[str, str]] = []
    out: dict[str, Any] = {"deleted": {}, "days": []}
    try:
        with feishu.Client(ctx) as api:
            # 删之前先回读一遍：人刚在「已上架的款」里填的值、改的标题先写回 mooya，挪过去时跟着写
            read = listed_table.read_back(ctx, api)
            out["read_back"] = {k: len(v) if isinstance(v, list) else v for k, v in read.items() if k in ("read", "changed", "titles")}
            for bid, items in sorted(_old_places(ctx).items(), key=lambda kv: kv[0] or ""):
                out["deleted"][bid or "旧表格"] = _delete_rows(ctx, api, bid, items, errors)
            # 归属早定在「已上架的款」、还没写过的：一并清掉
            with ctx.db.tx() as conn:
                for item in _rows(ctx):
                    if not item.get("record_id"):
                        conn.execute("DELETE FROM listed_books WHERE code = ?", (item["code"],))
            out["legacy"] = _drop_legacy(ctx, api, errors)
            out["views"] = _drop_store_views(ctx, api, errors)
            for day in _days_needed(ctx):
                lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
                lease.take(ctx, MIGRATE_LEASE, MIGRATE_SECONDS)
                try:
                    if not (day_info(ctx, day) or {}).get("table_id"):
                        ready_day(ctx, api, day)
                        out["days"].append(day)
                except KeyError as exc:
                    errors.append({"day": day, "error": str(exc)[:300]})
    finally:
        lease.drop(ctx, MIGRATE_LEASE)
        lease.drop(ctx, feishu_table.LEASE_KEY)
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "table",
            "crafted_migrated",
            {"deleted": out["deleted"], "days": len(out["days"]), "errors": len(errors)},
        )
    feishu_table.kick(ctx)
    left = plan(ctx)
    return {
        **out,
        "errors": errors[:20],
        "failed": len(errors),
        "left": {"not_migrated": sum(d["rows"] for d in left["delete"]), "new_days": left["new_days"]},
    }


# ---------------------------------------------------------------- 状态


def status(ctx: Ctx) -> dict[str, Any]:
    meta = template(ctx)
    items = _rows(ctx) if enabled(ctx) else []
    done = {r["code"] for r in ctx.db.q("SELECT code FROM crafted_places")}
    return {
        "enabled": enabled(ctx),
        "clients": clients(ctx),
        "ready": ready(ctx),
        "root": root(ctx).get("url"),
        "template": {
            "url": meta.get("url"),
            "ready": template_ready(meta),
            "locked_at": meta.get("locked_at"),
            "error": meta.get("error"),
            "lock_error": meta.get("lock_error"),
        },
        "books": [
            {"book_id": b["book_id"], "title": b.get("title"), "url": b.get("url"), "error": b.get("error")}
            for b in all_books(ctx)
        ],
        "days": len([d for d in days(ctx) if d.get("table_id")]),
        "day_errors": [{"day": d["day"], "error": d.get("error")} for d in days(ctx) if d.get("error")],
        "rows": len(items),
        "written": len([i for i in items if i.get("record_id") and i["code"] in done]),
        "not_migrated": len([i for i in items if i.get("record_id") and i["code"] not in done]),
    }
