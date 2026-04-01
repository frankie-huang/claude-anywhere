#!/bin/bash
# =============================================================================
# src/start-server.sh - 启动/停止 Claude Code 权限回调服务
#
# 用法:
#   ./src/start-server.sh [start|stop|restart|status]
#   不带参数默认为 start
#
# 功能:
#   启动 HTTP + Socket 双协议服务器，处理飞书按钮回调请求
#
# 环境变量:
#   FEISHU_WEBHOOK_URL          - 飞书 Webhook URL (可选，仅用于日志)
#   CALLBACK_SERVER_URL         - 回调服务外部访问地址 (默认: http://localhost:8080)
#   CALLBACK_SERVER_PORT        - HTTP 服务端口 (默认: 8080)
#   PERMISSION_SOCKET_PATH      - Unix Socket 路径 (默认: /tmp/claude-permission.sock)
#   PERMISSION_REQUEST_TIMEOUT  - 权限请求超时秒数 (需为正整数，默认值见 .env.example)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVER_DIR="${PROJECT_ROOT}/src/server"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
PID_FILE="${RUNTIME_DIR}/server.pid"
LOG_DIR="${PROJECT_ROOT}/log"

# 引入配置模块 (优先级: .env > 环境变量 > 默认值)
source "${SCRIPT_DIR}/lib/core.sh"

# 确保目录存在
mkdir -p "$LOG_DIR/callback"
mkdir -p "$RUNTIME_DIR"

# 检查 Python 3（使用 core.sh 统一检测的 PYTHON3 变量）
if [ -z "$PYTHON3" ]; then
    echo "Error: python3 is required but not found."
    echo "Searched: .env PYTHON_PATH, .venv, VIRTUAL_ENV, CONDA_PREFIX, pyenv, PATH"
    exit 1
fi
echo "Using Python: $PYTHON3"

# 验证 .env 中的 PYTHON_PATH 是否与实际使用的 Python 一致
# core.sh 已完成检测和验证，此处仅对比 .env 值与检测结果是否一致
validate_env_python_path() {
    local env_file="${PROJECT_ROOT}/.env"
    [ ! -f "$env_file" ] && return 0

    local env_python_path
    env_python_path=$(sed -n 's/^PYTHON_PATH=//p' "$env_file" 2>/dev/null | head -1)
    [ -z "$env_python_path" ] && return 0

    # 去除引号
    env_python_path="${env_python_path#\"}" ; env_python_path="${env_python_path%\"}"
    env_python_path="${env_python_path#\'}" ; env_python_path="${env_python_path%\'}"

    # 对比 .env 记录的路径与 core.sh 实际检测到的路径
    if [ "$env_python_path" != "$PYTHON3" ]; then
        echo ""
        echo "Warning: PYTHON_PATH in .env ($env_python_path) differs from detected Python ($PYTHON3)"
        echo "         To fix, manually edit .env or re-run ./setup.sh with configuration parameters"
        echo ""
        return 1
    fi

    return 0
}

# =============================================================================
# 辅助函数
# =============================================================================

# 获取进程 ID
get_pid() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE"
    fi
}

# 检查服务是否运行
is_running() {
    local pid=$(get_pid)
    if [ -z "$pid" ]; then
        return 1
    fi

    # 检查进程是否存在
    if ps -p "$pid" > /dev/null 2>&1; then
        return 0
    else
        # PID 文件存在但进程不存在，清理 PID 文件
        rm -f "$PID_FILE"
        return 1
    fi
}

