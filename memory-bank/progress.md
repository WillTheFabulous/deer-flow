# Progress
[Last Updated: 2026-11-02]

## 已完成里程碑

### 2026-11-02 SQLite 回滚文件提前删（`feature-plans/deerflow-postgres`）
- 做了什么：用户以为 SQLite 早下线了，核查是运行上早已全用 Postgres（pod_blanket 10-13、deerflow 10-29），只剩回滚文件。用户定提前删（`/work` 满了）。切换后一条 run 都没有，先做飞书真人验证：09:18 旧线程 `f3b752e1`（10-11 建）上的 run 成功、`/pod` 正常、网关日志无错。
- 删了：`data/deerflow.db*`、`backups/sqlite-to-pg-*` 两份快照、`backups/migrate-default-20261009-183312/`、pod-blanket `podflow.db*` 与 `.bak-20261013*`（约 610MB，`/work` 933M → 1.6G）。
- 保险：先拍 `deerflow_20261102_091948.dump`（29 张表），正式快照 gzip 到根盘 `/root/deerflow-sqlite-archive/`（sha256 核对一致，11-12 后删）；删前扫宿主机和容器确认没有进程打开。
- 部署要点：从此不能再回滚到 SQLite（`deploy/fork/config.yaml` 注释、DEPLOYMENT.md 三处已改），恢复只靠 Postgres 备份；定时备份 cron 仍没加（等磁盘清理）。

### 2026-11-02 1店 / 2店进 POD 全自动上架（00:41 上线；`feature-plans/pod-listing-autopilot`）
- 做了什么：用户要候选表也能勾 1店 / 2店、其余状态列跟着同步。自动化款货号用 FLRP / JUHP（mooya 按租户续号，沿用 FLR / JUH 会从 0001 起号撞上人工精作款），mooya `ai_temu_deerflow` 只加 s1 / s2 两行（`rules=pod10`、`ui=0`、自动发布）；上架后进「已上架的款」，精作款照旧按客户端分。配置顺序 s1 → s4，四家都全自动。
- 关键文件：`books.py`（周表格「候选表模板」店铺列少了也补）、`listed_table.py`（`local_columns`、`status` 按「客户端/店铺」）、`crafted.py`（`_store_views` 不删本机店视图）、`mooya_listing.py`（doctor 比对店名）、新 `test_four_stores.py`；人设 SOUL 已重装。
- 验证：SQLite 全量 646、Postgres 相关 107，5 项变异全抓到；四家样板 SKU / 申报价 / 类目逐项一致，2店 SKU 预览图不碰第 3 张尺码图；`doctor`「店铺配置两边一致」；第一次 `table sync` 35 张候选表补列并按 1 → 4 重摆、两张「候选表模板」补列。
- 部署要点：mooya 先配店、这边再改配置；改前紧挨着查过 s1 / s2 没有任何记录；备份 `pod-blanket-skill-20261102-003950.tgz`、`pod_blanket_20261102_003950.dump`、`podflow.yaml.bak-20261102-stores12`。
- 经验：同一家店跨租户配两行时前缀必须不同（mooya 按租户续号与查重，这里 `listed_rows` 以货号为主键）。

### 2026-10-31 精作款周表格（12:29 上线；`feature-plans/pod-weekly-books` 精作款一节）
- 做了什么：用户要 mooya 上做的款（1店 / 2店）单独按周登记：知识库「精作款」节点下锁住的「精作款模板（勿改）」+ 每周一本「精作款 W44（10.26–11.01）」，每天一张表「10-26 周一」，按上架日期；「已上架的款」只放 3店 / 4店。历史全部按周迁（W30–W44 共 15 本、60 天）。
- 关键文件：新 `crafted.py`；`listed_table.py`（`place` 给 `crafted:<day>`、店铺列分两套、模板锁上前不写、`sync --batch`）；`books.lock_tables`；`db.py`（`crafted_books` / `crafted_days` / `crafted_places`）；`cli.py`（`temu listed crafted template|lock|status|migrate`）；`test_crafted.py`。
- 验证：SQLite 全量 641、Postgres 相关 100，11 项变异全抓到；线上迁移 0 失败（删 1561 行、旧表格那张整张删、W44 删 1店 / 2店视图、建 60 天），12:39–14:15 重写完 1561 行，60 张天表逐张和业务库一致，`temu listed check` 通过。
- 部署要点：模板「看图」的封面效果只能人在飞书里设，要在复制周表格之前设好再锁；迁移前先回读一遍（人刚填的值不丢）；备份 `pod-blanket-skill-20261031-122903.tgz`、`pod_blanket_20261031_122903.dump`。
- 经验：假飞书整本复制会换视图 id（线上不换），`_ensure_views` 改成以表里现在有的视图为准，两边都对。

### 2026-10-31 套图并发 2 → 6、前端默认停、新成员脚本开通（02:12–02:46）
- 做了什么：约 200 款排队、每小时只出 9 套，用户不扩服务器、要直接到 6。停 deer-flow 前端（Next.js 开发服务器 4.7G + 2.2G swap）腾内存；`podflow.yaml` 新加 `scenes: {max_in_flight: 6, poll_seconds: 60}`；mooya 5175 配套（8G、生图名额前 8 个让给 5173、本地中断不砍配额、僵尸判定 8 分钟，`c9fc487`）。前端归 `web` profile、nginx 只依赖 gateway、gateway 6G；`rebuild-dev.sh` 加 `DEER_FLOW_FRONTEND=1`。新增 `scripts/fork/onboard_member.py`（add / code / list），自助注册关掉。
- 关键文件：`docker/docker-compose.cursor-agent.yaml`、`scripts/fork/{rebuild-dev.sh,onboard_member.py}`、`backend/tests/test_fork_onboard_member.py`、`deploy/fork/config.yaml`、`backend/.deer-flow/pod-blanket/podflow.yaml`（备份 `.bak-20261031-scenes6`）。
- 验证：网关容器里 7 条测试过（3 处变异都红），ruff 干净；`list` 连线上主库列出 3 个账号；`/register` 回 403；`/health` 200、网页 502；02:45 下一个套图进程交满 6 套（5175 RUNNING 6）。
- 经验：`scenes sync` 进程启动时就读好了配置，改并发要等下一个进程或发 SIGINT（容器里没有 `kill`，用 python `os.kill`）；宿主机 `backend/.venv` 已落后于代码（缺 `deerflow_extension_api`、langgraph 无 `DeltaChannel`），单测在网关容器里跑。

### 2026-10-31 Temu 上架统一到 mooya（00:26 / 00:30 上线，01:22 补丁；mooya `feature-plans/temu-listing-unify`）
- 做了什么：上架引擎只留 mooya 一份。`temu.listed.source: mooya`：「已上架的款」按客户端增量拉 mooya 四家店底账进 `listed_rows`（1店 / 2店回填 1467 + 交付工作台 94 款，本机 98 款推过去），人填列 / 改标题写回 mooya；`temu.engine: mooya`：确认上架的交单给 mooya，状态拉回 `store_listings`、轨迹进 `trace_events`（stage `mooya`），本机不再碰妙手。
- 关键文件：新 `mooya_listing.py`；`listed_table.py`、`temu.py`、`db.py`（只加表 `listed_rows` / `mooya_listings`）、`mooya.py`、`title_sync.py`、`trace.py`、`doctor.py`、`cli.py`；`test_mooya_listing.py`。
- 验证：SQLite 全量 634、Postgres 相关 94，9 项变异全抓到；切换前对拍 3店 / 4店 43 款（图序、图位、载荷、站点、价格）43/43 一致；第一批 FLow0063 / Deer0048 两分钟内已上架。
- 部署要点：线上与底稿 0 差异直接拷；备份 `pod-blanket-skill-20261031-002556.tgz`、`pod_blanket_20261031_002556.dump`、`podflow.yaml.bak-20261031-ledger`；`secrets.env` 加 `MOOYA_TOKEN_AI_TEMU`。回退：mooya s3 / s4 `enabled=0` + `temu.engine: local`。细节见 `.pod-temu-NOTE.md` 10-31 一节。
- 经验：人工款的妙手图链接大多已失效，取不到主图要能不带主图建行；`cp` 在这台机器上是 `cp -i`，拷线上一律 `command cp -f`。

### 2026-10-30 pod-blanket「上架状态」进妙手才算上架中 + 放行 6 套白底图等人工（20:41 上线）
- 做了什么：用户定等套图、出套图都算待上架，进了妙手（建箱起）才是上架中。之前全自动一确认就是 queued，被算成上架中（10-29 那张 164 款，多数在等套图）。另查「套图等人工」：mooya 白底图兜底只对 13:20 后建的任务生效，之前建的 6 个停在门控（含 4店 Deer0017），在 mooya 生产库按兜底口径放行。
- 关键文件：`temu.py`（`MIAOSHOU_STATES`、`aggregate`）、`feishu_table.py`（`LISTING_HELP_TEMU`、`ensure_options` 记号带说明、`add_options(rewrite_help=)`）、`test_temu.py`。
- 验证：SQLite 全量 627、Postgres 相关 105，5 项变异全抓到；`table sync` 5 张表说明换新、10-29 重写 157 行，读回 待上架 157 / 上架中 1 / 已上架 65，与业务库一致；6 套 20:37 前全部出完，Deer0017 20:38 已上架，「POD 待办」不再有套图等人工。
- 部署要点：备份 `pod-blanket-skill-20261030-204139.tgz`、`pod_blanket_20261030_204140.dump`；mooya 放行备份 `/work/backups/mooya-baseline-gate-release-20261030-202450.json`。用户决定不另加保险。

### 2026-10-30 pod-blanket「选中的款」按上架状态分视图（19:41 上线，视图版本 2）
- 做了什么：用户要每天的「选中的款」按上架情况分视图：从左到右 表格「全部」/ 画册「看图」（保留）/ 表格「待上架」（待上架、上架失败、空，用户定失败归待上架）/「上架中」/「已上架」，「不上了」只在「全部」，链接仍打开「待上架」。之前「待上架」视图连上架中、上架失败一起看（10-29 那张 165 款上架中都在里面）。
- 关键文件：`views.py`（`ensure_selected_views` 五视图 + 就地升级、删 `filter_to_list`）、`feishu_table.py`（`SELECTED_VIEWS_VERSION = 2`、`add_options` / `build_selected_views`、`_views_work` 看 `views_version`）、`books.py`（`upgrade_selected_templates`、`ensure_day` 带版本）、`temu.py`。
- 验证：在 `.pod-selected-views-wip` 做，和 19:04 的「全部画册」三方合并（3 处文档冲突）；SQLite 全量 625、Postgres 相关 125，13 项变异全抓到；`table sync` 旧表格 + 4 张天表 + 2 张模板 0 错误；读回 10-29：待上架 0 / 上架中 164 / 已上架 59。
- 部署要点：备份 `pod-blanket-skill-20261030-194141.tgz`、`pod_blanket_20261030_194141.dump`；`.pod-temu-NOTE.md` 记了 `.pod-temu-wip` 下次上线要合并。经验：这个 shell 的 `cp` 带 `-i`，脚本里用 `command cp -f`；飞书列视图接口不返回筛选，核对要逐个读视图详情。

### 2026-10-30 pod-blanket 候选表「全部画册」（19:04 上线，视图版本 6）
- 做了什么：用户要在表格「全部」后面加一个全部的画册、不放套图：从左到右 全部 / 全部画册（待选、已选中、没选中、已修改、已清出）/ 已选中 / 待选。飞书接口不能调视图先后，已有画册按先后依次改名改筛选、缺的最后补建（人拖乱了先改临时名字），封面效果跟着视图走。
- 关键文件：`views.py`（`CANDIDATE_BOARDS`、`_candidate_boards`、`_rename_boards`、`Layout.whole` / `boards()`）、`candidates.py`（`VIEWS_VERSION = 6`、`ALL_GALLERY_OPTIONS`、`WHOLE_FIELDS`）、`candidate_stores.py`、`books.py`、`table_template.py`，测试三个文件（新增 v5 挪名字、拖乱顺序、v4 一步到 v6、「全部画册」不含套图）。
- 验证：SQLite 全量 622、Postgres 相关 89；先在旧表格模板上实测；`table sync` 35 张 + 2 张周表格模板 0 错误；S36「全部画册」102 张（已选中 68 + 待选 34，没有套图行）。
- 部署要点：线上从 18:32 没变，直接拷 13 个文件；备份 `pod-blanket-skill-20261030-190345.tgz`、`pod_blanket_20261030_190346.dump`；SOUL 重装。最后新建的「待选」封面效果是默认「填充」，要用户在三张模板里设「适应」。

### 2026-10-30 pod-blanket 候选表三个视图（18:07 上线）
- 起因：用户两次说原图行的店铺勾「不见了」（S33、S36）；接口逐行核对 35 张表，勾都在，是选中后离开了默认的「待选」表格视图。用户定：表格「全部」、画册「已选中」、画册「待选」。
- 做了什么：视图版本 5，旧表就地改（删最后的「全部」、第一个表格改名去筛选、「看图」改名「已选中」保留封面效果、建「待选」画册）；去掉「看图跟着选款进度」；三张候选表模板（旧表格、模板表格、W44）一起升级；`table galleries` 列两个画册。
- 关键文件：`views.py`、`candidates.py`、`candidate_stores.py`、`books.py`、`table_template.py`、`doctor.py`、`cli.py`，测试 `conftest.py`（删视图、清筛选）/ `test_candidate_views.py` / `test_table_template.py` / `test_weekly_books.py`。
- 验证：SQLite 全量 619、Postgres 相关 86；先在旧表格模板上实测；`table sync` 35 张 + 2 张周表格模板 0 错误；S36「已选中」68 张都勾着店铺、「待选」34 张。
- 部署要点：线上从 17:30 没变，直接拷 14 个文件；备份 `pod-blanket-skill-20261030-180541.tgz`、`pod_blanket_20261030_180541.dump`；SOUL 重装。新「待选」画册封面效果默认「填充」，要用户在三张模板里设「适应」。
- 经验：飞书 PATCH 视图时 `filter_info: null` 被忽略，要给空条件才能去掉筛选。

