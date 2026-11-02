"""Temu 半托上架（2026-10-28，`temu.*`）：人在「选中的款」勾「确认上架」，后台以每家店的样板商品为底建进妙手采集箱，
发布到选款时定的店铺。

- 每个（款, 店）一行 store_listings（stores.py 建的），状态：
  chosen（选了店）→ queued（勾了确认上架）→ creating → in_box（拿到 collectBoxDetailId）→ publishing → listed；
  建箱超时 / 进程中途没了 → create_unknown（下一轮在采集箱里按标题 + 店铺找候选、读详情核对货号，找到就收下，
  过了 RECONCILE_AFTER_SECONDS 找不到才重建；列表接口不给货号）；
  妙手业务错误、定制款、样板不能用 → failed（「确认上架」的勾由程序取消，人再勾一次 / `temu retry` 重来；已有 detailId 的改成保存）。
  素材还没好（套图没出完、旧租户出的套图、图位对不上、还没领到货号、标题还在写）不算失败：排着队等，原因写在「上架备注」。
- 样板：每家店一个人在妙手里手工建好、不发布的商品（`temu.stores[].template_detail_id`），`get_site_collect_item_info` 读出来，
  每款只换标题、轮播图（10 张，temu_assets 的上架顺序）、SKU 预览图（样板里每个尺码用轮播第几张，就换成这款同一位置的图）、
  主货号和每个 SKU 的货号（都是货号，和 2店一致）；`collectBoxDetailShopList` 只留这家店那一条；详情图按样板（样板没有就没有）。
- 品牌：克隆带不进去（品牌只在店铺模式详情的 brandId 里，站点模式建箱没有这个字段）。配了 `temu.stores[].brand_id` 的店，
  进箱后 `_ensure_brand` 读店铺模式详情：已经是这个品牌（人在妙手里选过）只记一笔，不是就只改 brandId、整份存回去再读回核对，
  记在 temu_brands。没写上的在备注里写原因、auto 不发布，每轮补写，满 BRAND_MAX_ATTEMPTS 次停（`temu retry` 清零）。
- 发布 `temu.publish`：auto = 进箱后按店分组批量发布；box = 只进采集箱，人在妙手点发布。只认采集箱状态 published 为已上架；
  auto 发布后超过 `temu.publish_timeout_minutes` 还没发布记 failed，但接着盯 `temu.watch_days` 天（人在妙手修好重发也能自动标已上架）。
  查发布结果每 `temu.poll_minutes` 分钟一次。
- 已上架：store_listings 改 listed，写进业务库 listed_products（listed_table.py 和飞书「已上架的款」双向同步）、
  上架状态 listing_marks；这一款所有店都上了就自动勾「下载确认」（source listing，交付包与归档照旧接上）。
- 「选中的款」里程序写：店铺、货号、上架标题、上架状态（待上架 / 上架中 / 已上架 / 上架失败，按这一款所有店铺汇总；
  进了妙手（建箱起，MIAOSHOU_STATES）才算上架中，等套图、准备上架图、排队都是待上架）、上架备注（逐店）、
  上架日期、确认上架（在上 / 上过的勾着，没上成的取消）。只写和上次写的不一样的格子：人改过的「上架标题」在程序的标题没变时不会被盖掉；
  勾也只在要改时写（和下载确认一样），刚勾上、还没处理的勾不会被取消。人改的标题在勾「确认上架」时读回来，按同一套规则复核。
- 只有一个后台进程推进（租约 temu_publisher）；鉴权 / 权限 / 白名单错误记下来退避 SERVICE_BACKOFF_MINUTES 分钟，限流 / 下游错误这一轮停下。
- 全自动（2026-10-30，`temu.auto.*`，feature-plans/pod-listing-autopilot）：开着的店（`temu.auto.stores`，空 = 全部）
  记下店铺就由程序确认上架（确认人 system:temu，「确认上架」由程序勾上），进箱、写品牌后自动发布（不看 `temu.publish`）。
  人取消「确认上架」= 暂停这一款（listing_holds，程序不再替他确认），再勾就接着上；建箱以后取消不了，勾写回去。
  没配品牌的店全自动不发布（doctor 报错）。发布请求结果未知（超时）记 publish_unknown：等妙手的结果，过了
  PUBLISH_UNKNOWN_MINUTES 还没发布就重发一次，不当场重发（防重复上架）。品牌写满次数后隔 `temu.brand_recheck_hours`
  只读复查一次。在采集箱 / 还在盯的款图片链接剩不到 `temu.images.link_days - temu.link_resave_days` 天时重签存回妙手。
- 留痕：每次状态迁移记 trace（stage = listing，带确认人 / 来源 / 原因）；每（款, 店）的当前环节由 trace.refresh 算，
  「选中的款」另写「当前环节」「环节开始」「妙手 ID」「标题同步」四列（表格信息 extra_fields 里有了才写）。
- 标题同步：人在表里改了上架标题（cells.py 读回），合规的按环节生效：还没建箱的建箱时用，在采集箱的存回妙手（站点模式
  只改标题、读回核对，再核对品牌），发布中的等结果，已上架的走 Temu 官方接口（temu_open，没接上时记「要人去 Temu 改」）。
"""

from __future__ import annotations

import copy
import datetime as dt
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import (
    bg,
    downloads,
    feishu,
    feishu_table,
    labels,
    lease,
    listing,
    miaoshou,
    mooya,
    store,
    stores,
    table_registry,
    temu_assets,
    temu_title,
    trace,
)
from .config import ConfigError
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso, parse_iso, utcnow

LEASE_KEY = "temu_publisher"
LEASE_SECONDS = 300
# 写「选中的款」上架几列的租约（事件、巡检、后台、表格同步几个进程都会写）
WRITE_LEASE = "temu.writer"
WRITE_SECONDS = 120
WAIT_SECONDS = 45
SERVICE_META_KEY = "temu.service_error"
CHECKED_META_KEY = "temu.checked_at"
TEMPLATE_META_PREFIX = "temu.template."
# 「选中的款」补过的选项（上架状态的上架中 / 上架失败、店铺列的店铺）和列说明记在表格信息的这个键里
OPTIONS_KEY = "temu_options"
TEMPLATE_HOURS = 6
SERVICE_BACKOFF_MINUTES = 30
RECONCILE_AFTER_SECONDS = 120
# 写品牌连续失败这么多次不再自动试（`temu retry` 清零）
BRAND_MAX_ATTEMPTS = 6
MODE_AUTO = "auto"
MODE_BOX = "box"
BY_AUTO = "system:temu"
# 全自动时程序自己确认上架的来源（confirm 的 source）
SOURCE_AUTO = "auto"
_PAGE = 500

STATE_CHOSEN = stores.STATE_CHOSEN
STATE_QUEUED = "queued"
STATE_CREATING = "creating"
STATE_UNKNOWN = "create_unknown"
STATE_BOX = "in_box"
STATE_PUBLISHING = "publishing"
# 发布请求发出去之后超时 / 断开：妙手可能已经在发布了，不当场重发（2026-10-30）
STATE_PUBLISH_UNKNOWN = "publish_unknown"
STATE_LISTED = "listed"
STATE_FAILED = "failed"
STATE_CANCELLED = stores.STATE_CANCELLED
ACTIVE_STATES = (
    STATE_QUEUED,
    STATE_CREATING,
    STATE_UNKNOWN,
    STATE_BOX,
    STATE_PUBLISHING,
    STATE_PUBLISH_UNKNOWN,
)
# 已经进了妙手（从建箱起）：「上架状态」从这里起才写上架中，排队、等套图、准备上架图都还是待上架（2026-10-30 用户定）
MIAOSHOU_STATES = (
    STATE_CREATING,
    STATE_UNKNOWN,
    STATE_BOX,
    STATE_PUBLISHING,
    STATE_PUBLISH_UNKNOWN,
)
STATE_TEXT = {
    STATE_CHOSEN: "待上架",
    STATE_QUEUED: "排队中",
    STATE_CREATING: "建箱中",
    STATE_UNKNOWN: "建箱结果未知，核对中",
    STATE_BOX: "已进妙手采集箱",
    STATE_PUBLISHING: "发布中",
    STATE_PUBLISH_UNKNOWN: "发布结果未知，等妙手",
    STATE_LISTED: "已提交 Temu",
    STATE_FAILED: "没上成",
}
ERROR_REJECTED = "rejected"
# 发布结果未知的过这么久还没发布就重发一次；重发过这么多次还不行记没上成
PUBLISH_UNKNOWN_MINUTES = 30
PUBLISH_RESENDS = 1
# 「选中的款」全自动时加的四列（表格信息 extra_fields 里有了才写）
PIPELINE_FIELD_NAMES = feishu_table.TEMU_PIPELINE_FIELD_NAMES
PIPELINE_FIELD, PIPELINE_SINCE_FIELD, DETAIL_FIELD, TITLE_SYNC_FIELD = PIPELINE_FIELD_NAMES
PIPELINE_HELP = "程序写：这一款现在在流水线哪一环（几家店时写最落后、或要人看的那一家），逐店的情况看「上架备注」。"
PIPELINE_SINCE_HELP = "程序写：进到「当前环节」的时间，卡得久了会进「POD 待办」。"
DETAIL_HELP = "程序写：妙手采集箱里的商品 ID（每家店一个），去妙手里找这一款用它。"
TITLE_SYNC_HELP = "程序写：改了「上架标题」之后同步到哪了（已生效 / 已存进妙手采集箱 / 已提交 Temu 审核…），不合规写原因。"

STORE_FIELD = feishu_table.TEMU_STORE_FIELD
CODE_FIELD = feishu_table.TEMU_CODE_FIELD
TITLE_FIELD = feishu_table.TEMU_TITLE_FIELD
CONFIRM_FIELD = feishu_table.TEMU_CONFIRM_FIELD
STATE_FIELD = feishu_table.LISTING_STATE_FIELD
NOTE_FIELD = feishu_table.LISTING_NOTE_FIELD
DATE_FIELD = feishu_table.LISTING_DATE_FIELD
CONFIRM_HELP = (
    "看过上架标题后打勾：后台按选款时定的店铺建进妙手采集箱并发布到 Temu（套图还没出完也可以先勾，素材好了自动接着上）。"
    "没上成的勾会被取消、原因写在「上架备注」，改好再勾一次就是重试；定制款不会自动上。"
)
TITLE_HELP = (
    "程序看上架主图按 Temu 规则写好；可以直接改，勾「确认上架」时按同一套规则复核，不合规不上架、原因写在「上架备注」。"
)
STORE_HELP = "程序写：选款时在候选表勾的店铺。要改店铺在 POD 对话里说（已经在上架的店去不掉）。"
CODE_HELP = "程序写：mooya 按店铺前缀续的货号（如 FLow0006），也是妙手里的主货号和 SKU 货号、mooya 交付包的名字。"
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)


def enabled(ctx: Ctx) -> bool:
    return stores.enabled(ctx)


def mode(ctx: Ctx) -> str:
    """发布方式 temu.publish：auto / box；写错了按 box（只进采集箱，不会误发），doctor 报出来。"""
    value = str(ctx.setting("temu.publish", MODE_BOX) or MODE_BOX)
    return value if value in (MODE_AUTO, MODE_BOX) else MODE_BOX


# ---------------------------------------------------------------- 全自动（temu.auto.*）


def auto_stores(ctx: Ctx) -> set[str]:
    """开着全自动的店铺 key（temu.auto.enabled 关着时为空；temu.auto.stores 空 = 全部店）。"""
    if not enabled(ctx) or not ctx.setting("temu.auto.enabled", False):
        return set()
    try:
        configured = {s.key for s in stores.all_stores(ctx)}
    except ConfigError:
        return set()
    picked = {str(k) for k in ctx.setting("temu.auto.stores") or []}
    return configured & picked if picked else configured


def auto_on(ctx: Ctx, store_key: str | None = None) -> bool:
    """这家店（不给 = 任何一家）开着全自动。"""
    keys = auto_stores(ctx)
    return bool(keys) if store_key is None else store_key in keys


def auto_confirm_on(ctx: Ctx, store_key: str) -> bool:
    return auto_on(ctx, store_key) and bool(ctx.setting("temu.auto.confirm", True))


def mode_for(ctx: Ctx, store_key: str) -> str:
    """这家店的发布方式：全自动且 temu.auto.publish 开着 = auto，否则看 temu.publish。"""
    if auto_on(ctx, store_key) and ctx.setting("temu.auto.publish", True):
        return MODE_AUTO
    return mode(ctx)


def _mooya_engine(ctx: Ctx) -> bool:
    """上架交给 mooya（temu.engine: mooya，mooya_listing.py）：建箱、品牌、发布都在 mooya，这里只跟进。"""
    from . import mooya_listing  # mooya 模式的模块依赖本模块，用时再导入

    return mooya_listing.enabled(ctx)


def publish_wait_reason(ctx: Ctx, row: dict[str, Any]) -> str | None:
    """进了采集箱、品牌也好了，还不发布的原因；马上会发布为 None。"""
    if _mooya_engine(ctx):
        from . import mooya_listing  # 同上

        return mooya_listing.wait_reason(ctx, row)
    if mode_for(ctx, row["store_key"]) == MODE_BOX:
        return "等人在妙手里检查后点发布"
    if held(ctx, row["asset_id"]):
        return "已暂停（人取消了「确认上架」），再勾「确认上架」接着发布"
    try:
        store_ = stores.get(ctx, row["store_key"])
    except StageError:
        return None
    if auto_on(ctx, row["store_key"]) and not store_.brand_id:
        return "这家店没配品牌（temu.stores 的 brand_id），全自动不发布"
    return None


def held(ctx: Ctx, asset_id: str) -> bool:
    return bool(ctx.db.scalar("SELECT 1 FROM listing_holds WHERE asset_id = ?", (asset_id,)))


