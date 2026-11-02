"""候选表模板：新候选表从一张模板表复制结构，只能在飞书界面里设的视图设置（画册「全部画册」「已选中」「待选」卡片的
封面效果「适应」、卡片样式）跟着走——开放接口改不了这些（卡片接口只认 cover_field）。

- 模板「候选表模板（勿删）」：照新建候选表的方式建（`candidates.create_fresh` + `prepare_new`：列、店铺列、默认视图：
  表格「全部」、画册「全部画册」「已选中」「待选」），没有记录，放在「模板」文件夹里。人在它的三个画册里设好封面效果，
  以后的新表都照它（升级视图版本时已有画册依次改名挪位置、设过的封面效果跟着视图走，最后新建的「待选」要再设一次）。记在 meta
  candidates.template：{app_token, table_id, name, views, folder_id, in_folder, fields_version, package, stores,
  views_version, created_at, recreated_at, copies, last_copy_at, unmoved, last_fallback, lost_at, error, failed_at}。
- 跟上版本：代码给候选表加了列（`candidates.FIELDS_VERSION`）、开了交付包、店铺配置多了店（`candidate_stores`
  的店铺列）、默认视图换了一版（`VIEWS_VERSION`）时，同步开头先用旧候选表那一套升级把模板改好；没跟上之前不复制，
  新表照原样新建。
- 复制：`POST /base/v3/bases/:app/tables/:template/copy {"name", "range": "schema"}`，同步返回
  `{"state": "success", "table": {"id", "name"}}`（权限 base:table:create）。2026-10-28 线上实测：字段、视图 id 都不变，
  筛选、排序、可见列、封面和封面效果都跟着复制，记录不复制；新表落在模板所在的文件夹里，要挪回根目录（已经在根目录时
  挪也不报错）；复制后再写筛选 / 排序 / 可见列 / 封面不会冲掉封面效果。同名报 800010102，源表不在报 800030104。
- 模板被人删了（复制报 800030104）：这一张照原样新建，记下来，下一次同步重建模板（重建的要人再设一次封面效果）。
- 开关 table.template（缺省开），还要 table.enabled；换了一本选款表（table init --reset）旧记录作废。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from . import candidate_stores, candidates, feishu, feishu_table, packages, store, views
from .context import Ctx
from .db import log_event
from .util import dumps, loads, now_iso, parse_iso, utcnow

META_KEY = "candidates.template"
TEMPLATE_NAME = "候选表模板（勿删）"
FOLDER_NAME = "模板"
# 建模板、改模板失败后隔这么久再试（table sync --retry 马上试）
RETRY_MINUTES = 60
NAME_TAKEN = 800010102
NOT_FOUND = 800030104
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)


def enabled(ctx: Ctx) -> bool:
    return feishu_table.enabled(ctx) and bool(ctx.setting("table.template", True))


def _meta(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(META_KEY), None) or {}


def _save(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, META_KEY, dumps(meta))


def current(ctx: Ctx, info: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """这本选款表的模板（换过一本表的旧记录不算）；还没建或被删了为 None。"""
    info = info if info is not None else feishu_table.base(ctx)
    meta = _meta(ctx)
    if info and meta.get("table_id") and meta.get("app_token") == info["app_token"]:
        return meta
    return None


def _store_columns(ctx: Ctx) -> list[str]:
    return [s["field_name"] for s in candidate_stores.specs(ctx)]


def behind(ctx: Ctx, meta: dict[str, Any]) -> list[str]:
    """模板还没跟上当前版本的地方：fields（列）/ package（「套图交付包」列）/ stores（店铺列和「货号」）/
    views（默认视图）。"""
    out = []
    if int(meta.get("fields_version") or 0) < candidates.FIELDS_VERSION:
        out.append("fields")
    if packages.enabled(ctx) and not meta.get("package"):
        out.append("package")
    if not set(_store_columns(ctx)) <= set(meta.get("stores") or []):
        out.append("stores")
    if (
        views.enabled(ctx)
        and int(meta.get("views_version") or 0) < candidates.VIEWS_VERSION
    ):
        out.append("views")
    return out


def _add_store_columns(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], table_id: str
) -> list[str]:
    """给模板补上店铺配置里新加的店铺列和「货号」（加在最后），记进店铺列的记录（默认视图按它摆）。返回现在的店铺列。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{table_id}/fields"
    names = {f.get("field_name") for f in views.list_fields(api, info, table_id)}
    specs = candidate_stores.specs(ctx)
    for spec in specs:
        if spec["field_name"] not in names:
            api.call("POST", path, json=spec)
    columns = [s["field_name"] for s in specs]
    candidate_stores.mark_done(ctx, table_id, columns)
    return columns