### 2026-10-30 pod-blanket 候选表流程改造 + 策划连接修复（15:45 上线）
- 起因：S34 候选只有 10 张、S32 / S35 / S37 少一大半——10-29 17:46–17:51 Postgres 单测与线上共用 `pod_blanket` 角色 30 个连接，并行策划报「too many connections for role」；S33 选款后店铺勾「不见了」是选中的行离开了「待选」视图（数据没丢）。
- 做了什么：测试库改归独立角色 `pod_blanket_test`；`db.py` 连接重试、`plan.py` 失败组串行重试；犹豫期 5 分钟；删「▶ 选中的先出套图」行与任务列表按钮；「选款完成」随时能点（标记，清掉犹豫期记录再领号）；表名【已选N款】/【选款完成】/【有新图】；列说明跟上；选中的款都定了店铺时看上架归档；套图进程每一轮写表、拉起上架。
- 关键文件：`candidates.py` / `candidate_stores.py` / `shelf.py` / `temu.py` / `scenes.py` / `db.py` / `stages/plan.py` / `ui.py` / `cli.py` / `doctor.py` / `views.py`，测试 6 个（新 `test_db_retry.py`）。
- 验证：SQLite 全量 617、Postgres 相关 135；线上表格同步删 34 行、改名 27 张、0 错误；`doctor`「勾店铺后等 5 分钟」。
- 部署要点：线上从 14:50 没变，直接拷 22 个文件；备份 `pod-blanket-skill-20261030-154456.tgz`、`pod_blanket_20261030_154456.dump`、`podflow.yaml.bak-20261030-grace5`；SOUL 重装；16:31 S32 / S34 / S35 / S37 依次补完策划（B057 / B058 / B059 / B061，各 52–54 个方向，没有失败的组）。
- 经验：测试和线上不要共用一个数据库角色（连接上限按角色算）；改行为的开关前先普查会被影响的任务（这次没有任务会被新归档规则当场收起）。

### 2026-10-30 pod-blanket 全自动上架 + 全链路留痕 + 飞书改值同步（06:32 上线，`temu.auto` 关着）
- 做了什么：全自动的店勾店铺 = 选中 + 上架（2 分钟撤销窗口），程序确认、建箱、写品牌、发布；取消「确认上架」= 暂停（进了采集箱的也不发布）；货号只对 4xx 计次、没号不交 mooya；套图自愈；`publish_unknown` 防重发；品牌慢速复查、在箱 20 天重签链接；人改格子全落库、程序写的上架列改回；标题两张表随时改、按环节同步；「当前环节」等四列、`asset --timeline`；「上架要你看」与红卡；Temu Partner 接口客户端（关着）。
- 关键文件：新 `trace.py` / `cells.py` / `title_sync.py` / `temu_open.py`，改 `temu.py` / `candidate_stores.py` / `stores.py` / `scenes.py` / `listed_table.py` 等 34 个；13 张新表。mooya：`service_showcase.py`（`baseline_fallback` / `baseline_info`）、管线白底图兜底、`service_api.py` `/actions`。
- 验证：SQLite / Postgres 全量各 608（新 34 条），之后的改动相关 88；mooya 断言 215 / 246 / 226 / 68，dev `/actions` 路由在。
- 部署要点：线上自 10-29 21:13 没人改过，直接拷；备份 skill tgz、`pod_blanket_20261030_063155.dump`、`podflow.yaml.bak-20261030-autopilot`；`table sync` 补了 5 张「选中的款」四列；SOUL 重装。13:2x 开 3店全自动（13:09 第一次开时存量数错、超范围发布 13 款，见 feature 事故记录）、13:43 开 4店（99 款一起走：发布 11、排队 88），mooya 配套 13:20 上 5175（`0ce7c38`）；Temu 授权等用户。
- 经验：等货号的套图会让 `scenes sync` 空转到 40 分钟（只剩等货号就退出）；暂停要连自动发布一起停，否则进了采集箱的照样发出去。

### 2026-10-29 pod-blanket 历史款勾店铺即上架 + 68 套旧任务挪 mooya 租户（21:13 上线）
- 做了什么：已选中的款（含当时没分店的历史款）在候选表原图行或「套图」行勾店铺，事件到了就记店铺、领号、准备上架，两行和「选中的款」同步，巡检每 30 分钟兜底；最近一套出完、图位校验不过的（老格式 / 旧包没有前排钉序）取消勾写原因。旧租户能上的 68 套在 mooya 挪到 ai_temu_deerflow（新脚本，含尺码图单例、父目录、服务客户端），业务库 `scenes set-client` 记成 `deerflow`，上架就绪 6 → 74 款。
- 关键文件：`candidate_stores.py`（`tick_targets` / `apply_ticks` / `poll`、套图行镜像 `candidate_scene_codes`）、`candidates.control_event`、`scenes.set_clients`、`temu_assets.slot_check`；mooya `scripts/move_service_jobs_tenant.py` + 断言。
- 验证：6 条新用例、3 处变异变红，SQLite 574、Postgres 相关 148；mooya dev 断言 29 项、2 处变异变红；实测 IMG03738 勾 3店 → FLow0008、26 秒备好带字标的图，IMG02158 被拒。
- 部署要点：mooya 先 dry-run 给用户看、pg_dump 后执行，报告带回滚；第一次没改服务客户端（服务接口按 `serviceRequest.client` 认 job），`--retag` 补改。
- 经验：mooya 服务接口认 job 要租户和客户端都对上；共享单例（尺码图）不能跟着挪，要换成目标租户的。

### 2026-10-29 DeerFlow 主库 SQLite → Postgres（18:57 切换，含旧对话上下文）
- 做了什么：用户问 SQLite 还要不要，选「迁到 Postgres 并把旧对话上下文一起搬」。mooya Postgres 新建库 `deerflow`（角色 `deerflow`，连接上限 30）与测试库 `deerflow_test`（单独角色，连不上正式库），`.env` 加 `DATABASE_URL`，`database.backend: postgres`。DeerMem 检索索引仍是 SQLite（上游写死）。
- 关键文件：新脚本 `scripts/fork/migrate_sqlite_to_postgres.py`（上游 bootstrap 建到 head、ORM 表按外键顺序复制、检查点经 `AsyncPostgresSaver.aput`、writes 原样、逐行逐检查点核对）、`backend/tests/test_fork_migrate_sqlite_to_postgres.py`（15 个，6 处变异变红）；`backup_pod_blanket.sh` 加 `PG_USER`；`deploy/fork/config.yaml` 的 `database`。
- 验证：演练与正式各迁 493 行 + 3137 个检查点 + 3917 条 writes，0 差异，约 34 秒；以两个所有者身份走 HTTP 接口，10 个会话的消息数与快照一致；3 条飞书绑定经仓库层解析正确。
- 部署要点：`podctl pause` 等 worker 退出 → 停网关 → 一次性容器（`compose run --rm --no-deps`）迁移 → 网关停着时改 config → `--force-recreate`；飞书离线约 1.5 分钟；回滚只把 `backend` 改回 `sqlite`。首份备份 `deerflow_20261029_190224.dump`。
- 经验：config 会热加载，引用新环境变量要在旧容器停掉后再改；Postgres saver 不存 metadata 的 `writes`（57 个检查点）；宿主机 `cp` 是 `cp -i`，变异测试还原要用 `command cp`。详见 `feature-plans/deerflow-postgres/`。

### 2026-10-29 pod-blanket Temu 采集箱商品补写品牌 MIANDUDU（17:52 上线）
- 做了什么：第一款 FLow0006 进箱后用户发现「品牌名」是空的——样板里本来就没有品牌，品牌只在妙手店铺模式详情的 `brandId` 里，站点模式建箱接口没有这个字段。用户在妙手里选好后读回定了写法和编号（1000000793013；前端下拉栏旁的 98575110 不是它）。进箱后 `_ensure_brand` 读店铺模式详情，只改 brandId 整份存回去再读回核对，人选过的只记一笔；没写上不自动发布、每轮补写、满 6 次停。
- 关键文件：`temu.py`（品牌一节）、`miaoshou.py`（`shop_detail` / `save_shop_detail`）、`stores.py`（`brand` / `brand_id`）、`db.py`（新表 `temu_brands`）、`doctor.py`、`maintenance.py`；测试 5 条。
- 验证：以每周选款表 16:55 的线上为底三方合并 4 个文件（无冲突），SQLite 568、Postgres 相关 115；线上 FLow0006 记成已有品牌（没重写）。
- 部署要点：备份 `pod-blanket-skill-20261029-175231.tgz`、`pod_blanket_20261029_175231.dump`、`podflow.yaml.bak-20261029-brand`。程序第一次真写品牌在 4店 Deer0006，要读回核对。
- 经验：接口文档没写清的字段（品牌存在哪、编号是哪个），让人在界面上操作一次再读回来，比猜省事；测试里不要用秒级时间戳区分「这一轮」。

### 2026-10-29 pod-blanket 选款表链接导向这一周、去掉「场景图」、看图跟着选款进度（16:55 上线）
- 做了什么：对话里人设给的「选款表」改成这一周的周表格（`table status` 的 `url`，旧表格在 `legacy_url`）；「选中的款」不再生成、也不再保留十来张套图缩略图（「场景图」列从 6 张表里删掉），候选表套图行的 1 张缩略图保留；候选表「看图」选完前看待选 / 已选中 / 没选中，【已选】后只看已选中。
- 关键文件：`feishu_table.py`（status、SCENE_FIELDS）、`scenes.py`、`maintenance.drop_scene_images`、`views.any_option`、`candidates._sync_galleries`；SOUL 重装。
- 验证：新用例 + 8 项变异；SQLite / Postgres 各 563；读回 25 张表的「看图」筛选都对。
- 经验：改视图筛选只用 v1 更新视图，封面效果不受影响；破坏性的删列先 dry-run 给用户看。

### 2026-10-29 pod-blanket Temu 上架打开（box 模式，15:36），第一款备好待确认
- 做了什么：订正店铺 ↔ 前缀（3店 CozWeave H = FLow、4店 LoomNest J = Deer），mooya `ai_temu_deerflow` 补 FLow / Deer 0001–0005 预留，下一个 0006；建箱超时对账改按标题 + 店铺 + 时间找候选、读详情核对货号（妙手采集箱列表没有货号）；线上 `podflow.yaml` 打开 `temu`（两家店、样板 3476402596 / 3476332173）。第一款 IMG03683 定 3店、领 FLow0006，10 张图和标题已写进 W44「10-28 选中的款」。
- 关键文件：`temu.py`（`_find_created` / `_miaoshou_time`）、`tests/conftest.py`（假妙手列表 `itemNum: None`）、`test_temu.py` 两条对账用例；文档前缀例子。
- 验证：以线上为底三方合并 10 个文件（无冲突），SQLite 全量 561；`temu probe` 能看到两家店、样板都能用（类目 11901 标题上限 250）；`doctor` 通过；表格同步补列 0 失败；`temu preview` 核对图序、SKU 预览图位 1 / 2 / 2 / 1、价格、货号，10 个 OSS 链接都能取。
- 部署要点：等每周选款表那一路的 `migrate-week` 跑完才拷；备份 `pod-blanket-skill-20261029-153346.tgz`、`pod_blanket_20261029_153349.dump`、`podflow.yaml.bak-20261029-temu`。
- 经验：shell 里 `cp` 是 `cp -i`，无人值守时拷文件用 `command cp`，否则不会覆盖、也不报错；同步前先核对线上在合并之后有没有被别人热修。

### 2026-10-29 pod-blanket 每周选款表 + 锁住的模板（14:10 上线，15:07 W44 迁移完）
- 做了什么：知识库根目录放一本「POD 选款表模板（勿改）」（高级权限开着、编辑者只读，「看图」封面效果在里面设）。每周从它复制一本「POD 选款表 W44（10.26–11.01）」，每天一个文件夹：当天上传的任务的候选表、「10-26 选中的款」、「10-26 已归档」子文件夹；「已上架的款」每本一张。任务按上传日期归到那一天；10-26 起的 S16–S27 搬进 W44，旧表格里它们的 11 张候选表和 76 行「选中的款」删掉。
- 关键文件：新模块 `books.py` / `table_registry.py` / `week_migration.py`；`feishu_table` / `candidates` / `downloads` / `listing` / `temu` / `scenes` / `listed_table` / `table_template` / `doctor` / `cli` 按份分流；新表 `table_books` / `table_days` / `seed_places` / `table_places` / `listed_books`；CLI `table books [--lock]`、`maintenance migrate-week`；开关 `table.weekly`。
- 验证：12 个新用例，12 项变异变红；SQLite / Postgres 各 558；迁移逐格核对通过才删旧表，只读复核新旧行数、选款结果一致。
- 部署要点：用户开了 `wiki:node:create`、`base:app:read / update`、`base:role:read / update`；线上 `table.weekly: true`；备份 tgz、dump 和迁移 JSON 在 `/work/backups`。待事件实测和新一周手测（见 feature progress）。
- 经验：飞书单条写一行 3–4 秒，成批写要用 batch（第一版逐行建，两个多小时，中途改成一批 50 行）；整本复制是异步的（1254036），高级权限也跟着复制；v3 角色接口返回双重 JSON 字符串，改 Editor 要把三条规则带全。迁移要能断点续做，核对不过就不删旧表。

