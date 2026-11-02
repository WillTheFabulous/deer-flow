"""Temu 上架统一到 mooya（2026-10-30，mooya feature-plans/temu-listing-unify）：

- 「已上架的款」读 mooya 四家店底账（temu.listed.source: mooya）：增量拉、1店 / 2店进店铺列与视图、主图从 mooya 取、
  人填的列和改的标题写回 mooya、本机已经写过飞书的行接着用、本机引擎上的款推给 mooya。
- 上架交给 mooya（temu.engine: mooya）：交单（ref 幂等、标签、标题）、跟进状态写回「选中的款」、已上架走原来的收尾、
  轨迹拉进 trace、预检没过接着等、同一款第二家店等第一家的标题、暂停 / 取消、切换时搬单。
假 mooya 在 conftest 的 FakeProvider._mooya_temu。
"""

from __future__ import annotations

import pytest

from conftest import reopen, set_config
from podflow import candidates, feishu_table, listed_table, mooya_listing, review, scenes, stores, temu, temu_title
from test_temu import (  # noqa: F401 — spawned 是夹具
    COL3,
    COL4,
    _candidates,
    _ready,
    _record,
    _selected_record,
    _tick_confirm,
    spawned,
)

pytestmark = pytest.mark.usefixtures("spawned")

GOOD_TITLE = (
    "Vintage Floral Garden Flannel Blanket 280GSM Watercolor Rose Peony Print "
    "Lightweight Warm Throw Gift For Family For Sofa Couch Living Room Travel"
)
S1 = {"key": "s1", "label": "1店", "name": "毛毯pod", "column": "1店 毛毯pod", "prefix": "FLR", "enabled": True, "ui": True}
S2 = {"key": "s2", "label": "2店", "name": "毛毯POD2店", "column": "2店 毛毯POD2店", "prefix": "JUH", "enabled": True, "ui": True}
S3 = {"key": "s3", "label": "3店", "name": "CozWeave H", "column": COL3, "prefix": "Deer", "enabled": True, "ui": False}
S4 = {"key": "s4", "label": "4店", "name": "LoomNest J", "column": COL4, "prefix": "Flow", "enabled": True, "ui": False}


def _mooya_stores(fake) -> None:
    fake.mooya_tokens["test-mooya-ai-temu"] = ("temu-ledger-ai-temu", "ai_temu")
    fake.temu_stores["ai_temu"] = [S1, S2]
    fake.temu_stores["ai_temu_deerflow"] = [S3, S4]


def _ledger_source(home) -> None:
    with open(home / "secrets.env", "a", encoding="utf-8") as fh:
        fh.write("MOOYA_TOKEN_AI_TEMU=test-mooya-ai-temu\n")
    set_config(
        home,
        "mooya.clients",
        {
            "deerflow": {"token_name": "MOOYA_SERVICE_TOKEN_DEERFLOW"},
            "ai_temu": {"token_name": "MOOYA_TOKEN_AI_TEMU"},
        },
    )
    set_config(home, "temu.listed.source", "mooya")
    set_config(home, "temu.listed.clients", ["deerflow", "ai_temu"])


def _listed_records(fake, ctx) -> dict[str, dict]:
    meta = listed_table._meta(ctx)
    table = meta.get("table_id")
    out = {}
    for record_id, cells in fake.bitable_records.items():
        if fake.bitable_record_tables.get(record_id) == table and cells.get("货号"):
            out[cells["货号"]] = {"record_id": record_id, **cells}
    return out


# ---------------------------------------------------------------- 「已上架的款」读 mooya 底账


