#!/bin/bash
# =============================================================================
# src/hooks/permission.sh - Claude Code 权限请求处理脚本
#
# 此脚本由 hook-router.sh 通过 source 调用，不直接执行
#
# 前置条件（由 hook-router.sh 完成）:
#   - $INPUT 变量包含从 stdin 读取的 JSON 数据
#   - 核心库、JSON 解析器、日志系统已初始化
#   - $PROJECT_ROOT, $SRC_DIR, $LIB_DIR 等路径变量已设置
#
# 工作模式:
#   1. 交互模式: 回调服务运行时，发送带按钮的卡片，等待用户点击，返回决策
#   2. 降级模式: 回调服务不可用时，仅发送通知卡片，不等待响应
# =============================================================================

# =============================================================================
# 引入额外的函数库
# =============================================================================
source "$LIB_DIR/tool.sh"
source "$LIB_DIR/feishu.sh"
source "$LIB_DIR/socket.sh"
source "$LIB_DIR/vscode-proxy.sh"

# =============================================================================
# Claude Code Hook Exit Codes
# 参考: https://docs.anthropic.com/en/docs/claude-code/hooks
#
# EXIT_HOOK_SUCCESS (0)  - Hook 成功执行，使用输出的决策 JSON
# EXIT_FALLBACK (1)      - 回退到终端交互模式（非0非2的任意值）
# EXIT_HOOK_ERROR (2)    - Hook 执行错误，Claude 会显示错误信息
# =============================================================================
EXIT_HOOK_SUCCESS=0
EXIT_FALLBACK=1
EXIT_HOOK_ERROR=2

# =============================================================================
# 配置 (优先级: .env > 环境变量 > 默认值)
# =============================================================================
SOCKET_PATH=$(get_config "PERMISSION_SOCKET_PATH" "/tmp/claude-permission.sock")
WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "")
CALLBACK_SERVER_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")
NOTIFY_DELAY=$(get_config "PERMISSION_NOTIFY_DELAY" "60")
OWNER_ID=$(get_config "FEISHU_OWNER_ID" "")

# 时间戳
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

# =============================================================================
# 解析 JSON（使用 $INPUT 变量，不再从 stdin 读取）
# =============================================================================
TOOL_NAME=$(json_get "$INPUT" "tool_name")
TOOL_NAME="${TOOL_NAME:-unknown}"
SESSION_ID=$(json_get "$INPUT" "session_id")
SESSION_ID="${SESSION_ID:-unknown}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(json_get "$INPUT" "cwd")}"
PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
TRANSCRIPT_PATH=$(json_get "$INPUT" "transcript_path")
TOOL_USE_ID=$(json_get "$INPUT" "tool_use_id")

log "Tool: $TOOL_NAME, Session: $SESSION_ID, Project: $PROJECT_DIR, ToolUseID: ${TOOL_USE_ID:-N/A}"

