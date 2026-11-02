"""S05 批次裂变规划：程序从这张图的可变单元与可变字段里随机挑改动组成槽位，P05 给每个槽位写一个候选方向，
由人挑选后才建任务出图（开着方向自动确认时程序全部确认）。

- 小变化（plan.small_count 个）：改两处，一处 = 一个可变单元（画面档案里会一起改的一组对象）上的一个小变化字段，
  整体配色、排列密度这类整幅字段挂在「整幅」上（subjects.py）；整体构图不动，不改绘制技法；
- 大变化（plan.big_count 个）：要么改主题叙事 / 整体构图 / 绘制技法（换画法大类）/ 动态字段（动作姿态、朝向视线、
  距离互动、承托依附、陪衬角色，可以连带调整布局）中的一个字段（单字段大改，plan.big_single_count 个），要么改三处
  （三处组合，补足剩下的）：三处组合另外能用绘制技法（同一画法大类内调整）和构图不动时改不了的小变化字段
  （layout_change，可以连带调整布局）。
- 大变化的配额（2026-10-27）：单字段大改、三处组合、二次大变化里动态字段与换画法各占几成（plan.big_single_quota /
  plan.combo_quota / plan.derived.quota）；换画法的槽位由程序从画法大类里各分一种不重复的（technique_to）。
- 这张图还没有可变单元清单、或 plan.unit 设成 fields 时按字段策划：一处就是一个小变化字段，改到画面里的同类元素。
- 小变化按 plan.small_swap_ratio 分两组（2026-10-27）：换主体组每组至少一处是换主体（D09），其余组不换主体；
  一组凑不够由另一组补。要换主体时先调一次 P14 给每个要换的单元一份互不相同的新主体清单，按槽位分下去（swap_to）。
- 单字段大改里同一个大字段一轮最多 plan.big_single_repeat 次（2026-10-27，缺省 1）。
- 二次大变化（plan_derived，2026-10-27）：一批小变化出完后，以换了主体的小变化出的图为底图，各改一个大字段；
  只做一层，触发与幂等见 derive.py。

程序负责：建字段池（系列变化域里可变、没锁定的字段）与「单元 × 字段」组合池、按批次号做种子随机挑（少被改过的
优先、尽量不同维度、同一批不重复、做过的组合靠后）、按类型分块并发调 P05（单次调用有时限）、按槽位核对每个方向、
规范化计划指纹去重（换措辞不算新计划）、按「单元 + 字段 + 改成的样子」认出换了说法的同一个方向、分配 ID、
开「裂变方向」待审。某组调用失败只丢这一组，槽位没出方向记进覆盖缺口，不凑数；一个方向都没有时把系列标为 exhausted。
任务和出图槽位在确认方向时才建（directions.confirm）。
"""

from __future__ import annotations

import itertools
import math
import random
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .. import bg, fields, llm, subjects
from ..config import ConfigError
from ..context import Ctx
from ..db import log_event, next_id, open_review
from ..util import (
    canonical_json,
    clip,
    dumps,
    loads,
    normalize_phrase,
    now_iso,
    plain,
    sha256_text,
)
from .common import EDIT_SUFFIX, StageError, fanout_models, get_family
from .profile import describe_enabled, reference_profile
from .profile import unit_lines as profile_unit_lines

SMALL, BIG, COMBO = fields.SMALL, fields.BIG, fields.COMBO
# 二次大变化：以一个换了主体的小变化出的图为底图改一个大字段，只出现在二次批次里（plan_derived）
DERIVED = "derived"
# 一轮以原图为底的策划里的槽位类型（--kinds 只能取这几个）
PLAN_KINDS = (SMALL, BIG, COMBO)
# 单字段大改、三处组合和二次大变化都算大变化；这里是策划槽位的类型名（键名不变，--kinds / cancel --kind 用它）
KIND_LABELS = {
    SMALL: "小变化",
    BIG: "单字段大改",
    COMBO: "三处组合",
    DERIVED: "二次大变化",
}
# 每类方向一次改几处、槽位编号前缀：小变化两处，单字段大改一个大字段，三处组合三处，二次大变化一个大字段
FIELDS_PER_KIND = {SMALL: 2, BIG: 1, COMBO: 3, DERIVED: 1}
SLOT_PREFIX = {SMALL: "S", BIG: "B", COMBO: "T", DERIVED: "D"}
SWAP_FIELD = fields.SWAP_FIELD
BIG_RULE = (
    "只改槽位里这一个字段（主题叙事、整体构图、绘制技法，或动作姿态、朝向视线、距离互动、承托依附、陪衬角色这类动态字段），"
    "可以大改；动态字段按它的 big_change 改，可以连带调整位置、大小和布局；被它带动的主体、配角、装饰写进 coupled_changes；"
    "绘制技法要换成另一种画法大类（带 technique_to 的照它换）；同一组里改同一个字段的几个方向要改成明显不同的样子"
)
DERIVED_RULE = (
    "每个槽位的底图是 base：同一张参考图的一个小变化出的图，主体已经按 base.changes 换过。在底图上只改槽位里这一个"
    "字段（主题叙事、整体构图、绘制技法或动态字段），可以大改；新主体保持底图里的样子（物种或物件、标志性特征、配色），"
    "被改动带动的配角、装饰、背景写进 coupled_changes；绘制技法要换成另一种画法大类（带 technique_to 的照它换）"
)
COMBO_RULE = (
    "同时改槽位里这三处（每处是一个单元上的一个小变化字段，unit_id 为 canvas 的那处改整幅；没有 unit 的槽位每处就是"
    "一个小变化字段、改到画面里的同类元素），每处都改到单元里的全部成员、一眼看得出，三处改动相互配合、主题统一；"
    "可以为配合改动调整布局和其余部分"
)
SMALL_KEEP = "其余照原图"
# 随机挑字段组合的权重：同一组里有同维度的字段、和做过的组合一样的往后排；挑中一组后其中的字段权重减半
SAME_DIMENSION_WEIGHT = 0.15
DONE_COMBO_WEIGHT = 0.2
PICKED_FIELD_DECAY = 0.5

# 素材名字里方向短语最多多少字；短语里要去掉的字符（文件名不能有的）
LABEL_PHRASE_CHARS = 24
_LABEL_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 挑选卡上的截断长度（字）：提示词里要求的更短，这里只防超长
TITLE_CHARS = 16
BRIEF_CHARS = 24
KEEP_CHARS = 40
# 人对这一轮的要求（方向记录卡的输入框、plan --hint）最多多少字
HINT_CHARS = 100
# 给 P05 的做过的方向：每条目标值截到多少字、每个字段最多列几条（新的在后）
HISTORY_TO_CHARS = 40
HISTORY_PER_FIELD = 12
# 给 P05 的槽位现状（拆解里这个字段的完整取值）最多多少字：要放得下每个主体各自的描述
CURRENT_CHARS = 400
NO_REQUEST = "没有额外要求"


def field_based(spec: dict[str, Any] | None) -> bool:
    """系列规则是不是按梳理后的字段字典拆的（有可变字段判断，能按字段裂变）。"""
    return bool((spec or {}).get("field_schema"))


def legacy_reason(seed_id: str | None) -> str:
    seed = seed_id or "这张参考图"
    return (
        f"这个系列是按旧字段字典拆的，没有可变字段判断：先按新字段重新拆解"
        f"（POD 对话里说「{seed} 按新字段重新拆解」）"
    )


def field_aspects(field_ids: list[Any]) -> list[str]:
    """改动落在哪些维度（方位），按字典的维度顺序、去掉编号；停用字段按并入的字段算。"""
    present = set()
    for fid in field_ids:
        dim = fields.dimension(fields.canonical(fid) or fid)
        if dim:
            present.add(dim)
    return [fields.dimension_name(d) for d in fields.dimensions() if d in present]


def _on_unit(cf: dict[str, Any]) -> bool:
    """这处改动是不是落在一个可变单元上（整幅和按字段策划的不算）。"""
    return bool(cf.get("unit_id")) and cf.get("unit_id") != subjects.CANVAS


def change_aspect(cf: dict[str, Any]) -> str:
    """一处改动给人看的名字：「高地牛·表情」；整幅与按字段策划的只写字段短名。"""
    label = fields.label(cf.get("field_id"))
    return f"{cf.get('unit_name') or cf['unit_id']}·{label}" if _on_unit(cf) else label


def axis_text(field_ids: list[Any], unit_names: list[str | None] | None = None) -> str:
    """变化轴：「配色结构·整体配色」，改了几处就用 + 连接几段；落在单元上的那处写「高地牛·表情」。"""
    names = unit_names or [None] * len(field_ids)
    return "+".join(
        f"{name}·{fields.label(f)}"
        if name
        else f"{fields.dimension_name(fields.dimension(f))}·{fields.label(f)}"
        for f, name in zip(field_ids, names)
    )


def base_variant(task: dict[str, Any]) -> str | None:
    """二次大变化的底图来自哪个方向（父方向编号）；以原图为底的方向为 None。"""
    base = task.get("base")
    return (
        str(base["variant_id"])
        if isinstance(base, dict) and base.get("variant_id")
        else None
    )


def plan_hash(family_id: str, task: dict[str, Any]) -> str:
    """计划指纹：字段 + 改成的样子（规范化后）；落在单元上的另带单元键，二次大变化另带底图的方向编号。
    没有单元、没有底图时与 2026-10-23 前的算法一致。"""
    changed = sorted(
        (
            (
                str(cf["unit_key"]),
                str(cf.get("field_id")),
                normalize_phrase(cf.get("to")),
            )
            if cf.get("unit_key")
            else (str(cf.get("field_id")), normalize_phrase(cf.get("to")))
        )
        for cf in task.get("changed_fields", [])
    )
    payload: dict[str, Any] = {"family": family_id, "changed": changed}
    base = base_variant(task)
    if base:
        payload["base"] = base
    return sha256_text(canonical_json(payload))[:24]


def _change_key(item: dict[str, Any]) -> tuple[str, Any]:
    return (str(item.get("unit_id") or ""), item.get("field_id"))


def reconcile_brief(task: dict[str, Any]) -> dict[str, Any]:
    """给人看的简述与改动对齐：每处改动一条（按单元 + 字段），缺的用改动的目标值补上，没有改动支撑的丢掉。

    简述没写 unit_id、而这个字段只改了一处时按字段对上。按字段策划的方向与 2026-10-23 前完全一样。
    """
    changed = [cf for cf in task.get("changed_fields") or [] if isinstance(cf, dict)]
    keys = [_change_key(cf) for cf in changed]
    fids = [cf.get("field_id") for cf in changed]
    given: dict[tuple[str, Any], dict[str, Any]] = {}
    dropped: list[str] = []
    for item in task.get("brief") or []:
        if not isinstance(item, dict):
            continue
        key = _change_key(item)
        if key not in keys and not item.get("unit_id") and fids.count(key[1]) == 1:
            key = keys[fids.index(key[1])]
        if key in keys and key not in given and clip(item.get("to"), BRIEF_CHARS):
            given[key] = item
        else:
            dropped.append(str(item.get("field_id") or item.get("aspect")))
    lines, filled = [], []
    for cf, key in zip(changed, keys):
        src = given.get(key)
        if src is None:
            src = cf
            filled.append(str(cf.get("field_id")))
        lines.append(
            {
                "field_id": cf.get("field_id"),
                **({"unit_id": cf["unit_id"]} if cf.get("unit_id") else {}),
                "aspect": change_aspect(cf),
                "from": clip(src.get("from"), BRIEF_CHARS),
                "to": clip(src.get("to"), BRIEF_CHARS),
            }
        )
    title = clip(task.get("title"), TITLE_CHARS) or clip(
        "、".join(change_aspect(cf) for cf in changed) + "变化", TITLE_CHARS
    )
    return {
        "title": title,
        "brief": lines,
        "keep": clip(task.get("keep"), KEEP_CHARS),
        "aspects": field_aspects(fids),
        "brief_filled": filled,
        "brief_dropped": dropped,
    }


def planning_family(ctx: Ctx, family_id: str) -> dict[str, Any]:
    """裂变用的系列版本：最新的已批准或待认可版本（待认可的在确认方向时一并认可）。"""
    row = ctx.db.q1(
        "SELECT * FROM families WHERE family_id = ? AND status IN ('approved', 'pending')"
        " ORDER BY family_version DESC LIMIT 1",
        (family_id,),
    )
    if not row:
        latest = ctx.db.q1(
            "SELECT status FROM families WHERE family_id = ? ORDER BY family_version DESC LIMIT 1",
            (family_id,),
        )
        if not latest:
            raise StageError(f"没有系列 {family_id}")
        if latest["status"] == "exhausted":
            raise StageError(
                f"系列 {family_id} 已耗尽：给新参考图，或在 POD 对话里修订系列、扩大变化域"
            )
        raise StageError(
            f"系列 {family_id} 没有可以裂变的版本（当前 {latest['status']}）"
        )
    fam = dict(row)
    fam["spec"] = loads(fam["spec_json"], {})
    return fam


def proposed_batches(ctx: Ctx, family_id: str | None = None) -> list[dict[str, Any]]:
    """待挑选的批次（策划好、还没人确认）。"""
    sql = "SELECT * FROM batches WHERE status = 'proposed'"
    params: tuple = ()
    if family_id:
        sql += " AND family_id = ?"
        params = (family_id,)
    return [dict(r) for r in ctx.db.q(sql + " ORDER BY batch_id", params)]


def _latest_specs(
    ctx: Ctx, statuses: list[str]
) -> list[tuple[str, str, dict[str, Any]]]:
    """各系列最新版本（状态在 statuses 里的）：(family_id, seed_id, spec)。"""
    marks = ",".join("?" for _ in statuses)
    rows = ctx.db.q(
        f"SELECT f.family_id, f.seed_id, f.spec_json FROM families f WHERE f.status IN ({marks})"
        " AND f.family_version = (SELECT MAX(f2.family_version) FROM families f2 WHERE f2.family_id = f.family_id)"
        " ORDER BY f.family_id",
        tuple(statuses),
    )
    return [
        (r["family_id"], r["seed_id"], loads(r["spec_json"], {}) or {}) for r in rows
    ]


def _archived_families(ctx: Ctx) -> set[str]:
    from .. import shelf  # 归档模块依赖任务视图（任务视图又依赖本模块），放到用时再导入

    return shelf.archived_families(ctx)


def plannable_families(ctx: Ctx) -> list[str]:
    """可以再策划一轮的系列：最新版本已批准（开着拆解后自动策划时，待认可也算）、按新字段拆过，
    没有未完成的任务，也没有待挑选的批次；已归档的任务不算（要接着裂变先调出，或在历史任务里点「继续裂变」）。"""
    archived = _archived_families(ctx)
    statuses = ["approved"]
    if ctx.setting("batch.auto_plan_after_analyze", True):
        statuses.append("pending")
    marks = ",".join("?" for _ in statuses)
    rows = ctx.db.q(
        f"SELECT f.family_id, f.spec_json FROM families f WHERE f.status IN ({marks}) AND f.family_version = ("
        " SELECT MAX(f2.family_version) FROM families f2 WHERE f2.family_id = f.family_id)"
        " AND NOT EXISTS (SELECT 1 FROM batches b JOIN jobs j ON j.batch_id = b.batch_id"
        "   WHERE b.family_id = f.family_id AND j.status NOT IN ('complete', 'failed'))"
        " AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.family_id = f.family_id AND b.status = 'proposed')"
        " ORDER BY f.family_id",
        tuple(statuses),
    )
    return [
        r["family_id"]
        for r in rows
        if r["family_id"] not in archived
        and field_based(loads(r["spec_json"], {}) or {})
    ]


