"""候选表：每个任务（参考图）一张飞书多维表格数据表，进选款的图都在里面，人在表里打钩，点「选款完成」验收。

- 候选表和「选中的款」在同一个多维表格里（feishu_table.base），某张参考图第一次有图进选款时建一张
  「S01 森林三伙伴水彩 1020 候选」（还没起名时「S01 候选」）；任务名字有了或改了，同步时表名跟着改。
  表与行记在 candidate_tables / candidate_rows，表名记在 seed_names.table_name。
- 同步（`podctl table sync` 里和选款表一起做）：开着选款待审、还没进表的图新增一行（缩略图、选款结果「待选」）；
  在聊天框里定了的（选中 / 不要 / 修改 / 清出）把勾选框和选款结果改成对应的样子；原图传到 OSS 后补下载链接，快到期时续签。
  还没定的行不碰勾选框，那是人在表里勾的。
- 「选中」是人的输入，「选款结果」是程序写的结论（手改不算），两列在表头都带说明；2026-10-22 前建的表里
  「选款结果」叫「状态」，同步时改名、补说明（`_sync_fields`），没改成之前照旧按「状态」写。
- 「没选原因」（多选，人填，2026-10-26 加）：验收读勾选时一起读，判了不要的图有原因的记进 selection_reasons（reasons.py）。
- 验收（`podctl table accept --seed S01`）：回读这张表的勾选，选款还开着的勾上算选中、没勾算不要（和在聊天框里点一样）；
  没勾的只有验收前就在表里、进选款也满 FRESH_MINUTES 分钟才算不要，其余是人多半还没看到的新图，留到下次；
  聊天框里已经定了的以聊天框为准；还没进表的留到下次。验收完不打包下载：选中的款接着交给 mooya 出套图（scenes.py）。
- 表顶（table.control_row）：两个按钮行「▶ 选中的先出套图」「▶ 选款完成」，人在这一行的「选中」打勾就等于点了按钮；
  下面是各批次还没标已下载的交付包行（勾上 = 标已下载）和选中款的套图行（套图状态、张数、下载链接）。
  多维表格记录变更事件经网关转来（`podctl table control`），巡检再回读一遍兜底；勾上后取消勾选，按种类处理：
  先出套图 / 选款完成拉起后台（选款完成要等确认过的方向全部出完图），交付包行当场标已下载（packages.py）。
  2026-10-26 前建在表最底下的「▶ 选款完成」控制行（candidate_controls）同步时搬成表顶的按钮行，记录不动。
- 预留行（table.reserve_rows）：方向确认后按出图槽位先占好行，选款结果「未生成」；图进选款时在原行补上图（待选），
  方向出完了这个槽位也没图是「没出成」；修改出的新版本另起一行。这两种行没有图，验收和先出套图都不碰。
- 排序：所有默认视图按隐藏的「排序」列升序排（views.py），程序给每一行算好位置（order_*、_layout），
  只写变了的：按钮行 → 待下载的交付包行 → 它们批次的套图行 → 候选行（新批次在前，同一批次按方向、出图槽位）
  → 标了已下载的交付包行连同这个批次的套图行沉到最底。
- 「套图交付包」列（开着交付包时）：每一行（候选行和套图行）写这一行所在批次的交付包链接（packages.py），
  包重打 / 续签 / 删掉时同一批次的行一起改。新建的表放第 2 列；之前建的表补在最后一列（接口改不了表里的列序），
  默认视图里它和另两个下载链接一起摆在「选中」后面（LAYOUT）。
- 默认视图与表名前缀（table.views，views.py，视图版本 6）：第一个视图是表格「全部」（所有行，按钮行、交付包行、
  套图行和预留行也在，开头是素材、缩略图、「选中」和三个下载链接，其余列照原来的先后），接着是画册「全部画册」（有图的
  原图行，卡片上多一个选款结果）、「已选中」（选中的原图，卡片上是「选中」或店铺勾、货号、改了什么、类型、出图方式）和
  「待选」（还没选过的图）；程序给的链接打开「待选」；
  表名前面写选款进度：没前缀 = 还没选过，「【已选N款】」= 选了 N 款、还没点选款完成，「【选款完成】」= 点过选款完成，
  「【有新图】」= 点过选款完成后又进了新图。
- 归档（shelf.py）：任务归档后这张表挪进「已归档」文件夹，调出后挪回根目录（_sync_shelf，只在状态变化时挪一次）。
- 和选款表同步共用租约，同一时刻只有一个进程写表格。
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import time
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import (
    bg,
    candidate_stores,
    downloads,
    feishu,
    feishu_table,
    images,
    lease,
    listing,
    packages,
    reasons,
    review,
    shelf,
    store,
    table_registry,
    views,
)
from .context import Ctx
from .oss import OSSError
from .db import log_event
from .stages.common import MODE_LABELS, StageError, model_label, seed_name, slot_mode
from .stages.plan import (
    asset_label,
    asset_label_of,
    brief_line,
    direction_summary,
    direction_tag,
)
from .util import dumps, iso, loads, now_iso, parse_iso, utcnow

STATE_PENDING = "待选"
STATE_APPROVED = "已选中"
STATE_REJECTED = "没选中"
STATE_CHANGED = "已修改"
STATE_DISMISSED = "已清出"
# 预留行（2026-10-26）：方向确认后先按出图槽位占好行，图还没出来是「未生成」，任务结束也没出图是「没出成」
STATE_UNMADE = "未生成"
STATE_NO_IMAGE = "没出成"
STATES = (
    STATE_PENDING,
    STATE_APPROVED,
    STATE_REJECTED,
    STATE_CHANGED,
    STATE_DISMISSED,
    STATE_UNMADE,
    STATE_NO_IMAGE,
)
# 选款待审的人工决定 → 行的状态
_DECIDED = {
    "approved": STATE_APPROVED,
    "rejected": STATE_REJECTED,
    "needs_changes": STATE_CHANGED,
}
TABLE_SUFFIX = " 候选"
ACCEPT_NOTE = "候选表验收"
# 列记录、批量更新每次最多多少条（飞书接口上限 500）
_PAGE = 500
# 验收时等正在跑的同步放开租约，最多等这么久（秒）
LEASE_WAIT_SECONDS = 180
# 进选款不到这么久（分钟）、又没勾的图，验收时不算不要，留到下次
FRESH_MINUTES = 10

# 写表、读表都按列名：勾选框「选中」是人的输入，「选款结果」是程序写的结论
CHECK_FIELD = "选中"
STATE_FIELD = "选款结果"
OLD_STATE_FIELD = "状态"
CHECK_HELP = (
    "要的打勾。看完这一批在最上面「▶ 选款完成」那一行打勾（或在任务列表点「选款完成」）：勾上的算选中，出了图没勾的算没选中"
    f"（进选款不到 {FRESH_MINUTES} 分钟、或这次才进表的，没勾就留到下次）。已经定了的行再改勾不算。"
)
STATE_HELP = (
    "程序自动填，手改不算。待选 = 还没定；未生成 = 方向确认了、图还没出来；没出成 = 这一张最后没出图。"
    "勾店铺 5 分钟后、点「选款完成」或在聊天卡片里定了以后才会变。选款只看勾选框。"
)
# 没选原因（reasons.py）：人填的多选列，程序只读
REASON_FIELD = "没选原因"
REASON_HELP = (
    "选填，可多选：没选的图为什么不要。点「选款完成」时程序读回来，给批次反馈和统计用（之后补填的下次验收读到）；"
    "勾了「选中」的行不用填。"
)
# 列名和说明已经跟上的候选表：{"done": [table_id], "versions": {table_id: 版本}, "attempts": {table_id: 失败次数},
# "error": 最近的原因}。版本 2（2026-10-23）：「选中」说明加了控制行，补上套图三列；版本 3（2026-10-26）：补上「没选原因」；
# 版本 4（2026-10-26）：补上「排序」列，「选款结果」补「未生成」「没出成」，两列说明改成按钮在最上面
FIELDS_META_KEY = "candidates.fields"
FIELDS_VERSION = 4
DATA_NOT_CHANGE = 1254606
# 「选中」「选款结果」和店铺列的说明（candidates.help 记每张表改到哪一版）：2 = 2026-10-30 去掉「选中的先出套图」、
# 勾店铺 5 分钟后自动走、「选款完成」随时能点
HELP_META_KEY = "candidates.help"
HELP_VERSION = 2
HELP_TABLES_PER_SYNC = 10

# 按钮行（表最上面）：在它的「选中」打勾 = 点了这个按钮。2026-10-30 起只留「选款完成」（随时能点，是标记）；
# 「选中的先出套图」不再建（勾店铺 5 分钟后自动走），`podctl table dispatch` 留给运维
DISPATCH_TEXT = "▶ 选中的先出套图：在这一行的「选中」打勾，勾上的款先出套图"
DISPATCH_HELP = "勾上的、已经出了图的款定为选中，交给 mooya 出套图；没勾的不动，不算选款完成，出图过程中可以点很多次"
CONTROL_TEXT = "▶ 选款完成：看完这一批后在这一行的「选中」打勾，标记选款完成"
CONTROL_HELP = (
    "随时能点：勾上的算选中（接着出套图、上架），出了图没勾的算没选中"
    f"（进选款不到 {FRESH_MINUTES} 分钟、或这次才进表的留到下次）；表名改成【选款完成】，"
    "选中的款都上架后自动归档；之后又出了新图表名是【有新图】"
)
PACKAGE_ROW_TEXT = "▶ 交付包 {batch}（{items} 款）：下载后在这一行的「选中」打勾"
PACKAGE_ROW_WAIT = "交付包 {batch}：套图还在出，出完自动打包"
PACKAGE_ROW_DONE = "交付包 {batch}（{items} 款）已下载"
PACKAGE_ROW_HELP = (
    "下载好交付包后在这一行的「选中」打勾，标成已下载：这一行连同这个批次的套图沉到表底，"
    "每个批次都标完、套图都出完的任务才会自动归档；标错了在 POD 对话里撤销"
)
DISPATCH_NOTE = "候选表提前出套图"
# 按钮行的种类（candidate_actions.kind，也是后台任务的种类）：交付包行按批次一行，其余 batch_id 是空串
KIND_DISPATCH = "dispatch"
KIND_ACCEPT = "accept"
KIND_PACKAGE = "package"
ACTION_LABELS = {KIND_DISPATCH: "提前出套图", KIND_ACCEPT: "验收"}
# 表顶的行、各行的「排序」写不成的候选表：{"attempts": {table_id: 失败次数}, "error": 最近的原因}
ACTIONS_META_KEY = "candidates.actions"
ORDERS_META_KEY = "candidates.orders"
# 预留行（candidate_slots.state）：还没出图 / 方向出完了也没图 / 图已经补上
SLOT_UNMADE = "unmade"
SLOT_NO_IMAGE = "no_image"
SLOT_FILLED = "filled"
# 出完了的出图任务（和 tasks.JOB_DONE 一样）
JOB_DONE = ("complete", "failed")

# 「排序」列：所有默认视图按它升序排（views.py），程序给每一行算好位置——按钮行最上，接着是还没标已下载的交付包
# 和它们批次的套图行，然后是候选行（新批次在前，同一批次按方向、出图槽位），标了已下载的交付包连同套图行沉到最底。
# 数值压在 21 亿以内（Postgres INTEGER）
ORDER_FIELD = "排序"
ORDER_HELP = "程序自动填，表按这一列排：按钮在最上面，下载过的交付包沉到最底下。不用改"
ORDER_DISPATCH = 10
ORDER_ACCEPT = 20
ORDER_PACKAGE = 100
ORDER_SCENE = 100_000
ORDER_CANDIDATE = 10_000_000
ORDER_DONE = 1_000_000_000
SCENE_ROW_PREFIX = "套图 · "
SCENE_STATE_FIELD = "套图状态"
SCENE_COUNT_FIELD = "场景图张数"
SCENE_LINK_FIELD = "下载套图"
NOTE_FIELD = "备注"
# 回读按钮行拿不到修改人时（巡检兜底、事件里没有操作人）记在确认人上的名字
CONTROL_BY = "feishu:候选表"
PACKAGE_FIELD = "套图交付包"
PACKAGE_HELP = (
    "程序自动填，手改不算。这一行所在批次的交付包：选中的款出完套图后打的包，"
    "每款一个文件夹（设计图 + 按 mooya 交付包编号的场景图），同一批次的每一行都是同一个链接；"
    "之后有新的款出完会重打，链接跟着换。"
)
# 已经有「套图交付包」列的候选表：{"done": [table_id], "attempts": {table_id: 失败次数}, "error": 最近的原因}
PACKAGE_META_KEY = "candidates.package_field"
# 已经建好默认视图的候选表：{"done": {table_id: {all, all_gallery, picked, pending}}, "versions": {table_id: 视图版本},
# "attempts": {table_id: 失败次数}, "error": …}（视图版本 5 没有 all_gallery，之前 done 里是 {pending, all, gallery}）
VIEWS_META_KEY = "candidates.views"
# 版本 2（2026-10-26）：「待选」也看未生成的预留行，「看图」只看待选，三个视图都按「排序」排；
# 版本 3（2026-10-27）：表格视图的下载列挪到前面、「看图」卡片字段定下来（LAYOUT）；
# 版本 4（同日）：版本 3 把可见列接口给的列名当成字段 id，表格视图没摆上（画册摆好了），再摆一次；
# 版本 5（2026-10-30 用户定）：表格「全部」+ 画册「已选中」+ 画册「待选」（views.ensure_candidate_views 就地改）；
# 版本 6（同日）：「全部」后面加画册「全部画册」（有图的原图行，不放套图行），已有的画册依次改名挪位置
VIEWS_VERSION = 6
# 画册「全部画册」看的选款结果：有图的原图行（不放「未生成」「没出成」的空行；套图行、表顶几行的选款结果是空的）
ALL_GALLERY_OPTIONS = [
    STATE_PENDING,
    STATE_APPROVED,
    STATE_REJECTED,
    STATE_CHANGED,
    STATE_DISMISSED,
]
# 表名前面的选款状态（table.views 开着时）：没前缀 = 还没选过；【已选N款】= 选了 N 款、还没点「选款完成」；
# 【选款完成】= 点过「选款完成」、没有待选；【有新图】= 点过「选款完成」之后又进了新图
MARK_DONE = "【选款完成】"
MARK_NEW = "【有新图】"


def mark_picked(count: int) -> str:
    return f"【已选{count}款】"


def _help(text: str) -> dict[str, Any]:
    return {"disable_sync": True, "text": text}


REASON_SPEC: dict[str, Any] = {
    "field_name": REASON_FIELD,
    "type": 4,
    "property": {"options": [{"name": name} for name in reasons.REASONS]},
    "description": _help(REASON_HELP),
}
# 多维表格字段类型：1 文本、3 单选、4 多选、5 日期、7 勾选框、15 超链接、17 附件；第一列是主键
FIELDS: list[dict[str, Any]] = [
    {"field_name": "素材", "type": 1},
    {"field_name": "缩略图", "type": 17},
    {"field_name": CHECK_FIELD, "type": 7, "description": _help(CHECK_HELP)},
    {
        "field_name": STATE_FIELD,
        "type": 3,
        "property": {"options": [{"name": name} for name in STATES]},
        "description": _help(STATE_HELP),
    },
    REASON_SPEC,
    {"field_name": "下载原图", "type": 15},
    {"field_name": "方向", "type": 1},
    {"field_name": "改了什么", "type": 1},
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
    {"field_name": "链接到期", "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}},
    {"field_name": NOTE_FIELD, "type": 1},
]
# 套图行用的三列（2026-10-23 加）：新表建表时就有，已有的表在同步时补上（_sync_fields）；整组场景图只在选款表里放
SCENE_FIELDS: list[dict[str, Any]] = [
    copy.deepcopy(
        next(
            f for f in feishu_table.SCENE_FIELDS if f["field_name"] == SCENE_STATE_FIELD
        )
    ),
    {"field_name": SCENE_COUNT_FIELD, "type": 2, "property": {"formatter": "0"}},
    {"field_name": SCENE_LINK_FIELD, "type": 15},
]
FIELDS += SCENE_FIELDS
ORDER_SPEC: dict[str, Any] = {
    "field_name": ORDER_FIELD,
    "type": 2,
    "property": {"formatter": "0"},
    "description": _help(ORDER_HELP),
}
FIELDS.append(ORDER_SPEC)
PACKAGE_SPEC: dict[str, Any] = {
    "field_name": PACKAGE_FIELD,
    "type": 15,
    "description": _help(PACKAGE_HELP),
}
# 默认视图里列的先后（views.Layout）：表格「全部」开头是素材、缩略图、「选中」和三个下载链接，其余列照原来的先后、
# 「排序」藏起来；画册「已选中」「待选」卡片上是「选中」、改了什么、类型、出图方式，「全部画册」多一个选款结果（卡片标题
# 是素材，封面是缩略图；有店铺列时「选中」换成店铺列，「已选中」「全部画册」再加货号，见 candidate_stores.layout）
THUMB_FIELD = "缩略图"
GRID_FRONT = (
    "素材",
    THUMB_FIELD,
    CHECK_FIELD,
    "下载原图",
    SCENE_LINK_FIELD,
    PACKAGE_FIELD,
)
GALLERY_FIELDS = ("素材", CHECK_FIELD, "改了什么", "类型", "出图方式")
WHOLE_FIELDS = ("素材", CHECK_FIELD, STATE_FIELD, "改了什么", "类型", "出图方式")
LAYOUT = views.Layout(
    front=GRID_FRONT,
    known=frozenset(
        {*(f["field_name"] for f in FIELDS), PACKAGE_FIELD, OLD_STATE_FIELD}
    ),
    hidden=(ORDER_FIELD,),
    gallery=GALLERY_FIELDS,
    cover=THUMB_FIELD,
    whole=WHOLE_FIELDS,
)

_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)
# 清理时删 OSS 上 2026-10-20 前验收留下的旧压缩包可能遇到的错误（删不掉不影响清理）
_OSS_FAILURES = (OSSError, store.StorageError, httpx.HTTPError)


# ---------------------------------------------------------------- 查询


def table_info(ctx: Ctx, seed_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT * FROM candidate_tables WHERE seed_id = ?", (seed_id,))
    return dict(row) if row else None


def table_name(seed_id: str, name: str | None, mark: str = "") -> str:
    """候选表的表名：S01 森林三伙伴水彩 1020 候选；还没起名时 S01 候选；mark 是前面的选款状态（【选款完成】…）。"""
    core = f"{seed_id} {name}{TABLE_SUFFIX}" if name else f"{seed_id}{TABLE_SUFFIX}"
    return f"{mark}{core}"


def name_marks(ctx: Ctx) -> dict[str, str]:
    """各张候选表表名前面该写的选款状态（table.views 关着时为空）：点过「选款完成」、没有待选 →【选款完成】，
    点过之后又有待选 →【有新图】；没点过、已经选中了 N 款 →【已选N款】；其余没有前缀。
    表名跟着任务名字改（seed_names 记着现在的表名），还没起名的任务不加前缀，起名后连名字一起改。"""
    if not views.enabled(ctx):
        return {}
    accepted = {
        r["seed_id"]: r["accepted_at"]
        for r in ctx.db.q("SELECT seed_id, accepted_at FROM candidate_tables")
    }
    pending = {
        r[0]: r[1]
        for r in ctx.db.q(
            "SELECT f.seed_id, COUNT(*) FROM reviews r JOIN assets a ON a.asset_id = r.subject_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " WHERE r.subject_type = 'asset_selection' AND r.status = 'open' GROUP BY f.seed_id"
        )
    }
    approved = {
        r[0]: r[1]
        for r in ctx.db.q(
            "SELECT f.seed_id, COUNT(DISTINCT a.asset_id) FROM assets a"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " WHERE a.human_decision = 'approved' GROUP BY f.seed_id"
        )
    }
    out = {}
    for seed_id, accepted_at in accepted.items():
        if accepted_at:
            out[seed_id] = MARK_NEW if pending.get(seed_id) else MARK_DONE
        elif approved.get(seed_id):
            out[seed_id] = mark_picked(approved[seed_id])
        else:
            out[seed_id] = ""
    return out


def _renames(ctx: Ctx) -> list[dict[str, str]]:
    """有了名字（或选款状态变了）、候选表表名还没跟上的（改名失败满上限的不再试）：[{seed_id, table_id, name}]。"""
    marks = name_marks(ctx)
    out = []
    for r in ctx.db.q(
        "SELECT n.seed_id, n.name, n.table_name, t.table_id FROM seed_names n"
        " JOIN candidate_tables t ON t.seed_id = n.seed_id WHERE n.table_attempts < ?"
        " ORDER BY n.seed_id",
        (feishu_table.MAX_ATTEMPTS,),
    ):
        want = table_name(r["seed_id"], r["name"], marks.get(r["seed_id"], ""))
        if r["table_name"] != want:
            out.append(
                {"seed_id": r["seed_id"], "table_id": r["table_id"], "name": want}
            )
    return out


def _views_meta(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(VIEWS_META_KEY), None) or {}


def _save_views_meta(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, VIEWS_META_KEY, dumps(meta))


def _views_done(
    ctx: Ctx, table_id: str, ids: dict[str, str], version: int = VIEWS_VERSION
) -> None:
    meta = _views_meta(ctx)
    meta.setdefault("done", {})[table_id] = ids
    meta.setdefault("versions", {})[table_id] = version
    # 视图版本 5 起「看图」的筛选不再跟着选款进度改：之前记的清掉
    for key in ("gallery", "gallery_attempts"):
        (meta.get(key) or {}).pop(table_id, None)
    attempts = meta.get("attempts") or {}
    attempts.pop(table_id, None)
    meta["attempts"] = attempts
    if not attempts:
        meta.pop("error", None)
    _save_views_meta(ctx, meta)


def view_ids(ctx: Ctx, table_id: str) -> dict[str, str]:
    """这张候选表的默认视图 id（视图版本 6：all / all_gallery / picked / pending；版本 5 没有 all_gallery，
    之前那一套是 pending / all / gallery）；还没建为空。"""
    return (_views_meta(ctx).get("done") or {}).get(table_id) or {}


def _views_version(meta: dict[str, Any], table_id: str) -> int:
    """这张表的默认视图按哪一版建的（2026-10-26 前建的记作版本 1：还没按「排序」排）；还没建为 0。"""
    if table_id not in (meta.get("done") or {}):
        return 0
    return int((meta.get("versions") or {}).get(table_id, 1))


def _views_current(meta: dict[str, Any], table_id: str) -> bool:
    """默认视图已按当前这套建好。"""
    return _views_version(meta, table_id) >= VIEWS_VERSION


def _views_pending(ctx: Ctx) -> list[dict[str, str]]:
    """开着默认视图时，默认视图还没建或没跟上当前这套的候选表（失败满上限的不再试；要等列先跟上：
    筛选要用「未生成」选项、排序要用「排序」列）：[{seed_id, table_id}]。"""
    if not views.enabled(ctx):
        return []
    meta = _views_meta(ctx)
    fields = _fields_meta(ctx)
    attempts = meta.get("attempts") or {}
    return [
        {"seed_id": r["seed_id"], "table_id": r["table_id"]}
        for r in ctx.db.q(
            "SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id"
        )
        if (
            not _views_current(meta, r["table_id"])
            or candidate_stores.layout_pending(ctx, r["table_id"])
        )
        and _fields_current(fields, r["table_id"])
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    ]


def table_link(ctx: Ctx, seed_id: str) -> str | None:
    """这个任务候选表的链接：建好了默认视图时直接打开「待选」（视图版本 5 起是画册）。"""
    info = table_info(ctx, seed_id)
    if not info or not info.get("url"):
        return None
    pending = view_ids(ctx, info["table_id"]).get("pending")
    if pending and views.enabled(ctx):
        return views.link(info["url"], view_id=pending)
    return info["url"]


def seed_app(ctx: Ctx, info: dict[str, Any], seed_id: str | None) -> str:
    """这个任务的候选表在哪本表格（传缩略图的 parent_node 要用它）；还没有候选表时用 info 的。"""
    table = table_info(ctx, seed_id) if seed_id else None
    return table_registry.app_for(info, (table or {}).get("table_id"))


def _records_path(info: dict[str, Any], table_id: str) -> str:
    return f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/records"


def _fields_meta(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(FIELDS_META_KEY), None) or {}


def _save_fields_meta(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, FIELDS_META_KEY, dumps(meta))


def _fields_done(ctx: Ctx, table_id: str, version: int = FIELDS_VERSION) -> None:
    meta = _fields_meta(ctx)
    meta["done"] = sorted({*(meta.get("done") or []), table_id})
    meta.setdefault("versions", {})[table_id] = version
    attempts = meta.get("attempts") or {}
    attempts.pop(table_id, None)
    meta["attempts"] = attempts
    if not attempts:
        meta.pop("error", None)
    _save_fields_meta(ctx, meta)


def state_field(ctx: Ctx, table_id: str) -> str:
    """这张候选表里程序写结论的那一列：列名还没跟上的旧表照旧叫「状态」。"""
    done = _fields_meta(ctx).get("done") or []
    return STATE_FIELD if table_id in done else OLD_STATE_FIELD


def _fields_current(meta: dict[str, Any], table_id: str) -> bool:
    """列名、说明、套图三列都已跟上当前版本（2026-10-23 前改过列名的表记作版本 1）。"""
    if table_id not in set(meta.get("done") or []):
        return False
    return int((meta.get("versions") or {}).get(table_id, 1)) >= FIELDS_VERSION


def _stale_fields(ctx: Ctx) -> list[dict[str, str]]:
    """列名、说明、套图三列还没跟上的候选表（失败满上限的不再试）：[{seed_id, table_id}]。"""
    meta = _fields_meta(ctx)
    attempts = meta.get("attempts") or {}
    return [
        {"seed_id": r["seed_id"], "table_id": r["table_id"]}
        for r in ctx.db.q(
            "SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id"
        )
        if not _fields_current(meta, r["table_id"])
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    ]


def table_fields(ctx: Ctx) -> list[dict[str, Any]]:
    """新建候选表的列：开着交付包时「套图交付包」放在第 2 列（素材后面）；开着店铺列时店铺列和「货号」跟在「选中」后面。"""
    fields = list(FIELDS)
    extra = candidate_stores.specs(ctx)
    if extra:
        at = next(i for i, f in enumerate(fields) if f["field_name"] == CHECK_FIELD) + 1
        fields[at:at] = extra
    if not packages.enabled(ctx):
        return fields
    return [fields[0], PACKAGE_SPEC, *fields[1:]]


def _layout_for(ctx: Ctx, table_id: str) -> views.Layout:
    """这张候选表默认视图的列先后（有店铺列时店铺列在前、「选中」藏起来）。"""
    return candidate_stores.layout(ctx, table_id, LAYOUT, check_field=CHECK_FIELD)


def _package_meta(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(PACKAGE_META_KEY), None) or {}


def _save_package_meta(ctx: Ctx, meta: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, PACKAGE_META_KEY, dumps(meta))


def _package_field_done(ctx: Ctx, table_id: str) -> None:
    meta = _package_meta(ctx)
    meta["done"] = sorted({*(meta.get("done") or []), table_id})
    attempts = meta.get("attempts") or {}
    attempts.pop(table_id, None)
    meta["attempts"] = attempts
    if not attempts:
        meta.pop("error", None)
    _save_package_meta(ctx, meta)


def package_field_ready(ctx: Ctx, table_id: str) -> bool:
    """这张候选表已经有「套图交付包」列（新建时带着，或之后补上了）。"""
    return table_id in (_package_meta(ctx).get("done") or [])


def _package_columns_pending(ctx: Ctx) -> list[dict[str, str]]:
    """开着交付包时，还没有「套图交付包」列的候选表（失败满上限的不再试）：[{seed_id, table_id}]。"""
    if not packages.enabled(ctx):
        return []
    meta = _package_meta(ctx)
    done = set(meta.get("done") or [])
    attempts = meta.get("attempts") or {}
    return [
        {"seed_id": r["seed_id"], "table_id": r["table_id"]}
        for r in ctx.db.q(
            "SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id"
        )
        if r["table_id"] not in done
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    ]


def _package_seeds(ctx: Ctx) -> set[str]:
    """候选表已经有「套图交付包」列的任务。"""
    if not packages.enabled(ctx):
        return set()
    done = set(_package_meta(ctx).get("done") or [])
    return {
        r["seed_id"]
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables")
        if r["table_id"] in done
    }


def _packages_pending(ctx: Ctx) -> list[dict[str, Any]]:
    """候选行里该改「套图交付包」那一格的交付包（只算已经有这一列的表）。"""
    return packages.table_pending(ctx, _package_seeds(ctx))


def _pending(ctx: Ctx) -> list[dict[str, Any]]:
    """该进表的图：选款待审开着、还没有行、失败没到上限；按批次、方向、素材排。"""
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT a.asset_id, f.seed_id FROM reviews r"
            " JOIN assets a ON a.asset_id = r.subject_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " LEFT JOIN candidate_rows c ON c.asset_id = a.asset_id"
            " WHERE r.subject_type = 'asset_selection' AND r.status = 'open'"
            " AND c.record_id IS NULL AND COALESCE(c.attempts, 0) < ?"
            " ORDER BY v.batch_id, a.variant_id, a.asset_id",
            (feishu_table.MAX_ATTEMPTS,),
        )
    ]


# 还标着「待选」、选款待审却已经没有开着的：聊天框里定了、或清出了选款
_DECIDED_SQL = (
    f" FROM candidate_rows c WHERE c.record_id IS NOT NULL AND c.state = '{STATE_PENDING}'"
    " AND c.attempts < ? AND NOT EXISTS (SELECT 1 FROM reviews r"
    " WHERE r.subject_type = 'asset_selection' AND r.subject_id = c.asset_id AND r.status = 'open')"
)

# 该补链接（原图已上 OSS）或续签（快到期）的行；没选中、改过、清出的行不再续
_LINK_SQL = (
    " FROM candidate_rows c JOIN assets a ON a.asset_id = c.asset_id"
    " WHERE c.record_id IS NOT NULL AND c.attempts < ?"
    f" AND c.state IN ('{STATE_PENDING}', '{STATE_APPROVED}')"
    " AND ((c.link_expires_at IS NULL AND EXISTS ("
    "SELECT 1 FROM storage_objects o WHERE o.path = a.path AND o.status = 'uploaded'))"
    " OR c.link_expires_at < ?)"
)


def has_work(ctx: Ctx) -> bool:
    """候选表要同步的：新进选款的图、聊天框里定了的行、该补或该续的链接、跟着任务名字（和选款状态）改表名、
    旧表补列 / 改列、补「套图交付包」列、交付包链接变了的批次、默认视图、归档后挪表、预留行、表顶的行、各行的「排序」、
    还没建或没跟上当前版本的模板表。"""
    from . import table_template  # 模板依赖本模块，放到用时再导入

    if not feishu_table.enabled(ctx) or feishu_table.create_backoff(ctx):
        return False
    if (
        table_template.has_work(ctx)
        or _pending(ctx)
        or _stale_fields(ctx)
        or _package_columns_pending(ctx)
        or _packages_pending(ctx)
        or _views_pending(ctx)
        or shelf.pending_moves(ctx)
        or candidate_stores.has_work(ctx)
    ):
        return True
    decided = ctx.db.scalar(
        "SELECT COUNT(*)" + _DECIDED_SQL, (feishu_table.MAX_ATTEMPTS,)
    )
    links = ctx.db.scalar(
        "SELECT COUNT(*)" + _LINK_SQL,
        (feishu_table.MAX_ATTEMPTS, feishu_table.refresh_before(ctx)),
    )
    if decided or links or _renames(ctx) or _slot_work(ctx):
        return True
    plan = _top_plan(ctx)
    return _top_work(ctx, plan) or bool(_orders_pending(ctx, plan))


def _latest_selection(
    ctx: Ctx, asset_ids: list[str]
) -> dict[str, tuple[str, str | None]]:
    """每张图最近一条选款待审的 (status, decision)。"""
    out: dict[str, tuple[str, str | None, str, str]] = {}
    for i in range(0, len(asset_ids), _PAGE):
        chunk = asset_ids[i : i + _PAGE]
        marks = ",".join("?" for _ in chunk)
        for r in ctx.db.q(
            "SELECT subject_id, status, decision, created_at, review_id FROM reviews"
            f" WHERE subject_type = 'asset_selection' AND subject_id IN ({marks})",
            tuple(chunk),
        ):
            key = (r["created_at"], r["review_id"])
            seen = out.get(r["subject_id"])
            if seen is None or key > (seen[2], seen[3]):
                out[r["subject_id"]] = (
                    r["status"],
                    r["decision"],
                    r["created_at"],
                    r["review_id"],
                )
    return {k: (v[0], v[1]) for k, v in out.items()}


def state_of(status: str | None, decision: str | None) -> str:
    """选款待审的状态与决定 → 候选表上的状态。"""
    if status == "open":
        return STATE_PENDING
    if status == "decided":
        return _DECIDED.get(decision or "", STATE_REJECTED)
    return STATE_DISMISSED


# ---------------------------------------------------------------- 排序


def _digits(ident: str | None) -> int:
    digits = "".join(ch for ch in str(ident or "") if ch.isdigit())
    return int(digits) if digits else 0


def _rank(batch_id: str | None) -> int:
    """批次在表里的先后：新批次排前面。"""
    return max(0, 10_000 - _digits(batch_id))


def order_candidate(
    batch_id: str | None, direction: int, slot: int, version: int = 1
) -> int:
    """候选行（和还没出图的预留行）：新批次在前，同一批次按方向、出图槽位，修改出的新版本排在原图后面。"""
    return (
        ORDER_CANDIDATE
        + _rank(batch_id) * 10_000
        + min(direction, 99) * 100
        + min(slot, 9) * 10
        + min(max(version - 1, 0), 9)
    )


def order_scene(batch_id: str | None, index: int, *, done: bool) -> int:
    """套图行：跟在自己批次的交付包后面；这个批次的交付包标了已下载就一起沉到最底。"""
    if done:
        return ORDER_DONE + _rank(batch_id) * 100_000 + 1 + min(index, 99_998)
    return ORDER_SCENE + _rank(batch_id) * 100 + min(index, 99)


def order_action(kind: str, batch_id: str = "", *, done: bool = False) -> int:
    """按钮行在最上面；交付包行在按钮下面（新批次在前），标了已下载沉到最底。"""
    if kind == KIND_DISPATCH:
        return ORDER_DISPATCH
    if kind == KIND_ACCEPT:
        return ORDER_ACCEPT
    if done:
        return ORDER_DONE + _rank(batch_id) * 100_000
    return ORDER_PACKAGE + _rank(batch_id)


def _order_ready(ctx: Ctx, table_id: str) -> bool:
    """这张候选表要写「排序」：开着默认视图（视图按它排），也已经有这一列（新建时带着，或字段版本 4 起补上了）。"""
    return views.enabled(ctx) and _fields_current(_fields_meta(ctx), table_id)


def _remember_orders(ctx: Ctx, seed_id: str, values: dict[str, int]) -> None:
    """记下这几行在飞书里的「排序」，之后只在要改时才写。"""
    if not values:
        return
    now = now_iso()
    with ctx.db.tx() as conn:
        for record_id, value in values.items():
            conn.execute(
                "INSERT INTO candidate_orders(record_id, seed_id, order_value, updated_at)"
                " VALUES(?, ?, ?, ?) ON CONFLICT(record_id) DO UPDATE SET"
                " seed_id = excluded.seed_id, order_value = excluded.order_value,"
                " updated_at = excluded.updated_at",
                (record_id, seed_id, int(value), now),
            )


def _forget_orders(ctx: Ctx, record_ids: list[str]) -> None:
    ids = [r for r in record_ids if r]
    with ctx.db.tx() as conn:
        for i in range(0, len(ids), _PAGE):
            chunk = ids[i : i + _PAGE]
            conn.execute(
                f"DELETE FROM candidate_orders WHERE record_id IN ({','.join('?' for _ in chunk)})",
                tuple(chunk),
            )


def _positions(ctx: Ctx) -> tuple[dict[str, tuple[str, int]], dict[str, list[str]]]:
    """每个出图任务在批次里排第几个方向、它的出图槽位顺序：({job_id: (batch_id, 方向序号)}, {job_id: [slot_id…]})。"""
    by_batch: dict[str, list[tuple[int, str]]] = {}
    for r in ctx.db.q("SELECT job_id, variant_id, batch_id FROM jobs"):
        by_batch.setdefault(r["batch_id"], []).append(
            (_digits(r["variant_id"]), r["job_id"])
        )
    where: dict[str, tuple[str, int]] = {}
    for batch_id, items in by_batch.items():
        for index, (_, job_id) in enumerate(sorted(items)):
            where[job_id] = (batch_id, index)
    slots: dict[str, list[str]] = {}
    for r in ctx.db.q("SELECT job_id, slot_id FROM job_slots"):
        slots.setdefault(r["job_id"], []).append(r["slot_id"])
    return where, {k: sorted(v) for k, v in slots.items()}


def _slot_order(
    where: dict[str, tuple[str, int]],
    slots: dict[str, list[str]],
    job_id: str,
    slot_id: str,
    version: int = 1,
) -> int | None:
    found = where.get(job_id)
    if not found:
        return None
    ids = slots.get(job_id) or []
    slot = ids.index(slot_id) if slot_id in ids else len(ids)
    return order_candidate(found[0], found[1], slot, version)


def _asset_orders(ctx: Ctx, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """这些图在候选表里的位置和出图槽位：{asset_id: {job_id, slot_id, version, order}}。"""
    if not asset_ids:
        return {}
    where, slots = _positions(ctx)
    out: dict[str, dict[str, Any]] = {}
    ids = sorted(set(asset_ids))
    for i in range(0, len(ids), _PAGE):
        chunk = ids[i : i + _PAGE]
        for r in ctx.db.q(
            "SELECT asset_id, job_id, slot_id, asset_version FROM assets"
            f" WHERE asset_id IN ({','.join('?' for _ in chunk)})",
            tuple(chunk),
        ):
            version = int(r["asset_version"] or 1)
            out[r["asset_id"]] = {
                "job_id": r["job_id"],
                "slot_id": r["slot_id"],
                "version": version,
                "order": _slot_order(where, slots, r["job_id"], r["slot_id"], version),
            }
    return out


# ---------------------------------------------------------------- 写表


def _mark(ctx: Ctx, asset_id: str, seed_id: str, **values: Any) -> None:
    """写一行同步记录（没有就新增）。"""
    values = {"seed_id": seed_id, "updated_at": now_iso(), **values}
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO candidate_rows(asset_id, {cols}) VALUES(?, {marks})"
            f" ON CONFLICT(asset_id) DO UPDATE SET {sets}",
            (asset_id, *values.values()),
        )


def _fail(ctx: Ctx, asset_id: str, seed_id: str, exc: BaseException) -> str:
    reason = f"{type(exc).__name__}: {exc}"[:500]
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO candidate_rows(asset_id, seed_id, attempts, last_error, updated_at)"
            " VALUES(?, ?, 1, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET"
            " attempts = candidate_rows.attempts + 1, last_error = excluded.last_error,"
            " updated_at = excluded.updated_at",
            (asset_id, seed_id, reason, now_iso()),
        )
    return reason


def default_views(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], table_id: str
) -> dict[str, str] | None:
    """新表（新建的或从模板复制的）的默认视图：开着 table.views 时第一个视图是表格「全部」，接着是画册「全部画册」
    「已选中」「待选」，列和画册卡片按这张表的 layout 摆（有店铺列时店铺列在前、「选中」藏起来），返回四个视图的 id
    （没建成为 None，交给下一次同步的 _sync_views）；关着时补一个画册视图「看图」。从模板复制来的表视图都在，
    这里只是再写一遍筛选、排序和列（不会冲掉封面效果）。模板表升级视图也走这里（之前那一套就地改）。"""
    if views.enabled(ctx):
        try:
            ids = ensure_views(api, info, table_id, _layout_for(ctx, table_id))
        except _FAILURES:
            return None
        if candidate_stores.present(ctx, table_id):
            candidate_stores.layout_done(ctx, table_id)
        return ids
    try:
        api.call(
            "POST",
            f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/views",
            json={"view_name": "看图", "view_type": "gallery"},
        )
    except feishu.FeishuError:
        pass
    return None


def ensure_views(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    layout: views.Layout,
    *,
    resort: bool = True,
) -> dict[str, str]:
    """这张候选表（或候选表模板）的四个默认视图：表格「全部」、画册「全部画册」「已选中」「待选」
    （views.ensure_candidate_views）。"""
    return views.ensure_candidate_views(
        api,
        info,
        table_id,
        state_field=STATE_FIELD,
        all_options=ALL_GALLERY_OPTIONS,
        picked_option=STATE_APPROVED,
        pending_option=STATE_PENDING,
        layout=layout,
        order_field=ORDER_FIELD,
        resort=resort,
    )


def create_fresh(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], name: str
) -> tuple[str, str]:
    """照原样新建一张空的候选表（列 = table_fields）：返回 (table_id, 表名)。同名的表已经有了（库重建过）就在名字
    后面加上时间另建一张。模板表也这样建；建好后都要 prepare_new。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables"
    # 第一个视图就叫「全部」（视图版本 5 起它是第一个视图）：不用改名，紧接着建「待选」也不会撞上刚改掉的名字
    default_view = views.ALL_VIEW
    body = {
        "table": {
            "name": name,
            "default_view_name": default_view,
            "fields": table_fields(ctx),
        }
    }
    try:
        table_id = api.call("POST", path, json=body)["table_id"]
    except feishu.FeishuError:
        body["table"]["name"] = f"{name} {utcnow():%m%d%H%M}"
        table_id = api.call("POST", path, json=body)["table_id"]
    return table_id, body["table"]["name"]


