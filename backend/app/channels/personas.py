"""会话人设（Agent/Persona）辅助：列出可选人设、把用户输入归一化为有效人设名。

人设 = lead_agent（通用助手）或某个自定义 agent（如 software-team）。
渠道层据此决定一次运行用哪个 assistant_id / agent_name（见 manager._resolve_run_params）。
新增人设只需在 {DEER_FLOW_HOME}/agents/<name>/ 放 config.yaml + SOUL.md，会自动出现在菜单里。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 通用助手（默认人设）：对应 lead_agent，无 SOUL，适合日常问答与通用任务
DEFAULT_PERSONA = "lead_agent"
DEFAULT_PERSONA_LABEL = "通用助手"
DEFAULT_PERSONA_DESC = "默认通用助手，适合日常问答与通用任务"

# 选择默认人设时用户可能输入的别名
_DEFAULT_ALIASES = {"lead_agent", "lead-agent", "default", "通用", "通用助手", "general"}


def _safe_list_custom_agents() -> list:
    """读取自定义 agent 列表；失败时返回空列表（不阻塞渠道）。"""
    try:
        from deerflow.config.agents_config import list_custom_agents

        return list(list_custom_agents())
    except Exception:
        logger.debug("list_custom_agents failed", exc_info=True)
        return []


def list_personas() -> list[tuple[str, str, str]]:
    """返回可选人设列表：(name, label, description)。

    固定第一项为通用助手（lead_agent），其后是所有自定义 agent（按名称）。
    """
    personas: list[tuple[str, str, str]] = [(DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL, DEFAULT_PERSONA_DESC)]
    for agent in _safe_list_custom_agents():
        name = getattr(agent, "name", None)
        if not isinstance(name, str) or not name:
            continue
        description = getattr(agent, "description", "") or ""
        personas.append((name, name, description))
    return personas


def normalize_persona(raw: str) -> str | None:
    """把用户输入归一化为有效人设名。

    - 通用助手别名（lead_agent / default / 通用 ...）→ DEFAULT_PERSONA。
    - 自定义 agent 名：小写化、`_`→`-`（与 manager 的 assistant_id 归一化一致），
      且必须存在于自定义 agent 列表中，否则返回 None。
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None

    low = candidate.lower()
    if low in _DEFAULT_ALIASES:
        return DEFAULT_PERSONA

    norm = low.replace("_", "-")
    try:
        from deerflow.config.agents_config import validate_agent_name

        validate_agent_name(norm)
    except Exception:
        return None

    available = {getattr(a, "name", "").lower() for a in _safe_list_custom_agents()}
    if norm in available:
        return norm
    return None
