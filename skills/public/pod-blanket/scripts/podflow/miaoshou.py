"""妙手开放平台（`openapi-erp.91miaoshou.com`）的客户端：只管签名、发请求和把错误分类，不碰业务库。

接口说明见 `feature-plans/pod-temu-listing/miaoshou-api-guide.md`（以 Apifox 官方文档为准）。

- 凭证：AppKey / AppSecret 放 secrets.env，名字在 `miaoshou.app_key_name` / `app_secret_name`；
- 只有 POST、`application/json`。签名 = HmacSHA256(secret, secret + 路径 + 秒级时间戳 + key + body + secret)，小写 hex；
  body 是紧凑 JSON 串，签的就是发出去的这串字节——交给 httpx 的 `json=` 再序列化一次，签名就对不上；
- 业务码不是 success 时抛 `MiaoshouError`，`kind` 分四类：
  service（应用 / 签名 / 权限 / 白名单 / 路由：换哪一单都一样，要人去配）、rate（各级限流：等一会儿再试，不算失败）、
  transient（下游超时、系统错误、网络、5xx：下一轮再试）、invalid（这一单的数据问题）。
  写接口（建箱、保存、发布、删除）请求已经发出去才超时的，`maybe_applied` 为真：结果未知，要先对账再重试。
- 进程内节流：两次请求至少隔 `miaoshou.min_interval_seconds`（缺省 1 秒）；限流额度摸清前保守些。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Any

import httpx

from . import http
from .context import Ctx

DEFAULT_BASE_URL = "https://openapi-erp.91miaoshou.com"
DEFAULT_KEY_NAME = "MIAOSHOU_APP_KEY"
DEFAULT_SECRET_NAME = "MIAOSHOU_APP_SECRET"
PLATFORM = "pddkjChoice"
SITE = "PDDKJCHOICE"
SHOP_PATH = "/open/v1/product/shop/shop/get_shop_list"
BOX = "/open/v1/product/collect_box/pddkj_choice/collect_box"
PUBLISH_PATH = "/open/v1/product/collect_box/pddkj_choice/move_collect/save_move_collect_task"
# 采集箱状态（search_collect_box_detail_list 的 filter.status）
STATUS_NOT_PUBLISHED = "notPublished"
STATUS_TIMING = "timingPublish"
STATUS_PUBLISHED = "published"
SEARCH_PAGE_MAX = 500
SHOP_PAGE_MAX = 100
BATCH_MAX = 200

SERVICE_CODES = frozenset(
    {
        "appNotFound",
        "signMissing",
        "signExpired",
        "signInvalid",
        "appNoPermission",
        "ipNotInWhitelist",
        "routeNotFound",
    }
)
TRANSIENT_CODES = frozenset(
    {
        "upstreamError",
        "upstreamConnectTimeout",
        "upstreamReadTimeout",
        "upstreamDnsError",
        "upstreamReset",
        "systemError",
        "redisConnectionError",
        "redisTimeout",
    }
)
KIND_SERVICE = "service"
KIND_RATE = "rate"
KIND_TRANSIENT = "transient"
KIND_INVALID = "invalid"


class MiaoshouError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        code: str | None = None,
        status: int | None = None,
        maybe_applied: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.status = status
        # 写接口发出去之后才断开 / 超时：妙手可能已经做了，重试前要先对账
        self.maybe_applied = maybe_applied


def kind_of(code: str | None) -> str:
    """妙手的错误码归到哪一类。"""
    if not code:
        return KIND_INVALID
    if code in SERVICE_CODES:
        return KIND_SERVICE
    if code.endswith("RateLimit"):
        return KIND_RATE
    if code in TRANSIENT_CODES:
        return KIND_TRANSIENT
    return KIND_INVALID


def body_text(body: dict[str, Any] | None) -> str:
    """要发出去的 body：紧凑 JSON（不转义中文）；没有 body 是空串。"""
    if body is None:
        return ""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _shop_id(value: int | str) -> int | str:
    """店铺模式接口的 shopId 是 integer：数字串转成数，别的原样（不认识的交给妙手报错）。"""
    text = str(value).strip()
    return int(text) if text.isdigit() else text


def sign(secret: str, path: str, timestamp: str, key: str, body: str) -> str:
    """HmacSHA256(secret, secret + path + timestamp + key + body + secret)，小写 hex。"""
    content = f"{secret}{path}{timestamp}{key}{body}{secret}"
    return hmac.new(
        secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class _Pacer:
    """进程内所有客户端共用：两次请求之间至少隔 interval 秒。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, interval: float) -> None:
        if interval <= 0:
            return
        with self._lock:
            delay = self._last + interval - time.monotonic()
            if delay > 0:
                _sleep(delay)
            self._last = time.monotonic()


