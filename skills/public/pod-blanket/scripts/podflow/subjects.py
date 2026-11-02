"""可变单元与「单元 × 字段」组合：小变化改两处、三处组合改三处，一处 = 一个可变单元上的一个字段。

- 单元来自画面档案 P13 的 change_units（会一起改的一组对象：单个角色、满屏星星、一圈花边；多个角色时有组单元和
  成员单元），程序另加「整幅」（CANVAS）挂配色、技法、排列、密度、加陪衬、加定制位这类整幅字段；
- 组合（pair）= 单元 × 能挂在它上面的可变字段：字段可变且系列没锁（plan.field_pools 已筛，小变化与三处组合各有
  一个池）、类型对得上（fields.applies_to）、单元没否决（not_applicable）、依赖满足（fields.requires）；
- 档位 = 单元显著度，只动脸部的字段（fields.is_local）降一档；整幅字段按 fields.magnitude；小档组合不出；
- 一个槽位里的几处（slot_ok）：重叠的单元（同一个，或组与它的成员）上要有一方是大档单元、部位不同、不是同一字段、
  不互斥；整幅字段的互斥对整个槽位生效（整体配色和单元配色）；整幅最多一处（池里没有单元组合时不限）；
- 抽样（pick_pair_sets）：每个单元总机会相同（权重 ÷ 这个单元的组合数；同一个组下的成员单元合起来只占一份），
  少改过的组合优先，同维度 ×0.15，做过的组合 ×0.2，同一批里选中一次组合 ×0.5、这个单元所在的族（组与它的成员算一族）
  ×0.7；组合太多时改成按权重抽样再核对。must / forbid 限定一组里的处：must 至少有一处满足（小变化的换主体组），
  forbid 一处都不能满足（其余组）。

没有清单（画面描述关着、没写成、2026-10-23 前的旧描述）或 plan.unit 设成 fields 时，策划按字段（plan.py 的旧路径）。
"""

from __future__ import annotations

import itertools
import math
import random
from collections import Counter
from collections.abc import Callable
from typing import Any

from . import fields
from .config import ConfigError
from .context import Ctx
from .stages import profile

CANVAS = "canvas"
UNIT_MODE, FIELD_MODE = "pairs", "fields"
MODES = (UNIT_MODE, FIELD_MODE)
LARGE, MEDIUM, SMALL = "large", "medium", "small"
_DOWNGRADE = {LARGE: MEDIUM, MEDIUM: SMALL, SMALL: SMALL}
# 随机挑组合的权重（与 plan.py 按字段挑的同口径）；旧方向（没有单元）只按字段折半计入
SAME_DIMENSION_WEIGHT = 0.15
DONE_COMBO_WEIGHT = 0.2
LEGACY_DONE_WEIGHT = 0.5
LEGACY_USED_WEIGHT = 0.5
PICKED_PAIR_DECAY = 0.5
PICKED_UNIT_DECAY = 0.7
# 全枚举的组合数上限：超过就按权重抽样再核对（单元很多的图三处组合会到几十万种）
ENUM_LIMIT = 120_000
SAMPLE_ATTEMPTS = 400


def unit_mode(ctx: Ctx) -> str:
    """plan.unit：pairs（缺省，按「单元 × 字段」策划）/ fields（应急回退，按字段策划）。"""
    mode = str(ctx.setting("plan.unit", UNIT_MODE) or UNIT_MODE).strip()
    if mode not in MODES:
        raise ConfigError(f"plan.unit 只能是 {list(MODES)}，现在是 {mode!r}")
    return mode


def unit_key(object_ids: list[Any]) -> str:
    """单元的历史键：成员对象编号排序后拼接（重写画面描述、单元换了编号也对得上）；整幅是 CANVAS。"""
    return "+".join(sorted(str(o) for o in object_ids)) or CANVAS


