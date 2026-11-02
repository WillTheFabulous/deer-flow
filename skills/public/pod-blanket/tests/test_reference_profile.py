"""画面完整描述（S02D / P13）：拆解后写画面档案，系列、策划、出图指令、质检都照它写；
P06 附参考图，退回诊断文字与对象编号，返回 blocked 时任务停在 S06 不出图。"""

from __future__ import annotations

import json

import pytest

from conftest import (
    card_text,
    default_p05_tasks,
    default_result,
    envelope,
    input_block,
    p13_result,
    reopen,
    set_config,
)
from podflow import bg, cli, doctor, maintenance, notify
from podflow.stages import profile
from podflow.stages import seed as seedmod
from podflow.stages.common import STYLE_KEEP, STYLE_RESTYLE, style_scope
from podflow.util import loads
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_variant_rules import _chat_messages, _text


def _described(home):
    set_config(home, "describe.enabled", True)
    return reopen(home)


def _input(fake, stage: str, name: str, index: int = -1):
    return input_block({"messages": _chat_messages(fake, stage)[index]}, name)


def _images(messages: list[dict]) -> int:
    content = messages[1]["content"]
    return sum(1 for part in content if part.get("type") == "image_url")


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_analyze_writes_the_description_after_s02(home, fake, seed_image) -> None:
    ctx = _described(home)
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]] == ["S01", "S02", "S02D", "S03", "S04"]
    assert seedmod.step_label(ctx, "S02D") == "S02D 画面描述（3/6）"
    assert fake.stage_counts["P13"] == 1 and "description_error" not in out
    desc = profile.get_description(ctx, "S01-A01")
    assert desc["seed_id"] == "S01" and desc["model"] == "gpt-6.1-sol-high"
    assert [s["object_id"] for s in desc["result"]["subjects"]] == ["E01", "E02", "E03"]
    # P13 看图写：附一张参考图，输入里有拆解的对象和人的要求（这次没有）
    (messages,) = _chat_messages(fake, "P13")
    assert _images(messages) == 1
    assert _input(fake, "P13", "analysis")["objects"][0]["attributes"] == {
        "species": "长颈鹿"
    }
    assert _input(fake, "P13", "user_hint") == profile.NO_HINT
    # 系列规则拿到的是画面描述，只有中文部分
    prof = _input(fake, "P04", "reference_profile")
    assert prof["source"] == profile.DESCRIBED_SOURCE
    assert prof["harmony_rules"] and "prompt_en" not in prof
    assert all("en" not in s for s in prof["subjects"])


def test_description_failure_does_not_block_analysis(home, fake, seed_image) -> None:
    ctx = _described(home)
    fake.overrides["P13"] = lambda payload: "不是 JSON"
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]] == ["S01", "S02", "S02D", "S03", "S04"]
    assert out["description_error"].startswith("StageOutputInvalid")
    assert profile.get_description(ctx, "S01-A01") is None
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM events WHERE event = 'description_failed'")
        == 1
    )
    # 系列规则按拆解简版写
    assert _input(fake, "P04", "reference_profile")["source"] == profile.FALLBACK_SOURCE
    # 完成卡片写明没写成、怎么补
    summary = bg.summarize("analyze", out)
    card = notify.bg_card(ctx, {"kind": "analyze", "subject": "S01"}, summary, ok=True)
    assert "画面描述没写成" in card_text(card) and "重新写画面描述" in card_text(card)
    # 再跑拆解时只补写画面描述，别的阶段不重跑
    del fake.overrides["P13"]
    again = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in again["steps"]] == ["S02D"]
    assert profile.get_description(ctx, "S01-A01")


