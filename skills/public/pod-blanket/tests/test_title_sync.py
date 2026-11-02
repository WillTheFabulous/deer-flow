"""上架标题双向同步：人在「选中的款」/「已上架的款」改「上架标题」→ 完整复核 → 记版本 → 按环节生效
（还没建箱的建箱时用；采集箱里的存回妙手并读回核对；已上架的走 Temu 接口，没接上就提醒人去后台改）。"""

from __future__ import annotations

import pytest

from conftest import reopen, set_config
from podflow import (
    cells,
    digest,
    feishu_table,
    listed_table,
    stores,
    temu,
    temu_title,
    title_sync,
)
from test_auto_listing import _candidates, _pending_asset, _run_to_listed, _tick
from test_temu import COL3, _record, _selected_record, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

pytestmark = pytest.mark.usefixtures("spawned")

NEW = "Flannel Blanket Throw 280GSM Forest Friends Print Soft Throw for Couch Sofa Living Room"
BAD = "Flannel Blanket Throw Forest Friends Print Soft Throw for Couch Sofa"  # 少了 280GSM


def _home(home, fake, seed_image, **config):
    _candidates(home, fake, seed_image)
    for key, value in config.items():
        set_config(home, f"temu.auto.{key}", value)
    return reopen(home)


def _boxed(home, fake, seed_image):
    """全自动、但不自动发布：款停在采集箱里。"""
    ctx = _home(home, fake, seed_image, publish=False)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    out = _run_to_listed(ctx)
    assert out["boxed"] == ["Deer0001"] and not out["published"]
    feishu_table.sync(ctx)
    assert cells.read_selected(ctx, force=True)["titles"] == []
    return ctx, asset_id


def _edit_selected(ctx, fake, asset_id: str, title: str) -> dict:
    record = _selected_record(ctx, asset_id)
    fake.bitable_records[record]["上架标题"] = title
    fake.bitable_modifiers[record] = "ou_editor"
    return cells.read_selected(ctx, force=True)


def _detail(ctx) -> int:
    return int(stores.rows(ctx)[0]["detail_id"])


def test_selected_title_edit_is_saved_into_box(home, fake, seed_image) -> None:
    ctx, asset_id = _boxed(home, fake, seed_image)
    out = _edit_selected(ctx, fake, asset_id, NEW)
    assert out["titles"][0]["saved"] and out["titles"][0]["stores"] == [
        {"store": "s3", "state": "pending", "target": "box"}
    ]
    current = temu_title.get(ctx, asset_id)
    assert current["title"] == NEW and current["source"] == temu_title.SOURCE_HUMAN
    version = temu_title.latest_version(ctx, asset_id)
    assert (version["title"], version["source"], version["via"]) == (
        NEW,
        temu_title.SOURCE_HUMAN,
        "selected",
    )
    assert version["made_by"].endswith("ou_editor")
    # 下一轮上架后台：存回妙手采集箱（只改标题），读回核对，品牌照样在
    result = temu.sync(ctx)
    assert result["titles_saved"] == ["Deer0001"]
    info = fake.miaoshou_items[_detail(ctx)]["info"]
    assert info["title"] == NEW and info["multiLanguageTitleMap"]["en"] == NEW
    assert temu.brand_note(ctx, stores.rows(ctx)[0]) is None
    assert title_sync.status(ctx, asset_id)[0]["state"] == title_sync.STATE_DONE
    feishu_table.sync(ctx)
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert (
        row["标题同步"].startswith("3店：已生效（存进妙手采集箱）")
        and row["上架标题"] == NEW
    )
    log = ctx.db.q1(
        "SELECT * FROM table_cell_log WHERE kind = 'selected' AND field = '上架标题'"
    )
    assert log["outcome"] == cells.OUTCOME_ACCEPTED and log["editor"].endswith(
        "ou_editor"
    )
    # 再读一次没有新改动：不重复存
    assert cells.read_selected(ctx, force=True)["titles"] == []


def test_invalid_title_edit_is_kept_out(home, fake, seed_image) -> None:
    ctx, asset_id = _boxed(home, fake, seed_image)
    before = temu_title.get(ctx, asset_id)["title"]
    out = _edit_selected(ctx, fake, asset_id, BAD)
    assert "280GSM" in out["titles"][0]["rejected"]
    # 库里的标题不动；不合规的也记一版；表里人填的不改回，「标题同步」写原因；进「上架要你看」
    assert temu_title.get(ctx, asset_id)["title"] == before
    assert not temu_title.latest_version(ctx, asset_id, ok_only=False)["check_ok"]
    assert title_sync.status(ctx, asset_id)[0]["state"] == title_sync.STATE_INVALID
    log = ctx.db.q1(
        "SELECT * FROM table_cell_log WHERE kind = 'selected' AND field = '上架标题'"
    )
    assert log["outcome"] == cells.OUTCOME_REJECTED
    temu.sync(ctx)
    feishu_table.sync(ctx)
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert row["上架标题"] == BAD and row["标题同步"].startswith("3店：不合规")
    assert fake.miaoshou_items[_detail(ctx)]["info"]["title"] == before
    todo = digest.todo_items(ctx, kinds=["listing"])
    assert [x["kind"] for x in todo["listing"]] == ["title"]


def test_title_edit_before_box_is_used_when_boxing(home, fake, seed_image) -> None:
    # 程序不替人确认：标题准备好了停在「待确认上架」
    ctx = _home(home, fake, seed_image, confirm=False)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    out = _run_to_listed(ctx)
    assert out["prepared"] == [asset_id] and not out["boxed"]
    feishu_table.sync(ctx)
    cells.read_selected(ctx, force=True)
    out = _edit_selected(ctx, fake, asset_id, NEW)
    assert out["titles"][0]["stores"] == [
        {"store": "s3", "state": "done", "target": None}
    ]
    record = _selected_record(ctx, asset_id)
    fake.bitable_records[record]["确认上架"] = True
    temu.poll(ctx)
    temu.sync(ctx)
    assert fake.miaoshou_items[_detail(ctx)]["info"]["title"] == NEW
    feishu_table.sync(ctx)
    assert fake.bitable_records[record]["标题同步"].startswith(
        "3店：已生效（建箱时用）"
    )


def test_listed_title_edit_waits_for_temu_without_api(home, fake, seed_image) -> None:
    ctx = _home(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert _run_to_listed(ctx)["listed"] == ["Deer0001"]
    listed_table.read_back(ctx)
    record_id = listed_table.rows(ctx)[0]["record_id"]
    fake.bitable_records[record_id]["上架标题"] = NEW
    fake.bitable_modifiers[record_id] = "ou_editor"
    out = listed_table.read_back(ctx)
    assert out["titles"][0]["stores"] == [
        {"store": "s3", "state": "waiting_temu", "target": "temu"}
    ]
    # 已上架的记录跟着换标题；两张表的「上架标题」写成新的，「标题同步」写要人去 Temu 后台改
    assert listed_table.rows(ctx)[0]["title"] == NEW
    feishu_table.sync(ctx)
    assert "Temu 接口还没接上" in fake.bitable_records[record_id]["标题同步"]
    assert fake.bitable_records[_selected_record(ctx, asset_id)]["上架标题"] == NEW
    todo = digest.todo_items(ctx, kinds=["listing"])
    assert any(x["kind"] == "title" for x in todo["listing"])
