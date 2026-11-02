"""候选表的默认视图（视图版本 6：表格「全部」、画册「全部画册」「已选中」「待选」；之前的几套就地升级、挪位置）、
列的先后与画册卡片、表名前缀（【已选N款】【选款完成】【有新图】）：table.views 打开时。"""

from __future__ import annotations

import copy
import json

import pytest

from conftest import reopen, set_config
from podflow import candidates, cli, feishu_table, ui, views as viewmod
from test_candidate_control import _accept_one, _ready, _second_round
from test_candidates import _age_selection, _candidate_table
from test_scenes import _setup, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

# 假拉起后台进程（表格同步、套图）：只要它的副作用，不看拉起了什么
pytestmark = pytest.mark.usefixtures("spawned")
BY = "feishu:ou_tester"


def _views(fake, table_id: str) -> dict[str, dict]:
    return {v["view_name"]: v for v in fake.bitable_views[table_id]}


def _field(fake, table_id: str, name: str) -> dict:
    fields = fake.bitable_tables[table_id]["fields"]
    index = [f["field_name"] for f in fields].index(name)
    return fake._field_view(index, fields[index])


def _is(field: dict, option: str) -> dict:
    """单选「是 option」：选项 id 按假飞书列字段时给的取（一个「是」只放一个 id）。"""
    found = next(o["id"] for o in field["property"]["options"] if o["name"] == option)
    return {
        "field_id": field["field_id"],
        "operator": "is",
        "value": json.dumps([found]),
    }


def _state_is(fake, table_id: str, option: str) -> dict:
    """「选款结果是 option」。"""
    field = _field(fake, table_id, candidates.STATE_FIELD)
    return {"conjunction": "and", "conditions": [_is(field, option)]}


def _pending_filter(fake, table_id: str) -> dict:
    """画册「待选」：选款结果是待选（按钮行、预留行、套图行、已选中的不放进来）。"""
    return _state_is(fake, table_id, candidates.STATE_PENDING)


def _picked_filter(fake, table_id: str) -> dict:
    """画册「已选中」：选款结果是已选中。"""
    return _state_is(fake, table_id, candidates.STATE_APPROVED)


def _all_gallery_filter(fake, table_id: str) -> dict:
    """画册「全部画册」：有图的原图行（待选 / 已选中 / 没选中 / 已修改 / 已清出，每个选项一个「是」用「或」连）。"""
    field = _field(fake, table_id, candidates.STATE_FIELD)
    return {
        "conjunction": "or",
        "conditions": [_is(field, option) for option in candidates.ALL_GALLERY_OPTIONS],
    }


BOARDS = ["全部", "全部画册", "已选中", "待选"]


def _v4_pending_filter(fake, table_id: str) -> dict:
    """视图版本 4 的表格「待选」：选款结果是待选、未生成或为空。"""
    field = _field(fake, table_id, candidates.STATE_FIELD)
    return {
        "conjunction": "or",
        "conditions": [
            _is(field, candidates.STATE_PENDING),
            _is(field, candidates.STATE_UNMADE),
            {"field_id": field["field_id"], "operator": "isEmpty"},
        ],
    }


def _assert_sorted(fake, table_id: str) -> None:
    """三个视图都按「排序」升序排；哪个视图里都看不到「排序」列（表格视图藏起来，画册卡片上没有）。"""
    for view in fake.bitable_views[table_id]:
        assert fake.bitable_view_sorts[(table_id, view["view_id"])] == [
            {"field": candidates.ORDER_FIELD, "desc": False}
        ]
        assert candidates.ORDER_FIELD not in fake.visible_names(
            table_id, view["view_name"]
        )


