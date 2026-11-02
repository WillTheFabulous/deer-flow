"""Temu 上架标题（P15，2026-10-28）：原 temu-blanket-title skill 的规则原文移植，模型看上架轮播图第 1 张写一条英文标题，
程序按同一份规则逐条复核（原 temu-blanket-batch-title 的 verify 检查 + 合规词表），不合格带原因重答。

- 规则只有一份：`references/temu_title_rules.json`（词表、节日日历、首 5 词、长度、场景短语）。提示词把它当变量，复核也读它。
- 复核 `check_title`：长度不到 250（且不超过类目上限）；首 5 词含 `Flannel Blanket`；`280GSM` 恰好一次；
  床品词正则（和原 verify 同一条）；材质 / 夸大 / 儿童 / 平台残留词按词边界、不分大小写；定制款首词必须是 `Custom`，
  不是定制款不能出现 Custom / Personalized；节日款节日词在首 5 词；礼品款要有 `Gift`。
- 定制款（is_custom）照旧流程单独处理：标题照写，但不自动上架（temu.py 拦）。
- 人可以在「选中的款」或「已上架的款」的「上架标题」里改（2026-10-30 起随时改，巡检读回）：按同一套规则复核
  （`save_human`，节日词 / 礼品词按这一款原来的判定一起查），不合规不改库里的标题、原因写在「标题同步」；合规的存下，
  再由 temu.py 按这一款所处的环节同步到妙手采集箱或 Temu 店铺（title_sync）。
- 每个标题版本（模型写的、人改的、不合规的）都记一条 listing_title_versions，旧标题不会丢。
"""

from __future__ import annotations

import functools
import json
import re
from typing import Any

from . import llm, trace
from .context import Ctx
from .db import log_event
from .paths import REFERENCES_DIR
from .stages.common import StageError, vision_image
from .util import dumps, loads, now_iso

STAGE = "P15"
RULES_PATH = REFERENCES_DIR / "temu_title_rules.json"
SOURCE_AUTO = "auto"
SOURCE_HUMAN = "human"


@functools.lru_cache(maxsize=1)
def rules() -> dict[str, Any]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _term_re(term: str) -> re.Pattern[str]:
    """按词边界匹配一个词或短语（前后不能紧挨字母数字）；网址类的残留按原样找。"""
    if term.endswith(("://", ".")):
        return re.compile(re.escape(term), re.IGNORECASE)
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _banned() -> list[tuple[str, re.Pattern[str]]]:
    terms = rules()["banned_terms"]
    out = []
    for group in ("material_and_claims", "minor", "platform_residue"):
        out += [(t, _term_re(t)) for t in terms[group]]
    return out


@functools.lru_cache(maxsize=1)
def bed_use_re() -> re.Pattern[str]:
    return re.compile(rules()["bed_use_regex"], re.IGNORECASE)


def first_words(title: str, n: int | None = None) -> str:
    return " ".join(title.split()[: n or rules()["hard_limits"]["first_words"]])


def check_title(
    title: str,
    *,
    limit: int | None = None,
    is_custom: bool = False,
    holiday: str | None = None,
    gift_led: bool = False,
) -> list[str]:
    """按原 skill 的硬规则复核一条标题，返回没过的项（空 = 合规）。"""
    hard = rules()["hard_limits"]
    text = (title or "").strip()
    problems: list[str] = []
    if not text:
        return ["标题是空的"]
    cap = min(int(hard["max_length_exclusive"]), int(limit) + 1 if limit else 10**6)
    if len(text) >= cap:
        problems.append(f"长度 {len(text)}，要少于 {cap} 个字符")
    lead = first_words(text)
    if hard["must_in_first_words"] not in lead:
        problems.append(f"前 {hard['first_words']} 个词里要有 {hard['must_in_first_words']}（现在是「{lead}」）")
    count = text.count(hard["must_once"])
    if count != 1:
        problems.append(f"{hard['must_once']} 要恰好出现一次（现在 {count} 次）")
    bed = bed_use_re().search(text)
    if bed:
        problems.append(f"不能有床品 / 睡眠用词：{bed.group(0)}")
    for term, pattern in _banned():
        found = pattern.search(text)
        if found and not (bed and found.group(0).lower() == bed.group(0).lower()):
            problems.append(f"不能有「{found.group(0)}」（禁用词 {term}）")
    words = text.split()
    custom_word = hard["custom_first_word"]
    if is_custom and (not words or words[0] != custom_word):
        problems.append(f"定制款首词要是 {custom_word}")
    if not is_custom and re.search(r"(?<![A-Za-z])(custom|personalized)(?![A-Za-z])", text, re.IGNORECASE):
        problems.append("不是定制款，不能写 Custom / Personalized")
    if holiday:
        words_ok = [w for h in rules()["holidays"] if _same_holiday(h, holiday) for w in h["words"]]
        words_ok = words_ok or [holiday]
        if not any(w.lower() in lead.lower() for w in words_ok):
            problems.append(f"节日款要把节日词（{holiday}）放在前 {hard['first_words']} 个词里")
    if gift_led and not re.search(rf"(?<![A-Za-z]){hard['gift_word']}", text):
        problems.append(f"礼品款要有 {hard['gift_word']}")
    return problems


