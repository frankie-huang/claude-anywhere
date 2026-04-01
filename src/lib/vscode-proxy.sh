#!/bin/bash
#
# src/lib/vscode-proxy.sh - VSCode 本地代理客户端库
#
# 提供调用本地 VSCode 代理服务的函数，通过反向 SSH 隧道
# 打开本地电脑上的 VSCode 窗口
#
# 环境变量:
#   ACTIVATE_VSCODE_ON_CALLBACK - 是否自动激活 VSCode (默认: false)
#   VSCODE_SSH_PROXY_PORT - SSH 隧道代理服务端口 (默认: 空，不启用；如 9527)
#                            适用于 VSCode 通过 SSH Remote 连接到远程服务器的开发模式
#
# 使用方法:
#   source "$LIB_DIR/vscode-proxy.sh"
#   vscode_proxy_open "/path/to/project"
#   vscode_proxy_activate "/path/to/project"
#
# 测试代理服务是否可用 (假设端口 9527):
#   curl --noproxy "*" -s http://localhost:9527/
#   # 预期返回: {"status":"ok"}
#
# 服务端检测端口占用:
#   - lsof -i :9527              # macOS/Linux，查看占用进程
#   - ss -tuln | grep :9527      # Linux，检查监听状态
#   - netstat -an | grep 9527    # 通用，检查端口状态
#

# =============================================================================
# 内部配置
# =============================================================================
_VSCODE_PROXY_HOST="localhost"
_VSCODE_PROXY_TIMEOUT=5

# =============================================================================
# 获取代理端口
# =============================================================================
_vscode_proxy_get_port() {
    get_config "VSCODE_SSH_PROXY_PORT" ""
}

# =============================================================================
# 检查代理服务是否可用
# =============================================================================
# 返回: 0 = 可用, 1 = 不可用
# =============================================================================
vscode_proxy_health() {
    local port
    port=$(_vscode_proxy_get_port)

    local url="http://${_VSCODE_PROXY_HOST}:${port}/"
    local response

    log "Checking VSCode proxy health: ${url}"

    if command -v curl &> /dev/null; then
        response=$(curl -s --noproxy "*" --connect-timeout "$_VSCODE_PROXY_TIMEOUT" --max-time "$_VSCODE_PROXY_TIMEOUT" "$url" 2>/dev/null)
        if echo "$response" | grep -qE '"status"[[:space:]]*:[[:space:]]*"ok"' 2>/dev/null; then
            log "VSCode proxy is available"
            return 0
        fi
    fi

    log "VSCode proxy is not available, response: ${response}"
    return 1
}

# =============================================================================
# 打开 VSCode 项目
# =============================================================================
# 参数: $1 - 项目路径
# 返回: 0 = 成功, 1 = 失败
# =============================================================================
vscode_proxy_open() {
    local project_path="$1"

    if [ -z "$project_path" ]; then
        log "VSCode proxy open failed: project path is empty"
        return 1
    fi

    local port
    port=$(_vscode_proxy_get_port)

    local url="http://${_VSCODE_PROXY_HOST}:${port}/open"
    local response

    if ! command -v curl &> /dev/null; then
        log "VSCode proxy open failed: curl not found"
        return 1
    fi

    # URL 编码路径
    local encoded_path
    encoded_path=$(_HOOK_PATH="$project_path" "$PYTHON3" -c "import os, urllib.parse; print(urllib.parse.quote(os.environ['_HOOK_PATH'], safe=''))" 2>/dev/null)
    if [ -z "$encoded_path" ]; then
        encoded_path=$(echo -n "$project_path" | sed 's/ /%20/g')
    fi

    log "Opening VSCode project: ${project_path}"

    response=$(curl -s --noproxy "*" --connect-timeout "$_VSCODE_PROXY_TIMEOUT" --max-time "$_VSCODE_PROXY_TIMEOUT" "${url}?path=${encoded_path}" 2>/dev/null)

    if echo "$response" | grep -q '"success":true' 2>/dev/null; then
        log "VSCode project opened successfully: ${project_path}"
        return 0
    else
        log "VSCode proxy open failed: ${response}"
        return 1
    fi
}

# =============================================================================
# 规范化路径中的 HOME 目录
# =============================================================================
# 功能：如果路径以真实路径的 HOME 目录开头，转换为软链接形式的 HOME 目录
#       例如：/data/home/user/project -> /home/user/project（假设 $HOME=/home/user 是软链）
#
# 参数: $1 - 原始路径
# 输出: 规范化后的路径
# =============================================================================
_vscode_normalize_home_path() {
    local path="$1"

    # 获取 HOME 的真实路径
    local real_home
    real_home=$(readlink -f "$HOME" 2>/dev/null || realpath "$HOME" 2>/dev/null || echo "$HOME")

    # 如果 HOME 本身就是真实路径（没有软链接），直接返回原路径
    if [ "$HOME" = "$real_home" ]; then
        echo "$path"
        return
    fi

    # 检查路径是否以真实 HOME 开头，如果是则替换为软链接 HOME
    if [[ "$path" == "$real_home"/* ]]; then
        echo "${HOME}${path#$real_home}"
        return
    fi

    # 检查路径是否以软链接 HOME 开头，保持不变
    if [[ "$path" == "$HOME"/* ]]; then
        echo "$path"
        return
    fi

    # 其他情况，返回原路径
    echo "$path"
}

# =============================================================================
# 激活 VSCode 窗口
# =============================================================================
# 功能：根据配置自动选择激活方式
#       - 如果配置了 VSCODE_SSH_PROXY_PORT，使用本地代理
#       - 否则使用 code . 命令
#
# 参数: $1 - 项目路径
# 返回: 0 = 成功, 1 = 失败
# =============================================================================
vscode_proxy_activate() {
    local project_path="$1"

    # 规范化路径：确保使用软链接形式的 HOME 目录
    project_path=$(_vscode_normalize_home_path "$project_path")

    # 检查是否启用自动激活
    local activate_on_callback
    activate_on_callback=$(get_config "ACTIVATE_VSCODE_ON_CALLBACK" "false")

    if [ "$activate_on_callback" != "true" ]; then
        return 0
    fi

    # 检查是否配置了代理端口
    local proxy_port
    proxy_port=$(_vscode_proxy_get_port)

    if [ -n "$proxy_port" ]; then
        # 使用本地代理激活
        log "Activating VSCode via local proxy (port: ${proxy_port})"
        if vscode_proxy_health; then
            vscode_proxy_open "$project_path"
            return $?
        else
            log "VSCode proxy not available, skipping activation"
            return 1
        fi
    elif command -v code &> /dev/null; then
        # 使用 code . 命令激活
        log "Activating VSCode via local 'code .' command"
        (cd "$project_path" && code . &>/dev/null &)
        return 0
    else
        log "VSCode activation skipped: code command not found"
        return 1
    fi
}
