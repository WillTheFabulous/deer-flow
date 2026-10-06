# Active Context
[Last Updated: 2026-10-07]

## 当前焦点

1. **待确认后切换**：飞书绑定适配、迁移脚本和副本演练已完成。主目录仍在 `legacy/fork-2026-06`，切换、重建、`/setup` 和迁移要一起做，执行前需确认。步骤见 `feature-plans/upstream-sync-2026-09/progress.md`。
2. **Cursor Rules / Memory Bank 多人模式已初始化**：`.cursor/rules` 7 条、`memory-bank-merge` skill、OWNERS / CODEOWNERS / CI / pre-commit。建议以后单独打开 `/work/deerflow/deer-flow` 作为 workspace。

## 最近完成

- 2026-10-07 飞书绑定适配与 `default` 数据迁移演练完成（见 progress.md 同名条目）。
- 2026-09-29 fork 按上游最新版重建完成（见 progress.md「上游同步重建」）。
- 2026-09-28 宿主机 DNS 修复：Tailscale 放行阿里内网段 + systemd timer（见 progress.md）。
- 2026-09-28 备份：`legacy/fork-2026-06` 分支 + tag `fork-legacy-20260928`（含 WIP）已推 origin；配置与数据备份在 `/work/deerflow/backup-20260928/`。

## 最近技术决策

- 以上游为准重建 fork：fork 功能集中在 `backend/app/channels/fork/`，上游文件只留带「Fork」注释的挂接点。
- 人设沿用上游「钉在线程上」：切换人设 = 新会话；`/new` 沿用当前人设；旧 fork 的未钉住线程回落会话人设。
- `flatten_p2p` 废弃（上游原生单聊单线程 + 行内回复）。
- 子任务 token：不移植 harness 层增量上报，`/status` 改为读取 task 事件里的 usage。
- 部署配置追踪在 `deploy/fork/config.yaml`（根目录软链），只写 `$VAR`；memory-bank 纳入 git，禁止写入任何密钥。
- 飞书身份走上游 `channel_connections`（`require_bound_identity`）；旧 `default` 数据用 `scripts/fork/migrate_default_owner.py` 归到管理员。

## 活跃 Feature 目录

- `feature-plans/upstream-sync-2026-09/`

## 下一步 / 阻塞项

- 确认后才切换：停栈、主目录 `git switch main`、`rebuild-dev.sh --reset-venv`。见 feature progress。
- 确认后才建号迁移：`/setup` → 停 gateway → `migrate_default_owner.py --apply` → 飞书 `/connect`。
- 切换前从 `.env` 删除 `UV_INDEX_URL`；`docker/.env` 写 `BIND_HOST` 与 `DEER_FLOW_DEV_ALLOWED_ORIGINS`。
- 切换前定 cursor-agent 网络：启用 clash，或 `CURSOR_AGENT_PROXY` 置空并改角色模型。
- 绑定后跑飞书手测清单（feature progress）。
