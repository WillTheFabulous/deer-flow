# 上游同步重建（2026-09）

## 总览

fork `WillTheFabulous/deer-flow` 原基线是上游 2026-06-09（PR #3460 附近），落后 1385 个提交；上游大改了 channels（run policy / dedupe / 连接绑定等）。
本次**以上游 `main`（PR #5990，`857eac45`）为基线重建**，只移植仍需要的 fork 功能，并把 fork 代码集中隔离，方便以后定期同步。

- 旧状态：分支 `legacy/fork-2026-06`、tag `fork-legacy-20260928`（含当时未提交的 WIP），均已推到 origin。
- 新状态：`main` 已基于上游重建；**2026-10-08 主目录已切到 `main` 并重建 dev 容器**。worktree `/work/deerflow/deer-flow-sync` 已 detach（不再占用 `main`），`/setup` 与数据迁移完成后再删。

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
| 部署配置 | 追踪在 `deploy/fork/config.yaml`，根目录 `config.yaml` 为本机软链；以新示例为底迁移定制项 | 旧配置 `config_version 11` → 新 `50`；根目录被追踪的配置会让 fork 上的上游单测在收集阶段失败 |
| 飞书身份 | 启用上游 `channel_connections` 绑定到 Web 账号（`require_bound_identity`）；fork 渠道功能适配绑定；旧 `default` 数据用迁移脚本转给管理员 | 新版运行身份是 `safe(open_id)`，不绑定会和 Web、旧数据割裂 |

## fork 补丁清单（同步上游时逐条复核）

### fork 专属文件（上游没有，直接保留）

