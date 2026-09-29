# 个人 Memory 分片 · <把这里改成你的 git 用户名>
[Owner email: <你的 git config user.email>]

> 这是你**专属**的增量记忆分片：只有你会改，天然零冲突。
> 使用方法：复制本文件为 `memory-bank/personal/<你的用户名>.md` 后开始记录。
>
> 规则：
> - 会话 / 任务结束时，把「本次对 canonical Memory 的增量」追加到下面「## 待归并」区，
>   **不要**直接改 memory-bank/ 根目录的 canonical 文件（普通账号会被 CODEOWNERS / CI / pre-commit 拦截）。
> - 新增内容 → 直接写；改动已有 canonical → 写清 delta（把 X 改成 Y + 原因）。
> - 负责人会定期用 `memory-bank-merge` skill 把各分片归并进 canonical，并在下方打「水位线」。
> - 分片保持精简：**超 30K 字符**才整理，一整理就压到 18K 以下（自检 `bash scripts/memory_bank_budget.sh`），先合并同类项。

## 待归并

<!-- 新条目追加在本区顶部；格式见下例 -->

### 2026-01-01 · 示例主题（用完删除本示例）
- [新增 → systemPatterns.md·IM 渠道模式] 新增飞书卡片动作 foo，见 backend/app/channels/fork/feishu.py
- [修改 techContext.md·端口] 把 dev 入口端口从 2026 改为 2027（避让 xxx）
- [废弃 progress.md·待办] 旧待办「接入个人微信」已放弃

## 已归并（水位线以下，仅负责人维护）

<!-- 负责人归并后在此追加一行：merged up to <YYYY-MM-DD / commit> by <lead> -->
