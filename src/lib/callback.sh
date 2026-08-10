#!/bin/bash
# =============================================================================
# src/lib/callback.sh - Callback 后端通用客户端（平台无关）
# =============================================================================
#
# 功能说明:
#   提供与 Callback 后端通信所需的、与 IM 平台无关的基础能力。
#   任何 IM 平台的实现（lib/<platform>.sh）和 Hook 脚本都可以直接使用。
#
# 主要函数:
#   get_auth_token()          - 读取存储的 auth_token（网关双向认证）
#   do_curl_post()            - 通用 JSON POST 请求
#   do_callback_post()        - 向 Callback 后端 /cb/* 路由发送 POST
#   get_gateway_url()         - 解析网关地址（部署拓扑决定，与平台无关）
#   get_notify_config()       - 读取运行时通知配置覆盖
#   query_chat_id()           - 查询 session 对应的 chat_id（后端查询原语，
#                               im.sh 的 get_chat_id 在非 feishu 平台转调它）
#   check_skip_user_prompt()  - 检查本次 prompt 是否由 IM 发起（需跳过同步）
#   capture_session_env()     - 上报白名单环境变量到 session
#   resolve_at_target()       - 判定本次通知该 @ 谁
#   build_response_content()  - 构建平台面向的响应正文（截断 + truncated 标记）
#   agent_display_name()      - Agent 展示名
#   agent_resume_command()    - Agent 会话恢复命令
#
# 全局变量:
#   MUTED_SENTINEL             - muted session 哨兵值
#   HTTP_TIMEOUT               - HTTP 请求超时（秒）
#   CALLBACK_SERVER_URL/PORT   - Callback 后端地址
#
# 说明:
#   平台特有的逻辑（飞书的 chat_id 兜底解析、message_id 查询、卡片构建等）
#   放在各自的 lib/<platform>.sh，不属于本文件。
#
# =============================================================================

# 防重复加载：本文件定义了 readonly 常量，重复 source 会向 stderr 输出
# "readonly variable" 错误（不致命但污染 hook 输出）
if type do_callback_post &> /dev/null; then
    return 0
fi

# 引入核心库（如果尚未引入）
if ! type get_config &> /dev/null; then
    source "${BASH_SOURCE[0]%/*}/core.sh"
fi

# 引入 JSON 解析库（如果尚未引入）
if ! type json_init &> /dev/null; then
    source "${BASH_SOURCE[0]%/*}/json.sh"
fi

# =============================================================================
# 常量定义
# =============================================================================

# muted session 哨兵值：query_chat_id 返回此值表示 session 已静音，调用方应跳过发送
readonly MUTED_SENTINEL="__MUTED__"

# HTTP 请求超时时间（秒）
HTTP_TIMEOUT=10

# 回调服务器地址
CALLBACK_SERVER_PORT=$(get_config "CALLBACK_SERVER_PORT" "8080")
CALLBACK_SERVER_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:$CALLBACK_SERVER_PORT")

# =============================================================================
# 凭证读取
# =============================================================================

# ----------------------------------------------------------------------------
# get_auth_token - 获取存储的 auth_token
# ----------------------------------------------------------------------------
# 功能: 从 runtime/auth_token.json 读取存储的 auth_token
#
# 输出:
#   echos 返回: auth_token 字符串，不存在则返回空字符串
#
# 说明:
#   用于网关注册后的双向认证
# ----------------------------------------------------------------------------
get_auth_token() {
    # AUTH_TOKEN_FILE 由 core.sh 定义，指向 runtime/auth_token.json

    if [ ! -f "$AUTH_TOKEN_FILE" ]; then
        log "auth_token file not found: $AUTH_TOKEN_FILE"
        echo ""
        return 0
    fi

    # 使用 json_get 读取 auth_token
    local token_data
    token_data=$(cat "$AUTH_TOKEN_FILE" 2>/dev/null)
    if [ -z "$token_data" ]; then
        echo ""
        return 0
    fi

    local token
    token=$(json_get "$token_data" "auth_token")
    echo "$token"
}

# =============================================================================
# HTTP 请求
# =============================================================================

