"""套图（场景图）：选中的款交给 mooya 出场景图，拉回原图存进素材库，状态和缩略图写进「选中的款」表。

- 一款每出一次是一行 scene_sets（SS0001），拉回的图在 scene_images。场景图怎么出全由 mooya 决定（mooya.py）。
- 入队 `enqueue`：「选款完成」时自动给这个任务里选中、已入库、还没出过套图的款入队（`after_accept`）；
  也可以手动 `podctl scenes start --seed S01 | --asset IMG…`（`--force` 连已出过的也重出）。
- 推进 `sync`（后台 `podctl scenes sync`，租约 scenes_syncer，同一时刻只有一个进程）：同时最多
  `scenes.max_in_flight` 款在 mooya 上出图（停着等人工 / 已暂停的不算，刚停下时按任务发「套图等人工」卡片）；
  轮询进度，出完把原图拉回 assets/scenes/<款>/<套图>/，每款打一个套图压缩包放 OSS 给签名链接；
  同一轮出完的按任务合并发一张卡片。
- 压缩包里是设计图 + mooya 交付清单里的文件（`delivery`，表 scene_files）：顺序和文件名就是 mooya 交付工作台
  下载这个交付包的结果（001.png…），命名规则只在 mooya 那边。没有清单的旧套图仍是设计图 + 白底图 + 场景图。
- 「选中的款」表的套图列由 `podctl table sync` 写（`sync_table`），和选款表共用写入进程与租约。
- 拉起 `kick`：入队后、巡检时；mooya 令牌 / 租户配置出错后隔 SERVICE_RETRY_MINUTES 再试。
- 留痕（2026-10-30）：入队、交给 mooya、mooya 那边状态变了、出完都记一条 trace；scene_set_flags 记交 mooya 时用的
  包名（货号或素材号）、mooya 白底图质检结果（`baseline`，没过时 mooya 用最高分那张接着出、带 fallback）、停下的时间、自愈次数。
- 自愈（`scenes.auto_heal`，缺省开，只管定了店铺、还没进采集箱的款）：缺图（partial 过不了图位校验）先让 mooya 补缺图
  （`regenerate_missing`，最多 HEAL_MISSING_MAX 次），mooya 没有这个端点或补完还缺就整套重出；没出成的整套重出
  （两种重出合计每款最多 `scenes.auto_retry_failed` 次，缺省 1，requested_by = system:heal）；
  暂停超过 `scenes.auto_resume_minutes`（缺省 120）让 mooya 接着跑（最多 HEAL_RESUME_MAX 次）。都不行进「POD 待办」。
  套图出完马上拉起上架（temu.kick），不等下一次巡检。
"""

from __future__ import annotations

import datetime as dt
import re
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import bg, feishu, feishu_table, images, labels, lease, mooya, store, table_registry, trace
from .context import Ctx
from .db import log_event, next_id
from .oss import OSSError
from .stages.common import StageError, task_label
from .stages.plan import asset_label_of
from .util import dumps, iso, loads, now_iso, parse_iso, sha256_bytes, utcnow

STATUS_LABELS = {
    "queued": "排队中",  # 还没交给 mooya
    "submitted": "排队中",  # mooya 收下了、还没开始
    "running": "出图中",
    "waiting_human": "等人工",
    "paused": "已暂停",
    "done": "已出完",
    "partial": "缺图",
    "failed": "没出成",
    "cancelled": "已取消",
}
ACTIVE = ("queued", "submitted", "running", "waiting_human", "paused")
IN_FLIGHT = ("submitted", "running", "waiting_human", "paused")
# mooya 那边停着等人：后台进程不为它们一直守着，交给巡检隔一阵查一次
PARKED = ("waiting_human", "paused")
# 占 mooya 出图名额的（scenes.max_in_flight 只数它们）：停着等人的在 mooya 那边不跑，不占
BUSY = ("submitted", "running")
OK_FINAL = ("done", "partial")
FINAL = ("done", "partial", "failed", "cancelled")
# mooya 的进行中状态 → 本地状态
_LOCAL = {
    "queued": "submitted",
    "running": "running",
    "waiting_human": "waiting_human",
    "paused": "paused",
}
LEASE_KEY = "scenes_syncer"
LEASE_SECONDS = 300
SERVICE_ERROR_META_KEY = "scenes_service_error"
INSTANCE_META_KEY = "instance_id"
SERVICE_RETRY_MINUTES = 30
# 一个后台进程里连续几轮都连不上 mooya 就先退出，等下次巡检
MAX_TRANSIENT_PASSES = 3
# 自愈：缺图让 mooya 补几次、暂停让 mooya 接着跑几次；整套重出的次数看 scenes.auto_retry_failed
HEAL_MISSING_MAX = 2
HEAL_RESUME_MAX = 2
HEAL_BY = "system:heal"
SCENE_DIR = "assets/scenes"
EXPORT_DIR = "exports/scenes"
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\s]+')
_FAILURES = (feishu.FeishuError, store.StorageError, OSError, httpx.HTTPError, KeyError)
_OSS_FAILURES = (OSSError, store.StorageError, httpx.HTTPError)
_sleep = time.sleep


def enabled(ctx: Ctx) -> bool:
    return bool(ctx.setting("scenes.enabled", False))


def _marks(values: tuple[str, ...] | list[str]) -> str:
    return ",".join("?" for _ in values)


def set_order(row: dict[str, Any]) -> tuple[str, int]:
    """同一款的几次套图按先后排（建的时间，同一秒再看编号）。"""
    digits = "".join(ch for ch in str(row["set_id"]) if ch.isdigit())
    return (row["created_at"] or "", int(digits or 0))


# ---------------------------------------------------------------- 查询


def get_set(ctx: Ctx, set_id: str) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM scene_sets WHERE set_id = ?", (set_id,))
    if not row:
        raise StageError(f"没有套图 {set_id}")
    return dict(row)


