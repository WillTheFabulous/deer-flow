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
            self._save()
            return True

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