def ready(ctx: Ctx, info: dict[str, Any] | None = None) -> bool:
    """能从模板复制：开着、这本表的模板在、跟上了当前版本。"""
    meta = current(ctx, info)
    return enabled(ctx) and meta is not None and not behind(ctx, meta)


def _backoff(meta: dict[str, Any]) -> bool:
    failed = parse_iso(meta.get("failed_at"))
    return failed is not None and utcnow() - failed < dt.timedelta(
        minutes=RETRY_MINUTES
    )


def has_work(ctx: Ctx) -> bool:
    """模板还没建（或被删了要重建）、没跟上当前版本、或有复制出来还没挪回根目录的表，而且不在失败后的等待里。"""
    if not enabled(ctx):
        return False
    info = feishu_table.base(ctx)
    if not info:
        return False
    meta = _meta(ctx)
    if _backoff(meta):
        return False
    found = current(ctx, info)
    return found is None or bool(behind(ctx, found) or found.get("unmoved"))


def reset_attempts(ctx: Ctx) -> None:
    """table sync --retry：不等失败后的那一小时，马上再试。"""
    meta = _meta(ctx)
    if meta.pop("failed_at", None) is not None:
        _save(ctx, meta)


def _fail(
    ctx: Ctx,
    meta: dict[str, Any],
    exc: BaseException,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    reason = f"{type(exc).__name__}: {exc}"[:300]
    meta.update(error=reason, failed_at=now_iso())
    _save(ctx, meta)
    errors.append({"table": TEMPLATE_NAME, "error": reason})
    return {"error": reason}


def _folder(api: feishu.Client, info: dict[str, Any], meta: dict[str, Any]) -> str:
    """「模板」文件夹的块 id：按记下的 id 核对，没了就找根目录下同名的，再没有就建一个。"""
    app = info["app_token"]
    blocks = (
        api.call("POST", f"/base/v3/bases/{app}/blocks/list", json={}).get("blocks")
        or []
    )
    folders = [b for b in blocks if b.get("type") == "folder"]
    found = next(
        (b["id"] for b in folders if b.get("id") == meta.get("folder_id")), None
    ) or next(
        (
            b["id"]
            for b in folders
            if b.get("name") == FOLDER_NAME and not b.get("parent_id")
        ),
        None,
    )
    if not found:
        found = api.call(
            "POST",
            f"/base/v3/bases/{app}/blocks",
            json={"type": "folder", "name": FOLDER_NAME},
        )["id"]
    return str(found)


def ensure(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """候选表同步开头调（持有选款表的同步租约）：还没有模板就建一张放进「模板」文件夹，没跟上当前版本就先改好，
    把复制出来、还没挪回根目录的表挪回去。返回这次做了什么（created / upgraded / moved / error），没事做为 {}。
    出错只记下来，不挡后面的同步，隔 RETRY_MINUTES 再试。"""
    if not enabled(ctx):
        return {}
    meta = _meta(ctx)
    if meta.get("app_token") != info["app_token"]:
        # 还没建过，或者换了一本选款表（table init --reset）
        meta = {"app_token": info["app_token"]}
    if _backoff(meta):
        return {}
    out: dict[str, Any] = {}
    try:
        if not meta.get("table_id"):
            table_id, name = candidates.create_fresh(ctx, api, info, TEMPLATE_NAME)
            view_set = candidates.prepare_new(ctx, api, info, table_id)
            now = now_iso()
            recreated = bool(meta.get("lost_at"))
            meta.update(
                table_id=table_id,
                name=name,
                views=view_set or {},
                in_folder=False,
                created_at=now,
                fields_version=candidates.FIELDS_VERSION,
                package=packages.enabled(ctx),
                stores=_store_columns(ctx),
                views_version=candidates.VIEWS_VERSION if view_set else 0,
            )
            if recreated:
                meta["recreated_at"] = now
            # 上一张模板被删时那次退回新建的原因不再算数
            meta.pop("last_fallback", None)
            # 先记下来：后面挪文件夹失败了也不会再建一张
            _save(ctx, meta)
            with ctx.db.tx() as conn:
                log_event(
                    conn,
                    "table",
                    "candidate_template_created",
                    {"table_id": table_id, "name": name, "recreated": recreated},
                )
            out["created"] = table_id
        if not meta.get("in_folder"):
            meta["folder_id"] = _folder(api, info, meta)
            feishu_table.move_table(api, info, meta["table_id"], meta["folder_id"])
            meta["in_folder"] = True
        upgraded = behind(ctx, meta)
        if "fields" in upgraded:
            candidates.upgrade_fields(api, info, meta["table_id"])
            meta["fields_version"] = candidates.FIELDS_VERSION
        if "package" in upgraded:
            candidates.add_package_column(api, info, meta["table_id"])
            meta["package"] = True
        if "stores" in upgraded:
            meta["stores"] = _add_store_columns(ctx, api, info, meta["table_id"])
        # 店铺列变了，默认视图的列先后也跟着变
        if "views" in upgraded or "stores" in upgraded:
            view_set = candidates.default_views(ctx, api, info, meta["table_id"])
            if not view_set:
                raise KeyError("模板表的默认视图没建成")
            meta.update(views=view_set, views_version=candidates.VIEWS_VERSION)
        if upgraded and not out.get("created"):
            out["upgraded"] = upgraded
        moved = []
        for table_id in list(meta.get("unmoved") or []):
            feishu_table.move_table(api, info, table_id, None)
            moved.append(table_id)
            meta["unmoved"] = [t for t in meta["unmoved"] if t != table_id]
        if moved:
            out["moved"] = moved
    except _FAILURES as exc:
        return {**out, **_fail(ctx, meta, exc, errors)}
    meta.pop("error", None)
    meta.pop("failed_at", None)
    if not meta.get("unmoved"):
        meta.pop("unmoved", None)
    _save(ctx, meta)
    return out


def _fallback(ctx: Ctx, meta: dict[str, Any], exc: BaseException) -> None:
    """复制没成：记下原因（这张表照原样新建）；模板被人删了就忘掉它，下一次同步重建。"""
    reason = f"{type(exc).__name__}: {exc}"[:300]
    now = now_iso()
    meta["last_fallback"] = {"at": now, "reason": reason}
    lost = isinstance(exc, feishu.FeishuError) and exc.code == NOT_FOUND
    if lost:
        meta.update(lost_at=now, lost_table_id=meta.pop("table_id", None))
        meta.update(views={}, in_folder=False)
    _save(ctx, meta)
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "table",
            "candidate_template_lost" if lost else "candidate_template_fallback",
            {"reason": reason},
        )


def copy(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], name: str
) -> tuple[str, str] | None:
    """从模板复制一张新候选表（只复制结构）并挪到根目录：返回 (table_id, 表名)。没开、模板没有或没跟上、复制失败
    返回 None（调用方照原样新建）。同名的表已经有了就在名字后面加上时间再复制一次。"""
    if not ready(ctx, info):
        return None
    meta = _meta(ctx)
    path = f"/base/v3/bases/{info['app_token']}/tables/{meta['table_id']}/copy"
    try:
        try:
            data = api.call("POST", path, json={"name": name, "range": "schema"})
        except feishu.FeishuError as exc:
            if exc.code != NAME_TAKEN:
                raise
            name = f"{name} {utcnow():%m%d%H%M}"
            data = api.call("POST", path, json={"name": name, "range": "schema"})
        table = data.get("table") or {}
        if data.get("state") != "success" or not table.get("id"):
            raise KeyError(f"复制没有马上完成：{dumps(data)[:200]}")
    except _FAILURES as exc:
        _fallback(ctx, meta, exc)
        return None
    table_id = str(table["id"])
    meta["copies"] = int(meta.get("copies") or 0) + 1
    meta["last_copy_at"] = now_iso()
    meta.pop("last_fallback", None)
    try:
        # 复制出来的表落在模板所在的文件夹里
        feishu_table.move_table(api, info, table_id, None)
    except _FAILURES:
        # 表已经能用，只是还在「模板」文件夹里：下一次同步再挪
        meta["unmoved"] = [*(meta.get("unmoved") or []), table_id]
    _save(ctx, meta)
    return table_id, str(table.get("name") or name)


