"""按「可变单元 × 字段」策划：画面描述列出可变单元（单个角色、满屏星星、一组角色和它的成员），小变化改两处、
三处组合改三处，一处 = 一个单元上的一个字段（整幅字段挂在「整幅」上）；每处都要看得见、同一单元上的两处要求它是大档
且部位不同、整幅最多一处；没有清单或 plan.unit 是 fields 时按字段策划。"""

from __future__ import annotations

import json
import random
from collections import Counter

from conftest import (
    card_text,
    domains,
    envelope,
    input_block,
    p13_result,
    reopen,
    set_config,
)
from podflow import bg, cli, doctor, notify, subjects
from podflow.stages import plan as planmod
from podflow.stages import profile
from podflow.stages import seed as seedmod
from podflow.util import canonical_json, loads, normalize_phrase, sha256_text
from podflow.worker import Worker
from test_variant_rules import _chat_messages, _text

UNIT_VARIABLE = ("D09", "D11", "D17", "D20", "D37", "D41")
# 单主体图：中间一头大高地牛（大），两颗星星是一组装饰（中）
COW = [
    {
        "object_id": "E01",
        "role": "subject",
        "attributes": {"species": "高地牛"},
        "visual_function": "中央主角",
    },
    {
        "object_id": "E02",
        "role": "decoration",
        "attributes": {"category": "星星"},
        "visual_function": "左上点缀",
    },
    {
        "object_id": "E03",
        "role": "decoration",
        "attributes": {"category": "星星"},
        "visual_function": "右上点缀",
    },
]


def _home(home, fake, objects=COW):
    set_config(home, "describe.enabled", True)
    fake.p02_variable = set(UNIT_VARIABLE)
    fake.p04_domains = domains(UNIT_VARIABLE)
    fake.p02_objects = objects
    return reopen(home)


def _family(ctx, seed_image) -> str:
    seed = seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, seed["seed_id"])
    family_id = out["steps"][-1]["family_id"]
    seedmod.decide_family(ctx, family_id, approve=True, by="tester")
    return family_id


def _unit(uid, name, kind, objects, salience, parent=None, denied=None) -> dict:
    return {
        "unit_id": uid,
        "name": name,
        "kind": kind,
        "salience": salience,
        "object_ids": objects,
        "key": subjects.unit_key(objects),
        "parent": parent,
        "members": objects,
        "not_applicable": denied or {},
        "en": f"the {name}",
    }


def _info(fid: str) -> dict:
    from podflow import fields

    return {
        "field_id": fid,
        "field": fields.field_name(fid),
        "label": fields.label(fid),
        "dimension": fields.dimension(fid),
        "current": "",
        "options": [],
        "compatibility": [],
    }


def _pool(units, fids):
    pool = subjects.pair_pool(units, [_info(f) for f in fids])
    return pool, {p["pair_id"]: p for p in pool}


def test_pair_pool_follows_the_dictionary() -> None:
    """单元类型对得上才挂字段；互动只给角色组，承托依附要有承托物；否决的不挂；小档与降档后变小的不出。"""
    units = [
        _unit("U01", "全部动物", "character", ["E01", "E02"], "large"),
        _unit(
            "U02",
            "长颈鹿",
            "character",
            ["E01"],
            "medium",
            "U01",
            {"D20": "没有脖子以下"},
        ),
        _unit("U03", "小象", "character", ["E02"], "medium", "U01"),
        _unit("U04", "满屏星星", "decoration", ["E03", "E04"], "medium"),
        _unit("U05", "角落小花", "decoration", ["E05"], "small"),
    ]
    fids = ["D09", "D10", "D11", "D17", "D20", "D23", "D25", "D37", "D41"]
    pool, by = _pool(units, fids)
    assert [subjects.pair_label(p) for p in pool] == [
        "全部动物·主体",
        "全部动物·表情",
        "全部动物·服饰配件",
        "全部动物·距离互动",
        "长颈鹿·主体",
        "小象·主体",
        "小象·服饰配件",
        "满屏星星·背景装饰",
        "陪衬角色",
        "整体配色",
        "绘制技法",
    ]
    # 表情只动脸：大单元降成中档，中档成员降成小档就不出；长颈鹿否决了服饰配件
    assert by["U01:D17"]["tier"] == "medium" and "U02:D17" not in by
    assert "U02:D20" not in by and "U03:D20" in by
    # 承托依附要画面里有承托物单元；小花是小档，什么都不挂
    assert not any(p["field_id"] == "D25" for p in pool)
    assert not any(p["unit_id"] == "U05" for p in pool)
    # 整幅字段的档位按字段：加陪衬是中档，配色 / 技法是大档
    assert by["canvas:D10"]["tier"] == "medium" and by["canvas:D37"]["tier"] == "large"
    # 有承托物单元时才给角色挂承托依附
    pool2, _ = _pool(
        units + [_unit("U06", "枝条", "prop", ["E06"], "medium")], ["D21", "D25"]
    )
    assert {p["pair_id"] for p in pool2} >= {"U01:D25", "U06:D21", "U01:D21"}


