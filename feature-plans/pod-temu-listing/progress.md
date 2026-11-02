# POD 毛毯接妙手 · 进度

[Last Updated: 2026-10-30]

> 2026-10-30 起后续在 [`feature-plans/pod-listing-autopilot/`](../pod-listing-autopilot/)（全自动上架 + 全链路留痕 + 飞书改值同步）：本目录只记到妙手建箱发布这一段。

## 当前阶段

**Phase 5（2026-10-30 03:50）**：线上 `temu` 开着（`publish: box`，3店 CozWeave H = FLow、4店 LoomNest J = Deer，两家品牌 MIANDUDU / brandId 1000000793013）。
- FLow0006（3店）、Deer0006（4店）在采集箱、品牌都在了，**等用户在妙手点发布**。
- 历史款在表里勾店铺当场生效（10-29 21:13 上线）。旧租户里能上的 68 套已挪到新租户，上架就绪的从 6 款变 74 款。用户实测勾了 FLow0007（IMG03393）、FLow0008（IMG03738），图和标题都备好，**等用户勾「确认上架」**。

## 已完成

- 2026-10-28：建 feature 目录；接入指南挪进来（`miaoshou-api-guide.md`），开头加了订正。
- 2026-10-28：妙手密钥、mooya 第二个服务令牌写入 `secrets.env`（600）。只读探测原始数据在 `.pod-temu-probe/`（不入库）。
- 2026-10-28 mooya（dev 已验证，未上生产）：
  - 新租户 `ai_temu_deerflow`（`seed_ai_temu_tenant.py --tenant`，1 个账号），与 `ai_temu` 同属字标租户 `BADGE_TENANTS`。
  - 服务接口取图 `?variant=export|raw`；货号预留表 `delivery_name_reservations`（迁移 `20261028_0064`）+ `POST/GET /api/service/v1/codes`；交付工作台的序号推荐与重名检测算上预留。
  - dev `.env.dev` 的 `SERVICE_API_CLIENTS` 加 `pod-blanket-deerflow`；断言 `assert_showcase_badge` / `assert_service_api` / `assert_delivery_naming` 通过。
- 2026-10-28 pod-blanket 引擎（`.pod-temu-wip`）：
  - `miaoshou.py`（签名、错误分类、节流）；`mooya.py` 多客户端 + `scene_set_clients`（每套记交它的客户端）；套图拉原图带 `variant=raw`。
  - `stores.py` + `candidate_stores.py`：候选表每家店一列勾选框 +「货号」列（「选中」藏起来，按钮行勾任意一个勾选框），点按钮后记店铺、当场领货号；交套图用第一家店的货号当编号。
  - `temu_assets.py`（导出口径取带字标的交付图、原 `write_member_as_jpg` 逐字节一致、10 图位校验、尺码图挪第 3、传 OSS、上架图包）；`temu_title.py` + P15 + `temu_title_rules.json`。
  - `temu.py`：样板克隆建箱、超时按货号对账、按店批量发布、查发布结果、「选中的款」四列（店铺 / 货号 / 上架标题 / 确认上架）与上架状态四态（待上架 / 上架中 / 已上架 / 上架失败）、事件与巡检回读；只写变了的格子，勾只在要改时写。
  - `listed_table.py`：`listed_products` ↔ 飞书「已上架的款」（程序列库 → 飞书、人填列飞书 → 库、被改回 / 被删重建、导出 CSV）。
  - 挂接：表格同步、巡检（`temu sync`、`table poll`）、digest（待确认上架 / 采集箱等发布 / 没上成 / 今日上架 / 妙手异常）、doctor（Temu 一组）、CLI `podctl temu …`、`publish.auto_listing` 的提示。
  - 测试：`test_temu.py`（19 项：全流程 auto / box、超时对账、标题退回、定制款、巡检取消 / 写回、已上架的款回读与重建、样板克隆、图位、JPEG 逐字节、旧租户套图）、`test_miaoshou_client.py`（19 项）；SQLite 全量见下。

