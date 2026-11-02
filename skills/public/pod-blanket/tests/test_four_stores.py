"""1店 / 2店接入这条流水线（2026-11-02）：本机 temu.stores 配四家店（s1 / s2 排在前面，货号 FLRP / JUHP），
mooya 租户 ai_temu_deerflow 里同 key、同店名的店铺行。

- 已有的候选表补上两列，按配置顺序 1店 → 4店重摆列和画册卡片；周表格里的「候选表模板」也补上。
- 勾 1店：在 ai_temu_deerflow 按 FLRP 续号，交套图用配置里第一家店的货号起名，上架单交给 mooya 的 s1。
- 「已上架的款」：这里上的 1店 / 2店款（deerflow 客户端）进「已上架的款」，有 1店 / 2店的选项和视图；精作款（ai_temu
  客户端）照旧按客户端进「精作款」，精作款迁移不删本机店铺的视图。
- doctor：mooya 那边的店名和本机不一样要报出来（「已上架的款」的店铺列写的是 mooya 的店名）。
"""

from __future__ import annotations

import pytest

from conftest import reopen, set_config
from podflow import (
    books,
    candidate_stores,
    candidates,
    crafted,
    feishu,
    feishu_table,
    listed_table,
    mooya_listing,
    scenes,
    store,
    stores,
    temu,
)
from podflow.worker import Worker
from test_auto_listing import _auto_home, _pending_asset, _tick
from test_crafted import _codes
from test_crafted import _home as _crafted_home
from test_mooya_listing import S1, S2, S3, S4, _created_listings
from test_pipeline import bootstrap
from test_temu import (  # noqa: F401 - spawned 是夹具
    BRAND_ID,
    COL3,
    COL4,
    STORES,
    _candidates,
    _record,
    _selected_record,
    _temu_home,
    spawned,
)
from test_weekly_books import _template_with_fit, _weekly

pytestmark = pytest.mark.usefixtures("spawned")

COL1 = S1["column"]
COL2 = S2["column"]
NEW_STORES = [
    {"key": "s1", "label": "1店", "name": "毛毯pod", "prefix": "FLRP", "shop_id": "17356855", "template_detail_id": "8003"},
    {"key": "s2", "label": "2店", "name": "毛毯POD2店", "prefix": "JUHP", "shop_id": "18580271", "template_detail_id": "8004"},
]
FOUR = [*NEW_STORES, *STORES]
# mooya 租户 ai_temu_deerflow 里的 1店 / 2店：和精作款（ai_temu）同 key、同店名，前缀不同，交付工作台不出按钮
D1 = {**S1, "prefix": "FLRP", "ui": False}
D2 = {**S2, "prefix": "JUHP", "ui": False}


def _with_brand(items: list[dict]) -> list[dict]:
    return [{**s, "brand": "MIANDUDU", "brand_id": BRAND_ID} for s in items]


def test_new_stores_join_existing_candidate_tables_in_config_order(home, fake, seed_image) -> None:
    set_config(home, "table.views", True)
    ctx = _candidates(home, fake, seed_image)
    table_id = ctx.db.scalar("SELECT table_id FROM candidate_tables WHERE seed_id = 'S01'")
    assert candidate_stores.present(ctx, table_id) == [COL3, COL4]
    # 配置里在 3店 / 4店前面加上 1店 / 2店：这张表要补两列、重摆
    set_config(home, "temu.stores", FOUR)
    ctx = reopen(home)
    assert [p["table_id"] for p in candidate_stores.pending_tables(ctx)] == [table_id]
    candidates.sync(ctx)
    assert candidate_stores.present(ctx, table_id) == [COL1, COL2, COL3, COL4]
    assert candidate_stores.pending_tables(ctx) == []
    assert not candidate_stores.layout_pending(ctx, table_id)
    names = [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
    assert {COL1, COL2} <= set(names)
    # 列是加在最后的，默认视图把四家店按配置顺序摆到缩略图后面，「选中」藏着
    assert fake.visible_names(table_id, "全部")[:7] == [
        "素材",
        "缩略图",
        COL1,
        COL2,
        COL3,
        COL4,
        candidate_stores.CODE_FIELD,
    ]
    assert candidates.CHECK_FIELD not in fake.visible_names(table_id, "全部")
    for board in ("全部画册", "已选中", "待选"):
        assert fake.visible_names(table_id, board)[1:5] == [COL1, COL2, COL3, COL4]


def _four_auto(home, fake, seed_image):
    """四家店都开全自动、交 mooya 上架；mooya 的 ai_temu_deerflow 也配了四家店。"""
    _auto_home(home, fake, stores_on=("s1", "s2", "s3", "s4"))
    set_config(home, "temu.stores", _with_brand(FOUR))
    set_config(home, "temu.engine", "mooya")
    fake.temu_stores["ai_temu_deerflow"] = [D1, D2, S3, S4]
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)
    candidates.sync(ctx)
    return ctx


