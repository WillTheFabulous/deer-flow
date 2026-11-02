"""彻底删掉旧数据（maintenance purge-old）：先列清单不改；真删时库里、选款表、OSS、本机都清掉，新版本原样留着。"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from conftest import make_png, reopen, set_config
from podflow import bg, feishu_table, maintenance, review, store
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.stages.common import StageError
from podflow.util import now_iso
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_storage import _enable_oss
from test_ui import _enable_feishu, run

TABLES = (
    "seeds",
    "analyses",
    "hypotheses",
    "families",
    "batches",
    "variants",
    "jobs",
    "job_slots",
    "assets",
    "reviews",
    "feedback",
    "events",
    "bg_tasks",
    "storage_objects",
    "table_rows",
)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


def _counts(ctx) -> dict[str, int]:
    return {t: int(ctx.db.scalar(f"SELECT COUNT(*) FROM {t}")) for t in TABLES}


def _ids(ctx, sql: str) -> list[str]:
    return [r[0] for r in ctx.db.q(sql)]


def _world(home, fake, seed_image, tmp_path):
    """S01：v1 出过图（选中一张、写进选款表、其余待选）→ 按新字段重拆成 v2 并策划了一批待挑选；S02 只接入了。"""
    _enable_feishu(home)
    _enable_oss(home)
    set_config(home, "table.enabled", True)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=2)
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)
    item = review.list_reviews(ctx, types=["asset_selection"])[0]
    review.decide(ctx, item["review_id"], "approved", by="feishu:ou_tester")
    feishu_table.sync(ctx)
    store.sync(ctx)
    seedmod.analyze(ctx, "S01", reanalyze=True)
    new_batch = planmod.plan_batch(ctx, "F01", limit=1)["batch_id"]
    other = tmp_path / "other.png"
    other.write_bytes(make_png(77, size=(988, 1282)))
    seedmod.add_seed(ctx, other)
    store.sync(ctx)
    reports = home / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "M1_B001_archived.png").write_bytes(b"old")
    (reports / "notes.png").write_bytes(b"keep")
    thumbs = home / "cache" / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    (thumbs / "seed-S02.jpg").write_bytes(b"thumb")
    (thumbs / "seed-S01.jpg").write_bytes(b"thumb")
    return ctx, item["subject_id"], new_batch


def test_dry_run_lists_everything_and_changes_nothing(
    home, fake, seed_image, tmp_path, spawned, capsys
) -> None:
    ctx, approved, new_batch = _world(home, fake, seed_image, tmp_path)
    before = _counts(ctx)
    oss_before = set(fake.oss_objects)
    out = run(
        capsys,
        "maintenance",
        "purge-old",
        "--family",
        "F01",
        "--keep-version",
        "2",
        "--seed",
        "S02",
        "--by",
        "tester",
        "--dry-run",
    )
    assert out["dry_run"] is True
    assert out["batches"] == ["B001"] and new_batch == "B002"
    assert out["family_versions"] == ["F01@v1"] and out["seeds"] == ["S02"]
    assert out["analyses"] == ["S01-A01"] and out["approved_assets"] == [approved]
    counts = out["counts"]
    assert (counts["variants"], counts["jobs"], counts["table_rows"]) == (2, 2, 1)
    assert counts["assets"] == ctx.db.scalar("SELECT COUNT(*) FROM assets")
    assert counts["open_reviews"] >= 1 and counts["hypotheses"] == 1
    assert out["local_files"] == [
        "cache/thumbs/seed-S02.jpg",
        "reports/M1_B001_archived.png",
    ]
    assert _counts(ctx) == before and set(fake.oss_objects) == oss_before
    assert len(fake.bitable_records) == 1


def test_purge_removes_old_rows_files_and_table_rows_but_keeps_the_new_round(
    home, fake, seed_image, tmp_path, spawned
) -> None:
    ctx, approved, new_batch = _world(home, fake, seed_image, tmp_path)
    old_variants = _ids(ctx, "SELECT variant_id FROM variants WHERE batch_id = 'B001'")
    old_assets = _ids(ctx, "SELECT asset_id FROM assets")
    new_variants = _ids(
        ctx, f"SELECT variant_id FROM variants WHERE batch_id = '{new_batch}'"
    )

    out = maintenance.purge_old(
        ctx, family_id="F01", keep_version=2, seeds=["S02"], by="tester"
    )
    assert out["dry_run"] is False
    assert out["table"] == {"deleted": 1, "errors": []} and not fake.bitable_records
    assert out["files"]["errors"] == [] and out["files"]["objects"] > 0

    # 新版本这一轮原样留着
    assert _ids(ctx, "SELECT batch_id FROM batches") == [new_batch]
    assert (
        _ids(ctx, "SELECT variant_id FROM variants ORDER BY variant_id") == new_variants
    )
    assert _ids(ctx, "SELECT family_version FROM families") == [2]
    assert _ids(ctx, "SELECT seed_id FROM seeds") == ["S01"]
    assert _ids(ctx, "SELECT analysis_id FROM analyses") == ["S01-A02"]
    seed = seedmod.get_seed(ctx, "S01")
    assert seed["latest_analysis_id"] == "S01-A02"
    assert (
        ctx.db.scalar(
            "SELECT analysis_id FROM hypotheses WHERE hypothesis_set_id = ?",
            (seed["latest_hypothesis_set_id"],),
        )
        == "S01-A02"
    )
    assert _ids(ctx, "SELECT subject_id FROM reviews WHERE status = 'open'") == [
        new_batch
    ]
    # 旧的都没了
    for table in ("jobs", "job_slots", "assets", "feedback", "table_rows"):
        assert ctx.db.scalar(f"SELECT COUNT(*) FROM {table}") == 0, table
    assert not ctx.db.q1("SELECT 1 FROM reviews WHERE subject_type = 'asset_selection'")
    assert not ctx.db.q1("SELECT 1 FROM reviews WHERE subject_id = 'F01@v1'")
    gone = {"B001", "S02", *old_variants, *old_assets}
    scopes = {r[0] for r in ctx.db.q("SELECT scope FROM events")}
    assert not gone & scopes
    family_events = [
        json.loads(r["payload_json"] or "{}").get("version")
        for r in ctx.db.q("SELECT payload_json FROM events WHERE scope = 'F01'")
    ]
    assert 1 not in family_events and 2 in family_events
    assert ctx.db.q1(
        "SELECT 1 FROM events WHERE scope = 'maintenance' AND event = 'purge_old'"
    )
    # 文件：OSS、登记与本机目录都清掉，S01 的参考图还在
    paths = _ids(ctx, "SELECT path FROM storage_objects")
    assert paths and all(p.startswith("assets/seeds/S01/") for p in paths)
    assert set(fake.oss_objects) == {f"pod-blanket/{p}" for p in paths}
    for vid in old_variants:
        assert not (home / "assets" / "families" / "F01" / vid).exists()
    assert not (home / "assets" / "seeds" / "S02").exists()
    assert (home / "assets" / "seeds" / "S01").is_dir()
    assert not (home / "cache" / "thumbs" / "seed-S02.jpg").exists()
    assert (home / "cache" / "thumbs" / "seed-S01.jpg").exists()
    assert not (home / "reports" / "M1_B001_archived.png").exists()
    assert (home / "reports" / "notes.png").exists()

    # 再跑一次什么都不删；S02 已经没了，只记在 missing_seeds 里
    again = maintenance.purge_old(
        ctx, family_id="F01", keep_version=2, seeds=["S02"], by="tester"
    )
    assert again["missing_seeds"] == ["S02"] and again["seeds"] == []
    assert set(again["counts"].values()) == {0}, again["counts"]


def test_purge_refuses_when_unsafe(home, fake, seed_image, tmp_path, spawned) -> None:
    ctx, _, _ = _world(home, fake, seed_image, tmp_path)
    before = _counts(ctx)
    with pytest.raises(StageError, match="最新是 v2"):
        maintenance.purge_old(ctx, family_id="F01", keep_version=1, by="tester")
    with pytest.raises(StageError, match="不能整张删"):
        maintenance.purge_old(
            ctx, family_id="F01", keep_version=2, seeds=["S01"], by="tester"
        )
    with pytest.raises(StageError, match="至少给"):
        maintenance.purge_old(ctx, by="tester")
    with pytest.raises(StageError, match="keep-version"):
        maintenance.purge_old(ctx, family_id="F01", by="tester")
    # 要删的任务还在出图
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = 'J0001'")
    with pytest.raises(StageError, match="J0001 正在出图"):
        maintenance.purge_old(ctx, family_id="F01", keep_version=2, by="tester")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'complete' WHERE job_id = 'J0001'")
    # worker 正在推进要删的任务，或者在跑全量出图
    for info, message in (
        ({"only_jobs": ["J0002"]}, "J0002"),
        ({}, "全量出图"),
    ):
        with ctx.db.tx() as conn:
            conn.execute("DELETE FROM worker_lease")
            conn.execute(
                "INSERT INTO worker_lease(name, owner, pid, started_at, heartbeat_at, lease_until, info_json)"
                " VALUES('worker', 'other', 1, ?, ?, '2999-01-01T00:00:00+00:00', ?)",
                (now_iso(), now_iso(), json.dumps(info)),
            )
        with pytest.raises(StageError, match=message):
            maintenance.purge_old(ctx, family_id="F01", keep_version=2, by="tester")
    # worker 只在推进别的任务时照样能删
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE worker_lease SET info_json = ?",
            (json.dumps({"only_jobs": ["J9999"]}),),
        )
    assert _counts(ctx) == before
    out = maintenance.purge_old(ctx, family_id="F01", keep_version=2, by="tester")
    assert out["batches"] == ["B001"] and out["seeds"] == []
    assert _ids(ctx, "SELECT seed_id FROM seeds ORDER BY seed_id") == ["S01", "S02"]
