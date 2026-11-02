"""工作台流程：后台任务登记、下一步提示、最近动态、已选款、提案自动修订、网关定时巡检。"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import (
    SLOTS_PER_JOB,
    card_buttons,
    card_text,
    confirm_all,
    reopen,
    set_config,
)
from podflow import bg, cli, notify, review, ui
from podflow.db import open_review
from podflow.stages import seed as seedmod
from podflow.stages.plan import asset_label_of
from podflow.util import dumps
from podflow.worker import Worker
from test_pipeline import bootstrap
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


def _approved_family(ctx, seed_image) -> str:
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    family_id = seedmod.analyze(ctx, seed["seed_id"])["steps"][-1]["family_id"]
    seedmod.decide_family(ctx, family_id, approve=True, by="tester")
    return family_id


def _task(ctx, task_id: str) -> dict:
    return dict(ctx.db.q1("SELECT * FROM bg_tasks WHERE task_id = ?", (task_id,)))


def _actions(view: dict) -> list[dict]:
    return [a for step in view["next_steps"] for a in step["actions"]]


# ---------------------------------------------------------------- 后台任务


def test_plan_detach_runs_in_background_and_sends_card(
    ctx, fake, seed_image, spawned, capsys, monkeypatch
) -> None:
    _enable_feishu(ctx.home)
    ctx = reopen(ctx.home)
    family_id = _approved_family(ctx, seed_image)
    out = run(
        capsys, "plan", "--family", family_id, "--limit", "2", "--detach", "--notify"
    )
    assert out["detached"] and out["task_id"].startswith("BG")
    call = spawned[0]
    assert call["argv"][2:] == [
        "plan",
        "--family",
        family_id,
        "--limit",
        "2",
        "--notify",
    ]
    assert call["start_new_session"] is True
    assert call["env"][bg.ENV_KEY] == out["task_id"]
    assert _task(ctx, out["task_id"])["status"] == "starting"

    view = ui.dashboard(ctx)
    assert any(line.startswith(f"{family_id} 裂变策划中") for line in view["progress"])
    # 正在策划的系列不再给策划按钮，也不能重复提交
    assert not [a for a in _actions(view) if a["action"] == "pod_plan"]
    assert cli.main(["plan", "--family", family_id, "--detach"]) == 2
    assert "正在策划" in json.loads(capsys.readouterr().out)["detail"]
    assert cli.main(["plan", "--family", family_id, "--limit", "0", "--detach"]) == 2
    assert "limit 至少是 1" in json.loads(capsys.readouterr().out)["detail"]

    result = _run_child(capsys, monkeypatch, call)
    assert len(result["accepted"]) == 2
    task = _task(ctx, out["task_id"])
    assert task["status"] == "done" and task["pid"] == os.getpid()
    summary = json.loads(task["result_json"])
    assert summary["accepted"] == 2 and (summary["small"], summary["big"]) == (0, 2)
    assert summary["notified"]["sent"] is True
    card = fake.feishu_cards[-1]
    batch_id = summary["batch_id"]
    assert card["header"]["title"]["content"] == f"裂变方向已策划好｜{batch_id}"
    assert [b["value"] for b in card_buttons(card)] == [
        {"action": "pod_plan_view", "batch": batch_id},
        {"action": "pod_dashboard"},
    ]
    # 卡片上按小变化 / 大变化分段，逐行列出方向标题和改动的方位
    text = card_text(card)
    assert "（小变化 0 · 大变化 2），等你挑选" in text
    assert "**大变化**\n1. 主题方案1（主题叙事）\n2. 构图类型方案1（整体构图）" in text
    # 有一批待挑选时不能再提交策划
    assert cli.main(["plan", "--family", family_id, "--detach"]) == 2
    assert "待挑选" in json.loads(capsys.readouterr().out)["detail"]


def test_failed_background_task_sends_red_card(
    ctx, fake, spawned, capsys, monkeypatch
) -> None:
    _enable_feishu(ctx.home)
    ctx = reopen(ctx.home)
    task = bg.spawn(
        ctx,
        ["family", "revise", "F09", "--note", "x"],
        kind="revise",
        subject="F09",
        notify=True,
    )
    monkeypatch.setenv(bg.ENV_KEY, task["task_id"])
    assert cli.main(["family", "revise", "F09", "--note", "x"]) == 2
    capsys.readouterr()
    row = _task(ctx, task["task_id"])
    assert row["status"] == "failed" and json.loads(row["result_json"])["error"]
    card = fake.feishu_cards[-1]
    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == "系列修订失败｜F09"
    assert row["log_path"] in card_text(card)


def test_dead_process_is_marked_failed(ctx) -> None:
    # 超过 pid_max 的 pid 不可能存在，等同于进程已退出
    dead_pid = int(Path("/proc/sys/kernel/pid_max").read_text()) + 1
    with ctx.db.tx() as conn:
        for task_id, pid, started in (
            ("BG0001", dead_pid, "2026-01-01T00:00:00+00:00"),
            ("BG0002", None, dt.datetime.now(dt.UTC).isoformat(timespec="seconds")),
        ):
            conn.execute(
                "INSERT INTO bg_tasks(task_id, kind, subject, pid, status, notify, started_at, updated_at)"
                " VALUES(?, 'worker', NULL, ?, 'running', 0, ?, ?)",
                (task_id, pid, started, started),
            )
    # 刚登记、还没写 pid 的任务在宽限期内算活着
    assert [t["task_id"] for t in bg.running(ctx)] == ["BG0002"]
    dead = _task(ctx, "BG0001")
    assert dead["status"] == "failed"
    assert json.loads(dead["result_json"])["error"] == "exited"
    assert "后台出图失败" in [i["text"] for i in ui.timeline(ctx)["items"]]


def test_seed_analyze_records_steps_for_the_task(
    ctx, fake, seed_image, spawned, monkeypatch, capsys
) -> None:
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    steps: list[str] = []
    real_step = bg.step

    def record(ctx_, label):
        steps.append(label)
        real_step(ctx_, label)

    monkeypatch.setattr(bg, "step", record)
    out = run(capsys, "seed", "analyze", seed["seed_id"], "--detach", "--notify")
    _run_child(capsys, monkeypatch, spawned[0])
    # 拆解完接着策划方向：进度按 5 步显示，策划再按组数写到哪了
    assert list(dict.fromkeys(s.split()[0] for s in steps)) == [
        "S01",
        "S02",
        "S03",
        "S04",
        "S05",
    ]
    assert steps[0].endswith("（1/5）")
    # 默认假拆解：3 个单字段大改，一块（plan.slots_per_call 默认 4）
    assert steps[-2:] == [f"S05 裂变策划（5/5）：{n}/1 组" for n in range(2)]
    summary = json.loads(_task(ctx, out["task_id"])["result_json"])
    assert summary["family_id"] and summary["family_version"] == 1
    assert (
        summary["plan"]["batch_id"] == "B001"
        and len(summary["plan"]["big_titles"]) == 3
    )


# ---------------------------------------------------------------- 下一步


def test_next_steps_follow_pipeline_state(ctx, fake, seed_image, spawned) -> None:
    view = ui.dashboard(ctx)
    assert view["progress"] == [] and view["schedule_enabled"] is True
    assert _actions(view) == [
        {"action": "pod_enter_chat", "label": "进入 POD 对话", "style": "primary"}
    ]

    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    family_id = seedmod.analyze(ctx, seed["seed_id"])["steps"][-1]["family_id"]
    # 授权未知不再开待审；系列待认可时可以直接批准，也可以先策划方向（确认方向时一并认可）
    view = ui.dashboard(ctx)
    assert [(a["action"], a.get("type"), a.get("limit")) for a in _actions(view)] == [
        ("pod_reviews", "family", None),
        ("pod_plan", None, None),
    ]
    plan_step = next(
        s for s in view["next_steps"] if s["actions"][0]["action"] == "pod_plan"
    )
    assert plan_step["text"].endswith("（大变化 3）")

    seedmod.decide_family(ctx, family_id, approve=True, by="tester")
    view = ui.dashboard(ctx)
    plans = [a for a in _actions(view) if a["action"] == "pod_plan"]
    assert [(a["family_id"], a.get("limit"), a["label"]) for a in plans] == [
        (family_id, None, "策划 3 个方向"),
    ]
    # 没有排队的任务时不提连续运行
    assert "pod_trial_start" not in [a["action"] for a in _actions(view)]

    from podflow.stages import plan as planmod

    plan = planmod.plan_batch(ctx, family_id, limit=1)
    view = ui.dashboard(ctx)
    assert {
        "action": "pod_plan_view",
        "label": "去挑选",
        "style": "primary",
        "batch": "B001",
    } in _actions(view)
    actions = [a["action"] for a in _actions(view)]
    assert "pod_worker" not in actions and "pod_plan" not in actions
    assert "B001（F01）：1 个方向待挑选，已选 0 个" in view["progress"]

    confirm_all(ctx, plan)
    view = ui.dashboard(ctx)
    actions = [a["action"] for a in _actions(view)]
    assert "pod_worker" in actions and "pod_plan" not in actions
    assert "pod_trial_start" in actions
    assert any(line.startswith("B001（") for line in view["progress"])

    # 有 worker 在跑时不再给出图按钮，进度里显示它
    bg.spawn(ctx, ["worker"], kind="worker")
    view = ui.dashboard(ctx)
    assert "pod_worker" not in [a["action"] for a in _actions(view)]
    assert any(line.startswith("出图中") for line in view["progress"])

    cli.main(["pause", "--reason", "测试"])
    view = ui.dashboard(reopen(ctx.home))
    assert view["next_steps"][0]["actions"][0]["action"] == "pod_resume"
    assert "pod_trial_start" not in [a["action"] for a in _actions(view)]


def test_next_steps_point_to_asset_reviews(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    view = ui.dashboard(ctx)
    asset_step = next(s for s in view["next_steps"] if s["text"].startswith("待你处理"))
    assert f"选款 {SLOTS_PER_JOB}" in asset_step["text"]
    assert [a.get("type") for a in asset_step["actions"]] == ["asset_selection"]


# ---------------------------------------------------------------- 动态与已选款


def test_timeline_shows_readable_events_only(ctx, fake, seed_image, capsys) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    rid = review.list_reviews(ctx, types=["asset_selection"])[0]["review_id"]
    review.decide(ctx, rid, "approved", by="feishu:ou_x")
    run(capsys, "tick")
    texts = [i["text"] for i in ui.timeline(ctx)["items"]]
    assert any(t.startswith("选款 IMG") and t.endswith("：选中（飞书）") for t in texts)
    assert any("拆解完成" in t for t in texts)
    assert any(t.startswith("B001 策划：F01 提出 1 个裂变方向") for t in texts)
    assert "B001 选了 1 个方向出图（tester）" in texts
    assert any(t.startswith("出图一轮结束") for t in texts)
    events = {i["event"] for i in ui.timeline(ctx, limit=100)["items"]}
    # 内部流水和没派发任何东西的巡检不进动态
    assert not events & {
        "job_claimed",
        "asset_saved",
        "slot_updated",
        "worker_lease_acquired",
        "tick",
    }


def test_gallery_and_originals_only_cover_selected_assets(
    ctx, fake, seed_image, capsys
) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    items = review.list_reviews(ctx, types=["asset_selection"])
    chosen, other = items[0]["subject_id"], items[1]["subject_id"]
    review.decide(ctx, items[0]["review_id"], "approved", by="tester")
    review.decide(ctx, items[1]["review_id"], "rejected", by="tester")

    page = run(capsys, "ui", "gallery")
    assert page["total"] == 1 and [i["asset_id"] for i in page["items"]] == [chosen]
    assert Path(page["items"][0]["images"][0]["path"]).is_file()
    assert page["items"][0]["lines"][0].startswith("B001 · ")
    assert run(capsys, "ui", "gallery", "--batch", "B999")["total"] == 0

    out = run(capsys, "ui", "originals", "--asset", chosen, "--asset", other)
    assert [f["asset_id"] for f in out["files"]] == [chosen]
    original = Path(out["files"][0]["path"])
    assert original.is_absolute() and original.is_file()
    # 发出去的文件名是「编号_方向短语」，存储里的文件名不变
    assert (
        out["files"][0]["filename"] == f"{asset_label_of(ctx, chosen)}{original.suffix}"
    )
    assert out["files"][0]["filename"].startswith(f"{chosen}_")
    assert original.name == f"{chosen}{original.suffix}"
    assert out["refused"] == [
        {
            "asset_id": other,
            "label": asset_label_of(ctx, other),
            "reason": "不是已选中的款",
        }
    ]


# ---------------------------------------------------------------- 提案自动修订


def _open_proposal(ctx, changes: list[dict]) -> str:
    with ctx.db.tx() as conn:
        open_review(
            conn,
            "proposal",
            "B001",
            "反馈提出了方向调整",
            {"proposed_family_changes": changes, "next_experiments": []},
            batch_id="B001",
        )
    return review.list_reviews(ctx, types=["proposal"])[-1]["review_id"]


def test_approving_proposal_starts_family_revision(
    ctx, fake, seed_image, spawned
) -> None:
    bootstrap(ctx, seed_image, limit=1)
    rid = _open_proposal(
        ctx,
        [
            {
                "family_id": "F01",
                "change": "扩展到配色与密度方向",
                "rationale": "只测了 3 个轴",
            },
            {"family_id": "F01", "change": "纯技法变化降为验证方向"},
        ],
    )
    result = review.decide(ctx, rid, "approved", by="feishu:ou_x")
    assert result["effect"]["family_id"] == "F01"
    task = _task(ctx, result["effect"]["revise_started"])
    assert task["kind"] == "revise" and task["subject"] == "F01" and task["notify"] == 1
    argv = spawned[0]["argv"][2:]
    assert argv[:4] == ["family", "revise", "F01", "--note"]
    assert "1. 扩展到配色与密度方向（依据：只测了 3 个轴）" in argv[4]
    assert "2. 纯技法变化降为验证方向" in argv[4]
    assert "采纳后会自动按提案修订系列" in ui.HINTS["proposal"]

    # 提案里没有可执行的修改：只记录，不拉起
    rid = _open_proposal(ctx, [])
    assert (
        "已记录" in review.decide(ctx, rid, "approved", by="tester")["effect"]["note"]
    )
    rid = _open_proposal(ctx, [{"family_id": "F01", "change": "x"}])
    assert review.decide(ctx, rid, "rejected", by="tester")["effect"] == {
        "note": "已记录"
    }
    assert len(spawned) == 1


# ---------------------------------------------------------------- 网关定时巡检


def test_auto_tick_runs_when_due_and_sends_daily_once(
    home, fake, capsys, monkeypatch
) -> None:
    _enable_feishu(home)
    set_config(
        home,
        "schedule",
        {"enabled": True, "tick_interval_minutes": 10, "daily_report_at": "09:00"},
    )
    # 本地（Asia/Shanghai）12:00，已过日报时刻
    clock = {"now": dt.datetime(2026, 10, 10, 4, 0, tzinfo=dt.UTC)}
    monkeypatch.setattr(cli, "utcnow", lambda: clock["now"])
    first = run(capsys, "tick", "--auto")
    assert first["skipped"] is False and first["decision"] == "stop"
    assert first["daily"]["sent"] is True
    assert fake.feishu_cards[-1]["header"]["title"]["content"].startswith("POD 日报｜")

    again = run(capsys, "tick", "--auto")
    assert again == {"skipped": True}
    ctx = reopen(home)
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'tick'") == 1

    clock["now"] += dt.timedelta(minutes=9)
    assert run(capsys, "tick", "--auto") == {"skipped": True}
    # 定时器一分钟一跳，差 30 秒以内也算到点
    clock["now"] += dt.timedelta(seconds=40)
    third = run(capsys, "tick", "--auto")
    assert third["skipped"] is False and "daily" not in third
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'tick'") == 2
    assert (
        len(
            [
                c
                for c in fake.feishu_cards
                if c["header"]["title"]["content"].startswith("POD 日报")
            ]
        )
        == 1
    )

    set_config(home, "schedule.enabled", False)
    assert run(capsys, "tick", "--auto")["skipped"] is True


def test_auto_tick_without_daily_report(home, fake, capsys, monkeypatch) -> None:
    set_config(home, "schedule", {"daily_report_at": ""})
    out = run(capsys, "tick", "--auto")
    assert out["skipped"] is False and "daily" not in out

    # 日报时刻之前不发
    set_config(home, "schedule", {"daily_report_at": "21:00"})
    monkeypatch.setattr(
        cli, "utcnow", lambda: dt.datetime(2026, 10, 10, 4, 0, tzinfo=dt.UTC)
    )
    out = run(capsys, "tick", "--auto")
    assert "daily" not in out


# ---------------------------------------------------------------- 完成卡片


def test_worker_and_analyze_cards_point_to_reviews(ctx) -> None:
    with ctx.db.tx() as conn:
        open_review(conn, "asset_selection", "IMG00001", "选款", {})
        open_review(conn, "asset_qc", "IMG00002", "质检待审", {})
    worker = notify.bg_card(
        ctx,
        {"kind": "worker", "subject": None},
        {"jobs": 2, "stop_reason": "queue_empty", "batch_reports": ["B001"]},
        ok=True,
    )
    text = card_text(worker)
    assert "处理了 2 个任务；队列已空" in text and "选款 1、质检待审 1" in text
    assert "疑似重复" not in text
    assert "B001" in text
    assert [b["value"]["action"] for b in card_buttons(worker)] == [
        "pod_reviews",
        "pod_dashboard",
    ]

    analyzed = notify.bg_card(
        ctx,
        {"kind": "analyze", "subject": "S01"},
        {"family_id": "F01", "family_version": 1},
        ok=True,
    )
    assert analyzed["header"]["title"]["content"] == "母版拆解完成｜S01"
    assert card_buttons(analyzed)[0]["value"] == {
        "action": "pod_reviews",
        "type": "family",
    }

    stopped = notify.bg_card(
        ctx,
        {"kind": "analyze", "subject": "S02"},
        {"status": "restricted", "stopped_at": "S01"},
        ok=True,
    )
    assert stopped["header"]["template"] == "orange" and "S01" in card_text(stopped)

    planned = notify.bg_card(
        ctx,
        {"kind": "plan", "subject": "F01"},
        {
            "family_id": "F01",
            "accepted": 0,
            "dropped": {"duplicate_plan": 2},
            "exhausted": True,
        },
        ok=True,
    )
    assert "与历史计划重复 2" in card_text(planned) and "探索空间已耗尽" in card_text(
        planned
    )
    # 没策划出方向：去任务明细看做过哪些（没耗尽时还能写要求再策划）
    assert [b["value"] for b in card_buttons(planned)] == [
        {"action": "pod_history", "family": "F01"},
        {"action": "pod_dashboard"},
    ]
    assert dumps(planned)
