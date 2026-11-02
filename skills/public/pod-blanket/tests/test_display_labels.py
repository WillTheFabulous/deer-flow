"""飞书卡片和 POD 对话里的称呼：任务、批次、方向和图片的编号都带上名字（labels）。"""

from __future__ import annotations

import pytest

from conftest import card_text, confirm_all, reopen, set_config
from podflow import directions, images, labels, notify, report, review, scenes, ui
from podflow.db import log_event
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.stages.common import task_label
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_scenes import _setup as scenes_setup
from test_scenes import spawned  # noqa: F401
from test_ui import run

DESC = "动物三伙伴水彩"
# 假 P05 给第一个方向写的标题
TITLE = "主题方案1"


@pytest.fixture
def named(ctx, fake, seed_image):
    """出完一个方向（9 张进选款）并给 S01 起名：返回 (ctx, 任务称呼, 第一张待选的素材编号)。"""
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    seedmod.set_seed_name(ctx, "S01", DESC, by="tester")
    first = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    return ctx, task_label(ctx, "S01"), first


def test_ref_and_task_for_every_kind_of_id(named) -> None:
    ctx, name, asset = named
    assert name.startswith(f"S01 {DESC} ")
    assert labels.ref(ctx, "S01") == name
    assert labels.ref(ctx, "F01") == f"{name} · F01"
    assert labels.ref(ctx, "B001") == f"{name} · B001"
    assert labels.ref(ctx, "F01@v1") == f"{name} · F01 v1"
    assert labels.ref(ctx, asset) == f"{asset}_{TITLE}"
    assert labels.ref(ctx, "V0001") == f"V0001 {TITLE}"
    assert labels.ref(ctx, "J0001") == f"J0001 {TITLE}"
    for ref in ("S01", "F01", "F01@v1", "B001", "V0001", "J0001", asset):
        assert labels.task(ctx, ref) == name, ref
    # 不认识、查不到的编号原样返回，也没有所属任务
    for ref in ("RV0001", "SS0001", "C000001", "IMG99999", "V9999", "J9999", "B999"):
        assert labels.ref(ctx, ref) == ref
        assert labels.task(ctx, ref) is None
    assert labels.ref(ctx, None) == labels.ref(ctx, "") == ""


