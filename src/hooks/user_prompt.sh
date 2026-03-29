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
#   - 终端发起的 prompt 同步到飞书话题中
#   - 飞书发起的 prompt 自动跳过（通过 skip 标志去重）
#
# 设计原则:
#   - 快速返回，不阻塞 Claude Code
#   - 消息发送在后台异步执行
#   - 飞书发起的 prompt 通过 skip 标志跳过，避免重复
# =============================================================================

# =============================================================================
# 后台异步发送用户 prompt 消息
# =============================================================================
send_user_prompt_async() {
    # 捕获当前环境变量供后台使用
    local SEND_MODE=$(get_config "FEISHU_SEND_MODE" "webhook")
    local WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "")
    local CALLBACK_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")
    local SESSION_ID=$(json_get "$INPUT" "session_id")
    local PROMPT_CONTENT=$(json_get "$INPUT" "prompt")
    local PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(json_get "$INPUT" "cwd")}"

    # 检查是否有可用的发送渠道（webhook 需要 URL，openapi 模式直接放行）
    if [ "$SEND_MODE" != "openapi" ] && [ -z "$WEBHOOK_URL" ]; then
        return 0
    fi

    # 引入函数库（后台进程需要重新引入）
    source "$LIB_DIR/core.sh" 2>/dev/null || return 0
    source "$LIB_DIR/feishu.sh" 2>/dev/null || return 0
    source "$LIB_DIR/json.sh" 2>/dev/null || return 0
    json_init

    # 没有 session_id 则无法关联飞书话题，跳过
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

    # 检查 skip 标志（飞书发起的会话会设置此标志）
    local skip_flag
    skip_flag=$(_check_skip_user_prompt "$SESSION_ID")
    if [ "$skip_flag" = "true" ]; then
        log "UserPromptSubmit: skipped (feishu-originated)"
        return 0
    fi

    # 发送 prompt 文本到飞书话题
    # 截断过长的 prompt（纯文本消息不宜太长）
    local max_prompt_length=10000
    local display_prompt="$PROMPT_CONTENT"
    if [ ${#PROMPT_CONTENT} -gt "$max_prompt_length" ]; then
        display_prompt="${PROMPT_CONTENT:0:$max_prompt_length}
...(已截断)"
    fi

    # 构建显示文本
    local message_text="${display_prompt}"

    # 使用 send_feishu_post 发送富文本消息（带 session threading + at）
    local options
    options=$(json_build_object "project_dir" "$PROJECT_DIR" "session_id" "$SESSION_ID" "callback_url" "$CALLBACK_URL")
    send_feishu_post "$message_text" "$options" >/dev/null 2>&1
}

# 启动后台发送（不等待，立即返回）
# 注意: 不要在 settings.json 中给此 hook 加 async: true，
# 而是通过 & 将发送函数放到后台，脚本立即 exit 0 返回，不阻塞 Claude Code
send_user_prompt_async &

# 立即返回，不阻塞 Claude Code
exit 0
