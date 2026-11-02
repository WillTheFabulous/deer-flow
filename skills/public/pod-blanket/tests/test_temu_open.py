"""Temu 官方 Partner 接口（temu_open）：签名、按货号找 goodsId、回读状态进三张表、已上架的款改标题走 Temu、出错退避、探测与体检。"""

from __future__ import annotations

import datetime as dt
import hashlib
from zoneinfo import ZoneInfo

import pytest

from conftest import reopen, set_config
from podflow import (
    doctor,
    feishu_table,
    listed_table,
    stores,
    temu,
    temu_open,
    temu_title,
    title_sync,
    trace,
)
from test_auto_listing import _candidates, _pending_asset, _run_to_listed, _tick
from test_temu import COL3, _record, spawned  # noqa: F401 - 夹具，下面 usefixtures 用
from test_title_sync import NEW

pytestmark = pytest.mark.usefixtures("spawned")

GOODS_ID = 601


def test_sign_matches_partner_rule() -> None:
    params = {
        "type": "bg.x",
        "b": {"k": [1, "中"]},
        "a": True,
        "sign": "ignored",
        "n": 5,
    }
    raw = "s" + "atrue" + 'b{"k":[1,"中"]}' + "n5" + "typebg.x" + "s"
    assert (
        temu_open.sign("s", params)
        == hashlib.md5(raw.encode("utf-8")).hexdigest().upper()
    )


def _open_home(home, fake, seed_image, *, expires_on: str = "2099-01-01"):
    _candidates(home, fake, seed_image)
    with (home / "secrets.env").open("a", encoding="utf-8") as f:
        f.write(
            "TEMU_APP_KEY=tk_test\nTEMU_APP_SECRET=temu_secret\nTEMU_TOKEN_S3=tok_s3\n"
        )
    set_config(
        home,
        "temu_open",
        {
            "enabled": True,
            "base_url": "http://temu.test/openapi/router",
            "min_interval_seconds": 0,
            "goods_url": "https://www.temu.com/goods.html?goods_id={goods_id}",
            "stores": {"s3": {"token_name": "TEMU_TOKEN_S3", "expires_on": expires_on}},
        },
    )
    return reopen(home)


def _listed(home, fake, seed_image, **kwargs):
    ctx = _open_home(home, fake, seed_image, **kwargs)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert _run_to_listed(ctx)["listed"] == ["Deer0001"]
    return ctx, asset_id


def _goods(title: str, *, status: str = "在售中") -> dict:
    return {
        "goodsId": GOODS_ID,
        "goodsName": title,
        "outGoodsSn": "Deer0001",
        "skuIdList": [7001],
        "goodsShowSubStatus": status,
    }


def _due(ctx) -> None:
    with ctx.db.tx() as conn:
        ctx.db.del_meta(conn, temu_open.CHECKED_META_KEY)


def test_sync_finds_goods_and_writes_status_everywhere(home, fake, seed_image) -> None:
    ctx, asset_id = _listed(home, fake, seed_image)
    title = temu_title.get(ctx, asset_id)["title"]
    # 刚发布（上架后台那一轮顺带找过）：Temu 列表里还没有，记一次没找到
    assert dict(ctx.db.q1("SELECT attempts, goods_id FROM temu_goods")) == {
        "attempts": 1,
        "goods_id": None,
    }
    fake.temu_open_goods[GOODS_ID] = _goods(title)
    out = temu_open.sync(ctx)
    assert out["found"] == ["Deer0001"]
    goods = ctx.db.q1("SELECT * FROM temu_goods WHERE code = 'Deer0001'")
    assert (goods["goods_id"], goods["status"], goods["goods_name"]) == (
        str(GOODS_ID),
        "在售中",
        title,
    )
    listed = listed_table.rows(ctx)[0]
    assert (listed["temu_status"], listed["temu_product_id"]) == (
        "在售中",
        str(GOODS_ID),
    )
    assert listed["product_url"].endswith(f"goods_id={GOODS_ID}")
    assert [m["source"] for m in listed_table.metrics(ctx, "Deer0001")][-1] == "api"
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM trace_events WHERE stage = 'temu' AND source = 'temu_open'"
        )
        == 1
    )
    feishu_table.sync(ctx)
    cells = fake.bitable_records[listed["record_id"]]
    assert cells["Temu 状态"] == "在售中" and cells["Temu 商品 ID"] == str(GOODS_ID)
    # 接口管的列：人在表里改了，下次同步改回
    cells["Temu 状态"] = "人填的"
    listed_table.read_back(ctx)
    assert listed_table.rows(ctx)[0]["temu_status"] == "在售中"
    feishu_table.sync(ctx)
    assert cells["Temu 状态"] == "在售中"
    # 到点回读：被驳回进「上架要你看」
    fake.temu_open_goods[GOODS_ID]["goodsShowSubStatus"] = "审核驳回"
    _due(ctx)
    assert temu_open.sync(ctx)["refreshed"] == 1
    assert any("驳回" in a["text"] for a in temu.alerts(ctx))
    assert trace.timeline(ctx, asset_id)["count"] > 0


