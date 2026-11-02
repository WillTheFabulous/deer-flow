"""店铺与货号（2026-10-28，`temu.stores`）：候选表里每家店一列勾选框，勾哪列就上哪家店；选完店铺就向 mooya 领货号。

- 店铺配置 `temu.stores`：每家 {key, label, name, prefix, shop_id, template_detail_id, brand, brand_id}，顺序 = 候选表店铺列的顺序。
  brand / brand_id 是建箱后写进妙手的品牌（店铺模式详情的 brandId，样板克隆带不过去，见 temu._ensure_brand）；不配就不写。
  `temu.enabled` 关着时没有店铺：候选表不加列，验收照旧只看「选中」。
- 一款 × 一店一行（表 store_listings）：选款时记下店铺（state chosen），之后领货号、上架都在这一行推进（temu.py）。
- 货号由 mooya 分配（`POST /api/service/v1/codes`，按店铺前缀续号，同一个 ref 拿回同一个号）：
  ref = pod-blanket:<库编号>:<素材>:<店铺>。用 `mooya.submit_client` 那个客户端，它绑的租户就是序列所在的租户。
- 交给 mooya 出套图时用这一款第一家店（配置顺序）的货号当编号（scenes._submit），mooya 的交付包就叫这个名字；
  定了店铺、还没领到货号的款一律先不交（waiting_codes，2026-10-30 起领号失败满次数也不放行，免得交付包名和货号对不上）。
- 领号失败（2026-10-30）：mooya 连不上、5xx、限流、令牌 / 租户配置错只记原因、不算次数（下一轮再领）；
  只有这一单本身的错（其余 4xx）算一次，满 MAX_CODE_ATTEMPTS 停，进「POD 待办」，`temu codes --retry` 清零。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import labels, mooya, trace
from .config import ConfigError
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import now_iso

PREFIX_RE = re.compile(r"^[A-Za-z]{1,6}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
# 领货号连续失败这么多次不再自动试（`temu codes --retry` 清零）
MAX_CODE_ATTEMPTS = 8
STATE_CHOSEN = "chosen"
STATE_CANCELLED = "cancelled"
# 选款时从哪来：table = 候选表的店铺列，chat = POD 对话 / 命令
SOURCES = ("table", "chat")


@dataclass(frozen=True)
class Store:
    key: str
    label: str
    name: str
    prefix: str
    shop_id: str
    template_detail_id: str
    brand: str = ""
    brand_id: str = ""

    @property
    def column(self) -> str:
        """候选表里这家店那一列的列名，如「3店 CozWeave H」。"""
        return f"{self.label} {self.name}".strip()

    @property
    def title(self) -> str:
        return self.column


def all_stores(ctx: Ctx) -> list[Store]:
    """`temu.stores` 里配的店铺（不看开关）；写错了抛 ConfigError（doctor 报出来）。"""
    out: list[Store] = []
    seen: set[str] = set()
    for i, raw in enumerate(ctx.setting("temu.stores") or [], 1):
        if not isinstance(raw, dict):
            raise ConfigError(f"temu.stores 第 {i} 项要是一个对象")
        key = str(raw.get("key") or "").strip()
        prefix = str(raw.get("prefix") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not KEY_RE.match(key):
            raise ConfigError(f"temu.stores 第 {i} 项的 key 只能是小写字母开头的字母数字：{key!r}")
        if key in seen:
            raise ConfigError(f"temu.stores 里 key {key} 重复了")
        if not PREFIX_RE.match(prefix):
            raise ConfigError(f"temu.stores {key} 的 prefix 只能是 1–6 个英文字母：{prefix!r}")
        if not label:
            raise ConfigError(f"temu.stores {key} 缺 label（如「3店」）")
        brand_id = str(raw.get("brand_id") or "").strip()
        if brand_id and not brand_id.isdigit():
            raise ConfigError(f"temu.stores {key} 的 brand_id 要是妙手里的数字品牌 ID：{brand_id!r}")
        seen.add(key)
        out.append(
            Store(
                key=key,
                label=label,
                name=str(raw.get("name") or "").strip(),
                prefix=prefix,
                shop_id=str(raw.get("shop_id") or "").strip(),
                template_detail_id=str(raw.get("template_detail_id") or "").strip(),
                brand=str(raw.get("brand") or "").strip(),
                brand_id=brand_id,
            )
        )
    columns = [s.column for s in out]
    if len(set(columns)) != len(columns):
        raise ConfigError("temu.stores 里有两家店的「label name」一样，候选表的列名会撞")
    return out


def stores(ctx: Ctx) -> list[Store]:
    """开着 temu.enabled 时配的店铺；关着为空。"""
    if not ctx.setting("temu.enabled", False):
        return []
    return all_stores(ctx)


def enabled(ctx: Ctx) -> bool:
    """开着 temu.enabled 且配了店铺；店铺配置写错了算关着（doctor 报出来），不让巡检、表格同步跟着出错。"""
    try:
        return bool(stores(ctx))
    except ConfigError:
        return False


def get(ctx: Ctx, key: str) -> Store:
    for store in all_stores(ctx):
        if store.key == key:
            return store
    raise StageError(f"temu.stores 里没有店铺 {key}")


def resolve(ctx: Ctx, ref: str) -> Store:
    """对话 / 命令里说的店铺：key、label（3店）、店名或列名都认。"""
    text = (ref or "").strip()
    for store in all_stores(ctx):
        if text in (store.key, store.label, store.name, store.column):
            return store
    raise StageError(
        f"不认识店铺 {ref!r}：可以写 "
        + " / ".join(f"{s.label}（{s.name}）" for s in all_stores(ctx))
    )


def column_stores(ctx: Ctx) -> dict[str, Store]:
    """候选表列名 → 店铺。"""
    return {s.column: s for s in stores(ctx)}


def _order(ctx: Ctx) -> dict[str, int]:
    try:
        return {s.key: i for i, s in enumerate(all_stores(ctx))}
    except ConfigError:
        return {}


# ---------------------------------------------------------------- 选店铺


def rows(
    ctx: Ctx,
    *,
    asset_ids: list[str] | None = None,
    seed_id: str | None = None,
    active: bool = True,
) -> list[dict[str, Any]]:
    """store_listings 的行（active：去掉已取消的），按素材、店铺配置顺序排。"""
    sql = "SELECT * FROM store_listings WHERE 1 = 1"
    params: list[Any] = []
    if active:
        sql += " AND state <> ?"
        params.append(STATE_CANCELLED)
    if seed_id:
        sql += " AND seed_id = ?"
        params.append(seed_id)
    out: list[dict[str, Any]] = []
    if asset_ids is not None:
        ids = sorted(set(asset_ids))
        for i in range(0, len(ids), labels.BATCH):
            chunk = ids[i : i + labels.BATCH]
            out += [
                dict(r)
                for r in ctx.db.q(
                    sql + f" AND asset_id IN ({','.join('?' for _ in chunk)})",
                    (*params, *chunk),
                )
            ]
    else:
        out = [dict(r) for r in ctx.db.q(sql, tuple(params))]
    order = _order(ctx)
    return sorted(out, key=lambda r: (r["asset_id"], order.get(r["store_key"], 99)))


def by_asset(
    ctx: Ctx, asset_ids: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows(ctx, asset_ids=asset_ids):
        out.setdefault(row["asset_id"], []).append(row)
    return out


def _seed_of(ctx: Ctx, asset_id: str) -> str:
    seed = ctx.db.scalar(
        "SELECT f.seed_id FROM assets a JOIN variants v ON v.variant_id = a.variant_id"
        " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " WHERE a.asset_id = ?",
        (asset_id,),
    )
    if not seed:
        raise StageError(f"没有素材 {asset_id}")
    return seed


def choose(
    ctx: Ctx, asset_id: str, store_keys: list[str], *, by: str, source: str = "table"
) -> list[str]:
    """这一款要上这几家店（只加不减，已经有的不动）。返回新加的店铺 key。"""
    known = {s.key for s in all_stores(ctx)}
    keys = [k for k in dict.fromkeys(store_keys) if k in known]
    if not keys:
        return []
    seed_id = _seed_of(ctx, asset_id)
    added: list[str] = []
    now = now_iso()
    with ctx.db.tx() as conn:
        for key in keys:
            old = conn.execute(
                "SELECT state FROM store_listings WHERE asset_id = ? AND store_key = ?",
                (asset_id, key),
            ).fetchone()
            if old and old[0] != STATE_CANCELLED:
                continue
            if old:
                conn.execute(
                    "UPDATE store_listings SET state = ?, chosen_by = ?, chosen_at = ?, source = ?,"
                    " updated_at = ? WHERE asset_id = ? AND store_key = ?",
                    (STATE_CHOSEN, by, now, source, now, asset_id, key),
                )
            else:
                conn.execute(
                    "INSERT INTO store_listings(asset_id, store_key, seed_id, state, chosen_by, chosen_at,"
                    " source, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (asset_id, key, seed_id, STATE_CHOSEN, by, now, source, now, now),
                )
            added.append(key)
            trace.record(
                conn,
                asset_id,
                "store",
                frm=old[0] if old else None,
                to=STATE_CHOSEN,
                store=key,
                actor=by,
                source=source,
            )
        if added:
            log_event(
                conn, asset_id, "store_chosen", {"stores": added, "by": by, "source": source}
            )
    return added


def remove(
    ctx: Ctx,
    asset_id: str,
    keys: list[str],
    *,
    by: str,
    source: str,
    reason: str | None = None,
    allow_queued: bool = False,
) -> list[str]:
    """去掉这一款的这几家店（只去还没开始建箱的；allow_queued 时排队中的也去；领过的货号不再用，mooya 那边照样占着）。
    返回去掉了的店铺 key。"""
    current = {r["store_key"]: r for r in rows(ctx, asset_ids=[asset_id])}
    allowed = UNSTARTED if allow_queued else frozenset({STATE_CHOSEN})
    removable = [
        k for k in keys if k in current and (current[k]["state"] not in _LOCKED or current[k]["state"] in allowed)
    ]
    if not removable:
        return []
    now = now_iso()
    with ctx.db.tx() as conn:
        for key in removable:
            conn.execute(
                "UPDATE store_listings SET state = ?, updated_at = ? WHERE asset_id = ? AND store_key = ?",
                (STATE_CANCELLED, now, asset_id, key),
            )
            trace.record(
                conn,
                asset_id,
                "store",
                frm=current[key]["state"],
                to=STATE_CANCELLED,
                store=key,
                code=current[key].get("code"),
                actor=by,
                source=source,
                reason=reason,
            )
        log_event(conn, asset_id, "store_removed", {"stores": removable, "by": by, "source": source})
    return removable


# 这些状态的店铺不能再去掉（已经在上或上过了）
_LOCKED = frozenset(
    {"queued", "creating", "create_unknown", "in_box", "publishing", "publish_unknown", "listed"}
)
# 还没开始建箱、去掉也不用动妙手的状态（全自动时人在候选表取消店铺勾就去掉）
UNSTARTED = frozenset({STATE_CHOSEN, "queued"})


def set_stores(ctx: Ctx, asset_id: str, refs: list[str], *, by: str) -> dict[str, Any]:
    """对话里改一款的店铺（`podctl temu store IMG… --set 3店,4店`）：加上没有的，去掉不要的。
    已经在上架或上过的店去不掉；领过的货号去掉后也不再用（mooya 那边照样占着）。"""
    row = ctx.db.q1(
        "SELECT human_decision FROM assets WHERE asset_id = ?", (asset_id,)
    )
    if not row:
        raise StageError(f"没有素材 {asset_id}")
    if row["human_decision"] != "approved":
        raise StageError(f"{asset_id} 不是选中的款：先在候选表选中")
    wanted = [resolve(ctx, r).key for r in refs if r.strip()]
    current = {r["store_key"]: r for r in rows(ctx, asset_ids=[asset_id])}
    blocked = [k for k in current if k not in wanted and current[k]["state"] in _LOCKED]
    if blocked:
        raise StageError(
            "这几家店已经在上架或上过了，去不掉："
            + "、".join(get(ctx, k).column for k in blocked)
        )
    added = choose(ctx, asset_id, wanted, by=by, source="chat")
    removed = remove(ctx, asset_id, [k for k in current if k not in wanted], by=by, source="chat")
    codes = allocate_codes(ctx, asset_ids=[asset_id])
    return {
        "asset_id": asset_id,
        "added": added,
        "removed": removed,
        "stores": [r["store_key"] for r in rows(ctx, asset_ids=[asset_id])],
        "codes": codes,
    }


# ---------------------------------------------------------------- 货号


def code_ref(ctx: Ctx, asset_id: str, store_key: str) -> str:
    from .scenes import instance_id  # 套图模块依赖本模块，放到用时再导入

    return f"pod-blanket:{instance_id(ctx)}:{asset_id}:{store_key}"


def allocate_codes(ctx: Ctx, *, asset_ids: list[str] | None = None) -> dict[str, Any]:
    """没领到货号的（款, 店）向 mooya 领号。返回 {reserved: [{asset_id, store_key, code}], errors, stopped?}。
    mooya 连不上 / 限流 / 5xx / 令牌不对时这一轮停下、不算次数，下次巡检或交套图前再试；
    这一单本身的错（其余 4xx、没给货号）算一次，满 MAX_CODE_ATTEMPTS 不再自动试。"""
    if not enabled(ctx):
        return {"reserved": [], "errors": []}
    pending = [
        r
        for r in rows(ctx, asset_ids=asset_ids)
        if not r["code"] and r["code_attempts"] < MAX_CODE_ATTEMPTS
    ]
    out: dict[str, Any] = {"reserved": [], "errors": []}
    if not pending:
        return out
    store_by_key = {s.key: s for s in all_stores(ctx)}
    try:
        api = mooya.Client(ctx, client=mooya.submit_client(ctx))
    except mooya.MooyaError as exc:
        out["stopped"] = str(exc)[:300]
        for row in pending:
            _code_failed(ctx, row, str(exc), count=False)
        return out
    with api:
        for row in pending:
            store = store_by_key.get(row["store_key"])
            if store is None:
                continue
            ref = code_ref(ctx, row["asset_id"], row["store_key"])
            try:
                got = api.reserve_code(store.prefix, ref)
            except mooya.MooyaError as exc:
                passing = exc.service or exc.transient
                _code_failed(ctx, row, str(exc), count=not passing)
                out["errors"].append(
                    {"asset_id": row["asset_id"], "store_key": row["store_key"], "error": str(exc)[:300]}
                )
                if passing:
                    out["stopped"] = str(exc)[:300]
                    break
                continue
            code = str(got.get("code") or got.get("name") or "")
            if not code:
                _code_failed(ctx, row, f"mooya 没给货号：{got}")
                continue
            now = now_iso()
            with ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE store_listings SET code = ?, code_ref = ?, code_at = ?, code_error = NULL,"
                    " updated_at = ? WHERE asset_id = ? AND store_key = ?",
                    (code, ref, now, now, row["asset_id"], row["store_key"]),
                )
                log_event(
                    conn,
                    row["asset_id"],
                    "code_reserved",
                    {"store": row["store_key"], "code": code, "created": got.get("created")},
                )
                trace.record(
                    conn,
                    row["asset_id"],
                    "code",
                    to=code,
                    store=row["store_key"],
                    code=code,
                    source="mooya",
                    payload={"ref": ref, "created": got.get("created")},
                )
            out["reserved"].append(
                {"asset_id": row["asset_id"], "store_key": row["store_key"], "code": code}
            )
    return out


def _code_failed(ctx: Ctx, row: dict[str, Any], reason: str, *, count: bool = True) -> None:
    """记下领号失败的原因；count 为 False（mooya 抖动、限流、令牌配置）时不算次数。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE store_listings SET code_attempts = code_attempts + ?, code_error = ?, updated_at = ?"
            " WHERE asset_id = ? AND store_key = ?",
            (1 if count else 0, reason[:300], now_iso(), row["asset_id"], row["store_key"]),
        )
        if count:
            trace.record(
                conn,
                row["asset_id"],
                "code",
                store=row["store_key"],
                reason=reason,
                source="mooya",
                payload={"attempts": int(row.get("code_attempts") or 0) + 1},
            )


