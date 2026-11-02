"""候选表的预留行（table.reserve_rows）：方向一确认就按出图槽位占好行（选款结果「未生成」），图进选款时原行补图；
方向出完了这个槽位也没图是「没出成」，重出退回「未生成」，停掉方向、S06 去掉的槽位删行。验收和先出套图都不碰没图的行。"""

from __future__ import annotations

import pytest

from conftest import reopen, set_config
from podflow import candidates, directions, maintenance, store, tasks, ui
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.worker import Worker
from test_candidates import (  # noqa: F401
    _age_selection,
    _open_items,
    _record,
    spawned,
)
from test_pipeline import confirm_all
from test_storage import _enable_oss
from test_ui import _enable_feishu

pytestmark = pytest.mark.usefixtures("spawned")
ORDER = candidates.ORDER_FIELD
STATE = candidates.STATE_FIELD


def _confirmed(home, seed_image, *, limit: int = 1):
    """接好表格、OSS，打开预留行和默认视图（按「排序」排）；拆解、策划 limit 个方向、全部确认，还没出图。"""
    _enable_feishu(home)
    _enable_oss(home)
    set_config(home, "table.enabled", True)
    set_config(home, "table.reserve_rows", True)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    analysis = seedmod.analyze(ctx, seed["seed_id"])
    family_id = analysis["steps"][-1]["family_id"]
    plan = planmod.plan_batch(ctx, family_id, limit=limit)
    confirm_all(ctx, plan)
    return ctx, plan["batch_id"]


def _draw(ctx) -> None:
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)


def _slots(ctx) -> list[dict]:
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT s.job_id, s.slot_id, s.model, j.variant_id FROM job_slots s"
            " JOIN jobs j ON j.job_id = s.job_id ORDER BY s.job_id, s.slot_id"
        )
    ]


def _reserved(ctx) -> dict[tuple[str, str], dict]:
    return {
        (r["job_id"], r["slot_id"]): dict(r)
        for r in ctx.db.q("SELECT * FROM candidate_slots")
    }


def _table_rows(fake, ctx) -> list[tuple[str, dict]]:
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    rows = [
        (r, fields)
        for r, fields in fake.bitable_records.items()
        if fake.bitable_record_tables.get(r) == table_id
    ]
    return sorted(rows, key=lambda item: item[1].get(ORDER) or 0)


def test_confirmed_directions_get_reserved_rows(home, fake, seed_image) -> None:
    ctx, batch_id = _confirmed(home, seed_image)
    slots = _slots(ctx)
    assert slots
    # 确认方向时就拉起了表格同步；还没有图也先建候选表
    assert candidates.has_work(ctx)
    out = candidates.sync(ctx)
    assert out["tables_created"] == 1 and out["slots"]["reserved"] == len(slots)
    rows = _table_rows(fake, ctx)
    assert len(rows) == len(slots)
    reserved = _reserved(ctx)
    for slot, (record_id, fields) in zip(slots, rows):
        row = reserved[(slot["job_id"], slot["slot_id"])]
        assert row["record_id"] == record_id and row["state"] == candidates.SLOT_UNMADE
        assert fields[STATE] == candidates.STATE_UNMADE
        assert fields["素材"].startswith(slot["variant_id"])
        assert fields["素材"].endswith(" · 未生成")
        assert (
            fields["方向"].startswith(slot["variant_id"]) and fields["批次"] == batch_id
        )
        assert fields["出图方式"] and fields["模型"]
        assert "缩略图" not in fields and "选中" not in fields
        assert isinstance(fields[ORDER], int)
    assert not candidates.has_work(ctx)
    (table,) = candidates.status(ctx)["tables"]
    assert table["reserved"] == {"未生成": len(slots), "没出成": 0}
    # 任务列表：方向还在出，候选表已经能打开
    (item,) = ui.tasks_view(ctx)["items"]
    assert any(link["label"] == "候选表" for link in item["links"])

    # 出图：图进选款时在原行补上图（行数不变、记录不变），变成「待选」
    _draw(ctx)
    images = len(_open_items(ctx))
    out = candidates.sync(ctx)
    assert out["added"] == images
    rows = _table_rows(fake, ctx)
    filled = {
        r["record_id"]
        for r in ctx.db.q(
            "SELECT record_id FROM candidate_slots WHERE asset_id IS NOT NULL"
        )
    }
    assert len(filled) == images and filled <= {r for r, _ in rows}
    for record_id in filled:
        fields = fake.bitable_records[record_id]
        assert fields[STATE] == candidates.STATE_PENDING and fields["缩略图"]
        assert fields["选中"] is False and fields["备注"] == ""
    assert not candidates.has_work(ctx)