def hold(ctx: Ctx, asset_ids: list[str], *, by: str, source: str, note: str | None = None) -> list[str]:
    """暂停这几款（人取消了「确认上架」）：全自动不再替他确认；已经暂停的不动。返回这次暂停的。"""
    done = []
    now = now_iso()
    with ctx.db.tx() as conn:
        for asset_id in dict.fromkeys(asset_ids):
            if conn.execute("SELECT 1 FROM listing_holds WHERE asset_id = ?", (asset_id,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO listing_holds(asset_id, held_by, held_at, source, note) VALUES(?, ?, ?, ?, ?)",
                (asset_id, by, now, source, note),
            )
            trace.record(conn, asset_id, "hold", to="held", actor=by, source=source, reason=note)
            done.append(asset_id)
    return done


def release(ctx: Ctx, asset_ids: list[str], *, by: str, source: str) -> list[str]:
    """恢复这几款（人又勾上了「确认上架」）。返回这次恢复的。"""
    done = []
    with ctx.db.tx() as conn:
        for asset_id in dict.fromkeys(asset_ids):
            if not conn.execute("SELECT 1 FROM listing_holds WHERE asset_id = ?", (asset_id,)).fetchone():
                continue
            conn.execute("DELETE FROM listing_holds WHERE asset_id = ?", (asset_id,))
            trace.record(conn, asset_id, "hold", to="released", actor=by, source=source)
            done.append(asset_id)
    return done


def _title_limit(ctx: Ctx) -> int | None:
    value = ctx.setting("temu.title_limit")
    return int(value) if value else None


def fields(ctx: Ctx) -> list[dict[str, Any]]:
    """「选中的款」加的列：店铺、货号、上架标题、确认上架，2026-10-30 起再加当前环节、环节开始、妙手 ID、标题同步。"""
    return [
        _store_spec(ctx),
        {"field_name": CODE_FIELD, "type": 1, "description": {"disable_sync": True, "text": CODE_HELP}},
        {"field_name": TITLE_FIELD, "type": 1, "description": {"disable_sync": True, "text": TITLE_HELP}},
        {"field_name": CONFIRM_FIELD, "type": 7, "description": {"disable_sync": True, "text": CONFIRM_HELP}},
        *pipeline_fields(),
    ]


def pipeline_fields() -> list[dict[str, Any]]:
    return [
        _pipeline_spec(),
        {
            "field_name": PIPELINE_SINCE_FIELD,
            "type": 5,
            "property": {"date_formatter": "yyyy/MM/dd HH:mm"},
            "description": {"disable_sync": True, "text": PIPELINE_SINCE_HELP},
        },
        {"field_name": DETAIL_FIELD, "type": 1, "description": {"disable_sync": True, "text": DETAIL_HELP}},
        {"field_name": TITLE_SYNC_FIELD, "type": 1, "description": {"disable_sync": True, "text": TITLE_SYNC_HELP}},
    ]


def _pipeline_spec() -> dict[str, Any]:
    return {
        "field_name": PIPELINE_FIELD,
        "type": 3,
        "property": {"options": [{"name": label} for _key, label in trace.STAGES]},
        "description": {"disable_sync": True, "text": PIPELINE_HELP},
    }


def pipeline_ready(info: dict[str, Any] | None) -> bool:
    """「选中的款」已经有当前环节等四列（之前建的表在表格同步时补上）。"""
    return set(PIPELINE_FIELD_NAMES) <= set((info or {}).get("extra_fields") or [])


def _store_spec(ctx: Ctx) -> dict[str, Any]:
    return {
        "field_name": STORE_FIELD,
        "type": 4,
        "property": {"options": [{"name": s.column} for s in stores.all_stores(ctx)]},
        "description": {"disable_sync": True, "text": STORE_HELP},
    }


def _state_spec() -> dict[str, Any]:
    return feishu_table.listing_state_spec(True)


def ready(info: dict[str, Any] | None) -> bool:
    """「选中的款」已经有店铺 / 货号 / 上架标题 / 确认上架四列，上架状态也补上了新选项（当前环节等四列另看 pipeline_ready）。"""
    info = info or {}
    return set(feishu_table.TEMU_FIELD_NAMES) <= set(info.get("extra_fields") or []) and bool(
        info.get(OPTIONS_KEY)
    )


# ---------------------------------------------------------------- 服务错误与样板


def _service_error(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(SERVICE_META_KEY), None) or {}


def _record_service_error(ctx: Ctx, exc: BaseException) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, SERVICE_META_KEY, dumps({"error": str(exc)[:300], "at": now_iso()}))


def _clear_service_error(ctx: Ctx) -> None:
    if _service_error(ctx):
        with ctx.db.tx() as conn:
            ctx.db.del_meta(conn, SERVICE_META_KEY)


def _backing_off(ctx: Ctx) -> bool:
    at = parse_iso(_service_error(ctx).get("at"))
    return bool(at and utcnow() - at < dt.timedelta(minutes=SERVICE_BACKOFF_MINUTES))


def cached_template(ctx: Ctx, store_: stores.Store) -> dict[str, Any]:
    """上次读到的样板（没读过为空）：写备注、记已上架时用，不调妙手。"""
    cached = loads(ctx.db.get_meta(TEMPLATE_META_PREFIX + store_.key), None) or {}
    return cached.get("info") or {} if cached.get("detail_id") == store_.template_detail_id else {}


def template(
    ctx: Ctx, api: miaoshou.Client, store_: stores.Store, *, refresh: bool = False
) -> dict[str, Any]:
    """这家店的样板商品（站点模式详情里的 siteCollectItemInfo），缓存 TEMPLATE_HOURS 小时；样板编号改了就重读。
    样板不能用抛 StageError，妙手出错抛 MiaoshouError。"""
    if not store_.template_detail_id:
        raise StageError(f"{store_.column} 还没配样板商品（temu.stores 的 template_detail_id）")
    key = TEMPLATE_META_PREFIX + store_.key
    cached = loads(ctx.db.get_meta(key), None) or {}
    fetched = parse_iso(cached.get("fetched_at"))
    if (
        not refresh
        and cached.get("detail_id") == store_.template_detail_id
        and fetched
        and utcnow() - fetched < dt.timedelta(hours=TEMPLATE_HOURS)
    ):
        return cached["info"]
    data = api.site_detail(store_.template_detail_id) or {}
    info = data.get("siteCollectItemInfo") or {}
    problems = template_problems(store_, info)
    if problems:
        raise StageError(f"{store_.column} 的样板不能用：" + "；".join(problems))
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn,
            key,
            dumps({"detail_id": store_.template_detail_id, "fetched_at": now_iso(), "info": info}),
        )
    return info


def template_problems(store_: stores.Store, info: dict[str, Any]) -> list[str]:
    problems = []
    if not info.get("cid"):
        problems.append("没有类目 cid")
    if not info.get("skuMap"):
        problems.append("没有 SKU")
    if not info.get("imgUrls"):
        problems.append("没有轮播图（拿不到 SKU 预览图用的是第几张）")
    if not info.get("publishSiteList"):
        problems.append("没有发布站点")
    if _shop_entry(store_, info) is None:
        shops = [str(s.get("shopId")) for s in info.get("collectBoxDetailShopList") or []]
        problems.append(
            f"样板的预发布店铺 {shops} 里没有这家店（temu.stores 的 shop_id = {store_.shop_id or '空'}）"
        )
    return problems


def _shop_entry(store_: stores.Store, info: dict[str, Any]) -> dict[str, Any] | None:
    shops = info.get("collectBoxDetailShopList") or []
    if store_.shop_id:
        return next((s for s in shops if str(s.get("shopId")) == store_.shop_id), None)
    return shops[0] if len(shops) == 1 else None


# 创建接口认的 siteCollectItemInfo 字段（Apifox 文档）；样板详情里的其余键（detailId / editModel / site / shopId …）是只读的，不带
CREATE_KEYS = frozenset(
    {
        "collectBoxDetailShopList",
        "cid",
        "title",
        "multiLanguageTitleMap",
        "translateLanguages",
        "itemNum",
        "productOriginCountry",
        "productOriginProvince",
        "productOriginCertFiles",
        "imgUrls",
        "multiLanguageImgUrlsMap",
        "shopIdAndGoodsModelReqMap",
        "productMaterialImageUrl",
        "mainImgVideoUrl",
        "goodsLayerDecorationReqs",
        "productGuideFileUrl",
        "productGuideFileName",
        "outerGoodsUrl",
        "collectShowSizeTemplateIds",
        "sizeAttributeValueGroupId",
        "sizeCharts",
        "vehicleLibraryRelationList",
        "attributes",
        "saleAttributes",
        "skuMap",
        "publishSiteList",
        "publishEuSiteList",
        "siteAndSkuPriceMap",
        "siteShipmentLimitDayMap",
        "materialMultiLanguageType",
        "materialMultiLanguages",
        "discreetShipping",
        "currency",
        "shipmentLimitDay",
        "sizeTemplateId",
        "personalizationSwitch",
        "manufacturingLocationRegionShortNameList",
    }
)


def build_payload(
    store_: stores.Store,
    tpl: dict[str, Any],
    *,
    code: str,
    title: str,
    images: list[str],
) -> dict[str, Any]:
    """样板 → 这一款要建的 siteCollectItemInfo：只换标题、图、货号，`collectBoxDetailShopList` 只留这家店。"""
    if not images:
        raise StageError("没有上架图")
    info = {k: copy.deepcopy(v) for k, v in tpl.items() if k in CREATE_KEYS and v is not None}
    tpl_urls = list(tpl.get("imgUrls") or [])

    def same_slot(url: str | None, fallback: str | None) -> str | None:
        """样板里用轮播第 n 张的地方换成这款的第 n 张；不是轮播图的给 fallback。"""
        if url in tpl_urls:
            return images[min(tpl_urls.index(url), len(images) - 1)]
        return fallback

    entry = copy.deepcopy(_shop_entry(store_, tpl) or {})
    entry["itemNum"] = code
    entry.pop("shopName", None)
    info["collectBoxDetailShopList"] = [entry]
    info["title"] = title
    info["multiLanguageTitleMap"] = {"en": title}
    info["itemNum"] = code
    info["imgUrls"] = list(images)
    if tpl.get("multiLanguageImgUrlsMap"):
        info["multiLanguageImgUrlsMap"] = {lang: list(images) for lang in tpl["multiLanguageImgUrlsMap"]}
    # 详情图模块：用的是轮播图的换成同一位置，别的（通用说明图）照样板
    for layer in info.get("goodsLayerDecorationReqs") or []:
        content = layer.get("content")
        if isinstance(content, dict) and content.get("imgUrl"):
            content["imgUrl"] = same_slot(content["imgUrl"], content["imgUrl"])
    for sku in (info.get("skuMap") or {}).values():
        sku["itemNum"] = code
        if sku.get("imgUrl"):
            sku["imgUrl"] = same_slot(sku["imgUrl"], images[0])
        if isinstance(sku.get("multiLanguageImgUrlMap"), dict):
            sku["multiLanguageImgUrlMap"] = {
                lang: same_slot(url, images[0]) for lang, url in sku["multiLanguageImgUrlMap"].items()
            }
    for attr in info.get("saleAttributes") or []:
        for value in attr.get("values") or []:
            if value.get("imgUrls"):
                value["imgUrls"] = [same_slot(u, images[0]) for u in value["imgUrls"]]
    return info


def shop_sites(store_: stores.Store, tpl: dict[str, Any]) -> dict[str, list[str]]:
    """发布时的 shopIdAndSitesMap：{这家店的 shopId: 样板里的发布站点}。"""
    entry = _shop_entry(store_, tpl) or {}
    shop = str(entry.get("shopId") or store_.shop_id)
    return {shop: list(entry.get("publishSiteList") or tpl.get("publishSiteList") or [])}


def prices(tpl: dict[str, Any]) -> list[dict[str, Any]]:
    """样板里每个 SKU 的尺码与申报价（记进「已上架的款」）。"""
    names: dict[str, str] = {}
    for attr in tpl.get("saleAttributes") or []:
        for value in attr.get("values") or []:
            if value.get("skuKey"):
                names[str(value["skuKey"])] = value.get("name") or str(value["skuKey"])
    out = []
    for key, sku in (tpl.get("skuMap") or {}).items():
        parts = [names.get(p, p) for p in str(key).strip(";").split(";") if p]
        out.append({"sku": " / ".join(parts), "price": sku.get("price"), "currency": tpl.get("currency")})
    return out


# ---------------------------------------------------------------- 行与状态


def _rows(
    ctx: Ctx, states: tuple[str, ...] | None = None, asset_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    return [r for r in stores.rows(ctx, asset_ids=asset_ids) if states is None or r["state"] in states]


def _update(
    ctx: Ctx,
    row: dict[str, Any],
    *,
    _actor: str | None = None,
    _source: str | None = None,
    _reason: str | None = None,
    **values: Any,
) -> None:
    """改一（款, 店）的上架记录；状态变了记一条轨迹（带是谁、从哪来、为什么）。"""
    values["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in values)
    before = row.get("state")
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE store_listings SET {cols} WHERE asset_id = ? AND store_key = ?",
            (*values.values(), row["asset_id"], row["store_key"]),
        )
        if "state" in values and values["state"] != before:
            trace.record(
                conn,
                row["asset_id"],
                "listing",
                frm=before,
                to=values["state"],
                store=row["store_key"],
                code=row.get("code"),
                actor=_actor,
                source=_source,
                reason=_reason or (values.get("last_error") if values["state"] == STATE_FAILED else None),
                payload={k: values[k] for k in ("detail_id", "error_kind") if values.get(k)} or None,
            )
    row.update(values)


def _fail(ctx: Ctx, row: dict[str, Any], reason: str, *, kind: str = miaoshou.KIND_INVALID) -> None:
    _update(
        ctx,
        row,
        state=STATE_FAILED,
        last_error=reason[:500],
        error_kind=kind,
        attempts=int(row.get("attempts") or 0) + 1,
    )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            row["asset_id"],
            "temu_failed",
            {"store": row["store_key"], "code": row.get("code"), "reason": reason[:300]},
        )