def prepare_new(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], table_id: str
) -> dict[str, str] | None:
    """新表（新建的或从模板复制的，列都是当前这一版）建好之后：先记下店铺列（默认视图按它摆），再建 / 摆默认视图。
    返回视图 id（见 default_views）。"""
    store_fields = [f["field_name"] for f in candidate_stores.specs(ctx)]
    if store_fields:
        candidate_stores.mark_done(ctx, table_id, store_fields)
    return default_views(ctx, api, info, table_id)


def _ensure_table(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], seed_id: str
) -> dict[str, Any]:
    """这张参考图的候选表，还没有就建（起过名的表名带名字）：开着 table.template 时从模板表复制结构（「看图」的
    封面效果这类只能在飞书里设的视图设置跟着走），复制不成照原样新建。
    任务归到每周表格的某一天时（books.place），从那本周表格的「候选表模板」复制到那天的文件夹里；列、交付包、店铺列、
    默认视图按模板当时的版本记，没跟上当前版本的交给后面的升级补上。"""
    from . import books, table_template  # 都依赖本模块，放到用时再导入

    existing = table_info(ctx, seed_id)
    if existing:
        return existing
    name = table_name(seed_id, seed_name(ctx, seed_id))
    day = books.place(ctx, seed_id)
    fields_version, views_version, package = FIELDS_VERSION, VIEWS_VERSION, True
    if day:
        where = books.ready_day(ctx, api, day)
        tpl = books.candidate_template(ctx, where)
        table_id, name = books.copy_candidate(ctx, api, where, tpl, name)
        copied: tuple[str, str] | None = (table_id, name)
        fields_version = int(tpl.get("fields_version") or 1)
        views_version = int(tpl.get("views_version") or 0)
        package = bool(tpl.get("package"))
        # 复制件的列、视图 id 和模板里的一样；默认视图已经按模板的店铺列摆过
        candidate_stores.mark_done(ctx, table_id, list(tpl.get("stores") or []))
        candidate_stores.layout_done(ctx, table_id)
        view_set = (dict(tpl.get("views") or {}) or None) if views.enabled(ctx) else None
        url = f"{where['url']}?table={table_id}" if where.get("url") else None
    else:
        copied = table_template.copy(ctx, api, info, name)
        table_id, name = copied or create_fresh(ctx, api, info, name)
        view_set = prepare_new(ctx, api, info, table_id)
        url = f"{info['url']}?table={table_id}" if info.get("url") else None
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO candidate_tables(seed_id, table_id, url, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (seed_id, table_id, url, now, now),
        )
        conn.execute(
            "UPDATE seed_names SET table_name = ?, updated_at = ? WHERE seed_id = ?",
            (name, now, seed_id),
        )
        log_event(
            conn,
            seed_id,
            "candidate_table_created",
            {
                "table_id": table_id,
                "url": url,
                "from_template": bool(copied),
                **({"day": day} if day else {}),
            },
        )
    # 新表的列已经是当前这一版（旧表格的模板只在跟上当前版本时才复制）；开着交付包时也带着「套图交付包」列
    _fields_done(ctx, table_id, fields_version)
    if packages.enabled(ctx) and package:
        _package_field_done(ctx, table_id)
    if view_set:
        _views_done(ctx, table_id, view_set, views_version)
    return table_info(ctx, seed_id) or {}


