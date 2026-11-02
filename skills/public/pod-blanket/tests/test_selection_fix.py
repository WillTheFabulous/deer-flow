"""选款里人工要求的修改：后台只跑这一张，修改版跳过自动质检直接回选款；改不成就把原图放回选款。"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from conftest import card_text, reopen, set_config
from podflow import bg, fixes, review, state, ui
from podflow.stages.plan import asset_label_of
from podflow.tick import tick
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_qc_rules import one_model
from test_ui import _enable_feishu, run


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


def _run_child(capsys, monkeypatch: pytest.MonkeyPatch, call: dict) -> dict:
    """在本进程里扮演被拉起的后台进程：带上登记号跑同样的命令。"""
    monkeypatch.setenv(bg.ENV_KEY, call["env"][bg.ENV_KEY])
    return run(capsys, *call["argv"][2:])


@pytest.fixture
def ready(ctx, seed_image):
    """一个只有 flare 文生图的任务，跑完后有两张待选款。"""
    one_model(ctx.home, n=2)
    _enable_feishu(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    return ctx


def test_fix_repairs_and_comes_back_to_selection(
    ready, fake, spawned, capsys, monkeypatch
) -> None:
    ctx = ready
    # 积压上限只拦新增生成：另一张还在选款里，修改照样做
    set_config(ctx.home, "batch.review_backlog_limit", 1)
    ctx = reopen(ctx.home)
    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    parent = rv["subject_id"]
    out = review.decide(
        ctx, rv["review_id"], "needs_changes", by="tester", note="兔子耳朵改短"
    )
    assert out["effect"]["asset_status"] == "repair_pending"
    assert out["effect"]["fix_started"] and out["effect"]["assets"] == [parent]
    assert spawned[0]["argv"][2:] == ["worker", "--source", "fix"]
    task = ctx.db.q1("SELECT kind, subject FROM bg_tasks")
    assert (task["kind"], task["subject"]) == ("fix", parent)

    qc_calls = fake.stage_counts["P07"]
    result = _run_child(capsys, monkeypatch, spawned[0])
    assert [j["job_id"] for j in result["jobs"]] == ["J0001"]
    ctx = reopen(ctx.home)
    child = ctx.db.q1("SELECT * FROM assets WHERE parent_asset_id = ?", (parent,))
    assert child["status"] == "archived"
    assert (
        ctx.db.scalar("SELECT status FROM assets WHERE asset_id = ?", (parent,))
        == "superseded"
    )
    # 修改版不再自动质检；修改请求用原模型名、带原图，提示词是人的意见
    assert fake.stage_counts["P07"] == qc_calls
    edits = [p for p in fake.image_payloads() if "images" in p["vendor_params"]]
    assert len(edits) == 1 and edits[0]["model"] == "gpt-image-2.5-flare"
    assert "按下面的修改意见改" in edits[0]["prompt"]
    assert "兔子耳朵改短" in edits[0]["prompt"]

    new = review.find_review(ctx, "asset_selection", child["asset_id"])
    item = next(i for i in review.list_reviews(ctx) if i["review_id"] == new)
    assert item["payload"]["fixed_from"] == parent
    assert item["payload"]["fix_note"] == "兔子耳朵改短"
    view = ui.review_view(ctx, item)
    assert f"修改自 {asset_label_of(ctx, parent)}：兔子耳朵改短" in view["lines"]
    assert not review.find_review(ctx, "asset_selection", parent)
    # 卡片写图片的名字（编号_方向短语），修改版和原图是同一个方向
    card = fake.feishu_cards[-1]
    assert (
        card["header"]["title"]["content"] == f"修改完成｜{asset_label_of(ctx, parent)}"
    )
    assert f"新图 **{asset_label_of(ctx, child['asset_id'])}** 已回到选款" in card_text(
        card
    )
    assert asset_label_of(ctx, child["asset_id"]) != child["asset_id"]


def test_failed_fix_puts_original_back_into_selection(
    ready, fake, spawned, capsys, monkeypatch
) -> None:
    ctx = ready
    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    parent = rv["subject_id"]
    fake.image_modes_by_model["gpt-image-2.5-flare+images"] = "400"
    review.decide(
        ctx, rv["review_id"], "needs_changes", by="tester", note="背景换成雾蓝"
    )
    _run_child(capsys, monkeypatch, spawned[0])
    ctx = reopen(ctx.home)
    asset = ctx.db.q1("SELECT * FROM assets WHERE asset_id = ?", (parent,))
    assert asset["status"] == "archived" and asset["human_decision"] == "pending"
    rid = review.find_review(ctx, "asset_selection", parent)
    item = next(i for i in review.list_reviews(ctx) if i["review_id"] == rid)
    assert item["payload"]["fix_failed"].startswith("修改失败")
    assert fake.feishu_cards[-1]["header"]["title"]["content"] == (
        f"修改没成功｜{asset_label_of(ctx, parent)}"
    )
    # 再要求一次：之前失败的调用作废，不占重试次数
    fake.image_modes_by_model.clear()
    out = review.decide(ctx, rid, "needs_changes", by="tester", note="背景换成雾蓝")
    assert out["effect"]["voided_calls"]


def test_fix_of_a_retired_model_image_uses_the_replacement(
    ctx, seed_image, fake, spawned, capsys, monkeypatch
) -> None:
    set_config(
        ctx.home,
        "image_generation.fanout",
        [{"model": "gemini-3-pro-image-preview", "n": 1, "enabled": True}],
    )
    set_config(ctx.home, "image_generation.modes", ["t2i"])
    set_config(ctx.home, "image_generation.replaced_models", {})
    set_config(ctx.home, "prices/image/gemini-3-pro-image-preview", {"per_image": 0.5})
    _enable_feishu(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    parent = ctx.db.q1("SELECT asset_id, model FROM assets")
    assert parent["model"] == "gemini-3-pro-image-preview"

    # pro 退役后，旧图在选款里「修改」改用 nano-banana 2.1，新图记实际用的模型
    set_config(
        ctx.home,
        "image_generation.replaced_models",
        {"gemini-3-pro-image-preview": "gemini-nano-banana-2.1"},
    )
    ctx = reopen(ctx.home)
    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    review.decide(
        ctx, rv["review_id"], "needs_changes", by="tester", note="背景换成雾蓝"
    )
    _run_child(capsys, monkeypatch, spawned[0])
    ctx = reopen(ctx.home)
    child = ctx.db.q1(
        "SELECT * FROM assets WHERE parent_asset_id = ?", (parent["asset_id"],)
    )
    assert (child["model"], child["slot_id"]) == (
        "gemini-nano-banana-2.1",
        "gemini-3-pro-image-preview#1",
    )
    edits = [p for p in fake.image_payloads() if "images" in p["vendor_params"]]
    assert [p["model"] for p in edits] == ["gemini-nano-banana-2.1"]
    item = next(
        i
        for i in ui.reviews_page(ctx, limit=10, types=["asset_selection"])["items"]
        if i["subject_id"] == child["asset_id"]
    )
    assert any(
        line.startswith("出图：nano-banana-2.1 · 文生图") for line in item["lines"]
    )


def test_fix_needs_a_note_on_an_archived_asset(ready, spawned) -> None:
    ctx = ready
    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    with pytest.raises(review.ReviewError):
        review.decide(ctx, rv["review_id"], "needs_changes", by="t", note="  ")
    with pytest.raises(review.ReviewError):
        review.decide(
            ctx,
            rv["review_id"],
            "needs_changes",
            by="t",
            note="改" * (review.FIX_NOTE_CHARS + 1),
        )
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE assets SET status = 'preview_ready' WHERE asset_id = ?",
            (rv["subject_id"],),
        )
    with pytest.raises(review.ReviewError):
        review.decide(ctx, rv["review_id"], "needs_changes", by="t", note="改短")
    assert not spawned


def test_queued_fix_starts_after_the_running_worker(
    ctx, fake, seed_image, spawned
) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    bootstrap(ctx, seed_image, limit=2)
    # --job 只推进指定任务，别的排队任务不动
    first = Worker(ctx, max_minutes=5, only_jobs=["J0001"]).run()
    statuses = dict(ctx.db.q("SELECT job_id, status FROM jobs ORDER BY job_id"))
    assert statuses == {"J0001": "complete", "J0002": "ready"}
    # 人选中的方向一直出到出完：这一轮结束就接着拉起 J0002（这里拦下了，不真的跑）
    assert first["continued"]["jobs"] == ["J0002"]
    assert spawned.pop()["argv"][2:] == ["worker", "--source", "directions"]
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")

    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    assert state.acquire_worker_lease(ctx.db, "someone-else", lease_seconds=60)
    out = review.decide(ctx, rv["review_id"], "needs_changes", by="t", note="改短")
    assert out["effect"]["fix_queued"] == [rv["subject_id"]] and not spawned
    state.release_worker_lease(ctx.db, "someone-else")
    # 另一轮出图结束时，接着拉起排队的修改
    summary = Worker(ctx, max_minutes=5, only_jobs=["J0002"]).run()
    assert summary["fixes"]["fix_started"]
    assert spawned[0]["argv"][2:] == ["worker", "--source", "fix"]
    assert fixes.pending(ctx) == [{"asset_id": rv["subject_id"], "job_id": "J0001"}]


def test_paused_fix_starts_on_resume(ready, spawned, capsys) -> None:
    ctx = ready
    run(capsys, "pause", "--reason", "测试")
    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    out = review.decide(ctx, rv["review_id"], "needs_changes", by="t", note="改短")
    assert out["effect"]["fix_queued"] == [rv["subject_id"]]
    assert out["effect"]["reason"] == "流水线已暂停" and not spawned
    result = run(capsys, "resume", "--by", "t")
    assert result["fixes"]["fix_started"]
    assert spawned[0]["argv"][2:] == ["worker", "--source", "fix"]


def test_tick_picks_up_an_interrupted_fix(ready, spawned) -> None:
    """修改进程被打断（任务卡在 running、租约过期）：试运行窗口没开也接着改。"""
    ctx = ready
    rv = review.list_reviews(ctx, types=["asset_selection"])[0]
    review.decide(ctx, rv["review_id"], "needs_changes", by="t", note="改短")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET pid = 999999999")
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = 'gone' WHERE job_id = 'J0001'"
        )
    spawned.clear()
    assert not fixes.pending(ctx)
    result = tick(ctx)
    assert result["decision"] == "dispatch"
    assert result["jobs"][0]["kind"] == "fix"
    assert any("回收了上次中断的遗留" in a for a in result["alerts"])
    assert spawned[0]["argv"][2:] == ["worker", "--source", "fix"]
    # 不出图的巡检（spawn=False）不回收、不拉起
    spawned.clear()
    tick(ctx, spawn=False)
    assert not spawned
