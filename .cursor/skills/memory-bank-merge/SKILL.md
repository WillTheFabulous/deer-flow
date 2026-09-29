---
name: memory-bank-merge
description: Merge developers' personal Memory Bank shards (memory-bank/personal/*.md) into the canonical memory-bank/*.md files, following the update matrix and rolling-compaction rules in Memory-Bank-Protocol. Use only when the Memory Bank lead explicitly runs a periodic Memory Bank merge / consolidation.
disable-model-invocation: true
---

# Memory Bank 归并（负责人专用）

把各开发者的个人分片 `memory-bank/personal/*.md` 归并进 canonical `memory-bank/*.md`。
这是**唯一**把分片内容写入 canonical 的合法路径，只有负责人可执行。

## 前置校验（必做）

1. 运行 `git config user.email`，确认该邮箱出现在 `memory-bank/OWNERS`（第 2 列）。
2. 若不在名单 → **立即停止**，提示用户："当前 git 账号非 Memory Bank 负责人，不能执行归并。"

## 归并流程

复制以下清单并逐项执行：

```
归并进度：
- [ ] 1. 读 canonical 6 文件 + 所有 personal/*.md 的「待归并」区
- [ ] 2. 逐条把分片条目归位进对应 canonical 文件·章节（按更新矩阵）
- [ ] 3. 去重 + 冲突取舍（跨分片同主题合并；矛盾处以最新 / 最具体为准，存疑则问负责人）
- [ ] 4. 按 §3.1 配额归位；跑 `bash scripts/memory_bank_budget.sh`，仅对超触发线的文件压缩到目标线以下
- [ ] 5. 更新各 canonical 文件顶部 [Last Updated: YYYY-MM-DD]
- [ ] 6. 在每个已消费分片打水位线
- [ ] 7. 负责人 review diff → 提交
```

**Step 2 归位规则**（详见 `.cursor/rules/Memory-Bank-Protocol.mdc` §2 更新矩阵）：
- `[新增 → 目标文件·章节]` → 写入该 canonical 文件对应章节。
- `[修改 目标文件·章节]` → 按 delta 改写 canonical 对应处（把 X 改为 Y）。
- `[废弃 目标文件·章节]` → 删除 / 标记失效 canonical 对应内容。
- 分片没标目标文件的条目，按矩阵语义判断归属；拿不准就问负责人。
- systemPatterns 固定章节：技术架构与运行拓扑 → 目录结构与 fork 挂接点 → IM 渠道模式 → 子代理与角色流水线 → 模型与配置约定 → Cursor CLI 驱动模式 → 上游同步模式 → 经验教训速查。

**Step 4 压缩规则**（详见协议 §3）：只写当前契约、不写演化史；activeContext 焦点 ≤3 项 / 最近完成 14 天内 ≤8 条 / 决策 ≤10 条 / 下一步 ≤12 条；progress 新条目 ≤1.5K 字符。

**预算是双水位**（§3.0）：`bash scripts/memory_bank_budget.sh` 报 `OK` 的文件**不要压缩**；只对报 `COMPACT` 的压，且必须压到**目标线**以下，压完更新该文件顶部的 `<!-- Budget: … -->` 水位线注释。

**Step 6 水位线**（避免跨分支再生冲突）：
- 在每个已消费分片的「## 已归并」区追加一行：`merged up to <YYYY-MM-DD> by <lead>`。
- **不要删除**分片「待归并」区的正文（分片归开发者所有，由其自行清理已过水位线的条目）。
- 下次归并只处理各分片中**晚于**其最新水位线日期的条目。

## 提交

- 归并产物作为负责人的一次普通 commit / PR（canonical 改动只有负责人 PR 能过 CODEOWNERS + CI 门禁）。
- commit message 建议：`docs(memory-bank): merge personal shards up to <date>`。
