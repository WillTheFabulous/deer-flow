"""流式运行进度：plan mode 的 todos 与 task 子任务步骤。

- :class:`StreamProgressObserver`：挂在上游 ``ChannelManager._make_stream_observer`` 钩子上，
  逐流事件折叠 todos / 子任务状态，写进 outbound metadata 供飞书卡片渲染，
  子任务首次失败/超时时推送独立通知，run 结束时记录会话标题。
- :class:`LiveRunRegistry`：每线程的实时进度快照，供 ``/status`` 读取。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.channels.fork.manager import ForkChannelManager
    from app.channels.message_bus import InboundMessage

logger = logging.getLogger(__name__)

# outbound metadata 键：飞书卡片据此在正文上方渲染进度块
PROGRESS_TODOS_KEY = "fork_progress_todos"
PROGRESS_STEPS_KEY = "fork_progress_steps"
# 标记一条 outbound 为「独立通知」（如子任务失败）：渠道应另发新消息，不要 patch 运行中的卡片
NOTIFICATION_METADATA_KEY = "fork_notification"

CUSTOM_STREAM_MODE = "custom"

STATUS_ICONS = {
    "completed": "✅",
    "in_progress": "🔄",
    "running": "🔄",
    "pending": "⬜",
    "failed": "❌",
    "cancelled": "🚫",
}

# task 工具 custom 事件 type → 展示状态
_CUSTOM_EVENT_STATUS = {
    "task_started": "running",
    "task_running": "running",
    "task_completed": "completed",
    "task_failed": "failed",
    "task_timed_out": "failed",
    "task_cancelled": "cancelled",
}

_DEFAULT_STEP_DESCRIPTION = "子任务"
_ACTIVITY_MAX_CHARS = 200


def extract_todos(result: Any) -> list[dict[str, Any]] | None:
    """从 values 快照取出 plan mode 的 todos（write_todos 写入 ThreadState.todos）。"""
    if isinstance(result, Mapping):
        todos = result.get("todos")
        if isinstance(todos, list):
            return [t for t in todos if isinstance(t, dict)]
    return None


def extract_title(result: Any) -> str:
    """从 values 快照取出自动生成的会话标题（TitleMiddleware 写入 state.title）。"""
    if isinstance(result, Mapping):
        title = result.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return ""


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, Mapping) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, Mapping) and isinstance(block.get("text"), str)]
        return "".join(parts)
    return ""


def apply_custom_progress_event(steps: dict[str, dict[str, str]], data: Any) -> dict[str, str] | None:
    """把 task 工具的 custom 事件折叠进子任务进度。

    task_running 的消息文本记为「正在做什么」；某子任务首次进入失败/超时态时返回该步骤，
    供调用方推送通知（每个任务只返回一次）。
    """
    if not isinstance(data, Mapping):
        return None
    event_type = data.get("type")
    status = _CUSTOM_EVENT_STATUS.get(event_type) if isinstance(event_type, str) else None
    task_id = data.get("task_id")
    if status is None or not isinstance(task_id, str) or not task_id:
        return None

    description = data.get("description")
    step = steps.get(task_id)
    if step is None:
        step = {"description": description if isinstance(description, str) and description else _DEFAULT_STEP_DESCRIPTION, "status": status}
        steps[task_id] = step
    else:
        step["status"] = status
        if isinstance(description, str) and description and step.get("description") in (None, "", _DEFAULT_STEP_DESCRIPTION):
            step["description"] = description

    activity = _message_text(data.get("message")).strip()
    if activity:
        step["activity"] = activity[:_ACTIVITY_MAX_CHARS]

    if event_type in ("task_failed", "task_timed_out"):
        error_text = data.get("error")
        if isinstance(error_text, str) and error_text.strip():
            step["error"] = error_text.strip()
        if not step.get("_notified"):
            step["_notified"] = "1"
            return step
    return None


def steps_for_metadata(steps: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    return [{"description": s.get("description", ""), "status": s.get("status", ""), "activity": s.get("activity", "")} for s in steps.values()]


def render_progress_block(todos: Any, steps: Any) -> str:
    """把 todos + 正在执行的子任务渲染成 markdown（无进度返回空串）。"""
    lines: list[str] = []
    if isinstance(todos, list) and todos:
        lines.append("**任务进度**")
        for todo in todos:
            if not isinstance(todo, Mapping):
                continue
            content = str(todo.get("content", "")).strip()
            if content:
                lines.append(f"{STATUS_ICONS.get(str(todo.get('status', '')), '▫️')} {content}")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping) or step.get("status") != "running":
                continue
            description = str(step.get("description", "")).strip()
            if not description:
                continue
            line = f"🔧 正在执行：{description}"
            activity = str(step.get("activity", "")).strip()
            if activity:
                line += f"\n   ↳ {activity}"
            lines.append(line)
    return "\n".join(lines)


def format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{secs}秒"
    return f"{secs}秒"


class LiveRunRegistry:
    """每线程实时进度快照（key = DeerFlow thread_id），超过上限时淘汰最旧的已结束快照。"""

    def __init__(self, max_entries: int = 200) -> None:
        self._max_entries = max_entries
        self._runs: dict[str, dict[str, Any]] = {}

    def start(self, thread_id: str) -> None:
        """一个 run 首次收到流事件时重建为全新 running 快照，避免沿用上一轮的陈旧内容。"""
        now = time.time()
        self._runs[thread_id] = {"status": "running", "started_at": now, "updated_at": now, "todos": None, "steps": []}

    def update(self, thread_id: str, *, todos: list[dict[str, Any]] | None, steps: Mapping[str, Mapping[str, str]]) -> None:
        snapshot = self._runs.get(thread_id)
        if snapshot is None:
            return
        snapshot["updated_at"] = time.time()
        if todos is not None:
            snapshot["todos"] = todos
        if steps:
            snapshot["steps"] = steps_for_metadata(steps)

    def finish(self, thread_id: str) -> None:
        snapshot = self._runs.get(thread_id)
        if snapshot is not None:
            snapshot["status"] = "finished"
            snapshot["updated_at"] = time.time()
        if len(self._runs) > self._max_entries:
            finished = sorted(
                ((tid, snap) for tid, snap in self._runs.items() if snap.get("status") != "running"),
                key=lambda item: item[1].get("updated_at", 0.0),
            )
            for tid, _snap in finished[: len(self._runs) - self._max_entries]:
                self._runs.pop(tid, None)

    def get(self, thread_id: str | None) -> dict[str, Any] | None:
        return self._runs.get(thread_id) if thread_id else None

    @staticmethod
    def render(snapshot: Mapping[str, Any] | None) -> str:
        """渲染为 /status 用的 markdown；无快照返回空串。"""
        if not snapshot:
            return ""
        now = time.time()
        running = snapshot.get("status") == "running"
        lines = ["**🟢 正在执行中**" if running else "**✅ 本轮已完成**"]
        lines.append(f"已运行 {format_duration(now - (snapshot.get('started_at') or now))}，最近更新 {format_duration(now - (snapshot.get('updated_at') or now))}前")
        block = render_progress_block(snapshot.get("todos"), snapshot.get("steps"))
        if block:
            lines.extend(["", block])
        return "\n".join(lines)


class StreamProgressObserver:
    """一次流式 run 的进度观察者，由 ForkChannelManager._make_stream_observer 创建。

    所有方法都吞掉自身异常：进度只是锦上添花，绝不能打断上游的流式回复。
    """

    def __init__(self, manager: ForkChannelManager, msg: InboundMessage, thread_id: str) -> None:
        self._manager = manager
        self._msg = msg
        self._thread_id = thread_id
        self._todos: list[dict[str, Any]] | None = None
        self._steps: dict[str, dict[str, str]] = {}
        self._published_signature: str | None = None
        self._owns_snapshot = False

    def stream_modes(self, modes: list[str]) -> list[str]:
        return modes if CUSTOM_STREAM_MODE in modes else [*modes, CUSTOM_STREAM_MODE]

    def _signature(self) -> str:
        return repr((self._todos, [(tid, s.get("status"), s.get("activity")) for tid, s in self._steps.items()]))

    def _has_progress(self) -> bool:
        return self._todos is not None or bool(self._steps)

    async def on_event(self, event: str, data: Any) -> bool:
        """处理一个流事件；返回进度是否有尚未发布的变化。"""
        try:
            if not self._owns_snapshot:
                # 只有真正收到流事件的 run 才拥有快照：撞忙被拒的 run 在发事件前就报错，不会覆盖运行中的快照
                self._owns_snapshot = True
                self._manager.live_runs.start(self._thread_id)
            if event == "values":
                todos = extract_todos(data)
                if todos is not None:
                    self._todos = todos
            elif event == CUSTOM_STREAM_MODE:
                failed_step = apply_custom_progress_event(self._steps, data)
                if failed_step is not None:
                    await self._manager.publish_failure_notification(self._msg, self._thread_id, failed_step)
            self._manager.live_runs.update(self._thread_id, todos=self._todos, steps=self._steps)
            return self._has_progress() and self._signature() != self._published_signature
        except Exception:
            logger.debug("[Fork] progress observer failed on %s event", event, exc_info=True)
            return False

    def outbound_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """把当前进度写进即将发布的 outbound metadata，并记为已发布。"""
        try:
            if self._todos is not None:
                metadata[PROGRESS_TODOS_KEY] = self._todos
            if self._steps:
                metadata[PROGRESS_STEPS_KEY] = steps_for_metadata(self._steps)
            self._published_signature = self._signature()
        except Exception:
            logger.debug("[Fork] progress observer failed to attach metadata", exc_info=True)
        return metadata

    def finish(self, result: Any) -> None:
        """run 结束：收尾实时快照，并缓存会话标题 / 刷新会话活跃时间。"""
        try:
            final_todos = extract_todos(result)
            if final_todos is not None:
                self._todos = final_todos
            if self._owns_snapshot:
                self._manager.live_runs.update(self._thread_id, todos=self._todos, steps=self._steps)
                self._manager.live_runs.finish(self._thread_id)
            self._manager.record_session_activity(self._msg, self._thread_id, result)
        except Exception:
            logger.debug("[Fork] progress observer failed to finish", exc_info=True)
