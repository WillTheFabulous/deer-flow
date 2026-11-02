"""精作款周表格（temu.listed.crafted，2026-10-31）：mooya 上做的款（1店 / 2店，客户端 ai_temu）不进「已上架的款」，
按上架日期进知识库「精作款」下面的周表格「精作款 W44（10.26–11.01）」里那一天的表「10-30 周五」；模板锁上之前不写；
已经写进「已上架的款」的用 migrate 挪过来（删那边的行、只剩精作款的旧表格那张整张删、删周表格那张的精作款店铺视图）。
假飞书见 conftest：知识库文档 / 多维表格节点（带父节点）、整本复制、表复制、批量删行、删视图。"""

from __future__ import annotations

import json

import pytest

from conftest import reopen, set_config
from podflow import cli, crafted, feishu, feishu_table, listed_table
from podflow.stages.common import StageError
from test_mooya_listing import S1, S2, S3, _ledger_source, _mooya_stores
from test_temu import COL3, COL4, _temu_home, spawned  # noqa: F401 — spawned 是夹具

pytestmark = pytest.mark.usefixtures("spawned")

SPACE = "spcW"
COL1 = S1["column"]
COL2 = S2["column"]


def _home(home, fake, *, enabled: bool = True):
    """开着飞书表、每周选款表、mooya 四家店底账；enabled = 打开精作款周表格。"""
    _temu_home(home, fake)
    _mooya_stores(fake)
    _ledger_source(home)
    set_config(home, "table.wiki_space_id", SPACE)
    set_config(home, "table.weekly", True)
    set_config(home, "temu.listed.crafted.enabled", enabled)
    return reopen(home)


def _ledger(fake) -> None:
    """mooya 底账：1店 / 2店四款（7-23、10-25、10-30 两款），3店一款（10-30）。"""
    fake.temu_add_listed("ai_temu", "FLR0042", {**S1}, listedAt="2026-07-23T03:00:00Z", source="backfill",
                         human={"sales_total": 5})
    fake.temu_add_listed("ai_temu", "JUH0615", {**S2}, listedAt="2026-10-25T03:30:00Z", source="backfill")
    fake.temu_add_listed("ai_temu", "FLR1103", {**S1}, listedAt="2026-10-30T02:57:00Z",
                         mainImage={"path": "/temu/listed/3/image"})
    fake.temu_add_listed("ai_temu", "FLR1104", {**S1}, listedAt="2026-10-30T04:00:00Z")
    fake.temu_add_listed("ai_temu_deerflow", "Deer0006", {**S3}, listedAt="2026-10-30T05:50:00Z", source="deerflow")


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _tables(fake, app: str) -> dict[str, str]:
    return {t["name"]: tid for tid, t in fake.bitable_tables.items() if fake.bitable_table_apps.get(tid) == app}


def _codes(fake, table_id: str) -> dict[str, dict]:
    return {
        cells["货号"]: {"record_id": rid, **cells}
        for rid, cells in fake.bitable_records.items()
        if fake.bitable_record_tables.get(rid) == table_id and cells.get("货号")
    }


def _view_names(fake, table_id: str) -> list[str]:
    return [v["view_name"] for v in fake.bitable_views[table_id]]


def _fit(fake, ctx) -> None:
    """人在模板的「看图」里把封面效果设成适应（接口改不了，假飞书里记作 cover_fit）。"""
    table_id = crafted.template(ctx)["table"]["table_id"]
    for view in fake.bitable_views[table_id]:
        if view["view_type"] == "gallery":
            view["cover_fit"] = "fit"


def _weekly_listed(ctx) -> dict:
    return listed_table._meta(ctx, "2026-W44")


def test_names() -> None:
    assert crafted.book_title("2026-07-23") == "精作款 W30（07.20–07.26）"
    assert crafted.book_title("2026-11-01") == "精作款 W44（10.26–11.01）"
    assert crafted.table_name("2026-10-26") == "10-26 周一"
    assert crafted.key("2026-10-30") == "crafted:2026-10-30" and crafted.is_key("crafted:2026-10-30")
    assert not crafted.is_key("2026-W44") and not crafted.is_key(None)