def blocker(ctx: Ctx, row: dict[str, Any]) -> str | None:
    """这一（款, 店）现在还不能建箱的原因（要等的）；None = 可以建。"""
    if _mooya_engine(ctx):
        from . import mooya_listing  # mooya 模式：上架图与标题在 mooya 准备

        return mooya_listing.blocker(ctx, row)
    if not row.get("code"):
        if row.get("code_error"):
            return f"货号没领到：{row['code_error']}"[:200]
        return "领货号中"
    reason = temu_assets.readiness(ctx, row["asset_id"])
    if reason:
        return reason
    current = temu_assets.latest_ok_set(ctx, row["asset_id"])
    item = temu_assets.get(ctx, row["asset_id"], current["set_id"]) if current else None
    if not item or not item.get("prepared_at"):
        if item and item.get("last_error"):
            return f"上架图没准备好：{item['last_error']}"[:200]
        return "上架图准备中"
    if temu_title.get(ctx, row["asset_id"]) is None:
        if item.get("last_error"):
            return f"标题没写成：{item['last_error']}"[:200]
        return "标题还在写"
    return None


def confirm(
    ctx: Ctx,
    asset_ids: list[str],
    *,
    by: str,
    source: str = "table",
    titles: dict[str, str | None] | None = None,
    stores_only: set[str] | None = None,
) -> dict[str, Any]:
    """确认上架这几款（勾了「确认上架」/ 对话里说 / 全自动由程序确认）：定了的店里待上架、没上成的进 queued。
    返回 {queued, already, rejected[{asset_id, reason}], held}。
    titles：表里「上架标题」那一格；和库里的不一样就是人改过，按同一套规则复核，不合规这一款不上（原因记在店铺行上，备注里看得到）。
    stores_only：只确认这几家店（全自动只管开着的店）。人确认（source 不是 auto）时顺带解除暂停；程序确认跳过暂停着的款。"""
    if not enabled(ctx):
        raise StageError("上架没打开：temu.enabled 要开着、temu.stores 要配店铺")
    queued: list[str] = []
    already: list[str] = []
    rejected: list[dict[str, str]] = []
    skipped_held: list[str] = []
    automatic = source == SOURCE_AUTO
    groups = stores.by_asset(ctx, list(asset_ids))
    for asset_id in dict.fromkeys(asset_ids):
        items = [r for r in groups.get(asset_id) or [] if stores_only is None or r["store_key"] in stores_only]
        if not items:
            rejected.append({"asset_id": asset_id, "reason": "没定店铺：在候选表勾店铺列，或在 POD 对话里说上哪家店"})
            continue
        if automatic and held(ctx, asset_id):
            skipped_held.append(asset_id)
            continue
        todo = [r for r in items if r["state"] in ((STATE_CHOSEN,) if automatic else (STATE_CHOSEN, STATE_FAILED))]
        if not todo:
            already.append(asset_id)
            if not automatic:
                release(ctx, [asset_id], by=by, source=source)
            continue
        edited = ((titles or {}).get(asset_id) or "").strip()
        current = (temu_title.get(ctx, asset_id) or {}).get("title") or ""
        if edited and edited != current.strip():
            try:
                temu_title.save_human(ctx, asset_id, edited, by=by, limit=_title_limit(ctx), via="selected")
            except StageError as exc:
                for row in todo:
                    _update(ctx, row, last_error=str(exc)[:500], error_kind=ERROR_REJECTED)
                rejected.append({"asset_id": asset_id, "reason": str(exc)})
                continue
        if not automatic:
            release(ctx, [asset_id], by=by, source=source)
        now = now_iso()
        for row in todo:
            _update(
                ctx,
                row,
                state=STATE_QUEUED,
                confirmed_by=by,
                confirmed_at=now,
                last_error=None,
                error_kind=None,
                _actor=by,
                _source=source,
            )
        queued.append(asset_id)
        with ctx.db.tx() as conn:
            log_event(
                conn,
                asset_id,
                "temu_confirmed",
                {"by": by, "source": source, "stores": [r["store_key"] for r in todo]},
            )
    if queued:
        kick(ctx)
    return {"queued": queued, "already": already, "rejected": rejected, "held": skipped_held}


def auto_confirm(ctx: Ctx, asset_ids: list[str] | None = None) -> dict[str, Any]:
    """全自动：开着的店里待上架（chosen）、没暂停、标题没被退回的款由程序确认（确认人 system:temu）。
    素材没好的照样确认，排着队等（和人先勾一样）。返回 confirm 的结果（没有要确认的为空）。"""
    keys = {k for k in auto_stores(ctx) if auto_confirm_on(ctx, k)}
    if not keys:
        return {}
    todo = sorted(
        {
            r["asset_id"]
            for r in _rows(ctx, (STATE_CHOSEN,), asset_ids=asset_ids)
            if r["store_key"] in keys and r.get("error_kind") != ERROR_REJECTED
        }
    )
    todo = [a for a in todo if not held(ctx, a)]
    if not todo:
        return {}
    return confirm(ctx, todo, by=BY_AUTO, source=SOURCE_AUTO, stores_only=keys)


def cancel(ctx: Ctx, asset_ids: list[str], *, by: str, source: str = "chat") -> dict[str, Any]:
    """取消还没开始建箱的确认（回到待上架）；已经在建 / 进箱 / 上过的不动。返回 {cancelled, kept, held}。
    开着全自动时同时暂停这一款（listing_holds），程序不再替他确认，再勾「确认上架」或说确认就接着上。"""
    cancelled: list[str] = []
    kept: list[str] = []
    remote = _mooya_engine(ctx)
    for row in _rows(ctx, asset_ids=list(asset_ids)):
        if row["state"] == STATE_QUEUED and remote:
            from . import mooya_listing  # mooya 模式：交出去的先在 mooya 取消，取消不了（已建箱）的保持

            if not mooya_listing.cancel(ctx, row, by=by):
                kept.append(row["asset_id"])
                continue
        if row["state"] == STATE_QUEUED:
            _update(
                ctx,
                row,
                state=STATE_CHOSEN,
                confirmed_by=None,
                confirmed_at=None,
                _actor=by,
                _source=source,
                _reason="取消了确认上架",
            )
            cancelled.append(row["asset_id"])
        elif row["state"] in (*ACTIVE_STATES, STATE_LISTED):
            kept.append(row["asset_id"])
    cancelled = sorted(set(cancelled))
    paused: list[str] = []
    if auto_on(ctx):
        stoppable = sorted(set(asset_ids) - (set(kept) - set(cancelled)))
        paused = hold(ctx, stoppable, by=by, source=source, note="取消了「确认上架」")
    if cancelled:
        with ctx.db.tx() as conn:
            for asset_id in cancelled:
                log_event(conn, asset_id, "temu_confirm_cancelled", {"by": by})
    return {"cancelled": cancelled, "kept": sorted(set(kept) - set(cancelled)), "held": paused}


def retry(ctx: Ctx, asset_ids: list[str], *, by: str) -> dict[str, Any]:
    """没上成的重来（对话 / `temu retry`）：素材与标题、写品牌的失败次数清零，再确认一次（已有 detailId 的改成保存，不新建）。"""
    ids = list(dict.fromkeys(asset_ids))
    with ctx.db.tx() as conn:
        for asset_id in ids:
            conn.execute(
                "UPDATE listing_assets SET attempts = 0 WHERE asset_id = ? AND attempts > 0", (asset_id,)
            )
            conn.execute("DELETE FROM temu_brands WHERE asset_id = ? AND done_at IS NULL", (asset_id,))
            trace.record(conn, asset_id, "listing", to="retry", actor=by, source="chat")
    result = confirm(ctx, ids, by=by, source="chat")
    write_now(ctx, ids)
    return result


# ---------------------------------------------------------------- 后台推进


def _creatable(ctx: Ctx) -> bool:
    """有排着队、现在就能建箱（或要领货号）的。"""
    for row in _rows(ctx, (STATE_QUEUED,)):
        if not row["code"] and int(row["code_attempts"] or 0) < stores.MAX_CODE_ATTEMPTS:
            return True
        if blocker(ctx, row) is None:
            return True
    return False


def _prepare_targets(ctx: Ctx) -> list[str]:
    """定了店铺、套图出完、还没准备好上架图或标题的款（先准备好，人看了标题再勾确认上架）。"""
    out: list[str] = []
    seen: set[str] = set()
    for row in _rows(ctx, (STATE_CHOSEN, STATE_QUEUED)):
        asset_id = row["asset_id"]
        if asset_id in seen:
            continue
        seen.add(asset_id)
        current = temu_assets.latest_ok_set(ctx, asset_id)
        if current is None or temu_assets.readiness(ctx, asset_id):
            continue
        item = temu_assets.get(ctx, asset_id, current["set_id"]) or {}
        if int(item.get("attempts") or 0) >= feishu_table.MAX_ATTEMPTS:
            continue
        if not item.get("prepared_at") or temu_title.get(ctx, asset_id) is None:
            out.append(asset_id)
    return out


def _watch_targets(ctx: Ctx) -> list[dict[str, Any]]:
    """发布没成功、还在盯的（人在妙手修好重发后能自动标已上架）。"""
    days = float(ctx.setting("temu.watch_days", 7))
    out = []
    for row in _rows(ctx, (STATE_FAILED,)):
        at = parse_iso(row.get("publish_submitted_at") or row.get("boxed_at"))
        if row.get("detail_id") and at and utcnow() - at < dt.timedelta(days=days):
            out.append(row)
    return out


def _check_targets(ctx: Ctx) -> list[dict[str, Any]]:
    """要查发布结果的：发布中 / 发布结果未知的、进了采集箱的（人在妙手里点了发布也认得出）和还在盯的没上成的。"""
    states = (STATE_PUBLISHING, STATE_PUBLISH_UNKNOWN, STATE_BOX)
    return [r for r in _rows(ctx, states) if r.get("detail_id")] + _watch_targets(ctx)


def _auto_due(ctx: Ctx) -> bool:
    """有开着全自动的店里待上架、没暂停、等程序确认的款。"""
    keys = auto_stores(ctx)
    return bool(keys) and any(
        r["store_key"] in keys and r.get("error_kind") != ERROR_REJECTED and not held(ctx, r["asset_id"])
        for r in _rows(ctx, (STATE_CHOSEN,))
    )


def _link_floor(ctx: Ctx) -> dt.datetime:
    """图片链接剩得比这个时间还早到期的，在采集箱里的款要重签存回妙手。"""
    days = int(ctx.setting("temu.images.link_days", 30))
    resave = int(ctx.setting("temu.link_resave_days", 20) or 0)
    return utcnow() + dt.timedelta(days=max(1, days - resave))


def _resave_targets(ctx: Ctx) -> list[dict[str, Any]]:
    """在采集箱里（或没上成还在盯的）、图片链接快到期的（款, 店）：重签链接存回妙手，不然妙手里的图会失效。"""
    if not ctx.setting("temu.link_resave_days", 20):
        return []
    floor = _link_floor(ctx)
    out = []
    for row in _rows(ctx, (STATE_BOX,)) + _watch_targets(ctx):
        if not row.get("detail_id") or not row.get("set_id"):
            continue
        item = temu_assets.get(ctx, row["asset_id"], row["set_id"]) or {}
        expires = parse_iso(item.get("links_expire_at"))
        if item.get("files") and expires and expires < floor:
            out.append(row)
    return out


def _check_due(ctx: Ctx) -> bool:
    last = parse_iso(ctx.db.get_meta(CHECKED_META_KEY))
    minutes = float(ctx.setting("temu.poll_minutes", 5))
    return last is None or utcnow() - last >= dt.timedelta(minutes=minutes, seconds=-15)


def has_work(ctx: Ctx) -> bool:
    from . import title_sync  # 标题同步依赖本模块，放到用时再导入

    try:
        if not enabled(ctx) or _backing_off(ctx):
            return False
        if _mooya_engine(ctx):
            from . import mooya_listing  # mooya 模式：只看要交、要跟进、要对暂停 / 标题的

            return mooya_listing.has_work(ctx)
        if _auto_due(ctx):
            return True
        if _rows(ctx, (STATE_CREATING, STATE_UNKNOWN)) or _creatable(ctx) or _prepare_targets(ctx):
            return True
        if _brand_targets(ctx):
            return True
        if any(
            mode_for(ctx, r["store_key"]) == MODE_AUTO and brand_ready(ctx, r) and not publish_wait_reason(ctx, r)
            for r in _rows(ctx, (STATE_BOX,))
        ):
            return True
        if _resave_targets(ctx) or title_sync.due(ctx):
            return True
        return _check_due(ctx) and bool(_check_targets(ctx))
    except ConfigError:
        return False


def kick(ctx: Ctx) -> dict[str, Any] | None:
    """有要推进的、又没有上架进程在跑时拉起后台 `podctl temu sync`。"""
    if not has_work(ctx) or lease.active(ctx, LEASE_KEY):
        return None
    if any(t["kind"] == "temu" for t in bg.running(ctx)):
        return None
    return bg.spawn(ctx, ["temu", "sync"], kind="temu")


class _Stop(RuntimeError):
    """这一轮不往下做了（妙手鉴权 / 权限 / 限流 / 连不上），原因已经记下。"""


