"""「选中的款」的下载确认：一款一个勾，勾上 = 这一款现在这套套图下载好了；表按隐藏的「排序」列排。

- 列（feishu_table.DOWNLOAD_FIELDS）：「下载确认」（勾选框，人勾）、「排序」（数字，程序写，表格视图里藏起来）。
  开关 table.download_confirm（缺省开），还要开着 table.enabled 与 scenes.enabled（没有套图就没有可下载的）。
- 确认（表 selected_downloads）：记下这一款当时那套套图和它的指纹（每张图的版本 + 交付清单，同交付包的口径），
  重出、在 mooya 里重生过图都算新的一版，旧的确认作废（勾由程序取消，这一行回到上面）。
  只有最近一套出完（done / partial）的款能确认；套图没出完（排队、在出、等人工、没出成）勾了不算，程序把勾取消。
- 排序（三组；值只随这一行自己的状态变，进新行不用全表重写）：待下载（最近一套出完、没确认这一版，出完时间新的在前）
  → 还不能下（排队 / 在出 / 等人工 / 没出成 / 还没出套图，套图交出去或进表的时间新的在前）
  → 已下载（确认早的在前，刚确认的在最底下）。
- 和交付包打通（packages.py）：一个批次交付包里的款都确认了，自动标这个批次交付包已下载（source selected，再对这个任务
  扫一次归档）；交付包标了已下载（卡片、候选表交付包行、对话），包里的款都算确认（source package）；撤销交付包只撤掉
  由它带出来的确认，撤销单款不动交付包。
- 谁来写飞书：记录变更事件（网关转来的 table control，只转勾上的）当场确认、写这几行；巡检 table poll 回读整列兜底
  （漏掉的勾、人取消的勾）；表格同步（table sync）把各行的排序和勾写成该有的样子。只写变了的（selected_rows 记着上次
  写进去的值）；勾只在要改时才写，免得盖掉人刚打的勾。这几个是不同的进程：算该写成什么、写飞书、记下写了什么
  三步要在写入租约里一起做，交错着写会让表里留着旧的排序、记录却以为是新的，这一行就再也不挪了。
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from typing import Any

import httpx

from . import feishu, feishu_table, labels, lease, packages, scenes, shelf, store, table_registry
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, loads, now_iso, parse_iso

CONFIRM_FIELD = feishu_table.DOWNLOAD_CONFIRM_FIELD
ORDER_FIELD = feishu_table.DOWNLOAD_ORDER_FIELD
# 确认从哪来：table = 在「选中的款」里勾的，chat = POD 对话，package = 交付包标了已下载带出来的，migrate = 迁移旧标记，
# listing = 所有店都上了 Temu（temu.py）
SOURCES = ("table", "chat", "package", "migrate", "listing")
# 排序：三组，组内按分钟（从 2026-01-01 算起）；最大不到 3.1 亿，压在 Postgres INTEGER 以内
GROUP_PENDING = 100_000_000
GROUP_WAITING = 200_000_000
GROUP_DONE = 300_000_000
_SPAN = 10_000_000
_EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
# 巡检回读时拿不到是谁勾的，记在确认人上的名字
POLL_BY = "feishu:选中的款"
# 写排序 / 勾失败的次数和原因：{"attempts": n, "error": …}；满 feishu_table.MAX_ATTEMPTS 不再自动试
META_KEY = "selected_downloads.sync"
# 写「排序」和勾的租约：一次写几行到几百行，逐行补写时每行续一次；别的进程等着，等不到就留给下一次表格同步
WRITE_LEASE = "selected_downloads.writer"
WRITE_SECONDS = 120
WAIT_SECONDS = 45
_PAGE = 500
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)


def enabled(ctx: Ctx) -> bool:
    return (
        feishu_table.enabled(ctx)
        and scenes.enabled(ctx)
        and bool(ctx.setting("table.download_confirm", True))
    )


def ready(info: dict[str, Any] | None) -> bool:
    """这本表的「选中的款」已经有「下载确认」「排序」两列（新建时带着，或之后补上了）。"""
    names = {f["field_name"] for f in feishu_table.DOWNLOAD_FIELDS}
    return names <= set((info or {}).get("extra_fields") or [])


def _meta(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(META_KEY), None) or {}


def _failed(ctx: Ctx, exc: BaseException, errors: list[dict[str, str]]) -> str:
    reason = f"{type(exc).__name__}: {exc}"[:300]
    meta = _meta(ctx)
    meta["attempts"] = int(meta.get("attempts") or 0) + 1
    meta["error"] = reason
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, META_KEY, dumps(meta))
    errors.append({"table": feishu_table.TABLE_NAME, "error": reason})
    return reason


def _ok(ctx: Ctx) -> None:
    if _meta(ctx):
        with ctx.db.tx() as conn:
            ctx.db.del_meta(conn, META_KEY)


def reset_attempts(ctx: Ctx) -> None:
    """table sync --retry：失败次数清零，再试。"""
    _ok(ctx)


def _active(ctx: Ctx, info: dict[str, Any] | None) -> bool:
    return (
        enabled(ctx)
        and ready(info)
        and int(_meta(ctx).get("attempts") or 0) < feishu_table.MAX_ATTEMPTS
    )


# ---------------------------------------------------------------- 每一款现在的样子


def _minutes(ts: str | None) -> int:
    when = parse_iso(ts)
    if when is None:
        return 0
    return max(0, min(_SPAN - 1, int((when - _EPOCH).total_seconds() // 60)))


def order_value(group: str, at: str | None) -> int:
    """一行的「排序」：pending 待下载 / waiting 还不能下（这两组新的在前）/ done 已下载（确认早的在前，刚确认的在最底）。"""
    minutes = _minutes(at)
    if group == "done":
        return GROUP_DONE + minutes
    base = GROUP_PENDING if group == "pending" else GROUP_WAITING
    return base + (_SPAN - 1 - minutes)


def _marks(ctx: Ctx, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(asset_ids), labels.BATCH):
        chunk = asset_ids[i : i + labels.BATCH]
        for row in ctx.db.q(
            f"SELECT * FROM selected_downloads WHERE asset_id IN ({','.join('?' for _ in chunk)})",
            tuple(chunk),
        ):
            out[row["asset_id"]] = dict(row)
    return out


def items(ctx: Ctx, asset_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """选中、已入库的款（出套图的对象；asset_ids 只看这几款）：{asset_id: {asset_id, seed_id, batch_id,
    set（最近一套，没有为 None）, ready（最近一套出完了）, fingerprint, mark（selected_downloads 那一行）,
    confirmed（确认的是现在这一版）}}。"""
    if asset_ids is not None and not asset_ids:
        return {}
    approved = scenes.approved_assets(ctx, asset_ids=asset_ids)
    ids = [a["asset_id"] for a in approved]
    latest = scenes.latest_sets(ctx, ids)
    done = {a: s for a, s in latest.items() if s["status"] in scenes.OK_FINAL}
    prints = packages.set_fingerprints(
        ctx, [(asset_id, s["set_id"]) for asset_id, s in done.items()]
    )
    marks = _marks(ctx, ids)
    out: dict[str, dict[str, Any]] = {}
    for asset in approved:
        asset_id = asset["asset_id"]
        current = latest.get(asset_id)
        mark = marks.get(asset_id)
        fingerprint = prints.get(asset_id)
        out[asset_id] = {
            "asset_id": asset_id,
            "seed_id": asset["seed_id"],
            "batch_id": asset["batch_id"],
            "set": current,
            "ready": asset_id in done,
            "fingerprint": fingerprint,
            "mark": mark,
            "confirmed": bool(
                mark
                and fingerprint
                and mark["set_id"] == current["set_id"]
                and mark["fingerprint"] == fingerprint
            ),
        }
    return out


def _not_ready_text(current: dict[str, Any] | None) -> str:
    if current is None:
        return "还没出套图"
    if current["status"] in scenes.PARKED:
        return "套图在 mooya 等人工"
    if current["status"] in ("failed", "cancelled"):
        return "套图没出成"
    return "套图还在出"


def _rows_by_record(ctx: Ctx, record_ids: list[str] | None = None) -> dict[str, str]:
    """「选中的款」里的行：{record_id: asset_id}（record_ids 只看这几行）。"""
    rows = {
        r["record_id"]: r["asset_id"]
        for r in ctx.db.q(
            "SELECT asset_id, record_id FROM table_rows WHERE record_id IS NOT NULL"
        )
        if table_registry.mine(r["asset_id"])
    }
    if record_ids is None:
        return rows
    return {r: rows[r] for r in record_ids if r in rows}


def _layout(
    ctx: Ctx, found: dict[str, dict[str, Any]], asset_ids: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """「选中的款」每一行该写成的样子：{asset_id: {asset_id, record_id, group, order, checked}}。"""
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT asset_id, record_id, synced_at, updated_at FROM table_rows"
            " WHERE record_id IS NOT NULL"
        )
        if table_registry.mine(r["asset_id"])
    ]
    wanted = set(asset_ids) if asset_ids is not None else None
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if wanted is not None and row["asset_id"] not in wanted:
            continue
        item = found.get(row["asset_id"])
        current = (item or {}).get("set")
        if item and item["confirmed"]:
            group, at = "done", item["mark"]["marked_at"]
        elif item and item["ready"]:
            group, at = "pending", current["finished_at"] or current["updated_at"]
        else:
            group = "waiting"
            at = (
                (current or {}).get("created_at")
                or row["synced_at"]
                or row["updated_at"]
            )
        out[row["asset_id"]] = {
            "asset_id": row["asset_id"],
            "record_id": row["record_id"],
            "group": group,
            "order": order_value(group, at),
            "checked": group == "done",
        }
    return out


def _cache(ctx: Ctx) -> dict[str, dict[str, Any]]:
    return {r["asset_id"]: dict(r) for r in ctx.db.q("SELECT * FROM selected_rows")}


def _changes(
    layout: dict[str, dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    *,
    untick: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """要写进飞书的行：排序变了，或者勾要改（确认了还没打上、确认作废了要取消）。没写过勾、也不该勾的行不碰勾
    （可能是人刚勾的，等事件 / 巡检处理）；untick 里的（勾了但不能确认）一定取消。"""
    out = []
    for asset_id, want in layout.items():
        old = cache.get(asset_id)
        same = bool(old and old["record_id"] == want["record_id"])
        last = old["checked"] if same else None
        write_check = asset_id in untick or (
            want["checked"] if last is None else bool(last) != want["checked"]
        )
        if write_check or not same or old["order_value"] != want["order"]:
            out.append({**want, "write_check": write_check})
    return out


def _cells(change: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {ORDER_FIELD: change["order"]}
    if change["write_check"]:
        fields[CONFIRM_FIELD] = change["checked"]
    return fields


def _remember(
    ctx: Ctx, written: list[dict[str, Any]], cache: dict[str, dict[str, Any]]
) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        for change in written:
            old = cache.get(change["asset_id"]) or {}
            if change["write_check"]:
                checked = int(change["checked"])
            elif old.get("record_id") == change["record_id"]:
                checked = old.get("checked")
            else:
                checked = None
            conn.execute(
                "INSERT INTO selected_rows(asset_id, record_id, order_value, checked, updated_at)"
                " VALUES(?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET"
                " record_id = excluded.record_id, order_value = excluded.order_value,"
                " checked = excluded.checked, updated_at = excluded.updated_at",
                (
                    change["asset_id"],
                    change["record_id"],
                    change["order"],
                    checked,
                    now,
                ),
            )


def _write(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    changes: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
) -> int:
    """把这些行的「排序」（和要改的勾）写进飞书，写成的记进 selected_rows。整批写不成（多半是有的行被人删了）就逐行写，
    写不成的跳过；一行都没写成时抛出最后的错误。返回写成几行。调用方持有写入租约，这里每次调接口前续租。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    params = {"user_id_type": "open_id"}
    written: list[dict[str, Any]] = []
    last: feishu.FeishuError | None = None
    for i in range(0, len(changes), _PAGE):
        chunk = changes[i : i + _PAGE]
        lease.take(ctx, WRITE_LEASE, WRITE_SECONDS)
        try:
            api.call(
                "POST",
                f"{path}/batch_update",
                params=params,
                json={
                    "records": [
                        {"record_id": c["record_id"], "fields": _cells(c)}
                        for c in chunk
                    ]
                },
            )
            written += chunk
        except feishu.FeishuError:
            for change in chunk:
                lease.take(ctx, WRITE_LEASE, WRITE_SECONDS)
                try:
                    api.call(
                        "PUT",
                        f"{path}/{change['record_id']}",
                        params=params,
                        json={"fields": _cells(change)},
                    )
                    written.append(change)
                except feishu.FeishuError as exc:
                    last = exc
    _remember(ctx, written, cache)
    if changes and not written and last is not None:
        raise last
    return len(written)


