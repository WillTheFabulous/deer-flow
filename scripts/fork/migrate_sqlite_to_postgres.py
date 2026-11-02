#!/usr/bin/env python3
"""把 DeerFlow 主库从 SQLite（``<DEER_FLOW_HOME>/data/deerflow.db``）整库迁到 Postgres。

迁移内容：

1. 业务表（账号、飞书绑定、会话、运行与事件……）：目标库用 DeerFlow 自己的 bootstrap 建到 alembic head，
   再按外键顺序逐表复制；时间列存成带时区的 UTC，JSON 列区分 SQL NULL 与 JSON null，自增序列调到最大值。
2. LangGraph 检查点：两边表结构不同（SQLite 一个检查点一整块 msgpack；Postgres 拆成 JSONB + 按通道版本存的 blob），
   逐个检查点经 ``AsyncPostgresSaver.aput`` 写入，检查点 id 与父子链不变；pending writes 原样复制（type、字节、idx）。
3. LangGraph Store：只在目标库建表；源库 Store 不为空就拒绝。

不迁：DeerMem 记忆检索索引（``.retrieval/memory-fts5.sqlite3``，上游写死 SQLite）；记忆、人设、渠道 store 本来就是文件。

用法（在 gateway 镜像的一次性容器里跑，见 DEPLOYMENT.md「旧数据迁移」；正式切换前必须先停 gateway）：

    python scripts/fork/migrate_sqlite_to_postgres.py [--sqlite PATH] [--target-env DATABASE_URL] [--schema NAME] [--apply]

默认只预览（源库计数、能否迁、目标库是否为空），加 --apply 才写入。目标库连接串只从环境变量读，不进命令行参数。
写入前先用 SQLite backup API 拷一份快照（默认 ``<DEER_FLOW_HOME>/backups/sqlite-to-pg-<时间>/deerflow.db``），
之后只读快照，源文件不动。写完逐表逐行、逐个检查点核对，结果写进快照目录的 ``report.json``；
核对不过退出码 1，拒绝迁移退出码 2。目标库必须是空的：失败后清空目标库再重跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 源库里不归 DeerFlow ORM 管的表：alembic 版本、LangGraph 检查点、LangGraph Store
CHECKPOINT_TABLES = frozenset({"checkpoints", "writes"})
STORE_TABLES = frozenset({"store", "store_migrations"})
NON_ORM_SOURCE_TABLES = frozenset({"alembic_version"}) | CHECKPOINT_TABLES | STORE_TABLES

# Postgres 读 v<4 且有父检查点的检查点时，会从父检查点的 writes 补出这个通道（上游迁移逻辑）
TASKS_CHANNEL = "__pregel_tasks"
INSERT_BATCH = 500
MAX_LISTED_PROBLEMS = 50
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SELECT_CHECKPOINTS_SQL = "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata FROM checkpoints ORDER BY thread_id, checkpoint_ns, checkpoint_id"
_SELECT_WRITES_SQL = "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value FROM writes ORDER BY thread_id, checkpoint_ns, checkpoint_id, task_id, idx"
_INSERT_WRITES_SQL = "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, blob) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
_SELECT_TARGET_WRITES_SQL = "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, task_path FROM checkpoint_writes"


class MigrationError(RuntimeError):
    """拒绝迁移或迁移中断：信息直接给运维看。"""


@dataclass
class SourceInfo:
    path: str
    revision: str | None
    tables: dict[str, list[str]]
    counts: dict[str, int]
    checkpoint_threads: int


@dataclass
class Report:
    source: str
    snapshot: str | None = None
    target_schema: str = ""
    revision: str | None = None
    tables: dict[str, int] = field(default_factory=dict)
    checkpoints: int = 0
    checkpoint_threads: int = 0
    writes: int = 0
    # Postgres saver 写 metadata 时会丢掉 "writes" 键（上游一贯如此），这里只计数
    metadata_writes_dropped: int = 0
    legacy_checkpoints: int = 0
    sequences: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    problem_count: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.problem_count == 0

    def problem(self, message: str) -> None:
        self.problem_count += 1
        if len(self.problems) < MAX_LISTED_PROBLEMS:
            self.problems.append(message)

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "ok": self.ok}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# ORM 元数据与源库检查
# ---------------------------------------------------------------------------


def orm_tables() -> list[Any]:
    """DeerFlow 全部 ORM 表，按外键依赖排好序（父表在前）。"""
    import deerflow.persistence.models  # noqa: F401  注册全部 ORM 模型
    from deerflow.persistence.base import Base

    return list(Base.metadata.sorted_tables)


def head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import deerflow.persistence

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(deerflow.persistence.__file__).resolve().parent / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if not head:
        raise MigrationError("DeerFlow 的 alembic 迁移目录里没有 head")
    return head


def _quote(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise MigrationError(f"表名或列名不是普通标识符：{name!r}")
    return f'"{name}"'


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise MigrationError(f"找不到 SQLite 库：{path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def inspect_source(path: Path) -> SourceInfo:
    conn = connect_readonly(path)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = {name: [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(name)})")] for name in names}
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0] for name in names}
        revision = None
        if "alembic_version" in tables:
            rows = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]
            if len(rows) != 1:
                raise MigrationError(f"源库 alembic_version 应该正好一行，实际 {len(rows)} 行")
            revision = rows[0]
        threads = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0] if "checkpoints" in tables else 0
    finally:
        conn.close()
    return SourceInfo(path=str(path), revision=revision, tables=tables, counts=counts, checkpoint_threads=threads)


def validate_source(info: SourceInfo, tables: Sequence[Any], head: str) -> None:
    """源库必须和当前代码同一个 alembic 版本、表和列一一对得上，否则拒绝（宁可不迁也不丢数据）。"""
    errors: list[str] = []
    if info.revision != head:
        errors.append(f"源库 alembic 版本 {info.revision!r} 与代码 head {head!r} 不一致：先用当前代码在 SQLite 上启动一次网关，让它升到 head")
    orm_names = {table.name for table in tables}
    unknown = sorted(set(info.tables) - orm_names - NON_ORM_SOURCE_TABLES)
    if unknown:
        errors.append(f"源库有脚本不认识的表（迁过去会丢）：{', '.join(unknown)}")
    missing = sorted(orm_names - set(info.tables))
    if missing:
        errors.append(f"源库缺 ORM 表：{', '.join(missing)}")
    for table in tables:
        if table.name not in info.tables:
            continue
        source_columns = set(info.tables[table.name])
        orm_columns = {column.name for column in table.columns}
        if extra := sorted(source_columns - orm_columns):
            errors.append(f"{table.name} 有 ORM 里没有的列：{', '.join(extra)}")
        if lacking := sorted(orm_columns - source_columns):
            errors.append(f"{table.name} 缺列：{', '.join(lacking)}")
    for name in sorted(CHECKPOINT_TABLES):
        if name not in info.tables:
            errors.append(f"源库缺 LangGraph 检查点表 {name}")
    if store_rows := info.counts.get("store", 0):
        errors.append(f"源库 LangGraph Store 有 {store_rows} 行，本脚本只处理空 Store")
    if errors:
        raise MigrationError("；\n".join(errors))


# ---------------------------------------------------------------------------
# 单元格转换与比对
# ---------------------------------------------------------------------------


def to_utc(value: Any) -> datetime | None:
    """SQLite 里的时间是去掉时区的字符串（DeerFlow 一律按 UTC 写），统一成带时区的 UTC。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise MigrationError(f"无法识别的时间值：{value!r}")
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_json(raw: Any) -> Any:
    # SQLite 里 JSON 列是 NUMERIC 亲和性，纯数字的 JSON 会被存成数字，不是文本
    return json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw


