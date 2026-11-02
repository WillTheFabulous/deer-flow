"""scripts/fork/migrate_sqlite_to_postgres.py：DeerFlow 主库从 SQLite 整库迁到 Postgres。

Postgres 用例要设 DEERFLOW_TEST_POSTGRES_URL（只能指向测试库），没设就跳过；每个用例用独立 schema，结束时删掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, TypedDict

import pytest
import sqlalchemy as sa
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fork" / "migrate_sqlite_to_postgres.py"
_spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", _SCRIPT)
assert _spec is not None and _spec.loader is not None
migrate = importlib.util.module_from_spec(_spec)
# dataclass 解析注解时要在 sys.modules 里找到所属模块
sys.modules[_spec.name] = migrate
_spec.loader.exec_module(migrate)

POSTGRES_URL = os.getenv("DEERFLOW_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="set DEERFLOW_TEST_POSTGRES_URL to run the live PostgreSQL migration tests")

TARGET_ENV = "DEERFLOW_MIGRATE_TEST_TARGET"
USER_ID = "0b8f5a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
MAIN_THREAD = "thread-main"
SIDE_THREAD = "thread-side"
CREATED = datetime(2026, 10, 28, 6, 23, 13, 834051, tzinfo=UTC)


class _State(TypedDict):
    messages: Annotated[list, add_messages]
    title: str | None


def _graph(checkpointer):
    def respond(state: _State) -> dict:
        return {"messages": [AIMessage(content=f"第 {len(state['messages'])} 条回复")], "title": "测试会话"}

    builder = StateGraph(_State)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)


def _insert_rows(path: Path) -> None:
    from deerflow.persistence.base import Base

    tables = Base.metadata.tables
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(
            tables["users"].insert(),
            {"id": USER_ID, "email": "admin@example.com", "password_hash": "hash", "system_role": "admin", "created_at": CREATED, "needs_setup": False, "token_version": 3},
        )
        conn.execute(
            tables["user_preferences"].insert(),
            [
                {"user_id": USER_ID, "key": "theme", "value": {"mode": "暗色"}},
                {"user_id": USER_ID, "key": "json-null", "value": sa.JSON.NULL},
                {"user_id": USER_ID, "key": "sql-null", "value": sa.null()},
            ],
        )
        conn.execute(
            tables["threads_meta"].insert(),
            {"thread_id": MAIN_THREAD, "user_id": USER_ID, "assistant_id": "lead_agent", "display_name": "中文会话", "status": "idle", "metadata_json": {"agent": "pod", "n": [1, 2]}, "created_at": CREATED, "updated_at": CREATED},
        )
        conn.execute(
            tables["runs"].insert(),
            {
                "run_id": "run-1",
                "thread_id": MAIN_THREAD,
                "user_id": USER_ID,
                "status": "success",
                "metadata_json": {},
                "kwargs_json": {"input": "你好"},
                "token_usage_by_model": {"m": {"input": 3}},
                "total_tokens": 5,
                "change_seq": 2,
                "created_at": CREATED,
                "updated_at": CREATED,
            },
        )
        conn.execute(tables["run_change_clock"].insert(), {"id": 1, "value": 2})
        conn.execute(
            tables["run_events"].insert(),
            [{"thread_id": MAIN_THREAD, "run_id": "run-1", "user_id": USER_ID, "event_type": "message", "category": "message", "content": f"事件 {i}", "event_metadata": {"i": i}, "seq": i, "created_at": CREATED} for i in range(1, 4)],
        )
        conn.execute(
            tables["channel_connections"].insert(),
            {"id": "conn-1", "owner_user_id": USER_ID, "provider": "feishu", "external_account_id": "ou_x", "scopes_json": [], "capabilities_json": {}, "metadata_json": {}, "created_at": CREATED, "updated_at": CREATED},
        )
        conn.execute(
            tables["channel_conversations"].insert(),
            {"id": "conv-1", "connection_id": "conn-1", "owner_user_id": USER_ID, "provider": "feishu", "external_conversation_id": "oc_x", "thread_id": MAIN_THREAD, "created_at": CREATED, "updated_at": CREATED},
        )
    engine.dispose()


async def _build_source(path: Path) -> None:
    from langgraph.store.sqlite.aio import AsyncSqliteStore

    from deerflow.persistence.engine import close_engine, init_engine

    try:
        await init_engine("sqlite", url=f"sqlite+aiosqlite:///{path}", sqlite_dir=str(path.parent))
    finally:
        await close_engine()
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        graph = _graph(saver)
        for thread_id in (MAIN_THREAD, SIDE_THREAD):
            config = {"configurable": {"thread_id": thread_id}}
            await graph.ainvoke({"messages": [HumanMessage(content="你好")]}, config)
            await graph.ainvoke({"messages": [HumanMessage(content="再来一条")]}, config)
        latest = await saver.aget_tuple({"configurable": {"thread_id": MAIN_THREAD, "checkpoint_ns": ""}})
        await saver.aput_writes(latest.config, [("__error__", "boom"), ("messages", [AIMessage(content="没做完")])], task_id="task-pending")
    async with AsyncSqliteStore.from_conn_string(str(path)) as store:
        await store.setup()
    _insert_rows(path)


@pytest.fixture(scope="module")
def template_db(tmp_path_factory) -> Path:
    # 建库要跑一遍 alembic bootstrap（几秒），整个模块共用一份模板
    path = tmp_path_factory.mktemp("template") / "deerflow.db"
    asyncio.run(_build_source(path))
    return path


@pytest.fixture
def source_db(template_db: Path, tmp_path: Path) -> Path:
    path = tmp_path / "data" / "deerflow.db"
    path.parent.mkdir()
    with sqlite3.connect(template_db) as reader, sqlite3.connect(path) as writer:
        reader.backup(writer)
    return path


@pytest.fixture
def pg_schema():
    schema = f"mig_test_{uuid.uuid4().hex[:10]}"
    yield schema
    engine = sa.create_engine(migrate.database_config(POSTGRES_URL, "").app_sync_sqlalchemy_url)
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _apply_args(source_db: Path, snapshots: Path, schema: str) -> list[str]:
    return ["--sqlite", str(source_db), "--target-env", TARGET_ENV, "--schema", schema, "--snapshot-dir", str(snapshots), "--apply"]


# ---------------------------------------------------------------------------
# 离线
# ---------------------------------------------------------------------------


def test_to_utc_treats_naive_values_as_utc():
    assert migrate.to_utc("2026-10-28 06:23:13.834051") == CREATED
    assert migrate.to_utc("2026-10-28T14:23:13.834051+08:00") == CREATED
    assert migrate.to_utc("2026-10-28T06:23:13.834051Z") == CREATED
    assert migrate.to_utc("2026-10-28 14:23:13.834051+08:00").tzinfo is UTC
    assert migrate.to_utc(None) is None


def test_json_param_keeps_sql_null_apart_from_json_null():
    assert isinstance(migrate.json_param(None), sa.sql.elements.Null)
    assert migrate.json_param("null") is sa.JSON.NULL
    assert migrate.json_param('{"a": [1, "中"]}') == {"a": [1, "中"]}
    # SQLite 的 JSON 列是 NUMERIC 亲和性，纯数字 JSON 读出来就是数字
    assert migrate.json_param(5) == 5


def test_orm_tables_list_parents_before_children():
    names = [table.name for table in migrate.orm_tables()]
    assert names.index("users") < names.index("user_preferences")
    assert names.index("channel_connections") < names.index("channel_conversations")
    assert names.index("subagent_batches") < names.index("subagent_batch_items")


def test_diff_checkpoint_only_forgives_the_tasks_channel_postgres_adds_to_old_checkpoints():
    serde = JsonPlusSerializer()
    expected = {"v": 2, "id": "c2", "channel_values": {"title": "t"}, "channel_versions": {"title": "1"}, "pending_sends": []}
    actual = SimpleNamespace(
        checkpoint={**expected, "channel_values": {"title": "t", "__pregel_tasks": ["send"]}, "channel_versions": {"title": "1", "__pregel_tasks": "2"}},
        metadata={"step": 1},
        parent_config={"configurable": {"checkpoint_id": "c1"}},
    )
    assert migrate.diff_checkpoint(expected, {"step": 1, "writes": {"x": 1}}, "c1", actual, serde) == []
    assert migrate.diff_checkpoint(expected, {"step": 2}, "c1", actual, serde) == ["metadata 不同"]
    # 新格式检查点或没有父检查点时，多出的通道就是差异
    assert migrate.diff_checkpoint({**expected, "v": 4}, {"step": 1}, "c1", actual, serde)
    assert migrate.diff_checkpoint(expected, {"step": 1}, None, actual, serde)


def test_validate_source_accepts_a_database_at_head(source_db: Path):
    info = migrate.inspect_source(source_db)
    migrate.validate_source(info, migrate.orm_tables(), migrate.head_revision())
    assert info.checkpoint_threads == 2
    assert info.counts["checkpoints"] > 0 and info.counts["writes"] > 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("UPDATE alembic_version SET version_num = '0001_baseline'", "alembic 版本"),
        ("CREATE TABLE extra_things (id INTEGER)", "不认识的表"),
        ("ALTER TABLE users ADD COLUMN nickname TEXT", "ORM 里没有的列"),
        ("INSERT INTO store (prefix, key, value) VALUES ('p', 'k', '{}')", "Store"),
        ("DROP TABLE writes", "检查点表 writes"),
    ],
)
def test_validate_source_refuses_what_it_cannot_carry(source_db: Path, mutation: str, message: str):
    with sqlite3.connect(source_db) as conn:
        conn.execute(mutation)
    with pytest.raises(migrate.MigrationError, match=message):
        migrate.validate_source(migrate.inspect_source(source_db), migrate.orm_tables(), migrate.head_revision())


def test_preview_writes_nothing(source_db: Path, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv(TARGET_ENV, raising=False)
    before = _digest(source_db)
    snapshots = tmp_path / "snapshots"
    assert migrate.main(["--sqlite", str(source_db), "--target-env", TARGET_ENV, "--snapshot-dir", str(snapshots)]) == 0
    assert _digest(source_db) == before
    assert not snapshots.exists()
    assert "预览结束" in capsys.readouterr().out


def test_apply_refuses_without_a_target_url(source_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.delenv(TARGET_ENV, raising=False)
    snapshots = tmp_path / "snapshots"
    assert migrate.main(_apply_args(source_db, snapshots, "")) == 2
    assert not snapshots.exists()


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


async def _assert_same_conversations(source_db: Path, schema: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncSqliteSaver.from_conn_string(str(source_db)) as old_saver, AsyncPostgresSaver.from_conn_string(migrate.psycopg_dsn(POSTGRES_URL, schema)) as new_saver:
        old_graph, new_graph = _graph(old_saver), _graph(new_saver)
        for thread_id in (MAIN_THREAD, SIDE_THREAD):
            config = {"configurable": {"thread_id": thread_id}}
            old_state, new_state = await old_graph.aget_state(config), await new_graph.aget_state(config)
            assert new_state.values == old_state.values
            assert new_state.config["configurable"]["checkpoint_id"] == old_state.config["configurable"]["checkpoint_id"]
            old_history = [snapshot.config["configurable"]["checkpoint_id"] async for snapshot in old_graph.aget_state_history(config)]
            new_history = [snapshot.config["configurable"]["checkpoint_id"] async for snapshot in new_graph.aget_state_history(config)]
            assert new_history == old_history

        latest = await new_saver.aget_tuple({"configurable": {"thread_id": MAIN_THREAD, "checkpoint_ns": ""}})
        assert ("task-pending", "__error__", "boom") in latest.pending_writes

        # 迁过去的会话能接着聊
        result = await new_graph.ainvoke({"messages": [HumanMessage(content="迁移后继续")]}, {"configurable": {"thread_id": SIDE_THREAD}})
        assert [message.content for message in result["messages"]][-2:] == ["迁移后继续", "第 5 条回复"]


def _assert_rows(schema: str) -> None:
    engine = sa.create_engine(migrate.database_config(POSTGRES_URL, schema).app_sync_sqlalchemy_url)
    try:
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT created_at FROM users")).scalar_one() == CREATED
            assert dict(conn.execute(sa.text("SELECT key, value IS NULL FROM user_preferences")).all()) == {"theme": False, "json-null": False, "sql-null": True}
            assert conn.execute(sa.text("SELECT display_name FROM threads_meta")).scalar_one() == "中文会话"
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == migrate.head_revision()
            new_id = conn.execute(sa.text("INSERT INTO run_events (thread_id, run_id, event_type, category, content, event_metadata, seq, created_at) VALUES ('t', 'r', 'message', 'message', '', '{}', 9, now()) RETURNING id")).scalar_one()
            assert new_id == 4
    finally:
        engine.dispose()


@needs_postgres
def test_apply_copies_rows_and_conversations(source_db: Path, tmp_path: Path, monkeypatch, pg_schema: str):
    monkeypatch.setenv(TARGET_ENV, POSTGRES_URL)
    before = _digest(source_db)
    snapshots = tmp_path / "snapshots"
    code = migrate.main(_apply_args(source_db, snapshots, pg_schema))
    report = json.loads((snapshots / "report.json").read_text(encoding="utf-8"))
    assert code == 0, report["problems"]
    assert report["ok"] and report["checkpoint_threads"] == 2 and report["writes"] > 0
    assert report["tables"]["run_events"] == 3 and report["sequences"]["run_events.id"] == 3
    assert _digest(source_db) == before
    _assert_rows(pg_schema)
    asyncio.run(_assert_same_conversations(source_db, pg_schema))


@needs_postgres
def test_apply_refuses_a_target_that_is_not_empty(source_db: Path, tmp_path: Path, monkeypatch, pg_schema: str):
    monkeypatch.setenv(TARGET_ENV, POSTGRES_URL)
    assert migrate.main(_apply_args(source_db, tmp_path / "first", pg_schema)) == 0
    assert migrate.main(_apply_args(source_db, tmp_path / "second", pg_schema)) == 2
    assert not (tmp_path / "second").exists()


@needs_postgres
def test_verification_catches_a_tampered_target(source_db: Path, tmp_path: Path, monkeypatch, pg_schema: str):
    monkeypatch.setenv(TARGET_ENV, POSTGRES_URL)
    snapshots = tmp_path / "snapshots"
    assert migrate.main(_apply_args(source_db, snapshots, pg_schema)) == 0

    engine = sa.create_engine(migrate.database_config(POSTGRES_URL, pg_schema).app_sync_sqlalchemy_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE threads_meta SET display_name = '被改过'"))
            conn.execute(sa.text("UPDATE user_preferences SET value = NULL WHERE key = 'json-null'"))
            conn.execute(sa.text("UPDATE checkpoint_writes SET blob = decode('00', 'hex') WHERE task_id = 'task-pending' AND channel = '__error__'"))
            conn.execute(sa.text("UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{channel_values,title}', '\"别的标题\"') WHERE thread_id = 'thread-side' AND checkpoint -> 'channel_values' ? 'title'"))
            conn.execute(sa.text("UPDATE checkpoints SET metadata = metadata || '{\"source\": \"tampered\"}' WHERE thread_id = 'thread-main'"))
        snapshot = snapshots / "deerflow.db"
        report = migrate.Report(source=str(snapshot))
        migrate.verify_orm(snapshot, engine, migrate.orm_tables(), report)
        asyncio.run(migrate.verify_checkpoints(snapshot, migrate.psycopg_dsn(POSTGRES_URL, pg_schema), report))
    finally:
        engine.dispose()

    problems = "\n".join(report.problems)
    assert "threads_meta" in problems and "display_name" in problems
    assert "user_preferences" in problems and "value" in problems
    assert "pending writes" in problems
    assert "通道 title 的值不同" in problems
    assert "metadata 不同" in problems
