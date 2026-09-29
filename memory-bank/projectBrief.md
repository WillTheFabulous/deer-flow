# Project Brief
[Last Updated: 2026-09-29]

## 项目

- DeerFlow fork：`WillTheFabulous/deer-flow`，上游 `bytedance/deer-flow`（LangGraph super agent：sandbox 执行、记忆、子代理委派）。
- 定位：自托管在阿里云 ECS 上，主要通过飞书指挥 DeerFlow 及其驱动的 Cursor CLI 做远程编程和日常问答。

## 目标

- 跟随上游最新版；fork 定制用最小挂接点隔离，便于定期同步。
- 飞书为主渠道：交互卡片（工作目录 / 人设 / 会话 / 模型 / 记忆）、机器人菜单、流水线进度可见。
- 角色子代理流水线（需求 → 规划 → 实现 → 测试 → 评审 → 集成验收），由角色通过 cursor-agent 落地代码。
- 模型走国内：火山方舟豆包 + 硅基流动（GLM / Kimi / MiniMax）。

## 范围

- 日常运行形态是 Docker dev 栈（热重载）；生产栈（`make up`）暂不使用。
- 真实凭证与飞书开放平台配置由用户维护。
