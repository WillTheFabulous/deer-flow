"""精简通知（notify.style: brief）：每小时的待办提醒、每天几次的状态总结、人发起的事回一行、出错发红卡，
程序自己推进的（出图、套图、归档、巡检）不发卡。"""

from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import card_buttons, card_text, confirm_all, reopen, set_config
from podflow import (
    bg,
    candidates,
    digest,
    doctor,
    notify,
    packages,
    scenes,
    shelf,
)
from podflow.stages import plan as planmod
from podflow.stages.plan import asset_label_of
from podflow.util import iso, utcnow
from test_scenes import _sets, _setup
from test_ui import _enable_feishu, run

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    monkeypatch.setattr(scenes, "_sleep", lambda s: None)
    return calls


def _brief(home, **todo) -> None:
    set_config(home, "notify.style", "brief")
    for key, value in todo.items():
        set_config(home, f"notify.todo.{key}", value)


def _at(hour: int, minute: int = 0, *, days: int = 0) -> dt.datetime:
    """今天（北京时间）hour:minute 对应的 UTC 时刻；days 往后挪几天。"""
    local = utcnow().astimezone(TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) + dt.timedelta(days=days)
    return local.astimezone(dt.UTC)


def _titles(fake) -> list[str]:
    return [c["header"]["title"]["content"] for c in fake.feishu_cards]


def _lines(card: dict) -> list[str]:
    return [
        line
        for el in card["elements"]
        if el["tag"] == "markdown"
        for line in el["content"].split("\n")
        if line
    ]


# ---------------------------------------------------------------- 开关与回执


def test_full_is_the_default_and_doctor_says_so(ctx) -> None:
    assert digest.enabled(ctx) is False
    check = next(
        c for c in doctor.run(ctx)["checks"] if c["check"] == "通知方式（notify.style）"
    )
    assert check["ok"] and check["detail"].startswith("full：")


def test_user_actions_get_one_line_receipts(home, fake) -> None:
    _enable_feishu(home)
    _brief(home)
    ctx = reopen(home)
    analyze = digest.brief_bg_card(
        ctx,
        {"kind": "analyze", "subject": "S01"},
        {
            "seed_id": "S01",
            "family_id": "F01",
            "family_version": 1,
            "plan": {
                "batch_id": "B001",
                "family_id": "F01",
                "accepted": 40,
                "titles": ["1. 不该出现的方向标题"],
                "big_titles": ["2. 也不该出现"],
                "auto": {"picked": 40, "images": 112, "started": "BG0001"},
            },
        },
        ok=True,
    )
    assert analyze["header"]["title"]["content"] == "拆解完成｜S01"
    assert _lines(analyze) == ["40 个方向、约 112 张图，开始出图"]
    assert "方向标题" not in card_text(analyze)
    # 积压满了：一样一行，写明先排着
    queued = digest.brief_bg_card(
        ctx,
        {"kind": "plan", "subject": "F01"},
        {
            "batch_id": "B002",
            "family_id": "F01",
            "accepted": 20,
            "auto": {"picked": 20, "images": 56, "backlog": {"count": 1500}},
        },
        ok=True,
    )
    assert queued["header"]["title"]["content"] == "策划完成｜B002"
    assert _lines(queued) == [
        "20 个方向、约 56 张图，先排着：待审积压满了，选款处理一些后接着出"
    ]
    # 自动确认没成功：要人去挑，两行 + 「去挑选」
    pick = digest.brief_bg_card(
        ctx,
        {"kind": "plan", "subject": "F01"},
        {
            "batch_id": "B003",
            "family_id": "F01",
            "accepted": 8,
            "auto_error": "系列被驳回",
        },
        ok=True,
    )
    assert _lines(pick) == ["8 个方向等你挑选", "自动确认没成功：系列被驳回"]
    assert card_buttons(pick)[0]["value"] == {
        "action": "pod_plan_view",
        "batch": "B003",
    }
    empty = digest.brief_bg_card(
        ctx,
        {"kind": "plan", "subject": "F01"},
        {"batch_id": "B004", "family_id": "F01", "accepted": 0, "exhausted": True},
        ok=True,
    )
    assert empty["header"]["title"]["content"] == "没有策划出新方向｜F01"
    assert empty["header"]["template"] == "orange"
    assert card_buttons(empty)[0]["value"] == {"action": "pod_history", "family": "F01"}
    accept = digest.brief_bg_card(
        ctx,
        {"kind": "accept", "subject": "S01"},
        {
            "seed_id": "S01",
            "approved": 2,
            "rejected": 142,
            "fresh": 3,
            "scenes": {"enabled": True, "auto": True, "queued": 2},
            "backlog": {"before": 343, "after": 199, "limit": 1500},
            "table_url": "https://example.feishu.cn/base/x",
            "source": "table",
        },
        ok=True,
    )
    assert accept["header"]["title"]["content"] == "选款完成｜S01"
    assert _lines(accept) == ["选中 2 · 没选 142 · 3 张新图留到下次，2 款开始出套图"]
    assert card_buttons(accept)[0]["url"] == "https://example.feishu.cn/base/x"
    fix = digest.brief_bg_card(
        ctx, {"kind": "fix", "subject": "IMG00009"}, {"jobs": 1}, ok=True
    )
    assert fix["header"]["title"]["content"] == "修改还没完成｜IMG00009"
    # 多工位的 worker 改完一张就回过一行：后台任务结束时不再重复
    assert (
        digest.brief_bg_card(
            ctx,
            {"kind": "fix", "subject": "IMG00009"},
            {"jobs": 1, "fix_cards": ["IMG00009"], "fix_only": True},
            ok=True,
        )
        is None
    )
    # 失败：两行红卡，第二行写怎么办
    failed = digest.brief_bg_card(
        ctx,
        {"kind": "analyze", "subject": "S01", "task_id": "BG0009"},
        {"error": "StageError", "detail": "P02 三次都没给全字段"},
        ok=False,
    )
    assert failed["header"]["template"] == "red"
    assert failed["header"]["title"]["content"] == "母版拆解失败｜S01"
    assert _lines(failed) == [
        "StageError：P02 三次都没给全字段",
        "在 POD 对话里说「S01 重新拆解」，或问原因",
    ]
    # 发出去的就是这张；出图 worker 做完不发
    assert notify.bg_finished(
        ctx, {"kind": "accept", "subject": "S01"}, {"approved": 1}, ok=True
    )["sent"]
    quiet = notify.bg_finished(
        ctx,
        {"kind": "worker", "subject": None},
        {"jobs": 3, "batch_reports": ["B001"]},
        ok=True,
    )
    assert quiet == {"sent": False, "reason": digest.QUIET}
    # 工位改完一张图时的回执也是一行
    assert notify.fix_finished(ctx, "IMG00009")["sent"]
    assert _lines(fake.feishu_cards[-1]) == ["下一轮出图会接着做"]
    assert _titles(fake) == ["选款完成｜S01", "修改还没完成｜IMG00009"]