### 2026-10-28 pod-blanket「看图」封面效果默认适应：新候选表从模板表复制（20:13 上线）
- 做了什么：用户要选款表「看图」卡片的封面效果默认是「适应」，但开放接口改不了这一项（卡片接口只认 `cover_field`）。新候选表改为从「模板」文件夹里的「候选表模板（勿删）」复制结构（Base v3 复制数据表，只复制结构），只能在飞书里设的视图设置跟着走；旧的 24 个「看图」由用户照 `table galleries` 的链接手动改。
- 关键文件：新模块 `podflow/table_template.py`（ensure / copy / status / galleries）；`candidates.py` 拆出 `create_fresh` / `prepare_new` / `default_views` / `upgrade_fields` / `add_package_column`；CLI `table galleries`；doctor 一项；开关 `table.template`。
- 验证：先在线上做临时表实测，用户确认复制件的封面效果是适应、我们的视图写入冲不掉它；新增 11 个用例，16 处变异变红；以 temu 那一路的线上为底三方合并（店铺列挪进 `prepare_new`），SQLite / Postgres 各 547。
- 部署要点：模板 `tblNoNiOjOs6w7au` 已建，等用户设适应；模板被删会重建，要再设一次；temu 那一路下次上线要三方合并（`.pod-template-NOTE.md`）。
- 经验：接口改不了的视图设置，就让人在模板里设一次、程序照着复制；复制件落在源表的文件夹里，要挪回来。

### 2026-10-29 pod-blanket 新套图改走 mooya ai_temu_deerflow 租户、mooya 配套上生产（03:43）

- 做了什么：mooya 侧（字标租户、`variant`、`/codes`、迁移 0064）从干净 worktree（`cb75449`）构建上 5175 / 5173，建租户与 Keycloak 账号、加服务客户端；线上 `podflow.yaml` 加 `mooya.clients.deerflow`、`submit_client: deerflow`，doctor 套图检查改探交套图的客户端，`scenes show` 带 `mooya_client`。
- 验证：新旧令牌 health 各回各的租户，job 565 / 566 建在 `ai_temu_deerflow`；SQLite 全量通过（含另一会话的候选表模板）。
- 部署要点：为等在出的套图跑完，`scenes.max_in_flight` 临时设 0 约 5.5 小时；长跑的 `scenes sync` 进程只在启动时读配置，改配置后要停掉旧进程（等租约过期再拉）。
- 运维：`pod_blanket` 角色密码轮换（SCRAM 预哈希经 stdin）；`/work` 清掉 MinIO 旧卷与旧镜像，余约 21GB。

### 2026-10-28 pod-blanket Temu 半托上架（妙手开放平台）引擎上线、开关关着（19:00）

- 做了什么：候选表每家店一列勾选框（勾哪列上哪家店、选完当场向 mooya 领货号，货号也是套图交付包名）；套图出完自动准备 10 张带字标上架图（原 Windows 转换逐字节一致、尺码图第 3）和 P15 标题；「选中的款」加店铺 / 货号 / 上架标题 / 确认上架，人勾确认后以每店样板商品克隆建进妙手采集箱、发布（box / auto），已上架记进 `listed_products` 与飞书「已上架的款」（双向同步）。
- 关键文件：`podflow/{miaoshou,stores,candidate_stores,temu,temu_assets,temu_title,listed_table}.py`、`prompts/P15.md`、`references/temu_title_rules.json`；mooya 多客户端（`mooya.clients` / `submit_client`、`scene_set_clients`）。
- 验证：SQLite 全量 536 通过；Postgres 相关 14 个文件 187 通过；线上 `status` / `doctor` 正常，生产库新建 7 张空表。
- 部署：`.pod-temu-wip` 整体覆盖线上（备份 `/work/backups/pod-blanket-skill-20261028_185812.tgz`、`postgres/pod_blanket_20261028_185802.dump`），人设 SOUL 更新；`temu.enabled` 未开，等 mooya 生产上线、店铺权限与样板。
- 事故：18:32 `/work` 写满（Postgres 容器测试的 pytest 临时目录 248MB 压垮了本已 99% 的盘），共享 Postgres 一次 PANIC 重启、约 5 分钟写入失败；清 Docker 构建缓存 1.55GB 恢复，剩约 1.8GB。

### 2026-10-27 pod-blanket 去掉构图判断、设计建议不锁、动态字段与换画法配额（23:59 上线）
- 做了什么：用户审查全部限制后要「尽量有丰富的变化」：P02 只看能不能改出看得见的变化（去掉「挤动构图判不能变」）；P04 锁定只认用户 / 修订意见 / 印制要求（写 `source`，没写的程序放回变化域）；动作 / 朝向 / 互动 / 承托 / 陪衬五个动态字段进单字段大改；大变化按配额出动态与换画法（线上单字段大改 0.34 / 0.34、三处组合 0.5 / 0.25、二次大变化 0.25 / 0.4），换画法按 23 种画法大类各分一种不重复的。重拆老系列用户说先不做。
- 关键文件：`fields.py`（`slot_kinds` 写了 slots 就按它、`is_dynamic`）、字典五个字段 `slots` / `big_change`、`stages/plan.py`（`big_applicable`、`quota_split` / `big_split` / `combo_split`、`assign_techniques` / `technique_guard`）、`stages/seed.py`（`LOCK_SOURCES`）、P02 / P04 / P05、doctor。
- 验证：新增 12 个用例，15 处变异严格口径变红；SQLite / Postgres 各 497（临时库）；skill / 人设 / SkillScan 198、`tests/skills` 85；预览 22 个系列。上线后发现 F03 换画法上限随调用变、计数与槽位对不上，改成按 `big_single_count` 算的固定上限，00:03 补上。
- 部署要点：回退只改 `podflow.yaml`（quota 改 0）；老系列被判不能变的动态字段靠 `combo_layout_fields` 放进大变化。
- 经验：并发执行者会让变异验证互相还原错代码、引用不存在的用例也会被当成「变红」：变异前先确认目标用例存在且是绿的，变红只认退出码 1。

### 2026-10-27 pod-blanket「选中的款」下载确认 + 排序（21:44 上线）
- 做了什么：用户要在「选中的款」也加下载确认，下图只看这一张表：勾了这一行沉到最底，新出来的套图在顶上；选「和交付包打通」「迁移旧标记」。一款一个勾「下载确认」，表按隐藏「排序」列排三组（待下载 → 还不能下 → 已下载）；一个批次的款都确认了自动标交付包已下载（能自动归档），交付包标了已下载包里的款自动打勾；重出 / 重生过图作废。
- 关键文件：新模块 `podflow/downloads.py`（表 `selected_downloads` / `selected_rows`、写入租约）、`feishu_table.py`（两列、`LAYOUT_VERSION` 3、`views_sorted`）、`packages.py`（`set_fingerprints`、双向打通）、`candidates.py`（事件按表分流、巡检拉起）、CLI `table downloaded` / `maintenance import-downloads`。
- 验证：新增 14 个用例，15 处变异变红，SQLite / Postgres 全量各 483；上线后只读复核 70 行排序与勾、4 个视图的排序。
- 部署要点：开关 `table.download_confirm`（缺省开、线上直接生效）；旧标记迁移先 dry-run（10 款、不自动标包、不归档），用户确认后 21:59 正式跑，10 款打勾沉底。
- 经验：列表接口带 `field_names` 返回文本形式（数字是字符串）；跨进程写同一组格子要把「算 → 写 → 记」放进同一把租约；程序写过的旧勾要和人新打的勾分开。

### 2026-10-27 pod-blanket 只用 sunburst、小变化以换主体为主、二次大变化（19:37 上线）
- 做了什么：用户说小变化很多但图都非常相似（颜色 / 装饰不明显，主体变化最明显），要小变化里大量是换主体，再基于换过主体的小变化做一轮大变化；同日要求只用 sunburst 出图、小 / 大变化都多一些、小变化 80% 换主体。生图只剩 `gpt-image-2.5-sunburst`（17:26 只改配置）；每轮小变化 30（八成至少一处换主体 D09，P14 给每个分不同的新主体）+ 大变化 24（单字段大改 12，同一大字段最多 2 次）+ 二次大变化 16（小变化出完图后程序自动挑换了主体的当底图，图生图输入也换成底图），一轮约 110 张。
- 关键文件：`subjects.py`（must / forbid）、`stages/plan.py`（`small_split` / `assign_swaps` / `plan_derived`）、新模块 `derive.py`（表 `derived_rounds`）、`stages/common.py` / `job.py`（`input_image_bytes`）、`ui.py`（`CARD_TEXT_BUDGET`）、P05 / P06 / 新增 P14。
- 验证：新增 17 个用例，9 处变异变红；和候选表看图那一路两次上线三方合并（0 冲突），SQLite / Postgres 各 471（Postgres 用临时独立库，避开同时在跑的另一路）。
- 部署要点：回退只改 `podflow.yaml`（`small_swap_ratio: 0`、`derived.enabled: false`、数量）；旧批次不会被自动派生；F08 / F10 旧拆解判主体不能换，19:57 按用户要求改 P02 / P04 判断（换外形不算挤动构图、主体可换时不锁不限定，不加例子），两张旧图先不重拆。
- 经验：两路同时开发要用不同的 Postgres 测试库；`--preview` 一类只读参数要在每个分支先判断，加用例断言不落库。

### 2026-10-27 pod-blanket 候选表「看图」卡片与下载列前移（18:49 全部摆好）
- 做了什么：用户要「看图」建表时就有改了什么 / 类型 / 出图方式和「选中」，下载原图、下载套图挪到前面；选「只要这四个字段」「候选表和『选中的款』都改、21 张旧表一起改」。用 Base v3 `visible_fields` / `card`（`base:view:*` 已开）摆视图：候选表表格视图开头 素材 · 缩略图 · 选中 · 三个下载链接，「看图」卡片四个字段、封面缩略图；「选中的款」开头 素材 · 缩略图 · 下载原图 · 下载套图。人藏的列照旧藏，人加的列跟着左边那一列走。
- 关键文件：`podflow/views.py`（`Layout` / `arrange` / `apply_layout` / `_v3_put`，去掉 `hide_field`）、`candidates.py`（`LAYOUT`、视图版本 4、旧表只摆列）、`feishu_table.py`（`SELECTED_FRONT`、`views_layout` 2）、假飞书。
- 验证：新增 5 个用例，6 项变异变红，SQLite / Postgres 各 453；上线后只读复核 21 张表 × 3 视图 + 选中的款 3 视图全对。
- 经验：v3 可见列接口收发的是列名（封面是字段 id）；第一版假飞书写成 id，18:19 上线后表格视图没动、只摆了画册，18:49 修正并升版本重摆。上线后要回读真实接口。

### 2026-10-27 pod-blanket 飞书消息精简：每小时待办 + 每天 3 次状态总结（13:26 上线）
- 做了什么：用户说飞书里推的太多，只要提醒待办（待挑选的裂变结果、待下载的套图）和每天几次总结；选「有待办每小时提醒一次、可配」「10:00 / 15:00 / 21:00」「自动推进的停发，人发起的回一行，出错两行红卡」。开关 `notify.style`（代码 / 样例 full，线上 brief）。
- 关键文件：新模块 `podflow/digest.py`（待办、状态总结、回执与红卡、`run_schedule`）；挂接 `notify`（`bg_finished` / `fix_finished` / `notify_tick`）、`scenes`（套图出完不发、没出成红卡、等人工进待办）、`shelf.announce`、`cli`（`_auto_tick`、`report digest|todo`、brief 时 `report daily` / `ui daily` 给总结）、`doctor`；渠道不改。
- 验证：新增 `test_brief_notify.py` 15 个（9 项变异各自变红）；和多工位那一路三方合并后 SQLite / Postgres 各 448；SkillScan / bundled / 人设 198、`tests/skills` 85。顺带给 `test_feishu_table` 3 个会和真实子进程抢租约的用例加 `spawned`。
- 部署：备份 `pod-blanket-skill-20261027_124941.tgz` / `postgres/pod_blanket_20261027_124941.dump` / `podflow.yaml.bak-20261027-brief`；13:01 按文件拷入，用户看过线上数据预览后 13:25 写 `notify.*` / `schedule.digest_at`，13:26 第一张「POD 待办」；SOUL 重装。回退只改 `notify.style: full`。
- 经验：别的会话会整份覆盖共用文件，新功能的开关判断放在新模块，挂接点只依赖新文件；测试里真拉起的后台子进程会和用例抢租约，副本里没有字节码缓存时必挂。

### 2026-10-26 pod-blanket 多工位出图 worker（19:09 上线）
- 做了什么：用户说后面大量出图、worker 不够、队列要没有空等；选「一个调度进程里开多条并行工位」「生图同时 8 张、P06 同时 3 个」。worker 不再 9 分钟一轮：8 条工位同时推进 8 个方向，共用进程内名额（`limits.py`），有活一直跑，运行中接新确认的方向和刚点的修改（修改优先、其余按任务轮流），没活 60 秒退，跑满 120 分钟或配置 / 代码变化先拉起接班的再退；回收只碰租约过期的任务；批次反馈放单独线程；修改卡片做完一个发一个。
- 关键文件：`worker.py`（重写）、`limits.py`（新）、`state.py`（`claim_job` fair、`recover_stale`、心跳）、`imagegen.py` / `llm.py` / `stages/job.py`（名额与放连接）、`directions.start_runs` / `fixes.start`（不带 `--job`）、`notify` / `bg` / `ui` / `doctor` / `store` / `feishu_table`、渠道 `pod_cards.py` 两句排队文案。
- 验证：新增 `test_worker_lanes.py`（16，连跑 3 遍稳定、6 项变异各自变红），改 4 个旧用例文件；和同日「候选表重排」那一路三方合并后 SQLite / Postgres 各 433，渠道 189。
- 部署：备份 skill / 渠道 / `podflow.yaml.bak-20261026-lanes` / `postgres/pod_blanket_20261026_184604.dump`；暂停后按文件拷入；线上加 `batch.workers: 8`、`image_concurrency: 8`、`llm_concurrency: 3`、`queue_order: fair`、`worker_idle_seconds: 60`、`worker_lifetime_minutes: 120`，删 `worker_max_minutes`；doctor 连接估算 22 / 30。上线 6 分钟完成 8 个方向（约 75 / 小时，原约 24），生图峰值 7 / 8，LLM 3 / 3 占满（P06 平均约 90 秒）是新瓶颈。
- 经验：回收在途工作要按租约过期判断；并发放在「工位 × 全局名额」，等名额时放下数据库连接。
- 2026-10-27 调名额：10-26 晚剩下 37 个方向 33 分钟出完（约 67 / 小时），LLM 3/3 一直占满、P06 中位 130 秒，上游 3 并发时每秒出 token 和串行一样；用户选「LLM 名额 6、工位 12」「只调名额」，线上改 `llm_concurrency: 6`、`workers: 12`（备份 `podflow.yaml.bak-20261027-llm6`），doctor 连接估算 26 / 30；下一批出图时核对，429 / 5xx 或每秒输出低于约 50 token 时降到 4–5。

