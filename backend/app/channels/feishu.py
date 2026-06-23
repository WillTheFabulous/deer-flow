"""Feishu/Lark channel — connects to Feishu via WebSocket (no public IP needed)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Literal

from app.channels.base import Channel
from app.channels.commands import KNOWN_CHANNEL_COMMANDS
from app.channels.memory_view import MEMORY_SCOPES, render_memory
from app.channels.message_bus import (
    PENDING_CLARIFICATION_METADATA_KEY,
    RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths, make_safe_user_id
from deerflow.runtime.user_context import DEFAULT_USER_ID, get_effective_user_id
from deerflow.sandbox.sandbox_provider import get_sandbox_provider

logger = logging.getLogger(__name__)
PENDING_CLARIFICATION_TTL_SECONDS = 30 * 60


def _is_feishu_command(text: str) -> bool:
    if not text.startswith("/"):
        return False
    return text.split(maxsplit=1)[0].lower() in KNOWN_CHANNEL_COMMANDS


class FeishuChannel(Channel):
    """Feishu/Lark IM channel using the ``lark-oapi`` WebSocket client.

    Configuration keys (in ``config.yaml`` under ``channels.feishu``):
        - ``app_id``: Feishu app ID.
        - ``app_secret``: Feishu app secret.
        - ``verification_token``: (optional) Event verification token.

    The channel uses WebSocket long-connection mode so no public IP is required.

    Message flow:
        1. User sends a message → bot adds "OK" emoji reaction
        2. Bot replies in thread: "Working on it......"
        3. Agent processes the message and returns a result
        4. Bot replies in thread with the result
        5. Bot adds "DONE" emoji reaction to the original message
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="feishu", bus=bus, config=config)
        self._thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._api_client = None
        self._CreateMessageReactionRequest = None
        self._CreateMessageReactionRequestBody = None
        self._Emoji = None
        self._PatchMessageRequest = None
        self._PatchMessageRequestBody = None
        self._background_tasks: set[asyncio.Task] = set()
        self._running_card_ids: dict[str, str] = {}
        self._running_card_tasks: dict[str, asyncio.Task] = {}
        self._pending_clarifications: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._CreateFileRequest = None
        self._CreateFileRequestBody = None
        self._CreateImageRequest = None
        self._CreateImageRequestBody = None
        self._GetMessageResourceRequest = None
        self._thread_lock = threading.Lock()
        # chat_id -> chat_type（p2p / group），用于决定回复是否分线
        self._chat_types: dict[str, str] = {}
        # 单聊扁平化：p2p 单聊不新建话题、所有消息归一条连续线程
        self._flatten_p2p = bool(config.get("flatten_p2p", True))

    def _reply_in_thread_for(self, chat_id: str) -> bool:
        """决定给某会话回复时是否新建/沿用话题（thread）。

        p2p 单聊在 flatten 模式下行内回复（不分线）；群聊维持按话题分线。
        未知 chat_type 时保守地返回 True（沿用上游默认行为）。
        """
        if self._flatten_p2p and self._chat_types.get(chat_id) == "p2p":
            return False
        return True

    @staticmethod
    def _non_empty_str(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _pending_key(chat_id: str, user_id: str) -> tuple[str, str]:
        return (chat_id, user_id)

    @property
    def supports_streaming(self) -> bool:
        return True

    async def start(self) -> None:
        if self._running:
            return

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                CreateFileRequest,
                CreateFileRequestBody,
                CreateImageRequest,
                CreateImageRequestBody,
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
                CreateMessageRequest,
                CreateMessageRequestBody,
                Emoji,
                GetMessageResourceRequest,
                PatchMessageRequest,
                PatchMessageRequestBody,
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )
        except ImportError:
            logger.error("lark-oapi is not installed. Install it with: uv add lark-oapi")
            return

        self._lark = lark
        self._CreateMessageRequest = CreateMessageRequest
        self._CreateMessageRequestBody = CreateMessageRequestBody
        self._ReplyMessageRequest = ReplyMessageRequest
        self._ReplyMessageRequestBody = ReplyMessageRequestBody
        self._CreateMessageReactionRequest = CreateMessageReactionRequest
        self._CreateMessageReactionRequestBody = CreateMessageReactionRequestBody
        self._Emoji = Emoji
        self._PatchMessageRequest = PatchMessageRequest
        self._PatchMessageRequestBody = PatchMessageRequestBody
        self._CreateFileRequest = CreateFileRequest
        self._CreateFileRequestBody = CreateFileRequestBody
        self._CreateImageRequest = CreateImageRequest
        self._CreateImageRequestBody = CreateImageRequestBody
        self._GetMessageResourceRequest = GetMessageResourceRequest

        app_id = self.config.get("app_id", "")
        app_secret = self.config.get("app_secret", "")
        domain = self.config.get("domain", "https://open.feishu.cn")

        if not app_id or not app_secret:
            logger.error("Feishu channel requires app_id and app_secret")
            return

        self._api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()
        logger.info("[Feishu] using domain: %s", domain)
        self._main_loop = asyncio.get_event_loop()

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Both ws.Client construction and start() must happen in a dedicated
        # thread with its own event loop.  lark-oapi caches the running loop
        # at construction time and later calls loop.run_until_complete(),
        # which conflicts with an already-running uvloop.
        self._thread = threading.Thread(
            target=self._run_ws,
            args=(app_id, app_secret, domain),
            daemon=True,
        )
        self._thread.start()
        logger.info("Feishu channel started")

    def _run_ws(self, app_id: str, app_secret: str, domain: str) -> None:
        """Construct and run the lark WS client in a thread with a fresh event loop.

        The lark-oapi SDK captures a module-level event loop at import time
        (``lark_oapi.ws.client.loop``).  When uvicorn uses uvloop, that
        captured loop is the *main* thread's uvloop — which is already
        running, so ``loop.run_until_complete()`` inside ``Client.start()``
        raises ``RuntimeError``.

        We work around this by creating a plain asyncio event loop for this
        thread and patching the SDK's module-level reference before calling
        ``start()``.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            import lark_oapi as lark
            import lark_oapi.ws.client as _ws_client_mod

            # Replace the SDK's module-level loop so Client.start() uses
            # this thread's (non-running) event loop instead of the main
            # thread's uvloop.
            _ws_client_mod.loop = loop

            event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message).register_p2_card_action_trigger(self._on_card_action).register_p2_application_bot_menu_v6(self._on_bot_menu).build()
            ws_client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
                domain=domain,
            )
            ws_client.start()
        except Exception:
            if self._running:
                logger.exception("Feishu WebSocket error")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        for task in list(self._running_card_tasks.values()):
            task.cancel()
        self._running_card_tasks.clear()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Feishu channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        if not self._api_client:
            logger.warning("[Feishu] send called but no api_client available")
            return

        logger.info(
            "[Feishu] sending reply: chat_id=%s, thread_ts=%s, text_len=%d",
            msg.chat_id,
            msg.thread_ts,
            len(msg.text),
        )

        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                await self._send_card_message(msg)
                return  # success
            except Exception as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    delay = 2**attempt  # 1s, 2s
                    logger.warning(
                        "[Feishu] send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("[Feishu] send failed after %d attempts: %s", _max_retries, last_exc)
        if last_exc is None:
            raise RuntimeError("Feishu send failed without an exception from any attempt")
        raise last_exc

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._api_client:
            return False

        # Check size limits (image: 10MB, file: 30MB)
        if attachment.is_image and attachment.size > 10 * 1024 * 1024:
            logger.warning("[Feishu] image too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False
        if not attachment.is_image and attachment.size > 30 * 1024 * 1024:
            logger.warning("[Feishu] file too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False

        try:
            if attachment.is_image:
                file_key = await self._upload_image(attachment.actual_path)
                msg_type = "image"
                content = json.dumps({"image_key": file_key})
            else:
                file_key = await self._upload_file(attachment.actual_path, attachment.filename)
                msg_type = "file"
                content = json.dumps({"file_key": file_key})

            if msg.thread_ts:
                request = self._ReplyMessageRequest.builder().message_id(msg.thread_ts).request_body(self._ReplyMessageRequestBody.builder().msg_type(msg_type).content(content).reply_in_thread(self._reply_in_thread_for(msg.chat_id)).build()).build()
                await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            else:
                request = self._CreateMessageRequest.builder().receive_id_type("chat_id").request_body(self._CreateMessageRequestBody.builder().receive_id(msg.chat_id).msg_type(msg_type).content(content).build()).build()
                await asyncio.to_thread(self._api_client.im.v1.message.create, request)

            logger.info("[Feishu] file sent: %s (type=%s)", attachment.filename, msg_type)
            return True
        except Exception:
            logger.exception("[Feishu] failed to upload/send file: %s", attachment.filename)
            return False

    async def _upload_image(self, path) -> str:
        """Upload an image to Feishu and return the image_key."""
        with open(str(path), "rb") as f:
            request = self._CreateImageRequest.builder().request_body(self._CreateImageRequestBody.builder().image_type("message").image(f).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.image.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu image upload failed: code={response.code}, msg={response.msg}")
        return response.data.image_key

    async def _upload_file(self, path, filename: str) -> str:
        """Upload a file to Feishu and return the file_key."""
        suffix = path.suffix.lower() if hasattr(path, "suffix") else ""
        if suffix in (".xls", ".xlsx", ".csv"):
            file_type = "xls"
        elif suffix in (".ppt", ".pptx"):
            file_type = "ppt"
        elif suffix == ".pdf":
            file_type = "pdf"
        elif suffix in (".doc", ".docx"):
            file_type = "doc"
        else:
            file_type = "stream"

        with open(str(path), "rb") as f:
            request = self._CreateFileRequest.builder().request_body(self._CreateFileRequestBody.builder().file_type(file_type).file_name(filename).file(f).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.file.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu file upload failed: code={response.code}, msg={response.msg}")
        return response.data.file_key

    async def receive_file(self, msg: InboundMessage, thread_id: str) -> InboundMessage:
        """Download a Feishu file into the thread uploads directory.

        Returns the sandbox virtual path when the image is persisted successfully.
        """
        if not msg.thread_ts:
            logger.warning("[Feishu] received file message without thread_ts, cannot associate with conversation: %s", msg)
            return msg
        files = msg.files
        if not files:
            logger.warning("[Feishu] received message with no files: %s", msg)
            return msg
        text = msg.text
        for file in files:
            if file.get("image_key"):
                virtual_path = await self._receive_single_file(msg.thread_ts, file["image_key"], "image", thread_id)
                text = text.replace("[image]", virtual_path, 1)
            elif file.get("file_key"):
                virtual_path = await self._receive_single_file(msg.thread_ts, file["file_key"], "file", thread_id)
                text = text.replace("[file]", virtual_path, 1)
        msg.text = text
        return msg

    async def _receive_single_file(self, message_id: str, file_key: str, type: Literal["image", "file"], thread_id: str) -> str:
        request = self._GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(type).build()

        def inner():
            return self._api_client.im.v1.message_resource.get(request)

        try:
            response = await asyncio.to_thread(inner)
        except Exception:
            logger.exception("[Feishu] resource get request failed for resource_key=%s type=%s", file_key, type)
            return f"Failed to obtain the [{type}]"

        if not response.success():
            logger.warning(
                "[Feishu] resource get failed: resource_key=%s, type=%s, code=%s, msg=%s, log_id=%s ",
                file_key,
                type,
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return f"Failed to obtain the [{type}]"

        image_stream = getattr(response, "file", None)
        if image_stream is None:
            logger.warning("[Feishu] resource get returned no file stream: resource_key=%s, type=%s", file_key, type)
            return f"Failed to obtain the [{type}]"

        try:
            content: bytes = await asyncio.to_thread(image_stream.read)
        except Exception:
            logger.exception("[Feishu] failed to read resource stream: resource_key=%s, type=%s", file_key, type)
            return f"Failed to obtain the [{type}]"

        if not content:
            logger.warning("[Feishu] empty resource content: resource_key=%s, type=%s", file_key, type)
            return f"Failed to obtain the [{type}]"

        paths = get_paths()
        user_id = get_effective_user_id()
        paths.ensure_thread_dirs(thread_id, user_id=user_id)
        uploads_dir = paths.sandbox_uploads_dir(thread_id, user_id=user_id).resolve()

        ext = "png" if type == "image" else "bin"
        raw_filename = getattr(response, "file_name", "") or f"feishu_{file_key[-12:]}.{ext}"

        # Sanitize filename: preserve extension, replace path chars in name part
        if "." in raw_filename:
            name_part, ext = raw_filename.rsplit(".", 1)
            name_part = re.sub(r"[./\\]", "_", name_part)
            filename = f"{name_part}.{ext}"
        else:
            filename = re.sub(r"[./\\]", "_", raw_filename)
        resolved_target = uploads_dir / filename

        def down_load():
            # use thread_lock to avoid filename conflicts when writing
            with self._thread_lock:
                resolved_target.write_bytes(content)

        try:
            await asyncio.to_thread(down_load)
        except Exception:
            logger.exception("[Feishu] failed to persist downloaded resource: %s, type=%s", resolved_target, type)
            return f"Failed to obtain the [{type}]"

        virtual_path = f"{VIRTUAL_PATH_PREFIX}/uploads/{resolved_target.name}"

        try:
            sandbox_provider = get_sandbox_provider()
            sandbox_id = sandbox_provider.acquire(thread_id)
            if sandbox_id != "local":
                sandbox = sandbox_provider.get(sandbox_id)
                if sandbox is None:
                    logger.warning("[Feishu] sandbox not found for thread_id=%s", thread_id)
                    return f"Failed to obtain the [{type}]"
                sandbox.update_file(virtual_path, content)
        except Exception:
            logger.exception("[Feishu] failed to sync resource into non-local sandbox: %s", virtual_path)
            return f"Failed to obtain the [{type}]"

        logger.info("[Feishu] downloaded resource mapped: file_key=%s -> %s", file_key, virtual_path)
        return virtual_path

    # -- message formatting ------------------------------------------------

    @staticmethod
    def _build_card_content(text: str) -> str:
        """Build a Feishu interactive card with markdown content.

        Feishu's interactive card format natively renders markdown, including
        headers, bold/italic, code blocks, lists, and links.
        """
        card = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [{"tag": "markdown", "content": text}],
        }
        return json.dumps(card)

    # todo / 子任务状态 -> 展示图标
    _TODO_STATUS_ICONS = {
        "completed": "✅",
        "in_progress": "🔄",
        "running": "🔄",
        "pending": "⬜",
        "failed": "❌",
        "cancelled": "🚫",
    }

    @classmethod
    def _render_progress(cls, metadata: dict[str, Any]) -> str:
        """把 manager 注入的 todos / pipeline_steps 渲染成 markdown 进度块（无进度返回空串）。"""
        lines: list[str] = []

        todos = metadata.get("todos")
        if isinstance(todos, list) and todos:
            lines.append("**任务进度**")
            for todo in todos:
                if not isinstance(todo, dict):
                    continue
                content = str(todo.get("content", "")).strip()
                if not content:
                    continue
                icon = cls._TODO_STATUS_ICONS.get(str(todo.get("status", "")), "▫️")
                lines.append(f"{icon} {content}")

        steps = metadata.get("pipeline_steps")
        if isinstance(steps, list):
            running = [s for s in steps if isinstance(s, dict) and s.get("status") == "running"]
            for step in running:
                desc = str(step.get("description", "")).strip()
                if desc:
                    lines.append(f"🔧 正在执行：{desc}")

        return "\n".join(lines)

    @classmethod
    def _compose_card_text(cls, msg: OutboundMessage) -> str:
        """组合进度块与回答正文，用于卡片渲染。"""
        progress = cls._render_progress(msg.metadata)
        text = msg.text or ""
        if progress and text:
            return f"{progress}\n\n---\n\n{text}"
        return progress or text

    # -- workdir 选择卡片 ----------------------------------------------------

    _REPO_CARD_MAX_DIRS = 20
    _REPO_CARD_BUTTONS_PER_ROW = 4

    @classmethod
    def _workdir_buttons(cls, paths: list[str], current: str | None, *, primary: bool) -> list[dict[str, Any]]:
        """把目录路径列表转成飞书卡片 action 行（每行最多 4 个按钮）。"""
        buttons = []
        for path in paths:
            name = path.rstrip("/").rsplit("/", 1)[-1]
            label = f"✓ {name}" if path == current else name
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary" if primary or path == current else "default",
                    "value": {"action": "set_workdir", "path": path},
                }
            )
        rows = []
        for i in range(0, len(buttons), cls._REPO_CARD_BUTTONS_PER_ROW):
            rows.append({"tag": "action", "actions": buttons[i : i + cls._REPO_CARD_BUTTONS_PER_ROW]})
        return rows

    @classmethod
    def _build_repo_card(cls, current: str | None, history: list[str], available: list[str]) -> dict[str, Any]:
        """构建工作目录选择卡片（dict 形式，发送与回调更新共用）。"""
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": f"**当前工作目录**：{current or '未设置'}"},
        ]

        recent = [h for h in history if h in available][:5]
        if recent:
            elements.append({"tag": "markdown", "content": "**最近使用**"})
            elements.extend(cls._workdir_buttons(recent, current, primary=True))

        rest = [p for p in available if p not in recent]
        if rest:
            truncated = rest[: cls._REPO_CARD_MAX_DIRS]
            elements.append({"tag": "markdown", "content": "**全部目录**"})
            elements.extend(cls._workdir_buttons(truncated, current, primary=False))
            if len(rest) > len(truncated):
                elements.append({"tag": "markdown", "content": f"（仅显示前 {len(truncated)} 个，更多请用 /repo <名称> 设置）"})
        elif not recent:
            elements.append({"tag": "markdown", "content": "项目目录为空。把代码放到宿主机 /work/projects/ 下，或在对话里让工程师 git clone 到 /mnt/projects/。"})

        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "清除设置"},
                        "type": "danger",
                        "value": {"action": "clear_workdir"},
                    }
                ],
            }
        )
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "新增项目：把代码放到宿主机 /work/projects/ 下；或直接对我说「把 <git地址> 克隆到 /mnt/projects」。",
                    }
                ],
            }
        )
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "选择工作目录"}, "template": "blue"},
            "elements": elements,
        }

    def _channel_store(self):
        return self.config.get("channel_store")

    def _repo_card_for_chat(self, chat_id: str) -> dict[str, Any]:
        """根据该会话的 store 状态构建工作目录卡片。"""
        from app.channels.workdir import list_project_workdirs

        store = self._channel_store()
        current = store.get_workdir(self.name, chat_id) if store else None
        history = store.get_workdir_history(self.name, chat_id) if store else []
        return self._build_repo_card(current, history, list_project_workdirs())

    async def _send_repo_card(self, message_id: str, chat_id: str) -> None:
        """回复 /repo：发送工作目录选择卡片。"""
        if not self._api_client:
            return
        try:
            content = json.dumps(self._repo_card_for_chat(chat_id))
            request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(self._reply_in_thread_for(chat_id)).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            logger.info("[Feishu] repo card sent: chat_id=%s", chat_id)
        except Exception:
            logger.exception("[Feishu] failed to send repo card: chat_id=%s", chat_id)

    async def _send_repo_card_create(self, *, chat_id: str | None, open_id: str | None) -> None:
        """以新消息方式发送 repo 卡片（机器人菜单触发，无源消息可回复）。"""
        if not self._api_client:
            return
        receive_id = chat_id or open_id
        if not receive_id:
            return
        receive_id_type = "chat_id" if chat_id else "open_id"
        try:
            # 无 chat_id 时按空会话渲染（无当前/历史）；按钮回调自带 open_chat_id，状态仍会写对
            content = json.dumps(self._repo_card_for_chat(chat_id or ""))
            request = self._CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(self._CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("interactive").content(content).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.create, request)
            logger.info("[Feishu] repo card created via %s=%s", receive_id_type, receive_id)
        except Exception:
            logger.exception("[Feishu] failed to create repo card via %s", receive_id_type)

    # -- agent（人设）选择卡片 ----------------------------------------------

    @classmethod
    def _agent_buttons(cls, personas: list[tuple[str, str, str]], current_name: str) -> list[dict[str, Any]]:
        """把人设列表转成飞书卡片 action 行（每行最多 4 个按钮）。"""
        buttons = []
        for name, label, _desc in personas:
            is_current = name == current_name
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"✓ {label}" if is_current else label},
                    "type": "primary" if is_current else "default",
                    "value": {"action": "set_agent", "name": name},
                }
            )
        rows = []
        for i in range(0, len(buttons), cls._REPO_CARD_BUTTONS_PER_ROW):
            rows.append({"tag": "action", "actions": buttons[i : i + cls._REPO_CARD_BUTTONS_PER_ROW]})
        return rows

    @classmethod
    def _build_agent_card(cls, current_name: str, personas: list[tuple[str, str, str]]) -> dict[str, Any]:
        """构建人设选择卡片（dict 形式，发送与回调更新共用）。"""
        current_label = next((label for name, label, _ in personas if name == current_name), current_name)
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": f"**当前人设**：{current_label}"},
        ]
        desc_lines = [f"• **{label}**（{name}）：{desc}" if desc else f"• **{label}**（{name}）" for name, label, desc in personas]
        if desc_lines:
            elements.append({"tag": "markdown", "content": "\n".join(desc_lines)})
        elements.extend(cls._agent_buttons(personas, current_name))
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "恢复默认（通用助手）"},
                        "type": "danger",
                        "value": {"action": "clear_agent"},
                    }
                ],
            }
        )
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "新增人设：在 .deer-flow/agents/<name>/ 放 config.yaml + SOUL.md，会自动出现在这里。",
                    }
                ],
            }
        )
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "选择助手 / 人设"}, "template": "blue"},
            "elements": elements,
        }

    def _agent_card_for_chat(self, chat_id: str) -> dict[str, Any]:
        """根据该会话的 store 状态构建人设卡片。

        未选择时按通用助手（lead_agent）渲染——与 config 默认 assistant_id 一致。
        """
        from app.channels.personas import DEFAULT_PERSONA, list_personas

        store = self._channel_store()
        stored = store.get_agent(self.name, chat_id) if (store and chat_id) else None
        return self._build_agent_card(stored or DEFAULT_PERSONA, list_personas())

    async def _send_agent_card(self, message_id: str, chat_id: str) -> None:
        """回复 /agent：发送人设选择卡片。"""
        if not self._api_client:
            return
        try:
            content = json.dumps(self._agent_card_for_chat(chat_id))
            request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(self._reply_in_thread_for(chat_id)).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            logger.info("[Feishu] agent card sent: chat_id=%s", chat_id)
        except Exception:
            logger.exception("[Feishu] failed to send agent card: chat_id=%s", chat_id)

    async def _send_agent_card_create(self, *, chat_id: str | None, open_id: str | None) -> None:
        """以新消息方式发送人设卡片（机器人菜单触发，无源消息可回复）。"""
        if not self._api_client:
            return
        receive_id = chat_id or open_id
        if not receive_id:
            return
        receive_id_type = "chat_id" if chat_id else "open_id"
        try:
            content = json.dumps(self._agent_card_for_chat(chat_id or ""))
            request = self._CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(self._CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("interactive").content(content).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.create, request)
            logger.info("[Feishu] agent card created via %s=%s", receive_id_type, receive_id)
        except Exception:
            logger.exception("[Feishu] failed to create agent card via %s", receive_id_type)

    # -- 记忆查看卡片 --------------------------------------------------------

    _MEMORY_SCOPE_LABELS = {
        "global": "当前用户全局",
        "default": "跨用户共享",
        "persona": "当前人设",
    }

    @classmethod
    def _build_memory_card(cls, active_scope: str | None = None, body: str | None = None) -> dict[str, Any]:
        """构建记忆查看卡片：三个范围按钮 +（点击后）对应记忆内容。

        active_scope/body 为空时即「选择卡」（仅按钮）；点击后带上当前范围与内容重渲染。
        """
        buttons = []
        for scope in MEMORY_SCOPES:
            label = cls._MEMORY_SCOPE_LABELS.get(scope, scope)
            is_active = scope == active_scope
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"✓ {label}" if is_active else label},
                    "type": "primary" if is_active else "default",
                    "value": {"action": "view_memory", "scope": scope},
                }
            )
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": "选择要查看的记忆范围："},
            {"tag": "action", "actions": buttons},
        ]
        if body is not None:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": body})
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "记忆按你个人隔离，建议在单聊中查看。"}],
            }
        )
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "查看记忆"}, "template": "blue"},
            "elements": elements,
        }

    async def _send_memory_card(self, message_id: str, chat_id: str) -> None:
        """回复 /memory：发送记忆范围选择卡片。"""
        if not self._api_client:
            return
        try:
            content = json.dumps(self._build_memory_card())
            request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(self._reply_in_thread_for(chat_id)).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            logger.info("[Feishu] memory card sent: chat_id=%s", chat_id)
        except Exception:
            logger.exception("[Feishu] failed to send memory card: chat_id=%s", chat_id)

    async def _send_memory_card_create(self, *, chat_id: str | None, open_id: str | None) -> None:
        """以新消息方式发送记忆卡片（机器人菜单触发，无源消息可回复）。"""
        if not self._api_client:
            return
        receive_id = chat_id or open_id
        if not receive_id:
            return
        receive_id_type = "chat_id" if chat_id else "open_id"
        try:
            content = json.dumps(self._build_memory_card())
            request = self._CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(self._CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("interactive").content(content).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.create, request)
            logger.info("[Feishu] memory card created via %s=%s", receive_id_type, receive_id)
        except Exception:
            logger.exception("[Feishu] failed to create memory card via %s", receive_id_type)

    # -- 模型选择卡片 --------------------------------------------------------

    @staticmethod
    def _list_models() -> list[tuple[str, str | None]]:
        """返回 (name, display_name) 列表，首个为默认模型；失败返回空列表（不阻塞渠道）。"""
        try:
            from deerflow.config.app_config import get_app_config

            return [(m.name, getattr(m, "display_name", None)) for m in get_app_config().models]
        except Exception:
            logger.debug("[Feishu] list models failed", exc_info=True)
            return []

    @classmethod
    def _build_model_card(cls, current_name: str | None, models: list[tuple[str, str | None]]) -> dict[str, Any]:
        """构建模型选择卡片（dict 形式，发送与回调更新共用）。"""

        def _label(name: str, display: str | None) -> str:
            return display if display and display != name else name

        current_label = next((_label(name, disp) for name, disp in models if name == current_name), current_name or "（未配置）")
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": f"**当前模型**：{current_label}"},
        ]
        buttons = []
        for name, disp in models:
            is_current = name == current_name
            label = _label(name, disp)
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"✓ {label}" if is_current else label},
                    "type": "primary" if is_current else "default",
                    "value": {"action": "set_model", "name": name},
                }
            )
        for i in range(0, len(buttons), cls._REPO_CARD_BUTTONS_PER_ROW):
            elements.append({"tag": "action", "actions": buttons[i : i + cls._REPO_CARD_BUTTONS_PER_ROW]})
        if not buttons:
            elements.append({"tag": "markdown", "content": "（未配置任何模型）"})
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "恢复默认模型"},
                        "type": "danger",
                        "value": {"action": "clear_model"},
                    }
                ],
            }
        )
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "切换仅对本会话生效；新对话继续使用所选模型。"}],
            }
        )
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "选择模型"}, "template": "blue"},
            "elements": elements,
        }

    def _model_card_for_chat(self, chat_id: str) -> dict[str, Any]:
        """根据该会话的 store 状态构建模型卡片（未选择时按默认模型渲染）。"""
        models = self._list_models()
        store = self._channel_store()
        stored = store.get_model(self.name, chat_id) if (store and chat_id) else None
        default_name = models[0][0] if models else None
        return self._build_model_card(stored or default_name, models)

    async def _send_model_card(self, message_id: str, chat_id: str) -> None:
        """回复 /models：发送模型选择卡片。"""
        if not self._api_client:
            return
        try:
            content = json.dumps(self._model_card_for_chat(chat_id))
            request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(self._reply_in_thread_for(chat_id)).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            logger.info("[Feishu] model card sent: chat_id=%s", chat_id)
        except Exception:
            logger.exception("[Feishu] failed to send model card: chat_id=%s", chat_id)

    async def _send_model_card_create(self, *, chat_id: str | None, open_id: str | None) -> None:
        """以新消息方式发送模型卡片（机器人菜单触发，无源消息可回复）。"""
        if not self._api_client:
            return
        receive_id = chat_id or open_id
        if not receive_id:
            return
        receive_id_type = "chat_id" if chat_id else "open_id"
        try:
            content = json.dumps(self._model_card_for_chat(chat_id or ""))
            request = self._CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(self._CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("interactive").content(content).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.create, request)
            logger.info("[Feishu] model card created via %s=%s", receive_id_type, receive_id)
        except Exception:
            logger.exception("[Feishu] failed to create model card via %s", receive_id_type)

    async def _send_text_via_open_id(self, open_id: str, text: str) -> None:
        """通过 open_id 给用户单聊发一张文本卡片（菜单兜底提示用）。"""
        if not self._api_client or not open_id:
            return
        try:
            content = self._build_card_content(text)
            request = self._CreateMessageRequest.builder().receive_id_type("open_id").request_body(self._CreateMessageRequestBody.builder().receive_id(open_id).msg_type("interactive").content(content).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.create, request)
        except Exception:
            logger.exception("[Feishu] failed to send text via open_id")

    # 固定菜单项 event_key -> 命令。另有约定前缀 AGENT_<NAME>（不在此表里），
    # 由 _on_bot_menu 动态解析为 /agent <name>，实现菜单里直接选择人设：
    #   AGENT_DEFAULT -> /agent default（通用助手）；AGENT_SOFTWARE_TEAM -> /agent software-team。
    # 新增人设无需改代码，只在飞书后台加一个 event_key=AGENT_<名> 的菜单项即可。
    _MENU_COMMAND_MAP = {
        "REPO": "/repo",
        "AGENT": "/agent",
        "STATUS": "/status",
        "NEW": "/new",
        "HELP": "/help",
        "MODELS": "/models",
        # 记忆按钮拆分为三种视图；保留旧 MEMORY 以向后兼容（= 当前用户全局）。
        "MEMORY": "/memory",
        "MEMORY_GLOBAL": "/memory global",
        "MEMORY_DEFAULT": "/memory default",
        "MEMORY_PERSONA": "/memory persona",
    }

    def _on_bot_menu(self, data) -> None:
        """机器人自定义菜单事件（lark 线程内）：把菜单项路由为对应命令。"""
        try:
            event = getattr(data, "event", None)
            event_key = str(getattr(event, "event_key", "") or "").strip().upper()
            operator = getattr(event, "operator", None)
            operator_id = getattr(operator, "operator_id", None)
            open_id = self._non_empty_str(getattr(operator_id, "open_id", None))
            logger.info("[Feishu] bot menu event: key=%s open_id=%s", event_key, open_id)

            # 约定前缀 AGENT_<NAME>：菜单里直接选人设，路由为 /agent <name>。
            # normalize_persona 会把 _ 转成 -，故 AGENT_SOFTWARE_TEAM -> software-team。
            if event_key.startswith("AGENT_"):
                command = "/agent " + event_key[len("AGENT_") :].lower()
            else:
                command = self._MENU_COMMAND_MAP.get(event_key)
            if not command:
                logger.warning("[Feishu] unknown bot menu event_key: %s", event_key)
                return
            if not (self._main_loop and self._main_loop.is_running()):
                logger.warning("[Feishu] main loop not running, dropping bot menu event")
                return

            store = self._channel_store()
            chat_id = store.get_user_chat(self.name, open_id) if (store and open_id) else None

            if command == "/repo":
                fut = asyncio.run_coroutine_threadsafe(self._send_repo_card_create(chat_id=chat_id, open_id=open_id), self._main_loop)
                fut.add_done_callback(lambda f: self._log_future_error(f, "menu_repo_card", event_key))
                return

            if command == "/agent":
                fut = asyncio.run_coroutine_threadsafe(self._send_agent_card_create(chat_id=chat_id, open_id=open_id), self._main_loop)
                fut.add_done_callback(lambda f: self._log_future_error(f, "menu_agent_card", event_key))
                return

            if command == "/memory":
                fut = asyncio.run_coroutine_threadsafe(self._send_memory_card_create(chat_id=chat_id, open_id=open_id), self._main_loop)
                fut.add_done_callback(lambda f: self._log_future_error(f, "menu_memory_card", event_key))
                return

            if command == "/models":
                fut = asyncio.run_coroutine_threadsafe(self._send_model_card_create(chat_id=chat_id, open_id=open_id), self._main_loop)
                fut.add_done_callback(lambda f: self._log_future_error(f, "menu_model_card", event_key))
                return

            if not chat_id:
                # 还没和机器人聊过，无法定位会话；提示后返回
                if open_id:
                    fut = asyncio.run_coroutine_threadsafe(self._send_text_via_open_id(open_id, "请先给我发送一条消息，然后再使用菜单。"), self._main_loop)
                    fut.add_done_callback(lambda f: self._log_future_error(f, "menu_hint", event_key))
                return

            inbound = self._make_inbound(
                chat_id=chat_id,
                user_id=open_id or "",
                text=command,
                msg_type=InboundMessageType.COMMAND,
                metadata={"user_id": open_id, "source": "bot_menu"},
            )
            fut = asyncio.run_coroutine_threadsafe(self.bus.publish_inbound(inbound), self._main_loop)
            fut.add_done_callback(lambda f: self._log_future_error(f, "menu_command", command))
        except Exception:
            logger.exception("[Feishu] error handling bot menu event")

    _WORKDIR_ACTIONS = ("set_workdir", "clear_workdir")
    _AGENT_ACTIONS = ("set_agent", "clear_agent")
    _MODEL_ACTIONS = ("set_model", "clear_model")
    _MEMORY_ACTIONS = ("view_memory",)

    @staticmethod
    def _card_action_open_id(event) -> str | None:
        """从卡片回调事件中取点击者 open_id（兼容不同 lark 版本字段）。"""
        operator = getattr(event, "operator", None)
        if operator is None:
            return None
        open_id = getattr(operator, "open_id", None)
        if isinstance(open_id, str) and open_id.strip():
            return open_id
        operator_id = getattr(operator, "operator_id", None)
        nested = getattr(operator_id, "open_id", None)
        return nested if isinstance(nested, str) and nested.strip() else None

    def _on_card_action(self, data):
        """卡片按钮回调（lark 线程内同步执行）：工作目录/人设/模型设置，或记忆查看，并更新卡片。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        try:
            event = data.event
            value = (event.action.value if event and event.action else None) or {}
            action = value.get("action")
            chat_id = event.context.open_chat_id if event and event.context else None
            store = self._channel_store()

            known_actions = (*self._WORKDIR_ACTIONS, *self._AGENT_ACTIONS, *self._MODEL_ACTIONS, *self._MEMORY_ACTIONS)
            if not chat_id or store is None or action not in known_actions:
                return P2CardActionTriggerResponse({})

            if action in self._MEMORY_ACTIONS:
                # 记忆查看不改状态：按点击者身份渲染对应范围内容并原地刷新卡片
                return self._handle_view_memory_action(event, store, chat_id, value)

            if action in self._WORKDIR_ACTIONS:
                toast = self._apply_workdir_action(store, chat_id, action, value)
                if toast is None:
                    return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无效的目录"}})
                card = self._repo_card_for_chat(chat_id)
            elif action in self._MODEL_ACTIONS:
                toast = self._apply_model_action(store, chat_id, action, value)
                if toast is None:
                    return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无效的模型"}})
                card = self._model_card_for_chat(chat_id)
            else:
                toast = self._apply_agent_action(store, chat_id, action, value)
                if toast is None:
                    return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无效的人设"}})
                card = self._agent_card_for_chat(chat_id)

            logger.info("[Feishu] card action %s handled: chat_id=%s", action, chat_id)
            return P2CardActionTriggerResponse(
                {
                    "toast": toast,
                    "card": {"type": "raw", "data": card},
                }
            )
        except Exception:
            logger.exception("[Feishu] error handling card action")
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "处理失败，请重试"}})

    def _handle_view_memory_action(self, event, store, chat_id: str, value: dict):
        """处理记忆卡片的 view_memory：按点击者渲染该范围内容并更新卡片。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        scope = value.get("scope")
        if scope not in MEMORY_SCOPES:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无效的记忆范围"}})

        open_id = self._card_action_open_id(event)
        user_id = make_safe_user_id(open_id) if open_id else DEFAULT_USER_ID
        persona = store.get_agent(self.name, chat_id)
        body = render_memory(scope, user_id=user_id, persona=persona)
        card = self._build_memory_card(active_scope=scope, body=body)

        logger.info("[Feishu] card action view_memory(%s) handled: chat_id=%s", scope, chat_id)
        return P2CardActionTriggerResponse(
            {
                "toast": {"type": "info", "content": self._MEMORY_SCOPE_LABELS.get(scope, scope)},
                "card": {"type": "raw", "data": card},
            }
        )

    def _apply_model_action(self, store, chat_id: str, action: str, value: dict) -> dict | None:
        """执行模型卡片按钮动作，返回 toast；无效返回 None。"""
        models = self._list_models()
        default_name = models[0][0] if models else None

        if action == "clear_model":
            store.clear_model(self.name, chat_id)
            return {"type": "info", "content": f"已恢复默认模型：{default_name or '默认'}"}

        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        if name not in {model_name for model_name, _ in models}:
            return None
        # 选默认模型即清除覆盖（回落到 config 默认）
        if default_name is not None and name == default_name:
            store.clear_model(self.name, chat_id)
        else:
            store.set_model(self.name, chat_id, name)
        return {"type": "success", "content": f"模型已切换：{name}"}

    def _apply_workdir_action(self, store, chat_id: str, action: str, value: dict) -> dict | None:
        """执行工作目录卡片按钮动作，返回 toast；无效返回 None。"""
        if action == "set_workdir":
            path = value.get("path")
            if not isinstance(path, str) or not path.strip():
                return None
            store.set_workdir(self.name, chat_id, path)
            return {"type": "success", "content": f"工作目录已切换：{path.rsplit('/', 1)[-1]}"}
        store.clear_workdir(self.name, chat_id)
        return {"type": "info", "content": "已清除工作目录设置"}

    def _apply_agent_action(self, store, chat_id: str, action: str, value: dict) -> dict | None:
        """执行人设卡片按钮动作，返回 toast；无效返回 None。"""
        from app.channels.personas import DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL, normalize_persona

        if action == "clear_agent":
            store.clear_agent(self.name, chat_id)
            return {"type": "info", "content": f"已恢复默认人设：{DEFAULT_PERSONA_LABEL}"}

        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        persona = normalize_persona(name)
        if persona is None:
            return None
        # 选默认人设时清除覆盖即可（回落到 config 默认）
        if persona == DEFAULT_PERSONA:
            store.clear_agent(self.name, chat_id)
            return {"type": "success", "content": f"人设已切换：{DEFAULT_PERSONA_LABEL}"}
        store.set_agent(self.name, chat_id, persona)
        return {"type": "success", "content": f"人设已切换：{persona}"}

    # -- reaction helpers --------------------------------------------------

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """Add an emoji reaction to a message."""
        if not self._api_client or not self._CreateMessageReactionRequest:
            return
        try:
            request = self._CreateMessageReactionRequest.builder().message_id(message_id).request_body(self._CreateMessageReactionRequestBody.builder().reaction_type(self._Emoji.builder().emoji_type(emoji_type).build()).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message_reaction.create, request)
            logger.info("[Feishu] reaction '%s' added to message %s", emoji_type, message_id)
        except Exception:
            logger.exception("[Feishu] failed to add reaction '%s' to message %s", emoji_type, message_id)

    async def _reply_card(self, message_id: str, text: str, *, in_thread: bool = True) -> str | None:
        """Reply with an interactive card and return the created card message ID."""
        if not self._api_client:
            return None

        content = self._build_card_content(text)
        request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(in_thread).build()).build()
        response = await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
        response_data = getattr(response, "data", None)
        return getattr(response_data, "message_id", None)

    async def _create_card(self, chat_id: str, text: str) -> None:
        """Create a new card message in the target chat."""
        if not self._api_client:
            return

        content = self._build_card_content(text)
        request = self._CreateMessageRequest.builder().receive_id_type("chat_id").request_body(self._CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(content).build()).build()
        await asyncio.to_thread(self._api_client.im.v1.message.create, request)

    async def _update_card(self, message_id: str, text: str) -> None:
        """Patch an existing card message in place."""
        if not self._api_client or not self._PatchMessageRequest:
            return

        content = self._build_card_content(text)
        request = self._PatchMessageRequest.builder().message_id(message_id).request_body(self._PatchMessageRequestBody.builder().content(content).build()).build()
        await asyncio.to_thread(self._api_client.im.v1.message.patch, request)

    def _track_background_task(self, task: asyncio.Task, *, name: str, msg_id: str) -> None:
        """Keep a strong reference to fire-and-forget tasks and surface errors."""
        self._background_tasks.add(task)
        task.add_done_callback(lambda done_task, task_name=name, mid=msg_id: self._finalize_background_task(done_task, task_name, mid))

    def _finalize_background_task(self, task: asyncio.Task, name: str, msg_id: str) -> None:
        self._background_tasks.discard(task)
        self._log_task_error(task, name, msg_id)

    async def _create_running_card(self, source_message_id: str, text: str, *, in_thread: bool = True) -> str | None:
        """Create the running card and cache its message ID when available."""
        running_card_id = await self._reply_card(source_message_id, text, in_thread=in_thread)
        if running_card_id:
            self._running_card_ids[source_message_id] = running_card_id
            logger.info("[Feishu] running card created: source=%s card=%s", source_message_id, running_card_id)
        else:
            logger.warning("[Feishu] running card creation returned no message_id for source=%s, subsequent updates will fall back to new replies", source_message_id)
        return running_card_id

    def _ensure_running_card_started(self, source_message_id: str, text: str = "Working on it...", *, in_thread: bool = True) -> asyncio.Task | None:
        """Start running-card creation once per source message."""
        running_card_id = self._running_card_ids.get(source_message_id)
        if running_card_id:
            return None

        running_card_task = self._running_card_tasks.get(source_message_id)
        if running_card_task:
            return running_card_task

        running_card_task = asyncio.create_task(self._create_running_card(source_message_id, text, in_thread=in_thread))
        self._running_card_tasks[source_message_id] = running_card_task
        running_card_task.add_done_callback(lambda done_task, mid=source_message_id: self._finalize_running_card_task(mid, done_task))
        return running_card_task

    def _finalize_running_card_task(self, source_message_id: str, task: asyncio.Task) -> None:
        if self._running_card_tasks.get(source_message_id) is task:
            self._running_card_tasks.pop(source_message_id, None)
        self._log_task_error(task, "create_running_card", source_message_id)

    async def _ensure_running_card(self, source_message_id: str, text: str = "Working on it...", *, in_thread: bool = True) -> str | None:
        """Ensure the running card exists and track its message ID."""
        running_card_id = self._running_card_ids.get(source_message_id)
        if running_card_id:
            return running_card_id

        running_card_task = self._ensure_running_card_started(source_message_id, text, in_thread=in_thread)
        if running_card_task is None:
            return self._running_card_ids.get(source_message_id)
        return await running_card_task

    async def _send_running_reply(self, message_id: str) -> None:
        """Reply to a message in-thread with a running card."""
        try:
            await self._ensure_running_card(message_id)
        except Exception:
            logger.exception("[Feishu] failed to send running reply for message %s", message_id)

    async def _send_card_message(self, msg: OutboundMessage) -> None:
        """Send or update the Feishu card tied to the current request."""
        in_thread = self._reply_in_thread_for(msg.chat_id)
        # 在正文上方拼接流水线进度块（plan mode todos + 子任务步骤）
        card_text = self._compose_card_text(msg)
        source_message_id = msg.thread_ts
        if source_message_id:
            running_card_id = self._running_card_ids.get(source_message_id)
            awaited_running_card_task = False

            if not running_card_id:
                running_card_task = self._running_card_tasks.get(source_message_id)
                if running_card_task:
                    awaited_running_card_task = True
                    running_card_id = await running_card_task

            if running_card_id:
                try:
                    await self._update_card(running_card_id, card_text)
                except Exception:
                    if not msg.is_final:
                        raise
                    logger.exception(
                        "[Feishu] failed to patch running card %s, falling back to final reply",
                        running_card_id,
                    )
                    fallback_card_id = await self._reply_card(source_message_id, card_text, in_thread=in_thread)
                    self._remember_thread_mapping(msg, source_message_id, fallback_card_id)
                    self._remember_pending_clarification(msg, fallback_card_id)
                else:
                    self._remember_thread_mapping(msg, source_message_id, running_card_id)
                    self._remember_pending_clarification(msg, running_card_id)
                    logger.info("[Feishu] running card updated: source=%s card=%s", source_message_id, running_card_id)
            elif msg.is_final:
                final_card_id = await self._reply_card(source_message_id, card_text, in_thread=in_thread)
                self._remember_thread_mapping(msg, source_message_id, final_card_id)
                self._remember_pending_clarification(msg, final_card_id)
            elif awaited_running_card_task:
                logger.warning(
                    "[Feishu] running card task finished without message_id for source=%s, skipping duplicate non-final creation",
                    source_message_id,
                )
            else:
                created_card_id = await self._ensure_running_card(source_message_id, card_text, in_thread=in_thread)
                self._remember_thread_mapping(msg, source_message_id, created_card_id)

            if msg.is_final:
                self._running_card_ids.pop(source_message_id, None)
                await self._add_reaction(source_message_id, "DONE")
            return

        await self._create_card(msg.chat_id, card_text)

    # -- internal ----------------------------------------------------------

    def _remember_thread_mapping(self, msg: OutboundMessage, *topic_ids: str | None) -> None:
        store = self.config.get("channel_store")
        if store is None or not msg.thread_id:
            return

        # p2p 单聊扁平化：会话级映射已由 _create_thread 按 channel:chat_id 写好，
        # 无需逐条消息记录 topic 映射，避免 store.json 随消息无限增长。
        if self._flatten_p2p and msg.metadata.get("chat_type") == "p2p":
            return

        metadata_topic_ids = [
            msg.metadata.get("message_id"),
            msg.metadata.get("root_id"),
            msg.metadata.get("parent_id"),
            msg.metadata.get("thread_id"),
            msg.metadata.get("topic_id"),
        ]
        user_id = ""
        raw_user_id = msg.metadata.get("user_id")
        if isinstance(raw_user_id, str):
            user_id = raw_user_id

        seen: set[str] = set()
        for topic_id in [*topic_ids, *metadata_topic_ids]:
            topic_id = self._non_empty_str(topic_id)
            if not topic_id or topic_id in seen:
                continue
            seen.add(topic_id)
            try:
                store.set_thread_id(
                    self.name,
                    msg.chat_id,
                    msg.thread_id,
                    topic_id=topic_id,
                    user_id=user_id,
                )
            except Exception:
                logger.exception("[Feishu] failed to remember thread mapping for topic_id=%s", topic_id)

    def _remember_pending_clarification(self, msg: OutboundMessage, card_message_id: str | None) -> None:
        if not msg.is_final or msg.metadata.get(PENDING_CLARIFICATION_METADATA_KEY) is not True:
            return

        user_id = self._non_empty_str(msg.metadata.get("user_id"))
        topic_id = self._non_empty_str(msg.metadata.get("topic_id"))
        source_message_id = self._non_empty_str(msg.thread_ts) or self._non_empty_str(msg.metadata.get("message_id"))
        if not (user_id and topic_id and msg.thread_id and source_message_id and card_message_id):
            return

        key = self._pending_key(msg.chat_id, user_id)
        pending = {
            "thread_id": msg.thread_id,
            "topic_id": topic_id,
            "source_message_id": source_message_id,
            "card_message_id": card_message_id,
            "created_at": time.time(),
        }
        with self._thread_lock:
            # Plain-message clarification continuity is a short-lived in-memory
            # hint; explicit Feishu replies are still covered by persisted
            # message-id mappings.
            self._pending_clarifications.setdefault(key, []).append(pending)
        logger.info(
            "[Feishu] pending clarification remembered: chat_id=%s user_id=%s topic_id=%s thread_id=%s",
            msg.chat_id,
            user_id,
            topic_id,
            msg.thread_id,
        )

    def _consume_pending_clarification(self, chat_id: str, user_id: str) -> dict[str, Any] | None:
        key = self._pending_key(chat_id, user_id)
        with self._thread_lock:
            pending_items = self._pending_clarifications.get(key)
            if not pending_items:
                return None

            now = time.time()
            while pending_items:
                pending = pending_items.pop(0)
                created_at = pending.get("created_at")
                if isinstance(created_at, (int, float)) and now - created_at <= PENDING_CLARIFICATION_TTL_SECONDS:
                    if pending_items:
                        self._pending_clarifications[key] = pending_items
                    else:
                        self._pending_clarifications.pop(key, None)
                    return pending
                logger.info("[Feishu] pending clarification expired: chat_id=%s user_id=%s", chat_id, user_id)

            self._pending_clarifications.pop(key, None)
            return None

    def _ensure_pending_thread_mapping(self, chat_id: str, user_id: str, pending: dict[str, Any]) -> None:
        store = self.config.get("channel_store")
        topic_id = self._non_empty_str(pending.get("topic_id"))
        thread_id = self._non_empty_str(pending.get("thread_id"))
        if store is None or not topic_id or not thread_id:
            return
        try:
            store.set_thread_id(self.name, chat_id, thread_id, topic_id=topic_id, user_id=user_id)
        except Exception:
            logger.exception("[Feishu] failed to restore pending clarification mapping for topic_id=%s", topic_id)

    def _resolve_topic_id(
        self,
        chat_id: str,
        msg_id: str,
        *,
        root_id: str | None,
        parent_id: str | None,
        thread_id: str | None,
    ) -> tuple[str, bool]:
        store = self.config.get("channel_store")
        candidates = [root_id, parent_id, thread_id]

        if store is not None:
            for candidate in candidates:
                candidate = self._non_empty_str(candidate)
                if not candidate:
                    continue
                try:
                    if store.get_thread_id(self.name, chat_id, topic_id=candidate):
                        return candidate, True
                except Exception:
                    logger.exception("[Feishu] failed to resolve stored topic mapping for topic_id=%s", candidate)

        return root_id or msg_id, False

    @staticmethod
    def _log_future_error(fut, name: str, msg_id: str) -> None:
        """Callback for run_coroutine_threadsafe futures to surface errors."""
        try:
            exc = fut.exception()
            if exc:
                logger.error("[Feishu] %s failed for msg_id=%s: %s", name, msg_id, exc)
        except Exception:
            pass

    @staticmethod
    def _log_task_error(task: asyncio.Task, name: str, msg_id: str) -> None:
        """Callback for background asyncio tasks to surface errors."""
        try:
            exc = task.exception()
            if exc:
                logger.error("[Feishu] %s failed for msg_id=%s: %s", name, msg_id, exc)
        except asyncio.CancelledError:
            logger.info("[Feishu] %s cancelled for msg_id=%s", name, msg_id)
        except Exception:
            pass

    async def _prepare_inbound(self, msg_id: str, inbound) -> None:
        """Kick off Feishu side effects without delaying inbound dispatch."""
        reaction_task = asyncio.create_task(self._add_reaction(msg_id, "OK"))
        self._track_background_task(reaction_task, name="add_reaction", msg_id=msg_id)
        self._ensure_running_card_started(msg_id, in_thread=self._reply_in_thread_for(inbound.chat_id))
        await self.bus.publish_inbound(inbound)

    def _on_message(self, event) -> None:
        """Called by lark-oapi when a message is received (runs in lark thread)."""
        try:
            logger.info("[Feishu] raw event received: type=%s", type(event).__name__)
            message = event.event.message
            chat_id = message.chat_id
            msg_id = message.message_id
            sender_id = event.event.sender.sender_id.open_id

            # 记录 chat_type，供回复时决定是否分线（p2p 单聊扁平化）。
            chat_type = self._non_empty_str(getattr(message, "chat_type", None)) or ""
            if chat_id and chat_type:
                self._chat_types[chat_id] = chat_type

            # 记录 open_id → 单聊 chat_id 映射：机器人自定义菜单事件只带 open_id，
            # 需要据此找回会话来回复。
            if chat_type == "p2p":
                store = self._channel_store()
                if store and sender_id:
                    store.set_user_chat(self.name, sender_id, chat_id)

            # root_id is set when the message is a reply within a Feishu thread.
            # Use it as topic_id so all replies share the same DeerFlow thread.
            root_id = self._non_empty_str(getattr(message, "root_id", None))
            parent_id = self._non_empty_str(getattr(message, "parent_id", None))
            feishu_thread_id = self._non_empty_str(getattr(message, "thread_id", None))

            # Parse message content
            content = json.loads(message.content)

            # files_list store the any-file-key in feishu messages, which can be used to download the file content later
            # In Feishu channel, image_keys are independent of file_keys.
            # The file_key includes files, videos, and audio, but does not include stickers.
            files_list = []

            if "text" in content:
                # Handle plain text messages
                text = content["text"]
            elif "file_key" in content:
                file_key = content.get("file_key")
                if isinstance(file_key, str) and file_key:
                    files_list.append({"file_key": file_key})
                    text = "[file]"
                else:
                    text = ""
            elif "image_key" in content:
                image_key = content.get("image_key")
                if isinstance(image_key, str) and image_key:
                    files_list.append({"image_key": image_key})
                    text = "[image]"
                else:
                    text = ""
            elif "content" in content and isinstance(content["content"], list):
                # Handle rich-text messages with a top-level "content" list (e.g., topic groups/posts)
                text_paragraphs: list[str] = []
                for paragraph in content["content"]:
                    if isinstance(paragraph, list):
                        paragraph_text_parts: list[str] = []
                        for element in paragraph:
                            if isinstance(element, dict):
                                # Include both normal text and @ mentions
                                if element.get("tag") in ("text", "at"):
                                    text_value = element.get("text", "")
                                    if text_value:
                                        paragraph_text_parts.append(text_value)
                                elif element.get("tag") == "img":
                                    image_key = element.get("image_key")
                                    if isinstance(image_key, str) and image_key:
                                        files_list.append({"image_key": image_key})
                                        paragraph_text_parts.append("[image]")
                                elif element.get("tag") in ("file", "media"):
                                    file_key = element.get("file_key")
                                    if isinstance(file_key, str) and file_key:
                                        files_list.append({"file_key": file_key})
                                        paragraph_text_parts.append("[file]")
                        if paragraph_text_parts:
                            # Join text segments within a paragraph with spaces to avoid "helloworld"
                            text_paragraphs.append(" ".join(paragraph_text_parts))

                # Join paragraphs with blank lines to preserve paragraph boundaries
                text = "\n\n".join(text_paragraphs)
            else:
                text = ""
            text = text.strip()

            logger.info(
                "[Feishu] parsed message: chat_id=%s, msg_id=%s, root_id=%s, parent_id=%s, thread_id=%s, sender=%s, text=%r",
                chat_id,
                msg_id,
                root_id,
                parent_id,
                feishu_thread_id,
                sender_id,
                text[:100] if text else "",
            )

            if not (text or files_list):
                logger.info("[Feishu] empty text, ignoring message")
                return

            # 裸 /repo 由飞书渠道直接回复交互式卡片（不投递到 bus）；
            # 带参数形式（/repo <名称>、/repo clear）仍走通用命令处理。
            if text.lower() == "/repo":
                if self._main_loop and self._main_loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._send_repo_card(msg_id, chat_id), self._main_loop)
                    fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "send_repo_card", mid))
                return

            if text.lower() == "/agent":
                if self._main_loop and self._main_loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._send_agent_card(msg_id, chat_id), self._main_loop)
                    fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "send_agent_card", mid))
                return

            # 裸 /memory、/models 同样回复交互式卡片；带参数（/memory global、/model x）仍走文本命令。
            if text.lower() == "/memory":
                if self._main_loop and self._main_loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._send_memory_card(msg_id, chat_id), self._main_loop)
                    fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "send_memory_card", mid))
                return

            if text.lower() == "/models":
                if self._main_loop and self._main_loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._send_model_card(msg_id, chat_id), self._main_loop)
                    fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "send_model_card", mid))
                return

            # Only treat known slash commands as commands; absolute paths and
            # other slash-prefixed text should be handled as normal chat.
            if _is_feishu_command(text):
                msg_type = InboundMessageType.COMMAND
            else:
                msg_type = InboundMessageType.CHAT

            # Prefer any platform message id that already maps to a DeerFlow
            # thread. This keeps replies to bot clarification cards in the
            # original conversation even when Feishu reports the card as root.
            topic_id, resolved_from_stored_mapping = self._resolve_topic_id(
                chat_id,
                msg_id,
                root_id=root_id,
                parent_id=parent_id,
                thread_id=feishu_thread_id,
            )
            resolved_from_pending = False
            if msg_type == InboundMessageType.CHAT and not resolved_from_stored_mapping:
                pending = self._consume_pending_clarification(chat_id, sender_id)
                pending_topic_id = self._non_empty_str(pending.get("topic_id")) if pending else None
                if pending_topic_id:
                    topic_id = pending_topic_id
                    self._ensure_pending_thread_mapping(chat_id, sender_id, pending)
                    resolved_from_pending = True

            # p2p 单聊扁平化：强制会话级线程（topic_id=None），覆盖话题/pending 解析结果，
            # 使同一聊天的所有消息归到一条连续 DeerFlow 线程。
            if self._flatten_p2p and chat_type == "p2p":
                topic_id = None

            inbound = self._make_inbound(
                chat_id=chat_id,
                user_id=sender_id,
                text=text,
                msg_type=msg_type,
                thread_ts=msg_id,
                files=files_list,
                metadata={
                    "message_id": msg_id,
                    "root_id": root_id,
                    "parent_id": parent_id,
                    "thread_id": feishu_thread_id,
                    "topic_id": topic_id,
                    "chat_type": chat_type,
                    "user_id": sender_id,
                    RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY: resolved_from_pending,
                },
            )
            inbound.topic_id = topic_id

            # Schedule on the async event loop
            if self._main_loop and self._main_loop.is_running():
                logger.info("[Feishu] publishing inbound message to bus (type=%s, msg_id=%s)", msg_type.value, msg_id)
                fut = asyncio.run_coroutine_threadsafe(self._prepare_inbound(msg_id, inbound), self._main_loop)
                fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "prepare_inbound", mid))
            else:
                logger.warning("[Feishu] main loop not running, cannot publish inbound message")
        except Exception:
            logger.exception("[Feishu] error processing message")
