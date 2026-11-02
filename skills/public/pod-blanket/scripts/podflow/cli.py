"""podctl：POD 毛毯流水线命令行。所有子命令向 stdout 输出一个 JSON 对象。

设计约束：
- 预算上限与单价只能由人编辑 $PODFLOW_HOME/podflow.yaml，这里不提供修改命令；
- 需要人拍板的动作（授权、批准系列、开试运行、裁决审核项）都要求 --by 写明决定人；
- 长耗时命令（seed analyze / family revise / plan / worker）可加 --detach 转后台，避免沙箱 bash 的 600 秒超时；
  后台任务登记在 bg_tasks，加 --notify 时跑完发飞书卡片。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import (
    __version__,
    bg,
    books,
    budget,
    candidate_stores,
    candidates,
    cells,
    crafted,
    derive,
    digest,
    directions,
    doctor,
    downloads,
    feishu,
    feishu_table,
    fixes,
    labels,
    lease,
    listed_table,
    listing,
    llm,
    maintenance,
    miaoshou,
    mooya,
    notify,
    packages,
    reasons,
    report,
    review,
    scenes,
    shelf,
    state,
    store,
    stores,
    table_template,
    tasks,
    temu,
    temu_open,
    temu_title,
    title_sync,
    trace,
    ui,
    week_migration,
)
from . import config as cfgmod
from .context import DB_NAME, Ctx, open_ctx, open_db
from .db import MigrationError, copy_from_sqlite, log_event, table_counts
from .oss import OSSError
from .paths import CONFIG_EXAMPLE, podflow_home
from .prompts import PromptError
from .stages import plan as planmod
from .stages import profile as profilemod
from .stages import seed as seedmod
from .stages.common import StageError, get_family, seed_name, slot_mode
from .stages.job import asset_costs
from .tick import tick
from .util import iso, loads, now_iso, parse_iso, utcnow
from .worker import Worker

SECRETS_TEMPLATE = """# POD 毛毯流水线凭证：只放这里，权限保持 600，不要提交、不要打印
# LiteLLM（mooya 的代理 :4001）客户端 key
LITELLM_API_KEY=
# 飞书通知（M3 才需要）
FEISHU_APP_ID=
FEISHU_APP_SECRET=
# 业务库放 Postgres 时的连接串（podflow.yaml 的 database.url_name 填这一项的名字）
PODFLOW_DATABASE_URL=
# 素材存 OSS 时的凭证（podflow.yaml 的 storage.oss）
OSS_ACCESS_KEY_ID=
OSS_ACCESS_KEY_SECRET=
# mooya 机器调用接口的服务令牌（出套图用；mooya 那边只存它的 sha256）
MOOYA_SERVICE_TOKEN=
"""


def _out(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def _home() -> Path:
    return podflow_home()


# ------------------------------------------------------------------ 命令实现


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    home = _home()
    for sub in ("assets", "logs", "reports", "inbox"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    created = []
    cfg_path = home / cfgmod.CONFIG_NAME
    if not cfg_path.exists():
        shutil.copyfile(CONFIG_EXAMPLE, cfg_path)
        created.append(cfgmod.CONFIG_NAME)
    secrets_path = home / cfgmod.SECRETS_NAME
    if not secrets_path.exists():
        secrets_path.write_text(SECRETS_TEMPLATE, encoding="utf-8")
        created.append(cfgmod.SECRETS_NAME)
    os.chmod(secrets_path, 0o600)
    open_db(home, cfgmod.load_config(home), cfgmod.load_secrets(home)).init_schema()
    return {
        "home": str(home),
        "created": created,
        "next": [
            "在 secrets.env 填 LITELLM_API_KEY（不要打印）",
            "在 podflow.yaml 填 budget（CFG05–CFG08）与 prices（三个生图模型 + gpt-6.1-sol）",
            "podctl doctor 自检",
        ],
    }


def cmd_doctor(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    return doctor.run(ctx, probe=args.probe)


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    snap = report.snapshot(ctx)
    snap["last_tick"] = loads(ctx.db.get_meta("last_tick"))
    snap["version"] = __version__
    return snap


_RAW_ARGV: list[str] = []


def _spawn_self(
    ctx: Ctx, *, kind: str, subject: str | None, notify_on_finish: bool
) -> dict[str, Any]:
    """把当前这条命令（去掉 --detach）转成登记过的后台任务。"""
    argv = [a for a in _RAW_ARGV if a != "--detach"]
    return bg.spawn(ctx, argv, kind=kind, subject=subject, notify=notify_on_finish)


def cmd_seed(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.seed_cmd == "add":
        if args.from_inbox:
            src = ctx.home / "inbox" / args.from_inbox
        elif args.path:
            src = Path(args.path)
        else:
            raise StageError("需要图片路径或 --from-inbox 文件名")
        if not src.exists():
            raise StageError(f"找不到文件 {src}")
        return seedmod.add_seed(
            ctx,
            src,
            source_ref=args.source_ref,
            source_note=args.note,
            rights=args.rights,
            sales_evidence=args.sales_evidence,
            positioning=args.positioning,
        )
    if args.seed_cmd == "list":
        return {
            "seeds": [
                dict(r)
                for r in ctx.db.q(
                    "SELECT s.seed_id, n.name, s.status, s.rights_status, s.source_ref, s.latest_analysis_id,"
                    " s.created_at FROM seeds s LEFT JOIN seed_names n ON n.seed_id = s.seed_id"
                    " ORDER BY s.seed_id"
                )
            ]
        }
    if args.seed_cmd == "name":
        if args.description is not None:
            return seedmod.set_seed_name(
                ctx, args.seed_id, args.description, by=args.by
            )
        return seedmod.name_seed(ctx, args.seed_id)
    if args.seed_cmd == "show":
        seed = seedmod.get_seed(ctx, args.seed_id)
        seed["name"] = seed_name(ctx, args.seed_id)
        seed["seed_record"] = loads(seed.pop("seed_record_json"))
        pattern = ctx.db.q1(
            "SELECT path, version, model, hint, updated_at FROM seed_patterns WHERE seed_id = ?",
            (args.seed_id,),
        )
        # 提取出的平面图案（S01P）；之后的拆解、图生图、质检都用它
        seed["pattern"] = dict(pattern) if pattern else None
        analysis = seedmod.latest_analysis(ctx, args.seed_id)
        desc = profilemod.get_description(
            ctx, analysis["analysis_id"] if analysis else None
        )
        # 画面完整描述（S02D）只给总述；全文用 seed describe --show
        seed["description"] = (
            {
                "analysis_id": desc["analysis_id"],
                "summary": desc["result"].get("summary"),
                "genre": (desc["result"].get("style") or {}).get("genre"),
                "subjects": len(desc["result"].get("subjects") or []),
                "units": profilemod.unit_lines(profilemod.change_units(desc["result"])),
                "hint": desc["hint"],
                "updated_at": desc["updated_at"],
            }
            if desc
            else None
        )
        if analysis:
            result = loads(analysis["result_json"], {})
            seed["analysis"] = {
                "analysis_id": analysis["analysis_id"],
                "coverage": loads(analysis["coverage_json"]),
                "fields": result.get("fields")
                if args.full
                else [
                    f
                    for f in result.get("fields", [])
                    if f.get("field_id")
                    in (
                        "D05",
                        "D06",
                        "D09",
                        "D27",
                        "D32",
                        "D36",
                        "D37",
                        "D41",
                        "D42",
                        "D45",
                        "D46",
                        "D49",
                    )
                ],
                "objects": result.get("objects"),
                "relations": result.get("relations"),
                # 这张图能拿来裂变的字段（梳理字段之前拆的没有）
                "variation": result.get("variation"),
            }
        if seed.get("latest_hypothesis_set_id"):
            hyp = ctx.db.q1(
                "SELECT result_json FROM hypotheses WHERE hypothesis_set_id = ?",
                (seed["latest_hypothesis_set_id"],),
            )
            seed["hypotheses"] = loads(hyp["result_json"]) if hyp else None
        return seed
    if args.seed_cmd == "rights":
        return seedmod.set_rights(
            ctx, args.seed_id, args.status, by=args.by, note=args.note
        )
    if args.seed_cmd == "extract":
        seedmod.get_seed(ctx, args.seed_id)
        if not seedmod.pattern_enabled(ctx):
            raise StageError(
                "pattern_extract.enabled 为 false：图案提取关着，提取了也不会用"
            )
        if args.detach:
            busy = _running_task(ctx, "extract", args.seed_id) or _running_task(
                ctx, "analyze", args.seed_id
            )
            if busy:
                raise StageError(
                    f"{args.seed_id} 正在{bg.KIND_LABELS.get(busy['kind'], busy['kind'])}（{busy['task_id']}），完成后会通知"
                )
            return _spawn_self(
                ctx, kind="extract", subject=args.seed_id, notify_on_finish=args.notify
            )
        return seedmod.extract_pattern(ctx, args.seed_id, hint=args.hint)
    if args.seed_cmd == "describe":
        return _describe(ctx, args)
    if args.seed_cmd == "analyze":
        if args.force and args.reanalyze:
            raise StageError("--force 与 --reanalyze 只能选一个")
        # 已归档的任务重新拆解：先调出来（拆完会自动策划、出图）
        restored = shelf.touch(ctx, args.seed_id, reason="重新拆解")
        if args.detach:
            return {
                **_spawn_self(
                    ctx,
                    kind="analyze",
                    subject=args.seed_id,
                    notify_on_finish=args.notify,
                ),
                **({"restored": restored} if restored else {}),
            }
        result = seedmod.analyze(
            ctx,
            args.seed_id,
            force=args.force,
            reanalyze=args.reanalyze,
            positioning=args.positioning,
        )
        row = ctx.db.q1(
            "SELECT family_id FROM families WHERE seed_id = ? ORDER BY created_at DESC LIMIT 1",
            (args.seed_id,),
        )
        result.update(
            _auto_plan(
                ctx, row["family_id"] if row else None, seedmod.step_label(ctx, "S05")
            )
        )
        return result
    raise StageError(f"未知子命令 {args.seed_cmd}")


def _describe(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """画面完整描述（S02D）：写一张、给所有拆解过的参考图补写，或只看现在的描述。"""
    if args.show:
        if not args.seed_id:
            raise StageError(
                "--show 要写参考图编号，如 podctl seed describe S03 --show"
            )
        return profilemod.show(ctx, args.seed_id)
    if bool(args.seed_id) == bool(args.all):
        raise StageError("写参考图编号，或者 --all（所有拆解过的参考图），二选一")
    if args.all and args.hint:
        raise StageError("--hint 只能给一张参考图写")
    if args.seed_id:
        seedmod.get_seed(ctx, args.seed_id)
    if not profilemod.describe_enabled(ctx):
        raise StageError("describe.enabled 为 false：画面描述关着，写了也不会用")
    profilemod.clean_hint(args.hint)
    if args.detach:
        busy = _running_task(ctx, "describe") or (
            _running_task(ctx, "analyze", args.seed_id) if args.seed_id else None
        )
        if busy:
            raise StageError(
                f"正在{bg.KIND_LABELS.get(busy['kind'], busy['kind'])}（{busy['task_id']}），完成后再写"
            )
        return _spawn_self(
            ctx, kind="describe", subject=args.seed_id, notify_on_finish=args.notify
        )
    if args.all:
        return profilemod.describe_all(ctx, force=args.force)
    return profilemod.describe_seed(ctx, args.seed_id, force=args.force, hint=args.hint)


def _auto_plan(
    ctx: Ctx, family_id: str | None, step: str, kinds: list[str] | None = None
) -> dict[str, Any]:
    """拆解 / 修订出了待认可的系列：接着策划一轮候选方向，系列随方向一起认可（只审一次）；kinds 只策划这几类。

    开着 batch.auto_confirm_directions 时策划出的方向当场全部确认、开始出图（系列一并认可）。
    """
    if not family_id or not ctx.setting("batch.auto_plan_after_analyze", True):
        return {}
    fam = get_family(ctx, family_id)
    if fam["status"] != "pending" or any(
        b["family_version"] == fam["family_version"]
        for b in planmod.proposed_batches(ctx, family_id)
    ):
        return {}
    try:
        plan = planmod.plan_batch(ctx, family_id, step=step, kinds=kinds)
    except (StageError, llm.LLMError, budget.BudgetBlocked, PromptError) as exc:
        # 拆解 / 修订本身成功了：策划失败只记下原因，工作台上可以再点「策划」
        return {"plan_error": f"{type(exc).__name__}：{exc}"[:300]}
    plan.update(directions.auto_confirm(ctx, plan))
    return {"plan": plan}


def cmd_run(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    stage = args.stage.upper()
    if stage == "S01":
        return seedmod.run_s01(ctx, args.seed)
    if stage == "S02":
        return seedmod.run_s02(ctx, args.seed)
    if stage == "S03":
        return seedmod.run_s03(ctx, args.seed)
    if stage == "S04":
        return seedmod.run_s04(ctx, args.seed, positioning=args.positioning)
    raise StageError(
        "run 只用于母版阶段 S01–S04；S05 用 podctl plan，S06–S10 由 worker 推进"
    )


def _family_view(
    ctx: Ctx, family_id: str, version: int | None = None
) -> dict[str, Any]:
    fam = get_family(ctx, family_id, version)
    fam.pop("spec_json", None)
    return fam


def cmd_family(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.family_cmd == "list":
        return {
            "families": [
                dict(r)
                for r in ctx.db.q(
                    "SELECT family_id, family_version, seed_id, status, approved_by, exhaustion_reason, created_at FROM families"
                    " ORDER BY family_id, family_version"
                )
            ]
        }
    if args.family_cmd == "show":
        return _family_view(ctx, args.family_id, args.version)
    if args.family_cmd in ("approve", "reject"):
        return seedmod.decide_family(
            ctx,
            args.family_id,
            approve=args.family_cmd == "approve",
            by=args.by,
            note=args.note,
        )
    if args.family_cmd == "revise":
        fam = get_family(ctx, args.family_id)
        kinds = _plan_kinds(args.kinds)
        # 已归档的任务修订系列：先调出来（修订完会自动策划、出图）
        restored = shelf.touch(ctx, args.family_id, reason="修订系列")
        if args.detach:
            return {
                **_spawn_self(
                    ctx,
                    kind="revise",
                    subject=args.family_id,
                    notify_on_finish=args.notify,
                ),
                **({"restored": restored} if restored else {}),
            }
        result = seedmod.run_s04(
            ctx,
            fam["seed_id"],
            revise_family_id=args.family_id,
            revision_note=args.note,
        )
        result.update(
            _auto_plan(ctx, result["family_id"], "S05 裂变策划（修订后）", kinds)
        )
        return result
    raise StageError(f"未知子命令 {args.family_cmd}")


def _plan_kinds(
    text: str | None, allowed: tuple[str, ...] = planmod.PLAN_KINDS
) -> list[str] | None:
    kinds = [k for k in re.split(r"[\s,，、]+", text or "") if k]
    unknown = [k for k in kinds if k not in allowed]
    if unknown:
        raise StageError(f"--kinds 只能取 {list(allowed)}，不认识 {unknown}")
    return kinds or None


def _cmd_derive(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """plan --derive-due / --derive-from：二次大变化（以换了主体的小变化出的图为底图）。"""
    parents = directions.parse_refs(args.parents) or None
    if args.derive_due:
        if args.preview:
            # 只看：哪些批次该派生、派生过的记录
            return derive.status(ctx)
        if args.detach:
            if _running_task(ctx, derive.KIND):
                raise StageError("二次大变化正在策划，完成后会接着出图")
            return _spawn_self(
                ctx, kind=derive.KIND, subject=None, notify_on_finish=args.notify
            )
        return derive.run_due(ctx)
    if args.preview:
        return derive.preview(ctx, args.derive_from, parents=parents, count=args.count)
    if args.detach:
        if _running_task(ctx, derive.KIND):
            raise StageError("二次大变化正在策划，完成后再说")
        return _spawn_self(
            ctx,
            kind=derive.KIND,
            subject=args.derive_from,
            notify_on_finish=args.notify,
        )
    return derive.run_one(
        ctx, args.derive_from, by=args.by, parents=parents, count=args.count
    )


def cmd_plan(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.derive_due or args.derive_from:
        return _cmd_derive(ctx, args)
    if not args.family:
        raise StageError("要写 --family（或用 --derive-from B… 做二次大变化）")
    kinds = _plan_kinds(args.kinds)
    if args.preview:
        return planmod.preview_slots(ctx, args.family)
    if args.detach:
        fam = planmod.planning_family(ctx, args.family)
        if args.limit is not None and args.limit < 1:
            raise StageError("limit 至少是 1")
        # 按旧字段字典拆的系列、或没有可变字段：当场报错，不拉起后台
        inputs = planmod.unit_pools(ctx, fam)
        if not planmod.pool_counts(ctx, inputs["pools"], inputs["pairs"])["total"]:
            raise StageError(
                f"{args.family} 没有可以裂变的字段：拆解判定都不能变，或系列把它们都锁定了；"
                "在 POD 对话里修订系列"
            )
        planmod.clean_hint(args.hint)
        busy = (
            _running_task(ctx, "plan", args.family)
            or _running_task(ctx, "revise", args.family)
            or _running_task(ctx, "analyze", fam["seed_id"])
        )
        if busy:
            raise StageError(
                f"{args.family} 正在策划（{busy['task_id']}），完成后会通知"
            )
        waiting = [
            b["batch_id"]
            for b in planmod.proposed_batches(ctx, args.family)
            if b["family_version"] == fam["family_version"]
        ]
        if waiting:
            raise StageError(
                f"{args.family} 还有一批方向待挑选（{waiting[0]}）：先挑完，或点「都不满意，换一批」"
            )
        # 历史任务里点「继续裂变」：先调出来，任务列表里马上能看到它在策划
        restored = shelf.touch(ctx, args.family, reason="继续裂变")
        return {
            **_spawn_self(
                ctx, kind="plan", subject=args.family, notify_on_finish=args.notify
            ),
            **({"restored": restored} if restored else {}),
        }
    restored = shelf.touch(ctx, args.family, reason="继续裂变")
    result = planmod.plan_batch(
        ctx, args.family, limit=args.limit, hint=args.hint, kinds=kinds
    )
    result.update(directions.auto_confirm(ctx, result))
    if restored:
        result["restored"] = restored
    return result


def cmd_directions(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.directions_cmd == "show":
        data = directions.show(ctx, args.batch_id)
        return {"task": labels.task(ctx, args.batch_id), **data}
    if args.directions_cmd == "history":
        data = directions.overview(ctx, args.family_id)
        return {"task": labels.task(ctx, args.family_id), **data}
    if args.directions_cmd == "pick":
        return directions.pick(ctx, args.batch_id, args.direction, on=not args.off)
    if args.directions_cmd == "confirm":
        return directions.confirm(
            ctx,
            args.batch_id,
            by=args.by,
            pick=directions.parse_refs(args.pick) or None,
            start=args.start,
        )
    if args.directions_cmd == "cancel":
        return directions.cancel(
            ctx,
            args.batch_id,
            directions.parse_refs(args.pick) or None,
            kinds=_plan_kinds(args.kind, tuple(planmod.KIND_LABELS)),
            by=args.by,
        )
    if args.directions_cmd == "reject":
        return directions.reject(
            ctx, args.batch_id, by=args.by, replan=args.replan, notify=args.notify
        )
    raise StageError(f"未知子命令 {args.directions_cmd}")


def _running_task(
    ctx: Ctx, kind: str, subject: str | None = None
) -> dict[str, Any] | None:
    return next(
        (
            t
            for t in bg.running(ctx)
            if t["kind"] == kind and (subject is None or t["subject"] == subject)
        ),
        None,
    )


def cmd_worker(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.detach:
        worker = state.worker_status(ctx.db)
        if (
            _running_task(ctx, "worker")
            or _running_task(ctx, "fix")
            or (worker and worker.get("alive"))
        ):
            raise StageError("已经有 worker 在出图：排队的任务它会接着领，不用再开")
        return _spawn_self(
            ctx, kind="worker", subject=None, notify_on_finish=args.notify
        )
    worker = Worker(
        ctx,
        batch_limit=args.batch,
        max_minutes=args.max_minutes,
        allow_plan=False if args.no_plan else None,
        allow_generation=not args.no_generation,
        source=args.source,
        only_jobs=args.job,
        lanes=args.lanes,
    )
    return worker.run()


def cmd_tick(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.auto:
        return _auto_tick(ctx)
    result = tick(ctx, spawn=not args.no_spawn)
    if args.notify:
        result["notified"] = notify.notify_tick(ctx, result)
    return result


def _auto_tick(ctx: Ctx) -> dict[str, Any]:
    """网关定时器每分钟调一次：到了巡检间隔才巡检（有异常发卡片），到了日报时刻每天发一次日报；
    精简通知（notify.style: brief）时不发巡检卡和日报，改由 digest.run_schedule 按点发状态总结、按间隔提醒待办。

    没到点直接返回 skipped，不写事件，避免每分钟一条流水。
    """
    if not ctx.setting("schedule.enabled", True):
        return {"skipped": True, "reason": "schedule.enabled 为 false"}
    now = utcnow()
    out: dict[str, Any] = {"skipped": True}
    minutes = float(
        ctx.setting("schedule.tick_interval_minutes")
        or ctx.setting("batch.wake_interval_minutes", 10)
    )
    last = ctx.db.get_meta("last_auto_tick_at")
    # 定时器每分钟一跳，留 30 秒余量，免得刚好差几秒被推迟一整轮
    if not last or now - parse_iso(last) >= dt.timedelta(minutes=minutes, seconds=-30):
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, "last_auto_tick_at", iso(now))
        result = tick(ctx)
        out = {
            "skipped": False,
            "decision": result["decision"],
            "reason": result["reason"],
            "spawned": len(result["jobs"]),
            "notified": notify.notify_tick(ctx, result),
        }
        # 上传进程退出前后刚登记的素材会漏掉，巡检时补拉一次
        sync_task = store.kick(ctx)
        if sync_task:
            out["storage_sync"] = sync_task["task_id"]
        # 选款表：补漏、补上原图已传好的下载链接、续签快到期的链接
        table_task = feishu_table.kick(ctx)
        if table_task:
            out["table_sync"] = table_task["task_id"]
        # 候选表按钮行兜底：事件没送到（网关重启、还没订阅）时，勾着的按钮行 / 交付包行由后台回读接上
        controls_task = candidates.kick_controls(ctx)
        if controls_task:
            out["controls_poll"] = controls_task["task_id"]
        # 套图：接着交排队的款、查在 mooya 上出图的进度（等人工的也隔一轮查一次）
        scenes_task = scenes.kick(ctx)
        if scenes_task:
            out["scenes_sync"] = scenes_task["task_id"]
        # Temu 上架：准备上架图与标题、领货号、建箱、发布、查发布结果
        temu_task = temu.kick(ctx)
        if temu_task:
            out["temu_sync"] = temu_task["task_id"]
        # 二次大变化：小变化出完的批次接着以换了主体的小变化图为底图策划（worker 的反馈循环也会接）
        derive_task = derive.kick(ctx)
        if derive_task:
            out["derive"] = derive_task["task_id"]
        # 任务归档：有新动静的历史任务调出来；开着 shelf.auto 时做完的任务收起来（合并发一张卡片）
        try:
            shelved = shelf.sweep(ctx, source="tick")
        except Exception as exc:  # noqa: BLE001 - 归档出错不挡巡检
            shelved = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if shelved.get("archived") or shelved.get("reopened") or shelved.get("error"):
            out["shelf"] = {
                k: shelved.get(k)
                for k in ("archived", "reopened", "error")
                if shelved.get(k)
            }
            if shelved.get("archived"):
                out["shelf"]["notified"] = shelf.announce(ctx, shelved["archived"])
    if digest.enabled(ctx):
        try:
            out.update(digest.run_schedule(ctx, now))
        except Exception as exc:  # noqa: BLE001 - 抛出去会让网关定时器退避，连巡检一起变慢
            out["notify_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    report_at = ctx.setting("schedule.daily_report_at", "21:00")
    if report_at:
        tz = ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
        local = now.astimezone(tz)
        day = local.strftime("%Y-%m-%d")
        hour, minute = (int(x) for x in str(report_at).split(":", 1))
        if (local.hour, local.minute) >= (hour, minute) and ctx.db.get_meta(
            "last_daily_report_day"
        ) != day:
            # 发送失败也记下当天，不在通知故障时每分钟重试刷屏
            with ctx.db.tx() as conn:
                ctx.db.set_meta(conn, "last_daily_report_day", day)
            try:
                out["daily"] = notify.send_pod_card(
                    ctx,
                    notify.daily_card(ctx, day),
                    report.text_summary(report.snapshot(ctx)),
                )
            except notify.NotifyError as exc:
                out["daily"] = {"sent": False, "error": str(exc)}
    return out


def cmd_review(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.review_cmd == "list":
        status = "all" if args.all else "open"
        items = review.list_reviews(
            ctx, status=status, types=args.type, batch_id=args.batch
        )
        return {
            "count": len(items),
            "reviews": labels.reviews(ctx, items),
            "decision_help": review.DECISION_HELP,
        }
    if args.review_cmd == "decide":
        subject = ctx.db.scalar(
            "SELECT subject_id FROM reviews WHERE review_id = ?", (args.review_id,)
        )
        result = review.decide(
            ctx, args.review_id, args.decision, by=args.by, note=args.note
        )
        return {**result, "label": labels.ref(ctx, subject)}
    if args.review_cmd == "sheet":
        return review.contact_sheet(
            ctx,
            Path(args.out),
            batch_id=args.batch,
            review_type=args.type,
            asset_ids=args.assets,
        )
    if args.review_cmd == "export":
        return review.export_xlsx(ctx, Path(args.xlsx), batch_id=args.batch)
    if args.review_cmd == "import":
        return review.import_xlsx(ctx, Path(args.xlsx), by=args.by)
    raise StageError(f"未知子命令 {args.review_cmd}")


def cmd_asset(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    row = ctx.db.q1("SELECT * FROM assets WHERE asset_id = ?", (args.asset_id,))
    if not row:
        raise StageError(f"没有素材 {args.asset_id}")
    if args.timeline:
        # 一款从出图到上架的完整时间线（轨迹、事件、审核、调用、人改格子、各表时间点）
        return {"label": labels.asset(ctx, args.asset_id), **trace.timeline(ctx, args.asset_id, limit=args.limit)}
    asset = dict(row)
    # 近重复判定已下线：旧素材上的哈希与判定记录不再展示
    for key in ("dedup_json", "dhash", "phash"):
        asset.pop(key, None)
    asset["label"] = labels.asset(ctx, args.asset_id)
    asset["task"] = labels.task(ctx, args.asset_id)
    asset["mode"] = slot_mode(asset["slot_id"])
    for key in (
        "qc_json",
        "print_json",
        "archive_json",
        "repair_instructions_json",
    ):
        asset[key.removesuffix("_json")] = loads(asset.pop(key))
    asset["costs"] = asset_costs(ctx, dict(row))
    # 流水线现在在哪一环（每家店）、店铺 / 货号 / 妙手、套图（含 mooya 任务号、交给 mooya 的包名）
    asset["pipeline"] = trace.rows(ctx, args.asset_id)
    asset["listings"] = [
        {k: r.get(k) for k in ("store_key", "state", "code", "detail_id", "confirmed_by", "boxed_at", "listed_at", "last_error")}
        for r in stores.rows(ctx, asset_ids=[args.asset_id], active=False)
    ]
    asset["scene_sets"] = [
        {
            **{k: r[k] for k in ("set_id", "status", "mooya_job_id", "created_at", "finished_at", "last_error")},
            "flags": scenes.flags(ctx, r["set_id"]),
        }
        for r in ctx.db.q("SELECT * FROM scene_sets WHERE asset_id = ? ORDER BY created_at", (args.asset_id,))
    ]
    if args.export:
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store.ensure_local(ctx, asset["path"]), out)
        asset["exported_to"] = str(out)
    return asset


def cmd_job(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.job_cmd == "list":
        sql = "SELECT job_id, variant_id, batch_id, stage, status, attempt, blocked_reason, last_error FROM jobs"
        params: tuple = ()
        if args.batch:
            sql += " WHERE batch_id = ?"
            params = (args.batch,)
        rows = [dict(r) for r in ctx.db.q(sql + " ORDER BY job_id", params)]
        names = labels.jobs(ctx, [r["job_id"] for r in rows])
        return {"jobs": [{**r, "label": names[r["job_id"]]} for r in rows]}
    if args.job_cmd == "redo":
        result = maintenance.redo_jobs(
            ctx, args.job_ids, by=args.by, dry_run=args.dry_run
        )
        if not args.dry_run:
            # 已归档的任务按新规则重出：调出来（重出的图会进选款）
            for ref in dict.fromkeys(args.job_ids):
                restored = shelf.touch(ctx, ref, reason="按新规则重出", by=args.by)
                if restored:
                    result.setdefault("restored", []).append(restored)
            if ctx.db.get_meta("paused") == "1":
                result["next"] = (
                    "流水线暂停中：podctl resume 后按新规则重新写指令、重新出图"
                )
            else:
                result["directions"] = directions.start_runs(ctx)
        return result
    job = ctx.db.q1("SELECT * FROM jobs WHERE job_id = ?", (args.job_id,))
    if not job:
        raise StageError(f"没有任务 {args.job_id}")
    data = dict(job)
    data["label"] = labels.job(ctx, args.job_id)
    data["task"] = labels.task(ctx, args.job_id)
    data["render_spec"] = loads(data.pop("render_spec_json"))
    data["slots"] = [
        dict(r)
        for r in ctx.db.q("SELECT * FROM job_slots WHERE job_id = ?", (args.job_id,))
    ]
    data["assets"] = [
        dict(r)
        for r in ctx.db.q(
            "SELECT asset_id, model, slot_id, status, repair_depth, parent_asset_id FROM assets WHERE job_id = ?",
            (args.job_id,),
        )
    ]
    names = labels.assets(ctx, [a["asset_id"] for a in data["assets"]])
    for item in data["assets"]:
        item["label"] = names[item["asset_id"]]["label"]
    return data


def cmd_db(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.db_cmd == "info":
        return {
            "backend": ctx.db.dialect,
            "target": ctx.db.describe(),
            "rows": table_counts(ctx.db),
        }
    if args.db_cmd == "migrate-from-sqlite":
        src = Path(args.path) if args.path else ctx.home / DB_NAME
        return copy_from_sqlite(src, ctx.db)
    raise StageError(f"未知子命令 {args.db_cmd}")


def cmd_storage(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.storage_cmd == "status":
        return store.status(ctx)
    if args.storage_cmd == "sync":
        return store.sync(ctx, register_all=args.all, retry=args.retry)
    if args.storage_cmd == "prune":
        return store.prune(
            ctx, older_than_days=args.older_than_days, dry_run=args.dry_run
        )
    raise StageError(f"未知子命令 {args.storage_cmd}")


def cmd_table(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.table_cmd == "init":
        return feishu_table.init(ctx, reset=args.reset)
    if args.table_cmd == "sync":
        if args.detach:
            if not feishu_table.enabled(ctx):
                raise StageError(
                    "table.enabled 为 false：先在 podflow.yaml 打开 table.enabled"
                )
            return _spawn_self(ctx, kind="table", subject=None, notify_on_finish=False)
        # 选中的款与各任务的候选表一起同步（共用租约，先后做）
        out = feishu_table.sync(ctx, retry=args.retry)
        out["candidates"] = candidates.sync(ctx, retry=args.retry)
        return out
    if args.table_cmd in ("accept", "dispatch"):
        kind = args.table_cmd
        if not re.fullmatch(r"S\d+", args.seed or ""):
            raise StageError(f"任务编号不对：{args.seed}（应当像 S01）")
        if args.detach:
            if not feishu_table.enabled(ctx):
                raise StageError("table.enabled 为 false：候选表没有打开")
            if not candidates.table_info(ctx, args.seed):
                raise StageError(f"{args.seed} 还没有候选表：图进选款后会自动建表")
            for busy_kind in ("accept", "dispatch"):
                if _running_task(ctx, busy_kind, args.seed):
                    raise StageError(
                        f"{args.seed} 正在{candidates.ACTION_LABELS[busy_kind]}，完成后会通知"
                    )
            return _spawn_self(
                ctx, kind=kind, subject=args.seed, notify_on_finish=args.notify
            )
        if kind == "dispatch":
            return candidates.dispatch(ctx, args.seed, by=args.by, source=args.source)
        return candidates.accept(ctx, args.seed, by=args.by, source=args.source)
    if args.table_cmd == "control":
        records = [r for r in re.split(r"[\s,]+", args.records or "") if r]
        if not (
            _FEISHU_TOKEN_RE.fullmatch(args.file or "")
            and _FEISHU_TOKEN_RE.fullmatch(args.table or "")
            and records
            and all(_FEISHU_TOKEN_RE.fullmatch(r) for r in records)
            and re.fullmatch(r"feishu:ou_[A-Za-z0-9_-]+", args.by or "")
        ):
            raise StageError(
                "参数不对：--file / --table / --records 是飞书的编号，--by 是 feishu:ou_…"
            )
        if books.migrating(ctx):
            # 迁移完巡检回读会兜住这次的勾
            return {"ignored": "每周选款表正在迁移（maintenance migrate-week），搬完再处理"}
        return candidates.control_event(
            ctx,
            file_token=args.file,
            table_id=args.table,
            record_ids=records[:500],
            by=args.by,
        )
    if args.table_cmd == "subscribe":
        return feishu_table.subscribe_now(ctx)
    if args.table_cmd == "poll":
        if books.migrating(ctx):
            return {"skipped": "每周选款表正在迁移（maintenance migrate-week），搬完再回读"}
        out = candidates.poll_controls(ctx)
        # 「选中的款」的上架状态也在这里回读（到了 listing.read_minutes 才读）
        read = listing.refresh_due(ctx)
        if read is not None:
            out = {**out, "listing": read}
        # 「选中的款」的下载确认：事件漏掉的勾、人取消的勾
        confirms = downloads.poll(ctx)
        if confirms is not None:
            out = {**out, "downloads": confirms}
        # 「选中的款」的确认上架（同上），和「已上架的款」人填的列（到了 temu.listed.read_minutes 才读）
        listings = temu.poll(ctx)
        if listings is not None:
            out = {**out, "temu": listings}
        listed = listed_table.read_due(ctx)
        if listed is not None:
            out = {**out, "listed": listed}
        # 候选表里勾的店铺（事件漏掉的、撤销窗口到点的、取消的勾），人改的格子（到了 POLL_MINUTES 才整张读）
        ticks = candidate_stores.poll(ctx)
        if ticks is not None:
            out = {**out, "store_ticks": ticks}
        # 「选中的款」里人改的格子（备注、上架几列、人加的列）记日志；改了上架标题的同步到店铺（到了 cells_read_minutes 才读）
        edited = cells.read_selected(ctx)
        if edited is not None:
            out = {**out, "cells": edited}
        return out
    if args.table_cmd == "ticks":
        # 撤销窗口：勾了店铺的行到点回读、处理（control_event / 巡检拉起，没有要等的就退出）
        return candidate_stores.watch_pending(ctx, max_minutes=args.max_minutes)
    if args.table_cmd == "cells":
        # 人改格子的日志：看一款 / 一个货号的（--asset / --code），或现在回读一遍「选中的款」（--read）
        if args.read:
            return cells.read_selected(ctx, force=True) or {}
        return {"history": cells.history(ctx, asset_id=args.asset, code=args.code, limit=args.limit)}
    if args.table_cmd == "downloaded":
        assets = [a for a in re.split(r"[\s,]+", args.asset or "") if a]
        if not assets or not all(_ASSET_RE.fullmatch(a) for a in assets):
            raise StageError(
                f"素材编号不对：{args.asset}（应当像 IMG00327，多个用逗号分开）"
            )
        return downloads.mark_assets(ctx, assets, by=args.by, undo=args.undo)
    if args.table_cmd == "status":
        return {
            **feishu_table.status(ctx),
            "candidates": {
                **candidates.status(ctx),
                "template": table_template.status(ctx),
            },
        }
    if args.table_cmd == "galleries":
        if not feishu_table.base(ctx):
            raise StageError("选款表还没建")
        with feishu.Client(ctx) as api:
            return table_template.galleries(ctx, api)
    if args.table_cmd == "books":
        if args.lock:
            with feishu.Client(ctx) as api:
                locked = books.lock_template(ctx, api)
            return {"lock": locked, **books.status(ctx)}
        return books.status(ctx)
    if args.table_cmd == "reasons":
        _check_seed(args.seed)
        refreshed = (
            {"refresh": candidates.refresh_reasons(ctx, args.seed)}
            if args.refresh
            else {}
        )
        return {**reasons.summary(ctx, args.seed), **refreshed}
    raise StageError(f"未知子命令 {args.table_cmd}")


_ASSET_RE = re.compile(r"IMG\d+")
_SET_RE = re.compile(r"SS\d+")
# 飞书多维表格的 token / 数据表 id / 记录 id（网关从事件里转来的，只认字母数字）
_FEISHU_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")


def _check_seed(seed_id: str | None, *, required: bool = False) -> None:
    if (seed_id or required) and not re.fullmatch(r"S\d+", seed_id or ""):
        raise StageError(f"任务编号不对：{seed_id}（应当像 S01）")


def _cmd_packages(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """交付包：packages 列出、pack 打该打的、repack 重打一个、downloaded 标已下载（--undo 撤销）。"""
    if args.scenes_cmd == "packages":
        _check_seed(args.seed)
        return packages.overview(ctx, args.seed)
    if args.scenes_cmd == "downloaded":
        _check_seed(args.seed, required=True)
        if not re.fullmatch(r"B\d+", args.batch or ""):
            raise StageError(f"批次编号不对：{args.batch}（应当像 B010）")
        if args.undo:
            return packages.unmark_downloaded(ctx, args.seed, args.batch, by=args.by)
        out = packages.mark_downloaded(
            ctx, args.seed, args.batch, by=args.by, source=args.source
        )
        # 标完只对这个任务扫一次归档（开着 shelf.auto 时满足条件当场归档、发卡片）
        return {**out, **shelf.after_download(ctx, args.seed)}
    if args.scenes_cmd == "pack":
        _check_seed(args.seed)
        if args.detach:
            if not packages.enabled(ctx):
                raise StageError(
                    "交付包关着：scenes.enabled 与 scenes.batch_packages 都要打开"
                )
            if _running_task(ctx, "scenes", None):
                raise StageError("套图进程已经在跑（收尾时会打交付包）")
            return _spawn_self(ctx, kind="scenes", subject=None, notify_on_finish=False)
        return {"packages": packages.build_due(ctx, seed_id=args.seed)}
    _check_seed(args.seed, required=True)
    if not re.fullmatch(r"B\d+", args.batch or ""):
        raise StageError(f"批次编号不对：{args.batch}（应当像 B010）")
    return packages.repack(ctx, args.seed, args.batch)


def cmd_scenes(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.scenes_cmd in ("packages", "pack", "repack", "downloaded"):
        return _cmd_packages(ctx, args)
    if args.scenes_cmd == "start":
        if bool(args.seed) == bool(args.asset):
            raise StageError("--seed 和 --asset 二选一")
        if args.seed and not re.fullmatch(r"S\d+", args.seed):
            raise StageError(f"任务编号不对：{args.seed}（应当像 S01）")
        assets = [
            a.strip() for raw in args.asset or [] for a in raw.split(",") if a.strip()
        ]
        bad = [a for a in assets if not _ASSET_RE.fullmatch(a)]
        if bad:
            raise StageError(f"素材编号不对：{'、'.join(bad)}（应当像 IMG00012）")
        out = scenes.start(
            ctx,
            by=args.by,
            seed_id=args.seed,
            asset_ids=assets or None,
            force=args.force,
        )
        # 已归档的任务真的有款重出套图时调出来（出完会再自动归档）
        for seed_id in dict.fromkeys(q["seed_id"] for q in out.get("queued") or []):
            restored = shelf.touch(ctx, seed_id, reason="重出套图", by=args.by)
            if restored:
                out.setdefault("restored", []).append(restored)
        return out
    if args.scenes_cmd == "sync":
        if args.detach:
            if not scenes.enabled(ctx):
                raise StageError("scenes.enabled 为 false：套图没有打开")
            if _running_task(ctx, "scenes", None):
                raise StageError("套图进程已经在跑")
            return _spawn_self(ctx, kind="scenes", subject=None, notify_on_finish=False)
        return scenes.sync(ctx, max_minutes=args.max_minutes)
    if args.scenes_cmd == "status":
        return scenes.status(ctx, args.seed)
    if args.scenes_cmd == "refresh" and args.all:
        if args.set_id:
            raise StageError("--all 和套图编号二选一")
        _check_seed(args.seed)
        if args.detach:
            if _running_task(ctx, "scenes", None):
                raise StageError("套图进程已经在跑，等它跑完再重拉")
            return _spawn_self(ctx, kind="scenes", subject=None, notify_on_finish=False)
        return scenes.refresh_all(ctx, seed_id=args.seed)
    if args.scenes_cmd == "set-client":
        if bool(args.report) == bool(args.jobs):
            raise StageError("--report 和 --jobs 二选一")
        if args.report:
            jobs = json.loads(Path(args.report).read_text(encoding="utf-8")).get("jobs") or []
        else:
            jobs = [j for j in re.split(r"[,\s]+", args.jobs) if j]
        return scenes.set_clients(
            ctx, [int(j) for j in jobs], args.client, by=args.by, dry_run=args.dry_run
        )
    if not _SET_RE.fullmatch(args.set_id or ""):
        raise StageError(f"套图编号不对：{args.set_id}（应当像 SS0001）")
    if args.scenes_cmd == "show":
        return scenes.show(ctx, args.set_id)
    if args.scenes_cmd == "cancel":
        return scenes.cancel(ctx, args.set_id, by=args.by)
    if args.scenes_cmd == "refresh":
        return scenes.refresh(ctx, args.set_id)
    raise StageError(f"未知子命令 {args.scenes_cmd}")


def cmd_budget(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    info = budget.summary(ctx.db, ctx.cfg)
    info["prices"] = ctx.setting("prices")
    info["note"] = "预算与单价只能由人编辑 podflow.yaml；人设不得改动"
    return info


def cmd_pause(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "1")
        ctx.db.set_meta(conn, "pause_reason", args.reason or "未说明")
        log_event(conn, "control", "paused", {"reason": args.reason})
    return {
        "paused": True,
        "reason": args.reason,
        "note": "正在跑的 worker 会在当前步骤结束后停下",
    }


def cmd_resume(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    with ctx.db.tx() as conn:
        ctx.db.set_meta(conn, "paused", "0")
        ctx.db.del_meta(conn, "pause_reason")
        log_event(conn, "control", "resumed", {"by": args.by})
    result: dict[str, Any] = {"paused": False, "by": args.by}
    fix = fixes.start(ctx)
    if fix:
        result["fixes"] = fix
    if not fix.get("fix_started"):
        chosen = directions.start_runs(ctx)
        if chosen:
            result["directions"] = chosen
    return result


def cmd_trial(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.trial_cmd == "start":
        hours = float(args.hours or ctx.setting("trial.duration_hours", 24))
        start = utcnow()
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, "trial_started_at", iso(start))
            ctx.db.set_meta(
                conn, "trial_ends_at", iso(start + dt.timedelta(hours=hours))
            )
            ctx.db.set_meta(conn, "trial_by", args.by)
            log_event(conn, "control", "trial_started", {"hours": hours, "by": args.by})
    elif args.trial_cmd == "stop":
        with ctx.db.tx() as conn:
            ctx.db.set_meta(conn, "trial_ends_at", now_iso())
            log_event(conn, "control", "trial_stopped", {"by": args.by})
    return report.trial_window(ctx)


def cmd_reconcile(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    review_id = review.find_review(ctx, "call_reconcile", args.call_id)
    if not review_id:
        raise StageError(f"{args.call_id} 没有待对账记录")
    return review.decide(
        ctx,
        review_id,
        "charged" if args.charged else "not_charged",
        by=args.by,
        note=args.note,
    )


def cmd_requeue(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    return review.requeue_asset(
        ctx, args.asset_id, args.stage, by=args.by, note=args.note
    )


def cmd_report(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.report_cmd == "batch":
        return report.batch_report(ctx, args.batch_id)
    if args.report_cmd == "digest" or (
        args.report_cmd == "daily" and digest.enabled(ctx)
    ):
        # 精简通知时「日报」就是状态总结（人设收到「【日报】」消息时也走这里）
        view = digest.digest_view(ctx)
        if args.notify:
            view["notified"] = digest.send_digest(ctx, view=view)
        return view
    if args.report_cmd == "todo":
        view = digest.todo_view(ctx)
        if args.notify:
            view["notified"] = digest.send_todo(ctx, view=view, force=True)
        return view
    if args.report_cmd == "daily":
        data = report.daily(ctx, args.day)
        if args.notify:
            try:
                data["notified"] = notify.send_pod_card(
                    ctx,
                    notify.daily_card(ctx, args.day),
                    report.text_summary(report.snapshot(ctx)),
                )
            except notify.NotifyError as exc:
                data["notified"] = {"sent": False, "error": str(exc)}
        return data
    raise StageError(f"未知子命令 {args.report_cmd}")


def cmd_notify(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.text:
        return notify.send_text(ctx, args.text)
    return notify.send_pod_card(
        ctx, notify.dashboard_card(ctx), report.text_summary(report.snapshot(ctx))
    )


def cmd_tasks(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.family:
        return tasks.family_detail(ctx, args.family)
    view = "all" if args.all else "active"
    return {
        "tasks": tasks.summaries(ctx, view=view, query=args.search),
        "archived_count": tasks.archived_count(ctx),
        "hint": None
        if args.all
        else "已归档的任务不在这里：podctl shelf list [--search 关键字]，或 podctl tasks --all",
    }


def _shelf_item(task: dict[str, Any]) -> dict[str, Any]:
    """历史任务一条：给 POD 对话看的精简字段。"""
    info = task.get("shelf") or {}
    return {
        "seed_id": task["seed_id"],
        "family_id": task["family_id"],
        "label": f"{task['seed_id']} {task['name']}"
        if task.get("name")
        else task["seed_id"],
        "status": task["status_label"],
        "archived_at": info.get("archived_at"),
        "mode": info.get("mode_label"),
        "cycles": info.get("cycles"),
        "folder_state": info.get("folder_state"),
        "steps": " › ".join(s["text"] for s in task["steps"]),
        "counts": task.get("counts"),
        "packages": [
            {"batch_id": p["batch_id"], "ready": p["ready"], "download": p["url"]}
            for p in task.get("packages") or []
        ],
        "can_plan": task.get("can_plan"),
        "plan_reason": task.get("plan_reason"),
    }


def cmd_shelf(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.shelf_cmd == "list":
        items = tasks.summaries(ctx, view="archived", query=args.search)
        return {
            "count": len(items),
            "archived": [_shelf_item(t) for t in items],
            "hint": "调出：podctl shelf restore S03 --by <用户>；继续裂变直接 podctl plan --family F… --detach --notify（会自动调出）",
        }
    if args.shelf_cmd == "status":
        return shelf.status(ctx)
    if args.shelf_cmd == "sweep":
        return shelf.sweep(ctx, source="cli", dry_run=args.dry_run)
    _check_seed(args.seed_id, required=True)
    if args.shelf_cmd == "archive":
        return shelf.archive(
            ctx,
            args.seed_id,
            by=args.by,
            force=args.force,
            note=args.note,
            dry_run=args.dry_run,
        )
    if args.shelf_cmd == "restore":
        return shelf.restore(ctx, args.seed_id, by=args.by)
    raise StageError(f"未知子命令 {args.shelf_cmd}")


def cmd_listing(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.listing_cmd == "status":
        _check_seed(args.seed)
        assets = [
            a.strip() for raw in args.asset or [] for a in raw.split(",") if a.strip()
        ]
        bad = [a for a in assets if not _ASSET_RE.fullmatch(a)]
        if bad:
            raise StageError(f"素材编号不对：{'、'.join(bad)}（应当像 IMG00012）")
        return listing.status(
            ctx, seed_id=args.seed, asset_ids=assets or None, refresh_first=args.refresh
        )
    if args.listing_cmd == "refresh":
        return listing.refresh(ctx)
    if args.listing_cmd == "set":
        if not _ASSET_RE.fullmatch(args.asset_id or ""):
            raise StageError(f"素材编号不对：{args.asset_id}（应当像 IMG00012）")
        return listing.set_state(
            ctx,
            args.asset_id,
            args.state,
            by=args.by,
            date=args.date,
            note=args.note,
        )
    raise StageError(f"未知子命令 {args.listing_cmd}")


def _asset_list(values: list[str] | None) -> list[str]:
    assets = [a.strip() for raw in values or [] for a in raw.split(",") if a.strip()]
    bad = [a for a in assets if not _ASSET_RE.fullmatch(a)]
    if bad:
        raise StageError(f"素材编号不对：{'、'.join(bad)}（应当像 IMG00012）")
    return assets


def _temu_probe(ctx: Ctx) -> dict[str, Any]:
    """只读探一遍妙手：凭据、店铺列表、站点与标题上限、每家店的样板。"""
    out: dict[str, Any] = {}
    with miaoshou.Client(ctx) as api:
        for name, call in (
            ("site_options", api.site_options),
            ("item_options", api.item_options),
            ("shops", api.shops),
        ):
            try:
                data = call()
                out[name] = (
                    data
                    if name != "shops"
                    else [
                        {
                            k: s.get(k)
                            for k in ("shopId", "shopName", "platform", "site")
                        }
                        for s in data
                    ]
                )
            except miaoshou.MiaoshouError as exc:
                out[name] = {"error": str(exc), "kind": exc.kind, "code": exc.code}
        out["templates"] = []
        for store_ in stores.all_stores(ctx):
            entry: dict[str, Any] = {
                "store": store_.column,
                "template_detail_id": store_.template_detail_id,
            }
            try:
                tpl = temu.template(ctx, api, store_, refresh=True)
                entry.update(
                    ok=True,
                    cid=tpl.get("cid"),
                    images=len(tpl.get("imgUrls") or []),
                    skus=len(tpl.get("skuMap") or {}),
                    sites=temu.shop_sites(store_, tpl),
                    prices=temu.prices(tpl),
                )
            except (miaoshou.MiaoshouError, StageError) as exc:
                entry.update(ok=False, error=str(exc))
            out["templates"].append(entry)
    return out


def _old_titles(path: Path) -> list[str]:
    """旧标题表（.xlsx / .csv / .txt）里的标题：xlsx / csv 找表头叫 title / 标题 的那一列，txt 一行一个。"""
    if path.suffix.lower() == ".txt":
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if path.suffix.lower() == ".csv":
        import csv

        with path.open(encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))
    else:
        try:
            import openpyxl
        except ImportError as exc:
            raise StageError("读 xlsx 要 openpyxl；可以先另存成 csv") from exc
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [
            list(r)
            for sheet in book.worksheets
            for r in sheet.iter_rows(values_only=True)
        ]
    titles: list[str] = []
    column: int | None = None
    for row in rows:
        cells = [str(c).strip() if c is not None else "" for c in row]
        if column is None:
            for i, cell in enumerate(cells):
                if cell.lower() in ("title", "标题", "商品标题", "英文标题"):
                    column = i
            continue
        if column < len(cells) and cells[column]:
            titles.append(cells[column])
    return titles


def _crafted(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """temu listed crafted：精作款周表格的模板、锁、迁移与状态。"""
    sub = args.crafted_cmd
    if sub == "status":
        return crafted.status(ctx)
    if sub == "migrate":
        return crafted.migrate(ctx, dry_run=args.dry_run)
    if not crafted.enabled(ctx):
        raise StageError("精作款没打开（temu.listed.crafted.enabled，还要 temu.listed.source: mooya 和 table.wiki_space_id）")
    with feishu.Client(ctx) as api:
        if sub == "template":
            meta = crafted.ensure_template(ctx, api)
            return {
                "url": meta.get("url"),
                "root": crafted.root(ctx).get("url"),
                "locked_at": meta.get("locked_at"),
                "next": None
                if meta.get("locked_at")
                else "在模板「精作款模板」的「看图」里设好封面效果，再 podctl temu listed crafted lock",
            }
        if sub == "lock":
            return crafted.lock_template(ctx, api)
    raise StageError(f"不认识的子命令 {sub}")


def cmd_temu(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.temu_cmd
    if cmd == "status":
        _check_seed(args.seed)
        return {
            **temu.status(
                ctx, asset_ids=_asset_list(args.asset) or None, seed_id=args.seed
            ),
            "stores": stores.status(ctx),
            "listed": listed_table.status(ctx),
        }
    if cmd == "sync":
        return temu.sync(ctx)
    if cmd == "probe":
        return _temu_probe(ctx)
    if cmd == "template":
        refs = [args.store] if args.store else [s.key for s in stores.all_stores(ctx)]
        out = []
        with miaoshou.Client(ctx) as api:
            for ref in refs:
                store_ = stores.resolve(ctx, ref)
                tpl = temu.template(ctx, api, store_, refresh=args.refresh)
                out.append(
                    {
                        "store": store_.column,
                        "template_detail_id": store_.template_detail_id,
                        "sites": temu.shop_sites(store_, tpl),
                        "prices": temu.prices(tpl),
                        "images": len(tpl.get("imgUrls") or []),
                        **({"info": tpl} if args.full else {}),
                    }
                )
        return {"templates": out}
    if cmd == "store":
        assets = _asset_list(args.asset_ids)
        if not assets:
            raise StageError("写上素材编号，如 IMG00012")
        refs = (
            [r for raw in args.set.split(",") for r in [raw.strip()] if r]
            if args.set is not None
            else []
        )
        results = [stores.set_stores(ctx, a, refs, by=args.by) for a in assets]
        temu.write_now(ctx, assets)
        feishu_table.kick(ctx)
        return {"results": results}
    if cmd == "codes":
        if args.retry:
            reset = stores.retry_codes(ctx)
            return {"reset": reset, **stores.allocate_codes(ctx)}
        return stores.allocate_codes(ctx, asset_ids=_asset_list(args.asset) or None)
    if cmd == "preview":
        if not _ASSET_RE.fullmatch(args.asset_id or ""):
            raise StageError(f"素材编号不对：{args.asset_id}（应当像 IMG00012）")
        return temu.preview(ctx, args.asset_id, args.store)
    if cmd in ("confirm", "cancel", "retry", "hold", "release"):
        assets = _asset_list(args.asset_ids)
        if not assets:
            raise StageError("写上素材编号，如 IMG00012")
        if cmd == "confirm":
            result = temu.confirm(ctx, assets, by=args.by, source="chat")
        elif cmd == "cancel":
            result = temu.cancel(ctx, assets, by=args.by)
        elif cmd == "hold":
            result = {**temu.cancel(ctx, assets, by=args.by), "held": temu.hold(ctx, assets, by=args.by, source="chat")}
        elif cmd == "release":
            result = {"released": temu.release(ctx, assets, by=args.by, source="chat")}
            result.update(temu.auto_confirm(ctx, assets))
        else:
            result = temu.retry(ctx, assets, by=args.by)
        result.update(temu.write_now(ctx, assets))
        if cmd in ("confirm", "release"):
            # 进了采集箱暂停着的解除后接着发布
            temu.kick(ctx)
        return result
    if cmd == "titles-sync":
        return {"items": title_sync.status(ctx, args.asset), "cell": title_sync.cell_text(ctx, args.asset) if args.asset else None}
    if cmd == "open":
        if args.open_cmd == "probe":
            return temu_open.probe(ctx)
        if args.open_cmd == "sync":
            return temu_open.sync(ctx)
        return {
            "enabled": temu_open.enabled(ctx),
            "goods": [dict(r) for r in ctx.db.q("SELECT code, store_key, goods_id, status, goods_name, checked_at, last_error FROM temu_goods ORDER BY code")],
            "checks": temu_open.doctor_checks(ctx),
        }
    if cmd == "title":
        if not _ASSET_RE.fullmatch(args.asset_id or ""):
            raise StageError(f"素材编号不对：{args.asset_id}（应当像 IMG00012）")
        custom = None if args.custom is None else args.custom == "yes"
        if args.set:
            # 和表里改一样：复核、记版本，按每家店的环节同步（采集箱里的存回妙手，已上架的走 Temu 接口）；
            # 带 --custom 时标题和定制款判定一起改（先改哪个都会卡住的死锁，2026-10-30 起一起改）
            synced = title_sync.handle_edit(ctx, args.asset_id, args.set, by=args.by, via="chat", custom=custom)
            if synced.get("rejected"):
                raise StageError(synced["rejected"])
            if synced.get("same") and custom is not None:
                temu_title.set_custom(ctx, args.asset_id, custom, by=args.by)
            saved = temu_title.get(ctx, args.asset_id) or {}
            return {"title": saved, "sync": synced, **temu.write_now(ctx, [args.asset_id])}
        elif args.regenerate:
            from . import temu_assets

            item = temu_assets.current(ctx, args.asset_id) or temu_assets.prepare(
                ctx, args.asset_id
            )
            saved = temu_title.generate(
                ctx, args.asset_id, temu_assets.first_image(ctx, item)
            )
        else:
            saved = temu_title.get(ctx, args.asset_id) or {}
        if custom is not None:
            saved = temu_title.set_custom(ctx, args.asset_id, custom, by=args.by)
        return {"title": saved, **temu.write_now(ctx, [args.asset_id])}
    if cmd == "titles":
        titles = _old_titles(Path(args.file))
        checked = [
            {
                "title": t,
                "problems": temu_title.check_title(
                    t, is_custom=t.lower().startswith("custom")
                ),
            }
            for t in titles
        ]
        bad = [c for c in checked if c["problems"]]
        return {
            "read": len(titles),
            "ok": len(titles) - len(bad),
            "problems": bad[: args.limit],
        }
    if cmd == "listed":
        sub = args.listed_cmd
        if sub == "status":
            return listed_table.status(ctx)
        if sub == "sync":
            return listed_table.sync_now(ctx, batch=args.batch)
        if sub == "crafted":
            return _crafted(ctx, args)
        if sub == "read":
            return listed_table.read_back(ctx)
        if sub == "check":
            return listed_table.check(ctx)
        if sub == "backfill":
            return listed_table.backfill(ctx)
        if sub == "export":
            return listed_table.export_csv(ctx, Path(args.csv) if args.csv else None)
        if sub == "pull":
            return listed_table.pull(ctx)
    if cmd == "export-to-mooya":
        from . import mooya_listing  # 搬单要读上架的很多表，用时再导入

        if not lease.take(ctx, temu.LEASE_KEY, temu.LEASE_SECONDS):
            raise StageError("上架后台正在跑（temu sync 持有租约），等它这一轮结束再搬")
        try:
            return mooya_listing.export(ctx, codes=args.code, listed_only=args.listed_only, dry_run=args.dry_run)
        finally:
            lease.drop(ctx, temu.LEASE_KEY)
    raise StageError(f"未知子命令 {cmd}")


def cmd_ui(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.ui_cmd == "dashboard":
        return ui.dashboard(ctx)
    if args.ui_cmd == "tasks":
        return ui.tasks_view(ctx, offset=args.offset, limit=args.limit)
    if args.ui_cmd == "reviews":
        return ui.reviews_page(
            ctx,
            offset=args.offset,
            limit=args.limit,
            types=args.type,
            batch_id=args.batch,
        )
    if args.ui_cmd == "daily":
        # 工作台的「日报」按钮：精简通知时给状态总结（渠道只画标题和行）
        if digest.enabled(ctx):
            return digest.digest_view(ctx)
        return ui.daily_view(ctx, args.day)
    if args.ui_cmd == "timeline":
        return ui.timeline(ctx, limit=args.limit)
    if args.ui_cmd == "gallery":
        return ui.gallery(
            ctx, batch_id=args.batch, offset=args.offset, limit=args.limit
        )
    if args.ui_cmd == "originals":
        return ui.originals(ctx, args.asset)
    if args.ui_cmd == "directions":
        return ui.directions_view(ctx, args.batch)
    if args.ui_cmd == "history":
        return ui.history_view(ctx, args.family, offset=args.offset)
    if args.ui_cmd == "shelf":
        return ui.shelf_view(
            ctx, offset=args.offset, limit=args.limit, query=args.query
        )
    raise StageError(f"未知子命令 {args.ui_cmd}")


def cmd_maintenance(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.maintenance_cmd == "retire-dedup":
        return maintenance.retire_dedup(ctx, by=args.by)
    if args.maintenance_cmd == "restore-rejected":
        return maintenance.restore_rejected(
            ctx, batches=args.batch, by=args.by, dry_run=args.dry_run
        )
    if args.maintenance_cmd == "dismiss-selection":
        return maintenance.dismiss_selection(
            ctx, batches=args.batch, by=args.by, note=args.note, dry_run=args.dry_run
        )
    if args.maintenance_cmd == "relabel-assets":
        return maintenance.relabel_assets(ctx, by=args.by, dry_run=args.dry_run)
    if args.maintenance_cmd == "import-downloads":
        return downloads.import_marks(
            ctx,
            column=args.column,
            option=args.option,
            by=args.by,
            dry_run=args.dry_run,
        )
    if args.maintenance_cmd == "drop-scene-images":
        return maintenance.drop_scene_images(ctx, by=args.by, dry_run=args.dry_run)
    if args.maintenance_cmd == "migrate-week":
        if args.dry_run:
            return week_migration.plan(ctx, args.week)
        return week_migration.run(
            ctx,
            args.week,
            by=args.by,
            backup_dir=args.backup_dir,
            keep_columns=args.keep_columns,
        )
    if args.maintenance_cmd == "purge-old":
        return maintenance.purge_old(
            ctx,
            family_id=args.family,
            keep_version=args.keep_version,
            seeds=args.seed,
            by=args.by,
            dry_run=args.dry_run,
        )
    raise StageError(f"未知子命令 {args.maintenance_cmd}")


def cmd_log(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    logs = sorted((ctx.home / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return {"logs": []}
    target = logs[-1]
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    events = [
        dict(r)
        for r in ctx.db.q(
            "SELECT ts, scope, event, payload_json FROM events ORDER BY event_id DESC LIMIT ?",
            (args.events,),
        )
    ]
    return {
        "log": str(target.relative_to(ctx.home)),
        "tail": lines[-args.lines :],
        "recent_events": events,
    }


# ------------------------------------------------------------------ 参数

_NOTIFY_HELP = "与 --detach 一起用：后台跑完（或失败）发一张飞书卡片"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="podctl", description="POD 毛毯设计流水线（DeerFlow skill pod-blanket）"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据目录、配置与密钥模板")
    d = sub.add_parser("doctor", help="开跑前自检")
    d.add_argument(
        "--probe",
        action="store_true",
        help="在预算内发一次极小的文本调用验证 key 与模型",
    )
    sub.add_parser("status", help="状态快照")
    tk = sub.add_parser(
        "tasks",
        help="任务进度：一张参考图一个任务，走到哪一步（拆解 / 策划 / 挑选 / 出图 / 选款 / 套图 / 上架）；"
        "已归档的不列（podctl shelf list）",
    )
    tk.add_argument("--family", help="看这个系列的明细：每一批、每个方向细到哪一步")
    tk.add_argument("--all", action="store_true", help="连已归档的任务一起列")
    tk.add_argument(
        "--search", help="按编号、名字、日期、参考图描述搜，如「恐龙」「1020」"
    )
    shp = sub.add_parser(
        "shelf",
        help="任务归档（历史任务）：做完的任务从任务列表收起、候选表挪进「已归档」文件夹，随时调出继续裂变",
    )
    shs = shp.add_subparsers(dest="shelf_cmd", required=True)
    shl = shs.add_parser("list", help="已归档的任务（新归档的在前）")
    shl.add_argument("--search", help="按编号、名字、日期、参考图描述搜")
    sha = shs.add_parser(
        "archive",
        help="归档一个任务：有在做的事时拒绝；还有待选时要 --force（剩下的待选清出选款，不算不要）",
    )
    sha.add_argument("seed_id")
    sha.add_argument("--by", required=True)
    sha.add_argument(
        "--force", action="store_true", help="还有待选也归档：剩下的待选清出选款"
    )
    sha.add_argument("--note", help="记一句为什么归档")
    sha.add_argument(
        "--dry-run", action="store_true", help="只看能不能归档、会清出几张待选"
    )
    shr = shs.add_parser(
        "restore", help="调出一个已归档的任务（回到任务列表，候选表挪回根目录）"
    )
    shr.add_argument("seed_id")
    shr.add_argument("--by", required=True)
    shw = shs.add_parser(
        "sweep",
        help="现在扫一遍：有新动静的历史任务调出；开着 shelf.auto 时做完的任务归档（巡检和套图出完时会自动扫）",
    )
    shw.add_argument(
        "--dry-run",
        action="store_true",
        help="只列能自动归档的任务和其余任务不能归档的原因",
    )
    shs.add_parser(
        "status", help="归档开关、「已归档」文件夹、已归档几个、候选表还要挪几张"
    )
    lsp = sub.add_parser(
        "listing",
        help="上架状态：选中的款在「选中的款」表里标的待上架 / 已上架 / 不上了（人填、程序只读）",
    )
    lss = lsp.add_subparsers(dest="listing_cmd", required=True)
    lst = lss.add_parser("status", help="选中的款上没上架（每款一条 + 各状态款数）")
    lst.add_argument("--seed", help="只看这个任务的")
    lst.add_argument(
        "--asset", action="append", help="只看这几款（IMG 编号），可重复写或用逗号分隔"
    )
    lst.add_argument("--refresh", action="store_true", help="先从表格回读一次（几秒）")
    lss.add_parser(
        "refresh", help="现在从「选中的款」回读上架状态（巡检每 10 分钟会自动读）"
    )
    lse = lss.add_parser("set", help="改一款的上架状态（写进「选中的款」那一格）")
    lse.add_argument("asset_id")
    lse.add_argument("--state", required=True, choices=list(listing.STATES))
    lse.add_argument("--date", help="上架日期，如 2026-10-25；不写就不改")
    lse.add_argument(
        "--note", help="上架备注（≤200 字），如店铺货号、商品链接；不写就不改"
    )
    lse.add_argument("--by", required=True)

    tm = sub.add_parser(
        "temu",
        help="Temu 半托上架（妙手开放平台）：店铺与货号、上架图与标题、建箱发布、已上架的款",
    )
    tms = tm.add_subparsers(dest="temu_cmd", required=True)
    tmst = tms.add_parser(
        "status", help="各款各店上到哪一步（含备注）、店铺与货号、已上架的款"
    )
    tmst.add_argument("--seed", help="只看这个任务的")
    tmst.add_argument(
        "--asset", action="append", help="只看这几款，可重复写或用逗号分隔"
    )
    tms.add_parser(
        "sync", help="现在推进一轮（准备素材、建箱、发布、查结果；巡检会自动拉起）"
    )
    tms.add_parser(
        "probe", help="只读探一遍妙手：凭据、店铺、站点、标题上限、每家店的样板"
    )
    tmt = tms.add_parser("template", help="看样板商品（站点、尺码价格、轮播图张数）")
    tmt.add_argument("--store", help="哪家店（3店 / 店名 / key），不写看全部")
    tmt.add_argument("--refresh", action="store_true", help="不用缓存，重读妙手")
    tmt.add_argument("--full", action="store_true", help="带上样板原样的内容")
    tmo = tms.add_parser(
        "store", help="改款的店铺（加上没有的、去掉不要的；在上 / 上过的去不掉）"
    )
    tmo.add_argument("asset_ids", nargs="+")
    tmo.add_argument(
        "--set",
        required=True,
        help="店铺列表，逗号分隔，如 3店,4店；写空字符串去掉全部",
    )
    tmo.add_argument("--by", required=True)
    tmc = tms.add_parser("codes", help="没领到货号的现在领（mooya 按店铺前缀续号）")
    tmc.add_argument("--asset", action="append", help="只领这几款的")
    tmc.add_argument("--retry", action="store_true", help="失败满上限的清零再领")
    tmp = tms.add_parser(
        "preview", help="不建箱：看这一款会用的图、标题、货号和要发给妙手的内容"
    )
    tmp.add_argument("asset_id")
    tmp.add_argument("--store")
    for name, text in (
        ("confirm", "确认上架（和在「选中的款」勾「确认上架」一样；暂停着的接着上）"),
        ("cancel", "取消还没开始建箱的确认（开着全自动时同时暂停这一款）"),
        ("retry", "没上成的重来（素材 / 标题失败次数清零）"),
        ("hold", "暂停这几款（全自动不再替你确认上架；还没建箱的取消确认）"),
        ("release", "恢复暂停的款（全自动接着确认上架）"),
    ):
        tmx = tms.add_parser(name, help=text)
        tmx.add_argument("asset_ids", nargs="+")
        tmx.add_argument("--by", required=True)
    tmts = tms.add_parser("titles-sync", help="改了的上架标题同步到哪了（每款每店）")
    tmts.add_argument("--asset", help="只看这一款")
    tmop = tms.add_parser("open", help="Temu 官方 Partner 接口（temu_open）：probe 只读探测 / sync 推进一轮 / status")
    tmop.add_argument("open_cmd", choices=["probe", "sync", "status"])
    tmi = tms.add_parser("title", help="看 / 改 / 重写一款的上架标题")
    tmi.add_argument("asset_id")
    tmi.add_argument("--set", help="改成这个标题（按规则复核，不合规报错）")
    tmi.add_argument("--regenerate", action="store_true", help="让模型重写一遍")
    tmi.add_argument(
        "--custom", choices=["yes", "no"], help="改定制款判定（定制款不自动上）"
    )
    tmi.add_argument("--by", default="chat")
    tmh = tms.add_parser(
        "titles",
        help="拿旧标题表按新规则复核（xlsx / csv 找 title / 标题 列，txt 一行一个）",
    )
    tmh.add_argument("file")
    tmh.add_argument("--limit", type=int, default=50)
    tml = tms.add_parser(
        "listed", help="已上架的款（业务库 listed_products ↔ 飞书「已上架的款」）"
    )
    tmls = tml.add_subparsers(dest="listed_cmd", required=True)
    tmls.add_parser("status", help="几条、各店几条、还要写几条、上次回读时间")
    tmlsy = tmls.add_parser("sync", help="现在把库里的写进飞书（建表、建行、改程序列）")
    tmlsy.add_argument("--batch", type=int, help="这一次最多写几行（缺省 temu.listed.sync_batch；第一次补上千款时调大）")
    tmlc = tmls.add_parser(
        "crafted", help="精作款周表格（temu.listed.crafted：mooya 上做的 1店 / 2店按周一本、每天一张表）"
    )
    tmlcs = tmlc.add_subparsers(dest="crafted_cmd", required=True)
    tmlcs.add_parser("status", help="模板、各周表格、各天、写了几条、还有几条没迁")
    tmlcs.add_parser("template", help="建「精作款」节点和模板「精作款模板（勿改）」（建好后在「看图」里设封面效果）")
    tmlcs.add_parser("lock", help="设好封面效果后锁模板（编辑者只读）；锁上之后精作款的行才开始写")
    tmlcm = tmlcs.add_parser(
        "migrate",
        help="把精作款从「已上架的款」挪过来：删那边的行、旧表格那张只剩精作款时删表、删周表格里精作款店铺的视图，建好各周各天",
    )
    tmlcm.add_argument("--dry-run", action="store_true", help="只看要删多少行、要建哪几周哪几天（不写飞书）")
    tmls.add_parser("read", help="现在回读飞书里人填的列（巡检会定时读）")
    tmls.add_parser("check", help="核对上架记录、业务库和飞书行")
    tmls.add_parser("backfill", help="上了却没进库的补记（读 mooya 底账时再把没推过去的推给 mooya）")
    tmle = tmls.add_parser("export", help="导出 CSV（统计分析用）")
    tmle.add_argument("--csv", help="写到哪（缺省 exports/listed-时间.csv）")
    tmls.add_parser("pull", help="现在拉一次 mooya 的四家店底账（temu.listed.source: mooya；巡检会定时拉）")
    tmxm = tms.add_parser(
        "export-to-mooya",
        help="切换到 mooya 上架时把本机进了妙手的（建箱中 … 已上架）原样搬进 mooya（同一 ref 不重复建）",
    )
    tmxm.add_argument("--code", action="append", help="只搬这几个货号（可多次）")
    tmxm.add_argument("--listed-only", action="store_true", help="只搬已上架的（连底账）")
    tmxm.add_argument("--dry-run", action="store_true", help="只列出要搬的")

    s = sub.add_parser("seed", help="参考图（母版）")
    ss = s.add_subparsers(dest="seed_cmd", required=True)
    sa = ss.add_parser("add")
    sa.add_argument("path", nargs="?")
    sa.add_argument("--from-inbox")
    sa.add_argument("--source-ref")
    sa.add_argument("--note")
    sa.add_argument("--rights", default="unknown", choices=seedmod.RIGHTS)
    sa.add_argument("--sales-evidence")
    sa.add_argument("--positioning")
    ss.add_parser("list")
    sh = ss.add_parser("show")
    sh.add_argument("seed_id")
    sh.add_argument("--full", action="store_true", help="输出全部在用字段")
    sr = ss.add_parser("rights")
    sr.add_argument("seed_id")
    sr.add_argument("--status", required=True, choices=seedmod.RIGHTS)
    sr.add_argument("--by", required=True)
    sr.add_argument("--note")
    sx = ss.add_parser(
        "extract",
        help="重新提取这张参考图的平面图案（S01P，2D 化）：之后的图生图和质检用新图案，不重做拆解",
    )
    sx.add_argument("seed_id")
    sx.add_argument(
        "--hint",
        help="这次的补充要求（≤200 字），如「保留文字」「底色保持米白」；以「-」开头时写成 --hint=…",
    )
    sx.add_argument("--detach", action="store_true")
    sx.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)
    sn = ss.add_parser(
        "name",
        help="给任务起名（S01N）：不带 --set 时模型重新看图写描述；名字 = 描述 + 空格 + 这张图登记那天的月日",
    )
    sn.add_argument("seed_id")
    sn.add_argument(
        "--set",
        dest="description",
        help="人定的描述（2–10 个字，标点和空格会去掉），日期照旧；以「-」开头时写成 --set=…",
    )
    sn.add_argument("--by", help="谁改的名（写 --set 时记下来）")
    sd = ss.add_parser(
        "describe",
        help="画面完整描述（S02D）：给参考图写画面档案（画风、每个主体、环境、配色、画风统一规则），"
        "策划、出图指令和质检都照它写；不重做拆解和系列",
    )
    sd.add_argument("seed_id", nargs="?")
    sd.add_argument(
        "--all",
        action="store_true",
        help="所有拆解过的参考图里还没有描述的都补上（配 --force 全部重写）",
    )
    sd.add_argument("--force", action="store_true", help="已有描述也重写")
    sd.add_argument(
        "--hint",
        help="补充或更正（≤200 字），如「左边那只是甲龙不是剑龙」；写了就按它重写；以「-」开头时写成 --hint=…",
    )
    sd.add_argument(
        "--show", action="store_true", help="只看现在的画面描述，不调用模型"
    )
    sd.add_argument("--detach", action="store_true")
    sd.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)
    sz = ss.add_parser(
        "analyze", help="S01→S04（含图案提取 S01P、画面描述 S02D；同时给任务起名）"
    )
    sz.add_argument("seed_id")
    sz.add_argument(
        "--force", action="store_true", help="S01–S04 全部重跑（S04 建新系列）"
    )
    sz.add_argument(
        "--reanalyze",
        action="store_true",
        help="按当前字段字典重新拆解：只重跑 S02、S03，S04 修订这张图已有的系列（方向记录接着算），然后策划",
    )
    sz.add_argument("--positioning")
    sz.add_argument("--detach", action="store_true")
    sz.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)

    r = sub.add_parser("run", help="单独跑一个母版阶段")
    r.add_argument(
        "stage", choices=["S01", "S02", "S03", "S04", "s01", "s02", "s03", "s04"]
    )
    r.add_argument("--seed", required=True)
    r.add_argument("--positioning")

    f = sub.add_parser("family", help="系列规则")
    fs = f.add_subparsers(dest="family_cmd", required=True)
    fs.add_parser("list")
    fsh = fs.add_parser("show")
    fsh.add_argument("family_id")
    fsh.add_argument("--version", type=int)
    for name in ("approve", "reject"):
        fa = fs.add_parser(name)
        fa.add_argument("family_id")
        fa.add_argument("--by", required=True)
        fa.add_argument("--note")
    fr = fs.add_parser("revise", help="按修改意见重跑 S04，生成新的待批准版本")
    fr.add_argument("family_id")
    fr.add_argument("--note", required=True)
    fr.add_argument(
        "--kinds",
        help="修订后自动策划时只策划这几类，逗号分隔：small / big / combo；默认全部",
    )
    fr.add_argument("--detach", action="store_true")
    fr.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)

    pl = sub.add_parser(
        "plan",
        help="S05 裂变策划：程序随机挑改动出一轮候选方向（小变化各改两处：一处 = 一个可变单元上的一个字段，或整幅的一项；大变化改一个主题 / 构图 / 技法字段，或三处），人挑选后才出图；开着 batch.auto_confirm_directions 时全部自动确认、直接出图",
    )
    pl.add_argument(
        "--family",
        help="以原图为底策划这个系列（不用 --derive-from / --derive-due 时必填）",
    )
    pl.add_argument(
        "--derive-from",
        dest="derive_from",
        metavar="B",
        help="二次大变化：以这个批次里换了主体的小变化出的图为底图，各策划一个单字段大改",
    )
    pl.add_argument(
        "--derive-due",
        dest="derive_due",
        action="store_true",
        help="把所有该自动派生二次大变化的批次逐个派生（巡检 / worker 拉起的后台用）",
    )
    pl.add_argument(
        "--parents",
        help="和 --derive-from 一起用：只拿这几个小变化当底图（V 编号，逗号分隔）",
    )
    pl.add_argument(
        "--count",
        type=int,
        help="和 --derive-from 一起用：最多几个二次大变化（默认 plan.derived.count）",
    )
    pl.add_argument(
        "--limit",
        type=int,
        help="只策划前 n 个槽位（按 小变化 → 单字段大改 → 三处组合 的顺序）；默认全部",
    )
    pl.add_argument(
        "--preview",
        action="store_true",
        help="只看这一轮会挑出哪些改动（可变单元、每个槽位改哪几处；和 --derive-from 一起用时看挑哪些底图），不调模型、不建批次",
    )
    pl.add_argument("--by", default="manual", help="手动派生二次大变化时记的发起人")
    pl.add_argument(
        "--hint",
        help="这一轮想要的方向（≤100 字），如「换成海洋动物」；不写就按系列规则",
    )
    pl.add_argument(
        "--kinds",
        help="只策划这几类槽位，逗号分隔：small（小变化）/ big（单字段大改）/ combo（三处组合）；默认全部",
    )
    pl.add_argument("--detach", action="store_true")
    pl.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)

    dr = sub.add_parser(
        "directions", help="裂变方向：看候选、勾选、确认出图、换一批、方向记录"
    )
    drs = dr.add_subparsers(dest="directions_cmd", required=True)
    dsh = drs.add_parser("show")
    dsh.add_argument("batch_id")
    dhi = drs.add_parser("history", help="这个系列做过和提过的全部方向与状态")
    dhi.add_argument("family_id")
    dpk = drs.add_parser(
        "pick", help="勾选 / 取消一个方向（序号或 V 编号；all = 全部）"
    )
    dpk.add_argument("batch_id")
    dpk.add_argument(
        "direction", help="序号、V 编号，或 all（全部勾上，配 --off 全部取消）"
    )
    dpk.add_argument("--off", action="store_true", help="取消勾选")
    dcf = drs.add_parser("confirm", help="确认方向：选中的建任务出图，其余记人工没选")
    dcf.add_argument("batch_id")
    dcf.add_argument(
        "--pick", help="序号或 V 编号，用逗号分隔，all = 全部；不填就用已勾选的"
    )
    dcf.add_argument("--by", required=True)
    dcf.add_argument(
        "--start",
        action="store_true",
        help="确认后在后台出图，一直出到这些方向全部出完（完成发卡片）",
    )
    dcn = drs.add_parser(
        "cancel", help="停掉确认过、还没开始出图的方向（任务删掉，方向记已停掉）"
    )
    dcn.add_argument("batch_id")
    dcn.add_argument(
        "--pick", help="序号或 V 编号，逗号分隔，all = 全部；不写就是全部选中的"
    )
    dcn.add_argument("--kind", help="只停这几类：small / big / combo，逗号分隔")
    dcn.add_argument("--by", required=True)
    drj = drs.add_parser("reject", help="这一批都不要")
    drj.add_argument("batch_id")
    drj.add_argument("--by", required=True)
    drj.add_argument("--replan", action="store_true", help="在后台马上再策划一批")
    drj.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)

    w = sub.add_parser(
        "worker",
        help="出图 worker：多条工位同时推进 S06–S10，有活一直跑，没活或要交接时退出",
    )
    w.add_argument("--batch", type=int, help="这次最多领几个任务（缺省有活一直领）")
    w.add_argument(
        "--max-minutes",
        type=float,
        help="跑满这么久就交接给新的 worker（缺省 batch.worker_lifetime_minutes）",
    )
    w.add_argument("--lanes", type=int, help="工位数（缺省 batch.workers）")
    w.add_argument("--no-generation", action="store_true")
    w.add_argument("--no-plan", action="store_true")
    w.add_argument("--detach", action="store_true")
    w.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)
    w.add_argument("--source", default="manual")
    w.add_argument(
        "--job",
        action="append",
        help="只推进这些任务（手动指定用）；不写时按 --source 决定领什么：directions / fix 只领确认过的方向和修改",
    )

    t = sub.add_parser("tick", help="定时巡检入口（P00）")
    t.add_argument("--no-spawn", action="store_true", help="只判断不拉起 worker")
    t.add_argument("--notify", action="store_true", help="有异常时发飞书")
    t.add_argument(
        "--auto",
        action="store_true",
        help="网关定时器用：按 schedule 配置到点才巡检（带通知），并按时发日报",
    )

    rv = sub.add_parser("review", help="人工审核")
    rvs = rv.add_subparsers(dest="review_cmd", required=True)
    rl = rvs.add_parser("list")
    rl.add_argument("--type", action="append")
    rl.add_argument("--batch")
    rl.add_argument("--all", action="store_true")
    rd = rvs.add_parser("decide")
    rd.add_argument("review_id")
    rd.add_argument("--decision", required=True)
    rd.add_argument("--by", required=True)
    rd.add_argument("--note")
    rsh = rvs.add_parser("sheet", help="选款拼图")
    rsh.add_argument("--out", required=True)
    rsh.add_argument("--batch")
    rsh.add_argument("--type", default="asset_selection")
    rsh.add_argument("--assets", nargs="*")
    rex = rvs.add_parser("export", help="导出 xlsx 选款单")
    rex.add_argument("--xlsx", required=True)
    rex.add_argument("--batch")
    rim = rvs.add_parser("import", help="导入填好的 xlsx 选款单")
    rim.add_argument("--xlsx", required=True)
    rim.add_argument("--by", required=True)

    a = sub.add_parser("asset", help="素材详情（含流水线现在在哪一环）；--timeline 看从出图到上架的完整时间线")
    a.add_argument("asset_id")
    a.add_argument(
        "--export", help="把原图复制到这个路径（如 /mnt/user-data/outputs/x.png）"
    )
    a.add_argument("--timeline", action="store_true", help="完整时间线：轨迹、事件、审核、调用、人改格子、各表时间点")
    a.add_argument("--limit", type=int, default=400, help="时间线最多几条（最新的）")

    j = sub.add_parser("job", help="任务")
    js = j.add_subparsers(dest="job_cmd", required=True)
    jl = js.add_parser("list")
    jl.add_argument("--batch")
    jsh = js.add_parser("show")
    jsh.add_argument("job_id")
    jrd = js.add_parser(
        "redo",
        help="方向按新规则重出：作废这个任务已出的图（选中的款不能作废），重新写生成指令、重新出图",
    )
    jrd.add_argument("job_ids", nargs="+", help="任务 J 编号或方向 V 编号，可以写多个")
    jrd.add_argument("--by", required=True)
    jrd.add_argument(
        "--dry-run", action="store_true", help="只列要作废的图和待审，不改"
    )

    sub.add_parser("budget", help="预算与账本（只读）")
    dbp = sub.add_parser("db", help="业务库：后端与行数 / 从 SQLite 迁到 Postgres")
    dbs = dbp.add_subparsers(dest="db_cmd", required=True)
    dbs.add_parser("info")
    dbm = dbs.add_parser(
        "migrate-from-sqlite", help="把 SQLite 业务库整库复制进当前（空的）Postgres 库"
    )
    dbm.add_argument("--path", help="SQLite 文件，默认数据目录下的 podflow.db")
    stp = sub.add_parser("storage", help="素材存储（OSS）：状态 / 补传 / 清理本机缓存")
    sts = stp.add_subparsers(dest="storage_cmd", required=True)
    sts.add_parser("status")
    ssy = sts.add_parser("sync", help="上传所有待上传的素材")
    ssy.add_argument(
        "--all",
        action="store_true",
        help="先把 assets/ 下没登记的本机文件全部登记（迁移用）",
    )
    ssy.add_argument(
        "--retry", action="store_true", help="把失败次数清零，重新尝试卡住的文件"
    )
    spr = sts.add_parser("prune", help="删掉已在 OSS 上、超过 N 天没动的本机副本")
    spr.add_argument("--older-than-days", type=float, default=30)
    spr.add_argument("--dry-run", action="store_true")
    tbp = sub.add_parser(
        "table", help="选款表（飞书多维表格）：建表 / 同步选中的款 / 状态"
    )
    tbs = tbp.add_subparsers(dest="table_cmd", required=True)
    tbi = tbs.add_parser("init", help="建表并加协作者（第一次同步时也会自动建）")
    tbi.add_argument(
        "--reset", action="store_true", help="另建一张新表并重新同步（旧表不删）"
    )
    tbsy = tbs.add_parser(
        "sync", help="把选中的款同步进表格：新增、删除、补 / 续下载链接"
    )
    tbsy.add_argument(
        "--retry", action="store_true", help="把失败次数清零，重新尝试卡住的行"
    )
    tbsy.add_argument("--detach", action="store_true")
    tba = tbs.add_parser(
        "accept",
        help="选款完成：确认过的方向全部出完图后，回读候选表的勾选（勾上的算选中、没勾的算不要，"
        "刚进表的留到下次），选中的款接着出套图；没出完时报错，先用 table dispatch",
    )
    tba.add_argument("--seed", required=True, help="任务（参考图）编号，如 S01")
    tba.add_argument("--by", required=True)
    tba.add_argument("--detach", action="store_true")
    tba.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)
    tba.add_argument(
        "--source",
        choices=["table"],
        help="从哪提交的：table = 候选表最上面的「▶ 选款完成」按钮行（卡片上写明）",
    )
    tbd = tbs.add_parser(
        "dispatch",
        help="选中的先出套图：回读候选表的勾选，勾上的、出了图的款定为选中并交给 mooya 出套图；"
        "没勾的不动，不算选款完成，图没出完也能用",
    )
    tbd.add_argument("--seed", required=True, help="任务（参考图）编号，如 S01")
    tbd.add_argument("--by", required=True)
    tbd.add_argument("--detach", action="store_true")
    tbd.add_argument("--notify", action="store_true", help=_NOTIFY_HELP)
    tbd.add_argument(
        "--source",
        choices=["table"],
        help="从哪提交的：table = 候选表最上面的「▶ 选中的先出套图」按钮行（卡片上写明）",
    )
    tbc = tbs.add_parser(
        "control",
        help="多维表格记录变更事件（网关转来）：候选表表顶的按钮行 / 交付包行勾上了，"
        "就拉起提前出套图 / 选款完成，或标交付包已下载",
    )
    tbc.add_argument(
        "--file", required=True, help="多维表格 token（事件里的 file_token）"
    )
    tbc.add_argument("--table", required=True, help="数据表 id（事件里的 table_id）")
    tbc.add_argument("--records", required=True, help="勾上了的记录 id，逗号分隔")
    tbc.add_argument("--by", required=True, help="操作人，feishu:ou_…")
    tbs.add_parser(
        "subscribe",
        help="订阅选款表的记录变更事件（开好权限、加好事件后执行一次；表格同步时也会自动订阅）",
    )
    tbs.add_parser(
        "poll",
        help="回读各张候选表表顶的按钮行和交付包行、「选中的款」的下载确认，勾着的照样处理"
        "（巡检到点时自动在后台跑，兜住漏掉的事件）",
    )
    tbk = tbs.add_parser(
        "ticks",
        help="全自动上架的撤销窗口：候选表里勾了店铺的行到点回读、仍勾着就定为选中并上架（后台自动拉起）",
    )
    tbk.add_argument("--max-minutes", type=float, default=candidate_stores.TICKS_WATCH_MINUTES)
    tbce = tbs.add_parser(
        "cells",
        help="人在飞书表里改格子的日志（旧值 / 新值 / 修改人 / 时间 / 程序怎么处理的）；--read 现在回读一遍「选中的款」",
    )
    tbce.add_argument("--asset", help="只看这一款（IMG…）")
    tbce.add_argument("--code", help="只看这个货号（FLow0006…）")
    tbce.add_argument("--limit", type=int, default=100)
    tbce.add_argument("--read", action="store_true", help="现在回读「选中的款」（不等巡检）")
    tbdl = tbs.add_parser(
        "downloaded",
        help="标这几款的套图已下载（「选中的款」里打上「下载确认」、沉到表底；一个批次的款都标了，"
        "这个批次的交付包也算已下载）；--undo 撤销标错的",
    )
    tbdl.add_argument(
        "--asset", required=True, help="素材编号，如 IMG00327，多个用逗号分开"
    )
    tbdl.add_argument("--by", required=True)
    tbdl.add_argument("--undo", action="store_true", help="撤销下载确认")
    tbs.add_parser("status")
    tbs.add_parser(
        "galleries",
        help="所有画册视图的直达链接（候选表模板和各任务候选表的「全部画册」「已选中」「待选」，「选中的款」「已上架的款」的"
        "「看图」，每项带 view）：封面效果这类设置只能在飞书里改，照着链接一个个点开；新候选表从模板复制，"
        "改模板就改了以后的默认",
    )
    tbb = tbs.add_parser(
        "books",
        help="每周选款表（table.weekly）：模板表格（锁没锁）、各本周表格、各天的文件夹和「选中的款」",
    )
    tbb.add_argument(
        "--lock",
        action="store_true",
        help="锁上模板表格：开高级权限，编辑者三张模板表都只读（在模板的画册里设好封面效果之后；"
        "要 base:app:update、base:role:read、base:role:update 权限）",
    )
    tbr = tbs.add_parser(
        "reasons",
        help="没选原因汇总（候选表「没选原因」列）：按原因、方向类型、字段、「单元·字段」计数",
    )
    tbr.add_argument("--seed", help="只看这个任务（参考图），如 S01；不写看全部")
    tbr.add_argument(
        "--refresh",
        action="store_true",
        help="先重读候选表里的原因（验收时会自动读，之后补填的可以用它马上读进来）",
    )
    scp = sub.add_parser(
        "scenes",
        help="套图（场景图）：选中的款交给 mooya 出场景图，拉回原图、打套图压缩包、写进选款表",
    )
    scs = scp.add_subparsers(dest="scenes_cmd", required=True)
    sst = scs.add_parser(
        "start", help="给选中的款出套图（正在出的跳过；出过的要 --force；没出成的重出）"
    )
    sst.add_argument("--seed", help="这个任务（参考图）里全部选中的款，如 S01")
    sst.add_argument(
        "--asset", action="append", help="指定的款（IMG 编号），可重复写或用逗号分隔"
    )
    sst.add_argument("--force", action="store_true", help="已经出过套图的也重出")
    sst.add_argument("--by", required=True)
    ssn = scs.add_parser("sync", help="交排队的款、查进度、拉图（巡检会自动拉起）")
    ssn.add_argument("--detach", action="store_true")
    ssn.add_argument("--max-minutes", type=float, help="这次最多跑多久（分钟）")
    sss = scs.add_parser("status", help="每款最近一次套图的状态")
    sss.add_argument("--seed")
    ssh = scs.add_parser("show", help="一次套图的明细与图片")
    ssh.add_argument("set_id")
    ssc = scs.add_parser("cancel", help="停掉还没出完的套图（mooya 那边一起停）")
    ssc.add_argument("set_id")
    ssc.add_argument("--by", required=True)
    ssr = scs.add_parser(
        "refresh",
        help="在 mooya 里重生过单张图（或调过交付包）后重新拉图、重打压缩包、刷新表格",
    )
    ssr.add_argument("set_id", nargs="?")
    ssr.add_argument(
        "--all",
        action="store_true",
        help="出完的每款最近一套都重拉（不重新出图），给旧套图换上 mooya 交付包的文件名",
    )
    ssr.add_argument("--seed", help="与 --all 一起用：只重拉这个任务的")
    ssr.add_argument("--detach", action="store_true")
    spk = scs.add_parser(
        "packages",
        help="各任务各批次的套图交付包（每款一个文件夹）：状态、大小、下载链接",
    )
    spk.add_argument("--seed")
    spp = scs.add_parser(
        "pack",
        help="打该打的交付包、续签快到期的链接（套图进程收尾时和巡检会自动做）",
    )
    spp.add_argument("--seed")
    spp.add_argument("--detach", action="store_true")
    srp = scs.add_parser(
        "repack", help="重打一个批次的交付包（内容没变也重打，下载链接会换）"
    )
    srp.add_argument("--seed", required=True, help="任务（参考图）编号，如 S07")
    srp.add_argument("--batch", required=True, help="批次编号，如 B010")
    sdl = scs.add_parser(
        "downloaded",
        help="标这个批次的交付包已下载（人说下载好了才标）：每个批次都标完、套图都出完的任务到「套图已下载」，"
        "开着自动归档时当场归档；--undo 撤销标错的",
    )
    sdl.add_argument("--seed", required=True, help="任务（参考图）编号，如 S07")
    sdl.add_argument("--batch", required=True, help="批次编号，如 B010")
    sdl.add_argument("--by", required=True)
    sdl.add_argument("--undo", action="store_true", help="撤销「已下载」")
    scl = scs.add_parser(
        "set-client",
        help="mooya 里把套图任务挪到别的租户之后（mooya scripts/move_service_jobs_tenant.py），"
        "把这些套图改记成那个租户的客户端",
    )
    scl.add_argument("--report", help="挪租户脚本的报告（JSON，用里面的 jobs）")
    scl.add_argument("--jobs", help="逗号分隔的 mooya 任务 id")
    scl.add_argument("--client", required=True, help="mooya.clients 里的名字，如 deerflow")
    scl.add_argument("--by", required=True)
    scl.add_argument("--dry-run", action="store_true")
    sdl.add_argument(
        "--source",
        choices=["chat", "card"],
        default="chat",
        help="从哪标的：chat = POD 对话（缺省），card = 卡片按钮",
    )
    pa = sub.add_parser("pause")
    pa.add_argument("--reason")
    re_ = sub.add_parser("resume")
    re_.add_argument("--by", required=True)
    tr = sub.add_parser("trial", help="连续试运行窗口（CFG16）")
    trs = tr.add_subparsers(dest="trial_cmd", required=True)
    tst = trs.add_parser("start")
    tst.add_argument("--hours", type=float)
    tst.add_argument("--by", required=True)
    tsp = trs.add_parser("stop")
    tsp.add_argument("--by", required=True)
    trs.add_parser("show")

    rc = sub.add_parser("reconcile", help="结果未知调用的扣费对账")
    rc.add_argument("call_id")
    g = rc.add_mutually_exclusive_group(required=True)
    g.add_argument("--charged", action="store_true")
    g.add_argument("--not-charged", action="store_true")
    rc.add_argument("--by", required=True)
    rc.add_argument("--note")

    rq = sub.add_parser("requeue", help="运维纠错：把素材放回质检或修复重跑（留痕）")
    rq.add_argument("asset_id")
    rq.add_argument("--stage", required=True, choices=["qc", "repair"])
    rq.add_argument("--by", required=True)
    rq.add_argument("--note", required=True)

    rp = sub.add_parser("report", help="报告")
    rps = rp.add_subparsers(dest="report_cmd", required=True)
    rb = rps.add_parser("batch")
    rb.add_argument("batch_id")
    rdy = rps.add_parser("daily")
    rdy.add_argument("--day")
    rdy.add_argument("--notify", action="store_true")
    rdg = rps.add_parser(
        "digest", help="状态总结：待你处理 / 待生成·进行中 / 今日已完成 / 异常"
    )
    rdg.add_argument("--notify", action="store_true", help="发到飞书")
    rtd = rps.add_parser("todo", help="现在的待办：待挑选 / 待下载 / 套图等人工")
    rtd.add_argument("--notify", action="store_true", help="发到飞书（没有待办不发）")

    n = sub.add_parser("notify", help="发一条飞书通知（默认发工作台卡片）")
    n.add_argument("--text", help="改发这段纯文本")
    u = sub.add_parser("ui", help="飞书工作台的视图数据（渠道渲染卡片用）")
    us = u.add_subparsers(dest="ui_cmd", required=True)
    us.add_parser("dashboard")
    uts = us.add_parser("tasks", help="任务列表卡（一张参考图一个任务）")
    uts.add_argument("--offset", type=int, default=0)
    uts.add_argument("--limit", type=int, default=ui.TASK_PAGE_SIZE)
    ur = us.add_parser("reviews")
    ur.add_argument("--type", action="append")
    ur.add_argument("--batch")
    ur.add_argument("--offset", type=int, default=0)
    ur.add_argument("--limit", type=int, default=3)
    ud = us.add_parser("daily")
    ud.add_argument("--day")
    ut = us.add_parser("timeline", help="最近动态（关键事件的一句话说明）")
    ut.add_argument("--limit", type=int, default=20)
    ug = us.add_parser("gallery", help="已选中的款")
    ug.add_argument("--batch")
    ug.add_argument("--offset", type=int, default=0)
    ug.add_argument("--limit", type=int, default=3)
    uo = us.add_parser("originals", help="已选中款的原图路径")
    uo.add_argument("--asset", action="append", required=True)
    udr = us.add_parser("directions", help="裂变方向挑选卡")
    udr.add_argument("--batch", required=True)
    uhi = us.add_parser(
        "history", help="任务明细卡（原方向记录卡；不指定系列时只有一个就直接显示）"
    )
    uhi.add_argument("--family")
    uhi.add_argument("--offset", type=int, default=0)
    ush = us.add_parser("shelf", help="历史任务卡（已归档的任务，可以搜）")
    ush.add_argument("--offset", type=int, default=0)
    ush.add_argument("--limit", type=int, default=ui.SHELF_PAGE_SIZE)
    ush.add_argument("--query", help="搜名字、编号或日期")
    mt = sub.add_parser(
        "maintenance", help="流程调整后的存量数据迁移与旧数据清理（可重复执行）"
    )
    mts = mt.add_subparsers(dest="maintenance_cmd", required=True)
    mtr = mts.add_parser(
        "retire-dedup", help="近重复判定下线：疑似重复待审关闭，素材直接进选款"
    )
    mtr.add_argument("--by", required=True)
    mrr = mts.add_parser(
        "restore-rejected",
        help="把质检淘汰的图放回选款（标「未经质检」；质检暂停后用）",
    )
    mrr.add_argument(
        "--batch",
        action="append",
        default=[],
        help="只放回这些批次的，可重复写；不写就是全部",
    )
    mrr.add_argument("--by", required=True)
    mrr.add_argument("--dry-run", action="store_true", help="只列要放回的，不改")
    mds = mts.add_parser(
        "dismiss-selection",
        help="把这些批次还没处理的候选清出选款（图片和记录留着，不算人工没选）",
    )
    mds.add_argument(
        "--batch",
        action="append",
        required=True,
        help="清哪个批次的候选，可重复写",
    )
    mds.add_argument("--by", required=True)
    mds.add_argument("--note", help="记在待审上的说明；不写用默认说明")
    mds.add_argument("--dry-run", action="store_true", help="只列要清的，不改")
    mra = mts.add_parser(
        "relabel-assets",
        help="把选款表和各候选表已有行的素材名字改成「编号_方向短语」（素材列、缩略图、下载原图）",
    )
    mra.add_argument("--by", required=True)
    mra.add_argument("--dry-run", action="store_true", help="只数要改的行，不改")
    mid = mts.add_parser(
        "import-downloads",
        help="把「选中的款」里人自己记的下载状态收成下载确认（这一列是这个选项的行算已下载），"
        "已经标了已下载的交付包也给包里的款补上确认；先 --dry-run 看会自动标下载、归档的批次",
    )
    mid.add_argument("--column", required=True, help="人自己加的那一列，如 下载情况")
    mid.add_argument("--option", required=True, help="算已下载的选项，如 已下载")
    mid.add_argument("--by", required=True)
    mid.add_argument("--dry-run", action="store_true", help="只列会怎样，不改")
    mds2 = mts.add_parser(
        "drop-scene-images",
        help="删掉各张「选中的款」和「选中的款模板」里的「场景图」列（2026-10-29 起不放套图缩略图）；先 --dry-run 看哪些表还有",
    )
    mds2.add_argument("--by", required=True)
    mds2.add_argument("--dry-run", action="store_true", help="只列哪些表还有这一列，不删")
    mmw = mts.add_parser(
        "migrate-week",
        help="把一周里还在旧表格的任务搬进那一周的周表格（每周选款表）：候选表、「选中的款」照业务库重写，"
        "人填的格子搬过去、逐格核对后删掉旧的；先 --dry-run 看清单，中途失败了重跑接着做",
    )
    mmw.add_argument("--week", required=True, help="哪一周，如 2026-W44")
    mmw.add_argument("--by", required=True)
    mmw.add_argument("--dry-run", action="store_true", help="只列要搬的、要删的，不改")
    mmw.add_argument(
        "--backup-dir", help="旧表记录的 JSON 备份放哪；不写放数据目录的 backups/"
    )
    mmw.add_argument(
        "--keep-columns",
        action="store_true",
        help="人自己在旧表里加的列也在新表里建上、把值搬过去（缺省不搬，值留在 JSON 备份里）",
    )
    mpo = mts.add_parser(
        "purge-old",
        help="彻底删掉旧数据（库里、OSS、本机都删，不可恢复；先 --dry-run 看清单）",
    )
    mpo.add_argument(
        "--family", help="系列编号（如 F01）：删掉比 --keep-version 旧的批次与系列版本"
    )
    mpo.add_argument(
        "--keep-version", type=int, help="要保留的系列版本，必须是最新版本"
    )
    mpo.add_argument(
        "--seed",
        action="append",
        default=[],
        help="整张删掉的参考图（连同它的系列与批次），可重复写",
    )
    mpo.add_argument("--by", required=True)
    mpo.add_argument("--dry-run", action="store_true", help="只列要删的，不删")
    lg = sub.add_parser("log", help="最近的后台日志与事件")
    lg.add_argument("--lines", type=int, default=40)
    lg.add_argument("--events", type=int, default=20)
    return p


HANDLERS = {
    "doctor": cmd_doctor,
    "status": cmd_status,
    "tasks": cmd_tasks,
    "shelf": cmd_shelf,
    "listing": cmd_listing,
    "temu": cmd_temu,
    "seed": cmd_seed,
    "run": cmd_run,
    "family": cmd_family,
    "plan": cmd_plan,
    "directions": cmd_directions,
    "worker": cmd_worker,
    "tick": cmd_tick,
    "review": cmd_review,
    "asset": cmd_asset,
    "job": cmd_job,
    "budget": cmd_budget,
    "db": cmd_db,
    "storage": cmd_storage,
    "table": cmd_table,
    "scenes": cmd_scenes,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "trial": cmd_trial,
    "reconcile": cmd_reconcile,
    "requeue": cmd_requeue,
    "report": cmd_report,
    "notify": cmd_notify,
    "ui": cmd_ui,
    "maintenance": cmd_maintenance,
    "log": cmd_log,
}

USER_ERRORS = (
    StageError,
    review.ReviewError,
    cfgmod.ConfigError,
    MigrationError,
    store.StorageError,
    OSSError,
    budget.BudgetBlocked,
    notify.NotifyError,
    mooya.MooyaError,
    miaoshou.MiaoshouError,
    PromptError,
    ValueError,
)


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        if args.cmd == "init":
            return cmd_init(args), 0
        return HANDLERS[args.cmd](open_ctx(), args), 0
    except budget.BudgetBlocked as exc:
        return {
            "error": "budget_blocked",
            "reason": exc.reason,
            "detail": exc.detail,
        }, 2
    except llm.StageOutputInvalid as exc:
        return {
            "error": "stage_output_invalid",
            "stage": exc.stage,
            "errors": exc.errors,
        }, 2
    except llm.LLMResultUnknown as exc:
        return {
            "error": "result_unknown",
            "call_id": exc.call_id,
            "detail": str(exc),
            "hint": "LLM 调用结果未知，可直接重跑该阶段",
        }, 2
    except llm.LLMError as exc:
        return {"error": "llm_error", "detail": str(exc)}, 2
    except USER_ERRORS as exc:
        return {"error": type(exc).__name__, "detail": str(exc)}, 2
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，保证总输出 JSON
        return {
            "error": "internal",
            "detail": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-2000:],
        }, 1


def main(argv: list[str] | None = None) -> int:
    global _RAW_ARGV
    _RAW_ARGV = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_RAW_ARGV)
    task_id = bg.adopt()
    if task_id:
        bg.mark_running(open_ctx(), task_id)
    output, code = _execute(args)
    if task_id:
        bg.finish(open_ctx(), task_id, output, ok=code == 0)
    _out(output)
    return code