def json_param(raw: Any) -> Any:
    """SQL NULL 写成 SQL NULL，JSON null 写成 JSON null（SQLAlchemy 默认把 None 写成 JSON null）。"""
    from sqlalchemy import JSON, null

    if raw is None:
        return null()
    value = _parse_json(raw)
    return JSON.NULL if value is None else value


def convert_value(column: Any, raw: Any) -> Any:
    import sqlalchemy as sa

    if isinstance(column.type, sa.JSON):
        return json_param(raw)
    if raw is None:
        return None
    if isinstance(column.type, sa.DateTime):
        return to_utc(raw)
    if isinstance(column.type, sa.Boolean):
        return bool(raw)
    if isinstance(column.type, sa.Integer):
        return int(raw)
    return raw


def normalize_value(column: Any, value: Any) -> Any:
    """把一格值规范成可比较的形式：源库原始值与目标库读回值（JSON 列读成文本）走同一套规则。"""
    import sqlalchemy as sa

    if isinstance(column.type, sa.JSON):
        if value is None:
            return ("sql-null",)
        return ("json", json.dumps(_parse_json(value), sort_keys=True, ensure_ascii=False))
    if value is None:
        return None
    if isinstance(column.type, sa.DateTime):
        return to_utc(value).isoformat()
    if isinstance(column.type, sa.Boolean):
        return bool(value)
    if isinstance(column.type, sa.Integer):
        return int(value)
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)


