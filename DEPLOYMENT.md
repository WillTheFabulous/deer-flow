# DEPLOYMENT（WillTheFabulous/deer-flow fork）

本文件记录这个 fork 在阿里云 ECS 上的部署与运维方式。上游通用说明见 `README.md`、`Install.md`、`backend/docs/CONFIGURATION.md`。
改动触发的更新规则见 `.cursor/rules/DEPLOYMENT-md.mdc`。

## 部署拓扑

日常运行形态是 **dev 栈**（`docker/docker-compose-dev.yaml`，项目名 `deer-flow-dev`，热重载）：

| 容器 | 作用 | 端口 |
| --- | --- | --- |
| `deer-flow-nginx` | 统一入口，反代 frontend 与 gateway | 宿主 `${BIND_HOST:-127.0.0.1}:${PORT:-2026}` |
| `deer-flow-frontend` | Next.js dev server | 3000（仅容器网络） |
| `deer-flow-gateway` | FastAPI Gateway + 内嵌 agent runtime + IM 渠道（飞书 WS） | 8001（仅容器网络） |
| `deer-flow-redis` | stream bridge | 仅容器网络 |

- `scripts/docker.sh` 在 dev 栈自动叠加 `docker/docker-compose.cursor-agent.yaml`（`DEER_FLOW_CURSOR_AGENT=0` 可关闭）：
  - 挂载宿主 `~/.cursor-cli` → `/root/.cursor`，`/work/projects` → `/projects`，`/prod_data` → `/prod_data`；
  - 启动前运行 `docker/cursor-agent-setup.sh` 生成 cursor-agent 定向代理 wrapper，再交给上游 `dev-entrypoint.sh`。
- gateway 挂载 `../backend`（`uvicorn --reload`，**改代码立即作用于线上飞书机器人**）和 `../` → `/app/project`（`config.yaml` 从这里读）。
- 飞书、微信都是出站长连接，不需要公网 IP。

## 启动与重建

### 日常操作

下表里的 `$COMPOSE` 指：`cd docker && DEER_FLOW_ROOT=$(cd .. && pwd) docker compose -p deer-flow-dev -f docker-compose-dev.yaml -f docker-compose.cursor-agent.yaml`。

| 操作 | 命令 |
| --- | --- |
| 改了 `config.yaml` / `.env` | `docker restart deer-flow-gateway` |
| 启动（不重建） | `$COMPOSE up -d --no-build redis frontend gateway nginx` |
| 新增 / 修改挂载 | `$COMPOSE up -d --no-build --force-recreate gateway` |
| 看日志 | `docker logs -f deer-flow-gateway`、`tail -f logs/gateway.log`、`make docker-logs` |
| 停止 | `make docker-stop` |

**不要用 `make docker-start`**：它每次都 `up --build`，会用原始 `uv.lock` 重建镜像（原因见下节）。

### 重建镜像（国内网络）

`backend/uv.lock` 记录的是 pypi.org。设了镜像 `UV_INDEX_URL` 时，`uv sync --locked` 会判定 lock 过期而失败；宿主机直连 files.pythonhosted.org 又基本下不动。所以**不要直接用 `make docker-start` 重建**，改用 fork 脚本：

```bash
# 一次性：自建 uv 源镜像（ghcr.io 太慢；版本与 backend/Dockerfile 的 UV_IMAGE 默认值一致）
docker build -t local-uv:0.11.1 - <<'EOF'
FROM python:3.12-slim
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ uv==0.11.1 \
 && cp "$(command -v uv)" /uv && cp "$(command -v uvx)" /uvx
EOF

# 重建并启动（依赖版本有变化时加 --reset-venv，让新镜像重新填充 gateway-venv 卷）
bash scripts/fork/rebuild-dev.sh --reset-venv
```

脚本流程：临时把 `uv.lock` 改写为阿里云地址 → 带镜像参数构建 gateway / frontend → 用 `git checkout` 还原 `uv.lock` → `up -d --no-build` 启动。
前置条件：`.env` 里**不能有** `UV_INDEX_URL`（它会注入容器，导致启动时 `uv sync --locked` 失败）；`uv.lock` 没有未提交改动。

## 环境变量

### `.env`（容器环境，经 `env_file` 注入 gateway）

