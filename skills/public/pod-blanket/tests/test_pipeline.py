"""端到端（M1 形态）：母版 S01→S04 → 策划候选方向 → 人挑选（一并认可系列）→ worker 推进到归档 → 批次反馈。"""

from __future__ import annotations

import json

import pytest
from conftest import (
    EDIT_MODELS,
    FANOUT_MODELS,
    SLOTS_PER_JOB,
    confirm_all,
    p05_task,
    reopen,
    set_config,
)
from podflow import cli, review
from podflow.db import open_review
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.util import loads
from podflow.worker import Worker


def bootstrap(ctx, seed_image, *, limit: int = 3) -> dict:
    """拆解 → 策划 limit 个方向 → 全部选中（系列随之认可），任务就绪等出图。"""
    seed = seedmod.add_seed(
        ctx, seed_image, source_ref="测试参考图", source_note="儿童动物图"
    )
    analysis = seedmod.analyze(ctx, seed["seed_id"])
    family_id = analysis["steps"][-1]["family_id"]
    plan = planmod.plan_batch(ctx, family_id, limit=limit)
    plan["confirmed"] = confirm_all(ctx, plan)
    return plan


def test_seed_stages_enforce_program_truth(ctx, fake, seed_image) -> None:
    fake.p02_first_incomplete = True
    seed = seedmod.add_seed(ctx, seed_image)
    assert seedmod.add_seed(ctx, seed_image)["created"] is False
    result = seedmod.analyze(ctx, seed["seed_id"])
    assert [s["stage"] for s in result["steps"]] == ["S01", "S02", "S03", "S04"]
    row = seedmod.get_seed(ctx, seed["seed_id"])
    record = loads(row["seed_record_json"])
    # 模型说 approved 不算数，授权以人工登记（unknown）为准
    assert record["result"]["rights_status"] == "unknown"
    assert record["result"]["model_rights_guess"] == "approved"
    # P02 第一次缺字段 → 带校验错误重问一次
    assert fake.stage_counts["P02"] == 2
    analysis = seedmod.latest_analysis(ctx, seed["seed_id"])
    assert analysis["analysis_id"] == "S01-A01"
    # 梳理后在用 40 个字段（60 个编号里停用了 20 个，绘制技法 D42–D44 并进 D41）
    assert len(loads(analysis["result_json"])["fields"]) == 40
    hyp = ctx.db.q1("SELECT result_json FROM hypotheses")
    assert {h["status"] for h in loads(hyp["result_json"])["hypotheses"]} == {
        "unvalidated"
    }
    fam = ctx.db.q1("SELECT * FROM families")
    assert fam["status"] == "pending" and fam["family_id"] == "F01"
    assert ctx.db.q1(
        "SELECT 1 FROM reviews WHERE subject_type = 'family' AND status = 'open'"
    )


def test_plan_proposes_candidates_and_dedups(ctx, fake, seed_image) -> None:
    seed = seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, seed["seed_id"])
    # 系列还待认可也能策划：每个可变字段一个候选方向，不出图，人确认方向时一并认可系列
    plan = planmod.plan_batch(ctx, "F01")
    assert plan["status"] == "proposed" and plan["family_status"] == "pending"
    targets = [t["changed_fields"][0]["to"] for t in plan["accepted"]]
    assert targets == ["主题方案1", "构图类型方案1", "视觉中心方案1"]
    assert plan["dropped"] == [] and plan["coverage_gaps"] == []
    assert plan["slots"] == {"small": 0, "big": 3, "combo": 0, "total": 3}
    assert ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 0
    open_types = [
        r["subject_type"]
        for r in ctx.db.q("SELECT subject_type FROM reviews WHERE status = 'open'")
    ]
    # 只审一次：「系列规则」待审并进了「裂变方向」
    assert open_types == ["plan_selection"]
    # 有一批待挑选时不能再策划，要先挑完或换一批
    with pytest.raises(seedmod.StageError, match="待挑选"):
        planmod.plan_batch(ctx, "F01")

    confirmed = confirm_all(ctx, plan)
    assert confirmed["family_approved"] is True and confirmed["jobs"] == [
        "J0001",
        "J0002",
        "J0003",
    ]
    assert ctx.db.q1("SELECT status FROM families")["status"] == "approved"
    slots = ctx.db.q(
        "SELECT slot_id FROM job_slots WHERE job_id = 'J0001' ORDER BY slot_id"
    )
    # 大变化：每个模型文生图一份（编号与旧任务一致）、图生图一份、图生图 Edit 一份
    assert sorted(s["slot_id"] for s in slots) == sorted(
        [f"{m}#1" for m in FANOUT_MODELS]
        + [f"{m}#1@i2i" for m in FANOUT_MODELS]
        + [f"{m}#1@i2i" for m in EDIT_MODELS]
    )
    assert set(confirmed["images_per_direction"].values()) == {SLOTS_PER_JOB}
    # 同一系列再策划：同一套改动（换个标点也算）不再提；改成的样子和做过的一样、只换了说法的也不提
    replies = {
        "D05": p05_task("", [("D05", "主题方案1。")]),
        "D27": p05_task("", [("D27", "构图类型方案2")]),
        "D28": {
            **p05_task("", [("D28", "换了说法的长描述：左侧主角放大")]),
            "brief": [{"field_id": "D28", "from": "原样", "to": "视觉中心方案1"}],
        },
    }
    fake.p05_tasks = lambda group: [
        {**replies[s["fields"][0]["field_id"]], "slot_id": s["slot_id"]}
        for s in group["slots"]
    ]
    again = planmod.plan_batch(ctx, "F01")
    assert [t["changed_fields"][0]["to"] for t in again["accepted"]] == [
        "构图类型方案2"
    ]
    assert sorted(d["reason"] for d in again["dropped"]) == [
        "duplicate_direction",
        "duplicate_plan",
    ]
    assert {g["label"] for g in again["coverage_gaps"]} == {"主题", "视觉中心"}
    # 两轮各一块，P05 拿到这些字段上做过的方向
    assert [g["kind"] for g in fake.p05_groups] == ["big", "big"]