def _same_holiday(item: dict[str, Any], holiday: str) -> bool:
    key = holiday.strip().lower()
    return key == item["name"].lower() or any(key == w.lower() for w in item["words"])


def rules_for_prompt() -> dict[str, Any]:
    data = dict(rules())
    data.pop("source", None)
    data.pop("version", None)
    return data


def get(ctx: Ctx, asset_id: str) -> dict[str, Any] | None:
    row = ctx.db.q1("SELECT * FROM listing_titles WHERE asset_id = ?", (asset_id,))
    if not row:
        return None
    out = dict(row)
    out["meta"] = loads(out.pop("meta_json"), {}) or {}
    out["problems"] = loads(out.pop("problems_json"), []) or []
    return out


def generate(
    ctx: Ctx, asset_id: str, image: bytes, *, limit: int | None = None
) -> dict[str, Any]:
    """P15 看上架轮播图第 1 张写标题，按规则复核（不合格重答），存进 listing_titles。返回存下的那一行。"""

    def validate(result: dict[str, Any]) -> list[str]:
        return check_title(
            str(result.get("title") or ""),
            limit=limit,
            is_custom=bool(result.get("is_custom")),
            holiday=result.get("holiday"),
            gift_led=bool(result.get("gift_led")),
        )

    out = llm.run_stage(
        ctx,
        STAGE,
        scope_id=f"listing:{asset_id}",
        job_id=f"listing:{asset_id}",
        variables={
            "title_rules": rules_for_prompt(),
            "title_limit": int(limit or rules()["hard_limits"]["max_length_exclusive"] - 1),
        },
        images={"candidate_image": [vision_image(ctx, image, f"{asset_id} 上架轮播图第 1 张")]},
        asset_id=asset_id,
        extra_validate=validate,
    )
    result = out.envelope["result"]
    meta = {
        "is_custom": bool(result.get("is_custom")),
        "lead_type": result.get("lead_type"),
        "holiday": result.get("holiday"),
        "gift_led": bool(result.get("gift_led")),
        "theme": result.get("theme"),
    }
    return _save(
        ctx,
        asset_id,
        str(result["title"]).strip(),
        source=SOURCE_AUTO,
        meta=meta,
        model=out.model,
        call_ids=out.call_ids,
    )


def human_problems(
    ctx: Ctx, asset_id: str, title: str, *, limit: int | None = None, custom: bool | None = None
) -> list[str]:
    """人改的标题按完整规则复核：定制款 / 节日款 / 礼品款按这一款原来的判定（custom 给了就按它）。"""
    meta = dict((get(ctx, asset_id) or {}).get("meta") or {})
    return check_title(
        title,
        limit=limit,
        is_custom=bool(meta.get("is_custom")) if custom is None else custom,
        holiday=meta.get("holiday"),
        gift_led=bool(meta.get("gift_led")),
    )


def save_human(
    ctx: Ctx,
    asset_id: str,
    title: str,
    *,
    by: str,
    limit: int | None = None,
    via: str = "selected",
    custom: bool | None = None,
) -> dict[str, Any]:
    """人改过的标题（「选中的款」/「已上架的款」/ 对话）：按完整规则复核，合规就存下（source human），
    不合规记一个不合规的版本、抛 StageError 带原因（库里的标题不动）。custom：顺带改定制款判定。"""
    old = get(ctx, asset_id) or {}
    meta = dict(old.get("meta") or {})
    text = (title or "").strip()
    problems = human_problems(ctx, asset_id, text, limit=limit, custom=custom)
    if problems:
        record_version(ctx, asset_id, text, source=SOURCE_HUMAN, by=by, via=via, problems=problems)
        raise StageError("上架标题不合规：" + "；".join(problems[:4]))
    meta["edited_by"] = by
    if custom is not None:
        meta.update(is_custom=custom, custom_set_by=by)
    return _save(ctx, asset_id, text, source=SOURCE_HUMAN, meta=meta, by=by, via=via)


