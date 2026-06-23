#!/usr/bin/env sh
#
# DeerFlow gateway dev entrypoint — runs inside the docker-compose-dev gateway
# container. Extracted from docker/docker-compose-dev.yaml's inline `command:`
# (PR #2767, addressing review on Issue #2754).
#
# Responsibilities:
#   1. Resolve `--extra X` flags from UV_EXTRAS (comma- or whitespace-separated,
#      mirroring scripts/detect_uv_extras.py for parity with local `make dev`).
#   2. Validate each extra against [A-Za-z][A-Za-z0-9_-]* so a stray shell
#      metacharacter in `.env` cannot reach `uv sync`.
#   3. `uv sync --all-packages` so workspace member extras (deerflow-harness's
#      postgres extra in particular) are installed — see PR #2584.
#   4. Self-heal: if the first sync fails, recreate .venv and retry once.
#   5. Hand off to uvicorn with reload, replacing this shell so uvicorn becomes
#      PID 1 inside the container.
#
# Anchored at /bin/sh (not bash) since alpine-based base images may not ship
# bash. Uses POSIX-only constructs throughout.

set -e

# `--print-extras` is a dry-run hook: parse + validate UV_EXTRAS, print the
# resulting `--extra X` flags to stdout, and exit. Used by the unit test in
# backend/tests/test_dev_entrypoint.py and useful for ad-hoc debugging.
PRINT_EXTRAS_ONLY=0
if [ "${1:-}" = "--print-extras" ]; then
    PRINT_EXTRAS_ONLY=1
fi

# Mirror the legacy command's behavior: redirect both stdout and stderr to the
# host-mounted log file (../logs/gateway.log → /app/logs/gateway.log). Skip
# the redirect under --print-extras so the test runner can capture stdout.
if [ "$PRINT_EXTRAS_ONLY" = "0" ]; then
    exec >/app/logs/gateway.log 2>&1
fi

# ── Resolve extras ──────────────────────────────────────────────────────────

EXTRAS_FLAGS=""
if [ -n "${UV_EXTRAS:-}" ]; then
    # Normalize comma → space, then split on whitespace via the unquoted `for`.
    for raw in $(printf '%s' "$UV_EXTRAS" | tr ',' ' '); do
        [ -z "$raw" ] && continue
        # Reject anything that does not look like an identifier.
        # Two patterns: leading non-letter, or any non-[A-Za-z0-9_-] character.
        case "$raw" in
            [!A-Za-z]* | *[!A-Za-z0-9_-]*)
                echo "[startup] UV_EXTRAS entry '$raw' is invalid (must match [A-Za-z][A-Za-z0-9_-]*) — aborting" >&2
                exit 1
                ;;
        esac
        EXTRAS_FLAGS="$EXTRAS_FLAGS --extra $raw"
    done
fi

if [ "$PRINT_EXTRAS_ONLY" = "1" ]; then
    # Trim leading space for tidier output, then exit.
    printf '%s\n' "${EXTRAS_FLAGS# }"
    exit 0
fi

if [ -n "$EXTRAS_FLAGS" ]; then
    echo "[startup] uv extras:$EXTRAS_FLAGS"
fi

# ── Cursor CLI ──────────────────────────────────────────────────────────────
# /root/.cursor 由 compose 从宿主机 ~/.cursor-cli 挂载。生成代理 wrapper 而非软链：
# cursor-agent 必须走宿主机 clash 代理才能拿到完整订阅模型列表；
# 定向代理只影响 cursor-agent，不影响豆包（Ark 国内直连）与飞书 WS。
# 代理地址可用 CURSOR_AGENT_PROXY 覆盖（默认宿主机 clash 7890）。
if [ -x /root/.cursor/bin/cursor-agent ]; then
    # 必须先删除旧软链：cat > 会跟随软链写穿到真实二进制，造成自我覆盖
    rm -f /usr/local/bin/cursor-agent /usr/local/bin/agent
    cat > /usr/local/bin/cursor-agent <<'WRAPPER'
#!/bin/sh
export HTTP_PROXY="${CURSOR_AGENT_PROXY:-http://host.docker.internal:7890}"
export HTTPS_PROXY="$HTTP_PROXY"
export NO_PROXY="localhost,127.0.0.1"
exec /root/.cursor/bin/cursor-agent "$@"
WRAPPER
    chmod +x /usr/local/bin/cursor-agent
    ln -sf /usr/local/bin/cursor-agent /usr/local/bin/agent 2>/dev/null || true
fi

# Keep runtime-owned files out of uvicorn's reload watcher. Each excluded path
# must exist before uvicorn starts so watchfiles treats it as an excluded
# directory, not as a plain glob pattern — on Python 3.12, globbing an absolute
# pattern raises NotImplementedError and crashes startup (#3459 / #3454). That
# means `sandbox` must be created here too, not just `.deer-flow`.
: "${DEER_FLOW_HOME:=/app/backend/.deer-flow}"
export DEER_FLOW_HOME
mkdir -p "$DEER_FLOW_HOME" /app/backend/.deer-flow /app/backend/sandbox

# ── Sync dependencies (with self-heal) ──────────────────────────────────────

cd /app/backend

# `--all-packages` propagates extras into workspace members (PR #2584).
# `$EXTRAS_FLAGS` intentionally unquoted so each `--extra X` becomes its own arg.
# shellcheck disable=SC2086 # word-splitting is intentional here
if ! uv sync --all-packages $EXTRAS_FLAGS; then
    echo "[startup] uv sync failed; recreating .venv and retrying once"
    uv venv --allow-existing .venv
    # shellcheck disable=SC2086
    uv sync --all-packages $EXTRAS_FLAGS
fi

# ── Hand off to uvicorn ─────────────────────────────────────────────────────

PYTHONPATH=. exec uv run uvicorn app.gateway.app:app \
    --host 0.0.0.0 --port 8001 \
    --reload \
    --reload-include='*.yaml' \
    --reload-include='.env' \
    --reload-exclude=/app/backend/sandbox \
    --reload-exclude="$DEER_FLOW_HOME" \
    --reload-exclude=/app/backend/.deer-flow
