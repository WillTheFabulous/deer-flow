"""飞书通知（日报 / 异常）：定时 run 的结果不会自动推到飞书，由引擎直接调开放平台发消息。

凭证 FEISHU_APP_ID / FEISHU_APP_SECRET 放在 secrets.env；接收方在 podflow.yaml 的 notify.feishu。
通知优先发交互卡片（带「打开工作台」「查看待审」按钮），发送失败时回落成纯文本。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx

from . import bg, directions, feishu, images, labels, store, ui
from .context import Ctx
from .stages.common import pattern_path, subject_label, task_label
from .util import canonical_json, dumps, loads, now_iso, parse_iso, sha256_text, utcnow

# 按钮回调由 DeerFlow fork 的飞书渠道处理（backend/app/channels/fork/pod_cards.py），两边的动作名必须一致
CARD_ACTION_DASHBOARD = "pod_dashboard"
CARD_ACTION_REVIEWS = "pod_reviews"

_TEMPLATES = {
    "alert": "red",
    "attention": "orange",
    "paused": "orange",
    "normal": "blue",
}


class NotifyError(RuntimeError):
    pass


def _send(ctx: Ctx, msg_type: str, content: dict[str, Any]) -> dict[str, Any]:
    conf = ctx.setting("notify.feishu", {}) or {}
    if not conf.get("enabled"):
        return {"sent": False, "reason": "notify.feishu.enabled 为 false"}
    receive_id = conf.get("receive_id")
    if not receive_id:
        raise NotifyError("notify.feishu.receive_id 未配置")
    try:
        with feishu.Client(ctx) as api:
            data = api.call(
                "POST",
                "/im/v1/messages",
                params={"receive_id_type": conf.get("receive_id_type", "chat_id")},
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                },
            )
    except feishu.FeishuError as exc:
        raise NotifyError(str(exc)) from exc
    return {"sent": True, "message_id": data.get("message_id")}


def send_text(ctx: Ctx, text: str) -> dict[str, Any]:
    return _send(ctx, "text", {"text": text[:4000]})


def send_card(ctx: Ctx, card: dict[str, Any]) -> dict[str, Any]:
    return _send(ctx, "interactive", card)


def button(label: str, value: dict[str, Any], kind: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "value": value,
    }


def link_button(label: str, url: str, kind: str = "default") -> dict[str, Any]:
    """点了直接打开链接的按钮（不走回调）。"""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "url": url,
    }


DEFAULT_BUTTONS = (
    ("打开工作台", {"action": CARD_ACTION_DASHBOARD}, "primary"),
    ("查看待审", {"action": CARD_ACTION_REVIEWS}, "default"),
)


def pod_card(
    title: str,
    lines: list[str],
    *,
    alerts: list[str] | None = None,
    notes: list[str] | None = None,
    level: str = "normal",
    buttons: list[tuple[str, dict[str, Any], str]] | None = None,
    links: list[tuple[str, str, str]] | None = None,
    images: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """通知卡片。update_multi 让渠道可以在用户点按钮后原地把它换成工作台；links 是排在最前面的打开链接按钮；
    images 是 [(飞书 image_key, 图下写的字)]，并排放在正文下面。"""
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    if images:
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "bisect",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "img",
                                "img_key": key,
                                "alt": {"tag": "plain_text", "content": label},
                                "mode": "fit_horizontal",
                                "preview": True,
                            },
                            {"tag": "markdown", "content": label},
                        ],
                    }
                    for key, label in images
                ],
            }
        )
    if alerts:
        elements.append(
            {
                "tag": "markdown",
                "content": "**异常**\n" + "\n".join(f"- {a}" for a in alerts),
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                *(link_button(label, url, kind) for label, url, kind in links or []),
                *(
                    button(label, value, kind)
                    for label, value, kind in (buttons or DEFAULT_BUTTONS)
                ),
            ],
        }
    )
    if notes:
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "；".join(notes)}],
            }
        )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": _TEMPLATES.get(level, "blue"),
        },
        "elements": elements,
    }


def upload_image(ctx: Ctx, data: bytes, name: str = "image.jpg") -> str | None:
    """把一张图传到飞书（消息图片），返回 image_key；没开通知或上传失败时返回 None（卡片就不放图）。"""
    if not (ctx.setting("notify.feishu", {}) or {}).get("enabled"):
        return None
    try:
        with feishu.Client(ctx) as api:
            data = api.call(
                "POST",
                "/im/v1/images",
                data={"image_type": "message"},
                files={"image": (name, data, "image/jpeg")},
            )
    except (feishu.FeishuError, httpx.HTTPError, OSError):
        return None
    return data.get("image_key") or None


def _seed_images(ctx: Ctx, seed_id: str) -> list[tuple[str, str]]:
    """卡片上并排放原图和提取出的平面图案（各一张缩略图）；还没有图案或上传失败时不放。"""
    row = ctx.db.q1("SELECT original_path FROM seeds WHERE seed_id = ?", (seed_id,))
    pattern = pattern_path(ctx, seed_id)
    if not row or not pattern:
        return []
    out = []
    for stored, label in ((row["original_path"], "原图"), (pattern, "提取的图案")):
        try:
            thumb = images.thumbnail_jpeg(store.ensure_local(ctx, stored).read_bytes())
        except (OSError, store.StorageError):
            return []
        key = upload_image(ctx, thumb, f"{seed_id}.jpg")
        if not key:
            return []
        out.append((key, f"{label} {task_label(ctx, seed_id)}"))
    return out


def send_pod_card(ctx: Ctx, card: dict[str, Any], fallback_text: str) -> dict[str, Any]:
    """先发卡片；卡片被拒（如权限或格式问题）时回落成纯文本，两次都失败才报错。"""
    try:
        return send_card(ctx, card)
    except NotifyError as card_error:
        try:
            sent = send_text(ctx, fallback_text)
        except NotifyError as text_error:
            raise NotifyError(f"卡片：{card_error}；文本：{text_error}") from text_error
        sent["card_error"] = str(card_error)
        return sent


def dashboard_card(ctx: Ctx) -> dict[str, Any]:
    view = ui.dashboard(ctx)
    return pod_card(
        view["title"],
        view["lines"],
        alerts=view["alerts"],
        notes=view["notes"],
        level=view["level"],
    )


def daily_card(ctx: Ctx, day: str | None = None) -> dict[str, Any]:
    view = ui.daily_view(ctx, day)
    return pod_card(view["title"], view["lines"])


def notify_tick(ctx: Ctx, result: dict[str, Any]) -> dict[str, Any]:
    """巡检通知：只在有告警或需要人工时发；同样的内容在 notify.renotify_hours 内不重复发。

    每 10 分钟巡检一次，「试运行窗口未开」这类正常停止如果每次都发会刷屏，所以不发。
    精简通知（notify.style: brief）时不发，巡检发现的异常写进状态总结。
    """
    from . import digest  # 精简通知在 digest.py（它依赖本模块）

    if digest.enabled(ctx):
        return {"sent": False, "reason": "精简通知：巡检发现的异常写进状态总结"}
    if not result["alerts"] and result["decision"] != "needs_review":
        return {"sent": False, "reason": "无告警"}
    signature = sha256_text(
        canonical_json(
            {
                "decision": result["decision"],
                "reason": result["reason"],
                "alerts": sorted(result["alerts"]),
            }
        )
    )
    last = loads(ctx.db.get_meta("last_notify"), {}) or {}
    hours = float(ctx.setting("notify.renotify_hours", 6))
    if last.get("signature") == signature and utcnow() - parse_iso(
        last["at"]
    ) < dt.timedelta(hours=hours):
        return {
            "sent": False,
            "reason": f"与 {last['at']} 的通知相同，{hours:g} 小时内不重复发",
        }
    view = ui.dashboard(ctx)
    card = pod_card(
        "POD 巡检提醒",
        [f"**巡检结论**：{result['reason']}", *view["lines"]],
        alerts=result["alerts"],
        level="alert" if result["alerts"] else "attention",
    )
    try:
        sent = send_pod_card(ctx, card, result["summary"])
    except NotifyError as exc:
        return {"sent": False, "error": str(exc)}
    if sent.get("sent"):
        with ctx.db.tx() as conn:
            ctx.db.set_meta(
                conn, "last_notify", dumps({"signature": signature, "at": now_iso()})
            )
    return sent


# ---------------------------------------------------------------- 后台任务完成卡片

_OPEN = {"action": CARD_ACTION_DASHBOARD}


def _review_counts(ctx: Ctx) -> dict[str, int]:
    rows = ctx.db.q(
        "SELECT subject_type, COUNT(*) FROM reviews WHERE status = 'open' GROUP BY subject_type"
    )
    return {r[0]: r[1] for r in rows}


# 完成卡片每段最多列几个方向标题，其余在挑选卡里看
_PLAN_TITLES = 6


def _auto_lines(plan: dict[str, Any]) -> list[str]:
    """方向已自动确认：一共出几张图、出图开始了还是在排队、不想要的怎么停。"""
    auto = plan.get("auto") or {}
    if auto.get("started"):
        state = "后台已开始出图"
    elif auto.get("reason"):
        state = f"{auto['reason']}，恢复后接着出"
    elif auto.get("queued"):
        state = "已排进正在出图的队列，有空的工位就接着出"
    else:
        state = "巡检会接着出"
    lines = [
        f"共 **{auto.get('images', 0)}** 张图：{state}，一直出到这 {auto.get('picked', 0)} 个方向全部出完，出完发卡片"
    ]
    backlog = auto.get("backlog")
    if backlog:
        lines.append(
            f"待审积压 {backlog['count']} 已到上限 {backlog['limit']}：新方向先排着，选款处理掉一些后自动接着出"
        )
    lines.append(
        f"不想要的方向在 POD 对话里说「{plan.get('batch_id')} 停掉 3、5」（只能停还没开始出图的）"
    )
    return lines


def _plan_lines(plan: dict[str, Any]) -> list[str]:
    """策划结果：小变化 / 大变化分两段列方向标题与改动方位，再说丢弃原因和没出来的维度。"""
    lines = []
    if plan.get("hint"):
        lines.append(f"这一轮的要求：{plan['hint']}")
    auto = bool(plan.get("auto"))
    if plan.get("accepted"):
        counts = f"小变化 {plan.get('small', 0)} · 大变化 {plan.get('big', 0)}"
        if plan.get("combo"):
            counts += f"（含三处组合 {plan['combo']}）"
        lines.append(
            f"**{plan.get('family_id')}** 提出 {plan['accepted']} 个裂变方向（{counts}），"
            + ("已全部自动确认：" if auto else "等你挑选：")
        )
        if auto:
            lines += _auto_lines(plan)
        for head, titles in (
            ("小变化", plan.get("titles") or []),
            ("大变化", plan.get("big_titles") or []),
        ):
            if not titles:
                continue
            lines.append(f"**{head}**")
            lines += titles[:_PLAN_TITLES]
            if len(titles) > _PLAN_TITLES:
                lines.append(
                    f"……另有 {len(titles) - _PLAN_TITLES} 个，在"
                    + ("任务明细" if auto else "挑选卡")
                    + "里看全"
                )
    if plan.get("auto_error"):
        lines.append(
            f"自动确认没成功（{plan['auto_error']}）：这一批留在待挑选，点「去挑选」手动确认"
        )
    if plan.get("unit_reason") and plan.get("accepted"):
        lines.append(plan["unit_reason"])
    if plan.get("failed_groups"):
        lines.append("这次没策划出来：" + "、".join(plan["failed_groups"]))
    dropped = plan.get("dropped") or {}
    if dropped:
        lines.append(
            "丢弃："
            + "，".join(
                f"{ui.DROP_REASON_LABELS.get(k, k)} {v}" for k, v in dropped.items()
            )
        )
    if plan.get("exhausted"):
        lines.append("探索空间已耗尽：需要新方向或新母版")
    return lines


def _pick_buttons(batch_id: str | None) -> list[tuple[str, dict[str, Any], str]]:
    return [
        ("去挑选", {"action": ui.ACTION_PLAN_VIEW, "batch": batch_id}, "primary"),
        ("打开工作台", _OPEN, "default"),
    ]


def _plan_buttons(
    plan: dict[str, Any], family_id: str | None
) -> list[tuple[str, dict[str, Any], str]]:
    """策划完成卡的按钮：自动确认了就看任务明细（方向出到哪了），否则去挑选。"""
    if not plan.get("auto"):
        return _pick_buttons(plan.get("batch_id"))
    family = family_id or plan.get("family_id")
    if not family:
        return [("打开工作台", _OPEN, "primary")]
    return [
        ("任务明细", {"action": ui.ACTION_HISTORY, "family": family}, "primary"),
        ("打开工作台", _OPEN, "default"),
    ]


def bg_card(
    ctx: Ctx, task: dict[str, Any], summary: dict[str, Any], *, ok: bool
) -> dict[str, Any] | None:
    """后台任务的完成 / 失败卡片；选中的方向还在接着出时返回 None（出完再发）。"""
    kind, subject = task["kind"], task.get("subject") or ""
    label = bg.KIND_LABELS.get(kind, kind)
    if not ok:
        lines = [
            f"**{label}没有完成**：{summary.get('error', '未知错误')}",
            summary.get("detail") or "",
            f"日志：`{task.get('log_path')}`",
        ]
        return pod_card(
            f"{label}失败｜{labels.ref(ctx, subject)}".rstrip("｜"),
            [line for line in lines if line],
            level="alert",
            buttons=[("打开工作台", _OPEN, "primary")],
        )
    if kind == "plan":
        lines = _plan_lines(summary)
        if summary.get("accepted"):
            head = (
                "裂变方向已自动确认，开始出图"
                if summary.get("auto")
                else "裂变方向已策划好"
            )
            return pod_card(
                f"{head}｜{subject_label(ctx, summary.get('batch_id'))}",
                lines,
                buttons=_plan_buttons(summary, summary.get("family_id")),
            )
        family = summary.get("family_id")
        return pod_card(
            f"没有策划出新方向｜{subject_label(ctx, family)}".rstrip("｜"),
            lines or ["这一轮没有通过去重与范围校验的方向"],
            level="attention",
            buttons=[
                (
                    "任务明细",
                    {"action": ui.ACTION_HISTORY, "family": family},
                    "primary",
                ),
                ("打开工作台", _OPEN, "default"),
            ]
            if family
            else [("打开工作台", _OPEN, "primary")],
        )
    if kind == "derive":
        plans = [p for p in summary.get("plans") or [] if p.get("accepted")]
        if not plans:
            # 自动派生没出方向（没有能当底图的小变化等）：只在动态里，不单独发卡
            return None
        plan = plans[0]
        head = (
            "二次大变化已自动确认，开始出图"
            if plan.get("auto")
            else "二次大变化已策划好"
        )
        return pod_card(
            f"{head}｜{subject_label(ctx, plan.get('batch_id'))}",
            _plan_lines(plan)
            + [f"底图：{plan['derived_from']} 里换了主体的小变化"]
            * bool(plan.get("derived_from")),
            buttons=_plan_buttons(plan, plan.get("family_id")),
        )
    if kind == "fix" and "fix_cards" not in summary:
        # 旧版 worker 的修改任务：进程结束时才发修改卡片
        return _fix_card(ctx, subject, summary)
    if kind == "accept":
        return _accept_card(ctx, subject, summary)
    if kind == "dispatch":
        return _dispatch_card(ctx, subject, summary)
    if kind == "extract":
        lines = [
            f"**{task_label(ctx, subject)}** 的平面图案已提取（{summary.get('model')}）：之后的图生图和质检都用这张图案",
            "拆解和系列规则没有重做；要按新图案重新拆解，在 POD 对话里说「"
            f"{subject} 按新图案重新拆解」",
        ]
        if summary.get("hint"):
            lines.append(f"这次的要求：{summary['hint']}")
        return pod_card(
            f"图案已提取｜{task_label(ctx, subject)}",
            lines,
            buttons=[
                ("任务列表", {"action": ui.ACTION_TASKS}, "primary"),
                ("打开工作台", _OPEN, "default"),
            ],
            images=_seed_images(ctx, subject),
        )
    if kind == "describe":
        return _describe_card(ctx, subject, summary)
    if kind in ("worker", "fix"):
        # 交接给了接班的 / 选中的方向还在接着出 / 别的 worker 在跑 / 只做了修改（修改卡片已逐张发过）：不发
        if summary.get("continued") or summary.get("busy") or summary.get("fix_only"):
            return None
        counts = _review_counts(ctx)
        pending = [
            f"{name} {counts[t]}"
            for t, name in (
                ("asset_selection", "选款"),
                ("asset_qc", "质检待审"),
                ("call_reconcile", "结果未知对账"),
            )
            if counts.get(t)
        ]
        lines = [
            f"处理了 {summary.get('jobs', 0)} 个任务；"
            f"{ui.STOP_REASON_LABELS.get(summary.get('stop_reason'), summary.get('stop_reason') or '')}",
            *directions.progress_lines(ctx),
            "待你处理：" + ("、".join(pending) if pending else "无"),
        ]
        buttons = [
            ("查看待审", {"action": CARD_ACTION_REVIEWS}, "primary"),
            ("打开工作台", _OPEN, "default"),
        ]
        if summary.get("batch_reports"):
            lines.append(
                "已生成批次反馈："
                + "、".join(subject_label(ctx, b) for b in summary["batch_reports"])
            )
            # 一批出完了：顺手给「再策划新方向」，进任务明细写要求
            row = ctx.db.q1(
                "SELECT family_id FROM batches WHERE batch_id = ?",
                (summary["batch_reports"][0],),
            )
            if row:
                buttons.insert(
                    1,
                    (
                        "再策划新方向",
                        {"action": ui.ACTION_HISTORY, "family": row["family_id"]},
                        "default",
                    ),
                )
        return pod_card("出图一轮结束", lines, buttons=buttons)
    family = summary.get("family_id")
    version = summary.get("family_version")
    plan = summary.get("plan") or {}
    described = (
        [
            f"画面描述没写成（{summary['description_error']}）：策划和出图指令先按拆解写；"
            f"在 POD 对话说「{subject} 重新写画面描述」补写"
        ]
        if summary.get("description_error")
        else []
    )
    if summary.get("units"):
        described.append(_units_line(summary["units"]))
    if plan.get("accepted"):
        # 拆解 / 修订后自动策划了方向：系列规则随方向一起看、一起认可（开着自动确认时已一并认可）
        done = "已开始出图" if plan.get("auto") else "方向已策划好"
        head = (
            f"母版拆解完成，{done}｜{subject_label(ctx, subject)}"
            if kind == "analyze"
            else f"系列已修订，{done}｜{subject_label(ctx, family)}"
        )
        named = f"{family} v{version}" if version else family
        rule = (
            f"系列规则 **{named}**：已随方向自动认可；识别错了就进入 POD 对话说修改意见（修订后会重新策划、自动出图）"
            if plan.get("auto")
            else f"系列规则 **{named}**：确认方向时一并认可；识别错了就进入 POD 对话说修改意见"
        )
        return pod_card(
            head,
            [rule, *_plan_lines(plan), *described],
            buttons=_plan_buttons(plan, family),
            # 母版拆解完成：并排看原图和提取出的平面图案
            images=_seed_images(ctx, subject) if kind == "analyze" else None,
        )
    if kind == "analyze" and not family:
        lines = [
            f"**{task_label(ctx, subject)}** 停在 {summary.get('stopped_at') or '母版阶段'}，状态 {summary.get('status')}"
        ]
        return pod_card(
            f"母版拆解未完成｜{subject_label(ctx, subject)}",
            lines,
            level="attention",
            buttons=[
                ("查看待审", {"action": CARD_ACTION_REVIEWS}, "primary"),
                ("打开工作台", _OPEN, "default"),
            ],
        )
    title = (
        f"母版拆解完成｜{subject_label(ctx, subject)}"
        if kind == "analyze"
        else f"系列已修订｜{subject_label(ctx, family)}"
    )
    seed_images = _seed_images(ctx, subject) if kind == "analyze" else None
    lines = [
        f"系列规则 **{family} v{version}** 已生成，待你批准；要改就进入 POD 对话说修改意见",
        *described,
    ]
    buttons = [
        (
            "查看待批准的系列",
            {"action": CARD_ACTION_REVIEWS, "type": "family"},
            "primary",
        ),
        ("打开工作台", _OPEN, "default"),
    ]
    if summary.get("plan_error") or plan:
        # 自动策划失败或没产出：系列照常可看，给一个重新策划的按钮
        lines = [
            f"系列规则 **{family} v{version}** 已生成",
            "策划裂变方向没成功："
            + (summary.get("plan_error") or "这一轮没有通过去重与范围校验的方向"),
            *_plan_lines(plan),
            *described,
        ]
        buttons = [
            (
                "重新策划",
                {"action": "pod_plan", "family_id": family},
                "primary",
            ),
            (
                "查看系列规则",
                {"action": CARD_ACTION_REVIEWS, "type": "family"},
                "default",
            ),
            ("打开工作台", _OPEN, "default"),
        ]
        return pod_card(
            title, lines, level="attention", buttons=buttons, images=seed_images
        )
    return pod_card(title, lines, buttons=buttons, images=seed_images)


def _describe_card(ctx: Ctx, subject: str, summary: dict[str, Any]) -> dict[str, Any]:
    """画面描述写好了：一张图写总述；给全部补写的列补了哪几张、哪几张没写成。"""
    buttons = [
        ("任务列表", {"action": ui.ACTION_TASKS}, "primary"),
        ("打开工作台", _OPEN, "default"),
    ]
    if "described" in summary:
        lines = [
            "画面描述已补写："
            + (
                "、".join(task_label(ctx, s) for s in summary["described"])
                if summary["described"]
                else "没有要补的"
            )
        ]
        if summary.get("skipped"):
            lines.append(f"已有描述、这次没重写的 {summary['skipped']} 张")
        if summary.get("failed"):
            lines.append("没写成：" + "；".join(summary["failed"][:5]))
        lines.append("之后的策划、出图指令和质检都照画面描述写")
        return pod_card(
            "画面描述已补写",
            lines,
            level="attention" if summary.get("failed") else "normal",
            buttons=buttons,
        )
    if summary.get("skipped"):
        lines = [f"**{task_label(ctx, subject)}** {summary['skipped']}"]
    else:
        lines = [
            f"**{task_label(ctx, subject)}** 的画面描述已写好：{summary.get('summary') or ''}",
            f"主体 {summary.get('subjects') or 0} 个；之后的策划、出图指令和质检都照它写",
        ]
        if summary.get("units"):
            lines.append(_units_line(summary["units"]))
        if summary.get("hint"):
            lines.append(f"这次的要求：{summary['hint']}")
    return pod_card(
        f"画面描述已写好｜{task_label(ctx, subject)}", lines, buttons=buttons
    )


def _units_line(units: list[str]) -> str:
    """可改的主体：小变化每次改其中两处（单元 × 字段），分错了在 POD 对话说怎么分。"""
    return (
        f"可改的主体：{'、'.join(units)}；分得不对就在 POD 对话说"
        "（如「星星和月亮算一组」）"
    )


def _accept_card(ctx: Ctx, seed_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    """候选表验收完成：选中 / 没选几张，选中的款有没有交给 mooya 出套图。"""
    lines = [
        f"选中 **{summary.get('approved', 0)}** 张 · 没选 {summary.get('rejected', 0)} 张"
    ]
    if summary.get("already_decided"):
        lines.append(f"聊天框里已经定了的 {summary['already_decided']} 张照聊天框的算")
    if summary.get("not_in_table"):
        lines.append(f"表里找不到的 {summary['not_in_table']} 张没动，还在选款里")
    if summary.get("fresh"):
        lines.append(f"刚进表、没勾的 {summary['fresh']} 张留到下次验收（还是待选）")
    if summary.get("waiting"):
        lines.append(f"还没进表的 {summary['waiting']} 张新图留到下次验收")
    lines += _accept_scene_lines(summary.get("scenes") or {})
    backlog = summary.get("backlog") or {}
    if backlog.get("limit"):
        lines.append(
            f"待审积压 {backlog.get('before')} → {backlog.get('after')}（上限 {backlog['limit']}）"
        )
    if summary.get("generation") == "started":
        lines.append("排队的出图（包括之前被积压挡住的）已接着出")
    elif summary.get("generation") == "queued":
        lines.append("排队的出图已排进正在出图的队列，有空的工位就接着出")
    if summary.get("source") == "table":
        lines.append("这次是在候选表最上面「▶ 选款完成」那一行提交的，结果也写在那一行")
    links: list[tuple[str, str, str]] = []
    if summary.get("table_url"):
        links.append(("打开候选表", summary["table_url"], "default"))
    return pod_card(
        f"选款验收完成｜{task_label(ctx, seed_id)}",
        lines,
        buttons=[
            ("任务列表", {"action": ui.ACTION_TASKS}, "default"),
            ("打开工作台", _OPEN, "default"),
        ],
        links=links or None,
    )


def _accept_scene_lines(scenes: dict[str, Any]) -> list[str]:
    """验收卡片上套图那一行。"""
    if not scenes.get("enabled"):
        return []
    if scenes.get("error"):
        return [f"套图没开始：{scenes['error']}"]
    if not scenes.get("auto"):
        missing = scenes.get("missing") or 0
        return (
            [f"选中的款还有 {missing} 款没出套图：任务列表点「出套图」"]
            if missing
            else []
        )
    if scenes.get("queued"):
        return [
            f"已把选中的 **{scenes['queued']}** 款交给 mooya 出套图（场景图），出完发卡片，"
            "缩略图和下载链接写进选款表；候选表最上面的套图行也会列出下载链接"
        ]
    return ["选中的款都出过或正在出套图，这次没有新交的"]


def _dispatch_card(ctx: Ctx, seed_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    """候选表「选中的先出套图」完成：这次定为选中几张、交给 mooya 几款；不算选款完成。"""
    lines = [
        f"勾上的新图定为选中 **{summary.get('approved', 0)}** 张（没勾的不动，还是待选；不算选款完成）"
    ]
    if summary.get("already_decided"):
        lines.append(f"聊天框里已经定了的 {summary['already_decided']} 张照聊天框的算")
    scenes = summary.get("scenes") or {}
    if not summary.get("picked"):
        lines.append("候选表里没有勾上、出了图的款：先在「选中」列打勾再点")
    elif scenes.get("enabled") is False:
        lines.append("套图没打开（scenes.enabled），这次只定了选中")
    elif scenes.get("error"):
        lines.append(f"套图没开始：{scenes['error']}")
    elif scenes.get("queued"):
        lines.append(
            f"已把 **{scenes['queued']}** 款交给 mooya 出套图（场景图），出完发卡片，候选表最上面会列出下载链接"
        )
    else:
        lines.append("勾上的款都出过或正在出套图，这次没有新交的")
    lines.append(
        "图全部出完以后再点「选款完成」：勾上的算选中、没勾的算不要，剩下选中的都接着出套图"
    )
    if summary.get("source") == "table":
        lines.append(
            "这次是在候选表最上面「▶ 选中的先出套图」那一行提交的，结果也写在那一行"
        )
    links: list[tuple[str, str, str]] = []
    if summary.get("table_url"):
        links.append(("打开候选表", summary["table_url"], "default"))
    return pod_card(
        f"已提前出套图｜{task_label(ctx, seed_id)}",
        lines,
        buttons=[
            ("任务列表", {"action": ui.ACTION_TASKS}, "default"),
            ("打开工作台", _OPEN, "default"),
        ],
        links=links or None,
    )


def _fix_card(ctx: Ctx, asset_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    """选款里人工要求的修改跑完：新图回到选款，或原图放回选款并写明原因。"""
    child = ctx.db.q1(
        "SELECT asset_id FROM assets WHERE parent_asset_id = ? ORDER BY asset_id DESC LIMIT 1",
        (asset_id,),
    )
    asset = ctx.db.q1(
        "SELECT status, qc_json FROM assets WHERE asset_id = ?", (asset_id,)
    )
    failed = (loads(asset["qc_json"], {}) or {}).get("fix_failed") if asset else None
    name = labels.asset(ctx, asset_id)
    if child:
        title, level = f"修改完成｜{name}", "normal"
        lines = [f"新图 **{labels.asset(ctx, child['asset_id'])}** 已回到选款，等你挑"]
    elif failed:
        title, level = f"修改没成功｜{name}", "attention"
        lines = [f"原图已放回选款：{failed}"]
    else:
        title, level = f"修改还没完成｜{name}", "attention"
        state_label = ui._status("assets", asset["status"]) if asset else "未知"
        lines = [f"当前状态：{state_label}；下一轮出图会接着做"]
    if int(summary.get("jobs") or 0) > 1:
        lines.append(f"这一轮一共处理了 {summary['jobs']} 个修改任务，结果都在选款里")
    return pod_card(
        title,
        lines,
        level=level,
        buttons=[
            (
                "查看选款",
                {"action": CARD_ACTION_REVIEWS, "type": "asset_selection"},
                "primary",
            ),
            ("打开工作台", _OPEN, "default"),
        ],
    )


def fix_finished(ctx: Ctx, asset_id: str) -> dict[str, Any]:
    """worker 的工位做完一个选款里要求的修改：马上发「修改完成 / 没成功 / 还没完成」卡片。"""
    from . import digest  # 精简通知的卡片在 digest.py（它依赖本模块）

    if digest.enabled(ctx):
        return digest.fix_finished(ctx, asset_id)
    card = _fix_card(ctx, asset_id, {"jobs": 1})
    fallback = (
        card["header"]["title"]["content"] + "\n" + card["elements"][0]["content"]
    )
    try:
        return send_pod_card(ctx, card, fallback)
    except NotifyError as exc:
        return {"sent": False, "error": str(exc)}


def bg_finished(
    ctx: Ctx, task: dict[str, Any], summary: dict[str, Any], *, ok: bool
) -> dict[str, Any]:
    from . import digest  # 精简通知的卡片在 digest.py（它依赖本模块）

    if digest.enabled(ctx):
        return digest.bg_finished(ctx, task, summary, ok=ok)
    card = bg_card(ctx, task, summary, ok=ok)
    if card is None:
        if summary.get("busy"):
            return {
                "sent": False,
                "reason": "已有出图在跑，排队的任务由它接着领",
            }
        if summary.get("fix_only") and not summary.get("continued"):
            return {"sent": False, "reason": "修改的结果已逐张发过卡片"}
        return {"sent": False, "reason": "选中的方向还在接着出，出完再通知"}
    fallback = (
        card["header"]["title"]["content"] + "\n" + card["elements"][0]["content"]
    )
    try:
        return send_pod_card(ctx, card, fallback)
    except NotifyError as exc:
        return {"sent": False, "error": str(exc)}
