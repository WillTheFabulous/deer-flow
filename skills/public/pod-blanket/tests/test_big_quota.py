"""大变化的动态字段与配额（2026-10-27）：动作姿态 / 朝向视线 / 距离互动 / 承托依附 / 陪衬角色也进单字段大改（大变化里
可以连带调整布局），老系列被判不能变的由程序放进大变化；单字段大改 / 三处组合 / 二次大变化按配额出动态与换画法的方向，
换画法的槽位由程序各分一种不重复的画法大类（technique_to）；系列规则里的锁定只认用户、修订意见与印制要求。"""

from __future__ import annotations

import random

import pytest
from conftest import domains, p05_task, reopen, set_config
from podflow import doctor, fields
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.util import loads
from test_derived_big import _derived_ctx, _small_round, spawned  # noqa: F401
from test_subject_pairs import UNIT_VARIABLE, _family, _home, _info, _unit

DYNAMIC = ("D10", "D18", "D19", "D23", "D25")
COW = _unit("U01", "高地牛", "character", ["E01"], "large")
STARS = _unit("U02", "星星", "decoration", ["E02", "E03"], "medium")


def _quota_home(home, fake, extra=("D05", "D27", "D10", "D18", "D23")):
    """单主体图（高地牛 + 一组星星）：在常用可变字段上再加几个大字段 / 动态字段；不出小变化。"""
    _home(home, fake)
    variable = (*UNIT_VARIABLE, *extra)
    fake.p02_variable = set(variable)
    fake.p04_domains = domains(variable)
    set_config(home, "plan.small_count", 0)


def _technique_tasks(texts: list[str] | None = None):
    """P05：单字段大改 / 二次大变化里换画法那处写「整幅换成<technique_to>」（给了 texts 就按顺序用它），
    其余处写短名 + 槽位号。"""
    queue = list(texts or [])

    def tasks(group: dict) -> list[dict]:
        out = []
        for slot in group["slots"]:
            changes = []
            for f in slot["fields"]:
                if f["field_id"] == "D41" and group["kind"] in ("big", "derived"):
                    to = queue.pop(0) if queue else f"整幅换成{f.get('technique_to')}"
                else:
                    to = f"{f['label']}{slot['slot_id']}"
                changes.append(
                    (f["field_id"], to, f["unit_id"])
                    if f.get("unit_id")
                    else (f["field_id"], to)
                )
            out.append(
                p05_task(slot["slot_id"], changes, title=f"方向{slot['slot_id']}")
            )
        return out

    return tasks


def test_dynamic_fields_also_go_into_single_big_changes() -> None:
    """动态字段三类方向都能进、写了大改法；绘制技法只进大变化，换主体只进小变化与组合；画法大类 23 种。"""
    for fid in DYNAMIC:
        assert fields.slot_kinds(fid) == ["small", "big", "combo"], fid
        assert (
            fields.is_dynamic(fid)
            and fields.big_change(fid)
            and fields.small_change(fid)
        )
    assert fields.slot_kinds("D41") == ["big", "combo"] and not fields.is_dynamic("D41")
    assert fields.slot_kinds("D09") == ["small", "combo"] and not fields.big_change(
        "D09"
    )
    assert not fields.is_dynamic("D05") and not fields.is_dynamic("D14")
    families = fields.technique_families()
    assert len(families) == 23 and families[0].startswith("水彩")


def test_big_pool_needs_a_unit_for_dynamic_fields() -> None:
    """大字段池里的动态字段要找得到能挂的单元：单只角色不出距离互动，没有承托物不出承托依附，单元否决的不出；
    陪衬角色挂整幅，主题这类大字段不看单元。"""
    pool = [_info(f) for f in ("D05", "D10", "D18", "D23", "D25")]

    def keep(units: list[dict]) -> list[str]:
        return [i["field_id"] for i in planmod.big_applicable(pool, units)]

    assert keep([COW, STARS]) == ["D05", "D10", "D18"]
    group = [
        _unit("U01", "全部动物", "character", ["E01", "E02"], "large"),
        _unit("U02", "小熊", "character", ["E01"], "medium", parent="U01"),
        _unit("U03", "小兔", "character", ["E02"], "medium", parent="U01"),
        _unit("U04", "枝条", "prop", ["E05"], "medium"),
    ]
    assert keep(group) == ["D05", "D10", "D18", "D23", "D25"]
    denied = _unit(
        "U01", "高地牛", "character", ["E01"], "large", denied={"D18": "看不到身体"}
    )
    assert keep([denied, STARS]) == ["D05", "D10"]


