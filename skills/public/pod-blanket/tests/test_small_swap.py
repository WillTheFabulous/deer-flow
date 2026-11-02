"""小变化以换主体为主（2026-10-27）：plan.small_swap_ratio 把小变化分成换主体组（每组至少一处 D09）与其余组
（不换主体），一组凑不够由另一组补；要换主体时 P14 先给每个要换的单元一份互不相同的新主体清单，按槽位分下去
（swap_to），同一批里换成同一个主体的丢掉；单字段大改同一个大字段一轮最多 plan.big_single_repeat 次。"""

from __future__ import annotations

import random

from conftest import confirm_all, p05_task, reopen, set_config
from podflow import doctor, subjects
from podflow.stages import plan as planmod
from podflow.util import loads
from test_subject_pairs import _family, _home, _info, _unit

UNITS = [
    _unit("U01", "高地牛", "character", ["E01"], "large"),
    _unit("U02", "星星", "decoration", ["E02", "E03"], "medium"),
]
POOL_FIELDS = ("D09", "D11", "D17", "D20", "D37")


def _swap(item: dict) -> bool:
    return item["field_id"] == "D09"


def _pool() -> list[dict]:
    return subjects.pair_pool(UNITS, [_info(f) for f in POOL_FIELDS])


def _swap_tasks(group: dict) -> list[dict]:
    """P05 照清单换：换主体那处写「换成<swap_to>」，其余处写短名 + 槽位号。"""
    out = []
    for slot in group["slots"]:
        changes = []
        for f in slot["fields"]:
            if f["field_id"] == "D09" and f.get("swap_to"):
                to = f"换成{f['swap_to']['name']}"
            else:
                to = f"{f['label']}{slot['slot_id']}"
            changes.append(
                (f["field_id"], to, f["unit_id"])
                if f.get("unit_id")
                else (f["field_id"], to)
            )
        out.append(p05_task(slot["slot_id"], changes, title=f"方向{slot['slot_id']}"))
    return out


def _swap_ctx(home, fake, *, small: int = 5, ratio: float = 0.8):
    _home(home, fake)
    set_config(home, "plan.small_swap_ratio", ratio)
    set_config(home, "plan.small_count", small)
    # 只看小变化：大变化不出
    set_config(home, "plan.big_count", 0)
    return reopen(home)


def test_pick_pair_sets_must_and_forbid(monkeypatch) -> None:
    """must：每组至少一处换主体；forbid：一处都没有；计数与挑出来的一致，组合太多走抽样时也守规则。"""
    pool = _pool()
    assert [subjects.pair_label(p) for p in pool] == [
        "高地牛·主体",
        "高地牛·表情",
        "高地牛·服饰配件",
        "星星·背景装饰",
        "整体配色",
    ]
    swap = subjects.pick_pair_sets(pool, 2, 10, random.Random(1), must=_swap)
    assert all(any(_swap(p) for p in s) for s in swap)
    assert len(swap) == subjects.count_valid(pool, 2, 99, must=_swap) == 4
    other = subjects.pick_pair_sets(pool, 2, 10, random.Random(1), forbid=_swap)
    assert not any(_swap(p) for s in other for p in s)
    assert len(other) == subjects.count_valid(pool, 2, 99, forbid=_swap) == 6
    # 没有能换主体的处时 must 一组都挑不出
    no_swap = [p for p in pool if not _swap(p)]
    assert subjects.pick_pair_sets(no_swap, 2, 3, random.Random(1), must=_swap) == []
    assert subjects.count_valid(no_swap, 2, 9, must=_swap) == 0
    monkeypatch.setattr(subjects, "ENUM_LIMIT", 1)
    sampled = subjects.pick_pair_sets(pool, 2, 3, random.Random(2), must=_swap)
    assert len(sampled) == 3 and all(any(_swap(p) for p in s) for s in sampled)
    sampled = subjects.pick_pair_sets(pool, 2, 3, random.Random(2), forbid=_swap)
    assert len(sampled) == 3 and not any(_swap(p) for s in sampled for p in s)


