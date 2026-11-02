"""方向自动确认（batch.auto_confirm_directions）：人发起的每轮策划一出候选就全部确认、开始出图，不等人挑。"""

from __future__ import annotations

import pytest

from conftest import card_buttons, card_text, reopen, set_config, use_small
from podflow import bg, directions, doctor, notify, state, ui
from podflow.config import ConfigError
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.stages.common import StageError
from podflow.util import loads
from podflow.worker import Worker
from test_directions import spawned  # noqa: F401
from test_ui import run

DOCTOR_CHECK = "裂变方向等人挑选（batch.auto_confirm_directions 关着）"


def _auto(home) -> None:
    set_config(home, "batch.auto_confirm_directions", True)


def _open_types(ctx) -> list[str]:
    return [
        r["subject_type"]
        for r in ctx.db.q(
            "SELECT subject_type FROM reviews WHERE status = 'open' ORDER BY review_id"
        )
    ]


def _workers(calls: list[dict]) -> list[list[str]]:
    """拦下的后台出图进程的参数（去掉解释器和 podctl 路径）。"""
    return [c["argv"][2:] for c in calls if c["argv"][2] == "worker"]


def _card(ctx, kind: str, subject: str, out: dict) -> dict:
    return notify.bg_card(
        ctx, {"kind": kind, "subject": subject}, bg.summarize(kind, out), ok=True
    )


def _title(card: dict) -> str:
    return card["header"]["title"]["content"]


def test_off_by_default_still_waits_for_picks(home, fake, seed_image, capsys) -> None:
    run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    out = run(capsys, "seed", "analyze", "S01")
    assert out["plan"]["status"] == "proposed"
    assert not {"auto_confirm", "auto_confirm_error"} & set(out["plan"])
    ctx = reopen(home)
    assert _open_types(ctx) == ["plan_selection"]
    card = _card(ctx, "analyze", "S01", out)
    assert _title(card) == "母版拆解完成，方向已策划好｜S01"
    assert "等你挑选" in card_text(card)
    assert card_buttons(card)[0]["value"] == {
        "action": "pod_plan_view",
        "batch": "B001",
    }
    assert DOCTOR_CHECK not in {c["check"] for c in doctor.run(ctx)["checks"]}


def test_analyze_confirms_every_direction_and_starts_drawing(
    home,
    fake,
    seed_image,
    capsys,
    spawned,  # noqa: F811
) -> None:
    _auto(home)
    use_small(fake)
    run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    out = run(capsys, "seed", "analyze", "S01")
    plan = out["plan"]
    every = [a["variant_id"] for a in plan["accepted"]]
    confirmed = plan["auto_confirm"]
    # 小变化 3 个（只做图生图，6 张）+ 单字段大改 1 个 + 三处组合 4 个（各 9 张）
    assert confirmed["picked"] == every and len(every) == 8
    kinds = {a["variant_id"]: a["kind"] for a in plan["accepted"]}
    assert confirmed["images_per_direction"] == {
        v: 6 if kinds[v] == "small" else 9 for v in every
    }
    assert confirmed["images"] == 3 * 6 + 5 * 9 and confirmed["rejected"] == []
    assert confirmed["family_approved"] is True and "backlog" not in confirmed

    ctx = reopen(home)
    fam = ctx.db.q1("SELECT status, approved_by FROM families WHERE family_id = 'F01'")
    assert fam["status"] == "approved" and fam["approved_by"] == directions.AUTO_BY
    batch = ctx.db.q1("SELECT status, size, plan_json FROM batches")
    assert batch["status"] == "planned" and batch["size"] == 8
    assert loads(batch["plan_json"])["selection"]["by"] == directions.AUTO_BY
    # 挑选待审关掉，系列规则待审早已并进去：没有要人点的
    assert _open_types(ctx) == []
    assert ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'ready'") == 8
    # 后台拉起只领确认过的方向（和修改）的 worker，会一直出到出完；之后新确认的批次也由它接着领
    assert _workers(spawned) == [["worker", "--source", "directions"]]
    assert len(confirmed["jobs"]) == 8
    assert confirmed["directions_started"].startswith("BG")

    card = _card(ctx, "analyze", "S01", out)
    text = card_text(card)
    assert _title(card) == "母版拆解完成，已开始出图｜S01"
    assert "已随方向自动认可" in text and "已全部自动确认" in text
    assert "共 **63** 张图：后台已开始出图" in text
    assert "「B001 停掉 3、5」" in text and "在任务明细里看全" not in text
    buttons = card_buttons(card)
    assert buttons[0]["value"] == {"action": "pod_history", "family": "F01"}
    assert all(b["value"].get("action") != "pod_plan_view" for b in buttons)
    # 动态与挑选卡写「自动」
    assert "B001 选了 8 个方向出图（自动）" in [
        i["text"] for i in ui.timeline(ctx)["items"]
    ]
    view = ui.directions_view(ctx, "B001")
    assert view["status_line"] == "已确认：选了 8 个方向出图（自动）"


