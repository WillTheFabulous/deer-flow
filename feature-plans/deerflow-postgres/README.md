# DeerFlow 主库从 SQLite 迁到 Postgres（deerflow-postgres）

[Last Updated: 2026-11-02]

## 目标

- 用户 2026-10-29 要「只用 Postgres」：DeerFlow 网关自己的主库（账号、飞书绑定、会话、运行与事件、对话检查点、LangGraph Store）从 `backend/.deer-flow/data/deerflow.db` 搬到 mooya Postgres 实例上的独立库 `deerflow`。
- 旧对话上下文一起搬（用户在「只迁账号和会话列表」与「连检查点一起搬」之间选了后者）：飞书单聊和 Web 里的旧会话切换后能接着聊。

## 范围

- 迁：`deerflow.db` 里全部 ORM 表（22 张）、LangGraph 检查点（`checkpoints` + `writes`）。Store 在 SQLite 里是空的，只在目标库建表。
- 不动：DeerMem 记忆检索索引 `.retrieval/memory-fts5.sqlite3`（上游写死 SQLite FTS5，是从记忆派生的索引）；pod-blanket 引擎的 SQLite 分支（单测靠它，业务数据 10-13 起就在 Postgres）；记忆、人设、渠道 store 本来就是文件。
- 原定 2026-11-12 起删、**2026-11-02 已提前删**（用户定：`/work` 满了，飞书真人验证通过后删）：`data/deerflow.db*`（回滚用）、两份迁移快照 `backups/sqlite-to-pg-*`、pod-blanket 的 `podflow.db*`（10-13 起没用）、10-09 的旧备份 `backups/migrate-default-20261009-183312/`。从此不能再回滚到 SQLite，恢复靠 Postgres 备份；根盘 `/root/deerflow-sqlite-archive/` 留了一份正式快照的 gzip，11-12 后删。

## 关键决策

- 库放 mooya 的 Postgres（`litellm_multi_provider_db`，宿主机 5431）：独立库 `deerflow`、角色 `deerflow`（`CONNECTION LIMIT 30`）；测试库 `deerflow_test` 用单独的角色 `deerflow_test`（`CONNECTION LIMIT 10`），连不上正式库，测试连接串填错也删不到正式数据（计划里原写两库同一角色，执行时改成分开）。两库都 `REVOKE CONNECT FROM PUBLIC`，与 `pod_blanket` 一致。
- 默认 schema（`public`），不设 `database.postgres_schema`：库本身就是独立的。
- 上游没有 SQLite → Postgres 的迁移工具，写 fork 脚本 `scripts/fork/migrate_sqlite_to_postgres.py`，全部复用上游代码：建表走网关同一条 bootstrap（`init_engine`，空库 → `create_all` + `stamp head`），业务表按 `Base.metadata.sorted_tables` 复制，检查点经 `AsyncPostgresSaver.aput` 写（两边表结构不同：SQLite 一个检查点一整块 msgpack，Postgres 拆成 JSONB + 按通道版本存的 blob），writes 原样复制。
- 宁可不迁也不丢：源库版本与代码 head 不一致、有不认识的表或列、Store 不空、目标库不空，一律拒绝；写完逐行、逐个检查点核对，不过就退出码 1、不切换。
- 连接串只从环境变量读（`.env` 的 `DATABASE_URL`），不进命令行参数；脚本在 gateway 镜像的一次性容器里跑（`compose run --rm --no-deps`），会重新读 `.env`，演练时不影响在跑的网关。
- 先演练（网关照常、迁一份快照、看完清空目标库）再正式切换（POD 暂停到 worker 退出 → 停网关 → 迁移核对 → 网关停着时改 config → 重建网关）。

## 已知差异（接受）

- Postgres saver 写 metadata 时一贯丢掉 `writes` 键（上游 `get_serializable_checkpoint_metadata`）：线上 57 个检查点的 `writes` 标记（主要是运行时长的「duration-only」检查点）迁过去后没有了，和原生 Postgres 部署一样；上游检查点保留逻辑另有不靠这个标记的判断。
- 10 个 v2 老检查点（6 月）都没有父检查点，Postgres 读时的 v<4 迁移不触发，读回与 SQLite 一致。

## 风险

- 整个飞书机器人（含 `/pod`）和 mooya Postgres 同生死：它停了或 `/work` 写满（10-28 那次），网关起不来或请求报错。
- mooya Postgres 没有定时备份：`backup_pod_blanket.sh` 已能备份 `deerflow`（`PG_DATABASE=deerflow`），cron 还没加。11-02 起 SQLite 回滚文件已删，Postgres 备份是唯一的恢复手段（目前手动拍的：10-29 19:02、11-02 09:19 各一份）。
- 以后同步上游：新的 alembic 迁移会直接跑在这个库上，先在 `deerflow_test` 上跑上游 Postgres 用例。

## 相关文件

- `scripts/fork/migrate_sqlite_to_postgres.py`、`backend/tests/test_fork_migrate_sqlite_to_postgres.py`
- `deploy/fork/config.yaml` 的 `database` 段；`.env` 的 `DATABASE_URL`；`backend/.deer-flow/secrets/postgres-test.env`（`DEERFLOW_TEST_POSTGRES_URL`）
- `scripts/fork/backup_pod_blanket.sh`（`PG_USER` 缺省等于库名）
- `DEPLOYMENT.md`：部署拓扑、环境变量、配置文件、上游同步与切换 → 主库 SQLite → Postgres、安全清单