# =============================================================================
# 从 transcript 文件末尾查找 tool_use_id
# =============================================================================
# 功能：当输入 JSON 中没有 tool_use_id 时，从 transcript 文件的最后 N 行中
#       查找 type=tool_use 且 name 匹配的记录，返回其 id
# 用法：find_tool_use_id "tool_name" "transcript_path"
# 输出：tool_use_id 字符串，找不到则返回空
#
# 已知限制（误判风险）：
#   当 Claude 并发发起多个相同 tool_name 的权限请求时（例如同一条 assistant
#   消息中包含多个 Bash tool_use），各 hook 进程都会找到"最后一个"匹配的
#   tool_use_id，导致先触发的 hook 监听错误的 tool_result。
#   这是无法避免的，除非 Claude Code 在 PermissionRequest 输入中提供 tool_use_id。
#   好在此场景下文件大小降级检测仍可正常工作。
# =============================================================================
find_tool_use_id() {
    local tool_name="$1"
    local transcript="$2"

    if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
        return 1
    fi

    # 从文件末尾读取最近的行，在其中查找匹配的 tool_use
    # 优先级与 json_init() 保持一致：jq > python3
    if [ "$JSON_HAS_JQ" = "true" ]; then
        # 正向遍历，保留最后一个匹配的 id（避免依赖 tac/tail -r 的平台差异）
        local last_id=""
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            # 单次 jq 调用同时过滤 type 和提取 id，减少 fork 开销
            local found_id
            found_id=$(echo "$line" | jq -r --arg tn "$tool_name" \
                'select(.type=="assistant") | [.message.content[]? | select(.type=="tool_use" and .name==$tn) | .id] | last // empty' 2>/dev/null)
            if [ -n "$found_id" ]; then
                last_id="$found_id"
            fi
        done < <(tail -10 "$transcript" 2>/dev/null)
        if [ -n "$last_id" ]; then
            echo "$last_id"
            return 0
        else
            return 1
        fi
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        tail -10 "$transcript" 2>/dev/null | python3 -c "
import sys, json
tool_name = sys.argv[1]
# 倒序遍历，找到最近的匹配 tool_use
lines = list(sys.stdin)
for line in reversed(lines):
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        if obj.get('type') != 'assistant':
            continue
        msg = obj.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == tool_name:
                print(block.get('id', ''))
                sys.exit(0)
    except Exception:
        continue
sys.exit(1)  # 未找到匹配
" "$tool_name" 2>/dev/null
        return $?
    fi
    return 1  # 无可用解析器
}