def test_small_split_fills_from_the_other_group(ctx, home) -> None:
    """八成换主体：换主体组不够时由其余组补满；比例为 0 不分组；没有换主体的处时全走其余组并写原因。"""
    pool = _pool()
    set_config(home, "plan.small_swap_ratio", 0.8)
    set_config(home, "plan.small_count", 10)
    split = planmod.small_split(reopen(home), pool, by_unit=True)
    # 换主体的组合只有 4 种，其余 6 种：4 + 6 = 10
    assert (split["mode"], split["swap"], split["other"]) == ("swap", 4, 6)
    set_config(home, "plan.small_count", 5)
    split = planmod.small_split(reopen(home), pool, by_unit=True)
    assert (split["swap"], split["other"], split["reason"]) == (4, 1, None)
    no_swap = [p for p in pool if not _swap(p)]
    split = planmod.small_split(reopen(home), no_swap, by_unit=True)
    assert (split["swap"], split["other"]) == (0, 5)
    assert split["reason"] == planmod.NO_SWAP_REASON
    # 反过来：两只各自换主体 + 整体配色，不换主体的组合一个都没有，名额全由换主体组补
    twins = subjects.pair_pool(
        [
            _unit("U01", "小熊", "character", ["E01"], "large"),
            _unit("U03", "小兔", "character", ["E04"], "large"),
        ],
        [_info("D09"), _info("D37")],
    )
    set_config(home, "plan.small_swap_ratio", 0.5)
    set_config(home, "plan.small_count", 3)
    split = planmod.small_split(reopen(home), twins, by_unit=True)
    assert (split["swap"], split["other"]) == (3, 0)
    set_config(home, "plan.small_count", 5)
    set_config(home, "plan.small_swap_ratio", 0)
    split = planmod.small_split(reopen(home), pool, by_unit=True)
    assert split["mode"] is None and split["other"] == 5
    set_config(home, "plan.small_swap_ratio", 1.5)
    try:
        planmod.small_split(reopen(home), pool, by_unit=True)
    except planmod.ConfigError as exc:
        assert "plan.small_swap_ratio" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("比例超过 1 应该报错")


def test_plan_batch_swaps_from_the_roster(home, fake, seed_image) -> None:
    """一次 P14 给高地牛一份清单；四个换主体的小变化各分到一个不同的新主体，第五个不换主体；批次记下分组与清单。"""
    ctx = _swap_ctx(home, fake)
    family_id = _family(ctx, seed_image)
    fake.p05_tasks = _swap_tasks
    plan = planmod.plan_batch(ctx, family_id)
    assert len(fake.p14_inputs) == 1
    (unit,) = fake.p14_inputs[0]
    assert unit["name"] == "高地牛" and unit["kind"] == "character"
    assert unit["need"] == 4 + planmod.SWAP_SPARE
    assert plan["counts"] == {"small": 5}
    assert (plan["swap"]["swap"], plan["swap"]["other"]) == (4, 1)
    assert plan["swap"]["assigned"] == 4 and not plan["swap"]["error"]
    swaps = [
        cf["to"]
        for t in plan["accepted"]
        for cf in t["changed_fields"]
        if cf["field_id"] == "D09"
    ]
    assert sorted(swaps) == [f"换成高地牛新主体{i}" for i in range(1, 5)]
    assert not plan["swap"]["off_roster"]
    # P05 拿到的槽位：换主体那处带 swap_to，同一批里各不相同
    given = [
        f["swap_to"]["name"]
        for g in fake.p05_groups
        for s in g["slots"]
        for f in s["fields"]
        if f.get("swap_to")
    ]
    assert len(given) == len(set(given)) == 4
    batch = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM batches WHERE batch_id = ?", (plan["batch_id"],)
        )
    )
    assert batch["small_swap"] is True
    assert batch["swap"]["roster"] == {"E01": [f"高地牛新主体{i}" for i in range(1, 8)]}


def test_roster_skips_done_swaps(home, fake, seed_image) -> None:
    """清单里和做过的换法重复的（规范化后相同、或被做过的整个包含）不分出去。"""
    ctx = _swap_ctx(home, fake, small=2, ratio=1.0)
    family_id = _family(ctx, seed_image)
    fake.p05_tasks = _swap_tasks
    fake.p14_result = lambda units: {
        "units": [
            {
                "unit_id": u["unit_id"],
                "candidates": [{"name": n} for n in ("北极熊", "企鹅", "海豹", "狐狸")],
            }
            for u in units
        ]
    }
    first = planmod.plan_batch(ctx, family_id)
    assert sorted(first["swap"]["roster"]["E01"]) == ["企鹅", "北极熊", "海豹", "狐狸"]
    # 第一批的两个方向换成了北极熊、企鹅（确认出图）：第二批的清单只剩海豹、狐狸
    confirm_all(ctx, first)
    second = planmod.plan_batch(ctx, family_id)
    assert second["swap"]["roster"]["E01"] == ["海豹", "狐狸"]
    swaps = {
        cf["to"]
        for t in second["accepted"]
        for cf in t["changed_fields"]
        if cf["field_id"] == "D09"
    }
    assert swaps == {"换成海豹", "换成狐狸"}


def test_same_swap_in_one_batch_is_dropped(home, fake, seed_image) -> None:
    """P05 没照清单、几个方向都换成同一个主体：同一批里只留第一个，其余记 duplicate_swap。"""
    ctx = _swap_ctx(home, fake, small=4, ratio=1.0)
    family_id = _family(ctx, seed_image)

    def same(group):
        return [
            p05_task(
                s["slot_id"],
                [
                    (
                        f["field_id"],
                        "换成同一种熊猫"
                        if f["field_id"] == "D09"
                        else f"{f['label']}{s['slot_id']}",
                        f["unit_id"],
                    )
                    for f in s["fields"]
                ],
                title=f"方向{s['slot_id']}",
            )
            for s in group["slots"]
        ]

    fake.p05_tasks = same
    plan = planmod.plan_batch(ctx, family_id)
    kept = [
        t
        for t in plan["accepted"]
        if any(cf["field_id"] == "D09" for cf in t["changed_fields"])
    ]
    assert len(kept) == 1
    reasons = [d["reason"] for d in plan["dropped"]]
    assert reasons.count("duplicate_swap") == 3
    assert {o["slot_id"] for o in plan["swap"]["off_roster"]} == {kept[0]["slot_id"]}