def candidate_boards(ids: dict[str, str]) -> list[tuple[str, str | None]]:
    """候选表（和候选表模板）要人在飞书里设封面效果的画册：[(视图名, 视图 id)]，最后一个是「待选」。视图版本 6 是
    「全部画册」「已选中」「待选」，版本 5 没有「全部画册」，再之前是「看图」。"""
    if "picked" not in ids:
        return [(views.GALLERY_VIEW, ids.get("gallery"))]
    boards = [
        (views.PICKED_VIEW, ids.get("picked")),
        (views.PENDING_VIEW, ids.get("pending")),
    ]
    if "all_gallery" in ids:
        boards.insert(0, (views.ALL_GALLERY_VIEW, ids.get("all_gallery")))
    return boards


def status(ctx: Ctx) -> dict[str, Any]:
    """给 table status：开没开、能不能复制、模板画册的直达链接（url 是「待选」，没升级的是「看图」）、复制了几张、
    最近一次失败或退回新建的原因。"""
    info = feishu_table.base(ctx)
    meta = _meta(ctx)
    found = current(ctx, info)
    out: dict[str, Any] = {"enabled": enabled(ctx), "ready": ready(ctx, info)}
    if found and info:
        boards = candidate_boards(found.get("views") or {})
        out.update(
            table_id=found["table_id"],
            name=found.get("name"),
            url=views.link(
                info.get("url"),
                table_id=found["table_id"],
                view_id=boards[-1][1],
            ),
            created_at=found.get("created_at"),
            copies=int(found.get("copies") or 0),
            last_copy_at=found.get("last_copy_at"),
            behind=behind(ctx, found),
        )
    for key in (
        "recreated_at",
        "lost_at",
        "last_fallback",
        "unmoved",
        "error",
        "failed_at",
    ):
        if meta.get(key):
            out[key] = meta[key]
    return out


