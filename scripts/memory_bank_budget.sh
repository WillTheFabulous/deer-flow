#!/usr/bin/env bash
# Memory Bank 预算自检：按 .cursor/rules/Memory-Bank-Protocol.mdc §3.0 的双水位口径
# 报告各文件字符数与状态，免去让模型通读全文来判断要不要压缩。
#
#   OK      — 未超触发线，直接追加，不要压缩
#   COMPACT — 已超触发线，必须压缩到「目标线」以下（压到刚低于触发线＝没压完）
#
# 口径：字符（wc -m）。wc -c 数的是字节，中文 UTF-8 约 3 字节/字符，会虚高近 2 倍误判超标。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# 文件:触发线:目标线（单位为字符）
budgets=(
  "memory-bank/activeContext.md:32000:18000"
  "memory-bank/systemPatterns.md:220000:185000"
  "memory-bank/progress.md:500000:380000"
  "memory-bank/techContext.md:60000:40000"
  "memory-bank/productContext.md:30000:20000"
)

# 个人分片统一 30K/18K，数量不定故动态收集
for shard in memory-bank/personal/*.md; do
  [ -e "$shard" ] || continue
  case "$(basename "$shard")" in _TEMPLATE.md) continue ;; esac
  budgets+=("$shard:30000:18000")
done

exit_code=0
# 表头用 ASCII：printf 按字节补位，中文表头会把列对不齐
printf '%-38s %10s %10s %10s   %s\n' FILE NOW TRIGGER TARGET STATUS

for entry in "${budgets[@]}"; do
  IFS=: read -r file trigger target <<<"$entry"
  if [ ! -f "$file" ]; then
    printf '%-38s %10s\n' "$file" "缺失"
    continue
  fi

  chars="$(wc -m <"$file" | tr -d ' ')"

  if [ "$chars" -gt "$trigger" ]; then
    status="COMPACT  需压到 $((target / 1000))K 以下（超 $(((chars - trigger) / 1000))K）"
    exit_code=1
  else
    status="OK       余量 $(((trigger - chars) / 1000))K"
  fi

  printf '%-38s %9dK %9dK %9dK   %s\n' \
    "$file" "$((chars / 1000))" "$((trigger / 1000))" "$((target / 1000))" "$status"
done

echo
if [ "$exit_code" -eq 0 ]; then
  echo "全部在预算内 —— 本次写入无需任何压缩动作。"
else
  echo "有文件超触发线：压缩它到目标线以下，压完更新该文件顶部的 <!-- Budget: … --> 水位线注释。"
fi

exit "$exit_code"