def test_slot_rules() -> None:
    """同一单元上的两处要它是大档、部位不同、不互斥；组与成员算重叠；同一字段不能落在重叠单元上；整幅最多一处。"""
    units = [
        _unit("U01", "高地牛", "character", ["E01"], "large"),
        _unit("U02", "全部动物", "character", ["E02", "E03"], "large"),
        _unit("U03", "小熊", "character", ["E02"], "medium", "U02"),
        _unit("U04", "满屏星星", "decoration", ["E04"], "medium"),
    ]
    _, by = _pool(units, ["D09", "D13", "D15", "D17", "D20", "D11", "D37", "D41"])

    def ok(*ids, present=True):
        return subjects.slot_ok([by[i] for i in ids], units_present=present)

    assert not ok("U01:D15", "U01:D17")  # 都在脸上
    assert ok("U01:D17", "U01:D20")  # 大单元：脸 + 穿戴
    assert not ok("U01:D09", "U01:D13")  # 换物种和轮廓互斥
    assert not ok("U03:D13", "U03:D20")  # 中档单元不能吃两处
    assert ok("U02:D17", "U03:D20")  # 组（大）+ 成员，部位不同
    assert not ok("U02:D20", "U03:D20")  # 同一字段落在重叠的组和成员上
    assert ok("U01:D20", "U03:D20")  # 不重叠的两个单元可以改同一字段
    assert ok("U01:D17", "U04:D11")
    assert not ok("canvas:D37", "canvas:D41")  # 整幅最多一处
    assert ok("canvas:D37", "canvas:D41", present=False)  # 抽象图没有单元时不限
    assert ok("canvas:D37", "U01:D17", "U04:D11")
    # 抽象图：池里只有整幅字段，配色 + 技法算一组
    abstract, _ = _pool([], ["D37", "D41"])
    assert subjects.count_valid(abstract, 2, 9) == 1


def test_picking_spreads_over_units_and_is_reproducible() -> None:
    """字段多的单元不垄断：每个单元的总机会相同；同一个种子结果一样；挑出来的都合法、同一批不重复。"""
    units = [
        _unit("U01", "高地牛", "character", ["E01"], "large"),
        _unit("U02", "满屏星星", "decoration", ["E02"], "medium"),
    ]
    pool, _ = _pool(
        units, ["D09", "D13", "D14", "D16", "D18", "D19", "D20", "D11", "D37"]
    )
    hits: Counter[str] = Counter()
    for seed in range(150):
        sets = subjects.pick_pair_sets(pool, 2, 6, random.Random(seed))
        assert len({frozenset(p["pair_id"] for p in s) for s in sets}) == len(sets) == 6
        assert all(subjects.slot_ok(s) for s in sets)
        for s in sets:
            hits.update(p["unit_id"] for p in s)
    # 高地牛有 7 个字段、星星只有 1 个：按组合均匀抽时星星只占约 1/9，按单元平分后接近牛
    assert hits["U02"] > hits["U01"] * 0.5
    first = subjects.pick_pair_sets(pool, 2, 6, random.Random(7))
    again = subjects.pick_pair_sets(pool, 2, 6, random.Random(7))
    assert [[p["pair_id"] for p in s] for s in first] == [
        [p["pair_id"] for p in s] for s in again
    ]
    # 做过的组合往后排
    done = {frozenset({"E01:D09", "E02:D11"})}
    picked = Counter()
    for seed in range(200):
        for s in subjects.pick_pair_sets(pool, 2, 1, random.Random(seed), done=done):
            picked[frozenset(p["key"] for p in s)] += 1
    assert picked[frozenset({"E01:D09", "E02:D11"})] < 8