def latest_sets(
    ctx: Ctx, asset_ids: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """每款最近一次的套图：asset_id → 行。"""
    if asset_ids is None:
        rows = [dict(r) for r in ctx.db.q("SELECT * FROM scene_sets")]
    else:
        rows = []
        ids = sorted(set(asset_ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows += [
                dict(r)
                for r in ctx.db.q(
                    f"SELECT * FROM scene_sets WHERE asset_id IN ({_marks(chunk)})",
                    tuple(chunk),
                )
            ]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = out.get(row["asset_id"])
        if current is None or set_order(row) > set_order(current):
            out[row["asset_id"]] = row
    return out


def approved_assets(
    ctx: Ctx, *, seed_id: str | None = None, asset_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """选中、已入库（S10，assets.status = archived）的款（出套图的对象），按批次、方向、素材排。"""
    sql = (
        "SELECT a.asset_id, a.path, a.variant_id, v.batch_id, f.seed_id FROM assets a"
        " JOIN variants v ON v.variant_id = a.variant_id"
        " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " WHERE a.human_decision = 'approved' AND a.status = 'archived'"
    )
    params: list[Any] = []
    if seed_id:
        sql += " AND f.seed_id = ?"
        params.append(seed_id)
    if asset_ids:
        sql += f" AND a.asset_id IN ({_marks(asset_ids)})"
        params += list(asset_ids)
    sql += " ORDER BY v.batch_id, a.variant_id, a.asset_id"
    return [dict(r) for r in ctx.db.q(sql, tuple(params))]


def seed_counts(ctx: Ctx) -> dict[str, dict[str, int]]:
    """每个任务（参考图）的套图进度：选中几款、出完 / 在出 / 没出成 / 还差几款没出。"""
    approved = approved_assets(ctx)
    latest = latest_sets(ctx, [a["asset_id"] for a in approved])
    out: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "approved": 0,
            "done": 0,
            "active": 0,
            "parked": 0,
            "failed": 0,
            "missing": 0,
        }
    )
    for asset in approved:
        counts = out[asset["seed_id"]]
        counts["approved"] += 1
        row = latest.get(asset["asset_id"])
        status = row["status"] if row else None
        if status in OK_FINAL:
            counts["done"] += 1
        elif status in ACTIVE:
            counts["active"] += 1
            if status in PARKED:
                counts["parked"] += 1
        else:
            counts["missing"] += 1
            if status in ("failed", "cancelled"):
                counts["failed"] += 1
    return dict(out)


# ---------------------------------------------------------------- 入队


def _skip_reason(row: Any, *, force: bool, retry_failed: bool) -> str | None:
    """这款最近一套（没有为 None）挡不挡这次入队：在出的不重复交；出完过的要 force；没出成 / 停掉的看 retry_failed。"""
    if not row:
        return None
    status, label = row["status"], STATUS_LABELS[row["status"]]
    if status in ACTIVE:
        return f"{row['set_id']} {label}"
    if status in OK_FINAL and not force:
        return f"已出过套图（{row['set_id']} {label}），要重出加 --force"
    if status not in OK_FINAL and not retry_failed:
        return f"上次{label}（{row['set_id']}），要重出用「出套图」"
    return None


def enqueue(
    ctx: Ctx,
    *,
    by: str,
    seed_id: str | None = None,
    asset_ids: list[str] | None = None,
    force: bool = False,
    retry_failed: bool = True,
) -> dict[str, Any]:
    """给选中的款入队出套图。正在出的不重复入队；出完过的要 force；没出成 / 停掉的看 retry_failed。"""
    if not enabled(ctx):
        raise StageError("scenes.enabled 为 false：套图没有打开")
    if not seed_id and not asset_ids:
        raise StageError("要写 --seed 或 --asset")
    candidates = approved_assets(ctx, seed_id=seed_id, asset_ids=asset_ids)
    skipped: list[dict[str, str]] = []
    if asset_ids:
        found = {a["asset_id"] for a in candidates}
        skipped += [
            {"asset_id": a, "reason": "不是已选中的款（要先在选款里选中）"}
            for a in asset_ids
            if a not in found
        ]
    latest = latest_sets(ctx, [a["asset_id"] for a in candidates])
    queue = []
    for asset in candidates:
        reason = _skip_reason(
            latest.get(asset["asset_id"]), force=force, retry_failed=retry_failed
        )
        if reason:
            skipped.append({"asset_id": asset["asset_id"], "reason": reason})
            continue
        queue.append(asset)
    preset = str(ctx.setting("mooya.preset") or "pod_blanket")
    queued = []
    now = now_iso()
    with ctx.db.tx() as conn:
        for asset in queue:
            # 写事务是串行的（Postgres 全局 advisory lock / SQLite BEGIN IMMEDIATE）：插之前再看一眼，
            # 免得两个进程同时入队（连点两次「出套图」、选款完成和手动同时到）把同一款出两套
            current = conn.execute(
                "SELECT set_id, status FROM scene_sets WHERE asset_id = ?"
                " ORDER BY created_at DESC, set_id DESC LIMIT 1",
                (asset["asset_id"],),
            ).fetchone()
            reason = _skip_reason(current, force=force, retry_failed=retry_failed)
            if reason:
                skipped.append({"asset_id": asset["asset_id"], "reason": reason})
                continue
            set_id = next_id(conn, "scene")
            conn.execute(
                "INSERT INTO scene_sets(set_id, asset_id, seed_id, status, preset, requested_by, created_at, updated_at)"
                " VALUES(?, ?, ?, 'queued', ?, ?, ?, ?)",
                (set_id, asset["asset_id"], asset["seed_id"], preset, by, now, now),
            )
            log_event(
                conn,
                set_id,
                "scenes_queued",
                {
                    "asset_id": asset["asset_id"],
                    "seed_id": asset["seed_id"],
                    "by": by,
                    "force": force,
                },
            )
            trace.record(
                conn,
                asset["asset_id"],
                "scenes",
                frm=current["status"] if current else None,
                to="queued",
                set_id=set_id,
                actor=by,
                payload={"force": force, "previous": current["set_id"] if current else None},
            )
            queued.append(
                {
                    "set_id": set_id,
                    "asset_id": asset["asset_id"],
                    "seed_id": asset["seed_id"],
                }
            )
    return {"queued": queued, "skipped": skipped}


def start(
    ctx: Ctx,
    *,
    by: str,
    seed_id: str | None = None,
    asset_ids: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """手动出套图：入队并拉起后台。"""
    out = enqueue(ctx, by=by, seed_id=seed_id, asset_ids=asset_ids, force=force)
    task = kick(ctx) if out["queued"] else None
    return {**out, "sync_task": (task or {}).get("task_id")}


def after_accept(ctx: Ctx, seed_id: str, *, by: str) -> dict[str, Any]:
    """选款完成之后：给这个任务里选中、还没出过套图的款入队并拉起后台；关着或不自动时只报还差几款。"""
    if not enabled(ctx):
        return {"enabled": False}
    if not ctx.setting("scenes.auto_after_accept", True):
        return {
            "enabled": True,
            "auto": False,
            "missing": seed_counts(ctx).get(seed_id, {}).get("missing", 0),
        }
    try:
        out = enqueue(ctx, by=by, seed_id=seed_id, retry_failed=False)
    except StageError as exc:
        return {"enabled": True, "auto": True, "error": str(exc)}
    task = kick(ctx) if out["queued"] else None
    return {
        "enabled": True,
        "auto": True,
        "queued": len(out["queued"]),
        "skipped": len(out["skipped"]),
        "sync_task": (task or {}).get("task_id"),
    }


# ---------------------------------------------------------------- 后台推进


def _service_error(ctx: Ctx) -> dict[str, Any]:
    return loads(ctx.db.get_meta(SERVICE_ERROR_META_KEY), {}) or {}


def _service_backoff(ctx: Ctx) -> bool:
    error = _service_error(ctx)
    return bool(error) and utcnow() - parse_iso(error["at"]) < dt.timedelta(
        minutes=SERVICE_RETRY_MINUTES
    )


def _record_service_error(ctx: Ctx, exc: BaseException) -> None:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(
            conn,
            SERVICE_ERROR_META_KEY,
            dumps({"at": now_iso(), "error": str(exc)[:500]}),
        )
        log_event(conn, "scenes", "scenes_service_error", {"error": str(exc)[:300]})


def _clear_service_error(ctx: Ctx) -> None:
    if ctx.db.get_meta(SERVICE_ERROR_META_KEY):
        with ctx.db.tx() as conn:
            ctx.db.del_meta(conn, SERVICE_ERROR_META_KEY)


def _has_work(ctx: Ctx, statuses: tuple[str, ...] = ACTIVE) -> bool:
    return bool(
        ctx.db.scalar(
            f"SELECT COUNT(*) FROM scene_sets WHERE status IN ({_marks(statuses)})",
            statuses,
        )
    )


def _ready_queued(ctx: Ctx) -> bool:
    """排着的套图里有不等货号、这就能交的。"""
    from . import stores  # 同 _submit，用时再导入

    queued = [r["asset_id"] for r in _rows(ctx, ("queued",))]
    return bool(set(queued) - stores.waiting_codes(ctx, queued))


def kick(ctx: Ctx) -> dict[str, Any] | None:
    """有在排队 / 在出的套图、又没有套图进程在跑时，拉起后台 `podctl scenes sync`（收尾时顺带打交付包）；
    没有套图要推进（或 mooya 配置出错在退避）、只有交付包要打 / 续签时拉起 `podctl scenes pack`。"""
    from . import packages  # 交付包依赖本模块，放到用时再导入

    if not enabled(ctx) or lease.active(ctx, LEASE_KEY):
        return None
    work = (_has_work(ctx) or bool(heal_targets(ctx))) and not _service_backoff(ctx)
    if not work and (
        lease.active(ctx, packages.LEASE_KEY) or not packages.has_due(ctx)
    ):
        return None
    if any(t["kind"] == "scenes" for t in bg.running(ctx)):
        return None
    if work:
        return bg.spawn(ctx, ["scenes", "sync"], kind="scenes")
    return bg.spawn(ctx, ["scenes", "pack"], kind="scenes")


def _rows(ctx: Ctx, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in ctx.db.q(
            f"SELECT * FROM scene_sets WHERE status IN ({_marks(statuses)}) ORDER BY set_id",
            statuses,
        )
    ]


def _update(ctx: Ctx, set_id: str, **values: Any) -> None:
    values = {**values, "updated_at": now_iso()}
    sets = ", ".join(f"{k} = ?" for k in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE scene_sets SET {sets} WHERE set_id = ?", (*values.values(), set_id)
        )


def _progress(job: dict[str, Any]) -> dict[str, Any]:
    render = job.get("render") or {}
    return {
        "mooya_status": job.get("status"),
        "step_label": job.get("currentStepLabel"),
        "expected": render.get("expected"),
        "actual": render.get("actual"),
    }


def _bump(ctx: Ctx, row: dict[str, Any], exc: BaseException) -> bool:
    """这一款出错一次：记下原因，满 scenes.max_attempts 次记没出成。返回是否已经记成没出成。"""
    attempts = int(row["attempts"] or 0) + 1
    error = f"{type(exc).__name__}: {exc}"[:500]
    if attempts >= int(ctx.setting("scenes.max_attempts", 6)):
        _finish(ctx, row, "failed", error=f"连续 {attempts} 次没成功：{error}")
        return True
    _update(ctx, row["set_id"], attempts=attempts, last_error=error)
    return False


def _finish(
    ctx: Ctx,
    row: dict[str, Any],
    status: str,
    *,
    error: str | None = None,
    job: dict[str, Any] | None = None,
    export: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "last_error": error,
        "finished_at": now_iso(),
    }
    if job:
        values.update(_progress(job))
    if export:
        values.update(
            export_key=export.get("key"),
            export_path=export.get("path"),
            export_url=export.get("url"),
            export_expires_at=export.get("expires_at"),
            export_count=export.get("count"),
            export_bytes=export.get("bytes"),
        )
    _update(ctx, row["set_id"], **values)
    images_count = ctx.db.scalar(
        "SELECT COUNT(*) FROM scene_images WHERE set_id = ? AND role = 'scene'",
        (row["set_id"],),
    )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            row["set_id"],
            "scenes_finished",
            {
                "asset_id": row["asset_id"],
                "status": status,
                "scenes": int(images_count or 0),
                "error": (error or "")[:200] or None,
            },
        )
        trace.record(
            conn,
            row["asset_id"],
            "scenes",
            frm=row.get("status"),
            to=status,
            set_id=row["set_id"],
            reason=error,
            payload={"scenes": int(images_count or 0), "mooya_job_id": row.get("mooya_job_id")},
        )
    if row.get("parked_at") or _flags(ctx, row["set_id"]).get("parked_at"):
        _save_flags(ctx, row["set_id"], row["asset_id"], parked_at=None)


# ---------------------------------------------------------------- 附加记录（scene_set_flags）


def _flags(ctx: Ctx, set_id: str) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM scene_set_flags WHERE set_id = ?", (set_id,))
    if not row:
        return {}
    out = dict(row)
    out["baseline"] = loads(out.pop("baseline_json"), None)
    out["heal"] = loads(out.pop("heal_json"), {}) or {}
    return out


def flags(ctx: Ctx, set_id: str) -> dict[str, Any]:
    """一套套图的附加记录：{package_name, baseline, parked_at, heal}（没有为空）。"""
    return _flags(ctx, set_id)


_UNSET = object()


def _save_flags(
    ctx: Ctx,
    set_id: str,
    asset_id: str,
    *,
    package_name: Any = _UNSET,
    baseline: Any = _UNSET,
    parked_at: Any = _UNSET,
    heal: Any = _UNSET,
) -> None:
    """改一套套图的附加记录（只改给了的几项）。"""
    values: dict[str, Any] = {}
    if package_name is not _UNSET:
        values["package_name"] = package_name
    if baseline is not _UNSET:
        values["baseline_json"] = None if baseline is None else dumps(baseline)
    if parked_at is not _UNSET:
        values["parked_at"] = parked_at
    if heal is not _UNSET:
        values["heal_json"] = None if heal is None else dumps(heal)
    if not values:
        return
    values["updated_at"] = now_iso()
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"INSERT INTO scene_set_flags(set_id, asset_id, {cols}) VALUES(?, ?, {marks})"
            f" ON CONFLICT(set_id) DO UPDATE SET {sets}",
            (set_id, asset_id, *values.values()),
        )


def _note_baseline(ctx: Ctx, row: dict[str, Any], job: dict[str, Any]) -> None:
    """mooya 给了白底图质检结果就记下；用了最高分兜底的记一条轨迹（上架备注里会写）。"""
    info = mooya.baseline_info(job)
    if not info:
        return
    current = _flags(ctx, row["set_id"]).get("baseline")
    if current == info:
        return
    _save_flags(ctx, row["set_id"], row["asset_id"], baseline=info)
    if info.get("fallback"):
        trace.log(
            ctx,
            row["asset_id"],
            "scenes",
            set_id=row["set_id"],
            source="mooya",
            reason=f"白底图两轮质检都没过，mooya 用了最高分那张（{info.get('score')} 分）接着出",
            payload={"baseline": info},
        )


def baseline_note(ctx: Ctx, set_id: str) -> str | None:
    """这套套图白底图是兜底选的时写进上架备注的一句话；正常过了为 None。"""
    info = _flags(ctx, set_id).get("baseline") or {}
    if not info.get("fallback"):
        return None
    score = info.get("score")
    return "白底图质检没过，mooya 用了最高分那张" + (f"（{score} 分）" if score is not None else "")


def instance_id(ctx: Ctx) -> str:
    """这个业务库的编号（第一次用时生成）。SS 编号是每个库各自数的，拼进 mooya 幂等键，
    免得另一个数据目录（测试、重建的库）用同一个令牌时拿回别人的任务。"""
    value = ctx.db.get_meta(INSTANCE_META_KEY)
    if value:
        return value
    with ctx.db.tx() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (INSTANCE_META_KEY,)
        ).fetchone()
        if row:
            return row[0]
        value = uuid.uuid4().hex[:12]
        ctx.db.set_meta(conn, INSTANCE_META_KEY, value)
    return value


