"""任务视图：一张参考图（系列）就是一个任务，汇总它走到了哪一步。

五步：拆解（S01–S04）→ 策划（S05）→ 挑选方向 → 出图（按方向计，S06–S10）→ 选款；开着套图时再加一步「套图」
（选中的款出完场景图几款），并带上各批次的套图交付包（packages.by_seed，新批次在前）；开着交付包时、有选中的款时
再加一步「下载」（各批次最新的交付包标了已下载几个）；开着上架状态、有选中的款时再加一步「上架」
（选中的款在「选中的款」表里标了已上架 / 不上了几款，listing.py）。
选中的方向全部出完图就算「已完成」（status = done）；之后选中的款套图都出完是「套图完成」，交付包都标了已下载是
「套图已下载」（stage，只改 status_label，status 照旧是 done）。选款、套图、下载和上架只显示进度，不影响完成；
已完成的任务可以接着策划新一轮。
每个任务带归档信息（shelf.py）：summaries 默认只列没归档的，view="archived" 是历史任务（按归档时间从新到旧），
query 按编号、名字、参考图描述搜。
数据用几条聚合查询一次查齐，不按方向逐条查库；飞书任务列表 / 任务明细卡与 POD 对话（podctl tasks）共用。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import bg, directions, listing, packages, report, scenes, shelf, state
from .context import Ctx
from .stages.common import StageError
from .stages.job import step_for
from .stages.plan import direction_summary
from .util import loads

STEP_LABELS = {
    "analyze": "拆解",
    "plan": "策划",
    "pick": "挑选",
    "generate": "出图",
    "select": "选款",
    "scenes": "套图",
    "download": "下载",
    "listing": "上架",
}
VIEWS = ("active", "archived", "all")
# 已完成之后的两档（stage）：选中的款套图都出完 / 交付包也都标了已下载；任务上显示的状态换成这两个
STAGE_LABELS = {"scenes_done": "套图完成", "downloaded": "套图已下载"}

# 任务状态 → (给人看的标签, 分组)；列表按分组排：0 待你处理 → 1 进行中 → 2 已完成 → 3 其他
STATUSES: dict[str, tuple[str, int]] = {
    "intake": ("参考图待确认", 0),
    "waiting_analyze": ("待拆解", 0),
    "analyze_failed": ("拆解失败", 0),
    "picking": ("待你挑选", 0),
    "start_needed": ("等你开始出图", 0),
    "blocked": ("暂停中", 0),
    "analyzing": ("拆解中", 1),
    "planning": ("策划中", 1),
    "paused": ("已暂停", 1),
    "generating": ("出图中", 1),
    "queued": ("等出图", 1),
    "done": ("已完成", 2),
    "idle": ("待策划", 3),
    "exhausted": ("已耗尽", 3),
    "family_rejected": ("系列已驳回", 3),
    "restricted": ("授权受限", 3),
}
GROUP_LABELS = ("待你处理", "进行中", "已完成", "其他")
# 这些状态下要看「能不能再策划」：能就给继续裂变的输入框，不能就写原因
PLAN_STATUSES = frozenset({"done", "idle", "exhausted", "family_rejected"})

JOB_DONE = ("complete", "failed")
# 没出完的方向停在哪一步（出图另带张数）
_STEP_PHRASES = {
    "S06": "写生成指令",
    "S07": "质检",
    "T01R": "修复",
    "S09": "预检",
    "S10": "入库",
}
# 拆解停在半路时已经做完的那一步
_SEED_DONE = {"ingested": "S01", "analyzed": "S02", "hypothesized": "S03"}
_TITLE_CHARS = 12
_ERROR_CHARS = 60
_RUNNING_SHOWN = 2


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _num(ident: str | None) -> int:
    digits = "".join(ch for ch in str(ident or "") if ch.isdigit())
    return int(digits) if digits else 0


# ---------------------------------------------------------------- 数据


def _snapshot(ctx: Ctx) -> dict[str, Any]:
    db = ctx.db
    running = bg.running(ctx)
    worker = state.worker_status(db)
    jobs: dict[str, dict[str, Any]] = {}
    for row in db.q(
        "SELECT j.job_id, j.variant_id, j.batch_id, j.status, j.blocked_reason, j.resume_condition,"
        " j.last_error, j.updated_at, CASE WHEN j.render_spec_json IS NULL THEN 0 ELSE 1 END AS has_render,"
        " b.family_id FROM jobs j JOIN batches b ON b.batch_id = j.batch_id"
    ):
        job = dict(row)
        job.update(slots=Counter(), assets=Counter(), images=0, approved=0, pending=0)
        jobs[job["job_id"]] = job
    for row in db.q(
        "SELECT job_id, status, COUNT(*) FROM job_slots GROUP BY job_id, status"
    ):
        if row[0] in jobs:
            jobs[row[0]]["slots"][row[1]] += row[2]
    for row in db.q(
        "SELECT job_id, status, human_decision, COUNT(*) FROM assets"
        " GROUP BY job_id, status, human_decision"
    ):
        job = jobs.get(row[0])
        if job is None:
            continue
        job["assets"][row[1]] += row[3]
        if row[1] == "superseded":
            continue
        if row[1] not in ("failed", "rejected"):
            job["images"] += row[3]
        if row[2] == "approved":
            job["approved"] += row[3]
    for row in db.q(
        "SELECT a.job_id, COUNT(*) FROM reviews r JOIN assets a ON a.asset_id = r.subject_id"
        " WHERE r.subject_type = 'asset_selection' AND r.status = 'open' GROUP BY a.job_id"
    ):
        if row[0] in jobs:
            jobs[row[0]]["pending"] = row[1]
    # 每个参考图 / 系列最近一次结束的拆解、策划、修订：失败时任务上写原因
    last_bg: dict[tuple[str, str], dict[str, Any]] = {}
    for row in db.q(
        "SELECT kind, subject, status, result_json, started_at FROM bg_tasks"
        " WHERE kind IN ('analyze', 'plan', 'revise') AND status IN ('done', 'failed')"
        " ORDER BY started_at, task_id"
    ):
        last_bg[(row["kind"], row["subject"])] = dict(row)
    variants: Counter[str] = Counter()
    for row in db.q("SELECT family_id, COUNT(*) FROM variants GROUP BY family_id"):
        variants[row[0]] = row[1]
    return {
        "seeds": [
            dict(r)
            for r in db.q(
                "SELECT seed_id, status, rights_status, created_at FROM seeds"
            )
        ],
        # 任务名字（S01N）：参考图 → 「森林三伙伴水彩 1020」；还没起名的不在里面。描述给历史任务搜索用
        "names": {
            r["seed_id"]: r["name"]
            for r in db.q("SELECT seed_id, name FROM seed_names")
        },
        "descriptions": {
            r["seed_id"]: r["description"]
            for r in db.q("SELECT seed_id, description FROM seed_names")
        },
        "families": [
            dict(r)
            for r in db.q(
                "SELECT f.family_id, f.family_version, f.seed_id, f.status, f.exhaustion_reason FROM families f"
                " WHERE f.family_version = (SELECT MAX(f2.family_version) FROM families f2"
                " WHERE f2.family_id = f.family_id)"
            )
        ],
        "batches": [
            dict(r)
            for r in db.q(
                "SELECT batch_id, family_id, family_version, status, size, plan_json FROM batches"
                " ORDER BY batch_id"
            )
        ],
        "variants": variants,
        "jobs": jobs,
        "running": running,
        "worker_alive": bool(worker and worker.get("alive"))
        or any(t["kind"] in ("worker", "fix") for t in running),
        "trial_active": report.trial_window(ctx)["active"],
        "paused": db.get_meta("paused") == "1",
        "last_bg": last_bg,
        # 套图进度（参考图 → 选中几款、出完 / 在出 / 还差几款）；没开套图时为 None，任务只有五步
        "scenes": scenes.seed_counts(ctx) if scenes.enabled(ctx) else None,
        # 每个参考图最近一套套图结束的时间（「套图完成（10-26 15:30）」用）
        "scenes_at": {
            r[0]: r[1]
            for r in db.q(
                "SELECT seed_id, MAX(finished_at) FROM scene_sets"
                " WHERE finished_at IS NOT NULL GROUP BY seed_id"
            )
        }
        if scenes.enabled(ctx)
        else {},
        # 各批次的交付包（参考图 → [批次…]，新批次在前）；没开交付包时为空、没有「下载」这一步
        "packages": packages.by_seed(ctx),
        "packages_on": packages.enabled(ctx),
        # 归档记录（参考图 → seed_shelf 一行）；关着归档时为空，任务一律照常列
        "shelf": shelf.rows(ctx),
        # 上架状态（参考图 → 选中几款、已上架 / 不上了 / 待上架几款）；没开上架状态时为 None，没有「上架」这一步
        "listing": listing.seed_counts(ctx) if listing.enabled(ctx) else None,
    }


_NO_SCENES = {
    "approved": 0,
    "done": 0,
    "active": 0,
    "parked": 0,
    "failed": 0,
    "missing": 0,
}


def _scene_step(counts: dict[str, int]) -> dict[str, Any]:
    """第六步「套图 出完/选中」：有在出的是 current，选中的都出完是 done，有没出成的是 failed。"""
    if not counts["approved"]:
        return _step("scenes", "todo")
    text = f"套图 {counts['done']}/{counts['approved']}"
    if counts["active"]:
        return _step("scenes", "current", text)
    if counts["done"] == counts["approved"]:
        return _step("scenes", "done", text)
    if counts["failed"]:
        return _step("scenes", "failed", text)
    return _step("scenes", "todo", text)


def _download_step(downloads: dict[str, Any], scenes_done: bool) -> dict[str, Any]:
    """「下载 标了/批次」：每个批次最新的交付包都标了已下载是 done；套图都出完、还有没标的是 current。"""
    text = f"下载 {downloads['downloaded']}/{downloads['batches']}"
    if downloads["batches"] and not downloads["pending"]:
        return _step("download", "done", text)
    return _step("download", "current" if scenes_done else "todo", text)


_NO_LISTING = {"approved": 0, "listed": 0, "skipped": 0, "pending": 0}


def _listing_step(counts: dict[str, int], scenes_done: bool) -> dict[str, Any]:
    """第七步「上架 已上架/选中（不上 n）」：都标了（已上架或不上了）是 done；套图都出完、还有没上架的是 current。"""
    text = f"上架 {counts['listed']}/{counts['approved']}"
    if counts["skipped"]:
        text += f"（不上 {counts['skipped']}）"
    if counts["listed"] + counts["skipped"] >= counts["approved"]:
        return _step("listing", "done", text)
    return _step("listing", "current" if scenes_done else "todo", text)


def _scene_now(counts: dict[str, int]) -> list[str]:
    parts = []
    if counts["active"]:
        text = f"套图：{counts['active']} 款在 mooya 出图"
        if counts["parked"]:
            text += f"（其中 {counts['parked']} 款等人工：去 mooya 图案管线处理白底图）"
        parts.append(text)
    if counts["failed"] and not counts["active"]:
        parts.append(f"套图：{counts['failed']} 款没出成，点「出套图」重出")
    return parts


def _running(
    snap: dict[str, Any], kinds: tuple[str, ...], subject: str
) -> dict[str, Any] | None:
    return next(
        (t for t in snap["running"] if t["kind"] in kinds and t["subject"] == subject),
        None,
    )


def _bg_text(task: dict[str, Any]) -> str:
    label = bg.KIND_LABELS.get(task["kind"], task["kind"])
    step = f"：{task['step']}" if task.get("step") else ""
    return f"{label}中{step}（已用 {task['elapsed_minutes']} 分钟）"


def _bg_error(row: dict[str, Any] | None) -> str:
    """后台任务结束时留下的失败原因（拆解后自动策划没成功的记在 plan_error）。"""
    if not row:
        return ""
    result = loads(row["result_json"], {}) or {}
    if row["status"] == "failed":
        return _clip(result.get("detail") or result.get("error"), _ERROR_CHARS)
    return _clip(result.get("plan_error"), _ERROR_CHARS)


def _titles(ctx: Ctx, variant_ids: list[str]) -> dict[str, str]:
    if not variant_ids:
        return {}
    marks = ",".join("?" for _ in variant_ids)
    out = {}
    for row in ctx.db.q(
        f"SELECT variant_id, axis, plan_json FROM variants WHERE variant_id IN ({marks})",
        tuple(variant_ids),
    ):
        title = direction_summary(loads(row["plan_json"], {}) or {}, row["axis"])[
            "title"
        ]
        out[row["variant_id"]] = _clip(title or row["variant_id"], _TITLE_CHARS)
    return out


# ---------------------------------------------------------------- 单个方向


def _phrase(step: str | None, slots: Counter[str]) -> str:
    if step == "T01":
        return f"出图 {slots.get('generated', 0)}/{sum(slots.values())}"
    return _STEP_PHRASES.get(step or "", "收尾")


def job_text(job: dict[str, Any]) -> str:
    """一个方向细到哪一步：出图中 4/9、质检中、排队（停在质检）、已出完 9 张 · 待选 8 · 选中 1……"""
    status = job["status"]
    if status == "failed":
        error = _clip(job["last_error"], _ERROR_CHARS // 2)
        return "没出成" + (f"：{error}" if error else "")
    if status == "complete":
        if not job["images"]:
            return "没出成"
        text = f"已出完 {job['images']} 张"
    else:
        slots = job["slots"]
        step = step_for(bool(job["has_render"]), slots.get("pending", 0), job["assets"])
        if status == "running":
            text = (
                f"出图中 {slots.get('generated', 0)}/{sum(slots.values())}"
                if step == "T01"
                else f"{_phrase(step, slots)}中"
            )
        elif status == "blocked":
            text = "暂停：" + _clip(
                job["resume_condition"] or job["blocked_reason"], _ERROR_CHARS // 2
            )
        elif status == "waiting_external":
            text = "等外部结果"
        elif step == "S06":
            text = "待出图"
        else:
            text = f"排队（停在{_phrase(step, slots)}）"
    if job["pending"]:
        text += f" · 待选 {job['pending']}"
    if job["approved"]:
        text += f" · 选中 {job['approved']}"
    return text


# ---------------------------------------------------------------- 单个任务


def _step(key: str, state_: str, text: str | None = None) -> dict[str, Any]:
    """state：done 做完 / current 正在做或等你处理 / todo 还没到 / failed 停下了。"""
    return {
        "key": key,
        "label": STEP_LABELS[key],
        "state": state_,
        "text": text or STEP_LABELS[key],
    }


def _task(
    status: str,
    seed_id: str,
    fam: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    now: list[str],
    **extra: Any,
) -> dict[str, Any]:
    label, group = STATUSES[status]
    return {
        "key": fam["family_id"] if fam else seed_id,
        "seed_id": seed_id,
        "name": None,
        "family_id": fam["family_id"] if fam else None,
        "family_version": fam["family_version"] if fam else None,
        "family_status": fam["status"] if fam else None,
        "status": status,
        "status_label": label,
        "group": group,
        "steps": steps,
        "now": now,
        "counts": None,
        "pending_batch": None,
        "worker_needed": False,
        "finished_at": None,
        "can_plan": None,
        "plan_reason": None,
        "error": None,
        "packages": [],
        # 已完成之后到了哪一档（scenes_done / downloaded）、到这一档的时间
        "stage": None,
        "stage_at": None,
        "downloads": None,
        **extra,
    }


def _seed_task(snap: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
    """还没有系列的参考图：只看拆解这一步。"""
    seed_id = seed["seed_id"]
    analyzing = _running(snap, ("analyze",), seed_id)
    last = snap["last_bg"].get(("analyze", seed_id))
    error = None
    if seed["rights_status"] == "restricted" or seed["status"] == "blocked":
        status, first = "restricted", _step("analyze", "failed", "授权受限")
        now = ["授权受限：不拆解、不出图"]
    elif seed["status"] == "needs_review":
        status, first = "intake", _step("analyze", "current", "拆解：参考图待确认")
        now = ["参考图看不清或有疑问：在待审里确认要不要接入"]
    elif analyzing:
        status, first = "analyzing", _step("analyze", "current", "拆解中")
        now = [_bg_text(analyzing)]
    elif last and last["status"] == "failed":
        status, first = "analyze_failed", _step("analyze", "failed", "拆解失败")
        error = _bg_error(last)
        now = [
            "拆解失败"
            + (f"：{error}" if error else "")
            + f"；进入 POD 对话说「重新拆解 {seed_id}」"
        ]
    else:
        status, first = "waiting_analyze", _step("analyze", "todo")
        done = _SEED_DONE.get(seed["status"])
        now = [
            (f"拆解做到 {done}，还没做完" if done else "还没开始拆解")
            + f"：进入 POD 对话说「拆解 {seed_id}」"
        ]
    steps = [first, *(_step(k, "todo") for k in ("plan", "pick", "generate", "select"))]
    if snap["scenes"] is not None:
        steps.append(_step("scenes", "todo"))
    return _task(
        status, seed_id, None, steps, now, error=error, name=snap["names"].get(seed_id)
    )


def _manual_batches(
    snap: dict[str, Any],
    batches: list[dict[str, Any]],
    unfinished: list[dict[str, Any]],
) -> set[str]:
    """有方向排队、却要人点「开始出图」才会出的批次。

    人确认过方向的批次由巡检自己接着出；改版前的批次（没有挑选记录）只在连续运行开着时才会被巡检拉起。
    """
    if snap["worker_alive"] or snap["trial_active"]:
        return set()
    chosen = {
        b["batch_id"]
        for b in batches
        if (loads(b["plan_json"], {}) or {}).get("selection")
    }
    return {
        j["batch_id"]
        for j in unfinished
        if j["status"] == "ready" and j["batch_id"] not in chosen
    }


def _unfinished_text(
    ctx: Ctx, unfinished: list[dict[str, Any]], manual: set[str]
) -> list[str]:
    """出图中的任务：正在跑的方向细到哪一步，暂停的写原因，其余按批次说有几个在排队。"""
    parts = []
    shown = [j for j in unfinished if j["status"] == "running"][:_RUNNING_SHOWN]
    titles = _titles(ctx, [j["variant_id"] for j in shown])
    for job in shown:
        parts.append(
            f"{job['batch_id']}「{titles.get(job['variant_id'], job['variant_id'])}」{job_text(job)}"
        )
    blocked = [j for j in unfinished if j["status"] == "blocked"]
    if blocked:
        reason = blocked[0]["resume_condition"] or blocked[0]["blocked_reason"]
        parts.append(f"{len(blocked)} 个方向暂停：{_clip(reason, _ERROR_CHARS)}")
    waiting = Counter(
        j["batch_id"] for j in unfinished if j["status"] not in ("running", "blocked")
    )
    for batch_id in sorted(waiting, key=_num):
        text = f"{batch_id} 有 {waiting[batch_id]} 个方向排队"
        if batch_id in manual:
            text += "（要点「开始出图」）"
        parts.append(text)
    return parts


def _family_task(ctx: Ctx, snap: dict[str, Any], fam: dict[str, Any]) -> dict[str, Any]:
    fid, seed_id = fam["family_id"], fam["seed_id"]
    batches = [b for b in snap["batches"] if b["family_id"] == fid]
    jobs = sorted(
        (j for j in snap["jobs"].values() if j["family_id"] == fid),
        key=lambda j: _num(j["job_id"]),
    )
    proposed = [b for b in batches if b["status"] == "proposed"]
    unfinished = [j for j in jobs if j["status"] not in JOB_DONE]
    finished = len(jobs) - len(unfinished)
    images = sum(j["images"] for j in jobs)
    approved = sum(j["approved"] for j in jobs)
    pending = sum(j["pending"] for j in jobs)

    # 拆解后接着自动策划（步骤 S05）算策划；系列修订、裂变策划都是策划
    analyzing = _running(snap, ("analyze",), seed_id)
    planner = _running(snap, ("plan", "revise"), fid)
    if analyzing and str(analyzing.get("step") or "").startswith("S05"):
        planner, analyzing = planner or analyzing, None
    latest_batch = next(
        (b for b in reversed(batches) if b["family_version"] == fam["family_version"]),
        None,
    )
    plan_error = ""
    if latest_batch is None:
        recent = [
            row
            for row in (
                snap["last_bg"].get(("plan", fid)),
                snap["last_bg"].get(("revise", fid)),
                snap["last_bg"].get(("analyze", seed_id)),
            )
            if row
        ]
        if recent:
            plan_error = _bg_error(max(recent, key=lambda r: r["started_at"] or ""))

    steps = [
        _step("analyze", "current", "拆解中") if analyzing else _step("analyze", "done")
    ]
    if planner:
        steps.append(_step("plan", "current", "策划中"))
    elif batches:
        steps.append(_step("plan", "done"))
    elif plan_error:
        steps.append(_step("plan", "failed", "策划失败"))
    else:
        steps.append(_step("plan", "todo"))
    if proposed:
        steps.append(_step("pick", "current", f"挑选 {proposed[-1]['size']} 个方向"))
    else:
        steps.append(_step("pick", "done" if jobs else "todo"))
    if not jobs:
        steps.append(_step("generate", "todo"))
    else:
        steps.append(
            _step(
                "generate",
                "current" if unfinished else "done",
                f"出图 {finished}/{len(jobs)}",
            )
        )
    picks = []
    if pending:
        picks.append(f"待选 {pending}")
    if approved:
        picks.append(f"选中 {approved}")
    select_text = "选款" + (" " + " · ".join(picks) if picks else "")
    if pending:
        # 出完图后剩下的就是选款：这时才算正在等你
        steps.append(
            _step(
                "select", "current" if jobs and not unfinished else "todo", select_text
            )
        )
    else:
        steps.append(_step("select", "done" if images else "todo", select_text))
    scene_counts = None
    if snap["scenes"] is not None:
        scene_counts = snap["scenes"].get(seed_id) or dict(_NO_SCENES)
        steps.append(_scene_step(scene_counts))
    scenes_done = bool(
        scene_counts
        and scene_counts["approved"]
        and scene_counts["done"] == scene_counts["approved"]
    )
    downloads = packages.download_summary(snap["packages"].get(seed_id, []))
    if snap["packages_on"] and scene_counts and scene_counts["approved"]:
        steps.append(_download_step(downloads, scenes_done))
    listing_counts = None
    if snap["listing"] is not None:
        listing_counts = snap["listing"].get(seed_id) or dict(_NO_LISTING)
        if listing_counts["approved"]:
            steps.append(_listing_step(listing_counts, scenes_done))

    blocked = any(j["status"] == "blocked" for j in unfinished)
    working = [
        j
        for j in unfinished
        if not (j["status"] == "blocked" and j["blocked_reason"] == "review_backlog")
    ]
    manual = _manual_batches(snap, batches, unfinished)
    if proposed:
        status = "picking"
    elif blocked:
        status = "blocked"
    elif analyzing:
        status = "analyzing"
    elif planner:
        status = "planning"
    elif unfinished:
        if snap["paused"]:
            status = "paused"
        elif snap["worker_alive"] or any(j["status"] == "running" for j in unfinished):
            status = "generating"
        elif manual:
            status = "start_needed"
        else:
            status = "queued"
    elif jobs:
        status = "done"
    elif fam["status"] == "exhausted":
        status = "exhausted"
    elif fam["status"] == "rejected":
        status = "family_rejected"
    else:
        status = "idle"

    now = []
    for task in (analyzing, planner):
        if task:
            now.append(_bg_text(task))
    if proposed:
        batch = proposed[-1]
        picked = len((loads(batch["plan_json"], {}) or {}).get("picked") or [])
        now.append(
            f"{batch['batch_id']} 的 {batch['size']} 个方向等你挑选"
            + (f"（已勾 {picked} 个）" if picked else "")
        )
    if unfinished:
        now += _unfinished_text(ctx, unfinished, manual)
    if scene_counts:
        now += _scene_now(scene_counts)
    if snap["packages_on"] and scenes_done and downloads["pending"]:
        text = (
            f"交付包 {'、'.join(downloads['pending'])} 还没标已下载："
            "下载后点「B… 已下载」（或在候选表最上面那一行打勾），任务才会归档"
        )
        if downloads["outdated"]:
            text += f"；{'、'.join(downloads['outdated'])} 重打过，要下载新包"
        now.append(text)
    if not now:
        if status == "done":
            now.append(f"选中的 {len(jobs)} 个方向都出完了，共 {images} 张")
        elif status == "exhausted":
            now.append(
                "系列已耗尽"
                + (
                    f"：{_clip(fam['exhaustion_reason'], _ERROR_CHARS)}"
                    if fam["exhaustion_reason"]
                    else ""
                )
            )
        elif status == "family_rejected":
            now.append("系列规则被驳回：进入 POD 对话说修改意见")
        elif plan_error:
            now.append(f"策划没成功：{plan_error}")
        elif latest_batch and latest_batch["status"] == "rejected":
            now.append(f"{latest_batch['batch_id']} 的方向都没选中")
        elif latest_batch and latest_batch["status"] == "empty":
            now.append(f"{latest_batch['batch_id']} 没有策划出新方向")
        else:
            now.append("还没有裂变方向")

    extra: dict[str, Any] = {
        "name": snap["names"].get(seed_id),
        "counts": {
            "batches": len(batches),
            "directions": snap["variants"].get(fid, 0),
            "jobs": len(jobs),
            "jobs_done": finished,
            "images": images,
            "pending_selection": pending,
            "approved": approved,
            # 套图：出完几款、在出几款、选中了还没出（或没出成）几款；没开套图时都是 0
            "scenes_done": (scene_counts or _NO_SCENES)["done"],
            "scenes_active": (scene_counts or _NO_SCENES)["active"],
            # 在出的里面停在 mooya 等人工 / 已暂停的款数（任务归档写原因用）
            "scenes_parked": (scene_counts or _NO_SCENES)["parked"],
            "scenes_missing": (scene_counts or _NO_SCENES)["missing"],
            # 上架：选中的款里标了已上架 / 不上了 / 还没标的款数；没开上架状态时都是 0
            "listing_listed": (listing_counts or _NO_LISTING)["listed"],
            "listing_skipped": (listing_counts or _NO_LISTING)["skipped"],
            "listing_pending": (listing_counts or _NO_LISTING)["pending"],
            # 交付包：打好了几个批次、标了已下载几个；没开交付包时都是 0
            "packages_ready": sum(
                1 for v in snap["packages"].get(seed_id, []) if v["ready"]
            ),
            "packages_downloaded": downloads["downloaded"],
            # 还没出完的方向（只被待审积压挡住的不算）和它们还没出图的槽位：有就不给「选款完成」
            "jobs_unfinished": len(working),
            "images_unfinished": sum(
                j["slots"].get("pending", 0) + j["slots"].get("unknown", 0)
                for j in working
            ),
        },
        "downloads": downloads,
        "pending_batch": proposed[-1]["batch_id"] if proposed else None,
        "worker_needed": not snap["worker_alive"]
        and not snap["paused"]
        and any(j["status"] == "ready" for j in unfinished),
        "error": plan_error or None,
        "packages": snap["packages"].get(seed_id, []),
    }
    extra["stage"] = extra["stage_at"] = None
    if status == "done":
        extra["finished_at"] = max(j["updated_at"] for j in jobs)
        if scenes_done:
            if (
                snap["packages_on"]
                and downloads["batches"]
                and not downloads["pending"]
            ):
                extra["stage"], extra["stage_at"] = "downloaded", downloads["last_at"]
            else:
                extra["stage"] = "scenes_done"
                extra["stage_at"] = snap["scenes_at"].get(seed_id)
    if status in PLAN_STATUSES:
        available = directions.plan_availability(ctx, fid, running=snap["running"])
        extra["can_plan"] = bool(available["ok"])
        extra["plan_reason"] = available.get("reason")
    task = _task(status, seed_id, fam, steps, now, **extra)
    if task["stage"]:
        task["status_label"] = STAGE_LABELS[task["stage"]]
    return task


def _with_shelf(ctx: Ctx, snap: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """任务带上归档信息：shelf（没归档过为 None）、archived、能不能手动归档（archive_blockers 是挡着的事）。"""
    view = shelf.view(snap["shelf"].get(task["seed_id"]))
    task["shelf"] = view
    task["archived"] = bool(view and view["archived"])
    task["archive_blockers"] = (
        shelf.blockers(task, snap["running"]) if shelf.enabled(ctx) else []
    )
    task["can_archive"] = (
        shelf.enabled(ctx) and not task["archived"] and not task["archive_blockers"]
    )
    return task


def _all_tasks(ctx: Ctx, snap: dict[str, Any]) -> list[dict[str, Any]]:
    out = [_family_task(ctx, snap, fam) for fam in snap["families"]]
    with_family = {fam["seed_id"] for fam in snap["families"]}
    out += [
        _seed_task(snap, s) for s in snap["seeds"] if s["seed_id"] not in with_family
    ]
    out = [_with_shelf(ctx, snap, t) for t in out]
    # 同一分组里新的参考图在前
    out.sort(key=lambda t: (t["group"], -_num(t["seed_id"]), -_num(t["family_id"])))
    return out


def _matches(snap: dict[str, Any], task: dict[str, Any], query: str) -> bool:
    """历史任务的搜索：编号（S03 / F02）、名字（含日期「1020」）、参考图描述里有这几个字就算（不分大小写）。"""
    words = [w for w in query.lower().split() if w]
    haystack = " ".join(
        str(part or "")
        for part in (
            task["seed_id"],
            task.get("family_id"),
            task.get("name"),
            snap["descriptions"].get(task["seed_id"]),
        )
    ).lower()
    return all(word in haystack for word in words)


# ---------------------------------------------------------------- 对外


def summaries(
    ctx: Ctx, *, view: str = "active", query: str | None = None
) -> list[dict[str, Any]]:
    """任务（一张参考图一个）。view：active（默认，没归档的，按「待你处理 → 进行中 → 已完成 → 其他」排）、
    archived（历史任务，归档时间从新到旧）、all（全部）；query 按编号 / 名字 / 描述搜。"""
    if view not in VIEWS:
        raise StageError(f"view 只能是 {list(VIEWS)}")
    snap = _snapshot(ctx)
    items = _all_tasks(ctx, snap)
    if view == "active":
        items = [t for t in items if not t["archived"]]
    elif view == "archived":
        items = sorted(
            (t for t in items if t["archived"]),
            key=lambda t: (
                (t["shelf"] or {}).get("archived_at") or "",
                _num(t["seed_id"]),
            ),
            reverse=True,
        )
    if query and query.strip():
        items = [t for t in items if _matches(snap, t, query)]
    return items


def archived_count(ctx: Ctx) -> int:
    """已归档的任务（参考图）有几个：任务列表表头和「历史任务」按钮用。"""
    return len(shelf.archived_seeds(ctx))


def family_detail(ctx: Ctx, family_id: str) -> dict[str, Any]:
    """一个任务的明细：任务概况 + 方向记录（directions.overview），每个方向带细到哪一步的 progress_text。"""
    snap = _snapshot(ctx)
    fam = next((f for f in snap["families"] if f["family_id"] == family_id), None)
    if fam is None:
        raise StageError(f"没有系列 {family_id}")
    jobs = {
        j["variant_id"]: j for j in snap["jobs"].values() if j["family_id"] == family_id
    }
    view = directions.overview(ctx, family_id)
    for batch in view["batches"]:
        batch_jobs = [
            jobs[d["variant_id"]]
            for d in batch["directions"]
            if d["variant_id"] in jobs
        ]
        batch["jobs"] = len(batch_jobs)
        batch["jobs_done"] = sum(1 for j in batch_jobs if j["status"] in JOB_DONE)
        for d in batch["directions"]:
            job = jobs.get(d["variant_id"])
            d["job_id"] = job["job_id"] if job else None
            d["progress_text"] = job_text(job) if job else d["state_text"]
            d["pending_selection"] = job["pending"] if job else 0
    return {"task": _with_shelf(ctx, snap, _family_task(ctx, snap, fam)), **view}
