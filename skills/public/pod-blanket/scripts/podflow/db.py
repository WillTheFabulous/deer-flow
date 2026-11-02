"""业务任务库：seed / analysis / family / variant / job / asset / 调用账本 / 审核 / 事件 / 后台任务 / 素材上传。

两种后端，接口一致（`conn / tx / q / q1 / scalar / init_schema / get_meta / set_meta / close`）：
- `DB`：SQLite（WAL），数据目录下的 podflow.db，测试与未配置时的默认；
- `PgDB`：Postgres（`database.url_name` 指向 secrets.env 里的连接串）。SQL 一律按 SQLite 写法（`?` 占位符），
  由适配层转换；两边不同的写法（JSON 取值）走 `json_text()`。

所有写操作走 `tx()`：SQLite 是 BEGIN IMMEDIATE，Postgres 在事务开头拿同一把 advisory lock。
两者都保证同一时刻只有一个写事务——任务领取、编号、预算预留、sha256 查重这类读改写靠它保持原子。
连接按线程隔离，worker 的并发出图线程各用各的连接。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from .util import dumps, now_iso

SCHEMA_VERSION = 1
# 所有 podflow 进程共用的全局写锁（Postgres advisory lock 的键，值本身无含义）
PG_WRITE_LOCK = 0x706F64666C6F77

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS seeds (
  seed_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  original_path TEXT NOT NULL,
  mime TEXT, width INTEGER, height INTEGER, bytes INTEGER,
  source_ref TEXT, source_note TEXT,
  rights_status TEXT NOT NULL DEFAULT 'unknown',
  rights_confirmed_by TEXT, rights_note TEXT,
  sales_evidence TEXT,
  user_positioning TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  seed_record_json TEXT,
  latest_analysis_id TEXT,
  latest_hypothesis_set_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
  analysis_id TEXT PRIMARY KEY,
  seed_id TEXT NOT NULL REFERENCES seeds(seed_id),
  analysis_version INTEGER NOT NULL,
  model TEXT, prompt_version TEXT,
  result_json TEXT NOT NULL,
  coverage_json TEXT NOT NULL,
  unknowns_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
  hypothesis_set_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
  model TEXT, prompt_version TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS families (
  family_id TEXT NOT NULL,
  family_version INTEGER NOT NULL,
  analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
  seed_id TEXT NOT NULL,
  status TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  model TEXT, prompt_version TEXT,
  approved_by TEXT, approved_at TEXT,
  revision_note TEXT,
  exhaustion_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (family_id, family_version)
);

CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL,
  family_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  size INTEGER NOT NULL,
  plan_json TEXT,
  report_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS variants (
  variant_id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL,
  family_version INTEGER NOT NULL,
  batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  plan_hash TEXT NOT NULL,
  parent_variant_id TEXT,
  generation_depth INTEGER NOT NULL DEFAULT 1,
  axis TEXT,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (family_id, plan_hash)
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  variant_id TEXT NOT NULL UNIQUE REFERENCES variants(variant_id),
  batch_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT, lease_until TEXT,
  render_spec_json TEXT,
  blocked_reason TEXT, resume_condition TEXT,
  last_error TEXT,
  pipeline_version TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_slots (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  slot_id TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  asset_id TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, slot_id)
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  variant_id TEXT NOT NULL,
  slot_id TEXT NOT NULL,
  model TEXT NOT NULL,
  asset_version INTEGER NOT NULL DEFAULT 1,
  parent_asset_id TEXT,
  repair_depth INTEGER NOT NULL DEFAULT 0,
  call_id TEXT,
  path TEXT NOT NULL,
  thumb_path TEXT,
  sha256 TEXT NOT NULL UNIQUE,
  width INTEGER, height INTEGER, bytes INTEGER, format TEXT,
  dhash TEXT, phash TEXT,
  status TEXT NOT NULL,
  qc_json TEXT, dedup_json TEXT, print_json TEXT, archive_json TEXT,
  repair_instructions_json TEXT,
  human_decision TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_calls (
  call_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  stage TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  asset_id TEXT,
  model TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_hash TEXT,
  request_meta_json TEXT,
  status TEXT NOT NULL,
  http_status INTEGER,
  error TEXT,
  owner TEXT,
  day TEXT NOT NULL,
  currency TEXT NOT NULL,
  reserved_cost REAL NOT NULL,
  actual_cost REAL,
  cost_status TEXT NOT NULL,
  usage_json TEXT,
  response_meta_json TEXT,
  created_at TEXT NOT NULL,
  submitted_at TEXT, finished_at TEXT,
  latency_ms INTEGER
);

CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  batch_id TEXT,
  reason TEXT,
  payload_json TEXT,
  status TEXT NOT NULL,
  decision TEXT, decided_by TEXT, decided_at TEXT, note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY,
  batch_id TEXT,
  asset_id TEXT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  scope TEXT NOT NULL,
  event TEXT NOT NULL,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS worker_lease (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  pid INTEGER,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  lease_until TEXT NOT NULL,
  info_json TEXT
);

CREATE TABLE IF NOT EXISTS bg_tasks (
  task_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  subject TEXT,
  argv_json TEXT,
  pid INTEGER,
  status TEXT NOT NULL,
  step TEXT,
  notify INTEGER NOT NULL DEFAULT 0,
  log_path TEXT,
  result_json TEXT,
  started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);

CREATE TABLE IF NOT EXISTS storage_objects (
  path TEXT PRIMARY KEY,
  sha256 TEXT,
  bytes INTEGER,
  content_type TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  owner TEXT, lease_until TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS table_rows (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  record_id TEXT,
  link_expires_at TEXT,
  synced_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_patterns (
  seed_id TEXT PRIMARY KEY REFERENCES seeds(seed_id),
  path TEXT NOT NULL,
  version INTEGER NOT NULL,
  model TEXT,
  prompt_sha TEXT,
  hint TEXT,
  attempts_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_names (
  seed_id TEXT PRIMARY KEY REFERENCES seeds(seed_id),
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  name_date TEXT NOT NULL,
  source TEXT NOT NULL,
  model TEXT,
  call_ids_json TEXT,
  named_by TEXT,
  table_name TEXT,
  table_attempts INTEGER NOT NULL DEFAULT 0,
  table_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_descriptions (
  analysis_id TEXT PRIMARY KEY REFERENCES analyses(analysis_id),
  seed_id TEXT NOT NULL REFERENCES seeds(seed_id),
  result_json TEXT NOT NULL,
  hint TEXT,
  model TEXT, prompt_version TEXT,
  call_ids_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_tables (
  seed_id TEXT PRIMARY KEY REFERENCES seeds(seed_id),
  table_id TEXT NOT NULL,
  url TEXT,
  export_key TEXT,
  export_path TEXT,
  export_url TEXT,
  export_expires_at TEXT,
  export_count INTEGER,
  export_bytes INTEGER,
  accepted_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_rows (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  seed_id TEXT NOT NULL,
  record_id TEXT,
  state TEXT,
  link_expires_at TEXT,
  synced_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_controls (
  seed_id TEXT PRIMARY KEY REFERENCES seeds(seed_id),
  table_id TEXT NOT NULL,
  record_id TEXT,
  dirty INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  triggered_at TEXT,
  triggered_by TEXT,
  last_result_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_scene_rows (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  seed_id TEXT NOT NULL,
  set_id TEXT,
  record_id TEXT,
  thumb_token TEXT,
  thumb_set_id TEXT,
  state_key TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_sets (
  set_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  seed_id TEXT NOT NULL,
  status TEXT NOT NULL,
  preset TEXT NOT NULL,
  mooya_job_id INTEGER,
  mooya_status TEXT,
  step_label TEXT,
  expected INTEGER,
  actual INTEGER,
  requested_by TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  export_key TEXT,
  export_path TEXT,
  export_url TEXT,
  export_expires_at TEXT,
  export_count INTEGER,
  export_bytes INTEGER,
  notified_at TEXT,
  table_state TEXT,
  table_attempts INTEGER NOT NULL DEFAULT 0,
  table_error TEXT,
  created_at TEXT NOT NULL, submitted_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_images (
  set_id TEXT NOT NULL REFERENCES scene_sets(set_id),
  image_id TEXT NOT NULL,
  role TEXT NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT,
  vibe TEXT,
  plan_id TEXT,
  shot_type TEXT,
  rev TEXT,
  path TEXT NOT NULL,
  sha256 TEXT,
  width INTEGER,
  height INTEGER,
  bytes INTEGER,
  format TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (set_id, image_id)
);

CREATE TABLE IF NOT EXISTS scene_files (
  set_id TEXT NOT NULL REFERENCES scene_sets(set_id),
  seq INTEGER NOT NULL,
  image_id TEXT NOT NULL,
  file_name TEXT NOT NULL,
  role TEXT,
  title TEXT,
  PRIMARY KEY (set_id, seq)
);

CREATE TABLE IF NOT EXISTS scene_packages (
  seed_id TEXT NOT NULL REFERENCES seeds(seed_id),
  batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  fingerprint TEXT,
  set_ids_json TEXT,
  item_count INTEGER NOT NULL DEFAULT 0,
  file_count INTEGER NOT NULL DEFAULT 0,
  missing_json TEXT,
  export_name TEXT,
  export_key TEXT,
  export_path TEXT,
  export_url TEXT,
  export_expires_at TEXT,
  export_bytes INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  failed_fingerprint TEXT,
  table_state TEXT,
  table_attempts INTEGER NOT NULL DEFAULT 0,
  table_error TEXT,
  built_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (seed_id, batch_id)
);

CREATE TABLE IF NOT EXISTS seed_shelf (
  seed_id TEXT PRIMARY KEY REFERENCES seeds(seed_id),
  state TEXT NOT NULL,
  mode TEXT,
  archived_at TEXT,
  archived_by TEXT,
  note TEXT,
  restored_at TEXT,
  restored_by TEXT,
  restore_reason TEXT,
  cycles INTEGER NOT NULL DEFAULT 0,
  snapshot_json TEXT,
  folder_state TEXT,
  folder_attempts INTEGER NOT NULL DEFAULT 0,
  folder_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listing_marks (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  record_id TEXT,
  state TEXT NOT NULL,
  listed_on TEXT,
  note TEXT,
  first_listed_at TEXT,
  read_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selection_reasons (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  seed_id TEXT NOT NULL,
  record_id TEXT,
  reasons_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_downloads (
  seed_id TEXT NOT NULL REFERENCES seeds(seed_id),
  batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  fingerprint TEXT NOT NULL,
  export_key TEXT,
  item_count INTEGER,
  marked_by TEXT NOT NULL,
  marked_at TEXT NOT NULL,
  source TEXT,
  PRIMARY KEY (seed_id, batch_id)
);

CREATE TABLE IF NOT EXISTS candidate_slots (
  job_id TEXT NOT NULL,
  slot_id TEXT NOT NULL,
  seed_id TEXT NOT NULL,
  record_id TEXT,
  asset_id TEXT,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, slot_id)
);

CREATE TABLE IF NOT EXISTS candidate_actions (
  seed_id TEXT NOT NULL REFERENCES seeds(seed_id),
  kind TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  table_id TEXT NOT NULL,
  record_id TEXT,
  note TEXT,
  state_key TEXT,
  triggered_at TEXT,
  triggered_by TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (seed_id, kind, batch_id)
);

CREATE TABLE IF NOT EXISTS candidate_orders (
  record_id TEXT PRIMARY KEY,
  seed_id TEXT NOT NULL,
  order_value INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_rounds (
  source_batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id),
  family_id TEXT NOT NULL,
  status TEXT NOT NULL,
  batch_id TEXT,
  parents_json TEXT,
  reason TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selected_downloads (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  seed_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  set_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  marked_by TEXT NOT NULL,
  marked_at TEXT NOT NULL,
  source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selected_rows (
  asset_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  order_value INTEGER,
  checked INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_set_clients (
  set_id TEXT PRIMARY KEY REFERENCES scene_sets(set_id),
  client TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_listings (
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  store_key TEXT NOT NULL,
  seed_id TEXT NOT NULL,
  state TEXT NOT NULL,
  chosen_by TEXT,
  chosen_at TEXT,
  source TEXT,
  code TEXT,
  code_ref TEXT,
  code_at TEXT,
  code_attempts INTEGER NOT NULL DEFAULT 0,
  code_error TEXT,
  confirmed_by TEXT,
  confirmed_at TEXT,
  set_id TEXT,
  shop_id TEXT,
  template_detail_id TEXT,
  title TEXT,
  detail_id TEXT,
  create_submitted_at TEXT,
  boxed_at TEXT,
  publish_submitted_at TEXT,
  listed_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  error_kind TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, store_key)
);

CREATE TABLE IF NOT EXISTS listing_assets (
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  set_id TEXT NOT NULL,
  fingerprint TEXT,
  files_json TEXT,
  package_key TEXT,
  package_url TEXT,
  links_expire_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  prepared_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, set_id)
);

CREATE TABLE IF NOT EXISTS listing_titles (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
  title TEXT NOT NULL,
  source TEXT NOT NULL,
  meta_json TEXT,
  problems_json TEXT,
  model TEXT,
  call_ids_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temu_rows (
  asset_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  state_key TEXT,
  confirm_written INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temu_brands (
  asset_id TEXT NOT NULL,
  store_key TEXT NOT NULL,
  detail_id TEXT NOT NULL,
  brand_id TEXT NOT NULL,
  done_at TEXT,
  saved INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, store_key)
);

CREATE TABLE IF NOT EXISTS candidate_codes (
  asset_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  value TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_scene_codes (
  asset_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  value TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listed_products (
  code TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  store_key TEXT NOT NULL,
  seed_id TEXT NOT NULL,
  title TEXT,
  detail_id TEXT,
  listed_at TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  temu_status TEXT,
  temu_product_id TEXT,
  product_url TEXT,
  sales_total INTEGER,
  sales_7d INTEGER,
  revenue_total REAL,
  data_updated_on TEXT,
  note TEXT,
  record_id TEXT,
  written_hash TEXT,
  human_read_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- 每周选款表（books.py）：一周一本表格（info_json：app_token、知识库节点、带过来的模板表、事件订阅…）
CREATE TABLE IF NOT EXISTS table_books (
  book_id TEXT PRIMARY KEY,
  app_token TEXT NOT NULL,
  week_start TEXT NOT NULL,
  info_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 每天一个文件夹（info_json 是那天「选中的款」的表格信息，口径同 feishu_table.base：table_id、视图、补过的列…）
CREATE TABLE IF NOT EXISTS table_days (
  day TEXT PRIMARY KEY,
  book_id TEXT NOT NULL,
  info_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 任务归到哪一天（day 为空 = 旧表格）；第一次用到时定下来，之后不再变（搬家时由迁移改）
CREATE TABLE IF NOT EXISTS seed_places (
  seed_id TEXT PRIMARY KEY,
  day TEXT,
  placed_at TEXT NOT NULL
);
-- 每周表格里程序建的表在哪本表格（table_registry 按它拼接口路径）
CREATE TABLE IF NOT EXISTS table_places (
  table_id TEXT PRIMARY KEY,
  app_token TEXT NOT NULL,
  kind TEXT NOT NULL,
  day TEXT,
  created_at TEXT NOT NULL
);
-- 已上架的款写进哪一张「已上架的款」（book_id 空串 = 旧表格那一张）；第一次写飞书时定下来
CREATE TABLE IF NOT EXISTS listed_books (
  code TEXT PRIMARY KEY,
  book_id TEXT NOT NULL,
  placed_at TEXT NOT NULL
);

-- 全自动上架与留痕（2026-10-30，feature-plans/pod-listing-autopilot）：下面的表只追加、不改旧表
-- 单款流水线轨迹（trace.py）：每次状态迁移一条，只追加
CREATE TABLE IF NOT EXISTS trace_events (
  trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  store_key TEXT,
  code TEXT,
  set_id TEXT,
  stage TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  actor TEXT,
  source TEXT,
  reason TEXT,
  payload_json TEXT
);
-- 每（款, 店）现在在哪一环、从什么时候起、卡在哪（store_key 空串 = 还没定店铺）
CREATE TABLE IF NOT EXISTS pipeline_status (
  asset_id TEXT NOT NULL,
  store_key TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_since TEXT NOT NULL,
  blocker TEXT,
  blocker_since TEXT,
  detail_json TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, store_key)
);
-- 飞书表里每一格上次读到的值（cells.py：人改了什么就和它比）
CREATE TABLE IF NOT EXISTS table_cells (
  table_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  field TEXT NOT NULL,
  kind TEXT NOT NULL,
  asset_id TEXT,
  code TEXT,
  value_json TEXT,
  modified_by TEXT,
  modified_at TEXT,
  read_at TEXT NOT NULL,
  PRIMARY KEY (table_id, record_id, field)
);
-- 人改格子的日志（只追加）：旧值、新值、谁、什么时候、程序怎么处理的
CREATE TABLE IF NOT EXISTS table_cell_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  table_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  asset_id TEXT,
  code TEXT,
  field TEXT NOT NULL,
  old_json TEXT,
  new_json TEXT,
  editor TEXT,
  edited_at TEXT,
  via TEXT NOT NULL,
  outcome TEXT NOT NULL,
  note TEXT
);
-- 程序删行、清格之前的整行快照
CREATE TABLE IF NOT EXISTS table_row_snapshots (
  snap_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  table_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  asset_id TEXT,
  code TEXT,
  reason TEXT NOT NULL,
  fields_json TEXT NOT NULL
);
-- 候选表里勾了店铺、还在撤销窗口里的行（candidate_stores：到点回读仍勾着才处理）
CREATE TABLE IF NOT EXISTS store_tick_pending (
  table_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  keys_json TEXT NOT NULL,
  ticked_by TEXT,
  first_seen_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  PRIMARY KEY (table_id, record_id)
);
-- 一套套图的附加记录：交 mooya 时用的包名、mooya 白底图质检、停下的时间、自愈次数
CREATE TABLE IF NOT EXISTS scene_set_flags (
  set_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  package_name TEXT,
  baseline_json TEXT,
  parked_at TEXT,
  heal_json TEXT,
  updated_at TEXT NOT NULL
);
-- 人取消了「确认上架」（全自动时 = 暂停这一款，程序不再替他确认）；恢复时删掉，历史在 trace_events
CREATE TABLE IF NOT EXISTS listing_holds (
  asset_id TEXT PRIMARY KEY,
  held_by TEXT,
  held_at TEXT NOT NULL,
  source TEXT,
  note TEXT
);
-- 上架标题的每个版本（只追加）：谁写的、从哪改的、复核结果
CREATE TABLE IF NOT EXISTS listing_title_versions (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL,
  title TEXT NOT NULL,
  source TEXT NOT NULL,
  made_by TEXT,
  via TEXT,
  check_ok INTEGER NOT NULL,
  problems_json TEXT,
  at TEXT NOT NULL
);
-- 每（款, 店）的标题同步到哪了：要生效的版本、目标（采集箱 / Temu）、状态
CREATE TABLE IF NOT EXISTS title_sync (
  asset_id TEXT NOT NULL,
  store_key TEXT NOT NULL,
  version_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  target TEXT,
  modify_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  submitted_at TEXT,
  done_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, store_key)
);
-- 已上架的款的数据时间序列（人填的或接口读的）：每天一条，值有变化另记一条
CREATE TABLE IF NOT EXISTS listed_metrics (
  metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  store_key TEXT,
  observed_on TEXT NOT NULL,
  temu_status TEXT,
  sales_total INTEGER,
  sales_7d INTEGER,
  revenue_total REAL,
  extra_json TEXT,
  source TEXT NOT NULL,
  read_at TEXT NOT NULL
);
-- Temu 官方接口（temu_open.py）查到的商品：货号 × 店 → goodsId 与最近一次读到的状态
CREATE TABLE IF NOT EXISTS temu_goods (
  code TEXT NOT NULL,
  store_key TEXT NOT NULL,
  goods_id TEXT,
  sku_ids_json TEXT,
  status TEXT,
  sub_status TEXT,
  goods_name TEXT,
  raw_json TEXT,
  found_at TEXT,
  checked_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (code, store_key)
);
-- 查到妙手采集箱「已发布」时那一条的原始数据（以后对账用）
CREATE TABLE IF NOT EXISTS temu_publish_records (
  detail_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  store_key TEXT NOT NULL,
  code TEXT,
  raw_json TEXT,
  seen_at TEXT NOT NULL
);
-- Temu 上架统一到 mooya（2026-10-30，mooya feature-plans/temu-listing-unify）：下面两张只加表、不改旧表
-- 「已上架的款」飞书表的行（temu.listed.source: mooya）：四家店统一读 mooya 底账，这里是镜像 + 飞书行号；
-- 1店 / 2店（mooya 交付工作台上的、回填的人工款）没有 asset_id。人填的列以 mooya 为准，人在飞书改了回写 mooya
CREATE TABLE IF NOT EXISTS listed_rows (
  code TEXT PRIMARY KEY,
  client TEXT NOT NULL,
  store_key TEXT NOT NULL,
  mooya_id INTEGER,
  asset_id TEXT,
  seed_id TEXT,
  title TEXT,
  detail_id TEXT,
  listed_at TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  ledger_json TEXT NOT NULL,
  temu_status TEXT,
  temu_product_id TEXT,
  product_url TEXT,
  sales_total INTEGER,
  sales_7d INTEGER,
  revenue_total REAL,
  data_updated_on TEXT,
  note TEXT,
  mooya_updated_at TEXT,
  record_id TEXT,
  written_hash TEXT,
  human_read_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- 交给 mooya 上架的（款, 店）（temu.engine: mooya）：mooya 的上架单号、最近一次读到的状态与整条载荷
CREATE TABLE IF NOT EXISTS mooya_listings (
  asset_id TEXT NOT NULL,
  store_key TEXT NOT NULL,
  client TEXT NOT NULL,
  listing_id INTEGER NOT NULL,
  ref TEXT NOT NULL,
  state TEXT,
  held INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT,
  submitted_at TEXT NOT NULL,
  checked_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, store_key)
);

-- 精作款周表格（2026-10-31，crafted.py）：mooya 上做的款（1店 / 2店）一周一本表格（info_json：app_token、知识库节点、
-- 带过来的「精作款模板」），每天一张表（info_json 口径同 listed_table._meta：table_id、视图、列版本、店铺选项）
CREATE TABLE IF NOT EXISTS crafted_books (
  book_id TEXT PRIMARY KEY,
  app_token TEXT NOT NULL,
  week_start TEXT NOT NULL,
  info_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crafted_days (
  day TEXT PRIMARY KEY,
  book_id TEXT NOT NULL,
  info_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 精作款的一条归到哪一天（上架日期）；第一次写飞书（或迁移）时定下来，之后不变
CREATE TABLE IF NOT EXISTS crafted_places (
  code TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  placed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_store_listings_code ON store_listings(code) WHERE code IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_listed_products_asset_store ON listed_products(asset_id, store_key);
CREATE INDEX IF NOT EXISTS ix_scene_sets_asset ON scene_sets(asset_id);
CREATE INDEX IF NOT EXISTS ix_scene_sets_status ON scene_sets(status);
CREATE INDEX IF NOT EXISTS ix_storage_status ON storage_objects(status);
CREATE INDEX IF NOT EXISTS ix_bg_tasks_status ON bg_tasks(status);
CREATE INDEX IF NOT EXISTS ix_assets_job ON assets(job_id);
CREATE INDEX IF NOT EXISTS ix_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS ix_calls_day ON provider_calls(day);
CREATE INDEX IF NOT EXISTS ix_calls_scope ON provider_calls(scope_id);
CREATE INDEX IF NOT EXISTS ix_calls_status ON provider_calls(status);
CREATE INDEX IF NOT EXISTS ix_reviews_status ON reviews(status, subject_type);
CREATE INDEX IF NOT EXISTS ix_candidate_rows_seed ON candidate_rows(seed_id);
CREATE INDEX IF NOT EXISTS ix_candidate_scene_rows_seed ON candidate_scene_rows(seed_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_open ON reviews(subject_type, subject_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_trace_asset ON trace_events(asset_id, trace_id);
CREATE INDEX IF NOT EXISTS ix_trace_code ON trace_events(code);
CREATE INDEX IF NOT EXISTS ix_trace_ts ON trace_events(ts);
CREATE INDEX IF NOT EXISTS ix_cell_log_asset ON table_cell_log(asset_id, log_id);
CREATE INDEX IF NOT EXISTS ix_cell_log_code ON table_cell_log(code);
CREATE INDEX IF NOT EXISTS ix_title_versions_asset ON listing_title_versions(asset_id, version_id);
CREATE INDEX IF NOT EXISTS ix_listed_metrics_code ON listed_metrics(code, metric_id);
CREATE INDEX IF NOT EXISTS ix_scene_set_flags_asset ON scene_set_flags(asset_id);
CREATE INDEX IF NOT EXISTS ix_listed_rows_asset ON listed_rows(asset_id);
CREATE INDEX IF NOT EXISTS ix_mooya_listings_listing ON mooya_listings(listing_id);
"""

