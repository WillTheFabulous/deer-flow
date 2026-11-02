#!/usr/bin/env python3
"""POD 毛毯流水线命令行入口：python /mnt/skills/public/pod-blanket/scripts/podctl.py <命令>。"""

import os
import sys
from pathlib import Path

REQUIRED_MODULES = ("yaml", "httpx", "PIL", "numpy", "jsonschema")


def _ensure_interpreter() -> None:
    """沙箱里的 python 不一定是 gateway 的 venv：缺依赖时换到 venv 解释器重新执行，绝不 pip install。"""
    try:
        for name in REQUIRED_MODULES:
            __import__(name)
        return
    except ImportError as exc:
        missing = exc.name
    if os.environ.get("PODFLOW_REEXEC") != "1":
        here = Path(__file__).resolve()
        candidates = []
        if os.environ.get("VIRTUAL_ENV"):
            candidates.append(Path(os.environ["VIRTUAL_ENV"]) / "bin" / "python")
        if os.environ.get("DEER_FLOW_PROJECT_ROOT"):
            candidates.append(
                Path(os.environ["DEER_FLOW_PROJECT_ROOT"])
                / "backend"
                / ".venv"
                / "bin"
                / "python"
            )
        candidates.append(here.parents[4] / "backend" / ".venv" / "bin" / "python")
        for cand in candidates:
            if (
                cand.is_file()
                and os.access(cand, os.X_OK)
                and Path(sys.executable).absolute() != cand.absolute()
            ):
                os.environ["PODFLOW_REEXEC"] = "1"
                os.execv(str(cand), [str(cand), str(here), *sys.argv[1:]])
    sys.stdout.write(
        '{"error": "missing_dependencies", "detail": "当前 Python 缺少 %s，且找不到 gateway 的 venv。'
        '不要 pip install，把这条报错转告用户/运维。"}\n' % missing
    )
    sys.exit(1)


_ensure_interpreter()
sys.path.insert(0, str(Path(__file__).resolve().parent))

from podflow.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