def _stop_on(ctx: Ctx, exc: miaoshou.MiaoshouError, out: dict[str, Any]) -> None:
    """妙手的服务级错误停下这一轮（鉴权 / 权限 / 白名单还要退避）；业务错误返回，由调用方记到这一行上。"""
    if exc.kind == miaoshou.KIND_SERVICE:
        _record_service_error(ctx, exc)
        out["errors"].append(str(exc)[:300])
        raise _Stop(f"妙手的应用 / 权限 / 白名单有问题：{exc}")
    if exc.kind in (miaoshou.KIND_RATE, miaoshou.KIND_TRANSIENT):
        out["errors"].append(str(exc)[:300])
        raise _Stop(f"妙手暂时不通（{exc.code or exc.kind}），下一轮再试")


def sync(ctx: Ctx) -> dict[str, Any]:
    """推进全部在上架的（款, 店）：准备上架图与标题 → 领货号、建箱 / 对账 → 发布 → 查发布结果，最后写表。
    temu.engine: mooya 时交给 mooya_listing.sync（交单、跟进、对暂停、交标题、拉轨迹）。"""
    if not enabled(ctx):
        return {"skipped": True, "reason": "上架没打开"}
    if _mooya_engine(ctx):
        from . import mooya_listing  # mooya 模式的一轮

        return mooya_listing.sync(ctx)
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        return {"skipped": True, "reason": "另一个上架进程正在运行"}
    from . import temu_open, title_sync  # 都依赖本模块，放到用时再导入

    out: dict[str, Any] = {
        "prepared": [],
        "boxed": [],
        "published": [],
        "listed": [],
        "failed": [],
        "waiting": [],
        "errors": [],
    }
    touched: set[str] = set()
    try:
        confirmed = auto_confirm(ctx)
        if confirmed.get("queued"):
            out["auto_confirmed"] = confirmed["queued"]
            touched |= set(confirmed["queued"])
        _prepare(ctx, out, touched)
        try:
            with miaoshou.Client(ctx) as api:
                _create(ctx, api, out, touched)
                _brands(ctx, api, out, touched)
                _resave(ctx, api, out, touched)
                title_sync.process_box(ctx, api, out, touched)
                _publish(ctx, api, out, touched)
                if out["published"] or _check_due(ctx):
                    _check_published(ctx, api, out, touched)
            _clear_service_error(ctx)
        except _Stop as stop:
            out["stopped"] = str(stop)
        except miaoshou.MiaoshouError as exc:
            # 没配凭据之类：记下，别的照常
            _record_service_error(ctx, exc)
            out["stopped"] = str(exc)[:300]
    finally:
        lease.drop(ctx, LEASE_KEY)
    # Temu 官方接口（开着时）：上架后的商品 ID、审核 / 在售状态，已上架款的标题改写
    if temu_open.enabled(ctx):
        out["temu_open"] = temu_open.sync(ctx, touched)
    if touched or out["waiting"]:
        out["table"] = write_now(ctx, sorted(touched | {w["asset_id"] for w in out["waiting"]}))
    if out["listed"]:
        from . import listed_table  # 已上架的款依赖本模块，放到用时再导入

        out["listed_table"] = listed_table.sync_now(ctx)
        shelved = _sweep_listed(ctx, out["listed"])
        if shelved:
            out["shelf"] = shelved
        feishu_table.kick(ctx)
    if out["failed"]:
        out["alerted"] = notify_failed(ctx, out["failed"])
    return out


def _sweep_listed(ctx: Ctx, codes: list[str]) -> dict[str, Any] | None:
    """刚上架的款所在的任务：点过「选款完成」、选中的款都上架完了就当场归档（开着 shelf.auto 时；出错不挡上架）。"""
    from . import shelf  # 归档模块依赖任务视图，放到用时再导入

    if not codes or not shelf.auto_enabled(ctx):
        return None
    marks = ",".join("?" * len(codes))
    seeds = {
        r["seed_id"]
        for r in ctx.db.q(
            "SELECT f.seed_id FROM store_listings s JOIN assets a ON a.asset_id = s.asset_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
            f" WHERE s.code IN ({marks})",
            tuple(codes),
        )
    }
    if not seeds:
        return None
    try:
        swept = shelf.sweep(ctx, source="listed", seeds=seeds)
    except Exception as exc:  # noqa: BLE001 - 归档出错不挡上架
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    archived = [a["seed_id"] for a in swept.get("archived") or []]
    return {"archived": archived} if archived else None


def _prepare(ctx: Ctx, out: dict[str, Any], touched: set[str]) -> None:
    """取带字标的上架图、写标题（每款一次，失败记在 listing_assets，满 MAX_ATTEMPTS 不再自动试）。"""
    for asset_id in _prepare_targets(ctx):
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
        bg.step(ctx, f"准备上架素材 {labels.asset(ctx, asset_id)}")
        current = temu_assets.latest_ok_set(ctx, asset_id)
        try:
            item = temu_assets.prepare(ctx, asset_id)
            if temu_title.get(ctx, asset_id) is None:
                temu_title.generate(
                    ctx, asset_id, temu_assets.first_image(ctx, item), limit=_title_limit(ctx)
                )
        except Exception as exc:  # noqa: BLE001 — 一款准备失败不挡别的款，原因写进备注
            if current:
                temu_assets.failed(ctx, asset_id, current["set_id"], f"{type(exc).__name__}: {exc}")
            out["errors"].append(f"{asset_id}: {type(exc).__name__}: {exc}"[:300])
            touched.add(asset_id)
            continue
        out["prepared"].append(asset_id)
        touched.add(asset_id)


def _create(ctx: Ctx, api: miaoshou.Client, out: dict[str, Any], touched: set[str]) -> None:
    for row in _rows(ctx, (STATE_CREATING,)):
        # 上一个进程建箱建到一半没了（只有一个进程持有租约）：结果未知，按货号核对
        _update(ctx, row, state=STATE_UNKNOWN)
    for row in _rows(ctx, (STATE_UNKNOWN,)):
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
        _reconcile(ctx, api, row, out, touched)
    queued = _rows(ctx, (STATE_QUEUED,))
    if any(not r["code"] for r in queued):
        stores.allocate_codes(ctx, asset_ids=[r["asset_id"] for r in queued if not r["code"]])
        queued = _rows(ctx, (STATE_QUEUED,))
    for row in queued:
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
        _create_one(ctx, api, row, out, touched)


def _create_one(
    ctx: Ctx, api: miaoshou.Client, row: dict[str, Any], out: dict[str, Any], touched: set[str]
) -> None:
    asset_id = row["asset_id"]
    reason = blocker(ctx, row)
    if reason:
        out["waiting"].append({"asset_id": asset_id, "store": row["store_key"], "reason": reason})
        return
    title = temu_title.get(ctx, asset_id) or {}
    if (title.get("meta") or {}).get("is_custom"):
        _fail(ctx, row, "定制款：Temu 上要配定制信息，人工上架（不自动上）")
        out["failed"].append(asset_id)
        touched.add(asset_id)
        return
    store_ = stores.get(ctx, row["store_key"])
    try:
        tpl = template(ctx, api, store_)
    except miaoshou.MiaoshouError as exc:
        _stop_on(ctx, exc, out)
        _fail(ctx, row, f"样板商品读不到：{exc}")
        out["failed"].append(asset_id)
        touched.add(asset_id)
        return
    except StageError as exc:
        _fail(ctx, row, str(exc))
        out["failed"].append(asset_id)
        touched.add(asset_id)
        return
    current = temu_assets.latest_ok_set(ctx, asset_id)
    try:
        # 链接快到期时重签（不重取图）
        item = temu_assets.prepare(ctx, asset_id)
    except Exception as exc:  # noqa: BLE001 — 记下原因，排着队等下一轮
        if current:
            temu_assets.failed(ctx, asset_id, current["set_id"], f"{type(exc).__name__}: {exc}")
        out["waiting"].append({"asset_id": asset_id, "store": row["store_key"], "reason": str(exc)[:200]})
        return
    payload = build_payload(
        store_, tpl, code=row["code"], title=title["title"], images=temu_assets.urls(item)
    )
    bg.step(ctx, f"建采集箱 {row['code']}")
    _update(
        ctx,
        row,
        state=STATE_CREATING,
        create_submitted_at=now_iso(),
        set_id=item["set_id"],
        title=title["title"],
        template_detail_id=store_.template_detail_id,
        shop_id=next(iter(shop_sites(store_, tpl))),
    )
    touched.add(asset_id)
    resaved = bool(row.get("detail_id"))
    try:
        if resaved:
            api.save_site_detail(row["detail_id"], payload)
            detail_id = int(row["detail_id"])
        else:
            detail_id = api.create_item(payload)
    except miaoshou.MiaoshouError as exc:
        if exc.maybe_applied and not row.get("detail_id"):
            _update(ctx, row, state=STATE_UNKNOWN, last_error=str(exc)[:300])
            out["errors"].append(f"{row['code']} 建箱结果未知：{exc}"[:300])
            return
        if exc.kind == miaoshou.KIND_INVALID:
            _fail(ctx, row, f"妙手没建成：{exc}")
            out["failed"].append(asset_id)
            return
        _update(ctx, row, state=STATE_QUEUED)
        _stop_on(ctx, exc, out)
        return
    _boxed(ctx, row, detail_id)
    out["boxed"].append(row["code"])
    _ensure_brand(ctx, api, row, store_, out, force=resaved)


def _boxed(ctx: Ctx, row: dict[str, Any], detail_id: int) -> None:
    _update(
        ctx,
        row,
        state=STATE_BOX,
        detail_id=str(detail_id),
        boxed_at=now_iso(),
        last_error=None,
        error_kind=None,
    )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            row["asset_id"],
            "temu_boxed",
            {"store": row["store_key"], "code": row["code"], "detail_id": detail_id},
        )


def _search(
    ctx: Ctx, api: miaoshou.Client, status: str, wanted: set[str], *, by: str
) -> dict[str, dict[str, Any]]:
    """翻采集箱列表找 wanted（by 是列表行里的字段，如 collectBoxDetailId；列表里的 itemNum 是空的，不能按货号找），
    最多 temu.search_pages 页（新的在前）。"""
    found: dict[str, dict[str, Any]] = {}
    pages = int(ctx.setting("temu.search_pages", 4))
    for page in range(1, pages + 1):
        data = api.search_box(status=status, page=page) or {}
        items = data.get("detailList") or []
        for item in items:
            key = str(item.get(by) or "")
            if key in wanted and key not in found:
                found[key] = item
        if len(found) == len(wanted) or len(items) < miaoshou.SEARCH_PAGE_MAX:
            break
    return found


def _miaoshou_time(text: Any) -> dt.datetime | None:
    """采集箱列表的 gmtCreate（北京时间「2026-10-28 21:04:32」）→ UTC；认不出为 None。"""
    try:
        local = dt.datetime.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(dt.UTC)


def _find_created(ctx: Ctx, api: miaoshou.Client, row: dict[str, Any]) -> int | None:
    """找这一（款, 店）建箱时可能已经建出来的商品。列表接口不给货号（itemNum 是空的，货号只在详情的店铺那一条里）：
    先按标题完全相同、店铺相同、建箱时间不早于提交前一天找候选（时区差不了一天），再读详情核对货号。
    返回 collectBoxDetailId；没有为 None。"""
    title = (row.get("title") or "").strip()
    if not title:
        return None
    shop = str(row.get("shop_id") or "")
    since = parse_iso(row.get("create_submitted_at"))
    pages = int(ctx.setting("temu.search_pages", 4))
    for status in (miaoshou.STATUS_NOT_PUBLISHED, miaoshou.STATUS_PUBLISHED):
        for page in range(1, pages + 1):
            items = (api.search_box(status=status, page=page) or {}).get("detailList") or []
            for item in items:
                if (item.get("title") or "").strip() != title:
                    continue
                shops = {str(s.get("shopId")) for s in item.get("collectBoxDetailShopList") or []}
                if shop and shop not in shops:
                    continue
                created = _miaoshou_time(item.get("gmtCreate"))
                if since and created and created < since - dt.timedelta(days=1):
                    continue
                info = (api.site_detail(item["collectBoxDetailId"]) or {}).get("siteCollectItemInfo") or {}
                codes = {info.get("itemNum")} | {
                    e.get("itemNum") for e in info.get("collectBoxDetailShopList") or []
                }
                if row["code"] in codes:
                    return int(item["collectBoxDetailId"])
            if len(items) < miaoshou.SEARCH_PAGE_MAX:
                break
    return None


def _reconcile(
    ctx: Ctx, api: miaoshou.Client, row: dict[str, Any], out: dict[str, Any], touched: set[str]
) -> None:
    """建箱结果未知：在没发布、已发布的采集箱商品里找（_find_created）；找到就收下，过了 RECONCILE_AFTER_SECONDS 还找不到才重建。"""
    try:
        detail_id = _find_created(ctx, api, row)
    except miaoshou.MiaoshouError as exc:
        _stop_on(ctx, exc, out)
        out["errors"].append(f"{row['code']} 核对建箱结果没成：{exc}"[:300])
        return
    touched.add(row["asset_id"])
    if detail_id is not None:
        _boxed(ctx, row, detail_id)
        out["boxed"].append(row["code"])
        _ensure_brand(ctx, api, row, stores.get(ctx, row["store_key"]), out)
        return
    at = parse_iso(row.get("create_submitted_at"))
    if at and utcnow() - at < dt.timedelta(seconds=RECONCILE_AFTER_SECONDS):
        out["waiting"].append(
            {"asset_id": row["asset_id"], "store": row["store_key"], "reason": "建箱结果未知，稍后再核对"}
        )
        return
    _update(ctx, row, state=STATE_QUEUED)


# ---------------------------------------------------------------- 品牌


