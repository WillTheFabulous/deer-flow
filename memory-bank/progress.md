# Progress
[Last Updated: 2026-09-29]

## 已完成里程碑

### 2026-09-29 上游同步重建（sync/upstream-2026-09）

- 以 `upstream/main`（PR #5990）为基线重建 fork，逐项移植：cursor-agent overlay、子代理 thinking 开关、飞书功能包（`app/channels/fork/`）、`/status` 子任务 token、config.yaml 迁移到 `config_version 50`。
- 决策：`flatten_p2p` 废弃（上游原生）；人设改走上游线程钉住机制；不移植 harness 层的子任务 token 增量上报。
- 验证：ruff 全量通过；全量单测 21,202 通过 / 3 失败（均为宿主机缺 `/etc/mime.types` 导致的 `.xhtml` 环境性失败）；`config.yaml` 经 `AppConfig` 加载校验。
- 已推送：origin `main` `60270043` → `3759de00`（force-with-lease）；主目录切换与容器重建待执行。
- 详情：`feature-plans/upstream-sync-2026-09/`。

### 2026-09-29 Cursor Rules 与 Memory Bank 多人模式

- `.cursor/rules` 7 条（Memory Bank / Feature Planning / DEPLOYMENT 联动 / 开发环境 / 上游同步 / 回复风格 / 模块文档）+ `memory-bank-merge` skill。
- memory-bank 纳入 git：OWNERS、个人分片模板、预算脚本、pre-commit 拦截、CI owner 校验 + gitleaks、CODEOWNERS。

### 2026-09-28 宿主机 DNS 修复

- 根因：Tailscale `ts-input` 链丢弃 `100.64.0.0/10` 的非 tailscale0 流量，误伤阿里内网 DNS。
- 修复：`ts-input` 首位放行 eth0 的 `100.100.0.0/16`，`tailscale-aliyun-allow.timer` 每 30 秒补回。

### 2026-06-23 fork 定制整理

- 提交飞书交互卡片与会话级设置、子代理 thinking 开关、容器内 cursor-agent、追踪 `config.yaml`；随后的 `/sessions` 多会话、进度卡片、子任务实时 token 以 WIP 形式留在 `legacy/fork-2026-06`。

### 2026-06-11 ~ 06-16 飞书指挥 Cursor 远程编程

- `/repo` 工作目录卡片、机器人自定义菜单（bot.menu_v6）、单聊扁平化、多项目根目录（`/projects` + `/prod_data`）。
- cursor-agent 定向代理 wrapper（走 clash 可见约 120 个模型）；角色模型固定 Opus 4.8 Thinking。

### 2026-06-09 ~ 06-10 基础配置与首次启动

- 豆包 Seed 2.0 Pro 为默认模型；飞书 + 个人微信渠道；国内镜像构建（自建 `local-uv`、阿里云 PyPI / apt、npmmirror）；inotify 调大修复前端 500。

## 待办 TODO

- 切换主工作目录到新 `main` 并重建 dev 容器（步骤与风险见 `feature-plans/upstream-sync-2026-09/progress.md`）。
- 飞书手测清单（同上）。
- 飞书开放平台：回调订阅为长连接方式；事件订阅含 `application.bot.menu_v6`；菜单项 event_key 按 `productContext.md` 配置。
- 恢复 clash 代理，或确认 cursor-agent 直连可接受。
- `/setup` 建管理员（如尚未）；微信拿到真实 iLink 凭证后启用。
- inotify 配置持久化到 `/etc/sysctl.d/`。

## 已知问题 / 风险

- 上游会为 p2p 的每条回复记录话题映射，`store.json` 会随消息量缓慢增长（旧 fork 曾跳过）。
- 飞书卡片命令、菜单不经过 manager 的账号绑定检查；启用 `require_bound_identity` 前需补上。
- 群聊话题下 `/sessions` 仍是 chat 级登记。
- 大跨度同步后的第一次推送会触发上游全套 CI：agent-guidance（上游 `middlewares/AGENTS.md` 继承链超限）、Skill Review、E2E 书签插件失败与 fork 无关；单测 / Blocking IO 失败已通过把部署配置移到 `deploy/fork/` 解决。