def test_quota_split_fills_short_groups() -> None:
    """配额分组：各组按比例、互不重叠；一组凑不够先由其余组补、再按顺序由配额组补；都不够时不凑数；没配额只有一组。"""
    ratios = {"dynamic": 0.34, "technique": 0.34}

    def run(caps: dict[str, int]) -> dict[str, int]:
        groups = planmod.quota_split(
            12, ratios, lambda limits, name, target: caps[name]
        )
        return {g["quota"]: g["count"] for g in groups}

    assert run({"dynamic": 9, "technique": 9, "rest": 9}) == {
        "dynamic": 4,
        "technique": 4,
        "rest": 4,
    }
    assert run({"dynamic": 0, "technique": 9, "rest": 9}) == {
        "dynamic": 0,
        "technique": 4,
        "rest": 8,
    }
    assert run({"dynamic": 9, "technique": 9, "rest": 1}) == {
        "dynamic": 7,
        "technique": 4,
        "rest": 1,
    }
    assert run({"dynamic": 1, "technique": 2, "rest": 3}) == {
        "dynamic": 1,
        "technique": 2,
        "rest": 3,
    }
    (only,) = planmod.quota_split(5, {}, lambda limits, name, target: 3)
    assert (only["quota"], only["count"], only["limits"]) == ("rest", 3, {})
    # 互不重叠：技法组排除动态，其余组两类都排除
    groups = planmod.quota_split(12, ratios, lambda limits, name, target: 12)
    dyn, tech, rest = (g["limits"] for g in groups)
    d18, d41, d05 = _info("D18"), _info("D41"), _info("D05")
    assert dyn["must"](d18) and not dyn["must"](d41) and "forbid" not in dyn
    assert tech["must"](d41) and tech["forbid"](d18) and not tech["forbid"](d41)
    assert rest["forbid"](d18) and rest["forbid"](d41) and not rest["forbid"](d05)


def test_quota_settings_are_checked(home) -> None:
    """配额写错（合计超过 1、不认识的类、不是数、超出 0–1、不是字典）报配置错，doctor 也报；写 0 的类不列。"""
    for bad in (
        {"dynamic": 0.7, "technique": 0.5},
        {"motion": 0.2},
        {"dynamic": "很多"},
        {"technique": 1.5},
        "half",
    ):
        set_config(home, "plan.combo_quota", bad)
        with pytest.raises(planmod.ConfigError, match="plan.combo_quota"):
            planmod.quota_ratios(reopen(home), "plan.combo_quota")
        check = doctor._swap_checks(reopen(home))[0]
        assert check["ok"] is False and "plan.combo_quota" in check["detail"]
    set_config(home, "plan.combo_quota", {"dynamic": 0.5, "technique": 0})
    assert planmod.quota_ratios(reopen(home), "plan.combo_quota") == {"dynamic": 0.5}
    set_config(home, "plan.big_single_quota", {"dynamic": 0.34, "technique": 0.34})
    detail = doctor._swap_checks(reopen(home))[0]["detail"]
    assert "单字段大改：动态 34%、换画法 34%" in detail
    assert "三处组合：含动态 50%" in detail and "换画法的方向按清单" in detail


