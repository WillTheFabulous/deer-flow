"""开跑前自检：配置、密钥、价格、预算、提示词与 schema、LiteLLM 连通性与模型可见性。

不带 --probe 时不产生任何付费调用；--probe 会在预算内发一次极小的文本调用，验证 key 与模型可用。
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from . import (
    budget,
    candidate_stores,
    config as cfgmod,
    digest,
    directions,
    feishu_table,
    http,
    images,
    llm,
    mooya,
    packages,
    prompts,
    scenes,
    schemas,
    state,
    store,
    subjects,
)
from .context import Ctx
from .stages.common import (
    MODE_LABELS,
    StageError,
    fanout_slots,
    generation_modes,
    intake_review_enabled,
    qc_enabled,
    small_change_modes,
)
from .stages import plan as planmod
from .stages.profile import describe_enabled
from .util import parse_iso

STAGES = [
    "P01",
    "P02",
    "P03",
    "P04",
    "P05",
    "P06",
    "P07",
    "P11",
    "P12",
    "P13",
    "P14",
    "P15",
]


def _check(
    name: str, ok: bool, detail: Any = None, *, level: str = "error"
) -> dict[str, Any]:
    return {"check": name, "ok": ok, "level": "ok" if ok else level, "detail": detail}


def _round_images_text(ctx: Ctx, modes: list[str], small_modes: list[str]) -> str:
    """一轮策划默认出多少张图：小变化个数 × 小变化槽位 + 大变化个数 × 大变化槽位（开着二次大变化时再加它的）。"""
    try:
        small = int(ctx.setting("plan.small_count", 12)) * len(
            fanout_slots(ctx, small_modes)
        )
        big_slots = len(fanout_slots(ctx, modes))
        big = int(ctx.setting("plan.big_count", 8)) * big_slots
        derived = planmod.derived_settings(ctx)
        extra = derived["count"] * big_slots if derived["enabled"] else 0
    except (TypeError, ValueError, cfgmod.ConfigError):
        return ""
    if extra:
        return f"，一轮默认约 {small + big + extra} 张图（含二次大变化约 {extra} 张）"
    return f"，一轮默认约 {small + big} 张图"


def _storage_checks(ctx: Ctx) -> list[dict[str, Any]]:
    if not store.oss_enabled(ctx):
        return [_check("素材存储", True, "本机（storage.backend: local）")]
    checks = []
    key = store.object_key(ctx, f"_doctor/probe-{uuid.uuid4().hex[:8]}.txt")
    try:
        oss = store.client(ctx)
        payload = b"podflow doctor probe"
        oss.put(key, payload, content_type="text/plain", forbid_overwrite=True)
        ok = oss.get(key) == payload
        oss.delete(key)
        checks.append(_check("OSS 可写可读（写入、读回、删除探针对象）", ok, key))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _check(
                "OSS 可写可读（写入、读回、删除探针对象）",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        )
    info = store.status(ctx)
    checks.append(
        _check(
            "没有上传失败卡住的素材",
            not info["stuck"],
            info["stuck"] or f"待上传 {info['pending']} 个",
            level="warn",
        )
    )
    return checks


def _scene_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """套图打开时：mooya 机器调用接口连得上、令牌对、预设在；没有卡住的 mooya 配置错误。"""
    if not scenes.enabled(ctx):
        return [_check("套图（scenes.enabled）", True, "关着，不出套图")]
    checks = []
    preset = str(ctx.setting("mooya.preset") or "")
    try:
        # 交新套图用的客户端（mooya.submit_client）；查以前套图的其余客户端另列一项
        submit = mooya.submit_client(ctx)
        with mooya.Client(ctx, client=submit) as api:
            health = api.health()
        presets = [p.get("name") for p in health.get("presets") or []]
        checks.append(
            _check(
                "mooya 机器调用接口连得上、令牌有效、预设可用",
                preset in presets,
                {
                    "base_url": ctx.setting("mooya.base_url"),
                    "submit_client": submit,
                    "client": health.get("client"),
                    "tenant": health.get("tenant"),
                    "presets": presets,
                },
                level="warn",
            )
        )
    except mooya.MooyaError as exc:
        submit = None
        checks.append(
            _check(
                "mooya 机器调用接口连得上、令牌有效、预设可用",
                False,
                str(exc),
                level="warn",
            )
        )
    for name in mooya.client_names(ctx):
        if name == submit:
            continue
        try:
            with mooya.Client(ctx, client=name) as api:
                health = api.health()
            checks.append(
                _check(
                    f"mooya 客户端 {name}（查以前交的套图）连得上",
                    True,
                    {"client": health.get("client"), "tenant": health.get("tenant")},
                )
            )
        except mooya.MooyaError as exc:
            checks.append(
                _check(f"mooya 客户端 {name}（查以前交的套图）连得上", False, str(exc), level="warn")
            )
    error = scenes.status(ctx)["service_error"]
    checks.append(
        _check(
            "套图没有卡在 mooya 配置错误上",
            not error,
            error or "无",
            level="warn",
        )
    )
    if packages.enabled(ctx):
        stuck = [
            f"{p['label']}：{p['error']}"
            for p in packages.overview(ctx)["packages"]
            if p["error"]
        ]
        checks.append(
            _check(
                "套图交付包没有打包失败卡住的",
                not stuck,
                [*stuck, "修好后 podctl scenes repack --seed S --batch B 重打"]
                if stuck
                else "无",
                level="warn",
            )
        )
    return checks


def _temu_checks(ctx: Ctx, *, probe: bool = False) -> list[dict[str, Any]]:
    """Temu 自动上架：店铺配置、两个 mooya 客户端各自绑的租户、妙手凭据（probe 时再读每家店的样板）、上架卡住的原因。"""
    from . import (
        listed_table,
        miaoshou,
        stores,
        temu,
    )  # 上架模块依赖很多模块，放到用时再导入

    if not ctx.setting("temu.enabled", False):
        return [_check("Temu 自动上架（temu.enabled）", True, "关着")]
    checks: list[dict[str, Any]] = []
    info = stores.status(ctx)
    checks.append(
        _check(
            "上架店铺配置（temu.stores）",
            not info["error"] and bool(info["stores"]),
            info["error"]
            or [
                f"{s['column']} 前缀 {s['prefix']} 样板 {s['template_detail_id'] or '没配'}"
                f" 品牌 {s['brand_id'] or '没配'}"
                for s in info["stores"]
            ]
            or "一家店都没配",
        )
    )
    missing = [
        s["column"]
        for s in info["stores"]
        if not s["template_detail_id"] or not s["shop_id"]
    ]
    checks.append(
        _check(
            "每家店都配了妙手 shop_id 和样板商品",
            not missing,
            f"缺：{'、'.join(missing)}（在妙手里建好样板商品、不发布，把 collectBoxDetailId 填进 template_detail_id）"
            if missing
            else "都配了",
            level="warn",
        )
    )
    no_brand = [s["column"] for s in info["stores"] if not s["brand_id"]]
    checks.append(
        _check(
            "每家店都配了品牌（temu.stores 的 brand_id）",
            not no_brand,
            f"缺：{'、'.join(no_brand)}（样板克隆带不进品牌，不配就上成没有品牌的商品；"
            "编号是妙手店铺模式详情里的 brandId，在妙手选好品牌、保存后读回来）"
            if no_brand
            else "、".join(f"{s['column']}：{s['brand'] or '品牌'} {s['brand_id']}" for s in info["stores"]),
            level="warn",
        )
    )
    publish = str(ctx.setting("temu.publish", temu.MODE_BOX))
    checks.append(
        _check(
            "发布方式（temu.publish）",
            publish in (temu.MODE_AUTO, temu.MODE_BOX),
            f"{publish}：auto = 进箱后自动发布，box = 只进采集箱、人在妙手点发布"
            if publish in (temu.MODE_AUTO, temu.MODE_BOX)
            else f"只能是 auto / box，现在是 {publish!r}（按 box 处理）",
            level="warn",
        )
    )
    auto = sorted(temu.auto_stores(ctx))
    if ctx.setting("temu.auto.enabled", False):
        columns = {s.key: s.column for s in stores.all_stores(ctx)}
        unknown = sorted({str(k) for k in ctx.setting("temu.auto.stores") or []} - set(columns))
        no_brand_auto = [columns[k] for k in auto if not stores.get(ctx, k).brand_id]
        checks.append(
            _check(
                "全自动上架（temu.auto）",
                bool(auto) and not unknown and not no_brand_auto,
                (
                    [f"开着的店：{'、'.join(columns[k] for k in auto) or '没有'}"]
                    + [
                        f"勾店铺后等 {candidate_stores.grace_minutes(ctx):g} 分钟；"
                        f"程序确认上架 {'开' if ctx.setting('temu.auto.confirm', True) else '关'}，"
                        f"自动发布 {'开' if ctx.setting('temu.auto.publish', True) else '关'}"
                    ]
                    + ([f"temu.auto.stores 里不认识：{'、'.join(unknown)}"] if unknown else [])
                    + ([f"没配品牌的店全自动不发布：{'、'.join(no_brand_auto)}"] if no_brand_auto else [])
                ),
                level="error" if no_brand_auto else "warn",
            )
        )
    else:
        checks.append(_check("全自动上架（temu.auto）", True, "关着：勾店铺后照旧要人点按钮、勾「确认上架」"))
    from . import mooya_listing, temu_open  # Temu 官方接口 / mooya 上架都依赖上架模块，放到用时再导入

    checks += temu_open.doctor_checks(ctx)
    checks += mooya_listing.doctor_checks(ctx) if probe or mooya_listing.enabled(ctx) else []
    checks.append(
        _check(
            "上架要开 OSS 和上架状态（storage.backend: oss、listing.enabled）",
            store.oss_enabled(ctx) and feishu_table.listing_enabled(ctx),
            "妙手从 OSS 签名链接下载上架图；「选中的款」要有上架状态那一列",
        )
    )
    try:
        submit = mooya.submit_client(ctx)
    except mooya.MooyaError as exc:
        submit = None
        checks.append(
            _check("mooya 交套图的客户端（mooya.submit_client）", False, str(exc))
        )
    for name in mooya.client_names(ctx):
        try:
            with mooya.Client(ctx, client=name) as api:
                health = api.health()
            checks.append(
                _check(
                    f"mooya 客户端 {name} 连得上",
                    True,
                    {
                        "client": health.get("client"),
                        "tenant": health.get("tenant"),
                        "submit": name == submit,
                    },
                )
            )
        except mooya.MooyaError as exc:
            checks.append(
                _check(f"mooya 客户端 {name} 连得上", False, str(exc), level="warn")
            )
    if submit == mooya.LEGACY_CLIENT:
        checks.append(
            _check(
                "新交的套图走字标租户",
                False,
                "mooya.submit_client 还是 legacy：新套图取不到带 MIANDUDU 字标的上架图，不能自动上",
                level="warn",
            )
        )
    try:
        with miaoshou.Client(ctx) as api:
            api.site_options()
            checks.append(
                _check(
                    "妙手开放平台凭据有效（签名、白名单）",
                    True,
                    ctx.setting("miaoshou.base_url") or miaoshou.DEFAULT_BASE_URL,
                )
            )
            if probe:
                for store_ in stores.all_stores(ctx):
                    try:
                        tpl = temu.template(ctx, api, store_, refresh=True)
                        checks.append(
                            _check(
                                f"{store_.column} 的样板可用",
                                True,
                                {
                                    "sites": temu.shop_sites(store_, tpl),
                                    "skus": len(tpl.get("skuMap") or {}),
                                },
                            )
                        )
                    except (miaoshou.MiaoshouError, StageError) as exc:
                        checks.append(
                            _check(
                                f"{store_.column} 的样板可用",
                                False,
                                str(exc),
                                level="warn",
                            )
                        )
    except miaoshou.MiaoshouError as exc:
        checks.append(_check("妙手开放平台凭据有效（签名、白名单）", False, str(exc)))
    status = temu.status(ctx)
    checks.append(
        _check(
            "上架没有卡在妙手的权限 / 白名单错误上",
            not status["service_error"],
            status["service_error"] or "无",
            level="warn",
        )
    )
    failed_codes = info["codes_failed"]
    checks.append(
        _check(
            "货号都领到了",
            not failed_codes,
            [f"{f['asset_id']} {f['store_key']}：{f['error']}" for f in failed_codes]
            + ["修好后 podctl temu codes --retry"]
            if failed_codes
            else f"等着领的 {info['codes_waiting']} 个",
            level="warn",
        )
    )
    listed = listed_table.status(ctx)
    checks.append(
        _check(
            "「已上架的款」没有写失败卡住的",
            not listed["failed"],
            listed["failed"] or f"{listed['rows']} 条，待写 {listed['pending']} 条",
            level="warn",
        )
    )
    if listed.get("source") == listed_table.SOURCE_MOOYA:
        checks.append(
            _check(
                "「已上架的款」读 mooya 底账（temu.listed.source: mooya）",
                bool(listed.get("pulled_at")) and not listed.get("not_pushed"),
                (
                    [f"拉底账的客户端：{'、'.join(listed_table.ledger_clients(ctx))}；上次拉 {listed.get('pulled_at') or '还没拉过'}"]
                    + (
                        [f"本机上的、还没推给 mooya：{'、'.join(listed['not_pushed'][:10])}"]
                        if listed.get("not_pushed")
                        else []
                    )
                ),
                level="warn",
            )
        )
    crafted = listed.get("crafted")
    if crafted:
        tpl = crafted.get("template") or {}
        if crafted["ready"]:
            how: str | list[str] = f"{len(crafted['books'])} 本周表格、{crafted['days']} 天"
        elif tpl.get("ready"):
            how = [f"模板 {tpl.get('url')}：先在「看图」里设好封面效果，再 podctl temu listed crafted lock（锁上之前精作款的行不写）"]
        else:
            how = [tpl.get("error") or "还没建模板：podctl temu listed crafted template"]
        checks.append(
            _check("精作款周表格（temu.listed.crafted）模板建好、锁上了", bool(crafted["ready"]), how, level="warn")
        )
        checks.append(
            _check(
                "精作款都从「已上架的款」迁过去了",
                not crafted["not_migrated"],
                [f"还有 {crafted['not_migrated']} 条在「已上架的款」里：podctl temu listed crafted migrate --dry-run 看过再迁"]
                if crafted["not_migrated"]
                else f"{crafted['rows']} 条，已写进精作款 {crafted['written']} 条",
                level="warn",
            )
        )
    return checks


def _shelf_checks(ctx: Ctx, table: dict[str, Any]) -> list[dict[str, Any]]:
    """任务归档、上架状态、默认视图：块接口能用（挪候选表要它）、自动归档开没开、有没有挪不动 / 没建成的。"""
    from . import candidates, feishu, shelf  # 候选表模块依赖很多模块，放到用时再导入

    checks: list[dict[str, Any]] = []
    info = feishu_table.base(ctx)
    if shelf.enabled(ctx) and info:
        try:
            with feishu.Client(ctx) as api:
                api.call(
                    "POST", f"/base/v3/bases/{info['app_token']}/blocks/list", json={}
                )
            checks.append(
                _check(
                    "飞书 Base 块接口可用（候选表挪进「已归档」文件夹要用）",
                    True,
                    "base:block:read",
                )
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                _check(
                    "飞书 Base 块接口可用（候选表挪进「已归档」文件夹要用）",
                    False,
                    f"{exc}；在飞书开放平台给应用开 base:block:read / base:block:create / "
                    "base:block:update 并发布版本",
                    level="warn",
                )
            )
        status = shelf.status(ctx)
        checks.append(
            _check(
                "自动归档（shelf.auto）",
                status["auto"],
                f"已归档 {status['archived']} 个任务"
                if status["auto"]
                else "关着：做完的任务要在任务列表点「归档」；podctl shelf sweep --dry-run 看哪些能自动归档",
                level="warn",
            )
        )
        checks.append(
            _check(
                "归档的候选表都挪好了",
                not status["folder_errors"],
                status["folder_errors"] or f"还要挪 {status['moves_pending']} 张",
                level="warn",
            )
        )
    listing_info = table.get("listing") or {}
    if listing_info.get("enabled"):
        checks.append(
            _check(
                "「选中的款」有上架状态三列",
                bool(listing_info.get("ready")) and not listing_info.get("error"),
                listing_info.get("error")
                or ("已补上" if listing_info.get("ready") else "下一次表格同步时补上"),
                level="warn",
            )
        )
    view_errors = [
        e
        for e in (
            (table.get("views") or {}).get("error"),
            candidates.status(ctx).get("views_error"),
        )
        if e
    ]
    if view_errors:
        checks.append(
            _check(
                "候选表 / 选中的款的默认视图建好了（table.views）",
                False,
                view_errors,
                level="warn",
            )
        )
    checks.extend(_template_checks(ctx))
    checks.extend(_weekly_checks(ctx))
    return checks


def _weekly_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """每周选款表（table.weekly）：模板表格建好、锁上了；各天的文件夹、各本周表格的事件订阅没有报错。"""
    from . import books  # 每周选款表依赖表格模块，放到用时再导入

    state = books.status(ctx)
    if not state["enabled"]:
        return []
    tpl = state["template"]
    locked = bool(tpl.get("ready") and tpl.get("locked_at"))
    if locked:
        detail = (
            f"模板表格：{tpl['url']}（编辑者只读；画册的封面效果在它里面设：候选表模板的「全部画册」「已选中」「待选」、"
            "另两张模板的「看图」，以后的周表格照它）"
        )
    elif tpl.get("ready"):
        detail = (
            f"模板表格 {tpl['url']} 还没锁上：{tpl.get('lock_error') or '下一次表格同步时锁'}"
            "（要 base:app:update、base:role:read、base:role:update 权限）"
        )
    else:
        detail = tpl.get("error") or "下一次表格同步时建"
    problems = [
        f"{d['day']}：{d['error']}"
        for d in state["days"]
        if d.get("error") and not d.get("table_id")
    ] + [
        f"{b['book_id']} 事件订阅：{(b.get('events') or {}).get('error')}"
        for b in state["books"]
        if (b.get("events") or {}).get("error")
    ]
    current = books.current_book(ctx) or {}
    return [
        _check("每周选款表的模板表格建好并锁上了（table.weekly）", locked, detail, level="warn"),
        _check(
            "每周选款表各天的文件夹、各本周表格的事件订阅正常",
            not problems,
            "；".join(problems)
            if problems
            else f"这一周：{current.get('url') or '还没建（这周第一张表要建时再建）'}",
            level="warn",
        ),
    ]


def _template_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """新候选表从模板表复制（table.template）：模板在、跟上了当前版本，最近一次复制没有退回新建。"""
    from . import table_template  # 模板依赖候选表模块，放到用时再导入

    template = table_template.status(ctx)
    if not template["enabled"]:
        return []
    problem = template.get("error") or (template.get("last_fallback") or {}).get(
        "reason"
    )
    if problem:
        detail = problem
    elif template.get("ready"):
        detail = (
            f"模板：{template.get('url')}（画册「全部画册」「已选中」「待选」的封面效果、卡片样式在它里面设，新表照它）"
        )
        if template.get("recreated_at"):
            detail += f"；{template['recreated_at']} 重建过，要在它的三个画册重新设一次封面效果"
    else:
        detail = "下一次表格同步时建好 / 跟上当前版本"
    return [
        _check(
            "新候选表从模板表复制（table.template）",
            bool(template.get("ready")) and not problem,
            detail,
            level="warn",
        )
    ]


def run(ctx: Ctx, *, probe: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(_check("数据目录可写", os.access(ctx.home, os.W_OK), str(ctx.home)))
    checks.append(
        _check("业务库可用", ctx.db.scalar("SELECT 1") == 1, ctx.db.describe())
    )
    checks.extend(_storage_checks(ctx))
    checks.extend(_worker_checks(ctx))
    key_name = ctx.setting("litellm.api_key_name", "LITELLM_API_KEY")
    checks.append(
        _check(
            "secrets.env 存在且权限为 600",
            cfgmod.secrets_file_mode_ok(ctx.home),
            "chmod 600 secrets.env",
        )
    )
    checks.append(
        _check(f"LiteLLM key（{key_name}）已填写", bool(ctx.secrets.get(key_name)))
    )
    checks.append(
        _check(
            "deerflow_version 已记录（CFG01）",
            bool(ctx.setting("deerflow_version")),
            level="warn",
        )
    )
    checks.append(
        _check(
            "质检开着（qc.enabled）",
            qc_enabled(ctx),
            "质检已暂停：新图不调 P07、不自动修复，标「未经质检」直接进选款；要恢复就把 qc.enabled 改回 true",
            level="warn",
        )
    )
    checks.append(
        _check(
            "参考图接入确认开着（intake.review）",
            intake_review_enabled(ctx),
            "接入确认已冻结：看不清、模型拿不准的参考图也直接往下拆解（只有授权受限会停）；"
            "要恢复就把 intake.review 改回 true",
            level="warn",
        )
    )
    if describe_enabled(ctx):
        undescribed = [
            r["seed_id"]
            for r in ctx.db.q(
                "SELECT s.seed_id FROM seeds s LEFT JOIN seed_descriptions d"
                " ON d.analysis_id = s.latest_analysis_id WHERE s.latest_analysis_id IS NOT NULL"
                " AND s.status != 'blocked' AND d.analysis_id IS NULL ORDER BY s.seed_id"
            )
        ]
        checks.append(
            _check(
                "拆解过的参考图都有画面描述（S02D）",
                not undescribed,
                f"还没有：{'、'.join(undescribed)}（策划和出图指令先按拆解简版写）；"
                "podctl seed describe --all 补写"
                if undescribed
                else "全部都有",
                level="warn",
            )
        )
    else:
        checks.append(
            _check(
                "画面描述开着（describe.enabled）",
                False,
                "关着：策划、出图指令和质检按拆解简版写；要恢复就把 describe.enabled 改回 true",
                level="warn",
            )
        )
    checks.extend(digest.doctor_checks(ctx))
    checks.extend(_temu_checks(ctx, probe=probe))

    for pid in ["G00", *STAGES, "P00", "P09", "P10"]:
        try:
            prompts.load_prompt(pid)
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(f"提示词 {pid}", False, str(exc)))
    for stage in [*STAGES, "P09", "P10"]:
        try:
            schemas.validate_result(stage, {})
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(f"schema {stage}", False, str(exc)))
    checks.append(
        _check(
            "提示词与 schema 可加载",
            not any(
                not c["ok"]
                for c in checks
                if c["check"].startswith(("提示词", "schema"))
            ),
        )
    )

    stage_models = sorted({llm.stage_model(ctx, s) for s in STAGES})
    try:
        modes = generation_modes(ctx)
        checks.append(
            _check(
                "出图方式至少启用一种（image_generation.modes）",
                bool(modes),
                [MODE_LABELS[m] for m in modes],
            )
        )
    except cfgmod.ConfigError as exc:
        modes = []
        checks.append(
            _check("出图方式至少启用一种（image_generation.modes）", False, str(exc))
        )
    try:
        small_modes = small_change_modes(ctx)
        checks.append(
            _check(
                "小变化的出图方式（image_generation.small_change_modes）",
                bool(small_modes),
                [MODE_LABELS[m] for m in small_modes],
            )
        )
        if "t2i" in small_modes:
            checks.append(
                _check(
                    "小变化只做图生图（文生图从零画，保不住原构图）",
                    False,
                    "image_generation.small_change_modes 里有 t2i",
                    level="warn",
                )
            )
    except cfgmod.ConfigError as exc:
        small_modes = []
        checks.append(
            _check(
                "小变化的出图方式（image_generation.small_change_modes）",
                False,
                str(exc),
            )
        )
    plan_name = "裂变策划参数（plan.small_count / big_count / big_single_count ≥ 0，slots_per_call / concurrency ≥ 1）"
    try:
        small, big, single, per_call, concurrency = (
            int(ctx.setting(f"plan.{key}", default))
            for key, default in (
                ("small_count", 12),
                ("big_count", 8),
                ("big_single_count", 4),
                ("slots_per_call", 4),
                ("concurrency", 4),
            )
        )
        checks.append(
            _check(
                plan_name,
                min(small, big, single) >= 0 and min(per_call, concurrency) >= 1,
                f"小变化 {small} 个、大变化 {big} 个（单字段大改最多 {single} 个，其余三处组合），"
                f"每次 P05 写 {per_call} 个槽位，同时跑 {concurrency} 组",
            )
        )
    except (TypeError, ValueError) as exc:
        checks.append(_check(plan_name, False, str(exc)))
    checks.extend(_unit_checks(ctx))
    checks.extend(_swap_checks(ctx))
    try:
        fanout = fanout_slots(ctx, list(dict.fromkeys(modes + small_modes)))
        checks.append(
            _check(
                "生图 fanout 至少启用一个模型", bool(fanout), [s for s, _, _ in fanout]
            )
        )
    except cfgmod.ConfigError as exc:
        fanout = []
        checks.append(_check("生图 fanout 至少启用一个模型", False, str(exc)))
    if directions.auto_confirm_enabled(ctx):
        checks.append(
            _check(
                "裂变方向等人挑选（batch.auto_confirm_directions 关着）",
                False,
                "方向自动确认开着：每轮策划出的方向全部确认、直接出图（系列一并认可）"
                + _round_images_text(ctx, modes, small_modes)
                + "；预算关着时只受待审积压上限约束，要恢复人工挑选就改回 false",
                level="warn",
            )
        )
    # 退役模型的旧图修复时改用接替模型，接替模型也要可见、有单价
    replaced = ctx.setting("image_generation.replaced_models") or {}
    image_models = {m for _, m, _ in fanout} | set(replaced.values())
    retired = sorted({budget.base_model(m) for _, m, _ in fanout} & set(replaced))
    if retired:
        checks.append(
            _check(
                "fanout 里没有已退役的模型（image_generation.replaced_models）",
                False,
                f"{retired} 会改用接替模型出图，和 fanout 里的接替模型重复",
                level="warn",
            )
        )
    sheet_font = ctx.setting("review.sheet_font")
    if sheet_font:
        checks.append(
            _check(
                "选款拼图的中文字体可读（review.sheet_font）",
                images.label_font(sheet_font) is not None,
                f"{sheet_font}；读不到时拼图只写 ASCII",
                level="warn",
            )
        )
    if feishu_table.enabled(ctx):
        checks.append(
            _check(
                "选款表要用的飞书应用凭证已填写（table.enabled）",
                bool(
                    ctx.secrets.get("FEISHU_APP_ID")
                    and ctx.secrets.get("FEISHU_APP_SECRET")
                ),
                "secrets.env 的 FEISHU_APP_ID / FEISHU_APP_SECRET",
            )
        )
        checks.append(
            _check(
                "选款表的原图下载链接需要 OSS（storage.backend: oss）",
                store.oss_enabled(ctx),
                "本机存储时表里没有下载链接，去工作台「已选款」点「发原图」",
                level="warn",
            )
        )
        table = feishu_table.status(ctx)
        checks.append(
            _check(
                "选款表没有同步卡住的行",
                not table["failed"]
                and not table["create_error"]
                and not table.get("wiki_error"),
                table["create_error"]
                or table.get("wiki_error")
                or table["failed"]
                or f"已同步 {table['rows']} 行，待同步 {table['pending']}",
                level="warn",
            )
        )
        events = table["events"]
        if events["wanted"] and table.get("url"):
            if events["subscribed"]:
                tz = ZoneInfo(str(ctx.setting("timezone", "Asia/Shanghai")))
                when = parse_iso(events["subscribed_at"])
                detail = "已订阅" + (
                    f"（{when.astimezone(tz).strftime('%m-%d %H:%M')}）" if when else ""
                )
            else:
                detail = events["error"] or (
                    "还没订阅：下一次表格同步时会订阅，也可以 podctl table subscribe；"
                    "订阅之前按钮行靠巡检兜底，最多 10 分钟"
                )
            checks.append(
                _check(
                    "候选表按钮行的记录变更事件已订阅（勾上几秒内就处理）",
                    events["subscribed"],
                    detail,
                    level="warn",
                )
            )
        checks += _shelf_checks(ctx, table)
    checks += _scene_checks(ctx)
    if not budget.enforced(ctx.cfg):
        checks.append(
            _check(
                "预算控制已关闭（budget.enforce: false）：不查上限、不要求单价",
                False,
                "用户决定暂不设预算、不计费用；恢复时改回 true 并填写 prices / budget",
                level="warn",
            )
        )
    else:
        missing_prices = []
        for model in stage_models:
            try:
                budget.estimate_llm_cost(ctx.cfg, model, "P07")
            except budget.BudgetBlocked as exc:
                missing_prices.append(exc.detail)
        for model in image_models:
            try:
                budget.estimate_image_cost(ctx.cfg, model)
            except budget.BudgetBlocked as exc:
                missing_prices.append(exc.detail)
        checks.append(
            _check(
                "所有在用模型都有单价（未知价格会阻断）",
                not missing_prices,
                sorted(set(missing_prices)),
            )
        )
        b = budget.summary(ctx.db, ctx.cfg)
        caps_ok = b["daily_cap"] > 0 and b["total_cap"] > 0 and b["per_task_cap"] > 0
        checks.append(
            _check(
                "预算上限已填写（CFG06–CFG08，0 = 不接收付费任务）",
                caps_ok,
                {
                    k: b[k]
                    for k in ("currency", "daily_cap", "total_cap", "per_task_cap")
                },
            )
        )

    base_url = ctx.setting("litellm.base_url").rstrip("/")
    visible: list[str] = []
    try:
        key = ctx.secrets.get(key_name)
        with http.make_client(20) as client:
            resp = client.get(
                f"{base_url}/v1/models",
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
        reachable = resp.status_code == 200
        if reachable:
            visible = [m.get("id") for m in resp.json().get("data", [])]
        checks.append(
            _check("LiteLLM 可连通并鉴权通过", reachable, f"HTTP {resp.status_code}")
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _check("LiteLLM 可连通并鉴权通过", False, f"{type(exc).__name__}: {exc}")
        )
    if visible:

        def is_visible(model: str) -> bool:
            return any(
                v == model or (v.endswith("*") and model.startswith(v[:-1]))
                for v in visible
            )

        wanted = set(stage_models) | image_models
        hidden = sorted(m for m in wanted if not is_visible(m))
        checks.append(
            _check(
                "LiteLLM 上能看到全部所需模型",
                not hidden,
                hidden or "全部可见",
            )
        )

    if probe:
        checks.append(_probe(ctx, stage_models[0]))
    failed = [c for c in checks if not c["ok"] and c["level"] == "error"]
    warns = [c for c in checks if not c["ok"] and c["level"] == "warn"]
    return {
        "ok": not failed,
        "errors": len(failed),
        "warnings": len(warns),
        "checks": checks,
    }


def _probe(ctx: Ctx, model: str) -> dict[str, Any]:
    try:
        estimated = budget.estimate_llm_cost(ctx.cfg, model, "P07")
        text, usage, call_id, _ = llm._chat_once(
            ctx,
            stage="PROBE",
            scope_id=f"doctor:{uuid.uuid4().hex[:8]}",
            model=model,
            messages=[{"role": "user", "content": '只回复一个 JSON：{"ok": true}'}],
            estimated_cost=estimated,
            json_mode=bool(ctx.setting("llm.json_mode", True)),
            asset_id=None,
        )
        return _check(
            f"探测调用 {model}",
            "ok" in text,
            {"call_id": call_id, "usage": usage, "reply": text[:80]},
        )
    except Exception as exc:  # noqa: BLE001
        return _check(f"探测调用 {model}", False, f"{type(exc).__name__}: {exc}")


def _unit_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """按「单元 × 字段」策划（plan.unit）：取值对不对；按单元时哪些参考图还没有可变单元清单（会按字段策划）。"""
    name = "裂变按可变单元策划（plan.unit）"
    try:
        mode = subjects.unit_mode(ctx)
    except cfgmod.ConfigError as exc:
        return [_check(name, False, str(exc))]
    if mode == subjects.FIELD_MODE:
        return [
            _check(
                name,
                False,
                "plan.unit 是 fields：小变化按字段策划（两个字段、改到画面里的同类元素）；要按单元策划就改回 pairs",
                level="warn",
            )
        ]
    if not describe_enabled(ctx):
        return [
            _check(
                name,
                False,
                "画面描述关着、没有可变单元清单：策划会按字段",
                level="warn",
            )
        ]
    missing = subjects.missing_inventory(ctx)
    return [
        _check(
            "参考图都有可变单元清单（按单元策划）",
            not missing,
            f"还没有：{'、'.join(missing)}（这几张策划时按字段）；podctl seed describe --all --force 重写画面描述"
            if missing
            else "全部都有",
            level="warn",
        )
    ]


_QUOTA_KEYS = ("plan.big_single_quota", "plan.combo_quota", "plan.derived.quota")


def _quota_part(label: str, ratios: dict[str, float], labels: dict[str, str]) -> str:
    """「单字段大改：动态 34%、换画法 34%」；没配额时「单字段大改不设配额」。"""
    if not ratios:
        return f"{label}不设配额"
    return f"{label}：" + "、".join(f"{labels[k]} {v:.0%}" for k, v in ratios.items())


def _swap_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """小变化换主体（plan.small_swap_ratio）、单字段大改重复（plan.big_single_repeat）、大变化配额
    （plan.big_single_quota / plan.combo_quota / plan.derived.quota）与二次大变化（plan.derived）。"""
    name = "小变化换主体、大变化配额与二次大变化（plan.small_swap_ratio / *_quota / plan.derived）"
    try:
        ratio = planmod.swap_ratio(ctx)
        quotas = {key: planmod.quota_ratios(ctx, key) for key in _QUOTA_KEYS}
    except cfgmod.ConfigError as exc:
        return [_check(name, False, str(exc))]
    derived = planmod.derived_settings(ctx)
    parts = [
        f"小变化里换主体 {ratio:.0%}（P14 先给新主体清单）"
        if ratio
        else "小变化不分组（照旧随机挑）",
        f"单字段大改同一个大字段一轮最多 {planmod.big_repeat(ctx)} 次",
        _quota_part(
            "单字段大改", quotas["plan.big_single_quota"], planmod.QUOTA_LABELS
        ),
        _quota_part("三处组合", quotas["plan.combo_quota"], planmod.COMBO_QUOTA_LABELS),
        f"二次大变化每个原批次最多 {derived['count']} 个、每个小变化当 {derived['per_parent']} 次底图；"
        + _quota_part("二次大变化", quotas["plan.derived.quota"], planmod.QUOTA_LABELS)
        if derived["enabled"]
        else "二次大变化关着",
    ]
    if quotas["plan.big_single_quota"].get("technique") or quotas[
        "plan.derived.quota"
    ].get("technique"):
        parts.append("换画法的方向按清单各分一种不重复的画法大类")
    checks = [_check(name, True, "；".join(parts))]
    if derived["enabled"] and not ratio:
        checks.append(
            _check(
                "二次大变化有可派生的批次",
                False,
                "plan.derived 开着但 plan.small_swap_ratio 是 0：二次大变化只自动接换主体规则下策划的批次，"
                "现在不会自动派生（手动 plan --derive-from 照样能用）",
                level="warn",
            )
        )
    return checks


# 出图 worker 连接数估算里，工位和生图名额之外的连接：主线程、心跳、批次反馈、巡检、表格 / OSS / 套图同步
_WORKER_EXTRA_CONNECTIONS = 6


def _worker_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """出图 worker 的工位与名额：数值合法、领取顺序有效；Postgres 时估算连接峰值不超过库角色的连接上限。"""
    values: dict[str, int] = {}
    for key, default in (
        ("batch.workers", 1),
        ("batch.image_concurrency", 8),
        ("batch.llm_concurrency", 3),
    ):
        try:
            values[key] = int(ctx.setting(key, default))
        except (TypeError, ValueError):
            values[key] = 0
    bad = [k for k, v in values.items() if v < 1]
    checks = [
        _check(
            "出图工位与名额（batch.workers / image_concurrency / llm_concurrency）",
            not bad,
            f"{'、'.join(bad)} 要写正整数"
            if bad
            else f"{values['batch.workers']} 条工位，生图名额 {values['batch.image_concurrency']}，"
            f"LLM 名额 {values['batch.llm_concurrency']}",
        )
    ]
    order = ctx.setting("batch.queue_order", "fair")
    checks.append(
        _check(
            "领取顺序（batch.queue_order）",
            order in state.QUEUE_ORDERS,
            f"{order}：fair = 修改优先、按任务轮流；fifo = 先来先到（写错了按 fair）",
            level="warn",
        )
    )
    if bad or not ctx.db.describe().startswith("postgres:"):
        return checks
    try:
        limit = int(
            ctx.db.scalar(
                "SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user"
            )
        )
        used = int(
            ctx.db.scalar(
                "SELECT COUNT(*) FROM pg_stat_activity WHERE usename = current_user"
            )
        )
    except Exception as exc:  # noqa: BLE001 - 查不到权限视图不挡别的检查
        checks.append(
            _check(
                "业务库连接数够用", False, f"{type(exc).__name__}: {exc}", level="warn"
            )
        )
        return checks
    need = (
        values["batch.workers"]
        + values["batch.image_concurrency"]
        + _WORKER_EXTRA_CONNECTIONS
    )
    checks.append(
        _check(
            "业务库连接数够用（出图 worker 峰值估算 ≤ 库角色上限）",
            limit < 0 or need <= limit,
            f"估算峰值约 {need} 条（工位 + 生图名额 + {_WORKER_EXTRA_CONNECTIONS}），"
            f"角色上限 {'不限' if limit < 0 else limit}，现在 {used} 条",
            level="warn",
        )
    )
    return checks
