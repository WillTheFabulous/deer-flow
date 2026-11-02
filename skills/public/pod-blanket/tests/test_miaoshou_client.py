"""妙手开放平台客户端：签名、发出去的字节与签名一致、错误分类、节流、翻页。"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from podflow import miaoshou


def test_sign_matches_official_formula() -> None:
    # 文档的公式：HmacSHA256(secret, secret + path + timestamp + key + body + secret)，小写 hex
    secret, path, ts, key = "as_x", "/open/v1/order/create", "1700000000", "ak_1"
    body = '{"orderNo":"ORD2024001","amount":100.00}'
    want = hmac.new(
        secret.encode(), f"{secret}{path}{ts}{key}{body}{secret}".encode(), hashlib.sha256
    ).hexdigest()
    assert miaoshou.sign(secret, path, ts, key, body) == want
    assert want == want.lower()


def test_body_is_compact_json_and_keeps_chinese() -> None:
    assert miaoshou.body_text({"a": 1, "名": "毛毯"}) == '{"a":1,"名":"毛毯"}'
    assert miaoshou.body_text(None) == ""


def test_request_sends_exactly_the_signed_bytes(ctx, fake) -> None:
    with miaoshou.Client(ctx) as api:
        api.call(f"{miaoshou.BOX}/get_item_options", {"标题": "Flannel Blanket 法兰绒"})
    sent = [r for r in fake.requests if r["path"].endswith("/get_item_options")][-1]
    headers = sent["headers"]
    text = sent["body"].decode("utf-8")
    assert text == '{"标题":"Flannel Blanket 法兰绒"}'
    assert headers["x-app-key"] == "ak_test"
    assert headers["x-sign"] == miaoshou.sign(
        "as_test_secret",
        f"{miaoshou.BOX}/get_item_options",
        headers["x-timestamp"],
        "ak_test",
        text,
    )
    assert headers["content-type"] == "application/json"


def test_wrong_secret_is_a_service_error(ctx, fake) -> None:
    fake.miaoshou_secret = "another"
    with miaoshou.Client(ctx) as api, pytest.raises(miaoshou.MiaoshouError) as err:
        api.item_options()
    assert err.value.kind == miaoshou.KIND_SERVICE
    assert err.value.code == "signInvalid"


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("appNoPermission", miaoshou.KIND_SERVICE),
        ("ipNotInWhitelist", miaoshou.KIND_SERVICE),
        ("apiQpsRateLimit", miaoshou.KIND_RATE),
        ("accountQpdRateLimit", miaoshou.KIND_RATE),
        ("upstreamReadTimeout", miaoshou.KIND_TRANSIENT),
        ("systemError", miaoshou.KIND_TRANSIENT),
        ("fail", miaoshou.KIND_INVALID),
    ],
)
def test_error_codes_are_classified(ctx, fake, code, kind) -> None:
    fake.miaoshou_fail["get_shop_list"] = code
    with miaoshou.Client(ctx) as api, pytest.raises(miaoshou.MiaoshouError) as err:
        api.shops()
    assert err.value.kind == kind
    assert err.value.code == code
    # 读接口不会「结果未知」
    assert not err.value.maybe_applied


def test_write_timeout_after_send_is_maybe_applied(ctx, fake) -> None:
    fake.miaoshou_timeout["create_collect_box_item"] = "after"
    with miaoshou.Client(ctx) as api, pytest.raises(miaoshou.MiaoshouError) as err:
        api.create_item({"title": "x"})
    assert err.value.kind == miaoshou.KIND_TRANSIENT
    assert err.value.maybe_applied
    # 妙手其实已经建好了
    assert len(fake.miaoshou_items) == 1


def test_connect_error_is_not_maybe_applied(ctx, fake, monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    from podflow import http

    monkeypatch.setattr(
        http,
        "make_client",
        lambda timeout: httpx.Client(transport=httpx.MockTransport(refuse), timeout=timeout),
    )
    with miaoshou.Client(ctx) as api, pytest.raises(miaoshou.MiaoshouError) as err:
        api.create_item({"title": "x"})
    assert err.value.kind == miaoshou.KIND_TRANSIENT
    assert not err.value.maybe_applied


def test_http_500_without_json_is_transient(ctx, fake, monkeypatch) -> None:
    from podflow import http

    monkeypatch.setattr(
        http,
        "make_client",
        lambda timeout: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")),
            timeout=timeout,
        ),
    )
    with miaoshou.Client(ctx) as api, pytest.raises(miaoshou.MiaoshouError) as err:
        api.publish([1], {"1": ["US"]})
    assert err.value.kind == miaoshou.KIND_TRANSIENT
    assert err.value.status == 502
    assert err.value.maybe_applied


def test_missing_credentials(ctx) -> None:
    ctx.secrets.pop("MIAOSHOU_APP_SECRET")
    with pytest.raises(miaoshou.MiaoshouError) as err:
        miaoshou.Client(ctx)
    assert err.value.kind == miaoshou.KIND_SERVICE
    assert "MIAOSHOU_APP_SECRET" in str(err.value)


def test_pacer_spaces_requests(ctx, fake, monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(miaoshou, "_sleep", waits.append)
    monkeypatch.setattr(miaoshou, "_PACER", miaoshou._Pacer())
    ctx.cfg["miaoshou"]["min_interval_seconds"] = 5
    with miaoshou.Client(ctx) as api:
        api.item_options()
        api.item_options()
    assert len(waits) == 1
    assert 4 < waits[0] <= 5


def test_shops_reads_every_page(ctx, fake) -> None:
    fake.miaoshou_shops = [
        {"shopId": 1000 + i, "shopNick": f"店{i}", "status": "authorized"} for i in range(130)
    ]
    with miaoshou.Client(ctx) as api:
        shops = api.shops()
    assert [s["shopId"] for s in shops] == [1000 + i for i in range(130)]
    pages = [b["pageNo"] for tail, b in fake.miaoshou_calls if tail == "get_shop_list"]
    assert pages == [1, 2]


def test_publish_rejects_empty_and_oversized_batches(ctx, fake) -> None:
    with miaoshou.Client(ctx) as api:
        for ids in ([], list(range(miaoshou.BATCH_MAX + 1))):
            with pytest.raises(miaoshou.MiaoshouError):
                api.publish(ids, {"1": ["US"]})
    assert not [t for t, _ in fake.miaoshou_calls if t == "save_move_collect_task"]


def test_create_then_search_and_publish_roundtrip(ctx, fake) -> None:
    with miaoshou.Client(ctx) as api:
        detail_id = api.create_item(
            {"title": "T", "itemNum": "Deer0001", "collectBoxDetailShopList": [{"shopId": 7}]}
        )
        listed = api.search_box(status=miaoshou.STATUS_NOT_PUBLISHED)
        # 和真接口一样：列表行带编号、标题、店铺，itemNum 是空的（货号只在详情里）
        rows = listed["detailList"]
        assert [(int(r["collectBoxDetailId"]), r["title"], r["itemNum"]) for r in rows] == [(detail_id, "T", None)]
        detail = api.site_detail(detail_id)["siteCollectItemInfo"]
        assert detail["itemNum"] == "Deer0001"
        api.publish([detail_id], {"7": ["US"]})
        published = api.search_box(status=miaoshou.STATUS_PUBLISHED)
    assert [int(r["collectBoxDetailId"]) for r in published["detailList"]] == [detail_id]
    body = next(b for t, b in fake.miaoshou_calls if t == "save_move_collect_task")
    assert json.dumps(body["shopIdAndSitesMap"]) == '{"7": ["US"]}'