def idempotency_key(ctx: Ctx, set_id: str) -> str:
    return f"pod-blanket:{instance_id(ctx)}:{set_id}"


def client_of(ctx: Ctx, set_id: str) -> str:
    """这套套图是哪个 mooya 客户端交的（2026-10-28 前交的都是 legacy）。"""
    value = ctx.db.scalar(
        "SELECT client FROM scene_set_clients WHERE set_id = ?", (set_id,)
    )
    return value or mooya.LEGACY_CLIENT


def set_clients(
    ctx: Ctx, job_ids: list[int], client: str, *, by: str, dry_run: bool = False
) -> dict[str, Any]:
    """mooya 里把这些任务挪到别的租户、服务客户端也改了之后（mooya `scripts/move_service_jobs_tenant.py`），
    把对应的套图改记成那个客户端：之后查进度、取图、取带字标的上架图都走它（mooya 按租户和客户端认任务）。"""
    names = mooya.client_names(ctx)
    if client not in names:
        raise StageError(f"mooya.clients 里没有 {client}（有：{'、'.join(names)}）")
    ids = sorted({int(j) for j in job_ids})
    if not ids:
        raise StageError("没有要改的 mooya 任务")
    marks = ",".join("?" * len(ids))
    rows = ctx.db.q(
        f"SELECT set_id, asset_id, mooya_job_id FROM scene_sets WHERE mooya_job_id IN ({marks}) ORDER BY set_id",
        tuple(ids),
    )
    changed = []
    unchanged = []
    for row in rows:
        old = client_of(ctx, row["set_id"])
        item = {"set_id": row["set_id"], "asset_id": row["asset_id"], "from": old}
        (unchanged if old == client else changed).append(item)
    if changed and not dry_run:
        now = now_iso()
        with ctx.db.tx() as conn:
            for item in changed:
                conn.execute(
                    "INSERT INTO scene_set_clients(set_id, client, created_at) VALUES(?, ?, ?)"
                    " ON CONFLICT(set_id) DO UPDATE SET client = excluded.client",
                    (item["set_id"], client, now),
                )
                log_event(
                    conn,
                    item["asset_id"],
                    "scene_client_moved",
                    {"set_id": item["set_id"], "from": item["from"], "to": client, "by": by},
                )
    return {
        "client": client,
        "dry_run": dry_run,
        "changed": len(changed),
        "unchanged": len(unchanged),
        "missing_jobs": sorted(set(ids) - {int(r["mooya_job_id"]) for r in rows}),
        "sets": [item["set_id"] for item in changed],
    }


def _submit(ctx: Ctx, api: mooya.Client, row: dict[str, Any]) -> dict[str, Any]:
    from . import stores  # 店铺货号依赖本模块之外的表格，放到用时再导入

    asset = ctx.db.q1(
        "SELECT asset_id, path FROM assets WHERE asset_id = ?", (row["asset_id"],)
    )
    if not asset:
        raise store.StorageError(f"素材 {row['asset_id']} 不见了")
    local = store.ensure_local(ctx, asset["path"])
    data = local.read_bytes()
    # 选款时定了店铺的款用货号当编号：mooya 拿它给交付包起名（如 FLow0006），序号就在 mooya 那边接着排
    code = stores.primary_code(ctx, row["asset_id"])
    ref = code or row["asset_id"]
    out = api.submit(
        data=data,
        filename=f"{ref}{local.suffix or '.png'}",
        preset=row["preset"],
        client_ref=ref,
        idempotency_key=idempotency_key(ctx, row["set_id"]),
        label=" · ".join(p for p in (row["seed_id"], row["asset_id"], code) if p),
    )
    job = out["job"]
    status = job.get("status")
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO scene_set_clients(set_id, client, created_at) VALUES(?, ?, ?)"
            " ON CONFLICT(set_id) DO UPDATE SET client = excluded.client",
            (row["set_id"], api.name, now_iso()),
        )
    _update(
        ctx,
        row["set_id"],
        # 幂等命中一个已经结束的任务：先记已提交，下一轮查进度时拉图
        status=_LOCAL.get(status, "submitted"),
        mooya_job_id=int(job["jobId"]),
        submitted_at=now_iso(),
        attempts=0,
        last_error=None,
        **_progress(job),
    )
    with ctx.db.tx() as conn:
        log_event(
            conn,
            row["set_id"],
            "scenes_submitted",
            {
                "asset_id": row["asset_id"],
                "job_id": job["jobId"],
                "created": out.get("created"),
                "name": ref,
            },
        )
        trace.record(
            conn,
            row["asset_id"],
            "scenes",
            frm=row.get("status"),
            to=_LOCAL.get(status, "submitted"),
            set_id=row["set_id"],
            code=code,
            source="mooya",
            payload={"mooya_job_id": job["jobId"], "name": ref, "client": api.name, "created": out.get("created")},
        )
    _save_flags(ctx, row["set_id"], row["asset_id"], package_name=ref)
    return job


def _save_image(
    ctx: Ctx, row: dict[str, Any], item: dict[str, Any], data: bytes, rev: str
) -> None:
    fmt, width, height = images.probe(data)
    ext = images.FORMAT_EXT.get(fmt, "png")
    rev = rev or item.get("rev") or sha256_bytes(data)[:16]
    path = (
        ctx.home
        / SCENE_DIR
        / row["asset_id"]
        / row["set_id"]
        / f"{item['id']}-{rev[:8]}.{ext}"
    )
    now = now_iso()
    with ctx.db.tx() as conn:
        if path.exists():
            # 上次写好了文件、没来得及记库：照原样登记
            if store.oss_enabled(ctx):
                store.register(ctx, conn, path, data)
        else:
            store.save(ctx, conn, path, data)
        conn.execute(
            "INSERT INTO scene_images(set_id, image_id, role, idx, title, vibe, plan_id, shot_type, rev, path,"
            " sha256, width, height, bytes, format, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(set_id, image_id) DO UPDATE SET role = excluded.role, idx = excluded.idx,"
            " title = excluded.title, vibe = excluded.vibe, plan_id = excluded.plan_id,"
            " shot_type = excluded.shot_type, rev = excluded.rev, path = excluded.path,"
            " sha256 = excluded.sha256, width = excluded.width, height = excluded.height,"
            " bytes = excluded.bytes, format = excluded.format, updated_at = excluded.updated_at",
            (
                row["set_id"],
                item["id"],
                item.get("role") or "scene",
                int(item.get("index") or 0),
                item.get("title") or None,
                item.get("vibe") or None,
                item.get("planId") or None,
                item.get("shotType") or None,
                rev,
                store.rel(ctx.home, path),
                sha256_bytes(data),
                width,
                height,
                len(data),
                fmt,
                now,
                now,
            ),
        )


