"""人在飞书表里改的值落库（cells）：「选中的款」人填列记下、程序列记 reverted 并改回、程序自己写的不算人改、
删行前整行快照；候选表的备注 / 没选原因也记。"""

from __future__ import annotations

import json

import pytest

from podflow import candidate_stores, candidates, cells, cli, feishu_table, temu
from test_auto_listing import _candidates, _pending_asset, _run_to_listed, _tick
from test_temu import COL3, _record, _selected_record, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

pytestmark = pytest.mark.usefixtures("spawned")


def _selected(home, fake, seed_image):
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    feishu_table.sync(ctx)
    assert cells.read_selected(ctx, force=True)["changed"] == 0
    return ctx, asset_id


def _log(ctx, field: str) -> list[dict]:
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM table_cell_log WHERE field = ? ORDER BY log_id", (field,)
        )
    ]


def test_human_columns_are_logged_with_editor(home, fake, seed_image) -> None:
    ctx, asset_id = _selected(home, fake, seed_image)
    record = _selected_record(ctx, asset_id)
    fake.bitable_records[record]["备注"] = "主推"
    fake.bitable_records[record]["人加的列"] = "A"
    fake.bitable_modifiers[record] = "ou_editor"
    cells.read_selected(ctx, force=True)
    # 第一次读到、程序也不写的格子只记下
    assert _log(ctx, "备注") == []
    fake.bitable_records[record]["备注"] = "主推，先上 3店"
    out = cells.read_selected(ctx, force=True)
    assert out["changed"] == 1
    log = _log(ctx, "备注")[0]
    assert (
        json.loads(log["old_json"]),
        json.loads(log["new_json"]),
        log["outcome"],
    ) == (
        "主推",
        "主推，先上 3店",
        cells.OUTCOME_RECORDED,
    )
    assert log["editor"] == "feishu:ou_editor" and log["edited_at"]
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM trace_events WHERE asset_id = ? AND stage = 'cell'",
            (asset_id,),
        )
        == 1
    )
    # podctl table cells --asset
    assert cli.main(["table", "cells", "--asset", asset_id]) == 0


def test_program_columns_are_reverted(home, fake, seed_image) -> None:
    ctx, asset_id = _selected(home, fake, seed_image)
    record = _selected_record(ctx, asset_id)
    program_note = fake.bitable_records[record]["上架备注"]
    fake.bitable_records[record]["上架备注"] = "人写的"
    fake.bitable_records[record]["货号"] = "Deer9999"
    fake.bitable_modifiers[record] = "ou_editor"
    out = cells.read_selected(ctx, force=True)
    assert out["reverted"] == [asset_id]
    assert {
        (r["field"], r["outcome"])
        for r in ctx.db.q("SELECT field, outcome FROM table_cell_log")
    } == {
        ("上架备注", cells.OUTCOME_REVERTED),
        ("货号", cells.OUTCOME_REVERTED),
    }
    # 当场改回
    assert fake.bitable_records[record]["上架备注"] == program_note
    assert fake.bitable_records[record]["货号"] == "Deer0001"


def test_program_writes_are_not_human_edits(home, fake, seed_image) -> None:
    ctx, asset_id = _selected(home, fake, seed_image)
    # 程序往下走、写了当前环节 / 上架状态 / 妙手 ID…：读回来和程序写的一样，不记
    _run_to_listed(ctx)
    feishu_table.sync(ctx)
    assert cells.read_selected(ctx, force=True)["changed"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM table_cell_log") == 0


def test_row_snapshot_before_delete(home, fake, seed_image) -> None:
    ctx, asset_id = _selected(home, fake, seed_image)
    record = _selected_record(ctx, asset_id)
    fake.bitable_records[record]["备注"] = "留着看"
    assert feishu_table.delete_rows(ctx, [asset_id])["deleted"] == 1
    snap = ctx.db.q1("SELECT * FROM table_row_snapshots WHERE record_id = ?", (record,))
    assert (
        snap["asset_id"] == asset_id
        and json.loads(snap["fields_json"])["备注"] == "留着看"
    )
    # 整张表读到这一行没了：记一条 deleted
    cells.read_selected(ctx, force=True)
    assert [r["outcome"] for r in _log(ctx, cells.ROW_FIELD)] == [cells.OUTCOME_DELETED]


def test_candidate_notes_are_logged(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    record = _record(ctx, asset_id)

    def poll() -> None:
        with ctx.db.tx() as conn:
            ctx.db.del_meta(conn, candidate_stores.TICKS_META)
        candidate_stores.poll(ctx)

    fake.bitable_records[record][candidates.NOTE_FIELD] = "颜色再亮点"
    fake.bitable_modifiers[record] = "ou_picker"
    poll()
    fake.bitable_records[record][candidates.NOTE_FIELD] = "颜色再亮点，换个背景"
    poll()
    log = _log(ctx, candidates.NOTE_FIELD)
    assert (
        len(log) == 1
        and log[0]["kind"] == cells.KIND_CANDIDATE
        and log[0]["editor"] == "feishu:ou_picker"
    )
    assert temu.alerts(ctx) == []
