# POD 毛毯全自动上架 + 全链路留痕 + 飞书改值同步（pod-listing-autopilot）

[Last Updated: 2026-11-02]

接在 `feature-plans/pod-temu-listing/`（妙手建箱发布）之后。改动前的盘点见 [audit.md](audit.md)。

## 目标

- 候选表里勾上某家店铺，选中、领货号、出套图、准备上架图和标题、确认、建箱、写品牌、发布一路自动走完，人只在出异常时介入。
- 现有的飞书表（候选表、选中的款、已上架的款、交付包）和业务库记录全部保留；自动走的每一步照样写表、写库，事后可查。
- 每张图从生成到上架的状态有轨迹、有「现在在哪一环、从什么时候起、卡在哪」，库里和表里都看得到。
- 人在飞书里改的值都进库、留历史；以「上架标题」为第一个字段打通飞书改值 → 妙手采集箱 → Temu 店铺。

## 关键决策（2026-10-29 用户确认）

- 发布后改标题：本期接 Temu 官方 Partner 接口（改标题，回读商品 ID / 审核 / 在售 / 销量），先做可行性验证。
- mooya 白底图两轮质检都没过：服务任务自动取最高分那张继续出图，库里和「上架备注」留标记。
- 默认值（都可配）：勾店铺后 2 分钟撤销窗口（10-30 下午改 5 分钟，见下）；进采集箱就发布；定制款仍不自动上（告警）；撤掉的店铺货号不回收。
- 灰度：先只给 3店开全自动，跑通 3 款（含一次程序写品牌、一次标题改动同步）再开 4店。
- **2026-10-30 下午用户改**：勾店铺后 **5 分钟犹豫期**，过了才走套图生成与上架；候选表删掉「▶ 选中的先出套图」那一行（任务列表也不给这个按钮，`table dispatch` 命令留着）；「▶ 选款完成」留着、随时能点，是标记：勾上的定为选中（犹豫期里的店铺勾当场定）、出了图没勾的标没选中、表名改【选款完成】；之后又出了新图表名是【有新图】；选了几款还没点是【已选N款】；表里选中的款都上架完，表自动挪进「已归档」。数据表和库的状态要一直同步（套图进程每一轮有变化就写表、出完的马上接着准备上架）。
- **2026-10-30 傍晚用户定候选表视图**：从左到右四个视图——表格「全部」（所有行都看得到）、画册「全部画册」（所有有图的原图，不放套图，19:04 加）、画册「已选中」（所有已经选中的原图）、画册「待选」（还没选过的）；飞书接口不能调视图先后，用户选「程序依次改名挪位置」（封面效果跟着视图走）。「选中的款」照旧按参考图上传那天放（不按选中那天）。
- **2026-10-30 晚用户定「选中的款」视图**：每天的「选中的款」按上架状态分开看，从左到右 表格「全部」/ 画册「看图」（保留，封面效果不动）/ 表格「待上架」/「上架中」/「已上架」；「上架失败」的款归「待上架」（当作还要重新上，「上架备注」写原因），「不上了」的只在「全部」；程序给的链接仍打开「待上架」。实现见 `feishu_table.SELECTED_VIEWS_VERSION` 2 / `views.ensure_selected_views`，旧表与两张「选中的款模板」就地升级。
- **2026-11-02 用户定：1店 / 2店也进全自动**：候选表除了 3店 / 4店也能勾 1店 / 2店，其余状态列（货号、店铺、当前环节、上架状态、已上架的款的店铺视图）跟着同步。两个选择（用户选）：① 自动化款用新前缀 **1店 FLRP / 2店 JUHP**，不和 mooya 上人工精作款的 FLR / JUH 共号段（mooya 按租户续号、同店同货号按租户查，沿用 FLR / JUH 会从 0001 起号撞上；新前缀 mooya 只加数据、不改代码、不重建 5175）；② 上架后进**「已上架的款」**（和 3店 / 4店的自动化款一起、按店分视图），精作款周表格仍只放 mooya 上人工做的（按 mooya 客户端分，不按店铺）。配置顺序 s1、s2、s3、s4：候选表列 1店 → 4店，同一款几家店时交付包用 1店的货号起名。一上来就全自动（之前没有任何 1店 / 2店的勾，没有存量）。
- **2026-10-30 晚用户定「上架状态」口径**：出套图中、等出套图这些都属于待上架，只有后面进入妙手的流程才是上架中。即还没进妙手的（领货号、等出套图、出套图中、套图等人工、准备上架图、待确认、排队上架）写「待上架」；从建箱起（建箱中、写品牌、等发布、发布中，`temu.MIAOSHOU_STATES`）写「上架中」；有一家店已上架、别的还没上完也算上架中。列说明跟着改。同日用户要求「套图等人工」不该出现：原因是 mooya 白底图兜底只对 13:20 后建的任务生效，之前建的 6 个停在门控，已按兜底口径放行；不另加保险（以后万一再停，每小时「POD 待办」提醒）。

