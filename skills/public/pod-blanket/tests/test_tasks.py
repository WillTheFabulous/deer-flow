"""任务视图：一张参考图一个任务，五步进度、现在在做什么、按状态给的按钮；已完成的可以接着裂变。"""

from __future__ import annotations

import os
from collections import Counter
from types import SimpleNamespace

import pytest

from conftest import make_png, reopen
from podflow import bg, cli, directions, review, tasks, ui
from podflow.stages import plan as planmod
from podflow.stages import seed as seedmod
from podflow.util import dumps
from podflow.worker import Worker
from test_qc_rules import one_model
from test_ui import run


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


def _only(ctx) -> dict:
    items = tasks.summaries(ctx)
    assert len(items) == 1
    return items[0]


def _states(task: dict) -> list[tuple[str, str]]:
    return [(s["state"], s["text"]) for s in task["steps"]]


def _labels(actions: list[dict]) -> list[str]:
    return [a["label"] for a in actions]


def _analyzed(ctx, image) -> str:
    seed = seedmod.add_seed(ctx, image, source_ref="测试参考图")
    return seedmod.analyze(ctx, seed["seed_id"])["steps"][-1]["family_id"]


# ---------------------------------------------------------------- 还没有系列的参考图


def test_reference_image_before_its_family(ctx, fake, seed_image, spawned) -> None:
    seedmod.add_seed(ctx, seed_image, source_ref="测试参考图")
    task = _only(ctx)
    assert task["status"] == "waiting_analyze" and task["family_id"] is None
    assert _states(task)[0] == ("todo", "拆解")
    assert task["now"] == ["还没开始拆解：进入 POD 对话说「拆解 S01」"]

    bg.spawn(ctx, ["seed", "analyze", "S01"], kind="analyze", subject="S01")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET step = 'S02 14 维拆解（2/5）'")
    task = _only(ctx)
    assert task["status"] == "analyzing" and task["group"] == 1
    assert _states(task)[0] == ("current", "拆解中")
    assert task["now"] == ["母版拆解中：S02 14 维拆解（2/5）（已用 0 分钟）"]

    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE bg_tasks SET status = 'failed', result_json = ?",
            (dumps({"error": "llm_error", "detail": "模型超时"}),),
        )
    task = _only(ctx)
    assert task["status"] == "analyze_failed" and task["error"] == "模型超时"
    assert task["now"] == ["拆解失败：模型超时；进入 POD 对话说「重新拆解 S01」"]
    item = ui.tasks_view(ctx)["items"][0]
    assert item["title"] == "S01　拆解失败" and item["form"] is None
    # 拆解失败的任务只能手动归档：没有在做的事，给「归档」按钮
    assert _labels(item["actions"]) == ["进入 POD 对话", "归档"]

    seedmod.set_rights(ctx, "S01", "restricted", by="tester")
    task = _only(ctx)
    assert task["status"] == "restricted"
    assert _states(task)[0] == ("failed", "授权受限")


# ---------------------------------------------------------------- 一个任务从策划走到出完