def _row_fields(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    asset: dict[str, Any],
    state_key: str,
    *,
    package_column: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """新行的字段，以及链接到期时间（还没有链接时为 None）。勾选框不写：没勾，等人勾。
    package_column：这张表有「套图交付包」列，所在批次的交付包打好了就写上链接。"""
    plan = loads(asset["plan_json"], {}) or {}
    summary = direction_summary(plan, asset["axis"])
    label = asset_label(asset["asset_id"], plan, asset["axis"])
    tag = direction_tag(summary["task_type"], summary["aspects"], summary["kind"])
    fields: dict[str, Any] = {
        "素材": label,
        "缩略图": [
            {
                "file_token": feishu_table.upload_image(
                    api,
                    seed_app(ctx, info, asset["seed_id"]),
                    f"{label}.jpg",
                    feishu_table.thumb_bytes(ctx, asset["thumb_path"], asset["path"]),
                )
            }
        ],
        state_key: STATE_PENDING,
        "方向": " ".join(p for p in (asset["variant_id"], summary["title"]) if p),
        "改了什么": "\n".join(brief_line(b) for b in summary["brief"]),
        "类型": tag.split(" · ", 1)[0] if tag else None,
        "模型": model_label(asset["model"]),
        "出图方式": MODE_LABELS[slot_mode(asset["slot_id"])],
        "批次": asset["batch_id"],
    }
    if package_column and asset["seed_id"] and asset["batch_id"]:
        fields[PACKAGE_FIELD] = packages.cell(
            ctx, packages.get(ctx, asset["seed_id"], asset["batch_id"])
        )
    link = feishu_table.download_link(
        ctx, asset["path"], feishu_table.download_name(label, asset["path"])
    )
    if link:
        fields.update(link["fields"])
    return (
        {k: v for k, v in fields.items() if v not in (None, "", [])},
        link["expires_at"] if link else None,
    )


def _add_rows(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    errors: list[dict[str, str]],
    items: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    """新进选款的图各进一行：这个出图槽位有预留行（还没补上图）就原地补上，没有就新增一行；
    返回 (新建的表数, 新增或补上的行数)。items（[{asset_id, seed_id}]）是每周选款表迁移时重写的行（已经定了的也在内，
    先按待选写，_sync_states 再改成定了的样子），走 _add_rows_batch。"""
    if items is not None:
        return _add_rows_batch(ctx, api, info, errors, items)
    tables = added = 0
    pending = _pending(ctx)
    where = _asset_orders(ctx, [item["asset_id"] for item in pending])
    for item in pending:
        asset_id, seed_id = item["asset_id"], item["seed_id"]
        try:
            created = table_info(ctx, seed_id) is None
            table = _ensure_table(ctx, api, info, seed_id)
            tables += int(created)
            fields, expires_at = _row_fields(
                ctx,
                api,
                info,
                feishu_table.asset_info(ctx, asset_id),
                state_field(ctx, table["table_id"]),
                package_column=packages.enabled(ctx)
                and package_field_ready(ctx, table["table_id"]),
            )
            spot = where.get(asset_id) or {}
            order = spot.get("order")
            if order is not None and _order_ready(ctx, table["table_id"]):
                fields[ORDER_FIELD] = order
            else:
                order = None
            slot = _open_slot(ctx, spot.get("job_id"), spot.get("slot_id"))
            record_id = None
            if slot:
                try:
                    # 预留行上没图时有人勾过、写过没出成的原因：补上图时一起清掉
                    api.call(
                        "PUT",
                        f"{_records_path(info, table['table_id'])}/{slot['record_id']}",
                        params={"user_id_type": "open_id"},
                        json={
                            "fields": {
                                **fields,
                                **candidate_stores.cleared(
                                    ctx, table["table_id"], CHECK_FIELD
                                ),
                                NOTE_FIELD: "",
                            }
                        },
                    )
                    record_id = slot["record_id"]
                except feishu.FeishuError:
                    # 预留行被人删了：另起一行，留下的预留记录下次同步删掉
                    record_id = None
                if record_id:
                    _save_slot(
                        ctx,
                        slot["job_id"],
                        slot["slot_id"],
                        seed_id,
                        asset_id=asset_id,
                        state=SLOT_FILLED,
                    )
            if not record_id:
                data = api.call(
                    "POST",
                    _records_path(info, table["table_id"]),
                    params={"user_id_type": "open_id"},
                    json={"fields": fields},
                )
                record_id = data["record"]["record_id"]
            _mark(
                ctx,
                asset_id,
                seed_id,
                record_id=record_id,
                state=STATE_PENDING,
                link_expires_at=expires_at,
                synced_at=now_iso(),
                attempts=0,
                last_error=None,
            )
            if order is not None:
                _remember_orders(ctx, seed_id, {record_id: order})
            added += 1
        except _FAILURES as exc:
            errors.append(
                {"asset_id": asset_id, "error": _fail(ctx, asset_id, seed_id, exc)}
            )
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return tables, added


# 迁移重写行时一批建多少行：飞书写一行要 3–4 秒，一批几十行也差不多（2026-10-29 线上实测）
_REBUILD_BATCH = 50


def _add_rows_batch(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    errors: list[dict[str, str]],
    items: list[dict[str, Any]],
) -> tuple[int, int]:
    """迁移时重写的行（没有预留行要补，迁移时都清掉了）：缩略图一张张传，记录按任务一批批建、建好一批记一批。
    返回 (新建的表数, 新增的行数)。"""
    tables = added = 0
    where = _asset_orders(ctx, [item["asset_id"] for item in items])
    by_seed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_seed[item["seed_id"]].append(item)
    for seed_id, group in by_seed.items():
        try:
            created = table_info(ctx, seed_id) is None
            table = _ensure_table(ctx, api, info, seed_id)
        except _FAILURES as exc:
            for item in group:
                errors.append({"asset_id": item["asset_id"], "error": _fail(ctx, item["asset_id"], seed_id, exc)})
            continue
        tables += int(created)
        table_id = table["table_id"]
        key = state_field(ctx, table_id)
        package_column = packages.enabled(ctx) and package_field_ready(ctx, table_id)
        ordered = _order_ready(ctx, table_id)
        batch: list[tuple[str, dict[str, Any], str | None, int | None]] = []
        for n, item in enumerate(group, 1):
            asset_id = item["asset_id"]
            try:
                fields, expires_at = _row_fields(
                    ctx,
                    api,
                    info,
                    feishu_table.asset_info(ctx, asset_id),
                    key,
                    package_column=package_column,
                )
            except _FAILURES as exc:
                errors.append({"asset_id": asset_id, "error": _fail(ctx, asset_id, seed_id, exc)})
                continue
            order = (where.get(asset_id) or {}).get("order") if ordered else None
            if order is not None:
                fields[ORDER_FIELD] = order
            batch.append((asset_id, fields, expires_at, order))
            lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
            if len(batch) >= _REBUILD_BATCH or n == len(group):
                added += _create_batch(ctx, api, info, seed_id, table_id, batch, errors)
                batch = []
        if batch:
            added += _create_batch(ctx, api, info, seed_id, table_id, batch, errors)
    return tables, added


def _create_batch(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    seed_id: str,
    table_id: str,
    batch: list[tuple[str, dict[str, Any], str | None, int | None]],
    errors: list[dict[str, str]],
) -> int:
    """建一批行并记下记录号；飞书偶发报错（如 1255002 RpcError）时等两秒再试一次，还不成就都记一次失败（下一次重写
    接着建；报错的那一批飞书那边可能已经建了，迁移核对前会删掉业务库不认识的行）。"""
    if not batch:
        return 0
    records = [fields for _, fields, _, _ in batch]
    try:
        try:
            ids = _batch_create(api, info, table_id, records)
        except feishu.FeishuError:
            time.sleep(2)
            ids = _batch_create(api, info, table_id, records)
    except _FAILURES as exc:
        for asset_id, *_ in batch:
            errors.append({"asset_id": asset_id, "error": _fail(ctx, asset_id, seed_id, exc)})
        return 0
    now = now_iso()
    for (asset_id, _, expires_at, order), record_id in zip(batch, ids):
        _mark(
            ctx,
            asset_id,
            seed_id,
            record_id=record_id,
            state=STATE_PENDING,
            link_expires_at=expires_at,
            synced_at=now,
            attempts=0,
            last_error=None,
        )
        if order is not None:
            _remember_orders(ctx, seed_id, {record_id: order})
    return len(ids)


def _batch_update(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    updates: list[tuple[str, dict[str, Any]]],
) -> None:
    path = _records_path(info, table_id) + "/batch_update"
    for i in range(0, len(updates), _PAGE):
        api.call(
            "POST",
            path,
            params={"user_id_type": "open_id"},
            json={
                "records": [
                    {"record_id": record_id, "fields": fields}
                    for record_id, fields in updates[i : i + _PAGE]
                ]
            },
        )


def _sync_states(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """选款已经定了的行：勾选框和选款结果改成对应的样子（选中打勾，其余不勾）。"""
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT c.asset_id, c.seed_id, c.record_id" + _DECIDED_SQL,
            (feishu_table.MAX_ATTEMPTS,),
        )
    ]
    if not rows:
        return 0
    latest = _latest_selection(ctx, [r["asset_id"] for r in rows])
    by_seed: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for row in rows:
        status, decision = latest.get(row["asset_id"], ("dismissed", None))
        by_seed.setdefault(row["seed_id"], []).append((row, state_of(status, decision)))
    updated = 0
    for seed_id, items in by_seed.items():
        table = table_info(ctx, seed_id)
        if not table:
            continue
        key = state_field(ctx, table["table_id"])
        # 没选中的连店铺列一起取消；选中行的店铺列和货号照店铺记录写（candidate_stores.sync_cells）
        unpicked = candidate_stores.cleared(ctx, table["table_id"], CHECK_FIELD)
        try:
            _batch_update(
                api,
                info,
                table["table_id"],
                [
                    (
                        row["record_id"],
                        {key: state, CHECK_FIELD: True}
                        if state == STATE_APPROVED
                        else {key: state, **unpicked},
                    )
                    for row, state in items
                ],
            )
        except _FAILURES as exc:
            for row, _ in items:
                errors.append(
                    {
                        "asset_id": row["asset_id"],
                        "error": _fail(ctx, row["asset_id"], seed_id, exc),
                    }
                )
            continue
        for row, state in items:
            _mark(ctx, row["asset_id"], seed_id, state=state, synced_at=now_iso())
        updated += len(items)
    return updated


def _fill_links(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    filled = 0
    for row in ctx.db.q(
        "SELECT c.asset_id, c.seed_id, c.record_id, a.path" + _LINK_SQL,
        (feishu_table.MAX_ATTEMPTS, feishu_table.refresh_before(ctx)),
    ):
        try:
            link = feishu_table.download_link(
                ctx,
                row["path"],
                feishu_table.download_name(
                    asset_label_of(ctx, row["asset_id"]), row["path"]
                ),
            )
            table = table_info(ctx, row["seed_id"])
            if not link or not table:
                continue
            api.call(
                "PUT",
                f"{_records_path(info, table['table_id'])}/{row['record_id']}",
                params={"user_id_type": "open_id"},
                json={"fields": link["fields"]},
            )
            _mark(
                ctx,
                row["asset_id"],
                row["seed_id"],
                link_expires_at=link["expires_at"],
                synced_at=now_iso(),
            )
            filled += 1
        except _FAILURES as exc:
            errors.append(
                {
                    "asset_id": row["asset_id"],
                    "error": _fail(ctx, row["asset_id"], row["seed_id"], exc),
                }
            )
    return filled


def _sync_names(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """候选表跟着任务名字改表名（飞书「更新数据表」）；失败记次数，满上限不再试。返回改了几张。"""
    renamed = 0
    for item in _renames(ctx):
        try:
            api.call(
                "PATCH",
                f"/bitable/v1/apps/{table_registry.app_for(info, item['table_id'])}/tables/{item['table_id']}",
                json={"name": item["name"]},
            )
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": item["seed_id"], "error": reason})
            with ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE seed_names SET table_attempts = table_attempts + 1, table_error = ?,"
                    " updated_at = ? WHERE seed_id = ?",
                    (reason, now_iso(), item["seed_id"]),
                )
            continue
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE seed_names SET table_name = ?, table_error = NULL, updated_at = ?"
                " WHERE seed_id = ?",
                (item["name"], now_iso(), item["seed_id"]),
            )
            log_event(
                conn,
                item["seed_id"],
                "candidate_table_renamed",
                {"table_id": item["table_id"], "name": item["name"]},
            )
        renamed += 1
    return renamed


def _list_fields(
    api: feishu.Client, info: dict[str, Any], table_id: str
) -> list[dict[str, Any]]:
    path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/fields"
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if token:
            params["page_token"] = token
        data = api.call("GET", path, params=params)
        items += data.get("items") or []
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return items


def _sync_fields(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """旧候选表：「状态」改名「选款结果」，「选中」「选款结果」补上说明，「选款结果」补上「未生成」「没出成」两个选项，
    补上套图三列、「没选原因」与「排序」（加在最后）；失败记次数，满上限不再试。返回改了几张。

    飞书「更新字段」是全量覆盖：type 和原来的 property（单选带选项 id）原样带回，格子里已有的值才不丢。
    """
    fixed = 0
    for item in _stale_fields(ctx):
        table_id = item["table_id"]
        try:
            changed = upgrade_fields(api, info, table_id)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": item["seed_id"], "error": reason})
            meta = _fields_meta(ctx)
            attempts = meta.setdefault("attempts", {})
            attempts[table_id] = int(attempts.get(table_id, 0)) + 1
            meta["error"] = f"{item['seed_id']}：{reason}"
            _save_fields_meta(ctx, meta)
            continue
        _fields_done(ctx, table_id)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                item["seed_id"],
                "candidate_fields_updated",
                {"table_id": table_id, **changed},
            )
        fixed += 1
    return fixed


def _help_texts(ctx: Ctx, table_id: str) -> dict[str, str]:
    texts = {CHECK_FIELD: CHECK_HELP, STATE_FIELD: STATE_HELP}
    for name in candidate_stores.present(ctx, table_id):
        texts[name] = candidate_stores.STORE_HELP
    return texts


def _sync_help(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """候选表的「选中」「选款结果」和店铺列说明跟上 HELP_VERSION（模板复制来的表、之前建的表都在这里改一次）；
    列还没跟上的表等 _sync_fields 改好再说；说明已经是这样的列不再写。每次同步最多改 HELP_TABLES_PER_SYNC 张，
    失败记次数、满上限不再试。返回改了几张。"""
    meta = loads(ctx.db.get_meta(HELP_META_KEY), None) or {}
    done: dict[str, int] = meta.get("done") or {}
    attempts: dict[str, int] = meta.get("attempts") or {}
    fields_meta = _fields_meta(ctx)
    todo = [
        dict(r)
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id")
        if done.get(r["table_id"]) != HELP_VERSION
        and _fields_current(fields_meta, r["table_id"])
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    ][:HELP_TABLES_PER_SYNC]
    fixed = 0
    for item in todo:
        table_id = item["table_id"]
        path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/fields"
        try:
            by_name = {f.get("field_name"): f for f in _list_fields(api, info, table_id)}
            for name, text in _help_texts(ctx, table_id).items():
                field = by_name.get(name)
                if not field:
                    continue
                current = field.get("description")
                if (current.get("text") if isinstance(current, dict) else current) == text:
                    continue
                body: dict[str, Any] = {"field_name": name, "type": field["type"], "description": _help(text)}
                if field.get("property"):
                    body["property"] = copy.deepcopy(field["property"])
                try:
                    api.call("PUT", f"{path}/{field['field_id']}", json=body)
                except feishu.FeishuError as exc:
                    if exc.code != DATA_NOT_CHANGE:
                        raise
        except _FAILURES as exc:
            attempts[table_id] = int(attempts.get(table_id, 0)) + 1
            errors.append({"seed_id": item["seed_id"], "error": f"列说明没改成：{type(exc).__name__}: {exc}"[:300]})
            continue
        done[table_id] = HELP_VERSION
        attempts.pop(table_id, None)
        fixed += 1
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    if todo:
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, HELP_META_KEY, dumps({"done": done, "attempts": attempts}))
    return fixed


def upgrade_fields(
    api: feishu.Client, info: dict[str, Any], table_id: str
) -> dict[str, Any]:
    """把一张候选表（或模板表）的列跟上当前版本：「状态」改名「选款结果」，「选中」「选款结果」补上说明和选项，
    缺的套图三列、「没选原因」「排序」加在最后。已经是这样的不报错。返回 {renamed, added}。"""
    path = f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/fields"
    by_name = {f.get("field_name"): f for f in _list_fields(api, info, table_id)}
    # 上一次只改成了一半时，「状态」已经叫「选款结果」
    old = OLD_STATE_FIELD if OLD_STATE_FIELD in by_name else STATE_FIELD
    for name, new, text in (
        (CHECK_FIELD, CHECK_FIELD, CHECK_HELP),
        (old, STATE_FIELD, STATE_HELP),
    ):
        field = by_name[name]
        body: dict[str, Any] = {
            "field_name": new,
            "type": field["type"],
            "description": _help(text),
        }
        if field.get("property"):
            body["property"] = copy.deepcopy(field["property"])
        if new == STATE_FIELD:
            # 原有选项连 id 一起带回，只在后面补上缺的
            options = (body.setdefault("property", {})).setdefault("options", [])
            have = {o.get("name") for o in options}
            options += [{"name": s} for s in STATES if s not in have]
        try:
            api.call("PUT", f"{path}/{field['field_id']}", json=body)
        except feishu.FeishuError as exc:
            # 字段已经是这个样子（上一版升级改过）时飞书报 DataNotChange，算改好了
            if exc.code != DATA_NOT_CHANGE:
                raise
    added = [
        s
        for s in (*SCENE_FIELDS, REASON_SPEC, ORDER_SPEC)
        if s["field_name"] not in by_name
    ]
    for spec in added:
        api.call("POST", path, json=spec)
    return {"renamed": old != STATE_FIELD, "added": [s["field_name"] for s in added]}


def add_package_column(api: feishu.Client, info: dict[str, Any], table_id: str) -> bool:
    """这张表还没有「套图交付包」列就补上（加在最后）。返回补了没有。"""
    names = {f.get("field_name") for f in _list_fields(api, info, table_id)}
    if PACKAGE_FIELD in names:
        return False
    api.call(
        "POST",
        f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/fields",
        json=PACKAGE_SPEC,
    )
    return True


def _sync_package_columns(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """开着交付包时，给还没有「套图交付包」列的候选表补上（新增的列排在最后）；失败记次数，满上限不再试。"""
    added = 0
    for item in _package_columns_pending(ctx):
        table_id = item["table_id"]
        try:
            fresh = add_package_column(api, info, table_id)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": item["seed_id"], "error": reason})
            meta = _package_meta(ctx)
            attempts = meta.setdefault("attempts", {})
            attempts[table_id] = int(attempts.get(table_id, 0)) + 1
            meta["error"] = f"{item['seed_id']}：{reason}"
            _save_package_meta(ctx, meta)
            continue
        added += int(fresh)
        _package_field_done(ctx, table_id)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                item["seed_id"],
                "candidate_package_field_added",
                {"table_id": table_id, "added": fresh},
            )
    return added


def _batch_records(ctx: Ctx, seed_id: str, batch_id: str) -> list[str]:
    """候选表里属于这个批次的候选行（record_id；表顶的行和还没补上图的预留行不在里面）。"""
    return [
        r["record_id"]
        for r in ctx.db.q(
            "SELECT c.record_id FROM candidate_rows c"
            " JOIN assets a ON a.asset_id = c.asset_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " WHERE c.seed_id = ? AND v.batch_id = ? AND c.record_id IS NOT NULL"
            " ORDER BY c.asset_id",
            (seed_id, batch_id),
        )
    ]


def _sync_packages(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """交付包打好 / 重打 / 续签 / 删掉之后，把这个批次每一行候选行的「套图交付包」改成现在的链接（没有包时清空）；
    套图行、交付包行由 _sync_top 跟着改（交付包链接算在它们的 state_key 里）。返回改了几个批次。"""
    written = 0
    for pkg in _packages_pending(ctx):
        seed_id, batch_id = pkg["seed_id"], pkg["batch_id"]
        table = table_info(ctx, seed_id)
        if not table:
            continue
        value = packages.cell(ctx, pkg)
        try:
            records = _batch_records(ctx, seed_id, batch_id)
            if records:
                _batch_update(
                    api,
                    info,
                    table["table_id"],
                    [(record_id, {PACKAGE_FIELD: value}) for record_id in records],
                )
            packages.update(
                ctx,
                seed_id,
                batch_id,
                table_state=packages.table_key(pkg),
                table_attempts=0,
                table_error=None,
            )
            written += 1
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            packages.update(
                ctx,
                seed_id,
                batch_id,
                table_attempts=int(pkg["table_attempts"] or 0) + 1,
                table_error=reason,
            )
            errors.append({"seed_id": seed_id, "batch_id": batch_id, "error": reason})
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return written


def _sync_views(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """之前建的候选表补上或改成当前这套默认视图（表格「全部」+ 画册「全部画册」「已选中」「待选」，按「排序」排，列和
    画册卡片按 layout 摆）：没跟上这一版的就地改（之前已经按「排序」排好的——版本 2 起——只给新建的视图设排序，v3 排序
    接口有频率限制）；已经是这一版、只是店铺列变了的只重摆列和卡片。失败记次数，满上限不再试。返回改了几张。"""
    done = 0
    for item in _views_pending(ctx):
        table_id = item["table_id"]
        meta = _views_meta(ctx)
        ids = (meta.get("done") or {}).get(table_id) or {}
        version = _views_version(meta, table_id)
        arrange_only = version >= VIEWS_VERSION
        layout = _layout_for(ctx, table_id)
        try:
            if arrange_only:
                views.apply_layout(
                    api,
                    info,
                    table_id,
                    layout,
                    grids=(ids["all"],),
                    galleries=dict(
                        zip(
                            (ids["all_gallery"], ids["picked"], ids["pending"]),
                            layout.boards(),
                        )
                    ),
                )
            else:
                ids = ensure_views(api, info, table_id, layout, resort=version < 2)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": item["seed_id"], "error": reason})
            meta = _views_meta(ctx)
            attempts = meta.setdefault("attempts", {})
            attempts[table_id] = int(attempts.get(table_id, 0)) + 1
            meta["error"] = f"{item['seed_id']}：{reason}"
            _save_views_meta(ctx, meta)
            continue
        _views_done(ctx, table_id, ids)
        if candidate_stores.layout_pending(ctx, table_id):
            candidate_stores.layout_done(ctx, table_id)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                item["seed_id"],
                "candidate_views_arranged"
                if arrange_only
                else "candidate_views_created",
                {"table_id": table_id, **ids},
            )
        done += 1
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return done


def _sync_shelf(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """归档了的任务把候选表挪进「已归档」文件夹，调出了的挪回根目录（只在状态变化时挪一次）。返回挪了几张。
    每周表格里的候选表挪进那一天的「10-26 已归档」子文件夹，调出了挪回那一天的文件夹。"""
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    moves = shelf.pending_moves(ctx)
    if not moves:
        return 0
    days = {m["table_id"]: books.table_day(ctx, m["table_id"]) for m in moves}

    def legacy_in(item: dict[str, Any]) -> bool:
        return item["want"] == shelf.FOLDER_IN and not days[item["table_id"]]

    folder: str | None = None
    if any(legacy_in(m) for m in moves):
        try:
            folder = feishu_table.shelf_folder(ctx, api, info)
        except _FAILURES as exc:
            reason = f"没找到也没建成「已归档」文件夹：{type(exc).__name__}: {exc}"[
                :300
            ]
            for item in moves:
                if legacy_in(item):
                    shelf.mark_folder(ctx, item["seed_id"], error=reason)
                    errors.append({"seed_id": item["seed_id"], "error": reason})
            moves = [m for m in moves if not legacy_in(m)]
    moved = 0
    for item in moves:
        day = days[item["table_id"]]
        into = item["want"] == shelf.FOLDER_IN
        if day:
            where, target = day, day["archive_folder_id"] if into else day["folder_id"]
        else:
            where, target = info, folder if into else None
        try:
            feishu_table.move_table(api, where, item["table_id"], target)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:300]
            shelf.mark_folder(ctx, item["seed_id"], error=reason)
            errors.append({"seed_id": item["seed_id"], "error": reason})
            continue
        shelf.mark_folder(ctx, item["seed_id"], state=item["want"])
        moved += 1
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return moved


def _sync_locked(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """持有租约时做一遍同步：旧表补列 / 改列、补「套图交付包」列、默认视图 → 补新行（有预留行的原地补图）
    → 同步已定的状态 → 补 / 续链接 → 表名跟上任务名字和选款状态 → 预留行（建、没出成、删）
    → 表顶的按钮行、交付包行和套图行 → 候选行的交付包链接 → 各行的「排序」→ 归档 / 调出的任务挪表。
    最前面先备好模板表（新表从它复制）。"""
    from . import table_template  # 模板依赖本模块，放到用时再导入

    template = table_template.ensure(ctx, api, info, errors)
    fields = _sync_fields(ctx, api, info, errors)
    package_columns = _sync_package_columns(ctx, api, info, errors)
    store_columns = candidate_stores.sync_columns(ctx, api, info, errors)
    helped = _sync_help(ctx, api, info, errors)
    view_tables = _sync_views(ctx, api, info, errors)
    tables, added = _add_rows(ctx, api, info, errors)
    out = {
        "fields": fields,
        "package_columns": package_columns,
        "tables_created": tables,
        "added": added,
        "updated": _sync_states(ctx, api, info, errors),
        "linked": _fill_links(ctx, api, info, errors),
        "renamed": _sync_names(ctx, api, info, errors),
    }
    # 店铺列（开着 Temu 自动上架时）：补列、选中行的店铺勾和货号
    if store_columns:
        out["store_columns"] = store_columns
    store_cells = candidate_stores.sync_cells(ctx, api, info, errors)
    if store_cells:
        out["store_cells"] = store_cells
    slots = _sync_slot_rows(ctx, api, info, errors)
    out["tables_created"] += slots.pop("tables_created")
    plan = _top_plan(ctx)
    out["top"] = _sync_top(ctx, api, info, errors, plan)
    out["packages"] = _sync_packages(ctx, api, info, errors)
    orders = _sync_orders(ctx, api, info, errors, plan)
    # 模板、默认视图、挪表、预留行、排序：有事做时才出现在结果里
    if template:
        out["template"] = template
    if helped:
        out["help"] = helped
    if view_tables:
        out["views"] = view_tables
    if any(slots.values()):
        out["slots"] = slots
    if orders:
        out["orders"] = orders
    moved = _sync_shelf(ctx, api, info, errors)
    if moved:
        out["moved"] = moved
    return out


# ---------------------------------------------------------------- 预留行


def reserve_enabled(ctx: Ctx) -> bool:
    """方向确认后就按出图槽位在候选表里占好行（table.reserve_rows，缺省开着）。"""
    return feishu_table.enabled(ctx) and bool(ctx.setting("table.reserve_rows", True))


def _open_slot(
    ctx: Ctx, job_id: str | None, slot_id: str | None
) -> dict[str, Any] | None:
    """这个出图槽位还没补上图的预留行（未生成或没出成）。"""
    if not job_id or not slot_id:
        return None
    row = ctx.db.q1(
        "SELECT * FROM candidate_slots WHERE job_id = ? AND slot_id = ?"
        " AND record_id IS NOT NULL AND asset_id IS NULL",
        (job_id, slot_id),
    )
    return dict(row) if row else None


def _save_slot(
    ctx: Ctx, job_id: str, slot_id: str, seed_id: str, **values: Any
) -> None:
    values = {"seed_id": seed_id, "updated_at": now_iso(), **values}
    values.setdefault("state", SLOT_UNMADE)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO candidate_slots(job_id, slot_id, {cols}) VALUES(?, ?, {marks})"
            f" ON CONFLICT(job_id, slot_id) DO UPDATE SET {sets}",
            (job_id, slot_id, *values.values()),
        )


def _slot_failed(
    ctx: Ctx, seed_id: str, keys: list[tuple[str, str]], reason: str
) -> None:
    """这几个槽位的预留行没建成：记次数，满上限不再试。"""
    now = now_iso()
    with ctx.db.tx() as conn:
        for job_id, slot_id in keys:
            conn.execute(
                "INSERT INTO candidate_slots(job_id, slot_id, seed_id, state, attempts, last_error,"
                " updated_at) VALUES(?, ?, ?, ?, 1, ?, ?) ON CONFLICT(job_id, slot_id) DO UPDATE SET"
                " attempts = candidate_slots.attempts + 1, last_error = excluded.last_error,"
                " updated_at = excluded.updated_at",
                (job_id, slot_id, seed_id, SLOT_UNMADE, reason, now),
            )


def _slot_keys(
    ctx: Ctx, sql: str, params: tuple[Any, ...] = ()
) -> set[tuple[str, str]]:
    return {(r["job_id"], r["slot_id"]) for r in ctx.db.q(sql, params)}


def _slot_changes(ctx: Ctx) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """各任务的预留行要做的：{seed_id: {"create": [...], "update": [...], "delete": [...]}}。

    还在出图的方向里、这个槽位的图还没进表（也不在等着进表）→ 建一行「未生成」；
    已有的预留行：图进了表（另起了一行）、槽位没了（停掉方向、S06 对齐删掉）→ 删掉；
    方向出完了还没图 → 「没出成」，又重出了 → 回到「未生成」。列还没跟上的表先不动。
    """
    have = {
        (r["job_id"], r["slot_id"]): dict(r)
        for r in ctx.db.q("SELECT * FROM candidate_slots")
    }
    enabled = reserve_enabled(ctx)
    if not enabled and not any(
        r["record_id"] and not r["asset_id"] for r in have.values()
    ):
        return {}
    meta = _fields_meta(ctx)
    tables = {
        r["seed_id"]: r["table_id"]
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables")
    }

    def ready(seed_id: str) -> bool:
        table_id = tables.get(seed_id)
        return table_id is None or _fields_current(meta, table_id)

    in_table = _slot_keys(
        ctx,
        "SELECT DISTINCT a.job_id, a.slot_id FROM candidate_rows c"
        " JOIN assets a ON a.asset_id = c.asset_id WHERE c.record_id IS NOT NULL",
    )
    waiting = _slot_keys(
        ctx,
        "SELECT DISTINCT a.job_id, a.slot_id FROM reviews r JOIN assets a ON a.asset_id = r.subject_id"
        " LEFT JOIN candidate_rows c ON c.asset_id = a.asset_id"
        " WHERE r.subject_type = 'asset_selection' AND r.status = 'open' AND c.record_id IS NULL",
    )
    alive = _slot_keys(ctx, "SELECT job_id, slot_id FROM job_slots")
    status = {
        r["job_id"]: r["status"] for r in ctx.db.q("SELECT job_id, status FROM jobs")
    }
    out: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"create": [], "update": [], "delete": []}
    )
    for key, row in have.items():
        if (
            row["asset_id"]
            or not row["record_id"]
            or not ready(row["seed_id"])
            or int(row["attempts"] or 0) >= feishu_table.MAX_ATTEMPTS
        ):
            continue
        if key not in alive or key in in_table:
            out[row["seed_id"]]["delete"].append(row)
            continue
        if key in waiting:
            continue
        want = SLOT_NO_IMAGE if status.get(key[0]) in JOB_DONE else SLOT_UNMADE
        if row["state"] != want:
            out[row["seed_id"]]["update"].append({**row, "want": want})
    if enabled:
        for r in ctx.db.q(
            "SELECT s.job_id, s.slot_id, f.seed_id FROM job_slots s JOIN jobs j ON j.job_id = s.job_id"
            " JOIN variants v ON v.variant_id = j.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            f" WHERE j.status NOT IN ({','.join('?' for _ in JOB_DONE)})"
            " ORDER BY s.job_id, s.slot_id",
            JOB_DONE,
        ):
            key = (r["job_id"], r["slot_id"])
            row = have.get(key)
            if (
                (row and (row["record_id"] or row["asset_id"]))
                or (row and int(row["attempts"] or 0) >= feishu_table.MAX_ATTEMPTS)
                or key in in_table
                or key in waiting
                or not ready(r["seed_id"])
            ):
                continue
            out[r["seed_id"]]["create"].append(dict(r))
    return {s: v for s, v in out.items() if any(v.values())}