# =============================================================================
# 检查 transcript 中是否存在指定 tool_use_id 的 tool_result
# =============================================================================
# 功能：在 transcript 文件末尾检查是否有 tool_result 记录匹配给定的 tool_use_id
#       有则说明用户已在终端做出了权限决策
# 用法：check_tool_result_exists "tool_use_id" "transcript_path"
# 返回：0 = 存在（已决策），1 = 不存在（未决策）
# =============================================================================
check_tool_result_exists() {
    local tool_use_id="$1"
    local transcript="$2"

    if [ -z "$tool_use_id" ] || [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
        return 1
    fi

    # 从文件末尾读取，查找匹配的 tool_result
    # tool_use_id 出现在 user 类型条目的 message.content[].tool_use_id 中
    # 优先级与 json_init() 保持一致：jq > python3 > native
    if [ "$JSON_HAS_JQ" = "true" ]; then
        # 使用进程替换避免管道子 shell 问题（return 需要退出函数而非子 shell）
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            local has_result
            has_result=$(echo "$line" | jq -r --arg tid "$tool_use_id" \
                'select(.type=="user") | .message.content[]? | select(.type=="tool_result" and .tool_use_id==$tid) | "found"' 2>/dev/null | head -1)
            if [ "$has_result" = "found" ]; then
                return 0
            fi
        done < <(tail -10 "$transcript" 2>/dev/null)
        return 1
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        tail -10 "$transcript" 2>/dev/null | python3 -c "
import sys, json
target_id = sys.argv[1]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        if obj.get('type') != 'user':
            continue
        msg = obj.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') == target_id:
                sys.exit(0)
    except Exception:
        continue
sys.exit(1)
" "$tool_use_id" 2>/dev/null
        return $?
    else
        # 降级：使用 grep -F 固定字符串匹配，避免 tool_use_id 中特殊字符被解释为正则
        tail -10 "$transcript" 2>/dev/null | grep -qF "\"tool_use_id\": \"$tool_use_id\""
        return $?
    fi
}

# =============================================================================
# 延迟发送（带多种退出条件检测）
#
# 背景：
#   当用户在终端做出权限决策后，Claude Code 不会 kill hook 进程，也不会发送取消信号。
#   Hook 进程会继续运行直到超时，造成资源浪费。
#   参考讨论：Claude Code 的 hook 协议没有定义"取消"机制，hook 被视为独立进程。
#
# 检测机制：
#   1. 原父进程退出：kill -0 检测原始父进程是否存活（Claude 异常退出时）
#   2. 进程被 reparent：检测当前 PPID 是否变化，应对 PID 重用的极端情况
#   3. tool_result 检测：通过 tool_use_id 精确检测用户是否已在终端做出决策
#      - 优先从输入 JSON 读取 tool_use_id
#      - 读取不到则从 transcript 末尾匹配 tool_name 找到 tool_use_id
#      - 在循环中检查 transcript 是否出现对应的 tool_result 记录
#   4. 降级方案：无法获取 tool_use_id 时，使用 transcript 文件大小增长检测
# =============================================================================
delay_with_decision_check() {
    local delay="$1"
    local original_ppid="$PPID"

    if [ -z "$delay" ] || [ "$delay" -le 0 ] 2>/dev/null; then
        return 0
    fi

    # 解析 tool_use_id（优先从输入获取，否则从 transcript 查找）
    local tool_use_id="$TOOL_USE_ID"
    if [ -z "$tool_use_id" ] && [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
        tool_use_id=$(find_tool_use_id "$TOOL_NAME" "$TRANSCRIPT_PATH")
        if [ -n "$tool_use_id" ]; then
            log "Found tool_use_id from transcript: $tool_use_id (tool: $TOOL_NAME)"
        fi
    fi

    # 降级标记：无法获取 tool_use_id 时使用文件大小检测
    local use_size_fallback="false"
    local initial_size=0
    if [ -z "$tool_use_id" ]; then
        use_size_fallback="true"
        if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
            initial_size=$(stat -c%s "$TRANSCRIPT_PATH" 2>/dev/null || stat -f%z "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
        fi
        log "No tool_use_id available, using file size fallback (initial: $initial_size)"
    fi

    # 动态检测间隔：控制总检测次数不超过 60 次，减少 fork 开销
    # delay<=60s 时每秒检测；delay=120s 时每 2s；delay=180s 时每 3s，以此类推
    local sleep_interval=$(( (delay + 59) / 60 ))

    log "Delaying notification for ${delay}s (PPID: $original_ppid, tool_use_id: ${tool_use_id:-N/A}, interval: ${sleep_interval}s)"

    local elapsed=0
    while [ "$elapsed" -lt "$delay" ]; do
        sleep "$sleep_interval"
        elapsed=$((elapsed + sleep_interval))

        # 检测 1：原父进程退出
        if ! kill -0 "$original_ppid" 2>/dev/null; then
            log "Original parent process $original_ppid exited, skipping notification"
            return 1
        fi

        # 检测 2：进程被 reparent（当前 PPID != 原始 PPID，说明父进程已退出）
        local current_ppid
        current_ppid=$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')
        if [ -n "$current_ppid" ] && [ "$current_ppid" != "$original_ppid" ]; then
            log "Process reparented ($original_ppid -> $current_ppid), parent exited"
            return 1
        fi

        # 检测 3：transcript 中是否已有 tool_result（精确检测）
        if [ "$use_size_fallback" = "false" ]; then
            if check_tool_result_exists "$tool_use_id" "$TRANSCRIPT_PATH"; then
                log "tool_result found for $tool_use_id, permission decided at terminal"
                return 1
            fi
        else
            # 降级：transcript 文件大小增长检测
            if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
                local current_size
                current_size=$(stat -c%s "$TRANSCRIPT_PATH" 2>/dev/null || stat -f%z "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
                if [ "$current_size" -gt "$initial_size" ]; then
                    log "Transcript file grew ($initial_size -> $current_size), permission decided at terminal"
                    return 1
                fi
            fi
        fi

    done

    return 0
}

# =============================================================================
# 生成请求 ID（安全增强）
# =============================================================================
# 生成 32 字符的随机 request_id（192 位熵，降低可预测性）
generate_request_id() {
    # 优先使用 uuidgen 生成完整的 UUID，再取 32 字符
    if command -v uuidgen &> /dev/null; then
        # 生成两个 UUID 拼接，去除横线，得到 32 字符
        local uuid1=$(uuidgen | tr -d '-')
        local uuid2=$(uuidgen | tr -d '-')
        echo "${uuid1:0:16}${uuid2:0:16}"
    elif [ -f /proc/sys/kernel/random/uuid ]; then
        local uuid1=$(cat /proc/sys/kernel/random/uuid | tr -d '-')
        local uuid2=$(cat /proc/sys/kernel/random/uuid | tr -d '-')
        echo "${uuid1:0:16}${uuid2:0:16}"
    else
        # 从 urandom 读取 24 字节，base64 编码得到 32 字符
        head -c 24 /dev/urandom | base64 | tr -d '=+/'
    fi
}

REQUEST_ID=$(generate_request_id)
log "Request ID: $REQUEST_ID"

# =============================================================================
# 通用变量准备（被 run_interactive_mode 和 run_fallback_mode 共用）
# =============================================================================
prepare_common_vars() {
    # 提取工具详情
    extract_tool_detail "$INPUT" "$TOOL_NAME"
    COMMAND_CONTENT="$EXTRACTED_COMMAND"
    DESCRIPTION="$EXTRACTED_DESCRIPTION"
    TEMPLATE_COLOR="$EXTRACTED_COLOR"
    PROJECT_NAME=$(basename "$PROJECT_DIR")

    # 记录 command 到单独的日志文件
    log_command "$COMMAND_CONTENT" "$REQUEST_ID" "$TOOL_NAME" "$SESSION_ID"
}

# =============================================================================
# 发送飞书通知卡片
# =============================================================================
# 参数:
#   $1 - buttons_json (可选，空则无按钮)
#   $2 - footer_hint (可选，自定义底部提示)
# =============================================================================
send_permission_notification() {
    local buttons="${1:-}"
    local custom_footer_hint="${2:-}"

    # 构建飞书卡片
    local card
    card=$(build_permission_card "$TOOL_NAME" "$PROJECT_NAME" "$TIMESTAMP" "$COMMAND_CONTENT" "$DESCRIPTION" "$TEMPLATE_COLOR" "$buttons" "${SESSION_ID:0:8}" "$custom_footer_hint")

    # 传递 session_id、project_dir、callback_url 支持回复继续会话
    local options
    options=$(json_build_object "webhook_url" "$WEBHOOK_URL" "session_id" "$SESSION_ID" "project_dir" "$PROJECT_DIR" "callback_url" "$CALLBACK_SERVER_URL")
    send_feishu_card "$card" "$options"
}

# =============================================================================
# 交互模式主流程
# =============================================================================
run_interactive_mode() {
    log "Running in interactive mode"

    if [ -z "$WEBHOOK_URL" ]; then
        log "FEISHU_WEBHOOK_URL not set, falling back to terminal interaction"
        run_fallback_mode
        return
    fi

    # 准备通用变量
    prepare_common_vars

    # 延迟发送
    if ! delay_with_decision_check "$NOTIFY_DELAY"; then
        exit $EXIT_FALLBACK
    fi

    # AskUserQuestion 类型：发送通知卡片，不等待响应，回退到终端交互
    # 因为 AskUserQuestion 需要用户选择选项，而不是简单的批准/拒绝
    if [ "$TOOL_NAME" = "AskUserQuestion" ]; then
        log "AskUserQuestion detected, sending notification-only card and falling back to terminal"
        send_permission_notification "" "请在终端中选择您的答案"
        log "AskUserQuestion notification sent, falling back to terminal interaction"
        exit $EXIT_FALLBACK
    fi

    # 构建交互按钮（根据 FEISHU_SEND_MODE 自动选择按钮类型）
    local buttons
    buttons=$(build_permission_buttons "$CALLBACK_SERVER_URL" "$REQUEST_ID" "$OWNER_ID")

    # 发送带按钮的飞书卡片
    log "Sending interactive Feishu card"
    send_permission_notification "$buttons"

    # 构建请求 JSON
    local request_json
    local encoded_input
    encoded_input=$(echo "$INPUT" | base64 | tr -d '\n')

    if [ "$JSON_HAS_JQ" = "true" ]; then
        request_json=$(jq -n \
            --arg rid "$REQUEST_ID" \
            --arg pdir "$PROJECT_DIR" \
            --arg enc "$encoded_input" \
            --arg hpid "$$" \
            '{request_id: $rid, project_dir: $pdir, raw_input_encoded: $enc, hook_pid: $hpid}')
    else
        local escaped_project
        escaped_project=$(echo "$PROJECT_DIR" | sed 's/\\/\\\\/g; s/"/\\"/g')
        request_json=$(json_build_object "request_id" "$REQUEST_ID" "project_dir" "$escaped_project" "raw_input_encoded" "$encoded_input" "hook_pid" "$$")
    fi

    log "Sending request to callback server"

    # 通过 Socket 发送请求并等待响应
    local response
    response=$(socket_send_request "$request_json" "$SOCKET_PATH")

    if [ -z "$response" ]; then
        log "Socket communication failed, empty response (card already sent)"
        exit $EXIT_FALLBACK
    fi

    log "Received response: $response"

    # 解析响应
    parse_socket_response "$response"

    if [ "$RESPONSE_FALLBACK" = "true" ]; then
        log "Server timeout, falling back to terminal interaction (card already sent)"
        exit $EXIT_FALLBACK
    fi

    if [ "$RESPONSE_SUCCESS" = "true" ]; then
        output_decision "$RESPONSE_BEHAVIOR" "$RESPONSE_MESSAGE" "$RESPONSE_INTERRUPT"
        vscode_proxy_activate "$PROJECT_DIR"
    else
        log "Callback service returned error, falling back to terminal (card already sent)"
        exit $EXIT_FALLBACK
    fi
}

# =============================================================================
# 降级模式
# =============================================================================
run_fallback_mode() {
    log "Running in fallback mode (notification only)"

    if [ -z "$WEBHOOK_URL" ]; then
        log "FEISHU_WEBHOOK_URL not set, skipping notification"
        exit $EXIT_FALLBACK
    fi

    # 准备通用变量
    prepare_common_vars

    # 延迟发送
    if ! delay_with_decision_check "$NOTIFY_DELAY"; then
        exit $EXIT_FALLBACK
    fi

    # 发送不带按钮的通知卡片
    send_permission_notification ""

    log "Fallback notification sent"
    exit $EXIT_FALLBACK
}

# =============================================================================
# 输出决策给 Claude Code
# =============================================================================
output_decision() {
    local behavior="$1"
    local message="$2"
    local interrupt="$3"

    log "Outputting decision: behavior=$behavior, message=$message, interrupt=$interrupt"

    local decision_json

    if [ "$behavior" = "allow" ]; then
        decision_json='{"behavior": "allow"}'
    elif [ "$interrupt" = "true" ]; then
        decision_json="{\"behavior\": \"deny\", \"message\": \"${message}\", \"interrupt\": true}"
    else
        decision_json="{\"behavior\": \"deny\", \"message\": \"${message}\"}"
    fi

    cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": ${decision_json}
  }
}
EOF

    log "Decision output complete"
}

# =============================================================================
# 主逻辑
# =============================================================================

# 检查 Socket 通信工具可用性
check_socket_tools

# 无 Socket 工具时直接降级
if [ "$HAS_SOCKET_CLIENT" = "false" ] && [ "$HAS_SOCAT" = "false" ]; then
    log "Warning: neither socket_client.py (python3) nor socat available, falling back to notification-only mode"
    run_fallback_mode
    exit $EXIT_FALLBACK
fi

# 检查回调服务并选择模式
if check_socket_service "$SOCKET_PATH"; then
    run_interactive_mode
else
    log "Callback service not available"
    run_fallback_mode
fi

# 交互模式成功输出决策后正常退出
exit $EXIT_HOOK_SUCCESS