def _json_dumps(value: Any) -> str:
    # 与网关 ORM 引擎的 json_serializer 一致：中文不转义
    return json.dumps(value, ensure_ascii=False)


def same_value(left: Any, right: Any, serde: Any) -> bool:
    try:
        if left == right:
            return True
    except Exception:  # noqa: BLE001 - 有的对象 == 会抛错，退回按序列化字节比
        pass
    try:
        return serde.dumps_typed(left) == serde.dumps_typed(right)
    except Exception:  # noqa: BLE001
        return False


def diff_checkpoint(expected: dict, expected_metadata: dict, parent_id: str | None, actual: Any, serde: Any) -> list[str]:
    """比对快照里解出的检查点与 Postgres 读回的 CheckpointTuple，返回差异描述。"""
    problems: list[str] = []
    checkpoint = actual.checkpoint
    migrated = expected.get("v", 4) < 4 and parent_id is not None
    skipped = {TASKS_CHANNEL} if migrated else set()

    expected_values = expected.get("channel_values") or {}
    actual_values = checkpoint.get("channel_values") or {}
    for channel in sorted((set(expected_values) | set(actual_values)) - skipped):
        if channel not in actual_values:
            problems.append(f"少了通道 {channel}")
        elif channel not in expected_values:
            problems.append(f"多了通道 {channel}")
        elif not same_value(expected_values[channel], actual_values[channel], serde):
            problems.append(f"通道 {channel} 的值不同")

    for key in sorted((set(expected) | set(checkpoint)) - {"channel_values"}):
        left, right = expected.get(key), checkpoint.get(key)
        if key == "channel_versions" and migrated:
            left = {k: v for k, v in (left or {}).items() if k not in skipped}
            right = {k: v for k, v in (right or {}).items() if k not in skipped}
        if canonical(left) != canonical(right):
            problems.append(f"字段 {key} 不同")

    wanted_metadata = {key: value for key, value in expected_metadata.items() if key != "writes"}
    if canonical(wanted_metadata) != canonical(actual.metadata):
        problems.append("metadata 不同")

    actual_parent = ((actual.parent_config or {}).get("configurable") or {}).get("checkpoint_id")
    if actual_parent != parent_id:
        problems.append(f"父检查点不同（应为 {parent_id}，实际 {actual_parent}）")
    return problems