| 变量 | 说明 |
| --- | --- |
| `VOLCENGINE_API_KEY` | 火山方舟（豆包，默认模型） |
| `SILICONFLOW_API_KEY` | 硅基流动（GLM-5.2 / Kimi-K2.6 / MiniMax-M2.5） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭证 |
| `CURSOR_API_KEY` | cursor-agent 鉴权 |
| `CURSOR_AGENT_PROXY` | 可选；cursor-agent 代理，默认 `http://host.docker.internal:7890`，写成空值表示直连 |
| `WECHAT_BOT_TOKEN` / `WECHAT_ILINK_BOT_ID` | 个人微信（当前未启用） |

### `docker/.env` 或 shell export（compose 插值 / docker.sh 读取；根目录 `.env` 对它们无效）

| 变量 | 说明 |
| --- | --- |
| `BIND_HOST` | nginx 绑定地址。**局域网访问必须设 `0.0.0.0`**，默认只绑 `127.0.0.1` |
| `DEER_FLOW_DEV_ALLOWED_ORIGINS` | Next.js dev 的来源白名单（逗号分隔的主机名 / IP），默认只有 `127.0.0.1,::1`。**局域网访问要加上宿主机的局域网 IP**（用 Tailscale 访问再加 Tailscale IP），否则 dev 资源和热更新会被拦 |
| `PORT` | 入口端口，默认 2026 |
| `DEER_FLOW_PROJECTS_DIR` / `DEER_FLOW_EXTRA_PROJECTS_DIR` | 项目根目录 bind 源，默认 `/work/projects`、`/prod_data` |
| `DEER_FLOW_CURSOR_AGENT` | 设 `0` 关闭 cursor-agent 叠加 |
| `UV_IMAGE` / `APT_MIRROR` / `NPM_REGISTRY` | 构建镜像源，`rebuild-dev.sh` 已给默认值 |

## 配置文件

- `config.yaml` **纳入版本管理**（上游 `.gitignore` 忽略它，fork 用 `git add -f` 追踪），只写 `$VAR` 引用；当前 `config_version: 50`。
- fork 定制项：`models`（豆包 + 硅基流动）、`loop_detection`（阈值 + `tool_freq_overrides`）、`sandbox`（`allow_host_bash: true` + `mounts`）、`subagents`（6 个角色）、`run_events.backend: db`、`channels`（飞书启用、微信关闭、会话默认开 plan mode 与子代理）。
- 同步上游后：对比新旧示例 `git diff <旧基线> upstream/main -- config.example.yaml`，把新增字段与 `config_version` 手工合入，再复核上述定制项。
  **不要用 `make config-upgrade`**：它用 `yaml.dump` 写回，会删光全部注释；启用 `pii_redaction` 时还会把随机密钥写进被追踪的文件。
- 本地跑测试：先 `export DEER_FLOW_HOME=$(mktemp -d)`（测试默认写 `backend/.deer-flow`，也就是线上数据目录）；全量测试要在根目录没有 `config.yaml` 的 checkout 里跑（上游 CI 没有它，部分测试在导入时就会读取配置）。

## 项目根目录与挂载

每个项目根都要**三处一致**：

1. `docker/docker-compose.cursor-agent.yaml` 的 bind（宿主路径 → 容器路径）；
2. `config.yaml` 的 `sandbox.mounts`（`host_path` = 容器路径，`container_path` = agent 看到的虚拟路径）；
3. 修改后 `--force-recreate gateway`。

当前：`/projects` → `/mnt/projects`、`/prod_data` → `/mnt/prod_data`（均为读写）。`/repo` 会列出每个根的直接子目录（卡片最多显示 20 个）。

## 飞书渠道运维

- 开放平台配置：
  - 事件与回调 → **回调订阅用长连接方式**（否则卡片按钮没有回调）；
  - 事件订阅：`im.message.receive_v1`、`application.bot.menu_v6`（机器人菜单）；
  - 应用能力 → 机器人 → 自定义菜单，菜单项选「事件推送」，event_key：
    `REPO` `AGENT` `SESSIONS` `MEMORY` `MODELS`（出卡片），`STATUS` `NEW` `HELP` `MEMORY_GLOBAL` `MEMORY_DEFAULT` `MEMORY_PERSONA`（发命令），`AGENT_<NAME>`（直接切换人设）；
  - 改完发布新版本。
