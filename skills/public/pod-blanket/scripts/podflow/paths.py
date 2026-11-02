"""skill 资源目录与运行数据目录的定位。"""

from __future__ import annotations

import os
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = SKILL_ROOT / "prompts"
SCHEMAS_DIR = SKILL_ROOT / "schemas"
REFERENCES_DIR = SKILL_ROOT / "references"
CONFIG_EXAMPLE = SKILL_ROOT / "config" / "podflow.example.yaml"
PODCTL = SKILL_ROOT / "scripts" / "podctl.py"


def stable_podctl() -> Path:
    """后台进程（worker / --detach）用的 podctl 路径。

    显式限定 skills 的人设拿到的 /mnt/skills/... 是按线程复制的视图，线程视图可能被重建或删除；
    长时间运行的后台进程要从原始 skill 目录启动（DEER_FLOW_PROJECT_ROOT/skills/public/<skill>）。
    """
    candidates = []
    if os.environ.get("PODFLOW_SKILL_ROOT"):
        candidates.append(Path(os.environ["PODFLOW_SKILL_ROOT"]))
    if os.environ.get("DEER_FLOW_PROJECT_ROOT"):
        candidates.append(
            Path(os.environ["DEER_FLOW_PROJECT_ROOT"])
            / "skills"
            / "public"
            / SKILL_ROOT.name
        )
    for root in candidates:
        if (root / "scripts" / "podctl.py").is_file():
            return root / "scripts" / "podctl.py"
    return PODCTL


def podflow_home() -> Path:
    """运行数据目录：PODFLOW_HOME > $DEER_FLOW_HOME/pod-blanket > 仓库 backend/.deer-flow/pod-blanket。

    沙箱 bash 不允许在命令里写 /app/... 这类绝对路径，所以数据目录只能由环境变量推导，
    人设的命令里永远不出现它。
    """
    explicit = os.environ.get("PODFLOW_HOME")
    if explicit:
        return Path(explicit)
    deer_flow_home = os.environ.get("DEER_FLOW_HOME")
    if deer_flow_home:
        return Path(deer_flow_home) / "pod-blanket"
    return SKILL_ROOT.parents[2] / "backend" / ".deer-flow" / "pod-blanket"