def _member_names(desc: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for s in desc.get("subjects") or []:
        if isinstance(s, dict) and s.get("object_id"):
            names[str(s["object_id"])] = str(s.get("name") or s["object_id"])
    for e in desc.get("environment") or []:
        if not isinstance(e, dict):
            continue
        for oid in e.get("object_ids") or []:
            names.setdefault(str(oid), str(e.get("name") or oid))
    return names


def inventory(ctx: Ctx, analysis_id: str | None) -> list[dict[str, Any]] | None:
    """这份拆解的可变单元（不含整幅）；没有画面描述、或描述里没有 change_units（旧版）时为 None。

    类型 / 显著度不认识的单元丢掉；not_applicable 归到在用字段（停用编号按并入的字段算）。
    """
    desc = profile.get_description(ctx, analysis_id)
    if not desc or "change_units" not in (desc["result"] or {}):
        return None
    result = desc["result"]
    names = _member_names(result)
    subject_ids = {
        str(s["object_id"])
        for s in result.get("subjects") or []
        if isinstance(s, dict) and s.get("object_id")
    }
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in profile.change_units(result):
        uid = str(u["unit_id"])
        if (
            uid in seen
            or u.get("kind") not in fields.UNIT_KINDS
            or u.get("salience") not in fields.SALIENCE
        ):
            continue
        seen.add(uid)
        objects = sorted({str(o) for o in u.get("object_ids") or []})
        denied = {}
        for item in u.get("not_applicable") or []:
            if isinstance(item, dict):
                fid = fields.canonical(item.get("field_id"))
                if fid:
                    denied[fid] = str(item.get("reason") or "")
        units.append(
            {
                "unit_id": uid,
                "name": str(u.get("name") or uid),
                "kind": u["kind"],
                "salience": u["salience"],
                "object_ids": objects,
                "key": unit_key(objects),
                "parent": str(u["parent"]) if u.get("parent") else None,
                # 单元里有几个主体（角色组的判据：一只角色连着尾巴、道具等对象仍只算一个）
                "characters": len(set(objects) & subject_ids),
                "members": [names.get(o, o) for o in objects],
                "not_applicable": denied,
                "en": str(u.get("en") or ""),
            }
        )
    return units


def canvas_unit() -> dict[str, Any]:
    return {
        "unit_id": CANVAS,
        "name": fields.canvas_label(),
        "kind": CANVAS,
        "salience": None,
        "object_ids": [],
        "key": CANVAS,
        "parent": None,
        "characters": 0,
        "members": [],
        "not_applicable": {},
        "en": "",
    }


def _pair(unit: dict[str, Any], info: dict[str, Any], tier: str) -> dict[str, Any]:
    fid = info["field_id"]
    return {
        **info,
        "pair_id": f"{unit['unit_id']}:{fid}",
        "key": f"{unit['key']}:{fid}",
        "unit_id": unit["unit_id"],
        "unit_key": unit["key"],
        "unit_name": unit["name"],
        "unit_kind": unit["kind"],
        "unit_en": unit["en"],
        "salience": unit["salience"],
        "parent": unit.get("parent"),
        "objects": frozenset(unit["object_ids"]),
        "members": unit["members"],
        "region": fields.region(fid),
        "tier": tier,
    }


def pair_tier(unit: dict[str, Any], field_id: str) -> str:
    """这处改动在画面里的分量：整幅按字段档位，单元按显著度（只动脸部的降一档）。"""
    if unit["unit_id"] == CANVAS:
        return fields.magnitude(field_id)
    tier = unit["salience"]
    return _DOWNGRADE[tier] if fields.is_local(field_id) else tier


def pair_pool(
    units: list[dict[str, Any]], infos: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """能拿来改的「单元 × 字段」组合（小档的不出），按单元顺序、字段字典顺序排，整幅在最后。

    infos 是 plan.field_pools 的小变化池或三处组合池（这张图可变、系列没锁的字段），每项带现状、选项与兼容条件
    （三处组合池里构图不动时改不了的字段另带 layout_change，组合原样带上）。
    """
    has_prop = any(u["kind"] == "prop" for u in units)
    canvas = canvas_unit()
    unit_pairs: list[dict[str, Any]] = []
    canvas_pairs: list[dict[str, Any]] = []
    for info in infos:
        fid = info["field_id"]
        if fields.scope(fid) == fields.CANVAS:
            tier = pair_tier(canvas, fid)
            if tier != SMALL:
                canvas_pairs.append(_pair(canvas, info, tier))
            continue
        need = fields.requires(fid)
        for unit in units:
            if (
                unit["kind"] not in fields.applies_to(fid)
                or fid in unit["not_applicable"]
            ):
                continue
            if (
                need == fields.REQUIRES_GROUP
                and unit.get("characters", len(unit["object_ids"])) < 2
            ):
                continue
            if need == fields.REQUIRES_PROP and not has_prop:
                continue
            tier = pair_tier(unit, fid)
            if tier != SMALL:
                unit_pairs.append(_pair(unit, info, tier))
    order = {u["unit_id"]: i for i, u in enumerate(units)}
    field_order = {fid: i for i, fid in enumerate(fields.variation_field_ids())}
    unit_pairs.sort(
        key=lambda p: (order[p["unit_id"]], field_order.get(p["field_id"], 999))
    )
    return unit_pairs + canvas_pairs


def has_unit_pairs(pool: list[dict[str, Any]]) -> bool:
    return any(p["unit_id"] != CANVAS for p in pool)


def _overlap(p: dict[str, Any], q: dict[str, Any]) -> bool:
    return p["unit_key"] == q["unit_key"] or bool(p["objects"] & q["objects"])


def _conflict(p: dict[str, Any], q: dict[str, Any]) -> bool:
    return q["field_id"] in fields.conflicts(p["field_id"]) or p[
        "field_id"
    ] in fields.conflicts(q["field_id"])


def slot_ok(pairs: list[dict[str, Any]], *, units_present: bool = True) -> bool:
    """这几处能不能放进一个槽位（小变化两处 / 三处组合三处）。"""
    if units_present and sum(p["unit_id"] == CANVAS for p in pairs) > 1:
        return False
    for p, q in itertools.combinations(pairs, 2):
        if p["unit_id"] == CANVAS or q["unit_id"] == CANVAS:
            # 整幅字段的互斥对整个槽位生效：整体配色和任何一处单元配色都不同槽
            if p["key"] == q["key"] or _conflict(p, q):
                return False
            continue
        if not _overlap(p, q):
            continue
        if p["field_id"] == q["field_id"]:
            return False
        if LARGE not in (p["salience"], q["salience"]):
            return False
        if p["region"] and p["region"] == q["region"]:
            return False
        if _conflict(p, q):
            return False
    return True


PairTest = Callable[[dict[str, Any]], bool]


def _allowed(
    pool: list[dict[str, Any]], must: PairTest | None, forbid: PairTest | None
) -> tuple[list[int], set[int]]:
    """(能用的处, 满足 must 的处)：forbid 命中的处整个拿掉；没给 must 时第二项为空。"""
    allowed = [i for i, p in enumerate(pool) if not (forbid and forbid(p))]
    anchors = {i for i in allowed if must and must(pool[i])}
    return allowed, anchors


def count_valid(
    pool: list[dict[str, Any]],
    size: int,
    cap: int,
    *,
    must: PairTest | None = None,
    forbid: PairTest | None = None,
) -> int:
    """合法的槽位组合有几种（数到 cap 就停）：给每轮出几个、按钮文案用。must / forbid 同 pick_pair_sets。"""
    if cap <= 0 or len(pool) < size:
        return 0
    present = has_unit_pairs(pool)
    allowed, anchors = _allowed(pool, must, forbid)
    if must and not anchors:
        return 0
    n = 0
    for combo in itertools.combinations(allowed, size):
        if must and not anchors.intersection(combo):
            continue
        if slot_ok([pool[i] for i in combo], units_present=present):
            n += 1
            if n >= cap:
                break
    return n


def history(
    records: list[dict[str, Any]],
) -> tuple[Counter[str], Counter[str], set[frozenset], set[frozenset]]:
    """系列做过和提过的方向 → (组合改过几次, 旧方向每个字段改过几次, 做过的组合集, 旧方向的字段集)。

    组合按「单元键:字段」算；没有单元的旧方向（2026-10-23 前、按字段策划的、单字段大改）只按字段算。
    """
    used: Counter[str] = Counter()
    legacy_used: Counter[str] = Counter()
    done: set[frozenset] = set()
    legacy_done: set[frozenset] = set()
    for record in records:
        keys, legacy = [], []
        for cf in record.get("changed_fields") or []:
            if not isinstance(cf, dict):
                continue
            fid = fields.canonical(cf.get("field_id"))
            if not fid:
                continue
            if cf.get("unit_key"):
                keys.append(f"{cf['unit_key']}:{fid}")
            else:
                legacy.append(fid)
        used.update(keys)
        legacy_used.update(legacy)
        if keys and not legacy:
            done.add(frozenset(keys))
        elif legacy and not keys:
            legacy_done.add(frozenset(legacy))
    return used, legacy_used, done, legacy_done


def _base_weights(
    pool: list[dict[str, Any]], used: Counter[str], legacy_used: Counter[str]
) -> list[float]:
    """每处的基础权重：单元的份额平分给它的组合；同一个组下的成员单元合起来只占一份（十只恐龙不挤掉组、装饰和整幅）。"""
    per_unit = Counter(p["unit_key"] for p in pool)
    siblings = Counter(
        parent
        for parent, _ in {(p["parent"], p["unit_key"]) for p in pool if p["parent"]}
    )
    return [
        (1.0 / per_unit[p["unit_key"]])
        / (siblings[p["parent"]] if p["parent"] else 1)
        / (
            1
            + used.get(p["key"], 0)
            + LEGACY_USED_WEIGHT * legacy_used.get(p["field_id"], 0)
        )
        for p in pool
    ]


def _family(pair: dict[str, Any]) -> str:
    """同批衰减按「族」算：成员单元与它的组共用一个键（组的 unit_id），其余单元各算各的。"""
    return pair["parent"] or pair["unit_id"]


def _combo_weight(
    combo: tuple[int, ...],
    pool: list[dict[str, Any]],
    base: list[float],
    done: set[frozenset],
    legacy_done: set[frozenset],
) -> float:
    pairs = [pool[i] for i in combo]
    weight = math.prod(base[i] for i in combo)
    if len({p["dimension"] for p in pairs}) < len(pairs):
        weight *= SAME_DIMENSION_WEIGHT
    if frozenset(p["key"] for p in pairs) in done:
        weight *= DONE_COMBO_WEIGHT
    elif frozenset(p["field_id"] for p in pairs) in legacy_done:
        weight *= LEGACY_DONE_WEIGHT
    return weight


def pick_pair_sets(
    pool: list[dict[str, Any]],
    size: int,
    count: int,
    rng: random.Random,
    *,
    used: Counter[str] | None = None,
    legacy_used: Counter[str] | None = None,
    done: set[frozenset] | None = None,
    legacy_done: set[frozenset] | None = None,
    must: PairTest | None = None,
    forbid: PairTest | None = None,
) -> list[list[dict[str, Any]]]:
    """随机挑 count 组、每组 size 处：只挑合法的组合（slot_ok），同一批不重复。

    must：每组至少一处满足（如「是换主体」）；forbid：每组一处都不能满足。基础权重仍按整个池算。"""
    if count <= 0 or len(pool) < size:
        return []
    used, legacy_used = used or Counter(), legacy_used or Counter()
    done, legacy_done = done or set(), legacy_done or set()
    present = has_unit_pairs(pool)
    allowed, anchors = _allowed(pool, must, forbid)
    if len(allowed) < size or (must and not anchors):
        return []
    base = _base_weights(pool, used, legacy_used)
    if math.comb(len(allowed), size) > ENUM_LIMIT:
        return _sample_sets(
            pool,
            size,
            count,
            rng,
            base,
            done,
            legacy_done,
            units_present=present,
            allowed=allowed,
            anchors=anchors if must else None,
        )
    weights: dict[tuple[int, ...], float] = {}
    for combo in itertools.combinations(allowed, size):
        if must and not anchors.intersection(combo):
            continue
        if slot_ok([pool[i] for i in combo], units_present=present):
            weights[combo] = _combo_weight(combo, pool, base, done, legacy_done)
    family = [_family(p) for p in pool]
    picked: list[list[dict[str, Any]]] = []
    pair_hits: Counter[int] = Counter()
    family_hits: Counter[str] = Counter()
    for _ in range(min(count, len(weights))):
        candidates = list(weights)
        chosen = rng.choices(
            candidates,
            weights=[
                weights[c]
                * PICKED_PAIR_DECAY ** sum(pair_hits[i] for i in c)
                * PICKED_UNIT_DECAY ** sum(family_hits[family[i]] for i in c)
                for c in candidates
            ],
        )[0]
        del weights[chosen]
        pair_hits.update(chosen)
        family_hits.update(family[i] for i in chosen)
        picked.append([pool[i] for i in chosen])
    return picked


def _sample_sets(
    pool: list[dict[str, Any]],
    size: int,
    count: int,
    rng: random.Random,
    base: list[float],
    done: set[frozenset],
    legacy_done: set[frozenset],
    *,
    units_present: bool,
    allowed: list[int] | None = None,
    anchors: set[int] | None = None,
) -> list[list[dict[str, Any]]]:
    """组合太多时：按处的权重（含同批衰减）不放回地抽 size 处，合法且没挑过才收下。

    allowed 是能用的处（forbid 拿掉之后）；给了 anchors 时第一处只从 anchors 里抽（must）。"""
    family = [_family(p) for p in pool]
    usable = list(range(len(pool))) if allowed is None else list(allowed)
    picked: list[list[dict[str, Any]]] = []
    taken: set[tuple[int, ...]] = set()
    pair_hits: Counter[int] = Counter()
    family_hits: Counter[str] = Counter()
    for _ in range(count * SAMPLE_ATTEMPTS):
        if len(picked) >= count:
            break
        weights = [
            base[i]
            * PICKED_PAIR_DECAY ** pair_hits[i]
            * PICKED_UNIT_DECAY ** family_hits[family[i]]
            for i in range(len(pool))
        ]
        combo: list[int] = []
        avail = list(usable)
        if anchors:
            first = sorted(anchors)
            i = rng.choices(first, weights=[weights[j] for j in first])[0]
            combo.append(i)
            avail.remove(i)
        while len(combo) < size:
            i = rng.choices(avail, weights=[weights[j] for j in avail])[0]
            combo.append(i)
            avail.remove(i)
        key = tuple(sorted(combo))
        if key in taken or not slot_ok(
            [pool[i] for i in key], units_present=units_present
        ):
            continue
        # 同维度、做过的组合照样靠后：按组合权重与单处权重之比决定收不收
        keep = _combo_weight(key, pool, base, done, legacy_done) / math.prod(
            base[i] for i in key
        )
        if rng.random() > keep:
            continue
        taken.add(key)
        pair_hits.update(key)
        family_hits.update(family[i] for i in key)
        picked.append([pool[i] for i in key])
    return picked


def pair_label(pair: dict[str, Any]) -> str:
    """「高地牛·表情」；整幅的只写字段短名（「整体配色」）。"""
    label = fields.label(pair.get("field_id"))
    if not pair.get("unit_name") or pair.get("unit_id") in (None, CANVAS):
        return label
    return f"{pair['unit_name']}·{label}"


def unit_for_prompt(pair: dict[str, Any]) -> dict[str, Any]:
    """给 P05 的单元信息：整幅只写名字；单元写类型、显著度、成员（对象编号可在 reference_profile 里查到细节）。"""
    if pair["unit_id"] == CANVAS:
        return {"name": pair["unit_name"], "kind": CANVAS}
    return {
        "name": pair["unit_name"],
        "kind": pair["unit_kind"],
        "salience": pair["salience"],
        "object_ids": sorted(pair["objects"]),
        "members": pair["members"],
    }


def unit_overview(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批次里记下当时的单元清单（追溯用）。"""
    return [
        {k: u[k] for k in ("unit_id", "name", "kind", "salience", "key", "parent")}
        for u in units
    ]


def missing_inventory(ctx: Ctx) -> list[str]:
    """拆解过、有画面描述但描述里没有可变单元清单的参考图（策划会按字段）。"""
    out = []
    for row in ctx.db.q(
        "SELECT s.seed_id, d.result_json FROM seeds s JOIN seed_descriptions d"
        " ON d.analysis_id = s.latest_analysis_id WHERE s.status != 'blocked' ORDER BY s.seed_id"
    ):
        if '"change_units"' not in (row["result_json"] or ""):
            out.append(row["seed_id"])
    return out