def _delivery_files(job: dict[str, Any]) -> list[dict[str, Any]]:
    """mooya 给的交付清单（出完才有，老版本 mooya 没有这个字段）。"""
    delivery = job.get("delivery")
    files = delivery.get("files") if isinstance(delivery, dict) else None
    return [
        f
        for f in files or []
        if isinstance(f, dict) and f.get("imageId") and f.get("fileName")
    ]


def _file_name(name: Any, seq: int) -> str:
    """mooya 给的包内文件名（001.png…）。只防路径和非法字符，不另起命名。"""
    clean = _UNSAFE_NAME.sub("-", Path(str(name or "")).name).strip("-.")
    return clean or f"{seq:03d}"


def _save_files(ctx: Ctx, set_id: str, files: list[dict[str, Any]]) -> None:
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM scene_files WHERE set_id = ?", (set_id,))
        for seq, f in enumerate(files, 1):
            conn.execute(
                "INSERT INTO scene_files(set_id, seq, image_id, file_name, role, title)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (
                    set_id,
                    seq,
                    f["imageId"],
                    _file_name(f["fileName"], seq),
                    f.get("role") or None,
                    f.get("title") or None,
                ),
            )


def delivery_files(ctx: Ctx, set_id: str) -> list[dict[str, Any]]:
    """一套套图的交付清单（按 mooya 交付包的顺序），带拉回来的文件路径；没有清单（旧套图）时是空的。"""
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT f.seq, f.image_id, f.file_name, f.role, f.title, i.path, i.rev"
            " FROM scene_files f LEFT JOIN scene_images i"
            " ON i.set_id = f.set_id AND i.image_id = f.image_id"
            " WHERE f.set_id = ? ORDER BY f.seq",
            (set_id,),
        )
    ]


def _collect(
    ctx: Ctx, api: mooya.Client, row: dict[str, Any], job: dict[str, Any]
) -> str:
    """mooya 那边结束了：拉回还没拉过（或在 mooya 里重生过）的图和交付清单、打压缩包、记最终状态。"""
    items = [i for i in job.get("images") or [] if isinstance(i, dict) and i.get("id")]
    files = _delivery_files(job)
    pulled = {i["id"] for i in items}
    # 清单里不在 images 的文件（尺码图、mooya 里换成的调色版…）也拉回来；role 记成 delivery，
    # 不算进场景图张数（清单里的 role 在 scene_files）
    extra = [
        {
            "id": f["imageId"],
            "role": "delivery",
            "index": seq,
            "title": f.get("title"),
            "rev": f.get("rev"),
        }
        for seq, f in enumerate(files, 1)
        if f["imageId"] not in pulled
    ]
    have = {
        r["image_id"]: r["rev"]
        for r in ctx.db.q(
            "SELECT image_id, rev FROM scene_images WHERE set_id = ?", (row["set_id"],)
        )
    }
    for item in items + extra:
        if item.get("rev") and have.get(item["id"]) == item["rev"]:
            continue
        # 存档的套图一律不带字标（字标租户的交付文件默认会贴）；上架用的图另按导出口径取（temu_assets）
        data, rev = api.image(row["mooya_job_id"], item["id"], variant=mooya.VARIANT_RAW)
        _save_image(ctx, row, item, data, rev)
    if files:
        _save_files(ctx, row["set_id"], files)
    scenes = sum(1 for i in items if (i.get("role") or "scene") == "scene")
    status = job.get("status")
    if status == "failed":
        final = "partial" if scenes else "failed"
    elif status in FINAL:
        final = status
    else:
        final = "done"
    if final in OK_FINAL and not scenes:
        final = "failed"
    export = _build_zip(ctx, row) if final in OK_FINAL else None
    error = None
    if final != "done":
        error = job.get("error") or (
            f"缺 {len(job['render'].get('missing') or [])} 张"
            if final == "partial" and isinstance(job.get("render"), dict)
            else None
        )
    _finish(ctx, row, final, error=error, job=job, export=export)
    return final


def _classify(ctx: Ctx, exc: mooya.MooyaError) -> str:
    if exc.service:
        _record_service_error(ctx, exc)
        return "service"
    return "transient" if exc.transient else "set"


def _pass(ctx: Ctx, pool: mooya.Pool, out: dict[str, Any]) -> str:
    """查一轮进度、交一轮新任务。返回 ok / transient（连不上，等会儿再试）/ service（要人去配）。
    每套套图用交它的那个客户端查（mooya 按租户找任务）；新任务用 mooya.submit_client 交。"""
    from . import stores  # 同 _submit，用时再导入

    for row in _rows(ctx, IN_FLIGHT):
        try:
            api = pool.get(client_of(ctx, row["set_id"]))
            job = api.status(row["mooya_job_id"])
        except mooya.MooyaError as exc:
            kind = _classify(ctx, exc)
            if kind != "set":
                out["errors"].append({"set_id": row["set_id"], "error": str(exc)[:300]})
                return kind
            _finish(
                ctx,
                row,
                "failed",
                error=f"查不到 mooya 任务 {row['mooya_job_id']}：{exc}",
            )
            out["finished"].append(row["set_id"])
            continue
        status = job.get("status")
        _note_baseline(ctx, row, job)
        if status in mooya.TERMINAL:
            try:
                _collect(ctx, api, row, job)
            except mooya.MooyaError as exc:
                kind = _classify(ctx, exc)
                if kind != "set":
                    out["errors"].append(
                        {"set_id": row["set_id"], "error": str(exc)[:300]}
                    )
                    return kind
                if _bump(ctx, row, exc):
                    out["finished"].append(row["set_id"])
                continue
            except (store.StorageError, OSError, ValueError) as exc:
                out["errors"].append({"set_id": row["set_id"], "error": str(exc)[:300]})
                if _bump(ctx, row, exc):
                    out["finished"].append(row["set_id"])
                continue
            out["finished"].append(row["set_id"])
        else:
            local = _LOCAL.get(status, row["status"])
            _update(ctx, row["set_id"], status=local, **_progress(job))
            if local != row["status"]:
                out.setdefault("changed", []).append(row["set_id"])
                trace.log(
                    ctx,
                    row["asset_id"],
                    "scenes",
                    frm=row["status"],
                    to=local,
                    set_id=row["set_id"],
                    source="mooya",
                    reason=job.get("currentStepLabel") if local in PARKED else None,
                )
            if local in PARKED and row["status"] not in PARKED:
                out.setdefault("parked", []).append(row["set_id"])
                _save_flags(ctx, row["set_id"], row["asset_id"], parked_at=now_iso())
                with ctx.db.tx() as conn:
                    log_event(
                        conn,
                        row["set_id"],
                        "scenes_parked",
                        {
                            "asset_id": row["asset_id"],
                            "status": local,
                            "step": job.get("currentStepLabel"),
                        },
                    )
            elif local not in PARKED and row["status"] in PARKED:
                _save_flags(ctx, row["set_id"], row["asset_id"], parked_at=None)
    busy = len(_rows(ctx, BUSY))
    capacity = max(0, int(ctx.setting("scenes.max_in_flight", 2)) - busy)
    queued = _rows(ctx, ("queued",))
    # 选款时定了店铺、还没领到货号的款先领号（mooya 连不上就留着下一轮），领不到的这一轮不交
    waiting = stores.waiting_codes(ctx, [r["asset_id"] for r in queued])
    if waiting:
        stores.allocate_codes(ctx, asset_ids=sorted(waiting))
        waiting = stores.waiting_codes(ctx, sorted(waiting))
    for row in [r for r in queued if r["asset_id"] not in waiting][:capacity]:
        try:
            api = pool.get(mooya.submit_client(ctx))
            _submit(ctx, api, row)
        except mooya.MooyaError as exc:
            kind = _classify(ctx, exc)
            if kind != "set":
                out["errors"].append({"set_id": row["set_id"], "error": str(exc)[:300]})
                return kind
            # 这一单本身的问题（图坏了、预设不对）：重试也没用
            _finish(ctx, row, "failed", error=str(exc)[:500])
            out["finished"].append(row["set_id"])
            continue
        except (store.StorageError, OSError) as exc:
            out["errors"].append({"set_id": row["set_id"], "error": str(exc)[:300]})
            if _bump(ctx, row, exc):
                out["finished"].append(row["set_id"])
            continue
        out["submitted"].append(row["set_id"])
    _clear_service_error(ctx)
    return "ok"


# ---------------------------------------------------------------- 自愈（定了店铺的款）


def heal_enabled(ctx: Ctx) -> bool:
    return enabled(ctx) and bool(ctx.setting("scenes.auto_heal", True))


def _heal_resubmits(ctx: Ctx, asset_id: str) -> int:
    return int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM scene_sets WHERE asset_id = ? AND requested_by = ?",
            (asset_id, HEAL_BY),
        )
        or 0
    )


