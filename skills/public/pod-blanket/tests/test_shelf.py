"""任务归档（历史任务）：自动 / 手动归档、调出、有新动静自动调出、候选表挪进挪出「已归档」文件夹、
任务列表与历史任务卡、命令行。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import card_buttons, card_text, reopen, set_config
from podflow import (
    candidates,
    cli,
    doctor,
    maintenance,
    packages,
    report,
    review,
    scenes,
    shelf,
    tasks,
    ui,
)
from podflow.stages.common import StageError
from podflow.stages.plan import plannable_families
from podflow.util import iso, parse_iso
from test_candidates import _age_selection
from test_scenes import _finish_bg, _setup, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

# 假拉起后台进程（表格同步、套图、策划）：只要它的副作用，不看拉起了什么
pytestmark = pytest.mark.usefixtures("spawned")
BY = "feishu:ou_tester"


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _reasons(out: dict) -> dict[str, str]:
    return {b["seed_id"]: b["reason"] for b in out["blocked"]}


def _eligible(out: dict) -> list[str]:
    return [e["seed_id"] for e in out["eligible"]]


def _age_shelf(ctx, seconds: int = 120) -> None:
    """把归档 / 调出时间往前挪：时间戳只到秒，免得同一秒里的「完成」和「调出」分不出先后。"""
    for row in ctx.db.q("SELECT seed_id, archived_at, restored_at FROM seed_shelf"):
        shifted = [
            iso(parse_iso(v) - dt.timedelta(seconds=seconds)) if v else None
            for v in (row["archived_at"], row["restored_at"])
        ]
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE seed_shelf SET archived_at = ?, restored_at = ? WHERE seed_id = ?",
                (*shifted, row["seed_id"]),
            )


def _age_facts(ctx, seconds: int) -> None:
    """把选款验收、定下选款、套图结束、标已下载的时间往前挪（判断「这次完成」在不在上一次归档 / 调出之后用）。"""
    for table, column, key in (
        ("candidate_tables", "accepted_at", "seed_id"),
        ("reviews", "decided_at", "review_id"),
        ("scene_sets", "finished_at", "set_id"),
    ):
        for row in ctx.db.q(
            f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL"
        ):
            with ctx.db.tx() as conn:
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {key} = ?",
                    (
                        iso(parse_iso(row[column]) - dt.timedelta(seconds=seconds)),
                        row[key],
                    ),
                )
    for row in ctx.db.q("SELECT seed_id, batch_id, marked_at FROM package_downloads"):
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE package_downloads SET marked_at = ? WHERE seed_id = ? AND batch_id = ?",
                (
                    iso(parse_iso(row["marked_at"]) - dt.timedelta(seconds=seconds)),
                    row["seed_id"],
                    row["batch_id"],
                ),
            )


def _download_all(ctx) -> list[str]:
    """打好的、还没标的交付包都标成已下载（人下载过了）；返回标了哪几批。"""
    marked = []
    for seed_id, views in packages.by_seed(ctx).items():
        for view in views:
            if view["ready"] and not view["stale"] and not view["downloaded"]:
                packages.mark_downloaded(
                    ctx, seed_id, view["batch_id"], by=BY, source="chat"
                )
                marked.append(view["batch_id"])
    return marked


def _finished(home, seed_image, *, approve: int = 1, auto: bool = False):
    """出一批图、聊天框里选中前 approve 张，候选表点了选款完成（没勾的算不要），选中的款套图出完。"""
    ctx, picked = _setup(home, seed_image, approve=approve)
    if auto:
        set_config(home, "shelf.auto", True)
        ctx = reopen(home)
    candidates.sync(ctx)
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by=BY)
    return ctx, picked


def test_auto_archive_when_scenes_finish(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "shelf.auto", True)
    # 关着批次交付包时照旧：套图出完就收（开着时要等交付包标已下载，见下一个用例）
    set_config(home, "scenes.batch_packages", False)
    ctx = reopen(home)
    candidates.sync(ctx)
    # 还有待选、没点选款完成：不收
    assert "S01" in _reasons(shelf.sweep(ctx, source="test", dry_run=True))
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by=BY)
    # 选款完成了，套图还在出：不收
    dry = shelf.sweep(ctx, source="test", dry_run=True)
    assert "套图" in _reasons(dry)["S01"] and not _eligible(dry)

    out = scenes.sync(ctx)
    assert [a["seed_id"] for a in out["shelf"]["archived"]] == ["S01"]
    row = shelf.get(ctx, "S01")
    assert (row["state"], row["mode"], row["archived_by"], row["cycles"]) == (
        "archived",
        "auto",
        shelf.AUTO_BY,
        1,
    )
    assert json.loads(row["snapshot_json"])["approved"] == 1
    # 套图出完的卡片写明已归档，带「调出」「历史任务」
    (card,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    ]
    assert card["header"]["title"]["content"].startswith("套图出完，任务已归档｜S01")
    assert "历史任务" in card_text(card)
    buttons = {b["text"]["content"]: b.get("value") for b in card_buttons(card)}
    assert buttons["调出"] == {"action": ui.ACTION_RESTORE, "seed": "S01"}
    assert buttons["历史任务"]["action"] == ui.ACTION_SHELF
    # 任务列表不列，历史任务里有；工作台「可策划」、worker 补充策划都跳过
    assert tasks.summaries(ctx) == []
    (task,) = tasks.summaries(ctx, view="archived")
    assert task["archived"] and task["shelf"]["mode_label"] == "自动归档"
    assert tasks.archived_count(ctx) == 1
    assert [t["seed_id"] for t in tasks.summaries(ctx, view="all")] == ["S01"]
    assert plannable_families(ctx) == []
    snap = report.snapshot(ctx)
    assert snap["archived_tasks"] == 1
    # 再扫一遍：已经归档的不重复归档、不调出
    again = shelf.sweep(ctx, source="test")
    assert not again["archived"] and not again["reopened"]


def test_auto_archive_by_listing_when_every_pick_has_stores() -> None:
    task = {
        "status": "done",
        "status_label": "完成",
        "counts": {"approved": 2, "pending_selection": 0},
    }
    fact = {"accepted_at": "2026-10-30T01:00:00Z", "listed_at": "2026-10-30T02:00:00Z"}
    listing = {"approved": 2, "with_stores": 2, "listed": 1, "failed": 1}

    def block(fact=fact, row=None, **kw):
        return shelf.auto_block(task, fact, row, scenes_on=True, packages_on=True, listing=listing, **kw)

    assert block() == "选中的款还有 1 款没上架完，其中 1 款上架没成"
    # 选中的款都上架了：不看套图和交付包，直接能收
    listing.update(listed=2, failed=0)
    assert block() is None
    # 只在聊天里一款款定过、没点「选款完成」：不收
    assert block({"decided_at": "2026-10-30T01:00:00Z"}) == "还没点「选款完成」"
    # 调出过：要等调出之后有新的上架
    restored = {"archived_at": "2026-10-30T03:00:00Z", "restored_at": "2026-10-30T04:00:00Z"}
    assert block(row=restored) == "调出后还没有新的选款完成、套图出完、交付包下载或上架"
    # 有款没定店铺：照旧看套图和交付包
    listing.update(with_stores=1)
    assert shelf.auto_block(task, fact, None, scenes_on=False, listing=listing) == "套图没打开（要收起就手动归档）"


def test_auto_archive_waits_for_download(home, fake, seed_image, capsys) -> None:
    ctx, picked = _finished(home, seed_image, auto=True)
    out = scenes.sync(ctx)
    # 开着交付包：套图出完了也不收，任务到「套图完成」，等交付包标已下载
    assert "shelf" not in out and not shelf.is_archived(ctx, "S01")
    reason = _reasons(shelf.sweep(ctx, source="test", dry_run=True))["S01"]
    assert reason == "交付包还没标已下载：B001"
    (task,) = tasks.summaries(ctx)
    assert (task["status"], task["stage"], task["status_label"]) == (
        "done",
        "scenes_done",
        "套图完成",
    )
    assert task["steps"][-1] == {
        "key": "download",
        "label": "下载",
        "state": "current",
        "text": "下载 0/1",
    }
    assert any("B001 还没标已下载" in line for line in task["now"])
    # 套图出完的卡片带「B001 已下载」，写明下载后点了才归档
    (card,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("套图出完")
    ]
    assert "任务才会归档" in card_text(card)
    buttons = {b["text"]["content"]: b.get("value") for b in card_buttons(card)}
    assert buttons["B001 已下载"] == {
        "action": ui.ACTION_DOWNLOADED,
        "seed": "S01",
        "batch": "B001",
    }
    # 任务列表：表头写待标几个，任务上有「B001 已下载」
    view = ui.tasks_view(ctx)
    assert "交付包待标已下载 1" in view["lines"][0]
    (item,) = view["items"]
    assert any(
        a["action"] == ui.ACTION_DOWNLOADED and a["batch"] == "B001"
        for a in item["actions"]
    )
    assert item["title"].split("　", 1)[1].startswith("套图完成（")
    # 批次编号写错、交付包不存在：拒绝
    code, bad = _cli(
        capsys, "scenes", "downloaded", "--seed", "S01", "--batch", "X1", "--by", BY
    )
    assert code == 2 and "批次编号不对" in bad["detail"]
    code, bad = _cli(
        capsys, "scenes", "downloaded", "--seed", "S01", "--batch", "B009", "--by", BY
    )
    assert code == 2 and "还没打好" in bad["detail"]
    # 标已下载：当场只扫这个任务，满足条件就归档并发卡片
    code, out = _cli(
        capsys, "scenes", "downloaded", "--seed", "S01", "--batch", "B001", "--by", BY
    )
    assert code == 0 and [a["seed_id"] for a in out["archived"]] == ["S01"]
    assert out["notified"]
    row = ctx.db.q1("SELECT * FROM package_downloads WHERE seed_id = 'S01'")
    assert (row["batch_id"], row["marked_by"], row["source"]) == ("B001", BY, "chat")
    announced = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("任务已归档")
    ]
    assert "交付包都标了已下载" in card_text(announced[-1])
    (task,) = tasks.summaries(ctx, view="archived")
    assert (task["stage"], task["status_label"]) == ("downloaded", "套图已下载")
    assert (
        task["steps"][-1]["state"] == "done" and task["steps"][-1]["text"] == "下载 1/1"
    )
    # 再标一次：已经标过这一版
    code, again = _cli(
        capsys, "scenes", "downloaded", "--seed", "S01", "--batch", "B001", "--by", BY
    )
    assert code == 0 and again["already"] is True
    # 撤销（标错了）：标记删掉，任务回到「套图完成」；已经归档的不追溯
    code, undone = _cli(
        capsys,
        "scenes",
        "downloaded",
        "--seed",
        "S01",
        "--batch",
        "B001",
        "--by",
        BY,
        "--undo",
    )
    assert code == 0 and undone["undone"] is True
    assert not ctx.db.scalar("SELECT COUNT(*) FROM package_downloads")
    (task,) = tasks.summaries(ctx, view="archived")
    assert task["stage"] == "scenes_done" and shelf.is_archived(ctx, "S01")


def test_download_mark_follows_package_content(home, fake, seed_image) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    assert _download_all(ctx) == ["B001"]
    (view,) = packages.by_seed(ctx)["S01"]
    assert view["downloaded"] and packages.complete(view)
    # 续签链接（内容没变）：标记照旧
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE scene_packages SET export_expires_at = '2000-01-01T00:00:00+00:00'"
        )
    packages.build_due(ctx)
    (view,) = packages.by_seed(ctx)["S01"]
    assert view["downloaded"]
    # 重出一套：内容变了、包重打，标记作废（标的是旧包）
    scenes.start(ctx, by="t", asset_ids=picked, force=True)
    scenes.sync(ctx)
    (view,) = packages.by_seed(ctx)["S01"]
    assert view["ready"] and not view["downloaded"] and view["download_outdated"]
    assert packages.download_state(ctx)["S01"]["outdated"] == ["B001"]
    (task,) = tasks.summaries(ctx)
    assert task["stage"] == "scenes_done"
    reason = _reasons(shelf.sweep(ctx, source="test", dry_run=True))["S01"]
    assert reason == "交付包还没标已下载：B001（B001 重打过，下载新包后再标）"
    # 包还在重打时（有新出完的款）不让标
    with ctx.db.tx() as conn:
        conn.execute("UPDATE scene_packages SET fingerprint = 'old'")
    with pytest.raises(StageError, match="马上重打"):
        packages.mark_downloaded(ctx, "S01", "B001", by=BY, source="card")


def test_table_moves_into_folder_once(home, fake, seed_image) -> None:
    ctx, _ = _finished(home, seed_image)
    scenes.sync(ctx)
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    assert fake.bitable_parents[table_id] is None
    shelf.archive(ctx, "S01", by=BY)
    assert shelf.status(ctx)["moves_pending"] == 1
    assert candidates.has_work(ctx)

    # 表格同步：根目录下没有「已归档」文件夹就建一个，把候选表挪进去
    out = candidates.sync(ctx)
    assert out["moved"] == 1
    (folder,) = fake.bitable_folders.values()
    assert folder["name"] == "已归档" and fake.bitable_parents[table_id] == folder["id"]
    assert shelf.get(ctx, "S01")["folder_state"] == shelf.FOLDER_IN
    assert "moved" not in candidates.sync(ctx)
    # 人手动拖回根目录：只在状态变化时挪一次，不再拖回去
    fake.bitable_parents[table_id] = None
    assert "moved" not in candidates.sync(ctx)
    assert fake.bitable_parents[table_id] is None

    # 调出：挪回根目录；再归档时用记下的文件夹，不再建
    fake.bitable_parents[table_id] = folder["id"]
    shelf.restore(ctx, "S01", by=BY)
    assert candidates.sync(ctx)["moved"] == 1
    assert fake.bitable_parents[table_id] is None
    shelf.archive(ctx, "S01", by=BY)
    candidates.sync(ctx)
    assert (
        len(fake.bitable_folders) == 1
        and fake.bitable_parents[table_id] == folder["id"]
    )
    assert shelf.get(ctx, "S01")["cycles"] == 2


def test_move_failures_retry_then_stop(home, fake, seed_image) -> None:
    ctx, _ = _finished(home, seed_image)
    scenes.sync(ctx)
    shelf.archive(ctx, "S01", by=BY)
    # 没开块权限：建不了文件夹，记次数，满上限不再试，--retry 清零
    fake.bitable_fail.add(("POST", "/blocks"))
    out = candidates.sync(ctx)
    assert out.get("moved", 0) == 0 and out["failed"] == 1
    row = shelf.get(ctx, "S01")
    assert row["folder_attempts"] == 1 and "已归档" in row["folder_error"]
    assert shelf.status(ctx)["folder_errors"][0]["seed_id"] == "S01"
    from podflow import feishu_table

    for _ in range(feishu_table.MAX_ATTEMPTS - 1):
        candidates.sync(ctx)
    assert shelf.pending_moves(ctx) == []
    fake.bitable_fail.clear()
    out = candidates.sync(ctx, retry=True)
    assert out["moved"] == 1
    row = shelf.get(ctx, "S01")
    assert (row["folder_state"], row["folder_attempts"], row["folder_error"]) == (
        shelf.FOLDER_IN,
        0,
        None,
    )
    # 历史任务里写着候选表在文件夹里
    (item,) = ui.shelf_view(ctx)["items"]
    assert "候选表在「已归档」文件夹" in item["lines"][0]


def test_manual_archive_rules(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    pending = review.list_reviews(ctx, types=["asset_selection"])
    assert pending
    # 还有待选：要 --force；dry_run 先看会清出几张
    with pytest.raises(StageError, match="--force"):
        shelf.archive(ctx, "S01", by=BY)
    dry = shelf.archive(ctx, "S01", by=BY, force=True, dry_run=True)
    assert dry["dismiss"] == len(pending) and dry["mode"] == "force"
    assert shelf.get(ctx, "S01") is None
    # 套图在出：拒绝
    scenes.start(ctx, by="t", seed_id="S01")
    with pytest.raises(StageError, match="套图在出"):
        shelf.archive(ctx, "S01", by=BY, force=True)
    scenes.sync(ctx)
    # 加 force：剩下的待选清出选款（不算不要），候选表的行标「已清出」
    out = shelf.archive(ctx, "S01", by=BY, force=True, note="这款不做了")
    assert out["archived"] and out["dismissed"] == len(pending)
    assert out["mode_label"] == "手动归档（清出待选）"
    assert review.list_reviews(ctx, types=["asset_selection"]) == []
    statuses = {
        r["status"]
        for r in ctx.db.q(
            "SELECT status FROM reviews WHERE subject_type = 'asset_selection'"
            " AND decided_by = ?",
            (BY,),
        )
    }
    assert statuses == {"dismissed", "decided"}
    rejected = ctx.db.scalar(
        "SELECT COUNT(*) FROM assets WHERE human_decision = 'rejected'"
    )
    assert rejected == 0
    assert shelf.archive(ctx, "S01", by=BY)["already"] is True
    with pytest.raises(StageError, match="没有参考图"):
        shelf.archive(ctx, "S99", by=BY)
    # 关着归档：任务照常列，命令拒绝
    set_config(home, "shelf.enabled", False)
    ctx = reopen(home)
    assert [t["seed_id"] for t in tasks.summaries(ctx)] == ["S01"]
    with pytest.raises(StageError, match="shelf.enabled"):
        shelf.restore(ctx, "S01", by=BY)
    assert shelf.sweep(ctx, source="test") == {"skipped": "shelf.enabled 为 false"}


def test_parked_scenes_are_named_in_the_reason(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    _age_selection(ctx)
    fake.mooya_script = ["queued", "waiting_human"]
    candidates.accept(ctx, "S01", by=BY)
    scenes.sync(ctx)
    # 停在 mooya 等人工的也算在出：不自动归档，原因写明是等人工
    reason = _reasons(shelf.sweep(ctx, source="test", dry_run=True))["S01"]
    assert reason == "还有 1 款套图在出（其中 1 款在 mooya 等人工）"
    # 手动归档同样拒绝，并说怎么处理
    with pytest.raises(StageError, match="podctl scenes cancel"):
        shelf.archive(ctx, "S01", by=BY)


def test_auto_waits_for_a_new_completion_after_restore(home, fake, seed_image) -> None:
    ctx, picked = _finished(home, seed_image, auto=True)
    scenes.sync(ctx)
    assert _download_all(ctx) == ["B001"]
    assert [a["seed_id"] for a in shelf.after_download(ctx, "S01")["archived"]] == [
        "S01"
    ]
    assert shelf.is_archived(ctx, "S01")
    # 时间只到秒：上一次完成挪到 10 分钟前、归档挪到 5 分钟前，调出后再挪 2 分钟
    _age_facts(ctx, 600)
    _age_shelf(ctx, 300)
    shelf.restore(ctx, "S01", by=BY)
    _age_shelf(ctx, 120)
    (task,) = tasks.summaries(ctx)
    assert task["can_archive"] and not task["archived"]
    # 调出后没有新的选款完成 / 套图出完 / 标已下载：巡检不再收起
    out = shelf.sweep(ctx, source="tick")
    assert not out["archived"]
    assert "调出后" in _reasons(out)["S01"]
    # 重出一套：套图出完、交付包重打，标记作废；新包标了已下载（新的完成）后再收起
    scenes.start(ctx, by="t", asset_ids=picked, force=True)
    out = scenes.sync(ctx)
    assert "shelf" not in out
    assert _download_all(ctx) == ["B001"]
    assert shelf.after_download(ctx, "S01")["archived"]
    assert shelf.get(ctx, "S01")["cycles"] == 2


def test_new_activity_reopens_archived_task(home, fake, seed_image) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    shelf.archive(ctx, "S01", by=BY)
    # 不经命令入口直接出套图：巡检看到在出的套图就调出
    scenes.start(ctx, by="t", asset_ids=picked, force=True)
    out = shelf.sweep(ctx, source="tick")
    assert [r["seed_id"] for r in out["reopened"]] == ["S01"]
    row = shelf.get(ctx, "S01")
    assert row["state"] == "active" and row["restored_by"] == shelf.AUTO_BY
    assert "套图在出" in row["restore_reason"]
    assert [t["seed_id"] for t in tasks.summaries(ctx)] == ["S01"]


def test_touch_restores_from_command_entry(home, fake, seed_image, capsys) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    shelf.archive(ctx, "S01", by=BY)
    family_id = ctx.db.scalar("SELECT family_id FROM families WHERE seed_id = 'S01'")
    # 没归档的不动；按系列 / 方向 / 素材编号都找得到这张参考图
    assert shelf.touch(ctx, "S02", reason="x") == {}
    asset = picked[0]
    assert shelf.touch(ctx, asset, reason="按新规则重出")["restored"] is True
    assert shelf.get(ctx, "S01")["restore_reason"] == "按新规则重出"
    shelf.archive(ctx, "S01", by=BY)
    # 历史任务里点「继续裂变」：plan --detach 先调出，再在后台策划
    code, out = _cli(capsys, "plan", "--family", family_id, "--detach", "--notify")
    assert code == 0 and out["restored"]["reason"] == "继续裂变"
    assert not shelf.is_archived(ctx, "S01")
    _finish_bg(ctx)
    # 出套图的命令只在真有款排上队时调出
    shelf.archive(ctx, "S01", by=BY)
    code, out = _cli(capsys, "scenes", "start", "--seed", "S01", "--by", BY)
    assert code == 0 and not out["queued"] and "restored" not in out
    assert shelf.is_archived(ctx, "S01")
    code, out = _cli(capsys, "scenes", "start", "--seed", "S01", "--force", "--by", BY)
    assert code == 0 and out["queued"] and out["restored"][0]["seed_id"] == "S01"


def test_task_list_and_shelf_card(home, fake, seed_image) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    # 做完的任务：任务列表上有「归档」按钮
    (item,) = ui.tasks_view(ctx)["items"]
    assert {
        "action": ui.ACTION_ARCHIVE,
        "label": "归档",
        "style": "default",
        "seed": "S01",
    } in item["actions"]
    shelf.archive(ctx, "S01", by=BY, note="先放一放")
    view = ui.tasks_view(ctx)
    assert view["items"] == [] and "1 个任务已归档" in view["lines"][0]
    assert view["actions"][0]["action"] == ui.ACTION_SHELF
    assert "历史任务" in view["hint"]

    card = ui.shelf_view(ctx)
    assert card["title"] == "历史任务" and card["total"] == 1
    (entry,) = card["items"]
    assert (
        entry["lines"][0].startswith("**已归档**") and "手动归档" in entry["lines"][0]
    )
    assert entry["actions"][0] == {
        "action": ui.ACTION_RESTORE,
        "label": "调出",
        "style": "primary",
        "seed": "S01",
        "back": ui.BACK_SHELF,
    }
    assert entry["form"]["submit"]["action"] == "pod_plan"
    assert entry["form"]["submit"]["label"].startswith("继续裂变")
    assert {"label": "候选表", "url": candidates.table_link(ctx, "S01")} in entry[
        "links"
    ]
    assert card["search"]["input"]["name"] == ui.SHELF_QUERY_FIELD
    # 搜：编号 / 名字 / 日期；搜不到写明
    assert ui.shelf_view(ctx, query="S01")["total"] == 1
    miss = ui.shelf_view(ctx, query="没有这个")
    assert miss["total"] == 0 and "换个字" in miss["lines"][0]
    assert {
        "action": ui.ACTION_SHELF,
        "label": "全部历史任务",
        "style": "default",
    } in miss["nav"]
    # 任务明细卡：写明已归档，带「调出」
    detail = ui.history_view(ctx, None)
    assert any("已归档" in line for line in detail["lines"])
    assert detail["actions"][0]["action"] == ui.ACTION_RESTORE


def test_cli_shelf_commands(home, fake, seed_image, capsys) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    _download_all(ctx)
    code, dry = _cli(capsys, "shelf", "sweep", "--dry-run")
    assert code == 0 and _eligible(dry) == ["S01"] and dry["auto"] is False
    # 自动归档关着：只列能收的，不收
    code, out = _cli(capsys, "shelf", "sweep")
    assert code == 0 and not out["archived"] and _eligible(out) == ["S01"]
    code, out = _cli(capsys, "shelf", "archive", "S01", "--by", BY)
    assert code == 0 and out["archived"] is True
    code, listed = _cli(capsys, "shelf", "list")
    assert listed["count"] == 1 and listed["archived"][0]["seed_id"] == "S01"
    code, listed = _cli(capsys, "shelf", "list", "--search", "S02")
    assert listed["count"] == 0
    code, active = _cli(capsys, "tasks")
    assert active["tasks"] == [] and active["archived_count"] == 1
    code, every = _cli(capsys, "tasks", "--all")
    assert [t["seed_id"] for t in every["tasks"]] == ["S01"]
    code, card = _cli(capsys, "ui", "shelf", "--query", "S01")
    assert card["total"] == 1
    code, info = _cli(capsys, "shelf", "status")
    assert info["archived"] == 1 and info["recent"][0]["seed_id"] == "S01"
    code, out = _cli(capsys, "shelf", "restore", "S01", "--by", BY)
    assert code == 0 and out["restored"] is True
    code, out = _cli(capsys, "shelf", "restore", "S01", "--by", BY)
    assert out["already_active"] is True
    code, out = _cli(capsys, "shelf", "archive", "S1x", "--by", BY)
    assert code == 2


def test_tick_announces_auto_archive(home, fake, seed_image, capsys) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    # 自动归档关着时标的已下载：之后打开，巡检照样收起
    _download_all(ctx)
    _finish_bg(ctx)
    set_config(home, "shelf.auto", True)
    ctx = reopen(home)
    out = cli._auto_tick(ctx)
    assert [a["seed_id"] for a in out["shelf"]["archived"]] == ["S01"]
    (card,) = [
        c
        for c in fake.feishu_cards
        if c["header"]["title"]["content"].startswith("任务已归档")
    ]
    assert card["header"]["title"]["content"].startswith("任务已归档｜S01")
    labels = [b["text"]["content"] for b in card_buttons(card)]
    assert labels == ["历史任务", "调出", "任务列表"]
    # 下一轮巡检：没事就不出现在结果里
    _finish_bg(ctx)
    assert "shelf" not in cli._auto_tick(ctx)


def test_doctor_and_purge(home, fake, seed_image) -> None:
    ctx, picked = _finished(home, seed_image)
    scenes.sync(ctx)
    shelf.archive(ctx, "S01", by=BY)
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert (
        checks["飞书 Base 块接口可用（候选表挪进「已归档」文件夹要用）"]["ok"] is True
    )
    assert checks["自动归档（shelf.auto）"]["ok"] is False
    assert checks["自动归档（shelf.auto）"]["level"] == "warn"
    maintenance.purge_old(ctx, seeds=["S01"], by="t")
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_shelf") == 0
