"""全自动上架（2026-10-30，temu.auto.*）：候选表勾店铺 → 选中 → 领号 → 出套图 → 上架图与标题 → 程序确认 → 建箱 → 写品牌 → 发布，
人只在出异常时介入；撤销窗口、暂停、取消店铺勾、发布结果未知、套图自愈、货号失败、待办与红卡。"""

from __future__ import annotations

import pytest

from conftest import reopen, set_config
from podflow import (
    candidate_stores,
    candidates,
    digest,
    feishu_table,
    listed_table,
    mooya,
    review,
    scenes,
    shelf,
    store,
    stores,
    temu,
    trace,
)
from podflow.worker import Worker
from test_candidates import _age_selection
from test_pipeline import bootstrap
from test_temu import (  # noqa: F401 - spawned 是夹具
    BRAND_ID,
    COL3,
    COL4,
    _created,
    _record,
    _selected_record,
    _temu_home,
    spawned,
)

# 假拉起后台进程（表格同步、套图、撤销窗口）：只要它的副作用
pytestmark = pytest.mark.usefixtures("spawned")


def _auto_home(
    home,
    fake,
    *,
    grace: float = 0,
    stores_on: tuple[str, ...] = ("s3",),
    brand: bool = True,
) -> None:
    _temu_home(
        home,
        fake,
        publish="box",
        control_row=True,
        brand_id=BRAND_ID if brand else None,
    )
    set_config(
        home,
        "temu.auto",
        {"enabled": True, "stores": list(stores_on), "tick_grace_minutes": grace},
    )


def _candidates(home, fake, seed_image, **kwargs):
    _auto_home(home, fake, **kwargs)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)
    candidates.sync(ctx)
    return ctx


def _tick(ctx, fake, record_id: str, *columns: str, by: str = "ou_picker") -> dict:
    """人在候选表这一行勾了店铺列（最后修改人是他），网关转来记录变更事件。"""
    for column in columns:
        fake.bitable_records[record_id][column] = True
    fake.bitable_modifiers[record_id] = by
    table_id = ctx.db.scalar(
        "SELECT table_id FROM candidate_tables WHERE seed_id = 'S01'"
    )
    return candidates.control_event(
        ctx,
        file_token=feishu_table.base(ctx)["app_token"],
        table_id=table_id,
        record_ids=[record_id],
        by=f"feishu:{by}",
    )


def _pending_asset(ctx) -> str:
    return review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]


def _run_to_listed(ctx) -> dict:
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    return temu.sync(ctx)


def test_tick_store_on_unselected_row_lists_without_any_confirmation(
    home, fake, seed_image
) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    out = _tick(ctx, fake, _record(ctx, asset_id), COL3)
    # 不用点按钮：当场定为选中（确认人是勾的人）、记店铺、领号、交 mooya、程序确认上架
    assert out["stores"]["selected_rows"] == [asset_id]
    assert (
        ctx.db.scalar(
            "SELECT human_decision FROM assets WHERE asset_id = ?", (asset_id,)
        )
        == "approved"
    )
    decided = ctx.db.q1(
        "SELECT decided_by, note FROM reviews WHERE subject_type = 'asset_selection' AND subject_id = ?",
        (asset_id,),
    )
    assert (
        decided["decided_by"] == "feishu:ou_picker"
        and decided["note"] == candidate_stores.AUTO_TICK_NOTE
    )
    rows = stores.rows(ctx, asset_ids=[asset_id])
    assert [(r["store_key"], r["state"], r["code"]) for r in rows] == [
        ("s3", "queued", "Deer0001")
    ]
    assert rows[0]["confirmed_by"] == temu.BY_AUTO
    assert scenes.latest_sets(ctx, [asset_id])[asset_id]["status"] == "queued"
    # 套图出完、上架图和标题准备好，进箱、写品牌、自动发布（3店开着全自动，不看 temu.publish: box）
    result = _run_to_listed(ctx)
    code = "Deer0001"
    assert (
        result["boxed"] == [code]
        and result["published"] == [code]
        and result["listed"] == [code]
    )
    assert len(_created(fake)) == 1
    # 「选中的款」：确认上架由程序勾上，当前环节写已上架，妙手 ID 写上；上架状态汇总为已上架
    feishu_table.sync(ctx)
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert row["确认上架"] is True and row["上架状态"] == "已上架"
    assert row["当前环节"] == "已上架" and row["环节开始"]
    assert row["妙手 ID"].startswith("3店 ")
    assert listed_table.rows(ctx)[0]["code"] == code
    # 每一步都留了轨迹
    stages = [
        r["stage"]
        for r in ctx.db.q(
            "SELECT stage FROM trace_events WHERE asset_id = ? ORDER BY trace_id",
            (asset_id,),
        )
    ]
    for stage in (
        "select",
        "store",
        "code",
        "scenes",
        "assets",
        "title",
        "listing",
        "brand",
        "listed",
        "pipeline",
    ):
        assert stage in stages, stage
    confirmed = ctx.db.q1(
        "SELECT actor, source FROM trace_events WHERE asset_id = ? AND stage = 'listing' AND to_state = 'queued'",
        (asset_id,),
    )
    assert (confirmed["actor"], confirmed["source"]) == (temu.BY_AUTO, temu.SOURCE_AUTO)
    assert trace.summary(ctx, asset_id)["stage"] == "listed"


