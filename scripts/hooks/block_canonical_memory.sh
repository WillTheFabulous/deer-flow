#!/usr/bin/env bash
# 本地 pre-commit：非负责人不得提交 canonical memory-bank 改动。
# 可用 `git commit --no-verify` 绕过（硬闸以 CI 为准）。
# 由 .pre-commit-config.yaml 调用，参数为命中正则的暂存文件列表。
set -euo pipefail

email="$(git config user.email || true)"

# OWNERS 第 2 列 = git 邮箱
owners="$(grep -vE '^[[:space:]]*#' memory-bank/OWNERS 2>/dev/null | awk 'NF {print $2}')"

if printf '%s\n' "$owners" | grep -qix "$email"; then
  exit 0  # 负责人放行
fi

if [ "$#" -gt 0 ]; then
  echo "你（$email）不是 Memory Bank 负责人，不能改 canonical："
  printf '   %s\n' "$@"
  echo "   请写到 memory-bank/personal/<你>.md，由负责人归并。"
  echo "   （确需绕过：git commit --no-verify，但 CI 仍会拦截）"
  exit 1
fi
exit 0
