"""出图 worker：持全局主租约 + 心跳，回收死掉的进程留下的任务，多条工位同时推进，没活或要交接时退出。

方案「运行层分工」：DeerFlow 定时任务只负责唤醒；真正领取任务的权威入口只有持主租约的这个进程（worker_lease）。
进程里开 batch.workers 条工位，每条一次推进一个任务（S06 → T01 → S07 → S09 → S10）；生图 / LLM 请求共用进程内的
名额（limits：batch.image_concurrency / batch.llm_concurrency），慢的那张图只占一个名额、一条工位，别的工位照常推进。

- 领什么：给了 --job 只领这些；来源是 directions / fix 的每次领取都现查「人确认过的方向 + 选款里要求修改的任务」，
  新确认的批次、刚点的「修改」几秒内就开工；其余来源（巡检、手动「开始出图」）领全部就绪任务，巡检拉起的在连续运行
  窗口关掉后只领确认过的方向和修改。领取顺序见 state.claim_job（batch.queue_order）。
- 什么时候退出：没活等 batch.worker_idle_seconds（前台跑的不等）；领满 --batch 个；暂停、丢主租约时工位在下一步开始前
  放下任务；跑满 batch.worker_lifetime_minutes 或 podflow.yaml / secrets.env / 引擎代码有变化（稳定 30 秒后）就交接：
  先交出主租约、拉起接班的 worker，自己只把手上这几步做完再退出，不留空档。
- 批次反馈（S11）只由持主租约的主线程跑，同时只跑一个。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import bg, budget, directions, fixes, limits, llm, report, state, store
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .stages.feedback import finished_batches, run_s11
from .stages.job import advance_job, backlog_count
from .stages.plan import plan_batch, plannable_families
from .util import dumps, now_iso, utcnow

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
# 心跳连续失败这么久视为丢了主租约（租约 120 秒，留出余量）
LEASE_LOST_SECONDS = 90
# 没活时工位多久再看一次
POLL_SECONDS = 5.0
# 主线程最长多久看一次暂停、寿命、配置变化（工位状态一变就会叫醒它）
CHECK_SECONDS = 1.0
FEEDBACK_EVERY_SECONDS = 60.0
RELOAD_CHECK_SECONDS = 10.0
RELOAD_STABLE_SECONDS = 30.0
# 来源是这些的 worker 每次领取都现查「确认过的方向 + 选款里要求修改的任务」
CHOSEN_SOURCES = ("directions", "fix")
# 交接、暂停、丢租约：工位在下一步开始前把任务放回队列
RELEASE_REASONS = ("paused", "lease_lost", "lifetime", "reload")


def is_paused(ctx: Ctx) -> bool:
    return ctx.db.get_meta("paused") == "1"


def min_call_estimate(ctx: Ctx) -> float | None:
    """最便宜的一次调用预估（用于判断「还剩的预算够不够干点什么」）；价格未知返回 None。"""
    try:
        return budget.estimate_llm_cost(ctx.cfg, llm.stage_model(ctx, "P07"), "P07")
    except budget.BudgetBlocked:
        return None


def blocked_resumable(ctx: Ctx) -> bool:
    estimate = min_call_estimate(ctx)
    if estimate is None:
        return False
    ok, _ = budget.can_afford(ctx.db, ctx.cfg, estimate)
    return ok and backlog_count(ctx) < int(
        ctx.setting("batch.review_backlog_limit", 50)
    )


def plannable_family(ctx: Ctx) -> str | None:
    """可以再策划一轮的系列（每次 worker 最多补一批候选方向，出图仍要人挑选）。"""
    families = plannable_families(ctx)
    return families[0] if families else None


def chosen_jobs(ctx: Ctx) -> list[str]:
    """选款里要求修改的任务 + 人确认过的方向里等着出图的任务（来源是 directions / fix 的 worker 只领这些）。"""
    ids = list(dict.fromkeys(item["job_id"] for item in fixes.pending(ctx)))
    ids += [job_id for job_id in directions.pending_jobs(ctx) if job_id not in ids]
    return ids


class _Reload:
    """podflow.yaml / secrets.env / 引擎代码变了：稳定 RELOAD_STABLE_SECONDS 后交接给新进程（新配置、新代码）。"""

    def __init__(self, ctx: Ctx) -> None:
        self.paths = [ctx.home / "podflow.yaml", ctx.home / "secrets.env"]
        self.code = Path(__file__).resolve().parent
        self.base = self._fingerprint()
        self.seen = self.base
        self.since: float | None = None
        self.checked = time.monotonic()

    def _fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        out = []
        for path in [*self.paths, *sorted(self.code.rglob("*.py"))]:
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((str(path), st.st_mtime_ns, st.st_size))
        return tuple(out)

    def changed(self) -> bool:
        now = time.monotonic()
        if now - self.checked < RELOAD_CHECK_SECONDS:
            return False
        self.checked = now
        current = self._fingerprint()
        if current == self.base:
            self.seen, self.since = current, None
            return False
        if current != self.seen:
            # 还在拷文件：等它停下来再交接，免得接班的进程读到一半新一半旧的代码
            self.seen, self.since = current, now
            return False
        return self.since is not None and now - self.since >= RELOAD_STABLE_SECONDS


class Worker:
    def __init__(
        self,
        ctx: Ctx,
        *,
        batch_limit: int | None = None,
        max_minutes: float | None = None,
        allow_plan: bool | None = None,
        allow_generation: bool = True,
        source: str = "manual",
        only_jobs: list[str] | None = None,
        lanes: int | None = None,
        idle_seconds: float | None = None,
    ):
        self.ctx = ctx
        self.only_jobs = list(dict.fromkeys(only_jobs)) if only_jobs else None
        # 显式给了（--batch、测试）才限制这次领几个；缺省有活一直领
        self.batch_limit = int(batch_limit) if batch_limit else None
        self.max_minutes = float(
            max_minutes or ctx.setting("batch.worker_lifetime_minutes", 120)
        )
        self.allow_plan = (
            bool(ctx.setting("batch.auto_plan", False))
            if allow_plan is None
            else allow_plan
        )
        self.allow_generation = allow_generation
        self.source = source
        count = max(1, int(lanes or ctx.setting("batch.workers", 1)))
        for cap in (self.batch_limit, len(self.only_jobs or []) or None):
            if cap:
                count = min(count, cap)
        self.lanes = count
        if idle_seconds is None:
            # 后台 worker 没活时等一会儿（刚确认的方向、刚点的修改直接接上）；前台跑的没活就退出
            idle_seconds = (
                float(ctx.setting("batch.worker_idle_seconds", 60))
                if bg.current()
                else 0.0
            )
        self.idle_seconds = max(0.0, float(idle_seconds))
        order = str(ctx.setting("batch.queue_order", "fair"))
        self.queue_order = order if order in state.QUEUE_ORDERS else "fair"
        self.reload_check = bool(ctx.setting("batch.worker_reload_check", True))
        self._lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._feedback_lock = threading.Lock()
        # 工位状态变了：叫醒主线程；有新活或要收尾了：叫醒等活的工位（_cond）；心跳线程停
        self._changed = threading.Event()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._lease_lost = threading.Event()
        self._stop_reason: str | None = None
        self._release = False
        self._primary = True
        self._claimed = 0
        self._jobs: list[dict[str, Any]] = []
        self._busy: dict[int, dict[str, Any]] = {}
        self._idle: set[int] = set()
        self._idle_since: float | None = None
        # 上次看到「还有能领的活」时已领了几个：之后一个都没领到就不再看，照常退出
        self._peeked_at = -1
        self._fix_jobs: set[str] = set()
        self._fix_cards: list[str] = []
        self._notes: list[str] = []
        self._reports: list[dict[str, Any]] = []
        self._feedback_failed: set[str] = set()
        self._planned: list[dict[str, Any]] = []
        self._plan_tried = False
        self._continued: dict[str, Any] = {}
        self._last_beat = time.monotonic()

    # ------------------------------------------------------------ 状态

    def _info(self) -> dict[str, Any]:
        with self._lock:
            busy = [
                {"lane": lane + 1, "job_id": item["job_id"], "since": item["since"]}
                for lane, item in sorted(self._busy.items())
            ]
            claimed, stopping = self._claimed, self._stop_reason
        return {
            "source": self.source,
            "lanes": self.lanes,
            "busy": busy,
            "claimed": claimed,
            "batch_limit": self.batch_limit,
            "max_minutes": self.max_minutes,
            "only_jobs": self.only_jobs,
            "stopping": stopping,
            "limits": limits.snapshot(),
        }

    def _note(self, text: str) -> None:
        with self._lock:
            self._notes.append(text[:300])

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            primary = self._primary
            try:
                ok = state.heartbeat_worker(
                    self.ctx.db,
                    self.ctx.owner,
                    lease_seconds=LEASE_SECONDS,
                    info=self._info(),
                    primary=primary,
                )
                self._last_beat = time.monotonic()
            except Exception:  # noqa: BLE001 - 库一时连不上：下一跳再试，连续失败太久才算丢租约
                ok = time.monotonic() - self._last_beat < LEASE_LOST_SECONDS
            finally:
                self.ctx.db.close()
            # 交接时自己交出的主租约不算丢
            if not ok and primary and self._primary:
                self._lease_lost.set()
                self._changed.set()

    def _should_stop(self) -> str | None:
        """工位每一步开始前问一次：要停就返回原因，任务放回队列。"""
        if self._lease_lost.is_set():
            return "lease_lost"
        if self._release:
            return self._stop_reason or "paused"
        if is_paused(self.ctx):
            return "paused"
        return None

    # ------------------------------------------------------------ 工位

    def _window_open(self) -> bool:
        if not self.ctx.setting("trial.require_window_for_tick", True):
            return True
        return bool(report.trial_window(self.ctx)["active"])

    def _scope(self) -> list[str] | None:
        """这次能领的任务：None = 全部就绪任务。"""
        if self.only_jobs is not None:
            return self.only_jobs
        if self.source in CHOSEN_SOURCES or (
            self.source == "tick" and not self._window_open()
        ):
            return chosen_jobs(self.ctx)
        return None

    def _claim(self, lane: int) -> dict[str, Any] | None:
        ctx = self.ctx
        with self._claim_lock:
            if self._stop_reason or (
                self.batch_limit and self._claimed >= self.batch_limit
            ):
                return None
            job = state.claim_job(
                ctx.db,
                ctx.owner,
                lease_seconds=LEASE_SECONDS,
                include_blocked=blocked_resumable(ctx),
                job_ids=self._scope(),
                order=self.queue_order,
            )
            if job is None:
                return None
            with self._lock:
                self._claimed += 1
                self._idle.discard(lane)
                self._idle_since = None
                self._busy[lane] = {"job_id": job["job_id"], "since": now_iso()}
        self._changed.set()
        return job

    def _set_idle(self, lane: int) -> None:
        with self._lock:
            self._busy.pop(lane, None)
            self._idle.add(lane)
            if len(self._idle) >= self.lanes and self._idle_since is None:
                self._idle_since = time.monotonic()
        self._changed.set()

    def _fix_assets(self, job_id: str) -> list[str]:
        return [
            r["asset_id"]
            for r in self.ctx.db.q(
                "SELECT asset_id FROM assets WHERE job_id = ? AND status = 'repair_pending'"
                " AND human_decision = 'needs_changes' ORDER BY asset_id",
                (job_id,),
            )
        ]

    def _send_fix_cards(self, assets: list[str]) -> None:
        """选款里要求的修改：一个任务做完就发卡片，不等整个 worker 结束。"""
        from . import notify  # 通知依赖视图模块，放到用时再导入

        for asset_id in assets:
            status = self.ctx.db.scalar(
                "SELECT status FROM assets WHERE asset_id = ?", (asset_id,)
            )
            if status == "repair_pending":
                # 还没改（交接 / 暂停时放回了队列）：接手的那个做完再发
                continue
            try:
                notify.fix_finished(self.ctx, asset_id)
            except Exception as exc:  # noqa: BLE001 - 发卡失败不影响出图
                self._note(f"{asset_id} 修改卡片没发出去：{exc}")
            with self._lock:
                self._fix_cards.append(asset_id)

    def _run_job(self, lane: int, job: dict[str, Any]) -> None:
        ctx = self.ctx
        job_id = job["job_id"]
        fixing = self._fix_assets(job_id)
        if fixing:
            with self._lock:
                self._fix_jobs.add(job_id)
        try:
            result = advance_job(
                ctx,
                job_id,
                allow_generation=self.allow_generation,
                should_stop=self._should_stop,
            )
        except Exception as exc:  # noqa: BLE001 - 程序缺陷不拖垮别的工位：任务放回队列，记下原因
            detail = f"{type(exc).__name__}: {exc}"
            try:
                state.release_job(
                    ctx.db,
                    job_id,
                    status="ready",
                    last_error=f"工位出错：{detail}"[:2000],
                )
            except Exception:  # noqa: BLE001 - 库也连不上：等进程结束后由回收接上
                pass
            result = {"job_id": job_id, "status": "ready", "steps": [], "error": detail}
            self._note(f"{job_id} 推进出错，已放回队列：{detail}")
        with self._lock:
            self._jobs.append(result)
            self._busy.pop(lane, None)
        self._changed.set()
        if fixing:
            self._send_fix_cards(fixing)

    def _lane(self, lane: int) -> None:
        try:
            while not self._stop_reason and not self._should_stop():
                try:
                    job = self._claim(lane)
                except Exception as exc:  # noqa: BLE001 - 库一时连不上：等一会儿再领
                    self._note(
                        f"工位 {lane + 1} 领任务出错：{type(exc).__name__}: {exc}"
                    )
                    job = None
                if job is None:
                    self._set_idle(lane)
                    with self._cond:
                        if not self._stop_reason:
                            self._cond.wait(POLL_SECONDS)
                    continue
                self._run_job(lane, job)
        finally:
            self._set_idle(lane)
            self.ctx.db.close()

    # ------------------------------------------------------------ 主线程

    def _nudge(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _finish(self, reason: str) -> None:
        """不再领新的；手上的任务做完（没活、领满）。"""
        with self._lock:
            if self._stop_reason:
                return
            self._stop_reason = reason
        self._nudge()

    def _halt(self, reason: str) -> None:
        """不再领新的；手上的任务在下一步开始前放回队列（暂停、丢租约）。"""
        with self._lock:
            if self._stop_reason:
                return
            self._stop_reason = reason
            self._release = True
        self._nudge()

    def _handoff(self, reason: str) -> None:
        """交接：交出主租约、拉起接班的 worker；手上的任务在下一步开始前放回队列，由接班的接着做。"""
        self._halt(reason)
        if self._stop_reason != reason:
            return
        ctx = self.ctx
        # 先交出主租约，接班的才拿得到；手上的任务心跳照续，接班的不会回收
        self._primary = False
        state.release_worker_lease(ctx.db, ctx.owner)
        try:
            self._continued = self._spawn_successor()
        except Exception as exc:  # noqa: BLE001 - 拉不起来就照旧收尾，巡检会接上
            self._note(f"拉起接班 worker 失败：{exc}")
            self._continued = {}

    def _spawn_successor(self) -> dict[str, Any]:
        ctx = self.ctx
        pending = self._scope()
        if pending is None:
            pending = [
                r["job_id"]
                for r in ctx.db.q(
                    "SELECT job_id FROM jobs WHERE status = 'ready' ORDER BY created_at, job_id LIMIT 200"
                )
            ]
        with self._lock:
            mine = sorted(item["job_id"] for item in self._busy.values())
        if not pending and not mine:
            return {}
        current = bg.current()
        row = (
            ctx.db.q1(
                "SELECT kind, subject, notify FROM bg_tasks WHERE task_id = ?",
                (current,),
            )
            if current
            else None
        )
        argv = ["worker", "--source", self.source]
        for job_id in self.only_jobs or []:
            argv += ["--job", job_id]
        if not self.allow_generation:
            argv.append("--no-generation")
        task = bg.spawn(
            ctx,
            argv,
            kind=row["kind"] if row else "worker",
            subject=row["subject"] if row else None,
            notify=bool(row["notify"]) if row else False,
        )
        return {"handoff": task["task_id"], "jobs": pending or mine}

    def _peek_work(self) -> bool:
        """现在有没有能领的活（工位还在睡的这几秒里新确认的方向、刚点的修改）。"""
        ctx = self.ctx
        if self.only_jobs is None:
            ids = self._scope()
            if ids is not None:
                return bool(ids)
        clause, params = "", ()
        if self.only_jobs is not None:
            clause = f" AND job_id IN ({','.join('?' for _ in self.only_jobs)})"
            params = tuple(self.only_jobs)
        blocked = ""
        if blocked_resumable(ctx):
            reasons = ",".join(f"'{r}'" for r in sorted(state.RESUMABLE_BLOCKS))
            blocked = f" OR (status = 'blocked' AND blocked_reason IN ({reasons}))"
        return bool(
            ctx.db.scalar(
                f"SELECT COUNT(*) FROM jobs WHERE (status = 'ready'{blocked}){clause}",
                params,
            )
        )

    def _queue_empty(self) -> bool:
        with self._lock:
            if self._busy or len(self._idle) < self.lanes or self._idle_since is None:
                return False
            return time.monotonic() - self._idle_since >= self.idle_seconds

    def _plan_more(self) -> bool:
        """队列空了、开着 batch.auto_plan：补策划一批候选方向（每个 worker 最多一次，出图仍要人挑选）。"""
        if self._plan_tried or not (
            self.allow_plan and self.allow_generation and not self.only_jobs
        ):
            return False
        self._plan_tried = True
        ctx = self.ctx
        family_id = plannable_family(ctx)
        if not family_id or not blocked_resumable(ctx):
            return False
        try:
            self._planned.append(plan_batch(ctx, family_id))
        except (budget.BudgetBlocked, llm.LLMError, StageError) as exc:
            self._note(f"补充规划失败：{exc}")
            return False
        return True

    def _check(self, lifetime_end: float, watch: _Reload | None) -> None:
        if self._lease_lost.is_set():
            self._halt("lease_lost")
        elif is_paused(self.ctx):
            self._halt("paused")
        elif self.batch_limit and self._claimed >= self.batch_limit:
            with self._lock:
                busy = bool(self._busy)
            if not busy:
                self._finish("limit")
        elif self._queue_empty():
            if self._claimed != self._peeked_at and self._peek_work():
                # 工位睡着时来了新活：叫醒它们去领（之后一个都没领到就不再看，下次照常退出）
                self._peeked_at = self._claimed
                with self._lock:
                    self._idle_since = None
                self._nudge()
            elif not self._plan_more():
                self._finish("queue_empty")
        elif time.monotonic() >= lifetime_end:
            self._handoff("lifetime")
        elif watch is not None and watch.changed():
            self._handoff("reload")

    def _feedback(self) -> None:
        """出完的批次写反馈报告（P11）：只由持主租约的主线程跑，同时只跑一个。"""
        ctx = self.ctx
        with self._feedback_lock:
            try:
                for batch_id in finished_batches(ctx):
                    if batch_id in self._feedback_failed:
                        continue
                    if not self._primary or self._stop_reason in (
                        "paused",
                        "lease_lost",
                    ):
                        break
                    try:
                        result = run_s11(ctx, batch_id)
                    except (budget.BudgetBlocked, llm.LLMError, StageError) as exc:
                        self._feedback_failed.add(batch_id)
                        self._note(f"{batch_id} 反馈报告未生成：{exc}")
                        continue
                    with self._lock:
                        self._reports.append(result)
            except Exception as exc:  # noqa: BLE001 - 反馈出错不影响出图
                self._note(f"批次反馈出错：{type(exc).__name__}: {exc}")
            # 小变化出完的批次：拉起二次大变化策划（后台进程，单实例；不挡出图）
            try:
                from . import derive

                derive.kick(ctx)
            except Exception as exc:  # noqa: BLE001 - 派生出错不影响出图
                self._note(f"二次大变化没拉起来：{type(exc).__name__}: {exc}")

    def run(self) -> dict[str, Any]:
        ctx = self.ctx
        if not state.acquire_worker_lease(
            ctx.db, ctx.owner, lease_seconds=LEASE_SECONDS, info=self._info()
        ):
            return {"status": "busy", "worker": state.worker_status(ctx.db)}
        limits.configure(
            image=int(ctx.setting("batch.image_concurrency", 8)),
            llm=int(ctx.setting("batch.llm_concurrency", 3)),
        )
        beat = threading.Thread(
            target=self._heartbeat, name="podflow-heartbeat", daemon=True
        )
        beat.start()
        started = utcnow()
        lifetime_end = time.monotonic() + self.max_minutes * 60
        recovered: dict[str, Any] = {}
        lanes: list[threading.Thread] = []
        used: dict[str, Any] = {}
        try:
            recovered = state.recover_stale(ctx.db, ctx.owner)
            watch = _Reload(ctx) if self.reload_check else None
            lanes = [
                threading.Thread(
                    target=self._lane,
                    args=(i,),
                    name=f"podflow-lane-{i + 1}",
                    daemon=True,
                )
                for i in range(self.lanes)
            ]
            for thread in lanes:
                thread.start()
            feedback: threading.Thread | None = None
            last_feedback = -FEEDBACK_EVERY_SECONDS
            while any(thread.is_alive() for thread in lanes):
                self._changed.wait(CHECK_SECONDS)
                self._changed.clear()
                if self._stop_reason is None:
                    self._check(lifetime_end, watch)
                if (
                    self._primary
                    and self._stop_reason is None
                    and (feedback is None or not feedback.is_alive())
                    and time.monotonic() - last_feedback >= FEEDBACK_EVERY_SECONDS
                ):
                    last_feedback = time.monotonic()
                    feedback = threading.Thread(
                        target=self._feedback, name="podflow-feedback", daemon=True
                    )
                    feedback.start()
            if feedback is not None:
                feedback.join()
            if self._stop_reason is None:
                # 工位自己先看到暂停 / 丢租约停下了，主线程还没来得及记原因
                if self._lease_lost.is_set():
                    self._halt("lease_lost")
                elif is_paused(ctx):
                    self._halt("paused")
            if self._primary and self._stop_reason in (None, "queue_empty", "limit"):
                self._feedback()
        finally:
            self._stop.set()
            self._nudge()
            for thread in lanes:
                thread.join(timeout=5)
            beat.join(timeout=5)
            used = limits.snapshot()
            limits.reset()
            if self._primary:
                state.release_worker_lease(ctx.db, ctx.owner)
        jobs = list(self._jobs)
        stop_reason = self._stop_reason or "queue_empty"
        summary: dict[str, Any] = {
            "status": "done",
            "source": self.source,
            "lanes": self.lanes,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": now_iso(),
            "stop_reason": stop_reason,
            "recovered": recovered,
            "jobs": jobs,
            "planned_batches": [
                {
                    "batch_id": p["batch_id"],
                    "accepted": len(p["accepted"]),
                    "exhausted": p["exhausted"],
                }
                for p in self._planned
            ],
            "batch_reports": list(self._reports),
            "notes": list(self._notes),
            "fix_cards": list(self._fix_cards),
            # 这次只做了选款里要求的修改：修改卡片已逐张发过，不再发「出图一轮结束」
            "fix_only": bool(jobs) and all(j["job_id"] in self._fix_jobs for j in jobs),
            "limits": used,
        }
        with ctx.db.tx() as conn:
            log_event(
                conn,
                "worker",
                "worker_finished",
                {k: summary[k] for k in ("source", "stop_reason", "notes", "lanes")}
                | {"jobs": len(jobs)},
            )
            ctx.db.set_meta(conn, "last_worker_summary", dumps(summary))
        store.kick(ctx)
        if self._continued:
            # 已经交接给接班的 worker：排队的修改和方向都归它
            summary["fixes"] = {}
            summary["continued"] = self._continued
            return summary
        # 运行期间新排进来、又没被领到的人工修改（比如这个 worker 只推进指定任务）接着拉起来
        summary["fixes"] = fixes.start(ctx)
        # 人选中的裂变方向一直出到出完：还有没领到的就接一个 worker；这一次没推进任何任务就不再接
        summary["continued"] = {}
        if stop_reason not in ("paused", "lease_lost") and (
            jobs or self.source != "directions"
        ):
            if summary["fixes"].get("fix_started"):
                if directions.pending_jobs(ctx):
                    summary["continued"] = {
                        "after_fix": summary["fixes"]["fix_started"]
                    }
            else:
                summary["continued"] = directions.start_runs(ctx)
        return summary