def _brand_mark(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any] | None:
    found = ctx.db.q1(
        "SELECT * FROM temu_brands WHERE asset_id = ? AND store_key = ?",
        (row["asset_id"], row["store_key"]),
    )
    return dict(found) if found else None


def _same_target(mark: dict[str, Any] | None, row: dict[str, Any], store_: stores.Store) -> bool:
    """记录说的是这个采集箱商品、这个品牌（换了商品 / 改了配置就作废）。"""
    return bool(
        mark
        and mark.get("detail_id") == str(row.get("detail_id") or "")
        and mark.get("brand_id") == store_.brand_id
    )


def brand_ready(ctx: Ctx, row: dict[str, Any], store_: stores.Store | None = None) -> bool:
    """这家店不用写品牌（含已经不在配置里的店），或这个采集箱商品已经是配置的品牌。"""
    if store_ is None:
        try:
            store_ = stores.get(ctx, row["store_key"])
        except StageError:
            return True
    if not store_.brand_id:
        return True
    mark = _brand_mark(ctx, row)
    return _same_target(mark, row, store_) and bool((mark or {}).get("done_at"))


def brand_note(ctx: Ctx, row: dict[str, Any], store_: stores.Store | None = None) -> str | None:
    """进了采集箱、品牌还没写上时写在备注里的话；写好了 / 不用写为 None。"""
    if _mooya_engine(ctx):
        from . import mooya_listing  # mooya 模式：品牌由 mooya 写，说明照它给的

        return mooya_listing.brand_note(ctx, row)
    store_ = store_ or stores.get(ctx, row["store_key"])
    if not row.get("detail_id") or brand_ready(ctx, row, store_):
        return None
    mark = _brand_mark(ctx, row)
    if not _same_target(mark, row, store_) or not (mark or {}).get("last_error"):
        return "品牌还没写上"
    error = mark["last_error"]
    if int(mark.get("attempts") or 0) >= BRAND_MAX_ATTEMPTS:
        brand = store_.brand or store_.brand_id
        hours = float(ctx.setting("temu.brand_recheck_hours", 6) or 0)
        then = "程序接着发布" if mode_for(ctx, row["store_key"]) == MODE_AUTO else "再发布"
        again = f"，程序每 {hours:g} 小时复查一次，选好了{then}" if hours > 0 else f"后{then}"
        return f"品牌写不上（{error}）：在妙手里把品牌名选成 {brand}{again}"
    return f"品牌还没写上（{error}），下一轮再试"


def _brand_status(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any] | None:
    """`temu status` 里这一（款, 店）的品牌：没配品牌、还没进采集箱为 None。"""
    try:
        store_ = stores.get(ctx, row["store_key"])
    except StageError:
        return None
    if not store_.brand_id or not row.get("detail_id"):
        return None
    mark = _brand_mark(ctx, row)
    current = _same_target(mark, row, store_)
    return {
        "brand_id": store_.brand_id,
        "ok": current and bool((mark or {}).get("done_at")),
        "saved": bool(current and (mark or {}).get("saved")),
        "attempts": int((mark or {}).get("attempts") or 0) if current else 0,
        "error": (mark or {}).get("last_error") if current else None,
    }


def _brand_targets(ctx: Ctx) -> list[dict[str, Any]]:
    """进了采集箱、要写品牌、还没写上的：失败没满次数的（会写），满了次数、上次看过超过 temu.brand_recheck_hours 的
    （只读复查：人在妙手里选好了就接着走，`recheck` 为真）。"""
    hours = float(ctx.setting("temu.brand_recheck_hours", 6) or 0)
    out = []
    for row in _rows(ctx, (STATE_BOX,)):
        try:
            store_ = stores.get(ctx, row["store_key"])
        except StageError:
            continue
        if not row.get("detail_id") or brand_ready(ctx, row, store_):
            continue
        mark = _brand_mark(ctx, row)
        if _same_target(mark, row, store_) and int((mark or {}).get("attempts") or 0) >= BRAND_MAX_ATTEMPTS:
            seen = parse_iso((mark or {}).get("updated_at"))
            if hours <= 0 or (seen and utcnow() - seen < dt.timedelta(hours=hours)):
                continue
            out.append({**row, "recheck": True})
            continue
        out.append(row)
    return out


def _touch_brand(ctx: Ctx, row: dict[str, Any]) -> None:
    """复查过一次、品牌还没在：只记下看过的时间（不算次数），隔 temu.brand_recheck_hours 再看。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE temu_brands SET updated_at = ? WHERE asset_id = ? AND store_key = ?",
            (now_iso(), row["asset_id"], row["store_key"]),
        )


def _mark_brand(
    ctx: Ctx,
    row: dict[str, Any],
    store_: stores.Store,
    *,
    error: str | None = None,
    saved: bool = False,
) -> None:
    """记下这一次：error 为空 = 品牌在了（saved = 是程序写上的），否则失败次数 + 1。"""
    mark = _brand_mark(ctx, row)
    attempts = int(mark.get("attempts") or 0) if _same_target(mark, row, store_) else 0
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO temu_brands(asset_id, store_key, detail_id, brand_id, done_at, saved, attempts,"
            " last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(asset_id, store_key) DO UPDATE SET detail_id = excluded.detail_id,"
            " brand_id = excluded.brand_id, done_at = excluded.done_at, saved = excluded.saved,"
            " attempts = excluded.attempts, last_error = excluded.last_error, updated_at = excluded.updated_at",
            (
                row["asset_id"],
                row["store_key"],
                str(row["detail_id"]),
                store_.brand_id,
                None if error else now,
                int(saved),
                attempts + (1 if error else 0),
                error[:300] if error else None,
                now,
            ),
        )
        if error is None:
            log_event(
                conn,
                row["asset_id"],
                "temu_brand",
                {
                    "store": row["store_key"],
                    "code": row.get("code"),
                    "detail_id": row["detail_id"],
                    "brand_id": store_.brand_id,
                    "saved": saved,
                },
            )
        trace.record(
            conn,
            row["asset_id"],
            "brand",
            to="failed" if error else ("written" if saved else "present"),
            store=row["store_key"],
            code=row.get("code"),
            reason=error,
            payload={"detail_id": row["detail_id"], "brand_id": store_.brand_id, "attempts": attempts + (1 if error else 0)},
        )


def _ensure_brand(
    ctx: Ctx,
    api: miaoshou.Client,
    row: dict[str, Any],
    store_: stores.Store,
    out: dict[str, Any],
    *,
    force: bool = False,
    read_only: bool = False,
) -> str | None:
    """给进了采集箱的商品写上这家店的品牌：读店铺模式详情，已经是配置的品牌就只记一笔（人在妙手里选过的不重写），
    不是就只改 brandId、整份存回去，再读回核对。force：不看「已经写过」的记录，重新读一次（重试时站点模式保存过）。
    read_only：写满了次数之后的复查，只读不写，没在妙手里选好也不算次数（只记下看过的时间）。
    返回 None = 品牌在了 / 不用写；否则是没写上的原因（已记下）。妙手的服务级 / 限流 / 连不上错误记下后照常停下这一轮。"""
    if not store_.brand_id or not row.get("detail_id"):
        return None
    if not force and brand_ready(ctx, row, store_):
        return None
    detail_id = row["detail_id"]
    shop = row.get("shop_id") or store_.shop_id
    try:
        data = api.shop_detail(detail_id, shop)
        info = data.get("shopCollectItemInfo") or {}
        if not info:
            raise StageError("妙手没给店铺模式详情")
        saved = False
        if read_only and str(info.get("brandId") or "") != store_.brand_id:
            _touch_brand(ctx, row)
            return f"复查：妙手里还没选品牌 {store_.brand or store_.brand_id}"
        if str(info.get("brandId") or "") != store_.brand_id:
            info["brandId"] = store_.brand_id
            # 店铺模式详情里这一项读出来是空的，保存时必填：用这份详情的模式（shop）
            info["editModel"] = info.get("editModel") or data.get("editModel") or "shop"
            api.save_shop_detail(detail_id, shop, info)
            again = api.shop_detail(detail_id, shop).get("shopCollectItemInfo") or {}
            if str(again.get("brandId") or "") != store_.brand_id:
                raise StageError(f"妙手没存上（现在是 {again.get('brandId') or '空'}）")
            saved = True
    except miaoshou.MiaoshouError as exc:
        _mark_brand(ctx, row, store_, error=str(exc))
        _stop_on(ctx, exc, out)
        return str(exc)
    except StageError as exc:
        _mark_brand(ctx, row, store_, error=str(exc))
        return str(exc)
    _mark_brand(ctx, row, store_, saved=saved)
    return None


def _brands(ctx: Ctx, api: miaoshou.Client, out: dict[str, Any], touched: set[str]) -> None:
    """进了采集箱、品牌还没写上的补写（建箱当时没写成的、开品牌配置之前就在箱里的）。
    这一轮刚建进箱的（out["boxed"]）建完当场试过了，没写成的留到下一轮。"""
    for row in _brand_targets(ctx):
        if row["code"] in out["boxed"]:
            continue
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
        touched.add(row["asset_id"])
        recheck = bool(row.pop("recheck", False))
        reason = _ensure_brand(ctx, api, row, stores.get(ctx, row["store_key"]), out, read_only=recheck)
        if reason:
            out["errors"].append(f"{row['code']} 品牌没写上：{reason}"[:300])


def resave_box(
    ctx: Ctx,
    api: miaoshou.Client,
    row: dict[str, Any],
    out: dict[str, Any],
    *,
    reason: str,
    resign: bool = False,
) -> bool:
    """把采集箱里这一（款, 店）按样板 + 现在的标题 + 上架图整份存回妙手（站点模式），再核对品牌（站点模式保存后重读一遍）。
    resign：先重签图片链接。返回是否存上了；妙手的服务级 / 限流错误照常停下这一轮。"""
    store_ = stores.get(ctx, row["store_key"])
    item = temu_assets.get(ctx, row["asset_id"], row["set_id"]) if row.get("set_id") else None
    title = temu_title.get(ctx, row["asset_id"]) or {}
    if not item or not item.get("files") or not title.get("title") or not row.get("detail_id"):
        return False
    try:
        if resign:
            item = temu_assets.resign_now(ctx, item)
        tpl = template(ctx, api, store_)
        payload = build_payload(store_, tpl, code=row["code"], title=title["title"], images=temu_assets.urls(item))
        api.save_site_detail(row["detail_id"], payload)
    except miaoshou.MiaoshouError as exc:
        trace.log(ctx, row["asset_id"], "box", to="resave_failed", store=row["store_key"], code=row.get("code"), reason=str(exc))
        _stop_on(ctx, exc, out)
        out["errors"].append(f"{row['code']} 存回妙手没成：{exc}"[:300])
        return False
    except StageError as exc:
        out["errors"].append(f"{row['code']} 存回妙手没成：{exc}"[:300])
        return False
    _update(ctx, row, title=title["title"])
    trace.log(
        ctx,
        row["asset_id"],
        "box",
        to="resaved",
        store=row["store_key"],
        code=row.get("code"),
        reason=reason,
        payload={"detail_id": row["detail_id"], "links_expire_at": item.get("links_expire_at")},
    )
    _ensure_brand(ctx, api, row, store_, out, force=True)
    return True


def _resave(ctx: Ctx, api: miaoshou.Client, out: dict[str, Any], touched: set[str]) -> None:
    """在采集箱里放得久的款：图片链接快到期了，重签存回妙手（妙手原样存我们的签名链接，到期就打不开）。"""
    for row in _resave_targets(ctx):
        lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
        if resave_box(ctx, api, row, out, reason="图片链接快到期：重签存回妙手", resign=True):
            out.setdefault("resaved", []).append(row["code"])
            touched.add(row["asset_id"])


def _publish(ctx: Ctx, api: miaoshou.Client, out: dict[str, Any], touched: set[str]) -> None:
    """发布方式是 auto 的店（temu.publish: auto，或开着全自动）：进了采集箱的按店分组，每次最多 BATCH_MAX 个提交发布。
    品牌没写上的不发；全自动的店没配品牌不发。发布请求发出去才超时的记 publish_unknown，不当场重发。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    sites: dict[str, list[str]] = {}
    for row in _rows(ctx, (STATE_BOX,)):
        if mode_for(ctx, row["store_key"]) != MODE_AUTO:
            continue
        store_ = stores.get(ctx, row["store_key"])
        if not brand_ready(ctx, row, store_):
            out["waiting"].append(
                {"asset_id": row["asset_id"], "store": row["store_key"], "reason": "品牌还没写上"}
            )
            continue
        reason = publish_wait_reason(ctx, row)
        if reason:
            out["waiting"].append({"asset_id": row["asset_id"], "store": row["store_key"], "reason": reason})
            continue
        try:
            tpl = template(ctx, api, store_)
        except miaoshou.MiaoshouError as exc:
            _stop_on(ctx, exc, out)
            out["errors"].append(f"{row['code']}: {exc}"[:300])
            continue
        except StageError as exc:
            out["errors"].append(f"{row['code']}: {exc}"[:300])
            continue
        mapping = shop_sites(store_, tpl)
        shop = next(iter(mapping))
        groups.setdefault(shop, []).append(row)
        sites[shop] = mapping[shop]
    for shop, items in groups.items():
        for i in range(0, len(items), miaoshou.BATCH_MAX):
            chunk = items[i : i + miaoshou.BATCH_MAX]
            lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
            try:
                api.publish([int(r["detail_id"]) for r in chunk], {shop: sites[shop]})
            except miaoshou.MiaoshouError as exc:
                if exc.maybe_applied:
                    # 请求发出去了才断开：妙手可能已经在发布，等结果，过一阵还没发布再重发（不当场重发，免得重复上架）
                    now = now_iso()
                    for row in chunk:
                        _update(
                            ctx,
                            row,
                            state=STATE_PUBLISH_UNKNOWN,
                            publish_submitted_at=now,
                            _source="miaoshou",
                            _reason=f"发布请求结果未知：{exc}",
                        )
                        touched.add(row["asset_id"])
                _stop_on(ctx, exc, out)
                if exc.maybe_applied:
                    continue
                for row in chunk:
                    _fail(ctx, row, f"发布没提交上：{exc}")
                    touched.add(row["asset_id"])
                    out["failed"].append(row["asset_id"])
                continue
            now = now_iso()
            for row in chunk:
                _update(ctx, row, state=STATE_PUBLISHING, publish_submitted_at=now, _source="miaoshou")
                touched.add(row["asset_id"])
                out["published"].append(row["code"])
            with ctx.db.tx() as conn:
                log_event(
                    conn, "temu", "temu_publish_submitted", {"shop": shop, "codes": [r["code"] for r in chunk]}
                )
                for row in chunk:
                    log_event(
                        conn,
                        row["asset_id"],
                        "temu_publish_submitted",
                        {"store": row["store_key"], "code": row["code"], "detail_id": row["detail_id"], "shop": shop},
                    )


