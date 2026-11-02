"""套图交付包：每个裂变批次一个包（每款一个文件夹），链接在任务列表开头、候选表「套图交付包」列、套图出完卡片。"""

from __future__ import annotations

import datetime as dt
import io
import os
import zipfile
from types import SimpleNamespace

import pytest

from conftest import card_buttons, card_text, confirm_all, reopen, set_config
from podflow import (
    bg,
    candidates,
    doctor,
    feishu_table,
    maintenance,
    packages,
    review,
    scenes,
    store,
    ui,
)
from podflow.oss import OSSClient, OSSError
from podflow.stages import plan as planmod
from podflow.stages.plan import asset_label_of
from podflow.util import loads, utcnow
from podflow.worker import Worker
from test_scenes import _cli, _finish_bg, _sets, _setup, _spawned_kinds
from test_ui import run

PACKAGE = candidates.PACKAGE_FIELD


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


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """交付包的对象名带到秒：每取一次时间往后走一秒，同一个用例里重打的包不会撞名。"""
    state = {"now": utcnow()}

    def tick() -> dt.datetime:
        state["now"] += dt.timedelta(seconds=1)
        return state["now"]

    monkeypatch.setattr(packages, "utcnow", tick)


def _finished_cards(fake) -> list[dict]:
    return [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    ]


def _links(card: dict) -> dict[str, str | None]:
    return {b["text"]["content"]: b.get("url") for b in card_buttons(card)}


def _zip_names(fake, key: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(fake.oss_objects[key][0])) as zf:
        return zf.namelist()


def test_package_built_when_the_batch_finishes(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=2)
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    assert [b["batch_id"] for b in out["packages"]["built"]] == ["B001"]
    pkg = packages.get(ctx, "S01", "B001")
    # 两款，每款设计图 + 白底图 + 3 张场景图
    assert pkg["item_count"] == 2 and pkg["file_count"] == 10
    key = pkg["export_key"]
    assert key.startswith("pod-blanket/exports/packages/S01/B001/S01-B001-")
    names = _zip_names(fake, key)
    assert len(names) == 10
    for asset_id in picked:
        label = asset_label_of(ctx, asset_id)
        assert f"{label}/{label}_设计图.png" in names
        assert f"{label}/{asset_id}_白底图.png" in names
        assert f"{label}/{asset_id}_场景01_场景-1.png" in names
    # 从磁盘流式上传：带长度与 MD5，本机不留临时文件
    put = next(r for r in fake.oss_requests if r[0] == "PUT" and r[1] == key)
    headers = {k.lower(): v for k, v in put[2].items()}
    assert headers["content-length"] == str(len(fake.oss_objects[key][0]))
    assert headers["content-md5"] and headers["content-type"] == "application/zip"
    assert not list((home / "exports" / "packages").rglob("*.zip"))
    # 下载名带中文（RFC 5987），ASCII 兜底是对象名
    assert pkg["export_name"] == "S01_B001_套图交付包.zip"
    assert "filename%2A%3DUTF-8%27%27S01_B001_" in pkg["export_url"]
    # 套图出完卡片：写交付包、带下载按钮
    (card,) = _finished_cards(fake)
    assert "B001 交付包：2 款" in card_text(card)
    assert _links(card)["下载 B001 交付包"] == pkg["export_url"]
    # 没有别的要做：巡检不再拉进程
    assert packages.due(ctx) == []
    _finish_bg(ctx)
    assert scenes.kick(ctx) is None


def test_package_folders_use_mooya_delivery_names(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=2)
    fake.mooya_delivery = True
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    assert pkg["item_count"] == 2 and pkg["file_count"] == 10
    names = _zip_names(fake, pkg["export_key"])
    # 每款的文件夹和每款的套图压缩包一样：设计图 + mooya 交付包的 001–004
    for asset_id in picked:
        label = asset_label_of(ctx, asset_id)
        assert [n.split("/", 1)[1] for n in names if n.startswith(f"{label}/")] == [
            f"{label}_设计图.png",
            "001.png",
            "002.png",
            "003.png",
            "004.png",
        ]
    assert packages.due(ctx) == []
    # 在 mooya 交付工作台里调了顺序（图没变）：重拉后这个批次的交付包要重打
    fake.mooya_delivery_order = [2, 1, 0]
    scenes.refresh(ctx, _sets(ctx)[picked[0]]["set_id"])
    assert [(t["action"], t["batch_id"]) for t in packages.due(ctx)] == [
        ("build", "B001")
    ]