def record_version(
    ctx: Ctx,
    asset_id: str,
    title: str,
    *,
    source: str,
    by: str | None,
    via: str | None,
    problems: list[str] | None = None,
    conn: Any = None,
) -> int:
    """记一个标题版本（只追加）；problems 非空 = 不合规（没用上）。返回版本号。"""

    def write(c: Any) -> int:
        c.execute(
            "INSERT INTO listing_title_versions(asset_id, title, source, made_by, via, check_ok, problems_json, at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, title, source, by, via, 0 if problems else 1, dumps(problems or []), now_iso()),
        )
        version = c.execute(
            "SELECT MAX(version_id) FROM listing_title_versions WHERE asset_id = ?", (asset_id,)
        ).fetchone()[0]
        trace.record(
            c,
            asset_id,
            "title",
            to="rejected" if problems else source,
            actor=by,
            source=via,
            reason="；".join((problems or [])[:4]) or None,
            payload={"version": version, "chars": len(title), "title": title},
        )
        return int(version)

    if conn is not None:
        return write(conn)
    with ctx.db.tx() as c:
        return write(c)


def versions(ctx: Ctx, asset_id: str) -> list[dict[str, Any]]:
    out = []
    for r in ctx.db.q(
        "SELECT * FROM listing_title_versions WHERE asset_id = ? ORDER BY version_id", (asset_id,)
    ):
        item = dict(r)
        item["problems"] = loads(item.pop("problems_json"), []) or []
        out.append(item)
    return out


def latest_version(ctx: Ctx, asset_id: str, *, ok_only: bool = True) -> dict[str, Any] | None:
    sql = "SELECT * FROM listing_title_versions WHERE asset_id = ?"
    if ok_only:
        sql += " AND check_ok = 1"
    row = ctx.db.q1(sql + " ORDER BY version_id DESC LIMIT 1", (asset_id,))
    return dict(row) if row else None


def set_custom(ctx: Ctx, asset_id: str, custom: bool, *, by: str) -> dict[str, Any]:
    """人改定制款判定（模型判错时）：标题按新判定复核，不合规抛 StageError（先改标题再改判定）。"""
    old = get(ctx, asset_id)
    if not old:
        raise StageError(f"{asset_id} 还没有上架标题")
    problems = check_title(old["title"], is_custom=custom)
    if problems:
        raise StageError(
            "按新判定标题不合规：改标题时一起改判定（`temu title IMG… --set \"…\" --custom yes|no`）："
            + "；".join(problems[:4])
        )
    meta = {**old["meta"], "is_custom": custom, "custom_set_by": by}
    return _save(ctx, asset_id, old["title"], source=old["source"], meta=meta, by=by, via="chat", version=False)


def _save(
    ctx: Ctx,
    asset_id: str,
    title: str,
    *,
    source: str,
    meta: dict[str, Any],
    model: str | None = None,
    call_ids: list[str] | None = None,
    by: str | None = None,
    via: str | None = None,
    version: bool = True,
) -> dict[str, Any]:
    now = now_iso()
    with ctx.db.tx() as conn:
        if version:
            record_version(
                ctx,
                asset_id,
                title,
                source=source,
                by=by or (model if source == SOURCE_AUTO else None),
                via=via or ("p15" if source == SOURCE_AUTO else None),
                conn=conn,
            )
        conn.execute(
            "INSERT INTO listing_titles(asset_id, title, source, meta_json, problems_json, model, call_ids_json,"
            " created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(asset_id) DO UPDATE SET title = excluded.title, source = excluded.source,"
            " meta_json = excluded.meta_json, problems_json = excluded.problems_json,"
            " model = COALESCE(excluded.model, listing_titles.model),"
            " call_ids_json = COALESCE(excluded.call_ids_json, listing_titles.call_ids_json),"
            " updated_at = excluded.updated_at",
            (
                asset_id,
                title,
                source,
                dumps(meta),
                dumps([]),
                model,
                dumps(call_ids) if call_ids else None,
                now,
                now,
            ),
        )
        log_event(
            conn,
            asset_id,
            "listing_title_saved",
            {"source": source, "chars": len(title), "custom": meta.get("is_custom")},
        )
    return get(ctx, asset_id) or {}
