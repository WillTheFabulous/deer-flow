"""母版阶段：S01 接入 → S01P 图案提取（参考图 2D 化）→ S02 14 维拆解 → S02D 画面完整描述 → S03 吸引力假设
→ S04 系列规则（待人工批准）。

S01N 任务命名和 S01 同时跑（另一个线程）：模型看原图写一个简短描述，接上登记那天的月日作为任务名字。
S02D 见 stages/profile.py：没写成不挡拆解，之后用拆解简版兜底。
"""

from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import bg, feishu_table, fields, imagegen, images, llm, store
from ..context import Ctx
from ..db import log_event, next_id, next_seq, open_review
from ..paths import REFERENCES_DIR
from ..util import (
    clip,
    dumps,
    loads,
    local_day,
    now_iso,
    parse_iso,
    sha256_bytes,
    sha256_text,
)
from .common import (
    EVIDENCE_TYPES,
    INTAKE_OFF_NOTE,
    MODE_LABELS,
    StageError,
    fanout_models,
    generation_modes,
    get_family,
    get_seed,
    intake_review_enabled,
    latest_analysis,
    original_bytes,
    pattern_path,
    seed_bytes,
    seed_name,
    vision_image,
)
from .profile import (
    DESCRIBE_STAGE,
    describe_enabled,
    describe_quietly,
    get_description,
    reference_profile,
)

RIGHTS = ("approved", "unknown", "restricted")
# 可变字段清单里的当前取值截到多少字（给 P04 / 挑选卡看个大概）
VARIATION_VALUE_CHARS = 60
# 拆解之后字段字典把字段改成了只记录：读旧拆解时不再当可变字段
RECORD_ONLY_REASON = "字段字典已改为只记录，不单独出方向"
# 系列规则里锁定字段的来源只认这几种（用户定位、修订意见、印制要求）；设计建议不锁
LOCK_SOURCES = ("user_positioning", "revision_note", "production_constraints")
# 参考图 2D 化（S01P）：提示词照抄 Pattern DNA，存在 references/ 里；重提取时人的补充要求最多多少字
PATTERN_STAGE = "S01P"
PATTERN_PROMPT_FILE = "flatten_textile.md"
PATTERN_HINT_CHARS = 200
# 任务命名（S01N）用的提示词；描述去掉标点和空格后的字数范围（提示词要 4–8 个汉字）
NAME_PROMPT = "P12"
NAME_DESC_MIN = 2
NAME_DESC_MAX = 10
# 母版各步在进度里的名字；序号按实际要跑的步数算（开着图案提取、画面描述、拆解后自动策划时是 7 步）
STEP_NAMES = {
    "S01": "S01 参考图接入",
    PATTERN_STAGE: "S01P 图案提取",
    "S02": "S02 14 维拆解",
    DESCRIBE_STAGE: "S02D 画面描述",
    "S03": "S03 吸引力假设",
    "S04": "S04 系列规则",
    "S05": "S05 裂变策划",
}


def add_seed(
    ctx: Ctx,
    src: Path,
    *,
    source_ref: str | None = None,
    source_note: str | None = None,
    rights: str = "unknown",
    sales_evidence: str | None = None,
    positioning: str | None = None,
) -> dict[str, Any]:
    if rights not in RIGHTS:
        raise StageError(f"rights 只能是 {RIGHTS}")
    data = src.read_bytes()
    fmt, width, height = images.probe(data)
    sha = sha256_bytes(data)
    existing = ctx.db.q1("SELECT seed_id FROM seeds WHERE sha256 = ?", (sha,))
    if existing:
        return {
            "seed_id": existing["seed_id"],
            "created": False,
            "note": "同一文件已登记过（按 sha256 判定）",
        }
    ext = images.FORMAT_EXT.get(fmt, "png")
    now = now_iso()
    with ctx.db.tx() as conn:
        seed_id = next_id(conn, "seed")
        path = store.save(
            ctx, conn, store.seed_dir(ctx.home, seed_id) / f"original.{ext}", data
        )
        conn.execute(
            "INSERT INTO seeds(seed_id, sha256, original_path, mime, width, height, bytes, source_ref, source_note,"
            " rights_status, sales_evidence, user_positioning, status, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
            (
                seed_id,
                sha,
                store.rel(ctx.home, path),
                images.FORMAT_MIME.get(fmt, "image/png"),
                width,
                height,
                len(data),
                source_ref or src.name,
                source_note,
                rights,
                sales_evidence,
                positioning,
                now,
                now,
            ),
        )
        log_event(
            conn,
            seed_id,
            "seed_added",
            {"sha256": sha, "rights": rights, "source_ref": source_ref or src.name},
        )
    # 授权未知与已获授权效果相同，不开待审；受限时由人在对话里登记 restricted
    store.kick(ctx)
    return {
        "seed_id": seed_id,
        "created": True,
        "sha256": sha,
        "width": width,
        "height": height,
        "rights_status": rights,
    }


def set_rights(
    ctx: Ctx, seed_id: str, status: str, *, by: str, note: str | None = None
) -> dict[str, Any]:
    if status not in RIGHTS:
        raise StageError(f"status 只能是 {RIGHTS}")
    get_seed(ctx, seed_id)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE seeds SET rights_status = ?, rights_confirmed_by = ?, rights_note = ?, updated_at = ? WHERE seed_id = ?",
            (status, by, note, now_iso(), seed_id),
        )
        conn.execute(
            "UPDATE reviews SET status = 'decided', decision = ?, decided_by = ?, decided_at = ?, note = ?"
            " WHERE subject_type = 'seed_rights' AND subject_id = ? AND status = 'open'",
            (status, by, now_iso(), note, seed_id),
        )
        log_event(
            conn, seed_id, "seed_rights_set", {"status": status, "by": by, "note": note}
        )
    return {"seed_id": seed_id, "rights_status": status, "by": by}


def _save_seed(ctx: Ctx, seed_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE seeds SET {sets}, updated_at = ? WHERE seed_id = ?",
            (*fields.values(), now_iso(), seed_id),
        )


