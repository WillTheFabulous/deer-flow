"""测试夹具：临时数据目录 + 模拟 LiteLLM（chat / images / models），全程不连网、不花钱。

设了 PODFLOW_TEST_DATABASE_URL 时整套测试改用 Postgres：每个用例开始前清空该库的 public schema。
只能指向专门的测试库（如 pod_blanket_test），不要指向业务库。
"""

from __future__ import annotations

import base64
import gc
import io
import json
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
import yaml
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from podflow import cli, fields, http, llm, views  # noqa: E402
from podflow.context import open_ctx  # noqa: E402
from podflow.stages import job as jobmod  # noqa: E402

# 假拆解默认判定能变的字段：主题（02）、构图类型与视觉中心（08）三个大变化字段，没有小变化字段：
# 一轮 3 个单字段大改、每个出 9 张图（文生图 / 图生图 / 图生图 Edit）；小变化与三处组合的用例用 SMALL_VARIABLE
DEFAULT_VARIABLE = ("D05", "D27", "D28")
# 主体（03 元素角色）、表情（05）、整体配色（10 配色结构）三个小变化 + 绘制技法（11，只进单字段大改与三处组合）：
# 一轮小变化 3 个（三个小变化字段两两搭配）+ 单字段大改 1 个（绘制技法）+ 三处组合 4 个（四个字段挑三个）
SMALL_VARIABLE = ("D09", "D17", "D37", "D41")
DOMAIN_OPTIONS = {
    "D05": ["海洋伙伴", "冬季雪地"],
    "D09": ["小兔", "小鹿", "小狐狸"],
    "D17": ["一起开怀大笑", "一起困倦打盹"],
    "D27": ["环形构图", "边框构图"],
    "D28": ["左侧主角", "中心对称"],
    "D37": ["薄荷珊瑚", "极地冷色"],
    "D41": ["版画套色", "剪纸拼贴"],
}


def domains(field_ids: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """假系列规则的变化域：每个字段一条。"""
    return [
        {
            "field_id": fid,
            "options": list(DOMAIN_OPTIONS.get(fid, [])),
            "compatibility": [],
        }
        for fid in field_ids
    ]


def use_small(fake: FakeProvider) -> None:
    """切到有小变化的假拆解：主体、表情、整体配色三个小变化字段 + 绘制技法（只进单字段大改与三处组合）。"""
    fake.p02_variable = set(SMALL_VARIABLE)
    fake.p04_domains = domains(SMALL_VARIABLE)


FANOUT_MODELS = [
    "gemini-nano-banana-2.1",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
]
# 图生图另用 <模型>-edit 各出一张（image_generation.i2i_edit），每个任务 9 个槽位
EDIT_MODELS = [f"{m}-edit" for m in FANOUT_MODELS]
SLOTS_PER_JOB = len(FANOUT_MODELS) * 3


def make_png(
    seed: int,
    *,
    size: tuple[int, int] = (256, 320),
    noise: float = 0.0,
    noise_seed: int = 0,
    tweak: int = 0,
) -> bytes:
    """随机色块图。tweak = 重新上色的格子数：改几格得到「相似但不相同」的图。"""
    rng = np.random.default_rng(seed)
    grid = rng.integers(0, 255, (10, 8, 3), dtype=np.uint8)
    if tweak:
        trng = np.random.default_rng(10_000 + noise_seed)
        for _ in range(tweak):
            grid[trng.integers(0, 10), trng.integers(0, 8)] = trng.integers(0, 255, 3)
    arr = np.asarray(
        Image.fromarray(grid).resize(size, Image.Resampling.NEAREST), dtype=np.float64
    )
    if noise:
        arr = np.clip(
            arr + np.random.default_rng(noise_seed).normal(0, noise, arr.shape), 0, 255
        )
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def card_text(card: dict[str, Any]) -> str:
    """飞书卡片里全部可见文字（标题 + markdown + 备注）。"""
    parts = [card["header"]["title"]["content"]]
    for el in card["elements"]:
        if el["tag"] == "markdown":
            parts.append(el["content"])
        elif el["tag"] == "note":
            parts.extend(e["content"] for e in el["elements"])
    return "\n".join(parts)


def card_buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        b for el in card["elements"] if el["tag"] == "action" for b in el["actions"]
    ]


# 假拆解默认的对象：三只动物主体（组 G01）
DEFAULT_OBJECTS = [
    {
        "object_id": "E01",
        "role": "subject",
        "attributes": {"species": "长颈鹿"},
        "visual_function": "左侧高轮廓",
    },
    {
        "object_id": "E02",
        "role": "subject",
        "attributes": {"species": "小象"},
        "visual_function": "中间衔接",
    },
    {
        "object_id": "E03",
        "role": "subject",
        "attributes": {"species": "小熊"},
        "visual_function": "右侧暖色块",
    },
]


def input_block(payload: dict[str, Any], name: str) -> Any:
    """从发给模型的 user 消息里取出某个输入变量（### 名字 之后的 JSON 块）。"""
    content = payload["messages"][1]["content"]
    text = content[0]["text"] if isinstance(content, list) else content
    match = re.search(rf"### {name}\n(.*?)(?=\n\n### |\Z)", text, re.S)
    assert match, f"输入里没有 {name}"
    block = match.group(1)
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return block


def p05_task(
    slot_id: str,
    changes: list[tuple[str, str]],
    *,
    title: str | None = None,
    frm: str = "原样",
) -> dict[str, Any]:
    """P05 的一个任务：changes = [(字段, 改成的样子)]，按单元策划的槽位是 (字段, 样子, unit_id)。"""

    def item(change: tuple) -> dict[str, Any]:
        fid, to, *unit = change
        out = {"field_id": fid, "from": frm, "to": to}
        return {"unit_id": unit[0], **out} if unit else out

    return {
        "slot_id": slot_id,
        "title": title or f"{changes[0][1]}方向",
        "brief": [item(c) for c in changes],
        "keep": "三只陪伴的构图",
        "changed_fields": [item(c) for c in changes],
        "preserve": ["柔和画法"],
        "coupled_changes": [],
        "acceptance_criteria": [f"看得出{changes[0][1]}"],
        "hypothesis_ref": "H01",
        "difference_rationale": "一眼能看出改了哪里",
    }


def slot_changes(slot: dict[str, Any], tag: str) -> list[tuple]:
    """槽位里每处一条改动，改成「<短名><tag>」；按单元策划的槽位带上 unit_id（改成「<单元名><短名><tag>」）。"""
    return [
        (
            f["field_id"],
            f"{f['unit']['name']}{f['label']}{tag}",
            f["unit_id"],
        )
        if f.get("unit_id")
        else (f["field_id"], f"{f['label']}{tag}")
        for f in slot["fields"]
    ]


def default_p05_tasks(
    fake: FakeProvider, group: dict[str, Any]
) -> list[dict[str, Any]]:
    """每个槽位一个方向，槽位里的字段都改；同一个槽位第 n 次被策划时目标值带 n，避免和上一轮重复。

    单字段大改改成「<短名>方案n」；小变化、三处组合带槽位号（「主体S1-n」）：同一个字段在不同槽位里
    改成的样子不同，不会被当成做过的方向丢掉。
    """
    tasks = []
    for slot in group["slots"]:
        sid = slot["slot_id"]
        with fake.lock:
            fake.p05_seen[(group["kind"], sid)] += 1
            n = fake.p05_seen[(group["kind"], sid)]
        if group["kind"] == "big":
            changes = slot_changes(slot, f"方案{n}")
            tasks.append(p05_task(sid, changes, title=changes[0][1]))
        else:
            name = "组合" if group["kind"] == "combo" else "小变化"
            tasks.append(
                p05_task(
                    sid, slot_changes(slot, f"{sid}-{n}"), title=f"{name}{sid[1:]}-{n}"
                )
            )
    return tasks


def sse_response(model: str, content: str, usage: dict[str, Any]) -> httpx.Response:
    """按 OpenAI 流式格式回：内容拆成几段 delta，最后一段只带 usage。"""
    step = max(1, len(content) // 3)
    pieces = [content[i : i + step] for i in range(0, len(content), step)] or [""]
    chunks = [
        {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": p}}],
        }
        for p in pieces
    ]
    chunks.append(
        {"id": "chatcmpl-test", "model": model, "choices": [], "usage": usage}
    )
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(body + "data: [DONE]\n\n").encode("utf-8"),
    )


def envelope(
    stage: str, result: dict[str, Any], status: str = "complete"
) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "job_id": "model-should-not-decide",
        "stage": stage,
        "status": status,
        "result": result,
        "unknowns": [],
        "evidence_refs": [],
        "next_action": "交给下一阶段",
    }


def default_result(
    stage: str, payload: dict[str, Any], fake: FakeProvider
) -> dict[str, Any]:
    if stage == "P01":
        return {
            "seed_id": "X",
            "image_kind": "mockup",
            "design_region": {"bbox": [0.15, 0.17, 0.95, 0.87], "estimated": True},
            "excluded_regions": ["PRODUCT SIZE 标题", "尺寸线与尺寸文字"],
            "material_uncertainties": ["毛绒质感无法从图片确认"],
            "readability": "usable",
            "rights_status": "approved",
            "source_ref": "x",
        }
    if stage == "P02":
        items = []
        for fid in fields.active_field_ids():
            item: dict[str, Any] = {
                "field_id": fid,
                "value": fake.p02_values.get(fid, f"{fid} 的取值"),
                "evidence_type": "observed",
                "evidence_ref": "图1",
                "uncertainty": None,
            }
            if fields.tier(fid) in fields.VARIABLE_TIERS:
                item["variable"] = fid in fake.p02_variable
                item["variable_reason"] = (
                    f"{fields.label(fid)}可以换"
                    if item["variable"]
                    else "改了会挤动构图"
                )
            items.append(item)
        if fake.p02_first_incomplete and fake.stage_counts["P02"] == 1:
            items = items[:30]
        return {
            "analysis_id": "A",
            "seed_id": "X",
            "traits": dict(fake.p02_traits),
            "fields": items,
            "groups": [{"group_id": "G01", "members": ["E01", "E02", "E03"]}],
            "objects": [dict(o) for o in fake.p02_objects or DEFAULT_OBJECTS],
            "relations": [
                {
                    "from": "E01",
                    "type": "near",
                    "to": "E02",
                    "evidence_type": "observed",
                },
                {"from": "E01/E02/E03", "type": "supported_by", "to": "G01"},
            ],
        }
    if stage == "P03":
        return {
            "hypotheses": [
                {
                    "hypothesis_id": "H01",
                    "feature_refs": ["D13", "D23"],
                    "possible_effect": "陪伴感",
                    "evidence_refs": [],
                    "alternative_explanations": ["价格"],
                    "missing_evidence": ["评论"],
                    "test_axis": "并排 vs 依偎",
                    "status": "supported",
                }
            ]
        }
    if stage == "P04":
        return {
            "family_id": "F99",
            "analysis_id": "A",
            "positioning": "柔和儿童动物伙伴系列",
            "preserve": ["圆润幼态造型", "柔和晕染"],
            "variable_domains": [dict(d) for d in fake.p04_domains],
            "locked_fields": [dict(x) for x in fake.p04_locked],
            "coupled_rules": ["换主体需联动承托接触"],
            "exclude": ["攻击动作", "刺眼高对比"],
            "hypothesis_refs": ["H01"],
            "approval_status": "pending",
        }
    if stage == "P05":
        group = input_block(payload, "plan_group")
        with fake.lock:
            fake.p05_groups.append(group)
        tasks = None
        if fake.p05_tasks is not None:
            # 自定义：p05_tasks(这一组的 plan_group) 返回任务列表；返回 None 用默认
            tasks = fake.p05_tasks(group)
        if tasks is None:
            tasks = default_p05_tasks(fake, group)
        return {"family_id": "F99", "tasks": tasks, "skipped": [], "coverage_gaps": []}
    if stage == "P06":
        return {
            "variant_id": "V",
            "prompts": {
                family: f"[{family}] Flat original print artwork: three round baby animals sitting on a branch, warm off-white ground, soft watercolour, a few stars and clouds; no text, logos or product mockup."
                for family in fake.p06_families
            },
            "i2i_prompts": {
                family: f"[{family} i2i] Use the input image only as a reference for the soft watercolour palette; change the reference's bear to a baby fawn and redraw the layout; output a flat print artwork, no room, sofa or product photo."
                for family in fake.p06_i2i_families
            },
            "prompt_summary": "三只幼态动物坐在枝条上，米白底，水彩",
            "avoid_instructions": ["商品摆拍", "尺寸线"],
            "reference_asset_ids": ["seed:S01"],
            "edit_regions": [],
            "generation_parameters": {"steps": 50},
            "text_overlay": {"enabled": False, "exact_text": None, "layout": None},
            "checks": ["右侧主体为目标物种"],
            "output_level": "production_candidate",
        }
    if stage == "P07":
        decision = fake.qc_queue.pop(0) if fake.qc_queue else "pass"
        checks = [
            {
                "criterion": "目标变化落实",
                "verdict": "pass",
                "evidence_region": "右侧",
                "reason": "ok",
            }
        ]
        repair = []
        if decision == "pass_with_fail":
            decision = "pass"
            checks.append(
                {
                    "criterion": "肢体结构",
                    "verdict": "fail",
                    "evidence_region": "右下",
                    "reason": "多一只爪",
                }
            )
            repair = ["修正右下多出的爪子"]
        if decision == "repair":
            repair = ["修正右下多出的爪子"]
        if decision == "needs_review_systemic":
            decision = "needs_review"
            checks.append(
                {
                    "criterion": "文字、姓名、日期及标语排除",
                    "verdict": "unverified",
                    "evidence_region": None,
                    "reason": "OCR 不可用",
                }
            )
            checks.append(
                {
                    "criterion": "商标、既有角色特征与受保护元素排除",
                    "verdict": "unverified",
                    "evidence_region": None,
                    "reason": "无检索结果",
                }
            )
        if decision == "needs_review_visual":
            decision = "needs_review"
            checks.append(
                {
                    "criterion": "肢体与承托接触",
                    "verdict": "unverified",
                    "evidence_region": "右下",
                    "reason": "右下爪子与草丘交界看不清",
                }
            )
        return {
            "asset_id": "X",
            "decision": decision,
            "checks": checks,
            "repair_instructions": repair,
            "changed_fields_observed": ["D09"],
            "unintended_changes": [],
        }
    if stage == "P11":
        return {
            "technical_findings": ["首检通过率正常"],
            "human_preference_findings": [],
            "market_findings": [],
            "data_limitations": ["无市场数据"],
            "next_experiments": ["探索配色轴"],
            "proposed_family_changes": [],
            "requires_approval": False,
        }
    if stage == "P12":
        return {"description": "森林三伙伴水彩"}
    if stage == "P15":
        return dict(fake.p15_result)
    if stage == "P13":
        result = p13_result(input_block(payload, "analysis"))
        if fake.p13_units is not None:
            result["change_units"] = [dict(u) for u in fake.p13_units]
        return result
    if stage == "P14":
        units = input_block(payload, "swap_units")
        with fake.lock:
            fake.p14_inputs.append(units)
        if fake.p14_result is not None:
            # 自定义：p14_result(swap_units) 返回 result
            return fake.p14_result(units)
        return {
            "units": [
                {
                    "unit_id": u["unit_id"],
                    "candidates": [
                        {
                            "name": f"{u['name']}新主体{i}",
                            "en": f"new subject {i}",
                            "features": f"新主体{i}的特征",
                        }
                        for i in range(1, u["need"] + 1)
                    ],
                }
                for u in units
            ]
        }
    raise AssertionError(f"未处理的阶段 {stage}")


