"""上架素材（2026-10-28）：把一款最近一套出完的套图转成妙手要的 10 张上架图，和原 Windows 流程逐项一致。

- 图位：mooya 交付清单（scene_files）是交付工作台导出的顺序（001–002 是 04 / 05 那一对，先后按包抽签；
  003–007 其余点缀；008 适用场景；009 机洗；010 尺码图）。人工上架的款（10-28 读了 8 个线上款）把尺码图挪到第 3 张，
  其余照清单先后，所以上架顺序 = 1–2 那一对、3 尺码图、4–8 其余点缀、9 适用场景、10 机洗。
  校验（`temu.images.check_slots`，缺省开）：正好 10 张，清单最后一张是尺码图（role insert），1–2 是 04 / 05，8 / 9 是 08 / 07；
  对不上不自动上，原因写明哪一位不对。顺序只认 mooya 一处，不在这里重排别的位置。
- 字标：用交这套套图的那个 mooya 客户端按导出口径取（variant=export）：字标租户（ai_temu_deerflow）贴 MIANDUDU，
  和交付工作台导出 ZIP 同一个函数。2026-10-28 前在 platform_internal 交的旧套图取不到带字标的图，不自动上。
- 转换：原 wechat_flr.py 的 write_member_as_jpg 原样照搬（RGBA 用 alpha 贴到白底、其余模式转 RGB、JPEG 质量 95、不缩放），
  命名 1.jpg…10.jpg（原 image_sort_key 对 001–010 排出来的先后不变）。
- 存放：exports/temu/<IMG>/<SS>/<n>.jpg（以 OSS 为准），当场传 OSS、签 `temu.images.link_days` 天的链接给妙手；
  另打上架图包（`<货号>/1.jpg…10.jpg`，和以前「店铺轮播图/FLRxxxx/」一样），链接给「已上架的款」。
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from . import mooya, scenes, store, trace
from .context import Ctx
from .db import log_event
from .stages.common import StageError
from .util import dumps, iso, loads, now_iso, parse_iso, sha256_bytes, utcnow

EXPORT_DIR = Path("exports") / "temu"
SLOTS = 10
SIZE_CHART_POSITION = 3
HEAD_PLANS = frozenset({"blanket_04_model_read_lap", "blanket_05_sofa_drape"})
SCENARIO_PLAN = "blanket_08_use_scenario_flatlay"
WASH_PLAN = "blanket_07_wash_lifestyle"
MAIN_KINDS = {
    "blanket_04_model_read_lap": "模特盖腿读书",
    "blanket_05_sofa_drape": "沙发搭毯",
}
_SORT_DIGITS = re.compile(r"\d+")


# ---------------------------------------------------------------- 原 Windows 流程的两段代码（照搬）


def image_sort_key(member: str) -> tuple:
    """wechat_flr.py 的 image_sort_key：文件名里第一个数字排前面，没有数字的排后面。"""
    stem = Path(member.replace("\\", "/")).stem
    match = _SORT_DIGITS.search(stem)
    if match:
        return (0, int(match.group(0)), stem.lower())
    return (1, stem.lower())


def to_listing_jpeg(data: bytes) -> bytes:
    """wechat_flr.py 的 write_member_as_jpg：透明底贴白、其余转 RGB，JPEG 质量 95，不缩放。"""
    image = Image.open(io.BytesIO(data))
    image.load()
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        if image.mode == "RGBA":
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=95)
    return buf.getvalue()


# ---------------------------------------------------------------- 图位


def _delivery(ctx: Ctx, set_id: str) -> list[dict[str, Any]]:
    """交付清单 + 每张的方案 id（scene_images.plan_id；尺码图等插入图没有）。"""
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT f.seq, f.image_id, f.file_name, f.role, f.title, i.plan_id, i.path, i.rev"
            " FROM scene_files f LEFT JOIN scene_images i ON i.set_id = f.set_id AND i.image_id = f.image_id"
            " WHERE f.set_id = ? ORDER BY f.seq",
            (set_id,),
        )
    ]


def slot_problems(files: list[dict[str, Any]]) -> list[str]:
    """交付清单对不对得上标准 10 张图位；空 = 对得上。"""
    if len(files) != SLOTS:
        return [f"交付清单是 {len(files)} 张，点缀版应当正好 {SLOTS} 张"]
    problems = []
    if (files[-1].get("role") or "") != "insert":
        problems.append(f"第 {SLOTS} 张（{files[-1].get('file_name')}）不是尺码图")
    head = {files[0].get("plan_id"), files[1].get("plan_id")}
    if head != HEAD_PLANS:
        problems.append("第 1–2 张不是模特盖腿读书 / 沙发搭毯那一对（10-26 之前建的旧包没有前排钉序）")
    if files[7].get("plan_id") != SCENARIO_PLAN:
        problems.append("第 8 张不是适用场景版式图")
    if files[8].get("plan_id") != WASH_PLAN:
        problems.append("第 9 张不是机洗图")
    return problems


def listing_order(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """交付清单 → 上架顺序：最后一张尺码图挪到第 3 张，其余照清单先后。"""
    if not files:
        return []
    chart = [f for f in files if (f.get("role") or "") == "insert"]
    rest = [f for f in files if (f.get("role") or "") != "insert"]
    if len(chart) != 1:
        return list(files)
    at = SIZE_CHART_POSITION - 1
    return rest[:at] + chart + rest[at:]


# ---------------------------------------------------------------- 准备


def latest_ok_set(ctx: Ctx, asset_id: str) -> dict[str, Any] | None:
    current = scenes.latest_sets(ctx, [asset_id]).get(asset_id)
    if current and current["status"] in scenes.OK_FINAL:
        return current
    return None


def get(ctx: Ctx, asset_id: str, set_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT * FROM listing_assets WHERE asset_id = ? AND set_id = ?", (asset_id, set_id)
    )
    if not row:
        return None
    out = dict(row)
    out["files"] = loads(out.pop("files_json"), []) or []
    return out


def current(ctx: Ctx, asset_id: str) -> dict[str, Any] | None:
    """这一款最近一套出完的套图准备好的上架素材（套图换了新的一套就不算）。"""
    row = latest_ok_set(ctx, asset_id)
    if not row:
        return None
    found = get(ctx, asset_id, row["set_id"])
    return found if found and found.get("prepared_at") else None


def readiness(ctx: Ctx, asset_id: str) -> str | None:
    """还不能准备上架素材的原因；None = 可以准备。"""
    row = scenes.latest_sets(ctx, [asset_id]).get(asset_id)
    if row is None:
        return "还没出套图"
    if row["status"] in scenes.PARKED:
        return "套图在 mooya 等人工"
    if row["status"] not in scenes.OK_FINAL:
        return "套图还在出" if row["status"] in scenes.ACTIVE else "套图没出成"
    if scenes.client_of(ctx, row["set_id"]) == mooya.LEGACY_CLIENT:
        return "这套套图是在旧租户（platform_internal）出的，取不到带字标的图；重出套图后再上"
    if not store.oss_enabled(ctx):
        return "上架要开 OSS（storage.backend: oss），妙手要能下载到图"
    problems = slot_check(ctx, row["set_id"])
    if problems:
        return "图位对不上：" + "；".join(problems)
    return None


def slot_check(ctx: Ctx, set_id: str) -> list[str]:
    """这套套图的图位校验（关着 temu.images.check_slots 时不查）：对不上的地方，空 = 过了。"""
    if not ctx.setting("temu.images.check_slots", True):
        return []
    return slot_problems(_delivery(ctx, set_id))


def _link_days(ctx: Ctx) -> int:
    return int(ctx.setting("temu.images.link_days", 30))


def prepare(ctx: Ctx, asset_id: str, *, force: bool = False) -> dict[str, Any]:
    """取带字标的交付图、转 1.jpg…10.jpg、传 OSS、签链接，存进 listing_assets。已经准备好且链接没快到期的直接返回。"""
    reason = readiness(ctx, asset_id)
    if reason:
        raise StageError(reason)
    row = latest_ok_set(ctx, asset_id)
    assert row is not None
    existing = get(ctx, asset_id, row["set_id"])
    if existing and existing.get("prepared_at") and not force:
        # 链接快到期只重签（不重取图：字标落点每次随机，重取会换一套图）
        return _resign_files(ctx, existing) if _expiring(ctx, existing) else existing
    files = listing_order(_delivery(ctx, row["set_id"]))
    oss = store.client(ctx)
    days = _link_days(ctx)
    out_files: list[dict[str, Any]] = []
    with mooya.Client(ctx, client=scenes.client_of(ctx, row["set_id"])) as api:
        for n, item in enumerate(files, 1):
            raw, _rev = api.image(
                row["mooya_job_id"], item["image_id"], variant=mooya.VARIANT_EXPORT
            )
            data = to_listing_jpeg(raw)
            # 字标每次取都随机落点，文件名带内容摘要，重取不会和上一份撞名
            path = (
                ctx.home
                / EXPORT_DIR
                / asset_id
                / row["set_id"]
                / f"{n}-{sha256_bytes(data)[:8]}.jpg"
            )
            if not path.exists():
                with ctx.db.tx() as conn:
                    store.save(ctx, conn, path, data)
            key = store.upload_now(ctx, path)
            out_files.append(
                {
                    "n": n,
                    "image_id": item["image_id"],
                    "plan_id": item.get("plan_id"),
                    "role": item.get("role"),
                    "title": item.get("title"),
                    "path": store.rel(ctx.home, path),
                    "key": key,
                    "sha256": sha256_bytes(data),
                    "url": oss.sign_url(key, days * 86400),
                }
            )
    expires = iso(utcnow() + dt.timedelta(days=days))
    fingerprint = sha256_bytes(dumps([(f["image_id"], f["sha256"]) for f in out_files]).encode())
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO listing_assets(asset_id, set_id, fingerprint, files_json, links_expire_at, attempts,"
            " last_error, prepared_at, updated_at) VALUES(?, ?, ?, ?, ?, 0, NULL, ?, ?)"
            " ON CONFLICT(asset_id, set_id) DO UPDATE SET fingerprint = excluded.fingerprint,"
            " files_json = excluded.files_json, links_expire_at = excluded.links_expire_at, attempts = 0,"
            " last_error = NULL, prepared_at = excluded.prepared_at, updated_at = excluded.updated_at",
            (asset_id, row["set_id"], fingerprint, dumps(out_files), expires, now, now),
        )
        log_event(conn, asset_id, "listing_assets_prepared", {"set_id": row["set_id"], "files": len(out_files)})
        trace.record(
            conn,
            asset_id,
            "assets",
            to="prepared",
            set_id=row["set_id"],
            payload={"files": len(out_files), "links_expire_at": expires, "fingerprint": fingerprint},
        )
    return get(ctx, asset_id, row["set_id"]) or {}


def _expiring(ctx: Ctx, item: dict[str, Any]) -> bool:
    expires = parse_iso(item.get("links_expire_at"))
    days = int(ctx.setting("table.link_refresh_days", 15))
    return expires is None or expires < utcnow() + dt.timedelta(days=min(days, _link_days(ctx) - 1))


def _resign_files(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any]:
    """已经传到 OSS 的上架图重签一遍链接。"""
    oss = store.client(ctx)
    days = _link_days(ctx)
    files = [{**f, "url": oss.sign_url(f["key"], days * 86400)} for f in item.get("files") or []]
    expires = iso(utcnow() + dt.timedelta(days=days))
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE listing_assets SET files_json = ?, links_expire_at = ?, updated_at = ?"
            " WHERE asset_id = ? AND set_id = ?",
            (
                dumps(files),
                expires,
                now_iso(),
                item["asset_id"],
                item["set_id"],
            ),
        )
        trace.record(
            conn,
            item["asset_id"],
            "assets",
            to="resigned",
            set_id=item["set_id"],
            payload={"links_expire_at": expires, "was": item.get("links_expire_at")},
        )
    return get(ctx, item["asset_id"], item["set_id"]) or item


def resign_now(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any]:
    """不管还有多久到期，现在就重签（在采集箱里放得久的款存回妙手前用）。"""
    return _resign_files(ctx, item)


def failed(ctx: Ctx, asset_id: str, set_id: str, reason: str) -> None:
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO listing_assets(asset_id, set_id, attempts, last_error, updated_at)"
            " VALUES(?, ?, 1, ?, ?) ON CONFLICT(asset_id, set_id) DO UPDATE SET"
            " attempts = listing_assets.attempts + 1, last_error = excluded.last_error,"
            " updated_at = excluded.updated_at",
            (asset_id, set_id, reason[:300], now),
        )
        trace.record(conn, asset_id, "assets", to="failed", set_id=set_id, reason=reason)


def urls(item: dict[str, Any]) -> list[str]:
    return [f["url"] for f in item.get("files") or []]


def first_image(ctx: Ctx, item: dict[str, Any]) -> bytes:
    """上架轮播图第 1 张的字节（写标题用，看的就是这张）。"""
    first = (item.get("files") or [{}])[0]
    return store.ensure_local(ctx, first["path"]).read_bytes()


def main_kind(item: dict[str, Any]) -> str:
    first = (item.get("files") or [{}])[0]
    return MAIN_KINDS.get(first.get("plan_id") or "", "其他")


def image_order_text(item: dict[str, Any]) -> str:
    """「1 Sofa Drape｜2 …」：每个图位是哪个场景（给「已上架的款」记下来，以后分析主图效果）。"""
    return "｜".join(
        f"{f['n']} {f.get('title') or f.get('plan_id') or f.get('role') or ''}".strip()
        for f in item.get("files") or []
    )


def package(ctx: Ctx, item: dict[str, Any], code: str) -> dict[str, Any]:
    """上架图包：<货号>/1.jpg…10.jpg（和以前「店铺轮播图/FLRxxxx/」的结构一样），传 OSS、签链接。"""
    stamp = (item.get("fingerprint") or "")[:8]
    path = ctx.home / EXPORT_DIR / item["asset_id"] / item["set_id"] / f"{code}-{stamp}.zip"
    if not path.is_file():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for f in item.get("files") or []:
                zf.writestr(f"{code}/{f['n']}.jpg", store.ensure_local(ctx, f["path"]).read_bytes())
        with ctx.db.tx() as conn:
            store.save(ctx, conn, path, buf.getvalue())
    key = store.upload_now(ctx, path)
    days = int(ctx.setting("table.link_days", 90))
    return {
        "key": key,
        "url": store.client(ctx).sign_url(
            key, days * 86400, filename=f"{code}_上架图包.zip", fallback=f"{code}.zip"
        ),
        "expires_at": iso(utcnow() + dt.timedelta(days=days)),
    }


def resign(ctx: Ctx, key: str, *, filename: str, fallback: str) -> dict[str, Any]:
    days = int(ctx.setting("table.link_days", 90))
    return {
        "url": store.client(ctx).sign_url(key, days * 86400, filename=filename, fallback=fallback),
        "expires_at": iso(utcnow() + dt.timedelta(days=days)),
    }
