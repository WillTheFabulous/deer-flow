"""M1 实跑暴露的问题回归：编辑接口不认 format、500 包着参数错误、质检的系统性未验证项、作废失败修复后重修。"""

from __future__ import annotations

from conftest import reopen, set_config
from podflow import imagegen, review
from podflow.util import loads
from podflow.worker import Worker
from test_pipeline import bootstrap


def _single(ctx, model: str = "gpt-image-2.5-flare", n: int = 1):
    """只留一个模型、只做文生图：这些回归都发生在单条出图链路上。"""
    set_config(
        ctx.home, "image_generation.fanout", [{"model": model, "n": n, "enabled": True}]
    )
    set_config(ctx.home, "image_generation.modes", ["t2i"])
    return reopen(ctx.home)


def test_edit_payload_drops_format_but_keeps_size(ctx) -> None:
    gen = imagegen.build_payload(ctx, "gpt-image-2.5-sunburst", "p")
    edit = imagegen.build_payload(ctx, "gpt-image-2.5-sunburst", "p", [b"img"])
    assert gen["vendor_params"]["format"] == "png"
    # 是否带输入图决定走不走编辑接口，模型名不变
    assert edit["model"] == "gpt-image-2.5-sunburst"
    assert "format" not in edit["vendor_params"]
    assert (
        edit["vendor_params"]["size"] == "1856x2304" and edit["vendor_params"]["images"]
    )
    # -edit 模型按原模型的家族映射参数
    gem = imagegen.build_payload(ctx, "gemini-nano-banana-2.1-edit", "p", [b"img"])
    assert (
        gem["vendor_params"]["aspectRatio"] == "4:5" and gem["vendor_params"]["images"]
    )


def test_wrapped_parameter_error_is_not_retried(ctx, fake, seed_image) -> None:
    ctx = _single(ctx)
    bootstrap(ctx, seed_image, limit=1)
    fake.image_mode = "500_param"
    Worker(ctx, max_minutes=5).run()
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM provider_calls WHERE kind = 'image'") == 1
    )
    assert ctx.db.scalar("SELECT status FROM job_slots") == "failed"


def test_repair_succeeds_when_edit_rejects_format(ctx, fake, seed_image) -> None:
    ctx = _single(ctx, "gpt-image-2.5-sunburst")
    bootstrap(ctx, seed_image, limit=1)
    fake.image_mode = "edit_rejects_format"
    fake.qc_queue = ["repair", "pass"]
    Worker(ctx, max_minutes=5).run()
    assert [
        a["status"] for a in ctx.db.q("SELECT status FROM assets ORDER BY asset_id")
    ] == ["superseded", "archived"]


def test_systemic_unverified_passes_and_visual_doubt_is_flagged_into_selection(
    ctx, fake, seed_image
) -> None:
    ctx = _single(ctx, n=2)
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["needs_review_systemic", "needs_review_visual"]
    Worker(ctx, max_minutes=5).run()
    assets = ctx.db.q("SELECT * FROM assets ORDER BY asset_id")
    # 拿不准的那张也不停下等人：两张都归档进选款
    assert [a["status"] for a in assets] == ["archived", "archived"]
    qcs = {a["asset_id"]: loads(a["qc_json"]) for a in assets}
    systemic = next(q for q in qcs.values() if not q.get("flagged"))
    assert (
        systemic["model_decision"] == "needs_review"
        and systemic["decision"] == "pass"
        and "系统性" in systemic["override"]
    )
    doubt_id, doubt = next((k, q) for k, q in qcs.items() if q.get("flagged"))
    assert doubt["decision"] == "needs_review" and doubt["flags"] == [
        "肢体与承托接触：右下爪子与草丘交界看不清"
    ]
    assert not ctx.db.scalar(
        "SELECT COUNT(*) FROM reviews WHERE subject_type = 'asset_qc'"
    )
    payloads = {
        r["subject_id"]: loads(r["payload_json"])
        for r in ctx.db.q(
            "SELECT subject_id, payload_json FROM reviews WHERE subject_type = 'asset_selection'"
        )
    }
    assert payloads[doubt_id]["qc_flags"] == doubt["flags"]
    assert all(p["unverified_checks"] for p in payloads.values())
    # 首检通过率不把存疑的算作通过
    report = ctx.db.q1("SELECT report_json FROM batches WHERE batch_id = 'B001'")
    assert loads(report["report_json"])["metrics"]["first_pass_qc"] == {
        "numerator": 0,
        "denominator": 2,
    }


def test_requeue_repair_voids_program_failures(ctx, fake, seed_image) -> None:
    ctx = _single(ctx, "gpt-image-2.5-sunburst")
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["repair"]
    fake.image_mode = "ok"
    # 第一轮：修复请求全部失败（模拟当时的程序缺陷），父图被淘汰
    fake.image_modes_by_model["gpt-image-2.5-sunburst+images"] = "503"
    Worker(ctx, max_minutes=5).run()
    parent = ctx.db.q1("SELECT * FROM assets ORDER BY asset_id LIMIT 1")
    assert parent["status"] == "rejected"
    failed = ctx.db.scalar("SELECT COUNT(*) FROM provider_calls WHERE stage = 'T01R'")
    assert failed == 4
    # 人工放回修复：失败调用作废，重新修复成功
    fake.image_modes_by_model.clear()
    fake.qc_queue = ["pass"]
    result = review.requeue_asset(
        ctx, parent["asset_id"], "repair", by="tester", note="修复失败是程序参数缺陷"
    )
    assert len(result["voided_calls"]) == 4
    Worker(ctx, max_minutes=5).run()
    statuses = [
        a["status"] for a in ctx.db.q("SELECT status FROM assets ORDER BY asset_id")
    ]
    assert statuses == ["superseded", "archived"]
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM provider_calls WHERE stage = 'T01R'") == 5
    )


def test_requeue_qc_reruns_quality_check(ctx, fake, seed_image) -> None:
    ctx = _single(ctx)
    bootstrap(ctx, seed_image, limit=1)
    fake.qc_queue = ["reject", "pass"]
    Worker(ctx, max_minutes=5).run()
    asset = ctx.db.q1("SELECT * FROM assets")
    assert asset["status"] == "rejected"
    review.requeue_asset(
        ctx, asset["asset_id"], "qc", by="tester", note="质检口径修正后重判"
    )
    Worker(ctx, max_minutes=5).run()
    assert ctx.db.scalar("SELECT status FROM assets") == "archived"
