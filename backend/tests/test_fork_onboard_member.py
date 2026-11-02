"""scripts/fork/onboard_member.py：不经网页开通团队成员（建账号 + 飞书连接码）。

用 SQLite 临时库跑真实的用户库与连接码表：码要能被飞书那边的 ``consume_oauth_state`` 认出来。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fork" / "onboard_member.py"
_spec = importlib.util.spec_from_file_location("onboard_member", _SCRIPT)
assert _spec is not None and _spec.loader is not None
onboard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = onboard
_spec.loader.exec_module(onboard)


def _run(tmp_path: Path, body):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    async def go():
        await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'deerflow.db'}", sqlite_dir=str(tmp_path))
        try:
            return await body(get_session_factory())
        finally:
            await close_engine()

    return asyncio.run(go())


def _code(result: dict) -> str:
    command = result["command"]
    assert command.startswith("/connect ")
    return command.removeprefix("/connect ")


def test_add_creates_a_passwordless_user_and_a_working_code(tmp_path: Path):
    from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
    from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository

    async def body(sf):
        result = await onboard.add_member(sf, "  New.Member@Example.com ")
        user = await SQLiteUserRepository(sf).get_user_by_email("new.member@example.com")
        consumed = await ChannelConnectionRepository(sf).consume_oauth_state(provider="feishu", state=_code(result))
        replay = await ChannelConnectionRepository(sf).consume_oauth_state(provider="feishu", state=_code(result))
        return result, user, consumed, replay

    result, user, consumed, replay = _run(tmp_path, body)
    assert result["created"] is True
    assert user is not None and user.system_role == "user" and user.password_hash is None
    assert consumed is not None and consumed["owner_user_id"] == str(user.id)
    assert replay is None


def test_code_expires_after_ten_minutes(tmp_path: Path):
    from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository

    async def body(sf):
        result = await onboard.add_member(sf, "a@example.com")
        late = datetime.now(UTC) + timedelta(minutes=11)
        return result, await ChannelConnectionRepository(sf).consume_oauth_state(provider="feishu", state=_code(result), now=late)

    result, consumed = _run(tmp_path, body)
    assert timedelta(minutes=9) < result["expires_at"] - datetime.now(UTC) <= timedelta(minutes=10)
    assert consumed is None


def test_add_refuses_an_existing_email_and_points_to_code(tmp_path: Path):
    async def body(sf):
        await onboard.add_member(sf, "dup@example.com")
        with pytest.raises(onboard.MemberError, match="code"):
            await onboard.add_member(sf, "DUP@example.com")

    _run(tmp_path, body)


def test_code_for_unknown_email_is_refused(tmp_path: Path):
    async def body(sf):
        with pytest.raises(onboard.MemberError, match="add"):
            await onboard.issue_code(sf, "nobody@example.com")

    _run(tmp_path, body)


def test_sixth_pending_code_is_refused(tmp_path: Path):
    async def body(sf):
        await onboard.add_member(sf, "busy@example.com")
        for _ in range(4):
            await onboard.issue_code(sf, "busy@example.com")
        with pytest.raises(onboard.MemberError, match="5 个"):
            await onboard.issue_code(sf, "busy@example.com")

    _run(tmp_path, body)


def test_list_counts_only_live_feishu_bindings(tmp_path: Path):
    from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository

    async def body(sf):
        result = await onboard.add_member(sf, "bound@example.com")
        await onboard.add_member(sf, "fresh@example.com")
        repo = ChannelConnectionRepository(sf)
        await repo.upsert_connection(owner_user_id=result["user_id"], provider="feishu", external_account_id="ou_1", workspace_id="tenant")
        return await onboard.list_members(sf)

    rows = {r["email"]: r for r in _run(tmp_path, body)}
    assert rows["bound@example.com"]["feishu"] == 1
    assert rows["fresh@example.com"]["feishu"] == 0
    assert rows["fresh@example.com"]["web_login"] is False


def test_binding_switch_is_read_from_config():
    def config(enabled: bool, feishu: bool):
        return SimpleNamespace(channel_connections=SimpleNamespace(enabled=enabled, feishu=SimpleNamespace(enabled=feishu)))

    assert onboard.feishu_binding_enabled(config(True, True))
    assert not onboard.feishu_binding_enabled(config(True, False))
    assert not onboard.feishu_binding_enabled(config(False, True))
    assert not onboard.feishu_binding_enabled(SimpleNamespace())