- `backend/app/channels/fork/`：`store_ext.py`、`workdir.py`、`personas.py`、`memory_view.py`、`progress.py`、`manager.py`（`ForkChannelManager`）、`feishu.py`（`ForkFeishuChannel`）、`pod_bridge.py` + `pod_cards.py`（飞书 POD 工作台，2026-10-10）
  - `ForkChannelManager._store_thread_id`：已绑定时把会话指针镜像到 ChannelStore（上游只写绑定库，否则人设 / 模型 / 工作目录的变更会同步进旧会话的快照）
  - 2026-10-13：`CardActionContext.form_value`（`_on_card_action` 从 lark 回调的 `action.form_value` 取）；`pod_cards` 给带 `form` 的待审项画卡片 JSON 1.0 表单容器（输入框 + 提交按钮），`pod_decide` 把意见转成 `review decide --note`。用于选款里的「修改」，不碰上游文件
  - 2026-10-14：裂变方向挑选卡（`build_pod_directions_card`，动作 `pod_plan_view` / `pod_plan_pick` / `pod_plan_confirm` / `pod_plan_reject`，调 `podctl ui directions` 与 `podctl directions …`）；待审项支持 `links` 跳转按钮。只在 fork 文件里，经 `POD_CARD_ACTIONS` 进 `feishu.py` 的 `_CARD_ACTIONS`，不碰上游文件
  - 2026-10-14（同日）：挑选卡改成根节点 form 里的勾选清单（`checker` `pick_Vxxxx`，`pod_plan_confirm` 从 `form_value` 取勾选转 `--pick`）；方向记录卡（`build_pod_history_card`，动作 `pod_history` → `podctl ui history`，表单输入框 `hint_Fxx` 由 `pod_plan` 转成 `plan --hint=…`）；工作台固定按钮加「方向记录」。仍只改 fork 文件，复用 2026-10-13 的 `CardActionContext.form_value`
  - 2026-10-14（同日）：`_review_elements` 对两张及以上图片的待审项改成「标题 → 图片并排 → 说明」（选款的原图在左），抽出 `_image_row`。只改 fork 文件
  - 2026-10-15：已选款卡片在引擎给了 `table_url` 时加「打开选款表」链接按钮（新 `_link_button`，按钮带 `url`、只认 https）；同日工作台固定按钮在「已选款」后加「选款表」（`_table_link` 两处共用）。只改 fork 文件
  - 2026-10-14（同日）：任务列表卡（`build_pod_tasks_card`，动作 `pod_tasks` → `podctl ui tasks`；已完成任务的「继续裂变」表单复用 `_plan_form`，`pod_plan` 带 `back=tasks` 时回到任务列表）；工作台固定按钮「方向记录」换成「任务列表」，方向记录卡改名任务明细卡（动作仍是 `pod_history`）。只改 fork 文件，`feishu.py` 不动
  - 2026-10-16：挑选卡按引擎给的 `section` 分「小变化 / 大变化」两段（`_direction_rows`：换段插段标题、段内不加分割线，旧批次照旧逐个加分割线）；`pod_plan` 不再要求、也不再传 `limit`（方向数由引擎按可变字段定，删掉 `_plan_limit` / `_PLAN_LIMIT_MAX`）；表单 fallback 文案去掉「8 个」。只改 fork 文件与 `test_channels_fork_pod.py`
  - 2026-10-19：挑选卡表单上面加「全选 / 全不选」（引擎视图给 `pick_actions`，渠道用 `_step_button` 画在表单外）；新动作 `pod_plan_pick_all`（`_batch_id` + `_flag` 校验）→ `podctl directions pick B all [--off]` 后原地刷新挑选卡。只在 fork 文件里，`POD_CARD_ACTIONS` 随之多一项。
  - 2026-10-20：任务列表卡的任务项支持 `links`（「候选表」，用 `_table_link` 画成只认 https 的链接按钮，排在这个任务的按钮前面；「下载压缩包」同日随压缩包取消）；新动作 `pod_accept`（`_SEED_RE` `^S\d+$` 校验）→ `podctl table accept --seed S --by … --detach --notify` 后回到任务列表。只在 fork 文件里，`POD_CARD_ACTIONS` 随之多一项
  - 2026-10-20：新动作 `pod_scenes`（任务列表「出套图（n 款）」，`_SEED_RE` 校验）→ `podctl scenes start --seed S --by …`，结果经 `_scenes_notice` 写成提示后回到任务列表；fork 测试 `TestPodScenes`。只在 fork 文件里，`POD_CARD_ACTIONS` 随之多一项，不碰上游文件
  - 2026-10-21：`pod_decide` 的提示带审核对象的名字（读 `review decide` 输出的 `label`，如「已处理 RV0021（IMG00042_三伙伴开怀大笑）」），`_pod_send_originals` 的提示用 `ui originals` 的 `label`；引擎没给时退回编号。只改 fork 文件里的两处提示文字，fork 测试新增 2 个，不碰上游文件
  - 2026-10-23：`ForkFeishuChannel._event_handler_builder()` 在上游挂接点之后多注册 `register_p2_drive_file_bitable_record_changed_v1`（多维表格记录变更，候选表最底下的「▶ 选款完成」控制行）；`pod_cards._on_bitable_record_changed` 只留 `record_edited` 且改后值为勾选的记录（文件 / 表 / 记录 / open_id 都过正则），按 (file_token, table_id) 防抖 2 秒合并后调 `podctl table control --file --table --records --by feishu:<open_id>`，是不是控制行由引擎读回判断；`stop()` 多调 `_pod_bitable_stop()` 丢掉等着的防抖任务。fork 测试 `TestPodBitableEvents`，`test_channels_fork.py` 的注册断言多一项。同步时核对 lark-oapi 仍有该注册方法、事件模型仍是 `file_token` / `table_id` / `operator_id.open_id` / `action_list[].after_value[].field_value`。只改 fork 文件，不碰上游文件
  - 2026-10-25：新动作 `pod_shelf`（历史任务卡：翻页 / 刷新按钮带 `q`，根节点搜索表单 `shelf_q` 提交时以框里的字为准，去控制字符、≤ 20 字，写成 `--query=…` 一个参数）→ `podctl ui shelf`；`pod_archive`（任务列表「归档」，`_SEED_RE` 校验）→ `podctl shelf archive S --by …` 后回到任务列表；`pod_restore`（「调出」，`back=shelf` 时回历史任务卡，其余回任务列表）→ `podctl shelf restore S --by …`。`build_pod_shelf_card`（灰色卡，搜索框 `default_value` 留上次的字），任务段落抽成 `_task_item_elements` 与任务列表卡共用。fork 测试 `TestPodShelf`；只在 fork 文件里，`POD_CARD_ACTIONS` 随之多三项，不碰上游文件
  - 2026-10-26：新动作 `pod_dispatch`（任务列表「选中的先出套图」，`_seed_id` 校验）→ `podctl table dispatch --seed S --by … --detach --notify`，走 `_pod_background(back=tasks)`；`pod_downloaded`（「B… 已下载」，`_seed_id` + `_batch_id` 校验，`back=shelf` 回历史任务卡、其余回任务列表）→ `podctl scenes downloaded --seed S --batch B --by … --source card`，提示由 `_downloaded_notice` 拼（已标 / 之前标过 / 当场归档 / `shelf_block`）。候选表记录变更事件照旧交给 `table control`，按钮行 / 交付包行由引擎判断。fork 测试 `TestPodDispatch`、`TestPodDownloaded`；只在 fork 文件里，`POD_CARD_ACTIONS` 随之多两项，不碰上游文件
  - 2026-10-26 晚：`pod_cards` 两句排队提示跟着引擎的多工位出图 worker 改：确认方向时 `directions_queued` 写「已排进正在出图的队列，有空的工位就接着出」，选款「修改」排队写「正在出图的 worker 有空的工位就先改它」（原来都是「这一轮结束后接着…」）；`test_channels_fork_pod.py` 跟着改一处断言。只改 fork 文件，不碰上游文件
  - `ForkFeishuChannel` 混入 `PodCardsMixin`：`/pod`、菜单 `POD` / `POD_CHAT`、`pod_*` 按钮、`_patch_card`；`/pod` 不进 `KNOWN_CHANNEL_COMMANDS`（不动上游命令不变式链）
  - 2026-10-12 起 `ForkFeishuChannel` 覆盖 `start()` / `stop()`：先调上游，`_running` 为真才起 POD 定时器（`_pod_timer_start`，每 60 秒 `podctl tick --auto`），`stop()` 先取消定时器（2026-10-23 起再丢掉候选表事件的防抖任务）再调上游。同步时核对上游 `start()` 仍以 `_running` 表示启动成功、`stop()` 签名不变。发原图复用上游 `send_file(OutboundMessage, ResolvedAttachment)`（`is_image=False` 走文件消息），同步时核对其签名与返回值（bool）
