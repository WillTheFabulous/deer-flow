"""记忆视图渲染（文本 /memory 与飞书记忆卡片共用）。

纯同步函数，不依赖 ChannelManager，飞书卡片回调（lark 线程）可直接调用。
读取走上游可插拔的 memory manager，桶按 (user_id, agent_name) 划分。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.channels.fork.personas import DEFAULT_PERSONA, persona_label
from deerflow.runtime.user_context import DEFAULT_USER_ID

logger = logging.getLogger(__name__)

# 可选记忆范围：当前用户全局 / 跨用户共享 default 桶 / 当前用户 + 当前人设
MEMORY_SCOPES = ("global", "default", "persona")

_MEMORY_FACTS_DISPLAY_LIMIT = 50


def persona_to_agent_name(persona: str | None) -> str | None:
    """人设名 → 记忆用 agent_name：通用助手 → None（全局桶），自定义人设 → 归一化名。"""
    if not persona or persona == DEFAULT_PERSONA:
        return None
    from deerflow.config.agents_config import AGENT_NAME_PATTERN

    normalized = persona.strip().lower().replace("_", "-")
    return normalized if normalized and AGENT_NAME_PATTERN.match(normalized) else None


def _summary(section: Any) -> str:
    return section.get("summary", "").strip() if isinstance(section, Mapping) else ""


def format_memory(data: Mapping[str, Any]) -> str:
    """把记忆文档格式化为中文文本：事实逐条列出，再附用户画像与历史背景摘要。"""
    raw_facts = data.get("facts")
    facts = [f for f in raw_facts if isinstance(f, Mapping) and str(f.get("content", "")).strip()] if isinstance(raw_facts, list) else []
    user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
    history = data.get("history") if isinstance(data.get("history"), Mapping) else {}

    lines: list[str] = []
    if facts:
        lines.append(f"记忆事实（共 {len(facts)} 条）：")
        for idx, fact in enumerate(facts[:_MEMORY_FACTS_DISPLAY_LIMIT], 1):
            meta: list[str] = []
            category = str(fact.get("category", "")).strip()
            if category and category != "context":
                meta.append(category)
            confidence = fact.get("confidence")
            if isinstance(confidence, (int, float)):
                meta.append(f"置信度{confidence:.0%}")
            suffix = f"（{'·'.join(meta)}）" if meta else ""
            lines.append(f"{idx}. {str(fact.get('content', '')).strip()}{suffix}")
        if len(facts) > _MEMORY_FACTS_DISPLAY_LIMIT:
            lines.append(f"…还有 {len(facts) - _MEMORY_FACTS_DISPLAY_LIMIT} 条，请用 Web 端查看全部")

    for heading, section, keys in (
        ("用户画像：", user, (("工作", "workContext"), ("个人", "personalContext"), ("当前关注", "topOfMind"))),
        ("历史背景：", history, (("近期", "recentMonths"), ("早期", "earlierContext"), ("长期", "longTermBackground"))),
    ):
        rows = [(label, _summary(section.get(key))) for label, key in keys]
        rows = [(label, text) for label, text in rows if text]
        if rows:
            if lines:
                lines.append("")
            lines.append(heading)
            lines.extend(f"- {label}：{text}" for label, text in rows)

    return "\n".join(lines) if lines else "暂无记忆。"


def memory_scope_header(scope: str, persona: str | None = None) -> str | None:
    """某个范围的中文表头；未知范围返回 None。"""
    if scope == "default":
        return "【跨用户共享记忆 · default】"
    if scope == "global":
        return "【当前用户全局记忆】"
    if scope == "persona":
        suffix = "（通用助手即全局记忆）" if persona_to_agent_name(persona) is None else ""
        return f"【当前人设记忆：{persona_label(persona)}】{suffix}"
    return None


def render_memory(scope: str, *, user_id: str | None, persona: str | None = None) -> str:
    """按范围读取并渲染记忆。

    - global：当前用户全局（agent_name=None, user_id=当前用户）
    - default：跨用户共享 default 桶（agent_name=None, user_id=default）
    - persona：当前用户 + 当前人设（agent_name=当前人设, user_id=当前用户）
    """
    scope = (scope or "global").lower()
    header = memory_scope_header(scope, persona)
    if header is None:
        return f"未知的记忆范围：{scope}\n可用范围：global（当前用户全局）| default（跨用户共享）| persona（当前用户+当前人设）"

    agent_name = persona_to_agent_name(persona) if scope == "persona" else None
    resolved_user_id = DEFAULT_USER_ID if scope == "default" or not user_id else user_id

    # 延迟导入：渠道模块加载时不拉起记忆后端
    from deerflow.agents.memory.manager import get_memory_manager

    try:
        data = get_memory_manager().get_memory(user_id=resolved_user_id, agent_name=agent_name)
    except NotImplementedError:
        return f"{header}\n\n当前记忆后端不支持查看完整记忆，请在 Web 端查看。"
    except Exception:
        logger.exception("Failed to load memory (scope=%s, agent_name=%s, user_id=%s)", scope, agent_name, resolved_user_id)
        return "读取记忆失败，请稍后再试。"

    return f"{header}\n\n{format_memory(data if isinstance(data, Mapping) else {})}"