def _check_published(ctx: Ctx, api: miaoshou.Client, out: dict[str, Any], touched: set[str]) -> None:
    rows = _check_targets(ctx)
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, CHECKED_META_KEY, now_iso())
    if not rows:
        return
    wanted = {str(r["detail_id"]) for r in rows}
    try:
        found = _search(ctx, api, miaoshou.STATUS_PUBLISHED, wanted, by="collectBoxDetailId")
    except miaoshou.MiaoshouError as exc:
        _stop_on(ctx, exc, out)
        out["errors"].append(f"查发布结果没成：{exc}"[:300])
        return
    timeout = float(ctx.setting("temu.publish_timeout_minutes", 120))
    for row in rows:
        if str(row["detail_id"]) in found:
            _remember_published(ctx, row, found[str(row["detail_id"])])
            _listed(ctx, row)
            out["listed"].append(row["code"])
            touched.add(row["asset_id"])
            continue
        at = parse_iso(row.get("publish_submitted_at"))
        if row["state"] == STATE_PUBLISH_UNKNOWN and at and utcnow() - at > dt.timedelta(minutes=PUBLISH_UNKNOWN_MINUTES):
            resends = _publish_resends(ctx, row)
            if resends < PUBLISH_RESENDS:
                _update(
                    ctx,
                    row,
                    state=STATE_BOX,
                    publish_submitted_at=None,
                    _source="resend",
                    _reason=f"发布请求结果未知、{PUBLISH_UNKNOWN_MINUTES} 分钟了还没发布：重发一次",
                )
            else:
                _fail(ctx, row, "发布请求两次都没生效，去妙手采集箱看原因；在妙手里修好重发，这里会自动标已上架", kind="publish_timeout")
                out["failed"].append(row["asset_id"])
            touched.add(row["asset_id"])
            continue
        if row["state"] == STATE_PUBLISHING and at and utcnow() - at > dt.timedelta(minutes=timeout):
            _fail(
                ctx,
                row,
                f"提交发布 {int(timeout)} 分钟了还没发布，去妙手采集箱看失败原因；在妙手里修好重发，这里会自动标已上架",
                kind="publish_timeout",
            )
            out["failed"].append(row["asset_id"])
            touched.add(row["asset_id"])


def _publish_resends(ctx: Ctx, row: dict[str, Any]) -> int:
    """这一（款, 店）因为发布结果未知重发过几次（数轨迹）。"""
    return int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM trace_events WHERE asset_id = ? AND store_key = ? AND stage = 'listing'"
            " AND to_state = ? AND source = 'resend'",
            (row["asset_id"], row["store_key"], STATE_BOX),
        )
        or 0
    )


def _remember_published(ctx: Ctx, row: dict[str, Any], item: dict[str, Any]) -> None:
    """查到已发布时妙手列表里那一条原样记下（以后对账用）。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO temu_publish_records(detail_id, asset_id, store_key, code, raw_json, seen_at)"
            " VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(detail_id) DO UPDATE SET raw_json = excluded.raw_json,"
            " seen_at = excluded.seen_at",
            (str(row["detail_id"]), row["asset_id"], row["store_key"], row.get("code"), dumps(item), now_iso()),
        )


def _local_day(ctx: Ctx, ts: str | None) -> str | None:
    at = parse_iso(ts)
    return at.astimezone(_tz(ctx)).strftime("%Y-%m-%d") if at else None


def _listed(ctx: Ctx, row: dict[str, Any], *, listed_at: str | None = None) -> None:
    from . import listed_table  # 已上架的款依赖本模块，放到用时再导入

    _update(ctx, row, state=STATE_LISTED, listed_at=listed_at or now_iso(), last_error=None, error_kind=None)
    if not _mooya_engine(ctx):
        # mooya 模式：底账由 mooya 上架时自己记，这里拉（listed_table.pull）
        listed_table.record(ctx, row)
    with ctx.db.tx() as conn:
        log_event(
            conn,
            row["asset_id"],
            "temu_listed",
            {"store": row["store_key"], "code": row["code"], "detail_id": row["detail_id"]},
        )
    items = _rows(ctx, asset_ids=[row["asset_id"]])
    if not items or any(r["state"] != STATE_LISTED for r in items):
        return
    # 这一款所有店都上了：上架状态记已上架（不等表格回读），下载确认自动勾上（交付包、归档照旧接上）
    record_id = ctx.db.scalar("SELECT record_id FROM table_rows WHERE asset_id = ?", (row["asset_id"],))
    old = ctx.db.q1("SELECT * FROM listing_marks WHERE asset_id = ?", (row["asset_id"],))
    with ctx.db.tx() as conn:
        listing._save(
            conn,
            row["asset_id"],
            record_id,
            {
                "state": feishu_table.LISTING_LISTED,
                "listed_on": _local_day(ctx, row["listed_at"]),
                "note": "；".join(note_line(ctx, r) for r in items)[:500],
            },
            dict(old) if old else None,
            source="temu",
            by=BY_AUTO,
        )
    if downloads.enabled(ctx):
        try:
            downloads.confirm(ctx, [row["asset_id"]], by=BY_AUTO, source="listing")
        except StageError:
            pass


# ---------------------------------------------------------------- 写「选中的款」


def _tz(ctx: Ctx) -> ZoneInfo:
    return ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))


def aggregate(items: list[dict[str, Any]]) -> str | None:
    """这一款所有店铺汇总成一个上架状态；没有店铺为 None（不写）。每家店都上了是已上架，有一家没上成是上架失败；
    有一家已经进了妙手（MIAOSHOU_STATES）、或已经上了而别的店还没上完是上架中；其余（领货号、等套图、出套图、
    准备上架图、待确认、排队）是待上架。"""
    if not items:
        return None
    states = {r["state"] for r in items}
    if states == {STATE_LISTED}:
        return feishu_table.LISTING_LISTED
    if STATE_FAILED in states:
        return feishu_table.LISTING_FAILED
    if states & {*MIAOSHOU_STATES, STATE_LISTED}:
        return feishu_table.LISTING_IN_PROGRESS
    return feishu_table.LISTING_PENDING


def note_line(ctx: Ctx, row: dict[str, Any]) -> str:
    """一家店的上架备注：「3店 FLow0006：已提交 Temu（妙手 123）」。"""
    store_ = stores.get(ctx, row["store_key"])
    head = f"{store_.label} {row['code'] or '领号中'}"
    state = row["state"]
    if state == STATE_CHOSEN:
        if row.get("error_kind") == ERROR_REJECTED and row.get("last_error"):
            return f"{head}：没上架：{row['last_error']}"
        if held(ctx, row["asset_id"]):
            return f"{head}：已暂停（取消了「确认上架」），再勾就接着上"
        reason = blocker(ctx, row)
        if reason:
            return f"{head}：待上架（{reason}）"
        if _mooya_engine(ctx):
            # 上架交给 mooya：上架图与标题在 mooya 准备，确认之后才写
            if auto_confirm_on(ctx, row["store_key"]):
                return f"{head}：套图好了，全自动：马上交给 mooya 上架"
            return f"{head}：套图好了，勾「确认上架」交给 mooya 上架（标题由 mooya 写）"
        if auto_confirm_on(ctx, row["store_key"]):
            return f"{head}：标题写好了，全自动：马上建进妙手采集箱"
        return f"{head}：标题写好了，看过后勾「确认上架」"
    if state == STATE_QUEUED:
        reason = blocker(ctx, row)
        return f"{head}：排队中" + (f"（{reason}）" if reason else "")
    if state == STATE_BOX:
        pending = brand_note(ctx, row, store_)
        if pending:
            return f"{head}：已进妙手采集箱（{row['detail_id']}），{pending}"
        waiting = publish_wait_reason(ctx, row)
        if waiting and mode_for(ctx, row["store_key"]) == MODE_BOX:
            return f"{head}：已进妙手采集箱（{row['detail_id']}），在妙手里检查后点发布"
        if waiting:
            return f"{head}：已进妙手采集箱（{row['detail_id']}），{waiting}"
        return f"{head}：已进妙手采集箱（{row['detail_id']}），等发布"
    if state == STATE_PUBLISHING:
        return f"{head}：发布中（妙手 {row['detail_id']}）"
    if state == STATE_PUBLISH_UNKNOWN:
        return f"{head}：发布请求结果未知，等妙手的结果（妙手 {row['detail_id']}），一阵还没发布会重发一次"
    if state == STATE_LISTED:
        sites = "·".join(next(iter(shop_sites(store_, cached_template(ctx, store_)).values()), []))
        return f"{head}：已提交 Temu{(' ' + sites) if sites else ''}（妙手 {row['detail_id']}）"
    if state == STATE_FAILED:
        return f"{head}：没上成：{row.get('last_error') or ''}；改好后再勾「确认上架」"
    return f"{head}：{STATE_TEXT.get(state, state)}"


def _want(
    ctx: Ctx, asset_id: str, items: list[dict[str, Any]], *, extended: bool = False
) -> tuple[dict[str, Any], bool]:
    """这一款在「选中的款」里程序写的格子，和「确认上架」该不该勾着（在上 / 上过的勾着）。没有店铺时清空这几格。
    extended：表里已经有当前环节等四列，一起写。"""
    from . import title_sync  # 标题同步依赖本模块，放到用时再导入

    if not items:
        empty = {STORE_FIELD: None, CODE_FIELD: None, NOTE_FIELD: None}
        if extended:
            empty.update({PIPELINE_FIELD: None, PIPELINE_SINCE_FIELD: None, DETAIL_FIELD: None, TITLE_SYNC_FIELD: None})
        return empty, False
    notes = [note_line(ctx, r) for r in items]
    current = temu_assets.latest_ok_set(ctx, asset_id)
    fallback = scenes_baseline_note(ctx, current["set_id"]) if current else None
    if fallback:
        notes.append(fallback)
    cells: dict[str, Any] = {
        STORE_FIELD: [stores.get(ctx, r["store_key"]).column for r in items],
        CODE_FIELD: stores.codes_text(ctx, asset_id),
        NOTE_FIELD: "；".join(notes)[:1000],
        STATE_FIELD: aggregate(items),
    }
    title = temu_title.get(ctx, asset_id)
    if title:
        cells[TITLE_FIELD] = title["title"]
    if all(r["state"] == STATE_LISTED for r in items):
        day = max(parse_iso(r["listed_at"]) or utcnow() for r in items).astimezone(_tz(ctx))
        cells[DATE_FIELD] = int(dt.datetime(day.year, day.month, day.day, tzinfo=_tz(ctx)).timestamp() * 1000)
    if extended:
        summary = trace.summary(ctx, asset_id)
        since = parse_iso((summary or {}).get("since"))
        cells[PIPELINE_FIELD] = (summary or {}).get("label")
        cells[PIPELINE_SINCE_FIELD] = int(since.timestamp() * 1000) if since else None
        details = [
            f"{stores.get(ctx, r['store_key']).label} {r['detail_id']}" for r in items if r.get("detail_id")
        ]
        cells[DETAIL_FIELD] = " / ".join(details) or None
        cells[TITLE_SYNC_FIELD] = title_sync.cell_text(ctx, asset_id)
    checked = all(r["state"] in (*ACTIVE_STATES, STATE_LISTED) for r in items)
    if checked and _paused_in_box(ctx, asset_id, items):
        checked = False
    return cells, checked


def _paused_in_box(ctx: Ctx, asset_id: str, items: list[dict[str, Any]]) -> bool:
    """暂停着、有进了采集箱还没发布的（不发布，「确认上架」保持取消，再勾接着发布）。"""
    states = {r["state"] for r in items}
    return STATE_BOX in states and states <= {STATE_BOX, STATE_CHOSEN, STATE_FAILED} and held(ctx, asset_id)


def _pausable(ctx: Ctx, items: list[dict[str, Any]]) -> bool:
    """人取消「确认上架」时能停下的：全自动发布的店进了采集箱还没发布，其余的店也还没开始建箱。"""
    if not {r["state"] for r in items} <= {STATE_BOX, STATE_QUEUED, STATE_CHOSEN, STATE_FAILED}:
        return False
    return any(r["state"] == STATE_BOX and mode_for(ctx, r["store_key"]) == MODE_AUTO for r in items)


def scenes_baseline_note(ctx: Ctx, set_id: str) -> str | None:
    from . import scenes  # 套图模块在本模块之前导入，用时再取避免循环

    return scenes.baseline_note(ctx, set_id)


def _cache(ctx: Ctx) -> dict[str, dict[str, Any]]:
    return {r["asset_id"]: dict(r) for r in ctx.db.q("SELECT * FROM temu_rows")}


def _ours(cache: dict[str, dict[str, Any]], asset_id: str, record_id: str) -> bool:
    """「确认上架」现在的勾是程序写的（上次写的是勾上）。"""
    old = cache.get(asset_id) or {}
    return old.get("record_id") == record_id and bool(old.get("confirm_written"))


def _records(ctx: Ctx) -> dict[str, str]:
    # 按份同步「选中的款」时只挑归到这一份的款（每周选款表，books.scoped）
    return {
        r["asset_id"]: r["record_id"]
        for r in ctx.db.q("SELECT asset_id, record_id FROM table_rows WHERE record_id IS NOT NULL")
        if table_registry.mine(r["asset_id"])
    }


def _changes(
    ctx: Ctx,
    asset_ids: list[str] | None,
    cache: dict[str, dict[str, Any]],
    *,
    force_check: set[str] | frozenset[str] = frozenset(),
    untick: set[str] | frozenset[str] = frozenset(),
    extended: bool = False,
) -> list[dict[str, Any]]:
    """要写进飞书的行：只带和上次写的不一样的格子；勾只在要改时写（没写过勾、也不该勾的不碰，可能是人刚勾的），
    force_check 里的照样写一次，untick 里的一定取消。extended：连当前环节等四列一起写。"""
    records = _records(ctx)
    groups = stores.by_asset(ctx, asset_ids)
    ids = set(groups) | set(cache)
    if asset_ids is not None:
        ids &= set(asset_ids)
    out = []
    for asset_id in sorted(ids):
        record_id = records.get(asset_id)
        if not record_id:
            continue
        cells, checked = _want(ctx, asset_id, groups.get(asset_id) or [], extended=extended)
        if asset_id in untick:
            checked = False
        old = cache.get(asset_id) or {}
        same = old.get("record_id") == record_id
        last = (loads(old.get("state_key"), {}) or {}) if same else {}
        if not groups.get(asset_id) and not last:
            # 没定店铺、也没写过的行：不是程序管的
            continue
        send = {k: v for k, v in cells.items() if k not in last or last[k] != v}
        if not same:
            send = {k: v for k, v in cells.items() if v is not None}
        last_check = old.get("confirm_written") if same else None
        write_check = (
            asset_id in force_check
            or asset_id in untick
            or (checked if last_check is None else bool(last_check) != checked)
        )
        if write_check:
            send[CONFIRM_FIELD] = checked
        if not send:
            continue
        out.append(
            {
                "asset_id": asset_id,
                "record_id": record_id,
                "fields": send,
                "cells": cells,
                "checked": checked if write_check else last_check,
                "clear": not groups.get(asset_id),
            }
        )
    return out


def _take_writer(ctx: Ctx) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while not lease.take(ctx, WRITE_LEASE, WRITE_SECONDS):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


def _remember(ctx: Ctx, written: list[dict[str, Any]]) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        for change in written:
            if change["clear"]:
                conn.execute("DELETE FROM temu_rows WHERE asset_id = ?", (change["asset_id"],))
                continue
            conn.execute(
                "INSERT INTO temu_rows(asset_id, record_id, state_key, confirm_written, updated_at)"
                " VALUES(?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET record_id = excluded.record_id,"
                " state_key = excluded.state_key, confirm_written = excluded.confirm_written,"
                " updated_at = excluded.updated_at",
                (
                    change["asset_id"],
                    change["record_id"],
                    dumps(change["cells"]),
                    None if change["checked"] is None else int(bool(change["checked"])),
                    now,
                ),
            )


def write_rows(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    asset_ids: list[str] | None = None,
    *,
    force_check: set[str] | frozenset[str] = frozenset(),
    untick: set[str] | frozenset[str] = frozenset(),
) -> int:
    """把这些款（缺省全部）在「选中的款」里程序写的格子写成该有的样子。整批写不成逐行写，写不成的跳过；
    一行都没写成时抛出最后的错误。另一个进程一直占着写入租约时不写（返回 0，交给下一次表格同步）。"""
    if not ready(info):
        return 0
    if not _take_writer(ctx):
        feishu_table.kick(ctx)
        return 0
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    params = {"user_id_type": "open_id"}
    written: list[dict[str, Any]] = []
    last: feishu.FeishuError | None = None
    try:
        changes = _changes(
            ctx, asset_ids, _cache(ctx), force_check=force_check, untick=untick, extended=pipeline_ready(info)
        )
        for i in range(0, len(changes), _PAGE):
            chunk = changes[i : i + _PAGE]
            lease.take(ctx, WRITE_LEASE, WRITE_SECONDS)
            try:
                api.call(
                    "POST",
                    f"{path}/batch_update",
                    params=params,
                    json={"records": [{"record_id": c["record_id"], "fields": c["fields"]} for c in chunk]},
                )
                written += chunk
            except feishu.FeishuError:
                for change in chunk:
                    lease.take(ctx, WRITE_LEASE, WRITE_SECONDS)
                    try:
                        api.call(
                            "PUT", f"{path}/{change['record_id']}", params=params, json={"fields": change["fields"]}
                        )
                        written.append(change)
                    except feishu.FeishuError as exc:
                        last = exc
        _remember(ctx, written)
    finally:
        lease.drop(ctx, WRITE_LEASE)
    if changes and not written and last is not None:
        raise last
    return len(written)


def write_now(ctx: Ctx, asset_ids: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """状态已经记在库里，写表失败不往外抛：{written} 或 {write_error}，写不进去的交给下一次表格同步。
    旧表格和每周表格各天的「选中的款」各写各的款。"""
    from . import books  # 每周选款表依赖表格模块，放到用时再导入

    if not enabled(ctx):
        return {"written": 0}
    try:
        trace.refresh(ctx, asset_ids)
    except Exception as exc:  # noqa: BLE001 - 当前环节算不出来不挡写表
        return {"write_error": f"当前环节没算成：{type(exc).__name__}: {exc}"[:300]}
    written = 0
    try:
        with feishu.Client(ctx) as api:
            for info in books.scopes(ctx):
                if not ready(info):
                    continue
                with books.scoped(ctx, info):
                    written += write_rows(ctx, api, info, asset_ids, **kwargs)
    except _FAILURES as exc:
        feishu_table.kick(ctx)
        return {"write_error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"written": written}


def ensure_columns(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """给「选中的款」补上四列，「上架状态」补上「上架中」「上架失败」，「店铺」补上新加的店铺（调用方持有选款表的同步租约；
    要先有上架状态那一列）。"""
    if not enabled(ctx) or not feishu_table.listing_ready(info):
        return {}
    out: dict[str, Any] = {}
    try:
        added = feishu_table.ensure_fields(ctx, api, info, fields(ctx))
        if added:
            out["fields_added"] = added
        options = feishu_table.ensure_options(
            ctx, api, info, [_state_spec(), _store_spec(ctx), _pipeline_spec()], key=OPTIONS_KEY
        )
        if options:
            out["options_added"] = options
    except (*_FAILURES, ConfigError) as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        errors.append({"table": feishu_table.TABLE_NAME, "error": reason})
        out["error"] = reason
    return out


def sync_table(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """表格同步时把上架几列写成该有的样子（调用方持有选款表的同步租约）。先按库里的现状重算当前环节。"""
    if not enabled(ctx) or not ready(info):
        return {}
    try:
        trace.refresh(ctx)
        written = write_rows(ctx, api, info)
    except (*_FAILURES, ConfigError) as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        errors.append({"table": feishu_table.TABLE_NAME, "error": reason})
        return {"error": reason}
    return {"written": written} if written else {}


def has_table_work(ctx: Ctx, info: dict[str, Any] | None = None) -> bool:
    """「选中的款」还要补上架几列 / 选项，或有行的上架几列和该有的不一样。"""
    info = info if info is not None else feishu_table.base(ctx)
    if not info or not enabled(ctx) or not feishu_table.listing_ready(info):
        return False
    try:
        if not ready(info) or not pipeline_ready(info):
            return True
        return bool(_changes(ctx, None, _cache(ctx), extended=True))
    except ConfigError:
        return False


# ---------------------------------------------------------------- 飞书事件与巡检回读


def _read(api: feishu.Client, info: dict[str, Any], record_ids: list[str]) -> dict[str, dict[str, Any]]:
    """回读几行的「确认上架」和「上架标题」：{record_id: {confirm, title}}；表里已经没有的行不在结果里。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(record_ids), _PAGE):
        data = api.call(
            "POST",
            f"{path}/batch_get",
            json={"record_ids": record_ids[i : i + _PAGE], "user_id_type": "open_id"},
        )
        for item in data.get("records") or []:
            values = item.get("fields") or {}
            out[item["record_id"]] = {
                "confirm": downloads._ticked(values.get(CONFIRM_FIELD)),
                "title": listing._text(values.get(TITLE_FIELD)),
            }
    return out