def legacy_families(ctx: Ctx) -> list[dict[str, str]]:
    """按旧字段字典拆的系列（最新版本可用、没有可变字段判断）：要先重新拆解才能按字段裂变。已归档的不提。"""
    archived = _archived_families(ctx)
    return [
        {"family_id": fid, "seed_id": seed_id}
        for fid, seed_id, spec in _latest_specs(
            ctx, ["approved", "pending", "exhausted"]
        )
        if not field_based(spec) and fid not in archived
    ]


def _analysis_result(ctx: Ctx, analysis_id: str | None) -> dict[str, Any]:
    if not analysis_id:
        return {}
    row = ctx.db.q1(
        "SELECT result_json FROM analyses WHERE analysis_id = ?", (analysis_id,)
    )
    return (loads(row["result_json"], {}) or {}) if row else {}


def _judged_fields(result: dict[str, Any]) -> set[str]:
    """拆解里判断过的在用字段（停用编号按并入的字段算）。"""
    return {
        fields.canonical(f.get("field_id"))
        for f in result.get("fields") or []
        if isinstance(f, dict)
    } - {None}


def _analysis_values(result: dict[str, Any]) -> dict[str, str]:
    """拆解里每个可以裂变的字段的当前取值，给 P05 当「原来的样子」。

    取拆解的完整取值（截到 CURRENT_CHARS）：可变清单里的值只留了几十个字，几个主体的描述会被截掉；
    旧拆解里停用并入的字段（绘制技法 D42–D44）拼到并入的字段（D41）后面；完整取值没有时退回可变清单里的值。
    """
    raw = {
        f.get("field_id"): f.get("value")
        for f in result.get("fields") or []
        if isinstance(f, dict) and f.get("field_id")
    }
    out: dict[str, str] = {}
    for fid in fields.variation_field_ids():
        parts = [
            plain(raw.get(src))
            for src in [fid, *fields.merged_sources(fid)]
            if plain(raw.get(src))
        ]
        if parts:
            out[fid] = clip("；".join(parts), CURRENT_CHARS * len(parts))
    for v in (result.get("variation") or {}).get("variable", []):
        fid = fields.canonical(v.get("field_id")) or v.get("field_id")
        if fid and not out.get(fid):
            out[fid] = v.get("value") or ""
    return out


def combo_layout_fields(ctx: Ctx) -> bool:
    """plan.combo_layout_fields（缺省 true）：大变化也用拆解判成构图不动时改不了的小变化字段（三处组合全收，
    单字段大改与二次大变化只收动态字段），大变化里可以连带调整布局。"""
    return bool(ctx.setting("plan.combo_layout_fields", True))


def field_pools(ctx: Ctx, fam: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """这张图可以拿来裂变的字段：小变化池、大字段池（主题叙事 / 整体构图 / 绘制技法 / 动态字段）与三处组合池。

    系列变化域里可变、没锁定的字段才进池；旧系列变化域里停用的字段（D42–D44）并到它并入的字段（D41），选项合并。
    0.5 新加的字段（单元配色、表面纹样）在之前拆的图上没判断过，按可变处理（选项留空）。字段按 fields.slot_kinds
    进池（绘制技法不进小变化，动态字段三类都进）；拆解判成不可变的小变化字段（以前按「构图不动时改不了」判的）
    另进三处组合池，其中的动态字段也进大字段池，带 layout_change（大变化里可以连带调整布局），小变化池不收：
    被门控关掉的维度、系列锁定的不收，plan.combo_layout_fields 关掉时不收。
    """
    spec = fam["spec"]
    if not field_based(spec):
        raise StageError(legacy_reason(fam.get("seed_id")))
    result = _analysis_result(ctx, spec.get("analysis_id") or fam.get("analysis_id"))
    current = _analysis_values(result)
    judged = _judged_fields(result)
    variation = spec.get("variation") or {}
    gated = set(variation.get("gated_off") or {})
    locked = set()
    for item in spec.get("locked_fields") or []:
        fid = item.get("field_id") if isinstance(item, dict) else item
        locked.add(fields.canonical(fid) or fid)
    domains: dict[str, dict[str, list[Any]]] = {}
    for d in spec.get("variable_domains") or []:
        if not isinstance(d, dict):
            continue
        fid = fields.canonical(d.get("field_id"))
        if not fid or fid in locked or not fields.kinds(fid):
            continue
        merged = domains.setdefault(fid, {"options": [], "compatibility": []})
        for key in ("options", "compatibility"):
            for item in d.get(key) or []:
                if item not in merged[key]:
                    merged[key].append(item)

    def open_field(fid: str | None) -> bool:
        return bool(
            fid
            and fid not in domains
            and fid not in locked
            and fields.dimension(fid) not in gated
        )

    for fid in fields.variation_field_ids():
        if fields.since(fid) and fid not in judged and open_field(fid):
            domains[fid] = {"options": [], "compatibility": []}
    relaxed: set[str] = set()
    if combo_layout_fields(ctx):
        for item in variation.get("not_variable") or []:
            fid = (
                fields.canonical(item.get("field_id"))
                if isinstance(item, dict)
                else None
            )
            if open_field(fid) and SMALL in fields.slot_kinds(fid):
                domains[fid] = {"options": [], "compatibility": []}
                relaxed.add(fid)
    order = {fid: i for i, fid in enumerate(fields.variation_field_ids())}
    infos = [
        {
            "field_id": fid,
            "field": fields.field_name(fid),
            "label": fields.label(fid),
            "dimension": fields.dimension(fid),
            "current": current.get(fid, ""),
            **domains[fid],
            **({"layout_change": True} if fid in relaxed else {}),
        }
        for fid in sorted(domains, key=lambda f: order.get(f, 999))
    ]
    return {
        kind: [
            i
            for i in infos
            if kind in fields.slot_kinds(i["field_id"])
            and (kind != SMALL or not i.get("layout_change"))
        ]
        for kind in (SMALL, BIG, COMBO)
    }


def _count_setting(ctx: Ctx, key: str, default: int) -> int:
    try:
        return max(0, int(ctx.setting(key, default)))
    except (TypeError, ValueError):
        return default


NO_SWAP_REASON = "这张图没有能换的主体（没有角色 / 物件单元，或换主体被系列锁定、被单元否决、主体太小）：小变化照旧随机挑"


def swap_ratio(ctx: Ctx) -> float:
    """plan.small_swap_ratio：小变化里换主体组占几成（0–1，缺省 0 = 不分组，照旧随机挑）。"""
    raw = ctx.setting("plan.small_swap_ratio", 0)
    try:
        value = float(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"plan.small_swap_ratio 要是 0 到 1 之间的数，现在是 {raw!r}"
        ) from exc
    if not 0 <= value <= 1:
        raise ConfigError(f"plan.small_swap_ratio 要是 0 到 1 之间的数，现在是 {raw!r}")
    return value


def big_repeat(ctx: Ctx) -> int:
    """plan.big_single_repeat：单字段大改里同一个大字段一轮最多几次（缺省 1）。"""
    return max(1, _count_setting(ctx, "plan.big_single_repeat", 1))


def _is_swap(item: dict[str, Any]) -> bool:
    """这一处是不是换主体（D09）：组合池的 pair 与字段池的 info 都带 field_id。"""
    return item.get("field_id") == SWAP_FIELD


# 大变化的配额（plan.big_single_quota / plan.combo_quota / plan.derived.quota）：动态字段、绘制技法各占几成
QUOTA_KINDS = ("dynamic", "technique")
QUOTA_REST = "rest"
QUOTA_LABELS = {"dynamic": "动态", "technique": "换画法", QUOTA_REST: "其余"}
# 三处组合里的绘制技法是同一画法大类内的整幅调整
COMBO_QUOTA_LABELS = {
    "dynamic": "含动态",
    "technique": "含技法调整",
    QUOTA_REST: "其余",
}


def _is_dynamic(item: dict[str, Any]) -> bool:
    return fields.is_dynamic(item.get("field_id"))


def _is_technique(item: dict[str, Any]) -> bool:
    return fields.canonical(item.get("field_id")) == fields.TECHNIQUE_FIELD


QUOTA_TESTS: dict[str, subjects.PairTest] = {
    "dynamic": _is_dynamic,
    "technique": _is_technique,
}


def quota_ratios(ctx: Ctx, key: str) -> dict[str, float]:
    """{类: 占几成}：dynamic（动态字段）/ technique（绘制技法），各 0–1、合计不超过 1；没写或写 0 的类不列。"""
    raw = ctx.setting(key) or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{key} 要写成 {{dynamic: 0.3, technique: 0.3}}，现在是 {raw!r}"
        )
    unknown = sorted(set(raw) - set(QUOTA_KINDS))
    if unknown:
        raise ConfigError(f"{key} 只认 {list(QUOTA_KINDS)}，不认识 {unknown}")
    out: dict[str, float] = {}
    for name in QUOTA_KINDS:
        value = raw.get(name)
        try:
            ratio = float(value or 0)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{key}.{name} 要是 0 到 1 之间的数，现在是 {value!r}"
            ) from exc
        if not 0 <= ratio <= 1:
            raise ConfigError(f"{key}.{name} 要是 0 到 1 之间的数，现在是 {value!r}")
        if ratio:
            out[name] = ratio
    if sum(out.values()) > 1 + 1e-9:
        raise ConfigError(f"{key} 各项合计不能超过 1，现在是 {sum(out.values()):.2f}")
    return out


def _any_of(tests: list[subjects.PairTest]) -> subjects.PairTest:
    return lambda item: any(test(item) for test in tests)


def quota_split(
    want: int,
    ratios: dict[str, float],
    cap: Callable[[dict[str, Any], str, int], int],
) -> list[dict[str, Any]]:
    """want 个槽位按配额分成互不重叠的几组：[{quota, limits, want, cap, count}]，QUOTA_KINDS 顺序，其余组在最后。

    第 i 个配额组 must 这一类、forbid 排在它前面的几类；其余组 forbid 全部配额类，同一个组合不会落进两组。
    每组不超过 cap(limits, 组名, 目标数)；一组凑不够由别的组补（先补其余组，再按顺序补配额组），总数不凑数。
    没有配额时只有一个不限定的其余组（和不分组一样）。"""
    names = [n for n in QUOTA_KINDS if ratios.get(n)]
    targets = {n: round(ratios[n] * want) for n in names}
    excess = sum(targets.values()) - want
    for name in reversed(names):
        cut = min(max(excess, 0), targets[name])
        targets[name] -= cut
        excess -= cut
    targets[QUOTA_REST] = want - sum(targets[n] for n in names)
    specs: list[tuple[str, dict[str, Any]]] = []
    for i, name in enumerate(names):
        limits: dict[str, Any] = {"must": QUOTA_TESTS[name]}
        if i:
            limits["forbid"] = _any_of([QUOTA_TESTS[n] for n in names[:i]])
        specs.append((name, limits))
    specs.append(
        (
            QUOTA_REST,
            {"forbid": _any_of([QUOTA_TESTS[n] for n in names])} if names else {},
        )
    )
    groups = []
    for name, limits in specs:
        limit = max(0, cap(limits, name, targets[name]))
        groups.append(
            {
                "quota": name,
                "limits": limits,
                "want": targets[name],
                "cap": limit,
                "count": min(targets[name], limit),
            }
        )
    spare = want - sum(g["count"] for g in groups)
    for group in [groups[-1], *groups[:-1]]:
        add = min(spare, group["cap"] - group["count"])
        if add > 0:
            group["count"] += add
            spare -= add
    return groups


def quota_counts(groups: list[dict[str, Any]]) -> dict[str, int] | None:
    """计数 / 批次里记下的配额分组：{dynamic, technique, rest: 个数}；没有配额（只有其余组）时为 None。"""
    if len(groups) < 2:
        return None
    return {g["quota"]: g["count"] for g in groups}


def quota_text(counts: dict[str, int] | None, labels: dict[str, str]) -> str:
    """「动态 4 · 换画法 4 · 其余 4」；没有配额时为空串。"""
    if not counts:
        return ""
    return " · ".join(f"{labels[k]} {n}" for k, n in counts.items() if k in labels)


def _passes(item: dict[str, Any], limits: dict[str, Any]) -> bool:
    return _set_ok([item], limits.get("must"), limits.get("forbid"))


def big_split(ctx: Ctx, pool: list[dict[str, Any]], want: int) -> list[dict[str, Any]]:
    """单字段大改按 plan.big_single_quota 分组：每个字段一轮最多 plan.big_single_repeat 次；换画法组里绘制技法
    能出到按 plan.big_single_count 算的目标数（每个分一种不同的画法大类，换画法清单），至少也是 big_single_repeat 次。

    上限不随这一次的 want 变：统计个数（pool_counts）和挑槽位（plan_slots）用不同的 want 调它，分出来的总数要一致。"""
    repeat = big_repeat(ctx)
    ratios = quota_ratios(ctx, "plan.big_single_quota")
    technique_cap = max(
        round(
            ratios.get("technique", 0) * _count_setting(ctx, "plan.big_single_count", 4)
        ),
        repeat,
    )

    def cap(limits: dict[str, Any], name: str, target: int) -> int:
        n = sum(1 for f in pool if _passes(f, limits))
        if name == "technique":
            return technique_cap if n else 0
        return n * repeat

    return quota_split(want, ratios, cap)


def combo_split(
    ctx: Ctx, pool: list[dict[str, Any]], want: int, *, by_unit: bool
) -> list[dict[str, Any]]:
    """三处组合按 plan.combo_quota 分组（含动态字段的 / 含绘制技法的 / 其余）；每组的上限是它的合法组合数
    （按单元时看 subjects.count_valid，按字段时去掉互斥的字段组合）。"""

    def cap(limits: dict[str, Any], name: str, target: int) -> int:
        if by_unit:
            return subjects.count_valid(pool, 3, want, **limits)
        return _set_count(pool, 3, want, **limits)

    return quota_split(want, quota_ratios(ctx, "plan.combo_quota"), cap)


def small_split(
    ctx: Ctx, pool: list[dict[str, Any]], *, by_unit: bool
) -> dict[str, Any]:
    """小变化怎么分组：{mode, ratio, swap, other, reason}，pool 是小变化的组合池（按单元）或字段池（按字段）。

    plan.small_swap_ratio 为 0 时 mode 为 None：不分组、照旧随机挑，other 是全部个数。否则换主体组
    round(比例 × 个数) 个、其余组补足，各自不超过合法组合数；一组凑不够由另一组补（先补换主体组），总数不凑数。
    这张图一个换主体的组合都没有时全部走其余组，reason 写原因。"""
    want = _count_setting(ctx, "plan.small_count", 12)
    ratio = swap_ratio(ctx)

    def count(**kw: Any) -> int:
        if by_unit:
            return subjects.count_valid(pool, 2, want, **kw)
        return _set_count(pool, 2, want, **kw)

    if not ratio:
        return {"mode": None, "ratio": 0.0, "swap": 0, "other": count(), "reason": None}
    swap_cap = count(must=_is_swap)
    other_cap = count(forbid=_is_swap)
    swap_want = round(ratio * want)
    swap = min(swap_want, swap_cap)
    other = min(want - swap_want, other_cap)
    spare = want - swap - other
    extra = min(spare, swap_cap - swap)
    swap += extra
    other += min(spare - extra, other_cap - other)
    return {
        "mode": "swap",
        "ratio": ratio,
        "swap": swap,
        "other": other,
        "reason": None if swap_cap else NO_SWAP_REASON,
    }