# 启动服务
start_service() {
    if is_running; then
        local pid=$(get_pid)
        echo "Service is already running (PID: $pid)"
        return 0
    fi

    echo "Starting Claude Code Permission Callback Server..."

    # 验证 .env 中的 PYTHON_PATH（如果存在）
    validate_env_python_path

    # 检查必需的配置
    local webhook_url
    webhook_url=$(get_config "FEISHU_WEBHOOK_URL" "")
    if [ -z "$webhook_url" ]; then
        echo "Warning: FEISHU_WEBHOOK_URL is not set. Notifications will be skipped."
    fi

    # 后台启动服务：stdout 丢弃（由 Python 内部 FileHandler 直接写日志文件），stderr 捕获到错误文件
    local log_file="$LOG_DIR/callback/$(date +%Y-%m-%d).log"
    local error_file="$LOG_DIR/startup_error.log"
    nohup "$PYTHON3" "${SERVER_DIR}/main.py" >/dev/null 2>"$error_file" &
    local pid=$!

    # 保存 PID
    echo $pid > "$PID_FILE"

    # 等待启动完成（每秒检查一次）
    # 服务初始化约需 5 秒，成功后继续运行，失败则进程退出
    local count=0
    local min_stable_time=5  # 进程至少稳定运行 5 秒才算成功
    local startup_timeout=$((min_stable_time + 2))  # 超时保护

    echo -n "Waiting for service to start"
    while [ $count -lt $startup_timeout ]; do
        echo -n "."
        sleep 1
        count=$((count + 1))

        # 如果进程已退出，立即失败
        if ! is_running; then
            echo " failed."
            echo "Failed to start service."
            if [ -s "$error_file" ]; then
                echo "Error:"
                cat "$error_file"
            else
                echo "Check logs: $log_file"
            fi
            rm -f "$PID_FILE"
            return 1
        fi

        # 进程稳定运行超过 min_stable_time 秒且无致命错误，认为启动成功
        if [ $count -ge $min_stable_time ]; then
            if [ -s "$error_file" ] && grep -qE "^Traceback|^OSError:|^Exception:" "$error_file" 2>/dev/null; then
                echo " failed."
                echo "Failed to start service."
                echo "Error:"
                cat "$error_file"
                rm -f "$PID_FILE"
                return 1
            fi
            # 进程稳定运行且无致命错误
            echo " done."
            echo "Service started successfully (PID: $pid)"
            echo "Logs: $log_file"
            rm -f "$error_file"
            return 0
        fi
    done

    # 超时
    echo " timeout."
    echo "Check logs: $log_file"
    return 1
}

# 停止服务
stop_service() {
    if ! is_running; then
        echo "Service is not running."
        rm -f "$PID_FILE"
        return 0
    fi

    local pid=$(get_pid)
    echo "Stopping service (PID: $pid)..."

    # 尝试优雅关闭
    kill "$pid" 2>/dev/null

    # 等待进程结束（最多 5 秒）
    local count=0
    while is_running && [ $count -lt 5 ]; do
        sleep 1
        count=$((count + 1))
    done

    # 如果还在运行，强制终止
    if is_running; then
        echo "Force killing service..."
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi

    # 清理 PID 文件
    rm -f "$PID_FILE"

    # 清理 socket 文件
    local socket_path
    socket_path=$(get_config "PERMISSION_SOCKET_PATH" "/tmp/claude-permission.sock")
    if [ -S "$socket_path" ]; then
        rm -f "$socket_path"
        echo "Cleaned up socket file: $socket_path"
    fi

    if is_running; then
        echo "Failed to stop service."
        return 1
    else
        echo "Service stopped."
        return 0
    fi
}

# 重启服务
restart_service() {
    echo "Restarting service..."
    stop_service
    sleep 1
    start_service
}

# 显示服务状态
show_status() {
    if is_running; then
        local pid=$(get_pid)
        echo "Service is running (PID: $pid)"

        # 显示端口监听状态
        if command -v netstat &> /dev/null; then
            local port
            port=$(get_config "CALLBACK_SERVER_PORT" "8080")
            if netstat -tln 2>/dev/null | grep -q ":$port "; then
                echo "HTTP server listening on port $port"
            fi
        fi

        # 显示 socket 文件状态
        local socket_path
        socket_path=$(get_config "PERMISSION_SOCKET_PATH" "/tmp/claude-permission.sock")
        if [ -S "$socket_path" ]; then
            echo "Socket server listening on $socket_path"
        fi

        return 0
    else
        echo "Service is not running."
        return 1
    fi
}

# =============================================================================
# 主逻辑
# =============================================================================

case "${1:-start}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

exit $?