def test_without_a_name_only_ids_show(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    assert labels.ref(ctx, "S01") == "S01" and labels.ref(ctx, "B001") == "B001"
    assert labels.ref(ctx, "F01@v1") == "F01 v1"
    assert labels.task(ctx, "V0001") == "S01"
    assert ui._seed_label(ctx, "S01") == "参考图 S01"


def test_bulk_asset_names_take_one_query_per_chunk(named, monkeypatch) -> None:
    ctx, name, _ = named
    ids = [
        r["asset_id"] for r in ctx.db.q("SELECT asset_id FROM assets ORDER BY asset_id")
    ]
    sqls: list[str] = []
    query = ctx.db.q

    def counting(sql, params=()):
        sqls.append(sql)
        return query(sql, params)

    monkeypatch.setattr(ctx.db, "q", counting)
    found = labels.assets(ctx, [*ids, "IMG99999"])
    assert len(sqls) == 1
    assert found["IMG99999"] == {"label": "IMG99999", "task": None}
    assert all(found[a] == {"label": f"{a}_{TITLE}", "task": name} for a in ids)
    sqls.clear()
    monkeypatch.setattr(labels, "BATCH", 4)
    assert labels.assets(ctx, ids) == {a: found[a] for a in ids}
    assert len(sqls) == -(-len(ids) // 4)


def test_selection_card_names_the_task_and_the_image(named) -> None:
    ctx, name, asset = named
    item = ui.reviews_page(ctx, limit=1, types=["asset_selection"])["items"][0]
    label = f"{asset}_{TITLE}"
    # 回调参数仍是编号
    assert item["subject_id"] == asset
    assert item["title"].startswith(f"{item['review_id']} · 选款 · {label}")
    assert [i["label"] for i in item["images"]] == [f"原图 {name}", f"候选 {label}"]


def test_workbench_and_timeline_name_tasks_and_batches(ctx, fake, seed_image) -> None:
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    family_id = seedmod.analyze(ctx, seed["seed_id"])["steps"][-1]["family_id"]
    plan = planmod.plan_batch(ctx, family_id, limit=1)
    seedmod.set_seed_name(ctx, "S01", DESC, by="tester")
    name = task_label(ctx, "S01")

    view = ui.dashboard(ctx)
    text = "\n".join(view["lines"])
    assert f"**系列**：{name} · F01 v1 " in text
    assert f"**最近批次**：{name} · B001（F01，1 个方向）" in text
    assert f"{name} · B001（F01）：1 个方向待挑选，已选 0 个" in view["progress"]
    assert any(
        s["text"].startswith(f"{name} 的 1 个裂变方向待你挑选")
        for s in view["next_steps"]
    )

    confirm_all(ctx, plan)
    assert directions.progress_lines(ctx) == [
        f"{name} · B001 选中的 1 个方向：已出完 0 个，还剩 1 个"
    ]
    assert (
        f"{name} · B001（F01，1 个任务）：已完成 0，待出图 1"
        in ui.dashboard(ctx)["progress"]
    )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "BG0001",
            "bg_finished",
            {"kind": "plan", "subject": "F01", "status": "done"},
        )
    lines = ui.timeline(ctx)["lines"]
    assert any(line.endswith(f"新增参考图 {name}") for line in lines)
    assert any(
        line.endswith(f"{name} · B001 选了 1 个方向出图（tester）") for line in lines
    )
    assert any(line.endswith(f"后台裂变策划 {name} · F01 完成") for line in lines)

    running = [
        {"kind": "plan", "subject": "F01", "step": "S05", "elapsed_minutes": 1},
        {"kind": "worker", "subject": None, "step": None, "elapsed_minutes": 2},
    ]
    assert ui._task_lines(ctx, running) == [
        f"{name} · F01 裂变策划中：S05（已用 1 分钟）",
        "出图中（已用 2 分钟）",
    ]


def test_direction_and_detail_cards_open_with_the_task_name(named) -> None:
    ctx, name, asset = named
    head = f"参考图 {name} · 系列 F01 v1"
    assert ui.directions_view(ctx, "B001")["lines"][0].startswith(head)
    assert ui.history_view(ctx, "F01")["lines"][0].startswith(head)
    fixing = [{"kind": "fix", "subject": asset, "step": None, "elapsed_minutes": 3}]
    assert ui._task_lines(ctx, fixing) == [f"{asset}_{TITLE} 修改中（已用 3 分钟）"]


def test_gallery_and_originals_use_image_names(named) -> None:
    ctx, name, asset = named
    review.decide(
        ctx,
        review.find_review(ctx, "asset_selection", asset),
        "approved",
        by="tester",
    )
    label = f"{asset}_{TITLE}"
    (item,) = ui.gallery(ctx)["items"]
    assert item["asset_id"] == asset and item["title"] == label
    assert item["lines"][0] == f"{name} · B001 · V0001"
    assert [i["label"] for i in item["images"]] == [label]

    other = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    out = ui.originals(ctx, [asset, other])
    (sent,) = out["files"]
    assert sent["label"] == label and sent["filename"].startswith(f"{label}.")
    assert out["refused"] == [
        {"asset_id": other, "label": f"{other}_{TITLE}", "reason": "不是已选中的款"}
    ]


def test_notify_cards_name_images_tasks_and_batches(named) -> None:
    ctx, name, asset = named
    label = f"{asset}_{TITLE}"
    failed = notify.bg_card(
        ctx,
        {"kind": "fix", "subject": asset, "log_path": "x.log"},
        {"error": "boom"},
        ok=False,
    )
    assert failed["header"]["title"]["content"] == f"修改失败｜{label}"
    planned = notify.bg_card(
        ctx,
        {"kind": "plan", "subject": "F01", "log_path": "x.log"},
        {"error": "boom"},
        ok=False,
    )
    assert planned["header"]["title"]["content"] == f"裂变策划失败｜{name} · F01"
    fix = notify._fix_card(ctx, asset, {})
    assert fix["header"]["title"]["content"] == f"修改还没完成｜{label}"

    worker = notify.bg_card(
        ctx,
        {"kind": "worker", "subject": None},
        {"jobs": 1, "stop_reason": "queue_empty", "batch_reports": ["B001"]},
        ok=True,
    )
    assert f"已生成批次反馈：{name} · B001" in card_text(worker)
    described = notify.bg_card(
        ctx,
        {"kind": "describe", "subject": None},
        {"described": ["S01"], "skipped": 0, "failed": []},
        ok=True,
    )
    assert f"画面描述已补写：{name}" in card_text(described)
    extracted = notify.bg_card(
        ctx,
        {"kind": "extract", "subject": "S01"},
        {"model": "gpt-image-2.5-flare"},
        ok=True,
    )
    assert f"**{name}** 的平面图案已提取" in card_text(extracted)

    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE families SET status = 'exhausted', exhaustion_reason = '没有新方向'"
        )
    assert f"系列 {name} · F01 已耗尽：没有新方向" in report.snapshot(ctx)["alerts"]