def test_members_of_one_group_share_one_unit_of_chance() -> None:
    """十只恐龙这种多角色图：同一个组下的成员单元合起来只占一份，组、装饰和整幅不会被挤掉。"""
    members = [
        _unit(
            f"U{i + 2:02d}", f"恐龙{i}", "character", [f"E{i + 1:02d}"], "medium", "U01"
        )
        for i in range(8)
    ]
    units = [
        _unit(
            "U01",
            "全部恐龙",
            "character",
            [m["object_ids"][0] for m in members],
            "large",
        ),
        *members,
        _unit("U10", "满天星点", "decoration", ["E20"], "medium"),
    ]
    pool, _ = _pool(units, ["D09", "D13", "D17", "D20", "D11", "D37", "D41"])
    hits: Counter[str] = Counter()
    for seed in range(120):
        for s in subjects.pick_pair_sets(pool, 2, 12, random.Random(seed)):
            for p in s:
                hits["成员" if p["parent"] else p["unit_id"]] += 1
    total = sum(hits.values())
    assert hits["成员"] / total < 0.5
    assert hits["U01"] / total > 0.12 and hits["U10"] / total > 0.12
    assert hits["canvas"] / total > 0.1


def test_sampling_path_for_huge_pools(monkeypatch) -> None:
    """组合太多时按权重抽样再核对：照样合法、不重复、同一个种子一样。"""
    units = [
        _unit("U01", "全部动物", "character", ["E01", "E02", "E03"], "large"),
        *[
            _unit(
                f"U0{i + 2}", f"动物{i}", "character", [f"E0{i + 1}"], "medium", "U01"
            )
            for i in range(3)
        ],
    ]
    pool, _ = _pool(units, ["D09", "D13", "D20", "D23", "D37", "D41"])
    monkeypatch.setattr(subjects, "ENUM_LIMIT", 10)
    sets = subjects.pick_pair_sets(pool, 3, 4, random.Random(3))
    assert len(sets) == 4
    assert len({frozenset(p["pair_id"] for p in s) for s in sets}) == 4
    assert all(subjects.slot_ok(s) for s in sets)
    again = subjects.pick_pair_sets(pool, 3, 4, random.Random(3))
    assert [[p["pair_id"] for p in s] for s in sets] == [
        [p["pair_id"] for p in s] for s in again
    ]


def test_history_counts_pairs_and_old_fields_apart() -> None:
    records = [
        {
            "changed_fields": [
                {"field_id": "D17", "unit_key": "E01"},
                {"field_id": "D37", "unit_key": "canvas"},
            ]
        },
        {"changed_fields": [{"field_id": "D17"}, {"field_id": "D42"}]},
        {"changed_fields": [{"field_id": "D05"}]},
    ]
    used, legacy_used, done, legacy_done = subjects.history(records)
    assert used == Counter({"E01:D17": 1, "canvas:D37": 1})
    # 旧方向只按字段算，停用编号归到并入的字段
    assert legacy_used == Counter({"D17": 1, "D41": 1, "D05": 1})
    assert done == {frozenset({"E01:D17", "canvas:D37"})}
    assert legacy_done == {frozenset({"D17", "D41"}), frozenset({"D05"})}


