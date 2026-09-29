"""ForkChannelManager：在上游 ChannelManager 之上叠加 fork 的会话级功能。

- 会话工作目录（/repo）：消息前注入目录上下文。
- 会话模型覆盖（/model）：run context 注入 model_name（网关按模型白名单校验）。
- 人设：沿用上游「人设钉在线程上」的机制；fork 只让人设跨 /new 保持，并兼容旧 fork 线程。
- 多会话（/sessions）：新建线程自动登记，支持列表 / 切换 / 查看 / 重命名 / 删除。
- 记忆（/memory [global|default|persona]）与增强的 /status、/help。
- 流式进度：由 progress.StreamProgressObserver 经上游 ``_make_stream_observer`` 钩子接入。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from app.channels.fork.memory_view import render_memory
from app.channels.fork.personas import DEFAULT_PERSONA, DEFAULT_PERSONA_ALIASES, list_personas, persona_label
from app.channels.fork.progress import NOTIFICATION_METADATA_KEY, LiveRunRegistry, StreamProgressObserver, extract_title
from app.channels.fork.workdir import list_project_workdirs, normalize_workdir
from app.channels.manager import (
    ChannelManager,
    _channel_storage_user_id,
    _extract_response_text,
    _owner_headers,
    _slim_metadata,
)
from app.channels.message_bus import InboundMessage, OutboundMessage
from app.gateway.internal_auth import create_internal_auth_headers

logger = logging.getLogger(__name__)

WORKDIR_PREFIX_TEMPLATE = "[当前工作目录：{workdir}。未明确指定其他路径时，所有代码/文件操作均在此目录进行。]\n\n"

# 卡片回调投递的合成命令带此来源标记；用户手打的文本消息无法伪造 metadata
FORK_SOURCE_METADATA_KEY = "fork_source"
CARD_ACTION_SOURCE = "card_action"
BOT_MENU_SOURCE = "bot_menu"

# 这些渠道能识别 NOTIFICATION_METADATA_KEY，把失败通知另发为新消息
_NOTIFICATION_CHANNELS = frozenset({"feishu"})

_FORK_HANDLED_COMMANDS = frozenset({"repo", "sessions", "model", "memory", "status", "help"})
_MODEL_RESET_ALIASES = frozenset({"clear", "default", "reset"})
_SESSIONS_HINT = "用 /sessions switch <序号> 切换，/sessions view <序号> 查看，/sessions rename <序号> <名称> 重命名，/sessions delete <序号> 删除。"
_SESSION_REPLY_SNIPPET_CHARS = 200

FORK_HELP_TEXT = (
    "可用命令：\n"
    "/new — 开启新会话（沿用当前人设、模型与工作目录）\n"
    "/sessions — 历史会话列表（/sessions switch|view|rename|delete <序号>）\n"
    "/repo — 查看或选择工作目录（/repo <名称> 设置，/repo clear 清除）\n"
    "/agent — 查看或选择人设（/agent <名称> 切换并开启新会话，/agent default 恢复通用助手，/agent list 列表）\n"
    "/model — 查看或选择模型（/model <名称> 切换，/model default 恢复默认）\n"
    "/models — 列出可用模型\n"
    "/memory [global|default|persona] — 查看记忆（默认当前用户全局记忆）\n"
    "/status — 当前会话、运行进度与设置\n"
    "/goal [条件|clear] — 设置、查看或清除目标\n"
    "/bootstrap — 启动引导会话（用于创建 agent）\n"
    "/<skill-name> <任务> — 本轮启用某个 skill\n"
    "/help — 显示本帮助"
)


def available_models() -> list[tuple[str, str | None]]:
    """(name, display_name) 列表，首个为默认模型；读取失败返回空列表（不阻塞渠道）。"""
    try:
        from deerflow.config.app_config import get_app_config

        return [(m.name, getattr(m, "display_name", None)) for m in get_app_config().models]
    except Exception:
        logger.debug("list models failed", exc_info=True)
        return []


def relative_time(ts: Any) -> str:
    try:
        delta = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    return f"{int(delta // 86400)}天前"


def session_display_title(session: Mapping[str, Any]) -> str:
    title = session.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else "未命名会话"


def _resolve_session_ref(sessions: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """把用户输入（1 起的序号、thread_id 或其唯一前缀）解析为会话快照。"""
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        idx = int(token)
        return sessions[idx - 1] if 1 <= idx <= len(sessions) else None
    for session in sessions:
        if session.get("thread_id") == token:
            return session
    matches = [s for s in sessions if str(s.get("thread_id", "")).startswith(token)]
    return matches[0] if len(matches) == 1 else None


class ForkChannelManager(ChannelManager):
    """由 ChannelService 构造；未覆盖的行为与上游 ChannelManager 完全一致。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.live_runs = LiveRunRegistry()
        # 已尝试过沿用旧指针的会话 (connection_id, chat_id, topic_id)
        self._legacy_adoption_checked: set[tuple[str, str, str | None]] = set()

    # -- 上游钩子 -------------------------------------------------------------

    def _make_stream_observer(self, msg: InboundMessage, thread_id: str) -> StreamProgressObserver:
        return StreamProgressObserver(self, msg, thread_id)

    def _resolve_run_params(self, msg: InboundMessage, thread_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        assistant_id, run_config, run_context = super()._resolve_run_params(msg, thread_id)
        model = self._session_model(msg)
        if model:
            run_context["model_name"] = model
        return assistant_id, run_config, run_context

    async def _handle_chat(
        self,
        msg: InboundMessage,
        extra_context: dict[str, Any] | None = None,
        *,
        bound_identity_checked: bool = False,
    ) -> None:
        workdir = self.store.get_workdir(msg.channel_name, msg.chat_id)
        if workdir:
            msg = dataclasses.replace(msg, text=WORKDIR_PREFIX_TEMPLATE.format(workdir=workdir) + msg.text)
        await super()._handle_chat(msg, extra_context, bound_identity_checked=bound_identity_checked)

    async def _create_thread(self, client, msg: InboundMessage, *, agent_name: str | None = None) -> str:
        if agent_name is None:
            # 未显式选择时沿用本会话当前人设，让人设跨 /new 保持（与旧 fork 行为一致）
            agent_name = await self._sticky_persona(msg)
        thread_id = await super()._create_thread(client, msg, agent_name=agent_name)
        self.store.add_session(msg.channel_name, msg.chat_id, thread_id)
        if agent_name == DEFAULT_PERSONA:
            self.store.clear_agent(msg.channel_name, msg.chat_id)
        elif agent_name:
            self.store.set_agent(msg.channel_name, msg.chat_id, agent_name)
        return thread_id

    async def _load_thread_agent(self, client, msg: InboundMessage, thread_id: str) -> str | None:
        agent_name = await super()._load_thread_agent(client, msg, thread_id)
        if agent_name is not None:
            return agent_name
        stored = self.store.get_agent(msg.channel_name, msg.chat_id)
        if stored:
            # 线程没有钉住 agent（旧 fork 线程）却有会话人设：沿用它。fork 新建的线程在有人设时都会钉住，
            # 切换会话也会恢复该会话的人设，所以这里只会命中升级前的线程。
            self._remember_thread_agent(thread_id, stored)
            return stored
        return None

    def _is_bound(self, msg: InboundMessage) -> bool:
        return bool(msg.connection_id and msg.owner_user_id and self._connection_repo is not None)

    async def _lookup_thread_id(self, msg: InboundMessage) -> str | None:
        thread_id = await super()._lookup_thread_id(msg)
        if thread_id or not self._is_bound(msg):
            return thread_id
        return await self._adopt_legacy_thread(msg)

    async def _adopt_legacy_thread(self, msg: InboundMessage) -> str | None:
        """刚绑定、绑定库里还没有映射时，沿用绑定前 ChannelStore 里的会话指针，让对话接着进行。

        只有线程归属已迁移到该账号（见 scripts/fork/migrate_default_owner.py）才能读到它；
        读不到就放弃，下一条消息照常新建会话。每个会话在进程内只尝试一次。
        """
        key = (msg.connection_id or "", msg.chat_id, msg.topic_id)
        legacy = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        if not legacy or key in self._legacy_adoption_checked:
            return None
        self._legacy_adoption_checked.add(key)
        try:
            await self._get_client().threads.get(legacy, **self._owner_kwargs(msg))
        except Exception:
            logger.info("[Fork] legacy thread %s is not accessible for the bound owner; a new conversation will start", legacy)
            return None
        await self._store_thread_id(msg, legacy)
        logger.info("[Fork] adopted legacy thread %s for connection %s", legacy, msg.connection_id)
        return legacy

    # -- 命令 ---------------------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        parts = msg.text.strip().split(maxsplit=1)
        command = parts[0].lower().removeprefix("/") if parts and msg.text.startswith("/") else None
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "agent" and arg:
            rewritten = self._rewrite_agent_args(arg)
            if rewritten != arg:
                msg = dataclasses.replace(msg, text=f"/agent {rewritten}")
        if command not in _FORK_HANDLED_COMMANDS and not (command == "agent" and not arg):
            await super()._handle_command(msg)
            return

        rejection = await self._get_bound_identity_rejection(msg)
        if rejection is not None:
            await self._reject_unbound_channel_message(msg, bound_identity_rejection=rejection)
            return

        if command == "repo":
            reply: str | None = self._handle_repo_command(msg, arg)
        elif command == "sessions":
            reply = await self._handle_sessions_command(msg, arg)
        elif command == "model":
            reply = self._handle_model_command(msg, arg)
        elif command == "memory":
            reply = await self._render_memory(msg, arg.lower() or "global")
        elif command == "status":
            reply = await self._status_reply(msg)
        elif command == "agent":
            reply = await self._agent_overview(msg)
        else:
            reply = FORK_HELP_TEXT

        if reply is not None:
            await self._publish_reply(msg, reply)

    async def _publish_reply(self, msg: InboundMessage, text: str) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id=await self._lookup_thread_id(msg) or "",
                text=text,
                thread_ts=msg.thread_ts,
                connection_id=msg.connection_id,
                owner_user_id=msg.owner_user_id,
                metadata=_slim_metadata(msg.metadata),
            )
        )

    # -- 人设 ---------------------------------------------------------------

    @staticmethod
    def _rewrite_agent_args(arg: str) -> str:
        """``/agent <名称>`` → ``use <名称>``；默认人设别名 → ``use lead_agent``；list/use 原样交给上游。"""
        tokens = arg.split()
        if tokens[0].lower() in ("list", "use"):
            return arg
        if len(tokens) == 1:
            return f"use {DEFAULT_PERSONA}" if tokens[0].lower() in DEFAULT_PERSONA_ALIASES else f"use {tokens[0]}"
        return arg

    def _default_persona(self, msg: InboundMessage) -> str:
        channel_layer, user_layer = self._resolve_session_layer(msg)
        default_id = user_layer.get("assistant_id") or channel_layer.get("assistant_id") or self._default_session.get("assistant_id") or self._assistant_id
        return default_id if isinstance(default_id, str) and default_id.strip() else DEFAULT_PERSONA

    def current_persona(self, msg: InboundMessage, thread_id: str | None) -> str:
        """当前生效的人设：线程钉住的 agent > 会话人设 > 会话配置默认。"""
        pinned = self._thread_agent_names.get(thread_id) if thread_id else None
        return pinned or self.store.get_agent(msg.channel_name, msg.chat_id) or self._default_persona(msg)

    async def _sticky_persona(self, msg: InboundMessage) -> str | None:
        stored = self.store.get_agent(msg.channel_name, msg.chat_id)
        if not stored or stored == DEFAULT_PERSONA:
            return None
        from deerflow.config.agents_config import load_agent_config

        try:
            await asyncio.to_thread(load_agent_config, stored, user_id=_channel_storage_user_id(msg))
        except FileNotFoundError:
            logger.warning("[Fork] stored persona %r no longer exists; new thread falls back to default", stored)
            self.store.clear_agent(msg.channel_name, msg.chat_id)
            return None
        except Exception:
            logger.warning("[Fork] failed to validate stored persona %r; new thread falls back to default", stored, exc_info=True)
            return None
        return stored

    async def _agent_overview(self, msg: InboundMessage) -> str:
        user_id = await asyncio.to_thread(_channel_storage_user_id, msg)
        personas = await asyncio.to_thread(list_personas, user_id)
        current = self.current_persona(msg, await self._lookup_thread_id(msg))
        lines = [f"当前人设：{persona_label(current)}", "", "可选人设："]
        for name, label, desc in personas:
            mark = " ✓" if name == current else ""
            suffix = f" — {desc}" if desc else ""
            lines.append(f"• {label}（{name}）{mark}{suffix}")
        lines.extend(["", "用 /agent <名称> 切换（会开启新会话），/agent default 恢复通用助手。"])
        return "\n".join(lines)

    # -- 工作目录 -----------------------------------------------------------

    def _handle_repo_command(self, msg: InboundMessage, arg: str) -> str:
        if arg.lower() == "clear":
            cleared = self.store.clear_workdir(msg.channel_name, msg.chat_id)
            return "已清除工作目录设置。" if cleared else "当前未设置工作目录。"

        if arg:
            workdir = normalize_workdir(arg)
            if workdir is None:
                available = list_project_workdirs()
                hint = "\n".join(f"• {p}" for p in available) if available else "（项目目录为空）"
                return f"无效的工作目录：{arg}\n可选目录：\n{hint}"
            self.store.set_workdir(msg.channel_name, msg.chat_id, workdir)
            return f"工作目录已设置为：{workdir}"

        current = self.store.get_workdir(msg.channel_name, msg.chat_id)
        recent = [h for h in self.store.get_workdir_history(msg.channel_name, msg.chat_id) if h != current][:5]
        available = list_project_workdirs()
        lines = [f"当前工作目录：{current or '未设置'}"]
        if recent:
            lines.extend(["", "最近使用：", *(f"• {p}" for p in recent)])
        lines.extend(["", "全部可选："])
        if available:
            lines.extend(f"• {p}" for p in available)
        else:
            lines.append("（项目目录为空，把代码放到宿主机 /work/projects/ 下）")
        lines.extend(["", "用 /repo <名称或路径> 设置，/repo clear 清除。"])
        return "\n".join(lines)

    # -- 模型 ---------------------------------------------------------------

    def _session_model(self, msg: InboundMessage) -> str | None:
        """会话选定且仍在配置中的模型；失效的模型不注入（网关会以 400 拒绝整轮对话）。"""
        model = self.store.get_model(msg.channel_name, msg.chat_id)
        if not model:
            return None
        if model not in {name for name, _label in available_models()}:
            logger.warning("[Fork] session model %r is no longer configured; using the default model", model)
            return None
        return model

    def _handle_model_command(self, msg: InboundMessage, arg: str) -> str:
        models = available_models()
        default_name = models[0][0] if models else None

        if arg.lower() in _MODEL_RESET_ALIASES:
            self.store.clear_model(msg.channel_name, msg.chat_id)
            return f"已恢复默认模型：{default_name or '（未配置）'}"

        if arg:
            target = next((name for name, _label in models if name.lower() == arg.lower()), None)
            if target is None:
                hint = "\n".join(f"• {name}（{label}）" if label and label != name else f"• {name}" for name, label in models) or "（未配置模型）"
                return f"无效的模型：{arg}\n可选模型：\n{hint}"
            if target == default_name:
                self.store.clear_model(msg.channel_name, msg.chat_id)
            else:
                self.store.set_model(msg.channel_name, msg.chat_id, target)
            return f"模型已切换：{target}"

        current = self.store.get_model(msg.channel_name, msg.chat_id) or default_name
        lines = [f"当前模型：{current or '（未配置）'}", "", "可选模型："]
        for name, label in models:
            mark = " ✓" if name == current else ""
            suffix = f" — {label}" if label and label != name else ""
            lines.append(f"• {name}{mark}{suffix}")
        lines.extend(["", "用 /model <名称> 切换，/model default 恢复默认。"])
        return "\n".join(lines)

    # -- 记忆与状态 ---------------------------------------------------------

    async def _render_memory(self, msg: InboundMessage, scope: str) -> str:
        persona = self.current_persona(msg, await self._lookup_thread_id(msg))

        def _render() -> str:
            return render_memory(scope, user_id=_channel_storage_user_id(msg), persona=persona)

        return await asyncio.to_thread(_render)

    async def _status_reply(self, msg: InboundMessage) -> str:
        thread_id = await self._lookup_thread_id(msg)
        sections: list[str] = []

        snapshot = self.live_runs.get(thread_id)
        if snapshot and snapshot.get("status") == "running":
            sections.append(LiveRunRegistry.render(snapshot))

        if thread_id:
            run_status = await self._fetch_run_status(msg, thread_id)
            if run_status:
                sections.append(run_status)

        models = available_models()
        model = self.store.get_model(msg.channel_name, msg.chat_id) or (models[0][0] if models else "（未配置）")
        workdir = self.store.get_workdir(msg.channel_name, msg.chat_id)
        sections.append(
            "\n".join(
                [
                    f"Active thread: {thread_id}" if thread_id else "No active conversation.",
                    f"当前工作目录：{workdir or '未设置（用 /repo 选择）'}",
                    f"当前人设：{persona_label(self.current_persona(msg, thread_id))}",
                    f"当前模型：{model}",
                ]
            )
        )
        return "\n\n".join(sections)

    async def _fetch_run_status(self, msg: InboundMessage, thread_id: str) -> str:
        """best-effort：从网关取该线程最新 run 的状态与累计 token（失败返回空串）。"""
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self._gateway_url}/api/threads/{quote(thread_id, safe='')}/runs",
                    timeout=10,
                    headers=_owner_headers(msg) or create_internal_auth_headers(),
                )
                resp.raise_for_status()
                runs = resp.json()
        except Exception:
            logger.debug("[Fork] failed to fetch run status for thread %s", thread_id, exc_info=True)
            return ""
        if not isinstance(runs, list) or not runs:
            return ""
        latest = max((r for r in runs if isinstance(r, Mapping)), key=lambda r: str(r.get("created_at", "")), default=None)
        if latest is None:
            return ""
        lines = [f"运行状态：{latest.get('status') or 'unknown'}"]
        total_tokens = latest.get("total_tokens") or 0
        message_count = latest.get("message_count") or 0
        if total_tokens or message_count:
            lines.append(f"累计 token：{total_tokens}（消息 {message_count}）")
        return "\n".join(lines)

    # -- 流式进度回调（供 StreamProgressObserver 使用）------------------------

    async def publish_failure_notification(self, msg: InboundMessage, thread_id: str, step: Mapping[str, str]) -> None:
        """子任务失败/超时：推送一条独立通知（渠道据 NOTIFICATION_METADATA_KEY 另发新消息）。"""
        if msg.channel_name not in _NOTIFICATION_CHANNELS:
            return
        lines = [f"子任务执行失败：{str(step.get('description', '')).strip() or '子任务'}"]
        workdir = self.store.get_workdir(msg.channel_name, msg.chat_id)
        if workdir:
            lines.append(f"工作目录：{workdir}")
        error_text = str(step.get("error", "")).strip()
        if error_text:
            lines.append(f"错误：{error_text[:500]}")
        metadata = _slim_metadata(msg.metadata)
        metadata[NOTIFICATION_METADATA_KEY] = True
        await self.bus.publish_outbound(
            OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id=thread_id,
                text="\n".join(lines),
                is_final=False,
                thread_ts=msg.thread_ts,
                connection_id=msg.connection_id,
                owner_user_id=msg.owner_user_id,
                metadata=metadata,
            )
        )

    def record_session_activity(self, msg: InboundMessage, thread_id: str, result: Any) -> None:
        """每轮结束：缓存自动生成的标题，没有标题时只刷新会话活跃时间。"""
        if not thread_id:
            return
        title = extract_title(result)
        if title:
            self.store.set_session_title(msg.channel_name, msg.chat_id, thread_id, title)
        else:
            self.store.touch_session(msg.channel_name, msg.chat_id, thread_id)

    # -- /sessions 多会话 ----------------------------------------------------

    async def _handle_sessions_command(self, msg: InboundMessage, arg: str) -> str | None:
        """无参列表；子命令 switch / view / rename / delete / new。返回 None 表示静默。

        会话登记在 ChannelStore（按 chat）；「当前会话」指针走上游 ``_lookup_thread_id`` /
        ``_store_thread_id``：已绑定账号时在绑定库，未绑定时在 ChannelStore。
        """
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "list", "ls"):
            return await self._render_sessions_list(msg)
        if sub in ("switch", "use", "resume", "open"):
            return await self._switch_session(msg, rest)
        if sub in ("view", "show", "info"):
            return await self._view_session(msg, rest)
        if sub in ("rename", "name"):
            return await self._rename_session(msg, rest)
        if sub in ("delete", "del", "rm", "remove"):
            return await self._delete_session(msg, rest)
        if sub == "new":
            await self._create_thread(self._get_client(), msg)
            return "已开启新会话。直接发消息即可开始。"
        if sub == "_gcdelete":
            # 卡片删除已在本地移除登记并刷新卡片：这里清理网关线程，删的若是当前会话再修正指针；不回消息。
            # 只接受卡片回调投递的命令
            if rest and msg.metadata.get(FORK_SOURCE_METADATA_KEY) == CARD_ACTION_SOURCE:
                was_current = rest == await self._lookup_thread_id(msg)
                await self._delete_gateway_thread(msg, rest)
                if was_current:
                    await self._repoint_after_delete(msg)
            return None
        return f"未知子命令：{sub}\n{_SESSIONS_HINT}"

    def _session_persona_label(self, session: Mapping[str, Any]) -> str:
        agent = session.get("agent")
        return persona_label(agent if isinstance(agent, str) else None)

    async def _repoint_after_delete(self, msg: InboundMessage) -> dict[str, Any] | None:
        """当前会话被删后切到剩余最近的会话；一个不剩就新开一个。返回新的当前会话快照（新开时为 None）。"""
        remaining = self.store.list_sessions(msg.channel_name, msg.chat_id)
        if remaining:
            thread_id = remaining[0]["thread_id"]
            snapshot = self.store.switch_session(msg.channel_name, msg.chat_id, thread_id)
            await self._store_thread_id(msg, thread_id)
            return snapshot
        await self._create_thread(self._get_client(), msg)
        return None

    async def _render_sessions_list(self, msg: InboundMessage) -> str:
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        if not sessions:
            return "当前没有会话记录。直接发消息即可开始第一个会话。"
        current = await self._lookup_thread_id(msg)
        lines = [f"会话列表（共 {len(sessions)} 个）："]
        for idx, session in enumerate(sessions, 1):
            mark = "✓ " if session.get("thread_id") == current else ""
            meta = [self._session_persona_label(session)]
            when = relative_time(session.get("updated_at"))
            if when:
                meta.append(when)
            lines.append(f"{idx}. {mark}{session_display_title(session)}（{' · '.join(meta)}）")
        lines.extend(["", _SESSIONS_HINT])
        return "\n".join(lines)

    async def _switch_session(self, msg: InboundMessage, ref: str) -> str:
        target = _resolve_session_ref(self.store.list_sessions(msg.channel_name, msg.chat_id), ref)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        snapshot = self.store.switch_session(msg.channel_name, msg.chat_id, target["thread_id"])
        if snapshot is None:
            return "切换失败：会话不存在。"
        await self._store_thread_id(msg, target["thread_id"])
        settings = [f"人设：{self._session_persona_label(snapshot)}"]
        if snapshot.get("model"):
            settings.append(f"模型：{snapshot['model']}")
        if snapshot.get("workdir"):
            settings.append(f"工作目录：{snapshot['workdir']}")
        return f"已切换到会话：{session_display_title(snapshot)}\n已恢复 {'，'.join(settings)}。\n继续发消息即可在该会话上下文中对话。"

    async def _view_session(self, msg: InboundMessage, ref: str) -> str:
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        target = _resolve_session_ref(sessions, ref)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        thread_id = target["thread_id"]
        current = await self._lookup_thread_id(msg)
        lines = [f"会话：{session_display_title(target)}" + ("（当前）" if thread_id == current else ""), f"人设：{self._session_persona_label(target)}"]
        if target.get("model"):
            lines.append(f"模型：{target['model']}")
        if target.get("workdir"):
            lines.append(f"工作目录：{target['workdir']}")
        created = relative_time(target.get("created_at"))
        if created:
            lines.append(f"创建于：{created}")
        snippet = await self._fetch_thread_last_reply(msg, thread_id)
        if snippet:
            lines.extend(["", f"最近回复：{snippet}"])
        idx = next((i for i, s in enumerate(sessions, 1) if s.get("thread_id") == thread_id), None)
        if idx is not None and thread_id != current:
            lines.extend(["", f"用 /sessions switch {idx} 切换到该会话。"])
        return "\n".join(lines)

    async def _rename_session(self, msg: InboundMessage, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "用法：/sessions rename <序号> <新名称>"
        target = _resolve_session_ref(self.store.list_sessions(msg.channel_name, msg.chat_id), parts[0])
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        new_name = parts[1].strip()
        await self._rename_gateway_thread(msg, target["thread_id"], new_name)
        self.store.set_session_title(msg.channel_name, msg.chat_id, target["thread_id"], new_name)
        return f"已重命名为：{new_name}"

    async def _delete_session(self, msg: InboundMessage, ref: str) -> str:
        target = _resolve_session_ref(self.store.list_sessions(msg.channel_name, msg.chat_id), ref)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        thread_id = target["thread_id"]
        was_current = thread_id == await self._lookup_thread_id(msg)
        await self._delete_gateway_thread(msg, thread_id)
        result = self.store.remove_session(msg.channel_name, msg.chat_id, thread_id)
        if not result.get("removed"):
            return "删除失败：会话不存在。"
        lines = [f"已删除会话：{session_display_title(target)}"]
        if was_current:
            snapshot = await self._repoint_after_delete(msg)
            lines.append(f"已自动切换到最近会话：{session_display_title(snapshot)}" if snapshot else "这是当前会话，已为你开启新会话。")
        return "\n".join(lines)

    # -- 网关线程操作（/sessions 使用，失败只告警）----------------------------

    @staticmethod
    def _owner_kwargs(msg: InboundMessage) -> dict[str, Any]:
        headers = _owner_headers(msg)
        return {"headers": headers} if headers else {}

    async def _delete_gateway_thread(self, msg: InboundMessage, thread_id: str) -> None:
        try:
            await self._get_client().threads.delete(thread_id, **self._owner_kwargs(msg))
        except Exception:
            logger.warning("[Fork] failed to delete gateway thread %s", thread_id, exc_info=True)

    async def _rename_gateway_thread(self, msg: InboundMessage, thread_id: str, title: str) -> None:
        try:
            await self._get_client().threads.update_state(thread_id, {"title": title}, **self._owner_kwargs(msg))
        except Exception:
            logger.debug("[Fork] failed to rename gateway thread %s (non-fatal)", thread_id, exc_info=True)

    async def _fetch_thread_last_reply(self, msg: InboundMessage, thread_id: str) -> str:
        try:
            state = await self._get_client().threads.get_state(thread_id, **self._owner_kwargs(msg))
        except Exception:
            logger.debug("[Fork] failed to fetch state for thread %s", thread_id, exc_info=True)
            return ""
        values = state.get("values") if isinstance(state, Mapping) else None
        if not isinstance(values, Mapping):
            return ""
        text = _extract_response_text(dict(values)).strip().replace("\n", " ")
        if len(text) > _SESSION_REPLY_SNIPPET_CHARS:
            return text[:_SESSION_REPLY_SNIPPET_CHARS] + "…"
        return text