def retry_codes(ctx: Ctx) -> int:
    with ctx.db.tx() as conn:
        return conn.execute(
            "UPDATE store_listings SET code_attempts = 0, code_error = NULL WHERE code IS NULL"
        ).rowcount


def primary_code(ctx: Ctx, asset_id: str) -> str | None:
    """这一款第一家店（配置顺序）的货号：交给 mooya 出套图时当编号。没定店铺、或还没领到号为 None。"""
    for row in rows(ctx, asset_ids=[asset_id]):
        if row["code"]:
            return row["code"]
    return None


def waiting_codes(ctx: Ctx, asset_ids: list[str]) -> set[str]:
    """定了店铺、但第一家店还没领到货号的款：交套图要等货号（mooya 的交付包名就是货号）。
    领号失败满上限的也等（进「POD 待办」，`temu codes --retry` 清零后接着领），不再用素材号顶上。"""
    if not enabled(ctx) or not asset_ids:
        return set()
    out: set[str] = set()
    for asset_id, items in by_asset(ctx, asset_ids).items():
        if not items[0]["code"]:
            out.add(asset_id)
    return out


def code_failures(ctx: Ctx) -> list[dict[str, Any]]:
    """领号失败满上限、不再自动领的（款, 店）：要人处理（待办里列出来）。"""
    return [
        {"asset_id": r["asset_id"], "store_key": r["store_key"], "error": r["code_error"]}
        for r in rows(ctx)
        if not r["code"] and r["code_attempts"] >= MAX_CODE_ATTEMPTS
    ]