def test_worker_runs_batch_to_archive(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=3)
    summary = Worker(ctx, batch_limit=12, max_minutes=5).run()
    assert summary["status"] == "done"
    assert [j["status"] for j in summary["jobs"]] == ["complete"] * 3
    assets = ctx.db.q("SELECT * FROM assets ORDER BY asset_id")
    # 3 个任务 × 3 个模型 × 文生图 / 图生图 / 图生图 Edit
    assert len(assets) == 3 * SLOTS_PER_JOB
    assert {a["status"] for a in assets} == {"archived"}
    assert {a["model"] for a in assets} == set(FANOUT_MODELS) | set(EDIT_MODELS)
    modes = []
    for a in assets:
        manifest = (ctx.home / a["path"]).with_suffix(".manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert (
            data["lineage"]["seed_id"] == "S01"
            and data["lineage"]["family"] == "F01@v1"
        )
        assert "dedup_status" not in data
        modes.append(data["generation"]["mode"])
        assert data["costs"]["cost_status"] == "settled" and data["costs"]["total"] > 0
        assert (
            data["human_decision"] == "pending"
            and data["market_validation"] == "not_tested"
        )
    assert sorted(modes) == ["i2i"] * 18 + ["t2i"] * 9
    # 每张图都开了选款审核项（没有近重复判定）；批次反馈已生成
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM reviews WHERE subject_type = 'asset_selection' AND status = 'open'"
        )
        == 3 * SLOTS_PER_JOB
    )
    assert "P08" not in fake.stage_counts
    assert (
        summary["batch_reports"]
        and summary["batch_reports"][0]["metrics"]["first_pass_qc_rate"] == 1.0
    )
    batch = ctx.db.q1("SELECT * FROM batches WHERE batch_id = 'B001'")
    assert batch["status"] == "done"
    # 生图参数按模型族映射；图生图用原模型名和 -edit 名各一次、带一张输入图，去掉编辑接口不收的 format；
    # 不发计费上下文头
    payloads = fake.image_payloads()
    assert {p["model"] for p in payloads} == set(FANOUT_MODELS) | set(EDIT_MODELS)
    assert sum("images" in p["vendor_params"] for p in payloads) == 18
    i2i_prompts = {
        (p["model"], p["prompt"]) for p in payloads if "images" in p["vendor_params"]
    }
    edits = [p for p in payloads if p["model"].endswith("-edit")]
    assert len(edits) == 9 and all("images" in p["vendor_params"] for p in edits)
    # Edit 那张和同一模型的图生图用同一份提示词
    assert all(
        (p["model"].removesuffix("-edit"), p["prompt"]) in i2i_prompts for p in edits
    )
    for payload in payloads:
        vp = dict(payload["vendor_params"])
        edit = len(vp.pop("images", [])) == 1
        if payload["model"].startswith("gemini"):
            assert vp == {"imageSize": "2K", "aspectRatio": "4:5"}
        elif edit:
            assert vp == {"size": "1856x2304", "quality": "high"}
        else:
            assert vp == {"size": "1856x2304", "quality": "high", "format": "png"}
    assert all(
        "x-billing-context" not in {k.lower() for k in r["headers"]}
        for r in fake.requests
    )
    # 授权未知也做图生图：参考图作为输入图，文生图仍只用文字
    render = loads(
        ctx.db.scalar("SELECT render_spec_json FROM jobs WHERE job_id = 'J0001'")
    )
    assert (
        render["result"]["output_level"] == "preview"
        and render["result"]["reference_asset_ids"] == ["seed:S01"]
        and render["modes"] == ["t2i", "i2i"]
    )