def _chunks(rows: Iterator[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# 目标库
# ---------------------------------------------------------------------------


def database_config(url: str, schema: str) -> Any:
    from deerflow.config.database_config import DatabaseConfig

    return DatabaseConfig(backend="postgres", postgres_url=url, postgres_schema=schema)


def psycopg_dsn(url: str, schema: str) -> str:
    from deerflow.persistence.postgres_schema import dsn_with_search_path, normalize_libpq_dsn

    return dsn_with_search_path(normalize_libpq_dsn(url), schema)


def target_tables(dsn: str) -> list[str]:
    """目标 schema 里已有的表；schema 还不存在时 current_schema() 为 NULL，自然是空。"""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY table_name").fetchall()
    return [row[0] for row in rows]


async def bootstrap_orm(url: str, schema: str) -> None:
    """和网关启动走同一条 bootstrap：空库 → create_all + alembic stamp head。"""
    from deerflow.persistence.engine import close_engine, init_engine

    config = database_config(url, schema)
    try:
        await init_engine("postgres", url=config.app_sqlalchemy_url, postgres_schema=schema)
    finally:
        await close_engine()


def target_revision(engine: Any) -> str | None:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return rows[0] if len(rows) == 1 else None


def reset_sequences(conn: Any) -> dict[str, int]:
    """显式写入的自增主键不会推动序列，写完把每个序列调到当前最大值。"""
    from sqlalchemy import text

    columns = conn.execute(
        text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() AND (left(column_default, 8) = 'nextval(' OR is_identity = 'YES') ORDER BY table_name, column_name")
    ).all()
    result: dict[str, int] = {}
    for table_name, column_name in columns:
        sequence = conn.execute(text("SELECT pg_get_serial_sequence(:table, :column)"), {"table": table_name, "column": column_name}).scalar()
        if not sequence:
            continue
        max_value = conn.execute(text(f"SELECT MAX({_quote(column_name)}) FROM {_quote(table_name)}")).scalar()
        if max_value is None:
            conn.execute(text("SELECT setval(CAST(:sequence AS regclass), 1, false)"), {"sequence": sequence})
        else:
            conn.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, true)"), {"sequence": sequence, "value": int(max_value)})
        result[f"{table_name}.{column_name}"] = int(max_value or 0)
    return result


def copy_orm_rows(source_path: Path, engine: Any, tables: Sequence[Any], report: Report) -> None:
    """所有业务表在一个事务里写完，失败整体回滚。"""
    source = connect_readonly(source_path)
    try:
        with engine.begin() as conn:
            for table in tables:
                columns = list(table.columns)
                select_sql = f"SELECT {', '.join(_quote(column.name) for column in columns)} FROM {_quote(table.name)}"
                rows = ({column.key: convert_value(column, raw) for column, raw in zip(columns, record, strict=True)} for record in source.execute(select_sql))
                copied = 0
                for batch in _chunks(rows, INSERT_BATCH):
                    conn.execute(table.insert(), batch)
                    copied += len(batch)
                report.tables[table.name] = copied
            report.sequences = reset_sequences(conn)
    finally:
        source.close()


async def copy_checkpoints(source_path: Path, dsn: str, report: Report) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    source = connect_readonly(source_path)
    stored_blobs: set[tuple[str, str, str, str]] = set()
    try:
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            for thread_id, checkpoint_ns, checkpoint_id, parent_id, type_, blob, metadata_raw in source.execute(_SELECT_CHECKPOINTS_SQL):
                checkpoint = saver.serde.loads_typed((type_, blob))
                if checkpoint.get("id") != checkpoint_id:
                    raise MigrationError(f"检查点 {thread_id}/{checkpoint_id} 的内容 id 是 {checkpoint.get('id')!r}，对不上")
                metadata = json.loads(metadata_raw) if metadata_raw is not None else {}
                if "writes" in metadata:
                    report.metadata_writes_dropped += 1
                if checkpoint.get("v", 4) < 4:
                    report.legacy_checkpoints += 1
                # 同一通道同一版本的值只需写一次 blob（aput 本身也是 ON CONFLICT DO NOTHING），省掉重复序列化与传输
                new_versions = {}
                for channel, version in (checkpoint.get("channel_versions") or {}).items():
                    key = (thread_id, checkpoint_ns, channel, str(version))
                    if key not in stored_blobs:
                        new_versions[channel] = version
                        stored_blobs.add(key)
                configurable = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}
                if parent_id:
                    configurable["checkpoint_id"] = parent_id
                await saver.aput({"configurable": configurable}, checkpoint, metadata, new_versions)
                report.checkpoints += 1
                if report.checkpoints % 500 == 0:
                    print(f"  检查点已写 {report.checkpoints} 个", flush=True)
    finally:
        source.close()