def test_replan_revise_and_continue_also_confirm(
    home,
    fake,
    seed_image,
    capsys,
    spawned,  # noqa: F811
) -> None:
    run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    run(capsys, "seed", "analyze", "S01")
    _auto(home)
    ctx = reopen(home)
    # 开关打开前策划的一批不追认：照旧待挑选
    assert ctx.db.q1("SELECT status FROM batches")["status"] == "proposed"

    # 换一批：后台拉起的就是 podctl plan（这里直接跑那条命令）
    directions.reject(ctx, "B001", by="tester", replan=True)
    assert spawned[-1]["argv"][2:] == ["plan", "--family", "F01"]
    replanned = run(capsys, "plan", "--family", "F01")
    assert replanned["batch_id"] == "B002"
    assert replanned["auto_confirm"]["family_approved"] is True
    assert replanned["auto_confirm"]["directions_started"].startswith("BG")

    # 修订系列：新版本策划完同样全部确认、一并认可
    revised = run(capsys, "family", "revise", "F01", "--note", "中间是小象不是考拉")
    assert revised["family_version"] == 2
    assert revised["plan"]["auto_confirm"]["family_approved"] is True

    # 继续裂变（任务列表 / POD 对话的 plan --family）：前一轮的出图还在跑，排在后面
    again = run(capsys, "plan", "--family", "F01", "--hint=往海洋动物方向")
    confirmed = again["auto_confirm"]
    assert confirmed["picked"] == [a["variant_id"] for a in again["accepted"]]
    assert confirmed["directions_queued"] and "directions_started" not in confirmed

    ctx = reopen(home)
    statuses = {
        r["batch_id"]: r["status"]
        for r in ctx.db.q("SELECT batch_id, status FROM batches")
    }
    assert statuses == {
        "B001": "rejected",
        "B002": "planned",
        "B003": "planned",
        "B004": "planned",
    }
    assert _open_types(ctx) == []
    assert len(_workers(spawned)) == 1

    card = _card(ctx, "plan", "F01", again)
    text = card_text(card)
    assert _title(card) == "裂变方向已自动确认，开始出图｜B004"
    assert "这一轮的要求：往海洋动物方向" in text
    assert "已排进正在出图的队列，有空的工位就接着出" in text
    assert card_buttons(card)[0]["value"] == {"action": "pod_history", "family": "F01"}
    revise_card = _card(ctx, "revise", "F01", revised)
    assert _title(revise_card) == "系列已修订，已开始出图｜F01"


def test_paused_or_backlogged_rounds_say_when_drawing_resumes(
    home,
    fake,
    seed_image,
    capsys,
    spawned,  # noqa: F811
) -> None:
    _auto(home)
    run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    ctx = reopen(home)
    # 已经有 worker 在出图：只排队，不另起一个抢租约
    assert state.acquire_worker_lease(ctx.db, "other-worker", lease_seconds=120)
    out = run(capsys, "seed", "analyze", "S01")
    confirmed = out["plan"]["auto_confirm"]
    assert confirmed["directions_queued"] == confirmed["jobs"]
    assert _workers(spawned) == []
    assert "已排进正在出图的队列" in card_text(
        _card(reopen(home), "analyze", "S01", out)
    )

    state.release_worker_lease(reopen(home).db, "other-worker")
    run(capsys, "pause", "--reason", "测试")
    set_config(home, "batch.review_backlog_limit", 0)
    again = run(capsys, "plan", "--family", "F01")
    confirmed = again["auto_confirm"]
    assert confirmed["reason"] == "流水线已暂停"
    assert confirmed["backlog"] == {"count": 0, "limit": 0}
    assert _workers(spawned) == []
    text = card_text(_card(reopen(home), "plan", "F01", again))
    assert "流水线已暂停，恢复后接着出" in text
    assert "待审积压 0 已到上限 0：新方向先排着" in text


