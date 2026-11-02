"""参考图 2D 化（S01P 图案提取）：接入后先用图生图把参考图提取成平面图案，拆解、图生图、质检都用它。"""

from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace

import pytest

from conftest import reopen, set_config
from podflow import bg, images, maintenance, notify, store, ui
from podflow.stages import seed as seedmod
from podflow.stages.common import (
    StageError,
    get_seed,
    original_bytes,
    reference_path,
    seed_bytes,
)
from podflow.worker import Worker
from test_pipeline import bootstrap
from test_ui import _enable_feishu, run

EXTRACT_PREFIX = "You are a textile surface-design reconstruction specialist"


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bg, "_current", None)
    return calls


def _enable(home):
    set_config(home, "pattern_extract.enabled", True)
    return reopen(home)


def _extract_payloads(fake) -> list[dict]:
    return [p for p in fake.image_payloads() if p["prompt"].startswith(EXTRACT_PREFIX)]


def _user_content(fake, stage: str) -> list[dict]:
    for r in fake.requests:
        if r["path"] != "/v1/chat/completions":
            continue
        messages = json.loads(r["body"])["messages"]
        system = messages[0]["content"]
        text = system if isinstance(system, str) else json.dumps(system)
        if f"# 当前阶段：{stage}" in text:
            content = messages[1]["content"]
            return (
                content
                if isinstance(content, list)
                else [{"type": "text", "text": content}]
            )
    raise AssertionError(f"没有 {stage} 请求")


def test_analyze_extracts_the_pattern_first(
    home, fake, seed_image, monkeypatch
) -> None:
    ctx = _enable(home)
    labels: list[str] = []
    monkeypatch.setattr(bg, "step", lambda _ctx, label: labels.append(label))
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]] == ["S01", "S01P", "S02", "S03", "S04"]
    assert [x for x in labels if "（" in x] == [
        "S01 参考图接入（1/6）",
        "S01P 图案提取（2/6）",
        "S02 14 维拆解（3/6）",
        "S03 吸引力假设（4/6）",
        "S04 系列规则（5/6）",
    ]
    assert seedmod.step_label(ctx, "S05") == "S05 裂变策划（6/6）"

    # flatten_textile 提示词原文、4:5 高清、输入图压到 1536 照搬 Pattern DNA；模型按用户要求用 2.5-flare
    (payload,) = _extract_payloads(fake)
    assert payload["model"] == "gpt-image-2.5-flare"
    assert payload["prompt"] == seedmod.pattern_prompt()
    assert "<!--" not in payload["prompt"] and payload["prompt"].endswith(
        "with no mockup, border, labels, or surrounding scene."
    )
    params = payload["vendor_params"]
    assert (params["size"], params["quality"]) == ("1856x2304", "high")
    assert "format" not in params and len(params["images"]) == 1
    _, width, height = images.probe(base64.b64decode(params["images"][0]))
    assert max(width, height) <= 1536

    row = ctx.db.q1("SELECT * FROM seed_patterns WHERE seed_id = 'S01'")
    assert row["path"].endswith("assets/seeds/S01/pattern-1.png")
    assert (row["version"], row["model"]) == (1, "gpt-image-2.5-flare")
    seed = get_seed(ctx, "S01")
    assert reference_path(ctx, seed) == row["path"]
    assert seed_bytes(ctx, seed) != original_bytes(ctx, seed)
    call = ctx.db.q1("SELECT stage, scope_id FROM provider_calls WHERE kind = 'image'")
    assert (call["stage"], call["scope_id"]) == ("S01P", "seed:S01")

    # 拆解：两张图，图案在前（设计从它读），原图只看商品形态
    content = _user_content(fake, "P02")
    assert sum(1 for part in content if part.get("type") == "image_url") == 2
    text = "\n".join(part.get("text", "") for part in content)
    assert text.index("从参考图提取的平面图案") < text.index("原图：只用来看商品形态")
    # 系列规则写明图生图用的是提取出的图案
    p04 = "\n".join(part.get("text", "") for part in _user_content(fake, "P04"))
    assert "从参考图提取的平面图案作为输入图" in p04


def test_image_to_image_uses_the_pattern(home, fake, seed_image) -> None:
    ctx = _enable(home)
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    pattern = store.ensure_local(ctx, reference_path(ctx, get_seed(ctx, "S01")))
    expected = images.to_vision_jpeg(pattern.read_bytes(), 2048)
    i2i = [
        p
        for p in fake.image_payloads()
        if p["vendor_params"].get("images")
        and not p["prompt"].startswith(EXTRACT_PREFIX)
    ]
    assert i2i and all(
        base64.b64decode(p["vendor_params"]["images"][0]) == expected for p in i2i
    )