def planning_units(
    ctx: Ctx, fam: dict[str, Any]
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """(可变单元, 按字段策划的原因)：plan.unit 设成 fields、或这张图还没有可变单元清单时单元为 None。

    画面描述关着时不写原因（按字段是预期的，doctor 另有提示）。"""
    if subjects.unit_mode(ctx) == subjects.FIELD_MODE:
        return None, "plan.unit 设成了 fields：这一轮按字段策划"
    units = subjects.inventory(
        ctx, fam["spec"].get("analysis_id") or fam.get("analysis_id")
    )
    if units is None:
        if not describe_enabled(ctx):
            return None, None
        seed = fam.get("seed_id") or "这张参考图"
        return None, (
            f"{seed} 还没有可变单元清单（画面描述没写成或是旧版）：这一轮按字段策划；"
            f"podctl seed describe {seed} --force 补上"
        )
    return units, None


def big_applicable(
    pool: list[dict[str, Any]], units: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """大字段池里的动态字段要在可变单元里找得到能挂的单元（类型对得上、依赖满足、单元没否决、不是小档，口径同
    组合池）：只有一个角色的图不出距离互动，没有承托物的不出承托依附。其余大字段不受单元影响。"""
    return [
        info
        for info in pool
        if not fields.is_dynamic(info["field_id"]) or subjects.pair_pool(units, [info])
    ]


def unit_pools(
    ctx: Ctx,
    fam: dict[str, Any],
    pools: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """这一轮按什么策划：{mode, units, pairs, reason, pools}；pairs 按槽位类型分（{small, combo}：小变化池与
    三处组合池各自的「单元 × 字段」组合）。按字段策划时 units / pairs 为 None、reason 写原因。
    有单元清单时大字段池里的动态字段要找得到能挂的单元（big_applicable）。"""
    pools = pools if pools is not None else field_pools(ctx, fam)
    units, reason = planning_units(ctx, fam)
    if units is not None:
        pools = {**pools, BIG: big_applicable(pools[BIG], units)}
    pairs = (
        None
        if units is None
        else {kind: subjects.pair_pool(units, pools[kind]) for kind in (SMALL, COMBO)}
    )
    return {
        "mode": subjects.FIELD_MODE if units is None else subjects.UNIT_MODE,
        "units": units,
        "pairs": pairs,
        "reason": reason,
        "pools": pools,
    }


def pool_counts(
    ctx: Ctx,
    pools: dict[str, list[dict[str, Any]]],
    pairs: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    """这一轮每类出几个：小变化 plan.small_count 个；大变化 plan.big_count 个，其中单字段大改 plan.big_single_count 个，
    其余是三处组合。池不够时少出（不凑数）：大字段不够就多出三处组合，凑不成三处组合就多出单字段大改。

    小变化 / 三处组合的上限是各自合法的组合数：按单元策划（给了 pairs）时看 subjects.count_valid，按字段时去掉
    互斥的字段组合；小变化分组（small_split）时另给 small_swap / small_other。单字段大改的上限是大字段数 ×
    plan.big_single_repeat（换画法组另算，见 big_split）。单字段大改 / 三处组合配了配额时另给
    big_quota / combo_quota（{dynamic, technique, rest: 个数}）。"""
    big_total = _count_setting(ctx, "plan.big_count", 8)
    single_want = min(_count_setting(ctx, "plan.big_single_count", 4), big_total)
    big_groups = big_split(ctx, pools[BIG], single_want)
    single = sum(g["count"] for g in big_groups)
    split = small_split(
        ctx,
        pools[SMALL] if pairs is None else pairs[SMALL],
        by_unit=pairs is not None,
    )
    small = split["swap"] + split["other"]
    combo_pool = pools[COMBO] if pairs is None else pairs[COMBO]
    if pairs is None:
        combo = _set_count(combo_pool, 3, big_total - single)
    else:
        combo = subjects.count_valid(combo_pool, 3, big_total - single)
    if single + combo < big_total:
        # 凑不成三处组合时多出单字段大改
        single = max(
            single,
            sum(g["count"] for g in big_split(ctx, pools[BIG], big_total - combo)),
        )
    # 记下的分组按最终个数重算，和 plan_slots 挑槽位时一致
    big_groups = big_split(ctx, pools[BIG], single)
    combo_groups = combo_split(ctx, combo_pool, combo, by_unit=pairs is not None)
    out = {SMALL: small, BIG: single, COMBO: combo, "total": small + single + combo}
    if split["mode"]:
        out["small_swap"] = split["swap"]
        out["small_other"] = split["other"]
    for key, groups in (("big_quota", big_groups), ("combo_quota", combo_groups)):
        record = quota_counts(groups)
        if record:
            out[key] = record
    return out


def _clash(fids: list[str]) -> bool:
    """按字段挑时互斥的字段不同组（换物种和轮廓 / 头身、整体配色和单元配色）。"""
    return any(b in fields.conflicts(a) for a, b in itertools.permutations(fids, 2))


def _set_ok(
    items: list[dict[str, Any]],
    must: subjects.PairTest | None,
    forbid: subjects.PairTest | None,
) -> bool:
    """按字段挑的一组：forbid 一个都不能命中，must 至少命中一个。"""
    if forbid and any(forbid(f) for f in items):
        return False
    return not must or any(must(f) for f in items)


def _set_count(
    pool: list[dict[str, Any]],
    size: int,
    cap: int,
    *,
    must: subjects.PairTest | None = None,
    forbid: subjects.PairTest | None = None,
) -> int:
    """字段池里能组成几组（去掉互斥的，数到 cap 就停）；must / forbid 同 _pick_sets。"""
    if cap <= 0 or len(pool) < size:
        return 0
    n = 0
    for combo in itertools.combinations(pool, size):
        if _set_ok(list(combo), must, forbid) and not _clash(
            [f["field_id"] for f in combo]
        ):
            n += 1
            if n >= cap:
                break
    return n


def _pick_sets(
    pool: list[dict[str, Any]],
    size: int,
    count: int,
    rng: random.Random,
    used: dict[str, int],
    done: set[frozenset],
    *,
    must: subjects.PairTest | None = None,
    forbid: subjects.PairTest | None = None,
    repeat: int = 1,
) -> list[list[dict[str, Any]]]:
    """从字段池里随机挑 count 组、每组 size 个字段：同一批里一组最多挑 repeat 次（缺省 1，不重复）。

    权重：少被改过的字段优先（used：这个系列每个字段被改过几次）；一组里有同维度的字段、和做过的组合（done）一样的
    往后排；挑中一组后，其中的字段这一批里再被挑中的权重减半，让字段分散开。互斥的字段不同组。
    must：每组至少一个字段满足；forbid：每组一个都不能满足（小变化的换主体组 / 其余组）。
    """
    if count <= 0 or len(pool) < size:
        return []
    weights: dict[tuple[int, ...], float] = {}
    for combo in itertools.combinations(range(len(pool)), size):
        items = [pool[i] for i in combo]
        fids = [f["field_id"] for f in items]
        if not _set_ok(items, must, forbid) or _clash(fids):
            continue
        weight = math.prod(1.0 / (1 + used.get(f, 0)) for f in fids)
        if len({pool[i]["dimension"] for i in combo}) < size:
            weight *= SAME_DIMENSION_WEIGHT
        if frozenset(fids) in done:
            weight *= DONE_COMBO_WEIGHT
        weights[combo] = weight
    picked: list[list[dict[str, Any]]] = []
    hits: Counter[int] = Counter()
    taken: Counter[tuple[int, ...]] = Counter()
    while len(picked) < count and weights:
        candidates = list(weights)
        chosen = rng.choices(
            candidates,
            weights=[
                weights[c] * PICKED_FIELD_DECAY ** sum(hits[i] for i in c)
                for c in candidates
            ],
        )[0]
        taken[chosen] += 1
        if taken[chosen] >= max(1, repeat):
            del weights[chosen]
        hits.update(chosen)
        picked.append([pool[i] for i in chosen])
    return picked


def plan_slots(
    ctx: Ctx,
    fam: dict[str, Any],
    *,
    rng: random.Random | None = None,
    used: dict[str, int] | None = None,
    done: set[frozenset] | None = None,
    pools: dict[str, list[dict[str, Any]]] | None = None,
    units: dict[str, Any] | None = None,
    history: tuple[Counter, Counter, set, set] | None = None,
) -> dict[str, Any]:
    """这一轮的槽位：小变化 S1…（两处）、单字段大改 B1…（一个大字段）、三处组合 T1…（三处），
    由 rng 随机挑（策划时用「系列:批次」做种子，同一批能复现）。

    units 是 unit_pools 的结果（不给就现查）。按单元策划时小变化与三处组合各从自己池里的「单元 × 字段」组合挑
    （history = subjects.history(方向记录)），槽位另带 pairs / unit_ids；按字段策划时从各自的字段池挑（used / done
    按字段）。单字段大改两种模式都按字段挑。单字段大改 / 三处组合按配额分组挑（big_split / combo_split），
    配额组里的槽位带 quota（dynamic / technique）。返回 {slots, counts, mode, units, reason, swap}。"""
    inputs = units if units is not None else unit_pools(ctx, fam, pools)
    pools, pairs = inputs["pools"], inputs["pairs"]
    counts = pool_counts(ctx, pools, pairs)
    split = small_split(
        ctx,
        pools[SMALL] if pairs is None else pairs[SMALL],
        by_unit=pairs is not None,
    )
    rng = rng or random.Random(0)
    p_used, p_legacy_used, p_done, p_legacy_done = history or (
        Counter(),
        Counter(),
        set(),
        set(),
    )
    repeat = big_repeat(ctx)
    # 小变化分组时：换主体组（每组至少一处 D09）与其余组（不换主体）各挑各的；不分组时只有一组、不限定。
    # 单字段大改 / 三处组合按配额分组（没配额时只有一组、不限定）
    groups: dict[str, list[dict[str, Any]]] = {
        SMALL: [
            {"count": split["swap"], "limits": {"must": _is_swap}},
            {"count": split["other"], "limits": {"forbid": _is_swap}},
        ]
        if split["mode"]
        else [{"count": counts[SMALL], "limits": {}}],
        BIG: big_split(ctx, pools[BIG], counts[BIG]),
        COMBO: combo_split(
            ctx,
            pools[COMBO] if pairs is None else pairs[COMBO],
            counts[COMBO],
            by_unit=pairs is not None,
        ),
    }
    slots: list[dict[str, Any]] = []
    for kind in (SMALL, BIG, COMBO):
        size = FIELDS_PER_KIND[kind]
        tagged = len(groups[kind]) > 1 and kind != SMALL
        if kind != BIG and pairs is not None:
            sets: list[tuple[list[dict[str, Any]], str | None]] = []
            for group in groups[kind]:
                picked_sets = subjects.pick_pair_sets(
                    pairs[kind],
                    size,
                    group["count"],
                    rng,
                    used=p_used,
                    legacy_used=p_legacy_used,
                    done=p_done,
                    legacy_done=p_legacy_done,
                    **group["limits"],
                )
                quota = group.get("quota") if tagged else None
                sets += [(s, quota) for s in picked_sets]
            # 每组里的几处已按组合池的顺序（单元顺序、字段字典顺序）；挑选卡上同一单元的方向挨在一起
            pos = {p["pair_id"]: i for i, p in enumerate(pairs[kind])}
            sets.sort(key=lambda item: [pos[p["pair_id"]] for p in item[0]])
            for i, (picked, quota) in enumerate(sets, 1):
                slots.append(
                    {
                        "slot_id": f"{SLOT_PREFIX[kind]}{i}",
                        "kind": kind,
                        "field_ids": [p["field_id"] for p in picked],
                        "fields": picked,
                        "pairs": picked,
                        "unit_ids": [p["unit_id"] for p in picked],
                        "swap": any(_is_swap(p) for p in picked),
                        **({"quota": quota} if quota and quota != QUOTA_REST else {}),
                    }
                )
            continue
        pool = pools[kind]
        sets = []
        for group in groups[kind]:
            if kind == BIG:
                # 换画法组里绘制技法可以出到组里的个数（每个分一种不同的画法大类）
                times = (
                    max(group["count"], repeat)
                    if group.get("quota") == "technique"
                    else repeat
                )
            else:
                times = 1
            picked_sets = _pick_sets(
                pool,
                size,
                group["count"],
                rng,
                used or {},
                done or set(),
                repeat=times,
                **group["limits"],
            )
            quota = group.get("quota") if tagged else None
            sets += [(s, quota) for s in picked_sets]
        # 随机只决定挑哪几组；挑中的按字段字典顺序排，挑选卡上同一字段的方向挨在一起
        pos = {f["field_id"]: i for i, f in enumerate(pool)}
        sets.sort(key=lambda item: [pos[f["field_id"]] for f in item[0]])
        for i, (picked, quota) in enumerate(sets, 1):
            slots.append(
                {
                    "slot_id": f"{SLOT_PREFIX[kind]}{i}",
                    "kind": kind,
                    "field_ids": [f["field_id"] for f in picked],
                    "fields": picked,
                    "swap": any(_is_swap(f) for f in picked),
                    **({"quota": quota} if quota and quota != QUOTA_REST else {}),
                }
            )
    return {
        "slots": slots,
        "counts": counts,
        "mode": inputs["mode"],
        "units": inputs["units"],
        "reason": inputs["reason"],
        "swap": split,
    }


def slot_counts(ctx: Ctx, family_id: str) -> dict[str, int] | None:
    """策划按钮上写的数量（小变化 / 单字段大改 / 三处组合）；系列不能按字段策划时返回 None。"""
    try:
        inputs = unit_pools(ctx, planning_family(ctx, family_id))
        return pool_counts(ctx, inputs["pools"], inputs["pairs"])
    except (StageError, ConfigError):
        return None


def slot_changes(slot: dict[str, Any]) -> list[str]:
    """槽位里的几处给人看：「高地牛·表情」「整体配色」；按字段策划的写字段短名。"""
    if slot.get("pairs"):
        return [subjects.pair_label(p) for p in slot["pairs"]]
    return [f["label"] for f in slot["fields"]]


def counts_text(counts: dict[str, int] | None) -> str:
    """「小变化 12 · 大变化 8（单字段大改 4 + 三处组合 4）」；没有三处组合时只写「大变化 n」。"""
    if not counts:
        return ""
    small, single, combo = (counts.get(k, 0) for k in (SMALL, BIG, COMBO))
    parts = [f"小变化 {small}"] if small else []
    if small and counts.get("small_swap") is not None:
        parts[0] += f"（换主体 {counts['small_swap']}）"
    if single or combo:
        detail = " + ".join(
            f"{KIND_LABELS[k]} {n}" for k, n in ((BIG, single), (COMBO, combo)) if n
        )
        parts.append(f"大变化 {single + combo}" + (f"（{detail}）" if combo else ""))
    if counts.get(DERIVED):
        parts.append(f"{KIND_LABELS[DERIVED]} {counts[DERIVED]}")
    return " · ".join(parts)


def plan_button_label(
    counts: dict[str, int] | None, *, again: bool = False, auto: bool = False
) -> str:
    """「策划 20 个方向」；数不出来时「按可变字段策划」。auto（方向自动确认开着）时补「并出图」：点了就花出图的钱。"""
    tail = "并出图" if auto else ""
    if counts and counts.get("total"):
        return f"{'再' if again else ''}策划 {counts['total']} 个方向{tail}"
    return ("按可变字段再策划一轮" if again else "按可变字段策划") + tail


def _groups(
    slots: dict[str, Any],
    limit: int | None,
    kinds: set[str] | None = None,
    per_call: int = 4,
) -> list[dict[str, Any]]:
    """按类型分块（每块 per_call 个槽位，一块一次 P05 调用）；kinds 只取这几类槽位，limit 只取前 n 个。"""
    seq = list(slots["slots"])
    if kinds:
        seq = [s for s in seq if s["kind"] in kinds]
    if limit:
        seq = seq[:limit]
    per_call = max(1, per_call)
    groups = []
    for kind in KIND_LABELS:
        mine = [s for s in seq if s["kind"] == kind]
        for n, start in enumerate(range(0, len(mine), per_call), 1):
            groups.append(
                {
                    "key": f"{kind}:{n}",
                    "kind": kind,
                    "slots": mine[start : start + per_call],
                }
            )
    return groups


# 方向记录的状态归类（方向记录卡的统计、P05 的状态说明共用）
STATE_LABELS = {
    "done": "已出完",
    "running": "出图中",
    "waiting": "待出图",
    "failed": "没出成",
    "unpicked": "没选",
    "cancelled": "已停掉",
    "pending": "待挑选",
}


def _direction_state(selection: str | None, job_status: str | None, images: int) -> str:
    if selection == "rejected":
        return "unpicked"
    if selection == "cancelled":
        return "cancelled"
    if job_status is None:
        return "pending" if selection == "pending" else "unpicked"
    if job_status == "complete":
        return "done" if images else "failed"
    if job_status == "failed":
        return "failed"
    return "running" if job_status == "running" else "waiting"


def _brief_to(record: dict[str, Any]) -> dict[tuple[str, Any], Any]:
    """简述里每处改动的短句：按（单元, 字段）查；旧方向的简述没有单元。"""
    return {
        _change_key(b): b.get("to")
        for b in record.get("brief") or []
        if isinstance(b, dict) and b.get("field_id")
    }


def direction_signature(record: dict[str, Any]) -> frozenset:
    """方向的「字段 + 改成的样子」（规范化后），认出换了说法的同一个方向；停用字段归到并入的字段。

    落在单元上的改动另带单元键（「单元键, 字段, 样子」）；改成的样子优先取简述里的短句，没有简述的旧方向取改动的目标值。
    """
    briefs = _brief_to(record)
    sig = set()
    for cf in record.get("changed_fields") or []:
        if not isinstance(cf, dict):
            continue
        fid = cf.get("field_id")
        to = briefs.get(_change_key(cf)) or cf.get("to")
        item = (fields.canonical(fid) or str(fid), normalize_phrase(to))
        sig.add((str(cf["unit_key"]), *item) if cf.get("unit_key") else item)
    base = base_variant(record)
    if sig and base:
        # 二次大变化：同一个大改落在不同底图上不算同一个方向
        sig.add(("base", base, ""))
    return frozenset(sig)


def unitless_signature(signature: frozenset) -> frozenset:
    """去掉单元键只看「字段 + 样子」：新方向换个单元说法、和没有单元的旧方向一样时也算重复。"""
    return frozenset(tuple(item[-2:]) for item in signature)


def brief_line(item: dict[str, Any]) -> str:
    """一处改动：「底色：暖白 → 雾蓝」，没写原来的样子时「底色：改成 雾蓝」。"""
    if item.get("from"):
        return f"{item['aspect']}：{item['from']} → {item['to']}"
    return f"{item['aspect']}：改成 {item['to']}"


def direction_tag(
    task_type: str | None, aspects: list[str], kind: str | None = None
) -> str:
    """「小变化 · 表情动作 + 配色结构」「大变化 · 主题叙事」「大变化 · 配色结构 + 服饰道具 + 元素角色」；
    二次大变化是「二次大变化 · 主题叙事」；梳理前的变体是「组合 · 改 n 处」或「对照 · 只改某处」
    （没有类型的，只改一处算对照）。"""
    if not aspects:
        return ""
    if kind == DERIVED:
        return f"{KIND_LABELS[DERIVED]} · " + " + ".join(aspects)
    if task_type == SMALL:
        return "小变化 · " + " + ".join(aspects)
    if task_type == BIG:
        return "大变化 · " + " + ".join(aspects)
    if task_type == "control" or (task_type is None and len(aspects) == 1):
        return f"对照 · 只改{aspects[0]}"
    return f"组合 · 改 {len(aspects)} 处"


def direction_summary(plan: dict[str, Any], axis: str | None) -> dict[str, Any]:
    """给人看的方向：标题、类型、方位、简述、不变（方向记录与选款共用）。

    改版前的变体没有简述：标题用变化轴，简述按改动字段补出来。
    """
    legacy = not plan.get("brief")
    info = reconcile_brief(plan) if legacy else plan
    keep = plan.get("keep") or ""
    if plan.get("task_type") == SMALL and not keep:
        keep = SMALL_KEEP
    return {
        "title": (axis if legacy and axis else None) or info.get("title") or "",
        "task_type": plan.get("task_type"),
        "kind": plan.get("kind"),
        "aspects": info.get("aspects") or [],
        "brief": info.get("brief") or [],
        "keep": keep,
    }


def asset_label(asset_id: str, plan: dict[str, Any] | None, axis: str | None) -> str:
    """素材对外的名字「IMG00123_三伙伴开怀大笑」：编号 + 方向短语（「方向」列去掉 V 编号的那句）。

    选款表 / 候选表的「素材」列、缩略图附件名、下载原图与发原图的文件名、套图里的设计图都用它；
    短语里的空白换成下划线，去掉文件名不能有的字符；没有短语时只有编号。
    """
    if not plan and not axis:
        return asset_id
    title = direction_summary(plan or {}, axis)["title"]
    phrase = _LABEL_UNSAFE.sub("", "_".join(str(title or "").split()))
    phrase = re.sub(r"_{2,}", "_", phrase)[:LABEL_PHRASE_CHARS].strip("_.")
    return f"{asset_id}_{phrase}" if phrase else asset_id


def asset_label_of(ctx: Ctx, asset_id: str) -> str:
    """按编号查方向再拼名字；素材或方向找不到时只有编号。"""
    row = ctx.db.q1(
        "SELECT v.plan_json, v.axis FROM assets a"
        " LEFT JOIN variants v ON v.variant_id = a.variant_id WHERE a.asset_id = ?",
        (asset_id,),
    )
    if not row:
        return asset_id
    return asset_label(asset_id, loads(row["plan_json"], {}) or {}, row["axis"])


def direction_records(ctx: Ctx, family_id: str) -> list[dict[str, Any]]:
    """这个系列做过和提过的全部方向（各版本、各批次），按编号排序。"""
    rows = ctx.db.q(
        "SELECT v.*, j.status AS job_status FROM variants v"
        " LEFT JOIN jobs j ON j.variant_id = v.variant_id"
        " WHERE v.family_id = ? ORDER BY v.variant_id",
        (family_id,),
    )
    records = []
    for row in rows:
        plan = loads(row["plan_json"], {}) or {}
        assets = ctx.db.q(
            "SELECT status, human_decision FROM assets WHERE variant_id = ? AND status != 'superseded'",
            (row["variant_id"],),
        )
        images = sum(1 for a in assets if a["status"] not in ("failed", "rejected"))
        approved = sum(1 for a in assets if a["human_decision"] == "approved")
        summary = direction_summary(plan, row["axis"])
        selection = (plan.get("selection") or {}).get("status")
        state = _direction_state(selection, row["job_status"], images)
        text = STATE_LABELS[state]
        if state == "done":
            text += f" · {images} 张" + (f" · 选中 {approved}" if approved else "")
        records.append(
            {
                "variant_id": row["variant_id"],
                "batch_id": row["batch_id"],
                "family_version": row["family_version"],
                "created_at": row["created_at"],
                "title": summary["title"] or row["variant_id"],
                "axis": row["axis"],
                "task_type": summary["task_type"],
                "kind": summary["kind"],
                "aspects": summary["aspects"],
                "brief": summary["brief"],
                "keep": summary["keep"],
                "selection": selection,
                "job_status": row["job_status"],
                "state": state,
                "state_text": text,
                "images": images,
                "approved": approved,
                "asset_statuses": dict(Counter(a["status"] for a in assets)),
                "changed_fields": plan.get("changed_fields") or [],
                "plan_hash": row["plan_hash"],
                "base": plan.get("base"),
                "parent_variant_id": row["parent_variant_id"],
                # 换画法的方向换成了哪种画法大类（换画法清单避开做过的）
                "technique": plan.get("technique"),
            }
        )
    return records


def field_history(records: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """按字段汇总做过和提过的目标值：{字段: [{to, kind, status}]}；停用字段归到并入的字段，每个字段只留最近几条。

    kind 是方向类型（小变化 / 大变化 / 三处组合，2026-10-21 之前的组合叫两轴组合，梳理前的为「旧方向」）：
    绘制技法同一个字段上既有同一画法内的调整，也有换画法大类。落在单元上的改动另带 unit（单元名）。
    """
    out: dict[str, list[dict[str, str]]] = {}
    for record in records:
        briefs = _brief_to(record)
        kind = KIND_LABELS.get(record.get("kind") or "", "旧方向")
        if record.get("kind") == COMBO and len(record["changed_fields"]) == 2:
            kind = "两轴组合"
        for cf in record["changed_fields"]:
            if not isinstance(cf, dict):
                continue
            fid = fields.canonical(cf.get("field_id"))
            if not fid:
                continue
            short = briefs.get(_change_key(cf)) or cf.get("to")
            item = {
                "to": clip(short, HISTORY_TO_CHARS),
                "kind": kind,
                "status": record["state_text"],
            }
            if _on_unit(cf):
                item["unit"] = cf.get("unit_name") or cf["unit_id"]
            out.setdefault(fid, []).append(item)
    return {fid: items[-HISTORY_PER_FIELD:] for fid, items in out.items()}


def _change_spots(record: dict[str, Any]) -> list[tuple[str, str]]:
    """一个方向改了哪几处：（单元键, 字段），旧方向的单元键为空；停用字段归到并入的字段。"""
    spots = []
    for cf in record["changed_fields"]:
        if not isinstance(cf, dict):
            continue
        fid = fields.canonical(cf.get("field_id"))
        if fid and (str(cf.get("unit_key") or ""), fid) not in spots:
            spots.append((str(cf.get("unit_key") or ""), fid))
    return spots


def multi_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """改了不止一处的方向（小变化、三处组合、以前的两轴组合与组合变形）：标题 + 简述，多处方向用来避开。"""
    out = []
    for record in records:
        spots = _change_spots(record)
        if len(spots) < 2:
            continue
        out.append(
            {
                "title": record["title"],
                "fields": [b["aspect"] for b in record["brief"] if isinstance(b, dict)][
                    :4
                ]
                if any(key for key, _ in spots)
                else [fields.label(f) for f in sorted({f for _, f in spots})],
                "brief": [brief_line(b) for b in record["brief"]][:4],
                "status": record["state_text"],
            }
        )
    return out


def _history(
    ctx: Ctx, family_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """(方向记录, 覆盖统计, 排除项)：方向记录是做过和提过的全部方向，P05 不能重复其中任何一个。"""
    records = direction_records(ctx, family_id)
    rejected = []
    kinds: Counter[str] = Counter()
    by_field: Counter[str] = Counter()
    by_dim: Counter[str] = Counter()
    by_pair: Counter[str] = Counter()
    for record in records:
        kinds[record["kind"] or record["task_type"] or "legacy"] += 1
        for cf in record["changed_fields"]:
            if not isinstance(cf, dict):
                continue
            fid = fields.canonical(cf.get("field_id")) or str(cf.get("field_id"))
            by_field[fid] += 1
            if _on_unit(cf):
                by_pair[change_aspect({**cf, "field_id": fid})] += 1
            dim = fields.dimension(fid)
            if dim:
                by_dim[fields.dimension_name(dim)] += 1
        statuses = record["asset_statuses"]
        if record["selection"] == "rejected":
            rejected.append(
                {
                    "variant_id": record["variant_id"],
                    "title": record["title"],
                    "brief": record["brief"],
                    "reason": "人工挑选时没选",
                }
            )
        elif statuses and set(statuses) <= {"rejected", "duplicate", "failed"}:
            rejected.append(
                {
                    "variant_id": record["variant_id"],
                    "title": record["title"],
                    "brief": record["brief"],
                    "asset_statuses": statuses,
                }
            )
    coverage = {
        "total_variants": len(records),
        "count_by_kind": dict(kinds),
        "count_by_field": dict(by_field),
        "count_by_dimension": dict(by_dim),
        # 落在单元上的改动按「单元·字段」计（按字段策划与整幅的不在里面）
        "count_by_pair": dict(by_pair),
        "unexplored_fields": [
            fid for fid in fields.variation_field_ids() if fid not in by_field
        ],
    }
    return records, coverage, rejected


def _spec_for_prompt(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    variation = dict(out.get("variation") or {})
    variation.pop("notes", None)
    if variation:
        out["variation"] = variation
    return out


def _kind_rule(kind: str) -> str:
    if kind == BIG:
        return BIG_RULE
    if kind == COMBO:
        return COMBO_RULE
    if kind == DERIVED:
        return DERIVED_RULE
    return fields.tier_rule(SMALL)


def _field_for_prompt(info: dict[str, Any], kind: str) -> dict[str, Any]:
    out = {
        k: info[k]
        for k in (
            "field_id",
            "field",
            "label",
            "dimension",
            "current",
            "options",
            "compatibility",
            "layout_change",
        )
        if k in info
    }
    fid = info["field_id"]
    # 小变化、三处组合里的字段：怎么改才一眼看得出；单字段大改与二次大变化：怎么大改（绘制技法换画法大类）
    if kind in (SMALL, COMBO) and fields.small_change(fid):
        out["small_change"] = fields.small_change(fid)
    if kind in (BIG, DERIVED) and fields.big_change(fid):
        out["big_change"] = fields.big_change(fid)
    if fields.change_forms(fid):
        out["change_forms"] = fields.change_forms(fid)
    return out


def _spot_key(item: dict[str, Any]) -> str:
    """槽位里一处的键：按单元策划的是 pair_id（「U01:D09」），按字段策划的是字段号。"""
    return str(item.get("pair_id") or item["field_id"])


def _slot_for_prompt(slot: dict[str, Any]) -> dict[str, Any]:
    """槽位给 P05：fields 每项是一处；按单元策划的另带 unit_id 与单元信息（unit_id 为 canvas 的改整幅）。

    换主体那处另带 swap_to（P14 清单分给这一处的新主体）或 swap_avoid（清单不够时要避开的主体）；
    换画法那处（单字段大改 / 二次大变化里的绘制技法）另带 technique_to（换画法清单分给它的画法大类）与
    technique_spare（没分出去的备选）；二次大变化的槽位另带 base（底图是哪个小变化、主体已经换成了什么）。"""
    swaps = slot.get("swaps") or {}
    technique = slot.get("technique") or {}

    def spot(item: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        extra = dict(swaps.get(_spot_key(item), {}))
        if technique and _is_technique(item):
            extra.update(technique)
        return {**body, **extra}

    if slot.get("pairs"):
        out = {
            "slot_id": slot["slot_id"],
            "fields": [
                spot(
                    p,
                    {
                        "unit_id": p["unit_id"],
                        "unit": subjects.unit_for_prompt(p),
                        **_field_for_prompt(p, slot["kind"]),
                    },
                )
                for p in slot["pairs"]
            ],
        }
    else:
        out = {
            "slot_id": slot["slot_id"],
            "fields": [
                spot(f, _field_for_prompt(f, slot["kind"])) for f in slot["fields"]
            ],
        }
    if slot.get("base"):
        out["base"] = base_for_prompt(slot["base"])
    return out


def _group_input(
    group: dict[str, Any],
    by_field: dict[str, list[dict[str, str]]],
    multi: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(plan_group, completed_plan_index)：这一组的槽位（每个槽位要改的字段）与规则，以及这些字段上做过的方向。"""
    kind = group["kind"]
    plan_group: dict[str, Any] = {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "fields_per_slot": FIELDS_PER_KIND[kind],
        "rule": _kind_rule(kind),
        "slots": [_slot_for_prompt(s) for s in group["slots"]],
    }
    if kind == SMALL:
        plan_group["composition_lock"] = fields.COMPOSITION_LOCK
    fids = dict.fromkeys(f for s in group["slots"] for f in s["field_ids"])
    bank = fields.text_bank_for_prompt(fids)
    if bank:
        plan_group["text_bank"] = bank
    index: dict[str, Any] = {"fields_done": {f: by_field.get(f, []) for f in fids}}
    if kind not in (BIG, DERIVED):
        index["combos_done"] = multi
    return plan_group, index


def _fit_task(task: dict[str, Any], slot: dict[str, Any]) -> str | None:
    """按槽位核对改动；不合格返回丢弃原因。

    槽位里的每处都要改（缺一处就丢弃，slot_mismatch），写成停用字段的按并入的字段算；多写的改动：小变化丢掉，
    单字段大改和三处组合改记为联动改动。按单元策划的槽位每处按（unit_id, 字段）核对：没写 unit_id、而这个字段在
    槽位里只有一处时按字段对上；对上的改动补上单元信息（unit_id / unit_key / unit_name / unit_en），槽位里标了
    layout_change 的那处也带上（出图与质检知道这处可以调整布局）。
    """
    if slot.get("pairs"):
        wanted = [(p["unit_id"], p["field_id"]) for p in slot["pairs"]]
        by_key = dict(zip(wanted, slot["pairs"]))
        layout = {k for k, p in by_key.items() if p.get("layout_change")}
    else:
        wanted = [("", f) for f in slot["field_ids"]]
        by_key = {}
        layout = {
            ("", f["field_id"])
            for f in slot.get("fields") or []
            if f.get("layout_change")
        }
    slot_fids = [f for _, f in wanted]
    picked: dict[tuple[str, str], dict[str, Any]] = {}
    extra: list[dict[str, Any]] = []
    for cf in task.get("changed_fields") or []:
        if not isinstance(cf, dict) or not cf.get("field_id"):
            continue
        fid = fields.canonical(cf["field_id"]) or cf["field_id"]
        key = (str(cf.get("unit_id") or "") if by_key else "", fid)
        if key not in wanted and not cf.get("unit_id") and slot_fids.count(fid) == 1:
            key = wanted[slot_fids.index(fid)]
        if key in wanted and key not in picked:
            picked[key] = {**cf, "field_id": fid}
        else:
            extra.append(cf)
    if len(picked) < len(wanted):
        return "slot_mismatch"
    task["changed_fields"] = []
    for key in wanted:
        cf = picked[key]
        pair = by_key.get(key)
        if pair is not None:
            cf = {
                **cf,
                "unit_id": pair["unit_id"],
                "unit_key": pair["unit_key"],
                "unit_name": pair["unit_name"],
            }
            if pair["unit_en"]:
                cf["unit_en"] = pair["unit_en"]
        if key in layout:
            cf["layout_change"] = True
        task["changed_fields"].append(cf)
    if extra:
        task["extra_fields_dropped"] = [cf.get("field_id") for cf in extra]
        if slot["kind"] != SMALL:
            task["coupled_changes"] = list(task.get("coupled_changes") or []) + [
                f"{fields.label(cf.get('field_id'))}随之改成：{plain(cf.get('to'))}"
                for cf in extra
            ]
    return None


def clean_hint(hint: str | None) -> str | None:
    """人对这一轮的要求：去掉首尾空白，空的当没有；超长报错。"""
    text = " ".join((hint or "").split())
    if len(text) > HINT_CHARS:
        raise StageError(f"这一轮的要求最多 {HINT_CHARS} 个字，现在 {len(text)} 个")
    return text or None


def _retire_stale_batches(conn, family_id: str, family_version: int) -> list[str]:
    """系列有了新版本：旧版本上还没人挑的候选作废（没出过图，直接删掉，不占计划指纹）。"""
    stale = [
        r["batch_id"]
        for r in conn.execute(
            "SELECT batch_id FROM batches WHERE family_id = ? AND status = 'proposed' AND family_version != ?",
            (family_id, family_version),
        ).fetchall()
    ]
    for batch_id in stale:
        conn.execute("DELETE FROM variants WHERE batch_id = ?", (batch_id,))
        conn.execute(
            "UPDATE batches SET status = 'superseded', updated_at = ? WHERE batch_id = ?",
            (now_iso(), batch_id),
        )
        conn.execute(
            "UPDATE reviews SET status = 'dismissed', note = '系列有了新版本，这批候选作废'"
            " WHERE subject_type = 'plan_selection' AND subject_id = ? AND status = 'open'",
            (batch_id,),
        )
        log_event(conn, batch_id, "batch_superseded", {"family_id": family_id})
    return stale


def _group_name(group: dict[str, Any]) -> str:
    """「小变化 S5–S8」：这一组是哪类、哪几个槽位（策划没出来时卡片上写）。"""
    ids = [s["slot_id"] for s in group["slots"]]
    span = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
    return f"{KIND_LABELS[group['kind']]} {span}"


def _field_usage(
    records: list[dict[str, Any]],
) -> tuple[Counter[str], set[frozenset]]:
    """按字段算的历史（按字段策划与单字段大改用）：每个字段改过几次、做过的字段组合。"""
    used: Counter[str] = Counter()
    done: set[frozenset] = set()
    for record in records:
        fids = {
            fields.canonical(cf.get("field_id"))
            for cf in record["changed_fields"]
            if isinstance(cf, dict)
        } - {None}
        used.update(fids)
        if fids:
            done.add(frozenset(fids))
    return used, done


def _unit_record(slots: dict[str, Any]) -> dict[str, Any]:
    """批次 plan_json 里记下这一轮按什么策划：按单元时带挑中的「单元:字段」与当时的单元清单，按字段时带原因。"""
    if slots["mode"] != subjects.UNIT_MODE:
        return {"plan_unit": slots["mode"], "unit_reason": slots["reason"]}
    return {
        "plan_unit": slots["mode"],
        "slot_pairs": {
            s["slot_id"]: [p["pair_id"] for p in s["pairs"]]
            for s in slots["slots"]
            if s.get("pairs")
        },
        "units": subjects.unit_overview(slots["units"]),
    }


# ---------------------------------------------------------------- 换主体清单（P14）

SWAP_PROMPT = "P14"
# 每个单元比要换的次数多要几个，去掉和历史重复的之后还够分
SWAP_SPARE = 3
SWAP_NAME_CHARS = 24


def swap_spots(slot_list: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """这一轮要换主体的单元：{单元键: {item: 第一处的组合 / 字段, spots: [(槽位, 处的键)]}}，按槽位顺序
    （小变化在前、三处组合在后，清单先分给小变化）；按字段策划（没有单元）时只有一个键 D09（全部主体一起换）。"""
    out: dict[str, dict[str, Any]] = {}
    for slot in slot_list:
        if slot["kind"] not in (SMALL, COMBO):
            continue
        for item in slot.get("pairs") or slot["fields"]:
            if not _is_swap(item):
                continue
            key = str(item.get("unit_key") or SWAP_FIELD)
            entry = out.setdefault(key, {"item": item, "spots": []})
            entry["spots"].append((slot, _spot_key(item)))
    return out


def swaps_done(records: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """系列做过和提过的换主体：{单元键: [{to, status}]}；没有单元的旧方向记在 D09 下，对每个单元都要避开。"""
    out: dict[str, list[dict[str, str]]] = {}
    for record in records:
        briefs = _brief_to(record)
        for cf in record["changed_fields"]:
            if not isinstance(cf, dict):
                continue
            if fields.canonical(cf.get("field_id")) != SWAP_FIELD:
                continue
            to = briefs.get(_change_key(cf)) or cf.get("to")
            if not plain(to):
                continue
            out.setdefault(str(cf.get("unit_key") or SWAP_FIELD), []).append(
                {"to": clip(to, HISTORY_TO_CHARS), "status": record["state_text"]}
            )
    return out


def _past_swaps(
    key: str, done: dict[str, list[dict[str, str]]]
) -> list[dict[str, str]]:
    past = list(done.get(key, []))
    if key != SWAP_FIELD:
        past += done.get(SWAP_FIELD, [])
    return past[-HISTORY_PER_FIELD * 2 :]


def _swap_unit_input(
    key: str, entry: dict[str, Any], done: dict[str, list[dict[str, str]]]
) -> dict[str, Any]:
    item = entry["item"]
    return {
        "unit_id": str(item.get("unit_id") or SWAP_FIELD),
        "name": item.get("unit_name") or "画面里的全部主体",
        "kind": item.get("unit_kind") or "character",
        "members": item.get("members") or [],
        "current": clip(item.get("current"), CURRENT_CHARS),
        "need": len(entry["spots"]) + SWAP_SPARE,
        "done": _past_swaps(key, done),
    }


def _validate_p14(unit_ids: list[str]) -> Callable[[dict[str, Any]], list[str]]:
    def check(result: dict[str, Any]) -> list[str]:
        got = {
            str(u.get("unit_id"))
            for u in result.get("units") or []
            if isinstance(u, dict) and u.get("candidates")
        }
        missing = [u for u in unit_ids if u not in got]
        if missing:
            return [
                f"$.result.units 缺单元 {missing}：每个要换的单元都要给一份不为空的清单"
            ]
        return []

    return check


def _taken(name: str, avoid: set[str]) -> bool:
    """新主体和要避开的重复：规范化后相同，或被做过的换法整个包含（做过「三只北极熊」就不再给「北极熊」）。"""
    return any(name == a or (len(name) >= 2 and name in a) for a in avoid if a)


def assign_swaps(
    ctx: Ctx,
    fam: dict[str, Any],
    batch_id: str,
    slot_list: list[dict[str, Any]],
    records: list[dict[str, Any]],
    profile: dict[str, Any],
    hint: str | None,
) -> dict[str, Any]:
    """调一次 P14，给每个要换主体的单元一份互不相同、没做过的新主体，按槽位顺序分到每个换主体的处
    （写进槽位的 swaps：{处的键: {swap_to}}）；清单不够的处写 swap_avoid（本批已分出去的和做过的），由 P05 自己换。

    返回 {roster: {单元键: [名字]}, assigned, short, error}；P14 失败只记 error，P05 照常写（同批去重兜底）。"""
    spots = swap_spots(slot_list)
    if not spots:
        return {}
    done = swaps_done(records)
    units = [_swap_unit_input(key, entry, done) for key, entry in spots.items()]
    spec = fam["spec"]
    candidates: dict[str, list[dict[str, Any]]] = {}
    error = None
    try:
        out = llm.run_stage(
            ctx,
            SWAP_PROMPT,
            scope_id=f"plan:{batch_id}:swap",
            job_id=f"batch:{batch_id}",
            variables={
                "swap_units": units,
                "reference_profile": profile,
                "family_rules": {
                    k: spec.get(k)
                    for k in ("positioning", "preserve", "exclude")
                    if spec.get(k)
                },
                "user_direction_request": hint or NO_REQUEST,
            },
            extra_validate=_validate_p14([u["unit_id"] for u in units]),
        )
        for unit in out.result.get("units") or []:
            if isinstance(unit, dict):
                candidates[str(unit.get("unit_id"))] = [
                    c for c in unit.get("candidates") or [] if isinstance(c, dict)
                ]
    except (StageError, llm.LLMError, ConfigError) as exc:
        error = f"{type(exc).__name__}：{exc}"[:300]
    roster: dict[str, list[str]] = {}
    assigned = short = 0
    for (key, entry), unit in zip(spots.items(), units):
        avoid = {normalize_phrase(p["to"]) for p in unit["done"]}
        picked: list[dict[str, Any]] = []
        for cand in candidates.get(unit["unit_id"], []):
            name = clip(plain(cand.get("name")), SWAP_NAME_CHARS)
            norm = normalize_phrase(name)
            if not norm or _taken(norm, avoid):
                continue
            avoid.add(norm)
            picked.append(
                {
                    "name": name,
                    "en": clip(plain(cand.get("en")), 120),
                    "features": clip(plain(cand.get("features")), 120),
                }
            )
        roster[key] = [p["name"] for p in picked]
        avoid_names = roster[key] + [p["to"] for p in unit["done"]]
        for i, (slot, spot) in enumerate(entry["spots"]):
            swaps = slot.setdefault("swaps", {})
            if i < len(picked):
                swaps[spot] = {"swap_to": picked[i]}
                assigned += 1
            else:
                swaps[spot] = {"swap_avoid": avoid_names[: HISTORY_PER_FIELD * 2]}
                short += 1
    return {"roster": roster, "assigned": assigned, "short": short, "error": error}


def _swap_key(task: dict[str, Any], slot: dict[str, Any]) -> list[tuple[str, str]]:
    """一个方向里换主体换成了谁：[(单元键, 规范化的新主体)]；照清单换的按清单名字算，没照的按简述算。"""
    briefs = _brief_to(task)
    swaps = slot.get("swaps") or {}
    out = []
    for cf in task.get("changed_fields") or []:
        if not isinstance(cf, dict) or cf.get("field_id") != SWAP_FIELD:
            continue
        spot = f"{cf['unit_id']}:{SWAP_FIELD}" if cf.get("unit_id") else SWAP_FIELD
        to = normalize_phrase(briefs.get(_change_key(cf)) or cf.get("to"))
        target = (swaps.get(spot) or {}).get("swap_to") or {}
        name = normalize_phrase(target.get("name"))
        if name and name in normalize_phrase(cf.get("to")) + to:
            to = name
        out.append((str(cf.get("unit_key") or SWAP_FIELD), to))
    return out


def _off_roster(task: dict[str, Any], slot: dict[str, Any]) -> list[str]:
    """没照清单换的处：清单分给它的名字在改成的样子里找不到。"""
    swaps = slot.get("swaps") or {}
    out = []
    for cf in task.get("changed_fields") or []:
        if not isinstance(cf, dict) or cf.get("field_id") != SWAP_FIELD:
            continue
        spot = f"{cf['unit_id']}:{SWAP_FIELD}" if cf.get("unit_id") else SWAP_FIELD
        target = (swaps.get(spot) or {}).get("swap_to") or {}
        name = normalize_phrase(target.get("name"))
        text = normalize_phrase(cf.get("to")) + normalize_phrase(
            _brief_to(task).get(_change_key(cf))
        )
        if name and name not in text:
            out.append(target.get("name"))
    return out


# ---------------------------------------------------------------- 换画法清单（绘制技法的画法大类）

# 每个换画法的槽位另给几种没分出去的画法当备选：分到的和原图同类、或明显不配这张图的题材时从里面挑
TECHNIQUE_SPARE = 6


def _family_keys(name: Any) -> list[str]:
    """画法大类的关键词：括号前的部分按「/」拆开再规范化（「丝网印刷 / 孔版（Risograph）」→ 丝网印刷、孔版）。"""
    head = re.split(r"[（(]", plain(name))[0]
    return [k for k in (normalize_phrase(p) for p in head.split("/")) if k]


def families_in(text: Any, families: list[str]) -> list[str]:
    """文字里提到的画法大类（按关键词认，families 顺序）。"""
    norm = normalize_phrase(plain(text))
    if not norm:
        return []
    return [f for f in families if any(k in norm for k in _family_keys(f))]


def techniques_done(records: list[dict[str, Any]]) -> Counter[str]:
    """系列做过和提过的换画法：{画法大类: 次数}。单字段大改 / 二次大变化里的绘制技法：记了 technique 的按它，
    以前的按简述认、认不出再按改成的样子认（三处组合里是同一画法大类内的调整，不算）。"""
    families = fields.technique_families()
    done: Counter[str] = Counter()
    for record in records:
        if record.get("kind") == COMBO or record.get("task_type") == SMALL:
            continue
        if record.get("technique") in families:
            done[record["technique"]] += 1
            continue
        briefs = _brief_to(record)
        for cf in record["changed_fields"]:
            if not isinstance(cf, dict) or not _is_technique(cf):
                continue
            hits = families_in(briefs.get(_change_key(cf)), families) or families_in(
                cf.get("to"), families
            )
            done.update(set(hits))
    return done


def _reference_families(
    profile: dict[str, Any] | None, families: list[str]
) -> set[str]:
    """原图同类的画法大类：画面档案 style 里写到的画法，不分给换画法的槽位。"""
    style = (profile or {}).get("style")
    if isinstance(style, dict):
        text = " ".join(plain(v) for v in style.values())
    else:
        text = plain(style)
    return set(families_in(text, families))


def assign_techniques(
    slot_list: list[dict[str, Any]],
    records: list[dict[str, Any]],
    profile: dict[str, Any] | None,
    seed: str,
) -> dict[str, Any]:
    """给换画法的槽位（单字段大改 / 二次大变化里只改绘制技法的）各分一种互不相同的画法大类，写进槽位的
    technique：{technique_to, technique_spare}。原图同类的不分；没做过的先分（按种子打乱），做过的按次数少的
    排在后面；槽位比画法多时多出来的不分（P05 自己挑、同批去重兜底）。

    返回 {assigned: {槽位: 画法}, spare, reference}；没有这类槽位时返回 {}。"""
    targets = [
        s
        for s in slot_list
        if s["kind"] in (BIG, DERIVED)
        and len(s["fields"]) == 1
        and _is_technique(s["fields"][0])
    ]
    families = fields.technique_families()
    if not targets or not families:
        return {}
    reference = _reference_families(profile, families)
    done = techniques_done(records)
    fresh = [f for f in families if f not in reference and not done[f]]
    random.Random(seed).shuffle(fresh)
    tried = sorted(
        (f for f in families if f not in reference and done[f]), key=lambda f: done[f]
    )
    order = fresh + tried
    assigned = {s["slot_id"]: f for s, f in zip(targets, order)}
    taken = set(assigned.values())
    spare = [f for f in order if f not in taken][:TECHNIQUE_SPARE]
    for slot in targets:
        info: dict[str, Any] = {"technique_spare": spare}
        if slot["slot_id"] in assigned:
            info = {"technique_to": assigned[slot["slot_id"]], **info}
        slot["technique"] = info
    return {"assigned": assigned, "spare": spare, "reference": sorted(reference)}


def chosen_technique(task: dict[str, Any], slot: dict[str, Any]) -> str | None:
    """这个方向换成了哪种画法大类：提到了清单分给它的就算它，否则按简述、再按改成的样子认；认不出为 None。"""
    families = fields.technique_families()
    target = (slot.get("technique") or {}).get("technique_to")
    briefs = _brief_to(task)
    for cf in task.get("changed_fields") or []:
        if not isinstance(cf, dict) or not _is_technique(cf):
            continue
        texts = [briefs.get(_change_key(cf)), cf.get("to")]
        if target and any(families_in(t, [target]) for t in texts):
            return target
        for text in texts:
            hits = families_in(text, families)
            if hits:
                return hits[0]
    return None


def technique_guard() -> tuple[
    Callable[[dict[str, Any], dict[str, Any]], str | None], list[dict[str, Any]]
]:
    """同一批换画法去重：返回 (check, off_roster)。check(task, slot) 只看单字段大改与二次大变化，认出画法后记进
    task.technique；和本批已收下的同一种时返回 duplicate_technique（后出来的丢掉）；没照清单换的记进 off_roster。"""
    seen: set[str] = set()
    off_roster: list[dict[str, Any]] = []

    def check(task: dict[str, Any], slot: dict[str, Any]) -> str | None:
        if slot["kind"] not in (BIG, DERIVED):
            return None
        name = chosen_technique(task, slot)
        if not name:
            return None
        if name in seen:
            return "duplicate_technique"
        seen.add(name)
        task["technique"] = name
        target = (slot.get("technique") or {}).get("technique_to")
        if target and name != target:
            off_roster.append(
                {"slot_id": slot["slot_id"], "technique_to": target, "chosen": name}
            )
        return None

    return check, off_roster


def _technique_record(
    info: dict[str, Any], off_roster: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """批次里记下的换画法清单：分给了谁、备选、原图同类的、没照清单换的；这一批没有换画法的槽位时为 None。"""
    if not info:
        return None
    return {**info, "off_roster": off_roster}


# ---------------------------------------------------------------- 策划的公共步骤（以原图为底 / 二次大变化）


def _run_groups(
    ctx: Ctx,
    batch_id: str,
    groups: list[dict[str, Any]],
    label: str,
    variables_for: Callable[[dict[str, Any]], dict[str, Any]],
    failure_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """按块并发调 P05（plan.concurrency）：一块失败只丢这一块；全部失败时批次记 failed 并抛 StageError。"""
    bg.step(ctx, f"{label}：0/{len(groups)} 组")

    def run(group: dict[str, Any]):
        return llm.run_stage(
            ctx,
            "P05",
            scope_id=f"plan:{batch_id}:{group['key']}",
            job_id=f"batch:{batch_id}",
            variables=variables_for(group),
        )

    outputs: dict[str, Any] = {}
    failures: dict[str, str] = {}
    errors: dict[str, BaseException] = {}
    workers = max(1, min(int(ctx.setting("plan.concurrency", 4) or 1), len(groups)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, g): g for g in groups}
        for done, future in enumerate(as_completed(futures), 1):
            group = futures[future]
            try:
                outputs[group["key"]] = future.result()
            except Exception as exc:  # noqa: BLE001 — 一组失败只丢这一组
                failures[group["key"]] = f"{type(exc).__name__}：{exc}"[:300]
                errors[group["key"]] = exc
            bg.step(ctx, f"{label}：{done}/{len(groups)} 组")
    # 连不上库、上游一时出错丢掉的组，并发跑完后一组一组再试一次（输出校验没过的 run_stage 里已经重答过，不再试）
    retry = [g for g in groups if g["key"] in errors and not isinstance(errors[g["key"]], llm.StageOutputInvalid)]
    recovered: list[str] = []
    for i, group in enumerate(retry, 1):
        bg.step(ctx, f"{label}：重试失败的组 {i}/{len(retry)}")
        try:
            outputs[group["key"]] = run(group)
        except Exception as exc:  # noqa: BLE001 — 再失败就丢这一组
            failures[group["key"]] = f"{type(exc).__name__}：{exc}"[:300]
            continue
        failures.pop(group["key"], None)
        recovered.append(group["key"])
    if retry:
        with ctx.db.tx() as conn:
            log_event(
                conn,
                batch_id,
                "plan_groups_retried",
                {"retried": [g["key"] for g in retry], "recovered": recovered},
            )
    if not outputs:
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE batches SET status = 'failed', plan_json = ?, updated_at = ? WHERE batch_id = ?",
                (
                    dumps({**failure_record, "failed_groups": failures}),
                    now_iso(),
                    batch_id,
                ),
            )
        raise StageError(
            "裂变策划失败："
            + "；".join(f"{k} {v}" for k, v in list(failures.items())[:3])
        )
    return outputs, failures


def _collect_tasks(
    family_id: str,
    groups: list[dict[str, Any]],
    outputs: dict[str, Any],
    failures: dict[str, str],
    records: list[dict[str, Any]],
    slot_list: list[dict[str, Any]],
    *,
    prepare: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    finish: Callable[[dict[str, Any], dict[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
    """按槽位核对 P05 的方向：丢掉槽位不对、改得不全、和做过的方向重复的，返回收下的（按槽位顺序）与丢弃 / 缺口。

    prepare(task, slot) 在算计划指纹之前补字段（二次大变化的 base）；finish(task, slot) 在收下之前再看一次，
    返回丢弃原因（同批换成了同一个主体）或 None。"""
    existing_hashes = {r["plan_hash"] for r in records}
    existing_signatures = {direction_signature(r) for r in records} - {frozenset()}
    # 没有单元的旧方向：新方向去掉单元后和它一样也算重复（换个单元说法不算新方向）
    legacy_signatures = {
        s for s in existing_signatures if all(len(item) == 2 for item in s)
    }
    accepted: list[tuple[str, dict[str, Any]]] = []
    dropped: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    failed_groups: list[dict[str, str]] = []
    seen_hashes: set[str] = set()
    seen_signatures: set[frozenset] = set()
    model = prompt_version = None
    call_ids: list[str] = []
    for group in groups:
        out = outputs.get(group["key"])
        if out is None:
            failed_groups.append(
                {"group": _group_name(group), "error": failures.get(group["key"], "")}
            )
            gaps.extend(
                {"slot_id": s["slot_id"], "kind": s["kind"], "reason": "这一组策划失败"}
                for s in group["slots"]
            )
            continue
        model, prompt_version = out.model, out.prompt_version
        call_ids += out.call_ids
        result = out.result
        skipped = {
            str(s.get("slot_id")): s.get("reason") or ""
            for s in result.get("skipped") or []
            if isinstance(s, dict)
        }
        by_slot = {s["slot_id"]: s for s in group["slots"]}
        filled: set[str] = set()
        for task in result.get("tasks", []):
            sid = str(task.get("slot_id") or "").strip()
            slot = by_slot.get(sid)
            if slot is None or sid in filled:
                dropped.append(
                    {
                        "reason": "duplicate_slot" if slot else "unknown_slot",
                        "slot_id": sid,
                        "title": task.get("title"),
                    }
                )
                continue
            reason = _fit_task(task, slot)
            if reason:
                dropped.append(
                    {
                        "reason": reason,
                        "slot_id": sid,
                        "changed_fields": task.get("changed_fields"),
                    }
                )
                continue
            if prepare:
                prepare(task, slot)
            h = plan_hash(family_id, task)
            if h in existing_hashes or h in seen_hashes:
                dropped.append(
                    {
                        "reason": "duplicate_plan",
                        "slot_id": sid,
                        "plan_hash": h,
                        "changed_fields": task.get("changed_fields"),
                    }
                )
                continue
            fids = [cf.get("field_id") for cf in task["changed_fields"]]
            task["slot_id"] = sid
            task["kind"] = slot["kind"]
            task["task_type"] = SMALL if slot["kind"] == SMALL else BIG
            task["axis"] = axis_text(
                fids,
                [
                    cf.get("unit_name") if _on_unit(cf) else None
                    for cf in task["changed_fields"]
                ],
            )
            task.update(reconcile_brief(task))
            signature = direction_signature(task)
            if signature and (
                signature in existing_signatures
                or signature in seen_signatures
                or unitless_signature(signature) in legacy_signatures
            ):
                dropped.append(
                    {
                        "reason": "duplicate_direction",
                        "slot_id": sid,
                        "title": task["title"],
                        "changed_fields": task.get("changed_fields"),
                    }
                )
                continue
            extra = finish(task, slot) if finish else None
            if extra:
                dropped.append(
                    {
                        "reason": extra,
                        "slot_id": sid,
                        "title": task["title"],
                        "changed_fields": task.get("changed_fields"),
                    }
                )
                continue
            seen_hashes.add(h)
            seen_signatures.add(signature)
            filled.add(sid)
            task["selection"] = {"status": "pending"}
            accepted.append((h, task))
        for slot in group["slots"]:
            if slot["slot_id"] not in filled:
                gaps.append(
                    {
                        "slot_id": slot["slot_id"],
                        "kind": slot["kind"],
                        "label": " + ".join(slot_changes(slot)),
                        "reason": skipped.get(slot["slot_id"])
                        or "没出方向，或出的方向被丢掉了",
                    }
                )
    # 挑选卡按 小变化 → 单字段大改 → 三处组合、槽位顺序排（并发返回的先后不影响编号）
    slot_order = {(s["kind"], s["slot_id"]): i for i, s in enumerate(slot_list)}
    accepted.sort(
        key=lambda item: slot_order.get((item[1]["kind"], item[1]["slot_id"]), 999)
    )
    return {
        "accepted": accepted,
        "dropped": dropped,
        "gaps": gaps,
        "failed_groups": failed_groups,
        "model": model,
        "prompt_version": prompt_version,
        "call_ids": call_ids,
    }


def _insert_variants(
    conn,
    family_id: str,
    family_version: int,
    batch_id: str,
    accepted: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """把收下的方向写进 variants（候选，不建任务）；二次大变化记父方向与第 2 代。返回给卡片与命令输出的摘要。"""
    created = []
    for h, task in accepted:
        variant_id = next_id(conn, "variant")
        task["variant_id"] = variant_id
        task.pop("parent_variant_id", None)
        parent = base_variant(task)
        conn.execute(
            "INSERT INTO variants(variant_id, family_id, family_version, batch_id, plan_hash, parent_variant_id,"
            " generation_depth, axis, plan_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                variant_id,
                family_id,
                family_version,
                batch_id,
                h,
                parent,
                2 if parent else 1,
                task.get("axis"),
                dumps(task),
                now_iso(),
            ),
        )
        item = {
            "index": len(created) + 1,
            "variant_id": variant_id,
            "slot_id": task["slot_id"],
            "title": task["title"],
            "kind": task["kind"],
            "task_type": task["task_type"],
            "aspects": task["aspects"],
            "brief": task["brief"],
            "keep": task["keep"],
            "axis": task.get("axis"),
            "changed_fields": task.get("changed_fields"),
        }
        if parent:
            item["base"] = task["base"]
        created.append(item)
    return created


def preview_slots(ctx: Ctx, family_id: str) -> dict[str, Any]:
    """只看这一轮会挑出哪些改动（不调模型、不建批次）：种子「系列:preview」，历史与正式策划同口径。"""
    fam = planning_family(ctx, family_id)
    inputs = unit_pools(ctx, fam)
    records, _, _ = _history(ctx, family_id)
    used, done = _field_usage(records)
    slots = plan_slots(
        ctx,
        fam,
        rng=random.Random(f"{family_id}:preview"),
        used=used,
        done=done,
        units=inputs,
        history=subjects.history(records) if inputs["pairs"] is not None else None,
    )
    techniques = assign_techniques(
        slots["slots"],
        records,
        reference_profile(
            ctx, fam["spec"].get("analysis_id") or fam.get("analysis_id")
        ),
        f"{family_id}:preview:technique",
    )
    return {
        "family_id": family_id,
        "family_version": fam["family_version"],
        "plan_unit": slots["mode"],
        "unit_reason": slots["reason"],
        "units": profile_unit_lines(slots["units"] or []),
        "pairs": {k: len(v) for k, v in (inputs["pairs"] or {}).items()},
        # 大变化里可以连带调整布局的字段（以前按构图不动时改不了判的）：三处组合全收，单字段大改只收动态字段
        "layout_fields": [
            f["label"] for f in inputs["pools"][COMBO] if f.get("layout_change")
        ],
        "dynamic_fields": [
            f["label"] for f in inputs["pools"][BIG] if fields.is_dynamic(f["field_id"])
        ],
        "counts": slots["counts"],
        "counts_text": counts_text(slots["counts"]),
        # 大变化的配额（plan.big_single_quota / plan.combo_quota）：动态 / 换画法 / 其余各几个
        "big_quota": quota_text(slots["counts"].get("big_quota"), QUOTA_LABELS),
        "combo_quota": quota_text(
            slots["counts"].get("combo_quota"), COMBO_QUOTA_LABELS
        ),
        # 换画法清单（正式策划时同样按系列历史与原图画风分）：槽位 → 画法大类
        "techniques": techniques.get("assigned") or {},
        # 小变化分组（plan.small_swap_ratio）：换主体组 / 其余组各几个，没有能换的主体时写原因
        "swap": _swap_record(slots),
        # 要换主体的单元各要换几次（正式策划时 P14 按这个数多要几个）
        "swap_units": {
            entry["item"].get("unit_name") or "全部主体": len(entry["spots"])
            for entry in swap_spots(slots["slots"]).values()
        },
        # 单字段大改里一轮出了不止一次的字段（plan.big_single_repeat）
        "repeated_big_fields": sorted(
            fields.label(fid)
            for fid, n in Counter(
                s["field_ids"][0] for s in slots["slots"] if s["kind"] == BIG
            ).items()
            if n > 1
        ),
        "slots": [
            {
                "slot_id": s["slot_id"],
                "kind": KIND_LABELS[s["kind"]],
                "changes": slot_changes(s),
                **({"swap": True} if s.get("swap") else {}),
                **({"quota": s["quota"]} if s.get("quota") else {}),
            }
            for s in slots["slots"]
        ],
    }


def _swap_record(slots: dict[str, Any]) -> dict[str, Any] | None:
    """批次 / 预览里记下的小变化分组：不分组时为 None。"""
    split = slots.get("swap") or {}
    if not split.get("mode"):
        return None
    return {k: split.get(k) for k in ("ratio", "swap", "other", "reason")}


def plan_batch(
    ctx: Ctx,
    family_id: str,
    *,
    limit: int | None = None,
    step: str | None = None,
    hint: str | None = None,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    fam = planning_family(ctx, family_id)
    spec = fam["spec"]
    inputs = unit_pools(ctx, fam)
    if limit is not None and limit < 1:
        raise StageError("limit 至少是 1")
    wanted = set(kinds or [])
    if wanted - set(PLAN_KINDS):
        raise StageError(
            f"kinds 只能取 {list(PLAN_KINDS)}，不认识 {sorted(wanted - set(PLAN_KINDS))}"
        )
    counts_planned = pool_counts(ctx, inputs["pools"], inputs["pairs"])
    if not any(counts_planned[k] for k in wanted or PLAN_KINDS):
        raise StageError(
            f"{family_id} 没有可以裂变的字段：拆解判定都不能变，或系列把它们都锁定了；"
            "在 POD 对话里修订系列"
        )
    hint = clean_hint(hint)
    waiting = [
        b["batch_id"]
        for b in proposed_batches(ctx, family_id)
        if b["family_version"] == fam["family_version"]
    ]
    if waiting:
        raise StageError(
            f"{family_id} 还有一批方向待挑选（{waiting[0]}）：先挑完，或点「都不满意，换一批」"
        )
    with ctx.db.tx() as conn:
        superseded = _retire_stale_batches(conn, family_id, fam["family_version"])
        batch_id = next_id(conn, "batch")
        conn.execute(
            "INSERT INTO batches(batch_id, family_id, family_version, status, size, created_at, updated_at)"
            " VALUES(?, ?, ?, 'planning', 0, ?, ?)",
            (batch_id, family_id, fam["family_version"], now_iso(), now_iso()),
        )
    records, _, _ = _history(ctx, family_id)
    by_field = field_history(records)
    multi = multi_history(records)
    used, done = _field_usage(records)
    slots = plan_slots(
        ctx,
        fam,
        rng=random.Random(f"{family_id}:{batch_id}"),
        used=used,
        done=done,
        units=inputs,
        history=subjects.history(records) if inputs["pairs"] is not None else None,
    )
    groups = _groups(
        slots,
        limit,
        wanted or None,
        per_call=_count_setting(ctx, "plan.slots_per_call", 4) or 1,
    )
    prompt_spec = _spec_for_prompt(spec)
    profile = reference_profile(ctx, spec.get("analysis_id") or fam.get("analysis_id"))
    label = step or "策划裂变方向"
    planned = [s for g in groups for s in g["slots"]]
    swap_info: dict[str, Any] = {}
    if slots["swap"]["mode"]:
        bg.step(ctx, f"{label}：换主体清单")
        swap_info = assign_swaps(
            ctx,
            fam,
            batch_id,
            planned,
            records,
            profile,
            hint,
        )
    technique_info = assign_techniques(
        planned, records, profile, f"{family_id}:{batch_id}:technique"
    )

    def variables_for(group: dict[str, Any]) -> dict[str, Any]:
        plan_group, index = _group_input(group, by_field, multi)
        return {
            "approved_family_spec": prompt_spec,
            "reference_profile": profile,
            "plan_group": plan_group,
            "completed_plan_index": index,
            "rejected_patterns": {
                "family_exclude": spec.get("exclude", []),
                "note": "completed_plan_index 里状态是「没选」的方向是人挑选时没选的，不要换个说法再提",
            },
            "user_direction_request": hint or NO_REQUEST,
        }

    outputs, failures = _run_groups(
        ctx, batch_id, groups, label, variables_for, {"hint": hint}
    )
    seen_swaps: set[tuple[str, str]] = set()
    off_roster: list[dict[str, Any]] = []
    check_technique, off_technique = technique_guard()

    def finish(task: dict[str, Any], slot: dict[str, Any]) -> str | None:
        if slots["swap"]["mode"]:
            # 同一批里同一个单元换成了同一个主体：后出来的丢掉（照清单换的不会撞，没照清单的才会）
            keys = [k for k in _swap_key(task, slot) if k[1]]
            if any(k in seen_swaps for k in keys):
                return "duplicate_swap"
            seen_swaps.update(keys)
            missed = _off_roster(task, slot)
            if missed:
                off_roster.append({"slot_id": slot["slot_id"], "swap_to": missed})
        # 同一批里换成了同一种画法大类：后出来的丢掉
        return check_technique(task, slot)

    collected = _collect_tasks(
        family_id,
        groups,
        outputs,
        failures,
        records,
        slots["slots"],
        finish=finish,
    )
    accepted = collected["accepted"]
    dropped, gaps = collected["dropped"], collected["gaps"]
    failed_groups = collected["failed_groups"]
    model, prompt_version = collected["model"], collected["prompt_version"]
    call_ids = collected["call_ids"]
    swap_record = _swap_record(slots)
    if swap_record is not None:
        swap_record.update(
            {k: swap_info.get(k) for k in ("roster", "assigned", "short", "error")}
        )
        swap_record["off_roster"] = off_roster
    technique_record = _technique_record(technique_info, off_technique)
    pending_family = fam["status"] == "pending"
    # 待认可的系列耗尽时不改状态：它还在等人看，交给人修订或换母版；
    # 带了要求、或只策划部分类型的这一轮没出方向，只说明这几类做不出来，不算整个系列耗尽
    exhausted = not accepted and not pending_family and not hint and not wanted
    counts = Counter(task["kind"] for _, task in accepted)
    review_id = None
    with ctx.db.tx() as conn:
        created = _insert_variants(
            conn, family_id, fam["family_version"], batch_id, accepted
        )
        conn.execute(
            "UPDATE batches SET status = ?, size = ?, plan_json = ?, updated_at = ? WHERE batch_id = ?",
            (
                "proposed" if accepted else "empty",
                len(accepted),
                dumps(
                    {
                        "hint": hint,
                        "kinds": sorted(wanted) or None,
                        "slots": slots["counts"],
                        # 这一批随机挑中的字段组合（同一批号能复现）
                        "slot_fields": {
                            s["slot_id"]: s["field_ids"] for s in slots["slots"]
                        },
                        **_unit_record(slots),
                        # 按换主体规则策划的批次（小变化分组开着）：小变化出完后可以自动接二次大变化
                        **(
                            {"small_swap": True, "swap": swap_record}
                            if swap_record is not None
                            else {}
                        ),
                        # 换画法清单：每个换画法的槽位分到的画法大类
                        **(
                            {"technique": technique_record}
                            if technique_record is not None
                            else {}
                        ),
                        "counts": dict(counts),
                        "coverage_gaps": gaps,
                        "failed_groups": failed_groups,
                        "dropped": dropped,
                        "picked": [],
                        "model": model,
                        "prompt_version": prompt_version,
                        "call_ids": call_ids,
                    }
                ),
                now_iso(),
                batch_id,
            ),
        )
        if accepted:
            review_id = open_review(
                conn,
                "plan_selection",
                batch_id,
                f"{family_id} 的 {len(accepted)} 个裂变方向待挑选：选中的才出图",
                {
                    "family_id": family_id,
                    "family_version": fam["family_version"],
                    "seed_id": fam["seed_id"],
                    "candidates": len(accepted),
                    "counts": dict(counts),
                },
                batch_id=batch_id,
            )
            if pending_family:
                # 只审一次：系列规则随方向一起认可，不再单独等「批准系列」
                conn.execute(
                    "UPDATE reviews SET status = 'dismissed', note = '并入裂变方向挑选：确认方向时一并认可'"
                    " WHERE subject_type = 'family' AND subject_id = ? AND status = 'open'",
                    (f"{family_id}@v{fam['family_version']}",),
                )
        if exhausted:
            reason = "本轮每个槽位都没出不重复、又成立的新方向"
            conn.execute(
                "UPDATE families SET status = 'exhausted', exhaustion_reason = ?, updated_at = ?"
                " WHERE family_id = ? AND family_version = ?",
                (reason, now_iso(), family_id, fam["family_version"]),
            )
            open_review(
                conn,
                "family_exhausted",
                family_id,
                f"{reason}：需要人工批准新的探索方向或扩大系列范围（不靠换随机种子凑数）",
                {"coverage_gaps": gaps[:20], "dropped": dropped[:10]},
            )
        log_event(
            conn,
            batch_id,
            "batch_planned",
            {
                "family_id": family_id,
                "accepted": len(accepted),
                "counts": dict(counts),
                "dropped": len(dropped),
                "failed_groups": len(failed_groups),
                "candidates": True,
                "hint": hint,
                "plan_unit": slots["mode"],
            },
        )
    return {
        "batch_id": batch_id,
        "family_id": family_id,
        "family_version": fam["family_version"],
        "family_status": fam["status"],
        "hint": hint,
        "status": "proposed" if accepted else "empty",
        "review_id": review_id,
        "accepted": created,
        "counts": dict(counts),
        "slots": slots["counts"],
        "plan_unit": slots["mode"],
        "unit_reason": slots["reason"],
        "swap": swap_record,
        "technique": technique_record,
        "dropped": dropped,
        "coverage_gaps": gaps,
        "failed_groups": failed_groups,
        "exhausted": exhausted,
        "superseded_batches": superseded,
    }


# ---------------------------------------------------------------- 二次大变化

# 能当底图的图：出好了的（质检通过 / 预览就绪 / 已入库），被人判了「不要」的不用
BASE_STATUSES = ("qc_pass", "preview_ready", "archived")


def derived_settings(ctx: Ctx) -> dict[str, Any]:
    """plan.derived：{enabled（缺省 false）, count（每个原批次最多几个，缺省 8）, per_parent（每个小变化最多当几次底图，缺省 1）}。"""
    return {
        "enabled": bool(ctx.setting("plan.derived.enabled", False)),
        "count": _count_setting(ctx, "plan.derived.count", 8),
        "per_parent": max(1, _count_setting(ctx, "plan.derived.per_parent", 1)),
    }


def base_for_prompt(base: dict[str, Any]) -> dict[str, Any]:
    """给 P05 / P06 的底图：哪个小变化、标题、它改了什么（换主体在前，每处原来 → 改成写全）。"""
    return {
        "variant_id": base.get("variant_id"),
        "asset_id": base.get("asset_id"),
        "title": base.get("title"),
        "changes": base.get("changes") or [],
    }


def _base_changes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """父方向（小变化）的改动：换主体那处在前；每处 {field_id, aspect, from, to[, unit_name, unit_en]}，to 写全。"""
    out = []
    for cf in plan.get("changed_fields") or []:
        if not isinstance(cf, dict):
            continue
        item = {
            "field_id": fields.canonical(cf.get("field_id")) or cf.get("field_id"),
            "aspect": change_aspect(cf),
            "from": clip(plain(cf.get("from")), CURRENT_CHARS),
            "to": clip(plain(cf.get("to")), CURRENT_CHARS),
        }
        for key in ("unit_name", "unit_en"):
            if cf.get(key):
                item[key] = cf[key]
        out.append(item)
    out.sort(key=lambda c: c["field_id"] != SWAP_FIELD)
    return out


def _base_model_rank(ctx: Ctx) -> dict[str, int]:
    return {m: i for i, m in enumerate(fanout_models(ctx))}


def choose_base_asset(ctx: Ctx, variant_id: str) -> dict[str, Any] | None:
    """父方向出的图里挑一张当底图：出好了、没被判「不要」；按 fanout 顺序（只开一个模型时就是它），同一个模型取最新的。"""
    rank = _base_model_rank(ctx)
    rows = [
        dict(r)
        for r in ctx.db.q(
            "SELECT asset_id, model, status, human_decision, path, created_at FROM assets"
            " WHERE variant_id = ? ORDER BY created_at DESC, asset_id DESC",
            (variant_id,),
        )
        if r["status"] in BASE_STATUSES and r["human_decision"] != "rejected"
    ]
    rows.sort(
        key=lambda r: rank.get(str(r["model"]).removesuffix(EDIT_SUFFIX), len(rank))
    )
    return rows[0] if rows else None


def _swap_target(plan: dict[str, Any]) -> str:
    """小变化里换主体换成了什么（规范化），挑父方向时按它轮流挑，不同主体优先。"""
    briefs = _brief_to(plan)
    for cf in plan.get("changed_fields") or []:
        if isinstance(cf, dict) and fields.canonical(cf.get("field_id")) == SWAP_FIELD:
            return normalize_phrase(briefs.get(_change_key(cf)) or cf.get("to"))
    return ""


def derived_parents(
    ctx: Ctx,
    batch_id: str,
    *,
    parents: list[str] | None = None,
    count: int | None = None,
) -> list[dict[str, Any]]:
    """原批次里能当底图的小变化：改了换主体（D09）、任务出完了、有一张能用的图。

    给了 parents（V 编号）只在这些里挑，不能当底图的报错；最多 count 个（缺省 plan.derived.count），按换成的主体
    轮流挑（不同主体优先，组内顺序按批次号打乱、能复现），每个最多当 plan.derived.per_parent 次底图。"""
    settings = derived_settings(ctx)
    limit = settings["count"] if count is None else max(0, int(count))
    eligible: list[dict[str, Any]] = []
    reasons: dict[str, str] = {}
    for row in ctx.db.q(
        "SELECT v.variant_id, v.plan_json, v.axis, j.status AS job_status FROM variants v"
        " LEFT JOIN jobs j ON j.variant_id = v.variant_id WHERE v.batch_id = ? ORDER BY v.variant_id",
        (batch_id,),
    ):
        vid = row["variant_id"]
        plan = loads(row["plan_json"], {}) or {}
        if plan.get("kind") != SMALL:
            reasons[vid] = "不是小变化"
        elif not _swap_target(plan):
            reasons[vid] = "没有换主体"
        elif row["job_status"] != "complete":
            reasons[vid] = "还没出完"
        else:
            asset = choose_base_asset(ctx, vid)
            if asset is None:
                reasons[vid] = "没有能当底图的图"
            else:
                eligible.append(
                    {
                        "variant_id": vid,
                        "plan": plan,
                        "axis": row["axis"],
                        "asset": asset,
                    }
                )
    if parents:
        ok = {e["variant_id"] for e in eligible}
        bad = [p for p in parents if p not in ok]
        if bad:
            raise StageError(
                "这些方向不能当底图："
                + "；".join(
                    f"{p}（{reasons.get(p, f'不在 {batch_id} 里')}）" for p in bad
                )
            )
        eligible = [e for e in eligible if e["variant_id"] in parents]
    rng = random.Random(f"{batch_id}:derive")
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in eligible:
        groups.setdefault(_swap_target(e["plan"]), []).append(e)
    for members in groups.values():
        rng.shuffle(members)
    order = sorted(groups)
    rng.shuffle(order)
    queue = [
        groups[key][i]
        for i in range(max((len(g) for g in groups.values()), default=0))
        for key in order
        if i < len(groups[key])
    ]
    picks: list[dict[str, Any]] = []
    for _ in range(settings["per_parent"]):
        for e in queue:
            if len(picks) >= limit:
                break
            picks.append(e)
    return picks


def _base_info(pick: dict[str, Any]) -> dict[str, Any]:
    plan, asset = pick["plan"], pick["asset"]
    summary = direction_summary(plan, pick["axis"])
    return {
        "variant_id": pick["variant_id"],
        "asset_id": asset["asset_id"],
        "model": asset["model"],
        "title": summary["title"],
        "label": asset_label(asset["asset_id"], plan, pick["axis"]),
        "changes": _base_changes(plan),
        # 挑选卡 / 候选表「改了什么」最前面那一行：底图的主体换成了什么
        "brief": [
            {**b, "aspect": f"底图·{b.get('aspect')}", "inherited": True}
            for b in summary["brief"]
            if isinstance(b, dict) and fields.canonical(b.get("field_id")) == SWAP_FIELD
        ],
    }


def derived_slots(
    ctx: Ctx,
    fam: dict[str, Any],
    picks: list[dict[str, Any]],
    rng: random.Random,
    used: dict[str, int],
) -> list[dict[str, Any]]:
    """二次大变化的槽位 D1…：每个底图配一个大字段（主题叙事 / 整体构图 / 绘制技法 / 动态字段里可变、没锁定、
    找得到单元的）。按 plan.derived.quota 先定好动态 / 换画法各几个（这一类没有字段时由其余补），打乱后逐个挑：
    少改过的优先、同一批里分散开；同一个底图出不止一次时不重复字段。"""
    pool = unit_pools(ctx, fam)["pools"][BIG]
    if not pool:
        raise StageError(
            f"{fam['family_id']} 没有能大改的字段（主题叙事 / 整体构图 / 绘制技法 / 动态字段都不能变或被锁定）："
            "二次大变化做不了"
        )
    n = len(picks)
    groups = quota_split(
        n,
        quota_ratios(ctx, "plan.derived.quota"),
        lambda limits, name, target: n if any(_passes(f, limits) for f in pool) else 0,
    )
    plan = [g for g in groups for _ in range(g["count"])]
    if len(groups) > 1:
        rng.shuffle(plan)
    hits: Counter[str] = Counter()
    by_parent: dict[str, set[str]] = {}
    slots = []
    for i, (pick, group) in enumerate(zip(picks, plan), 1):
        taken = by_parent.setdefault(pick["variant_id"], set())
        allowed = [f for f in pool if _passes(f, group["limits"])] or pool
        options = [f for f in allowed if f["field_id"] not in taken] or allowed
        info = rng.choices(
            options,
            weights=[
                PICKED_FIELD_DECAY ** hits[f["field_id"]]
                / (1 + used.get(f["field_id"], 0))
                for f in options
            ],
        )[0]
        hits[info["field_id"]] += 1
        taken.add(info["field_id"])
        quota = group["quota"] if len(groups) > 1 else QUOTA_REST
        slots.append(
            {
                "slot_id": f"{SLOT_PREFIX[DERIVED]}{i}",
                "kind": DERIVED,
                "field_ids": [info["field_id"]],
                "fields": [info],
                "base": _base_info(pick),
                **({"quota": quota} if quota != QUOTA_REST else {}),
            }
        )
    return slots


def _derived_family(
    ctx: Ctx, source_batch_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(原批次, 它的系列版本)：原批次要存在、不是二次批次，系列版本要已批准或待认可、按新字段拆过。"""
    row = ctx.db.q1("SELECT * FROM batches WHERE batch_id = ?", (source_batch_id,))
    if not row:
        raise StageError(f"没有批次 {source_batch_id}")
    src = dict(row)
    src["plan"] = loads(src["plan_json"], {}) or {}
    if src["plan"].get("derived_from"):
        raise StageError(
            f"{source_batch_id} 本身是二次大变化批次：只做一层，不再往下派生"
        )
    fam = get_family(ctx, src["family_id"], src["family_version"])
    if fam["status"] not in ("approved", "pending"):
        raise StageError(
            f"{source_batch_id} 基于 {fam['family_id']} v{fam['family_version']}，这个版本已经「{fam['status']}」："
            "二次大变化用不了"
        )
    if not field_based(fam["spec"]):
        raise StageError(legacy_reason(fam.get("seed_id")))
    return src, fam


def preview_derived(
    ctx: Ctx,
    source_batch_id: str,
    *,
    parents: list[str] | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """只看二次大变化会挑哪些底图、各配哪个大字段（不调模型、不建批次）。"""
    src, fam = _derived_family(ctx, source_batch_id)
    records, _, _ = _history(ctx, fam["family_id"])
    used, _ = _field_usage(records)
    picks = derived_parents(ctx, source_batch_id, parents=parents, count=count)
    slot_list = (
        derived_slots(
            ctx, fam, picks, random.Random(f"{fam['family_id']}:preview-derived"), used
        )
        if picks
        else []
    )
    assign_techniques(
        slot_list,
        records,
        reference_profile(
            ctx, fam["spec"].get("analysis_id") or fam.get("analysis_id")
        ),
        f"{fam['family_id']}:preview-derived:technique",
    )
    return {
        "source_batch_id": source_batch_id,
        "family_id": fam["family_id"],
        "family_version": fam["family_version"],
        "slots": [
            {
                "slot_id": s["slot_id"],
                "field": fields.label(s["field_ids"][0]),
                "base": s["base"]["variant_id"],
                "base_title": s["base"]["title"],
                "base_asset": s["base"]["asset_id"],
                **({"quota": s["quota"]} if s.get("quota") else {}),
                **(
                    {"technique_to": s["technique"]["technique_to"]}
                    if (s.get("technique") or {}).get("technique_to")
                    else {}
                ),
            }
            for s in slot_list
        ],
    }


def plan_derived(
    ctx: Ctx,
    source_batch_id: str,
    *,
    parents: list[str] | None = None,
    count: int | None = None,
    step: str | None = None,
) -> dict[str, Any]:
    """二次大变化：以原批次里换了主体的小变化出的图为底图，各策划一个单字段大改（P05 组类型 derived）。

    方向写进新批次（plan_json.derived_from = 原批次），变体记父方向与第 2 代、plan_json.base；计划指纹与方向签名带
    底图，同一个大改落在不同底图上不算重复。和以原图为底的策划一样只出候选、开「裂变方向」待审，不标系列耗尽。"""
    src, fam = _derived_family(ctx, source_batch_id)
    family_id = fam["family_id"]
    waiting = [
        b["batch_id"]
        for b in proposed_batches(ctx, family_id)
        if b["family_version"] == fam["family_version"]
    ]
    if waiting:
        raise StageError(
            f"{family_id} 还有一批方向待挑选（{waiting[0]}）：先挑完，或点「都不满意，换一批」"
        )
    records, _, _ = _history(ctx, family_id)
    used, _ = _field_usage(records)
    picks = derived_parents(ctx, source_batch_id, parents=parents, count=count)
    if not picks:
        raise StageError(
            f"{source_batch_id} 里没有能当底图的小变化：要改了换主体、已经出完、有一张没被判「不要」的图"
        )
    with ctx.db.tx() as conn:
        batch_id = next_id(conn, "batch")
        conn.execute(
            "INSERT INTO batches(batch_id, family_id, family_version, status, size, created_at, updated_at)"
            " VALUES(?, ?, ?, 'planning', 0, ?, ?)",
            (batch_id, family_id, fam["family_version"], now_iso(), now_iso()),
        )
    slot_list = derived_slots(
        ctx, fam, picks, random.Random(f"{family_id}:{batch_id}"), used
    )
    groups = _groups(
        {"slots": slot_list},
        None,
        None,
        per_call=_count_setting(ctx, "plan.slots_per_call", 4) or 1,
    )
    spec = fam["spec"]
    prompt_spec = _spec_for_prompt(spec)
    profile = reference_profile(ctx, spec.get("analysis_id") or fam.get("analysis_id"))
    technique_info = assign_techniques(
        slot_list, records, profile, f"{family_id}:{batch_id}:technique"
    )
    by_field = field_history(records)
    record = {"derived_from": source_batch_id, "kinds": [DERIVED]}

    def variables_for(group: dict[str, Any]) -> dict[str, Any]:
        plan_group, index = _group_input(group, by_field, [])
        return {
            "approved_family_spec": prompt_spec,
            "reference_profile": profile,
            "plan_group": plan_group,
            "completed_plan_index": index,
            "rejected_patterns": {
                "family_exclude": spec.get("exclude", []),
                "note": "completed_plan_index 里状态是「没选」的方向是人挑选时没选的，不要换个说法再提",
            },
            "user_direction_request": NO_REQUEST,
        }

    outputs, failures = _run_groups(
        ctx, batch_id, groups, step or "策划二次大变化", variables_for, record
    )

    def prepare(task: dict[str, Any], slot: dict[str, Any]) -> None:
        task["base"] = {k: v for k, v in slot["base"].items() if k != "brief"}

    check_technique, off_technique = technique_guard()

    def finish(task: dict[str, Any], slot: dict[str, Any]) -> str | None:
        # 同一批里换成了同一种画法大类：后出来的丢掉
        dropped = check_technique(task, slot)
        if dropped:
            return dropped
        # 简述最前面补上底图的换主体：挑选卡、候选表「改了什么」一眼看出是在哪个新主体上做的大改
        task["brief"] = list(slot["base"]["brief"]) + list(task.get("brief") or [])
        return None

    collected = _collect_tasks(
        family_id,
        groups,
        outputs,
        failures,
        records,
        slot_list,
        prepare=prepare,
        finish=finish,
    )
    accepted = collected["accepted"]
    counts = Counter(task["kind"] for _, task in accepted)
    technique_record = _technique_record(technique_info, off_technique)
    quotas = Counter(s["quota"] for s in slot_list if s.get("quota"))
    review_id = None
    with ctx.db.tx() as conn:
        created = _insert_variants(
            conn, family_id, fam["family_version"], batch_id, accepted
        )
        conn.execute(
            "UPDATE batches SET status = ?, size = ?, plan_json = ?, updated_at = ? WHERE batch_id = ?",
            (
                "proposed" if accepted else "empty",
                len(accepted),
                dumps(
                    {
                        **record,
                        **(
                            {"technique": technique_record}
                            if technique_record is not None
                            else {}
                        ),
                        **({"derived_quota": dict(quotas)} if quotas else {}),
                        "hint": None,
                        "slots": {DERIVED: len(slot_list), "total": len(slot_list)},
                        "slot_fields": {
                            s["slot_id"]: s["field_ids"] for s in slot_list
                        },
                        "slot_bases": {
                            s["slot_id"]: s["base"]["variant_id"] for s in slot_list
                        },
                        "bases": {
                            s["base"]["variant_id"]: s["base"]["asset_id"]
                            for s in slot_list
                        },
                        "counts": dict(counts),
                        "coverage_gaps": collected["gaps"],
                        "failed_groups": collected["failed_groups"],
                        "dropped": collected["dropped"],
                        "picked": [],
                        "model": collected["model"],
                        "prompt_version": collected["prompt_version"],
                        "call_ids": collected["call_ids"],
                    }
                ),
                now_iso(),
                batch_id,
            ),
        )
        if accepted:
            review_id = open_review(
                conn,
                "plan_selection",
                batch_id,
                f"{family_id} 的 {len(accepted)} 个二次大变化方向待挑选：选中的才出图",
                {
                    "family_id": family_id,
                    "family_version": fam["family_version"],
                    "seed_id": fam["seed_id"],
                    "candidates": len(accepted),
                    "counts": dict(counts),
                    "derived_from": source_batch_id,
                },
                batch_id=batch_id,
            )
        log_event(
            conn,
            batch_id,
            "batch_planned",
            {
                "family_id": family_id,
                "accepted": len(accepted),
                "counts": dict(counts),
                "dropped": len(collected["dropped"]),
                "failed_groups": len(collected["failed_groups"]),
                "candidates": True,
                "derived_from": source_batch_id,
            },
        )
    return {
        "batch_id": batch_id,
        "family_id": family_id,
        "family_version": fam["family_version"],
        "family_status": fam["status"],
        "hint": None,
        "status": "proposed" if accepted else "empty",
        "review_id": review_id,
        "accepted": created,
        "counts": dict(counts),
        "slots": {DERIVED: len(slot_list), "total": len(slot_list)},
        "derived_from": source_batch_id,
        "parents": list(dict.fromkeys(p["variant_id"] for p in picks)),
        "technique": technique_record,
        "dropped": collected["dropped"],
        "coverage_gaps": collected["gaps"],
        "failed_groups": collected["failed_groups"],
        "exhausted": False,
        "superseded_batches": [],
    }
