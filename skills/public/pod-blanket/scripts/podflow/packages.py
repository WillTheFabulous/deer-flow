"""套图交付包：一个任务（参考图）的一个裂变批次打一个包，每款一个文件夹，里面的文件和每款的套图压缩包一样
（设计图 + 按 mooya 交付包编号的场景图，见 `scenes.zip_entries`）。

- 表 scene_packages（主键 seed_id + batch_id）。包里是这个批次选中的款各自最近一次出完（done / partial）的套图。
- 该做什么（`due`）：批次里没有排队 / 在出的套图（停在 mooya 等人工的不挡），要打进包的套图又和上次的不一样（指纹）
  → 打包；批次里已经没有出完的套图（改判、清理）→ 删掉包（行留着、链接清空）；链接快到期 → 续签。
  同一份内容打包失败满 MAX_ATTEMPTS 次不再自动试，内容变了（又有款出完）再试。
- 打包（`build`）：本机临时 zip（不压缩）→ OSS（流式上传）→ 签下载链接（中文文件名，table.link_days 天）→ 删旧包；
  本机存储时 zip 留在 exports/packages/。
- 谁来做：`scenes sync` 收尾时（`build_due`，租约 scene_packages）；没有套图在出、只有包要打时 `scenes.kick`
  拉起 `scenes pack`；`scenes repack` 手动重打一个。
- 用的地方：任务列表与任务明细（`by_seed`）、套图出完卡（`ready_for_sets`）、各任务候选表的「套图交付包」列
  （candidates 用 `cell` / `table_key` / `table_pending`）。
- 已下载（2026-10-26，表 package_downloads）：人下载了交付包后标一下（卡片按钮、候选表最上面的交付包行、POD 对话），
  记下当时那版包的指纹；包重打、内容变了标记就不算了，要再下载、再标。每个批次最新的包都标了，任务才算「套图已下载」，
  才会自动归档（shelf.py）。续签链接、内容不变的重打不影响标记。
"""

from __future__ import annotations

import datetime as dt
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import bg, feishu_table, labels, lease, scenes, store
from .config import ConfigError
from .context import Ctx
from .db import log_event
from .oss import OSSError
from .stages.common import StageError, seed_name, task_label
from .util import dumps, iso, now_iso, parse_iso, sha256_text, utcnow

EXPORT_DIR = "exports/packages"
LEASE_KEY = "scene_packages"
LEASE_SECONDS = 600
MAX_ATTEMPTS = 5
# 挡着不打包的套图：还没交、mooya 收下了、正在出；停在 mooya 等人工 / 已暂停的（scenes.PARKED）不挡，出完再重打
BUSY = ("queued", "submitted", "running")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\s]+')
_FAILURES = (store.StorageError, OSSError, OSError, httpx.HTTPError, ConfigError)


def enabled(ctx: Ctx) -> bool:
    return scenes.enabled(ctx) and bool(ctx.setting("scenes.batch_packages", True))


def _num(ident: str | None) -> int:
    digits = "".join(ch for ch in str(ident or "") if ch.isdigit())
    return int(digits) if digits else 0