def test_old_judgements_open_dynamic_fields_for_big_changes(
    home, fake, seed_image
) -> None:
    """老系列被判不能变的动态字段（开着 combo_layout_fields）：进单字段大改并带 layout_change，小变化不收；
    单只角色、没有承托物的图照样不出距离互动、承托依附；关掉开关就不收。"""
    _home(home, fake)
    set_config(home, "plan.combo_layout_fields", True)
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    inputs = planmod.unit_pools(ctx, planmod.planning_family(ctx, family_id))
    big = {f["field_id"]: f for f in inputs["pools"]["big"]}
    assert set(big) == {"D10", "D18", "D19", "D41"}
    assert all(big[f].get("layout_change") for f in ("D10", "D18", "D19"))
    assert not big["D41"].get("layout_change")
    assert not any(fields.is_dynamic(f["field_id"]) for f in inputs["pools"]["small"])
    set_config(home, "plan.combo_layout_fields", False)
    ctx = reopen(home)
    inputs = planmod.unit_pools(ctx, planmod.planning_family(ctx, family_id))
    assert [f["field_id"] for f in inputs["pools"]["big"]] == ["D41"]


def test_single_big_quota_and_technique_roster(home, fake, seed_image) -> None:
    """单字段大改按配额：动态 2、换画法 2、其余 2；单只角色的图不出距离互动；两个换画法的槽位各分一种不重复的
    画法（不分原图同类的水彩），P05 拿到 technique_to / technique_spare 与动态字段的大改法；批次与方向记下分到的
    画法，下一轮避开做过的。"""
    _quota_home(home, fake)
    set_config(home, "plan.big_count", 6)
    set_config(home, "plan.big_single_count", 6)
    set_config(home, "plan.big_single_repeat", 2)
    set_config(home, "plan.big_single_quota", {"dynamic": 0.34, "technique": 0.34})
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    preview = planmod.preview_slots(ctx, family_id)
    assert preview["counts"]["big"] == 6
    assert preview["counts"]["big_quota"] == {"dynamic": 2, "technique": 2, "rest": 2}
    assert preview["big_quota"] == "动态 2 · 换画法 2 · 其余 2"
    assert preview["dynamic_fields"] == ["陪衬角色", "动作姿态"]
    tags = [s.get("quota") for s in preview["slots"]]
    assert tags.count("dynamic") == 2 and tags.count("technique") == 2
    planned = list(preview["techniques"].values())
    assert len(planned) == 2 == len(set(planned))
    assert not any(t.startswith("水彩") for t in planned)

    fake.p05_tasks = _technique_tasks()
    plan = planmod.plan_batch(ctx, family_id)
    assert plan["counts"] == {"big": 6}
    slots = [s for g in fake.p05_groups if g["kind"] == "big" for s in g["slots"]]
    spots = {s["slot_id"]: s["fields"][0] for s in slots}
    tech = [f for f in spots.values() if f["field_id"] == "D41"]
    assigned = set(plan["technique"]["assigned"].values())
    assert len(tech) == 2 and {f["technique_to"] for f in tech} == assigned
    assert all(
        f["technique_spare"] and not set(f["technique_spare"]) & assigned for f in tech
    )
    dyn = [f for f in spots.values() if fields.is_dynamic(f["field_id"])]
    assert len(dyn) == 2 and all(f["big_change"] for f in dyn)
    assert "D23" not in {f["field_id"] for f in spots.values()}
    assert not plan["technique"]["off_roster"]
    stored = {
        loads(r["plan_json"]).get("technique")
        for r in ctx.db.q("SELECT plan_json FROM variants")
    } - {None}
    assert stored == assigned
    batch = loads(
        ctx.db.q1(
            "SELECT plan_json FROM batches WHERE batch_id = ?", (plan["batch_id"],)
        )["plan_json"]
    )
    assert batch["technique"]["assigned"] == plan["technique"]["assigned"]
    assert batch["slots"]["big_quota"] == {"dynamic": 2, "technique": 2, "rest": 2}
    # 下一轮避开做过的画法
    again = planmod.preview_slots(ctx, family_id)
    assert again["techniques"] and not set(again["techniques"].values()) & stored


