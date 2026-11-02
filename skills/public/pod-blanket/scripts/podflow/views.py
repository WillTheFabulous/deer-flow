"""多维表格的默认视图：候选表打开看待选的图，选中的款打开只看待上架；程序给的链接带 view= 直接打开这个视图。

- 候选表（视图版本 6，2026-10-30 用户定）：从左到右是表格「全部」（不筛选，表顶的按钮行、交付包行、套图行和「未生成」
  「没出成」的行也只在这里）、画册「全部画册」（所有有图的原图行：选款结果是待选 / 已选中 / 没选中 / 已修改 / 已清出，
  不放套图行和表顶几行）、画册「已选中」（卡片上有店铺勾和货号）、画册「待选」；四个视图都按隐藏的「排序」列升序排。
  飞书开放接口不能调视图的先后（新建的总在最后），所以已有的画册按表里的先后依次改名、改筛选成这三个，缺的在最后补建
  （封面效果跟着视图走：之前的「看图」/「已选中」变成「全部画册」，之前的「待选」变成「已选中」，新建的是「待选」）；
  之前那一套最后的表格「全部」先删掉、第一个表格改名「全部」（candidates 的 meta candidates.views 记下四个视图的 id 和
  这套设置的版本）。2026-10-30 实测：PATCH 视图时 filter_info 给 null 会被忽略，要给空条件
  {"conjunction": "and", "conditions": []} 才去掉筛选；删掉一个视图后马上把别的视图改成它的名字、改掉名字后马上建同名
  视图都没撞上 1254020（照旧留着重试）。
- 选中的款（视图版本 2，2026-10-30 用户定）：从左到右是表格「全部」（第一个，不筛选，「不上了」的款只在这里）、画册
  「看图」、表格「待上架」（上架状态是待上架、上架失败或为空）、「上架中」「已上架」；「看图」和「待上架」一样筛。新建的表
  建表时就这样建；之前那一套（第一个表格「待上架」+ 画册「看图」+ 表格「已上架」「全部」）在表格同步时就地改：先删掉后面的
  「已上架」「全部」，第一个表格改名「全部」、去掉筛选，「看图」留着（人设的封面效果在），再依次建「待上架」「上架中」
  「已上架」（新建的总在最后，接口调不了先后）。五个 id 记在 feishu_table 表格信息的 views 里。
- 列的先后与画册卡片（Layout，2026-10-27）：表格视图开头放人最常点的几列（勾选框、下载链接），其余列照原来的先后，
  人藏起来的列照旧藏着，人自己加的列跟着它左边那一列走；画册卡片上只放定好的几个字段，封面是缩略图。
- 开关 table.views（缺省 true）；同一个开关也管候选表表名前面的【已选N款】【选款完成】【有新图】。
- 飞书「更新视图」（PATCH /bitable/v1/apps/:app/tables/:table/views/:view，权限 bitable:app）2026-10-25 实测只认这种写法：
  单选「是」的 value 写选项 id 列表的 JSON 字符串，「为空」不带 value；改名和筛选可以一次提交，画册视图也能筛。
- 刚改掉的视图名飞书要过一会儿才放开：紧接着建同名视图会报 1254020 ViewNameDuplicated（2026-10-25 线上 13 张表都碰上），
  所以先筛画册、最后建「全部」，撞上了等几秒再试。
- 排序、可见列、画册封面用 Base v3（/base/v3/bases/:app/tables/:table/views/:view/…，权限 base:view:read /
  base:view:write_only）：PUT …/sort {"sort_config": [{"field": 列名, "desc": false}]}；GET / PUT …/visible_fields
  {"visible_fields": [列名…]}：列表就是看得见的列和先后，没列出的藏起来（和 v1 的 hidden_fields 是同一份设置），
  画册视图里就是卡片上的字段，主键列总在第一个；GET 给的是列名（不是字段 id），PUT 认列名也认字段 id；
  GET / PUT …/card {"cover_field": 附件列 id}：画册封面（给的是字段 id），和卡片上放不放这一列无关。
  2026-10-26 / 10-27 实测：单选「是」的 value 里放两个选项 id 只认第一个，要几个选项就拆成几个「是」用「或」连；
  v1 改筛选 / 隐藏列不会冲掉 v3 的排序，写可见列也不动排序和筛选；画册视图不认 v1 的 hidden_fields（1254019），
  可见列接口认；带 view_id 列记录时按视图排好的顺序返回；写进去的和现在一样不报错；只改先后也马上生效（写完不到一秒
  就读偶尔还是旧的）；设过可见列之后新加的列在表格视图里看得见、排在最后，不会自己上画册卡片；三个写接口都有频率限制
  （800004135，连着写约每秒只放一两次）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from . import feishu, table_registry
from .context import Ctx

# 建视图撞上「名字刚被改掉、还没放开」时，隔这么久再试
NAME_RETRY_SECONDS = (1.0, 2.0, 4.0)
_NAME_DUPLICATED = 1254020
# v3 设排序 / 可见列 / 卡片都有频率限制，同一个错误码（2026-10-26 线上连着设二十来个视图的排序后报 800004135
# OpenAPIUpdateViewSort limited，10-27 连写可见列报 OpenAPISetVisibleFields limited）：等一等再试
V3_RETRY_SECONDS = (2.0, 5.0, 10.0, 20.0)
_V3_LIMITED = 800004135
# 刚建出来的视图 v3 接口过一两秒才查得到（800030501 not_found）：同样等一等再试
_V3_NOT_FOUND = 800030501
_sleep = time.sleep

PENDING_VIEW = "待选"
ALL_VIEW = "全部"
GALLERY_VIEW = "看图"
PICKED_VIEW = "已选中"
ALL_GALLERY_VIEW = "全部画册"
# 候选表的三个画册，按表里从左到右的先后（视图版本 6）；认作候选表画册的名字还有之前那一套的「看图」
CANDIDATE_BOARDS = (ALL_GALLERY_VIEW, PICKED_VIEW, PENDING_VIEW)
_BOARD_NAMES = frozenset({GALLERY_VIEW, *CANDIDATE_BOARDS})
# 去掉视图筛选（filter_info 给 null 飞书不理）
NO_FILTER: dict[str, Any] = {"conjunction": "and", "conditions": []}
TO_LIST_VIEW = "待上架"
IN_PROGRESS_VIEW = "上架中"
LISTED_VIEW = "已上架"


def enabled(ctx: Ctx) -> bool:
    from . import feishu_table  # 选款表模块依赖本模块，放到用时再导入

    return feishu_table.enabled(ctx) and bool(ctx.setting("table.views", True))


def _views_path(info: dict[str, Any], table_id: str) -> str:
    return f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/views"


def _paged(api: feishu.Client, path: str) -> list[dict[str, Any]]:
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


def list_views(
    api: feishu.Client, info: dict[str, Any], table_id: str
) -> list[dict[str, Any]]:
    return _paged(api, _views_path(info, table_id))


def list_fields(
    api: feishu.Client, info: dict[str, Any], table_id: str
) -> list[dict[str, Any]]:
    return _paged(
        api,
        f"/bitable/v1/apps/{table_registry.app_for(info, table_id)}/tables/{table_id}/fields",
    )


def _field(fields: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = next((f for f in fields if f.get("field_name") == name), None)
    if not found:
        raise KeyError(f"表里没有「{name}」列")
    return found


def _option(field: dict[str, Any], name: str) -> str:
    for option in (field.get("property") or {}).get("options") or []:
        if option.get("name") == name and option.get("id"):
            return str(option["id"])
    raise KeyError(f"「{field.get('field_name')}」列没有选项「{name}」")


def any_or_empty(field: dict[str, Any], options: list[str]) -> dict[str, Any]:
    """单选是这几个选项之一或为空：每个选项一个「是」（一个「是」里放几个 id 只认第一个），用「或」连。"""
    conditions: list[dict[str, Any]] = [
        {
            "field_id": field["field_id"],
            "operator": "is",
            "value": json.dumps([_option(field, option)]),
        }
        for option in options
    ]
    conditions.append({"field_id": field["field_id"], "operator": "isEmpty"})
    return {"conjunction": "or", "conditions": conditions}


def is_or_empty(field: dict[str, Any], option: str) -> dict[str, Any]:
    """单选「是 option」或为空。"""
    return any_or_empty(field, [option])


def is_option(field: dict[str, Any], option: str) -> dict[str, Any]:
    """单选「是 option」。"""
    return {
        "conjunction": "and",
        "conditions": [
            {
                "field_id": field["field_id"],
                "operator": "is",
                "value": json.dumps([_option(field, option)]),
            }
        ],
    }


def any_option(field: dict[str, Any], options: list[str]) -> dict[str, Any]:
    """单选是这几个选项之一（不含为空）：每个选项一个「是」，用「或」连；只有一个时同 is_option。"""
    if len(options) == 1:
        return is_option(field, options[0])
    return {
        "conjunction": "or",
        "conditions": [
            {
                "field_id": field["field_id"],
                "operator": "is",
                "value": json.dumps([_option(field, option)]),
            }
            for option in options
        ],
    }


def _patch(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    view_id: str,
    *,
    name: str | None = None,
    filter_info: dict[str, Any] | None = None,
) -> None:
    """改名 / 改筛选（filter_info 给 NO_FILTER 是去掉筛选）；改名撞上「名字刚被改掉、还没放开」时等几秒再试。"""
    body: dict[str, Any] = {}
    if name:
        body["view_name"] = name
    if filter_info:
        body["property"] = {"filter_info": filter_info}
    if not body:
        return
    delays = iter(NAME_RETRY_SECONDS)
    while True:
        try:
            api.call("PATCH", f"{_views_path(info, table_id)}/{view_id}", json=body)
            return
        except feishu.FeishuError as exc:
            delay = next(delays, None)
            if not name or exc.code != _NAME_DUPLICATED or delay is None:
                raise
            _sleep(delay)


def _delete(
    api: feishu.Client, info: dict[str, Any], table_id: str, view_id: str
) -> None:
    api.call("DELETE", f"{_views_path(info, table_id)}/{view_id}")


def _v3_view(info: dict[str, Any], table_id: str, view_id: str) -> str:
    return f"/base/v3/bases/{table_registry.app_for(info, table_id)}/tables/{table_id}/views/{view_id}"


def _v3_call(
    api: feishu.Client, method: str, path: str, body: dict[str, Any] | None = None
) -> Any:
    """Base v3 读 / 改视图设置（排序 / 可见列 / 卡片）；撞上频率限制、或视图刚建还查不到时等一等再试。"""
    delays = iter(V3_RETRY_SECONDS)
    while True:
        try:
            if body is None:
                return api.call(method, path)
            return api.call(method, path, json=body)
        except feishu.FeishuError as exc:
            delay = next(delays, None)
            if exc.code not in (_V3_LIMITED, _V3_NOT_FOUND) or delay is None:
                raise
            _sleep(delay)


def _v3_put(api: feishu.Client, path: str, body: dict[str, Any]) -> Any:
    return _v3_call(api, "PUT", path, body)


def set_sort(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    view_id: str,
    field_name: str,
) -> None:
    """视图按这一列升序排（覆盖视图原来的排序）。"""
    _v3_put(
        api,
        f"{_v3_view(info, table_id, view_id)}/sort",
        {"sort_config": [{"field": field_name, "desc": False}]},
    )


@dataclass(frozen=True)
class Layout:
    """视图里列的先后与画册卡片。

    front：表格视图开头的几列（列名，按这个先后）；其余看得见的列照原来的先后，人藏起来的列照旧藏着。
    known：程序建的列名；不在里面的是人自己加的列，跟着它左边那一列走。
    hidden：表格视图里藏起来的列（「排序」）。
    gallery / cover：画册卡片上的字段（主键列总在第一个）和封面（附件列）；gallery 为 None 时画册不动。
    picked / whole：候选表画册「已选中」「全部画册」的卡片字段（None = 同 gallery，gallery 是画册「待选」的）。
    """

    front: tuple[str, ...]
    known: frozenset[str]
    hidden: tuple[str, ...] = ()
    gallery: tuple[str, ...] | None = None
    cover: str | None = None
    picked: tuple[str, ...] | None = None
    whole: tuple[str, ...] | None = None

    def boards(self) -> tuple[tuple[str, ...] | None, ...]:
        """候选表三个画册（CANDIDATE_BOARDS 的先后）各自的卡片字段。"""
        return (
            self.whole if self.whole is not None else self.gallery,
            self.picked if self.picked is not None else self.gallery,
            self.gallery,
        )


def arrange(current: list[str], layout: Layout) -> list[str]:
    """表格视图新的可见列（列名，按先后）。current 是现在看得见的列：人加的列（不在 known 里）和它左边那一列算一组，
    front 里的组按 front 的先后排在最前，其余组照原来的先后；hidden 里的列去掉。"""
    groups: list[list[str]] = []
    for name in current:
        if name in layout.hidden:
            continue
        if groups and name not in layout.known:
            groups[-1].append(name)
        else:
            groups.append([name])
    rank = {name: i for i, name in enumerate(layout.front)}
    ahead = sorted((g for g in groups if g[0] in rank), key=lambda g: rank[g[0]])
    rest = [g for g in groups if g[0] not in rank]
    return [name for group in (*ahead, *rest) for name in group]


def card_fields(
    current: list[str],
    present: set[str],
    layout: Layout,
    cards: tuple[str, ...] | None = None,
) -> list[str]:
    """画册卡片上的字段（列名）：cards（缺省 layout.gallery）里表中有的（present 是表里全部列名，按 cards 的先后），
    再加上人自己放到卡片上的列。"""
    wanted = [
        name for name in (layout.gallery if cards is None else cards) or () if name in present
    ]
    return wanted + [
        name for name in current if name not in layout.known and name not in wanted
    ]


def visible_fields(
    api: feishu.Client, info: dict[str, Any], table_id: str, view_id: str
) -> list[str]:
    """视图现在看得见的列（列名，按先后；画册是卡片上的字段）。"""
    data = _v3_call(api, "GET", f"{_v3_view(info, table_id, view_id)}/visible_fields")
    # 接口的 data 就是列名列表（不是字段 id），空列表被 feishu.Client 换成了 {}
    return list(data) if isinstance(data, list) else []


def set_visible_fields(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    view_id: str,
    names: list[str],
) -> None:
    """视图里看得见哪些列（列名）、什么先后（没列出的藏起来；画册视图是卡片上的字段）。"""
    _v3_put(
        api,
        f"{_v3_view(info, table_id, view_id)}/visible_fields",
        {"visible_fields": names},
    )


def set_cover(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    view_id: str,
    field_id: str,
) -> bool:
    """画册封面用这一列（附件列）；已经是就不写。返回写了没有。"""
    path = f"{_v3_view(info, table_id, view_id)}/card"
    if (_v3_call(api, "GET", path) or {}).get("cover_field") == field_id:
        return False
    _v3_put(api, path, {"cover_field": field_id})
    return True


def apply_layout(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    layout: Layout,
    *,
    grids: tuple[str, ...],
    gallery: str | None = None,
    galleries: dict[str, tuple[str, ...] | None] | None = None,
    fields: list[dict[str, Any]] | None = None,
) -> int:
    """按 layout 摆这几个表格视图的列，以及画册的卡片字段和封面：gallery 用 layout.gallery，galleries 是
    {画册 id: 卡片字段}（候选表的「全部画册」「已选中」「待选」）；和现在一样的不写。fields 是表里的列（刚列过就传进来，
    省一次调用）。返回写了几处。"""
    if fields is None:
        fields = list_fields(api, info, table_id)
    present = {f["field_name"] for f in fields}
    boards = {k: v for k, v in (galleries or {}).items() if v is not None}
    if gallery and layout.gallery is not None:
        boards.setdefault(gallery, layout.gallery)
    views: list[tuple[str, tuple[str, ...] | None]] = [(v, None) for v in grids]
    views += list(boards.items())
    written = 0
    for view_id, cards in views:
        current = visible_fields(api, info, table_id, view_id)
        wanted = (
            card_fields(current, present, layout, cards)
            if cards is not None
            else arrange(current, layout)
        )
        if wanted != current:
            set_visible_fields(api, info, table_id, view_id, wanted)
            written += 1
    # 卡片接口的封面给的是字段 id
    cover = next(
        (f["field_id"] for f in fields if f["field_name"] == layout.cover), None
    )
    for view_id in boards:
        if cover and set_cover(api, info, table_id, view_id, cover):
            written += 1
    return written


def _create(
    api: feishu.Client, info: dict[str, Any], table_id: str, name: str, view_type: str
) -> str:
    delays = iter(NAME_RETRY_SECONDS)
    while True:
        try:
            data = api.call(
                "POST",
                _views_path(info, table_id),
                json={"view_name": name, "view_type": view_type},
            )
            return str((data.get("view") or {})["view_id"])
        except feishu.FeishuError as exc:
            delay = next(delays, None)
            if exc.code != _NAME_DUPLICATED or delay is None:
                raise
            _sleep(delay)


def _first_grid(views: list[dict[str, Any]]) -> dict[str, Any]:
    found = next((v for v in views if v.get("view_type") == "grid"), None)
    if not found:
        raise KeyError("表里没有表格视图")
    return found


def _rename(view: dict[str, Any], name: str) -> str | None:
    """已经叫这个名字就不改名（重试、新表建表时就叫这个名字）。"""
    return None if view.get("view_name") == name else name


def _named(
    views: list[dict[str, Any]], view_type: str, name: str, skip: str | None = None
) -> str | None:
    return next(
        (
            v["view_id"]
            for v in views
            if v.get("view_type") == view_type
            and v.get("view_name") == name
            and v["view_id"] != skip
        ),
        None,
    )


def _gallery(views: list[dict[str, Any]]) -> str | None:
    """「看图」画册视图；没有叫这个名字的就用第一个画册视图。"""
    return _named(views, "gallery", GALLERY_VIEW) or next(
        (v["view_id"] for v in views if v.get("view_type") == "gallery"), None
    )


def _candidate_boards(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选表里程序管的画册（按表里的先后）：叫「看图」「全部画册」「已选中」「待选」的；一个都没有时用第一个画册
    （关着默认视图时建的「看图」被人改过名）。"""
    boards = [v for v in views if v.get("view_type") == "gallery"]
    named = [v for v in boards if v.get("view_name") in _BOARD_NAMES]
    return named or boards[:1]


