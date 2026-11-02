"""S02D 画面完整描述（P13）：拆解之后给参考图写一份画面档案（反推）——画风、配色、每个主体（物种、外形、颜色斑纹、
表情、动作、位置）、环境、构图、情绪与画风统一规则，中英两份；另列可变单元清单（change_units：会一起改的一组对象、
类型、显著度、不成立的字段），裂变按「单元 × 字段」挑改动（subjects.py）。

系列（P04）、策划（P05）、出图指令（P06）和质检（P07）都拿它（reference_profile）做依据：文生图照它把没改的部分写全，
新画进来的内容照它的画法画。按拆解存（seed_descriptions，主键 analysis_id）：重新拆解就要重新描述；老参考图用
`podctl seed describe` 补做，不动拆解和系列。没有描述时（关着、还没补、没写成）用程序从拆解拼的简版档案兜底。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import bg, fields, llm
from ..context import Ctx
from ..db import log_event
from ..util import dumps, loads, now_iso
from .common import (
    StageError,
    get_seed,
    latest_analysis,
    pattern_path,
    seed_bytes,
    vision_image,
)

DESCRIBE_STAGE = "S02D"
DESCRIBE_PROMPT = "P13"
# 人对描述的补充或更正最多多少字
HINT_CHARS = 200
NO_HINT = "没有额外要求"
DESCRIBED_SOURCE = "画面描述（S02D）"
FALLBACK_SOURCE = "拆解简版（这张图还没有画面描述，按拆解的对象与字段拼的）"
# 兜底档案带的拆解字段：主题叙事、主体、构图、配色、绘制技法、风格情绪
FALLBACK_FIELDS = (
    "D05",
    "D06",
    "D07",
    "D09",
    "D27",
    "D36",
    "D37",
    "D39",
    "D41",
    "D42",
    "D43",
    "D44",
    "D45",
    "D46",
)
# 档案里给模型看的部分（中文）；en 与 prompt_en 只给写英文提示词的 P06
PROFILE_KEYS = (
    "summary",
    "style",
    "palette",
    "subjects",
    "environment",
    "composition",
    "mood",
    "text_elements",
    "harmony_rules",
    "corrections",
)


def describe_enabled(ctx: Ctx) -> bool:
    return bool(ctx.setting("describe.enabled", True))


def is_subject(obj: dict[str, Any]) -> bool:
    return str(obj.get("role") or "").strip().lower() in ("subject", "主体")


def _objects(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        o
        for o in result.get("objects") or []
        if isinstance(o, dict) and o.get("object_id")
    ]


def get_description(ctx: Ctx, analysis_id: str | None) -> dict[str, Any] | None:
    """这份拆解的画面描述（含 hint、模型、时间）；没有为 None。"""
    if not analysis_id:
        return None
    row = ctx.db.q1(
        "SELECT * FROM seed_descriptions WHERE analysis_id = ?", (analysis_id,)
    )
    if not row:
        return None
    out = dict(row)
    out["result"] = loads(out.pop("result_json"), {}) or {}
    out["call_ids"] = loads(out.pop("call_ids_json"), []) or []
    return out


def clean_hint(hint: str | None) -> str | None:
    text = " ".join((hint or "").split())
    if len(text) > HINT_CHARS:
        raise StageError(f"补充要求最多 {HINT_CHARS} 个字，现在 {len(text)} 个")
    return text or None


def change_units(desc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """画面描述里的可变单元（没有清单的旧描述为空列表）。"""
    return [
        u
        for u in (desc or {}).get("change_units") or []
        if isinstance(u, dict) and u.get("unit_id") and u.get("object_ids")
    ]


KIND_SHORT = {
    "character": "角色",
    "object": "物件",
    "decoration": "装饰",
    "prop": "承托物",
    "text": "文字",
}
SALIENCE_SHORT = {"large": "大", "medium": "中", "small": "小"}


def unit_lines(units: list[dict[str, Any]]) -> list[str]:
    """「全部恐龙（角色组·大）」「满屏星星（装饰·中）」「游戏手柄（物件·大）」：卡片与命令输出里列可改的主体。

    角色组 / 物件组 = 有成员单元挂在它下面的角色或物件单元（一只角色连着尾巴等对象不算组）。"""
    parents = {str(u["parent"]) for u in units if u.get("parent")}
    out = []
    for u in units:
        kind = KIND_SHORT.get(u.get("kind"), u.get("kind") or "")
        if (
            u.get("kind") in ("character", "object")
            and str(u.get("unit_id")) in parents
        ):
            kind += "组"
        out.append(
            f"{u.get('name') or u.get('unit_id')}（{kind}·{SALIENCE_SHORT.get(u.get('salience'), '?')}）"
        )
    return out


def _unit_errors(
    result: dict[str, Any], known: set[str], subjects: list[str]
) -> list[str]:
    units = [u for u in result.get("change_units") or [] if isinstance(u, dict)]
    errors: list[str] = []
    ids = [str(u.get("unit_id")) for u in units]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        errors.append(f"change_units 里 {dup} 重复了：unit_id 不能重复")
    unknown = sorted(
        {
            str(oid)
            for u in units
            for oid in u.get("object_ids") or []
            if str(oid) not in known
        }
    )
    if unknown:
        errors.append(
            f"change_units 用了拆解里没有的对象编号 {unknown}：object_ids 只能用 analysis.objects 里的编号"
        )
    by_id = {str(u.get("unit_id")): u for u in units}
    for u in units:
        parent = u.get("parent")
        if not parent:
            continue
        group = by_id.get(str(parent))
        if group is None or group is u:
            errors.append(
                f"{u.get('unit_id')} 的 parent {parent} 不存在：成员单元写组单元的 unit_id，不属于组就写 null"
            )
        elif group.get("parent"):
            errors.append(
                f"{u.get('unit_id')} 的组 {parent} 自己也写了 parent：只分两层（组和成员）"
            )
        elif not {str(o) for o in u.get("object_ids") or []} <= {
            str(o) for o in group.get("object_ids") or []
        }:
            errors.append(
                f"{u.get('unit_id')} 的成员不全在组 {parent} 里：组单元的 object_ids 要包含它每个成员的对象"
            )
    covered = {str(oid) for u in units for oid in u.get("object_ids") or []}
    left = [oid for oid in subjects if oid not in covered]
    if left:
        errors.append(
            f"主体 {left} 没有落在任何可变单元里：每个主体至少要在 change_units 的一个单元里"
        )
    return errors


def _validate_p13(objects: list[dict[str, Any]]):
    """主体每个一条、编号照抄拆解；拆解里的每个对象都要落在主体或某条环境里；可变单元的编号与组对得上。"""
    known = {str(o["object_id"]) for o in objects}
    subjects = [str(o["object_id"]) for o in objects if is_subject(o)]

    def check(result: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        listed = [
            str(s.get("object_id"))
            for s in result.get("subjects") or []
            if isinstance(s, dict)
        ]
        env = [
            str(oid)
            for e in result.get("environment") or []
            if isinstance(e, dict)
            for oid in e.get("object_ids") or []
        ]
        missing = [oid for oid in subjects if oid not in listed]
        if missing:
            errors.append(
                f"subjects 缺少主体 {missing}：analysis.objects 里 role 是 subject 的每个一条，object_id 照抄"
            )
        dup = sorted({oid for oid in listed if listed.count(oid) > 1})
        if dup:
            errors.append(f"subjects 里 {dup} 写了不止一条：每个主体只写一条")
        unknown = sorted({oid for oid in listed + env if oid not in known})
        if unknown:
            errors.append(
                f"出现了拆解里没有的对象编号 {unknown}：object_id / object_ids 只能用 analysis.objects 里的编号"
            )
        left = sorted(known - set(listed) - set(env))
        if left:
            errors.append(
                f"这些对象没有写到：{left}（写进 subjects，或归到某条 environment 的 object_ids 里）"
            )
        return errors + _unit_errors(result, known, subjects)

    return check


def _analysis_input(analysis_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """给 P13 的拆解：对象、关系、组合、参考图特征与在用字段的取值（不带证据和可变判断），
    另带能挂在可变单元上的字段（unit_fields），写单元的 not_applicable 用。"""
    return {
        "analysis_id": analysis_id,
        "traits": result.get("traits"),
        "objects": _objects(result),
        "relations": result.get("relations") or [],
        "groups": result.get("groups") or [],
        "fields": [
            {
                "field_id": f.get("field_id"),
                "label": fields.label(f.get("field_id")),
                "value": f.get("value"),
            }
            for f in result.get("fields") or []
            if isinstance(f, dict) and fields.is_active(f.get("field_id"))
        ],
        "unit_fields": [
            {
                "field_id": fid,
                "label": fields.label(fid),
                "applies_to": fields.applies_to(fid),
            }
            for fid in fields.variation_field_ids(fields.SMALL)
            if fields.scope(fid) == fields.UNIT
        ],
    }


def describe_seed(
    ctx: Ctx, seed_id: str, *, force: bool = False, hint: str | None = None
) -> dict[str, Any]:
    """给这张参考图最新的拆解写画面描述（P13）。已有描述时跳过，除非 force 或写了 hint（按要求重写）。"""
    seed = get_seed(ctx, seed_id)
    if seed["status"] == "blocked":
        raise StageError(f"{seed_id} 素材受限，已隔离，不写画面描述")
    analysis = latest_analysis(ctx, seed_id)
    if not analysis:
        raise StageError(f"{seed_id} 还没做 S02 拆解")
    hint = clean_hint(hint)
    analysis_id = analysis["analysis_id"]
    old = get_description(ctx, analysis_id)
    if old and not force and not hint:
        return {
            "seed_id": seed_id,
            "analysis_id": analysis_id,
            "skipped": "已有画面描述（要重写加 --force，或写 --hint）",
        }
    # 重写时没给新要求：沿用这份描述上次的更正（补可变单元清单之类的重写不能丢掉人改过的地方）
    hint = hint or (old or {}).get("hint")
    result = loads(analysis["result_json"], {}) or {}
    objects = _objects(result)
    if ctx.setting("pattern_extract.enabled", True) and pattern_path(ctx, seed_id):
        label = f"{seed_id} 从参考图提取的平面图案：画面都从这张读"
    else:
        label = f"{seed_id} 参考图原图：只描述 seed_record 标出的有效印花区域"
    out = llm.run_stage(
        ctx,
        DESCRIBE_PROMPT,
        scope_id=f"seed:{seed_id}",
        job_id=f"seed:{seed_id}",
        variables={
            "analysis": _analysis_input(analysis_id, result),
            "seed_record": (loads(seed["seed_record_json"], {}) or {}).get("result")
            or {},
            "user_hint": hint or NO_HINT,
        },
        images={"source_image": [vision_image(ctx, seed_bytes(ctx, seed), label)]},
        extra_validate=_validate_p13(objects),
    )
    desc = out.result
    units = unit_lines(change_units(desc))
    now = now_iso()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO seed_descriptions(analysis_id, seed_id, result_json, hint, model, prompt_version,"
            " call_ids_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(analysis_id) DO UPDATE SET result_json = excluded.result_json, hint = excluded.hint,"
            " model = excluded.model, prompt_version = excluded.prompt_version,"
            " call_ids_json = excluded.call_ids_json, updated_at = excluded.updated_at",
            (
                analysis_id,
                seed_id,
                dumps(desc),
                hint,
                out.model,
                out.prompt_version,
                dumps(out.call_ids),
                now,
                now,
            ),
        )
        log_event(
            conn,
            seed_id,
            "description_saved",
            {
                "analysis_id": analysis_id,
                "subjects": len(desc.get("subjects") or []),
                "units": len(units),
                "hint": hint,
                "replaced": bool(old),
            },
        )
    return {
        "seed_id": seed_id,
        "analysis_id": analysis_id,
        "summary": desc.get("summary"),
        "subjects": len(desc.get("subjects") or []),
        "environment": len(desc.get("environment") or []),
        "units": units,
        "harmony_rules": desc.get("harmony_rules") or [],
        "hint": hint,
        "replaced": bool(old),
    }


def describe_quietly(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    """拆解流程里的 S02D：没写成只记事件和 description_error，拆解照常往下走（之后用拆解简版兜底）。

    预算阻断照常抛出（后面的阶段一样会被挡住）。
    """
    try:
        return describe_seed(ctx, seed_id, force=True)
    except (StageError, llm.LLMError) as exc:
        error = f"{type(exc).__name__}：{exc}"[:300]
        with ctx.db.tx() as conn:
            log_event(conn, seed_id, "description_failed", {"error": error})
        return {"description_error": error}


def describe_all(ctx: Ctx, *, force: bool = False) -> dict[str, Any]:
    """所有拆解过的参考图（隔离的除外）补画面描述；force 时已有的也重写。几张同时写（describe.concurrency）。"""
    rows = ctx.db.q(
        "SELECT seed_id, latest_analysis_id FROM seeds WHERE latest_analysis_id IS NOT NULL"
        " AND status != 'blocked' ORDER BY seed_id"
    )
    todo = [
        r["seed_id"]
        for r in rows
        if force or not get_description(ctx, r["latest_analysis_id"])
    ]
    skipped = [r["seed_id"] for r in rows if r["seed_id"] not in todo]
    described: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    if not todo:
        return {"described": described, "skipped": skipped, "failed": failed}
    bg.step(ctx, f"画面描述：0/{len(todo)}")

    def run(seed_id: str) -> None:
        try:
            try:
                out = describe_seed(ctx, seed_id, force=True)
                described.append(
                    {k: out.get(k) for k in ("seed_id", "analysis_id", "subjects")}
                )
            except Exception as exc:  # noqa: BLE001 — 一张没写成不影响别的
                failed.append(
                    {"seed_id": seed_id, "error": f"{type(exc).__name__}：{exc}"[:300]}
                )
            bg.step(ctx, f"画面描述：{len(described) + len(failed)}/{len(todo)}")
        finally:
            # 每个线程用自己的连接，用完关掉
            ctx.db.close()

    workers = max(1, min(int(ctx.setting("describe.concurrency", 3) or 1), len(todo)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run, todo))
    described.sort(key=lambda d: str(d["seed_id"]))
    failed.sort(key=lambda d: d["seed_id"])
    return {"described": described, "skipped": skipped, "failed": failed}


def show(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    """这张参考图现在的画面描述（不调用模型）。"""
    get_seed(ctx, seed_id)
    analysis = latest_analysis(ctx, seed_id)
    desc = get_description(ctx, analysis["analysis_id"] if analysis else None)
    if not desc:
        raise StageError(
            f"{seed_id} 还没有画面描述：podctl seed describe {seed_id} 补写"
        )
    return {
        "seed_id": seed_id,
        "analysis_id": desc["analysis_id"],
        "hint": desc["hint"],
        "model": desc["model"],
        "updated_at": desc["updated_at"],
        # 可改的主体（可变单元）；没有清单的旧描述要 seed describe --force 重写
        "units": unit_lines(change_units(desc["result"])),
        "description": desc["result"],
    }


def _fallback(result: dict[str, Any]) -> dict[str, Any]:
    values = {
        f.get("field_id"): f.get("value")
        for f in result.get("fields") or []
        if isinstance(f, dict)
    }

    def brief(o: dict[str, Any]) -> dict[str, Any]:
        return {
            k: o.get(k)
            for k in ("object_id", "role", "attributes", "visual_function")
            if o.get(k) is not None
        }

    objects = _objects(result)
    return {
        "source": FALLBACK_SOURCE,
        "subjects": [brief(o) for o in objects if is_subject(o)],
        "environment": [brief(o) for o in objects if not is_subject(o)],
        "fields": {
            f"{fid} {fields.label(fid)}": values[fid]
            for fid in FALLBACK_FIELDS
            if values.get(fid) not in (None, "", [], {})
        },
    }


def reference_profile(
    ctx: Ctx, analysis_id: str | None, *, english: bool = False
) -> dict[str, Any]:
    """给 P04 / P05 / P06 / P07 的参考图画面档案：有画面描述用描述，没有用拆解简版兜底。

    english 只给写英文提示词的 P06：带上每个主体 / 环境的英文写法（en）与 prompt_en（画风句、整幅描述）。
    """
    desc = get_description(ctx, analysis_id)
    if desc:
        data = desc["result"]
        out: dict[str, Any] = {"source": DESCRIBED_SOURCE}
        for key in PROFILE_KEYS:
            if data.get(key) in (None, "", []):
                continue
            value = data[key]
            if key in ("subjects", "environment") and not english:
                value = [
                    {k: v for k, v in item.items() if k != "en"}
                    for item in value
                    if isinstance(item, dict)
                ]
            out[key] = value
        if english and data.get("prompt_en"):
            out["prompt_en"] = data["prompt_en"]
        return out
    row = (
        ctx.db.q1(
            "SELECT result_json FROM analyses WHERE analysis_id = ?", (analysis_id,)
        )
        if analysis_id
        else None
    )
    if not row:
        return {"source": "没有拆解记录：只能按系列规则和任务描述写"}
    return _fallback(loads(row["result_json"], {}) or {})
