"""候选表：每个任务一张飞书多维表格，进选款的图自动进表；聊天框的决定同步过去；打钩验收（不打包下载）。"""

from __future__ import annotations

import datetime as dt
import json
import os
from types import SimpleNamespace

import pytest

from conftest import reopen, set_config
from podflow import (
    bg,
    candidates,
    cli,
    feishu_table,
    maintenance,
    notify,
    review,
    store,
    ui,
)
from podflow.stages.common import StageError
from podflow.stages.plan import asset_label_of
from podflow.util import iso, utcnow
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
    return calls


def _setup(home, seed_image, *, oss: bool = True):
    _enable_feishu(home)
    if oss:
        _enable_oss(home)
    set_config(home, "table.enabled", True)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    if oss:
        store.sync(ctx)
    return ctx


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _open_items(ctx) -> list[dict]:
    return review.list_reviews(ctx, types=["asset_selection"])


def _record(ctx, asset_id: str) -> str:
    return ctx.db.scalar(
        "SELECT record_id FROM candidate_rows WHERE asset_id = ?", (asset_id,)
    )


def _candidate_table(fake) -> tuple[str, dict]:
    return next(
        (tid, t)
        for tid, t in fake.bitable_tables.items()
        if t["name"].endswith(candidates.TABLE_SUFFIX)
    )


def _age_selection(ctx, review_ids: list[str] | None = None) -> None:
    """把选款待审的进选款时间往前拨过 FRESH_MINUTES：验收时没勾的才会算不要。"""
    old = iso(utcnow() - dt.timedelta(minutes=candidates.FRESH_MINUTES + 1))
    with ctx.db.tx() as conn:
        if review_ids is None:
            conn.execute(
                "UPDATE reviews SET created_at = ? WHERE subject_type = 'asset_selection'",
                (old,),
            )
        for review_id in review_ids or []:
            conn.execute(
                "UPDATE reviews SET created_at = ? WHERE review_id = ?",
                (old, review_id),
            )


def _make_old(ctx, fake, table_id: str) -> list[dict]:
    """装成 2026-10-22 前建的表：那一列叫「状态」、两列都没说明、meta 里没记；返回单选的选项（带 id）。"""
    table = fake.bitable_tables[table_id]
    options: list[dict] = []
    for field in table["fields"]:
        field.pop("description", None)
        if field["field_name"] == candidates.STATE_FIELD:
            field["field_name"] = candidates.OLD_STATE_FIELD
            options = [
                {**o, "id": f"opt{i}", "color": i}
                for i, o in enumerate(field["property"]["options"])
            ]
            field["property"] = {"options": options}
    for record_id, tid in fake.bitable_record_tables.items():
        fields = fake.bitable_records.get(record_id) or {}
        if tid == table_id and candidates.STATE_FIELD in fields:
            fields[candidates.OLD_STATE_FIELD] = fields.pop(candidates.STATE_FIELD)
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, candidates.FIELDS_META_KEY)
    return options


def _field_calls(fake, table_id: str) -> list[tuple[str, str]]:
    return [c for c in fake.bitable_calls if f"/tables/{table_id}/fields" in c[1]]


