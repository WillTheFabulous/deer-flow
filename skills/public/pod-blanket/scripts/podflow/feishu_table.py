"""选中的款同步到飞书多维表格：每行一张选中的图，放缩略图（不放原图）、裂变方向、原图和原图下载链接。

- 表格由程序以应用身份创建（第一次同步时自动，或 `podctl table init`），把选款的人加为可管理的协作者；
  app_token / table_id / 链接记在业务库 meta。
- 配了 `table.wiki_space_id` 时，把这本表从云空间挪进该知识库（`app_token` 不变，行和缩略图都还在），
  打开链接改成知识库节点；各任务候选表的链接跟着改。没配、或应用还不是知识库成员时，表留在原处，同步照常写行。
- 同步（`podctl table sync`）：没同步过的「选中」素材先上传缩略图、再新增一行；已不是「选中」的删掉对应行；
  原图下载链接是 OSS 签名链接（不占飞书空间），快到期时续签，原图还没传到 OSS 时先不放、之后补上。
  逐条记失败次数，满 MAX_ATTEMPTS 次不再自动重试；同一时刻只有一个同步进程（库里的租约）。
- 触发：选款「选中」后 `kick()`；巡检时再 `kick()` 一次（补漏、续签、还没进知识库的再试）。
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from . import bg, feishu, images, lease, limits, store, table_registry
from . import fields as field_dict
from .context import Ctx
from .db import log_event
from .stages.common import (
    MODE_LABELS,
    StageError,
    model_label,
    reference_path,
    slot_mode,
)
from .stages.plan import (
    asset_label,
    asset_label_of,
    brief_line,
    direction_summary,
    direction_tag,
)
from .util import dumps, iso, loads, now_iso, parse_iso, utcnow

TABLE_META_KEY = "feishu_table"
ERROR_META_KEY = "feishu_table_error"
WIKI_ERROR_META_KEY = "feishu_table_wiki_error"
# 多维表格记录变更事件的订阅（候选表表顶的按钮行 / 交付包行勾上就处理）：{app_token, subscribed_at | error, error_at}
EVENTS_META_KEY = "feishu_table_events"
# 订阅失败（多半是没开权限、应用不是表格管理者）后隔这么久再试
EVENTS_RETRY_MINUTES = 60
LEASE_KEY = "feishu_table_syncer"
LEASE_SECONDS = 300
MAX_ATTEMPTS = 8
# 建表失败（多半是应用还没开权限）后，巡检隔这么久才再试，免得每 10 分钟拉起一个注定失败的进程
CREATE_RETRY_MINUTES = 60
# 挪进知识库失败（多半是没开 wiki 权限或应用还不是成员）后，巡检同样隔这么久再试
WIKI_RETRY_MINUTES = 60
WIKI_POLL_TRIES = 15
WIKI_POLL_SECONDS = 2
TABLE_NAME = "选中的款"
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)

# 多维表格字段类型：1 文本、3 单选、4 多选、5 日期、11 人员、15 超链接、17 附件；第一列是主键
FIELDS: list[dict[str, Any]] = [
    {"field_name": "素材", "type": 1},
    {"field_name": "缩略图", "type": 17},
    {"field_name": "原图", "type": 17},
    {"field_name": "原图编号", "type": 1},
    {"field_name": "方向", "type": 1},
    {"field_name": "改了什么", "type": 1},
    {"field_name": "不变", "type": 1},
    {
        "field_name": "方位",
        "type": 4,
        "property": {
            "options": [
                {"name": field_dict.dimension_name(dim)}
                for dim in field_dict.dimensions()
                if dim
                in field_dict.tier_dimensions("small")
                + field_dict.tier_dimensions("big")
            ]
        },
    },
    {
        "field_name": "类型",
        "type": 3,
        "property": {"options": [{"name": "小变化"}, {"name": "大变化"}]},
    },
    {"field_name": "模型", "type": 3},
    {
        "field_name": "出图方式",
        "type": 3,
        "property": {"options": [{"name": label} for label in MODE_LABELS.values()]},
    },
    {"field_name": "批次", "type": 3},
    {"field_name": "系列", "type": 1},
    {"field_name": "尺寸", "type": 1},
    {
        "field_name": "选中时间",
        "type": 5,
        "property": {"date_formatter": "yyyy/MM/dd HH:mm"},
    },
    {"field_name": "选中人", "type": 11, "property": {"multiple": False}},
    {"field_name": "修改自", "type": 1},
    {"field_name": "下载原图", "type": 15},
    {"field_name": "链接到期", "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}},
    {"field_name": "备注", "type": 1},
]
# 套图（场景图）的列（2026-10-20 加）：新表建表时就有，已经建好的表在第一次写套图时补上（ensure_fields）
SCENE_FIELDS: list[dict[str, Any]] = [
    {
        "field_name": "套图状态",
        "type": 3,
        "property": {
            "options": [
                {"name": name}
                for name in (
                    "排队中",
                    "出图中",
                    "等人工",
                    "已暂停",
                    "已出完",
                    "缺图",
                    "没出成",
                    "已取消",
                )
            ]
        },
    },
    {"field_name": "场景图张数", "type": 2, "property": {"formatter": "0"}},
    {"field_name": "下载套图", "type": 15},
]
FIELDS += SCENE_FIELDS
# 2026-10-29 起「选中的款」不放套图缩略图：原来的附件列「场景图」不再建，已有的由 maintenance drop-scene-images 删掉
DROPPED_SCENE_FIELD = "场景图"
# 上架状态的三列（2026-10-25 加，listing.py）：人在这里标这一款上没上架，程序只读；开着 listing.enabled 时
# 新表建表时就有，已经建好的表在表格同步时补上（ensure_fields），旧行一次性补「待上架」
LISTING_STATE_FIELD = "上架状态"
LISTING_DATE_FIELD = "上架日期"
LISTING_NOTE_FIELD = "上架备注"
LISTING_PENDING = "待上架"
LISTING_LISTED = "已上架"
LISTING_SKIPPED = "不上了"
LISTING_STATES = (LISTING_PENDING, LISTING_LISTED, LISTING_SKIPPED)
# Temu 自动上架（2026-10-28，temu.py）：定了店铺的款由程序写上架状态，多两个选项；listing.py 回读时按待上架算
LISTING_IN_PROGRESS = "上架中"
LISTING_FAILED = "上架失败"
LISTING_AUTO_STATES = (LISTING_IN_PROGRESS, LISTING_FAILED)
# 「待上架」视图（画册「看图」也一样）看的上架状态，另加空着的：上架失败的也算待上架（2026-10-30 用户定）
LISTING_TO_LIST = (LISTING_PENDING, LISTING_FAILED)
LISTING_HELP = (
    "你来标：上架了改成「已上架」，决定不上就改成「不上了」（上架日期、上架备注可填可不填）。"
    "程序只读，用来回答这个款上过没有；不影响任务归档。"
)
LISTING_HELP_TEMU = (
    "定了店铺的款由程序写（按这一款所有店铺汇总，逐店的情况看「上架备注」，走到哪一步看「当前环节」）："
    "待上架 = 还没进妙手（领货号、等出套图、出套图中、准备上架图、排队上架）；"
    "上架中 = 已经进了妙手（建箱、写品牌、等发布、发布中）；已上架 = 每家店都上了；上架失败 = 有一家没上成。"
    "没定店铺的款你来标：上架了改成「已上架」，决定不上就改成「不上了」。不影响任务归档。"
)
# Temu 自动上架在「选中的款」加的四列（temu.fields）：开着时三个表格视图把它们和上架状态、上架备注摆在「下载确认」后面
TEMU_STORE_FIELD = "店铺"
TEMU_CODE_FIELD = "货号"
TEMU_TITLE_FIELD = "上架标题"
TEMU_CONFIRM_FIELD = "确认上架"
TEMU_FIELD_NAMES = (TEMU_STORE_FIELD, TEMU_CODE_FIELD, TEMU_TITLE_FIELD, TEMU_CONFIRM_FIELD)
# 全自动上架（2026-10-30，temu.pipeline_fields）再加的四列：当前环节、环节开始、妙手 ID、标题同步
TEMU_PIPELINE_FIELD_NAMES = ("当前环节", "环节开始", "妙手 ID", "标题同步")
TEMU_FRONT = (*TEMU_FIELD_NAMES, LISTING_STATE_FIELD, LISTING_NOTE_FIELD, *TEMU_PIPELINE_FIELD_NAMES)
DATA_NOT_CHANGE = 1254606
LISTING_FIELDS: list[dict[str, Any]] = [
    {
        "field_name": LISTING_STATE_FIELD,
        "type": 3,
        "property": {"options": [{"name": name} for name in LISTING_STATES]},
        "description": {"disable_sync": True, "text": LISTING_HELP},
    },
    {
        "field_name": LISTING_DATE_FIELD,
        "type": 5,
        "property": {"date_formatter": "yyyy/MM/dd"},
    },
    {"field_name": LISTING_NOTE_FIELD, "type": 1},
]


def listing_state_spec(temu_on: bool) -> dict[str, Any]:
    """「上架状态」带上全部五个选项（补选项用）：Temu 自动上架写「上架中」「上架失败」，「上架中」视图也要用到这个选项；
    说明按开没开 Temu 自动上架。"""
    return {
        "field_name": LISTING_STATE_FIELD,
        "type": 3,
        "property": {
            "options": [
                {"name": name} for name in (*LISTING_STATES, *LISTING_AUTO_STATES)
            ]
        },
        "description": {
            "disable_sync": True,
            "text": LISTING_HELP_TEMU if temu_on else LISTING_HELP,
        },
    }


# 下载确认的两列（2026-10-27，downloads.py）：人在「下载确认」打勾 = 这一款的套图下载好了；「排序」程序写、表格视图里
# 藏起来，表按它排。开着 table.download_confirm 时新表建表时就有，已经建好的表在表格同步时补上（ensure_fields）
DOWNLOAD_CONFIRM_FIELD = "下载确认"
DOWNLOAD_ORDER_FIELD = "排序"
DOWNLOAD_CONFIRM_HELP = (
    "下载好这一款的套图后打勾，这一行沉到表底；一个批次的款都勾了，这个批次的交付包也算下载好了（任务照常自动归档）。"
    "套图还没出完的勾了不算，程序会取消；重出了新套图，勾会被取消、这一行回到上面。"
    "在卡片或候选表标了交付包已下载，这个批次的款也会自动打勾。"
)
DOWNLOAD_ORDER_HELP = "程序自动填，表按这一列排：新出完的套图在最上面，在出的在中间，下载确认过的沉到最底下。不用改"
DOWNLOAD_FIELDS: list[dict[str, Any]] = [
    {
        "field_name": DOWNLOAD_CONFIRM_FIELD,
        "type": 7,
        "description": {"disable_sync": True, "text": DOWNLOAD_CONFIRM_HELP},
    },
    {
        "field_name": DOWNLOAD_ORDER_FIELD,
        "type": 2,
        "property": {"formatter": "0"},
        "description": {"disable_sync": True, "text": DOWNLOAD_ORDER_HELP},
    },
]
# 候选表挪进去的文件夹（Base v3 块接口，权限 base:block:read / create / update；shelf.folder_name 可改名）
SHELF_FOLDER_DEFAULT = "已归档"
# 「选中的款」表格视图开头的几列（views.Layout）：素材、缩略图、两个下载链接和「下载确认」，其余列照原来的先后
# （人加的列跟着它左边那一列走），「排序」藏起来；画册不动。版本记在表格信息的 views_layout（2026-10-27 前建的视图
# 没有，同步时摆一次；版本 1 把可见列接口给的列名当成字段 id、没摆上，版本 2 再摆一次；版本 3 加「下载确认」，
# 开着下载确认时四个视图按「排序」排）
SELECTED_FRONT = ("素材", "缩略图", "下载原图", "下载套图", DOWNLOAD_CONFIRM_FIELD)
# 版本 4（2026-10-30）：开着 Temu 时当前环节等四列跟在上架备注后面
LAYOUT_VERSION = 4
# 「选中的款」默认视图这一套的版本，记在表格信息的 views_version（没记的是第一套：表格「待上架」+ 画册「看图」+ 表格
# 「已上架」「全部」）。版本 2（2026-10-30）：全部 · 看图 · 待上架 · 上架中 · 已上架（views.ensure_selected_views）
SELECTED_VIEWS_VERSION = 2
# 表格信息 views 里四个表格视图的键（另有画册 gallery）
SELECTED_GRIDS = ("all", "pending", "in_progress", "listed")

_APPROVED = "a.human_decision = 'approved'"


def enabled(ctx: Ctx) -> bool:
    return bool(ctx.setting("table.enabled", False))


def listing_enabled(ctx: Ctx) -> bool:
    """「选中的款」要不要带上架状态三列（listing.enabled，缺省 true）。"""
    return enabled(ctx) and bool(ctx.setting("listing.enabled", True))


def listing_ready(info: dict[str, Any] | None) -> bool:
    """这本表的「选中的款」已经有上架状态那一列（新建时带着，或之后补上了）。"""
    return LISTING_STATE_FIELD in set((info or {}).get("extra_fields") or [])


def base(ctx: Ctx) -> dict[str, Any] | None:
    """已建好的表格（app_token / table_id / url / shared_with / seed_files）；还没建返回 None。"""
    return loads(ctx.db.get_meta(TABLE_META_KEY), None) or None


def table_url(ctx: Ctx) -> str | None:
    """「选中的款」的链接：建好了默认视图（待上架）时直接打开那个视图。开着每周选款表时是这一周的周表格（今天的文件夹
    建好了就打开今天的「选中的款」）。"""
    from . import books, views  # 都依赖本模块，放到用时再导入

    week = books.current_book(ctx) if books.enabled(ctx) else None
    if week and week.get("url"):
        today = books.day_info(ctx, books.local_day(ctx, now_iso()).isoformat()) or {}
        if today.get("table_id") and today.get("book_id") == week.get("book_id"):
            view = (today.get("views") or {}).get("pending") if views.enabled(ctx) else None
            return views.link(week["url"], table_id=today["table_id"], view_id=view)
        return week["url"]
    info = base(ctx) if enabled(ctx) else None
    url = (info or {}).get("url") or None
    pending = ((info or {}).get("views") or {}).get("pending")
    if url and pending and views.enabled(ctx):
        return views.link(url, table_id=info.get("table_id"), view_id=pending)
    return url


def _save_base(ctx: Ctx, info: dict[str, Any]) -> None:
    """存表格信息：带 day 的是每周选款表某一天的「选中的款」（存进 table_days），其余是旧表格。"""
    if info.get("day"):
        from . import books  # 每周选款表依赖本模块，放到用时再导入

        books.save_day(ctx, info)
        return
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, TABLE_META_KEY, dumps(info))


def _records_path(info: dict[str, Any]) -> str:
    return f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"


# ---------------------------------------------------------------- 建表


def _share_targets(ctx: Ctx) -> list[str]:
    """加为协作者的 open_id：配置了就用配置，否则取最近一次在卡片上做决定的人。"""
    configured = [str(x) for x in ctx.setting("table.share_with") or [] if x]
    if configured:
        return configured
    row = ctx.db.q1(
        "SELECT decided_by FROM reviews WHERE decided_by LIKE 'feishu:ou_%' ORDER BY decided_at DESC LIMIT 1"
    )
    return [row["decided_by"].split(":", 1)[1]] if row else []


def _create_base(ctx: Ctx, api: feishu.Client) -> dict[str, Any]:
    from . import downloads  # 下载确认依赖本模块，放到用时再导入

    app = api.call(
        "POST",
        "/bitable/v1/apps",
        json={"name": ctx.setting("table.name") or "POD 毛毯选款表"},
    )["app"]
    app_token = app["app_token"]
    listing_on = listing_enabled(ctx)
    extra = (LISTING_FIELDS if listing_on else []) + (
        DOWNLOAD_FIELDS if downloads.enabled(ctx) else []
    )
    table_id = api.call(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables",
        json={
            "table": {
                "name": TABLE_NAME,
                "default_view_name": "全部",
                "fields": FIELDS + extra,
            }
        },
    )["table_id"]
    problems: list[str] = []
    default_table = app.get("default_table_id")
    if default_table and default_table != table_id:
        try:
            api.call("DELETE", f"/bitable/v1/apps/{app_token}/tables/{default_table}")
        except feishu.FeishuError as exc:
            problems.append(f"没删掉自带的空数据表：{exc}")
    try:
        api.call(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/views",
            json={"view_name": "看图", "view_type": "gallery"},
        )
    except feishu.FeishuError as exc:
        problems.append(f"没建成画册视图：{exc}")
    shared = []
    for open_id in _share_targets(ctx):
        try:
            api.call(
                "POST",
                f"/drive/v1/permissions/{app_token}/members",
                params={"type": "bitable", "need_notification": "false"},
                json={
                    "member_type": "openid",
                    "member_id": open_id,
                    "perm": "full_access",
                },
            )
            shared.append(open_id)
        except feishu.FeishuError as exc:
            problems.append(f"没加上协作者 {open_id}：{exc}")
    if not shared:
        problems.append(
            "表格目前只有应用能打开：在 podflow.yaml 的 table.share_with 填上 open_id 后执行 podctl table init --reset"
        )
    info = {
        "app_token": app_token,
        "table_id": table_id,
        "url": app.get("url"),
        "created_at": now_iso(),
        "shared_with": shared,
        "seed_files": {},
        # 建表时 FIELDS 已含后加的列（开着上架状态、下载确认时也带上那几列），不用再补；新表没有旧行要补「待上架」
        "extra_fields": sorted(f["field_name"] for f in SCENE_FIELDS + extra),
        "listing_backfilled": listing_on,
    }
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, TABLE_META_KEY, dumps(info))
        ctx.db.del_meta(conn, ERROR_META_KEY)
        # 旧表（--reset 之前）的行不再算数，选中的款全部重新同步进新表；各任务的候选表也在新表格里重建；
        # 套图列、候选表的交付包链接在新表里重新写；下载确认记在库里（selected_downloads），新表的勾和排序重写
        conn.execute("DELETE FROM table_rows")
        conn.execute("DELETE FROM selected_rows")
        conn.execute("DELETE FROM candidate_rows")
        conn.execute("DELETE FROM candidate_scene_rows")
        conn.execute("DELETE FROM candidate_controls")
        # 表顶的按钮行 / 交付包行、预留行、各行的排序跟着旧表一起作废（已下载标记是交付包的，不动）
        conn.execute("DELETE FROM candidate_actions")
        conn.execute("DELETE FROM candidate_slots")
        conn.execute("DELETE FROM candidate_orders")
        conn.execute("DELETE FROM candidate_tables")
        # 新表格要重新订阅记录变更事件
        ctx.db.del_meta(conn, EVENTS_META_KEY)
        conn.execute(
            "UPDATE scene_sets SET table_state = NULL, table_attempts = 0, table_error = NULL"
        )
        conn.execute(
            "UPDATE scene_packages SET table_state = NULL, table_attempts = 0, table_error = NULL"
        )
        # 已归档任务的候选表在新表格里重建在根目录：再挪一次进新表格的「已归档」文件夹
        conn.execute(
            "UPDATE seed_shelf SET folder_state = NULL, folder_attempts = 0, folder_error = NULL"
        )
        log_event(
            conn, "table", "table_created", {"url": info["url"], "shared_with": shared}
        )
    # 配了知识库就先挪进去，建好卡片上的链接直接是知识库节点
    place_in_wiki(ctx, api, info, announce=False)
    final = base(ctx) or info
    _announce(ctx, final, problems)
    return {**final, "problems": problems}


def wiki_space_id(ctx: Ctx) -> str:
    return str(ctx.setting("table.wiki_space_id") or "").strip()


def _in_wiki(info: dict[str, Any], space: str) -> bool:
    return bool(info.get("wiki_node_token")) and info.get("wiki_space_id") == space


def _needs_wiki(ctx: Ctx, info: dict[str, Any]) -> bool:
    space = wiki_space_id(ctx)
    return bool(space) and not _in_wiki(info, space)


def wiki_pending(ctx: Ctx) -> bool:
    """配了知识库、表已建好、还没挪进去，而且上次失败已过冷却：巡检可以再试。"""
    info = base(ctx)
    if not info or not _needs_wiki(ctx, info):
        return False
    error = loads(ctx.db.get_meta(WIKI_ERROR_META_KEY), {}) or {}
    if not error.get("at"):
        return True
    return utcnow() - parse_iso(error["at"]) >= dt.timedelta(minutes=WIKI_RETRY_MINUTES)


def _wiki_url(current: str | None, node_token: str) -> str:
    host = "https://www.feishu.cn"
    if current:
        parts = urlsplit(current)
        if parts.scheme and parts.netloc:
            host = f"{parts.scheme}://{parts.netloc}"
    return f"{host}/wiki/{node_token}"


def _wiki_failure_text(exc: BaseException) -> str:
    if isinstance(exc, feishu.FeishuError):
        msg = f"{exc.msg} {exc}".lower()
        if exc.code in (131006, 99991672) or "permission" in msg or "applied" in msg:
            return (
                "应用还不能把表格放进知识库：在飞书开放平台打开 wiki:wiki 并发布版本，"
                "再把应用加为这个知识库的可编辑成员"
            )
        if exc.code == 131005 or "space not found" in msg:
            return "知识库不存在：核对 podflow.yaml 的 table.wiki_space_id"
    return f"没放进知识库：{type(exc).__name__}: {exc}"[:300]


def _record_wiki_error(ctx: Ctx, exc: BaseException) -> str:
    text = _wiki_failure_text(exc)
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn, WIKI_ERROR_META_KEY, dumps({"at": now_iso(), "error": text})
        )
    return text


def _clear_wiki_error(ctx: Ctx) -> None:
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, WIKI_ERROR_META_KEY)


def _rewrite_candidate_urls(ctx: Ctx, wiki_url: str) -> None:
    """候选表链接跟着知识库节点走，任务列表「候选表」打开的是知识库里的那一页（只改旧表格里的，每周表格的候选表链接
    建表时就是周表格的知识库链接）。"""
    rows = ctx.db.q(
        "SELECT c.seed_id, c.table_id, c.url FROM candidate_tables c"
        " LEFT JOIN table_places p ON p.table_id = c.table_id WHERE p.table_id IS NULL"
    )
    now = now_iso()
    with ctx.db.tx() as conn:
        for row in rows:
            new = f"{wiki_url}?table={row['table_id']}"
            if row["url"] == new:
                continue
            conn.execute(
                "UPDATE candidate_tables SET url = ?, updated_at = ? WHERE seed_id = ?",
                (new, now, row["seed_id"]),
            )


def _save_wiki(
    ctx: Ctx, info: dict[str, Any], space: str, node_token: str
) -> dict[str, Any]:
    url = _wiki_url(info.get("url"), node_token)
    updated = {
        **info,
        "wiki_space_id": space,
        "wiki_node_token": node_token,
        "url": url,
    }
    _save_base(ctx, updated)
    _rewrite_candidate_urls(ctx, url)
    _clear_wiki_error(ctx)
    return updated


def _poll_move(api: feishu.Client, task_id: str, app_token: str) -> str:
    """轮询挪入任务，返回知识库节点 token。已经在知识库里时改向节点查询。"""
    for _ in range(WIKI_POLL_TRIES):
        data = api.call(
            "GET", f"/wiki/v2/tasks/{task_id}", params={"task_type": "move"}
        )
        result = ((data.get("task") or {}).get("move_result") or [{}])[0]
        status = result.get("status")
        if status == 1:
            time.sleep(WIKI_POLL_SECONDS)
            continue
        if status == 0:
            token = (result.get("node") or {}).get("node_token")
            if token:
                return str(token)
            raise feishu.FeishuError("挪入知识库成功但没有节点")
        message = str(result.get("status_msg") or "")
        if status == -1 and "already in wiki" in message:
            return _node_for_obj(api, app_token)
        raise feishu.FeishuError(f"挪入知识库失败：{message or status}", msg=message)
    raise feishu.FeishuError("挪入知识库超时：任务还在处理")


def _node_for_obj(api: feishu.Client, app_token: str) -> str:
    data = api.call(
        "GET",
        "/wiki/v2/spaces/get_node",
        params={"token": app_token, "obj_type": "bitable"},
    )
    token = (data.get("node") or {}).get("node_token")
    if not token:
        raise feishu.FeishuError("表格已在知识库里，但没有拿到节点")
    return str(token)


def _move_node(api: feishu.Client, space: str, app_token: str) -> str:
    data = api.call(
        "POST",
        f"/wiki/v2/spaces/{space}/nodes/move_docs_to_wiki",
        json={"obj_type": "bitable", "obj_token": app_token},
    )
    if data.get("wiki_token"):
        return str(data["wiki_token"])
    if data.get("applied"):
        raise feishu.FeishuError(
            "已提交迁入申请，等知识库管理员通过后再同步",
            code=131006,
            msg="applied",
        )
    task_id = data.get("task_id")
    if not task_id:
        raise feishu.FeishuError("挪入知识库没有返回节点")
    return _poll_move(api, str(task_id), app_token)


def place_in_wiki(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], *, announce: bool
) -> dict[str, Any]:
    """把这本多维表格挪进配置的知识库。没配、或已经在这个知识库里时什么都不做。失败只记错误。"""
    space = wiki_space_id(ctx)
    if not space:
        return {"wiki": "skipped"}
    current = base(ctx) or info
    if _in_wiki(current, space):
        return {"wiki": "already", "url": current.get("url")}
    app_token = current.get("app_token")
    if not app_token:
        return {"wiki": "skipped"}
    try:
        node_token = _move_node(api, space, app_token)
        updated = _save_wiki(ctx, current, space, node_token)
    except _FAILURES as exc:
        return {"wiki": "failed", "wiki_error": _record_wiki_error(ctx, exc)}
    if announce and updated.get("url"):
        _announce_wiki(ctx, updated["url"])
    return {"wiki": "moved", "url": updated.get("url")}


def events_wanted(ctx: Ctx) -> bool:
    """候选表表顶的按钮行或「选中的款」的下载确认开着时，要订阅这本表格的记录变更事件（勾上几秒内就处理）。"""
    from . import downloads  # 下载确认依赖本模块，放到用时再导入

    return enabled(ctx) and (
        bool(ctx.setting("table.control_row", True)) or downloads.enabled(ctx)
    )


def events_state(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(EVENTS_META_KEY), {}) or {}


def events_subscribed(ctx: Ctx) -> bool:
    info = base(ctx) or {}
    state = events_state(ctx)
    return (
        bool(info.get("app_token"))
        and state.get("app_token") == info["app_token"]
        and bool(state.get("subscribed_at"))
    )


def events_pending(ctx: Ctx) -> bool:
    """表已建好、还没订阅这本表格的事件（或换了表格），而且上次失败已过冷却：同步时再试。"""
    if not events_wanted(ctx):
        return False
    info = base(ctx)
    if not info or events_subscribed(ctx):
        return False
    state = events_state(ctx)
    if state.get("app_token") != info.get("app_token") or not state.get("error_at"):
        return True
    return utcnow() - parse_iso(state["error_at"]) >= dt.timedelta(
        minutes=EVENTS_RETRY_MINUTES
    )


def _events_failure_text(exc: BaseException) -> str:
    if isinstance(exc, feishu.FeishuError):
        msg = f"{exc.msg} {exc}".lower()
        if exc.code == 1069603 or "forbidden" in msg:
            return (
                "应用没有这本表格的管理权限：打开选款表，右上角「…」→「更多」→「添加文档应用」，"
                "把应用加为「可管理」，再执行 podctl table subscribe"
            )
        if exc.code in (99991672, 99991679) or "scope" in msg or "permission" in msg:
            return (
                "应用还不能订阅表格事件：在飞书开放平台开「订阅云文档事件」（docs:event:subscribe），"
                "给 bitable:app 同时开用户身份，添加事件「多维表格记录变更」后发布版本"
            )
        if exc.code == 1069604:
            return "表格不存在：用 podctl table status 核对选款表"
    return f"没订阅上：{type(exc).__name__}: {exc}"[:300]


def subscribe_events(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any]
) -> dict[str, Any]:
    """订阅这本多维表格的记录变更事件（POST drive/v1/files/:token/subscribe）。失败只记原因，一小时后再试。"""
    token = info.get("app_token")
    if not token:
        return {"subscribed": False, "error": "还没有选款表"}
    try:
        api.call(
            "POST",
            f"/drive/v1/files/{token}/subscribe",
            params={"file_type": "bitable"},
        )
    except _FAILURES as exc:
        text = _events_failure_text(exc)
        with ctx.db.tx() as conn:
            ctx.db.set_meta(
                conn,
                EVENTS_META_KEY,
                dumps({"app_token": token, "error": text, "error_at": now_iso()}),
            )
        return {"subscribed": False, "error": text}
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn,
            EVENTS_META_KEY,
            dumps({"app_token": token, "subscribed_at": now_iso()}),
        )
        log_event(conn, "table", "table_events_subscribed", {"app_token": token})
    return {"subscribed": True}


def subscribe_now(ctx: Ctx) -> dict[str, Any]:
    """`podctl table subscribe`：现在就订阅（开好权限、加好事件之后执行一次）。"""
    if not enabled(ctx):
        raise StageError("table.enabled 为 false：先在 podflow.yaml 打开 table.enabled")
    if not events_wanted(ctx):
        raise StageError("table.control_row 为 false：按钮行关着，不用订阅")
    info = base(ctx)
    if not info:
        raise StageError(
            "选款表还没建：第一张图选中后会自动建表，也可以 podctl table init"
        )
    with feishu.Client(ctx) as api:
        return subscribe_events(ctx, api, info)


def _announce_wiki(ctx: Ctx, url: str) -> None:
    from . import notify  # 通知依赖视图模块，放到用时再导入

    card = notify.pod_card(
        "选款表已放进知识库",
        [
            "「POD 毛毯选款表」已挪进知识库：各任务的候选表和「选中的款」还在这本表里。",
            "工作台的「选款表」「候选表」会打开知识库里的这本表。",
        ],
        buttons=[("打开工作台", {"action": notify.CARD_ACTION_DASHBOARD}, "default")],
        links=[("打开选款表", url, "primary")],
    )
    try:
        notify.send_pod_card(ctx, card, f"选款表已放进知识库：{url}")
    except notify.NotifyError:
        pass


def _announce(ctx: Ctx, info: dict[str, Any], problems: list[str]) -> None:
    from . import notify  # 通知依赖视图模块，放到用时再导入

    days = int(ctx.setting("table.link_days", 90))
    lines = [
        "选款里点了「选中」的图会自动同步进这张多维表格：缩略图、裂变方向、原图和原图下载链接。",
        f"表里只放缩略图（每张约 40KB），原图用下载链接拿，链接 {days} 天有效、快到期时自动续签。",
    ]
    if info["shared_with"]:
        lines.append("已把你加为可管理的协作者，可以分享给团队。")
    lines += [f"注意：{p}" for p in problems]
    card = notify.pod_card(
        "选款表已建好",
        lines,
        level="attention" if problems else "normal",
        buttons=[("打开工作台", {"action": notify.CARD_ACTION_DASHBOARD}, "default")],
        links=[("打开选款表", info["url"], "primary")] if info.get("url") else None,
    )
    try:
        notify.send_pod_card(ctx, card, f"选款表已建好：{info.get('url')}")
    except notify.NotifyError:
        pass


def init(ctx: Ctx, *, reset: bool = False) -> dict[str, Any]:
    """建表并加协作者；已经建过时直接返回（--reset 时另建一张新表，旧表不删）。

    配了知识库、而这本表还没挪进去时，不重建表，只把现有的表挪进知识库。
    """
    if not enabled(ctx):
        raise StageError("table.enabled 为 false：先在 podflow.yaml 打开 table.enabled")
    existing = base(ctx)
    if existing and not reset and not _needs_wiki(ctx, existing):
        return {**existing, "created": False}
    # 和同步共用租约：巡检拉起的同步正好也在建表时，不会建出两张表
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        raise StageError("选款表正在同步，稍后再试")
    try:
        current = base(ctx)
        if current and not reset:
            with feishu.Client(ctx) as api:
                placed = place_in_wiki(ctx, api, current, announce=True)
            return {**(base(ctx) or current), "created": False, **placed}
        with feishu.Client(ctx) as api:
            return {**_create_base(ctx, api), "created": True}
    finally:
        lease.drop(ctx, LEASE_KEY)


# ---------------------------------------------------------------- 同步


def asset_info(ctx: Ctx, asset_id: str) -> dict[str, Any]:
    row = ctx.db.q1(
        "SELECT a.asset_id, a.variant_id, a.model, a.slot_id, a.path, a.thumb_path, a.width, a.height,"
        " a.updated_at, v.axis, v.plan_json, v.batch_id, v.family_id, v.family_version,"
        " f.seed_id, s.original_path FROM assets a"
        " LEFT JOIN variants v ON v.variant_id = a.variant_id"
        " LEFT JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " LEFT JOIN seeds s ON s.seed_id = f.seed_id"
        " WHERE a.asset_id = ?",
        (asset_id,),
    )
    if not row:
        raise KeyError(f"没有素材 {asset_id}")
    info = dict(row)
    review = ctx.db.q1(
        "SELECT decided_by, decided_at, payload_json FROM reviews"
        " WHERE subject_type = 'asset_selection' AND subject_id = ? AND decision = 'approved'"
        " ORDER BY decided_at DESC LIMIT 1",
        (asset_id,),
    )
    info["review"] = dict(review) if review else {}
    return info


def _ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def ensure_fields(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], specs: list[dict[str, Any]]
) -> list[str]:
    """给已经建好的「选中的款」补上新版本加的列；补过的记在表格信息里，之后不再去查。返回这次补上的列名。"""
    names = {spec["field_name"] for spec in specs}
    done = set(info.get("extra_fields") or [])
    if names <= done:
        return []
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/fields"
    existing: set[str] = set()
    token = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if token:
            params["page_token"] = token
        data = api.call("GET", path, params=params)
        existing |= {item.get("field_name") for item in data.get("items") or []}
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            break
    added = []
    for spec in specs:
        if spec["field_name"] not in existing:
            api.call("POST", path, json=spec)
            added.append(spec["field_name"])
    info["extra_fields"] = sorted(done | names)
    _save_base(ctx, info)
    if added:
        with ctx.db.tx() as conn:
            log_event(conn, "table", "table_fields_added", {"fields": added})
    return added


def ensure_options(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    specs: list[dict[str, Any]],
    *,
    key: str,
) -> dict[str, list[str]]:
    """给「选中的款」里已有的单选 / 多选列补上缺的选项，列说明也写成 spec 里的（add_options）。补好的选项和说明记在
    表格信息的 key 里，没变就不再去查；只是说明改了，也给每张表补写一次。返回 {列名: 这次补上的选项}。"""
    stamp = dumps(
        {
            s["field_name"]: {
                "options": [o["name"] for o in s["property"]["options"]],
                "help": (s.get("description") or {}).get("text"),
            }
            for s in specs
        }
    )
    if info.get(key) == stamp:
        return {}
    added = add_options(api, info, specs, rewrite_help=True)
    info[key] = stamp
    _save_base(ctx, info)
    if added:
        with ctx.db.tx() as conn:
            log_event(conn, "table", "table_options_added", added)
    return added


def add_options(
    api: feishu.Client,
    info: dict[str, Any],
    specs: list[dict[str, Any]],
    *,
    rewrite_help: bool = False,
) -> dict[str, list[str]]:
    """给 info 这张表（app_token / table_id）里已有的单选 / 多选列补上缺的选项（说明一并改成 spec 里的）；rewrite_help：
    选项都在的列也把说明写成 spec 里的。飞书「更新字段」是全量覆盖：原有选项连 id 一起带回，格子里已有的值才不丢。
    不记、不存表格信息（模板表也用）。返回 {列名: 这次补上的选项}。"""
    from . import views  # 视图模块依赖本模块，放到用时再导入

    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/fields"
    by_name = {
        f.get("field_name"): f for f in views.list_fields(api, info, info["table_id"])
    }
    added: dict[str, list[str]] = {}
    for spec in specs:
        field = by_name.get(spec["field_name"])
        if field is None:
            continue
        current = list((field.get("property") or {}).get("options") or [])
        have = {o.get("name") for o in current}
        missing = [
            o["name"] for o in spec["property"]["options"] if o["name"] not in have
        ]
        if not missing and not (rewrite_help and spec.get("description")):
            continue
        body: dict[str, Any] = {
            "field_name": spec["field_name"],
            "type": field["type"],
            "property": {
                **(field.get("property") or {}),
                "options": [
                    {k: o[k] for k in ("id", "name", "color") if k in o} for o in current
                ]
                + [{"name": name} for name in missing],
            },
        }
        if spec.get("description"):
            body["description"] = spec["description"]
        try:
            api.call("PUT", f"{path}/{field['field_id']}", json=body)
        except feishu.FeishuError as exc:
            if exc.code != DATA_NOT_CHANGE:
                raise
        if missing:
            added[spec["field_name"]] = missing
    return added


def upload_image(api: feishu.Client, app_token: str, name: str, data: bytes) -> str:
    return api.call(
        "POST",
        "/drive/v1/medias/upload_all",
        data={
            "file_name": name,
            "parent_type": "bitable_image",
            "parent_node": app_token,
            "size": str(len(data)),
        },
        files={"file": (name, data, "image/jpeg")},
    )["file_token"]


def thumb_bytes(ctx: Ctx, thumb_path: str | None, path: str | None) -> bytes:
    if thumb_path:
        local = store.ensure_local(ctx, thumb_path)
        if local.is_file():
            return local.read_bytes()
    if not path:
        raise store.StorageError("没有可用的图片文件")
    return images.thumbnail_jpeg(store.ensure_local(ctx, path).read_bytes())


def _seed_file(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], asset: dict[str, Any]
) -> str | None:
    """参考图的缩略图只传一次，记下 file_token 给之后的行复用；提取过平面图案就传图案（换了图案重新传）。"""
    seed_id = asset["seed_id"]
    if not seed_id or not asset["original_path"]:
        return None
    ref = reference_path(
        ctx, {"seed_id": seed_id, "original_path": asset["original_path"]}
    )
    key = seed_id if ref == asset["original_path"] else f"{seed_id}:{Path(ref).name}"
    cached = (info.get("seed_files") or {}).get(key)
    if cached:
        return cached
    token = upload_image(
        api,
        info["app_token"],
        f"{seed_id}.jpg",
        thumb_bytes(ctx, None, ref),
    )
    info.setdefault("seed_files", {})[key] = token
    _save_base(ctx, info)
    return token


def _base_file(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], asset_id: str
) -> str | None:
    """二次大变化的底图缩略图：同一张底图只传一次（和参考图共用 seed_files 缓存，键是「base:素材编号」）。"""
    key = f"base:{asset_id}"
    cached = (info.get("seed_files") or {}).get(key)
    if cached:
        return cached
    row = ctx.db.q1(
        "SELECT path, thumb_path FROM assets WHERE asset_id = ?", (asset_id,)
    )
    if not row:
        return None
    token = upload_image(
        api,
        info["app_token"],
        f"{asset_id}.jpg",
        thumb_bytes(ctx, row["thumb_path"], row["path"]),
    )
    info.setdefault("seed_files", {})[key] = token
    _save_base(ctx, info)
    return token


def download_name(label: str, path: str | None) -> str:
    """下载 / 发送用的文件名：素材名字 + 原图后缀（IMG00123_三伙伴开怀大笑.png）。"""
    return f"{label}{Path(path or '').suffix}"


def download_link(
    ctx: Ctx, path: str | None, filename: str | None = None
) -> dict[str, Any] | None:
    """原图下载链接（OSS 签名，下载成 filename，缺省用存储里的文件名）；本机存储或原图还没传到 OSS 时返回 None。"""
    if not path or not store.oss_enabled(ctx):
        return None
    status = ctx.db.scalar("SELECT status FROM storage_objects WHERE path = ?", (path,))
    if status != "uploaded":
        return None
    days = int(ctx.setting("table.link_days", 90))
    expires = utcnow() + dt.timedelta(days=days)
    url = store.client(ctx).sign_url(
        store.object_key(ctx, path),
        days * 86400,
        filename=filename or Path(path).name,
        fallback=Path(path).name,
    )
    local = expires.astimezone(ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai"))))
    return {
        "fields": {
            "下载原图": {"link": url, "text": f"下载原图（{local:%Y-%m-%d} 前有效）"},
            "链接到期": _ms(expires),
        },
        "expires_at": iso(expires),
    }


def _fields(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], asset: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """一行的字段，以及链接到期时间（没有链接时为 None）。空值不写，免得覆盖成空。"""
    plan = loads(asset["plan_json"], {}) or {}
    summary = direction_summary(plan, asset["axis"])
    label = asset_label(asset["asset_id"], plan, asset["axis"])
    review = asset["review"]
    payload = loads(review.get("payload_json"), {}) or {}
    decided_by = str(review.get("decided_by") or "")
    tag = direction_tag(summary["task_type"], summary["aspects"], summary["kind"])
    fields: dict[str, Any] = {
        "素材": label,
        "缩略图": [
            {
                "file_token": upload_image(
                    api,
                    info["app_token"],
                    f"{label}.jpg",
                    thumb_bytes(ctx, asset["thumb_path"], asset["path"]),
                )
            }
        ],
        "原图编号": asset["seed_id"],
        "方向": " ".join(p for p in (asset["variant_id"], summary["title"]) if p),
        "改了什么": "\n".join(brief_line(b) for b in summary["brief"]),
        "不变": summary["keep"],
        "方位": summary["aspects"],
        "类型": tag.split(" · ", 1)[0] if tag else None,
        "模型": model_label(asset["model"]),
        "出图方式": MODE_LABELS[slot_mode(asset["slot_id"])],
        "批次": asset["batch_id"],
        "系列": f"{asset['family_id']} v{asset['family_version']}"
        if asset["family_id"]
        else None,
        "尺寸": f"{asset['width']}×{asset['height']}" if asset["width"] else None,
        "选中时间": _ms(parse_iso(review.get("decided_at") or asset["updated_at"])),
        "选中人": [{"id": decided_by.split(":", 1)[1]}]
        if decided_by.startswith("feishu:ou_") and not info.get("no_person")
        else None,
        "修改自": f"{payload['fixed_from']}：{payload.get('fix_note') or ''}".rstrip(
            "："
        )
        if payload.get("fixed_from")
        else None,
    }
    # 新进表的款先标「待上架」，之后由人改（程序不再写这一列）
    if listing_enabled(ctx) and listing_ready(info):
        fields[LISTING_STATE_FIELD] = LISTING_PENDING
    base = plan.get("base") if isinstance(plan.get("base"), dict) else {}
    # 二次大变化的「原图」放底图（换了主体的小变化图），其余放参考图
    seed_token = (
        _base_file(ctx, api, info, base["asset_id"])
        if base.get("asset_id")
        else _seed_file(ctx, api, info, asset)
    )
    if seed_token:
        fields["原图"] = [{"file_token": seed_token}]
    link = download_link(ctx, asset["path"], download_name(label, asset["path"]))
    if link:
        fields.update(link["fields"])
    return (
        {k: v for k, v in fields.items() if v not in (None, "", [])},
        link["expires_at"] if link else None,
    )


def _create_record(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], fields: dict[str, Any]
) -> str:
    params = {"user_id_type": "open_id"}
    try:
        data = api.call(
            "POST", _records_path(info), params=params, json={"fields": fields}
        )
    except feishu.FeishuError:
        if "选中人" not in fields:
            raise
        # 人员字段写不进去（例如对方不在应用可见范围）：去掉人员再写，之后的行也不再写
        rest = {k: v for k, v in fields.items() if k != "选中人"}
        data = api.call(
            "POST", _records_path(info), params=params, json={"fields": rest}
        )
        info["no_person"] = True
        _save_base(ctx, info)
    return data["record"]["record_id"]


def _mark(ctx: Ctx, asset_id: str, **values: Any) -> None:
    """写一行同步记录（没有就新增）。"""
    values = {"updated_at": now_iso(), **values}
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO table_rows(asset_id, {cols}) VALUES(?, {marks})"
            f" ON CONFLICT(asset_id) DO UPDATE SET {sets}",
            (asset_id, *values.values()),
        )


def _fail(ctx: Ctx, asset_id: str, exc: BaseException) -> str:
    reason = f"{type(exc).__name__}: {exc}"[:500]
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO table_rows(asset_id, attempts, last_error, updated_at) VALUES(?, 1, ?, ?)"
            " ON CONFLICT(asset_id) DO UPDATE SET attempts = table_rows.attempts + 1,"
            " last_error = excluded.last_error, updated_at = excluded.updated_at",
            (asset_id, reason, now_iso()),
        )
    return reason


def _pending(ctx: Ctx) -> list[str]:
    """该新增的行：选中了、还没有记录、失败没到上限（按份同步时只挑归到这一份的）。"""
    return [
        r["asset_id"]
        for r in ctx.db.q(
            "SELECT a.asset_id FROM assets a LEFT JOIN table_rows t ON t.asset_id = a.asset_id"
            f" WHERE {_APPROVED} AND t.record_id IS NULL AND COALESCE(t.attempts, 0) < ?"
            " ORDER BY a.asset_id",
            (MAX_ATTEMPTS,),
        )
        if table_registry.mine(r["asset_id"])
    ]


_STALE_SQL = (
    " FROM table_rows t JOIN assets a ON a.asset_id = t.asset_id"
    " WHERE (a.human_decision IS NULL OR a.human_decision <> 'approved') AND t.attempts < ?"
)

# 该补链接（原图已上 OSS）或续签（快到期）的行
_REFRESH_SQL = (
    " FROM table_rows t JOIN assets a ON a.asset_id = t.asset_id"
    f" WHERE {_APPROVED} AND t.record_id IS NOT NULL AND t.attempts < ?"
    " AND ((t.link_expires_at IS NULL AND EXISTS ("
    "SELECT 1 FROM storage_objects o WHERE o.path = a.path AND o.status = 'uploaded'))"
    " OR t.link_expires_at < ?)"
)


def refresh_before(ctx: Ctx) -> str:
    days = float(ctx.setting("table.link_refresh_days", 15))
    return iso(utcnow() + dt.timedelta(days=days))


def _remove_stale(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    from . import cells  # 人改格子的日志依赖本模块之外的表，放到用时再导入

    removed = 0
    for row in ctx.db.q("SELECT t.asset_id, t.record_id" + _STALE_SQL, (MAX_ATTEMPTS,)):
        if not table_registry.mine(row["asset_id"]):
            continue
        try:
            if row["record_id"]:
                # 删之前整行存一份：人填的备注、人加的列（2026-10-30 前删了就没了）
                cells.snapshot_records(
                    ctx,
                    api,
                    info,
                    info["table_id"],
                    [row["record_id"]],
                    kind=cells.KIND_SELECTED,
                    reason="不再是选中的款，删行",
                    refs={row["record_id"]: {"asset_id": row["asset_id"]}},
                )
                api.call("DELETE", f"{_records_path(info)}/{row['record_id']}")
            with ctx.db.tx() as conn:
                conn.execute(
                    "DELETE FROM table_rows WHERE asset_id = ?", (row["asset_id"],)
                )
                conn.execute(
                    "DELETE FROM selected_rows WHERE asset_id = ?", (row["asset_id"],)
                )
            removed += 1
        except _FAILURES as exc:
            errors.append(
                {"asset_id": row["asset_id"], "error": _fail(ctx, row["asset_id"], exc)}
            )
    return removed


def _refresh_links(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    refreshed = 0
    for row in ctx.db.q(
        "SELECT t.asset_id, t.record_id, a.path" + _REFRESH_SQL,
        (MAX_ATTEMPTS, refresh_before(ctx)),
    ):
        if not table_registry.mine(row["asset_id"]):
            continue
        try:
            link = download_link(
                ctx,
                row["path"],
                download_name(asset_label_of(ctx, row["asset_id"]), row["path"]),
            )
            if not link:
                continue
            api.call(
                "PUT",
                f"{_records_path(info)}/{row['record_id']}",
                params={"user_id_type": "open_id"},
                json={"fields": link["fields"]},
            )
            _mark(
                ctx,
                row["asset_id"],
                link_expires_at=link["expires_at"],
                synced_at=now_iso(),
            )
            refreshed += 1
        except _FAILURES as exc:
            errors.append(
                {"asset_id": row["asset_id"], "error": _fail(ctx, row["asset_id"], exc)}
            )
    return refreshed


def relabel_rows(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, int]:
    """把「选中的款」已有行的素材名字改成「编号_方向短语」（调用方持租约）：素材列、缩略图（按新名字重传）、
    下载原图（按新名字重签）。出错只记下来，不计入行的失败次数。"""
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    out = {"rows": 0, "updated": 0, "links": 0}
    with books.scoped(ctx, info):
        rows = [
            dict(r)
            for r in ctx.db.q(
                "SELECT asset_id, record_id FROM table_rows WHERE record_id IS NOT NULL"
                " ORDER BY asset_id"
            )
            if table_registry.mine(r["asset_id"])
        ]
    for row in rows:
        out["rows"] += 1
        try:
            asset = asset_info(ctx, row["asset_id"])
            label = asset_label(
                asset["asset_id"], loads(asset["plan_json"], {}) or {}, asset["axis"]
            )
            fields: dict[str, Any] = {
                "素材": label,
                "缩略图": [
                    {
                        "file_token": upload_image(
                            api,
                            info["app_token"],
                            f"{label}.jpg",
                            thumb_bytes(ctx, asset["thumb_path"], asset["path"]),
                        )
                    }
                ],
            }
            link = download_link(
                ctx, asset["path"], download_name(label, asset["path"])
            )
            if link:
                fields.update(link["fields"])
            api.call(
                "PUT",
                f"{_records_path(info)}/{row['record_id']}",
                params={"user_id_type": "open_id"},
                json={"fields": fields},
            )
            if link:
                _mark(
                    ctx,
                    row["asset_id"],
                    link_expires_at=link["expires_at"],
                    synced_at=now_iso(),
                )
                out["links"] += 1
            out["updated"] += 1
        except _FAILURES as exc:
            errors.append(
                {
                    "asset_id": row["asset_id"],
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
    return out


# ---------------------------------------------------------------- 上架状态三列与默认视图


def _listing_work(ctx: Ctx, info: dict[str, Any]) -> bool:
    """「选中的款」还没补上架状态三列、或旧行还没补「待上架」（失败满上限的不再试）。"""
    if (
        not listing_enabled(ctx)
        or int(info.get("listing_attempts") or 0) >= MAX_ATTEMPTS
    ):
        return False
    return not listing_ready(info) or not info.get("listing_backfilled")


def _views_work(ctx: Ctx, info: dict[str, Any]) -> bool:
    """「选中的款」还没建默认视图，或还是之前那一套视图（views_version），或列的先后还没按当前这套摆，或开着下载确认、
    视图还没按「排序」排（要先有上架状态那一列，开着下载确认时还要先有「下载确认」「排序」两列；失败满上限的不再试）。"""
    from . import downloads, views

    confirm_on = downloads.enabled(ctx)
    return (
        views.enabled(ctx)
        and listing_enabled(ctx)
        and listing_ready(info)
        and (not confirm_on or downloads.ready(info))
        and (
            not info.get("views")
            or int(info.get("views_version") or 0) < SELECTED_VIEWS_VERSION
            or int(info.get("views_layout") or 0) < LAYOUT_VERSION
            or (confirm_on and not info.get("views_sorted"))
            or (_temu_on(ctx, info) and not info.get("views_temu"))
        )
        and int(info.get("views_attempts") or 0) < MAX_ATTEMPTS
    )


def _temu_on(ctx: Ctx, info: dict[str, Any]) -> bool:
    """开着 Temu 自动上架、「选中的款」已经有那四列和上架状态的新选项（表格视图要把上架几列摆到前面）。"""
    from . import temu  # 上架模块依赖本模块，放到用时再导入

    return temu.enabled(ctx) and temu.ready(info)


def _note_failure(
    ctx: Ctx,
    info: dict[str, Any],
    key: str,
    exc: BaseException,
    errors: list[dict[str, str]],
) -> None:
    reason = f"{type(exc).__name__}: {exc}"[:300]
    info[f"{key}_attempts"] = int(info.get(f"{key}_attempts") or 0) + 1
    info[f"{key}_error"] = reason
    _save_base(ctx, info)
    errors.append({"table": TABLE_NAME, "error": reason})


def _read_listing_cells(api: feishu.Client, info: dict[str, Any]) -> dict[str, Any]:
    """「选中的款」每一行的上架状态：{record_id: 原样的格子值（没填为 None）}。"""
    cells: dict[str, Any] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "page_size": 500,
            "field_names": dumps([LISTING_STATE_FIELD]),
        }
        if token:
            params["page_token"] = token
        data = api.call("GET", _records_path(info), params=params)
        for item in data.get("items") or []:
            cells[item["record_id"]] = (item.get("fields") or {}).get(
                LISTING_STATE_FIELD
            )
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return cells


def _ensure_listing(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """补上架状态三列（ensure_fields），再把旧行一次性补成「待上架」（只改空的格子）。"""
    if not _listing_work(ctx, info):
        return {}
    out: dict[str, Any] = {}
    try:
        out["fields_added"] = ensure_fields(ctx, api, info, LISTING_FIELDS)
        if not info.get("listing_backfilled"):
            empty = [
                r for r, cell in _read_listing_cells(api, info).items() if not cell
            ]
            for i in range(0, len(empty), 500):
                api.call(
                    "POST",
                    _records_path(info) + "/batch_update",
                    params={"user_id_type": "open_id"},
                    json={
                        "records": [
                            {
                                "record_id": r,
                                "fields": {LISTING_STATE_FIELD: LISTING_PENDING},
                            }
                            for r in empty[i : i + 500]
                        ]
                    },
                )
                lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
            info["listing_backfilled"] = True
            info.pop("listing_error", None)
            info["listing_attempts"] = 0
            _save_base(ctx, info)
            out["backfilled"] = len(empty)
            with ctx.db.tx() as conn:
                log_event(conn, "table", "listing_backfilled", {"rows": len(empty)})
    except _FAILURES as exc:
        _note_failure(ctx, info, "listing", exc, errors)
        out["error"] = info["listing_error"]
    return out


def selected_layout(temu_on: bool) -> Any:
    """「选中的款」四个表格视图的列先后（开着 Temu 自动上架时上架几列也排到前面），「排序」藏起来。"""
    from . import views

    return views.Layout(
        front=SELECTED_FRONT + (TEMU_FRONT if temu_on else ()),
        known=frozenset(
            {
                *(f["field_name"] for f in FIELDS + LISTING_FIELDS + DOWNLOAD_FIELDS),
                *TEMU_FIELD_NAMES,
                *TEMU_PIPELINE_FIELD_NAMES,
            }
        ),
        hidden=(DOWNLOAD_ORDER_FIELD,),
    )


def build_selected_views(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    *,
    layout: Any,
    order_field: str | None,
    resort: bool = True,
) -> dict[str, str]:
    """「选中的款」（旧表格的、每天的，或模板表）建默认视图、或从之前那一套就地升级（views.ensure_selected_views）：先给
    「上架状态」补齐五个选项（「上架中」视图的筛选要用到这个选项），返回五个视图的 id。不存表格信息（调用方存）。"""
    from . import temu, views  # 都依赖本模块，放到用时再导入

    added = add_options(api, info, [listing_state_spec(temu.enabled(ctx))])
    if added:
        with ctx.db.tx() as conn:
            log_event(conn, "table", "table_options_added", added)
    return views.ensure_selected_views(
        api,
        info,
        state_field=LISTING_STATE_FIELD,
        pending_options=list(LISTING_TO_LIST),
        in_progress_option=LISTING_IN_PROGRESS,
        listed_option=LISTING_LISTED,
        layout=layout,
        order_field=order_field,
        resort=resort,
    )


def _ensure_views(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """「选中的款」的默认视图：全部（第一个）/ 看图 / 待上架 / 上架中 / 已上架，建一次，id 记在表格信息的 views；之前那一套
    （views_version 没到 SELECTED_VIEWS_VERSION）就地改一次（build_selected_views）。四个表格视图的列按 SELECTED_FRONT 摆，
    开着下载确认时五个视图按「排序」排；已经是这一套的只摆列、补排序。"""
    from . import downloads, views

    if not _views_work(ctx, info):
        return {}
    order = DOWNLOAD_ORDER_FIELD if downloads.enabled(ctx) else None
    temu_on = _temu_on(ctx, info)
    layout = selected_layout(temu_on)
    ids = info.get("views")
    try:
        if ids and int(info.get("views_version") or 0) >= SELECTED_VIEWS_VERSION:
            views.apply_layout(
                api,
                info,
                info["table_id"],
                layout,
                grids=tuple(ids[key] for key in SELECTED_GRIDS),
            )
            if order and not info.get("views_sorted"):
                for view_id in ids.values():
                    views.set_sort(api, info, info["table_id"], view_id, order)
            event = "table_views_arranged"
        else:
            event = "table_views_upgraded" if ids else "table_views_created"
            ids = build_selected_views(
                ctx,
                api,
                info,
                layout=layout,
                order_field=order,
                resort=not (ids and info.get("views_sorted")),
            )
    except _FAILURES as exc:
        _note_failure(ctx, info, "views", exc, errors)
        return {"error": info["views_error"]}
    info["views"] = ids
    info["views_version"] = SELECTED_VIEWS_VERSION
    info["views_layout"] = LAYOUT_VERSION
    info["views_sorted"] = bool(order)
    info["views_temu"] = temu_on
    info.pop("views_error", None)
    info["views_attempts"] = 0
    _save_base(ctx, info)
    with ctx.db.tx() as conn:
        log_event(conn, "table", event, ids)
    return {"views": ids}


# ---------------------------------------------------------------- 「已归档」文件夹（Base v3 块接口）


def shelf_folder(ctx: Ctx, api: feishu.Client, info: dict[str, Any]) -> str:
    """「已归档」文件夹的块 id：按记下的 id 在块列表里核对，没了就找根目录下同名的文件夹，再没有就建一个。

    块接口 POST /base/v3/bases/:app/blocks/list | blocks（权限 base:block:read / create）；id 记在表格信息的 shelf_folder。
    """
    name = (
        str(ctx.setting("shelf.folder_name") or SHELF_FOLDER_DEFAULT).strip()
        or SHELF_FOLDER_DEFAULT
    )
    app = info["app_token"]
    blocks = (
        api.call("POST", f"/base/v3/bases/{app}/blocks/list", json={}).get("blocks")
        or []
    )
    folders = [b for b in blocks if b.get("type") == "folder"]
    known = (info.get("shelf_folder") or {}).get("id")
    found = next((b["id"] for b in folders if b.get("id") == known), None) or next(
        (b["id"] for b in folders if b.get("name") == name and not b.get("parent_id")),
        None,
    )
    if not found:
        found = api.call(
            "POST",
            f"/base/v3/bases/{app}/blocks",
            json={"type": "folder", "name": name},
        )["id"]
        with ctx.db.tx() as conn:
            log_event(
                conn, "table", "shelf_folder_created", {"id": found, "name": name}
            )
    if found != known or (info.get("shelf_folder") or {}).get("name") != name:
        info["shelf_folder"] = {"id": found, "name": name}
        _save_base(ctx, info)
    return str(found)


def move_table(
    api: feishu.Client, info: dict[str, Any], table_id: str, parent_id: str | None
) -> dict[str, Any]:
    """把一张数据表挪进文件夹（parent_id 为 None 时挪回根目录）：POST /base/v3/bases/:app/blocks/:table/move。"""
    return api.call(
        "POST",
        f"/base/v3/bases/{table_registry.app_for(info, table_id)}/blocks/{table_id}/move",
        json={"parent_id": parent_id},
    )


def _sync_scope(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    errors: list[dict[str, str]],
    out: dict[str, Any],
) -> dict[str, Any]:
    """同步一份「选中的款」（旧表格的，或每周表格某一天的；调用方在 books.scoped 里调，只挑归到这一份的款）：
    补列、默认视图、删行、新增、续链接、套图列、排序与下载确认、上架几列。返回这一份做了什么。"""
    from . import downloads, scenes, temu  # 都依赖本模块，放到用时再导入

    part: dict[str, Any] = {}
    # 上架状态三列与默认视图：要先有这一列，新进表的行才写「待上架」
    listing_out = _ensure_listing(ctx, api, info, errors)
    if listing_out:
        part["listing"] = listing_out
    # 下载确认两列也要在默认视图之前补上：视图按「排序」排
    columns_out = downloads.ensure_columns(ctx, api, info, errors)
    if columns_out:
        part["download_columns"] = columns_out
    # 上架四列与上架状态的新选项（开着 Temu 自动上架时）：也在默认视图之前，表格视图要把上架几列摆到前面
    temu_columns = temu.ensure_columns(ctx, api, info, errors)
    if temu_columns:
        part["temu_columns"] = temu_columns
    views_out = _ensure_views(ctx, api, info, errors)
    if views_out:
        part["views"] = views_out
    part["removed"] = _remove_stale(ctx, api, info, errors)
    part["added"] = out.get("added") or 0
    for asset_id in _pending(ctx):
        bg.step(ctx, f"新增 {asset_id}")
        try:
            fields, expires_at = _fields(ctx, api, info, asset_info(ctx, asset_id))
            record_id = _create_record(ctx, api, info, fields)
            _mark(
                ctx,
                asset_id,
                record_id=record_id,
                link_expires_at=expires_at,
                synced_at=now_iso(),
                attempts=0,
                last_error=None,
            )
            part["added"] += 1
        except _FAILURES as exc:
            errors.append({"asset_id": asset_id, "error": _fail(ctx, asset_id, exc)})
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
    part["refreshed"] = _refresh_links(ctx, api, info, errors)
    # 套图列：每款最近一次套图的状态、缩略图、压缩包链接
    part["scenes"] = scenes.sync_table(ctx, api, info, errors)
    # 各行的「排序」和「下载确认」：套图状态变了、确认了 / 作废了的行挪位置
    downloads_out = downloads.sync_table(ctx, api, info, errors)
    if downloads_out:
        part["downloads"] = downloads_out
    # 上架几列（店铺、货号、标题、状态、备注、确认上架）
    temu_out = temu.sync_table(ctx, api, info, errors)
    if temu_out:
        part["temu"] = temu_out
    return part


def sync(ctx: Ctx, *, retry: bool = False) -> dict[str, Any]:
    """把选中的款同步进表格（还没建表就先建）；另有同步进程持有租约时直接返回。"""
    from . import books, downloads  # 都依赖本模块，放到用时再导入

    if not enabled(ctx):
        return {"skipped": True, "reason": "table.enabled 为 false"}
    if retry:
        with ctx.db.tx() as conn:
            conn.execute("UPDATE table_rows SET attempts = 0 WHERE attempts > 0")
        info = base(ctx)
        if info and (info.get("listing_attempts") or info.get("views_attempts")):
            info["listing_attempts"] = info["views_attempts"] = 0
            _save_base(ctx, info)
        downloads.reset_attempts(ctx)
        books.reset_attempts(ctx)
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        return {"skipped": True, "reason": "另一个同步进程正在运行"}
    errors: list[dict[str, str]] = []
    try:
        with feishu.Client(ctx) as api:
            out = sync_locked(ctx, api, errors)
    finally:
        lease.drop(ctx, LEASE_KEY)
    return {
        **out,
        "failed": len(errors),
        "errors": errors[:5],
        "url": (base(ctx) or {}).get("url"),
    }


def sync_locked(
    ctx: Ctx, api: feishu.Client, errors: list[dict[str, str]]
) -> dict[str, Any]:
    """持有选款表的同步租约时做一遍「选中的款」的同步（每周选款表迁移时也直接调它，租约不放）。"""
    from . import books, listed_table  # 都依赖本模块，放到用时再导入

    out: dict[str, Any] = {"created": False, "added": 0, "refreshed": 0, "removed": 0}
    info, out["created"] = ensure_base(ctx, api)
    # 候选表按钮行要的记录变更事件：还没订阅（或上次失败已过冷却）就订一次
    if events_pending(ctx):
        out["events"] = subscribe_events(ctx, api, base(ctx) or info)
    # 每周选款表：有款要进的那一天还没建就先建（周表格、文件夹、当天的「选中的款」）
    weekly = books.prepare(ctx, api, errors)
    if weekly:
        out["weekly"] = weekly
    # 旧表格的「选中的款」一份，每周表格每天一份；各份只挑归到自己的款
    with books.scoped(ctx, info):
        out.update(_sync_scope(ctx, api, info, errors, out))
    for day in books.days(ctx):
        if not day.get("table_id"):
            continue
        with books.scoped(ctx, day):
            part = _sync_scope(
                ctx,
                api,
                day,
                errors,
                {"added": 0, "refreshed": 0, "removed": 0},
            )
        for key in ("added", "refreshed", "removed"):
            out[key] += part.get(key) or 0
        out.setdefault("days", {})[day["day"]] = part
    listed_out = listed_table.sync_table(ctx, api, info, errors)
    if listed_out:
        out["listed"] = listed_out
    return out


def delete_rows(ctx: Ctx, asset_ids: list[str]) -> dict[str, Any]:
    """删掉这些素材在选款表里的行（清理旧数据用）：先删飞书表格里的记录，再删 table_rows。

    这些素材没有行时什么都不做；和同步进程共用租约，拿不到就报错（什么都没删）。
    飞书删记录失败的列在 errors 里（table_rows 照样删掉，那一行要到表格里手动删）。
    """
    ids = sorted(set(asset_ids))
    rows: list[dict[str, Any]] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        rows += [
            dict(r)
            for r in ctx.db.q(
                f"SELECT asset_id, record_id FROM table_rows WHERE asset_id IN ({marks})",
                tuple(chunk),
            )
        ]
    if not rows:
        return {"deleted": 0, "errors": []}
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        raise StageError("选款表正在同步：等一两分钟再删")
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    done: set[str] = set()
    errors: list[dict[str, str]] = []
    try:
        info = base(ctx)
        remote = [r for r in rows if r["record_id"]] if info else []
        if remote:
            try:
                from . import cells  # 同上

                with feishu.Client(ctx) as api:
                    for row in remote:
                        # 这一款的行在它那一份「选中的款」里（旧表格，或每周表格的那一天）
                        scope = books.scope_of_asset(ctx, row["asset_id"]) or info
                        try:
                            cells.snapshot_records(
                                ctx,
                                api,
                                scope,
                                scope["table_id"],
                                [row["record_id"]],
                                kind=cells.KIND_SELECTED,
                                reason="清理旧数据，删行",
                                refs={row["record_id"]: {"asset_id": row["asset_id"]}},
                            )
                            api.call(
                                "DELETE", f"{_records_path(scope)}/{row['record_id']}"
                            )
                            done.add(row["asset_id"])
                        except _FAILURES as exc:
                            errors.append(_row_error(row, exc))
            except _FAILURES as exc:
                failed = {e["asset_id"] for e in errors}
                errors += [
                    _row_error(r, exc)
                    for r in remote
                    if r["asset_id"] not in done and r["asset_id"] not in failed
                ]
        with ctx.db.tx() as conn:
            for row in rows:
                conn.execute(
                    "DELETE FROM table_rows WHERE asset_id = ?", (row["asset_id"],)
                )
                conn.execute(
                    "DELETE FROM selected_rows WHERE asset_id = ?", (row["asset_id"],)
                )
    finally:
        lease.drop(ctx, LEASE_KEY)
    return {"deleted": len(done), "errors": errors}


def _row_error(row: dict[str, Any], exc: BaseException) -> dict[str, str]:
    return {
        "asset_id": row["asset_id"],
        "record_id": row["record_id"],
        "error": f"{type(exc).__name__}: {exc}"[:300],
    }


def ensure_base(ctx: Ctx, api: feishu.Client) -> tuple[dict[str, Any], bool]:
    """已建好的多维表格，还没建就建（调用方持有同步租约）；返回 (表格信息, 这次是否新建)。建表失败记下原因再抛出。

    表已经在、又配了知识库时，顺手把还没挪进去的表挪进去；挪失败不挡后面的写行。
    """
    info = base(ctx)
    if info:
        place_in_wiki(ctx, api, info, announce=True)
        return base(ctx) or info, False
    try:
        return _create_base(ctx, api), True
    except _FAILURES as exc:
        with ctx.db.tx() as conn:
            ctx.db.set_meta(
                conn,
                ERROR_META_KEY,
                dumps({"at": now_iso(), "error": f"{type(exc).__name__}: {exc}"[:500]}),
            )
        raise


def create_backoff(ctx: Ctx) -> bool:
    """还没建表、而且上次建表失败不到 CREATE_RETRY_MINUTES：先别再试。"""
    if base(ctx):
        return False
    error = loads(ctx.db.get_meta(ERROR_META_KEY), {}) or {}
    return bool(error) and utcnow() - parse_iso(error["at"]) < dt.timedelta(
        minutes=CREATE_RETRY_MINUTES
    )


def _has_work(ctx: Ctx) -> bool:
    """选款表（选中的款）要同步的：新选中、要删的行、该补或该续的链接、上架状态三列与默认视图、下载确认两列与各行的排序、
    上架几列（开着 Temu 自动上架时）与「已上架的款」。"""
    from . import books, downloads, listed_table, temu  # 都依赖本模块，放到用时再导入

    approved = int(
        ctx.db.scalar(f"SELECT COUNT(*) FROM assets a WHERE {_APPROVED}") or 0
    )
    info = base(ctx)
    if not info:
        return bool(approved) and not create_backoff(ctx)
    # 旧表格的「选中的款」和每周表格各天的「选中的款」
    for scope in (info, *(d for d in books.days(ctx) if d.get("table_id"))):
        if (
            _listing_work(ctx, scope)
            or _views_work(ctx, scope)
            or downloads.has_work(ctx, scope)
            or temu.has_table_work(ctx, scope)
        ):
            return True
    if listed_table.has_work(ctx) or books.has_work(ctx):
        return True
    stale = ctx.db.scalar("SELECT COUNT(*)" + _STALE_SQL, (MAX_ATTEMPTS,))
    refresh = ctx.db.scalar(
        "SELECT COUNT(*)" + _REFRESH_SQL, (MAX_ATTEMPTS, refresh_before(ctx))
    )
    return bool(_pending(ctx) or stale or refresh)


def kick(ctx: Ctx) -> dict[str, Any] | None:
    """选款表、候选表或套图列有要同步的、又没有同步进程在跑时，拉起后台 `podctl table sync`（一起同步）。"""
    from . import candidates, scenes  # 都依赖本模块，放到用时再导入

    # worker 的多条工位会接连调：串行检查，刚拉起过就不再查
    with limits.kick_lock("table"):
        if limits.recently_kicked("table"):
            return None
        if not enabled(ctx) or lease.active(ctx, LEASE_KEY):
            return None
        if not (
            _has_work(ctx)
            or candidates.has_work(ctx)
            or wiki_pending(ctx)
            or events_pending(ctx)
            or scenes.table_has_work(ctx)
        ):
            return None
        if any(t["kind"] == "table" for t in bg.running(ctx)):
            return None
        task = bg.spawn(ctx, ["table", "sync"], kind="table")
        limits.mark_kicked("table")
        return task


def status(ctx: Ctx) -> dict[str, Any]:
    from . import books, downloads, listed_table, temu  # 都依赖本模块，放到用时再导入

    info = base(ctx) or {}
    failed = ctx.db.q(
        "SELECT asset_id, attempts, last_error FROM table_rows WHERE attempts >= ? ORDER BY asset_id LIMIT 10",
        (MAX_ATTEMPTS,),
    )
    return {
        "enabled": enabled(ctx),
        # 给人打开的链接：开着每周选款表时是这一周的周表格（今天的「选中的款」），旧表格的在 legacy_url
        "url": table_url(ctx) or info.get("url"),
        "legacy_url": info.get("url"),
        "created_at": info.get("created_at"),
        "shared_with": info.get("shared_with") or [],
        "rows": int(
            ctx.db.scalar("SELECT COUNT(*) FROM table_rows WHERE record_id IS NOT NULL")
            or 0
        ),
        "pending": len(_pending(ctx)),
        "without_link": int(
            ctx.db.scalar(
                "SELECT COUNT(*) FROM table_rows WHERE record_id IS NOT NULL AND link_expires_at IS NULL"
            )
            or 0
        ),
        "failed": [dict(r) for r in failed],
        "create_error": (loads(ctx.db.get_meta(ERROR_META_KEY), {}) or {}).get("error"),
        "wiki_space_id": info.get("wiki_space_id"),
        "wiki_node_token": info.get("wiki_node_token"),
        "wiki_error": (loads(ctx.db.get_meta(WIKI_ERROR_META_KEY), {}) or {}).get(
            "error"
        ),
        "events": {
            "wanted": events_wanted(ctx),
            "subscribed": events_subscribed(ctx),
            "subscribed_at": events_state(ctx).get("subscribed_at"),
            "error": events_state(ctx).get("error"),
        },
        "listing": {
            "enabled": listing_enabled(ctx),
            "ready": listing_ready(info),
            "backfilled": bool(info.get("listing_backfilled")),
            "error": info.get("listing_error"),
        },
        "views": {
            "ids": info.get("views"),
            "version": info.get("views_version"),
            "error": info.get("views_error"),
        },
        "downloads": downloads.status(ctx),
        "temu": {"ready": temu.ready(info), "enabled": temu.enabled(ctx)},
        "listed": listed_table.status(ctx),
        "link": table_url(ctx),
        "shelf_folder": info.get("shelf_folder"),
        "weekly": books.status(ctx),
    }