def _marks(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def size_text(n: int | None) -> str:
    """包的大小：227MB、1.3GB、860KB。"""
    n = int(n or 0)
    if n >= 1024**3:
        return f"{n / 1024**3:.1f}GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f}MB"
    return f"{max(1, round(n / 1024))}KB"


# ---------------------------------------------------------------- 查询


def _empty(seed_id: str, batch_id: str) -> dict[str, Any]:
    return {
        "seed_id": seed_id,
        "batch_id": batch_id,
        "approved": 0,
        "busy": 0,
        "parked": 0,
        "failed": 0,
        "missing": 0,
        "items": [],
    }


def _groups(
    ctx: Ctx, seed_id: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """每个 (任务, 批次)：选中几款、各状态几款，以及要打进包的套图（每款最近一次出完的那套，按方向、素材排）。"""
    approved = scenes.approved_assets(ctx, seed_id=seed_id)
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ids = [a["asset_id"] for a in approved]
    for i in range(0, len(ids), labels.BATCH):
        chunk = ids[i : i + labels.BATCH]
        for row in ctx.db.q(
            f"SELECT * FROM scene_sets WHERE asset_id IN ({_marks(chunk)})",
            tuple(chunk),
        ):
            history[row["asset_id"]].append(dict(row))
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for asset in approved:
        key = (asset["seed_id"], asset["batch_id"])
        group = out.setdefault(key, _empty(*key))
        group["approved"] += 1
        rows = sorted(history.get(asset["asset_id"], []), key=scenes.set_order)
        status = rows[-1]["status"] if rows else None
        ok = next((r for r in reversed(rows) if r["status"] in scenes.OK_FINAL), None)
        if status in BUSY:
            group["busy"] += 1
        elif status in scenes.PARKED:
            group["parked"] += 1
        elif ok is None:
            group["missing"] += 1
            if status in ("failed", "cancelled"):
                group["failed"] += 1
        if ok:
            group["items"].append({"asset_id": asset["asset_id"], "set": ok})
    return out


def _pictures(ctx: Ctx, set_ids: list[str]) -> dict[str, list[str]]:
    """每套套图的内容（指纹用）：每张图的版本与路径，再接交付清单的顺序与文件名。"""
    pictures: dict[str, list[str]] = defaultdict(list)
    for i in range(0, len(set_ids), labels.BATCH):
        chunk = set_ids[i : i + labels.BATCH]
        for row in ctx.db.q(
            "SELECT set_id, image_id, rev, path FROM scene_images"
            f" WHERE set_id IN ({_marks(chunk)}) ORDER BY set_id, image_id",
            tuple(chunk),
        ):
            pictures[row["set_id"]].append(
                f"{row['image_id']}={row['rev']}@{row['path']}"
            )
        for row in ctx.db.q(
            "SELECT set_id, seq, image_id, file_name FROM scene_files"
            f" WHERE set_id IN ({_marks(chunk)}) ORDER BY set_id, seq",
            tuple(chunk),
        ):
            pictures[row["set_id"]].append(
                f"#{row['seq']}={row['image_id']}>{row['file_name']}"
            )
    return pictures


def _fingerprint(ctx: Ctx, items: list[dict[str, Any]]) -> str:
    """要打进包的内容：哪几款、各用哪一套、每套里每张图的版本（在 mooya 里重生过图、重出过都会变）、
    交付清单的顺序与文件名（在 mooya 交付工作台里调过序、换过图都会变）。"""
    pictures = _pictures(ctx, [item["set"]["set_id"] for item in items])
    parts = sorted(
        f"{item['asset_id']}:{item['set']['set_id']}:{','.join(pictures[item['set']['set_id']])}"
        for item in items
    )
    return sha256_text("|".join(parts))[:16]


def set_fingerprints(ctx: Ctx, pairs: list[tuple[str, str]]) -> dict[str, str]:
    """每款这一套套图的指纹（口径同 _fingerprint，一款一个）：{asset_id: 指纹}，pairs 是 (asset_id, set_id)。
    「选中的款」的下载确认按它记：在 mooya 里重生过图、调过交付清单都会变。"""
    pictures = _pictures(ctx, [set_id for _, set_id in pairs])
    return {
        asset_id: sha256_text(f"{asset_id}:{set_id}:{','.join(pictures[set_id])}")[:16]
        for asset_id, set_id in pairs
    }


def _packages(
    ctx: Ctx, seed_id: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    sql, params = "SELECT * FROM scene_packages", ()
    if seed_id:
        sql, params = sql + " WHERE seed_id = ?", (seed_id,)
    return {(r["seed_id"], r["batch_id"]): dict(r) for r in ctx.db.q(sql, params)}


def by_key(ctx: Ctx) -> dict[tuple[str, str], dict[str, Any]]:
    """全部交付包：{(任务, 批次): 行}（候选表的套图行一次取齐）。"""
    return _packages(ctx)


def get(ctx: Ctx, seed_id: str, batch_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT * FROM scene_packages WHERE seed_id = ? AND batch_id = ?",
        (seed_id, batch_id),
    )
    return dict(row) if row else None


def ready(pkg: dict[str, Any] | None) -> bool:
    """这个包打好了（OSS 上或本机有 zip）。"""
    return bool(pkg and (pkg["export_key"] or pkg["export_path"]))


def _todo(action: str, key: tuple[str, str]) -> dict[str, Any]:
    return {"action": action, "seed_id": key[0], "batch_id": key[1]}


def due(ctx: Ctx, *, seed_id: str | None = None) -> list[dict[str, Any]]:
    """该做的事：build（打 / 重打）、remove（批次里没有出完的套图了）、renew（链接快到期）。"""
    if not enabled(ctx):
        return []
    groups = _groups(ctx, seed_id)
    have = _packages(ctx, seed_id)
    out: list[dict[str, Any]] = []
    touched: set[tuple[str, str]] = set()
    for key, group in groups.items():
        if group["busy"]:
            continue
        pkg = have.get(key)
        if not group["items"]:
            if ready(pkg):
                out.append(_todo("remove", key))
                touched.add(key)
            continue
        fingerprint = _fingerprint(ctx, group["items"])
        if ready(pkg) and pkg["fingerprint"] == fingerprint:
            continue
        if (
            pkg
            and pkg["failed_fingerprint"] == fingerprint
            and int(pkg["attempts"] or 0) >= MAX_ATTEMPTS
        ):
            continue
        out.append({**_todo("build", key), "fingerprint": fingerprint})
        touched.add(key)
    for key, pkg in have.items():
        if key not in groups and ready(pkg):
            out.append(_todo("remove", key))
            touched.add(key)
    renew_before = feishu_table.refresh_before(ctx)
    for key, pkg in have.items():
        if key in touched or not pkg["export_key"]:
            continue
        if not pkg["export_expires_at"] or pkg["export_expires_at"] < renew_before:
            out.append(_todo("renew", key))
    return sorted(out, key=lambda t: (_num(t["seed_id"]), _num(t["batch_id"])))


def has_due(ctx: Ctx) -> bool:
    return bool(due(ctx))


# ---------------------------------------------------------------- 打包


def download_name(ctx: Ctx, seed_id: str, batch_id: str) -> str:
    """下载下来的文件名：S07_热带恐龙瀑布_1021_B010_套图交付包.zip（还没起名时没有中间那段）。"""
    parts = [seed_id, seed_name(ctx, seed_id) or "", batch_id, "套图交付包"]
    return _UNSAFE_NAME.sub("_", "_".join(p for p in parts if p)) + ".zip"


def _write_zip(
    ctx: Ctx, seed_id: str, batch_id: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """打 zip：每款一个文件夹（素材名字「IMG…_短语」），里面的文件名和每款的套图压缩包一样。
    OSS 存储时流式上传、签链接、删掉本机临时文件；本机存储时 zip 留在 exports/packages/。"""
    names = labels.assets(ctx, [item["asset_id"] for item in items])
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    stored = f"{EXPORT_DIR}/{seed_id}/{batch_id}/{seed_id}-{batch_id}-{stamp}.zip"
    local = store.resolve(ctx.home, stored)
    local.parent.mkdir(parents=True, exist_ok=True)
    keep_local = not store.oss_enabled(ctx)
    missing: list[str] = []
    count = 0
    finished = False
    try:
        with zipfile.ZipFile(local, "w", compression=zipfile.ZIP_STORED) as zf:
            for item in items:
                folder = names[item["asset_id"]]["label"]
                for path, arcname in scenes.zip_entries(ctx, item["set"]):
                    try:
                        src = store.ensure_local(ctx, path)
                    except store.StorageError:
                        missing.append(path)
                        continue
                    if not src.is_file():
                        missing.append(path)
                        continue
                    zf.write(src, arcname=f"{folder}/{arcname}{src.suffix}")
                    count += 1
        if not count:
            raise store.StorageError("套图的文件一个都找不到（本机和 OSS 上都没有）")
        name = download_name(ctx, seed_id, batch_id)
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
        if not keep_local:
            client = store.client(ctx)
            key = store.object_key(ctx, stored)
            client.put_file(
                key, local, content_type="application/zip", forbid_overwrite=False
            )
            days = int(ctx.setting("table.link_days", 90))
            out.update(
                key=key,
                path=None,
                url=client.sign_url(
                    key, days * 86400, filename=name, fallback=Path(stored).name
                ),
                expires_at=iso(utcnow() + dt.timedelta(days=days)),
            )
        finished = True
    finally:
        if not (finished and keep_local):
            local.unlink(missing_ok=True)
    return out


def _upsert(conn: Any, seed_id: str, batch_id: str, values: dict[str, Any]) -> None:
    now = now_iso()
    values = {**values, "updated_at": now}
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    conn.execute(
        f"INSERT INTO scene_packages(seed_id, batch_id, created_at, {cols})"
        f" VALUES(?, ?, ?, {marks}) ON CONFLICT(seed_id, batch_id) DO UPDATE SET {sets}",
        (seed_id, batch_id, now, *values.values()),
    )


def update(ctx: Ctx, seed_id: str, batch_id: str, **values: Any) -> None:
    values = {**values, "updated_at": now_iso()}
    sets = ", ".join(f"{k} = ?" for k in values)
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE scene_packages SET {sets} WHERE seed_id = ? AND batch_id = ?",
            (*values.values(), seed_id, batch_id),
        )


def _delete_export(
    ctx: Ctx, pkg: dict[str, Any] | None, *, keep: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """删掉包的文件（OSS 对象或本机 zip）；keep 是刚打好的新包，同一个对象不删。删不掉只记下来。"""
    errors: list[dict[str, str]] = []
    if not pkg:
        return errors
    keep = keep or {}
    if (
        pkg["export_key"]
        and pkg["export_key"] != keep.get("key")
        and store.oss_enabled(ctx)
    ):
        try:
            store.client(ctx).delete(pkg["export_key"])
        except _FAILURES as exc:
            errors.append({"key": pkg["export_key"], "error": str(exc)[:300]})
    if pkg["export_path"] and pkg["export_path"] != keep.get("path"):
        store.resolve(ctx.home, pkg["export_path"]).unlink(missing_ok=True)
    return errors


def _record_failure(
    ctx: Ctx,
    seed_id: str,
    batch_id: str,
    fingerprint: str,
    exc: BaseException,
    previous: dict[str, Any] | None,
) -> str:
    error = f"{type(exc).__name__}: {exc}"[:500]
    attempts = 1
    if previous and previous["failed_fingerprint"] == fingerprint:
        attempts = int(previous["attempts"] or 0) + 1
    with ctx.db.tx() as conn:
        _upsert(
            conn,
            seed_id,
            batch_id,
            {
                "attempts": attempts,
                "last_error": error,
                "failed_fingerprint": fingerprint,
            },
        )
        log_event(
            conn,
            batch_id,
            "scene_package_failed",
            {"seed_id": seed_id, "attempts": attempts, "error": error[:200]},
        )
    return error


def build(ctx: Ctx, seed_id: str, batch_id: str) -> dict[str, Any]:
    """打（或重打）这个批次的交付包，新包记好后删旧包；失败记次数再抛出。调用方持有租约。"""
    group = _groups(ctx, seed_id).get((seed_id, batch_id))
    if not group or not group["items"]:
        raise StageError(f"{seed_id} · {batch_id} 还没有出完的套图，没有东西可以打包")
    items = group["items"]
    fingerprint = _fingerprint(ctx, items)
    previous = get(ctx, seed_id, batch_id)
    try:
        export = _write_zip(ctx, seed_id, batch_id, items)
    except _FAILURES as exc:
        _record_failure(ctx, seed_id, batch_id, fingerprint, exc, previous)
        raise
    with ctx.db.tx() as conn:
        _upsert(
            conn,
            seed_id,
            batch_id,
            {
                "fingerprint": fingerprint,
                "set_ids_json": dumps([item["set"]["set_id"] for item in items]),
                "item_count": len(items),
                "file_count": export["count"],
                "missing_json": dumps(export["missing"]) if export["missing"] else None,
                "export_name": export["name"],
                "export_key": export["key"],
                "export_path": export["path"],
                "export_url": export["url"],
                "export_expires_at": export["expires_at"],
                "export_bytes": export["bytes"],
                "attempts": 0,
                "last_error": None,
                "failed_fingerprint": None,
                "table_attempts": 0,
                "table_error": None,
                "built_at": now_iso(),
            },
        )
        log_event(
            conn,
            batch_id,
            "scene_package_built",
            {
                "seed_id": seed_id,
                "items": len(items),
                "files": export["count"],
                "bytes": export["bytes"],
                "missing": len(export["missing"]),
            },
        )
    cleanup = _delete_export(ctx, previous, keep=export)
    return {
        "seed_id": seed_id,
        "batch_id": batch_id,
        "items": len(items),
        "files": export["count"],
        "bytes": export["bytes"],
        "missing": export["missing"],
        "url": export["url"],
        "path": export["path"],
        "cleanup_errors": cleanup,
    }


def _remove(ctx: Ctx, seed_id: str, batch_id: str) -> None:
    """批次里已经没有出完的套图：删掉包的文件，行留着（链接清空），候选表那一格跟着清掉。"""
    pkg = get(ctx, seed_id, batch_id)
    if not ready(pkg):
        return
    _delete_export(ctx, pkg)
    with ctx.db.tx() as conn:
        _upsert(
            conn,
            seed_id,
            batch_id,
            {
                "fingerprint": None,
                "set_ids_json": "[]",
                "item_count": 0,
                "file_count": 0,
                "missing_json": None,
                "export_key": None,
                "export_path": None,
                "export_url": None,
                "export_expires_at": None,
                "export_bytes": None,
                "attempts": 0,
                "last_error": None,
                "failed_fingerprint": None,
                "table_attempts": 0,
                "table_error": None,
            },
        )
        log_event(conn, batch_id, "scene_package_removed", {"seed_id": seed_id})


def _renew(ctx: Ctx, pkg: dict[str, Any]) -> None:
    days = int(ctx.setting("table.link_days", 90))
    key = pkg["export_key"]
    url = store.client(ctx).sign_url(
        key,
        days * 86400,
        filename=pkg["export_name"] or Path(key).name,
        fallback=Path(key).name,
    )
    update(
        ctx,
        pkg["seed_id"],
        pkg["batch_id"],
        export_url=url,
        export_expires_at=iso(utcnow() + dt.timedelta(days=days)),
        table_attempts=0,
        table_error=None,
    )


def build_due(ctx: Ctx, *, seed_id: str | None = None) -> dict[str, Any]:
    """把该打的包打了、该删的删了、快到期的链接续签（租约 scene_packages）；有变化时拉起表格同步。"""
    out: dict[str, Any] = {"built": [], "renewed": [], "removed": [], "failed": []}
    if not enabled(ctx):
        return {
            **out,
            "skipped": True,
            "reason": "交付包关着（scenes.enabled / scenes.batch_packages）",
        }
    if not due(ctx, seed_id=seed_id):
        return out
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        return {**out, "skipped": True, "reason": "另一个进程正在打交付包"}
    try:
        for todo in due(ctx, seed_id=seed_id):
            item = {"seed_id": todo["seed_id"], "batch_id": todo["batch_id"]}
            try:
                if todo["action"] == "build":
                    bg.step(ctx, f"打交付包 {todo['seed_id']} · {todo['batch_id']}")
                    out["built"].append(build(ctx, todo["seed_id"], todo["batch_id"]))
                elif todo["action"] == "renew":
                    pkg = get(ctx, todo["seed_id"], todo["batch_id"])
                    if pkg:
                        _renew(ctx, pkg)
                        out["renewed"].append(item)
                else:
                    _remove(ctx, todo["seed_id"], todo["batch_id"])
                    out["removed"].append(item)
            except (*_FAILURES, StageError) as exc:
                out["failed"].append(
                    {
                        **item,
                        "action": todo["action"],
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
            lease.take(ctx, LEASE_KEY, LEASE_SECONDS)
    finally:
        lease.drop(ctx, LEASE_KEY)
    if out["built"] or out["renewed"] or out["removed"]:
        feishu_table.kick(ctx)
    return out


def repack(ctx: Ctx, seed_id: str, batch_id: str) -> dict[str, Any]:
    """手动重打一个批次的交付包（内容没变也重打，链接会换）。"""
    if not enabled(ctx):
        raise StageError("交付包关着：scenes.enabled 与 scenes.batch_packages 都要打开")
    if not lease.take(ctx, LEASE_KEY, LEASE_SECONDS):
        raise StageError("另一个进程正在打交付包，过一会儿再试")
    try:
        try:
            out = build(ctx, seed_id, batch_id)
        except _FAILURES as exc:
            raise StageError(f"交付包没打成：{type(exc).__name__}: {exc}") from exc
    finally:
        lease.drop(ctx, LEASE_KEY)
    feishu_table.kick(ctx)
    return out


# ---------------------------------------------------------------- 视图


def _view(
    key: tuple[str, str],
    group: dict[str, Any],
    pkg: dict[str, Any] | None,
    fingerprint: str | None,
    mark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    is_ready = ready(pkg)
    gave_up = bool(
        pkg
        and fingerprint
        and pkg["failed_fingerprint"] == fingerprint
        and int(pkg["attempts"] or 0) >= MAX_ATTEMPTS
    )
    # 标的是现在这一版包才算下载过；标的是重打前的旧包时是 download_outdated
    downloaded = bool(is_ready and mark and mark["fingerprint"] == pkg["fingerprint"])
    return {
        "seed_id": key[0],
        "batch_id": key[1],
        "approved": group["approved"],
        "done": len(group["items"]),
        "busy": group["busy"],
        "parked": group["parked"],
        "failed": group["failed"],
        "missing": group["missing"],
        "ready": is_ready,
        # 打好的包之后又有款出完（或重生过图），还没重打
        "stale": bool(is_ready and fingerprint and fingerprint != pkg["fingerprint"]),
        "items": pkg["item_count"] if is_ready else 0,
        "files": pkg["file_count"] if is_ready else 0,
        "bytes": pkg["export_bytes"] if is_ready else None,
        "url": pkg["export_url"] if is_ready else None,
        "path": pkg["export_path"] if is_ready else None,
        "expires_at": pkg["export_expires_at"] if is_ready else None,
        "built_at": pkg["built_at"] if is_ready else None,
        "error": pkg["last_error"] if gave_up else None,
        "downloaded": downloaded,
        "downloaded_at": mark["marked_at"] if downloaded else None,
        "downloaded_by": mark["marked_by"] if downloaded else None,
        "download_outdated": bool(is_ready and mark and not downloaded),
    }


def _download_marks(
    ctx: Ctx, seed_id: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    sql, params = "SELECT * FROM package_downloads", ()
    if seed_id:
        sql, params = sql + " WHERE seed_id = ?", (seed_id,)
    return {(r["seed_id"], r["batch_id"]): dict(r) for r in ctx.db.q(sql, params)}


def by_seed(ctx: Ctx, seed_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """每个任务各批次的交付包情况（新批次在前）：打好的、在出套图等着打包的；只选中了还没出套图的批次不列。"""
    if not enabled(ctx):
        return {}
    groups = _groups(ctx, seed_id)
    have = _packages(ctx, seed_id)
    marks = _download_marks(ctx, seed_id)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key in set(groups) | {k for k, p in have.items() if ready(p)}:
        group = groups.get(key) or _empty(*key)
        pkg = have.get(key)
        if (
            not ready(pkg)
            and not group["items"]
            and not group["busy"]
            and not group["parked"]
        ):
            continue
        fingerprint = _fingerprint(ctx, group["items"]) if group["items"] else None
        out[key[0]].append(_view(key, group, pkg, fingerprint, marks.get(key)))
    for views in out.values():
        views.sort(key=lambda v: _num(v["batch_id"]), reverse=True)
    return dict(out)


def complete(view: dict[str, Any]) -> bool:
    """这个批次的交付包打好了、是最新内容、也标了已下载（没有在出的款、没有等着重打的新款）。"""
    return bool(
        view["ready"]
        and view["downloaded"]
        and not view["stale"]
        and not view["busy"]
        and not view["parked"]
    )


def download_summary(views: list[dict[str, Any]]) -> dict[str, Any]:
    """一个任务各批次交付包（by_seed 的一项）的下载情况：{batches, downloaded, pending[], outdated[], last_at}。
    pending 是还没标、或标的是旧包、或又有新款要重打的批次（新批次在前）。"""
    done = [v for v in views if complete(v)]
    return {
        "batches": len(views),
        "downloaded": len(done),
        "pending": [v["batch_id"] for v in views if not complete(v)],
        "outdated": [v["batch_id"] for v in views if v["download_outdated"]],
        "last_at": max((v["downloaded_at"] for v in done), default=None),
    }


def download_state(ctx: Ctx) -> dict[str, dict[str, Any]]:
    """每个任务的交付包下载情况：{seed_id: download_summary}。只算有交付包或在出套图的批次。"""
    return {seed_id: download_summary(views) for seed_id, views in by_seed(ctx).items()}


def _view_of(ctx: Ctx, seed_id: str, batch_id: str) -> dict[str, Any] | None:
    return next(
        (
            v
            for v in by_seed(ctx, seed_id).get(seed_id, [])
            if v["batch_id"] == batch_id
        ),
        None,
    )


def mark_downloaded(
    ctx: Ctx, seed_id: str, batch_id: str, *, by: str, source: str
) -> dict[str, Any]:
    """标「这个批次的交付包已下载」：记下现在这版包的指纹。包还没打好、或有新出完的款马上要重打时拒绝。
    已经标过这一版时返回 already。source：card / table / chat / selected（「选中的款」里这批的款都勾了）。
    包里的款在「选中的款」里也都算下载确认了（downloads.confirm_package，已经确认的不动）。"""
    from . import downloads  # 下载确认依赖本模块，放到用时再导入

    if not enabled(ctx):
        raise StageError("交付包没打开（scenes.batch_packages）")
    label = f"{task_label(ctx, seed_id)} · {batch_id}"
    pkg = get(ctx, seed_id, batch_id)
    view = _view_of(ctx, seed_id, batch_id)
    if not ready(pkg) or view is None:
        raise StageError(f"{label} 的交付包还没打好：出完套图打好包再标")
    if view["stale"]:
        raise StageError(
            f"{label} 有新出完的款，交付包马上重打：等新包打好、下载以后再标"
        )
    out = {
        "seed_id": seed_id,
        "batch_id": batch_id,
        "label": label,
        "items": pkg["item_count"],
        "busy": view["busy"],
        "parked": view["parked"],
    }
    if view["downloaded"]:
        downloads.confirm_package(ctx, seed_id, batch_id, by=by)
        return {**out, "already": True, "marked_at": view["downloaded_at"]}
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO package_downloads(seed_id, batch_id, fingerprint, export_key, item_count,"
            " marked_by, marked_at, source) VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(seed_id, batch_id) DO UPDATE SET fingerprint = excluded.fingerprint,"
            " export_key = excluded.export_key, item_count = excluded.item_count,"
            " marked_by = excluded.marked_by, marked_at = excluded.marked_at, source = excluded.source",
            (
                seed_id,
                batch_id,
                pkg["fingerprint"],
                pkg["export_key"],
                pkg["item_count"],
                by,
                now,
                source,
            ),
        )
        log_event(
            conn,
            seed_id,
            "package_downloaded",
            {
                "batch_id": batch_id,
                "items": pkg["item_count"],
                "by": by,
                "source": source,
            },
        )
    downloads.confirm_package(ctx, seed_id, batch_id, by=by)
    # 候选表最上面的交付包行改备注、连同这个批次的套图行沉到最底；「选中的款」里这批的款打勾沉底
    feishu_table.kick(ctx)
    return {**out, "marked_at": now}


def unmark_downloaded(
    ctx: Ctx, seed_id: str, batch_id: str, *, by: str
) -> dict[str, Any]:
    """撤销「已下载」（标错了）：交付包行回到候选表最上面，任务回到「套图完成」；「选中的款」里由这个交付包
    带出来的下载确认一起撤掉（人自己勾的留着）。"""
    from . import downloads  # 下载确认依赖本模块，放到用时再导入

    label = f"{task_label(ctx, seed_id)} · {batch_id}"
    with ctx.db.tx() as conn:
        removed = conn.execute(
            "DELETE FROM package_downloads WHERE seed_id = ? AND batch_id = ?",
            (seed_id, batch_id),
        ).rowcount
        if removed:
            log_event(
                conn,
                seed_id,
                "package_download_undone",
                {"batch_id": batch_id, "by": by},
            )
    undone = (
        downloads.unconfirm_package(ctx, seed_id, batch_id, by=by) if removed else []
    )
    if removed:
        feishu_table.kick(ctx)
    return {
        "seed_id": seed_id,
        "batch_id": batch_id,
        "label": label,
        "undone": bool(removed),
        "selected_undone": undone,
    }


def ready_for_sets(ctx: Ctx, seed_id: str, set_ids: list[str]) -> list[dict[str, Any]]:
    """这几套套图所在批次里、已经打好且是最新内容的交付包（套图出完卡片放下载链接）。"""
    if not enabled(ctx) or not set_ids:
        return []
    batches = {
        r["batch_id"]
        for r in ctx.db.q(
            "SELECT DISTINCT v.batch_id FROM scene_sets s"
            " JOIN assets a ON a.asset_id = s.asset_id"
            " JOIN variants v ON v.variant_id = a.variant_id"
            f" WHERE s.set_id IN ({_marks(set_ids)})",
            tuple(set_ids),
        )
    }
    return [
        v
        for v in by_seed(ctx, seed_id).get(seed_id, [])
        if v["batch_id"] in batches and v["ready"] and not v["stale"]
    ]


def overview(ctx: Ctx, seed_id: str | None = None) -> dict[str, Any]:
    """命令输出：各任务各批次的交付包（带任务称呼与下载链接）、还有哪些要打。"""
    views = by_seed(ctx, seed_id)
    items = []
    for sid in sorted(views, key=_num):
        task = task_label(ctx, sid)
        for view in views[sid]:
            items.append(
                {
                    **view,
                    "task": task,
                    "label": f"{task} · {view['batch_id']}",
                    "size": size_text(view["bytes"]) if view["ready"] else None,
                    "download": view["url"] or view["path"],
                }
            )
    return {
        "enabled": enabled(ctx),
        "packages": items,
        "due": due(ctx, seed_id=seed_id),
        "building": lease.active(ctx, LEASE_KEY),
    }


# ---------------------------------------------------------------- 候选表


def cell(ctx: Ctx, pkg: dict[str, Any] | None) -> dict[str, str] | None:
    """候选表「套图交付包」那一格：有下载链接时 {link, text}；还没打好、本机存储、已删掉时 None（清空）。"""
    if not ready(pkg) or not pkg["export_url"]:
        return None
    text = f"下载 {pkg['batch_id']} 交付包（{pkg['item_count']} 款"
    if pkg["export_expires_at"]:
        until = parse_iso(pkg["export_expires_at"]).astimezone(
            ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
        )
        text += f" · {until:%Y-%m-%d} 前有效"
    return {"link": pkg["export_url"], "text": text + "）"}


def table_key(pkg: dict[str, Any] | None) -> str:
    """写进候选表的样子：链接或款数变了才再写；没有链接时是空串。"""
    if not ready(pkg) or not pkg["export_url"]:
        return ""
    return f"{pkg['export_url']}|{pkg['item_count']}"


def table_pending(ctx: Ctx, seed_ids: set[str]) -> list[dict[str, Any]]:
    """这些任务里、候选表那一格和现在不一样的包（失败没到上限）。"""
    if not enabled(ctx) or not seed_ids:
        return []
    return [
        pkg
        for key, pkg in sorted(
            _packages(ctx).items(), key=lambda kv: (_num(kv[0][0]), _num(kv[0][1]))
        )
        if key[0] in seed_ids
        and int(pkg["table_attempts"] or 0) < feishu_table.MAX_ATTEMPTS
        and (pkg["table_state"] or "") != table_key(pkg)
    ]


# ---------------------------------------------------------------- 清理


def purge_scope(ctx: Ctx, seeds: list[str], batches: list[str]) -> dict[str, Any]:
    """清理旧数据时要删的交付包：这些参考图或批次的包，和它们的文件（已下载标记跟着包的行一起删）。"""
    rows = [
        pkg
        for (sid, bid), pkg in _packages(ctx).items()
        if sid in set(seeds) or bid in set(batches)
    ]
    return {
        "packages": sorted(f"{p['seed_id']}:{p['batch_id']}" for p in rows),
        "rows": [(p["seed_id"], p["batch_id"]) for p in rows],
        "exports": [
            {"key": p["export_key"], "path": p["export_path"]}
            for p in rows
            if p["export_key"] or p["export_path"]
        ],
    }