def test_counts_match_slots_when_only_technique_is_left(home, fake, seed_image) -> None:
    """只剩绘制技法能大改、又凑不成三处组合（抽象花型这类图）：换画法组的上限按 big_single_count 算（0.34 × 12 → 4），
    统计出的个数和真正挑出的槽位一致，每个都分到不同的画法。"""
    _home(home, fake)
    fake.p02_variable = {"D37", "D41"}
    fake.p04_domains = domains(("D37", "D41"))
    set_config(home, "plan.big_count", 24)
    set_config(home, "plan.big_single_count", 12)
    set_config(home, "plan.big_single_repeat", 2)
    set_config(home, "plan.big_single_quota", {"technique": 0.34})
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    preview = planmod.preview_slots(ctx, family_id)
    big = [s for s in preview["slots"] if s["kind"] == "单字段大改"]
    assert preview["counts"]["big"] == len(big) == 4
    assert preview["counts"]["combo"] == 0
    assert preview["counts"]["big_quota"] == {"technique": 4, "rest": 0}
    assert len(set(preview["techniques"].values())) == 4


def test_same_technique_twice_is_dropped(home, fake, seed_image) -> None:
    """P05 把两个换画法的槽位写成同一种画法（说法不同，计划指纹不重复）：后出来的丢掉（duplicate_technique）。"""
    _quota_home(home, fake)
    set_config(home, "plan.big_count", 2)
    set_config(home, "plan.big_single_count", 2)
    set_config(home, "plan.big_single_quota", {"technique": 1.0})
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    fake.p05_tasks = _technique_tasks(
        ["整幅换成剪纸拼贴，暖色纸边", "整幅换成层叠纸雕拼贴"]
    )
    plan = planmod.plan_batch(ctx, family_id)
    reasons = [d["reason"] for d in plan["dropped"]]
    assert reasons.count("duplicate_technique") == 1 and plan["counts"] == {"big": 1}
    (kept,) = [
        loads(r["plan_json"]) for r in ctx.db.q("SELECT plan_json FROM variants")
    ]
    assert kept["technique"].startswith("剪纸")


def test_technique_roster_avoids_history_and_the_reference() -> None:
    """换画法清单：不分原图同类的画法；没做过的先分、做过的排在后面；同一个种子能复现；槽位比画法多时多出来的
    只给备选。"""

    def slot(i: int) -> dict:
        return {
            "slot_id": f"B{i}",
            "kind": "big",
            "field_ids": ["D41"],
            "fields": [_info("D41")],
        }

    profile = {"style": {"genre": "儿童水彩绘本", "coloring": "湿画法晕染"}}
    families = fields.technique_families()
    paper = next(f for f in families if f.startswith("剪纸"))
    records = [
        {
            "kind": "big",
            "task_type": "big",
            "changed_fields": [{"field_id": "D41", "to": "整幅换成剪纸拼贴"}],
            "brief": [{"field_id": "D41", "to": "剪纸拼贴"}],
        }
    ]
    assert planmod.techniques_done(records) == {paper: 1}
    slots = [slot(i) for i in range(1, 4)]
    info = planmod.assign_techniques(slots, records, profile, "F01:B001:technique")
    picked = list(info["assigned"].values())
    assert len(set(picked)) == 3 and info["reference"] == [families[0]]
    assert families[0] not in picked and paper not in picked
    assert all(
        s["technique"]["technique_to"] == info["assigned"][s["slot_id"]] for s in slots
    )
    assert len(info["spare"]) == planmod.TECHNIQUE_SPARE
    assert not set(info["spare"]) & set(picked)
    again = planmod.assign_techniques(
        [slot(i) for i in range(1, 4)], records, profile, "F01:B001:technique"
    )
    assert again["assigned"] == info["assigned"]
    # 做过的排在最后：没做过的分完了才轮到它
    crowd = [slot(i) for i in range(1, 23)]
    info = planmod.assign_techniques(crowd, records, profile, "F01:B002:technique")
    assert info["assigned"]["B22"] == paper
    # 槽位比画法多：分完 22 种（去掉原图同类），剩下的不分
    many = [slot(i) for i in range(1, 31)]
    info = planmod.assign_techniques(many, [], profile, "x")
    assert len(info["assigned"]) == len(families) - 1
    assert "technique_to" not in many[-1]["technique"]
    # 不是只改绘制技法的槽位不分
    assert (
        planmod.assign_techniques([{**slot(1), "kind": "combo"}], [], profile, "x")
        == {}
    )


