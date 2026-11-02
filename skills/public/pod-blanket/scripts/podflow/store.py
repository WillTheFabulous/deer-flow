"""素材存储：本机目录是工作副本；`storage.backend: oss` 时以 OSS 为准，本机只做缓存。

- 布局（库里存数据目录内的相对路径）：assets/seeds/<seed>/…、assets/families/<F>/<V>/…；只写新文件，从不覆盖已有版本。
- OSS：对象 key = `storage.oss.prefix` + 相对路径。写入先落本机，并在调用方的事务里登记 storage_objects（pending）；
  提交后 `kick()` 拉起后台 `podctl storage sync` 上传。同一时刻只有一个上传进程（库里的租约），
  上传带 x-oss-forbid-overwrite，保持「版本不覆盖」。
- 读取一律走 `ensure_local()`：本机缺文件时从 OSS 拉回，按登记的 sha256 校验后原子写入。
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from . import bg, lease, limits
from . import config as cfgmod
from .oss import OSSClient, OSSError
from .util import now_iso, sha256_bytes

UPLOADER_LEASE_KEY = "storage_uploader"
UPLOADER_LEASE_SECONDS = 300
MAX_UPLOAD_ATTEMPTS = 8
_BATCH = 20


class StorageError(RuntimeError):
    pass


def seed_dir(home: Path, seed_id: str) -> Path:
    return home / "assets" / "seeds" / seed_id


def asset_dir(home: Path, family_id: str, variant_id: str) -> Path:
    return home / "assets" / "families" / family_id / variant_id


def write_new(path: Path, data: bytes) -> Path:
    """只写新文件：目标已存在说明编号冲突，直接报错而不是覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"{path} 已存在，素材版本不可覆盖")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def rel(home: Path, path: Path) -> str:
    try:
        return str(path.relative_to(home))
    except ValueError:
        return str(path)


def resolve(home: Path, stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else home / p


# ---------------------------------------------------------------- OSS


def oss_enabled(ctx: Any) -> bool:
    return str(ctx.setting("storage.backend", "local") or "local") == "oss"


def client(ctx: Any) -> OSSClient:
    conf = ctx.setting("storage.oss", {}) or {}
    id_name = conf.get("access_key_id_name") or "OSS_ACCESS_KEY_ID"
    secret_name = conf.get("access_key_secret_name") or "OSS_ACCESS_KEY_SECRET"
    ak, sk = ctx.secrets.get(id_name), ctx.secrets.get(secret_name)
    if not (conf.get("bucket") and conf.get("endpoint")):
        raise cfgmod.ConfigError(
            "storage.backend 为 oss，但 storage.oss.bucket / endpoint 没填"
        )
    if not (ak and sk):
        raise cfgmod.ConfigError(
            f"storage.backend 为 oss，但 secrets.env 里缺 {id_name} / {secret_name}"
        )
    return OSSClient(
        bucket=conf["bucket"],
        endpoint=conf["endpoint"],
        public_endpoint=conf.get("public_endpoint") or None,
        access_key_id=ak,
        access_key_secret=sk,
        timeout=float(conf.get("timeout_seconds") or 120),
        upload_rate_kbps=float(conf.get("upload_rate_kbps") or 0),
    )


def object_key(ctx: Any, stored: str) -> str:
    return f"{ctx.setting('storage.oss.prefix', 'pod-blanket/') or ''}{stored}"


def _content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def register(ctx: Any, conn: Any, path: Path, data: bytes | None = None) -> bool:
    """在调用方事务里登记一个待上传对象；已登记的不重复。返回是否新登记。"""
    payload = path.read_bytes() if data is None else data
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO storage_objects(path, sha256, bytes, content_type, status, attempts, created_at, updated_at)"
        " VALUES(?, ?, ?, ?, 'pending', 0, ?, ?) ON CONFLICT(path) DO NOTHING",
        (
            rel(ctx.home, path),
            sha256_bytes(payload),
            len(payload),
            _content_type(path),
            now,
            now,
        ),
    )
    return bool(cur.rowcount)


def save(ctx: Any, conn: Any, path: Path, data: bytes) -> Path:
    """写入一个新素材文件；OSS 模式下同时登记待上传（事务提交后调 kick）。"""
    write_new(path, data)
    if oss_enabled(ctx):
        register(ctx, conn, path, data)
    return path


def pending_count(ctx: Any) -> int:
    return int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM storage_objects WHERE status <> 'uploaded' AND attempts < ?",
            (MAX_UPLOAD_ATTEMPTS,),
        )
        or 0
    )


def uploader_active(ctx: Any) -> bool:
    return lease.active(ctx, UPLOADER_LEASE_KEY)


