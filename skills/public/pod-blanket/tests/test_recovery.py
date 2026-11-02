"""Q15–Q18 演练：结果未知不重付费、进程中断回收、零预算、积压背压、空队列、暂停、试运行窗口、单 worker。"""

from __future__ import annotations

import datetime as dt

from conftest import (
    SLOTS_PER_JOB,
    confirm_all,
    p05_task,
    reopen,
    set_config,
    slot_changes,
)
from podflow import review, state
from podflow.db import next_id
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.tick import tick
from podflow.util import iso, loads, now_iso, utcnow
from podflow.worker import Worker
from test_pipeline import bootstrap


def _image_calls(ctx, model: str) -> list:
    """某模型文生图槽位的调用：图生图用同一个模型名（Edit 用 -edit 名），按槽位编号区分。"""
    rows = ctx.db.q(
        "SELECT * FROM provider_calls WHERE kind = 'image' AND model = ? ORDER BY call_id",
        (model,),
    )
    return [
        r
        for r in rows
        if not (loads(r["request_meta_json"], {}) or {})
        .get("slot_id", "")
        .endswith("@i2i")
    ]


def test_timeout_becomes_unknown_and_is_never_resubmitted(
    ctx, fake, seed_image
) -> None:
    bootstrap(ctx, seed_image, limit=1)
    fake.image_modes_by_model["gpt-image-2.5-sunburst"] = "timeout"
    Worker(ctx, max_minutes=5).run()
    calls = _image_calls(ctx, "gpt-image-2.5-sunburst")
    assert (
        len(calls) == 1
        and calls[0]["status"] == "unknown"
        and calls[0]["cost_status"] == "unknown"
    )
    slot = ctx.db.q1(
        "SELECT * FROM job_slots WHERE slot_id = 'gpt-image-2.5-sunburst#1'"
    )
    assert slot["status"] == "unknown"
    rv = ctx.db.q1(
        "SELECT * FROM reviews WHERE subject_type = 'call_reconcile' AND status = 'open'"
    )
    assert rv and rv["subject_id"] == calls[0]["call_id"]
    # 再跑一轮：不会对同一槽位再次付费提交
    fake.image_modes_by_model.clear()
    Worker(ctx, max_minutes=5).run()
    assert len(_image_calls(ctx, "gpt-image-2.5-sunburst")) == 1
    # 人工对账「未扣费」后才允许重新提交，且只补这一张
    review.decide(ctx, rv["review_id"], "not_charged", by="tester")
    Worker(ctx, max_minutes=5).run()
    calls = _image_calls(ctx, "gpt-image-2.5-sunburst")
    assert [c["status"] for c in calls] == ["failed", "succeeded"]
    assert calls[0]["cost_status"] == "released"
    # 3 个模型 × 文生图 / 图生图 / 图生图 Edit
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'")
        == SLOTS_PER_JOB
    )


def test_transient_errors_retry_with_limit(ctx, fake, seed_image) -> None:
    set_config(ctx.home, "batch.transient_retry_limit", 2)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.image_modes_by_model["gpt-image-2.5-flare"] = "503"
    Worker(ctx, max_minutes=5).run()
    calls = _image_calls(ctx, "gpt-image-2.5-flare")
    assert len(calls) == 3 and {c["cost_status"] for c in calls} == {"released"}
    slot = ctx.db.q1("SELECT * FROM job_slots WHERE slot_id = 'gpt-image-2.5-flare#1'")
    assert slot["status"] == "failed"
    # 这一路如实留缺，其余 8 路（含 flare 的图生图和 Edit）照常归档
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'")
        == SLOTS_PER_JOB - 1
    )


def test_permanent_error_is_not_retried(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    fake.image_modes_by_model["gemini-nano-banana-2.1"] = "400"
    Worker(ctx, max_minutes=5).run()
    assert len(_image_calls(ctx, "gemini-nano-banana-2.1")) == 1


def test_crash_recovery_marks_submitted_calls_unknown(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    with ctx.db.tx() as conn:
        call_id = next_id(conn, "call")
        conn.execute(
            "INSERT INTO provider_calls(call_id, kind, stage, scope_id, model, idempotency_key, request_meta_json, status,"
            " owner, day, currency, reserved_cost, cost_status, created_at) VALUES(?, 'image', 'T01', 'J0001',"
            " 'gpt-image-2.5-flare', 'crash-key', '{\"slot_id\": \"gpt-image-2.5-flare#1\"}', 'submitted', 'dead-worker',"
            " '2026-10-08', 'CNY', 0.5, 'reserved', ?)",
            (call_id, now_iso()),
        )
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = 'dead-worker' WHERE job_id = 'J0001'"
        )
    recovered = state.recover_stale(ctx.db, "new-worker")
    assert recovered["jobs"] == ["J0001"] and recovered["image_calls_unknown"] == [
        call_id
    ]
    slot = ctx.db.q1(
        "SELECT status FROM job_slots WHERE slot_id = 'gpt-image-2.5-flare#1'"
    )
    assert slot["status"] == "unknown"
    Worker(ctx, max_minutes=5).run()
    # 中断那一路不会被重新提交；其余 8 路照常出图
    assert len(_image_calls(ctx, "gpt-image-2.5-flare")) == 1
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'")
        == SLOTS_PER_JOB - 1
    )


