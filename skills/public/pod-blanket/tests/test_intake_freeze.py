"""参考图接入确认冻结（intake.review: false）：看不清、模型拿不准都不停下，直接拆解、策划；授权受限照旧停。"""

from __future__ import annotations

import json

import pytest

from conftest import default_result, envelope, reopen, set_config
from podflow import doctor, review, tasks
from podflow.stages import seed as seedmod
from podflow.stages.common import INTAKE_OFF_NOTE, get_seed
from test_ui import run


def _frozen(home):
    set_config(home, "intake.review", False)
    return reopen(home)


def _p01(fake, *, status: str = "needs_review", readability: str = "limited") -> None:
    """P01 拿不准：要人确认授权、补平铺原图（S03 线上就是这样停住的）。"""

    def respond(payload: dict) -> str:
        result = {**default_result("P01", payload, fake), "readability": readability}
        return json.dumps(envelope("P01", result, status=status), ensure_ascii=False)

    fake.overrides["P01"] = respond


def _intake_reviews(ctx, status: str = "open") -> list[dict]:
    return review.list_reviews(ctx, status=status, types=["seed_intake"])


@pytest.mark.parametrize(
    ("status", "readability"),
    [("needs_review", "limited"), ("complete", "unusable"), ("blocked", "usable")],
)
def test_frozen_does_not_stop_at_intake(
    home, fake, seed_image, status, readability
) -> None:
    ctx = _frozen(home)
    seedmod.add_seed(ctx, seed_image)
    _p01(fake, status=status, readability=readability)
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]] == ["S01", "S02", "S03", "S04"]
    skipped = out["steps"][0]["intake_review"]
    assert skipped["skipped"] is True and skipped["reason"] == INTAKE_OFF_NOTE
    assert f"参考图可读性 {readability}" in skipped["would_review"]
    assert get_seed(ctx, "S01")["status"] == "family_pending"
    assert _intake_reviews(ctx) == []
    record = json.loads(get_seed(ctx, "S01")["seed_record_json"])
    assert record["intake_review"] == skipped
    assert ctx.db.scalar(
        "SELECT COUNT(*) FROM events WHERE event = 'intake_review_skipped'"
    )


def test_restricted_still_stops(home, fake, seed_image) -> None:
    ctx = _frozen(home)
    seedmod.add_seed(ctx, seed_image, rights="restricted")
    out = seedmod.analyze(ctx, "S01")
    assert out["stopped_at"] == "S01" and out["status"] == "blocked"
    (item,) = _intake_reviews(ctx)
    assert item["subject_id"] == "S01" and "素材受限" in item["reason"]


def test_review_on_keeps_the_old_gate(home, fake, seed_image) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    _p01(fake)
    out = seedmod.analyze(ctx, "S01")
    assert out["stopped_at"] == "S01" and out["status"] == "needs_review"
    assert "intake_review" not in out["steps"][0]
    (item,) = _intake_reviews(ctx)
    assert item["reason"].startswith("参考图可读性 limited")
    assert tasks.summaries(ctx)[0]["status"] == "intake"


def test_release_a_seed_stuck_before_freezing(home, fake, seed_image, capsys) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    _p01(fake)
    assert seedmod.analyze(ctx, "S01")["stopped_at"] == "S01"
    (item,) = _intake_reviews(ctx)

    # 冻结后再跑拆解：先放行（关掉接入待审、状态改回 ingested），S01 不重跑，接着拆完并自动策划第一轮
    _frozen(home)
    out = run(capsys, "seed", "analyze", "S01")
    assert out["intake_released"] == {"reviews": 1, "note": INTAKE_OFF_NOTE}
    assert [s["stage"] for s in out["steps"]] == ["S02", "S03", "S04"]
    assert fake.stage_counts["P01"] == 1
    assert out["plan"]["batch_id"] and out["plan"]["accepted"]
    ctx = reopen(home)
    assert _intake_reviews(ctx) == []
    closed = ctx.db.q1(
        "SELECT status, decided_by, note FROM reviews WHERE review_id = ?",
        (item["review_id"],),
    )
    assert (closed["status"], closed["decided_by"], closed["note"]) == (
        "dismissed",
        "system",
        INTAKE_OFF_NOTE,
    )
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'intake_released'")
    assert tasks.summaries(ctx)[0]["status"] == "picking"
    # 已经放行过：再跑不再放行
    again = seedmod.analyze(ctx, "S01")
    assert "intake_released" not in again


def test_doctor_warns_when_frozen(home, fake) -> None:
    name = "参考图接入确认开着（intake.review）"
    check = {c["check"]: c for c in doctor.run(reopen(home))["checks"]}[name]
    assert check["ok"] is True
    check = {c["check"]: c for c in doctor.run(_frozen(home))["checks"]}[name]
    assert check["ok"] is False and check["level"] == "warn"
    assert "intake.review" in check["detail"]
