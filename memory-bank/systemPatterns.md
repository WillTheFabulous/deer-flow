# System Patterns
[Last Updated: 2026-10-07]

## 1. 技术架构与运行拓扑

- 飞书 WS 长连接 → gateway 进程内 `ChannelService` → `ForkChannelManager` → Gateway 的 LangGraph 兼容 API（nginx `/api/langgraph/*` → `/api/*`）→ `lead_agent` → `task` 工具 → 角色子代理 → `bash` → `cursor-agent`。
- 渠道地址：Docker 内由 compose 注入 `DEER_FLOW_CHANNELS_LANGGRAPH_URL` / `DEER_FLOW_CHANNELS_GATEWAY_URL`，`config.yaml` 不写 `langgraph_url` / `gateway_url`。
- 上游把 IM 请求的 run 身份解析为 `_channel_storage_user_id(msg)`：已绑定是 Web 账号 id，未绑定是 `safe(open_id)`。它同时决定文件桶和记忆桶。本部署 `channel_connections.require_bound_identity: true`，未绑定请求在 manager 入口被拒。

## 2. 目录结构与 fork 挂接点

- fork 专属代码：`backend/app/channels/fork/`（`store_ext` / `workdir` / `personas` / `memory_view` / `progress` / `manager` / `feishu`）、`docker/docker-compose.cursor-agent.yaml`、`docker/cursor-agent-setup.sh`、`scripts/fork/rebuild-dev.sh`、`scripts/fork/migrate_default_owner.py`；fork 测试 `backend/tests/test_channels_fork.py`、`test_subagent_thinking_config.py`、`test_fork_migrate_default_owner.py`。
- 上游文件里的挂接点（同步时逐条复核，完整清单见 `feature-plans/upstream-sync-2026-09/README.md`）：
  - `app/channels/store.py`：继承 `ChannelStoreExtensionsMixin`；`get_thread_id` 用 `entry.get`；`set_thread_id` 合并写。
  - `app/channels/manager.py`：`_NullStreamObserver` + `_make_stream_observer()`，`_handle_streaming_chat` 里创建观察者 1 处 + 调用 5 处。
  - `app/channels/feishu.py`：`_event_handler_builder()` 拆出 builder。
  - `app/channels/service.py`：注册表 `feishu` → `app.channels.fork.feishu:ForkFeishuChannel`；构造 `ForkChannelManager`。
  - `app/channels/commands.py` / `telegram.py` / `deerflow/skills/slash.py` / `contracts/slash_skill_contract.json` / `frontend/src/core/skills/slash.ts`：登记 `/model` `/repo` `/sessions`。
  - `deerflow/config/subagents_config.py`、`subagents/config.py|registry.py|executor.py`：`thinking_enabled`。
  - `scripts/docker.sh`：`_refresh_compose_cmd` 叠加 cursor-agent overlay。

## 3. IM 渠道模式

- **store 扩展字段**（按 `channel:chat_id`，不分 topic）：`workdir` / `workdir_history`（MRU 8）/ `agent`（人设展示缓存）/ `model` / `sessions{thread_id: {title, agent, model, workdir, created_at, updated_at}}`（上限 50）；`channel:user:<open_id>` → `chat_id`。所有读写持 `_lock`。
- **人设**：真正生效的是线程钉住的 agent（上游 `_thread_agent_names` + 线程 metadata `channel_agent_name`）。fork：`/agent <名>` 改写为 `/agent use <名>`；`_create_thread` 未指定 agent 时沿用 store 里的人设（校验失败则回落默认）；未钉住的线程回落该会话存的人设。
- **模型**：`store.model` 仅在仍在 `config.models` 中时注入 `run_context["model_name"]`（网关按白名单校验，不在白名单会直接 400）。
- **工作目录**：消息前缀 `[当前工作目录：X。未明确指定其他路径时…]`；`normalize_workdir` 只接受 `sandbox.mounts` 各根的直接子目录，拒绝穿越。
- **会话**：登记在 ChannelStore；「当前会话」指针走 `_lookup_thread_id` / `_store_thread_id`（已绑定在连接库，未绑定在 store）。删除当前会话后切到剩余最近的一条，没有则新开。绑定后第一次查找会尝试沿用 store 里的旧指针（owner 读得到才写入连接库，每个会话只试一次）。卡片切换发 `/sessions _gcswitch`，卡片删除先本地移除再发 `_gcdelete`；两者都只接受 `fork_source=card_action`。
- **流式进度**：`StreamProgressObserver` 追加 `custom` 流模式；只有 `values` / `custom` 重算进度，消息块只刷新活动时间。todos / 子任务写入 outbound metadata（`fork_progress_todos` / `fork_progress_steps`）；子任务首次失败发 `fork_notification`（仅飞书）。`ForkChannelManager._handle_streaming_chat` 退出时 `close()`，避免最终回复发送失败后快照一直停在 running。`LiveRunRegistry` 供 `/status`（含子任务累计 token）。
- **飞书**：裸卡片命令在上游 `_on_message` 之前拦截，并记录 p2p 的 open_id 映射。卡片与菜单按 `ChatIdentity` 构建（归属 owner、当前线程读连接库）；要求绑定且未绑定时只回绑定提示。合成命令先 `_attach_connection_identity` 再投递。卡片回调在 lark 线程里同步执行，身份解析投到主循环并限时 2 秒。`/repo` 的目录扫描在线程池里做。