_PACER = _Pacer()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> int:
    return int(time.time())


class Client:
    def __init__(self, ctx: Ctx) -> None:
        self.base_url = str(
            ctx.setting("miaoshou.base_url") or DEFAULT_BASE_URL
        ).rstrip("/")
        key_name = str(ctx.setting("miaoshou.app_key_name") or DEFAULT_KEY_NAME)
        secret_name = str(
            ctx.setting("miaoshou.app_secret_name") or DEFAULT_SECRET_NAME
        )
        self.app_key = ctx.secrets.get(key_name) or ""
        self._secret = ctx.secrets.get(secret_name) or ""
        if not self.app_key or not self._secret:
            raise MiaoshouError(
                f"secrets.env 里缺 {key_name} / {secret_name}", kind=KIND_SERVICE
            )
        self.interval = float(ctx.setting("miaoshou.min_interval_seconds", 1.0))
        self._http = http.make_client(float(ctx.setting("miaoshou.timeout_seconds") or 60))

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def call(
        self, path: str, body: dict[str, Any] | None = None, *, write: bool = False
    ) -> Any:
        """调一个接口，返回 data；失败抛 MiaoshouError。write：会改妙手那边数据的接口（超时要对账）。"""
        text = body_text(body)
        _PACER.wait(self.interval)
        timestamp = str(_now())
        headers = {
            "x-app-key": self.app_key,
            "x-timestamp": timestamp,
            "x-sign": sign(self._secret, path, timestamp, self.app_key, text),
            "Content-Type": "application/json",
        }
        try:
            resp = self._http.post(
                self.base_url + path, content=text.encode("utf-8"), headers=headers
            )
        except httpx.TransportError as exc:
            sent = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
            raise MiaoshouError(
                f"连不上妙手（{path}）：{type(exc).__name__}: {exc}",
                kind=KIND_TRANSIENT,
                maybe_applied=write and sent,
            ) from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise MiaoshouError(
                f"妙手返回 HTTP {resp.status_code}（{path}）：{resp.text[:200]}",
                kind=KIND_TRANSIENT if resp.status_code >= 500 else KIND_INVALID,
                status=resp.status_code,
                maybe_applied=write and resp.status_code >= 500,
            )
        code = str(data.get("code") or "")
        if data.get("result") == "success" or code == "success":
            return data.get("data")
        kind = kind_of(code)
        raise MiaoshouError(
            f"妙手 {path.rsplit('/', 1)[-1]} 失败：{code or resp.status_code} {data.get('message') or ''}".strip(),
            kind=kind if code else (KIND_TRANSIENT if resp.status_code >= 500 else KIND_INVALID),
            code=code or None,
            status=resp.status_code,
            maybe_applied=write and kind == KIND_TRANSIENT,
        )

    # ------------------------------------------------------------ 店铺与选项（只读）

    def shops(self) -> list[dict[str, Any]]:
        """当前应用能用的 Temu 半托店铺（翻完所有页）。"""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.call(
                SHOP_PATH,
                {
                    "platform": PLATFORM,
                    "site": SITE,
                    "pageNo": page,
                    "pageSize": SHOP_PAGE_MAX,
                },
            ) or {}
            items = data.get("shopList") or []
            out += items
            if len(items) < SHOP_PAGE_MAX:
                return out
            page += 1

    def site_options(self) -> dict[str, Any]:
        return self.call(f"{BOX}/get_publish_site_options", {}) or {}

    def item_options(self) -> dict[str, Any]:
        return self.call(f"{BOX}/get_item_options", {}) or {}

    def category_tree(self) -> Any:
        return self.call(f"{BOX}/get_category_tree_by_site", {})

    def category_rules(self, cid: int | str) -> dict[str, Any]:
        return self.call(f"{BOX}/get_category_attribute_rules", {"cid": int(cid)}) or {}

    def shop_warehouses(self, shop_id: Any, *, sync: bool = False) -> dict[str, Any]:
        return self.call(
            f"{BOX}/get_shop_warehouse", {"shopId": shop_id, "isRsync": int(sync)}
        ) or {}

    def freight_templates(self, shop_id: Any, *, sync: bool = False) -> dict[str, Any]:
        return self.call(
            f"{BOX}/get_shop_freight_template",
            {"shopId": shop_id, "isRsync": int(sync)},
        ) or {}

    # ------------------------------------------------------------ 采集箱

    def site_detail(self, detail_id: int | str) -> dict[str, Any]:
        """采集箱站点模式详情（样板商品就从这里读）：{editModel, claimToShopIds, siteCollectItemInfo}。"""
        return self.call(
            f"{BOX}/get_site_collect_item_info", {"detailId": int(detail_id)}
        ) or {}

    def shop_detail(self, detail_id: int | str, shop_id: int | str) -> dict[str, Any]:
        """采集箱店铺模式详情：{editModel: "shop", claimToShopIds, shopCollectItemInfo}。
        品牌（brandId）只在这里，站点模式详情和建箱接口里都没有。"""
        return self.call(
            f"{BOX}/get_shop_collect_item_info",
            {"detailId": int(detail_id), "shopId": _shop_id(shop_id)},
        ) or {}

    def save_shop_detail(
        self, detail_id: int | str, shop_id: int | str, info: dict[str, Any]
    ) -> Any:
        """保存店铺模式详情：整份 shopCollectItemInfo（读出来改了再存）。"""
        return self.call(
            f"{BOX}/save_shop_collect_item_info",
            {
                "detailId": int(detail_id),
                "shopId": _shop_id(shop_id),
                "shopCollectItemInfo": info,
            },
            write=True,
        )

    def search_box(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        size: int = SEARCH_PAGE_MAX,
        source_keyword: str | None = None,
    ) -> Any:
        """一页采集箱列表（返回原样的 data，结构以实测为准）。"""
        flt: dict[str, Any] = {}
        if status:
            flt["status"] = status
        if source_keyword:
            flt["sourceItemIdKeyword"] = source_keyword
        body: dict[str, Any] = {"pageNo": page, "pageSize": min(size, SEARCH_PAGE_MAX)}
        if flt:
            body["filter"] = flt
        return self.call(f"{BOX}/search_collect_box_detail_list", body)

    def create_item(self, info: dict[str, Any]) -> int:
        """按站点模式建一个采集箱商品，返回 collectBoxDetailId。"""
        data = self.call(
            f"{BOX}/create_collect_box_item", {"siteCollectItemInfo": info}, write=True
        ) or {}
        detail_id = data.get("collectBoxDetailId")
        if detail_id in (None, ""):
            raise MiaoshouError(
                "妙手建箱成功但没给 collectBoxDetailId", kind=KIND_INVALID
            )
        return int(detail_id)

    def save_site_detail(self, detail_id: int | str, info: dict[str, Any]) -> Any:
        return self.call(
            f"{BOX}/save_site_collect_item_info",
            {"detailId": int(detail_id), "siteCollectItemInfo": info},
            write=True,
        )

    def publish(self, detail_ids: list[int], shop_sites: dict[str, list[str]]) -> Any:
        """把这些采集箱商品发布到 shop_sites（{shopId: [站点…]}）；异步，结果看采集箱状态。"""
        if not detail_ids or len(detail_ids) > BATCH_MAX:
            raise MiaoshouError(
                f"一次发布 1–{BATCH_MAX} 个（这次 {len(detail_ids)} 个）",
                kind=KIND_INVALID,
            )
        return self.call(
            PUBLISH_PATH,
            {"detailIds": [int(d) for d in detail_ids], "shopIdAndSitesMap": shop_sites},
            write=True,
        )

    def delete_items(self, detail_ids: list[int]) -> dict[str, Any]:
        """删掉还没发布的采集箱商品：{successNum, failNum, errorMap, errorMsg}。"""
        return self.call(
            f"{BOX}/delete_collect_box_detail",
            {"detailIds": [int(d) for d in detail_ids]},
            write=True,
        ) or {}