def test_ledger_source_mirrors_four_stores_into_listed_table(home, fake, seed_image, monkeypatch) -> None:
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    _mooya_stores(fake)
    _ledger_source(home)
    ctx = reopen(home)
    fake.temu_add_listed("ai_temu", "FLR0042", {**S1}, listedAt="2026-06-01T03:00:00Z", source="backfill",
                         human={"sales_total": 5})
    fake.temu_add_listed("ai_temu", "JUH0615", {**S2}, listedAt="2026-10-25T03:30:00Z", source="backfill")
    fake.temu_add_listed(
        "ai_temu", "FLR1103", {**S1}, listedAt="2026-10-30T10:57:00Z",
        snapshot={"label": "花园", "task": "1026 毯子", "store": S1["column"], "prices": [{"sku": "30x40in", "price": 150}],
                  "images": [{"n": 1, "plan_id": "blanket_04_model_read_lap"}], "main_kind": "模特盖腿读书",
                  "image_order": "1 read"},
        mainImage={"path": "/temu/listed/3/image"},
        package={"url": "https://oss.test/FLR1103.zip", "expiresAt": "2027-01-28T00:00:00Z", "filename": "FLR1103_上架图包.zip"},
    )
    monkeypatch.setattr(listed_table, "_PULL_PAGE", 2)
    out = listed_table.pull(ctx)
    assert out["pulled"] == 3 and sorted(out["changed"]) == ["FLR0042", "FLR1103", "JUH0615"] and not out["errors"]
    # 按页拉：两页（2 + 1），游标记在 meta
    pages = [c for c in fake.temu_calls if c[1] == "/temu/listed"]
    assert len([p for p in pages if p[2].get("limit") == "2"]) >= 3
    rows = {r["code"]: r for r in listed_table.rows(ctx)}
    assert set(rows) == {"FLR0042", "JUH0615", "FLR1103"}
    assert rows["FLR0042"]["client"] == "ai_temu" and rows["FLR0042"]["sales_total"] == 5 and rows["FLR0042"]["asset_id"] is None
    # 店铺列选项：本机两家店在前，mooya 那边的 1店 / 2店接在后面
    assert listed_table.store_columns(ctx) == [COL3, COL4, S1["column"], S2["column"]]
    # 再拉一次没变化
    again = listed_table.pull(ctx)
    assert again["pulled"] == 0 and again["changed"] == []

    feishu_table.sync(ctx)
    records = _listed_records(fake, ctx)
    assert set(records) == {"FLR0042", "JUH0615", "FLR1103"}
    one = records["FLR1103"]
    assert one["店铺"] == S1["column"] and one["素材"] == "花园" and one["任务"] == "1026 毯子"
    assert one["主图类型"] == "模特盖腿读书" and one["上架图包"]["link"] == "https://oss.test/FLR1103.zip"
    assert one["主图"] and records["FLR0042"]["主图"]
    assert records["FLR0042"]["累计销量"] == 5
    # 主图：人工款从妙手图链接取，上架引擎上的从 mooya 带令牌的接口取
    assert any(c[1] == "/temu/listed/3/image" for c in fake.temu_calls)
    meta = listed_table._meta(ctx)
    assert {S1["column"], S2["column"]} <= set((meta.get("views") or {}).get("stores") or {})
    # 没有新变化：不再写
    assert listed_table._pending(ctx) == []
    # mooya 续签了上架图包：拉回来程序列变了，下次同步改这一格
    fake.temu_add_listed(
        "ai_temu", "FLR1103", {**S1}, listedAt="2026-10-30T10:57:00Z",
        snapshot=fake.temu_listed[("ai_temu", "FLR1103")]["snapshot"],
        mainImage={"path": "/temu/listed/3/image"},
        package={"url": "https://oss.test/FLR1103-new.zip", "expiresAt": "2027-03-01T00:00:00Z"},
    )
    listed_table.pull(ctx)
    assert [r["code"] for r in listed_table._pending(ctx)] == ["FLR1103"]
    feishu_table.sync(ctx)
    assert _listed_records(fake, ctx)["FLR1103"]["上架图包"]["link"] == "https://oss.test/FLR1103-new.zip"


