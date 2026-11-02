"""候选表表顶：两个按钮行「▶ 选中的先出套图」「▶ 选款完成」（勾上就处理，事件实时 / 巡检兜底），各批次的交付包行
（勾上 = 标已下载）、选中款的套图行；所有行按隐藏的「排序」列排，标了已下载的交付包连同套图行沉到最底。"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import card_text, make_png, reopen, set_config
from podflow import (
    bg,
    candidates,
    doctor,
    feishu_table,
    maintenance,
    notify,
    packages,
    shelf,
    tasks,
    ui,
)
from podflow.stages import plan as planmod
from podflow.util import iso, now_iso, utcnow
from podflow.worker import Worker
from test_candidates import (  # noqa: F401
    _age_selection,
    _cli,
    _open_items,
    _record,
    _setup,
    spawned,
)
from test_pipeline import confirm_all

ORDER = candidates.ORDER_FIELD


def _ready(home, seed_image, *, scenes_on: bool = False, views_on: bool = True):
    set_config(home, "table.control_row", True)
    # 排序靠默认视图（视图按「排序」列排）：测表顶的位置要打开
    set_config(home, "table.views", views_on)
    if scenes_on:
        set_config(home, "scenes.enabled", True)
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    return ctx


def _rows(fake, table_id: str) -> list[tuple[str, dict]]:
    """这张数据表的每一行（新增的顺序）：[(record_id, fields)]。"""
    return [
        (r, fields)
        for r, fields in fake.bitable_records.items()
        if fake.bitable_record_tables.get(r) == table_id
    ]


def _ordered(fake, table_id: str) -> list[tuple[str, dict]]:
    """按「排序」排好的样子（视图里看到的顺序）；每一行都要有排序值。"""
    rows = _rows(fake, table_id)
    assert all(isinstance(f.get(ORDER), int) for _, f in rows), [
        f.get("素材") for _, f in rows if not isinstance(f.get(ORDER), int)
    ]
    return sorted(rows, key=lambda item: item[1][ORDER])


def _table_id(ctx) -> str:
    return candidates.table_info(ctx, "S01")["table_id"]


def _action_id(ctx, kind: str, batch_id: str = "") -> str:
    return ctx.db.scalar(
        "SELECT record_id FROM candidate_actions WHERE seed_id = 'S01' AND kind = ? AND batch_id = ?",
        (kind, batch_id),
    )


def _tick(
    ctx, fake, kind: str, modifier: str | None = "ou_owner", batch_id: str = ""
) -> str:
    record_id = _action_id(ctx, kind, batch_id)
    fake.bitable_records[record_id]["选中"] = True
    if modifier:
        fake.bitable_modifiers[record_id] = modifier
    return record_id


def _runs(spawned, kind: str) -> list[list[str]]:  # noqa: F811
    return [argv[2:] for argv in spawned if argv[2:4] == ["table", kind]]


def _second_round(ctx, *, run: bool = True) -> None:
    """再策划一个方向（新批次）；run 时接着出图，新图进选款。"""
    family_id = ctx.db.scalar("SELECT family_id FROM families LIMIT 1")
    confirm_all(ctx, planmod.plan_batch(ctx, family_id, limit=1))
    if run:
        Worker(ctx, max_minutes=5).run()


def _finish_set(ctx, asset_id: str, *, url: str = "https://oss.example/S01.zip"):
    """把这款最近一套套图装成已出完：一张场景图、压缩包链接。"""
    set_id = ctx.db.scalar(
        "SELECT set_id FROM scene_sets WHERE asset_id = ? ORDER BY set_id DESC LIMIT 1",
        (asset_id,),
    )
    path = f"assets/scenes/{asset_id}/{set_id}/scene-01.png"
    (ctx.home / path).parent.mkdir(parents=True, exist_ok=True)
    (ctx.home / path).write_bytes(make_png(7, size=(64, 80)))
    expires = iso(utcnow() + dt.timedelta(days=90))
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO scene_images(set_id, image_id, role, idx, title, path, created_at, updated_at)"
            " VALUES(?, 'scene-01', 'scene', 1, '客厅', ?, ?, ?)",
            (set_id, path, now_iso(), now_iso()),
        )
        conn.execute(
            "UPDATE scene_sets SET status = 'done', expected = 1, actual = 1, export_url = ?,"
            " export_expires_at = ?, finished_at = ? WHERE set_id = ?",
            (url, expires, now_iso(), set_id),
        )
    return set_id


def _accept_one(ctx, fake) -> str:
    """勾一张、点选款完成；返回选中的款。"""
    _age_selection(ctx)
    picked = _open_items(ctx)[0]["subject_id"]
    fake.bitable_records[_record(ctx, picked)]["选中"] = True
    candidates.accept(ctx, "S01", by="feishu:ou_x")
    return picked


# ---------------------------------------------------------------- 表顶的位置


def test_buttons_sit_on_top_and_stay_put(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    table_id = _table_id(ctx)
    images = ctx.db.scalar("SELECT COUNT(*) FROM candidate_rows")
    rows = _ordered(fake, table_id)
    # 2026-10-30 起表顶只有「选款完成」一个按钮行（「选中的先出套图」不再建）
    assert len(rows) == images + 1
    accept_id, accept = rows[0]
    assert accept_id == _action_id(ctx, "accept") and _action_id(ctx, "dispatch") is None
    assert (accept["素材"], accept["方向"]) == (
        candidates.CONTROL_TEXT,
        candidates.CONTROL_HELP,
    )
    assert accept[ORDER] == candidates.ORDER_ACCEPT
    assert not accept.get("选中")
    assert all(f[ORDER] >= candidates.ORDER_CANDIDATE for _, f in rows[1:])
    assert not candidates.has_work(ctx)

    # 新一轮出图：按钮行原地不动（不删不建），新批次的图排在旧批次前面
    old = {r for r, _ in rows[1:]}
    _second_round(ctx)
    out = candidates.sync(ctx)
    assert (
        out["added"] > 0 and out["top"]["created"] == 0 and out["top"]["deleted"] == 0
    )
    rows = _ordered(fake, table_id)
    assert [r for r, _ in rows[:1]] == [accept_id]
    new = [r for r, _ in rows[1:] if r not in old]
    assert new and [r for r, _ in rows[1 : 1 + len(new)]] == new
    assert not candidates.has_work(ctx)


def test_old_dispatch_row_is_removed_but_still_works_until_then(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    table_id = _table_id(ctx)
    # 装成 10-30 前建的表：表顶还有「▶ 选中的先出套图」
    fake.bitable_records["recOldDispatch"] = {
        "素材": candidates.DISPATCH_TEXT,
        "方向": candidates.DISPATCH_HELP,
        ORDER: candidates.ORDER_DISPATCH,
    }
    fake.bitable_record_tables["recOldDispatch"] = table_id
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO candidate_actions(seed_id, kind, batch_id, table_id, record_id, created_at, updated_at)"
            " VALUES('S01', 'dispatch', '', ?, 'recOldDispatch', ?, ?)",
            (table_id, now_iso(), now_iso()),
        )
    # 删之前有人勾了：照旧提前出套图
    _tick(ctx, fake, "dispatch")
    out = candidates.control_event(
        ctx,
        file_token=feishu_table.base(ctx)["app_token"],
        table_id=table_id,
        record_ids=["recOldDispatch"],
        by="feishu:ou_evt",
    )
    assert out["started"].startswith("BG") and out["kind"] == "dispatch"
    # 下一次同步：这一行删掉
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")
    assert candidates.sync(ctx)["top"]["deleted"] == 1
    assert "recOldDispatch" not in fake.bitable_records and _action_id(ctx, "dispatch") is None


def test_order_values() -> None:
    # 批次越新越靠前；同一批次按方向、槽位，修改出的新版本跟在原图后面
    newer = candidates.order_candidate("B002", 0, 0)
    older = candidates.order_candidate("B001", 0, 0)
    assert newer < older
    assert (
        candidates.order_candidate("B001", 0, 0)
        < candidates.order_candidate("B001", 0, 0, version=2)
        < candidates.order_candidate("B001", 0, 1)
        < candidates.order_candidate("B001", 1, 0)
    )
    # 按钮 → 待下载的交付包 → 套图 → 候选行 → 标了已下载的交付包和它的套图（最底）
    top = [
        candidates.order_action("dispatch"),
        candidates.order_action("accept"),
        candidates.order_action("package", "B002"),
        candidates.order_action("package", "B001"),
        candidates.order_scene("B002", 0, done=False),
        candidates.order_scene("B001", 3, done=False),
        candidates.order_candidate("B002", 0, 0),
        candidates.order_candidate("B001", 99, 9, version=9),
        candidates.order_action("package", "B002", done=True),
        candidates.order_scene("B002", 5, done=True),
        candidates.order_action("package", "B001", done=True),
        candidates.order_scene("B001", 0, done=True),
    ]
    assert top == sorted(top) and len(set(top)) == len(top)
    # 都压在 Postgres INTEGER 以内
    assert max(top) < 2**31 - 1


def test_legacy_control_row_moves_to_top(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    table_id = _table_id(ctx)
    # 装成 2026-10-26 前的样子：只有表最底下的「▶ 选款完成」控制行（candidate_controls），各行没有排序
    accept_id = _action_id(ctx, "accept")
    fake.bitable_records[accept_id] = {
        "素材": "▶ 选款完成：勾上这一行的「选中」",
        "备注": "上次验收 10-23 15:50",
    }
    for _, fields in _rows(fake, table_id):
        fields.pop(ORDER, None)
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM candidate_actions")
        conn.execute("DELETE FROM candidate_orders")
        conn.execute(
            "INSERT INTO candidate_controls(seed_id, table_id, record_id, dirty, note, created_at, updated_at)"
            " VALUES('S01', ?, ?, 0, '上次验收 10-23 15:50', ?, ?)",
            (table_id, accept_id, now_iso(), now_iso()),
        )
    assert candidates.has_work(ctx)
    out = candidates.sync(ctx)
    # 控制行原地搬成按钮行（记录不变、备注照旧），各行补上排序；不再补建「选中的先出套图」
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_controls") == 0
    assert _action_id(ctx, "accept") == accept_id
    assert out["top"]["created"] == 0 and out["top"]["updated"] == 1
    assert out["orders"] >= 1
    rows = _ordered(fake, table_id)
    assert [f["素材"] for _, f in rows[:1]] == [candidates.CONTROL_TEXT]
    assert rows[0][0] == accept_id and rows[0][1]["备注"] == "上次验收 10-23 15:50"
    assert not candidates.has_work(ctx)


def test_rows_without_views_keep_working(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    # 关着默认视图：表不按「排序」排，也就不写排序；按钮行照样建、照样能点
    ctx = _ready(home, seed_image, views_on=False)
    table_id = _table_id(ctx)
    assert all(ORDER not in f for _, f in _rows(fake, table_id))
    assert _action_id(ctx, "accept") and _action_id(ctx, "dispatch") is None
    assert not candidates.has_work(ctx)


# ---------------------------------------------------------------- 事件与兜底


def test_event_on_ticked_button_starts_accept(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    app = feishu_table.base(ctx)["app_token"]
    table_id = _table_id(ctx)
    image_record = _record(ctx, _open_items(ctx)[0]["subject_id"])

    def event(**kw):
        args = {
            "file_token": app,
            "table_id": table_id,
            "record_ids": [_action_id(ctx, "accept")],
            "by": "feishu:ou_evt",
            **kw,
        }
        return candidates.control_event(ctx, **args)

    calls = len(fake.bitable_calls)
    assert event(file_token="bascnOther")["ignored"] == "不是选款表"
    assert event(table_id="tblNope")["ignored"] == "改的不是按钮行"
    assert event(record_ids=[image_record])["ignored"] == "改的不是按钮行"
    # 上面三种都不调飞书
    assert len(fake.bitable_calls) == calls
    # 按钮行没勾（比如我们自己取消勾选发出的事件）：回读一下就放过
    assert event()["ignored"] == "按钮行没勾"
    assert _runs(spawned, "accept") == []

    record_id = _tick(ctx, fake, "accept")
    out = event()
    assert out["started"].startswith("BG") and out["by"] == "feishu:ou_evt"
    assert _runs(spawned, "accept") == [
        [
            "table",
            "accept",
            "--seed",
            "S01",
            "--by",
            "feishu:ou_evt",
            "--source",
            "table",
        ]
    ]
    assert not fake.bitable_records[record_id].get("选中")
    assert fake.bitable_records[record_id]["备注"].startswith("验收中")
    task = ctx.db.q1(
        "SELECT kind, subject, notify FROM bg_tasks WHERE task_id = ?",
        (out["started"],),
    )
    assert (task["kind"], task["subject"], task["notify"]) == ("accept", "S01", 1)
    events = [
        r["event"] for r in ctx.db.q("SELECT event FROM events WHERE scope = 'S01'")
    ]
    assert "selection_accept_requested" in events

    # 还在验收时又勾一次：只取消勾选、提示，不重复拉起
    _tick(ctx, fake, "accept")
    again = event()
    assert again["busy"] == out["started"] and len(_runs(spawned, "accept")) == 1
    assert fake.bitable_records[record_id]["备注"].startswith("正在验收")
    assert not fake.bitable_records[record_id].get("选中")


def test_poll_picks_up_ticks_the_event_missed(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    assert candidates.poll_controls(ctx)["started"] == []
    _tick(ctx, fake, "accept", "ou_poller")
    out = candidates.poll_controls(ctx)
    assert len(out["started"]) == 1 and out["errors"] == []
    assert out["checked"] == 1
    assert _runs(spawned, "accept")[0][4:6] == ["--by", "feishu:ou_poller"]
    # 读不到修改人时记成「候选表」
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")
    _tick(ctx, fake, "accept", None)
    fake.bitable_modifiers.clear()
    candidates.poll_controls(ctx)
    assert _runs(spawned, "accept")[1][4:6] == ["--by", candidates.CONTROL_BY]


def test_cli_control_validates_and_auto_tick_polls(
    home,
    fake,
    seed_image,
    capsys,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    app = feishu_table.base(ctx)["app_token"]
    args = ["table", "control", "--file", app, "--table", _table_id(ctx)]
    accept_id = _action_id(ctx, "accept")
    code, out = _cli(capsys, *args, "--records", accept_id, "--by", "ou_x")
    assert code != 0 and "参数不对" in out["detail"]
    code, out = _cli(capsys, *args, "--records", "rec;drop", "--by", "feishu:ou_x")
    assert code != 0
    _tick(ctx, fake, "accept")
    code, out = _cli(capsys, *args, "--records", accept_id, "--by", "feishu:ou_x")
    assert code == 0 and out["started"].startswith("BG")

    # 巡检到点时在后台回读按钮行（同时只跑一个），不占巡检本身的时间
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")
    _tick(ctx, fake, "accept", "ou_tick")
    code, out = _cli(capsys, "tick", "--auto")
    assert code == 0 and out["controls_poll"].startswith("BG")
    assert [a[2:] for a in spawned if a[2:4] == ["table", "poll"]] == [
        ["table", "poll"]
    ]
    assert _runs(spawned, "accept")[-1][4:6] != ["--by", "feishu:ou_tick"]
    assert candidates.kick_controls(ctx) is None
    code, out = _cli(capsys, "table", "poll")
    assert code == 0 and len(out["started"]) == 1
    assert _runs(spawned, "accept")[-1][4:6] == ["--by", "feishu:ou_tick"]
    # 回读成功不进最近动态，失败才进
    poll_task = ctx.db.scalar("SELECT task_id FROM bg_tasks WHERE kind = 'controls'")
    bg.finish(ctx, poll_task, {"checked": 1}, ok=True)
    texts = [i["text"] for i in ui.timeline(ctx, limit=100)["items"]]
    assert not any("回读候选表按钮行" in t for t in texts)
    bg.finish(ctx, poll_task, {"error": "boom"}, ok=False)
    texts = [i["text"] for i in ui.timeline(ctx, limit=100)["items"]]
    assert "后台回读候选表按钮行失败" in texts


# ---------------------------------------------------------------- 选款完成 / 先出套图


def test_accept_can_be_marked_while_drawing(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    _age_selection(ctx)
    pending = len(_open_items(ctx))
    # 又确认了一个方向、还没出图：「选款完成」照样能点（2026-10-30 起是标记，不等图出完）
    _second_round(ctx, run=False)
    left = candidates.unfinished_images(ctx, "S01")
    assert left["jobs"] == 1 and left["images"] > 0 and left["waiting"] == 0
    # 任务列表一直给「选款完成」，不再有「选中的先出套图」
    (item,) = ui.tasks_view(ctx)["items"]
    labels = [a["label"] for a in item["actions"]]
    assert any(x.startswith("选款完成（待选") for x in labels) and "选中的先出套图" not in labels
    # 表里勾「选款完成」：直接拉起，不再拒绝
    record_id = _tick(ctx, fake, "accept")
    out = candidates.poll_controls(ctx)
    assert len(out["started"]) == 1 and not out.get("refused")
    assert fake.bitable_records[record_id]["备注"].startswith("验收中")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")
    # 没勾的都定为没选中；表名【选款完成】
    out = candidates.accept(ctx, "S01", by="feishu:ou_x")
    assert len(out["rejected"]) == pending and out["approved"] == []
    candidates.sync(ctx)
    assert candidates.name_marks(ctx) == {"S01": candidates.MARK_DONE}
    # 之后新一轮出了图进选款：表名【有新图】
    Worker(ctx, max_minutes=5).run()
    candidates.sync(ctx)
    assert candidates.name_marks(ctx) == {"S01": candidates.MARK_NEW}


def test_dispatch_starts_scenes_for_ticked_rows_only(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image, scenes_on=True)
    _age_selection(ctx)
    items = _open_items(ctx)
    picked = items[0]["subject_id"]
    fake.bitable_records[_record(ctx, picked)]["选中"] = True
    # 还有方向没出完也能点
    _second_round(ctx, run=False)
    out = candidates.dispatch(ctx, "S01", by="feishu:ou_x", source="table")
    assert out["approved"] == [picked] and out["picked"] == [picked]
    assert out["scenes"]["queued"] == 1
    # 没勾的不动，还是待选；不算选款完成（不写验收时间、表名不加【已选】）
    assert len(_open_items(ctx)) == len(items) - 1
    assert candidates.table_info(ctx, "S01")["accepted_at"] is None
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE human_decision = 'rejected'")
        == 0
    )
    assert (
        fake.bitable_records[_record(ctx, picked)][candidates.STATE_FIELD] == "已选中"
    )
    # 表顶没有「选中的先出套图」那一行了（命令留给运维），结果不写行
    assert _action_id(ctx, "dispatch") is None
    assert "selection_dispatched" in [
        r["event"] for r in ctx.db.q("SELECT event FROM events WHERE scope = 'S01'")
    ]
    # 再点一次：勾着的这款已经在出套图，不重复交
    again = candidates.dispatch(ctx, "S01", by="feishu:ou_x")
    assert again["approved"] == [] and again["picked"] == [picked]
    assert again["scenes"]["queued"] == 0
    # 完成卡片写明不算选款完成
    summary = bg.summarize("dispatch", out)
    assert summary == {
        "seed_id": "S01",
        "approved": 1,
        "already_decided": 0,
        "picked": 1,
        "table_url": out["table_url"],
        "scenes": out["scenes"],
        "source": "table",
    }
    card = notify.bg_card(ctx, {"kind": "dispatch", "subject": "S01"}, summary, ok=True)
    text = card_text(card)
    assert card["header"]["title"]["content"].startswith("已提前出套图｜S01")
    assert "不算选款完成" in text and "「▶ 选中的先出套图」那一行提交" in text


def test_accept_writes_result_resumes_drawing_and_reports_backlog(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    _age_selection(ctx)
    items = _open_items(ctx)
    fake.bitable_records[_record(ctx, items[0]["subject_id"])]["选中"] = True
    # 还有方向被待审积压挡着：验收让积压降下来后马上接着出
    _second_round(ctx, run=False)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'blocked', blocked_reason = 'review_backlog'"
            " WHERE status = 'ready'"
        )
        conn.execute("UPDATE bg_tasks SET status = 'done'")

    out = candidates.accept(ctx, "S01", by="feishu:ou_x", source="table")
    assert out["approved"] == [items[0]["subject_id"]]
    assert out["backlog"] == {"before": len(items), "after": 0, "limit": 50}
    assert out["generation"]["directions_started"].startswith("BG")
    assert ["worker", "--source", "directions"] == [
        argv[2:5] for argv in spawned if argv[2:3] == ["worker"]
    ][0]
    note = fake.bitable_records[_action_id(ctx, "accept")]["备注"]
    assert note.startswith("上次验收") and f"选中 1 · 没选 {len(items) - 1}" in note

    summary = bg.summarize("accept", out)
    assert summary["generation"] == "started" and summary["source"] == "table"
    text = card_text(
        notify.bg_card(ctx, {"kind": "accept", "subject": "S01"}, summary, ok=True)
    )
    assert f"待审积压 {len(items)} → 0（上限 50）" in text
    assert "已接着出" in text and "「▶ 选款完成」那一行提交" in text


def test_failed_accept_leaves_a_note(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
    monkeypatch,
) -> None:
    ctx = _ready(home, seed_image)

    def busy(ctx, message="候选表正在同步，稍后再点「选款完成」"):
        raise candidates.StageError(message)

    monkeypatch.setattr(candidates, "take_lease", busy)
    with pytest.raises(candidates.StageError):
        candidates.accept(ctx, "S01", by="feishu:ou_x", source="table")
    note = fake.bitable_records[_action_id(ctx, "accept")]["备注"]
    assert note.startswith("验收没成功") and "正在同步" in note
    # 运维命令的提前出套图照样报错（表顶没有那一行，不写）
    with pytest.raises(candidates.StageError):
        candidates.dispatch(ctx, "S01", by="feishu:ou_x", source="table")


# ---------------------------------------------------------------- 套图行与交付包行


def test_scene_rows_follow_the_scene_sets(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image, scenes_on=True)
    set_config(home, "scenes.batch_packages", False)
    ctx = reopen(home)
    picked = _accept_one(ctx, fake)
    # 套图入队以后的同步：表顶（按钮行下面）出现「排队中」的套图行，还没有图和链接
    assert candidates.has_work(ctx)
    candidates.sync(ctx)
    table_id = _table_id(ctx)
    rows = _ordered(fake, table_id)
    assert [f["素材"] for _, f in rows[:1]] == [candidates.CONTROL_TEXT]
    scene_id, scene = rows[1]
    assert scene["素材"].startswith(candidates.SCENE_ROW_PREFIX + picked)
    assert scene["套图状态"] == "排队中"
    assert "下载套图" not in scene and "缩略图" not in scene
    assert "选中的款接着出套图，出完列在表顶" in rows[0][1]["备注"]

    # 出完了：原地写张数、缩略图（只传一次）、下载链接
    set_id = _finish_set(ctx, picked)
    assert candidates.has_work(ctx)
    uploads = len(fake.bitable_files)
    assert candidates.sync(ctx)["top"]["scene_rows"] == 1
    scene = fake.bitable_records[scene_id]
    assert scene["套图状态"] == "已出完" and scene["场景图张数"] == 1
    assert scene["下载套图"]["link"] == "https://oss.example/S01.zip"
    assert (
        scene["下载套图"]["text"].startswith("下载套图（")
        and "前有效" in scene["下载套图"]["text"]
    )
    token = scene["缩略图"][0]["file_token"]
    assert len(fake.bitable_files) == uploads + 1
    assert not candidates.has_work(ctx)

    # 又出了新图：套图行原地不动，缩略图不重传
    images = ctx.db.scalar("SELECT COUNT(*) FROM candidate_rows")
    files = len(fake.bitable_files)
    _second_round(ctx)
    candidates.sync(ctx)
    rows = _ordered(fake, table_id)
    assert rows[1][0] == scene_id and rows[1][1]["缩略图"] == [{"file_token": token}]
    # 新加的候选图各传一张缩略图，套图那张没再传
    added = ctx.db.scalar("SELECT COUNT(*) FROM candidate_rows") - images
    assert added > 0 and len(fake.bitable_files) == files + added

    # 链接续签：换了链接就重写
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE scene_sets SET export_url = 'https://oss.example/renewed.zip' WHERE set_id = ?",
            (set_id,),
        )
    candidates.sync(ctx)
    assert fake.bitable_records[scene_id]["下载套图"]["link"].endswith("renewed.zip")

    # 重出（--force）：同一款换成新的一套，先清掉旧链接和图
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO scene_sets(set_id, asset_id, seed_id, status, preset, created_at, updated_at)"
            " VALUES('SS9999', ?, 'S01', 'queued', 'pod_blanket', ?, ?)",
            (picked, iso(utcnow() + dt.timedelta(seconds=5)), now_iso()),
        )
    candidates.sync(ctx)
    scene = fake.bitable_records[scene_id]
    assert scene["套图状态"] == "排队中"
    assert scene.get("下载套图") is None and scene.get("缩略图") == []
    assert scene.get("场景图张数") is None

    # 这款不再是选中的：套图行删掉
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE assets SET human_decision = 'rejected' WHERE asset_id = ?",
            (picked,),
        )
    out = candidates.sync(ctx)
    assert out["top"]["deleted"] == 1 and scene_id not in fake.bitable_records
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_scene_rows") == 0


def test_rows_for_other_states_say_what_to_do(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image, scenes_on=True)
    picked = _accept_one(ctx, fake)
    candidates.sync(ctx)
    scene_id = ctx.db.scalar(
        "SELECT record_id FROM candidate_scene_rows WHERE asset_id = ?", (picked,)
    )
    for status, extra, expected in (
        ("waiting_human", "", "去 mooya 图案管线处理"),
        ("failed", "mooya 拒收", "没出成：mooya 拒收"),
    ):
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE scene_sets SET status = ?, last_error = ? WHERE asset_id = ?",
                (status, extra or None, picked),
            )
        candidates.sync(ctx)
        assert expected in fake.bitable_records[scene_id]["备注"]


def test_package_row_marks_download_and_sinks(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image, scenes_on=True)
    set_config(home, "shelf.auto", True)
    ctx = reopen(home)
    picked = _accept_one(ctx, fake)
    candidates.sync(ctx)
    table_id = _table_id(ctx)
    # 套图还在出：交付包行写「出完自动打包」，没有链接
    package_id = _action_id(ctx, "package", "B001")
    row = fake.bitable_records[package_id]
    assert row["素材"] == candidates.PACKAGE_ROW_WAIT.format(batch="B001")
    assert row[ORDER] == candidates.order_action("package", "B001")
    _finish_set(ctx, picked)
    packages.build_due(ctx)
    candidates.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    row = fake.bitable_records[package_id]
    assert row["素材"] == candidates.PACKAGE_ROW_TEXT.format(batch="B001", items=1)
    assert row["下载套图"]["link"] == pkg["export_url"]
    assert row["套图交付包"]["link"] == pkg["export_url"]
    assert row["备注"] == "下载后在这一行的「选中」打勾，任务才会归档"
    rows = _ordered(fake, table_id)
    assert [r for r, _ in rows[:3]] == [
        _action_id(ctx, "accept"),
        package_id,
        ctx.db.scalar("SELECT record_id FROM candidate_scene_rows"),
    ]
    (task,) = tasks.summaries(ctx)
    assert task["stage"] == "scenes_done"

    # 勾交付包行：当场标已下载、取消勾选、写备注；满足条件当场归档
    _tick(ctx, fake, "package", "ou_dl", batch_id="B001")
    out = candidates.poll_controls(ctx)
    assert out["downloaded"] == ["B001"] and out["errors"] == []
    row = fake.bitable_records[package_id]
    assert not row.get("选中") and row["备注"].endswith("标了已下载")
    mark = ctx.db.q1("SELECT * FROM package_downloads WHERE seed_id = 'S01'")
    assert (mark["batch_id"], mark["marked_by"], mark["source"]) == (
        "B001",
        "feishu:ou_dl",
        "table",
    )
    assert shelf.is_archived(ctx, "S01")
    # 同步：交付包行改成「已下载」，连同这个批次的套图行沉到最底
    candidates.sync(ctx)
    row = fake.bitable_records[package_id]
    assert row["素材"] == candidates.PACKAGE_ROW_DONE.format(batch="B001", items=1)
    rows = _ordered(fake, table_id)
    scene_id = ctx.db.scalar("SELECT record_id FROM candidate_scene_rows")
    assert [r for r, _ in rows[-2:]] == [package_id, scene_id]
    assert rows[-1][1][ORDER] >= candidates.ORDER_DONE

    # 包的内容变了（重出过）：标记作废，交付包行回到表顶，备注写明要下载新包
    with ctx.db.tx() as conn:
        conn.execute("UPDATE scene_images SET rev = 'r2'")
    packages.build_due(ctx)
    candidates.sync(ctx)
    row = fake.bitable_records[package_id]
    assert row["备注"].startswith("重打过，下载新包后再勾")
    rows = _ordered(fake, table_id)
    assert [r for r, _ in rows[1:3]] == [package_id, scene_id]
    # 包还没重打好时勾：没标上，写明原因
    with ctx.db.tx() as conn:
        conn.execute("UPDATE scene_packages SET fingerprint = 'old'")
    _tick(ctx, fake, "package", batch_id="B001")
    out = candidates.poll_controls(ctx)
    assert out["refused"] and "马上重打" in fake.bitable_records[package_id]["备注"]


# ---------------------------------------------------------------- 订阅与开关


def test_sync_subscribes_events_and_reports_failures(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
    capsys,
) -> None:
    set_config(home, "table.control_row", True)
    fake.subscribe_error = (
        99991672,
        "Access denied. One of the following scopes is required: [docs:event:subscribe]",
    )
    ctx = _setup(home, seed_image)
    feishu_table.sync(ctx)
    state = feishu_table.status(ctx)["events"]
    assert state["subscribed"] is False and "docs:event:subscribe" in state["error"]
    # 失败后一小时内同步不再试
    assert not feishu_table.events_pending(ctx)
    check = next(
        c for c in doctor.run(ctx)["checks"] if c["check"].startswith("候选表按钮行")
    )
    assert check["level"] == "warn" and "docs:event:subscribe" in check["detail"]

    fake.subscribe_error = (1069603, "forbidden")
    code, out = _cli(capsys, "table", "subscribe")
    assert code == 0 and "添加文档应用" in out["error"]

    fake.subscribe_error = None
    code, out = _cli(capsys, "table", "subscribe")
    assert code == 0 and out == {"subscribed": True}
    app = feishu_table.base(ctx)["app_token"]
    assert fake.bitable_subscriptions == [(app, "bitable")]
    ctx = reopen(home)
    assert feishu_table.events_subscribed(ctx)
    check = next(
        c for c in doctor.run(ctx)["checks"] if c["check"].startswith("候选表按钮行")
    )
    assert check["level"] == "ok" and check["detail"].startswith("已订阅（")


def test_switch_off_means_no_button_rows_and_no_subscription(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _setup(home, seed_image)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_actions") == 0
    assert fake.bitable_subscriptions == []
    assert candidates.poll_controls(ctx) == {}
    assert candidates.control_event(
        ctx,
        file_token=feishu_table.base(ctx)["app_token"],
        table_id=_table_id(ctx),
        record_ids=["rec1"],
        by="feishu:ou_x",
    ) == {"ignored": "按钮行关着（table.control_row）"}


def test_old_tables_get_new_columns_options_and_help(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image)
    table_id = _table_id(ctx)
    table = fake.bitable_tables[table_id]
    # 装成 2026-10-26 前的表（字段版本 3）：没有「排序」列，「选款结果」没有「未生成」「没出成」
    table["fields"] = [f for f in table["fields"] if f["field_name"] != ORDER]
    state = next(
        f for f in table["fields"] if f["field_name"] == candidates.STATE_FIELD
    )
    state["property"] = {
        "options": [
            {"name": s, "id": f"optOld{i}"} for i, s in enumerate(candidates.STATES[:5])
        ]
    }
    meta = candidates._fields_meta(ctx)
    meta["versions"][table_id] = 3
    candidates._save_fields_meta(ctx, meta)
    assert candidates.has_work(ctx)
    assert candidates.sync(ctx)["fields"] == 1
    names = [f["field_name"] for f in table["fields"]]
    assert names[-1] == ORDER
    state = next(
        f for f in table["fields"] if f["field_name"] == candidates.STATE_FIELD
    )
    # 原有选项连 id 带回，缺的补在后面
    assert state["property"]["options"] == [
        {"name": s, "id": f"optOld{i}"} for i, s in enumerate(candidates.STATES[:5])
    ] + [{"name": candidates.STATE_UNMADE}, {"name": candidates.STATE_NO_IMAGE}]
    check = next(f for f in table["fields"] if f["field_name"] == "选中")
    assert check["description"]["text"] == candidates.CHECK_HELP and "▶ 选款完成" in check["description"]["text"]
    assert candidates.sync(ctx)["fields"] == 0


def test_reset_and_purge_clean_up_top_rows(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _ready(home, seed_image, scenes_on=True)
    # 重建多维表格：按钮行、排序、订阅记录都作废，候选表在新表格里重建时按钮行跟着建
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, feishu_table.EVENTS_META_KEY, '{"app_token": "x"}')
    feishu_table.init(ctx, reset=True)
    for name in ("candidate_actions", "candidate_orders", "candidate_slots"):
        assert ctx.db.scalar(f"SELECT COUNT(*) FROM {name}") == 0
    assert feishu_table.events_state(ctx) == {}
    candidates.sync(ctx)
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_actions") == 1

    # 有了套图行之后整张参考图删掉：按钮行、套图行、排序的记录一起删
    picked = _accept_one(ctx, fake)
    candidates.sync(ctx)
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_scene_rows") == 1
    assert picked
    # 还在出的套图不让删：先取消
    with ctx.db.tx() as conn:
        conn.execute("UPDATE scene_sets SET status = 'cancelled'")
    maintenance.purge_old(ctx, seeds=["S01"], by="tester")
    for name in (
        "candidate_controls",
        "candidate_actions",
        "candidate_orders",
        "candidate_scene_rows",
        "candidate_tables",
    ):
        assert ctx.db.scalar(f"SELECT COUNT(*) FROM {name}") == 0
