"""飞书 POD 工作台（pod-blanket 设计流水线）：卡片渲染、按钮回调与定时巡检，由 ForkFeishuChannel 混入。

- 视图数据全部来自引擎（``podctl ui ...``，见 pod_bridge），这里只负责画卡片：缩略图上传换 image_key，进程内缓存。
- 按钮回调先回 toast，再在主循环里调 podctl、原地 patch 原卡片：一次 podctl 约 0.5 秒，加上传图会超出飞书 3 秒的回调时限。
- 裂变策划 / 出图这类长任务只让引擎转后台（``--detach --notify``），跑完由引擎发完成卡片。
- 裂变方向挑选卡（``podctl ui directions``）：待挑选时是一张勾选清单（根节点表单，每个方向一个勾选框，勾选只在飞书本地生效），
  点「按勾选的方向出图」一次提交，form_value 里勾上的 ``pick_V…`` 交给 ``directions confirm --pick``；确认后引擎在后台
  一直出到这些方向全部出完。已经发出去的旧卡片（「选这个」/ 不带表单的确认按钮）照旧可用。
- 任务列表卡（``podctl ui tasks``）：一张参考图一个任务，五步进度（拆解 / 策划 / 挑选 / 出图 / 选款，开着套图时再加「套图」）
  + 现在在做什么 + 按状态给的按钮；已完成的任务带「这次想要的方向 + 继续裂变」表单，提交后（``pod_plan`` 带 ``back=tasks``）
  回到任务列表。「出套图」（``pod_scenes``）把选中了还没出的款交给 mooya 出场景图，引擎拉起后台、出完发卡片。
  有待选时：图还没出完给「选中的先出套图」（``pod_dispatch`` → ``table dispatch``，勾上的先出套图、不算选款完成），
  全部出完才给「选款完成」（``pod_accept`` → ``table accept``）。每个打好、还没标的交付包有「B… 已下载」
  （``pod_downloaded`` → ``scenes downloaded``，任务列表、历史任务卡（带 ``back=shelf``）、任务明细、套图出完卡都有），
  标完满足条件时引擎当场归档；撤销只在 POD 对话里说。
- 任务明细卡（``podctl ui history``，原方向记录卡）：这张参考图做过和提过的方向、每个方向细到哪一步，
  底部表单可以写一句这次想要的方向（``hint_F…``，可不填）再策划一轮。
- 任务归档：任务列表上做完的任务有「归档」（``pod_archive`` → ``shelf archive``），收起后在历史任务卡
  （``podctl ui shelf``，``pod_shelf``）里：顶部搜索框（``shelf_q``，名字 / 编号 / 日期），每个任务带「调出」
  （``pod_restore`` → ``shelf restore``，带 ``back=shelf`` 时调出后回到历史任务卡）和「继续裂变」表单（引擎自动调出）。
- 选款表（飞书多维表格，引擎同步）建好后，工作台固定按钮里多一个「选款表」、已选款卡（``podctl ui gallery``）底部多一个
  「打开选款表」，都是带 ``url`` 的链接按钮（不走回调，``_table_link`` 只认 https）。
- 待审项可以带意见表单（选款的「修改」）：表单容器提交时回调带 form_value，意见原样交给 ``review decide --note``；
  也可以带跳转按钮（links，如裂变方向的「去挑选」），参数原样回传。
- 进入 POD 对话：切到本单聊里最近的 pod-blanket 会话，没有就以该人设新建；进入前记下当前会话，「返回原会话」切回。
- 只在单聊、已绑定时可用（审批会改业务库，群里谁都能点的卡片不放行）。
- 定时巡检：渠道启动后每分钟调一次 ``podctl tick --auto``，由引擎按 schedule 配置决定是否到点。
- 多维表格记录变更事件（``drive.file.bitable_record_changed_v1``，引擎订阅了选款表）：只留「改成勾上」的修改，
  按数据表防抖 2 秒合并后交给 ``podctl table control``，是不是候选表表顶的按钮行 / 交付包行由引擎判断。
  能改表格的人本来就决定着勾选，这里不查绑定；操作人记 ``feishu:<open_id>``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.channels.fork.manager import CARD_ACTION_SOURCE, session_display_title
from app.channels.fork.pod_bridge import PodBridge, PodctlError, podctl_path
from app.channels.message_bus import OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)

POD_PERSONA = "pod-blanket"
POD_PAGE_SIZE = 3
POD_CARD_KIND = "pod"
POD_CARD_ACTIONS = frozenset(
    {
        "pod_dashboard",
        "pod_reviews",
        "pod_decide",
        "pod_tick",
        "pod_pause",
        "pod_resume",
        "pod_daily",
        "pod_enter_chat",
        "pod_return_chat",
        "pod_plan",
        "pod_worker",
        "pod_trial_start",
        "pod_trial_stop",
        "pod_timeline",
        "pod_gallery",
        "pod_send_originals",
        "pod_plan_view",
        "pod_plan_pick",
        "pod_plan_pick_all",
        "pod_plan_confirm",
        "pod_plan_reject",
        "pod_history",
        "pod_tasks",
        "pod_accept",
        "pod_dispatch",
        "pod_scenes",
        "pod_downloaded",
        "pod_shelf",
        "pod_archive",
        "pod_restore",
    }
)
POD_TASK_PAGE_SIZE = 4
POD_SHELF_PAGE_SIZE = 4
POD_TIMER_INTERVAL_SECONDS = 60
POD_TIMER_MAX_BACKOFF_SECONDS = 1800
# 同一张数据表的记录变更攒这么久再交给引擎：人连着勾一串图时只起一次 podctl
POD_BITABLE_DEBOUNCE_SECONDS = 2.0
BACKGROUND_NOTICE = "已在后台开始，完成后会通知你"

_BUTTONS_PER_ROW = 4
_IMAGE_KEY_CACHE_LIMIT = 256
_TEMPLATES = {"alert": "red", "paused": "orange", "normal": "blue"}
_REVIEW_ID_RE = re.compile(r"^RV\d+$")
_DECISION_RE = re.compile(r"^[a-z_]{1,32}$")
_FIELD_RE = re.compile(r"^note_RV\d+$")
_TYPE_RE = re.compile(r"^[a-z_]{1,32}$")
_FAMILY_RE = re.compile(r"^F\d+$")
_ASSET_RE = re.compile(r"^IMG\d+$")
_BATCH_RE = re.compile(r"^B\d+$")
_VARIANT_RE = re.compile(r"^V\d+$")
_SEED_RE = re.compile(r"^S\d+$")
# 多维表格事件里的 token / 数据表 id / 记录 id，和操作人的 open_id（与引擎 table control 的校验一致）
_BITABLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")
# 挑选卡表单的勾选框 pick_V0012、任务列表 / 任务明细卡表单的输入框 hint_F01（字段名由引擎的视图给出）
_PICK_FIELD_RE = re.compile(r"^pick_(V\d+)$")
_HINT_FIELD_RE = re.compile(r"^hint_F\d+$")
# 策划按钮带 back=tasks 时，提交后回到任务列表（只认这一个值）
_BACK_TASKS = "tasks"
# 调出按钮带 back=shelf 时，调出后回到历史任务卡（只认这一个值）
_BACK_SHELF = "shelf"
# 与引擎 plan --hint 的上限一致
_HINT_MAX_CHARS = 100
# 历史任务卡的搜索框：字段名与上限和引擎 ui.SHELF_QUERY_FIELD / SHELF_QUERY_CHARS 一致
_SHELF_QUERY_FIELD = "shelf_q"
_SHELF_QUERY_MAX_CHARS = 20
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# 飞书文件消息上限 30MB
_ORIGINAL_MAX_BYTES = 30 * 1024 * 1024
P2P_ONLY_TEXT = "POD 工作台只在和我的单聊里使用。"

# 进程内只跑一个定时器：多个飞书连接各自 start 时只有第一个生效
_timer_owner: object | None = None


# -- 卡片构建（纯函数）--------------------------------------------------------


def _toast(kind: str, content: str) -> dict[str, str]:
    return {"type": kind, "content": content}


def _value(action: str, **fields: Any) -> dict[str, Any]:
    return {"action": action, **{k: v for k, v in fields.items() if v is not None}}


def _button(label: str, value: dict[str, Any], *, kind: str = "default") -> dict[str, Any]:
    return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind, "value": value}


def _link_button(label: str, url: str, *, kind: str = "default") -> dict[str, Any]:
    """点了直接打开链接的按钮（不走回调）。"""
    return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind, "url": url}


def _table_link(url: Any, label: str, *, kind: str = "default") -> dict[str, Any] | None:
    """选款表（飞书多维表格）的链接按钮；引擎没给链接或不是 https 时不画。"""
    if isinstance(url, str) and url.startswith("https://"):
        return _link_button(label, url, kind=kind)
    return None


def _button_rows(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"tag": "action", "actions": buttons[i : i + _BUTTONS_PER_ROW]} for i in range(0, len(buttons), _BUTTONS_PER_ROW)]


def _markdown(text: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": text}


def _note(text: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _card(title: str, elements: list[dict[str, Any]], *, template: str = "blue") -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def _img(image_key: str, label: str) -> dict[str, Any]:
    return {"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": label}, "mode": "fit_horizontal", "preview": True}


def _column(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top", "elements": elements}


def _image_row(images: list[tuple[str, str]]) -> dict[str, Any]:
    """几张图并排，图下写标签；顺序就是引擎给的顺序。"""
    columns = [_column([_img(key, label), _markdown(label)]) for key, label in images]
    return {"tag": "column_set", "flex_mode": "bisect", "background_style": "default", "columns": columns}


def _media_block(text: str, images: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if len(images) == 1:
        # 单张图放左栏、文字放右栏，卡片不会被竖图撑得太长
        return [{"tag": "column_set", "flex_mode": "none", "background_style": "default", "columns": [_column([_img(*images[0])]), _column([_markdown(text)])]}]
    elements = [_markdown(text)]
    if images:
        elements.append(_image_row(images))
    return elements


def _step_button(action: dict[str, Any]) -> dict[str, Any]:
    """引擎给的下一步按钮：label / style 用来画，其余字段原样作为回调参数。"""
    params = {k: v for k, v in action.items() if k not in ("action", "label", "style")}
    return _button(action["label"], _value(action["action"], **params), kind=action.get("style") or "default")


def build_pod_dashboard_card(view: dict[str, Any], *, notice: str | None = None) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append(_markdown(notice))
    if view.get("progress"):
        elements.append(_markdown("**进度**\n" + "\n".join(f"- {line}" for line in view["progress"])))
    steps = view.get("next_steps") or []
    if steps:
        elements.append(_markdown("**下一步**"))
        for i, step in enumerate(steps, 1):
            elements.append(_markdown(f"{i}. {step['text']}"))
            elements.extend(_button_rows([_step_button(a) for a in step.get("actions") or []]))
        elements.append({"tag": "hr"})
    elements.append(_markdown("\n".join(view["lines"])))
    if view.get("alerts"):
        elements.append(_markdown("**异常**\n" + "\n".join(f"- {a}" for a in view["alerts"])))
    pending = int(view.get("open_reviews") or 0)
    paused = bool(view.get("paused"))
    trial = bool(view.get("trial_active"))
    buttons = [
        _button(f"待审（{pending}）", _value("pod_reviews"), kind="primary" if pending else "default"),
        _button("任务列表", _value("pod_tasks")),
        _button("刷新", _value("pod_dashboard")),
        _button("巡检一次", _value("pod_tick")),
        _button("恢复" if paused else "暂停", _value("pod_resume" if paused else "pod_pause"), kind="primary" if paused else "danger"),
        _button("最近动态", _value("pod_timeline")),
        _button("已选款", _value("pod_gallery")),
        _table_link(view.get("table_url"), "选款表"),
        _button("日报", _value("pod_daily")),
        _button("停止连续运行" if trial else "开启连续运行", _value("pod_trial_stop" if trial else "pod_trial_start")),
        _button("进入 POD 对话", _value("pod_enter_chat"), kind="primary"),
        _button("返回原会话", _value("pod_return_chat")),
    ]
    elements.extend(_button_rows([b for b in buttons if b]))
    if view.get("notes"):
        elements.append(_note("；".join(view["notes"])))
    return _card(view.get("title") or "POD 毛毯工作台", elements, template=_TEMPLATES.get(view.get("level", "normal"), "blue"))


def _review_elements(item: dict[str, Any], image_keys: dict[str, str], *, offset: int, review_type: str | None) -> list[dict[str, Any]]:
    title, lines = f"**{item['title']}**", item.get("lines", [])
    images = [(image_keys[img["path"]], img["label"]) for img in item.get("images", []) if image_keys.get(img["path"])]
    elements: list[dict[str, Any]] = [{"tag": "hr"}]
    if len(images) > 1:
        # 选款：标题下先并排看图（原图在前），再读方向、模型等说明
        elements += [_markdown(title), _image_row(images)]
        if lines:
            elements.append(_markdown("\n".join(lines)))
    else:
        elements += _media_block("\n".join([title, *lines]), images)
    buttons = [_button(action["label"], _value("pod_decide", review_id=item["review_id"], decision=action["decision"], offset=offset, type=review_type), kind=action.get("style", "default")) for action in item.get("actions", [])]
    buttons += [_step_button(link) for link in item.get("links") or []]
    elements.extend(_button_rows(buttons))
    if item.get("form"):
        elements.append(_review_form(item["form"], item["review_id"], offset=offset, review_type=review_type))
    if item.get("hint"):
        elements.append(_note(item["hint"]))
    return elements


def _review_form(form: dict[str, Any], review_id: str, *, offset: int, review_type: str | None) -> dict[str, Any]:
    """待审项里的意见表单（选款的「修改」）：卡片 JSON 1.0 的表单容器只能放在卡片根节点，提交时回调带 form_value。

    输入框要飞书 V6.8+、表单容器要 V6.6+；更老的客户端显示 fallback，改去 POD 对话里说。
    """
    fallback = {"tag": "fallback_text", "text": {"tag": "plain_text", "content": f"飞书版本太低看不到输入框：在 POD 对话里说「{review_id} 需要修改：……」"}}
    field: dict[str, Any] = {
        "tag": "input",
        "name": form["input"],
        "required": True,
        "input_type": "multiline_text",
        "rows": 2,
        "auto_resize": True,
        "max_length": int(form.get("max_length") or 300),
        "width": "fill",
        "placeholder": {"tag": "plain_text", "content": form.get("placeholder") or "写清楚要改什么"},
        "fallback": fallback,
    }
    if form.get("default"):
        field["default_value"] = form["default"]
    submit = {
        **_button(form.get("submit_label") or "修改", _value("pod_decide", review_id=review_id, decision=form["decision"], field=form["input"], offset=offset, type=review_type), kind="primary"),
        "name": f"{form['name']}_submit",
        "action_type": "form_submit",
    }
    return {"tag": "form", "name": form["name"], "elements": [field, submit], "fallback": fallback}


def _form_text(ctx, field: Any, pattern: re.Pattern[str] = _FIELD_RE) -> str:
    """表单提交的某个输入框文本；不是表单提交、字段名不合法或没填都返回空串。"""
    form_value = getattr(ctx, "form_value", None)
    if not (isinstance(field, str) and pattern.match(field) and isinstance(form_value, dict)):
        return ""
    text = form_value.get(field)
    return text.strip() if isinstance(text, str) else ""


def build_pod_reviews_card(page: dict[str, Any], image_keys: dict[str, str], *, notice: str | None = None) -> dict[str, Any]:
    total = int(page.get("total") or 0)
    offset = int(page.get("offset") or 0)
    limit = int(page.get("limit") or POD_PAGE_SIZE)
    types = page.get("types") or []
    review_type = types[0] if len(types) == 1 else None
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append(_markdown(notice))
    counts = page.get("type_counts") or []
    chips = [_button(("✓ " if review_type is None else "") + f"全部 {sum(t['count'] for t in counts)}", _value("pod_reviews"))]
    chips += [_button(("✓ " if review_type == t["type"] else "") + f"{t['label']} {t['count']}", _value("pod_reviews", type=t["type"])) for t in counts]
    elements.extend(_button_rows(chips))
    items = page.get("items") or []
    if not items:
        elements.append(_markdown("这里没有待审项了。"))
    for item in items:
        elements.extend(_review_elements(item, image_keys, offset=offset, review_type=review_type))
    nav = []
    if offset > 0:
        nav.append(_button("上一页", _value("pod_reviews", offset=max(0, offset - limit), type=review_type)))
    if offset + limit < total:
        nav.append(_button("下一页", _value("pod_reviews", offset=offset + limit, type=review_type)))
    nav.append(_button("返回工作台", _value("pod_dashboard")))
    elements.append({"tag": "hr"})
    elements.extend(_button_rows(nav))
    if total:
        elements.append(_note(f"第 {offset + 1}–{min(offset + limit, total)} 项，共 {total} 项"))
    return _card(f"{page.get('title') or 'POD 待审'}（{total}）", elements, template="blue")


def build_pod_daily_card(view: dict[str, Any]) -> dict[str, Any]:
    elements = [_markdown("\n".join(view["lines"]))]
    elements.extend(_button_rows([_button("返回工作台", _value("pod_dashboard")), _button("查看待审", _value("pod_reviews"))]))
    return _card(view.get("title") or "POD 日报", elements, template="blue")


def build_pod_timeline_card(view: dict[str, Any]) -> dict[str, Any]:
    elements = [_markdown("\n".join(view["lines"]))]
    elements.extend(_button_rows([_button("刷新", _value("pod_timeline")), _button("返回工作台", _value("pod_dashboard"))]))
    return _card(view.get("title") or "POD 最近动态", elements, template="blue")


def build_pod_gallery_card(page: dict[str, Any], image_keys: dict[str, str], *, notice: str | None = None) -> dict[str, Any]:
    total = int(page.get("total") or 0)
    offset = int(page.get("offset") or 0)
    limit = int(page.get("limit") or POD_PAGE_SIZE)
    batch = page.get("batch_id")
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append(_markdown(notice))
    items = page.get("items") or []
    if not items:
        elements.append(_markdown("还没有选中的款：在待审的「选款」里点「选中」后会出现在这里。"))
    for item in items:
        text = "\n".join([f"**{item['title']}**", *item.get("lines", [])])
        images = [(image_keys[img["path"]], img["label"]) for img in item.get("images", []) if image_keys.get(img["path"])]
        elements.append({"tag": "hr"})
        elements.extend(_media_block(text, images))
        elements.extend(_button_rows([_button("发原图", _value("pod_send_originals", asset_id=item["asset_id"], offset=offset, batch=batch), kind="primary")]))
    nav = []
    # 选款表（飞书多维表格）建好后可以直接打开：每张选中的款、方向、原图和原图下载链接
    table = _table_link(page.get("table_url"), "打开选款表", kind="primary")
    if table:
        nav.append(table)
    if offset > 0:
        nav.append(_button("上一页", _value("pod_gallery", offset=max(0, offset - limit), batch=batch)))
    if offset + limit < total:
        nav.append(_button("下一页", _value("pod_gallery", offset=offset + limit, batch=batch)))
    nav.append(_button("返回工作台", _value("pod_dashboard")))
    elements.append({"tag": "hr"})
    elements.extend(_button_rows(nav))
    if total:
        elements.append(_note(f"第 {offset + 1}–{min(offset + limit, total)} 款，共 {total} 款"))
    return _card(f"{page.get('title') or 'POD 已选款'}（{total}）", elements, template="blue")


_DIRECTION_TEMPLATES = {"proposed": "blue", "planned": "green", "running": "green", "done": "green"}


def _with_rules(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """相邻两块之间加分割线。"""
    out: list[dict[str, Any]] = []
    for block in blocks:
        if out:
            out.append({"tag": "hr"})
        out.append(block)
    return out


def _form_submit(form: dict[str, Any]) -> dict[str, Any]:
    """表单的提交按钮：动作与参数来自引擎给的 submit，提交时回调另带 form_value。"""
    return {**_step_button(form["submit"]), "name": f"{form['name']}_submit", "action_type": "form_submit"}


def _direction_text(item: dict[str, Any]) -> str:
    title = f"**{item['index']}. {item['title']}**"
    for extra in (item.get("tag"), item.get("state")):
        if extra:
            title += f"　{extra}"
    body = [title, *(f"· {line}" for line in item.get("lines") or [])]
    if item.get("keep"):
        body.append(f"不变：{item['keep']}")
    return "\n".join(body)


def _direction_checker(checker: dict[str, Any], text: str) -> dict[str, Any]:
    """勾选清单的一行：不配回调，勾选只在飞书本地生效（要飞书 V7.9+），提交表单时随 form_value 回传 {name: true/false}。"""
    return {
        "tag": "checker",
        "name": checker["name"],
        "checked": bool(checker.get("checked")),
        "text": {"tag": "lark_md", "content": text},
        "overall_checkable": True,
        "checked_style": {"show_strikethrough": False, "opacity": 1},
    }


def _direction_rows(items: list[dict[str, Any]], render: Any) -> list[dict[str, Any]]:
    """方向逐行排开。引擎给了分段（小变化 / 大变化）时每段先一行段标题、段内不加分割线（一轮几十个方向，卡片要省着用）；
    梳理字段之前的批次没有分段，照旧相邻两个之间加分割线。"""
    if not any(item.get("section") for item in items):
        return _with_rules([render(item) for item in items])
    out: list[dict[str, Any]] = []
    section = None
    for item in items:
        if item.get("section") and item["section"] != section:
            section = item["section"]
            if out:
                out.append({"tag": "hr"})
            out.append(_markdown(f"**{section}**"))
        out.append(render(item))
    return out


def build_pod_directions_card(view: dict[str, Any], image_keys: dict[str, str], *, notice: str | None = None) -> dict[str, Any]:
    """裂变方向挑选卡：顶部参考图 + 系列要点，每个方向一段（标题、改哪几处、不变的部分），按小变化 / 大变化分段。

    待挑选时这些方向是根节点表单里的勾选清单，底部「按勾选的方向出图」一次提交；表单上面是全选 / 全不选
    （回调，引擎改完勾选后整张卡刷新），换一批 / 任务明细 / 返回工作台在表单下面。回调按钮都放在表单外面。
    """
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append(_markdown(notice))
    head = "\n".join([f"**{view.get('status_line') or ''}**", *view.get("lines", [])])
    images = [(image_keys[img["path"]], img["label"]) for img in view.get("images", []) if image_keys.get(img["path"])]
    elements.extend(_media_block(head, images))
    elements.append({"tag": "hr"})
    form = view.get("form")
    items = view.get("items") or []
    if form and view.get("pick_actions"):
        elements.extend(_button_rows([_step_button(action) for action in view["pick_actions"]]))
    if form:
        rows = _direction_rows(items, lambda item: _direction_checker(item["checker"], _direction_text(item)) if item.get("checker") else _markdown(_direction_text(item)))
        fallback = {"tag": "fallback_text", "text": {"tag": "plain_text", "content": f"飞书版本太低看不到勾选框：在 POD 对话里说「{view.get('batch_id')} 选 1、3」"}}
        elements.append({"tag": "form", "name": form["name"], "elements": [*rows, _form_submit(form)], "fallback": fallback})
    else:
        elements.extend(_direction_rows(items, lambda item: _markdown(_direction_text(item))))
        elements.append({"tag": "hr"})
    buttons = [_step_button(action) for action in view.get("actions") or []]
    buttons.append(_button("返回工作台", _value("pod_dashboard")))
    elements.extend(_button_rows(buttons))
    if view.get("hint"):
        elements.append(_note(view["hint"]))
    return _card(view.get("title") or "挑选裂变方向", elements, template=_DIRECTION_TEMPLATES.get(view.get("status"), "grey"))


def _plan_form(form: dict[str, Any]) -> dict[str, Any]:
    """任务列表 / 任务明细卡里「这次想要的方向 + 策划」：输入框可以不填；一张卡上可以有多个（表单名按系列区分）。"""
    spec = form["input"]
    fallback = {"tag": "fallback_text", "text": {"tag": "plain_text", "content": "飞书版本太低看不到输入框：在 POD 对话里说「再策划一轮，往……方向」"}}
    field = {
        "tag": "input",
        "name": spec["name"],
        "required": False,
        "max_length": int(spec.get("max_length") or _HINT_MAX_CHARS),
        "width": "fill",
        "placeholder": {"tag": "plain_text", "content": spec.get("placeholder") or "这次想要的方向（可选）"},
        "fallback": fallback,
    }
    return {"tag": "form", "name": form["name"], "elements": [field, _form_submit(form)], "fallback": fallback}


def build_pod_history_card(view: dict[str, Any], image_keys: dict[str, str]) -> dict[str, Any]:
    """任务明细卡：这张参考图走到哪一步，做过和提过的方向各自细到哪一步，按批次从新到旧分页；底部可以写一句要求再策划一轮。

    没指定系列又有多个系列时，先列出系列（kind = families）。
    """
    elements: list[dict[str, Any]] = []
    if view.get("kind") == "families":
        elements.append(_markdown("\n".join(view.get("lines") or []) or "选一个系列看它做过的方向"))
        for family in view.get("families") or []:
            elements.append({"tag": "hr"})
            elements.append(_markdown(family["line"]))
            elements.extend(_button_rows([_step_button(family["action"])]))
    else:
        images = [(image_keys[img["path"]], img["label"]) for img in view.get("images", []) if image_keys.get(img["path"])]
        elements.extend(_media_block("\n".join(view.get("lines") or []), images))
        groups = view.get("groups") or []
        for group in groups:
            elements.append({"tag": "hr"})
            elements.append(_markdown("\n".join([group["head"], *(group.get("lines") or [])])))
        if not groups:
            elements.append(_markdown("还没有策划过方向。"))
        elements.extend(_button_rows([_step_button(action) for action in view.get("nav") or []]))
        elements.append({"tag": "hr"})
        if view.get("form"):
            elements.append(_plan_form(view["form"]))
        elif view.get("note"):
            elements.append(_markdown(f"**现在不能再策划**：{view['note']}"))
    buttons = [_step_button(action) for action in view.get("actions") or []]
    buttons += [_button("任务列表", _value("pod_tasks")), _button("返回工作台", _value("pod_dashboard"))]
    elements.extend(_button_rows(buttons))
    if view.get("hint"):
        elements.append(_note(view["hint"]))
    return _card(view.get("title") or "任务明细", elements, template="blue")


def _task_item_elements(item: dict[str, Any], image_keys: dict[str, str]) -> list[dict[str, Any]]:
    """任务列表 / 历史任务卡里的一个任务：缩略图 + 进度，「继续裂变」表单（或不能再策划的原因），链接与按钮。"""
    images = [(image_keys[img["path"]], img["label"]) for img in item.get("images", []) if image_keys.get(img["path"])]
    elements: list[dict[str, Any]] = [{"tag": "hr"}]
    elements.extend(_media_block("\n".join([f"**{item['title']}**", *item.get("lines", [])]), images))
    if item.get("form"):
        elements.append(_plan_form(item["form"]))
    elif item.get("note"):
        elements.append(_markdown(f"**现在不能再策划**：{item['note']}"))
    # 候选表、交付包是直接打开的链接，排在这个任务的按钮前面
    links = [_table_link(link.get("url"), str(link.get("label") or "打开链接")) for link in item.get("links") or []]
    buttons = [b for b in links if b] + [_step_button(action) for action in item.get("actions") or []]
    elements.extend(_button_rows(buttons))
    return elements


def build_pod_tasks_card(view: dict[str, Any], image_keys: dict[str, str], *, notice: str | None = None) -> dict[str, Any]:
    """任务列表卡：一张参考图一个任务，每个任务一段（缩略图、五步进度、现在在做什么），下面是按状态给的按钮。

    已完成的任务在按钮上方多一个「这次想要的方向 + 继续裂变」表单（根节点 form，每个任务各一个）。
    """
    total = int(view.get("total") or 0)
    offset = int(view.get("offset") or 0)
    limit = int(view.get("limit") or POD_TASK_PAGE_SIZE)
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append(_markdown(notice))
    if view.get("lines"):
        elements.append(_markdown("\n".join(view["lines"])))
    elements.extend(_button_rows([_step_button(action) for action in view.get("actions") or []]))
    for item in view.get("items") or []:
        elements.extend(_task_item_elements(item, image_keys))
    nav = [_step_button(action) for action in view.get("nav") or []]
    nav += [_button("刷新", _value("pod_tasks", offset=offset)), _button("返回工作台", _value("pod_dashboard"))]
    elements.append({"tag": "hr"})
    elements.extend(_button_rows(nav))
    hint = view.get("hint") or ""
    if total:
        hint = f"第 {offset + 1}–{min(offset + limit, total)} 个，共 {total} 个。{hint}"
    if hint:
        elements.append(_note(hint))
    return _card(f"{view.get('title') or 'POD 任务列表'}（{total}）", elements, template="blue")


def _shelf_search_form(search: dict[str, Any]) -> dict[str, Any]:
    """历史任务卡顶部的搜索框（根节点表单）：空着提交就是全部；上一次搜的字留在框里。"""
    spec = search["input"]
    fallback = {"tag": "fallback_text", "text": {"tag": "plain_text", "content": "飞书版本太低看不到搜索框：在 POD 对话里说「找一下历史任务里的恐龙」"}}
    field: dict[str, Any] = {
        "tag": "input",
        "name": spec["name"],
        "required": False,
        "max_length": int(spec.get("max_length") or _SHELF_QUERY_MAX_CHARS),
        "width": "fill",
        "placeholder": {"tag": "plain_text", "content": spec.get("placeholder") or "搜名字、编号或日期"},
        "fallback": fallback,
    }
    if spec.get("value"):
        field["default_value"] = spec["value"]
    return {"tag": "form", "name": search["name"], "elements": [field, _form_submit(search)], "fallback": fallback}


def build_pod_shelf_card(view: dict[str, Any], image_keys: dict[str, str], *, notice: str | None = None) -> dict[str, Any]:
    """历史任务卡：已归档的任务（新归档的在前），顶部搜索框；每个任务和任务列表里一样一段，
    带「调出」「看明细」和「这次想要的方向 + 继续裂变」表单（继续裂变由引擎自动调出）。"""
    total = int(view.get("total") or 0)
    offset = int(view.get("offset") or 0)
    limit = int(view.get("limit") or POD_SHELF_PAGE_SIZE)
    query = view.get("query") or None
    elements: list[dict[str, Any]] = []
    if notice:
        elements.append(_markdown(notice))
    if view.get("lines"):
        elements.append(_markdown("\n".join(view["lines"])))
    if view.get("search"):
        elements.append(_shelf_search_form(view["search"]))
    for item in view.get("items") or []:
        elements.extend(_task_item_elements(item, image_keys))
    nav = [_step_button(action) for action in view.get("nav") or []]
    nav += [_button("刷新", _value("pod_shelf", offset=offset, q=query)), _button("返回工作台", _value("pod_dashboard"))]
    elements.append({"tag": "hr"})
    elements.extend(_button_rows(nav))
    hint = view.get("hint") or ""
    if total:
        hint = f"第 {offset + 1}–{min(offset + limit, total)} 个，共 {total} 个。{hint}"
    if hint:
        elements.append(_note(hint))
    return _card(f"{view.get('title') or '历史任务'}（{total}）", elements, template="grey")


def build_pod_error_card(message: str) -> dict[str, Any]:
    elements = [_markdown(f"**操作失败**：{message}"), *_button_rows([_button("重新打开工作台", _value("pod_dashboard"))])]
    return _card("POD 毛毯工作台", elements, template="red")


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _review_type(value: Any) -> str | None:
    return value if isinstance(value, str) and _TYPE_RE.match(value) else None


def _batch_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _BATCH_RE.match(value) else None


def _seed_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _SEED_RE.match(value) else None


def _shelf_query(value: Any) -> str | None:
    """历史任务的搜索字：去掉控制字符、合并空白，最多 _SHELF_QUERY_MAX_CHARS 个字；空的为 None（看全部）。"""
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL_CHARS_RE.sub(" ", value).split())[:_SHELF_QUERY_MAX_CHARS]
    return text or None


def _variant_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _VARIANT_RE.match(value) else None


def _flag(value: Any) -> bool | None:
    """勾选按钮的 on：只认 0 / 1（布尔值和别的写法一律当无效）。"""
    if isinstance(value, bool) or value not in (0, 1, "0", "1"):
        return None
    return str(value) == "1"


def _picked_variants(form_value: dict[str, Any]) -> list[str]:
    """勾选清单提交的 form_value 里勾上的方向（pick_V… 为 true），按编号排序。"""
    picked = {match.group(1) for name, checked in form_value.items() if checked in (True, "true") and isinstance(name, str) and (match := _PICK_FIELD_RE.match(name))}
    return sorted(picked, key=lambda variant: int(variant[1:]))


def _ticked_records(actions: Any) -> list[str]:
    """记录变更事件里「改成勾上」的记录：只看修改（新增 / 删除行是程序挪控制行时的），after_value 里有字段值为 true。"""
    records: list[str] = []
    for action in actions or []:
        if getattr(action, "action", None) != "record_edited":
            continue
        record_id = getattr(action, "record_id", None)
        if not (isinstance(record_id, str) and _BITABLE_ID_RE.match(record_id)):
            continue
        values = [getattr(field, "field_value", None) for field in getattr(action, "after_value", None) or []]
        if any(value is True or (isinstance(value, str) and value.strip().lower() == "true") for value in values):
            records.append(record_id)
    return records


def _confirm_notice(result: dict[str, Any]) -> str:
    count = len(result.get("picked") or [])
    if result.get("directions_started"):
        notice = f"已确认 {count} 个方向，后台开始出图：会一直出到这几个方向全部出完，完成后通知你（{result['directions_started']}）"
    elif result.get("directions_queued"):
        wait = "流水线已暂停，恢复后接着出" if result.get("reason") else "已排进正在出图的队列，有空的工位就接着出"
        notice = f"已确认 {count} 个方向：{wait}"
    else:
        notice = f"已确认 {count} 个方向"
    if result.get("family_approved"):
        notice += f"；系列 {result.get('family_id')} 已一并认可"
    return notice


def _scenes_notice(result: dict[str, Any]) -> str:
    queued = len(result.get("queued") or [])
    if queued:
        task = f"（{result['sync_task']}）" if result.get("sync_task") else ""
        return f"已把 {queued} 款交给 mooya 出套图，出完会通知你{task}"
    skipped = result.get("skipped") or []
    if skipped:
        return f"没有新的款要出套图：{skipped[0].get('reason') or ''}"
    return "这个任务还没有选中的款"


def _downloaded_notice(result: dict[str, Any], fallback: str) -> str:
    """「B… 已下载」之后的提示：标上了没有、这个任务是不是当场归档了、还差什么。"""
    label = result.get("label") or fallback
    notice = f"**{label}** 的交付包之前就标过已下载" if result.get("already") else f"已标 **{label}** 交付包已下载"
    if result.get("archived"):
        notice += "；任务的套图都出完、交付包都下载了，已归档到「历史任务」"
    elif result.get("shelf_block"):
        notice += f"；还没归档：{result['shelf_block']}"
    return notice


# -- 渠道混入 -----------------------------------------------------------------


class PodCardsMixin:
    """由 ForkFeishuChannel 继承；依赖其 name / _channel_store / _schedule / _publish_synthetic_command /
    _upload_image / send_file / _patch_card / _build_card_content。按钮回调的 ctx 是 feishu.CardActionContext。"""

    name: str

    def _pod(self) -> PodBridge:
        bridge = getattr(self, "_pod_bridge", None)
        if bridge is None:
            bridge = self._pod_bridge = PodBridge()
        return bridge

    def _pod_is_p2p(self, chat_id: str | None, open_id: str | None) -> bool:
        store = self._channel_store()
        return bool(store is not None and chat_id and open_id and store.get_user_chat(self.name, open_id) == chat_id)

    # -- 视图 → 卡片 ----------------------------------------------------------

    async def _pod_image_keys(self, paths: list[str]) -> dict[str, str]:
        cache: OrderedDict[str, str] | None = getattr(self, "_pod_image_cache", None)
        if cache is None:
            cache = self._pod_image_cache = OrderedDict()
        keys: dict[str, str] = {}
        for path in dict.fromkeys(paths):
            try:
                cache_key = f"{path}:{os.stat(path).st_mtime_ns}"
            except OSError:
                continue
            if cache_key in cache:
                cache.move_to_end(cache_key)
                keys[path] = cache[cache_key]
                continue
            try:
                image_key = await self._upload_image(path)
            except Exception:
                logger.warning("[Pod] thumbnail upload failed: %s", path, exc_info=True)
                continue
            cache[cache_key] = keys[path] = image_key
            while len(cache) > _IMAGE_KEY_CACHE_LIMIT:
                cache.popitem(last=False)
        return keys

    async def _pod_dashboard_card(self, *, notice: str | None = None) -> dict[str, Any]:
        return build_pod_dashboard_card(await self._pod().run("ui", "dashboard"), notice=notice)

    async def _pod_reviews_card(self, offset: int = 0, review_type: str | None = None, *, notice: str | None = None) -> dict[str, Any]:
        args = ["ui", "reviews", "--offset", str(offset), "--limit", str(POD_PAGE_SIZE)]
        if review_type:
            args += ["--type", review_type]
        page = await self._pod().run(*args)
        paths = [img["path"] for item in page.get("items") or [] for img in item.get("images") or []]
        return build_pod_reviews_card(page, await self._pod_image_keys(paths), notice=notice)

    async def _pod_daily_card(self) -> dict[str, Any]:
        return build_pod_daily_card(await self._pod().run("ui", "daily"))

    async def _pod_timeline_card(self) -> dict[str, Any]:
        return build_pod_timeline_card(await self._pod().run("ui", "timeline", "--limit", "20"))

    async def _pod_gallery_card(self, offset: int = 0, batch: str | None = None, *, notice: str | None = None) -> dict[str, Any]:
        args = ["ui", "gallery", "--offset", str(offset), "--limit", str(POD_PAGE_SIZE)]
        if batch:
            args += ["--batch", batch]
        page = await self._pod().run(*args)
        paths = [img["path"] for item in page.get("items") or [] for img in item.get("images") or []]
        return build_pod_gallery_card(page, await self._pod_image_keys(paths), notice=notice)

    async def _pod_directions_card(self, batch: str, *, notice: str | None = None) -> dict[str, Any]:
        view = await self._pod().run("ui", "directions", "--batch", batch)
        paths = [img["path"] for img in view.get("images") or []]
        return build_pod_directions_card(view, await self._pod_image_keys(paths), notice=notice)

    async def _pod_history_card(self, family: str | None = None, offset: int = 0) -> dict[str, Any]:
        args = ["ui", "history", "--offset", str(offset)]
        if family:
            args += ["--family", family]
        view = await self._pod().run(*args)
        paths = [img["path"] for img in view.get("images") or []]
        return build_pod_history_card(view, await self._pod_image_keys(paths))

    async def _pod_tasks_card(self, offset: int = 0, *, notice: str | None = None) -> dict[str, Any]:
        view = await self._pod().run("ui", "tasks", "--offset", str(offset), "--limit", str(POD_TASK_PAGE_SIZE))
        paths = [img["path"] for item in view.get("items") or [] for img in item.get("images") or []]
        return build_pod_tasks_card(view, await self._pod_image_keys(paths), notice=notice)

    async def _pod_shelf_card(self, offset: int = 0, query: str | None = None, *, notice: str | None = None) -> dict[str, Any]:
        args = ["ui", "shelf", "--offset", str(offset), "--limit", str(POD_SHELF_PAGE_SIZE)]
        if query:
            # 写成一个参数：以「-」开头的搜索字也不会被当成选项
            args.append(f"--query={query}")
        view = await self._pod().run(*args)
        paths = [img["path"] for item in view.get("items") or [] for img in item.get("images") or []]
        return build_pod_shelf_card(view, await self._pod_image_keys(paths), notice=notice)

    async def _pod_entry_card(self, chat_id: str | None, open_id: str | None) -> dict[str, Any] | str:
        """/pod 与菜单 POD 打开的工作台（调用方已校验绑定）。"""
        if not self._pod_is_p2p(chat_id, open_id):
            return self._build_card_content(P2P_ONLY_TEXT)
        try:
            return await self._pod_dashboard_card()
        except PodctlError as exc:
            return build_pod_error_card(str(exc))

    async def _pod_send_originals(self, chat_id: str, asset_ids: list[str]) -> str:
        """把已选款的原图作为文件消息发到本单聊（文件消息不压缩，适合拿去打样）。"""
        args = ["ui", "originals"]
        for asset_id in asset_ids:
            args += ["--asset", asset_id]
        result = await self._pod().run(*args)
        sent: list[str] = []
        # 提示里写图片名字「IMG…_方向短语」（引擎给的 label），老版本引擎没给时写编号
        failed = [f"{r.get('label') or r['asset_id']}（{r['reason']}）" for r in result.get("refused") or []]
        for item in result.get("files") or []:
            name = item.get("label") or item["asset_id"]
            size = int(item.get("bytes") or 0)
            if size > _ORIGINAL_MAX_BYTES:
                failed.append(f"{name}（超过飞书 30MB 文件上限）")
                continue
            path = Path(item["path"])
            attachment = ResolvedAttachment(virtual_path=str(path), actual_path=path, filename=item["filename"], mime_type="application/octet-stream", size=size, is_image=False)
            message = OutboundMessage(channel_name=self.name, chat_id=chat_id, thread_id="", text="")
            if await self.send_file(message, attachment):
                sent.append(name)
            else:
                failed.append(f"{name}（发送失败）")
        parts = []
        if sent:
            parts.append(f"已发送原图：{'、'.join(sent)}")
        if failed:
            parts.append(f"没有发送：{'、'.join(failed)}")
        return "；".join(parts) or "没有可发送的原图"

    # -- 按钮回调（lark 线程里同步执行）-----------------------------------------

    def _pod_guard(self, ctx: Any) -> dict[str, Any] | None:
        if not self._pod_is_p2p(ctx.chat_id, ctx.open_id):
            return {"toast": _toast("warning", P2P_ONLY_TEXT)}
        return None

    def _pod_async(self, ctx: Any, label: str, render: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """先回 toast；podctl 与上传缩略图放到主循环里做，完成后原地 patch 这张卡片。"""
        guard = self._pod_guard(ctx)
        if guard is not None:
            return guard
        if not ctx.message_id:
            return {"toast": _toast("error", "找不到这张卡片，请发 /pod 重新打开")}
        self._schedule(self._pod_patch(ctx.message_id, render), name=f"pod_{label}", msg_id=ctx.message_id)
        return {"toast": _toast("info", f"{label}…")}

    async def _pod_patch(self, message_id: str, render: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        try:
            card = await render()
        except PodctlError as exc:
            card = build_pod_error_card(str(exc))
        except Exception:
            logger.exception("[Pod] rendering card failed")
            card = build_pod_error_card("内部错误，请稍后重试")
        await self._patch_card(message_id, card)

    def _pod_background(self, ctx: Any, label: str, *args: str, back: str | None = None) -> dict[str, Any]:
        """让引擎把长任务转后台；工作台（back=tasks 时是任务列表）原地刷新并说明已开始（或为什么没开始）。"""

        async def render() -> dict[str, Any]:
            try:
                result = await self._pod().run(*args)
                notice = f"{BACKGROUND_NOTICE}（{result.get('task_id')}）"
            except PodctlError as exc:
                notice = f"**没有开始**：{exc}"
            if back == _BACK_TASKS:
                return await self._pod_tasks_card(notice=notice)
            return await self._pod_dashboard_card(notice=notice)

        return self._pod_async(ctx, label, render)

    @staticmethod
    def _pod_by(ctx) -> str:
        return f"feishu:{ctx.open_id}"

    def _card_action_pod_dashboard(self, ctx) -> dict[str, Any]:
        return self._pod_async(ctx, "正在刷新", self._pod_dashboard_card)

    def _card_action_pod_reviews(self, ctx) -> dict[str, Any]:
        offset, review_type = _int(ctx.value.get("offset")), _review_type(ctx.value.get("type"))
        return self._pod_async(ctx, "正在加载待审", lambda: self._pod_reviews_card(offset, review_type))

    def _card_action_pod_decide(self, ctx) -> dict[str, Any]:
        review_id, decision = ctx.value.get("review_id"), ctx.value.get("decision")
        if not (isinstance(review_id, str) and _REVIEW_ID_RE.match(review_id) and isinstance(decision, str) and _DECISION_RE.match(decision)):
            return {"toast": _toast("error", "无效的审核操作")}
        offset, review_type = _int(ctx.value.get("offset")), _review_type(ctx.value.get("type"))
        by = self._pod_by(ctx)
        args = ["review", "decide", review_id, "--decision", decision, "--by", by]
        if decision == "needs_changes":
            note = _form_text(ctx, ctx.value.get("field"))
            if not note:
                return {"toast": _toast("warning", "先在输入框写清楚要改什么")}
            args += ["--note", note]

        async def render() -> dict[str, Any]:
            try:
                result = await self._pod().run(*args)
                effect = result.get("effect") or {}
                # 审核对象的名字（如「IMG…_方向短语」），老版本引擎没给 label 时只写审核编号
                subject = f"**{review_id}**（{result['label']}）" if result.get("label") else f"**{review_id}**"
                notice = f"已处理 {subject}"
                if effect.get("revise_started"):
                    notice += f"：已在后台按提案修订系列 {effect.get('family_id')}，完成后会通知你"
                elif effect.get("fix_started"):
                    notice = f"{subject} 已开始修改：改好的新图会回到选款，完成后会通知你"
                elif effect.get("fix_queued"):
                    wait = f"{effect['reason']}，恢复后接着改" if effect.get("reason") else "正在出图的 worker 有空的工位就先改它"
                    notice = f"{subject} 的修改已排队：{wait}"
            except PodctlError as exc:
                notice = f"**{review_id}** 没有处理：{exc}"
            return await self._pod_reviews_card(offset, review_type, notice=notice)

        return self._pod_async(ctx, f"正在提交 {review_id}", render)

    def _card_action_pod_tick(self, ctx) -> dict[str, Any]:
        async def render() -> dict[str, Any]:
            result = await self._pod().run("tick")
            return await self._pod_dashboard_card(notice=f"**巡检结论**：{result.get('reason') or result.get('decision')}")

        return self._pod_async(ctx, "正在巡检", render)

    def _card_action_pod_pause(self, ctx) -> dict[str, Any]:
        async def render() -> dict[str, Any]:
            await self._pod().run("pause", "--reason", "飞书工作台暂停")
            return await self._pod_dashboard_card(notice="已暂停：后台不会再领取新任务")

        return self._pod_async(ctx, "正在暂停", render)

    def _card_action_pod_resume(self, ctx) -> dict[str, Any]:
        by = self._pod_by(ctx)

        async def render() -> dict[str, Any]:
            result = await self._pod().run("resume", "--by", by)
            fixing = (result.get("fixes") or {}).get("assets")
            notice = f"已恢复，接着改 {'、'.join(fixing)}" if fixing else "已恢复"
            return await self._pod_dashboard_card(notice=notice)

        return self._pod_async(ctx, "正在恢复", render)

    def _card_action_pod_daily(self, ctx) -> dict[str, Any]:
        return self._pod_async(ctx, "正在生成日报", self._pod_daily_card)

    def _card_action_pod_plan(self, ctx) -> dict[str, Any]:
        # 每轮按这张图的可变字段出方向，数量由引擎定：旧卡片按钮里的 limit 一律不传
        family_id = ctx.value.get("family_id")
        if not (isinstance(family_id, str) and _FAMILY_RE.match(family_id)):
            return {"toast": _toast("error", "无效的裂变参数")}
        args = ["plan", "--family", family_id]
        # 任务列表 / 任务明细卡的表单提交带「这次想要的方向」（可以不填）；写成一个参数，以「-」开头也不会被当成选项
        hint = " ".join(_form_text(ctx, f"hint_{family_id}", _HINT_FIELD_RE).split())
        if len(hint) > _HINT_MAX_CHARS:
            return {"toast": _toast("warning", f"这次想要的方向最多 {_HINT_MAX_CHARS} 个字")}
        if hint:
            args.append(f"--hint={hint}")
        back = _BACK_TASKS if ctx.value.get("back") == _BACK_TASKS else None
        return self._pod_background(ctx, f"正在提交 {family_id} 策划", *args, "--detach", "--notify", back=back)

    def _card_action_pod_plan_view(self, ctx) -> dict[str, Any]:
        batch = _batch_id(ctx.value.get("batch"))
        if batch is None:
            return {"toast": _toast("error", "无效的批次编号")}
        return self._pod_async(ctx, "正在打开挑选卡", lambda: self._pod_directions_card(batch))

    def _card_action_pod_plan_pick(self, ctx) -> dict[str, Any]:
        batch, variant, on = _batch_id(ctx.value.get("batch")), _variant_id(ctx.value.get("variant")), _flag(ctx.value.get("on"))
        if batch is None or variant is None or on is None:
            return {"toast": _toast("error", "无效的挑选操作")}
        args = ["directions", "pick", batch, variant, *([] if on else ["--off"])]

        async def render() -> dict[str, Any]:
            notice = None
            try:
                await self._pod().run(*args)
            except PodctlError as exc:
                notice = f"**没有改成**：{exc}"
            return await self._pod_directions_card(batch, notice=notice)

        return self._pod_async(ctx, "正在勾选" if on else "正在取消", render)

    def _card_action_pod_plan_pick_all(self, ctx) -> dict[str, Any]:
        batch, on = _batch_id(ctx.value.get("batch")), _flag(ctx.value.get("on"))
        if batch is None or on is None:
            return {"toast": _toast("error", "无效的挑选操作")}
        args = ["directions", "pick", batch, "all", *([] if on else ["--off"])]

        async def render() -> dict[str, Any]:
            try:
                result = await self._pod().run(*args)
            except PodctlError as exc:
                return await self._pod_directions_card(batch, notice=f"**没有改成**：{exc}")
            if on:
                notice = f"已全选 {len(result.get('picked') or [])} 个方向：可以再取消几个，最后点「按勾选的方向出图」"
            else:
                notice = "已全部取消勾选"
            return await self._pod_directions_card(batch, notice=notice)

        return self._pod_async(ctx, "正在全选" if on else "正在全部取消", render)

    def _card_action_pod_plan_confirm(self, ctx) -> dict[str, Any]:
        batch = _batch_id(ctx.value.get("batch"))
        if batch is None:
            return {"toast": _toast("error", "无效的批次编号")}
        args = ["directions", "confirm", batch, "--by", self._pod_by(ctx), "--start"]
        # 勾选清单的表单提交：一次带上全部勾上的方向；旧卡片的确认按钮不带表单，按引擎里记下的勾选确认
        form_value = getattr(ctx, "form_value", None)
        if isinstance(form_value, dict):
            picked = _picked_variants(form_value)
            if not picked:
                return self._pod_guard(ctx) or {"toast": _toast("warning", "先勾选要出图的方向")}
            args += ["--pick", ",".join(picked)]

        async def render() -> dict[str, Any]:
            try:
                notice = _confirm_notice(await self._pod().run(*args))
            except PodctlError as exc:
                notice = f"**没有开始**：{exc}"
            return await self._pod_directions_card(batch, notice=notice)

        return self._pod_async(ctx, f"正在确认 {batch}", render)

    def _card_action_pod_plan_reject(self, ctx) -> dict[str, Any]:
        batch = _batch_id(ctx.value.get("batch"))
        if batch is None:
            return {"toast": _toast("error", "无效的批次编号")}
        by = self._pod_by(ctx)

        async def render() -> dict[str, Any]:
            try:
                result = await self._pod().run("directions", "reject", batch, "--by", by, "--replan", "--notify")
            except PodctlError as exc:
                return await self._pod_directions_card(batch, notice=f"**没有换成**：{exc}")
            if result.get("replan_started"):
                notice = f"已换一批：后台正在重新策划，完成后会通知你（{result['replan_started']}）"
            else:
                notice = f"这一批都不要了；{result.get('replan_skipped') or '没有重新策划'}"
            return await self._pod_dashboard_card(notice=notice)

        return self._pod_async(ctx, "正在换一批", render)

    def _card_action_pod_history(self, ctx) -> dict[str, Any]:
        family = ctx.value.get("family")
        if family is not None and not (isinstance(family, str) and _FAMILY_RE.match(family)):
            return {"toast": _toast("error", "无效的系列编号")}
        offset = _int(ctx.value.get("offset"))
        return self._pod_async(ctx, "正在打开任务明细", lambda: self._pod_history_card(family, offset))

    def _card_action_pod_tasks(self, ctx) -> dict[str, Any]:
        offset = _int(ctx.value.get("offset"))
        return self._pod_async(ctx, "正在加载任务列表", lambda: self._pod_tasks_card(offset))

    def _card_action_pod_accept(self, ctx) -> dict[str, Any]:
        """任务列表上的「选款完成」：引擎回读这个任务的候选表勾选，选中的款接着交给 mooya 出套图，在后台做完发卡片；
        确认过的方向还没全部出完图时引擎当场拒绝，提示里写明还差多少。"""
        seed = ctx.value.get("seed")
        if not (isinstance(seed, str) and _SEED_RE.match(seed)):
            return {"toast": _toast("error", "无效的任务编号")}
        by = self._pod_by(ctx)
        return self._pod_background(ctx, f"正在提交 {seed} 选款验收", "table", "accept", "--seed", seed, "--by", by, "--detach", "--notify", back=_BACK_TASKS)

    def _card_action_pod_dispatch(self, ctx) -> dict[str, Any]:
        """任务列表上的「选中的先出套图」（图还没出完时）：引擎回读候选表的勾选，勾上的款先定为选中、交给 mooya 出套图，
        没勾的不动、不算选款完成，在后台做完发卡片。"""
        seed = _seed_id(ctx.value.get("seed"))
        if seed is None:
            return {"toast": _toast("error", "无效的任务编号")}
        by = self._pod_by(ctx)
        return self._pod_background(ctx, f"正在提交 {seed} 先出套图", "table", "dispatch", "--seed", seed, "--by", by, "--detach", "--notify", back=_BACK_TASKS)

    def _card_action_pod_downloaded(self, ctx) -> dict[str, Any]:
        """「B… 已下载」：标这个批次的交付包已下载，满足条件时引擎当场归档这个任务；
        从历史任务卡点的（back=shelf）回历史任务卡，其余回任务列表。撤销只在 POD 对话里说。"""
        seed, batch = _seed_id(ctx.value.get("seed")), _batch_id(ctx.value.get("batch"))
        if seed is None or batch is None:
            return {"toast": _toast("error", "无效的交付包")}
        args = ["scenes", "downloaded", "--seed", seed, "--batch", batch, "--by", self._pod_by(ctx), "--source", "card"]
        back = _BACK_SHELF if ctx.value.get("back") == _BACK_SHELF else None

        async def render() -> dict[str, Any]:
            try:
                notice = _downloaded_notice(await self._pod().run(*args), f"{seed} · {batch}")
            except PodctlError as exc:
                notice = f"**{seed} · {batch} 没有标上**：{exc}"
            if back == _BACK_SHELF:
                return await self._pod_shelf_card(notice=notice)
            return await self._pod_tasks_card(notice=notice)

        return self._pod_async(ctx, f"正在标 {batch} 已下载", render)

    def _card_action_pod_scenes(self, ctx) -> dict[str, Any]:
        """任务列表上的「出套图」：选中了还没出（或没出成）的款交给 mooya；引擎当场入队并拉起后台，出完发卡片。"""
        seed = ctx.value.get("seed")
        if not (isinstance(seed, str) and _SEED_RE.match(seed)):
            return {"toast": _toast("error", "无效的任务编号")}
        args = ["scenes", "start", "--seed", seed, "--by", self._pod_by(ctx)]

        async def render() -> dict[str, Any]:
            try:
                notice = _scenes_notice(await self._pod().run(*args))
            except PodctlError as exc:
                notice = f"**没有开始**：{exc}"
            return await self._pod_tasks_card(notice=notice)

        return self._pod_async(ctx, f"正在提交 {seed} 出套图", render)

    def _card_action_pod_shelf(self, ctx) -> dict[str, Any]:
        """历史任务卡：翻页 / 刷新按钮带 q（上一次搜的字）；搜索框提交时以框里的字为准，从第一页看起。"""
        offset = _int(ctx.value.get("offset"))
        form_value = getattr(ctx, "form_value", None)
        if isinstance(form_value, dict):
            query, offset = _shelf_query(form_value.get(_SHELF_QUERY_FIELD)), 0
        else:
            query = _shelf_query(ctx.value.get("q"))
        return self._pod_async(ctx, "正在打开历史任务", lambda: self._pod_shelf_card(offset, query))

    def _card_action_pod_archive(self, ctx) -> dict[str, Any]:
        """任务列表上的「归档」：引擎有在做的事、还有待选时拒绝（写明原因）；收起后回到任务列表。"""
        seed = _seed_id(ctx.value.get("seed"))
        if seed is None:
            return {"toast": _toast("error", "无效的任务编号")}
        by = self._pod_by(ctx)

        async def render() -> dict[str, Any]:
            try:
                result = await self._pod().run("shelf", "archive", seed, "--by", by)
                label = result.get("label") or seed
                if result.get("already"):
                    notice = f"**{label}** 已经归档了，在「历史任务」里"
                else:
                    notice = f"已归档 **{label}**：在「历史任务」里，候选表马上挪进「已归档」文件夹；要接着裂变就在那里点「调出」或「继续裂变」"
            except PodctlError as exc:
                notice = f"**{seed} 没有归档**：{exc}"
            return await self._pod_tasks_card(notice=notice)

        return self._pod_async(ctx, f"正在归档 {seed}", render)

    def _card_action_pod_restore(self, ctx) -> dict[str, Any]:
        """「调出」：任务回到任务列表、候选表挪回根目录；从历史任务卡点的（back=shelf）回到历史任务卡，其余回任务列表。"""
        seed = _seed_id(ctx.value.get("seed"))
        if seed is None:
            return {"toast": _toast("error", "无效的任务编号")}
        by = self._pod_by(ctx)
        back = _BACK_SHELF if ctx.value.get("back") == _BACK_SHELF else None

        async def render() -> dict[str, Any]:
            try:
                result = await self._pod().run("shelf", "restore", seed, "--by", by)
                label = result.get("label") or seed
                if result.get("already_active"):
                    notice = f"**{label}** 本来就在任务列表里"
                else:
                    notice = f"已调出 **{label}**：回到任务列表，候选表马上挪回根目录"
            except PodctlError as exc:
                notice = f"**{seed} 没有调出**：{exc}"
            if back == _BACK_SHELF:
                return await self._pod_shelf_card(notice=notice)
            return await self._pod_tasks_card(notice=notice)

        return self._pod_async(ctx, f"正在调出 {seed}", render)

    def _card_action_pod_worker(self, ctx) -> dict[str, Any]:
        return self._pod_background(ctx, "正在提交出图", "worker", "--detach", "--source", "feishu", "--notify")

    def _card_action_pod_trial_start(self, ctx) -> dict[str, Any]:
        by = self._pod_by(ctx)

        async def render() -> dict[str, Any]:
            await self._pod().run("trial", "start", "--hours", "24", "--by", by)
            return await self._pod_dashboard_card(notice="已开启 24 小时连续运行：后台按巡检间隔自动推进，随时可以停止")

        return self._pod_async(ctx, "正在开启连续运行", render)

    def _card_action_pod_trial_stop(self, ctx) -> dict[str, Any]:
        by = self._pod_by(ctx)

        async def render() -> dict[str, Any]:
            await self._pod().run("trial", "stop", "--by", by)
            return await self._pod_dashboard_card(notice="已停止连续运行：巡检不再派发新任务，正在跑的这一轮会做完")

        return self._pod_async(ctx, "正在停止连续运行", render)

    def _card_action_pod_timeline(self, ctx) -> dict[str, Any]:
        return self._pod_async(ctx, "正在加载动态", self._pod_timeline_card)

    def _card_action_pod_gallery(self, ctx) -> dict[str, Any]:
        offset, batch = _int(ctx.value.get("offset")), _batch_id(ctx.value.get("batch"))
        return self._pod_async(ctx, "正在加载已选款", lambda: self._pod_gallery_card(offset, batch))

    def _card_action_pod_send_originals(self, ctx) -> dict[str, Any]:
        asset_id = ctx.value.get("asset_id")
        if not (isinstance(asset_id, str) and _ASSET_RE.match(asset_id)):
            return {"toast": _toast("error", "无效的素材编号")}
        offset, batch, chat_id = _int(ctx.value.get("offset")), _batch_id(ctx.value.get("batch")), ctx.chat_id

        async def render() -> dict[str, Any]:
            notice = await self._pod_send_originals(chat_id, [asset_id])
            return await self._pod_gallery_card(offset, batch, notice=notice)

        return self._pod_async(ctx, f"正在发送 {asset_id} 原图", render)

    def _card_action_pod_enter_chat(self, ctx) -> dict[str, Any]:
        guard = self._pod_guard(ctx)
        if guard is not None:
            return guard
        kind, text = self._pod_enter_chat(ctx.store, ctx.chat_id, ctx.open_id, ctx.identity.current_thread_id)
        return {"toast": _toast(kind, text)}

    def _card_action_pod_return_chat(self, ctx) -> dict[str, Any]:
        guard = self._pod_guard(ctx)
        if guard is not None:
            return guard
        kind, text = self._pod_return_chat(ctx.store, ctx.chat_id, ctx.open_id, ctx.identity.current_thread_id)
        return {"toast": _toast(kind, text)}

    # -- 进入 / 返回 POD 专属对话 ---------------------------------------------

    def _pod_enter_chat(self, store, chat_id: str, open_id: str | None, current: str | None) -> tuple[str, str]:
        sessions = store.list_sessions(self.name, chat_id)
        pod_ids = {s["thread_id"] for s in sessions if s.get("agent") == POD_PERSONA}
        if current in pod_ids:
            return "info", "已经在 POD 对话里了，直接发消息即可"
        if current:
            store.set_pod_return_thread(self.name, chat_id, current)
        target = next((s for s in sessions if s["thread_id"] in pod_ids), None)
        if target is None:
            # manager 以该人设新建线程并回复确认
            self._publish_synthetic_command(chat_id, open_id, f"/agent use {POD_PERSONA}", source=CARD_ACTION_SOURCE)
            return "info", "正在新建 POD 对话…"
        # 与 /sessions 卡片切换一致：本地先恢复会话设置，当前会话指针（已绑定时在绑定库）交给 manager 写
        store.switch_session(self.name, chat_id, target["thread_id"])
        self._publish_synthetic_command(chat_id, open_id, f"/sessions _gcswitch {target['thread_id']}", source=CARD_ACTION_SOURCE)
        return "success", f"已进入 POD 对话：{session_display_title(target)}"

    def _pod_return_chat(self, store, chat_id: str, open_id: str | None, current: str | None) -> tuple[str, str]:
        target = store.get_pod_return_thread(self.name, chat_id)
        if not target or store.get_session(self.name, chat_id, target) is None:
            return "warning", "没有记录可返回的会话，请用 /sessions 选择"
        if target == current:
            store.set_pod_return_thread(self.name, chat_id, None)
            return "info", "已经在原会话里了"
        snapshot = store.switch_session(self.name, chat_id, target)
        self._publish_synthetic_command(chat_id, open_id, f"/sessions _gcswitch {target}", source=CARD_ACTION_SOURCE)
        store.set_pod_return_thread(self.name, chat_id, None)
        return "success", f"已返回：{session_display_title(snapshot or {})}"

    # -- 多维表格记录变更（候选表的「▶ 选款完成」控制行）----------------------

    def _on_bitable_record_changed(self, data) -> None:
        """lark 线程里调用：只留「改成勾上」的记录，投到主循环按数据表防抖；没装 pod-blanket 时不处理。"""
        try:
            event = getattr(data, "event", None)
            file_token = getattr(event, "file_token", None)
            table_id = getattr(event, "table_id", None)
            open_id = getattr(getattr(event, "operator_id", None), "open_id", None)
            if not all(isinstance(value, str) and _BITABLE_ID_RE.match(value) for value in (file_token, table_id)):
                return
            if not (isinstance(open_id, str) and _OPEN_ID_RE.match(open_id)):
                return
            records = _ticked_records(getattr(event, "action_list", None))
            if not records or not podctl_path().is_file():
                return
            self._schedule(self._pod_bitable_collect(file_token, table_id, records, open_id), name="pod_bitable", msg_id=table_id)
        except Exception:
            logger.exception("[Pod] bitable record event handling failed")

    async def _pod_bitable_collect(self, file_token: str, table_id: str, records: list[str], open_id: str) -> None:
        """主循环里：同一张数据表的改动先攒着，第一条到达后 POD_BITABLE_DEBOUNCE_SECONDS 秒一起交给引擎。"""
        pending: dict[tuple[str, str], dict[str, Any]] = self.__dict__.setdefault("_pod_bitable_pending", {})
        tasks: dict[tuple[str, str], asyncio.Task] = self.__dict__.setdefault("_pod_bitable_tasks", {})
        key = (file_token, table_id)
        entry = pending.setdefault(key, {"records": set(), "open_id": open_id})
        entry["records"].update(records)
        entry["open_id"] = open_id
        if key not in tasks:
            tasks[key] = asyncio.get_running_loop().create_task(self._pod_bitable_flush(key), name="pod_bitable_flush")

    async def _pod_bitable_flush(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(POD_BITABLE_DEBOUNCE_SECONDS)
        finally:
            self.__dict__.get("_pod_bitable_tasks", {}).pop(key, None)
        entry = self.__dict__.get("_pod_bitable_pending", {}).pop(key, None)
        if not entry:
            return
        file_token, table_id = key
        try:
            result = await self._pod().run(
                "table",
                "control",
                "--file",
                file_token,
                "--table",
                table_id,
                "--records",
                ",".join(sorted(entry["records"])),
                "--by",
                f"feishu:{entry['open_id']}",
            )
        except Exception as exc:
            logger.warning("[Pod] bitable control failed: %s", exc, exc_info=not isinstance(exc, PodctlError))
            return
        if result.get("started") or result.get("busy"):
            logger.info("[Pod] bitable control: %s", result)

    async def _pod_bitable_stop(self) -> None:
        """渠道停止时取消还在攒的记录变更（没交出去的那几秒，巡检兜底会接上）。"""
        tasks: dict[tuple[str, str], asyncio.Task] = self.__dict__.get("_pod_bitable_tasks", {})
        for task in list(tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        tasks.clear()
        self.__dict__.get("_pod_bitable_pending", {}).clear()

    # -- 定时巡检 -------------------------------------------------------------

    def _pod_timer_start(self) -> None:
        """渠道启动后调用（须在主事件循环里）；没装 pod-blanket 时不启动。"""
        global _timer_owner
        if _timer_owner is not None or not podctl_path().is_file():
            return
        _timer_owner = self
        self._pod_timer_task = asyncio.get_running_loop().create_task(self._pod_timer_loop(), name="pod_timer")

    async def _pod_timer_stop(self) -> None:
        global _timer_owner
        task = getattr(self, "_pod_timer_task", None)
        if task is None:
            return
        self._pod_timer_task = None
        if _timer_owner is self:
            _timer_owner = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _pod_timer_loop(self) -> None:
        delay = POD_TIMER_INTERVAL_SECONDS
        while True:
            await asyncio.sleep(delay)
            try:
                result = await self._pod().run("tick", "--auto")
            except Exception as exc:
                # 引擎出错时退避，免得每分钟刷一遍同样的错误
                delay = min(delay * 2, POD_TIMER_MAX_BACKOFF_SECONDS)
                logger.warning("[Pod] auto tick failed, retry in %ss: %s", delay, exc, exc_info=not isinstance(exc, PodctlError))
                continue
            delay = POD_TIMER_INTERVAL_SECONDS
            if not result.get("skipped"):
                logger.info("[Pod] auto tick: %s（%s）", result.get("decision"), result.get("reason"))
