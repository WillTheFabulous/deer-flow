"""二次大变化（2026-10-27）：一批小变化出完后，以换了主体的小变化出的图为底图，各策划一个单字段大改；
图生图的输入图是这张底图，P06 照底图里的新主体写；一个原批次自动只派生一次（derived_rounds），只做一层；
选款卡 / 选款表的第一张图放底图；卡片里方向太多时按文字预算收紧。"""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest
from conftest import confirm_all, input_block, reopen, set_config
from podflow import bg, cli, derive, images, ui
from podflow.stages import plan as planmod
from podflow.stages.common import asset_bytes
from podflow.stages.job import REFERENCE_MAX_SIDE
from podflow.util import loads
from podflow.worker import Worker
from test_small_swap import _swap_tasks
from test_subject_pairs import _family, _home
from test_variant_rules import _chat_messages

SUNBURST = "gpt-image-2.5-sunburst"
# 出图 worker 收尾时会拉起二次大变化的后台进程：一律拦下，不真的起子进程去抢测试库
pytestmark = pytest.mark.usefixtures("spawned")


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


def _derived_ctx(home, fake, *, count: int = 2):
    _home(home, fake)
    set_config(home, "plan.small_swap_ratio", 1.0)
    set_config(home, "plan.small_count", 2)
    set_config(home, "plan.big_count", 0)
    set_config(home, "plan.derived.enabled", True)
    set_config(home, "plan.derived.count", count)
    set_config(
        home,
        "image_generation.fanout",
        [{"model": SUNBURST, "n": 1, "enabled": True}],
    )
    set_config(home, "image_generation.i2i_edit", False)
    return reopen(home)


def _small_round(ctx, fake, seed_image) -> tuple[str, dict]:
    """策划一轮换主体的小变化、全部确认、出完。"""
    family_id = _family(ctx, seed_image)
    fake.p05_tasks = lambda group: (
        _swap_tasks(group) if group["kind"] == "small" else None
    )
    plan = planmod.plan_batch(ctx, family_id)
    assert plan["counts"] == {"small": 2}
    confirm_all(ctx, plan)
    Worker(ctx, max_minutes=5).run()
    return family_id, plan


def test_due_plan_and_run_derived(home, fake, seed_image) -> None:
    """小变化出完：这一批该派生；派生出的方向记父方向与第 2 代、底图是父方向出的那张图；同一批不再派生。"""
    ctx = _derived_ctx(home, fake)
    family_id, plan = _small_round(ctx, fake, seed_image)
    source = plan["batch_id"]
    assert derive.due(ctx) == [source]
    out = derive.run_due(ctx)
    assert not out["skipped"]
    (derived,) = out["planned"]
    assert derived["derived_from"] == source
    assert derived["counts"] == {"derived": 2}
    parents = {t["variant_id"] for t in plan["accepted"]}
    assert set(derived["parents"]) == parents
    rows = ctx.db.q(
        "SELECT * FROM variants WHERE batch_id = ? ORDER BY variant_id",
        (derived["batch_id"],),
    )
    assert {r["parent_variant_id"] for r in rows} == parents
    assert {r["generation_depth"] for r in rows} == {2}
    for row in rows:
        vplan = loads(row["plan_json"])
        assert vplan["kind"] == "derived" and vplan["task_type"] == "big"
        base = vplan["base"]
        asset = ctx.db.q1(
            "SELECT * FROM assets WHERE asset_id = ?", (base["asset_id"],)
        )
        assert asset["variant_id"] == row["parent_variant_id"]
        assert asset["model"] == SUNBURST
        assert base["changes"][0]["field_id"] == "D09"
        # 简述最前面是底图的换主体
        assert vplan["brief"][0]["inherited"] is True
        assert vplan["brief"][0]["aspect"].startswith("底图·")
        assert [cf["field_id"] for cf in vplan["changed_fields"]] == ["D41"]
    record = ctx.db.q1(
        "SELECT * FROM derived_rounds WHERE source_batch_id = ?", (source,)
    )
    assert record["status"] == "planned" and record["batch_id"] == derived["batch_id"]
    # 一个原批次自动只派生一次；二次批次本身不再派生
    assert derive.due(ctx) == []
    assert derive.run_due(ctx) == {"planned": [], "skipped": []}
    with pytest.raises(planmod.StageError, match="只做一层"):
        planmod.plan_derived(ctx, derived["batch_id"])
    # P05 拿到 derived 组：槽位带底图与它的换主体
    group = next(g for g in fake.p05_groups if g["kind"] == "derived")
    assert group["rule"] == planmod.DERIVED_RULE
    assert all(s["base"]["changes"][0]["field_id"] == "D09" for s in group["slots"])


