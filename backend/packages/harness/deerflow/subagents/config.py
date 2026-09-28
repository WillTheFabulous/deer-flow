"""Subagent configuration definitions."""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from deerflow.config.prompt_overlay import PromptOverlay

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class SubagentConfig:
    """Configuration for a subagent.

    Attributes:
        name: Unique identifier for the subagent.
        description: When Claude should delegate to this subagent.
        system_prompt: The system prompt that guides the subagent's behavior.
        tools: Optional list of tool names to allow. If None, inherits all tools.
        disallowed_tools: Optional list of tool names to deny.
        skills: Optional list of skill names to make discoverable and activatable.
                If None, all enabled skills are available. If empty, skills are
                disabled for this subagent. Skill bodies and their allowed-tools
                policies take effect only after activation/loading at runtime.
        model: Model to use - 'inherit' uses parent's model.
        thinking_enabled: Request the model's thinking/reasoning mode for this
            subagent. Falls back to off when the resolved model cannot think.
        max_turns: Maximum agent turns — model call plus the tools it runs —
            before stopping. Built-in agents use the value set here
            (general-purpose=150, bash=60) unless the global
            ``subagents.max_turns`` is set. ``turn_budget.py`` converts this
            into the LangGraph ``recursion_limit`` that buys that many turns
            through the assembled middleware chain; it is not passed through as
            a super-step count.
        timeout_seconds: Bare fallback execution-time cap. For built-in agents the
            effective limit is the global ``subagents.timeout_seconds`` (default
            1800 = 30 min), layered on by the registry; this 900 only applies
            when no differing global value exists.
        prompt_overlay: Operator instructions around the complete system message.
    """

    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["task"])
    skills: list[str] | None = None
    model: str = "inherit"
    # 是否为该子代理开启模型的 thinking/推理模式（默认关闭，保持内置代理行为不变）
    thinking_enabled: bool = False
    max_turns: int = 50
    timeout_seconds: int = 900
    prompt_overlay: PromptOverlay = field(default_factory=PromptOverlay)


def _default_model_name(app_config: "AppConfig") -> str:
    if not app_config.models:
        raise ValueError("No chat models are configured. Please configure at least one model in config.yaml.")
    return app_config.models[0].name


def resolve_subagent_model_name(config: SubagentConfig, parent_model: str | None, *, app_config: "AppConfig | None" = None) -> str:
    """Resolve the effective model name a subagent should use."""
    if config.model != "inherit":
        return config.model

    if parent_model is not None:
        return parent_model

    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()
    return _default_model_name(app_config)


def resolve_subagent_thinking_enabled(config: SubagentConfig, model_name: str | None, *, app_config: "AppConfig") -> bool:
    """Return the thinking flag a subagent should request for its model.

    Honors ``config.thinking_enabled`` but falls back to off when the resolved
    model cannot think: legacy model profiles make ``create_chat_model`` raise
    on that combination instead of degrading.
    """
    if not config.thinking_enabled:
        return False
    from deerflow.models.reasoning import resolve_reasoning_contract

    model_config = app_config.get_model_config(model_name) if model_name else None
    if model_config is None or not resolve_reasoning_contract(model_config).supports_thinking:
        logger.warning("Subagent %s requested thinking but model '%s' does not support it; falling back to non-thinking mode", config.name, model_name)
        return False
    return True