# ----------------------------------------------------------------------------
# do_curl_post - 执行 POST 请求的通用函数
# ----------------------------------------------------------------------------
# 功能: 执行 JSON POST 请求，并解析响应
#
# 参数:
#   $1 - url           请求 URL
#   $2 - request_body  请求 JSON 字符串
#   $3 - log_prefix    日志前缀 (可选)
#   $4 - auth_token    认证令牌 (可选，会添加到 X-Auth-Token header)
#
# 输出:
#   echos 返回: http_code 响应体
#
# 返回:
#   0 - HTTP 请求成功
#   1 - HTTP 请求失败
#
# 说明:
#   调用方需要根据 http_code 和响应内容判断业务逻辑是否成功
#   auth_token 用于网关注册后的双向认证
# ----------------------------------------------------------------------------
do_curl_post() {
    local url="$1"
    local request_body="$2"
    local log_prefix="${3:-curl}"
    local auth_token="${4:-}"

    # 构建 curl 命令用于日志（请求体截断避免过长）
    local curl_log_cmd
    local truncated_body="${request_body:0:200}"
    if [ ${#request_body} -gt 200 ]; then
        truncated_body="${truncated_body}..."
    fi
    curl_log_cmd=$(cat <<EOF
curl -X POST "$url" \
    -H "Content-Type: application/json" \
    -d "$truncated_body" \
    --max-time "$HTTP_TIMEOUT" \
    --noproxy "*"
EOF
)

    local response
    local http_code

    # 使用数组构建 headers，避免 shell 解析问题
    local headers=("-H" "Content-Type: application/json")
    if [ -n "$auth_token" ]; then
        headers+=("-H" "X-Auth-Token: $auth_token")
    fi

    # 执行 curl 请求，"${headers[@]}" 确保每个元素作为独立参数传递
    response=$(curl -X POST "$url" \
        "${headers[@]}" \
        -d "$request_body" \
        --max-time "$HTTP_TIMEOUT" \
        --noproxy "*" \
        --silent \
        --show-error \
        -w "\n%{http_code}" 2>&1)

    local curl_exit=$?

    # 分离响应体和状态码（跨平台兼容）
    http_code=$(echo "$response" | tail -n1)
    response=$(echo "$response" | sed '$d')

    # 输出 http_code 和 response
    echo "$http_code"
    echo "$response"

    if [ $curl_exit -ne 0 ] || [ "$http_code" -ge 400 ]; then
        log_error "${log_prefix}: curl command failed"
        log_error "${log_prefix}: $curl_log_cmd"
        log_error "${log_prefix}: exit=$curl_exit, http_code=$http_code"
        return 1
    fi

    return 0
}

# ----------------------------------------------------------------------------
# do_callback_post - 向 Callback 后端发送 POST 请求
# ----------------------------------------------------------------------------
# 功能: 封装 callback_url 构造和 auth_token，简化 /cb/* 路由的调用
#
# 参数:
#   $1 - path          路由路径（如 /cb/session/set-env）
#   $2 - request_body  请求 JSON 字符串
#   $3 - log_prefix    日志前缀 (可选，默认取 path 去掉前导 /)
#
# 输出/返回: 同 do_curl_post
# ----------------------------------------------------------------------------
do_callback_post() {
    local path="$1"
    local request_body="$2"
    local log_prefix="${3:-${path#/}}"

    local callback_url="${CALLBACK_SERVER_URL:-http://localhost:${CALLBACK_SERVER_PORT:-8080}}"
    callback_url=$(echo "$callback_url" | sed 's:/*$::')

    do_curl_post "${callback_url}${path}" "$request_body" "$log_prefix" "$(get_auth_token)"
}

# ----------------------------------------------------------------------------
# get_gateway_url - 解析网关地址
# ----------------------------------------------------------------------------
# 功能: 确定 Hook 发送消息的目标服务地址
#
# 输出:
#   echos 返回: 网关地址字符串
#
# 说明:
#   优先级由部署拓扑决定，与 IM 平台无关：
#     FEISHU_GATEWAY_URL  - 分离部署，指向远端网关
#     CALLBACK_SERVER_URL - 未配置网关即单机部署，网关与 callback 同进程
#   FEISHU_GATEWAY_URL 是历史键名，后续会引入平台无关的 GATEWAY_URL 并保留兼容
# ----------------------------------------------------------------------------
get_gateway_url() {
    local gateway_url
    gateway_url=$(get_config "FEISHU_GATEWAY_URL" "")
    echo "${gateway_url:-$CALLBACK_SERVER_URL}"
}

# =============================================================================
# 运行时配置读取
# =============================================================================

# ----------------------------------------------------------------------------
# get_notify_config - 读取运行时通知配置覆盖
# ----------------------------------------------------------------------------
# 功能: 从 runtime/notify_config.json 读取配置
# 输出: JSON 字符串，文件不存在时输出空
# ----------------------------------------------------------------------------
get_notify_config() {
    local config_file="${RUNTIME_DIR}/notify_config.json"
    if [ ! -f "$config_file" ]; then
        return 0
    fi
    cat "$config_file" 2>/dev/null
}

# =============================================================================
# 会话查询与上报
# =============================================================================

# ----------------------------------------------------------------------------
# query_chat_id - 根据 session_id 获取对应的 chat_id
# ----------------------------------------------------------------------------
# 功能: 调用 Callback 后端的 /cb/session/get-chat-id 接口查询 session_id 对应的 chat_id
#
# 参数:
#   $1 - session_id  Agent 会话 ID
#   $2 - project_dir 项目工作目录（可选，用于 mute 目录检查）
#
# 输出:
#   echos 返回: chat_id 字符串，查询失败返回空字符串
#               session 已 muted 时返回 MUTED_SENTINEL
#
# 说明:
#   用于确定会话消息发送的目标群聊
#   查询失败时，调用方可使用平台配置的默认 chat 作为兜底
#
# 与 get_chat_id 的关系:
#   im.sh 的 get_chat_id() 是对 Hook 暴露的派发入口——feishu 走 _resolve_chat_id
#   （含 ensure_chat + FEISHU_CHAT_ID 兜底），其他平台走本函数（只查后端一次）。
#   本函数是底层查询原语，故用 query_ 动词区别于那个派发入口。
# ----------------------------------------------------------------------------
query_chat_id() {
    local session_id="$1"
    local project_dir="${2:-}"

    if [ -z "$session_id" ]; then
        echo ""
        return 0
    fi

    # 传入 project_dir 用于 mute 目录检查（session 不存在时自动继承目录 mute 状态）
    # 传入 agent_type 用于 auto-mute 创建占位 session 时写入正确的 agent 类型
    local agent_type="${AGENT_TYPE:-claude}"
    local request_body
    if [ -n "$project_dir" ]; then
        local escaped_dir
        escaped_dir=$(json_escape "$project_dir")
        request_body="{\"session_id\":\"$session_id\",\"agent_type\":\"$agent_type\",\"project_dir\":\"$escaped_dir\"}"
    else
        request_body="{\"session_id\":\"$session_id\",\"agent_type\":\"$agent_type\"}"
    fi

    local response
    response=$(do_callback_post "/cb/session/get-chat-id" "$request_body")

    local http_code
    http_code=$(echo "$response" | head -n 1)
    response=$(echo "$response" | sed '1d')

    if [ "$http_code" != "200" ]; then
        return 0
    fi

    # 检查 muted 状态：muted 的 session 直接返回哨兵值，跳过后续发送
    local muted
    muted=$(json_get "$response" "muted")
    if [ "$muted" = "true" ]; then
        echo "$MUTED_SENTINEL"
        return 0
    fi

    local chat_id
    chat_id=$(json_get "$response" "chat_id")
    # 移除可能的引号
    chat_id=$(echo "$chat_id" | sed 's/^"//;s/"$//')

    if [ -n "$chat_id" ] && [ "$chat_id" != "null" ] && [ "$chat_id" != "''" ]; then
        echo "$chat_id"
    else
        echo ""
    fi
}

# ----------------------------------------------------------------------------
# check_skip_user_prompt - 检查是否跳过 UserPromptSubmit
# ----------------------------------------------------------------------------
# 功能: 查询 Callback 后端，判断本次 prompt 是否由 IM 发起（需要跳过）
#
# 参数:
#   $1 - session_id  Agent 会话 ID
#
# 返回:
#   0 + stdout "true"  - 应跳过
#   0 + stdout "false" - 不跳过
# ----------------------------------------------------------------------------
check_skip_user_prompt() {
    local session_id="$1"

    if [ -z "$session_id" ]; then
        echo "false"
        return 0
    fi

    local response
    response=$(do_callback_post "/cb/session/check-skip-user-prompt" \
        "{\"session_id\":\"$session_id\"}")

    local http_code
    http_code=$(echo "$response" | head -n 1)
    response=$(echo "$response" | sed '1d')

    if [ "$http_code" != "200" ]; then
        echo "false"
        return 0
    fi

    local skip_flag
    skip_flag=$(json_get "$response" "skip")
    if [ "$skip_flag" = "true" ]; then
        echo "true"
    else
        echo "false"
    fi
}

# ----------------------------------------------------------------------------
# capture_session_env - 把启动 agent 时的白名单 env 快照上报给 callback
# ----------------------------------------------------------------------------
# 功能:
#   hook 是 agent 的子进程，继承了启动 shell 实际生效的 env。
#   按 SESSION_ENV_WHITELIST 配置取出匹配的 env，POST 给 /cb/session/set-env，
#   后端写入 session.env_overrides。续聊时 AgentAdapter 读出作 K=V 前缀注入。
#
# 白名单格式（SESSION_ENV_WHITELIST）:
#   逗号或空格分隔；末尾带 * 视作前缀通配，否则视作精确名。
#   例: "ANTHROPIC_*, OPENAI_*, API_TIMEOUT_MS"
#
# 参数:
#   $1 - session_id  当前会话 ID（必填，空则跳过）
#
# 行为:
#   - 失败时仅记录日志，不影响主流程（best-effort）
#   - 每次 hook 触发都调用一次（幂等覆盖）
# ----------------------------------------------------------------------------
capture_session_env() {
    local session_id="$1"
    [ -z "$session_id" ] && return 0

    local whitelist
    whitelist=$(get_config "SESSION_ENV_WHITELIST" "")
    [ -z "$whitelist" ] && return 0
    whitelist="${whitelist//,/ }"

    # 单遍遍历 env，逐条匹配白名单规则
    local json_pairs=""
    local line var_name val rule match
    while IFS= read -r line; do
        var_name="${line%%=*}"
        val="${line#*=}"
        [ -z "$var_name" ] && continue

        match=""
        for rule in $whitelist; do
            [ -z "$rule" ] && continue
            if [[ "$rule" == *\* ]]; then
                [[ "$var_name" == "${rule%\*}"* ]] && match=1 && break
            else
                [ "$var_name" = "$rule" ] && match=1 && break
            fi
        done
        [ -z "$match" ] && continue

        [ -n "$json_pairs" ] && json_pairs="${json_pairs},"
        json_pairs="${json_pairs}\"${var_name}\":\"$(json_escape "$val")\""
    done < <(env 2>/dev/null)

    if [ -z "$json_pairs" ]; then
        log "capture_session_env: no whitelisted env vars present, skipping"
        return 0
    fi

    local request_body="{\"session_id\":\"$session_id\",\"env\":{${json_pairs}}}"

    local response http_code
    response=$(do_callback_post "/cb/session/set-env" "$request_body")

    http_code=$(echo "$response" | head -n 1)
    if [ "$http_code" != "200" ]; then
        log "capture_session_env: backend returned $http_code (non-fatal)"
    fi
    return 0
}

# =============================================================================
# Agent 展示信息
# =============================================================================

agent_display_name() {
    case "${AGENT_TYPE:-claude}" in
        claude) echo "Claude Code" ;;
        codex) echo "Codex" ;;
        *) echo "${AGENT_TYPE}" ;;
    esac
}

