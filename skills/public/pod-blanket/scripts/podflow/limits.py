"""出图 worker 进程内的并发名额：所有工位共用同一批生图 / LLM 名额。

- batch.image_concurrency：同时在途的生图请求（含修改）；
- batch.llm_concurrency：同时在途的结构化阶段请求（P06 生成指令、P11 批次反馈等）。

只有 worker 调了 configure 才生效；拆解、策划等别的进程不受限（它们各有并发设置）。
名额生效时，排队等名额和发长请求期间都先放下本线程的数据库连接（每条线程一条连接，库角色连接数有上限），
回来再连；重试退避的等待在名额外面，不占名额。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class Slots:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self._sem = threading.BoundedSemaphore(self.size)
        self._lock = threading.Lock()
        self.busy = 0
        self.peak = 0

    @contextmanager
    def hold(self) -> Iterator[None]:
        self._sem.acquire()
        with self._lock:
            self.busy += 1
            self.peak = max(self.peak, self.busy)
        try:
            yield
        finally:
            with self._lock:
                self.busy -= 1
            self._sem.release()


_image: Slots | None = None
_llm: Slots | None = None
# 后台同步（上传 OSS、同步表格）的「踢一下」：多条工位接连调时串行检查，worker 里刚拉起过就不再查
KICK_THROTTLE_SECONDS = 10.0
_kick_locks: dict[str, threading.Lock] = {}
_kicked: dict[str, float] = {}
_registry_lock = threading.Lock()


def configure(*, image: int, llm: int) -> None:
    global _image, _llm
    _image, _llm = Slots(image), Slots(llm)
    _kicked.clear()


def reset() -> None:
    global _image, _llm
    _image = _llm = None
    _kicked.clear()


def kick_lock(name: str) -> threading.Lock:
    with _registry_lock:
        return _kick_locks.setdefault(name, threading.Lock())


def recently_kicked(name: str) -> bool:
    """worker 里 KICK_THROTTLE_SECONDS 内拉起过同一个后台同步（它会一并处理新来的）；不在 worker 里不节流。"""
    if not active():
        return False
    at = _kicked.get(name)
    return at is not None and time.monotonic() - at < KICK_THROTTLE_SECONDS


def mark_kicked(name: str) -> None:
    if active():
        _kicked[name] = time.monotonic()


def active() -> bool:
    return _image is not None


def release_db(ctx: Any) -> None:
    """名额生效时放下本线程的数据库连接（下次用库时自动重连）。"""
    if active():
        ctx.db.close()


@contextmanager
def _slot(slots: Slots | None, ctx: Any) -> Iterator[None]:
    if slots is None:
        yield
        return
    ctx.db.close()
    with slots.hold():
        yield


def image_slot(ctx: Any):
    return _slot(_image, ctx)


def llm_slot(ctx: Any):
    return _slot(_llm, ctx)


def snapshot() -> dict[str, Any]:
    """心跳写进 worker_lease.info_json：现在占了几个名额、最多同时占过几个。"""
    out: dict[str, Any] = {}
    for name, slots in (("image", _image), ("llm", _llm)):
        if slots is not None:
            out[name] = {"busy": slots.busy, "size": slots.size, "peak": slots.peak}
    return out
