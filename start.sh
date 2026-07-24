#!/bin/bash
# ============================================
#   基金管理工具 v2.0 - Linux 启动脚本
# ============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/venv"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
LOG_FILE="$SCRIPT_DIR/backend.log"

# 颜色输出
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_err()   { echo -e "${RED}[ERR]${NC}   $*"; }

echo "========================================"
echo "  基金管理工具 v2.0 - 启动脚本 (Linux)"
echo "========================================"
echo

# 1. 检查 Python
log_info "检查 Python 环境..."
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    log_err "未找到 $PYTHON，请先安装 Python 3.10+"
    log_info "Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    log_info "CentOS/RHEL:   sudo yum install python3 python3-pip"
    exit 1
fi
PY_VER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
log_ok "Python $PY_VER"

# 2. 创建/激活虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    log_info "创建虚拟环境 $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR" || { log_err "创建虚拟环境失败"; exit 1; }
    log_ok "虚拟环境已创建"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log_ok "虚拟环境已激活 ($(which python))"

# 3. 安装/更新依赖
log_info "检查依赖（首次运行会安装，可能需要 1-2 分钟）..."
pip install --upgrade pip --quiet
pip install -r "$SCRIPT_DIR/web-app/backend/requirements.txt" --quiet \
    || { log_err "依赖安装失败，请检查网络或使用国内镜像："; \
         log_info "  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt"; \
         exit 1; }
log_ok "依赖已就绪"

# 4. 检查端口占用
if ss -tln 2>/dev/null | grep -q ":$PORT\b"; then
    log_warn "端口 $PORT 已被占用，尝试终止旧进程..."
    PID=$(ss -tlnp 2>/dev/null | grep ":$PORT\b" | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$PID" ]; then
        kill -9 "$PID" 2>/dev/null && log_ok "已终止 PID $PID"
        sleep 1
    fi
fi

# 5. 启动后端
cd "$SCRIPT_DIR/web-app/backend"
log_info "启动后端服务（端口 $PORT，监听 $HOST）..."
nohup "$VENV_DIR/bin/python" main.py > "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$SCRIPT_DIR/.backend.pid"
sleep 2

# 6. 验证启动
if kill -0 "$PID" 2>/dev/null; then
    log_ok "后端已启动，PID=$PID"
else
    log_err "后端启动失败，请查看 $LOG_FILE"
    tail -20 "$LOG_FILE"
    exit 1
fi

# 7. 健康检查
sleep 1
if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    log_ok "健康检查通过"
else
    log_warn "健康检查未响应（可能首次请求较慢，浏览器仍可访问）"
fi

echo
echo "========================================"
echo -e "  ${GREEN}服务已启动${NC}"
echo "  电脑浏览器:  http://localhost:$PORT/"
echo "  手机浏览器:  http://$(hostname -I | awk '{print $1}'):$PORT/"
echo "  日志文件:    $LOG_FILE"
echo "  停止服务:    $SCRIPT_DIR/stop.sh"
echo "========================================"
echo
