"""待审积压上限调高后（线上 1000）：待审列表、CLI、xlsx 导出与工作台待审页都不能在 200 / 1000 条截断。"""

from __future__ import annotations

from conftest import reopen, set_config
from podflow import report, review, ui
from podflow.db import open_review
from test_ui import run


def _open_proposals(ctx, n: int) -> None:
    with ctx.db.tx() as conn:
        for i in range(n):
            open_review(
                conn,
                "proposal",
                f"B{i:03d}",
                "反馈提出了方向调整",
                {"proposed_family_changes": [], "next_experiments": []},
            )


def test_lists_are_not_cut_at_200(home, fake, capsys) -> None:
    ctx = reopen(home)
    _open_proposals(ctx, 250)
    assert len(review.list_reviews(ctx)) == 250
    assert run(capsys, "review", "list")["count"] == 250
    # 工作台待审页翻到最后一页也拿得到
    page = ui.reviews_page(ctx, offset=247, limit=3)
    assert page["total"] == 250 and len(page["items"]) == 3
    # 列表上限要留出积压上限（线上 1000）被在跑任务略微顶过的余量
    assert review.LIST_LIMIT > 1000


def test_backlog_limit_comes_from_config(home, fake) -> None:
    set_config(home, "batch.review_backlog_limit", 1000)
    assert report.snapshot(reopen(home))["review_backlog"]["limit"] == 1000