- `backend/tests/test_channels_fork.py`、`backend/tests/test_channels_fork_pod.py`、`backend/tests/test_subagent_thinking_config.py`、`backend/tests/test_fork_migrate_default_owner.py`、`backend/tests/test_fork_personas.py`（2026-10-14：开着 host bash 时 `deploy/fork/agents/*` 不能设 `skills` 白名单，否则上游 #5077 拒绝运行；同步上游时若该规则变化，复核这条）、`backend/tests/test_fork_migrate_sqlite_to_postgres.py`（2026-10-29，主库迁移脚本；Postgres 用例要 `DEERFLOW_TEST_POSTGRES_URL`，CI 里跳过）、`backend/tests/test_fork_onboard_member.py`（2026-10-31，新成员开通脚本：SQLite 临时库跑真实用户库与连接码表）
- `docker/docker-compose.cursor-agent.yaml`（2026-10-31 起还管：`frontend` 归 `web` profile 默认不启动、nginx `depends_on: !override [gateway]`、gateway 内存上限 6G；同步上游时若 dev compose 的 nginx 依赖或 frontend 服务名变了，复核这几行）、`docker/cursor-agent-setup.sh`、`scripts/fork/rebuild-dev.sh`（国内网络重建 dev 栈；2026-10-31 起只在 `DEER_FLOW_FRONTEND=1` 时构建、启动前端）、`scripts/fork/onboard_member.py`（2026-10-31，不经网页开通成员：建账号 + 飞书 `/connect` 码；引用上游 `app.gateway.routers.channel_connections._create_state` / `_STATE_TTL_SECONDS` 与 `SQLiteUserRepository` / `LocalAuthProvider`，同步上游时这几个名字变了测试会红）、`scripts/fork/migrate_default_owner.py`（`default` 数据归到 Web 账号）、`scripts/fork/probe_litellm_models.py`（接入 LiteLLM 模型前的能力探测）、`scripts/fork/backup_pod_blanket.sh`（备份 pod-blanket 业务库，2026-10-13；2026-10-29 起 `PG_USER` 缺省等于库名，`PG_DATABASE=deerflow` 备份 DeerFlow 主库）、`scripts/fork/migrate_sqlite_to_postgres.py`（2026-10-29，DeerFlow 主库 SQLite → Postgres 一次性迁移；复用上游 `init_engine` bootstrap、`Base.metadata`、`AsyncPostgresSaver` / `AsyncPostgresStore`，不改上游文件）
  - 2026-10-13 起 overlay 给 gateway 加 `build.args.UV_EXTRAS: postgres` 与 `environment: UV_EXTRAS=postgres`，装上游 `postgres` extra，供 pod-blanket 连 mooya 的 Postgres；2026-10-29 起 DeerFlow 主库也靠它（asyncpg、psycopg / psycopg-pool、langgraph-checkpoint-postgres）。同步时核对：上游 `Dockerfile` 仍接受 `UV_EXTRAS` 构建参数，`dev-entrypoint.sh` 仍按运行时 `UV_EXTRAS` 执行 `uv sync --locked`，`postgres` extra 仍存在且包含上面几个包
