#!/usr/bin/env bash
# 启动 mysql_mcp_server（SSE 模式，后台运行）
# 可通过环境变量覆盖：MCP_SSE_PORT、MCP_SSE_HOST、ADMIN_TOKEN、MYSQL_MCP_CONFIG_DIR
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$APP_DIR/mysql_mcp_server"
PID_FILE="$APP_DIR/mysql_mcp_server.pid"
LOG_DIR="$APP_DIR/logs"
CONFIG_DIR="${MYSQL_MCP_CONFIG_DIR:-$APP_DIR/config}"

[ -x "$BIN" ] || { echo "错误：找不到可执行文件 $BIN"; exit 1; }

# 已在运行则跳过
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "已在运行 (PID $(cat "$PID_FILE"))，如需重启请先执行 ./stop.sh"
    exit 0
fi

mkdir -p "$LOG_DIR" "$CONFIG_DIR"

export MCP_TRANSPORT="${MCP_TRANSPORT:-sse}"
export MCP_SSE_HOST="${MCP_SSE_HOST:-0.0.0.0}"
export MCP_SSE_PORT="${MCP_SSE_PORT:-8000}"
export MYSQL_MCP_CONFIG_DIR="$CONFIG_DIR"
# 管理页面局域网访问令牌（不设置则管理页面仅限本机访问）
# export ADMIN_TOKEN="change-me"

nohup "$BIN" >> "$LOG_DIR/server.log" 2>&1 &
echo $! > "$PID_FILE"

sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "错误：进程启动失败，请查看 $LOG_DIR/server.log"
    rm -f "$PID_FILE"
    exit 1
fi

echo "已启动 PID $(cat "$PID_FILE")"
echo "  管理页面 : http://<本机IP>:$MCP_SSE_PORT/admin/"
echo "  MCP SSE  : http://<本机IP>:$MCP_SSE_PORT/sse?alias=<别名>"
echo "  日志     : $LOG_DIR/server.log"