def _operator_ok(ctx: Ctx, by: str) -> bool:
    allowed = {str(o) for o in ctx.setting("temu.operators") or []}
    return not allowed or by.removeprefix("feishu:") in allowed or by == downloads.POLL_BY


def _confirm_ticked(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    ticked: list[str],
    titles: dict[str, str | None],
    *,
    by: str,
) -> dict[str, Any]:
    """人勾上的这几款：确认上架；不能上的取消勾（没定店铺的备注写原因），当场写这几行。"""
    if _operator_ok(ctx, by):
        result = confirm(ctx, ticked, by=by, source="table", titles=titles)
    else:
        result = {
            "queued": [],
            "already": [],
            "rejected": [{"asset_id": a, "reason": "不在 temu.operators 里，不能发起上架"} for a in ticked],
        }
    rejected = {r["asset_id"] for r in result["rejected"]}
    if rejected:
        with ctx.db.tx() as conn:
            for item in result["rejected"]:
                log_event(conn, item["asset_id"], "temu_confirm_rejected", {"by": by, "reason": item["reason"][:300]})
    result.update(_write_now(ctx, api, info, ticked, untick=rejected))
    bare = [r for r in result["rejected"] if not stores.rows(ctx, asset_ids=[r["asset_id"]])]
    if bare:
        _untick_bare(ctx, api, info, bare)
    return result


def _write_now(ctx: Ctx, api: feishu.Client, info: dict[str, Any], asset_ids: list[str], **kwargs: Any) -> dict[str, Any]:
    try:
        return {"written": write_rows(ctx, api, info, asset_ids, **kwargs)}
    except _FAILURES as exc:
        feishu_table.kick(ctx)
        return {"write_error": f"{type(exc).__name__}: {exc}"[:300]}


def _untick_bare(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], rejected: list[dict[str, str]]
) -> None:
    """没定店铺的款勾了「确认上架」：取消勾、备注写原因（这几行程序不管别的格子）。"""
    records = _records(ctx)
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    for item in rejected:
        record_id = records.get(item["asset_id"])
        if not record_id:
            continue
        try:
            api.call(
                "PUT",
                f"{path}/{record_id}",
                params={"user_id_type": "open_id"},
                json={"fields": {CONFIRM_FIELD: False, NOTE_FIELD: f"没上架：{item['reason']}"[:300]}},
            )
        except feishu.FeishuError:
            continue


def table_event(
    ctx: Ctx, *, record_ids: list[str], by: str, info: dict[str, Any] | None = None
) -> dict[str, Any]:
    """「选中的款」的记录变更事件（网关转来有格子改成勾上的记录）：回读「确认上架」，人勾上的就确认上架；
    程序自己写的勾（事件回声）不处理。info 是事件所在的那一份「选中的款」（缺省旧表格的）。"""
    from . import books  # 每周选款表依赖表格模块，放到用时再导入

    info = info or feishu_table.base(ctx) or {}
    if not enabled(ctx) or not ready(info):
        return {"ignored": "上架没打开，或「选中的款」还没有「确认上架」列"}
    with books.scoped(ctx, info):
        return _table_event(ctx, info, record_ids, by)


def _table_event(
    ctx: Ctx, info: dict[str, Any], record_ids: list[str], by: str
) -> dict[str, Any]:
    rows = downloads._rows_by_record(ctx, record_ids)
    if not rows:
        return {"ignored": "改的不是选中的款里的行"}
    with feishu.Client(ctx) as api:
        seen = _read(api, info, list(rows))
        cache = _cache(ctx)
        groups = stores.by_asset(ctx, list(rows.values()))
        ticked = []
        resumed = []
        for record_id, asset_id in rows.items():
            got = seen.get(record_id)
            if not got or not got["confirm"] or _ours(cache, asset_id, record_id):
                continue
            items = groups.get(asset_id) or []
            if not items or any(r["state"] in (STATE_CHOSEN, STATE_FAILED) for r in items):
                ticked.append(asset_id)
            elif _paused_in_box(ctx, asset_id, items):
                resumed.append(asset_id)
        if not ticked and not resumed:
            return {"ignored": "确认上架没勾，或是程序写的勾"}
        out: dict[str, Any] = {}
        if resumed:
            out.update(_resume_box(ctx, api, info, resumed, by=by))
        if ticked:
            titles = {rows[r]: (seen.get(r) or {}).get("title") for r in rows}
            out.update(_confirm_ticked(ctx, api, info, ticked, titles, by=by))
        return out


def _resume_box(ctx: Ctx, api: feishu.Client, info: dict[str, Any], asset_ids: list[str], *, by: str) -> dict[str, Any]:
    """进了采集箱暂停着的款人又勾上「确认上架」：解除暂停，接着发布。"""
    resumed = release(ctx, asset_ids, by=by, source="table")
    out = {"resumed": resumed, **_write_now(ctx, api, info, asset_ids, force_check=set(asset_ids))}
    if resumed:
        kick(ctx)
    return out


