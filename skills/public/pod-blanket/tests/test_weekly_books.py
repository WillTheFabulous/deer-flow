"""每周选款表（table.weekly）：知识库根目录一本锁住的模板表格，每周从它复制一本周表格，每天一个文件夹（当天上传的
任务的候选表、当天的「选中的款」、「已归档」子文件夹）；任务按上传日期归到那一天。还有把一周的旧任务搬进周表格的迁移
（maintenance migrate-week）。假飞书见 conftest：知识库节点新建 / 整本复制、嵌套文件夹、高级权限和角色。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from conftest import reopen, set_config
from podflow import (
    books,
    candidates,
    cli,
    feishu,
    feishu_table,
    lease,
    listed_table,
    shelf,
    table_registry,
    week_migration,
)
from podflow.util import loads
from test_candidate_control import _action_id, _runs, _tick
from test_candidates import _age_selection
from test_listing import as_old_selected, assert_selected_views
from test_scenes import _setup, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

pytestmark = pytest.mark.usefixtures("spawned")

SPACE = "spcW"
BY = "feishu:ou_tester"
# 10-27 周二 11:00（Asia/Shanghai）：W44（10.26–11.01）
TUESDAY = "2026-10-27T03:00:00+00:00"
# 10-25 周日：早于 table.weekly_since（缺省 2026-10-26），留在旧表格
SUNDAY = "2026-10-25T03:00:00+00:00"


def _weekly(home, seed_image, *, when: str = TUESDAY, weekly: bool = True, **settings):
    """出一批图、聊天框里选中第一张；表格放进知识库、打开默认视图（和每周选款表），任务的上传时间拨到 when。"""
    set_config(home, "table.wiki_space_id", SPACE)
    set_config(home, "table.views", True)
    set_config(home, "table.weekly", weekly)
    for key, value in settings.items():
        set_config(home, key, value)
    ctx, picked = _setup(home, seed_image, approve=1, enabled=False)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE seeds SET created_at = ?", (when,))
    return ctx, picked


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _tables(fake, app: str) -> dict[str, str]:
    """这本表格里的数据表：{表名: table_id}。"""
    return {
        t["name"]: tid
        for tid, t in fake.bitable_tables.items()
        if fake.bitable_table_apps.get(tid) == app
    }


def _gallery(fake, table_id: str) -> dict:
    return next(v for v in fake.bitable_views[table_id] if v["view_type"] == "gallery")


def _folders(fake, app: str) -> dict[str, dict]:
    return {f["id"]: f for f in fake.bitable_folders.values() if f["app"] == app}


def _records(fake, table_id: str) -> list[str]:
    """这张表现在还在的行（删掉的行假飞书里只从 bitable_records 去掉）。"""
    return [
        r
        for r, t in fake.bitable_record_tables.items()
        if t == table_id and r in fake.bitable_records
    ]


def _template_with_fit(ctx, fake) -> dict:
    """先建好模板表格，人在各个画册里把封面效果改成适应（两张模板的「看图」、候选表模板的「已选中」「待选」；
    接口读写不到，假飞书里记作 cover_fit）。"""
    with feishu.Client(ctx) as api:
        tpl = books.ensure_template(ctx, api)
    for meta in tpl["tables"].values():
        for view in fake.bitable_views[meta["table_id"]]:
            if view["view_type"] == "gallery":
                view["cover_fit"] = "fit"
    return tpl


def _named_views(fake, table_id: str) -> dict[str, dict]:
    return {v["view_name"]: v for v in fake.bitable_views[table_id]}


def _as_v4_template(fake, meta: dict) -> None:
    """把一张「候选表模板」装成视图版本 4（第一个表格「待选」、画册「看图」、最后的表格「全部」）。"""
    first, picked, *_rest = fake.bitable_views[meta["table_id"]]
    first["view_name"] = "待选"
    picked["view_name"] = "看图"
    fake.bitable_views[meta["table_id"]] = [
        first,
        picked,
        {"view_id": f"vewOld{meta['table_id']}", "view_name": "全部", "view_type": "grid", "property": {}},
    ]
    meta["views"] = {"pending": first["view_id"], "all": f"vewOld{meta['table_id']}", "gallery": picked["view_id"]}
    meta["views_version"] = 4


def test_candidate_templates_are_upgraded_in_place(home, fake, seed_image) -> None:
    ctx, _picked = _weekly(home, seed_image)
    _template_with_fit(ctx, fake)
    feishu_table.sync(ctx)
    book = books.book(ctx, "2026-W44")
    # 装成 10-30 前建的：模板表格和这本周表格里的「候选表模板」都是视图版本 4
    tpl = books.template(ctx)
    _as_v4_template(fake, tpl["tables"]["candidate"])
    books._save_template(ctx, tpl)
    _as_v4_template(fake, book["templates"]["candidate"])
    books.save_book(ctx, book)
    ctx = reopen(home)
    assert books.has_work(ctx)
    errors: list[dict] = []
    with feishu.Client(ctx) as api:
        out = books.prepare(ctx, api, errors)
    assert errors == [] and out["candidate_templates"] == ["template", "2026-W44"]
    for found in (books.template(ctx)["tables"]["candidate"], books.book(ctx, "2026-W44")["templates"]["candidate"]):
        named = _named_views(fake, found["table_id"])
        # 就地改：「看图」改名「全部画册」（人设的封面效果留着），再建「已选中」「待选」；之后从它复制的候选表就是这一版
        assert list(named) == ["全部", "全部画册", "已选中", "待选"]
        assert named["全部画册"]["cover_fit"] == "fit"
        assert found["views"] == {
            "all": named["全部"]["view_id"],
            "all_gallery": named["全部画册"]["view_id"],
            "picked": named["已选中"]["view_id"],
            "pending": named["待选"]["view_id"],
        }
        assert found["views_version"] == candidates.VIEWS_VERSION
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'candidate_templates_upgraded'") == 1
    assert not books.has_work(ctx)


def test_selected_templates_are_upgraded_in_place(home, fake, seed_image) -> None:
    ctx, _picked = _weekly(home, seed_image, **{"listing.enabled": True})
    _template_with_fit(ctx, fake)
    feishu_table.sync(ctx)
    # 装成 10-30 前建的：模板表格和这本周表格里的「选中的款模板」都是之前那一套视图（人在「看图」设过适应）
    tpl = books.template(ctx)
    book = books.book(ctx, "2026-W44")
    for meta in (tpl["tables"]["selected"], book["templates"]["selected"]):
        meta["views"] = as_old_selected(fake, meta["table_id"])
        meta.pop("views_version")
    books._save_template(ctx, tpl)
    books.save_book(ctx, book)
    ctx = reopen(home)
    assert books.has_work(ctx)
    errors: list[dict] = []
    with feishu.Client(ctx) as api:
        out = books.prepare(ctx, api, errors)
    assert errors == [] and out["selected_templates"] == ["template", "2026-W44"]
    old_galleries = {
        tpl["tables"]["selected"]["views"]["gallery"],
        book["templates"]["selected"]["views"]["gallery"],
    }
    upgraded = (
        books.template(ctx)["tables"]["selected"],
        books.book(ctx, "2026-W44")["templates"]["selected"],
    )
    for found in upgraded:
        # 就地改成五个视图，「看图」还是原来那个（封面效果在），上架状态补齐五个选项
        assert_selected_views(fake, found["table_id"], found["views"])
        assert found["views"]["gallery"] in old_galleries
        assert _gallery(fake, found["table_id"])["cover_fit"] == "fit"
        assert found["views_version"] == feishu_table.SELECTED_VIEWS_VERSION
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM events WHERE event = 'selected_templates_upgraded'"
        )
        == 1
    )
    assert not books.has_work(ctx)
    # 新的一天从这本周表格的模板复制：直接是五个视图（id 和模板里的一样），封面效果跟着走，不用再改
    with feishu.Client(ctx) as api:
        day = books.ready_day(ctx, api, "2026-10-28")
    week_tpl = upgraded[1]
    assert day["views"] == week_tpl["views"]
    assert day["views_version"] == feishu_table.SELECTED_VIEWS_VERSION
    assert_selected_views(fake, day["table_id"], day["views"])
    assert _gallery(fake, day["table_id"])["cover_fit"] == "fit"
    assert not feishu_table._views_work(ctx, day)
    # 升级失败满上限不再试，--retry 清零
    tpl = books.template(ctx)
    tpl["tables"]["selected"].update(
        views_version=1, views_attempts=feishu_table.MAX_ATTEMPTS
    )
    books._save_template(ctx, tpl)
    assert not books.has_work(ctx)
    books.reset_attempts(ctx)
    assert books.has_work(ctx)


def test_names_and_weeks() -> None:
    assert books.book_id("2026-10-26") == "2026-W44"
    assert books.book_id("2026-11-01") == "2026-W44"
    assert books.book_id("2026-11-02") == "2026-W45"
    assert books.week_monday("2026-W44") == dt.date(2026, 10, 26)
    assert books.book_title("2026-10-29") == "POD 选款表 W44（10.26–11.01）"
    assert books.folder_name("2026-10-26") == "10-26 周一"
    assert books.folder_name("2026-11-01") == "11-01 周日"
    assert books.selected_name("2026-10-27") == "10-27 选中的款"
    assert books.archive_name("2026-10-27") == "10-27 已归档"


def test_new_task_goes_into_its_upload_day(home, fake, seed_image, monkeypatch) -> None:
    ctx, picked = _weekly(home, seed_image)
    tpl = _template_with_fit(ctx, fake)
    # 整本复制是异步的：复制件前两次读报「正在复制」，等一等再接着做
    fake.copy_pending = 2
    waits: list[float] = []
    monkeypatch.setattr(books, "_sleep", waits.append)
    # 模板表格在知识库根目录：三张模板表，新建时自带的空表删掉了
    assert fake.wiki_titles[tpl["node_token"]] == books.TEMPLATE_TITLE
    assert set(_tables(fake, tpl["app_token"])) == set(books.TEMPLATE_NAMES.values())

    out = feishu_table.sync(ctx)
    # 锁上了：开着高级权限，「编辑者」三张模板表都只读
    assert out["weekly"]["template_lock"] == {"locked": True}
    assert fake.bitable_advanced[tpl["app_token"]] is True
    reader, editor = fake.bitable_roles[tpl["app_token"]]
    assert editor["role_type"] == "editor" and editor["table_rule_map"] == {
        name: books.READ_ONLY_RULE for name in books.TEMPLATE_NAMES.values()
    }
    # 只改了编辑者，Base 级权限照原样
    assert editor["base_rule_map"] == {"download": True, "copy": True}
    assert reader["table_rule_map"] == {name: {"perm": "read_only"} for name in books.TEMPLATE_NAMES.values()}
    assert out["weekly"]["days"] == ["2026-10-27"]
    book = books.book(ctx, "2026-W44")
    assert fake.wiki_titles[book["node_token"]] == "POD 选款表 W44（10.26–11.01）"
    assert waits[:2] == [2, 3] and fake.bitable_copying[book["app_token"]] == 0
    app = book["app_token"]
    # 复制件带过来的高级权限关掉（周表格要人能改），三张模板表挪进「模板」文件夹，订阅了记录变更事件
    assert fake.bitable_advanced[app] is False
    folders = _folders(fake, app)
    assert folders[book["template_folder_id"]]["name"] == books.TEMPLATE_FOLDER
    for meta in book["templates"].values():
        assert fake.bitable_parents[meta["table_id"]] == book["template_folder_id"]
    assert (app, "bitable") in fake.bitable_subscriptions
    # 那一天：文件夹、里面的「已归档」子文件夹、当天的「选中的款」；选中的那一款写进它，不进旧表格
    day = books.day_info(ctx, "2026-10-27")
    assert folders[day["folder_id"]]["name"] == "10-27 周二"
    assert folders[day["folder_id"]]["parent_id"] is None
    archive = folders[day["archive_folder_id"]]
    assert archive["name"] == "10-27 已归档" and archive["parent_id"] == day["folder_id"]
    assert fake.bitable_tables[day["table_id"]]["name"] == "10-27 选中的款"
    assert fake.bitable_parents[day["table_id"]] == day["folder_id"]
    assert table_registry.lookup(day["table_id"]) == app
    record = ctx.db.scalar(
        "SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],)
    )
    assert fake.bitable_record_tables[record] == day["table_id"]
    assert _records(fake, feishu_table.base(ctx)["table_id"]) == []
    # 「看图」的封面效果一路跟着走：模板表格 → 周表格 → 当天的「选中的款」
    assert _gallery(fake, day["table_id"])["cover_fit"] == "fit"

    # 候选表：从这本周表格的「候选表模板」复制进那天的文件夹，链接是周表格的
    candidates.sync(ctx)
    table = candidates.table_info(ctx, "S01")
    copied = fake.bitable_tables[table["table_id"]]
    assert copied["copied_from"] == book["templates"]["candidate"]["table_id"]
    assert fake.bitable_table_apps[table["table_id"]] == app
    assert fake.bitable_parents[table["table_id"]] == day["folder_id"]
    assert table["url"].startswith(book["url"])
    assert _gallery(fake, table["table_id"])["cover_fit"] == "fit"
    rows = [
        r["record_id"]
        for r in ctx.db.q(
            "SELECT record_id FROM candidate_rows WHERE seed_id = 'S01' AND record_id IS NOT NULL"
        )
    ]
    assert rows and {fake.bitable_record_tables[r] for r in rows} == {table["table_id"]}
    # 复制件的默认视图就是模板的（id 不变），不用再建
    assert candidates.view_ids(ctx, table["table_id"]) == book["templates"]["candidate"]["views"]
    assert set(book["templates"]["candidate"]["views"]) == {"all", "all_gallery", "picked", "pending"}
    assert candidates._views_pending(ctx) == []
    # 三个画册的筛选模板里就是对的（全部画册 / 已选中 / 待选），封面效果跟着走
    named = _named_views(fake, table["table_id"])
    assert list(named) == ["全部", "全部画册", "已选中", "待选"]
    for view, count in (("全部画册", len(candidates.ALL_GALLERY_OPTIONS)), ("已选中", 1), ("待选", 1)):
        assert [c["operator"] for c in named[view]["property"]["filter_info"]["conditions"]] == ["is"] * count
        assert named[view]["cover_fit"] == "fit"
    # 工作台的「选款表」：这一周的周表格，今天的文件夹建好了就打开今天的「选中的款」
    monkeypatch.setattr(books, "utcnow", lambda: dt.datetime(2026, 10, 27, 5, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(feishu_table, "now_iso", lambda: "2026-10-27T05:00:00+00:00")
    url = feishu_table.table_url(ctx)
    assert url.startswith(book["url"]) and f"table={day['table_id']}" in url
    # 对话里人设用 table status 的 url：也是这一周的，旧表格的在 legacy_url
    status = feishu_table.status(ctx)
    assert status["url"] == url and status["legacy_url"] == feishu_table.base(ctx)["url"]
    monkeypatch.setattr(feishu_table, "now_iso", lambda: "2026-10-28T05:00:00+00:00")
    assert feishu_table.table_url(ctx) == book["url"]
    monkeypatch.undo()
    # 归属定下来了；没别的事做
    assert books.place_of(ctx, "S01") == "2026-10-27"
    assert not books.has_work(ctx)
    status = books.status(ctx)
    assert status["template"]["locked_at"] and status["books"][0]["book_id"] == "2026-W44"
    assert status["days"] == [
        {"day": "2026-10-27", "book_id": "2026-W44", "table_id": day["table_id"], "error": None}
    ]


def test_old_tasks_stay_in_the_old_book(home, fake, seed_image) -> None:
    # 上传日期早于 table.weekly_since：照旧进旧表格
    ctx, picked = _weekly(home, seed_image, when=SUNDAY)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    legacy = feishu_table.base(ctx)
    assert books.place(ctx, "S01") is None
    assert books.days(ctx) == [] and books.books(ctx) == []
    table = candidates.table_info(ctx, "S01")
    assert fake.bitable_table_apps[table["table_id"]] == legacy["app_token"]
    record = ctx.db.scalar(
        "SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],)
    )
    assert fake.bitable_record_tables[record] == legacy["table_id"]


def test_task_already_in_old_book_stays_there(home, fake, seed_image) -> None:
    # 开关打开之前就进了旧表格：打开以后也留在旧表格（要搬用 maintenance migrate-week）
    ctx, _ = _weekly(home, seed_image, weekly=False)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    set_config(home, "table.weekly", True)
    ctx = reopen(home)
    assert books.place(ctx, "S01") is None
    feishu_table.sync(ctx)
    assert books.days(ctx) == []
    assert ctx.db.q1("SELECT day FROM seed_places WHERE seed_id = 'S01'")["day"] is None


def test_day_folder_resumes_after_a_failed_move(home, fake, seed_image) -> None:
    ctx, _ = _weekly(home, seed_image)
    with feishu.Client(ctx) as api:
        books.ensure_book(ctx, api, "2026-10-27")
    # 当天的「选中的款」复制好了、挪进文件夹失败：记在那一天上，一小时内不再试
    fake.bitable_fail.add(("POST", "/move"))
    with feishu.Client(ctx) as api, pytest.raises(KeyError, match="10-27"):
        books.ready_day(ctx, api, "2026-10-27")
    failed = books.day_info(ctx, "2026-10-27")
    assert failed["error"] and failed["copied_table_id"] and not failed.get("table_id")
    with feishu.Client(ctx) as api, pytest.raises(KeyError, match="过一会儿再试"):
        books.ready_day(ctx, api, "2026-10-27")
    # --retry 之后接着挪，不再复制一张
    fake.bitable_fail.clear()
    books.reset_attempts(ctx)
    with feishu.Client(ctx) as api:
        day = books.ready_day(ctx, api, "2026-10-27")
    assert day["table_id"] == failed["copied_table_id"] and "error" not in day
    names = list(_tables(fake, day["app_token"]))
    assert names.count("10-27 选中的款") == 1


def test_events_from_a_week_book(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx, _ = _weekly(home, seed_image, **{"table.control_row": True})
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    book = books.book(ctx, "2026-W44")
    day = books.day_info(ctx, "2026-10-27")
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    legacy = feishu_table.base(ctx)["app_token"]
    record_id = _tick(ctx, fake, "accept")
    assert _action_id(ctx, "accept") in _records(fake, table_id)

    def event(**kw):
        args = {
            "file_token": book["app_token"],
            "table_id": table_id,
            "record_ids": [record_id],
            "by": "feishu:ou_evt",
            **kw,
        }
        return candidates.control_event(ctx, **args)

    # 别的表格发来的同一张表：不认
    assert event(file_token=legacy)["ignored"] == "不是选款表"
    # 周表格里的候选表：按钮行照样拉起选款完成
    out = event()
    assert out["started"].startswith("BG")
    assert _runs(spawned, "accept") == [
        ["table", "accept", "--seed", "S01", "--by", "feishu:ou_evt", "--source", "table"]
    ]
    # 当天的「选中的款」：交给下载确认（这里关着），不当按钮行看
    sel = event(table_id=day["table_id"], record_ids=["recX"])
    assert sel == {"ignored": "下载确认关着（table.download_confirm）"}


def test_archive_moves_into_the_day_archive_folder(home, fake, seed_image) -> None:
    ctx, _ = _weekly(home, seed_image)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by=BY)
    day = books.day_info(ctx, "2026-10-27")
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    shelf.archive(ctx, "S01", by=BY, force=True)
    assert candidates.sync(ctx)["moved"] == 1
    assert fake.bitable_parents[table_id] == day["archive_folder_id"]
    # 旧表格里没有建「已归档」文件夹
    legacy = feishu_table.base(ctx)["app_token"]
    assert _folders(fake, legacy) == {}
    # 调出：挪回那一天的文件夹（不是根目录）
    shelf.restore(ctx, "S01", by=BY)
    assert candidates.sync(ctx)["moved"] == 1
    assert fake.bitable_parents[table_id] == day["folder_id"]


def test_listed_rows_go_to_the_listing_week(home, fake, seed_image) -> None:
    ctx, _ = _weekly(home, seed_image)

    def item(code: str, listed_at: str, record_id: str | None = None) -> dict:
        return {"code": code, "listed_at": listed_at, "record_id": record_id}

    assert listed_table.place(ctx, item("C1", "2026-10-30T02:00:00+00:00")) == "2026-W44"
    assert listed_table.place(ctx, item("C2", "2026-11-02T02:00:00+00:00")) == "2026-W45"
    # 早于 table.weekly_since 的、已经写进旧表格的：旧表格那一张
    assert listed_table.place(ctx, item("C3", "2026-10-20T02:00:00+00:00")) is None
    assert listed_table.place(ctx, item("C4", "2026-10-30T02:00:00+00:00", "recOld")) is None
    # 定下来就不变（freeze=False 只算不记）
    assert listed_table.place(ctx, item("C1", "2026-10-20T02:00:00+00:00")) == "2026-W44"
    assert listed_table.place(ctx, item("C5", "2026-10-30T02:00:00+00:00"), freeze=False) == "2026-W44"
    placed = {r["code"]: r["book_id"] for r in ctx.db.q("SELECT code, book_id FROM listed_books")}
    assert placed == {"C1": "2026-W44", "C2": "2026-W45", "C3": ""}


def test_poll_and_events_wait_while_migrating(home, fake, seed_image, capsys) -> None:
    ctx, _ = _weekly(home, seed_image)
    other = reopen(home)
    assert lease.take(other, books.MIGRATE_LEASE, books.MIGRATE_SECONDS)
    assert books.migrating(ctx) and not books.migrating(other)
    code, out = _cli(capsys, "table", "poll")
    assert code == 0 and "正在迁移" in out["skipped"]
    code, out = _cli(
        capsys,
        "table",
        "control",
        "--file",
        "bascnX",
        "--table",
        "tblX",
        "--records",
        "recX",
        "--by",
        "feishu:ou_x",
    )
    assert code == 0 and "正在迁移" in out["ignored"]
    lease.drop(other, books.MIGRATE_LEASE)
    assert not books.migrating(ctx)


def _old_book(home, fake, seed_image, *, accepted: bool = False):
    """开关打开之前：S01（10-27 上传）的候选表、「选中的款」都在旧表格里；人在旧表里填了几格，还自己加了一列。
    accepted：点过选款完成（行都定了，同步不会再补这些行，和线上本周的任务一样）。"""
    ctx, picked = _weekly(home, seed_image, weekly=False)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    if accepted:
        _age_selection(ctx)
        candidates.accept(ctx, "S01", by=BY)
    legacy = feishu_table.base(ctx)
    opened = ctx.db.q(
        "SELECT asset_id, record_id FROM candidate_rows WHERE seed_id = 'S01' AND record_id IS NOT NULL"
        " ORDER BY asset_id"
    )
    first = dict(opened[0])
    fake.bitable_records[first["record_id"]].update({"没选原因": ["颜色不对"], "备注": "再看看"})
    selected = ctx.db.scalar(
        "SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],)
    )
    fake.bitable_tables[legacy["table_id"]]["fields"].append(
        {"field_name": "使用情况", "type": 4, "property": {"options": [{"name": "已用", "color": 0}]}}
    )
    fake.bitable_records[selected].update({"使用情况": ["已用"], "备注": "主推"})
    # 模板表格第 0 步就建好了（线上也是），开关还关着
    with feishu.Client(ctx) as api:
        books.ensure_template(ctx, api)
    return ctx, picked, first, selected


def test_migrate_week_moves_a_week_into_its_book(home, fake, seed_image, capsys) -> None:
    ctx, picked, first, selected = _old_book(home, fake, seed_image)
    legacy = feishu_table.base(ctx)
    old_table = candidates.table_info(ctx, "S01")["table_id"]
    old_rows = len(_records(fake, old_table))

    code, plan = _cli(
        capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY, "--dry-run"
    )
    assert code == 0, plan
    assert plan["book"] == {"title": "POD 选款表 W44（10.26–11.01）", "url": None, "exists": False}
    assert plan["days"]["2026-10-27"]["seeds"] == ["S01"]
    assert plan["days"]["2026-10-27"]["folder"] == "10-27 周二"
    assert plan["days"]["2026-10-27"]["selected_rows"] == 1
    assert plan["delete"]["tables"][0]["table_id"] == old_table
    assert plan["delete"]["selected_rows"] == 1
    assert plan["user_columns"]["selected"] == ["使用情况"]
    assert plan["human_cells"] == {
        "candidates": {"没选原因": 1, "备注": 1},
        "selected": {"备注": 1},
    }
    assert plan["user_cells"] == {"选中的款·使用情况": 1}
    assert plan["missing_tables"] == [] and plan["unknown_rows"] == {}
    # dry-run 什么都不建、不改
    assert books.books(ctx) == [] and old_table in fake.bitable_tables

    code, out = _cli(capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY)
    assert code == 0 and out["verified"] is True, out
    day = books.day_info(ctx, "2026-10-27")
    assert out["seeds"] == {"S01": "2026-10-27"} and out["days"] == {"2026-10-27": day["table_id"]}
    # 候选表：新表在那天的文件夹里，行数和旧表一样，人填的格子跟着走；旧表删了
    new = candidates.table_info(ctx, "S01")
    assert new["table_id"] != old_table and old_table not in fake.bitable_tables
    assert fake.bitable_parents[new["table_id"]] == day["folder_id"]
    assert len(_records(fake, new["table_id"])) == old_rows
    # 重写的行一批批建（飞书写一行要 3–4 秒），不一行行建
    path = f"/bitable/v1/apps/{day['app_token']}/tables/{new['table_id']}/records"
    assert ("POST", path + "/batch_create") in fake.bitable_calls
    assert ("POST", path) not in fake.bitable_calls
    moved = ctx.db.scalar(
        "SELECT record_id FROM candidate_rows WHERE asset_id = ?", (first["asset_id"],)
    )
    assert fake.bitable_records[moved]["没选原因"] == ["颜色不对"]
    assert fake.bitable_records[moved]["备注"] == "再看看"
    # 「选中的款」：旧行删了，新行在那天的表里，人填的跟着走；人自己加的列缺省不搬（值在备份里）
    assert selected not in fake.bitable_records
    left = _records(fake, legacy["table_id"])
    assert left == [], {r: fake.bitable_records[r] for r in left}
    row = ctx.db.scalar("SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],))
    assert fake.bitable_record_tables[row] == day["table_id"]
    assert fake.bitable_records[row]["备注"] == "主推"
    assert "使用情况" not in fake.bitable_records[row]
    assert "使用情况" not in [f["field_name"] for f in fake.bitable_tables[day["table_id"]]["fields"]]
    assert books.place_of(ctx, "S01") == "2026-10-27"
    # 备份：旧表的记录、业务库相关的行
    (path,) = out["backups"]
    backup = loads(Path(path).read_text(encoding="utf-8"))
    assert backup["candidates"]["S01"]["table_id"] == old_table
    assert len(backup["candidates"]["S01"]["records"]) == old_rows
    assert list(backup["selected"]) == [selected]
    assert backup["selected"][selected]["使用情况"] == ["已用"]
    assert backup["db"]["candidate_tables"][0]["table_id"] == old_table
    # 重跑：已经搬完，什么都不做
    code, again = _cli(capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY)
    assert code == 0 and again["already"] is True
    # 之后的同步照常写新表，不再碰旧表格
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    assert _records(fake, legacy["table_id"]) == []


def test_migrate_week_stops_when_check_fails_and_resumes(
    home, fake, seed_image, capsys, monkeypatch
) -> None:
    ctx, picked, first, selected = _old_book(home, fake, seed_image)
    old_table = candidates.table_info(ctx, "S01")["table_id"]
    # 新表的备注写不进去：核对不过，旧表都还在
    original = week_migration._batch_update

    def broken(api, app, table_id, updates):
        updates = [(r, {k: v for k, v in f.items() if k != "备注"}) for r, f in updates]
        return original(api, app, table_id, updates)

    monkeypatch.setattr(week_migration, "_batch_update", broken)
    code, out = _cli(
        capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY, "--keep-columns"
    )
    monkeypatch.setattr(week_migration, "_batch_update", original)
    assert code == 0 and out["verified"] is False
    assert any("备注" in p for p in out["problems"])
    assert old_table in fake.bitable_tables and selected in fake.bitable_records
    stages = week_migration.state(ctx, "2026-W44")["seeds"]["S01"]["stage"]
    assert stages == "rebuilt"
    # 修好后重跑：接着做，不再建表，搬完删旧的
    tables_before = set(fake.bitable_tables)
    code, out = _cli(capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY)
    assert code == 0 and out["verified"] is True, out
    assert set(fake.bitable_tables) == tables_before - {old_table}
    assert selected not in fake.bitable_records
    # --keep-columns：人加的列在新表里建上了，值也搬了过去（第一次跑时定下来的，重跑照旧）
    day = books.day_info(ctx, "2026-10-27")
    row = ctx.db.scalar("SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],))
    assert fake.bitable_records[row]["使用情况"] == ["已用"]
    assert "使用情况" in [f["field_name"] for f in fake.bitable_tables[day["table_id"]]["fields"]]


def test_migrate_week_fills_rows_a_failed_batch_missed(
    home, fake, seed_image, capsys, monkeypatch
) -> None:
    ctx, picked, first, selected = _old_book(home, fake, seed_image, accepted=True)
    old_table = candidates.table_info(ctx, "S01")["table_id"]
    old_rows = len(_records(fake, old_table))
    assert not candidates._pending(ctx)
    monkeypatch.setattr(candidates.time, "sleep", lambda s: None)
    # 新候选表的行一批批建：飞书报错（重试一次也不成），这一批没建上（同步时逐行补也报错）；核对不过，旧表都还在
    fake.bitable_fail.add(("POST", "/records"))
    code, out = _cli(capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY)
    assert code == 0 and out["verified"] is False
    assert any("在新候选表里没有行" in p for p in out["problems"])
    assert old_table in fake.bitable_tables and selected in fake.bitable_records
    assert week_migration.state(ctx, "2026-W44")["seeds"]["S01"]["stage"] == "rebuilt"
    # 报错的那一批飞书那边其实建了（假装）：新表里多一行业务库不认识的
    new_table = candidates.table_info(ctx, "S01")["table_id"]
    fake.bitable_records["recOrphan"] = {"素材": "报错时建出来的"}
    fake.bitable_record_tables["recOrphan"] = new_table
    # 重跑：重建过的任务也再过一遍，缺的行补上，业务库不认识的行删掉，核对通过才删旧表
    fake.bitable_fail.clear()
    code, out = _cli(capsys, "maintenance", "migrate-week", "--week", "2026-W44", "--by", BY)
    assert code == 0 and out["verified"] is True, out
    assert len(_records(fake, new_table)) == old_rows
    assert "recOrphan" not in fake.bitable_records
    assert week_migration.state(ctx, "2026-W44")["orphans_dropped"] == 1
    assert old_table not in fake.bitable_tables


def test_drop_scene_images_column_everywhere(home, fake, seed_image, capsys) -> None:
    """2026-10-29 起不放套图缩略图：各张「选中的款」和两本里的「选中的款模板」都删掉「场景图」列，可以重跑。"""
    ctx, picked = _weekly(home, seed_image)
    feishu_table.sync(ctx)
    legacy = feishu_table.base(ctx)
    day = books.day_info(ctx, "2026-10-27")
    tpl = books.template(ctx)
    book = books.book(ctx, "2026-W44")
    tables = [
        legacy["table_id"],
        day["table_id"],
        tpl["tables"]["selected"]["table_id"],
        book["templates"]["selected"]["table_id"],
    ]
    # 上线前建的表都有这一列（现在新建的已经没有了），旧行里还有缩略图
    for table_id in tables:
        assert "场景图" not in [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
        fake.bitable_tables[table_id]["fields"].append({"field_name": "场景图", "type": 17})
    record = ctx.db.scalar("SELECT record_id FROM table_rows WHERE asset_id = ?", (picked[0],))
    fake.bitable_records[record]["场景图"] = [{"file_token": "fileOld"}]
    day["extra_fields"] = sorted({*day.get("extra_fields", []), "场景图"})
    books.save_day(ctx, day)

    code, plan = _cli(capsys, "maintenance", "drop-scene-images", "--by", BY, "--dry-run")
    assert code == 0 and plan["dry_run"] is True and plan["dropped"] == []
    assert [t["has_column"] for t in plan["tables"]] == [True, True, True, True]
    assert "场景图" in fake.bitable_records[record]
    assert "场景图" in books.day_info(ctx, "2026-10-27")["extra_fields"]
    code, out = _cli(capsys, "maintenance", "drop-scene-images", "--by", BY)
    assert code == 0 and len(out["dropped"]) == 4 and out["errors"] == []
    for table_id in tables:
        assert "场景图" not in [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
    assert "场景图" not in fake.bitable_records[record]
    assert "场景图" not in books.day_info(ctx, "2026-10-27")["extra_fields"]
    # 重跑：都没有这一列了，什么都不删
    code, again = _cli(capsys, "maintenance", "drop-scene-images", "--by", BY)
    assert code == 0 and again["dropped"] == []
    assert [t["has_column"] for t in again["tables"]] == [False, False, False, False]