def test_plan_hash_and_signature_stay_compatible() -> None:
    """没有单元的改动算出的计划指纹和以前一样；新方向去掉单元和旧方向一样时认得出是同一个。"""
    old = {"changed_fields": [{"field_id": "D37", "to": "极地 冷色"}], "brief": []}
    expected = sha256_text(
        canonical_json(
            {"family": "F01", "changed": [["D37", normalize_phrase("极地 冷色")]]}
        )
    )[:24]
    assert planmod.plan_hash("F01", old) == expected
    new = {
        "changed_fields": [
            {
                "field_id": "D37",
                "to": "极地冷色",
                "unit_id": "canvas",
                "unit_key": "canvas",
            }
        ],
        "brief": [{"field_id": "D37", "unit_id": "canvas", "to": "极地冷色"}],
    }
    assert planmod.plan_hash("F01", new) != expected
    old_sig, new_sig = (
        planmod.direction_signature(old),
        planmod.direction_signature(new),
    )
    assert old_sig != new_sig and planmod.unitless_signature(new_sig) == old_sig


def test_inventory_is_checked_and_shown(home, fake, seed_image, capsys) -> None:
    """P13 的单元清单：编号重复、对象不存在、组不包含成员、主体没落进单元都退回重答；卡片与命令输出列出可改的主体。"""
    ctx = _home(home, fake)

    def respond(payload: dict) -> str:
        result = p13_result(input_block(payload, "analysis"))
        if fake.stage_counts["P13"] == 1:
            result["change_units"] = [
                {
                    "unit_id": "U01",
                    "name": "星星",
                    "kind": "decoration",
                    "object_ids": ["E02", "E99"],
                    "salience": "medium",
                    "en": "stars",
                },
                {
                    "unit_id": "U01",
                    "name": "星星2",
                    "kind": "decoration",
                    "object_ids": ["E03"],
                    "parent": "U07",
                    "salience": "medium",
                    "en": "stars",
                },
            ]
        return json.dumps(envelope("P13", result), ensure_ascii=False)

    fake.overrides["P13"] = respond
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    assert fake.stage_counts["P13"] == 2
    feedback = _text(_chat_messages(fake, "P13")[-1][-1]["content"])
    assert "U01" in feedback and "E99" in feedback and "U07" in feedback
    assert "E01" in feedback  # 主体高地牛没落进任何单元
    # P13 拿到能挂在单元上的字段
    unit_fields = input_block({"messages": _chat_messages(fake, "P13")[0]}, "analysis")[
        "unit_fields"
    ]
    assert {
        "field_id": "D11",
        "label": "背景装饰",
        "applies_to": ["decoration"],
    } in unit_fields
    assert not any(f["field_id"] == "D37" for f in unit_fields)
    assert out["units"] == ["高地牛（角色·大）", "满屏星星（装饰·中）"]
    summary = bg.summarize("analyze", out)
    card = notify.bg_card(ctx, {"kind": "analyze", "subject": "S01"}, summary, ok=True)
    assert "可改的主体：高地牛（角色·大）、满屏星星（装饰·中）" in card_text(card)
    assert profile.show(ctx, "S01")["units"] == out["units"]
    assert cli.main(["seed", "show", "S01"]) == 0
    assert json.loads(capsys.readouterr().out)["description"]["units"] == out["units"]


def test_a_character_with_its_tail_is_not_a_group(home, fake, seed_image) -> None:
    """一只角色连着尾巴等对象仍是一个角色：不算角色组、不挂「距离互动」；有成员单元挂着的才是角色组。"""
    tail = {
        "object_id": "E02",
        "role": "part",
        "attributes": {"category": "尾巴"},
        "visual_function": "牛尾",
    }
    ctx = _home(home, fake, objects=[COW[0], tail, COW[2]])
    fake.p02_variable = set(UNIT_VARIABLE) | {"D23"}
    fake.p04_domains = domains((*UNIT_VARIABLE, "D23"))
    fake.p13_units = [
        {
            "unit_id": "U01",
            "name": "高地牛",
            "kind": "character",
            "object_ids": ["E01", "E02"],
            "parent": None,
            "salience": "large",
            "not_applicable": [],
            "en": "the highland cow",
        }
    ]
    family_id = _family(ctx, seed_image)
    inputs = planmod.unit_pools(ctx, planmod.planning_family(ctx, family_id))
    assert inputs["units"][0]["characters"] == 1
    assert not any(
        p["field_id"] == "D23" for pool in inputs["pairs"].values() for p in pool
    )
    assert profile.show(ctx, "S01")["units"] == ["高地牛（角色·大）"]
    assert profile.unit_lines(
        [
            {
                "unit_id": "U01",
                "name": "三只动物",
                "kind": "character",
                "object_ids": ["E01", "E02"],
                "salience": "large",
            },
            {
                "unit_id": "U02",
                "name": "小熊",
                "kind": "character",
                "object_ids": ["E02"],
                "parent": "U01",
                "salience": "medium",
            },
        ]
    ) == ["三只动物（角色组·大）", "小熊（角色·中）"]