- 2026-10-28 19:00 引擎并回线上（备份 `pod-blanket-skill-20261028_185812.tgz`），人设 SOUL 更新；同晚另一个会话在线上加了「候选表模板」（`table_template.py`），10-29 已把它合进 `.pod-temu-wip`，doctor.py 三方合并（见根目录 `.pod-temu-NOTE.md`）。
- 2026-10-29 03:36–03:43 mooya 上生产（用户确认）：从干净 worktree（`cb75449`）构建，迁移 `0064`、建租户与 Keycloak 账号、`SERVICE_API_CLIENTS` 加 `pod-blanket-deerflow`；deer-flow 配 `mooya.clients.deerflow` + `submit_client: deerflow`，job 565 / 566 建在 `ai_temu_deerflow`（SS0141 / SS0142 记 `deerflow`）。为等在出的套图跑完，线上 `scenes.max_in_flight` 临时设 0 约 5.5 小时（10-28 22:08 → 10-29 03:37），已恢复。
- 2026-10-29：`pod_blanket` 角色密码轮换（测试库连接串在 10-28 的工具输出里露过）；`/work` 清理（MinIO 旧卷、旧镜像），余约 21GB。
- 2026-10-29 订正店铺 ↔ 前缀：3店 CozWeave H = FLow、4店 LoomNest J = Deer（照店里 10-23 人工上的 FLow0001–0005 / Deer0001–0005）。mooya `ai_temu_deerflow` 租户补了这 10 个号的预留（`client='miaoshou-manual'`），5175 / 5173 的 `/codes` 都给下一个 FLow0006 / Deer0006。
- 2026-10-29 对账修正：妙手采集箱列表里 `itemNum` 是空的（货号只在详情的店铺那条），`temu._reconcile` 改成按「标题完全相同 + 店铺 + 建箱时间不早于提交前一天」找候选，再读 `site_detail` 核对货号（`_find_created`）；假妙手改成和真接口一样，加两条用例。
- 2026-10-29 15:34 并回线上：等每周选款表那一路的 `migrate-week` 跑完，以线上为底三方合并 10 个文件（无冲突），带上他们的热修后 SQLite 全量 561 通过；拷前备份 `pod-blanket-skill-20261029-153346.tgz`、`pod_blanket_20261029_153349.dump`；SOUL.md 装上。给他们的说明在根目录 `.pod-temu-NOTE.md`（`.pod-weekly-wip` 还没有这 10 个文件的改动）。
- 2026-10-29 15:36 打开 `temu`（改前备份 `podflow.yaml.bak-20261029-temu`）：
  - `temu probe`：能看到 4 家店（19569774 / 19569702 / 18580271 / 17356855）；两个样板都能用（类目 11901、9 图、4 个 SKU、只上 US、299 / 210 / 190 / 150）；类目 11901 标题上限 250，和规则一致。
  - `doctor` 的 Temu / mooya 检查全过；表格同步给旧表格和 W44 三天的「选中的款」补了四列和上架状态新选项，25 张候选表补了店铺列，无错误。
- 2026-10-29 15:47 第一款准备好：`temu store IMG03683 --set 3店` 领到 FLow0006；`temu sync` 备好 10 张图（JPEG 1024²、带 MIANDUDU，顺序 04 / 05 / 尺码图 / 01 / 02 / 03 / 06 / 09 / 08 / 07，OSS 签名链接都能取）和标题（P15，172 字符，复核无问题）；`temu preview` 核对 SKU 预览图位 1 / 2 / 2 / 1、价格、货号、运费模板和仓库都随样板。
- 2026-10-29 16:3x 第一款进箱：用户勾「确认上架」后建进 3店采集箱 3491496907。妙手收下了 10 张带 query 的 OSS 签名链接（原样存，30 天有效）。用户发现「品牌名」是空的：两个样板的站点 / 店铺模式详情里都没有品牌，克隆带不过去。
- 2026-10-29 16:5x 定品牌写法：用户在妙手给 FLow0006 选 MIANDUDU、保存后读回。品牌只存在店铺模式详情的 `brandId` 里，值 1000000793013（和 2店人工上的款一样），没有「品牌名」属性；前端下拉栏旁的 98575110 不是接口用的号。标题、图、属性、SKU 都没被这次保存改动。
- 2026-10-29 17:52 品牌上线：
  - 做法：进箱后 `_ensure_brand` 读店铺模式详情，不是这个品牌就只改 brandId（editModel 填 shop）、整份存回去再读回核对，人选过的只记一笔；记在新表 `temu_brands`；没写上不自动发布、每轮补写、满 6 次停（`temu retry` 清零）。
  - 测试：5 条新用例。以每周选款表 16:55 的线上为底三方合并，SQLite 全量 568、Postgres 相关 115 通过。
  - 上线：备份 `pod-blanket-skill-20261029-175231.tgz`、`pod_blanket_20261029_175231.dump`、`podflow.yaml.bak-20261029-brand`；两家店配上品牌后 `temu sync` 把 FLow0006 记成已有品牌（saved = 0，没重写）。
- 2026-10-29 17:58 4店第一款准备好：`temu store IMG03700 --set 4店` 领到 Deer0006；10 张图顺序同上，SKU 预览图都用第 4 张（照 LoomNest J 样板），价格 299 / 210 / 190 / 150，标题 170 字符复核无问题。
- 2026-10-29 19:10 Deer0006 进箱（3492158195），程序第一次写品牌：妙手拒了 `save_shop_collect_item_info`「品牌不存在，请同步后重试」。行在 in_box、备注写了原因、每轮重试（box 模式本来就不自动发布）；读回核对标题、10 张图、属性、SKU 都和提交的一样，没被改动。4店的 MIANDUDU 要么编号不同、要么妙手里 4店的品牌没同步，等用户在妙手选好后读回。
- 2026-10-29 19:21 4店品牌对上：用户在妙手里给 4店同步品牌、给 Deer0006 选好保存，读回 brandId 也是 1000000793013（编号一样，是没同步）。`temu sync` 记成已有品牌（saved = 0，前面失败 2 次），备注回到「在妙手里检查后点发布」。两款都在采集箱、品牌都在了。

