"""单个变体任务的推进：S06 编译 → T01 生图（每个模型文生图 + 图生图 + 图生图 Edit 并发）→ S07 独立质检（→ T01R 定点修复）→ S09 印制预检 → S10 归档。

推进是「看状态决定下一步」：每一步的结果都先落库，进程中断后重新领取会从断点继续；
人工审核项（质检待审、结果未知对账、选款）不阻塞任务完结，人工裁决后再把任务放回队列。
不做候选之间、候选与参考图之间的相似度判定：相近的候选一并进选款，由人挑。
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import budget, feishu_table, imagegen, images, limits, llm, state, store
from ..context import Ctx
from ..db import log_event, next_id, open_review
from ..util import dumps, loads, now_iso, sha256_bytes, utcnow
from .common import (
    GEN_MODES,
    MODE_LABELS,
    QC_OFF_REASON,
    StageError,
    asset_bytes,
    base_asset_id,
    fanout_slots,
    get_family,
    get_job,
    get_seed,
    input_image_bytes,
    seed_bytes,
    slot_mode,
    variant_modes,
    qc_enabled,
    variant_view,
    vision_image,
)
from .profile import reference_profile

GEN_STEPS = frozenset({"T01", "T01R"})
BACKLOG_TYPES = ("asset_qc", "asset_selection", "call_reconcile")
# 被阻断的任务只在条件恢复后才会再被领取，所以领取次数主要来自中断回收与到时限放回
MAX_JOB_CLAIMS = 20
_sleep: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------- 辅助


def backlog_count(ctx: Ctx) -> int:
    marks = ",".join("?" for _ in BACKLOG_TYPES)
    return int(
        ctx.db.scalar(
            f"SELECT COUNT(*) FROM reviews WHERE status = 'open' AND subject_type IN ({marks})",
            BACKLOG_TYPES,
        )
    )


def target_ratio(ctx: Ctx) -> tuple[str, float]:
    profile = (
        ctx.setting(
            f"format_profiles.{ctx.setting('image_generation.format_profile')}", {}
        )
        or {}
    )
    text = str(profile.get("target_ratio") or "4:5")
    w, h = (float(x) for x in text.split(":"))
    return text, w / h


def _assets(ctx: Ctx, job_id: str, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        rows = ctx.db.q(
            "SELECT * FROM assets WHERE job_id = ? AND status = ? ORDER BY asset_id",
            (job_id, status),
        )
    else:
        rows = ctx.db.q(
            "SELECT * FROM assets WHERE job_id = ? ORDER BY asset_id", (job_id,)
        )
    return [dict(r) for r in rows]


def _set_asset(ctx: Ctx, asset_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE assets SET {sets}, updated_at = ? WHERE asset_id = ?",
            (*fields.values(), now_iso(), asset_id),
        )
        log_event(
            conn,
            asset_id,
            "asset_updated",
            {k: v for k, v in fields.items() if k == "status"},
        )


def _asset_bytes(ctx: Ctx, asset: dict[str, Any]) -> bytes:
    return store.ensure_local(ctx, asset["path"]).read_bytes()


def _parallel(ctx: Ctx, items: list[Any], fn: Callable[[Any], Any]) -> list[Any]:
    """按 CFG10 并发执行；预算阻断等异常收集起来，等其余项跑完再抛第一个。"""
    if not items:
        return []
    workers = max(1, min(int(ctx.setting("batch.gen_concurrency", 3)), len(items)))
    errors: list[BaseException] = []
    results: list[Any] = []
    # worker 的工位等这几路出完要一两分钟：等的时候不占数据库连接
    limits.release_db(ctx)

    def wrapped(item: Any) -> Any:
        try:
            return fn(item)
        except BaseException as exc:  # noqa: BLE001 - 汇总后统一抛出
            errors.append(exc)
            return None
        finally:
            ctx.db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(wrapped, items))
    if errors:
        blocked = [e for e in errors if isinstance(e, budget.BudgetBlocked)]
        raise blocked[0] if blocked else errors[0]
    return results


MODE_PROMPT_KEYS = {"t2i": "prompts", "i2i": "i2i_prompts"}
_MODE_INPUTS = {
    "t2i": "生图模型只看得到提示词，看不到参考图",
    "i2i": "同一个模型，参考图原图作为输入图和提示词一起发给模型",
}
# 参考图原图可能很大、也可能是编辑接口不收的格式：压成最长边不超过它的 JPEG 再发
REFERENCE_MAX_SIDE = 2048


def generator_capabilities(
    ctx: Ctx, models: list[str], modes: list[str]
) -> dict[str, Any]:
    ratio_text, _ = target_ratio(ctx)
    entries = [
        {
            "model": model,
            "family": imagegen.model_family(model),
            "program_params": imagegen.vendor_params(ctx, model),
            "supports_negative_prompt": False,
            "exact_text_rendering": "不可靠：精确姓名等文字交给独立排版，不让生成模型自由书写",
        }
        for model in dict.fromkeys(models)
    ]
    return {
        "models": entries,
        "prompt_keys": list(dict.fromkeys(m["family"] for m in entries)),
        "generation_modes": [
            {
                "mode": mode,
                "label": MODE_LABELS[mode],
                "result_key": MODE_PROMPT_KEYS[mode],
                "model_input": _MODE_INPUTS[mode],
            }
            for mode in modes
        ],
        "output": "平面印花设计稿（flat artwork），不是商品摆拍图",
        "target_aspect_ratio": ratio_text,
        "parameters": "画幅与分辨率由程序按模型映射；generation_parameters 只作记录，不要发明模型不支持的参数",
        "same_prompt_for_all_models": False,
    }


# 文生图的模型看不到参考图，提示词里出现这些说法只会误导模型
NO_REFERENCE_FORBIDDEN = (
    re.compile(r"\breference images?\b", re.IGNORECASE),
    re.compile(r"\bthe original\b", re.IGNORECASE),
    re.compile(r"\bvariations? of\b", re.IGNORECASE),
    re.compile(r"\bpreserve the original\b", re.IGNORECASE),
    re.compile(r"\bregenerat", re.IGNORECASE),
    re.compile(r"参考图|原图"),
)
# 模型写不出来时把说明、诊断或拒绝写进了提示词：这种文字原样发给生图模型只会出一张无关的图
DIAGNOSTIC_TEXT = re.compile(
    r"COMPILATION BLOCKED|NOT A GENERATION PROMPT|\bdo not submit\b|cannot be compiled"
    r"|无法编译|不是生成提示词|不能作为提示词",
    re.IGNORECASE,
)
# 拆解里的对象编号（E01、S08.E01、S12:E01）与可变单元编号（U01）：生图模型不认识
OBJECT_ID = re.compile(r"\b[EU]\d{2,3}\b")
# P06 结果里只做记录的字段（下游只用 text_overlay.enabled 加一条备注）：模型漏写、写错层级时程序归位或补默认值，
# 不为它们重答。2026-10-26 一天 76 次 P06 有 27 次因为漏了这几个字段被退回，3 个方向因此没出图
P06_RECORD_DEFAULTS: dict[str, Callable[[], Any]] = {
    "avoid_instructions": list,
    "reference_asset_ids": list,
    "edit_regions": list,
    "generation_parameters": dict,
    "text_overlay": lambda: {"enabled": False, "exact_text": None, "layout": None},
    "checks": list,
}
_P06_LIST_FIELDS = (
    "avoid_instructions",
    "reference_asset_ids",
    "edit_regions",
    "checks",
)


def _normalize_p06(variant: dict[str, Any]):
    title = str(variant.get("title") or variant.get("axis") or variant["variant_id"])

    def normalize(envelope: dict[str, Any]) -> list[str]:
        result = envelope["result"]
        keys = ("prompt_summary", "variant_id", "output_level", *P06_RECORD_DEFAULTS)
        moved: list[str] = []
        for key in keys:
            if key in result:
                continue
            # 归位：写在外壳顶层，或写进了 result 下一层的对象（如 i2i_prompts）里
            if key in envelope:
                result[key] = envelope.pop(key)
                moved.append(key)
                continue
            for holder in list(result.values()):
                if isinstance(holder, dict) and key in holder:
                    result[key] = holder.pop(key)
                    moved.append(key)
                    break
        filled: list[str] = []
        if not str(result.get("prompt_summary") or "").strip():
            result["prompt_summary"] = f"{title}（模型没写摘要，程序按方向标题补）"
            filled.append("prompt_summary")
        if not result.get("variant_id"):
            result["variant_id"] = variant["variant_id"]
            filled.append("variant_id")
        if result.get("output_level") not in ("preview", "production_candidate"):
            result["output_level"] = "preview"
            filled.append("output_level")
        for key, make in P06_RECORD_DEFAULTS.items():
            value = result.get(key)
            if value is None:
                result[key] = make()
                filled.append(key)
            elif key in _P06_LIST_FIELDS and not isinstance(value, list):
                result[key] = [value]
        overlay = result["text_overlay"]
        if not isinstance(overlay, dict):
            result["text_overlay"] = P06_RECORD_DEFAULTS["text_overlay"]()
            filled.append("text_overlay")
        elif not isinstance(overlay.get("enabled"), bool):
            overlay["enabled"] = bool(overlay.get("enabled"))
        if not isinstance(result["generation_parameters"], dict):
            result["generation_parameters"] = {"note": result["generation_parameters"]}
        notes = []
        if moved:
            notes.append(f"P06 把 {'、'.join(moved)} 写错了位置，程序挪回 result")
        if filled:
            notes.append(f"P06 没写 {'、'.join(filled)}，程序补了默认值")
        return notes

    return normalize


def _validate_p06(prompt_keys: list[str], modes: list[str]):
    def check(result: dict[str, Any]) -> list[str]:
        errors = []
        for mode in modes:
            key = MODE_PROMPT_KEYS[mode]
            prompts = result.get(key) or {}
            errors += [
                f"{key} 缺少模型家族 {family} 的{MODE_LABELS[mode]}提示词"
                for family in prompt_keys
                if not str(prompts.get(family) or "").strip()
            ]
        for key in MODE_PROMPT_KEYS.values():
            for family, text in (result.get(key) or {}).items():
                if DIAGNOSTIC_TEXT.search(str(text)):
                    errors.append(
                        f"{key}/{family} 是说明或诊断文字，不是提示词：要写成可以直接作画的英文提示词，"
                        "没改的部分照 reference_profile 和参考图写；确实写不出就把 status 写成 blocked，原因写进 unknowns"
                    )
                ids = sorted(set(OBJECT_ID.findall(str(text))))
                if ids:
                    errors.append(
                        f"{key}/{family} 出现了对象编号或单元编号 {ids}：生图模型不认识编号，"
                        "换成画面上看得见的说法（物种、颜色、位置），可以用 reference_profile 里的 en 或改动里的 unit_en"
                    )
        for family, text in (result.get("prompts") or {}).items():
            hits = sorted(
                {
                    m.group(0)
                    for p in NO_REFERENCE_FORBIDDEN
                    for m in p.finditer(str(text))
                }
            )
            if hits:
                errors.append(
                    f"prompts/{family} 出现了 {hits}：文生图的模型看不到参考图，"
                    "要把成品完整描述出来"
                )
        return errors

    return check


def prompt_for_slot(render: dict[str, Any], model: str, mode: str) -> str:
    """按出图方式与模型家族取提示词；旧任务只有一份 final_prompt 时文生图回落到它。"""
    family = imagegen.model_family(model)
    if mode == "i2i":
        text = (render.get("i2i_prompts") or {}).get(family)
        if not text:
            raise StageError(f"任务缺少 {family} 的图生图提示词，要重新编译（S06）")
        return str(text)
    prompts = render.get("prompts") or {}
    return str(prompts.get(family) or render["final_prompt"])


# ---------------------------------------------------------------- 状态判断


def _slot_calls(
    ctx: Ctx, job_id: str, slot_id: str, stage: str = "T01"
) -> list[dict[str, Any]]:
    rows = ctx.db.q(
        "SELECT * FROM provider_calls WHERE scope_id = ? AND kind = 'image' AND stage = ?"
        f" AND {ctx.db.json_text('request_meta_json', 'slot_id')} = ? ORDER BY call_id",
        (job_id, stage, slot_id),
    )
    return [dict(r) for r in rows]


def _live(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """计入尝试次数的调用：人工作废（voided）的失败调用不算。幂等键编号仍按全部调用递增。"""
    return [
        c for c in calls if not (loads(c["request_meta_json"], {}) or {}).get("voided")
    ]


# 素材状态 → 下一步，按顺序取第一个有素材的状态
_ASSET_STEPS = (
    ("generated", "S07"),
    ("repair_pending", "T01R"),
    ("qc_pass", "S09"),
    # 下线近重复判定前已判「非重复」、还没预检的旧素材
    ("distinct", "S09"),
    ("preview_ready", "S10"),
)


def step_for(
    has_render: bool, pending_slots: int, asset_counts: Mapping[str, int]
) -> str | None:
    """看状态决定下一步；任务视图也用它推算正在跑的任务走到哪了（jobs.stage 只在交还任务时更新）。"""
    if not has_render:
        return "S06"
    if pending_slots:
        return "T01"
    for status, step in _ASSET_STEPS:
        if asset_counts.get(status):
            return step
    return None


def next_step(ctx: Ctx, job: dict[str, Any]) -> str | None:
    if not job["render_spec_json"]:
        return "S06"
    pending = ctx.db.scalar(
        "SELECT COUNT(*) FROM job_slots WHERE job_id = ? AND status = 'pending'",
        (job["job_id"],),
    )
    counts = Counter(a["status"] for a in _assets(ctx, job["job_id"]))
    return step_for(True, int(pending or 0), counts)


# ---------------------------------------------------------------- S06


def _job_slots(ctx: Ctx, job_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM job_slots WHERE job_id = ? ORDER BY slot_id", (job_id,)
        )
    ]


def sync_slots(
    ctx: Ctx,
    job: dict[str, Any],
    seed: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """出图前按当前配置与参考图授权核对槽位，返回 (本任务的出图方式, 说明)。

    放在 S06 而不只在裂变时建：裂变后才改了出图方式、生图模型或授权的任务，都按开始出图时的配置来。
    目标是 fanout_slots(这个方向的出图方式，再按授权过滤)：小变化按 small_change_modes（缺省只做图生图），
    其余按 image_generation.modes；缺的补上，不在目标里、还没发过请求的删掉；
    发过请求或出过图的槽位保留（退役模型在发请求时换成接替模型）。
    """
    if plan is None:
        row = ctx.db.q1(
            "SELECT plan_json FROM variants WHERE variant_id = ?", (job["variant_id"],)
        )
        plan = loads(row["plan_json"], {}) if row else {}
    modes = variant_modes(ctx, plan)
    notes = []
    if "i2i" in modes and seed["rights_status"] == "restricted":
        modes = [m for m in modes if m != "i2i"]
        notes.append(f"参考图 {seed['seed_id']} 授权受限：本任务不做图生图")
    wanted = fanout_slots(ctx, modes)
    wanted_ids = {slot_id for slot_id, _, _ in wanted}
    slots = _job_slots(ctx, job["job_id"])
    have = {s["slot_id"] for s in slots}
    added, removed = [], []
    with ctx.db.tx() as conn:
        for slot_id, model, _ in wanted:
            if slot_id not in have:
                conn.execute(
                    "INSERT INTO job_slots(job_id, slot_id, model, status, updated_at) VALUES(?, ?, ?, 'pending', ?)",
                    (job["job_id"], slot_id, model, now_iso()),
                )
                added.append(slot_id)
        for s in slots:
            if (
                s["slot_id"] not in wanted_ids
                and s["status"] == "pending"
                and not s["attempts"]
            ):
                conn.execute(
                    "DELETE FROM job_slots WHERE job_id = ? AND slot_id = ?",
                    (job["job_id"], s["slot_id"]),
                )
                removed.append(s["slot_id"])
        if added or removed:
            log_event(
                conn,
                job["job_id"],
                "slots_synced",
                {"added": added, "removed": removed},
            )
    present = {slot_mode(s["slot_id"]) for s in _job_slots(ctx, job["job_id"])}
    return [m for m in GEN_MODES if m in present], notes


def reference_traits(fam: dict[str, Any]) -> dict[str, Any] | None:
    """参考图特征（单幅 / 花型、有没有文字），系列规则按新字段拆过才有。"""
    return ((fam.get("spec") or {}).get("variation") or {}).get("traits")


def step_s06(ctx: Ctx, job: dict[str, Any]) -> None:
    variant = variant_view(ctx, job["variant_id"])
    fam = get_family(ctx, variant["family_id"], variant["family_version"])
    seed = get_seed(ctx, fam["seed_id"])
    modes, notes = sync_slots(ctx, job, seed, variant)
    if not modes:
        if variant.get("task_type") == "small":
            raise StageError("小变化只做图生图，参考图授权受限不能做：这个方向出不了图")
        raise StageError("本任务没有可出图的槽位：图生图因授权受限撤掉了，又没开文生图")
    use_i2i = "i2i" in modes
    # -edit 槽位和原模型共用一套提示词：P06 只看原模型（退役的按接替模型算）
    capabilities = generator_capabilities(
        ctx,
        [
            imagegen.current_model(ctx, budget.base_model(s["model"]))
            for s in _job_slots(ctx, job["job_id"])
        ],
        modes,
    )
    # 二次大变化的输入图是底图（同一系列一个小变化出的图）；授权受限的参考图不附图，只给文字档案
    base_asset = base_asset_id(variant)
    reference = (
        []
        if seed["rights_status"] == "restricted"
        else [
            vision_image(
                ctx,
                input_image_bytes(ctx, seed, variant),
                f"底图 {base_asset}（{variant['base'].get('variant_id')} 小变化出的图，主体已按 base.changes 换过）："
                "图生图的输入图，文生图的模型看不到它"
                if base_asset
                else f"{seed['seed_id']} 参考图：只用来看清画面、把提示词写准（文生图的模型看不到它）",
            )
        ]
    )
    input_ref = base_asset or f"seed:{seed['seed_id']}"
    out = llm.run_stage(
        ctx,
        "P06",
        scope_id=job["job_id"],
        job_id=job["job_id"],
        variables={
            "variant_job": variant,
            "family_spec": fam["spec"],
            "reference_profile": reference_profile(
                ctx,
                fam.get("analysis_id") or fam["spec"].get("analysis_id"),
                english=True,
            ),
            "reference_assets": [
                {
                    "asset_id": input_ref,
                    "rights_status": seed["rights_status"],
                    "used_as": "图生图的输入图（底图：主体已经换过的小变化图）"
                    if base_asset
                    else "图生图的输入图",
                }
            ]
            if use_i2i
            else [],
            "generator_capabilities": capabilities,
            "reference_traits": reference_traits(fam)
            or {"note": "梳理字段之前拆的系列，没有记录参考图特征"},
            "optional_sku_template": None,
        },
        images={"reference_image": reference},
        extra_validate=_validate_p06(capabilities["prompt_keys"], modes),
        normalize=_normalize_p06(variant),
    )
    notes += out.notes
    if out.envelope.get("status") in ("blocked", "failed"):
        reasons = [str(u) for u in out.envelope.get("unknowns") or []][:3]
        reasons += [str(out.envelope.get("next_action") or "")]
        raise StageError(
            f"生成指令没写出来（P06 返回 {out.envelope['status']}，没有出图）："
            + "；".join(r for r in reasons if r.strip())[:300]
        )
    spec = out.result
    prompt_sets = {
        key: {
            family: str(text).strip()
            for family, text in (spec.get(key) or {}).items()
            if str(text).strip()
        }
        for key in MODE_PROMPT_KEYS.values()
    }
    # 只做图生图的任务（小变化）用不到文生图提示词，模型顺手写了也不留
    prompts = prompt_sets["prompts"] if "t2i" in modes else {}
    spec["variant_id"] = variant["variant_id"]
    spec["output_level"] = "preview"
    spec["reference_asset_ids"] = [input_ref] if use_i2i else []
    if (spec.get("text_overlay") or {}).get("enabled"):
        notes.append(
            "text_overlay 暂不做精确排版：文字由生图模型照提示词画，姓名等精确文字后续独立排版"
        )
    i2i_prompts = prompt_sets["i2i_prompts"] if use_i2i else {}
    render = {
        "result": spec,
        "modes": modes,
        "prompts": prompts,
        "i2i_prompts": i2i_prompts,
        "reference_seed_id": seed["seed_id"] if use_i2i else None,
        # 二次大变化：图生图的输入图是这张底图（不是参考图）
        "input_asset_id": base_asset if use_i2i else None,
        # 文生图旧任务的回落提示词；只做图生图的小变化没有文生图提示词，记一份图生图的方便查看
        "final_prompt": prompts.get("gemini")
        or next(iter(prompts.values()), "")
        or str(spec.get("positive_prompt") or "").strip()
        or i2i_prompts.get("gemini")
        or next(iter(i2i_prompts.values()), ""),
        "notes": notes,
        "model": out.model,
        "prompt_version": out.prompt_version,
        "call_ids": out.call_ids,
    }
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET render_spec_json = ?, stage = 'T01', updated_at = ? WHERE job_id = ?",
            (dumps(render), now_iso(), job["job_id"]),
        )
        log_event(
            conn,
            job["job_id"],
            "render_spec_saved",
            {"prompt_version": out.prompt_version},
        )


# ---------------------------------------------------------------- T01 / 素材落库


def save_asset(
    ctx: Ctx,
    job: dict[str, Any],
    *,
    slot_id: str,
    model: str,
    data: bytes,
    call_id: str | None,
    parent: dict[str, Any] | None = None,
) -> str | None:
    fmt, width, height = images.probe(data)
    sha = sha256_bytes(data)
    thumb = images.thumbnail_jpeg(data)
    variant = ctx.db.q1(
        "SELECT family_id FROM variants WHERE variant_id = ?", (job["variant_id"],)
    )
    folder = store.asset_dir(ctx.home, variant["family_id"], job["variant_id"])
    ext = images.FORMAT_EXT.get(fmt, "png")
    with ctx.db.tx() as conn:
        # 查重放在写锁内：并发的几路同时返回同一份字节时只落一份
        if conn.execute("SELECT 1 FROM assets WHERE sha256 = ?", (sha,)).fetchone():
            return None
        asset_id = next_id(conn, "asset")
        path = store.save(ctx, conn, folder / f"{asset_id}.{ext}", data)
        thumb_path = store.save(ctx, conn, folder / f"{asset_id}.thumb.jpg", thumb)
        conn.execute(
            "INSERT INTO assets(asset_id, job_id, variant_id, slot_id, model, asset_version, parent_asset_id, repair_depth,"
            " call_id, path, thumb_path, sha256, width, height, bytes, format, status, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?, ?)",
            (
                asset_id,
                job["job_id"],
                job["variant_id"],
                slot_id,
                model,
                (parent["asset_version"] + 1) if parent else 1,
                parent["asset_id"] if parent else None,
                (parent["repair_depth"] + 1) if parent else 0,
                call_id,
                store.rel(ctx.home, path),
                store.rel(ctx.home, thumb_path),
                sha,
                width,
                height,
                len(data),
                fmt,
                now_iso(),
                now_iso(),
            ),
        )
        if call_id:
            conn.execute(
                "UPDATE provider_calls SET asset_id = COALESCE(asset_id, ?) WHERE call_id = ?",
                (asset_id, call_id),
            )
        log_event(
            conn,
            asset_id,
            "asset_saved",
            {
                "job_id": job["job_id"],
                "slot_id": slot_id,
                "model": model,
                "parent": parent and parent["asset_id"],
            },
        )
    store.kick(ctx)
    return asset_id


def _attempt_limit(ctx: Ctx) -> int:
    return int(ctx.setting("batch.transient_retry_limit", 3)) + 1


def step_t01(ctx: Ctx, job: dict[str, Any]) -> None:
    render = loads(job["render_spec_json"])
    slots = [
        dict(r)
        for r in ctx.db.q(
            "SELECT * FROM job_slots WHERE job_id = ? AND status = 'pending'",
            (job["job_id"],),
        )
    ]
    reference = None
    if any(slot_mode(s["slot_id"]) == "i2i" for s in slots):
        if render.get("input_asset_id"):
            data = asset_bytes(ctx, render["input_asset_id"])
        else:
            seed = get_seed(ctx, get_family(ctx, *_family_of(ctx, job))["seed_id"])
            data = seed_bytes(ctx, seed)
        reference = images.to_vision_jpeg(data, REFERENCE_MAX_SIDE)
    limit = _attempt_limit(ctx)

    def run(slot: dict[str, Any]) -> None:
        all_calls = _slot_calls(ctx, job["job_id"], slot["slot_id"])
        calls = _live(all_calls)
        if any(c["status"] in ("reserved", "submitted", "unknown") for c in calls):
            _set_slot(
                ctx,
                job,
                slot["slot_id"],
                status="unknown",
                last_error="已有未结清的请求，等待对账",
            )
            return
        attempt = len(calls) + 1
        if attempt > limit:
            _set_slot(
                ctx,
                job,
                slot["slot_id"],
                status="failed",
                last_error=f"已尝试 {len(calls)} 次",
            )
            return
        if attempt > 1:
            _sleep(min(10.0 * 2 ** (attempt - 2), 60.0))
        i2i = slot_mode(slot["slot_id"]) == "i2i"
        model = imagegen.current_model(ctx, slot["model"])
        res = imagegen.generate(
            ctx,
            scope_id=job["job_id"],
            stage="T01",
            model=model,
            prompt=prompt_for_slot(render, model, "i2i" if i2i else "t2i"),
            idempotency_key=f"img:{job['job_id']}:{slot['slot_id']}:a{len(all_calls) + 1}",
            slot_id=slot["slot_id"],
            input_images=[reference] if i2i else None,
        )
        if res.status == "ok" and res.data:
            asset_id = save_asset(
                ctx,
                job,
                slot_id=slot["slot_id"],
                model=model,
                data=res.data,
                call_id=res.call_id,
            )
            if asset_id:
                _set_slot(
                    ctx,
                    job,
                    slot["slot_id"],
                    status="generated",
                    asset_id=asset_id,
                    attempts=attempt,
                )
            else:
                _set_slot(
                    ctx,
                    job,
                    slot["slot_id"],
                    status="failed",
                    attempts=attempt,
                    last_error="与已有素材字节完全相同",
                )
        elif res.status == "unknown":
            _set_slot(
                ctx,
                job,
                slot["slot_id"],
                status="unknown",
                attempts=attempt,
                last_error=res.error,
            )
            with ctx.db.tx() as conn:
                open_review(
                    conn,
                    "call_reconcile",
                    res.call_id,
                    f"{slot['model']}（{MODE_LABELS['i2i' if i2i else 't2i']}）请求已提交但结果未知：按请求查供应商记录，用 podctl reconcile 登记是否扣费",
                    {
                        "job_id": job["job_id"],
                        "slot_id": slot["slot_id"],
                        "error": res.error,
                    },
                    batch_id=job["batch_id"],
                )
        elif res.transient and attempt < limit:
            _set_slot(
                ctx,
                job,
                slot["slot_id"],
                status="pending",
                attempts=attempt,
                last_error=res.error,
            )
        else:
            _set_slot(
                ctx,
                job,
                slot["slot_id"],
                status="failed",
                attempts=attempt,
                last_error=res.error,
            )

    _parallel(ctx, slots, run)


def _family_of(ctx: Ctx, job: dict[str, Any]) -> tuple[str, int]:
    row = ctx.db.q1(
        "SELECT family_id, family_version FROM variants WHERE variant_id = ?",
        (job["variant_id"],),
    )
    return row["family_id"], row["family_version"]


def _set_slot(ctx: Ctx, job: dict[str, Any], slot_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with ctx.db.tx() as conn:
        conn.execute(
            f"UPDATE job_slots SET {sets}, updated_at = ? WHERE job_id = ? AND slot_id = ?",
            (*fields.values(), now_iso(), job["job_id"], slot_id),
        )
        log_event(
            conn,
            job["job_id"],
            "slot_updated",
            {
                "slot_id": slot_id,
                **{k: v for k, v in fields.items() if k in ("status", "last_error")},
            },
        )


# ---------------------------------------------------------------- S07 质检


def file_checks(ctx: Ctx, asset: dict[str, Any]) -> dict[str, Any]:
    path = store.ensure_local(ctx, asset["path"])
    if not path.exists():
        return {"exists": False, "sha256_match": False}
    data = path.read_bytes()
    fmt, width, height = images.probe(data)
    ratio_text, ratio = target_ratio(ctx)
    actual = width / height
    deviation = abs(actual - ratio) / ratio
    return {
        "exists": True,
        "sha256_match": sha256_bytes(data) == asset["sha256"],
        "format": fmt,
        "width": width,
        "height": height,
        "aspect_ratio": round(actual, 4),
        "target_ratio": ratio_text,
        "ratio_deviation": round(deviation, 4),
        "ratio_ok": deviation <= 0.03,
        "min_side_px": min(width, height),
    }


SYSTEMIC_NOTE = (
    "流水线没有接入 OCR、商标或既有角色检索工具：这类检查标 unverified 即可，"
    "它们由人工选款环节兜底，不要仅因为它们就判 needs_review；判 needs_review 只用于画面里能指出的具体疑点"
)
# 设计上就无法由工具验证的检查项（文字精确性、商标 / 角色检索、资产状态）
SYSTEMIC_KEYWORDS = (
    "文字",
    "OCR",
    "ocr",
    "姓名",
    "日期",
    "标语",
    "商标",
    "品牌",
    "既有角色",
    "角色特征",
    "受保护",
    "权利",
    "原创性",
    "preview_ready",
    "生产适配",
    "印刷验证",
    "资产状态",
    "状态字段",
)


def _is_systemic(check: dict[str, Any]) -> bool:
    text = f"{check.get('criterion') or ''} {check.get('reason') or ''}"
    return any(keyword in text for keyword in SYSTEMIC_KEYWORDS)


def qc_asset(
    ctx: Ctx,
    job: dict[str, Any],
    asset: dict[str, Any],
    variant: dict[str, Any],
    fam: dict[str, Any],
    reference: list[Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> None:
    """reference 是参考图（只有小变化给）：对照它核对构图没有漂移、只改了目标字段。

    profile 是参考图的画面档案：核对画风统一（新内容与主体同一套画法）、主体还是参考图那几只。
    """
    checks = _file_ok(ctx, asset)
    if checks is None:
        return
    previous = loads(asset["qc_json"], {}) or {}
    try:
        out = llm.run_stage(
            ctx,
            "P07",
            scope_id=job["job_id"],
            job_id=job["job_id"],
            asset_id=asset["asset_id"],
            variables={
                "variant_job": variant,
                "family_spec": fam["spec"],
                "reference_profile": profile
                or reference_profile(
                    ctx, fam.get("analysis_id") or fam["spec"].get("analysis_id")
                ),
                "file_check_results": {
                    **checks,
                    "asset_status_ceiling": "preview_ready（由程序强制限定，不会标记生产适配或印刷验证，本阶段无需核对）",
                },
                "ocr_results": {
                    "available": False,
                    "trademark_or_character_search": False,
                    "note": SYSTEMIC_NOTE,
                },
            },
            images={
                "candidate_image": [
                    vision_image(
                        ctx,
                        _asset_bytes(ctx, asset),
                        f"{asset['asset_id']}（{asset['model']}）",
                    )
                ],
                "reference_image": reference or [],
            },
        )
    except llm.LLMResultUnknown as exc:
        attempts = int(previous.get("qc_attempts", 0)) + 1
        if attempts >= 2:
            _qc_unchecked(ctx, asset, f"质检调用两次结果未知：{exc}", checks)
        else:
            _set_asset(
                ctx,
                asset["asset_id"],
                qc_json=dumps({"qc_attempts": attempts, "last_error": str(exc)}),
            )
        return
    except (llm.StageOutputInvalid, llm.LLMPermanentError) as exc:
        _qc_unchecked(ctx, asset, f"质检调用失败：{exc}", checks)
        return
    result = out.result
    result["asset_id"] = asset["asset_id"]
    decision = result["decision"]
    failed_checks = [c for c in result["checks"] if c.get("verdict") == "fail"]
    unverified = [c for c in result["checks"] if c.get("verdict") == "unverified"]
    override = None
    if (
        decision == "needs_review"
        and not failed_checks
        and unverified
        and all(_is_systemic(c) for c in unverified)
    ):
        decision = "pass"
        override = "只有系统性未验证项（无 OCR / 商标与角色检索 / 状态由程序限定），由人工选款环节兜底"
    if decision == "pass" and failed_checks:
        decision = "repair" if result.get("repair_instructions") else "needs_review"
        override = "存在 fail 的检查项，不能判通过"
    if decision == "repair":
        if asset["repair_depth"] >= int(ctx.setting("batch.repair_limit", 2)):
            decision, override = "reject", "修复次数已用尽，淘汰"
        elif not result.get("repair_instructions"):
            decision, override = (
                "needs_review",
                "判定需要修复但没有给出可执行的修复说明",
            )
    qc = {
        "decision": decision,
        "model_decision": result["decision"],
        "override": override,
        "first_pass": asset["repair_depth"] == 0,
        "result": result,
        "file_checks": checks,
        "model": out.model,
        "prompt_version": out.prompt_version,
        "call_ids": out.call_ids,
    }
    if decision == "needs_review":
        # 拿不准不再停下等人：带着疑点照常预检、归档，在选款时由人一起看
        doubts = failed_checks + [c for c in unverified if not _is_systemic(c)]
        qc["flagged"] = True
        qc["flags"] = [
            f"{c.get('criterion') or '检查项'}：{c.get('reason') or c.get('verdict')}"
            for c in doubts
        ] or [override or "质检判定需要人工确认"]
    status = {
        "pass": "qc_pass",
        "repair": "repair_pending",
        "reject": "rejected",
        "needs_review": "qc_pass",
    }[decision]
    fields: dict[str, Any] = {"status": status, "qc_json": dumps(qc)}
    if decision == "repair":
        fields["repair_instructions_json"] = dumps(result["repair_instructions"])
    _set_asset(ctx, asset["asset_id"], **fields)


def _file_ok(ctx: Ctx, asset: dict[str, Any]) -> dict[str, Any] | None:
    """程序的文件检查（不调模型）：缺文件或哈希不符的记失败、返回 None，否则返回检查结果。"""
    checks = file_checks(ctx, asset)
    if checks["exists"] and checks["sha256_match"]:
        return checks
    _set_asset(
        ctx,
        asset["asset_id"],
        status="failed",
        qc_json=dumps(
            {
                "decision": "failed",
                "file_checks": checks,
                "reason": "文件缺失或哈希不符",
            }
        ),
    )
    return None


def _qc_unchecked(
    ctx: Ctx,
    asset: dict[str, Any],
    reason: str,
    checks: dict[str, Any],
) -> None:
    """质检调用失败：不卡住，标「未经质检」照常进选款。"""
    _set_asset(
        ctx,
        asset["asset_id"],
        status="qc_pass",
        qc_json=dumps(
            {
                "decision": "unchecked",
                "flagged": True,
                "reason": reason,
                "file_checks": checks,
            }
        ),
    )


def step_s07(ctx: Ctx, job: dict[str, Any]) -> None:
    pending = _assets(ctx, job["job_id"], "generated")
    if not qc_enabled(ctx):
        # 质检暂停：只做程序的文件检查，不调模型，标「未经质检」照常进选款
        for asset in pending:
            checks = _file_ok(ctx, asset)
            if checks is not None:
                _qc_unchecked(ctx, asset, QC_OFF_REASON, checks)
        return
    variant = variant_view(ctx, job["variant_id"])
    fam = get_family(ctx, variant["family_id"], variant["family_version"])
    reference = None
    if pending and variant.get("task_type") == "small":
        seed = get_seed(ctx, fam["seed_id"])
        reference = [
            vision_image(
                ctx,
                seed_bytes(ctx, seed),
                f"{seed['seed_id']} 参考图原图：小变化要保持它的构图",
            )
        ]
    profile = reference_profile(
        ctx, fam.get("analysis_id") or fam["spec"].get("analysis_id")
    )
    _parallel(
        ctx,
        pending,
        lambda a: qc_asset(ctx, job, a, variant, fam, reference, profile),
    )


# ---------------------------------------------------------------- T01R 定点修复


def repair_prompt(
    render: dict[str, Any],
    variant: dict[str, Any],
    instructions: list[Any],
    *,
    human: bool = False,
) -> str:
    head = (
        "以输入图为底图，按下面的修改意见改；意见没提到的部分（构图、角色、配色、技法）保持不变。"
        if human
        else "以输入图为底图做定点修正：构图、角色身份、配色体系和绘制技法保持不变，只修正下面列出的问题。"
    )
    lines = [
        head,
        *[
            f"{i}. {item if isinstance(item, str) else dumps(item)}"
            for i, item in enumerate(instructions, 1)
        ],
    ]
    preserve = variant.get("preserve") or []
    # 人工意见可以有意改动保留项，以人的意见为准
    if preserve and not human:
        lines.append(
            "修正后仍须满足："
            + "；".join(p if isinstance(p, str) else dumps(p) for p in preserve)
        )
    lines.append("输出仍是完整的平面印花设计稿，画面只包含图案本身。")
    return "\n".join(lines)


def _back_to_selection(ctx: Ctx, asset: dict[str, Any], reason: str) -> None:
    """人工要求的修改没做成：原图放回选款（S10 重开选款项）并写明原因，不淘汰。"""
    qc = loads(asset["qc_json"], {}) or {}
    qc["fix_failed"] = reason
    _set_asset(ctx, asset["asset_id"], status="preview_ready", qc_json=dumps(qc))


def step_repair(ctx: Ctx, job: dict[str, Any]) -> None:
    render = loads(job["render_spec_json"])
    variant = variant_view(ctx, job["variant_id"])
    limit = _attempt_limit(ctx)

    def run(asset: dict[str, Any]) -> None:
        human = is_human_fix(asset)
        if not human and not qc_enabled(ctx):
            # 质检暂停后不再自动修复：之前判了修复的图原样进选款（选款时人工要求的修改照常做）
            previous = loads(asset["qc_json"], {}) or {}
            _set_asset(
                ctx,
                asset["asset_id"],
                status="qc_pass",
                qc_json=dumps(
                    {
                        "decision": "unchecked",
                        "flagged": True,
                        "reason": f"{QC_OFF_REASON}；之前判了修复，没修直接进选款",
                        "file_checks": previous.get("file_checks"),
                        "previous": {
                            k: previous[k]
                            for k in ("decision", "result")
                            if k in previous
                        },
                    }
                ),
            )
            return
        all_calls = [
            dict(r)
            for r in ctx.db.q(
                "SELECT * FROM provider_calls WHERE scope_id = ? AND stage = 'T01R'"
                f" AND {ctx.db.json_text('request_meta_json', 'asset_hint')} = ?",
                (job["job_id"], asset["asset_id"]),
            )
        ]
        calls = _live(all_calls)
        if any(c["status"] in ("reserved", "submitted", "unknown") for c in calls):
            _set_asset(ctx, asset["asset_id"], status="repair_unknown")
            return
        attempt = len(calls) + 1
        key_seq = len(all_calls) + 1
        if attempt > limit:
            if human:
                _back_to_selection(ctx, asset, f"已尝试 {len(calls)} 次仍未改成")
            else:
                _set_asset(ctx, asset["asset_id"], status="rejected")
            return
        if attempt > 1:
            _sleep(min(10.0 * 2 ** (attempt - 2), 60.0))
        instructions = loads(asset["repair_instructions_json"], []) or []
        # 退役模型出的旧图改用接替模型修，新图记实际用的模型
        model = imagegen.current_model(ctx, asset["model"])
        res = imagegen.generate(
            ctx,
            scope_id=job["job_id"],
            stage="T01R",
            model=model,
            prompt=repair_prompt(render, variant, instructions, human=human),
            idempotency_key=f"img:{job['job_id']}:{asset['slot_id']}:repair:{asset['asset_id']}:a{key_seq}",
            slot_id=asset["slot_id"],
            input_images=[_asset_bytes(ctx, asset)],
            asset_hint=asset["asset_id"],
        )
        if res.status == "ok" and res.data:
            child = save_asset(
                ctx,
                job,
                slot_id=asset["slot_id"],
                model=model,
                data=res.data,
                call_id=res.call_id,
                parent=asset,
            )
            if child and human:
                # 人工修改版直接回选款：人马上就会看这张图，不再自动质检（也就不会被自动修复或淘汰）
                _set_asset(
                    ctx,
                    child,
                    status="qc_pass",
                    qc_json=dumps(
                        {
                            "decision": "pass",
                            "override": "人工修改版：不再自动质检，直接回选款",
                            "human_fix": "；".join(map(str, instructions)),
                            "fixed_from": asset["asset_id"],
                        }
                    ),
                )
            if child:
                _set_asset(ctx, asset["asset_id"], status="superseded")
            elif human:
                _back_to_selection(ctx, asset, "改出来的图与已有素材字节完全相同")
            else:
                _set_asset(ctx, asset["asset_id"], status="rejected")
        elif res.status == "unknown":
            _set_asset(ctx, asset["asset_id"], status="repair_unknown")
            with ctx.db.tx() as conn:
                open_review(
                    conn,
                    "call_reconcile",
                    res.call_id,
                    "修复请求已提交但结果未知：查供应商记录后用 podctl reconcile 登记",
                    {"job_id": job["job_id"], "asset_id": asset["asset_id"]},
                    batch_id=job["batch_id"],
                )
        elif not (res.transient and attempt < limit):
            if human:
                _back_to_selection(ctx, asset, f"修改失败：{res.error}")
            else:
                _set_asset(
                    ctx,
                    asset["asset_id"],
                    status="rejected",
                    qc_json=_with_note(asset, f"修复失败：{res.error}"),
                )

    _parallel(ctx, _assets(ctx, job["job_id"], "repair_pending"), run)


def is_human_fix(asset: dict[str, Any]) -> bool:
    """选款时人工要求的修改（区别于质检自动判定的修复）。"""
    return asset["human_decision"] == "needs_changes"


def _with_note(asset: dict[str, Any], note: str) -> str:
    qc = loads(asset["qc_json"], {}) or {}
    qc.setdefault("notes", []).append(note)
    return dumps(qc)


# ---------------------------------------------------------------- S09 / S10


def _lineage_ids(ctx: Ctx, asset: dict[str, Any]) -> set[str]:
    ids = {asset["asset_id"]}
    parent = asset["parent_asset_id"]
    while parent:
        ids.add(parent)
        row = ctx.db.q1(
            "SELECT parent_asset_id FROM assets WHERE asset_id = ?", (parent,)
        )
        parent = row["parent_asset_id"] if row else None
    return ids


def step_s09(ctx: Ctx, job: dict[str, Any]) -> None:
    for asset in [
        *_assets(ctx, job["job_id"], "qc_pass"),
        *_assets(ctx, job["job_id"], "distinct"),
    ]:
        checks = file_checks(ctx, asset)
        actions = []
        if not checks.get("ratio_ok"):
            actions.append(
                f"画幅 {checks.get('aspect_ratio')} 与目标 {checks.get('target_ratio')} 偏差 {checks.get('ratio_deviation', 0):.1%}："
                "上模板前需重排或延展背景，不能非等比拉伸角色"
            )
        actions.append(
            "未配置供应商 SKU 模板：像素、DPI、出血、安全区要在拿到模板后复检"
        )
        report = {
            "asset_id": asset["asset_id"],
            "sku_id": None,
            "template_version": None,
            "readiness": "preview_ready",
            "checks": checks,
            "adaptation_actions": actions,
            "print_file_ref": None,
            "mockup_ref": None,
            "physical_sample_status": "not_tested",
        }
        _set_asset(
            ctx, asset["asset_id"], status="preview_ready", print_json=dumps(report)
        )


def asset_costs(ctx: Ctx, asset: dict[str, Any]) -> dict[str, Any]:
    lineage = _lineage_ids(ctx, asset)
    marks = ",".join("?" for _ in lineage)
    calls = [
        dict(r)
        for r in ctx.db.q(
            f"SELECT call_id, kind, stage, model, cost_status, reserved_cost, actual_cost, currency FROM provider_calls"
            f" WHERE asset_id IN ({marks}) OR {ctx.db.json_text('request_meta_json', 'asset_hint')} IN ({marks})",
            (*lineage, *lineage),
        )
    ]
    total = 0.0
    statuses = set()
    for c in calls:
        statuses.add(c["cost_status"])
        total += (
            (c["actual_cost"] or 0)
            if c["cost_status"] in ("settled", "estimated", "reconciled")
            else (
                c["reserved_cost"] if c["cost_status"] in ("reserved", "unknown") else 0
            )
        )
    if "unknown" in statuses:
        status = "unknown"
    elif "untracked" in statuses:
        # 预算控制关闭期间的调用：只有次数没有金额
        return {"calls": calls, "total": None, "cost_status": "untracked"}
    elif statuses & {"reserved", "estimated"}:
        status = "pending"
    else:
        status = "settled"
    return {"calls": calls, "total": round(total, 6), "cost_status": status}


def step_s10(ctx: Ctx, job: dict[str, Any]) -> None:
    variant = variant_view(ctx, job["variant_id"])
    fam = get_family(ctx, variant["family_id"], variant["family_version"])
    render = loads(job["render_spec_json"])
    for asset in _assets(ctx, job["job_id"], "preview_ready"):
        checks = file_checks(ctx, asset)
        if not checks["exists"] or not checks["sha256_match"]:
            _set_asset(
                ctx,
                asset["asset_id"],
                status="failed",
                archive_json=dumps({"error": "归档前文件缺失或哈希不符"}),
            )
            continue
        chain = []
        parent = asset["parent_asset_id"]
        while parent:
            chain.append(parent)
            row = ctx.db.q1(
                "SELECT parent_asset_id FROM assets WHERE asset_id = ?", (parent,)
            )
            parent = row["parent_asset_id"] if row else None
        call = (
            ctx.db.q1(
                "SELECT request_meta_json FROM provider_calls WHERE call_id = ?",
                (asset["call_id"],),
            )
            if asset["call_id"]
            else None
        )
        qc = loads(asset["qc_json"], {}) or {}
        manifest = {
            "asset_id": asset["asset_id"],
            "files": {
                "image": asset["path"],
                "thumbnail": asset["thumb_path"],
                "sha256": asset["sha256"],
                "width": asset["width"],
                "height": asset["height"],
                "format": asset["format"],
            },
            "lineage": {
                "seed_id": fam["seed_id"],
                "analysis_id": fam["analysis_id"],
                "family": f"{fam['family_id']}@v{fam['family_version']}",
                "batch_id": job["batch_id"],
                "variant_id": job["variant_id"],
                "parent_variant_id": variant.get("parent_variant_id"),
                "job_id": job["job_id"],
                "asset_version": asset["asset_version"],
                "repair_chain": chain,
            },
            "generation": {
                "model": asset["model"],
                "mode": slot_mode(asset["slot_id"]),
                "slot_id": asset["slot_id"],
                "call_id": asset["call_id"],
                "request": loads(call["request_meta_json"], {}) if call else None,
                "render_spec_prompt_version": render.get("prompt_version"),
                "pipeline_version": job["pipeline_version"],
            },
            "difference_summary": {
                "planned_changes": variant.get("changed_fields"),
                "observed_changes": (qc.get("result") or {}).get(
                    "changed_fields_observed"
                ),
                "unintended_changes": (qc.get("result") or {}).get(
                    "unintended_changes"
                ),
            },
            "quality_status": qc.get("decision"),
            "print_status": "preview_ready",
            "human_decision": "pending",
            "market_validation": "not_tested",
            "costs": asset_costs(ctx, asset),
            "archived_at": now_iso(),
        }
        manifest_path = store.resolve(ctx.home, asset["path"]).with_suffix(
            ".manifest.json"
        )
        if not manifest_path.exists():
            with ctx.db.tx() as conn:
                store.save(
                    ctx, conn, manifest_path, dumps(manifest, indent=2).encode("utf-8")
                )
        _set_asset(
            ctx,
            asset["asset_id"],
            status="archived",
            archive_json=dumps(manifest),
            human_decision="pending",
        )
        with ctx.db.tx() as conn:
            open_review(
                conn,
                "asset_selection",
                asset["asset_id"],
                "候选已入库，等待人工选款",
                {
                    "model": asset["model"],
                    "qc_override": qc.get("override"),
                    "unverified_checks": [
                        c.get("criterion")
                        for c in (qc.get("result") or {}).get("checks", [])
                        if c.get("verdict") == "unverified"
                    ],
                    "qc_flags": qc.get("flags") or [],
                    "qc_unchecked": qc.get("reason")
                    if qc.get("decision") == "unchecked"
                    else None,
                    "fixed_from": qc.get("fixed_from"),
                    "fix_note": qc.get("human_fix"),
                    "fix_failed": qc.get("fix_failed"),
                },
                batch_id=job["batch_id"],
            )
    store.kick(ctx)
    # 进了选款就进候选表（table.enabled 时）
    feishu_table.kick(ctx)


STEPS: dict[str, Callable[[Ctx, dict[str, Any]], None]] = {
    "S06": step_s06,
    "T01": step_t01,
    "S07": step_s07,
    "T01R": step_repair,
    "S09": step_s09,
    "S10": step_s10,
}


# ---------------------------------------------------------------- 推进


def _retry_s06(ctx: Ctx, job_id: str, exc: llm.StageOutputInvalid) -> bool:
    """P06 几次重答都没过：同一个任务（最近一次重出之后）按 llm.s06_job_retries 放回队列，下次新开对话重写。"""
    limit = int(ctx.setting("llm.s06_job_retries", 1))
    used = int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM events WHERE scope = ? AND event = 's06_output_retry' AND event_id >"
            " COALESCE((SELECT MAX(event_id) FROM events WHERE scope = ? AND event = 'job_redo'), 0)",
            (job_id, job_id),
        )
        or 0
    )
    if used >= limit:
        return False
    with ctx.db.tx() as conn:
        log_event(
            conn,
            job_id,
            "s06_output_retry",
            {"attempt": used + 1, "errors": exc.errors[:5]},
        )
    state.release_job(
        ctx.db,
        job_id,
        status="ready",
        stage="S06",
        last_error=f"{exc}（放回队列，新开对话重写一次）",
    )
    return True


def advance_job(
    ctx: Ctx,
    job_id: str,
    *,
    deadline: dt.datetime | None = None,
    allow_generation: bool = True,
    should_stop: Callable[[], bool | str | None] | None = None,
) -> dict[str, Any]:
    """调用方须已持有该任务（running）。返回本次推进的步骤与任务最终状态。

    should_stop 在每一步开始前问一次：返回真值就把任务放回队列（ready），返回字符串时记成停下的原因
    （worker 交接、暂停、丢租约），否则记 paused。
    """
    done: list[str] = []
    job = get_job(ctx, job_id)
    if job["attempt"] > MAX_JOB_CLAIMS:
        state.release_job(
            ctx.db,
            job_id,
            status="failed",
            last_error=f"任务已被领取 {job['attempt']} 次仍未完成",
        )
        return {"job_id": job_id, "status": "failed", "steps": done}
    while True:
        job = get_job(ctx, job_id)
        step = next_step(ctx, job)
        if step is None:
            assets = _assets(ctx, job_id)
            failed_all = not assets and not ctx.db.scalar(
                "SELECT COUNT(*) FROM job_slots WHERE job_id = ? AND status IN ('generated', 'unknown')",
                (job_id,),
            )
            final = "failed" if failed_all else "complete"
            state.release_job(
                ctx.db,
                job_id,
                status=final,
                stage="done",
                last_error="所有槽位均未出图" if failed_all else None,
            )
            return {"job_id": job_id, "status": final, "steps": done}
        stop = should_stop() if should_stop else None
        if stop:
            state.release_job(ctx.db, job_id, status="ready", stage=step)
            return {
                "job_id": job_id,
                "status": "ready",
                "steps": done,
                "stopped": stop if isinstance(stop, str) else "paused",
            }
        if deadline and utcnow() >= deadline:
            state.release_job(ctx.db, job_id, status="ready", stage=step)
            return {
                "job_id": job_id,
                "status": "ready",
                "steps": done,
                "stopped": "deadline",
            }
        # 积压上限只拦新增生成；选款时人工要求的修改照常做
        human_fixes_only = step == "T01R" and all(
            is_human_fix(a) for a in _assets(ctx, job_id, "repair_pending")
        )
        if step in GEN_STEPS and not human_fixes_only:
            limit = int(ctx.setting("batch.review_backlog_limit", 50))
            if not allow_generation or backlog_count(ctx) >= limit:
                state.release_job(
                    ctx.db,
                    job_id,
                    status="blocked",
                    stage=step,
                    blocked_reason="review_backlog",
                    resume_condition=f"待人工审核积压降到 {limit} 以下",
                )
                return {
                    "job_id": job_id,
                    "status": "blocked",
                    "steps": done,
                    "blocked": "review_backlog",
                }
        try:
            STEPS[step](ctx, job)
            done.append(step)
        except budget.BudgetBlocked as exc:
            state.release_job(
                ctx.db,
                job_id,
                status="blocked",
                stage=step,
                blocked_reason=exc.reason,
                resume_condition="预算 / 单价条件恢复后自动继续"
                if exc.reason in state.RESUMABLE_BLOCKS
                else "需要人工处理",
                last_error=str(exc),
            )
            return {
                "job_id": job_id,
                "status": "blocked",
                "steps": done,
                "blocked": exc.reason,
                "detail": exc.detail,
            }
        except llm.LLMResultUnknown as exc:
            state.release_job(
                ctx.db, job_id, status="ready", stage=step, last_error=str(exc)
            )
            return {
                "job_id": job_id,
                "status": "ready",
                "steps": done,
                "error": str(exc),
            }
        except llm.StageOutputInvalid as exc:
            if step == "S06" and _retry_s06(ctx, job_id, exc):
                return {
                    "job_id": job_id,
                    "status": "ready",
                    "steps": done,
                    "error": str(exc),
                    "retry": "S06",
                }
            state.release_job(
                ctx.db, job_id, status="failed", stage=step, last_error=str(exc)
            )
            return {
                "job_id": job_id,
                "status": "failed",
                "steps": done,
                "error": str(exc),
            }
        except (llm.LLMError, StageError) as exc:
            state.release_job(
                ctx.db, job_id, status="failed", stage=step, last_error=str(exc)
            )
            return {
                "job_id": job_id,
                "status": "failed",
                "steps": done,
                "error": str(exc),
            }