# 编号前缀与位数：与方案里的 S01 / F01 / V001 风格一致
ID_FORMATS: dict[str, tuple[str, int]] = {
    "seed": ("S", 2),
    "family": ("F", 2),
    "batch": ("B", 3),
    "variant": ("V", 4),
    "job": ("J", 4),
    "asset": ("IMG", 5),
    "call": ("C", 6),
    "review": ("RV", 4),
    "feedback": ("FB", 4),
    "bgtask": ("BG", 4),
    "scene": ("SS", 4),
}


SCHEMA_HASH = hashlib.sha256(DDL.encode("utf-8")).hexdigest()[:16]
# 外键依赖顺序（迁移时按这个顺序整表复制）
TABLES = (
    "meta",
    "counters",
    "seeds",
    "analyses",
    "hypotheses",
    "families",
    "batches",
    "variants",
    "jobs",
    "job_slots",
    "assets",
    "provider_calls",
    "reviews",
    "feedback",
    "events",
    "worker_lease",
    "bg_tasks",
    "storage_objects",
    "table_rows",
    "candidate_tables",
    "candidate_rows",
    "candidate_controls",
    "candidate_scene_rows",
    "seed_patterns",
    "seed_names",
    "seed_descriptions",
    "scene_sets",
    "scene_images",
    "scene_files",
    "scene_packages",
    "seed_shelf",
    "listing_marks",
    "selection_reasons",
    "package_downloads",
    "candidate_slots",
    "candidate_actions",
    "candidate_orders",
    "derived_rounds",
    "selected_downloads",
    "selected_rows",
    "scene_set_clients",
    "store_listings",
    "listing_assets",
    "listing_titles",
    "temu_rows",
    "temu_brands",
    "candidate_codes",
    "candidate_scene_codes",
    "listed_products",
    "table_books",
    "table_days",
    "seed_places",
    "table_places",
    "listed_books",
    "trace_events",
    "pipeline_status",
    "table_cells",
    "table_cell_log",
    "table_row_snapshots",
    "store_tick_pending",
    "scene_set_flags",
    "listing_holds",
    "listing_title_versions",
    "title_sync",
    "listed_metrics",
    "temu_goods",
    "temu_publish_records",
    "listed_rows",
    "mooya_listings",
    "crafted_books",
    "crafted_days",
    "crafted_places",
)
# 自增主键的表（整库复制后要把序列挪到最大值之后）
SERIAL_COLUMNS = (
    ("events", "event_id"),
    ("trace_events", "trace_id"),
    ("table_cell_log", "log_id"),
    ("table_row_snapshots", "snap_id"),
    ("listing_title_versions", "version_id"),
    ("listed_metrics", "metric_id"),
)


