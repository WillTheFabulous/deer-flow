#!/usr/bin/env bash
#
# Fork：备份 Mooya Postgres 实例上的 fork 业务库：pod-blanket 的 pod_blanket 库（默认），
# 以及 DeerFlow 主库 deerflow（PG_DATABASE=deerflow）。
#
# 在 Postgres 容器里经本地 socket 跑 pg_dump（容器内 local 连接是 trust，不需要密码），
# 产出 custom 格式的 .dump，可用 pg_restore 恢复；按保留天数清理同一个库的旧文件。
# pod-blanket 素材在 OSS（Mooya 主桶 pod-blanket/ 前缀）且本机有缓存，这里只备份数据库。
#
# 用法：bash scripts/fork/backup_pod_blanket.sh
#       PG_DATABASE=deerflow bash scripts/fork/backup_pod_blanket.sh
# 可覆盖：PG_CONTAINER（默认 litellm_multi_provider_db）、PG_DATABASE（默认 pod_blanket）、
#         PG_USER（默认与库名相同：两个库都由同名角色持有）、
#         BACKUP_DIR（默认 /work/backups/postgres）、BACKUP_RETAIN_DAYS（默认 30）
# 建议加 crontab：
#   0 3 * * * bash /work/deerflow/deer-flow/scripts/fork/backup_pod_blanket.sh >> /var/log/pod_blanket_backup.log 2>&1
#   10 3 * * * PG_DATABASE=deerflow bash /work/deerflow/deer-flow/scripts/fork/backup_pod_blanket.sh >> /var/log/deerflow_db_backup.log 2>&1
#
# 恢复（先停流水线 / gateway）：
#   docker exec -i litellm_multi_provider_db pg_restore -U <库名> -d <库名> --clean --if-exists < <备份文件>

set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-litellm_multi_provider_db}"
PG_DATABASE="${PG_DATABASE:-pod_blanket}"
PG_USER="${PG_USER:-$PG_DATABASE}"
BACKUP_DIR="${BACKUP_DIR:-/work/backups/postgres}"
BACKUP_RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/${PG_DATABASE}_$(date +%Y%m%d_%H%M%S).dump"
tmp="$out.partial"

docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DATABASE" --format=custom --no-owner --no-acl > "$tmp"
mv "$tmp" "$out"
echo "[$(date '+%F %T')] 备份完成：$out（$(du -h "$out" | cut -f1)）"

find "$BACKUP_DIR" -name "${PG_DATABASE}_*.dump" -type f -mtime "+${BACKUP_RETAIN_DAYS}" -print -delete \
    | sed 's/^/  已删除过期备份：/'
