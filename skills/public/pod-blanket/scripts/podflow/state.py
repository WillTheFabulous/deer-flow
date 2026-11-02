"""任务状态机与租约。

job.status：ready / running / waiting_external / needs_review / blocked / failed / complete
- 领取：写锁内把一条 ready（或可恢复的 blocked）改成 running 并写租约（jobs.lease_until）；
- 主租约 `worker_lease` 同一时刻只有一个 worker 持有（方案「只有一个负责领取任务的权威入口」）；
  进程里的多条工位共用这一个租约，心跳续主租约，也续本进程名下全部在跑任务的 lease_until；
  交接时先交出主租约、拉起接班的，自己只续手上任务的租约直到做完；
- 回收：只回收 lease_until 已过期的任务（活着的 worker 心跳会续它们），running 回到 ready；
  已提交但没结果的生图请求转为结果未知 + 人工对账，绝不自动重新提交。
"""

from __future__ import annotations

import datetime as dt
import os
from collections import Counter
from typing import Any

from .db import DB, log_event, open_review
from .util import dumps, iso, loads, now_iso, parse_iso, utcnow

WORKER_LEASE_NAME = "worker"
JOB_TERMINAL = frozenset({"complete", "failed"})
# 这些阻断原因在条件恢复后可以自动重新领取；其余阻断需要人处理
RESUMABLE_BLOCKS = frozenset(
    {"daily_cap", "budget_zero", "total_cap", "review_backlog", "deadline"}
)
# 领取顺序：fair = 选款里要求的修改最先，其余按任务（系列）轮流——当前在出最少的先领，一样多时先来先到；
# fifo = 先来先到
QUEUE_ORDERS = ("fair", "fifo")
# 不属于任何任务的调用（拆解、策划、画面描述、图案提取、批次反馈）是别的进程在跑：超过这么久还没结才算遗留
STALE_CALL_MINUTES = 60
_FAIR_CANDIDATES = 500


def _lease_until(seconds: int) -> str:
    return iso(utcnow() + dt.timedelta(seconds=seconds))


def _fair_pick(conn, rows: list[Any]) -> Any:
    """候选里先挑选款里要求修改的任务，再挑当前在出最少的任务（系列），一样多时按原顺序（先来先到）。"""
    marks = ",".join("?" for _ in rows)
    ids = [r["job_id"] for r in rows]
    fixing = {
        r["job_id"]
        for r in conn.execute(
            "SELECT DISTINCT job_id FROM assets WHERE status = 'repair_pending'"
            f" AND human_decision = 'needs_changes' AND job_id IN ({marks})",
            ids,
        ).fetchall()
    }
    batches = sorted({r["batch_id"] for r in rows})
    bmarks = ",".join("?" for _ in batches)
    family_of = {
        r["batch_id"]: r["family_id"]
        for r in conn.execute(
            f"SELECT batch_id, family_id FROM batches WHERE batch_id IN ({bmarks})",
            batches,
        ).fetchall()
    }
    running = Counter(
        r["family_id"]
        for r in conn.execute(
            "SELECT b.family_id FROM jobs j JOIN batches b ON b.batch_id = j.batch_id"
            " WHERE j.status = 'running'"
        ).fetchall()
    )
    return min(
        enumerate(rows),
        key=lambda item: (
            item[1]["job_id"] not in fixing,
            running[family_of.get(item[1]["batch_id"], item[1]["batch_id"])],
            item[0],
        ),
    )[1]


def claim_job(
    db: DB,
    owner: str,
    *,
    lease_seconds: int,
    include_blocked: bool = False,
    job_ids: list[str] | None = None,
    order: str = "fifo",
) -> dict[str, Any] | None:
    """领一个可推进的任务；给了 job_ids 就只在这些任务里领（空列表 = 没有可领的）。"""
    if job_ids is not None and not job_ids:
        return None
    blocked_clause = ""
    if include_blocked:
        reasons = ",".join(f"'{r}'" for r in sorted(RESUMABLE_BLOCKS))
        blocked_clause = f" OR (status = 'blocked' AND blocked_reason IN ({reasons}))"
    only_clause, params = "", ()
    if job_ids:
        only_clause = f" AND job_id IN ({','.join('?' for _ in job_ids)})"
        params = tuple(job_ids)
    fair = order == "fair"
    with db.tx() as conn:
        rows = conn.execute(
            f"SELECT job_id, batch_id FROM jobs WHERE (status = 'ready'{blocked_clause}){only_clause}"
            f" ORDER BY created_at, job_id LIMIT {_FAIR_CANDIDATES if fair else 1}",
            params,
        ).fetchall()
        if not rows:
            return None
        row = _fair_pick(conn, rows) if fair and len(rows) > 1 else rows[0]
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = ?, lease_until = ?, attempt = attempt + 1,"
            " blocked_reason = NULL, resume_condition = NULL, updated_at = ? WHERE job_id = ?",
            (owner, _lease_until(lease_seconds), now_iso(), row["job_id"]),
        )
        log_event(conn, row["job_id"], "job_claimed", {"owner": owner})
        job = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
    return dict(job)


