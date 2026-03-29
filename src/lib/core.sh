#!/bin/bash
# =============================================================================
# src/lib/core.sh - 核心函数库
#
# 整合项目路径、环境配置、日志功能的核心库
# 这是所有脚本的基础依赖，提供统一的初始化逻辑
#
# 功能模块:
#   1. 路径解析 - 获取项目根目录和各子目录路径
#   2. 环境配置 - 从 .env 文件读取配置
#   3. 日志记录 - 统一的日志接口
#
# 导出变量:
#   PROJECT_ROOT    - 项目根目录
#   SRC_DIR         - 源代码目录 (src/)
#   LIB_DIR         - Shell 库目录 (src/lib/)
#   CONFIG_DIR      - 配置目录 (src/config/)
#   TEMPLATES_DIR   - 模板目录 (src/templates/)
#   SHARED_DIR      - 共享资源目录 (src/shared/)
#   LOG_DIR         - 日志目录 (log/)
#   RUNTIME_DIR     - 运行时目录 (runtime/)
#   AUTH_TOKEN_FILE - 认证令牌文件 (runtime/auth_token.json)
#
# 函数:
#   get_project_root()    - 获取项目根目录
#   get_config()          - 获取配置值
#   log()                 - 记录日志
#   log_init()            - 初始化日志
#   log_error()           - 记录错误日志
#   log_debug()           - 记录调试日志
#   log_input()           - 记录输入数据
#   log_command()         - 记录命令日志
#
# 使用示例:
#   source "$SCRIPT_DIR/lib/core.sh"
#   log "Starting process"
#   WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "")
# =============================================================================

# =============================================================================
# 第一部分：路径解析
# =============================================================================

# -----------------------------------------------------------------------------
# 获取项目根目录
# -----------------------------------------------------------------------------
# 功能：获取当前项目的根目录路径
# 用法：get_project_root
# 输出：项目根目录的绝对路径
#
# 实现原理：
#   1. 使用 readlink -f 解析软链接，获取真实文件路径
#   2. 从 src/lib/core.sh 向上两级到达项目根目录
# -----------------------------------------------------------------------------
get_project_root() {
    local real_lib_dir
    real_lib_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
    # 从 src/lib/ 向上两级到项目根目录
    printf '%s' "$(cd "${real_lib_dir}/../.." && pwd)"
}

# 初始化并导出路径变量
if [ -z "$PROJECT_ROOT" ]; then
    PROJECT_ROOT="$(get_project_root)"
    export PROJECT_ROOT
fi

# 定义各目录路径
SRC_DIR="$PROJECT_ROOT/src"
LIB_DIR="$SRC_DIR/lib"
CONFIG_DIR="$SRC_DIR/config"
TEMPLATES_DIR="$SRC_DIR/templates"
SHARED_DIR="$SRC_DIR/shared"
LOG_DIR="$PROJECT_ROOT/log"
RUNTIME_DIR="$PROJECT_ROOT/runtime"
AUTH_TOKEN_FILE="$RUNTIME_DIR/auth_token.json"

export SRC_DIR LIB_DIR CONFIG_DIR TEMPLATES_DIR SHARED_DIR LOG_DIR RUNTIME_DIR AUTH_TOKEN_FILE

# =============================================================================
# 第二部分：环境配置
# =============================================================================

# 内部变量
_ENV_FILE_CACHE=""
_ENV_FILE_LOADED="false"

# -----------------------------------------------------------------------------
# 加载 .env 文件到缓存
# -----------------------------------------------------------------------------
_load_env_cache() {
    [ "$_ENV_FILE_LOADED" = "true" ] && return

    local env_file="${PROJECT_ROOT}/.env"
    if [ -f "$env_file" ]; then
        _ENV_FILE_CACHE=$(cat "$env_file")
    fi
    _ENV_FILE_LOADED="true"
}

