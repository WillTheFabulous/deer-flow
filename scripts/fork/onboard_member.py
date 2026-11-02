"""Fork：不经网页开通团队成员（2026-10-31 起 deer-flow 前端默认不启动）。

在 gateway 容器里跑，直接连主库（与网关同一份 config.yaml）：

    docker exec -it deer-flow-gateway sh -c \\
      'cd /app/backend && PYTHONPATH=. .venv/bin/python /app/project/scripts/fork/onboard_member.py add --email x@y.com'

- ``add --email``：建普通账号（不设密码，只走飞书），接着给一个飞书连接码。
- ``code --email``：给已有账号再要一个连接码（码过期了，或要在某个群里绑定）。
- ``list``：列出所有账号和各自绑着的飞书会话数。

连接码与网页「设置 → 连接飞书」是同一套（``channel_connections._create_state``）：一次性、10 分钟有效、
每人最多 5 个没用掉的码。成员在机器人单聊里（群里就 @机器人）发 ``/connect <码>`` 就绑定上。
以后要给某人开网页登录：临时起前端，再用上游 ``python -m app.gateway.auth.reset_admin --email <邮箱>`` 设密码。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

PROVIDER = "feishu"


class MemberError(RuntimeError):
    """这一单做不了（账号已存在 / 不存在 / 码太多 / 飞书绑定没开）：退出码 1，原因给人看。"""


def feishu_binding_enabled(config: Any) -> bool:
    connections = getattr(config, "channel_connections", None)
    return bool(connections and connections.enabled and connections.feishu.enabled)


async def issue_code(session_factory: Any, email: str) -> dict[str, Any]:
    from fastapi import HTTPException

    from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
    from app.gateway.routers.channel_connections import _STATE_TTL_SECONDS, _create_state
    from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository

    user = await SQLiteUserRepository(session_factory).get_user_by_email(email)
    if user is None:
        raise MemberError(f"没有账号 {email.strip()}：新成员用 add")
    try:
        code = await _create_state(ChannelConnectionRepository(session_factory), owner_user_id=str(user.id), provider=PROVIDER)
    except HTTPException as exc:
        if exc.status_code == 429:
            raise MemberError(f"{user.email} 还有 5 个没用掉的连接码：等 10 分钟过期，或让他先用掉其中一个") from exc
        raise
    return {
        "email": user.email,
        "user_id": str(user.id),
        "command": f"/connect {code}",
        "expires_at": datetime.now(UTC) + timedelta(seconds=_STATE_TTL_SECONDS),
    }


async def add_member(session_factory: Any, email: str) -> dict[str, Any]:
    from app.gateway.auth.local_provider import LocalAuthProvider
    from app.gateway.auth.repositories.sqlite import SQLiteUserRepository

    email = email.strip()
    if "@" not in email:
        raise MemberError(f"邮箱不对：{email}")
    repo = SQLiteUserRepository(session_factory)
    if await repo.get_user_by_email(email) is not None:
        raise MemberError(f"{email} 已经有账号：要连接码用 code")
    user = await LocalAuthProvider(repository=repo).create_user(email=email, password=None, system_role="user")
    return {"created": True, **await issue_code(session_factory, user.email)}


async def list_members(session_factory: Any) -> list[dict[str, Any]]:
    from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
    from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository

    users = SQLiteUserRepository(session_factory)
    connections = ChannelConnectionRepository(session_factory)
    rows = []
    for user_id in await users.list_user_ids():
        user = await users.get_user_by_id(user_id)
        if user is None:
            continue
        bound = [c for c in await connections.list_connections(str(user.id)) if c["provider"] == PROVIDER and c["status"] == "connected"]
        rows.append({"email": user.email, "role": user.system_role, "feishu": len(bound), "web_login": bool(user.password_hash)})
    return sorted(rows, key=lambda r: r["email"])


def _print_code(result: dict[str, Any]) -> None:
    local = result["expires_at"].astimezone().strftime("%H:%M")
    if result.get("created"):
        print(f"已建账号：{result['email']}（不设密码，只走飞书）")
    print("让他在 10 分钟内把下面这一句发给机器人（单聊；要在群里用就在群里 @机器人 发）：")
    print(f"  {result['command']}")
    print(f"有效到 {local}，一次性。")


async def _run(args: argparse.Namespace) -> int:
    from deerflow.config import get_app_config
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config

    config = get_app_config()
    if args.cmd in ("add", "code") and not feishu_binding_enabled(config):
        print("config.yaml 的 channel_connections（或其中的 feishu）没开，发不了连接码", file=sys.stderr)
        return 1
    await init_engine_from_config(config.database)
    try:
        session_factory = get_session_factory()
        if session_factory is None:
            print("主库没配（database.backend 是 memory），开通不了", file=sys.stderr)
            return 1
        if args.cmd == "list":
            for row in await list_members(session_factory):
                web = "可网页登录" if row["web_login"] else "只走飞书"
                print(f"{row['email']:40s} {row['role']:6s} 飞书绑定 {row['feishu']}  {web}")
            return 0
        fn = add_member if args.cmd == "add" else issue_code
        _print_code(await fn(session_factory, args.email))
        return 0
    except MemberError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await close_engine()


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="不经网页开通团队成员：建账号、发飞书连接码")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add", help="建普通账号并给一个飞书连接码").add_argument("--email", required=True)
    sub.add_parser("code", help="给已有账号再要一个飞书连接码").add_argument("--email", required=True)
    sub.add_parser("list", help="列出账号和飞书绑定数")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