def release_job(
    db: DB,
    job_id: str,
    *,
    status: str,
    stage: str | None = None,
    blocked_reason: str | None = None,
    resume_condition: str | None = None,
    last_error: str | None = None,
) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, stage = COALESCE(?, stage), lease_owner = NULL, lease_until = NULL,"
            " blocked_reason = ?, resume_condition = ?, last_error = COALESCE(?, last_error), updated_at = ?"
            " WHERE job_id = ?",
            (
                status,
                stage,
                blocked_reason,
                resume_condition,
                last_error,
                now_iso(),
                job_id,
            ),
        )
        log_event(
            conn,
            job_id,
            "job_released",
            {"status": status, "stage": stage, "blocked": blocked_reason},
        )


def reopen_job(conn, job_id: str, reason: str) -> None:
    """人工决定让某个素材继续往下走时，把已结束的任务重新放回队列。"""
    conn.execute(
        "UPDATE jobs SET status = 'ready', updated_at = ? WHERE job_id = ? AND status IN ('complete', 'needs_review', 'blocked')",
        (now_iso(), job_id),
    )
    log_event(conn, job_id, "job_reopened", {"reason": reason})


def acquire_worker_lease(
    db: DB, owner: str, *, lease_seconds: int, info: dict[str, Any] | None = None
) -> bool:
    now = now_iso()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM worker_lease WHERE name = ?", (WORKER_LEASE_NAME,)
        ).fetchone()
        if row and row["owner"] != owner and parse_iso(row["lease_until"]) > utcnow():
            return False
        conn.execute(
            "INSERT INTO worker_lease(name, owner, pid, started_at, heartbeat_at, lease_until, info_json)"
            " VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, pid = excluded.pid,"
            " started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at,"
            " lease_until = excluded.lease_until, info_json = excluded.info_json",
            (
                WORKER_LEASE_NAME,
                owner,
                os.getpid(),
                now,
                now,
                _lease_until(lease_seconds),
                dumps(info or {}),
            ),
        )
        log_event(
            conn,
            "worker",
            "worker_lease_acquired",
            {"owner": owner, "previous": dict(row) if row else None},
        )
    return True


def heartbeat_worker(
    db: DB,
    owner: str,
    *,
    lease_seconds: int,
    info: dict[str, Any] | None = None,
    primary: bool = True,
) -> bool:
    """续主租约（primary）与本进程名下在跑任务的 lease_until；主租约被别人拿走时返回 False。

    交出主租约后（交接中）只续任务：接班的 worker 不会回收这些任务，做完的步骤照常交还。
    """
    with db.tx() as conn:
        ok = True
        if primary:
            cur = conn.execute(
                "UPDATE worker_lease SET heartbeat_at = ?, lease_until = ?, info_json = COALESCE(?, info_json)"
                " WHERE name = ? AND owner = ?",
                (
                    now_iso(),
                    _lease_until(lease_seconds),
                    None if info is None else dumps(info),
                    WORKER_LEASE_NAME,
                    owner,
                ),
            )
            ok = cur.rowcount == 1
        conn.execute(
            "UPDATE jobs SET lease_until = ? WHERE lease_owner = ? AND status = 'running'",
            (_lease_until(lease_seconds), owner),
        )
        return ok


def release_worker_lease(db: DB, owner: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM worker_lease WHERE name = ? AND owner = ?",
            (WORKER_LEASE_NAME, owner),
        )
        log_event(conn, "worker", "worker_lease_released", {"owner": owner})


def worker_status(db: DB) -> dict[str, Any] | None:
    row = db.q1("SELECT * FROM worker_lease WHERE name = ?", (WORKER_LEASE_NAME,))
    if not row:
        return None
    data = dict(row)
    data["alive"] = parse_iso(row["lease_until"]) > utcnow()
    data["info"] = loads(row["info_json"])
    data.pop("info_json", None)
    return data