### 2026-10-26 pod-blanket P06 容错（17:34 上线）
- 做了什么：当天 76 次 P06 有 27 次因漏写 5 个只做记录的字段被退回、6 次一段输出里有两个 JSON，J0361 / J0366 / J0375（及 J0398 / J0399）判失败。`llm.run_stage` 加 `normalize`：P06 只做记录的字段归位 / 补默认值；`json_objects` 解出多个 JSON 取能通过校验的；退回原文写 `logs/llm-rejects/`；S06 几次重答都没过放回队列重写一次（`llm.s06_job_retries`）。
- 验证：`test_p06_tolerance.py`（9，3 项变异各自变红），SQLite / Postgres 各 402。上线后 J0366 那次模型把 8 个字段写在 result 外面、程序挪回；J0361 / J0375 / J0403 是两个 JSON；5 个失败方向 `job redo` 后都出完。

### 2026-10-26 pod-blanket 候选表重排、预留行、先出套图与下载后归档（已上线）

- **用户要求：** 套图出完、交付包下载过才归档，任务多「套图完成」「套图已下载」两档；标了已下载的行沉底；方向一确认就在候选表预留「未生成」的行；出图中能先把选中的出套图（不算选款完成），全部出完才能选款完成，没出图的不算没选中；派发按钮放表顶。选人工标、只认批次交付包、按钮全放表顶，开了 `base:view:*`。
- **做法：** 视图按隐藏「排序」列排（v3 排序，`candidate_orders`），去掉表尾整块重建、旧控制行搬到表顶（`candidate_actions`：dispatch / accept / package）；预留行 `candidate_slots`；`table dispatch`，`accept` 要全部出完（积压挡住的不算）；`package_downloads` + `scenes downloaded [--undo]`、候选表交付包行打勾、卡片「B… 已下载」，标完只扫这个任务；任务 stage 与「下载 a/b」；`shelf.auto_block` 要全部标完。渠道 `pod_dispatch` / `pod_downloaded`。
- **验证：** 引擎 SQLite 417 / Postgres 408，渠道 188，SkillScan 同基线，人设 1、`tests/skills` 85。
- **上线：** 18:07 备份后拷入；首次同步 8 分钟（15 表补列、140 预留行、2424 行排序），画册不认 `hidden_fields`（1254019）、v3 排序有频率限制（800004135）各修一次后视图全部做完；渠道 18:23 写入。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-26 pod-blanket 字段字典 0.5（已上线）

- **用户要求：** 按已出的款看字段够不够、合不合现在的策划方式。复盘（已审 1329、选中 56）：组合 6.5% > 大改 4.4% > 小变化 2.6%，D41 在小变化里 1/168；构图锁挡住组合用动作 / 互动 / 陪衬等字段；物件、装饰几乎只有一个字段；配色只能整套换；有字的图缺文案；没选的图没原因。选 7 项、不选大变化构图降权。
- **做法：** 字典 0.5：单元类型 `object`、D59 单元配色 / D60 表面纹样（`since`，旧拆解按可变）、`slots`（D41 只进大改与组合）、region surface、整幅互斥对整槽生效；`field_pools` 出 small / big / combo 三池，组合另收 `variation.not_variable`（`layout_change`，`plan.combo_layout_fields`）；`style_scope` 只认整幅字段；文案库 `references/text_bank.json`；候选表「没选原因」多选列（`FIELDS_VERSION` 3）+ 新模块 `reasons.py` / 表 `selection_reasons`，P11 与 `table reasons` 用；P13 / P04–P07 / P11 措辞。
- **验证：** 新增 12 个用例；SQLite / Postgres 各 393；SkillScan / bundled / 人设 198、`tests/skills` 85。
- **上线：** 03:10 暂停、备份（skill tgz、库 dump、`podflow.yaml.bak-20261026-f05`）、按文件拷入、写 `plan.combo_layout_fields: true`、doctor、03:12 恢复、SOUL 重装；13 张候选表补列；`seed describe --all --force` 重写 14 张描述（约 19 分钟，0 失败）：33 个物件单元（控制器、太阳、海星贝壳、圣诞袜…），预览里物件挂上主体 / 轮廓 / 加五官 / 单元配色 / 表面纹样。还没真实策划过一轮。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-25 pod-blanket 只跑 GPT 两个模型、每轮 40 个方向（已上线，只改配置）

- **用户要求：** 出图时模型太多，去掉 Nano Banana 和 Edit 模型，只跑 GPT 的两个模型；小变化、大变化都一次性多出方向。选「小变化 24 + 大变化 16（单字段大改 8 + 三处组合 8），一轮约 112 张」「选款里已有的 nano / Edit 图保留」。
- **做法：** 只改线上 `podflow.yaml`（备份 `.bak-20261025-gptonly`）：`fanout` 只留 flare / sunburst、`i2i_edit: false`、`replaced_models` 把 nano 2.1 与 pro 指到 flare、`gen_concurrency: 4`、`plan` 24 / 16 / 8、删 nano 单价。引擎代码、样例配置和测试不动（槽位、卡片张数、P06 的模型家族都按配置算）。
- **验证：** 切换时没有在出图的任务；`doctor` 通过（一轮约 112 张）；`plan --family F01 --preview` 40 个槽位；小变化 2 槽位、大变化 4 槽位，P06 只要 gpt；SkillScan / bundled / 人设 198、`tests/skills` 85。
- **部署要点：** SKILL / guide / usage / runbook 按文件拷入，SOUL 重装；DEPLOYMENT「配置文件」与 Excel 同步（`excel-sync-2026-10-25.md` 同日追加）已更新。单字段大改按系列可变大字段数封顶（5–8 个）；实测旧任务 p50 约 5 分钟，一轮 40 个方向约 3 小时；40 个方向时挑选卡与任务明细卡约 24KB，再多要先给卡片分页。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-25 pod-blanket 任务归档、历史任务与上架状态（已上线，自动归档已开）

- **用户要求：** 候选表越积越多、不知道选过没有；选中的款越来越多、不知道上过没有；做完的任务要归档，又能随时调出继续裂变。选「选款完成 + 套图出完自动归档、卡片带调出」「上架状态三态单选」「两期一起做」，改成「不重传缩略图，同一本表格里挪进『已归档』文件夹」。
- **做法：** 新表 `seed_shelf` / `listing_marks`；新模块 `shelf.py`（归档 / 调出 / 有新动静自动调出 / sweep，候选表经 Base v3 块接口挪进挪出文件夹）、`views.py`（候选表「待选 / 看图 / 全部」、选中的款「待上架 / 看图 / 已上架 / 全部」，链接带 `view=`）、`listing.py`（三列、旧行补「待上架」、`table poll` 回读、`listing set`）；表名前缀【已选】【有新图】；任务列表「归档」按钮与「历史任务（n）」、历史任务卡（搜索 / 调出 / 继续裂变）；渠道 `pod_shelf / pod_archive / pod_restore`；S10 改叫「已入库」。
- **验证：** 新增 22 个引擎用例、渠道 `TestPodShelf`；和「可变单元」那一路三方合并后 SQLite / Postgres 各 378，含补上线修正的终版 SQLite 381，渠道 170，SkillScan 与基线一致。
- **上线：** 19:37 备份（库 `pod_blanket_20261025_193728.dump`、skill / SOUL / 渠道 tgz）后按文件拷入；`table sync` 补三列、56 行补「待上架」、7 表【已选】1 表【有新图】；13 张候选表建「全部」撞 1254020，改成先筛画册、最后建并重试后补上线修好；渠道 19:51 热重载；S03 手动归档 → 调出 → 再归档，候选表跟着挪；20:00 用户确认后 `shelf.auto: true`（备份 `.bak-20261025-shelf`），20:09 第一轮自动收起 S08 / S09 / S12 并发卡。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-25 pod-blanket 小变化按可变单元改（已上线）

- **用户要求：** 单主体图上改一个字段只动一个主体，两个字段也看不出；字段再多多主体图又改太多。要在反推时识别可以变化的主体（成组的同类元素算一个），小变化改两个「主体 - 字段」组合、三字段组合改三个；选「两类一起改」「直接全量切换」。
- **做法：** P13 另出可变单元清单（组 / 成员两层、类型、显著度、不成立的字段、英文点名），字段字典 0.4 加挂在哪 / 适用类型 / 部位 / 依赖 / 互斥 / 档位；新模块 `subjects.py` 配「单元 × 字段」组合（小档不出、脸部降档、同单元两处要大档且部位不同、整幅最多一处；单元均分、组内成员合占一份、同族衰减）；`plan.py` 按 `plan.unit` 与有没有清单分模式，指纹无单元时与旧算法一致、去单元签名拦旧方向；P05 / P06 / P07 / P04 按「处」写，P06 用 `unit_en` 点名；三字段组合改名三处组合；`plan --preview`。
- **验证：** 新增 `test_subject_pairs.py` 15 个、14 项变异变红；引擎 SQLite / Postgres 各 360；SkillScan / bundled / 人设 198、`tests/skills` 85。
- **上线：** 19:09 暂停，按文件拷入线上与 `.pod-wip`，`plan.unit: pairs`（备份 `podflow.yaml.bak-20261025-pairs`），`seed describe --all --force` 补 14 张清单（0 次退回）；抽查后补「按主体数判组」「成员合占一份」再拷入；19:27 恢复，SOUL 重装。另一路 `.pod-shelf-*` 改了其中 8 个文件，试合并干净。还没真实策划过一轮。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-25 pod-blanket 套图只出点缀版、文件名跟 mooya 交付包（已上线，mooya 走 5175）

- **用户要求：** 套图一套 16 张，只要点缀版；图片像 mooya 交付工具一样按数字编号方便上架，建议直接调交付包的功能让两边统一。选「设计图 + mooya 交付包（001–009 场景 + 010 尺码图），不放白底图」。
- **做法：** mooya 侧 `pod_blanket` 只出点缀版（9 张），出完的 job 带 `delivery` 交付清单（mooya 交付工作台「下载交付包」同一个函数）。这边 `_collect` 按清单拉图（清单外的尺码图走 `file-N`，存 role `delivery`），存表 `scene_files`；`zip_entries` 有清单就按它命名，没有退回旧命名；缩略图按清单、不放尺码图；交付包指纹算上清单；新命令 `scenes refresh --all [--seed] [--detach]`。
- **验证：** 引擎 345 SQLite / Postgres（新增 7 个，5 项变异各自变红），ruff；SkillScan / 人设 / 渠道 353、`tests/skills` 85；mooya dev job 257 与 5175 上 prod job 454 的真实清单都是 001–017（尾部适用场景 / 机洗 / 尺码图）。
- **上线：** 用户选「只重建 mooya 5175、deer-flow 指过去」「旧套图不重打」。引擎按文件拷入线上（备份 `pod-blanket-skill-20261025_173027.tgz`），`scene_files` 自动建表；5175 重建（回滚点 `mooya-internal-*:pre-delivery-20261025`）；`podflow.yaml` 的 `mooya.base_url` 5173 → 5175（备份 `.bak-20261025-mooya5175`），doctor 通过；SKILL / references / SOUL 随后上线。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-23 pod-blanket 套图批次交付包（已上线）

- **用户要求：** 问套图出完交付包在哪下载，要把交付包链接放到任务列表和选款表的开头，并考虑同一任务后续的裂变批次与套图；选「每个裂变批次一个包」「放在每个任务自己的候选表里」「已有的表新列加在最后、人手动拖一次」。
- **做法：** 新模块 `podflow/packages.py`（表 `scene_packages`）：批次里没有在出的套图就打包（等人工不挡），每款一个文件夹，内容变了重打、删旧对象，`oss.put_file` 流式上传；`scenes sync` 收尾打包、`kick` 只有包要打时拉起 `scenes pack`。链接放任务列表第一行与最前按钮、候选表「套图交付包」列（新表第 2 列，旧表补在最后；候选行整批改，套图行算进 `state_key`）、任务明细批次标题、套图出完卡片。CLI `scenes packages / pack / repack`。
- **验证：** 新增 `test_packages.py` 16 个，9 个变异变红；和控制行那一路合并后引擎 338 SQLite / Postgres；SkillScan / 人设 / 渠道 326、`tests/skills` 85。
- **上线：** 19:32，以控制行那一路的线上为底三方合并（`candidates.py` 重加）；首轮巡检约 30 秒打出 8 个包（73–520MB），13 张候选表补列、链接写入，下载 206、中文文件名。待用户把旧表的列拖到前面。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-23 pod-blanket 候选表里直接选款完成 + 套图行（已上线，实时待开放平台配置）

- **用户要求：** 选款完成不回聊天框、在候选表里直接做；问验收会不会扣积压（会，刚进表 / 表里删掉 / 没进表的不扣）；选「表最底下加控制行」「实时：事件订阅」，又要套图下载链接列在控制行下面。
- **做法：** 每张候选表最底下「▶ 选款完成」控制行（表 `candidate_controls`），勾上 → 飞书事件 `drive.file.bitable_record_changed_v1` → 渠道过滤 + 按表防抖 2 秒 → `table control`（引擎回读确认）→ 后台 `table accept --source table`，结果写回控制行；事件丢了巡检回读兜底。下面每个选中款一行套图（表 `candidate_scene_rows`，候选表补三列）。验收后当场 `directions.start_runs`，卡片写积压变化。新命令 `table control` / `table subscribe`，开关 `table.control_row`。
- **验证：** 新增 `test_candidate_control.py` 13 个、渠道 4 个；引擎 322 SQLite / Postgres；SkillScan / 人设 / 渠道 / bundled 353、技能审查 91。
- **上线：** 17:02（备份业务库与 skill）；首次同步发现飞书对没变的字段更新报 `1254606 DataNotChange`，13 张表补列失败，修好后 13 张表都有控制行、8 个任务 56 行套图；巡检里同步回读控制行要 14 秒（网关等 podctl 最多 30 秒），改成巡检点拉起后台 `table poll`。订阅等用户在开放平台开 `docs:event:subscribe` 等。另一路「套图批次交付包」19:32 以这一路为底合并后上线（见上）。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-22 pod-blanket 方向自动确认：图进聊天框后一路自动出图（已上线）