def test_auto_failures_are_deduped(home, fake) -> None:
    _enable_feishu(home)
    _brief(home)
    ctx = reopen(home)
    task = {"kind": "worker", "subject": None, "task_id": "BG0001"}
    boom = {"error": "RuntimeError", "detail": "租约丢失"}
    assert notify.bg_finished(ctx, task, boom, ok=False)["sent"]
    again = notify.bg_finished(ctx, {**task, "task_id": "BG0002"}, boom, ok=False)
    assert again["sent"] is False and "6 小时内发过" in again["reason"]
    other = {"error": "RuntimeError", "detail": "连不上 LiteLLM"}
    assert notify.bg_finished(ctx, task, other, ok=False)["sent"]
    # 人发起的失败不去重：每次都说
    mine = {"kind": "extract", "subject": "S01"}
    assert notify.bg_finished(ctx, mine, boom, ok=False)["sent"]
    assert notify.bg_finished(ctx, mine, boom, ok=False)["sent"]
    assert _titles(fake) == [
        "出图失败",
        "出图失败",
        "图案提取失败｜S01",
        "图案提取失败｜S01",
    ]


# ---------------------------------------------------------------- 静默与待办


def test_scenes_and_archive_cards_go_quiet(home, fake, seed_image, spawned) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=2)
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    assert [b["batch_id"] for b in out["packages"]["built"]] == ["B001"]
    # 没发「套图出完」卡，但照样记了已通知（切回 full 也不会补发一串旧卡）
    assert not any("套图" in t for t in _titles(fake))
    assert all(r["notified_at"] for r in _sets(ctx).values())
    assert {n["reason"] for n in out["notified"]} == {digest.QUIET}
    # 交付包打好了、还没标已下载：进待办「待下载」
    todo = digest.todo_items(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    assert [(d["seed_id"], d["batch_id"], d["items"]) for d in todo["download"]] == [
        ("S01", "B001", 2)
    ]
    assert todo["download"][0]["url"] == pkg["export_url"]
    view = digest.todo_view(ctx)
    card = digest.todo_card(view)
    assert f"[下载]({pkg['export_url']})" in card_text(card)
    assert {"action": "pod_downloaded", "seed": "S01", "batch": "B001"} in [
        b.get("value") for b in card_buttons(card)
    ]
    # 标了已下载就不再是待办
    packages.mark_downloaded(ctx, "S01", "B001", by="t", source="chat")
    assert digest.todo_items(ctx)["download"] == []
    # 归档卡、巡检提醒卡也不发
    assert shelf.announce(ctx, [{"seed_id": "S01", "label": "S01"}])["sent"] is False
    tick = notify.notify_tick(
        ctx,
        {
            "alerts": ["结果未知"],
            "decision": "needs_review",
            "reason": "r",
            "summary": "s",
        },
    )
    assert tick["sent"] is False
    assert not fake.feishu_cards


def test_parked_sets_wait_in_the_todo_and_failed_sets_go_red(
    home, fake, seed_image, spawned
) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_script = ["queued", "waiting_human"]
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    assert out["parked"] and not fake.feishu_cards
    (parked,) = digest.todo_items(ctx)["parked"]
    assert parked["asset_id"] == picked[0] and parked["step"] == "白底图等人工确认"
    assert "套图等人工 1 款" in "\n".join(digest.todo_view(ctx)["lines"])
    # mooya 那边没出成：一个任务一张两行红卡
    for job in fake.mooya_jobs.values():
        job.update(status="failed", frozen=True, no_images=True)
    scenes.sync(ctx)
    assert _sets(ctx)[picked[0]]["status"] == "failed"
    (card,) = fake.feishu_cards
    assert card["header"]["title"]["content"] == "套图没出成｜S01"
    assert card["header"]["template"] == "red"
    lines = _lines(card)
    assert len(lines) == 2 and asset_label_of(ctx, picked[0]) in lines[0]
    assert "出套图" in lines[1]
    assert digest.todo_items(ctx)["parked"] == []


def test_todo_lists_pending_picks_with_table_links(
    home, fake, seed_image, spawned
) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    pending = int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM reviews WHERE subject_type = 'asset_selection' AND status = 'open'"
        )
    )
    assert pending > 0
    (item,) = digest.todo_items(ctx)["pick"]
    assert item["seed_id"] == "S01" and item["pending"] == pending
    assert item["unfinished"] == 0 and item["url"] is None
    candidates.sync(ctx)
    (item,) = digest.todo_items(ctx)["pick"]
    assert item["url"] and item["url"].startswith("https://")
    view = digest.todo_view(ctx)
    assert view["lines"][:2] == [
        f"**待挑选 1 个任务 · {pending} 张**",
        f"- S01：{pending} 张（已出完） [候选表]({item['url']})",
    ]
    # 只提醒待下载时，待挑选不算
    assert digest.todo_items(ctx, kinds=["download"])["count"] == 0


