# Product Context
[Last Updated: 2026-09-29]

## 为什么做

- 在 IM 里直接用自托管 agent；复杂编码任务交给角色流水线 + Cursor CLI，人在飞书里看进度、做决策。

## 使用入口

- Web：`http://<宿主机>:2026`（dev 栈要设 `BIND_HOST=0.0.0.0` 并把局域网 IP 加进 `DEER_FLOW_DEV_ALLOWED_ORIGINS` 才能局域网访问）；首次 `/setup` 建管理员。
- 飞书：单聊，或群里 @机器人。渠道全是出站连接，不需要公网 IP。

## 飞书交互

- **命令**：`/new` `/sessions` `/repo` `/agent` `/model` `/models` `/memory` `/status` `/goal` `/bootstrap` `/help`，以及 `/<skill-name> <任务>`。
- **卡片**：裸命令 `/repo` `/agent` `/sessions` `/memory` `/model(s)` 回复交互卡片；带参数时走文本命令。
- **人设**：切换人设 = 以该人设开启新会话（上游把 agent 钉在线程上，同一会话中途不换人设）；`/new` 沿用当前人设。
- **工作目录**：选定后每条消息前注入目录上下文；候选目录来自 `sandbox.mounts` 的各项目根（`/mnt/projects`、`/mnt/prod_data`）的直接子目录。
- **多会话**：`/sessions` 列表 / 切换 / 查看 / 重命名 / 删除；切换时恢复该会话的人设、模型、工作目录；会话标题自动生成。
- **模型**：按会话切换，新会话沿用；失效的模型自动回落默认模型。
- **记忆**：global（个人全局）/ default（跨用户共享桶）/ persona（个人 + 当前人设）。
- **进度**：运行卡片正文上方显示 todos 与正在执行的子任务；子任务失败另发红色通知卡片；`/status` 显示运行时长、子任务累计 token、当前设置。
- **机器人菜单**（飞书后台按 event_key 配置）：REPO / AGENT / SESSIONS / MEMORY / MODELS 出卡片；STATUS / NEW / HELP / MEMORY_GLOBAL / MEMORY_DEFAULT / MEMORY_PERSONA 发命令；`AGENT_<NAME>` 直接切换人设。菜单依赖用户先在单聊发过一条消息。
- **单聊**：所有消息归一条连续会话、行内回复（上游原生行为）；群聊按话题分线。