def test_busy_batch_waits_until_everything_is_out(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=3, max_in_flight=2)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx, max_minutes=0)
    first = min(fake.mooya_jobs.values(), key=lambda j: j["id"])
    first.update(status="done", frozen=True)
    out = scenes.sync(ctx, max_minutes=0)
    # 一款出完、一款在出、一款刚交出去：先不打包
    assert out["packages"]["built"] == [] and packages.due(ctx) == []
    assert packages.get(ctx, "S01", "B001") is None
    (view,) = packages.by_seed(ctx)["S01"]
    assert (view["ready"], view["done"], view["busy"]) == (False, 1, 2)
    (item,) = ui.tasks_view(ctx)["items"]
    assert item["lines"][0] == "交付包：B001 套图 1/3 出完，出完后打包"
    assert not [x for x in item["links"] if x["label"].startswith("交付包")]
    (card,) = _finished_cards(fake)
    assert "交付包" not in card_text(card)
    for job in fake.mooya_jobs.values():
        job.pop("frozen", None)
    scenes.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    assert pkg["item_count"] == 3 and pkg["file_count"] == 15
    assert _links(_finished_cards(fake)[-1])["下载 B001 交付包"] == pkg["export_url"]


def test_parked_set_does_not_block_and_is_added_later(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=2, max_in_flight=2)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx, max_minutes=0)
    first, second = sorted(fake.mooya_jobs.values(), key=lambda j: j["id"])
    first.update(status="done", frozen=True)
    second.update(status="waiting_human", frozen=True)
    scenes.sync(ctx, max_minutes=0)
    # 停在 mooya 等人工的那款不挡：先把出完的打包
    pkg = packages.get(ctx, "S01", "B001")
    assert pkg["item_count"] == 1
    (view,) = packages.by_seed(ctx)["S01"]
    assert view["ready"] and view["parked"] == 1 and view["busy"] == 0
    line = ui.tasks_view(ctx)["items"][0]["lines"][0]
    assert line.startswith("交付包：B001 1 款 · ") and line.endswith("，1 款等人工")
    # 等人工的处理完出完了：重打，换新对象、删旧的
    second["status"] = "done"
    scenes.sync(ctx, max_minutes=0)
    rebuilt = packages.get(ctx, "S01", "B001")
    assert rebuilt["item_count"] == 2 and rebuilt["export_key"] != pkg["export_key"]
    assert pkg["export_key"] not in fake.oss_objects
    assert rebuilt["export_key"] in fake.oss_objects


