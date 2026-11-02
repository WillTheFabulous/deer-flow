"""裂变方向挑选：策划只出候选（带给人看的简述），人勾选确认后才建任务，选中的方向一直出到出完。"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from conftest import (
    card_buttons,
    card_text,
    confirm_all,
    envelope,
    p05_task,
    reopen,
    set_config,
    slot_changes,
    use_small,
)
from podflow import bg, cli, directions, notify, review, ui
from podflow.db import open_review
from podflow.paths import SCHEMAS_DIR
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.tick import tick
from podflow.util import loads
from podflow.worker import Worker
from test_qc_rules import one_model
from test_ui import _enable_feishu, run


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """拦下后台进程：只记录启动参数，pid 用本进程（视为存活）。"""
    calls: list[dict] = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


def _finish_background(ctx) -> None:
    """拦下的后台任务当作已经跑完，别挡着后面的出图。"""
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")


def _seed_family(ctx, seed_image) -> str:
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    return seedmod.analyze(ctx, seed["seed_id"])["steps"][-1]["family_id"]


def _open_types(ctx) -> list[str]:
    return [
        r["subject_type"]
        for r in ctx.db.q(
            "SELECT subject_type FROM reviews WHERE status = 'open' ORDER BY review_id"
        )
    ]


# ---------------------------------------------------------------- 给人看的简述


def test_brief_follows_changed_fields() -> None:
    task = {
        "title": "极地冷色三伙伴（超长的标题会被截短）",
        "brief": [
            {
                "field_id": "D09",
                "from": "狐狸、兔子、刺猬",
                "to": "北极熊、北极狐、企鹅",
            },
            {"field_id": "D42", "from": "水彩", "to": "版画"},  # 没有改这个字段：丢掉
            {"field_id": "D09", "from": "x", "to": "重复的字段只留第一条"},
        ],
        "keep": "三只动物陪伴",
        "changed_fields": [
            {"field_id": "D09", "to": "北极熊、北极狐、企鹅"},
            {
                "field_id": "D10",
                "to": {"option_id": "D10-O04", "value": "冰雪小企鹅"},
            },
            {"field_id": "D36", "from": "暖近白", "to": "冷调极浅灰"},
        ],
    }
    out = planmod.reconcile_brief(task)
    assert out["aspects"] == ["元素角色", "配色结构"]
    assert out["brief"] == [
        {
            "field_id": "D09",
            "aspect": "主体",
            "from": "狐狸、兔子、刺猬",
            "to": "北极熊、北极狐、企鹅",
        },
        {"field_id": "D10", "aspect": "陪衬角色", "from": "", "to": "冰雪小企鹅"},
        {"field_id": "D36", "aspect": "底色", "from": "暖近白", "to": "冷调极浅灰"},
    ]
    assert out["brief_filled"] == ["D10", "D36"]
    assert out["brief_dropped"] == ["D42", "D09"]
    assert len(out["title"]) == planmod.TITLE_CHARS and out["title"].endswith("…")
    assert out["keep"] == "三只动物陪伴"
    # 没写标题时用字段短名拼一个
    assert (
        planmod.reconcile_brief(
            {"changed_fields": [{"field_id": "D18", "to": "前倾"}]}
        )["title"]
        == "动作姿态变化"
    )


def test_brief_items_use_field_ids_and_aspects_are_dimensions() -> None:
    schema = json.loads(
        (SCHEMAS_DIR / "P05.result.schema.json").read_text(encoding="utf-8")
    )
    brief = schema["properties"]["tasks"]["items"]["properties"]["brief"]["items"]
    assert (
        brief["required"] == ["field_id", "to"] and "aspect" not in brief["properties"]
    )
    # 方位是维度名；停用字段按并入的字段算（D38 面积关系 → D37 配色角色与面积）
    assert planmod.field_aspects(["D38", "D09", "D05"]) == [
        "主题叙事",
        "元素角色",
        "配色结构",
    ]
    assert planmod.axis_text(["D36", "D20"]) == "配色结构·底色+服饰道具·服饰配件"
    # 签名按「字段 + 改成的样子」：换个说法（标点）还是同一个方向
    a = {"changed_fields": [{"field_id": "D36", "to": "雾蓝底"}], "brief": []}
    b = {"changed_fields": [{"field_id": "D36", "to": "雾蓝底。"}], "brief": []}
    assert planmod.direction_signature(a) == planmod.direction_signature(b)
    old = {"changed_fields": [{"field_id": "D38", "to": "浅底大面积"}], "brief": []}
    new = {"changed_fields": [{"field_id": "D37", "to": "浅底大面积"}], "brief": []}
    assert planmod.direction_signature(old) == planmod.direction_signature(new)


# ---------------------------------------------------------------- 策划、勾选、确认


def test_plan_gives_candidates_with_brief_and_no_jobs(ctx, fake, seed_image) -> None:
    family_id = _seed_family(ctx, seed_image)
    assert _open_types(ctx) == ["family"]
    plan = planmod.plan_batch(ctx, family_id, limit=2)
    assert plan["status"] == "proposed" and len(plan["accepted"]) == 2
    first = plan["accepted"][0]
    assert first["index"] == 1 and first["title"] == "主题方案1"
    assert first["aspects"] == ["主题叙事"] and first["task_type"] == "big"
    assert first["brief"][0] == {
        "field_id": "D05",
        "aspect": "主题",
        "from": "原样",
        "to": "主题方案1",
    }
    assert ctx.db.scalar("SELECT COUNT(*) FROM jobs") == 0
    # 系列规则并进「裂变方向」：只审一次
    assert _open_types(ctx) == ["plan_selection"]
    # P05 拿到这一组的槽位：字段、当前取值、系列给的选项
    texts = [
        json.dumps(json.loads(r["body"]), ensure_ascii=False)
        for r in fake.requests
        if r["path"] == "/v1/chat/completions"
    ]
    # 3 个单字段大改一块（plan.slots_per_call 默认 4）
    p05 = [t for t in texts if "plan_group" in t]
    assert len(p05) == 1
    assert '\\"slot_id\\": \\"B1\\"' in p05[0] and "海洋伙伴" in p05[0]


def test_pick_then_confirm_builds_jobs_only_for_chosen(ctx, fake, seed_image) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=3)
    batch_id = plan["batch_id"]
    with pytest.raises(seedmod.StageError, match="还没选方向"):
        directions.confirm(ctx, batch_id, by="tester")
    directions.pick(ctx, batch_id, "1")
    shown = directions.pick(ctx, batch_id, "3")
    assert shown["picked"] == [
        plan["accepted"][0]["variant_id"],
        plan["accepted"][2]["variant_id"],
    ]
    shown = directions.pick(ctx, batch_id, plan["accepted"][0]["variant_id"], on=False)
    assert shown["picked"] == [plan["accepted"][2]["variant_id"]]
    directions.pick(ctx, batch_id, "1")
    with pytest.raises(seedmod.StageError, match="没有方向 9"):
        directions.pick(ctx, batch_id, "9")

    result = directions.confirm(ctx, batch_id, by="feishu:ou_x")
    assert result["family_approved"] is True
    assert result["picked"] == [
        plan["accepted"][0]["variant_id"],
        plan["accepted"][2]["variant_id"],
    ]
    assert result["jobs"] == ["J0001", "J0002"] and set(
        result["images_per_direction"].values()
    ) == {1}
    fam = ctx.db.q1(
        "SELECT status, approved_by FROM families WHERE family_id = ?", (family_id,)
    )
    assert fam["status"] == "approved" and fam["approved_by"] == "feishu:ou_x"
    batch = ctx.db.q1(
        "SELECT status, size FROM batches WHERE batch_id = ?", (batch_id,)
    )
    assert batch["status"] == "planned" and batch["size"] == 2
    assert _open_types(ctx) == []
    rejected = plan["accepted"][1]["variant_id"]
    vplan = loads(
        ctx.db.scalar(
            "SELECT plan_json FROM variants WHERE variant_id = ?", (rejected,)
        )
    )
    assert vplan["selection"]["status"] == "rejected"
    with pytest.raises(seedmod.StageError, match="不能再挑"):
        directions.pick(ctx, batch_id, "2")

    # 没选的方向进下一轮 P05 的排除项
    _, _, patterns = planmod._history(ctx, family_id)
    assert {"variant_id": rejected, "reason": "人工挑选时没选"}.items() <= next(
        p for p in patterns if p["variant_id"] == rejected
    ).items()

    # 跑完一轮，S11 只统计出过图的方向
    Worker(ctx, max_minutes=5).run()
    report = ctx.db.q1(
        "SELECT report_json FROM batches WHERE batch_id = ?", (batch_id,)
    )
    assert report["report_json"]
    p11 = [r for r in fake.requests if r["path"] == "/v1/chat/completions"][-1]
    body = json.loads(p11["body"])
    assert rejected not in json.dumps(body["messages"][-1], ensure_ascii=False)


def test_pick_all_and_none_then_confirm_all(ctx, fake, seed_image, capsys) -> None:
    """挑选卡的全选 / 全不选：all 一次勾上 / 取消全部；确认时 --pick all 选中全部；确认后不能再挑。"""
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    batch_id = plan["batch_id"]
    every = [a["variant_id"] for a in plan["accepted"]]
    directions.pick(ctx, batch_id, "2")
    assert directions.pick(ctx, batch_id, "all")["picked"] == every
    view = ui.directions_view(ctx, batch_id)
    assert all(item["checker"]["checked"] for item in view["items"])
    assert view["pick_actions"] == [
        {
            "action": "pod_plan_pick_all",
            "label": "全选",
            "style": "default",
            "batch": batch_id,
            "on": 1,
        },
        {
            "action": "pod_plan_pick_all",
            "label": "全不选",
            "style": "default",
            "batch": batch_id,
            "on": 0,
        },
    ]
    assert "先点「全选」" in view["hint"]
    assert directions.pick(ctx, batch_id, "ALL", on=False)["picked"] == []
    assert not any(
        item["checker"]["checked"]
        for item in ui.directions_view(ctx, batch_id)["items"]
    )
    # 命令行：pick B00x all（--off 全部取消）
    assert run(capsys, "directions", "pick", batch_id, "all")["picked"] == every
    assert run(capsys, "directions", "pick", batch_id, "all", "--off")["picked"] == []

    result = directions.confirm(
        ctx, batch_id, by="tester", pick=directions.parse_refs("全部")
    )
    assert result["picked"] == every and len(result["jobs"]) == len(every)
    assert ui.directions_view(ctx, batch_id)["pick_actions"] == []
    with pytest.raises(seedmod.StageError, match="不能再挑"):
        directions.pick(ctx, batch_id, "all")


def test_reject_with_replan_starts_a_new_round(ctx, fake, seed_image, spawned) -> None:
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=2)
    out = directions.reject(ctx, plan["batch_id"], by="tester", replan=True)
    assert out["rejected"] == 2 and out["replan_started"].startswith("BG")
    assert spawned[0]["argv"][2:] == ["plan", "--family", family_id]
    assert ctx.db.q1("SELECT status FROM batches")["status"] == "rejected"
    # 系列还待认可（没人批准过）；工作台给「策划」按钮
    assert ctx.db.q1("SELECT status FROM families")["status"] == "pending"
    texts = [i["text"] for i in ui.timeline(ctx)["items"]]
    assert "B001 的方向都没选中，换一批（tester）" in texts
    # 新一轮不会再提人工没选的方向：P05 看得到它们，模型照着写了一模一样的也会被丢掉
    fake.p05_tasks = lambda group: [
        p05_task(s["slot_id"], slot_changes(s, "方案1")) for s in group["slots"]
    ]
    again = planmod.plan_batch(ctx, family_id, limit=2)
    assert again["accepted"] == [] and {d["reason"] for d in again["dropped"]} == {
        "duplicate_plan"
    }
    # 两轮各一块（每轮 2 个单字段大改）
    assert [g["kind"] for g in fake.p05_groups] == ["big", "big"]
    p05 = [
        json.dumps(json.loads(r["body"]), ensure_ascii=False)
        for r in fake.requests
        if r["path"] == "/v1/chat/completions"
    ]
    assert any(
        "主题方案1" in t and "没选" in t and "completed_plan_index" in t for t in p05
    )


def test_revision_retires_unpicked_candidates_and_plans_again(
    ctx, fake, seed_image, capsys
) -> None:
    family_id = _seed_family(ctx, seed_image)
    first = planmod.plan_batch(ctx, family_id, limit=2)
    out = run(capsys, "family", "revise", family_id, "--note", "中间是小象不是考拉")
    assert out["family_version"] == 2 and out["plan"]["status"] == "proposed"
    old = ctx.db.q1(
        "SELECT status FROM batches WHERE batch_id = ?", (first["batch_id"],)
    )
    assert old["status"] == "superseded"
    # 旧版本上没人看过的候选直接作废（删掉、不占计划指纹）：新一轮可以重新提同样的方向
    assert (
        ctx.db.scalar(
            "SELECT COUNT(*) FROM variants WHERE batch_id = ?", (first["batch_id"],)
        )
        == 0
    )
    assert len(out["plan"]["accepted"]) == 3
    assert _open_types(ctx) == ["plan_selection"]
    view = ui.directions_view(ctx, out["plan"]["batch_id"])
    assert any(line.startswith("修订意见：中间是小象") for line in view["lines"])
    # 系列认可之后，挑选卡不再列修订意见和保留项
    directions.pick(ctx, out["plan"]["batch_id"], "1")
    directions.confirm(ctx, out["plan"]["batch_id"], by="tester")
    view = ui.directions_view(ctx, out["plan"]["batch_id"])
    assert view["lines"] == [
        "参考图 S01 · 系列 F01 v2",
        "按可变字段策划：小变化 0 个（改一个字段、同类元素一起改，构图不动）· 大变化 3 个",
    ]


def test_cli_analyze_records_plan_error_and_keeps_family_review(
    home, fake, seed_image, capsys
) -> None:
    def broken(payload):
        return json.dumps(envelope("P05", {"tasks": "不是列表"}), ensure_ascii=False)

    fake.overrides["P05"] = broken
    run(capsys, "seed", "add", str(seed_image), "--source-ref", "测试")
    out = run(capsys, "seed", "analyze", "S01")
    assert "StageOutputInvalid" in out["plan_error"]
    ctx = reopen(home)
    # 策划失败：系列规则的待审还在，工作台给「策划」按钮
    assert _open_types(ctx) == ["family"]
    actions = [a for s in ui.dashboard(ctx)["next_steps"] for a in s["actions"]]
    plan_button = next(a for a in actions if a["action"] == "pod_plan")
    assert plan_button == {
        "action": "pod_plan",
        "label": "策划 3 个方向",
        "style": "primary",
        "family_id": "F01",
    }
    card = notify.bg_card(
        ctx,
        {"kind": "analyze", "subject": "S01"},
        bg.summarize("analyze", out),
        ok=True,
    )
    assert "策划裂变方向没成功" in card_text(card)
    assert card_buttons(card)[0]["value"] == {"action": "pod_plan", "family_id": "F01"}


# ---------------------------------------------------------------- 选中的方向出到出完


def test_chosen_directions_run_until_done(ctx, fake, seed_image, spawned) -> None:
    one_model(ctx.home)
    _enable_feishu(ctx.home)
    ctx = reopen(ctx.home)
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=2)
    result = confirm_all(ctx, plan, start=True)
    assert result["directions_started"].startswith("BG")
    # 拉起的 worker 不带 --job：每次领取都现查确认过的方向，之后新确认的批次也由它接着领
    assert spawned[0]["argv"][2:] == ["worker", "--source", "directions"]
    assert result["jobs"] == ["J0001", "J0002"]
    task = dict(ctx.db.q1("SELECT * FROM bg_tasks"))
    assert (
        task["kind"] == "worker"
        and task["subject"] == plan["batch_id"]
        and task["notify"] == 1
    )
    _finish_background(ctx)

    # 只领一个就停（--batch 1）：结束时接着拉起下一个 worker，这一次不发卡片
    first = Worker(ctx, batch_limit=1, max_minutes=5, source="directions").run()
    assert first["stop_reason"] == "limit"
    assert first["continued"]["jobs"] == ["J0002"]
    summary = bg.summarize("worker", first)
    assert summary["continued"] is True
    assert (
        notify.bg_card(ctx, {"kind": "worker", "subject": None}, summary, ok=True)
        is None
    )
    _finish_background(ctx)

    # 最后一轮：没有剩下的，发卡片并写明出到哪了
    last = Worker(ctx, max_minutes=5, source="directions").run()
    assert last["continued"] == {}
    card = notify.bg_card(
        ctx, {"kind": "worker", "subject": None}, bg.summarize("worker", last), ok=True
    )
    assert "出图一轮结束" in card_text(card)


def test_tick_resumes_chosen_directions_without_trial_window(
    ctx, fake, seed_image, spawned
) -> None:
    family_id = _seed_family(ctx, seed_image)
    confirm_all(ctx, planmod.plan_batch(ctx, family_id, limit=1))
    out = tick(ctx)
    assert out["decision"] == "dispatch" and "已选的裂变方向" in out["reason"]
    assert spawned[0]["argv"][2:] == ["worker", "--source", "directions"]
    assert out["jobs"][0]["kind"] == "worker"
    # 已经有出图在跑：不重复拉起
    assert tick(ctx)["decision"] == "stop"
    assert len(spawned) == 1
    # 暂停时不接
    _finish_background(ctx)
    cli.main(["pause", "--reason", "测试"])
    ctx = reopen(ctx.home)
    assert tick(ctx)["decision"] == "stop" and len(spawned) == 1
    # 恢复运行时接上
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "0")
    assert directions.start_runs(ctx)["directions_started"]
    assert len(spawned) == 2


def test_legacy_batches_are_not_auto_continued(ctx, fake, seed_image) -> None:
    family_id = _seed_family(ctx, seed_image)
    confirm_all(ctx, planmod.plan_batch(ctx, family_id, limit=1))
    # 旧批次没有挑选记录：照旧等人点「开始出图」或连续运行
    with ctx.db.tx() as conn:
        conn.execute("UPDATE batches SET plan_json = '{}'")
    assert directions.pending_jobs(ctx) == [] and directions.start_runs(ctx) == {}


# ---------------------------------------------------------------- 工作台视图


def test_directions_view_and_review_link(ctx, fake, seed_image, spawned) -> None:
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=2)
    batch_id = plan["batch_id"]
    view = ui.directions_view(ctx, batch_id)
    assert view["title"] == f"挑选裂变方向｜{batch_id}" and view["editable"] is True
    assert view["lines"][0] == "参考图 S01 · 系列 F01 v1（待认可：确认方向时一并认可）"
    assert (
        "识别出的主体：长颈鹿、小象、小熊（认错了就进入 POD 对话说）" in view["lines"]
    )
    assert (
        "按可变字段策划：小变化 0 个（改一个字段、同类元素一起改，构图不动）· 大变化 2 个"
        in view["lines"]
    )
    # 这张图哪些维度不适用、哪些字段拆解判定不能变
    assert (
        "不适用：排列重复（单幅场景没有重复花型，改排列等于改构图）；文字定制（图上没有文字）"
        in view["lines"]
    )
    assert any(line.startswith("拆解判定不能变的字段 ") for line in view["lines"])
    item = view["items"][0]
    assert item["title"] == "主题方案1" and item["tag"] == "主题叙事"
    assert item["lines"] == ["主题：原样 → 主题方案1"]
    assert (
        item["section"]
        == "大变化（2）：每个出 9 张图（3 个模型 × 文生图 / 图生图 / 图生图 Edit）"
    )
    assert item["keep"] == "三只陪伴的构图"
    # 勾选清单：每个方向一个勾选框，表单提交一次
    assert item["checker"] == {"name": f"pick_{item['variant_id']}", "checked": False}
    assert view["form"] == {
        "name": f"pick_{batch_id}",
        "submit": {
            "action": "pod_plan_confirm",
            "label": "按勾选的方向出图",
            "style": "primary",
            "batch": batch_id,
        },
    }
    assert [a["action"] for a in view["actions"]] == ["pod_plan_reject", "pod_history"]
    assert view["actions"][1]["family"] == family_id
    assert view["status_line"] == "已选 0 / 2 个方向"
    assert f"「{batch_id} 选 1、3」" in view["hint"]
    assert view["images"] and os.path.isfile(view["images"][0]["path"])

    # 对话里勾过的方向，卡片上默认勾着
    directions.pick(ctx, batch_id, "2")
    view = ui.directions_view(ctx, batch_id)
    assert [i["checker"]["checked"] for i in view["items"]] == [False, True]
    assert view["status_line"] == "已选 1 / 2 个方向"

    # 待审页：裂变方向一项带「去挑选」，不放决策按钮
    page = ui.reviews_page(ctx, types=["plan_selection"])
    entry = page["items"][0]
    assert entry["actions"] == [] and entry["links"] == [
        {
            "action": "pod_plan_view",
            "label": "去挑选",
            "style": "primary",
            "batch": batch_id,
        }
    ]
    assert (
        entry["lines"][0]
        == "2 个候选方向（小变化 0 · 大变化 2），已选 1 个；选中的才出图"
    )
    dash = ui.dashboard(ctx)
    assert dash["next_steps"][0]["text"].startswith(
        "S01 的 2 个裂变方向待你挑选（已选 1 个）"
    )

    # 待审里直接点「确认」也一样：按已勾选的确认并开始出图
    result = review.decide(ctx, entry["review_id"], "approved", by="feishu:ou_x")
    assert (
        result["effect"]["jobs"] == ["J0001"] and result["effect"]["directions_started"]
    )
    view = ui.directions_view(ctx, batch_id)
    assert view["editable"] is False and view["form"] is None
    assert [a["action"] for a in view["actions"]] == ["pod_history"]
    assert [i["checker"] for i in view["items"]] == [None, None]
    assert [i["state"] for i in view["items"]] == ["没选", "已选（待推进）"]
    assert view["status_line"] == "已确认：选了 1 个方向出图（飞书）"
    texts = [i["text"] for i in ui.timeline(ctx)["items"]]
    assert "B001 选了 1 个方向出图（飞书）" in texts
    assert not any(t.startswith("裂变方向") for t in texts)


def test_selection_cards_show_direction_title(ctx, fake, seed_image) -> None:
    one_model(ctx.home)
    ctx = reopen(ctx.home)
    family_id = _seed_family(ctx, seed_image)
    confirm_all(ctx, planmod.plan_batch(ctx, family_id, limit=1))
    Worker(ctx, max_minutes=5).run()
    item = ui.reviews_page(ctx, types=["asset_selection"])["items"][0]
    # 选款项写明这张候选属于哪个裂变方向：改了哪几处、什么不变，再写模型和出图方式
    assert item["lines"][:3] == [
        "方向：V0001 主题方案1（大变化 · 主题叙事）· B001",
        "· 主题：原样 → 主题方案1",
        "不变：三只陪伴的构图",
    ]
    # 文生图、图生图同时出，哪张先进选款不一定
    assert item["lines"][3] in (
        "出图：gpt-2.5-flare · 文生图",
        "出图：gpt-2.5-flare · 图生图",
    )


def test_small_change_selection_item_says_rest_stays(ctx, fake, seed_image) -> None:
    use_small(fake)
    one_model(ctx.home)
    set_config(ctx.home, "image_generation.i2i_edit", False)
    ctx = reopen(ctx.home)
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id)
    directions.confirm(ctx, plan["batch_id"], by="tester", pick=["1"])
    Worker(ctx, max_minutes=5).run()
    item = ui.reviews_page(ctx, types=["asset_selection"])["items"][0]
    # 小变化只做图生图，改两个字段：「不变」写其余照原图
    assert item["lines"][:5] == [
        "方向：V0001 小变化1-1（小变化 · 元素角色 + 表情动作）· B001",
        "· 主体：原样 → 主体S1-1",
        "· 表情：原样 → 表情S1-1",
        "不变：三只陪伴的构图",
        "出图：gpt-2.5-flare · 图生图",
    ]
    view = ui.directions_view(ctx, plan["batch_id"])
    assert (
        "按可变字段策划：小变化 3 个（改两个字段、同类元素一起改，构图不动）· 大变化 5 个"
        in (view["lines"])
    )
    small = view["items"][0]
    assert small["section"].startswith(
        "小变化（3）：改两个字段（同类元素一起改），其余照原图，每个出 1 张图（1 个模型 × 图生图）"
    )
    assert small["tag"] == "元素角色 + 表情动作" and small["keep"] == ""
    # 槽位按字段字典顺序排：小变化、单字段大改，三处组合排在最后
    third, switch = view["items"][2], view["items"][3]
    assert (third["title"], third["tag"]) == ("小变化3-1", "表情动作 + 配色结构")
    assert third["section"].startswith("小变化（3）")
    assert switch["title"] == "绘制技法方案1" and switch["section"].startswith(
        "大变化（5）"
    )
    assert view["items"][4]["tag"] == "元素角色 + 表情动作 + 配色结构"


# ---------------------------------------------------------------- 方向记录与带要求的策划


def _p05_payloads(fake) -> list[str]:
    texts = [
        json.dumps(json.loads(r["body"]), ensure_ascii=False)
        for r in fake.requests
        if r["path"] == "/v1/chat/completions"
    ]
    return [t for t in texts if "completed_plan_index" in t]


def test_direction_records_are_compact_and_block_repeats(ctx, fake, seed_image) -> None:
    family_id = _seed_family(ctx, seed_image)
    first = planmod.plan_batch(ctx, family_id, limit=2)
    directions.confirm(ctx, first["batch_id"], by="tester", pick=["1"])
    # 改版前的老变体：没有标题和简述
    legacy = first["accepted"][0]["variant_id"]
    vplan = loads(
        ctx.db.scalar("SELECT plan_json FROM variants WHERE variant_id = ?", (legacy,))
    )
    for key in ("title", "brief", "aspects", "keep", "selection"):
        vplan.pop(key)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE variants SET plan_json = ? WHERE variant_id = ?",
            (json.dumps(vplan, ensure_ascii=False), legacy),
        )
    records = planmod.direction_records(ctx, family_id)
    assert [(r["title"], r["state_text"]) for r in records] == [
        ("主题叙事·主题", "待出图"),
        ("构图类型方案1", "没选"),
    ]
    # 老变体按改动字段补出简述
    assert records[0]["brief"] == [
        {"field_id": "D05", "aspect": "主题", "from": "原样", "to": "主题方案1"}
    ]
    # 给 P05 的方向记录按字段汇总：只留改成的样子和状态
    assert planmod.field_history(records) == {
        "D05": [{"to": "主题方案1", "kind": "单字段大改", "status": "待出图"}],
        "D27": [{"to": "构图类型方案1", "kind": "单字段大改", "status": "没选"}],
    }

    def tasks(group):
        out = []
        for s in group["slots"]:
            (field,) = s["fields"]
            if field["field_id"] == "D05":
                # 换了长描述，改成的样子（简述）和做过的一样
                out.append(
                    {
                        **p05_task(
                            s["slot_id"],
                            [("D05", "海洋主题：鲸鱼、海龟、小丑鱼一起游")],
                        ),
                        "brief": [
                            {"field_id": "D05", "from": "原样", "to": "主题方案1"}
                        ],
                    }
                )
            else:
                out.append(
                    p05_task(
                        s["slot_id"],
                        [(field["field_id"], f"海洋{field['label']}")],
                    )
                )
        return out

    fake.p05_tasks = tasks
    again = planmod.plan_batch(ctx, family_id, hint="  换成  海洋动物 ")
    assert again["hint"] == "换成 海洋动物"
    assert [t["title"] for t in again["accepted"]] == [
        "海洋构图类型方向",
        "海洋视觉中心方向",
    ]
    # 3 个大字段都挑中、按字段顺序排：主题叙事是 B1
    assert [(d["reason"], d["slot_id"]) for d in again["dropped"]] == [
        ("duplicate_direction", "B1"),
    ]
    assert ui.DROP_REASON_LABELS["duplicate_direction"] == "和做过的方向一样"

    # P05 拿到这些字段上做过的方向与这一轮的要求
    payloads = _p05_payloads(fake)
    assert "没有额外要求" in payloads[0] and "user_direction_request" in payloads[0]
    assert "换成 海洋动物" in payloads[-1]
    theme = [p for p in payloads if "换成 海洋动物" in p and "主题方案1" in p]
    assert theme and "待出图" in theme[0]

    # 要求显示在挑选卡、任务明细和动态里
    assert directions.show(ctx, again["batch_id"])["hint"] == "换成 海洋动物"
    view = ui.directions_view(ctx, again["batch_id"])
    assert "这一轮的要求：换成 海洋动物" in view["lines"]
    texts = [i["text"] for i in ui.timeline(ctx)["items"]]
    assert any(
        "提出 2 个裂变方向（小变化 0 · 大变化 2）（要求：换成 海洋动物）" in t
        for t in texts
    )
    summary = bg.summarize("plan", again)
    assert summary["hint"] == "换成 海洋动物"
    card = notify.bg_card(ctx, {"kind": "plan", "subject": family_id}, summary, ok=True)
    assert "这一轮的要求：换成 海洋动物" in card_text(card)


def test_hint_round_without_new_directions_does_not_exhaust(
    ctx, fake, seed_image
) -> None:
    family_id = _seed_family(ctx, seed_image)
    confirm_all(ctx, planmod.plan_batch(ctx, family_id, limit=2))
    # 每个槽位都只给出做过的方向：全部丢掉
    fake.p05_tasks = lambda group: [p05_task("D05", [("D05", "主题方案1")])]
    out = planmod.plan_batch(ctx, family_id, hint="换成海洋动物")
    # 只说明这个要求做不出新方向，系列照旧能策划
    assert out["status"] == "empty" and out["exhausted"] is False
    assert ctx.db.q1("SELECT status FROM families")["status"] == "approved"
    out = planmod.plan_batch(ctx, family_id)
    assert out["exhausted"] is True
    assert ctx.db.q1("SELECT status FROM families")["status"] == "exhausted"
    with pytest.raises(seedmod.StageError, match="最多 100 个字"):
        planmod.clean_hint("海" * 101)
    assert planmod.clean_hint("   ") is None


def test_replan_keeps_the_hint(ctx, fake, seed_image, spawned) -> None:
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=2, hint="-冬季主题")
    directions.reject(ctx, plan["batch_id"], by="tester", replan=True)
    argv = spawned[0]["argv"][2:]
    assert argv == ["plan", "--family", family_id, "--hint=-冬季主题"]
    # 以「-」开头的要求照样解析成要求
    assert cli.build_parser().parse_args(argv).hint == "-冬季主题"


def test_replan_keeps_the_kinds(ctx, fake, seed_image, spawned) -> None:
    """只策划部分类型的一轮，换一批也只策划这几类。"""
    use_small(fake)
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, kinds=["small", "combo"])
    directions.reject(ctx, plan["batch_id"], by="tester", replan=True)
    argv = spawned[0]["argv"][2:]
    assert argv == ["plan", "--family", family_id, "--kinds=combo,small"]
    assert cli.build_parser().parse_args(argv).kinds == "combo,small"


def test_blocked_directions_resume_once_backlog_clears(
    ctx, fake, seed_image, spawned
) -> None:
    """因待审积压阻断的已选方向：积压降回上限以下后接着出；要人处理的阻断不接。"""
    set_config(ctx.home, "batch.review_backlog_limit", 1)
    ctx = reopen(ctx.home)
    family_id = _seed_family(ctx, seed_image)
    plan = planmod.plan_batch(ctx, family_id, limit=2)
    first, second = confirm_all(ctx, plan)["jobs"]
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'blocked', blocked_reason = 'review_backlog' WHERE job_id = ?",
            (first,),
        )
        conn.execute(
            "UPDATE jobs SET status = 'blocked', blocked_reason = 'needs_human' WHERE job_id = ?",
            (second,),
        )
        open_review(conn, "asset_selection", "IMG99999", "积压中的候选")
    # 积压还在上限：不接
    assert directions.start_runs(ctx) == {}
    assert directions.pending_jobs(ctx) == []
    with ctx.db.tx() as conn:
        conn.execute("UPDATE reviews SET status = 'dismissed'")
    out = directions.start_runs(ctx)
    assert out["jobs"] == [first] and out["directions_started"].startswith("BG")
    assert spawned[-1]["argv"][2:] == ["worker", "--source", "directions"]


def test_history_view_lists_batches_and_gates_planning(
    ctx, fake, seed_image, spawned
) -> None:
    empty = ui.history_view(ctx)
    assert empty["kind"] == "families" and empty["families"] == []
    family_id = _seed_family(ctx, seed_image)
    # 只有一个系列：不指定也直接显示它；还没有方向时就能策划
    view = ui.history_view(ctx)
    assert view["kind"] == "family" and view["family_id"] == family_id
    assert view["groups"] == [] and view["form"]["submit"]["label"] == "再策划 3 个方向"
    assert "limit" not in view["form"]["submit"]

    first = planmod.plan_batch(ctx, family_id, limit=2)
    view = ui.history_view(ctx, family_id)
    # 有待挑选的批次：换成「去挑选」
    assert view["form"] is None and view["note"] == "还有一批方向待挑选（B001）"
    assert view["actions"] == [
        {
            "action": "pod_plan_view",
            "label": "去挑选 B001",
            "style": "primary",
            "batch": "B001",
        }
    ]
    assert view["groups"][0]["lines"] == [
        "1. 主题方案1（主题叙事）— 待挑选",
        "2. 构图类型方案1（整体构图）— 待挑选",
    ]

    directions.confirm(ctx, first["batch_id"], by="tester", pick=["1"])
    view = ui.history_view(ctx, family_id)
    # 任务明细：顶部是这张参考图走到哪一步，每个方向写细到哪一步
    assert view["title"] == f"任务明细｜S01 · {family_id}"
    assert view["lines"][1] == "拆解 ✓ › 策划 ✓ › 挑选 ✓ › **出图 0/1** › 选款"
    assert view["lines"][2] == "现在：B001 有 1 个方向排队"
    assert view["lines"][-1] == "做过 2 个方向：待出图 1、没选 1；选中的款 0 张"
    assert view["groups"][0]["head"].startswith("**B001**")
    assert "选了 1 个，出完 0 个" in view["groups"][0]["head"]
    assert view["groups"][0]["lines"] == [
        "1. 主题方案1（主题叙事）— 待出图",
        "2. 构图类型方案1（整体构图）— 没选",
    ]
    assert [a["label"] for a in view["actions"]] == ["开始出图"]
    assert view["form"] == {
        "name": f"plan_{family_id}",
        "input": {
            "name": f"hint_{family_id}",
            "placeholder": "这次想要的方向（可选），如：换成海洋动物、冬季主题",
            "max_length": 100,
        },
        "submit": {
            "action": "pod_plan",
            "label": "再策划 3 个方向",
            "style": "primary",
            "family_id": family_id,
            "back": "tasks",
        },
    }
    assert view["note"] is None and view["nav"] == []

    # 正在策划：不能再点
    bg.spawn(ctx, ["plan", "--family", family_id], kind="plan", subject=family_id)
    view = ui.history_view(ctx, family_id)
    assert view["form"] is None and view["note"] == "正在策划，完成后会通知你"
    _finish_background(ctx)

    # 每页 3 个批次，从新到旧
    for _ in range(3):
        plan = planmod.plan_batch(ctx, family_id, limit=1)
        directions.reject(ctx, plan["batch_id"], by="tester")
    view = ui.history_view(ctx, family_id)
    assert [g["batch_id"] for g in view["groups"]] == ["B004", "B003", "B002"]
    assert [(a["label"], a["offset"]) for a in view["nav"]] == [("更早的批次", 3)]
    older = ui.history_view(ctx, family_id, offset=3)
    assert [g["batch_id"] for g in older["groups"]] == ["B001"]
    assert [(a["label"], a["offset"]) for a in older["nav"]] == [("更新的批次", 0)]
    assert older["lines"] == view["lines"]


def test_dashboard_offers_history_once_directions_exist(ctx, fake, seed_image) -> None:
    family_id = _seed_family(ctx, seed_image)

    def history_buttons() -> list[dict]:
        steps = ui.dashboard(ctx)["next_steps"]
        return [a for s in steps for a in s["actions"] if a["action"] == "pod_history"]

    assert history_buttons() == []
    plan = planmod.plan_batch(ctx, family_id, limit=1)
    directions.reject(ctx, plan["batch_id"], by="tester")
    assert history_buttons() == [
        {
            "action": "pod_history",
            "label": "任务明细",
            "style": "default",
            "family": family_id,
        }
    ]


def test_reanalyze_turns_a_legacy_family_into_field_planning(
    ctx, fake, seed_image, capsys
) -> None:
    """按旧字段字典拆的系列：工作台提示重新拆解；--reanalyze 只重跑 S02–S04 修订原系列，方向记录接着算。"""
    family_id = _seed_family(ctx, seed_image)
    first = planmod.plan_batch(ctx, family_id, limit=1)
    directions.confirm(ctx, first["batch_id"], by="tester", pick=["1"])
    # 造一份梳理字段之前的系列与拆解
    spec = loads(ctx.db.scalar("SELECT spec_json FROM families"))
    spec.pop("field_schema")
    result = loads(ctx.db.scalar("SELECT result_json FROM analyses"))
    result.pop("variation")
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE families SET spec_json = ?", (json.dumps(spec, ensure_ascii=False),)
        )
        conn.execute(
            "UPDATE analyses SET result_json = ?",
            (json.dumps(result, ensure_ascii=False),),
        )
    steps = ui.dashboard(ctx)["next_steps"]
    legacy = next(s for s in steps if "按旧字段拆的" in s["text"])
    assert "「S01 按新字段重新拆解」" in legacy["text"]
    assert legacy["actions"] == [
        {"action": "pod_enter_chat", "label": "进入 POD 对话", "style": "default"}
    ]
    assert ui.history_view(ctx, family_id)["note"].startswith(
        "这个系列是按旧字段字典拆的"
    )

    assert cli.main(["seed", "analyze", "S01", "--force", "--reanalyze"]) == 2
    capsys.readouterr()
    out = run(capsys, "seed", "analyze", "S01", "--reanalyze")
    assert out["reanalyzed"] is True and [s["stage"] for s in out["steps"]] == [
        "S02",
        "S03",
        "S04",
    ]
    assert out["steps"][0]["analysis_id"] == "S01-A02"
    assert (out["steps"][2]["family_id"], out["steps"][2]["family_version"]) == (
        family_id,
        2,
    )
    assert out["plan"]["status"] == "proposed" and out["plan"]["family_version"] == 2
    # S01 不重跑；P04 拿到修订说明
    assert fake.stage_counts["P01"] == 1
    p04 = [
        json.dumps(json.loads(r["body"]), ensure_ascii=False)
        for r in fake.requests
        if r["path"] == "/v1/chat/completions"
        and "P04 系列规则设计" in r["body"].decode("utf-8", "ignore")
    ]
    assert p04 and "重新拆解" in p04[-1]
    spec = loads(
        ctx.db.scalar("SELECT spec_json FROM families WHERE family_version = 2")
    )
    assert spec["field_schema"] == "0.5"
    # 方向记录接着算：上一版做过的方向进了这一轮 P05
    assert any("主题方案1" in t and "待出图" in t for t in _p05_payloads(fake)[-3:])
    assert not [
        s for s in ui.dashboard(ctx)["next_steps"] if "按旧字段拆的" in s["text"]
    ]


def test_reanalyze_resumes_after_s04_failed(ctx, fake, seed_image, capsys) -> None:
    """重拆停在 S04：再跑一次只补 S04，不重付 S02 / S03；系列建成后再重拆才从头来。"""
    family_id = _seed_family(ctx, seed_image)
    fake.overrides["P04"] = lambda payload: "不是 JSON"
    assert cli.main(["seed", "analyze", "S01", "--reanalyze"]) != 0
    capsys.readouterr()
    counts = (fake.stage_counts["P02"], fake.stage_counts["P03"])
    del fake.overrides["P04"]

    out = run(capsys, "seed", "analyze", "S01", "--reanalyze")
    assert [(s["stage"], s.get("resumed", False)) for s in out["steps"]] == [
        ("S02", True),
        ("S03", True),
        ("S04", False),
    ]
    assert out["steps"][0]["analysis_id"] == "S01-A02"
    assert (fake.stage_counts["P02"], fake.stage_counts["P03"]) == counts
    assert (out["steps"][2]["family_id"], out["steps"][2]["family_version"]) == (
        family_id,
        2,
    )

    again = run(capsys, "seed", "analyze", "S01", "--reanalyze")
    assert not any(s.get("resumed") for s in again["steps"])
    assert again["steps"][0]["analysis_id"] == "S01-A03"
    assert fake.stage_counts["P02"] == counts[0] + 1


def test_cli_plan_hint_and_history(ctx, fake, seed_image, capsys, spawned) -> None:
    family_id = _seed_family(ctx, seed_image)
    out = run(capsys, "plan", "--family", family_id, "--limit", "2", "--hint", "冬季")
    assert out["hint"] == "冬季"
    history = run(capsys, "directions", "history", family_id)
    assert history["batches"][0]["hint"] == "冬季" and history["total"] == 2
    assert history["availability"]["pending_batch"] == out["batch_id"]
    assert run(capsys, "ui", "history")["family_id"] == family_id
    # 要求超长：不起后台任务
    assert cli.main(["plan", "--family", family_id, "--hint", "海" * 101, "--detach"])
    assert "最多 100 个字" in capsys.readouterr().out and spawned == []
