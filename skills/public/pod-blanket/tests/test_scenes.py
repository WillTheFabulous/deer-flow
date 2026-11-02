"""套图：选中的款交给 mooya 出场景图（假 mooya 在 mooya.test），拉回原图、打套图压缩包、写进选款表、发卡片。"""

from __future__ import annotations

import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from conftest import card_buttons, card_text, reopen, set_config
from podflow import (
    bg,
    candidates,
    cli,
    doctor,
    feishu_table,
    maintenance,
    mooya,
    notify,
    review,
    scenes,
    store,
    tasks,
    ui,
)
from podflow.stages.common import StageError
from podflow.stages.plan import asset_label_of
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_storage import _enable_oss
from test_ui import _enable_feishu, run


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


def _setup(
    home,
    seed_image,
    *,
    approve: int = 1,
    oss: bool = True,
    max_in_flight: int = 2,
    enabled: bool = True,
):
    """出一批图，在聊天框里选中前 approve 张。"""
    _enable_feishu(home)
    if oss:
        _enable_oss(home)
    set_config(home, "table.enabled", True)
    set_config(home, "scenes.enabled", enabled)
    set_config(home, "scenes.max_in_flight", max_in_flight)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    if oss:
        store.sync(ctx)
    items = review.list_reviews(ctx, types=["asset_selection"])
    for item in items[:approve]:
        review.decide(ctx, item["review_id"], "approved", by="feishu:ou_tester")
    return ctx, [i["subject_id"] for i in items[:approve]]


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _sets(ctx) -> dict[str, dict]:
    return scenes.latest_sets(ctx)


def _spawned_kinds(spawned: list[list[str]]) -> list[list[str]]:
    return [argv[2:4] for argv in spawned]


