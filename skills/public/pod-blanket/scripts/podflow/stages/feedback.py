"""S11 批次指标与反馈：口径严格按方案「反馈口径」，没有数据记为未知（null），不记 0。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import budget, llm, reasons
from ..context import Ctx
from ..db import log_event, next_id, open_review
from ..util import dumps, loads, now_iso
from .common import get_family, slot_mode
from .plan import _history

# distinct / duplicate 是近重复判定下线前的旧状态，旧批次重算指标时仍会遇到
PASSED_STATES = ("qc_pass", "distinct", "preview_ready", "archived")
EVALUATED_STATES = (*PASSED_STATES, "rejected", "duplicate")


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def batch_metrics(ctx: Ctx, batch_id: str) -> dict[str, Any]:
    assets = [
        dict(r)
        for r in ctx.db.q(
            "SELECT a.* FROM assets a JOIN jobs j ON j.job_id = a.job_id WHERE j.batch_id = ?",
            (batch_id,),
        )
    ]
    leaves = [a for a in assets if a["status"] != "superseded"]
    first_round = [a for a in assets if a["repair_depth"] == 0]
    first_done = [
        a
        for a in first_round
        if (loads(a["qc_json"], {}) or {}).get("decision")
        in ("pass", "repair", "reject", "needs_review")
    ]
    first_pass = [
        a
        for a in first_done
        if (loads(a["qc_json"], {}) or {}).get("model_decision") == "pass"
        and (loads(a["qc_json"], {}) or {}).get("decision") == "pass"
    ]
    evaluated = [a for a in leaves if a["status"] in EVALUATED_STATES]
    passed = [a for a in leaves if a["status"] in PASSED_STATES]
    jobs = [
        r["job_id"]
        for r in ctx.db.q("SELECT job_id FROM jobs WHERE batch_id = ?", (batch_id,))
    ]
    scopes = [*jobs, f"plan:{batch_id}", f"feedback:{batch_id}"]
    marks = ",".join("?" for _ in scopes)
    calls = [
        dict(r)
        for r in ctx.db.q(
            f"SELECT * FROM provider_calls WHERE scope_id IN ({marks})", scopes
        )
    ]
    unsettled = [c for c in calls if c["cost_status"] in ("reserved", "unknown")]
    total_cost = sum(
        (c["actual_cost"] or 0)
        for c in calls
        if c["cost_status"] in ("settled", "estimated", "reconciled")
    )
    reviews = [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM reviews WHERE batch_id = ? AND subject_type = 'asset_selection'",
            (batch_id,),
        )
    ]
    decided = [r for r in reviews if r["status"] == "decided"]
    approved = [r for r in decided if r["decision"] == "approved"]
    # 候选表「没选原因」：人工判了不要的图里写了原因的，按原因计数（一张图可以有几个原因）
    noted = reasons.for_assets(ctx, [a["asset_id"] for a in leaves])
    why = Counter(r for got in noted.values() for r in got)
    return {
        "batch_id": batch_id,
        "assets_total": len(assets),
        "status_counts": dict(Counter(a["status"] for a in assets)),
        "final_assets": len(leaves),
        "note": "assets_total 含被修复版接替（superseded）的旧版本；final_assets 是每条修复链最后一版的数量",
        "first_pass_qc_rate": _ratio(len(first_pass), len(first_done)),
        "first_pass_qc": {"numerator": len(first_pass), "denominator": len(first_done)},
        "qc_pass_candidate_rate": _ratio(len(passed), len(evaluated)),
        "qc_pass_candidate": {
            "numerator": len(passed),
            "denominator": len(evaluated),
        },
        "cost_per_candidate": None
        if (unsettled or not passed or not budget.enforced(ctx.cfg))
        else round(total_cost / len(passed), 6),
        "cost": {
            "settled_total": round(total_cost, 6) if budget.enforced(ctx.cfg) else None,
            "unsettled_calls": len(unsettled),
            "calls": len(calls),
            "currency": ctx.setting("budget.currency", "CNY"),
            "note": "含失败、重试、修复、质检与规划；存在未结算调用时每候选成本记为待对账"
            if budget.enforced(ctx.cfg)
            else "预算控制已关闭：费用未跟踪，只统计调用次数",
        },
        "human_selection_rate": _ratio(len(approved), len(decided)),
        "human_selection": {
            "numerator": len(approved),
            "denominator": len(decided),
            "pending": len(reviews) - len(decided),
            "rejected_with_reason": len(noted),
            "reject_reasons": dict(why.most_common()),
        },
        "market": "未接入市场数据：不计算点击率、转化率与净收益",
    }


def _variant_summaries(ctx: Ctx, batch_id: str) -> list[dict[str, Any]]:
    out = []
    # 只统计出过图的方向：人挑选时没选的候选不建任务
    for v in ctx.db.q(
        "SELECT v.* FROM variants v JOIN jobs j ON j.variant_id = v.variant_id"
        " WHERE v.batch_id = ? ORDER BY v.variant_id",
        (batch_id,),
    ):
        plan = loads(v["plan_json"], {}) or {}
        assets = ctx.db.q(
            "SELECT asset_id, model, slot_id, status, qc_json, human_decision FROM assets"
            " WHERE variant_id = ? AND status != 'superseded'",
            (v["variant_id"],),
        )
        noted = reasons.for_assets(ctx, [a["asset_id"] for a in assets])
        out.append(
            {
                "variant_id": v["variant_id"],
                "title": plan.get("title"),
                # 小变化 small / 大变化 big（三处组合 kind = combo）：反馈可以比较哪类方向更常被选中
                "task_type": plan.get("task_type"),
                "kind": plan.get("kind"),
                "axis": v["axis"],
                "changed_fields": plan.get("changed_fields"),
                "assets": [
                    {
                        "asset_id": a["asset_id"],
                        "model": a["model"],
                        "mode": slot_mode(a["slot_id"]),
                        "status": a["status"],
                        "qc_failures": [
                            c.get("criterion")
                            for c in (
                                (loads(a["qc_json"], {}) or {}).get("result") or {}
                            ).get("checks", [])
                            if c.get("verdict") == "fail"
                        ],
                        # 人工选款：approved / rejected / needs_changes；没选的另带候选表里写的原因
                        "human_decision": a["human_decision"],
                        **(
                            {"reject_reasons": noted[a["asset_id"]]}
                            if a["asset_id"] in noted
                            else {}
                        ),
                    }
                    for a in assets
                ],
            }
        )
    return out


def run_s11(ctx: Ctx, batch_id: str) -> dict[str, Any]:
    batch = ctx.db.q1("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
    fam = get_family(ctx, batch["family_id"], batch["family_version"])
    metrics = batch_metrics(ctx, batch_id)
    reviews = [
        {
            "subject": r["subject_id"],
            "type": r["subject_type"],
            "decision": r["decision"],
            "note": r["note"],
        }
        for r in ctx.db.q(
            "SELECT * FROM reviews WHERE batch_id = ? AND status = 'decided'",
            (batch_id,),
        )
    ]
    _, coverage, _ = _history(ctx, batch["family_id"])
    out = llm.run_stage(
        ctx,
        "P11",
        scope_id=f"feedback:{batch_id}",
        job_id=f"batch:{batch_id}",
        variables={
            "batch_results": {
                "metrics": metrics,
                "variants": _variant_summaries(ctx, batch_id),
            },
            "human_reviews": reviews,
            "optional_market_data": None,
            "coverage_summary": coverage,
            "current_family_versions": [
                {
                    "family_id": f["family_id"],
                    "family_version": f["family_version"],
                    "status": f["status"],
                }
                for f in ctx.db.q(
                    "SELECT family_id, family_version, status FROM families WHERE family_id = ?",
                    (batch["family_id"],),
                )
            ],
        },
    )
    result = out.result
    report = {
        "metrics": metrics,
        "p11": result,
        "model": out.model,
        "prompt_version": out.prompt_version,
        "created_at": now_iso(),
    }
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE batches SET status = 'done', report_json = ?, updated_at = ? WHERE batch_id = ?",
            (dumps(report), now_iso(), batch_id),
        )
        for kind, key in (
            ("technical", "technical_findings"),
            ("human", "human_preference_findings"),
        ):
            if result.get(key):
                conn.execute(
                    "INSERT INTO feedback(feedback_id, batch_id, kind, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                    (
                        next_id(conn, "feedback"),
                        batch_id,
                        kind,
                        dumps(result[key]),
                        now_iso(),
                    ),
                )
        if result.get("requires_approval"):
            open_review(
                conn,
                "proposal",
                batch_id,
                "反馈提出了系列规则 / 方向调整，需要人工批准后另建 family 版本（不直接覆盖已批准规则）",
                {
                    "proposed_family_changes": result.get("proposed_family_changes"),
                    "next_experiments": result.get("next_experiments"),
                },
                batch_id=batch_id,
            )
        log_event(
            conn,
            batch_id,
            "batch_reported",
            {"requires_approval": result.get("requires_approval")},
        )
    return {
        "batch_id": batch_id,
        "family_id": fam["family_id"],
        "metrics": metrics,
        "requires_approval": result.get("requires_approval"),
    }


def finished_batches(ctx: Ctx) -> list[str]:
    """所有任务都已终结、但还没出反馈报告的批次。"""
    rows = ctx.db.q(
        "SELECT b.batch_id FROM batches b WHERE b.status IN ('planned', 'running') AND b.size > 0"
        " AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.batch_id = b.batch_id AND j.status NOT IN ('complete', 'failed'))"
    )
    return [r["batch_id"] for r in rows]