def test_first_sync_builds_a_table_per_task(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    items = _open_items(ctx)
    # 图进选款时就拉起了后台同步
    assert [argv[2:] for argv in spawned if argv[2:3] == ["table"]] == [
        ["table", "sync"]
    ]
    assert candidates.has_work(ctx)

    out = candidates.sync(ctx)
    assert (out["tables_created"], out["added"], out["failed"]) == (1, len(items), 0)
    # 多维表格还没建：先建（里面有「选中的款」），再建这个任务的「S01 候选」
    (app,) = fake.bitable_apps
    table_id, table = _candidate_table(fake)
    assert table["name"] == "S01 候选" and table["default_view_name"] == "全部"
    assert [f["field_name"] for f in table["fields"]] == [
        f["field_name"] for f in candidates.FIELDS
    ]
    assert {"view_name": "看图", "view_type": "gallery"} in table["views"]
    by_name = {f["field_name"]: f for f in table["fields"]}
    assert by_name["选中"]["type"] == 7
    assert [o["name"] for o in by_name["选款结果"]["property"]["options"]] == list(
        candidates.STATES
    )
    # 新表建的时候就是新列名、两列带说明，不用再改字段
    assert "状态" not in by_name
    assert by_name["选中"]["description"] == {
        "disable_sync": True,
        "text": candidates.CHECK_HELP,
    }
    assert "手改不算" in by_name["选款结果"]["description"]["text"]
    assert candidates.state_field(ctx, table_id) == "选款结果"
    assert _field_calls(fake, table_id) == []
    info = candidates.table_info(ctx, "S01")
    assert (
        info["table_id"] == table_id and info["url"] == f"{app['url']}?table={table_id}"
    )

    rows = [
        fake.bitable_records[r]
        for r, t in fake.bitable_record_tables.items()
        if t == table_id
    ]
    # 「素材」是「编号_方向短语」，缩略图附件也按它命名
    assert sorted(r["素材"] for r in rows) == sorted(
        asset_label_of(ctx, i["subject_id"]) for i in items
    )
    assert all(r["素材"] != r["素材"].split("_", 1)[0] for r in rows)
    row = rows[0]
    assert fake.bitable_files[row["缩略图"][0]["file_token"]]["file_name"] == (
        f"{row['素材']}.jpg"
    )
    assert row["选款结果"] == "待选" and "选中" not in row
    assert row["方向"].startswith("V0001 ") and row["批次"] == "B001"
    assert fake.bitable_files[row["缩略图"][0]["file_token"]]["parent_type"] == (
        "bitable_image"
    )
    assert row["下载原图"]["link"].startswith("https://") and row["链接到期"] > 0
    assert not candidates.has_work(ctx)
    # 再同步不重复
    again = candidates.sync(ctx)
    assert (again["tables_created"], again["added"]) == (0, 0)
    status = candidates.status(ctx)
    assert status["tables"][0]["rows"] == len(items) and status["pending"] == 0
    assert status["fields_pending"] == 0 and status["fields_error"] is None


def test_chat_decisions_show_up_in_the_table(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    first, second = _open_items(ctx)[:2]
    review.decide(ctx, first["review_id"], "approved", by="feishu:ou_tester")
    review.decide(ctx, second["review_id"], "rejected", by="feishu:ou_tester")
    assert candidates.has_work(ctx)
    assert candidates.sync(ctx)["updated"] == 2
    picked = fake.bitable_records[_record(ctx, first["subject_id"])]
    dropped = fake.bitable_records[_record(ctx, second["subject_id"])]
    assert (picked["选款结果"], picked["选中"]) == ("已选中", True)
    assert (dropped["选款结果"], dropped["选中"]) == ("没选中", False)
    assert not candidates.has_work(ctx)
    # 清出选款的行标「已清出」
    out = maintenance.dismiss_selection(ctx, batches=["B001"], by="tester")
    assert out["reviews"]
    assert candidates.sync(ctx)["updated"] == len(out["reviews"])
    states = {fake.bitable_records[_record(ctx, a)]["选款结果"] for a in out["assets"]}
    assert states == {"已清出"}


def test_accept_reads_ticks_and_makes_no_zip(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    _age_selection(ctx)
    items = _open_items(ctx)
    chat, ticked, gone = items[0], items[1:3], items[3]
    # 聊天框里先选了一张，人又在表里把它的勾去掉：以聊天框为准
    review.decide(ctx, chat["review_id"], "approved", by="feishu:ou_tester")
    candidates.sync(ctx)
    fake.bitable_records[_record(ctx, chat["subject_id"])]["选中"] = False
    for item in ticked:
        fake.bitable_records[_record(ctx, item["subject_id"])]["选中"] = True
    # 有一行被人从表里删了：不替人决定
    fake.bitable_records.pop(_record(ctx, gone["subject_id"]))

    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert sorted(out["approved"]) == sorted(i["subject_id"] for i in ticked)
    assert out["not_in_table"] == [gone["subject_id"]]
    rest = {i["subject_id"] for i in items[4:]}
    assert set(out["rejected"]) == rest and out["fresh"] == []
    decisions = {
        r["subject_id"]: (r["status"], r["decision"])
        for r in review.list_reviews(ctx, types=["asset_selection"], status="all")
    }
    assert decisions[chat["subject_id"]] == ("decided", "approved")
    assert all(decisions[i["subject_id"]] == ("decided", "approved") for i in ticked)
    assert decisions[gone["subject_id"]] == ("open", None)
    assert all(decisions[a] == ("decided", "rejected") for a in rest)
    for item in ticked:
        assert (
            fake.bitable_records[_record(ctx, item["subject_id"])]["选款结果"]
            == "已选中"
        )
    for asset_id in rest:
        assert fake.bitable_records[_record(ctx, asset_id)]["选款结果"] == "没选中"

    # 验收完不打包下载：OSS 和本机都没有压缩包，库里也不记
    assert "export" not in out
    assert not [k for k in fake.oss_objects if "/exports/" in k or k.endswith(".zip")]
    assert not (home / "exports").exists()
    info = candidates.table_info(ctx, "S01")
    assert info["accepted_at"] and info["export_url"] is None
    assert "export" not in candidates.status(ctx)["tables"][0]

    # 再验收一次：表里重新出现的那行照常定下来
    fake.bitable_records[_record(ctx, gone["subject_id"])] = {"选中": True}
    fake.bitable_record_tables[_record(ctx, gone["subject_id"])] = info["table_id"]
    again = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert again["approved"] == [gone["subject_id"]]


def test_accept_leaves_fresh_unticked_rows(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    items = _open_items(ctx)
    old, recent = items[:2], items[2:]
    _age_selection(ctx, [i["review_id"] for i in old])
    # 刚进选款的：勾上的照样算选中，没勾的人多半还没看到，留到下次
    fake.bitable_records[_record(ctx, recent[0]["subject_id"])]["选中"] = True
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert out["approved"] == [recent[0]["subject_id"]]
    assert set(out["rejected"]) == {i["subject_id"] for i in old}
    assert sorted(out["fresh"]) == sorted(i["subject_id"] for i in recent[1:])
    assert {i["subject_id"] for i in _open_items(ctx)} == set(out["fresh"])
    for asset_id in out["fresh"]:
        row = fake.bitable_records[_record(ctx, asset_id)]
        assert row["选款结果"] == "待选" and not row.get("选中")
    summary = bg.summarize("accept", out)
    assert summary["fresh"] == len(out["fresh"])
    card = notify.bg_card(ctx, {"kind": "accept", "subject": "S01"}, summary, ok=True)
    assert (
        f"刚进表、没勾的 {len(out['fresh'])} 张留到下次验收（还是待选）"
        in card["elements"][0]["content"]
    )
    # 过了 FRESH_MINUTES 再验收，还是没勾就算不要
    _age_selection(ctx)
    again = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert sorted(again["rejected"]) == sorted(out["fresh"]) and again["fresh"] == []


def test_accept_does_not_judge_rows_it_just_added(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    _age_selection(ctx)
    items = _open_items(ctx)
    # 最后两张之前没进成表（比如同步失败过），到验收时才补进去：人没见过，不判
    late = items[-2:]
    records = [_record(ctx, i["subject_id"]) for i in late]
    with ctx.db.tx() as conn:
        for item in late:
            conn.execute(
                "DELETE FROM candidate_rows WHERE asset_id = ?", (item["subject_id"],)
            )
    for record_id in records:
        fake.bitable_records.pop(record_id)
        fake.bitable_record_tables.pop(record_id)
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert out["synced"]["added"] == 2
    assert sorted(out["fresh"]) == sorted(i["subject_id"] for i in late)
    assert set(out["rejected"]) == {i["subject_id"] for i in items[:-2]}
    again = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert sorted(again["rejected"]) == sorted(i["subject_id"] for i in late)


def test_old_tables_get_renamed_column_and_help(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    options = _make_old(ctx, fake, table_id)
    assert candidates.state_field(ctx, table_id) == "状态"
    assert candidates.has_work(ctx)
    assert candidates.status(ctx)["fields_pending"] == 1
    first, second, third = _open_items(ctx)[:3]
    first_row = fake.bitable_records[_record(ctx, first["subject_id"])]

    # 改字段失败：照旧按「状态」写，聊天框的决定照样同步过去；失败满上限不再试，--retry 清零
    fake.bitable_fail.add(("PUT", "/fields/"))
    review.decide(ctx, first["review_id"], "approved", by="feishu:ou_tester")
    out = candidates.sync(ctx)
    assert (out["fields"], out["updated"], out["failed"]) == (0, 1, 1)
    assert (first_row["状态"], first_row["选中"]) == ("已选中", True)
    assert "选款结果" not in first_row
    assert candidates.status(ctx)["fields_error"].startswith("S01：")
    for _ in range(feishu_table.MAX_ATTEMPTS - 1):
        assert candidates.sync(ctx)["failed"] == 1
    assert not candidates.has_work(ctx)
    assert candidates.status(ctx)["fields_pending"] == 1

    # 改字段恢复：改名、补说明，单选的选项（带 id）原样带回，格子里的值都在
    fake.bitable_fail.clear()
    out = candidates.sync(ctx, retry=True)
    assert (out["fields"], out["failed"]) == (1, 0)
    by_name = {f["field_name"]: f for f in table["fields"]}
    assert "状态" not in by_name
    assert by_name["选款结果"]["type"] == 3
    assert by_name["选款结果"]["property"] == {"options": options}
    assert by_name["选款结果"]["description"]["text"] == candidates.STATE_HELP
    assert by_name["选中"]["description"]["text"] == candidates.CHECK_HELP
    assert first_row["选款结果"] == "已选中" and "状态" not in first_row
    assert candidates.state_field(ctx, table_id) == "选款结果"
    status = candidates.status(ctx)
    assert status["fields_pending"] == 0 and status["fields_error"] is None
    assert not candidates.has_work(ctx)

    # 之后按新列名写，不再调字段接口
    calls = len(_field_calls(fake, table_id))
    review.decide(ctx, second["review_id"], "rejected", by="feishu:ou_tester")
    assert candidates.sync(ctx)["updated"] == 1
    second_row = fake.bitable_records[_record(ctx, second["subject_id"])]
    assert second_row["选款结果"] == "没选中" and "状态" not in second_row
    _age_selection(ctx)
    fake.bitable_records[_record(ctx, third["subject_id"])]["选中"] = True
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert out["approved"] == [third["subject_id"]]
    assert fake.bitable_records[_record(ctx, third["subject_id"])]["选款结果"] == (
        "已选中"
    )
    assert len(_field_calls(fake, table_id)) == calls


def test_task_list_has_table_link_and_accept(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image)
    # 还没建表：没有候选表的按钮
    (item,) = ui.tasks_view(ctx)["items"]
    assert item["links"] == [] and "pod_accept" not in [
        a["action"] for a in item["actions"]
    ]
    candidates.sync(ctx)
    (item,) = ui.tasks_view(ctx)["items"]
    info = candidates.table_info(ctx, "S01")
    assert item["links"] == [{"label": "候选表", "url": info["url"]}]
    accept = item["actions"][0]
    pending = len(_open_items(ctx))
    assert accept == {
        "action": ui.ACTION_ACCEPT,
        "label": f"选款完成（待选 {pending}）",
        "style": "primary",
        "seed": "S01",
    }
    fake.bitable_records[_record(ctx, _open_items(ctx)[0]["subject_id"])]["选中"] = True
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by="feishu:ou_tester")
    (item,) = ui.tasks_view(ctx)["items"]
    # 验收后只剩「候选表」，没有下载压缩包
    assert [link["label"] for link in item["links"]] == ["候选表"]
    assert "pod_accept" not in [a["action"] for a in item["actions"]]


def test_accept_card_has_table_link_but_no_download(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    fake.bitable_records[_record(ctx, _open_items(ctx)[0]["subject_id"])]["选中"] = True
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    summary = bg.summarize("accept", out)
    assert summary["approved"] == 1 and "export" not in summary
    card = notify.bg_card(ctx, {"kind": "accept", "subject": "S01"}, summary, ok=True)
    assert card["header"]["title"]["content"] == "选款验收完成｜S01"
    text = card["elements"][0]["content"]
    assert "选中 **1** 张" in text and "压缩包" not in text
    links = next(e for e in card["elements"] if e["tag"] == "action")["actions"]
    labels = [b["text"]["content"] for b in links]
    assert "下载压缩包" not in labels and labels[0] == "打开候选表"


def test_accept_without_oss_makes_no_zip(home, fake, seed_image, spawned) -> None:
    ctx = _setup(home, seed_image, oss=False)
    candidates.sync(ctx)
    fake.bitable_records[_record(ctx, _open_items(ctx)[0]["subject_id"])]["选中"] = True
    out = candidates.accept(ctx, "S01", by="t")
    assert len(out["approved"]) == 1 and "export" not in out
    assert not (home / "exports").exists()


def test_cli_accept_and_guards(home, fake, seed_image, capsys, spawned) -> None:
    _setup(home, seed_image)
    accept = ("table", "accept", "--seed", "S01", "--by", "t")
    code, err = _cli(capsys, *accept, "--detach")
    assert code == 2 and "还没有候选表" in err["detail"]
    out = run(capsys, "table", "sync")
    assert out["candidates"]["tables_created"] == 1
    task = run(capsys, *accept, "--detach")
    assert task["task_id"].startswith("BG")
    assert spawned[-1][2:] == list(accept)
    code, busy = _cli(capsys, *accept, "--detach")
    assert code == 2 and "正在验收" in busy["detail"]
    code, bad = _cli(capsys, "table", "accept", "--seed", "S01;x", "--by", "t")
    assert code == 2 and "任务编号不对" in bad["detail"]
    status = run(capsys, "table", "status")
    assert status["candidates"]["tables"][0]["seed_id"] == "S01"
    set_config(home, "table.enabled", False)
    code, off = _cli(capsys, *accept)
    assert code == 2 and "table.enabled 为 false" in off["detail"]
    set_config(home, "table.enabled", True)
    with pytest.raises(StageError, match="还没有候选表"):
        candidates.accept(reopen(home), "S09", by="t")


def test_reset_and_purge_clean_up_candidate_tables(
    home, fake, seed_image, spawned
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    # 重建多维表格：候选表在新表格里重建
    feishu_table.init(ctx, reset=True)
    assert candidates.table_info(ctx, "S01") is None
    assert candidates.sync(ctx)["tables_created"] == 1
    info = candidates.table_info(ctx, "S01")
    assert info["url"].startswith(fake.bitable_apps[-1]["url"])
    # 整张参考图删掉：候选表、行，连 2026-10-20 前验收留下的压缩包一起删
    key = "pod-blanket/exports/S01/S01-selected-old.zip"
    fake.oss_objects[key] = (b"PK\x05\x06" + b"\0" * 18, {})
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE candidate_tables SET export_key = ? WHERE seed_id = 'S01'", (key,)
        )
    out = maintenance.purge_old(ctx, seeds=["S01"], by="tester")
    assert out["candidates"]["tables"] == 1
    assert (
        "DELETE",
        f"/bitable/v1/apps/{fake.bitable_apps[-1]['app_token']}/tables/{info['table_id']}",
    ) in fake.bitable_calls
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_rows") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM candidate_tables") == 0
    assert key not in fake.oss_objects
