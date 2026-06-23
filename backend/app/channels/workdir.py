"""会话工作目录辅助：扫描可选项目目录、虚拟路径与实际路径互转。

工作目录的「真实路径」指 gateway 容器内的挂载点（默认 /projects），
「虚拟路径」指 agent/sandbox 视角的路径（默认 /mnt/projects），
两者的映射关系来自 config.yaml 的 sandbox.mounts。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置缺失时的兜底默认值（与 docker-compose-dev.yaml / config.yaml 默认一致）
DEFAULT_HOST_DIR = "/projects"
DEFAULT_VIRTUAL_PREFIX = "/mnt/projects"

# 扫描时排除的目录名（隐藏目录与长任务日志目录）
_EXCLUDED_DIR_NAMES = {".tasks"}


def _project_mounts() -> list[tuple[str, str]]:
    """返回所有项目根的 (容器内真实目录, agent 虚拟前缀) 列表。

    每个 sandbox.mounts 条目都被视为一个项目根；无配置时回退默认单根。
    """
    try:
        from deerflow.config.app_config import get_app_config

        mounts = get_app_config().sandbox.mounts or []
        result = [(m.host_path, m.container_path) for m in mounts if m.host_path and m.container_path]
        if result:
            return result
    except Exception:
        logger.debug("read sandbox.mounts failed, falling back to defaults", exc_info=True)
    return [(DEFAULT_HOST_DIR, DEFAULT_VIRTUAL_PREFIX)]


def _is_selectable_dir(entry: Path) -> bool:
    return entry.is_dir() and not entry.name.startswith(".") and entry.name not in _EXCLUDED_DIR_NAMES


def list_project_workdirs() -> list[str]:
    """扫描所有项目根，返回可选工作目录的虚拟路径列表（按根顺序、名称排序）。"""
    workdirs: list[str] = []
    for host_dir, virtual_prefix in _project_mounts():
        base = Path(host_dir)
        if not base.is_dir():
            continue
        vp = virtual_prefix.rstrip("/")
        try:
            entries = sorted((e for e in base.iterdir() if _is_selectable_dir(e)), key=lambda e: e.name)
        except OSError:
            logger.warning("scan project root failed: %s", host_dir, exc_info=True)
            continue
        workdirs.extend(f"{vp}/{e.name}" for e in entries)
    return workdirs


def normalize_workdir(raw: str) -> str | None:
    """把用户输入归一化为有效的虚拟工作目录路径。

    接受两种形式：
    - 完整虚拟路径：/mnt/projects/demo、/mnt/prod_data/xxx
    - 仅仓库名：demo（在所有项目根中查找首个同名目录）

    目录在容器内真实存在才返回，否则返回 None。
    """
    candidate = raw.strip().rstrip("/")
    if not candidate:
        return None

    mounts = _project_mounts()

    # 完整虚拟路径：匹配对应根的前缀
    for host_dir, virtual_prefix in mounts:
        vp = virtual_prefix.rstrip("/")
        if candidate.startswith(vp + "/"):
            name = candidate[len(vp) + 1 :]
            # 拒绝路径穿越与子路径（只允许挂载目录的直接子目录）
            if not name or "/" in name or name.startswith("."):
                return None
            if (Path(host_dir) / name).is_dir():
                return f"{vp}/{name}"
            return None

    # 裸名称：在所有根里查找首个存在的同名目录
    if "/" not in candidate and not candidate.startswith("."):
        for host_dir, virtual_prefix in mounts:
            if (Path(host_dir) / candidate).is_dir():
                return f"{virtual_prefix.rstrip('/')}/{candidate}"

    return None