- `deploy/fork/config.yaml`（被追踪的部署配置；根目录 `config.yaml` 是本机软链，上游 `.gitignore` 忽略）
  - 2026-10-29 起 `database.backend: postgres` + `postgres_url: $DATABASE_URL`（主库在 mooya Postgres 的 `deerflow` 库，`sqlite_dir` 留给回滚）。同步上游时：新的 alembic 迁移会在网关启动时直接跑在这个库上，先在测试库 `deerflow_test` 上跑一遍上游 Postgres 用例与 bootstrap；`langgraph-checkpoint-postgres` 升级时看 `checkpoint_migrations` 有没有新迁移
  - 2026-10-08 起 `scheduler.enabled: true`（原为 pod-blanket 定时巡检；2026-10-12 起巡检改由飞书渠道定时器驱动，此项留给其他定时任务）
  - `auth.local.allow_registration`：2026-10-09 起 `false`（2026 端口对公网开放后关闭自助注册）；2026-10-14 临时改为 `true` 给团队成员注册，注册完改回 `false`
  - 2026-10-10 起 `models` 追加 mooya LiteLLM 块（13 个 gpt-5.6 / deepseek，`$LITELLM_API_KEY`）
- `skills/public/pod-blanket/`：POD 毛毯长任务流水线（SKILL.md + `scripts/podflow` 引擎 + `prompts/` `schemas/` `references/` + 自带 `tests/`）。只是新增目录，不改上游文件；上游 `tests/test_skills_bundled.py` 会校验它的 frontmatter。2026-10-20 起引擎经 HTTP 调 mooya 的机器调用接口出套图（`podflow/mooya.py` / `scenes.py`），依赖只是已有的 httpx，不碰上游依赖文件。2026-10-28 起另经 HTTP 调妙手开放平台做 Temu 上架（`podflow/miaoshou.py` / `temu.py` 等），同样只用 httpx；`podctl temu titles` 读 xlsx 时按需 import 环境里已有的 openpyxl。2026-10-29 起每周选款表（`podflow/books.py` / `table_registry.py` / `week_migration.py`）另调飞书知识库节点新建 / 复制和 Base v3 高级权限 / 角色接口，同样只用 httpx。 2026-10-30 起 Temu 上架统一到 mooya（`podflow/mooya_listing.py`，`listed_table.py` 读 mooya 底账）：只是多调 mooya 服务接口的 `/temu/*`，同样只用 httpx，不改上游文件。 2026-10-31 起精作款周表格（`podflow/crafted.py`）另在飞书知识库建文档节点、把多维表格建 / 复制到它下面，同样只用 httpx，不改上游文件。
- `deploy/fork/agents/pod-blanket/`：`pod-blanket` 人设源文件，安装时复制到 `backend/.deer-flow/agents/`（见 DEPLOYMENT.md「角色与人设」）
- `.cursor/`、`memory-bank/`、`feature-plans/`、`DEPLOYMENT.md`、`.github/CODEOWNERS`、`.github/workflows/memory-bank-guard.yml`、`scripts/memory_bank_budget.sh`、`scripts/hooks/`、`scripts/ci/`

### 上游文件里的挂接点（冲突多发区）

| 文件 | 改动 |
| --- | --- |
| `backend/app/channels/store.py` | 继承 `ChannelStoreExtensionsMixin`；`get_thread_id` 用 `entry.get`；`set_thread_id` 合并写 |
| `backend/app/channels/manager.py` | `_NullStreamObserver` 类 + `_make_stream_observer()`；`_handle_streaming_chat` 中创建观察者 1 处 + 调用 5 处（stream_mode / on_event / 两处 outbound metadata / finish） |
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
| `.env.example` | `SILICONFLOW_API_KEY`、cursor-agent 变量与 `LITELLM_API_KEY`（mooya LiteLLM）说明；2026-10-29 fork 段加 DeerFlow 主库 `DATABASE_URL` 的本机写法（上游 Database 段原有的占位不动） |
| `.pre-commit-config.yaml` | 追加 Memory Bank 本地钩子 |

## 与旧 fork 的已知差异

- 上游为 p2p 每条回复记录话题映射，`store.json` 会缓慢增长（旧 fork 跳过了）。
- 切换人设会开启新会话（旧 fork 是同一会话里直接换）。
- 启用 `require_bound_identity` 后，未绑定用户的卡片、菜单和消息都会被拦住；合成命令会附上绑定身份。绑定后第一次查会话会尝试沿用升级前的 ChannelStore 指针。
- 群聊话题下 `/sessions` 是 chat 级登记。
- 没有移植 Frontend-Layout / Test-Runs 规则（mooya 专属），改为 `Module-Guides` 规则指向上游文档。

## 回滚

- 代码：`git switch legacy/fork-2026-06`（或 `git reset --hard fork-legacy-20260928`），重启 dev 容器。
- 配置与数据：`/work/deerflow/backup-20260928/`（`config.yaml`、`.env`、`frontend/.env`、`extensions_config.json`、`memory-bank/`、`backend/.deer-flow/`）。
