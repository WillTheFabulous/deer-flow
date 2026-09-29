# 上游同步重建 · 进度
[Last Updated: 2026-09-29]

## 当前阶段

代码移植与验证完成，**等待用户确认 force-push `main`**；主目录切换与容器重建尚未执行（线上仍跑 `legacy/fork-2026-06`）。

## 已完成

- [x] 宿主机 DNS 修复（Tailscale 放行阿里内网段 + `tailscale-aliyun-allow.timer`）
- [x] 备份：`legacy/fork-2026-06` + tag `fork-legacy-20260928` 已推 origin；`/work/deerflow/backup-20260928/`
- [x] `git fetch upstream`，worktree `/work/deerflow/deer-flow-sync`（`sync/upstream-2026-09` 基于 `857eac45`）
- [x] `feat(dev)`：cursor-agent overlay（`docker-compose.cursor-agent.yaml` + `cursor-agent-setup.sh` + `docker.sh` 自动叠加）；一次性容器内验证 wrapper 三种代理设置
- [x] `feat(subagents)`：`thinking_enabled`（236 个子代理相关测试通过）
- [x] `feat(channels)`：飞书功能包移植到 `app/channels/fork/`（49 个 fork 测试 + 717 个渠道相关上游测试通过）
- [x] `feat(channels)`：`/status` 显示子任务累计 token（替代 harness 增量上报）
- [x] `chore(config)`：`config.yaml` 迁移到 `config_version 50`，`AppConfig` 加载校验通过；`.env.example` 补变量说明
- [x] Cursor Rules 7 条 + `memory-bank-merge` skill；memory-bank 多人模式基础设施；DEPLOYMENT.md
- [x] 验证：`ruff check` / `ruff format --check` 全量通过；全量单测见下

### 全量单测

- 命令：临时移走根目录 `config.yaml` 后 `pytest -m "not live" --ignore=tests/blocking_io tests/`（共 21,400 个，耗时约 19 分钟）。
- 结果：**21,202 通过、201 跳过、3 失败**。
- 3 个失败都是 `page.xhtml` 的下载判定（`test_artifacts_router.py` ×2、`test_project_documents_router.py` ×1）：宿主机没有 `/etc/mime.types`，Python 识别不出 `.xhtml`，属于环境差异；相关路由与工具代码相对上游零改动（上游 CI 的 Ubuntu 镜像自带该文件）。

## 下一步

1. **用户确认后推送**：`git -C /work/deerflow/deer-flow branch -f main sync/upstream-2026-09 && git push --force-with-lease origin main`（`main` 旧状态已由 `legacy/fork-2026-06` / tag 保留）。
2. **切换与重建**（需要短暂停机，飞书机器人会离线几分钟）：按 `DEPLOYMENT.md`「上游同步与切换」执行，要点：
   - 从 `.env` 删除 `UV_INDEX_URL`；在 `docker/.env` 写 `BIND_HOST=0.0.0.0`；
   - 自建 `local-uv:0.11.1` 镜像（命令见 DEPLOYMENT.md）；
   - `make docker-stop` → 主目录 `git switch main` → `bash scripts/fork/rebuild-dev.sh --reset-venv`（脚本会临时改写 `uv.lock` 走阿里云镜像构建，构建完自动还原，再 `up --no-build`）；
   - 验证：`docker logs deer-flow-gateway`、`logs/gateway.log` 出现飞书连接成功；Web 2026 可访问。
3. **飞书手测清单**（重建后）：
   - [ ] 单聊发消息，回复正常且连续对话有上下文
   - [ ] `/repo` 卡片选目录 → 下一条消息落在该目录
   - [ ] `/agent` 卡片切人设 → 收到「新会话」确认；`/new` 后人设保持
   - [ ] `/sessions` 卡片：切换 / 查看 / 删除 / 新会话
   - [ ] `/model` 卡片切模型 → `/status` 显示新模型
   - [ ] `/memory` 卡片三个范围
   - [ ] 机器人菜单各项（需飞书后台配置 event_key）
   - [ ] 角色流水线任务：卡片显示 todos / 正在执行的子任务；`/status` 显示子任务累计 token
   - [ ] 旧会话（升级前的 software-team 线程）继续对话时人设不变

## 阻塞项 / 风险

- 容器内 `uv sync --locked` 与阿里云镜像不兼容（lock 记录的是 pypi.org），直连 PyPI 又太慢：必须按上面的改写流程构建，并删除旧 venv 卷。
- clash 代理未运行：cursor-agent 只能看到直连可见的少数模型；角色提示词固定的 `claude-opus-4-8-thinking-high` 可能不可用。
- 线上容器依赖的 `gateway-venv` 卷来自旧版本，直接 `restart` 新代码会因依赖不匹配而崩溃——切换必须和重建一起做。
