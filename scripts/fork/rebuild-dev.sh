#!/usr/bin/env bash
#
# Fork：国内网络下重建 dev 栈（阿里云 PyPI 镜像 + uv.lock 临时改写）。
#
# 为什么需要：backend/uv.lock 记录的是 pypi.org，设了镜像 UV_INDEX_URL 时 `uv sync --locked`
# 会判定 lock 过期而失败；宿主机直连 files.pythonhosted.org 又基本下不动。做法是：
#   1. 构建前把 uv.lock 的源地址临时改写为镜像，配合镜像 UV_INDEX_URL 构建镜像；
#   2. 构建完立即还原 uv.lock（运行时挂载的是宿主机上的原始 lock）；
#   3. 用 `up --no-build` 启动，避免 `make docker-start` 的 `--build` 用原始 lock 再构建一次。
# 运行时容器里不能有 UV_INDEX_URL（.env 会经 env_file 注入），依赖直接来自镜像里的 .venv。
#
# 用法：bash scripts/fork/rebuild-dev.sh [--reset-venv]
#   --reset-venv  删除旧的 gateway-venv 卷，让新镜像重新填充依赖（依赖版本有变化时必须加）
#
# 可覆盖的环境变量：UV_IMAGE（默认 local-uv:0.11.1）、APT_MIRROR、NPM_REGISTRY、PYPI_MIRROR、
# DEER_FLOW_CURSOR_AGENT=0（不叠加 cursor-agent）。BIND_HOST / PORT 写在 docker/.env 或先 export。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

LOCK="backend/uv.lock"
PYPI_MIRROR="${PYPI_MIRROR:-https://mirrors.aliyun.com/pypi}"

export DEER_FLOW_ROOT="$ROOT"
export UV_IMAGE="${UV_IMAGE:-local-uv:0.11.1}"
export APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"
export NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

COMPOSE=(docker compose -p deer-flow-dev -f docker-compose-dev.yaml)
if [ "${DEER_FLOW_CURSOR_AGENT:-1}" != "0" ]; then
    COMPOSE+=(-f docker-compose.cursor-agent.yaml)
fi

fail() {
    echo "✗ $*" >&2
    exit 1
}

# 被追踪的部署配置在 deploy/fork/config.yaml；根目录 config.yaml 是指向它的本机软链（上游 gitignore 忽略它）
DEPLOY_CONFIG="deploy/fork/config.yaml"
if [ ! -e config.yaml ] && [ ! -L config.yaml ]; then
    ln -s "$DEPLOY_CONFIG" config.yaml
    echo "==> 已创建软链 config.yaml -> $DEPLOY_CONFIG"
elif [ "$(readlink config.yaml 2>/dev/null)" != "$DEPLOY_CONFIG" ]; then
    fail "根目录 config.yaml 不是指向 $DEPLOY_CONFIG 的软链：确认其内容已合入部署配置后删除它，再重跑本脚本"
fi

if grep -qE '^[[:space:]]*UV_INDEX_URL=' .env 2>/dev/null; then
    fail ".env 里有 UV_INDEX_URL：它会注入容器，导致启动时 uv sync --locked 失败，请先删除该行"
fi
git diff --quiet -- "$LOCK" || fail "$LOCK 有未提交改动，脚本结束时会用 git checkout 还原它，请先处理"
docker image inspect "$UV_IMAGE" >/dev/null 2>&1 || fail "缺少 $UV_IMAGE 镜像，先按 DEPLOYMENT.md「启动与重建」构建"

restore_lock() {
    git checkout -- "$LOCK"
}
trap restore_lock EXIT

echo "==> 临时改写 $LOCK 为镜像源并构建镜像"
sed -i \
    -e "s#https://files.pythonhosted.org/packages/#${PYPI_MIRROR}/packages/#g" \
    -e "s#registry = \"https://pypi.org/simple\"#registry = \"${PYPI_MIRROR}/simple/\"#g" \
    "$LOCK"
(cd docker && UV_INDEX_URL="${PYPI_MIRROR}/simple/" "${COMPOSE[@]}" build gateway frontend)

restore_lock
trap - EXIT
echo "==> 已还原 $LOCK"

if [ "${1:-}" = "--reset-venv" ]; then
    echo "==> 删除旧的 gateway-venv 卷"
    (cd docker && "${COMPOSE[@]}" rm -sf gateway)
    docker volume rm deer-flow-dev_gateway-venv >/dev/null 2>&1 || true
fi

echo "==> 启动 dev 栈（不再构建）"
(cd docker && "${COMPOSE[@]}" up -d --no-build --remove-orphans redis frontend gateway nginx)
echo "==> 完成：用 docker logs -f deer-flow-gateway 与 tail -f logs/gateway.log 观察启动"