def test_crafted_rows_wait_for_lock_then_go_by_day(home, fake, capsys) -> None:
    ctx = _home(home, fake)
    _ledger(fake)
    listed_table.pull(ctx)
    # 店铺列分两套：「已上架的款」只有本机的两家，精作款只有 1店 / 2店
    assert listed_table.store_columns(ctx) == [COL3, COL4]
    assert crafted.store_columns(ctx) == [COL1, COL2]

    # 第一次同步：模板自己建好（「精作款」节点下面），没锁上之前精作款的行哪里都不写；3店的照常进 W44「已上架的款」
    feishu_table.sync(ctx)
    tpl = crafted.template(ctx)
    root = crafted.root(ctx)
    assert crafted.template_ready(tpl) and not tpl.get("locked_at") and not crafted.ready(ctx)
    assert fake.wiki_types[root["node_token"]] == "docx" and fake.wiki_parents[root["node_token"]] is None
    assert fake.wiki_titles[root["node_token"]] == "精作款"
    assert fake.wiki_parents[tpl["node_token"]] == root["node_token"]
    assert fake.wiki_titles[tpl["node_token"]] == "精作款模板（勿改）"
    assert list(_tables(fake, tpl["app_token"])) == ["精作款模板"]
    assert _view_names(fake, tpl["table"]["table_id"]) == ["全部", COL1, COL2, "看图"]
    weekly = _weekly_listed(ctx)
    assert set(_codes(fake, weekly["table_id"])) == {"Deer0006"}
    assert set((weekly.get("views") or {}).get("stores") or {}) == {COL3, COL4}
    assert not any(c.get("货号", "").startswith(("FLR", "JUH")) for c in fake.bitable_records.values())
    assert [i["code"] for i in listed_table._pending(ctx)] == []
    assert not listed_table.has_work(ctx)
    # 没锁上不能迁
    with pytest.raises(StageError):
        crafted.migrate(ctx)

    # 人在「看图」里设好封面效果，锁上
    _fit(fake, ctx)
    code, out = _cli(capsys, "temu", "listed", "crafted", "lock")
    assert code == 0 and out["locked"] is True
    ctx = reopen(home)
    assert crafted.ready(ctx)
    pending = {i["code"] for i in listed_table._pending(ctx)}
    assert pending == {"FLR0042", "JUH0615", "FLR1103", "FLR1104"}

    feishu_table.sync(ctx)
    books_ = {b["book_id"]: b for b in crafted.all_books(ctx)}
    assert set(books_) == {"2026-W30", "2026-W43", "2026-W44"}
    assert books_["2026-W30"]["title"] == "精作款 W30（07.20–07.26）"
    for found in books_.values():
        assert fake.wiki_parents[found["node_token"]] == root["node_token"]
    days = {d["day"]: d for d in crafted.days(ctx)}
    assert set(days) == {"2026-07-23", "2026-10-25", "2026-10-30"}
    day = days["2026-10-30"]
    w44 = books_["2026-W44"]
    names = _tables(fake, w44["app_token"])
    assert names["10-30 周五"] == day["table_id"] and "精作款模板" in names
    # 天表在根目录，模板表在「模板」文件夹；复制件带着封面效果和每家店的视图
    assert fake.bitable_parents[day["table_id"]] is None
    assert fake.bitable_parents[names["精作款模板"]] == w44["template_folder_id"]
    gallery = next(v for v in fake.bitable_views[day["table_id"]] if v["view_type"] == "gallery")
    assert gallery["cover_fit"] == "fit"
    assert _view_names(fake, day["table_id"]) == ["全部", COL1, COL2, "看图"]
    rows = _codes(fake, day["table_id"])
    assert set(rows) == {"FLR1103", "FLR1104"}
    assert rows["FLR1103"]["店铺"] == COL1 and rows["FLR1103"]["主图"]
    assert set(_codes(fake, days["2026-07-23"]["table_id"])) == {"FLR0042"}
    assert _codes(fake, days["2026-07-23"]["table_id"])["FLR0042"]["累计销量"] == 5
    assert set(_codes(fake, days["2026-10-25"]["table_id"])) == {"JUH0615"}
    # 「已上架的款」里还是只有 3店那一款；归属记下来了
    assert set(_codes(fake, _weekly_listed(ctx)["table_id"])) == {"Deer0006"}
    assert crafted.placed(ctx, "FLR1103") == "2026-10-30"
    assert listed_table._pending(ctx) == []

    # 精作款表里改销量、改标题：写回 mooya
    rid = rows["FLR1103"]["record_id"]
    fake.bitable_records[rid]["累计销量"] = 12
    fake.bitable_records[rid]["上架标题"] = "FLR1103 Flannel Blanket 280GSM New Title For Sofa"
    listed_table.read_back(ctx)
    calls = [c[1] for c in fake.temu_calls]
    assert "/temu/listed/FLR1103/human" in calls and "/temu/listed/FLR1103/title" in calls
    status = listed_table.status(ctx)["crafted"]
    assert status["ready"] and status["days"] == 3 and status["written"] == 4 and status["not_migrated"] == 0