def kick(ctx: Any) -> dict[str, Any] | None:
    """有待上传的对象、又没有上传进程在跑时，拉起一个后台 `podctl storage sync`。

    worker 的多条工位会接连调：串行检查，刚拉起过就不再查（limits.recently_kicked）。
    """
    with limits.kick_lock("storage"):
        if limits.recently_kicked("storage"):
            return None
        if not oss_enabled(ctx) or uploader_active(ctx) or not pending_count(ctx):
            return None
        if any(t["kind"] == "sync" for t in bg.running(ctx)):
            return None
        task = bg.spawn(ctx, ["storage", "sync"], kind="sync")
        limits.mark_kicked("storage")
        return task


def _take_lease(ctx: Any) -> bool:
    return lease.take(ctx, UPLOADER_LEASE_KEY, UPLOADER_LEASE_SECONDS)


def _drop_lease(ctx: Any) -> None:
    lease.drop(ctx, UPLOADER_LEASE_KEY)


def _upload_one(ctx: Any, oss: OSSClient, row: dict[str, Any]) -> str | None:
    """上传一个对象；成功返回 None，失败记一次尝试并返回原因。"""
    key = object_key(ctx, row["path"])
    try:
        path = resolve(ctx.home, row["path"])
        if not path.is_file():
            raise StorageError("本机文件不存在，无法上传")
        data = path.read_bytes()
        sha = sha256_bytes(data)
        if row["sha256"] and sha != row["sha256"]:
            raise StorageError("本机文件与登记的 sha256 不一致")
        try:
            oss.put(
                key,
                data,
                content_type=row["content_type"] or _content_type(path),
                meta={"sha256": sha},
            )
        except OSSError as exc:
            # 409 = 已有同名对象（上次传完没来得及记账）：内容一致就当已上传
            if exc.status != 409:
                raise
            head = oss.head(key) or {}
            if head.get("x-oss-meta-sha256") != sha:
                raise StorageError(f"OSS 上已有内容不同的同名对象 {key}") from exc
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE storage_objects SET status = 'uploaded', sha256 = ?, bytes = ?, last_error = NULL,"
                " owner = NULL, lease_until = NULL, uploaded_at = ?, updated_at = ? WHERE path = ?",
                (sha, len(data), now_iso(), now_iso(), row["path"]),
            )
        return None
    except (OSSError, StorageError, OSError, httpx.HTTPError) as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE storage_objects SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE path = ?",
                (reason, now_iso(), row["path"]),
            )
        return reason


def upload_now(ctx: Any, path: Path) -> str:
    """马上把一个已登记的本机文件传上 OSS（要当场拿签名链接给外部系统用，如妙手上架图），返回对象 key。
    已经传过的直接返回；传不上抛 StorageError。"""
    stored = rel(ctx.home, path)
    row = ctx.db.q1("SELECT * FROM storage_objects WHERE path = ?", (stored,))
    if row is None:
        raise StorageError(f"{stored} 没有登记，传不了")
    if row["status"] != "uploaded":
        reason = _upload_one(ctx, client(ctx), dict(row))
        if reason:
            raise StorageError(f"{stored} 没传上 OSS：{reason}")
    return object_key(ctx, stored)


def _register_local_files(ctx: Any) -> int:
    """把数据目录 assets/ 下还没登记的文件全部登记为待上传（迁移已有素材用）。"""
    root = ctx.home / "assets"
    files = [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith((".tmp", ".dl"))
    ]
    added = 0
    with ctx.db.tx() as conn:
        for path in files:
            added += register(ctx, conn, path)
    return added


def sync(
    ctx: Any, *, register_all: bool = False, retry: bool = False
) -> dict[str, Any]:
    """上传所有待上传对象；另有上传进程持有租约时直接返回。"""
    if not oss_enabled(ctx):
        raise StorageError("storage.backend 不是 oss")
    registered = _register_local_files(ctx) if register_all else 0
    if retry:
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE storage_objects SET attempts = 0 WHERE status <> 'uploaded'"
            )
    if not _take_lease(ctx):
        return {
            "skipped": True,
            "reason": "另一个上传进程正在运行",
            "registered": registered,
        }
    oss = client(ctx)
    uploaded, errors, seen = 0, [], set()
    query = (
        "SELECT * FROM storage_objects WHERE status <> 'uploaded' AND attempts < ?"
        " ORDER BY created_at, path LIMIT ?"
    )
    try:
        while True:
            rows = [
                dict(r)
                for r in ctx.db.q(query, (MAX_UPLOAD_ATTEMPTS, len(seen) + _BATCH))
                if r["path"] not in seen
            ]
            if not rows:
                # 先放租约再复查：放租约前登记的对象，写入方 kick 时会因为租约还在而跳过
                _drop_lease(ctx)
                fresh = [
                    r
                    for r in ctx.db.q(query, (MAX_UPLOAD_ATTEMPTS, len(seen) + 1))
                    if r["path"] not in seen
                ]
                if not fresh or not _take_lease(ctx):
                    break
                continue
            for row in rows:
                seen.add(row["path"])
                bg.step(ctx, f"剩 {pending_count(ctx)} 个文件")
                reason = _upload_one(ctx, oss, row)
                if reason:
                    errors.append({"path": row["path"], "error": reason})
                else:
                    uploaded += 1
                _take_lease(ctx)
    finally:
        _drop_lease(ctx)
    return {
        "uploaded": uploaded,
        "failed": len(errors),
        "errors": errors[:5],
        "pending": pending_count(ctx),
        "registered": registered,
    }