def test_tick_new_store_reserves_its_own_prefix_and_lists_through_mooya(home, fake, seed_image) -> None:
    ctx = _four_auto(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    out = _tick(ctx, fake, _record(ctx, asset_id), COL1, COL3)
    assert out["stores"]["selected_rows"] == [asset_id]
    # 每家店在 ai_temu_deerflow 按自己的前缀续号：1店 FLRP、3店 Deer
    rows = stores.rows(ctx, asset_ids=[asset_id])
    assert [(r["store_key"], r["code"]) for r in rows] == [("s1", "FLRP0001"), ("s3", "Deer0001")]
    assert set(fake.mooya_code_last) == {("ai_temu_deerflow", "FLRP"), ("ai_temu_deerflow", "DEER")}
    assert stores.codes_text(ctx, asset_id) == "FLRP0001 / Deer0001"
    # 交 mooya 出套图：用配置里第一家店（1店）的货号起名
    scenes.sync(ctx)
    jobs = [j for j in fake.mooya_jobs.values() if j["meta"].get("client_ref", "").startswith(("FLRP", "Deer"))]
    assert [j["meta"]["client_ref"] for j in jobs] == ["FLRP0001"]
    feishu_table.sync(ctx)
    temu.sync(ctx)
    # 程序确认后先交配置里的第一家（1店），店铺 s1、货号 FLRP0001；3店等它的标题
    assert [(b["store"], b["code"]) for b in _created_listings(fake)] == [("s1", "FLRP0001")]
    feishu_table.sync(ctx)
    selected = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert set(selected["店铺"]) == {COL1, COL3}
    assert selected["货号"] == "FLRP0001 / Deer0001"


def test_listed_table_takes_new_stores_while_crafted_stays_by_client(home, fake) -> None:
    _crafted_home(home, fake)
    set_config(home, "temu.stores", FOUR)
    fake.temu_stores["ai_temu_deerflow"] = [D1, D2, S3, S4]
    ctx = reopen(home)
    fake.temu_add_listed("ai_temu", "FLR1103", {**S1}, listedAt="2026-10-30T02:57:00Z")
    fake.temu_add_listed("ai_temu_deerflow", "FLRP0001", {**D1}, listedAt="2026-10-30T06:00:00Z")
    fake.temu_add_listed("ai_temu_deerflow", "Deer0006", {**S3}, listedAt="2026-10-30T05:50:00Z", source="deerflow")
    listed_table.pull(ctx)
    # 「已上架的款」：本机四家店（1店 / 2店也回来了）；精作款仍只有精作款客户端的两家
    assert listed_table.local_columns(ctx) == [COL1, COL2, COL3, COL4]
    assert listed_table.store_columns(ctx) == [COL1, COL2, COL3, COL4]
    assert crafted.store_columns(ctx) == [COL1, COL2]
    # 同一家店分两边：按 mooya 客户端分，不按店铺
    rows = {r["code"]: r for r in listed_table.rows(ctx)}
    assert crafted.is_crafted(ctx, rows["FLR1103"]) and not crafted.is_crafted(ctx, rows["FLRP0001"])
    assert listed_table.place(ctx, rows["FLRP0001"]) == "2026-W44"
    feishu_table.sync(ctx)
    weekly = listed_table._meta(ctx, "2026-W44")
    written = _codes(fake, weekly["table_id"])
    # 精作款模板还没锁：精作款哪里都不写；这里上的 1店款和 3店款进 W44「已上架的款」
    assert set(written) == {"FLRP0001", "Deer0006"}
    assert written["FLRP0001"]["店铺"] == COL1
    assert set((weekly.get("views") or {}).get("stores") or {}) == {COL1, COL2, COL3, COL4}
    # 以后再跑精作款迁移，也不会删「已上架的款」里本机店铺的视图
    assert crafted._store_views(ctx) == []
    # 状态按「客户端/店铺」分开数
    assert listed_table.status(ctx)["by_store"] == {"ai_temu/s1": 1, "deerflow/s1": 1, "deerflow/s3": 1}


def test_weekly_candidate_templates_get_new_store_columns(home, fake, seed_image) -> None:
    ctx, _picked = _weekly(home, seed_image, **{"temu": {"enabled": True, "stores": STORES}})
    _template_with_fit(ctx, fake)
    feishu_table.sync(ctx)
    book = books.book(ctx, "2026-W44")
    assert book["templates"]["candidate"]["stores"] == [COL3, COL4, candidate_stores.CODE_FIELD]
    assert books._candidate_templates(ctx) == []
    # 店铺配置在前面加了 1店 / 2店：模板表格和这本周表格里的「候选表模板」都要补
    set_config(home, "temu.stores", FOUR)
    ctx = reopen(home)
    assert [label for label, _info, _meta in books._candidate_templates(ctx)] == ["template", "2026-W44"]
    assert books.has_work(ctx)
    errors: list[dict] = []
    with feishu.Client(ctx) as api:
        out = books.prepare(ctx, api, errors)
    assert errors == [] and out["candidate_templates"] == ["template", "2026-W44"]
    for found in (books.template(ctx)["tables"]["candidate"], books.book(ctx, "2026-W44")["templates"]["candidate"]):
        assert found["stores"] == [COL1, COL2, COL3, COL4, candidate_stores.CODE_FIELD]
        names = [f["field_name"] for f in fake.bitable_tables[found["table_id"]]["fields"]]
        assert {COL1, COL2} <= set(names)
        assert fake.visible_names(found["table_id"], "全部")[:7] == [
            "素材",
            "缩略图",
            COL1,
            COL2,
            COL3,
            COL4,
            candidate_stores.CODE_FIELD,
        ]
        assert fake.visible_names(found["table_id"], "待选")[1:5] == [COL1, COL2, COL3, COL4]
    assert books._candidate_templates(ctx) == []


def test_doctor_flags_store_name_mismatch(home, fake) -> None:
    _temu_home(home, fake)
    set_config(home, "temu.stores", FOUR)
    set_config(home, "temu.engine", "mooya")
    fake.temu_stores["ai_temu_deerflow"] = [{**D1, "column": "1店 毛毯旧名"}, D2, S3, S4]
    ctx = reopen(home)
    check = mooya_listing.doctor_checks(ctx)[0]
    assert check["level"] == "warn" and "s1 的店名两边不一样" in check["detail"]
    fake.temu_stores["ai_temu_deerflow"] = [D1, D2, S3, S4]
    check = mooya_listing.doctor_checks(ctx)[0]
    assert check["level"] == "ok", check