def test_migrate_moves_crafted_rows_out_of_listed(home, fake, capsys) -> None:
    # 打开之前（10-31 线上那样）：1店 / 2店写进了旧表格那张和 W44 那张「已上架的款」，W44 还有 1店 / 2店的视图
    ctx = _home(home, fake, enabled=False)
    _ledger(fake)
    listed_table.pull(ctx)
    feishu_table.sync(ctx)
    legacy = listed_table._meta(ctx)
    weekly = _weekly_listed(ctx)
    assert set(_codes(fake, legacy["table_id"])) == {"FLR0042", "JUH0615"}
    assert set(_codes(fake, weekly["table_id"])) == {"FLR1103", "FLR1104", "Deer0006"}
    assert {COL1, COL2} <= set(weekly["views"]["stores"])

    # 打开精作款：没迁过的照旧在那边；新来的一款等模板锁上
    set_config(home, "temu.listed.crafted.enabled", True)
    ctx = reopen(home)
    fake.temu_add_listed("ai_temu", "FLR1105", {**S1}, listedAt="2026-10-31T02:00:00Z")
    listed_table.pull(ctx)
    item = next(i for i in listed_table.rows(ctx) if i["code"] == "FLR1103")
    assert listed_table.place(ctx, item, freeze=False) == "2026-W44"
    fresh = next(i for i in listed_table.rows(ctx) if i["code"] == "FLR1105")
    assert listed_table.place(ctx, fresh, freeze=False) == "crafted:2026-10-31"
    assert "FLR1105" not in {i["code"] for i in listed_table._pending(ctx)}

    # 看一眼要做什么：不写飞书
    before = dict(fake.bitable_records)
    code, todo = _cli(capsys, "temu", "listed", "crafted", "migrate", "--dry-run")
    assert code == 0 and todo["dry_run"] is True
    assert {d["book_id"]: d["rows"] for d in todo["delete"]} == {None: 2, "2026-W44": 2}
    assert todo["legacy_table"]["drop"] is True and todo["legacy_table"]["table_id"] == legacy["table_id"]
    assert todo["views"] == [{"book_id": "2026-W44", "views": sorted([COL1, COL2])}]
    assert [b["book_id"] for b in todo["books"]] == ["2026-W30", "2026-W43", "2026-W44"]
    assert todo["days"] == 4 and todo["new_days"] == 4
    assert fake.bitable_records == before

    # 建模板、设封面、锁上，再迁；迁之前人刚在 W44 那张里填了 FLR1103 的销量（还没到回读的时候）
    _cli(capsys, "temu", "listed", "crafted", "template")
    ctx = reopen(home)
    _fit(fake, ctx)
    _cli(capsys, "temu", "listed", "crafted", "lock")
    ctx = reopen(home)
    fake.bitable_records[_codes(fake, weekly["table_id"])["FLR1103"]["record_id"]]["累计销量"] = 7
    out = crafted.migrate(ctx)
    assert out["failed"] == 0, out["errors"]
    assert out["read_back"]["changed"] == 1
    assert out["deleted"] == {"旧表格": 2, "2026-W44": 2}
    assert out["legacy"] == {"dropped": legacy["table_id"]}
    assert out["views"] == [{"book_id": "2026-W44", "views": [COL1, COL2]}]
    assert out["days"] == ["2026-07-23", "2026-10-25", "2026-10-30", "2026-10-31"]
    assert out["left"] == {"not_migrated": 0, "new_days": 0}
    # 旧表格那张整张删了、记录清掉；W44 那张只剩 3店、视图只有本机两家
    assert legacy["table_id"] not in fake.bitable_tables and listed_table._meta(ctx) == {}
    weekly = _weekly_listed(ctx)
    assert set(_codes(fake, weekly["table_id"])) == {"Deer0006"}
    assert set(weekly["views"]["stores"]) == {COL3, COL4}
    names = _view_names(fake, weekly["table_id"])
    assert COL1 not in names and COL2 not in names
    # 天表按日期先后建在各自那一周里
    days = {d["day"]: d for d in crafted.days(ctx)}
    w44 = crafted.book(ctx, "2026-W44")
    day_tables = [n for n in _tables(fake, w44["app_token"]) if n != "精作款模板"]
    assert day_tables == ["10-30 周五", "10-31 周六"]
    # 重跑：不再删、不再建
    again = crafted.migrate(ctx)
    assert again["deleted"] == {} and again["days"] == [] and again["failed"] == 0

    # 写行：一次多写些；人原来填的值跟着写过去
    written = listed_table.sync_now(ctx, batch=500)
    assert written["created"] == 5, written
    assert set(_codes(fake, days["2026-10-30"]["table_id"])) == {"FLR1103", "FLR1104"}
    assert set(_codes(fake, days["2026-10-31"]["table_id"])) == {"FLR1105"}
    assert _codes(fake, days["2026-07-23"]["table_id"])["FLR0042"]["累计销量"] == 5
    assert _codes(fake, days["2026-10-30"]["table_id"])["FLR1103"]["累计销量"] == 7
    check = listed_table.check(ctx)
    assert check["crafted_not_migrated"] == [] and check["without_record"] == []
    assert listed_table._pending(ctx) == []
    # 旧表格那张没有再建回来
    assert listed_table._meta(ctx) == {}


