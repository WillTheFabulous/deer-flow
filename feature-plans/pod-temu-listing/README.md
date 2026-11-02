# POD 毛毯接妙手：选款定店铺，一键上架 Temu 半托管（pod-temu-listing）

[Last Updated: 2026-10-28]

## 目标

pod-blanket 出完套图后不再下载图包、手工整理轮播图和标题表，而是：

- 在候选表选款时就定上哪家店；
- mooya 给货号、贴字标；
- 后台把图包转成上架用的 10 张图并写好合规标题；
- 人在飞书「选中的款」勾「确认上架」，程序通过妙手开放平台建进 Temu 半托采集箱并发布。

上架效果要和以前的 Windows 流程一样：同样的图、同样的顺序和格式、同样的字标、同样的标题规则。

## 范围

- 店铺：3店 CozWeave H（前缀 `FLow`，妙手店铺 19569774）、4店 LoomNest J（前缀 `Deer`，19569702）；以后在 `temu.stores` 里加。1店 / 2店（FLR / JUH）的人工流程不动。
  - 10-29 订正：原先说的「3店 Deer / 4店 Flow」和妙手里 10-23 人工上的 10 款对不上（CozWeave H 是 FLow0001–0005，LoomNest J 是 Deer0001–0005），用户确认按已上的来，号从 0006 接着编。
- 迁移包 `毛毯图包处理能力_迁移包_20261027/` 里的三个 Windows skill 改写进 pod-blanket（对应关系见下）。
- mooya 侧：新租户 `ai_temu_deerflow`、字标资格扩到新租户、服务接口取图口径、货号预留与分配接口。
- 已上架的款：pod-blanket 业务库记录为准，和新飞书表「已上架的款」双向同步。
- 不做：销量数据同步（只留列和连接键）、下单、改价、Temu 后台的审核 / 核价。

## 关键决策（用户 2026-10-27 / 10-28 确认）

- 商品资料：每家店一个样板商品。用户在妙手里手工建好（不发布），程序用 `get_site_collect_item_info` 读取，每款只换标题、轮播图、SKU 预览图和货号（详情图模块里用的是轮播图的换成同一位置，别的照样板）；`collectBoxDetailShopList` 只留这家店那一条。
- 品牌（10-29 用户发现采集箱商品没有品牌）：样板克隆带不过去，品牌只在店铺模式详情的 `brandId` 里。每家店配 `brand` / `brand_id`（两家都是 MIANDUDU / 1000000793013，是用户在 FLow0006、Deer0006 上选好后读回来确认的，妙手前端下拉栏旁的 98575110 不是接口用的号；新店要先在妙手里同步品牌，否则接口报「品牌不存在，请同步后重试」），进箱后程序用店铺模式保存写上，再读回核对；没写上不自动发布。
- 发布：勾了直接发布（`temu.publish: auto`）；上线头几天先用 `box`（只进采集箱、人在妙手点发布）。
- 历史款（10-29 用户决定）：已经选中、当时没分店的款，在候选表的原图行或「套图」行勾店铺就当场领号、准备上架，不用点按钮；两行和「选中的款」同步。旧租户（platform_internal）出的套图里点缀版 10 张的 68 套在 mooya 整体挪到 ai_temu_deerflow（不重出）；10-26 前的老格式 66 套不处理，勾了程序取消、备注写原因。
- 店铺在候选表选：每家店一列勾选框（列名「3店 CozWeave H」），勾哪列上哪家店，勾任意一列算选中；两列都勾就两家店都上，各有各的货号。「选中」列留着（聊天卡片定的款、不上这两家店的款用），开着店铺列时默认视图里藏起来；按钮行勾任意一个勾选框都算点了按钮。旁边「货号」列程序写。
- 货号由 mooya 分配：按店铺前缀续号（`FLow0006` / `Deer0006` …；店里人工用过的号先在 mooya 预留表补上），选完店铺就定。交给 mooya 出套图时文件名用货号，mooya 里的交付包就叫这个名字。
- mooya 新开独立租户 `ai_temu_deerflow` 和 1 个登录账号：
  - 和 ai_temu 一样有贴 MIANDUDU 的导出资格。
  - 自动化以后出的套图、交付包、FLow / Deer 序列都记在这里。
  - 已交出去的旧套图留在 platform_internal。
- 字标由 mooya 贴：用服务接口 `variant=export` 取上架图，和 ai_temu 导出用同一个函数。3店、4店都贴。
- 点缀版每款正好 10 张，按今天人工上架的款同样的位置放（10-28 读了 8 个线上款核对：mooya 交付包顺序不变，只把尺码图挪到第 3 张），对不上的不自动上：
  - 1–2：04 模特盖腿读书 / 05 沙发搭毯，先后由 mooya 按包抽签。
  - 3：尺码对照图。
  - 4–8：其余 5 张点缀场景（按 mooya 顺序：01 模特手持、02 床上平铺、03 户外挂晒、06 车内座椅、09 模特胸前放松）。
  - 9：适用场景（08）。
  - 10：机洗（07）。
