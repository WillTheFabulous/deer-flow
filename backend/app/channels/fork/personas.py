"""会话人设辅助：列出可选人设（通用助手 + 归属用户的 Custom Agents）。

上游把人设钉在线程上：``/agent use <name>`` 会新建一个绑定该 agent 的会话，同一线程中途不换 agent。
这里只负责给卡片 / 文本列表提供候选项与展示名。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_PERSONA = "lead_agent"
DEFAULT_PERSONA_LABEL = "通用助手"
DEFAULT_PERSONA_DESC = "默认通用助手，适合日常问答与通用任务"

# 选择默认人设时用户可能输入的别名
DEFAULT_PERSONA_ALIASES = frozenset({"lead_agent", "lead-agent", "default", "clear", "reset", "通用", "通用助手", "general"})

MAX_PERSONAS = 50


def list_personas(user_id: str | None) -> list[tuple[str, str, str]]:
    """返回 (name, label, description)，第一项固定为通用助手，其后按名称排序。

    读取失败时只返回通用助手，不阻塞渠道。
    """
    personas: list[tuple[str, str, str]] = [(DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL, DEFAULT_PERSONA_DESC)]
    try:
        from deerflow.config.agents_config import list_custom_agents

        agents = sorted(list_custom_agents(user_id=user_id), key=lambda agent: agent.name)
    except Exception:
        logger.debug("list_custom_agents failed", exc_info=True)
        return personas
    for agent in agents[:MAX_PERSONAS]:
        description = " ".join((agent.description or "").split())
        personas.append((agent.name, agent.name, description))
    return personas


def persona_label(name: str | None) -> str:
    """人设展示名：通用助手 / 未设置显示「通用助手」，自定义人设显示其名字。"""
    if not name or name == DEFAULT_PERSONA:
        return DEFAULT_PERSONA_LABEL
    return name