def test_tick_on_store_without_auto_waits_for_button(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    out = _tick(ctx, fake, _record(ctx, asset_id), COL4)
    # 4店没开全自动：还没选的行照旧等按钮
    assert out.get("ignored") and stores.rows(ctx, asset_ids=[asset_id]) == []


def test_grace_window_waits_and_withdrawn_tick_is_dropped(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811 - 要看拉起了什么
) -> None:
    ctx = _candidates(home, fake, seed_image, grace=2)
    items = review.list_reviews(ctx, types=["asset_selection"])
    keep, drop = items[0]["subject_id"], items[1]["subject_id"]
    first = _tick(ctx, fake, _record(ctx, keep), COL3)
    second = _tick(ctx, fake, _record(ctx, drop), COL3)
    assert first["stores"]["pending"] == [keep] and second["stores"]["pending"] == [
        drop
    ]
    assert ["table", "ticks"] in [argv[2:4] for argv in spawned]
    assert stores.rows(ctx) == [] and candidate_stores.process_pending(ctx)["due"] == 0
    # 撤销窗口里取消了一个；到点后回读：仍勾着的定为选中，取消了的丢掉
    fake.bitable_records[_record(ctx, drop)][COL3] = False
    with ctx.db.tx() as conn:
        conn.execute("UPDATE store_tick_pending SET due_at = '2000-01-01T00:00:00Z'")
    out = candidate_stores.process_pending(ctx)
    assert out["selected"] == [keep] and out["withdrawn"] == [drop]
    assert [r["asset_id"] for r in stores.rows(ctx)] == [keep]
    assert not candidate_stores.has_pending(ctx)
    withdrawn = ctx.db.q1(
        "SELECT to_state FROM trace_events WHERE asset_id = ? AND stage = 'tick' ORDER BY trace_id DESC",
        (drop,),
    )
    assert withdrawn["to_state"] == "withdrawn"


def test_grace_window_defaults_to_five_minutes(home, fake) -> None:
    _auto_home(home, fake)
    set_config(home, "temu.auto", {"enabled": True, "stores": ["s3"]})
    assert candidate_stores.grace_minutes(reopen(home)) == 5


def test_accept_inside_grace_window_settles_ticks_at_once(
    home, fake, seed_image
) -> None:
    ctx = _candidates(home, fake, seed_image, grace=5)
    asset_id = _pending_asset(ctx)
    out = _tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert out["stores"]["pending"] == [asset_id] and stores.rows(ctx) == []
    # 犹豫期里点了「选款完成」：勾着店铺的当场定为选中、按勾的店铺领号，撤销窗口里的记录删掉
    accepted = candidates.accept(ctx, "S01", by="feishu:ou_x", source="table")
    assert asset_id in accepted["approved"]
    assert [(r["store_key"], r["code"]) for r in stores.rows(ctx)] == [
        ("s3", "Deer0001")
    ]
    assert not candidate_stores.has_pending(ctx)


def test_table_archives_once_every_pick_is_listed(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    set_config(home, "shelf.auto", True)
    ctx = reopen(home)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by="feishu:ou_x")
    # 选款完成了，选中的款还没上架：不收
    scenes.sync(ctx)
    assert not shelf.is_archived(ctx, "S01")
    blocked = {
        b["seed_id"]: b["reason"]
        for b in shelf.sweep(ctx, source="test", dry_run=True)["blocked"]
    }
    assert blocked["S01"].startswith("选中的款还有 1 款没上架完")
    # 上架完成：当场归档（不等交付包标已下载）
    feishu_table.sync(ctx)
    out = temu.sync(ctx)
    assert out["listed"] == ["Deer0001"] and out["shelf"] == {"archived": ["S01"]}
    assert shelf.is_archived(ctx, "S01")


def test_missed_tick_event_on_unselected_row_is_picked_up_by_poll(
    home, fake, seed_image
) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    fake.bitable_records[_record(ctx, asset_id)][COL3] = True
    fake.bitable_modifiers[_record(ctx, asset_id)] = "ou_picker"
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, candidate_stores.TICKS_META)
    out = candidate_stores.poll(ctx)
    assert asset_id in out["selected_rows"] and out["chosen"] == {asset_id: ["s3"]}


