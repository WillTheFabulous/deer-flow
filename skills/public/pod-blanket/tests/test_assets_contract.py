"""方案资产契约：提示词段落与变量、schema 合法性、字段字典覆盖。"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from podflow import fields, prompts, schemas
from podflow.paths import PROMPTS_DIR, REFERENCES_DIR, SCHEMAS_DIR
from podflow.stages.common import reference

# P08 近重复复核已下线（2026-10-13），编号空着不复用；P12 任务命名（2026-10-20）；P13 画面完整描述（2026-10-21）；
# P14 换主体清单（2026-10-27）
ALL_PROMPTS = ["G00", *(f"P{i:02d}" for i in range(15) if i != 8)]


def test_every_prompt_has_five_sections() -> None:
    for pid in ALL_PROMPTS:
        prompt = prompts.load_prompt(pid)
        assert set(prompts.SECTIONS) <= set(prompt.sections), pid


def test_prompt_files_have_no_comments() -> None:
    """注释里的占位符会被二次替换进 prompt（mooya 教训），提示词文件不允许出现注释。"""
    for path in PROMPTS_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "<!--" not in text and "-->" not in text, path.name


def test_declared_variables_match_the_plan() -> None:
    assert prompts.load_prompt("G00").variables == [
        "project_constraints",
        "schema_version",
        "job_context",
    ]
    assert prompts.load_prompt("P02").variables == [
        "seed_record",
        "source_image",
        "field_dictionary",
    ]
    assert prompts.load_prompt("P04").variables == [
        "analysis",
        "reference_profile",
        "variable_fields",
        "hypotheses",
        "user_positioning",
        "rights_scope",
        "production_constraints",
    ]
    assert prompts.load_prompt("P05").variables == [
        "approved_family_spec",
        "reference_profile",
        "plan_group",
        "completed_plan_index",
        "rejected_patterns",
        "user_direction_request",
    ]
    assert prompts.load_prompt("P07").variables == [
        "candidate_image",
        "reference_image",
        "variant_job",
        "family_spec",
        "reference_profile",
        "file_check_results",
        "ocr_results",
    ]
    assert prompts.load_prompt("P06").variables == [
        "variant_job",
        "family_spec",
        "reference_profile",
        "reference_image",
        "reference_assets",
        "generator_capabilities",
        "reference_traits",
        "optional_sku_template",
    ]
    assert prompts.load_prompt("P12").variables == [
        "source_images",
        "source_metadata",
    ]
    assert prompts.load_prompt("P13").variables == [
        "source_image",
        "analysis",
        "seed_record",
        "user_hint",
    ]
    assert prompts.load_prompt("P14").variables == [
        "swap_units",
        "reference_profile",
        "family_rules",
        "user_direction_request",
    ]
    assert not (PROMPTS_DIR / "P08.md").exists()
    assert not (SCHEMAS_DIR / "P08.result.schema.json").exists()


@pytest.mark.parametrize(
    "path", sorted(SCHEMAS_DIR.glob("*.schema.json")), ids=lambda p: p.name
)
def test_schemas_are_valid(path) -> None:
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_every_model_stage_has_a_result_schema() -> None:
    for pid in ALL_PROMPTS[2:]:
        assert (SCHEMAS_DIR / f"{pid}.result.schema.json").exists() or pid == "P00", pid


def test_field_dictionary_keeps_60_ids_and_40_active_fields() -> None:
    d = reference("field_dictionary.json")
    assert d["schema_version"] == "0.5"
    assert len(d["dimensions"]) == 14
    # 编号永不复用：0.5 加的 D59 / D60 排在各自维度里（字典顺序 = 策划时的字段顺序）
    ids = fields.all_design_field_ids()
    assert sorted(ids) == [f"D{i:02d}" for i in range(1, 61)] and len(set(ids)) == 60
    assert ids.index("D16") < ids.index("D60") < ids.index("D17")
    assert ids.index("D40") < ids.index("D59") < ids.index("D41")
    assert [f["field_id"] for f in d["input_fields"]] == [
        f"M{i:02d}" for i in range(1, 7)
    ]
    assert len(fields.active_field_ids()) == 40
    # 小变化字段 22 个 + 绘制技法 D41（两层都在）；大字段 D05–D07、D27–D30、D41
    assert len(fields.variation_field_ids("small")) == 23
    assert len(fields.variation_field_ids("big")) == 8
    assert len(fields.variation_field_ids()) == 30
    record = [f["field_id"] for f in fields.active_fields() if f["tier"] == "record"]
    assert record == [
        "D01",
        "D24",
        "D32",
        "D36",
        "D39",
        "D45",
        "D46",
        "D53",
        "D54",
        "D55",
    ]
    checks = [c["check_id"] for c in reference("qc_checks.json")["checks"]]
    assert len(checks) == 17 and "Q10" not in checks
    assert (REFERENCES_DIR / "variation_pool.json").exists()


def test_kinds_technique_fields_give_both_small_and_big() -> None:
    """绘制技法只剩 D41 一个字段，两层都在（只进单字段大改与三处组合）；其余按层级进一类，只记录 / 停用的不进。"""
    assert [
        fid
        for fid in fields.active_field_ids()
        if fields.kinds(fid) == ["small", "big"]
    ] == ["D41"]
    assert fields.label("D41") == "绘制技法"
    # 线条、上色、光影、纹理单独改在复杂图案上看不出来：并进 D41，旧数据读取时归过去
    assert fields.merged_sources("D41") == ["D42", "D43", "D44"]
    assert {fields.canonical(f) for f in ("D42", "D43", "D44")} == {"D41"}
    assert fields.kinds("D42") == [] and fields.change_forms("D42") == []
    groups = fields.change_forms("D41")
    assert [g["aspect"] for g in groups] == [
        "画法大类",
        "线条与轮廓",
        "上色方式",
        "光影与体积",
        "纹理与细节",
        "造型处理",
    ]
    assert all(len(g["forms"]) >= 4 for g in groups)
    assert "画法大类" in fields.small_change("D41") and "画法大类" in fields.big_change(
        "D41"
    )
    assert fields.big_change("D37") == ""  # 只进小变化的字段没有大改写法
    assert fields.kinds("D37") == ["small"]
    assert fields.kinds("D05") == ["big"]
    # 底色、明暗、配件材质并进整体配色 / 服饰配件的小变化，只记录
    assert (
        fields.kinds("D36") == []
        and fields.kinds("D39") == []
        and fields.kinds("D24") == []
    )
    assert fields.kinds("D32") == [] and fields.kinds("D01") == []
    assert fields.kinds("D38") == []  # 停用字段
    # 槽位类型（0.5）：绘制技法不进小变化；小变化字段进小变化与三处组合
    assert fields.slot_kinds("D41") == ["big", "combo"]
    assert fields.slot_kinds("D37") == ["small", "combo"]
    assert fields.slot_kinds("D05") == ["big"]
    assert fields.slot_kinds("D36") == [] and fields.slot_kinds("D42") == []
    small_rule, big_rule = fields.tier_rule("small"), fields.tier_rule("big")
    assert "改两处" in small_rule and "小变化不改绘制技法" in small_rule
    assert "改三处" in big_rule and "换画法大类" in big_rule
    assert "layout_change" in big_rule


def test_every_small_field_says_how_to_make_it_visible() -> None:
    """小变化要一眼看得出：每个小变化字段都写了 small_change（单元里的全部成员一起改），只出大变化 / 只记录的没有。"""
    for fid in fields.variation_field_ids("small"):
        assert len(fields.small_change(fid)) >= 15, fid
    assert fields.small_change("D05") == "" and fields.small_change("D36") == ""
    assert "单元里的角色一起" in fields.small_change("D17")
    assert "整套配色" in fields.small_change("D37")
    assert "不只换底色" in fields.small_change("D37")
    rule = fields.tier_rule("small")
    assert "一眼看得出" in rule and "单元里的全部成员一起改" in rule
    assert "不做只动一个局部的微调" in rule


def test_field_tiers_follow_dimensions_and_retired_fields_point_somewhere() -> None:
    small_dims = set(fields.tier_dimensions("small"))
    big_dims = set(fields.tier_dimensions("big"))
    assert small_dims == {
        "03 元素角色",
        "04 主体外形",
        "05 表情动作",
        "06 服饰道具",
        "07 关系结构",
        "09 排列重复",
        "10 配色结构",
        "11 绘制技法",
        "13 文字定制",
    }
    assert big_dims == {"02 主题叙事", "08 整体构图", "11 绘制技法"}
    for f in fields.active_fields():
        if f["tier"] == "small":
            assert f["dimension"] in small_dims, f["field_id"]
        elif f["tier"] == "big":
            assert f["dimension"] in big_dims, f["field_id"]
        else:
            # 只记录的：需求语境 / 风格情绪 / 版型印制，排列重复里做门控的 D32，
            # 以及并进整体配色 / 服饰配件小变化的底色、明暗、配件材质
            assert f["dimension"] not in big_dims, f["field_id"]
        assert f.get("label"), f["field_id"]
    labels = [fields.label(fid) for fid in fields.variation_field_ids()]
    assert len(labels) == len(set(labels))
    retired = [
        f
        for f in reference("field_dictionary.json")["design_fields"]
        if f.get("status") == "retired"
    ]
    assert len(retired) == 20
    for f in retired:
        assert f.get("merged_into") or f.get("moved_to"), f["field_id"]
        if f.get("merged_into"):
            assert fields.is_active(f["merged_into"]), f["field_id"]
            assert fields.canonical(f["field_id"]) == f["merged_into"]
        else:
            assert fields.canonical(f["field_id"]) is None
    assert fields.label("D38") == "整体配色"
    assert {g["dimension"] for g in fields.gates()} == {"09 排列重复", "13 文字定制"}
    assert fields.gated_off({"layout_kind": "scene", "has_text": False}) == {
        "09 排列重复": "单幅场景没有重复花型，改排列等于改构图",
        "13 文字定制": "图上没有文字",
    }
    assert fields.gated_off({"layout_kind": "mixed", "has_text": True}) == {}


def test_build_rejects_missing_variables_and_numbers_images() -> None:
    with pytest.raises(prompts.PromptError):
        prompts.build(
            "P02",
            job_id="seed:S01",
            variables={},
            images={},
            result_schema=schemas.schema_text("P02"),
        )
    built = prompts.build(
        "P07",
        job_id="J0001",
        variables={
            "project_constraints": {},
            "schema_version": "0.1",
            "job_context": {},
            "variant_job": {},
            "family_spec": {},
            "reference_profile": {},
            "file_check_results": {},
            "ocr_results": {},
        },
        images={
            "candidate_image": [
                prompts.ImageInput(label="候选", data=b"x"),
                prompts.ImageInput(label="修复前", data=b"y"),
            ],
            "reference_image": [prompts.ImageInput(label="参考图", data=b"z")],
        },
        result_schema=schemas.schema_text("P07"),
    )
    text = built.user_content[0]["text"]
    assert "### candidate_image\n见附图：图1、图2" in text
    assert "### reference_image\n见附图：图3" in text
    labels = [part["text"] for part in built.user_content[1:] if part["type"] == "text"]
    assert labels == [
        "图1（candidate_image）：候选",
        "图2（candidate_image）：修复前",
        "图3（reference_image）：参考图",
    ]
    assert "{{" not in built.system and 'stage="P07"' in built.system
