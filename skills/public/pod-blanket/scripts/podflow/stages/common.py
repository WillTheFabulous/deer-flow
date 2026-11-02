"""阶段共用：参考资料、记录读取与视图。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .. import fields
from ..config import ConfigError
from ..context import Ctx
from ..paths import REFERENCES_DIR
from ..prompts import ImageInput
from ..store import ensure_local
from ..util import loads
from .. import images

# 出图方式：t2i 文生图（模型只看提示词）；i2i 图生图（同一个模型，加上参考图原图作为输入图）
GEN_MODES = ("t2i", "i2i")
MODE_LABELS = {"t2i": "文生图", "i2i": "图生图"}
I2I_SLOT_SUFFIX = "@i2i"
# image_generation.i2i_edit：图生图另用 <模型>-edit 再出一张；mooya 代理里两者走同一条编辑路由
EDIT_SUFFIX = "-edit"
EDIT_LABEL = "图生图 Edit"


def model_label(model: str | None) -> str:
    """给人看的模型名：Gemini 出图模型叫 nano-banana-xxx，gpt-image-2.5-xxx 省掉 image-，Edit 模型保留 -edit。"""
    return (
        (model or "")
        .replace("gemini-3-pro-image-preview", "nano-banana-pro")
        .replace("gemini-nano-banana-", "nano-banana-")
        .replace("gpt-image-", "gpt-")
    )


EVIDENCE_TYPES = {
    "observed": "观察：图中直接可见",
    "user_provided": "用户提供",
    "inferred": "推断：由可见特征推出，需标明",
    "judgment": "设计判断",
    "suggestion": "建议：新方向，不是原图事实",
    "measured": "测量：有工具测量依据",
    "unknown": "未知：看不清或无法确认",
    "not_applicable": "不适用",
}


class StageError(RuntimeError):
    """阶段前置条件不满足（缺记录、状态不对、被阻断）。"""


@lru_cache(maxsize=None)
def reference(name: str) -> dict[str, Any]:
    return json.loads((REFERENCES_DIR / name).read_text(encoding="utf-8"))


def get_seed(ctx: Ctx, seed_id: str) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM seeds WHERE seed_id = ?", (seed_id,))
    if not row:
        raise StageError(f"没有 seed {seed_id}")
    return dict(row)


def pattern_path(ctx: Ctx, seed_id: str) -> str | None:
    """这张参考图提取出的平面图案（S01P）；还没提取过为 None。"""
    row = ctx.db.q1("SELECT path FROM seed_patterns WHERE seed_id = ?", (seed_id,))
    return row["path"] if row else None


def seed_name(ctx: Ctx, seed_id: str) -> str | None:
    """任务名字（S01N：参考图描述 + 登记那天的月日，如「森林三伙伴水彩 1020」）；还没起名为 None。"""
    row = ctx.db.q1("SELECT name FROM seed_names WHERE seed_id = ?", (seed_id,))
    return row["name"] if row else None


def task_label(ctx: Ctx, seed_id: str) -> str:
    """给人看的任务称呼：编号 + 名字（S01 森林三伙伴水彩 1020）；还没起名只有编号。"""
    name = seed_name(ctx, seed_id)
    return f"{seed_id} {name}" if name else seed_id


_SUBJECT_SEED_SQL = {
    "F": "SELECT seed_id FROM families WHERE family_id = ?"
    " ORDER BY family_version DESC LIMIT 1",
    "B": "SELECT f.seed_id FROM batches b JOIN families f"
    " ON f.family_id = b.family_id AND f.family_version = b.family_version"
    " WHERE b.batch_id = ?",
}


def subject_label(ctx: Ctx, subject: str | None) -> str:
    """卡片标题里的主体：参考图 S01 → 「S01 名字」；系列 F01 / 批次 B008 → 「S01 名字 · F01」。

    还没起名、查不到所属参考图或是别的编号（素材、空）时原样返回。
    """
    subject = subject or ""
    if re.fullmatch(r"S\d+", subject):
        return task_label(ctx, subject)
    sql = (
        _SUBJECT_SEED_SQL.get(subject[:1])
        if re.fullmatch(r"[FB]\d+", subject)
        else None
    )
    row = ctx.db.q1(sql, (subject,)) if sql else None
    if not row or not seed_name(ctx, row["seed_id"]):
        return subject
    return f"{task_label(ctx, row['seed_id'])} · {subject}"


def reference_path(ctx: Ctx, seed: dict[str, Any]) -> str:
    """后续步骤用的参考图：开着图案提取、提取过就用平面图案，否则用原图。"""
    if ctx.setting("pattern_extract.enabled", True):
        path = pattern_path(ctx, seed["seed_id"])
        if path:
            return path
    return seed["original_path"]


def original_bytes(ctx: Ctx, seed: dict[str, Any]) -> bytes:
    """参考图原图（S01 接入与图案提取用）。"""
    return ensure_local(ctx, seed["original_path"]).read_bytes()


def seed_bytes(ctx: Ctx, seed: dict[str, Any]) -> bytes:
    """拆解、图生图、质检用的参考图（提取过平面图案就是图案）。"""
    return ensure_local(ctx, reference_path(ctx, seed)).read_bytes()


def base_asset_id(variant: dict[str, Any] | None) -> str | None:
    """二次大变化的底图（父方向出的那张图）的素材编号；以原图为底的方向为 None。"""
    base = (variant or {}).get("base")
    return (
        str(base["asset_id"])
        if isinstance(base, dict) and base.get("asset_id")
        else None
    )


def asset_bytes(ctx: Ctx, asset_id: str) -> bytes:
    row = ctx.db.q1("SELECT path FROM assets WHERE asset_id = ?", (asset_id,))
    if not row:
        raise StageError(f"底图 {asset_id} 找不到")
    return ensure_local(ctx, row["path"]).read_bytes()


def input_image_bytes(
    ctx: Ctx, seed: dict[str, Any], variant: dict[str, Any] | None = None
) -> bytes:
    """图生图的输入图：二次大变化是底图（父方向出的那张图），其余是参考图（提取过平面图案就是图案）。"""
    base = base_asset_id(variant)
    return asset_bytes(ctx, base) if base else seed_bytes(ctx, seed)


def vision_image(ctx: Ctx, data: bytes, label: str) -> ImageInput:
    return ImageInput(
        label=label,
        data=images.to_vision_jpeg(data, int(ctx.setting("llm.vision_max_side", 2048))),
    )


def latest_analysis(ctx: Ctx, seed_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1(
        "SELECT * FROM analyses WHERE seed_id = ? ORDER BY analysis_version DESC LIMIT 1",
        (seed_id,),
    )
    return dict(row) if row else None


def get_family(ctx: Ctx, family_id: str, version: int | None = None) -> dict[str, Any]:
    if version is None:
        row = ctx.db.q1(
            "SELECT * FROM families WHERE family_id = ? ORDER BY family_version DESC LIMIT 1",
            (family_id,),
        )
    else:
        row = ctx.db.q1(
            "SELECT * FROM families WHERE family_id = ? AND family_version = ?",
            (family_id, version),
        )
    if not row:
        raise StageError(f"没有系列 {family_id}" + (f" v{version}" if version else ""))
    fam = dict(row)
    fam["spec"] = loads(fam["spec_json"], {})
    return fam


def approved_family(ctx: Ctx, family_id: str) -> dict[str, Any]:
    row = ctx.db.q1(
        "SELECT * FROM families WHERE family_id = ? AND status = 'approved' ORDER BY family_version DESC LIMIT 1",
        (family_id,),
    )
    if not row:
        raise StageError(
            f"系列 {family_id} 没有已批准的版本：第一份系列规则必须人工确认（podctl family approve）"
        )
    fam = dict(row)
    fam["spec"] = loads(fam["spec_json"], {})
    return fam


def get_job(ctx: Ctx, job_id: str) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if not row:
        raise StageError(f"没有任务 {job_id}")
    return dict(row)


# 改了就换画风的维度：配色结构、绘制技法里挂在整幅上的字段（整体配色、绘制技法：主体和背景整幅一起换）；
# 单元配色只换一个单元，和其余方向一样新画进来的内容沿用参考图的画法
RESTYLE_DIMENSIONS = ("10 配色结构", "11 绘制技法")
STYLE_KEEP = (
    "沿用参考图画风：新画进来或改掉的场景、背景、道具和配角用参考图同一套画法"
    "（reference_profile 的 style 与 harmony_rules），不加参考图没有的写实渲染，主体不重新打光"
)
STYLE_RESTYLE = "这个方向改的是整体配色或绘制技法：主体和背景整幅一起换成新的配色 / 画法，换完仍是统一的一套画法"


def _restyles(field_id: Any) -> bool:
    fid = fields.canonical(field_id) or field_id
    return (
        fields.dimension(fid) in RESTYLE_DIMENSIONS
        and fields.scope(fid) == fields.CANVAS
    )


def style_scope(changed_fields: Any) -> str:
    """这个方向的画风怎么处理：改了整体配色 / 绘制技法的整幅一起换，其余（含单元配色）沿用参考图画风。"""
    restyle = any(
        _restyles(cf.get("field_id"))
        for cf in changed_fields or []
        if isinstance(cf, dict)
    )
    return STYLE_RESTYLE if restyle else STYLE_KEEP


def variant_view(ctx: Ctx, variant_id: str) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM variants WHERE variant_id = ?", (variant_id,))
    if not row:
        raise StageError(f"没有变体 {variant_id}")
    plan = loads(row["plan_json"], {})
    view = {
        "variant_id": row["variant_id"],
        "family_id": row["family_id"],
        "family_version": row["family_version"],
        "parent_variant_id": row["parent_variant_id"],
        "generation_depth": row["generation_depth"],
        "axis": row["axis"],
        # 小变化 small / 大变化 big（三处组合 kind = combo，2026-10-21 前的两轴组合也是）；梳理前的方向是 combo / control 或没有
        "task_type": plan.get("task_type"),
        "kind": plan.get("kind"),
        "title": plan.get("title"),
        "keep": plan.get("keep"),
        **{
            k: plan.get(k)
            for k in (
                "changed_fields",
                "preserve",
                "coupled_changes",
                "acceptance_criteria",
                "hypothesis_ref",
                "difference_rationale",
                "style_fit",
            )
        },
        "style_scope": style_scope(plan.get("changed_fields")),
    }
    if plan.get("task_type") == fields.SMALL:
        view["composition_lock"] = fields.COMPOSITION_LOCK
    if isinstance(plan.get("base"), dict):
        # 二次大变化：底图是哪个小变化出的哪张图、主体已经换成了什么（P06 照它写新主体）
        view["base"] = {
            k: plan["base"].get(k)
            for k in ("variant_id", "asset_id", "title", "changes")
        }
    return view


QC_OFF_REASON = "质检已暂停（qc.enabled: false），没有自动核对"


def qc_enabled(ctx: Ctx) -> bool:
    """质检开关：关掉时新图不调 P07、不自动修复，标「未经质检」直接进选款；质检代码保留，改回 true 就恢复。"""
    return bool(ctx.setting("qc.enabled", True))


INTAKE_OFF_NOTE = "接入确认已冻结（intake.review: false），不停下等人确认"


def intake_review_enabled(ctx: Ctx) -> bool:
    """参考图接入确认开关：关掉时 S01 看不清、模型拿不准都不开「参考图接入」待审，直接往下拆解；
    授权受限照旧停下。代码保留，改回 true 就恢复。"""
    return bool(ctx.setting("intake.review", True))


def generation_modes(ctx: Ctx) -> list[str]:
    """启用的出图方式，按 GEN_MODES 排序；没配置时只做文生图。"""
    raw = ctx.setting("image_generation.modes") or ["t2i"]
    unknown = [m for m in raw if m not in GEN_MODES]
    if unknown:
        raise ConfigError(
            f"image_generation.modes 只能取 {list(GEN_MODES)}，不认识 {unknown}"
        )
    return [m for m in GEN_MODES if m in raw]


def small_change_modes(ctx: Ctx) -> list[str]:
    """小变化的出图方式（改两处、构图不动）：缺省只做图生图，文生图从零画保不住原构图。"""
    raw = ctx.setting("image_generation.small_change_modes") or ["i2i"]
    unknown = [m for m in raw if m not in GEN_MODES]
    if unknown:
        raise ConfigError(
            f"image_generation.small_change_modes 只能取 {list(GEN_MODES)}，不认识 {unknown}"
        )
    return [m for m in GEN_MODES if m in raw]


def variant_modes(ctx: Ctx, plan: dict[str, Any] | None) -> list[str]:
    """这个方向的出图方式：小变化按 small_change_modes，大变化与梳理前的方向按 image_generation.modes。"""
    if (plan or {}).get("task_type") == "small":
        return small_change_modes(ctx)
    return generation_modes(ctx)


def slot_mode(slot_id: str) -> str:
    return "i2i" if slot_id.endswith(I2I_SLOT_SUFFIX) else "t2i"


def fanout_models(ctx: Ctx) -> list[str]:
    """启用的生图模型（去重、保持配置顺序，不含 -edit）。"""
    return list(dict.fromkeys(model for _, model, _ in fanout_slots(ctx, ["t2i"])))


def fanout_slots(
    ctx: Ctx, modes: list[str] | None = None
) -> list[tuple[str, str, str]]:
    """(slot_id, model, mode)：每个启用的模型按 n 展开，每种出图方式各一份。

    文生图槽位编号是 model#k，与加入图生图之前建的任务一致；图生图是 model#k@i2i；
    开着 image_generation.i2i_edit 时图生图另有一份 model-edit#k@i2i，排在原模型的图生图后面
    （并发不够时先出原模型的）。
    """
    edit = bool(ctx.setting("image_generation.i2i_edit", False))
    slots: list[tuple[str, str, str]] = []
    for mode in generation_modes(ctx) if modes is None else modes:
        edits: list[tuple[str, str, str]] = []
        for item in ctx.setting("image_generation.fanout", []) or []:
            if not item.get("enabled", True):
                continue
            model = item["model"]
            if model.endswith(EDIT_SUFFIX):
                raise ConfigError(
                    f"image_generation.fanout 不要直接写 {model}：-edit 模型只做图生图，"
                    "由 image_generation.i2i_edit 按原模型自动加"
                )
            for k in range(1, int(item.get("n", 1)) + 1):
                if mode != "i2i":
                    slots.append((f"{model}#{k}", model, mode))
                    continue
                slots.append((f"{model}#{k}{I2I_SLOT_SUFFIX}", model, mode))
                if edit:
                    edit_model = model + EDIT_SUFFIX
                    edits.append(
                        (f"{edit_model}#{k}{I2I_SLOT_SUFFIX}", edit_model, mode)
                    )
        slots += edits
    return slots
