# DeerFlow 主库迁 Postgres · 进度

[Last Updated: 2026-11-02]

## 当前阶段

已切换（2026-10-29 18:57），网关跑在 Postgres 上；11-02 飞书真人验证通过，SQLite 回滚文件已提前删（用户定）。只剩加备份 cron（等 `/work` 磁盘清理定了再加）。

## 已完成

- 18:30 建角色与库：`deerflow`（正式）/ `deerflow_test`（测试，单独角色），SCRAM 预哈希经 stdin 交给 `psql -U llmproxy`，明文不进服务器和进程参数；`.env` 加 `DATABASE_URL`，测试库连接串放 `backend/.deer-flow/secrets/postgres-test.env`（600）。两个连接串在容器里都连通，测试角色连不上正式库。
- 迁移脚本与测试：15 个用例（离线 12 个，其中拒绝迁移的一组参数化 5 个；Postgres 3 个），在容器里对 `deerflow_test` 的临时 schema 跑通；6 处变异（SQL NULL 写成 JSON null、不调序列、检查点不写 blob、核对忽略 metadata、核对忽略通道值、时间不补时区）都会让用例失败。ruff 用 `backend/ruff.toml` 通过。
- 备份脚本：`-U pod_blanket` 改成 `PG_USER`（缺省等于库名）；`pod_blanket` / `deerflow` / `deerflow_test` 三个库在临时目录试跑都成功。
- 18:51 演练（网关照常）：快照迁进 `deerflow`，业务表 493 行、检查点 3137 个（15 个线程）、writes 3917 条，核对 0 差异，共 34.9 秒（复制约 13 秒、核对约 17 秒）；Postgres 里 42MB（SQLite 148MB）。看完 `DROP DATABASE` 再建回空库。
- 18:53 正式切换：`podctl pause` → 18:56 出图 worker 停下（12 条工位各做完手上一步）→ 18:56:15 停网关 → 一次性容器迁移核对通过（34.0 秒，快照 `backups/sqlite-to-pg-20261029-105631/`）→ 网关停着时改 `deploy/fork/config.yaml` 的 `database` → 18:57:24 `--force-recreate gateway`，18:57:38 启动完成、飞书长连接连上。飞书离线约 1.5 分钟。
- 启动日志：`bootstrap: branch=versioned revision=0026_mcp_task_lease_tokens`、`Persistence engine initialized: backend=postgres`、`Store: using AsyncPostgresStore`、入站去重也走 Postgres；`/health` 200；没有数据库相关报错。
- 19:00 `podctl resume`：worker 重新起来，12 条工位接着出图。
- 冒烟（只读）：以两个会话所有者身份（5 分钟临时令牌）走真实 HTTP 接口：`POST /api/threads/search` 列出的会话与 `threads_meta` 一致，10 个会话 `GET /state` 的消息数都和切换前快照一致（飞书单聊那个 50 条），`/history` 正常；3 条飞书绑定经 `ChannelConnectionRepository.find_connection_by_external_identity` 在 Postgres 上都能解析到原账号，2 条会话映射指向迁过来的线程。
- 19:02 首份备份 `/work/backups/postgres/deerflow_20261029_190224.dump`（8.1MB）。`deerflow.db` 自 18:44 起不再被写；切换后 `deerflow` 库 7 个连接，实例共 59 / 200。
- 文档：DEPLOYMENT.md（部署拓扑、环境变量、配置文件、上游同步与切换 → 主库 SQLite → Postgres、安全清单、pod-blanket 备份行）、`.env.example` fork 段、upstream-sync 补丁清单、memory-bank。
- 2026-11-02 真人验证 + 提前删 SQLite 回滚文件（用户定：`/work` 只剩约 1GB）：
  - 核查：切换后到 11-02 凌晨 `runs` 一条新记录都没有（最新是 10-29 09:37 UTC），旧会话从没被真正接着用过，所以先做真人验证。
  - 09:18 用户在单聊接着原会话发一句：run `afbcd3f3` 建在 10-11 的旧线程 `f3b752e1` 上，10 秒后 success，回复正常发出，`/pod` 卡片正常；09:10 之后网关日志无错误 / 告警。
  - 新恢复点 `/work/backups/postgres/deerflow_20261102_091948.dump`（8.1M，29 张表都有数据）；正式快照 gzip 一份到根盘 `/root/deerflow-sqlite-archive/`（解压后 sha256 与原文件一致，11-12 后删）。
  - 再扫一遍宿主机与容器没有进程打开后，按完整路径删：`data/deerflow.db*`、`backups/sqlite-to-pg-20261029-105123/` 与 `-105631/`、`backups/migrate-default-20261009-183312/`、pod-blanket 的 `podflow.db*` 与 `podflow.db.bak-20261013*`（14 个文件、3 个空目录）；`/work` 空闲 933M → 1.6G。网关 `/health` 200、`deerflow` 库 7 个连接、`podctl doctor` 只有原来 4 个已知 warn。
  - `deploy/fork/config.yaml` 的 `database` 注释改成「不能再回滚到 SQLite，恢复用 pg_dump」，只改注释、没重启。

## 待手测

- [x] 飞书单聊发一句：机器人回复、能接上原会话上下文；`/sessions` 列表、`/pod` 工作台照常（2026-11-02 用户确认，库里与日志见「已完成」）。
- [ ] Web 用原账号登录看会话与历史（前端 10-31 起默认不启动，要看时临时起 `--profile web`）。

## 下一步

- ~~2026-11-12 后删除旧文件~~：11-02 已提前删（见「已完成」）。根盘 `/root/deerflow-sqlite-archive/`（正式快照 gzip，约 45MB）11-12 后删。
- 给两个库加备份 cron（`backup_pod_blanket.sh` 注释里有两行写法），与原有待办合并。

## 阻塞项

- 无。
