#!/bin/bash
# 启动 AutomotiveGUI 面板服务
# 用法: ./start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/server/autogui/.venv/Scripts/python.exe"
DASHBOARD="$SCRIPT_DIR/dist/dashboard_server.py"
PID_FILE="$SCRIPT_DIR/.dashboard.pid"
LOG_FILE="$SCRIPT_DIR/.dashboard.log"

# 已在运行？
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✗ 面板已在运行 (PID: $(cat "$PID_FILE"))"
    echo "  如需重启请先执行: ./stop.sh"
    exit 1
fi

# 检查 Python 是否存在
if [ ! -f "$PY" ]; then
    echo "✗ 未找到 Python: $PY"
    echo "  请先在 server/autogui 下创建 .venv 并安装依赖"
    exit 1
fi

# 加载 .env（如有）
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# 后台启动
"$PY" "$DASHBOARD" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 1

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✓ 面板已启动 (PID: $(cat "$PID_FILE"))"
    echo "  地址: http://localhost:8777"
    echo "  日志: $LOG_FILE"
    echo "  停止: ./stop.sh"
    # 打开浏览器（Windows）
    cmd.exe /c start "" "http://localhost:8777" 2>/dev/null || true
else
    echo "✗ 启动失败，日志如下:"
    cat "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
