"""没选原因：候选表里人给没选的图标的原因（多选列「没选原因」），程序只读，给批次反馈和统计用。

- 列（candidates.REASON_FIELD，飞书多选，选项 REASONS）：新表建表时就有（放在「选款结果」后面），之前建的表在
  表格同步时补在最后（候选表列版本 3）。人在飞书里加的选项读回来照存。
- 回读：每次验收（`podctl table accept`、控制行「▶ 选款完成」）读勾选时一起读这一列：这张表里人工判了不要的图
  有原因的写进 selection_reasons，原因清空了（或这张图后来改成了别的决定）的删掉；选完以后再补的原因下次验收时
  读到，`podctl table reasons --refresh` 随时重读。
- 用处：批次反馈（P11）每张图带上 human_decision 与 reject_reasons，批次指标里按原因计数；
  `podctl table reasons` 按原因、方向类型、字段（落在单元上的另按「单元·字段」）汇总。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import fields
from .context import Ctx
from .db import log_event
from .stages.plan import KIND_LABELS
from .util import dumps, loads, now_iso

REASONS = (
    "变化太小",
    "变化太大不像原图",
    "画错变形",
    "画风不统一",
    "配色不好看",
    "不好卖",
    "和别的款重复",
    "其他",
)
# 一个原因最多多少字、一张图最多记几个（人在飞书里加的选项也照存，防超长）
REASON_CHARS = 20
MAX_REASONS = 8
_SCOPE_SQL = (
    " FROM assets a JOIN variants v ON v.variant_id = a.variant_id"
    " JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
)


def normalize(value: Any) -> list[str]:
    """飞书多选格子 → 原因列表（去重保序）；没填的是空列表。"""
    items = value if isinstance(value, list) else [value] if value else []
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name")
        text = str(item or "").strip()[:REASON_CHARS]
        if text and text not in out:
            out.append(text)
    return out[:MAX_REASONS]


def save(ctx: Ctx, seed_id: str, cells: dict[str, Any]) -> dict[str, int]:
    """把一张候选表回读的原因（{record_id: 格子}）写进 selection_reasons：只记人工判了不要的图，
    原因清空了或不再是「不要」的删掉；表里没读到的行不动。返回 {saved, cleared}。"""
    rows = ctx.db.q(
        "SELECT c.asset_id, c.record_id, a.human_decision FROM candidate_rows c"
        " JOIN assets a ON a.asset_id = c.asset_id"
        " WHERE c.seed_id = ? AND c.record_id IS NOT NULL",
        (seed_id,),
    )
    old = {
        r["asset_id"]: loads(r["reasons_json"], []) or []
        for r in ctx.db.q(
            "SELECT asset_id, reasons_json FROM selection_reasons WHERE seed_id = ?",
            (seed_id,),
        )
    }
    saved = cleared = 0
    now = now_iso()
    with ctx.db.tx() as conn:
        for r in rows:
            if r["record_id"] not in cells:
                continue
            got = (
                normalize(cells[r["record_id"]])
                if r["human_decision"] == "rejected"
                else []
            )
            before = old.get(r["asset_id"])
            if got and got != before:
                conn.execute(
                    "INSERT INTO selection_reasons(asset_id, seed_id, record_id, reasons_json, updated_at)"
                    " VALUES(?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET"
                    " seed_id = excluded.seed_id, record_id = excluded.record_id,"
                    " reasons_json = excluded.reasons_json, updated_at = excluded.updated_at",
                    (r["asset_id"], seed_id, r["record_id"], dumps(got), now),
                )
                saved += 1
            elif not got and before is not None:
                conn.execute(
                    "DELETE FROM selection_reasons WHERE asset_id = ?", (r["asset_id"],)
                )
                cleared += 1
        if saved or cleared:
            log_event(
                conn,
                seed_id,
                "selection_reasons_read",
                {"saved": saved, "cleared": cleared},
            )
    return {"saved": saved, "cleared": cleared}


def for_assets(ctx: Ctx, asset_ids: list[str]) -> dict[str, list[str]]:
    """这些图记下的没选原因：{asset_id: [原因]}（没记的不在里面）。"""
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    return {
        r["asset_id"]: loads(r["reasons_json"], []) or []
        for r in ctx.db.q(
            f"SELECT asset_id, reasons_json FROM selection_reasons WHERE asset_id IN ({marks})",
            tuple(ids),
        )
    }


def _top(counter: Counter[str]) -> dict[str, int]:
    return dict(counter.most_common())


def summary(ctx: Ctx, seed_id: str | None = None) -> dict[str, Any]:
    """没选原因汇总（整体或一个任务）：没选了几张、几张写了原因，按原因、方向类型、字段、「单元·字段」计数。"""
    where, params = (" WHERE f.seed_id = ?", (seed_id,)) if seed_id else ("", ())
    rejected = int(
        ctx.db.scalar(
            "SELECT COUNT(*)"
            + _SCOPE_SQL
            + (where + " AND" if where else " WHERE")
            + " a.human_decision = 'rejected'",
            params,
        )
        or 0
    )
    by_reason: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = {}
    by_field: dict[str, Counter[str]] = {}
    by_pair: dict[str, Counter[str]] = {}
    rows = ctx.db.q(
        "SELECT r.reasons_json, v.plan_json"
        + _SCOPE_SQL
        + " JOIN selection_reasons r ON r.asset_id = a.asset_id"
        + where,
        params,
    )
    for row in rows:
        got = loads(row["reasons_json"], []) or []
        plan = loads(row["plan_json"], {}) or {}
        kind = plan.get("kind") or plan.get("task_type") or "未知"
        by_reason.update(got)
        by_kind.setdefault(KIND_LABELS.get(kind, kind), Counter()).update(got)
        for cf in plan.get("changed_fields") or []:
            if not isinstance(cf, dict) or not cf.get("field_id"):
                continue
            label = fields.label(fields.canonical(cf["field_id"]) or cf["field_id"])
            by_field.setdefault(label, Counter()).update(got)
            if cf.get("unit_name") and cf.get("unit_id") not in (None, "canvas"):
                by_pair.setdefault(f"{cf['unit_name']}·{label}", Counter()).update(got)
    return {
        "seed_id": seed_id,
        "rejected": rejected,
        "with_reason": len(rows),
        "by_reason": _top(by_reason),
        "by_kind": {k: _top(v) for k, v in by_kind.items()},
        "by_field": {
            k: _top(v)
            for k, v in sorted(by_field.items(), key=lambda kv: -sum(kv[1].values()))
        },
        "by_pair": {
            k: _top(v)
            for k, v in sorted(by_pair.items(), key=lambda kv: -sum(kv[1].values()))
        },
    }