def _heal_plan(ctx: Ctx, row: dict[str, Any]) -> str | None:
    """这一款最近一套要怎么自愈：regenerate_missing / resubmit / resume；不用（或次数用完了）为 None。"""
    from . import temu_assets  # 上架素材依赖本模块，用时再导入

    heal = _flags(ctx, row["set_id"]).get("heal") or {}
    retry_limit = int(ctx.setting("scenes.auto_retry_failed", 1) or 0)
    can_resubmit = _heal_resubmits(ctx, row["asset_id"]) < retry_limit
    if row["status"] == "partial":
        if not temu_assets.slot_check(ctx, row["set_id"]):
            return None
        if (
            int(heal.get(mooya.ACTION_REGENERATE_MISSING) or 0) < HEAL_MISSING_MAX
            and not heal.get("unsupported_regenerate")
        ):
            return mooya.ACTION_REGENERATE_MISSING
        return "resubmit" if can_resubmit else None
    if row["status"] == "failed":
        return "resubmit" if can_resubmit else None
    if row["status"] == "paused":
        parked = parse_iso(_flags(ctx, row["set_id"]).get("parked_at") or row["updated_at"])
        minutes = float(ctx.setting("scenes.auto_resume_minutes", 120) or 0)
        if (
            minutes > 0
            and parked
            and utcnow() - parked >= dt.timedelta(minutes=minutes)
            and int(heal.get(mooya.ACTION_RESUME) or 0) < HEAL_RESUME_MAX
            and not heal.get("unsupported_resume")
        ):
            return mooya.ACTION_RESUME
    return None


def heal_targets(ctx: Ctx) -> list[tuple[dict[str, Any], str]]:
    """要自愈的套图：定了店铺、还没进采集箱的款，最近一套缺图 / 没出成 / 暂停太久。[(套图那一行, 怎么做)]。"""
    from . import stores  # 店铺依赖本模块，用时再导入

    if not heal_enabled(ctx) or not stores.enabled(ctx):
        return []
    assets = sorted({r["asset_id"] for r in stores.rows(ctx) if r["state"] in ("chosen", "queued")})
    if not assets:
        return []
    out = []
    for row in latest_sets(ctx, assets).values():
        plan = _heal_plan(ctx, row)
        if plan:
            out.append((row, plan))
    return out


def _heal_resubmit(ctx: Ctx, row: dict[str, Any], heal: dict[str, Any], out: dict[str, Any]) -> None:
    try:
        got = enqueue(ctx, by=HEAL_BY, asset_ids=[row["asset_id"]], force=True)
    except StageError as exc:
        out["errors"].append({"set_id": row["set_id"], "error": str(exc)[:300]})
        return
    if got["queued"]:
        heal.update(resubmitted=got["queued"][0]["set_id"], last_at=now_iso())
        _save_flags(ctx, row["set_id"], row["asset_id"], heal=heal)
        out.setdefault("healed", []).append({"set_id": row["set_id"], "action": "resubmit"})


def _heal(ctx: Ctx, pool: mooya.Pool, out: dict[str, Any]) -> str:
    """做一轮自愈；mooya 连不上 / 令牌配置错时停下，返回 transient / service，否则 ok。"""
    for row, plan in heal_targets(ctx):
        heal = dict(_flags(ctx, row["set_id"]).get("heal") or {})
        if plan == "resubmit":
            _heal_resubmit(ctx, row, heal, out)
            continue
        if not row.get("mooya_job_id"):
            continue
        try:
            pool.get(client_of(ctx, row["set_id"])).action(row["mooya_job_id"], plan)
        except mooya.MooyaError as exc:
            if mooya.unsupported(exc):
                heal["unsupported_regenerate" if plan == mooya.ACTION_REGENERATE_MISSING else "unsupported_resume"] = True
                heal["last_error"] = str(exc)[:300]
                _save_flags(ctx, row["set_id"], row["asset_id"], heal=heal)
                # mooya 还没有动作端点：缺图的这一轮就改成整套重出
                if _heal_plan(ctx, row) == "resubmit":
                    _heal_resubmit(ctx, row, heal, out)
                continue
            kind = _classify(ctx, exc)
            if kind != "set":
                out["errors"].append({"set_id": row["set_id"], "error": str(exc)[:300]})
                return kind
            heal[plan] = int(heal.get(plan) or 0) + 1
            heal["last_error"] = str(exc)[:300]
            _save_flags(ctx, row["set_id"], row["asset_id"], heal=heal)
            continue
        heal[plan] = int(heal.get(plan) or 0) + 1
        heal.update(last_at=now_iso(), last_error=None)
        _save_flags(ctx, row["set_id"], row["asset_id"], heal=heal, parked_at=None)
        # mooya 那边又在跑了：交给 _pass 接着查，出完重新拉图（只拉变了的）、重打压缩包
        _update(ctx, row["set_id"], status="running", table_state=None, table_attempts=0, table_error=None)
        trace.log(
            ctx,
            row["asset_id"],
            "scenes",
            frm=row["status"],
            to="running",
            set_id=row["set_id"],
            actor=HEAL_BY,
            source="heal",
            reason="缺图：让 mooya 补出缺的图" if plan == mooya.ACTION_REGENERATE_MISSING else "暂停太久：让 mooya 接着跑",
        )
        out.setdefault("healed", []).append({"set_id": row["set_id"], "action": plan})
    return "ok"


def _progress_text(ctx: Ctx) -> str:
    counts = Counter(r["status"] for r in _rows(ctx, ACTIVE))
    parts = [
        f"{STATUS_LABELS[s]} {counts[s]}"
        for s in ("running", "submitted", "queued", "waiting_human", "paused")
        if counts.get(s)
    ]
    return "套图：" + " · ".join(parts) if parts else "套图：收尾"


def sync(ctx: Ctx, *, max_minutes: float | None = None) -> dict[str, Any]:
    """推进全部套图：交新任务、查进度、拉图、打包；还有在出的就每隔 scenes.poll_seconds 再查，直到出完或到时间。"""
    if not enabled(ctx):
        return {"skipped": True, "reason": "scenes.enabled 为 false"}
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        return {"skipped": True, "reason": "另一个套图进程正在运行"}
    out: dict[str, Any] = {"submitted": [], "finished": [], "errors": [], "passes": 0}
    limit = float(
        max_minutes
        if max_minutes is not None
        else ctx.setting("scenes.sync_max_minutes", 40)
    )
    poll = float(ctx.setting("scenes.poll_seconds", 30))
    deadline = time.monotonic() + limit * 60
    transient = 0
    flushed = 0
    relayed: set[str] = set()
    try:
        pool = mooya.Pool(ctx)
        try:
            pool.get(mooya.submit_client(ctx))
        except mooya.MooyaError as exc:
            pool.close()
            _record_service_error(ctx, exc)
            out["stopped"] = str(exc)
            return out
        with pool:
            while True:
                lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
                out["passes"] += 1
                result = _pass(ctx, pool, out)
                if result == "ok":
                    result = _heal(ctx, pool, out)
                flushed = _flush_parked(ctx, out, flushed)
                _relay(ctx, out, relayed)
                if result == "service":
                    out["stopped"] = "mooya 的令牌或租户配置有问题，修好后巡检会接着出"
                    break
                transient = transient + 1 if result == "transient" else 0
                if transient >= MAX_TRANSIENT_PASSES:
                    out["stopped"] = "连续几轮连不上 mooya，等下次巡检再试"
                    break
                # 只剩 mooya 那边等人的、或排着的都在等货号：不在这里守着，巡检隔一阵会再查
                if not _has_work(ctx, ("submitted", "running")) and not _ready_queued(ctx):
                    break
                if time.monotonic() + poll > deadline:
                    out["stopped"] = "到了单次最长时间，没出完的由下次巡检接着查"
                    break
                bg.step(ctx, _progress_text(ctx))
                _sleep(poll)
    finally:
        lease.drop(ctx, LEASE_KEY)
        _flush_parked(ctx, out, flushed)
        out["packages"] = _build_packages(ctx)
        shelved = _sweep_shelf(ctx)
        if shelved.get("archived") or shelved.get("reopened") or shelved.get("error"):
            out["shelf"] = {
                k: shelved[k]
                for k in ("archived", "reopened", "error")
                if shelved.get(k)
            }
        out["notified"] = _notify_finished(
            ctx, archived={a["seed_id"] for a in shelved.get("archived") or []}
        )
        feishu_table.kick(ctx)
        store.kick(ctx)
        listing = _after_sets(ctx, out)
        if listing:
            out["listing"] = listing
    out["active"] = len(_rows(ctx, ACTIVE))
    return out


def _relay(ctx: Ctx, out: dict[str, Any], relayed: set[str]) -> None:
    """这一轮新动过的套图（交出、开跑、停下、出完、自愈）：马上重算当前环节、写「选中的款」，出完的拉起上架、
    拉起表格同步写套图状态和表顶套图行，不等这个进程结束（它最长要跑 sync_max_minutes）。"""
    finished = set(out.get("finished", []))
    ids = {
        *finished,
        *out.get("submitted", []),
        *out.get("changed", []),
        *(h["set_id"] for h in out.get("healed", [])),
    } - relayed
    if not ids:
        return
    relayed |= ids
    _after_sets(ctx, {"finished": sorted(ids & finished), "submitted": sorted(ids - finished)})
    try:
        feishu_table.kick(ctx)
    except Exception:  # noqa: BLE001 - 拉不起表格同步不挡套图，收尾时还会再拉
        pass


