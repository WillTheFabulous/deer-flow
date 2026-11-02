"""调用 pod-blanket 引擎（``podctl``）的薄封装：渠道只拿视图 JSON 渲染卡片，人工决定原样转交引擎。

podctl 自己定位数据目录（``$DEER_FLOW_HOME/pod-blanket``）、读 secrets.env；这里只负责定位脚本、
限时运行、解析 JSON。引擎的业务错误（退出码非 0 或输出带 ``error``）转成 :class:`PodctlError`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PODCTL_TIMEOUT_SECONDS = 30.0
_ERROR_CHARS = 300


class PodctlError(RuntimeError):
    pass


def podctl_path() -> Path:
    root = os.environ.get("PODFLOW_SKILL_ROOT")
    if root:
        return Path(root) / "scripts" / "podctl.py"
    project = os.environ.get("DEER_FLOW_PROJECT_ROOT") or str(Path(__file__).resolve().parents[4])
    return Path(project) / "skills" / "public" / "pod-blanket" / "scripts" / "podctl.py"


def _error_message(data: Any, stderr: bytes, returncode: int) -> str:
    if isinstance(data, dict) and data.get("error"):
        detail = data.get("detail") or data.get("reason") or data.get("errors")
        message = f"{data['error']}：{detail}" if detail else str(data["error"])
    else:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        message = tail[-1] if tail else f"podctl 退出码 {returncode}"
    return message[:_ERROR_CHARS]


class PodBridge:
    def __init__(self, *, timeout: float = PODCTL_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    async def run(self, *args: str) -> dict[str, Any]:
        script = podctl_path()
        if not script.is_file():
            raise PodctlError(f"找不到 pod-blanket 引擎：{script}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise PodctlError(f"podctl {args[0] if args else ''} 超时（{self.timeout:g} 秒）") from None
        try:
            data = json.loads(stdout.decode("utf-8") or "null")
        except ValueError:
            data = None
        if proc.returncode != 0 or not isinstance(data, dict) or data.get("error"):
            message = _error_message(data, stderr, proc.returncode or 0)
            logger.warning("[Pod] podctl %s failed: %s", " ".join(args[:2]), message)
            raise PodctlError(message)
        return data