- 菜单事件只带 open_id：用户要先在单聊里给机器人发过一条消息，fork 才能找回会话。
- 渠道命令：`/new` `/sessions` `/repo` `/agent` `/model` `/models` `/memory` `/status` `/goal` `/bootstrap` `/help`。
- 状态文件：`backend/.deer-flow/channels/store.json`（会话指针、工作目录、人设、模型、会话登记、open_id 映射）。

## 角色与人设

- 角色子代理在 `config.yaml` 的 `subagents.custom_agents`：requirement-analyst / architect-planner / implementer / tester / code-reviewer / integrator-verifier（`thinking_enabled: true`）。
- 人设 = Custom Agent（Web 端创建，按用户隔离；旧的共享目录 `backend/.deer-flow/agents/<name>/` 仍可读）。切换人设会以该人设开启新会话。
- 角色通过 `bash` 调 `cursor-agent`：headless 必带 `--trust`；单条 bash 命令超时 600 秒，长任务用 `nohup` + 轮询。

## 对外端点

fork 没有新增 Gateway 路由。对外只有 nginx 入口（`/` 前端、`/api/*` Gateway、`/api/langgraph/*` agent runtime）。

## 上游同步与切换

- 仓库：`origin` = `WillTheFabulous/deer-flow`，`upstream` = `bytedance/deer-flow`。同步流程见 `.cursor/rules/Upstream-Sync-Protocol.mdc`，fork 补丁清单见 `feature-plans/upstream-sync-2026-09/README.md`。
- **主目录切换必须和容器重建一起做**：线上 `gateway-venv` 卷里是旧依赖，直接切到新代码热重载会崩。
- 切换步骤（飞书机器人会离线几分钟）：
  1. 从 `.env` 删除 `UV_INDEX_URL`；在 `docker/.env` 写 `BIND_HOST=0.0.0.0` 和 `DEER_FLOW_DEV_ALLOWED_ORIGINS=<局域网IP>,<Tailscale IP>`；
  2. `make docker-stop`；
  3. `git switch main`（主目录 `/work/deerflow/deer-flow`）；
  4. `bash scripts/fork/rebuild-dev.sh --reset-venv`；
  5. `logs/gateway.log` 出现飞书连接成功、Web 可访问后，按 `feature-plans/upstream-sync-2026-09/progress.md` 的手测清单验证。
- 回滚：`git switch legacy/fork-2026-06`，重新构建旧镜像；配置与数据备份在 `/work/deerflow/backup-20260928/`。

## 宿主机与网络

- **Tailscale 与阿里内网冲突**：Tailscale 丢弃 `100.64.0.0/10` 的非 tailscale0 流量，阿里内网 DNS（`100.100.2.136/138`）、元数据、NTP 都在这个网段里。
  修复：`/usr/local/sbin/tailscale-aliyun-allow.sh` 在 `ts-input` 链首放行 eth0 的 `100.100.0.0/16`，`tailscale-aliyun-allow.timer` 每 30 秒检查补回（`systemctl status tailscale-aliyun-allow.timer`）。
- PyPI：宿主机直连基本不可用，一律走阿里云镜像。宿主机上给 worktree 装依赖：临时改写 `uv.lock`（同 `rebuild-dev.sh`）后 `UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv sync --frozen`，再 `git checkout -- backend/uv.lock`。
- 镜像：docker.io 走 daemon 的 registry mirror；ghcr.io 慢，用自建 `local-uv`。
- 代理：clash（宿主 7890）目前未运行；cursor-agent 走代理才能看到完整订阅模型列表（直连约 6 个）。
- inotify：前端 dev 需要 `fs.inotify.max_user_watches=524288`（当前为运行时设置，重启失效）。

## 安全清单

- `allow_host_bash: true`：LocalSandbox 不是隔离边界，只适合单用户可信部署。
- `BIND_HOST=0.0.0.0` 会把入口暴露到局域网：先在 `/setup` 建好管理员。
- `/prod_data` 以读写方式挂进 gateway，agent 可以修改其中的文件（包括其他项目）。
- 飞书卡片命令、菜单不经过账号绑定检查；启用 `channel_connections` / `require_bound_identity` 前需要补上。
- 密钥只放 `.env`；`config.yaml`、memory-bank、feature-plans、本文件都进 git，禁止写入真实密钥（CI 的 `memory-bank-guard` 会跑 gitleaks）。
