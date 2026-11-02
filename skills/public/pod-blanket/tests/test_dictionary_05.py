"""字段字典 0.5：物件单元、绘制技法不进小变化、三处组合用构图不动时改不了的字段（layout_change）、
单元配色 / 表面纹样（之前拆的图上按可变处理）、整体配色与单元配色不同槽、文字定制的文案库。"""

from __future__ import annotations

import json
import random
from typing import Any

from conftest import reopen, set_config, use_small
from podflow import fields, subjects
from podflow.paths import SCHEMAS_DIR
from podflow.stages import plan as planmod
from podflow.stages import profile
from podflow.stages import seed as seedmod
from podflow.stages.common import STYLE_KEEP, STYLE_RESTYLE, style_scope
from podflow.util import dumps, loads
from test_subject_pairs import _family, _home, _info, _unit
from test_variant_rules import _approved_family


def _enums(node: Any, key: str) -> list[list[Any]]:
    """schema 里所有叫 key 的属性的枚举值。"""
    found: list[list[Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict) and "enum" in v:
                found.append(v["enum"])
            found += _enums(v, key)
    elif isinstance(node, list):
        for v in node:
            found += _enums(v, key)
    return found


def test_object_units_take_object_fields() -> None:
    """物件主体能换主体、改轮廓、加五官、戴配件、换色、换纹样；表情、五官画法只挂角色。装饰群也能改轮廓、加五官。"""
    units = [
        _unit("U01", "游戏手柄", "object", ["E01"], "large"),
        _unit("U02", "满屏星星", "decoration", ["E02", "E03"], "medium"),
    ]
    fids = ["D09", "D11", "D13", "D15", "D16", "D17", "D20", "D59", "D60"]
    pool = subjects.pair_pool(units, [_info(f) for f in fids])
    assert [subjects.pair_label(p) for p in pool] == [
        "游戏手柄·主体",
        "游戏手柄·轮廓体量",
        "游戏手柄·拟人程度",
        "游戏手柄·表面纹样",
        "游戏手柄·服饰配件",
        "游戏手柄·单元配色",
        "满屏星星·背景装饰",
        "满屏星星·轮廓体量",
        "满屏星星·拟人程度",
        "满屏星星·表面纹样",
        "满屏星星·单元配色",
    ]
    assert "object" in fields.UNIT_KINDS and fields.unit_kind_labels()["object"]
    assert profile.unit_lines(
        [
            {
                "unit_id": "U01",
                "name": "两个手柄",
                "kind": "object",
                "object_ids": ["E01", "E02"],
                "salience": "large",
            },
            {
                "unit_id": "U02",
                "name": "左手柄",
                "kind": "object",
                "object_ids": ["E01"],
                "parent": "U01",
                "salience": "medium",
            },
        ]
    ) == ["两个手柄（物件组·大）", "左手柄（物件·中）"]
    schema = json.loads(
        (SCHEMAS_DIR / "P13.result.schema.json").read_text(encoding="utf-8")
    )
    assert ["character", "object", "decoration", "prop", "text"] in _enums(
        schema, "kind"
    )


def test_inventory_keeps_object_units(home, fake, seed_image) -> None:
    """画面描述里写成 object 的单元进清单，挂上物件能用的字段。"""
    ctx = _home(home, fake)
    fake.p13_units = [
        {
            "unit_id": "U01",
            "name": "游戏手柄",
            "kind": "object",
            "object_ids": ["E01"],
            "parent": None,
            "salience": "large",
            "not_applicable": [],
            "en": "the game controller",
        },
        {
            "unit_id": "U02",
            "name": "满屏星星",
            "kind": "decoration",
            "object_ids": ["E02", "E03"],
            "parent": None,
            "salience": "medium",
            "not_applicable": [],
            "en": "the stars",
        },
    ]
    family_id = _family(ctx, seed_image)
    inputs = planmod.unit_pools(ctx, planmod.planning_family(ctx, family_id))
    assert [u["kind"] for u in inputs["units"]] == ["object", "decoration"]
    labels = [subjects.pair_label(p) for p in inputs["pairs"]["small"]]
    assert "游戏手柄·主体" in labels and "游戏手柄·服饰配件" in labels
    assert "游戏手柄·表情" not in labels
    assert profile.show(ctx, "S01")["units"] == [
        "游戏手柄（物件·大）",
        "满屏星星（装饰·中）",
    ]


def test_unit_color_and_whole_color_never_share_a_slot() -> None:
    """整体配色和单元配色互斥（整幅字段的互斥对整个槽位生效）；单元配色和表面纹样都在表面，同一单元上不一起改。"""
    units = [_unit("U01", "高地牛", "character", ["E01"], "large")]
    pool = subjects.pair_pool(units, [_info(f) for f in ("D17", "D59", "D60", "D37")])
    by = {subjects.pair_label(p): p for p in pool}
    assert not subjects.slot_ok([by["整体配色"], by["高地牛·单元配色"]])
    assert subjects.slot_ok([by["整体配色"], by["高地牛·表面纹样"]])
    assert not subjects.slot_ok([by["高地牛·单元配色"], by["高地牛·表面纹样"]])
    assert subjects.slot_ok([by["高地牛·单元配色"], by["高地牛·表情"]])
    # 按字段挑时也不同组，数量跟着少
    assert planmod._clash(["D37", "D59"]) and not planmod._clash(["D37", "D60"])
    flat = [_info(f) for f in ("D17", "D37", "D59")]
    sets = planmod._pick_sets(flat, 2, 9, random.Random(0), {}, set())
    assert sorted(sorted(f["field_id"] for f in s) for s in sets) == [
        ["D17", "D37"],
        ["D17", "D59"],
    ]
    assert planmod._set_count(flat, 2, 9) == 2


def test_unit_color_and_pattern_keep_the_reference_style() -> None:
    """单元配色、表面纹样只改一个单元：画风沿用参考图；整体配色、绘制技法才整幅一起换。"""
    assert style_scope([{"field_id": "D59"}]) == STYLE_KEEP
    assert style_scope([{"field_id": "D60"}, {"field_id": "D09"}]) == STYLE_KEEP
    assert style_scope([{"field_id": "D59"}, {"field_id": "D37"}]) == STYLE_RESTYLE
    assert style_scope([{"field_id": "D41"}]) == STYLE_RESTYLE


def test_combo_pool_takes_fields_the_composition_lock_rules_out(
    home, fake, seed_image
) -> None:
    """三处组合池另收拆解判成不可变的小变化字段（带 layout_change）：被门控关掉的维度、系列锁定的不收；
    其中的动态字段也进单字段大改，小变化池不受影响；改动带着 layout_change 存下来，出图与质检看得到。"""
    use_small(fake)
    set_config(home, "plan.combo_layout_fields", True)
    ctx = reopen(home)
    family_id = _approved_family(ctx, seed_image)
    fam = planmod.planning_family(ctx, family_id)
    pools = planmod.field_pools(ctx, fam)
    assert [f["field_id"] for f in pools["small"]] == ["D09", "D17", "D37"]
    assert not any(f.get("layout_change") for f in pools["small"])
    # 被判不能变的动态字段（动作 / 朝向 / 互动 / 承托 / 陪衬）进单字段大改，头身比例这类不进
    big = {f["field_id"]: f for f in pools["big"]}
    assert list(big) == ["D10", "D18", "D19", "D23", "D25", "D41"]
    assert {fid for fid, f in big.items() if f.get("layout_change")} == {
        "D10",
        "D18",
        "D19",
        "D23",
        "D25",
    }
    combo = {f["field_id"]: f for f in pools["combo"]}
    layout = {fid for fid, f in combo.items() if f.get("layout_change")}
    assert {"D10", "D14", "D18", "D23", "D25"} <= layout
    # 单幅场景关掉排列重复，没有文字关掉文字定制
    assert not layout & {"D33", "D34", "D49", "D50", "D51"}
    assert {"D09", "D17", "D37", "D41"} <= set(combo) and not layout & {
        "D09",
        "D17",
        "D37",
        "D41",
    }
    assert combo["D18"]["options"] == [] and combo["D18"]["current"] == "D18 的取值"
    fam["spec"]["locked_fields"] = [{"field_id": "D18", "reason": "姿态保持"}]
    assert "D18" not in {f["field_id"] for f in planmod.field_pools(ctx, fam)["combo"]}
    preview = planmod.preview_slots(ctx, family_id)
    assert "动作姿态" in preview["layout_fields"]

    plan = planmod.plan_batch(ctx, family_id)
    # 大字段池有 6 个字段：单字段大改出满 4 个，三处组合补足其余 4 个
    assert plan["slots"]["big"] == 4 and plan["slots"]["combo"] == 4
    assert plan["slots"]["small"] == 3
    combo_group = next(g for g in fake.p05_groups if g["kind"] == "combo")
    marked = [
        f for s in combo_group["slots"] for f in s["fields"] if f.get("layout_change")
    ]
    assert marked and all(f["small_change"] for f in marked)
    small_group = next(g for g in fake.p05_groups if g["kind"] == "small")
    assert not any(
        f.get("layout_change") for s in small_group["slots"] for f in s["fields"]
    )
    stored = [
        cf
        for r in ctx.db.q("SELECT plan_json FROM variants")
        for cf in loads(r["plan_json"])["changed_fields"]
    ]
    flagged = {cf["field_id"] for cf in stored if cf.get("layout_change")}
    assert flagged and flagged <= layout

    # 关掉开关：组合池只有小变化池 + 绘制技法
    set_config(home, "plan.combo_layout_fields", False)
    ctx = reopen(home)
    pools = planmod.field_pools(ctx, planmod.planning_family(ctx, family_id))
    assert [f["field_id"] for f in pools["combo"]] == ["D09", "D17", "D37", "D41"]


def test_new_fields_count_as_variable_on_older_analyses(home, fake, seed_image) -> None:
    """0.5 加的单元配色、表面纹样：之前拆的图上没判断过，按可变处理（选项留空）；系列锁了就不用；
    拆解判断过的按判断。"""
    use_small(fake)
    ctx = reopen(home)
    family_id = _approved_family(ctx, seed_image)
    analysis = seedmod.latest_analysis(ctx, "S01")
    result = loads(analysis["result_json"])
    fam = planmod.planning_family(ctx, family_id)
    # 0.5 拆的：两个新字段判成了不可变，小变化池里没有
    assert not {"D59", "D60"} & {
        f["field_id"] for f in planmod.field_pools(ctx, fam)["small"]
    }
    old = {
        **result,
        "fields": [f for f in result["fields"] if f["field_id"] not in ("D59", "D60")],
    }
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE analyses SET result_json = ? WHERE analysis_id = ?",
            (dumps(old), analysis["analysis_id"]),
        )
    pools = planmod.field_pools(ctx, fam)
    small = {f["field_id"]: f for f in pools["small"]}
    assert {"D59", "D60"} <= set(small)
    assert small["D59"]["options"] == [] and not small["D59"].get("layout_change")
    assert {"D59", "D60"} <= {f["field_id"] for f in pools["combo"]}
    fam["spec"]["locked_fields"] = [{"field_id": "D60", "reason": "纹样保持"}]
    assert "D60" not in {f["field_id"] for f in planmod.field_pools(ctx, fam)["small"]}


