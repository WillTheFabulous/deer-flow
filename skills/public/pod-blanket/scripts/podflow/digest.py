"""精简通知（notify.style: brief，2026-10-26）：飞书里只提醒待办、每天几次状态总结，其余做完回一行、出错发红卡。

- 待办（`todo_items`）三类：待挑选（还开着的选款，按任务）、待下载（打好、是最新内容、还没标已下载的批次交付包）、
  套图等人工（mooya 那边停着等人的款）。只看没归档的任务（归档前打的旧交付包不算待下载）。
- 待办提醒（`todo_card`）：有待办时每 notify.todo.interval_minutes 分钟一次，只在 notify.todo.active_hours 里发
  （开始含、结束不含）；状态总结刚发过的那一个间隔里不再单独提醒；notify.todo.skip_unchanged 开着时和上次一样就不发。
- 状态总结（`digest_view` / `digest_card`）：schedule.digest_at 的每个时刻一次（错过了补发最近的那一个），
  四节：待你处理 / 待生成·进行中 / 今日已完成 / 异常；什么都没有时不发。工作台「日报」按钮、`report digest` 也给它。
- 后台任务（`bg_finished`）：人发起的（拆解、策划、修订、提取图案、画面描述、选款完成、先出套图、修改）做完回一行；
  要人接着处理的（自动确认没成功、没策划出方向）和失败发两行；程序自己推进的（出图、套图、同步）成功不发，
  失败时同样的原因 notify.renotify_hours 内只发一次。套图没出成由 scenes 收尾时发红卡（`failed_sets_card`）。
- 调度（`run_schedule`）：网关定时器每分钟的 `tick --auto` 调；发送时刻记在 meta（先记再发，发失败不重试）。
  第一次打开时只记下最近一个总结时刻、不补发。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from . import (
    bg,
    budget,
    candidates,
    feishu_table,
    labels,
    notify,
    packages,
    scenes,
    tasks,
    ui,
)
from .config import ConfigError
from .context import Ctx
from .stages.common import subject_label, task_label
from .stages.job import backlog_count
from .util import (
    canonical_json,
    clip,
    dumps,
    iso,
    loads,
    parse_iso,
    sha256_text,
    utcnow,
)

# notify.style：full = 每个后台任务、套图、归档、巡检都发卡片，每天一张日报；brief = 本模块的精简通知
STYLES = ("full", "brief")
# listing（2026-10-30）：上架这一路要人看的（没上成、货号 / 品牌、卡太久、链接、标题、Temu 驳回），见 temu.alerts
TODO_KINDS = ("pick", "download", "parked", "listing")
DEFAULT_DIGEST_AT = ("10:00", "15:00", "21:00")
DEFAULT_ACTIVE_HOURS = "09:00-21:00"
DEFAULT_INTERVAL_MINUTES = 60
# 每类待办、每节最多列几条，其余合成一行
SHOWN = 6
# 卡片上最多几个「B… 已下载」按钮（再多就去任务列表点）
DOWNLOAD_BUTTONS = 2
# 程序自己推进的后台任务：成功不发；失败按原因去重
AUTO_KINDS = frozenset({"worker", "scenes", "sync", "table", "controls", "temu", "ticks"})
# 积压到上限的这个比例就在异常里提醒「快满了」
BACKLOG_WARN_RATIO = 0.9
META_DIGEST_SLOT = "last_digest_slot"
META_TODO_AT = "last_todo_at"
META_TODO_SIGNATURE = "last_todo_signature"
META_ALERTS = "brief_alerts"
# 记着的出错原因最多留几条（只为去重，旧的丢掉）
_ALERTS_KEPT = 50
QUIET = "精简通知：不单独发，写进状态总结"
_ERROR_CHARS = 90
_NAME_CHARS = 60
_FAILURE_HINTS = {
    "analyze": "在 POD 对话里说「{subject} 重新拆解」，或问原因",
    "revise": "在 POD 对话里重新说一遍修改意见，或问原因",
    "plan": "在任务列表点「继续裂变」再试，或在 POD 对话里问原因",
    "extract": "在 POD 对话里说「{subject} 重新提取图案」，或问原因",
    "describe": "在 POD 对话里说「{subject} 重新写画面描述」，或问原因",
    "accept": "在候选表最上面再勾一次「▶ 选款完成」，或在 POD 对话里问原因",
    "dispatch": "在候选表里再提交一次，或在 POD 对话里问原因",
    "fix": "原图还在选款里；在 POD 对话里重新说修改意见，或问原因",
    "worker": "下一轮巡检会接着出；一直这样就在 POD 对话里问原因",
    "scenes": "下一轮巡检会接着查；一直这样就在 POD 对话里问原因",
}


def _local(ctx: Ctx, now: dt.datetime) -> dt.datetime:
    return now.astimezone(ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai"))))


# ---------------------------------------------------------------- 配置


def style(ctx: Ctx) -> str:
    return str(ctx.setting("notify.style", "full") or "full").strip()


def enabled(ctx: Ctx) -> bool:
    """notify.style 为 brief：只发待办提醒、状态总结、出错红卡和人发起的事的一行回执。"""
    return style(ctx) == "brief"


def _hhmm(value: Any) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f"时刻要写成 HH:MM（00:00–23:59）：{value!r}")
    return int(m[1]), int(m[2])


def digest_times(ctx: Ctx) -> list[tuple[int, int]]:
    """状态总结的时刻（schedule.digest_at，按 timezone 的本地时间）；留空 = 不发总结。"""
    raw = ctx.setting("schedule.digest_at", list(DEFAULT_DIGEST_AT))
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[,，\s]+", raw) if part]
    if not isinstance(raw, list | tuple):
        raise ConfigError(f"schedule.digest_at 要写成时刻列表：{raw!r}")
    return sorted({_hhmm(v) for v in raw})


def todo_settings(ctx: Ctx) -> dict[str, Any]:
    """待办提醒的设置（notify.todo）：间隔（0 = 不单独提醒）、时段（None = 全天）、提醒哪几类、和上次一样要不要发。"""
    conf = ctx.setting("notify.todo", {}) or {}
    if not isinstance(conf, dict):
        raise ConfigError(f"notify.todo 要写成一组设置：{conf!r}")
    try:
        interval = float(conf.get("interval_minutes", DEFAULT_INTERVAL_MINUTES) or 0)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"notify.todo.interval_minutes 要写分钟数：{conf.get('interval_minutes')!r}"
        ) from exc
    hours = conf.get("active_hours", DEFAULT_ACTIVE_HOURS)
    window = None
    if hours not in (None, ""):
        parts = str(hours).split("-")
        if len(parts) != 2:
            raise ConfigError(f"notify.todo.active_hours 要写成 HH:MM-HH:MM：{hours!r}")
        window = (_hhmm(parts[0]), _hhmm(parts[1]))
    kinds = conf.get("kinds")
    if kinds is None:
        kinds = list(TODO_KINDS)
    if not isinstance(kinds, list | tuple) or any(k not in TODO_KINDS for k in kinds):
        raise ConfigError(
            f"notify.todo.kinds 只能从 {list(TODO_KINDS)} 里挑：{kinds!r}"
        )
    return {
        "interval_minutes": max(0.0, interval),
        "active_hours": window,
        "kinds": [k for k in TODO_KINDS if k in kinds],
        "skip_unchanged": bool(conf.get("skip_unchanged", False)),
    }


def _in_window(local: dt.datetime, window: Any) -> bool:
    """本地时间在不在提醒时段里：开始含、结束不含；结束早于开始时跨午夜；开始等于结束算全天。"""
    if not window:
        return True
    (sh, sm), (eh, em) = window
    start, end, now = sh * 60 + sm, eh * 60 + em, local.hour * 60 + local.minute
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def settings_text(ctx: Ctx) -> str:
    """doctor 和命令输出里的一句话：现在按什么节奏发。"""
    times = digest_times(ctx)
    todo = todo_settings(ctx)
    digest_part = (
        "状态总结 " + " / ".join(f"{h:02d}:{m:02d}" for h, m in times)
        if times
        else "不发状态总结"
    )
    if todo["interval_minutes"] <= 0 or not todo["kinds"]:
        todo_part = "不单独提醒待办"
    else:
        window = todo["active_hours"]
        span = (
            f"{window[0][0]:02d}:{window[0][1]:02d}–{window[1][0]:02d}:{window[1][1]:02d}"
            if window
            else "全天"
        )
        todo_part = f"有待办时每 {todo['interval_minutes']:g} 分钟提醒一次（{span}）"
    return f"{digest_part}；{todo_part}"


# ---------------------------------------------------------------- 待办


def _task_label(task: dict[str, Any]) -> str:
    return f"{task['seed_id']} {task['name']}" if task.get("name") else task["seed_id"]


def _table_link(ctx: Ctx, seed_id: str) -> str | None:
    if not feishu_table.enabled(ctx):
        return None
    try:
        return candidates.table_link(ctx, seed_id)
    except Exception:  # noqa: BLE001 - 链接拿不到就不放，不挡提醒
        return None


def _parked(ctx: Ctx) -> list[dict[str, Any]]:
    """停在 mooya 等人工 / 已暂停的款（同一款只看最近一套）。"""
    if not scenes.enabled(ctx):
        return []
    marks = ",".join("?" for _ in scenes.PARKED)
    rows = [
        dict(r)
        for r in ctx.db.q(
            f"SELECT set_id, seed_id, asset_id, status, step_label, mooya_job_id FROM scene_sets"
            f" WHERE status IN ({marks}) ORDER BY seed_id, set_id",
            scenes.PARKED,
        )
    ]
    if not rows:
        return []
    latest = scenes.latest_sets(ctx, [r["asset_id"] for r in rows])
    rows = [
        r for r in rows if latest.get(r["asset_id"], {}).get("set_id") == r["set_id"]
    ]
    names = labels.assets(ctx, [r["asset_id"] for r in rows])
    return [
        {
            "seed_id": r["seed_id"],
            "set_id": r["set_id"],
            "asset_id": r["asset_id"],
            "task": task_label(ctx, r["seed_id"]),
            "asset": names.get(r["asset_id"], {}).get("label") or r["asset_id"],
            # mooya 的步骤名后面常带一段括号里的处理说明（标题里已经写了去哪处理），只留步骤名
            "step": re.split(
                r"[（(]",
                r["step_label"] or scenes.STATUS_LABELS.get(r["status"], r["status"]),
            )[0].strip(),
            "mooya_job_id": r["mooya_job_id"],
        }
        for r in rows
    ]


def todo_items(
    ctx: Ctx,
    *,
    kinds: list[str] | tuple[str, ...] = TODO_KINDS,
    task_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """现在的待办：{pick: [...], download: [...], parked: [...], count, signature}。task_list 可传已查好的任务列表。"""
    items = task_list if task_list is not None else tasks.summaries(ctx)
    pick: list[dict[str, Any]] = []
    download: list[dict[str, Any]] = []
    for task in items:
        if task.get("archived"):
            continue
        counts = task.get("counts") or {}
        pending = int(counts.get("pending_selection") or 0)
        if "pick" in kinds and pending:
            pick.append(
                {
                    "seed_id": task["seed_id"],
                    "label": _task_label(task),
                    "pending": pending,
                    "unfinished": int(counts.get("jobs_unfinished") or 0),
                    "url": _table_link(ctx, task["seed_id"]),
                }
            )
        if "download" in kinds:
            for view in task.get("packages") or []:
                if not ui._downloadable(view):
                    continue
                download.append(
                    {
                        "seed_id": task["seed_id"],
                        "batch_id": view["batch_id"],
                        "label": _task_label(task),
                        "items": view["items"],
                        "size": packages.size_text(view["bytes"]),
                        "url": view["url"],
                        "outdated": bool(view.get("download_outdated")),
                    }
                )
    pick.sort(key=lambda p: (-p["pending"], p["seed_id"]))
    parked = _parked(ctx) if "parked" in kinds else []
    listing_items = _listing_alerts(ctx) if "listing" in kinds else []
    signature = sha256_text(
        canonical_json(
            {
                "pick": [[p["seed_id"], p["pending"]] for p in pick],
                "download": [
                    [d["seed_id"], d["batch_id"], d["items"]] for d in download
                ],
                "parked": [p["set_id"] for p in parked],
                "listing": [[x["asset_id"], x["store"], x["kind"]] for x in listing_items],
            }
        )
    )[:16]
    return {
        "pick": pick,
        "download": download,
        "parked": parked,
        "listing": listing_items,
        "count": len(pick) + len(download) + len(parked) + len(listing_items),
        "signature": signature,
    }


def _listing_alerts(ctx: Ctx) -> list[dict[str, Any]]:
    """上架这一路要人看的（temu.alerts），带上款的名字。"""
    from . import temu  # 上架模块依赖很多模块，放到用时再导入

    try:
        items = temu.alerts(ctx)
    except Exception:  # noqa: BLE001 - 上架那边出错不挡别的待办
        return []
    names = labels.assets(ctx, sorted({x["asset_id"] for x in items}))
    return [{**x, "asset": (names.get(x["asset_id"]) or {}).get("label") or x["asset_id"]} for x in items]


def _todo_lines(todo: dict[str, Any], *, bold: bool = True) -> list[str]:
    def head(text: str) -> str:
        return f"**{text}**" if bold else text

    lines: list[str] = []
    pick = todo["pick"]
    if pick:
        total = sum(p["pending"] for p in pick)
        lines.append(head(f"待挑选 {len(pick)} 个任务 · {total} 张"))
        for p in pick[:SHOWN]:
            state = (
                f"还在出，剩 {p['unfinished']} 个方向" if p["unfinished"] else "已出完"
            )
            link = f" [候选表]({p['url']})" if p["url"] else ""
            lines.append(f"- {p['label']}：{p['pending']} 张（{state}）{link}")
        rest = pick[SHOWN:]
        if rest:
            lines.append(
                f"- 另 {len(rest)} 个任务共 {sum(p['pending'] for p in rest)} 张：在任务列表里看"
            )
    download = todo["download"]
    if download:
        lines.append(
            head(f"待下载 {len(download)} 个交付包")
            + "（下载后点「已下载」，任务才会归档）"
        )
        for d in download[:SHOWN]:
            link = f" [下载]({d['url']})" if d["url"] else ""
            again = "，重打过，下载新包" if d["outdated"] else ""
            lines.append(
                f"- {d['label']} · {d['batch_id']}：{d['items']} 款 · {d['size']}{again}{link}"
            )
        if len(download) > SHOWN:
            lines.append(f"- 另 {len(download) - SHOWN} 个：在任务列表里看")
    parked = todo["parked"]
    if parked:
        lines.append(
            head(f"套图等人工 {len(parked)} 款")
            + "（去 mooya「图案管线」处理，这边会自动接着拉）"
        )
        for x in parked[:SHOWN]:
            lines.append(f"- {x['task']}：{x['asset']}（{x['step']}）")
        if len(parked) > SHOWN:
            lines.append(f"- 另 {len(parked) - SHOWN} 款：在任务列表里看")
    listing_items = todo.get("listing") or []
    if listing_items:
        lines.append(
            head(f"上架要你看 {len(listing_items)} 个")
            + "（详情在「选中的款」的上架备注 / 当前环节；POD 对话问「IMG… 走到哪了」）"
        )
        for x in listing_items[:SHOWN]:
            code = f" {x['code']}" if x.get("code") else ""
            lines.append(f"- {x['asset']} {x['store']}{code}：{x['text']}")
        if len(listing_items) > SHOWN:
            lines.append(f"- 另 {len(listing_items) - SHOWN} 个：`podctl temu status` 里看")
    return lines


def _download_buttons(todo: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
    return [
        (
            f"{d['batch_id']} 已下载",
            {
                "action": ui.ACTION_DOWNLOADED,
                "seed": d["seed_id"],
                "batch": d["batch_id"],
            },
            "default",
        )
        for d in todo["download"][:DOWNLOAD_BUTTONS]
    ]


def todo_view(
    ctx: Ctx,
    now: dt.datetime | None = None,
    *,
    kinds: list[str] | tuple[str, ...] = TODO_KINDS,
    task_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """待办提醒：标题、markdown 行和结构化的待办（`report todo` 原样输出给人设）。"""
    local = _local(ctx, now or utcnow())
    todo = todo_items(ctx, kinds=kinds, task_list=task_list)
    lines = _todo_lines(todo) or ["现在没有要你处理的事"]
    return {
        "title": f"POD 待办｜{local:%H:%M}",
        "lines": lines,
        "time": f"{local:%m-%d %H:%M}",
        **todo,
    }


def todo_card(view: dict[str, Any]) -> dict[str, Any]:
    return notify.pod_card(
        view["title"],
        view["lines"],
        buttons=[
            ("任务列表", {"action": ui.ACTION_TASKS}, "primary"),
            *_download_buttons(view),
        ],
    )


# ---------------------------------------------------------------- 状态总结


def _working(ctx: Ctx, task_list: list[dict[str, Any]]) -> list[str]:
    """待生成 / 进行中：每个在拆解、策划、出图的任务一行，最后合计在 mooya 出的套图（等人工的在待办里）。"""
    lines: list[str] = []
    scenes_running = 0
    for task in task_list:
        if task.get("archived"):
            continue
        counts = task.get("counts") or {}
        status = task["status"]
        label = _task_label(task)
        scenes_running += max(
            0,
            int(counts.get("scenes_active") or 0)
            - int(counts.get("scenes_parked") or 0),
        )
        if status == "analyzing":
            lines.append(f"- {label}：拆解中")
        elif status == "planning":
            lines.append(f"- {label}：策划中")
        elif status == "picking":
            lines.append(
                f"- {label}：{task.get('pending_batch') or ''} 的方向等你挑选（任务列表点「去挑选」）"
            )
        elif counts.get("jobs") and counts.get("jobs_done", 0) < counts["jobs"]:
            text = f"- {label}：出图 {counts['jobs_done']}/{counts['jobs']} 个方向"
            left = int(counts.get("images_unfinished") or 0)
            if left:
                text += f"，还要出约 {left} 张"
            note = {
                "queued": "排队",
                "paused": "流水线暂停中",
                "blocked": "有方向暂停，原因在任务列表",
                "start_needed": "要点「开始出图」",
            }.get(status)
            if note:
                text += f"（{note}）"
            lines.append(text)
    if len(lines) > SHOWN * 2:
        rest = len(lines) - SHOWN * 2
        lines = lines[: SHOWN * 2] + [f"- 另 {rest} 个任务：在任务列表里看"]
    if scenes_running:
        lines.append(f"- 套图：{scenes_running} 款在 mooya 出")
    return lines


def _day_start(ctx: Ctx, now: dt.datetime) -> str:
    local = _local(ctx, now)
    return iso(local.replace(hour=0, minute=0, second=0, microsecond=0))


def _scalar(ctx: Ctx, sql: str, params: tuple[Any, ...]) -> int:
    return int(ctx.db.scalar(sql, params) or 0)


def _names(ctx: Ctx, seed_ids: list[str], limit: int = 4) -> str:
    shown = "、".join(task_label(ctx, s) for s in seed_ids[:limit])
    return shown + (f" 等 {len(seed_ids)} 个" if len(seed_ids) > limit else "")


def today_counts(ctx: Ctx, now: dt.datetime | None = None) -> dict[str, Any]:
    """今天（按 timezone 的本地日界线）做完了什么：新任务、出图、选款、套图、交付包、下载、归档。"""
    since = _day_start(ctx, now or utcnow())
    seeds = [
        r["seed_id"]
        for r in ctx.db.q(
            "SELECT seed_id FROM seeds WHERE created_at >= ? ORDER BY seed_id", (since,)
        )
    ]
    decided = {
        r[0]: r[1]
        for r in ctx.db.q(
            "SELECT decision, COUNT(*) FROM reviews WHERE subject_type = 'asset_selection'"
            " AND status = 'decided' AND decided_at >= ? GROUP BY decision",
            (since,),
        )
    }
    sets = {
        r[0]: r[1]
        for r in ctx.db.q(
            "SELECT status, COUNT(*) FROM scene_sets WHERE finished_at >= ? GROUP BY status",
            (since,),
        )
    }
    try:
        downloads = _scalar(
            ctx, "SELECT COUNT(*) FROM package_downloads WHERE marked_at >= ?", (since,)
        )
    except Exception:  # noqa: BLE001 - 没有「已下载」那张表的旧库
        downloads = 0
    return {
        "since": since,
        "seeds": seeds,
        "images": _scalar(
            ctx,
            "SELECT COUNT(*) FROM assets WHERE created_at >= ? AND status != 'failed'",
            (since,),
        ),
        "jobs_done": _scalar(
            ctx,
            "SELECT COUNT(*) FROM jobs WHERE status = 'complete' AND updated_at >= ?",
            (since,),
        ),
        "jobs_failed": _scalar(
            ctx,
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND updated_at >= ?",
            (since,),
        ),
        "batches_done": _scalar(
            ctx,
            "SELECT COUNT(*) FROM batches WHERE status = 'done' AND updated_at >= ?",
            (since,),
        ),
        "approved": int(decided.get("approved") or 0),
        "rejected": int(decided.get("rejected") or 0),
        "accepted_tasks": _scalar(
            ctx,
            "SELECT COUNT(DISTINCT scope) FROM events WHERE event = 'selection_accepted' AND ts >= ?",
            (since,),
        ),
        "scenes_done": int(sets.get("done") or 0) + int(sets.get("partial") or 0),
        "scenes_failed": int(sets.get("failed") or 0),
        "packages_built": _scalar(
            ctx, "SELECT COUNT(*) FROM scene_packages WHERE built_at >= ?", (since,)
        ),
        "downloads": downloads,
        "archived": _scalar(
            ctx,
            "SELECT COUNT(*) FROM seed_shelf WHERE state = 'archived' AND archived_at >= ?",
            (since,),
        ),
        "temu_listed": _scalar(
            ctx,
            "SELECT COUNT(*) FROM store_listings WHERE state = 'listed' AND listed_at >= ?",
            (since,),
        ),
    }


def _today_lines(ctx: Ctx, today: dict[str, Any]) -> list[str]:
    lines = []
    if today["seeds"]:
        lines.append(
            f"- 新任务 {len(today['seeds'])} 个：{_names(ctx, today['seeds'])}"
        )
    if today["images"] or today["jobs_done"] or today["jobs_failed"]:
        text = f"- 出图 {today['images']} 张 · 出完 {today['jobs_done']} 个方向"
        if today["batches_done"]:
            text += f"（{today['batches_done']} 批全部出完）"
        if today["jobs_failed"]:
            text += f"；{today['jobs_failed']} 个方向没出成"
        lines.append(text)
    if today["approved"] or today["rejected"]:
        text = f"- 选款：选中 {today['approved']} 张 · 没选 {today['rejected']} 张"
        if today["accepted_tasks"]:
            text += f"（{today['accepted_tasks']} 个任务点了选款完成）"
        lines.append(text)
    parts = [
        f"{label} {today[key]} {unit}"
        for key, label, unit in (
            ("scenes_done", "套图出完", "款"),
            ("scenes_failed", "套图没出成", "款"),
            ("packages_built", "交付包打好", "个"),
            ("downloads", "标已下载", "个"),
            ("temu_listed", "上架 Temu", "个（款 × 店）"),
        )
        if today.get(key)
    ]
    if parts:
        lines.append("- " + " · ".join(parts))
    if today["archived"]:
        lines.append(f"- 归档 {today['archived']} 个任务")
    return lines


def _listing_counts(ctx: Ctx) -> dict[str, int]:
    """Temu 上架（开着时）：标题写好等确认上架的款、没上成的款、在上的店铺行。"""
    from . import stores, temu  # 上架模块依赖很多模块，放到用时再导入

    if not stores.enabled(ctx):
        return {}
    rows = stores.rows(ctx)
    ready = {
        r["asset_id"]
        for r in rows
        if r["state"] == temu.STATE_CHOSEN
        and not temu.auto_confirm_on(ctx, r["store_key"])
        and not temu.held(ctx, r["asset_id"])
        and temu.blocker(ctx, r) is None
    }
    return {
        "ready": len(ready),
        "held": len({r["asset_id"] for r in rows if r["state"] == temu.STATE_CHOSEN and temu.held(ctx, r["asset_id"])}),
        "in_box": sum(
            1 for r in rows if r["state"] == temu.STATE_BOX and temu.mode_for(ctx, r["store_key"]) == temu.MODE_BOX
        ),
    }


def _listing_todo(ctx: Ctx) -> list[str]:
    """状态总结里上架这一路要人做的（没上成、卡住的在待办「上架要你看」里，这里不重复）。"""
    counts = _listing_counts(ctx)
    lines = []
    if counts.get("ready"):
        lines.append(f"- 待确认上架 {counts['ready']} 款：标题写好了，在「选中的款」看过后勾「确认上架」")
    if counts.get("held"):
        lines.append(f"- 暂停上架 {counts['held']} 款：在「选中的款」再勾「确认上架」就接着上")
    if counts.get("in_box"):
        lines.append(f"- 妙手采集箱里 {counts['in_box']} 个等发布：在妙手里检查后点发布")
    return lines


def _alerts(ctx: Ctx, task_list: list[dict[str, Any]]) -> list[str]:
    """只写现在的、要人管的异常（不报历史上失败过几次）。"""
    from . import temu  # 上架模块依赖很多模块，放到用时再导入

    out: list[str] = []
    listing_error = temu.status(ctx)["service_error"] if temu.enabled(ctx) else None
    if listing_error:
        out.append(f"- 妙手开放平台出错：{clip(listing_error, 60)}（上架先停着，修好后自动接着上）")
    if ctx.db.get_meta("paused") == "1":
        reason = ctx.db.get_meta("pause_reason")
        out.append(
            "- 流水线已暂停"
            + (f"：{clip(reason, 40)}" if reason else "")
            + "（工作台点「恢复」）"
        )
    limit = int(ctx.setting("batch.review_backlog_limit", 50) or 0)
    backlog = backlog_count(ctx)
    if limit and backlog >= limit:
        out.append(
            f"- 待审积压 {backlog}/{limit} 已满：新的图停出，选款处理一些后自动接着出"
        )
    elif limit and backlog >= limit * BACKLOG_WARN_RATIO:
        out.append(f"- 待审积压 {backlog}/{limit} 快满了：满了会停出新图")
    service = loads(ctx.db.get_meta(scenes.SERVICE_ERROR_META_KEY), {}) or {}
    if service.get("error") and scenes.enabled(ctx):
        out.append(
            f"- mooya 套图接口出错：{clip(service['error'], 60)}（套图先停着，修好后自动接着出）"
        )
    unknown = int(budget.summary(ctx.db, ctx.cfg).get("unknown_calls") or 0)
    if unknown:
        out.append(f"- {unknown} 个调用结果未知，要对账（在 POD 对话里说「对账」）")
    for task in task_list:
        if task.get("archived"):
            continue
        if task["status"] == "analyze_failed":
            out.append(
                f"- {_task_label(task)} 拆解失败"
                + (f"：{clip(task.get('error'), 40)}" if task.get("error") else "")
                + f"（在 POD 对话里说「{task['seed_id']} 重新拆解」）"
            )
    return out


def digest_view(ctx: Ctx, now: dt.datetime | None = None) -> dict[str, Any]:
    """状态总结：待你处理 / 待生成·进行中 / 今日已完成 / 异常（`report digest`、工作台「日报」按钮、定时推送共用）。"""
    now = now or utcnow()
    local = _local(ctx, now)
    task_list = tasks.summaries(ctx)
    todo = todo_items(ctx, task_list=task_list)
    working = _working(ctx, task_list)
    today = today_counts(ctx, now)
    today_lines = _today_lines(ctx, today)
    alerts = _alerts(ctx, task_list)
    lines: list[str] = []
    todo_lines = _todo_lines(todo, bold=False) + _listing_todo(ctx)
    if todo_lines:
        lines += ["**待你处理**", *todo_lines]
    else:
        lines.append("**待你处理**：没有")
    lines += ["", "**待生成 / 进行中**", *(working or ["- 没有在出的图"])]
    lines += [
        "",
        f"**今日已完成**（{local:%m-%d} 00:00 起）",
        *(today_lines or ["- 还没有"]),
    ]
    if alerts:
        lines += ["", "**异常**", *alerts]
    return {
        "title": f"POD 状态总结｜{local:%m-%d %H:%M}",
        "lines": lines,
        "time": f"{local:%m-%d %H:%M}",
        "todo": todo,
        "working": working,
        "today": today,
        "alerts": alerts,
        "empty": not (todo_lines or working or today_lines or alerts),
        "level": "attention" if alerts else "normal",
    }


def digest_card(view: dict[str, Any]) -> dict[str, Any]:
    return notify.pod_card(
        view["title"],
        view["lines"],
        level=view.get("level", "normal"),
        buttons=[
            ("任务列表", {"action": ui.ACTION_TASKS}, "primary"),
            *_download_buttons(view["todo"]),
            ("打开工作台", {"action": notify.CARD_ACTION_DASHBOARD}, "default"),
        ],
    )


# ---------------------------------------------------------------- 发送与调度


def _send(ctx: Ctx, card: dict[str, Any]) -> dict[str, Any]:
    fallback = "\n".join(
        [card["header"]["title"]["content"], card["elements"][0]["content"]]
    )
    try:
        return notify.send_pod_card(ctx, card, fallback)
    except notify.NotifyError as exc:
        return {"sent": False, "error": str(exc)}


def send_digest(
    ctx: Ctx, now: dt.datetime | None = None, *, view: dict[str, Any] | None = None
) -> dict[str, Any]:
    """发一次状态总结（什么都没有时不发）；view 可传已算好的 digest_view。"""
    view = view or digest_view(ctx, now)
    if view["empty"]:
        return {"sent": False, "reason": "没有待办、在出的图和今天做完的事"}
    return _send(ctx, digest_card(view))


def send_todo(
    ctx: Ctx,
    now: dt.datetime | None = None,
    *,
    settings: dict[str, Any] | None = None,
    view: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """发一次待办提醒（没有待办不发；skip_unchanged 时和上次一样不发，force 时照发）。
    view 可传已算好的 todo_view（手动 `report todo --notify` 列全部三类）。"""
    if view is None:
        settings = settings or todo_settings(ctx)
        view = todo_view(ctx, now, kinds=settings["kinds"])
    if not view["count"]:
        return {"sent": False, "reason": "没有待办"}
    if not force:
        settings = settings or todo_settings(ctx)
        if (
            settings["skip_unchanged"]
            and ctx.db.get_meta(META_TODO_SIGNATURE) == view["signature"]
        ):
            return {"sent": False, "reason": "和上次提醒的一样"}
    sent = _send(ctx, todo_card(view))
    if sent.get("sent"):
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, META_TODO_SIGNATURE, view["signature"])
    return sent


def _latest_slot(ctx: Ctx, now: dt.datetime) -> str | None:
    """今天已经过了的最近一个总结时刻（「2026-10-26 15:00」）；今天还没到第一个时刻时为 None。"""
    local = _local(ctx, now)
    past = [
        (h, m) for h, m in digest_times(ctx) if (h, m) <= (local.hour, local.minute)
    ]
    if not past:
        return None
    h, m = max(past)
    return f"{local:%Y-%m-%d} {h:02d}:{m:02d}"


def _todo_due(ctx: Ctx, now: dt.datetime, settings: dict[str, Any]) -> bool:
    if settings["interval_minutes"] <= 0 or not settings["kinds"]:
        return False
    if not _in_window(_local(ctx, now), settings["active_hours"]):
        return False
    last = parse_iso(ctx.db.get_meta(META_TODO_AT))
    # 定时器每分钟一跳，留 30 秒余量，免得刚好差几秒被推迟一整轮
    return not last or now - last >= dt.timedelta(
        minutes=settings["interval_minutes"], seconds=-30
    )


def run_schedule(ctx: Ctx, now: dt.datetime | None = None) -> dict[str, Any]:
    """网关定时器每分钟调一次（tick --auto，brief 时）：到了总结时刻发状态总结；
    否则到了提醒间隔、在提醒时段里、有待办就发待办提醒。没到点返回空 dict。"""
    now = now or utcnow()
    out: dict[str, Any] = {}
    slot = _latest_slot(ctx, now)
    recorded = ctx.db.get_meta(META_DIGEST_SLOT)
    if recorded is None:
        # 第一次打开：只记下现在，从下一个总结时刻开始发（不补发打开之前的时刻）
        with ctx.db.tx() as conn:
            ctx.db.set_meta(
                conn,
                META_DIGEST_SLOT,
                slot or f"{_local(ctx, now):%Y-%m-%d %H:%M} 开始",
            )
    elif slot and recorded != slot:
        # 先记再发：发不出去也不在之后每分钟重试刷屏；总结里已经列了待办，这一个间隔里不再单独提醒
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, META_DIGEST_SLOT, slot)
            ctx.db.set_meta(conn, META_TODO_AT, iso(now))
        out["digest"] = {"slot": slot, **send_digest(ctx, now)}
        return out
    settings = todo_settings(ctx)
    if _todo_due(ctx, now, settings):
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, META_TODO_AT, iso(now))
        out["todo"] = send_todo(ctx, now, settings=settings)
    return out


# ---------------------------------------------------------------- 后台任务的回执与红卡


def _auto_text(auto: dict[str, Any]) -> str:
    if auto.get("started"):
        return "开始出图"
    if auto.get("backlog"):
        return "先排着：待审积压满了，选款处理一些后接着出"
    if auto.get("reason"):
        return f"先排着：{clip(auto['reason'], 30)}"
    if auto.get("queued"):
        return "排在正在出的图后面"
    return "等巡检接着出"


def _card(
    title: str,
    lines: list[str],
    *,
    level: str = "normal",
    buttons: list[tuple[str, dict[str, Any], str]] | None = None,
    links: list[tuple[str, str, str]] | None = None,
    images: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return notify.pod_card(
        title,
        [line for line in lines if line],
        level=level,
        buttons=buttons or [("任务列表", {"action": ui.ACTION_TASKS}, "default")],
        links=links,
        images=images,
    )


def _plan_card(
    ctx: Ctx, plan: dict[str, Any], title: str, family: str | None, *, note: str = ""
) -> dict[str, Any]:
    """策划结果：自动确认了就一行「n 个方向、约 m 张图，开始出图」；要人挑、没策划出方向时两行。"""
    accepted = int(plan.get("accepted") or 0)
    auto = plan.get("auto")
    if accepted and auto:
        line = (
            f"{auto.get('picked') or accepted} 个方向、约 {auto.get('images') or 0} 张图，"
            f"{_auto_text(auto)}{note}"
        )
        return _card(title, [line])
    if accepted:
        reason = (
            f"自动确认没成功：{clip(plan['auto_error'], 60)}"
            if plan.get("auto_error")
            else "自动确认关着：点「去挑选」勾要出图的方向"
        )
        return _card(
            title,
            [f"{accepted} 个方向等你挑选{note}", reason],
            level="attention",
            buttons=[
                (
                    "去挑选",
                    {"action": ui.ACTION_PLAN_VIEW, "batch": plan.get("batch_id")},
                    "primary",
                )
            ],
        )
    why = "这一轮没有通过去重与范围校验的方向"
    if plan.get("exhausted"):
        why = "探索空间已耗尽，要新方向或新参考图"
    elif plan.get("failed_groups"):
        why = "这几块没策划出来：" + "、".join(plan["failed_groups"][:3])
    buttons = (
        [("任务明细", {"action": ui.ACTION_HISTORY, "family": family}, "primary")]
        if family
        else None
    )
    return _card(
        f"没有策划出新方向｜{subject_label(ctx, family)}".rstrip("｜")
        if family
        else title,
        [f"{why}{note}", "在任务明细里写一句想要的方向再策划"],
        level="attention",
        buttons=buttons,
    )


def _family_card(
    ctx: Ctx, kind: str, subject: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """拆解 / 修订做完：带着自动策划的结果一行说完；没策划成时写原因、给「重新策划」。"""
    family = summary.get("family_id")
    task = task_label(ctx, subject)
    if kind == "analyze":
        if not family:
            return _card(
                f"拆解没做完｜{task}",
                [
                    f"停在 {summary.get('stopped_at') or '母版阶段'}（{summary.get('status') or '未知'}）",
                    f"在 POD 对话里问原因，或说「{subject} 重新拆解」",
                ],
                level="attention",
            )
        title = f"拆解完成｜{task}"
    else:
        version = summary.get("family_version")
        title = f"系列已修订｜{subject_label(ctx, family)}" + (
            f" v{version}" if version else ""
        )
    plan = summary.get("plan") or {}
    note = (
        "（画面描述没写成，策划先按拆解写）" if summary.get("description_error") else ""
    )
    if plan.get("accepted"):
        return _plan_card(ctx, plan, title, family, note=note)
    if summary.get("plan_error") or plan:
        reason = summary.get("plan_error") or "这一轮没有通过去重与范围校验的方向"
        return _card(
            title,
            [
                f"策划裂变方向没成功：{clip(reason, _ERROR_CHARS)}{note}",
                "点「重新策划」再试一次",
            ],
            level="attention",
            buttons=[
                ("重新策划", {"action": "pod_plan", "family_id": family}, "primary")
            ]
            if family
            else None,
        )
    return _card(
        title,
        [f"系列规则 {family} 待你批准{note}", "要改就在 POD 对话里说修改意见"],
        level="attention",
        buttons=[
            (
                "查看待批准的系列",
                {"action": notify.CARD_ACTION_REVIEWS, "type": "family"},
                "primary",
            )
        ],
    )


def _fix_card(ctx: Ctx, asset_id: str) -> dict[str, Any]:
    """选款里要求的修改跑完：新图回到选款，或原图放回选款并写明原因。"""
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
        return _card(
            f"修改完成｜{name}",
            [f"新图 {labels.asset(ctx, child['asset_id'])} 已回到候选表，等你挑"],
        )
    if failed:
        return _card(
            f"修改没成功｜{name}",
            [
                f"原图放回选款：{clip(failed, _ERROR_CHARS)}",
                "在 POD 对话里重新说修改意见",
            ],
            level="attention",
        )
    return _card(f"修改还没完成｜{name}", ["下一轮出图会接着做"])


def _accept_card(
    ctx: Ctx, kind: str, seed_id: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """选款完成 / 先出套图：选中几张、没选几张、几款开始出套图，一行说完；套图没开始时两行。"""
    task = task_label(ctx, seed_id)
    info = summary.get("scenes") or {}
    links = (
        [("候选表", summary["table_url"], "default")]
        if summary.get("table_url")
        else None
    )
    if kind == "accept":
        title = f"选款完成｜{task}"
        line = f"选中 {summary.get('approved', 0)} · 没选 {summary.get('rejected', 0)}"
        later = int(summary.get("fresh") or 0) + int(summary.get("waiting") or 0)
        if later:
            line += f" · {later} 张新图留到下次"
    else:
        title = f"先出套图｜{task}"
        line = (
            f"定为选中 {summary.get('approved', 0)} 张"
            if summary.get("picked")
            else "候选表里没有勾上、出了图的款"
        )
    if info.get("error"):
        return _card(
            title,
            [line, f"套图没开始：{clip(info['error'], _ERROR_CHARS)}"],
            level="attention",
            links=links,
        )
    if info.get("queued"):
        line += f"，{info['queued']} 款开始出套图"
    return _card(title, [line], links=links)


def _describe_card(ctx: Ctx, subject: str, summary: dict[str, Any]) -> dict[str, Any]:
    if "described" in summary:
        done = summary.get("described") or []
        failed = summary.get("failed") or []
        line = f"补写了 {len(done)} 张" + (
            f"，{len(failed)} 张没写成" if failed else ""
        )
        return _card(
            "画面描述已补写", [line], level="attention" if failed else "normal"
        )
    task = task_label(ctx, subject)
    if summary.get("skipped"):
        return _card(f"画面描述｜{task}", [clip(summary["skipped"], _ERROR_CHARS)])
    units = summary.get("units") or []
    line = "之后的策划和出图指令照它写"
    if units:
        line = f"可改的主体 {len(units)} 个；{line}"
    return _card(f"画面描述已写好｜{task}", [line])


def _done_card(
    ctx: Ctx, task: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any] | None:
    kind, subject = task["kind"], task.get("subject") or ""
    if kind in AUTO_KINDS:
        return None
    if kind in ("analyze", "revise"):
        return _family_card(ctx, kind, subject, summary)
    if kind == "plan":
        title = f"策划完成｜{subject_label(ctx, summary.get('batch_id') or subject)}"
        return _plan_card(
            ctx, summary, title, summary.get("family_id") or subject or None
        )
    if kind == "derive":
        plans = summary.get("plans") or []
        if not plans:
            reasons = "；".join(
                str(s.get("reason") or "") for s in summary.get("skipped") or []
            )
            return _card(
                f"二次大变化没策划出来｜{labels.ref(ctx, subject)}".rstrip("｜"),
                [clip(reasons, _ERROR_CHARS) or "没有该派生的批次"],
                level="attention",
            )
        plan = plans[0]
        title = (
            f"二次大变化策划完成｜{subject_label(ctx, plan.get('batch_id') or subject)}"
        )
        return _plan_card(ctx, plan, title, plan.get("family_id"))
    if kind == "fix":
        # 多工位的 worker 每改完一张就回过一行（fix_cards，见 fix_finished），这里不再重复
        if "fix_cards" in summary:
            return None
        return _fix_card(ctx, subject)
    if kind in ("accept", "dispatch"):
        return _accept_card(ctx, kind, subject, summary)
    if kind == "extract":
        line = "之后的图生图按新图案出" + (
            f"（{summary['model']}）" if summary.get("model") else ""
        )
        return _card(
            f"图案已重新提取｜{task_label(ctx, subject)}",
            [line],
            images=notify._seed_images(ctx, subject) or None,
        )
    if kind == "describe":
        return _describe_card(ctx, subject, summary)
    label = bg.KIND_LABELS.get(kind, kind)
    return _card(f"{label}完成｜{labels.ref(ctx, subject)}".rstrip("｜"), ["做完了"])


def _failure_card(
    ctx: Ctx, task: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    """两行红卡：出了什么错、该怎么办。"""
    kind, subject = task["kind"], task.get("subject") or ""
    label = bg.KIND_LABELS.get(kind, kind)
    error = str(summary.get("error") or "未知错误")
    detail = str(summary.get("detail") or "")
    reason = clip(
        f"{error}：{detail}" if detail and detail not in error else error, _ERROR_CHARS
    )
    hint = _FAILURE_HINTS.get(kind, "").format(subject=subject)
    if not hint:
        hint = "在 POD 对话里问原因" + (
            f"（{task['task_id']}）" if task.get("task_id") else ""
        )
    return _card(
        f"{label}失败｜{labels.ref(ctx, subject)}".rstrip("｜"),
        [reason, hint],
        level="alert",
    )


def brief_bg_card(
    ctx: Ctx, task: dict[str, Any], summary: dict[str, Any], *, ok: bool
) -> dict[str, Any] | None:
    """后台任务结束时的卡片（精简）：人发起的回一行，要人接着处理的和失败两行；程序自己推进的成功时为 None。"""
    if not ok:
        return _failure_card(ctx, task, summary)
    return _done_card(ctx, task, summary)


def _alert_signature(task: dict[str, Any], summary: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(
            [
                task["kind"],
                task.get("subject"),
                summary.get("error"),
                summary.get("detail"),
            ]
        )
    )[:16]


def _recent_alert(ctx: Ctx, signature: str, now: dt.datetime) -> bool:
    hours = float(ctx.setting("notify.renotify_hours", 6))
    seen = loads(ctx.db.get_meta(META_ALERTS), {}) or {}
    at = parse_iso(seen.get(signature))
    return bool(at and now - at < dt.timedelta(hours=hours))


def _remember_alert(ctx: Ctx, signature: str, now: dt.datetime) -> None:
    seen = loads(ctx.db.get_meta(META_ALERTS), {}) or {}
    seen[signature] = iso(now)
    kept = dict(sorted(seen.items(), key=lambda kv: kv[1])[-_ALERTS_KEPT:])
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, META_ALERTS, dumps(kept))


def bg_finished(
    ctx: Ctx, task: dict[str, Any], summary: dict[str, Any], *, ok: bool
) -> dict[str, Any]:
    """后台任务结束（精简通知时由 notify.bg_finished 转来）：按上面的规则发或不发。"""
    card = brief_bg_card(ctx, task, summary, ok=ok)
    if card is None:
        return {"sent": False, "reason": QUIET}
    auto = task["kind"] in AUTO_KINDS
    now = utcnow()
    signature = _alert_signature(task, summary) if not ok and auto else None
    if signature and _recent_alert(ctx, signature, now):
        hours = float(ctx.setting("notify.renotify_hours", 6))
        return {"sent": False, "reason": f"同样的出错 {hours:g} 小时内发过"}
    sent = _send(ctx, card)
    if signature and sent.get("sent"):
        _remember_alert(ctx, signature, now)
    return sent


def fix_finished(ctx: Ctx, asset_id: str) -> dict[str, Any]:
    """worker 的工位改完一张选款里要求修改的图（精简通知时由 notify.fix_finished 转来）：一行回执。"""
    return _send(ctx, _fix_card(ctx, asset_id))


def failed_sets_card(
    ctx: Ctx, seed_id: str, sets: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """这一轮出完的套图里有没出成的（人取消的不算）：一个任务一张两行红卡；都出成了返回 None（进状态总结）。"""
    bad = [r for r in sets if r["status"] == "failed"]
    if not bad:
        return None
    names = labels.assets(ctx, [r["asset_id"] for r in bad])
    shown = "、".join(
        names.get(r["asset_id"], {}).get("label") or r["asset_id"] for r in bad[:3]
    )
    if len(bad) > 3:
        shown += f" 等 {len(bad)} 款"
    error = next((r["last_error"] for r in bad if r["last_error"]), None)
    return _card(
        f"套图没出成｜{task_label(ctx, seed_id)}",
        [
            f"{len(bad)} 款没出成：{clip(shown, _NAME_CHARS)}"
            + (f"（{clip(error, 50)}）" if error else ""),
            "任务列表点「出套图」重出，或在 POD 对话说「IMG… 重新出套图」",
        ],
        level="alert",
    )


# ---------------------------------------------------------------- 自检


def doctor_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """doctor 里的一项：通知方式；精简时再核对总结时刻与提醒设置写得对不对。"""
    current = style(ctx)
    if current not in STYLES:
        return [
            {
                "check": "通知方式（notify.style）",
                "ok": False,
                "level": "warn",
                "detail": f"只认 {list(STYLES)}，现在写的是 {current!r}：按 full 发",
            }
        ]
    if current != "brief":
        return [
            {
                "check": "通知方式（notify.style）",
                "ok": True,
                "level": "ok",
                "detail": "full：每个后台任务、套图、归档、巡检都发卡片，每天按 daily_report_at 发日报",
            }
        ]
    try:
        detail = "brief：" + settings_text(ctx)
    except ConfigError as exc:
        return [
            {
                "check": "通知方式（notify.style）",
                "ok": False,
                "level": "error",
                "detail": f"brief 的设置写错了：{exc}",
            }
        ]
    return [
        {
            "check": "通知方式（notify.style）",
            "ok": True,
            "level": "ok",
            "detail": detail,
        }
    ]
