"""变体规则：拆解的可变判断与门控、随机挑字段出方向（小变化两字段 / 单字段大改 / 三处组合）的槽位规则，
小变化只做图生图，P06 按模型家族与出图方式分发提示词。"""

from __future__ import annotations

import base64
import json
import random
from collections import Counter

import pytest
from conftest import (
    EDIT_MODELS,
    FANOUT_MODELS,
    confirm_all,
    domains,
    envelope,
    input_block,
    p05_task,
    reopen,
    set_config,
    use_small,
)
from podflow import bg, directions, doctor, fields, images, notify, ui
from podflow.config import ConfigError
from podflow.stages import job as jobmod
from podflow.stages.common import (
    StageError,
    fanout_slots,
    generation_modes,
    small_change_modes,
    variant_modes,
)
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.stages.feedback import finished_batches
from podflow.util import dumps, loads
from podflow.worker import Worker
from test_pipeline import bootstrap


def _approved_family(ctx, seed_image) -> str:
    seed = seedmod.add_seed(ctx, seed_image)
    analysis = seedmod.analyze(ctx, seed["seed_id"])
    family_id = analysis["steps"][-1]["family_id"]
    seedmod.decide_family(ctx, family_id, approve=True, by="tester")
    return family_id


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))


def _chat_messages(fake, stage: str) -> list[list[dict]]:
    """某阶段每次请求的 messages（请求体里的中文是转义过的，要先解析）。"""
    out = []
    for r in fake.requests:
        if r["path"] != "/v1/chat/completions":
            continue
        messages = json.loads(r["body"])["messages"]
        if f"# 当前阶段：{stage}" in _text(messages[0]["content"]):
            out.append(messages)
    return out


def test_analysis_judges_variable_fields_and_gates_dimensions(
    ctx, fake, seed_image
) -> None:
    """拆解判断哪些字段能变：单幅场景关掉排列重复、没有文字关掉文字定制；系列变化域只留可变字段。"""
    fake.p02_variable = {"D09", "D33", "D41", "D49"}
    # 系列规则多写了不可变的 D53、单幅图上的 D33，漏了 D41；锁定的 D49 被门控关掉了
    fake.p04_domains = domains(["D09", "D33", "D53"])
    fake.p04_locked = [{"field_id": "D49", "reason": "文字保持原样"}]
    seed = seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, seed["seed_id"])
    analysis = loads(seedmod.latest_analysis(ctx, "S01")["result_json"])
    variation = analysis["variation"]
    assert analysis["field_schema"] == "0.5"
    assert variation["traits"] == {"layout_kind": "scene", "has_text": False}
    assert [v["field_id"] for v in variation["variable"]] == ["D09", "D41"]
    gated = {v["field_id"]: v for v in variation["not_variable"] if v.get("gated")}
    assert (
        gated["D33"]["reason"].startswith("单幅场景")
        and gated["D49"]["reason"] == "图上没有文字"
    )
    # 停用字段不再输出（含并进绘制技法的 D42–D44），只剩 40 个在用字段
    assert len(analysis["fields"]) == 40 and not {"D12", "D42"} & {
        f["field_id"] for f in analysis["fields"]
    }
    # P04 看得到可变 / 不可变清单，绘制技法带着技法变化清单
    p04_text = _text(_chat_messages(fake, "P04")[-1][1]["content"])
    assert "variable_fields" in p04_text and "图上没有文字" in p04_text
    assert "change_forms" in p04_text
    spec = loads(ctx.db.scalar("SELECT spec_json FROM families"))
    assert spec["field_schema"] == "0.5"
    assert [d["field_id"] for d in spec["variable_domains"]] == ["D09", "D41"]
    assert spec["locked_fields"] == []
    notes = " ".join(spec["variation"]["notes"])
    assert "D33" in notes and "D53" in notes and "补上" in notes and "D49" in notes


def test_analysis_without_variable_judgments_is_asked_again(
    ctx, fake, seed_image
) -> None:
    calls = {"n": 0}
    original = fake.p02_variable

    def p02(payload):
        calls["n"] += 1
        from conftest import default_result

        result = default_result("P02", payload, fake)
        if calls["n"] == 1:
            for f in result["fields"]:
                f.pop("variable", None)
        return json.dumps(envelope("P02", result), ensure_ascii=False)

    fake.overrides["P02"] = p02
    fake.p02_variable = original
    seed = seedmod.add_seed(ctx, seed_image)
    seedmod.run_s01(ctx, seed["seed_id"])
    seedmod.run_s02(ctx, seed["seed_id"])
    assert calls["n"] == 2
    retry = _text(_chat_messages(fake, "P02")[-1][-1]["content"])
    assert "缺少 variable" in retry


