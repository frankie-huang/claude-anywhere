#!/bin/bash
# =============================================================================
# src/hooks/user_prompt.sh - UserPromptSubmit 事件处理脚本
#
# 此脚本由 hook-router.sh 通过 source 调用，不直接执行
#
# 前置条件（由 hook-router.sh 完成）:
#   - $INPUT 变量包含从 stdin 读取的 JSON 数据
#   - 核心库、JSON 解析器、日志系统已初始化
#   - $PROJECT_ROOT, $SRC_DIR, $LIB_DIR 等路径变量已设置
#
# 适用场景:
#   - 用户提交 prompt 时触发
#   - 终端发起的 prompt 同步到 IM 会话中
#   - IM 发起的 prompt 自动跳过（通过 skip 标志去重）
#
# 设计原则:
#   - 快速返回，不阻塞 Agent
#   - 消息发送在后台异步执行
#   - IM 发起的 prompt 通过 skip 标志跳过，避免重复
#   - 只调用 im.sh 的平台无关接口，不感知具体 IM 平台
# =============================================================================

# =============================================================================
# 后台异步发送用户 prompt 消息
# =============================================================================
send_user_prompt_async() {
    # 捕获当前环境变量供后台使用
    local SESSION_ID=$(json_get "$INPUT" "session_id")
    local PROMPT_CONTENT=$(json_get "$INPUT" "prompt")
    local PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(json_get "$INPUT" "cwd")}"

    # 检查是否有可用的发送渠道
    if ! im_channel_ready; then
        return 0
    fi

    # 没有 session_id 则无法关联 IM 会话，跳过
    if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "null" ]; then
        log "UserPromptSubmit: no session_id, skipping"
        return 0
    fi

    # 没有 prompt 内容则跳过
    if [ -z "$PROMPT_CONTENT" ] || [ "$PROMPT_CONTENT" = "null" ]; then
        log "UserPromptSubmit: no prompt content, skipping"
        return 0
    fi

    log "UserPromptSubmit: session=$SESSION_ID, prompt=${PROMPT_CONTENT:0:50}..."

    # 检查 skip 标志（IM 发起的会话会设置此标志）
    local skip_flag
    skip_flag=$(check_skip_user_prompt "$SESSION_ID")
    if [ "$skip_flag" = "true" ]; then
        log "UserPromptSubmit: skipped (IM-originated)"
        return 0
    fi

    # 前置解析 chat_id 并检查 mute 状态，muted 时跳过发送
    local RESOLVED_CHAT_ID
    RESOLVED_CHAT_ID=$(get_chat_id "$SESSION_ID" "$PROJECT_DIR")
    if [ "$RESOLVED_CHAT_ID" = "$MUTED_SENTINEL" ]; then
        log "UserPromptSubmit: session muted, skipping: $SESSION_ID"
        return 0
    fi

    # 截断过长的 prompt（纯文本消息不宜太长）
    local max_prompt_length=10000
    local display_prompt="$PROMPT_CONTENT"
    if [ ${#PROMPT_CONTENT} -gt "$max_prompt_length" ]; then
        display_prompt="${PROMPT_CONTENT:0:$max_prompt_length}
...(已截断)"
    fi

    send_user_prompt_notification "$display_prompt" >/dev/null 2>&1
}

# 后台发送；单独 & 不够——宿主以 pipe 关闭（非 PID 退出）判断 hook 结束，
# 子进程继承 stdout/stderr 会导致 pipe 未关闭，需要 >/dev/null 2>&1 切断
# 注意: UserPromptSubmit hook 配置不要加 async: true，而是通过 & 放后台，脚本立即 exit 0 返回
send_user_prompt_async >/dev/null 2>&1 &

# 立即返回，不阻塞 Agent
exit 0