def test_force_rewrite_keeps_the_last_correction(home, fake, seed_image) -> None:
    """--force 重写（如补可变单元清单）没写新要求时沿用上次的更正；写了新要求用新的。"""
    ctx = _home(home, fake)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    profile.describe_seed(ctx, "S01", hint="星星和月亮算一组")
    out = profile.describe_seed(ctx, "S01", force=True)
    assert out["hint"] == "星星和月亮算一组"
    assert (
        input_block({"messages": _chat_messages(fake, "P13")[-1]}, "user_hint")
        == "星星和月亮算一组"
    )
    out = profile.describe_seed(ctx, "S01", hint="小熊单独算一个")
    assert out["hint"] == "小熊单独算一个"


def test_single_subject_changes_reach_other_units(home, fake, seed_image) -> None:
    """单主体图：两处改动可以一处在牛身上、一处在星星或整幅上；每处带单元信息，简述写「主体·字段」。"""
    ctx = _home(home, fake)
    family_id = _family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    assert plan["plan_unit"] == "pairs" and plan["unit_reason"] is None
    # 高地牛 3 个字段 + 星星 1 个 + 整幅 1 个（整体配色）：小变化 10；绘制技法不进小变化，
    # 单字段大改（技法）1，三处组合池另有整幅技法：三处组合 7
    assert plan["slots"] == {"small": 10, "big": 1, "combo": 7, "total": 18}
    batch = loads(ctx.db.scalar("SELECT plan_json FROM batches"))
    assert [u["name"] for u in batch["units"]] == ["高地牛", "满屏星星"]
    assert set(batch["slot_pairs"]) == {f"S{i}" for i in range(1, 11)} | {
        f"T{i}" for i in range(1, 8)
    }
    small_fids = {
        f
        for slot, pairs in batch["slot_pairs"].items()
        if slot.startswith("S")
        for f in (p.split(":")[1] for p in pairs)
    }
    assert "D41" not in small_fids
    assert "B1" not in batch["slot_pairs"] and batch["slot_fields"]["B1"] == ["D41"]
    groups = {g["kind"]: g for g in fake.p05_groups}
    small_slots = [
        s for g in fake.p05_groups if g["kind"] == "small" for s in g["slots"]
    ]
    assert all(
        len(s["fields"]) == 2 and all("unit_id" in f for f in s["fields"])
        for s in small_slots
    )
    first = small_slots[0]["fields"][0]
    assert first["unit"]["name"] and first["small_change"]
    if first["unit_id"] != "canvas":
        assert first["unit"]["object_ids"]
    # 整幅最多一处；星星这组被挑到过
    for s in small_slots:
        assert sum(f["unit_id"] == "canvas" for f in s["fields"]) <= 1
    assert any(
        f["unit"]["name"] == "满屏星星" for s in small_slots for f in s["fields"]
    )
    assert "composition_lock" in groups["small"] and "这三处" in groups["combo"]["rule"]
    # 存下的方向：每处带单元键 / 名字 / 英文点名，简述写「主体·字段」，变化轴带主体名
    stored = [
        loads(r["plan_json"])
        for r in ctx.db.q("SELECT plan_json FROM variants ORDER BY variant_id")
    ]
    cow = next(
        p
        for p in stored
        if any(cf.get("unit_name") == "高地牛" for cf in p["changed_fields"])
    )
    cf = next(c for c in cow["changed_fields"] if c.get("unit_name") == "高地牛")
    assert cf["unit_key"] == "E01" and cf["unit_en"] == "the baby animal e01"
    brief = next(b for b in cow["brief"] if b.get("unit_id") == "U01")
    assert brief["aspect"].startswith("高地牛·")
    assert planmod.brief_line(brief).startswith("高地牛·")
    assert "高地牛·" in cow["axis"]
    canvas = next(
        c for p in stored for c in p["changed_fields"] if c.get("unit_id") == "canvas"
    )
    assert canvas["unit_key"] == "canvas" and "unit_en" not in canvas
    # 方位仍是维度名（选款表的多选列）
    assert all(
        set(p["aspects"])
        <= {"元素角色", "表情动作", "服饰道具", "配色结构", "绘制技法"}
        for p in stored
    )
    # 再策划：做过的组合与方向避开（换个说法也认得出）
    _, coverage, _ = planmod._history(ctx, family_id)
    assert sum(coverage["count_by_pair"].values()) > 0


