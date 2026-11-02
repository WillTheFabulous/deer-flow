"""一次 CLI 调用 / 一个 worker 进程的运行上下文。"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__, table_registry
from . import config as cfgmod
from .db import DB, PgDB
from .paths import podflow_home

DB_NAME = "podflow.db"


@dataclass
class Ctx:
    home: Path
    cfg: dict[str, Any]
    db: DB | PgDB
    secrets: dict[str, str]
    owner: str

    @property
    def pipeline_version(self) -> str:
        return f"podflow-{__version__}"

    def setting(self, dotted: str, default: Any = None) -> Any:
        return cfgmod.get(self.cfg, dotted, default)


def make_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def open_db(home: Path, cfg: dict[str, Any], secrets: dict[str, str]) -> DB | PgDB:
    """`database.url_name` 留空用数据目录下的 SQLite；填了就用 secrets.env 里那个连接串连 Postgres。"""
    name = str(cfgmod.get(cfg, "database.url_name") or "").strip()
    if not name:
        return DB(home / DB_NAME)
    url = secrets.get(name) or os.environ.get(name)
    if not url:
        raise cfgmod.ConfigError(
            f"database.url_name = {name}，但 secrets.env 里没有这一项"
        )
    return PgDB(url)


def open_ctx(home: Path | None = None) -> Ctx:
    home = home or podflow_home()
    cfg = cfgmod.load_config(home)
    secrets = cfgmod.load_secrets(home)
    db = open_db(home, cfg, secrets)
    db.init_schema()
    # 每周选款表的表在哪本表格（拼接口路径用）
    table_registry.load(db)
    return Ctx(home=home, cfg=cfg, db=db, secrets=secrets, owner=make_owner())