def test_p13_must_cover_every_object(home, fake, seed_image) -> None:
    ctx = _described(home)

    def respond(payload: dict) -> str:
        result = p13_result(input_block(payload, "analysis"))
        if fake.stage_counts["P13"] == 1:
            result["subjects"] = result["subjects"][:2]
            result["environment"] = [
                {
                    "object_ids": ["E99"],
                    "name": "树",
                    "appearance": "一棵树",
                    "rendering": "水彩",
                    "en": "a tree",
                }
            ]
        return json.dumps(envelope("P13", result), ensure_ascii=False)

    fake.overrides["P13"] = respond
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    assert fake.stage_counts["P13"] == 2
    feedback = _text(_chat_messages(fake, "P13")[-1][-1]["content"])
    assert "E03" in feedback and "E99" in feedback
    assert len(profile.get_description(ctx, "S01-A01")["result"]["subjects"]) == 3


def test_reanalyze_describes_the_new_analysis(home, fake, seed_image) -> None:
    ctx = _described(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    out = seedmod.analyze(ctx, "S01", reanalyze=True)
    assert [s["stage"] for s in out["steps"]][:3] == ["S02", "S02D", "S03"]
    assert profile.get_description(ctx, "S01-A02")
    assert fake.stage_counts["P13"] == 2


def test_seed_describe_cli(home, fake, seed_image, capsys) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    assert fake.stage_counts["P13"] == 0
    code, err = _cli(capsys, "seed", "describe", "S01")
    assert code == 2 and "describe.enabled" in err["detail"]
    ctx = _described(home)
    check = "拆解过的参考图都有画面描述（S02D）"
    warn = {c["check"]: c for c in doctor.run(ctx)["checks"]}[check]
    assert warn["ok"] is False and "S01" in warn["detail"]

    code, out = _cli(capsys, "seed", "describe", "S01")
    assert code == 0 and out["subjects"] == 3 and out["replaced"] is False
    code, out = _cli(capsys, "seed", "describe", "S01")
    assert code == 0 and "已有画面描述" in out["skipped"]
    assert fake.stage_counts["P13"] == 1
    code, out = _cli(capsys, "seed", "describe", "S01", "--hint", "左边那只是小鹿")
    assert code == 0 and out["replaced"] is True and out["hint"] == "左边那只是小鹿"
    assert _input(fake, "P13", "user_hint") == "左边那只是小鹿"
    code, out = _cli(capsys, "seed", "describe", "S01", "--force")
    assert code == 0 and fake.stage_counts["P13"] == 3
    code, shown = _cli(capsys, "seed", "describe", "S01", "--show")
    assert code == 0 and shown["description"]["prompt_en"]["style_anchor"]
    code, seed = _cli(capsys, "seed", "show", "S01")
    assert seed["description"]["subjects"] == 3
    code, out = _cli(capsys, "seed", "describe", "--all")
    assert code == 0 and out == {"described": [], "skipped": ["S01"], "failed": []}
    code, out = _cli(capsys, "seed", "describe", "--all", "--force")
    assert [d["seed_id"] for d in out["described"]] == ["S01"]
    assert fake.stage_counts["P13"] == 4
    for argv in (["S01", "--all"], [], ["--all", "--hint", "x"], ["--show"]):
        code, err = _cli(capsys, "seed", "describe", *argv)
        assert code == 2, argv
    ok = {c["check"]: c for c in doctor.run(reopen(home))["checks"]}[check]
    assert ok["ok"] is True


def test_profile_feeds_planning_and_prompts(home, fake, seed_image) -> None:
    ctx = _described(home)
    fake.p05_tasks = lambda group: [
        {**t, "style_fit": "新背景同样用水彩晕染"}
        for t in default_p05_tasks(fake, group)
    ]
    bootstrap(ctx, seed_image, limit=1)
    # 策划看中文档案（没有英文写法）
    p05 = _input(fake, "P05", "reference_profile")
    assert p05["source"] == profile.DESCRIBED_SOURCE and "prompt_en" not in p05
    Worker(ctx, max_minutes=5).run()
    # 写出图指令：带英文写法、附参考图、方向带画风处理
    p06 = _input(fake, "P06", "reference_profile")
    assert p06["prompt_en"]["style_anchor"].startswith("Soft children's-book")
    assert all(s["en"] for s in p06["subjects"])
    assert _images(_chat_messages(fake, "P06")[-1]) == 1
    variant = _input(fake, "P06", "variant_job")
    assert variant["style_scope"] == STYLE_KEEP
    assert variant["style_fit"] == "新背景同样用水彩晕染"


def test_fallback_profile_without_description(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    prof = _input(fake, "P05", "reference_profile")
    assert prof["source"] == profile.FALLBACK_SOURCE
    assert [s["object_id"] for s in prof["subjects"]] == ["E01", "E02", "E03"]
    assert prof["fields"]["D05 主题"] == "D05 的取值"
    assert "D41 绘制技法" in prof["fields"] and "D45 风格锚点" in prof["fields"]


def test_restricted_seed_gets_no_image_in_p06(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    seedmod.set_rights(ctx, "S01", "restricted", by="tester")
    Worker(ctx, max_minutes=5).run()
    assert _images(_chat_messages(fake, "P06")[-1]) == 0


def test_style_scope_by_changed_dimension() -> None:
    assert style_scope([{"field_id": "D37"}]) == STYLE_RESTYLE
    assert style_scope([{"field_id": "D42"}]) == STYLE_RESTYLE
    assert style_scope([{"field_id": "D09"}, {"field_id": "D37"}]) == STYLE_RESTYLE
    assert style_scope([{"field_id": "D07"}]) == STYLE_KEEP
    assert style_scope([{"field_id": "D27"}]) == STYLE_KEEP
    assert style_scope(None) == STYLE_KEEP


@pytest.mark.parametrize(
    ("bad", "said"),
    [
        (
            "COMPILATION BLOCKED — NOT A GENERATION PROMPT. The source artwork is not visible.",
            "诊断文字",
        ),
        (
            "Keep E01 and E02 on the left and turn E03 into a fawn on a branch.",
            "对象编号",
        ),
    ],
)
def test_p06_bad_prompts_are_sent_back(ctx, fake, seed_image, bad, said) -> None:
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        result = default_result("P06", payload, fake)
        if fake.stage_counts["P06"] == 1:
            result["prompts"] = {family: bad for family in result["prompts"]}
        return json.dumps(envelope("P06", result), ensure_ascii=False)

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    assert fake.stage_counts["P06"] == 2
    assert said in _text(_chat_messages(fake, "P06")[-1][-1]["content"])
    # 退回的原因记成事件，线上能查到为什么重写
    (rejected,) = ctx.db.q(
        "SELECT scope, payload_json FROM events WHERE event = 'stage_output_rejected'"
    )
    payload = loads(rejected["payload_json"])
    assert rejected["scope"].startswith("J") and payload["stage"] == "P06"
    assert any(said in e for e in payload["errors"])
    render = loads(ctx.db.scalar("SELECT render_spec_json FROM jobs"))
    assert all(text != bad for text in render["prompts"].values())
    assert ctx.db.scalar("SELECT status FROM jobs") == "complete"


def test_p06_blocked_fails_the_job_without_images(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)

    def respond(payload: dict) -> str:
        out = envelope("P06", default_result("P06", payload, fake), status="blocked")
        out["unknowns"] = ["看不出右边那只是什么动物"]
        return json.dumps(out, ensure_ascii=False)

    fake.overrides["P06"] = respond
    Worker(ctx, max_minutes=5).run()
    job = ctx.db.q1("SELECT status, last_error, render_spec_json FROM jobs")
    assert job["status"] == "failed" and job["render_spec_json"] is None
    assert "生成指令没写出来" in job["last_error"]
    assert "看不出右边" in job["last_error"]
    assert fake.image_payloads() == []


def test_purge_deletes_descriptions(home, fake, seed_image) -> None:
    ctx = _described(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_descriptions") == 1
    maintenance.purge_old(ctx, seeds=["S01"], by="tester")
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_descriptions") == 0
