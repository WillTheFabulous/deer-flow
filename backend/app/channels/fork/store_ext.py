"""ChannelStore 的 fork 扩展字段：工作目录 / 人设 / 模型 / 多会话注册表 / open_id→会话映射。

全部按「channel:chat_id」（不区分 topic）记忆，与 ChannelStore 共用同一个 JSON 文件，
因此磁盘格式与旧 fork 完全兼容。上游约定所有对 ``_data`` 的访问都必须持 ``_lock``，这里同样遵守。
"""

from __future__ import annotations

import threading
import time
from typing import Any

# upsert 会话时区分「未传入 title」与「传入空 title」的哨兵
_UNSET: Any = object()


class ChannelStoreExtensionsMixin:
    """由 ``ChannelStore`` 继承；依赖其 ``_data`` / ``_lock`` / ``_key`` / ``_save``。"""

    _data: dict[str, dict[str, Any]]
    _lock: threading.Lock

    WORKDIR_HISTORY_LIMIT = 8
    # 会话快照随会话保存的 chat 级设置；切换会话时一并恢复
    SESSION_FIELDS = ("agent", "model", "workdir")
    # 每个 chat 的会话注册表上限：超过时裁剪最旧的非当前会话（只清本地注册项，不删 Gateway 线程）
    SESSION_LIMIT = 50

    # -- 通用 chat 级字段读写 -------------------------------------------------

    def _get_chat_str(self, channel_name: str, chat_id: str, field: str) -> str | None:
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            value = entry.get(field) if entry else None
        return value if isinstance(value, str) and value.strip() else None

    def _set_chat_field(self, channel_name: str, chat_id: str, field: str, value: str) -> None:
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.update({field: value, "created_at": entry.get("created_at", now), "updated_at": now})
            self._sync_session_setting_locked(entry, field, value)
            self._data[key] = entry
            self._save()

    def _clear_chat_field(self, channel_name: str, chat_id: str, field: str) -> bool:
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            if not entry or field not in entry:
                return False
            del entry[field]
            entry["updated_at"] = time.time()
            self._sync_session_setting_locked(entry, field, None)
            self._save()
            return True

    # -- workdir（会话工作目录，/repo）-----------------------------------------

    def get_workdir(self, channel_name: str, chat_id: str) -> str | None:
        """当前工作目录（agent 视角的虚拟路径）；未设置返回 None。"""
        return self._get_chat_str(channel_name, chat_id, "workdir")

    def set_workdir(self, channel_name: str, chat_id: str, workdir: str) -> None:
        """设置工作目录，并推入 MRU 历史（去重，上限 WORKDIR_HISTORY_LIMIT）。"""
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            history = [h for h in entry.get("workdir_history", []) if isinstance(h, str) and h != workdir]
            history.insert(0, workdir)
            entry.update(
                {
                    "workdir": workdir,
                    "workdir_history": history[: self.WORKDIR_HISTORY_LIMIT],
                    "created_at": entry.get("created_at", now),
                    "updated_at": now,
                }
            )
            self._sync_session_setting_locked(entry, "workdir", workdir)
            self._data[key] = entry
            self._save()

    def clear_workdir(self, channel_name: str, chat_id: str) -> bool:
        """清除工作目录设置（保留历史）。返回是否确实清除了。"""
        return self._clear_chat_field(channel_name, chat_id, "workdir")

    def get_workdir_history(self, channel_name: str, chat_id: str) -> list[str]:
        """工作目录 MRU 历史（最近使用在前）。"""
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            history = entry.get("workdir_history") if entry else None
            return [h for h in history if isinstance(h, str)] if isinstance(history, list) else []

    # -- agent（会话人设的展示缓存；真正生效的是线程上钉住的 agent）-------------

    def get_agent(self, channel_name: str, chat_id: str) -> str | None:
        return self._get_chat_str(channel_name, chat_id, "agent")

    def set_agent(self, channel_name: str, chat_id: str, agent: str) -> None:
        self._set_chat_field(channel_name, chat_id, "agent", agent)

    def clear_agent(self, channel_name: str, chat_id: str) -> bool:
        return self._clear_chat_field(channel_name, chat_id, "agent")

    # -- model（会话模型覆盖，/model）-------------------------------------------

    def get_model(self, channel_name: str, chat_id: str) -> str | None:
        return self._get_chat_str(channel_name, chat_id, "model")

    def set_model(self, channel_name: str, chat_id: str, model: str) -> None:
        self._set_chat_field(channel_name, chat_id, "model", model)

    def clear_model(self, channel_name: str, chat_id: str) -> bool:
        return self._clear_chat_field(channel_name, chat_id, "model")

    # -- 会话注册表（/sessions：记录 / 命名 / 切换 / 删除 / 查看）----------------
    # entry["sessions"][thread_id] = {title, agent, model, workdir, created_at, updated_at}
    # entry["thread_id"] 仍是「当前会话」指针；agent/model/workdir 是「当前生效设置」。

    @classmethod
    def _session_snapshot_from_entry(cls, entry: dict[str, Any]) -> dict[str, Any]:
        return {field: entry.get(field) for field in cls.SESSION_FIELDS}

    def _upsert_session_locked(self, entry: dict[str, Any], thread_id: str, now: float, *, title: Any = _UNSET) -> dict[str, Any]:
        """持锁前提下确保 entry.sessions 里有 thread_id 的会话（缺失则按当前设置补建）。"""
        sessions = dict(entry.get("sessions") or {})
        existing = sessions.get(thread_id)
        session = dict(existing) if isinstance(existing, dict) else {"title": None, "created_at": now, **self._session_snapshot_from_entry(entry)}
        if title is not _UNSET and title:
            session["title"] = title
        session["updated_at"] = now
        sessions[thread_id] = session
        entry["sessions"] = sessions
        return session

    def _sync_session_setting_locked(self, entry: dict[str, Any], field: str, value: str | None) -> None:
        """把 chat 级设置变更同步到「当前会话」快照（当前会话已注册时；不补建）。"""
        thread_id = entry.get("thread_id")
        src = entry.get("sessions")
        if not thread_id or not isinstance(src, dict) or not isinstance(src.get(thread_id), dict):
            return
        sessions = dict(src)
        session = dict(sessions[thread_id])
        if value:
            session[field] = value
        else:
            session.pop(field, None)
        session["updated_at"] = time.time()
        sessions[thread_id] = session
        entry["sessions"] = sessions

    def _prune_sessions_locked(self, entry: dict[str, Any]) -> None:
        """超过 SESSION_LIMIT 时丢弃最旧的非当前会话注册项。"""
        sessions = entry.get("sessions")
        if not isinstance(sessions, dict) or len(sessions) <= self.SESSION_LIMIT:
            return
        current = entry.get("thread_id")
        prunable = sorted(
            ((tid, snap) for tid, snap in sessions.items() if tid != current and isinstance(snap, dict)),
            key=lambda kv: kv[1].get("updated_at", 0.0),
        )
        for tid, _snap in prunable[: len(sessions) - self.SESSION_LIMIT]:
            sessions.pop(tid, None)

    def _mutate_session(self, channel_name: str, chat_id: str, thread_id: str, *, title: Any = _UNSET, prune: bool = False) -> None:
        if not thread_id:
            return
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.setdefault("created_at", now)
            entry["updated_at"] = now
            self._upsert_session_locked(entry, thread_id, now, title=title)
            if prune:
                self._prune_sessions_locked(entry)
            self._data[key] = entry
            self._save()

    def add_session(self, channel_name: str, chat_id: str, thread_id: str) -> None:
        """注册会话并快照当前 人设/模型/工作目录；已存在则只刷新时间。"""
        self._mutate_session(channel_name, chat_id, thread_id, prune=True)

    def touch_session(self, channel_name: str, chat_id: str, thread_id: str) -> None:
        """刷新会话活跃时间（缺失则按当前设置补建，兼容旧线程）。"""
        self._mutate_session(channel_name, chat_id, thread_id)

    def set_session_title(self, channel_name: str, chat_id: str, thread_id: str, title: str) -> None:
        """缓存会话标题（空标题忽略）。"""
        if title and title.strip():
            self._mutate_session(channel_name, chat_id, thread_id, title=title.strip())

    def get_session(self, channel_name: str, chat_id: str, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            sessions = entry.get("sessions") if entry else None
            if isinstance(sessions, dict) and isinstance(sessions.get(thread_id), dict):
                return {"thread_id": thread_id, **sessions[thread_id]}
            return None

    def list_sessions(self, channel_name: str, chat_id: str) -> list[dict[str, Any]]:
        """全部会话快照（含 thread_id），按 updated_at 倒序。"""
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            sessions = entry.get("sessions") if entry else None
            if not isinstance(sessions, dict):
                return []
            items = [{"thread_id": tid, **snap} for tid, snap in sessions.items() if isinstance(snap, dict)]
        items.sort(key=lambda s: s.get("updated_at", 0.0), reverse=True)
        return items

    def _restore_session_settings_locked(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        for field in self.SESSION_FIELDS:
            value = snapshot.get(field)
            if value:
                entry[field] = value
            else:
                entry.pop(field, None)

    def switch_session(self, channel_name: str, chat_id: str, thread_id: str) -> dict[str, Any] | None:
        """切换当前会话指针并恢复该会话的 人设/模型/工作目录；目标不存在返回 None。"""
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            sessions = entry.get("sessions") if entry else None
            if not isinstance(sessions, dict) or not isinstance(sessions.get(thread_id), dict):
                return None
            now = time.time()
            entry["thread_id"] = thread_id
            snapshot = dict(sessions[thread_id])
            self._restore_session_settings_locked(entry, snapshot)
            snapshot["updated_at"] = now
            sessions[thread_id] = snapshot
            entry["updated_at"] = now
            self._save()
            return {"thread_id": thread_id, **snapshot}

    def remove_session(self, channel_name: str, chat_id: str, thread_id: str) -> dict[str, Any]:
        """移除会话；删的是当前会话时切到剩余最近的一个，没有则清空指针。

        返回 {removed, was_current, new_current}。
        """
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            sessions = entry.get("sessions") if entry else None
            if not isinstance(sessions, dict) or thread_id not in sessions:
                return {"removed": False, "was_current": False, "new_current": None}

            del sessions[thread_id]
            was_current = entry.get("thread_id") == thread_id
            new_current: str | None = None
            if was_current:
                remaining = sorted(
                    ((tid, snap) for tid, snap in sessions.items() if isinstance(snap, dict)),
                    key=lambda kv: kv[1].get("updated_at", 0.0),
                    reverse=True,
                )
                if remaining:
                    new_current, snap = remaining[0]
                    entry["thread_id"] = new_current
                    self._restore_session_settings_locked(entry, snap)
                else:
                    entry.pop("thread_id", None)
            entry["updated_at"] = time.time()
            self._save()
            return {"removed": True, "was_current": was_current, "new_current": new_current}

    # -- open_id → 单聊 chat_id 映射（飞书机器人菜单事件只带 open_id）------------

    def set_user_chat(self, channel_name: str, user_id: str, chat_id: str) -> None:
        with self._lock:
            key = f"{channel_name}:user:{user_id}"
            entry = dict(self._data.get(key) or {})
            if entry.get("chat_id") == chat_id:
                return
            entry.update({"chat_id": chat_id, "updated_at": time.time()})
            self._data[key] = entry
            self._save()

    def get_user_chat(self, channel_name: str, user_id: str) -> str | None:
        with self._lock:
            entry = self._data.get(f"{channel_name}:user:{user_id}")
            chat_id = entry.get("chat_id") if entry else None
        return chat_id if isinstance(chat_id, str) and chat_id else None
