#!/bin/bash
# 基金管理工具 v2.0 - 停止脚本
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.backend.pid"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${YELLOW}[INFO]${NC} 停止后端 PID=$PID ..."
        kill "$PID" 2>/dev/null
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            echo -e "${YELLOW}[WARN]${NC} 进程未退出，强制终止"
            kill -9 "$PID" 2>/dev/null
        fi
        echo -e "${GREEN}[OK]${NC}   服务已停止"
    else
        echo -e "${YELLOW}[WARN]${NC} PID $PID 不存在（可能已退出）"
    fi
    rm -f "$PID_FILE"
else
    # 兜底：通过端口查找
    PORT="${PORT:-8000}"
    if command -v ss >/dev/null 2>&1; then
        PID=$(ss -tlnp 2>/dev/null | grep ":$PORT\b" | grep -oP 'pid=\K[0-9]+' | head -1)
        if [ -n "$PID" ]; then
            echo -e "${YELLOW}[INFO]${NC} 通过端口 $PORT 找到 PID=$PID，停止中..."
            kill -9 "$PID" 2>/dev/null
            echo -e "${GREEN}[OK]${NC}   服务已停止"
            exit 0
        fi
    fi
    echo -e "${YELLOW}[WARN]${NC} 未发现运行中的服务（PID 文件不存在）"
fi
