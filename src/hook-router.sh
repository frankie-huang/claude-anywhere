#!/bin/bash
# =============================================================================
# src/hook-router.sh - Claude Code Hook 统一路由入口
#
# 这是所有 Claude Code Hook 的唯一入口脚本
# 根据事件类型（UserPromptSubmit, PermissionRequest, Notification, Stop）分发到对应处理脚本
#
# 用法: 配置到 Claude Code settings.json 的 hooks 中
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

log "Hook router received event: $HOOK_EVENT"

# =============================================================================
# 路由分发
# =============================================================================

# 根据事件类型分发到对应处理脚本
# 子脚本通过 $INPUT 变量获取输入数据（不再从 stdin 读取）
case "$HOOK_EVENT" in
    PermissionRequest)
        log "Routing to permission handler"
        source "$SRC_DIR/hooks/permission.sh"
        ;;
    Notification)
        log "Routing to webhook handler"
        source "$SRC_DIR/hooks/webhook.sh"
        ;;
    Stop)
        log "Routing to stop handler"
        source "$SRC_DIR/hooks/stop.sh"
        ;;
    UserPromptSubmit)
        log "Routing to user prompt handler"
        source "$SRC_DIR/hooks/user_prompt.sh"
        ;;
    *)
        log_error "Unknown hook event: $HOOK_EVENT, falling back to terminal"
        # 未知事件类型，回退到终端交互
        exit 1
        ;;
esac
