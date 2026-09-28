"""Tests for the fork's per-subagent thinking mode override.

Covers:
- CustomSubagentConfig / SubagentOverrideConfig ``thinking_enabled`` fields
- SubagentsAppConfig.get_thinking_for()
- Registry: custom agent passthrough and per-agent overrides
- resolve_subagent_thinking_enabled() fallback for models that cannot think
"""

from types import SimpleNamespace

from deerflow.config.subagents_config import (
    CustomSubagentConfig,
    SubagentOverrideConfig,
    get_subagents_app_config,
    load_subagents_config_from_dict,
)
from deerflow.subagents.config import SubagentConfig, resolve_subagent_thinking_enabled


def _reset_subagents_config(**kwargs) -> None:
    load_subagents_config_from_dict(kwargs)


class TestThinkingConfigFields:
    def test_subagent_config_defaults_to_off(self):
        assert SubagentConfig(name="t", description="t").thinking_enabled is False

    def test_custom_subagent_defaults_to_off(self):
        custom = CustomSubagentConfig(description="d", system_prompt="p")
        assert custom.thinking_enabled is False

    def test_override_defaults_to_none(self):
        assert SubagentOverrideConfig().thinking_enabled is None


class TestGetThinkingFor:
    def teardown_method(self):
        _reset_subagents_config()

    def test_returns_none_without_override(self):
        _reset_subagents_config()
        assert get_subagents_app_config().get_thinking_for("general-purpose") is None

    def test_returns_explicit_override(self):
        load_subagents_config_from_dict({"agents": {"bash": {"thinking_enabled": True}, "general-purpose": {"thinking_enabled": False}}})
        cfg = get_subagents_app_config()
        assert cfg.get_thinking_for("bash") is True
        assert cfg.get_thinking_for("general-purpose") is False


class TestRegistryThinking:
    def teardown_method(self):
        _reset_subagents_config()

    def test_custom_agent_thinking_passthrough(self):
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict({"custom_agents": {"planner": {"description": "Plan", "system_prompt": "Plan.", "thinking_enabled": True}}})
        config = get_subagent_config("planner")
        assert config is not None
        assert config.thinking_enabled is True

    def test_override_enables_thinking_on_builtin_without_mutating_it(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict({"agents": {"general-purpose": {"thinking_enabled": True}}})
        assert get_subagent_config("general-purpose").thinking_enabled is True
        assert BUILTIN_SUBAGENTS["general-purpose"].thinking_enabled is False

    def test_override_disables_custom_agent_thinking(self):
        from deerflow.subagents.registry import get_subagent_config

        load_subagents_config_from_dict(
            {
                "custom_agents": {"planner": {"description": "Plan", "system_prompt": "Plan.", "thinking_enabled": True}},
                "agents": {"planner": {"thinking_enabled": False}},
            }
        )
        assert get_subagent_config("planner").thinking_enabled is False


def _config(*, thinking_enabled: bool) -> SubagentConfig:
    return SubagentConfig(name="planner", description="d", thinking_enabled=thinking_enabled)


def _app_config(model_config) -> SimpleNamespace:
    return SimpleNamespace(get_model_config=lambda name: model_config)


class TestResolveSubagentThinking:
    def test_disabled_never_requests_thinking(self):
        app_config = _app_config(SimpleNamespace(supports_thinking=True, reasoning=None))
        assert resolve_subagent_thinking_enabled(_config(thinking_enabled=False), "m", app_config=app_config) is False

    def test_enabled_on_thinking_model(self):
        app_config = _app_config(SimpleNamespace(supports_thinking=True, reasoning=None))
        assert resolve_subagent_thinking_enabled(_config(thinking_enabled=True), "m", app_config=app_config) is True

    def test_falls_back_when_model_cannot_think(self):
        app_config = _app_config(SimpleNamespace(supports_thinking=False, reasoning=None))
        assert resolve_subagent_thinking_enabled(_config(thinking_enabled=True), "m", app_config=app_config) is False

    def test_falls_back_when_model_unknown(self):
        assert resolve_subagent_thinking_enabled(_config(thinking_enabled=True), "m", app_config=_app_config(None)) is False