def test_derived_jobs_use_the_base_image(home, fake, seed_image) -> None:
    """二次大变化出图：两路（文生图 + 图生图），图生图的输入图是底图，P06 拿到底图与它的换主体。"""
    ctx = _derived_ctx(home, fake, count=1)
    _small_round(ctx, fake, seed_image)
    (derived,) = derive.run_due(ctx)["planned"]
    confirm = confirm_all(ctx, derived)
    assert list(confirm["images_per_direction"].values()) == [2]
    before = len(fake.image_payloads())
    Worker(ctx, max_minutes=5).run()
    vplan = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM variants WHERE batch_id = ?", (derived["batch_id"],)
        )
    )
    base_asset = vplan["base"]["asset_id"]
    job = ctx.db.q1("SELECT * FROM jobs WHERE batch_id = ?", (derived["batch_id"],))
    assert job["status"] == "complete"
    render = loads(job["render_spec_json"])
    assert render["input_asset_id"] == base_asset
    assert render["result"]["reference_asset_ids"] == [base_asset]
    expected = base64.b64encode(
        images.to_vision_jpeg(asset_bytes(ctx, base_asset), REFERENCE_MAX_SIDE)
    ).decode("ascii")
    i2i = [
        p
        for p in fake.image_payloads()[before:]
        if "images" in p.get("vendor_params", {})
    ]
    assert i2i and all(p["vendor_params"]["images"] == [expected] for p in i2i)
    p06 = _chat_messages(fake, "P06")[-1]
    payload = {"messages": p06}
    variant_job = input_block(payload, "variant_job")
    assert variant_job["base"]["asset_id"] == base_asset
    assert input_block(payload, "reference_assets")[0]["asset_id"] == base_asset


def test_selection_and_cards_show_the_base(home, fake, seed_image) -> None:
    """选款项第一张图是底图；挑选卡分段写「二次大变化」；任务明细的批次标题写底图来自哪一批。"""
    ctx = _derived_ctx(home, fake, count=1)
    family_id, plan = _small_round(ctx, fake, seed_image)
    (derived,) = derive.run_due(ctx)["planned"]
    view = ui.directions_view(ctx, derived["batch_id"])
    assert view["items"][0]["section"].startswith("二次大变化（1）")
    assert view["items"][0]["lines"][0].startswith("底图·")
    confirm_all(ctx, derived)
    Worker(ctx, max_minutes=5).run()
    items = ui.reviews_page(
        ctx, limit=10, types=["asset_selection"], batch_id=derived["batch_id"]
    )["items"]
    assert items
    for item in items:
        assert item["images"][0]["label"].startswith("底图 IMG")
        assert "二次大变化" in item["lines"][0]
    history = ui.history_view(ctx, family_id)
    head = history["groups"][0]["head"]
    assert derived["batch_id"] in head and f"底图来自 {plan['batch_id']}" in head


def test_parents_need_a_swap_and_a_finished_image(home, fake, seed_image) -> None:
    """没出完、没有换主体的小变化不能当底图；手动指定不能当底图的方向报原因；预览不调模型。"""
    ctx = _derived_ctx(home, fake)
    family_id = _family(ctx, seed_image)
    fake.p05_tasks = _swap_tasks
    plan = planmod.plan_batch(ctx, family_id)
    confirm_all(ctx, plan)
    # 还没出图：不该派生，手动也挑不出底图
    assert derive.due(ctx) == []
    with pytest.raises(planmod.StageError, match="没有能当底图的小变化"):
        planmod.plan_derived(ctx, plan["batch_id"])
    vid = plan["accepted"][0]["variant_id"]
    with pytest.raises(planmod.StageError, match="还没出完"):
        planmod.plan_derived(ctx, plan["batch_id"], parents=[vid])
    Worker(ctx, max_minutes=5).run()
    calls = sum(fake.stage_counts.values())
    preview = derive.preview(ctx, plan["batch_id"], count=1)
    assert sum(fake.stage_counts.values()) == calls
    assert len(preview["slots"]) == 1 and preview["slots"][0]["field"] == "绘制技法"
    # 判了不要的图不当底图
    for row in ctx.db.q("SELECT asset_id FROM assets WHERE variant_id = ?", (vid,)):
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE assets SET human_decision = 'rejected' WHERE asset_id = ?",
                (row["asset_id"],),
            )
    picks = planmod.derived_parents(ctx, plan["batch_id"])
    assert vid not in {p["variant_id"] for p in picks}