def _slot_work(ctx: Ctx) -> bool:
    return bool(_slot_changes(ctx))


def _slot_details(
    ctx: Ctx, keys: list[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """这几个槽位写进预留行要用的：方向、模型、出图方式、批次、没出成的原因。"""
    jobs = sorted({k[0] for k in keys})
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for i in range(0, len(jobs), _PAGE):
        chunk = jobs[i : i + _PAGE]
        for r in ctx.db.q(
            "SELECT s.job_id, s.slot_id, s.model, s.last_error, j.batch_id, j.variant_id,"
            " j.last_error AS job_error, v.plan_json, v.axis FROM job_slots s"
            " JOIN jobs j ON j.job_id = s.job_id JOIN variants v ON v.variant_id = j.variant_id"
            f" WHERE s.job_id IN ({','.join('?' for _ in chunk)})",
            tuple(chunk),
        ):
            out[(r["job_id"], r["slot_id"])] = dict(r)
    return out


def _slot_fields(detail: dict[str, Any], state: str) -> dict[str, Any]:
    """预留行的字段：素材写「V0123 方向标题 · flare 图生图 · 未生成」，方向、模型、出图方式、批次照填。"""
    plan = loads(detail["plan_json"], {}) or {}
    summary = direction_summary(plan, detail["axis"])
    tag = direction_tag(summary["task_type"], summary["aspects"], summary["kind"])
    result = STATE_NO_IMAGE if state == SLOT_NO_IMAGE else STATE_UNMADE
    direction = " ".join(p for p in (detail["variant_id"], summary["title"]) if p)
    model = model_label(detail["model"])
    mode = MODE_LABELS[slot_mode(detail["slot_id"])]
    note = ""
    if state == SLOT_NO_IMAGE:
        reason = (detail.get("last_error") or detail.get("job_error") or "").strip()
        note = f"没出成：{reason[:80]}".rstrip("：")
    return {
        "素材": " · ".join(
            p for p in (direction, f"{model} {mode}".strip(), result) if p
        ),
        STATE_FIELD: result,
        "方向": direction,
        "改了什么": "\n".join(brief_line(b) for b in summary["brief"]),
        "类型": tag.split(" · ", 1)[0] if tag else None,
        "模型": model,
        "出图方式": mode,
        "批次": detail["batch_id"],
        NOTE_FIELD: note,
    }


def _sync_slot_rows(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, int]:
    """预留行跟上出图进度：建「未生成」的行（还没有候选表就先建表），出完没图的改「没出成」，
    图另起了一行、槽位没了的删掉。返回各做了几行。"""
    out = {"reserved": 0, "changed": 0, "dropped": 0, "tables_created": 0}
    changes = _slot_changes(ctx)
    if not changes:
        return out
    details = _slot_details(
        ctx,
        [
            (item["job_id"], item["slot_id"])
            for change in changes.values()
            for item in change["create"] + change["update"]
        ],
    )
    where, slots = _positions(ctx)
    for seed_id, change in sorted(changes.items()):
        creates = [
            item
            for item in change["create"]
            if (item["job_id"], item["slot_id"]) in details
        ]
        try:
            if creates and table_info(ctx, seed_id) is None:
                _ensure_table(ctx, api, info, seed_id)
                out["tables_created"] += 1
            table = table_info(ctx, seed_id)
            if not table:
                continue
            table_id = table["table_id"]
            dropped = [r["record_id"] for r in change["delete"]]
            if dropped:
                _delete_records(api, info, table_id, dropped)
                _forget_orders(ctx, dropped)
                with ctx.db.tx() as conn:
                    for row in change["delete"]:
                        conn.execute(
                            "DELETE FROM candidate_slots WHERE job_id = ? AND slot_id = ?",
                            (row["job_id"], row["slot_id"]),
                        )
                out["dropped"] += len(dropped)
            updates = [
                row
                for row in change["update"]
                if (row["job_id"], row["slot_id"]) in details
            ]
            if updates:
                _batch_update(
                    api,
                    info,
                    table_id,
                    [
                        (
                            row["record_id"],
                            {
                                k: v
                                for k, v in _slot_fields(
                                    details[(row["job_id"], row["slot_id"])],
                                    row["want"],
                                ).items()
                                if k in ("素材", STATE_FIELD, NOTE_FIELD)
                            },
                        )
                        for row in updates
                    ],
                )
                for row in updates:
                    _save_slot(
                        ctx,
                        row["job_id"],
                        row["slot_id"],
                        seed_id,
                        state=row["want"],
                    )
                out["changed"] += len(updates)
            if creates:
                ordered = _order_ready(ctx, table_id)
                values: list[int | None] = [
                    _slot_order(where, slots, item["job_id"], item["slot_id"])
                    for item in creates
                ]
                records = []
                for item, value in zip(creates, values):
                    fields = _filled(
                        _slot_fields(
                            details[(item["job_id"], item["slot_id"])], SLOT_UNMADE
                        )
                    )
                    if ordered and value is not None:
                        fields[ORDER_FIELD] = value
                    records.append(fields)
                ids = _batch_create(api, info, table_id, records)
                for item, record_id in zip(creates, ids):
                    _save_slot(
                        ctx,
                        item["job_id"],
                        item["slot_id"],
                        seed_id,
                        record_id=record_id,
                        state=SLOT_UNMADE,
                        attempts=0,
                        last_error=None,
                    )
                if ordered:
                    _remember_orders(
                        ctx,
                        seed_id,
                        {
                            record_id: value
                            for record_id, value in zip(ids, values)
                            if value is not None
                        },
                    )
                out["reserved"] += len(ids)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": seed_id, "error": reason})
            _slot_failed(
                ctx,
                seed_id,
                [(i["job_id"], i["slot_id"]) for i in creates]
                + [(r["job_id"], r["slot_id"]) for r in change["update"]],
                reason,
            )
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return out


# ---------------------------------------------------------------- 表顶：按钮行、交付包行与套图行


def control_enabled(ctx: Ctx) -> bool:
    """候选表最上面的按钮行（「选中的先出套图」「选款完成」）、交付包行和套图行开着（table.control_row）。"""
    return feishu_table.enabled(ctx) and bool(ctx.setting("table.control_row", True))


def _actions(ctx: Ctx, seed_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (r["kind"], r["batch_id"]): dict(r)
        for r in ctx.db.q(
            "SELECT * FROM candidate_actions WHERE seed_id = ?", (seed_id,)
        )
    }


def _action(
    ctx: Ctx, seed_id: str, kind: str, batch_id: str = ""
) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT * FROM candidate_actions WHERE seed_id = ? AND kind = ? AND batch_id = ?",
        (seed_id, kind, batch_id),
    )
    return dict(row) if row else None


def _save_action(
    ctx: Ctx, seed_id: str, kind: str, batch_id: str, table_id: str, **values: Any
) -> None:
    """写一行按钮行 / 交付包行的记录（没有就新增）。"""
    now = now_iso()
    values = {"table_id": table_id, "updated_at": now, **values}
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO candidate_actions(seed_id, kind, batch_id, created_at,"
            f" {cols}) VALUES(?, ?, ?, ?, {marks})"
            f" ON CONFLICT(seed_id, kind, batch_id) DO UPDATE SET {sets}",
            (seed_id, kind, batch_id, now, *values.values()),
        )


def _adopt_legacy(ctx: Ctx) -> int:
    """2026-10-26 前建在表最底下的「▶ 选款完成」控制行（candidate_controls）搬成表顶的按钮行：
    飞书里的记录不动（同步时改文字、排到表顶），备注和上次提交照旧。返回搬了几行。"""
    rows = [dict(r) for r in ctx.db.q("SELECT * FROM candidate_controls")]
    if not rows:
        return 0
    tables = {
        r["seed_id"]: r["table_id"]
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables")
    }
    now = now_iso()
    with ctx.db.tx() as conn:
        for row in rows:
            taken = conn.execute(
                "SELECT 1 FROM candidate_actions WHERE seed_id = ? AND kind = ? AND batch_id = ''",
                (row["seed_id"], KIND_ACCEPT),
            ).fetchone()
            if (
                row["record_id"]
                and row["table_id"] == tables.get(row["seed_id"])
                and not taken
            ):
                conn.execute(
                    "INSERT INTO candidate_actions(seed_id, kind, batch_id, table_id, record_id, note,"
                    " triggered_at, triggered_by, attempts, created_at, updated_at)"
                    " VALUES(?, ?, '', ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        row["seed_id"],
                        KIND_ACCEPT,
                        row["table_id"],
                        row["record_id"],
                        row["note"],
                        row["triggered_at"],
                        row["triggered_by"],
                        row["created_at"],
                        now,
                    ),
                )
            conn.execute(
                "DELETE FROM candidate_controls WHERE seed_id = ?", (row["seed_id"],)
            )
    return len(rows)


