<!-- 归档快照：整理为 canonical 结构前的原文（2026-06-16 前后的内容，2026-09-29 归档）。只读，按需追溯。 -->

# Active Context

## 当前状态（2026-06-11 代理 + 模型固定完成）
- 飞书：已连接（最新重启后 15:19 重连成功）。
- 豆包 2.0 Pro：已开通并验证（直连 Ark 正常回复）。
- CURSOR_API_KEY：真实值已生效（容器内已注入）。
- cursor-agent：经 clash 代理（wrapper 模式）可见完整订阅约 120 个模型。
- 角色模型：工程师/测试/评审 全部固定 `claude-opus-4-8-thinking-high`（命令模板含 `--trust`）。
- 冒烟：Opus 4.8 Thinking 经代理 30s 完成 demo 仓库只读分析 ✓。
- gateway 已重启加载全部新配置（注意：config.yaml 不在 uvicorn reload 监听范围内，改了要 `docker restart deer-flow-gateway`）。

## 新功能：/repo 工作目录菜单（已实测通过：卡片选择 demo 成功）
- 飞书发 `/repo` → 交互卡片（最近使用 + 全部目录按钮 + 清除 + 新增项目指引）。
- 选中后每条消息自动注入 `[当前工作目录：X]`；`/repo <名称>`、`/repo clear`、`/status` 均可用。

## 新功能：飞书机器人自定义菜单（2026-06-11 代码已就绪，等控制台配置）
- 已注册 `application.bot.menu_v6` 事件；event_key 映射：REPO→/repo 卡片、STATUS/NEW/HELP/MODELS/MEMORY→对应命令。
- 菜单事件只带 open_id：依赖 `_on_message` 学习的 open_id→单聊 chat_id 映射（持久化在 store）；
  没聊过的用户点菜单会收到「请先发送一条消息」提示。
- **用户操作**：开放平台 → 应用能力 → 机器人 → 自定义菜单：添加「📁 选择工作目录(REPO)」「ℹ️ 状态(STATUS)」
  「🆕 新对话(NEW)」「❓ 帮助(HELP)」；事件订阅添加 `application.bot.menu_v6`；发布新版本。
- 平台限制：菜单项为静态配置，无法动态注入 repo 列表（动态选择由 REPO 卡片承担）。

## 新功能：飞书单聊会话扁平化（2026-06-15 已实现，待实测）
- p2p 单聊：行内回复（不新建话题）+ 所有消息归一条连续对话（有上下文记忆，/new 重置）；群聊仍按话题分线。
- 开关 `channels.feishu.flatten_p2p: true`（config.yaml）。代码已热重载，飞书重连正常。
- 待你实测：单聊连发两条 → 回复在主对话流、左侧不再增多、第二条能接上下文。

## 新功能：多项目根目录（2026-06-16 已实现，待实测）
- 新增 `/prod_data` 作为第二个项目根（compose bind + sandbox.mounts + workdir 多根扫描）。
- `/repo` 现在会列出 `/mnt/projects/demo` 与 `/mnt/prod_data/{clash-for-linux-install, mooya_tool_collection, nanobot}`。
- 已 force-recreate 并重装 git；容器内验证扫描/归一化/沙箱允许均通过；飞书重连正常。
- 待你飞书实测：`/repo` 选 mooya_tool_collection → 发「列出当前目录文件」确认落在该目录。
- 以后再加别的父目录：照搬「compose bind + sandbox.mounts + force-recreate + 重装 git」。

## 已修复 bug
- `/status` KeyError 'thread_id'：/repo 先建只含 workdir 的 entry 导致 get_thread_id 硬取崩溃 → 改 entry.get（单测验证）。

## 待办：飞书三级测试（需用户在飞书发消息）
1. 一级：发「你好，介绍一下你自己」→ 验证豆包链路。
2. 二级：发「让代码评审专员评审 /mnt/projects/demo 的代码质量」→ 验证角色委派 + cursor-agent。
3. 三级：发「让产品经理给 /mnt/projects/demo 的 calculator 写除零防护需求，工程师实现，测试验证，评审把关」→ 四角色流水线。
- 观察方式：`tail -f logs/gateway.log`，看 `[Manager] received inbound` 与 task 委派。

## 关键运维事实
- cursor-agent 代理 wrapper：`/usr/local/bin/cursor-agent`（dev-entrypoint 每次启动重新生成）；
  代理地址可用 `.env` 的 `CURSOR_AGENT_PROXY` 覆盖，默认 `http://host.docker.internal:7890`。
- clash 必须保持运行且容器可达，否则 cursor-agent 不可用（豆包/飞书不受影响）。
- `docker restart` 保留容器内 exec 安装的 git 与 wrapper；`--force-recreate` 会丢 git（需重装）。
- 用户尚未在 `/setup` 创建管理员（不阻塞飞书，仅影响 Web 登录）。