def test_untick_confirm_pauses_and_retick_resumes(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    feishu_table.sync(ctx)
    record = _selected_record(ctx, asset_id)
    assert fake.bitable_records[record]["确认上架"] is True
    # 人取消「确认上架」（还没建箱）：取消确认并暂停，程序不再替他确认
    fake.bitable_records[record]["确认上架"] = False
    out = temu.poll(ctx)
    assert out["cancelled"] == [asset_id] and temu.held(ctx, asset_id)
    assert temu.auto_confirm(ctx) == {}
    assert stores.rows(ctx)[0]["state"] == "chosen"
    assert "已暂停" in temu.note_line(ctx, stores.rows(ctx)[0])
    # 再勾：解除暂停，接着上
    fake.bitable_records[record]["确认上架"] = True
    out = temu.poll(ctx)
    assert out["confirmed"] == [asset_id] and not temu.held(ctx, asset_id)
    assert stores.rows(ctx)[0]["state"] == "queued"


def test_untick_in_box_pauses_publish_and_retick_resumes(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    set_config(home, "temu.auto.publish", False)
    ctx = reopen(home)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert _run_to_listed(ctx)["boxed"] == ["Deer0001"]
    feishu_table.sync(ctx)
    record = _selected_record(ctx, asset_id)
    assert fake.bitable_records[record]["确认上架"] is True
    # 打开自动发布，发布之前人取消「确认上架」：进了采集箱的先不发布，勾保持取消
    set_config(home, "temu.auto.publish", True)
    ctx = reopen(home)
    fake.bitable_records[record]["确认上架"] = False
    out = temu.poll(ctx)
    assert out["paused"] == [asset_id] and out["reticked"] == []
    assert fake.bitable_records[record]["确认上架"] is False
    assert not temu.sync(ctx)["published"] and stores.rows(ctx)[0]["state"] == temu.STATE_BOX
    assert trace.summary(ctx, asset_id)["stage"] == "hold"
    feishu_table.sync(ctx)
    assert fake.bitable_records[record]["确认上架"] is False
    assert fake.bitable_records[record]["当前环节"] == "已暂停上架"
    # 再勾：解除暂停、接着发布
    fake.bitable_records[record]["确认上架"] = True
    fake.bitable_modifiers[record] = "ou_picker"
    got = temu.table_event(ctx, record_ids=[record], by="feishu:ou_picker")
    assert got["resumed"] == [asset_id] and not temu.held(ctx, asset_id)
    assert temu.sync(ctx)["published"] == ["Deer0001"]


def test_untick_store_column_removes_store_until_box_then_reticks(
    home, fake, seed_image
) -> None:
    ctx = _candidates(home, fake, seed_image)
    items = review.list_reviews(ctx, types=["asset_selection"])
    early, late = items[0]["subject_id"], items[1]["subject_id"]
    for asset_id in (early, late):
        _tick(ctx, fake, _record(ctx, asset_id), COL3)
    # late 进了采集箱；early 还在排队
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE store_listings SET state = 'in_box', detail_id = '9001' WHERE asset_id = ?",
            (late,),
        )
    for asset_id in (early, late):
        fake.bitable_records[_record(ctx, asset_id)][COL3] = False
        fake.bitable_modifiers[_record(ctx, asset_id)] = "ou_picker"
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, candidate_stores.TICKS_META)
    out = candidate_stores.poll(ctx)
    gone = out["unticked"][0]
    assert gone["removed"] == {early: ["s3"]} and gone["reticked"] == [late]
    assert stores.rows(ctx, asset_ids=[early]) == []
    # 写回去：下一次同步把 late 那一行的勾写回
    candidates.sync(ctx)
    assert fake.bitable_records[_record(ctx, late)][COL3] is True