agent_resume_command() {
    local session_id="$1"
    case "${AGENT_TYPE:-claude}" in
        claude) echo "claude --resume $session_id" ;;
        codex) echo "codex resume $session_id" ;;
        *) echo "${AGENT_TYPE} --resume $session_id" ;;
    esac
}

# =============================================================================
# @ 提醒策略
# =============================================================================
# 「该不该 @、@ 谁」由运行时通知配置（/notify at）决定，与 IM 平台无关；
# 「@ 标签长什么样」是平台产物，由各平台自行渲染。

# ----------------------------------------------------------------------------
# _is_in_at_time_range - 检查当前时间是否在 @ 时段内
# ----------------------------------------------------------------------------
# 参数:
#   $1 - at_start (HH:MM)
#   $2 - at_end   (HH:MM)
# 返回值:
#   0 = 在时段内，应该 @
#   1 = 不在时段内，不应该 @
# ----------------------------------------------------------------------------
_is_in_at_time_range() {
    local at_start="$1" at_end="$2"
    local current
    current=$(date +%H:%M)

    if ! [ "$at_start" \> "$at_end" ]; then
        # 不跨午夜：08:00-22:00
        ! [ "$current" \< "$at_start" ] && ! [ "$current" \> "$at_end" ] && return 0
        return 1
    else
        # 跨午夜：22:00-08:00（current >= start 或 current <= end）
        ! [ "$current" \< "$at_start" ] && return 0
        ! [ "$current" \> "$at_end" ] && return 0
        return 1
    fi
}