def _meta_attempts(ctx: Ctx, key: str) -> dict[str, int]:
    return (loads(ctx.db.get_meta(key), None) or {}).get("attempts") or {}


def _meta_failed(ctx: Ctx, key: str, table_id: str, reason: str) -> None:
    meta = loads(ctx.db.get_meta(key), None) or {}
    attempts = meta.setdefault("attempts", {})
    attempts[table_id] = int(attempts.get(table_id, 0)) + 1
    meta["error"] = reason
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, key, dumps(meta))


def _meta_ok(ctx: Ctx, key: str, table_id: str) -> None:
    meta = loads(ctx.db.get_meta(key), None) or {}
    attempts = meta.get("attempts") or {}
    if table_id not in attempts:
        return
    attempts.pop(table_id)
    meta["attempts"] = attempts
    if not attempts:
        meta.pop("error", None)
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, key, dumps(meta))


def _scene_rows(ctx: Ctx, seed_id: str) -> dict[str, dict[str, Any]]:
    return {
        r["asset_id"]: dict(r)
        for r in ctx.db.q(
            "SELECT * FROM candidate_scene_rows WHERE seed_id = ? ORDER BY asset_id",
            (seed_id,),
        )
    }


def _save_scene_row(ctx: Ctx, item: dict[str, Any], **values: Any) -> None:
    values = {
        "seed_id": item["seed_id"],
        "set_id": item["set"]["set_id"],
        "state_key": item["key"],
        "updated_at": now_iso(),
        **values,
    }
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO candidate_scene_rows(asset_id, {cols}) VALUES(?, {marks})"
            f" ON CONFLICT(asset_id) DO UPDATE SET {sets}",
            (item["asset_id"], *values.values()),
        )


def _scene_targets(ctx: Ctx) -> dict[str, list[dict[str, Any]]]:
    """各任务表顶该列的套图：选中、出过套图的款（最近一套），按款号排。{seed_id: [item]}。"""
    from . import scenes  # scenes 依赖本模块之外的表格与通知，放到用时再导入

    if not scenes.enabled(ctx):
        return {}
    approved = scenes.approved_assets(ctx)
    sets = scenes.latest_sets(ctx, [a["asset_id"] for a in approved])
    ids = sorted({row["set_id"] for row in sets.values()})
    counts: dict[str, int] = {}
    for i in range(0, len(ids), _PAGE):
        chunk = ids[i : i + _PAGE]
        marks = ",".join("?" for _ in chunk)
        counts.update(
            {
                r[0]: int(r[1])
                for r in ctx.db.q(
                    "SELECT set_id, COUNT(*) FROM scene_images WHERE role = 'scene'"
                    f" AND set_id IN ({marks}) GROUP BY set_id",
                    tuple(chunk),
                )
            }
        )
    # 表里有「套图交付包」列的任务：套图行也写所在批次的交付包，链接变了跟着改
    package_seeds = _package_seeds(ctx)
    bundles = packages.by_key(ctx) if package_seeds else {}
    out: dict[str, list[dict[str, Any]]] = {}
    for asset in sorted(approved, key=lambda a: a["asset_id"]):
        row = sets.get(asset["asset_id"])
        if not row:
            continue
        scenes_n = counts.get(row["set_id"], 0)
        item = {
            "asset_id": asset["asset_id"],
            "seed_id": asset["seed_id"],
            "batch_id": asset["batch_id"],
            "set": row,
            "scenes": scenes_n,
            # 写进表里的样子：哪一套、状态、张数、压缩包链接（续签后会变）变了才再写
            "key": f"{row['set_id']}|{row['status']}|{scenes_n}|{row['export_url'] or ''}",
        }
        if asset["seed_id"] in package_seeds:
            bundle = bundles.get((asset["seed_id"], asset["batch_id"]))
            item["package"] = packages.cell(ctx, bundle)
            item["key"] += f"|{packages.table_key(bundle)}"
        out.setdefault(asset["seed_id"], []).append(item)
    return out


def _top_plan(ctx: Ctx) -> dict[str, Any]:
    """表顶各行和「排序」要用的，一次算齐：各任务各批次的交付包、该列的套图、交付包标完已下载的批次。"""
    on = control_enabled(ctx)
    views_by_seed = packages.by_seed(ctx) if on and packages.enabled(ctx) else {}
    return {
        "packages": views_by_seed,
        "scenes": _scene_targets(ctx) if on else {},
        "done": {
            (v["seed_id"], v["batch_id"])
            for items in views_by_seed.values()
            for v in items
            if packages.complete(v)
        },
    }


def _scene_orders(
    seed_id: str, items: list[dict[str, Any]], done: set[tuple[str, str]]
) -> dict[str, int]:
    """套图行的「排序」：{asset_id: 排序}，同一批次按款号排。"""
    seen: dict[str, int] = defaultdict(int)
    out = {}
    for item in items:
        batch_id = item.get("batch_id") or ""
        out[item["asset_id"]] = order_scene(
            batch_id, seen[batch_id], done=(seed_id, batch_id) in done
        )
        seen[batch_id] += 1
    return out


def _local(ctx: Ctx, value: str | None) -> str:
    if not value:
        return ""
    tz = ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
    return f"{parse_iso(value).astimezone(tz):%m-%d %H:%M}"


def _package_note(view: dict[str, Any]) -> str:
    """交付包行的备注：现在是什么情况、还要做什么。"""
    if view["error"]:
        return f"交付包没打成：{view['error']}"[:200]
    if not view["ready"]:
        return "套图出完后自动打包，打好后这一行出现下载链接"
    parts = []
    if view["download_outdated"]:
        parts.append("重打过，下载新包后再勾")
    elif view["downloaded"]:
        parts.append("这一版已经标了已下载")
    if view["stale"]:
        parts.append("有新出完的款，交付包马上重打：等新包打好再下载")
    if view["busy"]:
        parts.append(f"还有 {view['busy']} 款在出套图，出完会重打")
    if view["parked"]:
        parts.append(f"{view['parked']} 款在 mooya 那边等人工或暂停")
    return "；".join(parts) or "下载后在这一行的「选中」打勾，任务才会归档"


def _package_fields(
    ctx: Ctx, view: dict[str, Any], package_column: bool
) -> dict[str, Any]:
    batch = view["batch_id"]
    cell = packages.cell(ctx, packages.get(ctx, view["seed_id"], batch))
    if packages.complete(view):
        text = PACKAGE_ROW_DONE.format(batch=batch, items=view["items"])
        note = (
            f"{_local(ctx, view['downloaded_at'])} 标了已下载；标错了在 POD 对话里撤销"
        )
    elif view["ready"]:
        text = PACKAGE_ROW_TEXT.format(batch=batch, items=view["items"])
        note = _package_note(view)
    else:
        text = PACKAGE_ROW_WAIT.format(batch=batch)
        note = _package_note(view)
    fields: dict[str, Any] = {
        "素材": text,
        "方向": PACKAGE_ROW_HELP,
        NOTE_FIELD: note,
        SCENE_LINK_FIELD: cell,
    }
    if package_column:
        fields[PACKAGE_FIELD] = cell
    return fields