## 配置（`podflow.yaml`）

- `temu.auto.enabled`（缺省 false）：全自动总开关；`temu.auto.stores`：只对这几家店（key），空 = 全部店。
- `temu.auto.tick_grace_minutes`（5，10-30 下午从 2 改）：勾店铺后等几分钟仍勾着才开始；0 = 立即；犹豫期里点「选款完成」当场定。
- `temu.auto.confirm`（true）：程序替人勾「确认上架」，人取消勾 = 暂停这一款；`temu.auto.publish`（true）：全自动的店进箱就发布（不看 `temu.publish`）。
- `temu.link_resave_days`（20）：在采集箱 / 还在盯的款超过这么多天，重签图片链接存回妙手。
- `temu.brand_recheck_hours`（6）：品牌写满次数后隔这么久只读复查一次（人在妙手选好就接着发布）。
- `temu.alert_stuck_hours`（6）：同一环节卡这么久进「POD 待办」；`temu.alert_failures`（true）：上架后台没上成发红卡。
- `scenes.auto_heal`（true）：定了店铺的款套图缺图补图（mooya `regenerate_missing`，最多 2 次；mooya 没有动作端点时当轮改成整套重出）、没出成重出 `scenes.auto_retry_failed`（1）次、暂停超 `scenes.auto_resume_minutes`（120）自动继续（最多 2 次）。
- `table.cells_enabled`（true）/ `table.cells_read_minutes`（10）：整张读「选中的款」记人改的格子。
- `notify.todo.kinds` 加 `listing`：每小时「POD 待办」的「上架要你看」。
- `temu_open.*`（缺省关）：Temu 官方 Partner 接口（见下）。

## 模块与数据（pod-blanket，只加表、不改旧表）