# ----------------------------------------------------------------------------
# resolve_at_target - 判定本次通知该 @ 谁
# ----------------------------------------------------------------------------
# 功能: 按运行时通知配置 + 可选 sender_id 决定 @ 目标
#
# 参数:
#   $1 - sender_id (可选): 本次 prompt 发送者 user_id，由 hook-router.sh 注入
#   $2 - owner_id  (可选): 当前平台配置的 owner id，由调用方（平台实现）传入
#
# 输出:
#   @ 目标标识（user id，或 /notify at 配置的字面值如 all），不需要 @ 时输出空
#
# 优先级:
#   1. /notify at off → 空（全局关闭）
#   2. 不在 at 时段内 → 空（全局关闭）
#   3. 传入的 sender_id 非空 → sender（本次提问的人；协作模式下定位提问者）
#   4. /notify at self/all/<user_id> → 按 config
#   5. 默认 → owner_id
# ----------------------------------------------------------------------------
resolve_at_target() {
    local sender_id="${1:-}"
    local owner_id="${2:-}"
    local notify_config_json at_user_config at_start at_end

    # 读取运行时通知配置覆盖
    notify_config_json=$(get_notify_config)
    if [ -n "$notify_config_json" ]; then
        # 一次 json_get_multi 取 3 个字段，减少子进程调用
        local -a vals=()
        while IFS= read -r _line; do
            vals+=("$_line")
        done <<< "$(json_get_multi "$notify_config_json" at_user at_start at_end)"
        at_user_config="${vals[0]:-}"
        at_start="${vals[1]:-}"
        at_end="${vals[2]:-}"
    fi

    # 优先级 1: off 全局关闭
    if [ "$at_user_config" = "off" ]; then
        echo ""
        return 0
    fi

    # 优先级 2: 时间窗口外，全局关闭
    if [ -n "$at_start" ] && [ -n "$at_end" ]; then
        if ! _is_in_at_time_range "$at_start" "$at_end"; then
            echo ""
            return 0
        fi
    fi

    # 优先级 3: sender_id 优先于 config（协作模式下定位提问者）
    if [ -n "$sender_id" ]; then
        echo "$sender_id"
        return 0
    fi

    # 优先级 4: config self/all/<user_id>
    if [ "$at_user_config" = "self" ]; then
        echo "$owner_id"
        return 0
    fi
    if [ -n "$at_user_config" ]; then
        # all 或 user_id
        echo "$at_user_config"
        return 0
    fi

    # 优先级 5: 默认 owner
    echo "$owner_id"
}