- 详情图不放（线上款 `goodsLayerDecorationReqs` 都为空，照样板）。主货号和每个 SKU 的货号都填货号，和 2店一致。SKU 预览图按样板里每个尺码用的图位，换成这款同一位置的图。
- 标题：temu-blanket-title 的规则原文移植成 P15，看 1.jpg 写；程序复核移植 blanket_batch verify 和合规词表。定制款不自动上。
- 「已上架」= 妙手采集箱 published（已提交到 Temu 店铺）。已上架的款自动勾「下载确认」，上架状态 `listing_marks` 当场记已上架。
- 「选中的款」（实现时定）：加「店铺」（多选）「货号」「上架标题」（文本，人可改，确认时按同一套规则复核，不合规不上、原因进备注）「确认上架」（勾选框）四列；「上架状态」多两个选项「上架中」「上架失败」，按这一款所有店铺汇总写，「待上架」视图也看这两种；「上架备注」逐店写到哪一步。程序只写和上次写的不一样的格子，勾只在要改时写；没上成的勾由程序取消，人再勾就是重试；在上 / 上过的人取消勾会被写回。
- 素材没好（套图没出完、旧租户出的套图、图位对不上、货号 / 标题还没有）不算失败：确认了就排着队等，好了自动接着上；定制款、样板不能用、妙手业务错误才算没上成。
- 已上架的款以库为准：程序写的列库写飞书；人填的列（Temu 状态、商品 ID、销量、备注）回读进库；飞书删掉的行从库恢复。

## 旧 skill → 新模块

- **wechat-flr-blanket-pipeline**：
  - 解压 zip、按 FLR / JUH 分店，改成直接用 pod-blanket 的套图交付清单（mooya `plan_package_files` + `pin_blanket_tail`），店铺看候选表的勾。
  - `write_member_as_jpg`、`image_sort_key` 原样移植到 `podflow/temu_assets.py`。
  - 「店铺轮播图/FLRxxxx/1.jpg…」变成每款一个上架图包 `<货号>/1.jpg…10.jpg`。
  - 联系图改用飞书缩略图；微信 / TraceMemo / Windows 路径相关的都去掉。
- **temu-blanket-title**：
  - SKILL.md 流程进 `prompts/P15.md`。
  - `compliance.md` / `holiday-calendar.md` 进 `references/temu_title_rules.json`，提示词和程序复核共用一份。
- **temu-blanket-batch-title**：
  - `verify` 的五项检查进 `podflow/temu_title.py`。
  - 写 xlsx、Office 锁处理不要了：标题进「选中的款」的「上架标题」列；`podctl temu titles --xlsx` 可导出同格式两列表。
- **append_titles.py**：不需要，前缀由店铺配置决定。

## 架构

```mermaid
flowchart LR
  pick["候选表勾 3店 或 4店 列"] --> accept["选款完成 / 先出套图"]
  accept --> code["mooya 分配货号 FLow0006"]
  code --> scenes["交 mooya ai_temu_deerflow 出套图 文件名=货号"]
  scenes --> assets["variant=export 取图 贴 MIANDUDU"]
  assets --> jpg["校验 10 个图位 + 转 1.jpg..10.jpg 传 OSS"]
  jpg --> title["P15 看 1.jpg 写标题 + 程序复核"]
  title --> table["选中的款: 店铺 货号 上架标题"]
  table --> confirm["人勾确认上架"]
  confirm --> box["按店样板克隆 create_collect_box_item"]
  box --> brand["店铺模式写品牌 brandId"]
  brand --> pub["save_move_collect_task"]
  pub --> done["查到 published: 已上架"]
  done --> db["业务库 listed_products"]
  db <--> archive["飞书 已上架的款"]
```

## 模块与数据（pod-blanket）

- `podflow/miaoshou.py`：只管妙手接口（签名、发送字节与签名一致、错误分类、节流），不碰业务库。
- `podflow/temu_assets.py`：向 mooya 取贴好字标的交付图、校验 10 个图位、旧转换、传 OSS、打上架图包。
- `podflow/temu_title.py`：P15 调用与复核。
- `podflow/temu.py`：每店样板、克隆、建箱（超时对账：采集箱列表没有货号，按标题 + 店铺 + 建箱时间找候选，再读详情核对货号）、写品牌（`_ensure_brand`：店铺模式详情的 brandId，记在 `temu_brands`）、批量发布、查 published、（款 × 店）状态机、写「选中的款」。
- `podflow/listed_table.py`：`listed_products` 和飞书「已上架的款」双向同步。
- `podflow/stores.py`：店铺配置、`store_listings`、向 mooya 领货号；`podflow/candidate_stores.py`：候选表店铺列与货号列。
- 新表（只加表，不改旧表）：`store_listings`（款 × 店：货号、状态、妙手编号…）、`listing_assets`、`listing_titles`、`listed_products`、`temu_rows` / `candidate_codes`（上次写进飞书的值）、`scene_set_clients`（每套交它的 mooya 客户端，代替给 `scene_sets` 加列）、`temu_brands`（款 × 店：给哪个采集箱商品写了哪个品牌、是不是程序写的、失败次数）。

## mooya 侧改动（`/prod_data/mooya_tool_collection`）

- `scripts/seed_ai_temu_tenant.py` 支持 `--tenant ai_temu_deerflow`：建租户、路由 / 工具授权、1 个登录账号，预览水印关。
- `services/showcase_badge.py`：字标资格从只认 `ai_temu` 改成认 `BADGE_TENANTS`（ai_temu、ai_temu_deerflow）。
- 服务接口：
  - 新客户端 `pod-blanket-deerflow` 绑新租户。旧客户端保留，因为查 job 按租户过滤。
  - 取图加 `variant=export|raw`。
  - 货号预留表与 `POST /api/service/v1/codes`；交付工作台推荐序号把预留算进去。

## 风险与待探测

- 店铺 id：采集箱接口里是整数（1店 17356855），`v2_…` 不能直接用；要应用开「店铺」权限后用 `podctl temu probe` 对上。
- 妙手原样存我们的 OSS 签名链接（10-29 确认收下了 10 张、带 query 的链接），链接 30 天有效：采集箱里的商品要在这之前发布。限流额度还没摸到。
- 店铺模式保存 `save_shop_collect_item_info` 第一次由程序真写是 4店第一款：要读回核对没有改动别的字段。
- mooya 那边另有会话在改 rug / 管线代码，改 mooya 时只动本功能需要的文件，共享文档做小范围编辑。