def test_slots_pick_random_field_sets(home, fake, seed_image) -> None:
    """小变化随机两个小变化字段、单字段大改一个大字段、三处组合三个字段（绘制技法也能进）；同一个种子结果一样。"""
    use_small(fake)
    ctx = reopen(home)
    family_id = _approved_family(ctx, seed_image)
    fam = planmod.planning_family(ctx, family_id)
    slots = planmod.plan_slots(ctx, fam)
    got = [(s["slot_id"], s["kind"], sorted(s["field_ids"])) for s in slots["slots"]]
    assert [g[:2] for g in got] == [
        ("S1", "small"),
        ("S2", "small"),
        ("S3", "small"),
        ("B1", "big"),
        ("T1", "combo"),
        ("T2", "combo"),
        ("T3", "combo"),
        ("T4", "combo"),
    ]
    # 三个小变化字段两两搭配正好 3 组（绘制技法不进小变化），同一批里不重复
    assert sorted(g[2] for g in got[:3]) == [
        ["D09", "D17"],
        ["D09", "D37"],
        ["D17", "D37"],
    ]
    # 四个字段（含绘制技法）挑三个正好 4 组三处组合
    assert got[3][2] == ["D41"]
    assert sorted(g[2] for g in got[4:]) == [
        ["D09", "D17", "D37"],
        ["D09", "D17", "D41"],
        ["D09", "D37", "D41"],
        ["D17", "D37", "D41"],
    ]
    assert slots["counts"] == {"small": 3, "big": 1, "combo": 4, "total": 8}
    by_id = {f["field_id"]: f for s in slots["slots"] for f in s["fields"]}
    assert (
        by_id["D09"]["current"] == "D09 的取值" and by_id["D41"]["label"] == "绘制技法"
    )
    assert (
        planmod.counts_text(slots["counts"])
        == "小变化 3 · 大变化 5（单字段大改 1 + 三处组合 4）"
    )
    assert planmod.plan_button_label(slots["counts"]) == "策划 8 个方向"
    again = planmod.plan_slots(ctx, fam, rng=random.Random(0))
    assert [s["field_ids"] for s in again["slots"]] == [
        s["field_ids"] for s in slots["slots"]
    ]
    # 锁定的字段哪类都不进：锁了绘制技法，没有大字段，三处组合只剩一组
    fam["spec"]["locked_fields"] = [{"field_id": "D41", "reason": "画法保持"}]
    slots = planmod.plan_slots(ctx, fam)
    assert [(s["slot_id"], sorted(s["field_ids"])) for s in slots["slots"]][3:] == [
        ("T1", ["D09", "D17", "D37"])
    ]
    assert slots["counts"] == {"small": 3, "big": 0, "combo": 1, "total": 4}
    assert planmod.counts_text(slots["counts"]) == "小变化 3 · 大变化 1（三处组合 1）"


def _pool(dims: list[str]) -> list[dict[str, str]]:
    return [{"field_id": f"X{i:02d}", "dimension": d} for i, d in enumerate(dims)]


def test_pool_counts_follow_config_and_fall_back(home) -> None:
    """默认小变化 12、大变化 8（单字段 4 + 三字段 4）；字段池不够时不凑数，大字段和三处组合互相补位。"""
    ctx = reopen(home)

    def counts(n_small: int, n_big: int) -> dict[str, int]:
        small = _pool([f"s{i}" for i in range(n_small)])
        pools = {
            "small": small,
            "big": _pool([f"b{i}" for i in range(n_big)]),
            "combo": small,
        }
        return planmod.pool_counts(ctx, pools)

    assert counts(10, 8) == {"small": 12, "big": 4, "combo": 4, "total": 20}
    # 大字段只有 1 个：三处组合补到 7 个
    assert counts(10, 1) == {"small": 12, "big": 1, "combo": 7, "total": 20}
    # 小变化字段只有 3 个：两两搭配 3 组、三处组合 1 组，单字段大改补到 7 个
    assert counts(3, 8) == {"small": 3, "big": 7, "combo": 1, "total": 11}
    assert counts(1, 0) == {"small": 0, "big": 0, "combo": 0, "total": 0}
    set_config(home, "plan.small_count", 6)
    set_config(home, "plan.big_count", 4)
    set_config(home, "plan.big_single_count", 0)
    ctx = reopen(home)
    assert counts(10, 8) == {"small": 6, "big": 0, "combo": 4, "total": 10}


def test_pick_sets_spread_fields_and_avoid_repeats() -> None:
    """随机挑字段组合：同一批不重复；同维度的、做过的组合、常被改的字段都往后排。"""
    pool = _pool(["d1", "d1", "d2", "d3", "d4"])
    every = planmod._pick_sets(pool, 2, 99, random.Random(1), {}, set())
    assert (
        len(every) == 10
        and len({frozenset(f["field_id"] for f in s) for s in every}) == 10
    )

    def tally(seeds: range, **kw) -> Counter:
        hits: Counter = Counter()
        for seed in seeds:
            (picked,) = planmod._pick_sets(pool, 2, 1, random.Random(seed), **kw)
            hits[frozenset(f["field_id"] for f in picked)] += 1
        return hits

    seeds = range(300)
    same_dim = frozenset({"X00", "X01"})
    # 均匀挑时每组约 30 次；同维度的那组权重只有 0.15
    assert tally(seeds, used={}, done=set())[same_dim] < 12
    done = frozenset({"X02", "X03"})
    assert tally(seeds, used={}, done={done})[done] < 15
    # 被改过 9 次的字段权重 1/10：均匀挑时含它的组约 120 次
    worn = tally(seeds, used={"X04": 9}, done=set())
    assert sum(n for s, n in worn.items() if "X04" in s) < 40
    # 字段不够一组时不出
    assert planmod._pick_sets(pool[:2], 3, 1, random.Random(0), {}, set()) == []