async def copy_writes(source_path: Path, dsn: str, report: Report) -> None:
    import psycopg

    source = connect_readonly(source_path)
    try:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    for batch in _chunks(iter(source.execute(_SELECT_WRITES_SQL)), INSERT_BATCH):
                        params = []
                        for thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type_, value in batch:
                            if value is None:
                                if type_ != "null":
                                    raise MigrationError(f"writes {thread_id}/{checkpoint_id}/{task_id}/{idx} 的值是 NULL 但类型是 {type_!r}")
                                value = b""
                            params.append((thread_id, checkpoint_ns, checkpoint_id, task_id, "", idx, channel, type_, bytes(value)))
                        await cur.executemany(_INSERT_WRITES_SQL, params)
                        report.writes += len(params)
    finally:
        source.close()


async def setup_store(dsn: str) -> None:
    from langgraph.store.postgres.aio import AsyncPostgresStore

    async with AsyncPostgresStore.from_conn_string(dsn) as store:
        await store.setup()


# ---------------------------------------------------------------------------
# 核对
# ---------------------------------------------------------------------------


def verify_orm(source_path: Path, engine: Any, tables: Sequence[Any], report: Report) -> None:
    import sqlalchemy as sa

    source = connect_readonly(source_path)
    try:
        with engine.connect() as conn:
            for table in tables:
                columns = list(table.columns)
                key_positions = [index for index, column in enumerate(columns) if column.primary_key]

                def keyed(records: Any, columns: list[Any] = columns, key_positions: list[int] = key_positions) -> dict[tuple, tuple]:
                    result: dict[tuple, tuple] = {}
                    for record in records:
                        values = tuple(normalize_value(column, raw) for column, raw in zip(columns, record, strict=True))
                        result[tuple(values[index] for index in key_positions) if key_positions else values] = values
                    return result

                expected = keyed(source.execute(f"SELECT {', '.join(_quote(column.name) for column in columns)} FROM {_quote(table.name)}"))
                selected = [sa.cast(column, sa.Text) if isinstance(column.type, sa.JSON) else column for column in columns]
                actual = keyed(conn.execute(sa.select(*selected)))
                if len(expected) != len(actual):
                    report.problem(f"表 {table.name} 行数不同：源 {len(expected)}，目标 {len(actual)}")
                for key in sorted(set(expected) | set(actual), key=repr):
                    left, right = expected.get(key), actual.get(key)
                    if left == right:
                        continue
                    if left is None or right is None:
                        report.problem(f"表 {table.name} 主键 {key} 只在{'目标' if left is None else '源'}库里有")
                        continue
                    differing = [column.name for column, a, b in zip(columns, left, right, strict=True) if a != b]
                    report.problem(f"表 {table.name} 主键 {key} 的列不同：{', '.join(differing)}")
    finally:
        source.close()


