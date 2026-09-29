#!/usr/bin/env bash
# CI 门禁：非负责人 PR 不得修改 canonical memory-bank。
# 由 .github/workflows/memory-bank-guard.yml 调用。
set -euo pipefail

: "${PR_ACTOR:?需要 PR_ACTOR}"
: "${BASE_SHA:?需要 BASE_SHA}"
: "${HEAD_SHA:?需要 HEAD_SHA}"

changed="$(git diff --name-only "$BASE_SHA" "$HEAD_SHA")"

# 受保护：memory-bank 根目录 .md、archive/、OWNERS（不含 personal/ 子目录）
protected="$(printf '%s\n' "$changed" \
  | grep -E '^memory-bank/[^/]+\.md$|^memory-bank/archive/|^memory-bank/OWNERS$' || true)"

if [ -z "$protected" ]; then
  echo "无 canonical memory-bank 改动，跳过。"
  exit 0
fi

# OWNERS 第 1 列 = GitHub 用户名（忽略注释 / 空行）
owners="$(grep -vE '^[[:space:]]*#' memory-bank/OWNERS | awk 'NF {print $1}')"

if printf '%s\n' "$owners" | grep -qix "$PR_ACTOR"; then
  echo "作者 $PR_ACTOR 是负责人，允许修改 canonical。"
  exit 0
fi

echo "::error::$PR_ACTOR 非 Memory Bank 负责人，禁止修改以下 canonical 文件："
printf '  %s\n' $protected
echo "请把改动写进 memory-bank/personal/<你>.md，由负责人用 memory-bank-merge 归并。"
exit 1