def test_plan_groups_by_kind_and_tags_small_and_big(ctx, fake, seed_image) -> None:
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    got = [
        (t["slot_id"], t["kind"], t["task_type"], len(t["changed_fields"]))
        for t in plan["accepted"]
    ]
    assert got == [
        ("S1", "small", "small", 2),
        ("S2", "small", "small", 2),
        ("S3", "small", "small", 2),
        ("B1", "big", "big", 1),
        ("T1", "combo", "big", 3),
        ("T2", "combo", "big", 3),
        ("T3", "combo", "big", 3),
        ("T4", "combo", "big", 3),
    ]
    assert plan["counts"] == {"small": 3, "big": 1, "combo": 4}
    big, combos = plan["accepted"][3], plan["accepted"][4:]
    assert (big["title"], big["axis"], big["aspects"]) == (
        "绘制技法方案1",
        "绘制技法·绘制技法",
        ["绘制技法"],
    )
    assert ["元素角色", "配色结构", "绘制技法"] in [c["aspects"] for c in combos]
    small = plan["accepted"][0]
    fids = [b["field_id"] for b in small["brief"]]
    assert small["brief"] == [
        {
            "field_id": f,
            "aspect": fields.label(f),
            "from": "原样",
            "to": f"{fields.label(f)}S1-1",
        }
        for f in fids
    ]
    assert small["title"] == "小变化1-1"
    # 按类型分块调用（每块最多 plan.slots_per_call 个槽位）
    assert sorted(
        (g["kind"], [s["slot_id"] for s in g["slots"]]) for g in fake.p05_groups
    ) == [
        ("big", ["B1"]),
        ("combo", ["T1", "T2", "T3", "T4"]),
        ("small", ["S1", "S2", "S3"]),
    ]
    groups = {g["kind"]: g for g in fake.p05_groups}
    small_group, big_group, combo_group = (
        groups["small"],
        groups["big"],
        groups["combo"],
    )
    # 小变化：两个字段、带构图锁，字段都有 small_change；绘制技法不进小变化
    assert small_group["fields_per_slot"] == 2 and small_group["kind_label"] == "小变化"
    assert small_group["composition_lock"][0] == "整体构图与布局结构"
    assert (
        "改两处" in small_group["rule"] and "小变化不改绘制技法" in small_group["rule"]
    )
    small_fields = [f for s in small_group["slots"] for f in s["fields"]]
    assert all(f["small_change"] and "big_change" not in f for f in small_fields)
    assert not any(f["field_id"] == "D41" for f in small_fields)
    # 三处组合里的绘制技法按同一画法大类内调整写，另带技法变化清单
    tech = next(
        f for s in combo_group["slots"] for f in s["fields"] if f["field_id"] == "D41"
    )
    assert tech["change_forms"][0]["aspect"] == "画法大类"
    assert "三处组合里" in tech["small_change"] and "big_change" not in tech
    # 单字段大改：换画法大类的写法，不带构图锁
    assert "composition_lock" not in big_group and "画法大类" in big_group["rule"]
    (switch,) = big_group["slots"][0]["fields"]
    assert (
        switch["big_change"] and switch["change_forms"] and "small_change" not in switch
    )
    # 三处组合：三个字段都按小变化做法改，看得到做过的多字段方向
    assert combo_group["fields_per_slot"] == 3 and "composition_lock" not in combo_group
    assert combo_group["kind_label"] == "三处组合" and "这三处" in combo_group["rule"]
    assert all(f["small_change"] for f in combo_group["slots"][0]["fields"])
    stored = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM variants WHERE variant_id = ?",
            (small["variant_id"],),
        )
    )
    assert stored["slot_id"] == "S1" and stored["task_type"] == "small"
    batch = loads(ctx.db.scalar("SELECT plan_json FROM batches"))
    assert batch["slot_fields"]["T1"] == ["D09", "D17", "D37"]
    assert batch["slot_fields"]["T4"] == ["D17", "D37", "D41"]
    assert planmod.direction_tag("small", ["表情动作", "配色结构"]) == (
        "小变化 · 表情动作 + 配色结构"
    )
    assert (
        planmod.direction_tag("big", ["配色结构", "服饰道具"])
        == "大变化 · 配色结构 + 服饰道具"
    )
    assert planmod.direction_tag("control", ["主体"]) == "对照 · 只改主体"
    assert planmod.direction_tag("combo", ["主体", "配色"]) == "组合 · 改 2 处"
    # 覆盖统计按字段与维度计数：D09 在两组小变化 + 三组三处组合里，D41 是单字段大改 + 三组三处组合
    _, coverage, _ = planmod._history(ctx, family_id)
    assert (
        coverage["count_by_field"]["D09"] == 5
        and coverage["count_by_field"]["D41"] == 4
        and coverage["count_by_kind"]["combo"] == 4
    )
    # 给 P05 的方向记录写明类型：同一个技法字段上分得清整幅调整（组合）和换画法（单字段大改）
    records = planmod.direction_records(ctx, family_id)
    assert [x["kind"] for x in planmod.field_history(records)["D41"]] == [
        "单字段大改",
        "三处组合",
        "三处组合",
        "三处组合",
    ]
    assert len(planmod.multi_history(records)) == 7
    assert "D18" in coverage["unexplored_fields"]


