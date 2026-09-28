#!/bin/bash
# 停止 AutomotiveGUI 面板服务
# 用法: ./stop.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.dashboard.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "✗ 面板未运行"
    exit 1
fi

PID=$(cat "$PID_FILE")

if ! kill -0 "$PID" 2>/dev/null; then
    echo "✓ 面板已不在运行 (清理残留 PID 文件)"
    rm -f "$PID_FILE"
    exit 0
fi

# Windows: taskkill 杀进程树（含 main.py 子进程）
if command -v taskkill.exe >/dev/null 2>&1; then
    taskkill.exe /F /T /PID "$PID" >/dev/null 2>&1
else
    kill "$PID" 2>/dev/null
    sleep 1
    kill -9 "$PID" 2>/dev/null
fi

sleep 1
if kill -0 "$PID" 2>/dev/null; then
    echo "✗ 停止失败 (PID: $PID)"
    exit 1
fi

echo "✓ 面板已停止 (PID: $PID)"
rm -f "$PID_FILE"
