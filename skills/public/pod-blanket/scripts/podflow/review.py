"""人工审核入口：列表、裁决、选款拼图、xlsx 选款单导出 / 导入。

方案「人工审核」：素材授权范围、首个系列规则、难修缺陷、结果未知的扣费对账、最终选款都由人决定；
程序只登记、执行裁决的后果（放回队列、释放或确认费用），不替人做决定。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import bg, feishu_table, fixes, images, store, trace
from .context import Ctx
from .db import log_event
from .state import reopen_job
from .stages import seed as seedmod
from .stages.common import (
    MODE_LABELS,
    model_label,
    reference_path,
    slot_mode,
    task_label,
)
from .stages.plan import asset_label, direction_summary
from .util import dumps, loads, now_iso

DECISIONS: dict[str, tuple[str, ...]] = {
    "seed_rights": ("approved", "unknown", "restricted"),
    "seed_intake": ("approved", "rejected"),
    "family": ("approved", "rejected"),
    "asset_qc": ("approved", "rejected", "needs_changes"),
    "asset_selection": ("approved", "rejected", "needs_changes"),
    "call_reconcile": ("charged", "not_charged"),
    "family_exhausted": ("acknowledged",),
    "proposal": ("approved", "rejected"),
    "plan_selection": ("approved", "rejected"),
}

DECISION_HELP = {
    "plan_selection": "approved = 按已勾选的方向确认出图（直接指定用 podctl directions confirm B00x --pick 1,3）；rejected = 这一批都不要（要顺带换一批用 podctl directions reject B00x --replan）",
    "asset_qc": "approved = 质检放行；rejected = 淘汰；needs_changes = 按 --note 定点修复（计入修复次数）",
    "asset_selection": "approved = 选中保留；rejected = 不要；needs_changes = 按 --note 修改这张图，改好的新图回到选款",
    "call_reconcile": "charged = 供应商已扣费（无图）；not_charged = 未扣费，允许重新提交",
}


# 选款「修改」意见的长度上限，飞书卡片输入框用同一个值
FIX_NOTE_CHARS = 300
# 列待审时最多取多少条：要大于待审积压上限（batch.review_backlog_limit，线上 1000），
# 在跑的任务出完图会让积压略超上限，列表、xlsx 导出和工作台待审页都不能在这之前截断
LIST_LIMIT = 2000


class ReviewError(RuntimeError):
    pass


def list_reviews(
    ctx: Ctx,
    *,
    status: str = "open",
    types: list[str] | None = None,
    batch_id: str | None = None,
    limit: int = LIST_LIMIT,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reviews WHERE 1=1"
    params: list[Any] = []
    if status != "all":
        sql += " AND status = ?"
        params.append(status)
    if types:
        sql += f" AND subject_type IN ({','.join('?' for _ in types)})"
        params += types
    if batch_id:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    sql += " ORDER BY created_at, review_id LIMIT ?"
    params.append(limit)
    out = []
    for row in ctx.db.q(sql, tuple(params)):
        item = dict(row)
        item["payload"] = loads(item.pop("payload_json"))
        item["allowed_decisions"] = DECISIONS.get(item["subject_type"], ())
        if item["subject_type"].startswith("asset_"):
            asset = ctx.db.q1(
                "SELECT model, status, variant_id, thumb_path FROM assets WHERE asset_id = ?",
                (item["subject_id"],),
            )
            if asset:
                item["asset"] = dict(asset)
        out.append(item)
    return out


def _close(
    conn, review: dict[str, Any], decision: str, by: str, note: str | None
) -> None:
    conn.execute(
        "UPDATE reviews SET status = 'decided', decision = ?, decided_by = ?, decided_at = ?, note = ? WHERE review_id = ?",
        (decision, by, now_iso(), note, review["review_id"]),
    )
    log_event(
        conn,
        review["subject_id"],
        "review_decided",
        {"review_id": review["review_id"], "decision": decision, "by": by},
    )
    if review["subject_type"] in ("asset_selection", "asset_qc"):
        trace.record(
            conn,
            review["subject_id"],
            "select" if review["subject_type"] == "asset_selection" else "qc",
            to=decision,
            actor=by,
            reason=note,
            payload={"review_id": review["review_id"]},
        )


def _proposal_revision(conn, review: dict[str, Any]) -> tuple[str, str] | None:
    """把批次反馈里的系列修改提案整理成 family revise 的修订意见：(family_id, 意见)。"""
    payload = loads(review["payload_json"], {}) or {}
    changes = [
        c
        for c in payload.get("proposed_family_changes") or []
        if isinstance(c, dict) and c.get("change")
    ]
    family_id = next((c["family_id"] for c in changes if c.get("family_id")), None)
    if not family_id and review.get("batch_id"):
        row = conn.execute(
            "SELECT family_id FROM batches WHERE batch_id = ?", (review["batch_id"],)
        ).fetchone()
        family_id = row["family_id"] if row else None
    changes = [c for c in changes if c.get("family_id") in (None, family_id)]
    if not changes or not family_id:
        return None
    lines = [f"按批次 {review['subject_id']} 反馈的方向提案修订（人工已采纳）："]
    for i, c in enumerate(changes, 1):
        reason = f"（依据：{c['rationale']}）" if c.get("rationale") else ""
        lines.append(f"{i}. {c['change']}{reason}")
    return family_id, "\n".join(lines)


def decide(
    ctx: Ctx, review_id: str, decision: str, *, by: str, note: str | None = None
) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM reviews WHERE review_id = ?", (review_id,))
    if not row:
        raise ReviewError(f"没有审核项 {review_id}")
    review = dict(row)
    if review["status"] != "open":
        raise ReviewError(f"{review_id} 已是 {review['status']}")
    kind = review["subject_type"]
    allowed = DECISIONS.get(kind, ())
    if decision not in allowed:
        raise ReviewError(
            f"{kind} 只接受 {allowed}"
            + (f"（{DECISION_HELP[kind]}）" if kind in DECISION_HELP else "")
        )
    subject = review["subject_id"]
    effect: dict[str, Any] = {}
    revision: tuple[str, str] | None = None
    fix_asset: str | None = None

    if kind == "seed_rights":
        seedmod.set_rights(ctx, subject, decision, by=by, note=note)
        return {
            "review_id": review_id,
            "decision": decision,
            "effect": {"rights_status": decision},
        }
    if kind == "family":
        family_id = subject.split("@", 1)[0]
        res = seedmod.decide_family(
            ctx, family_id, approve=decision == "approved", by=by, note=note
        )
        return {"review_id": review_id, "decision": decision, "effect": res}
    if kind == "plan_selection":
        from . import directions  # directions 依赖阶段模块，放到用时再导入

        if decision == "approved":
            res = directions.confirm(ctx, subject, by=by, start=True)
        else:
            res = directions.reject(ctx, subject, by=by)
        return {
            "review_id": review_id,
            "subject_type": kind,
            "subject_id": subject,
            "decision": decision,
            "effect": res,
        }

    with ctx.db.tx() as conn:
        if kind == "seed_intake":
            if decision == "approved":
                conn.execute(
                    "UPDATE seeds SET status = 'ingested', updated_at = ? WHERE seed_id = ?",
                    (now_iso(), subject),
                )
            effect = {
                "seed_status": "ingested" if decision == "approved" else "unchanged"
            }
        elif kind in ("asset_qc", "asset_selection"):
            asset = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (subject,)
            ).fetchone()
            if not asset:
                raise ReviewError(f"没有素材 {subject}")
            if kind == "asset_qc":
                if decision == "approved":
                    status = "qc_pass"
                elif decision == "rejected":
                    status = "rejected"
                else:
                    if not note:
                        raise ReviewError("needs_changes 需要用 --note 写清楚要修什么")
                    status = "repair_pending"
                    conn.execute(
                        "UPDATE assets SET repair_instructions_json = ? WHERE asset_id = ?",
                        (dumps([note]), subject),
                    )
                conn.execute(
                    "UPDATE assets SET status = ?, updated_at = ? WHERE asset_id = ?",
                    (status, now_iso(), subject),
                )
                if status != "rejected":
                    reopen_job(conn, asset["job_id"], f"人工质检裁决 {decision}")
                effect = {"asset_status": status}
            elif decision == "needs_changes":
                note = (note or "").strip()
                if not note:
                    raise ReviewError("needs_changes 需要用 --note 写清楚要改什么")
                if len(note) > FIX_NOTE_CHARS:
                    raise ReviewError(
                        f"修改意见最多 {FIX_NOTE_CHARS} 字，现在 {len(note)} 字"
                    )
                if asset["status"] != "archived":
                    raise ReviewError(f"{subject} 当前状态 {asset['status']}，不能修改")
                voided = _void_failed_repairs(
                    ctx, conn, subject, by=by, reason="人工重新要求修改"
                )
                conn.execute(
                    "UPDATE assets SET status = 'repair_pending', human_decision = 'needs_changes',"
                    " repair_instructions_json = ?, updated_at = ? WHERE asset_id = ?",
                    (dumps([note]), now_iso(), subject),
                )
                reopen_job(conn, asset["job_id"], f"人工要求修改 {subject}")
                fix_asset = subject
                effect = {"asset_status": "repair_pending", "voided_calls": voided}
            else:
                conn.execute(
                    "UPDATE assets SET human_decision = ?, updated_at = ? WHERE asset_id = ?",
                    (decision, now_iso(), subject),
                )
                effect = {"human_decision": decision}
        elif kind == "call_reconcile":
            call = conn.execute(
                "SELECT * FROM provider_calls WHERE call_id = ?", (subject,)
            ).fetchone()
            if not call:
                raise ReviewError(f"没有调用 {subject}")
            meta = loads(call["request_meta_json"], {}) or {}
            if decision == "not_charged":
                conn.execute(
                    "UPDATE provider_calls SET cost_status = 'released', actual_cost = 0, status = 'failed',"
                    " error = COALESCE(error, '') || ' | 人工对账：未扣费' WHERE call_id = ?",
                    (subject,),
                )
                if call["stage"] == "T01" and meta.get("slot_id"):
                    conn.execute(
                        "UPDATE job_slots SET status = 'pending', updated_at = ? WHERE job_id = ? AND slot_id = ? AND status = 'unknown'",
                        (now_iso(), call["scope_id"], meta["slot_id"]),
                    )
                if call["stage"] == "T01R" and meta.get("asset_hint"):
                    conn.execute(
                        "UPDATE assets SET status = 'repair_pending', updated_at = ? WHERE asset_id = ? AND status = 'repair_unknown'",
                        (now_iso(), meta["asset_hint"]),
                    )
                reopen_job(conn, call["scope_id"], "对账：未扣费，允许重新提交")
            else:
                conn.execute(
                    "UPDATE provider_calls SET cost_status = 'reconciled', actual_cost = reserved_cost,"
                    " error = COALESCE(error, '') || ' | 人工对账：已扣费、无结果' WHERE call_id = ?",
                    (subject,),
                )
                if call["stage"] == "T01" and meta.get("slot_id"):
                    conn.execute(
                        "UPDATE job_slots SET status = 'failed', last_error = '已扣费但未拿到图', updated_at = ?"
                        " WHERE job_id = ? AND slot_id = ? AND status = 'unknown'",
                        (now_iso(), call["scope_id"], meta["slot_id"]),
                    )
                if call["stage"] == "T01R" and meta.get("asset_hint"):
                    target = conn.execute(
                        "SELECT human_decision, qc_json FROM assets WHERE asset_id = ? AND status = 'repair_unknown'",
                        (meta["asset_hint"],),
                    ).fetchone()
                    if target and target["human_decision"] == "needs_changes":
                        # 人工要求的修改没拿到图：原图放回选款，而不是淘汰
                        qc = loads(target["qc_json"], {}) or {}
                        qc["fix_failed"] = "修改请求已扣费但没拿到图"
                        conn.execute(
                            "UPDATE assets SET status = 'preview_ready', qc_json = ?, updated_at = ? WHERE asset_id = ?",
                            (dumps(qc), now_iso(), meta["asset_hint"]),
                        )
                        reopen_job(
                            conn, call["scope_id"], "对账：修改没拿到图，原图放回选款"
                        )
                    elif target:
                        conn.execute(
                            "UPDATE assets SET status = 'rejected', updated_at = ? WHERE asset_id = ?",
                            (now_iso(), meta["asset_hint"]),
                        )
            effect = {
                "cost_status": "released" if decision == "not_charged" else "reconciled"
            }
        elif kind == "proposal" and decision == "approved":
            revision = _proposal_revision(conn, review)
            effect = {"note": "已记录（提案里没有可执行的系列修改）"}
        else:
            effect = {"note": "已记录"}
        _close(conn, review, decision, by, note)
    if kind == "proposal" and decision == "approved" and revision:
        family_id, revision_note = revision
        task = bg.spawn(
            ctx,
            ["family", "revise", family_id, "--note", revision_note],
            kind="revise",
            subject=family_id,
            notify=True,
        )
        effect = {"revise_started": task["task_id"], "family_id": family_id}
    if fix_asset or (kind == "call_reconcile" and decision == "not_charged"):
        effect.update(fixes.start(ctx, subject=fix_asset))
    # 选中的进选款表；无论怎么定，候选表上的勾选框和状态都要跟着变
    if kind == "asset_selection":
        task = feishu_table.kick(ctx)
        if task:
            effect["table_sync"] = task["task_id"]
    return {
        "review_id": review_id,
        "subject_type": kind,
        "subject_id": subject,
        "decision": decision,
        "effect": effect,
    }


def requeue_asset(
    ctx: Ctx, asset_id: str, stage: str, *, by: str, note: str
) -> dict[str, Any]:
    """运维纠错：程序缺陷导致的误判 / 失败，人工把素材放回某一步重跑（全程留痕）。

    - qc：放回质检（例如质检口径修正后重新判定）；
    - repair：放回定点修复；之前因程序缺陷失败的修复调用标记为作废（voided），不再计入尝试次数。
    """
    if stage not in ("qc", "repair"):
        raise ReviewError("stage 只能是 qc 或 repair")
    with ctx.db.tx() as conn:
        asset = conn.execute(
            "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if not asset:
            raise ReviewError(f"没有素材 {asset_id}")
        voided: list[str] = []
        if stage == "qc":
            if asset["status"] not in ("review_qc", "qc_pass", "rejected", "generated"):
                raise ReviewError(
                    f"{asset_id} 当前状态 {asset['status']}，不能放回质检"
                )
            conn.execute(
                "UPDATE assets SET status = 'generated', updated_at = ? WHERE asset_id = ?",
                (now_iso(), asset_id),
            )
            conn.execute(
                "UPDATE reviews SET status = 'dismissed', decided_by = ?, decided_at = ?, note = ?"
                " WHERE subject_type = 'asset_qc' AND subject_id = ? AND status = 'open'",
                (by, now_iso(), f"放回质检重判：{note}", asset_id),
            )
        else:
            if not asset["repair_instructions_json"]:
                raise ReviewError(f"{asset_id} 没有修复说明，不能放回修复")
            if asset["status"] not in ("rejected", "repair_unknown", "repair_pending"):
                raise ReviewError(
                    f"{asset_id} 当前状态 {asset['status']}，不能放回修复"
                )
            voided = _void_failed_repairs(ctx, conn, asset_id, by=by, reason=note)
            conn.execute(
                "UPDATE assets SET status = 'repair_pending', updated_at = ? WHERE asset_id = ?",
                (now_iso(), asset_id),
            )
        reopen_job(conn, asset["job_id"], f"人工放回 {stage}：{note}")
        log_event(
            conn,
            asset_id,
            "asset_requeued",
            {"stage": stage, "by": by, "note": note, "voided_calls": voided},
        )
    return {
        "asset_id": asset_id,
        "stage": stage,
        "voided_calls": voided,
        "job_id": asset["job_id"],
    }


def _void_failed_repairs(
    ctx: Ctx, conn, asset_id: str, *, by: str, reason: str
) -> list[str]:
    """把这张图失败过的修复调用标记作废，不再计入尝试次数；有结果未知的调用就拒绝（先对账）。"""
    voided: list[str] = []
    for call in conn.execute(
        "SELECT call_id, status, request_meta_json FROM provider_calls WHERE stage = 'T01R'"
        f" AND {ctx.db.json_text('request_meta_json', 'asset_hint')} = ?",
        (asset_id,),
    ).fetchall():
        if call["status"] in ("reserved", "submitted", "unknown"):
            raise ReviewError(f"{call['call_id']} 结果未知，先用 podctl reconcile 对账")
        meta = loads(call["request_meta_json"], {}) or {}
        if call["status"] == "failed" and not meta.get("voided"):
            meta.update({"voided": True, "void_reason": reason, "voided_by": by})
            conn.execute(
                "UPDATE provider_calls SET request_meta_json = ? WHERE call_id = ?",
                (dumps(meta), call["call_id"]),
            )
            voided.append(call["call_id"])
    return voided


def find_review(ctx: Ctx, subject_type: str, subject_id: str) -> str | None:
    row = ctx.db.q1(
        "SELECT review_id FROM reviews WHERE subject_type = ? AND subject_id = ? AND status = 'open'",
        (subject_type, subject_id),
    )
    return row["review_id"] if row else None


def _sheet_assets(
    ctx: Ctx, *, batch_id: str | None, review_type: str, asset_ids: list[str] | None
) -> list[dict[str, Any]]:
    if asset_ids:
        marks = ",".join("?" for _ in asset_ids)
        return [
            dict(r)
            for r in ctx.db.q(
                f"SELECT * FROM assets WHERE asset_id IN ({marks}) ORDER BY asset_id",
                tuple(asset_ids),
            )
        ]
    sql = (
        "SELECT a.* FROM assets a JOIN reviews r ON r.subject_id = a.asset_id"
        " WHERE r.status = 'open' AND r.subject_type = ?"
    )
    params: list[Any] = [review_type]
    if batch_id:
        sql += " AND r.batch_id = ?"
        params.append(batch_id)
    return [dict(r) for r in ctx.db.q(sql + " ORDER BY a.asset_id", tuple(params))]


def contact_sheet(
    ctx: Ctx,
    out: Path,
    *,
    batch_id: str | None = None,
    review_type: str = "asset_selection",
    asset_ids: list[str] | None = None,
) -> dict[str, Any]:
    assets = _sheet_assets(
        ctx, batch_id=batch_id, review_type=review_type, asset_ids=asset_ids
    )
    if not assets:
        raise ReviewError("没有符合条件的素材可以拼图")
    meta = _sheet_meta(ctx, [a["asset_id"] for a in assets])
    # 按参考图分组、组内按方向和出图顺序，每组第一格放原图
    assets.sort(
        key=lambda a: (
            meta[a["asset_id"]]["seed_id"] or "",
            a["variant_id"] or "",
            a["asset_id"],
        )
    )
    shown = assets[:24]
    font = images.label_font(ctx.setting("review.sheet_font"))
    tiles: list[dict[str, Any]] = []
    items: list[tuple[bytes, list[str]]] = []
    seen_seeds: set[str] = set()
    for a in shown:
        info = meta[a["asset_id"]]
        seed_id = info["seed_id"]
        if seed_id and info["original_path"] and seed_id not in seen_seeds:
            seen_seeds.add(seed_id)
            # 第一格放这张参考图：提取过平面图案就放图案（候选是照它出的）
            ref = reference_path(
                ctx, {"seed_id": seed_id, "original_path": info["original_path"]}
            )
            pattern = ref != info["original_path"]
            try:
                original = store.ensure_local(ctx, ref).read_bytes()
            except (OSError, store.StorageError):
                original = None
            if original:
                word = (
                    ("图案" if font else "pattern")
                    if pattern
                    else ("原图" if font else "original")
                )
                name = task_label(ctx, seed_id)
                # 没有中文字体时只写 ASCII 的编号
                lines = [f"{word} {name if font else seed_id}"]
                tiles.append(
                    {"kind": "original", "id": seed_id, "label": name, "lines": lines}
                )
                items.append((original, lines))
        model, mode = model_label(a["model"]), slot_mode(a["slot_id"])
        if font:
            direction = " ".join(p for p in (a["variant_id"], info["title"]) if p)
            lines = [
                f"{a['asset_id']} · {direction}" if direction else a["asset_id"],
                f"{model} · {MODE_LABELS[mode]}",
            ]
        else:
            lines = [
                f"{a['asset_id']}  {a['variant_id'] or ''}".strip(),
                f"{model} {mode}",
            ]
        tiles.append(
            {
                "kind": "candidate",
                "id": a["asset_id"],
                "label": info["label"],
                "lines": lines,
            }
        )
        thumb = store.ensure_local(ctx, a["thumb_path"] or a["path"]).read_bytes()
        items.append((thumb, lines))
    data = images.contact_sheet(items, font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    reports = ctx.home / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    copy = reports / out.name
    if copy.resolve() != out.resolve():
        copy.write_bytes(data)
    return {
        "out": str(out),
        "assets": [a["asset_id"] for a in shown],
        "originals": [t["id"] for t in tiles if t["kind"] == "original"],
        "tiles": tiles,
        "truncated": max(len(assets) - 24, 0),
    }


def _sheet_meta(ctx: Ctx, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """拼图每张候选的参考图（原图）与方向标题。"""
    marks = ",".join("?" for _ in asset_ids)
    rows = ctx.db.q(
        "SELECT a.asset_id, v.axis, v.plan_json, f.seed_id, s.original_path FROM assets a"
        " LEFT JOIN variants v ON v.variant_id = a.variant_id"
        " LEFT JOIN families f ON f.family_id = v.family_id AND f.family_version = v.family_version"
        " LEFT JOIN seeds s ON s.seed_id = f.seed_id"
        f" WHERE a.asset_id IN ({marks})",
        tuple(asset_ids),
    )
    return {
        r["asset_id"]: {
            "seed_id": r["seed_id"],
            "original_path": r["original_path"],
            "title": direction_summary(loads(r["plan_json"], {}) or {}, r["axis"])[
                "title"
            ],
            "label": asset_label(
                r["asset_id"], loads(r["plan_json"], {}) or {}, r["axis"]
            ),
        }
        for r in rows
    }


EXPORT_COLUMNS = [
    "review_id",
    "asset_id",
    "variant_id",
    "model",
    "status",
    "changed_fields",
    "qc",
    "mode",
    "thumbnail",
    "decision",
    "note",
]


def export_xlsx(ctx: Ctx, out: Path, *, batch_id: str | None = None) -> dict[str, Any]:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.worksheet.datavalidation import DataValidation

    reviews = list_reviews(ctx, types=["asset_selection"], batch_id=batch_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "选款"
    ws.append(EXPORT_COLUMNS)
    dv = DataValidation(
        type="list", formula1='"approved,rejected,needs_changes"', allow_blank=True
    )
    ws.add_data_validation(dv)
    for idx, review in enumerate(reviews, start=2):
        asset = dict(
            ctx.db.q1(
                "SELECT * FROM assets WHERE asset_id = ?", (review["subject_id"],)
            )
        )
        plan = (
            loads(
                ctx.db.scalar(
                    "SELECT plan_json FROM variants WHERE variant_id = ?",
                    (asset["variant_id"],),
                ),
                {},
            )
            or {}
        )
        ws.append(
            [
                review["review_id"],
                asset["asset_id"],
                asset["variant_id"],
                asset["model"],
                asset["status"],
                dumps(plan.get("changed_fields")),
                (loads(asset["qc_json"], {}) or {}).get("decision"),
                MODE_LABELS[slot_mode(asset["slot_id"])],
                "",
                "",
                "",
            ]
        )
        dv.add(f"J{idx}")
        thumb_path = store.ensure_local(ctx, asset["thumb_path"] or asset["path"])
        img = XLImage(str(thumb_path))
        img.width, img.height = 160, 200
        ws.add_image(img, f"I{idx}")
        ws.row_dimensions[idx].height = 155
    ws.column_dimensions["I"].width = 24
    ws.column_dimensions["F"].width = 40
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"out": str(out), "rows": len(reviews)}


def import_xlsx(ctx: Ctx, path: Path, *, by: str) -> dict[str, Any]:
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb["选款"] if "选款" in wb.sheetnames else wb.active
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    col = {
        name: header.index(name)
        for name in ("review_id", "decision", "note")
        if name in header
    }
    if len(col) < 2:
        raise ReviewError("表头缺少 review_id / decision")
    applied, skipped = [], []
    for row in ws.iter_rows(min_row=2, values_only=True):
        review_id = row[col["review_id"]]
        decision = (row[col["decision"]] or "").strip() if row[col["decision"]] else ""
        note = row[col["note"]] if "note" in col else None
        if not review_id or not decision:
            continue
        try:
            applied.append(decide(ctx, str(review_id), decision, by=by, note=note))
        except ReviewError as exc:
            skipped.append({"review_id": review_id, "error": str(exc)})
    return {"applied": len(applied), "skipped": skipped}