def test_unit_slots_are_checked_by_unit_and_field(home, fake, seed_image) -> None:
    """没写 unit_id 但字段在槽位里只有一处时按字段对上；unit_id 写错算少了一处；小变化多写的改动丢掉。"""
    ctx = _home(home, fake)
    family_id = _family(ctx, seed_image)

    def tasks(group: dict):
        if group["kind"] != "small":
            return None
        out = []
        for slot in group["slots"]:
            changes = [
                {
                    "unit_id": f["unit_id"],
                    "field_id": f["field_id"],
                    "from": "原样",
                    "to": f"{slot['slot_id']}{f['label']}新样子",
                }
                for f in slot["fields"]
            ]
            if slot["slot_id"] == "S1":
                changes = [
                    {k: v for k, v in c.items() if k != "unit_id"} for c in changes
                ]
            if slot["slot_id"] == "S2":
                changes[0]["unit_id"] = "U99"
            if slot["slot_id"] == "S3":
                changes.append({"field_id": "D05", "from": "原样", "to": "海洋"})
            out.append(
                {
                    "slot_id": slot["slot_id"],
                    "title": f"方向{slot['slot_id']}",
                    "brief": changes,
                    "keep": "构图",
                    "changed_fields": changes,
                    "acceptance_criteria": ["看得出"],
                }
            )
        return out

    fake.p05_tasks = tasks
    plan = planmod.plan_batch(ctx, family_id, kinds=["small"])
    by_slot = {a["slot_id"]: a for a in plan["accepted"]}
    assert "S1" in by_slot and all(
        cf.get("unit_key") for cf in by_slot["S1"]["changed_fields"]
    )
    assert all(b.get("unit_id") for b in by_slot["S1"]["brief"])
    assert "S2" not in by_slot
    assert (
        next(d for d in plan["dropped"] if d["slot_id"] == "S2")["reason"]
        == "slot_mismatch"
    )
    s3 = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM variants WHERE variant_id = ?",
            (by_slot["S3"]["variant_id"],),
        )
    )
    assert s3["extra_fields_dropped"] == ["D05"] and len(s3["changed_fields"]) == 2