def _finish_bg(ctx) -> None:
    """假拉起的后台进程（pid 是测试进程自己）永远算活着：手动记结束，免得挡住下一次 kick。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET status = 'done' WHERE status IN ('starting', 'running')"
        )


def test_accept_hands_picked_designs_to_mooya(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    ticked = review.list_reviews(ctx, types=["asset_selection"])[0]
    record = ctx.db.scalar(
        "SELECT record_id FROM candidate_rows WHERE asset_id = ?",
        (ticked["subject_id"],),
    )
    fake.bitable_records[record]["选中"] = True
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    # 聊天框里选的 1 张 + 表里勾的 1 张：都交给 mooya
    assert out["scenes"]["queued"] == 2 and out["scenes"]["auto"] is True
    assert set(_sets(ctx)) == {picked[0], ticked["subject_id"]}
    assert all(r["status"] == "queued" for r in _sets(ctx).values())
    assert ["scenes", "sync"] in _spawned_kinds(spawned)
    summary = bg.summarize("accept", out)
    card = notify.bg_card(ctx, {"kind": "accept", "subject": "S01"}, summary, ok=True)
    assert "交给 mooya 出套图" in card_text(card) and "压缩包" not in card_text(card)
    # 再验收一次：没有新的款，不重复交
    again = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert again["scenes"]["queued"] == 0 and len(_sets(ctx)) == 2


def test_each_round_relays_changed_sets_once(monkeypatch) -> None:
    calls: list[dict] = []
    kicks: list[object] = []
    monkeypatch.setattr(scenes, "_after_sets", lambda ctx, out: calls.append(out) or {})
    monkeypatch.setattr(scenes.feishu_table, "kick", lambda ctx: kicks.append(ctx))
    relayed: set[str] = set()
    # 这一轮：交出 SS1、SS2 出完、SS3 状态变了：当场重算环节、拉起表格同步，不等进程结束
    out = {"submitted": ["SS1"], "finished": ["SS2"], "changed": ["SS2", "SS3"]}
    scenes._relay("ctx", out, relayed)
    assert calls == [{"finished": ["SS2"], "submitted": ["SS1", "SS3"]}] and kicks == ["ctx"]
    # 下一轮没有新动静：不再重复
    scenes._relay("ctx", out, relayed)
    assert len(calls) == 1 and len(kicks) == 1
    # 又有一套自愈重交：只处理它
    out["healed"] = [{"set_id": "SS4"}]
    scenes._relay("ctx", out, relayed)
    assert calls[-1] == {"finished": [], "submitted": ["SS4"]} and len(kicks) == 2


def test_sync_submits_polls_and_collects(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=3, max_in_flight=2)
    assert len(scenes.start(ctx, by="t", seed_id="S01")["queued"]) == 3
    # 同时最多 2 款在 mooya 上跑
    with mooya.Pool(ctx) as pool:
        first = {"submitted": [], "finished": [], "errors": []}
        assert scenes._pass(ctx, pool, first) == "ok"
    assert len(first["submitted"]) == 2
    states = sorted(r["status"] for r in _sets(ctx).values())
    assert states == ["queued", "submitted", "submitted"]

    out = scenes.sync(ctx)
    assert out["active"] == 0 and len(out["finished"]) == 3
    rows = _sets(ctx)
    assert {r["status"] for r in rows.values()} == {"done"}
    assert len(fake.mooya_jobs) == 3
    # 幂等键带这个业务库的编号：别的数据目录（测试、重建的库）同样从 SS0001 数起，也不会拿回这里的任务
    instance = scenes.instance_id(ctx)
    assert len(instance) == 12 and scenes.instance_id(ctx) == instance
    for job in fake.mooya_jobs.values():
        meta = job["meta"]
        assert meta["preset"] == "pod_blanket" and meta["client_ref"] in picked
        assert (
            meta["idempotency_key"]
            == f"pod-blanket:{instance}:{rows[meta['client_ref']]['set_id']}"
        )
    asset_id = picked[0]
    row = rows[asset_id]
    pictures = ctx.db.q(
        "SELECT image_id, role, path, rev FROM scene_images WHERE set_id = ? ORDER BY image_id",
        (row["set_id"],),
    )
    assert [p["image_id"] for p in pictures] == [
        "baseline",
        "scene-01",
        "scene-02",
        "scene-03",
    ]
    for pic in pictures:
        assert pic["path"].startswith(f"assets/scenes/{asset_id}/{row['set_id']}/")
        assert (home / pic["path"]).is_file()
        assert ctx.db.scalar(
            "SELECT status FROM storage_objects WHERE path = ?", (pic["path"],)
        )
    # 套图压缩包：设计图 + 白底图 + 3 张场景图，放 OSS 给签名链接
    key = row["export_key"]
    assert (
        key
        == f"pod-blanket/exports/scenes/{asset_id}/{asset_id}-scenes-{row['set_id']}.zip"
    )
    assert row["export_url"].startswith("https://") and row["export_count"] == 5
    with zipfile.ZipFile(io.BytesIO(fake.oss_objects[key][0])) as zf:
        names = zf.namelist()
    # 设计图用素材的名字（编号_方向短语），白底图和场景图照旧用编号
    assert names[0] == f"{asset_label_of(ctx, asset_id)}_设计图.png"
    assert asset_label_of(ctx, asset_id) != asset_id
    assert (
        f"{asset_id}_白底图.png" in names and f"{asset_id}_场景01_场景-1.png" in names
    )
    # 同一轮出完的按任务合并成一张卡片
    (card,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图")
    ]
    assert card["header"]["title"]["content"] == "套图出完｜S01"
    assert "**3** 款出完 · 共 9 张场景图" in card_text(card)
    assert all(r["notified_at"] for r in _sets(ctx).values())
    # 出完后表格要写套图列
    assert ["table", "sync"] in _spawned_kinds(spawned)


def test_table_columns_are_added_and_written(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    feishu_table.sync(ctx)
    # 模拟 2026-10-20 前建的表：没有套图列，表格信息里也没记
    info = feishu_table.base(ctx)
    table = fake.bitable_tables[info["table_id"]]
    scene_names = [f["field_name"] for f in feishu_table.SCENE_FIELDS]
    table["fields"] = [f for f in table["fields"] if f["field_name"] not in scene_names]
    info.pop("extra_fields")
    feishu_table._save_base(ctx, info)

    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    assert scenes.table_has_work(ctx)
    uploads = len(fake.bitable_files)
    out = feishu_table.sync(ctx)
    assert out["scenes"] == 1 and out["failed"] == 0
    assert [f["field_name"] for f in table["fields"]][-len(scene_names):] == scene_names
    assert feishu_table.base(ctx)["extra_fields"] == sorted(scene_names)
    record_id = ctx.db.scalar(
        "SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],)
    )
    record = fake.bitable_records[record_id]
    row = _sets(ctx)[picked[0]]
    assert record["套图状态"] == "已出完" and record["场景图张数"] == 3
    # 2026-10-29 起不放套图缩略图：没有「场景图」列，也不生成、不上传
    assert "场景图" not in record and len(fake.bitable_files) == uploads
    assert "场景图" not in [f["field_name"] for f in table["fields"]]
    assert record["下载套图"]["link"] == row["export_url"]
    assert not scenes.table_has_work(ctx)
    assert feishu_table.sync(ctx)["scenes"] == 0
    # 压缩包链接快到期：只续签链接，不重传缩略图
    uploads = len(fake.bitable_files)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE scene_sets SET export_expires_at = '2000-01-01T00:00:00+00:00'"
        )
    assert scenes.table_has_work(ctx)
    assert feishu_table.sync(ctx)["scenes"] == 1
    assert len(fake.bitable_files) == uploads
    renewed = _sets(ctx)[picked[0]]
    assert renewed["export_expires_at"] > "2001"
    assert fake.bitable_records[record_id]["下载套图"]["link"] == renewed["export_url"]


def test_waiting_human_then_partial(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_script = ["queued", "waiting_human"]
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    # 只剩 mooya 那边等人的：后台进程不守着，交给巡检
    assert "stopped" not in out and out["active"] == 1
    row = _sets(ctx)[picked[0]]
    assert row["status"] == "waiting_human" and row["step_label"] == "白底图等人工确认"
    (task,) = tasks.summaries(ctx)
    # 套图之后是「下载」（交付包开着）：套图还没出完，下载这一步还没到
    assert [(s["key"], s["state"]) for s in task["steps"][-2:]] == [
        ("scenes", "current"),
        ("download", "todo"),
    ]
    assert any("等人工" in line for line in task["now"])
    # 刚停下就发一张「等人工」卡片，写清卡在哪一步、去哪处理
    (parked,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"] == "套图等人工｜S01"
    ]
    assert "白底图等人工确认" in card_text(
        parked
    ) and f"S01 · {picked[0]}" in card_text(parked)
    # 巡检照样会拉起后台去查（套图进程已退出时）
    _finish_bg(ctx)
    assert scenes.kick(ctx) is not None

    fake.mooya_script = ["queued", "waiting_human", "partial"]
    scenes.sync(ctx)
    row = _sets(ctx)[picked[0]]
    assert row["status"] == "partial" and row["last_error"] == "上游失败"
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM scene_images WHERE set_id = ? AND role = 'scene'",
            (row["set_id"],),
        )
        == 2
    )
    assert row["export_url"]
    card = next(
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    )
    assert card["header"]["title"]["content"] == "套图出完｜S01"
    assert "缺图（2/3）" in card_text(card) and "还有" not in card_text(card)


def _parked_cards(fake) -> list[dict]:
    return [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图等人工")
    ]


def test_parked_sets_free_the_slot_and_notify_once(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=3, max_in_flight=2)
    scenes.start(ctx, by="t", seed_id="S01")
    assert len(scenes.sync(ctx, max_minutes=0)["submitted"]) == 2
    # 交出去的两款都停在白底图门控等人：不占名额，第三款照常交出去
    for job in fake.mooya_jobs.values():
        job.update(status="waiting_human", frozen=True)
    out = scenes.sync(ctx, max_minutes=0)
    assert len(out["parked"]) == 2 and len(out["submitted"]) == 1
    rows = _sets(ctx)
    assert sorted(r["status"] for r in rows.values()) == [
        "submitted",
        "waiting_human",
        "waiting_human",
    ]
    (card,) = _parked_cards(fake)
    text = card_text(card)
    assert card["header"]["title"]["content"] == "套图等人工｜S01"
    assert "**2** 款停在 mooya 等人工" in text and "platform_internal" in text
    parked = sorted(
        (r for r in rows.values() if r["status"] == "waiting_human"),
        key=lambda r: r["set_id"],
    )
    for row in parked:
        assert f"#{row['mooya_job_id']}" in text
        assert f"S01 · {row['asset_id']}" in text
    # 还停着：不重发
    again = scenes.sync(ctx, max_minutes=0)
    assert not again.get("parked") and len(_parked_cards(fake)) == 1
    # 有人处理了一款，它后来又被暂停：再提醒一次
    job = fake.mooya_jobs[parked[0]["mooya_job_id"]]
    job["status"] = "running"
    scenes.sync(ctx, max_minutes=0)
    assert _sets(ctx)[parked[0]["asset_id"]]["status"] == "running"
    job["status"] = "paused"
    out = scenes.sync(ctx, max_minutes=0)
    assert out["parked"] == [parked[0]["set_id"]]
    cards = _parked_cards(fake)
    assert len(cards) == 2
    assert "**1** 款" in card_text(cards[-1]) and "已暂停" in card_text(cards[-1])
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'scenes_parked'") == 3
    )


def test_finished_card_counts_sets_still_in_progress(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=3, max_in_flight=2)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx, max_minutes=0)
    first, second = sorted(fake.mooya_jobs.values(), key=lambda j: j["id"])
    first.update(status="done", frozen=True)
    second.update(status="waiting_human", frozen=True)
    # 第一款出完、第二款停下等人、第三款交出去：卡片写明还有几款在出
    scenes.sync(ctx, max_minutes=0)
    (card,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    ]
    assert card["header"]["title"]["content"] == "套图出完 1 款｜S01"
    assert "还有 2 款在出（其中 1 款等人工）" in card_text(card)


def test_enqueue_rechecks_inside_the_write_transaction(
    home, fake, seed_image, spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    assert len(scenes.enqueue(ctx, by="t", seed_id="S01")["queued"]) == 1
    # 另一个进程在这一套插进去之前读的数据：写事务里复查，不会再插第二套（--force 也一样）
    monkeypatch.setattr(scenes, "latest_sets", lambda ctx, asset_ids=None: {})
    for force in (False, True):
        out = scenes.enqueue(ctx, by="t", seed_id="S01", force=force)
        assert out["queued"] == [] and "排队中" in out["skipped"][0]["reason"]
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM scene_sets WHERE asset_id = ?", (picked[0],)
        )
        == 1
    )


def test_failed_without_images_can_be_restarted(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_script = ["queued", "failed"]
    fake.mooya_no_images = True
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    row = _sets(ctx)[picked[0]]
    assert row["status"] == "failed" and row["last_error"] == "上游失败"
    assert not row["export_key"]
    card = next(
        c for c in fake.feishu_cards if "套图" in c["header"]["title"]["content"]
    )
    assert card["header"]["title"]["content"] == "套图有没出成的｜S01"
    # 任务列表给「出套图」重出（选款还有待选的，按钮不抢「去选款」的主色）
    (item,) = ui.tasks_view(ctx)["items"]
    scene_buttons = [a for a in item["actions"] if a["action"] == ui.ACTION_SCENES]
    assert scene_buttons == [
        {
            "action": "pod_scenes",
            "label": "出套图（1 款）",
            "style": "default",
            "seed": "S01",
        }
    ]
    # 选款完成时不自动重出没出成的，手动出套图会重出
    auto = scenes.after_accept(ctx, "S01", by="t")
    assert auto["queued"] == 0 and auto["skipped"] == 1
    fake.mooya_no_images = False
    fake.mooya_script = ["queued", "done"]
    assert len(scenes.start(ctx, by="t", seed_id="S01")["queued"]) == 1
    scenes.sync(ctx)
    assert _sets(ctx)[picked[0]]["status"] == "done"


def test_service_error_backs_off_until_fixed(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_token = "rotated"
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    assert "令牌" in out["stopped"]
    assert _sets(ctx)[picked[0]]["status"] == "queued"
    assert "HTTP 401" in scenes.status(ctx)["service_error"]
    # 隔一阵再试，不每分钟撞墙
    assert scenes.kick(ctx) is None
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert checks["mooya 机器调用接口连得上、令牌有效、预设可用"]["ok"] is False
    assert checks["套图没有卡在 mooya 配置错误上"]["level"] == "warn"
    # 令牌修好、过了退避时间：接着交
    fake.mooya_token = "test-mooya-token"
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn,
            scenes.SERVICE_ERROR_META_KEY,
            json.dumps({"at": "2000-01-01T00:00:00+00:00", "error": "old"}),
        )
    _finish_bg(ctx)
    assert scenes.kick(ctx) is not None
    scenes.sync(ctx)
    assert _sets(ctx)[picked[0]]["status"] == "done"
    assert scenes.status(ctx)["service_error"] is None


def test_transient_errors_keep_the_queue(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_fail["submit"] = 503
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    assert "连不上 mooya" in out["stopped"]
    row = _sets(ctx)[picked[0]]
    assert row["status"] == "queued" and row["attempts"] == 0
    fake.mooya_fail.clear()
    scenes.sync(ctx)
    assert _sets(ctx)[picked[0]]["status"] == "done"


def test_rejected_request_fails_the_set(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_fail["submit"] = 400
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    row = _sets(ctx)[picked[0]]
    assert row["status"] == "failed" and "HTTP 400" in row["last_error"]


def test_image_download_errors_retry_then_fail(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "scenes.max_attempts", 2)
    ctx = reopen(home)
    fake.mooya_fail["image"] = 404
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    row = _sets(ctx)[picked[0]]
    assert row["status"] == "failed" and "连续 2 次" in row["last_error"]


def test_cli_start_status_show_cancel(home, fake, seed_image, capsys, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    asset_id = picked[0]
    out = run(capsys, "scenes", "start", "--seed", "S01", "--by", "t")
    assert [q["asset_id"] for q in out["queued"]] == [asset_id]
    assert out["sync_task"].startswith("BG")
    again = run(capsys, "scenes", "start", "--asset", asset_id, "--by", "t")
    assert again["queued"] == [] and "排队中" in again["skipped"][0]["reason"]
    status = run(capsys, "scenes", "status", "--seed", "S01")
    assert status["sets"][0]["status"] == "queued"
    assert status["seeds"]["S01"]["active"] == 1
    set_id = status["sets"][0]["set_id"]
    shown = run(capsys, "scenes", "show", set_id)
    assert shown["status_label"] == "排队中" and shown["images"] == []
    cancelled = run(capsys, "scenes", "cancel", set_id, "--by", "t")
    assert cancelled["status"] == "cancelled" and cancelled["notified_at"]
    for argv, words in (
        (("scenes", "start", "--seed", "S01;x", "--by", "t"), "任务编号不对"),
        (("scenes", "start", "--asset", "X1", "--by", "t"), "素材编号不对"),
        (
            ("scenes", "start", "--seed", "S01", "--asset", asset_id, "--by", "t"),
            "二选一",
        ),
        (("scenes", "show", "SSx"), "套图编号不对"),
        (("scenes", "cancel", set_id, "--by", "t"), "不用停"),
    ):
        code, err = _cli(capsys, *argv)
        assert code == 2 and words in err["detail"], err
    # 停掉的可以重出；出完后不加 --force 不重出
    run(capsys, "scenes", "start", "--seed", "S01", "--by", "t")
    run(capsys, "scenes", "sync")
    done = run(capsys, "scenes", "start", "--seed", "S01", "--by", "t")
    assert done["queued"] == [] and "已出过套图" in done["skipped"][0]["reason"]
    forced = run(capsys, "scenes", "start", "--seed", "S01", "--force", "--by", "t")
    assert len(forced["queued"]) == 1
    other = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    code, out = _cli(capsys, "scenes", "start", "--asset", other, "--by", "t")
    assert code == 0 and "不是已选中的款" in out["skipped"][0]["reason"]
    # 已经有套图进程在跑：不再拉第二个
    code, busy = _cli(capsys, "scenes", "sync", "--detach")
    assert code == 2 and "已经在跑" in busy["detail"]
    _finish_bg(ctx)
    code, out = _cli(capsys, "scenes", "sync", "--detach")
    assert code == 0 and out["task_id"].startswith("BG")


def test_refresh_pulls_regenerated_images(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    row = _sets(ctx)[picked[0]]
    downloads = [c for c in fake.mooya_calls if "/images/" in c[1]]
    assert len(downloads) == 4
    # 在 mooya 里重生了第 2 张：只拉这一张，重打压缩包，表格那一行再写一次
    (job,) = fake.mooya_jobs.values()
    job["revs"]["scene-02"] = "r2"
    # 还在排队的套图不能拉
    queued = scenes.enqueue(ctx, by="t", asset_ids=picked, force=True)["queued"][0][
        "set_id"
    ]
    with pytest.raises(StageError, match="还在出"):
        scenes.refresh(ctx, queued)
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM scene_sets WHERE set_id = ?", (queued,))
    out = scenes.refresh(ctx, row["set_id"])
    assert len([c for c in fake.mooya_calls if "/images/" in c[1]]) == 5
    revs = {i["image_id"]: i["rev"] for i in out["images"]}
    assert revs["scene-02"] == "r2" and revs["scene-01"] == "r1"
    assert _sets(ctx)[picked[0]]["table_state"] is None
    assert scenes.table_has_work(ctx)


def test_task_list_and_gallery_show_scenes(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=2)
    (item,) = ui.tasks_view(ctx)["items"]
    assert "套图 0/2" in item["lines"][0]
    assert {
        "action": "pod_scenes",
        "label": "出套图（2 款）",
        "style": "default",
        "seed": "S01",
    } in item["actions"]
    scenes.start(ctx, by="t", seed_id="S01")
    (task,) = tasks.summaries(ctx)
    assert (
        task["counts"]["scenes_active"] == 2 and task["counts"]["scenes_missing"] == 0
    )
    assert any("2 款在 mooya 出图" in line for line in task["now"])
    scenes.sync(ctx)
    (item,) = ui.tasks_view(ctx)["items"]
    # 出完就打了这个批次的交付包，放在这个任务的开头
    assert item["lines"][0].startswith("交付包：B001 2 款")
    assert "套图 2/2 ✓" in item["lines"][1]
    assert ui.ACTION_SCENES not in [a["action"] for a in item["actions"]]
    gallery = ui.gallery(ctx)
    assert all("套图：已出完 3 张" in it["lines"][-1] for it in gallery["items"])


def test_purge_removes_scene_sets_and_files(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    store.sync(ctx)
    row = _sets(ctx)[picked[0]]
    zip_key = row["export_key"]
    scene_keys = [k for k in fake.oss_objects if "/assets/scenes/" in k]
    assert zip_key in fake.oss_objects and len(scene_keys) == 4
    # 还有在出的套图时不让删
    scenes.start(ctx, by="t", asset_ids=picked, force=True)
    with pytest.raises(StageError, match="还在出"):
        maintenance.purge_old(ctx, seeds=["S01"], by="t")
    queued = next(r for r in _sets(ctx).values() if r["status"] == "queued")
    scenes.cancel(ctx, queued["set_id"], by="t")
    plan = maintenance.purge_old(ctx, seeds=["S01"], by="t", dry_run=True)
    assert plan["counts"]["scene_sets"] == 2
    maintenance.purge_old(ctx, seeds=["S01"], by="t")
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_sets") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_images") == 0
    assert zip_key not in fake.oss_objects
    assert not [k for k in fake.oss_objects if "/assets/scenes/" in k]
    assert not (home / "assets" / "scenes" / picked[0]).exists()


def test_disabled_keeps_five_steps_and_no_handoff(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1, enabled=False)
    (task,) = tasks.summaries(ctx)
    assert [s["key"] for s in task["steps"]] == [
        "analyze",
        "plan",
        "pick",
        "generate",
        "select",
    ]
    assert task["counts"]["scenes_missing"] == 0
    candidates.sync(ctx)
    out = candidates.accept(ctx, "S01", by="t")
    assert out["scenes"] == {"enabled": False}
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_sets") == 0
    with pytest.raises(StageError, match="scenes.enabled 为 false"):
        scenes.start(ctx, by="t", seed_id="S01")
    assert scenes.kick(ctx) is None
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert checks["套图（scenes.enabled）"]["ok"] is True
    assert "套图" not in "".join(ui.gallery(ctx)["items"][0]["lines"])


def test_manual_mode_reports_missing_after_accept(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "scenes.auto_after_accept", False)
    ctx = reopen(home)
    candidates.sync(ctx)
    out = candidates.accept(ctx, "S01", by="t")
    assert out["scenes"] == {"enabled": True, "auto": False, "missing": 1}
    card = notify.bg_card(
        ctx, {"kind": "accept", "subject": "S01"}, bg.summarize("accept", out), ok=True
    )
    assert "还有 1 款没出套图" in card_text(card)
    assert "任务列表" in [b["text"]["content"] for b in card_buttons(card)]
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_sets") == 0


# ---------------------------------------------------------------- mooya 交付清单（命名跟 mooya 交付包）


def _zip_names(fake, key: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(fake.oss_objects[key][0])) as zf:
        return zf.namelist()


def _zip_bytes(fake, key: str, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(fake.oss_objects[key][0])) as zf:
        return zf.read(name)


def _image_bytes(home, ctx, set_id: str, image_id: str) -> bytes:
    path = ctx.db.scalar(
        "SELECT path FROM scene_images WHERE set_id = ? AND image_id = ?",
        (set_id, image_id),
    )
    return (home / path).read_bytes()


def test_zip_uses_mooya_delivery_names(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_delivery = True
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    asset_id = picked[0]
    row = _sets(ctx)[asset_id]
    assert row["status"] == "done"
    # 设计图 + mooya 交付包里的文件（001–003 场景图、004 尺码图），不放白底图
    names = _zip_names(fake, row["export_key"])
    assert names == [
        f"{asset_label_of(ctx, asset_id)}_设计图.png",
        "001.png",
        "002.png",
        "003.png",
        "004.png",
    ]
    assert row["export_count"] == 5
    # 清单里不在 images 的尺码图也拉回来，但不算场景图
    downloads = [
        c[1].rsplit("/", 1)[-1] for c in fake.mooya_calls if "/images/" in c[1]
    ]
    assert sorted(downloads) == [
        "baseline",
        "file-9001",
        "scene-01",
        "scene-02",
        "scene-03",
    ]
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM scene_images WHERE set_id = ? AND role = 'scene'",
            (row["set_id"],),
        )
        == 3
    )
    assert _zip_bytes(fake, row["export_key"], "004.png") == _image_bytes(
        home, ctx, row["set_id"], "file-9001"
    )
    shown = scenes.show(ctx, row["set_id"])
    assert [
        (f["seq"], f["file_name"], f["image_id"], f["role"]) for f in shown["files"]
    ] == [
        (1, "001.png", "scene-01", "scene"),
        (2, "002.png", "scene-02", "scene"),
        (3, "003.png", "scene-03", "scene"),
        (4, "004.png", "file-9001", "insert"),
    ]
    (card,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    ]
    assert "3 张场景图" in card_text(card) and "白底图" not in card_text(card)


def test_delivery_order_change_is_followed_on_refresh(
    home, fake, seed_image, spawned
) -> None:
    """在 mooya 交付工作台里调了顺序：refresh 后压缩包按新顺序编号，不重复下载没变的图。"""
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_delivery = True
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    row = _sets(ctx)[picked[0]]
    downloads = len([c for c in fake.mooya_calls if "/images/" in c[1]])
    fake.mooya_delivery_order = [2, 0, 1]
    scenes.refresh(ctx, row["set_id"])
    assert len([c for c in fake.mooya_calls if "/images/" in c[1]]) == downloads
    row = _sets(ctx)[picked[0]]
    key = row["export_key"]
    assert _zip_bytes(fake, key, "001.png") == _image_bytes(
        home, ctx, row["set_id"], "scene-03"
    )
    assert _zip_bytes(fake, key, "002.png") == _image_bytes(
        home, ctx, row["set_id"], "scene-01"
    )
    assert [f["image_id"] for f in scenes.delivery_files(ctx, row["set_id"])] == [
        "scene-03",
        "scene-01",
        "scene-02",
        "file-9001",
    ]


def test_selected_table_has_no_scene_thumbnails(
    home, fake, seed_image, spawned
) -> None:
    """2026-10-29 起「选中的款」不放套图缩略图：有交付清单时也只写状态、张数和下载链接，不生成、不上传缩略图。"""
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_delivery = True
    fake.mooya_delivery_order = [1, 0, 2]
    feishu_table.sync(ctx)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    uploads = len(fake.bitable_files)
    assert feishu_table.sync(ctx)["scenes"] == 1
    record_id = ctx.db.scalar(
        "SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],)
    )
    record = fake.bitable_records[record_id]
    assert "场景图" not in record and len(fake.bitable_files) == uploads
    assert record["套图状态"] == "已出完" and record["场景图张数"] == 3
    assert record["下载套图"]["link"] == _sets(ctx)[picked[0]]["export_url"]


def test_refresh_all_renames_old_sets(home, fake, seed_image, capsys, spawned) -> None:
    """mooya 上线交付清单前出的套图（旧命名）：refresh --all 重拉清单、换上交付包的文件名，不重新出图。"""
    ctx, picked = _setup(home, seed_image, approve=2)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    old = {a: _sets(ctx)[a] for a in picked}
    assert all(
        f"{a}_白底图.png" in _zip_names(fake, old[a]["export_key"]) for a in picked
    )
    submitted = len(fake.mooya_jobs)
    fake.mooya_delivery = True
    code, err = _cli(capsys, "scenes", "refresh", "SS0001", "--all")
    assert code == 2 and "二选一" in err["detail"]
    code, err = _cli(capsys, "scenes", "refresh", "--all", "--seed", "X1")
    assert code == 2 and "任务编号不对" in err["detail"]
    code, out = _cli(capsys, "scenes", "refresh", "--all", "--seed", "S01")
    assert code == 0 and sorted(out["refreshed"]) == sorted(
        r["set_id"] for r in old.values()
    )
    assert out["failed"] == [] and len(fake.mooya_jobs) == submitted
    for asset_id in picked:
        row = _sets(ctx)[asset_id]
        assert row["set_id"] == old[asset_id]["set_id"]
        assert _zip_names(fake, row["export_key"])[1:] == [
            "001.png",
            "002.png",
            "003.png",
            "004.png",
        ]
        assert row["table_state"] is None
    # 重拉完拉起了打交付包的后台；它跑着的时候不让再开一个
    code, err = _cli(capsys, "scenes", "refresh", "--all", "--detach")
    assert code == 2 and "已经在跑" in err["detail"]
    _finish_bg(ctx)
    code, out = _cli(capsys, "scenes", "refresh", "--all", "--detach")
    assert code == 0 and out["task_id"].startswith("BG")
    assert ["scenes", "refresh"] in _spawned_kinds(spawned)


def test_unsafe_delivery_file_names_are_cleaned() -> None:
    assert scenes._file_name("../../etc/001.png", 1) == "001.png"
    assert scenes._file_name("a b:c.png", 2) == "a-b-c.png"
    assert scenes._file_name("", 3) == "003"
    assert scenes._file_name("..", 4) == "004"


def test_purge_removes_delivery_files(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    fake.mooya_delivery = True
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_files") == 4
    maintenance.purge_old(ctx, seeds=["S01"], by="t")
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_files") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_sets") == 0