def test_ledger_source_writes_rows_whose_main_image_is_gone(home, fake, seed_image) -> None:
    """人工款的妙手图链接失效、mooya 那边也取不到图（都是 404）：照样建行，只是没有主图，不算失败。"""
    _ready(home, fake, seed_image)
    _mooya_stores(fake)
    _ledger_source(home)
    ctx = reopen(home)
    fake.temu_add_listed("ai_temu", "FLR0043", {**S1}, listedAt="2026-06-01T03:00:00Z", source="backfill",
                         mainImage={"url": "http://img.test/gone/FLR0043.jpg"})
    fake.temu_add_listed("ai_temu", "FLR0044", {**S1}, listedAt="2026-06-01T03:00:00Z", source="backfill",
                         mainImage={"path": "/temu/listed/gone/image"})
    fake.temu_add_listed("ai_temu", "FLR0045", {**S1}, listedAt="2026-06-01T03:00:00Z", source="backfill")
    listed_table.pull(ctx)
    feishu_table.sync(ctx)
    records = _listed_records(fake, ctx)
    assert {"FLR0043", "FLR0044", "FLR0045"} <= set(records)
    assert not records["FLR0043"].get("主图") and not records["FLR0044"].get("主图") and records["FLR0045"]["主图"]
    assert listed_table._pending(ctx) == []
    assert not [r for r in listed_table.rows(ctx) if r["last_error"]]


def test_ledger_source_pushes_human_edits_and_titles_to_mooya(home, fake, seed_image) -> None:
    ctx, *_ = _ready(home, fake, seed_image)
    _mooya_stores(fake)
    _ledger_source(home)
    ctx = reopen(home)
    fake.temu_add_listed("ai_temu", "FLR0042", {**S1}, title="Old Flannel Blanket 280GSM Print")
    listed_table.pull(ctx)
    feishu_table.sync(ctx)
    record_id = _listed_records(fake, ctx)["FLR0042"]["record_id"]
    listed_table.read_back(ctx)
    # 人填的列：写回 mooya（修改人、来源），本机不再记时间序列
    fake.bitable_records[record_id].update({"Temu 状态": "在售", "累计销量": 12})
    out = listed_table.read_back(ctx)
    assert out["changed"] == ["FLR0042"] and not out.get("push_errors")
    pushes = [c for c in fake.temu_calls if c[1] == "/temu/listed/FLR0042/human"]
    assert pushes and pushes[-1][2]["fields"] == {"temu_status": "在售", "sales_total": 12} and pushes[-1][2]["via"] == "feishu"
    assert fake.temu_listed[("ai_temu", "FLR0042")]["human"]["sales_total"] == 12
    row = next(r for r in listed_table.rows(ctx) if r["code"] == "FLR0042")
    assert row["sales_total"] == 12 and row["temu_status"] == "在售"
    assert ctx.db.scalar("SELECT COUNT(*) FROM listed_metrics") == 0
    # 再读一次：本机镜像和飞书一样，不再写
    before = len(fake.temu_calls)
    listed_table.read_back(ctx)
    assert not [c for c in fake.temu_calls[before:] if c[1].endswith("/human")]
    # 改标题：不合规的 mooya 退回（记 rejected），合规的交给 mooya（没接 Temu 接口：要人去 Temu 后台改）
    fake.bitable_records[record_id]["上架标题"] = "No Gsm Flannel Blanket"
    out = listed_table.read_back(ctx)
    assert out["titles"] and out["titles"][0].get("rejected")
    outcomes = {r["outcome"] for r in ctx.db.q("SELECT outcome FROM table_cell_log WHERE field = '上架标题'")}
    assert "rejected" in outcomes
    fake.bitable_records[record_id]["上架标题"] = GOOD_TITLE
    out = listed_table.read_back(ctx)
    assert out["titles"][-1].get("saved")
    assert fake.temu_listed[("ai_temu", "FLR0042")]["title"] == GOOD_TITLE
    feishu_table.sync(ctx)
    assert "要人在 Temu 后台改" in _listed_records(fake, ctx)["FLR0042"]["标题同步"]