def test_plan_batch_seeds_slots_with_the_batch_id(ctx, fake, seed_image) -> None:
    """批次记下随机挑中的字段组合，用「系列:批次」做种子能复现；完成卡片写明含几个三处组合。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    stored = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM batches WHERE batch_id = ?", (plan["batch_id"],)
        )
    )
    fam = planmod.planning_family(ctx, family_id)
    replay = planmod.plan_slots(
        ctx, fam, rng=random.Random(f"{family_id}:{plan['batch_id']}")
    )
    assert stored["slot_fields"] == {
        s["slot_id"]: s["field_ids"] for s in replay["slots"]
    }
    assert stored["slots"] == {"small": 3, "big": 1, "combo": 4, "total": 8}
    lines = notify._plan_lines(bg._plan_summary(plan))
    assert "（小变化 3 · 大变化 5（含三处组合 4）），等你挑选" in lines[0]


def test_slot_rules_trim_or_drop_out_of_slot_changes(ctx, fake, seed_image) -> None:
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)

    roles: dict[str, str] = {}

    def tasks(group):
        slots = {
            s["slot_id"]: [f["field_id"] for f in s["fields"]] for s in group["slots"]
        }
        if group["kind"] == "small":
            # 三组小变化：不含整体配色的那组试多写和重复，含整体配色的两组一组写旧编号、一组少改一个
            plain = next(sid for sid, f in slots.items() if "D37" not in f)
            alias, short = [sid for sid in slots if sid != plain]
            roles.update(plain=plain, alias=alias, short=short)
            a, b = slots[plain]
            other = next(f for f in slots[alias] if f != "D37")
            return [
                # 小变化多写了一个字段：只留槽位里的两个
                p05_task(plain, [(a, "小刺猬"), (b, "开怀大笑"), ("D05", "海洋主题")]),
                p05_task(plain, [(a, "小松鼠"), (b, "困倦打盹")]),
                p05_task("S9", [(a, "小鸭子"), (b, "惊喜张嘴")]),
                # 写成停用的面积关系 D38：按并入的整体配色 D37 算
                p05_task(alias, [(other, "整组换成小熊"), ("D38", "冰川蓝白")]),
                # 少改了槽位里的一个字段
                p05_task(short, [("D37", "薄荷珊瑚")]),
            ]
        if group["kind"] == "big":
            # 单字段大改多写的字段改记为联动
            return [p05_task("B1", [("D41", "剪纸拼贴"), ("D09", "纸雕小熊")])]
        # 三处组合 T1（主体 + 表情 + 整体配色）少了整体配色；其余组合这次没出
        return [p05_task("T1", [("D09", "小企鹅"), ("D17", "一起打盹")])]

    fake.p05_tasks = tasks
    plan = planmod.plan_batch(ctx, family_id)
    accepted = {t["slot_id"]: t for t in plan["accepted"]}
    assert sorted(accepted) == sorted(["B1", roles["plain"], roles["alias"]])
    assert [cf["to"] for cf in accepted[roles["plain"]]["changed_fields"]] == [
        "小刺猬",
        "开怀大笑",
    ]
    assert [cf["field_id"] for cf in accepted[roles["alias"]]["changed_fields"]][
        -1
    ] == "D37"
    assert accepted["B1"]["changed_fields"] == [
        {"field_id": "D41", "from": "原样", "to": "剪纸拼贴"}
    ]
    reasons = sorted(d["reason"] for d in plan["dropped"])
    assert reasons == [
        "duplicate_slot",
        "slot_mismatch",
        "slot_mismatch",
        "unknown_slot",
    ]
    assert {g["slot_id"] for g in plan["coverage_gaps"]} == {
        roles["short"],
        "T1",
        "T2",
        "T3",
        "T4",
    }

    def stored(sid: str) -> dict:
        return loads(
            ctx.db.scalar(
                "SELECT plan_json FROM variants WHERE variant_id = ?",
                (accepted[sid]["variant_id"],),
            )
        )

    big = stored("B1")
    assert big["coupled_changes"] == ["主体随之改成：纸雕小熊"] and big[
        "extra_fields_dropped"
    ] == ["D09"]
    small = stored(roles["plain"])
    assert small["extra_fields_dropped"] == ["D05"] and small["coupled_changes"] == []


def test_one_failed_group_keeps_the_rest(ctx, fake, seed_image) -> None:
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)

    def p05(payload):
        group = input_block(payload, "plan_group")
        if group["kind"] == "combo":
            return "不是 JSON"
        from conftest import default_p05_tasks

        result = {"family_id": "F99", "tasks": default_p05_tasks(fake, group)}
        return json.dumps(envelope("P05", result), ensure_ascii=False)

    fake.overrides["P05"] = p05
    plan = planmod.plan_batch(ctx, family_id)
    assert [t["kind"] for t in plan["accepted"]] == ["small", "small", "small", "big"]
    assert plan["failed_groups"][0]["group"] == "三处组合 T1–T4"
    assert plan["status"] == "proposed"

    # 全部失败：批次记失败、报错
    fake.overrides["P05"] = lambda payload: "不是 JSON"
    directions.reject(ctx, plan["batch_id"], by="tester")
    with pytest.raises(StageError, match="裂变策划失败"):
        planmod.plan_batch(ctx, family_id)
    assert (
        ctx.db.scalar("SELECT status FROM batches ORDER BY batch_id DESC LIMIT 1")
        == "failed"
    )


def test_legacy_family_has_to_be_reanalyzed_first(ctx, fake, seed_image) -> None:
    family_id = _approved_family(ctx, seed_image)
    spec = loads(ctx.db.scalar("SELECT spec_json FROM families"))
    spec.pop("field_schema")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE families SET spec_json = ?", (dumps(spec),))
    with pytest.raises(StageError, match="按新字段重新拆解"):
        planmod.plan_batch(ctx, family_id)
    assert planmod.plannable_families(ctx) == []
    assert planmod.legacy_families(ctx) == [{"family_id": family_id, "seed_id": "S01"}]
    available = directions.plan_availability(ctx, family_id)
    assert available["ok"] is False and available["needs_reanalyze"] == "S01"
    assert planmod.slot_counts(ctx, family_id) is None
    assert planmod.plan_button_label(None, again=True) == "按可变字段再策划一轮"


def test_small_change_job_only_does_i2i_and_qc_compares_with_reference(
    ctx, fake, seed_image
) -> None:
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    small, big = plan["accepted"][0], plan["accepted"][3]
    assert big["title"] == "绘制技法方案1" and big["task_type"] == "big"
    result = directions.confirm(
        ctx,
        plan["batch_id"],
        by="tester",
        pick=[small["variant_id"], big["variant_id"]],
    )
    assert result["images_per_direction"] == {
        small["variant_id"]: 6,
        big["variant_id"]: 9,
    }
    small_job, big_job = result["jobs"]
    slots = [
        r["slot_id"]
        for r in ctx.db.q(
            "SELECT slot_id FROM job_slots WHERE job_id = ?", (small_job,)
        )
    ]
    assert sorted(slots) == sorted(
        [f"{m}#1@i2i" for m in FANOUT_MODELS] + [f"{m}#1@i2i" for m in EDIT_MODELS]
    )
    Worker(ctx, max_minutes=5).run()
    render = loads(
        ctx.db.scalar(
            "SELECT render_spec_json FROM jobs WHERE job_id = ?", (small_job,)
        )
    )
    assert render["modes"] == ["i2i"] and render["reference_seed_id"] == "S01"
    assert render["final_prompt"] == render["i2i_prompts"]["gemini"]
    # P06 拿到小变化的类型与构图锁、参考图特征
    p06 = [_text(m[1]["content"]) for m in _chat_messages(fake, "P06")]
    small_p06 = next(t for t in p06 if '"task_type": "small"' in t)
    assert "composition_lock" in small_p06 and '"has_text": false' in small_p06
    # 小变化的质检附上参考图（候选 + 参考图两张），大变化只有候选
    images_per_qc = sorted(
        sum(1 for part in m[1]["content"] if part.get("type") == "image_url")
        for m in _chat_messages(fake, "P07")
    )
    assert images_per_qc == [1] * 9 + [2] * 6
    archived = ctx.db.q(
        "SELECT a.slot_id FROM assets a WHERE a.job_id = ? AND a.status = 'archived'",
        (small_job,),
    )
    assert len(archived) == 6 and all(a["slot_id"].endswith("@i2i") for a in archived)


def test_combo_changes_three_small_fields_including_technique(
    ctx, fake, seed_image
) -> None:
    """三处组合算大变化：绘制技法能进三处组合，旧编号 D42 按 D41 算，多写的字段记为联动。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)

    def tasks(group):
        from conftest import default_p05_tasks

        if group["kind"] == "combo":
            slot = next(
                s["slot_id"]
                for s in group["slots"]
                if {f["field_id"] for f in s["fields"]} == {"D09", "D37", "D41"}
            )
            return [
                p05_task(
                    slot,
                    [
                        ("D37", "薄荷珊瑚"),
                        ("D42", "两阶赛璐璐"),
                        ("D09", "小企鹅"),
                        ("D05", "海洋主题"),
                    ],
                )
            ]
        return default_p05_tasks(fake, group)

    fake.p05_tasks = tasks
    plan = planmod.plan_batch(ctx, family_id)
    combo = next(t for t in plan["accepted"] if t["kind"] == "combo")
    assert (combo["kind"], combo["task_type"]) == ("combo", "big")
    assert combo["aspects"] == ["元素角色", "配色结构", "绘制技法"]
    assert [cf["field_id"] for cf in combo["changed_fields"]] == ["D09", "D37", "D41"]
    stored = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM variants WHERE variant_id = ?",
            (combo["variant_id"],),
        )
    )
    assert stored["coupled_changes"] == ["主题随之改成：海洋主题"]


