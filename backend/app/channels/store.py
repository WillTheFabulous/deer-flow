"""ChannelStore — persists IM chat-to-DeerFlow thread mappings."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# upsert 会话时区分“未传入 title”与“传入空 title”的哨兵
_UNSET: Any = object()


class ChannelStore:
    """JSON-file-backed store that maps IM conversations to DeerFlow threads.

    Data layout (on disk)::

        {
            "<channel_name>:<chat_id>": {
                "thread_id": "<uuid>",
                "user_id": "<platform_user>",
                "created_at": 1700000000.0,
                "updated_at": 1700000000.0
            },
            ...
        }

    The store is intentionally simple — a single JSON file that is atomically
    rewritten on every mutation. For production workloads with high concurrency,
    this can be swapped for a proper database backend.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from deerflow.config.paths import get_paths

            path = Path(get_paths().base_dir) / "channels" / "store.json"
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = self._load()
        self._lock = threading.Lock()

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt channel store at %s, starting fresh", self._path)
        return {}

    def _save(self) -> None:
        fd = tempfile.NamedTemporaryFile(
            mode="w",
            dir=self._path.parent,
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(self._data, fd, indent=2)
            fd.close()
            Path(fd.name).replace(self._path)
        except BaseException:
            fd.close()
            Path(fd.name).unlink(missing_ok=True)
            raise

    # -- key helpers -------------------------------------------------------

    @staticmethod
    def _key(channel_name: str, chat_id: str, topic_id: str | None = None) -> str:
        if topic_id:
            return f"{channel_name}:{chat_id}:{topic_id}"
        return f"{channel_name}:{chat_id}"

    # -- public API --------------------------------------------------------

    def get_thread_id(self, channel_name: str, chat_id: str, topic_id: str | None = None) -> str | None:
        """Look up the DeerFlow thread_id for a given IM conversation/topic."""
        entry = self._data.get(self._key(channel_name, chat_id, topic_id))
        # entry 可能由 /repo 等先行创建而尚无 thread_id，必须用 get 安全读取
        return entry.get("thread_id") if entry else None

    def set_thread_id(
        self,
        channel_name: str,
        chat_id: str,
        thread_id: str,
        *,
        topic_id: str | None = None,
        user_id: str = "",
    ) -> None:
        """Create or update the mapping for an IM conversation/topic."""
        with self._lock:
            key = self._key(channel_name, chat_id, topic_id)
            now = time.time()
            # 合并而非整体重写：保留 workdir 等自定义扩展字段
            entry = dict(self._data.get(key) or {})
            entry.update(
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "created_at": entry.get("created_at", now),
                    "updated_at": now,
                }
            )
            self._data[key] = entry
            self._save()

    def remove(self, channel_name: str, chat_id: str, topic_id: str | None = None) -> bool:
        """Remove a mapping.

        If ``topic_id`` is provided, only that specific conversation/topic mapping is removed.
        If ``topic_id`` is omitted, all mappings whose key starts with
        ``"<channel_name>:<chat_id>"`` (including topic-specific ones) are removed.

        Returns True if at least one mapping was removed.
        """
        with self._lock:
            # Remove a specific conversation/topic mapping.
            if topic_id is not None:
                key = self._key(channel_name, chat_id, topic_id)
                if key in self._data:
                    del self._data[key]
                    self._save()
                    return True
                return False

            # Remove all mappings for this channel/chat_id (base and any topic-specific keys).
            prefix = self._key(channel_name, chat_id)
            keys_to_delete = [k for k in self._data if k == prefix or k.startswith(prefix + ":")]
            if not keys_to_delete:
                return False

            for k in keys_to_delete:
                del self._data[k]
            self._save()
            return True

    # -- workdir（会话工作目录）---------------------------------------------
    # 工作目录按「channel:chat_id」记忆（不区分 topic），历史为 MRU 列表。

    WORKDIR_HISTORY_LIMIT = 8

    def get_workdir(self, channel_name: str, chat_id: str) -> str | None:
        """获取该会话当前设置的工作目录（agent 视角虚拟路径）。"""
        entry = self._data.get(self._key(channel_name, chat_id))
        if entry:
            workdir = entry.get("workdir")
            if isinstance(workdir, str) and workdir.strip():
                return workdir
        return None

    def set_workdir(self, channel_name: str, chat_id: str, workdir: str) -> None:
        """设置会话工作目录，并把它推入 MRU 历史（去重，上限 WORKDIR_HISTORY_LIMIT）。"""
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
        """清除会话工作目录设置（保留历史）。返回是否确实清除了。"""
        with self._lock:
            key = self._key(channel_name, chat_id)
            entry = self._data.get(key)
            if not entry or "workdir" not in entry:
                return False
            del entry["workdir"]
            entry["updated_at"] = time.time()
            self._sync_session_setting_locked(entry, "workdir", None)
            self._save()
            return True

    def get_workdir_history(self, channel_name: str, chat_id: str) -> list[str]:
        """获取会话的工作目录 MRU 历史（最近使用在前）。"""
        entry = self._data.get(self._key(channel_name, chat_id))
        if entry:
            history = entry.get("workdir_history")
            if isinstance(history, list):
                return [h for h in history if isinstance(h, str)]
        return []

    # -- agent（会话人设/Agent）--------------------------------------------
    # 人设按「channel:chat_id」记忆（不区分 topic），由 /agent 菜单选择。
    # 存的是 assistant 名（如 "software-team"）或 "lead_agent"（通用助手）。

    def get_agent(self, channel_name: str, chat_id: str) -> str | None:
        """获取该会话当前选定的人设（assistant 名）；未设置返回 None。"""
        entry = self._data.get(self._key(channel_name, chat_id))
        if entry:
            agent = entry.get("agent")
            if isinstance(agent, str) and agent.strip():
                return agent
        return None

    def set_agent(self, channel_name: str, chat_id: str, agent: str) -> None:
        """设置会话人设（合并写，保留 thread_id / workdir 等扩展字段）。"""
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.update(
                {
                    "agent": agent,
                    "created_at": entry.get("created_at", now),
                    "updated_at": now,
                }
            )
            self._sync_session_setting_locked(entry, "agent", agent)
            self._data[key] = entry
            self._save()

    def clear_agent(self, channel_name: str, chat_id: str) -> bool:
        """清除会话人设设置（回落到 config 默认）。返回是否确实清除了。"""
        with self._lock:
            key = self._key(channel_name, chat_id)
            entry = self._data.get(key)
            if not entry or "agent" not in entry:
                return False
            del entry["agent"]
            entry["updated_at"] = time.time()
            self._sync_session_setting_locked(entry, "agent", None)
            self._save()
            return True

    # -- 会话模型覆盖（/model 或飞书模型卡选择，按 channel:chat_id 记忆）---------
    # 存的是模型 name（config.models[*].name）；未设置时运行时回落 config 默认模型。

    def get_model(self, channel_name: str, chat_id: str) -> str | None:
        """获取该会话当前选定的模型 name；未设置返回 None。"""
        entry = self._data.get(self._key(channel_name, chat_id))
        if entry:
            model = entry.get("model")
            if isinstance(model, str) and model.strip():
                return model
        return None

    def set_model(self, channel_name: str, chat_id: str, model: str) -> None:
        """设置会话模型（合并写，保留 thread_id / agent / workdir 等扩展字段）。"""
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.update(
                {
                    "model": model,
                    "created_at": entry.get("created_at", now),
                    "updated_at": now,
                }
            )
            self._sync_session_setting_locked(entry, "model", model)
            self._data[key] = entry
            self._save()

    def clear_model(self, channel_name: str, chat_id: str) -> bool:
        """清除会话模型设置（回落到 config 默认）。返回是否确实清除了。"""
        with self._lock:
            key = self._key(channel_name, chat_id)
            entry = self._data.get(key)
            if not entry or "model" not in entry:
                return False
            del entry["model"]
            entry["updated_at"] = time.time()
            self._sync_session_setting_locked(entry, "model", None)
            self._save()
            return True

    # -- 会话注册表（多会话：记录 / 命名 / 切换 / 删除 / 查看）-------------------
    # 每个「channel:chat_id」维护一组会话快照，key = DeerFlow thread_id：
    #   sessions[thread_id] = {title, agent, model, workdir, created_at, updated_at}
    # 现有 thread_id 字段仍为「当前会话」指针；agent/model/workdir 为「当前生效设置」。
    # 注册表按 chat 级（不区分 topic）维护，与 agent/model/workdir 作用域一致；
    # p2p 扁平化（topic_id=None）下即单条连续线程，正是主用例。

    SESSION_FIELDS = ("agent", "model", "workdir")
    # 每个 chat 的会话注册表上限：超过时裁剪最旧的、且非当前的会话（仅清本地注册项，不删 Gateway 线程）
    SESSION_LIMIT = 50

    @staticmethod
    def _session_snapshot_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """从 chat 级条目抽取当前 人设/模型/工作目录 作为会话快照。"""
        return {field: entry.get(field) for field in ChannelStore.SESSION_FIELDS}

    def _upsert_session_locked(self, entry: dict[str, Any], thread_id: str, now: float, *, title: Any = _UNSET) -> dict[str, Any]:
        """在已持锁前提下，确保 entry.sessions 内存在 thread_id 的会话（缺失则按当前设置补建）。

        返回写入后的会话快照（已挂回 entry["sessions"]）。
        """
        sessions = dict(entry.get("sessions") or {})
        existing = sessions.get(thread_id)
        if isinstance(existing, dict):
            session = dict(existing)
        else:
            session = {"title": None, "created_at": now, **self._session_snapshot_from_entry(entry)}
        if title is not _UNSET and title:
            session["title"] = title
        session["updated_at"] = now
        sessions[thread_id] = session
        entry["sessions"] = sessions
        return session

    def _sync_session_setting_locked(self, entry: dict[str, Any], field: str, value: str | None) -> None:
        """把 chat 级设置变更同步到「当前会话」的快照（当前会话存在时；不补建）。"""
        thread_id = entry.get("thread_id")
        src = entry.get("sessions")
        if not thread_id or not isinstance(src, dict) or thread_id not in src or not isinstance(src[thread_id], dict):
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
        """裁剪会话注册表：超过 SESSION_LIMIT 时丢弃最旧的、且非当前会话的注册项。

        仅清理本地注册项（不删 Gateway 线程）；当前会话（entry["thread_id"]）永不裁剪。
        """
        sessions = entry.get("sessions")
        if not isinstance(sessions, dict) or len(sessions) <= self.SESSION_LIMIT:
            return
        current = entry.get("thread_id")
        prunable = sorted(
            ((tid, snap) for tid, snap in sessions.items() if tid != current and isinstance(snap, dict)),
            key=lambda kv: kv[1].get("updated_at", 0.0),
        )
        excess = len(sessions) - self.SESSION_LIMIT
        for tid, _snap in prunable[:excess]:
            sessions.pop(tid, None)

    def add_session(self, channel_name: str, chat_id: str, thread_id: str) -> None:
        """注册一个会话，快照当前 chat 级 人设/模型/工作目录；已存在则仅刷新时间。"""
        if not thread_id:
            return
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.setdefault("created_at", now)
            entry["updated_at"] = now
            self._upsert_session_locked(entry, thread_id, now)
            self._prune_sessions_locked(entry)
            self._data[key] = entry
            self._save()

    def touch_session(self, channel_name: str, chat_id: str, thread_id: str) -> None:
        """刷新会话的 updated_at（缺失则按当前设置补建，兼容旧线程）。"""
        if not thread_id:
            return
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.setdefault("created_at", now)
            entry["updated_at"] = now
            self._upsert_session_locked(entry, thread_id, now)
            self._data[key] = entry
            self._save()

    def set_session_title(self, channel_name: str, chat_id: str, thread_id: str, title: str) -> None:
        """缓存会话标题（首轮对话后由总结模型生成；缺失则补建）。空标题忽略。"""
        if not thread_id or not title or not title.strip():
            return
        with self._lock:
            key = self._key(channel_name, chat_id)
            now = time.time()
            entry = dict(self._data.get(key) or {})
            entry.setdefault("created_at", now)
            entry["updated_at"] = now
            self._upsert_session_locked(entry, thread_id, now, title=title.strip())
            self._data[key] = entry
            self._save()

    def get_session(self, channel_name: str, chat_id: str, thread_id: str) -> dict[str, Any] | None:
        """返回某会话的快照（含 thread_id）；不存在返回 None。"""
        # 持锁读：飞书回调线程与 manager 的 asyncio 线程共用本 store，避免与并发改动竞争。
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            if not entry:
                return None
            sessions = entry.get("sessions")
            if isinstance(sessions, dict) and isinstance(sessions.get(thread_id), dict):
                return {"thread_id": thread_id, **sessions[thread_id]}
            return None

    def list_sessions(self, channel_name: str, chat_id: str) -> list[dict[str, Any]]:
        """列出该会话下全部会话快照（含 thread_id），按 updated_at 倒序。"""
        # 持锁读 + 迭代：否则并发 remove_session 的 del 会触发 "dictionary changed size during iteration"。
        with self._lock:
            entry = self._data.get(self._key(channel_name, chat_id))
            if not entry:
                return []
            sessions = entry.get("sessions")
            if not isinstance(sessions, dict):
                return []
            items = [{"thread_id": tid, **snap} for tid, snap in sessions.items() if isinstance(snap, dict)]
        items.sort(key=lambda s: s.get("updated_at", 0.0), reverse=True)
        return items

    def switch_session(self, channel_name: str, chat_id: str, thread_id: str) -> dict[str, Any] | None:
        """切换当前会话：设当前 thread_id 指针 + 从快照恢复 人设/模型/工作目录。

        返回恢复后的会话快照（含 thread_id）；目标会话不存在返回 None。
        """
        with self._lock:
            key = self._key(channel_name, chat_id)
            entry = self._data.get(key)
            if not entry:
                return None
            sessions = entry.get("sessions")
            if not isinstance(sessions, dict) or not isinstance(sessions.get(thread_id), dict):
                return None
            now = time.time()
            entry["thread_id"] = thread_id
            snapshot = dict(sessions[thread_id])
            # 恢复该会话的 人设/模型/工作目录（None/空 表示回落默认 → 删除覆盖）
            for field in self.SESSION_FIELDS:
                value = snapshot.get(field)
                if value:
                    entry[field] = value
                else:
                    entry.pop(field, None)
            snapshot["updated_at"] = now
            sessions[thread_id] = snapshot
            entry["updated_at"] = now
            self._save()
            return {"thread_id": thread_id, **snapshot}

    def remove_session(self, channel_name: str, chat_id: str, thread_id: str) -> dict[str, Any]:
        """从注册表移除某会话。

        若删除的是当前会话：切到剩余里最近使用的一个（并恢复其设置），
        没有则清空当前指针（下条消息会自动新建线程）。
        返回 {removed, was_current, new_current}。
        """
        with self._lock:
            key = self._key(channel_name, chat_id)
            entry = self._data.get(key)
            if not entry:
                return {"removed": False, "was_current": False, "new_current": None}
            sessions = entry.get("sessions")
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
                    for field in self.SESSION_FIELDS:
                        value = snap.get(field)
                        if value:
                            entry[field] = value
                        else:
                            entry.pop(field, None)
                else:
                    entry.pop("thread_id", None)
            entry["updated_at"] = time.time()
            self._save()
            return {"removed": True, "was_current": was_current, "new_current": new_current}

    # -- 用户与会话映射（机器人菜单事件只带 open_id，需要据此找回会话）-------

    def set_user_chat(self, channel_name: str, user_id: str, chat_id: str) -> None:
        """记录用户与其会话（如飞书 p2p chat）的映射。"""
        with self._lock:
            key = f"{channel_name}:user:{user_id}"
            entry = dict(self._data.get(key) or {})
            if entry.get("chat_id") == chat_id:
                return
            entry.update({"chat_id": chat_id, "updated_at": time.time()})
            self._data[key] = entry
            self._save()

    def get_user_chat(self, channel_name: str, user_id: str) -> str | None:
        """查询用户映射到的会话 chat_id。"""
        entry = self._data.get(f"{channel_name}:user:{user_id}")
        if entry:
            chat_id = entry.get("chat_id")
            if isinstance(chat_id, str) and chat_id:
                return chat_id
        return None

    def list_entries(self, channel_name: str | None = None) -> list[dict[str, Any]]:
        """List all stored mappings, optionally filtered by channel."""
        results = []
        for key, entry in self._data.items():
            parts = key.split(":", 2)
            ch = parts[0]
            chat = parts[1] if len(parts) > 1 else ""
            topic = parts[2] if len(parts) > 2 else None
            if channel_name and ch != channel_name:
                continue
            item: dict[str, Any] = {"channel_name": ch, "chat_id": chat, **entry}
            if topic is not None:
                item["topic_id"] = topic
            results.append(item)
        return results