def test_ledger_source_reuses_rows_and_pushes_local_listings(home, fake, seed_image) -> None:
    # 先在本机引擎上一款（listed_products + 飞书行）
    ctx, asset_id, *_ = _ready(home, fake, seed_image)
    _tick_confirm(ctx, fake, asset_id)
    temu.sync(ctx)
    local = listed_table.rows(ctx)
    assert [r["code"] for r in local] == ["Deer0001"] and local[0]["record_id"]
    old_record = local[0]["record_id"]
    records_before = set(fake.bitable_records)
    # 切到 mooya 底账：本机上的推给 mooya（搬单），拉回来接着用原来那一行
    _mooya_stores(fake)
    _ledger_source(home)
    ctx = reopen(home)
    out = listed_table.pull(ctx)
    imports = [c for c in fake.temu_calls if c[1] == "/temu/listings/import"]
    assert imports and imports[0][2]["items"][0]["code"] == "Deer0001"
    item = imports[0][2]["items"][0]
    assert item["state"] == "listed" and item["listed"]["snapshot"]["store"] == COL3
    assert item["labels"]["asset_id"] == asset_id and item["ref"] == stores.code_ref(ctx, asset_id, "s3")
    assert out.get("pushed") == ["Deer0001"]
    # 推过去之后下一次拉就有了（本轮推完再拉一次）
    listed_table.pull(ctx)
    rows = {r["code"]: r for r in listed_table.rows(ctx)}
    assert rows["Deer0001"]["record_id"] == old_record and rows["Deer0001"]["asset_id"] == asset_id
    assert listed_table.status(ctx)["not_pushed"] == []
    feishu_table.sync(ctx)
    # 没建新行（原来那一行改写）
    assert old_record in fake.bitable_records
    new_rows = [r for r in set(fake.bitable_records) - records_before if fake.bitable_records[r].get("货号") == "Deer0001"]
    assert new_rows == []


# ---------------------------------------------------------------- 上架交给 mooya


def _mooya_engine(home, fake) -> None:
    _mooya_stores(fake)
    set_config(home, "temu.engine", "mooya")


def _ready_mooya(home, fake, seed_image, *, picks=(COL3,)):
    """同 test_temu._ready，只是候选表建好以后切到 mooya 上架（_temu_home 会整段改写 temu 配置）。"""
    ctx = _candidates(home, fake, seed_image)
    _mooya_engine(home, fake)
    ctx = reopen(home)
    asset_id = review.list_reviews(ctx, types=["asset_selection"])[0]["subject_id"]
    for column in picks:
        fake.bitable_records[_record(ctx, asset_id)][column] = True
    candidates.accept(ctx, "S01", by="feishu:ou_tester")
    scenes.sync(ctx)
    feishu_table.sync(ctx)
    prepared = temu.sync(ctx)
    return ctx, asset_id, prepared


def _created_listings(fake) -> list[dict]:
    return [c[2] for c in fake.temu_calls if c[0] == "POST" and c[1] == "/temu/listings"]


