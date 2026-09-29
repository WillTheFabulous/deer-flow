# 上游同步重建（2026-09）

## 总览

fork `WillTheFabulous/deer-flow` 原基线是上游 2026-06-09（PR #3460 附近），落后 1385 个提交；上游大改了 channels（run policy / dedupe / 连接绑定等）。
本次**以上游 `main`（PR #5990，`857eac45`）为基线重建**，只移植仍需要的 fork 功能，并把 fork 代码集中隔离，方便以后定期同步。

- 旧状态：分支 `legacy/fork-2026-06`、tag `fork-legacy-20260928`（含当时未提交的 WIP），均已推到 origin。
- 新状态：分支 `sync/upstream-2026-09`（worktree `/work/deerflow/deer-flow-sync`），确认后重置为 `main`。

## 目标与范围

- 跟上上游最新版；fork 功能行为与升级前一致（或按上游新机制等价实现）。
- 不在本次范围：容器重建与主目录切换（单独执行，见 progress）；生产栈（`make up`）。

## 关键决策

| 主题 | 决策 | 原因 |
| --- | --- | --- |
| 同步方式 | 以上游为准重建，而不是 merge | channels 冲突面过大，重建更干净 |
| fork 代码位置 | `backend/app/channels/fork/` + 上游文件最小挂接点 | 以后同步只需复核挂接点 |
| 飞书单聊扁平化 `flatten_p2p` | 废弃 | 上游已原生：p2p `topic_id=None` + 行内回复 |
| 人设 | 沿用上游「agent 钉在线程上」；`/agent <名>` → `/agent use <名>`；`/new` 沿用人设；旧线程回落会话人设 | 上游刻意禁止同一线程中途换 agent |
| 会话模型 | 注入 `run_context.model_name`，注入前校验仍在配置中 | 网关按模型白名单校验，失效模型会 400 |
| 记忆读取 | 走上游 `get_memory_manager().get_memory()` | 上游改为可插拔记忆后端 |
| 子任务 token 实时上报 | 不移植 harness 层增量上报；`/status` 读 task 事件的 usage | 上游已发布实时快照，父 run 行只在结束时上报是刻意设计 |
| cursor-agent | 改为可选叠加文件 `docker-compose.cursor-agent.yaml` | 符合上游 overlay 模式，不改上游 compose / entrypoint |
| config.yaml | 继续纳入版本管理，以新示例为底迁移定制项 | 旧配置 `config_version 11` → 新 `50` |

## fork 补丁清单（同步上游时逐条复核）

### fork 专属文件（上游没有，直接保留）

- `backend/app/channels/fork/`：`store_ext.py`、`workdir.py`、`personas.py`、`memory_view.py`、`progress.py`、`manager.py`（`ForkChannelManager`）、`feishu.py`（`ForkFeishuChannel`）
- `backend/tests/test_channels_fork.py`、`backend/tests/test_subagent_thinking_config.py`
- `docker/docker-compose.cursor-agent.yaml`、`docker/cursor-agent-setup.sh`、`scripts/fork/rebuild-dev.sh`（国内网络重建 dev 栈）
- `config.yaml`（强制追踪，上游 `.gitignore` 忽略）
- `.cursor/`、`memory-bank/`、`feature-plans/`、`DEPLOYMENT.md`、`.github/CODEOWNERS`、`.github/workflows/memory-bank-guard.yml`、`scripts/memory_bank_budget.sh`、`scripts/hooks/`、`scripts/ci/`

### 上游文件里的挂接点（冲突多发区）

| 文件 | 改动 |
| --- | --- |
| `backend/app/channels/store.py` | 继承 `ChannelStoreExtensionsMixin`；`get_thread_id` 用 `entry.get`；`set_thread_id` 合并写 |
| `backend/app/channels/manager.py` | `_NullStreamObserver` 类 + `_make_stream_observer()`；`_handle_streaming_chat` 中 stream_mode / on_event / 两处 outbound metadata / finish 共 5 处调用 |
| `backend/app/channels/feishu.py` | `_build_event_handler` 拆出 `_event_handler_builder()` |
| `backend/app/channels/service.py` | 注册表 `feishu` 指向 `ForkFeishuChannel`；构造 `ForkChannelManager` |
| `backend/app/channels/commands.py` | `KNOWN_CHANNEL_COMMANDS` 加 `/model` `/repo` `/sessions` |
| `backend/app/channels/telegram.py` | 注册同样 3 个命令（上游不变式） |
| `backend/packages/harness/deerflow/skills/slash.py`、`contracts/slash_skill_contract.json`、`frontend/src/core/skills/slash.ts` | 保留 slash 名加 `model` `repo` `sessions`（跨语言契约） |
| `backend/packages/harness/deerflow/config/subagents_config.py` | `thinking_enabled` 字段 + `get_thinking_for()` |
| `backend/packages/harness/deerflow/subagents/config.py` | `SubagentConfig.thinking_enabled` + `resolve_subagent_thinking_enabled()` |
| `backend/packages/harness/deerflow/subagents/registry.py` | 自定义角色透传 + 覆盖 |
| `backend/packages/harness/deerflow/subagents/executor.py` | 按 thinking 开关创建模型；装配描述用请求值 |
| `scripts/docker.sh` | `_refresh_compose_cmd` 叠加 cursor-agent overlay |
| `config.example.yaml` | `thinking_enabled` 两行示例注释 |
| `.env.example` | `SILICONFLOW_API_KEY` 与 cursor-agent 变量说明 |
| `.pre-commit-config.yaml` | 追加 Memory Bank 本地钩子 |

## 与旧 fork 的已知差异

- 上游为 p2p 每条回复记录话题映射，`store.json` 会缓慢增长（旧 fork 跳过了）。
- 切换人设会开启新会话（旧 fork 是同一会话里直接换）。
- 飞书卡片命令、菜单不经过账号绑定检查（与旧 fork 一致；启用 `require_bound_identity` 前需补）。
- 群聊话题下 `/sessions` 是 chat 级登记。
- 没有移植 Frontend-Layout / Test-Runs 规则（mooya 专属），改为 `Module-Guides` 规则指向上游文档。

## 回滚

- 代码：`git switch legacy/fork-2026-06`（或 `git reset --hard fork-legacy-20260928`），重启 dev 容器。
- 配置与数据：`/work/deerflow/backup-20260928/`（`config.yaml`、`.env`、`frontend/.env`、`extensions_config.json`、`memory-bank/`、`backend/.deer-flow/`）。