def test_slot_without_image_becomes_no_image_and_back(home, fake, seed_image) -> None:
    ctx, _ = _confirmed(home, seed_image)
    candidates.sync(ctx)
    slot = _slots(ctx)[0]
    key = (slot["job_id"], slot["slot_id"])
    record_id = _reserved(ctx)[key]["record_id"]
    # 方向出完了，这个槽位没图：「没出成」，备注写原因
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', last_error = '所有槽位均未出图' WHERE job_id = ?",
            (slot["job_id"],),
        )
        conn.execute(
            "UPDATE job_slots SET status = 'failed', last_error = '内容审核拦截' WHERE job_id = ?",
            (slot["job_id"],),
        )
    assert candidates.has_work(ctx)
    out = candidates.sync(ctx)
    assert out["slots"]["changed"] >= 1
    fields = fake.bitable_records[record_id]
    assert fields[STATE] == candidates.STATE_NO_IMAGE
    assert (
        fields["素材"].endswith(" · 没出成")
        and fields["备注"] == "没出成：内容审核拦截"
    )
    assert _reserved(ctx)[key]["state"] == candidates.SLOT_NO_IMAGE
    # 又接着出（重出 / 恢复）：回到「未生成」
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'ready' WHERE job_id = ?", (slot["job_id"],)
        )
    candidates.sync(ctx)
    fields = fake.bitable_records[record_id]
    assert fields[STATE] == candidates.STATE_UNMADE and fields["备注"] == ""


def test_cancelled_directions_and_removed_slots_drop_rows(
    home, fake, seed_image
) -> None:
    ctx, batch_id = _confirmed(home, seed_image, limit=2)
    candidates.sync(ctx)
    slots = _slots(ctx)
    jobs = sorted({s["job_id"] for s in slots})
    assert len(jobs) == 2
    # S06 对齐槽位时去掉了一个（没发过请求的）槽位：它的预留行删掉
    gone = next(s for s in slots if s["job_id"] == jobs[0])
    gone_record = _reserved(ctx)[(gone["job_id"], gone["slot_id"])]["record_id"]
    with ctx.db.tx() as conn:
        conn.execute(
            "DELETE FROM job_slots WHERE job_id = ? AND slot_id = ?",
            (gone["job_id"], gone["slot_id"]),
        )
    out = candidates.sync(ctx)
    assert out["slots"]["dropped"] == 1 and gone_record not in fake.bitable_records
    # 停掉第二个方向：这个方向的预留行全删
    second = [
        _reserved(ctx)[(s["job_id"], s["slot_id"])]["record_id"]
        for s in slots
        if s["job_id"] == jobs[1]
    ]
    variant = next(s["variant_id"] for s in slots if s["job_id"] == jobs[1])
    out = directions.cancel(ctx, batch_id, [variant], by="tester")
    assert out["cancelled"] == [variant]
    out = candidates.sync(ctx)
    assert out["slots"]["dropped"] == len(second)
    assert not set(second) & set(fake.bitable_records)
    assert all(k[0] == jobs[0] for k in _reserved(ctx))
    assert not candidates.has_work(ctx)