def test_publish_timeout_is_not_resent_at_once(
    home, fake, seed_image, monkeypatch
) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    fake.miaoshou_publish = "hold"
    fake.miaoshou_timeout["save_move_collect_task"] = "after"
    temu.sync(ctx)
    row = stores.rows(ctx)[0]
    assert row["state"] == temu.STATE_PUBLISH_UNKNOWN
    publishes = [t for t, _ in fake.miaoshou_calls if t == "save_move_collect_task"]
    # 下一轮：结果未知的不重发
    temu.sync(ctx)
    assert [
        t for t, _ in fake.miaoshou_calls if t == "save_move_collect_task"
    ] == publishes
    # 过了等待时间还没发布：重发一次（只一次）
    monkeypatch.setattr(temu, "PUBLISH_UNKNOWN_MINUTES", -1)
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, temu.CHECKED_META_KEY)
    temu.sync(ctx)
    assert stores.rows(ctx)[0]["state"] == temu.STATE_BOX
    temu.sync(ctx)
    assert (
        len([t for t, _ in fake.miaoshou_calls if t == "save_move_collect_task"])
        == len(publishes) + 1
    )


def test_auto_store_without_brand_is_not_published(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image, brand=False)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    out = _run_to_listed(ctx)
    assert out["boxed"] == ["Deer0001"] and not out["published"]
    assert "没配品牌" in temu.note_line(ctx, stores.rows(ctx)[0])


def test_transient_code_errors_do_not_count_and_scenes_wait_for_code(
    home, fake, seed_image
) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    fake.mooya_fail["codes"] = 503
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    row = stores.rows(ctx)[0]
    assert not row["code"] and row["code_attempts"] == 0 and "503" in row["code_error"]
    # 没货号不交 mooya（交付包名要是货号）；满了次数也不放行
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE store_listings SET code_attempts = ?", (stores.MAX_CODE_ATTEMPTS,)
        )
    assert stores.waiting_codes(ctx, [asset_id]) == {asset_id}
    scenes.sync(ctx)
    assert scenes.latest_sets(ctx, [asset_id])[asset_id]["status"] == "queued"
    # 进「POD 待办」的上架要你看
    todo = digest.todo_items(ctx, kinds=["listing"])
    assert any(x["kind"] == "code" for x in todo["listing"])
    fake.mooya_fail.pop("codes")
    stores.retry_codes(ctx)
    scenes.sync(ctx)
    assert scenes.latest_sets(ctx, [asset_id])[asset_id]["status"] == "done"
    sets = scenes.latest_sets(ctx, [asset_id])[asset_id]
    assert scenes.flags(ctx, sets["set_id"])["package_name"] == "Deer0001"