def p13_units(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """假可变单元：两个以上主体时一个组单元（大）+ 每个主体一个成员单元（中），只有一个主体时它是大单元；
    其余对象按 role 归成承托物（prop）或一组装饰（中）。"""
    subjects = [o for o in objects if o.get("role") == "subject"]
    units: list[dict[str, Any]] = []

    def add(**unit: Any) -> str:
        unit_id = f"U{len(units) + 1:02d}"
        units.append({"unit_id": unit_id, "parent": None, "not_applicable": [], **unit})
        return unit_id

    group = None
    if len(subjects) > 1:
        group = add(
            name="全部动物",
            kind="character",
            object_ids=[o["object_id"] for o in subjects],
            salience="large",
            en="all the baby animals",
        )
    for o in subjects:
        add(
            name=(o.get("attributes") or {}).get("species") or "动物",
            kind="character",
            object_ids=[o["object_id"]],
            parent=group,
            salience="medium" if group else "large",
            en=f"the baby animal {o['object_id'].lower()}",
        )
    props = [o["object_id"] for o in objects if o.get("role") == "prop"]
    if props:
        add(
            name="承托枝条",
            kind="prop",
            object_ids=props,
            salience="medium",
            en="the branch",
        )
    decor = [
        o["object_id"] for o in objects if o.get("role") not in ("subject", "prop")
    ]
    if decor:
        add(
            name="满屏星星",
            kind="decoration",
            object_ids=decor,
            salience="medium",
            en="all the little stars",
        )
    return units


def p13_result(analysis: dict[str, Any]) -> dict[str, Any]:
    """假画面描述：拆解里的主体每个一条，其余对象归进一条环境；可变单元见 p13_units。"""
    objects = analysis.get("objects") or []
    subjects = [o for o in objects if o.get("role") == "subject"]
    others = [o["object_id"] for o in objects if o.get("role") != "subject"]
    return {
        "summary": "三只圆润幼态动物坐在枝条上的柔和水彩",
        "style": {
            "genre": "儿童水彩绘本",
            "linework": "细软的暖灰轮廓线",
            "coloring": "湿画法晕染，边缘柔和",
            "lighting": "没有统一光源，用浅色晕染表现体积",
            "texture_detail": "纸纹轻微可见，主体细节适中",
            "shape_language": "大头小身、圆眼、圆润轮廓",
            "saturation_contrast": "低饱和、低对比",
        },
        "palette": {
            "background": "暖米白",
            "subjects": "浅黄、浅灰、浅棕",
            "accents": "浅粉腮红",
            "relationships": "暖色为主，面积均衡",
        },
        "subjects": [
            {
                "object_id": o["object_id"],
                "name": (o.get("attributes") or {}).get("species") or "动物",
                "appearance": "圆头圆身",
                "colors": "浅暖色",
                "face_expression": "微笑",
                "pose_action": "坐着",
                "position_scale": "画面中部，中等大小",
                "en": f"a soft watercolour baby animal {o['object_id'].lower()} with a round head, smiling, sitting",
            }
            for o in subjects
        ],
        "environment": [
            {
                "object_ids": others,
                "name": "枝条与装饰",
                "appearance": "细枝和小星星",
                "rendering": "和主体同样的水彩晕染",
                "en": "a thin watercolour branch with tiny stars",
            }
        ]
        if others
        else [],
        "change_units": p13_units(objects),
        "composition": "三只主体横向并排，四周留白",
        "mood": "安静、温柔的陪伴感",
        "text_elements": None,
        "harmony_rules": [
            "新背景同样用湿画法晕染和细软轮廓线",
            "保持低饱和、低对比，不加写实光照或雾气",
        ],
        "corrections": [],
        "prompt_en": {
            "style_anchor": "Soft children's-book watercolour with thin warm-grey outlines, gentle washes and low saturation; no realistic lighting.",
            "full_scene": "A soft children's-book watercolour illustration of three round baby animals sitting side by side on a thin branch, warm off-white ground, tiny stars, generous margins.",
        },
    }


class FakeProvider:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.stage_counts: Counter[str] = Counter()
        self.image_counts: Counter[str] = Counter()
        self.image_mode = "ok"
        self.image_modes_by_model: dict[str, str] = {}
        self.qc_queue: list[str] = []
        self.p02_first_incomplete = False
        # 假拆解的对象（None = DEFAULT_OBJECTS 三只动物）；假画面描述的可变单元（None = 按对象生成）
        self.p02_objects: list[dict[str, Any]] | None = None
        self.p13_units: list[dict[str, Any]] | None = None
        # 假拆解：判定能变的字段与参考图特征
        self.p02_variable: set[str] = set(DEFAULT_VARIABLE)
        self.p02_traits: dict[str, Any] = {"layout_kind": "scene", "has_text": False}
        # 假拆解里个别字段的取值（没写的用「Dxx 的取值」）
        self.p02_values: dict[str, Any] = {}
        # 假系列规则：变化域与锁定字段
        self.p04_domains: list[dict[str, Any]] = domains(DEFAULT_VARIABLE)
        self.p04_locked: list[dict[str, Any]] = []
        # P05 自定义任务：p05_tasks(plan_group) 返回这一组的任务列表；None = 每个槽位一个默认任务
        self.p05_tasks: Any = None
        self.p05_groups: list[dict[str, Any]] = []
        self.p05_seen: Counter[tuple[str, str]] = Counter()
        # P14 换主体清单：收到的 swap_units；自定义 p14_result(swap_units) 返回 result（None = 每个单元给 need 个）
        self.p14_inputs: list[list[dict[str, Any]]] = []
        self.p14_result: Any = None
        self.p06_families: tuple[str, ...] = ("gemini", "gpt")
        self.p06_i2i_families: tuple[str, ...] = ("gemini", "gpt")
        self._image_seq = 0
        self.overrides: dict[str, Any] = {}
        # 文本消息原文；卡片消息记录其中全部文字，便于统一断言
        self.feishu_messages: list[str] = []
        # 传到飞书的消息图片（卡片里放图用）：image_key → 上传的表单字段
        self.feishu_images: dict[str, dict[str, Any]] = {}
        self.feishu_cards: list[dict[str, Any]] = []
        self.feishu_reject_cards = False
        # 内存版 OSS：key → (内容, x-oss-meta-* 头)；oss_fail_puts > 0 时接下来的几次 PUT 返回 503
        self.oss_objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.oss_requests: list[tuple[str, str, dict[str, str]]] = []
        self.oss_fail_puts = 0
        # 内存版飞书多维表格：建的表与视图、协作者、上传的素材、每一行；
        # bitable_fail 里的 (方法, 路径片段) 返回业务错误，用来测失败与重试
        self.bitable_calls: list[tuple[str, str]] = []
        self.bitable_apps: list[dict[str, Any]] = []
        self.bitable_tables: dict[str, dict[str, Any]] = {}
        self.bitable_members: list[dict[str, Any]] = []
        self.bitable_files: dict[str, dict[str, str]] = {}
        self.bitable_records: dict[str, dict[str, Any]] = {}
        # 每条记录在哪张数据表（列记录时按表分）
        self.bitable_record_tables: dict[str, str] = {}
        self.bitable_fail: set[tuple[str, str]] = set()
        # 模拟人员字段写不进去（对方不在应用可见范围等）
        self.bitable_reject_person = False
        self._bitable_seq = 0
        # 回读记录（batch_get + automatic_fields）时的最后修改人：{record_id: open_id}
        self.bitable_modifiers: dict[str, str] = {}
        # 每张表的全部视图（含建表时的默认表格视图，带 view_id / property）；table["views"] 只记程序新建的视图
        self.bitable_views: dict[str, list[dict[str, Any]]] = {}
        # 和飞书一样：刚改掉的视图名还占着，紧接着建同名视图报 ViewNameDuplicated（{(表, 名字): 还要拒几次}）；
        # view_waits 记下程序为此等了几次、每次几秒
        self.bitable_view_reserved: dict[tuple[str, str], int] = {}
        self.view_waits: list[float] = []
        # Base v3 视图排序：{(表, 视图): sort_config}（2026-10-26 实测的写法：{"field": 列名, "desc": false}）；
        # sort_limited > 0 时接下来几次设排序报频率限制 800004135
        self.bitable_view_sorts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.sort_limited = 0
        # Base v3 可见列与画册封面（记在视图的 visible / cover 上）；visible_limited > 0 时接下来几次写可见列报
        # 频率限制 800004135
        self.visible_limited = 0
        # Base v3 块：每张数据表在哪本表格、在哪个文件夹（None = 根目录）；建的文件夹 {id: {id, name, app}}
        self.bitable_table_apps: dict[str, str] = {}
        self.bitable_parents: dict[str, str | None] = {}
        self.bitable_folders: dict[str, dict[str, Any]] = {}
        # 订阅云文档事件：订阅过的 (file_token, file_type)；subscribe_error = (业务码, msg) 时返回错误
        self.bitable_subscriptions: list[tuple[str, str | None]] = []
        self.subscribe_error: tuple[int, str] | None = None
        # 知识库：挪入任务。wiki_fail_code 非 0 时移动接口直接返回这个业务码；
        # wiki_processing_left > 0 时前几次查询任务结果还在处理中
        self.wiki_fail_code: int | None = None
        self.wiki_processing_left = 0
        self.wiki_tasks: dict[str, dict[str, str]] = {}
        self.wiki_nodes: dict[str, str] = {}
        # 知识库里新建 / 复制出来的多维表格节点（每周选款表）：{node_token: obj_token}、{node_token: 标题}
        self.wiki_node_objs: dict[str, str] = {}
        self.wiki_titles: dict[str, str] = {}
        # 节点挂在哪个节点下面（None = 知识库根目录）、什么类型（bitable / docx）
        self.wiki_parents: dict[str, str | None] = {}
        self.wiki_types: dict[str, str] = {}
        # 高级权限：{app: 开没开}；开着时的角色 {app: [{role_id, role_name, role_type, table_rule_map}]}
        self.bitable_advanced: dict[str, bool] = {}
        self.bitable_roles: dict[str, list[dict[str, Any]]] = {}
        # 整本复制是异步的：复制出来的表格前 copy_pending 次读本身报 1254036「Bitable is copying」
        self.copy_pending = 0
        self.bitable_copying: dict[str, int] = {}
        # 假 mooya 机器调用接口（出套图）：每个任务第 n 次查进度返回 mooya_script[n-1]（超出取最后一个）；
        # 出完给 mooya_scenes 张场景图 + 白底图，partial 少一张。mooya_fail = {"submit"|"status"|"image": HTTP 状态码}
        self.mooya_token = "test-mooya-token"
        # 每个令牌绑一个客户端和租户（和 mooya 的 SERVICE_API_CLIENTS 一样）：mooya_token 是老客户端（platform_internal），
        # mooya_tokens 是另加的；任务只有交它的租户查得到；
        # 货号 /codes 按租户 + 前缀续号（mooya_code_start[前缀] 是已经占到的号），同一个 ref 拿回同一个号；
        # 取图时带的 variant 记在 mooya_variants（任务, 图, variant）；export 只在字标租户贴字标（图换一张）
        self.mooya_tokens: dict[str, tuple[str, str]] = {
            "test-mooya-deerflow": ("pod-blanket-deerflow", "ai_temu_deerflow"),
        }
        self.mooya_badge_tenants = {"ai_temu", "ai_temu_deerflow"}
        self.mooya_codes: dict[tuple[str, str], dict[str, Any]] = {}
        self.mooya_code_last: dict[tuple[str, str], int] = {}
        self.mooya_code_start: dict[str, int] = {}
        self.mooya_variants: list[tuple[int, str, str | None]] = []
        self.mooya_jobs: dict[int, dict[str, Any]] = {}
        self.mooya_keys: dict[str, int] = {}
        self.mooya_calls: list[tuple[str, str]] = []
        self.mooya_script: list[str] = ["queued", "running", "done"]
        self.mooya_scenes = 3
        self.mooya_fail: dict[str, int] = {}
        self.mooya_presets = ["pod_blanket"]
        # 新建的任务 failed 时一张图都没有（否则 failed 也带图，按缺图算）
        self.mooya_no_images = False
        # 出完带交付清单（mooya 交付包的顺序与文件名）：场景图按 mooya_delivery_order（场景图下标，缺省原顺序），
        # 最后一张是尺码图 file-9001。关着时载荷里没有 delivery（老版本 mooya）
        self.mooya_delivery = False
        self.mooya_delivery_order: list[int] | None = None
        # 场景图的方案 id（按场景图下标；None = 0n_tpl）：上架图位校验看 04 / 05 / 08 / 07 在哪一位
        self.mooya_plan_ids: list[str] | None = None
        # P15 上架标题的假回答
        self.p15_result: dict[str, Any] = {
            "title": "Flannel Blanket Throw 280GSM Floral Print Cozy Throw for Couch Sofa Living Room Decor",
            "is_custom": False,
            "lead_type": "graphic",
            "holiday": None,
            "gift_led": False,
            "theme": "floral",
        }
        # 假妙手开放平台（miaoshou.test）：按 secret 验签；店铺、采集箱商品（{detail_id: 商品}）、每次调用（路径尾段, body）。
        # miaoshou_fail = {路径尾段: 错误码}（如 {"get_shop_list": "appNoPermission"}）；
        # miaoshou_timeout = {路径尾段: "before" | "after"}：请求处理之前 / 之后断开（after = 妙手已经做了）
        self.miaoshou_key = "ak_test"
        self.miaoshou_secret = "as_test_secret"
        self.miaoshou_calls: list[tuple[str, Any]] = []
        self.miaoshou_fail: dict[str, str] = {}
        self.miaoshou_timeout: dict[str, str] = {}
        self.miaoshou_shops: list[dict[str, Any]] = []
        self.miaoshou_items: dict[int, dict[str, Any]] = {}
        # 发布之后：auto = 下一次查采集箱就是 published；hold = 一直 notPublished（发布失败 / 还在审）
        self.miaoshou_publish = "auto"
        self.miaoshou_title_limit = 250
        # 店铺模式保存回成功、但品牌没存上（读回来还是原来的）
        self.miaoshou_brand_ignored = False
        self._miaoshou_seq = 9_000_000_000
        # 假 mooya 的白底图质检结果（2026-10-30 起任务里带 baseline）：None = 不带（老版本）；
        # 任务动作端点 /showcase/jobs/<id>/actions：mooya_actions 记每次 (任务, 动作)，mooya_actions_supported 关掉 = 老版本 404
        self.mooya_baseline: dict[str, Any] | None = None
        self.mooya_actions: list[tuple[int, str]] = []
        self.mooya_actions_supported = True
        # 飞书列记录带 automatic_fields 时的最后修改时间：{record_id: 毫秒}
        self.bitable_modified_at: dict[str, int] = {}
        # 假 Temu 官方接口（temu.test）：商品 {goods_id: 商品}，调用 (type, 参数)，temu_open_fail = {type: (errorCode, errorMsg)}，
        # 改标题（partial.update）后读回的标题：temu_open_apply_titles 为真时马上生效，否则还是旧标题（审核中）
        self.temu_open_goods: dict[int, dict[str, Any]] = {}
        self.temu_open_calls: list[tuple[str, dict[str, Any]]] = []
        self.temu_open_fail: dict[str, tuple[Any, str]] = {}
        self.temu_open_apply_titles = True
        self.temu_open_secret = "temu_secret"
        # 假 mooya 的 Temu 上架与四家店底账（/api/service/v1/temu/*，2026-10-30 统一到 mooya）：
        # 店铺 {租户: [店铺]}、上架单 {id: 载荷 + tenant / client}、轨迹、底账 {(租户, 货号): 一行}、
        # temu_problems = {ref: [原因]}（建单 409）、temu_calls 记每次 (方法, 路径, body)、img.test 上的图（人工款主图）
        self.temu_stores: dict[str, list[dict[str, Any]]] = {}
        self.temu_listings: dict[int, dict[str, Any]] = {}
        self.temu_events: list[dict[str, Any]] = []
        self.temu_listed: dict[tuple[str, str], dict[str, Any]] = {}
        self.temu_problems: dict[str, list[str]] = {}
        self.temu_calls: list[tuple[str, str, Any]] = []
        self._temu_clock = 0

    def _miaoshou(self, request: httpx.Request) -> httpx.Response:
        from podflow import miaoshou as ms

        path = request.url.path
        tail = path.rsplit("/", 1)[-1]
        text = request.content.decode("utf-8")
        body = json.loads(text) if text else {}

        def ok(data: Any) -> httpx.Response:
            return httpx.Response(
                200,
                json={"result": "success", "code": "success", "message": "success", "data": data},
            )

        def fail(code: str, message: str = "") -> httpx.Response:
            return httpx.Response(
                200, json={"result": "fail", "code": code, "message": message or code, "data": None}
            )

        with self.lock:
            self.miaoshou_calls.append((tail, body))
            expected = ms.sign(
                self.miaoshou_secret,
                path,
                request.headers.get("x-timestamp", ""),
                request.headers.get("x-app-key", ""),
                text,
            )
            if request.headers.get("x-app-key") != self.miaoshou_key:
                return fail("appNotFound")
            if request.headers.get("x-sign") != expected:
                return fail("signInvalid", "签名验证不通过")
            if tail in self.miaoshou_fail:
                return fail(self.miaoshou_fail[tail], "fake")
            if self.miaoshou_timeout.get(tail) == "before":
                raise httpx.ReadTimeout("模拟读超时", request=request)
            response = self._miaoshou_route(tail, body, ok, fail)
            if self.miaoshou_timeout.get(tail) == "after":
                self.miaoshou_timeout.pop(tail)
                raise httpx.ReadTimeout("模拟读超时（妙手已处理）", request=request)
            return response

    def _miaoshou_route(self, tail: str, body: dict[str, Any], ok: Any, fail: Any) -> httpx.Response:
        if tail == "get_shop_list":
            size = body.get("pageSize") or 100
            start = (int(body.get("pageNo") or 1) - 1) * size
            return ok({"shopList": self.miaoshou_shops[start : start + size]})
        if tail == "get_publish_site_options":
            return ok({"publishSiteOptions": [{"key": "US", "value": "美国站"}]})
        if tail == "get_item_options":
            return ok({"catIdAndProductNameMaxLengthMap": {"11901": self.miaoshou_title_limit}})
        if tail == "get_site_collect_item_info":
            item = self.miaoshou_items.get(int(body["detailId"]))
            if item is None:
                return fail("fail", "采集箱商品不存在")
            return ok({"editModel": "site", "claimToShopIds": [], "siteCollectItemInfo": json.loads(json.dumps(item["info"]))})
        if tail == "create_collect_box_item":
            self._miaoshou_seq += 1
            detail_id = self._miaoshou_seq
            info = body["siteCollectItemInfo"]
            self.miaoshou_items[detail_id] = {
                "info": info,
                "status": "notPublished",
                "published_by": None,
                "gmt_create": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            }
            return ok({"collectBoxDetailId": detail_id})
        if tail == "save_site_collect_item_info":
            item = self.miaoshou_items.get(int(body["detailId"]))
            if item is None:
                return fail("fail", "采集箱商品不存在")
            item["info"] = body["siteCollectItemInfo"]
            return ok(None)
        # 店铺模式（和真接口一样，2026-10-29 实测）：品牌 brandId 只在这里；读出来的 editModel 在外层是 shop、里面是空的
        if tail == "get_shop_collect_item_info":
            item = self.miaoshou_items.get(int(body["detailId"]))
            if item is None:
                return fail("fail", "采集箱商品不存在")
            shop = str(body.get("shopId"))
            info = json.loads(json.dumps(item["info"]))
            entry = next(
                (e for e in info.get("collectBoxDetailShopList") or [] if str(e.get("shopId")) == shop), None
            )
            if entry is None:
                return fail("fail", "这家店没有认领这个商品")
            shop_info = {k: v for k, v in info.items() if k not in ("collectBoxDetailShopList", "detailId")}
            shop_info.update(
                detailId=int(body["detailId"]),
                shopId=body["shopId"],
                editModel=None,
                itemNum=entry.get("itemNum"),
                brandId=(item.get("brands") or {}).get(shop),
            )
            return ok({"editModel": "shop", "claimToShopIds": [body["shopId"]], "shopCollectItemInfo": shop_info})
        if tail == "save_shop_collect_item_info":
            item = self.miaoshou_items.get(int(body.get("detailId") or 0))
            if item is None:
                return fail("fail", "采集箱商品不存在")
            info = body.get("shopCollectItemInfo") or {}
            # 真接口必填（Apifox）：外层 detailId / shopId，里面的 editModel 只能是 shop / site
            if body.get("shopId") in (None, "") or info.get("editModel") not in ("shop", "site"):
                return fail("paramError", "参数错误")
            if not info.get("title") or not info.get("skuMap"):
                return fail("paramError", "参数错误：店铺模式数据不全")
            if not self.miaoshou_brand_ignored:
                item.setdefault("brands", {})[str(body["shopId"])] = info.get("brandId")
            return ok(None)
        if tail == "search_collect_box_detail_list":
            wanted = (body.get("filter") or {}).get("status")
            rows = []
            for detail_id, item in sorted(self.miaoshou_items.items(), reverse=True):
                if item["status"] == "publishing" and self.miaoshou_publish == "auto":
                    item["status"] = "published"
                status = "notPublished" if item["status"] == "publishing" else item["status"]
                if wanted and status != wanted:
                    continue
                info = item["info"]
                # 和真接口一样（2026-10-29 实测）：列表里的 itemNum 是空的，货号只在详情的店铺那一条里；
                # gmtCreate 是北京时间
                rows.append(
                    {
                        "collectBoxDetailId": str(detail_id),
                        "itemNum": None,
                        "title": info.get("title"),
                        "gmtCreate": item.get("gmt_create") or "2026-10-23 16:00:00",
                        "collectBoxDetailShopList": [
                            {"shopId": str(s.get("shopId"))} for s in info.get("collectBoxDetailShopList") or []
                        ],
                    }
                )
            size = body.get("pageSize") or 500
            start = (int(body.get("pageNo") or 1) - 1) * size
            return ok({"detailList": rows[start : start + size]})
        if tail == "save_move_collect_task":
            for detail_id in body["detailIds"]:
                item = self.miaoshou_items.get(int(detail_id))
                if item is None:
                    return fail("fail", f"采集箱商品 {detail_id} 不存在")
                item["status"] = "publishing"
                item["published_by"] = body["shopIdAndSitesMap"]
            return ok(None)
        if tail == "delete_collect_box_detail":
            removed = [d for d in body["detailIds"] if self.miaoshou_items.pop(int(d), None)]
            return ok({"successNum": len(removed), "failNum": len(body["detailIds"]) - len(removed)})
        return fail("routeNotFound", tail)

    def _mooya_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        status = job["status"]
        images = []
        if status in ("done", "partial", "failed"):
            count = self.mooya_scenes - (1 if status != "done" else 0)
            if status == "failed" and job.get("no_images"):
                count = 0
            for i in range(count):
                images.append(
                    {
                        "id": f"scene-{i + 1:02d}",
                        "role": "scene",
                        "index": i + 1,
                        "title": f"场景 {i + 1}",
                        "planId": self.mooya_plan_ids[i]
                        if self.mooya_plan_ids and i < len(self.mooya_plan_ids)
                        else f"0{i + 1}_tpl",
                        "vibe": "accent" if i % 2 == 0 else "immersive",
                        "shotType": "",
                        "rev": job["revs"].get(f"scene-{i + 1:02d}", "r1"),
                    }
                )
            if count:
                images.append(
                    {
                        "id": "baseline",
                        "role": "baseline",
                        "index": 0,
                        "title": "白底图",
                        "planId": "",
                        "vibe": "",
                        "shotType": "",
                        "rev": job["revs"].get("baseline", "r1"),
                    }
                )
        payload = {
            "jobId": job["id"],
            "taskId": job["id"] * 10,
            "status": status,
            "preset": job["meta"].get("preset"),
            "clientRef": job["meta"].get("client_ref"),
            "currentStep": "showcase_render" if status == "running" else None,
            "currentStepLabel": {
                "running": "场景出图",
                "waiting_human": "白底图等人工确认",
            }.get(status),
            "render": {
                "expected": self.mooya_scenes,
                "actual": sum(1 for i in images if i["role"] == "scene"),
                "missing": [self.mooya_scenes - 1] if status == "partial" else [],
            },
            "retrying": False,
            "retryCount": 0,
            "error": "上游失败" if status in ("partial", "failed") else None,
            "errorAttribution": None,
            "images": images,
        }
        if self.mooya_delivery:
            payload["delivery"] = self._mooya_delivery(job, images)
        if self.mooya_baseline is not None:
            payload["baseline"] = dict(self.mooya_baseline)
        return payload

    def _temu_open(self, request: httpx.Request) -> httpx.Response:
        """假 Temu Partner 接口：按 temu_open.sign 验签；商品列表按 searchText / goodsIdList 筛，部分编辑改标题。"""
        from podflow import temu_open as to

        body = json.loads(request.content)
        with self.lock:
            self.temu_open_calls.append((body.get("type"), body))
            if body.get("sign") != to.sign(self.temu_open_secret, body):
                return httpx.Response(200, json={"success": False, "errorCode": 7000016, "errorMsg": "sign invalid"})
            if body.get("type") in self.temu_open_fail:
                code, msg = self.temu_open_fail[body["type"]]
                return httpx.Response(200, json={"success": False, "errorCode": code, "errorMsg": msg})
            if body["type"] == "bg.local.goods.list.query":
                items = list(self.temu_open_goods.values())
                if body.get("goodsIdList"):
                    wanted = {int(g) for g in body["goodsIdList"]}
                    items = [g for g in items if int(g["goodsId"]) in wanted]
                elif body.get("searchText"):
                    items = [g for g in items if body["searchText"][:20] in g["goodsName"]]
                return httpx.Response(200, json={"success": True, "result": {"total": len(items), "goodsList": items}})
            if body["type"] == "bg.local.goods.partial.update":
                goods = self.temu_open_goods.get(int(body["goodsId"]))
                if goods is None:
                    return httpx.Response(200, json={"success": False, "errorCode": 150010001, "errorMsg": "goods not found"})
                if self.temu_open_apply_titles:
                    goods["goodsName"] = body["goodsBasic"]["goodsName"]
                return httpx.Response(200, json={"success": True, "result": {"modifyId": f"M{body['goodsId']}"}})
        return httpx.Response(200, json={"success": False, "errorCode": 404, "errorMsg": "unknown type"})

    def _mooya_delivery(
        self, job: dict[str, Any], images: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        scenes = [i for i in images if i["role"] == "scene"]
        if not scenes:
            return None
        order = self.mooya_delivery_order or list(range(len(scenes)))
        files = [
            {
                "imageId": scenes[i]["id"],
                "role": "scene",
                "title": scenes[i]["title"],
                "rev": scenes[i]["rev"],
            }
            for i in order
            if i < len(scenes)
        ]
        files.append(
            {
                "imageId": "file-9001",
                "role": "insert",
                "title": "毯子尺码对照图",
                "rev": job["revs"].get("file-9001", "r1"),
            }
        )
        for seq, f in enumerate(files, 1):
            f.update(seq=seq, fileName=f"{seq:03d}.png")
        return {
            "packageId": job["id"] + 5000,
            "name": job["meta"].get("client_ref"),
            "files": files,
        }

    def _mooya_codes(self, request: httpx.Request, tenant: str) -> httpx.Response:
        """货号：POST {prefix, ref} 领号（同一个 ref 拿回原来的号，201 新领 / 200 已有）；GET ?prefix= 看下一个号。
        调用方已持有 self.lock。"""
        if self.mooya_fail.get("codes"):
            return httpx.Response(self.mooya_fail["codes"], json={"error": "fake"})
        if request.method == "GET":
            prefix = request.url.params.get("prefix") or ""
            last = self.mooya_code_last.get(
                (tenant, prefix.upper()), self.mooya_code_start.get(prefix, 0)
            )
            return httpx.Response(
                200, json={"prefix": prefix, "next": f"{prefix}{last + 1:04d}"}
            )
        body = json.loads(request.content)
        prefix, ref = body["prefix"], body["ref"]
        old = self.mooya_codes.get((tenant, ref))
        if old:
            return httpx.Response(200, json={**old, "created": False})
        key = (tenant, prefix.upper())
        number = self.mooya_code_last.get(key, self.mooya_code_start.get(prefix, 0)) + 1
        self.mooya_code_last[key] = number
        item = {"code": f"{prefix}{number:04d}", "prefix": prefix, "number": number, "ref": ref}
        self.mooya_codes[(tenant, ref)] = item
        return httpx.Response(201, json={**item, "created": True})

    # ---------------------------------------------------------------- 假 mooya：Temu 上架与底账

    def temu_tick(self) -> str:
        """假 mooya 的 updated_at：每改一次往后一秒（底账增量拉按它翻页）。"""
        self._temu_clock += 1
        return (datetime(2026, 10, 30, 12, 0, 0) + timedelta(seconds=self._temu_clock)).isoformat() + "Z"

    def temu_listing(self, listing_id: int, **changes: Any) -> dict[str, Any]:
        """测试里改一条上架单（状态、妙手 ID、标题…），记一条轨迹。"""
        item = self.temu_listings[listing_id]
        before = item.get("state")
        item.update(changes)
        item["updatedAt"] = self.temu_tick()
        if "state" in changes and changes["state"] != before:
            self._temu_event(item, frm=before, to=changes["state"], stage="publish" if changes["state"] == "published" else "listing")
        return item

    def _temu_event(self, item: dict[str, Any], *, frm: Any, to: Any, stage: str, actor: str | None = None) -> None:
        self.temu_events.append(
            {
                "id": len(self.temu_events) + 1,
                "at": item.get("updatedAt"),
                "stage": stage,
                "from": frm,
                "to": to,
                "actor": actor or "system:temu-listing",
                "source": "engine",
                "reason": None,
                "payload": None,
                "listingId": item["id"],
                "ref": item.get("ref"),
                "code": item.get("code"),
                "store": item["store"]["key"],
                "client": item.get("client"),
            }
        )

    def temu_add_listed(self, tenant: str, code: str, store: dict[str, Any], **values: Any) -> dict[str, Any]:
        """往假 mooya 底账里放一行（1店 / 2店的、或 mooya 自己上架记的）。"""
        key = (tenant, code)
        old = self.temu_listed.get(key) or {}
        item = {
            "id": old.get("id") or (len(self.temu_listed) + 1),
            "code": code,
            "tenant": tenant,
            "store": store,
            "title": values.pop("title", f"{code} Flannel Blanket 280GSM"),
            "detailId": values.pop("detailId", None),
            "listedAt": values.pop("listedAt", "2026-10-30T05:00:00Z"),
            "source": values.pop("source", "engine"),
            "listingId": values.pop("listingId", None),
            "packageId": None,
            "snapshot": values.pop(
                "snapshot",
                {"label": "", "task": "", "store": store["column"], "prices": [], "images": [{"n": 1}], "main_kind": "其他"},
            ),
            "mainImage": values.pop("mainImage", {"url": f"http://img.test/{code}.jpg"}),
            "package": values.pop("package", None),
            "human": {
                "temu_status": None,
                "temu_product_id": None,
                "product_url": None,
                "sales_total": None,
                "sales_7d": None,
                "revenue_total": None,
                "data_updated_on": None,
                "note": None,
                **values.pop("human", {}),
            },
            "humanUpdatedAt": None,
            "humanUpdatedBy": None,
            "titleSync": values.pop("titleSync", None),
            "temuGoods": None,
            **values,
        }
        item["updatedAt"] = self.temu_tick()
        self.temu_listed[key] = item
        return item

    def _mooya_temu(
        self, request: httpx.Request, method: str, path: str, client: str, tenant: str
    ) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.temu_calls.append((method, path, body or dict(request.url.params)))
        params = request.url.params
        if path == "/temu/stores":
            return httpx.Response(200, json={"tenant": tenant, "stores": self.temu_stores.get(tenant, [])})
        if path == "/temu/listings" and method == "POST":
            ref = body["ref"]
            for item in self.temu_listings.values():
                if item["tenant"] == tenant and item["client"] == client and item["ref"] == ref and item["state"] != "cancelled":
                    return httpx.Response(200, json={"listing": item, "created": False})
            if ref in self.temu_problems:
                return httpx.Response(409, json={"error": "图位对不上", "problems": self.temu_problems[ref]})
            store = next((s for s in self.temu_stores.get(tenant, []) if s["key"] == body["store"]), None)
            if store is None or not store.get("enabled", True):
                return httpx.Response(409, json={"error": "店铺停用着", "problems": ["店铺停用着"]})
            listing_id = 700 + len(self.temu_listings) + 1
            item = {
                "id": listing_id,
                "ref": ref,
                "client": client,
                "tenant": tenant,
                "code": body["code"],
                "store": {"key": store["key"], "label": store["label"], "column": store["column"]},
                "state": "queued",
                "stateLabel": "排队中",
                "held": bool(body.get("held")),
                "jobId": body["job_id"],
                "detailId": None,
                "title": body.get("title") or "",
                "titleMeta": body.get("title_meta") or {},
                "titleSource": body.get("title_source") or None,
                "titleDirty": False,
                "brandNote": None,
                "waitReason": None,
                "lastError": "",
                "errorKind": None,
                "images": [],
                "publishedAt": None,
                "body": body,
                "actions": {"cancel": True, "hold": True, "release": False, "retry": False, "editTitle": True},
            }
            item["updatedAt"] = self.temu_tick()
            self.temu_listings[listing_id] = item
            self._temu_event(item, frm=None, to="queued", stage="listing", actor=body.get("actor"))
            return httpx.Response(201, json={"listing": item, "created": True})
        if path == "/temu/listings" and method == "GET":
            ids = {int(i) for i in (params.get("ids") or "").split(",") if i}
            refs = {r for r in (params.get("refs") or "").split(",") if r}
            found = [
                i for i in self.temu_listings.values()
                if i["tenant"] == tenant and i["client"] == client and (i["id"] in ids or i["ref"] in refs)
            ]
            return httpx.Response(200, json={"listings": found})
        if path == "/temu/listings/import" and method == "POST":
            results = []
            for entry in body["items"]:
                if entry.get("state") in ("chosen", "queued"):
                    results.append({"ref": entry["ref"], "ok": False, "error": "不用搬"})
                    continue
                existing = next(
                    (i for i in self.temu_listings.values() if i["tenant"] == tenant and i["ref"] == entry["ref"]), None
                )
                state = {"listed": "published", "creating": "create_unknown"}.get(entry["state"], entry["state"])
                if existing is None:
                    listing_id = 700 + len(self.temu_listings) + 1
                    store = next(s for s in self.temu_stores.get(tenant, []) if s["key"] == entry["store"])
                    existing = {
                        "id": listing_id, "ref": entry["ref"], "client": client, "tenant": tenant, "code": entry["code"],
                        "store": {"key": store["key"], "label": store["label"], "column": store["column"]},
                        "state": state, "held": bool(entry.get("held")), "detailId": entry.get("detail_id"),
                        "title": entry.get("title") or "", "titleDirty": False, "lastError": "", "imported": entry,
                        "actions": {"cancel": False, "hold": state == "in_box", "release": False, "retry": False},
                    }
                    existing["updatedAt"] = self.temu_tick()
                    self.temu_listings[listing_id] = existing
                    created = True
                else:
                    created = False
                if entry.get("listed"):
                    store = existing["store"]
                    self.temu_add_listed(
                        tenant, entry["code"], {**store, "name": ""}, source="deerflow", listingId=existing["id"],
                        detailId=entry.get("detail_id"), listedAt=entry["listed"].get("listed_at"),
                        snapshot={**(entry["listed"].get("snapshot") or {}), "origin": {"kind": "deerflow", **(entry.get("labels") or {})}},
                        mainImage={"path": "/x"}, human={k: v for k, v in (entry["listed"].get("human") or {}).items() if v is not None},
                    )
                results.append({"ref": entry["ref"], "ok": True, "listingId": existing["id"], "state": existing["state"], "created": created})
            return httpx.Response(200, json={"results": results})
        if path == "/temu/events":
            after = int(params.get("after") or 0)
            limit = int(params.get("limit") or 500)
            events = [e for e in self.temu_events if e["id"] > after and e.get("client") == client][:limit]
            return httpx.Response(200, json={"events": events, "last": events[-1]["id"] if events else after})
        match = re.fullmatch(r"/temu/listings/(\d+)(/actions)?", path)
        if match:
            item = self.temu_listings.get(int(match.group(1)))
            if item is None or item["tenant"] != tenant:
                return httpx.Response(404, json={"error": "上架单不存在"})
            if not match.group(2):
                return httpx.Response(200, json={"listing": {**item, "events": []}})
            action = body.get("action")
            allowed = item.get("actions") or {}
            if action in ("hold", "release", "cancel", "retry") and not allowed.get(action):
                return httpx.Response(409, json={"error": f"现在 {item['state']}，{action} 做不了"})
            if action == "hold":
                self.temu_listing(item["id"], held=True, actions={**allowed, "hold": False, "release": True})
            elif action == "release":
                self.temu_listing(item["id"], held=False, actions={**allowed, "hold": True, "release": False})
            elif action == "cancel":
                self.temu_listing(item["id"], state="cancelled")
            elif action == "retry":
                self.temu_listing(item["id"], state="titled", lastError="", errorKind=None,
                                  actions={**allowed, "retry": False})
            elif action == "title":
                if "280GSM" not in (body.get("title") or ""):
                    return httpx.Response(409, json={"error": "标题不合规：缺 280GSM", "problems": ["标题不合规：缺 280GSM"]})
                self.temu_listing(item["id"], title=body["title"], titleSource="human",
                                  titleDirty=item["state"] == "in_box")
            else:
                return httpx.Response(400, json={"error": "不认识的动作"})
            return httpx.Response(200, json={"listing": self.temu_listings[item["id"]], "action": action})
        if path == "/temu/listed" and method == "GET":
            since = params.get("changed_since")
            after_id = int(params.get("after_id") or 0)
            limit = int(params.get("limit") or 200)
            rows = sorted(
                (i for (t, _c), i in self.temu_listed.items() if t == tenant),
                key=lambda i: (i["updatedAt"], i["id"]),
            )
            if since:
                rows = [i for i in rows if (i["updatedAt"], i["id"]) > (since, after_id)]
            page = rows[:limit]
            nxt = {"changed_since": page[-1]["updatedAt"], "after_id": page[-1]["id"]} if len(page) == limit else None
            return httpx.Response(200, json={"items": page, "next": nxt})
        match = re.fullmatch(r"/temu/listed/(\d+)/image", path)
        if match:
            item = next((i for i in self.temu_listed.values() if i["id"] == int(match.group(1))), None)
            if item is not None and "gone" in str((item.get("mainImage") or {}).get("path") or ""):
                return httpx.Response(404, json={"error": "这一款的主图不在 mooya"})
            return httpx.Response(200, content=make_png(int(match.group(1)), size=(64, 64)))
        match = re.fullmatch(r"/temu/listed/([^/]+)/(human|title)", path)
        if match:
            item = self.temu_listed.get((tenant, match.group(1)))
            if item is None:
                return httpx.Response(404, json={"error": "底账里没有"})
            if match.group(2) == "human":
                changed = [k for k, v in body["fields"].items() if item["human"].get(k) != v]
                item["human"].update(body["fields"])
                item["humanUpdatedBy"] = body.get("editor")
                item["updatedAt"] = self.temu_tick()
                return httpx.Response(200, json={"changed": changed, "item": item})
            text = body["title"]
            if "280GSM" not in text:
                item["titleSync"] = {"state": "invalid", "text": f"{item['store']['label']}：不合规，用的还是原标题：缺 280GSM"}
                item["updatedAt"] = self.temu_tick()
                return httpx.Response(409, json={"state": "invalid", "problems": ["缺 280GSM"], "item": item})
            item["title"] = text
            item["titleSync"] = {
                "state": "waiting_temu", "target": "temu", "title": text,
                "text": f"{item['store']['label']}：已上架：Temu 接口还没接上，要人在 Temu 后台改",
            }
            item["updatedAt"] = self.temu_tick()
            return httpx.Response(200, json={"state": "waiting_temu", "item": item})
        return httpx.Response(404, json={"error": f"没有 {path}"})

    def _mooya(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/service/v1")
        method = request.method
        with self.lock:
            self.mooya_calls.append((method, path))
            token = (request.headers.get("authorization") or "").removeprefix("Bearer ")
            tokens = {
                self.mooya_token: ("pod-blanket", "platform_internal"),
                **self.mooya_tokens,
            }
            if token not in tokens:
                return httpx.Response(401, json={"error": "服务令牌无效"})
            client_name, tenant = tokens[token]
            if path == "/health":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "api": "v1",
                        "client": client_name,
                        "tenant": tenant,
                        "presets": [{"name": p} for p in self.mooya_presets],
                    },
                )
            if path == "/codes":
                return self._mooya_codes(request, tenant)
            if path.startswith("/temu/"):
                return self._mooya_temu(request, method, path, client_name, tenant)
            if method == "POST" and path == "/showcase/jobs":
                if self.mooya_fail.get("submit"):
                    return httpx.Response(
                        self.mooya_fail["submit"], json={"error": "fake"}
                    )
                meta = json.loads(
                    re.search(rb'name="meta"\r\n\r\n(.*?)\r\n--', request.content, re.S)
                    .group(1)
                    .decode()
                )
                assert b'name="image"' in request.content
                key = meta.get("idempotency_key") or ""
                if key and key in self.mooya_keys:
                    job = self.mooya_jobs[self.mooya_keys[key]]
                    return httpx.Response(
                        200, json={"job": self._mooya_payload(job), "created": False}
                    )
                job_id = 300 + len(self.mooya_jobs) + 1
                job = {
                    "id": job_id,
                    "status": "queued",
                    "meta": meta,
                    "polls": 0,
                    "revs": {},
                    "image_bytes": len(request.content),
                    "no_images": self.mooya_no_images,
                    "tenant": tenant,
                }
                self.mooya_jobs[job_id] = job
                if key:
                    self.mooya_keys[key] = job_id
                return httpx.Response(
                    201, json={"job": self._mooya_payload(job), "created": True}
                )
            match = re.fullmatch(
                r"/showcase/jobs/(\d+)(/images/([\w-]+)|/cancel|/actions)?", path
            )
            if (
                not match
                or int(match.group(1)) not in self.mooya_jobs
                or self.mooya_jobs[int(match.group(1))].get("tenant", tenant) != tenant
            ):
                return httpx.Response(404, json={"error": "任务不存在"})
            job = self.mooya_jobs[int(match.group(1))]
            if match.group(2) == "/cancel":
                job["status"] = "cancelled"
                return httpx.Response(200, json={"job": self._mooya_payload(job)})
            if match.group(2) == "/actions":
                if not self.mooya_actions_supported:
                    return httpx.Response(404, text="<html>Not Found</html>")
                action = json.loads(request.content).get("action")
                self.mooya_actions.append((job["id"], action))
                if action not in ("regenerate_missing", "resume"):
                    return httpx.Response(400, json={"error": "不认识的动作"})
                # 补完缺图 / 接着跑：之后先查到 running，再查到出完
                job["script"] = ["running", "done"]
                job["polls"] = 0
                job["status"] = "running"
                return httpx.Response(200, json={"job": self._mooya_payload(job), "action": action, "started": True})
            if match.group(3):
                if self.mooya_fail.get("image"):
                    return httpx.Response(
                        self.mooya_fail["image"], json={"error": "fake"}
                    )
                image_id = match.group(3)
                variant = request.url.params.get("variant")
                self.mooya_variants.append((job["id"], image_id, variant))
                if variant not in (None, "export", "raw"):
                    return httpx.Response(400, json={"error": "variant 只能是 export / raw"})
                rev = job["revs"].get(image_id, "r1")
                if image_id.startswith("file-"):
                    if not self.mooya_delivery:
                        return httpx.Response(
                            404, json={"error": f"没有这张图: {image_id}"}
                        )
                    seed = job["id"] * 100 + 50
                else:
                    seed = job["id"] * 100 + (
                        0 if image_id == "baseline" else int(image_id[-2:])
                    )
                if variant == "export":
                    delivery = self._mooya_delivery(
                        job, self._mooya_payload(job)["images"]
                    )
                    if not delivery or image_id not in {
                        f["imageId"] for f in delivery["files"]
                    }:
                        return httpx.Response(
                            400, json={"error": "export 只取交付清单里的图"}
                        )
                    if tenant in self.mooya_badge_tenants:
                        seed += 13
                return httpx.Response(
                    200,
                    content=make_png(seed + (7 if rev != "r1" else 0), size=(64, 64)),
                    headers={"content-type": "image/png", "x-image-rev": rev},
                )
            if self.mooya_fail.get("status"):
                return httpx.Response(self.mooya_fail["status"], json={"error": "fake"})
            if job["status"] not in ("cancelled",) and not job.get("frozen"):
                job["polls"] += 1
                script = job.get("script") or self.mooya_script
                job["status"] = script[min(job["polls"], len(script)) - 1]
            return httpx.Response(200, json={"job": self._mooya_payload(job)})

    def _wiki(
        self, method: str, api: str, request: httpx.Request
    ) -> httpx.Response | None:
        """知识库挪入。调用方已持有 self.lock。"""

        def ok(data: dict[str, Any]) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": data})

        if method == "POST" and api.endswith("/nodes/move_docs_to_wiki"):
            if self.wiki_fail_code:
                return httpx.Response(
                    200,
                    json={
                        "code": self.wiki_fail_code,
                        "msg": "wiki space permission denied",
                    },
                )
            body = json.loads(request.content)
            self._bitable_seq += 1
            seq = self._bitable_seq
            space_id = api.split("/")[4]
            node = f"wik{seq}"
            task_id = f"task{seq}"
            self.wiki_nodes[body["obj_token"]] = node
            self.wiki_tasks[task_id] = {
                "node": node,
                "space_id": space_id,
                "obj": body["obj_token"],
            }
            return ok({"task_id": task_id})
        if method == "GET" and api.startswith("/wiki/v2/tasks/"):
            task_id = api.split("/")[4]
            task = self.wiki_tasks[task_id]
            if self.wiki_processing_left > 0:
                self.wiki_processing_left -= 1
                return ok(
                    {
                        "task": {
                            "task_id": task_id,
                            "move_result": [
                                {"node": {}, "status": 1, "status_msg": "processing"}
                            ],
                        }
                    }
                )
            return ok(
                {
                    "task": {
                        "task_id": task_id,
                        "move_result": [
                            {
                                "node": {
                                    "node_token": task["node"],
                                    "obj_token": task["obj"],
                                    "space_id": task["space_id"],
                                    "obj_type": "bitable",
                                },
                                "status": 0,
                                "status_msg": "success",
                            }
                        ],
                    }
                }
            )
        created = re.fullmatch(r"/wiki/v2/spaces/(\w+)/nodes", api)
        if method == "POST" and created:
            body = json.loads(request.content)
            if body.get("obj_type") == "docx":
                node = self._wiki_new_doc(created.group(1), body)
            else:
                node = self._wiki_new_base(created.group(1), body)
            self.wiki_parents[node["node_token"]] = body.get("parent_node_token")
            return ok({"node": node})
        copied = re.fullmatch(r"/wiki/v2/spaces/(\w+)/nodes/(\w+)/copy", api)
        if method == "POST" and copied:
            source = self.wiki_node_objs.get(copied.group(2))
            if not source:
                return httpx.Response(200, json={"code": 131005, "msg": "node not found"})
            body = json.loads(request.content)
            node = self._wiki_copy_base(copied.group(1), source, body)
            self.wiki_parents[node["node_token"]] = body.get("target_parent_token")
            return ok({"node": node})
        if method == "GET" and api == "/wiki/v2/spaces/get_node":
            obj = request.url.params.get("token", "")
            node = self.wiki_nodes.get(obj)
            if not node:
                return httpx.Response(
                    200, json={"code": 131005, "msg": "node not found"}
                )
            return ok(
                {"node": {"node_token": node, "obj_token": obj, "obj_type": "bitable"}}
            )
        return None

    def _wiki_node(self, space_id: str, app: str, title: str, obj_type: str = "bitable") -> dict[str, Any]:
        node = f"wikN{self._bitable_seq}"
        self.wiki_nodes[app] = node
        self.wiki_node_objs[node] = app
        self.wiki_titles[node] = title
        self.wiki_types[node] = obj_type
        return {
            "node_token": node,
            "obj_token": app,
            "obj_type": obj_type,
            "space_id": space_id,
            "title": title,
        }

    def _wiki_new_doc(self, space_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """知识库里新建一篇文档（只当目录用，下面挂别的节点）。调用方已持有 self.lock。"""
        self._bitable_seq += 1
        return self._wiki_node(space_id, f"doxcnW{self._bitable_seq}", body.get("title") or "", "docx")

    def _wiki_new_base(self, space_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """知识库里新建一本多维表格：和飞书一样自带一张空的数据表。调用方已持有 self.lock。"""
        self._bitable_seq += 1
        seq = self._bitable_seq
        app = f"bascnW{seq}"
        table_id = f"tbl{seq}"
        self.bitable_tables[table_id] = {
            "name": "数据表",
            "views": [],
            "fields": [{"field_name": "文本", "type": 1}],
        }
        self.bitable_table_apps[table_id] = app
        self.bitable_parents[table_id] = None
        self.bitable_views[table_id] = [
            {"view_id": f"vew{seq}d", "view_name": "表格", "view_type": "grid"}
        ]
        return self._wiki_node(space_id, app, body.get("title") or "")

    def _wiki_copy_base(
        self, space_id: str, source: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """整本复制（知识库节点复制）：数据表、文件夹换新 id，列、视图（也换新 id，设置连封面效果都跟着走）照搬，
        记录不复制；高级权限和角色跟着走。调用方已持有 self.lock。"""
        self._bitable_seq += 1
        seq = self._bitable_seq
        app = f"bascnW{seq}"
        folders = {
            fid: f"bfl{seq}x{n}"
            for n, (fid, f) in enumerate(self.bitable_folders.items())
            if f["app"] == source
        }
        for fid, new_id in folders.items():
            old = self.bitable_folders[fid]
            self.bitable_folders[new_id] = {
                **old,
                "id": new_id,
                "app": app,
                "parent_id": folders.get(old.get("parent_id")),
            }
        tables = [t for t in self.bitable_tables if self.bitable_table_apps.get(t) == source]
        for n, table_id in enumerate(tables):
            new_table = f"tbl{seq}x{n}"
            self.bitable_tables[new_table] = json.loads(
                json.dumps(self.bitable_tables[table_id])
            )
            self.bitable_table_apps[new_table] = app
            self.bitable_parents[new_table] = folders.get(self.bitable_parents.get(table_id))
            copied_views = json.loads(json.dumps(self.bitable_views.get(table_id) or []))
            for k, view in enumerate(copied_views):
                old_view = view["view_id"]
                view["view_id"] = f"vew{seq}x{n}v{k}"
                sort = self.bitable_view_sorts.get((table_id, old_view))
                if sort is not None:
                    self.bitable_view_sorts[(new_table, view["view_id"])] = json.loads(
                        json.dumps(sort)
                    )
            self.bitable_views[new_table] = copied_views
        self.bitable_advanced[app] = self.bitable_advanced.get(source, False)
        self.bitable_copying[app] = self.copy_pending
        if source in self.bitable_roles:
            self.bitable_roles[app] = json.loads(json.dumps(self.bitable_roles[source]))
        return self._wiki_node(space_id, app, body.get("title") or "")

    def _advanced_api(
        self, method: str, api: str, request: httpx.Request, seq: int
    ) -> httpx.Response | None:
        """多维表格本身（v1：读 / 改 is_advanced）、高级权限开关和角色（Base v3）。"""

        def ok(data: dict[str, Any]) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": data})

        whole = re.fullmatch(r"/bitable/v1/apps/(\w+)", api)
        if whole and method in ("GET", "PUT"):
            app = whole.group(1)
            if self.bitable_copying.get(app):
                self.bitable_copying[app] -= 1
                return httpx.Response(
                    200, json={"code": 1254036, "msg": "Bitable is copying, please try again later."}
                )
            if method == "PUT":
                body = json.loads(request.content)
                if "is_advanced" in body:
                    self.bitable_advanced[app] = bool(body["is_advanced"])
            return ok({"app": {"app_token": app, "is_advanced": self.bitable_advanced.get(app, False)}})
        switch = re.fullmatch(r"/base/v3/bases/(\w+)/advperm/enable", api)
        if switch and method == "PUT":
            app = switch.group(1)
            on = request.url.params.get("enable") == "true"
            self.bitable_advanced[app] = on
            if on and app not in self.bitable_roles:
                # 和飞书一样：开了高级权限，原来能编辑的人归到一个「编辑者」角色
                tables = [
                    t["name"]
                    for tid, t in self.bitable_tables.items()
                    if self.bitable_table_apps.get(tid) == app
                ]
                self.bitable_roles[app] = [
                    {
                        "role_id": f"rolR{seq}",
                        "role_name": "Reader",
                        "role_type": "reader",
                        "base_rule_map": {"copy": True, "download": True},
                        "table_rule_map": {n: {"perm": "read_only"} for n in tables},
                    },
                    {
                        "role_id": f"rolE{seq}",
                        "role_name": "Editor",
                        "role_type": "editor",
                        "base_rule_map": {"download": True, "copy": True},
                        "dashboard_rule_map": {"Dashboard": {"perm": "read_only"}},
                        "table_rule_map": {n: {"perm": "manage"} for n in tables},
                    },
                ]
            return ok({})
        roles = re.fullmatch(r"/base/v3/bases/(\w+)/roles(?:/(\w+))?", api)
        if roles:
            # 2026-10-29 线上实测：返回的 data 里是一段 JSON 字符串，列表里每个角色也是 JSON 字符串；改角色是增量合并，
            # role_name / role_type / base_rule_map 必带，表权限不是 no_perm 时视图、记录、字段规则都要给，否则 code 1
            app, role_id = roles.groups()
            if not self.bitable_advanced.get(app):
                return httpx.Response(200, json={"code": 800004006, "msg": "advperm not enabled"})
            items = self.bitable_roles.get(app) or []
            if method == "GET" and not role_id:
                summary = [
                    json.dumps({k: r[k] for k in ("role_id", "role_name", "role_type")})
                    for r in items
                ]
                return ok({"data": json.dumps({"base_roles": summary, "total": len(items)})})
            role = next((r for r in items if r["role_id"] == role_id), None)
            if role is None:
                return httpx.Response(200, json={"code": 800030104, "msg": "not_found"})
            if method == "GET":
                return ok({"data": json.dumps(role, ensure_ascii=False)})
            if method == "PUT":
                body = json.loads(request.content)
                rules = body.get("table_rule_map") or {}
                incomplete = any(
                    r.get("perm") != "no_perm"
                    and not all(r.get(k) for k in ("view_rule", "record_rule", "field_rule"))
                    for r in rules.values()
                )
                if incomplete or not all(body.get(k) for k in ("role_name", "role_type", "base_rule_map")):
                    return httpx.Response(200, json={"code": 1, "msg": ""})
                role["base_rule_map"] = body["base_rule_map"]
                role.setdefault("table_rule_map", {}).update(rules)
                return ok({})
        return None

    @staticmethod
    def _field_view(index: int, field: dict[str, Any]) -> dict[str, Any]:
        """列字段时返回的一列：和飞书一样，单选的每个选项都带 id（没存 id 的按位置给一个固定的）。"""
        view = {"field_id": f"fld{index}", **field}
        options = (field.get("property") or {}).get("options")
        if options:
            view["property"] = {
                **field["property"],
                "options": [
                    {**o, "id": o.get("id") or f"opt{index}_{j}"}
                    for j, o in enumerate(options)
                ],
            }
        return view

    def _patch_view(
        self, table_id: str, view_id: str, request: httpx.Request
    ) -> httpx.Response:
        """更新视图：照 2026-10-25 实测的飞书规矩校验筛选——「为空」不能带 value，「是」的 value 是选项 id 列表的 JSON 字符串。"""
        view = next(
            (
                v
                for v in self.bitable_views.get(table_id) or []
                if v["view_id"] == view_id
            ),
            None,
        )
        if view is None:
            return httpx.Response(200, json={"code": 1254003, "msg": "WrongViewId"})
        body = json.loads(request.content)
        # 和飞书一样（2026-10-26 线上）：画册视图不认 hidden_fields
        if view.get("view_type") == "gallery" and "hidden_fields" in (
            body.get("property") or {}
        ):
            return httpx.Response(
                200,
                json={
                    "code": 1254019,
                    "msg": "The view type is invalid, please modify it.",
                },
            )
        filter_info = (body.get("property") or {}).get("filter_info")
        for cond in (filter_info or {}).get("conditions") or []:
            value = cond.get("value")
            if cond["operator"] == "isEmpty" and value is not None:
                return httpx.Response(
                    200, json={"code": 1254001, "msg": "WrongRequestBody"}
                )
            if cond["operator"] == "is":
                if not isinstance(value, str):
                    return httpx.Response(
                        200, json={"code": 9499, "msg": "Bad Request"}
                    )
                if not isinstance(json.loads(value), list):
                    return httpx.Response(
                        200, json={"code": 1254001, "msg": "WrongRequestBody"}
                    )
                # 2026-10-26 实测：一个「是」里放几个选项 id 只认第一个（不报错）；假飞书直接拒，免得程序这样写
                if len(json.loads(value)) != 1:
                    return httpx.Response(
                        200, json={"code": 1254001, "msg": "WrongRequestBody"}
                    )
        if body.get("view_name"):
            # 和别的视图重名不行；改成自己现在的名字也当重名（程序不该发这种改名）
            if any(
                v["view_name"] == body["view_name"]
                for v in self.bitable_views[table_id]
            ):
                return httpx.Response(
                    200, json={"code": 1254020, "msg": "ViewNameDuplicated"}
                )
            key = (table_id, view["view_name"])
            self.bitable_view_reserved[key] = max(
                self.bitable_view_reserved.get(key, 0), 1
            )
            view["view_name"] = body["view_name"]
        if body.get("property"):
            prop = dict(body["property"])
            if "filter_info" in prop:
                if prop["filter_info"] is None:
                    # 和飞书一样（2026-10-30 实测）：filter_info 给 null 不理
                    prop.pop("filter_info")
                elif not prop["filter_info"].get("conditions"):
                    # 空条件 = 去掉筛选（读回来是 null）
                    prop["filter_info"] = None
            view["property"] = {**(view.get("property") or {}), **prop}
        return httpx.Response(200, json={"code": 0, "data": {"view": dict(view)}})

    def _view_sort(
        self, method: str, api: str, request: httpx.Request
    ) -> httpx.Response | None:
        """Base v3 视图排序：PUT 设、GET 读；排序的列要在表里（按列名）。"""
        match = re.fullmatch(r"/base/v3/bases/\w+/tables/(\w+)/views/(\w+)/sort", api)
        if not match or method not in ("PUT", "GET"):
            return None
        table_id, view_id = match.groups()
        if not any(
            v["view_id"] == view_id for v in self.bitable_views.get(table_id) or []
        ):
            return httpx.Response(200, json={"code": 1254003, "msg": "WrongViewId"})
        key = (table_id, view_id)
        if method == "PUT" and self.sort_limited > 0:
            self.sort_limited -= 1
            return httpx.Response(
                200,
                json={
                    "code": 800004135,
                    "msg": "the method：OpenAPIUpdateViewSort limited",
                },
            )
        if method == "PUT":
            config = json.loads(request.content)["sort_config"]
            names = {
                f["field_name"]
                for f in self.bitable_tables[table_id].get("fields") or []
            }
            if any(c.get("field") not in names for c in config):
                return httpx.Response(
                    200, json={"code": 1254045, "msg": "FieldNameNotFound"}
                )
            self.bitable_view_sorts[key] = config
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"sort_config": self.bitable_view_sorts.get(key, [])},
            },
        )

    def _visible(self, table_id: str, view: dict[str, Any]) -> list[str]:
        """视图现在看得见的列（字段 id）：表格视图是没藏起来的列（设过可见列的按设的先后，之后新加的列排在最后），
        画册是设过的卡片字段（没设过是前 4 列，之后新加的列不上卡片）。"""
        ids = [
            f"fld{i}"
            for i in range(len(self.bitable_tables[table_id].get("fields") or []))
        ]
        if view.get("view_type") == "gallery":
            if "visible" not in view:
                return ids[:4]
            return [f for f in view["visible"] if f in ids]
        hidden = set((view.get("property") or {}).get("hidden_fields") or [])
        order = [f for f in view.get("visible") or [] if f in ids and f not in hidden]
        return order + [f for f in ids if f not in hidden and f not in order]

    def visible_names(self, table_id: str, view_name: str) -> list[str]:
        """这个视图现在看得见的列名（按先后；画册是卡片上的字段）。"""
        view = next(
            v for v in self.bitable_views[table_id] if v["view_name"] == view_name
        )
        fields = self.bitable_tables[table_id]["fields"]
        return [
            fields[int(f.removeprefix("fld"))]["field_name"]
            for f in self._visible(table_id, view)
        ]

    def _view_layout(
        self, method: str, api: str, request: httpx.Request
    ) -> httpx.Response | None:
        """Base v3 可见列与画册卡片（2026-10-27 实测）：可见列 GET / PUT 给的都是列名（不是字段 id），PUT 整份覆盖
        （认列名也认字段 id，主键列总在第一个，没列出的藏起来；表格视图里和 v1 的 hidden_fields 是同一份设置）；
        卡片封面只有画册 / 看板有、必须是附件列，给的是字段 id，没设过是第一个附件列。"""
        match = re.fullmatch(
            r"/base/v3/bases/\w+/tables/(\w+)/views/(\w+)/(visible_fields|card)", api
        )
        if not match or method not in ("PUT", "GET"):
            return None
        table_id, view_id, what = match.groups()
        view = next(
            (
                v
                for v in self.bitable_views.get(table_id) or []
                if v["view_id"] == view_id
            ),
            None,
        )
        if view is None:
            return httpx.Response(200, json={"code": 1254003, "msg": "WrongViewId"})
        fields = [
            self._field_view(i, f)
            for i, f in enumerate(self.bitable_tables[table_id].get("fields") or [])
        ]
        invalid = httpx.Response(
            200, json={"code": 800010521, "msg": "invalid_request: invalid_request"}
        )
        if what == "card":
            if view.get("view_type") not in ("gallery", "kanban"):
                return invalid
            if method == "PUT":
                cover = json.loads(request.content)["cover_field"]
                found = next(
                    (f for f in fields if cover in (f["field_id"], f["field_name"])),
                    None,
                )
                if found is None or found["type"] != 17:
                    return invalid
                view["cover"] = found["field_id"]
            first = next((f["field_id"] for f in fields if f["type"] == 17), None)
            return httpx.Response(
                200,
                json={"code": 0, "data": {"cover_field": view.get("cover", first)}},
            )
        if method == "PUT":
            if self.visible_limited > 0:
                self.visible_limited -= 1
                return httpx.Response(
                    200,
                    json={
                        "code": 800004135,
                        "msg": "the method：OpenAPISetVisibleFields limited",
                    },
                )
            ids = [f["field_id"] for f in fields]
            by_name = {f["field_name"]: f["field_id"] for f in fields}
            wanted: list[str] = []
            for item in json.loads(request.content)["visible_fields"]:
                field_id = item if item in ids else by_name.get(item)
                if field_id is None:
                    return httpx.Response(
                        200, json={"code": 800030201, "msg": "not_found: not_found"}
                    )
                if field_id not in wanted:
                    wanted.append(field_id)
            view["visible"] = [ids[0], *(f for f in wanted if f != ids[0])]
            if view.get("view_type") != "gallery":
                view.setdefault("property", {})["hidden_fields"] = [
                    f for f in ids if f not in view["visible"]
                ]
        names = {f["field_id"]: f["field_name"] for f in fields}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [names[f] for f in self._visible(table_id, view)],
            },
        )

    def _blocks(
        self, method: str, api: str, request: httpx.Request, seq: int
    ) -> httpx.Response | None:
        """Base v3 块接口：列根目录 / 文件夹里的块、建文件夹、把数据表挪进挪出文件夹。"""
        match = re.fullmatch(r"/base/v3/bases/(\w+)/blocks(?:/(\w+))?(?:/(\w+))?", api)
        if not match or method != "POST":
            return None
        app, block_id, action = match.groups()
        body = json.loads(request.content or b"{}")

        def ok(data: dict[str, Any]) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": data})

        if block_id == "list" and action is None:
            parent = body.get("parent_id")
            # 文件夹带着它在哪个文件夹里（没传 parent_id 时列出全部文件夹，嵌套的也在）
            items = [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "parent_id": f.get("parent_id"),
                    "type": "folder",
                }
                for f in self.bitable_folders.values()
                if f["app"] == app and (parent is None or f.get("parent_id") == parent)
            ] + [
                {
                    "id": t,
                    "name": self.bitable_tables[t].get("name"),
                    "parent_id": p,
                    "type": "table",
                }
                for t, p in self.bitable_parents.items()
                if self.bitable_table_apps.get(t) == app
                and t in self.bitable_tables
                and p == parent
            ]
            return ok({"blocks": items, "total": len(items)})
        if block_id is None:
            if body.get("type") != "folder":
                return httpx.Response(
                    200, json={"code": 1254001, "msg": "WrongRequestBody"}
                )
            parent = body.get("parent_id")
            if parent is not None and (
                parent not in self.bitable_folders
                or self.bitable_folders[parent]["app"] != app
            ):
                return httpx.Response(200, json={"code": 1254004, "msg": "NotFound"})
            folder = {
                "id": f"bfl{seq}",
                "name": body["name"],
                "app": app,
                "parent_id": parent,
            }
            self.bitable_folders[folder["id"]] = folder
            return ok(
                {
                    **{k: folder[k] for k in ("id", "name")},
                    "parent_id": parent,
                    "type": "folder",
                }
            )
        if action == "move":
            parent = body.get("parent_id")
            if block_id not in self.bitable_tables or (
                parent is not None and parent not in self.bitable_folders
            ):
                return httpx.Response(200, json={"code": 1254004, "msg": "NotFound"})
            self.bitable_parents[block_id] = parent
            return ok({})
        return None

    def _table_copy(
        self, method: str, api: str, request: httpx.Request, seq: int
    ) -> httpx.Response | None:
        """Base v3 复制数据表（2026-10-28 线上实测）：只复制结构时同步返回 success；列和视图（id 都不变，筛选、排序、
        可见列、封面，连只能在界面里设的封面效果，这里记作视图上的 cover_fit）都跟着复制，记录不复制；新表落在源表
        所在的文件夹里。同名报 800010102，源表不在报 800030104。新表上的 copied_from 记着从哪张复制的。"""
        match = re.fullmatch(r"/base/v3/bases/(\w+)/tables/(\w+)/copy", api)
        if not match or method != "POST":
            return None
        app, source = match.groups()
        if (
            source not in self.bitable_tables
            or self.bitable_table_apps.get(source) != app
        ):
            return httpx.Response(
                200, json={"code": 800030104, "msg": "not_found: not_found"}
            )
        body = json.loads(request.content)
        name = body.get("name")
        taken = any(
            t.get("name") == name
            for tid, t in self.bitable_tables.items()
            if self.bitable_table_apps.get(tid) == app
        )
        if body.get("range") != "schema" or taken:
            return httpx.Response(
                200,
                json={"code": 800010102, "msg": "validation_error: validation_error"},
            )
        table_id = f"tbl{seq}"
        self.bitable_tables[table_id] = {
            **json.loads(json.dumps(self.bitable_tables[source])),
            "name": name,
            "copied_from": source,
        }
        self.bitable_table_apps[table_id] = app
        self.bitable_parents[table_id] = self.bitable_parents.get(source)
        self.bitable_views[table_id] = json.loads(
            json.dumps(self.bitable_views.get(source) or [])
        )
        for (owner, view_id), config in list(self.bitable_view_sorts.items()):
            if owner == source:
                self.bitable_view_sorts[(table_id, view_id)] = json.loads(
                    json.dumps(config)
                )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"state": "success", "table": {"id": table_id, "name": name}},
            },
        )

    def _bitable(self, request: httpx.Request, api: str) -> httpx.Response | None:
        method = request.method

        def ok(data: dict[str, Any]) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": data})

        with self.lock:
            self.bitable_calls.append((method, api))
            moved = self._wiki(method, api, request)
            if moved is not None:
                return moved
            if any(m == method and part in api for m, part in self.bitable_fail):
                return httpx.Response(
                    200, json={"code": 1254000, "msg": "fake failure"}
                )
            self._bitable_seq += 1
            seq = self._bitable_seq
            if method == "POST" and api == "/bitable/v1/apps":
                app = {
                    "app_token": f"bascn{seq}",
                    "name": json.loads(request.content)["name"],
                    "url": f"https://example.feishu.cn/base/bascn{seq}",
                    "default_table_id": f"tblDefault{seq}",
                }
                self.bitable_apps.append(app)
                return ok({"app": app})
            if method == "POST" and api == "/drive/v1/medias/upload_all":
                form = {
                    k.decode(): v.decode()
                    for k, v in re.findall(
                        rb'name="(\w+)"\r\n\r\n([^\r]*)\r\n', request.content
                    )
                }
                token = f"file{seq}"
                self.bitable_files[token] = form
                return ok({"file_token": token})
            if method == "POST" and re.fullmatch(r"/drive/v1/files/\w+/subscribe", api):
                # 订阅云文档事件：subscribe_error 设成 (code, msg) 时返回业务错误
                if self.subscribe_error:
                    code, msg = self.subscribe_error
                    return httpx.Response(200, json={"code": code, "msg": msg})
                self.bitable_subscriptions.append(
                    (api.split("/")[4], request.url.params.get("file_type"))
                )
                return ok({})
            if method == "POST" and re.fullmatch(
                r"/drive/v1/permissions/\w+/members", api
            ):
                self.bitable_members.append(
                    {**json.loads(request.content), **dict(request.url.params)}
                )
                return ok({"member": json.loads(request.content)})
            blocks = self._blocks(method, api, request, seq)
            if blocks is not None:
                return blocks
            advanced = self._advanced_api(method, api, request, seq)
            if advanced is not None:
                return advanced
            copied = self._table_copy(method, api, request, seq)
            if copied is not None:
                return copied
            sort = self._view_sort(method, api, request)
            if sort is not None:
                return sort
            layout = self._view_layout(method, api, request)
            if layout is not None:
                return layout
            # /bitable/v1/apps/<app>/tables[/<table>[/views | /records[/<record>]]]
            parts = api.split("/")
            if len(parts) >= 6 and parts[1:4] == ["bitable", "v1", "apps"]:
                table_id = parts[6] if len(parts) > 6 else None
                rest = parts[7:]
                if method == "GET" and len(parts) == 6:
                    items = [
                        {"table_id": t, "name": table.get("name")}
                        for t, table in self.bitable_tables.items()
                        if self.bitable_table_apps.get(t) == parts[4]
                    ]
                    return ok({"items": items, "has_more": False, "total": len(items)})
                if method == "POST" and len(parts) == 6:
                    table = json.loads(request.content)["table"]
                    table_id = f"tbl{seq}"
                    self.bitable_tables[table_id] = {**table, "views": []}
                    self.bitable_table_apps[table_id] = parts[4]
                    self.bitable_parents[table_id] = None
                    # 和飞书一样：建表时带一个默认表格视图
                    self.bitable_views[table_id] = [
                        {
                            "view_id": f"vew{seq}d",
                            "view_name": table.get("default_view_name") or "表格",
                            "view_type": "grid",
                        }
                    ]
                    return ok({"table_id": table_id})
                if method == "DELETE" and not rest:
                    self.bitable_tables.pop(table_id, None)
                    return ok({"deleted": True, "table_id": table_id})
                if method == "PATCH" and not rest and table_id:
                    name = json.loads(request.content)["name"]
                    self.bitable_tables[table_id]["name"] = name
                    return ok({"name": name})
                if method == "POST" and rest == ["views"]:
                    body = json.loads(request.content)
                    key = (table_id, body.get("view_name"))
                    if self.bitable_view_reserved.get(key) or any(
                        v["view_name"] == body.get("view_name")
                        for v in self.bitable_views.get(table_id) or []
                    ):
                        if self.bitable_view_reserved.get(key):
                            self.bitable_view_reserved[key] -= 1
                        return httpx.Response(
                            200, json={"code": 1254020, "msg": "ViewNameDuplicated"}
                        )
                    self.bitable_tables[table_id]["views"].append(body)
                    view = {"view_id": f"vew{seq}", **body}
                    self.bitable_views.setdefault(table_id, []).append(view)
                    return ok({"view": view})
                if method == "GET" and rest == ["views"]:
                    return ok(
                        {
                            "items": [
                                dict(v) for v in self.bitable_views.get(table_id) or []
                            ],
                            "has_more": False,
                            "page_token": None,
                        }
                    )
                if method == "PATCH" and rest[:1] == ["views"] and len(rest) == 2:
                    return self._patch_view(table_id, rest[1], request)
                if method == "DELETE" and rest[:1] == ["views"] and len(rest) == 2:
                    before = self.bitable_views.get(table_id) or []
                    left = [v for v in before if v["view_id"] != rest[1]]
                    if len(left) == len(before):
                        return httpx.Response(
                            200, json={"code": 1254003, "msg": "WrongViewId"}
                        )
                    self.bitable_views[table_id] = left
                    return ok({"view_id": rest[1]})
                if method == "GET" and rest[:1] == ["views"] and len(rest) == 2:
                    view = next(
                        (
                            v
                            for v in self.bitable_views.get(table_id) or []
                            if v["view_id"] == rest[1]
                        ),
                        None,
                    )
                    if view is None:
                        return httpx.Response(
                            200, json={"code": 1254003, "msg": "WrongViewId"}
                        )
                    return ok({"view": dict(view)})
                if rest == ["fields"]:
                    table = self.bitable_tables[table_id]
                    if method == "POST":
                        table.setdefault("fields", []).append(
                            json.loads(request.content)
                        )
                        return ok({"field": {"field_id": f"fld{seq}"}})
                    return ok(
                        {
                            "items": [
                                self._field_view(i, f)
                                for i, f in enumerate(table.get("fields") or [])
                            ],
                            "has_more": False,
                            "page_token": None,
                        }
                    )
                if method == "DELETE" and rest[:1] == ["fields"] and len(rest) == 2:
                    # 删字段：这一列和各行里这一列的值一起没了（字段 id 按位置给，后面的列 id 会前移）
                    fields = self.bitable_tables[table_id]["fields"]
                    index = int(rest[1].removeprefix("fld"))
                    if index >= len(fields):
                        return httpx.Response(200, json={"code": 1254045, "msg": "FieldNameNotFound"})
                    name = fields.pop(index)["field_name"]
                    for r, t in self.bitable_record_tables.items():
                        if t == table_id and r in self.bitable_records:
                            self.bitable_records[r].pop(name, None)
                    return ok({"field_id": rest[1], "deleted": True})
                if method == "PUT" and rest[:1] == ["fields"] and len(rest) == 2:
                    # 更新字段是全量覆盖；改了名，已有记录里的键跟着改
                    fields = self.bitable_tables[table_id]["fields"]
                    index = int(rest[1].removeprefix("fld"))
                    body = json.loads(request.content)
                    if body == fields[index]:
                        # 和飞书一样：内容一点没变时报 DataNotChange
                        return httpx.Response(
                            200, json={"code": 1254606, "msg": "DataNotChange"}
                        )
                    old, new = fields[index]["field_name"], body["field_name"]
                    fields[index] = body
                    if old != new:
                        for r, t in self.bitable_record_tables.items():
                            if t == table_id and old in self.bitable_records.get(r, {}):
                                self.bitable_records[r][new] = self.bitable_records[
                                    r
                                ].pop(old)
                    return ok({"field": {"field_id": rest[1], **body}})
                if rest[:1] == ["records"]:
                    if method == "POST" and len(rest) == 1:
                        fields = json.loads(request.content)["fields"]
                        if self.bitable_reject_person and "选中人" in fields:
                            return httpx.Response(
                                200, json={"code": 1254066, "msg": "UserFieldConvFail"}
                            )
                        record_id = f"rec{seq}"
                        self.bitable_records[record_id] = fields
                        self.bitable_record_tables[record_id] = table_id
                        return ok({"record": {"record_id": record_id}})
                    if method == "GET" and len(rest) == 1:
                        # 列记录：按插入顺序分页，page_token 是下一页的起点
                        ids = [
                            r
                            for r in self.bitable_records
                            if self.bitable_record_tables.get(r) == table_id
                        ]
                        size = int(request.url.params.get("page_size", 500))
                        start = int(request.url.params.get("page_token") or 0)
                        page = ids[start : start + size]
                        more = start + size < len(ids)
                        automatic = request.url.params.get("automatic_fields") in ("true", "True", "1")
                        return ok(
                            {
                                "items": [
                                    {
                                        "record_id": r,
                                        "fields": {
                                            k: v
                                            for k, v in self.bitable_records[r].items()
                                            if v not in (False, None)
                                        },
                                        **(
                                            {
                                                "last_modified_by": {"id": self.bitable_modifiers[r]},
                                                "last_modified_time": self.bitable_modified_at.get(r, 1761800000000),
                                            }
                                            if automatic and r in self.bitable_modifiers
                                            else {}
                                        ),
                                    }
                                    for r in page
                                ],
                                "has_more": more,
                                "page_token": str(start + size) if more else None,
                                "total": len(ids),
                            }
                        )
                    if method == "POST" and rest == ["records", "batch_update"]:
                        for item in json.loads(request.content)["records"]:
                            self.bitable_records[item["record_id"]].update(
                                item["fields"]
                            )
                        return ok({"records": []})
                    if method == "POST" and rest == ["records", "batch_create"]:
                        # 按传进来的顺序追加在表的末尾
                        created = []
                        for item in json.loads(request.content)["records"]:
                            self._bitable_seq += 1
                            record_id = f"rec{self._bitable_seq}"
                            self.bitable_records[record_id] = dict(item["fields"])
                            self.bitable_record_tables[record_id] = table_id
                            created.append(
                                {"record_id": record_id, "fields": item["fields"]}
                            )
                        return ok({"records": created})
                    if method == "POST" and rest == ["records", "batch_delete"]:
                        # 和飞书一样：有一条不存在整批失败
                        ids = json.loads(request.content)["records"]
                        if any(r not in self.bitable_records for r in ids):
                            return httpx.Response(
                                200, json={"code": 1254043, "msg": "RecordIdNotFound"}
                            )
                        for record_id in ids:
                            self.bitable_records.pop(record_id, None)
                        return ok(
                            {
                                "records": [
                                    {"deleted": True, "record_id": r} for r in ids
                                ]
                            }
                        )
                    if method == "POST" and rest == ["records", "batch_get"]:
                        body = json.loads(request.content)
                        found = [
                            r for r in body["record_ids"] if r in self.bitable_records
                        ]
                        return ok(
                            {
                                "records": [
                                    {
                                        "record_id": r,
                                        "fields": {
                                            k: v
                                            for k, v in self.bitable_records[r].items()
                                            if v not in (False, None)
                                        },
                                        **(
                                            {
                                                "last_modified_by": {
                                                    "id": self.bitable_modifiers[r]
                                                }
                                            }
                                            if body.get("automatic_fields")
                                            and r in self.bitable_modifiers
                                            else {}
                                        ),
                                    }
                                    for r in found
                                ],
                                "absent_record_ids": [
                                    r for r in body["record_ids"] if r not in found
                                ],
                            }
                        )
                    if method == "PUT" and len(rest) == 2:
                        self.bitable_records[rest[1]].update(
                            json.loads(request.content)["fields"]
                        )
                        return ok({"record": {"record_id": rest[1]}})
                    if method == "DELETE" and len(rest) == 2:
                        self.bitable_records.pop(rest[1], None)
                        return ok({"deleted": True, "record_id": rest[1]})
        return None

    def _oss(self, request: httpx.Request) -> httpx.Response:
        key = unquote(request.url.path.lstrip("/"))
        method = request.method
        with self.lock:
            self.oss_requests.append((method, key, dict(request.headers)))
            if method == "PUT":
                if self.oss_fail_puts > 0:
                    self.oss_fail_puts -= 1
                    return httpx.Response(
                        503, text="<Error><Code>ServiceUnavailable</Code></Error>"
                    )
                if (
                    request.headers.get("x-oss-forbid-overwrite") == "true"
                    and key in self.oss_objects
                ):
                    return httpx.Response(
                        409, text="<Error><Code>FileAlreadyExists</Code></Error>"
                    )
                meta = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower().startswith("x-oss-meta-")
                }
                self.oss_objects[key] = (request.content, meta)
                return httpx.Response(200)
            if method == "DELETE":
                self.oss_objects.pop(key, None)
                return httpx.Response(204)
            if key not in self.oss_objects:
                if method == "HEAD":
                    return httpx.Response(404, headers={"x-oss-ec": "0026-00000001"})
                return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
            body, meta = self.oss_objects[key]
            if method == "HEAD":
                return httpx.Response(
                    200, headers={**meta, "x-oss-object-size": str(len(body))}
                )
            return httpx.Response(200, content=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        request.read()
        if request.url.host.endswith(".aliyuncs.com"):
            return self._oss(request)
        with self.lock:
            self.requests.append(
                {
                    "path": path,
                    "headers": dict(request.headers),
                    "body": request.content,
                }
            )
        if request.url.host == "open.feishu.cn":
            if path.endswith("/tenant_access_token/internal"):
                return httpx.Response(
                    200,
                    json={"code": 0, "tenant_access_token": "t-test", "expire": 7200},
                )
            if path.endswith("/im/v1/messages"):
                body = json.loads(request.content)
                content = json.loads(body["content"])
                if body["msg_type"] == "interactive":
                    if self.feishu_reject_cards:
                        return httpx.Response(
                            200, json={"code": 230099, "msg": "card content failed"}
                        )
                    with self.lock:
                        self.feishu_cards.append(content)
                        self.feishu_messages.append(card_text(content))
                else:
                    with self.lock:
                        self.feishu_messages.append(content["text"])
                return httpx.Response(
                    200, json={"code": 0, "data": {"message_id": "om_test"}}
                )
            if path.endswith("/im/v1/images"):
                with self.lock:
                    key = f"img_v3_{len(self.feishu_images) + 1}"
                    self.feishu_images[key] = {"bytes": len(request.content)}
                return httpx.Response(200, json={"code": 0, "data": {"image_key": key}})
            table_response = self._bitable(request, path.removeprefix("/open-apis"))
            if table_response is not None:
                return table_response
            return httpx.Response(404, json={"code": 404})
        if request.url.host == "mooya.test":
            return self._mooya(request)
        if request.url.host == "img.test":
            # 妙手图（人工上的款的主图）；路径带 gone 的是妙手清掉了的图
            if "gone" in path:
                return httpx.Response(404, content=b"not found")
            return httpx.Response(200, content=make_png(len(path), size=(64, 64)))
        if request.url.host == "miaoshou.test":
            return self._miaoshou(request)
        if request.url.host == "temu.test":
            return self._temu_open(request)
        if path == "/v1/models":
            ids = ["gpt-6.1-sol*", *FANOUT_MODELS, *EDIT_MODELS]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        payload = json.loads(request.content)
        if path == "/v1/chat/completions":
            system = payload["messages"][0]["content"]
            stage = (
                system.split("# 当前阶段：", 1)[1].split()[0]
                if "# 当前阶段：" in system
                else "PROBE"
            )
            with self.lock:
                self.stage_counts[stage] += 1
            if stage == "PROBE" or "# 当前阶段：" not in system:
                content = '{"ok": true}'
            elif stage in self.overrides:
                content = self.overrides[stage](payload)
            else:
                content = json.dumps(
                    envelope(stage, default_result(stage, payload, self)),
                    ensure_ascii=False,
                )
            usage = {"prompt_tokens": 2000, "completion_tokens": 800}
            if payload.get("stream"):
                return sse_response(payload["model"], content, usage)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "model": payload["model"],
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": usage,
                },
            )
        if path == "/v1/images/generations":
            model = payload["model"]
            with self.lock:
                self.image_counts[model] += 1
                self._image_seq += 1
                seq = self._image_seq
            # 图生图、修复和文生图是同一个模型名：带输入图的请求按「模型+images」取模式
            key = f"{model}+images" if payload["vendor_params"].get("images") else model
            mode = self.image_modes_by_model.get(key, self.image_mode)
            if mode == "timeout":
                raise httpx.ReadTimeout("模拟读超时", request=request)
            if mode == "503":
                return httpx.Response(
                    503, text="upstream unavailable", headers={"retry-after": "0"}
                )
            if mode == "400":
                return httpx.Response(400, text="content policy")
            if mode == "500_param":
                return httpx.Response(
                    500,
                    text='{"error":{"message":"litellm.APIConnectionError: 所有调用池均失败(2次尝试): L0:x=HTTP 400: Unknown parameter: \'format\'."}}',
                )
            if (
                mode == "edit_rejects_format"
                and "images" in payload["vendor_params"]
                and "format" in payload["vendor_params"]
            ):
                return httpx.Response(
                    500, text="HTTP 400: Unknown parameter: 'format'."
                )
            if mode == "identical":
                data = make_png(7)
            elif mode == "similar":
                data = make_png(7, tweak=3, noise_seed=seq)
            else:
                data = make_png(1000 + seq)
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "data": [{"b64_json": base64.b64encode(data).decode()}],
                },
            )
        return httpx.Response(404, text="not found")

    def image_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(r["body"])
            for r in self.requests
            if r["path"] == "/v1/images/generations"
        ]

    def calls_by_path(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for r in self.requests:
            out[r["path"]] += 1
        return dict(out)


PG_TEST_URL_NAME = "PODFLOW_TEST_DATABASE_URL"


def _reset_pg_schema(url: str) -> None:
    import psycopg

    # 上一个用例留下的连接要先释放，否则会占着角色的连接数上限
    gc.collect()
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PODFLOW_HOME", str(tmp_path))
    assert cli.main(["init"]) == 0
    cfg_path = tmp_path / "podflow.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # 用 os.getenv：同一文件里有 httpx，SkillScan 把 os.environ 加网络调用判成环境变量外泄
    pg_url = os.getenv(PG_TEST_URL_NAME)
    if pg_url:
        _reset_pg_schema(pg_url)
        cfg["database"] = {"url_name": PG_TEST_URL_NAME}
    cfg["deerflow_version"] = "test"
    # 参考图 2D 化默认关着：每张图多一次出图会改动现有用例的出图次数；test_pattern_extract 里单独打开
    cfg.setdefault("pattern_extract", {})["enabled"] = False
    # 任务命名默认关着：拆解时多一次 P12 调用会改动现有用例的调用次数；test_task_name 里单独打开
    cfg.setdefault("task_name", {})["enabled"] = False
    # 画面描述默认关着：拆解时多一次 P13 调用会改动现有用例的调用次数；test_reference_profile 里单独打开
    cfg.setdefault("describe", {})["enabled"] = False
    # 套图默认关着：选款完成不自动交 mooya、任务只有五步；test_scenes 里单独打开（假 mooya 在 mooya.test）
    cfg.setdefault("scenes", {})["enabled"] = False
    # 候选表表顶的按钮行默认关着：表里多出的按钮行 / 交付包行 / 套图行会改动现有用例的行数；
    # test_candidate_control 里单独打开
    cfg.setdefault("table", {})["control_row"] = False
    # 预留行默认关着：方向一确认就多出「未生成」的行，会改动现有用例的行数和调用；test_candidate_slots 里单独打开
    cfg["table"]["reserve_rows"] = False
    # 「选中的款」的下载确认默认关着：多出的两列、排序和勾的写入会改动现有用例的调用；
    # test_selected_downloads 里单独打开
    cfg["table"]["download_confirm"] = False
    # 候选表模板默认关着：多一张模板表和复制调用会改动现有用例的表数和调用；test_table_template 里单独打开
    cfg["table"]["template"] = False
    # 每周选款表默认关着（线上也是迁移跑完才开）；test_weekly_books 里单独打开
    cfg["table"]["weekly"] = False
    # 默认视图与表名前缀、上架状态三列默认关着：多出的视图 / 字段调用和改名会改动现有用例；
    # test_candidate_views、test_listing 里单独打开（归档 shelf 默认开着、自动归档默认关，和线上样例一致）
    cfg["table"]["views"] = False
    cfg.setdefault("listing", {})["enabled"] = False
    # 三处组合用构图不动时改不了的字段默认关着：假拆解里判成不可变的小变化字段会全进组合池、改动现有用例的槽位；
    # test_dictionary_05 里单独打开
    cfg.setdefault("plan", {})["combo_layout_fields"] = False
    # 出图 worker 默认一条工位、没活马上退出：多工位并发与空闲等待在 test_worker_lanes 里单独测
    cfg.setdefault("batch", {})["workers"] = 1
    cfg["batch"]["worker_idle_seconds"] = 0
    cfg.setdefault("mooya", {})["base_url"] = "http://mooya.test"
    cfg.setdefault("miaoshou", {})["base_url"] = "http://miaoshou.test"
    cfg.setdefault("temu_open", {})["base_url"] = "http://temu.test/openapi/router"
    cfg["miaoshou"]["min_interval_seconds"] = 0
    cfg["budget"] = {
        "currency": "CNY",
        "daily_cap": 100,
        "total_cap": 500,
        "per_task_cap": 20,
    }
    cfg["prices"]["chat"]["gpt-6.1-sol-high"] = {
        "input_per_mtok": 10,
        "output_per_mtok": 30,
    }
    for model in FANOUT_MODELS:
        cfg["prices"]["image"][model] = {"per_image": 0.5}
    cfg_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "LITELLM_API_KEY=test-key\nMOOYA_SERVICE_TOKEN=test-mooya-token\n"
        "MIAOSHOU_APP_KEY=ak_test\nMIAOSHOU_APP_SECRET=as_test_secret\n",
        encoding="utf-8",
    )
    os.chmod(secrets, 0o600)
    return tmp_path


def set_config(home: Path, dotted: str, value: Any) -> None:
    """改临时配置；键名本身带点（如模型名）时用 / 分隔路径。"""
    path = home / "podflow.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    node = cfg
    parts = dotted.split("/") if "/" in dotted else dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(
        http,
        "make_client",
        lambda timeout: httpx.Client(
            transport=httpx.MockTransport(provider.handler), timeout=timeout
        ),
    )
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.setattr(jobmod, "_sleep", lambda s: None)
    monkeypatch.setattr(views, "_sleep", provider.view_waits.append)
    return provider


@pytest.fixture
def ctx(home: Path, fake: FakeProvider):
    c = open_ctx(home)
    yield c
    c.db.close()


@pytest.fixture
def seed_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("src") / "seed.png"
    path.write_bytes(make_png(42, size=(988, 1282)))
    return path


def reopen(home: Path):
    return open_ctx(home)


def confirm_all(ctx, plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """把一轮策划出的候选方向全部选中（不在后台出图）。"""
    from podflow import directions

    return directions.confirm(
        ctx,
        plan["batch_id"],
        by="tester",
        pick=[t["variant_id"] for t in plan["accepted"]],
        **kwargs,
    )