class DB:
    dialect = "sqlite"

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()

    def describe(self) -> str:
        return f"sqlite:{self.path}"

    @staticmethod
    def json_text(column: str, key: str) -> str:
        """取 JSON 文本列里某个键的值（文本）；需要数字比较时调用方再 CAST。"""
        return f"json_extract({column}, '$.{key}')"

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, timeout=30, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.q1(sql, params)
        return None if row is None else row[0]

    def init_schema(self) -> None:
        conn = self.conn()
        conn.executescript(DDL)
        with self.tx() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            c.execute(
                "INSERT INTO meta(key, value) VALUES('created_at', ?) ON CONFLICT(key) DO NOTHING",
                (now_iso(),),
            )

    # ---- meta ----
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else value

    def set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def del_meta(self, conn: sqlite3.Connection, key: str) -> None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


# ---------------------------------------------------------------- Postgres


@lru_cache(maxsize=2048)
def pg_sql(sql: str) -> str:
    """SQLite 写法 → psycopg：先转义字面量 %，再把 ? 换成 %s（SQL 文本里不出现带 ? 的字符串字面量）。"""
    return sql.replace("%", "%%").replace("?", "%s")


def pg_ddl(ddl: str) -> list[str]:
    text = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    text = text.replace(" REAL", " DOUBLE PRECISION")
    return [s.strip() for s in text.split(";") if s.strip()]