- `podflow/trace.py`：`trace_events`（单款轨迹，只追加）、`pipeline_status`（每（款, 店）现在在哪一环 / 从什么时候起 / 卡在哪）；`timeline()` 合并轨迹、事件、审核、调用账本和各表时间戳。
- `podflow/cells.py`：`table_cells`（每格上次读到的值）、`table_cell_log`（人改格子的日志：旧值 / 新值 / 修改人 / 修改时间 / 结果）、`table_row_snapshots`（程序删行、清格前的整行快照）。
- `candidate_stores.py`：待选的行勾店铺也当场生效（撤销窗口 `store_tick_pending`），处理后取消勾按环节撤店或写回。
- `stores.py`：货号只对 4xx 计失败次数；定了店铺的款没货号不交 mooya。
- `scenes.py` + `mooya.py`：`scene_set_flags`（交 mooya 用的包名、白底图质检结果、停下的时间、自愈次数）；自愈；套图出完马上拉起上架。
- `temu.py`：自动确认（`listing_holds` 记暂停）、全自动的店自动发布、`publish_unknown` 防重复发布、品牌慢速复查、链接重签存回、标题同步、「当前环节」等列、上架没成发红卡。
- `temu_title.py`：`listing_title_versions`（每个标题版本、复核结果）、`title_sync`（每（款, 店）标题同步到哪了）；人改标题按完整规则复核。
- `listed_table.py`：人填列进 `listed_metrics` 时间序列；「上架标题」改成指令列；接 Temu 接口后程序写商品 ID 与状态。
- `temu_open.py`：Temu Partner 接口客户端（签名、错误分类、节流），`temu_goods`（货号 × 店 → goodsId），只读探测 `podctl temu open probe`。
- 候选表（10-30 下午）：`candidates._top_specs` 只给「选款完成」+ 交付包（老表的「选中的先出套图」第一次同步时删掉）；`accept` 不再等出完图，先 `candidate_stores.forget_pending` 清掉犹豫期记录再按勾的店铺领号；`name_marks` 出【已选N款】/【选款完成】/【有新图】；`_sync_help` 让老表的「选中」「选款结果」、店铺列说明跟上（meta `candidates.help`）。
- 归档（10-30 下午）：`shelf._listing_facts` + `auto_block`：选中的款都定了店铺时，点过「选款完成」、没有待选、每家店都已上架就收（不看交付包）；`temu.sync` 查到新上架后 `_sweep_listed` 当场对这几个任务扫一遍。
- 套图同步（10-30 下午）：`scenes._relay` 每一轮把新交出 / 变了 / 出完 / 自愈的套图马上 `_after_sets` + `feishu_table.kick`，不等进程结束。
- 1店 / 2店（2026-11-02）：店铺本来就按配置走（候选表补列并按配置顺序重摆、「选中的款」店铺选项、「已上架的款」选项与视图都自动补）；补了四处：`books.upgrade_candidate_templates` 周表格（含锁住的模板表格）的「候选表模板」店铺列少了也补（之前只看视图版本）；`listed_table.local_columns` 本机的店总在「已上架的款」里，`crafted._store_views` 不再把它们当精作款的视图删；`mooya_listing.doctor_checks` 加比对店名；`listed_table.status` 按「客户端/店铺」计数。
- 连接（10-30 下午）：`db.py` 连不上 / 角色连接数满时按 1/2/4/8 秒重试；`stages/plan.py` 并行策划里失败的组（不含输出不合格）串行再跑一次，事件 `plan_groups_retried`；测试库 `pod_blanket_test` 改用独立角色。

## 飞书表

- 「选中的款」新加：「当前环节」（单选，程序写）、「环节开始」（日期时间）、「妙手 ID」、「标题同步」。
- 「已上架的款」新加：「标题同步」；「上架标题」可改（改了就同步到店铺）。
- 列的归属见 [audit.md](audit.md) 第五节。实现上：「选中的款」里表格同步写、又没记着写了什么的列（素材、方向、套图状态、链接…）分不清是不是人改的，不看；上架那几列（店铺、货号、上架状态 / 备注 / 日期、当前环节、环节开始、妙手 ID、标题同步）`temu_rows` 记着上次写的值，人改了记 reverted、删掉缓存里那一格，下次写表改回；其余（备注、人加的列）记 recorded。

## 暂停的语义

- 人取消「确认上架」（或 `temu hold`）= 暂停：排队的取消确认；全自动发布的店进了采集箱还没发布的不发布，「确认上架」保持取消、当前环节写「已暂停上架」；再勾（或 `temu release`）解除、接着上。
- 已经在发布 / 上过的停不了，勾写回去。

## mooya 侧（`/prod_data/mooya_tool_collection`）

- 预设 `pod_blanket` 加 `baseline_fallback: 'best'`（随 job 存进 `serviceRequest`，不进 showcase 白名单）；白底图全轮不合格时服务任务用最高分那张预写门控结果（`fallback: 'best'`），页面作业不变。
- 服务接口 job 返回加 `baseline: {passed, score, attempts, fallback, manual}`；新端点 `POST /api/service/v1/showcase/jobs/<id>/actions`（`regenerate_missing` 与页面「补生成缺失」共用 `queue_regenerate_missing` / `resume` 同页面「继续」；202 已排上、409 做不了）。deer-flow 遇到 404 / 405（mooya 还没上这一版）当作不支持，缺图改成整套重出。

