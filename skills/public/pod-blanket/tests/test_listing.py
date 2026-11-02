"""上架状态：「选中的款」加三列（上架状态 / 上架日期 / 上架备注），旧行补「待上架」，默认视图（全部 · 看图 · 待上架 ·
上架中 · 已上架，之前那一套就地升级），回读、改、任务进度第七步「上架」。"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from conftest import reopen, set_config
from podflow import cli, doctor, feishu_table, listing, tasks
from podflow.util import iso, utcnow
from test_scenes import _setup, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

# 假拉起后台进程（表格同步、套图）：只要它的副作用，不看拉起了什么
pytestmark = pytest.mark.usefixtures("spawned")
BY = "feishu:ou_tester"
LISTING_NAMES = ["上架状态", "上架日期", "上架备注"]
# 建默认视图时「上架状态」补齐的五个选项（「上架中」视图要用到「上架中」）
STATE_OPTIONS = ["待上架", "已上架", "不上了", "上架中", "上架失败"]
SELECTED_VIEWS = ["全部", "看图", "待上架", "上架中", "已上架"]
VIEW_KEYS = {
    "all": "全部",
    "gallery": "看图",
    "pending": "待上架",
    "in_progress": "上架中",
    "listed": "已上架",
}


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _enable(home) -> None:
    set_config(home, "listing.enabled", True)
    set_config(home, "table.views", True)


def _record(ctx, asset_id: str) -> str:
    return ctx.db.scalar(
        "SELECT record_id FROM table_rows WHERE asset_id = ?", (asset_id,)
    )


def _day_ms(day: dt.date) -> int:
    local = dt.datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(local.timestamp() * 1000)


def _selected_order(names: list[str]) -> list[str]:
    """「选中的款」表格视图该有的列序：素材、缩略图和两个下载链接排最前，其余照表里的先后（「排序」藏起来）。"""
    front = [n for n in feishu_table.SELECTED_FRONT if n in names]
    return front + [
        n for n in names if n not in front and n != feishu_table.DOWNLOAD_ORDER_FIELD
    ]


def _state_field(fake, table_id: str) -> dict:
    fields = fake.bitable_tables[table_id]["fields"]
    index = [f["field_name"] for f in fields].index("上架状态")
    return fake._field_view(index, fields[index])


def _state_filter(
    fake, table_id: str, options: list[str], *, empty: bool = False
) -> dict:
    """上架状态是这几个选项之一（empty：或为空）：选项 id 按假飞书列字段时给的取，一个「是」只放一个 id。"""
    field = _state_field(fake, table_id)
    ids = {o["name"]: o["id"] for o in field["property"]["options"]}
    conditions = [
        {
            "field_id": field["field_id"],
            "operator": "is",
            "value": json.dumps([ids[name]]),
        }
        for name in options
    ]
    if empty:
        conditions.append({"field_id": field["field_id"], "operator": "isEmpty"})
    return {
        "conjunction": "or" if len(conditions) > 1 else "and",
        "conditions": conditions,
    }


def _views(fake, table_id: str) -> dict[str, dict]:
    return {v["view_name"]: v for v in fake.bitable_views[table_id]}


def assert_selected_views(fake, table_id: str, ids: dict[str, str]) -> None:
    """「选中的款」五个视图：全部（第一个，不筛选）· 看图 · 待上架（待上架 / 上架失败 / 空，看图一样）· 上架中 · 已上架；
    ids 是记下的五个 id。"""
    views = _views(fake, table_id)
    assert list(views) == SELECTED_VIEWS
    assert [v["view_type"] for v in views.values()] == [
        "grid",
        "gallery",
        "grid",
        "grid",
        "grid",
    ]
    assert (views["全部"].get("property") or {}).get("filter_info") is None
    pending = _state_filter(fake, table_id, ["待上架", "上架失败"], empty=True)
    assert views["待上架"]["property"]["filter_info"] == pending
    assert views["看图"]["property"]["filter_info"] == pending
    assert views["上架中"]["property"]["filter_info"] == _state_filter(
        fake, table_id, ["上架中"]
    )
    assert views["已上架"]["property"]["filter_info"] == _state_filter(
        fake, table_id, ["已上架"]
    )
    assert ids == {key: views[name]["view_id"] for key, name in VIEW_KEYS.items()}
    assert [
        o["name"] for o in _state_field(fake, table_id)["property"]["options"]
    ] == STATE_OPTIONS


def as_old_selected(fake, table_id: str) -> dict[str, str]:
    """把一张「选中的款」（或「选中的款模板」）装成 2026-10-30 前建的那一套：第一个表格「待上架」（筛选待上架或为空）、
    画册「看图」（人设过封面效果）、表格「已上架」「全部」，上架状态只有人填的三个选项。返回那时记的四个视图 id。"""
    whole, gallery, _pending, _progress, listed = fake.bitable_views[table_id]
    state = next(
        f
        for f in fake.bitable_tables[table_id]["fields"]
        if f["field_name"] == "上架状态"
    )
    state["property"]["options"] = state["property"]["options"][:3]
    whole["view_name"] = "待上架"
    whole["property"] = {
        "filter_info": _state_filter(fake, table_id, ["待上架"], empty=True)
    }
    gallery["cover_fit"] = "fit"
    old_all = f"vewOldAll{table_id}"
    fake.bitable_views[table_id] = [
        whole,
        gallery,
        listed,
        {"view_id": old_all, "view_name": "全部", "view_type": "grid", "property": {}},
    ]
    sort = fake.bitable_view_sorts.get((table_id, whole["view_id"]))
    if sort:
        fake.bitable_view_sorts[(table_id, old_all)] = sort
    return {
        "pending": whole["view_id"],
        "listed": listed["view_id"],
        "all": old_all,
        "gallery": gallery["view_id"],
    }


def test_new_base_has_listing_columns_and_views(home, fake, seed_image) -> None:
    _enable(home)
    ctx, picked = _setup(home, seed_image, approve=2)
    feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    table = fake.bitable_tables[info["table_id"]]
    names = [f["field_name"] for f in table["fields"]]
    assert names[-3:] == LISTING_NAMES
    by_name = {f["field_name"]: f for f in table["fields"]}
    # 建表时三个人填的选项；建默认视图时补齐「上架中」「上架失败」（「上架中」视图要用到），说明照旧
    assert [
        o["name"] for o in by_name["上架状态"]["property"]["options"]
    ] == STATE_OPTIONS
    assert "程序只读" in by_name["上架状态"]["description"]["text"]
    # 新进表的行写「待上架」；新表建表时就有三列，不用补
    for asset_id in picked:
        assert fake.bitable_records[_record(ctx, asset_id)]["上架状态"] == "待上架"
    assert listing.ready(ctx) and info["listing_backfilled"] is True
    # 默认视图：第一个表格「全部」不筛选、画册「看图」，再建「待上架」「上架中」「已上架」；上架失败的算待上架
    assert_selected_views(fake, info["table_id"], info["views"])
    assert info["views_version"] == feishu_table.SELECTED_VIEWS_VERSION
    # 新表的第一个表格本来就叫「全部」：不用改名，建视图也不用等名字放开
    assert fake.view_waits == []
    # 四个表格视图开头是素材、缩略图和两个下载链接，其余照表里的先后；画册不动（飞书缺省的前 4 列）
    for name in ("全部", "待上架", "上架中", "已上架"):
        assert fake.visible_names(info["table_id"], name) == _selected_order(names)
    assert fake.visible_names(info["table_id"], "待上架")[:4] == [
        "素材",
        "缩略图",
        "下载原图",
        "下载套图",
    ]
    assert fake.visible_names(info["table_id"], "看图") == names[:4]
    assert info["views_layout"] == feishu_table.LAYOUT_VERSION
    # 程序给的「选中的款」链接直接打开「待上架」
    assert feishu_table.table_url(ctx).endswith(
        f"?table={info['table_id']}&view={info['views']['pending']}"
    )
    status = feishu_table.status(ctx)
    assert status["listing"]["ready"] and status["views"]["ids"] == info["views"]
    assert status["link"] == feishu_table.table_url(ctx)
    # 再同步：不重复建列、建视图
    before = len(fake.bitable_views[info["table_id"]])
    feishu_table.sync(ctx)
    assert len(fake.bitable_views[info["table_id"]]) == before


def test_old_base_gets_columns_and_backfill(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=3)
    feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    table = fake.bitable_tables[info["table_id"]]
    assert not set(LISTING_NAMES) & {f["field_name"] for f in table["fields"]}
    # 有人先手动加了「上架状态」列、标了一行已上架
    table["fields"].append(dict(feishu_table.LISTING_FIELDS[0]))
    done = _record(ctx, picked[0])
    fake.bitable_records[done]["上架状态"] = "已上架"

    _enable(home)
    ctx = reopen(home)
    assert feishu_table._has_work(ctx)
    feishu_table.sync(ctx)
    names = [f["field_name"] for f in table["fields"]]
    # 只补缺的两列；空着的格子补「待上架」，已经标了的不动
    assert names.count("上架状态") == 1 and set(LISTING_NAMES) <= set(names)
    assert fake.bitable_records[done]["上架状态"] == "已上架"
    for asset_id in picked[1:]:
        assert fake.bitable_records[_record(ctx, asset_id)]["上架状态"] == "待上架"
    info = feishu_table.base(ctx)
    assert info["listing_backfilled"] and listing.ready(ctx) and info["views"]
    assert not feishu_table._has_work(ctx)


def test_old_views_get_download_columns_first(home, fake, seed_image) -> None:
    _enable(home)
    ctx, _ = _setup(home, seed_image, approve=1)
    feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    table_id = info["table_id"]
    table = fake.bitable_tables[table_id]
    # 装成 2026-10-27 前建好的视图：列照建表时的先后；人在「下载套图」后面加过一列「下载情况」
    for view in fake.bitable_views[table_id]:
        view.pop("visible", None)
    info.pop("views_layout")
    feishu_table._save_base(ctx, info)
    before = [f["field_name"] for f in table["fields"]]
    table["fields"].insert(
        before.index("下载套图") + 1, {"field_name": "下载情况", "type": 1}
    )
    names = [f["field_name"] for f in table["fields"]]
    gallery = fake.visible_names(table_id, "看图")
    assert feishu_table._has_work(ctx)
    calls = len(fake.bitable_calls)
    out = feishu_table.sync(ctx)
    assert out["views"]["views"] == info["views"]
    # 下载链接挪到前面，人加的「下载情况」跟着「下载套图」；其余照原来的先后
    expected = ["素材", "缩略图", "下载原图", "下载套图", "下载情况"] + [
        n
        for n in names
        if n not in ("素材", "缩略图", "下载原图", "下载套图", "下载情况")
    ]
    for name in ("全部", "待上架", "上架中", "已上架"):
        assert fake.visible_names(table_id, name) == expected
    # 只摆列：不重建视图、不改筛选，画册不动
    made = fake.bitable_calls[calls:]
    assert not [c for c in made if c[0] in ("POST", "PATCH") and "/views" in c[1]]
    assert not [c for c in made if "/card" in c[1]]
    assert fake.visible_names(table_id, "看图") == gallery
    info = feishu_table.base(ctx)
    assert info["views_layout"] == feishu_table.LAYOUT_VERSION
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM events WHERE event = 'table_views_arranged'"
        )
        == 1
    )
    assert not feishu_table._has_work(ctx)


def _view_calls(fake, since: int, table_id: str) -> list[tuple]:
    return [
        c for c in fake.bitable_calls[since:] if f"/tables/{table_id}/views" in c[1]
    ]


def test_old_views_are_upgraded_in_place(home, fake, seed_image) -> None:
    _enable(home)
    set_config(home, "table.download_confirm", True)
    ctx, _picked = _setup(home, seed_image, approve=2)
    feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    table_id = info["table_id"]
    # 装成 2026-10-30 前建的那一套（表格信息里没有 views_version）
    old = as_old_selected(fake, table_id)
    info["views"] = old
    info.pop("views_version")
    feishu_table._save_base(ctx, info)
    ctx = reopen(home)
    assert feishu_table._has_work(ctx)
    calls = len(fake.bitable_calls)
    out = feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    assert out["views"] == {"views": info["views"]}
    assert_selected_views(fake, table_id, info["views"])
    assert info["views_version"] == feishu_table.SELECTED_VIEWS_VERSION
    # 就地改：第一个表格（原来的「待上架」）改名「全部」、去掉筛选；画册还是原来那个（人设的封面效果在）；
    # 原来的「已上架」「全部」删掉，三个状态视图在画册后面新建
    assert info["views"]["all"] == old["pending"]
    assert info["views"]["gallery"] == old["gallery"]
    assert _views(fake, table_id)["看图"]["cover_fit"] == "fit"
    left = {v["view_id"] for v in fake.bitable_views[table_id]}
    assert old["listed"] not in left and old["all"] not in left
    # 「待上架」这个名字是第一个表格刚让出来的：等一秒再建
    assert fake.view_waits == [1.0]
    # 五个视图都按「排序」排：原来排好的两个不重设（v3 排序接口有频率限制），只设新建的三个
    sorts = [c for c in _view_calls(fake, calls, table_id) if c[1].endswith("/sort")]
    assert len(sorts) == 3
    for view in fake.bitable_views[table_id]:
        assert fake.bitable_view_sorts[(table_id, view["view_id"])] == [
            {"field": feishu_table.DOWNLOAD_ORDER_FIELD, "desc": False}
        ]
    names = [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
    for name in ("全部", "待上架", "上架中", "已上架"):
        assert fake.visible_names(table_id, name) == _selected_order(names)
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM events WHERE event = 'table_views_upgraded'"
        )
        == 1
    )
    # 程序给的「选中的款」链接打开新的「待上架」
    assert feishu_table.table_url(ctx).endswith(
        f"?table={table_id}&view={info['views']['pending']}"
    )
    # 只改一次
    assert not feishu_table._views_work(ctx, info)
    calls = len(fake.bitable_calls)
    feishu_table.sync(ctx)
    assert _view_calls(fake, calls, table_id) == []


def test_upgrade_resumes_after_a_failed_step(home, fake, seed_image) -> None:
    _enable(home)
    set_config(home, "table.download_confirm", True)
    ctx, _picked = _setup(home, seed_image, approve=1)
    feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    table_id = info["table_id"]
    old = as_old_selected(fake, table_id)
    info["views"] = old
    info.pop("views_version")
    feishu_table._save_base(ctx, info)
    # 删旧视图失败：删在改名之前，这一轮什么视图都没动，下一轮还认得出是之前那一套
    fake.bitable_fail.add(("DELETE", "/views/"))
    feishu_table.sync(ctx)
    assert list(_views(fake, table_id)) == ["待上架", "看图", "已上架", "全部"]
    assert feishu_table.base(ctx)["views_attempts"] == 1
    fake.bitable_fail.clear()
    # 「上架中」这个名字一直不放开：等 1、2、4 秒都没建成，这一轮记一次失败（前面几步已经做了）
    fake.bitable_view_reserved[(table_id, "上架中")] = 9
    out = feishu_table.sync(ctx)
    assert any("ViewNameDuplicated" in e["error"] for e in out["errors"])
    info = feishu_table.base(ctx)
    assert info["views_attempts"] == 2 and "ViewNameDuplicated" in info["views_error"]
    assert list(_views(fake, table_id)) == ["全部", "看图", "待上架"]
    assert fake.view_waits == [1.0, 1.0, 2.0, 4.0]
    assert feishu_table._views_work(ctx, info)
    # 下一轮接着做：第一个表格已经叫「全部」，不再删视图、不再改名，「待上架」认回来，补建「上架中」「已上架」
    fake.bitable_view_reserved.clear()
    calls = len(fake.bitable_calls)
    feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    assert_selected_views(fake, table_id, info["views"])
    assert info["views"]["all"] == old["pending"]
    assert info["views_attempts"] == 0 and not info.get("views_error")
    made = _view_calls(fake, calls, table_id)
    assert not [c for c in made if c[0] == "DELETE"]
    assert len([c for c in made if c[0] == "POST"]) == 2
    # 上一轮建了、没来得及设排序的「待上架」这一轮也补上：五个视图都按「排序」排
    for view in fake.bitable_views[table_id]:
        assert fake.bitable_view_sorts[(table_id, view["view_id"])] == [
            {"field": feishu_table.DOWNLOAD_ORDER_FIELD, "desc": False}
        ]


def test_listing_failure_retries_then_stops(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    feishu_table.sync(ctx)
    _enable(home)
    ctx = reopen(home)
    fake.bitable_fail.add(("POST", "/fields"))
    for _ in range(feishu_table.MAX_ATTEMPTS):
        feishu_table.sync(ctx)
    info = feishu_table.base(ctx)
    assert info["listing_attempts"] == feishu_table.MAX_ATTEMPTS
    assert feishu_table.status(ctx)["listing"]["error"]
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert checks["「选中的款」有上架状态三列"]["ok"] is False
    # 视图要等上架状态那一列有了才建
    assert not info.get("views")
    fake.bitable_fail.clear()
    feishu_table.sync(ctx, retry=True)
    info = feishu_table.base(ctx)
    assert listing.ready(ctx) and info["views"] and not info.get("listing_error")


def test_refresh_set_and_task_step(home, fake, seed_image, capsys) -> None:
    _enable(home)
    ctx, picked = _setup(home, seed_image, approve=2)
    feishu_table.sync(ctx)
    first, second = picked
    (task,) = tasks.summaries(ctx)
    assert task["steps"][-1]["key"] == "listing"
    assert task["steps"][-1]["text"] == "上架 0/2"
    # 人在表里标了已上架、日期和备注：回读进库
    today = dt.date(2026, 10, 25)
    fake.bitable_records[_record(ctx, first)].update(
        {"上架状态": "已上架", "上架日期": _day_ms(today), "上架备注": "店铺 A123"}
    )
    out = listing.refresh(ctx)
    assert out["changed"] == [{"asset_id": first, "state": "已上架"}]
    assert listing.refresh(ctx)["changed"] == []
    info = listing.status(ctx, seed_id="S01")
    assert info["counts"] == {"listed": 1, "skipped": 0, "pending": 1}
    item = next(i for i in info["items"] if i["asset_id"] == first)
    assert (item["state"], item["listed_on"], item["note"]) == (
        "已上架",
        "2026-10-25",
        "店铺 A123",
    )
    assert item["first_listed_at"]
    (task,) = tasks.summaries(ctx)
    assert task["steps"][-1]["text"] == "上架 1/2"
    assert task["counts"]["listing_listed"] == 1

    # POD 对话里改：写进飞书那一格，再写库
    code, out = _cli(
        capsys,
        "listing",
        "set",
        second,
        "--state",
        "不上了",
        "--note",
        "撞款",
        "--by",
        BY,
    )
    assert code == 0 and out["state"] == "不上了"
    row = fake.bitable_records[_record(ctx, second)]
    assert (row["上架状态"], row["上架备注"]) == ("不上了", "撞款")
    (task,) = tasks.summaries(ctx)
    assert task["steps"][-1]["state"] == "done"
    assert task["steps"][-1]["text"] == "上架 1/2（不上 1）"
    code, out = _cli(capsys, "listing", "status", "--asset", f"{first},{second}")
    assert out["counts"] == {"listed": 1, "skipped": 1, "pending": 0}
    # 不是选中的款、写错的日期：拒绝
    other = ctx.db.scalar(
        "SELECT asset_id FROM assets WHERE human_decision IS NULL OR human_decision <> 'approved'"
        " ORDER BY asset_id LIMIT 1"
    )
    code, out = _cli(capsys, "listing", "set", other, "--state", "已上架", "--by", BY)
    assert code == 2 and "不是选中的款" in out["detail"]
    code, out = _cli(
        capsys,
        "listing",
        "set",
        first,
        "--state",
        "已上架",
        "--date",
        "10-25",
        "--by",
        BY,
    )
    assert code == 2 and "2026-10-25" in out["detail"]


def test_table_poll_reads_when_due(home, fake, seed_image, capsys) -> None:
    _enable(home)
    ctx, picked = _setup(home, seed_image, approve=1)
    feishu_table.sync(ctx)
    assert listing.due(ctx)
    code, out = _cli(capsys, "table", "poll")
    assert code == 0 and out["listing"]["rows"] == 1
    # 没到 listing.read_minutes：不读
    code, out = _cli(capsys, "table", "poll")
    assert "listing" not in out
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn, listing.READ_META_KEY, iso(utcnow() - dt.timedelta(minutes=11))
        )
    assert listing.due(ctx)
    # 读失败只记原因，下一轮再读
    fake.bitable_fail.add(("GET", "/records"))
    code, out = _cli(capsys, "table", "poll")
    assert code == 0 and "error" in out["listing"]
    assert not listing.due(ctx)