async def verify_checkpoints(source_path: Path, dsn: str, report: Report) -> None:
    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    source = connect_readonly(source_path)
    try:
        expected_total = 0
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            for thread_id, checkpoint_ns, checkpoint_id, parent_id, type_, blob, metadata_raw in source.execute(_SELECT_CHECKPOINTS_SQL):
                expected_total += 1
                config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id}}
                actual = await saver.aget_tuple(config)
                if actual is None:
                    report.problem(f"检查点 {thread_id}/{checkpoint_id} 在目标库里读不到")
                    continue
                expected = saver.serde.loads_typed((type_, blob))
                expected_metadata = json.loads(metadata_raw) if metadata_raw is not None else {}
                for problem in diff_checkpoint(expected, expected_metadata, parent_id, actual, saver.serde):
                    report.problem(f"检查点 {thread_id}/{checkpoint_id}：{problem}")

        expected_writes = set()
        for thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type_, value in source.execute(_SELECT_WRITES_SQL):
            expected_writes.add((thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type_, bytes(value or b""), ""))
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            actual_total = (await (await conn.execute("SELECT COUNT(*) FROM checkpoints")).fetchone())[0]
            rows = await (await conn.execute(_SELECT_TARGET_WRITES_SQL)).fetchall()
        actual_writes = {(*row[:7], bytes(row[7] or b""), row[8]) for row in rows}
        if actual_total != expected_total:
            report.problem(f"检查点总数不同：源 {expected_total}，目标 {actual_total}")
        if len(rows) != len(expected_writes) or actual_writes != expected_writes:
            report.problem(f"pending writes 不同：源 {len(expected_writes)} 条，目标 {len(rows)} 条，不一致 {len(actual_writes ^ expected_writes)} 条")
    finally:
        source.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def take_snapshot(source: Path, destination_dir: Path) -> Path:
    """用 SQLite backup API 拷一份一致的快照（网关在写也不怕），之后只读快照。"""
    destination_dir.mkdir(parents=True, exist_ok=False)
    destination = destination_dir / "deerflow.db"
    reader = connect_readonly(source)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
        # 快照改回 rollback journal，只读打开时不需要 -wal / -shm
        writer.execute("PRAGMA journal_mode=DELETE")
    finally:
        writer.close()
        reader.close()
    return destination


def _print_source(info: SourceInfo, tables: Sequence[Any]) -> None:
    print(f"源库：{info.path}（alembic {info.revision}）")
    rows = sum(info.counts.get(table.name, 0) for table in tables)
    busy = ", ".join(f"{table.name} {info.counts[table.name]}" for table in tables if info.counts.get(table.name))
    print(f"  业务表 {len(tables)} 张，共 {rows} 行：{busy or '全空'}")
    print(f"  检查点 {info.counts.get('checkpoints', 0)} 个（{info.checkpoint_threads} 个线程），writes {info.counts.get('writes', 0)} 条，Store {info.counts.get('store', 0)} 行")


def _target_url(env_name: str) -> str:
    url = os.environ.get(env_name, "").strip()
    if not url:
        raise MigrationError(f"环境变量 {env_name} 没有设置：目标库连接串只从环境变量读")
    return url


def run_preview(args: argparse.Namespace) -> int:
    tables = orm_tables()
    info = inspect_source(args.sqlite)
    _print_source(info, tables)
    exit_code = 0
    try:
        validate_source(info, tables, head_revision())
        print("  检查通过：版本、表与列都对得上")
    except MigrationError as exc:
        print(f"  不能迁：{exc}")
        exit_code = 2
    url = os.environ.get(args.target_env, "").strip()
    if not url:
        print(f"目标库：没有设置 {args.target_env}，跳过检查")
    elif existing := target_tables(psycopg_dsn(url, args.schema)):
        print(f"目标库（{args.schema or '默认 schema'}）：已有 {len(existing)} 张表，不能迁：{', '.join(existing[:12])}")
        exit_code = 2
    else:
        print(f"目标库（{args.schema or '默认 schema'}）：空，可以迁")
    print("预览结束，没有写任何东西；加 --apply 才会迁移")
    return exit_code