def ensure_local(ctx: Any, stored: str) -> Path:
    """返回素材的本机路径；本机缺文件且开了 OSS 时先从 OSS 拉回（OSS 上也没有就原样返回）。"""
    path = resolve(ctx.home, stored)
    if path.exists() or not oss_enabled(ctx):
        return path
    rel_path = rel(ctx.home, path)
    try:
        data = client(ctx).get(object_key(ctx, rel_path))
    except OSSError as exc:
        if exc.status == 404:
            return path
        raise StorageError(f"从 OSS 拉回 {rel_path} 失败：{exc}") from exc
    except httpx.HTTPError as exc:
        raise StorageError(
            f"从 OSS 拉回 {rel_path} 失败：{type(exc).__name__}: {exc}"
        ) from exc
    row = ctx.db.q1("SELECT sha256 FROM storage_objects WHERE path = ?", (rel_path,))
    if row and row["sha256"] and sha256_bytes(data) != row["sha256"]:
        raise StorageError(f"{rel_path} 从 OSS 拉回的内容与登记的 sha256 不一致")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.dl")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def delete_tree(ctx: Any, prefix: str) -> dict[str, Any]:
    """彻底删掉一个目录（数据目录内的相对前缀，如 assets/families/F01/V0001/）：OSS 对象、登记与本机文件。

    只给清理旧数据用（maintenance purge-old），不可恢复。OSS 删失败的对象留着登记、列在 errors 里，重跑会再试；
    本机目录照样删掉。
    """
    prefix = prefix.rstrip("/") + "/"
    paths = [
        r["path"]
        for r in ctx.db.q(
            "SELECT path FROM storage_objects WHERE path LIKE ?", (prefix + "%",)
        )
        if r["path"].startswith(prefix)
    ]
    oss = client(ctx) if paths and oss_enabled(ctx) else None
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        if oss is not None:
            try:
                oss.delete(object_key(ctx, path))
            except (OSSError, httpx.HTTPError) as exc:
                errors.append(
                    {"path": path, "error": f"{type(exc).__name__}: {exc}"[:300]}
                )
                continue
        deleted.append(path)
    if deleted:
        with ctx.db.tx() as conn:
            for path in deleted:
                conn.execute("DELETE FROM storage_objects WHERE path = ?", (path,))
    folder = resolve(ctx.home, prefix)
    local = sum(1 for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else 0
    shutil.rmtree(folder, ignore_errors=True)
    return {"objects": len(deleted), "local_files": local, "errors": errors}


def status(ctx: Any) -> dict[str, Any]:
    rows = ctx.db.q(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS total FROM storage_objects GROUP BY status"
    )
    stuck = ctx.db.q(
        "SELECT path, attempts, last_error FROM storage_objects WHERE status <> 'uploaded' AND attempts >= ?"
        " ORDER BY path LIMIT 10",
        (MAX_UPLOAD_ATTEMPTS,),
    )
    conf = ctx.setting("storage.oss", {}) or {}
    return {
        "backend": ctx.setting("storage.backend", "local"),
        "bucket": conf.get("bucket"),
        "endpoint": conf.get("endpoint"),
        "prefix": conf.get("prefix"),
        "objects": {
            r["status"]: {"count": int(r["n"]), "bytes": int(r["total"])} for r in rows
        },
        "pending": pending_count(ctx),
        "uploader_active": uploader_active(ctx),
        "stuck": [dict(r) for r in stuck],
    }


def prune(
    ctx: Any, *, older_than_days: float = 30, dry_run: bool = False
) -> dict[str, Any]:
    """删掉已确认在 OSS 上、且本机副本超过 N 天没动的文件；缩略图缓存同理。待上传的绝不删。"""
    if not oss_enabled(ctx):
        raise StorageError("storage.backend 不是 oss：本机就是唯一副本，不能清理")
    cutoff = time.time() - float(older_than_days) * 86400
    removed, freed = 0, 0
    candidates = [
        resolve(ctx.home, r["path"])
        for r in ctx.db.q("SELECT path FROM storage_objects WHERE status = 'uploaded'")
    ]
    thumbs = ctx.home / "cache" / "thumbs"
    if thumbs.is_dir():
        candidates += [p for p in thumbs.iterdir() if p.is_file()]
    for path in candidates:
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        removed += 1
        freed += st.st_size
        if not dry_run:
            path.unlink(missing_ok=True)
    return {
        "removed": removed,
        "freed_bytes": freed,
        "dry_run": dry_run,
        "older_than_days": older_than_days,
    }