## 4. 子代理与角色流水线

- `subagents.custom_agents`：requirement-analyst / architect-planner / implementer / tester / code-reviewer / integrator-verifier，全部 `thinking_enabled: true`，超时 1800–7200 秒。
- `thinking_enabled`（fork 字段）：`CustomSubagentConfig` 与 `agents.<name>` 覆盖均可设；`resolve_subagent_thinking_enabled()` 在模型不支持 thinking 时回落关闭（旧式模型配置下 `create_chat_model` 会直接抛错）。
- 渠道会话默认：`is_plan_mode: true`（todos 可见）、`subagent_enabled: true`（否则无法委派角色）、`recursion_limit: 250`（上限 `max_recursion_limit` 1000）。
- 长任务：`bash` 单命令有超时（`sandbox.bash_command_timeout` 默认 600 秒）→ 角色提示词内置 `nohup … &` + 轮询日志模式。

## 5. 模型与配置约定

- 部署配置是被追踪的 `deploy/fork/config.yaml`，根目录 `config.yaml` 是指向它的本机软链（上游 gitignore 忽略；fork CI 因此看不到它）；只写 `$VAR`；启用块放在对应注释示例块之前；fork 定制项：models、loop_detection、sandbox（host bash + mounts）、subagents、run_events、channel_connections、channels。
- `models` 第一个即默认模型（title / summarization / memory 在 `model_name: null` 时复用）。
- 豆包 thinking 走 `extra_body.thinking.type`；硅基流动走 `extra_body.enable_thinking`；MiniMax-M2.5 只能开 thinking。
- 同步上游后对比新旧 `config.example.yaml` 手工合入新字段与 `config_version`，再复核 fork 定制项；**禁用 `make config-upgrade`**（`yaml.dump` 写回会删光注释）。fork 只加可选字段时不改 `config_version`。

## 6. Cursor CLI 驱动模式

- 容器启动时 `cursor-agent-setup.sh` 生成 `/usr/local/bin/cursor-agent` wrapper（先 `rm -f`，防止写穿软链）；`CURSOR_AGENT_PROXY` 覆盖代理，空值 = 直连。
- headless 必带 `--trust`（或 `--force`）；规划用 `--plan`（只读）；规划与实现复用同一 Cursor 会话（`cursor-agent create-chat`，不用 `--worktree`）。
- 可见模型取决于网络路径：直连约 6 个，走代理约 120 个。

## 7. 上游同步模式

- 备份 tag → `git fetch upstream` → 临时 worktree 以 `upstream/main` 为基线重建 → 逐项移植提交 → lint + 全量单测（根目录无 `config.yaml`、临时 `DEER_FLOW_HOME`）→ 用户确认后 force-with-lease → 主目录切换与容器重建单独做。
- 上游不变式要在实现侧满足：`KNOWN_CHANNEL_COMMANDS` ⊆ Telegram 注册命令 ⊆ `RESERVED_SLASH_SKILL_NAMES`（并同步 contracts 与前端）。
- 国内重建镜像用 `scripts/fork/rebuild-dev.sh`：`uv.lock` 临时改写为阿里云源 + 镜像 `UV_INDEX_URL` 构建 → 还原 lock → `up --no-build`；运行时容器里不能有 `UV_INDEX_URL`。

## 8. 经验教训速查

### 后端

- `tests/conftest.py` 会 mock 掉 `deerflow.subagents.executor`：可测逻辑放到不被 mock 的模块（如 `subagents/config.py`）。
- 上游 CI 不带 `config.yaml`：全量测试要在根目录没有 `config.yaml` 的 checkout 里跑，否则导入期读取配置的模块会因 `$VAR` 缺失报错。
- 测试默认写 `backend/.deer-flow`：宿主机跑测试一律 `DEER_FLOW_HOME=$(mktemp -d)`，否则在主目录里会污染线上数据。
- 飞书卡片的构建涉及文件 IO：在主事件循环里要用 `asyncio.to_thread`；在 lark 回调线程里可以同步执行。

### 运维

- 对软链做 `cat >` 会写穿到链接目标：生成 wrapper 前必须 `rm -f`。
- `config.yaml` 不在 reload 监听范围：改完 `docker restart deer-flow-gateway`；新增挂载必须 `--force-recreate`。
- 宿主机 `cp` 被别名成 `cp -i`，非交互下不会覆盖：还原仓库文件用 `git checkout -- <file>`。
- 宿主机 DNS 全挂时先查 Tailscale 的 `ts-input` 链和 `tailscale-aliyun-allow.timer`。
