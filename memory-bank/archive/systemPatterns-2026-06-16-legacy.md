<!-- 归档快照：整理为 canonical 结构前的原文（2026-06-16 前后的内容，2026-09-29 归档）。只读，按需追溯。 -->

# System Patterns

## 渠道网络地址解析（重要）
- `ChannelService._resolve_service_url` 顺序：`config.yaml` 值 → 环境变量 → 默认 `localhost:8001`。
- 模式：`config.yaml` **不写死** `langgraph_url`/`gateway_url`：
  - Docker：用 compose 注入的 `http://gateway:8001`；
  - 本地 `make dev`：回退 `localhost:8001`。
- 文件：`backend/app/channels/service.py`。

## 渠道启用模式
- 仅 `channels.<name>.enabled: true` 才启动；只填凭证不 enable 会被跳过（warning）。
- 个人微信凭证键为 `bot_token`；无 token 时需 `qrcode_login_enabled: true`。

## 配置文件约定
- `config.yaml` 由 `config.example.yaml` 复制；保留原注释示例块（全注释，不影响解析）。
- 启用项放对应 section 顶部，示例块紧随其后。
- 模型列表第一个即默认模型（title/summarization/memory 在 `model_name: null` 时复用）。

## 密钥处理
- `config.yaml` 用 `$VAR` 引用，真实值放 `.env`（gitignored）。
- 生产密钥由 `deploy.sh` 自动生成并持久化到 `.deer-flow/`。

## 国内网络构建加速（recurring）
- ghcr.io 拉取极慢（uv 镜像）；daocloud/NJU 等 ghcr 镜像 manifest 通但 blob 回源、卡死。
- 可靠做法：自建 `local-uv:<ver>` —— `FROM python:3.12-slim; pip install uv==<ver>`（阿里云 PyPI），
  `cp $(which uv) /uv && cp $(which uvx) /uvx`，再用 `UV_IMAGE=local-uv:<ver>` 构建。
- 同时传 `UV_INDEX_URL`（阿里云 PyPI）、`APT_MIRROR=mirrors.aliyun.com`、`NPM_REGISTRY=npmmirror`。
- docker.io 基础镜像由 daemon `registry-mirrors` 已加速；ghcr.io 不在其列。

## 前端 dev 文件监听
- Next.js/Turbopack dev 需较高 inotify 配额；宿主默认 8192 会致前端 500（OS file watch limit）。
- 解决：`sysctl -w fs.inotify.max_user_watches=524288 fs.inotify.max_user_instances=1024`（运行时，重启失效）。

## IM 占位凭证副作用
- 飞书占位 app_id：WS 报 `1000040346 app_id is invalid`，仅偶发错误日志。
- 微信占位 bot_token：iLink `getupdates` 200 后立即重轮询，刷爆日志 —— 无真实凭证务必 `enabled: false`。
- gateway dev 日志在 `logs/gateway.log`（容器内 `exec >`），重启会清空。

## Cursor CLI 驱动模式（飞书远程编程）
- 链路：飞书 → lead_agent → task 工具 → 角色子代理（custom_agents）→ bash → `cursor-agent -p [--force]`。
- 角色：PM（只读+写文档）/ 工程师（全工具）/ 测试（全工具）/ 评审（只读+bash，cursor-agent 不带 --force）。
- 鉴权：`CURSOR_API_KEY` 经 `.env` → compose env_file → 容器环境 → bash 子进程继承。
- CLI 安装布局：`~/.cursor-cli/versions/<ver>/cursor-agent` + `bin/` 相对软链（宿主/容器两侧均有效）；
  官方 install 脚本实际装到 `~/.local/share/cursor-agent`，我们自定义为挂载目录以持久化。
- downloads.cursor.com 直连可达（~2.3MB/s）；走 clash 代理反而 SSL_ERROR_SYSCALL。
- 长任务：bash 单命令 600s 硬超时（local_sandbox.py subprocess timeout）→ 提示词内置
  `nohup ... > /mnt/projects/.tasks/<id>.log &` + `kill -0` 轮询模式，cursor 进程本身不受限。
- IM 渠道默认 `subagent_enabled: false`（manager.py DEFAULT_RUN_CONTEXT）→ 必须在
  channels.session.context 显式开 true，否则飞书消息无法触发角色委派。

## cursor-agent 代理与模型（2026-06-11）
- 模型可见性取决于网络路径：直连仅 6 个（composer/grok/kimi）；经 clash 代理约 120 个（GPT-5.x/Codex、Claude Opus/Fable/Sonnet、Gemini 等）。
- 定向代理 wrapper：dev-entrypoint 生成 `/usr/local/bin/cursor-agent`（设 HTTP(S)_PROXY 后 exec 真实二进制），
  只代理 cursor-agent，不污染 gateway 全局环境（豆包 Ark、飞书 WS 保持直连）。