def test_mooya_engine_submits_follows_and_lists(home, fake, seed_image) -> None:
    ctx, asset_id, prepared = _ready_mooya(home, fake, seed_image)
    # mooya 模式不在本机准备上架图、不写标题
    assert prepared.get("engine") == "mooya" and temu_title.get(ctx, asset_id) is None
    assert "交给 mooya 上架" in fake.bitable_records[_selected_record(ctx, asset_id)]["上架备注"]
    temu.confirm(ctx, [asset_id], by="feishu:ou_tester", source="table")
    out = temu.sync(ctx)
    assert out["submitted"] == ["Deer0001"]
    body = _created_listings(fake)[0]
    current = ctx.db.q1("SELECT mooya_job_id, set_id FROM scene_sets WHERE asset_id = ? ORDER BY set_id DESC", (asset_id,))
    assert body["job_id"] == int(current["mooya_job_id"]) and body["store"] == "s3" and body["code"] == "Deer0001"
    assert body["ref"] == stores.code_ref(ctx, asset_id, "s3") and body["held"] is False
    assert body["labels"]["asset_id"] == asset_id and body["labels"]["set_id"] == current["set_id"]
    assert body["actor"] == "feishu:ou_tester" and "title" not in body
    row = stores.rows(ctx)[0]
    assert row["state"] == temu.STATE_QUEUED
    assert "mooya 排队中" in temu.note_line(ctx, row)
    # 不会再交第二次（同一 ref）
    temu.sync(ctx)
    assert len(_created_listings(fake)) == 1
    listing_id = mooya_listing.mapping(ctx, asset_id, "s3")["listing_id"]

    # mooya 建进采集箱、写好标题：写回 store_listings 与「选中的款」，标题记进本机
    fake.temu_listing(listing_id, state="in_box", detailId="3500000001", title=GOOD_TITLE, titleSource="model",
                      titleMeta={"is_custom": False}, waitReason=None)
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, mooya_listing.POLLED_META_KEY)
    out = temu.sync(ctx)
    row = stores.rows(ctx)[0]
    assert row["state"] == temu.STATE_BOX and row["detail_id"] == "3500000001" and out["boxed"] == ["Deer0001"]
    title = temu_title.get(ctx, asset_id)
    assert title["title"] == GOOD_TITLE and title["source"] == "auto"
    selected = fake.bitable_records[_selected_record(ctx, asset_id)]
    assert selected["上架状态"] == "上架中" and selected["上架标题"] == GOOD_TITLE
    # 在箱里还没发布：到了该问的时候要起后台
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, mooya_listing.POLLED_META_KEY)
    assert temu.has_work(ctx) is True

    # mooya 查到已上架：本机走原来的收尾（上架状态、listing_marks），底账在 mooya、本机不记 listed_products
    fake.temu_listing(listing_id, state="published", publishedAt="2026-10-30T12:30:00Z")
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, mooya_listing.POLLED_META_KEY)
    out = temu.sync(ctx)
    assert out["listed"] == ["Deer0001"]
    row = stores.rows(ctx)[0]
    assert row["state"] == temu.STATE_LISTED and row["listed_at"] == "2026-10-30T12:30:00Z"
    assert ctx.db.q1("SELECT state FROM listing_marks WHERE asset_id = ?", (asset_id,))["state"] == "已上架"
    assert ctx.db.scalar("SELECT COUNT(*) FROM listed_products") == 0
    # mooya 的轨迹拉进 trace（stage mooya）
    stages = [r["to_state"] for r in ctx.db.q("SELECT to_state FROM trace_events WHERE stage = 'mooya' AND asset_id = ?", (asset_id,))]
    assert {"queued", "in_box", "published"} <= set(stages)
    assert temu.has_work(ctx) is False
    # 到了该问 mooya 的时候也不用起后台：已上架、本机也记上了
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, mooya_listing.POLLED_META_KEY)
    assert temu.has_work(ctx) is False


def test_mooya_engine_waits_on_problems_and_second_store_waits_for_title(home, fake, seed_image) -> None:
    ctx, asset_id, _ = _ready_mooya(home, fake, seed_image, picks=(COL3, COL4))
    ref3 = stores.code_ref(ctx, asset_id, "s3")
    fake.temu_problems[ref3] = ["第 1–2 张不是模特盖腿读书 / 沙发搭毯那一对（10-26 之前建的旧包没有前排钉序）"]
    temu.confirm(ctx, [asset_id], by="feishu:ou_tester", source="table")
    out = temu.sync(ctx)
    # 3店预检没过：不算失败，接着等，原因进备注；4店照常交
    rows = {r["store_key"]: r for r in stores.rows(ctx)}
    assert rows["s3"]["state"] == temu.STATE_QUEUED and "第 1–2 张" in rows["s3"]["last_error"]
    assert out["submitted"] == ["Flow0001"]
    assert "第 1–2 张" in fake.bitable_records[_selected_record(ctx, asset_id)]["上架备注"]
    # 包修好了：3店要等 4店的标题（一款一个标题）
    fake.temu_problems.clear()
    out = temu.sync(ctx)
    assert "submitted" not in out and any(w["reason"] == "等同一款另一家店的标题" for w in out["waiting"])
    listing4 = mooya_listing.mapping(ctx, asset_id, "s4")["listing_id"]
    fake.temu_listing(listing4, state="titled", title=GOOD_TITLE, titleSource="model")
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, mooya_listing.POLLED_META_KEY)
    out = temu.sync(ctx)
    # 先拉到 4店的标题，下一轮带着标题交 3店
    out = temu.sync(ctx) if "submitted" not in out else out
    body3 = [b for b in _created_listings(fake) if b["store"] == "s3"][-1]
    assert body3["title"] == GOOD_TITLE and body3["title_source"] == "model"


