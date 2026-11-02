"""Temu 官方 Partner 接口（2026-10-30，feature-plans/pod-listing-autopilot）：上架后按货号找到 Temu 商品 ID、
定时回读审核 / 在售状态，已上架的款改标题（妙手开放平台改不了已发布的 Temu 半托商品）。缺省关着（`temu_open.enabled`）。

**可行性待验证**（半托店能不能授权、走哪个网关和接口族、签名写法；先跑只读的 `podctl temu open probe`）：
- 凭据：应用的 app_key / app_secret 在 secrets.env（名字 `temu_open.app_key_name` / `app_secret_name`）；每家店在跨境卖家中心
  「系统管理 → 授权管理」授权后拿到的 access_token 也放 secrets.env，名字在 `temu_open.stores.<店铺 key>.token_name`，
  到期日写 `temu_open.stores.<店铺 key>.expires_on`（doctor 提前 TOKEN_WARN_DAYS 天提醒）。
- 网关 `temu_open.base_url`（缺省 US：https://openapi-b-us.temu.com/openapi/router），POST JSON：公共参数 type / app_key /
  access_token / timestamp / data_type，业务参数放顶层（`temu_open.wrap_request: true` 时包在 request 里），最后加 sign。
- 签名（Temu Partner 的写法）：除 sign 外的参数按键排序，键和值依次拼起来（对象 / 数组按紧凑 JSON），前后各拼一次
  app_secret，MD5 取大写 hex。
- 接口名可改：`temu_open.api.list`（缺省 bg.local.goods.list.query）、`temu_open.api.update`（bg.local.goods.partial.update）。
- 错误：success 为假按 errorCode 分：鉴权 / 权限 / 签名（service，整轮停、退避）、限流 / 系统（transient，下一轮再试）、其余 invalid。

用法（`sync`，上架后台每一轮调）：listed 的（款, 店）没有 goodsId 的按标题搜、按货号（outGoodsSn / outSkuSnList）核对，
记进 temu_goods；每 `temu_open.poll_hours` 小时回读状态，写进 temu_goods、listed_products（Temu 状态、商品 ID、商品链接）
和 listed_metrics（source api）；title_sync 里要走 Temu 的标题：提交编辑（进 Temu 审核），读回商品标题是新的就算生效。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import http, stores, trace
from .context import Ctx
from .util import dumps, loads, now_iso, parse_iso, utcnow

DEFAULT_BASE_URL = "https://openapi-b-us.temu.com/openapi/router"
DEFAULT_KEY_NAME = "TEMU_APP_KEY"
DEFAULT_SECRET_NAME = "TEMU_APP_SECRET"
DEFAULT_LIST_API = "bg.local.goods.list.query"
DEFAULT_UPDATE_API = "bg.local.goods.partial.update"
SERVICE_META_KEY = "temu_open.service_error"
CHECKED_META_KEY = "temu_open.checked_at"
SERVICE_BACKOFF_MINUTES = 30
TOKEN_WARN_DAYS = 7
MAX_FIND_ATTEMPTS = 12
PAGE_SIZE = 100
KIND_SERVICE = "service"
KIND_TRANSIENT = "transient"
KIND_INVALID = "invalid"
# 错误码里能认出来的几类（以可行性验证为准，认不出的按 invalid）
_SERVICE_HINTS = (
    "sign",
    "token",
    "auth",
    "permission",
    "app_key",
    "appkey",
    "forbidden",
)
_TRANSIENT_HINTS = ("limit", "frequency", "busy", "timeout", "system")


class TemuOpenError(RuntimeError):
    def __init__(self, message: str, *, kind: str, code: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def enabled(ctx: Ctx) -> bool:
    """开着 temu_open.enabled、配了 app_key / app_secret。"""
    if not ctx.setting("temu_open.enabled", False):
        return False
    return bool(ctx.secrets.get(_key_name(ctx)) and ctx.secrets.get(_secret_name(ctx)))


def _key_name(ctx: Ctx) -> str:
    return str(ctx.setting("temu_open.app_key_name") or DEFAULT_KEY_NAME)


def _secret_name(ctx: Ctx) -> str:
    return str(ctx.setting("temu_open.app_secret_name") or DEFAULT_SECRET_NAME)


def store_conf(ctx: Ctx, store_key: str) -> dict[str, Any]:
    conf = (ctx.setting("temu_open.stores") or {}).get(store_key)
    return conf if isinstance(conf, dict) else {}


def token_for(ctx: Ctx, store_key: str) -> str | None:
    name = store_conf(ctx, store_key).get("token_name")
    return ctx.secrets.get(str(name)) if name else None


def sign(secret: str, params: dict[str, Any]) -> str:
    """Temu Partner 的签名：除 sign 外按键排序、键值拼接（对象 / 数组用紧凑 JSON），前后拼 secret，MD5 大写。"""
    parts = []
    for key in sorted(params):
        if key == "sign":
            continue
        value = params[key]
        if isinstance(value, dict | list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}{value}")
    raw = f"{secret}{''.join(parts)}{secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _kind(code: Any, message: str) -> str:
    text = f"{code} {message}".lower()
    if any(h in text for h in _SERVICE_HINTS):
        return KIND_SERVICE
    if any(h in text for h in _TRANSIENT_HINTS):
        return KIND_TRANSIENT
    return KIND_INVALID


class Client:
    """一家店的 Temu 接口客户端（这家店的 access_token）。"""

    def __init__(self, ctx: Ctx, store_key: str) -> None:
        self.store_key = store_key
        self.base_url = str(ctx.setting("temu_open.base_url") or DEFAULT_BASE_URL)
        self.app_key = ctx.secrets.get(_key_name(ctx)) or ""
        self.secret = ctx.secrets.get(_secret_name(ctx)) or ""
        self.token = token_for(ctx, store_key) or ""
        self.wrap = bool(ctx.setting("temu_open.wrap_request", False))
        self.list_api = str(ctx.setting("temu_open.api.list") or DEFAULT_LIST_API)
        self.update_api = str(ctx.setting("temu_open.api.update") or DEFAULT_UPDATE_API)
        self.min_interval = float(ctx.setting("temu_open.min_interval_seconds", 1) or 0)
        if not self.app_key or not self.secret:
            raise TemuOpenError(
                "secrets.env 里缺 Temu 应用的 app_key / app_secret", kind=KIND_SERVICE
            )
        if not self.token:
            raise TemuOpenError(
                f"{store_key} 没有 access_token（temu_open.stores.{store_key}.token_name 指向 secrets.env 里的名字）",
                kind=KIND_SERVICE,
            )
        self._http = http.make_client(
            float(ctx.setting("temu_open.timeout_seconds") or 30)
        )
        self._last = 0.0

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def call(self, api_type: str, business: dict[str, Any]) -> dict[str, Any]:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        params: dict[str, Any] = {
            "type": api_type,
            "app_key": self.app_key,
            "access_token": self.token,
            "timestamp": str(int(time.time())),
            "data_type": "JSON",
        }
        if self.wrap:
            params["request"] = business
        else:
            params.update(business)
        params["sign"] = sign(self.secret, params)
        body = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        try:
            resp = self._http.post(
                self.base_url,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise TemuOpenError(
                f"连不上 Temu 接口：{type(exc).__name__}: {exc}", kind=KIND_TRANSIENT
            ) from exc
        finally:
            self._last = time.monotonic()
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TemuOpenError(
                f"Temu 接口 HTTP {resp.status_code}",
                kind=KIND_TRANSIENT,
                code=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise TemuOpenError(
                f"Temu 接口返回的不是 JSON（HTTP {resp.status_code}）",
                kind=KIND_INVALID,
            ) from exc
        if not data.get("success", False):
            code = data.get("errorCode")
            message = str(data.get("errorMsg") or data.get("message") or data)[:300]
            raise TemuOpenError(
                f"Temu 接口出错 {code}：{message}", kind=_kind(code, message), code=code
            )
        return data.get("result") or {}

    # ---- 商品

    def list_goods(self, **query: Any) -> dict[str, Any]:
        business = {
            "pageNo": 1,
            "pageSize": PAGE_SIZE,
            "goodsSearchType": 1,
            "goodsStatusFilterType": 1,
            **query,
        }
        return self.call(self.list_api, business)

    def update_title(self, goods_id: str | int, title: str) -> dict[str, Any]:
        return self.call(
            self.update_api,
            {"goodsId": int(goods_id), "goodsBasic": {"goodsName": title}},
        )


def _codes_of(item: dict[str, Any]) -> set[str]:
    out = {str(item.get("outGoodsSn") or "")}
    out |= {str(s) for s in item.get("outSkuSnList") or []}
    for sku in item.get("skuInfoList") or []:
        if isinstance(sku, dict):
            out.add(str(sku.get("outSkuSn") or ""))
    return {c for c in out if c}


def status_text(item: dict[str, Any]) -> str | None:
    """商品状态的说明文字（接口给的字段以验证为准：先找带说明的，再退回状态码）。"""
    for key in ("subStatus4VO", "status4VO"):
        value = item.get(key)
        if isinstance(value, dict):
            text = value.get("desc") or value.get("statusDesc") or value.get("name")
            if text:
                return str(text)
    for key in ("goodsShowSubStatus", "subStatus", "goodsStatus", "status"):
        if item.get(key) not in (None, ""):
            return str(item[key])
    return None


def find_goods(
    client: Client, *, code: str, title: str, since: dt.datetime | None
) -> dict[str, Any] | None:
    """按标题搜（Temu 的商品列表不能按货号筛），再按货号核对；在售和还没发布的都找。"""
    query: dict[str, Any] = {"searchText": title[:120]} if title else {}
    if since:
        query["crtFrom"] = int((since - dt.timedelta(days=1)).timestamp() * 1000)
    for status in (1, 4):
        result = client.list_goods(
            goodsSearchType=status, goodsStatusFilterType=status, **query
        )
        for item in result.get("goodsList") or []:
            if code in _codes_of(item):
                return item
    return None


# ---------------------------------------------------------------- 后台


def _service_error(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(SERVICE_META_KEY), None) or {}


def _backing_off(ctx: Ctx) -> bool:
    at = parse_iso(_service_error(ctx).get("at"))
    return bool(at and utcnow() - at < dt.timedelta(minutes=SERVICE_BACKOFF_MINUTES))


def _record_service_error(ctx: Ctx, exc: BaseException) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn, SERVICE_META_KEY, dumps({"error": str(exc)[:300], "at": now_iso()})
        )


def _goods(ctx: Ctx, code: str, store_key: str) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT * FROM temu_goods WHERE code = ? AND store_key = ?", (code, store_key)
    )
    return dict(row) if row else None


def _save_goods(ctx: Ctx, code: str, store_key: str, **values: Any) -> None:
    values = {"updated_at": now_iso(), **values}
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO temu_goods(code, store_key, {cols}) VALUES(?, ?, {marks})"
            f" ON CONFLICT(code, store_key) DO UPDATE SET {sets}",
            (code, store_key, *values.values()),
        )


def _goods_url(ctx: Ctx, goods_id: str) -> str | None:
    template = str(ctx.setting("temu_open.goods_url") or "")
    return template.format(goods_id=goods_id) if template and goods_id else None


def _due(ctx: Ctx) -> bool:
    last = parse_iso(ctx.db.get_meta(CHECKED_META_KEY))
    hours = float(ctx.setting("temu_open.poll_hours", 6) or 0)
    return last is None or (hours > 0 and utcnow() - last >= dt.timedelta(hours=hours))


def sync(ctx: Ctx, touched: set[str] | None = None) -> dict[str, Any]:
    """一轮：给已上架、还没有 goodsId 的找 goodsId；到点回读状态；提交 / 核对要走 Temu 的标题。出错按店停这一家。"""
    from . import listed_table, title_sync  # 都依赖上架模块，用时再导入

    out: dict[str, Any] = {"found": [], "refreshed": 0, "titles": [], "errors": []}
    if not enabled(ctx) or _backing_off(ctx):
        return {**out, "skipped": True}
    touched = touched if touched is not None else set()
    listed = [r for r in stores.rows(ctx) if r["state"] == "listed" and r.get("code")]
    refresh = _due(ctx)
    by_store: dict[str, list[dict[str, Any]]] = {}
    for row in listed:
        if token_for(ctx, row["store_key"]):
            by_store.setdefault(row["store_key"], []).append(row)
    pending_titles = {
        (t["asset_id"], t["store_key"]): t for t in title_sync.temu_targets(ctx)
    }
    for store_key, rows in by_store.items():
        try:
            with Client(ctx, store_key) as api:
                for row in rows:
                    goods = _goods(ctx, row["code"], store_key) or {}
                    if not goods.get("goods_id"):
                        if int(goods.get("attempts") or 0) >= MAX_FIND_ATTEMPTS:
                            continue
                        item = find_goods(
                            api,
                            code=row["code"],
                            title=row.get("title") or "",
                            since=parse_iso(row.get("listed_at")),
                        )
                        if not item:
                            _save_goods(
                                ctx,
                                row["code"],
                                store_key,
                                attempts=int(goods.get("attempts") or 0) + 1,
                                last_error="还没在 Temu 商品列表里找到（刚发布要等一阵）",
                            )
                            continue
                        goods = _remember(ctx, row, item)
                        out["found"].append(row["code"])
                        touched.add(row["asset_id"])
                    elif refresh:
                        result = api.list_goods(goodsIdList=[int(goods["goods_id"])])
                        item = next(iter(result.get("goodsList") or []), None)
                        if item:
                            goods = _remember(ctx, row, item)
                            out["refreshed"] += 1
                            touched.add(row["asset_id"])
                    task = pending_titles.get((row["asset_id"], store_key))
                    if task and goods.get("goods_id"):
                        out["titles"].append(_title(ctx, api, row, goods, task))
                        touched.add(row["asset_id"])
        except TemuOpenError as exc:
            out["errors"].append({"store": store_key, "error": str(exc)[:300]})
            if exc.kind == KIND_SERVICE:
                _record_service_error(ctx, exc)
                break
            continue
    if refresh:
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, CHECKED_META_KEY, now_iso())
    if out["found"] or out["refreshed"] or out["titles"]:
        listed_table.sync_now(ctx)
    return out


def _remember(ctx: Ctx, row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """接口读到的商品：记进 temu_goods、listed_products（状态、商品 ID、链接）和 listed_metrics（source api）。"""
    from . import listed_table  # 已上架的款依赖上架模块，用时再导入

    goods_id = str(item.get("goodsId") or "")
    status = status_text(item)
    old = _goods(ctx, row["code"], row["store_key"]) or {}
    _save_goods(
        ctx,
        row["code"],
        row["store_key"],
        goods_id=goods_id,
        sku_ids_json=dumps(item.get("skuIdList") or []),
        status=status,
        sub_status=str(item.get("goodsShowSubStatus") or "") or None,
        goods_name=item.get("goodsName"),
        raw_json=dumps(item),
        found_at=old.get("found_at") or now_iso(),
        checked_at=now_iso(),
        last_error=None,
    )
    if old.get("status") != status or not old.get("goods_id"):
        trace.log(
            ctx,
            row["asset_id"],
            "temu",
            frm=old.get("status"),
            to=status,
            store=row["store_key"],
            code=row["code"],
            source="temu_open",
            payload={"goods_id": goods_id},
        )
    url = _goods_url(ctx, goods_id)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE listed_products SET temu_status = ?, temu_product_id = ?, product_url = COALESCE(?, product_url),"
            " written_hash = NULL, updated_at = ? WHERE code = ?",
            (status, goods_id or None, url, now_iso(), row["code"]),
        )
    listed = ctx.db.q1("SELECT * FROM listed_products WHERE code = ?", (row["code"],))
    if listed:
        listed_table.record_metrics(
            ctx, dict(listed), source="api", extra={"goods_id": goods_id}
        )
    return _goods(ctx, row["code"], row["store_key"]) or {}


def _title(
    ctx: Ctx,
    api: Client,
    row: dict[str, Any],
    goods: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    """一（款, 店）的标题：读回已经是新标题就记生效；还没提交的提交编辑（进 Temu 审核）。"""
    from . import title_sync  # 标题同步依赖本模块，用时再导入

    if (goods.get("goods_name") or "").strip() == task["title"].strip():
        title_sync.mark(
            ctx,
            row["asset_id"],
            row["store_key"],
            title_sync.STATE_DONE,
            last_error=None,
        )
        return {"code": row["code"], "state": "done"}
    if task["state"] == title_sync.STATE_TEMU_SUBMITTED:
        return {"code": row["code"], "state": "waiting_review"}
    try:
        result = api.update_title(goods["goods_id"], task["title"])
    except TemuOpenError as exc:
        if exc.kind != KIND_INVALID:
            raise
        attempts = int(task.get("attempts") or 0) + 1
        title_sync.mark(
            ctx,
            row["asset_id"],
            row["store_key"],
            title_sync.STATE_FAILED
            if attempts >= title_sync.MAX_ATTEMPTS
            else title_sync.STATE_PENDING,
            attempts=attempts,
            last_error=str(exc)[:300],
        )
        return {"code": row["code"], "state": "failed", "error": str(exc)[:200]}
    title_sync.mark(
        ctx,
        row["asset_id"],
        row["store_key"],
        title_sync.STATE_TEMU_SUBMITTED,
        modify_id=str(result.get("modifyId") or "") or None,
        submitted_at=now_iso(),
        last_error=None,
    )
    return {"code": row["code"], "state": "submitted"}


def probe(ctx: Ctx) -> dict[str, Any]:
    """只读探测（可行性验证）：凭据、每家店的 token、商品列表第一页、已上架的款能不能按货号找到 goodsId。不写库。"""
    out: dict[str, Any] = {
        "enabled": bool(ctx.setting("temu_open.enabled", False)),
        "base_url": str(ctx.setting("temu_open.base_url") or DEFAULT_BASE_URL),
        "credentials": bool(
            ctx.secrets.get(_key_name(ctx)) and ctx.secrets.get(_secret_name(ctx))
        ),
        "stores": [],
    }
    for store_ in stores.all_stores(ctx):
        entry: dict[str, Any] = {
            "store": store_.column,
            "token": bool(token_for(ctx, store_.key)),
        }
        if not out["credentials"] or not entry["token"]:
            out["stores"].append(entry)
            continue
        try:
            with Client(ctx, store_.key) as api:
                first = api.list_goods(pageSize=5)
                entry["total"] = first.get("total")
                entry["sample"] = [
                    {
                        k: g.get(k)
                        for k in (
                            "goodsId",
                            "goodsName",
                            "outGoodsSn",
                            "goodsShowSubStatus",
                        )
                    }
                    for g in (first.get("goodsList") or [])[:5]
                ]
                entry["listed"] = []
                for row in [
                    r
                    for r in stores.rows(ctx)
                    if r["store_key"] == store_.key and r["state"] == "listed"
                ][:5]:
                    item = find_goods(
                        api,
                        code=row["code"],
                        title=row.get("title") or "",
                        since=parse_iso(row.get("listed_at")),
                    )
                    entry["listed"].append(
                        {
                            "code": row["code"],
                            "goods_id": (item or {}).get("goodsId"),
                            "status": status_text(item or {}),
                        }
                    )
        except TemuOpenError as exc:
            entry.update(error=str(exc), kind=exc.kind)
        out["stores"].append(entry)
    return out


def doctor_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """doctor 里的一项：开着 temu_open 时凭据、每家店 token 和到期日。"""
    if not ctx.setting("temu_open.enabled", False):
        return [
            {
                "check": "Temu 官方接口（temu_open）",
                "ok": True,
                "level": "ok",
                "detail": "没开（已上架的款改标题要人去 Temu 后台改）",
            }
        ]
    problems = []
    if not (ctx.secrets.get(_key_name(ctx)) and ctx.secrets.get(_secret_name(ctx))):
        problems.append(f"secrets.env 缺 {_key_name(ctx)} / {_secret_name(ctx)}")
    today = (
        utcnow()
        .astimezone(ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai"))))
        .date()
    )
    for store_ in stores.all_stores(ctx):
        if not token_for(ctx, store_.key):
            problems.append(f"{store_.column} 没有 access_token")
            continue
        expires = store_conf(ctx, store_.key).get("expires_on")
        if expires:
            try:
                left = (dt.date.fromisoformat(str(expires)) - today).days
            except ValueError:
                problems.append(f"{store_.column} 的 expires_on 写错了：{expires}")
                continue
            if left <= TOKEN_WARN_DAYS:
                problems.append(
                    f"{store_.column} 的 access_token {left} 天后到期，去卖家中心重新授权"
                )
    error = _service_error(ctx).get("error")
    if error:
        problems.append(f"上次出错：{error}")
    return [
        {
            "check": "Temu 官方接口（temu_open）",
            "ok": not problems,
            "level": "warn" if problems else "ok",
            "detail": "；".join(problems) or "凭据和各店 token 都在",
        }
    ]
