"""多工位出图 worker：名额上限、慢的那张图不挡别的方向、运行中接新确认的方向、修改优先与按任务轮流、
没活等一会儿再退、跑满时长交接、回收不误伤活着的 worker 和拆解进程、丢租约就停、批次反馈只跑一次、
程序出错把任务放回队列、等名额和发请求时放下数据库连接、配置变化稳定后才交接、工作台显示工位。"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from conftest import confirm_all, reopen, set_config
from podflow import bg, budget, http, limits, notify, state, ui
from podflow import worker as workermod
from podflow.stages import plan as planmod
from podflow.util import iso, utcnow
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_qc_rules import one_model


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """拦下后台进程：只记录启动参数，pid 用本进程（视为存活）。"""
    calls: list[dict] = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


class Gauge:
    """包一层假上游：统计同时在途的生图 / LLM 请求数，可以让请求慢一点（第一张图可以单独很慢）。"""

    def __init__(
        self,
        fake,
        monkeypatch: pytest.MonkeyPatch,
        *,
        image_delay: float = 0.0,
        llm_delay: float = 0.0,
        slow_first_image: float = 0.0,
    ) -> None:
        self.fake = fake
        self.lock = threading.Lock()
        self.now = {"image": 0, "llm": 0}
        self.peak = {"image": 0, "llm": 0}
        self.image_delay = image_delay
        self.llm_delay = llm_delay
        self.slow_first_image = slow_first_image
        self.images = 0
        monkeypatch.setattr(
            http,
            "make_client",
            lambda timeout: httpx.Client(
                transport=httpx.MockTransport(self.handler), timeout=timeout
            ),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/images/generations":
            kind = "image"
        elif path == "/v1/chat/completions":
            kind = "llm"
        else:
            return self.fake.handler(request)
        first = False
        with self.lock:
            self.now[kind] += 1
            self.peak[kind] = max(self.peak[kind], self.now[kind])
            if kind == "image":
                self.images += 1
                first = self.images == 1
        try:
            if kind == "llm":
                delay = self.llm_delay
            elif first and self.slow_first_image:
                delay = self.slow_first_image
            else:
                delay = self.image_delay
            time.sleep(delay)
            return self.fake.handler(request)
        finally:
            with self.lock:
                self.now[kind] -= 1


def _wait(check, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("等了太久")


def _statuses(ctx) -> dict[str, str]:
    return dict(ctx.db.q("SELECT job_id, status FROM jobs ORDER BY job_id"))


def test_lanes_share_image_and_llm_quotas(ctx, fake, seed_image, monkeypatch) -> None:
    """三条工位同时推进，生图同时在途不超过 image_concurrency、LLM 不超过 llm_concurrency。"""
    one_model(ctx.home, n=2)
    set_config(ctx.home, "batch.workers", 3)
    set_config(ctx.home, "batch.image_concurrency", 2)
    set_config(ctx.home, "batch.llm_concurrency", 1)
    set_config(ctx.home, "batch.gen_concurrency", 2)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=4)
    gauge = Gauge(fake, monkeypatch, image_delay=0.15, llm_delay=0.05)
    out = Worker(ctx, max_minutes=5).run()
    assert out["lanes"] == 3 and out["stop_reason"] == "queue_empty"
    assert len(out["jobs"]) >= 3
    assert sorted(j["job_id"] for j in out["jobs"]) == sorted(_statuses(ctx))
    assert set(_statuses(ctx).values()) == {"complete"}
    assert gauge.peak["image"] == 2 and gauge.peak["llm"] == 1
    assert out["limits"]["image"] == {"busy": 0, "size": 2, "peak": 2}
    # 跑完名额就撤了：别的进程（拆解、策划）不受限
    assert not limits.active()


def test_slow_image_does_not_hold_other_directions(
    ctx, fake, seed_image, monkeypatch
) -> None:
    """一张图卡住（线上见过 13 分钟）：只占它那一条工位，别的方向照常出完。"""
    one_model(ctx.home)
    set_config(ctx.home, "batch.workers", 2)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=3)
    Gauge(fake, monkeypatch, slow_first_image=1.0)
    out = Worker(ctx, max_minutes=5).run()
    slow = ctx.db.scalar(
        "SELECT scope_id FROM provider_calls WHERE kind = 'image'"
        " ORDER BY latency_ms DESC LIMIT 1"
    )
    order = [j["job_id"] for j in out["jobs"]]
    assert len(order) == 3 and order[-1] == slow


def test_running_worker_picks_up_newly_confirmed_directions(
    ctx, fake, seed_image, spawned
) -> None:
    """worker 还在跑时新确认的方向：只排队、不另起进程，由它接着领。"""
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    family_id = bootstrap(ctx, seed_image, limit=1)["family_id"]
    result: dict = {}

    def run() -> None:
        result.update(
            Worker(
                reopen(ctx.home), source="directions", idle_seconds=5, max_minutes=5
            ).run()
        )

    runner = threading.Thread(target=run)
    runner.start()
    _wait(lambda: _statuses(ctx).get("J0001") == "complete")
    plan = planmod.plan_batch(ctx, family_id, limit=1)
    confirmed = confirm_all(ctx, plan, start=True)
    assert confirmed["directions_queued"] and "directions_started" not in confirmed
    runner.join(timeout=60)
    assert not runner.is_alive()
    assert [j["job_id"] for j in result["jobs"]] == ["J0001", "J0002"]
    assert result["stop_reason"] == "queue_empty"
    assert not [c for c in spawned if c["argv"][2] == "worker"]


def _second_task(ctx, jobs: tuple[str, ...]) -> None:
    """把几个方向挪到另一个任务（系列）名下。"""
    row = dict(ctx.db.q1("SELECT * FROM batches WHERE batch_id = 'B001'"))
    row.update(batch_id="B900", family_id="F900")
    cols = list(row)
    marks = ",".join("?" for _ in jobs)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO batches({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
        conn.execute(
            f"UPDATE jobs SET batch_id = 'B900' WHERE job_id IN ({marks})", jobs
        )


def _release_all(ctx) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'ready', lease_owner = NULL, lease_until = NULL"
        )


def test_fair_order_puts_fixes_first_and_alternates_tasks(
    ctx, fake, seed_image
) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=3)
    assert sorted(_statuses(ctx)) == ["J0001", "J0002", "J0003"]
    _second_task(ctx, ("J0003",))

    def claim(n: int, order: str) -> list[str]:
        return [
            state.claim_job(ctx.db, "w", lease_seconds=60, order=order)["job_id"]
            for _ in range(n)
        ]

    assert claim(2, "fifo") == ["J0001", "J0002"]
    _release_all(ctx)
    # 按任务轮流：当前在出最少的任务先领，一样多时先来先到
    assert claim(3, "fair") == ["J0001", "J0003", "J0002"]
    _release_all(ctx)
    # 选款里要求修改的任务最先领（有人在等），哪怕它最晚建
    variant = ctx.db.scalar("SELECT variant_id FROM jobs WHERE job_id = 'J0003'")
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO assets(asset_id, job_id, variant_id, slot_id, model, path, sha256, status,"
            " human_decision, created_at, updated_at) VALUES('IMG90001', 'J0003', ?, 's#1', 'm', 'x.png',"
            " 'sha-fix', 'repair_pending', 'needs_changes', ?, ?)",
            (variant, iso(utcnow()), iso(utcnow())),
        )
    assert claim(2, "fair") == ["J0003", "J0001"]
    # 给了 job_ids 就只在里面领；空列表 = 没有可领的
    _release_all(ctx)
    assert state.claim_job(ctx.db, "w", lease_seconds=60, job_ids=[]) is None
    assert (
        state.claim_job(ctx.db, "w", lease_seconds=60, job_ids=["J0002"], order="fair")[
            "job_id"
        ]
        == "J0002"
    )


def test_idle_wait_only_for_background_workers(ctx, fake, monkeypatch) -> None:
    monkeypatch.setattr(bg, "_current", None)
    start = time.monotonic()
    out = Worker(ctx, source="directions", idle_seconds=0.6, max_minutes=5).run()
    assert out["stop_reason"] == "queue_empty" and out["jobs"] == []
    assert time.monotonic() - start >= 0.6
    # 前台跑的没活就退；后台 worker 按 batch.worker_idle_seconds 等新活
    set_config(ctx.home, "batch.worker_idle_seconds", 45)
    ctx = reopen(ctx.home)
    assert Worker(ctx).idle_seconds == 0
    monkeypatch.setattr(bg, "_current", "BG9999")
    assert Worker(ctx).idle_seconds == 45


def test_lifetime_handoff_spawns_successor_and_releases_jobs(
    ctx, fake, seed_image, monkeypatch, spawned
) -> None:
    """跑满时长：先交出主租约、拉起接班的，手上的任务在下一步前放回队列，不发卡片。"""
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=2)
    Gauge(fake, monkeypatch, llm_delay=0.5)
    # 拉起接班的那一刻主租约已经交出：接班的马上拿得到，不用等这一个做完手上的步骤
    leases: list = []
    stub = bg.subprocess.Popen

    def popen(argv, **kwargs):
        leases.append(state.worker_status(reopen(ctx.home).db))
        return stub(argv, **kwargs)

    monkeypatch.setattr(bg.subprocess, "Popen", popen)
    out = Worker(ctx, source="directions", max_minutes=0.0001).run()
    assert leases == [None]
    assert out["stop_reason"] == "lifetime"
    assert out["continued"]["handoff"].startswith("BG")
    assert "J0002" in out["continued"]["jobs"]
    assert [c["argv"][2:] for c in spawned] == [["worker", "--source", "directions"]]
    # 手上的方向（S06 前或 S06 后）放回队列，没领到的还排着
    assert len(out["jobs"]) <= 1
    assert all(
        j["status"] == "ready" and j["stopped"] == "lifetime" for j in out["jobs"]
    )
    assert set(_statuses(ctx).values()) == {"ready"}
    assert state.worker_status(ctx.db) is None
    summary = bg.summarize("worker", out)
    assert summary["continued"] is True
    assert (
        notify.bg_card(ctx, {"kind": "worker", "subject": None}, summary, ok=True)
        is None
    )


def _call(ctx, scope: str, owner: str, key: str) -> str:
    reservation = budget.reserve(
        ctx.db,
        ctx.cfg,
        kind="llm",
        stage="P06",
        scope_id=scope,
        model="gpt-6.1-sol-high",
        idempotency_key=key,
        estimated_cost=0.01,
        owner=owner,
    )
    budget.mark_submitted(ctx.db, reservation.call_id)
    return reservation.call_id


def test_recover_stale_leaves_live_workers_and_other_processes_alone(
    ctx, fake, seed_image
) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=2)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = 'peer', lease_until = ? WHERE job_id = 'J0001'",
            (iso(utcnow() + dt.timedelta(minutes=2)),),
        )
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = 'dead', lease_until = ? WHERE job_id = 'J0002'",
            (iso(utcnow() - dt.timedelta(minutes=2)),),
        )
    live = _call(ctx, "J0001", "peer", "k-live")
    dead = _call(ctx, "J0002", "dead", "k-dead")
    analyzing = _call(ctx, "seed:S01", "analyze-proc", "k-analyze")
    old = _call(ctx, "seed:S01", "analyze-gone", "k-old")
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE provider_calls SET created_at = ? WHERE call_id = ?",
            (iso(utcnow() - dt.timedelta(hours=2)), old),
        )
    out = state.recover_stale(ctx.db, "me")
    assert out["jobs"] == ["J0002"]
    assert sorted(out["llm_calls_unknown"]) == sorted([dead, old])
    calls = dict(ctx.db.q("SELECT call_id, status FROM provider_calls"))
    assert calls[live] == "submitted" and calls[analyzing] == "submitted"
    assert _statuses(ctx) == {"J0001": "running", "J0002": "ready"}


@pytest.mark.parametrize("mode", ["taken", "db_down"])
def test_lost_lease_stops_claiming(ctx, fake, seed_image, monkeypatch, mode) -> None:
    """主租约被别人拿走，或心跳连续失败太久：工位在下一步开始前放下任务，不再领新的。"""
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=3)
    monkeypatch.setattr(workermod, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(workermod, "LEASE_LOST_SECONDS", 0.1)

    def heartbeat(*args, **kwargs):
        if mode == "taken":
            return False
        raise RuntimeError("库连不上")

    monkeypatch.setattr(state, "heartbeat_worker", heartbeat)
    Gauge(fake, monkeypatch, llm_delay=0.4)
    out = Worker(ctx, max_minutes=5).run()
    assert out["stop_reason"] == "lease_lost"
    assert [j["job_id"] for j in out["jobs"]] == ["J0001"]
    assert out["jobs"][0]["status"] == "ready"
    assert out["jobs"][0]["stopped"] == "lease_lost"
    assert out["continued"] == {}
    assert set(_statuses(ctx).values()) == {"ready"}


def test_batch_feedback_runs_once_with_several_lanes(ctx, fake, seed_image) -> None:
    one_model(ctx.home)
    set_config(ctx.home, "batch.workers", 3)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=3)
    out = Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P11"] == 1
    assert [r["batch_id"] for r in out["batch_reports"]] == ["B001"]


def test_unexpected_error_puts_the_job_back(ctx, fake, seed_image, monkeypatch) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    real = workermod.advance_job
    calls: list[str] = []

    def flaky(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(workermod, "advance_job", flaky)
    out = Worker(ctx, max_minutes=5).run()
    assert [j["status"] for j in out["jobs"]] == ["ready", "complete"]
    assert any("boom" in n for n in out["notes"])


def test_slots_release_the_db_connection(ctx) -> None:
    ctx.db.conn()
    with limits.image_slot(ctx):
        # 不在 worker 里：不限流，也不动连接
        assert ctx.db._local.conn is not None
    limits.configure(image=1, llm=1)
    try:
        ctx.db.conn()
        with limits.image_slot(ctx):
            assert getattr(ctx.db._local, "conn", None) is None
            ctx.db.conn()
            limits.release_db(ctx)
            assert ctx.db._local.conn is None
        with limits.llm_slot(ctx):
            pass
        snap = limits.snapshot()
        assert snap["image"] == {"busy": 0, "size": 1, "peak": 1}
        assert snap["llm"]["peak"] == 1
    finally:
        limits.reset()


def test_kicks_from_lanes_are_throttled(ctx, monkeypatch) -> None:
    assert limits.recently_kicked("storage") is False
    limits.mark_kicked("storage")
    # 不在 worker 里不节流
    assert limits.recently_kicked("storage") is False
    limits.configure(image=1, llm=1)
    try:
        limits.mark_kicked("storage")
        assert limits.recently_kicked("storage") is True
        assert limits.recently_kicked("table") is False
    finally:
        limits.reset()
    assert limits.recently_kicked("storage") is False


def test_reload_watch_waits_until_files_settle(ctx, monkeypatch) -> None:
    monkeypatch.setattr(workermod, "RELOAD_CHECK_SECONDS", 0)
    monkeypatch.setattr(workermod, "RELOAD_STABLE_SECONDS", 0.2)
    watch = workermod._Reload(ctx)
    assert watch.changed() is False
    path = ctx.home / "podflow.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# 改过\n", encoding="utf-8")
    # 刚变：等它稳定（可能还在拷文件）
    assert watch.changed() is False
    time.sleep(0.25)
    assert watch.changed() is True


def test_dashboard_line_shows_lanes(ctx) -> None:
    line = ui.worker_line(
        ctx,
        {
            "alive": True,
            "lease_until": iso(utcnow()),
            "info": {
                "lanes": 8,
                "busy": [{"job_id": "J0001"}, {"job_id": "J0002"}],
                "limits": {"image": {"busy": 5, "size": 8, "peak": 8}},
            },
        },
    )
    assert "8 条工位，在出 2 个方向" in line and "生图 5/8" in line
    assert ui.worker_line(ctx, None) == "**后台出图**：空闲"


def test_fix_only_and_handoff_summaries_send_no_round_card(ctx) -> None:
    task = {"kind": "fix", "subject": "IMG00001"}
    fix_only = bg.summarize(
        "fix",
        {
            "status": "done",
            "jobs": [{"job_id": "J0001", "status": "complete"}],
            "fix_cards": ["IMG00001"],
            "fix_only": True,
            "stop_reason": "queue_empty",
        },
    )
    assert notify.bg_card(ctx, task, fix_only, ok=True) is None
    assert notify.bg_finished(ctx, task, fix_only, ok=True)["reason"] == (
        "修改的结果已逐张发过卡片"
    )
    handoff = bg.summarize(
        "worker", {"status": "done", "jobs": [], "continued": {"handoff": "BG0001"}}
    )
    assert handoff["continued"] is True