def test_technique_goes_into_big_and_combo_but_not_small(ctx, fake, seed_image) -> None:
    """绘制技法不进小变化（之前在小变化里几乎没被选中）：单字段大改换画法大类，三处组合里同一画法大类内调整，
    组合按大变化出图（文生图 + 图生图）。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    by_kind: dict[str, list[set[str]]] = {}
    for t in plan["accepted"]:
        by_kind.setdefault(t["kind"], []).append(
            {cf["field_id"] for cf in t["changed_fields"]}
        )
    assert by_kind["small"] and not any("D41" in f for f in by_kind["small"])
    assert by_kind["big"] == [{"D41"}]
    assert sum("D41" in f for f in by_kind["combo"]) == 3
    combo = next(
        t
        for t in plan["accepted"]
        if t["kind"] == "combo"
        and "D41" in {cf["field_id"] for cf in t["changed_fields"]}
    )
    assert combo["task_type"] == "big"
    result = directions.confirm(
        ctx, plan["batch_id"], by="tester", pick=[combo["variant_id"]]
    )
    assert result["images_per_direction"] == {combo["variant_id"]: 9}


def test_small_change_modes_config(home, fake) -> None:
    ctx = reopen(home)
    assert small_change_modes(ctx) == ["i2i"]
    assert variant_modes(ctx, {"task_type": "small"}) == ["i2i"]
    assert variant_modes(ctx, {"task_type": "big"}) == ["t2i", "i2i"]
    assert variant_modes(ctx, {"task_type": "combo"}) == ["t2i", "i2i"]
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    assert (
        checks["小变化的出图方式（image_generation.small_change_modes）"]["ok"] is True
    )
    plan_check = next(c for n, c in checks.items() if n.startswith("裂变策划参数"))
    assert plan_check["ok"] is True and "小变化 12 个、大变化 8 个" in str(
        plan_check["detail"]
    )
    # 小变化也做文生图：能跑，但 doctor 提醒保不住构图
    set_config(home, "image_generation.small_change_modes", ["t2i", "i2i"])
    checks = {c["check"]: c for c in doctor.run(reopen(home))["checks"]}
    warn = checks["小变化只做图生图（文生图从零画，保不住原构图）"]
    assert warn["ok"] is False and warn["level"] == "warn"
    set_config(home, "image_generation.small_change_modes", ["edit"])
    ctx = reopen(home)
    with pytest.raises(ConfigError):
        small_change_modes(ctx)
    for key, bad in (("plan.concurrency", 0), ("plan.slots_per_call", 0)):
        set_config(home, key, bad)
        checks = {c["check"]: c for c in doctor.run(reopen(home))["checks"]}
        plan_check = next(c for n, c in checks.items() if n.startswith("裂变策划参数"))
        assert plan_check["ok"] is False, key
        set_config(home, key, 4)


def test_each_model_family_and_mode_gets_its_own_prompt(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    job = ctx.db.q1("SELECT render_spec_json FROM jobs WHERE job_id = 'J0001'")
    render = loads(job["render_spec_json"])
    assert render["modes"] == ["t2i", "i2i"] and render["reference_seed_id"] == "S01"
    assert set(render["prompts"]) == set(render["i2i_prompts"]) == {"gemini", "gpt"}
    assert render["final_prompt"] == render["prompts"]["gemini"]
    payloads = fake.image_payloads()
    t2i = {p["model"]: p for p in payloads if "images" not in p["vendor_params"]}
    i2i = {p["model"]: p for p in payloads if "images" in p["vendor_params"]}
    # 图生图用原模型名和 -edit 名各出一张，-edit 只做图生图
    assert set(t2i) == set(FANOUT_MODELS)
    assert set(i2i) == set(FANOUT_MODELS) | set(EDIT_MODELS)
    for model, p in t2i.items():
        family = "gemini" if model.startswith("gemini-") else "gpt"
        assert p["prompt"].startswith(f"[{family}]"), model
    for model, p in i2i.items():
        family = "gemini" if model.startswith("gemini-") else "gpt"
        assert p["prompt"].startswith(f"[{family} i2i]"), model
        # 输入图是参考图原图（压过尺寸的 JPEG），不是文生图的产出
        assert len(p["vendor_params"]["images"]) == 1
        assert "format" not in p["vendor_params"]
    sent_ref = base64.b64decode(next(iter(i2i.values()))["vendor_params"]["images"][0])
    assert images.probe(sent_ref)[0] == "JPEG"


def test_generation_modes_config(home, fake) -> None:
    set_config(home, "image_generation.modes", ["i2i", "t2i"])
    ctx = reopen(home)
    assert generation_modes(ctx) == ["t2i", "i2i"]
    # 原模型的图生图在前，-edit 的排在后面
    assert [s for s, _, _ in fanout_slots(ctx, ["i2i"])] == [
        f"{m}#1@i2i" for m in FANOUT_MODELS + EDIT_MODELS
    ]
    set_config(home, "image_generation.modes", ["img2img"])
    ctx = reopen(home)
    with pytest.raises(ConfigError):
        generation_modes(ctx)
    checks = {c["check"]: c for c in doctor.run(ctx)["checks"]}
    mode_check = next(v for k, v in checks.items() if k.startswith("出图方式"))
    assert mode_check["ok"] is False and "img2img" in str(mode_check["detail"])


def test_capabilities_list_prompt_keys_and_modes(ctx) -> None:
    caps = jobmod.generator_capabilities(ctx, FANOUT_MODELS, ["t2i", "i2i"])
    assert caps["prompt_keys"] == ["gemini", "gpt"]
    assert [m["result_key"] for m in caps["generation_modes"]] == [
        "prompts",
        "i2i_prompts",
    ]
    assert caps["same_prompt_for_all_models"] is False


def test_legacy_render_spec_falls_back_to_final_prompt() -> None:
    legacy = {"final_prompt": "旧版单一提示词：三只幼态动物"}
    assert (
        jobmod.prompt_for_slot(legacy, "gpt-image-2.5-flare", "t2i")
        == legacy["final_prompt"]
    )
    partial = {"final_prompt": "gemini 版", "prompts": {"gemini": "gemini 版"}}
    assert (
        jobmod.prompt_for_slot(partial, "gpt-image-2.5-sunburst", "t2i") == "gemini 版"
    )
    # 图生图不回落到文生图的提示词：缺了就报错，要重新编译
    with pytest.raises(StageError):
        jobmod.prompt_for_slot(partial, "gemini-nano-banana-2.1-edit", "i2i")


def test_p06_validator_rules() -> None:
    t2i_only = jobmod._validate_p06(["gemini", "gpt"], ["t2i"])
    good = (
        "Flat original print artwork of three baby animals on a warm off-white ground."
    )
    assert t2i_only({"prompts": {"gemini": good, "gpt": good}}) == []
    errors = t2i_only({"prompts": {"gemini": good}})
    assert errors == ["prompts 缺少模型家族 gpt 的文生图提示词"]
    errors = t2i_only(
        {
            "prompts": {
                "gemini": "Keep the layout of the reference image, unlike the original.",
                "gpt": "不要画参考图里的小象。" + good,
            }
        }
    )
    assert len(errors) == 2 and "reference image" in errors[0] and "参考图" in errors[1]
    # 图生图的提示词可以提到参考图；文生图的仍然不行
    both = jobmod._validate_p06(["gemini"], ["t2i", "i2i"])
    i2i = {"gemini": "Use the reference image only for the palette."}
    assert both({"prompts": {"gemini": good}, "i2i_prompts": i2i}) == []
    assert both({"prompts": {"gemini": good}}) == [
        "i2i_prompts 缺少模型家族 gemini 的图生图提示词"
    ]
    assert both({"prompts": i2i, "i2i_prompts": i2i}) != []


def test_p06_retries_until_prompts_are_valid(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    good = (
        "Flat original print artwork of three baby animals on a warm off-white ground."
    )
    edit = "Use the reference image only for the palette; redraw everything else."
    answers = [
        {"gemini": good},
        {"gemini": "Redraw the reference image with new animals.", "gpt": good},
        {"gemini": good, "gpt": good},
    ]

    def p06(payload):
        prompts_ = answers[min(fake.stage_counts["P06"] - 1, len(answers) - 1)]
        return json.dumps(
            envelope(
                "P06",
                {
                    "variant_id": "V",
                    "prompts": prompts_,
                    "i2i_prompts": {"gemini": edit, "gpt": edit},
                    "prompt_summary": "三只幼态动物",
                    "avoid_instructions": [],
                    "generation_parameters": {},
                    "text_overlay": {"enabled": False},
                    "checks": [],
                    "output_level": "preview",
                },
            ),
            ensure_ascii=False,
        )

    fake.overrides["P06"] = p06
    job = dict(ctx.db.q1("SELECT * FROM jobs WHERE job_id = 'J0001'"))
    jobmod.step_s06(ctx, job)
    assert fake.stage_counts["P06"] == 3
    calls = _chat_messages(fake, "P06")
    # 每次重答都把上一轮的校验错误发回给模型
    assert "缺少模型家族 gpt" in _text(calls[1][-1]["content"])
    assert "reference image" in _text(calls[2][-1]["content"])
    row = ctx.db.q1("SELECT render_spec_json FROM jobs WHERE job_id = 'J0001'")
    assert loads(row["render_spec_json"])["prompts"] == {"gemini": good, "gpt": good}


def test_small_slots_say_how_to_make_the_change_visible(ctx, fake, seed_image) -> None:
    """小变化与三处组合的字段都带 small_change（同类元素一起改），单字段大改带 big_change；P04 也拿到。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    planmod.plan_batch(ctx, family_id)
    small = next(g for g in fake.p05_groups if g["kind"] == "small")
    palette = next(
        f for s in small["slots"] for f in s["fields"] if f["field_id"] == "D37"
    )
    assert palette["small_change"] == fields.small_change("D37")
    assert "整套配色" in palette["small_change"]
    big = next(g for g in fake.p05_groups if g["kind"] == "big")
    assert all(
        "small_change" not in f and f["big_change"]
        for s in big["slots"]
        for f in s["fields"]
    )
    combo = next(g for g in fake.p05_groups if g["kind"] == "combo")
    assert all(f["small_change"] for s in combo["slots"] for f in s["fields"])
    p04 = _text(_chat_messages(fake, "P04")[-1][1]["content"])
    assert '"small_change"' in p04 and '"kinds"' in p04