def test_restricted_seed_skips_i2i(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    seedmod.set_rights(ctx, "S01", "restricted", by="tester")
    Worker(ctx, max_minutes=5).run()
    slots = [r["slot_id"] for r in ctx.db.q("SELECT slot_id FROM job_slots")]
    assert sorted(slots) == sorted(f"{m}#1" for m in FANOUT_MODELS)
    assert all("images" not in p["vendor_params"] for p in fake.image_payloads())
    render = loads(ctx.db.scalar("SELECT render_spec_json FROM jobs"))
    assert render["modes"] == ["t2i"] and render["i2i_prompts"] == {}
    assert any("授权受限" in note for note in render["notes"])
    assert ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'") == 3


def test_jobs_planned_before_i2i_get_i2i_slots_at_s06(home, fake, seed_image) -> None:
    set_config(home, "image_generation.modes", ["t2i"])
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    assert ctx.db.scalar("SELECT COUNT(*) FROM job_slots") == 3
    set_config(home, "image_generation.modes", ["t2i", "i2i"])
    ctx = reopen(home)
    Worker(ctx, max_minutes=5).run()
    slots = [r["slot_id"] for r in ctx.db.q("SELECT slot_id FROM job_slots")]
    # 补上图生图和图生图 Edit
    assert len(slots) == SLOTS_PER_JOB and sum(s.endswith("@i2i") for s in slots) == 6
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'")
        == SLOTS_PER_JOB
    )
    synced = ctx.db.q1("SELECT payload_json FROM events WHERE event = 'slots_synced'")
    assert len(loads(synced["payload_json"])["added"]) == 6


def test_retire_dedup_moves_suspected_duplicates_to_selection(
    ctx, fake, seed_image, capsys
) -> None:
    set_config(ctx.home, "image_generation.modes", ["t2i"])
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    # 造一份下线前的存量：一张图停在「疑似重复」（没预检、没归档、没开选款）
    asset_id = ctx.db.scalar("SELECT asset_id FROM assets ORDER BY asset_id LIMIT 1")
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE assets SET status = 'review_dup', print_json = NULL, archive_json = NULL WHERE asset_id = ?",
            (asset_id,),
        )
        conn.execute(
            "DELETE FROM reviews WHERE subject_type = 'asset_selection' AND subject_id = ?",
            (asset_id,),
        )
        open_review(conn, "asset_dup", asset_id, "疑似重复", {})
    assert cli.main(["maintenance", "retire-dedup", "--by", "tester"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["assets_to_selection"] == [asset_id]
    assert out["processed_jobs"] == ["J0001"] and len(out["dismissed_reviews"]) == 1
    ctx = reopen(ctx.home)
    status = ctx.db.scalar("SELECT status FROM assets WHERE asset_id = ?", (asset_id,))
    assert status == "archived"
    assert review.find_review(ctx, "asset_selection", asset_id)
    assert not review.list_reviews(ctx, types=["asset_dup"])
    # 再跑一次什么都不改
    assert cli.main(["maintenance", "retire-dedup", "--by", "tester"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["dismissed_reviews"] == again["assets_to_selection"] == []


def test_selection_review_and_export_import(ctx, fake, seed_image, tmp_path) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, batch_limit=12, max_minutes=5).run()
    items = review.list_reviews(ctx, types=["asset_selection"])
    assert len(items) == SLOTS_PER_JOB
    sheet = review.contact_sheet(ctx, tmp_path / "out" / "sheet.png", batch_id="B001")
    assert (
        len(sheet["assets"]) == SLOTS_PER_JOB
        and (tmp_path / "out" / "sheet.png").exists()
    )
    xlsx = tmp_path / "out" / "review.xlsx"
    review.export_xlsx(ctx, xlsx, batch_id="B001")
    from openpyxl import load_workbook

    wb = load_workbook(xlsx)
    ws = wb["选款"]
    assert ws["H1"].value == "mode"
    assert sorted(
        ws.cell(row=r, column=8).value for r in range(2, 2 + SLOTS_PER_JOB)
    ) == (["图生图"] * 6 + ["文生图"] * 3)
    ws["J2"] = "approved"
    ws["J3"] = "rejected"
    ws["K3"] = "配色太灰"
    wb.save(xlsx)
    result = review.import_xlsx(ctx, xlsx, by="tester")
    assert result["applied"] == 2 and not result["skipped"]
    decisions = {
        r["decision"]
        for r in ctx.db.q(
            "SELECT decision FROM reviews WHERE status = 'decided' AND subject_type = 'asset_selection'"
        )
    }
    assert decisions == {"approved", "rejected"}