def poll(ctx: Ctx) -> dict[str, Any] | None:
    """巡检兜底：回读整列「确认上架」。人勾上、还没确认的确认上架（事件漏掉的）；程序勾上、人取消了的：
    还没开始建箱就取消确认，已经在上 / 上过的把勾写回去。没开、还没有这一列时返回 None。
    旧表格和每周表格各天的「选中的款」各读一遍。"""
    from . import books  # 每周选款表依赖表格模块，放到用时再导入

    if not enabled(ctx):
        return None
    out: dict[str, Any] | None = None
    for info in books.scopes(ctx):
        if not ready(info):
            continue
        with books.scoped(ctx, info):
            part = _poll_scope(ctx, info)
        if out is None:
            out = part
            continue
        out["read"] += part["read"]
        for key in ("confirmed", "rejected", "cancelled", "reticked", "paused", "resumed"):
            out[key] += part[key]
        for key in ("written", "write_error"):
            if part.get(key):
                out[key] = part[key]
    return out


def _poll_scope(ctx: Ctx, info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "read": 0, "confirmed": [], "rejected": [], "cancelled": [], "reticked": [], "paused": [], "resumed": []
    }
    with feishu.Client(ctx) as api:
        cells = downloads.read_column(api, info, CONFIRM_FIELD)
        out["read"] = len(cells)
        rows = downloads._rows_by_record(ctx)
        cache = _cache(ctx)
        groups = stores.by_asset(ctx, list(rows.values()))
        tick: list[str] = []
        undo: list[str] = []
        retick: list[str] = []
        pause: list[str] = []
        resume: list[str] = []
        for record_id, asset_id in rows.items():
            if record_id not in cells:
                continue
            checked = downloads._ticked(cells[record_id])
            ours = _ours(cache, asset_id, record_id)
            items = groups.get(asset_id) or []
            if checked and not ours:
                if not items or any(r["state"] in (STATE_CHOSEN, STATE_FAILED) for r in items):
                    tick.append(asset_id)
                elif _paused_in_box(ctx, asset_id, items):
                    resume.append(asset_id)
            elif not checked and ours and items:
                if all(r["state"] in (STATE_QUEUED, STATE_CHOSEN) for r in items):
                    undo.append(asset_id)
                elif _pausable(ctx, items):
                    pause.append(asset_id)
                else:
                    retick.append(asset_id)
        if tick:
            titles_cells = downloads.read_column(api, info, TITLE_FIELD)
            titles = {a: listing._text(titles_cells.get(r)) for r, a in rows.items() if a in tick}
            result = _confirm_ticked(ctx, api, info, tick, titles, by=downloads.POLL_BY)
            out.update(confirmed=result["queued"], rejected=result["rejected"])
        if resume:
            out["resumed"] = _resume_box(ctx, api, info, resume, by=downloads.POLL_BY)["resumed"]
        if undo:
            out["cancelled"] = cancel(ctx, undo, by=downloads.POLL_BY)["cancelled"]
        if pause:
            # 全自动发布的店进了采集箱还没发布：不发布、勾保持取消（排着队的别的店一起取消确认），再勾接着上
            cancel(ctx, pause, by=downloads.POLL_BY)
            out["paused"] = hold(
                ctx, pause, by=downloads.POLL_BY, source="table", note="取消了「确认上架」（已进采集箱，先不发布）"
            )
        if undo or retick or pause:
            out["reticked"] = retick
            out.update(_write_now(ctx, api, info, undo + retick + pause, force_check=set(retick), untick=set(pause)))
    return out


# ---------------------------------------------------------------- 待办与红卡

# 卡在这些环节超过 temu.alert_stuck_hours 进「POD 待办」（等人确认 / 人自己暂停的不算卡）
_STUCK_STAGES = frozenset(
    {"code", "scenes_wait", "scenes", "assets", "queued", "box", "brand", "publish_wait", "publishing"}
)


def alerts(ctx: Ctx) -> list[dict[str, Any]]:
    """上架这一路要人看的：没上成、领号失败满次数、品牌写不上、卡在同一环节太久、图片链接快到期、标题不合规 / 没同步上、
    Temu 驳回。[{asset_id, store, code, kind, text}]（每（款, 店）一条，最要紧的那一件）。"""
    from . import title_sync  # 标题同步依赖本模块，放到用时再导入

    if not enabled(ctx):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(row: dict[str, Any], kind: str, text: str) -> None:
        key = (row["asset_id"], row["store_key"])
        if key in seen:
            return
        seen.add(key)
        try:
            label = stores.get(ctx, row["store_key"]).label
        except StageError:
            label = row["store_key"]
        out.append(
            {"asset_id": row["asset_id"], "store": label, "code": row.get("code"), "kind": kind, "text": text[:120]}
        )

    items = stores.rows(ctx)
    for row in items:
        if row["state"] == STATE_FAILED:
            add(row, "failed", f"没上成：{row.get('last_error') or ''}")
        elif not row.get("code") and int(row.get("code_attempts") or 0) >= stores.MAX_CODE_ATTEMPTS:
            add(row, "code", f"货号领不到：{row.get('code_error') or ''}（`temu codes --retry`）")
        elif row["state"] == STATE_BOX:
            note = brand_note(ctx, row)
            if note and "写不上" in note:
                add(row, "brand", note)
    hours = float(ctx.setting("temu.alert_stuck_hours", 6) or 0)
    if hours > 0:
        limit = utcnow() - dt.timedelta(hours=hours)
        by_key = {(r["asset_id"], r["store_key"]): r for r in items}
        for status_row in ctx.db.q("SELECT * FROM pipeline_status"):
            row = by_key.get((status_row["asset_id"], status_row["store_key"]))
            since = parse_iso(status_row["stage_since"])
            if not row or status_row["stage"] not in _STUCK_STAGES or not since or since > limit:
                continue
            label = trace.LABELS.get(status_row["stage"], status_row["stage"])
            reason = status_row["blocker"] or ""
            add(row, "stuck", f"在「{label}」卡了 {int((utcnow() - since).total_seconds() // 3600)} 小时" + (f"：{reason}" if reason else ""))
    floor = utcnow() + dt.timedelta(days=5)
    for row in items:
        if row["state"] != STATE_BOX or not row.get("set_id"):
            continue
        expires = parse_iso((temu_assets.get(ctx, row["asset_id"], row["set_id"]) or {}).get("links_expire_at"))
        if expires and expires < floor:
            add(row, "links", "妙手里的图片链接 5 天内到期，重签存回没成：看上架后台的出错")
    by_key = {(r["asset_id"], r["store_key"]): r for r in items}
    for item in title_sync.status(ctx):
        row = by_key.get((item["asset_id"], item["store_key"]))
        if row and item["state"] in (title_sync.STATE_INVALID, title_sync.STATE_FAILED, title_sync.STATE_WAITING_TEMU):
            add(row, "title", f"标题{title_sync.TEXT.get(item['state'], item['state'])}：{item.get('last_error') or ''}")
    for goods in ctx.db.q("SELECT * FROM temu_goods WHERE status IS NOT NULL"):
        if "驳回" in str(goods["status"]) or "reject" in str(goods["status"]).lower():
            row = next((r for r in items if r.get("code") == goods["code"] and r["store_key"] == goods["store_key"]), None)
            if row:
                add(row, "temu", f"Temu 驳回：{goods['status']}")
    return out


def notify_failed(ctx: Ctx, asset_ids: list[str]) -> dict[str, Any]:
    """这一轮刚记成没上成的款：发一张两行红卡（哪几款、为什么；去哪看、怎么重来）。发不出不重试，状态总结里照样有。"""
    from . import notify  # 通知依赖视图模块，放到用时再导入

    if not ctx.setting("temu.alert_failures", True):
        return {"sent": False, "reason": "temu.alert_failures 关着"}
    rows = [r for r in _rows(ctx, (STATE_FAILED,), asset_ids=sorted(set(asset_ids)))]
    if not rows:
        return {"sent": False, "reason": "没有没上成的"}
    names = labels.assets(ctx, sorted({r["asset_id"] for r in rows}))
    shown = "、".join(
        f"{(names.get(r['asset_id']) or {}).get('label') or r['asset_id']} {stores.get(ctx, r['store_key']).label}"
        f" {r['code'] or ''}".strip()
        for r in rows[:3]
    ) + (f" 等 {len(rows)} 个" if len(rows) > 3 else "")
    error = next((r["last_error"] for r in rows if r.get("last_error")), "")
    card = notify.pod_card(
        f"上架没成｜{len(rows)} 个（款, 店）",
        [
            f"{shown}：{error[:80]}",
            "原因在「选中的款」的上架备注；改好在「选中的款」再勾「确认上架」，或在 POD 对话说「IMG… 重新上架」",
        ],
        level="alert",
        buttons=[("打开工作台", {"action": notify.CARD_ACTION_DASHBOARD}, "default")],
    )
    try:
        return notify.send_pod_card(ctx, card, f"上架没成：{shown}")
    except notify.NotifyError as exc:
        return {"sent": False, "error": str(exc)}


# ---------------------------------------------------------------- 状态与预览


def _grace_minutes(ctx: Ctx) -> float:
    from . import candidate_stores  # 候选表店铺列依赖上架模块，放到用时再导入

    return candidate_stores.grace_minutes(ctx)


def status(ctx: Ctx, *, asset_ids: list[str] | None = None, seed_id: str | None = None) -> dict[str, Any]:
    items = stores.rows(ctx, asset_ids=asset_ids, seed_id=seed_id)
    names = labels.assets(ctx, sorted({r["asset_id"] for r in items}))
    counts: dict[str, int] = {}
    for row in items:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    pipeline = {
        (r["asset_id"], r["store_key"]): dict(r)
        for r in ctx.db.q("SELECT * FROM pipeline_status")
    }
    from . import mooya_listing  # mooya 模式的模块依赖本模块，用时再导入

    return {
        "enabled": enabled(ctx),
        "engine": mooya_listing.status(ctx),
        "publish": ctx.setting("temu.publish", MODE_BOX),
        "auto": {
            "stores": sorted(auto_stores(ctx)),
            "confirm": bool(ctx.setting("temu.auto.confirm", True)),
            "publish": bool(ctx.setting("temu.auto.publish", True)),
            "tick_grace_minutes": _grace_minutes(ctx),
        },
        "held": [r["asset_id"] for r in ctx.db.q("SELECT asset_id FROM listing_holds ORDER BY held_at")],
        "service_error": _service_error(ctx).get("error"),
        "checked_at": ctx.db.get_meta(CHECKED_META_KEY),
        "counts": counts,
        "items": [
            {
                "asset_id": r["asset_id"],
                "label": (names.get(r["asset_id"]) or {}).get("label"),
                "store": stores.get(ctx, r["store_key"]).column,
                "code": r["code"],
                "state": r["state"],
                "state_text": STATE_TEXT.get(r["state"], r["state"]),
                "note": note_line(ctx, r),
                "stage": trace.LABELS.get((pipeline.get((r["asset_id"], r["store_key"])) or {}).get("stage") or ""),
                "stage_since": (pipeline.get((r["asset_id"], r["store_key"])) or {}).get("stage_since"),
                "detail_id": r["detail_id"],
                "brand": _brand_status(ctx, r),
                "title": (temu_title.get(ctx, r["asset_id"]) or {}).get("title"),
                "listed_at": r["listed_at"],
            }
            for r in items
        ],
    }


def preview(ctx: Ctx, asset_id: str, store_ref: str | None = None) -> dict[str, Any]:
    """不建箱：看这一款会用的图、标题、货号、样板和要发的内容（会读一次样板）。
    上架交给 mooya 时看 mooya 那边的上架单（图位、标题、品牌、等待原因、轨迹）。"""
    items = stores.rows(ctx, asset_ids=[asset_id])
    if store_ref:
        key = stores.resolve(ctx, store_ref).key
        items = [r for r in items if r["store_key"] == key]
    if _mooya_engine(ctx):
        from . import mooya_listing  # 同上

        out = []
        for row in items:
            found = mooya_listing.mapping(ctx, row["asset_id"], row["store_key"])
            if not found:
                out.append({"store": row["store_key"], "code": row["code"], "state": row["state"], "mooya": None,
                            "waiting": mooya_listing.blocker(ctx, row)})
                continue
            with mooya.Client(ctx, client=found["client"]) as api:
                out.append({"store": row["store_key"], "code": row["code"], "mooya": api.temu_listing(int(found["listing_id"]))})
        return {"asset_id": asset_id, "engine": "mooya", "stores": out}
    if not items:
        raise StageError(f"{asset_id} 没定店铺")
    item = temu_assets.current(ctx, asset_id)
    title = temu_title.get(ctx, asset_id)
    out: dict[str, Any] = {
        "asset_id": asset_id,
        "label": labels.asset(ctx, asset_id),
        "readiness": temu_assets.readiness(ctx, asset_id),
        "images": [
            {"n": f["n"], "title": f.get("title"), "plan_id": f.get("plan_id"), "url": f.get("url")}
            for f in (item or {}).get("files") or []
        ],
        "title": title,
        "stores": [],
    }
    with miaoshou.Client(ctx) as api:
        for row in items:
            store_ = stores.get(ctx, row["store_key"])
            entry: dict[str, Any] = {
                "store": store_.column,
                "code": row["code"],
                "state": row["state"],
                "blocker": blocker(ctx, row),
            }
            try:
                tpl = template(ctx, api, store_, refresh=True)
                entry["sites"] = shop_sites(store_, tpl)
                entry["prices"] = prices(tpl)
                if item and title and row["code"]:
                    entry["payload"] = build_payload(
                        store_, tpl, code=row["code"], title=title["title"], images=temu_assets.urls(item)
                    )
            except (miaoshou.MiaoshouError, StageError) as exc:
                entry["error"] = str(exc)
            out["stores"].append(entry)
    return out