def test_text_bank_reaches_p05_only_for_text_slots() -> None:
    bank = fields.text_bank()
    assert bank["fields"] == ["D49", "D50"] and "Game On" in bank["phrases"]["gamer"]
    assert "Merry Christmas" in bank["phrases"]["christmas"] and bank["names"]
    assert fields.text_bank_for_prompt(["D09", "D37"]) is None
    got = fields.text_bank_for_prompt(["D17", "D49"])
    assert set(got) == {"rules", "phrases", "names", "name_templates", "date_formats"}

    def group(fids: list[str]) -> dict:
        return {
            "kind": "small",
            "slots": [
                {
                    "slot_id": "S1",
                    "kind": "small",
                    "field_ids": fids,
                    "fields": [_info(f) for f in fids],
                }
            ],
        }

    plan_group, _ = planmod._group_input(group(["D49", "D17"]), {}, [])
    assert plan_group["text_bank"]["names"] == bank["names"]
    assert "text_bank" not in planmod._group_input(group(["D09", "D17"]), {}, [])[0]


def test_p04_sees_which_slots_each_field_can_enter() -> None:
    def item(fid: str) -> dict:
        return {
            "field_id": fid,
            "label": fields.label(fid),
            "dimension": fields.dimension(fid),
            "tier": fields.tier(fid),
            "value": "x",
            "reason": "",
        }

    out = seedmod._variable_fields_input(
        {
            "traits": {"layout_kind": "scene", "has_text": False},
            "gated_off": {},
            "variable": [item("D41"), item("D09"), item("D05")],
            "not_variable": [],
        }
    )
    assert {v["field_id"]: v["kinds"] for v in out["variable"]} == {
        "D41": ["big", "combo"],
        "D09": ["small", "combo"],
        "D05": ["big"],
    }
