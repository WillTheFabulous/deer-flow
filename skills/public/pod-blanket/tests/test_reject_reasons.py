"""没选原因：候选表的多选列「没选原因」由人填、程序只读；验收时读回来记进 selection_reasons，
给批次反馈（P11）和 `podctl table reasons` 汇总用。"""

from __future__ import annotations

import json

from podflow import candidates, cli, maintenance, reasons
from podflow.stages import feedback
from test_candidates import (  # noqa: F401
    _age_selection,
    _candidate_table,
    _open_items,
    _record,
    _setup,
    spawned,
)


def test_new_table_has_the_reason_column_next_to_the_result(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    _, table = _candidate_table(fake)
    names = [f["field_name"] for f in table["fields"]]
    assert names.index("没选原因") == names.index("选款结果") + 1
    spec = next(f for f in table["fields"] if f["field_name"] == "没选原因")
    assert spec["type"] == 4
    assert [o["name"] for o in spec["property"]["options"]] == list(reasons.REASONS)
    assert "选填" in spec["description"]["text"]


def test_old_tables_get_the_reason_column_at_the_end(
    home,
    fake,
    seed_image,
    spawned,  # noqa: F811
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    table_id, table = _candidate_table(fake)
    # 装成版本 2 的表：没有「没选原因」，列版本记着 2
    table["fields"] = [f for f in table["fields"] if f["field_name"] != "没选原因"]
    meta = candidates._fields_meta(ctx)
    meta["versions"][table_id] = 2
    candidates._save_fields_meta(ctx, meta)
    assert candidates.status(ctx)["fields_pending"] == 1
    out = candidates.sync(ctx)
    assert out["fields"] == 1
    assert table["fields"][-1]["field_name"] == "没选原因"
    assert table["fields"][-1]["type"] == 4
    assert candidates.status(ctx)["fields_pending"] == 0


def test_accept_and_refresh_record_reasons_of_rejected_rows(
    home,
    fake,
    seed_image,
    capsys,
    spawned,  # noqa: F811
) -> None:
    ctx = _setup(home, seed_image)
    candidates.sync(ctx)
    _age_selection(ctx)
    items = _open_items(ctx)
    picked, small, wrong, later = items[:4]
    rows = {
        i["subject_id"]: fake.bitable_records[_record(ctx, i["subject_id"])]
        for i in items
    }
    rows[picked["subject_id"]]["选中"] = True
    # 勾了「选中」又填了原因：不算
    rows[picked["subject_id"]]["没选原因"] = ["变化太小"]
    rows[small["subject_id"]]["没选原因"] = ["变化太小", "变化太小", "配色不好看"]
    rows[wrong["subject_id"]]["没选原因"] = [{"text": "画错变形"}]
    out = candidates.accept(ctx, "S01", by="feishu:ou_tester")
    assert out["approved"] == [picked["subject_id"]]
    assert out["reasons"] == {"saved": 2, "cleared": 0}
    ids = [i["subject_id"] for i in items]
    assert reasons.for_assets(ctx, ids) == {
        small["subject_id"]: ["变化太小", "配色不好看"],
        wrong["subject_id"]: ["画错变形"],
    }
    # 选完以后再补填、清空的：重读（或下次验收）读到；没变的不重写
    rows[later["subject_id"]]["没选原因"] = ["和别的款重复"]
    rows[wrong["subject_id"]]["没选原因"] = []
    again = candidates.refresh_reasons(ctx, "S01")
    assert again == {"read": {"S01": {"saved": 1, "cleared": 1}}, "errors": []}
    assert set(reasons.for_assets(ctx, ids)) == {
        small["subject_id"],
        later["subject_id"],
    }
    events = ctx.db.q(
        "SELECT payload_json FROM events WHERE event = 'selection_reasons_read'"
    )
    assert len(events) == 2

    # 汇总：没选了几张、几张写了原因，按原因 / 方向类型 / 字段计数
    summary = reasons.summary(ctx, "S01")
    assert summary["rejected"] == len(items) - 1 and summary["with_reason"] == 2
    assert summary["by_reason"] == {"变化太小": 1, "配色不好看": 1, "和别的款重复": 1}
    assert set(summary["by_kind"]) == {"单字段大改"}
    assert summary["by_field"] and summary["by_pair"] == {}
    assert reasons.summary(ctx)["with_reason"] == 2
    assert cli.main(["table", "reasons", "--seed", "S01"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["by_reason"] == summary["by_reason"] and "refresh" not in shown
    assert cli.main(["table", "reasons", "--refresh"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["refresh"]["read"] == {"S01": {"saved": 0, "cleared": 0}}

    # 批次反馈：每张图带上人工决定与原因，批次指标按原因计数
    batch_id = ctx.db.scalar("SELECT batch_id FROM batches")
    metrics = feedback.batch_metrics(ctx, batch_id)["human_selection"]
    assert metrics["rejected_with_reason"] == 2
    assert metrics["reject_reasons"] == summary["by_reason"]
    assets = {
        a["asset_id"]: a
        for v in feedback._variant_summaries(ctx, batch_id)
        for a in v["assets"]
    }
    assert assets[small["subject_id"]]["reject_reasons"] == ["变化太小", "配色不好看"]
    assert assets[small["subject_id"]]["human_decision"] == "rejected"
    assert assets[picked["subject_id"]]["human_decision"] == "approved"
    assert "reject_reasons" not in assets[picked["subject_id"]]

    # 清理旧数据：原因跟着图一起删，不挡外键
    maintenance.purge_old(ctx, seeds=["S01"], by="tester")
    assert ctx.db.scalar("SELECT COUNT(*) FROM selection_reasons") == 0


def test_normalize_keeps_order_and_clips() -> None:
    assert reasons.normalize(None) == [] and reasons.normalize([]) == []
    assert reasons.normalize("其他") == ["其他"]
    assert reasons.normalize(["画风不统一", {"name": "不好卖"}, " ", "画风不统一"]) == [
        "画风不统一",
        "不好卖",
    ]
    assert reasons.normalize(["很" * 40]) == ["很" * reasons.REASON_CHARS]