def test_locked_subject_falls_back(home, fake, seed_image) -> None:
    """系列锁定了换主体：小变化照旧随机、不调 P14，批次写原因。"""
    fake.p04_locked = [
        {"field_id": "D09", "source": "user_positioning", "reason": "主体是品牌形象"}
    ]
    ctx = _swap_ctx(home, fake, small=3)
    family_id = _family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    assert not fake.p14_inputs
    assert (
        plan["swap"]["swap"] == 0 and plan["swap"]["reason"] == planmod.NO_SWAP_REASON
    )
    assert plan["counts"]["small"] == 3
    assert not any(
        cf["field_id"] == "D09" for t in plan["accepted"] for cf in t["changed_fields"]
    )


def test_ratio_off_keeps_the_old_plan(home, fake, seed_image) -> None:
    """比例为 0：不调 P14、批次不带换主体标记，槽位和原来一样随机挑。"""
    ctx = _swap_ctx(home, fake, small=3, ratio=0)
    family_id = _family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    assert not fake.p14_inputs and plan["swap"] is None
    batch = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM batches WHERE batch_id = ?", (plan["batch_id"],)
        )
    )
    assert "small_swap" not in batch


def test_field_mode_swap_group(home, fake, seed_image) -> None:
    """按字段策划（plan.unit: fields）时同样分组：换主体组的小变化都带主体字段。"""
    ctx = _swap_ctx(home, fake, small=5)
    set_config(home, "plan.unit", "fields")
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    preview = planmod.preview_slots(ctx, family_id)
    assert preview["plan_unit"] == "fields"
    smalls = [s for s in preview["slots"] if s["kind"] == "小变化"]
    assert len(smalls) == 5
    assert sum(1 for s in smalls if s.get("swap")) == preview["swap"]["swap"] == 4
    assert all("主体" in s["changes"] for s in smalls if s.get("swap"))
    assert preview["swap_units"] == {"全部主体": 4}


def test_big_single_repeat(home, fake, seed_image) -> None:
    """单字段大改同一个大字段一轮最多 big_single_repeat 次：三个大字段、每个出两次；重复 1 次时只有三个。"""
    set_config(home, "plan.big_count", 6)
    set_config(home, "plan.big_single_count", 6)
    set_config(home, "plan.big_single_repeat", 2)
    ctx = reopen(home)
    family_id = _family(ctx, seed_image)
    preview = planmod.preview_slots(ctx, family_id)
    bigs = [s for s in preview["slots"] if s["kind"] == "单字段大改"]
    assert len(bigs) == 6
    assert sorted(preview["repeated_big_fields"]) == ["主题", "构图类型", "视觉中心"]
    # 同一个字段的两个槽位挨在一起（P05 分块时尽量在同一块）
    assert [s["changes"] for s in bigs] == [
        ["主题"],
        ["主题"],
        ["构图类型"],
        ["构图类型"],
        ["视觉中心"],
        ["视觉中心"],
    ]

    def distinct(group):
        return [
            p05_task(
                s["slot_id"],
                [(f["field_id"], f"{f['label']}{s['slot_id']}") for f in s["fields"]],
            )
            for s in group["slots"]
        ]

    fake.p05_tasks = distinct
    plan = planmod.plan_batch(ctx, family_id)
    assert plan["counts"] == {"big": 6}
    set_config(home, "plan.big_single_repeat", 1)
    preview = planmod.preview_slots(reopen(home), family_id)
    assert sum(1 for s in preview["slots"] if s["kind"] == "单字段大改") == 3
    assert preview["repeated_big_fields"] == []


def test_counts_text_and_doctor(home, fake, seed_image) -> None:
    """按钮文案写出换主体几个；doctor 报设置写错、二次大变化开着但没分组。"""
    assert planmod.counts_text(
        {"small": 30, "small_swap": 24, "big": 12, "combo": 12}
    ) == ("小变化 30（换主体 24） · 大变化 24（单字段大改 12 + 三处组合 12）")
    assert planmod.counts_text({"derived": 16}) == "二次大变化 16"
    ctx = _swap_ctx(home, fake)
    checks = {c["check"]: c for c in doctor._swap_checks(ctx)}
    (main,) = [c for c in checks.values() if c["check"].startswith("小变化换主体")]
    assert main["ok"] and "换主体 80%" in main["detail"]
    set_config(home, "plan.small_swap_ratio", 0)
    set_config(home, "plan.derived.enabled", True)
    checks = doctor._swap_checks(reopen(home))
    assert any(not c["ok"] and c["level"] == "warn" for c in checks)
    set_config(home, "plan.small_swap_ratio", "八成")
    (bad,) = doctor._swap_checks(reopen(home))
    assert not bad["ok"] and bad["level"] == "error"
