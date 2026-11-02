"""库里的进程租约：meta 一行 {owner, until}，同一时刻只让一个后台进程做某件事（上传 OSS、同步选款表）。

持有者在干活过程中反复 take 续租；进程死了租约到期自然失效，别的进程可以接手。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .util import dumps, iso, loads, parse_iso, utcnow


def active(ctx: Any, key: str) -> bool:
    lease = loads(ctx.db.get_meta(key), {}) or {}
    return bool(lease.get("until")) and parse_iso(lease["until"]) > utcnow()


def take(ctx: Any, key: str, seconds: int) -> bool:
    """拿到或续上租约；别的进程持有且没到期时返回 False。"""
    with ctx.db.tx() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        lease = loads(row[0], {}) if row else {}
        if (
            lease
            and lease.get("owner") != ctx.owner
            and parse_iso(lease["until"]) > utcnow()
        ):
            return False
        until = utcnow() + dt.timedelta(seconds=seconds)
        ctx.db.set_meta(conn, key, dumps({"owner": ctx.owner, "until": iso(until)}))
        return True


def drop(ctx: Any, key: str) -> None:
    """放掉自己持有的租约（别人的不动）。"""
    with ctx.db.tx() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row and (loads(row[0], {}) or {}).get("owner") == ctx.owner:
            ctx.db.del_meta(conn, key)
