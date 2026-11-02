"""候选表模板（table.template）：新候选表从「模板」文件夹里的模板表复制结构，只能在飞书里设的视图设置
（「看图」的封面效果，假飞书里记作视图上的 cover_fit）跟着走；模板跟着代码的版本升级，被删了重建，复制不成照原样新建。"""

from __future__ import annotations

import re

import pytest

from conftest import reopen, set_config
from podflow import (
    candidate_stores,
    candidates,
    doctor,
    feishu,
    feishu_table,
    table_template,
)
from podflow.util import loads
from test_candidate_views import (
    _all_gallery_filter,
    _assert_layout,
    _assert_sorted,
    _pending_filter,
    _picked_filter,
    _views,
)
from test_candidates import _candidate_table, _cli
from test_scenes import _setup, spawned  # noqa: F401 - 夹具，下面 usefixtures 用
from test_temu import STORES

# 假拉起后台进程（表格同步、套图）：只要它的副作用，不看拉起了什么
pytestmark = pytest.mark.usefixtures("spawned")


def _on(home, seed_image, *, scenes_on: bool = True):
    """出一批图、选中一张，打开默认视图和模板；还没同步过候选表。"""
    ctx, _picked = _setup(home, seed_image, approve=1, enabled=scenes_on)
    set_config(home, "table.views", True)
    set_config(home, "table.template", True)
    return reopen(home)


def _ensure(ctx) -> dict:
    """先只备好模板（选款表也一起建好），不同步候选表。"""
    errors: list[dict] = []
    with feishu.Client(ctx) as api:
        info, _ = feishu_table.ensure_base(ctx, api)
        out = table_template.ensure(ctx, api, info, errors)
    assert errors == []
    return out


def _template(ctx) -> str:
    return table_template.current(ctx)["table_id"]


def _gallery(fake, table_id: str, view: str = "待选") -> dict:
    return _views(fake, table_id)[view]


def _set_fit(fake, table_id: str) -> None:
    """人在飞书里把这张表三个画册的封面效果改成适应（接口读写不到）。"""
    for view in ("全部画册", "已选中", "待选"):
        _gallery(fake, table_id, view)["cover_fit"] = "fit"


def _records(fake, table_id: str) -> list[str]:
    return [r for r, t in fake.bitable_record_tables.items() if t == table_id]