- **用户要求：** 图扔进聊天框之后的步骤都不需要人确认（选方向、选裂变方向都不要），一路自动到生图；选「全部自动确认（每轮 12 + 8，约 144 张）」「所有人发起的策划都算」。
- **做法：** 开关 `batch.auto_confirm_directions`（代码与样例缺省 false）。`cli._auto_plan` / `cmd_plan` 在 `plan_batch` 后调 `directions.auto_confirm` → `confirm(by="system:auto", pick=["all"], start=True)`，系列一并认可、后台出到出完；worker 补充规划不挂。确认出错只记 `auto_confirm_error`，批次留在待挑选。完成卡片改「已开始出图」（几张图、开始还是排队、积压满了何时接上、怎么停掉），按钮「任务明细」；策划按钮写「…并出图」；`doctor` warn；抢不到 worker 租约的后台 worker 不再发空卡。
- **验证：** 新增 `test_auto_confirm.py` 10 个（4 个变异各自变红）；引擎 306 SQLite / Postgres，和另一路合并后 309；SkillScan / 人设 / 渠道 257、`tests/skills` 85；线上 `doctor` 通过，工作台与任务列表实测「策划 20 个方向并出图」。
- **上线：** 另一路同时改「候选表两列」，在独立副本做、`git merge-file` 合进 `.pod-wip`（0 冲突），对方 00:16 上线一并带上；线上 `podflow.yaml` 备份 `.bak-20261022-autoconfirm` 后开 true。B016 不追认；开关打开时选款积压 1428 / 1500，新图会被积压挡住。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-22 pod-blanket 候选表两列分清、选款完成不误判新图（已上线）

- **起因：** 用户分不清候选表的「选中」和「状态」哪个用来选款。查代码发现「选款完成」先把新图补进表、紧接着读勾选，人没见过的新图会被直接算不要。
- **做法：** `candidates.py`：「状态」改名「选款结果」（程序写的结论，程序不读），两列加飞书字段说明；旧表在 `table sync` 第一步用「更新字段」改名补说明（带回原 `type` / `property`），按表记 meta `candidates.fields`，没改成照旧写「状态」，失败满 8 次停。`accept` 没勾的行只有验收前就在表里、进选款满 10 分钟才算不要，其余记 `fresh` 留到下次，验收卡片写明。
- **验证：** 引擎 299 SQLite / Postgres（新增 3 个），ruff；SkillScan / 人设 / 渠道 257、`tests/skills` 85。线上 10 张候选表改名 0 失败，飞书读回选项 id 与格子值都在；之前 S03 / S07 验收的 298 张不要里没有不到 10 分钟的。
- **上线：** `.pod-wip` 改测，按文件拷入 10 个文件，备份 `/work/backups/pod-blanket-skill-20261023-001608.tgz`，doctor 通过，SOUL 重装。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-21 pod-blanket 飞书卡片与 POD 对话里的编号带名字（已上线）

- **起因：** 用户说飞书里所有对话里的任务和图片只有编号，没有交付表格里加上的名字（任务短名字、「编号_方向短语」），分不清编号属于哪张图。
- **做法：** 新模块 `podflow/labels.py`（`ref` / `task` / `asset` / `assets` 批量 / `direction` / `job(s)` / `reviews`）：任务「S01 名字」、系列 / 批次「S01 名字 · B008」、图片「IMG…_短语」、方向 / 出图任务「V0012 标题」。卡片：待审项标题与图下的字、工作台系列 / 最近批次 / 进度 / 下一步、最近动态、已选款、挑选卡 / 任务明细首行、失败 / 修改 / 出图一轮结束 / 拆解未完成 / 图案 / 画面描述 / 套图 / 等人工卡、巡检结论。POD 对话：`status`、`review list`（批量）/ `decide`、`asset`、`job`、`directions show|history`、`scenes status|show`、拼图 `tiles` 带 `label` / `task`，SOUL / SKILL 加称呼规则。渠道审核与发原图提示读 `label`。回调参数、命令参数、mooya 任务名仍是编号。
- **验证：** 引擎 296 SQLite / Postgres（新增 `test_display_labels.py` 11 个），ruff；渠道 86 + 回归 679，上线后 SkillScan / 人设 / 渠道 257、`tests/skills` 85；线上 doctor 通过，选款卡、工作台、动态、已选款、`review list`（1098 条全带 label，1.25 秒）、`status`、`scenes status` 实数据核对。
- **上线：** 与「小变化两字段」并行，在独立副本做，等它上线后逐文件 `git merge-file` 三方合并（0 冲突），按文件拷入线上并同步回 `.pod-wip`；`pod_cards.py` 一次写入（热重载、飞书重连），SOUL 合并后重装。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-21 pod-blanket 小变化两字段、大变化三字段、绘制技法合并（已上线）

- **起因：** 用户觉得小变化只改一个字段变化太小；技法里的描边、渐变上色、纹理单独改在复杂图案上看不出来。选「小变化随机两个小变化字段；大变化要么一个主题 / 构图 / 技法字段，要么三个小变化字段」「每轮固定 12 + 8」。
- **做法：** 字段字典 0.3：D42–D44 并进 D41「绘制技法」，配技法变化清单 `change_forms`，旧拆解 / 系列 / 方向读取时归并；`plan.py` 改成程序随机挑字段（`field_pools` / `pool_counts` / `_pick_sets` / `plan_slots`，种子「系列:批次」，少改过的字段优先、同批不重复，池不够不凑数），P05 按类型分块，`_fit_task` 要求槽位字段全改；两轴组合取消；P05 重写槽位规则，P02 / P04 / P06 / P07 改口径并写明系列规则里「只改一个字段」以槽位为准。
- **验证：** 引擎 285 SQLite / Postgres，ruff；SkillScan / 人设 280；线上 doctor 通过，只读核对 11 个系列槽位（10 个 12 + 4 + 4，F03 3 + 1 + 1）。
- **上线：** 线上 `plan.*` 换五项、`default_positioning` 改「改两个字段」（备份 `podflow.yaml.bak-20261021-plan`）；B016–B018 是旧规则策划的，照常挑或「换一批」。现有 11 个系列规则都写着「小变化只改一个字段」，第一轮新规则策划要看 `slot_mismatch` 多不多。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-21 pod-blanket 画面描述与画风统一（已上线）

- **起因：** S03「奇幻恐龙卡通」的「金叶湖岸群像」背景画成写实暮色薄雾，和高饱和撞色卡通恐龙打架；「水潭环岸群像」文生图和原图脱钩——J0077 的 P06 看不到图、不知道 E01–E09 是什么，返回「COMPILATION BLOCKED」诊断文字，被原样发去出图。用户要拆解后对参考图做完整反推，换背景要守原画风；选「大变化保留文生图但按描述写」「先暂停，上线后这两个方向重出」。
- **做法：** 新步骤 S02D / P13 画面档案（`stages/profile.py`，表 `seed_descriptions`，`seed describe`）；P04–P07 带 `reference_profile`，统一画风规则（只有配色 / 技法方向整幅换画风），P05 加 `style_fit`，方向带 `style_scope`；P06 附参考图，`_validate_p06` 拦诊断文字与对象编号，blocked / failed 时任务停在 S06；`job redo` 作废已出的图重出。
- **验证：** 引擎 281 SQLite / Postgres（新增 `test_reference_profile.py` 13 个、`test_job_redo.py` 3 个），ruff；线上 `doctor` 通过。
- **上线：** 先 `pause`；部署后 `seed describe --all` 给 11 张参考图补写（17 分钟，每张 71–515 秒、3k–11k 输出 token，全部一次过校验；S03 还补出拆解漏记的第 10 只翼龙）；`job redo J0076 J0077`（18 张撤出选款、候选表删 18 行、B009 重开）；`resume` 后 J0076 / J0077 与 B015 剩下的任务按新规则出。实测新图背景与恐龙同一画法、文生图的动物与原图一致（旧 IMG00541 画的是「COMPILATION BLOCKED」监控墙）。P06 两次首版被校验退回后补了事件 `stage_output_rejected`。
- **注意：** worker 启动时 `recover_stale` 会把别的存活进程（如 describe）正在进行的调用标成 unknown，调用完成后会被改回成功（error 字段留着「进程中断」）。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-21 pod-blanket 待审积压上限调到 1000，再到 1500（已上线）

- **用户要求：** 把选图待审拉到 1000 个为上限；随后要求排队的任务全部出完再审（158 个任务还要出约 1229 张，积压会到约 1400），选了 1500。
- **做法：** 线上 `batch.review_backlog_limit` 200 → 1000 → 1500（备份 `podflow.yaml.bak-20261021-backlog` / `-backlog1500`）；`review.LIST_LIMIT = 2000` 作为 `list_reviews` 缺省（原 200，`review list` 与 xlsx 导出会截断），工作台待审页也用它（原 1000）。
- **验证：** 引擎 264 SQLite / Postgres（新增 `test_review_backlog.py` 2 个）、SkillScan / 人设 171、ruff；线上 `status` 显示积压 180 / 上限 1000，列表与工作台都是 180 条；调到 1500 后为积压 186 / 上限 1500、没有被挡住的任务，`doctor` 通过。
- **注意：** 当时 worker 在出 B009、B010–B015 排了 157 个任务（早上新传的 S04–S09），没等空闲就按文件上线（在跑进程不受影响）。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-21 pod-blanket 参考图接入确认冻结（已上线）

- **用户要求：** 参考图确认接入的过程暂时不需要（上传的图都要用），直接进入第一轮策划，先冷冻掉。
- **做法：** 开关 `intake.review`（缺省 true，线上 false，代码保留）：S01 看不清、模型拿不准都不开「参考图接入」待审，直接图案提取 → 拆解 → 自动策划，疑点记在接入记录 `intake_review`；授权受限照旧停。`analyze()` 开头放行冻结前停住的图（关掉接入待审、改回 `ingested`，S01 不重跑）；`doctor` warn。
- **验证：** 引擎 262 SQLite / Postgres（新增 `test_intake_freeze.py` 7 个）、SkillScan / 人设 / skills 280、`tests/skills` 85、ruff；线上 S03 放行（RV0371 关掉），16 分钟拆完并策划出 B009（F02 v1）27 个方向，卡片已发。
- **部署要点：** 按文件拷入 6 个文件；线上 `podflow.yaml` 备份 `.bak-20261021-intake` 后加 `intake: {review: false}`。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket 素材名加方向短语（已上线）

- **用户要求：** 每张素材图的命名和候选表「素材」列加上方向短语（「方向」列那句）。选格式「IMG00123_三伙伴开怀大笑」，下载名、缩略图附件名、发原图、选款表「素材」列、套图设计图都改。
- **做法：** `stages/plan.asset_label` 从方向标题现算名字；两张表的「素材」列、缩略图上传名、下载链接、`ui.originals`、套图设计图都用它，存储路径不变。中文下载名用 RFC 5987 `filename*` 写进 OSS 签名链接（`oss.content_disposition`，ASCII 兜底是存储里的文件名）。旧行用新命令 `maintenance relabel-assets` 补改（重传缩略图、重签链接）。
- **验证：** 引擎 255 SQLite / Postgres（新增 `test_asset_label.py` 5 个，改 5 处旧断言）、SkillScan / 人设 / skills 280、`tests/skills` 85、ruff；线上 S01 候选 120 行补改完、0 失败（约 65 秒），飞书接口读回名字正确，签名链接 206 且浏览器文件名为「IMG00327_羊驼河马小熊猫.png」。
- **注意：** 飞书内置浏览器是否认 `filename*` 待用户手测；不认时退回 `IMG00327.png`。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket 任务短名字（已上线）

- **用户要求：** 图片上传、创建任务之后，在对图片进行分析的同时给任务做一个短的名字：参考图的简短描述 + 当天的日期。选格式「森林三伙伴水彩 1020」，显示在任务列表 / 任务明细 / 人设汇报、这张图相关的卡片标题、候选表表名。
- **做法：** 新提示词 P12「任务命名」；`seed analyze` 没有名字时另起线程和 S01 同时跑（`stages/seed.py::name_seed`），描述去掉空格标点后 2–10 字、接上登记那天的月日，存新表 `seed_names`；失败只记事件，拆解照常。`task_label` / `subject_label` 统一显示「S01 名字（· F01 / B008）」；候选表建表带名字，已建的表由 `table sync` 调飞书「更新数据表」改名。新命令 `seed name [--set]`，开关 `task_name.enabled`。
- **验证：** 引擎 250 SQLite / Postgres（新增 `test_task_name.py` 13 个，含「P01 等 P12 到了才回」的并行用例）、SkillScan / 人设 / skills 280、`tests/skills` 85、ruff。
- **部署要点：** 另一路的套图修复同时在 `.pod-wip` 里改 `scenes.py`，等它先上线、再在其上补标题，最后按文件拷入（拷完线上与 `.pod-wip` 一致）；S01「动物三伙伴水彩 1008」、S03「奇幻恐龙卡通 1020」已补名，「S01 候选」已改名。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket 套图接 mooya（已上线，接 mooya 生产）

