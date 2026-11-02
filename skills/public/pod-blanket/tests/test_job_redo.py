"""方向按新规则重出（podctl job redo）：作废已出的图、关待审、删候选表行、重置槽位与出图调用，
回到 S06 按现在的提示词重新写指令、重新出图；选中的款、没出完的任务、结果未知的请求不能重出。"""

from __future__ import annotations

import json

import pytest

from conftest import SLOTS_PER_JOB, reopen
from podflow import cli, maintenance, review
from podflow.stages.common import StageError
from podflow.util import loads, now_iso
from podflow.worker import Worker
from test_pipeline import bootstrap


def _done_job(ctx, seed_image) -> dict:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    job = dict(ctx.db.q1("SELECT * FROM jobs"))
    assert job["status"] == "complete"
    return job


def _selection(ctx) -> set[str]:
    return {
        r["subject_id"] for r in review.list_reviews(ctx, types=["asset_selection"])
    }


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_redo_voids_images_and_generates_again(ctx, fake, seed_image) -> None:
    job = _done_job(ctx, seed_image)
    old = [
        r["asset_id"] for r in ctx.db.q("SELECT asset_id FROM assets ORDER BY asset_id")
    ]
    assert len(old) == SLOTS_PER_JOB and _selection(ctx) == set(old)
    assert ctx.db.scalar("SELECT status FROM batches") == "done"
    # 候选表里有一行：重出时删掉
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO candidate_rows(asset_id, seed_id, state, updated_at) VALUES(?, 'S01', '待选', ?)",
            (old[0], now_iso()),
        )

    out = maintenance.redo_jobs(ctx, [job["job_id"]], by="tester")
    assert out["assets"] == old and out["variants"] == [job["variant_id"]]
    assert out["reviews"] == SLOTS_PER_JOB and out["voided_calls"] == SLOTS_PER_JOB
    assert out["batches_reopened"] == [job["batch_id"]]
    assert {r[0] for r in ctx.db.q("SELECT status FROM assets")} == {"superseded"}
    assert _selection(ctx) == set()
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_rows") == 0
    row = ctx.db.q1("SELECT status, stage, render_spec_json, attempt FROM jobs")
    assert (row["status"], row["stage"], row["render_spec_json"], row["attempt"]) == (
        "ready",
        "S06",
        None,
        0,
    )
    slots = ctx.db.q("SELECT status, attempts, asset_id FROM job_slots")
    assert {(s["status"], s["attempts"], s["asset_id"]) for s in slots} == {
        ("pending", 0, None)
    }
    voided = [
        loads(r["request_meta_json"])
        for r in ctx.db.q(
            "SELECT request_meta_json FROM provider_calls WHERE kind = 'image'"
        )
    ]
    assert all(m["voided"] and m["voided_by"] == "tester" for m in voided)
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'job_redo'") == 1

    # 再出一轮：重新写指令、9 张新图，旧图不回选款；幂等键接着编号
    p06 = fake.stage_counts["P06"]
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == p06 + 1
    new = [
        r["asset_id"]
        for r in ctx.db.q(
            "SELECT asset_id FROM assets WHERE status != 'superseded' ORDER BY asset_id"
        )
    ]
    assert len(new) == SLOTS_PER_JOB and not set(new) & set(old)
    assert _selection(ctx) == set(new)
    keys = [
        r["idempotency_key"]
        for r in ctx.db.q(
            "SELECT idempotency_key FROM provider_calls WHERE kind = 'image'"
        )
    ]
    assert len(keys) == len(set(keys)) == 2 * SLOTS_PER_JOB
    assert ctx.db.scalar("SELECT status FROM jobs") == "complete"
    assert ctx.db.scalar("SELECT status FROM batches") == "done"


def test_redo_refuses_selected_unfinished_and_unknown(ctx, fake, seed_image) -> None:
    job = _done_job(ctx, seed_image)
    asset = ctx.db.scalar("SELECT asset_id FROM assets ORDER BY asset_id LIMIT 1")
    call = ctx.db.scalar(
        "SELECT call_id FROM provider_calls WHERE kind = 'image' ORDER BY call_id LIMIT 1"
    )

    def update(sql: str, *params) -> None:
        with ctx.db.tx() as conn:
            conn.execute(sql, params)

    update("UPDATE assets SET human_decision = 'approved' WHERE asset_id = ?", asset)
    with pytest.raises(StageError, match="已经选中"):
        maintenance.redo_jobs(ctx, [job["job_id"]], by="tester")
    update("UPDATE assets SET human_decision = 'pending' WHERE asset_id = ?", asset)

    update("UPDATE provider_calls SET status = 'unknown' WHERE call_id = ?", call)
    with pytest.raises(StageError, match="对账"):
        maintenance.redo_jobs(ctx, [job["job_id"]], by="tester")
    update("UPDATE provider_calls SET status = 'succeeded' WHERE call_id = ?", call)

    update("UPDATE jobs SET status = 'ready' WHERE job_id = ?", job["job_id"])
    with pytest.raises(StageError, match="还没出完"):
        maintenance.redo_jobs(ctx, [job["job_id"]], by="tester")
    update("UPDATE jobs SET status = 'complete' WHERE job_id = ?", job["job_id"])

    for bad in (["X1"], ["V9999"], []):
        with pytest.raises(StageError):
            maintenance.redo_jobs(ctx, bad, by="tester")
    # 被拒绝的几次什么都没改
    assert ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'superseded'") == 0
    assert len(_selection(ctx)) == SLOTS_PER_JOB


def test_redo_cli_dry_run_by_direction_and_paused(
    home, fake, seed_image, capsys
) -> None:
    ctx = reopen(home)
    job = _done_job(ctx, seed_image)
    code, out = _cli(
        capsys, "job", "redo", job["variant_id"], "--by", "tester", "--dry-run"
    )
    assert code == 0 and out["dry_run"] is True and out["jobs"] == [job["job_id"]]
    assert len(out["assets"]) == SLOTS_PER_JOB and out["reviews"] == SLOTS_PER_JOB
    ctx = reopen(home)
    assert ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'superseded'") == 0
    # 流水线暂停时只排队，恢复后再出
    assert _cli(capsys, "pause", "--reason", "测试")[0] == 0
    code, out = _cli(capsys, "job", "redo", job["job_id"], "--by", "tester")
    assert code == 0 and out["dry_run"] is False and "podctl resume" in out["next"]
    ctx = reopen(home)
    assert ctx.db.scalar("SELECT status FROM jobs") == "ready"
    code, err = _cli(capsys, "job", "redo", job["job_id"], "--by", "tester")
    assert code == 2 and "还没出完" in err["detail"]
