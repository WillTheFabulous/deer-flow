# 每周选款表 · 进度

[Last Updated: 2026-10-31]

## 当前阶段

已上线（2026-10-29 14:10），W44 迁移完成（15:07），`table.weekly` 已打开。2026-10-31 12:29 精作款周表格上线（1店 / 2店按周、按天单独登记）。剩飞书里手测和事件实测。

## 已完成

- 2026-10-31 12:29 精作款周表格上线（用户确认）：12:29 建「精作款」节点和模板，用户在「看图」里设好封面效果，12:31 锁上；12:31–12:38 迁移：删「已上架的款」里 1店 / 2店 1561 行（旧表格 1353、W44 208），旧表格那张整张删，W44 删 1店 / 2店视图，建 15 本周表格（W30–W44）、60 天的表；12:39–14:15 分批重写完 1561 行，核对 60 张天表逐张一致、`temu listed check` 通过。执行细节见 `.pod-temu-NOTE.md` 同名一节。
- 2026-10-31 精作款周表格（见 README 同名一节）代码完成：`.pod-crafted-wip`（以 03:50 的线上为底）新 `crafted.py`、`listed_table` 接上、`cli` 加 `temu listed crafted template|lock|status|migrate` 与 `temu listed sync --batch`、doctor 两项检查；新 `tests/test_crafted.py` 6 个，10 项变异全红。

- 线上实测（10-29）：见 README「线上实测结论」。
- 模板表格「POD 选款表模板（勿改）」（app `XioIbBZPcaBbnXs0d8Hc2IfdnId`，节点 `INz5wYhB8idDPHkYfJdcwLganIg`）：用户在三个「看图」设了适应，12:59 锁上（Editor 三张表 read_only，读回核对过）。
- W44「POD 选款表 W44（10.26–11.01）」（app `HCE3b2QwyanLhAskMgTcMFj4nih`，节点 `OlrKwbycciqYx7kuTHzc8nAZned`）13:36 复制：带过来的高级权限关掉了，三张模板表在「模板」文件夹，事件订阅上了。用户看过 W44 的「看图」是适应。
- 引擎：新模块 `books` / `table_registry` / `week_migration`；`feishu_table` / `candidates` / `downloads` / `listing` / `temu` / `scenes` / `listed_table` / `table_template` / `doctor` / `cli` 按份分流。
- 测试：`tests/test_weekly_books.py` 12 个用例；假飞书补了知识库节点新建 / 整本复制（含异步复制）、嵌套文件夹、高级权限与角色（照线上的返回格式和校验）。12 项变异全抓到。SQLite / Postgres 全量各 558，SkillScan 没有 CRITICAL，backend 的 bundled / 人设 / 渠道 / SkillScan 用例 153 通过。
- 上线 14:10：线上和基线一致，直接拷入。备份 `/work/backups/pod-blanket-skill-20261029-141008.tgz`、`postgres/pod_blanket_20261029_141008.dump`。
- 迁移（`--by ops`）：
  - 14:11 开始。逐行建一行要 3–4 秒，照这个速度要两个多小时。14:20 停下，改成迁移重写时 50 行一批建，14:21 接着跑。停的时候 S17 多出 1 行业务库不认识的，当场删掉了。
  - 第一遍 15:03 结束：S27 有一批 50 行碰上飞书 1255002 RpcError，核对报 50 处没有行，旧表都没删。
  - 加了三条补救：建行批次失败重试一次；重跑时重建过的任务也再过一遍；核对前删掉新候选表里业务库不认识的行。15:05 重跑，15:07 核对通过，删掉旧候选表 11 张、旧「选中的款」76 行。
  - 只读复核：
    - 12 张新候选表都在当天的文件夹里，行数、选款结果分布和旧表一样。例外有两个：S22 旧表早没了，新建了 82 行（80 待选 + 2 表顶）；S24 少了旧表里那 1 行空行。
    - 「10-26 / 10-27 / 10-28 选中的款」分别是 4 / 8 / 64 行，都是「待上架」。旧「选中的款」剩 70 行。
    - `table galleries` 没有 missing；`doctor` 通过，每周选款表两项都 ok。
  - 备份 JSON：`/work/backups/migrate-2026-W44-20261029-061147.json`。
- 16:55 跟进上线：对话里人设给的「选款表」（`table status` 的 `url`）改成这一周的周表格，旧表格在 `legacy_url`；「选中的款」去掉「场景图」列（旧表格、10-26 / 27 / 28、模板表格和 W44 的「选中的款模板」都删了，不再生成缩略图）；候选表「看图」跟着选款进度改筛选（见 `pod-blanket-agent/progress.md` 同日条目）。
- 10-30 19:41「选中的款模板」视图升级：模板表格和 W44 的「选中的款模板」改成 全部 / 看图 / 待上架 / 上架中 / 已上架（`books.upgrade_selected_templates`，表格同步开头做，`ensure_day` 带 `views_version`），新的一天复制出来就是这一套；详见 `feature-plans/pod-listing-autopilot/progress.md` 同日条目。
- 打开 `table.weekly`（线上 `podflow.yaml`，旧的留在 `podflow.yaml.bak-20261029-weekly`）；`.pod-wip` 已同步到线上。给 Temu 那一路留了 `.pod-weekly-NOTE.md`（改了他们的 `temu.py` / `listed_table.py`）。

## 下一步

0. 精作款：在飞书里看一眼几张天表的「看图」封面是不是「适应」；下周一（11-02）第一款 1店 / 2店上架时看有没有自动建出「精作款 W45（11.02–11.08）」和「11-02 周一」。
1. 事件实测：W44 某天「选中的款」有人勾「下载确认」后，看 `selected_downloads.marked_by`。是勾的人 `feishu:ou_…` 说明事件送到了；是 `feishu:选中的款` 说明是巡检兜底读到的。也可以在 W44 候选表的按钮行勾一下，看网关日志的 `[Pod] bitable control`。
2. 第一个 10-29（周四）上传的任务：看程序有没有自动建「10-29 周四」文件夹、候选表、「10-29 选中的款」。
3. 下周一第一张表：看 W45 有没有从模板复制出来、高级权限有没有关掉。
4. 归档一个周表格里的任务：候选表挪进当天的「已归档」，调出后挪回当天的文件夹。

## 阻塞项

无。