def test_mooya_engine_cancel_hold_and_retry(home, fake, seed_image) -> None:
    ctx, asset_id, _ = _ready_mooya(home, fake, seed_image)
    temu.confirm(ctx, [asset_id], by="feishu:ou_tester", source="table")
    temu.sync(ctx)
    first = mooya_listing.mapping(ctx, asset_id, "s3")["listing_id"]
    # 取消确认：mooya 那边先取消，本机回到待上架
    out = temu.cancel(ctx, [asset_id], by="feishu:ou_tester", source="table")
    assert out["cancelled"] == [asset_id]
    assert fake.temu_listings[first]["state"] == "cancelled"
    assert stores.rows(ctx)[0]["state"] == temu.STATE_CHOSEN
    # 再确认：建一条新的上架单
    temu.confirm(ctx, [asset_id], by="feishu:ou_tester", source="table")
    temu.sync(ctx)
    second = mooya_listing.mapping(ctx, asset_id, "s3")["listing_id"]
    assert second != first and len(_created_listings(fake)) == 2
    # 暂停以本机为准：本机暂停 → mooya hold；恢复 → release
    temu.hold(ctx, [asset_id], by="feishu:ou_tester", source="table")
    assert temu.has_work(ctx)
    temu.sync(ctx)
    assert fake.temu_listings[second]["held"] is True
    temu.release(ctx, [asset_id], by="feishu:ou_tester", source="table")
    temu.sync(ctx)
    assert fake.temu_listings[second]["held"] is False
    holds = [c[2]["action"] for c in fake.temu_calls if c[1] == f"/temu/listings/{second}/actions"]
    assert holds == ["hold", "release"]
    # mooya 里没上成：本机记失败；重新确认 = mooya 的 retry（不建新单）
    fake.temu_listing(second, state="failed", lastError="定制款", errorKind="custom",
                      actions={"cancel": True, "hold": False, "release": False, "retry": True})
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, mooya_listing.POLLED_META_KEY)
    out = temu.sync(ctx)
    row = stores.rows(ctx)[0]
    assert row["state"] == temu.STATE_FAILED and "定制款" in row["last_error"] and out["failed"] == [asset_id]
    temu.confirm(ctx, [asset_id], by="feishu:ou_tester", source="table")
    out = temu.sync(ctx)
    assert out.get("retried") == ["Deer0001"] and len(_created_listings(fake)) == 2
    assert fake.temu_listings[second]["state"] == "titled"


def test_export_to_mooya_moves_in_flight_rows(home, fake, seed_image) -> None:
    # 本机引擎建进采集箱（box 店）
    ctx, asset_id, *_ = _ready(home, fake, seed_image, publish="box")
    _tick_confirm(ctx, fake, asset_id)
    temu.sync(ctx)
    row = stores.rows(ctx)[0]
    assert row["state"] == temu.STATE_BOX
    # 切到 mooya：搬单（dry-run 先看），搬过去的记下 mooya 上架单号
    _mooya_engine(home, fake)
    ctx = reopen(home)
    dry = mooya_listing.export(ctx, dry_run=True)
    assert dry["would_push"] == [{"code": "Deer0001", "state": "in_box"}]
    out = mooya_listing.export(ctx)
    assert out["pushed"] == ["Deer0001"] and not out["errors"]
    item = [c for c in fake.temu_calls if c[1] == "/temu/listings/import"][0][2]["items"][0]
    assert item["state"] == "in_box" and item["detail_id"] == row["detail_id"]
    assert len(item["images"]) == 10 and all(f["key"] for f in item["images"])
    assert item["title"] and item["ref"] == stores.code_ref(ctx, asset_id, "s3")
    found = mooya_listing.mapping(ctx, asset_id, "s3")
    assert found and found["state"] == "in_box"
    # 再搬一次不重复
    assert mooya_listing.export(ctx)["pushed"] == []
    # 之后 mooya 发布、查到已上架：本机照常收尾
    fake.temu_listing(found["listing_id"], state="published", publishedAt="2026-10-30T13:00:00Z")
    out = temu.sync(ctx)
    assert out["listed"] == ["Deer0001"] and stores.rows(ctx)[0]["state"] == temu.STATE_LISTED
