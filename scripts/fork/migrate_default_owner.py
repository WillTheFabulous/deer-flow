#!/usr/bin/env python3
"""把旧版单用户数据（user_id = "default"）归到 Web 账号（通常是管理员）名下。

旧 fork 没有登录体系，线程 / run / 记忆都记在 "default" 名下；升级后开启认证，
管理员在 Web 上看不到这些数据，飞书绑定到管理员后也接不上旧会话。本脚本一次性迁移：

1. 数据库：带 user_id 列的业务表里 user_id='default' 的行改为目标账号 id
   （跳过 users / user_preferences / personal_access_tokens 与 channel_* 表：它们归属真实账号）；
2. 文件：users/default/ 下的 threads/*、agents/* 与 memory.json 复制到目标账号目录，已存在的不覆盖。

用法（必须先停 gateway；默认只预览，加 --apply 才写入）：

    python3 scripts/fork/migrate_default_owner.py <账号 id 或邮箱> [--data-dir backend/.deer-flow] [--db PATH] [--apply]

写入前用 SQLite backup API 把数据库完整备份到 <data-dir>/backups/。只依赖标准库，宿主机直接运行。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LEGACY_USER_ID = "default"
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SKIPPED_TABLES = frozenset({"users", "user_preferences", "personal_access_tokens", "alembic_version"})
_SKIPPED_TABLE_PREFIXES = ("channel_", "sqlite_")
# users/default/ 下按子项逐个复制的目录
_COPIED_DIRS = ("threads", "agents")


class MigrationError(RuntimeError):
    pass


@dataclass
class MigrationPlan:
    user_id: str
    email: str
    role: str
    row_counts: dict[str, int] = field(default_factory=dict)
    copies: list[tuple[Path, Path]] = field(default_factory=list)
    existing: list[Path] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


def _connect(db_path: Path) -> sqlite3.Connection:
    # 自动提交模式，事务边界显式控制；timeout=0：库被占用时立即失败而不是等待
    return sqlite3.connect(db_path, timeout=0, isolation_level=None)


def _resolve_account(conn: sqlite3.Connection, ref: str) -> tuple[str, str, str]:
    row = conn.execute("SELECT id, email, system_role FROM users WHERE id = ? OR lower(email) = lower(?)", (ref, ref)).fetchone()
    if row is None:
        raise MigrationError(f"找不到账号：{ref}（先在 Web /setup 创建管理员）")
    user_id, email, role = row
    if user_id == LEGACY_USER_ID or not _SAFE_USER_ID_RE.fullmatch(user_id):
        raise MigrationError(f"账号 id 不能用作存储目录：{user_id!r}")
    return user_id, email, role


def _owned_tables(conn: sqlite3.Connection) -> list[str]:
    tables = [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    owned = []
    for table in tables:
        if table in _SKIPPED_TABLES or table.startswith(_SKIPPED_TABLE_PREFIXES):
            continue
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if "user_id" in columns:
            owned.append(table)
    return owned


def _plan_copies(data_dir: Path, user_id: str, plan: MigrationPlan) -> None:
    source_root = data_dir / "users" / LEGACY_USER_ID
    target_root = data_dir / "users" / user_id
    candidates: list[tuple[Path, Path]] = []
    for name in _COPIED_DIRS:
        source_dir = source_root / name
        if source_dir.is_dir():
            candidates.extend((child, target_root / name / child.name) for child in sorted(source_dir.iterdir()))
    memory = source_root / "memory.json"
    if memory.is_file():
        candidates.append((memory, target_root / "memory.json"))
    for source, target in candidates:
        if target.exists():
            plan.existing.append(target)
        else:
            plan.copies.append((source, target))


def build_plan(data_dir: Path, db_path: Path, account_ref: str) -> MigrationPlan:
    if not db_path.is_file():
        raise MigrationError(f"数据库不存在：{db_path}")
    conn = _connect(db_path)
    try:
        user_id, email, role = _resolve_account(conn, account_ref)
        plan = MigrationPlan(user_id=user_id, email=email, role=role)
        for table in _owned_tables(conn):
            (count,) = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE user_id = ?', (LEGACY_USER_ID,)).fetchone()
            if count:
                plan.row_counts[table] = count
    finally:
        conn.close()
    _plan_copies(data_dir, user_id, plan)
    return plan


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / db_path.name
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        # backup API 连同 WAL 里尚未落盘的内容一起得到一致快照
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _update_rows(db_path: Path, plan: MigrationPlan) -> None:
    conn = _connect(db_path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError(f"拿不到数据库写锁（gateway 还在运行？）：{exc}") from exc
        try:
            for table in plan.row_counts:
                conn.execute(f'UPDATE "{table}" SET user_id = ? WHERE user_id = ?', (plan.user_id, LEGACY_USER_ID))
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(f"更新数据库失败，已回滚：{exc}") from exc
    finally:
        conn.close()


def _copy_files(plan: MigrationPlan) -> None:
    for source, target in plan.copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def apply_plan(data_dir: Path, db_path: Path, plan: MigrationPlan) -> Path:
    """先备份数据库，再改库，最后复制文件；返回备份路径。"""
    backup = backup_database(db_path, data_dir / "backups" / f"migrate-default-{datetime.now():%Y%m%d-%H%M%S}")
    if plan.row_counts:
        _update_rows(db_path, plan)
    _copy_files(plan)
    return backup


def _describe(plan: MigrationPlan, db_path: Path, out: Callable[[str], None]) -> None:
    out(f"目标账号：{plan.email}（id={plan.user_id}，角色={plan.role}）")
    out(f"数据库：{db_path}")
    if plan.row_counts:
        for table, count in plan.row_counts.items():
            out(f"  {table}: {count} 行 user_id='{LEGACY_USER_ID}' → {plan.user_id}")
    else:
        out(f"  没有 user_id='{LEGACY_USER_ID}' 的行")
    out(f"复制文件：{len(plan.copies)} 项")
    for source, target in plan.copies:
        out(f"  {source} → {target}")
    for target in plan.existing:
        out(f"  跳过（目标已存在）：{target}")


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="把旧版 default 用户的数据归到指定 Web 账号名下（先停 gateway）。")
    parser.add_argument("account", help="目标账号 id 或邮箱")
    parser.add_argument("--data-dir", type=Path, default=Path("backend/.deer-flow"), help="DEER_FLOW_HOME（默认 backend/.deer-flow）")
    parser.add_argument("--db", type=Path, default=None, help="SQLite 数据库路径（默认 <data-dir>/data/deerflow.db）")
    parser.add_argument("--apply", action="store_true", help="实际写入；不加只预览")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    db_path: Path = args.db or data_dir / "data" / "deerflow.db"
    try:
        plan = build_plan(data_dir, db_path, args.account)
        _describe(plan, db_path, out)
        if not args.apply:
            out("预览模式，未做任何修改；确认 gateway 已停止后加 --apply 执行。")
            return 0
        backup = apply_plan(data_dir, db_path, plan)
    except MigrationError as exc:
        out(f"错误：{exc}")
        return 1
    out(f"数据库备份：{backup}")
    out(f"完成：更新 {plan.total_rows} 行，复制 {len(plan.copies)} 项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