def test_legacy_table_kept_when_people_added_rows(home, fake, capsys) -> None:
    ctx = _home(home, fake, enabled=False)
    _ledger(fake)
    listed_table.pull(ctx)
    feishu_table.sync(ctx)
    legacy = listed_table._meta(ctx)
    # 人在旧表格那张里自己加了一行
    fake.bitable_records["recHuman"] = {"货号": "人加的", "备注": "别删"}
    fake.bitable_record_tables["recHuman"] = legacy["table_id"]
    set_config(home, "temu.listed.crafted.enabled", True)
    ctx = reopen(home)
    with feishu.Client(ctx) as api:
        crafted.ensure_template(ctx, api)
        crafted.lock_template(ctx, api)
    out = crafted.migrate(ctx)
    assert out["deleted"]["旧表格"] == 2
    assert "kept" in out["legacy"] and legacy["table_id"] in fake.bitable_tables
    assert listed_table._meta(ctx)["table_id"] == legacy["table_id"]


def test_day_failure_waits_an_hour(home, fake, monkeypatch) -> None:
    ctx = _home(home, fake)
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise StageError("复制周表格没成")

    monkeypatch.setattr(crafted, "ensure_book", boom)
    with feishu.Client(ctx) as api:
        with pytest.raises(KeyError):
            crafted.ready_day(ctx, api, "2026-10-30")
        # 一小时内不再试
        with pytest.raises(KeyError):
            crafted.ready_day(ctx, api, "2026-10-30")
    assert len(calls) == 1
    assert "复制周表格没成" in crafted.day_info(ctx, "2026-10-30")["error"]


def test_off_keeps_crafted_stores_in_listed(home, fake) -> None:
    ctx = _home(home, fake, enabled=False)
    _ledger(fake)
    listed_table.pull(ctx)
    assert not crafted.enabled(ctx)
    assert listed_table.store_columns(ctx) == [COL3, COL4, COL1, COL2]
    item = next(i for i in listed_table.rows(ctx) if i["code"] == "FLR1103")
    assert listed_table.place(ctx, item, freeze=False) == "2026-W44"
    # 不读 mooya 底账、没配知识库都开不了
    set_config(home, "temu.listed.crafted.enabled", True)
    assert crafted.enabled(reopen(home))
    set_config(home, "temu.listed.source", "local")
    assert not crafted.enabled(reopen(home))
    set_config(home, "temu.listed.source", "mooya")
    set_config(home, "table.wiki_space_id", "")
    assert not crafted.enabled(reopen(home))
