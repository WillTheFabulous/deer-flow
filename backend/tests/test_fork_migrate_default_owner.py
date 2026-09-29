"""scripts/fork/migrate_default_owner.py：旧版 default 用户数据归属迁移。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fork" / "migrate_default_owner.py"
_spec = importlib.util.spec_from_file_location("migrate_default_owner", _SCRIPT)
assert _spec is not None and _spec.loader is not None
migrate = importlib.util.module_from_spec(_spec)
# dataclass 解析注解时要在 sys.modules 里找到所属模块
sys.modules[_spec.name] = migrate
_spec.loader.exec_module(migrate)

ADMIN_ID = "0b8f5a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, system_role TEXT);
        CREATE TABLE threads_meta (thread_id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE agents (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, UNIQUE (user_id, name));
        CREATE TABLE personal_access_tokens (id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE channel_conversations (id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE checkpoints (thread_id TEXT, checkpoint BLOB);
        CREATE TABLE workspace_items (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, UNIQUE (user_id, name));
        INSERT INTO users VALUES ('{ADMIN_ID}', 'Admin@Example.com', 'admin');
        INSERT INTO threads_meta VALUES ('t1', 'default'), ('t2', 'default'), ('t3', 'someone');
        INSERT INTO runs VALUES ('r1', 'default');
        INSERT INTO agents VALUES ('a1', 'default', 'coder');
        INSERT INTO personal_access_tokens VALUES ('p1', 'default');
        INSERT INTO channel_conversations VALUES ('c1', 'default');
        INSERT INTO workspace_items VALUES ('w1', 'default', 'notes');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / ".deer-flow"
    (root / "data").mkdir(parents=True)
    _make_db(root / "data" / "deerflow.db")
    legacy = root / "users" / "default"
    (legacy / "threads" / "t1" / "user-data").mkdir(parents=True)
    (legacy / "threads" / "t1" / "user-data" / "note.txt").write_text("hello", encoding="utf-8")
    (legacy / "threads" / "t2").mkdir(parents=True)
    (legacy / "memory.json").write_text(json.dumps({"facts": [{"content": "喜欢 Python"}]}), encoding="utf-8")
    existing = root / "users" / ADMIN_ID / "threads" / "t2"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("admin", encoding="utf-8")
    return root


_KEY_COLUMNS = {"threads_meta": "thread_id", "runs": "run_id"}


def _owners(db: Path, table: str) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute(f"SELECT {_KEY_COLUMNS.get(table, 'id')}, user_id FROM {table}"))
    finally:
        conn.close()


def _run(argv: list[str]) -> tuple[int, str]:
    lines: list[str] = []
    return migrate.main(argv, out=lines.append), "\n".join(lines)


def test_preview_reports_the_plan_without_changes(data_dir: Path):
    code, output = _run(["admin@example.com", "--data-dir", str(data_dir)])

    assert code == 0
    assert "threads_meta: 2 行" in output and "runs: 1 行" in output and "agents: 1 行" in output and "workspace_items: 1 行" in output
    assert "personal_access_tokens" not in output and "channel_conversations" not in output
    assert "复制文件：2 项" in output and "跳过（目标已存在）" in output
    assert _owners(data_dir / "data" / "deerflow.db", "threads_meta")["t1"] == "default"
    assert not (data_dir / "users" / ADMIN_ID / "memory.json").exists()
    assert not (data_dir / "backups").exists()


def test_apply_moves_ownership_and_copies_files(data_dir: Path):
    db = data_dir / "data" / "deerflow.db"

    code, output = _run([ADMIN_ID, "--data-dir", str(data_dir), "--apply"])

    assert code == 0, output
    assert _owners(db, "threads_meta") == {"t1": ADMIN_ID, "t2": ADMIN_ID, "t3": "someone"}
    assert _owners(db, "runs") == {"r1": ADMIN_ID}
    assert _owners(db, "personal_access_tokens") == {"p1": "default"}
    assert _owners(db, "channel_conversations") == {"c1": "default"}

    admin_dir = data_dir / "users" / ADMIN_ID
    assert (admin_dir / "threads" / "t1" / "user-data" / "note.txt").read_text(encoding="utf-8") == "hello"
    assert (admin_dir / "threads" / "t2" / "keep.txt").read_text(encoding="utf-8") == "admin"
    assert json.loads((admin_dir / "memory.json").read_text(encoding="utf-8"))["facts"][0]["content"] == "喜欢 Python"
    assert (data_dir / "users" / "default" / "memory.json").exists()

    backups = list((data_dir / "backups").glob("migrate-default-*/deerflow.db"))
    assert len(backups) == 1 and _owners(backups[0], "threads_meta")["t1"] == "default"

    code, output = _run([ADMIN_ID, "--data-dir", str(data_dir)])
    assert code == 0 and "没有 user_id='default' 的行" in output and "复制文件：0 项" in output


def test_conflict_rolls_back_every_table(data_dir: Path):
    db = data_dir / "data" / "deerflow.db"
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO workspace_items VALUES ('w2', ?, 'notes')", (ADMIN_ID,))
    conn.commit()
    conn.close()

    code, output = _run([ADMIN_ID, "--data-dir", str(data_dir), "--apply"])

    assert code == 1 and "已回滚" in output
    assert _owners(db, "agents")["a1"] == "default"
    assert _owners(db, "threads_meta")["t1"] == "default"
    assert not (data_dir / "users" / ADMIN_ID / "memory.json").exists()


def test_unknown_account_is_rejected(data_dir: Path):
    code, output = _run(["nobody@example.com", "--data-dir", str(data_dir), "--apply"])
    assert code == 1 and "找不到账号" in output


def test_locked_database_aborts_before_changes(data_dir: Path):
    db = data_dir / "data" / "deerflow.db"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        code, output = _run([ADMIN_ID, "--data-dir", str(data_dir), "--apply"])
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert code == 1 and "写锁" in output
    assert _owners(db, "threads_meta")["t1"] == "default"
    assert not (data_dir / "users" / ADMIN_ID / "memory.json").exists()