def test_kick_spawns_one_derive_process(home, fake, seed_image, spawned) -> None:
    """有该派生的批次时巡检 / worker 拉起一个 plan --derive-due；已经有一个在跑就不再拉；开关关着什么都不做。"""
    ctx = _derived_ctx(home, fake)
    _small_round(ctx, fake, seed_image)
    # 出图 worker 收尾时的反馈循环已经拉起了一个（拦下的进程视为还在跑）
    derives = [c for c in spawned if c["argv"][-2:] == ["plan", "--derive-due"]]
    assert len(derives) == 1
    assert derive.kick(ctx) is None
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")
    # 那个进程结束了、这一批还没派生：巡检再拉一个
    task = derive.kick(ctx)
    assert task and spawned[-1]["argv"][-2:] == ["plan", "--derive-due"]
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")
    set_config(home, "plan.derived.enabled", False)
    assert derive.kick(reopen(home)) is None


def test_cli_derive_from_and_hash(home, fake, seed_image, capsys) -> None:
    """命令行手动派生：--preview 只看、--derive-from 真做（补一行记录）；计划指纹带底图，同一个大改落在不同底图上不重复。"""
    ctx = _derived_ctx(home, fake, count=1)
    _, plan = _small_round(ctx, fake, seed_image)
    source = plan["batch_id"]
    assert cli.main(["plan", "--derive-from", source, "--preview"]) == 0
    capsys.readouterr()
    # --derive-due --preview 只列该派生的批次，不派生
    assert cli.main(["plan", "--derive-due", "--preview"]) == 0
    assert source in capsys.readouterr().out
    assert ctx.db.scalar("SELECT COUNT(*) FROM derived_rounds") == 0
    assert cli.main(["plan", "--derive-from", source, "--by", "tester"]) == 0
    record = ctx.db.q1(
        "SELECT * FROM derived_rounds WHERE source_batch_id = ?", (source,)
    )
    assert record["status"] == "planned" and record["created_by"] == "tester"
    task = {"changed_fields": [{"field_id": "D41", "to": "剪纸拼贴"}]}
    plain_hash = planmod.plan_hash("F01", task)
    assert (
        planmod.plan_hash("F01", {**task, "base": {"variant_id": "V0001"}})
        != plain_hash
    )
    assert planmod.plan_hash("F01", {**task, "base": {"variant_id": "V0002"}}) != (
        planmod.plan_hash("F01", {**task, "base": {"variant_id": "V0001"}})
    )


def test_card_budget_keeps_cards_small(home, fake, seed_image, monkeypatch) -> None:
    """方向多到撑过文字预算：任务明细改紧凑行、只列前几行并写其余几个；挑选卡只留标题和勾选框。"""
    batch = {
        "batch_id": "B099",
        "directions": [
            {
                "index": i,
                "title": f"很长很长的方向标题第{i}个用来撑大卡片",
                "aspects": ["元素角色", "配色结构"],
                "state_text": "已出完 · 2 张",
            }
            for i in range(1, 61)
        ],
    }
    rows = ui._fit_rows(batch, 2_000)
    assert ui._text_bytes(rows) <= 2_000
    assert rows[-1].startswith("…其余") and "B099 的方向" in rows[-1]
    assert ui._fit_rows(batch, 100_000)[0].startswith("1. 很长很长")
    ctx = _derived_ctx(home, fake)
    family_id, plan = _small_round(ctx, fake, seed_image)
    monkeypatch.setattr(ui, "CARD_TEXT_BUDGET", 50)
    view = ui.directions_view(ctx, plan["batch_id"])
    assert all(item["lines"] == [] for item in view["items"])
    assert any("卡片只列标题" in line for line in view["lines"])
    history = ui.history_view(ctx, family_id)
    assert len(history["groups"]) == 1
