# Tech Context
[Last Updated: 2026-09-29]

## 技术栈

- Backend：Python 3.12，FastAPI Gateway（内嵌 agent runtime，端口 8001），LangGraph；`uv` 管依赖（`backend/uv.lock`，workspace 成员 harness + extension-api）。
- Frontend：Next.js（pnpm，dev 端口 3000）。反代 nginx（2026）。dev 栈另有 Redis（stream bridge）。
- 数据：SQLite（`backend/.deer-flow/data`）；`run_events` 落库；渠道状态在 `backend/.deer-flow/channels/store.json`。

## Docker（dev 栈，项目名 deer-flow-dev）

- `scripts/docker.sh`（`make docker-*`）：`docker-compose-dev.yaml` + 自动叠加 `docker-compose.cursor-agent.yaml`（`DEER_FLOW_CURSOR_AGENT=0` 可关）。重建镜像只用 `scripts/fork/rebuild-dev.sh`，不用 `make docker-start`。
- gateway 挂载：`../backend`（热重载）、`../`→`/app/project`（读 `config.yaml` 软链 → `deploy/fork/config.yaml`）、`~/.cursor-cli`→`/root/.cursor`、`/work/projects`→`/projects`、`/prod_data`→`/prod_data`。
- nginx 默认只绑 `127.0.0.1`；局域网访问要设 `BIND_HOST=0.0.0.0`，并把局域网 IP 加进 `DEER_FLOW_DEV_ALLOWED_ORIGINS`（Next.js dev 来源白名单）。二者都是 compose 插值变量，写在 `docker/.env` 或执行前 export，根目录 `.env` 对它们无效。
- 线上容器目前仍是 2026-06 的旧 fork 镜像（主目录停在 `legacy/fork-2026-06`），切换步骤见 `feature-plans/upstream-sync-2026-09/progress.md`。

## 宿主机与网络

- 阿里云 ECS。装了 Tailscale，它的 `ts-input` 链会丢弃 `100.64.0.0/10` 的非 tailscale0 流量，误伤阿里内网 DNS / 元数据 / NTP（`100.100.0.0/16`）。
  修复：`/usr/local/sbin/tailscale-aliyun-allow.sh` + `tailscale-aliyun-allow.timer`（每 30 秒补放行规则）。
- 宿主机直连 PyPI（files.pythonhosted.org）基本不可用，阿里云镜像约 12MB/s。
  `uv.lock` 记录的是 pypi.org：设了镜像 `UV_INDEX_URL` 时 `uv sync --locked` 会失败，做法见 `DEPLOYMENT.md`。
- docker.io 已配 registry mirror；ghcr.io 慢（用自建 `local-uv:<ver>` 镜像规避）。
- clash 代理（宿主机 7890）目前**未运行**；cursor-agent 默认走它才能看到完整订阅模型列表。

## 外部服务与环境变量（`.env`）

- `VOLCENGINE_API_KEY`（豆包）、`SILICONFLOW_API_KEY`（GLM / Kimi / MiniMax）、`FEISHU_APP_ID` / `FEISHU_APP_SECRET`、`CURSOR_API_KEY`、`CURSOR_AGENT_PROXY`（可选，空值 = 直连）、`WECHAT_*`（未启用）。
- 旧 `.env` 里的 `UV_INDEX_URL` 在切换到新版前必须删掉，否则容器启动时 `uv sync --locked` 连续失败，gateway 起不来。
- 宿主 inotify 已调到 `max_user_watches=524288`（运行时设置，重启失效）。