def _grid_order(fake, table_id: str) -> list[str]:
    """表格视图该有的列序：GRID_FRONT 里表中有的排最前，其余照表里的先后，「排序」藏起来。"""
    names = [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
    front = [n for n in candidates.GRID_FRONT if n in names]
    return front + [n for n in names if n not in front and n != candidates.ORDER_FIELD]


def _cover(fake, table_id: str, view: str = "已选中") -> str:
    """画册现在的封面（字段 id；没设过时飞书用第一个附件列）。"""
    gallery = _views(fake, table_id)[view]
    first = _field(fake, table_id, candidates.THUMB_FIELD)["field_id"]
    return gallery.get("cover", first)


def _assert_layout(fake, table_id: str) -> None:
    """表格「全部」开头是素材、缩略图、选中和三个下载链接；「已选中」「待选」卡片上是选中、改了什么、类型、出图方式，
    「全部画册」多一个选款结果；三个画册封面都是缩略图。"""
    assert fake.visible_names(table_id, "全部") == _grid_order(fake, table_id)
    thumb = _field(fake, table_id, candidates.THUMB_FIELD)["field_id"]
    for name in ("全部画册", "已选中", "待选"):
        cards = candidates.WHOLE_FIELDS if name == "全部画册" else candidates.GALLERY_FIELDS
        assert fake.visible_names(table_id, name) == list(cards)
        assert _cover(fake, table_id, name) == thumb


def _as_before_layout(ctx, fake, table_id: str) -> None:
    """装成 2026-10-27 前建好的默认视图（版本 2）：筛选、排序都在，列照建表时的先后（「排序」用 v1 藏起来），
    画册卡片是飞书缺省的前 4 列。"""
    meta = candidates._views_meta(ctx)
    meta["versions"][table_id] = 2
    candidates._save_views_meta(ctx, meta)
    order = _field(fake, table_id, candidates.ORDER_FIELD)
    for view in fake.bitable_views[table_id]:
        view.pop("visible", None)
        view.pop("cover", None)
        if view["view_type"] != "gallery":
            view["property"]["hidden_fields"] = [order["field_id"]]


def _as_v5(ctx, fake, table_id: str) -> dict[str, str]:
    """装成视图版本 5（10-30 18:07 到 18:3x）：表格「全部」+ 画册「已选中」「待选」，各设过一种封面效果；返回那时记下的
    三个视图 id。"""
    first, _whole, picked, pending = fake.bitable_views[table_id]
    picked["cover_fit"] = "fit-picked"
    pending["cover_fit"] = "fit-pending"
    fake.bitable_views[table_id] = [first, picked, pending]
    ids = {"all": first["view_id"], "picked": picked["view_id"], "pending": pending["view_id"]}
    meta = candidates._views_meta(ctx)
    meta["done"][table_id] = ids
    meta["versions"][table_id] = 5
    candidates._save_views_meta(ctx, meta)
    return ids


def _as_v4(ctx, fake, table_id: str) -> dict[str, str]:
    """装成视图版本 4（2026-10-30 前）：第一个表格「待选」（待选 / 未生成 / 为空）、画册「看图」（人设过封面效果）、
    最后的表格「全部」；返回那时记下的三个视图 id。"""
    first, picked, *_rest = fake.bitable_views[table_id]
    first["view_name"] = "待选"
    first.setdefault("property", {})["filter_info"] = _v4_pending_filter(fake, table_id)
    picked["view_name"] = "看图"
    picked["cover_fit"] = "fit"
    whole = {"view_id": "vewOldAll", "view_name": "全部", "view_type": "grid", "property": {}}
    fake.bitable_views[table_id] = [first, picked, whole]
    fake.bitable_view_sorts[(table_id, "vewOldAll")] = [
        {"field": candidates.ORDER_FIELD, "desc": False}
    ]
    ids = {"pending": first["view_id"], "all": "vewOldAll", "gallery": picked["view_id"]}
    meta = candidates._views_meta(ctx)
    meta["done"][table_id] = ids
    meta["versions"][table_id] = 4
    candidates._save_views_meta(ctx, meta)
    return ids


def _view_events(ctx, event: str) -> int:
    return int(ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = ?", (event,)))


def _expected_name(ctx, mark: str = "") -> str:
    return candidates.table_name("S01", candidates.seed_name(ctx, "S01"), mark)


def test_new_table_has_four_views(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    views = _views(fake, table_id)
    # 建表时默认视图就叫「全部」（不用改名、不筛选），接着依次建画册「全部画册」「已选中」「待选」；四个都按「排序」排
    assert table["default_view_name"] == "全部"
    assert list(views) == BOARDS
    assert [views[n]["view_type"] for n in views] == ["grid", "gallery", "gallery", "gallery"]
    assert fake.view_waits == []
    assert views["全部"]["property"]["filter_info"] is None
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    _assert_sorted(fake, table_id)
    # 建表时就摆好列：下载链接在前面，画册卡片上是勾选框、改了什么、类型、出图方式（「全部画册」多一个选款结果）
    _assert_layout(fake, table_id)
    assert fake.visible_names(table_id, "全部")[:6] == [
        "素材",
        "缩略图",
        "选中",
        "下载原图",
        "下载套图",
        "套图交付包",
    ]
    # 新进的行带着「排序」
    record_id = ctx.db.scalar(
        "SELECT record_id FROM candidate_rows WHERE seed_id = 'S01' ORDER BY asset_id LIMIT 1"
    )
    assert isinstance(fake.bitable_records[record_id][candidates.ORDER_FIELD], int)
    ids = candidates.view_ids(ctx, table_id)
    assert ids == {
        "all": views["全部"]["view_id"],
        "all_gallery": views["全部画册"]["view_id"],
        "picked": views["已选中"]["view_id"],
        "pending": views["待选"]["view_id"],
    }
    # 程序给的候选表链接直接打开画册「待选」
    link = candidates.table_link(ctx, "S01")
    assert link.endswith(f"?table={table_id}&view={ids['pending']}")
    (item,) = ui.tasks_view(ctx)["items"]
    assert {"label": "候选表", "url": link} in item["links"]
    status = candidates.status(ctx)
    assert status["views_pending"] == 0 and status["views_error"] is None
    # 卡片上逐张选过、还有待选：表名前面不加
    assert table["name"] == _expected_name(ctx)


def test_name_marks_follow_selection(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    # 表名跟着任务名字改：没起名的任务不加前缀，先起个名
    assert cli.main(["seed", "name", "S01", "--set", "森林三伙伴", "--by", BY]) == 0
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    # 选了 1 款、还没点选款完成 →【已选1款】
    assert table["name"] == _expected_name(ctx, candidates.mark_picked(1))
    assert table["name"].startswith("【已选1款】") and "森林三伙伴" in table["name"]
    # 一款都没选、也没点选款完成：不加前缀
    with ctx.db.tx() as conn:
        conn.execute("UPDATE assets SET human_decision = NULL WHERE asset_id = ?", (picked[0],))
    assert candidates.name_marks(ctx) == {"S01": ""}
    with ctx.db.tx() as conn:
        conn.execute("UPDATE assets SET human_decision = 'approved' WHERE asset_id = ?", (picked[0],))
    # 选款完成（没勾的算不要）→【选款完成】
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by=BY)
    candidates.sync(ctx)
    assert table["name"] == _expected_name(ctx, candidates.MARK_DONE)
    # 验收过、又有新图进选款 →【有新图】
    review_id = ctx.db.scalar(
        "SELECT review_id FROM reviews WHERE subject_type = 'asset_selection'"
        " AND status = 'decided' AND subject_id <> ? ORDER BY review_id LIMIT 1",
        (picked[0],),
    )
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE reviews SET status = 'open', decided_at = NULL, decided_by = NULL"
            " WHERE review_id = ?",
            (review_id,),
        )
    assert candidates.name_marks(ctx) == {"S01": candidates.MARK_NEW}
    candidates.sync(ctx)
    assert table["name"] == _expected_name(ctx, candidates.MARK_NEW)
    # 关掉 table.views：前缀去掉
    set_config(home, "table.views", False)
    ctx = reopen(home)
    candidates.sync(ctx)
    assert table["name"] == _expected_name(ctx)


def test_old_tables_get_views_on_next_sync(home, fake, seed_image) -> None:
    ctx, _ = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    table_id, _table = _candidate_table(fake)
    # 关着视图时建的表：默认视图「全部」+ 画册「看图」
    assert [v["view_name"] for v in fake.bitable_views[table_id]] == ["全部", "看图"]
    assert candidates.table_link(ctx, "S01").endswith(f"?table={table_id}")
    set_config(home, "table.views", True)
    ctx = reopen(home)
    assert candidates.has_work(ctx)
    assert candidates.status(ctx)["views_pending"] == 1

    # 改视图失败：记次数，满上限不再试，--retry 清零
    fake.bitable_fail.add(("PATCH", "/views/"))
    out = candidates.sync(ctx)
    assert "views" not in out and out["failed"] == 1
    assert candidates.status(ctx)["views_error"].startswith("S01：")
    for _ in range(feishu_table.MAX_ATTEMPTS - 1):
        candidates.sync(ctx)
    assert candidates.status(ctx)["views_pending"] == 0
    assert not candidates.has_work(ctx)
    fake.bitable_fail.clear()
    out = candidates.sync(ctx, retry=True)
    assert out["views"] == 1
    # 第一个视图本来就叫「全部」（不改名、去掉筛选），画册「看图」改名「全部画册」（不重建），再依次建「已选中」「待选」
    views = _views(fake, table_id)
    assert list(views) == BOARDS
    assert fake.view_waits == []
    assert views["全部"]["property"]["filter_info"] is None
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    _assert_sorted(fake, table_id)
    _assert_layout(fake, table_id)
    status = candidates.status(ctx)
    assert status["views_error"] is None and status["views_pending"] == 0
    # 只建一次
    assert "views" not in candidates.sync(ctx)


def test_views_from_before_sorting_are_redone_once(home, fake, seed_image) -> None:
    ctx, _ = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, _table = _candidate_table(fake)
    batch = _field(fake, table_id, "批次")
    # 2026-10-26 前建的默认视图（版本 1）：只筛「待选或为空」、没排序、列照建表时的先后；人在「全部」里藏过一列
    meta = candidates._views_meta(ctx)
    meta.pop("versions")
    candidates._save_views_meta(ctx, meta)
    fake.bitable_view_sorts.clear()
    views = _views(fake, table_id)
    for view in views.values():
        view.pop("visible", None)
        view["property"] = {
            "hidden_fields": [batch["field_id"]] if view["view_name"] == "全部" else []
        }
    assert candidates.status(ctx)["views_pending"] == 1
    out = candidates.sync(ctx)
    assert out["views"] == 1
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    _assert_sorted(fake, table_id)
    # 可见列是整份覆盖：人藏过的列照样藏着，其余照新的列序
    assert fake.visible_names(table_id, "全部") == [
        n for n in _grid_order(fake, table_id) if n != "批次"
    ]
    assert "views" not in candidates.sync(ctx)
    # 设排序撞上频率限制：等 2、5 秒再试，这一轮照样做完
    meta = candidates._views_meta(ctx)
    meta.pop("versions")
    candidates._save_views_meta(ctx, meta)
    fake.sort_limited = 2
    fake.view_waits.clear()
    assert candidates.sync(ctx)["views"] == 1
    assert fake.view_waits == [2.0, 5.0]
    # 「排序」列还没跟上的旧表先不改视图（筛选要用「未生成」选项）
    meta = candidates._views_meta(ctx)
    meta.pop("versions")
    candidates._save_views_meta(ctx, meta)
    fields_meta = candidates._fields_meta(ctx)
    fields_meta["versions"][table_id] = 3
    candidates._save_fields_meta(ctx, fields_meta)
    fake.bitable_fail.add(("PUT", "/fields/"))
    candidates.sync(ctx)
    assert not candidates._views_current(candidates._views_meta(ctx), table_id)


def test_view_name_still_taken_retries_next_sync(home, fake, seed_image) -> None:
    ctx, _ = _setup(home, seed_image, approve=1)
    candidates.sync(ctx)
    table_id, _table = _candidate_table(fake)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    # 「待选」这个名字一直没放开：等 1、2、4 秒都没建成，这一轮记一次失败（「看图」改名「全部画册」、建「已选中」已经做了）
    fake.bitable_view_reserved[(table_id, "待选")] = 9
    out = candidates.sync(ctx)
    assert "views" not in out and out["failed"] == 1
    assert fake.view_waits == [1.0, 2.0, 4.0]
    assert "ViewNameDuplicated" in candidates.status(ctx)["views_error"]
    assert list(_views(fake, table_id)) == ["全部", "全部画册", "已选中"]
    # 下一轮：已经叫「全部画册」「已选中」的照同样的分配不再改名（假飞书把改成同名当错误），只补「待选」
    fake.bitable_view_reserved.clear()
    out = candidates.sync(ctx)
    assert out["views"] == 1
    assert list(_views(fake, table_id)) == BOARDS
    assert candidates.status(ctx)["views_error"] is None


def test_sorted_tables_upgrade_without_resorting(home, fake, seed_image) -> None:
    ctx, _ = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    _as_before_layout(ctx, fake, table_id)
    names = [f["field_name"] for f in table["fields"]]
    assert fake.visible_names(table_id, "已选中") == names[:4]
    assert candidates.status(ctx)["views_pending"] == 1 and candidates.has_work(ctx)
    filters = {
        v["view_name"]: copy.deepcopy(v["property"].get("filter_info"))
        for v in fake.bitable_views[table_id]
    }
    calls = len(fake.bitable_calls)
    out = candidates.sync(ctx)
    assert out["views"] == 1
    _assert_layout(fake, table_id)
    _assert_sorted(fake, table_id)
    # 已经按「排序」排好的表（版本 2 起）：不重设排序（v3 排序接口有频率限制）、不建不删视图，四个视图各写一次可见列
    made = fake.bitable_calls[calls:]
    assert not [c for c in made if c[1].endswith("/sort")]
    assert not [c for c in made if c[0] in ("POST", "DELETE") and "/views" in c[1]]
    assert len([c for c in made if c[0] == "PUT" and "/visible_fields" in c[1]]) == 4
    assert {
        v["view_name"]: v["property"].get("filter_info")
        for v in fake.bitable_views[table_id]
    } == filters
    assert _view_events(ctx, "candidate_views_created") == 1
    assert candidates._views_current(candidates._views_meta(ctx), table_id)
    # 再同步：已经跟上了，什么都不写
    calls = len(fake.bitable_calls)
    assert "views" not in candidates.sync(ctx)
    assert not [c for c in fake.bitable_calls[calls:] if "/visible_fields" in c[1]]
    # 版本记录丢了要再摆一次：列和卡片已经是这个样子，只读不写
    meta = candidates._views_meta(ctx)
    meta["versions"][table_id] = 2
    candidates._save_views_meta(ctx, meta)
    calls = len(fake.bitable_calls)
    assert candidates.sync(ctx)["views"] == 1
    assert not [
        c for c in fake.bitable_calls[calls:] if c[0] == "PUT" and "/views/" in c[1]
    ]


def test_layout_keeps_hidden_and_added_columns(home, fake, seed_image) -> None:
    ctx, _ = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    _as_before_layout(ctx, fake, table_id)
    base = _grid_order(fake, table_id)
    # 人在表里加了两列（飞书加在最后）：一列文字，一列附件（拿它当了「已选中」的封面、也放到了卡片上）
    table["fields"] += [
        {"field_name": "我的备注", "type": 1},
        {"field_name": "参考", "type": 17},
    ]
    names = [f["field_name"] for f in table["fields"]]
    ids = {name: f"fld{i}" for i, name in enumerate(names)}
    views = _views(fake, table_id)
    # 「全部」里人把「我的备注」拖到「方向」后面、藏了「批次」和「参考」
    whole = [n for n in names if n not in (candidates.ORDER_FIELD, "批次", "参考")]
    whole.remove("我的备注")
    whole.insert(whole.index("方向") + 1, "我的备注")
    views["全部"]["visible"] = [ids[n] for n in whole]
    views["全部"]["property"]["hidden_fields"] = [
        ids[candidates.ORDER_FIELD],
        ids["批次"],
        ids["参考"],
    ]
    views["已选中"]["visible"] = [ids[n] for n in ("素材", "缩略图", "选中", "参考")]
    views["已选中"]["cover"] = ids["参考"]
    assert candidates.sync(ctx)["views"] == 1
    # 「全部」：人藏的照样藏着，拖到「方向」后面的跟着「方向」
    expected = [n for n in base if n != "批次"]
    expected.insert(expected.index("方向") + 1, "我的备注")
    assert fake.visible_names(table_id, "全部") == expected
    # 「已选中」卡片：定好的四个字段，人放上去的列留着；封面改回缩略图。「待选」卡片照定好的
    assert fake.visible_names(table_id, "已选中") == [
        *candidates.GALLERY_FIELDS,
        "参考",
    ]
    assert _cover(fake, table_id) == ids[candidates.THUMB_FIELD]
    assert fake.visible_names(table_id, "待选") == list(candidates.GALLERY_FIELDS)


def test_layout_retries_when_limited_and_counts_failures(
    home, fake, seed_image
) -> None:
    ctx, _ = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, _table = _candidate_table(fake)
    # 写可见列失败：记次数和原因，下一轮再摆
    _as_before_layout(ctx, fake, table_id)
    fake.bitable_fail.add(("PUT", "/visible_fields"))
    out = candidates.sync(ctx)
    assert "views" not in out and out["failed"] == 1
    status = candidates.status(ctx)
    assert status["views_pending"] == 1 and status["views_error"].startswith("S01：")
    assert candidates._views_meta(ctx)["attempts"][table_id] == 1
    fake.bitable_fail.clear()
    # 撞上频率限制：等 2、5 秒再写，这一轮照样摆完
    fake.visible_limited = 2
    fake.view_waits.clear()
    assert candidates.sync(ctx)["views"] == 1
    assert fake.view_waits == [2.0, 5.0]
    _assert_layout(fake, table_id)
    status = candidates.status(ctx)
    assert status["views_pending"] == 0 and status["views_error"] is None


def test_arrange_and_card_fields() -> None:
    layout = viewmod.Layout(
        front=("A", "C", "Z"),
        known=frozenset({"A", "B", "C", "D", "H"}),
        hidden=("H",),
        gallery=("A", "D", "Z"),
    )
    # 人加的 X 跟着左边的 B、Y 跟着 C；H 藏起来；D 本来就被藏着（不在可见列里）还藏着；表里没有的 Z 跳过
    arranged = viewmod.arrange(["A", "B", "X", "C", "Y", "H"], layout)
    assert arranged == ["A", "C", "Y", "B", "X"]
    # 已经是这个样子时原样返回（不会再写一次）
    assert viewmod.arrange(arranged, layout) == arranged
    # 卡片：定好的字段（表里有的，按定的先后）+ 人放上去的列；程序的其它列拿掉
    present = {"A", "B", "C", "D", "H", "X", "Y"}
    assert viewmod.card_fields(["A", "B", "X"], present, layout) == ["A", "D", "X"]
    # 另给一套卡片字段（候选表的「已选中」）：照它摆，人放上去的列照样留着
    assert viewmod.card_fields(["A", "X"], present, layout, ("A", "B", "C")) == ["A", "B", "C", "X"]


def test_galleries_stay_put_while_selecting(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, _ = _candidate_table(fake)
    views = _views(fake, table_id)
    views["已选中"]["cover_fit"] = views["待选"]["cover_fit"] = "fit"
    # 三个画册的筛选都是固定的：选款怎么走都不用改（2026-10-30 前「看图」跟着选款进度改筛选）
    _age_selection(ctx)
    candidates.accept(ctx, "S01", by="feishu:ou_tester")
    calls = len(fake.bitable_calls)
    out = candidates.sync(ctx)
    assert "views" not in out and "galleries" not in out
    assert not [c for c in fake.bitable_calls[calls:] if "/views" in c[1]]
    assert candidates.name_marks(ctx)["S01"] == candidates.MARK_DONE
    _second_round(ctx)
    candidates.sync(ctx)
    assert candidates.name_marks(ctx)["S01"] == candidates.MARK_NEW
    views = _views(fake, table_id)
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    assert views["已选中"]["cover_fit"] == views["待选"]["cover_fit"] == "fit"


def test_all_gallery_has_every_design_but_no_scene_rows(home, fake, seed_image) -> None:
    ctx = _ready(home, seed_image, scenes_on=True)
    _accept_one(ctx, fake)
    candidates.sync(ctx)
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    rows = [f for r, f in fake.bitable_records.items() if fake.bitable_record_tables.get(r) == table_id]
    allowed = set(candidates.ALL_GALLERY_OPTIONS)
    # 「全部画册」认的选款结果：有图的原图行（这里有待选、已选中、没选中）都在；表顶的按钮行、套图行的选款结果是空的，
    # 「未生成」「没出成」的空行也不在
    assert candidates.STATE_UNMADE not in allowed and candidates.STATE_NO_IMAGE not in allowed
    designs = {f.get(candidates.STATE_FIELD) for f in rows if f.get(candidates.STATE_FIELD) in allowed}
    assert {candidates.STATE_APPROVED, candidates.STATE_REJECTED} <= designs
    scene_rows = [f for f in rows if str(f.get("素材", "")).startswith(candidates.SCENE_ROW_PREFIX)]
    button_rows = [f for f in rows if str(f.get("素材", "")).startswith("▶")]
    assert scene_rows and button_rows
    assert all(f.get(candidates.STATE_FIELD) not in allowed for f in scene_rows + button_rows)


def test_v5_views_shift_into_place(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, _ = _candidate_table(fake)
    old = _as_v5(ctx, fake, table_id)
    assert candidates.status(ctx)["views_pending"] == 1
    fake.view_waits.clear()
    calls = len(fake.bitable_calls)
    assert candidates.sync(ctx)["views"] == 1
    views = _views(fake, table_id)
    # 接口不能调视图先后：之前的「已选中」改名「全部画册」、之前的「待选」改名「已选中」（封面效果跟着视图走），
    # 最后新建「待选」（这个名字刚改掉、飞书还占着：等一秒再建）
    assert list(views) == BOARDS
    assert views["全部画册"]["view_id"] == old["picked"] and views["全部画册"]["cover_fit"] == "fit-picked"
    assert views["已选中"]["view_id"] == old["pending"] and views["已选中"]["cover_fit"] == "fit-pending"
    assert "cover_fit" not in views["待选"]
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    assert fake.view_waits == [1.0]
    made = fake.bitable_calls[calls:]
    assert not [c for c in made if c[0] == "DELETE"]
    sorts = [c for c in made if c[1].endswith("/sort")]
    assert len(sorts) == 1 and views["待选"]["view_id"] in sorts[0][1]
    _assert_sorted(fake, table_id)
    _assert_layout(fake, table_id)
    ids = candidates.view_ids(ctx, table_id)
    assert ids == {
        "all": old["all"],
        "all_gallery": old["picked"],
        "picked": old["pending"],
        "pending": views["待选"]["view_id"],
    }
    assert candidates.table_link(ctx, "S01").endswith(f"view={ids['pending']}")
    assert "views" not in candidates.sync(ctx)


def test_galleries_dragged_out_of_order_are_renamed_by_position(
    home, fake, seed_image
) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, _ = _candidate_table(fake)
    # 人在飞书里把「待选」拖到了最前面：全部 / 待选 / 全部画册 / 已选中
    first, whole, picked_view, pending = fake.bitable_views[table_id]
    fake.bitable_views[table_id] = [first, pending, whole, picked_view]
    meta = candidates._views_meta(ctx)
    meta["versions"][table_id] = 5
    candidates._save_views_meta(ctx, meta)
    calls = len(fake.bitable_calls)
    assert candidates.sync(ctx)["views"] == 1
    # 名字按位置分：依次改不过去时先改成临时名字再改回来；不建不删
    assert [v["view_id"] for v in fake.bitable_views[table_id]] == [
        first["view_id"],
        pending["view_id"],
        whole["view_id"],
        picked_view["view_id"],
    ]
    assert list(_views(fake, table_id)) == BOARDS
    made = fake.bitable_calls[calls:]
    assert not [c for c in made if c[0] in ("POST", "DELETE") and "/views" in c[1]]
    views = _views(fake, table_id)
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)


def test_v4_views_go_straight_to_v6(home, fake, seed_image) -> None:
    ctx, picked = _setup(home, seed_image, approve=1)
    set_config(home, "table.views", True)
    ctx = reopen(home)
    candidates.sync(ctx)
    table_id, _ = _candidate_table(fake)
    old = _as_v4(ctx, fake, table_id)
    assert candidates.status(ctx)["views_pending"] == 1
    fake.view_waits.clear()
    calls = len(fake.bitable_calls)
    out = candidates.sync(ctx)
    assert out["views"] == 1
    views = _views(fake, table_id)
    # 删掉最后的「全部」，第一个表格「待选」改名「全部」、去掉筛选；「看图」改名「全部画册」（人设的封面效果留着）；
    # 再依次建「已选中」「待选」（「待选」这个名字刚改掉、飞书还占着：等一秒再建）
    assert list(views) == BOARDS
    assert views["全部"]["view_id"] == old["pending"] and views["全部"]["property"]["filter_info"] is None
    assert views["全部画册"]["view_id"] == old["gallery"] and views["全部画册"]["cover_fit"] == "fit"
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    assert "vewOldAll" not in {v["view_id"] for v in fake.bitable_views[table_id]}
    assert fake.view_waits == [1.0]
    # 之前那一套已经排好序：只给新建的「已选中」「待选」设排序
    sorts = [c for c in fake.bitable_calls[calls:] if c[1].endswith("/sort")]
    assert len(sorts) == 2
    _assert_sorted(fake, table_id)
    _assert_layout(fake, table_id)
    ids = candidates.view_ids(ctx, table_id)
    assert ids["all"] == old["pending"] and ids["all_gallery"] == old["gallery"]
    assert candidates.table_link(ctx, "S01").endswith(f"view={ids['pending']}")
    assert candidates._views_current(candidates._views_meta(ctx), table_id)
    assert "views" not in candidates.sync(ctx)
