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

# 引入飞书/通信函数库（子脚本共享，不需要重复 source）
source "$LIB_DIR/feishu.sh"

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
# per-prompt 回复基准 message_id
# =============================================================================
# callback 的 launch_agent 把 message_id 注入到子进程环境（CODE_ANYWHERE_MESSAGE_ID），
# 此处统一捕获并导出为 REPLY_TO_MSG_ID，供 stop/permission 等 hook 作为卡片 reply_to。
# 终端发起（无飞书消息）时为空，下游 send_feishu_card 自动 fallback 到查 last_message_id。
REPLY_TO_MSG_ID="${CODE_ANYWHERE_MESSAGE_ID:-}"
export REPLY_TO_MSG_ID
log "Reply target message_id: ${REPLY_TO_MSG_ID:-<none, will fallback to last_message_id>}"

# =============================================================================
# per-prompt 发送者 user_id
# =============================================================================
# callback 的 launch_agent 把发送者 user_id 注入到子进程环境（CODE_ANYWHERE_SENDER_ID），
# 此处统一捕获并导出为 SENDER_USER_ID，供 stop 卡片优先 at 这个用户（协作模式下定位提问者）。
# 终端发起（无飞书消息）时为空，_build_at_user_tag 自动 fallback 到 /notify at 配置或默认 owner。
SENDER_USER_ID="${CODE_ANYWHERE_SENDER_ID:-}"
export SENDER_USER_ID
log "Sender user_id: ${SENDER_USER_ID:-<none>}"

# =============================================================================
# 会话 env 快照
# =============================================================================
# hook 继承了用户 shell 实际生效的 env，路由前抓一次白名单 env 供续聊注入
if [ "${MCP_MODE:-}" != "1" ]; then
    # 仅非 MCP 模式：MCP 下 env 来自 server 进程而非用户 shell，捕获无意义
    _session_id=$(json_get "$INPUT" "session_id")
    if [ -n "$_session_id" ] && [ "$_session_id" != "null" ]; then
        _capture_session_env "$_session_id" >/dev/null 2>&1 &  # 后台执行，不阻塞 hook 主流程
    fi
    unset _session_id
fi

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
