"""换模型：图生图另用 -edit 模型出一张、旧任务开工时按当前配置换槽位、退役模型改用接替模型。"""

from __future__ import annotations

import json

import pytest
from conftest import EDIT_MODELS, FANOUT_MODELS, SLOTS_PER_JOB, reopen, set_config
from podflow import doctor, imagegen, ui
from podflow.config import ConfigError
from podflow.stages.common import fanout_models, fanout_slots, model_label
from podflow.util import loads
from podflow.worker import Worker
from test_pipeline import bootstrap

PRO = "gemini-3-pro-image-preview"
NANO = "gemini-nano-banana-2.1"


def _fanout(*models: str) -> list[dict]:
    return [{"model": m, "n": 1, "enabled": True} for m in models]


def _image_models(fake) -> set[str]:
    return {p["model"] for p in fake.image_payloads()}


def _p06_inputs(fake) -> list[str]:
    """发给 P06 的变量（system 之后的消息），序列化成文本方便查字样。"""
    out = []
    for r in fake.requests:
        if r["path"] != "/v1/chat/completions":
            continue
        payload = json.loads(r["body"])
        if "# 当前阶段：P06" in str(payload["messages"][0]["content"]):
            out.append(json.dumps(payload["messages"][1:], ensure_ascii=False))
    return out


def test_i2i_adds_an_edit_slot_per_model(home) -> None:
    ctx = reopen(home)
    # 文生图、原模型的图生图在前，-edit 的图生图排在最后（并发不够时先出原模型的）
    assert fanout_slots(ctx) == (
        [(f"{m}#1", m, "t2i") for m in FANOUT_MODELS]
        + [(f"{m}#1@i2i", m, "i2i") for m in FANOUT_MODELS]
        + [(f"{m}#1@i2i", m, "i2i") for m in EDIT_MODELS]
    )
    assert len(fanout_slots(ctx)) == SLOTS_PER_JOB
    assert fanout_models(ctx) == FANOUT_MODELS
    set_config(home, "image_generation.i2i_edit", False)
    assert [m for _, m, _ in fanout_slots(reopen(home))] == FANOUT_MODELS * 2
    set_config(home, "image_generation.i2i_edit", True)
    set_config(home, "image_generation.modes", ["t2i"])
    assert [m for _, m, _ in fanout_slots(reopen(home))] == FANOUT_MODELS


def test_fanout_must_not_list_edit_models(home, fake) -> None:
    set_config(home, "image_generation.fanout", _fanout(NANO, f"{NANO}-edit"))
    ctx = reopen(home)
    with pytest.raises(ConfigError, match="i2i_edit"):
        fanout_slots(ctx)
    check = next(
        c for c in doctor.run(ctx)["checks"] if c["check"].startswith("生图 fanout")
    )
    assert check["ok"] is False and f"{NANO}-edit" in check["detail"]


def test_current_model_replaces_retired_models(home) -> None:
    ctx = reopen(home)
    assert imagegen.current_model(ctx, PRO) == NANO
    assert imagegen.current_model(ctx, f"{PRO}-edit") == f"{NANO}-edit"
    assert imagegen.current_model(ctx, "gpt-image-2.5-flare-edit") == (
        "gpt-image-2.5-flare-edit"
    )
    # 映射以用户配置为准，不与样例合并：写成空的就不再替换
    set_config(home, "image_generation.replaced_models", {})
    assert imagegen.current_model(reopen(home), PRO) == PRO


def test_model_labels_and_slot_summary(home) -> None:
    assert model_label(NANO) == "nano-banana-2.1"
    assert model_label(f"{NANO}-edit") == "nano-banana-2.1-edit"
    assert model_label("gpt-image-2.5-sunburst-edit") == "gpt-2.5-sunburst-edit"
    # 已经出过的旧图照旧显示
    assert model_label(PRO) == "nano-banana-pro"
    ctx = reopen(home)
    assert ui._slots_text(ctx) == "9 张图（3 个模型 × 文生图 / 图生图 / 图生图 Edit）"
    set_config(home, "image_generation.i2i_edit", False)
    assert ui._slots_text(reopen(home)) == "6 张图（3 个模型 × 文生图 / 图生图）"


def test_old_jobs_switch_to_current_models_at_s06(home, fake, seed_image) -> None:
    # 换模型之前策划好的任务：nano-banana pro，只做文生图
    set_config(
        home,
        "image_generation.fanout",
        _fanout(PRO, "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"),
    )
    set_config(home, "image_generation.modes", ["t2i"])
    set_config(home, "image_generation.i2i_edit", False)
    ctx = reopen(home)
    bootstrap(ctx, seed_image, limit=1)
    assert sorted(r["slot_id"] for r in ctx.db.q("SELECT slot_id FROM job_slots")) == [
        f"{PRO}#1",
        "gpt-image-2.5-flare#1",
        "gpt-image-2.5-sunburst#1",
    ]

    # 换成现在的配置再开始出图：没发过请求的 pro 槽位撤掉，按配置补齐 9 个
    set_config(home, "image_generation.fanout", _fanout(*FANOUT_MODELS))
    set_config(home, "image_generation.modes", ["t2i", "i2i"])
    set_config(home, "image_generation.i2i_edit", True)
    ctx = reopen(home)
    Worker(ctx, max_minutes=5).run()
    slots = sorted(r["slot_id"] for r in ctx.db.q("SELECT slot_id FROM job_slots"))
    assert slots == sorted(s for s, _, _ in fanout_slots(ctx))
    assert _image_models(fake) == set(FANOUT_MODELS) | set(EDIT_MODELS)
    synced = loads(
        ctx.db.q1("SELECT payload_json FROM events WHERE event = 'slots_synced'")[
            "payload_json"
        ]
    )
    assert synced["removed"] == [f"{PRO}#1"] and len(synced["added"]) == 7
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM assets WHERE status = 'archived'")
        == SLOTS_PER_JOB
    )
    # P06 只看原模型：-edit 和原模型共用一套提示词
    inputs = _p06_inputs(fake)
    assert inputs and all(
        NANO in text and "-edit" not in text and PRO not in text for text in inputs
    )


def test_retired_model_left_in_fanout_draws_with_replacement(
    home, fake, seed_image
) -> None:
    set_config(home, "image_generation.fanout", _fanout(PRO))
    set_config(home, "image_generation.modes", ["t2i"])
    ctx = reopen(home)
    warn = next(
        c
        for c in doctor.run(ctx)["checks"]
        if c["check"].startswith("fanout 里没有已退役的模型")
    )
    assert warn["ok"] is False and warn["level"] == "warn" and PRO in warn["detail"]
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    assert _image_models(fake) == {NANO}
    asset = ctx.db.q1("SELECT slot_id, model FROM assets")
    assert (asset["slot_id"], asset["model"]) == (f"{PRO}#1", NANO)
