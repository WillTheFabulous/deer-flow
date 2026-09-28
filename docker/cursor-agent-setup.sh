#!/usr/bin/env sh
#
# DeerFlow fork — 在 gateway dev 容器里安装 cursor-agent 定向代理 wrapper。
# 由 docker-compose.cursor-agent.yaml 在上游 dev-entrypoint.sh 之前调用；失败不阻断 gateway 启动。
#
# /root/.cursor 由宿主 ~/.cursor-cli 挂载。这里生成 wrapper 而不是软链：cursor-agent 要走宿主代理
# 才能看到完整订阅模型列表（直连只有少数几个），而定向代理只影响 cursor-agent，
# 豆包（Ark 国内直连）与飞书 WebSocket 仍保持直连。
#
# CURSOR_AGENT_PROXY 覆盖代理地址（默认宿主 7890）；在 .env 里显式写成空值 `CURSOR_AGENT_PROXY=` 则直连。

set -eu

REAL_BIN=/root/.cursor/bin/cursor-agent
WRAPPER=/usr/local/bin/cursor-agent

if [ ! -x "$REAL_BIN" ]; then
    echo "[cursor-agent] $REAL_BIN not found, skipping (install Cursor CLI into host ~/.cursor-cli first)"
    exit 0
fi

# 必须先删除旧文件：若 WRAPPER 是指向真实二进制的软链，`cat >` 会写穿并覆盖真实启动器，
# 造成 cursor-agent 自我 exec 死循环。
rm -f "$WRAPPER" /usr/local/bin/agent
cat > "$WRAPPER" <<'EOF'
#!/bin/sh
proxy="${CURSOR_AGENT_PROXY-http://host.docker.internal:7890}"
if [ -n "$proxy" ]; then
    export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
    export NO_PROXY="localhost,127.0.0.1"
fi
exec /root/.cursor/bin/cursor-agent "$@"
EOF
chmod +x "$WRAPPER"
ln -sf "$WRAPPER" /usr/local/bin/agent
echo "[cursor-agent] wrapper installed at $WRAPPER"