def _call_is_stale(
    row: Any,
    dead_jobs: set[str],
    job_status: dict[str, str],
    cutoff: dt.datetime,
) -> bool:
    """调用是不是没人管了：属于刚回收的任务；任务已经不在跑；不属于任务、又超过 STALE_CALL_MINUTES。"""
    scope = row["scope_id"]
    if scope in dead_jobs:
        return True
    if scope in job_status:
        # 任务还在跑、租约也没过期：是活着的 worker 正在发的请求
        return job_status[scope] != "running"
    return parse_iso(row["created_at"]) < cutoff


def recover_stale(db: DB, owner: str) -> dict[str, Any]:
    """回收死掉的进程留下的东西：lease_until 过期的在跑任务回到 ready，它们没结的调用转结果未知 / 释放。

    活着的 worker（包括交接中、还在做手上几步的上一任）心跳会续任务租约，不会被回收；
    拆解、策划、画面描述、图案提取这些不属于任务的调用，超过 STALE_CALL_MINUTES 还没结才算遗留。
    """
    recovered: dict[str, list[str]] = {
        "jobs": [],
        "image_calls_unknown": [],
        "llm_calls_unknown": [],
        "reservations_released": [],
    }
    now = utcnow()
    with db.tx() as conn:
        dead: set[str] = set()
        for row in conn.execute(
            "SELECT job_id, lease_until FROM jobs WHERE status = 'running' AND (lease_owner IS NULL OR lease_owner != ?)",
            (owner,),
        ).fetchall():
            if row["lease_until"] and parse_iso(row["lease_until"]) > now:
                continue
            conn.execute(
                "UPDATE jobs SET status = 'ready', lease_owner = NULL, lease_until = NULL, updated_at = ? WHERE job_id = ?",
                (now_iso(), row["job_id"]),
            )
            dead.add(row["job_id"])
            recovered["jobs"].append(row["job_id"])
            log_event(conn, row["job_id"], "job_recovered", {"by": owner})
        calls = conn.execute(
            "SELECT call_id, kind, scope_id, asset_id, created_at FROM provider_calls"
            " WHERE status IN ('reserved', 'submitted') AND (owner IS NULL OR owner != ?)",
            (owner,),
        ).fetchall()
        scopes = sorted({c["scope_id"] for c in calls})
        job_status: dict[str, str] = {}
        if scopes:
            marks = ",".join("?" for _ in scopes)
            job_status = {
                r["job_id"]: r["status"]
                for r in conn.execute(
                    f"SELECT job_id, status FROM jobs WHERE job_id IN ({marks})", scopes
                ).fetchall()
            }
        cutoff = now - dt.timedelta(minutes=STALE_CALL_MINUTES)
        for row in calls:
            if not _call_is_stale(row, dead, job_status, cutoff):
                continue
            call = conn.execute(
                "SELECT status FROM provider_calls WHERE call_id = ?", (row["call_id"],)
            ).fetchone()
            if call["status"] == "reserved":
                # 预留后还没发出去进程就没了：确定没有发生调用，释放
                conn.execute(
                    "UPDATE provider_calls SET status = 'failed', cost_status = 'released', actual_cost = 0,"
                    " error = '进程中断，调用未发出', finished_at = ? WHERE call_id = ?",
                    (now_iso(), row["call_id"]),
                )
                recovered["reservations_released"].append(row["call_id"])
                continue
            conn.execute(
                "UPDATE provider_calls SET status = 'unknown', cost_status = 'unknown',"
                " error = '进程中断，已提交但结果未知', finished_at = ? WHERE call_id = ?",
                (now_iso(), row["call_id"]),
            )
            if row["kind"] == "image":
                recovered["image_calls_unknown"].append(row["call_id"])
                conn.execute(
                    "UPDATE job_slots SET status = 'unknown', last_error = '进程中断，结果未知', updated_at = ?"
                    " WHERE job_id = ? AND status = 'pending' AND slot_id IN"
                    f" (SELECT {db.json_text('request_meta_json', 'slot_id')} FROM provider_calls WHERE call_id = ?)",
                    (now_iso(), row["scope_id"], row["call_id"]),
                )
                if row["asset_id"]:
                    # 修复请求：被修的那张图停在「修复结果未知」，对账前不再重修
                    conn.execute(
                        "UPDATE assets SET status = 'repair_unknown', updated_at = ? WHERE asset_id = ? AND status = 'repair_pending'",
                        (now_iso(), row["asset_id"]),
                    )
                open_review(
                    conn,
                    "call_reconcile",
                    row["call_id"],
                    "生图请求已提交但进程中断，结果与扣费未知：按请求查供应商记录后对账",
                    {"scope_id": row["scope_id"]},
                )
            else:
                recovered["llm_calls_unknown"].append(row["call_id"])
        if any(recovered.values()):
            log_event(conn, "worker", "stale_recovered", recovered)
    return recovered