def _rename_boards(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    plan: list[tuple[dict[str, Any], str]],
    filters: dict[str, dict[str, Any]],
) -> None:
    """已有的画册按 plan [(视图, 目标名字)] 的先后依次改名、改成 filters 里那个名字的筛选：前一个让出的名字正好给
    后一个（之前的「已选中」改成「全部画册」，之前的「待选」改成「已选中」）。目标名字被排在后面、还没改的画册占着时
    （人在飞书里拖过视图的先后）依次改不过去：先都改成临时名字，再改成目标名字。"""
    held = {view.get("view_name"): i for i, (view, _name) in enumerate(plan)}
    if any(held.get(name, -1) > i for i, (_view, name) in enumerate(plan)):
        for i, (view, name) in enumerate(plan):
            if view.get("view_name") != name:
                temp = f"{name}（调整中 {i + 1}）"
                _patch(api, info, table_id, view["view_id"], name=temp)
                view["view_name"] = temp
    for view, name in plan:
        _patch(
            api,
            info,
            table_id,
            view["view_id"],
            name=_rename(view, name),
            filter_info=filters[name],
        )
        view["view_name"] = name


def ensure_candidate_views(
    api: feishu.Client,
    info: dict[str, Any],
    table_id: str,
    *,
    state_field: str,
    all_options: list[str],
    picked_option: str,
    pending_option: str,
    layout: Layout,
    order_field: str | None = None,
    resort: bool = True,
) -> dict[str, str]:
    """候选表四个视图（视图版本 6）：第一个表格视图「全部」（去掉筛选），画册「全部画册」（选款结果是 all_options 之一）、
    「已选中」（picked_option）、「待选」（pending_option），按这个先后排：已有的画册按表里的先后依次分给这三个名字、
    就地改名改筛选，缺的在最后补建（见模块说明）；之前那一套最后的表格「全部」先删掉。给了 order_field 时新建的视图都按
    它升序排，resort 为真时四个都重设（已经按「排序」排好的不用）；最后按 layout 摆「全部」的列（「排序」列由
    layout.hidden 藏起来）、三个画册的卡片（layout.boards()）和封面。返回 {all, all_gallery, picked, pending}。
    可以重复执行，做到一半失败的下一次照同样的分配接着做。"""
    fields = list_fields(api, info, table_id)
    state = _field(fields, state_field)
    views = list_views(api, info, table_id)
    grid = _first_grid(views)
    first = grid["view_id"]
    # 之前那一套最后的表格「全部」：第一个表格要叫这个名字，先删掉它
    for view in views:
        if (
            view["view_id"] != first
            and view.get("view_type") == "grid"
            and view.get("view_name") == ALL_VIEW
        ):
            _delete(api, info, table_id, view["view_id"])
    _patch(
        api, info, table_id, first, name=_rename(grid, ALL_VIEW), filter_info=NO_FILTER
    )
    filters = {
        ALL_GALLERY_VIEW: any_option(state, all_options),
        PICKED_VIEW: is_option(state, picked_option),
        PENDING_VIEW: is_option(state, pending_option),
    }
    plan = list(zip(_candidate_boards(views), CANDIDATE_BOARDS))
    _rename_boards(api, info, table_id, plan, filters)
    ids = {name: view["view_id"] for view, name in plan}
    created: set[str] = set()
    for name in CANDIDATE_BOARDS[len(plan) :]:
        ids[name] = _create(api, info, table_id, name, "gallery")
        created.add(ids[name])
        _patch(api, info, table_id, ids[name], filter_info=filters[name])
    if order_field:
        for view_id in (first, *(ids[name] for name in CANDIDATE_BOARDS)):
            if resort or view_id in created:
                set_sort(api, info, table_id, view_id, order_field)
    apply_layout(
        api,
        info,
        table_id,
        layout,
        grids=(first,),
        galleries=dict(zip((ids[name] for name in CANDIDATE_BOARDS), layout.boards())),
        fields=fields,
    )
    return {
        "all": first,
        "all_gallery": ids[ALL_GALLERY_VIEW],
        "picked": ids[PICKED_VIEW],
        "pending": ids[PENDING_VIEW],
    }