def test_refresh_pack_and_repack(home, fake, seed_image, capsys, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    first = packages.get(ctx, "S01", "B001")
    # 在 mooya 里重生了一张：重新拉图后交付包要重打；没有套图在出，拉起的是 scenes pack
    (job,) = fake.mooya_jobs.values()
    job["revs"]["scene-02"] = "r2"
    _finish_bg(ctx)
    scenes.refresh(ctx, _sets(ctx)[picked[0]]["set_id"])
    assert [d["action"] for d in packages.due(ctx)] == ["build"]
    assert ["scenes", "pack"] in _spawned_kinds(spawned)
    out = run(capsys, "scenes", "pack")
    assert [b["batch_id"] for b in out["packages"]["built"]] == ["B001"]
    second = packages.get(ctx, "S01", "B001")
    assert second["fingerprint"] != first["fingerprint"]
    assert first["export_key"] not in fake.oss_objects
    assert run(capsys, "scenes", "pack")["packages"]["built"] == []
    # 手动重打：内容没变也重打，链接会换
    again = run(capsys, "scenes", "repack", "--seed", "S01", "--batch", "B001")
    third = packages.get(ctx, "S01", "B001")
    assert again["items"] == 1 and third["export_url"] != second["export_url"]
    assert third["fingerprint"] == second["fingerprint"]
    listed = run(capsys, "scenes", "packages", "--seed", "S01")
    (item,) = listed["packages"]
    assert item["label"] == "S01 · B001" and item["download"] == third["export_url"]
    assert item["size"].endswith("KB") and listed["due"] == []
    status = run(capsys, "scenes", "status", "--seed", "S01")
    assert [p["batch_id"] for p in status["packages"]] == ["B001"]
    for argv, words in (
        (("scenes", "repack", "--seed", "S01", "--batch", "X1"), "批次编号不对"),
        (("scenes", "repack", "--seed", "S1x", "--batch", "B001"), "任务编号不对"),
        (("scenes", "packages", "--seed", "S01;x"), "任务编号不对"),
        (("scenes", "repack", "--seed", "S01", "--batch", "B009"), "没有出完的套图"),
    ):
        code, err = _cli(capsys, *argv)
        assert code == 2 and words in err["detail"], err


def test_rerun_keeps_the_old_package_until_it_finishes(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    first = packages.get(ctx, "S01", "B001")
    scenes.start(ctx, by="t", asset_ids=picked, force=True)
    # 重出还在排队：旧包照留，写明出完重打
    assert packages.due(ctx) == []
    (view,) = packages.by_seed(ctx)["S01"]
    assert view["ready"] and view["busy"] == 1
    assert "还有 1 款在出，出完重打" in ui.tasks_view(ctx)["items"][0]["lines"][0]
    # 重出没出成：包里还是上一套，不重打
    fake.mooya_script = ["queued", "failed"]
    fake.mooya_no_images = True
    scenes.sync(ctx)
    assert _sets(ctx)[picked[0]]["status"] == "failed"
    assert packages.due(ctx) == []
    assert packages.get(ctx, "S01", "B001")["export_key"] == first["export_key"]
    # 再重出成功：换成新的一套
    fake.mooya_script = ["queued", "done"]
    fake.mooya_no_images = False
    scenes.start(ctx, by="t", asset_ids=picked)
    scenes.sync(ctx)
    rebuilt = packages.get(ctx, "S01", "B001")
    assert loads(rebuilt["set_ids_json"]) == [_sets(ctx)[picked[0]]["set_id"]]
    assert rebuilt["export_key"] != first["export_key"]


def test_candidate_table_column_links_and_renewal(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    table = candidates.table_info(ctx, "S01")
    fields = fake.bitable_tables[table["table_id"]]["fields"]
    # 新建的候选表：「套图交付包」在第 2 列，带表头说明
    assert [f["field_name"] for f in fields[:3]] == ["素材", PACKAGE, "缩略图"]
    assert fields[1]["type"] == 15 and "同一批次" in fields[1]["description"]["text"]
    assert candidates.package_field_ready(ctx, table["table_id"])
    records = candidates._batch_records(ctx, "S01", "B001")
    assert records and all(PACKAGE not in fake.bitable_records[r] for r in records)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    assert candidates.has_work(ctx)
    assert candidates.sync(ctx)["packages"] == 1
    for record in records:
        cell = fake.bitable_records[record][PACKAGE]
        assert cell["link"] == pkg["export_url"]
        assert cell["text"].startswith("下载 B001 交付包（1 款 · ")
        assert cell["text"].endswith(" 前有效）")
    assert packages.get(ctx, "S01", "B001")["table_state"] == packages.table_key(pkg)
    assert candidates.status(ctx)["packages_pending"] == 0
    assert candidates.sync(ctx)["packages"] == 0
    # 链接快到期：巡检续签，表里跟着换
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE scene_packages SET export_expires_at = '2000-01-01T00:00:00+00:00'"
        )
    assert [d["action"] for d in packages.due(ctx)] == ["renew"]
    assert packages.build_due(ctx)["renewed"] == [
        {"seed_id": "S01", "batch_id": "B001"}
    ]
    renewed = packages.get(ctx, "S01", "B001")
    assert renewed["export_expires_at"] > "2001"
    assert renewed["export_key"] == pkg["export_key"]
    candidates.sync(ctx)
    assert all(
        fake.bitable_records[r][PACKAGE]["link"] == renewed["export_url"]
        for r in records
    )


def test_rows_added_after_packing_carry_the_link(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    # 包打好之后才建的表：新行一进表就带着链接
    candidates.sync(ctx)
    records = candidates._batch_records(ctx, "S01", "B001")
    assert records
    assert all(
        fake.bitable_records[r][PACKAGE]["link"] == pkg["export_url"] for r in records
    )


def test_old_candidate_tables_get_the_column_appended(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "scenes.batch_packages", False)
    ctx = reopen(home)
    candidates.sync(ctx)
    table = candidates.table_info(ctx, "S01")
    fields = fake.bitable_tables[table["table_id"]]["fields"]
    assert PACKAGE not in [f["field_name"] for f in fields]
    assert not candidates.package_field_ready(ctx, table["table_id"])
    assert candidates.status(ctx)["package_columns_pending"] == 0
    # 打开交付包：补列失败记次数，满上限不再试
    set_config(home, "scenes.batch_packages", True)
    ctx = reopen(home)
    fake.bitable_fail.add(("POST", "/fields"))
    assert candidates.has_work(ctx)
    for _ in range(feishu_table.MAX_ATTEMPTS):
        candidates.sync(ctx)
    status = candidates.status(ctx)
    assert status["package_columns_pending"] == 0 and status["package_columns_error"]
    # 修好后 table sync --retry：补在最后一列（飞书接口不能调列顺序）
    fake.bitable_fail.clear()
    assert candidates.sync(ctx, retry=True)["package_columns"] == 1
    assert fields[-1]["field_name"] == PACKAGE
    assert candidates.package_field_ready(ctx, table["table_id"])
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    candidates.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    records = candidates._batch_records(ctx, "S01", "B001")
    assert records and all(
        fake.bitable_records[r][PACKAGE]["link"] == pkg["export_url"] for r in records
    )


def test_scene_rows_under_the_control_row_carry_the_package(
    home, fake, seed_image, spawned
) -> None:
    set_config(home, "table.control_row", True)
    ctx, picked = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    candidates.sync(ctx)
    scene_row = ctx.db.scalar(
        "SELECT record_id FROM candidate_scene_rows WHERE asset_id = ?", (picked[0],)
    )
    control_row = ctx.db.scalar(
        "SELECT record_id FROM candidate_actions WHERE seed_id = 'S01' AND kind = 'accept'"
    )
    package_row = ctx.db.scalar(
        "SELECT record_id FROM candidate_actions WHERE seed_id = 'S01' AND kind = 'package'"
        " AND batch_id = 'B001'"
    )
    # 表顶的套图行和交付包行都写所在批次的交付包；按钮行不写
    assert fake.bitable_records[scene_row][PACKAGE]["link"] == pkg["export_url"]
    assert fake.bitable_records[package_row][PACKAGE]["link"] == pkg["export_url"]
    assert PACKAGE not in fake.bitable_records[control_row]
    records = candidates._batch_records(ctx, "S01", "B001")
    assert not {scene_row, control_row, package_row} & set(records)
    assert all(
        fake.bitable_records[r][PACKAGE]["link"] == pkg["export_url"] for r in records
    )
    assert not candidates.has_work(ctx)
    # 重打后候选行和套图行一起换成新链接
    (job,) = fake.mooya_jobs.values()
    job["revs"]["scene-02"] = "r2"
    _finish_bg(ctx)
    scenes.refresh(ctx, _sets(ctx)[picked[0]]["set_id"])
    packages.build_due(ctx)
    rebuilt = packages.get(ctx, "S01", "B001")
    assert rebuilt["export_url"] != pkg["export_url"]
    assert candidates.has_work(ctx)
    candidates.sync(ctx)
    assert fake.bitable_records[scene_row][PACKAGE]["link"] == rebuilt["export_url"]
    assert all(
        fake.bitable_records[r][PACKAGE]["link"] == rebuilt["export_url"]
        for r in records
    )


def test_later_batches_get_their_own_packages(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    candidates.sync(ctx)
    # 同一个任务继续裂变出 B002，选中一款出套图
    plan = planmod.plan_batch(ctx, "F01", limit=1)
    assert plan["batch_id"] == "B002" and plan["accepted"]
    confirm_all(ctx, plan)
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)
    newer = ctx.db.scalar(
        "SELECT r.review_id FROM reviews r JOIN assets a ON a.asset_id = r.subject_id"
        " JOIN variants v ON v.variant_id = a.variant_id"
        " WHERE r.subject_type = 'asset_selection' AND r.status = 'open' AND v.batch_id = 'B002'"
        " ORDER BY r.review_id LIMIT 1"
    )
    review.decide(ctx, newer, "approved", by="feishu:ou_tester")
    _finish_bg(ctx)
    queued = scenes.start(ctx, by="t", seed_id="S01")["queued"]
    assert len(queued) == 1
    scenes.sync(ctx)
    views = packages.by_seed(ctx)["S01"]
    assert [v["batch_id"] for v in views] == ["B002", "B001"]
    assert all(v["ready"] for v in views)
    b1 = packages.get(ctx, "S01", "B001")
    b2 = packages.get(ctx, "S01", "B002")
    assert b1["export_key"] != b2["export_key"]
    # 任务列表：开头一行两个批次，新批次在前；交付包链接排在候选表前面
    candidates.sync(ctx)
    (item,) = ui.tasks_view(ctx)["items"]
    assert item["lines"][0].startswith("交付包：B002 1 款 · ")
    assert " · B001 1 款 · " in item["lines"][0]
    assert [x["label"] for x in item["links"]] == [
        "交付包 B002",
        "交付包 B001",
        "候选表",
    ]
    assert [x["url"] for x in item["links"][:2]] == [b2["export_url"], b1["export_url"]]
    # 候选表：每一行写它所在批次的包
    for batch, pkg in (("B001", b1), ("B002", b2)):
        rows = candidates._batch_records(ctx, "S01", batch)
        assert rows and all(
            fake.bitable_records[r][PACKAGE]["link"] == pkg["export_url"] for r in rows
        )
    # 任务明细：每个批次标题带交付包链接
    heads = {g["batch_id"]: g["head"] for g in ui.history_view(ctx, "F01")["groups"]}
    assert f"[交付包 1 款]({b2['export_url']})" in heads["B002"]
    assert f"[交付包 1 款]({b1['export_url']})" in heads["B001"]
    # B002 这一轮的套图出完卡片只带 B002 的包
    links = _links(_finished_cards(fake)[-1])
    assert "下载 B002 交付包" in links and "下载 B001 交付包" not in links


def test_package_removed_when_nothing_finished_is_left(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    candidates.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    records = candidates._batch_records(ctx, "S01", "B001")
    assert all(fake.bitable_records[r][PACKAGE] for r in records)
    # 选中的款被改判掉：批次里没有出完的套图了，删包、清掉表里的链接
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE assets SET human_decision = 'rejected' WHERE asset_id = ?",
            (picked[0],),
        )
    assert [d["action"] for d in packages.due(ctx)] == ["remove"]
    assert packages.build_due(ctx)["removed"] == [
        {"seed_id": "S01", "batch_id": "B001"}
    ]
    emptied = packages.get(ctx, "S01", "B001")
    assert not packages.ready(emptied) and emptied["item_count"] == 0
    assert pkg["export_key"] not in fake.oss_objects
    assert "S01" not in packages.by_seed(ctx)
    assert "交付包" not in ui.tasks_view(ctx)["items"][0]["lines"][0]
    assert candidates.status(ctx)["packages_pending"] == 1
    candidates.sync(ctx)
    assert all(fake.bitable_records[r][PACKAGE] is None for r in records)
    assert candidates.status(ctx)["packages_pending"] == 0


def test_failed_packing_stops_retrying_until_repacked(
    home, fake, seed_image, spawned
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "scenes.batch_packages", False)
    ctx = reopen(home)
    scenes.start(ctx, by="t", seed_id="S01")
    out = scenes.sync(ctx)
    assert out["packages"]["skipped"] and packages.by_seed(ctx) == {}
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_packages") == 0
    set_config(home, "scenes.batch_packages", True)
    ctx = reopen(home)
    # 上传一直失败：同一份内容满 MAX_ATTEMPTS 次不再自动试
    for n in range(1, packages.MAX_ATTEMPTS + 1):
        fake.oss_fail_puts = 1
        failed = packages.build_due(ctx)["failed"]
        assert [f["batch_id"] for f in failed] == ["B001"]
        pkg = packages.get(ctx, "S01", "B001")
        assert pkg["attempts"] == n and "ServiceUnavailable" in pkg["last_error"]
    assert packages.due(ctx) == [] and not packages.ready(pkg)
    assert not list((home / "exports" / "packages").rglob("*.zip"))
    (view,) = packages.by_seed(ctx)["S01"]
    assert view["error"]
    assert "B001 交付包没打成" in ui.tasks_view(ctx)["items"][0]["lines"][0]
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert checks["套图交付包没有打包失败卡住的"]["ok"] is False
    # 修好后手动重打
    packages.repack(ctx, "S01", "B001")
    fixed = packages.get(ctx, "S01", "B001")
    assert packages.ready(fixed) and fixed["attempts"] == 0 and not fixed["last_error"]
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert checks["套图交付包没有打包失败卡住的"]["ok"] is True


def test_local_storage_keeps_the_zip_on_disk(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1, oss=False)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    pkg = packages.get(ctx, "S01", "B001")
    assert pkg["export_key"] is None and pkg["export_url"] is None
    path = home / pkg["export_path"]
    assert path.is_file() and zipfile.is_zipfile(path)
    # 没有下载链接：表里不写，任务列表只写情况、不放按钮
    assert packages.cell(ctx, pkg) is None and packages.table_key(pkg) == ""
    (item,) = ui.tasks_view(ctx)["items"]
    assert item["lines"][0].startswith("交付包：B001 1 款 · ")
    assert not [x for x in item["links"] if x["label"].startswith("交付包")]
    # 重打：旧 zip 删掉，只留新的
    packages.repack(ctx, "S01", "B001")
    again = packages.get(ctx, "S01", "B001")
    assert not path.exists() and (home / again["export_path"]).is_file()


def test_purge_removes_packages(home, fake, seed_image, spawned) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    key = packages.get(ctx, "S01", "B001")["export_key"]
    plan = maintenance.purge_old(ctx, seeds=["S01"], by="t", dry_run=True)
    assert plan["counts"]["scene_packages"] == 1
    maintenance.purge_old(ctx, seeds=["S01"], by="t")
    assert ctx.db.scalar("SELECT COUNT(*) FROM scene_packages") == 0
    assert key not in fake.oss_objects


def test_task_list_line_caps_batches(ctx) -> None:
    views = [
        {
            "batch_id": f"B00{n}",
            "ready": True,
            "url": f"https://example.com/{n}.zip",
            "items": n,
            "bytes": n * 1024**2,
            "expires_at": "2027-01-21T00:00:00+00:00",
            "busy": 0,
            "stale": False,
            "parked": 0,
            "done": n,
            "approved": n,
            "error": None,
            # B004 标过已下载，B003 标的是重打前的旧包
            "downloaded": n == 4,
            "download_outdated": n == 3,
        }
        for n in (4, 3, 2, 1)
    ]
    line, links, marks = ui._package_parts(ctx, {"seed_id": "S01", "packages": views})
    assert line == (
        "交付包：B004 4 款 · 4MB（01-21 前有效） · 已下载 ✓"
        " · B003 3 款 · 3MB（01-21 前有效），重打过，下载新包后再标"
        " · B002 2 款 · 2MB（01-21 前有效） · 更早的 1 批在任务明细"
    )
    assert [x["label"] for x in links] == ["交付包 B004", "交付包 B003", "交付包 B002"]
    # 还没标（或标的是旧包）的每批一个「B… 已下载」
    assert [(m["label"], m["batch"]) for m in marks] == [
        ("B003 已下载", "B003"),
        ("B002 已下载", "B002"),
    ]
    assert all(
        m["action"] == ui.ACTION_DOWNLOADED and m["seed"] == "S01" for m in marks
    )
    assert ui._package_parts(ctx, {"packages": []}) == (None, [], [])
    assert packages.size_text(9 * 1024) == "9KB"
    assert packages.size_text(int(1.3 * 1024**3)) == "1.3GB"


def test_put_file_streams_from_disk(fake, tmp_path) -> None:
    data = os.urandom(3 * 1024 * 1024 + 17)
    path = tmp_path / "big.zip"
    path.write_bytes(data)
    client = OSSClient(
        bucket="bucket",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        access_key_id="ak",
        access_key_secret="sk",
        upload_rate_kbps=1024 * 1024,
    )
    client.put_file("pod-blanket/x/big.zip", path, content_type="application/zip")
    assert fake.oss_objects["pod-blanket/x/big.zip"][0] == data
    method, key, headers = fake.oss_requests[-1]
    lowered = {k.lower(): v for k, v in headers.items()}
    assert (method, key) == ("PUT", "pod-blanket/x/big.zip")
    assert lowered["x-oss-forbid-overwrite"] == "true"
    assert lowered["content-length"] == str(len(data))
    # 同名再传：不许覆盖时 OSS 拒绝
    with pytest.raises(OSSError, match="FileAlreadyExists"):
        client.put_file("pod-blanket/x/big.zip", path)
