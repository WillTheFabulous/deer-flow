"""飞书工作台的视图数据：引擎决定显示什么、有哪些按钮，渠道只负责画成卡片。

输出与渲染无关：标题、markdown 行、图片绝对路径、按钮（决策值 + 中文标签 + 样式）。
按钮只覆盖不需要附言的决策；needs_changes 这类必须写修改意见的，提示进入 POD 对话说明。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

from . import (
    bg,
    candidates,
    directions,
    feishu_table,
    images,
    labels,
    packages,
    report,
    review,
    scenes,
    shelf,
    store,
    tasks,
)
from .context import Ctx
from .fields import dimension_name
from .fields import kinds as field_kinds
from .fields import label as field_label
from .stages.common import (
    EDIT_LABEL,
    EDIT_SUFFIX,
    MODE_LABELS,
    fanout_slots,
    get_family,
    model_label,
    reference_path,
    slot_mode,
    subject_label,
    task_label,
    variant_modes,
)
from .stages.plan import (
    DERIVED,
    HINT_CHARS,
    SMALL_KEEP,
    STATE_LABELS,
    asset_label,
    asset_label_of,
    brief_line,
    counts_text,
    direction_summary,
    direction_tag,
    legacy_families,
    plan_button_label,
    plannable_families,
    proposed_batches,
    slot_counts,
)
from .util import loads, parse_iso

# 挑选卡、任务列表卡与任务明细卡的按钮动作、表单字段前缀，与渠道（backend/app/channels/fork/pod_cards.py）约定一致
ACTION_PLAN_VIEW = "pod_plan_view"
ACTION_PLAN_PICK = "pod_plan_pick"
ACTION_PLAN_CONFIRM = "pod_plan_confirm"
ACTION_PLAN_PICK_ALL = "pod_plan_pick_all"  # 挑选卡的全选 / 全不选（on=1 / 0）
ACTION_PLAN_REJECT = "pod_plan_reject"
ACTION_HISTORY = "pod_history"  # 任务明细卡（原方向记录卡，旧卡片上的按钮照常能用）
ACTION_TASKS = "pod_tasks"
ACTION_ACCEPT = (
    "pod_accept"  # 任务列表上的「选款完成」：回读候选表的勾选，选中的款接着出套图
)
ACTION_SCENES = "pod_scenes"  # 任务列表上的「出套图」：选中了还没出（或没出成）的款交给 mooya 出场景图
# 任务列表上的「选中的先出套图」：图还没出完时，候选表里勾上的款先定为选中、出套图（不算选款完成）
ACTION_DISPATCH = "pod_dispatch"
# 「B… 已下载」：标这个批次的交付包已下载（任务列表、历史任务、任务明细、套图出完卡），满足条件当场归档
ACTION_DOWNLOADED = "pod_downloaded"
ACTION_SHELF = "pod_shelf"  # 历史任务卡（已归档的任务，可以搜）
ACTION_ARCHIVE = "pod_archive"  # 任务列表上的「归档」：从任务列表收起（有待选时不给按钮，要在 POD 对话里强制归档）
ACTION_RESTORE = "pod_restore"  # 历史任务卡 / 归档卡片上的「调出」：回到任务列表
BACK_TASKS = "tasks"  # 策划按钮带 back=tasks：提交后回到任务列表
BACK_SHELF = "shelf"  # 调出按钮带 back=shelf：调出后回到历史任务卡
SHELF_QUERY_FIELD = "shelf_q"  # 历史任务卡的搜索框（名字 / 编号 / 日期）
SHELF_QUERY_CHARS = 20
SHELF_PAGE_SIZE = 4
PICK_FIELD_PREFIX = "pick_"  # 挑选表单 pick_B003，勾选框 pick_V0012
HINT_FIELD_PREFIX = "hint_"  # 任务列表 / 任务明细卡里「这次想要的方向」输入框 hint_F01
# 挑选卡、任务明细卡里方向列表的文字预算（UTF-8 字节）：飞书卡片整体约 30KB，留出卡片结构、表头和按钮的余量；
# 超了挑选卡只留标题，任务明细少放几批、改紧凑行
CARD_TEXT_BUDGET = 18_000

TYPE_LABELS = {
    "seed_rights": "素材授权",
    "seed_intake": "参考图接入",
    "plan_selection": "裂变方向",
    "family": "系列规则",
    "asset_qc": "质检待审",
    "asset_selection": "选款",
    "call_reconcile": "结果未知对账",
    "family_exhausted": "系列耗尽",
    "proposal": "方向提案",
}

# 每类审核项的按钮：(决策, 标签, 样式)；样式只用 primary / danger / default
DECISION_BUTTONS: dict[str, list[tuple[str, str, str]]] = {
    "seed_rights": [
        ("approved", "已获授权", "primary"),
        ("restricted", "授权受限", "danger"),
        ("unknown", "保持未知", "default"),
    ],
    "seed_intake": [("approved", "接入", "primary"), ("rejected", "不接入", "danger")],
    "family": [("approved", "批准系列", "primary"), ("rejected", "驳回", "danger")],
    "asset_qc": [("approved", "放行", "primary"), ("rejected", "淘汰", "danger")],
    "asset_selection": [
        ("approved", "选中", "primary"),
        ("rejected", "不要", "danger"),
    ],
    "call_reconcile": [
        ("not_charged", "未扣费，重新提交", "primary"),
        ("charged", "已扣费", "danger"),
    ],
    "family_exhausted": [("acknowledged", "知道了", "default")],
    "proposal": [("approved", "采纳", "primary"), ("rejected", "不采纳", "danger")],
    # 裂变方向在挑选卡里勾选、确认（links 里的「去挑选」），待审页不放决策按钮
    "plan_selection": [],
}

HINTS = {
    "plan_selection": "点「去挑选」打开挑选卡：勾选要出图的方向，点「按选中的方向出图」；都不满意就「换一批」。",
    "seed_rights": "已获授权或保持未知：文生图和图生图都会做，图生图会把这张参考图发给生图模型；授权受限：不再拆解，也不做图生图。",
    "family": "要改系列规则：进入 POD 对话说修改意见，会按意见重做系列（S04）。",
    "asset_qc": "要定点修复：进入 POD 对话说「{rid} 需要修改：……」。",
    "asset_selection": "需要修改：在输入框写清楚改什么，点「修改」，改好的新图会回到选款（也可以在 POD 对话里说「{rid} 需要修改：……」）。",
    "call_reconcile": "先到供应商后台按请求查记录，再选是否扣费。",
    "proposal": "采纳后会自动按提案修订系列，新版本需要你批准。",
}

DROP_REASON_LABELS = {
    "duplicate_plan": "与历史计划重复",
    "duplicate_direction": "和做过的方向一样",
    "unknown_slot": "不是这一轮的槽位",
    "duplicate_slot": "同一个槽位出了两个",
    "slot_mismatch": "没把槽位里的字段都改到",
    "duplicate_swap": "同一批里换成了同一个主体",
    "combo_mismatch": "组合没凑齐两个不同维度的可变字段",
    # 梳理字段（2026-10-16）之前的丢弃原因，旧批次还会显示
    "outside_variable_domains": "超出系列变化域",
    "no_visual_change": "没有画面变化",
    "control_quota_exceeded": "单轴对照超出配额",
    "over_batch_limit": "超出批次上限",
}
STOP_REASON_LABELS = {
    "queue_empty": "队列已空",
    "deadline": "到了单次时限，剩下的下一轮继续",
    "limit": "领满了这次的任务数，剩下的交给下一个 worker",
    "lifetime": "跑满时长，已交接给新的 worker",
    "reload": "配置或引擎代码有更新，已交接给新的 worker",
    "paused": "流水线已暂停",
    "lease_lost": "租约丢失",
}


def worker_line(ctx: Ctx, worker: dict[str, Any] | None) -> str:
    """工作台「后台出图」一行：几条工位、在出几个方向、生图名额占了几个。"""
    if not (worker and worker.get("alive")):
        return "**后台出图**：空闲"
    info = worker.get("info") or {}
    parts = []
    if info.get("lanes"):
        parts.append(
            f"{info['lanes']} 条工位，在出 {len(info.get('busy') or [])} 个方向"
        )
    image = (info.get("limits") or {}).get("image")
    if image:
        parts.append(f"生图 {image['busy']}/{image['size']}")
    parts.append(f"租约到 {_local_time(ctx, worker.get('lease_until'))}")
    return f"**后台出图**：运行中（{'，'.join(parts)}）"


_STATUS_LABELS = {
    "jobs": {
        "ready": "待推进",
        "running": "进行中",
        "blocked": "阻塞",
        "waiting_external": "等外部结果",
        "complete": "完成",
        "failed": "失败",
    },
    "assets": {
        "generated": "待质检",
        "qc_pass": "质检通过",
        "repair_pending": "待修复",
        "repair_unknown": "修复结果未知",
        "preview_ready": "预览就绪",
        "archived": "已入库",
        "rejected": "淘汰",
        "failed": "失败",
        "superseded": "已被新版本取代",
    },
    "families": {
        "proposed": "待批准",
        "approved": "已批准",
        "rejected": "已驳回",
        "superseded": "已被新版本取代",
        "exhausted": "已耗尽",
    },
    "batches": {
        "planning": "策划中",
        "proposed": "方向待挑选",
        "planned": "已选方向",
        "running": "进行中",
        "done": "已完成",
        "failed": "失败",
        "empty": "没有新方向",
        "rejected": "方向都没选中",
        "superseded": "已被新一轮策划取代",
        "cancelled": "方向全部停掉了",
    },
}

_REASON_CHARS = 160
_ITEM_CHARS = 120
_THUMB_SIDE = 512


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _status(kind: str, value: str) -> str:
    return _STATUS_LABELS.get(kind, {}).get(value, value)


def _counts_line(kind: str, counts: dict[str, int]) -> str:
    if not counts:
        return "无"
    return " · ".join(
        f"{_status(kind, k)} {v}"
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def _local_time(ctx: Ctx, iso: str | None) -> str:
    if not iso:
        return "-"
    tz = ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
    return parse_iso(iso).astimezone(tz).strftime("%m-%d %H:%M")


def _thumb(ctx: Ctx, stored: str | None, cache_key: str) -> str | None:
    """缩略图绝对路径：已有缩略图直接用；没有就从原图生成到 cache/thumbs/（只生成一次）。"""
    if not stored:
        return None
    try:
        src = store.ensure_local(ctx, stored)
    except store.StorageError:
        return None
    if not src.exists():
        return None
    if src.name.endswith(".thumb.jpg"):
        return str(src)
    out = ctx.home / "cache" / "thumbs" / f"{cache_key}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        # 渠道和人设可能同时打开同一页：各写各的临时文件再原子替换
        tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
        tmp.write_bytes(images.thumbnail_jpeg(src.read_bytes(), _THUMB_SIDE))
        os.replace(tmp, out)
    return str(out)


def _asset_thumb(ctx: Ctx, asset_id: str) -> str | None:
    row = ctx.db.q1(
        "SELECT path, thumb_path FROM assets WHERE asset_id = ?", (asset_id,)
    )
    if not row:
        return None
    return _thumb(ctx, row["thumb_path"], asset_id) or _thumb(
        ctx, row["path"], asset_id
    )


def _seed_ref(ctx: Ctx, seed_id: str) -> tuple[str | None, bool]:
    """卡片上放这张参考图的哪个版本：提取过平面图案就放图案。返回 (存储路径, 是不是图案)。"""
    row = ctx.db.q1("SELECT * FROM seeds WHERE seed_id = ?", (seed_id,))
    if not row:
        return None, False
    seed = dict(row)
    path = reference_path(ctx, seed)
    return path, path != seed["original_path"]


def _seed_thumb(ctx: Ctx, seed_id: str) -> str | None:
    path, pattern = _seed_ref(ctx, seed_id)
    if not path:
        return None
    # 图案的缩略图按图案文件缓存：重提取后换新图，不用旧缓存
    key = f"seed-{seed_id}-{Path(path).stem}" if pattern else f"seed-{seed_id}"
    return _thumb(ctx, path, key)


def _seed_label(ctx: Ctx, seed_id: str, word: str = "参考图") -> str:
    """缩略图下面的字：「原图 S01 名字」，放的是提取出的图案时写「图案 S01 名字」。"""
    name = task_label(ctx, seed_id)
    return f"图案 {name}" if _seed_ref(ctx, seed_id)[1] else f"{word} {name}"


def _review_type_counts(ctx: Ctx) -> list[dict[str, Any]]:
    rows = ctx.db.q(
        "SELECT subject_type, COUNT(*) FROM reviews WHERE status = 'open' GROUP BY subject_type"
    )
    counts = {r[0]: r[1] for r in rows}
    order = list(TYPE_LABELS)
    return [
        {"type": t, "label": TYPE_LABELS.get(t, t), "count": counts[t]}
        for t in sorted(
            counts, key=lambda t: order.index(t) if t in order else len(order)
        )
    ]


# ---------------------------------------------------------------- 工作台


def dashboard(ctx: Ctx) -> dict[str, Any]:
    snap = report.snapshot(ctx)
    lines: list[str] = []
    if snap["paused"]:
        reason = snap.get("pause_reason")
        lines.append(
            "**状态**：已暂停" + (f"（{_clip(reason, 60)}）" if reason else "")
        )
    else:
        lines.append("**状态**：运行中")
    trial = snap["trial"]
    if trial["active"]:
        lines.append(f"**试运行窗口**：开启中，至 {_local_time(ctx, trial['ends_at'])}")
    else:
        lines.append("**试运行窗口**：未开启（定时巡检不会派发新任务）")
    worker = snap["worker"]
    lines.append(worker_line(ctx, worker))
    if snap["families"]:
        fams = "；".join(
            f"{subject_label(ctx, f['family_id'])} v{f['family_version']} {_status('families', f['status'])}"
            for f in snap["families"]
        )
        lines.append(f"**系列**：{fams}")
    if snap["batches"]:
        batch = snap["batches"][0]
        unit = "个方向" if batch["status"] == "proposed" else "个任务"
        lines.append(
            f"**最近批次**：{subject_label(ctx, batch['batch_id'])}（{batch['family_id']}，{batch['size']} {unit}）{_status('batches', batch['status'])}"
        )
    lines.append(f"**任务**：{_counts_line('jobs', snap['jobs'])}")
    lines.append(f"**素材**：{_counts_line('assets', snap['assets'])}")
    review_types = _review_type_counts(ctx)
    total = sum(t["count"] for t in review_types)
    lines.append(
        "**待审**："
        + (
            " · ".join(f"{t['label']} {t['count']}" for t in review_types)
            if review_types
            else "无"
        )
    )
    b = snap["budget"]
    if b["enforced"]:
        lines.append(
            f"**今日预算**：已占用 {b['today_committed']}/{b['daily_cap']} {b['currency']}"
        )
    else:
        lines.append(f"**今日调用**：{b['calls_today']} 次（不计费）")
    level = "alert" if snap["alerts"] else ("paused" if snap["paused"] else "normal")
    return {
        "title": "POD 毛毯工作台",
        "level": level,
        "lines": lines,
        "alerts": snap["alerts"],
        "notes": snap["notes"],
        "paused": snap["paused"],
        "open_reviews": total,
        "review_types": review_types,
        "worker_alive": bool(worker and worker.get("alive")),
        "trial_active": trial["active"],
        "schedule_enabled": bool(ctx.setting("schedule.enabled", True)),
        # 选款表（飞书多维表格）建好后，固定按钮里多一个「选款表」直接打开它
        "table_url": feishu_table.table_url(ctx),
        **_guidance(ctx, snap, review_types),
    }


# ---------------------------------------------------------------- 下一步与进度


def _action(
    action: str, label: str, style: str = "default", **params: Any
) -> dict[str, Any]:
    return {"action": action, "label": label, "style": style, **params}


def _busy_families(ctx: Ctx, running: list[dict[str, Any]]) -> set[str]:
    """正在策划的系列：裂变策划、系列修订、或它的参考图还在拆解（拆完会自动策划）。"""
    families = {t["subject"] for t in running if t["kind"] in ("plan", "revise")}
    seeds = [t["subject"] for t in running if t["kind"] == "analyze" and t["subject"]]
    if seeds:
        marks = ",".join("?" for _ in seeds)
        families |= {
            r["family_id"]
            for r in ctx.db.q(
                f"SELECT DISTINCT family_id FROM families WHERE seed_id IN ({marks})",
                tuple(seeds),
            )
        }
    return families


def _task_lines(ctx: Ctx, running: list[dict[str, Any]]) -> list[str]:
    lines = []
    for task in running:
        label = bg.KIND_LABELS.get(task["kind"], task["kind"])
        subject = f"{labels.ref(ctx, task['subject'])} " if task.get("subject") else ""
        step = f"：{task['step']}" if task.get("step") else ""
        lines.append(f"{subject}{label}中{step}（已用 {task['elapsed_minutes']} 分钟）")
    return lines


def _picked_count(batch: dict[str, Any]) -> int:
    return len((loads(batch.get("plan_json"), {}) or {}).get("picked") or [])


def _progress(ctx: Ctx, running: list[dict[str, Any]], worker_alive: bool) -> list[str]:
    lines = _task_lines(ctx, running)
    if worker_alive and not any(t["kind"] in ("worker", "fix") for t in running):
        lines.append("出图中（前台 worker）")
    for batch in proposed_batches(ctx):
        lines.append(
            f"{subject_label(ctx, batch['batch_id'])}（{batch['family_id']}）：{batch['size']} 个方向待挑选，已选 {_picked_count(batch)} 个"
        )
    for batch in ctx.db.q(
        "SELECT batch_id, family_id, size FROM batches WHERE status IN ('planned', 'running') ORDER BY batch_id"
    ):
        counts = {
            r[0]: r[1]
            for r in ctx.db.q(
                "SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY status",
                (batch["batch_id"],),
            )
        }
        done = counts.get("complete", 0) + counts.get("failed", 0)
        parts = [f"已完成 {done}"]
        for status, label in (
            ("running", "进行中"),
            ("ready", "待出图"),
            ("blocked", "暂停中"),
        ):
            if counts.get(status):
                parts.append(f"{label} {counts[status]}")
        lines.append(
            f"{subject_label(ctx, batch['batch_id'])}（{batch['family_id']}，{batch['size']} 个任务）："
            + "，".join(parts)
        )
    return lines


def _guidance(
    ctx: Ctx, snap: dict[str, Any], review_types: list[dict[str, Any]]
) -> dict[str, Any]:
    """首页的「下一步」：告诉人现在卡在哪、该点什么；按钮参数由渠道原样回传。"""
    counts = {t["type"]: t["count"] for t in review_types}
    running = bg.running(ctx)
    worker = snap["worker"]
    worker_alive = bool(worker and worker.get("alive")) or any(
        t["kind"] in ("worker", "fix") for t in running
    )
    planning = _busy_families(ctx, running)
    steps: list[dict[str, Any]] = []

    def add(text: str, *actions: dict[str, Any]) -> None:
        steps.append({"text": text, "actions": list(actions)})

    if snap["paused"]:
        add(
            "流水线已暂停：恢复后才会继续推进", _action("pod_resume", "恢复", "primary")
        )
    rights = counts.get("seed_rights", 0) + counts.get("seed_intake", 0)
    if rights:
        seed_type = "seed_rights" if counts.get("seed_rights") else "seed_intake"
        add(
            f"有 {rights} 张参考图待你确认接入",
            _action("pod_reviews", "去确认", "primary", type=seed_type),
        )
    for batch in proposed_batches(ctx)[:3]:
        fam = ctx.db.q1(
            "SELECT seed_id FROM families WHERE family_id = ? AND family_version = ?",
            (batch["family_id"], batch["family_version"]),
        )
        source = (
            f"{task_label(ctx, fam['seed_id'])} 的"
            if fam
            else f"{batch['family_id']} 的"
        )
        add(
            f"{source} {batch['size']} 个裂变方向待你挑选（已选 {_picked_count(batch)} 个）：选中的才出图",
            _action(ACTION_PLAN_VIEW, "去挑选", "primary", batch=batch["batch_id"]),
        )
    if counts.get("family"):
        add(
            "有系列规则待你批准；要改就进入 POD 对话说修改意见",
            _action("pod_reviews", "去批准", "primary", type="family"),
        )
    if running:
        add("后台正在处理：" + "；".join(_task_lines(ctx, running)))
    plannable = [f for f in plannable_families(ctx) if f not in planning]
    pending_families = {
        r["family_id"]
        for r in ctx.db.q("SELECT family_id FROM families WHERE status = 'pending'")
    }
    planned_families = {
        r["family_id"] for r in ctx.db.q("SELECT DISTINCT family_id FROM variants")
    }
    auto = directions.auto_confirm_enabled(ctx)
    for family_id in plannable[:3]:
        slot_n = slot_counts(ctx, family_id)
        family = subject_label(ctx, family_id)
        if family_id not in pending_families:
            text = f"系列 {family} 可以再策划一轮裂变方向"
        elif auto:
            text = f"系列 {family} 还没有裂变方向：按可变字段策划一轮，策划完自动全部出图（系列一并认可）"
        else:
            text = f"系列 {family} 还没有裂变方向：按可变字段策划一轮给你挑（确认方向时一并认可系列）"
        if slot_n:
            text += f"（{counts_text(slot_n)}）"
        buttons = [
            _action(
                "pod_plan",
                plan_button_label(slot_n, auto=auto),
                "primary",
                family_id=family_id,
            )
        ]
        if family_id in planned_families:
            # 任务明细里能看到做过的方向，也能写一句要求再策划
            buttons.append(
                _action(ACTION_HISTORY, "任务明细", "default", family=family_id)
            )
        add(text, *buttons)
    for legacy in [f for f in legacy_families(ctx) if f["family_id"] not in planning][
        :3
    ]:
        # 按旧字段字典拆的系列：重新拆解（只重跑 S02–S04，系列与方向记录接着算）后才能按字段裂变
        add(
            f"系列 {subject_label(ctx, legacy['family_id'])} 是按旧字段拆的：进入 POD 对话说"
            f"「{legacy['seed_id']} 按新字段重新拆解」，之后按可变字段出方向",
            _action("pod_enter_chat", "进入 POD 对话", "default"),
        )
    waiting = int(
        ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('ready', 'blocked')")
        or 0
    )
    if waiting and not worker_alive:
        add(f"{waiting} 个任务等着出图", _action("pod_worker", "开始出图", "primary"))
    asset_types = [
        (t, TYPE_LABELS[t])
        for t in ("asset_selection", "asset_qc", "call_reconcile")
        if counts.get(t)
    ]
    if asset_types:
        add(
            "待你处理："
            + "、".join(f"{label} {counts[t]}" for t, label in asset_types),
            *[
                _action("pod_reviews", f"处理{label}", "default", type=t)
                for t, label in asset_types[:3]
            ],
        )
    if counts.get("proposal"):
        add(
            "批次反馈提出了新的方向提案，待你决定",
            _action("pod_reviews", "看提案", "default", type="proposal"),
        )
    if packages.enabled(ctx):
        archived = shelf.archived_seeds(ctx)
        for seed_id, views in sorted(
            packages.by_seed(ctx).items(),
            key=lambda kv: -int("".join(c for c in kv[0] if c.isdigit()) or 0),
        ):
            ready = [v["batch_id"] for v in views if _downloadable(v)]
            if not ready or seed_id in archived:
                continue
            add(
                f"{task_label(ctx, seed_id)} 的交付包 {'、'.join(ready)} 打好了："
                "下载后点「已下载」，任务才会归档",
                *(
                    _action(
                        ACTION_DOWNLOADED,
                        f"{b} 已下载",
                        "default",
                        seed=seed_id,
                        batch=b,
                    )
                    for b in ready[:2]
                ),
            )
            if len(steps) >= 12:
                break
    exhausted = [
        subject_label(ctx, f["family_id"])
        for f in snap["families"]
        if f["status"] == "exhausted"
    ]
    if exhausted:
        add(
            f"系列 {'、'.join(exhausted)} 已耗尽：上传新参考图，或进入 POD 对话扩大系列范围",
            _action("pod_enter_chat", "进入 POD 对话", "default"),
        )
    if not ctx.db.scalar("SELECT COUNT(*) FROM seeds"):
        add(
            "还没有参考图：进入 POD 对话，发一张爆款参考图开始",
            _action("pod_enter_chat", "进入 POD 对话", "primary"),
        )
    # 人选中的方向自己会出到出完；连续运行只对还在排队的其他任务有用
    if (
        bool(ctx.setting("schedule.enabled", True))
        and not snap["trial"]["active"]
        and not snap["paused"]
        and waiting
    ):
        minutes = int(ctx.setting("schedule.tick_interval_minutes", 10))
        add(
            f"开启连续运行后，后台每 {minutes} 分钟自动推进一次（需要你授权）",
            _action("pod_trial_start", "开启 24 小时连续运行", "default"),
        )
    if not steps:
        add("暂时没有需要你处理的事")
    return {"next_steps": steps, "progress": _progress(ctx, running, worker_alive)}


# ---------------------------------------------------------------- 待审


def _asset_info(ctx: Ctx, asset_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT a.variant_id, a.model, a.slot_id, a.repair_depth, v.axis, v.plan_json, f.seed_id FROM assets a"
        " LEFT JOIN variants v ON v.variant_id = a.variant_id"
        " LEFT JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " WHERE a.asset_id = ?",
        (asset_id,),
    )
    if not row:
        return None
    info = dict(row)
    plan = loads(info.pop("plan_json"), {}) or {}
    info["direction"] = direction_summary(plan, info["axis"])
    # 二次大变化：底图（父方向出的那张小变化图），选款时放在候选前面
    base = plan.get("base")
    info["base_asset_id"] = (
        base.get("asset_id")
        if isinstance(base, dict) and base.get("asset_id")
        else None
    )
    return info


def _asset_lines(asset: dict[str, Any] | None, item: dict[str, Any]) -> list[str]:
    """选款项：这张候选属于哪个裂变方向（改了哪几处、什么不变），由哪个模型、哪种方式出的图。"""
    if not asset:
        return []
    direction = asset["direction"]
    tag = direction_tag(direction["task_type"], direction["aspects"], direction["kind"])
    head = " ".join(p for p in (asset["variant_id"], direction["title"]) if p)
    if tag:
        head += f"（{tag}）"
    if item.get("batch_id"):
        head += ("· " if tag else " · ") + item["batch_id"]
    lines = [f"方向：{head}"]
    lines += [f"· {brief_line(b)}" for b in direction["brief"]]
    if direction["keep"]:
        lines.append(f"不变：{direction['keep']}")
    made = f"出图：{model_label(asset['model'])} · {MODE_LABELS[slot_mode(asset['slot_id'])]}"
    if asset["repair_depth"]:
        made += f" · 修复 {asset['repair_depth']} 次"
    lines.append(made)
    return lines


def review_view(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any]:
    kind = item["subject_type"]
    rid = item["review_id"]
    subject = item["subject_id"]
    payload = item.get("payload") or {}
    lines: list[str] = []
    imgs: list[dict[str, str]] = []
    # 跳到别的卡片的按钮（如裂变方向的「去挑选」），渠道原样作回调参数
    links: list[dict[str, Any]] = []

    if kind.startswith("asset_"):
        asset = _asset_info(ctx, subject)
        lines += _asset_lines(asset, item)
        # 选款时先看这个任务的原图（参考图），再看候选：改了多少、是否太像；二次大变化先看底图（换了主体的小变化图）
        base_thumb = (
            _asset_thumb(ctx, asset["base_asset_id"])
            if kind == "asset_selection" and asset and asset.get("base_asset_id")
            else None
        )
        if base_thumb:
            imgs.append(
                {
                    "path": base_thumb,
                    "label": f"底图 {labels.asset(ctx, asset['base_asset_id'])}",
                }
            )
        elif kind == "asset_selection" and asset and asset["seed_id"]:
            seed_thumb = _seed_thumb(ctx, asset["seed_id"])
            if seed_thumb:
                imgs.append(
                    {
                        "path": seed_thumb,
                        "label": _seed_label(ctx, asset["seed_id"], "原图"),
                    }
                )
        thumb = _asset_thumb(ctx, subject)
        if thumb:
            imgs.append({"path": thumb, "label": f"候选 {labels.asset(ctx, subject)}"})
    if kind == "asset_selection":
        if payload.get("qc_unchecked"):
            lines.append("未经质检：" + _clip(payload["qc_unchecked"], _ITEM_CHARS))
        if payload.get("qc_flags"):
            lines.append(
                "质检存疑：" + _clip("；".join(payload["qc_flags"]), _REASON_CHARS)
            )
        if payload.get("fixed_from"):
            lines.append(
                f"修改自 {labels.asset(ctx, payload['fixed_from'])}："
                + _clip(payload.get("fix_note"), _ITEM_CHARS)
            )
        if payload.get("fix_failed"):
            lines.append("上次修改没成功：" + _clip(payload["fix_failed"], _ITEM_CHARS))
        unverified = payload.get("unverified_checks") or []
        if unverified:
            lines.append("未自动核验：" + _clip("；".join(unverified), _ITEM_CHARS))
        if payload.get("qc_override"):
            lines.append("质检说明：" + _clip(payload["qc_override"], _ITEM_CHARS))
    elif kind == "asset_qc":
        checks = payload.get("checks") or []
        for c in checks[:3]:
            if isinstance(c, dict):
                lines.append(
                    f"- {_clip(c.get('criterion'), 40)}：{_clip(c.get('reason'), 80)}"
                )
    elif kind in ("seed_rights", "seed_intake"):
        seed = ctx.db.q1(
            "SELECT source_ref, source_note, rights_status FROM seeds WHERE seed_id = ?",
            (subject,),
        )
        if seed:
            src = "；".join(
                _clip(s, 60) for s in (seed["source_ref"], seed["source_note"]) if s
            )
            if src:
                lines.append(f"来源：{src}")
            lines.append(f"当前授权：{seed['rights_status']}")
        thumb = _seed_thumb(ctx, subject)
        if thumb:
            imgs.append({"path": thumb, "label": _seed_label(ctx, subject)})
    elif kind == "family":
        family_id, _, version = subject.partition("@v")
        fam = get_family(ctx, family_id, int(version) if version.isdigit() else None)
        spec = fam.get("spec") or {}
        if spec.get("positioning"):
            lines.append("定位：" + _clip(spec["positioning"], _ITEM_CHARS))
        if spec.get("preserve"):
            lines.append(
                "保持不变："
                + _clip("、".join(_rule_text(p) for p in spec["preserve"]), _ITEM_CHARS)
            )
        domains = spec.get("variable_domains") or []
        if domains:
            lines.append(
                f"可变字段 {len(domains)} 个："
                + _clip(
                    "、".join(field_label(d.get("field_id")) for d in domains),
                    _ITEM_CHARS,
                )
            )
        if spec.get("exclude"):
            lines.append(
                "排除："
                + _clip("、".join(_rule_text(p) for p in spec["exclude"]), _ITEM_CHARS)
            )
        thumb = _seed_thumb(ctx, fam["seed_id"])
        if thumb:
            imgs.append({"path": thumb, "label": _seed_label(ctx, fam["seed_id"])})
    elif kind == "proposal":
        for change in (payload.get("proposed_family_changes") or [])[:3]:
            lines.append(
                "- "
                + _clip(
                    change.get("change") if isinstance(change, dict) else change,
                    _ITEM_CHARS,
                )
            )
        experiments = payload.get("next_experiments") or []
        if experiments:
            lines.append(
                f"另有 {len(experiments)} 个下一轮实验建议（POD 对话里可展开）"
            )
    elif kind == "plan_selection":
        data = directions.show(ctx, subject)
        small = sum(1 for c in data["candidates"] if c["task_type"] == "small")
        split = (
            f"（小变化 {small} · 大变化 {len(data['candidates']) - small}）"
            if any(c["task_type"] in ("small", "big") for c in data["candidates"])
            else ""
        )
        lines.append(
            f"{len(data['candidates'])} 个候选方向{split}，已选 {len(data['picked'])} 个；选中的才出图"
        )
        for c in data["candidates"][:3]:
            aspects = f"（{'、'.join(c['aspects'])}）" if c["aspects"] else ""
            lines.append(f"{c['index']}. {_clip(c['title'], 20)}{aspects}")
        if len(data["candidates"]) > 3:
            lines.append(f"……另有 {len(data['candidates']) - 3} 个，在挑选卡里看全")
        seed_id = payload.get("seed_id")
        thumb = _seed_thumb(ctx, seed_id) if seed_id else None
        if thumb:
            imgs.append({"path": thumb, "label": _seed_label(ctx, seed_id)})
        links.append(_action(ACTION_PLAN_VIEW, "去挑选", "primary", batch=subject))
    elif kind == "family_exhausted":
        gaps = payload.get("coverage_gaps") or []
        if gaps:
            lines.append("覆盖缺口：" + _clip("；".join(map(str, gaps)), _ITEM_CHARS))
    elif kind == "call_reconcile":
        where = " · ".join(
            labels.ref(ctx, str(payload[k]))
            for k in ("job_id", "slot_id", "asset_id", "scope_id")
            if payload.get(k)
        )
        if where:
            lines.append(f"位置：{where}")
        if payload.get("error"):
            lines.append("错误：" + _clip(payload["error"], _ITEM_CHARS))

    if item.get("reason"):
        lines.append("原因：" + _clip(item["reason"], _REASON_CHARS))
    hint = HINTS.get(kind)
    tag = ""
    form = None
    if kind == "asset_selection":
        flags = payload.get("qc_flags") or []
        tag = (
            "（未经质检）"
            if payload.get("qc_unchecked")
            else ("（质检存疑）" if flags else "")
        )
        form = {
            "name": f"fix_{rid}",
            "input": f"note_{rid}",
            "placeholder": "要改什么？例：兔子耳朵改短、背景换成雾蓝",
            # 质检存疑的图预填疑点：直接点「修改」就按疑点改
            "default": _clip(
                "按质检疑点修正：" + "；".join(flags), review.FIX_NOTE_CHARS
            )
            if flags
            else "",
            "max_length": review.FIX_NOTE_CHARS,
            "submit_label": "修改",
            "decision": "needs_changes",
        }
    return {
        "review_id": rid,
        "type": kind,
        "type_label": TYPE_LABELS.get(kind, kind),
        "subject_id": subject,
        "title": f"{rid} · {TYPE_LABELS.get(kind, kind)} · {labels.ref(ctx, subject)}{tag}",
        "lines": lines,
        "images": imgs,
        "actions": [
            {"decision": d, "label": label, "style": style}
            for d, label, style in DECISION_BUTTONS.get(kind, [])
        ],
        "links": links,
        "form": form,
        "hint": hint.format(rid=rid) if hint else None,
    }


def reviews_page(
    ctx: Ctx,
    *,
    offset: int = 0,
    limit: int = 3,
    types: list[str] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    items = review.list_reviews(ctx, types=types, batch_id=batch_id)
    total = len(items)
    limit = max(1, min(limit, 10))
    offset = max(0, offset)
    if total and offset >= total:
        # 裁决掉最后一页的最后一项后退回上一页
        offset = (total - 1) // limit * limit
    return {
        "title": "POD 待审",
        "total": total,
        "offset": offset,
        "limit": limit,
        "types": list(types or []),
        "batch_id": batch_id,
        "type_counts": _review_type_counts(ctx),
        "items": [review_view(ctx, it) for it in items[offset : offset + limit]],
    }


# ---------------------------------------------------------------- 裂变方向挑选


def _rule_text(rule: Any) -> str:
    """系列规则里的一条（保留项等）：对象取 control 说明，字符串原样。"""
    if isinstance(rule, dict):
        return str(rule.get("control") or rule.get("rule") or rule.get("rule_id") or "")
    return str(rule)


def _seed_subjects(ctx: Ctx, seed_id: str) -> str:
    """拆解里识别出的主体（如「长颈鹿、小象、小熊」），给人核对有没有认错。"""
    row = ctx.db.q1(
        "SELECT result_json FROM analyses WHERE seed_id = ? ORDER BY analysis_version DESC LIMIT 1",
        (seed_id,),
    )
    if not row:
        return ""
    names = []
    for obj in (loads(row["result_json"], {}) or {}).get("objects") or []:
        if not isinstance(obj, dict) or obj.get("role") != "subject":
            continue
        attrs = obj.get("attributes") or {}
        name = attrs.get("category") or attrs.get("species")
        if name:
            names.append(str(name))
    return "、".join(names)


def _slots_text(ctx: Ctx, task_type: str | None = None) -> str:
    """一个方向出几张图：「6 张图（3 个模型 × 图生图 / 图生图 Edit）」；小变化按 small_change_modes。"""
    slots = fanout_slots(ctx, variant_modes(ctx, {"task_type": task_type}))
    models = len({model.removesuffix(EDIT_SUFFIX) for _, model, _ in slots})
    kinds = [MODE_LABELS[m] for m in ("t2i", "i2i") if any(s[2] == m for s in slots)]
    if any(model.endswith(EDIT_SUFFIX) for _, model, _ in slots):
        kinds.append(EDIT_LABEL)
    return f"{len(slots)} 张图（{models} 个模型 × {' / '.join(kinds)}）"


def _variation_lines(spec: dict[str, Any]) -> list[str]:
    """挑选卡顶部：这张图哪些维度不适用、系列锁定了哪些字段（按新字段拆过的系列才有）。"""
    variation = spec.get("variation") or {}
    lines = []
    off = variation.get("gated_off") or {}
    if off:
        lines.append(
            "不适用："
            + "；".join(f"{dimension_name(d)}（{why}）" for d, why in off.items())
        )
    # 只记录的字段本来就不出方向，不算「判定不能变」
    not_variable = [
        x["label"]
        for x in variation.get("not_variable") or []
        if x.get("dimension") not in off
        and x.get("label")
        and field_kinds(x.get("field_id"))
    ]
    if not_variable:
        lines.append(
            f"拆解判定不能变的字段 {len(not_variable)} 个："
            + _clip("、".join(not_variable), _ITEM_CHARS)
        )
    locked = spec.get("locked_fields") or []
    if locked:
        lines.append(
            "系列锁定："
            + _clip(
                "；".join(
                    f"{x.get('label') or x.get('field_id')}"
                    + (f"（{x['reason']}）" if x.get("reason") else "")
                    for x in locked
                ),
                _ITEM_CHARS,
            )
        )
    return lines


def directions_view(ctx: Ctx, batch_id: str) -> dict[str, Any]:
    """挑选卡：系列要点 + 每个候选方向（标题、改哪几处、不变的部分）。

    待挑选时是一张勾选清单：勾选只在飞书本地生效，点表单的提交按钮才把勾选结果一次交给引擎。
    """
    data = directions.show(ctx, batch_id)
    fam = get_family(ctx, data["family_id"], data["family_version"])
    spec = fam.get("spec") or {}
    editable = data["status"] == "proposed"
    cands = data["candidates"]
    picked = set(data["picked"])
    head = f"参考图 {task_label(ctx, fam['seed_id'])} · 系列 {fam['family_id']} v{fam['family_version']}"
    if fam["status"] == "pending" and editable:
        head += "（待认可：确认方向时一并认可）"
    lines = [head]
    kinds = Counter("small" if c["task_type"] == "small" else "big" for c in cands)
    smalls = [c for c in cands if c["task_type"] == "small"]
    # 2026-10-23 起按可变单元策划（简述带 unit_id）：一处是一个单元上的一个字段；
    # 之前按字段策划的批次 10-21 起小变化改两个字段，更早的是一个
    by_unit = any(
        isinstance(b, dict) and b.get("unit_id") for c in smalls for b in c["brief"]
    )
    small_fields = (
        "两个字段" if any(len(c["brief"]) > 1 for c in smalls) else "一个字段"
    )
    if by_unit:
        small_head = "按可变单元策划"
        small_how = "改两处：每处是一个单元上的一个字段，单元里的成员一起改"
        small_section = "改两处（单元·字段）"
    else:
        small_head = "按可变字段策划"
        small_how = f"改{small_fields}、同类元素一起改"
        small_section = f"改{small_fields}（同类元素一起改）"
    if any(c["task_type"] in ("small", "big") for c in cands):
        lines.append(
            f"{small_head}：小变化 {kinds['small']} 个（{small_how}，构图不动）· "
            f"大变化 {kinds['big']} 个"
        )
        if editable:
            lines += _variation_lines(spec)
    if data.get("hint"):
        lines.append("这一轮的要求：" + _clip(data["hint"], _ITEM_CHARS))
    # 待认可的系列才需要核对：第一版看识别出的主体，修订版看修订意见
    if fam["status"] == "pending" and editable:
        if fam["family_version"] == 1:
            subjects = _seed_subjects(ctx, fam["seed_id"])
            if subjects:
                lines.append(f"识别出的主体：{subjects}（认错了就进入 POD 对话说）")
        elif fam.get("revision_note"):
            lines.append("修订意见：" + _clip(fam["revision_note"], _ITEM_CHARS))
        if spec.get("preserve"):
            lines.append(
                "保持不变："
                + _clip("；".join(_rule_text(p) for p in spec["preserve"]), _ITEM_CHARS)
            )
    selection = data.get("selection") or {}
    status_line = {
        "proposed": f"已选 {len(picked)} / {len(cands)} 个方向",
        "planned": f"已确认：选了 {len(picked)} 个方向出图（{_who(selection.get('by'))}）",
        "running": f"已确认：选了 {len(picked)} 个方向出图（{_who(selection.get('by'))}）",
        "done": f"已出完：选了 {len(picked)} 个方向",
        "rejected": "这一批都没选中",
        "superseded": "系列有了新版本，这一批已作废",
    }.get(data["status"], _status("batches", data["status"]))
    items = []
    derived_n = sum(1 for c in cands if c.get("kind") == DERIVED)
    sections = {
        "small": f"小变化（{kinds['small']}）：{small_section}，{SMALL_KEEP}，每个出 {_slots_text(ctx, 'small')}",
        "big": f"大变化（{kinds['big'] - derived_n}）：每个出 {_slots_text(ctx, 'big')}",
        DERIVED: f"二次大变化（{derived_n}）：底图是换了主体的小变化图，每个出 {_slots_text(ctx, 'big')}",
    }
    for c in cands:
        tag = direction_tag(c["task_type"], c["aspects"], c.get("kind"))
        state = ""
        if not editable:
            state = (
                "已选"
                if c["picked"]
                else ("已停掉" if c.get("selection") == "cancelled" else "没选")
            )
            if c.get("job_status"):
                state += f"（{_status('jobs', c['job_status'])}）"
        small = c["task_type"] == "small"
        tiered = c["task_type"] in ("small", "big")
        items.append(
            {
                "variant_id": c["variant_id"],
                "index": c["index"],
                "title": c["title"],
                # 梳理前的批次没有分段；段标题已写小变化 / 大变化，标签只留改动的维度；
                # 小变化的「不变」在段标题里统一写
                "section": (
                    sections[DERIVED]
                    if c.get("kind") == DERIVED
                    else sections["small"]
                    if small
                    else sections["big"]
                )
                if tiered
                else None,
                "tag": tag.split(" · ", 1)[1] if tiered and " · " in tag else tag,
                "lines": [brief_line(b) for b in c["brief"]],
                "keep": "" if small else c["keep"],
                "picked": c["picked"],
                "state": state,
                "checker": {
                    "name": PICK_FIELD_PREFIX + c["variant_id"],
                    "checked": c["picked"],
                }
                if editable
                else None,
            }
        )
    if _text_bytes(items) > CARD_TEXT_BUDGET:
        # 方向太多会撑过飞书卡片约 30KB 的上限：只留标题和勾选框，改了什么去任务明细 / POD 对话里看
        for item in items:
            item["lines"], item["keep"] = [], ""
        lines.append(
            f"这一批 {len(cands)} 个方向，卡片只列标题；每个方向改了什么在「任务明细」或 POD 对话里看"
        )
    form = None
    actions = []
    pick_actions = []
    if editable:
        form = {
            "name": PICK_FIELD_PREFIX + batch_id,
            "submit": _action(
                ACTION_PLAN_CONFIRM, "按勾选的方向出图", "primary", batch=batch_id
            ),
        }
        pick_actions = [
            _action(ACTION_PLAN_PICK_ALL, "全选", "default", batch=batch_id, on=1),
            _action(ACTION_PLAN_PICK_ALL, "全不选", "default", batch=batch_id, on=0),
        ]
        actions.append(
            _action(ACTION_PLAN_REJECT, "都不满意，换一批", "default", batch=batch_id)
        )
    actions.append(
        _action(ACTION_HISTORY, "任务明细", "default", family=data["family_id"])
    )
    thumb = _seed_thumb(ctx, fam["seed_id"])
    return {
        "title": f"挑选裂变方向｜{subject_label(ctx, batch_id)}",
        "batch_id": batch_id,
        "family_id": data["family_id"],
        "status": data["status"],
        "editable": editable,
        "lines": lines,
        "status_line": status_line,
        "images": [{"path": thumb, "label": _seed_label(ctx, fam["seed_id"])}]
        if thumb
        else [],
        "items": items,
        "picked": len(picked),
        "total": len(cands),
        "form": form,
        "pick_actions": pick_actions,
        "actions": actions,
        "hint": (
            "勾选后点「按勾选的方向出图」才会提交，确认后会一直出到这几个方向全部出完；"
            "方向多时可以先点「全选」，再取消不要的几个；"
            "没选的方向以后不会再提。勾选框要飞书 7.9 及以上，看不到就在 POD 对话里说"
            f"「{batch_id} 选 1、3」；系列识别错了就进入 POD 对话说修改意见。"
        )
        if editable
        else None,
    }


# ---------------------------------------------------------------- 任务列表与任务明细

TASK_PAGE_SIZE = 4
HISTORY_PAGE_BATCHES = 3
# 任务列表开头最多列几个批次的交付包（新批次在前），更早的在任务明细和候选表里
PACKAGES_SHOWN = 3
_HISTORY_ASPECTS = 4
# 这些任务要人在 POD 对话里处理（重新拆解、修订系列、换母版）
_CHAT_STATUSES = frozenset(
    {"waiting_analyze", "analyze_failed", "restricted", "exhausted", "family_rejected"}
)


def _step_text(step: dict[str, Any]) -> str:
    if step["state"] == "done":
        return f"{step['text']} ✓"
    if step["state"] == "current":
        return f"**{step['text']}**"
    return step["text"]


def _steps_line(task: dict[str, Any]) -> str:
    """五步进度条：拆解 ✓ › 策划 ✓ › 挑选 ✓ › **出图 6/8** › 选款 待选 12。"""
    return " › ".join(_step_text(s) for s in task["steps"])


def _task_name(task: dict[str, Any]) -> str:
    """任务列表里的称呼：S01 森林三伙伴水彩 1020 · F01 v7（还没起名时只有编号）。"""
    label = f"{task['seed_id']} {task['name']}" if task.get("name") else task["seed_id"]
    if not task["family_id"]:
        return label
    return f"{label} · {task['family_id']} v{task['family_version']}"


def _task_status(ctx: Ctx, task: dict[str, Any]) -> str:
    """「已完成（10-26 15:30 出完）」；到了套图完成 / 套图已下载这两档时写那一刻的时间。"""
    label = task["status_label"]
    if task.get("stage"):
        if task.get("stage_at"):
            label += f"（{_local_time(ctx, task['stage_at'])}）"
    elif task["finished_at"]:
        label += f"（{_local_time(ctx, task['finished_at'])} 出完）"
    return label


def _task_actions(task: dict[str, Any], *, detail: bool = True) -> list[dict[str, Any]]:
    """按任务状态给的按钮：去挑选 / 去确认 / 开始出图 / 去选款 / 看明细 / 进入 POD 对话。"""
    actions = []
    if task["pending_batch"]:
        actions.append(
            _action(
                ACTION_PLAN_VIEW,
                f"去挑选 {task['pending_batch']}",
                "primary",
                batch=task["pending_batch"],
            )
        )
    if task["status"] == "intake":
        actions.append(_action("pod_reviews", "去确认", "primary", type="seed_intake"))
    if task["worker_needed"]:
        actions.append(_action("pod_worker", "开始出图", "primary"))
    pending = (task["counts"] or {}).get("pending_selection")
    if pending:
        actions.append(
            _action(
                "pod_reviews",
                f"去选款（{pending}）",
                "primary" if task["status"] in ("done", "blocked") else "default",
                type="asset_selection",
            )
        )
    missing = (task["counts"] or {}).get("scenes_missing")
    if missing:
        actions.append(
            _action(
                ACTION_SCENES,
                f"出套图（{missing} 款）",
                "default" if pending else "primary",
                seed=task["seed_id"],
            )
        )
    if detail and task["family_id"]:
        actions.append(
            _action(ACTION_HISTORY, "看明细", "default", family=task["family_id"])
        )
    if task["status"] in _CHAT_STATUSES or task["can_plan"] is False:
        actions.append(_action("pod_enter_chat", "进入 POD 对话", "default"))
    # 手动归档：没有在做的事、也没有待选（有待选要在 POD 对话里说「强制归档」，剩下的待选会清出选款）
    if task.get("can_archive") and not pending:
        actions.append(_action(ACTION_ARCHIVE, "归档", "default", seed=task["seed_id"]))
    return actions


def _plan_form(
    ctx: Ctx, family_id: str, *, again: bool, wrap: str = "{}"
) -> dict[str, Any]:
    """「这次想要的方向（可选）+ 策划」表单；按钮写「（再）策划 n 个方向（并出图）」（wrap 再套一层说法），提交后回到任务列表。"""
    label = wrap.format(
        plan_button_label(
            slot_counts(ctx, family_id),
            again=again,
            auto=directions.auto_confirm_enabled(ctx),
        )
    )
    return {
        "name": f"plan_{family_id}",
        "input": {
            "name": HINT_FIELD_PREFIX + family_id,
            "placeholder": "这次想要的方向（可选），如：换成海洋动物、冬季主题",
            "max_length": HINT_CHARS,
        },
        "submit": _action(
            "pod_plan",
            label,
            "primary",
            family_id=family_id,
            back=BACK_TASKS,
        ),
    }


def _local_day(ctx: Ctx, iso: str) -> str:
    tz = ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
    return parse_iso(iso).astimezone(tz).strftime("%m-%d")


def _package_text(ctx: Ctx, view: dict[str, Any]) -> str:
    """一个批次的交付包：B010 7 款 · 227MB（01-21 前有效）/ B019 套图 3/7 出完，出完后打包。"""
    batch = view["batch_id"]
    if view["ready"]:
        text = f"{batch} {view['items']} 款 · {packages.size_text(view['bytes'])}"
        if view["expires_at"]:
            text += f"（{_local_day(ctx, view['expires_at'])} 前有效）"
        if view["busy"]:
            text += f"，还有 {view['busy']} 款在出，出完重打"
        elif view["stale"]:
            text += "，有新出完的款，马上重打"
        if view["parked"]:
            text += f"，{view['parked']} 款等人工"
        if view["downloaded"]:
            text += " · 已下载 ✓"
        elif view["download_outdated"]:
            text += "，重打过，下载新包后再标"
        return text
    if view["busy"] or view["parked"]:
        text = f"{batch} 套图 {view['done']}/{view['approved']} 出完，出完后打包"
        if view["parked"]:
            text += f"（{view['parked']} 款等人工）"
        return text
    if view["error"]:
        return f"{batch} 交付包没打成：{_clip(view['error'], 40)}"
    return f"{batch} 交付包正在打"


def _downloadable(view: dict[str, Any]) -> bool:
    """这个批次的交付包能标「已下载」了：打好了、是最新内容、这一版还没标。"""
    return bool(view["ready"] and not view["stale"] and not view["downloaded"])


def _downloaded_actions(
    task: dict[str, Any], views: list[dict[str, Any]], *, back: str | None = None
) -> list[dict[str, Any]]:
    """每个还没标的交付包一个「B… 已下载」按钮（back=shelf：点完回历史任务卡）。"""
    params: dict[str, Any] = {"seed": task["seed_id"]}
    if back:
        params["back"] = back
    return [
        _action(
            ACTION_DOWNLOADED,
            f"{v['batch_id']} 已下载",
            "default",
            batch=v["batch_id"],
            **params,
        )
        for v in views
        if _downloadable(v)
    ]


def _package_parts(
    ctx: Ctx, task: dict[str, Any], *, back: str | None = None
) -> tuple[str | None, list[dict[str, str]], list[dict[str, Any]]]:
    """任务列表开头那一行「交付包：…」（新批次在前，最多 PACKAGES_SHOWN 批）、下载交付包的链接按钮，
    和下载后点的「B… 已下载」按钮。"""
    views = task.get("packages") or []
    if not views:
        return None, [], []
    shown = views[:PACKAGES_SHOWN]
    line = "交付包：" + " · ".join(_package_text(ctx, v) for v in shown)
    if len(views) > len(shown):
        line += f" · 更早的 {len(views) - len(shown)} 批在任务明细"
    links = [
        {"label": f"交付包 {v['batch_id']}", "url": v["url"]}
        for v in shown
        if v["ready"] and v["url"]
    ]
    return line, links, _downloaded_actions(task, shown, back=back)


def _candidate_links(
    ctx: Ctx, task: dict[str, Any]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """任务的候选表：(链接按钮「候选表」, 选款按钮)；没开表格或还没建表时都没有。
    有待选时给「选款完成（待选 n）」（2026-10-30 起随时能点，「选中的先出套图」不再给）。"""
    if not feishu_table.enabled(ctx):
        return [], []
    info = candidates.table_info(ctx, task["seed_id"])
    if not info:
        return [], []
    url = candidates.table_link(ctx, task["seed_id"])
    links = [{"label": "候选表", "url": url}] if url else []
    counts = task["counts"] or {}
    pending = counts.get("pending_selection")
    if not pending:
        return links, []
    return links, [
        _action(
            ACTION_ACCEPT,
            f"选款完成（待选 {pending}）",
            "primary",
            seed=task["seed_id"],
        )
    ]


def tasks_view(
    ctx: Ctx, offset: int = 0, limit: int = TASK_PAGE_SIZE
) -> dict[str, Any]:
    """任务列表：一张参考图一个任务，写着走到哪一步、现在在做什么；已完成的可以写一句要求接着裂变。"""
    all_tasks = tasks.summaries(ctx)
    archived_n = tasks.archived_count(ctx)
    total = len(all_tasks)
    limit = max(1, min(limit, 10))
    offset = max(0, min(offset, (total - 1) // limit * limit)) if total else 0
    groups = Counter(t["group"] for t in all_tasks)
    lines: list[str] = []
    actions: list[dict[str, Any]] = []
    if all_tasks:
        head = f"共 {total} 张参考图：" + " · ".join(
            f"{label} {groups[g]}"
            for g, label in enumerate(tasks.GROUP_LABELS)
            if groups[g] or g < 3
        )
        pending = sum(
            t["counts"]["pending_selection"] for t in all_tasks if t["counts"]
        )
        if pending:
            head += f"｜选款待审 {pending}"
        unmarked = sum(
            1 for t in all_tasks for v in t.get("packages") or [] if _downloadable(v)
        )
        if unmarked:
            head += f"｜交付包待标已下载 {unmarked}"
        if archived_n:
            head += f"｜已归档 {archived_n}（在「历史任务」里）"
        lines.append(head)
    elif archived_n:
        lines.append(
            f"进行中的任务都处理完了：{archived_n} 个任务已归档，在「历史任务」里，可以调出来继续裂变"
        )
        actions.append(_action(ACTION_SHELF, f"历史任务（{archived_n}）", "primary"))
    else:
        lines.append("还没有参考图：进入 POD 对话，发一张爆款参考图开始")
        actions.append(_action("pod_enter_chat", "进入 POD 对话", "primary"))
    if ctx.db.get_meta("paused") == "1":
        lines.append("**流水线已暂停**：恢复后才会接着出图")
        actions.insert(0, _action("pod_resume", "恢复", "primary"))
    items = []
    for task in all_tasks[offset : offset + limit]:
        # 交付包放在这个任务的开头：第一行写各批次的包，下载按钮排在最前面
        package_line, package_links, downloaded = _package_parts(ctx, task)
        body = [package_line] if package_line else []
        body.append(_steps_line(task))
        if task["now"]:
            body.append("现在：" + "；".join(task["now"]))
        counts = task["counts"]
        if counts and counts["directions"]:
            body.append(
                f"{counts['batches']} 批 · {counts['directions']} 个方向 · 出图 {counts['images']} 张"
            )
        form = note = None
        if task["status"] in ("done", "idle") and task["can_plan"]:
            done = task["status"] == "done"
            form = _plan_form(
                ctx,
                task["family_id"],
                again=done,
                wrap="继续裂变（{}）" if done else "{}",
            )
        elif task["can_plan"] is False and task["plan_reason"]:
            note = task["plan_reason"]
        thumb = _seed_thumb(ctx, task["seed_id"])
        links, accept = _candidate_links(ctx, task)
        items.append(
            {
                "key": task["key"],
                "seed_id": task["seed_id"],
                "family_id": task["family_id"],
                "status": task["status"],
                "title": f"{_task_name(task)}　{_task_status(ctx, task)}",
                "lines": body,
                "images": [{"path": thumb, "label": _seed_label(ctx, task["seed_id"])}]
                if thumb
                else [],
                "actions": accept + downloaded + _task_actions(task),
                "links": package_links + links,
                "form": form,
                "note": note,
            }
        )
    nav = []
    if offset > 0:
        nav.append(
            _action(ACTION_TASKS, "上一页", "default", offset=max(0, offset - limit))
        )
    if offset + limit < total:
        nav.append(_action(ACTION_TASKS, "下一页", "default", offset=offset + limit))
    if archived_n and all_tasks:
        nav.append(_action(ACTION_SHELF, f"历史任务（{archived_n}）", "default"))
    return {
        "title": "POD 任务列表",
        "total": total,
        "offset": offset,
        "limit": limit,
        "lines": lines,
        "actions": actions,
        "items": items,
        "nav": nav,
        "hint": "一张参考图一个任务。出图是选中的方向出完了几个；选款只显示待选 / 选中张数，不影响完成。"
        "方向确认后「候选表」里就按出图张数占好了行（未生成），出图后原行补上图。选款在候选表里勾店铺："
        "勾了 5 分钟后自动定为选中、出套图（场景图）、上架（5 分钟内取消勾算撤销）；"
        "看完这一批点「选款完成」（或勾表最上面那一行）：勾上的算选中、没勾的算没选中（刚进表的新图留到下次），表名改成【选款完成】。"
        "每个批次出完打一个交付包（每款一个文件夹），点「交付包 B…」下载，下载后点「B… 已下载」"
        "（或勾候选表最上面那一行），任务从「套图完成」到「套图已下载」。"
        "已完成的任务可以写一句这次想要的方向（可不填），点「继续裂变」再策划一轮。"
        + (
            "做完的任务（选款完成、选中的款套图都出完"
            + ("、交付包都标了已下载" if packages.enabled(ctx) else "")
            + "）会归档到「历史任务」，随时能调出来继续裂变。"
            if shelf.enabled(ctx)
            else ""
        ),
    }


def _shelf_line(ctx: Ctx, task: dict[str, Any]) -> str:
    """已归档的任务：「已归档（10-25 自动归档）」+ 归档那一刻的进度。"""
    info = task.get("shelf") or {}
    when = _local_day(ctx, info["archived_at"]) if info.get("archived_at") else ""
    text = f"**已归档**（{when} {info.get('mode_label') or ''}".rstrip() + "）"
    if int(info.get("cycles") or 0) > 1:
        text += f"，第 {info['cycles']} 次"
    if info.get("folder_state") == shelf.FOLDER_IN:
        text += "；候选表在「已归档」文件夹"
    elif info.get("folder_error"):
        text += f"；候选表还没挪进文件夹：{_clip(info['folder_error'], 40)}"
    return text


def _restore_action(task: dict[str, Any], *, back: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"seed": task["seed_id"]}
    if back:
        params["back"] = back
    return _action(ACTION_RESTORE, "调出", "primary", **params)


def shelf_view(
    ctx: Ctx, offset: int = 0, limit: int = SHELF_PAGE_SIZE, query: str | None = None
) -> dict[str, Any]:
    """历史任务：已归档的任务，归档时间从新到旧；可以按名字、编号、日期搜。
    每个任务写着七步进度、交付包和候选表链接，带「调出」「看明细」和「这次想要的方向 + 继续裂变」表单
    （继续裂变会自动调出）。"""
    query = " ".join(str(query or "").split())[:SHELF_QUERY_CHARS]
    items_all = tasks.summaries(ctx, view="archived", query=query or None)
    total = len(items_all)
    archived_n = tasks.archived_count(ctx)
    limit = max(1, min(limit, 10))
    offset = max(0, min(offset, (total - 1) // limit * limit)) if total else 0
    lines = []
    if query:
        lines.append(
            f"搜「{query}」：{total} 个" + ("" if total else "，换个字或编号再搜")
        )
    elif archived_n:
        lines.append(
            f"已归档 {archived_n} 个任务（新归档的在前）：调出后回到任务列表，继续裂变会自动调出"
        )
    else:
        lines.append(
            "还没有归档的任务：选款完成、选中的款套图都出完"
            + ("、交付包都标了已下载" if packages.enabled(ctx) else "")
            + "的任务会自动归档，也可以在任务列表点「归档」"
        )
    items = []
    for task in items_all[offset : offset + limit]:
        package_line, package_links, downloaded = _package_parts(
            ctx, task, back=BACK_SHELF
        )
        body = [_shelf_line(ctx, task), _steps_line(task)]
        if package_line:
            body.append(package_line)
        counts = task["counts"]
        if counts and counts["directions"]:
            body.append(
                f"{counts['batches']} 批 · {counts['directions']} 个方向 · 出图 {counts['images']} 张"
            )
        form = note = None
        if task["family_id"] and task["can_plan"]:
            form = _plan_form(ctx, task["family_id"], again=True, wrap="继续裂变（{}）")
        elif task["can_plan"] is False and task["plan_reason"]:
            note = task["plan_reason"]
        thumb = _seed_thumb(ctx, task["seed_id"])
        links = []
        url = (
            candidates.table_link(ctx, task["seed_id"])
            if feishu_table.enabled(ctx)
            else None
        )
        if url:
            links.append({"label": "候选表", "url": url})
        actions = [_restore_action(task, back=BACK_SHELF), *downloaded]
        if task["family_id"]:
            actions.append(
                _action(ACTION_HISTORY, "看明细", "default", family=task["family_id"])
            )
        items.append(
            {
                "key": task["key"],
                "seed_id": task["seed_id"],
                "family_id": task["family_id"],
                "status": task["status"],
                "title": f"{_task_name(task)}　{_task_status(ctx, task)}",
                "lines": body,
                "images": [{"path": thumb, "label": _seed_label(ctx, task["seed_id"])}]
                if thumb
                else [],
                "actions": actions,
                "links": package_links + links,
                "form": form,
                "note": note,
            }
        )
    nav = []
    if offset > 0:
        nav.append(
            _action(
                ACTION_SHELF,
                "上一页",
                "default",
                offset=max(0, offset - limit),
                **({"q": query} if query else {}),
            )
        )
    if offset + limit < total:
        nav.append(
            _action(
                ACTION_SHELF,
                "下一页",
                "default",
                offset=offset + limit,
                **({"q": query} if query else {}),
            )
        )
    if query:
        nav.append(_action(ACTION_SHELF, "全部历史任务", "default"))
    nav.append(_action(ACTION_TASKS, "任务列表", "default"))
    return {
        "title": "历史任务",
        "total": total,
        "offset": offset,
        "limit": limit,
        "query": query,
        "lines": lines,
        "actions": [],
        "items": items,
        "nav": nav,
        "search": {
            "name": "shelf_search",
            "input": {
                "name": SHELF_QUERY_FIELD,
                "placeholder": "搜名字、编号或日期，如：恐龙、S03、1020",
                "max_length": SHELF_QUERY_CHARS,
                "value": query,
            },
            "submit": _action(ACTION_SHELF, "搜索", "default"),
        },
        "hint": "历史任务是选款完成、套图出完"
        + ("、交付包下载过" if packages.enabled(ctx) else "")
        + "后收起的任务：方向记录、候选表（在「已归档」文件夹）、"
        "交付包和选中的款都还在。调出后回到任务列表；写一句这次想要的方向点「继续裂变」会自动调出、再策划一轮，"
        "做过的和没选的方向照样避开。",
    }


def _latest_families(ctx: Ctx) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT f.family_id, f.family_version, f.seed_id, f.status FROM families f WHERE f.family_version = ("
            " SELECT MAX(f2.family_version) FROM families f2 WHERE f2.family_id = f.family_id)"
            " ORDER BY f.family_id"
        )
    ]


def _overview_counts(view: dict[str, Any]) -> str:
    stats = "、".join(f"{STATE_LABELS[k]} {v}" for k, v in view["counts"].items() if v)
    return f"做过 {view['total']} 个方向" + (f"：{stats}" if stats else "")


def _direction_row(d: dict[str, Any]) -> str:
    aspects = d["aspects"][:_HISTORY_ASPECTS]
    more = "…" if len(d["aspects"]) > _HISTORY_ASPECTS else ""
    where = f"（{'、'.join(aspects)}{more}）" if aspects else " "
    state = d.get("progress_text") or d["state_text"]
    return f"{d['index']}. {_clip(d['title'], 20)}{where}— {state}"


def _text_bytes(value: Any) -> int:
    """估算放进卡片的文字有多大（UTF-8 字节）：飞书卡片整体约 30KB 上限，CARD_TEXT_BUDGET 给卡片结构留了余量。"""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _fit_rows(batch: dict[str, Any], budget: int) -> list[str]:
    """任务明细里一批方向的行：放得下就照常写；放不下改成紧凑行（序号 · 标题 · 状态）；还放不下只列前面几行，
    末尾写其余几个方向各在哪一步、去 POD 对话里问。"""
    rows = [_direction_row(d) for d in batch["directions"]]
    if _text_bytes(rows) <= budget:
        return rows
    rows = [
        f"{d['index']}. {_clip(d['title'], 14)} — {d.get('progress_text') or d['state_text']}"
        for d in batch["directions"]
    ]
    if _text_bytes(rows) <= budget:
        return rows
    kept: list[str] = []
    for i, row in enumerate(rows):
        rest = batch["directions"][i:]
        counts = Counter(d["state_text"].split(" · ")[0] for d in rest)
        tail = (
            f"…其余 {len(rest)} 个方向（"
            + "、".join(f"{k} {n}" for k, n in counts.most_common(4))
            + f"）在 POD 对话里问「{batch['batch_id']} 的方向」"
        )
        if _text_bytes([*kept, row, tail]) > budget:
            return [*kept, tail]
        kept.append(row)
    return kept


def history_view(
    ctx: Ctx, family_id: str | None = None, offset: int = 0
) -> dict[str, Any]:
    """任务明细：这张参考图（系列）走到哪一步，做过和提过的全部方向各自细到哪一步（出图 4/9、质检中、待选 5……），
    可以带一句要求再策划一轮。

    不指定系列时：只有一个系列就直接显示它，多个就先列出系列。
    """
    if family_id is None:
        families = _latest_families(ctx)
        if len(families) != 1:
            items = []
            for fam in families:
                view = directions.overview(ctx, fam["family_id"])
                items.append(
                    {
                        "family_id": fam["family_id"],
                        "line": f"**{fam['family_id']}**（参考图 {task_label(ctx, fam['seed_id'])}）：{_overview_counts(view)}；选中的款 {view['approved_images']} 张",
                        "action": _action(
                            ACTION_HISTORY, "看明细", "default", family=fam["family_id"]
                        ),
                    }
                )
            return {
                "title": "任务明细",
                "kind": "families",
                "lines": [] if items else ["还没有参考图：进入 POD 对话发一张图开始"],
                "families": items,
            }
        family_id = families[0]["family_id"]
    view = tasks.family_detail(ctx, family_id)
    task = view["task"]
    bundles = {p["batch_id"]: p for p in task.get("packages") or []}
    offset = max(0, min(offset, max(len(view["batches"]) - 1, 0)))
    groups = []
    used = 0
    for batch in view["batches"][offset : offset + HISTORY_PAGE_BATCHES]:
        head = f"**{batch['batch_id']}**　{_local_time(ctx, batch['created_at'])} · {_status('batches', batch['status'])}"
        if batch.get("derived_from"):
            head += f" · 二次大变化（底图来自 {batch['derived_from']}）"
        if batch["jobs"]:
            head += f" · 选了 {batch['jobs']} 个，出完 {batch['jobs_done']} 个"
        bundle = bundles.get(batch["batch_id"])
        if bundle and bundle["ready"] and bundle["url"]:
            head += f" · [交付包 {bundle['items']} 款]({bundle['url']})"
            if bundle["downloaded"]:
                head += " 已下载 ✓"
            elif bundle["download_outdated"]:
                head += "（重打过，下载新包后再标）"
        elif bundle:
            head += " · " + _package_text(ctx, bundle).removeprefix(
                f"{batch['batch_id']} "
            )
        if batch["hint"]:
            head += f"\n要求：{_clip(batch['hint'], 60)}"
        group = {
            "batch_id": batch["batch_id"],
            "head": head,
            "lines": _fit_rows(batch, CARD_TEXT_BUDGET - used - _text_bytes(head)),
        }
        size = _text_bytes(group)
        # 一页最多 HISTORY_PAGE_BATCHES 批，并且不超过卡片的文字预算（至少放一批）
        if groups and used + size > CARD_TEXT_BUDGET:
            break
        groups.append(group)
        used += size
    nav = []
    if offset > 0:
        nav.append(
            _action(
                ACTION_HISTORY,
                "更新的批次",
                "default",
                family=family_id,
                offset=max(0, offset - HISTORY_PAGE_BATCHES),
            )
        )
    if offset + len(groups) < len(view["batches"]):
        nav.append(
            _action(
                ACTION_HISTORY,
                "更早的批次",
                "default",
                family=family_id,
                offset=offset + len(groups),
            )
        )
    available = view["availability"]
    form = _plan_form(ctx, family_id, again=True) if available["ok"] else None
    lines = [
        f"参考图 {task_label(ctx, view['seed_id'])} · 系列 {family_id} v{view['family_version']}　{_task_status(ctx, task)}",
        _steps_line(task),
    ]
    if task["now"]:
        lines.append("现在：" + "；".join(task["now"]))
    lines.append(f"{_overview_counts(view)}；选中的款 {view['approved_images']} 张")
    actions = _task_actions(task, detail=False) + _downloaded_actions(
        task, task.get("packages") or []
    )
    if task.get("archived"):
        lines.insert(1, _shelf_line(ctx, task))
        actions.insert(0, _restore_action(task, back=None))
    thumb = _seed_thumb(ctx, view["seed_id"])
    return {
        "title": f"任务明细｜{task_label(ctx, view['seed_id'])} · {family_id}",
        "kind": "family",
        "family_id": family_id,
        "lines": lines,
        "images": [{"path": thumb, "label": _seed_label(ctx, view["seed_id"])}]
        if thumb
        else [],
        "groups": groups,
        "nav": nav,
        "form": form,
        "actions": actions,
        "note": None if available["ok"] else available["reason"],
        "hint": "每个方向写着细到哪一步；再策划时程序重新随机挑改动再出一轮（小变化每个改两处：一处是一个可变单元上的一个字段，或整幅的一项；大变化改一个主题 / 构图 / 技法字段或三处），做过的和没选的方向都会避开，写了要求就围绕要求出。",
    }


# ---------------------------------------------------------------- 日报


def daily_view(ctx: Ctx, day: str | None = None) -> dict[str, Any]:
    data = report.daily(ctx, day)
    lines = [f"**日期**：{data['day']}"]
    calls = data["calls_by_model"]
    if calls:
        for model, c in sorted(calls.items(), key=lambda kv: -kv[1]["calls"]):
            extra = "，".join(
                f"{label} {c[key]}"
                for key, label in (("failed", "失败"), ("unknown", "未知"))
                if c.get(key)
            )
            lines.append(
                f"- {model}：{c['calls']} 次" + (f"（{extra}）" if extra else "")
            )
    else:
        lines.append("今日没有模型调用")
    lines.append(
        f"**今日新素材**：{_counts_line('assets', data['assets_created_since_day'])}"
    )
    opened = data["open_reviews"]
    lines.append(
        "**待审**："
        + (
            " · ".join(f"{TYPE_LABELS.get(t, t)} {n}" for t, n in opened.items())
            if opened
            else "无"
        )
    )
    b = data["budget"]
    if b["enforced"]:
        lines.append(
            f"**预算**：今日已占用 {b['today_committed']}/{b['daily_cap']} {b['currency']}"
        )
    else:
        lines.append("预算控制已关闭（不计费），只统计调用次数")
    return {"title": f"POD 日报｜{data['day']}", "lines": lines, "day": data["day"]}


# ---------------------------------------------------------------- 最近动态

_SEED_RIGHTS_LABELS = {
    "approved": "已获授权",
    "restricted": "授权受限",
    "unknown": "未知",
}
_TIMELINE_EVENTS = (
    "seed_added",
    "seed_rights_set",
    "analysis_saved",
    "family_proposed",
    "family_approved",
    "family_rejected",
    "batch_planned",
    "directions_selected",
    "directions_rejected",
    "batch_reported",
    "worker_finished",
    "review_decided",
    "asset_requeued",
    "paused",
    "resumed",
    "trial_started",
    "trial_stopped",
    "bg_finished",
    "tick",
    # 选款之后（2026-10-30）：店铺、货号、套图、上架
    "store_chosen",
    "store_removed",
    "code_reserved",
    "scenes_finished",
    "temu_confirmed",
    "temu_boxed",
    "temu_listed",
    "temu_failed",
    "listing_title_saved",
)


def _who(by: Any) -> str:
    by = str(by or "")
    if by == directions.AUTO_BY:
        return "自动"
    return "飞书" if by.startswith("feishu:") else _clip(by, 16)


def _event_text(
    event: str, scope: str, p: dict[str, Any], review_types: dict[str, str]
) -> str | None:
    if event == "seed_added":
        return f"新增参考图 {scope}"
    if event == "seed_rights_set":
        return f"{scope} 授权设为「{_SEED_RIGHTS_LABELS.get(p.get('status'), p.get('status'))}」"
    if event == "analysis_saved":
        return f"{scope} 拆解完成"
    if event == "family_proposed":
        return f"系列 {scope} v{p.get('version')} 已生成，待批准"
    if event in ("family_approved", "family_rejected"):
        verb = "已批准" if event == "family_approved" else "已驳回"
        return f"系列 {scope} v{p.get('version')} {verb}（{_who(p.get('by'))}）"
    if event == "batch_planned":
        dropped = f"，丢弃 {p['dropped']}" if p.get("dropped") else ""
        if p.get("candidates"):
            asked = f"（要求：{_clip(p['hint'], 30)}）" if p.get("hint") else ""
            kinds = p.get("counts") or {}
            split = (
                f"（小变化 {kinds.get('small', 0)} · 大变化 {kinds.get('big', 0) + kinds.get('combo', 0)}）"
                if kinds
                else ""
            )
            return f"{scope} 策划：{p.get('family_id')} 提出 {p.get('accepted', 0)} 个裂变方向{split}{asked}，待挑选{dropped}"
        return f"{scope} 裂变：{p.get('family_id')} 新增 {p.get('accepted', 0)} 个任务{dropped}"
    if event == "directions_selected":
        return f"{scope} 选了 {len(p.get('picked') or [])} 个方向出图（{_who(p.get('by'))}）"
    if event == "directions_rejected":
        again = "，换一批" if p.get("replan") else ""
        return f"{scope} 的方向都没选中{again}（{_who(p.get('by'))}）"
    if event == "batch_reported":
        extra = "，有方向提案待你决定" if p.get("requires_approval") else ""
        return f"{scope} 批次反馈已生成{extra}"
    if event == "worker_finished":
        stop = STOP_REASON_LABELS.get(p.get("stop_reason"), p.get("stop_reason") or "")
        return f"出图一轮结束：处理 {p.get('jobs', 0)} 个任务（{stop}）"
    if event == "review_decided":
        kind = review_types.get(p.get("review_id"), "")
        if kind == "plan_selection":
            return None  # 挑选结果已由 directions_selected / directions_rejected 说明
        labels = {d: label for d, label, _ in DECISION_BUTTONS.get(kind, [])}
        labels.setdefault("needs_changes", "需要修改")
        decision = labels.get(p.get("decision"), p.get("decision"))
        kind_label = TYPE_LABELS.get(kind, "审核")
        return f"{kind_label} {scope}：{decision}（{_who(p.get('by'))}）"
    if event == "asset_requeued":
        return f"{scope} 放回{'质检' if p.get('stage') == 'qc' else '修复'}（{_who(p.get('by'))}）"
    if event == "paused":
        return "暂停" + (f"：{_clip(p.get('reason'), 40)}" if p.get("reason") else "")
    if event == "resumed":
        return f"恢复运行（{_who(p.get('by'))}）"
    if event == "trial_started":
        return (
            f"开启连续运行 {float(p.get('hours') or 0):g} 小时（{_who(p.get('by'))}）"
        )
    if event == "trial_stopped":
        return f"停止连续运行（{_who(p.get('by'))}）"
    if event == "bg_finished":
        # 每次出图都会触发一次 OSS 同步、每次巡检都回读一次控制行，成功的不进动态
        if p.get("kind") in ("sync", "controls") and p.get("status") == "done":
            return None
        label = bg.KIND_LABELS.get(p.get("kind"), p.get("kind"))
        subject = f" {p['subject']} " if p.get("subject") else ""
        return f"后台{label}{subject}{'完成' if p.get('status') == 'done' else '失败'}"
    if event == "tick" and p.get("spawned"):
        return f"自动巡检拉起出图：{_clip(p.get('reason'), 40)}"
    if event == "store_chosen":
        return f"{scope} 定了店铺 {'、'.join(p.get('stores') or [])}（{_who(p.get('by'))}）"
    if event == "store_removed":
        return f"{scope} 去掉店铺 {'、'.join(p.get('stores') or [])}（{_who(p.get('by'))}）"
    if event == "code_reserved":
        return f"{scope} 领到货号 {p.get('code')}"
    if event == "scenes_finished":
        label = {
            "done": "套图出完",
            "partial": "套图缺图",
            "failed": "套图没出成",
            "cancelled": "套图停掉",
        }
        return f"{_clip(p.get('asset_id') or scope, 20)} {label.get(p.get('status'), p.get('status'))}（{p.get('scenes', 0)} 张）"
    if event == "temu_confirmed":
        who = "全自动" if p.get("source") == "auto" else _who(p.get("by"))
        return f"{scope} 确认上架（{who}）"
    if event == "temu_boxed":
        return f"{scope} 进了妙手采集箱 {p.get('code')}（{p.get('detail_id')}）"
    if event == "temu_listed":
        return f"{scope} 已上架 {p.get('code')}（妙手 {p.get('detail_id')}）"
    if event == "temu_failed":
        return f"{scope} 上架没成 {p.get('code') or ''}：{_clip(p.get('reason'), 40)}"
    if event == "listing_title_saved" and p.get("source") == "human":
        return f"{scope} 上架标题改了（{p.get('chars')} 字符）"
    return None


def timeline(ctx: Ctx, limit: int = 20) -> dict[str, Any]:
    """人能看懂的最近动态：只挑关键事件翻译成一句话，内部流水（领取、落盘、租约）不显示。"""
    marks = ",".join("?" for _ in _TIMELINE_EVENTS)
    rows = ctx.db.q(
        f"SELECT ts, scope, event, payload_json FROM events WHERE event IN ({marks})"
        f" AND NOT (event = 'tick' AND COALESCE(CAST({ctx.db.json_text('payload_json', 'spawned')} AS INTEGER), 0) = 0)"
        " ORDER BY event_id DESC LIMIT ?",
        (*_TIMELINE_EVENTS, limit),
    )
    payloads = [(row, loads(row["payload_json"], {}) or {}) for row in rows]
    review_ids = [
        p["review_id"]
        for row, p in payloads
        if row["event"] == "review_decided" and p.get("review_id")
    ]
    review_types = {}
    if review_ids:
        review_types = {
            r["review_id"]: r["subject_type"]
            for r in ctx.db.q(
                f"SELECT review_id, subject_type FROM reviews WHERE review_id IN ({','.join('?' for _ in review_ids)})",
                tuple(review_ids),
            )
        }
    names: dict[str, str] = {}

    def name(ref: str) -> str:
        if ref not in names:
            names[ref] = labels.ref(ctx, ref)
        return names[ref]

    items = []
    for row, payload in payloads:
        if row["event"] == "bg_finished" and payload.get("subject"):
            payload = {**payload, "subject": name(payload["subject"])}
        text = _event_text(
            row["event"], name(row["scope"] or ""), payload, review_types
        )
        if text:
            items.append(
                {"at": _local_time(ctx, row["ts"]), "event": row["event"], "text": text}
            )
    lines = [f"**进行中**：{line}" for line in _task_lines(ctx, bg.running(ctx))]
    lines += [f"`{item['at']}` {item['text']}" for item in items]
    return {"title": "POD 最近动态", "lines": lines or ["还没有动态"], "items": items}


# ---------------------------------------------------------------- 已选款


_APPROVED_SQL = (
    " FROM assets a JOIN jobs j ON j.job_id = a.job_id LEFT JOIN variants v ON v.variant_id = a.variant_id"
    " WHERE a.human_decision = 'approved'"
)


def _scene_line(row: dict[str, Any] | None) -> str:
    """已选款上的一行套图状态。"""
    if not row:
        return "套图：还没出"
    label = scenes.STATUS_LABELS[row["status"]]
    if row["status"] in scenes.OK_FINAL:
        count = row["actual"] or row["expected"]
        return (
            f"套图：{label}" + (f" {count} 张" if count else "") + "（选款表里能下载）"
        )
    if row["status"] == "running" and row["step_label"]:
        return f"套图：{label}（{row['step_label']}）"
    return f"套图：{label}"


def gallery(
    ctx: Ctx, *, batch_id: str | None = None, offset: int = 0, limit: int = 3
) -> dict[str, Any]:
    """人工选中的款（选款审核点了「选中」），带缩略图，按选中时间倒序。"""
    where, params = _APPROVED_SQL, []
    if batch_id:
        where += " AND j.batch_id = ?"
        params.append(batch_id)
    total = int(ctx.db.scalar("SELECT COUNT(*)" + where, tuple(params)) or 0)
    rows = ctx.db.q(
        "SELECT a.asset_id, a.variant_id, a.model, a.slot_id, a.width, a.height, a.bytes, j.batch_id, v.axis, v.plan_json"
        + where
        + " ORDER BY a.updated_at DESC, a.asset_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    sets = (
        scenes.latest_sets(ctx, [r["asset_id"] for r in rows])
        if scenes.enabled(ctx)
        else None
    )
    items = []
    for row in rows:
        thumb = _asset_thumb(ctx, row["asset_id"])
        size = f"{row['width']}x{row['height']}" if row["width"] else ""
        label = asset_label(
            row["asset_id"], loads(row["plan_json"], {}) or {}, row["axis"]
        )
        lines = [
            " · ".join(
                x for x in (subject_label(ctx, row["batch_id"]), row["variant_id"]) if x
            ),
            " · ".join(
                x
                for x in (
                    model_label(row["model"]),
                    MODE_LABELS[slot_mode(row["slot_id"])],
                    size,
                )
                if x
            ),
        ]
        if sets is not None:
            lines.append(_scene_line(sets.get(row["asset_id"])))
        items.append(
            {
                "asset_id": row["asset_id"],
                "title": label,
                "lines": lines,
                "images": [{"path": thumb, "label": label}] if thumb else [],
            }
        )
    return {
        "title": "POD 已选款",
        "total": total,
        "offset": offset,
        "limit": limit,
        "batch_id": batch_id,
        "items": items,
        # 选款表（飞书多维表格）建好后，渠道给一个「打开选款表」链接按钮
        "table_url": feishu_table.table_url(ctx),
    }


def originals(ctx: Ctx, asset_ids: list[str]) -> dict[str, Any]:
    """已选中素材的原图绝对路径（渠道据此上传发文件）；没选中的不给。"""
    files, refused = [], []
    for asset_id in dict.fromkeys(asset_ids):
        label = asset_label_of(ctx, asset_id)
        row = ctx.db.q1(
            "SELECT asset_id, path, bytes, human_decision FROM assets WHERE asset_id = ?",
            (asset_id,),
        )
        if not row or row["human_decision"] != "approved":
            refused.append(
                {"asset_id": asset_id, "label": label, "reason": "不是已选中的款"}
            )
            continue
        try:
            path = store.ensure_local(ctx, row["path"])
        except store.StorageError as exc:
            refused.append({"asset_id": asset_id, "label": label, "reason": str(exc)})
            continue
        if not path.is_file():
            refused.append(
                {"asset_id": asset_id, "label": label, "reason": "原图文件不存在"}
            )
            continue
        files.append(
            {
                "asset_id": asset_id,
                "label": label,
                "path": str(path),
                "filename": f"{label}{path.suffix}",
                "bytes": path.stat().st_size,
            }
        )
    return {"files": files, "refused": refused}