- **用户要求：** 选款完成后一般不直接下载，删掉压缩包；接着出场景图，复用 mooya 的 pattern showcase blanket，直接调 mooya 的代码，那边代码和提示词更新这边自动生效。选「mooya 加机器调用接口、内部建图案管线 skip_dna 毛毯任务」「选款完成后自动出」。
- **做法：** mooya 新增 `/api/service/v1`（服务令牌、fail-closed，见 mooya 仓库 `feature-plans/service-api/`）；引擎新模块 `mooya.py` / `scenes.py`（表 `scene_sets` / `scene_images`，后台 `scenes sync` 交图、查进度、按版本拉原图、每款套图压缩包、选款表补 4 列、按任务发卡片）；`table accept` 去掉压缩包、末尾自动入队；任务列表第六步「套图」与「出套图」（渠道 `pod_scenes`）。
- **验证：** 引擎 233 SQLite / Postgres（新增 15）、渠道 / SkillScan / 人设 255、渠道回归 804、skills 85；沙箱对 mooya dev 复用 job 257 跑通（17 张图、18 个文件的压缩包，没有新建 mooya 任务）。
- **注意：** 用户确认后 mooya 生产已上接口（只叠接口文件，没带 mooya 工作区里未上线的毯子模板改动），线上 `mooya.base_url` 已切 5173；幂等键带库编号（联调时发现只带 SS 号会撞）。当晚审查后补修：等人工 / 已暂停的套图不占名额并发「套图等人工」卡片、入队在写事务里复查防重复、「套图出完」卡片写剩余款数（引擎 236 双后端；只装了 `scenes.py` 与测试、文档，`.pod-wip` 里另一路的 P12 任务命名没带上）。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket 参考图 2D 化（已上线）

- **用户要求：** 把 Pattern DNA 上传参考图后先做的 2D 化（提取参考图里的图案）完整移植过来，放在上传之后马上做，图案提取出来再做方向分析。选「提取完自动接着分析、不加确认」「S01 只补提取，拆解不重做」。
- **做法：** 新步骤 S01P：原图压到 1536，照抄 Pattern DNA 的 `flatten_textile` 提示词图生图（模型同日按用户要求从 gpt-image-2 优先改成 2.5-flare → 2.5-sunburst），图案记在新表 `seed_patterns`；读图统一走 `reference_path` / `seed_bytes`，S02（图案 + 原图）、T01 图生图、S07 小变化质检、缩略图、拼图、选款表都换成图案，接入与授权仍看原图。新命令 `seed extract [--hint]`，卡片并排原图与图案（`im/v1/images`）。P02 输入改写，见 `excel-sync-2026-10-20.md`。
- **验证：** 引擎 214 SQLite / Postgres（新增 8 个）、SkillScan / 人设 171、skills 85 通过；线上 S01 用 gpt-image-2 一次提取成功（约 2 分钟，1856x2304），「图案已提取」卡片已发。
- **注意：** S01 的图案是改模型前 gpt-image-2 提的，没重提；B008 已出完、用的是原图，下一轮裂变起才用图案；提取会让画面放大铺满、留白变少，要人看卡片核对。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket 多维表格选款（已上线）

- **用户要求：** 聊天框里选款不舒服：图放进飞书多维表格（缩略图）、打钩选中、有原图下载链接；在工作台针对任务选款完成验收，回读表格拿选中的款、下载压缩包。选「每个任务一张候选表」「表格和聊天框两边都能选」。
- **做法：** 新模块 `candidates.py`：每张参考图一张「S01 候选」（与选中的款同一个多维表格），进选款的图自动进表，聊天框的决定同步成勾和状态；`table accept`：勾上的选中、没勾的不要，再把这个任务选中的原图打成压缩包上 OSS、发签名链接。任务列表加「候选表 / 选款完成 / 下载压缩包」，渠道新动作 `pod_accept`。
- **验证：** 引擎 206 SQLite / Postgres、渠道 141 + 渠道 / 飞书回归 1059、SkillScan / 人设 171、skills 85 通过；线上 B008 的 120 张待选图进了「S01 候选」。
- **注意：** 「选款完成」会把待选里没勾的都记成不要，定了就不能改。压缩包同日被套图取代（见上一条）。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket 旧候选清出选款（已执行）

- **用户要求：** 把旧规则出的候选图都清掉，选款时直接看最新的图。
- **做法：** 新命令 `podctl maintenance dismiss-selection`：关掉指定批次开着的选款待审（不算人工没选，图和记录留着）；已选方向里因积压 / 预算阻断的任务，条件恢复后 `start_runs` 自动接上（原来只接 ready 的，积压清掉也会一直停着）。
- **验证：** 引擎 198 SQLite / Postgres、SkillScan / 人设 171、skills 85 通过；线上 B006 的 177 张清出，积压 201 → 24，选款只剩 B008 的 24 张；`podctl tick` 拉起 BG0378 接着出 B008 的 14 个任务。
- **注意：** 每轮出图约 100 张，积压上限 200，两轮不选款就会挡住出图。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-20 pod-blanket F01 修订成 v7（已执行）

- **用户要求：** 放开 v6 按旧口径写的小变化限制，现在就修订；选「B007 直接作废」「修订后只策划小变化 + 两轴组合」。
- **做法：** 程序补 `family revise --kinds`、「换一批」带 `--kinds`、P05 槽位现状读拆解完整取值（`CURRENT_CHARS 400`，原来截到 60 字）、`analysis_variation` 把拆解后改成只记录的字段挪进不能变；修订意见 8 条：D09 整组换动物、D14 头身比例、D18 动作、表情幅度、C01 / C07 / P06 口径、C06 整套换色、options 重写、其余不变。
- **验证：** 引擎 195 SQLite / Postgres、SkillScan / 人设 171、skills 85 通过；v7 逐条落实；B007 作废，B008 18 个方向（小 14 · 组合 4），D09 / D14 / D18 / D44 都出了方向，只跳过 D21；挑选卡 9.8KB。
- **经验：** 修订 + 策划约 15 分钟（P04 长）；系列规则要跟上字段字典的口径，否则 P05 照系列规则跳槽位。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-19 pod-blanket 小变化改得一眼看得出（已上线）

- **用户要求：** 小变化太小、几乎看不出：表情动作要几个主体一起变、服饰道具不能只改一个物体、小改动也要有构思、配色整套换成另一组和谐颜色。选「配色合并成整体配色」「B006 已出完的小变化保留、停掉没开始的组合」。
- **做法：** 字典每个小变化字段加 `small_change`（同类元素一起改），D37 改名整体配色，D24 / D36 / D39 改只记录（小变化字段 23 → 20）；P04–P07 按它改写，默认定位去掉「只改一处」。新命令 `directions cancel`（只停没开始的任务，方向记「已停掉」）与 `plan --kinds`。
- **验证：** 引擎 191 SQLite / Postgres、SkillScan / 人设 171、skills 85 通过；线上停掉 B006 的 4 个组合（J0040–J0043），`plan --kinds small,combo` → B007 15 个方向（小 11 · 组合 4），挑选卡 8.0KB。
- **经验：** 4 个组合全照抄了 P05 示例里的「薄荷珊瑚」，已改成抽象说法、组合之间不共用改动；F01 v6 系列规则（只换一个主体、表情平静、固定坐姿）挡住了 D09 / D14 / D18，修订系列待用户决定。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-19 pod-blanket 淘汰图放回选款（已执行）

- **做法：** 新命令 `podctl maintenance restore-rejected`：淘汰的图改回质检通过、标「未经质检」（原判定留在 `previous`），已结束任务当场预检、归档、开选款。
- **验证：** 引擎 186 SQLite / Postgres 通过；线上 B006 的 19 张（J0015–J0018）放回，选款待审 41 张。

### 2026-10-19 pod-blanket 质检暂停（已上线）

- **起因：** B006 前两个小变化方向 12 张终稿淘汰 11 张：质检把图生图对非目标细节的重绘判成「改了别处」，参考图又模糊。
- **用户决定：** 保留质检代码、暂时封存、先不跑，以后有需要再启用。
- **做法：** `qc.enabled`（缺省 true）；关掉时 S07 只做文件检查、标「未经质检」进选款，T01R 不再修质检判定的图，人工修改照常，doctor 警告。线上已关（备份 `podflow.yaml.bak-20261019-qc`）。
- **验证：** 引擎 184 SQLite / Postgres 通过。

### 2026-10-19 pod-blanket 清空旧任务（已执行）

- **用户要求：** 除了正在进行的任务，把老任务都删掉（以前的规则不用了）；选「S01 旧批次 + S02」「全部彻底删掉，含已选款和选款表」。
- **做法：** 新命令 `podctl maintenance purge-old`（`maintenance.purge_old` + `feishu_table.delete_rows` + `store.delete_tree`）：先 dry-run 列清单，再删选款表记录、一个事务按外键顺序删库、按目录删 OSS 与本机文件。
- **验证：** 引擎 182 SQLite / Postgres 通过；线上先备份 `pod_blanket_20261019_203543.dump`，删了 5 个批次 / 14 个方向 / 104 张图（已选 7）/ 待审 65 / 表格 7 行 / OSS 258 个对象 / F01 v1–v5 / S01-A01 / S02，没有失败；只剩 F01 v6 / B006，再跑 dry-run 为 0。
- **注意：** 不可恢复（数据库有备份，OSS 图没有）；调用账本与编号计数器保留。

### 2026-10-19 pod-blanket 挑选卡「全选 / 全不选」（已上线）

- **用户要求：** 方向太多，挑选卡要有全选和全不选。
- **做法：** 勾选框只在飞书本地切换，按钮走回调：引擎 `_resolve` 认 `all`、`pick` 按集合勾 / 取消，视图给 `pick_actions`；渠道新动作 `pod_plan_pick_all` → `directions pick B all [--off]` 后原地刷新，按钮放在表单外的清单上面。
- **验证：** 引擎 179 SQLite / Postgres、渠道 134、渠道 / 飞书回归 749 通过；线上 B006 卡片 14.3KB、按钮在清单上面，勾选未动。
- **部署要点：** 先覆盖引擎再写渠道文件，一次热重载、飞书自动重连。

### 2026-10-19 pod-blanket 取消原创性排除项 + 绘制技法小 / 大变化各一个（已上线）

- **用户要求：** X01 / X02（不复现参考图组合 / 布局）对大变化也不一定适用，大变化可以参考原图；X10 / X11（纯技法不算独立款 / 轴覆盖）也有问题，纯技法有时算小变化、有时算大变化。选「大变化没改的部分不要求也不禁止」「技法字段每轮小 / 大变化各一个」。
- **做法：** 字典小变化层级加 11 绘制技法 + `fields.kinds`，`plan_slots` 按 kinds 出槽位（技法同号分两组，按 `(kind, slot_id)` 排序），方向记录带类型；P04–P07 / G00 / `default_positioning` / 授权说明去掉原创性限制，P06 大变化没改的部分可沿用可调整。
- **验证：** 引擎 178 个 SQLite / Postgres 全过，SkillScan / 人设 171、skills 85 通过；线上 F01 v6（排除项只剩既有角色 / 非印花 / 文字等）→ B006 29 / 32 个方向（小 15、大 10、组合 4，技法 4 个字段小 / 大各一），P05 10 组 5.5 分钟，挑选卡 13.9KB。
- **部署要点：** 不动渠道；`podflow.yaml` 备份 `.bak-20261019`；SOUL 重装；B005 作废。
- **待办：** 用户挑 B006；Excel 按 `excel-sync-2026-10-19.md` 同步。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-16 pod-blanket 裂变改小变化 / 大变化 + 字段梳理（已上线）

- **用户要求：** 小变化只改一个字段（8 个维度）、构图不动；大变化改主题叙事 / 整体构图 / 绘制技法的一个字段或两轴组合；每轮往每个可行字段各出一个；梳理重复含糊的字段。选「每个可变字段 1 个 + 4 个组合」「小变化只做图生图 6 张」。
- **做法：** 字段字典 0.2（停用 17 个，`fields.py`）；P02 可变判断 + 门控；P04 变化域只放可变字段；P05 按槽位分组并发；小变化 i2i 6 张、P07 对照原图；挑选卡分段；`--reanalyze`。上线实测后补：LLM 流式（`llm.stream`）、流内报错重试、重拆接着做、「不照搬原构图」只约束大变化（`default_positioning` 同步）。
- **验证：** 引擎 175 个 SQLite / Postgres 全过，渠道 128、skills 85、SkillScan 245 通过；线上 S01 重拆 → F01 v5，B005 16 个方向（小 12 / 大 4），P02 290 秒、P05 9 组 4.8 分钟、挑选卡 8.9KB。
- **部署要点：** 先拷渠道文件（一次热重载）再覆盖引擎；`podflow.yaml` 备份 `.bak-20261016`；SOUL 重装。
- **待办：** 用户挑 B005；下一轮看技法 / 组合槽位是否补齐（B005 时被旧排除项挡掉）；Excel 按 `excel-sync-2026-10-16.md` 同步。详见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-15 pod-blanket 换模型：GPT 6.1 Sol + Nano Banana 2.1 + 图生图加 Edit（已上线）

- **用户要求：** GPT 5.6 全换成 GPT 6.1 Sol；生图去掉 Nano Banana Pro、换 Nano Banana 2.1，全部加上 Edit 模型（图生图除原模型外再用 Edit 模型跑一遍）。先说明了代理里 `X` + 参考图与 `X-edit` 同路由（Edit 那张只是多一次采样），用户仍选「照做」；旧 pro 图修复选「改用 2.1」。
- **做法：** P 阶段 `gpt-6.1-sol-high`；`fanout` 换 `gemini-nano-banana-2.1`；`image_generation.i2i_edit`（每个模型多一个 `model-edit#k@i2i`，共 9 路，`gen_concurrency: 9`）；`replaced_models` + `imagegen.current_model`（T01 与修复换接替模型）；`sync_slots` 改按配置对齐（原来按已有槽位推导，换模型不生效）；P06 只看原模型名。
- **验证：** 引擎 161 个（新增 7 个）SQLite 与 Postgres 各一遍全过；线上 doctor 通过、`doctor --probe` 6.1 一次 7 秒；nano-banana 2.1 文生图与 `-edit` 带参考图实测 4:5 / 2K 都出 1856×2304。
- **部署要点：** 确认没有后台任务后整体覆盖引擎，再改线上 `podflow.yaml`（备份 `.bak-20261015`）；SOUL / 人设 config 重装。
- **待办：** 用户在飞书点 B002「开始出图」看 9 路新模型；S02 拆解（BG0110，gpt-5.6 输出解析失败）要不要用 6.1 重跑由用户定。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-15 pod-blanket 选款表（已上线）