def test_scene_cards_name_each_design(home, fake, seed_image, spawned) -> None:  # noqa: F811
    ctx, picked = scenes_setup(home, seed_image, approve=1)
    seedmod.set_seed_name(ctx, "S01", DESC, by="tester")
    name, label = task_label(ctx, "S01"), f"{picked[0]}_{TITLE}"
    fake.mooya_script = ["queued", "waiting_human"]
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    parked = next(
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图等人工")
    )
    # mooya 里的任务名不变（去 mooya 里按它找），条目开头写图片名字
    text = card_text(parked)
    assert f"- {label}：白底图等人工确认" in text and f"「S01 · {picked[0]}」" in text
    (row,) = scenes.status(ctx, "S01")["sets"]
    assert row["label"] == label and row["task"] == name
    shown = scenes.show(ctx, row["set_id"])
    assert shown["label"] == label and shown["task"] == name

    fake.mooya_script = ["queued", "waiting_human", "partial"]
    scenes.sync(ctx)
    done = next(
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    )
    assert f"- {label}：缺图（2/3）" in card_text(done)
    pictures = next(e for e in done["elements"] if e["tag"] == "column_set")
    assert pictures["columns"][0]["elements"][1]["content"] == label


def test_cli_outputs_carry_names_for_the_operator(named, capsys) -> None:
    ctx, name, asset = named
    label = f"{asset}_{TITLE}"
    listed = run(capsys, "review", "list", "--type", "asset_selection")
    assert listed["reviews"] and all(
        r["label"] == f"{r['subject_id']}_{TITLE}" and r["task"] == name
        for r in listed["reviews"]
    )
    status = run(capsys, "status")
    assert status["families"][0]["task"] == name
    assert status["batches"][0]["task"] == name
    shown = run(capsys, "asset", asset)
    assert shown["label"] == label and shown["task"] == name
    job = run(capsys, "job", "show", "J0001")
    assert job["label"] == f"J0001 {TITLE}" and job["task"] == name
    assert all(a["label"] == f"{a['asset_id']}_{TITLE}" for a in job["assets"])
    assert run(capsys, "job", "list")["jobs"][0]["label"] == f"J0001 {TITLE}"
    assert run(capsys, "directions", "show", "B001")["task"] == name
    assert run(capsys, "directions", "history", "F01")["task"] == name
    rid = review.find_review(ctx, "asset_selection", asset)
    decided = run(
        capsys, "review", "decide", rid, "--decision", "approved", "--by", "tester"
    )
    assert decided["label"] == label


def test_contact_sheet_original_tile_carries_the_task_name(
    named, tmp_path, monkeypatch
) -> None:
    ctx, name, _ = named
    plain = review.contact_sheet(ctx, tmp_path / "plain.png", batch_id="B001")
    # 没配中文字体：图上只写 ASCII 编号，名字放在 label 里给人设讲
    assert plain["tiles"][0]["lines"] == ["original S01"]
    assert plain["tiles"][0]["label"] == name
    set_config(ctx.home, "review.sheet_font", "fonts/cjk.ttf")
    monkeypatch.setattr(images, "label_font", lambda path, size=18: images._font(size))
    ctx = reopen(ctx.home)
    sheet = review.contact_sheet(ctx, tmp_path / "cjk.png", batch_id="B001")
    assert sheet["tiles"][0]["lines"] == [f"原图 {name}"]
    assert all(t["label"] == f"{t['id']}_{TITLE}" for t in sheet["tiles"][1:])
