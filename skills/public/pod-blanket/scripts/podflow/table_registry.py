"""数据表在哪本多维表格里：每周选款表（books.py）建的表都登记在业务库 table_places（table_id → app_token），
拼接口路径时按表找表格；没登记的表在旧表格（「POD 毛毯选款表」）里，用调用方给的 info["app_token"]。

拼路径的地方（views 的视图 / 字段路径、候选表的记录 / 字段路径、挪表、传缩略图）都走 app_for，所以候选表的同步
不用按表格分批：同一轮同步里旧表格和各本周表格的候选表一起处理。只登记程序复制、新建出来的表（table_id 唯一）；
周表格里从模板表格整本带过来的模板表 id 可能和模板表格里的一样，不登记，用到时直接拿那本表格的 info。

不依赖 podflow 的其他模块（views 等底层模块也要用）：打开上下文时 load(db)，登记新表时 register()，
查不到时回库里再查一次（别的进程刚登记的）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_APPS: dict[str, str] = {}
_DB: Any = None
# 这一轮在同步哪一份「选中的款」：(那一天或 None = 旧表格, 素材 → 归到哪天)；没设时不过滤
_SCOPE: ContextVar[tuple[str | None, Callable[[str], str | None]] | None] = ContextVar(
    "table_scope", default=None
)


@contextmanager
def scope(day: str | None, resolve: Callable[[str], str | None]) -> Iterator[None]:
    """按份同步「选中的款」时设（books.scoped）：里面 mine() 只认归到这一份的素材。"""
    token = _SCOPE.set((day, resolve))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def mine(asset_id: str) -> bool:
    """这一款归不归现在同步的这一份「选中的款」；没在按份同步时都算。"""
    current = _SCOPE.get()
    if current is None:
        return True
    day, resolve = current
    return resolve(asset_id) == day


def scope_day() -> str | None:
    current = _SCOPE.get()
    return current[0] if current else None


def load(db: Any) -> None:
    """从业务库读全部登记（打开上下文时调）。"""
    global _DB
    _DB = db
    try:
        rows = db.q("SELECT table_id, app_token FROM table_places")
    except Exception:  # noqa: BLE001 - 库还没建表（init_schema 之前）时当作没有登记
        rows = []
    _APPS.clear()
    _APPS.update({r["table_id"]: r["app_token"] for r in rows})


def register(table_id: str, app_token: str) -> None:
    _APPS[table_id] = app_token


def forget(table_id: str) -> None:
    _APPS.pop(table_id, None)


def lookup(table_id: str | None) -> str | None:
    """这张表登记在哪本表格；没登记返回 None（在旧表格里）。"""
    if not table_id:
        return None
    if table_id in _APPS:
        return _APPS[table_id]
    if _DB is not None:
        try:
            row = _DB.q1(
                "SELECT app_token FROM table_places WHERE table_id = ?", (table_id,)
            )
        except Exception:  # noqa: BLE001 - 同上
            row = None
        if row:
            _APPS[table_id] = row["app_token"]
            return str(row["app_token"])
    return None


def app_for(info: dict[str, Any], table_id: str | None) -> str:
    """拼这张表的接口路径用的 app_token：登记过的按登记，否则用 info 的（旧表格，或调用方给的就是那本表格）。"""
    return lookup(table_id) or str(info["app_token"])