def run_s01(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    seed = get_seed(ctx, seed_id)
    data = original_bytes(ctx, seed)
    out = llm.run_stage(
        ctx,
        "P01",
        scope_id=f"seed:{seed_id}",
        job_id=f"seed:{seed_id}",
        variables={
            "source_metadata": {
                "seed_id": seed_id,
                "sha256": seed["sha256"],
                "width": seed["width"],
                "height": seed["height"],
                "source_ref": seed["source_ref"],
                "source_note": seed["source_note"],
                "rights_status_recorded_by_human": seed["rights_status"],
                "sales_evidence": seed["sales_evidence"] or "未知",
            },
            "user_constraints": {
                "project": ctx.setting("project.user_constraints", []),
                "seed_positioning": seed["user_positioning"],
            },
        },
        images={
            "source_images": [
                vision_image(
                    ctx, data, f"{seed_id} 原图（{seed['width']}x{seed['height']}）"
                )
            ]
        },
    )
    result = out.result
    result["seed_id"] = seed_id
    # 授权状态只认人工登记，模型从画面得出的判断不作数
    result["model_rights_guess"] = result.get("rights_status")
    result["rights_status"] = seed["rights_status"]
    result["source_ref"] = seed["source_ref"] or seed["original_path"]
    record = {
        "result": result,
        "status": out.envelope["status"],
        "unknowns": out.envelope["unknowns"],
        "next_action": out.envelope["next_action"],
        "model": out.model,
        "prompt_version": out.prompt_version,
        "call_ids": out.call_ids,
    }
    concern = result["readability"] == "unusable" or out.envelope["status"] in (
        "needs_review",
        "blocked",
    )
    review_note = f"参考图可读性 {result['readability']}：{out.envelope['next_action']}"
    if seed["rights_status"] == "restricted":
        status = "blocked"
    elif concern and intake_review_enabled(ctx):
        status = "needs_review"
    else:
        status = "ingested"
        if concern:
            # 接入确认冻结时不停下，模型原本要人确认的理由留在接入记录里
            record["intake_review"] = {
                "skipped": True,
                "reason": INTAKE_OFF_NOTE,
                "would_review": review_note,
            }
    _save_seed(ctx, seed_id, seed_record_json=dumps(record), status=status)
    if status != "ingested":
        with ctx.db.tx() as conn:
            open_review(
                conn,
                "seed_intake",
                seed_id,
                "素材受限，隔离等待确认" if status == "blocked" else review_note,
                {"unknowns": out.envelope["unknowns"]},
            )
    elif "intake_review" in record:
        with ctx.db.tx() as conn:
            log_event(
                conn,
                seed_id,
                "intake_review_skipped",
                {"readability": result["readability"], "note": review_note},
            )
    return {
        "seed_id": seed_id,
        "status": status,
        "seed_record": result,
        "unknowns": out.envelope["unknowns"],
        **(
            {"intake_review": record["intake_review"]}
            if "intake_review" in record
            else {}
        ),
    }


def _release_intake(ctx: Ctx, seed: dict[str, Any]) -> dict[str, Any] | None:
    """接入确认冻结后，放行停在「参考图接入」待确认的参考图：关掉开着的接入待审，状态改回 ingested。

    只在开关关着、参考图是 needs_review 时做；授权受限（blocked）不动。返回关了几条待审，没做事返回 None。
    """
    if intake_review_enabled(ctx) or seed["status"] != "needs_review":
        return None
    now = now_iso()
    with ctx.db.tx() as conn:
        closed = conn.execute(
            "UPDATE reviews SET status = 'dismissed', decided_by = 'system', decided_at = ?, note = ?"
            " WHERE subject_type = 'seed_intake' AND subject_id = ? AND status = 'open'",
            (now, INTAKE_OFF_NOTE, seed["seed_id"]),
        ).rowcount
        conn.execute(
            "UPDATE seeds SET status = 'ingested', updated_at = ? WHERE seed_id = ?"
            " AND status = 'needs_review'",
            (now, seed["seed_id"]),
        )
        log_event(
            conn,
            seed["seed_id"],
            "intake_released",
            {"reviews": closed, "note": INTAKE_OFF_NOTE},
        )
    return {"reviews": closed, "note": INTAKE_OFF_NOTE}


def pattern_enabled(ctx: Ctx) -> bool:
    return bool(ctx.setting("pattern_extract.enabled", True))


def step_label(ctx: Ctx, stage: str) -> str:
    """进度里的「S02 14 维拆解（3/7）」：序号按这次要跑的母版步骤（含图案提取、画面描述、自动策划）算。"""
    stages = (
        ["S01"]
        + ([PATTERN_STAGE] if pattern_enabled(ctx) else [])
        + ["S02"]
        + ([DESCRIBE_STAGE] if describe_enabled(ctx) else [])
        + ["S03", "S04"]
    )
    if ctx.setting("batch.auto_plan_after_analyze", True):
        stages.append("S05")
    if stage not in stages:
        return STEP_NAMES[stage]
    return f"{STEP_NAMES[stage]}（{stages.index(stage) + 1}/{len(stages)}）"


def pattern_prompt(hint: str | None = None) -> str:
    """S01P 的图生图提示词（references/flatten_textile.md 去掉注释）；重提取时附上人的补充要求。"""
    text = (REFERENCES_DIR / PATTERN_PROMPT_FILE).read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    if hint:
        text += f"\nAdditional requirement from the user (may be written in Chinese): {hint}"
    return text


def extract_pattern(
    ctx: Ctx, seed_id: str, *, hint: str | None = None
) -> dict[str, Any]:
    """S01P 参考图 2D 化：把参考图里的印花用图生图提取成干净的平面图案（照搬 Pattern DNA 的预处理）。

    输入原图压成最长边 pattern_extract.input_max_side 的 JPEG，按 pattern_extract.models 依次尝试，前一个失败再换下一个；
    出图参数走 format_profile 的 gpt 参数（4:5）。成功后存 assets/seeds/<S>/pattern-<n>.<ext>，记进 seed_patterns，
    之后拆解、图生图、质检都读这张图案（stages.common.reference_path）。全部失败抛 StageError，再跑从这一步接着做。
    """
    seed = get_seed(ctx, seed_id)
    if seed["rights_status"] == "restricted" or seed["status"] == "blocked":
        raise StageError(f"{seed_id} 素材受限：不提取图案（不能把参考图发给生图模型）")
    if not seed["seed_record_json"]:
        raise StageError(f"{seed_id} 还没做 S01 接入")
    hint = " ".join((hint or "").split()) or None
    if hint and len(hint) > PATTERN_HINT_CHARS:
        raise StageError(f"补充要求最多 {PATTERN_HINT_CHARS} 个字，现在 {len(hint)} 个")
    models = [str(m) for m in ctx.setting("pattern_extract.models") or [] if m]
    if not models:
        raise StageError("pattern_extract.models 没有配置模型")
    source = images.to_vision_jpeg(
        original_bytes(ctx, seed),
        int(ctx.setting("pattern_extract.input_max_side", 1536)),
    )
    prompt = pattern_prompt(hint)
    with ctx.db.tx() as conn:
        version = next_seq(conn, f"pattern_version:{seed_id}")
    attempts: list[dict[str, Any]] = []
    result = None
    for i, name in enumerate(models, 1):
        model = imagegen.current_model(ctx, name)
        bg.step(ctx, f"{STEP_NAMES[PATTERN_STAGE]}：{model}")
        res = imagegen.generate(
            ctx,
            scope_id=f"seed:{seed_id}",
            stage=PATTERN_STAGE,
            model=model,
            prompt=prompt,
            idempotency_key=f"img:seed:{seed_id}:pattern:v{version}:{i}",
            slot_id=f"pattern#{i}",
            input_images=[source],
            asset_hint=f"seed:{seed_id}:pattern-{version}",
        )
        attempts.append(
            {
                "model": model,
                "status": res.status,
                "call_id": res.call_id,
                "error": (res.error or "")[:300] or None,
            }
        )
        if res.status == "ok" and res.data:
            result = (model, res.data)
            break
    if result is None:
        with ctx.db.tx() as conn:
            log_event(conn, seed_id, "pattern_failed", {"attempts": attempts})
        raise StageError(
            f"{seed_id} 图案提取没成功："
            + "；".join(f"{a['model']} {a['error'] or a['status']}" for a in attempts)
        )
    model, data = result
    fmt, width, height = images.probe(data)
    ext = images.FORMAT_EXT.get(fmt, "png")
    now = now_iso()
    with ctx.db.tx() as conn:
        path = store.save(
            ctx,
            conn,
            store.seed_dir(ctx.home, seed_id) / f"pattern-{version}.{ext}",
            data,
        )
        stored = store.rel(ctx.home, path)
        conn.execute(
            "INSERT INTO seed_patterns(seed_id, path, version, model, prompt_sha, hint, attempts_json,"
            " created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(seed_id) DO UPDATE SET path = excluded.path, version = excluded.version,"
            " model = excluded.model, prompt_sha = excluded.prompt_sha, hint = excluded.hint,"
            " attempts_json = excluded.attempts_json, updated_at = excluded.updated_at",
            (
                seed_id,
                stored,
                version,
                model,
                sha256_text(prompt)[:16],
                hint,
                dumps(attempts),
                now,
                now,
            ),
        )
        log_event(
            conn,
            seed_id,
            "pattern_extracted",
            {"path": stored, "version": version, "model": model, "hint": hint},
        )
    store.kick(ctx)
    return {
        "seed_id": seed_id,
        "pattern_path": stored,
        "version": version,
        "model": model,
        "width": width,
        "height": height,
        "attempts": attempts,
        "hint": hint,
    }


def naming_enabled(ctx: Ctx) -> bool:
    return bool(ctx.setting("task_name.enabled", True))


def clean_description(text: Any) -> str:
    """名字的描述部分只留文字和数字：去掉空白、标点和符号（飞书数据表名不认 [ ] : \\ / ? *）。"""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(text or "")))


