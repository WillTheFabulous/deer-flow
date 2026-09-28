"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via Gateway."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from langgraph_sdk.errors import ConflictError

from app.channels.commands import KNOWN_CHANNEL_COMMANDS
from app.channels.memory_view import format_memory, render_memory
from app.channels.message_bus import (
    NOTIFICATION_METADATA_KEY,
    PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from app.channels.store import ChannelStore
from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token
from app.gateway.internal_auth import create_internal_auth_headers
from deerflow.config.paths import make_safe_user_id
from deerflow.runtime.user_context import DEFAULT_USER_ID, get_effective_user_id

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:8001/api"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"
CUSTOM_AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 100}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35
THREAD_BUSY_MESSAGE = "This conversation is already processing another request. Please wait for it to finish and try again."
# 实时进度快照（self._live_runs）的保留上限：超过时淘汰最旧的已结束快照，避免长期运行累积
_LIVE_RUNS_MAX = 200

CHANNEL_CAPABILITIES = {
    "dingtalk": {"supports_streaming": False},
    "discord": {"supports_streaming": False},
    "feishu": {"supports_streaming": True},
    "slack": {"supports_streaming": False},
    "telegram": {"supports_streaming": False},
    "wechat": {"supports_streaming": False},
    "wecom": {"supports_streaming": True},
}

InboundFileReader = Callable[[dict[str, Any], httpx.AsyncClient], Awaitable[bytes | None]]

_METADATA_DROP_KEYS = frozenset({"raw_message", "ref_msg"})


def _slim_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *meta* with known-large keys removed."""
    return {k: v for k, v in meta.items() if k not in _METADATA_DROP_KEYS}


INBOUND_FILE_READERS: dict[str, InboundFileReader] = {}


def register_inbound_file_reader(channel_name: str, reader: InboundFileReader) -> None:
    INBOUND_FILE_READERS[channel_name] = reader


async def _read_http_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    url = file_info.get("url")
    if not isinstance(url, str) or not url:
        return None

    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def _read_wecom_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    data = await _read_http_inbound_file(file_info, client)
    if data is None:
        return None

    aeskey = file_info.get("aeskey") if isinstance(file_info.get("aeskey"), str) else None
    if not aeskey:
        return data

    try:
        from aibot.crypto_utils import decrypt_file
    except Exception:
        logger.exception("[Manager] failed to import WeCom decrypt_file")
        return None

    return decrypt_file(data, aeskey)


async def _read_wechat_inbound_file(file_info: dict[str, Any], client: httpx.AsyncClient) -> bytes | None:
    raw_path = file_info.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        try:
            return await asyncio.to_thread(Path(raw_path).read_bytes)
        except OSError:
            logger.exception("[Manager] failed to read WeChat inbound file from local path: %s", raw_path)
            return None

    full_url = file_info.get("full_url")
    if isinstance(full_url, str) and full_url.strip():
        return await _read_http_inbound_file({"url": full_url}, client)

    return None


register_inbound_file_reader("wecom", _read_wecom_inbound_file)
register_inbound_file_reader("wechat", _read_wechat_inbound_file)


class InvalidChannelSessionConfigError(ValueError):
    """Raised when IM channel session overrides contain invalid agent config."""


def _is_thread_busy_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConflictError):
        return True
    return "already running a task" in str(exc)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_dicts(*layers: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if isinstance(layer, Mapping):
            merged.update(layer)
    return merged


def _normalize_custom_agent_name(raw_value: str) -> str:
    """Normalize legacy channel assistant IDs into valid custom agent names."""
    normalized = raw_value.strip().lower().replace("_", "-")
    if not normalized:
        raise InvalidChannelSessionConfigError("Channel session assistant_id is empty. Use 'lead_agent' or a valid custom agent name.")
    if not CUSTOM_AGENT_NAME_PATTERN.fullmatch(normalized):
        raise InvalidChannelSessionConfigError(f"Invalid channel session assistant_id {raw_value!r}. Use 'lead_agent' or a custom agent name containing only letters, digits, and hyphens.")
    return normalized


def _extract_response_text(result: dict | list) -> str:
    """Extract the last AI message text from a LangGraph runs.wait result.

    ``runs.wait`` returns the final state dict which contains a ``messages``
    list.  Each message is a dict with at least ``type`` and ``content``.

    Handles special cases:
    - Regular AI text responses
    - Clarification interrupts (``ask_clarification`` tool messages)
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return ""

    # Walk backwards to find usable response text, but stop at the last
    # human message to avoid returning text from a previous turn.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")

        # Stop at the last human message — anything before it is a previous turn
        if msg_type == "human":
            if _is_hidden_human_control_message(msg):
                continue
            break

        # Check for tool messages from ask_clarification (interrupt case)
        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content

        # Regular AI message with text content
        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
            # content can be a list of content blocks
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts)
                if text:
                    return text
    return ""


def _extract_title(result: dict | list) -> str:
    """从 graph 状态快照/结果中取出自动生成的会话标题（TitleMiddleware 写入 state.title）。

    标题存在于 ThreadState.title 通道，会随 values 流事件与 runs.wait 结果一并返回。
    """
    if isinstance(result, dict):
        title = result.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return ""


def _messages_from_result(result: dict | list) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        messages = result.get("messages", [])
        if isinstance(messages, list):
            return messages
    return []


def _current_turn_messages(result: dict | list) -> list[dict[str, Any]]:
    messages = _messages_from_result(result)
    current_turn: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "human":
            break
        current_turn.append(msg)
    current_turn.reverse()
    return current_turn


def _has_current_turn_clarification(result: dict | list) -> bool:
    """Return True only when the current turn's final result is clarification."""
    for msg in reversed(_current_turn_messages(result)):
        msg_type = msg.get("type")
        if msg_type == "tool":
            return msg.get("name") == "ask_clarification"
        if msg_type == "ai":
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    return False
            elif content:
                return False
            if msg.get("tool_calls"):
                return False
    return False


def _response_metadata(base_metadata: dict[str, Any], *, pending_clarification: bool = False) -> dict[str, Any]:
    metadata = _slim_metadata(base_metadata)
    if pending_clarification:
        metadata[PENDING_CLARIFICATION_METADATA_KEY] = True
    return metadata


def _extract_todos(result: dict | list) -> list[dict[str, Any]] | None:
    """Extract the current todo list from a graph state snapshot/result.

    Plan mode 下 lead agent 通过 write_todos 把流水线写入 ThreadState.todos，
    这里从 values 快照中取出，供渠道渲染进度。
    """
    if isinstance(result, dict):
        todos = result.get("todos")
        if isinstance(todos, list):
            return [t for t in todos if isinstance(t, dict)]
    return None


# 子任务/todo 状态 -> 展示图标（渠道无关的纯文本渲染用，飞书卡片另有自己的一套）
_TODO_STATUS_ICONS = {
    "completed": "✅",
    "in_progress": "🔄",
    "running": "🔄",
    "pending": "⬜",
    "failed": "❌",
    "cancelled": "🚫",
}


