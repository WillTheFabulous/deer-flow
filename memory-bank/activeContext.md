# Active Context
[Last Updated: 2026-09-29]

## 当前焦点

1. **上游同步重建待收尾**：`sync/upstream-2026-09` 已完成移植与验证；等用户确认后把 `main` 重置为该分支并 force-with-lease 推送。主工作目录仍在 `legacy/fork-2026-06`（线上容器在跑旧代码），切换与容器重建单独执行。见 `feature-plans/upstream-sync-2026-09/progress.md`。
2. **Cursor Rules / Memory Bank 多人模式已初始化**：`.cursor/rules` 7 条、`memory-bank-merge` skill、OWNERS / CODEOWNERS / CI / pre-commit。建议以后单独打开 `/work/deerflow/deer-flow` 作为 workspace，避免和 mooya 的常驻规则同时生效。

## 最近完成

- 2026-09-29 fork 按上游最新版重建完成（见 progress.md「上游同步重建」）。
- 2026-09-28 宿主机 DNS 修复：Tailscale 放行阿里内网段 + systemd timer（见 progress.md）。
- 2026-09-28 备份：`legacy/fork-2026-06` 分支 + tag `fork-legacy-20260928`（含 WIP）已推 origin；配置与数据备份在 `/work/deerflow/backup-20260928/`。

## 最近技术决策

- 以上游为准重建 fork：fork 功能集中在 `backend/app/channels/fork/`，上游文件只留带「Fork」注释的挂接点。
- 人设沿用上游「钉在线程上」：切换人设 = 新会话；`/new` 沿用当前人设；旧 fork 的未钉住线程回落会话人设。
- `flatten_p2p` 废弃（上游原生单聊单线程 + 行内回复）。
- 子任务 token：不移植 harness 层增量上报，`/status` 改为读取 task 事件里的 usage。
- `config.yaml` 继续纳入版本管理，只写 `$VAR`；memory-bank 纳入 git，禁止写入任何密钥。

## 活跃 Feature 目录

- `feature-plans/upstream-sync-2026-09/`

## 下一步 / 阻塞项

- 用户确认后 force-push `main`，再按 feature progress 切换主目录、重建 dev 容器。
- 切换前从 `.env` 删除 `UV_INDEX_URL`；构建镜像按 `DEPLOYMENT.md` 临时改写 `uv.lock` 走阿里云镜像。
- 局域网访问 Web 需在 `docker/.env` 写 `BIND_HOST=0.0.0.0`。
- 容器重建后跑飞书手测清单（见 feature progress）。
- clash 代理未运行：cursor-agent 目前只能看到少数模型。