def test_new_table_is_copied_from_the_template(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    out = _ensure(ctx)
    template = _template(ctx)
    assert out == {"created": template}
    # 模板照新建候选表的样子建：三个默认视图、列和卡片都摆好，放在「模板」文件夹里，没有记录
    table = fake.bitable_tables[template]
    assert table["name"] == table_template.TEMPLATE_NAME
    folder = fake.bitable_parents[template]
    assert fake.bitable_folders[folder]["name"] == table_template.FOLDER_NAME
    assert list(_views(fake, template)) == ["全部", "全部画册", "已选中", "待选"]
    _assert_layout(fake, template)
    assert _records(fake, template) == []
    _set_fit(fake, template)
    # 同步候选表：S01 的表从模板复制，封面效果跟着走（复制后再写的筛选、排序、列都不会冲掉它），挪回根目录
    out = candidates.sync(ctx)
    assert "template" not in out and out["tables_created"] == 1
    table_id, table = _candidate_table(fake)
    assert table["copied_from"] == template
    for view in ("全部画册", "已选中", "待选"):
        assert _gallery(fake, table_id, view)["cover_fit"] == "fit"
    assert fake.bitable_parents[table_id] is None
    views = _views(fake, table_id)
    assert views["待选"]["property"]["filter_info"] == _pending_filter(fake, table_id)
    assert views["已选中"]["property"]["filter_info"] == _picked_filter(fake, table_id)
    assert views["全部画册"]["property"]["filter_info"] == _all_gallery_filter(fake, table_id)
    _assert_sorted(fake, table_id)
    _assert_layout(fake, table_id)
    assert candidates.view_ids(ctx, table_id) == {
        "all": views["全部"]["view_id"],
        "all_gallery": views["全部画册"]["view_id"],
        "picked": views["已选中"]["view_id"],
        "pending": views["待选"]["view_id"],
    }
    # 行照常写进新表，模板里还是空的；列都已跟上，不会再走旧表补列
    assert _records(fake, table_id) and _records(fake, template) == []
    assert candidates.package_field_ready(ctx, table_id)
    assert candidates._stale_fields(ctx) == []
    event = ctx.db.q1(
        "SELECT payload_json FROM events WHERE event = 'candidate_table_created'"
    )
    assert loads(event["payload_json"])["from_template"] is True
    status = table_template.status(ctx)
    assert status["ready"] and status["copies"] == 1
    assert status["url"].endswith(
        f"table={template}&view={_gallery(fake, template)['view_id']}"
    )
    # 模板没事做：不再建，也不改
    assert not table_template.has_work(ctx)
    assert _ensure(ctx) == {}


def test_template_catches_up_with_new_versions(home, fake, seed_image) -> None:
    # 套图关着时建的模板没有「套图交付包」列
    ctx = _on(home, seed_image, scenes_on=False)
    _ensure(ctx)
    template = _template(ctx)
    names = [f["field_name"] for f in fake.bitable_tables[template]["fields"]]
    assert candidates.PACKAGE_FIELD not in names
    # 装成更早一版：最后一列「排序」还没有，默认视图也是旧的
    fake.bitable_tables[template]["fields"].pop()
    meta = table_template._meta(ctx)
    meta.update(fields_version=candidates.FIELDS_VERSION - 1, views_version=1)
    table_template._save(ctx, meta)
    set_config(home, "scenes.enabled", True)
    ctx = reopen(home)
    found = table_template.current(ctx)
    assert table_template.behind(ctx, found) == ["fields", "package", "views"]
    # 没跟上之前不复制，新表照原样新建
    assert not table_template.ready(ctx)
    assert table_template.has_work(ctx)
    # 同步开头先把模板改好，再从它复制
    out = candidates.sync(ctx)
    assert out["template"] == {"upgraded": ["fields", "package", "views"]}
    names = [f["field_name"] for f in fake.bitable_tables[template]["fields"]]
    assert candidates.ORDER_FIELD in names and candidates.PACKAGE_FIELD in names
    assert table_template.ready(ctx)
    _table_id, table = _candidate_table(fake)
    assert table["copied_from"] == template


def test_template_gets_new_store_columns(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    template = _template(ctx)
    assert table_template.current(ctx)["stores"] == []
    # 打开 Temu 上架、配了店铺：模板先补上店铺列和「货号」，再从它复制
    set_config(home, "temu", {"enabled": True, "stores": STORES})
    ctx = reopen(home)
    stores = list(candidate_stores.column_map(ctx))
    assert stores
    assert table_template.behind(ctx, table_template.current(ctx)) == ["stores"]
    out = candidates.sync(ctx)
    assert out["template"] == {"upgraded": ["stores"]}
    names = [f["field_name"] for f in fake.bitable_tables[template]["fields"]]
    assert {*stores, candidate_stores.CODE_FIELD} <= set(names)
    table_id, table = _candidate_table(fake)
    assert table["copied_from"] == template
    # 复制来的表记着有店铺列，默认视图按它摆：店铺列和「货号」在缩略图后面，「选中」藏起来
    assert candidate_stores.present(ctx, table_id) == stores
    assert candidate_stores.pending_tables(ctx) == []
    assert not candidate_stores.layout_pending(ctx, table_id)
    visible = fake.visible_names(table_id, "全部")
    assert visible[: len(stores) + 3] == [
        "素材",
        "缩略图",
        *stores,
        candidate_stores.CODE_FIELD,
    ]
    assert candidates.CHECK_FIELD not in visible
    # 画册卡片：店铺勾换掉「选中」；「已选中」「全部画册」再放货号，「全部画册」还有选款结果
    rest = [n for n in candidates.GALLERY_FIELDS if n not in ("素材", candidates.CHECK_FIELD)]
    assert fake.visible_names(table_id, "已选中") == [
        "素材",
        *stores,
        candidate_stores.CODE_FIELD,
        *rest,
    ]
    assert fake.visible_names(table_id, "待选") == ["素材", *stores, *rest]
    assert fake.visible_names(table_id, "全部画册") == [
        "素材",
        *stores,
        candidate_stores.CODE_FIELD,
        candidates.STATE_FIELD,
        *rest,
    ]


def test_template_that_cannot_catch_up_is_not_copied(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    template = _template(ctx)
    meta = table_template._meta(ctx)
    meta["fields_version"] = candidates.FIELDS_VERSION - 1
    table_template._save(ctx, meta)
    # 改模板的列失败了：记下原因、不挡同步，这张新表照原样新建（不从旧模板复制）
    fake.bitable_fail.add(("PUT", "/fields/"))
    out = candidates.sync(ctx)
    assert "fake failure" in out["template"]["error"]
    assert out["tables_created"] == 1
    _table_id, table = _candidate_table(fake)
    assert "copied_from" not in table
    status = table_template.status(ctx)
    assert status["behind"] == ["fields"] and status["failed_at"]
    # 失败后一小时内不再试；--retry 马上试
    assert not table_template.has_work(ctx)
    fake.bitable_fail.clear()
    candidates.sync(ctx, retry=True)
    assert table_template.ready(ctx) and _template(ctx) == template


def test_deleted_template_falls_back_and_is_rebuilt(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    template = _template(ctx)
    # 人把模板表删了
    fake.bitable_tables.pop(template)
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    # 这一张照原样新建，模板记录作废
    assert "copied_from" not in table
    assert table_template.current(ctx) is None
    status = table_template.status(ctx)
    assert not status["ready"] and "800030104" in status["last_fallback"]["reason"]
    assert ctx.db.scalar(
        "SELECT COUNT(*) FROM events WHERE event = 'candidate_template_lost'"
    )
    # 下一次同步重建模板（只剩这件事要做也会同步），记下重建时间，doctor 提醒再设一次封面效果
    assert candidates.has_work(ctx)
    out = candidates.sync(ctx)
    rebuilt = _template(ctx)
    assert out["template"] == {"created": rebuilt} and rebuilt != template
    status = table_template.status(ctx)
    assert status["ready"] and status["recreated_at"]
    (check,) = doctor._template_checks(ctx)
    assert check["ok"] and "重建过" in check["detail"]


def test_copy_error_falls_back_but_keeps_the_template(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    template = _template(ctx)
    fake.bitable_fail.add(("POST", "/copy"))
    candidates.sync(ctx)
    _table_id, table = _candidate_table(fake)
    assert "copied_from" not in table
    # 模板还在，下一张新表照样先试复制；doctor 写着这次退回新建的原因
    assert _template(ctx) == template and table_template.ready(ctx)
    (check,) = doctor._template_checks(ctx)
    assert not check["ok"] and "fake failure" in check["detail"]


def test_taken_name_copies_with_a_time_suffix(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    template = _template(ctx)
    # 同名的表已经有了（比如库重建过）
    name = candidates.table_name("S01", candidates.seed_name(ctx, "S01"))
    app = feishu_table.base(ctx)["app_token"]
    fake.bitable_tables["tblTaken"] = {"name": name, "fields": [], "views": []}
    fake.bitable_table_apps["tblTaken"] = app
    candidates.sync(ctx)
    copied = [
        t for t in fake.bitable_tables.values() if t.get("copied_from") == template
    ]
    assert len(copied) == 1
    assert re.fullmatch(re.escape(name) + r" \d{8}", copied[0]["name"])


def test_copy_left_in_the_folder_is_moved_back_later(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    template = _template(ctx)
    folder = fake.bitable_parents[template]
    fake.bitable_fail.add(("POST", "/move"))
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    # 复制成了、只是没挪出「模板」文件夹：表照常用，下一次同步再挪
    assert table["copied_from"] == template
    assert fake.bitable_parents[table_id] == folder
    assert table_template.status(ctx)["unmoved"] == [table_id]
    assert table_template.has_work(ctx)
    fake.bitable_fail.clear()
    assert candidates.sync(ctx)["template"] == {"moved": [table_id]}
    assert fake.bitable_parents[table_id] is None
    assert "unmoved" not in table_template.status(ctx)


def test_new_base_gets_its_own_template(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    _ensure(ctx)
    old = _template(ctx)
    old_app = feishu_table.base(ctx)["app_token"]
    # table init --reset：另建一本选款表，旧表格里的模板不再用
    feishu_table.init(ctx, reset=True)
    new_app = feishu_table.base(ctx)["app_token"]
    assert new_app != old_app
    assert table_template.current(ctx) is None
    assert table_template.has_work(ctx)
    candidates.sync(ctx)
    new = _template(ctx)
    assert new != old and fake.bitable_table_apps[new] == new_app
    # 新表格里的候选表从新模板复制
    (table,) = [
        t
        for tid, t in fake.bitable_tables.items()
        if fake.bitable_table_apps.get(tid) == new_app
        and t["name"].endswith(candidates.TABLE_SUFFIX)
    ]
    assert table["copied_from"] == new


def test_template_off_creates_tables_as_before(home, fake, seed_image) -> None:
    ctx = _on(home, seed_image)
    set_config(home, "table.template", False)
    ctx = reopen(home)
    assert not table_template.has_work(ctx)
    candidates.sync(ctx)
    _table_id, table = _candidate_table(fake)
    assert "copied_from" not in table
    assert table_template.current(ctx) is None
    assert doctor._template_checks(ctx) == []


def test_galleries_lists_every_gallery_link(home, fake, seed_image, capsys) -> None:
    ctx = _on(home, seed_image)
    # 「选中的款」的视图要等上架状态那一列建好才建（和线上一样打开）
    set_config(home, "listing.enabled", True)
    ctx = reopen(home)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    template = _template(ctx)
    table_id, _table = _candidate_table(fake)
    code, out = _cli(capsys, "table", "galleries")
    assert code == 0 and out["missing"] == []
    kinds = {(g["kind"], g.get("view")): g for g in out["galleries"]}
    # 候选表模板和候选表各三个画册（「全部画册」「已选中」「待选」），「选中的款」一个「看图」
    assert set(kinds) == {
        ("template", "全部画册"),
        ("template", "已选中"),
        ("template", "待选"),
        ("selected", None),
        ("candidate", "全部画册"),
        ("candidate", "已选中"),
        ("candidate", "待选"),
    }
    for view in ("全部画册", "已选中", "待选"):
        assert kinds[("template", view)]["url"].endswith(
            f"table={template}&view={_gallery(fake, template, view)['view_id']}"
        )
    selected = feishu_table.base(ctx)
    assert kinds[("selected", None)]["url"].endswith(
        f"table={selected['table_id']}&view={selected['views']['gallery']}"
    )
    ids = candidates.view_ids(ctx, table_id)
    assert kinds[("candidate", "已选中")]["seed_id"] == "S01"
    assert kinds[("candidate", "全部画册")]["url"].endswith(f"table={table_id}&view={ids['all_gallery']}")
    assert kinds[("candidate", "已选中")]["url"].endswith(f"table={table_id}&view={ids['picked']}")
    assert kinds[("candidate", "待选")]["url"].endswith(f"table={table_id}&view={ids['pending']}")
    # 人在飞书里删掉的候选表：列在 missing，不给死链接
    fake.bitable_tables.pop(table_id)
    code, out = _cli(capsys, "table", "galleries")
    assert out["missing"] == [{"seed_id": "S01", "table_id": table_id}]
    assert "candidate" not in {g["kind"] for g in out["galleries"]}
    # table status 带着模板
    code, out = _cli(capsys, "table", "status")
    assert out["candidates"]["template"]["table_id"] == template