def _write_rows(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    asset_ids: list[str],
    *,
    untick: set[str] | frozenset[str] = frozenset(),
) -> int:
    """只写这几款的行（事件、巡检、对话里刚确认 / 撤销的），当场挪位置。另一个进程一直占着写入租约时不写，
    交给下一次表格同步（取消不了的勾由巡检再取消）。"""
    if not asset_ids:
        return 0
    if not _take_writer(ctx):
        feishu_table.kick(ctx)
        return 0
    try:
        layout = _layout(ctx, items(ctx, asset_ids), asset_ids)
        cache = _cache(ctx)
        changes = _changes(layout, cache, untick=untick)
        return _write(ctx, api, info, changes, cache) if changes else 0
    finally:
        lease.drop(ctx, WRITE_LEASE)


def _write_now(
    ctx: Ctx,
    api: feishu.Client,
    info: dict[str, Any],
    asset_ids: list[str],
    *,
    untick: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """确认已经记下了，写表失败不往外抛：{written} 或 {write_error}，写不进去的交给下一次表格同步。"""
    try:
        return {"written": _write_rows(ctx, api, info, asset_ids, untick=untick)}
    except _FAILURES as exc:
        feishu_table.kick(ctx)
        return {"write_error": f"{type(exc).__name__}: {exc}"[:300]}


def _take_writer(ctx: Ctx) -> bool:
    """拿写入租约；别的进程正在写就等，等满 WAIT_SECONDS 还没拿到返回 False。"""
    deadline = time.monotonic() + WAIT_SECONDS
    while not lease.take(ctx, WRITE_LEASE, WRITE_SECONDS):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


# ---------------------------------------------------------------- 确认与撤销


def confirm(
    ctx: Ctx, asset_ids: list[str], *, by: str, source: str, roll: bool = True
) -> dict[str, Any]:
    """确认这几款的套图下载好了（记下最近一套和它的指纹）。返回 {confirmed, already, rejected[{asset_id, reason}],
    packages}。roll：再看这些款所在批次的交付包是不是都确认了，是就标交付包已下载（交付包带出来的确认不往回走）。"""
    ids = list(dict.fromkeys(asset_ids))
    found = items(ctx, ids)
    confirmed: list[str] = []
    already: list[str] = []
    rejected: list[dict[str, str]] = []
    now = now_iso()
    with ctx.db.tx() as conn:
        for asset_id in ids:
            item = found.get(asset_id)
            if item is None:
                rejected.append({"asset_id": asset_id, "reason": "不是选中的款"})
                continue
            if not item["ready"]:
                rejected.append(
                    {"asset_id": asset_id, "reason": _not_ready_text(item["set"])}
                )
                continue
            if item["confirmed"]:
                already.append(asset_id)
                continue
            conn.execute(
                "INSERT INTO selected_downloads(asset_id, seed_id, batch_id, set_id, fingerprint,"
                " marked_by, marked_at, source) VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(asset_id) DO UPDATE SET seed_id = excluded.seed_id,"
                " batch_id = excluded.batch_id, set_id = excluded.set_id,"
                " fingerprint = excluded.fingerprint, marked_by = excluded.marked_by,"
                " marked_at = excluded.marked_at, source = excluded.source",
                (
                    asset_id,
                    item["seed_id"],
                    item["batch_id"],
                    item["set"]["set_id"],
                    item["fingerprint"],
                    by,
                    now,
                    source,
                ),
            )
            log_event(
                conn,
                asset_id,
                "selected_download_confirmed",
                {"set_id": item["set"]["set_id"], "by": by, "source": source},
            )
            confirmed.append(asset_id)
    out: dict[str, Any] = {
        "confirmed": confirmed,
        "already": already,
        "rejected": rejected,
        "packages": [],
    }
    if roll:
        keys = {
            (found[a]["seed_id"], found[a]["batch_id"]) for a in confirmed + already
        }
        out["packages"] = roll_up(ctx, keys, by=by)
    return out


def unconfirm(
    ctx: Ctx, asset_ids: list[str], *, by: str, only_source: str | None = None
) -> dict[str, Any]:
    """撤销这几款的下载确认（only_source 只撤这种来源的）；不动交付包的已下载。返回 {undone, missing}。"""
    ids = list(dict.fromkeys(asset_ids))
    undone: list[str] = []
    with ctx.db.tx() as conn:
        for asset_id in ids:
            sql, params = (
                "DELETE FROM selected_downloads WHERE asset_id = ?",
                (asset_id,),
            )
            if only_source:
                sql, params = sql + " AND source = ?", (asset_id, only_source)
            if conn.execute(sql, params).rowcount:
                log_event(conn, asset_id, "selected_download_undone", {"by": by})
                undone.append(asset_id)
    return {"undone": undone, "missing": [a for a in ids if a not in undone]}


def package_assets(ctx: Ctx, seed_id: str, batch_id: str) -> dict[str, str]:
    """这个批次交付包里的款：{asset_id: 包里那一套的 set_id}；包还没打好为空。"""
    pkg = packages.get(ctx, seed_id, batch_id)
    if not packages.ready(pkg):
        return {}
    set_ids = loads(pkg["set_ids_json"], []) or []
    if not set_ids:
        return {}
    marks = ",".join("?" for _ in set_ids)
    return {
        r["asset_id"]: r["set_id"]
        for r in ctx.db.q(
            f"SELECT set_id, asset_id FROM scene_sets WHERE set_id IN ({marks})",
            tuple(set_ids),
        )
    }


def _package_view(ctx: Ctx, seed_id: str, batch_id: str) -> dict[str, Any] | None:
    return next(
        (
            v
            for v in packages.by_seed(ctx, seed_id).get(seed_id, [])
            if v["batch_id"] == batch_id
        ),
        None,
    )


def _covered(
    assets: dict[str, str],
    found: dict[str, dict[str, Any]],
    extra: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """包里每一款都确认了包里那一套（extra 里的款当作马上会确认）。"""
    for asset_id, set_id in assets.items():
        item = found.get(asset_id)
        if not item or (item["set"] or {}).get("set_id") != set_id:
            return False
        if not (item["confirmed"] or (asset_id in extra and item["ready"])):
            return False
    return bool(assets)


def roll_up(ctx: Ctx, keys: set[tuple[str, str]], *, by: str) -> list[dict[str, Any]]:
    """这些批次的交付包里的款在「选中的款」都确认了（确认的是包里那一套、内容没变）就标交付包已下载，再对这个任务
    扫一次归档。返回标上的 [{seed_id, batch_id, label, archived? / shelf_block?}] 和没标上的 refused。"""
    if not packages.enabled(ctx):
        return []
    out: list[dict[str, Any]] = []
    for seed_id, batch_id in sorted(keys):
        view = _package_view(ctx, seed_id, batch_id)
        if not view or not view["ready"] or view["stale"] or view["downloaded"]:
            continue
        assets = package_assets(ctx, seed_id, batch_id)
        if not _covered(assets, items(ctx, list(assets))):
            continue
        try:
            marked = packages.mark_downloaded(
                ctx, seed_id, batch_id, by=by, source="selected"
            )
        except StageError as exc:
            out.append({"seed_id": seed_id, "batch_id": batch_id, "refused": str(exc)})
            continue
        out.append(
            {
                "seed_id": seed_id,
                "batch_id": batch_id,
                "label": marked["label"],
                **shelf.after_download(ctx, seed_id),
            }
        )
    return out


def confirm_package(ctx: Ctx, seed_id: str, batch_id: str, *, by: str) -> list[str]:
    """交付包标了已下载：包里的款都算确认（确认的是包里那一套；之后又重出了的不算）。返回这次新确认的款。"""
    if not enabled(ctx):
        return []
    assets = package_assets(ctx, seed_id, batch_id)
    latest = scenes.latest_sets(ctx, list(assets))
    ids = [a for a, s in assets.items() if (latest.get(a) or {}).get("set_id") == s]
    if not ids:
        return []
    return confirm(ctx, ids, by=by, source="package", roll=False)["confirmed"]


def unconfirm_package(ctx: Ctx, seed_id: str, batch_id: str, *, by: str) -> list[str]:
    """撤销交付包的已下载：只撤掉由它带出来的确认（人在「选中的款」里勾的、对话里标的留着）。返回撤掉的款。"""
    if not enabled(ctx):
        return []
    assets = package_assets(ctx, seed_id, batch_id)
    return unconfirm(ctx, list(assets), by=by, only_source="package")["undone"]


# ---------------------------------------------------------------- 飞书：补列、写表、事件、巡检


def ensure_columns(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """给已经建好的「选中的款」补上「下载确认」「排序」两列（新建的表建表时就有）；调用方持有选款表的同步租约。"""
    if (
        not enabled(ctx)
        or ready(info)
        or int(_meta(ctx).get("attempts") or 0) >= feishu_table.MAX_ATTEMPTS
    ):
        return {}
    try:
        added = feishu_table.ensure_fields(ctx, api, info, feishu_table.DOWNLOAD_FIELDS)
    except _FAILURES as exc:
        return {"error": _failed(ctx, exc, errors)}
    return {"fields_added": added} if added else {}


def has_work(ctx: Ctx, info: dict[str, Any] | None = None) -> bool:
    """「选中的款」还要补列，或者有行的排序 / 勾和该有的不一样（失败满上限的不再试）。"""
    info = info if info is not None else feishu_table.base(ctx)
    if (
        not enabled(ctx)
        or not info
        or int(_meta(ctx).get("attempts") or 0) >= feishu_table.MAX_ATTEMPTS
    ):
        return False
    if not ready(info):
        return True
    return bool(_changes(_layout(ctx, items(ctx)), _cache(ctx)))


def sync_table(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    """把「选中的款」各行的「排序」和「下载确认」写成该有的样子（调用方持有选款表的同步租约）。返回 {written} 或 {}；
    写入租约一直被别的进程占着时返回 {skipped}，不算失败，下一次同步再写。"""
    if not _active(ctx, info):
        return {}
    if not _take_writer(ctx):
        return {"skipped": "另一个进程正在写「选中的款」的排序"}
    try:
        layout = _layout(ctx, items(ctx))
        cache = _cache(ctx)
        changes = _changes(layout, cache)
        if not changes:
            return {}
        try:
            written = _write(ctx, api, info, changes, cache)
        except _FAILURES as exc:
            return {"error": _failed(ctx, exc, errors)}
    finally:
        lease.drop(ctx, WRITE_LEASE)
    _ok(ctx)
    return {"written": written}


def _ticked(value: Any) -> bool:
    """勾选框的值算不算勾着：只认 true（列表接口返回文本形式，没勾过是空、取消过是 false）。"""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _read_checks(
    api: feishu.Client, info: dict[str, Any], record_ids: list[str]
) -> dict[str, bool]:
    """回读这几行的「下载确认」：{record_id: 勾着没有}；表里已经没有的行不在结果里。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    out: dict[str, bool] = {}
    for i in range(0, len(record_ids), _PAGE):
        data = api.call(
            "POST",
            f"{path}/batch_get",
            json={"record_ids": record_ids[i : i + _PAGE], "user_id_type": "open_id"},
        )
        for item in data.get("records") or []:
            out[item["record_id"]] = _ticked(
                (item.get("fields") or {}).get(CONFIRM_FIELD)
            )
    return out


def read_column(
    api: feishu.Client, info: dict[str, Any], field_name: str
) -> dict[str, Any]:
    """分页读「选中的款」一整列：{record_id: 值}（没填的是 None）。带 field_names 的列表接口返回文本形式：
    数字是字符串、多选是选项文字，勾选框是 true / false。"""
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    out: dict[str, Any] = {}
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "page_size": _PAGE,
            "field_names": dumps([field_name]),
        }
        if token:
            params["page_token"] = token
        data = api.call("GET", path, params=params)
        for item in data.get("items") or []:
            out[item["record_id"]] = (item.get("fields") or {}).get(field_name)
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            return out


def table_event(
    ctx: Ctx,
    *,
    record_ids: list[str],
    by: str,
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """「选中的款」的记录变更事件（网关转来勾上了的记录）：回读还勾着的就确认，不能确认的取消勾；当场写这几行。
    我们自己写勾也会发事件，回读后都是已确认的，什么都不做。info 是事件所在的那一份「选中的款」（缺省旧表格的）。"""
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    if not enabled(ctx):
        return {"ignored": "下载确认关着（table.download_confirm）"}
    info = info or feishu_table.base(ctx) or {}
    if not ready(info):
        return {"ignored": "「选中的款」还没有「下载确认」列"}
    with books.scoped(ctx, info):
        return _table_event(ctx, info, record_ids, by)


def _table_event(
    ctx: Ctx, info: dict[str, Any], record_ids: list[str], by: str
) -> dict[str, Any]:
    rows = _rows_by_record(ctx, record_ids)
    if not rows:
        return {"ignored": "改的不是选中的款里的行"}
    with feishu.Client(ctx) as api:
        seen = _read_checks(api, info, list(rows))
        ticked = [rows[r] for r in rows if seen.get(r)]
        if not ticked:
            return {"ignored": "下载确认没勾"}
        result = confirm(ctx, ticked, by=by, source="table")
        result.update(
            _write_now(
                ctx,
                api,
                info,
                ticked,
                untick={r["asset_id"] for r in result["rejected"]},
            )
        )
    return result


def poll(ctx: Ctx) -> dict[str, Any] | None:
    """巡检兜底：回读「选中的款」整列「下载确认」。勾着还没确认的确认（事件没送到、网关重启时漏掉的）；
    程序写过勾、现在没勾的算人取消了，撤销。勾着、确认却已作废的（重出、在 mooya 里重生过图，表格同步还没来得及
    取消勾）是程序上次写的勾，不算人确认了新的一版，取消掉（cleared）。没开、还没有这一列时返回 None。
    旧表格和每周表格各天的「选中的款」各读一遍。"""
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    out: dict[str, Any] | None = None
    for info in books.scopes(ctx):
        if not _active(ctx, info):
            continue
        with books.scoped(ctx, info):
            part = _poll_scope(ctx, info)
        if out is None:
            out = part
            continue
        out["read"] += part["read"]
        for key in ("confirmed", "undone", "cleared", "rejected", "packages"):
            out[key] += part[key]
        if part.get("write_error"):
            out["write_error"] = part["write_error"]
    return out


def _poll_scope(ctx: Ctx, info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "read": 0,
        "confirmed": [],
        "undone": [],
        "cleared": [],
        "rejected": [],
        "packages": [],
    }
    with feishu.Client(ctx) as api:
        cells = read_column(api, info, CONFIRM_FIELD)
        out["read"] = len(cells)
        rows = _rows_by_record(ctx)
        found = items(ctx, list(rows.values()))
        cache = _cache(ctx)
        tick: list[str] = []
        untick: list[str] = []
        for record_id, asset_id in rows.items():
            if record_id not in cells:
                continue
            item = found.get(asset_id)
            confirmed = bool(item and item["confirmed"])
            written = cache.get(asset_id) or {}
            ours = written.get("record_id") == record_id and bool(
                written.get("checked")
            )
            checked = _ticked(cells[record_id])
            if checked and not confirmed:
                (out["cleared"] if ours else tick).append(asset_id)
            elif not checked and confirmed and ours:
                untick.append(asset_id)
        if tick:
            result = confirm(ctx, tick, by=POLL_BY, source="table")
            out.update(
                confirmed=result["confirmed"],
                rejected=result["rejected"],
                packages=result["packages"],
            )
        if untick:
            out["undone"] = unconfirm(ctx, untick, by=POLL_BY)["undone"]
        if tick or untick or out["cleared"]:
            out.update(
                _write_now(
                    ctx,
                    api,
                    info,
                    tick + untick + out["cleared"],
                    untick={r["asset_id"] for r in out["rejected"]},
                )
            )
    return out


def mark_assets(
    ctx: Ctx, asset_ids: list[str], *, by: str, undo: bool = False
) -> dict[str, Any]:
    """对话里标 / 撤这几款的下载确认（podctl table downloaded），当场写进「选中的款」。"""
    if not enabled(ctx):
        raise StageError(
            "下载确认没打开：table.download_confirm、table.enabled、scenes.enabled 都要开着"
        )
    from . import books  # 每周选款表依赖本模块，放到用时再导入

    if undo:
        result = unconfirm(ctx, asset_ids, by=by)
    else:
        result = confirm(ctx, asset_ids, by=by, source="chat")
    # 每一款写进它那一份「选中的款」（旧表格，或每周表格的那一天）
    groups: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for asset_id in asset_ids:
        info = books.scope_of_asset(ctx, asset_id) or {}
        if ready(info):
            key = str(info.get("day") or "")
            groups.setdefault(key, (info, []))[1].append(asset_id)
    if groups:
        with feishu.Client(ctx) as api:
            for info, ids in groups.values():
                with books.scoped(ctx, info):
                    part = _write_now(ctx, api, info, ids)
                result["written"] = int(result.get("written") or 0) + int(
                    part.get("written") or 0
                )
                if part.get("write_error"):
                    result["write_error"] = part["write_error"]
    result["labels"] = {
        a: v["label"] for a, v in labels.assets(ctx, list(asset_ids)).items()
    }
    return result


# ---------------------------------------------------------------- 迁移旧标记


def _has_option(value: Any, option: str) -> bool:
    """人自己记的一格算不算「已下载」：勾选框勾着、单选 / 文字是 option、多选里有 option。"""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip() == option
    if isinstance(value, list):
        return any(
            v == option
            or (isinstance(v, dict) and str(v.get("text", "")).strip() == option)
            for v in value
        )
    return False


def _downloaded_packages(ctx: Ctx) -> dict[str, tuple[str, str]]:
    """已经标了已下载（标的是现在这一版包）的交付包里的款：{asset_id: (seed_id, batch_id)}。"""
    out: dict[str, tuple[str, str]] = {}
    for seed_id, views in packages.by_seed(ctx).items():
        for view in views:
            if view["downloaded"]:
                for asset_id in package_assets(ctx, seed_id, view["batch_id"]):
                    out[asset_id] = (seed_id, view["batch_id"])
    return out


def _blocked_batches(reason: str) -> set[str] | None:
    """「交付包还没标已下载：B008、B010（…）」里的批次；不是这个原因返回 None。"""
    prefix = "交付包还没标已下载："
    if not reason.startswith(prefix):
        return None
    listed = reason[len(prefix) :].split("（", 1)[0]
    return {b.strip() for b in listed.split("、") if b.strip()}


def import_marks(
    ctx: Ctx, *, column: str, option: str, by: str, dry_run: bool = False
) -> dict[str, Any]:
    """把人在「选中的款」里自己记的下载状态收成下载确认：column 那一列是 option 的行算已下载（只收最近一套出完的）；
    已经标了已下载的交付包，包里的款也补上确认。dry_run 只列会怎样：会确认的款、会因此自动标已下载的批次、
    可能跟着自动归档的任务（开着 shelf.auto 时），什么都不改。可以重复执行。"""
    if not enabled(ctx):
        raise StageError(
            "下载确认没打开：table.download_confirm、table.enabled、scenes.enabled 都要开着"
        )
    info = feishu_table.base(ctx) or {}
    if not info:
        raise StageError("选款表还没建")
    with feishu.Client(ctx) as api:
        cells = read_column(api, info, column)
    rows = _rows_by_record(ctx)
    marked = [rows[r] for r, v in cells.items() if r in rows and _has_option(v, option)]
    from_packages = _downloaded_packages(ctx)
    found = items(ctx, list(dict.fromkeys(marked + list(from_packages))))
    plan_table = [
        a
        for a in marked
        if a in found and found[a]["ready"] and not found[a]["confirmed"]
    ]
    plan_package = [
        a
        for a in from_packages
        if a not in plan_table
        and a in found
        and found[a]["ready"]
        and not found[a]["confirmed"]
    ]
    skipped = [
        {
            "asset_id": a,
            "reason": _not_ready_text(found[a]["set"])
            if a in found
            else "不是选中的款",
        }
        for a in marked
        if a not in found or not found[a]["ready"]
    ]
    planned = set(plan_table) | set(plan_package)
    keys = {(found[a]["seed_id"], found[a]["batch_id"]) for a in planned}
    would_mark = []
    for seed_id, batch_id in sorted(keys):
        view = _package_view(ctx, seed_id, batch_id)
        if not view or not view["ready"] or view["stale"] or view["downloaded"]:
            continue
        assets = package_assets(ctx, seed_id, batch_id)
        if _covered(assets, items(ctx, list(assets)), planned):
            would_mark.append({"seed_id": seed_id, "batch_id": batch_id})
    names = labels.assets(ctx, list(planned) + [s["asset_id"] for s in skipped])
    summary = {
        "column": column,
        "option": option,
        "marked_rows": len(marked),
        "confirm": [{"asset_id": a, "label": names[a]["label"]} for a in plan_table],
        "from_packages": [
            {"asset_id": a, "label": names[a]["label"], "batch_id": from_packages[a][1]}
            for a in plan_package
        ],
        "skipped": [{**s, "label": names[s["asset_id"]]["label"]} for s in skipped],
        "would_mark": would_mark,
    }
    if dry_run:
        by_seed: dict[str, set[str]] = {}
        for key in would_mark:
            by_seed.setdefault(key["seed_id"], set()).add(key["batch_id"])
        may_archive = []
        if by_seed:
            swept = shelf.sweep(ctx, source="preview", dry_run=True, seeds=set(by_seed))
            for entry in swept.get("blocked") or []:
                pending = _blocked_batches(entry["reason"])
                if pending is not None and pending <= by_seed.get(
                    entry["seed_id"], set()
                ):
                    may_archive.append(
                        {"seed_id": entry["seed_id"], "label": entry["label"]}
                    )
            may_archive += [
                {"seed_id": e["seed_id"], "label": e["label"]}
                for e in swept.get("eligible") or []
            ]
            summary["auto_archive"] = bool(swept.get("auto"))
        return {"dry_run": True, **summary, "may_archive": may_archive}
    first = confirm(ctx, plan_table, by=by, source="migrate")
    second = confirm(ctx, plan_package, by=by, source="package", roll=False)
    with ctx.db.tx() as conn:
        log_event(
            conn,
            "table",
            "selected_downloads_imported",
            {
                "by": by,
                "column": column,
                "confirmed": len(first["confirmed"]),
                "from_packages": len(second["confirmed"]),
                "packages": [
                    f"{p['seed_id']}·{p['batch_id']}" for p in first["packages"]
                ],
            },
        )
    feishu_table.kick(ctx)
    return {
        "dry_run": False,
        **summary,
        "confirmed": len(first["confirmed"]) + len(second["confirmed"]),
        "packages": first["packages"],
    }


def status(ctx: Ctx) -> dict[str, Any]:
    """给 table status：开没开、列有没有、各组几行、还有几行要写、失败原因。"""
    info = feishu_table.base(ctx) or {}
    meta = _meta(ctx)
    out: dict[str, Any] = {
        "enabled": enabled(ctx),
        "ready": ready(info),
        "attempts": int(meta.get("attempts") or 0),
        "error": meta.get("error"),
    }
    if not out["enabled"] or not info:
        return out
    layout = _layout(ctx, items(ctx))
    groups = Counter(v["group"] for v in layout.values())
    out.update(
        rows=len(layout),
        to_download=groups["pending"],
        waiting=groups["waiting"],
        downloaded=groups["done"],
        writes_pending=len(_changes(layout, _cache(ctx))) if out["ready"] else 0,
    )
    return out