def _tables(api: feishu.Client, info: dict[str, Any]) -> dict[str, str]:
    """飞书里这本表现在有的数据表：{table_id: 表名}。"""
    out: dict[str, str] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if token:
            params["page_token"] = token
        data = api.call(
            "GET", f"/bitable/v1/apps/{info['app_token']}/tables", params=params
        )
        for item in data.get("items") or []:
            out[item["table_id"]] = item.get("name") or ""
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return out


def galleries(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    """所有画册的直达链接（封面效果只能在飞书里改，改的时候一个个点开）：候选表模板和各任务候选表的「全部画册」「已选中」
    「待选」（还没升级的少几个，每项带 view），「选中的款」「已上架的款」的「看图」；每周选款表另有模板表格的三张模板表
    （周表格都从它复制）、每天的「选中的款」。库里有、飞书里已经没有的候选表列在 missing。"""
    from . import books, listed_table  # 都依赖表格模块，放到用时再导入

    info = feishu_table.base(ctx)
    if not info:
        return {"galleries": [], "missing": []}
    known: dict[str, dict[str, str]] = {}

    def tables_of(app: str) -> dict[str, str]:
        if app not in known:
            known[app] = _tables(api, {"app_token": app})
        return known[app]

    existing = tables_of(info["app_token"])
    url = info.get("url")
    items: list[dict[str, Any]] = []
    found = current(ctx, info)
    if found and found["table_id"] in existing:
        for view, view_id in candidate_boards(found.get("views") or {}):
            items.append(
                {
                    "kind": "template",
                    "name": existing[found["table_id"]],
                    "view": view,
                    "url": views.link(url, table_id=found["table_id"], view_id=view_id),
                }
            )
    tpl = books.template(ctx) or {}
    if tpl.get("app_token"):
        names = tables_of(tpl["app_token"])
        for kind, meta in sorted((tpl.get("tables") or {}).items()):
            if meta.get("table_id") not in names:
                continue
            ids = meta.get("views") or {}
            boards = (
                candidate_boards(ids)
                if kind == "candidate"
                else [(views.GALLERY_VIEW, ids.get("gallery"))]
            )
            for view, view_id in boards:
                items.append(
                    {
                        "kind": f"book_template:{kind}",
                        "name": names[meta["table_id"]],
                        "view": view,
                        "url": views.link(
                            tpl.get("url"), table_id=meta["table_id"], view_id=view_id
                        ),
                    }
                )
    # 各本周表格里的「候选表模板」（这一周的新候选表从它复制）
    for found_book in books.books(ctx):
        meta = (found_book.get("templates") or {}).get("candidate") or {}
        names = tables_of(found_book["app_token"])
        if meta.get("table_id") not in names:
            continue
        for view, view_id in candidate_boards(meta.get("views") or {}):
            items.append(
                {
                    "kind": "week_template:candidate",
                    "book_id": found_book["book_id"],
                    "name": names[meta["table_id"]],
                    "view": view,
                    "url": views.link(
                        found_book.get("url"), table_id=meta["table_id"], view_id=view_id
                    ),
                }
            )
    for scope in books.scopes(ctx):
        names = tables_of(scope["app_token"])
        if scope["table_id"] not in names:
            continue
        items.append(
            {
                "kind": "selected",
                **({"day": scope["day"]} if scope.get("day") else {}),
                "name": names[scope["table_id"]],
                "url": views.link(
                    scope.get("url"),
                    table_id=scope["table_id"],
                    view_id=(scope.get("views") or {}).get("gallery"),
                ),
            }
        )
    missing = []
    for row in ctx.db.q(
        "SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id"
    ):
        day = books.table_day(ctx, row["table_id"])
        names = tables_of(day["app_token"]) if day else existing
        if row["table_id"] not in names:
            missing.append({"seed_id": row["seed_id"], "table_id": row["table_id"]})
            continue
        for view, view_id in candidate_boards(candidates.view_ids(ctx, row["table_id"])):
            items.append(
                {
                    "kind": "candidate",
                    "seed_id": row["seed_id"],
                    **({"day": day["day"]} if day else {}),
                    "name": names[row["table_id"]],
                    "view": view,
                    "url": views.link(
                        day.get("url") if day else url,
                        table_id=row["table_id"],
                        view_id=view_id,
                    ),
                }
            )
    for bid, where, meta in listed_table._places(ctx):
        names = tables_of(where["app_token"])
        if meta["table_id"] not in names:
            continue
        items.append(
            {
                "kind": "listed",
                **({"book_id": bid} if bid else {}),
                "name": names[meta["table_id"]],
                "url": views.link(
                    where.get("url"),
                    table_id=meta["table_id"],
                    view_id=(meta.get("views") or {}).get("gallery"),
                ),
            }
        )
    return {"galleries": items, "missing": missing}