def _description_errors(desc: str) -> list[str]:
    if NAME_DESC_MIN <= len(desc) <= NAME_DESC_MAX:
        return []
    return [
        f"description 去掉标点和空格后要 {NAME_DESC_MIN}–{NAME_DESC_MAX} 个字（4–8 个汉字最好），"
        f"现在 {len(desc)} 个：「{desc}」"
    ]


def _validate_p12(result: dict[str, Any]) -> list[str]:
    return _description_errors(clean_description(result.get("description")))


def name_day(ctx: Ctx, seed: dict[str, Any]) -> str:
    """名字里的日期：这张图登记那天（按配置时区），YYYY-MM-DD。"""
    return local_day(
        str(ctx.setting("timezone", "Asia/Shanghai")), parse_iso(seed["created_at"])
    )


def format_name(description: str, day: str) -> str:
    """任务名字 = 描述 + 空格 + 月日，如「森林三伙伴水彩 1020」。"""
    return f"{description} {day[5:7]}{day[8:10]}"


def _save_name(
    ctx: Ctx,
    seed: dict[str, Any],
    description: str,
    *,
    source: str,
    by: str | None = None,
    model: str | None = None,
    call_ids: list[str] | None = None,
) -> dict[str, Any]:
    """存名字（每张参考图一行，改名就覆盖）；候选表跟着改名的次数清零，交给下一次 table sync。"""
    seed_id = seed["seed_id"]
    day = name_day(ctx, seed)
    name = format_name(description, day)
    now = now_iso()
    with ctx.db.tx() as conn:
        old = conn.execute(
            "SELECT name FROM seed_names WHERE seed_id = ?", (seed_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO seed_names(seed_id, name, description, name_date, source, model, call_ids_json,"
            " named_by, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(seed_id) DO UPDATE SET name = excluded.name, description = excluded.description,"
            " name_date = excluded.name_date, source = excluded.source, model = excluded.model,"
            " call_ids_json = excluded.call_ids_json, named_by = excluded.named_by,"
            " table_attempts = 0, table_error = NULL, updated_at = excluded.updated_at",
            (
                seed_id,
                name,
                description,
                day,
                source,
                model,
                dumps(call_ids or []),
                by,
                now,
                now,
            ),
        )
        previous = old["name"] if old else None
        log_event(
            conn,
            seed_id,
            "task_named",
            {"name": name, "source": source, "by": by, "previous": previous},
        )
    feishu_table.kick(ctx)
    return {
        "seed_id": seed_id,
        "name": name,
        "label": f"{seed_id} {name}",
        "description": description,
        "date": day,
        "source": source,
        "previous": previous,
    }


def name_seed(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    """S01N 任务命名：模型看原图写一个简短描述（P12），接上登记那天的月日存成任务名字。"""
    seed = get_seed(ctx, seed_id)
    out = llm.run_stage(
        ctx,
        NAME_PROMPT,
        scope_id=f"seed:{seed_id}",
        job_id=f"seed:{seed_id}",
        variables={
            "source_metadata": {
                "seed_id": seed_id,
                "width": seed["width"],
                "height": seed["height"],
                "source_ref": seed["source_ref"],
                "source_note": seed["source_note"],
            },
        },
        images={
            "source_images": [
                vision_image(
                    ctx,
                    original_bytes(ctx, seed),
                    f"{seed_id} 原图（{seed['width']}x{seed['height']}）",
                )
            ]
        },
        extra_validate=_validate_p12,
    )
    return _save_name(
        ctx,
        seed,
        clean_description(out.result["description"]),
        source="auto",
        model=out.model,
        call_ids=out.call_ids,
    )


def set_seed_name(
    ctx: Ctx, seed_id: str, description: str, *, by: str | None = None
) -> dict[str, Any]:
    """人改名：只换描述，日期仍是这张图登记那天。"""
    seed = get_seed(ctx, seed_id)
    desc = clean_description(description)
    if _description_errors(desc):
        raise StageError(
            f"名字的描述要 {NAME_DESC_MIN}–{NAME_DESC_MAX} 个字（不算标点和空格），现在 {len(desc)} 个"
        )
    return _save_name(ctx, seed, desc, source="manual", by=by)


def _name_quietly(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    """拆解时顺带起名（在另一个线程里跑）：失败只记事件和 name_error，不影响拆解。"""
    try:
        return {"name": name_seed(ctx, seed_id)["name"]}
    except Exception as exc:  # 起名是顺带的，任何错误都不能挡住拆解
        error = f"{type(exc).__name__}：{exc}"[:300]
        with ctx.db.tx() as conn:
            log_event(conn, seed_id, "task_name_failed", {"error": error})
        return {"name_error": error}
    finally:
        ctx.db.close()


def _split_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [p for p in re.split(r"[/,，、\s]+", str(value or "")) if p]


def _validate_p02(result: dict[str, Any]) -> list[str]:
    """在用字段要齐、不重复，小变化 / 大变化字段要有 variable；停用或不认识的字段由程序丢掉，不为它重答。"""
    errors: list[str] = []
    expected = fields.active_field_ids()
    ids = [f.get("field_id") for f in result.get("fields", [])]
    missing = [fid for fid in expected if fid not in ids]
    dup = sorted({fid for fid in ids if fid in expected and ids.count(fid) > 1})
    if missing:
        errors.append(
            f"fields 缺少在用字段：{missing}（field_dictionary.fields 里每个都必须出现一次；"
            "看不清写 unknown，没有写 not_applicable）"
        )
    if dup:
        errors.append(f"fields 有重复字段：{dup}")
    no_judgment = [
        f.get("field_id")
        for f in result.get("fields", [])
        if fields.tier(f.get("field_id")) in fields.VARIABLE_TIERS
        and not isinstance(f.get("variable"), bool)
    ]
    if no_judgment:
        errors.append(
            f"这些小变化 / 大变化字段缺少 variable（true / false）和 variable_reason：{no_judgment}"
        )
    known_refs = {o.get("object_id") for o in result.get("objects", [])} | {
        g.get("group_id") for g in result.get("groups", []) if isinstance(g, dict)
    }
    for rel in result.get("relations", []):
        for ref in _split_ids(rel.get("from")) + _split_ids(rel.get("to")):
            if ref not in known_refs:
                errors.append(
                    f"relations 引用了不存在的对象 {ref}（必须先在 objects/groups 里定义）"
                )
                break
    return errors


def build_variation(result: dict[str, Any]) -> dict[str, Any]:
    """拆解里的可变判断 → 可变字段清单：先按参考图特征的门控关掉维度，再看模型给的 variable。"""
    traits = result.get("traits") or {}
    off = fields.gated_off(traits)
    order = {fid: i for i, fid in enumerate(fields.variation_field_ids())}
    variable: list[dict[str, Any]] = []
    not_variable: list[dict[str, Any]] = []
    for f in result.get("fields", []):
        fid = f.get("field_id")
        tier = fields.tier(fid)
        if tier not in fields.VARIABLE_TIERS:
            continue
        dim = fields.dimension(fid)
        item = {
            "field_id": fid,
            "field": fields.field_name(fid),
            "label": fields.label(fid),
            "dimension": dim,
            "tier": tier,
            "value": clip(f.get("value"), VARIATION_VALUE_CHARS),
        }
        if dim in off:
            not_variable.append({**item, "reason": off[dim], "gated": True})
        elif f.get("variable"):
            variable.append({**item, "reason": f.get("variable_reason") or ""})
        else:
            not_variable.append(
                {**item, "reason": f.get("variable_reason") or "这张图上改不出来"}
            )
    variable.sort(key=lambda x: order.get(x["field_id"], 999))
    not_variable.sort(key=lambda x: order.get(x["field_id"], 999))
    return {
        "traits": {
            "layout_kind": traits.get("layout_kind"),
            "has_text": bool(traits.get("has_text")),
        },
        "gated_off": off,
        "variable": variable,
        "not_variable": not_variable,
    }


def analysis_variation(analysis: dict[str, Any] | None) -> dict[str, Any] | None:
    """这份拆解的可变字段清单；梳理字段之前拆的没有，返回 None。

    按现在的字段字典再过一遍：
    - 停用并进别的字段的（0.3 起绘制技法 D42–D44 并入 D41）：任一判定能变，并入的字段就算能变，它们自己不再列出；
    - 拆解之后改成只记录的字段（不再出任何方向）挪进不能变。
    """
    if not analysis:
        return None
    result = loads(analysis["result_json"], {}) or {}
    variation = result.get("variation")
    if not variation:
        return variation
    keep, moved = [], []
    folded: dict[str, list[dict[str, Any]]] = {}
    for item in variation.get("variable") or []:
        fid = item.get("field_id")
        target = fields.canonical(fid)
        if target and target != fid:
            folded.setdefault(target, []).append(item)
        elif fields.kinds(fid):
            keep.append(
                {**item, "field": fields.field_name(fid), "label": fields.label(fid)}
            )
        else:
            moved.append(item)
    original = list(variation.get("not_variable") or [])
    not_variable = [
        x
        for x in original
        if fields.canonical(x.get("field_id")) in (None, x.get("field_id"))
    ]
    have = {x["field_id"] for x in keep}
    for target, items in folded.items():
        if target in have:
            continue
        not_variable = [x for x in not_variable if x.get("field_id") != target]
        keep.append(
            {
                "field_id": target,
                "field": fields.field_name(target),
                "label": fields.label(target),
                "dimension": fields.dimension(target),
                "tier": fields.tier(target),
                "value": clip(
                    "；".join(str(i.get("value") or "") for i in items),
                    VARIATION_VALUE_CHARS,
                ),
                "reason": "；".join(str(i["reason"]) for i in items if i.get("reason")),
            }
        )
    if not moved and not folded and len(not_variable) == len(original):
        return {**variation, "variable": keep}
    order = {fid: i for i, fid in enumerate(fields.variation_field_ids())}
    keep.sort(key=lambda x: order.get(x["field_id"], 999))
    return {
        **variation,
        "variable": keep,
        "not_variable": not_variable
        + [{**item, "reason": RECORD_ONLY_REASON} for item in moved],
    }


def run_s02(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    seed = get_seed(ctx, seed_id)
    record = loads(seed["seed_record_json"])
    if not record:
        raise StageError(f"{seed_id} 还没做 S01 接入")
    if seed["status"] == "blocked":
        raise StageError(f"{seed_id} 素材受限，已隔离，不做拆解")
    if pattern_enabled(ctx) and pattern_path(ctx, seed_id):
        # 设计从提取出的平面图案读；原图只用来看商品形态、尺寸与面料工艺
        source = [
            vision_image(
                ctx,
                seed_bytes(ctx, seed),
                f"{seed_id} 从参考图提取的平面图案：设计、构图、配色都从这张读",
            ),
            vision_image(
                ctx,
                original_bytes(ctx, seed),
                f"{seed_id} 原图：只用来看商品形态、尺寸与面料工艺，不从这里读设计",
            ),
        ]
    else:
        source = [
            vision_image(
                ctx,
                original_bytes(ctx, seed),
                f"{seed_id} 原图，只分析 seed_record 标出的有效印花区域",
            )
        ]
    out = llm.run_stage(
        ctx,
        "P02",
        scope_id=f"seed:{seed_id}",
        job_id=f"seed:{seed_id}",
        variables={
            "seed_record": record["result"],
            "field_dictionary": {
                **fields.prompt_dictionary(),
                "evidence_types": EVIDENCE_TYPES,
            },
        },
        images={"source_image": source},
        extra_validate=_validate_p02,
    )
    result = out.result
    active = set(fields.active_field_ids())
    dropped = sorted(
        {
            str(f.get("field_id"))
            for f in result["fields"]
            if str(f.get("field_id", "")).startswith("D")
            and f.get("field_id") not in active
        }
    )
    result["fields"] = [
        f
        for f in result["fields"]
        if f.get("field_id") in active or str(f.get("field_id", "")).startswith("M")
    ]
    result["field_schema"] = fields.schema_version()
    result["variation"] = build_variation(result)
    if dropped:
        result["variation"]["dropped_fields"] = dropped
    coverage: dict[str, Any] = {
        "design_fields": len(active),
        "variable": len(result["variation"]["variable"]),
        "not_variable": len(result["variation"]["not_variable"]),
    }
    for f in result["fields"]:
        coverage[f["evidence_type"]] = coverage.get(f["evidence_type"], 0) + 1
    with ctx.db.tx() as conn:
        version = next_seq(conn, f"analysis_version:{seed_id}")
        analysis_id = f"{seed_id}-A{version:02d}"
        result["analysis_id"] = analysis_id
        result["seed_id"] = seed_id
        conn.execute(
            "INSERT INTO analyses(analysis_id, seed_id, analysis_version, model, prompt_version, result_json,"
            " coverage_json, unknowns_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                analysis_id,
                seed_id,
                version,
                out.model,
                out.prompt_version,
                dumps(result),
                dumps(coverage),
                dumps(out.envelope["unknowns"]),
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE seeds SET latest_analysis_id = ?, status = 'analyzed', updated_at = ? WHERE seed_id = ?",
            (analysis_id, now_iso(), seed_id),
        )
        log_event(
            conn,
            seed_id,
            "analysis_saved",
            {"analysis_id": analysis_id, "coverage": coverage},
        )
    return {
        "seed_id": seed_id,
        "analysis_id": analysis_id,
        "coverage": coverage,
        "objects": len(result["objects"]),
    }


def _analysis_for_prompt(analysis: dict[str, Any]) -> dict[str, Any]:
    return loads(analysis["result_json"])


def run_s03(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    seed = get_seed(ctx, seed_id)
    analysis = latest_analysis(ctx, seed_id)
    if not analysis:
        raise StageError(f"{seed_id} 还没做 S02 拆解")
    out = llm.run_stage(
        ctx,
        "P03",
        scope_id=f"seed:{seed_id}",
        job_id=f"seed:{seed_id}",
        variables={
            "analysis": _analysis_for_prompt(analysis),
            "optional_reviews": None,
            "optional_performance_data": seed["sales_evidence"] or None,
        },
    )
    result = out.result
    if not seed["sales_evidence"]:
        # 没有市场数据时假设只能是待验证
        for h in result["hypotheses"]:
            h["status"] = "unvalidated"
    with ctx.db.tx() as conn:
        n = next_seq(conn, f"hypotheses:{analysis['analysis_id']}")
        set_id = f"{analysis['analysis_id']}-H{n:02d}"
        conn.execute(
            "INSERT INTO hypotheses(hypothesis_set_id, analysis_id, model, prompt_version, result_json, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (
                set_id,
                analysis["analysis_id"],
                out.model,
                out.prompt_version,
                dumps(result),
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE seeds SET latest_hypothesis_set_id = ?, status = 'hypothesized', updated_at = ? WHERE seed_id = ?",
            (set_id, now_iso(), seed_id),
        )
    return {
        "seed_id": seed_id,
        "hypothesis_set_id": set_id,
        "hypotheses": len(result["hypotheses"]),
    }


def _validate_p04_legacy(result: dict[str, Any]) -> list[str]:
    """梳理字段之前的拆解没有可变判断：变化域只要求字段在字典里。"""
    valid = fields.known_field_ids()
    bad = [
        d.get("field_id")
        for d in result.get("variable_domains", [])
        if d.get("field_id") not in valid
    ]
    return [f"variable_domains 引用了字典里不存在的字段：{bad}"] if bad else []


def _variable_fields_input(variation: dict[str, Any] | None) -> dict[str, Any]:
    if not variation:
        return {
            "note": "这份拆解是梳理字段之前做的，没有可变判断：variable_domains 照原来的写法从字典里选字段",
        }
    return {
        "rules": {name: fields.tier_rule(name) for name in fields.VARIABLE_TIERS},
        "traits": variation["traits"],
        "gated_off": variation["gated_off"],
        "variable": [
            {
                **v,
                "kinds": fields.slot_kinds(v["field_id"]),
                **(
                    {"small_change": fields.small_change(v["field_id"])}
                    if fields.small_change(v["field_id"])
                    else {}
                ),
                **(
                    {"big_change": fields.big_change(v["field_id"])}
                    if fields.big_change(v["field_id"])
                    else {}
                ),
                **(
                    {"change_forms": fields.change_forms(v["field_id"])}
                    if fields.change_forms(v["field_id"])
                    else {}
                ),
            }
            for v in variation["variable"]
        ],
        "not_variable": [
            {k: x.get(k) for k in ("field_id", "label", "dimension", "reason")}
            for x in variation["not_variable"]
        ],
    }


def normalize_family_spec(
    spec: dict[str, Any], variation: dict[str, Any]
) -> dict[str, Any]:
    """变化域只留这张图的可变字段：多出来的丢掉、漏掉的补一条空选项（不为这个重答），锁定的单独列。

    锁定只认来自用户或印制要求的（source 是 LOCK_SOURCES 之一）；没写或写了别的按设计建议处理：不锁，放回变化域。"""
    variable_ids = [v["field_id"] for v in variation["variable"]]
    variable_set = set(variable_ids)
    notes: list[str] = []
    locked: list[dict[str, Any]] = []
    unlocked: set[str] = set()
    for item in spec.get("locked_fields") or []:
        fid = item.get("field_id") if isinstance(item, dict) else item
        fid = fields.canonical(fid) or fid
        reason = (item.get("reason") if isinstance(item, dict) else None) or ""
        source = item.get("source") if isinstance(item, dict) else None
        if fid in variable_set and fid not in {x["field_id"] for x in locked}:
            if source not in LOCK_SOURCES:
                unlocked.add(fid)
                notes.append(
                    f"锁定字段 {fid} 没写来自用户或印制要求（source），按设计建议处理：不锁，放回变化域"
                )
                continue
            locked.append(
                {
                    "field_id": fid,
                    "label": fields.label(fid),
                    "reason": reason,
                    "source": source,
                }
            )
        elif fid:
            notes.append(f"锁定字段 {fid} 不是这张图的可变字段，已忽略")
    locked_ids = {x["field_id"] for x in locked}
    domains: dict[str, dict[str, Any]] = {}
    for d in spec.get("variable_domains") or []:
        fid = d.get("field_id") if isinstance(d, dict) else None
        fid = fields.canonical(fid) or fid
        if fid in locked_ids or fid in domains:
            continue
        if fid not in variable_set:
            notes.append(f"变化域里的 {fid} 不是这张图的可变字段，已去掉")
            continue
        domains[fid] = {
            "field_id": fid,
            "options": d.get("options") or [],
            "compatibility": d.get("compatibility") or [],
        }
    for fid in variable_ids:
        if fid not in domains and fid not in locked_ids:
            domains[fid] = {"field_id": fid, "options": [], "compatibility": []}
            if fid not in unlocked:
                notes.append(f"变化域漏了可变字段 {fid}，已补上（选项留给裂变时定）")
    spec["variable_domains"] = [domains[f] for f in variable_ids if f in domains]
    spec["locked_fields"] = locked
    spec["field_schema"] = fields.schema_version()
    spec["variation"] = {
        "traits": variation["traits"],
        "gated_off": variation["gated_off"],
        "not_variable": [
            {k: x.get(k) for k in ("field_id", "label", "dimension", "reason")}
            for x in variation["not_variable"]
        ],
        "notes": notes,
    }
    return spec


def run_s04(
    ctx: Ctx,
    seed_id: str,
    *,
    positioning: str | None = None,
    revise_family_id: str | None = None,
    revision_note: str | None = None,
) -> dict[str, Any]:
    seed = get_seed(ctx, seed_id)
    analysis = latest_analysis(ctx, seed_id)
    if not analysis or not seed["latest_hypothesis_set_id"]:
        raise StageError(f"{seed_id} 需要先完成 S02 拆解与 S03 假设")
    hyp = ctx.db.q1(
        "SELECT * FROM hypotheses WHERE hypothesis_set_id = ?",
        (seed["latest_hypothesis_set_id"],),
    )
    previous = get_family(ctx, revise_family_id) if revise_family_id else None
    rights_ok = seed["rights_status"] == "approved"
    reference = (
        "从参考图提取的平面图案"
        if pattern_enabled(ctx) and pattern_path(ctx, seed_id)
        else "参考图原图"
    )
    user_positioning: dict[str, Any] = {
        "positioning": positioning
        or seed["user_positioning"]
        or ctx.setting("project.default_positioning"),
    }
    if previous:
        user_positioning["revision_request"] = revision_note
        user_positioning["previous_family_spec"] = previous["spec"]
    variation = analysis_variation(analysis)
    with ctx.db.tx() as conn:
        proposal = next_seq(conn, f"family_proposal:{seed_id}")
    out = llm.run_stage(
        ctx,
        "P04",
        scope_id=f"family-proposal:{seed_id}:p{proposal}",
        job_id=f"family:{revise_family_id or 'pending'}",
        variables={
            "analysis": _analysis_for_prompt(analysis),
            "reference_profile": reference_profile(ctx, analysis["analysis_id"]),
            "variable_fields": _variable_fields_input(variation),
            "hypotheses": loads(hyp["result_json"]),
            "user_positioning": user_positioning,
            "rights_scope": {
                "rights_status": seed["rights_status"],
                "confirmed_by": seed["rights_confirmed_by"],
                "reference_image_use": (
                    f"已授权：文生图只用文字指令；图生图把{reference}作为输入图"
                    if rights_ok
                    else f"授权未确认（用户决定照常使用）：文生图只用文字指令；图生图把{reference}作为输入图，"
                    "不得出现可识别的既有角色、品牌标识或商标"
                ),
            },
            "production_constraints": {
                "format_profile": ctx.setting("image_generation.format_profile"),
                "target_ratio": ctx.setting(
                    f"format_profiles.{ctx.setting('image_generation.format_profile')}.target_ratio"
                ),
                "sku_template": None,
                "readiness_ceiling": "preview_ready（无供应商模板，不承诺可生产）",
                "generators": fanout_models(ctx),
                "generation_modes": [MODE_LABELS[m] for m in generation_modes(ctx)],
            },
        },
        extra_validate=None if variation else _validate_p04_legacy,
    )
    spec = out.result
    if variation:
        spec = normalize_family_spec(spec, variation)
    spec["approval_status"] = "pending"
    spec["analysis_id"] = analysis["analysis_id"]
    with ctx.db.tx() as conn:
        family_id = revise_family_id or next_id(conn, "family")
        version = next_seq(conn, f"family_version:{family_id}")
        spec["family_id"] = family_id
        spec["family_version"] = version
        conn.execute(
            "UPDATE families SET status = 'superseded', updated_at = ? WHERE family_id = ? AND status = 'pending'",
            (now_iso(), family_id),
        )
        conn.execute(
            "INSERT INTO families(family_id, family_version, analysis_id, seed_id, status, spec_json, model,"
            " prompt_version, revision_note, created_at, updated_at) VALUES(?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (
                family_id,
                version,
                analysis["analysis_id"],
                seed_id,
                dumps(spec),
                out.model,
                out.prompt_version,
                revision_note,
                now_iso(),
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE seeds SET status = 'family_pending', updated_at = ? WHERE seed_id = ?",
            (now_iso(), seed_id),
        )
        conn.execute(
            "UPDATE reviews SET status = 'dismissed', note = '已被新版本取代' WHERE subject_type = 'family'"
            " AND subject_id LIKE ? AND status = 'open'",
            (f"{family_id}@%",),
        )
        open_review(
            conn,
            "family",
            f"{family_id}@v{version}",
            "系列规则需要人工确认后才能裂变与生图（podctl family approve）",
            {"positioning": spec.get("positioning")},
        )
        log_event(
            conn, family_id, "family_proposed", {"version": version, "seed_id": seed_id}
        )
    return {
        "family_id": family_id,
        "family_version": version,
        "status": "pending",
        "spec": spec,
    }


def decide_family(
    ctx: Ctx, family_id: str, *, approve: bool, by: str, note: str | None = None
) -> dict[str, Any]:
    fam = get_family(ctx, family_id)
    if fam["status"] != "pending":
        raise StageError(
            f"{family_id} v{fam['family_version']} 当前状态 {fam['status']}，只有 pending 可以审批"
        )
    status = "approved" if approve else "rejected"
    with ctx.db.tx() as conn:
        if approve:
            conn.execute(
                "UPDATE families SET status = 'superseded', updated_at = ? WHERE family_id = ? AND status IN ('approved', 'exhausted')",
                (now_iso(), family_id),
            )
        spec = fam["spec"]
        spec["approval_status"] = status
        conn.execute(
            "UPDATE families SET status = ?, spec_json = ?, approved_by = ?, approved_at = ?, updated_at = ?"
            " WHERE family_id = ? AND family_version = ?",
            (
                status,
                dumps(spec),
                by,
                now_iso(),
                now_iso(),
                family_id,
                fam["family_version"],
            ),
        )
        conn.execute(
            "UPDATE reviews SET status = 'decided', decision = ?, decided_by = ?, decided_at = ?, note = ?"
            " WHERE subject_type = 'family' AND subject_id = ? AND status = 'open'",
            (status, by, now_iso(), note, f"{family_id}@v{fam['family_version']}"),
        )
        log_event(
            conn,
            family_id,
            f"family_{status}",
            {"version": fam["family_version"], "by": by, "note": note},
        )
    return {
        "family_id": family_id,
        "family_version": fam["family_version"],
        "status": status,
        "by": by,
    }


def _reanalyze(ctx: Ctx, seed_id: str, positioning: str | None) -> dict[str, Any]:
    """按当前字段字典重新拆解：重跑 S02、S02D、S03，S04 修订这张图已有的系列（方向记录接着算）。

    开着图案提取、这张图还没有图案时先提取（S01P），拆解用图案。

    上次重拆停在半路时接着做：已按当前字典拆过、还没建成系列的拆解与它的假设不再重跑。
    """
    steps: list[dict[str, Any]] = []
    row = ctx.db.q1(
        "SELECT family_id, analysis_id FROM families WHERE seed_id = ?"
        " ORDER BY created_at DESC, family_id DESC, family_version DESC LIMIT 1",
        (seed_id,),
    )
    analysis = latest_analysis(ctx, seed_id)
    resume = (
        analysis is not None
        and (loads(analysis["result_json"], {}) or {}).get("field_schema")
        == fields.schema_version()
        and (row is None or row["analysis_id"] != analysis["analysis_id"])
    )
    if pattern_enabled(ctx) and not pattern_path(ctx, seed_id):
        bg.step(ctx, step_label(ctx, PATTERN_STAGE))
        steps.append({"stage": PATTERN_STAGE, **extract_pattern(ctx, seed_id)})
    if resume:
        analysis_id = analysis["analysis_id"]
        steps.append({"stage": "S02", "analysis_id": analysis_id, "resumed": True})
    else:
        bg.step(ctx, step_label(ctx, "S02"))
        steps.append({"stage": "S02", **run_s02(ctx, seed_id)})
        analysis_id = steps[-1]["analysis_id"]
    steps += _describe_step(ctx, seed_id, analysis_id)
    hypothesis = ctx.db.q1(
        "SELECT h.hypothesis_set_id FROM seeds s JOIN hypotheses h ON h.hypothesis_set_id = s.latest_hypothesis_set_id"
        " WHERE s.seed_id = ? AND h.analysis_id = ?",
        (seed_id, analysis_id),
    )
    if resume and hypothesis:
        steps.append(
            {
                "stage": "S03",
                "hypothesis_set_id": hypothesis["hypothesis_set_id"],
                "resumed": True,
            }
        )
    else:
        bg.step(ctx, step_label(ctx, "S03"))
        steps.append({"stage": "S03", **run_s03(ctx, seed_id)})
    bg.step(ctx, step_label(ctx, "S04"))
    if row:
        steps.append(
            {
                "stage": "S04",
                **run_s04(
                    ctx,
                    seed_id,
                    positioning=positioning,
                    revise_family_id=row["family_id"],
                    revision_note=f"按 {fields.schema_version()} 版字段字典重新拆解（{analysis_id}）："
                    "变化域只放这次拆解判定能变的字段；上一版的锁定、保留项、排除项和联动条款只沿用来自用户要求"
                    "（user_positioning、以前的修订意见）和印制要求的，设计建议类的锁定与把可变字段限定死的条款不沿用；"
                    "删掉「不复现参考图组合 / 布局 / 构图」「纯技法不算独立款 / 只作验证任务」「轴覆盖记录」这几类旧条款",
                ),
            }
        )
    else:
        steps.append({"stage": "S04", **run_s04(ctx, seed_id, positioning=positioning)})
    return {
        "seed_id": seed_id,
        "status": get_seed(ctx, seed_id)["status"],
        "reanalyzed": True,
        "steps": steps,
    }


def analyze(
    ctx: Ctx,
    seed_id: str,
    *,
    force: bool = False,
    reanalyze: bool = False,
    positioning: str | None = None,
) -> dict[str, Any]:
    """S01 → S01P → S02 → S02D → S03 → S04 顺序执行；已完成的阶段默认跳过，遇到阻断立即停下。

    S01P（参考图 2D 化）在接入之后马上做：开着 pattern_extract 时先提取平面图案，后面的拆解都用它；
    提取失败抛错停在这一步，再跑接着做。
    S02D（画面完整描述）在拆解之后做：最新的拆解还没有描述就写一份；没写成只在结果里带 description_error，
    拆解照常往下走（之后用拆解简版兜底，可以用 seed describe 补写）。
    reanalyze：接入过的参考图按当前字段字典重新拆解（S02–S04，S04 修订已有系列）。
    开着拆解后自动策划时，命令行接着跑 S05（cli._auto_plan），进度序号见 step_label。
    这张图还没有名字、开着 task_name 时，另起一个线程同时跑 S01N 任务命名：结果里带 name，
    起名失败只带 name_error；拆解中途报错也先等起名结束再抛出。已有名字不再改。
    接入确认冻结（intake.review: false）时，先放行停在接入待确认的这张图（结果里带 intake_released）。
    """
    released = _release_intake(ctx, get_seed(ctx, seed_id))
    if not naming_enabled(ctx) or seed_name(ctx, seed_id):
        result = _analyze(
            ctx, seed_id, force=force, reanalyze=reanalyze, positioning=positioning
        )
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            naming = pool.submit(_name_quietly, ctx, seed_id)
            result = _analyze(
                ctx, seed_id, force=force, reanalyze=reanalyze, positioning=positioning
            )
        result = {**result, **naming.result()}
    if released:
        result["intake_released"] = released
    error = next(
        (
            s["description_error"]
            for s in result.get("steps") or []
            if s.get("description_error")
        ),
        None,
    )
    if error:
        result["description_error"] = error
    units = next(
        (
            s["units"]
            for s in result.get("steps") or []
            if s.get("stage") == DESCRIBE_STAGE and s.get("units")
        ),
        None,
    )
    if units:
        result["units"] = units
    return result


def _describe_step(
    ctx: Ctx, seed_id: str, analysis_id: str | None
) -> list[dict[str, Any]]:
    """S02D：开着画面描述、这份拆解还没有描述时写一份。"""
    if (
        not describe_enabled(ctx)
        or not analysis_id
        or get_description(ctx, analysis_id)
    ):
        return []
    bg.step(ctx, step_label(ctx, DESCRIBE_STAGE))
    return [{"stage": DESCRIBE_STAGE, **describe_quietly(ctx, seed_id)}]


def _analyze(
    ctx: Ctx,
    seed_id: str,
    *,
    force: bool,
    reanalyze: bool,
    positioning: str | None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    seed = get_seed(ctx, seed_id)
    if reanalyze and seed["seed_record_json"] and seed["status"] != "needs_review":
        return _reanalyze(ctx, seed_id, positioning)
    if force or not seed["seed_record_json"]:
        bg.step(ctx, step_label(ctx, "S01"))
        steps.append({"stage": "S01", **run_s01(ctx, seed_id)})
        seed = get_seed(ctx, seed_id)
        if seed["status"] != "ingested":
            return {
                "seed_id": seed_id,
                "stopped_at": "S01",
                "status": seed["status"],
                "steps": steps,
            }
    if pattern_enabled(ctx) and (force or not pattern_path(ctx, seed_id)):
        bg.step(ctx, step_label(ctx, PATTERN_STAGE))
        steps.append({"stage": PATTERN_STAGE, **extract_pattern(ctx, seed_id)})
    if force or not seed["latest_analysis_id"]:
        bg.step(ctx, step_label(ctx, "S02"))
        steps.append({"stage": "S02", **run_s02(ctx, seed_id)})
        seed = get_seed(ctx, seed_id)
    steps += _describe_step(ctx, seed_id, seed["latest_analysis_id"])
    if force or not seed["latest_hypothesis_set_id"]:
        bg.step(ctx, step_label(ctx, "S03"))
        steps.append({"stage": "S03", **run_s03(ctx, seed_id)})
    has_family = ctx.db.q1("SELECT 1 FROM families WHERE seed_id = ?", (seed_id,))
    if force or not has_family:
        bg.step(ctx, step_label(ctx, "S04"))
        steps.append({"stage": "S04", **run_s04(ctx, seed_id, positioning=positioning)})
    return {
        "seed_id": seed_id,
        "status": get_seed(ctx, seed_id)["status"],
        "steps": steps,
    }
