#!/usr/bin/env bash
# 停止 mysql_mcp_server
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/mysql_mcp_server.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "未找到 PID 文件（可能未在运行）"
    exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for _ in $(seq 1 10); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "进程未响应，强制结束"
        kill -9 "$PID" || true
    fi
    echo "已停止 PID $PID"
else
    echo "进程不存在（可能已停止）"
fi
rm -f "$PID_FILE"
