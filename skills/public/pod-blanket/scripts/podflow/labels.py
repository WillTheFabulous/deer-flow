"""飞书卡片和 POD 对话里的称呼：提到编号时带上名字，和选款表、候选表是同一套写法。

- 任务（参考图）S01：「S01 森林三伙伴水彩 1020」；系列 / 批次 F01 / B008：「S01 森林三伙伴水彩 1020 · B008」
- 素材 IMG00123：「IMG00123_三伙伴开怀大笑」（和候选表「素材」列、下载下来的原图同名）
- 方向 V0012 / 出图任务 J0012：「V0012 三伙伴开怀大笑」（挑选方向时看到的标题）

没起名、找不到所属方向时只有编号。只用来显示：命令参数和按钮回调里仍然只用编号。
"""

from __future__ import annotations

import re
from typing import Any

from .context import Ctx
from .stages.common import subject_label, task_label
from .stages.plan import asset_label, asset_label_of, direction_summary
from .util import loads

# 批量查询时一条语句最多带多少个编号（老版本 SQLite 的变量上限是 999）
BATCH = 500

# 系列规则待审的对象写成「F01@v7」
_FAMILY_VERSION = re.compile(r"(F\d+)@v(\d+)")
_REF = re.compile(r"(IMG|[SFBVJ])\d+")
_FAMILY_JOIN = " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
# 编号 → 所属参考图（任务）
_SEED_SQL = {
    "IMG": "SELECT f.seed_id FROM assets a JOIN variants v ON v.variant_id = a.variant_id"
    + _FAMILY_JOIN
    + " WHERE a.asset_id = ?",
    "V": "SELECT f.seed_id FROM variants v" + _FAMILY_JOIN + " WHERE v.variant_id = ?",
    "J": "SELECT f.seed_id FROM jobs j JOIN variants v ON v.variant_id = j.variant_id"
    + _FAMILY_JOIN
    + " WHERE j.job_id = ?",
    "B": "SELECT f.seed_id FROM batches b JOIN families f"
    " ON f.family_id = b.family_id AND f.family_version = b.family_version"
    " WHERE b.batch_id = ?",
    "F": "SELECT seed_id FROM families WHERE family_id = ?"
    " ORDER BY family_version DESC LIMIT 1",
}


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[i : i + BATCH] for i in range(0, len(ids), BATCH)]


def _titled(ref: str, row: Any) -> str:
    """编号 + 方向标题；没有方向或没有标题时只有编号。"""
    if not row:
        return ref
    title = direction_summary(loads(row["plan_json"], {}) or {}, row["axis"])["title"]
    return f"{ref} {title}" if title else ref


def asset(ctx: Ctx, asset_id: str) -> str:
    """素材「IMG00123_三伙伴开怀大笑」。"""
    return asset_label_of(ctx, asset_id)


def assets(ctx: Ctx, asset_ids: list[str]) -> dict[str, dict[str, str | None]]:
    """一批素材：{编号: {"label": 「IMG…_短语」, "task": 「S01 名字」}}，素材每 BATCH 个一条查询。

    查不到的素材 label 只有编号、task 为 None。
    """
    ids = list(dict.fromkeys(a for a in asset_ids if a))
    out: dict[str, dict[str, str | None]] = {a: {"label": a, "task": None} for a in ids}
    seeds: dict[str, str] = {}
    for chunk in _chunks(ids):
        rows = ctx.db.q(
            "SELECT a.asset_id, v.plan_json, v.axis, f.seed_id FROM assets a"
            " LEFT JOIN variants v ON v.variant_id = a.variant_id"
            " LEFT JOIN families f ON f.family_id = v.family_id"
            " AND f.family_version = v.family_version"
            f" WHERE a.asset_id IN ({','.join('?' for _ in chunk)})",
            tuple(chunk),
        )
        for row in rows:
            seed_id = row["seed_id"]
            if seed_id and seed_id not in seeds:
                seeds[seed_id] = task_label(ctx, seed_id)
            out[row["asset_id"]] = {
                "label": asset_label(
                    row["asset_id"], loads(row["plan_json"], {}) or {}, row["axis"]
                ),
                "task": seeds.get(seed_id) if seed_id else None,
            }
    return out


def direction(ctx: Ctx, variant_id: str) -> str:
    """方向「V0012 三伙伴开怀大笑」。"""
    row = ctx.db.q1(
        "SELECT plan_json, axis FROM variants WHERE variant_id = ?", (variant_id,)
    )
    return _titled(variant_id, row)


def job(ctx: Ctx, job_id: str) -> str:
    """出图任务「J0012 三伙伴开怀大笑」：一个任务出一个方向的图，标题取它的方向。"""
    return jobs(ctx, [job_id]).get(job_id, job_id)


def jobs(ctx: Ctx, job_ids: list[str]) -> dict[str, str]:
    """一批出图任务的称呼，每 BATCH 个一条查询。"""
    ids = list(dict.fromkeys(j for j in job_ids if j))
    out = {j: j for j in ids}
    for chunk in _chunks(ids):
        for row in ctx.db.q(
            "SELECT j.job_id, v.plan_json, v.axis FROM jobs j"
            " JOIN variants v ON v.variant_id = j.variant_id"
            f" WHERE j.job_id IN ({','.join('?' for _ in chunk)})",
            tuple(chunk),
        ):
            out[row["job_id"]] = _titled(row["job_id"], row)
    return out


def ref(ctx: Ctx, ref: str | None) -> str:
    """按编号前缀给称呼：S / F / B 带任务名，IMG 是素材名字，V / J 带方向标题，「F01@v7」写成「… · F01 v7」；
    别的编号（RV、C、SS……）和空串原样返回。"""
    ref = ref or ""
    if re.fullmatch(r"IMG\d+", ref):
        return asset(ctx, ref)
    if re.fullmatch(r"V\d+", ref):
        return direction(ctx, ref)
    if re.fullmatch(r"J\d+", ref):
        return job(ctx, ref)
    match = _FAMILY_VERSION.fullmatch(ref)
    if match:
        return f"{subject_label(ctx, match.group(1))} v{match.group(2)}"
    return subject_label(ctx, ref)


def seed_of(ctx: Ctx, ref: str | None) -> str | None:
    """编号所属的参考图（任务）编号 S01；找不到时为 None。"""
    ref = ref or ""
    match = _FAMILY_VERSION.fullmatch(ref)
    if match:
        ref = match.group(1)
    found = _REF.fullmatch(ref)
    if not found:
        return None
    if found.group(1) == "S":
        return ref
    row = ctx.db.q1(_SEED_SQL[found.group(1)], (ref,))
    return row["seed_id"] if row and row["seed_id"] else None


def task(ctx: Ctx, ref: str | None) -> str | None:
    """编号所属的任务（参考图）「S01 名字」；找不到所属参考图时为 None。"""
    seed_id = seed_of(ctx, ref)
    return task_label(ctx, seed_id) if seed_id else None


def reviews(ctx: Ctx, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """待审列表每一条加 label（审核对象的称呼）与 task（所属任务）：素材批量查，其余逐条查（数量很少）。"""
    found = assets(
        ctx,
        [it["subject_id"] for it in items if it["subject_type"].startswith("asset_")],
    )
    for item in items:
        subject = item["subject_id"]
        if subject in found:
            item.update(found[subject])
        else:
            item["label"] = ref(ctx, subject)
            item["task"] = task(ctx, subject)
    return items
