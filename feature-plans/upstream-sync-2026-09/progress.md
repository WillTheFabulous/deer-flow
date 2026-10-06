# 上游同步重建 · 进度
[Last Updated: 2026-10-07]

## 当前阶段

绑定适配、迁移脚本与离线演练已完成，待推送后做切换。**主目录切换、容器重建、`/setup` 与数据迁移尚未执行**（需确认；线上仍跑 `legacy/fork-2026-06`）。

## 已完成

- [x] 宿主机 DNS 修复（Tailscale 放行阿里内网段 + `tailscale-aliyun-allow.timer`）
- [x] 备份：`legacy/fork-2026-06` + tag `fork-legacy-20260928` 已推 origin；`/work/deerflow/backup-20260928/`
- [x] `git fetch upstream`，worktree `/work/deerflow/deer-flow-sync`（`sync/upstream-2026-09` 基于 `857eac45`）
- [x] `feat(dev)`：cursor-agent overlay（`docker-compose.cursor-agent.yaml` + `cursor-agent-setup.sh` + `docker.sh` 自动叠加）；一次性容器内验证 wrapper 三种代理设置
- [x] `feat(subagents)`：`thinking_enabled`（236 个子代理相关测试通过）
- [x] `feat(channels)`：飞书功能包移植到 `app/channels/fork/`，并适配 `channel_connections`（会话指针走绑定库、合成命令附身份、未绑定门禁、旧指针沿用）
- [x] `scripts/fork/migrate_default_owner.py`：`default` 数据归到 Web 账号（预览 / 备份 / 事务更新 / 文件复制不覆盖）
- [x] 离线演练（`/tmp/rehearsal` 副本，未动线上数据）：旧库升到 `0026_mcp_task_lease_tokens`；迁移后管理员可见 7 个旧线程、3 个线程目录、7 条记忆；其他用户访问被拒；飞书 store 指针指向的线程归属正确
- [x] `feat(channels)`：`/status` 显示子任务累计 token（替代 harness 增量上报）
- [x] `chore(config)`：`config.yaml` 迁移到 `config_version 50`，`AppConfig` 加载校验通过；`.env.example` 补变量说明
- [x] Cursor Rules 7 条 + `memory-bank-merge` skill；memory-bank 多人模式基础设施；DEPLOYMENT.md
- [x] 验证：`ruff check` / `ruff format --check` 全量通过；全量单测见下

### 全量单测

- 命令：`DEER_FLOW_HOME=$(mktemp -d) pytest -m "not live" --ignore=tests/blocking_io tests/`（根目录无 `config.yaml`，约 18 分钟）。
- 结果：**21,223 通过、201 跳过、3 失败**。Blocking IO 180 通过；`ruff check` / `ruff format --check` 通过。
- 3 个失败都是 `page.xhtml` 的下载判定（`test_artifacts_router.py` ×2、`test_project_documents_router.py` ×1）：宿主机没有 `/etc/mime.types`，Python 识别不出 `.xhtml`，属于环境差异；相关路由与工具代码相对上游零改动（上游 CI 的 Ubuntu 镜像自带该文件）。

## 下一步

1. 推送 `main`（旧状态由 `legacy/fork-2026-06` 与 tag `fork-legacy-20260928` 保留）。worktree 在切换完成后再删。
2. **切换与重建**（需确认；飞书会离线几分钟）：按 `DEPLOYMENT.md`「上游同步与切换」。
   - 从 `.env` 删除 `UV_INDEX_URL`；`docker/.env` 写 `BIND_HOST=0.0.0.0` 与 `DEER_FLOW_DEV_ALLOWED_ORIGINS`；
   - 自建 `local-uv:0.11.1`；`make docker-stop` → 主目录 `git switch main` → 软链 → `bash scripts/fork/rebuild-dev.sh --reset-venv`。
3. **建号与迁移**（需确认，gateway 停着时跑迁移）：Web `/setup` 建管理员 → `scripts/fork/migrate_default_owner.py <邮箱> --data-dir backend/.deer-flow` 先预览再 `--apply` → 启动 → 飞书单聊发 `/connect <连接码>`。
4. **飞书手测清单**（绑定后）：
   - [ ] 未绑定发 `/repo`：只收到绑定提示，不建会话
   - [ ] `/connect` 后单聊连续对话有上下文
   - [ ] `/repo` 卡片选目录 → 下一条消息落在该目录
   - [ ] `/agent` 卡片切人设 → 「新会话」确认；`/new` 后人设保持
   - [ ] `/sessions` 卡片：切换 / 查看 / 删除 / 新会话（切换后指针在绑定账号下）
   - [ ] `/model` 卡片 → `/status` 显示新模型
   - [ ] `/memory` 卡片三个范围（global 能看到迁移过来的旧事实）
   - [ ] 机器人菜单各项（需飞书后台 event_key）
   - [ ] 角色流水线：卡片显示 todos / 子任务；`/status` 有子任务累计 token
   - [ ] Web 端能看到迁移后的旧线程；旧 software-team 会话继续对话人设不变

## 阻塞项 / 风险

- 容器内 `uv sync --locked` 与阿里云镜像不兼容（lock 记录的是 pypi.org），直连 PyPI 又太慢：必须按上面的改写流程构建，并删除旧 venv 卷。
- clash 代理未运行：cursor-agent 只能看到直连可见的少数模型；角色提示词固定的 `claude-opus-4-8-thinking-high` 可能不可用。切换前需决定启用代理，或把 `CURSOR_AGENT_PROXY` 设空并改角色模型。
- 线上容器依赖的 `gateway-venv` 卷来自旧版本，直接 `restart` 新代码会因依赖不匹配而崩溃——切换必须和重建一起做。