def test_task_walks_through_the_steps(ctx, fake, seed_image, spawned) -> None:
    one_model(ctx.home, n=2)
    ctx = reopen(ctx.home)
    family_id = _analyzed(ctx, seed_image)
    # 拆完还没策划（自动策划没成功时就是这样）：待策划，可以直接策划
    task = _only(ctx)
    assert task["status"] == "idle" and task["can_plan"] is True
    assert _states(task) == [
        ("done", "拆解"),
        ("todo", "策划"),
        ("todo", "挑选"),
        ("todo", "出图"),
        ("todo", "选款"),
    ]
    assert task["now"] == ["还没有裂变方向"]
    assert ui.tasks_view(ctx)["items"][0]["form"]["submit"]["label"] == "策划 3 个方向"

    bg.spawn(ctx, ["plan", "--family", family_id], kind="plan", subject=family_id)
    task = _only(ctx)
    assert task["status"] == "planning" and task["can_plan"] is None
    assert _states(task)[1] == ("current", "策划中")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE bg_tasks SET status = 'done'")

    plan = planmod.plan_batch(ctx, family_id, limit=2)
    task = _only(ctx)
    assert task["status"] == "picking" and task["pending_batch"] == plan["batch_id"]
    assert _states(task)[1:3] == [("done", "策划"), ("current", "挑选 2 个方向")]
    assert task["now"] == ["B001 的 2 个方向等你挑选"]
    assert ui.tasks_view(ctx)["items"][0]["actions"][0] == {
        "action": "pod_plan_view",
        "label": "去挑选 B001",
        "style": "primary",
        "batch": "B001",
    }

    directions.confirm(ctx, plan["batch_id"], by="tester", pick=["1", "2"])
    task = _only(ctx)
    # 人确认过的方向由巡检自己接着出：算进行中，也可以马上点「开始出图」
    assert task["status"] == "queued" and task["group"] == 1
    assert task["worker_needed"] is True
    assert _states(task)[2:4] == [("done", "挑选"), ("current", "出图 0/2")]
    assert task["now"] == ["B001 有 2 个方向排队"]
    assert _labels(ui.tasks_view(ctx)["items"][0]["actions"]) == ["开始出图", "看明细"]
    # 改版前的批次（没有挑选记录）不会自己接着出：等你点「开始出图」
    saved = ctx.db.scalar("SELECT plan_json FROM batches")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE batches SET plan_json = '{}'")
    task = _only(ctx)
    assert task["status"] == "start_needed" and task["group"] == 0
    assert task["now"] == ["B001 有 2 个方向排队（要点「开始出图」）"]
    # 开着连续运行时巡检会拉起出图，不用人点
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "trial_started_at", "2000-01-01T00:00:00+00:00")
        ctx.db.set_meta(conn, "trial_ends_at", "2999-01-01T00:00:00+00:00")
    assert _only(ctx)["status"] == "queued"
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "trial_ends_at", "2000-01-02T00:00:00+00:00")
        conn.execute("UPDATE batches SET plan_json = ?", (saved,))

    Worker(ctx, max_minutes=5).run()
    task = _only(ctx)
    # 选中的方向都出完就算完成，选款还没审也算
    assert task["status"] == "done" and task["finished_at"]
    assert _states(task)[3:] == [("done", "出图 2/2"), ("current", "选款 待选 4")]
    assert task["counts"] == {
        "batches": 1,
        "directions": 2,
        "jobs": 2,
        "jobs_done": 2,
        "images": 4,
        "pending_selection": 4,
        "approved": 0,
        # 套图关着（测试默认）：都是 0，进度条也只有五步
        "scenes_done": 0,
        "scenes_active": 0,
        "scenes_parked": 0,
        "scenes_missing": 0,
        # 上架状态关着（测试默认）：都是 0，没有「上架」这一步
        "listing_listed": 0,
        "listing_skipped": 0,
        "listing_pending": 0,
        # 交付包关着：都是 0，没有「下载」这一步；方向都出完了
        "packages_ready": 0,
        "packages_downloaded": 0,
        "jobs_unfinished": 0,
        "images_unfinished": 0,
    }
    assert task["stage"] is None and task["status_label"] == "已完成"
    assert len(task["steps"]) == 5
    assert task["now"] == ["选中的 2 个方向都出完了，共 4 张"]
    item = ui.tasks_view(ctx)["items"][0]
    assert item["title"].startswith(f"S01 · {family_id} v1　已完成（")
    assert item["title"].endswith(" 出完）")
    assert item["lines"][2] == "1 批 · 2 个方向 · 出图 4 张"
    assert item["form"]["submit"] == {
        "action": "pod_plan",
        "label": "继续裂变（再策划 3 个方向）",
        "style": "primary",
        "family_id": family_id,
        "back": "tasks",
    }
    assert item["form"]["input"]["name"] == f"hint_{family_id}"
    assert _labels(item["actions"]) == ["去选款（4）", "看明细"]

    rid = review.list_reviews(ctx, types=["asset_selection"])[0]["review_id"]
    review.decide(ctx, rid, "approved", by="tester")
    assert _states(_only(ctx))[4] == ("current", "选款 待选 3 · 选中 1")

    detail = tasks.family_detail(ctx, family_id)
    assert detail["task"]["status"] == "done"
    batch = detail["batches"][0]
    assert (batch["jobs"], batch["jobs_done"]) == (2, 2)
    assert sorted(d["progress_text"] for d in batch["directions"]) == [
        "已出完 2 张 · 待选 1 · 选中 1",
        "已出完 2 张 · 待选 2",
    ]
    history = ui.history_view(ctx, family_id)
    assert (
        history["lines"][1]
        == "拆解 ✓ › 策划 ✓ › 挑选 ✓ › 出图 2/2 ✓ › **选款 待选 3 · 选中 1**"
    )
    assert _labels(history["actions"]) == ["去选款（3）"]


# ---------------------------------------------------------------- 单个方向细到哪一步


def _job(
    status: str,
    *,
    render: bool = True,
    slots: dict | None = None,
    assets: dict | None = None,
    images: int = 0,
    pending: int = 0,
    approved: int = 0,
    **extra,
) -> dict:
    return {
        "status": status,
        "has_render": int(render),
        "slots": Counter(slots or {}),
        "assets": Counter(assets or {}),
        "images": images,
        "pending": pending,
        "approved": approved,
        "blocked_reason": None,
        "resume_condition": None,
        "last_error": None,
        **extra,
    }