- 2026-10-29 20:xx 历史套图盘点：已选中、套图出完的 140 套里，新租户 6 套；旧租户 134 套中，68 套是点缀版 10 张（能上），66 套是 10-26 前的老格式或旧包没有前排钉序（不处理）；另 6 套没出完。用户定：68 套在 mooya 挪租户（不重出），66 套勾了程序取消写原因，已选中款的原图行 / 套图行勾店铺都当场生效。
- 2026-10-29 20:28 mooya 挪租户：新脚本 `move_service_jobs_tenant.py`（只改库、按行数核对、报告带回滚；dev 断言 29 项，两处变异变红）。
  - 正式库 dry-run 给用户看过后执行：68 任务 / 833 成片 / 68 项目（父目录 43 → 357）/ 68 包 / 680 关联（尺码图 98920 → 113757，字节相同）。
  - 第一次没改服务客户端，新客户端查不到（服务接口按 `serviceRequest.client` 认 job），`--retag` 补改成 `pod-blanket-deerflow`。
  - 核对：新客户端查得到、导出图带 MIANDUDU，旧客户端 404。
  - 业务库用新命令 `scenes set-client` 把这 68 套记成 `deerflow`，上架就绪 74 款。
- 2026-10-29 21:13 勾店铺当场生效上线：`candidate_stores.apply_ticks` / `poll`、`control_event` 认已选中款的原图行和套图行、套图行镜像（新表 `candidate_scene_codes`）、老格式拒绝；6 条新用例（3 处变异都变红），SQLite 574、Postgres 相关 148。上线后表格同步把 FLow0006 / Deer0006 的套图行补上了店铺和货号。
- 2026-10-30 03:4x 实测：S25 IMG03738 套图行勾 3店 → 当场领 FLow0008，原图行、套图行、「选中的款」都写上，26 秒备好 10 张图（带字标）、40 秒写好标题；S13 IMG02158 勾了被取消，备注「套图图位对不上（第 1–2 张不是…旧包没有前排钉序），不能自动上架」。

## 探测结论（2026-10-28，只读）

- 签名、IP 都通过；接口返回 `{result, code, message, data}`。
- `get_shop_list` 当时报 `appNoPermission`（10-29 用户开了店铺权限后正常）。
  - 接口用的是整数店铺 id（如 `17356855`），用户给的 `v2_…` 是另一种编号，不能直接用：`temu.stores[].shop_id` 要填整数 id（`podctl temu probe` 能列出来）。
- 采集箱列表的行里 `itemNum` 是空的，货号只在详情（`get_site_collect_item_info`）的店铺那条里；`gmtCreate` 是北京时间。
- 能用的接口：`get_publish_site_options`、`get_item_options`、`get_category_tree_by_site`、`search_collect_box_detail_list`（`{detailList}`，没有 total，每页最多 500）、`get_site_collect_item_info` / `get_shop_collect_item_info`。
- 现有店铺（从采集箱里看到）：`17356855`「毛毯pod」= 1店（FLR）；`18580271`「毛毯POD2店」= 2店（JUH）。3店 / 4店在采集箱里还没有商品。
- 人工上架的 8 个款：9 张轮播图、第 3 张尺码图、倒数第二适用场景、最后机洗；不放详情图；2店主货号 = SKU 货号 = mooya 交付包名；4 个尺码、申报价按店不同；只上 US；SKU 预览图按样板图位；2店 JPEG。

## 下一步

1. FLow0006、Deer0006：用户在妙手点发布 → 每 5 分钟查一次，查到写「已上架」、「已上架的款」建表加一行。
2. FLow0007 / FLow0008：用户勾「确认上架」→ 这是程序第一次真写品牌（店铺模式保存）：进箱后读回 brandId 和其余字段核对，再让用户发布。
3. 都上了：问用户是否换 `auto`。
4. 旧标题表跑一遍 `podctl temu titles <文件>`，看复核有没有误杀。
5. 候选表模板（每周选款表的锁定模板）里店铺列的表头说明还是旧的，要每周选款表那一路升模板版本时换成新的 `STORE_HELP`。

## 阻塞项（等用户）

- 在妙手发布 FLow0006、Deer0006；FLow0007 / FLow0008 勾「确认上架」。
- 旧标题表 `毛毯标题表.xlsx` / `毛毯2店标题表.xlsx`（`podctl temu titles <文件>` 验证复核不误杀）。

## 待手测

- 已过（10-29）：候选表店铺列与货号（25 张表补列）、「选中的款」四列与视图（旧表格 + W44 三天）、勾「确认上架」事件触发建箱（FLow0006）、妙手收 OSS 签名链接和 10 张图。
- 待测：程序写品牌（Deer0006）、发布后回写已上架、「已上架的款」建表与回读、候选表里勾店铺列点按钮当场领号（这两款用的是 `temu store` 命令）。