- **用户要求：** 做完的款用飞书表格呈现：看审核通过、要用的图，它的变化方向、原图是哪一张；只放缩略图免得塞满飞书空间；给下载链接。选了「多维表格」「程序自动建」。
- **做法：** 新增 `podflow/feishu_table.py`：程序以应用身份建多维表格（20 个字段 + 画册视图，加最近选款的人为 full_access），选中的款每行上传 512px 缩略图（约 40KB；原图平均 4.85MB），参考图缩略图复用，原图用 OSS V1 签名链接（下载成原文件名，90 天、剩 15 天续签）；改判删行、逐行失败上限 8、建表失败一小时退避；`review.decide` 选中后与巡检时 kick。抽出 `feishu.py`（通知共用）与 `lease.py`（存储上传共用）；新表 `table_rows`；CLI `podctl table init|sync|status`；已选款卡片「打开选款表」链接按钮。
- **验证：** 引擎 154 个测试 SQLite 与 Postgres 各一遍全过；渠道 64 个 + 回归 657 个（容器内）全过。用户开 `bitable:app` 后（只需这一项，加协作者接口也认它）线上建表、5 行写入：缩略图 25–42KB，参考图缩略图跨行复用、人员字段、单选新选项都被飞书接受；下载链接 206 + `attachment; filename="IMG00002.jpg"`，有效到 2027-01-13。
- **部署要点：** 先渠道后引擎；`table init` 也持同步租约（防止和巡检拉起的同步同时建出两张表）；线上 `podflow.yaml` 的 `table.enabled: true`；SOUL 重装。
- **待办：** 用户在飞书里看表。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-14 pod-blanket 任务列表与任务明细

- **用户要求：** 看不到详尽的任务明细（状态、完成到哪一步），要清晰罗列当前任务；完成的任务能点，继续生成裂变方向再继续裂变。选了「按参考图」「出完图就算完成」。
- **做法：** 新增 `podflow/tasks.py`（聚合查询按参考图给五步进度 拆解 / 策划 / 挑选 / 出图 x/y / 选款、状态分组、「现在」一行；方向细步骤用从 `next_step` 拆出的 `step_for` 现推）；`ui tasks` + 渠道 `pod_tasks` / `build_pod_tasks_card`（已完成任务带「继续裂变」表单，`pod_plan` 带 `back=tasks` 回到列表）；方向记录卡升级为任务明细卡（动作仍 `pod_history`）；工作台固定按钮「方向记录」→「任务列表」；CLI `podctl tasks [--family]`。
- **验证：** 引擎 147 个测试 SQLite 与 Postgres 各一遍全过；渠道 64 个 + 渠道 / 飞书 / slash / 人设回归 657 个（容器内隔离副本）全过；线上 `ui tasks` 显示 F01「等你开始出图」、出图 11/14、选款待选 44 · 选中 5。
- **部署要点：** 先覆盖引擎（新命令是新增的），再一次拷入渠道文件与测试（一次热重载）；SOUL 重装。改版前的旧批次排队且没开连续运行时算「等你开始出图」。
- **待办：** 用户在飞书看任务列表 / 任务明细，B002 出完后试「继续裂变」。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-14 pod-blanket 选款卡原图在前、写明裂变方向与模型

- **用户要求：** 选款时第一个看到任务的原图，也要看到这个任务的裂变方向、生成的模型和文生图 / 图生图。选了「每张一项」（每页仍 3 张）、「拼图也改」。
- **做法：** `ui.review_view` 选款项图片固定 `[原图 Sxx, 候选]`，说明为方向（编号、标题、组合 / 对照、批次）/ 每处改动 / 不变 / 出图（`model_label` · 文生图或图生图）；`stages/plan.direction_summary`（老变体按字段补）与方向记录共用；渠道 `_review_elements` 多图时「标题 → 图片并排 → 说明」。拼图按参考图分组、第一格原图，标签写方向标题与出图方式，中文字体走 `review.sheet_font`（线上 = mooya `NotoSansSC-Medium.ttf`，读不到退回 ASCII，`doctor` warn），返回 `originals` / `tiles`。
- **验证：** 引擎 132 个测试 SQLite 与 Postgres 各一遍全过；渠道 61 个 + 回归 654 个（容器内）全过；线上选款页与 B003 拼图人工看过（中文标签清楚）。
- **部署要点：** 先渠道后引擎（没有后台任务时）；线上 `podflow.yaml` 加 `review.sheet_font`；SOUL 重装。
- **待办：** 用户在飞书「待审 → 选款」看新布局。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-14 pod-blanket 挑选卡勾选清单 + 方向记录 + 带要求再策划

- **用户要求：** 勾选不要一次发一个请求，勾完统一提交；每张图做过的方向要记下来，再策划时作为不能重复的款放进上下文；能看到已有的方向再接着出。选了「勾选清单」「要可选输入框」。
- **做法：** 挑选卡改根节点 form + `checker`（`pick_V…`，本地切换），提交时 `form_value` → `directions confirm --pick --start`；`direction_records` 汇总系列全部变体与状态，精简后作为 P05 `completed_plan_index`，P05 改写不重复规则、加 `user_direction_request`；程序兜底 `duplicate_direction`；`plan --hint`（≤100 字，存 `plan_json.hint`，带要求的一轮不标耗尽）；`directions history` / `ui history` + 飞书方向记录卡（`pod_history`，输入框 `hint_F…`）。
- **验证：** 引擎 128 个测试 SQLite 与 Postgres 各一遍全过；渠道 60 个 + 渠道 / 飞书 / slash / 人设回归 653 个（容器内隔离副本）全过；线上 `ui history` 列出 F01 的 14 个方向，P05 方向记录约 8.5K 字符。
- **部署要点：** 先拷渠道文件（一次热重载），再覆盖引擎（只有出图 worker 在跑时）；SOUL 重装；`--hint` 一律写成单个参数 `--hint=…`。
- **待办：** 用户在飞书看方向记录卡、下一批验证勾选清单（V7.9+）；按 `excel-sync-2026-10-14.md`「同日追加」同步 Excel。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-14 pod-blanket 裂变方向先审后出图

- **用户要求：** 新图进来就开始策划；审核时简洁明了地说清每个方向往哪改、从哪些方位改；选了方向才往这几个方向出图。选了「只审一次」（系列随方向一起认可）、「每轮 8 个」。
- **做法：** P05 加 `title` / `brief`（方位枚举，from → to）/ `keep`，程序按改动字段推算方位并对齐；S05 只出候选（批次 `proposed`、`plan_selection` 待审，系列待审并入）；新 `podflow/directions.py` 勾选 / 确认（建任务、没选的进排除项、认可待认可系列）/ 换一批，选中的方向在 worker 结束、巡检、恢复时接着出，出完才发卡片；拆解 / 修订后自动策划；飞书挑选卡（`pod_plan_*` 动作，每个方向一个切换按钮）。
- **验证：** 引擎 122 个测试 SQLite 与 Postgres 各一遍全过；渠道 122 个、渠道 / 飞书 / slash 回归 781 个全过（容器内）；线上真实策划 F01 一次（P05 约 6 分钟一次通过，8 个方向简述与字段全部对得上），卡片已发飞书。
- **部署要点：** 渠道文件先在 `/tmp` 改好一次拷入，引擎在 `.pod-wip/` 改测后整体覆盖；线上 `podflow.yaml` 加 `batch.candidates` / `auto_plan_after_analyze`；SOUL 重装。改版前的批次（B002）不自动接着出。
- **待办：** ~~用户在飞书挑选 B003~~（10-14 已选 8 个方向）；按 `excel-sync-2026-10-14.md` 同步 Excel。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-14 修好 POD 对话无响应

- **现象：** POD 对话里上传图片，卡片只显示「(No response from agent)」。
- **原因：** 上游 #5077：`sandbox.allow_host_bash: true` 时 `LocalSandboxProvider` 不支持按人设隔离 skill，`SandboxMiddleware.before_agent` 拒绝运行带 `skills` 白名单的 Custom Agent；pod-blanket 配了 `skills: [pod-blanket]`，所有消息都失败（不只图片）。
- **做法：** 用户选「去掉白名单」（不给上游打补丁）：人设 config 去掉 `skills`，SOUL 写明只用 pod-blanket、只发图片也直接登记并拆解；新增 `backend/tests/test_fork_personas.py`（开着 host bash 时 `deploy/fork/agents/*` 不能设 `skills`），加回白名单时变红。
- **待办：** 用户在飞书验证 POD 对话。

### 2026-10-13 pod-blanket 质检待审并入选款 + 选款卡片内修改

- **用户要求：** 让人尽量少参与中间环节；选款里加「修改」，改完直接回到选款审；去掉素材授权待审；用卡片输入框写意见。
- **做法：** 质检存疑 / 调用失败记 `qc_pass` + 标记直接进选款，不再开 `asset_qc` / `seed_rights`；选款 `needs_changes` + 意见 → 后台 `worker --job`（新 `fixes.py`，kind `fix`）只改这一张，修改版跳过自动质检回选款，失败原图退回选款；绕过积压上限；worker 结束 / `resume` / `tick` 时接上排队或被打断的修改。渠道 `CardActionContext.form_value` + 根节点 form。
- **验证：** 引擎 109 个测试 SQLite 与 Postgres 各一遍全过；渠道 fork 测试 110 个（容器内）；线上 doctor 通过，网关热重载后飞书自动重连。
- **部署要点：** 引擎在 `.pod-wip/` 副本改测后覆盖回去；渠道文件先在 `/tmp` 改好再一次拷入（只触发一次热重载）；SOUL 已重装。线上没有未处理的旧类型待审，不需要迁移。
- **待办：** 用户在飞书手测修改流程（要 V6.8+）；按 `excel-sync-2026-10-13.md`「同日追加」同步 Excel。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-13 pod-blanket 去掉近重复判定 + 文生图 / 图生图同时跑

- **用户要求：** 检测重复的步骤全部去掉（相近的候选反倒好挑）；像 Pattern DNA 一样文生图、图生图一起跑。选了「除授权受限外都开图生图」「只保留计划去重」。
- **做法：** 删 S08 / P08 / `asset_dup` / `calibrate dedup` / pHash / Q10 / CFG18，`qc_pass` 直接进 S09；`image_generation.modes: [t2i, i2i]`、并发 6，槽位 `model#k` / `model#k@i2i`，S06 按配置与授权补齐或撤掉图生图槽位，P06 一次写 `prompts` + `i2i_prompts`，T01 图生图用原款模型名 + 参考图原图（不用 `-edit` 别名，修复也一样）；选款标出图方式，图生图并排参考图。新增 `podctl maintenance retire-dedup`。
- **验证：** 引擎 102 个测试在 SQLite 与 Postgres 各跑一遍全过；线上 doctor 全过；迁移把 5 张疑似重复当场归档进选款（选款 2 → 7），重跑无改动。
- **部署要点：** 在 `.pod-wip/` 副本里改和测，再整体覆盖回 `skills/public/pod-blanket/`（线上定时器每分钟调 podctl）；人设 SOUL 已重装。
- **待办：** 用户按 `feature-plans/pod-blanket-agent/excel-sync-2026-10-13.md` 同步 Excel；整体测试时看文生图 / 图生图哪种更常被选中、图生图是否太像参考图。详情见 `feature-plans/pod-blanket-agent/progress.md`。

### 2026-10-13 deer-flow 容器完全直连 + pod OSS 切内网

- **用户要求：** 暂不考虑海外工具，让 deer-flow 完全不走代理。范围只限 deer-flow 容器，cursor-agent 仍显式走 7890。
- **做法：** 根因是 `clashrestart` 不合并 mixin，运行中的 `runtime.yaml` 还在排除旧网桥。备份后用 `systemd-run` 后台执行 `_merge_config_restart`，只变了 `exclude-interface` 一行；再把 pod `podflow.yaml` 的 OSS endpoint 改为内网，`upload_rate_kbps: 0`。
- **验证：** 容器 DNS 拿到真实 IP（OSS 内网 `100.118.x`），飞书自动重连，cursor-agent 经代理能列出模型，Tailscale / Mooya 正常；pod doctor 全过，4MB 上传 0.26 秒、下载 0.15 秒。
- **代价：** DuckDuckGo / Jina 直连不通，`web_search` / `web_fetch` 失效（用户已接受）；Tavily / Serper / GitHub 可用。
- **回滚：** 把 `resources/runtime.yaml.bak-20261013` 换回并重启 mihomo；pod 切回公网 endpoint 并限速 300。

### 2026-10-13 pod-blanket 复用 mooya 的 Postgres 与 OSS

- **用户要求：** 图片存阿里云 OSS、数据库用 mooya 的 Postgres。用户选「POD 业务库独立库」+「OSS 为准、本机缓存」。
- **基础设施：** mooya Postgres 实例上建 `pod_blanket` / `pod_blanket_test` 两个库和同名角色；连接串和 OSS 凭证写进 pod `secrets.env`（不打印）；overlay 设 `UV_EXTRAS=postgres` 后重建网关（飞书离线约 2 分钟）。
- **引擎：** `db.PgDB` 适配 SQLite 写法的 SQL（advisory lock 单写者）、`podctl db migrate-from-sqlite`；`oss.py`（httpx V1 签名）+ `store.py` 以 OSS 为准（待上传表、后台单进程限速上传、缺失拉回并校验）、`podctl storage status / sync / prune`、doctor 新增检查。
- **验证：** 引擎 101 个测试在 SQLite 与 Postgres 各跑一遍全过；真实 OSS + Postgres 端到端通过；03:30–03:45 正式迁移，18 张表行数一致，32 个文件（63MB）全部上传，冒烟包括删除本机副本后自动拉回。
- **遗留：** OSS 内网 endpoint 当时因 clash 名单过期不通，先走公网并限速，当天已切内网（见上一条）；备份脚本 `scripts/fork/backup_pod_blanket.sh` 待加 cron。详情见 `feature-plans/pod-blanket-agent/`。