def test_plan_kinds_only_plans_those_slots(ctx, fake, seed_image) -> None:
    """--kinds small,combo：只策划小变化与三处组合，这一轮不出单字段大改。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, kinds=["small", "combo"])
    assert [t["kind"] for t in plan["accepted"]] == ["small"] * 3 + ["combo"] * 4
    assert {g["kind"] for g in fake.p05_groups} == {"small", "combo"}
    stored = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM batches WHERE batch_id = ?", (plan["batch_id"],)
        )
    )
    assert stored["kinds"] == ["combo", "small"]
    with pytest.raises(StageError, match="kinds"):
        planmod.plan_batch(ctx, family_id, kinds=["tiny"])


def test_cancel_stops_queued_directions_only(ctx, fake, seed_image) -> None:
    """停掉没开始的方向：任务和出图槽位删掉、方向记已停掉、批次方向数跟着减；开始了的跳过。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    with pytest.raises(StageError, match="还没确认"):
        directions.cancel(ctx, plan["batch_id"], by="tester")
    result = confirm_all(ctx, plan)
    small, combos = plan["accepted"][0], plan["accepted"][4:]
    combo, combo_jobs = combos[-1], result["jobs"][4:]
    assert [c["kind"] for c in combos] == ["combo"] * 4

    out = directions.cancel(ctx, plan["batch_id"], kinds=["combo"], by="tester")
    assert out["cancelled"] == [c["variant_id"] for c in combos]
    assert out["jobs"] == combo_jobs
    assert out["skipped"] == [] and out["remaining_jobs"] == 4
    for table in ("jobs", "job_slots"):
        assert (
            ctx.db.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (combo_jobs[-1],)
            )
            == 0
        )
    batch = ctx.db.q(
        "SELECT status, size, plan_json FROM batches WHERE batch_id = ?",
        (plan["batch_id"],),
    )[0]
    assert (batch["status"], batch["size"]) == ("planned", 4)
    assert combo["variant_id"] not in loads(batch["plan_json"])["picked"]
    records = {r["variant_id"]: r for r in planmod.direction_records(ctx, family_id)}
    assert records[combo["variant_id"]]["state_text"] == "已停掉"
    view = ui.directions_view(ctx, plan["batch_id"])
    states = {i["variant_id"]: i["state"] for i in view["items"]}
    assert states[combo["variant_id"]] == "已停掉"
    assert states[small["variant_id"]].startswith("已选")

    # 已经开始出图的不停
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running' WHERE variant_id = ?",
            (small["variant_id"],),
        )
    out = directions.cancel(ctx, plan["batch_id"], ["1"], by="tester")
    assert out["cancelled"] == [] and "已经开始出图" in out["skipped"][0]["reason"]

    # 一个任务都不剩：批次记「全部停掉了」，不再等反馈报告
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'ready' WHERE variant_id = ?",
            (small["variant_id"],),
        )
    out = directions.cancel(ctx, plan["batch_id"], ["all"], by="tester")
    assert len(out["cancelled"]) == 4 and out["remaining_jobs"] == 0
    assert (
        ctx.db.scalar(
            "SELECT status FROM batches WHERE batch_id = ?", (plan["batch_id"],)
        )
        == "cancelled"
    )
    assert plan["batch_id"] not in finished_batches(ctx)
    with pytest.raises(StageError, match="全部停掉了"):
        directions.cancel(ctx, plan["batch_id"], by="tester")