# ---------------------------------------------------------------- 调度


def test_hourly_todo_reminder_follows_window_and_digest(
    home, fake, seed_image, spawned
) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    # 08:30 不在提醒时段：第一次打开只记下总结时刻，不发
    assert digest.run_schedule(ctx, _at(8, 30)) == {}
    assert ctx.db.get_meta(digest.META_DIGEST_SLOT).endswith("08:30 开始")
    out = digest.run_schedule(ctx, _at(9, 5))
    assert out["todo"]["sent"] and _titles(fake) == ["POD 待办｜09:05"]
    assert digest.run_schedule(ctx, _at(9, 40)) == {}
    # 10:00 是总结时刻：发总结，这一个钟头里不再单独提醒
    out = digest.run_schedule(ctx, _at(10, 0))
    assert out["digest"]["sent"] and out["digest"]["slot"].endswith(" 10:00")
    assert _titles(fake)[-1].startswith("POD 状态总结｜")
    assert digest.run_schedule(ctx, _at(10, 30)) == {}
    assert digest.run_schedule(ctx, _at(10, 59)) == {}
    assert digest.run_schedule(ctx, _at(11, 0))["todo"]["sent"]
    # 和上次一样就不发（skip_unchanged）
    set_config(home, "notify.todo.skip_unchanged", True)
    ctx = reopen(home)
    out = digest.run_schedule(ctx, _at(12, 0))
    assert out["todo"] == {"sent": False, "reason": "和上次提醒的一样"}
    # 只提醒待下载：待挑选不算，没有待办就不发
    set_config(home, "notify.todo.kinds", ["download"])
    ctx = reopen(home)
    assert digest.run_schedule(ctx, _at(13, 0))["todo"] == {
        "sent": False,
        "reason": "没有待办",
    }
    # 间隔改成 0：不单独提醒
    set_config(home, "notify.todo.interval_minutes", 0)
    set_config(home, "notify.todo.kinds", ["pick"])
    ctx = reopen(home)
    assert digest.run_schedule(ctx, _at(14, 30)) == {}
    sent = [t for t in _titles(fake) if t.startswith("POD 待办")]
    assert sent == ["POD 待办｜09:05", "POD 待办｜11:00"]