def test_listed_title_edit_goes_through_temu(home, fake, seed_image) -> None:
    ctx, asset_id = _listed(home, fake, seed_image)
    fake.temu_open_goods[GOODS_ID] = _goods(temu_title.get(ctx, asset_id)["title"])
    _due(ctx)
    temu_open.sync(ctx)
    listed_table.read_back(ctx)
    record_id = listed_table.rows(ctx)[0]["record_id"]
    fake.bitable_records[record_id]["上架标题"] = NEW
    fake.bitable_modifiers[record_id] = "ou_editor"
    out = listed_table.read_back(ctx)
    assert out["titles"][0]["stores"] == [
        {"store": "s3", "state": "pending", "target": "temu"}
    ]
    # 提交编辑（进 Temu 审核）：读回还是旧标题时等着，不重复提交
    fake.temu_open_apply_titles = False
    result = temu_open.sync(ctx)
    assert result["titles"] == [{"code": "Deer0001", "state": "submitted"}]
    task = title_sync.status(ctx, asset_id)[0]
    assert (task["state"], task["modify_id"]) == (
        title_sync.STATE_TEMU_SUBMITTED,
        f"M{GOODS_ID}",
    )
    _due(ctx)
    assert temu_open.sync(ctx)["titles"] == [
        {"code": "Deer0001", "state": "waiting_review"}
    ]
    updates = [t for t, _ in fake.temu_open_calls if t == temu_open.DEFAULT_UPDATE_API]
    assert len(updates) == 1
    # 审核过了：读回是新标题，记生效
    fake.temu_open_goods[GOODS_ID]["goodsName"] = NEW
    _due(ctx)
    assert temu_open.sync(ctx)["titles"] == [{"code": "Deer0001", "state": "done"}]
    assert title_sync.status(ctx, asset_id)[0]["state"] == title_sync.STATE_DONE
    feishu_table.sync(ctx)
    assert fake.bitable_records[record_id]["标题同步"].startswith("3店：已生效（Temu）")


def test_service_error_backs_off(home, fake, seed_image) -> None:
    ctx, _asset_id = _listed(home, fake, seed_image)
    fake.temu_open_fail[temu_open.DEFAULT_LIST_API] = (7000020, "access_token expired")
    _due(ctx)
    out = temu_open.sync(ctx)
    assert "access_token" in out["errors"][0]["error"]
    calls = len(fake.temu_open_calls)
    # 鉴权类错误整轮停、退避一阵，不每轮都去撞
    assert temu_open.sync(ctx).get("skipped") and len(fake.temu_open_calls) == calls
    check = temu_open.doctor_checks(ctx)[0]
    assert check["level"] == "warn" and "上次出错" in check["detail"]


def test_probe_and_doctor(home, fake, seed_image) -> None:
    soon = (
        dt.datetime.now(ZoneInfo("Asia/Shanghai")).date() + dt.timedelta(days=3)
    ).isoformat()
    ctx, asset_id = _listed(home, fake, seed_image, expires_on=soon)
    fake.temu_open_goods[GOODS_ID] = _goods(temu_title.get(ctx, asset_id)["title"])
    before = [dict(r) for r in ctx.db.q("SELECT * FROM temu_goods")]
    got = temu_open.probe(ctx)
    assert got["credentials"] is True
    s3, s4 = got["stores"]
    assert (
        s3["token"]
        and s3["total"] == 1
        and s3["listed"]
        == [{"code": "Deer0001", "goods_id": GOODS_ID, "status": "在售中"}]
    )
    assert s4 == {"store": stores.get(ctx, "s4").column, "token": False}
    # 探测不写库
    assert [dict(r) for r in ctx.db.q("SELECT * FROM temu_goods")] == before
    check = temu_open.doctor_checks(ctx)[0]
    assert "3 天后到期" in check["detail"] and "没有 access_token" in check["detail"]
    names = [c["check"] for c in doctor.run(ctx)["checks"]]
    assert "Temu 官方接口（temu_open）" in names