- 陷阱：对已是软链的目标 `cat >` 会写穿到链接目标——必须先 `rm -f` 再写 wrapper；
  曾把 922B 的 cursor-agent 启动器覆盖成 wrapper 自身，造成无输出死循环（status/list-models 全挂）。
- headless 必带 `--trust`（或 `--force`），否则卡在目录信任交互提示。
- config.yaml 不在 uvicorn --reload 监听目录（watch 的是 /app/backend）——改完要 `docker restart deer-flow-gateway`。
- `docker restart` 保留 exec 安装的 git/wrapper；`--force-recreate` 丢失（git 需重装，wrapper 由 entrypoint 重生成）。

## 会话工作目录（/repo 菜单，2026-06-11）
- 数据：ChannelStore（channels/store.json）按 `channel:chat_id` 存 `workdir` + `workdir_history`（MRU，上限 8）；
  `set_thread_id` 已改为合并写，避免覆盖扩展字段。
- 注入：manager._handle_chat 在消息前注入 `[当前工作目录：X ...]` 文本前缀（模型可见，零侵入）。
- 飞书 UX：裸 `/repo` 由渠道层拦截发交互卡片（最近使用置顶 + 全部目录按钮 + 清除）；
  按钮回调走 `register_p2_card_action_trigger`（与事件订阅同一 WS 长连接），
  响应 `P2CardActionTriggerResponse({toast, card:{type:"raw",data:卡片dict}})` 实现 toast + 卡片原地更新。
- 通用回退：`/repo <名称|路径>` / `/repo clear` 文本式（所有渠道可用，feishu 也支持）；`/status` 显示当前目录。
- 目录扫描：app/channels/workdir.py 从 sandbox.mounts[0] 推导（容器实际路径 /projects ↔ 虚拟路径 /mnt/projects），
  只允许挂载目录的直接子目录，拒绝路径穿越。
- 前置条件：飞书后台「事件与回调 → 回调订阅」须设为长连接方式，否则按钮点击无回调。

## 飞书单聊会话扁平化（flatten_p2p，2026-06-15）
- 动机：旧逻辑回复硬编码 reply_in_thread=True 且新消息 topic_id=msg_id → 每条消息新建话题+新线程（堆会话、丢记忆）。
- 方案（仅 p2p 单聊，群聊不变）：
  - `_on_message` 记录 `self._chat_types[chat_id]=chat_type`；p2p 且 flatten 时强制 `topic_id=None`（覆盖话题/pending），并把 chat_type 写入 inbound metadata。
  - `_reply_in_thread_for(chat_id)`：p2p+flatten→False，否则 True；所有回复点（_reply_card/running card/_send_card_message/send_file/_send_repo_card/_prepare_inbound）按它决定 reply_in_thread。
  - `_remember_thread_mapping` 对 p2p+flatten 直接 return（会话级映射由 _create_thread 写好，避免 store.json 膨胀）。
- 效果：单聊所有消息归 `channel:chat_id` 单一 DeerFlow 线程，多轮接续，行内回复不堆话题；`/new` 重置；群聊仍按话题分线。
- 开关：`channels.feishu.flatten_p2p`（默认 true，代码默认也 true）。
- 长对话由 summarization 自动压缩，不会爆上下文。

## 多项目根目录（2026-06-16）
- 项目根 = `sandbox.mounts` 的每一条；[workdir.py](deer-flow/backend/app/channels/workdir.py) `_project_mounts()` 遍历全部 mount，
  扫描各自子目录列入 `/repo`，normalize 支持完整虚拟路径与裸名（裸名跨根查首个）。
- 新增一个父目录的完整步骤（三处必须一致）：
  1. docker-compose-dev.yaml gateway 加 bind：宿主机路径 → 容器路径（如 /prod_data → /prod_data）。
  2. config.yaml sandbox.mounts 加：host_path=容器路径(/prod_data)，container_path=虚拟前缀(/mnt/prod_data)。
  3. `docker compose ... up -d --force-recreate gateway`（加挂载必须重建）+ 重装 git（exec 装的会丢）。
- 当前已配：`/projects→/mnt/projects`、`/prod_data→/mnt/prod_data`。
- 注意：每个 mount 的所有直接子目录都会被列出（card 截断 20）；`/prod_data` 读写挂载，agent 可改其中文件。
- bash 校验/翻译走 `_get_custom_mounts()`（读 sandbox.mounts，host_path 存在即放行），与 /mnt/projects 同机制。

## 工作区文件易失（注意）
- 本工作区会丢失非近期写入的文件（`config.yaml`/`.env`/部分 memory-bank 曾消失）。
- 运行中的容器保留挂载的 `config.yaml` 与环境变量，可用 `docker exec ... cat` / `printenv` 恢复。
- 建议对 `config.yaml`/`.env` 做外部备份。