def codes_text(ctx: Ctx, asset_id: str) -> str:
    """「FLow0006」或「FLow0006 / Deer0006」（还没领到的写店铺 + 领号中）。"""
    parts = []
    for row in rows(ctx, asset_ids=[asset_id]):
        parts.append(row["code"] or f"{get(ctx, row['store_key']).label}领号中")
    return " / ".join(parts)


def purge_blockers(ctx: Ctx, asset_ids: list[str]) -> list[str]:
    """清理旧数据时不能删的款：领了货号、在上架或已上架。"""
    held = []
    for row in rows(ctx, asset_ids=asset_ids):
        if row["code"] or row["state"] in _LOCKED:
            held.append(row["asset_id"])
    return sorted(set(held))


def status(ctx: Ctx) -> dict[str, Any]:
    try:
        configured = all_stores(ctx)
        error = None
    except ConfigError as exc:
        configured, error = [], str(exc)
    counts: dict[str, dict[str, int]] = {}
    for row in rows(ctx):
        item = counts.setdefault(row["store_key"], {"chosen": 0, "coded": 0, "listed": 0})
        item["chosen"] += 1
        item["coded"] += bool(row["code"])
        item["listed"] += row["state"] == "listed"
    return {
        "enabled": bool(ctx.setting("temu.enabled", False)),
        "error": error,
        "stores": [
            {
                "key": s.key,
                "column": s.column,
                "prefix": s.prefix,
                "shop_id": s.shop_id,
                "template_detail_id": s.template_detail_id,
                "brand": s.brand,
                "brand_id": s.brand_id,
                **counts.get(s.key, {"chosen": 0, "coded": 0, "listed": 0}),
            }
            for s in configured
        ],
        "codes_waiting": sum(
            1
            for r in rows(ctx)
            if not r["code"] and r["code_attempts"] < MAX_CODE_ATTEMPTS
        ),
        "codes_failed": [
            {"asset_id": r["asset_id"], "store_key": r["store_key"], "error": r["code_error"]}
            for r in rows(ctx)
            if not r["code"] and r["code_attempts"] >= MAX_CODE_ATTEMPTS
        ],
    }
