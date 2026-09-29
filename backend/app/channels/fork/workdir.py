"""会话工作目录辅助：扫描可选项目目录、归一化用户输入。

「真实路径」指 gateway 容器内的挂载点（如 /projects），「虚拟路径」指 agent/sandbox 视角的路径
（如 /mnt/projects），两者的映射来自 config.yaml 的 ``sandbox.mounts``：每一条 mount 都是一个项目根。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置缺失时的兜底默认值（与 docker-compose.cursor-agent.yaml 的默认挂载一致）
DEFAULT_HOST_DIR = "/projects"
DEFAULT_VIRTUAL_PREFIX = "/mnt/projects"

# 扫描时排除的目录名（长任务日志目录）；隐藏目录另行排除
_EXCLUDED_DIR_NAMES = {".tasks"}


def _project_mounts() -> list[tuple[str, str]]:
    """返回所有项目根的 (容器内真实目录, agent 虚拟前缀)；无配置时回退默认单根。"""
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
    """扫描所有项目根，返回可选工作目录的虚拟路径（按根顺序、名称排序）。"""
    workdirs: list[str] = []
    for host_dir, virtual_prefix in _project_mounts():
        base = Path(host_dir)
        if not base.is_dir():
            continue
        prefix = virtual_prefix.rstrip("/")
        try:
            entries = sorted((e for e in base.iterdir() if _is_selectable_dir(e)), key=lambda e: e.name)
        except OSError:
            logger.warning("scan project root failed: %s", host_dir, exc_info=True)
            continue
        workdirs.extend(f"{prefix}/{e.name}" for e in entries)
    return workdirs


def normalize_workdir(raw: str) -> str | None:
    """把用户输入归一化为有效的虚拟工作目录路径；目录在容器内不存在时返回 None。

    接受完整虚拟路径（/mnt/projects/demo）或裸仓库名（demo，在所有项目根中取首个同名目录）。
    只允许挂载目录的直接子目录，拒绝路径穿越。
    """
    candidate = raw.strip().rstrip("/")
    if not candidate:
        return None

    mounts = _project_mounts()

    for host_dir, virtual_prefix in mounts:
        prefix = virtual_prefix.rstrip("/")
        if candidate.startswith(prefix + "/"):
            name = candidate[len(prefix) + 1 :]
            if not name or "/" in name or name.startswith("."):
                return None
            return f"{prefix}/{name}" if (Path(host_dir) / name).is_dir() else None

    if "/" not in candidate and not candidate.startswith("."):
        for host_dir, virtual_prefix in mounts:
            if (Path(host_dir) / candidate).is_dir():
                return f"{virtual_prefix.rstrip('/')}/{candidate}"

    return None