def _after_sets(ctx: Ctx, out: dict[str, Any]) -> dict[str, Any]:
    """这一轮动过的套图里定了店铺的款：重算当前环节（变了写进「选中的款」），出完了马上拉起上架准备。出了意外不挡收尾。"""
    from . import stores, temu  # 上架模块依赖本模块，放到用时再导入

    ids = {*out.get("finished", []), *out.get("submitted", []), *(h["set_id"] for h in out.get("healed", []))}
    if not ids or not stores.enabled(ctx):
        return {}
    try:
        rows = ctx.db.q(
            f"SELECT DISTINCT asset_id FROM scene_sets WHERE set_id IN ({_marks(sorted(ids))})",
            tuple(sorted(ids)),
        )
        assets = sorted({r["asset_id"] for r in rows} & {r["asset_id"] for r in stores.rows(ctx)})
        if not assets:
            return {}
        result: dict[str, Any] = {"pipeline": trace.refresh(ctx, assets)}
        task = temu.kick(ctx) if out.get("finished") else None
        if task:
            result["temu_sync"] = task["task_id"]
        if result["pipeline"].get("changed"):
            result["table"] = temu.write_now(ctx, assets)
        return result
    except Exception as exc:  # noqa: BLE001 - 收尾时的上架接力出错不挡套图本身
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _build_packages(ctx: Ctx) -> dict[str, Any]:
    """出完一轮后先打交付包，「套图出完」卡片就能带上链接；打包出了意外也不挡后面发卡片。"""
    from . import packages  # 交付包依赖本模块，放到用时再导入

    try:
        return packages.build_due(ctx)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _sweep_shelf(ctx: Ctx) -> dict[str, Any]:
    """套图出完后扫一遍任务归档（shelf.auto 开着时选款完成、套图都出完的任务收起来，卡片上写明）；出了意外不挡发卡片。"""
    from . import shelf  # 归档依赖任务视图（任务视图依赖本模块），放到用时再导入

    try:
        return shelf.sweep(ctx, source="scenes")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


# ---------------------------------------------------------------- 压缩包


def _safe(text: Any) -> str:
    return _UNSAFE_NAME.sub("-", str(text or "")).strip("-")[:40]


def zip_entries(ctx: Ctx, row: dict[str, Any]) -> list[tuple[str, str]]:
    """一套套图要打包的文件：(存储路径, 包里的文件名，不带后缀)，交付包也用它。

    设计图 + mooya 交付清单里的文件（顺序、文件名照 mooya 交付包）；没有清单的旧套图是设计图 + 白底图 + 场景图。
    """
    asset = ctx.db.q1("SELECT path FROM assets WHERE asset_id = ?", (row["asset_id"],))
    asset_id = row["asset_id"]
    entries: list[tuple[str, str]] = []
    if asset:
        entries.append((asset["path"], f"{asset_label_of(ctx, asset_id)}_设计图"))
    files = delivery_files(ctx, row["set_id"])
    if files:
        entries += [(f["path"], Path(f["file_name"]).stem) for f in files if f["path"]]
        return entries
    pictures = [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM scene_images WHERE set_id = ?"
            " ORDER BY CASE role WHEN 'baseline' THEN 0 ELSE 1 END, idx, image_id",
            (row["set_id"],),
        )
    ]
    for pic in pictures:
        if pic["role"] == "baseline":
            entries.append((pic["path"], f"{asset_id}_白底图"))
        else:
            title = _safe(pic["title"])
            entries.append(
                (
                    pic["path"],
                    f"{asset_id}_场景{pic['idx']:02d}" + (f"_{title}" if title else ""),
                )
            )
    return entries


