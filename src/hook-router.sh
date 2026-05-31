#!/bin/bash
# =============================================================================
# src/hook-router.sh - Agent Hook 统一路由入口
#
# 这是所有 Agent Hook 的唯一入口脚本
# 根据事件类型（UserPromptSubmit, PermissionRequest, Stop）分发到对应处理脚本
#
# 用法: 配置到 Agent CLI 的 hooks 中
#
# 工作流程:
#   1. 初始化核心库（路径、环境、日志）
#   2. 初始化 JSON 解析器
#   3. 从 stdin 读取 JSON 数据
#   4. 解析事件类型
#   5. 分发到对应处理脚本
#
# 注意: stdin 只能读取一次，读取后保存到 $INPUT 变量
#       子脚本通过 $INPUT 变量获取数据，不再从 stdin 读取
# =============================================================================

# =============================================================================
# 初始化
# =============================================================================

# 获取脚本所在目录（支持软链接）
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# 初始化核心库（路径、环境、日志）
source "$SCRIPT_DIR/lib/core.sh"

# 初始化 JSON 解析器（自动选择 jq > python3 > grep/sed）
source "$LIB_DIR/json.sh"
json_init

# 初始化日志
log_init

# =============================================================================
# 读取输入
# =============================================================================

# 从 stdin 读取 JSON 数据（只能读取一次，子脚本共享此变量）
INPUT=$(cat)

# 记录输入
log_input "$INPUT"

# =============================================================================
# 解析事件类型
# =============================================================================

# 使用 json_get 解析事件类型（自动降级，不强依赖 jq）
HOOK_EVENT=$(json_get "$INPUT" "hook_event_name")
HOOK_EVENT="${HOOK_EVENT:-unknown}"

# Hook 由 CLI 直接启动，进程环境中没有 AGENT_TYPE。
# 从 transcript 路径推断 agent 类型；无法识别时回退到 DEFAULT_AGENT 配置。
TRANSCRIPT_PATH_FOR_AGENT=$(json_get "$INPUT" "transcript_path")
case "$TRANSCRIPT_PATH_FOR_AGENT" in
    */.codex/sessions/*)
        AGENT_TYPE="codex"
        ;;
    */.claude/*)
        AGENT_TYPE="claude"
        ;;
    *)
        AGENT_TYPE="$(get_config "DEFAULT_AGENT" "claude")"
        ;;
esac
export AGENT_TYPE

log "Hook router received event: $HOOK_EVENT, agent: $AGENT_TYPE"

# =============================================================================
# 路由分发
# =============================================================================

# 根据事件类型分发到对应处理脚本
# 子脚本通过 $INPUT 变量获取输入数据（不再从 stdin 读取）
case "$HOOK_EVENT" in
    UserPromptSubmit)
        if [ "$(get_config "HOOK_USER_PROMPT_ENABLED" "true")" = "false" ]; then
            log "UserPromptSubmit hook disabled, skipping"
            exit 0
        fi
        log "Routing to user prompt handler"
        source "$SRC_DIR/hooks/user_prompt.sh"
        ;;
    PermissionRequest)
        if [ "$(get_config "HOOK_PERMISSION_ENABLED" "true")" = "false" ]; then
            log "PermissionRequest hook disabled, falling back to terminal"
            exit 1
        fi
        log "Routing to permission handler"
        source "$SRC_DIR/hooks/permission.sh"
        ;;
    Stop)
        if [ "$(get_config "HOOK_STOP_ENABLED" "true")" = "false" ]; then
            log "Stop hook disabled, skipping"
            exit 0
        fi
        log "Routing to stop handler"
        source "$SRC_DIR/hooks/stop.sh"
        ;;
    *)
        log_error "Unknown hook event: $HOOK_EVENT, falling back to terminal"
        # 未知事件类型，回退到终端交互
        exit 1
        ;;
esac