def test_fallback_models_and_resume_after_failure(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    for model in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
        fake.image_modes_by_model[f"{model}+images"] = "400"
    with pytest.raises(StageError, match="图案提取没成功"):
        seedmod.analyze(ctx, "S01")
    # 停在 S01P：接入做完了，还没拆解
    seed = get_seed(ctx, "S01")
    assert seed["status"] == "ingested" and seed["latest_analysis_id"] is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_patterns") == 0
    assert [p["model"] for p in _extract_payloads(fake)] == [
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
    ]
    # 第一个模型不行就换下一个；再跑从 S01P 接着做
    del fake.image_modes_by_model["gpt-image-2.5-sunburst+images"]
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]][:2] == ["S01P", "S02"]
    extracted = out["steps"][0]
    assert extracted["model"] == "gpt-image-2.5-sunburst" and extracted["version"] == 2
    assert [a["status"] for a in extracted["attempts"]] == ["failed", "ok"]


def test_restricted_reference_is_never_extracted(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image, rights="restricted")
    out = seedmod.analyze(ctx, "S01")
    assert out["stopped_at"] == "S01" and _extract_payloads(fake) == []
    with pytest.raises(StageError, match="素材受限"):
        seedmod.extract_pattern(ctx, "S01")


def test_extract_again_with_a_hint_and_card(
    home, fake, seed_image, capsys, spawned
) -> None:
    _enable_feishu(home)
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    first = ui._seed_thumb(ctx, "S01")
    assert first.endswith("seed-S01-pattern-1.jpg")
    out = run(capsys, "seed", "extract", "S01", "--hint", "保留文字")
    assert out["version"] == 2 and out["pattern_path"].endswith("pattern-2.png")
    assert _extract_payloads(fake)[-1]["prompt"].endswith(
        "Additional requirement from the user (may be written in Chinese): 保留文字"
    )
    # 缩略图跟着换成新图案，标「图案 S01」
    assert ui._seed_thumb(ctx, "S01").endswith("seed-S01-pattern-2.jpg")
    assert ui._seed_label(ctx, "S01") == "图案 S01"
    # 拆解没有重做
    assert ctx.db.scalar("SELECT COUNT(*) FROM analyses") == 1

    task = run(capsys, "seed", "extract", "S01", "--detach", "--notify")
    assert task["task_id"].startswith("BG") and spawned[-1][2:] == [
        "seed",
        "extract",
        "S01",
        "--notify",
    ]
    busy = run_error(capsys, "seed", "extract", "S01", "--detach")
    assert "正在图案提取" in busy["detail"]

    summary = bg.summarize("extract", out)
    card = notify.bg_card(ctx, {"kind": "extract", "subject": "S01"}, summary, ok=True)
    assert card["header"]["title"]["content"] == "图案已提取｜S01"
    assert "这次的要求：保留文字" in card["elements"][0]["content"]
    columns = next(e for e in card["elements"] if e["tag"] == "column_set")["columns"]
    assert [c["elements"][1]["content"] for c in columns] == [
        "原图 S01",
        "提取的图案 S01",
    ]
    assert all(c["elements"][0]["img_key"] in fake.feishu_images for c in columns)


def run_error(capsys, *argv: str) -> dict:
    from podflow import cli

    assert cli.main(list(argv)) != 0
    return json.loads(capsys.readouterr().out)


def test_analyze_card_shows_original_and_pattern(home, fake, seed_image) -> None:
    _enable_feishu(home)
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    summary = bg.summarize("analyze", {**out, "seed_id": "S01"})
    card = notify.bg_card(ctx, {"kind": "analyze", "subject": "S01"}, summary, ok=True)
    columns = next(e for e in card["elements"] if e["tag"] == "column_set")["columns"]
    assert [c["elements"][1]["content"] for c in columns] == [
        "原图 S01",
        "提取的图案 S01",
    ]


def test_disabled_keeps_the_old_flow(home, fake, seed_image, capsys) -> None:
    ctx = reopen(home)
    seedmod.add_seed(ctx, seed_image)
    out = seedmod.analyze(ctx, "S01")
    assert [s["stage"] for s in out["steps"]] == ["S01", "S02", "S03", "S04"]
    assert _extract_payloads(fake) == []
    seed = get_seed(ctx, "S01")
    assert reference_path(ctx, seed) == seed["original_path"]
    assert seedmod.step_label(ctx, "S02") == "S02 14 维拆解（2/5）"
    content = _user_content(fake, "P02")
    assert sum(1 for part in content if part.get("type") == "image_url") == 1
    err = run_error(capsys, "seed", "extract", "S01")
    assert "图案提取关着" in err["detail"]


def test_purge_removes_the_pattern(home, fake, seed_image) -> None:
    ctx = _enable(home)
    seedmod.add_seed(ctx, seed_image)
    seedmod.analyze(ctx, "S01")
    thumb = ui._seed_thumb(ctx, "S01")
    assert os.path.exists(thumb)
    out = maintenance.purge_old(ctx, seeds=["S01"], by="tester")
    assert "cache/thumbs/seed-S01-pattern-1.jpg" in out["local_files"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM seed_patterns") == 0
    assert not os.path.exists(thumb)
