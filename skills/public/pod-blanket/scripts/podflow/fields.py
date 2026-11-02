"""字段字典：在用 / 停用、小变化 / 大变化 / 只记录、短名与维度门控。

字典是 references/field_dictionary.json（方案「设计拆解模板」）。编号永不复用：停用字段只为读旧数据保留，
merged_into 指向并入的字段（0.3 起绘制技法 D42–D44 并入 D41）；拆解（P02）只输出在用字段，裂变只在小变化 / 大变化字段上做。
0.4 起可变字段另记挂在哪（可变单元 / 整幅）、能挂哪类单元、部位、互斥与档位，给按「单元 × 字段」组合策划用（subjects.py）。
0.5 起另记能进哪几类槽位（slots：绘制技法不进小变化，动作姿态这类动态字段也进单字段大改）与哪一版加的字段
（since：之前拆的图上没判断过）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .paths import REFERENCES_DIR

SMALL, BIG, RECORD = "small", "big", "record"
VARIABLE_TIERS = (SMALL, BIG)
# 槽位类型（0.5）：小变化（两处）/ 单字段大改 / 三处组合（三处），字段能进哪几类看 slot_kinds
COMBO = "combo"
SLOT_KINDS = (SMALL, BIG, COMBO)
# 换主体：单元里的角色 / 物件主体整个换成同主题的另一种（小变化的换主体组每组至少有这一处）
SWAP_FIELD = "D09"
# 绘制技法：单字段大改与二次大变化里换画法大类（按换画法清单分），三处组合里同一画法大类内整幅调整
TECHNIQUE_FIELD = "D41"
TECHNIQUE_FAMILY_ASPECT = "画法大类"
# 字段挂在哪（0.4）：可变单元上 / 整幅上；单元类型、显著度与 requires 的取值
UNIT, CANVAS = "unit", "canvas"
UNIT_KINDS = ("character", "object", "decoration", "prop", "text")
SALIENCE = ("large", "medium", "small")
REQUIRES_GROUP, REQUIRES_PROP = "group", "prop"
TIER_LABELS = {SMALL: "小变化", BIG: "大变化", RECORD: "只记录"}
LAYOUT_KINDS = ("scene", "pattern", "mixed")
LAYOUT_LABELS = {"scene": "单幅场景", "pattern": "满版花型", "mixed": "混合"}
# 小变化的构图锁：参照 Mooya Pattern DNA 相似度「中 / 高」两档的保留清单，改的就是其中一项时除外
COMPOSITION_LOCK = [
    "整体构图与布局结构",
    "每个元素的位置、大小与比例关系",
    "留白与疏密",
    "排列方式与重复逻辑",
    "配色",
    "线条与上色方式",
    "质感与细节尺度",
]


@lru_cache(maxsize=None)
def dictionary() -> dict[str, Any]:
    return json.loads(
        (REFERENCES_DIR / "field_dictionary.json").read_text(encoding="utf-8")
    )


@lru_cache(maxsize=None)
def _entries() -> dict[str, dict[str, Any]]:
    return {f["field_id"]: f for f in dictionary()["design_fields"]}


def entry(field_id: Any) -> dict[str, Any] | None:
    return _entries().get(str(field_id)) if field_id else None


def schema_version() -> str:
    """字典版本：拆解结果与系列规则里记的 field_schema，没有它的是梳理前（2026-10-16 之前）拆的。"""
    return str(dictionary()["schema_version"])


def dimensions() -> list[str]:
    return list(dictionary()["dimensions"])


def all_design_field_ids() -> list[str]:
    """全部 D 字段编号（含停用的）。"""
    return list(_entries())


def known_field_ids() -> set[str]:
    """字典里出现过的全部编号（D 含停用 + M）。"""
    return set(_entries()) | {f["field_id"] for f in dictionary()["input_fields"]}


def is_active(field_id: Any) -> bool:
    e = entry(field_id)
    return bool(e) and e.get("status", "active") == "active"


def active_fields() -> list[dict[str, Any]]:
    return [f for f in _entries().values() if f.get("status", "active") == "active"]


def active_field_ids() -> list[str]:
    return [f["field_id"] for f in active_fields()]


def tier(field_id: Any) -> str | None:
    """在用字段的层级（small / big / record）；停用或不认识的返回 None。"""
    e = entry(field_id)
    if not e or e.get("status", "active") != "active":
        return None
    return e.get("tier")


def kinds(field_id: Any) -> list[str]:
    """这个字段在哪几层（small / big）：按它所在的维度算，拆解按层判断能不能变。绘制技法两层都在：三处组合里
    同一画法大类内调整，单字段大改换画法大类。能进哪类槽位看 slot_kinds。"""
    t = tier(field_id)
    if t not in VARIABLE_TIERS:
        return []
    dim = dimension(field_id)
    return [k for k in VARIABLE_TIERS if k == t or dim in tier_dimensions(k)]


def slot_kinds(field_id: Any) -> list[str]:
    """这个字段能进哪几类槽位（SLOT_KINDS 顺序）：小变化层的字段进小变化与三处组合，大变化层的进单字段大改；
    字典里写了 slots 的按它（绘制技法只进单字段大改与三处组合，动作姿态这类动态字段三类都进）。"""
    layers = kinds(field_id)
    if not layers:
        return []
    allowed = _canonical_entry(field_id).get("slots")
    if allowed is not None:
        return [k for k in SLOT_KINDS if k in allowed]
    default = set()
    if SMALL in layers:
        default |= {SMALL, COMBO}
    if BIG in layers:
        default.add(BIG)
    return [k for k in SLOT_KINDS if k in default]


def is_dynamic(field_id: Any) -> bool:
    """动态字段：小变化层、又能进单字段大改的字段（动作姿态、朝向视线、距离互动、承托依附、陪衬角色）。

    小变化里照 small_change 留在原位置改；大变化里可以连带调整位置、大小和布局。"""
    fid = canonical(field_id)
    return bool(fid) and tier(fid) == SMALL and BIG in slot_kinds(fid)


def technique_families() -> list[str]:
    """绘制技法的画法大类（change_forms 里「画法大类」那一组）：换画法清单从这里分。"""
    for form in change_forms(TECHNIQUE_FIELD):
        if form.get("aspect") == TECHNIQUE_FAMILY_ASPECT:
            return [str(f) for f in form.get("forms") or [] if f]
    return []


def since(field_id: Any) -> str | None:
    """字段是哪一版字典加的（D59 / D60 是 0.5）；早期就有的字段为 None。之前拆的图上没判断过它。"""
    return _canonical_entry(field_id).get("since")


def small_change(field_id: Any) -> str:
    """这个字段的小变化怎么改才看得出（单元里的全部成员一起改）；不出小变化的字段返回空串。"""
    e = entry(field_id)
    return (e or {}).get("small_change", "") if SMALL in kinds(field_id) else ""


def big_change(field_id: Any) -> str:
    """单字段大改时怎么改（绘制技法：换画法大类；动态字段：连带调整布局的大改）；没写的返回空串。"""
    e = entry(field_id)
    return (e or {}).get("big_change", "") if BIG in slot_kinds(field_id) else ""


def change_forms(field_id: Any) -> list[dict[str, Any]]:
    """可以改的形式清单（绘制技法：画法大类、线条、上色、光影、纹理、造型处理），模型按原图画风挑着改。"""
    e = entry(field_id)
    return list((e or {}).get("change_forms") or []) if is_active(field_id) else []


def merged_sources(field_id: Any) -> list[str]:
    """并进这个字段的停用字段（D41 ← D42 / D43 / D44），读旧拆解与旧系列时归到它名下。"""
    return [
        f["field_id"]
        for f in _entries().values()
        if f.get("status") == "retired" and f.get("merged_into") == field_id
    ]


def _canonical_entry(field_id: Any) -> dict[str, Any]:
    fid = canonical(field_id)
    return (entry(fid) or {}) if fid else {}


def scope(field_id: Any) -> str:
    """这个字段挂在哪：unit（可变单元上）/ canvas（整幅）；没写时大变化字段算整幅、其余算单元。"""
    e = _canonical_entry(field_id)
    return e.get("scope") or (CANVAS if e.get("tier") == BIG else UNIT)


def applies_to(field_id: Any) -> list[str]:
    """挂在单元上的字段能挂哪几类单元（UNIT_KINDS）；整幅字段为空。"""
    if scope(field_id) != UNIT:
        return []
    return list(_canonical_entry(field_id).get("applies_to") or [])


def region(field_id: Any) -> str | None:
    """同一单元上的部位（face / body / wear / species / surface）：两处落在同一单元时部位要不同。"""
    return _canonical_entry(field_id).get("region")


def is_local(field_id: Any) -> bool:
    """只动脸部这类局部：档位比单元的显著度低一档。"""
    return bool(_canonical_entry(field_id).get("local"))


def requires(field_id: Any) -> str | None:
    """REQUIRES_GROUP：只给有两个以上成员的角色组；REQUIRES_PROP：画面里要有承托物单元。"""
    return _canonical_entry(field_id).get("requires")


def conflicts(field_id: Any) -> list[str]:
    """不能和它一起改的字段：单元字段之间看同一单元（换物种和轮廓、头身）；有一方是整幅字段时对整个槽位生效
    （整体配色和单元配色）。"""
    return [
        canonical(f) or f for f in _canonical_entry(field_id).get("conflicts") or []
    ]


def magnitude(field_id: Any) -> str:
    """整幅字段的档位（large / medium），没写算 large。"""
    return _canonical_entry(field_id).get("magnitude") or "large"


def _units_spec() -> dict[str, Any]:
    return dictionary().get("change_units") or {}


def unit_kind_labels() -> dict[str, str]:
    return dict(_units_spec().get("kinds") or {})


def salience_labels() -> dict[str, str]:
    return dict(_units_spec().get("salience") or {})


def region_labels() -> dict[str, str]:
    return dict(_units_spec().get("regions") or {})


def canvas_label() -> str:
    return _units_spec().get("canvas_label") or "整幅"


def variation_field_ids(which: str | None = None) -> list[str]:
    """可以裂变的在用字段（小变化 + 大变化，或只取会出其中一类方向的）。"""
    if which is None:
        return [
            f["field_id"] for f in active_fields() if f.get("tier") in VARIABLE_TIERS
        ]
    return [f["field_id"] for f in active_fields() if which in kinds(f["field_id"])]


def dimension(field_id: Any) -> str | None:
    e = entry(field_id)
    return e.get("dimension") if e else None


def dimension_name(dim: str | None) -> str:
    """「10 配色结构」→「配色结构」。"""
    if not dim:
        return ""
    head, _, rest = dim.partition(" ")
    return rest if head.isdigit() and rest else dim


def field_name(field_id: Any) -> str:
    e = entry(field_id)
    return e.get("field", str(field_id)) if e else str(field_id)


def label(field_id: Any) -> str:
    """挑选卡上的短名；停用字段用它并入的字段的短名。"""
    e = entry(field_id)
    if not e:
        return str(field_id or "")
    if e.get("label"):
        return e["label"]
    merged = e.get("merged_into")
    if merged and entry(merged):
        return label(merged)
    return e.get("field", str(field_id))


def canonical(field_id: Any) -> str | None:
    """旧数据归位：在用字段是它自己，停用并入的是并入目标，其余（移出字典的）返回 None。"""
    e = entry(field_id)
    if not e:
        return None
    if e.get("status", "active") == "active":
        return e["field_id"]
    merged = e.get("merged_into")
    return merged if merged and is_active(merged) else None


def tier_dimensions(which: str) -> list[str]:
    return list(dictionary()["tiers"][which]["dimensions"])


def tier_rule(which: str) -> str:
    return dictionary()["tiers"][which].get("rule", "")


def gates() -> list[dict[str, Any]]:
    return list(dictionary().get("gates") or [])


def gated_off(traits: dict[str, Any] | None) -> dict[str, str]:
    """按参考图特征关掉的维度 → 原因（单幅场景关排列重复，没有文字关文字定制）。"""
    traits = traits or {}
    off: dict[str, str] = {}
    for gate in gates():
        if traits.get(gate["trait"]) not in gate["allowed"]:
            off[gate["dimension"]] = gate["reason"]
    return off


def prompt_dictionary() -> dict[str, Any]:
    """给 P02 的字典：只有在用字段，带层级；停用字段只列编号与去向，告诉模型不要再输出。"""
    d = dictionary()
    return {
        "schema_version": d.get("schema_version"),
        "dimensions": d["dimensions"],
        "tiers": {
            name: {
                "label": spec["label"],
                "dimensions": spec["dimensions"],
                "rule": spec.get("rule"),
            }
            for name, spec in d["tiers"].items()
        },
        "gates": gates(),
        "fields": [
            {
                "field_id": f["field_id"],
                "dimension": f["dimension"],
                "field": f["field"],
                "tier": f.get("tier"),
                "requirement": f["requirement"],
            }
            for f in active_fields()
        ],
        "retired_fields": [
            {
                "field_id": f["field_id"],
                "field": f["field"],
                "goes_to": f.get("merged_into") or f.get("moved_to"),
            }
            for f in _entries().values()
            if f.get("status") == "retired"
        ],
    }


@lru_cache(maxsize=None)
def text_bank() -> dict[str, Any]:
    """文字定制的文案库（references/text_bank.json）：改文字内容、加定制位时从里面挑；文件不在时为空。"""
    path = REFERENCES_DIR / "text_bank.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def text_bank_for_prompt(field_ids: Any) -> dict[str, Any] | None:
    """槽位里有用文案库的字段（文字内容、可定制位）时给 P05 的文案库（不带说明与字段清单），否则 None。"""
    bank = text_bank()
    wanted = {canonical(f) or f for f in field_ids or []}
    if not bank or not wanted & set(bank.get("fields") or []):
        return None
    return {k: v for k, v in bank.items() if k not in ("note", "fields", "version")}
