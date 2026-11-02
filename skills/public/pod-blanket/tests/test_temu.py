"""Temu 半托上架：候选表店铺列与货号、上架图与标题、妙手建箱发布（假妙手 miaoshou.test）、「选中的款」上架几列、
「已上架的款」双向同步。"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import make_png, reopen, set_config
from podflow import (
    bg,
    candidate_stores,
    candidates,
    feishu_table,
    listed_table,
    mooya,
    review,
    scenes,
    store,
    stores,
    temu,
    temu_assets,
    temu_title,
)
from podflow.stages.common import StageError
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_storage import _enable_oss
from test_ui import _enable_feishu

PLAN_IDS = [
    "blanket_04_model_read_lap",
    "blanket_05_sofa_drape",
    "blanket_01_model_hold",
    "blanket_02_bed_flatlay",
    "blanket_03_outdoor_hang",
    "blanket_06_car_seat",
    "blanket_09_model_chest_relax",
    "blanket_08_use_scenario_flatlay",
    "blanket_07_wash_lifestyle",
]
STORES = [
    {
        "key": "s3",
        "label": "3店",
        "name": "CozWeave H",
        "prefix": "Deer",
        "shop_id": "v2_shop3",
        "template_detail_id": "8001",
    },
    {
        "key": "s4",
        "label": "4店",
        "name": "LoomNest J",
        "prefix": "Flow",
        "shop_id": "v2_shop4",
        "template_detail_id": "8002",
    },
]
COL3 = "3店 CozWeave H"
COL4 = "4店 LoomNest J"
SIZES = ["60*80in", "50*60in", "40*50in", "30*40in"]
BRAND_ID = "1000000793013"


def _stores(brand_id: str | None = None) -> list[dict]:
    """店铺配置；给了 brand_id 就两家店都配上 MIANDUDU。"""
    if not brand_id:
        return STORES
    return [{**s, "brand": "MIANDUDU", "brand_id": brand_id} for s in STORES]


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


def _template(shop_id: str, prices: list[str]) -> dict:
    """样板商品（站点模式详情）：10 张轮播，4 个尺码；第一个尺码的预览图用第 1 张，其余用第 3 张。"""
    urls = [f"https://img.miaoshou.test/{shop_id}/{n}.jpg" for n in range(1, 11)]
    return {
        "detailId": 1,
        "editModel": "site",
        "site": "PDDKJCHOICE",
        "cid": 11901,
        "title": "Template Flannel Blanket 280GSM",
        "multiLanguageTitleMap": {"en": "Template Flannel Blanket 280GSM"},
        "itemNum": "TPL0001",
        "imgUrls": urls,
        "multiLanguageImgUrlsMap": {"en": urls},
        "collectBoxDetailShopList": [
            {
                "shopId": shop_id,
                "shopName": "样板店",
                "itemNum": "TPL0001",
                "publishSiteList": ["US"],
                "freightTemplateId": "ft-1",
            }
        ],
        "publishSiteList": ["US"],
        "currency": "USD",
        "attributes": [{"attrName": "品牌名", "attrValue": "MIANDUDU"}],
        "saleAttributes": [
            {"name": "尺码", "values": [{"skuKey": f"10{i}", "name": s} for i, s in enumerate(SIZES)]}
        ],
        "skuMap": {
            f";10{i};": {"price": p, "stock": 999, "itemNum": "TPL0001", "imgUrl": urls[0] if i == 0 else urls[2]}
            for i, p in enumerate(prices)
        },
        "goodsLayerDecorationReqs": [],
    }


def _temu_home(
    home, fake, *, publish: str = "auto", control_row: bool = False, brand_id: str | None = None
) -> None:
    _enable_feishu(home)
    _enable_oss(home)
    with open(home / "secrets.env", "a", encoding="utf-8") as fh:
        fh.write(
            "MOOYA_SERVICE_TOKEN_DEERFLOW=test-mooya-deerflow\n"
            "MIAOSHOU_APP_KEY=ak_test\nMIAOSHOU_APP_SECRET=as_test_secret\n"
        )
    set_config(home, "table.enabled", True)
    set_config(home, "table.control_row", control_row)
    set_config(home, "listing.enabled", True)
    set_config(home, "scenes.enabled", True)
    set_config(home, "mooya.clients", {"deerflow": {"token_name": "MOOYA_SERVICE_TOKEN_DEERFLOW"}})
    set_config(home, "mooya.submit_client", "deerflow")
    set_config(home, "temu", {"enabled": True, "publish": publish, "stores": _stores(brand_id)})
    fake.mooya_delivery = True
    fake.mooya_scenes = 9
    fake.mooya_plan_ids = list(PLAN_IDS)
    fake.miaoshou_items[8001] = {
        "info": _template("v2_shop3", ["123.12", "96.51", "74.18", "63.13"]),
        "status": "notPublished",
        "published_by": None,
    }
    fake.miaoshou_items[8002] = {
        "info": _template("v2_shop4", ["299", "210", "190", "150"]),
        "status": "notPublished",
        "published_by": None,
    }


def _candidates(home, fake, seed_image, **kwargs):
    """出一批图进候选表（带店铺列）。"""
    _temu_home(home, fake, **kwargs)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)
    candidates.sync(ctx)
    return ctx


def _record(ctx, asset_id: str) -> str:
    return ctx.db.scalar("SELECT record_id FROM candidate_rows WHERE asset_id = ?", (asset_id,))


def _selected_record(ctx, asset_id: str) -> str:
    return ctx.db.scalar("SELECT record_id FROM table_rows WHERE asset_id = ?", (asset_id,))


def _ready(home, fake, seed_image, *, picks=(COL3,), **kwargs):
    """候选表里勾店铺列、选款完成 → 领货号、出套图 → 「选中的款」有这一行、准备好上架图和标题。"""
    ctx = _candidates(home, fake, seed_image, **kwargs)
    asset_id = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    for column in picks:
        fake.bitable_records[_record(ctx, asset_id)][column] = True
    accepted = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    prepared = temu.sync(ctx)
    return ctx, asset_id, accepted, prepared


def _tick_confirm(ctx, fake, asset_id: str, **cells):
    info = feishu_table.base(ctx)
    record = _selected_record(ctx, asset_id)
    fake.bitable_records[record].update({"确认上架": True, **cells})
    return candidates.control_event(
        ctx,
        file_token=info["app_token"],
        table_id=info["table_id"],
        record_ids=[record],
        by="feishu:ou_tester",
    )


def _created(fake) -> list[dict]:
    return [body["siteCollectItemInfo"] for tail, body in fake.miaoshou_calls if tail == "create_collect_box_item"]


# ---------------------------------------------------------------- 纯函数


def _original_write_member_as_jpg(data: bytes) -> bytes:
    """原 Windows 流程 wechat_flr.py 的 write_member_as_jpg（只把 zip 成员换成字节进、字节出）。"""
    image = Image.open(io.BytesIO(data))
    image.load()
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        if image.mode == "RGBA":
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95)
    return out.getvalue()


@pytest.mark.parametrize("mode", ["RGBA", "RGB", "P", "L", "LA"])
def test_listing_jpeg_is_byte_identical_to_old_pipeline(mode: str) -> None:
    base = Image.open(io.BytesIO(make_png(7, size=(96, 72)))).convert("RGBA")
    pixels = base.load()
    for x in range(0, 96, 5):
        pixels[x, 10] = (pixels[x, 10][0], pixels[x, 10][1], pixels[x, 10][2], 0)
    image = base.convert(mode) if mode != "RGBA" else base
    buf = io.BytesIO()
    image.save(buf, "PNG")
    data = buf.getvalue()
    assert temu_assets.to_listing_jpeg(data) == _original_write_member_as_jpg(data)


def test_listing_order_moves_size_chart_to_third() -> None:
    files = [{"image_id": f"scene-{i:02d}", "role": "scene", "plan_id": p} for i, p in enumerate(PLAN_IDS, 1)]
    files.append({"image_id": "file-9001", "role": "insert", "plan_id": None})
    assert temu_assets.slot_problems(files) == []
    order = temu_assets.listing_order(files)
    assert [f["plan_id"] for f in order] == [
        PLAN_IDS[0],
        PLAN_IDS[1],
        None,
        *PLAN_IDS[2:7],
        PLAN_IDS[7],
        PLAN_IDS[8],
    ]
    # 04 / 05 不在前两位、少一张、尺码图不在最后：都不自动上
    swapped = [files[2], *files[:2], *files[3:]]
    assert any("第 1–2 张" in p for p in temu_assets.slot_problems(swapped))
    assert "正好 10 张" in temu_assets.slot_problems(files[:9])[0]
    assert any("尺码图" in p for p in temu_assets.slot_problems([files[-1], *files[:-1]]))


def test_payload_clones_template_and_swaps_images_codes() -> None:
    store_ = stores.Store("s3", "3店", "CozWeave H", "Deer", "v2_shop3", "8001")
    tpl = _template("v2_shop3", ["1", "2", "3", "4"])
    tpl["collectBoxDetailShopList"].append({"shopId": "other", "itemNum": "X"})
    images = [f"https://oss.test/{n}.jpg" for n in range(1, 11)]
    payload = temu.build_payload(store_, tpl, code="Deer0007", title="T 280GSM", images=images)
    assert "detailId" not in payload and "editModel" not in payload and "site" not in payload
    assert payload["collectBoxDetailShopList"] == [
        {"shopId": "v2_shop3", "itemNum": "Deer0007", "publishSiteList": ["US"], "freightTemplateId": "ft-1"}
    ]
    assert payload["itemNum"] == "Deer0007" and payload["title"] == "T 280GSM"
    assert payload["multiLanguageTitleMap"] == {"en": "T 280GSM"}
    assert payload["imgUrls"] == images and payload["multiLanguageImgUrlsMap"] == {"en": images}
    skus = list(payload["skuMap"].values())
    assert {s["itemNum"] for s in skus} == {"Deer0007"}
    # 样板里第一个尺码用第 1 张、其余用第 3 张：换成这款同一位置的图
    assert [s["imgUrl"] for s in skus] == [images[0], images[2], images[2], images[2]]
    assert [s["price"] for s in skus] == ["1", "2", "3", "4"]
    assert payload["attributes"] == tpl["attributes"]
    assert tpl["skuMap"][";100;"]["itemNum"] == "TPL0001"
    assert temu.shop_sites(store_, tpl) == {"v2_shop3": ["US"]}
    assert [p["sku"] for p in temu.prices(tpl)] == SIZES


def test_store_config_errors_disable_quietly(home, fake) -> None:
    set_config(home, "temu", {"enabled": True, "stores": [{"key": "Bad Key", "label": "3店", "prefix": "Deer"}]})
    ctx = reopen(home)
    assert stores.enabled(ctx) is False and temu.enabled(ctx) is False
    assert stores.status(ctx)["error"]
    assert temu.has_work(ctx) is False


# ---------------------------------------------------------------- 候选表店铺列与货号


def test_candidate_store_columns_pick_stores_and_reserve_codes(home, fake, seed_image, spawned) -> None:
    ctx = _candidates(home, fake, seed_image, control_row=True)
    table_id = candidates.table_info(ctx, "S01")["table_id"]
    names = [f["field_name"] for f in fake.bitable_tables[table_id]["fields"]]
    at = names.index("选中")
    assert names[at + 1 : at + 4] == [COL3, COL4, "货号"]
    assert candidate_stores.present(ctx, table_id) == [COL3, COL4]
    candidates.sync(ctx)
    # 按钮行的说明改成勾任意一个勾选框（「选中」在视图里藏起来了）
    tops = [
        r for r in fake.bitable_records.values() if str(r.get("素材", "")).startswith("▶")
    ]
    assert tops and all("任意一个勾选框" in r["素材"] for r in tops)

    items = review.list_reviews(ctx, types=["asset_selection"])
    one, two = items[0]["subject_id"], items[1]["subject_id"]
    fake.bitable_records[_record(ctx, one)][COL3] = True
    fake.bitable_records[_record(ctx, two)][COL3] = True
    fake.bitable_records[_record(ctx, two)][COL4] = True
    fake.mooya_code_start["Deer"] = 41
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert {one, two} <= set(out["approved"])
    assert out["stores"]["chosen"] == {one: ["s3"], two: ["s3", "s4"]}
    codes = {(r["asset_id"], r["store_key"]): r["code"] for r in stores.rows(ctx)}
    assert codes == {(one, "s3"): "Deer0042", (two, "s3"): "Deer0043", (two, "s4"): "Flow0001"}
    # mooya 那边：在 ai_temu_deerflow 租户领的号，ref 带素材和店铺，同一个 ref 拿回同一个号
    assert all(t == "ai_temu_deerflow" for t, _ref in fake.mooya_codes)
    assert stores.allocate_codes(ctx)["reserved"] == []
    row = fake.bitable_records[_record(ctx, two)]
    assert row[COL3] is True and row[COL4] is True and row["货号"] == "Deer0043 / Flow0001"
    assert row["选中"] is True and row["选款结果"] == "已选中"
    # 没选中的行连店铺列一起取消
    rejected = [r for r in out["rejected"]]
    for asset_id in rejected:
        cells = fake.bitable_records[_record(ctx, asset_id)]
        assert not cells.get(COL3) and not cells.get(COL4)

    # 交套图：编号用第一家店的货号，用新租户的客户端
    scenes.sync(ctx)
    metas = {j["meta"]["client_ref"]: j for j in fake.mooya_jobs.values()}
    assert {"Deer0042", "Deer0043"} <= set(metas)
    assert {j["tenant"] for j in metas.values()} == {"ai_temu_deerflow"}
    sets = scenes.latest_sets(ctx, [one])
    assert scenes.client_of(ctx, sets[one]["set_id"]) == "deerflow"
    # 拉原图不贴字标
    assert {v for _job, _img, v in fake.mooya_variants} == {"raw"}


def test_control_row_ticked_in_store_column_counts_as_button(home, fake, seed_image, spawned) -> None:
    ctx = _candidates(home, fake, seed_image, control_row=True)
    candidates.sync(ctx)
    action = next(
        dict(r) for r in ctx.db.q("SELECT * FROM candidate_actions WHERE kind = 'accept'")
    )
    fake.bitable_records[action["record_id"]][COL4] = True
    info = feishu_table.base(ctx)
    out = candidates.control_event(
        ctx,
        file_token=info["app_token"],
        table_id=action["table_id"],
        record_ids=[action["record_id"]],
        by="feishu:ou_tester",
    )
    assert out.get("started")
    cells = fake.bitable_records[action["record_id"]]
    assert cells[COL4] is False and cells["选中"] is False


def test_old_sets_stay_on_legacy_client_after_switch(home, fake, seed_image, spawned) -> None:
    _temu_home(home, fake)
    # 先按老配置出一套：老客户端（platform_internal），不开上架
    set_config(home, "mooya.submit_client", "legacy")
    set_config(home, "temu.enabled", False)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    store.sync(ctx)
    asset_id = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    review.decide(ctx, review.list_reviews(ctx, types=["asset_selection"])[0]["review_id"], "approved", by="t")
    scenes.start(ctx, by="t", seed_id="S01")
    scenes.sync(ctx)
    old_set = scenes.latest_sets(ctx, [asset_id])[asset_id]
    assert old_set["status"] == "done" and scenes.client_of(ctx, old_set["set_id"]) == "legacy"
    assert fake.mooya_jobs[int(old_set["mooya_job_id"])]["tenant"] == "platform_internal"
    # 换到新租户、开上架：旧套图照样用老客户端重拉（假 mooya 按租户隔离任务），但不能自动上
    set_config(home, "mooya.submit_client", "deerflow")
    set_config(home, "temu.enabled", True)
    ctx = reopen(home)
    out = scenes.refresh(ctx, old_set["set_id"])
    assert not out.get("error")
    stores.choose(ctx, asset_id, ["s3"], by="t", source="chat")
    assert "旧租户" in temu_assets.readiness(ctx, asset_id)
    assert temu._prepare_targets(ctx) == []


# ---------------------------------------------------------------- 上架全流程


def test_listing_flow_auto_publish(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, accepted, prepared = _ready(home, fake, seed_image)
    code = "Deer0001"
    assert prepared["prepared"] == [asset_id]
    # 上架图：带字标的导出口径（新租户），10 张，尺码图挪到第 3 张
    item = temu_assets.current(ctx, asset_id)
    assert [f["n"] for f in item["files"]] == list(range(1, 11))
    assert [f["plan_id"] for f in item["files"]] == [
        PLAN_IDS[0],
        PLAN_IDS[1],
        None,
        *PLAN_IDS[2:7],
        PLAN_IDS[7],
        PLAN_IDS[8],
    ]
    assert item["files"][2]["role"] == "insert"
    exports = [(img, v) for _job, img, v in fake.mooya_variants if v == "export"]
    assert len(exports) == 10 and "file-9001" in {img for img, _ in exports}
    first = store.ensure_local(ctx, item["files"][0]["path"]).read_bytes()
    assert first[:3] == b"\xff\xd8\xff"
    assert all(f["key"] in fake.oss_objects for f in item["files"])
    assert all(f["url"].startswith("https://") for f in item["files"])
    # 标题：P15 写的，合规
    title = temu_title.get(ctx, asset_id)
    assert title["source"] == "auto" and temu_title.check_title(title["title"]) == []

    # 「选中的款」：四列 + 上架状态补了两个选项，程序写店铺、货号、标题、待上架
    info = feishu_table.base(ctx)
    assert temu.ready(info)
    fields = {f["field_name"]: f for f in fake.bitable_tables[info["table_id"]]["fields"]}
    assert {"店铺", "货号", "上架标题", "确认上架"} <= set(fields)
    assert [o["name"] for o in fields["上架状态"]["property"]["options"]] == [
        "待上架",
        "已上架",
        "不上了",
        "上架中",
        "上架失败",
    ]
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert row["店铺"] == [COL3] and row["货号"] == code
    assert row["上架标题"] == title["title"] and row["上架状态"] == "待上架"
    assert "勾「确认上架」" in row["上架备注"]
    assert "确认上架" not in row or row["确认上架"] is False

    # 人勾「确认上架」：排队、勾由程序记下（事件回声不再处理）；还没进妙手，上架状态仍是待上架
    out = _tick_confirm(ctx, fake, asset_id)
    assert out["queued"] == [asset_id]
    assert ["temu", "sync"] in [argv[2:4] for argv in spawned]
    assert fake.bitable_records[_selected_record(ctx, asset_id)]["上架状态"] == "待上架"
    again = _tick_confirm(ctx, fake, asset_id)
    assert "ignored" in again

    out = temu.sync(ctx)
    assert out["boxed"] == [code] and out["published"] == [code] and out["listed"] == [code]
    created = _created(fake)
    assert len(created) == 1
    payload = created[0]
    assert payload["itemNum"] == code and payload["title"] == title["title"]
    assert payload["imgUrls"] == temu_assets.urls(item)
    assert payload["collectBoxDetailShopList"][0]["shopId"] == "v2_shop3"
    assert {s["itemNum"] for s in payload["skuMap"].values()} == {code}
    publish = [body for tail, body in fake.miaoshou_calls if tail == "save_move_collect_task"]
    assert publish == [{"detailIds": [int(stores.rows(ctx)[0]["detail_id"])], "shopIdAndSitesMap": {"v2_shop3": ["US"]}}]

    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert row["上架状态"] == "已上架" and row["确认上架"] is True
    assert "已提交 Temu US" in row["上架备注"] and row["上架日期"]
    mark = ctx.db.q1("SELECT state FROM listing_marks WHERE asset_id = ?", (asset_id,))
    assert mark["state"] == "已上架"

    # 已上架的款：业务库 + 飞书表（同一个多维表格）
    listed = listed_table.rows(ctx)
    assert [r["code"] for r in listed] == [code]
    snap = listed[0]["snapshot"]
    assert snap["store"] == COL3 and snap["sites"] == ["US"]
    assert [p["sku"] for p in snap["prices"]] == SIZES and snap["prices"][0]["price"] == "123.12"
    assert snap["main_kind"] == "模特盖腿读书" and len(snap["images"]) == 10
    meta = listed_table._meta(ctx)
    table = fake.bitable_tables[meta["table_id"]]
    assert table["name"] == "已上架的款"
    record = fake.bitable_records[listed[0]["record_id"]]
    assert record["货号"] == code and record["店铺"] == COL3 and record["上架标题"] == title["title"]
    assert record["上架图包"]["text"] == f"{code}_上架图包.zip" and record["主图"]
    package = snap.get("package") or listed_table.rows(ctx)[0]["snapshot"]["package"]
    data = fake.oss_objects[package["key"]][0]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == sorted(f"{code}/{n}.jpg" for n in range(1, 11))
    assert listed_table.check(ctx)["ok"]
    # 没有要做的了
    assert temu.has_work(ctx) is False


def test_box_mode_waits_for_publish_in_miaoshou(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    out = temu.sync(ctx)
    assert out["boxed"] == ["Deer0001"] and not out["published"] and not out["listed"]
    row = stores.rows(ctx)[0]
    assert row["state"] == "in_box"
    selected = fake.bitable_records[_selected_record(ctx, asset_id)]
    # 进了妙手采集箱：上架中
    assert (
        "在妙手里检查后点发布" in selected["上架备注"]
        and selected["上架状态"] == "上架中"
    )
    assert not [t for t, _ in fake.miaoshou_calls if t == "save_move_collect_task"]
    # 没配品牌的店不碰店铺模式
    assert not [t for t, _ in fake.miaoshou_calls if t.endswith("shop_collect_item_info")]
    # 人在妙手里发布了：下一次查发布结果时标已上架
    fake.miaoshou_items[int(row["detail_id"])]["status"] = "published"
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, temu.CHECKED_META_KEY)
    out = temu.sync(ctx)
    assert out["listed"] == ["Deer0001"]
    assert stores.rows(ctx)[0]["state"] == "listed"


def test_listing_state_is_in_progress_only_inside_miaoshou() -> None:
    """上架状态（2026-10-30 用户定）：进了妙手（建箱起）才算上架中；等套图、准备上架图、待确认、排队都是待上架。"""

    def state(*states: str) -> str | None:
        return temu.aggregate([{"state": s} for s in states])

    assert state() is None
    assert set(temu.MIAOSHOU_STATES) == {
        "creating",
        "create_unknown",
        "in_box",
        "publishing",
        "publish_unknown",
    }
    for before in (temu.STATE_CHOSEN, temu.STATE_QUEUED):
        assert state(before) == "待上架"
    for inside in temu.MIAOSHOU_STATES:
        assert state(inside) == "上架中"
        assert state(temu.STATE_QUEUED, inside) == "上架中"
    # 一家已经上了、另一家还没进妙手：上架中；每家都上了：已上架
    assert state(temu.STATE_LISTED, temu.STATE_QUEUED) == "上架中"
    assert state(temu.STATE_LISTED, temu.STATE_LISTED) == "已上架"
    # 有一家没上成：上架失败（在「待上架」视图里）
    assert state(temu.STATE_FAILED, temu.STATE_BOX) == "上架失败"


def test_listing_help_is_rewritten_once_when_it_changes(
    home, fake, seed_image, spawned, monkeypatch
) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    table = fake.bitable_tables[feishu_table.base(ctx)["table_id"]]

    def state_field() -> dict:
        return next(f for f in table["fields"] if f["field_name"] == "上架状态")

    assert state_field()["description"]["text"] == feishu_table.LISTING_HELP_TEMU
    options = [o["name"] for o in state_field()["property"]["options"]]
    record = _selected_record(ctx, asset_id)
    before = fake.bitable_records[record]["上架状态"]
    # 说明改了：下一次表格同步给已有的表补写一次，选项和格子都不动
    monkeypatch.setattr(feishu_table, "LISTING_HELP_TEMU", "新的上架状态说明")
    feishu_table.sync(ctx)
    assert state_field()["description"]["text"] == "新的上架状态说明"
    assert [o["name"] for o in state_field()["property"]["options"]] == options
    assert fake.bitable_records[record]["上架状态"] == before
    # 说明没再变：不再写字段
    calls = len(fake.bitable_calls)
    feishu_table.sync(ctx)
    assert not [
        c for c in fake.bitable_calls[calls:] if c[0] == "PUT" and "/fields/" in c[1]
    ]


def _shop_saves(fake) -> list[dict]:
    return [body for tail, body in fake.miaoshou_calls if tail == "save_shop_collect_item_info"]


def test_brand_written_in_shop_mode_after_create(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box", brand_id=BRAND_ID)
    _tick_confirm(ctx, fake, asset_id)
    out = temu.sync(ctx)
    assert out["boxed"] == ["Deer0001"] and not out["errors"]
    row = stores.rows(ctx)[0]
    detail = int(row["detail_id"])
    # 建箱载荷里没有品牌（站点模式没有这个字段）；建完用店铺模式整份存回去，只改 brandId
    assert "brandId" not in _created(fake)[0]
    saves = _shop_saves(fake)
    assert len(saves) == 1
    body = saves[0]
    assert body["detailId"] == detail and body["shopId"] == "v2_shop3"
    info = body["shopCollectItemInfo"]
    assert info["brandId"] == BRAND_ID and info["editModel"] == "shop"
    assert info["itemNum"] == "Deer0001" and info["title"] == row["title"]
    assert fake.miaoshou_items[detail]["brands"] == {"v2_shop3": BRAND_ID}
    mark = ctx.db.q1("SELECT * FROM temu_brands WHERE asset_id = ?", (asset_id,))
    assert mark["done_at"] and mark["saved"] == 1 and mark["detail_id"] == str(detail)
    assert "在妙手里检查后点发布" in fake.bitable_records[_selected_record(ctx, asset_id)]["上架备注"]
    assert temu.status(ctx)["items"][0]["brand"]["ok"] is True
    # 再推进不重复写
    temu.sync(ctx)
    assert len(_shop_saves(fake)) == 1


def test_brand_already_chosen_in_miaoshou_is_only_recorded(home, fake, seed_image, spawned) -> None:
    # 先没配品牌建进箱（线上 FLow0006 就是这样），人在妙手里选了品牌，之后再配上
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    temu.sync(ctx)
    detail = int(stores.rows(ctx)[0]["detail_id"])
    fake.miaoshou_items[detail]["brands"] = {"v2_shop3": BRAND_ID}
    set_config(home, "temu", {"enabled": True, "publish": "box", "stores": _stores(BRAND_ID)})
    ctx = reopen(home)
    assert temu.has_work(ctx)
    out = temu.sync(ctx)
    assert not out["errors"] and not _shop_saves(fake)
    mark = ctx.db.q1("SELECT * FROM temu_brands WHERE asset_id = ?", (asset_id,))
    assert mark["done_at"] and mark["saved"] == 0
    assert not temu.has_work(ctx)


def test_brand_failure_holds_auto_publish_until_written(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, brand_id=BRAND_ID)
    _tick_confirm(ctx, fake, asset_id)
    # 妙手报业务错误：进了箱，品牌没写上，不发布
    fake.miaoshou_fail["save_shop_collect_item_info"] = "paramError"
    out = temu.sync(ctx)
    assert out["boxed"] == ["Deer0001"] and not out["published"]
    assert stores.rows(ctx)[0]["state"] == "in_box"
    note = fake.bitable_records[_selected_record(ctx, asset_id)]["上架备注"]
    assert "品牌还没写上" in note and "下一轮再试" in note
    assert temu.has_work(ctx)
    # 存了回成功但没存上：读回核对出来，照样不发布
    fake.miaoshou_fail.clear()
    fake.miaoshou_brand_ignored = True
    out = temu.sync(ctx)
    assert not out["published"] and "妙手没存上" in " ".join(out["errors"])
    # 存上了：这一轮接着发布
    fake.miaoshou_brand_ignored = False
    out = temu.sync(ctx)
    assert out["published"] == ["Deer0001"] and out["listed"] == ["Deer0001"]
    assert temu.status(ctx)["items"][0]["brand"]["ok"] is True


def test_brand_gives_up_after_max_attempts_until_retry(home, fake, seed_image, spawned, monkeypatch) -> None:
    monkeypatch.setattr(temu, "BRAND_MAX_ATTEMPTS", 2)
    ctx, asset_id, *_ = _ready(home, fake, seed_image, brand_id=BRAND_ID)
    _tick_confirm(ctx, fake, asset_id)
    fake.miaoshou_fail["save_shop_collect_item_info"] = "paramError"
    temu.sync(ctx)
    temu.sync(ctx)
    assert "在妙手里把品牌名选成 MIANDUDU" in fake.bitable_records[_selected_record(ctx, asset_id)]["上架备注"]
    assert not temu.has_work(ctx)
    calls = len(_shop_saves(fake))
    temu.sync(ctx)
    assert len(_shop_saves(fake)) == calls
    # 人在妙手里选好了品牌、说了重试：重新核对，认下人选的，接着发布
    detail = int(stores.rows(ctx)[0]["detail_id"])
    fake.miaoshou_items[detail]["brands"] = {"v2_shop3": BRAND_ID}
    temu.retry(ctx, [asset_id], by="feishu:ou_tester")
    out = temu.sync(ctx)
    assert out["published"] == ["Deer0001"]


def test_retry_resave_checks_brand_again(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, brand_id=BRAND_ID)
    _tick_confirm(ctx, fake, asset_id)
    fake.miaoshou_fail["save_move_collect_task"] = "publishRejected"
    out = temu.sync(ctx)
    assert out["failed"] == [asset_id] and len(_shop_saves(fake)) == 1
    row = stores.rows(ctx)[0]
    assert row["state"] == "failed" and row["detail_id"]
    # 重试走站点模式保存（不新建）；保存后品牌没了也要补上，不能只看上次的记录
    fake.miaoshou_fail.clear()
    fake.miaoshou_items[int(row["detail_id"])]["brands"] = {}
    temu.retry(ctx, [asset_id], by="feishu:ou_tester")
    out = temu.sync(ctx)
    assert len(_created(fake)) == 1
    assert [t for t, _ in fake.miaoshou_calls].count("save_site_collect_item_info") == 1
    assert len(_shop_saves(fake)) == 2
    assert fake.miaoshou_items[int(row["detail_id"])]["brands"] == {"v2_shop3": BRAND_ID}
    assert out["published"] == ["Deer0001"]


def _approved_without_store(home, fake, seed_image, *, delivery: bool = True):
    """历史款：选中时没分店（只勾「选中」），套图出完，表顶有它的「套图」行。delivery=False 是 10-26 前的老格式套图。"""
    ctx = _candidates(home, fake, seed_image, control_row=True)
    fake.mooya_delivery = delivery
    asset_id = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    fake.bitable_records[_record(ctx, asset_id)]["选中"] = True
    candidates.accept(ctx, "S01", by="feishu:ou_tester")
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    candidates.sync(ctx)
    assert stores.rows(ctx, asset_ids=[asset_id]) == []
    return ctx, asset_id


def _scene_record(ctx, asset_id: str) -> str:
    return ctx.db.scalar("SELECT record_id FROM candidate_scene_rows WHERE asset_id = ?", (asset_id,))


def _store_tick(ctx, fake, record_id: str, *columns: str) -> dict:
    """人在候选表这一行勾了店铺列，网关转来记录变更事件。"""
    for column in columns:
        fake.bitable_records[record_id][column] = True
    table_id = ctx.db.scalar("SELECT table_id FROM candidate_tables WHERE seed_id = 'S01'")
    return candidates.control_event(
        ctx,
        file_token=feishu_table.base(ctx)["app_token"],
        table_id=table_id,
        record_ids=[record_id],
        by="feishu:ou_tester",
    )


def test_tick_store_on_scene_row_of_selected_design(home, fake, seed_image, spawned) -> None:
    ctx, asset_id = _approved_without_store(home, fake, seed_image)
    scene_record = _scene_record(ctx, asset_id)
    assert scene_record
    out = _store_tick(ctx, fake, scene_record, COL3)
    assert out["stores"]["chosen"] == {asset_id: ["s3"]}
    rows = stores.rows(ctx, asset_ids=[asset_id])
    assert [(r["store_key"], r["state"], r["code"]) for r in rows] == [("s3", "chosen", "Deer0001")]
    # 不用点按钮：当场拉起上架准备，「选中的款」和原图行、套图行都写上店铺和货号
    assert ["temu", "sync"] in [argv[2:4] for argv in spawned]
    selected = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert selected["店铺"] == [COL3] and selected["货号"] == "Deer0001"
    for record in (_record(ctx, asset_id), scene_record):
        assert fake.bitable_records[record][COL3] is True and fake.bitable_records[record]["货号"] == "Deer0001"
    # 后台准备上架图和标题，备注等人勾「确认上架」
    assert temu.sync(ctx)["prepared"] == [asset_id]
    assert "勾「确认上架」" in fake.bitable_records[_selected_record(ctx, asset_id)]["上架备注"]
    # 程序自己写的勾也会发事件：已经记下的店不再处理
    again = _store_tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert "stores" not in again and len(stores.rows(ctx, asset_ids=[asset_id])) == 1


def test_tick_store_on_design_row_of_selected_design(home, fake, seed_image, spawned) -> None:
    ctx, asset_id = _approved_without_store(home, fake, seed_image)
    out = _store_tick(ctx, fake, _record(ctx, asset_id), COL3, COL4)
    assert out["stores"]["chosen"] == {asset_id: ["s3", "s4"]}
    codes = {r["store_key"]: r["code"] for r in stores.rows(ctx, asset_ids=[asset_id])}
    assert codes == {"s3": "Deer0001", "s4": "Flow0001"}
    scene = fake.bitable_records[_scene_record(ctx, asset_id)]
    assert scene[COL3] is True and scene[COL4] is True and scene["货号"] == "Deer0001 / Flow0001"
    # 取消勾不去店：巡检回读也不动
    fake.bitable_records[_scene_record(ctx, asset_id)][COL4] = False
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, candidate_stores.TICKS_META)
    candidate_stores.poll(ctx)
    assert {r["store_key"] for r in stores.rows(ctx, asset_ids=[asset_id])} == {"s3", "s4"}


def test_tick_store_on_unselected_row_waits_for_button(home, fake, seed_image, spawned) -> None:
    ctx = _candidates(home, fake, seed_image, control_row=True)
    asset_id = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    out = _store_tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert out == {"ignored": "改的不是按钮行"}
    assert stores.rows(ctx, asset_ids=[asset_id]) == []


def test_tick_store_on_old_format_scene_row_is_refused(home, fake, seed_image, spawned) -> None:
    ctx, asset_id = _approved_without_store(home, fake, seed_image, delivery=False)
    scene_record = _scene_record(ctx, asset_id)
    out = _store_tick(ctx, fake, scene_record, COL3)
    assert out["stores"]["refused"] == {asset_id: candidate_stores.OLD_FORMAT_NOTE}
    assert stores.rows(ctx, asset_ids=[asset_id]) == []
    row = fake.bitable_records[scene_record]
    assert row.get(COL3) is False and row["备注"] == candidate_stores.OLD_FORMAT_NOTE
    assert not [argv for argv in spawned if argv[2:4] == ["temu", "sync"]]


def test_store_tick_missed_event_is_picked_up_by_poll(home, fake, seed_image, spawned) -> None:
    ctx, asset_id = _approved_without_store(home, fake, seed_image)
    fake.bitable_records[_scene_record(ctx, asset_id)][COL4] = True
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, candidate_stores.TICKS_META)
    assert candidate_stores.poll_due(ctx)
    out = candidate_stores.poll(ctx)
    assert out["chosen"] == {asset_id: ["s4"]}
    assert [r["code"] for r in stores.rows(ctx, asset_ids=[asset_id])] == ["Flow0001"]
    # 刚读过：没到时候不再读
    assert candidate_stores.poll(ctx) is None


def test_set_clients_after_mooya_tenant_move(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    row = scenes.latest_sets(ctx, [asset_id])[asset_id]
    # 当作 10-28 前交的旧套图：没有客户端记录 = legacy，取不到带字标的图
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM scene_set_clients WHERE set_id = ?", (row["set_id"],))
    assert "旧租户" in (temu_assets.readiness(ctx, asset_id) or "")
    job = int(row["mooya_job_id"])
    with pytest.raises(StageError):
        scenes.set_clients(ctx, [job], "nobody", by="ops")
    dry = scenes.set_clients(ctx, [job, 999999], "deerflow", by="ops", dry_run=True)
    assert dry["changed"] == 1 and dry["missing_jobs"] == [999999]
    assert scenes.client_of(ctx, row["set_id"]) == mooya.LEGACY_CLIENT
    out = scenes.set_clients(ctx, [job], "deerflow", by="ops")
    assert out["sets"] == [row["set_id"]] and scenes.client_of(ctx, row["set_id"]) == "deerflow"
    assert temu_assets.readiness(ctx, asset_id) is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'scene_client_moved'") == 1
    assert scenes.set_clients(ctx, [job], "deerflow", by="ops")["changed"] == 0


def test_create_timeout_reconciles_by_code_without_duplicate(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    fake.miaoshou_timeout["create_collect_box_item"] = "after"
    out = temu.sync(ctx)
    assert stores.rows(ctx)[0]["state"] == "create_unknown" and not out["boxed"]
    out = temu.sync(ctx)
    assert out["boxed"] == ["Deer0001"]
    assert len(_created(fake)) == 1
    assert stores.rows(ctx)[0]["state"] == "in_box"


def test_reconcile_checks_code_in_detail_not_just_title(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    fake.miaoshou_timeout["create_collect_box_item"] = "after"
    temu.sync(ctx)
    row = stores.rows(ctx)[0]
    assert row["state"] == "create_unknown"
    real = next(d for d, it in fake.miaoshou_items.items() if it["info"].get("itemNum") == "Deer0001")
    # 同店同标题、货号不同的商品（人手建的、或别的款撞了标题）排在列表更前面：不能当成这一款
    decoy = dict(fake.miaoshou_items[real]["info"])
    decoy["itemNum"] = "Deer0999"
    decoy["collectBoxDetailShopList"] = [{**decoy["collectBoxDetailShopList"][0], "itemNum": "Deer0999"}]
    fake.miaoshou_items[real + 100] = {"info": decoy, "status": "notPublished", "published_by": None}
    out = temu.sync(ctx)
    assert out["boxed"] == ["Deer0001"]
    assert stores.rows(ctx)[0]["detail_id"] == str(real)
    assert len(_created(fake)) == 1


def test_reconcile_recreates_once_when_nothing_was_created(home, fake, seed_image, spawned, monkeypatch) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    fake.miaoshou_timeout["create_collect_box_item"] = "before"

    def made() -> list[int]:
        return [d for d, it in fake.miaoshou_items.items() if it["info"].get("itemNum") == "Deer0001"]

    temu.sync(ctx)
    assert stores.rows(ctx)[0]["state"] == "create_unknown" and not made()
    # 刚超时：先等，不急着重建
    fake.miaoshou_timeout.clear()
    out = temu.sync(ctx)
    assert stores.rows(ctx)[0]["state"] == "create_unknown" and not out["boxed"]
    # 过了等待时间还找不到：回到排队，下一轮只建一次
    monkeypatch.setattr(temu, "RECONCILE_AFTER_SECONDS", 0)
    temu.sync(ctx)
    temu.sync(ctx)
    assert stores.rows(ctx)[0]["state"] == "in_box"
    assert len(made()) == 1


def test_service_error_backs_off(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    _tick_confirm(ctx, fake, asset_id)
    fake.miaoshou_fail["get_site_collect_item_info"] = "appNoPermission"
    with ctx.db.tx() as conn:
        for key in ("s3", "s4"):
            ctx.db.del_meta(conn, temu.TEMPLATE_META_PREFIX + key)
    out = temu.sync(ctx)
    assert "权限" in out["stopped"]
    assert stores.rows(ctx)[0]["state"] == "queued"
    assert temu.has_work(ctx) is False
    assert temu.status(ctx)["service_error"]


def test_edited_title_is_checked_and_rejected(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    out = _tick_confirm(ctx, fake, asset_id, 上架标题="Soft Blanket for Bed 280GSM")
    assert out["queued"] == [] and "上架标题不合规" in out["rejected"][0]["reason"]
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert row["确认上架"] is False and "没上架：上架标题不合规" in row["上架备注"]
    assert stores.rows(ctx)[0]["state"] == "chosen"
    # 改成合规的再勾：用人改的标题
    good = "Flannel Blanket Throw 280GSM Botanical Print Soft Throw for Couch"
    out = _tick_confirm(ctx, fake, asset_id, 上架标题=good)
    assert out["queued"] == [asset_id]
    assert temu_title.get(ctx, asset_id)["title"] == good
    assert temu_title.get(ctx, asset_id)["source"] == "human"
    temu.sync(ctx)
    assert _created(fake)[0]["title"] == good
    # 程序的标题没变：人改的那一格不会被盖掉
    assert fake.bitable_records[_selected_record(ctx, asset_id)]["上架标题"] == good


def test_custom_design_is_not_listed_automatically(home, fake, seed_image, spawned) -> None:
    fake.p15_result = {
        "title": "Custom Flannel Blanket 280GSM Personalized Name Throw for Couch",
        "is_custom": True,
        "lead_type": "text",
        "holiday": None,
        "gift_led": False,
    }
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    _tick_confirm(ctx, fake, asset_id)
    out = temu.sync(ctx)
    assert out["failed"] == [asset_id] and not _created(fake)
    row = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert row["上架状态"] == "上架失败" and "定制款" in row["上架备注"]
    assert row["确认上架"] is False
    # 人改判定为不是定制款（先改标题）
    with pytest.raises(StageError):
        temu_title.set_custom(ctx, asset_id, False, by="t")


def test_poll_cancels_unticked_queue_and_reticks_started(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    record = _selected_record(ctx, asset_id)
    fake.bitable_records[record]["确认上架"] = False
    out = temu.poll(ctx)
    assert out["cancelled"] == [asset_id]
    assert stores.rows(ctx)[0]["state"] == "chosen"
    # 漏掉事件的勾：巡检接上
    fake.bitable_records[record]["确认上架"] = True
    out = temu.poll(ctx)
    assert out["confirmed"] == [asset_id]
    temu.sync(ctx)
    assert stores.rows(ctx)[0]["state"] == "in_box"
    # 已经进箱了还取消勾：取消不了，勾写回去
    fake.bitable_records[record]["确认上架"] = False
    out = temu.poll(ctx)
    assert out["reticked"] == [asset_id] and fake.bitable_records[record]["确认上架"] is True


def test_listed_table_reads_human_columns_and_restores_deleted_rows(home, fake, seed_image, spawned) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    _tick_confirm(ctx, fake, asset_id)
    temu.sync(ctx)
    record_id = listed_table.rows(ctx)[0]["record_id"]
    fake.bitable_records[record_id].update(
        {"Temu 状态": "在售", "累计销量": 12, "近 7 天销量": 3, "累计销售额": 456.789, "备注": "首单"}
    )
    # 「妙手 ID」是程序列（2026-10-30 起「上架标题」是指令列，改了就同步到店铺，见 test_title_sync）
    fake.bitable_records[record_id]["妙手 ID"] = "人手改的"
    out = listed_table.read_back(ctx)
    assert out["changed"] == ["Deer0001"] and out["rewrite"] == ["Deer0001"]
    row = listed_table.rows(ctx)[0]
    assert (row["temu_status"], row["sales_total"], row["sales_7d"], row["revenue_total"], row["note"]) == (
        "在售",
        12,
        3,
        456.79,
        "首单",
    )
    # 人填的数据进了时间序列；程序列被改了记成 reverted（下次同步改回）
    assert [(m["temu_status"], m["sales_total"], m["source"]) for m in listed_table.metrics(ctx, "Deer0001")][-1] == (
        "在售",
        12,
        "human",
    )
    outcomes = {(r["field"], r["outcome"]) for r in ctx.db.q("SELECT field, outcome FROM table_cell_log WHERE kind = 'listed'")}
    assert ("妙手 ID", "reverted") in outcomes
    # 之后再改人填的列：旧值、新值都记下，时间序列多一条
    fake.bitable_records[record_id]["累计销量"] = 15
    listed_table.read_back(ctx)
    log = ctx.db.q1("SELECT * FROM table_cell_log WHERE kind = 'listed' AND field = '累计销量'")
    assert (log["old_json"], log["new_json"], log["outcome"]) == ("12", "15", "recorded")
    assert [m["sales_total"] for m in listed_table.metrics(ctx, "Deer0001")][-1] == 15
    # 程序列被改了：同步时改回来
    feishu_table.sync(ctx)
    assert fake.bitable_records[record_id]["妙手 ID"] != "人手改的"
    # 行被删了：按库里的程序列 + 人填列重建
    fake.bitable_records.pop(record_id)
    out = listed_table.read_back(ctx)
    assert out["restore"] == ["Deer0001"]
    feishu_table.sync(ctx)
    new_id = listed_table.rows(ctx)[0]["record_id"]
    assert new_id and new_id != record_id
    assert fake.bitable_records[new_id]["Temu 状态"] == "在售" and fake.bitable_records[new_id]["累计销量"] == 15
    # 整行被删记一条 deleted；重建的新行第一次读到只记下
    assert ctx.db.scalar("SELECT COUNT(*) FROM table_cell_log WHERE kind = 'listed' AND outcome = 'deleted'") == 1
    exported = listed_table.export_csv(ctx, Path(home) / "listed.csv")
    text = (Path(home) / "listed.csv").read_text(encoding="utf-8-sig")
    assert exported["rows"] == 1 and "Deer0001" in text and "在售" in text
