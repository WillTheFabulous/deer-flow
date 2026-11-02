"""HTTP 客户端工厂：单测替换 make_client 注入 MockTransport，生产走真实网络。"""

from __future__ import annotations

import httpx


def make_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=30.0))
