"""后台任务：统一拉起后台 podctl 进程并登记进度，工作台与通知卡片据此显示「正在做什么」。

子进程靠环境变量 PODFLOW_BG_TASK 认领自己的登记行：开始时记 running，结束时记 done / failed，
带 notify 的任务结束后发一张完成卡片。进程意外退出、登记却还停在 running 的，查询时改记 failed。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .context import Ctx
from .db import log_event, next_id
from .paths import stable_podctl
from .util import dumps, loads, now_iso, parse_iso, utcnow

ENV_KEY = "PODFLOW_BG_TASK"
KIND_LABELS = {
    "analyze": "母版拆解",
    "plan": "裂变策划",
    "worker": "出图",
    "fix": "修改",
    "revise": "系列修订",
    "sync": "同步 OSS",
    "table": "同步选款表",
    "accept": "选款验收",
    "dispatch": "提前出套图",
    "controls": "回读候选表按钮行",
    "extract": "图案提取",
    "describe": "画面描述",
    "scenes": "出套图",
    "derive": "二次大变化策划",
    "temu": "Temu 上架",
    "ticks": "候选表店铺勾（撤销窗口）",
}
_STARTING_GRACE_SECONDS = 60

_current: str | None = None


def spawn(
    ctx: Ctx,
    argv: list[str],
    *,
    kind: str,
    subject: str | None = None,
    notify: bool = False,
) -> dict[str, Any]:
    logs = ctx.home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    # 精确到毫秒：多条工位同一秒踢起的两个同类后台任务不会写进同一个日志
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")[:-3]
    log_path = logs / f"{kind}-{subject or 'all'}-{stamp}.log"
    with ctx.db.tx() as conn:
        task_id = next_id(conn, "bgtask")
        conn.execute(
            "INSERT INTO bg_tasks(task_id, kind, subject, argv_json, status, notify, log_path, started_at, updated_at)"
            " VALUES(?, ?, ?, ?, 'starting', ?, ?, ?, ?)",
            (
                task_id,
                kind,
                subject,
                dumps(argv),
                int(notify),
                str(log_path.relative_to(ctx.home)),
                now_iso(),
                now_iso(),
            ),
        )
        log_event(conn, task_id, "bg_started", {"kind": kind, "subject": subject})
    env = os.environ.copy()
    env["PODFLOW_HOME"] = str(ctx.home)
    env[ENV_KEY] = task_id
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(  # noqa: S603 - 固定调用本 skill 的 podctl
            [sys.executable, str(stable_podctl()), *argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET pid = ?, updated_at = ? WHERE task_id = ?",
            (proc.pid, now_iso(), task_id),
        )
    return {
        "detached": True,
        "task_id": task_id,
        "pid": proc.pid,
        "log": str(log_path.relative_to(ctx.home)),
        "hint": "用 podctl status / podctl log 查看进度",
    }


def adopt() -> str | None:
    """子进程认领登记行；环境变量随即移除，免得再往下拉起的进程误认。"""
    global _current
    _current = os.environ.pop(ENV_KEY, None) or None
    return _current


def current() -> str | None:
    return _current


def mark_running(ctx: Ctx, task_id: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET status = 'running', pid = ?, updated_at = ? WHERE task_id = ?",
            (os.getpid(), now_iso(), task_id),
        )


def step(ctx: Ctx, label: str) -> None:
    if not _current:
        return
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET step = ?, updated_at = ? WHERE task_id = ?",
            (label, now_iso(), _current),
        )


def _auto_summary(confirmed: dict[str, Any] | None) -> dict[str, Any] | None:
    """自动确认的结果（directions.auto_confirm）：选了几个、共几张图、出图开始了还是在排队。"""
    if not confirmed:
        return None
    return {
        "picked": len(confirmed.get("picked") or []),
        "images": confirmed.get("images") or 0,
        "started": confirmed.get("directions_started"),
        "queued": bool(confirmed.get("directions_queued")),
        "reason": confirmed.get("reason"),
        "family_approved": bool(confirmed.get("family_approved")),
        "backlog": confirmed.get("backlog"),
    }


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    accepted = plan.get("accepted") or []
    kinds = Counter(t.get("kind") or t.get("task_type") for t in accepted)

    def line(t: dict[str, Any], i: int) -> str:
        # 完成卡片逐行列出方向：「1. 雾蓝底色（配色结构）」
        return (
            f"{t.get('index', i)}. {t.get('title') or t.get('axis') or t.get('variant_id')}"
            + (f"（{' + '.join(t['aspects'])}）" if t.get("aspects") else "")
        )

    return {
        "batch_id": plan.get("batch_id"),
        "family_id": plan.get("family_id"),
        "family_status": plan.get("family_status"),
        "hint": plan.get("hint"),
        "accepted": len(accepted),
        "small": kinds.get("small", 0),
        # 大变化含三处组合与二次大变化
        "big": kinds.get("big", 0) + kinds.get("combo", 0) + kinds.get("derived", 0),
        "combo": kinds.get("combo", 0),
        "derived": kinds.get("derived", 0),
        # 二次大变化批次：底图来自哪个原批次
        "derived_from": plan.get("derived_from"),
        "titles": [
            line(t, i)
            for i, t in enumerate(accepted, 1)
            if t.get("task_type") == "small"
        ],
        "big_titles": [
            line(t, i)
            for i, t in enumerate(accepted, 1)
            if t.get("task_type") != "small"
        ],
        "dropped": dict(Counter(d.get("reason") for d in plan.get("dropped") or [])),
        "gaps": len(plan.get("coverage_gaps") or []),
        "failed_groups": [
            g.get("group") for g in plan.get("failed_groups") or [] if g.get("group")
        ],
        "exhausted": bool(plan.get("exhausted")),
        "auto": _auto_summary(plan.get("auto_confirm")),
        "auto_error": plan.get("auto_confirm_error"),
        # 这一轮按字段策划（没有可变单元清单 / plan.unit 是 fields）的原因；按单元策划时为空
        "unit_reason": plan.get("unit_reason"),
    }


def summarize(kind: str, output: dict[str, Any]) -> dict[str, Any]:
    """完成卡片与动态里用的结果摘要。"""
    if output.get("error"):
        detail = (
            output.get("detail") or output.get("errors") or output.get("reason") or ""
        )
        return {"error": str(output["error"]), "detail": str(detail)[:300]}
    if kind == "plan":
        return _plan_summary(output)
    if kind == "derive":
        # run_due 给 {planned, skipped}；手动 plan --derive-from 给一份策划结果
        plans = output.get("planned") if "planned" in output else [output]
        return {
            "plans": [_plan_summary(p) for p in plans or []],
            "skipped": [
                {k: s.get(k) for k in ("source", "reason")}
                for s in output.get("skipped") or []
            ],
        }
    if kind in ("worker", "fix"):
        continued = output.get("continued") or {}
        return {
            "jobs": len(output.get("jobs") or []),
            "stop_reason": output.get("stop_reason"),
            "lanes": output.get("lanes"),
            "batch_reports": [
                r.get("batch_id") for r in output.get("batch_reports") or []
            ],
            "notes": output.get("notes") or [],
            # 选中的方向还没出完、已经接上下一个 worker（含交接）：这一次不发卡片
            "continued": bool(
                continued.get("directions_started")
                or continued.get("after_fix")
                or continued.get("handoff")
            ),
            # 没抢到 worker 租约（别的 worker 在出图）：什么也没做，排队的任务由它接着领
            "busy": output.get("status") == "busy",
            # 工位做完一个修改就发过卡片的素材；只做了修改时不再发「出图一轮结束」
            "fix_cards": output.get("fix_cards") or [],
            "fix_only": bool(output.get("fix_only")),
        }
    planned: dict[str, Any] = {}
    if output.get("plan"):
        planned = {"plan": _plan_summary(output["plan"])}
    elif output.get("plan_error"):
        planned = {"plan_error": str(output["plan_error"])[:300]}
    if kind == "analyze":
        steps = output.get("steps") or []
        family = next((s for s in steps if s.get("stage") == "S04"), {})
        return {
            "seed_id": output.get("seed_id"),
            "status": output.get("status"),
            "stopped_at": output.get("stopped_at"),
            "family_id": family.get("family_id")
            or (planned.get("plan") or {}).get("family_id"),
            "family_version": family.get("family_version"),
            **(
                {"description_error": str(output["description_error"])[:300]}
                if output.get("description_error")
                else {}
            ),
            **({"units": output["units"][:12]} if output.get("units") else {}),
            **planned,
        }
    if kind == "revise":
        return {
            "family_id": output.get("family_id"),
            "family_version": output.get("family_version"),
            **planned,
        }
    if kind == "sync":
        return {k: output.get(k) for k in ("uploaded", "failed", "pending", "skipped")}
    if kind == "table":
        return {
            k: output.get(k)
            for k in ("created", "added", "refreshed", "removed", "failed", "skipped")
        }
    if kind == "extract":
        return {
            k: output.get(k)
            for k in ("seed_id", "model", "version", "pattern_path", "hint")
        }
    if kind == "describe":
        if "described" in output:
            return {
                "described": [d.get("seed_id") for d in output.get("described") or []],
                "skipped": len(output.get("skipped") or []),
                "failed": [
                    f"{f.get('seed_id')} {f.get('error')}"[:200]
                    for f in output.get("failed") or []
                ],
            }
        return {
            k: output.get(k)
            for k in (
                "seed_id",
                "analysis_id",
                "summary",
                "subjects",
                "units",
                "hint",
                "skipped",
            )
        }
    if kind == "accept":
        return {
            "seed_id": output.get("seed_id"),
            "approved": len(output.get("approved") or []),
            "rejected": len(output.get("rejected") or []),
            "already_decided": len(output.get("already_decided") or []),
            "not_in_table": len(output.get("not_in_table") or []),
            "fresh": len(output.get("fresh") or []),
            "waiting": output.get("waiting") or 0,
            "table_url": output.get("table_url"),
            "scenes": output.get("scenes") or {},
            "backlog": output.get("backlog"),
            # 验收后马上接着出图：started 已拉起 / queued 排在正在跑的出图后面
            "generation": "started"
            if (output.get("generation") or {}).get("directions_started")
            else "queued"
            if (output.get("generation") or {}).get("directions_queued")
            else None,
            "source": output.get("source"),
        }
    if kind == "dispatch":
        return {
            "seed_id": output.get("seed_id"),
            "approved": len(output.get("approved") or []),
            "already_decided": len(output.get("already_decided") or []),
            "picked": len(output.get("picked") or []),
            "table_url": output.get("table_url"),
            "scenes": output.get("scenes") or {},
            "source": output.get("source"),
        }
    if kind == "temu":
        return {
            "prepared": len(output.get("prepared") or []),
            "boxed": output.get("boxed") or [],
            "published": len(output.get("published") or []),
            "listed": output.get("listed") or [],
            "failed": len(output.get("failed") or []),
            "waiting": len(output.get("waiting") or []),
            "stopped": output.get("stopped"),
            "skipped": output.get("skipped"),
        }
    if kind == "scenes":
        if "refreshed" in output:
            # scenes refresh --all
            return {
                k: len(output.get(k) or []) for k in ("refreshed", "running", "failed")
            }
        packed = output.get("packages") or {}
        return {
            "submitted": len(output.get("submitted") or []),
            "finished": len(output.get("finished") or []),
            "active": output.get("active"),
            "stopped": output.get("stopped"),
            "skipped": output.get("skipped"),
            "packages_built": len(packed.get("built") or []),
            "packages_failed": len(packed.get("failed") or []),
        }
    return {}


def finish(ctx: Ctx, task_id: str, output: dict[str, Any], *, ok: bool) -> None:
    row = ctx.db.q1("SELECT * FROM bg_tasks WHERE task_id = ?", (task_id,))
    if not row:
        return
    summary = summarize(row["kind"], output)
    status = "done" if ok and "error" not in summary else "failed"
    if row["notify"]:
        from . import notify  # 通知依赖视图模块，避免与阶段模块循环导入

        summary["notified"] = notify.bg_finished(
            ctx, dict(row), summary, ok=status == "done"
        )
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET status = ?, result_json = ?, finished_at = ?, updated_at = ? WHERE task_id = ?",
            (status, dumps(summary), now_iso(), now_iso(), task_id),
        )
        log_event(
            conn,
            task_id,
            "bg_finished",
            {"kind": row["kind"], "subject": row["subject"], "status": status},
        )


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    try:
        # 已退出但还没被回收的僵尸进程也算结束；进程名里可能带括号，从右边切
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def running(ctx: Ctx) -> list[dict[str, Any]]:
    """进程还活着的后台任务；进程已退出却仍记着 running 的改记 failed。"""
    alive = []
    for row in ctx.db.q(
        "SELECT * FROM bg_tasks WHERE status IN ('starting', 'running') ORDER BY started_at"
    ):
        item = dict(row)
        age = (utcnow() - parse_iso(item["started_at"])).total_seconds()
        if _alive(item["pid"]) or (
            item["pid"] is None and age < _STARTING_GRACE_SECONDS
        ):
            item["elapsed_minutes"] = int(age // 60)
            alive.append(item)
            continue
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE bg_tasks SET status = 'failed', result_json = ?, finished_at = ?, updated_at = ?"
                " WHERE task_id = ? AND status IN ('starting', 'running')",
                (
                    dumps({"error": "exited", "detail": "进程已退出，没有留下结果"}),
                    now_iso(),
                    now_iso(),
                    item["task_id"],
                ),
            )
            log_event(
                conn,
                item["task_id"],
                "bg_finished",
                {"kind": item["kind"], "subject": item["subject"], "status": "failed"},
            )
    return alive


def recent(ctx: Ctx, limit: int = 20) -> list[dict[str, Any]]:
    rows = ctx.db.q(
        "SELECT * FROM bg_tasks WHERE status IN ('done', 'failed') ORDER BY finished_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["result"] = loads(item.pop("result_json"), {}) or {}
        out.append(item)
    return out
