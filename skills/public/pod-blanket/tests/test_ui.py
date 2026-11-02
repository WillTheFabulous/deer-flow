"""飞书工作台视图（podctl ui）与卡片通知。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import SLOTS_PER_JOB, card_buttons, reopen, set_config
from podflow import cli, doctor, images, notify, review, ui
from podflow.stages import seed as seedmod
from podflow.stages.plan import asset_label_of
from podflow.worker import Worker
from test_pipeline import bootstrap

MODEL_LABELS = ("nano-banana-2.1", "gpt-2.5-flare", "gpt-2.5-sunburst")


def _made(sep: str, t2i: str, i2i: str) -> list[str]:
    """每个模型文生图、图生图各一张，另用 -edit 模型出一张图生图。"""
    return sorted(
        [f"{m}{sep}{mode}" for m in MODEL_LABELS for mode in (t2i, i2i)]
        + [f"{m}-edit{sep}{i2i}" for m in MODEL_LABELS]
    )


def run(capsys, *argv: str) -> dict:
    assert cli.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def _enable_feishu(home: Path) -> None:
    set_config(
        home,
        "notify.feishu",
        {"enabled": True, "receive_id_type": "chat_id", "receive_id": "oc_test"},
    )
    secrets = home / "secrets.env"
    secrets.write_text(
        "LITELLM_API_KEY=test-key\nFEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\n"
        "MOOYA_SERVICE_TOKEN=test-mooya-token\n",
        encoding="utf-8",
    )
    os.chmod(secrets, 0o600)


def test_decision_buttons_match_engine_decisions() -> None:
    assert set(ui.DECISION_BUTTONS) == set(review.DECISIONS) == set(ui.TYPE_LABELS)
    for kind, buttons in ui.DECISION_BUTTONS.items():
        decisions = [d for d, _, _ in buttons]
        assert set(decisions) <= set(review.DECISIONS[kind]), kind
        # 必须附修改意见的决策不做成按钮，只给进入对话的提示
        assert "needs_changes" not in decisions
        if "needs_changes" in review.DECISIONS[kind]:
            assert "需要修改" in ui.HINTS[kind]
        assert all(style in ("primary", "danger", "default") for _, _, style in buttons)


def test_dashboard_reports_state_and_level(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    view = ui.dashboard(ctx)
    text = "\n".join(view["lines"])
    assert view["level"] == "normal" and not view["paused"]
    assert "**状态**：运行中" in text and "试运行窗口" in text
    # 3 个模型 × 文生图 / 图生图 / 图生图 Edit；授权未知不再开待审
    assert {t["type"]: t["count"] for t in view["review_types"]} == {
        "asset_selection": SLOTS_PER_JOB,
    }
    assert view["open_reviews"] == SLOTS_PER_JOB and f"选款 {SLOTS_PER_JOB}" in text

    cli.main(["pause", "--reason", "测试暂停"])
    view = ui.dashboard(reopen(ctx.home))
    assert view["paused"] and view["level"] == "paused"
    assert view["lines"][0] == "**状态**：已暂停（测试暂停）"


def test_reviews_page_paginates_with_images_and_buttons(ctx, fake, seed_image) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    page = ui.reviews_page(ctx, limit=2, types=["asset_selection"])
    assert (
        page["total"] == SLOTS_PER_JOB
        and len(page["items"]) == 2
        and page["offset"] == 0
    )
    item = page["items"][0]
    assert item["type_label"] == "选款"
    assert [a["decision"] for a in item["actions"]] == ["approved", "rejected"]
    assert item["hint"] and item["review_id"] in item["hint"]
    assert [i["label"] for i in item["images"]] == [
        "原图 S01",
        f"候选 {asset_label_of(ctx, item['subject_id'])}",
    ]
    assert all(Path(i["path"]).is_file() for i in item["images"])
    assert Path(item["images"][0]["path"]).is_absolute()
    # 选款项带一个修改表单：输入框名按审核项区分，提交的决定是 needs_changes
    rid = item["review_id"]
    assert item["form"] == {
        "name": f"fix_{rid}",
        "input": f"note_{rid}",
        "placeholder": "要改什么？例：兔子耳朵改短、背景换成雾蓝",
        "default": "",
        "max_length": review.FIX_NOTE_CHARS,
        "submit_label": "修改",
        "decision": "needs_changes",
    }

    # 翻过头（裁决掉最后一页的最后一项）退回最后一页
    page = ui.reviews_page(ctx, offset=10, limit=4, types=["asset_selection"])
    assert page["offset"] == 8 and len(page["items"]) == 1
    # 类型统计不受筛选影响
    assert {t["type"] for t in page["type_counts"]} == {"asset_selection"}


def _made_line(item: dict) -> str:
    return next(line for line in item["lines"] if line.startswith("出图："))


def test_selection_shows_original_first_with_model_and_mode(
    ctx, fake, seed_image
) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    items = ui.reviews_page(ctx, limit=10, types=["asset_selection"])["items"]
    made = sorted(_made_line(i) for i in items)
    assert made == [f"出图：{line}" for line in _made(" · ", "文生图", "图生图")]
    # 文生图、图生图都先放这个任务的原图（参考图），再放候选
    for item in items:
        assert [img["label"] for img in item["images"]] == [
            "原图 S01",
            f"候选 {asset_label_of(ctx, item['subject_id'])}",
        ]
        assert all(Path(img["path"]).is_file() for img in item["images"])
        assert item["lines"][0].startswith("方向：V0001 ")

    i2i = next(i for i in items if _made_line(i).endswith("图生图"))
    review.decide(ctx, i2i["review_id"], "approved", by="tester")
    gallery = ui.gallery(ctx)
    assert gallery["total"] == 1 and "图生图" in gallery["items"][0]["lines"][1]
    assert (
        gallery["items"][0]["lines"][1].split(" · ")[0].removesuffix("-edit")
        in MODEL_LABELS
    )


def test_selection_of_legacy_direction_and_missing_original(
    ctx, fake, seed_image
) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    # 改版前的变体没有标题和简述：标题用变化轴，简述按改动字段补
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE variants SET plan_json = ?",
            (
                json.dumps(
                    {
                        "task_type": None,
                        "changed_fields": [
                            {"field_id": "D18", "from": "坐姿", "to": "前倾嗅花"}
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        axis = conn.execute("SELECT axis FROM variants").fetchone()["axis"]
    item = ui.reviews_page(ctx, limit=1, types=["asset_selection"])["items"][0]
    assert item["lines"][:2] == [
        f"方向：V0001 {axis}（对照 · 只改表情动作）· B001",
        "· 动作姿态：坐姿 → 前倾嗅花",
    ]
    assert not any(line.startswith("不变：") for line in item["lines"])
    # 原图找不到时只放候选
    with ctx.db.tx() as conn:
        conn.execute("UPDATE seeds SET original_path = 'seeds/missing.png'")
    item = ui.reviews_page(ctx, limit=1, types=["asset_selection"])["items"][0]
    assert [img["label"] for img in item["images"]] == [
        f"候选 {asset_label_of(ctx, item['subject_id'])}"
    ]


def test_contact_sheet_puts_original_first_with_labels(
    ctx, fake, seed_image, tmp_path, monkeypatch
) -> None:
    bootstrap(ctx, seed_image, limit=1)
    Worker(ctx, max_minutes=5).run()
    # 没配中文字体：说明只写 ASCII
    sheet = review.contact_sheet(ctx, tmp_path / "plain.png", batch_id="B001")
    assert sheet["originals"] == ["S01"] and len(sheet["assets"]) == SLOTS_PER_JOB
    assert [t["kind"] for t in sheet["tiles"]] == (
        ["original"] + ["candidate"] * SLOTS_PER_JOB
    )
    assert sheet["tiles"][0] == {
        "kind": "original",
        "id": "S01",
        "label": "S01",
        "lines": ["original S01"],
    }
    candidates = sheet["tiles"][1:]
    assert all(t["lines"][0] == f"{t['id']}  V0001" for t in candidates)
    assert sorted(t["lines"][1] for t in candidates) == _made(" ", "t2i", "i2i")

    # 配了中文字体：第一格是原图，每格写方向标题、模型和出图方式
    set_config(ctx.home, "review.sheet_font", "fonts/cjk.ttf")
    monkeypatch.setattr(images, "label_font", lambda path, size=18: images._font(size))
    ctx = reopen(ctx.home)
    sheet = review.contact_sheet(ctx, tmp_path / "cjk.png", batch_id="B001")
    assert sheet["tiles"][0]["lines"] == ["原图 S01"]
    candidates = sheet["tiles"][1:]
    assert all(t["lines"][0] == f"{t['id']} · V0001 主题方案1" for t in candidates)
    assert sorted(t["lines"][1] for t in candidates) == _made(" · ", "文生图", "图生图")
    assert (tmp_path / "cjk.png").is_file()


def test_doctor_warns_when_sheet_font_is_unreadable(home, fake) -> None:
    assert not any(
        "review.sheet_font" in c["check"] for c in doctor.run(reopen(home))["checks"]
    )
    set_config(home, "review.sheet_font", "/nonexistent/cjk.ttf")
    check = next(
        c
        for c in doctor.run(reopen(home))["checks"]
        if "review.sheet_font" in c["check"]
    )
    assert check["ok"] is False and check["level"] == "warn"


def test_family_review_renders_seed_thumbnail(ctx, fake, seed_image) -> None:
    seed = seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    seedmod.analyze(ctx, seed["seed_id"])
    page = ui.reviews_page(ctx, limit=10)
    by_type = {i["type"]: i for i in page["items"]}
    assert set(by_type) == {"family"}
    family = by_type["family"]
    assert family["form"] is None
    assert any(line.startswith("定位：") for line in family["lines"])
    assert "可变字段 3 个：主题、构图类型、视觉中心" in family["lines"]
    thumb = Path(family["images"][0]["path"])
    # 参考图原图很大：缩略图只生成一次，放在 cache/thumbs/
    assert thumb.parent == ctx.home / "cache" / "thumbs" and thumb.is_file()
    assert ui._seed_thumb(ctx, seed["seed_id"]) == str(thumb)


def test_cli_ui_commands(home, fake, capsys) -> None:
    assert run(capsys, "ui", "dashboard")["title"] == "POD 毛毯工作台"
    page = run(capsys, "ui", "reviews", "--limit", "1")
    assert page["total"] == 0 and page["items"] == []
    daily = run(capsys, "ui", "daily")
    assert (
        daily["title"].startswith("POD 日报｜") and "今日没有模型调用" in daily["lines"]
    )


def test_notify_sends_card_with_workbench_buttons(home, fake, capsys) -> None:
    _enable_feishu(home)
    out = run(capsys, "notify")
    assert out["sent"] is True and len(fake.feishu_cards) == 1
    card = fake.feishu_cards[0]
    assert card["config"]["update_multi"] is True
    assert [b["value"]["action"] for b in card_buttons(card)] == [
        notify.CARD_ACTION_DASHBOARD,
        notify.CARD_ACTION_REVIEWS,
    ]
    assert "POD 毛毯工作台" in fake.feishu_messages[0]
    # 指定文本时仍发纯文本
    run(capsys, "notify", "--text", "hello")
    assert fake.feishu_messages[-1] == "hello" and len(fake.feishu_cards) == 1


def test_notify_falls_back_to_text_when_card_rejected(home, fake, capsys) -> None:
    _enable_feishu(home)
    fake.feishu_reject_cards = True
    out = run(capsys, "notify")
    assert out["sent"] is True and "card_error" in out
    assert not fake.feishu_cards and fake.feishu_messages[0].startswith(
        "POD 毛毯流水线"
    )


def test_daily_report_notify_sends_daily_card(home, fake, capsys) -> None:
    _enable_feishu(home)
    out = run(capsys, "report", "daily", "--notify")
    assert out["notified"]["sent"] is True
    assert fake.feishu_cards[0]["header"]["title"]["content"].startswith("POD 日报｜")
