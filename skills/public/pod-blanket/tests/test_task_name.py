"""任务命名（S01N）：拆解时另起一个线程让模型看原图写短描述（P12），接上登记那天的月日作为任务名字。"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import default_result, envelope, reopen, set_config
from podflow import (
    bg,
    candidates,
    feishu_table,
    maintenance,
    notify,
    scenes,
    tasks,
    ui,
)
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.stages.common import StageError, get_seed, subject_label, task_label
from test_candidates import _setup as _table_setup
from test_pattern_extract import _user_content, run_error
from test_pipeline import bootstrap
from test_ui import run

DESC = "森林三伙伴水彩"


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


def _enable(home):
    set_config(home, "task_name.enabled", True)
    return reopen(home)


def _mmdd(ctx, seed_id: str = "S01") -> str:
    created = dt.datetime.fromisoformat(get_seed(ctx, seed_id)["created_at"])
    return created.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m%d")


def _content(fake, stage: str, payload: dict, result: dict | None = None) -> str:
    body = result if result is not None else default_result(stage, payload, fake)
    return json.dumps(envelope(stage, body), ensure_ascii=False)


def test_analyze_names_the_task_alongside_s01(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    p12_arrived = threading.Event()
    overlapped: list[bool] = []

    def p01(payload: dict) -> str:
        # S01 的请求等起名的请求到了才回：起名是在另一个线程里同时跑的
        overlapped.append(p12_arrived.wait(10))
        return _content(fake, "P01", payload)

    def p12(payload: dict) -> str:
        p12_arrived.set()
        return _content(fake, "P12", payload)

    fake.overrides.update(P01=p01, P12=p12)
    out = seedmod.analyze(ctx, "S01")
    assert overlapped == [True]
    assert [s["stage"] for s in out["steps"]] == ["S01", "S02", "S03", "S04"]
    name = f"{DESC} {_mmdd(ctx)}"
    assert out["name"] == name and "name_error" not in out
    assert fake.stage_counts["P12"] == 1

    row = dict(ctx.db.q1("SELECT * FROM seed_names WHERE seed_id = 'S01'"))
    assert (row["name"], row["description"], row["source"]) == (name, DESC, "auto")
    assert row["model"] == "gpt-6.1-sol-high" and row["table_name"] is None
    assert len(json.loads(row["call_ids_json"])) == 1
    call = ctx.db.q1("SELECT stage, scope_id FROM provider_calls WHERE stage = 'P12'")
    assert call["scope_id"] == "seed:S01"
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'task_named'") == 1
    # 看的是原图一张，带上登记信息
    content = _user_content(fake, "P12")
    assert sum(1 for part in content if part.get("type") == "image_url") == 1
    assert "source_metadata" in content[0]["text"]

    # 已有名字：再拆解、按新字段重拆都不再起名
    seedmod.analyze(ctx, "S01", force=True)
    seedmod.analyze(ctx, "S01", reanalyze=True)
    assert fake.stage_counts["P12"] == 1


def test_date_is_the_upload_day_in_local_time(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE seeds SET created_at = ? WHERE seed_id = 'S01'",
            ("2026-10-19T17:30:00+00:00",),
        )
    out = seedmod.name_seed(ctx, "S01")
    # UTC 10-19 17:30 是上海 10-20 01:30
    assert (out["name"], out["date"]) == (f"{DESC} 1020", "2026-10-20")
    assert out["label"] == f"S01 {DESC} 1020"


def test_bad_descriptions_are_cleaned_or_answered_again(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    answers = iter(["一二三四五六七八九十十一", " 海洋·鲸鱼 卡通！"])

    fake.overrides["P12"] = lambda payload: _content(
        fake, "P12", payload, {"description": next(answers)}
    )
    out = seedmod.name_seed(ctx, "S01")
    # 第一次 11 个字被退回重答；第二次去掉空格和标点
    assert fake.stage_counts["P12"] == 2
    assert out["description"] == "海洋鲸鱼卡通"
    assert seedmod.clean_description("复古[玫瑰]:格纹/?*") == "复古玫瑰格纹"


def test_naming_failure_does_not_block_analysis(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    fake.overrides["P12"] = lambda payload: "不是 JSON"
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]] == ["S01", "S02", "S03", "S04"]
    assert "name" not in out and "StageOutputInvalid" in out["name_error"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_names") == 0
    failed = ctx.db.q1(
        "SELECT payload_json FROM events WHERE event = 'task_name_failed'"
    )
    assert "StageOutputInvalid" in json.loads(failed["payload_json"])["error"]
    # 没有名字时照旧只显示编号
    assert task_label(ctx, "S01") == "S01"
    (item,) = ui.tasks_view(ctx)["items"]
    assert item["title"].startswith("S01 · F01 v1　")
    # 下一次拆解还会再试着起名
    del fake.overrides["P12"]
    again = seedmod.analyze(ctx, "S01")
    assert again["name"] == f"{DESC} {_mmdd(ctx)}"


def test_disabled_does_not_name(home, fake, seed_image) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    assert "name" not in out and "name_error" not in out
    assert fake.stage_counts["P12"] == 0


def test_seed_name_command_and_manual_name(home, fake, seed_image, capsys) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    mmdd = _mmdd(ctx)
    auto = run(capsys, "seed", "name", "S01")
    assert (auto["name"], auto["source"], auto["previous"]) == (
        f"{DESC} {mmdd}",
        "auto",
        None,
    )
    manual = run(
        capsys, "seed", "name", "S01", "--set=小熊·野餐", "--by", "feishu:ou_x"
    )
    assert (manual["name"], manual["source"]) == (f"小熊野餐 {mmdd}", "manual")
    assert manual["previous"] == f"{DESC} {mmdd}"
    row = ctx.db.q1(
        "SELECT named_by, call_ids_json FROM seed_names WHERE seed_id = 'S01'"
    )
    assert (row["named_by"], json.loads(row["call_ids_json"])) == ("feishu:ou_x", [])
    assert fake.stage_counts["P12"] == 1

    err = run_error(capsys, "seed", "name", "S01", "--set=一二三四五六七八九十十一")
    assert "2–10 个字" in err["detail"]
    with pytest.raises(StageError, match="没有 seed"):
        seedmod.set_seed_name(ctx, "S09", "小熊")

    assert run(capsys, "seed", "list")["seeds"][0]["name"] == f"小熊野餐 {mmdd}"
    assert run(capsys, "seed", "show", "S01")["name"] == f"小熊野餐 {mmdd}"


def test_subject_label(home, fake, seed_image) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    assert subject_label(ctx, "S01") == "S01"
    seedmod.analyze(ctx, "S01")
    assert subject_label(ctx, "F01") == "F01"
    seedmod.set_seed_name(ctx, "S01", DESC)
    label = f"S01 {DESC} {_mmdd(ctx)}"
    assert subject_label(ctx, "S01") == task_label(ctx, "S01") == label
    assert subject_label(ctx, "F01") == f"{label} · F01"
    plan = planmod.plan_batch(ctx, "F01", limit=1)
    assert subject_label(ctx, plan["batch_id"]) == f"{label} · {plan['batch_id']}"
    # 不认识的编号、素材编号、空：原样
    assert subject_label(ctx, "F09") == "F09"
    assert subject_label(ctx, "IMG00001") == "IMG00001"
    assert subject_label(ctx, None) == ""


def test_names_show_in_views_and_cards(home, fake, seed_image, capsys) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    analyzed = seedmod.analyze(ctx, "S01")
    label = f"S01 {DESC} {_mmdd(ctx)}"

    summary = bg.summarize("analyze", {**analyzed, "seed_id": "S01"})
    card = notify.bg_card(ctx, {"kind": "analyze", "subject": "S01"}, summary, ok=True)
    assert card["header"]["title"]["content"] == f"母版拆解完成｜{label}"
    card = notify.bg_card(
        ctx, {"kind": "extract", "subject": "S01"}, {"model": "m"}, ok=True
    )
    assert card["header"]["title"]["content"] == f"图案已提取｜{label}"
    card = notify.bg_card(
        ctx, {"kind": "revise", "subject": "F01"}, {"error": "x"}, ok=False
    )
    assert card["header"]["title"]["content"] == f"系列修订失败｜{label} · F01"
    card = notify.bg_card(
        ctx, {"kind": "plan", "subject": "F01"}, {"family_id": "F01"}, ok=True
    )
    assert card["header"]["title"]["content"] == f"没有策划出新方向｜{label} · F01"

    plan = planmod.plan_batch(ctx, "F01", limit=1)
    batch_id = plan["batch_id"]
    card = notify.bg_card(
        ctx, {"kind": "plan", "subject": "F01"}, bg.summarize("plan", plan), ok=True
    )
    assert (
        card["header"]["title"]["content"] == f"裂变方向已策划好｜{label} · {batch_id}"
    )
    assert ui.directions_view(ctx, batch_id)["title"] == (
        f"挑选裂变方向｜{label} · {batch_id}"
    )
    accept = notify.bg_card(
        ctx, {"kind": "accept", "subject": "S01"}, {"approved": 1}, ok=True
    )
    assert accept["header"]["title"]["content"] == f"选款验收完成｜{label}"
    assert scenes.scenes_card(ctx, "S01", [])["header"]["title"]["content"] == (
        f"套图出完｜{label}"
    )
    assert scenes.parked_card(ctx, "S01", [])["header"]["title"]["content"] == (
        f"套图等人工｜{label}"
    )

    (item,) = ui.tasks_view(ctx)["items"]
    assert item["title"].startswith(f"{label} · F01 v1　")
    assert ui.history_view(ctx, "F01")["title"] == f"任务明细｜{label} · F01"
    (summary_task,) = tasks.summaries(ctx)
    assert summary_task["name"] == f"{DESC} {_mmdd(ctx)}"
    assert run(capsys, "tasks")["tasks"][0]["name"] == summary_task["name"]


def test_seed_without_family_shows_the_name(home, fake, seed_image) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.set_seed_name(ctx, "S01", DESC)
    (item,) = ui.tasks_view(ctx)["items"]
    assert item["title"].startswith(f"S01 {DESC} {_mmdd(ctx)}　")


def test_candidate_table_is_named_when_created(home, fake, seed_image, spawned) -> None:
    set_config(home, "task_name.enabled", True)
    ctx = _table_setup(home, seed_image)
    candidates.sync(ctx)
    want = f"S01 {DESC} {_mmdd(ctx)} 候选"
    (table,) = [t for t in fake.bitable_tables.values() if t["name"].endswith(" 候选")]
    assert table["name"] == want
    assert not [c for c in fake.bitable_calls if c[0] == "PATCH"]
    row = ctx.db.q1("SELECT table_name FROM seed_names WHERE seed_id = 'S01'")
    assert row["table_name"] == want
    status = candidates.status(ctx)
    assert status["tables"][0]["name"] == want and status["renames_pending"] == 0


def test_existing_candidate_table_is_renamed(home, fake, seed_image, spawned) -> None:
    ctx = _table_setup(home, seed_image)
    candidates.sync(ctx)
    table_id, table = next(
        (tid, t) for tid, t in fake.bitable_tables.items() if t["name"] == "S01 候选"
    )
    assert not candidates.has_work(ctx)
    # 之前拉起的同步（假进程，pid 是测试进程本身）算已结束，免得 kick 以为还有同步在跑
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET status = 'done' WHERE status IN ('starting', 'running')"
        )
    kicks = len(spawned)

    # 补名字：拉起一次同步，表名跟着改
    seedmod.set_seed_name(ctx, "S01", DESC)
    assert [argv[2:] for argv in spawned[kicks:]] == [["table", "sync"]]
    assert candidates.has_work(ctx)
    out = candidates.sync(ctx)
    want = f"S01 {DESC} {_mmdd(ctx)} 候选"
    assert out["renamed"] == 1 and table["name"] == want
    app_token = fake.bitable_apps[0]["app_token"]
    assert ("PATCH", f"/bitable/v1/apps/{app_token}/tables/{table_id}") in (
        fake.bitable_calls
    )
    assert not candidates.has_work(ctx) and candidates.sync(ctx)["renamed"] == 0
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM events WHERE event = 'candidate_table_renamed'"
        )
        == 1
    )

    # 改名失败记次数，满上限不再试；再改一次名字就重新开始
    fake.bitable_fail.add(("PATCH", "/tables/"))
    seedmod.set_seed_name(ctx, "S01", "海洋鲸鱼卡通")
    for _ in range(feishu_table.MAX_ATTEMPTS):
        failed = candidates.sync(ctx)
        assert failed["renamed"] == 0 and failed["failed"] == 1
    row = ctx.db.q1(
        "SELECT table_name, table_attempts, table_error FROM seed_names WHERE seed_id = 'S01'"
    )
    assert (
        row["table_attempts"] == feishu_table.MAX_ATTEMPTS and row["table_name"] == want
    )
    assert "fake failure" in row["table_error"]
    assert not candidates.has_work(ctx)
    assert candidates.status(ctx)["tables"][0]["rename_error"] == row["table_error"]

    fake.bitable_fail.clear()
    seedmod.set_seed_name(ctx, "S01", "海洋鲸鱼卡通")
    assert candidates.sync(ctx)["renamed"] == 1
    assert table["name"] == f"S01 海洋鲸鱼卡通 {_mmdd(ctx)} 候选"


def test_purge_removes_the_name(home, fake, seed_image) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    seedmod.set_seed_name(ctx, "S01", DESC)
    maintenance.purge_old(ctx, seeds=["S01"], by="tester")
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_names") == 0


def test_bootstrap_still_works_with_naming(home, fake, seed_image) -> None:
    ctx = _enable(home)
    plan = bootstrap(ctx, seed_image, limit=1)
    assert plan["accepted"] and task_label(ctx, "S01").startswith(f"S01 {DESC} ")
