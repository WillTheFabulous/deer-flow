"""渠道层记忆视图渲染（文本命令与飞书卡片回调共用）。

把记忆的范围解析、人设→agent_name 映射与文本格式化抽成纯同步函数，
供 ChannelManager（文本 /memory）与 FeishuChannel（交互卡片回调，运行在
lark 线程内）共同复用，避免逻辑重复，且不依赖 manager 以规避循环导入。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.channels.personas import DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL
from deerflow.config.agents_config import AGENT_NAME_PATTERN
from deerflow.runtime.user_context import DEFAULT_USER_ID

# 可选记忆范围：当前用户全局 / 跨用户共享 default 桶 / 当前用户+当前人设
MEMORY_SCOPES = ("global", "default", "persona")

_MEMORY_FACTS_DISPLAY_LIMIT = 50


def persona_to_agent_name(persona: str | None) -> str | None:
    """把人设名映射为记忆用的 agent_name：默认人设→None（全局），自定义人设→归一化名。

    与 manager._resolve_run_params 的映射逻辑镜像一致，保证读到的记忆桶与实际写入的一致。
    """
    if not persona or persona == DEFAULT_PERSONA:
        return None
    normalized = persona.strip().lower().replace("_", "-")
    if normalized and AGENT_NAME_PATTERN.match(normalized):
        return normalized
    return None


def persona_label(persona: str | None) -> str:
    """人设展示名：默认人设显示「通用助手」，自定义人设直接显示其名字。"""
    if not persona or persona == DEFAULT_PERSONA:
        return DEFAULT_PERSONA_LABEL
    return persona


def format_memory(data: dict[str, Any]) -> str:
    """把记忆数据格式化成面向用户的中文文本（逐条列出事实 + 画像/历史摘要）。"""

    def _summary(section: Any) -> str:
        return section.get("summary", "").strip() if isinstance(section, Mapping) else ""

    raw_facts = data.get("facts") if isinstance(data, Mapping) else None
    # 仅保留有内容的事实，使表头计数与实际列出条数一致
    facts = [f for f in raw_facts if isinstance(f, Mapping) and str(f.get("content", "")).strip()] if isinstance(raw_facts, list) else []
    user = data.get("user") if isinstance(data, Mapping) else {}
    history = data.get("history") if isinstance(data, Mapping) else {}
    user = user if isinstance(user, Mapping) else {}
    history = history if isinstance(history, Mapping) else {}

    lines: list[str] = []

    if facts:
        lines.append(f"记忆事实（共 {len(facts)} 条）：")
        for idx, fact in enumerate(facts[:_MEMORY_FACTS_DISPLAY_LIMIT], 1):
            content = str(fact.get("content", "")).strip()
            meta: list[str] = []
            category = str(fact.get("category", "")).strip()
            if category and category != "context":
                meta.append(category)
            confidence = fact.get("confidence")
            if isinstance(confidence, (int, float)):
                meta.append(f"置信度{confidence:.0%}")
            suffix = f"（{'·'.join(meta)}）" if meta else ""
            lines.append(f"{idx}. {content}{suffix}")
        if len(facts) > _MEMORY_FACTS_DISPLAY_LIMIT:
            lines.append(f"…还有 {len(facts) - _MEMORY_FACTS_DISPLAY_LIMIT} 条，请用 Web 端查看全部")

    profile = [
        (label, _summary(user.get(key)))
        for label, key in (("工作", "workContext"), ("个人", "personalContext"), ("当前关注", "topOfMind"))
    ]
    profile = [(label, summary) for label, summary in profile if summary]
    if profile:
        if lines:
            lines.append("")
        lines.append("用户画像：")
        lines.extend(f"- {label}：{summary}" for label, summary in profile)

    background = [
        (label, _summary(history.get(key)))
        for label, key in (("近期", "recentMonths"), ("早期", "earlierContext"), ("长期", "longTermBackground"))
    ]
    background = [(label, summary) for label, summary in background if summary]
    if background:
        if lines:
            lines.append("")
        lines.append("历史背景：")
        lines.extend(f"- {label}：{summary}" for label, summary in background)

    if not lines:
        return "暂无记忆。"
    return "\n".join(lines)


def memory_scope_header(scope: str, persona: str | None = None) -> str | None:
    """返回某个范围的中文表头；未知范围返回 None。"""
    if scope == "default":
        return "【跨用户共享记忆 · default】"
    if scope == "global":
        return "【当前用户全局记忆】"
    if scope == "persona":
        label = persona_label(persona)
        # 人设为通用助手时其记忆即全局记忆，提示用户避免误解
        suffix = "（通用助手即全局记忆）" if persona_to_agent_name(persona) is None else ""
        return f"【当前人设记忆：{label}】{suffix}"
    return None


def render_memory(scope: str, *, user_id: str, persona: str | None = None) -> str:
    """按 scope 读取并渲染记忆为中文文本。

    Args:
        scope: global / default / persona 之一。
        user_id: 当前用户 id（调用方负责回落到 DEFAULT_USER_ID）。
        persona: 当前会话人设名（仅 persona 范围使用）。

    - global：当前用户全局记忆（agent_name=None, user_id=当前用户）
    - default：跨用户共享 default 桶（agent_name=None, user_id=default）
    - persona：当前用户 + 当前人设记忆（agent_name=当前人设, user_id=当前用户）
    """
    # 延迟导入，避免渠道模块加载时拉起重量级 deerflow 记忆栈
    from deerflow.agents.memory.updater import get_memory_data

    scope = (scope or "global").lower()
    header = memory_scope_header(scope, persona)
    if header is None:
        return "未知的记忆范围：" + scope + "\n可用范围：global（当前用户全局）| default（跨用户共享）| persona（当前用户+当前人设）"

    if scope == "default":
        agent_name: str | None = None
        resolved_user_id = DEFAULT_USER_ID
    elif scope == "persona":
        agent_name = persona_to_agent_name(persona)
        resolved_user_id = user_id
    else:  # global
        agent_name = None
        resolved_user_id = user_id

    try:
        data = get_memory_data(agent_name, user_id=resolved_user_id)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Failed to load memory (scope=%s, agent_name=%s, user_id=%s)", scope, agent_name, resolved_user_id
        )
        return "读取记忆失败，请稍后再试。"

    return f"{header}\n\n{format_memory(data)}"