# 「选中的款」之前那一套里排在画册后面的两个表格视图：升级时删掉，三个状态视图要排在画册后面重建
_OLD_SELECTED_GRIDS = (LISTED_VIEW, ALL_VIEW)


def ensure_selected_views(
    api: feishu.Client,
    info: dict[str, Any],
    *,
    state_field: str,
    pending_options: list[str],
    in_progress_option: str,
    listed_option: str,
    layout: Layout,
    order_field: str | None = None,
    resort: bool = True,
) -> dict[str, str]:
    """「选中的款」五个视图（视图版本 2）：第一个表格视图「全部」（去掉筛选）、画册「看图」、表格「待上架」（上架状态是
    pending_options 之一或为空，「看图」也这样筛）、「上架中」（in_progress_option）、「已上架」（listed_option），按这个
    先后排；之前那一套就地改（见模块说明）。三个选项表里都要有，没有的报 KeyError（什么都还没改）。给了 order_field 时
    三个状态视图和新建的画册都按它升序排，resort 为真时五个都重设（之前那一套已经排好的不用）；最后按 layout 摆四个表格
    视图的列（「排序」列由 layout.hidden 藏起来），画册的卡片不动。返回 {all, gallery, pending, in_progress, listed}。
    可以重复执行，做到一半失败的下一次接着做。"""
    table_id = info["table_id"]
    fields = list_fields(api, info, table_id)
    state = _field(fields, state_field)
    filters = {
        TO_LIST_VIEW: any_or_empty(state, pending_options),
        IN_PROGRESS_VIEW: is_option(state, in_progress_option),
        LISTED_VIEW: is_option(state, listed_option),
    }
    views = list_views(api, info, table_id)
    grid = _first_grid(views)
    first = grid["view_id"]
    if grid.get("view_name") != ALL_VIEW:
        # 之前那一套：先删掉后面的「已上架」「全部」（删在第一个表格改名之前，做到一半失败的下一次还认得出来）
        stale = [
            v
            for v in views
            if v["view_id"] != first
            and v.get("view_type") == "grid"
            and v.get("view_name") in _OLD_SELECTED_GRIDS
        ]
        for view in stale:
            _delete(api, info, table_id, view["view_id"])
        views = [v for v in views if v not in stale]
    _patch(
        api, info, table_id, first, name=_rename(grid, ALL_VIEW), filter_info=NO_FILTER
    )
    created: set[str] = set()
    gallery = _gallery(views)
    if not gallery:
        gallery = _create(api, info, table_id, GALLERY_VIEW, "gallery")
        created.add(gallery)
    _patch(api, info, table_id, gallery, filter_info=filters[TO_LIST_VIEW])
    ids = {"all": first, "gallery": gallery}
    for key, name in (
        ("pending", TO_LIST_VIEW),
        ("in_progress", IN_PROGRESS_VIEW),
        ("listed", LISTED_VIEW),
    ):
        view_id = _named(views, "grid", name, skip=first)
        if not view_id:
            view_id = _create(api, info, table_id, name, "grid")
        # 三个状态视图都当新的排（上一次建了、没来得及设排序的也补上）
        created.add(view_id)
        _patch(api, info, table_id, view_id, filter_info=filters[name])
        ids[key] = view_id
    if order_field:
        for view_id in ids.values():
            if resort or view_id in created:
                set_sort(api, info, table_id, view_id, order_field)
    apply_layout(
        api,
        info,
        table_id,
        layout,
        grids=(first, ids["pending"], ids["in_progress"], ids["listed"]),
        fields=fields,
    )
    return ids


def link(
    url: str | None, *, table_id: str | None = None, view_id: str | None = None
) -> str | None:
    """表格链接加上 table= / view=（已经带着 table= 时只补 view=）。"""
    if not url:
        return url
    parts = []
    if table_id and "table=" not in url:
        parts.append(f"table={table_id}")
    if view_id:
        parts.append(f"view={view_id}")
    if not parts:
        return url
    return url + ("&" if "?" in url else "?") + "&".join(parts)