def _build_zip(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    """这一款的套图压缩包（文件见 `zip_entries`）；OSS 存储时上传并签下载链接，旧包删掉。"""
    asset_id = row["asset_id"]
    name = f"{asset_id}-scenes-{row['set_id']}.zip"
    stored = f"{EXPORT_DIR}/{asset_id}/{name}"
    local = store.resolve(ctx.home, stored)
    local.parent.mkdir(parents=True, exist_ok=True)
    entries = zip_entries(ctx, row)
    missing: list[str] = []
    count = 0
    with zipfile.ZipFile(local, "w", compression=zipfile.ZIP_STORED) as zf:
        for path, arcname in entries:
            try:
                src = store.ensure_local(ctx, path)
            except store.StorageError:
                missing.append(path)
                continue
            if not src.is_file():
                missing.append(path)
                continue
            zf.write(src, arcname=arcname + src.suffix)
            count += 1
    out: dict[str, Any] = {
        "name": name,
        "count": count,
        "bytes": local.stat().st_size,
        "missing": missing,
        "key": None,
        "path": stored,
        "url": None,
        "expires_at": None,
    }
    if store.oss_enabled(ctx):
        client = store.client(ctx)
        key = store.object_key(ctx, stored)
        client.put(
            key,
            local.read_bytes(),
            content_type="application/zip",
            forbid_overwrite=False,
        )
        days = int(ctx.setting("table.link_days", 90))
        out.update(
            key=key,
            url=client.sign_url(key, days * 86400, filename=name),
            expires_at=iso(utcnow() + dt.timedelta(days=days)),
            path=None,
        )
        local.unlink()
        if row.get("export_key") and row["export_key"] != key:
            try:
                client.delete(row["export_key"])
            except _OSS_FAILURES:
                pass
    return out


# ---------------------------------------------------------------- 卡片


def _set_line(row: dict[str, Any], scenes: int, label: str) -> str:
    """套图卡片上一款一行，label 是这款图的名字「IMG…_短语」。"""
    if row["status"] == "done":
        return f"- {label}：{scenes} 张场景图"
    if row["status"] == "partial":
        expected = row["expected"] or scenes
        return f"- {label}：缺图（{scenes}/{expected}）" + (
            f"，{row['last_error']}" if row["last_error"] else ""
        )
    return f"- {label}：{STATUS_LABELS[row['status']]}" + (
        f"：{str(row['last_error'])[:80]}" if row["last_error"] else ""
    )


def _notify_finished(
    ctx: Ctx, archived: set[str] | None = None
) -> list[dict[str, Any]]:
    """把出完、还没通知的套图按任务合并发卡片（发不出也记已通知，免得反复刷）。
    archived 是这一轮刚自动归档的任务：卡片上写明已归档，带「调出」按钮。
    精简通知（notify.style: brief）时只给没出成的发红卡，出完的进待办「待下载」和状态总结，照样记已通知。"""
    from . import digest, notify  # 通知依赖视图模块，放到用时再导入

    brief = digest.enabled(ctx)
    rows = [
        dict(r)
        for r in ctx.db.q(
            f"SELECT * FROM scene_sets WHERE status IN ({_marks(FINAL)}) AND notified_at IS NULL"
            " ORDER BY seed_id, set_id",
            FINAL,
        )
    ]
    if not rows:
        return []
    by_seed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_seed[row["seed_id"]].append(row)
    sent = []
    for seed_id, sets in by_seed.items():
        card = (
            digest.failed_sets_card(ctx, seed_id, sets)
            if brief
            else scenes_card(
                ctx, seed_id, sets, archived=seed_id in (archived or set())
            )
        )
        if card is None:
            result = {"sent": False, "reason": digest.QUIET}
        else:
            fallback = (
                card["header"]["title"]["content"]
                + "\n"
                + card["elements"][0]["content"]
            )
            try:
                result = notify.send_pod_card(ctx, card, fallback)
            except notify.NotifyError as exc:
                result = {"sent": False, "error": str(exc)}
        now = now_iso()
        with ctx.db.tx() as conn:
            for row in sets:
                conn.execute(
                    "UPDATE scene_sets SET notified_at = ? WHERE set_id = ?",
                    (now, row["set_id"]),
                )
        sent.append({"seed_id": seed_id, "sets": [r["set_id"] for r in sets], **result})
    return sent


def parked_card(ctx: Ctx, seed_id: str, sets: list[dict[str, Any]]) -> dict[str, Any]:
    """一个任务刚停在 mooya 等人工（或被暂停）的款：哪个 mooya 任务、卡在哪一步、去哪里处理。"""
    from . import notify, ui

    lines = [
        f"**{len(sets)}** 款停在 mooya 等人工，处理完这边会自动接着拉图；"
        "停着的不占同时出图的名额，后面的款照常出",
    ]
    for row in sets:
        step = row["step_label"] or STATUS_LABELS[row["status"]]
        lines.append(
            f"- {labels.asset(ctx, row['asset_id'])}：{step}（mooya 任务 #{row['mooya_job_id']}，"
            f"名称以「{row['seed_id']} · {row['asset_id']}」结尾）"
        )
    lines.append(
        "去处理：登录 mooya 网页、切到交这套的租户（10-29 起新交的在字标租户 ai_temu_deerflow，之前的在 platform_internal），"
        "打开「图案管线」找到上面的任务，白底图没过的选一张继续或重新生成，已暂停的点继续"
    )
    return notify.pod_card(
        f"套图等人工｜{task_label(ctx, seed_id)}",
        lines,
        level="attention",
        buttons=[
            ("任务列表", {"action": ui.ACTION_TASKS}, "default"),
            ("打开工作台", {"action": notify.CARD_ACTION_DASHBOARD}, "default"),
        ],
    )


def _notify_parked(ctx: Ctx, set_ids: list[str]) -> list[dict[str, Any]]:
    """刚停下等人的款按任务合并发卡片（发不出也不重试，任务列表的「现在」一行照样写着）。
    精简通知时不单独发：每小时的待办提醒里有「套图等人工」。"""
    from . import digest, notify

    ids = list(dict.fromkeys(set_ids))
    if not ids or digest.enabled(ctx):
        return []
    rows = [
        dict(r)
        for r in ctx.db.q(
            f"SELECT * FROM scene_sets WHERE set_id IN ({_marks(ids)})"
            f" AND status IN ({_marks(PARKED)}) ORDER BY seed_id, set_id",
            (*ids, *PARKED),
        )
    ]
    by_seed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_seed[row["seed_id"]].append(row)
    sent = []
    for seed_id, sets in by_seed.items():
        card = parked_card(ctx, seed_id, sets)
        fallback = (
            card["header"]["title"]["content"] + "\n" + card["elements"][0]["content"]
        )
        try:
            result = notify.send_pod_card(ctx, card, fallback)
        except notify.NotifyError as exc:
            result = {"sent": False, "error": str(exc)}
        sent.append({"seed_id": seed_id, "sets": [r["set_id"] for r in sets], **result})
    return sent


def _flush_parked(ctx: Ctx, out: dict[str, Any], flushed: int) -> int:
    """这一轮刚停下的款马上发卡片，不等后台进程退出（别的款还在出时进程要跑满 sync_max_minutes）。
    返回已经发到 out["parked"] 的第几条。"""
    parked = out.get("parked") or []
    if len(parked) > flushed:
        out.setdefault("parked_notified", []).extend(
            _notify_parked(ctx, parked[flushed:])
        )
    return len(parked)


def scenes_card(
    ctx: Ctx, seed_id: str, sets: list[dict[str, Any]], *, archived: bool = False
) -> dict[str, Any]:
    """一个任务这一轮出完的套图：几款出完、共几张，没出成的写原因，并排放前几款的第一张场景图；
    所在批次的交付包打好了就带上下载链接。archived：这个任务刚自动归档，写明在哪找、怎么调出。"""
    from . import notify, packages, ui

    counts = {
        r["set_id"]: int(
            ctx.db.scalar(
                "SELECT COUNT(*) FROM scene_images WHERE set_id = ? AND role = 'scene'",
                (r["set_id"],),
            )
            or 0
        )
        for r in sets
    }
    ok = [r for r in sets if r["status"] in OK_FINAL]
    bad = [r for r in sets if r["status"] not in OK_FINAL]
    # 这个任务还在出的款（一个后台进程只跑 sync_max_minutes，出得多的任务会分几次发卡片）
    remaining = Counter(
        r["status"]
        for r in ctx.db.q(
            f"SELECT status FROM scene_sets WHERE seed_id = ? AND status IN ({_marks(ACTIVE)})",
            (seed_id, *ACTIVE),
        )
    )
    active = sum(remaining.values())
    parked = sum(remaining[s] for s in PARKED)
    names = labels.assets(ctx, [r["asset_id"] for r in sets])
    lines = [
        f"**{len(ok)}** 款出完 · 共 {sum(counts[r['set_id']] for r in ok)} 张场景图"
        + (f" · {len(bad)} 款没出成" if bad else "")
        + "（mooya 图案管线）",
        *(
            _set_line(r, counts[r["set_id"]], names[r["asset_id"]]["label"])
            for r in sets
        ),
    ]
    if active:
        lines.append(
            f"还有 {active} 款在出"
            + (f"（其中 {parked} 款等人工）" if parked else "")
            + "，出完再发卡片"
        )
    bundles = packages.ready_for_sets(ctx, seed_id, [r["set_id"] for r in ok])
    for view in bundles:
        lines.append(
            f"{view['batch_id']} 交付包：{view['items']} 款 · {packages.size_text(view['bytes'])}，"
            "每款一个文件夹（设计图 + 按 mooya 交付包编号的场景图）"
        )
    if any(view["url"] for view in bundles):
        lines.append(
            "任务列表和候选表「套图交付包」列都有交付包链接；"
            "每款的缩略图、下载套图在选款表「选中的款」里（同步要一两分钟）"
        )
    elif ok:
        lines.append("缩略图、下载套图在选款表「选中的款」里（同步要一两分钟）")
    unmarked = [v for v in bundles if not v["downloaded"]]
    if unmarked and not archived:
        buttons_text = "、".join(f"「{v['batch_id']} 已下载」" for v in unmarked[:2])
        lines.append(
            f"下载后点{buttons_text}（或在候选表最上面的交付包那一行打勾），任务才会归档"
        )
    if bad:
        lines.append(
            "要重出：任务列表点「出套图」，或在 POD 对话说「IMGxxx 重新出套图」"
        )
    if archived:
        lines.append(
            "选款完成、选中的款套图都出完了：这个任务已归档，从任务列表收起，候选表挪进「已归档」文件夹。"
            "交付包、选中的款都还在；任务列表点「历史任务」能找到，要接着裂变点「调出」"
        )
    pictures = []
    for row in ok[:3]:
        first = ctx.db.q1(
            "SELECT path FROM scene_images WHERE set_id = ? AND role = 'scene' ORDER BY idx LIMIT 1",
            (row["set_id"],),
        )
        if not first:
            continue
        try:
            thumb = images.thumbnail_jpeg(
                store.ensure_local(ctx, first["path"]).read_bytes()
            )
        except (OSError, store.StorageError, ValueError):
            continue
        key = notify.upload_image(ctx, thumb, f"{row['asset_id']}.jpg")
        if key:
            pictures.append((key, names[row["asset_id"]]["label"]))
    links: list[tuple[str, str, str]] = []
    table_url = feishu_table.table_url(ctx)
    if table_url:
        links.append(("打开选款表", table_url, "primary"))
    links += [
        (f"下载 {view['batch_id']} 交付包", view["url"], "default")
        for view in bundles[:2]
        if view["url"]
    ]
    if len(ok) == 1 and ok[0].get("export_url"):
        links.append(("下载套图", ok[0]["export_url"], "default"))
    task = task_label(ctx, seed_id)
    if bad:
        title = f"套图有没出成的｜{task}"
    elif active:
        title = f"套图出完 {len(ok)} 款｜{task}"
    elif archived:
        title = f"套图出完，任务已归档｜{task}"
    else:
        title = f"套图出完｜{task}"
    buttons: list[tuple[str, dict[str, Any], str]] = [
        (
            f"{v['batch_id']} 已下载",
            {"action": ui.ACTION_DOWNLOADED, "seed": seed_id, "batch": v["batch_id"]},
            "default",
        )
        for v in (unmarked[:2] if not archived else [])
    ]
    buttons += [
        ("任务列表", {"action": ui.ACTION_TASKS}, "default"),
        ("打开工作台", {"action": notify.CARD_ACTION_DASHBOARD}, "default"),
    ]
    if archived:
        buttons = [
            ("调出", {"action": ui.ACTION_RESTORE, "seed": seed_id}, "default"),
            ("历史任务", {"action": ui.ACTION_SHELF}, "default"),
            *buttons,
        ]
    return notify.pod_card(
        title,
        lines,
        level="normal" if not bad else "attention",
        buttons=buttons,
        links=links or None,
        images=pictures or None,
    )


# ---------------------------------------------------------------- 手动操作


def show(ctx: Ctx, set_id: str) -> dict[str, Any]:
    row = get_set(ctx, set_id)
    pictures = [
        dict(r)
        for r in ctx.db.q(
            "SELECT image_id, role, idx, title, vibe, plan_id, shot_type, rev, path, width, height, bytes"
            " FROM scene_images WHERE set_id = ?"
            " ORDER BY CASE role WHEN 'baseline' THEN 1 ELSE 0 END, idx",
            (set_id,),
        )
    ]
    return {
        **row,
        "label": labels.asset(ctx, row["asset_id"]),
        "task": task_label(ctx, row["seed_id"]),
        "status_label": STATUS_LABELS[row["status"]],
        # 交这套的 mooya 客户端（legacy = platform_internal 租户；其余见 mooya.clients），去 mooya 页面处理等人工时切到它的租户
        "mooya_client": client_of(ctx, set_id),
        "images": pictures,
        # 压缩包里的文件名（mooya 交付包的编号）；旧套图没有
        "files": [
            {k: f[k] for k in ("seq", "file_name", "image_id", "role", "title")}
            for f in delivery_files(ctx, set_id)
        ],
    }


def status(ctx: Ctx, seed_id: str | None = None) -> dict[str, Any]:
    """每款最近一次套图的状态（可只看一个任务）、在出的数量、各批次的交付包、mooya 配置错误。"""
    from . import packages  # 交付包依赖本模块，放到用时再导入

    latest = latest_sets(ctx)
    items = sorted(
        (r for r in latest.values() if not seed_id or r["seed_id"] == seed_id),
        key=lambda r: (r["seed_id"], r["asset_id"]),
    )
    per_seed = seed_counts(ctx)
    names = labels.assets(ctx, [r["asset_id"] for r in items])
    return {
        "enabled": enabled(ctx),
        "base_url": ctx.setting("mooya.base_url"),
        "preset": ctx.setting("mooya.preset"),
        "counts": dict(Counter(r["status"] for r in items)),
        "seeds": {k: v for k, v in per_seed.items() if not seed_id or k == seed_id},
        "sets": [
            {
                "set_id": r["set_id"],
                "asset_id": r["asset_id"],
                "label": names[r["asset_id"]]["label"],
                "seed_id": r["seed_id"],
                "task": names[r["asset_id"]]["task"],
                "status": r["status"],
                "status_label": STATUS_LABELS[r["status"]],
                "step": r["step_label"],
                "mooya_job_id": r["mooya_job_id"],
                "progress": f"{r['actual'] or 0}/{r['expected'] or 0}"
                if r["expected"]
                else None,
                "error": r["last_error"],
                "download": r["export_url"] or r["export_path"],
            }
            for r in items
        ],
        "packages": packages.overview(ctx, seed_id)["packages"],
        "service_error": _service_error(ctx).get("error"),
        "syncing": lease.active(ctx, LEASE_KEY),
    }


def cancel(ctx: Ctx, set_id: str, *, by: str) -> dict[str, Any]:
    row = get_set(ctx, set_id)
    if row["status"] in FINAL:
        raise StageError(f"{set_id} 已经{STATUS_LABELS[row['status']]}，不用停")
    if row["mooya_job_id"] and row["status"] in IN_FLIGHT:
        try:
            with mooya.Client(ctx, client=client_of(ctx, set_id)) as api:
                api.cancel(row["mooya_job_id"])
        except mooya.MooyaError as exc:
            raise StageError(f"mooya 那边没停掉：{exc}") from exc
    _finish(ctx, row, "cancelled", error=f"{by} 停掉了")
    # 手动停的不发卡片
    _update(ctx, set_id, notified_at=now_iso())
    return show(ctx, set_id)


def _refresh_one(ctx: Ctx, api: mooya.Client, row: dict[str, Any]) -> bool:
    """重拉一套：mooya 那边已结束就拉变了的图和交付清单、重打压缩包，返回 True；又在跑了交给后台，返回 False。"""
    job = api.status(row["mooya_job_id"])
    if job.get("status") not in mooya.TERMINAL:
        # mooya 里又在跑了（例如补生成缺失）：交给后台接着查
        _update(
            ctx,
            row["set_id"],
            status=_LOCAL.get(job.get("status"), "running"),
            **_progress(job),
        )
        return False
    _collect(ctx, api, row, job)
    _update(ctx, row["set_id"], table_state=None, table_attempts=0, table_error=None)
    return True


def refresh(ctx: Ctx, set_id: str) -> dict[str, Any]:
    """在 mooya 里重生过单张图（或调过交付包）后，把这一款的图重新拉一遍（只拉变了的）、重打压缩包、刷新表格那一行。"""
    row = get_set(ctx, set_id)
    if row["status"] not in FINAL or not row["mooya_job_id"]:
        raise StageError(
            f"{set_id} 还在出（{STATUS_LABELS[row['status']]}），出完会自动拉图"
        )
    try:
        with mooya.Client(ctx, client=client_of(ctx, set_id)) as api:
            collected = _refresh_one(ctx, api, row)
    except mooya.MooyaError as exc:
        raise StageError(f"没拉成：{exc}") from exc
    if collected:
        feishu_table.kick(ctx)
        store.kick(ctx)
    # 图变了，这一款所在批次的交付包要重打；又在跑了的交给后台接着查
    kick(ctx)
    return show(ctx, set_id)


def refresh_all(ctx: Ctx, *, seed_id: str | None = None) -> dict[str, Any]:
    """出完的每款最近一套都重拉一遍（只拉变了的图和交付清单）、重打压缩包，不重新出图。
    mooya 改了交付包的命名或顺序后，用它给旧套图换上新的文件名。"""
    rows = sorted(
        (
            r
            for r in latest_sets(ctx).values()
            if r["status"] in OK_FINAL
            and r["mooya_job_id"]
            and (not seed_id or r["seed_id"] == seed_id)
        ),
        key=set_order,
    )
    out: dict[str, Any] = {"refreshed": [], "running": [], "failed": []}
    with mooya.Pool(ctx) as pool:
        for n, row in enumerate(rows, 1):
            bg.step(ctx, f"重拉套图 {n}/{len(rows)}")
            try:
                collected = _refresh_one(
                    ctx, pool.get(client_of(ctx, row["set_id"])), row
                )
            except (mooya.MooyaError, store.StorageError, OSError, ValueError) as exc:
                out["failed"].append(
                    {
                        "set_id": row["set_id"],
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue
            out["refreshed" if collected else "running"].append(row["set_id"])
    feishu_table.kick(ctx)
    store.kick(ctx)
    kick(ctx)
    return out


# ---------------------------------------------------------------- 选款表（选中的款）的套图列


def _table_key(row: dict[str, Any], scenes: int) -> str:
    """写进表里的样子：状态、张数、压缩包链接变了才再写。"""
    return f"{row['status']}|{scenes}|{row['export_url'] or ''}"


def _table_candidates(ctx: Ctx) -> list[dict[str, Any]]:
    """该写套图列的行：选款表里有这一款、最近一次套图的样子和表里不一样（或压缩包链接该续签了），失败没到上限。"""
    rows = {
        r["asset_id"]: r["record_id"]
        for r in ctx.db.q(
            "SELECT asset_id, record_id FROM table_rows WHERE record_id IS NOT NULL"
        )
        # 按份同步「选中的款」时只挑归到这一份的款
        if table_registry.mine(r["asset_id"])
    }
    if not rows:
        return []
    refresh_before = feishu_table.refresh_before(ctx)
    out = []
    for asset_id, row in latest_sets(ctx, list(rows)).items():
        if int(row["table_attempts"] or 0) >= feishu_table.MAX_ATTEMPTS:
            continue
        scenes = int(
            ctx.db.scalar(
                "SELECT COUNT(*) FROM scene_images WHERE set_id = ? AND role = 'scene'",
                (row["set_id"],),
            )
            or 0
        )
        renew = bool(
            row["export_key"]
            and row["export_expires_at"]
            and row["export_expires_at"] < refresh_before
        )
        if renew or row["table_state"] != _table_key(row, scenes):
            out.append(
                {**row, "record_id": rows[asset_id], "scenes": scenes, "renew": renew}
            )
    return out


def table_has_work(ctx: Ctx) -> bool:
    return enabled(ctx) and bool(_table_candidates(ctx))


def _renew_link(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    days = int(ctx.setting("table.link_days", 90))
    url = store.client(ctx).sign_url(
        row["export_key"], days * 86400, filename=Path(row["export_key"]).name
    )
    expires = iso(utcnow() + dt.timedelta(days=days))
    _update(ctx, row["set_id"], export_url=url, export_expires_at=expires)
    return {**row, "export_url": url, "export_expires_at": expires}


def _table_fields(ctx: Ctx, row: dict[str, Any]) -> dict[str, Any]:
    """这一行要写的套图列：套图状态、场景图张数、下载套图（2026-10-29 起不放场景图缩略图，也不再生成）。
    表里原来是出完的、这次没出完（重出排队中）时清掉张数和链接。"""
    previous = (row["table_state"] or "").split("|")
    had = previous[0] in OK_FINAL
    fields: dict[str, Any] = {"套图状态": STATUS_LABELS[row["status"]]}
    if row["status"] not in OK_FINAL:
        if had:
            fields.update({"场景图张数": 0, "下载套图": None})
        return fields
    if row["export_url"]:
        until = parse_iso(row["export_expires_at"]).astimezone(
            ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
        )
        fields["下载套图"] = {
            "link": row["export_url"],
            "text": f"下载套图（{until:%Y-%m-%d} 前有效）",
        }
    fields["场景图张数"] = row["scenes"]
    return fields


def sync_table(
    ctx: Ctx, api: feishu.Client, info: dict[str, Any], errors: list[dict[str, str]]
) -> int:
    """把每款最近一次套图写进「选中的款」那一行（调用方持有选款表的同步租约）。返回写了几行。"""
    if not enabled(ctx):
        return 0
    rows = _table_candidates(ctx)
    if not rows:
        return 0
    feishu_table.ensure_fields(ctx, api, info, feishu_table.SCENE_FIELDS)
    path = f"/bitable/v1/apps/{info['app_token']}/tables/{info['table_id']}/records"
    updated = 0
    for row in rows:
        try:
            if row["renew"] and store.oss_enabled(ctx):
                row = _renew_link(ctx, row)
            api.call(
                "PUT",
                f"{path}/{row['record_id']}",
                params={"user_id_type": "open_id"},
                json={"fields": _table_fields(ctx, row)},
            )
            _update(
                ctx,
                row["set_id"],
                table_state=_table_key(row, row["scenes"]),
                table_attempts=0,
                table_error=None,
            )
            updated += 1
        except _FAILURES as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            _update(
                ctx,
                row["set_id"],
                table_attempts=int(row["table_attempts"] or 0) + 1,
                table_error=error,
            )
            errors.append({"asset_id": row["asset_id"], "error": error})
        lease.take(ctx, feishu_table.LEASE_KEY, feishu_table.LEASE_SECONDS)
    return updated


# ---------------------------------------------------------------- 清理


def purge_scope(ctx: Ctx, asset_ids: list[str]) -> dict[str, Any]:
    """清理旧数据时这些款的套图：套图编号、图片目录、压缩包。"""
    sets = list(latest_sets(ctx, asset_ids).values())
    all_sets = []
    ids = sorted(set(asset_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        all_sets += [
            dict(r)
            for r in ctx.db.q(
                f"SELECT * FROM scene_sets WHERE asset_id IN ({_marks(chunk)})",
                tuple(chunk),
            )
        ]
    return {
        "sets": sorted(r["set_id"] for r in all_sets),
        "active": sorted(r["set_id"] for r in sets if r["status"] in ACTIVE),
        "exports": [
            {"key": r["export_key"], "path": r["export_path"]}
            for r in all_sets
            if r["export_key"] or r["export_path"]
        ],
        "prefixes": sorted({f"{SCENE_DIR}/{r['asset_id']}/" for r in all_sets}),
    }


def delete_exports(ctx: Ctx, exports: list[dict[str, Any]]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for item in exports:
        if item.get("key") and store.oss_enabled(ctx):
            try:
                store.client(ctx).delete(item["key"])
            except _OSS_FAILURES as exc:
                errors.append({"key": item["key"], "error": str(exc)[:300]})
        if item.get("path"):
            local = store.resolve(ctx.home, item["path"])
            if local.is_file():
                local.unlink()
    return errors
