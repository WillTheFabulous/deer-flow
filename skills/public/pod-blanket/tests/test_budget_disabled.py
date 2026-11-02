"""预算控制关闭（budget.enforce: false）：不查上限、不要单价，但调用照样落账，结果未知照样不重付费。"""

from __future__ import annotations

import datetime as dt
import json

from conftest import SLOTS_PER_JOB, reopen, set_config
from podflow import doctor, report
from podflow.tick import tick
from podflow.util import iso, now_iso, utcnow
from podflow.worker import Worker
from test_pipeline import bootstrap


def _disable(home) -> None:
    set_config(
        home,
        "budget",
        {
            "enforce": False,
            "currency": "CNY",
            "daily_cap": 0,
            "total_cap": 0,
            "per_task_cap": 0,
        },
    )
    set_config(
        home,
        "prices/chat/gpt-6.1-sol-high",
        {"input_per_mtok": None, "output_per_mtok": None},
    )
    set_config(home, "prices/image/gpt-image-2.5-flare", {"per_image": None})


def test_pipeline_runs_without_caps_or_prices(home, fake, seed_image) -> None:
    _disable(home)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'")
        == SLOTS_PER_JOB
    )
    assert {
        r["cost_status"]
        for r in ctx.db.q("SELECT DISTINCT cost_status FROM provider_calls")
    } == {"untracked"}
    asset = ctx.db.q1("SELECT path FROM assets LIMIT 1")
    manifest = json.loads(
        (ctx.home / asset["path"])
        .with_suffix(".manifest.json")
        .read_text(encoding="utf-8")
    )
    assert (
        manifest["costs"]["cost_status"] == "untracked"
        and manifest["costs"]["total"] is None
    )
    metrics = report.batch_report(ctx, "B001")["metrics"]
    assert (
        metrics["cost_per_candidate"] is None
        and metrics["cost"]["settled_total"] is None
    )
    snap = report.snapshot(ctx)
    assert snap["notes"] and not any("预算上限为 0" in a for a in snap["alerts"])
    assert "不计费" in report.text_summary(snap)


def test_tick_and_doctor_accept_disabled_budget(home, fake, seed_image) -> None:
    _disable(home)
    ctx = reopen(home)
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "trial_started_at", now_iso())
        ctx.db.set_meta(conn, "trial_ends_at", iso(utcnow() + dt.timedelta(hours=1)))
    assert tick(ctx, spawn=False)["decision"] == "wait"
    bootstrap(ctx, seed_image, limit=1)
    assert tick(ctx, spawn=False)["decision"] == "dispatch"
    result = doctor.run(ctx)
    assert result["ok"], [
        c for c in result["checks"] if not c["ok"] and c["level"] == "error"
    ]


def test_unknown_result_still_never_resubmitted(home, fake, seed_image) -> None:
    _disable(home)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    fake.image_modes_by_model["gemini-nano-banana-2.1"] = "timeout"
    Worker(ctx, max_minutes=5).run()
    fake.image_modes_by_model.clear()
    Worker(ctx, max_minutes=5).run()
    # 带输入图的请求按「模型+images」取模式：只有文生图槽位超时
    calls = ctx.db.q(
        "SELECT status FROM provider_calls WHERE model = 'gemini-nano-banana-2.1'"
        f" AND {ctx.db.json_text('request_meta_json', 'slot_id')} = 'gemini-nano-banana-2.1#1'"
    )
    assert [c["status"] for c in calls] == ["unknown"]