def test_old_directions_still_block_new_ones(home, fake, seed_image) -> None:
    """没有单元清单时这一轮按字段策划并在卡片上说明；之后按单元策划，去掉单元和旧方向一样的仍算做过。"""
    ctx = _home(home, fake)
    family_id = _family(ctx, seed_image)
    # 把画面描述改成旧版（没有单元清单）
    row = ctx.db.q1("SELECT analysis_id, result_json FROM seed_descriptions")
    desc = loads(row["result_json"])
    old = {k: v for k, v in desc.items() if k != "change_units"}

    def save(result: dict) -> None:
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE seed_descriptions SET result_json = ? WHERE analysis_id = ?",
                (json.dumps(result, ensure_ascii=False), row["analysis_id"]),
            )

    save(old)
    assert subjects.missing_inventory(ctx) == ["S01"]
    check = doctor._unit_checks(ctx)[0]
    assert not check["ok"] and "S01" in check["detail"]

    def same(group: dict) -> list[dict]:
        # 每处都改成「<字段>同样的样子」：两轮里字段组合相同的方向就是同一个
        out = []
        for s in group["slots"]:
            changes = [
                {
                    **({"unit_id": f["unit_id"]} if f.get("unit_id") else {}),
                    "field_id": f["field_id"],
                    "from": "原样",
                    "to": f"{f['field_id']}同样的样子",
                }
                for f in s["fields"]
            ]
            out.append(
                {
                    "slot_id": s["slot_id"],
                    "title": "同样的方向",
                    "brief": changes,
                    "changed_fields": changes,
                    "acceptance_criteria": ["看得出"],
                }
            )
        return out

    fake.p05_tasks = same
    first = planmod.plan_batch(ctx, family_id, kinds=["small"])
    assert (
        first["plan_unit"] == "fields" and "还没有可变单元清单" in first["unit_reason"]
    )
    assert first["accepted"] and all(
        "unit_key" not in cf for a in first["accepted"] for cf in a["changed_fields"]
    )
    card = notify.bg_card(
        ctx,
        {"kind": "plan", "subject": family_id},
        bg.summarize("plan", first),
        ok=True,
    )
    assert "还没有可变单元清单" in card_text(card)
    from podflow import directions

    directions.reject(ctx, first["batch_id"], by="tester", notify=False)
    old_sets = {
        frozenset(cf["field_id"] for cf in a["changed_fields"])
        for a in first["accepted"]
    }
    save(desc)
    second = planmod.plan_batch(ctx, family_id, kinds=["small"])
    assert second["plan_unit"] == "pairs"
    dup = [d for d in second["dropped"] if d["reason"] == "duplicate_direction"]
    assert dup and all(
        frozenset(cf["field_id"] for cf in d["changed_fields"]) in old_sets for d in dup
    )
    assert not any(
        frozenset(cf["field_id"] for cf in a["changed_fields"]) in old_sets
        for a in second["accepted"]
    )


def test_plan_unit_fields_falls_back(home, fake, seed_image, capsys) -> None:
    """plan.unit 是 fields：按字段策划（槽位没有单元），doctor 提示；--preview 只列槽位不建批次。"""
    ctx = _home(home, fake)
    family_id = _family(ctx, seed_image)
    # 预览：不调 P05、不建批次，列出单元和每个槽位改哪几处
    before = fake.stage_counts["P05"]
    assert cli.main(["plan", "--family", family_id, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["plan_unit"] == "pairs" and preview["units"] == [
        "高地牛（角色·大）",
        "满屏星星（装饰·中）",
    ]
    assert preview["counts_text"] == "小变化 10 · 大变化 8（单字段大改 1 + 三处组合 7）"
    assert (
        preview["pairs"] == {"small": 5, "combo": 6} and preview["layout_fields"] == []
    )
    assert (
        preview["slots"][0]["kind"] == "小变化"
        and len(preview["slots"][0]["changes"]) == 2
    )
    assert (
        fake.stage_counts["P05"] == before
        and ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    )
    set_config(home, "plan.unit", "fields")
    ctx = reopen(home)
    check = next(c for c in doctor._unit_checks(ctx))
    assert not check["ok"] and check["level"] == "warn"
    plan = planmod.plan_batch(ctx, family_id, kinds=["small"])
    assert plan["plan_unit"] == "fields" and "plan.unit" in plan["unit_reason"]
    assert all(
        "unit_id" not in f
        for g in fake.p05_groups[-1:]
        for s in g["slots"]
        for f in s["fields"]
    )
    set_config(home, "plan.unit", "bogus")
    ctx = reopen(home)
    assert not next(c for c in doctor._unit_checks(ctx))["ok"]


def test_p06_names_the_unit(home, fake, seed_image) -> None:
    """出图指令拿到每处改动的单元名与英文点名（unit_en），用它指明改哪里。"""
    ctx = _home(home, fake)
    family_id = _family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=1)
    from conftest import confirm_all

    confirm_all(ctx, plan)
    Worker(ctx, max_minutes=5).run()
    variant = input_block({"messages": _chat_messages(fake, "P06")[-1]}, "variant_job")
    named = [
        cf
        for cf in variant["changed_fields"]
        if cf.get("unit_id") not in (None, "canvas")
    ]
    assert named and all(cf["unit_en"] and cf["unit_name"] for cf in named)
    assert variant["task_type"] == "small" and variant["composition_lock"]