def _top_specs(
    ctx: Ctx, seed_id: str, table_id: str, plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """这张表表顶该有的按钮行和交付包行：[{kind, batch_id, fields, key}]。
    按钮行只管文字（备注是点了以后写的）；交付包行连备注和链接一起跟着交付包变。"""
    # 「选中的先出套图」2026-10-30 起不再建，已有的那一行按 specs 里没有它删掉
    specs: list[dict[str, Any]] = [
        {
            "kind": KIND_ACCEPT,
            "batch_id": "",
            "fields": {"素材": CONTROL_TEXT, "方向": CONTROL_HELP},
        },
    ]
    column = package_field_ready(ctx, table_id)
    for view in plan["packages"].get(seed_id, []):
        specs.append(
            {
                "kind": KIND_PACKAGE,
                "batch_id": view["batch_id"],
                "fields": _package_fields(ctx, view, column),
            }
        )
    if candidate_stores.present(ctx, table_id):
        # 有店铺列时「选中」藏起来了：说明里改成勾任意一个勾选框
        for spec in specs:
            spec["fields"] = {
                k: candidate_stores.control_text(ctx, v) if isinstance(v, str) else v
                for k, v in spec["fields"].items()
            }
    for spec in specs:
        spec["key"] = dumps(spec["fields"])
    return specs


def _top_tables(ctx: Ctx) -> list[tuple[str, str]]:
    """要管表顶的候选表（列已跟上、失败没满上限）：[(seed_id, table_id)]。"""
    if not control_enabled(ctx):
        return []
    meta = _fields_meta(ctx)
    attempts = _meta_attempts(ctx, ACTIONS_META_KEY)
    return [
        (r["seed_id"], r["table_id"])
        for r in ctx.db.q(
            "SELECT seed_id, table_id FROM candidate_tables ORDER BY seed_id"
        )
        if _fields_current(meta, r["table_id"])
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    ]


def _top_changes(
    ctx: Ctx, seed_id: str, table_id: str, plan: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """这张表表顶要做的：建 / 改 / 删的按钮行和交付包行，建 / 改 / 删的套图行。"""
    have = _actions(ctx, seed_id)
    specs = _top_specs(ctx, seed_id, table_id, plan)
    wanted = {(s["kind"], s["batch_id"]) for s in specs}
    out: dict[str, list[dict[str, Any]]] = {
        "create": [],
        "update": [],
        "drop": [r for key, r in have.items() if key not in wanted],
        "scene_create": [],
        "scene_update": [],
        "scene_drop": [],
    }
    for spec in specs:
        row = have.get((spec["kind"], spec["batch_id"]))
        if not row or not row["record_id"]:
            out["create"].append(spec)
        elif row["state_key"] != spec["key"]:
            out["update"].append({**spec, "record_id": row["record_id"]})
    rows = _scene_rows(ctx, seed_id)
    items = plan["scenes"].get(seed_id, [])
    for item in items:
        row = rows.get(item["asset_id"])
        if not row or not row["record_id"]:
            out["scene_create"].append(item)
        elif row["state_key"] != item["key"]:
            out["scene_update"].append(item)
    keep = {item["asset_id"] for item in items}
    out["scene_drop"] = [r for asset_id, r in rows.items() if asset_id not in keep]
    return out


def _top_work(ctx: Ctx, plan: dict[str, Any]) -> bool:
    """表顶有要写的：旧的控制行还没搬、按钮行 / 交付包行 / 套图行要建、要改或要删。"""
    if not control_enabled(ctx):
        return False
    if ctx.db.scalar("SELECT COUNT(*) FROM candidate_controls"):
        return True
    return any(
        any(_top_changes(ctx, seed_id, table_id, plan).values())
        for seed_id, table_id in _top_tables(ctx)
    )


def _scene_note(row: dict[str, Any]) -> str:
    status = row["status"]
    if status == "partial":
        missing = int(row["expected"] or 0) - int(row["actual"] or 0)
        return f"缺 {missing} 张，已出的照样能下载" if missing > 0 else "缺图"
    if status == "failed":
        return f"没出成：{(row['last_error'] or '').strip()[:80]}".rstrip("：")
    if status == "waiting_human":
        return "mooya 那边在等人工：去 mooya 图案管线处理，处理完这边自动接着拉"
    if status == "paused":
        return "mooya 那边已暂停"
    return ""


def _scene_thumb(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    item: dict[str, Any],
    row: dict[str, Any] | None,
) -> str | None:
    """套图行的缩略图：这一套的第一张场景图，每套只传一次（file_token 记下来，重写这一行时复用）。"""
    set_id = item["set"]["set_id"]
    if row and row.get("thumb_token") and row.get("thumb_set_id") == set_id:
        return row["thumb_token"]
    picture = ctx.db.q1(
        "SELECT path FROM scene_images WHERE set_id = ?"
        " ORDER BY CASE role WHEN 'scene' THEN 0 ELSE 1 END, idx LIMIT 1",
        (set_id,),
    )
    if not picture:
        return None
    data = images.thumbnail_jpeg(store.ensure_local(ctx, picture["path"]).read_bytes())
    return feishu_table.upload_image(
        api, seed_app(ctx, info, item["seed_id"]), f"{item['asset_id']}_套图.jpg", data
    )


def _scene_fields(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    item: dict[str, Any],
    row: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """套图行要写的字段与缩略图 file_token。没出完的清掉图、张数和链接（重出时旧链接不留着）。"""
    from . import scenes  # 同上，用时再导入

    current = item["set"]
    fields: dict[str, Any] = {
        "素材": SCENE_ROW_PREFIX + asset_label_of(ctx, item["asset_id"]),
        SCENE_STATE_FIELD: scenes.STATUS_LABELS.get(
            current["status"], current["status"]
        ),
        NOTE_FIELD: _scene_note(current),
        SCENE_COUNT_FIELD: None,
        SCENE_LINK_FIELD: None,
        "缩略图": [],
    }
    if "package" in item:
        fields[PACKAGE_FIELD] = item["package"]
    token = None
    if current["status"] in scenes.OK_FINAL:
        fields[SCENE_COUNT_FIELD] = item["scenes"]
        if current["export_url"] and current["export_expires_at"]:
            until = parse_iso(current["export_expires_at"]).astimezone(
                ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
            )
            fields[SCENE_LINK_FIELD] = {
                "link": current["export_url"],
                "text": f"下载套图（{until:%Y-%m-%d} 前有效）",
            }
        token = _scene_thumb(ctx, api, info, item, row)
        if token:
            fields["缩略图"] = [{"file_token": token}]
    return fields, token


def _filled(fields: dict[str, Any]) -> dict[str, Any]:
    """新增的行只写有值的字段。"""
    return {k: v for k, v in fields.items() if v not in (None, "", [])}


def _batch_create(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    records: list[dict[str, Any]],
) -> list[str]:
    """新增几行，返回记录号（和传进去的顺序一致）。"""
    ids: list[str] = []
    path = _records_path(info, table_id) + "/batch_create"
    for i in range(0, len(records), _PAGE):
        data = api.call(
            "POST",
            path,
            params={"user_id_type": "open_id"},
            json={"records": [{"fields": f} for f in records[i : i + _PAGE]]},
        )
        ids += [r["record_id"] for r in data.get("records") or []]
    if len(ids) != len(records):
        raise feishu.FeishuError(f"新增了 {len(ids)} 行，应该是 {len(records)} 行")
    return ids


def _delete_records(
    api: feishu.Client, info: dict[str, Any], table_id: str, record_ids: list[str]
) -> None:
    """删掉这几行；有的行已经被人删了时整批会失败，改成逐行删、删不掉的跳过。"""
    if not record_ids:
        return
    path = _records_path(info, table_id)
    try:
        for i in range(0, len(record_ids), _PAGE):
            api.call(
                "POST",
                path + "/batch_delete",
                json={"records": record_ids[i : i + _PAGE]},
            )
    except feishu.FeishuError:
        for record_id in record_ids:
            with contextlib.suppress(feishu.FeishuError):
                api.call("DELETE", f"{path}/{record_id}")


def _read_records(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    record_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """回读几行的勾（「选中」或任意一个店铺列）和最后修改人：{record_id: {"checked", "by"}}；表里已经没有的行不在结果里。"""
    data = api.call(
        "POST",
        _records_path(info, table_id) + "/batch_get",
        json={
            "record_ids": record_ids,
            "user_id_type": "open_id",
            "automatic_fields": True,
        },
    )
    out: dict[str, dict[str, Any]] = {}
    for item in data.get("records") or []:
        modifier = (item.get("last_modified_by") or {}).get("id")
        out[item["record_id"]] = {
            "checked": candidate_stores.checked(
                ctx, table_id, item.get("fields") or {}, CHECK_FIELD
            ),
            "by": f"feishu:{modifier}"
            if isinstance(modifier, str) and modifier.startswith("ou_")
            else None,
        }
    return out


def _apply_top(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    seed_id: str,
    table_id: str,
    plan: dict[str, Any],
) -> dict[str, int]:
    """把一张表的表顶改成该有的样子；新建的行带上「排序」（之后变了由 _sync_orders 改）。"""
    change = _top_changes(ctx, seed_id, table_id, plan)
    ordered = _order_ready(ctx, table_id)
    out = {"created": 0, "updated": 0, "deleted": 0, "scene_rows": 0}
    dropped = [
        r["record_id"] for r in change["drop"] + change["scene_drop"] if r["record_id"]
    ]
    if dropped:
        _delete_records(api, info, table_id, dropped)
        _forget_orders(ctx, dropped)
    if change["drop"] or change["scene_drop"]:
        with ctx.db.tx() as conn:
            for r in change["drop"]:
                conn.execute(
                    "DELETE FROM candidate_actions WHERE seed_id = ? AND kind = ? AND batch_id = ?",
                    (seed_id, r["kind"], r["batch_id"]),
                )
            for r in change["scene_drop"]:
                conn.execute(
                    "DELETE FROM candidate_scene_rows WHERE asset_id = ?",
                    (r["asset_id"],),
                )
        out["deleted"] = len(change["drop"]) + len(change["scene_drop"])
    if change["update"]:
        _batch_update(
            api,
            info,
            table_id,
            [(s["record_id"], s["fields"]) for s in change["update"]],
        )
        for spec in change["update"]:
            values: dict[str, Any] = {"state_key": spec["key"]}
            if NOTE_FIELD in spec["fields"]:
                values["note"] = spec["fields"][NOTE_FIELD]
            _save_action(
                ctx, seed_id, spec["kind"], spec["batch_id"], table_id, **values
            )
        out["updated"] = len(change["update"])
    if change["create"]:
        values_by_spec = [
            order_action(
                s["kind"], s["batch_id"], done=(seed_id, s["batch_id"]) in plan["done"]
            )
            for s in change["create"]
        ]
        records = []
        for spec, value in zip(change["create"], values_by_spec):
            fields = _filled(spec["fields"])
            if ordered:
                fields[ORDER_FIELD] = value
            records.append(fields)
        ids = _batch_create(api, info, table_id, records)
        for spec, record_id in zip(change["create"], ids):
            _save_action(
                ctx,
                seed_id,
                spec["kind"],
                spec["batch_id"],
                table_id,
                record_id=record_id,
                state_key=spec["key"],
                note=spec["fields"].get(NOTE_FIELD),
                attempts=0,
                last_error=None,
            )
        if ordered:
            _remember_orders(ctx, seed_id, dict(zip(ids, values_by_spec)))
        out["created"] = len(ids)
    rows = _scene_rows(ctx, seed_id)
    updates = []
    for item in change["scene_update"]:
        row = rows[item["asset_id"]]
        fields, token = _scene_fields(ctx, api, info, item, row)
        updates.append((item, fields, token, row["record_id"]))
    if updates:
        _batch_update(api, info, table_id, [(rid, f) for _, f, _, rid in updates])
        for item, _, token, rid in updates:
            _save_scene_row(
                ctx,
                item,
                record_id=rid,
                thumb_token=token,
                thumb_set_id=item["set"]["set_id"] if token else None,
            )
    creates = []
    scene_orders = _scene_orders(seed_id, plan["scenes"].get(seed_id, []), plan["done"])
    for item in change["scene_create"]:
        fields, token = _scene_fields(ctx, api, info, item, rows.get(item["asset_id"]))
        fields = _filled(fields)
        if ordered:
            fields[ORDER_FIELD] = scene_orders[item["asset_id"]]
        creates.append((item, fields, token))
    if creates:
        ids = _batch_create(api, info, table_id, [f for _, f, _ in creates])
        for (item, _, token), rid in zip(creates, ids):
            _save_scene_row(
                ctx,
                item,
                record_id=rid,
                thumb_token=token,
                thumb_set_id=item["set"]["set_id"] if token else None,
            )
        if ordered:
            _remember_orders(
                ctx,
                seed_id,
                {
                    rid: scene_orders[item["asset_id"]]
                    for (item, _, _), rid in zip(creates, ids)
                },
            )
    out["scene_rows"] = len(updates) + len(creates)
    return out


def _sync_top(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    errors: list[dict[str, str]],
    plan: dict[str, Any],
) -> dict[str, int]:
    """每张候选表表顶的按钮行、交付包行和套图行：没有就建，文字 / 备注 / 链接变了就改，不该有的删掉
    （位置由「排序」管，见 _sync_orders）。失败记次数，满上限的表不再试。"""
    out = {"created": 0, "updated": 0, "deleted": 0, "scene_rows": 0}
    if not control_enabled(ctx):
        return out
    _adopt_legacy(ctx)
    for seed_id, table_id in _top_tables(ctx):
        try:
            for key, n in _apply_top(ctx, api, info, seed_id, table_id, plan).items():
                out[key] += n
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": seed_id, "error": reason})
            _meta_failed(ctx, ACTIONS_META_KEY, table_id, f"{seed_id}：{reason}")
            continue
        _meta_ok(ctx, ACTIONS_META_KEY, table_id)
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return out


# ---------------------------------------------------------------- 各行的「排序」


def _layout(ctx: Ctx, plan: dict[str, Any]) -> dict[str, dict[str, int]]:
    """各张候选表每一行该排在哪：{seed_id: {record_id: 排序}}（只算开着默认视图、有「排序」列、没失败满上限的表）。"""
    if not views.enabled(ctx):
        return {}
    meta = _fields_meta(ctx)
    attempts = _meta_attempts(ctx, ORDERS_META_KEY)
    out: dict[str, dict[str, int]] = {
        r["seed_id"]: {}
        for r in ctx.db.q("SELECT seed_id, table_id FROM candidate_tables")
        if _fields_current(meta, r["table_id"])
        and int(attempts.get(r["table_id"], 0)) < feishu_table.MAX_ATTEMPTS
    }
    if not out:
        return out
    where, slots = _positions(ctx)
    for r in ctx.db.q(
        "SELECT c.seed_id, c.record_id, a.job_id, a.slot_id, a.asset_version FROM candidate_rows c"
        " JOIN assets a ON a.asset_id = c.asset_id WHERE c.record_id IS NOT NULL"
    ):
        if r["seed_id"] in out:
            value = _slot_order(
                where, slots, r["job_id"], r["slot_id"], int(r["asset_version"] or 1)
            )
            out[r["seed_id"]][r["record_id"]] = (
                value if value is not None else ORDER_DONE - 1
            )
    for r in ctx.db.q(
        "SELECT seed_id, record_id, job_id, slot_id FROM candidate_slots"
        " WHERE record_id IS NOT NULL AND asset_id IS NULL"
    ):
        if r["seed_id"] in out:
            value = _slot_order(where, slots, r["job_id"], r["slot_id"])
            if value is not None:
                out[r["seed_id"]][r["record_id"]] = value
    if control_enabled(ctx):
        for r in ctx.db.q(
            "SELECT seed_id, kind, batch_id, record_id FROM candidate_actions"
            " WHERE record_id IS NOT NULL"
        ):
            if r["seed_id"] in out:
                out[r["seed_id"]][r["record_id"]] = order_action(
                    r["kind"],
                    r["batch_id"],
                    done=(r["seed_id"], r["batch_id"]) in plan["done"],
                )
        for seed_id, items in plan["scenes"].items():
            if seed_id not in out:
                continue
            rows = _scene_rows(ctx, seed_id)
            for asset_id, value in _scene_orders(seed_id, items, plan["done"]).items():
                record_id = (rows.get(asset_id) or {}).get("record_id")
                if record_id:
                    out[seed_id][record_id] = value
    return out


def _orders_pending(ctx: Ctx, plan: dict[str, Any]) -> dict[str, dict[str, int]]:
    """「排序」要改的行：{seed_id: {record_id: 排序}}。"""
    have: dict[str, dict[str, int]] = defaultdict(dict)
    for r in ctx.db.q("SELECT seed_id, record_id, order_value FROM candidate_orders"):
        have[r["seed_id"]][r["record_id"]] = int(r["order_value"])
    out = {}
    for seed_id, desired in _layout(ctx, plan).items():
        changed = {
            record_id: value
            for record_id, value in desired.items()
            if have[seed_id].get(record_id) != value
        }
        if changed:
            out[seed_id] = changed
    return out


def _write_orders(
    api: feishu.Client, info: dict[str, Any], table_id: str, changed: dict[str, int]
) -> None:
    """改「排序」：整批改不成（多半是有的行被人删了）就逐行改，改不了的跳过。"""
    updates = [
        (record_id, {ORDER_FIELD: value}) for record_id, value in changed.items()
    ]
    try:
        _batch_update(api, info, table_id, updates)
    except feishu.FeishuError:
        path = _records_path(info, table_id)
        for record_id, fields in updates:
            with contextlib.suppress(feishu.FeishuError):
                api.call(
                    "PUT",
                    f"{path}/{record_id}",
                    params={"user_id_type": "open_id"},
                    json={"fields": fields},
                )


def _sync_orders(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    errors: list[dict[str, str]],
    plan: dict[str, Any],
) -> int:
    """各行的「排序」跟上该在的位置（新进的行、方向变了、交付包标了已下载……）；只写变了的。返回改了几行。"""
    written = 0
    for seed_id, changed in sorted(_orders_pending(ctx, plan).items()):
        table = table_info(ctx, seed_id)
        if not table:
            continue
        try:
            _write_orders(api, info, table["table_id"], changed)
        except _FAILURES as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            errors.append({"seed_id": seed_id, "error": reason})
            _meta_failed(
                ctx, ORDERS_META_KEY, table["table_id"], f"{seed_id}：{reason}"
            )
            continue
        _remember_orders(ctx, seed_id, changed)
        _meta_ok(ctx, ORDERS_META_KEY, table["table_id"])
        written += len(changed)
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return written


# ---------------------------------------------------------------- 按钮行被勾上


def _local_time(ctx: Ctx) -> str:
    tz = ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
    return f"{utcnow().astimezone(tz):%m-%d %H:%M}"


def _write_action(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    action: dict[str, Any],
    note: str,
) -> None:
    """按钮行 / 交付包行取消勾选（「选中」和店铺列）、备注写成 note（库里也记下）。"""
    api.call(
        "PUT",
        f"{_records_path(info, action['table_id'])}/{action['record_id']}",
        params={"user_id_type": "open_id"},
        json={
            "fields": {
                **candidate_stores.cleared(ctx, action["table_id"], CHECK_FIELD),
                NOTE_FIELD: note,
            }
        },
    )
    _save_action(
        ctx,
        action["seed_id"],
        action["kind"],
        action["batch_id"],
        action["table_id"],
        note=note,
    )


def unfinished_images(ctx: Ctx, seed_id: str) -> dict[str, int]:
    """这个任务确认过的方向里还没出完的：{"jobs": 方向数, "images": 还没进表的出图槽位数,
    "waiting": 只在等待审积压降下来的方向数}。只被待审积压挡住的不算没出完：选款之前积压降不下来，
    算上就永远点不了「选款完成」。"""
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT j.job_id, j.status, j.blocked_reason FROM jobs j"
            " JOIN variants v ON v.variant_id = j.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            f" WHERE f.seed_id = ? AND j.status NOT IN ({','.join('?' for _ in JOB_DONE)})",
            (seed_id, *JOB_DONE),
        )
    ]
    jobs = [
        r["job_id"]
        for r in rows
        if not (r["status"] == "blocked" and r["blocked_reason"] == "review_backlog")
    ]
    images = 0
    for i in range(0, len(jobs), _PAGE):
        chunk = jobs[i : i + _PAGE]
        marks = ",".join("?" for _ in chunk)
        slots = _slot_keys(
            ctx,
            f"SELECT job_id, slot_id FROM job_slots WHERE job_id IN ({marks})",
            tuple(chunk),
        )
        shown = _slot_keys(
            ctx,
            "SELECT DISTINCT a.job_id, a.slot_id FROM candidate_rows c"
            " JOIN assets a ON a.asset_id = c.asset_id"
            f" WHERE c.record_id IS NOT NULL AND a.job_id IN ({marks})",
            tuple(chunk),
        )
        images += len(slots - shown)
    return {"jobs": len(jobs), "images": images, "waiting": len(rows) - len(jobs)}


def unfinished_text(left: dict[str, int]) -> str:
    """还有图没出完怎么说（选款完成随时能点，之后再出的图表名是【有新图】）。"""
    if left["images"]:
        return f"还有 {left['jobs']} 个方向 {left['images']} 张图没出完"
    return f"还有 {left['jobs']} 个方向在收尾（图在检查、入库）"


def _busy(ctx: Ctx, seed_id: str) -> dict[str, Any] | None:
    """这个任务正在跑的验收或提前出套图。"""
    return next(
        (
            t
            for t in bg.running(ctx)
            if t["kind"] in (KIND_ACCEPT, KIND_DISPATCH) and t["subject"] == seed_id
        ),
        None,
    )


def _trigger_package(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    action: dict[str, Any],
    *,
    by: str,
) -> dict[str, Any]:
    """交付包行勾上了：当场标已下载（取消勾选、写备注），只对这个任务扫一次归档。"""
    seed_id, batch_id = action["seed_id"], action["batch_id"]
    try:
        marked = packages.mark_downloaded(ctx, seed_id, batch_id, by=by, source="table")
    except StageError as exc:
        _write_action(
            ctx, api, info, action, f"没标上（{_local_time(ctx)}）：{exc}"[:200]
        )
        return {
            "refused": str(exc),
            "seed_id": seed_id,
            "batch_id": batch_id,
            "kind": KIND_PACKAGE,
        }
    _write_action(ctx, api, info, action, f"{_local_time(ctx)} 标了已下载")
    return {
        "downloaded": batch_id,
        "seed_id": seed_id,
        "by": by,
        "kind": KIND_PACKAGE,
        "already": bool(marked.get("already")),
        **shelf.after_download(ctx, seed_id),
    }


def trigger_action(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    action: dict[str, Any],
    *,
    by: str,
    source: str,
) -> dict[str, Any]:
    """按钮行 / 交付包行勾上了。交付包行当场标已下载；「选款完成」（和删掉之前的「选中的先出套图」）取消勾选、写「…中」，
    拉起后台（做完发卡片）。这个任务正在验收或提前出套图时只取消勾选并提示。「选款完成」随时能点（2026-10-30 起不等图出完）。"""
    seed_id, kind = action["seed_id"], action["kind"]
    if kind == KIND_PACKAGE:
        return _trigger_package(ctx, api, info, action, by=by)
    running = _busy(ctx, seed_id)
    if running:
        doing = ACTION_LABELS.get(running["kind"], running["kind"])
        _write_action(
            ctx, api, info, action, f"正在{doing}（{running['task_id']}），完成后会通知"
        )
        return {"busy": running["task_id"], "seed_id": seed_id, "kind": kind}
    _write_action(
        ctx, api, info, action, f"{ACTION_LABELS[kind]}中（{_local_time(ctx)} 提交）"
    )
    task = bg.spawn(
        ctx,
        ["table", kind, "--seed", seed_id, "--by", by, "--source", "table"],
        kind=kind,
        subject=seed_id,
        notify=True,
    )
    _save_action(
        ctx,
        seed_id,
        kind,
        "",
        action["table_id"],
        triggered_at=now_iso(),
        triggered_by=by,
    )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            seed_id,
            "selection_accept_requested"
            if kind == KIND_ACCEPT
            else "selection_dispatch_requested",
            {"by": by, "source": source, "task_id": task["task_id"]},
        )
    return {"started": task["task_id"], "seed_id": seed_id, "by": by, "kind": kind}


def control_event(
    ctx: Ctx, *, file_token: str, table_id: str, record_ids: list[str], by: str
) -> dict[str, Any]:
    """多维表格记录变更事件（网关转来勾上了的记录）：是按钮行或交付包行、回读确认还勾着，就按种类处理；
    「选中的款」里的勾是下载确认，交给 downloads。

    不是选款表、不认识的表、改的不是这几行，都直接返回，不调飞书；我们自己取消勾选也会发事件，回读是没勾。
    每周选款表：「选中的款」是旧表格的那一张或某一天的那一张，候选表在旧表格或某本周表格里（按 table_id 认）。
    """
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    info = feishu_table.base(ctx) or {}
    scope = books.scope_of_table(ctx, table_id)
    if scope and file_token == scope.get("app_token"):
        # 「选中的款」里勾上的可能是下载确认，也可能是确认上架：两边各自回读自己那一列
        from . import temu  # 上架模块依赖本模块，放到用时再导入

        result = downloads.table_event(ctx, record_ids=record_ids, by=by, info=scope)
        if temu.enabled(ctx):
            listed = temu.table_event(ctx, record_ids=record_ids, by=by, info=scope)
            if "ignored" not in listed:
                return (
                    {"temu": listed, "downloads": result}
                    if "ignored" not in result
                    else listed
                )
        return result
    if not info or file_token != table_registry.app_for(info, table_id):
        return {"ignored": "不是选款表"}
    # 已经选中的款在原图行 / 「套图」行勾店铺列：当场记店铺、领货号、准备上架（不看按钮行开关）
    ticks = candidate_stores.tick_targets(ctx, table_id, record_ids)
    stores_out: dict[str, Any] | None = None
    if ticks:
        with feishu.Client(ctx) as api:
            stores_out = candidate_stores.apply_ticks(ctx, api, info, table_id, ticks, by=by)
    if not control_enabled(ctx):
        return _with_stores({"ignored": "按钮行关着（table.control_row）"}, stores_out)
    _adopt_legacy(ctx)
    wanted = set(record_ids) - {t["record_id"] for t in ticks}
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM candidate_actions WHERE table_id = ? AND record_id IS NOT NULL"
            " ORDER BY kind, batch_id",
            (table_id,),
        )
        if r["record_id"] in wanted
    ]
    if not rows:
        return _with_stores({"ignored": "改的不是按钮行"}, stores_out)
    with feishu.Client(ctx) as api:
        seen = _read_records(ctx, api, info, table_id, [r["record_id"] for r in rows])
        results = [
            trigger_action(ctx, api, info, row, by=by, source="event")
            for row in rows
            if (seen.get(row["record_id"]) or {}).get("checked")
        ]
    if not results:
        return _with_stores({"ignored": "按钮行没勾", "seed_id": rows[0]["seed_id"]}, stores_out)
    return _with_stores(results[0] if len(results) == 1 else {"results": results}, stores_out)