### 2026-10-12 POD 流程清晰化与工作台增强

- **起因（用户）：** 使用流程不清晰；用户选了全部五项改进，定时改用网关定时器。
- **引擎：** `podflow/bg.py` + `bg_tasks`（后台任务登记、步骤、摘要、死进程回收、完成 / 失败卡片）；`ui dashboard` 加 `progress` / `next_steps`；`ui timeline / gallery / originals`；采纳提案自动 `family revise`；`tick --auto`（`schedule` 配置，日报每天一次）；`plan --limit` 限制在 `batch.size` 内，后台裂变 / 出图防重复提交。
- **渠道：** `pod_cards.py` 画「进度 / 下一步」与新固定按钮，新增 7 个动作（裂变、出图、连续运行开关、动态、已选款、发原图）；`ForkFeishuChannel.start / stop` 挂定时器。
- **文档：** 新增 `references/usage.md`；SKILL / SOUL（已重装）/ DEPLOYMENT / 补丁清单同步；数据目录 `podflow.yaml` 加 `schedule`。
- **验证：** 引擎 92 个、渠道 41 个测试通过，ruff 通过；线上视图实数据正确，定时器首跳巡检正常（窗口未开 → stop、不发通知）。
- **详情：** `feature-plans/pod-blanket-agent/`；按钮待用户手测。

### 2026-10-10 pod-blanket P05 / P06 改写（参考 Pattern DNA）

- **前提（用户）：** 对一张底图做幅度更大、种类更多的变形。
- **只借拉大变化的写法：** 从 Mooya 纹样爆改借了「目标写成品、纯文生图不提参考图、分模型写法、同批互不雷同」；它守住原图的保守规则一律不借（明细见 `skills/public/pod-blanket/references/SOURCE.md`）。
- **P05：**
  - 每批约 3/4 组合变形（2–3 个视觉维度）+ 约 1/4 单轴对照；
  - 程序按视觉维度判定类型，执行对照配额（`batch.control_ratio`），丢弃无视觉改动的任务；
  - 覆盖统计拆分组合轴。
- **P06：**
  - gemini / gpt 各一份英文提示词 + 中文摘要；只保留明确列出的保留项；
  - 缺家族或出现参考图措辞就重答；
  - T01 按家族取用，旧任务回落。
- **验证：** 引擎测试 80 个通过。
- **待办：** 用户把新全文同步回 Excel（`feature-plans/pod-blanket-agent/excel-sync-2026-10-10.md`）；整体测试时观察疑似重复率。

### 2026-10-10 飞书 POD 工作台 + LiteLLM 语言模型接入

- **LiteLLM 模型：**
  - 用 `scripts/fork/probe_litellm_models.py` 探测 15 个候选（对话 / 流式 / 两步工具 / 识图），接入 13 个（gpt-5.6 terra / sol 各四档、luna 两档、deepseek-v4 三个）进 `deploy/fork/config.yaml`。
  - `LITELLM_API_KEY` 先进 `.env` 并 recreate，再改 config（缺变量会让整份配置加载失败）。
  - 用 `create_chat_model` 两步工具调用、流式输出、内嵌客户端完整对话做了验证。
- **POD 工作台：**
  - `/pod` 卡片：状态、带缩略图的待审页、按钮直接裁决、巡检、暂停、日报、进出 POD 专属对话。
  - 引擎：`podflow/ui.py` 提供视图 JSON；通知改成带按钮的卡片。
  - 渠道：`fork/pod_bridge.py` 与 `fork/pod_cards.py`。
  - 顺带修复：已绑定账号新建线程时，会话快照的人设记错（指针镜像到 store）。
- **验证：** 引擎测试 71 个、POD 渠道测试 23 个、渠道 / slash 回归 513 个、根目录 skill 测试 85 个，全部通过；卡片过飞书真实接口（上传、发送、patch）。
- **详情：** `feature-plans/pod-blanket-agent/`；按钮点击待用户手测。

### 2026-10-08 pod-blanket 长任务 agent（POD 毛毯设计流水线）M1

- 依据用户方案 xlsx（14 维拆解 / G00+P00–P11 / S00–S11 / Q01–Q18）在 `skills/public/pod-blanket/` 建引擎 `podflow`（SQLite 业务库、调用账本、状态机与单 worker 租约、三路生图、质检去重、审核、飞书通知）+ `pod-blanket` 人设（`deploy/fork/agents/`）。
- 模型：gpt-5.6-sol-high（P 阶段）+ nano-banana pro / gpt-image-2.5 flare / sunburst，经 mooya LiteLLM；用户决定暂不设预算、不计费（`budget.enforce: false`）。
- 验证：59 个 mock 单测 + 6 个安全性质变异全红；容器内 doctor / probe / 飞书通知 / 人设真实对话均通过；M1 用 S01 跑通（系列经人工更正「小象 ≠ 考拉」后批准，B001 3 任务 × 3 路）。
- 实跑修掉两处：2.5 `-edit` 不认 `format`（LiteLLM 包成 500 被误重试）、质检把系统性未验证项判待审。
- M1 结果：最终 3 张自动归档、5 张疑似重复待人工、1 张淘汰；首检 5/9、独立候选 3/4；48 次调用。M3 接线完成（scheduler 生效、通知去重、日报入口）；用户决定先不裁决 / 不跑 M2，搭完后整体测试。
- 详情：`feature-plans/pod-blanket-agent/`。

### 2026-10-08 主目录切换到 main 并重建 dev 容器

- 自建 `local-uv:0.11.1`，先在 worktree 预构建镜像，再停栈 → detach worktree → `git switch main` → `rebuild-dev.sh --reset-venv`；飞书离线约 3 分钟，gateway 启动与飞书长连接正常。
- `.env` 删 `UV_INDEX_URL`（已备份）；`docker/.env` 写 `BIND_HOST` / `DEER_FLOW_DEV_ALLOWED_ORIGINS`；去掉 `extensions_config.json` 的示例占位 interceptor。
- 待办：`/setup` → 迁移 → `/connect` → 手测（见 upstream-sync progress）。

### 2026-10-07 飞书账号绑定与旧数据迁移演练

- `/sessions` 当前指针改走绑定库；删除当前会话会改指向或新开；绑定后第一次查找沿用旧指针。飞书卡片按绑定身份构建，未绑定只回提示，合成命令附身份。
- `scripts/fork/migrate_default_owner.py`：预览默认，`--apply` 先备份再改库、复制线程目录与 `memory.json`（不覆盖）。
- 验证：fork 与渠道相关测试通过；全量单测 21,223 通过 / 3 个 `.xhtml` 环境性失败。副本上演练：旧库升到 `0026`，管理员可见 7 个旧线程和 7 条记忆。线上数据未动。
- 详情：`feature-plans/upstream-sync-2026-09/`。切换、`/setup`、正式迁移待确认。

### 2026-09-29 上游同步重建（sync/upstream-2026-09）

- 以 `upstream/main`（PR #5990）为基线重建 fork，逐项移植：cursor-agent overlay、子代理 thinking 开关、飞书功能包（`app/channels/fork/`）、`/status` 子任务 token、config.yaml 迁移到 `config_version 50`。
- 决策：`flatten_p2p` 废弃（上游原生）；人设改走上游线程钉住机制；不移植 harness 层的子任务 token 增量上报。
- 验证：ruff 全量通过；全量单测 21,202 通过 / 3 失败（均为宿主机缺 `/etc/mime.types` 导致的 `.xhtml` 环境性失败）；`config.yaml` 经 `AppConfig` 加载校验。
- 已推送：origin `main` `60270043` → `3759de00`（force-with-lease）；主目录切换与容器重建待执行。
- 详情：`feature-plans/upstream-sync-2026-09/`。

### 2026-09-29 Cursor Rules 与 Memory Bank 多人模式

- `.cursor/rules` 7 条（Memory Bank / Feature Planning / DEPLOYMENT 联动 / 开发环境 / 上游同步 / 回复风格 / 模块文档）+ `memory-bank-merge` skill。
- memory-bank 纳入 git：OWNERS、个人分片模板、预算脚本、pre-commit 拦截、CI owner 校验 + gitleaks、CODEOWNERS。

### 2026-09-28 宿主机 DNS 修复

- 根因：Tailscale `ts-input` 链丢弃 `100.64.0.0/10` 的非 tailscale0 流量，误伤阿里内网 DNS。
- 修复：`ts-input` 首位放行 eth0 的 `100.100.0.0/16`，`tailscale-aliyun-allow.timer` 每 30 秒补回。

### 2026-06-23 fork 定制整理

- 提交飞书交互卡片与会话级设置、子代理 thinking 开关、容器内 cursor-agent、追踪 `config.yaml`；随后的 `/sessions` 多会话、进度卡片、子任务实时 token 以 WIP 形式留在 `legacy/fork-2026-06`。

### 2026-06-11 ~ 06-16 飞书指挥 Cursor 远程编程

- `/repo` 工作目录卡片、机器人自定义菜单（bot.menu_v6）、单聊扁平化、多项目根目录（`/projects` + `/prod_data`）。
- cursor-agent 定向代理 wrapper（走 clash 可见约 120 个模型）；角色模型固定 Opus 4.8 Thinking。

### 2026-06-09 ~ 06-10 基础配置与首次启动

- 豆包 Seed 2.0 Pro 为默认模型；飞书 + 个人微信渠道；国内镜像构建（自建 `local-uv`、阿里云 PyPI / apt、npmmirror）；inotify 调大修复前端 500。

## 待办 TODO

- 飞书手测（管理员与 `default` 数据迁移已于 2026-10-09 完成；同日飞书应用换到用户新企业并已 `/connect` 绑定，见 `feature-plans/upstream-sync-2026-09/progress.md`）。
- pod-blanket：整体测试 → 开 24 小时连续运行前重新评估预算（见 `feature-plans/pod-blanket-agent/progress.md`）；巡检 / 日报已由网关定时器驱动，不再建定时任务。
- pod-blanket 方案 Excel 同步：`excel-sync-2026-10-10.md`（P05 / P06）与 `excel-sync-2026-10-13.md`（删 S08 / P08 / Q10 / CFG18、P06 双模式新全文），由用户执行；2026-10-21 新增 `excel-sync-2026-10-21.md`（S02D / P13 全文、P04–P07 画风统一改动）。
- pod-blanket：`state.recover_stale` 只看调用 owner 不是自己，会把别的存活进程（describe / analyze / plan 后台任务）正在进行的 LLM 调用标成 unknown（完成后被改回成功，`error` 留着「进程中断」）；可改成只回收 owner 进程已不在的调用。
- 给 `scripts/fork/backup_pod_blanket.sh` 加 cron（03:00 `pod_blanket`、03:10 `PG_DATABASE=deerflow`，脚本注释里有写法）；mooya 自己的 `backup_postgres.sh` 也没有定时任务，需要提醒用户。
- ~~2026-11-12 后删除 SQLite 回滚文件~~：2026-11-02 飞书真人验证通过后已提前删（见「已完成里程碑」同日条目）；剩根盘 `/root/deerflow-sqlite-archive/`（正式快照 gzip，约 45MB）11-12 后删。
- 飞书 POD 工作台手测（菜单加 `POD` / `POD_CHAT`；清单见 `feature-plans/pod-blanket-agent/progress.md`「待手测」）。
- 宿主机 `backend/.venv` 仍是切换前的旧环境：需要时按 DEPLOYMENT.md 的镜像流程重建，目前测试在容器里跑。
- 绑定后的飞书手测清单（同上）。
- 飞书开放平台：回调订阅为长连接方式；事件订阅含 `application.bot.menu_v6`；菜单项 event_key 按 `productContext.md` 配置。
- 微信拿到真实 iLink 凭证后启用。
- inotify 配置持久化到 `/etc/sysctl.d/`。
- mihomo 合并重启后，公网 2026 入口待用户从外网确认一次（本机与 eth0 地址已返回 200；2026-10-23 从 activeContext 外迁）。

## 已知问题 / 风险

- 上游会为 p2p 的每条回复记录话题映射，`store.json` 会随消息量缓慢增长（旧 fork 曾跳过）。
- 启用绑定后，未在 Web 连接飞书的用户无法使用机器人，直到单聊发送 `/connect`。
- 2026-10-14 起自助注册临时打开（给团队成员）：打开期间能访问 2026 端口的人都能注册，账号可经 agent 执行 host bash、读写 `/prod_data`；成员注册完要改回 `false`。POD 通知只发 `podflow.yaml` 的 `notify.feishu.receive_id`（管理员单聊），成员点的后台任务完成卡片不会发给成员本人。
- 群聊话题下 `/sessions` 仍是 chat 级登记。
- pod-blanket 预算控制关闭期间，付费调用只受批次大小、worker 时限、试运行窗口与审核积压上限约束；2026-10-15 到 10-25 每个任务出 9 张（图生图另加 Edit），2026-10-25 起大变化 4 张、小变化 2 张，每轮 40 个方向约 112 张。
- pod-blanket 去掉近重复判定后，没有自动检查拦「照抄参考图」；图生图把参考图原图发给模型（授权未知也做，用户决定），只靠选款时并排看参考图把关。
- pod-blanket 套图依赖 mooya：mooya 宕机、令牌轮换、预设下线时套图停住（`doctor` 报错、全局退避，恢复后自动接上）；套图的模型费用在 mooya 租户 `platform_internal`，不进 pod 的调用账本和预算。
- deer-flow 容器直连后，`web_search`（DuckDuckGo）和 `web_fetch`（Jina）不通；需要海外搜索时换成 Tavily 这类直连能用的工具。
- 大跨度同步后的第一次推送会触发上游全套 CI：agent-guidance（上游 `middlewares/AGENTS.md` 继承链超限）、Skill Review、E2E 书签插件失败与 fork 无关；单测 / Blocking IO 失败已通过把部署配置移到 `deploy/fork/` 解决。
