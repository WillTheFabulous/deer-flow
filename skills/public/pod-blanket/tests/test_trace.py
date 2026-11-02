"""流水线留痕：trace_events / pipeline_status 跟着每一环走，单款时间线与工作台动态能看到选款之后的事。"""

from __future__ import annotations

import json

import pytest

from podflow import cli, stores, trace, ui
from test_auto_listing import _candidates, _pending_asset, _run_to_listed, _tick
from test_temu import COL3, _record, spawned  # noqa: F401 - 夹具，下面 usefixtures 用

pytestmark = pytest.mark.usefixtures("spawned")


def _pipeline(ctx, asset_id: str) -> list[tuple[str | None, str]]:
    return [
        (r["from_state"], r["to_state"])
        for r in ctx.db.q(
            "SELECT from_state, to_state FROM trace_events WHERE asset_id = ? AND stage = ? ORDER BY trace_id",
            (asset_id, trace.STAGE_PIPELINE),
        )
    ]


def test_pipeline_status_follows_each_stage(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert trace.summary(ctx, asset_id)["stage"] == "scenes_wait"
    since = trace.rows(ctx, asset_id)[0]["stage_since"]
    # 没变：不改环节开始时间、不重复记轨迹
    assert trace.refresh(ctx, [asset_id])["changed"] == 0
    assert trace.rows(ctx, asset_id)[0]["stage_since"] == since
    _run_to_listed(ctx)
    pairs = _pipeline(ctx, asset_id)
    assert pairs[0] == (None, "scenes_wait") and pairs[-1][1] == "listed"
    # 环节一条接一条：上一条去的地方是下一条来的地方
    assert all(a[1] == b[0] for a, b in zip(pairs, pairs[1:]))
    assert trace.summary(ctx, asset_id)["label"] == "已上架"


def test_removed_store_is_cancelled_once(home, fake, seed_image) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    assert stores.remove(
        ctx, asset_id, ["s3"], by="tester", source="chat", allow_queued=True
    ) == ["s3"]
    trace.refresh(ctx, [asset_id])
    assert trace.summary(ctx, asset_id)["stage"] == "cancelled"
    count = len(_pipeline(ctx, asset_id))
    trace.refresh(ctx, [asset_id])
    assert len(_pipeline(ctx, asset_id)) == count
    removed = ctx.db.q1(
        "SELECT actor, source, to_state FROM trace_events WHERE asset_id = ? AND stage = 'store' ORDER BY trace_id DESC",
        (asset_id,),
    )
    assert (removed["actor"], removed["source"], removed["to_state"]) == (
        "tester",
        "chat",
        "cancelled",
    )


def test_aggregate_puts_attention_first() -> None:
    items = [
        {"store_key": "s3", "stage": "listed", "stage_since": "2026-10-30T01:00:00Z"},
        {
            "store_key": "s4",
            "stage": "failed",
            "stage_since": "2026-10-30T02:00:00Z",
            "blocker": "标题不合规",
        },
    ]
    assert trace.aggregate(items)["stage"] == "failed"
    items[1]["stage"] = "box"
    # 都不用人看：取最落后的一家
    assert trace.aggregate(items)["stage"] == "box"
    assert trace.aggregate([]) is None


def test_asset_timeline_has_every_step(home, fake, seed_image, capsys) -> None:
    ctx = _candidates(home, fake, seed_image)
    asset_id = _pending_asset(ctx)
    _tick(ctx, fake, _record(ctx, asset_id), COL3)
    _run_to_listed(ctx)
    got = trace.timeline(ctx, asset_id)
    sources = {e["source"] for e in got["timeline"]}
    assert {
        "trace",
        "event",
        "review",
        "call",
        "scenes",
        "listing",
        "title",
        "listed",
    } <= sources
    stamps = [e["ts"] for e in got["timeline"]]
    assert stamps == sorted(stamps)
    whats = [e["what"] for e in got["timeline"] if e["source"] == "listing"]
    for what in (
        "选了店铺",
        "领到货号",
        "确认上架",
        "进了采集箱",
        "提交发布",
        "已上架（妙手 published）",
    ):
        assert what in whats, what
    assert got["summary"]["stage"] == "listed"
    # podctl asset --timeline
    capsys.readouterr()
    assert cli.main(["asset", asset_id, "--timeline", "--limit", "5"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["count"] == got["count"] and len(printed["timeline"]) == 5
    # 工作台最近动态：选款之后的事也有一句话
    texts = [item["text"] for item in ui.timeline(ctx, limit=50)["items"]]
    assert any("定了店铺" in t for t in texts)
    assert any("确认上架（全自动）" in t for t in texts)
    assert any("已上架" in t for t in texts)