def _with_stores(out: dict[str, Any], stores_out: dict[str, Any] | None) -> dict[str, Any]:
    """control_event 的结果带上店铺勾的处理结果；店铺勾真处理了（不是忽略）就不再报「忽略」。"""
    if not stores_out or "ignored" in stores_out:
        return out
    if "ignored" in out:
        return {"stores": stores_out}
    return {**out, "stores": stores_out}


def kick_controls(ctx: Ctx) -> dict[str, Any] | None:
    """巡检到点时在后台跑 `table poll`：每张表要回读一次，表多了十几秒，网关等一次巡检最多 30 秒。
    「选中的款」的上架状态（到了 listing.read_minutes 才读）和下载确认也在这个进程里回读。"""
    info = feishu_table.base(ctx)
    if not info:
        return None
    controls = control_enabled(ctx) and bool(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM candidate_actions WHERE record_id IS NOT NULL"
        )
        or ctx.db.scalar(
            "SELECT COUNT(*) FROM candidate_controls WHERE record_id IS NOT NULL"
        )
    )
    confirms = downloads.enabled(ctx) and downloads.ready(info)
    from . import cells, listed_table, temu  # 都依赖本模块，放到用时再导入

    listings = temu.enabled(ctx) and temu.ready(info)
    if (
        not controls
        and not confirms
        and not listings
        and not listing.due(ctx)
        and not listed_table.due(ctx)
        and not candidate_stores.poll_due(ctx)
        and not cells.due(ctx)
    ):
        return None
    # 撤销窗口里的勾：没有守着的后台进程（比如上一个退出了）就再拉一个
    candidate_stores.kick_ticks(ctx)
    if any(t["kind"] == "controls" for t in bg.running(ctx)):
        return None
    return bg.spawn(ctx, ["table", "poll"], kind="controls")


def poll_controls(ctx: Ctx) -> dict[str, Any]:
    """巡检兜底：回读各张表的按钮行和交付包行，勾着的（事件没送到、网关重启时漏掉的）照样按种类处理。"""
    if not control_enabled(ctx):
        return {}
    info = feishu_table.base(ctx)
    _adopt_legacy(ctx)
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.db.q(
        "SELECT * FROM candidate_actions WHERE record_id IS NOT NULL"
        " ORDER BY seed_id, kind, batch_id"
    ):
        by_table[r["table_id"]].append(dict(r))
    if not info or not by_table:
        return {}
    out: dict[str, Any] = {
        "checked": 0,
        "started": [],
        "busy": [],
        "refused": [],
        "downloaded": [],
        "errors": [],
    }
    try:
        with feishu.Client(ctx) as api:
            for table_id, rows in by_table.items():
                try:
                    seen = _read_records(
                        ctx, api, info, table_id, [r["record_id"] for r in rows]
                    )
                except _FAILURES as exc:
                    out["errors"].append(
                        {
                            "seed_id": rows[0]["seed_id"],
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    )
                    continue
                out["checked"] += len(rows)
                for row in rows:
                    got = seen.get(row["record_id"])
                    if not got or not got["checked"]:
                        continue
                    try:
                        result = trigger_action(
                            ctx,
                            api,
                            info,
                            row,
                            by=got["by"] or CONTROL_BY,
                            source="poll",
                        )
                    except _FAILURES as exc:
                        out["errors"].append(
                            {
                                "seed_id": row["seed_id"],
                                "error": f"{type(exc).__name__}: {exc}"[:300],
                            }
                        )
                        continue
                    for key in ("started", "busy", "refused", "downloaded"):
                        if result.get(key):
                            out[key].append(result[key])
    except _FAILURES as exc:
        out["errors"].append({"error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


def _result_note(ctx: Ctx, result: dict[str, Any]) -> str:
    """验收结果写进「选款完成」那一行的备注：「上次验收 10-23 15:50：选中 6 · 没选 114 · 留到下次 3」。"""
    from . import scenes  # 同上，用时再导入

    parts = [
        f"选中 {len(result['approved'])}",
        f"没选 {len(result['rejected'])}",
    ]
    if result["fresh"]:
        parts.append(f"留到下次 {len(result['fresh'])}")
    if result["already_decided"]:
        parts.append(f"卡片上已定 {len(result['already_decided'])}")
    note = f"上次验收 {_local_time(ctx)}：" + " · ".join(parts)
    if (
        result["approved"]
        and scenes.enabled(ctx)
        and ctx.setting("scenes.auto_after_accept", True)
    ):
        note += "；选中的款接着出套图，出完列在表顶"
    return note


def _dispatch_note(ctx: Ctx, result: dict[str, Any]) -> str:
    """提前出套图的结果写进「选中的先出套图」那一行的备注。"""
    parts = [f"选中 {len(result['approved'])}"]
    if result["already_decided"]:
        parts.append(f"卡片上已定 {len(result['already_decided'])}")
    note = f"上次提前出套图 {_local_time(ctx)}：" + " · ".join(parts)
    scene_out = result["scenes"]
    if not result["picked"]:
        return note + "；没有勾上、出了图的款"
    if scene_out.get("enabled") is False:
        return note + "；套图没打开（scenes.enabled）"
    if scene_out.get("error"):
        return (note + f"；没交上套图：{scene_out['error']}")[:300]
    if scene_out.get("queued"):
        return note + f"；交给 mooya 出套图 {scene_out['queued']} 款，出完列在表顶"
    return note + "；勾上的款都已经出过或在出套图"


def relabel_rows(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, int]:
    """把各候选表已有行的素材名字改成「编号_方向短语」（调用方持租约）：素材列、缩略图（按新名字重传）；
    待选 / 已选中的行顺带按新名字重签下载原图。每张表攒齐后一次 batch_update；出错只记下来，不计入行的失败次数。"""
    out = {"rows": 0, "updated": 0, "links": 0}
    by_seed: dict[str, list[dict[str, Any]]] = {}
    for row in ctx.db.q(
        "SELECT asset_id, seed_id, record_id, state FROM candidate_rows"
        " WHERE record_id IS NOT NULL ORDER BY seed_id, asset_id"
    ):
        by_seed.setdefault(row["seed_id"], []).append(dict(row))
    for seed_id, rows in by_seed.items():
        table = table_info(ctx, seed_id)
        if not table:
            continue
        updates: list[tuple[str, dict[str, Any]]] = []
        links: list[tuple[str, str]] = []
        for row in rows:
            out["rows"] += 1
            try:
                asset = feishu_table.asset_info(ctx, row["asset_id"])
                label = asset_label(
                    asset["asset_id"],
                    loads(asset["plan_json"], {}) or {},
                    asset["axis"],
                )
                fields: dict[str, Any] = {
                    "素材": label,
                    "缩略图": [
                        {
                            "file_token": feishu_table.upload_image(
                                api,
                                seed_app(ctx, info, seed_id),
                                f"{label}.jpg",
                                feishu_table.thumb_bytes(
                                    ctx, asset["thumb_path"], asset["path"]
                                ),
                            )
                        }
                    ],
                }
                if row["state"] in (STATE_PENDING, STATE_APPROVED):
                    link = feishu_table.download_link(
                        ctx,
                        asset["path"],
                        feishu_table.download_name(label, asset["path"]),
                    )
                    if link:
                        fields.update(link["fields"])
                        links.append((row["asset_id"], link["expires_at"]))
                updates.append((row["record_id"], fields))
            except _FAILURES as exc:
                errors.append(
                    {
                        "asset_id": row["asset_id"],
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
            lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
        if not updates:
            continue
        try:
            _batch_update(api, info, table["table_id"], updates)
        except _FAILURES as exc:
            errors.append(
                {"seed_id": seed_id, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
            continue
        out["updated"] += len(updates)
        now = now_iso()
        for asset_id, expires_at in links:
            _mark(ctx, asset_id, seed_id, link_expires_at=expires_at, synced_at=now)
        out["links"] += len(links)
    return out


def sync(ctx: Ctx, *, retry: bool = False) -> dict[str, Any]:
    """把进选款的图同步进各任务的候选表；另有同步进程持有租约时直接返回。"""
    from . import table_template  # 模板依赖本模块，放到用时再导入

    if not feishu_table.enabled(ctx):
        return {"skipped": True, "reason": "table.enabled 为 false"}
    if retry:
        failed = [k for k in (ACTIONS_META_KEY, ORDERS_META_KEY) if ctx.db.get_meta(k)]
        with ctx.db.tx() as conn:
            conn.execute("UPDATE candidate_rows SET attempts = 0 WHERE attempts > 0")
            conn.execute("UPDATE candidate_slots SET attempts = 0 WHERE attempts > 0")
            conn.execute(
                "UPDATE scene_packages SET table_attempts = 0 WHERE table_attempts > 0"
            )
            for key in failed:
                ctx.db.set_meta(conn, key, dumps({}))
        meta = _fields_meta(ctx)
        if meta.get("attempts"):
            meta["attempts"] = {}
            _save_fields_meta(ctx, meta)
        package_meta = _package_meta(ctx)
        if package_meta.get("attempts"):
            package_meta["attempts"] = {}
            _save_package_meta(ctx, package_meta)
        views_meta = _views_meta(ctx)
        if views_meta.get("attempts"):
            views_meta["attempts"] = {}
            _save_views_meta(ctx, views_meta)
        shelf.reset_folder_attempts(ctx)
        table_template.reset_attempts(ctx)
    if not has_work(ctx):
        return {
            "fields": 0,
            "package_columns": 0,
            "tables_created": 0,
            "added": 0,
            "updated": 0,
            "linked": 0,
            "renamed": 0,
            "packages": 0,
            "failed": 0,
        }
    if not lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS):
        return {"skipped": True, "reason": "另一个同步进程正在运行"}
    errors: list[dict[str, str]] = []
    try:
        with feishu.Client(ctx) as api:
            info, _ = feishu_table.ensure_base(ctx, api)
            out = _sync_locked(ctx, api, info, errors)
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    return {**out, "failed": len(errors), "errors": errors[:5]}


# ---------------------------------------------------------------- 验收


def _read_checks(
    api: feishu.Client, info: dict[str, Any], table_id: str
) -> tuple[dict[str, bool], dict[str, Any]]:
    """表里每一行勾没勾、没选原因填了什么：({record_id: 是否勾上}, {record_id: 原因格子})。
    没勾的勾选框、没填的原因接口里可能不返回，按没勾、没填算。"""
    checks, cells, _ = _read_picks(None, api, info, table_id)
    return checks, cells


def _read_picks(
    ctx: Ctx | None, api: feishu.Client, info: dict[str, Any], table_id: str
) -> tuple[dict[str, bool], dict[str, Any], dict[str, list[str]]]:
    """同 _read_checks，再加上每一行勾了哪几家店（开着店铺列时）：勾了任意一家店也算勾上。
    返回 ({record_id: 是否勾上}, {record_id: 原因格子}, {record_id: [店铺 key]})。"""
    columns = candidate_stores.present(ctx, table_id) if ctx is not None else []
    checks: dict[str, bool] = {}
    cells: dict[str, Any] = {}
    picks: dict[str, list[str]] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "page_size": _PAGE,
            "field_names": json.dumps(
                [CHECK_FIELD, REASON_FIELD, *columns], ensure_ascii=False
            ),
        }
        if token:
            params["page_token"] = token
        data = api.call("GET", _records_path(info, table_id), params=params)
        for item in data.get("items") or []:
            values = item.get("fields") or {}
            picked = (
                candidate_stores.picks(ctx, table_id, values) if columns and ctx else []
            )
            # 带 field_names 的列表接口里勾选框可能是文字 "false"（取消过的勾）：只认 true（2026-10-30 前用 bool() 会把它当勾上）
            checks[item["record_id"]] = downloads._ticked(values.get(CHECK_FIELD)) or bool(picked)
            cells[item["record_id"]] = values.get(REASON_FIELD)
            if picked:
                picks[item["record_id"]] = picked
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return checks, cells, picks


def take_lease(ctx: Ctx, busy: str = "候选表正在同步，稍后再点「选款完成」") -> None:
    """要独占表格时（验收、补改名字）：同步正在跑就等它放开，等太久报 busy。"""
    deadline = time.monotonic() + LEASE_WAIT_SECONDS
    while not lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS):
        if time.monotonic() >= deadline:
            raise StageError(busy)
        time.sleep(5)


def _record_ids(ctx: Ctx, seed_id: str) -> set[str]:
    return {
        r["record_id"]
        for r in ctx.db.q(
            "SELECT record_id FROM candidate_rows WHERE seed_id = ? AND record_id IS NOT NULL",
            (seed_id,),
        )
    }


def _note_failure(
    ctx: Ctx, seed_id: str, exc: BaseException, kind: str = KIND_ACCEPT
) -> None:
    """验收 / 提前出套图没成功：对应按钮行的备注写上原因（尽力而为，写不进去不影响报错）。"""
    action = _action(ctx, seed_id, kind)
    info = feishu_table.base(ctx)
    if not action or not action.get("record_id") or not info:
        return
    with contextlib.suppress(*_FAILURES):
        with feishu.Client(ctx) as api:
            _write_action(
                ctx,
                api,
                info,
                action,
                f"{ACTION_LABELS[kind]}没成功（{_local_time(ctx)}）：{exc}"[:200],
            )


def accept(
    ctx: Ctx, seed_id: str, *, by: str, source: str | None = None
) -> dict[str, Any]:
    """选款完成：回读候选表的勾选，勾上的选中、没勾的不要。不打包下载，选中的款由调用方接着交给套图。

    随时能点（2026-10-30 起不等图出完，之后再出的图表名是【有新图】）；勾着的含还在犹豫期里的店铺勾，
    这些款撤销窗口里的记录删掉。没勾的行只有验收前就在表里、进选款也满 FRESH_MINUTES 分钟才算不要；
    验收时才补进表的（含刚补上图的预留行）、刚进选款的多半人还没看到，留到下次（选款待审还开着，表里还是待选），
    记在 fresh。「未生成」「没出成」的预留行没有图，不算。
    结果写进「选款完成」那一行的备注；定下来的图让待审积压降了，被积压挡住的出图马上接着出（不等巡检）。
    source 是从哪提交的（table = 候选表的按钮行），卡片上写明。
    """
    from .stages.job import backlog_count  # 阶段模块依赖表格同步，放到用时再导入

    if not feishu_table.enabled(ctx):
        raise StageError("table.enabled 为 false：候选表没有打开")
    table = table_info(ctx, seed_id)
    if not table:
        raise StageError(f"{seed_id} 还没有候选表：图进选款后会自动建表")
    try:
        take_lease(ctx)
    except StageError as exc:
        _note_failure(ctx, seed_id, exc)
        raise
    limit = int(ctx.setting("batch.review_backlog_limit", 50))
    backlog_before = backlog_count(ctx)
    errors: list[dict[str, str]] = []
    approved: list[str] = []
    rejected: list[str] = []
    already: list[str] = []
    not_in_table: list[str] = []
    fresh: list[str] = []
    try:
        with feishu.Client(ctx) as api:
            info = feishu_table.base(ctx) or {}
            shown = _record_ids(ctx, seed_id)
            # 先把新进选款的图补进表、把聊天框里定了的同步过去
            synced = _sync_locked(ctx, api, info, errors)
            checks, reason_cells, picks = _read_picks(ctx, api, info, table["table_id"])
            fresh_after = iso(utcnow() - dt.timedelta(minutes=FRESH_MINUTES))
            rows = ctx.db.q(
                "SELECT c.asset_id, c.record_id, r.review_id, r.created_at FROM candidate_rows c"
                " JOIN reviews r ON r.subject_type = 'asset_selection'"
                " AND r.subject_id = c.asset_id AND r.status = 'open'"
                " WHERE c.seed_id = ? AND c.record_id IS NOT NULL ORDER BY c.asset_id",
                (seed_id,),
            )
            for row in rows:
                if row["record_id"] not in checks:
                    # 表里找不到这一行（被删了）：不替人决定
                    not_in_table.append(row["asset_id"])
                    continue
                ticked = checks[row["record_id"]]
                if not ticked and (
                    row["record_id"] not in shown or row["created_at"] > fresh_after
                ):
                    fresh.append(row["asset_id"])
                    continue
                decision = "approved" if ticked else "rejected"
                try:
                    review.decide(
                        ctx, row["review_id"], decision, by=by, note=ACCEPT_NOTE
                    )
                except review.ReviewError:
                    already.append(row["asset_id"])
                    continue
                (approved if decision == "approved" else rejected).append(
                    row["asset_id"]
                )
            # 这张表里判了不要的图（含之前定的）有原因的记下来，选完以后才补填的这次也读到
            reason_counts = reasons.save(ctx, seed_id, reason_cells)
            # 犹豫期里的店铺勾这次已经定了：到点不再处理
            candidate_stores.forget_pending(ctx, approved + rejected)
            # 选中的款勾了哪几家店：记下店铺、当场领货号（之后交套图就用货号当编号）
            store_out = candidate_stores.apply(ctx, seed_id, picks, by=by)
            synced["updated"] += _sync_states(ctx, api, info, errors)
            synced["store_cells"] = candidate_stores.sync_cells(ctx, api, info, errors)
            action = (
                _action(ctx, seed_id, KIND_ACCEPT) if control_enabled(ctx) else None
            )
            if action and action.get("record_id"):
                note = _result_note(
                    ctx,
                    {
                        "approved": approved,
                        "rejected": rejected,
                        "fresh": fresh,
                        "already_decided": already,
                    },
                )
                try:
                    _write_action(ctx, api, info, action, note)
                except _FAILURES as exc:
                    errors.append(
                        {"seed_id": seed_id, "error": f"按钮行没写上结果：{exc}"[:300]}
                    )
    except Exception as exc:
        _note_failure(ctx, seed_id, exc)
        raise
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    waiting = int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM reviews r JOIN assets a ON a.asset_id = r.subject_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            " LEFT JOIN candidate_rows c ON c.asset_id = a.asset_id"
            " WHERE r.subject_type = 'asset_selection' AND r.status = 'open'"
            " AND f.seed_id = ? AND c.record_id IS NULL",
            (seed_id,),
        )
        or 0
    )
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE candidate_tables SET accepted_at = ?, updated_at = ? WHERE seed_id = ?",
            (now, now, seed_id),
        )
        log_event(
            conn,
            seed_id,
            "selection_accepted",
            {
                "by": by,
                "approved": len(approved),
                "rejected": len(rejected),
                "already": len(already),
                "not_in_table": len(not_in_table),
                "fresh": len(fresh),
                "reasons": reason_counts,
                "source": source,
                "stores": {a: k for a, k in (store_out.get("chosen") or {}).items()},
            },
        )
    # 选中的款接着出套图；directions 和 scenes 依赖本模块之外的表格与通知，放到用时再导入
    from . import directions, scenes

    scene_out = scenes.after_accept(ctx, seed_id, by=by)
    # 套图入队之后再拉同步：表顶马上出现「排队中」的套图行
    feishu_table.kick(ctx)
    # 待审少了：被积压挡住的出图不用等下一次巡检
    generation = directions.start_runs(ctx) if (approved or rejected) else {}
    return {
        "seed_id": seed_id,
        "approved": approved,
        "rejected": rejected,
        "already_decided": already,
        "not_in_table": not_in_table,
        "fresh": fresh,
        "waiting": waiting,
        "reasons": reason_counts,
        "synced": synced,
        "errors": errors[:5],
        "table_url": table.get("url"),
        "scenes": scene_out,
        "backlog": {
            "before": backlog_before,
            "after": backlog_count(ctx),
            "limit": limit,
        },
        "generation": generation,
        "source": source,
        "stores": store_out,
    }


def _dispatch_scenes(ctx: Ctx, picked: list[str], by: str) -> dict[str, Any]:
    """勾上的选中款里还没出过套图的交给 mooya（出过、在出的跳过；没出成的不自动重出）。"""
    from . import scenes  # 同上，用时再导入

    if not picked:
        return {"enabled": scenes.enabled(ctx), "queued": 0}
    if not scenes.enabled(ctx):
        return {"enabled": False}
    try:
        out = scenes.enqueue(ctx, by=by, asset_ids=picked, retry_failed=False)
    except StageError as exc:
        return {"enabled": True, "error": str(exc)}
    task = scenes.kick(ctx) if out["queued"] else None
    return {
        "enabled": True,
        "queued": len(out["queued"]),
        "skipped": len(out["skipped"]),
        "sync_task": (task or {}).get("task_id"),
    }


def dispatch(
    ctx: Ctx, seed_id: str, *, by: str, source: str | None = None
) -> dict[str, Any]:
    """选中的先出套图：回读候选表的勾选，勾上的、出了图、选款还开着的定为选中，勾上的选中款里
    还没出过套图的交给 mooya；没勾的不动。不算选款完成：不写验收时间、不判没选中，图没出完也能点、能点很多次。
    结果写进「选中的先出套图」那一行的备注。"""
    if not feishu_table.enabled(ctx):
        raise StageError("table.enabled 为 false：候选表没有打开")
    table = table_info(ctx, seed_id)
    if not table:
        raise StageError(f"{seed_id} 还没有候选表：图进选款后会自动建表")
    try:
        take_lease(ctx, "候选表正在同步，稍后再点「选中的先出套图」")
    except StageError as exc:
        _note_failure(ctx, seed_id, exc, kind=KIND_DISPATCH)
        raise
    errors: list[dict[str, str]] = []
    approved: list[str] = []
    already: list[str] = []
    picked: list[str] = []
    try:
        with feishu.Client(ctx) as api:
            info = feishu_table.base(ctx) or {}
            synced = _sync_locked(ctx, api, info, errors)
            checks, _, picks = _read_picks(ctx, api, info, table["table_id"])
            for row in ctx.db.q(
                "SELECT c.asset_id, c.record_id, c.state, r.review_id FROM candidate_rows c"
                " LEFT JOIN reviews r ON r.subject_type = 'asset_selection'"
                " AND r.subject_id = c.asset_id AND r.status = 'open'"
                " WHERE c.seed_id = ? AND c.record_id IS NOT NULL ORDER BY c.asset_id",
                (seed_id,),
            ):
                if not checks.get(row["record_id"]):
                    continue
                if not row["review_id"]:
                    # 已经定了的：选中的照样算勾上（程序给选中的行打了勾），别的不碰
                    if row["state"] == STATE_APPROVED:
                        picked.append(row["asset_id"])
                    continue
                try:
                    review.decide(
                        ctx, row["review_id"], "approved", by=by, note=DISPATCH_NOTE
                    )
                except review.ReviewError:
                    already.append(row["asset_id"])
                    continue
                approved.append(row["asset_id"])
                picked.append(row["asset_id"])
            # 选中的款勾了哪几家店：记下店铺、当场领货号（交套图前要有货号）
            store_out = candidate_stores.apply(ctx, seed_id, picks, by=by)
            synced["updated"] += _sync_states(ctx, api, info, errors)
            synced["store_cells"] = candidate_stores.sync_cells(ctx, api, info, errors)
    except Exception as exc:
        _note_failure(ctx, seed_id, exc, kind=KIND_DISPATCH)
        raise
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    scene_out = _dispatch_scenes(ctx, picked, by)
    result = {
        "seed_id": seed_id,
        "approved": approved,
        "already_decided": already,
        "picked": picked,
        "scenes": scene_out,
        "stores": store_out,
    }
    action = _action(ctx, seed_id, KIND_DISPATCH) if control_enabled(ctx) else None
    if action and action.get("record_id"):
        try:
            with feishu.Client(ctx) as api:
                _write_action(
                    ctx,
                    api,
                    feishu_table.base(ctx) or {},
                    action,
                    _dispatch_note(ctx, result),
                )
        except _FAILURES as exc:
            errors.append(
                {"seed_id": seed_id, "error": f"按钮行没写上结果：{exc}"[:300]}
            )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            seed_id,
            "selection_dispatched",
            {
                "by": by,
                "approved": len(approved),
                "picked": len(picked),
                "queued": scene_out.get("queued", 0),
                "source": source,
            },
        )
    # 套图入队之后再拉同步：表顶马上出现「排队中」的套图行
    feishu_table.kick(ctx)
    from . import directions  # 同上，用时再导入

    # 待审少了：被积压挡住的出图不用等下一次巡检
    generation = directions.start_runs(ctx) if approved else {}
    return {
        **result,
        "synced": synced,
        "errors": errors[:5],
        "table_url": table.get("url"),
        "generation": generation,
        "source": source,
    }