def test_p05_current_is_the_full_analysis_value(ctx, fake, seed_image) -> None:
    """P05 的「原来的样子」用拆解的完整取值：几个主体的描述不再被截到几十个字。"""
    use_small(fake)
    long_value = "E01 长颈鹿浅奶油底配棕斑，" * 6 + "E03 小熊蜂蜜棕"
    fake.p02_values["D09"] = long_value
    family_id = _approved_family(ctx, seed_image)
    planmod.plan_batch(ctx, family_id)
    subjects = [
        f
        for g in fake.p05_groups
        for s in g["slots"]
        for f in s["fields"]
        if f["field_id"] == "D09"
    ]
    assert len(long_value) > 60 and subjects
    assert all(f["current"].endswith("E03 小熊蜂蜜棕") for f in subjects)
    # 可变清单里仍是短值（挑选卡等处用）
    variation = seedmod.analysis_variation(seedmod.latest_analysis(ctx, "S01"))
    d09 = next(v for v in variation["variable"] if v["field_id"] == "D09")
    assert len(d09["value"]) <= 60


def test_old_analysis_drops_fields_that_became_record_only(
    ctx, fake, seed_image
) -> None:
    """拆解之后 D36 改成只记录：读旧拆解时不再当可变字段，修订出的系列也没有它的变化域。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    analysis = seedmod.latest_analysis(ctx, "S01")
    result = loads(analysis["result_json"])
    result["variation"]["variable"].append(
        {
            "field_id": "D36",
            "label": "底色",
            "dimension": "10 配色结构",
            "tier": "small",
            "value": "暖白",
            "reason": "底色可以换",
        }
    )
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE analyses SET result_json = ? WHERE analysis_id = ?",
            (dumps(result), analysis["analysis_id"]),
        )
    variation = seedmod.analysis_variation(seedmod.latest_analysis(ctx, "S01"))
    assert "D36" not in [v["field_id"] for v in variation["variable"]]
    moved = next(v for v in variation["not_variable"] if v["field_id"] == "D36")
    assert moved["reason"] == seedmod.RECORD_ONLY_REASON
    revised = seedmod.run_s04(
        ctx, "S01", revise_family_id=family_id, revision_note="放开主体"
    )
    spec = revised["spec"]
    assert "D36" not in [d["field_id"] for d in spec["variable_domains"]]
    p04 = {"messages": _chat_messages(fake, "P04")[-1]}
    variable = input_block(p04, "variable_fields")["variable"]
    assert "D36" not in [v["field_id"] for v in variable]
    # 挑选卡不把只记录的字段算成「判定不能变」
    assert not any("底色" in line for line in ui._variation_lines(spec))


def test_old_technique_fields_fold_into_d41(ctx, fake, seed_image) -> None:
    """D42–D44 并进 D41 之前的拆解与系列：判定可变的上色方式算绘制技法可变，原值拼进现状，选项并进 D41。"""
    use_small(fake)
    family_id = _approved_family(ctx, seed_image)
    analysis = seedmod.latest_analysis(ctx, "S01")
    result = loads(analysis["result_json"])
    values = {"D41": "细黑描边", "D42": "水彩晕染", "D43": "柔光", "D44": "纸纹"}
    result["fields"] = [f for f in result["fields"] if f["field_id"] != "D41"] + [
        {"field_id": fid, "value": value} for fid, value in values.items()
    ]
    variation = result["variation"]
    variation["variable"] = [
        v for v in variation["variable"] if v["field_id"] != "D41"
    ] + [
        {
            "field_id": "D42",
            "label": "上色方式",
            "dimension": "11 绘制技法",
            "tier": "big",
            "value": "水彩晕染",
            "reason": "可以换画法",
        }
    ]
    variation["not_variable"].append(
        {"field_id": "D43", "label": "光影体积", "reason": "光影很弱"}
    )
    spec = loads(ctx.db.scalar("SELECT spec_json FROM families"))
    spec["variable_domains"] = [
        d for d in spec["variable_domains"] if d["field_id"] != "D41"
    ] + [
        {"field_id": "D42", "options": ["丝网版画"], "compatibility": []},
        {"field_id": "D44", "options": ["做旧颗粒", "丝网版画"], "compatibility": []},
    ]
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE analyses SET result_json = ? WHERE analysis_id = ?",
            (dumps(result), analysis["analysis_id"]),
        )
        conn.execute("UPDATE families SET spec_json = ?", (dumps(spec),))
    variation = seedmod.analysis_variation(seedmod.latest_analysis(ctx, "S01"))
    tech = [v for v in variation["variable"] if v["dimension"] == "11 绘制技法"]
    assert [(v["field_id"], v["label"]) for v in tech] == [("D41", "绘制技法")]
    assert not {"D42", "D43"} & {v["field_id"] for v in variation["not_variable"]}
    fam = planmod.planning_family(ctx, family_id)
    pools = planmod.field_pools(ctx, fam)
    (switch,) = pools["big"]
    assert switch["field_id"] == "D41"
    assert switch["options"] == ["丝网版画", "做旧颗粒"]
    assert switch["current"] == "细黑描边；水彩晕染；柔光；纸纹"
    assert [f["field_id"] for f in pools["small"]] == ["D09", "D17", "D37"]
    assert [f["field_id"] for f in pools["combo"]] == ["D09", "D17", "D37", "D41"]