def test_combo_quota_groups_stay_apart(home, fake, seed_image) -> None:
    """三处组合按配额：含动态的 4 个、含技法调整（不含动态）的 2 个、其余 2 个两类都不含，组合互不重复。"""
    _quota_home(home, fake, extra=("D10", "D18"))
    set_config(home, "plan.big_count", 8)
    set_config(home, "plan.big_single_count", 0)
    set_config(home, "plan.combo_quota", {"dynamic": 0.5, "technique": 0.25})
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    fam = planmod.planning_family(ctx, family_id)
    for seed in range(6):
        out = planmod.plan_slots(ctx, fam, rng=random.Random(seed))
        assert out["counts"]["combo"] == 8
        assert out["counts"]["combo_quota"] == {
            "dynamic": 4,
            "technique": 2,
            "rest": 2,
        }
        combos = [s for s in out["slots"] if s["kind"] == "combo"]
        for s in combos:
            dyn = any(fields.is_dynamic(f) for f in s["field_ids"])
            tech = "D41" in s["field_ids"]
            if s.get("quota") == "dynamic":
                assert dyn
            elif s.get("quota") == "technique":
                assert tech and not dyn
            else:
                assert not dyn and not tech, (seed, s["field_ids"])
        assert [s.get("quota") for s in combos].count("dynamic") == 4
        assert len({frozenset(p["pair_id"] for p in s["pairs"]) for s in combos}) == 8


@pytest.mark.usefixtures("spawned")
def test_derived_quota_and_technique(home, fake, seed_image) -> None:
    """二次大变化按配额：一个换画法（分到一种画法）、一个动态字段；批次记下配额与换画法清单。"""
    _derived_ctx(home, fake, count=2)
    variable = (*UNIT_VARIABLE, "D18")
    fake.p02_variable = set(variable)
    fake.p04_domains = domains(variable)
    set_config(home, "plan.derived.quota", {"dynamic": 0.5, "technique": 0.5})
    ctx = reopen(home)
    _, plan = _small_round(ctx, fake, seed_image)
    preview = planmod.preview_derived(ctx, plan["batch_id"])
    by_quota = {s.get("quota"): s for s in preview["slots"]}
    assert set(by_quota) == {"dynamic", "technique"}
    assert (
        by_quota["technique"]["field"] == "绘制技法"
        and by_quota["technique"]["technique_to"]
    )
    assert by_quota["dynamic"]["field"] == "动作姿态"
    fake.p05_tasks = _technique_tasks()
    out = planmod.plan_derived(ctx, plan["batch_id"])
    assert out["counts"] == {"derived": 2}
    batch = loads(
        ctx.db.q1(
            "SELECT plan_json FROM batches WHERE batch_id = ?", (out["batch_id"],)
        )["plan_json"]
    )
    assert batch["derived_quota"] == {"dynamic": 1, "technique": 1}
    assert len(batch["technique"]["assigned"]) == 1
    assert out["technique"]["assigned"] == batch["technique"]["assigned"]


def test_locks_need_a_user_or_print_source() -> None:
    """系列规则的锁定只认用户 / 修订意见 / 印制要求：没写来源或来源不对的按设计建议处理，放回变化域并记一条说明。"""
    variation = {
        "traits": {},
        "gated_off": {},
        "variable": [{"field_id": f} for f in ("D06", "D09", "D37")],
        "not_variable": [],
    }
    spec = {
        "variable_domains": [{"field_id": "D37", "options": ["冬日冷色"]}],
        "locked_fields": [
            {"field_id": "D06", "reason": "建议保持静态肖像"},
            {
                "field_id": "D09",
                "source": "user_positioning",
                "reason": "用户要求保留原主体",
            },
            {"field_id": "D37", "source": "设计建议", "reason": "配色保持"},
        ],
    }
    out = seedmod.normalize_family_spec(spec, variation)
    assert [x["field_id"] for x in out["locked_fields"]] == ["D09"]
    assert out["locked_fields"][0]["source"] == "user_positioning"
    assert [d["field_id"] for d in out["variable_domains"]] == ["D06", "D37"]
    assert out["variable_domains"][1]["options"] == ["冬日冷色"]
    notes = out["variation"]["notes"]
    assert sum("按设计建议处理" in n for n in notes) == 2
    assert not any("漏了" in n for n in notes)