def refresh_reasons(ctx: Ctx, seed_id: str | None = None) -> dict[str, Any]:
    """重读候选表的「没选原因」写进 selection_reasons（一个任务，或全部有候选表的任务）；只读表，不碰勾选。
    返回 {read: {seed_id: {saved, cleared}}, errors}。"""
    if not feishu_table.enabled(ctx):
        raise StageError("table.enabled 为 false：候选表没有打开")
    if seed_id:
        table = table_info(ctx, seed_id)
        if not table:
            raise StageError(f"{seed_id} 还没有候选表：图进选款后会自动建表")
        tables = [table]
    else:
        tables = [
            dict(r) for r in ctx.db.q("SELECT * FROM candidate_tables ORDER BY seed_id")
        ]
    read: dict[str, dict[str, int]] = {}
    errors: list[dict[str, str]] = []
    with feishu.Client(ctx) as api:
        info = feishu_table.base(ctx) or {}
        for table in tables:
            try:
                _, cells = _read_checks(api, info, table["table_id"])
            except _FAILURES as exc:
                errors.append(
                    {
                        "seed_id": table["seed_id"],
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue
            read[table["seed_id"]] = reasons.save(ctx, table["seed_id"], cells)
    return {"read": read, "errors": errors}


# ---------------------------------------------------------------- 清理与状态


def delete_rows(
    ctx: Ctx, asset_ids: list[str], seeds: list[str] | tuple[str, ...] = ()
) -> dict[str, Any]:
    """清理旧数据用：删掉这些图在候选表里的行；整张参考图删掉时连表和（2026-10-20 前验收留下的）压缩包一起删。

    和同步共用租约，拿不到就报错（什么都没删）。飞书或 OSS 删失败的列在 errors 里，库里的记录照样删掉。
    """
    ids = sorted(set(asset_ids))
    rows: list[dict[str, Any]] = []
    for i in range(0, len(ids), _PAGE):
        chunk = ids[i : i + _PAGE]
        marks = ",".join("?" for _ in chunk)
        # 候选图的行（补上图的预留行也是这一行），和这些款在表顶的套图行
        for source in ("candidate_rows", "candidate_scene_rows"):
            rows += [
                dict(r)
                for r in ctx.db.q(
                    f"SELECT asset_id, seed_id, record_id FROM {source} WHERE asset_id IN ({marks})",
                    tuple(chunk),
                )
            ]
    tables = [t for t in (table_info(ctx, s) for s in seeds) if t]
    if not rows and not tables:
        return {"deleted": 0, "tables": 0, "errors": []}
    if not lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS):
        raise StageError("候选表正在同步：等一两分钟再删")
    errors: list[dict[str, str]] = []
    deleted = dropped = 0
    whole = {t["seed_id"] for t in tables}
    try:
        info = feishu_table.base(ctx)
        if info:
            with feishu.Client(ctx) as api:
                for row in rows:
                    table = table_info(ctx, row["seed_id"])
                    if not row["record_id"] or not table or row["seed_id"] in whole:
                        continue
                    try:
                        api.call(
                            "DELETE",
                            f"{_records_path(info, table['table_id'])}/{row['record_id']}",
                        )
                        deleted += 1
                    except _FAILURES as exc:
                        errors.append(
                            {
                                "asset_id": row["asset_id"],
                                "error": f"{type(exc).__name__}: {exc}"[:300],
                            }
                        )
                for table in tables:
                    try:
                        api.call(
                            "DELETE",
                            f"/bitable/v1/apps/{table_registry.app_for(info, table['table_id'])}/tables/{table['table_id']}",
                        )
                        dropped += 1
                    except _FAILURES as exc:
                        errors.append(
                            {
                                "seed_id": table["seed_id"],
                                "error": f"{type(exc).__name__}: {exc}"[:300],
                            }
                        )
        for table in tables:
            if table.get("export_key") and store.oss_enabled(ctx):
                try:
                    store.client(ctx).delete(table["export_key"])
                except _OSS_FAILURES as exc:
                    errors.append(
                        {"seed_id": table["seed_id"], "error": str(exc)[:300]}
                    )
            if table.get("export_path"):
                local = store.resolve(ctx.home, table["export_path"])
                if local.is_file():
                    local.unlink()
        with ctx.db.tx() as conn:
            for row in rows:
                conn.execute(
                    "DELETE FROM candidate_rows WHERE asset_id = ?", (row["asset_id"],)
                )
                conn.execute(
                    "DELETE FROM candidate_scene_rows WHERE asset_id = ?",
                    (row["asset_id"],),
                )
                # 补上了这张图的预留行跟着没了：方向重出时同步再按槽位预留一行
                conn.execute(
                    "DELETE FROM candidate_slots WHERE asset_id = ?", (row["asset_id"],)
                )
            for table in tables:
                for name in (
                    "candidate_rows",
                    "candidate_scene_rows",
                    "candidate_controls",
                    "candidate_actions",
                    "candidate_slots",
                    "candidate_orders",
                    "candidate_tables",
                ):
                    conn.execute(
                        f"DELETE FROM {name} WHERE seed_id = ?", (table["seed_id"],)
                    )
        _forget_orders(ctx, [r["record_id"] for r in rows if r["record_id"]])
    finally:
        lease.drop(ctx, feishu_table.LEASE_KEY)
    return {"deleted": deleted, "tables": dropped, "errors": errors}


def status(ctx: Ctx) -> dict[str, Any]:
    """各任务的候选表：表名、链接（带默认视图）、行数、各状态行数、最近一次验收、表顶的按钮行 / 交付包行 / 套图行、
    预留行、归档后在哪；还有几张表名、列名、默认视图、表顶、排序没跟上，几张要挪。"""
    shelf_rows = shelf.rows(ctx)
    out = []
    for row in ctx.db.q(
        "SELECT t.*, n.table_name, n.table_error FROM candidate_tables t"
        " LEFT JOIN seed_names n ON n.seed_id = t.seed_id ORDER BY t.seed_id"
    ):
        info = dict(row)
        counts = {
            r[0]: r[1]
            for r in ctx.db.q(
                "SELECT state, COUNT(*) FROM candidate_rows WHERE seed_id = ? AND record_id IS NOT NULL"
                " GROUP BY state",
                (info["seed_id"],),
            )
        }
        actions = _actions(ctx, info["seed_id"])
        control = actions.get((KIND_ACCEPT, "")) or {}
        reserved = {
            r[0]: r[1]
            for r in ctx.db.q(
                "SELECT state, COUNT(*) FROM candidate_slots WHERE seed_id = ?"
                " AND record_id IS NOT NULL AND asset_id IS NULL GROUP BY state",
                (info["seed_id"],),
            )
        }
        out.append(
            {
                "seed_id": info["seed_id"],
                "name": info["table_name"] or table_name(info["seed_id"], None),
                "rename_error": info["table_error"],
                "url": info["url"],
                "rows": sum(counts.values()),
                "states": counts,
                "reserved": {
                    STATE_UNMADE: reserved.get(SLOT_UNMADE, 0),
                    STATE_NO_IMAGE: reserved.get(SLOT_NO_IMAGE, 0),
                },
                "accepted_at": info["accepted_at"],
                "control": {
                    "ready": bool(control.get("record_id")),
                    "note": control.get("note"),
                    "triggered_at": control.get("triggered_at"),
                    "triggered_by": control.get("triggered_by"),
                },
                "actions": [
                    {
                        "kind": kind,
                        "batch_id": batch_id or None,
                        "ready": bool(r["record_id"]),
                        "note": r["note"],
                    }
                    for (kind, batch_id), r in sorted(actions.items())
                ],
                "scene_rows": int(
                    ctx.db.scalar(
                        "SELECT COUNT(*) FROM candidate_scene_rows WHERE seed_id = ? AND record_id IS NOT NULL",
                        (info["seed_id"],),
                    )
                    or 0
                ),
                "package_column": package_field_ready(ctx, info["table_id"]),
                "link": table_link(ctx, info["seed_id"]),
                "views": view_ids(ctx, info["table_id"]),
                "shelf": shelf.view(shelf_rows.get(info["seed_id"])),
            }
        )
    failed = ctx.db.q(
        "SELECT asset_id, attempts, last_error FROM candidate_rows WHERE attempts >= ?"
        " ORDER BY asset_id LIMIT 10",
        (feishu_table.MAX_ATTEMPTS,),
    )
    meta = _fields_meta(ctx)
    plan = _top_plan(ctx)
    return {
        "views_pending": len(_views_pending(ctx)),
        "views_error": _views_meta(ctx).get("error"),
        "moves_pending": len(shelf.pending_moves(ctx)),
        "tables": out,
        "pending": len(_pending(ctx)),
        "renames_pending": len(_renames(ctx)),
        "fields_pending": sum(
            1
            for r in ctx.db.q("SELECT table_id FROM candidate_tables")
            if not _fields_current(meta, r["table_id"])
        ),
        "fields_error": meta.get("error"),
        "control_row": control_enabled(ctx),
        "reserve_rows": reserve_enabled(ctx),
        "slots_pending": _slot_work(ctx),
        "top_pending": _top_work(ctx, plan),
        "top_error": (loads(ctx.db.get_meta(ACTIONS_META_KEY), None) or {}).get(
            "error"
        ),
        "orders_pending": sum(len(v) for v in _orders_pending(ctx, plan).values()),
        "orders_error": (loads(ctx.db.get_meta(ORDERS_META_KEY), None) or {}).get(
            "error"
        ),
        "package_columns_pending": len(_package_columns_pending(ctx)),
        "package_columns_error": _package_meta(ctx).get("error"),
        "packages_pending": len(_packages_pending(ctx)),
        "failed": [dict(r) for r in failed],
    }
