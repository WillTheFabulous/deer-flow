"""时间、JSON 与哈希的小工具。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value)


def local_day(tz: str, ts: dt.datetime | None = None) -> str:
    """预算日界线：按配置时区取日期。"""
    return (ts or utcnow()).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PUNCT_RE = re.compile(
    r"[\s\u3000，。、；：！？,.;:!?'\"“”‘’（）()\[\]【】<>《》·\-—_/\\|]+"
)


def plain(value: Any) -> str:
    """字段取值转成一句话：字符串原样；对象优先取 value，否则拼起除编号外的各项。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "value" in value:
            return plain(value["value"])
        return "，".join(
            plain(v) for k, v in value.items() if k != "option_id" and plain(v)
        )
    if isinstance(value, list | tuple):
        return "、".join(plain(v) for v in value if plain(v))
    return str(value)


def clip(text: Any, limit: int) -> str:
    s = " ".join(plain(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def normalize_phrase(value: Any) -> str:
    """计划指纹用的规范化：去掉大小写、全半角、空白和标点差异，措辞微调不产生新指纹。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = canonical_json(value)
    text = unicodedata.normalize("NFKC", value).lower()
    return _PUNCT_RE.sub("", text)