def test_digest_goes_out_once_per_slot_and_catches_up(
    home, fake, seed_image, spawned
) -> None:
    _brief(home, interval_minutes=0)
    ctx, picked = _setup(home, seed_image, approve=1)
    # 12:00 第一次打开：记下 10:00（不补发打开之前的时刻）
    assert digest.run_schedule(ctx, _at(12, 0)) == {}
    assert ctx.db.get_meta(digest.META_DIGEST_SLOT).endswith(" 10:00")
    assert digest.run_schedule(ctx, _at(14, 59)) == {}
    assert digest.run_schedule(ctx, _at(15, 0))["digest"]["sent"]
    assert digest.run_schedule(ctx, _at(15, 1)) == {}
    # 第二天错过了 10:00 和 15:00：16:20 只补发最近的一次
    assert digest.run_schedule(ctx, _at(9, 0, days=1)) == {}
    out = digest.run_schedule(ctx, _at(16, 20, days=1))
    assert out["digest"]["slot"].endswith(" 15:00")
    assert digest.run_schedule(ctx, _at(16, 21, days=1)) == {}
    assert len([t for t in _titles(fake) if t.startswith("POD 状态总结")]) == 2


def test_empty_digest_is_not_sent(home, fake) -> None:
    _enable_feishu(home)
    _brief(home)
    ctx = reopen(home)
    view = digest.digest_view(ctx)
    assert view["empty"] and view["lines"][0] == "**待你处理**：没有"
    assert digest.send_digest(ctx)["sent"] is False
    assert digest.send_todo(ctx)["reason"] == "没有待办"
    assert not fake.feishu_cards


# ---------------------------------------------------------------- 状态总结的内容


def test_digest_sections(home, fake, seed_image, spawned) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    # 再策划一轮、还没出图：进「待生成 / 进行中」
    family = ctx.db.scalar("SELECT family_id FROM families LIMIT 1")
    plan = planmod.plan_batch(ctx, family, limit=1)
    confirm_all(ctx, plan)
    view = digest.digest_view(ctx, _at(15, 0))
    text = "\n".join(view["lines"])
    assert view["title"].startswith("POD 状态总结｜") and not view["empty"]
    assert view["lines"][0] == "**待你处理**"
    assert "待挑选 1 个任务" in text
    assert "**待生成 / 进行中**" in text and "- S01：出图 1/2 个方向" in text
    assert "**今日已完成**" in text and "- 新任务 1 个：S01" in text
    images = int(ctx.db.scalar("SELECT COUNT(*) FROM assets"))
    assert f"- 出图 {images} 张 · 出完 1 个方向（1 批全部出完）" in text
    assert "- 选款：选中 1 张 · 没选 0 张" in text
    assert "**异常**" not in text
    # 暂停：写进异常
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "1")
        ctx.db.set_meta(conn, "pause_reason", "上线")
    view = digest.digest_view(ctx, _at(15, 0))
    assert view["alerts"] == ["- 流水线已暂停：上线（工作台点「恢复」）"]
    assert view["level"] == "attention"
    card = digest.digest_card(view)
    assert [b["text"]["content"] for b in card_buttons(card)] == [
        "任务列表",
        "打开工作台",
    ]


def test_today_counts_use_the_local_day(home, fake, seed_image, spawned) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    ids = [r[0] for r in ctx.db.q("SELECT asset_id FROM assets ORDER BY asset_id")]
    today = _at(10, 0)
    midnight = today.astimezone(TZ).replace(hour=0, minute=0)
    with ctx.db.tx() as conn:
        # 一张是本地昨天 23:59 出的，其余是今天 00:01
        conn.execute(
            "UPDATE assets SET created_at = ?",
            (iso(midnight + dt.timedelta(minutes=1)),),
        )
        conn.execute(
            "UPDATE assets SET created_at = ? WHERE asset_id = ?",
            (iso(midnight - dt.timedelta(minutes=1)), ids[0]),
        )
    counts = digest.today_counts(ctx, today)
    assert counts["images"] == len(ids) - 1
    assert counts["since"] == iso(midnight)


