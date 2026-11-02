"""上架标题同步（2026-10-30，feature-plans/pod-listing-autopilot）：人在「选中的款」或「已上架的款」改了「上架标题」
（cells.py 读回）→ 复核 → 记版本 → 按每家店所处的环节生效。

- title_sync 每（款, 店）一行：要生效的版本、目标（box = 存回妙手采集箱，temu = Temu 官方接口）、状态：
  - done：已生效（还没建箱的建箱时就用新标题；采集箱里的已存回、读回核对过；Temu 上读回已经是新标题）；
  - pending：等着（建箱中 / 发布中的等结果，采集箱里的下一轮存回）；
  - temu_submitted：已提交 Temu（编辑会进 Temu 审核，读回商品标题是新的就算生效）；
  - waiting_temu：已上架、Temu 接口还没接上（temu_open 关着），要人在 Temu 后台改；
  - invalid：人改的不合规（库里的标题不动、表里人填的值也不改回），failed：存不回去（原因在 last_error）。
- 「选中的款」的「标题同步」、「已上架的款」的「标题同步」都读 `cell_text`。
- 上架交给 mooya 时（2026-10-30，temu.engine: mooya）目标多两种：mooya（转 mooya 上架单的 title 动作，在箱的由 mooya
  存回采集箱）、listed（mooya 底账的改标题，接 Temu 接口在 mooya），见 mooya_listing.plan_title / process_titles。
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from . import stores, temu_title, trace
from .context import Ctx
from .stages.common import StageError
from .util import now_iso, parse_iso

STATE_DONE = "done"
STATE_PENDING = "pending"
STATE_TEMU_SUBMITTED = "temu_submitted"
STATE_WAITING_TEMU = "waiting_temu"
STATE_INVALID = "invalid"
STATE_FAILED = "failed"
TARGET_BOX = "box"
TARGET_TEMU = "temu"
MAX_ATTEMPTS = 5
TEXT = {
    STATE_DONE: "已生效",
    STATE_PENDING: "同步中",
    STATE_TEMU_SUBMITTED: "已提交 Temu（审核中）",
    STATE_WAITING_TEMU: "已上架：Temu 接口还没接上，要人在 Temu 后台改",
    STATE_INVALID: "不合规，用的还是原标题",
    STATE_FAILED: "没同步上",
}
# 这些状态的（款, 店）要等：建箱 / 发布有结果了再说
_WAIT_STATES = frozenset(
    {"creating", "create_unknown", "publishing", "publish_unknown"}
)


def _rows(
    ctx: Ctx, asset_id: str | None = None, states: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM title_sync WHERE 1 = 1"
    params: list[Any] = []
    if asset_id:
        sql += " AND asset_id = ?"
        params.append(asset_id)
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params += list(states)
    return [
        dict(r) for r in ctx.db.q(sql + " ORDER BY asset_id, store_key", tuple(params))
    ]


def _upsert(
    ctx: Ctx,
    row: dict[str, Any],
    *,
    version_id: int,
    title: str,
    state: str,
    target: str | None,
    error: str | None = None,
    done: bool = False,
) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO title_sync(asset_id, store_key, version_id, title, state, target, modify_id, attempts,"
            " last_error, submitted_at, done_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, NULL, 0, ?, NULL, ?, ?)"
            " ON CONFLICT(asset_id, store_key) DO UPDATE SET version_id = excluded.version_id,"
            " title = excluded.title, state = excluded.state, target = excluded.target, modify_id = NULL,"
            " attempts = 0, last_error = excluded.last_error, submitted_at = NULL, done_at = excluded.done_at,"
            " updated_at = excluded.updated_at",
            (
                row["asset_id"],
                row["store_key"],
                version_id,
                title,
                state,
                target,
                error,
                now if done else None,
                now,
            ),
        )
        trace.record(
            conn,
            row["asset_id"],
            "title_sync",
            to=state,
            store=row["store_key"],
            code=row.get("code"),
            reason=error,
            payload={"version": version_id, "target": target},
        )


def mark(ctx: Ctx, asset_id: str, store_key: str, state: str, **values: Any) -> None:
    """改一（款, 店）的同步状态（modify_id / last_error / attempts / submitted_at…）；生效时记 done_at。"""
    now = now_iso()
    values = {"state": state, "updated_at": now, **values}
    if state == STATE_DONE:
        values.setdefault("done_at", now)
    cols = ", ".join(f"{k} = ?" for k in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE title_sync SET {cols} WHERE asset_id = ? AND store_key = ?",
            (*values.values(), asset_id, store_key),
        )
        trace.record(
            conn,
            asset_id,
            "title_sync",
            to=state,
            store=store_key,
            reason=values.get("last_error"),
            payload={k: v for k, v in values.items() if k in ("modify_id", "target")}
            or None,
        )


def _plan(ctx: Ctx, row: dict[str, Any]) -> tuple[str, str | None]:
    """一（款, 店）收到一个新标题时：(状态, 目标)。上架交给 mooya 时（temu.engine: mooya）按 mooya 上架单的环节排
    （目标 mooya = 转 mooya 的 title 动作，listed = mooya 底账改标题，mooya_listing.process_titles 去做）。"""
    from . import mooya_listing, temu_open  # 都依赖上架模块，用时再导入

    if mooya_listing.enabled(ctx):
        return mooya_listing.plan_title(ctx, row)
    state = row["state"]
    if state == "listed":
        return (
            (STATE_PENDING, TARGET_TEMU)
            if temu_open.enabled(ctx)
            else (STATE_WAITING_TEMU, TARGET_TEMU)
        )
    if state == "in_box" or (state == "failed" and row.get("detail_id")):
        return STATE_PENDING, TARGET_BOX
    if state in _WAIT_STATES:
        return STATE_PENDING, TARGET_BOX
    # 还没建箱：建箱时就用库里的新标题
    return STATE_DONE, None


def handle_edit(
    ctx: Ctx,
    asset_id: str,
    title: str,
    *,
    by: str,
    via: str,
    custom: bool | None = None,
) -> dict[str, Any]:
    """人在表里改了这一款的上架标题：和库里的一样就不管；合规的存下、每家店按环节排上同步；不合规的每家店记 invalid。
    custom：顺带改定制款判定（对话里 `temu title --set … --custom`）。"""
    from . import temu  # 上架模块依赖本模块，用时再导入

    text = (title or "").strip()
    current = temu_title.get(ctx, asset_id)
    if not text or (current and current["title"].strip() == text):
        return {"same": True}
    items = stores.rows(ctx, asset_ids=[asset_id])
    try:
        temu_title.save_human(
            ctx,
            asset_id,
            text,
            by=by,
            limit=temu._title_limit(ctx),
            via=via,
            custom=custom,
        )
    except StageError as exc:
        version = temu_title.latest_version(ctx, asset_id, ok_only=False) or {}
        for row in items:
            _upsert(
                ctx,
                row,
                version_id=int(version.get("version_id") or 0),
                title=text,
                state=STATE_INVALID,
                target=None,
                error=str(exc)[:300],
            )
        return {"rejected": str(exc)}
    version = temu_title.latest_version(ctx, asset_id) or {}
    planned = []
    for row in items:
        state, target = _plan(ctx, row)
        _upsert(
            ctx,
            row,
            version_id=int(version.get("version_id") or 0),
            title=text,
            state=state,
            target=target,
            error="Temu 接口还没接上：已上架的商品要人在 Temu 后台改标题"
            if state == STATE_WAITING_TEMU
            else None,
            done=state == STATE_DONE,
        )
        planned.append({"store": row["store_key"], "state": state, "target": target})
    if any(p["state"] == STATE_PENDING for p in planned):
        temu.kick(ctx)
    return {"saved": True, "version": version.get("version_id"), "stores": planned}


def due(ctx: Ctx) -> bool:
    """有要往妙手采集箱存的标题（对应的店已经在采集箱里，或没上成还留着采集箱商品）。"""
    for item in _rows(ctx, states=(STATE_PENDING,)):
        if item["target"] != TARGET_BOX:
            continue
        row = _store_row(ctx, item)
        if row and (
            row["state"] == "in_box"
            or (row["state"] == "failed" and row.get("detail_id"))
        ):
            return True
        if row and row["state"] in ("chosen", "queued", "listed"):
            return True
    return False


def _store_row(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any] | None:
    found = [
        r
        for r in stores.rows(ctx, asset_ids=[item["asset_id"]])
        if r["store_key"] == item["store_key"]
    ]
    return found[0] if found else None


def process_box(ctx: Ctx, api: Any, out: dict[str, Any], touched: set[str]) -> None:
    """上架后台每一轮：采集箱里的（款, 店）把新标题存回妙手（站点模式详情只改标题、读回核对，再核对品牌）。
    还没建箱的改记生效；建箱中 / 发布中的等；已经上架了的转给 Temu 接口。"""
    from . import miaoshou, temu, temu_open  # 都依赖本模块，用时再导入

    for item in _rows(ctx, states=(STATE_PENDING,)):
        if item["target"] != TARGET_BOX:
            continue
        row = _store_row(ctx, item)
        if row is None:
            mark(
                ctx,
                item["asset_id"],
                item["store_key"],
                STATE_DONE,
                last_error="这家店已经不上了",
            )
            continue
        state = row["state"]
        if state in ("chosen", "queued"):
            mark(ctx, item["asset_id"], item["store_key"], STATE_DONE)
            touched.add(item["asset_id"])
            continue
        if state == "listed":
            new = STATE_PENDING if temu_open.enabled(ctx) else STATE_WAITING_TEMU
            mark(
                ctx,
                item["asset_id"],
                item["store_key"],
                new,
                target=TARGET_TEMU,
                last_error=None
                if new == STATE_PENDING
                else "Temu 接口还没接上：已上架的商品要人在 Temu 后台改标题",
            )
            touched.add(item["asset_id"])
            continue
        if state in _WAIT_STATES or not row.get("detail_id"):
            continue
        try:
            info = (api.site_detail(row["detail_id"]) or {}).get(
                "siteCollectItemInfo"
            ) or {}
            if not info:
                raise StageError("妙手没给站点模式详情")
            payload = {
                k: v for k, v in info.items() if k in temu.CREATE_KEYS and v is not None
            }
            payload["title"] = item["title"]
            payload["multiLanguageTitleMap"] = {
                **(info.get("multiLanguageTitleMap") or {}),
                "en": item["title"],
            }
            api.save_site_detail(row["detail_id"], payload)
            again = (api.site_detail(row["detail_id"]) or {}).get(
                "siteCollectItemInfo"
            ) or {}
            if (again.get("title") or "").strip() != item["title"].strip():
                raise StageError(
                    f"妙手没存上（读回来是「{(again.get('title') or '')[:40]}」）"
                )
        except miaoshou.MiaoshouError as exc:
            if exc.kind == miaoshou.KIND_INVALID:
                _bump(ctx, item, str(exc))
                touched.add(item["asset_id"])
                continue
            temu._stop_on(ctx, exc, out)
            continue
        except StageError as exc:
            _bump(ctx, item, str(exc))
            touched.add(item["asset_id"])
            continue
        temu._update(ctx, row, title=item["title"])
        mark(ctx, item["asset_id"], item["store_key"], STATE_DONE, last_error=None)
        temu._ensure_brand(
            ctx, api, row, stores.get(ctx, row["store_key"]), out, force=True
        )
        out.setdefault("titles_saved", []).append(row["code"])
        touched.add(item["asset_id"])


def _bump(ctx: Ctx, item: dict[str, Any], error: str) -> None:
    attempts = int(item.get("attempts") or 0) + 1
    state = STATE_FAILED if attempts >= MAX_ATTEMPTS else STATE_PENDING
    mark(
        ctx,
        item["asset_id"],
        item["store_key"],
        state,
        attempts=attempts,
        last_error=error[:300],
    )


def temu_targets(ctx: Ctx) -> list[dict[str, Any]]:
    """要走 Temu 接口的：已上架、等提交的和已提交等生效的。"""
    return [
        item
        for item in _rows(
            ctx, states=(STATE_PENDING, STATE_TEMU_SUBMITTED, STATE_WAITING_TEMU)
        )
        if item["target"] == TARGET_TEMU
    ]


def _tz(ctx: Ctx) -> ZoneInfo:
    return ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))


def _when(ctx: Ctx, ts: str | None) -> str:
    at = parse_iso(ts)
    return f"（{at.astimezone(_tz(ctx)):%m-%d %H:%M}）" if at else ""


def cell_text(ctx: Ctx, asset_id: str) -> str | None:
    """「标题同步」那一格：每家店一句；没改过标题为 None。"""
    items = _rows(ctx, asset_id)
    if not items:
        return None
    parts = []
    for item in items:
        try:
            label = stores.get(ctx, item["store_key"]).label
        except StageError:
            label = item["store_key"]
        text = TEXT.get(item["state"], item["state"])
        if item["state"] == STATE_DONE:
            text += (
                "（存进妙手采集箱）"
                if item["target"] == TARGET_BOX
                else ("（Temu）" if item["target"] == TARGET_TEMU else "（建箱时用）")
            )
            text += _when(ctx, item.get("done_at"))
        elif item.get("last_error") and item["state"] in (
            STATE_INVALID,
            STATE_FAILED,
            STATE_PENDING,
        ):
            text += f"：{item['last_error'][:80]}"
        parts.append(f"{label}：{text}")
    return "；".join(parts)[:500]


def status(ctx: Ctx, asset_id: str | None = None) -> list[dict[str, Any]]:
    return _rows(ctx, asset_id)