# -----------------------------------------------------------------------------
# 从缓存中读取指定 key 的值
# -----------------------------------------------------------------------------
_read_from_cache() {
    local key="$1"

    [ -z "$_ENV_FILE_CACHE" ] && return 1

    local line value
    while IFS= read -r line; do
        # 跳过空行和注释
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        # 匹配 key=value 格式
        if [[ "$line" =~ ^${key}= ]]; then
            # 提取等号后的值
            value="${line#*=}"
            # 去除首尾引号 (单引号或双引号)
            value="${value#\"}"
            value="${value%\"}"
            value="${value#\'}"
            value="${value%\'}"
            # 去除首尾空白
            value="${value#"${value%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"
            printf '%s' "$value"
            return 0
        fi
    done <<< "$_ENV_FILE_CACHE"

    return 1
}

# -----------------------------------------------------------------------------
# 获取配置值
# -----------------------------------------------------------------------------
# 功能：按优先级获取配置值
# 优先级：.env 文件 > 环境变量 > 默认值
#
# 用法：get_config KEY [DEFAULT]
# 参数：
#   $1 - 配置项名称 (如 FEISHU_WEBHOOK_URL)
#   $2 - 默认值 (可选，默认为空字符串)
# 输出：配置值
#
# 示例：
#   WEBHOOK=$(get_config "FEISHU_WEBHOOK_URL" "")
#   PORT=$(get_config "CALLBACK_SERVER_PORT" "8080")
# -----------------------------------------------------------------------------
get_config() {
    local key="$1"
    local default="${2:-}"

    # 确保缓存已加载
    _load_env_cache

    # 1. 优先从 .env 文件读取
    local env_value
    if env_value=$(_read_from_cache "$key"); then
        printf '%s' "$env_value"
        return
    fi

    # 2. 其次从环境变量读取
    local shell_env="${!key}"
    if [ -n "$shell_env" ]; then
        printf '%s' "$shell_env"
        return
    fi

    # 3. 使用默认值
    printf '%s' "$default"
}

# -----------------------------------------------------------------------------
# 重新加载 .env 文件
# -----------------------------------------------------------------------------
env_reload() {
    _ENV_FILE_CACHE=""
    _ENV_FILE_LOADED="false"
    _load_env_cache
}

# =============================================================================
# 第三部分：日志功能
# =============================================================================

# 日志文件路径
: "${LOG_FILE:=}"

# 日志配置
_LOG_DATE_FORMAT="%Y-%m-%d"
_LOG_DATETIME_FORMAT="%Y-%m-%d %H:%M:%S"
_LOG_FILE_PATTERN="hook/{date}.log"

# -----------------------------------------------------------------------------
# 加载日志配置
# -----------------------------------------------------------------------------
_load_log_config() {
    local config_file="${SHARED_DIR}/logging.json"

    if [ -f "$config_file" ]; then
        # 尝试使用 jq 读取配置
        if command -v jq &> /dev/null; then
            _LOG_DATE_FORMAT=$(jq -r '.date_format // "%Y-%m-%d"' "$config_file" 2>/dev/null || echo "%Y-%m-%d")
            _LOG_DATETIME_FORMAT=$(jq -r '.datetime_format // "%Y-%m-%d %H:%M:%S"' "$config_file" 2>/dev/null || echo "%Y-%m-%d %H:%M:%S")
            _LOG_FILE_PATTERN=$(jq -r '.file_patterns.hook // "hook/{date}.log"' "$config_file" 2>/dev/null || echo "hook/{date}.log")
        # 尝试使用 python3 读取配置
        elif command -v python3 &> /dev/null; then
            _LOG_DATE_FORMAT=$(python3 -c "import sys, json; print(json.load(open(sys.argv[1])).get('date_format', '%Y-%m-%d'))" "$config_file" 2>/dev/null || echo "%Y-%m-%d")
            _LOG_DATETIME_FORMAT=$(python3 -c "import sys, json; print(json.load(open(sys.argv[1])).get('datetime_format', '%Y-%m-%d %H:%M:%S'))" "$config_file" 2>/dev/null || echo "%Y-%m-%d %H:%M:%S")
            _LOG_FILE_PATTERN=$(python3 -c "import sys, json; print(json.load(open(sys.argv[1])).get('file_patterns', {}).get('hook', 'hook/{date}.log'))" "$config_file" 2>/dev/null || echo "hook/{date}.log")
        fi
    fi
}

# -----------------------------------------------------------------------------
# 初始化日志系统
# -----------------------------------------------------------------------------
# 功能：设置日志文件路径，创建日志目录
# 用法：log_init [log_file_path]
# 参数：
#   log_file_path - 可选，日志文件路径
# -----------------------------------------------------------------------------
log_init() {
    local log_file="${1:-}"

    # 加载配置
    _load_log_config

    if [ -n "$log_file" ]; then
        LOG_FILE="$log_file"
    else
        local log_date
        log_date=$(date "+${_LOG_DATE_FORMAT}")
        local log_filename
        log_filename=$(echo "$_LOG_FILE_PATTERN" | sed "s/{date}/${log_date}/g")
        LOG_FILE="${LOG_DIR}/${log_filename}"
    fi

    # 确保日志目录存在
    local log_dir_path
    log_dir_path="$(dirname "$LOG_FILE")"
    mkdir -p "$log_dir_path"

    # 创建日志文件
    touch "$LOG_FILE" 2>/dev/null || {
        echo "Warning: Cannot create log file: $LOG_FILE" >&2
        LOG_FILE="/dev/null"
    }

    export LOG_FILE
}

# -----------------------------------------------------------------------------
# 通用日志函数
# -----------------------------------------------------------------------------
log() {
    if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
        log_init
    fi

    echo "[$(date "+${_LOG_DATETIME_FORMAT}")] $1" >> "$LOG_FILE"
}

# -----------------------------------------------------------------------------
# 错误日志函数
# -----------------------------------------------------------------------------
log_error() {
    if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
        log_init
    fi

    echo "[$(date "+${_LOG_DATETIME_FORMAT}")] ERROR: $1" >> "$LOG_FILE"
}

# -----------------------------------------------------------------------------
# 原始日志函数（不带时间戳）
# -----------------------------------------------------------------------------
log_raw() {
    if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
        log_init
    fi

    echo "$1" >> "$LOG_FILE"
}

# -----------------------------------------------------------------------------
# 调试日志函数
# -----------------------------------------------------------------------------
log_debug() {
    if [ "${DEBUG:-0}" != "1" ]; then
        return
    fi

    if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
        log_init
    fi

    echo "[$(date "+${_LOG_DATETIME_FORMAT}")] DEBUG: $1" >> "$LOG_FILE"
}

# -----------------------------------------------------------------------------
# 记录输入数据
# -----------------------------------------------------------------------------
log_input() {
    local json_data="${1:-$INPUT}"

    if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
        log_init
    fi

    {
        echo "=========================================="
        echo "时间: $(date "+${_LOG_DATETIME_FORMAT}")"
        echo "=========================================="
        echo ""
        echo "=== 原始 JSON 数据 ==="
        echo "$json_data" | if command -v jq &> /dev/null; then jq .; else cat; fi
        echo ""
    } >> "$LOG_FILE"
}

# -----------------------------------------------------------------------------
# 记录命令日志
# -----------------------------------------------------------------------------
# 功能：将工具命令内容记录到按 session 分组的日志文件
# 用法：log_command "command_content" "request_id" "tool_name" "session_id"
# 参数：
#   $1 - command_content  - 命令内容（必填）
#   $2 - request_id       - 请求 ID（可选，默认 unknown）
#   $3 - tool_name        - 工具名称（可选，默认 unknown）
#   $4 - session_id       - 会话 ID（可选，默认 unknown）
# 输出：写入日志到 ${LOG_DIR}/command/${date}_${session_id}.log
# 返回：0 = 成功
# -----------------------------------------------------------------------------
log_command() {
    local command_content="$1"
    local request_id="${2:-unknown}"
    local tool_name="${3:-unknown}"
    local session_id="${4:-unknown}"

    local command_log_dir="${LOG_DIR}/command"
    mkdir -p "$command_log_dir"

    local date_part
    date_part=$(date "+%Y-%m-%d")
    local log_filename="${date_part}_${session_id}.log"
    local log_file="${command_log_dir}/${log_filename}"

    {
        echo "=========================================="
        echo "时间: $(date "+${_LOG_DATETIME_FORMAT}")"
        echo "请求 ID: ${request_id}"
        echo "工具: ${tool_name}"
        echo "------------------------------------------"
        echo "$command_content"
        echo ""
    } >> "$log_file"

    log "Command logged to: ${log_filename}"
}