def test_partial_scenes_are_healed_by_mooya_regenerate(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    fake.mooya_script = ["queued", "running", "partial"]
    scenes.sync(ctx)
    current = scenes.latest_sets(ctx, [asset_id])[asset_id]
    # 缺一张：图位校验过不了 → 让 mooya 补缺图，补完接着拉图
    assert current["status"] == "done"
    assert [a for _job, a in fake.mooya_actions] == [mooya.ACTION_REGENERATE_MISSING]
    assert (
        scenes.flags(ctx, current["set_id"])["heal"][mooya.ACTION_REGENERATE_MISSING]
        == 1
    )
    assert temu.sync(ctx)["prepared"] == [asset_id]


def test_partial_scenes_resubmitted_when_mooya_has_no_actions(
    home, fake, seed_image
) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    fake.mooya_actions_supported = False
    fake.mooya_script = ["queued", "running", "partial"]
    scenes.sync(ctx)
    sets = ctx.db.q(
        "SELECT set_id, status, requested_by FROM scene_sets WHERE asset_id = ? ORDER BY set_id",
        (asset_id,),
    )
    # 老 mooya 没有动作端点：整套重出一次（requested_by system:heal），只一次
    assert [r["requested_by"] for r in sets][-1] == scenes.HEAL_BY
    assert len(sets) == 2
    scenes.sync(ctx)
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM scene_sets WHERE asset_id = ?", (asset_id,))
        == 2
    )


def test_paused_scenes_are_resumed_after_a_while(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    fake.mooya_script = ["queued", "paused"]
    scenes.sync(ctx)
    current = scenes.latest_sets(ctx, [asset_id])[asset_id]
    assert (
        current["status"] == "paused"
        and scenes.flags(ctx, current["set_id"])["parked_at"]
    )
    set_config(home, "scenes.auto_resume_minutes", 0.0001)
    ctx = reopen(home)
    scenes.sync(ctx)
    assert [a for _job, a in fake.mooya_actions] == [mooya.ACTION_RESUME]
    assert scenes.latest_sets(ctx, [asset_id])[asset_id]["status"] == "done"


def test_baseline_fallback_is_noted_in_listing_note(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    fake.mooya_baseline = {"passed": False, "score": 61, "fallback": True}
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert "白底图质检没过" in row["上架备注"] and "61" in row["上架备注"]
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM trace_events WHERE asset_id = ? AND stage = 'scenes' AND reason LIKE '白底图%'",
            (asset_id,),
        )
        == 1
    )


def test_custom_design_fails_with_red_card_and_todo(home, fake, seed_image) -> None:
    fake.p15_result = {
        "title": "Custom Flannel Blanket 280GSM Personalized Name Throw for Couch",
        "is_custom": True,
        "lead_type": "text",
        "holiday": None,
        "gift_led": False,
    }
    ctx = _candidates(home, fake, seed_image)
    set_config(
        home,
        "notify.feishu",
        {"enabled": True, "receive_id_type": "chat_id", "receive_id": "oc_test"},
    )
    ctx = reopen(home)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    out = _run_to_listed(ctx)
    assert out["failed"] == [asset_id] and not _created(fake)
    assert out["alerted"].get("sent") or out["alerted"].get("error") is None
    assert any("上架没成" in text for text in fake.feishu_messages)
    todo = digest.todo_items(ctx, kinds=["listing"])
    assert [x["kind"] for x in todo["listing"]] == ["failed"]
    assert any("上架要你看" in line for line in digest._todo_lines(todo))


@pytest.mark.parametrize("auto_publish", [True, False])
def test_auto_publish_switch(home, fake, seed_image, auto_publish) -> None:
    ctx = _candidates(home, fake, seed_image)
    set_config(home, "temu.auto.publish", auto_publish)
    ctx = reopen(home)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    out = _run_to_listed(ctx)
    assert out["boxed"] == ["Deer0001"]
    assert bool(out["published"]) is auto_publish