def test_redo_turns_filled_rows_back_to_unmade(home, fake, seed_image) -> None:
    ctx, _ = _confirmed(home, seed_image)
    candidates.sync(ctx)
    _draw(ctx)
    candidates.sync(ctx)
    job_id = _slots(ctx)[0]["job_id"]
    filled = {
        r["record_id"]: r["slot_id"]
        for r in ctx.db.q(
            "SELECT record_id, slot_id FROM candidate_slots WHERE asset_id IS NOT NULL"
        )
    }
    orders = {r: fake.bitable_records[r][ORDER] for r in filled}
    # 重出：作废的图在候选表里的行删掉，方向回到出图前，按槽位重新预留「未生成」（排在原来的位置）
    maintenance.redo_jobs(ctx, [job_id], by="tester")
    assert not set(filled) & set(fake.bitable_records)
    out = candidates.sync(ctx)
    assert out["slots"]["reserved"] == len(_slots(ctx))
    rows = {
        r["slot_id"]: r["record_id"]
        for r in ctx.db.q("SELECT slot_id, record_id FROM candidate_slots")
    }
    for record_id, slot_id in filled.items():
        fields = fake.bitable_records[rows[slot_id]]
        assert fields[STATE] == candidates.STATE_UNMADE
        assert fields[ORDER] == orders[record_id]


def test_accept_and_dispatch_never_touch_rows_without_images(
    home, fake, seed_image
) -> None:
    ctx, batch_id = _confirmed(home, seed_image, limit=2)
    # 待审积压上限压到 1：第一个方向出完图进了选款，第二个就被积压挡住（不挡选款完成）
    set_config(home, "batch.review_backlog_limit", 1)
    ctx = reopen(home)
    candidates.sync(ctx)
    jobs = sorted({s["job_id"] for s in _slots(ctx)})
    _draw(ctx)
    blocked = ctx.db.q1(
        "SELECT status, blocked_reason FROM jobs WHERE job_id = ?", (jobs[1],)
    )
    assert (blocked["status"], blocked["blocked_reason"]) == (
        "blocked",
        "review_backlog",
    )
    candidates.sync(ctx)
    waiting = [
        r["record_id"]
        for r in ctx.db.q(
            "SELECT record_id FROM candidate_slots WHERE job_id = ? AND asset_id IS NULL",
            (jobs[1],),
        )
    ]
    assert waiting
    # 有人在没图的行上打了勾：不算数
    for record_id in waiting:
        fake.bitable_records[record_id]["选中"] = True
    (task,) = tasks.summaries(ctx)
    assert task["counts"]["jobs_unfinished"] == 0
    _age_selection(ctx)
    out = candidates.dispatch(ctx, "S01", by="feishu:ou_x")
    assert out["approved"] == [] and out["picked"] == []
    out = candidates.accept(ctx, "S01", by="feishu:ou_x")
    assert out["rejected"] and not set(waiting) & {
        _record(ctx, a) for a in out["rejected"]
    }
    for record_id in waiting:
        assert fake.bitable_records[record_id][STATE] == candidates.STATE_UNMADE
    # 选款完成后积压降下来，这个方向接着出了图：原行补上图，那个误打的勾一起清掉，还是待选
    _draw(ctx)
    candidates.sync(ctx)
    for record_id in waiting:
        fields = fake.bitable_records[record_id]
        assert fields[STATE] == candidates.STATE_PENDING and fields["选中"] is False


def test_switch_off_keeps_old_behaviour(home, fake, seed_image) -> None:
    ctx, _ = _confirmed(home, seed_image)
    set_config(home, "table.reserve_rows", False)
    ctx = reopen(home)
    # 关着：方向确认后不建表、不占行；图进选款才建表
    assert not candidates.has_work(ctx)
    assert candidates.table_info(ctx, "S01") is None
    _draw(ctx)
    candidates.sync(ctx)
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_slots") == 0
    assert candidates.status(ctx)["reserve_rows"] is False