class Row:
    """与 sqlite3.Row 用法一致：按列名或下标取值；dict(row) 得到列名到值的映射。"""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: Any):
        self._index = index
        self._values = tuple(values)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._index)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._index, self._values, strict=False))!r})"


def _row_factory(cursor: Any) -> Any:
    index: dict[str, int] = {}
    for i, col in enumerate(cursor.description or ()):
        index.setdefault(col.name, i)
    return lambda values: Row(index, values)


class PgConn:
    """psycopg 连接的薄封装：接受 SQLite 写法的 SQL，execute 返回游标（fetchone / fetchall）。"""

    dialect = "postgres"

    def __init__(self, raw: Any):
        self.raw = raw

    def execute(self, sql: str, params: tuple | list | dict | None = None) -> Any:
        # psycopg 只在给了参数时才处理占位符与 %% 转义，所以无参也传空元组
        return self.raw.execute(pg_sql(sql), params if params is not None else ())


# 连不上 / 角色连接数满时重试的间隔（秒）：共用实例偶尔被别的进程顶满，等十几秒多半就有空位
CONNECT_RETRY_DELAYS = (1, 2, 4, 8)


class PgDB:
    dialect = "postgres"

    def __init__(self, url: str):
        self.url = url
        self._local = threading.local()

    def describe(self) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(self.url)
        return f"postgres:{parts.hostname}:{parts.port or 5432}{parts.path}"

    @staticmethod
    def json_text(column: str, key: str) -> str:
        return f"({column}::jsonb ->> '{key}')"

    def conn(self) -> PgConn:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.raw.closed:
            import psycopg

            for delay in (*CONNECT_RETRY_DELAYS, None):
                try:
                    raw = psycopg.connect(
                        self.url,
                        autocommit=True,
                        row_factory=_row_factory,
                        connect_timeout=10,
                        application_name="podflow",
                    )
                    break
                except psycopg.OperationalError:
                    if delay is None:
                        raise
                    time.sleep(delay)
            conn = self._local.conn = PgConn(raw)
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.raw.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[PgConn]:
        conn = self.conn()
        with conn.raw.transaction():
            conn.raw.execute("SELECT pg_advisory_xact_lock(%s)", (PG_WRITE_LOCK,))
            yield conn

    def q(self, sql: str, params: tuple | dict = ()) -> list[Row]:
        return self.conn().execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple | dict = ()) -> Row | None:
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.q1(sql, params)
        return None if row is None else row[0]

    def init_schema(self) -> None:
        import psycopg

        # 每次 podctl 都会调用：表结构没变就跳过，免得每条命令都跑一遍建表语句
        try:
            if self.get_meta("schema_hash") == SCHEMA_HASH:
                return
        except psycopg.errors.UndefinedTable:
            pass
        with self.tx() as c:
            for stmt in pg_ddl(DDL):
                c.execute(stmt)
            c.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            c.execute(
                "INSERT INTO meta(key, value) VALUES('created_at', ?) ON CONFLICT(key) DO NOTHING",
                (now_iso(),),
            )
            self.set_meta(c, "schema_hash", SCHEMA_HASH)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else value

    def set_meta(self, conn: PgConn, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def del_meta(self, conn: PgConn, key: str) -> None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


class MigrationError(RuntimeError):
    pass


def table_counts(db: DB | PgDB) -> dict[str, int]:
    return {t: int(db.scalar(f"SELECT COUNT(*) FROM {t}") or 0) for t in TABLES}


def copy_from_sqlite(src: Path, target: PgDB) -> dict[str, Any]:
    """把 SQLite 业务库整库复制进（空的）Postgres 库：保留全部编号，复制后逐表核对行数。"""
    if target.dialect != "postgres":
        raise MigrationError(
            "当前后端不是 Postgres：先在 podflow.yaml 配好 database.url_name"
        )
    if not src.is_file():
        raise MigrationError(f"找不到 SQLite 业务库 {src}")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    copied: dict[str, int] = {}
    try:
        with target.tx() as conn:
            occupied = [
                t
                for t in TABLES
                if t != "meta"
                and conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            ]
            if occupied:
                raise MigrationError(f"目标库已有数据，拒绝覆盖：{occupied}")
            # init_schema 写的几行 meta 换成源库的，只保留表结构指纹
            conn.execute("DELETE FROM meta WHERE key <> 'schema_hash'")
            for table in TABLES:
                src_cols = [r[1] for r in source.execute(f"PRAGMA table_info({table})")]
                dst_cols = {
                    r[0]
                    for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                        (table,),
                    ).fetchall()
                }
                cols = [c for c in src_cols if c in dst_cols]
                if not cols:
                    copied[table] = 0
                    continue
                where = " WHERE key <> 'schema_hash'" if table == "meta" else ""
                rows = [
                    tuple(r)
                    for r in source.execute(
                        f"SELECT {', '.join(cols)} FROM {table}{where}"
                    )
                ]
                if rows:
                    sql = f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})"
                    with conn.raw.cursor() as cur:
                        cur.executemany(pg_sql(sql), rows)
                copied[table] = len(rows)
            for table, column in SERIAL_COLUMNS:
                conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'),"
                    f" COALESCE((SELECT MAX({column}) FROM {table}), 0) + 1, false)"
                )
    finally:
        source.close()
    after = table_counts(target)
    mismatched = {
        t: {"source": n, "target": after[t]}
        for t, n in copied.items()
        if t != "meta" and after[t] != n
    }
    if mismatched:
        raise MigrationError(f"复制后行数不一致：{mismatched}")
    return {"source": str(src), "target": target.describe(), "copied": copied}


