"""Fork：部署配置开着 host bash 时，deploy/fork/agents 下的人设不能设 skills 白名单。

上游（#5077）在 LocalSandboxProvider + allow_host_bash 下拒绝运行带 skills 白名单的 Custom Agent
（SandboxMiddleware 报 cannot enforce per-Agent skill filesystem isolation），飞书里只会显示 No response。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_PARENT = Path(__file__).resolve().parents[2]
# dev 容器把 backend 挂在 /app/backend、仓库根挂在 /app/project
_ROOT = next((p for p in (_PARENT, _PARENT / "project") if (p / "deploy" / "fork").is_dir()), None)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.mark.skipif(_ROOT is None, reason="没有 deploy/fork（不是完整的仓库检出）")
def test_personas_have_no_skill_allowlist_when_host_bash_is_allowed():
    sandbox = _load(_ROOT / "deploy" / "fork" / "config.yaml").get("sandbox") or {}
    if not sandbox.get("allow_host_bash") or not str(sandbox.get("use", "")).endswith(":LocalSandboxProvider"):
        pytest.skip("部署配置没有在 LocalSandboxProvider 上开 host bash")
    personas = sorted((_ROOT / "deploy" / "fork" / "agents").glob("*/config.yaml"))
    assert personas, "deploy/fork/agents 下至少应有一个人设"
    offenders = [p.parent.name for p in personas if _load(p).get("skills") is not None]
    assert not offenders, f"开着 host bash 时这些人设不能设 skills 白名单（上游会拒绝运行）：{offenders}"