@pytest.mark.parametrize(
    ("job", "text"),
    [
        (_job("ready", render=False, slots={"pending": 6}), "待出图"),
        (_job("running", render=False, slots={"pending": 6}), "写生成指令中"),
        (_job("running", slots={"generated": 4, "pending": 2}), "出图中 4/6"),
        (_job("ready", slots={"generated": 3, "pending": 3}), "排队（停在出图 3/6）"),
        (
            _job(
                "running",
                slots={"generated": 6},
                assets={"generated": 2, "archived": 4},
                images=6,
                pending=4,
            ),
            "质检中 · 待选 4",
        ),
        (
            _job("running", slots={"generated": 6}, assets={"repair_pending": 1}),
            "修复中",
        ),
        (
            _job(
                "blocked",
                slots={"pending": 6},
                resume_condition="待人工审核积压降到 50 以下",
            ),
            "暂停：待人工审核积压降到 50 以下",
        ),
        (
            _job("complete", slots={"generated": 6}, images=6, pending=5, approved=1),
            "已出完 6 张 · 待选 5 · 选中 1",
        ),
        (_job("complete", slots={"failed": 6}), "没出成"),
        (_job("failed", last_error="所有槽位均未出图"), "没出成：所有槽位均未出图"),
    ],
)
def test_direction_progress_text(job: dict, text: str) -> None:
    assert tasks.job_text(job) == text


# ---------------------------------------------------------------- 耗尽、暂停、卡住


def test_exhausted_paused_and_blocked_tasks(ctx, fake, seed_image) -> None:
    family_id = _analyzed(ctx, seed_image)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE families SET status = 'exhausted', exhaustion_reason = '连续两轮没有新方向'"
        )
    task = _only(ctx)
    assert task["status"] == "exhausted" and task["can_plan"] is False
    assert task["now"] == ["系列已耗尽：连续两轮没有新方向"]
    item = ui.tasks_view(ctx)["items"][0]
    assert item["form"] is None and "已耗尽" in item["note"]
    # 耗尽的任务只能手动归档：给「归档」按钮
    assert _labels(item["actions"]) == ["看明细", "进入 POD 对话", "归档"]

    with ctx.db.tx() as conn:
        conn.execute("UPDATE families SET status = 'pending', exhaustion_reason = NULL")
    plan = planmod.plan_batch(ctx, family_id, limit=1)
    directions.confirm(ctx, plan["batch_id"], by="tester", pick=["1"])
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "1")
    task = _only(ctx)
    assert task["status"] == "paused" and task["worker_needed"] is False
    view = ui.tasks_view(ctx)
    assert "**流水线已暂停**：恢复后才会接着出图" in view["lines"]
    assert view["actions"] == [
        {"action": "pod_resume", "label": "恢复", "style": "primary"}
    ]

    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "0")
        conn.execute(
            "UPDATE jobs SET status = 'blocked', blocked_reason = 'review_backlog',"
            " resume_condition = '待人工审核积压降到 50 以下'"
        )
    task = _only(ctx)
    assert task["status"] == "blocked" and task["group"] == 0
    assert task["now"] == ["1 个方向暂停：待人工审核积压降到 50 以下"]


# ---------------------------------------------------------------- 列表排序与翻页


def test_tasks_view_orders_and_pages(ctx, fake, tmp_path) -> None:
    empty = ui.tasks_view(ctx)
    assert empty["items"] == [] and empty["total"] == 0
    assert _labels(empty["actions"]) == ["进入 POD 对话"]

    paths = []
    for i in range(3):
        path = tmp_path / f"seed{i}.png"
        path.write_bytes(make_png(100 + i, size=(400, 500)))
        paths.append(path)
    # S01：策划好了等挑选；S02：拆完还没策划；S03：刚上传
    planmod.plan_batch(ctx, _analyzed(ctx, paths[0]), limit=1)
    _analyzed(ctx, paths[1])
    seedmod.add_seed(ctx, paths[2])
    assert [t["seed_id"] for t in tasks.summaries(ctx)] == ["S03", "S01", "S02"]

    view = ui.tasks_view(ctx, limit=2)
    assert (
        view["lines"][0] == "共 3 张参考图：待你处理 2 · 进行中 0 · 已完成 0 · 其他 1"
    )
    assert [i["seed_id"] for i in view["items"]] == ["S03", "S01"]
    assert all(i["images"] for i in view["items"])
    assert view["nav"] == [
        {"action": "pod_tasks", "label": "下一页", "style": "default", "offset": 2}
    ]
    page2 = ui.tasks_view(ctx, offset=2, limit=2)
    assert [i["seed_id"] for i in page2["items"]] == ["S02"]
    assert page2["nav"] == [
        {"action": "pod_tasks", "label": "上一页", "style": "default", "offset": 0}
    ]
    # 翻过头退回最后一页
    assert ui.tasks_view(ctx, offset=9, limit=2)["offset"] == 2


def test_cli_tasks(ctx, fake, seed_image, capsys) -> None:
    family_id = _analyzed(ctx, seed_image)
    assert [t["family_id"] for t in run(capsys, "tasks")["tasks"]] == [family_id]
    detail = run(capsys, "tasks", "--family", family_id)
    assert detail["task"]["status"] == "idle" and detail["family_id"] == family_id
    assert run(capsys, "ui", "tasks")["title"] == "POD 任务列表"
    assert cli.main(["tasks", "--family", "F99"]) == 2
    assert "没有系列 F99" in capsys.readouterr().out