# ---------------------------------------------------------------- 命令、定时器与自检


def test_report_and_ui_commands(home, fake, seed_image, spawned, capsys) -> None:
    _brief(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    out = run(capsys, "report", "digest")
    assert out["title"].startswith("POD 状态总结｜") and "notified" not in out
    out = run(capsys, "report", "todo")
    assert out["title"].startswith("POD 待办｜") and out["pick"][0]["seed_id"] == "S01"
    # 工作台「日报」按钮、人设的「【日报】」都给状态总结
    assert run(capsys, "ui", "daily")["title"].startswith("POD 状态总结｜")
    out = run(capsys, "report", "daily", "--notify")
    assert out["notified"]["sent"] and _titles(fake)[-1].startswith("POD 状态总结｜")
    out = run(capsys, "report", "todo", "--notify")
    assert out["notified"]["sent"] and _titles(fake)[-1].startswith("POD 待办｜")
    # 定时器：每分钟一跳，巡检照常
    out = run(capsys, "tick", "--auto")
    assert out["skipped"] is False and "notify_error" not in out
    # 总结时刻写错了：巡检照跑、不发总结，原因写在输出里；doctor 报错
    set_config(home, "schedule.digest_at", ["25:00"])
    with reopen(home).db.tx() as conn:
        conn.execute("DELETE FROM meta WHERE key = 'last_auto_tick_at'")
    out = run(capsys, "tick", "--auto")
    assert out["skipped"] is False and "HH:MM" in out["notify_error"]
    check = next(
        c
        for c in doctor.run(reopen(home))["checks"]
        if c["check"] == "通知方式（notify.style）"
    )
    assert not check["ok"] and check["level"] == "error"


def test_full_style_keeps_the_daily_report(home, fake, capsys) -> None:
    _enable_feishu(home)
    assert run(capsys, "ui", "daily")["title"].startswith("POD 日报｜")
    out = run(capsys, "report", "daily", "--notify")
    assert out["notified"]["sent"] and len(fake.feishu_cards) == 1
    assert _titles(fake)[0].startswith("POD 日报｜")


def test_doctor_describes_the_brief_schedule(home, fake) -> None:
    _brief(home)
    check = next(
        c
        for c in doctor.run(reopen(home))["checks"]
        if c["check"] == "通知方式（notify.style）"
    )
    assert check["ok"] and check["detail"] == (
        "brief：状态总结 10:00 / 15:00 / 21:00；有待办时每 60 分钟提醒一次（09:00–21:00）"
    )
    set_config(home, "notify.style", "loud")
    check = next(
        c
        for c in doctor.run(reopen(home))["checks"]
        if c["check"] == "通知方式（notify.style）"
    )
    assert not check["ok"] and check["level"] == "warn"
    assert digest.enabled(reopen(home)) is False


def test_todo_settings_validation(home, fake) -> None:
    _brief(home)
    window = digest.todo_settings(reopen(home))["active_hours"]
    # 缺省 09:00–21:00：开始含、结束不含
    assert digest._in_window(_at(9, 0).astimezone(TZ), window)
    assert digest._in_window(_at(20, 59).astimezone(TZ), window)
    assert not digest._in_window(_at(21, 0).astimezone(TZ), window)
    assert not digest._in_window(_at(8, 59).astimezone(TZ), window)
    _brief(home, active_hours="22:00-07:00", kinds=["pick", "parked"])
    ctx = reopen(home)
    settings = digest.todo_settings(ctx)
    assert settings["kinds"] == ["pick", "parked"]
    # 跨午夜的时段
    assert digest._in_window(_at(23, 30).astimezone(TZ), settings["active_hours"])
    assert digest._in_window(_at(6, 59).astimezone(TZ), settings["active_hours"])
    assert not digest._in_window(_at(7, 0).astimezone(TZ), settings["active_hours"])
    set_config(home, "notify.todo.kinds", ["pick", "loud"])
    with pytest.raises(digest.ConfigError):
        digest.todo_settings(reopen(home))
    set_config(home, "notify.todo.kinds", ["pick"])
    set_config(home, "notify.todo.active_hours", "9点到21点")
    with pytest.raises(digest.ConfigError):
        digest.todo_settings(reopen(home))
    set_config(home, "notify.todo.active_hours", "")
    assert digest.todo_settings(reopen(home))["active_hours"] is None
    set_config(home, "schedule.digest_at", "21:00, 10:00")
    assert digest.digest_times(reopen(home)) == [(10, 0), (21, 0)]