def _format_duration(seconds: float) -> str:
    """把秒数格式化为“X小时Y分钟”/“X分钟Y秒”/“X秒”，用于进度展示。"""
    total = int(max(0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{secs}秒"
    return f"{secs}秒"


# task 工具发出的 custom 事件 type -> 展示用状态
_CUSTOM_PROGRESS_STATUS = {
    "task_started": "running",
    "task_running": "running",
    "task_completed": "completed",
    "task_failed": "failed",
    "task_timed_out": "failed",
    "task_cancelled": "cancelled",
}


def _apply_custom_progress_event(running_steps: dict[str, dict[str, str]], data: Any) -> dict[str, str] | None:
    """把 task 工具的 custom 流事件（task_started/running/completed...）折叠进子任务进度。

    额外：从 task_running 的 message 抽取“活动文本”，从 task_failed/timed_out 抽取错误文本。
    当某子任务首次进入失败/超时态时，返回该步骤 dict 作为信号，供调用方推送通知（每个任务只返回一次）。
    """
    if not isinstance(data, Mapping):
        return None
    event_type = data.get("type")
    status = _CUSTOM_PROGRESS_STATUS.get(event_type) if isinstance(event_type, str) else None
    if status is None:
        return None
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    description = data.get("description")
    step = running_steps.get(task_id)
    if step is None:
        step = {
            "description": description if isinstance(description, str) and description else "子任务",
            "status": status,
        }
        running_steps[task_id] = step
    else:
        step["status"] = status
        if isinstance(description, str) and description and step.get("description") in (None, "", "子任务"):
            step["description"] = description

    # task_running/task_started：记录子智能体最近一条消息文本，作为“正在做什么”的活动提示
    activity = _extract_text_content(data.get("message"))
    if activity:
        step["activity"] = activity.strip()[:200]

    # task_failed/task_timed_out：记录错误并作为信号返回（每个任务仅首次返回，避免重复推送）
    if event_type in ("task_failed", "task_timed_out"):
        error_text = data.get("error")
        if isinstance(error_text, str) and error_text.strip():
            step["error"] = error_text.strip()
        if not step.get("_notified"):
            step["_notified"] = "1"
            return step
    return None


def _attach_progress_metadata(
    metadata: dict[str, Any],
    todos: list[dict[str, Any]] | None,
    running_steps: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """把 todos 与子任务进度写进 outbound metadata，供渠道（飞书卡片）渲染。"""
    if todos is not None:
        metadata["todos"] = todos
    if running_steps:
        metadata["pipeline_steps"] = [{"description": s.get("description", ""), "status": s.get("status", ""), "activity": s.get("activity", "")} for s in running_steps.values()]
    return metadata


def _extract_text_content(content: Any) -> str:
    """Extract text from a streaming payload content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _merge_stream_text(existing: str, chunk: str) -> str:
    """Merge either delta text or cumulative text into a single snapshot."""
    if not chunk:
        return existing
    if not existing or chunk == existing:
        return chunk or existing
    if chunk.startswith(existing):
        return chunk
    if existing.endswith(chunk):
        return existing
    return existing + chunk


def _extract_stream_message_id(payload: Any, metadata: Any) -> str | None:
    """Best-effort extraction of the streamed AI message identifier."""
    candidates = [payload, metadata]
    if isinstance(payload, Mapping):
        candidates.append(payload.get("kwargs"))

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("id", "message_id"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _accumulate_stream_text(
    buffers: dict[str, str],
    current_message_id: str | None,
    event_data: Any,
) -> tuple[str | None, str | None]:
    """Convert a ``messages-tuple`` event into the latest displayable AI text."""
    payload = event_data
    metadata: Any = None
    if isinstance(event_data, (list, tuple)):
        if event_data:
            payload = event_data[0]
        if len(event_data) > 1:
            metadata = event_data[1]

    if isinstance(payload, str):
        message_id = current_message_id or "__default__"
        buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), payload)
        return buffers[message_id], message_id

    if not isinstance(payload, Mapping):
        return None, current_message_id

    payload_type = str(payload.get("type", "")).lower()
    if "tool" in payload_type:
        return None, current_message_id

    text = _extract_text_content(payload.get("content"))
    if not text and isinstance(payload.get("kwargs"), Mapping):
        text = _extract_text_content(payload["kwargs"].get("content"))
    if not text:
        return None, current_message_id

    message_id = _extract_stream_message_id(payload, metadata) or current_message_id or "__default__"
    buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), text)
    return buffers[message_id], message_id


def _extract_artifacts(result: dict | list) -> list[str]:
    """Extract artifact paths from the last AI response cycle only.

    Instead of reading the full accumulated ``artifacts`` state (which contains
    all artifacts ever produced in the thread), this inspects the messages after
    the last human message and collects file paths from ``present_files`` tool
    calls.  This ensures only newly-produced artifacts are returned.
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return []

    artifacts: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        # Stop at the last human message — anything before it is a previous turn
        if msg.get("type") == "human":
            if _is_hidden_human_control_message(msg):
                continue
            break
        # Look for AI messages with present_files tool calls
        if msg.get("type") == "ai":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("name") == "present_files":
                    args = tc.get("args", {})
                    paths = args.get("filepaths", [])
                    if isinstance(paths, list):
                        artifacts.extend(p for p in paths if isinstance(p, str))
    return artifacts


def _is_hidden_human_control_message(msg: Mapping[str, Any]) -> bool:
    """Return whether a human message is an internal control message hidden from UI."""
    if msg.get("type") != "human":
        return False

    additional_kwargs = msg.get("additional_kwargs")
    if not isinstance(additional_kwargs, Mapping):
        return False

    return additional_kwargs.get("hide_from_ui") is True


def _format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath

    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"


def _resolve_attachments(thread_id: str, artifacts: list[str]) -> list[ResolvedAttachment]:
    """Resolve virtual artifact paths to host filesystem paths with metadata.

    Only paths under ``/mnt/user-data/outputs/`` are accepted; any other
    virtual path is rejected with a warning to prevent exfiltrating uploads
    or workspace files via IM channels.

    Skips artifacts that cannot be resolved (missing files, invalid paths)
    and logs warnings for them.
    """
    from deerflow.config.paths import get_paths

    attachments: list[ResolvedAttachment] = []
    paths = get_paths()
    user_id = get_effective_user_id()
    outputs_dir = paths.sandbox_outputs_dir(thread_id, user_id=user_id).resolve()
    for virtual_path in artifacts:
        # Security: only allow files from the agent outputs directory
        if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path, user_id=user_id)
            # Verify the resolved path is actually under the outputs directory
            # (guards against path-traversal even after prefix check)
            try:
                actual.resolve().relative_to(outputs_dir)
            except ValueError:
                logger.warning("[Manager] artifact path escapes outputs dir: %s -> %s", virtual_path, actual)
                continue
            if not actual.is_file():
                logger.warning("[Manager] artifact not found on disk: %s -> %s", virtual_path, actual)
                continue
            mime, _ = mimetypes.guess_type(str(actual))
            mime = mime or "application/octet-stream"
            attachments.append(
                ResolvedAttachment(
                    virtual_path=virtual_path,
                    actual_path=actual,
                    filename=actual.name,
                    mime_type=mime,
                    size=actual.stat().st_size,
                    is_image=mime.startswith("image/"),
                )
            )
        except (ValueError, OSError) as exc:
            logger.warning("[Manager] failed to resolve artifact %s: %s", virtual_path, exc)
    return attachments


def _prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response."""
    attachments: list[ResolvedAttachment] = []
    if not artifacts:
        return response_text, attachments

    attachments = _resolve_attachments(thread_id, artifacts)
    resolved_virtuals = {attachment.virtual_path for attachment in attachments}
    unresolved = [path for path in artifacts if path not in resolved_virtuals]

    if unresolved:
        artifact_text = _format_artifact_text(unresolved)
        response_text = (response_text + "\n\n" + artifact_text) if response_text else artifact_text

    # Always include resolved attachment filenames as a text fallback so files
    # remain discoverable even when the upload is skipped or fails.
    if attachments:
        resolved_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
        response_text = (response_text + "\n\n" + resolved_text) if response_text else resolved_text

    return response_text, attachments


async def _ingest_inbound_files(thread_id: str, msg: InboundMessage) -> list[dict[str, Any]]:
    if not msg.files:
        return []

    from deerflow.uploads.manager import (
        UnsafeUploadPathError,
        claim_unique_filename,
        ensure_uploads_dir,
        normalize_filename,
        write_upload_file_no_symlink,
    )

    uploads_dir = ensure_uploads_dir(thread_id)
    seen_names = {entry.name for entry in uploads_dir.iterdir() if entry.is_file()}

    created: list[dict[str, Any]] = []
    file_reader = INBOUND_FILE_READERS.get(msg.channel_name, _read_http_inbound_file)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for idx, f in enumerate(msg.files):
            if not isinstance(f, dict):
                continue

            ftype = f.get("type") if isinstance(f.get("type"), str) else "file"
            filename = f.get("filename") if isinstance(f.get("filename"), str) else ""

            try:
                data = await file_reader(f, client)
            except Exception:
                logger.exception(
                    "[Manager] failed to read inbound file: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if data is None:
                logger.warning(
                    "[Manager] inbound file reader returned no data: channel=%s, file=%s",
                    msg.channel_name,
                    f.get("url") or filename or idx,
                )
                continue

            if not filename:
                ext = ".bin"
                if ftype == "image":
                    ext = ".png"
                filename = f"{msg.thread_ts or 'msg'}_{idx}{ext}"

            try:
                safe_name = claim_unique_filename(normalize_filename(filename), seen_names)
            except ValueError:
                logger.warning(
                    "[Manager] skipping inbound file with unsafe filename: channel=%s, file=%r",
                    msg.channel_name,
                    filename,
                )
                continue

            dest = uploads_dir / safe_name
            try:
                dest = write_upload_file_no_symlink(uploads_dir, safe_name, data)
            except UnsafeUploadPathError:
                logger.warning("[Manager] skipping inbound file with unsafe destination: %s", safe_name)
                continue
            except Exception:
                logger.exception("[Manager] failed to write inbound file: %s", dest)
                continue

            created.append(
                {
                    "filename": safe_name,
                    "size": len(data),
                    "path": f"/mnt/user-data/uploads/{safe_name}",
                    "is_image": ftype == "image",
                }
            )

    return created


def _format_uploaded_files_block(files: list[dict[str, Any]]) -> str:
    lines = [
        "<uploaded_files>",
        "The following files were uploaded in this message:",
        "",
    ]
    if not files:
        lines.append("(empty)")
    else:
        for f in files:
            filename = f.get("filename", "")
            size = int(f.get("size") or 0)
            size_kb = size / 1024 if size else 0
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            path = f.get("path", "")
            is_image = bool(f.get("is_image"))
            file_kind = "image" if is_image else "file"
            lines.append(f"- {filename} ({size_str})")
            lines.append(f"  Type: {file_kind}")
            lines.append(f"  Path: {path}")
            lines.append("")
    lines.append("Use `read_file` for text-based files and documents.")
    lines.append("Use `view_image` for image files (jpg, jpeg, png, webp) so the model can inspect the image content.")
    lines.append("</uploaded_files>")
    return "\n".join(lines)


# 记忆文本格式化与范围渲染统一由 app.channels.memory_view 提供（文本命令与飞书卡片共用）。


class ChannelManager:
    """Core dispatcher that bridges IM channels to the DeerFlow agent.

    It reads from the MessageBus inbound queue, creates/reuses threads on
    Gateway's LangGraph-compatible API, sends messages via ``runs.wait``, and publishes
    outbound responses back through the bus.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        *,
        max_concurrency: int = 5,
        langgraph_url: str = DEFAULT_LANGGRAPH_URL,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        default_session: dict[str, Any] | None = None,
        channel_sessions: dict[str, Any] | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self._max_concurrency = max_concurrency
        self._langgraph_url = langgraph_url
        self._gateway_url = gateway_url
        self._assistant_id = assistant_id
        self._default_session = _as_dict(default_session)
        self._channel_sessions = dict(channel_sessions or {})
        self._client = None  # lazy init — langgraph_sdk async client
        self._csrf_token = generate_csrf_token()
        self._semaphore: asyncio.Semaphore | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        # 每线程实时进度快照（key = DeerFlow thread_id），供 /status 与忙时回复读取
        self._live_runs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _channel_supports_streaming(channel_name: str) -> bool:
        from .service import get_channel_service

        service = get_channel_service()
        if service:
            channel = service.get_channel(channel_name)
            if channel is not None:
                return channel.supports_streaming
        return CHANNEL_CAPABILITIES.get(channel_name, {}).get("supports_streaming", False)

    def _resolve_session_layer(self, msg: InboundMessage) -> tuple[dict[str, Any], dict[str, Any]]:
        channel_layer = _as_dict(self._channel_sessions.get(msg.channel_name))
        users_layer = _as_dict(channel_layer.get("users"))
        user_layer = _as_dict(users_layer.get(msg.user_id))
        return channel_layer, user_layer

    def _resolve_run_params(self, msg: InboundMessage, thread_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        channel_layer, user_layer = self._resolve_session_layer(msg)

        # 会话级人设（/agent 菜单选择，按 channel:chat_id 记忆）优先级最高，
        # 其次才是渠道/用户/全局默认会话配置。
        stored_agent = self.store.get_agent(msg.channel_name, msg.chat_id)

        assistant_id = stored_agent or user_layer.get("assistant_id") or channel_layer.get("assistant_id") or self._default_session.get("assistant_id") or self._assistant_id
        if not isinstance(assistant_id, str) or not assistant_id.strip():
            assistant_id = self._assistant_id

        run_config = _merge_dicts(
            DEFAULT_RUN_CONFIG,
            self._default_session.get("config"),
            channel_layer.get("config"),
            user_layer.get("config"),
        )

        configurable = run_config.get("configurable")
        if isinstance(configurable, Mapping):
            configurable = dict(configurable)
        else:
            configurable = {}
        run_config["configurable"] = configurable
        # Pin channel-triggered runs to the root graph namespace so follow-up
        # turns continue from the same conversation checkpoint.
        configurable["checkpoint_ns"] = ""
        configurable["thread_id"] = thread_id

        # 会话级模型覆盖（/model 或飞书模型卡选择，按 channel:chat_id 记忆）。
        # 仅注入；无效/失效的模型名由 lead_agent._resolve_model_name 安全回退默认模型。
        stored_model = self.store.get_model(msg.channel_name, msg.chat_id)
        if stored_model:
            configurable["model_name"] = stored_model

        # ``user_id`` drives user-scoped filesystem buckets that only accept
        # ``[A-Za-z0-9_-]``, so normalize the channel id and keep the raw value
        # under ``channel_user_id`` for platform-facing lookups.
        run_context_identity: dict[str, Any] = {"thread_id": thread_id}
        if msg.user_id:
            run_context_identity["user_id"] = make_safe_user_id(msg.user_id)
            run_context_identity["channel_user_id"] = msg.user_id

        run_context = _merge_dicts(
            DEFAULT_RUN_CONTEXT,
            self._default_session.get("context"),
            channel_layer.get("context"),
            user_layer.get("context"),
            run_context_identity,
        )

        # Custom agents are implemented as lead_agent + agent_name context.
        # Keep backward compatibility for channel configs that set
        # assistant_id: <custom-agent-name> by routing through lead_agent.
        if assistant_id != DEFAULT_ASSISTANT_ID:
            run_context.setdefault("agent_name", _normalize_custom_agent_name(assistant_id))
            assistant_id = DEFAULT_ASSISTANT_ID

        return assistant_id, run_config, run_context

    # -- live progress snapshot（实时进度，供 /status 与忙时回复读取）---------

    def _start_live_progress(self, thread_id: str) -> None:
        """一个 run 首次拥有快照时，重建为全新 running 状态。

        必须在每轮 run 收到首个流事件时调用一次：否则同一会话的第二轮起会复用上一轮
        已结束的快照，导致 started_at 陈旧（已运行时长偏大）、残留上一轮的 todos/子任务。
        被拒（busy）的 run 收不到事件、不会调用此方法，因此不会覆盖正在运行的活动快照。
        """
        if not thread_id:
            return
        now = time.time()
        self._live_runs[thread_id] = {
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "todos": None,
            "steps": {},
            "latest_text": "",
        }

    def _update_live_progress(
        self,
        thread_id: str,
        *,
        todos: list[dict[str, Any]] | None,
        steps: dict[str, dict[str, str]] | None,
        latest_text: str,
    ) -> None:
        """逐流事件更新某线程的实时进度快照（不存在则创建为 running）。"""
        if not thread_id:
            return
        now = time.time()
        snapshot = self._live_runs.get(thread_id)
        if snapshot is None:
            snapshot = {"status": "running", "started_at": now, "todos": None, "steps": {}, "latest_text": ""}
            self._live_runs[thread_id] = snapshot
        snapshot["status"] = "running"
        snapshot["updated_at"] = now
        if todos is not None:
            snapshot["todos"] = todos
        if steps:
            # 深拷贝一层，避免后续流循环里就地修改污染快照
            snapshot["steps"] = {tid: dict(step) for tid, step in steps.items()}
        if latest_text:
            snapshot["latest_text"] = latest_text

    def _finish_live_progress(self, thread_id: str) -> None:
        """标记某线程的实时进度快照为已结束（保留内容供随后查询），并按上限清理。"""
        snapshot = self._live_runs.get(thread_id) if thread_id else None
        if snapshot is not None:
            snapshot["status"] = "finished"
            snapshot["updated_at"] = time.time()
        self._prune_live_runs()

    def _prune_live_runs(self) -> None:
        """限制 _live_runs 规模：超过上限时淘汰最旧的已结束快照（运行中的一律保留）。"""
        if len(self._live_runs) <= _LIVE_RUNS_MAX:
            return
        finished = [(tid, snap) for tid, snap in self._live_runs.items() if snap.get("status") != "running"]
        finished.sort(key=lambda item: item[1].get("updated_at", 0.0))
        excess = len(self._live_runs) - _LIVE_RUNS_MAX
        for tid, _snap in finished[:excess]:
            self._live_runs.pop(tid, None)

    def _render_progress_snapshot(self, snapshot: dict[str, Any] | None) -> str:
        """把实时进度快照渲染成 markdown 文本（无快照返回空串）。"""
        if not snapshot:
            return ""
        now = time.time()
        started = snapshot.get("started_at") or now
        updated = snapshot.get("updated_at") or now
        is_running = snapshot.get("status") == "running"
        lines = ["**🟢 团队正在执行中**" if is_running else "**✅ 团队已完成本轮**"]
        lines.append(f"已运行 {_format_duration(now - started)}，最近更新 {_format_duration(now - updated)}前")

        todos = snapshot.get("todos")
        if isinstance(todos, list) and todos:
            lines.append("")
            lines.append("**任务进度**")
            for todo in todos:
                if not isinstance(todo, dict):
                    continue
                content = str(todo.get("content", "")).strip()
                if not content:
                    continue
                icon = _TODO_STATUS_ICONS.get(str(todo.get("status", "")), "▫️")
                lines.append(f"{icon} {content}")

        steps = snapshot.get("steps") or {}
        running_steps = [s for s in steps.values() if isinstance(s, dict) and s.get("status") == "running"]
        if running_steps:
            lines.append("")
            lines.append("**当前子任务**")
            for step in running_steps:
                desc = str(step.get("description", "")).strip() or "子任务"
                line = f"🔧 {desc}"
                activity = str(step.get("activity", "")).strip()
                if activity:
                    line += f"\n   ↳ {activity}"
                lines.append(line)

        return "\n".join(lines)

    async def _fetch_run_status(self, thread_id: str) -> str:
        """best-effort：从 Gateway 取该 thread 最新 run 的权威状态/累计 token（失败返回空串）。"""
        if not thread_id:
            return ""
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self._gateway_url}/api/threads/{thread_id}/runs",
                    timeout=10,
                    headers=create_internal_auth_headers(),
                )
                resp.raise_for_status()
                runs = resp.json()
        except Exception:
            logger.debug("Failed to fetch run status for thread %s", thread_id, exc_info=True)
            return ""
        if not isinstance(runs, list) or not runs:
            return ""
        run = runs[0]  # list_by_thread 已按 created_at 倒序，[0] 为最新
        status = str(run.get("status", "")) or "unknown"
        total_tokens = run.get("total_tokens", 0) or 0
        message_count = run.get("message_count", 0) or 0
        lines = [f"运行状态：{status}"]
        if total_tokens or message_count:
            lines.append(f"累计 token：{total_tokens}（消息 {message_count}）")
        return "\n".join(lines)

    async def _publish_failure_notification(self, msg: InboundMessage, thread_id: str, step: dict[str, str]) -> None:
        """子任务失败/超时时，推送一条独立通知 outbound（渠道据 NOTIFICATION_METADATA_KEY 另发新消息）。"""
        desc = str(step.get("description", "")).strip() or "子任务"
        error_text = str(step.get("error", "")).strip()
        workdir = self.store.get_workdir(msg.channel_name, msg.chat_id)
        lines = [f"子任务执行失败：{desc}"]
        if workdir:
            lines.append(f"工作目录：{workdir}")
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
                metadata=metadata,
            )
        )

    # -- LangGraph SDK client (lazy) ----------------------------------------

    def _get_client(self):
        """Return the ``langgraph_sdk`` async client, creating it on first use."""
        if self._client is None:
            from langgraph_sdk import get_client

            self._client = get_client(
                url=self._langgraph_url,
                headers={
                    **create_internal_auth_headers(),
                    CSRF_HEADER_NAME: self._csrf_token,
                    "Cookie": f"{CSRF_COOKIE_NAME}={self._csrf_token}",
                },
            )
        return self._client

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatch loop."""
        if self._running:
            return
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("ChannelManager started (max_concurrency=%d)", self._max_concurrency)

    async def stop(self) -> None:
        """Stop the dispatch loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ChannelManager stopped")

    # -- dispatch loop -----------------------------------------------------

    async def _dispatch_loop(self) -> None:
        logger.info("[Manager] dispatch loop started, waiting for inbound messages")
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            logger.info(
                "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text=%r",
                msg.channel_name,
                msg.chat_id,
                msg.msg_type.value,
                msg.text[:100] if msg.text else "",
            )
            task = asyncio.create_task(self._handle_message(msg))
            task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("[Manager] unhandled error in message task: %s", exc, exc_info=exc)

    async def _handle_message(self, msg: InboundMessage) -> None:
        async with self._semaphore:
            try:
                if msg.msg_type == InboundMessageType.COMMAND:
                    await self._handle_command(msg)
                else:
                    await self._handle_chat(msg)
            except InvalidChannelSessionConfigError as exc:
                logger.warning(
                    "Invalid channel session config for %s (chat=%s): %s",
                    msg.channel_name,
                    msg.chat_id,
                    exc,
                )
                await self._send_error(msg, str(exc))
            except Exception:
                logger.exception(
                    "Error handling message from %s (chat=%s)",
                    msg.channel_name,
                    msg.chat_id,
                )
                await self._send_error(msg, "An internal error occurred. Please try again.")

    # -- chat handling -----------------------------------------------------

    async def _create_thread(self, client, msg: InboundMessage) -> str:
        """Create a new thread through Gateway and store the mapping."""
        thread = await client.threads.create()
        thread_id = thread["thread_id"]
        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )
        # 把新线程登记进会话注册表，并快照当前 人设/模型/工作目录（供 /sessions 切换时恢复）
        self.store.add_session(msg.channel_name, msg.chat_id, thread_id)
        logger.info("[Manager] new thread created through Gateway: thread_id=%s for chat_id=%s topic_id=%s", thread_id, msg.chat_id, msg.topic_id)
        return thread_id

    async def _start_new_thread(self, msg: InboundMessage) -> str:
        """显式开启一个新会话（/new、/sessions new、飞书「新会话」按钮共用）。"""
        client = self._get_client()
        thread = await client.threads.create()
        new_thread_id = thread["thread_id"]
        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            new_thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )
        self.store.add_session(msg.channel_name, msg.chat_id, new_thread_id)
        return new_thread_id

    def _record_session_activity(self, msg: InboundMessage, thread_id: str, result: dict | list) -> None:
        """每轮 run 结束：缓存自动生成的标题 + 刷新会话活跃时间（注册表用于 /sessions）。"""
        if not thread_id:
            return
        try:
            title = _extract_title(result)
            if title:
                self.store.set_session_title(msg.channel_name, msg.chat_id, thread_id, title)
            else:
                self.store.touch_session(msg.channel_name, msg.chat_id, thread_id)
        except Exception:
            logger.debug("[Manager] failed to record session activity for thread %s", thread_id, exc_info=True)

    async def _handle_chat(self, msg: InboundMessage, extra_context: dict[str, Any] | None = None) -> None:
        client = self._get_client()

        # 会话设置过工作目录（/repo）时，在消息前注入目录上下文，
        # 让 lead agent 与角色子代理默认在该目录操作。
        workdir = self.store.get_workdir(msg.channel_name, msg.chat_id)
        if workdir:
            msg.text = f"[当前工作目录：{workdir}。未明确指定其他路径时，所有代码/文件操作均在此目录进行。]\n\n{msg.text}"

        # Look up existing DeerFlow thread.
        # topic_id may be None (e.g. Telegram private chats) — the store
        # handles this by using the "channel:chat_id" key without a topic suffix.
        thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        if thread_id:
            logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)

        # No existing thread found — create a new one
        if thread_id is None:
            thread_id = await self._create_thread(client, msg)

        assistant_id, run_config, run_context = self._resolve_run_params(msg, thread_id)

        # If the inbound message contains file attachments, let the channel
        # materialize (download) them and update msg.text to include sandbox file paths.
        # This enables downstream models to access user-uploaded files by path.
        # Channels that do not support file download will simply return the original message.
        if msg.files:
            from .service import get_channel_service

            service = get_channel_service()
            channel = service.get_channel(msg.channel_name) if service else None
            logger.info("[Manager] preparing receive file context for %d attachments", len(msg.files))
            msg = await channel.receive_file(msg, thread_id) if channel else msg
        if extra_context:
            run_context.update(extra_context)

        uploaded = await _ingest_inbound_files(thread_id, msg)
        if uploaded:
            msg.text = f"{_format_uploaded_files_block(uploaded)}\n\n{msg.text}".strip()

        if self._channel_supports_streaming(msg.channel_name):
            await self._handle_streaming_chat(
                client,
                msg,
                thread_id,
                assistant_id,
                run_config,
                run_context,
            )
            return

        logger.info("[Manager] invoking runs.wait(thread_id=%s, text=%r)", thread_id, msg.text[:100])
        try:
            result = await client.runs.wait(
                thread_id,
                assistant_id,
                input={"messages": [{"role": "human", "content": msg.text}]},
                config=run_config,
                context=run_context,
                multitask_strategy="reject",
            )
        except Exception as exc:
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
                # 撞上运行中：优先回显实时进度快照（仅当同线程有流式 run 在跑时才有内容），否则回退通用文案
                snapshot = self._live_runs.get(thread_id)
                rendered = self._render_progress_snapshot(snapshot) if snapshot else ""
                await self._send_error(msg, rendered or THREAD_BUSY_MESSAGE)
                return
            else:
                raise

        response_text = _extract_response_text(result)
        pending_clarification = _has_current_turn_clarification(result)
        artifacts = _extract_artifacts(result)
        self._record_session_activity(msg, thread_id, result)

        logger.info(
            "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
            thread_id,
            len(response_text) if response_text else 0,
            len(artifacts),
        )

        response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts)

        if not response_text:
            if attachments:
                response_text = _format_artifact_text([a.virtual_path for a in attachments])
            else:
                response_text = "(No response from agent)"

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=thread_id,
            text=response_text,
            artifacts=artifacts,
            attachments=attachments,
            thread_ts=msg.thread_ts,
            metadata=_response_metadata(msg.metadata, pending_clarification=pending_clarification),
        )
        logger.info("[Manager] publishing outbound message to bus: channel=%s, chat_id=%s", msg.channel_name, msg.chat_id)
        await self.bus.publish_outbound(outbound)

    async def _handle_streaming_chat(
        self,
        client,
        msg: InboundMessage,
        thread_id: str,
        assistant_id: str,
        run_config: dict[str, Any],
        run_context: dict[str, Any],
    ) -> None:
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text=%r)", thread_id, msg.text[:100])

        last_values: dict[str, Any] | list | None = None
        streamed_buffers: dict[str, str] = {}
        current_message_id: str | None = None
        latest_text = ""
        last_published_text = ""
        last_publish_at = 0.0
        stream_error: BaseException | None = None
        # plan mode / 流水线进度：todos 来自 values 快照，子任务步骤来自 task 工具的 custom 事件
        latest_todos: list[dict[str, Any]] | None = None
        running_steps: dict[str, dict[str, str]] = {}
        last_published_progress = ""
        # 仅在真正收到首个流事件时才“拥有”该线程的实时快照：撞忙被拒的 run 在发事件前就报错，
        # 不会置位，从而不会覆盖/收尾正在运行的那条活动快照。
        owns_snapshot = False

        try:
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                input={"messages": [{"role": "human", "content": msg.text}]},
                config=run_config,
                context=run_context,
                stream_mode=["messages-tuple", "values", "custom"],
                multitask_strategy="reject",
            ):
                if not owns_snapshot:
                    # 首个流事件：本轮 run 取得快照所有权，重建为全新状态（避免复用上一轮的陈旧快照）
                    owns_snapshot = True
                    self._start_live_progress(thread_id)
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                if event == "messages-tuple":
                    accumulated_text, current_message_id = _accumulate_stream_text(streamed_buffers, current_message_id, data)
                    if accumulated_text:
                        latest_text = accumulated_text
                elif event == "values" and isinstance(data, (dict, list)):
                    last_values = data
                    todos = _extract_todos(data)
                    if todos is not None:
                        latest_todos = todos
                    snapshot_text = _extract_response_text(data)
                    if snapshot_text:
                        latest_text = snapshot_text
                elif event == "custom":
                    failed_step = _apply_custom_progress_event(running_steps, data)
                    if failed_step is not None:
                        await self._publish_failure_notification(msg, thread_id, failed_step)

                # 每个流事件都刷新该线程的实时进度快照（供 /status 与忙时回复读取）
                self._update_live_progress(thread_id, todos=latest_todos, steps=running_steps, latest_text=latest_text)

                # 文本或进度任一变化即推送（进度可在尚无文本时先行展示）
                progress_signature = repr((latest_todos, [(tid, s.get("status"), s.get("activity")) for tid, s in running_steps.items()]))
                text_changed = bool(latest_text) and latest_text != last_published_text
                progress_changed = progress_signature != last_published_progress
                if not (text_changed or progress_changed):
                    continue
                if not latest_text and latest_todos is None and not running_steps:
                    continue

                now = time.monotonic()
                if (last_published_text or last_published_progress) and now - last_publish_at < STREAM_UPDATE_MIN_INTERVAL_SECONDS:
                    continue

                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel_name=msg.channel_name,
                        chat_id=msg.chat_id,
                        thread_id=thread_id,
                        text=latest_text,
                        is_final=False,
                        thread_ts=msg.thread_ts,
                        metadata=_attach_progress_metadata(_response_metadata(msg.metadata), latest_todos, running_steps),
                    )
                )
                last_published_text = latest_text
                last_published_progress = progress_signature
                last_publish_at = now
        except Exception as exc:
            stream_error = exc
            if _is_thread_busy_error(exc):
                logger.warning("[Manager] thread busy (concurrent run rejected): thread_id=%s", thread_id)
            else:
                logger.exception("[Manager] streaming error: thread_id=%s", thread_id)
        finally:
            result = last_values if last_values is not None else {"messages": [{"type": "ai", "content": latest_text}]}
            response_text = _extract_response_text(result)
            pending_clarification = _has_current_turn_clarification(result)
            artifacts = _extract_artifacts(result)
            self._record_session_activity(msg, thread_id, result)
            final_todos = _extract_todos(result)
            if final_todos is not None:
                latest_todos = final_todos
            response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts)

            if not response_text:
                if attachments:
                    response_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
                elif stream_error:
                    if _is_thread_busy_error(stream_error):
                        # 撞上运行中：回显当前运行的实时进度快照，而非通用忙碌文案
                        snapshot = self._live_runs.get(thread_id)
                        rendered = self._render_progress_snapshot(snapshot) if snapshot else ""
                        response_text = rendered or THREAD_BUSY_MESSAGE
                    else:
                        response_text = "An error occurred while processing your request. Please try again."
                else:
                    response_text = latest_text or "(No response from agent)"

            logger.info(
                "[Manager] streaming response completed: thread_id=%s, response_len=%d, artifacts=%d, error=%s",
                thread_id,
                len(response_text),
                len(artifacts),
                stream_error,
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    thread_id=thread_id,
                    text=response_text,
                    artifacts=artifacts,
                    attachments=attachments,
                    is_final=True,
                    thread_ts=msg.thread_ts,
                    metadata=_attach_progress_metadata(
                        _response_metadata(msg.metadata, pending_clarification=pending_clarification),
                        latest_todos,
                        running_steps,
                    ),
                )
            )

            # 仅由真正拥有快照的 run（本轮活动 run）负责收尾，避免撞忙被拒的 run 误标结束
            if owns_snapshot:
                self._finish_live_progress(thread_id)

    # -- command handling --------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        text = msg.text.strip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower().lstrip("/")

        if command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(msg, text=chat_text, msg_type=InboundMessageType.CHAT)
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True})
            return

        if command == "new":
            # Create a new thread through Gateway
            await self._start_new_thread(msg)
            reply = "New conversation started."
        elif command == "repo":
            reply = self._handle_repo_command(msg, parts[1].strip() if len(parts) > 1 else "")
        elif command == "agent":
            reply = self._handle_agent_command(msg, parts[1].strip() if len(parts) > 1 else "")
        elif command == "sessions":
            reply = await self._handle_sessions_command(msg, parts[1].strip() if len(parts) > 1 else "")
        elif command == "status":
            thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
            sections: list[str] = []

            # 1) 运行中：前置实时进度快照（当前步骤/子任务/已运行时长）
            snapshot = self._live_runs.get(thread_id) if thread_id else None
            if snapshot and snapshot.get("status") == "running":
                rendered = self._render_progress_snapshot(snapshot)
                if rendered:
                    sections.append(rendered)

            # 2) 权威状态（best-effort 查 Gateway 最新 run 的 status / 累计 token）
            if thread_id:
                gateway_status = await self._fetch_run_status(thread_id)
                if gateway_status:
                    sections.append(gateway_status)

            # 3) 原有基础信息
            base = f"Active thread: {thread_id}" if thread_id else "No active conversation."
            workdir = self.store.get_workdir(msg.channel_name, msg.chat_id)
            base += f"\n当前工作目录：{workdir}" if workdir else "\n当前工作目录：未设置（用 /repo 选择）"
            base += f"\n当前人设：{self._current_persona_label(msg)}"
            sections.append(base)

            reply = "\n\n".join(sections)
        elif command == "models":
            reply = await self._fetch_gateway("/api/models", "models")
        elif command == "model":
            reply = self._handle_model_command(msg, parts[1].strip() if len(parts) > 1 else "")
        elif command == "memory":
            # /memory [global|default|persona]，无参数默认查看当前用户全局记忆
            scope = parts[1].strip().lower() if len(parts) > 1 else "global"
            reply = await self._render_memory(msg, scope)
        elif command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/new — Start a new conversation\n"
                "/sessions — 列出/切换/删除/查看历史会话（/sessions switch|delete|view|rename <序号>）\n"
                "/repo — 选择/查看当前工作目录（/repo <名称> 直接设置，/repo clear 清除）\n"
                "/agent — 选择/查看当前人设（/agent <名称> 直接切换，/agent default 恢复通用助手）\n"
                "/status — Show current thread info\n"
                "/models — List available models\n"
                "/model — 选择/查看当前模型（/model <名称> 直接切换，/model default 恢复默认）\n"
                "/memory — 查看记忆（默认 = 当前用户全局记忆）\n"
                "/memory global|default|persona — 分别查看 当前用户全局 / 跨用户共享 default 桶 / 当前用户+当前人设 记忆\n"
                "/help — Show this help"
            )
        else:
            available = " | ".join(sorted(KNOWN_CHANNEL_COMMANDS))
            reply = f"Unknown command: /{command}. Available commands: {available}"

        # reply 为 None 表示静默命令（如 /sessions _gcdelete）：不产生任何 outbound。
        if reply is None:
            return

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=reply,
            thread_ts=msg.thread_ts,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)

    def _handle_repo_command(self, msg: InboundMessage, arg: str) -> str:
        """处理 /repo 命令的通用文本逻辑（飞书无参数时由渠道层改发交互卡片）。"""
        from app.channels.workdir import list_project_workdirs, normalize_workdir

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

        # 无参数：文本式列出当前设置、历史与全部可选目录
        current = self.store.get_workdir(msg.channel_name, msg.chat_id)
        history = self.store.get_workdir_history(msg.channel_name, msg.chat_id)
        available = list_project_workdirs()

        lines = [f"当前工作目录：{current or '未设置'}"]
        recent = [h for h in history if h != current][:5]
        if recent:
            lines.append("\n最近使用：")
            lines.extend(f"• {p}" for p in recent)
        lines.append("\n全部可选：")
        if available:
            lines.extend(f"• {p}" for p in available)
        else:
            lines.append("（项目目录为空，把代码放到宿主机 /work/projects/ 下）")
        lines.append("\n用 /repo <名称或路径> 设置，/repo clear 清除。")
        return "\n".join(lines)

    def _effective_persona(self, msg: InboundMessage) -> str:
        """返回该会话当前生效的人设名（会话已选 > config 默认）。"""
        stored = self.store.get_agent(msg.channel_name, msg.chat_id)
        if stored:
            return stored
        default_id = self._default_session.get("assistant_id") or self._assistant_id
        return default_id if isinstance(default_id, str) and default_id.strip() else self._assistant_id

    @staticmethod
    def _persona_label(name: str) -> str:
        from app.channels.personas import DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL

        return DEFAULT_PERSONA_LABEL if name == DEFAULT_PERSONA or name == DEFAULT_ASSISTANT_ID else name

    def _current_persona_label(self, msg: InboundMessage) -> str:
        return self._persona_label(self._effective_persona(msg))

    async def _render_memory(self, msg: InboundMessage, scope: str) -> str:
        """按 scope 读取并渲染记忆（委托给 memory_view 共享实现）。

        - global：当前用户全局记忆（agent_name=None, user_id=当前用户）
        - default：跨用户共享 default 桶（agent_name=None, user_id=default）
        - persona：当前用户 + 当前人设记忆（agent_name=当前人设, user_id=当前用户）
        """
        # 无 msg.user_id 时回落 DEFAULT_USER_ID，与实际 run 的 user_id 解析行为一致
        current_user_id = make_safe_user_id(msg.user_id) if msg.user_id else DEFAULT_USER_ID
        persona = self._effective_persona(msg)
        # render_memory 内部为同步文件 IO，放到线程池避免阻塞事件循环
        return await asyncio.to_thread(render_memory, scope, user_id=current_user_id, persona=persona)

    def _handle_agent_command(self, msg: InboundMessage, arg: str) -> str:
        """处理 /agent 命令的通用文本逻辑（飞书无参数时由渠道层改发交互卡片）。"""
        from app.channels.personas import DEFAULT_PERSONA, DEFAULT_PERSONA_LABEL, list_personas, normalize_persona

        if arg.lower() in ("clear", "default", "reset"):
            self.store.clear_agent(msg.channel_name, msg.chat_id)
            return f"已恢复默认人设：{DEFAULT_PERSONA_LABEL}"

        if arg:
            persona = normalize_persona(arg)
            if persona is None:
                hint = "\n".join(f"• {label}（{name}）" for name, label, _desc in list_personas())
                return f"无效的人设：{arg}\n可选人设：\n{hint}"
            # 选默认人设时清除覆盖即可（回落到 config 默认）
            if persona == DEFAULT_PERSONA:
                self.store.clear_agent(msg.channel_name, msg.chat_id)
                return f"人设已切换为：{DEFAULT_PERSONA_LABEL}"
            self.store.set_agent(msg.channel_name, msg.chat_id, persona)
            return f"人设已切换为：{persona}"

        # 无参数：文本式列出当前与全部可选人设
        current_name = self._effective_persona(msg)
        lines = [f"当前人设：{self._persona_label(current_name)}", "\n可选人设："]
        for name, label, desc in list_personas():
            mark = " ✓" if name == current_name else ""
            suffix = f" — {desc}" if desc else ""
            lines.append(f"• {label}（{name}）{mark}{suffix}")
        lines.append("\n用 /agent <名称> 切换，/agent default 恢复通用助手。")
        return "\n".join(lines)

    # -- /sessions 多会话管理（列表 / 切换 / 删除 / 查看 / 重命名）----------------

    _SESSIONS_LIST_HINT = "用 /sessions switch <序号> 切换，/sessions view <序号> 查看，/sessions delete <序号> 删除。"

    async def _handle_sessions_command(self, msg: InboundMessage, arg: str) -> str | None:
        """处理 /sessions 命令：无参列表；子命令 switch/delete/view/rename/new。

        返回回复文本；返回 None 表示静默（不回消息，用于卡片侧已处理的内部清理命令）。
        """
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if not sub or sub in ("list", "ls"):
            return self._render_sessions_list(msg)
        if sub in ("switch", "use", "resume", "go", "open"):
            return self._handle_session_switch(msg, rest)
        if sub in ("delete", "del", "rm", "remove"):
            return await self._handle_session_delete(msg, rest)
        if sub in ("view", "show", "info"):
            return await self._handle_session_view(msg, rest)
        if sub in ("rename", "name"):
            return await self._handle_session_rename(msg, rest)
        if sub == "new":
            await self._start_new_thread(msg)
            return "已开启新会话。直接发消息即可开始。"
        if sub == "_gcdelete":
            # 内部命令：飞书卡片删除已在本地移除并刷新卡片，这里只做 Gateway 线程清理，不回消息
            if rest:
                await self._delete_gateway_thread(rest)
            return None
        return f"未知子命令：{sub}\n{self._SESSIONS_LIST_HINT}"

    # -- session 展示与解析助手 --------------------------------------------

    @staticmethod
    def _session_display_title(session: Mapping[str, Any]) -> str:
        title = session.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        return "未命名会话"

    def _session_persona_label(self, agent: Any) -> str:
        return self._persona_label(agent) if isinstance(agent, str) and agent.strip() else "通用助手"

    @staticmethod
    def _relative_time(ts: Any) -> str:
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

    @staticmethod
    def _resolve_session_ref(sessions: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
        """把用户输入（1 起的序号，或 thread_id / 其前缀）解析为某个会话快照。"""
        token = (token or "").strip()
        if not token:
            return None
        if token.isdigit():
            idx = int(token)
            return sessions[idx - 1] if 1 <= idx <= len(sessions) else None
        for s in sessions:
            if s.get("thread_id") == token:
                return s
        matches = [s for s in sessions if str(s.get("thread_id", "")).startswith(token)]
        return matches[0] if len(matches) == 1 else None

    def _render_sessions_list(self, msg: InboundMessage) -> str:
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        if not sessions:
            return "当前没有会话记录。直接发消息即可开始第一个会话。"
        current = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        lines = [f"会话列表（共 {len(sessions)} 个）："]
        for idx, s in enumerate(sessions, 1):
            mark = "✓ " if s.get("thread_id") == current else ""
            meta_bits = [self._session_persona_label(s.get("agent"))]
            rel = self._relative_time(s.get("updated_at"))
            if rel:
                meta_bits.append(rel)
            lines.append(f"{idx}. {mark}{self._session_display_title(s)}（{' · '.join(meta_bits)}）")
        lines.append("")
        lines.append(self._SESSIONS_LIST_HINT)
        return "\n".join(lines)

    def _handle_session_switch(self, msg: InboundMessage, rest: str) -> str:
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        target = self._resolve_session_ref(sessions, rest)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        snapshot = self.store.switch_session(msg.channel_name, msg.chat_id, target["thread_id"])
        if snapshot is None:
            return "切换失败：会话不存在。"
        settings = [f"人设：{self._session_persona_label(snapshot.get('agent'))}"]
        if snapshot.get("model"):
            settings.append(f"模型：{snapshot['model']}")
        if snapshot.get("workdir"):
            settings.append(f"工作目录：{snapshot['workdir']}")
        return f"已切换到会话：{self._session_display_title(snapshot)}\n已恢复 {'，'.join(settings)}。\n继续发消息即可在该会话上下文中对话。"

    async def _handle_session_delete(self, msg: InboundMessage, rest: str) -> str:
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        target = self._resolve_session_ref(sessions, rest)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        thread_id = target["thread_id"]
        title = self._session_display_title(target)
        await self._delete_gateway_thread(thread_id)
        result = self.store.remove_session(msg.channel_name, msg.chat_id, thread_id)
        if not result.get("removed"):
            return "删除失败：会话不存在。"
        lines = [f"已删除会话：{title}"]
        if result.get("was_current"):
            new_current = result.get("new_current")
            if new_current:
                snap = self.store.get_session(msg.channel_name, msg.chat_id, new_current)
                lines.append(f"已自动切换到最近会话：{self._session_display_title(snap or {})}")
            else:
                lines.append("这是当前会话，已清空；下条消息将开启新会话。")
        return "\n".join(lines)

    async def _handle_session_view(self, msg: InboundMessage, rest: str) -> str:
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        target = self._resolve_session_ref(sessions, rest)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        thread_id = target["thread_id"]
        current = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        idx = next((i for i, s in enumerate(sessions, 1) if s.get("thread_id") == thread_id), None)
        lines = [f"会话：{self._session_display_title(target)}" + ("（当前）" if thread_id == current else "")]
        lines.append(f"人设：{self._session_persona_label(target.get('agent'))}")
        if target.get("model"):
            lines.append(f"模型：{target['model']}")
        if target.get("workdir"):
            lines.append(f"工作目录：{target['workdir']}")
        created = self._relative_time(target.get("created_at"))
        if created:
            lines.append(f"创建于：{created}")
        snippet = await self._fetch_thread_last_reply(thread_id)
        if snippet:
            lines.append("")
            lines.append(f"最近回复：{snippet}")
        if idx is not None and thread_id != current:
            lines.append("")
            lines.append(f"用 /sessions switch {idx} 切换到该会话。")
        return "\n".join(lines)

    async def _handle_session_rename(self, msg: InboundMessage, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "用法：/sessions rename <序号> <新名称>"
        ref, new_name = parts[0], parts[1].strip()
        sessions = self.store.list_sessions(msg.channel_name, msg.chat_id)
        target = self._resolve_session_ref(sessions, ref)
        if target is None:
            return "找不到该会话。用 /sessions 查看列表。"
        thread_id = target["thread_id"]
        await self._rename_gateway_thread(thread_id, new_name)
        self.store.set_session_title(msg.channel_name, msg.chat_id, thread_id, new_name)
        return f"已重命名为：{new_name}"

    # -- Gateway 线程操作（删除 / 重命名 / 取最近回复，供 /sessions 使用）---------

    async def _delete_gateway_thread(self, thread_id: str) -> None:
        """通过 Gateway 彻底删除线程（文件/检查点/元数据）。失败仅告警，不阻断本地清理。"""
        try:
            await self._get_client().threads.delete(thread_id)
        except Exception:
            logger.warning("[Manager] failed to delete gateway thread %s", thread_id, exc_info=True)

    async def _rename_gateway_thread(self, thread_id: str, title: str) -> None:
        """通过 Gateway 更新线程标题（写 state.title → 同步 threads_meta.display_name）。"""
        try:
            await self._get_client().threads.update_state(thread_id, {"title": title})
        except Exception:
            logger.debug("[Manager] failed to rename gateway thread %s (non-fatal)", thread_id, exc_info=True)

    async def _fetch_thread_last_reply(self, thread_id: str) -> str:
        """best-effort：取某线程最近一条助手回复片段（用于 /sessions view）。"""
        try:
            state = await self._get_client().threads.get_state(thread_id)
        except Exception:
            logger.debug("[Manager] failed to fetch state for thread %s", thread_id, exc_info=True)
            return ""
        values = state.get("values") if isinstance(state, Mapping) else None
        if not isinstance(values, Mapping):
            return ""
        text = _extract_response_text(dict(values)).strip().replace("\n", " ")
        if not text:
            return ""
        return text[:200] + ("…" if len(text) > 200 else "")

    @staticmethod
    def _available_models() -> list[tuple[str, str | None]]:
        """返回 (name, display_name) 列表，首个为默认模型；失败时返回空列表（不阻塞渠道）。"""
        try:
            from deerflow.config.app_config import get_app_config

            return [(m.name, getattr(m, "display_name", None)) for m in get_app_config().models]
        except Exception:
            logger.debug("list models failed", exc_info=True)
            return []

    def _handle_model_command(self, msg: InboundMessage, arg: str) -> str:
        """处理 /model 命令的通用文本逻辑（飞书无参数时由渠道层改发交互卡片）。"""
        models = self._available_models()
        default_name = models[0][0] if models else None

        if arg.lower() in ("clear", "default", "reset"):
            self.store.clear_model(msg.channel_name, msg.chat_id)
            return f"已恢复默认模型：{default_name or '（未配置）'}"

        if arg:
            target = next((name for name, _label in models if name.lower() == arg.lower()), None)
            if target is None:
                hint = "\n".join(f"• {name}（{label}）" if label and label != name else f"• {name}" for name, label in models) or "（未配置模型）"
                return f"无效的模型：{arg}\n可选模型：\n{hint}"
            # 选默认模型即清除覆盖（回落到 config 默认）
            if default_name is not None and target == default_name:
                self.store.clear_model(msg.channel_name, msg.chat_id)
            else:
                self.store.set_model(msg.channel_name, msg.chat_id, target)
            return f"模型已切换：{target}"

        # 无参数：文本式列出当前与全部可选模型
        current = self.store.get_model(msg.channel_name, msg.chat_id) or default_name
        lines = [f"当前模型：{current or '（未配置）'}", "\n可选模型："]
        for name, label in models:
            mark = " ✓" if name == current else ""
            suffix = f" — {label}" if label and label != name else ""
            lines.append(f"• {name}{mark}{suffix}")
        lines.append("\n用 /model <名称> 切换，/model default 恢复默认。")
        return "\n".join(lines)

    async def _fetch_gateway(self, path: str, kind: str) -> str:
        """Fetch data from the Gateway API for command responses."""
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self._gateway_url}{path}",
                    timeout=10,
                    headers=create_internal_auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Failed to fetch %s from gateway", kind)
            return f"Failed to fetch {kind} information."

        if kind == "models":
            names = [m["name"] for m in data.get("models", [])]
            return ("Available models:\n" + "\n".join(f"• {n}" for n in names)) if names else "No models configured."
        elif kind == "memory":
            return format_memory(data)
        return str(data)

    # -- error helper ------------------------------------------------------

    async def _send_error(self, msg: InboundMessage, error_text: str) -> None:
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=error_text,
            thread_ts=msg.thread_ts,
            metadata=_slim_metadata(msg.metadata),
        )
        await self.bus.publish_outbound(outbound)