# =============================================================================
# 响应正文构建
# =============================================================================

# ----------------------------------------------------------------------------
# build_response_content - 构建平台面向的响应正文
# ----------------------------------------------------------------------------
# 功能: 把 extract_response 的原始产出转换成各平台渲染所需的正文内容：
#       按总长度截断 texts，并标记本次是否发生截断
#
# 参数:
#   $1 - response_json  extract_response 产出的 JSON（含 texts 数组）
#   $2 - max_length     所有 texts 累计的最大长度
#
# 输出:
#   {"texts":[...],"truncated":true|false}
#   输入为空/无 texts 数组时输出 {"texts":[],"truncated":false}
#
# 说明:
#   截断是与 IM 平台无关的策略。只输出 texts 与 truncated 两个字段——
#   session_id 由 Hook 自行提取；thinking 是另一条内容通道（长度策略不同、可整体
#   禁用），作为独立参数传递，不并入本结构
# ----------------------------------------------------------------------------
build_response_content() {
    local response_json="$1"
    local max_length="$2"

    local empty='{"texts":[],"truncated":false}'

    if [ -z "$response_json" ] || [ "$response_json" = "null" ]; then
        echo "$empty"
        return 0
    fi

    if [ "$JSON_HAS_JQ" = "true" ]; then
        echo "$response_json" | jq -c --argjson max_len "$max_length" '
            if (.texts | type) != "array" then {texts: [], truncated: false}
            else
                (reduce .texts[] as $t (
                    {remaining: $max_len, result: [], truncated: false};
                    if .remaining <= 0 then .truncated = true
                    elif ($t | length) <= .remaining then
                        .result += [$t] | .remaining -= ($t | length)
                    else
                        .result += [$t[:.remaining] + "..."] | .remaining = 0 | .truncated = true
                    end
                )) as $acc
                | {texts: $acc.result, truncated: $acc.truncated}
            end
        ' 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        echo "$response_json" | "$PYTHON3" -c "
import sys, json
data = json.load(sys.stdin)
texts = data.get('texts')
if not isinstance(texts, list):
    print(json.dumps({'texts': [], 'truncated': False}, ensure_ascii=False))
    sys.exit(0)
max_len = int(sys.argv[1])
result = []
remaining = max_len
is_truncated = False
for text in texts:
    if remaining <= 0:
        is_truncated = True
        break
    if len(text) <= remaining:
        result.append(text)
        remaining -= len(text)
    else:
        result.append(text[:remaining] + '...')
        is_truncated = True
        remaining = 0
print(json.dumps({'texts': result, 'truncated': is_truncated}, ensure_ascii=False))
" "$max_length" 2>/dev/null
    else
        echo "$empty"
    fi
}