def test_single_worker_lease(ctx) -> None:
    assert state.acquire_worker_lease(ctx.db, "A", lease_seconds=60)
    assert not state.acquire_worker_lease(ctx.db, "B", lease_seconds=60)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE worker_lease SET lease_until = ?",
            (iso(utcnow() - dt.timedelta(seconds=1)),),
        )
    assert state.acquire_worker_lease(ctx.db, "B", lease_seconds=60)
    assert Worker(ctx).run()["status"] == "busy"


def test_zero_budget_blocks_without_calls_then_resumes(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    calls_before = ctx.db.scalar("SELECT COUNT(*) FROM provider_calls")
    set_config(ctx.home, "budget.daily_cap", 0)
    zero = reopen(ctx.home)
    summary = Worker(zero, max_minutes=5).run()
    assert (
        summary["jobs"][0]["status"] == "blocked"
        and summary["jobs"][0]["blocked"] == "budget_zero"
    )
    assert zero.db.scalar("SELECT COUNT(*) FROM provider_calls") == calls_before
    with zero.db.tx() as conn:
        zero.db.set_meta(conn, "trial_started_at", now_iso())
        zero.db.set_meta(conn, "trial_ends_at", iso(utcnow() + dt.timedelta(hours=1)))
    assert tick(zero, spawn=False)["decision"] == "stop"
    set_config(ctx.home, "budget.daily_cap", 100)
    back = reopen(ctx.home)
    assert tick(back, spawn=False)["decision"] == "dispatch"
    summary = Worker(back, max_minutes=5).run()
    assert summary["jobs"][0]["status"] == "complete"


def test_review_backlog_stops_new_generation(ctx, fake, seed_image) -> None:
    set_config(ctx.home, "batch.review_backlog_limit", 0)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    summary = Worker(ctx, max_minutes=5).run()
    assert summary["jobs"][0]["blocked"] == "review_backlog"
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM provider_calls WHERE kind = 'image'") == 0
    )


def test_tick_decisions(ctx, fake, seed_image) -> None:
    assert tick(ctx, spawn=False)["decision"] == "stop"  # 试运行窗口未开
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "trial_started_at", now_iso())
        ctx.db.set_meta(conn, "trial_ends_at", iso(utcnow() + dt.timedelta(hours=24)))
    assert tick(ctx, spawn=False)["decision"] == "wait"  # 空队列不等于失败
    seed = seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, seed["seed_id"])
    seedmod.decide_family(ctx, "F01", approve=True, by="tester")
    confirm_all(ctx, planmod.plan_batch(ctx, "F01", limit=1))
    assert tick(ctx, spawn=False)["decision"] == "dispatch"
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "1")
    assert tick(ctx, spawn=False)["decision"] == "stop"
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "0")
    state.acquire_worker_lease(ctx.db, "other", lease_seconds=60)
    assert tick(ctx, spawn=False)["decision"] == "wait"


def test_exhausted_family_asks_for_new_direction(ctx, fake, seed_image) -> None:
    seed = seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, seed["seed_id"])
    seedmod.decide_family(ctx, "F01", approve=True, by="tester")
    confirm_all(ctx, planmod.plan_batch(ctx, "F01"))
    # 第二轮每个槽位都只给出做过的方向
    fake.p05_tasks = lambda group: [
        p05_task(s["slot_id"], slot_changes(s, "方案1")) for s in group["slots"]
    ]
    second = planmod.plan_batch(ctx, "F01")
    assert second["exhausted"] is True and second["accepted"] == []
    assert len(second["coverage_gaps"]) == 3
    fam = ctx.db.q1("SELECT status FROM families WHERE family_id = 'F01'")
    assert fam["status"] == "exhausted"
    assert ctx.db.q1(
        "SELECT 1 FROM reviews WHERE subject_type = 'family_exhausted' AND status = 'open'"
    )
