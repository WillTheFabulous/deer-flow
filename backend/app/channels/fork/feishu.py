"""ForkFeishuChannel：飞书交互卡片、机器人菜单、卡片回调、进度块与失败通知卡片。

- 裸命令 ``/repo`` ``/agent`` ``/sessions`` ``/memory`` ``/model(s)`` 直接回复交互卡片；带参数时仍走文本命令。
- 卡片按钮回调（card.action.trigger）：工作目录 / 模型 / 会话登记就地改 store 并原地刷新卡片；
  人设切换、查看 / 新建会话、网关线程清理与当前会话指针经总线投递合成命令交给 ForkChannelManager
  （它持有网关客户端与绑定库）。
- 机器人自定义菜单（application.bot.menu_v6）：event_key 映射到卡片或命令，只带 open_id，
  靠 p2p 消息记录的 open_id → chat_id 映射找回会话。
- 启用 channel_connections 后，卡片与合成命令按绑定身份解析人设 / 记忆 / 当前会话（与 manager 的运行身份一致）；
  要求绑定而未绑定时只回绑定提示。
- 运行卡片正文上方渲染 todos / 子任务进度；子任务失败另发红头通知卡片。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from typing import Any

from app.channels.commands import strip_leading_mentions
from app.channels.feishu import FeishuChannel
from app.channels.fork.manager import (
    BOT_MENU_SOURCE,
    CARD_ACTION_SOURCE,
    FORK_SOURCE_METADATA_KEY,
    available_models,
    relative_time,
    session_display_title,
)
from app.channels.fork.memory_view import MEMORY_SCOPES, render_memory
from app.channels.fork.personas import DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL, list_personas, persona_label
from app.channels.fork.progress import NOTIFICATION_METADATA_KEY, PROGRESS_STEPS_KEY, PROGRESS_TODOS_KEY, render_progress_block
from app.channels.fork.workdir import list_project_workdirs, normalize_workdir
from app.channels.manager import _auth_disabled_owner_user_id, _channel_storage_user_id
from app.channels.message_bus import InboundMessage, InboundMessageType, OutboundMessage

logger = logging.getLogger(__name__)

_BUTTONS_PER_ROW = 4
_REPO_CARD_MAX_DIRS = 20
_SESSIONS_CARD_MAX = 15

# 卡片回调在 lark 线程里同步等待主循环解析身份；飞书要求回调在 3 秒内响应
_IDENTITY_TIMEOUT_SECONDS = 2.0
_BINDING_REQUIRED_TEXT = "还没有绑定 DeerFlow 账号：请在 DeerFlow 网页的设置里连接飞书，把网页给出的 /connect 连接码发给我，绑定后再使用。"
_BINDING_REQUIRED_TOAST = "请先绑定 DeerFlow 账号（网页设置里连接飞书）"

# 裸命令 → 卡片类型
_CARD_COMMANDS = {"/repo": "repo", "/agent": "agent", "/sessions": "sessions", "/memory": "memory", "/models": "model", "/model": "model"}

# 机器人自定义菜单 event_key（不区分大小写）。飞书后台「应用能力 → 机器人 → 自定义菜单」新增菜单项，
# 选「事件推送」并填写下表的 event_key；另支持前缀 AGENT_<NAME> 直接切换人设（如 AGENT_SOFTWARE_TEAM）。
_MENU_CARDS = {"REPO": "repo", "AGENT": "agent", "SESSIONS": "sessions", "MEMORY": "memory", "MODELS": "model"}
_MENU_COMMANDS = {
    "STATUS": "/status",
    "NEW": "/new",
    "HELP": "/help",
    "MEMORY_GLOBAL": "/memory global",
    "MEMORY_DEFAULT": "/memory default",
    "MEMORY_PERSONA": "/memory persona",
}
_MENU_AGENT_PREFIX = "AGENT_"

_MEMORY_SCOPE_LABELS = {"global": "当前用户全局", "default": "跨用户共享", "persona": "当前人设"}

_CARD_ACTIONS = frozenset(
    {
        "set_workdir",
        "clear_workdir",
        "set_model",
        "clear_model",
        "set_agent",
        "clear_agent",
        "view_memory",
        "switch_session",
        "view_session",
        "delete_session",
        "new_session",
    }
)


@dataclass(frozen=True)
class ChatIdentity:
    """卡片读写用的会话身份，归属规则与 manager 的运行身份一致。"""

    bound: bool
    binding_required: bool
    storage_user_id: str | None
    current_thread_id: str | None

    @property
    def blocked(self) -> bool:
        return self.binding_required and not self.bound


@dataclass(frozen=True)
class CardActionContext:
    store: Any
    chat_id: str
    open_id: str | None
    identity: ChatIdentity
    value: dict[str, Any]


# -- 卡片构建（纯函数，发送与回调刷新共用）-------------------------------------


def _toast(kind: str, content: str) -> dict[str, str]:
    return {"type": kind, "content": content}


def _button(label: str, value: dict[str, Any], *, kind: str = "default") -> dict[str, Any]:
    return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind, "value": value}


def _button_rows(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"tag": "action", "actions": buttons[i : i + _BUTTONS_PER_ROW]} for i in range(0, len(buttons), _BUTTONS_PER_ROW)]


def _note(text: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _markdown(text: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": text}


def _card(title: str, elements: list[dict[str, Any]], *, template: str = "blue") -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def _workdir_buttons(paths: list[str], current: str | None, *, primary: bool) -> list[dict[str, Any]]:
    buttons = []
    for path in paths:
        name = path.rstrip("/").rsplit("/", 1)[-1]
        is_current = path == current
        buttons.append(_button(f"✓ {name}" if is_current else name, {"action": "set_workdir", "path": path}, kind="primary" if primary or is_current else "default"))
    return _button_rows(buttons)


def build_repo_card(current: str | None, history: list[str], available: list[str]) -> dict[str, Any]:
    elements = [_markdown(f"**当前工作目录**：{current or '未设置'}")]
    recent = [h for h in history if h in available][:5]
    if recent:
        elements.append(_markdown("**最近使用**"))
        elements.extend(_workdir_buttons(recent, current, primary=True))
    rest = [p for p in available if p not in recent]
    if rest:
        shown = rest[:_REPO_CARD_MAX_DIRS]
        elements.append(_markdown("**全部目录**"))
        elements.extend(_workdir_buttons(shown, current, primary=False))
        if len(rest) > len(shown):
            elements.append(_markdown(f"（仅显示前 {len(shown)} 个，更多请用 /repo <名称> 设置）"))
    elif not recent:
        elements.append(_markdown("项目目录为空。把代码放到宿主机 /work/projects/ 下，或在对话里让工程师 git clone 到 /mnt/projects/。"))
    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": [_button("清除设置", {"action": "clear_workdir"}, kind="danger")]})
    elements.append(_note("新增项目：把代码放到宿主机 /work/projects/ 下；或直接对我说「把 <git地址> 克隆到 /mnt/projects」。"))
    return _card("选择工作目录", elements)


def build_agent_card(current: str, personas: list[tuple[str, str, str]]) -> dict[str, Any]:
    current_label = next((label for name, label, _ in personas if name == current), persona_label(current))
    elements = [_markdown(f"**当前人设**：{current_label}")]
    descriptions = [f"• **{label}**（{name}）：{desc}" if desc else f"• **{label}**（{name}）" for name, label, desc in personas]
    if descriptions:
        elements.append(_markdown("\n".join(descriptions)))
    buttons = [_button(f"✓ {label}" if name == current else label, {"action": "set_agent", "name": name}, kind="primary" if name == current else "default") for name, label, _desc in personas]
    elements.extend(_button_rows(buttons))
    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": [_button(f"恢复默认（{DEFAULT_PERSONA_LABEL}）", {"action": "clear_agent"}, kind="danger")]})
    elements.append(_note("切换人设会开启一个新会话（旧会话可在 /sessions 找回）。新增人设：在 Web 端创建 Custom Agent。"))
    return _card("选择助手 / 人设", elements)


def build_sessions_card(sessions: list[dict[str, Any]], current_thread_id: str | None) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if not sessions:
        elements.append(_markdown("当前没有会话记录。直接发消息即可开始第一个会话。"))
    else:
        elements.append(_markdown(f"共 **{len(sessions)}** 个会话，点按钮切换 / 查看 / 删除："))
        for session in sessions[:_SESSIONS_CARD_MAX]:
            thread_id = str(session.get("thread_id", ""))
            is_current = bool(thread_id) and thread_id == current_thread_id
            title = session_display_title(session)
            agent = session.get("agent")
            meta = [persona_label(agent if isinstance(agent, str) else None)]
            when = relative_time(session.get("updated_at"))
            if when:
                meta.append(when)
            head = f"**🟢 {title}**（当前）" if is_current else f"**{title}**"
            elements.append(_markdown(f"{head}\n{' · '.join(meta)}"))
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        _button("切换", {"action": "switch_session", "thread_id": thread_id}, kind="default" if is_current else "primary"),
                        _button("查看", {"action": "view_session", "thread_id": thread_id}),
                        _button("删除", {"action": "delete_session", "thread_id": thread_id}, kind="danger"),
                    ],
                }
            )
        if len(sessions) > _SESSIONS_CARD_MAX:
            elements.append(_markdown(f"（仅显示最近 {_SESSIONS_CARD_MAX} 个，用 /sessions 查看全部）"))
    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": [_button("＋ 新会话", {"action": "new_session"}, kind="primary")]})
    elements.append(_note("切换后继续发消息即可在该会话上下文中对话；删除不可恢复。"))
    return _card("会话管理", elements)


def build_memory_card(active_scope: str | None = None, body: str | None = None) -> dict[str, Any]:
    """记忆卡片：三个范围按钮；点击后带上该范围的内容重新渲染。"""
    buttons = [
        _button(f"✓ {_MEMORY_SCOPE_LABELS[scope]}" if scope == active_scope else _MEMORY_SCOPE_LABELS[scope], {"action": "view_memory", "scope": scope}, kind="primary" if scope == active_scope else "default") for scope in MEMORY_SCOPES
    ]
    elements = [_markdown("选择要查看的记忆范围："), {"tag": "action", "actions": buttons}]
    if body is not None:
        elements.extend([{"tag": "hr"}, _markdown(body)])
    elements.append(_note("记忆按你个人隔离，建议在单聊中查看。"))
    return _card("查看记忆", elements)


def build_model_card(current: str | None, models: list[tuple[str, str | None]]) -> dict[str, Any]:
    def _label(name: str, display: str | None) -> str:
        return display if display and display != name else name

    current_label = next((_label(name, display) for name, display in models if name == current), current or "（未配置）")
    elements = [_markdown(f"**当前模型**：{current_label}")]
    buttons = [_button(f"✓ {_label(name, display)}" if name == current else _label(name, display), {"action": "set_model", "name": name}, kind="primary" if name == current else "default") for name, display in models]
    elements.extend(_button_rows(buttons) if buttons else [_markdown("（未配置任何模型）")])
    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": [_button("恢复默认模型", {"action": "clear_model"}, kind="danger")]})
    elements.append(_note("切换仅对本会话生效；新会话继续使用所选模型。"))
    return _card("选择模型", elements)


def build_notification_card(text: str, *, title: str = "子任务执行失败") -> str:
    return json.dumps(_card(title, [_markdown(text)], template="red"))


class ForkFeishuChannel(FeishuChannel):
    """fork 的飞书渠道；未覆盖的行为与上游 FeishuChannel 完全一致。"""

    # -- 事件注册与卡片正文 ---------------------------------------------------

    def _event_handler_builder(self, lark):
        return super()._event_handler_builder(lark).register_p2_card_action_trigger(self._on_card_action).register_p2_application_bot_menu_v6(self._on_bot_menu)

    @classmethod
    def _compose_card_text(cls, text: str, metadata: dict[str, Any] | None = None) -> str:
        composed = super()._compose_card_text(text, metadata)
        if not isinstance(metadata, dict):
            return composed
        progress = render_progress_block(metadata.get(PROGRESS_TODOS_KEY), metadata.get(PROGRESS_STEPS_KEY))
        if not progress:
            return composed
        return f"{progress}\n\n---\n\n{composed}" if composed.strip() else progress

    async def _send_card_message(self, msg: OutboundMessage) -> None:
        if msg.metadata.get(NOTIFICATION_METADATA_KEY):
            # 独立通知另发一张红头卡片，不 patch 运行中的卡片，也不加 DONE 反应
            await self._send_interactive(build_notification_card(msg.text), reply_to=msg.thread_ts, chat_id=msg.chat_id)
            return
        await super()._send_card_message(msg)

    # -- 发送与调度助手 -------------------------------------------------------

    def _channel_store(self):
        return self.config.get("channel_store")

    async def _send_interactive(
        self,
        card: dict[str, Any] | str,
        *,
        reply_to: str | None = None,
        chat_id: str | None = None,
        open_id: str | None = None,
    ) -> None:
        """回复某条消息，或按 chat_id / open_id 新发一张交互卡片。"""
        if not self._api_client:
            return
        content = card if isinstance(card, str) else json.dumps(card)
        if reply_to:
            request = self._ReplyMessageRequest.builder().message_id(reply_to).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
        else:
            receive_id_type, receive_id = ("chat_id", chat_id) if chat_id else ("open_id", open_id)
            if not receive_id:
                return
            request = self._CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(self._CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("interactive").content(content).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu interactive send failed: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}")

    def _schedule(self, coroutine: Coroutine[Any, Any, Any], *, name: str, msg_id: str) -> None:
        """从 lark 线程把协程投递到主事件循环（主循环未运行时丢弃并告警）。"""
        if not self._submit_threadsafe_coroutine(coroutine, self._main_loop, name=name, msg_id=msg_id):
            logger.warning("[Feishu] main loop not running, dropping %s", name)

    def _publish_synthetic_command(self, chat_id: str, open_id: str | None, text: str, *, source: str) -> None:
        """投递一条合成命令，交给 ForkChannelManager 处理（回复会作为新卡片发到会话里）。"""
        inbound = self._make_inbound(
            chat_id=chat_id,
            user_id=open_id or "",
            text=text,
            msg_type=InboundMessageType.COMMAND,
            metadata={"user_id": open_id, FORK_SOURCE_METADATA_KEY: source},
        )
        self._schedule(self._publish_with_identity(inbound), name=f"{source}_command", msg_id=text)

    async def _publish_with_identity(self, inbound: InboundMessage) -> None:
        """和普通消息一样先附上绑定身份（manager 会按 workspace 复核），再投递到总线。"""
        await self._publish_inbound_or_drop(await self._attach_connection_identity(inbound))

    # -- 身份 -----------------------------------------------------------------

    def _binding_required(self) -> bool:
        """与 manager 门禁一致：启用 channel_connections 且 require_bound_identity 时要求绑定，免登录模式放行。同步读配置。"""
        if self._connection_repo is None or _auth_disabled_owner_user_id():
            return False
        from deerflow.config.app_config import get_app_config

        connections = getattr(get_app_config(), "channel_connections", None)
        return bool(getattr(connections, "require_bound_identity", True))

    def _identity_blocking_parts(self, probe: InboundMessage) -> tuple[str | None, bool]:
        return _channel_storage_user_id(probe), self._binding_required()

    async def _resolve_identity(self, chat_id: str | None, open_id: str | None) -> ChatIdentity:
        """已绑定：归属 owner，当前会话指针在绑定库；未绑定：归属飞书用户，指针在 ChannelStore。"""
        probe = self._make_inbound(chat_id=chat_id or "", user_id=open_id or "", text="")
        if chat_id:
            probe = await self._attach_connection_identity(probe)
        bound = bool(probe.connection_id and probe.owner_user_id)
        if bound and self._connection_repo is not None:
            current = await self._connection_repo.get_thread_id(probe.connection_id, probe.chat_id, None)
        else:
            store = self._channel_store()
            current = store.get_thread_id(self.name, chat_id) if (store is not None and chat_id) else None
        storage_user_id, binding_required = await asyncio.to_thread(self._identity_blocking_parts, probe)
        return ChatIdentity(bound=bound, binding_required=binding_required, storage_user_id=storage_user_id, current_thread_id=current)

    def _resolve_identity_blocking(self, chat_id: str, open_id: str | None) -> ChatIdentity | None:
        """供 lark 线程里的卡片回调使用：把身份解析投到主循环并限时等待，失败返回 None。"""
        loop = self._main_loop
        if loop is None or not loop.is_running():
            return None
        future = asyncio.run_coroutine_threadsafe(self._resolve_identity(chat_id, open_id), loop)
        try:
            return future.result(timeout=_IDENTITY_TIMEOUT_SECONDS)
        except Exception:
            future.cancel()
            logger.warning("[Feishu] resolving card identity failed: chat_id=%s", chat_id, exc_info=True)
            return None

    # -- 卡片发送 -------------------------------------------------------------

    def _build_card(self, kind: str, chat_id: str, identity: ChatIdentity) -> dict[str, Any]:
        """按会话的 store 状态与身份构建某类卡片（同步 IO，主循环里调用需放进线程池）。"""
        store = self._channel_store()
        has_chat = store is not None and bool(chat_id)
        if kind == "repo":
            current = store.get_workdir(self.name, chat_id) if has_chat else None
            history = store.get_workdir_history(self.name, chat_id) if has_chat else []
            return build_repo_card(current, history, list_project_workdirs())
        if kind == "agent":
            current = (store.get_agent(self.name, chat_id) if has_chat else None) or DEFAULT_PERSONA
            return build_agent_card(current, list_personas(identity.storage_user_id))
        if kind == "sessions":
            sessions = store.list_sessions(self.name, chat_id) if has_chat else []
            return build_sessions_card(sessions, identity.current_thread_id)
        if kind == "model":
            models = available_models()
            current = (store.get_model(self.name, chat_id) if has_chat else None) or (models[0][0] if models else None)
            return build_model_card(current, models)
        return build_memory_card()

    async def _card_or_binding_hint(self, kind: str, chat_id: str | None, open_id: str | None) -> dict[str, Any] | str:
        identity = await self._resolve_identity(chat_id, open_id)
        if identity.blocked:
            return self._build_card_content(_BINDING_REQUIRED_TEXT)
        return await asyncio.to_thread(self._build_card, kind, chat_id or "", identity)

    async def _reply_card_of_kind(self, msg_id: str, chat_id: str, open_id: str | None, kind: str) -> None:
        await self._send_interactive(await self._card_or_binding_hint(kind, chat_id, open_id), reply_to=msg_id)
        logger.info("[Feishu] %s card sent: chat_id=%s", kind, chat_id)

    async def _create_card_of_kind(self, chat_id: str | None, open_id: str | None, kind: str) -> None:
        await self._send_interactive(await self._card_or_binding_hint(kind, chat_id, open_id), chat_id=chat_id, open_id=open_id)
        logger.info("[Feishu] %s card created: chat_id=%s open_id=%s", kind, chat_id, open_id)

    # -- 入站消息：记录 open_id 映射，裸命令回卡片 ---------------------------

    def _on_message(self, event) -> None:
        try:
            if self._intercept_card_command(event):
                return
        except Exception:
            logger.exception("[Feishu] fork pre-processing failed, falling back to upstream handling")
        super()._on_message(event)

    def _intercept_card_command(self, event) -> bool:
        message = event.event.message
        chat_id = self._non_empty_str(getattr(message, "chat_id", None))
        msg_id = self._non_empty_str(getattr(message, "message_id", None))
        open_id = self._non_empty_str(getattr(event.event.sender.sender_id, "open_id", None))

        # 机器人菜单事件只带 open_id：在 p2p 消息里记下 open_id → 单聊 chat_id
        store = self._channel_store()
        if getattr(message, "chat_type", None) == "p2p" and store is not None and chat_id and open_id:
            store.set_user_chat(self.name, open_id, chat_id)

        if not (chat_id and msg_id):
            return False
        try:
            content = json.loads(message.content)
        except (TypeError, ValueError):
            return False
        text = content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str):
            return False
        kind = _CARD_COMMANDS.get(strip_leading_mentions(text.strip()).strip().lower())
        if kind is None:
            return False
        self._schedule(self._reply_card_of_kind(msg_id, chat_id, open_id, kind), name=f"send_{kind}_card", msg_id=msg_id)
        return True

    # -- 机器人自定义菜单 -----------------------------------------------------

    def _on_bot_menu(self, data) -> None:
        try:
            event = getattr(data, "event", None)
            event_key = str(getattr(event, "event_key", "") or "").strip().upper()
            operator_id = getattr(getattr(event, "operator", None), "operator_id", None)
            open_id = self._non_empty_str(getattr(operator_id, "open_id", None))
            logger.info("[Feishu] bot menu event: key=%s open_id=%s", event_key, open_id)

            store = self._channel_store()
            chat_id = store.get_user_chat(self.name, open_id) if (store is not None and open_id) else None

            kind = _MENU_CARDS.get(event_key)
            if kind is not None:
                self._schedule(self._create_card_of_kind(chat_id, open_id, kind), name=f"menu_{kind}_card", msg_id=event_key)
                return

            if event_key.startswith(_MENU_AGENT_PREFIX):
                command: str | None = "/agent " + event_key[len(_MENU_AGENT_PREFIX) :].lower()
            else:
                command = _MENU_COMMANDS.get(event_key)
            if command is None:
                logger.warning("[Feishu] unknown bot menu event_key: %s", event_key)
                return
            if not chat_id:
                # 还没和机器人单聊过，找不到会话：提示后返回
                if open_id:
                    self._schedule(self._send_interactive(self._build_card_content("请先给我发送一条消息，然后再使用菜单。"), open_id=open_id), name="menu_hint", msg_id=event_key)
                return
            self._publish_synthetic_command(chat_id, open_id, command, source=BOT_MENU_SOURCE)
        except Exception:
            logger.exception("[Feishu] error handling bot menu event")

    # -- 卡片按钮回调（lark 线程内同步执行，须尽快返回）------------------------

    @staticmethod
    def _card_action_open_id(event) -> str | None:
        """取点击者 open_id（兼容不同 lark 版本的字段位置）。"""
        operator = getattr(event, "operator", None)
        if operator is None:
            return None
        open_id = getattr(operator, "open_id", None)
        if isinstance(open_id, str) and open_id.strip():
            return open_id
        nested = getattr(getattr(operator, "operator_id", None), "open_id", None)
        return nested if isinstance(nested, str) and nested.strip() else None

    def _on_card_action(self, data):
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        try:
            event = data.event
            value = (event.action.value if event and event.action else None) or {}
            action = value.get("action") if isinstance(value, dict) else None
            chat_id = event.context.open_chat_id if event and event.context else None
            store = self._channel_store()
            if not chat_id or store is None or action not in _CARD_ACTIONS:
                return P2CardActionTriggerResponse({})
            open_id = self._card_action_open_id(event)
            identity = self._resolve_identity_blocking(chat_id, open_id)
            if identity is None:
                return P2CardActionTriggerResponse({"toast": _toast("error", "处理失败，请重试")})
            if identity.blocked:
                return P2CardActionTriggerResponse({"toast": _toast("warning", _BINDING_REQUIRED_TOAST)})
            context = CardActionContext(store=store, chat_id=chat_id, open_id=open_id, identity=identity, value=value)
            payload = getattr(self, f"_card_action_{action}")(context)
            logger.info("[Feishu] card action %s handled: chat_id=%s", action, chat_id)
            return P2CardActionTriggerResponse(payload)
        except Exception:
            logger.exception("[Feishu] error handling card action")
            return P2CardActionTriggerResponse({"toast": _toast("error", "处理失败，请重试")})

    @staticmethod
    def _refresh(toast: dict[str, str], card: dict[str, Any]) -> dict[str, Any]:
        return {"toast": toast, "card": {"type": "raw", "data": card}}

    def _card_action_set_workdir(self, ctx: CardActionContext) -> dict[str, Any]:
        path = ctx.value.get("path")
        workdir = normalize_workdir(path) if isinstance(path, str) else None
        if workdir is None:
            return {"toast": _toast("error", "无效的目录")}
        ctx.store.set_workdir(self.name, ctx.chat_id, workdir)
        return self._refresh(_toast("success", f"工作目录已切换：{workdir.rsplit('/', 1)[-1]}"), self._build_card("repo", ctx.chat_id, ctx.identity))

    def _card_action_clear_workdir(self, ctx: CardActionContext) -> dict[str, Any]:
        ctx.store.clear_workdir(self.name, ctx.chat_id)
        return self._refresh(_toast("info", "已清除工作目录设置"), self._build_card("repo", ctx.chat_id, ctx.identity))

    def _card_action_set_model(self, ctx: CardActionContext) -> dict[str, Any]:
        models = available_models()
        name = ctx.value.get("name")
        if not isinstance(name, str) or name not in {model_name for model_name, _ in models}:
            return {"toast": _toast("error", "无效的模型")}
        if name == models[0][0]:
            ctx.store.clear_model(self.name, ctx.chat_id)
        else:
            ctx.store.set_model(self.name, ctx.chat_id, name)
        return self._refresh(_toast("success", f"模型已切换：{name}"), self._build_card("model", ctx.chat_id, ctx.identity))

    def _card_action_clear_model(self, ctx: CardActionContext) -> dict[str, Any]:
        ctx.store.clear_model(self.name, ctx.chat_id)
        models = available_models()
        return self._refresh(_toast("info", f"已恢复默认模型：{models[0][0] if models else '默认'}"), self._build_card("model", ctx.chat_id, ctx.identity))

    def _card_action_set_agent(self, ctx: CardActionContext) -> dict[str, Any]:
        personas = list_personas(ctx.identity.storage_user_id)
        name = ctx.value.get("name")
        if not isinstance(name, str) or name not in {persona_name for persona_name, _, _ in personas}:
            return {"toast": _toast("error", "无效的人设")}
        # 上游把人设钉在线程上，切换 = 以该人设开启新会话（由 manager 创建线程并回复确认）
        self._publish_synthetic_command(ctx.chat_id, ctx.open_id, f"/agent use {name}", source=CARD_ACTION_SOURCE)
        return self._refresh(_toast("info", f"正在切换到「{persona_label(name)}」并开启新会话…"), build_agent_card(name, personas))

    def _card_action_clear_agent(self, ctx: CardActionContext) -> dict[str, Any]:
        self._publish_synthetic_command(ctx.chat_id, ctx.open_id, f"/agent use {DEFAULT_PERSONA}", source=CARD_ACTION_SOURCE)
        personas = list_personas(ctx.identity.storage_user_id)
        return self._refresh(_toast("info", f"正在恢复「{DEFAULT_PERSONA_LABEL}」并开启新会话…"), build_agent_card(DEFAULT_PERSONA, personas))

    def _card_action_view_memory(self, ctx: CardActionContext) -> dict[str, Any]:
        scope = ctx.value.get("scope")
        if scope not in MEMORY_SCOPES:
            return {"toast": _toast("error", "无效的记忆范围")}
        body = render_memory(scope, user_id=ctx.identity.storage_user_id, persona=ctx.store.get_agent(self.name, ctx.chat_id))
        return self._refresh(_toast("info", _MEMORY_SCOPE_LABELS[scope]), build_memory_card(scope, body))

    def _card_action_switch_session(self, ctx: CardActionContext) -> dict[str, Any]:
        thread_id = ctx.value.get("thread_id")
        snapshot = ctx.store.switch_session(self.name, ctx.chat_id, thread_id) if isinstance(thread_id, str) and thread_id else None
        if snapshot is None:
            return {"toast": _toast("error", "会话不存在")}
        # 会话设置已在本地恢复；当前会话指针（已绑定时在绑定库）交给 manager 写
        self._publish_synthetic_command(ctx.chat_id, ctx.open_id, f"/sessions _gcswitch {thread_id}", source=CARD_ACTION_SOURCE)
        card = self._build_card("sessions", ctx.chat_id, replace(ctx.identity, current_thread_id=thread_id))
        return self._refresh(_toast("success", f"已切换：{session_display_title(snapshot)}"), card)

    def _card_action_view_session(self, ctx: CardActionContext) -> dict[str, Any]:
        thread_id = ctx.value.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return {"toast": _toast("error", "无效的会话")}
        self._publish_synthetic_command(ctx.chat_id, ctx.open_id, f"/sessions view {thread_id}", source=CARD_ACTION_SOURCE)
        return {"toast": _toast("info", "正在查看会话…")}

    def _card_action_delete_session(self, ctx: CardActionContext) -> dict[str, Any]:
        thread_id = ctx.value.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return {"toast": _toast("error", "无效的会话")}
        # 本地即时移除并刷新卡片；网关线程与当前会话指针交给 manager（它持有网关客户端与绑定库）
        if not ctx.store.remove_session(self.name, ctx.chat_id, thread_id).get("removed"):
            return self._refresh(_toast("error", "会话不存在"), self._build_card("sessions", ctx.chat_id, ctx.identity))
        self._publish_synthetic_command(ctx.chat_id, ctx.open_id, f"/sessions _gcdelete {thread_id}", source=CARD_ACTION_SOURCE)
        current = ctx.identity.current_thread_id
        if current == thread_id:
            # 与 manager 一致：删掉当前会话后切到剩余最近的会话
            remaining = ctx.store.list_sessions(self.name, ctx.chat_id)
            current = remaining[0]["thread_id"] if remaining else None
        card = self._build_card("sessions", ctx.chat_id, replace(ctx.identity, current_thread_id=current))
        return self._refresh(_toast("success", "已删除"), card)

    def _card_action_new_session(self, ctx: CardActionContext) -> dict[str, Any]:
        self._publish_synthetic_command(ctx.chat_id, ctx.open_id, "/new", source=CARD_ACTION_SOURCE)
        return {"toast": _toast("info", "正在新建会话…")}