async def migrate(source_path: Path, url: str, schema: str, snapshot_dir: Path) -> Report:
    import sqlalchemy as sa

    report = Report(source=str(source_path), target_schema=schema)
    dsn = psycopg_dsn(url, schema)
    if existing := target_tables(dsn):
        raise MigrationError(f"目标库不是空的（{schema or '默认 schema'} 里已有 {len(existing)} 张表：{', '.join(existing[:12])}），先清空再迁")

    started = time.monotonic()
    snapshot = take_snapshot(source_path, snapshot_dir)
    report.snapshot = str(snapshot)
    print(f"快照：{snapshot}", flush=True)
    tables = orm_tables()
    info = inspect_source(snapshot)
    _print_source(info, tables)
    head = head_revision()
    validate_source(info, tables, head)
    report.checkpoint_threads = info.checkpoint_threads

    step = time.monotonic()
    await bootstrap_orm(url, schema)
    engine = sa.create_engine(database_config(url, schema).app_sync_sqlalchemy_url, json_serializer=_json_dumps)
    try:
        report.revision = target_revision(engine)
        if report.revision != head:
            raise MigrationError(f"目标库建表后 alembic 版本是 {report.revision!r}，应为 {head!r}")
        copy_orm_rows(snapshot, engine, tables, report)
        report.seconds["orm"] = round(time.monotonic() - step, 1)
        print(f"业务表已写：{sum(report.tables.values())} 行（{report.seconds['orm']} 秒）", flush=True)

        step = time.monotonic()
        await copy_checkpoints(snapshot, dsn, report)
        await copy_writes(snapshot, dsn, report)
        await setup_store(dsn)
        report.seconds["checkpoints"] = round(time.monotonic() - step, 1)
        print(f"检查点已写：{report.checkpoints} 个，writes {report.writes} 条（{report.seconds['checkpoints']} 秒）", flush=True)

        step = time.monotonic()
        verify_orm(snapshot, engine, tables, report)
        await verify_checkpoints(snapshot, dsn, report)
        report.seconds["verify"] = round(time.monotonic() - step, 1)
    finally:
        engine.dispose()
    report.seconds["total"] = round(time.monotonic() - started, 1)
    return report


def run_apply(args: argparse.Namespace) -> int:
    url = _target_url(args.target_env)
    snapshot_dir = args.snapshot_dir or args.sqlite.resolve().parent.parent / "backups" / f"sqlite-to-pg-{datetime.now():%Y%m%d-%H%M%S}"
    report = asyncio.run(migrate(args.sqlite, url, args.schema, snapshot_dir))
    (snapshot_dir / "report.json").write_text(report.to_json(), encoding="utf-8")
    if not report.ok:
        print(f"核对不过：{report.problem_count} 处差异，前几条：")
        for problem in report.problems[:20]:
            print(f"  - {problem}")
        print(f"完整报告：{snapshot_dir / 'report.json'}")
        return 1
    print(
        f"核对通过：业务表 {sum(report.tables.values())} 行，检查点 {report.checkpoints} 个（{report.checkpoint_threads} 个线程），"
        f"writes {report.writes} 条；Postgres 不保存 metadata 里的 writes 标记，涉及 {report.metadata_writes_dropped} 个检查点；"
        f"共 {report.seconds['total']} 秒。报告：{snapshot_dir / 'report.json'}"
    )
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    home = Path(os.environ.get("DEER_FLOW_HOME") or "backend/.deer-flow")
    parser = argparse.ArgumentParser(description="把 DeerFlow 主库从 SQLite 迁到 Postgres（默认只预览）")
    parser.add_argument("--sqlite", type=Path, default=home / "data" / "deerflow.db", help="源 SQLite 库（默认 $DEER_FLOW_HOME/data/deerflow.db）")
    parser.add_argument("--target-env", default="DATABASE_URL", help="目标库连接串所在的环境变量名（默认 DATABASE_URL）")
    parser.add_argument("--schema", default="", help="目标 schema，对应 database.postgres_schema；留空用库的默认 schema")
    parser.add_argument("--snapshot-dir", type=Path, default=None, help="快照与报告目录（默认 $DEER_FLOW_HOME/backups/sqlite-to-pg-<时间>）")
    parser.add_argument("--apply", action="store_true", help="真正迁移（不加只预览）")
    args = parser.parse_args(argv)
    if args.schema:
        from deerflow.config.postgres_schema import validate_postgres_schema

        validate_postgres_schema(args.schema)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_apply(args) if args.apply else run_preview(args)
    except MigrationError as exc:
        print(f"拒绝迁移：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
