"""质检的程序规则：关键项失败不能通过、修复上限；相近的候选不再判重复，一并进选款；质检可以整体暂停。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import reopen, set_config
from podflow import doctor, maintenance, review
from podflow.stages import job as jobmod
from podflow.stages.common import StageError, get_job
from podflow.util import loads
from podflow.worker import Worker
from test_pipeline import bootstrap


def one_model(home: Path, n: int = 1) -> None:
    """只留 flare 一个模型、只做文生图：质检规则与出图方式无关，槽位少断言才直观。"""
    set_config(
        home,
        "image_generation.fanout",
        [{"model": "gpt-image-2.5-flare", "n": n, "enabled": True}],
    )
    set_config(home, "image_generation.modes", ["t2i"])


def test_failed_check_overrides_pass_and_repairs_with_input_image(
    ctx, fake, seed_image
) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["pass_with_fail", "pass"]
    Worker(ctx, max_minutes=5).run()
    assets = ctx.db.q("SELECT * FROM assets ORDER BY asset_id")
    assert [a["status"] for a in assets] == ["superseded", "archived"]
    parent, child = assets
    qc = loads(parent["qc_json"])
    assert (
        qc["model_decision"] == "pass" and qc["decision"] == "repair" and qc["override"]
    )
    assert (
        child["parent_asset_id"] == parent["asset_id"]
        and child["repair_depth"] == 1
        and child["asset_version"] == 2
    )
    # 修复也用原模型名，只是带上候选图作为输入图
    edit = [p for p in fake.image_payloads() if "images" in p["vendor_params"]]
    assert len(edit) == 1 and len(edit[0]["vendor_params"]["images"]) == 1
    assert edit[0]["model"] == "gpt-image-2.5-flare"
    # 首检通过率按第一次质检计：这张第一次没过
    report = ctx.db.q1("SELECT report_json FROM batches WHERE batch_id = 'B001'")
    assert loads(report["report_json"])["metrics"]["first_pass_qc_rate"] == 0.0


def test_repair_limit_rejects(ctx, fake, seed_image) -> None:
    one_model(ctx.home)
    set_config(ctx.home, "batch.repair_limit", 1)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["repair", "repair"]
    Worker(ctx, max_minutes=5).run()
    statuses = [
        a["status"] for a in ctx.db.q("SELECT status FROM assets ORDER BY asset_id")
    ]
    assert statuses == ["superseded", "rejected"]


def test_qc_doubt_goes_to_selection_flagged(ctx, fake, seed_image) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["needs_review"]
    Worker(ctx, max_minutes=5).run()
    assert ctx.db.scalar("SELECT status FROM assets") == "archived"
    assert not review.list_reviews(ctx, types=["asset_qc"])
    item = review.list_reviews(ctx, types=["asset_selection"])[0]
    assert item["payload"]["qc_flags"] and not item["payload"]["qc_unchecked"]


def test_qc_call_failure_goes_to_selection_unchecked(ctx, fake, seed_image) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.overrides["P07"] = lambda payload: "这不是 JSON"
    Worker(ctx, max_minutes=5).run()
    assert ctx.db.scalar("SELECT status FROM assets") == "archived"
    qc = loads(ctx.db.scalar("SELECT qc_json FROM assets"))
    assert qc["decision"] == "unchecked" and qc["flagged"] is True
    item = review.list_reviews(ctx, types=["asset_selection"])[0]
    assert item["payload"]["qc_unchecked"].startswith("质检调用失败")
    assert not review.list_reviews(ctx, types=["asset_qc"])


def test_qc_switched_off_sends_images_to_selection_unchecked(
    ctx, fake, seed_image
) -> None:
    one_model(ctx.home)
    set_config(ctx.home, "qc.enabled", False)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P07"] == 0
    assert ctx.db.scalar("SELECT status FROM assets") == "archived"
    qc = loads(ctx.db.scalar("SELECT qc_json FROM assets"))
    assert qc["decision"] == "unchecked" and qc["flagged"] is True
    assert qc["reason"].startswith("质检已暂停") and qc["file_checks"]["exists"]
    item = review.list_reviews(ctx, types=["asset_selection"])[0]
    assert item["payload"]["qc_unchecked"].startswith("质检已暂停")
    check = {c["check"]: c for c in doctor.run(ctx)["checks"]}["质检开着（qc.enabled）"]
    assert check["ok"] is False and check["level"] == "warn"


def test_pending_auto_repairs_are_skipped_when_qc_is_turned_off(
    ctx, fake, seed_image
) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    job_id = bootstrap(ctx, seed_image, limit=1)["confirmed"]["jobs"][0]
    fake.qc_queue = ["repair"]
    for step in ("S06", "T01", "S07"):
        jobmod.STEPS[step](ctx, get_job(ctx, job_id))
    assert ctx.db.scalar("SELECT status FROM assets") == "repair_pending"
    # 质检暂停后：之前判了修复的图不再修，原样进选款
    set_config(ctx.home, "qc.enabled", False)
    ctx = reopen(ctx.home)
    Worker(ctx, max_minutes=5).run()
    assets = ctx.db.q("SELECT status, qc_json FROM assets")
    assert [a["status"] for a in assets] == ["archived"]
    qc = loads(assets[0]["qc_json"])
    assert "之前判了修复" in qc["reason"] and qc["previous"]["decision"] == "repair"
    assert not [p for p in fake.image_payloads() if "images" in p["vendor_params"]]
    assert fake.stage_counts["P07"] == 1


def _one_rejected(ctx, fake, seed_image):
    one_model(ctx.home)
    set_config(ctx.home, "batch.repair_limit", 1)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["repair", "repair"]
    Worker(ctx, max_minutes=5).run()
    rejected = ctx.db.scalar("SELECT asset_id FROM assets WHERE status = 'rejected'")
    return ctx, rejected


def test_rejected_images_can_be_put_back_into_selection(ctx, fake, seed_image) -> None:
    ctx, rejected = _one_rejected(ctx, fake, seed_image)
    dry = maintenance.restore_rejected(ctx, by="tester", dry_run=True)
    assert dry["assets"] == [rejected] and dry["batches"] == ["B001"]
    assert (
        ctx.db.scalar("SELECT status FROM assets WHERE asset_id = ?", (rejected,))
        == "rejected"
    )
    assert (
        maintenance.restore_rejected(ctx, batches=["B999"], by="t", dry_run=True)[
            "assets"
        ]
        == []
    )

    out = maintenance.restore_rejected(ctx, batches=["B001"], by="tester")
    assert out["processed_jobs"] == ["J0001"] and out["left_to_worker"] == []
    asset = ctx.db.q1(
        "SELECT status, qc_json FROM assets WHERE asset_id = ?", (rejected,)
    )
    assert asset["status"] == "archived"
    qc = loads(asset["qc_json"])
    assert qc["decision"] == "unchecked" and qc["flagged"] is True
    assert qc["reason"].startswith("质检暂停后放回选款（原判淘汰：修复次数已用尽")
    assert qc["previous"]["decision"] == "reject"
    item = next(
        i
        for i in review.list_reviews(ctx, types=["asset_selection"])
        if i["subject_id"] == rejected
    )
    assert item["payload"]["qc_unchecked"].startswith("质检暂停后放回选款")
    # 再跑一次什么都不改
    assert maintenance.restore_rejected(ctx, by="tester")["assets"] == []


def test_restore_leaves_unfinished_jobs_to_the_worker(ctx, fake, seed_image) -> None:
    ctx, rejected = _one_rejected(ctx, fake, seed_image)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'ready' WHERE job_id = 'J0001'")
    out = maintenance.restore_rejected(ctx, by="tester")
    assert out["processed_jobs"] == [] and out["left_to_worker"] == ["J0001"]
    assert (
        ctx.db.scalar("SELECT status FROM assets WHERE asset_id = ?", (rejected,))
        == "qc_pass"
    )


def test_old_candidates_can_be_cleared_out_of_selection(ctx, fake, seed_image) -> None:
    """旧候选清出选款：待审关掉、图和记录留着、不算人工没选；还在出图的批次不让清。"""
    one_model(ctx.home, n=2)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    items = review.list_reviews(ctx, types=["asset_selection"])
    assert len(items) == 2
    with pytest.raises(StageError, match="要写清楚"):
        maintenance.dismiss_selection(ctx, batches=[], by="tester")
    with pytest.raises(StageError, match="没有批次"):
        maintenance.dismiss_selection(ctx, batches=["B999"], by="tester")
    dry = maintenance.dismiss_selection(
        ctx, batches=["B001"], by="tester", dry_run=True
    )
    assert dry["dry_run"] and len(dry["reviews"]) == 2
    assert len(review.list_reviews(ctx, types=["asset_selection"])) == 2

    out = maintenance.dismiss_selection(ctx, batches=["B001"], by="tester")
    assert sorted(out["assets"]) == sorted(i["subject_id"] for i in items)
    assert review.list_reviews(ctx, types=["asset_selection"]) == []
    assert jobmod.backlog_count(ctx) == 0
    # 图和记录留着，不记成人工「不要」（人工决定仍是没决定过）
    assets = ctx.db.q("SELECT status, human_decision FROM assets")
    assert {(a["status"], a["human_decision"]) for a in assets} == {
        ("archived", "pending")
    }
    rows = ctx.db.q(
        "SELECT status, decision, note FROM reviews WHERE subject_type = 'asset_selection'"
    )
    assert {(r["status"], r["decision"], r["note"]) for r in rows} == {
        ("dismissed", None, maintenance.DISMISS_NOTE)
    }
    # 再跑一次什么都不改
    assert (
        maintenance.dismiss_selection(ctx, batches=["B001"], by="tester")["reviews"]
        == []
    )
    # 还有任务没出完的批次不让清
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'ready'")
    with pytest.raises(StageError, match="没出完"):
        maintenance.dismiss_selection(ctx, batches=["B001"], by="tester")


def test_similar_candidates_all_go_to_selection(ctx, fake, seed_image) -> None:
    one_model(ctx.home, n=2)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.image_mode = "similar"
    Worker(ctx, max_minutes=5).run()
    statuses = sorted(a["status"] for a in ctx.db.q("SELECT status FROM assets"))
    assert statuses == ["archived", "archived"]
    open_types = [
        r["subject_type"]
        for r in ctx.db.q("SELECT subject_type FROM reviews WHERE status = 'open'")
    ]
    assert open_types.count("asset_selection") == 2 and "asset_dup" not in open_types
    assert ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE phash IS NOT NULL") == 0
    assert "P08" not in fake.stage_counts


def test_identical_bytes_are_never_stored_twice(ctx, fake, seed_image) -> None:
    one_model(ctx.home, n=2)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    fake.image_mode = "identical"
    Worker(ctx, max_minutes=5).run()
    assert ctx.db.scalar("SELECT COUNT(*) FROM assets") == 1
    failed = ctx.db.q1("SELECT * FROM job_slots WHERE status = 'failed'")
    assert failed and "完全相同" in failed["last_error"]