@pytest.mark.parametrize(
    "error", [StageError("测试：确认失败"), ConfigError("测试：fanout 写错了")]
)
def test_failed_auto_confirm_falls_back_to_picking(
    home, fake, seed_image, capsys, monkeypatch, error
) -> None:
    _auto(home)

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(directions, "confirm", boom)
    run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    out = run(capsys, "seed", "analyze", "S01")
    assert out["plan"]["auto_confirm_error"] == str(error)
    assert "auto_confirm" not in out["plan"]
    ctx = reopen(home)
    assert ctx.db.q1("SELECT status FROM batches")["status"] == "proposed"
    assert _open_types(ctx) == ["plan_selection"]
    card = _card(ctx, "analyze", "S01", out)
    assert _title(card) == "母版拆解完成，方向已策划好｜S01"
    assert f"自动确认没成功（{error}）" in card_text(card)
    assert card_buttons(card)[0]["value"] == {
        "action": "pod_plan_view",
        "batch": "B001",
    }


def test_worker_top_up_plan_still_waits_for_picks(ctx, fake, seed_image) -> None:
    """worker 的补充规划（batch.auto_plan）不自动确认，否则会「策划 → 出图 → 再策划」停不下来。"""
    _auto(ctx.home)
    set_config(ctx.home, "batch.auto_plan", True)
    ctx = reopen(ctx.home)
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试")
    seedmod.analyze(ctx, seed["seed_id"])
    out = Worker(ctx, max_minutes=5).run()
    assert [p["batch_id"] for p in out["planned_batches"]] == ["B001"]
    assert ctx.db.q1("SELECT status FROM batches")["status"] == "proposed"
    assert _open_types(ctx) == ["plan_selection"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 0


def test_worker_that_found_another_running_sends_no_card(ctx) -> None:
    task = {"kind": "worker", "subject": None}
    busy = bg.summarize("worker", {"status": "busy", "worker": {"owner": "x"}})
    assert busy["busy"] is True and busy["jobs"] == 0
    assert notify.bg_card(ctx, task, busy, ok=True) is None
    assert notify.bg_finished(ctx, task, busy, ok=True) == {
        "sent": False,
        "reason": "已有出图在跑，排队的任务由它接着领",
    }
    # 正常跑完的一轮照旧发「出图一轮结束」
    done = bg.summarize(
        "worker", {"status": "done", "jobs": [], "stop_reason": "queue_empty"}
    )
    assert done["busy"] is False
    assert "出图一轮结束" in _title(notify.bg_card(ctx, task, done, ok=True))


def test_plan_buttons_say_drawing_starts(ctx, fake, seed_image) -> None:
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试")
    seedmod.analyze(ctx, seed["seed_id"])

    def plan_step(c) -> tuple[str, list[str]]:
        step = next(
            s
            for s in ui.dashboard(c)["next_steps"]
            if any(a["action"] == "pod_plan" for a in s["actions"])
        )
        return step["text"], [
            a["label"] for a in step["actions"] if a["action"] == "pod_plan"
        ]

    text, labels = plan_step(ctx)
    assert "给你挑" in text and labels == ["策划 3 个方向"]
    _auto(ctx.home)
    text, labels = plan_step(reopen(ctx.home))
    assert "策划完自动全部出图" in text and labels == ["策划 3 个方向并出图"]
    assert (
        planmod.plan_button_label(None, again=True, auto=True)
        == "按可变字段再策划一轮并出图"
    )


def test_doctor_warns_when_on(home, fake) -> None:
    _auto(home)
    check = {c["check"]: c for c in doctor.run(reopen(home))["checks"]}[DOCTOR_CHECK]
    assert check["ok"] is False and check["level"] == "warn"
    # 默认每轮小变化 12 × 6 张 + 大变化 8 × 9 张
    assert "一轮默认约 144 张图" in check["detail"]