## 与 mooya「交付包直通 Temu 上架」的分工（2026-10-30）

- mooya 交付工作台也能上架了（`/prod_data/mooya_tool_collection/feature-plans/delivery-temu-listing/`）：引擎是这里 `temu.py` / `miaoshou.py` / `temu_title.py` 的移植，以交付包为单位，店铺配在 mooya 表 `temu_stores`。
- 店铺分工：1店（17356855，FLR / Copy）、2店（18580271，JUH）只配在 mooya；3店（FLow）、4店（Deer）只配在这里的 `temu.stores`。同一家店两边都配会重复上架。（10-31 起引擎只留 mooya 一份，这条约束就不存在了；**2026-11-02 起**这里也配了 1店 s1 / 2店 s2，前缀 FLRP / JUHP，mooya `ai_temu_deerflow` 里同 key、同前缀、同店名、`rules=pod10`、`ui=0`；`ai_temu` 那两行（FLR / JUH，交付工作台）不动。）
- 两边共用同一个妙手应用（`MIAOSHOU_APP_KEY`）：各自进程内限速 1 秒，合起来最多约 2 次 / 秒；限流两边都只退避重试、不算失败。
- 标题规则文件两份逐字节相同：这里的 `references/temu_title_rules.json` 与 mooya `backend/prompt_templates/temu_listing/title_rules.json`，改规则两边一起改（mooya 断言会对账）。
- 2026-10-30 收敛成一份（mooya `feature-plans/temu-listing-unify/`，**2026-10-31 已上线，线上两个开关都是 mooya**）：`temu.engine: mooya` 时这里只交单、跟进、写表（`podflow/mooya_listing.py`），取图、标题、建箱、品牌、发布、Temu 接口都在 mooya；`temu.listed.source: mooya` 时「已上架的款」读 mooya 四家店底账（含 1店 / 2店），人填列与改标题写回 mooya。切换后这里的妙手客户端、P15 与标题规则副本、转图上传、temu_open 都不再用，稳定几天后删。

## Temu 官方接口（`temu_open`）

- 授权：Partner 平台建应用（app_key / app_secret）；每家店在跨境卖家中心「系统管理 → 授权管理」授权，拿 access_token（有到期时间）。
- 用到的接口（以可行性验证为准，名字可在 `temu_open.api.*` 改）：商品列表 `bg.local.goods.list.query`（按标题搜、按 outGoodsSn / outSkuSn 核货号）、部分编辑 `bg.local.goods.partial.update`（改 goodsName，进审核）。
- 没配凭据时整块不动；配上后先 `podctl temu open probe` 只读探测。
- 2026-10-31 起上架引擎在 mooya：这块也移进了 mooya（`services/temu_listing/temu_open.py`，`TEMU_OPEN_*`，缺省关，`scripts/temu_open_probe.py` 只读探测），授权后在 mooya 配；这边的 `temu_open` 只在 `temu.engine: local` 时用，等删旧引擎时一起删。

## 风险

- 自动确认少了「人看过标题」：程序复核只查词表和格式，看不出图上的品牌 / IP。用撤销窗口 + 暂停勾 + 红卡兜底。
- 发布请求超时后是否已生效不确定：`publish_unknown` 等发布超时再重发一次，不当场重发。
- 1店 / 2店两个租户各配一行（2026-11-02）：前缀必须不同——mooya 货号按租户续号、同店同货号按租户查，这里 `listed_rows` 以货号为主键（精作款与自动化款同号会互相覆盖）。以后接 Temu 官方接口，mooya `TEMU_OPEN_TOKENS` 按「租户/店铺 key」取 token，1店 / 2店两个租户下要各配一份。
- Temu 接口：半托店能否授权、接口族、签名写法都待验证；改标题会进 Temu 审核，审核中不能再改。
