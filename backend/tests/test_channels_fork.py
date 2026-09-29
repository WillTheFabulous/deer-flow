"""Fork 渠道扩展测试（app/channels/fork）。

覆盖：
- ChannelStore 扩展字段：工作目录 / 人设 / 模型 / 多会话注册表 / open_id 映射，以及 set_thread_id 合并写
- 记忆视图与流式进度的纯函数
- ForkChannelManager：/repo /model /agent /sessions /memory /status /help、人设钉线程与兼容旧线程、流式进度
- ForkFeishuChannel：卡片构建、裸命令回卡片、卡片回调、机器人菜单、失败通知卡片、绑定身份与未绑定门禁
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.channels.fork import feishu as fork_feishu
from app.channels.fork import manager as fork_manager
from app.channels.fork.feishu import (
    CardActionContext,
    ChatIdentity,
    ForkFeishuChannel,
    build_agent_card,
    build_memory_card,
    build_model_card,
    build_repo_card,
    build_sessions_card,
)
from app.channels.fork.manager import BOT_MENU_SOURCE, CARD_ACTION_SOURCE, FORK_HELP_TEXT, FORK_SOURCE_METADATA_KEY, ForkChannelManager
from app.channels.fork.memory_view import persona_to_agent_name, render_memory
from app.channels.fork.progress import (
    NOTIFICATION_METADATA_KEY,
    PROGRESS_STEPS_KEY,
    PROGRESS_TODOS_KEY,
    apply_custom_progress_event,
    render_progress_block,
)
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage
from app.channels.store import ChannelStore
from deerflow.config.paths import make_safe_user_id
from deerflow.runtime.user_context import DEFAULT_USER_ID

MODELS = [("default-model", "Default"), ("gpt-x", "GPT X")]


@pytest.fixture(autouse=True)
def _pure_storage_user_id(monkeypatch):
    """渠道用户归属解析会在磁盘上建用户目录；测试里换成纯函数。"""
    monkeypatch.setattr("app.channels.manager._safe_user_id_for_run", make_safe_user_id)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("condition not met")


def _part(event: str, data):
    return SimpleNamespace(event=event, data=data)


def _async_iter(items):
    async def iterator():
        for item in items:
            yield item

    return iterator()


def _mock_client(thread_id: str = "t-new", run_result: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.threads.create = AsyncMock(return_value={"thread_id": thread_id})
    client.threads.update = AsyncMock(return_value={"thread_id": thread_id})
    client.threads.get = AsyncMock(return_value={"thread_id": thread_id, "metadata": {}})
    client.threads.delete = AsyncMock(return_value=None)
    client.threads.update_state = AsyncMock(return_value=None)
    client.threads.get_state = AsyncMock(return_value={"values": {"messages": [{"type": "ai", "content": "上次的回答"}]}})
    client.runs.wait = AsyncMock(return_value=run_result or {"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": "Hello"}]})
    return client


def _manager(tmp_path: Path, client: MagicMock | None = None) -> tuple[ForkChannelManager, MessageBus, ChannelStore]:
    bus = MessageBus()
    store = ChannelStore(path=tmp_path / "store.json")
    manager = ForkChannelManager(bus=bus, store=store)
    manager._client = client or _mock_client()
    return manager, bus, store


async def _dispatch(manager: ForkChannelManager, bus: MessageBus, msg: InboundMessage, *, until=None) -> list[OutboundMessage]:
    """跑一条入站消息并收集 outbound；``until(received)`` 为真时停止，默认等到第一条。"""
    received: list[OutboundMessage] = []

    async def capture(out: OutboundMessage) -> None:
        received.append(out)

    bus.subscribe_outbound(capture)
    await manager.start()
    try:
        await bus.publish_inbound(msg)
        await _wait_for(lambda: until(received) if until else len(received) >= 1)
    finally:
        await manager.stop()
    return received


def _command(text: str, *, channel: str = "test", chat_id: str = "chat1", metadata: dict | None = None) -> InboundMessage:
    return InboundMessage(channel_name=channel, chat_id=chat_id, user_id="user1", text=text, msg_type=InboundMessageType.COMMAND, metadata=metadata or {})


# ---------------------------------------------------------------------------
# ChannelStore 扩展字段
# ---------------------------------------------------------------------------


class TestChannelStoreExtensions:
    @pytest.fixture
    def store(self, tmp_path):
        return ChannelStore(path=tmp_path / "store.json")

    def test_set_thread_id_preserves_extension_fields(self, store):
        store.set_workdir("feishu", "c1", "/mnt/projects/a")
        store.set_model("feishu", "c1", "gpt-x")
        store.set_thread_id("feishu", "c1", "t1", user_id="u1")
        assert store.get_workdir("feishu", "c1") == "/mnt/projects/a"
        assert store.get_model("feishu", "c1") == "gpt-x"
        assert store.get_thread_id("feishu", "c1") == "t1"

    def test_entry_without_thread_returns_none(self, store):
        store.set_workdir("feishu", "c1", "/mnt/projects/a")
        assert store.get_thread_id("feishu", "c1") is None

    def test_workdir_history_is_mru_and_clear_keeps_history(self, store):
        for path in ("/mnt/projects/a", "/mnt/projects/b", "/mnt/projects/a"):
            store.set_workdir("feishu", "c1", path)
        assert store.get_workdir_history("feishu", "c1") == ["/mnt/projects/a", "/mnt/projects/b"]
        assert store.clear_workdir("feishu", "c1") is True
        assert store.get_workdir("feishu", "c1") is None
        assert store.clear_workdir("feishu", "c1") is False
        assert store.get_workdir_history("feishu", "c1") == ["/mnt/projects/a", "/mnt/projects/b"]

    def test_user_chat_mapping(self, store):
        assert store.get_user_chat("feishu", "ou_1") is None
        store.set_user_chat("feishu", "ou_1", "oc_1")
        assert store.get_user_chat("feishu", "ou_1") == "oc_1"


class TestChannelStoreSessions:
    @pytest.fixture
    def store(self, tmp_path):
        return ChannelStore(path=tmp_path / "store.json")

    def test_new_thread_does_not_drop_old_session(self, store):
        store.set_thread_id("feishu", "c1", "t1")
        store.add_session("feishu", "c1", "t1")
        store.set_thread_id("feishu", "c1", "t2")
        store.add_session("feishu", "c1", "t2")
        assert store.get_thread_id("feishu", "c1") == "t2"
        assert {s["thread_id"] for s in store.list_sessions("feishu", "c1")} == {"t1", "t2"}

    def test_add_session_snapshots_current_settings(self, store):
        store.set_agent("feishu", "c1", "software-team")
        store.set_model("feishu", "c1", "gpt-x")
        store.set_workdir("feishu", "c1", "/mnt/projects/a")
        store.set_thread_id("feishu", "c1", "t1")
        store.add_session("feishu", "c1", "t1")
        snapshot = store.get_session("feishu", "c1", "t1")
        assert (snapshot["agent"], snapshot["model"], snapshot["workdir"]) == ("software-team", "gpt-x", "/mnt/projects/a")

    def test_session_title(self, store):
        store.add_session("feishu", "c1", "t1")
        store.set_session_title("feishu", "c1", "t1", "   ")
        assert store.get_session("feishu", "c1", "t1")["title"] is None
        store.set_session_title("feishu", "c1", "t1", "我的第一个会话")
        assert store.get_session("feishu", "c1", "t1")["title"] == "我的第一个会话"

    def test_setting_change_syncs_current_session_snapshot(self, store):
        store.set_thread_id("feishu", "c1", "t1")
        store.add_session("feishu", "c1", "t1")
        store.set_agent("feishu", "c1", "software-team")
        assert store.get_session("feishu", "c1", "t1")["agent"] == "software-team"
        store.clear_agent("feishu", "c1")
        assert store.get_session("feishu", "c1", "t1").get("agent") is None

    def test_switch_session_restores_full_context(self, store):
        store.set_agent("feishu", "c1", "software-team")
        store.set_model("feishu", "c1", "gpt-x")
        store.set_thread_id("feishu", "c1", "t1")
        store.add_session("feishu", "c1", "t1")
        store.set_thread_id("feishu", "c1", "t2")
        store.add_session("feishu", "c1", "t2")
        store.clear_agent("feishu", "c1")
        store.clear_model("feishu", "c1")

        snapshot = store.switch_session("feishu", "c1", "t1")
        assert (snapshot["agent"], snapshot["model"]) == ("software-team", "gpt-x")
        assert store.get_thread_id("feishu", "c1") == "t1"
        assert store.get_agent("feishu", "c1") == "software-team"

        store.switch_session("feishu", "c1", "t2")
        assert store.get_agent("feishu", "c1") is None
        assert store.get_model("feishu", "c1") is None
        assert store.switch_session("feishu", "c1", "nope") is None

    def test_remove_sessions(self, store):
        store.set_thread_id("feishu", "c1", "t1")
        store.add_session("feishu", "c1", "t1")
        store.set_thread_id("feishu", "c1", "t2")
        store.add_session("feishu", "c1", "t2")

        assert store.remove_session("feishu", "c1", "nope")["removed"] is False
        result = store.remove_session("feishu", "c1", "t2")
        assert (result["was_current"], result["new_current"]) == (True, "t1")
        assert store.get_thread_id("feishu", "c1") == "t1"
        result = store.remove_session("feishu", "c1", "t1")
        assert (result["was_current"], result["new_current"]) == (True, None)
        assert store.get_thread_id("feishu", "c1") is None

    def test_touch_creates_missing_session(self, store):
        store.touch_session("feishu", "c1", "legacy-thread")
        assert store.get_session("feishu", "c1", "legacy-thread") is not None

    def test_sessions_persist_across_reload(self, tmp_path):
        path = tmp_path / "store.json"
        first = ChannelStore(path=path)
        first.set_thread_id("feishu", "c1", "t1")
        first.add_session("feishu", "c1", "t1")
        first.set_session_title("feishu", "c1", "t1", "持久会话")
        assert ChannelStore(path=path).get_session("feishu", "c1", "t1")["title"] == "持久会话"

    def test_add_session_prunes_oldest_but_keeps_current(self, store):
        store.SESSION_LIMIT = 3
        store.set_thread_id("feishu", "c1", "cur")
        store.add_session("feishu", "c1", "cur")
        for i in range(6):
            store.add_session("feishu", "c1", f"t{i}")
        ids = {s["thread_id"] for s in store.list_sessions("feishu", "c1")}
        assert len(ids) <= 3
        assert "cur" in ids

    def test_concurrent_list_while_mutating_does_not_crash(self, store):
        for i in range(30):
            store.add_session("feishu", "c1", f"t{i}")
        errors: list[Exception] = []
        stop = threading.Event()

        def churn():
            i = 0
            while not stop.is_set():
                store.add_session("feishu", "c1", f"x{i % 50}")
                store.remove_session("feishu", "c1", f"x{i % 50}")
                i += 1

        def lister():
            try:
                for _ in range(200):
                    store.list_sessions("feishu", "c1")
            except Exception as exc:  # noqa: BLE001 - 收集后断言
                errors.append(exc)

        writer = threading.Thread(target=churn)
        reader = threading.Thread(target=lister)
        writer.start()
        reader.start()
        reader.join(timeout=10)
        stop.set()
        writer.join(timeout=10)
        assert errors == []


# ---------------------------------------------------------------------------
# 记忆视图与进度纯函数
# ---------------------------------------------------------------------------


class _FakeMemoryManager:
    def __init__(self, captured: dict, *, unsupported: bool = False) -> None:
        self._captured = captured
        self._unsupported = unsupported

    def get_memory(self, *, user_id=None, agent_name=None):
        self._captured.update(user_id=user_id, agent_name=agent_name)
        if self._unsupported:
            raise NotImplementedError
        return {"facts": [{"content": "喜欢 Python", "category": "preference", "confidence": 0.9}], "user": {"workContext": {"summary": "在做 DeerFlow"}}}


def _patch_memory(captured: dict, **kwargs):
    return patch("deerflow.agents.memory.manager.get_memory_manager", return_value=_FakeMemoryManager(captured, **kwargs))


class TestMemoryView:
    def test_scopes_select_expected_bucket(self):
        captured: dict = {}
        with _patch_memory(captured):
            text = render_memory("global", user_id="u1")
            assert captured == {"user_id": "u1", "agent_name": None}
            assert "当前用户全局记忆" in text and "喜欢 Python" in text and "在做 DeerFlow" in text

            render_memory("default", user_id="u1")
            assert captured == {"user_id": DEFAULT_USER_ID, "agent_name": None}

            text = render_memory("persona", user_id="u1", persona="Software_Team")
            assert captured == {"user_id": "u1", "agent_name": "software-team"}

            text = render_memory("persona", user_id="u1", persona="lead_agent")
            assert captured["agent_name"] is None
            assert "通用助手即全局记忆" in text

            render_memory("global", user_id=None)
            assert captured["user_id"] == DEFAULT_USER_ID

    def test_unknown_scope_and_unsupported_backend(self):
        assert "persona" in render_memory("bogus", user_id="u1")
        with _patch_memory({}, unsupported=True):
            assert "不支持查看完整记忆" in render_memory("global", user_id="u1")

    def test_persona_to_agent_name(self):
        assert persona_to_agent_name("lead_agent") is None
        assert persona_to_agent_name(None) is None
        assert persona_to_agent_name("Software_Team") == "software-team"


class TestProgressHelpers:
    def test_custom_events_fold_into_steps_and_signal_failure_once(self):
        steps: dict = {}
        assert apply_custom_progress_event(steps, {"type": "task_started", "task_id": "a", "description": "写代码"}) is None
        apply_custom_progress_event(steps, {"type": "task_running", "task_id": "a", "message": {"content": [{"type": "text", "text": "正在改 foo.py"}]}})
        assert steps["a"] == {"description": "写代码", "status": "running", "activity": "正在改 foo.py"}

        failed = apply_custom_progress_event(steps, {"type": "task_failed", "task_id": "a", "error": "boom"})
        assert failed is not None and failed["error"] == "boom"
        assert apply_custom_progress_event(steps, {"type": "task_timed_out", "task_id": "a"}) is None
        assert apply_custom_progress_event(steps, {"type": "other", "task_id": "a"}) is None

    def test_live_usage_from_task_events_shows_in_status(self):
        from app.channels.fork.progress import LiveRunRegistry

        steps: dict = {}
        apply_custom_progress_event(steps, {"type": "task_running", "task_id": "a", "description": "实现", "usage": {"total_tokens": 1200}})
        apply_custom_progress_event(steps, {"type": "task_running", "task_id": "b", "description": "测试", "usage": {"total_tokens": 300}})
        registry = LiveRunRegistry()
        registry.start("t1")
        registry.update("t1", todos=None, steps=steps)
        assert "子任务累计 token：1500" in LiveRunRegistry.render(registry.get("t1"))

    def test_render_progress_block(self):
        block = render_progress_block(
            [{"content": "规划", "status": "completed"}, {"content": "实现", "status": "in_progress"}],
            [{"description": "实现", "status": "running", "activity": "改 foo.py"}, {"description": "旧任务", "status": "completed"}],
        )
        assert "✅ 规划" in block and "🔄 实现" in block
        assert "🔧 正在执行：实现" in block and "↳ 改 foo.py" in block
        assert "旧任务" not in block
        assert render_progress_block(None, None) == ""


# ---------------------------------------------------------------------------
# ForkChannelManager
# ---------------------------------------------------------------------------


class TestForkManagerThreadsAndPersona:
    def test_new_registers_session_and_keeps_old(self, tmp_path):
        manager, bus, store = _manager(tmp_path)
        store.set_thread_id("test", "chat1", "old-thread")
        store.add_session("test", "chat1", "old-thread")

        replies = _run(_dispatch(manager, bus, _command("/new")))

        assert replies[0].text == "New conversation started."
        assert store.get_thread_id("test", "chat1") == "t-new"
        assert {s["thread_id"] for s in store.list_sessions("test", "chat1")} == {"old-thread", "t-new"}

    def test_agent_shorthand_pins_new_thread_and_records_persona(self, tmp_path):
        manager, bus, store = _manager(tmp_path)
        with patch("app.channels.manager.load_agent_config", return_value=SimpleNamespace(name="software-team")):
            replies = _run(_dispatch(manager, bus, _command("/agent Software_Team")))

        assert "software-team" in replies[0].text
        metadata = manager._client.threads.create.call_args.kwargs["metadata"]
        assert metadata["channel_agent_name"] == "software-team"
        assert store.get_agent("test", "chat1") == "software-team"
        assert store.get_session("test", "chat1", "t-new")["agent"] == "software-team"

    def test_agent_default_alias_resets_persona(self, tmp_path):
        manager, bus, store = _manager(tmp_path)
        store.set_agent("test", "chat1", "software-team")
        _run(_dispatch(manager, bus, _command("/agent default")))
        assert manager._client.threads.create.call_args.kwargs["metadata"]["channel_agent_name"] == "lead_agent"
        assert store.get_agent("test", "chat1") is None

    def test_bare_agent_lists_personas(self, tmp_path):
        manager, bus, _store = _manager(tmp_path)
        with patch.object(fork_manager, "list_personas", return_value=[("lead_agent", "通用助手", ""), ("software-team", "software-team", "工程团队")]):
            replies = _run(_dispatch(manager, bus, _command("/agent")))
        assert "当前人设：通用助手" in replies[0].text
        assert "software-team（software-team）" in replies[0].text

    def test_new_keeps_sticky_persona(self, tmp_path):
        manager, bus, store = _manager(tmp_path)
        store.set_agent("test", "chat1", "software-team")
        with patch("deerflow.config.agents_config.load_agent_config", return_value=SimpleNamespace(name="software-team")):
            _run(_dispatch(manager, bus, _command("/new")))
        assert manager._client.threads.create.call_args.kwargs["metadata"]["channel_agent_name"] == "software-team"
        assert store.get_agent("test", "chat1") == "software-team"

    def test_missing_sticky_persona_falls_back_to_default(self, tmp_path):
        manager, bus, store = _manager(tmp_path)
        store.set_agent("test", "chat1", "deleted-agent")
        with patch("deerflow.config.agents_config.load_agent_config", side_effect=FileNotFoundError):
            _run(_dispatch(manager, bus, _command("/new")))
        assert "channel_agent_name" not in manager._client.threads.create.call_args.kwargs["metadata"]
        assert store.get_agent("test", "chat1") is None

    def test_legacy_thread_without_pinned_agent_uses_stored_persona(self, tmp_path):
        manager, _bus, store = _manager(tmp_path)
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="hi")
        assert _run(manager._load_thread_agent(manager._client, msg, "no-persona-thread")) is None

        store.set_agent("test", "chat1", "software-team")
        agent = _run(manager._load_thread_agent(manager._client, msg, "legacy"))

        assert agent == "software-team"
        assert manager._thread_agent_names["legacy"] == "software-team"

    def test_pinned_thread_ignores_stored_persona(self, tmp_path):
        manager, _bus, store = _manager(tmp_path)
        store.set_agent("test", "chat1", "software-team")
        manager._client.threads.get = AsyncMock(return_value={"thread_id": "t1", "metadata": {"channel_agent_name": "lead_agent"}})
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="hi")
        assert _run(manager._load_thread_agent(manager._client, msg, "t1")) == "lead_agent"


class TestForkManagerSettings:
    def test_model_command_and_context_injection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_manager, "available_models", lambda: MODELS)
        manager, _bus, store = _manager(tmp_path)
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="hi")

        assert "当前模型：default-model" in manager._handle_model_command(msg, "")
        assert "无效的模型" in manager._handle_model_command(msg, "nope")
        assert manager._handle_model_command(msg, "GPT-X") == "模型已切换：gpt-x"
        _assistant, _config, context = manager._resolve_run_params(msg, "t1")
        assert context["model_name"] == "gpt-x"

        manager._handle_model_command(msg, "default")
        assert store.get_model("test", "chat1") is None
        _assistant, _config, context = manager._resolve_run_params(msg, "t1")
        assert "model_name" not in context

    def test_stale_session_model_is_not_injected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_manager, "available_models", lambda: MODELS)
        manager, _bus, store = _manager(tmp_path)
        store.set_model("test", "chat1", "removed-model")
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="hi")
        _assistant, _config, context = manager._resolve_run_params(msg, "t1")
        assert "model_name" not in context

    def test_repo_command_sets_workdir_and_prefixes_chat(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_manager, "normalize_workdir", lambda raw: "/mnt/projects/demo" if raw == "demo" else None)
        monkeypatch.setattr(fork_manager, "list_project_workdirs", lambda: ["/mnt/projects/demo"])
        manager, bus, store = _manager(tmp_path)
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="/repo demo")

        assert manager._handle_repo_command(msg, "nope").startswith("无效的工作目录")
        assert manager._handle_repo_command(msg, "demo") == "工作目录已设置为：/mnt/projects/demo"
        assert "当前工作目录：/mnt/projects/demo" in manager._handle_repo_command(msg, "")

        _run(_dispatch(manager, bus, InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="列出文件")))
        sent = manager._client.runs.wait.call_args.kwargs["input"]["messages"][0]["content"]
        assert sent.startswith("[当前工作目录：/mnt/projects/demo。")
        assert sent.endswith("列出文件")
        assert manager._handle_repo_command(msg, "clear") == "已清除工作目录设置。"
        assert store.get_workdir("test", "chat1") is None

    def test_memory_command_uses_channel_owner_bucket(self, tmp_path):
        manager, bus, _store = _manager(tmp_path)
        captured: dict = {}
        with _patch_memory(captured):
            replies = _run(_dispatch(manager, bus, _command("/memory")))
        assert captured == {"user_id": make_safe_user_id("user1"), "agent_name": None}
        assert "喜欢 Python" in replies[0].text

    def test_status_reply_includes_progress_and_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_manager, "available_models", lambda: MODELS)
        manager, _bus, store = _manager(tmp_path)
        store.set_thread_id("test", "chat1", "t1")
        store.set_workdir("test", "chat1", "/mnt/projects/demo")
        manager.live_runs.start("t1")
        manager.live_runs.update("t1", todos=[{"content": "实现", "status": "in_progress"}], steps={})
        monkeypatch.setattr(manager, "_fetch_run_status", AsyncMock(return_value="运行状态：running"))
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="/status")

        reply = _run(manager._status_reply(msg))

        assert "正在执行中" in reply and "🔄 实现" in reply
        assert "运行状态：running" in reply
        assert "Active thread: t1" in reply
        assert "当前工作目录：/mnt/projects/demo" in reply
        assert "当前人设：通用助手" in reply
        assert "当前模型：default-model" in reply

    def test_help_is_fork_text_and_other_commands_fall_through(self, tmp_path):
        manager, bus, _store = _manager(tmp_path)
        assert _run(_dispatch(manager, bus, _command("/help")))[0].text == FORK_HELP_TEXT

        manager, bus, _store = _manager(tmp_path / "second")
        with patch("app.channels.manager.list_custom_agents", return_value=[]):
            replies = _run(_dispatch(manager, bus, _command("/agent list")))
        assert replies[0].text.startswith("Available agents:")


class TestForkManagerSessions:
    def _seed(self, store: ChannelStore) -> None:
        for thread_id, title in (("t1", "会话甲"), ("t2", "会话乙")):
            store.set_thread_id("test", "chat1", thread_id)
            store.add_session("test", "chat1", thread_id)
            store.set_session_title("test", "chat1", thread_id, title)

    def test_list_switch_view_rename_delete(self, tmp_path):
        manager, _bus, store = _manager(tmp_path)
        self._seed(store)
        msg = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="/sessions")

        listing = _run(manager._handle_sessions_command(msg, ""))
        assert "会话列表（共 2 个）" in listing and "✓ 会话乙" in listing

        assert "已切换到会话：会话甲" in _run(manager._handle_sessions_command(msg, "switch 2"))
        assert store.get_thread_id("test", "chat1") == "t1"

        view = _run(manager._handle_sessions_command(msg, "view t2"))
        assert "会话：会话乙" in view and "最近回复：上次的回答" in view

        assert _run(manager._handle_sessions_command(msg, "rename 1 新名字")) == "已重命名为：新名字"
        manager._client.threads.update_state.assert_awaited()

        deleted = _run(manager._handle_sessions_command(msg, "delete t2"))
        assert "已删除会话：会话乙" in deleted
        manager._client.threads.delete.assert_awaited_with("t2")
        assert {s["thread_id"] for s in store.list_sessions("test", "chat1")} == {"t1"}
        assert "找不到该会话" in _run(manager._handle_sessions_command(msg, "switch 9"))

    def test_gcdelete_is_silent_and_requires_card_source(self, tmp_path):
        manager, _bus, _store = _manager(tmp_path)
        typed = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="/sessions _gcdelete t9")
        assert _run(manager._handle_sessions_command(typed, "_gcdelete t9")) is None
        manager._client.threads.delete.assert_not_awaited()

        from_card = InboundMessage(channel_name="test", chat_id="chat1", user_id="user1", text="", metadata={FORK_SOURCE_METADATA_KEY: CARD_ACTION_SOURCE})
        assert _run(manager._handle_sessions_command(from_card, "_gcdelete t9")) is None
        manager._client.threads.delete.assert_awaited_with("t9")


class _FakeConnectionRepo:
    """只实现 manager 用到的线程映射接口（与上游 ChannelConnectionRepository 同签名）。"""

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str, str], str] = {}

    async def get_thread_id(self, connection_id: str, external_conversation_id: str, external_topic_id: str | None = None) -> str | None:
        return self.threads.get((connection_id, external_conversation_id, external_topic_id or ""))

    async def set_thread_id(self, *, connection_id: str, owner_user_id: str, provider: str, external_conversation_id: str, thread_id: str, external_topic_id: str | None = None) -> None:
        self.threads[(connection_id, external_conversation_id, external_topic_id or "")] = thread_id


def _bound_manager(tmp_path: Path) -> tuple[ForkChannelManager, ChannelStore, _FakeConnectionRepo]:
    repo = _FakeConnectionRepo()
    store = ChannelStore(path=tmp_path / "store.json")
    manager = ForkChannelManager(bus=MessageBus(), store=store, connection_repo=repo)
    manager._client = _mock_client()
    return manager, store, repo


def _bound_msg(text: str = "", *, metadata: dict | None = None) -> InboundMessage:
    return InboundMessage(
        channel_name="feishu",
        chat_id="chat1",
        user_id="ou_x",
        text=text,
        msg_type=InboundMessageType.COMMAND,
        connection_id="conn-1",
        owner_user_id="admin-1",
        metadata=metadata or {},
    )


class TestForkManagerBoundIdentity:
    def _seed(self, store: ChannelStore, repo: _FakeConnectionRepo) -> None:
        for thread_id, title in (("t1", "会话甲"), ("t2", "会话乙")):
            store.add_session("feishu", "chat1", thread_id)
            store.set_session_title("feishu", "chat1", thread_id, title)
        repo.threads[("conn-1", "chat1", "")] = "t2"

    def test_switch_writes_the_connection_repo(self, tmp_path):
        manager, store, repo = _bound_manager(tmp_path)
        self._seed(store, repo)
        msg = _bound_msg()

        assert "✓ 会话乙" in _run(manager._handle_sessions_command(msg, ""))
        assert "已切换到会话：会话甲" in _run(manager._handle_sessions_command(msg, "switch t1"))
        assert repo.threads[("conn-1", "chat1", "")] == "t1"

    def test_deleting_the_current_session_repoints_then_creates(self, tmp_path):
        manager, store, repo = _bound_manager(tmp_path)
        self._seed(store, repo)
        msg = _bound_msg()

        reply = _run(manager._handle_sessions_command(msg, "delete t2"))
        assert "已自动切换到最近会话：会话甲" in reply
        assert repo.threads[("conn-1", "chat1", "")] == "t1"

        reply = _run(manager._handle_sessions_command(msg, "delete t1"))
        assert "已为你开启新会话" in reply
        assert repo.threads[("conn-1", "chat1", "")] == "t-new"

    def test_card_gcdelete_fixes_the_bound_pointer(self, tmp_path):
        manager, store, repo = _bound_manager(tmp_path)
        self._seed(store, repo)
        store.remove_session("feishu", "chat1", "t2")
        msg = _bound_msg(metadata={FORK_SOURCE_METADATA_KEY: CARD_ACTION_SOURCE})

        assert _run(manager._handle_sessions_command(msg, "_gcdelete t2")) is None
        manager._client.threads.delete.assert_awaited()
        assert repo.threads[("conn-1", "chat1", "")] == "t1"

    def test_card_gcswitch_writes_the_bound_pointer(self, tmp_path):
        manager, store, repo = _bound_manager(tmp_path)
        self._seed(store, repo)
        card = _bound_msg(metadata={FORK_SOURCE_METADATA_KEY: CARD_ACTION_SOURCE})

        assert _run(manager._handle_sessions_command(card, "_gcswitch t1")) is None
        assert repo.threads[("conn-1", "chat1", "")] == "t1"
        assert _run(manager._handle_sessions_command(_bound_msg(), "_gcswitch t2")) is None
        assert _run(manager._handle_sessions_command(card, "_gcswitch ghost")) is None
        assert repo.threads[("conn-1", "chat1", "")] == "t1"

    def test_first_bound_lookup_adopts_an_accessible_legacy_pointer(self, tmp_path):
        manager, store, repo = _bound_manager(tmp_path)
        store.set_thread_id("feishu", "chat1", "legacy")

        assert _run(manager._lookup_thread_id(_bound_msg())) == "legacy"
        assert repo.threads[("conn-1", "chat1", "")] == "legacy"

    def test_inaccessible_legacy_pointer_is_skipped_once(self, tmp_path):
        manager, store, repo = _bound_manager(tmp_path)
        store.set_thread_id("feishu", "chat1", "legacy")
        manager._client.threads.get = AsyncMock(side_effect=RuntimeError("404"))

        assert _run(manager._lookup_thread_id(_bound_msg())) is None
        assert _run(manager._lookup_thread_id(_bound_msg())) is None
        assert manager._client.threads.get.await_count == 1
        assert repo.threads == {}


class TestForkManagerStreamingProgress:
    def test_streaming_run_attaches_progress_notifies_failure_and_records_title(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.channels.manager.STREAM_UPDATE_MIN_INTERVAL_SECONDS", 0.0)
        manager, bus, store = _manager(tmp_path, _mock_client(thread_id="t-stream"))
        todos = [{"content": "实现", "status": "in_progress"}]
        events = [
            _part("custom", {"type": "task_started", "task_id": "a", "description": "实现功能"}),
            _part("values", {"messages": [{"type": "human", "content": "hi"}], "todos": todos}),
            _part("messages-tuple", [{"id": "ai-1", "content": "好的", "type": "AIMessageChunk"}, {"langgraph_node": "agent"}]),
            _part("custom", {"type": "task_failed", "task_id": "a", "error": "boom"}),
            _part("values", {"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": "好的"}], "todos": todos, "title": "功能实现"}),
        ]
        manager._client.runs.stream = MagicMock(return_value=_async_iter(events))
        msg = InboundMessage(channel_name="feishu", chat_id="chat1", user_id="user1", text="hi", thread_ts="om-1")

        received = _run(_dispatch(manager, bus, msg, until=lambda outs: any(o.is_final and not o.metadata.get(NOTIFICATION_METADATA_KEY) for o in outs)))

        assert "custom" in manager._client.runs.stream.call_args.kwargs["stream_mode"]
        assert any(PROGRESS_TODOS_KEY in out.metadata for out in received if not out.is_final)
        notifications = [out for out in received if out.metadata.get(NOTIFICATION_METADATA_KEY)]
        assert len(notifications) == 1 and "boom" in notifications[0].text
        final = received[-1]
        assert final.is_final and final.metadata[PROGRESS_TODOS_KEY] == todos
        assert final.metadata[PROGRESS_STEPS_KEY][0]["status"] == "failed"
        assert store.get_session("feishu", "chat1", "t-stream")["title"] == "功能实现"
        assert manager.live_runs.get("t-stream")["status"] == "finished"


# ---------------------------------------------------------------------------
# ForkFeishuChannel
# ---------------------------------------------------------------------------


def _buttons(card: dict) -> list[dict]:
    return [button for element in card["elements"] if element.get("tag") == "action" for button in element["actions"]]


_IDENTITY = ChatIdentity(bound=False, binding_required=False, storage_user_id="u1", current_thread_id=None)


def _channel(tmp_path: Path) -> tuple[ForkFeishuChannel, ChannelStore]:
    store = ChannelStore(path=tmp_path / "store.json")
    channel = ForkFeishuChannel(MessageBus(), config={"app_id": "a", "app_secret": "s", "channel_store": store})
    channel._schedule = MagicMock(side_effect=lambda coroutine, **kwargs: coroutine.close())
    channel._publish_synthetic_command = MagicMock()
    channel._resolve_identity_blocking = MagicMock(return_value=_IDENTITY)
    return channel, store


def _ctx(store: ChannelStore, value: dict, *, identity: ChatIdentity = _IDENTITY) -> CardActionContext:
    return CardActionContext(store=store, chat_id="chat-1", open_id="ou_x", identity=identity, value=value)


def _message_event(text: str, *, chat_type: str = "p2p") -> MagicMock:
    event = MagicMock()
    event.event.message.chat_id = "chat-1"
    event.event.message.message_id = "om-1"
    event.event.message.chat_type = chat_type
    event.event.message.root_id = None
    event.event.message.content = json.dumps({"text": text})
    event.event.sender.sender_id.open_id = "ou_x"
    return event


_CLICKER = SimpleNamespace(operator=SimpleNamespace(open_id="ou_x"))


class TestFeishuCards:
    def test_sessions_card_marks_current_and_offers_actions(self):
        sessions = [{"thread_id": "t1", "title": "会话甲", "agent": "software-team", "updated_at": 0.0}, {"thread_id": "t2", "title": None, "agent": None}]
        card = build_sessions_card(sessions, "t1")
        blob = json.dumps(card, ensure_ascii=False)
        assert "会话甲" in blob and "未命名会话" in blob and "（当前）" in blob
        assert {"switch_session", "view_session", "delete_session", "new_session"} <= {b["value"]["action"] for b in _buttons(card)}
        assert "没有会话记录" in json.dumps(build_sessions_card([], None), ensure_ascii=False)

    def test_selection_cards_highlight_current(self):
        model_card = build_model_card("gpt-x", MODELS)
        assert "✓ GPT X" in [b["text"]["content"] for b in _buttons(model_card)]
        agent_card = build_agent_card("software-team", [("lead_agent", "通用助手", ""), ("software-team", "software-team", "工程团队")])
        assert "✓ software-team" in [b["text"]["content"] for b in _buttons(agent_card)]
        memory_card = build_memory_card("persona", "正文")
        assert "✓ 当前人设" in [b["text"]["content"] for b in _buttons(memory_card)]
        repo_card = build_repo_card("/mnt/projects/a", ["/mnt/projects/a"], ["/mnt/projects/a", "/mnt/projects/b"])
        labels = [b["text"]["content"] for b in _buttons(repo_card)]
        assert "✓ a" in labels and "b" in labels and "清除设置" in labels

    def test_compose_card_text_prepends_progress(self):
        metadata = {PROGRESS_TODOS_KEY: [{"content": "实现", "status": "in_progress"}]}
        assert ForkFeishuChannel._compose_card_text("正文", metadata).startswith("**任务进度**")
        assert ForkFeishuChannel._compose_card_text("正文", metadata).endswith("---\n\n正文")
        assert ForkFeishuChannel._compose_card_text("正文", {}) == "正文"
        assert ForkFeishuChannel._compose_card_text("", metadata) == "**任务进度**\n🔄 实现"

    def test_event_handler_registers_card_action_and_bot_menu(self, tmp_path):
        channel, _store = _channel(tmp_path)
        registered: list[str] = []

        class FakeBuilder:
            def __getattr__(self, name):
                def register(_handler):
                    registered.append(name)
                    return self

                return register

            def build(self):
                return registered

        lark = SimpleNamespace(EventDispatcherHandler=SimpleNamespace(builder=lambda *_args: FakeBuilder()))
        channel._build_event_handler(lark)
        assert {"register_p2_im_message_receive_v1", "register_p2_card_action_trigger", "register_p2_application_bot_menu_v6"} <= set(registered)


class TestFeishuInbound:
    def test_bare_card_command_is_intercepted_and_records_open_id(self, tmp_path):
        channel, store = _channel(tmp_path)
        assert channel._intercept_card_command(_message_event("/repo")) is True
        assert channel._schedule.call_args.kwargs["name"] == "send_repo_card"
        assert store.get_user_chat("feishu", "ou_x") == "chat-1"

        assert channel._intercept_card_command(_message_event("@_user_1 /sessions", chat_type="group")) is True
        assert channel._schedule.call_args.kwargs["name"] == "send_sessions_card"

    def test_command_with_args_goes_to_upstream_as_command(self, tmp_path):
        channel, _store = _channel(tmp_path)
        assert channel._intercept_card_command(_message_event("/repo demo")) is False
        channel._make_inbound = MagicMock()
        channel._on_message(_message_event("/repo demo"))
        assert channel._make_inbound.call_args.kwargs["msg_type"] == InboundMessageType.COMMAND

    def test_notification_outbound_sends_red_card(self, tmp_path):
        channel, _store = _channel(tmp_path)
        channel._send_interactive = AsyncMock()
        out = OutboundMessage(channel_name="feishu", chat_id="chat-1", thread_id="t1", text="子任务执行失败：x", thread_ts="om-1", metadata={NOTIFICATION_METADATA_KEY: True})
        _run(channel._send_card_message(out))
        card = json.loads(channel._send_interactive.call_args.args[0])
        assert card["header"]["template"] == "red"
        assert channel._send_interactive.call_args.kwargs["reply_to"] == "om-1"


class TestFeishuCardActions:
    def test_on_card_action_updates_model_and_refreshes_card(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_feishu, "available_models", lambda: MODELS)
        channel, store = _channel(tmp_path)
        data = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"action": "set_model", "name": "gpt-x"}), context=SimpleNamespace(open_chat_id="chat-1"), operator=_CLICKER.operator))

        response = channel._on_card_action(data)

        assert response.toast.content == "模型已切换：gpt-x"
        assert store.get_model("feishu", "chat-1") == "gpt-x"
        channel._resolve_identity_blocking.assert_called_with("chat-1", "ou_x")
        channel._card_action_set_model(_ctx(store, {"name": "default-model"}))
        assert store.get_model("feishu", "chat-1") is None
        assert channel._card_action_set_model(_ctx(store, {"name": "nope"}))["toast"]["type"] == "error"

        unknown = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"action": "rm_rf"}), context=SimpleNamespace(open_chat_id="chat-1")))
        assert channel._on_card_action(unknown).toast is None

    def test_card_action_is_gated_on_identity(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_feishu, "available_models", lambda: MODELS)
        channel, store = _channel(tmp_path)
        data = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"action": "clear_model"}), context=SimpleNamespace(open_chat_id="chat-1"), operator=_CLICKER.operator))
        store.set_model("feishu", "chat-1", "gpt-x")

        channel._resolve_identity_blocking.return_value = replace(_IDENTITY, binding_required=True)
        assert channel._on_card_action(data).toast.type == "warning"
        channel._resolve_identity_blocking.return_value = None
        assert channel._on_card_action(data).toast.type == "error"
        assert store.get_model("feishu", "chat-1") == "gpt-x"

        channel._resolve_identity_blocking.return_value = replace(_IDENTITY, bound=True, binding_required=True)
        assert channel._on_card_action(data).toast.type == "info"
        assert store.get_model("feishu", "chat-1") is None

    def test_session_actions(self, tmp_path):
        channel, store = _channel(tmp_path)
        for thread_id, title in (("t1", "会话甲"), ("t2", "会话乙")):
            store.set_thread_id("feishu", "chat-1", thread_id)
            store.add_session("feishu", "chat-1", thread_id)
            store.set_session_title("feishu", "chat-1", thread_id, title)

        payload = channel._card_action_switch_session(_ctx(store, {"thread_id": "t1"}, identity=replace(_IDENTITY, current_thread_id="t2")))
        assert store.get_thread_id("feishu", "chat-1") == "t1"
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/sessions _gcswitch t1", source=CARD_ACTION_SOURCE)
        assert "🟢 会话甲**（当前）" in json.dumps(payload["card"]["data"], ensure_ascii=False)

        payload = channel._card_action_delete_session(_ctx(store, {"thread_id": "t1"}, identity=replace(_IDENTITY, current_thread_id="t1")))
        assert payload["toast"]["content"] == "已删除"
        assert {s["thread_id"] for s in store.list_sessions("feishu", "chat-1")} == {"t2"}
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/sessions _gcdelete t1", source=CARD_ACTION_SOURCE)
        assert "🟢 会话乙**（当前）" in json.dumps(payload["card"]["data"], ensure_ascii=False)
        assert channel._card_action_delete_session(_ctx(store, {"thread_id": "t1"}))["toast"]["type"] == "error"

        channel._card_action_view_session(_ctx(store, {"thread_id": "t2"}))
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/sessions view t2", source=CARD_ACTION_SOURCE)
        channel._card_action_new_session(_ctx(store, {}))
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/new", source=CARD_ACTION_SOURCE)

    def test_agent_actions_route_through_agent_use(self, tmp_path, monkeypatch):
        personas = [("lead_agent", "通用助手", ""), ("software-team", "software-team", "工程团队")]
        seen: list[str | None] = []
        monkeypatch.setattr(fork_feishu, "list_personas", lambda user_id: seen.append(user_id) or personas)
        channel, store = _channel(tmp_path)

        payload = channel._card_action_set_agent(_ctx(store, {"name": "software-team"}))
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/agent use software-team", source=CARD_ACTION_SOURCE)
        assert "✓ software-team" in [b["text"]["content"] for b in _buttons(payload["card"]["data"])]
        assert channel._card_action_set_agent(_ctx(store, {"name": "ghost"}))["toast"]["type"] == "error"

        channel._card_action_clear_agent(_ctx(store, {}))
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/agent use lead_agent", source=CARD_ACTION_SOURCE)
        assert set(seen) == {"u1"}

    def test_view_memory_renders_in_card(self, tmp_path, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(fork_feishu, "render_memory", lambda scope, *, user_id, persona: calls.append((scope, user_id, persona)) or "记忆正文")
        channel, store = _channel(tmp_path)
        store.set_agent("feishu", "chat-1", "software-team")

        payload = channel._card_action_view_memory(_ctx(store, {"scope": "persona"}, identity=replace(_IDENTITY, bound=True, storage_user_id="admin-1")))

        assert calls == [("persona", "admin-1", "software-team")]
        assert "记忆正文" in json.dumps(payload["card"]["data"], ensure_ascii=False)
        assert channel._card_action_view_memory(_ctx(store, {"scope": "bogus"}))["toast"]["type"] == "error"


class _FakeFeishuConnections:
    """绑定库替身：chat-1 里的 ou_x 已绑定到 admin-1。"""

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], str] = {("conn-1", "chat-1"): "t-bound"}

    async def find_connection_by_external_identity(self, *, provider: str, external_account_id: str, workspace_id: str | None = None) -> dict | None:
        if (provider, external_account_id, workspace_id) == ("feishu", "ou_x", "chat-1"):
            return {"id": "conn-1", "owner_user_id": "admin-1", "workspace_id": "chat-1"}
        return None

    async def get_thread_id(self, connection_id: str, external_conversation_id: str, external_topic_id: str | None = None) -> str | None:
        return self.threads.get((connection_id, external_conversation_id))


class TestFeishuBoundIdentity:
    def _bound_channel(self, tmp_path: Path, monkeypatch, *, required: bool = True) -> tuple[ForkFeishuChannel, ChannelStore]:
        store = ChannelStore(path=tmp_path / "store.json")
        channel = ForkFeishuChannel(MessageBus(), config={"app_id": "a", "app_secret": "s", "channel_store": store, "connection_repo": _FakeFeishuConnections()})
        monkeypatch.setattr(channel, "_binding_required", lambda: required)
        return channel, store

    def test_bound_identity_uses_owner_and_repo_pointer(self, tmp_path, monkeypatch):
        channel, store = self._bound_channel(tmp_path, monkeypatch)
        store.set_thread_id("feishu", "chat-1", "t-legacy")

        identity = _run(channel._resolve_identity("chat-1", "ou_x"))

        assert identity == ChatIdentity(bound=True, binding_required=True, storage_user_id="admin-1", current_thread_id="t-bound")
        assert not identity.blocked

    def test_unbound_user_is_blocked_when_binding_is_required(self, tmp_path, monkeypatch):
        channel, store = self._bound_channel(tmp_path, monkeypatch)
        store.set_thread_id("feishu", "chat-1", "t-legacy")

        identity = _run(channel._resolve_identity("chat-1", "ou_stranger"))

        assert identity.blocked and identity.storage_user_id == "ou_stranger" and identity.current_thread_id == "t-legacy"
        channel._send_interactive = AsyncMock()
        _run(channel._reply_card_of_kind("om-1", "chat-1", "ou_stranger", "sessions"))
        assert "/connect" in channel._send_interactive.call_args.args[0]

    def test_unbound_user_passes_when_binding_is_optional(self, tmp_path, monkeypatch):
        channel, _store = self._bound_channel(tmp_path, monkeypatch, required=False)
        channel._send_interactive = AsyncMock()

        _run(channel._reply_card_of_kind("om-1", "chat-1", "ou_stranger", "repo"))

        assert channel._send_interactive.call_args.args[0]["header"]["title"]["content"]

    def test_synthetic_command_carries_bound_identity(self, tmp_path, monkeypatch):
        channel, _store = self._bound_channel(tmp_path, monkeypatch)
        published: list[InboundMessage] = []
        channel._publish_inbound_or_drop = AsyncMock(side_effect=lambda inbound: published.append(inbound) or True)
        inbound = channel._make_inbound(chat_id="chat-1", user_id="ou_x", text="/new", msg_type=InboundMessageType.COMMAND)

        _run(channel._publish_with_identity(inbound))

        assert (published[0].connection_id, published[0].owner_user_id, published[0].workspace_id) == ("conn-1", "admin-1", "chat-1")

    def test_blocking_resolution_runs_on_the_main_loop(self, tmp_path, monkeypatch):
        channel, _store = self._bound_channel(tmp_path, monkeypatch)
        assert channel._resolve_identity_blocking("chat-1", "ou_x") is None

        loop = asyncio.new_event_loop()
        runner = threading.Thread(target=loop.run_forever, daemon=True)
        runner.start()
        try:
            channel._main_loop = loop
            identity = channel._resolve_identity_blocking("chat-1", "ou_x")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            runner.join(timeout=5)
            loop.close()
        assert identity is not None and identity.storage_user_id == "admin-1"


class TestFeishuBotMenu:
    @staticmethod
    def _menu(key: str) -> SimpleNamespace:
        return SimpleNamespace(event=SimpleNamespace(event_key=key, operator=SimpleNamespace(operator_id=SimpleNamespace(open_id="ou_x"))))

    def test_card_menu_items_create_cards(self, tmp_path):
        channel, store = _channel(tmp_path)
        store.set_user_chat("feishu", "ou_x", "chat-1")
        channel._on_bot_menu(self._menu("repo"))
        assert channel._schedule.call_args.kwargs["name"] == "menu_repo_card"

    def test_command_menu_items_need_known_chat(self, tmp_path):
        channel, store = _channel(tmp_path)
        channel._on_bot_menu(self._menu("STATUS"))
        assert channel._schedule.call_args.kwargs["name"] == "menu_hint"
        channel._publish_synthetic_command.assert_not_called()

        store.set_user_chat("feishu", "ou_x", "chat-1")
        channel._on_bot_menu(self._menu("STATUS"))
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/status", source=BOT_MENU_SOURCE)
        channel._on_bot_menu(self._menu("AGENT_SOFTWARE_TEAM"))
        channel._publish_synthetic_command.assert_called_with("chat-1", "ou_x", "/agent software_team", source=BOT_MENU_SOURCE)

        calls_before = channel._publish_synthetic_command.call_count
        channel._on_bot_menu(self._menu("UNKNOWN"))
        assert channel._publish_synthetic_command.call_count == calls_before