def next_id(conn: sqlite3.Connection, kind: str, *, scope: str | None = None) -> str:
    """在调用方事务内分配顺序编号（seed→S01，variant→V0001 …）。"""
    prefix, width = ID_FORMATS[kind]
    name = f"{kind}:{scope}" if scope else kind
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, 0) ON CONFLICT(name) DO NOTHING",
        (name,),
    )
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    value = conn.execute(
        "SELECT value FROM counters WHERE name = ?", (name,)
    ).fetchone()[0]
    return f"{prefix}{value:0{width}d}"


def next_seq(conn: sqlite3.Connection, name: str) -> int:
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, 0) ON CONFLICT(name) DO NOTHING",
        (name,),
    )
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    return conn.execute(
        "SELECT value FROM counters WHERE name = ?", (name,)
    ).fetchone()[0]


def log_event(
    conn: sqlite3.Connection, scope: str, event: str, payload: Any = None
) -> None:
    conn.execute(
        "INSERT INTO events(ts, scope, event, payload_json) VALUES(?, ?, ?, ?)",
        (now_iso(), scope, event, None if payload is None else dumps(payload)),
    )


def open_review(
    conn: sqlite3.Connection,
    subject_type: str,
    subject_id: str,
    reason: str,
    payload: Any = None,
    *,
    batch_id: str | None = None,
) -> str:
    """登记一条待人工处理项；同一对象已有未结项时复用，不重复登记。"""
    existing = conn.execute(
        "SELECT review_id FROM reviews WHERE subject_type = ? AND subject_id = ? AND status = 'open'",
        (subject_type, subject_id),
    ).fetchone()
    if existing:
        return existing[0]
    review_id = next_id(conn, "review")
    conn.execute(
        "INSERT INTO reviews(review_id, subject_type, subject_id, batch_id, reason, payload_json, status, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, 'open', ?)",
        (
            review_id,
            subject_type,
            subject_id,
            batch_id,
            reason,
            None if payload is None else dumps(payload),
            now_iso(),
        ),
    )
    log_event(
        conn,
        subject_id,
        "review_opened",
        {"review_id": review_id, "type": subject_type, "reason": reason},
    )
    return review_id
