#!/bin/bash
# =============================================================================
# src/lib/feishu.sh - 飞书卡片构建和发送函数库
# =============================================================================
#
# 功能说明:
#   提供飞书卡片消息的构建、渲染和发送功能
#   支持模板化渲染,提供子模板组合能力
#
# 主要函数:
#   render_template()         - 渲染模板文件(变量替换)
#   render_sub_template()     - 渲染子模板元素
#   render_card_template()    - 渲染卡片模板
#   build_permission_card()   - 构建权限请求卡片
#   build_permission_buttons()- 构建权限请求按钮
#   build_notification_card() - 构建通用通知卡片
#   send_feishu_card()        - 发送飞书卡片
#   send_feishu_text()        - 发送飞书文本消息(降级)
#   send_feishu_post()        - 发送飞书富文本消息(支持 at、链式回复)
#
# 全局变量:
#   FEISHU_WEBHOOK_URL        - 飞书 Webhook URL
#   FEISHU_SEND_MODE          - 发送模式 (webhook/openapi)
#   FEISHU_TEMPLATE_PATH      - 自定义模板目录路径(可选)
#
# 使用示例:
#   source "$LIB_DIR/feishu.sh"
#   buttons=$(build_permission_buttons "http://localhost:8080" "req-123" "$owner_id")
#   card=$(build_permission_card "Bash" "myproject" "2024-01-01 12:00:00" \
#       "npm install" "安装依赖" "orange" "$buttons")
#   send_feishu_card "$card"
#
# =============================================================================

# =============================================================================
# 常量定义
# =============================================================================

# 引入核心库（如果尚未引入）
if ! type get_config &> /dev/null; then
    source "${BASH_SOURCE[0]%/*}/core.sh"
fi

# 引入 JSON 解析库（如果尚未引入）
if ! type json_init &> /dev/null; then
    source "${BASH_SOURCE[0]%/*}/json.sh"
fi

# muted session 哨兵值：_get_chat_id / _resolve_chat_id 返回此值表示 session 已静音，调用方应跳过发送
readonly MUTED_SENTINEL="__MUTED__"

# 初始化 JSON 解析器
json_init >/dev/null 2>&1 || true

# 默认模板目录
DEFAULT_TEMPLATE_DIR="${TEMPLATES_DIR}/feishu"

# HTTP 请求超时时间（秒）
FEISHU_HTTP_TIMEOUT=10

# 回调服务器地址（用于 OpenAPI 发送）
CALLBACK_SERVER_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")
CALLBACK_SERVER_PORT=$(get_config "CALLBACK_SERVER_PORT" "8080")

# =============================================================================
# 模板验证函数
# =============================================================================

# ----------------------------------------------------------------------------
# validate_template - 验证模板文件
# ----------------------------------------------------------------------------
# 功能: 验证模板文件是否存在并可读
#
# 参数:
#   $1 - 模板文件路径
#
# 返回:
#   0 - 验证成功
#   1 - 验证失败
#
# 注意:
#   模板文件包含占位符,无法直接用 JSON 验证器验证
#   渲染后会验证最终输出的 JSON 格式
# ----------------------------------------------------------------------------
validate_template() {
    local template_file="$1"

    [ -f "$template_file" ] && [ -r "$template_file" ]
}

# =============================================================================
# 模板渲染函数
# =============================================================================

# ----------------------------------------------------------------------------
# render_template - 渲染模板文件(核心函数)
# ----------------------------------------------------------------------------
# 功能: 加载模板文件并替换变量占位符
#
# 参数:
#   $1               - 模板文件路径
#   ${@:2}           - 变量键值对 (格式: "key=value")
#
# 输出:
#   渲染后的模板内容
#
# 返回:
#   0 - 渲染成功
#   1 - 渲染失败
#
# 示例:
#   render_template "card.json" "title=通知" "content=**Hello**"
#
# 注意:
#   - 占位符格式: {{variable_name}}
#   - JSON 片段类型变量不会被转义 (buttons_json, description_element, detail_elements)
# ----------------------------------------------------------------------------
render_template() {
    local template_file="$1"
    shift

    # 检查模板文件
    if [ ! -f "$template_file" ]; then
        log_error "Template file not found: $template_file"
        return 1
    fi

    # 读取模板内容
    local template_content
    template_content=$(cat "$template_file")

    # 替换所有变量占位符
    local var_assign
    for var_assign in "$@"; do
        local key="${var_assign%%=*}"
        local value="${var_assign#*=}"

        # JSON 片段类型变量不需要转义（已预先转义的 JSON 片段）
        # 注意：response_content 需要转义，因为它嵌入在 JSON 字符串内
        if [[ "$key" != "buttons_json" ]] && \
           [[ "$key" != "description_element" ]] && \
           [[ "$key" != "detail_elements" ]] && \
           [[ "$key" != "thinking_element" ]] && \
           [[ "$key" != "response_elements" ]] && \
           [[ "$key" != "ask_question_form_elements" ]]; then
            # 转义特殊字符为 JSON 格式
            # JSON 解析器会将转义序列解释为实际字符（\t → TAB, \n → LF），需重新转义回 JSON 格式
            if [[ "$key" == "command" ]] || [[ "$key" == "diff_old" ]] || [[ "$key" == "diff_new" ]] || [[ "$key" == "write_content" ]]; then
                # 代码类变量（嵌入飞书 Markdown 代码块）：
                # 1. 转义行首 ``` 为 \` \` \`（防止破坏外层代码块），保留前面的空格
                # 2. 哨兵技巧：追加 x 防止 $() 吞掉尾部换行，sed 后去除
                # 3. 用 python3 json.dumps 处理 JSON 转义（降级到 sed 链）
                value=$(printf '%sx' "$value" | sed 's/^\([[:space:]]*\)```/\1\\`\\`\\`/g')
                value="${value%x}"
                if [ -n "$PYTHON3" ]; then
                    value=$(RENDER_CONTENT="$value" "$PYTHON3" -c 'import os,json; print(json.dumps(os.environ["RENDER_CONTENT"]))' 2>/dev/null | sed 's/^"//;s/"$//')
                else
                    # 降级到 sed 链
                    value=$(printf '%s' "$value" | \
                        sed 's/\\/\\\\/g' | \
                        sed 's/"/\\"/g' | \
                        sed $'s/\t/\\\\t/g' | \
                        sed 's/$/\\n/g' | tr -d '\n' | sed 's/\\n$//')
                fi
            elif [[ "$key" == "response_content" ]] || [[ "$key" == "thinking_content" ]] || [[ "$key" == "plan_content" ]]; then
                # response_content: 飞书 Markdown 代码块必须在行首
                # 1. 删除代码块标记前的空格（如 "   ```bash" → "```bash"）
                # 2. 用 python3 json.dumps 处理 JSON 转义
                value=$(printf '%s' "$value" | sed 's/^[[:space:]]*```/```/g')
                if [ -n "$PYTHON3" ]; then
                    value=$(RENDER_CONTENT="$value" "$PYTHON3" -c 'import os,json; print(json.dumps(os.environ["RENDER_CONTENT"]))' 2>/dev/null | sed 's/^"//;s/"$//')
                else
                    # 降级到 sed 链
                    value=$(printf '%s' "$value" | \
                        sed 's/\\/\\\\/g' | \
                        sed 's/"/\\"/g' | \
                        sed 's/$/\\n/g' | tr -d '\n' | sed 's/\\n$//')
                fi
            else
                # 其他变量: 同样处理所有 JSON 特殊字符
                value=$(printf '%s' "$value" | \
                    sed 's/\\/\\\\/g' | \
                    sed 's/"/\\"/g' | \
                    sed $'s/\t/\\\\t/g' | \
                    sed 's/$/\\n/g' | tr -d '\n' | sed 's/\\n$//')
            fi
        fi

        # tr -c 将非 [a-zA-Z0-9_] 字符替换为 _，防止 key 注入 awk 代码
        local awk_key="TEMPLATE_KEY_$(echo "$key" | tr -c 'a-zA-Z0-9_' '_')"
        local awk_val="TEMPLATE_VAL_$(echo "$key" | tr -c 'a-zA-Z0-9_' '_')"
        export "$awk_key"="{{$key}}"
        export "$awk_val"="$value"

        # 使用 awk index+substr 进行字面替换（避免 gsub 对 \ 和 & 的特殊解释）
        # 通过 ENVIRON 传递变量避免 shell 展开问题
        template_content=$(awk '
        {
            key = ENVIRON["'"$awk_key"'"]
            val = ENVIRON["'"$awk_val"'"]
            out = ""
            while ((i = index($0, key)) > 0) {
                out = out substr($0, 1, i-1) val
                $0 = substr($0, i + length(key))
            }
            print out $0
        }
        ' <<< "$template_content")

        # 清理环境变量
        unset "$awk_key" "$awk_val"
    done

    printf '%s\n' "$template_content"
}

# ----------------------------------------------------------------------------
# render_sub_template - 渲染子模板元素
# ----------------------------------------------------------------------------
# 功能: 渲染子模板并返回 JSON 元素,用于嵌入主模板
#
# 参数:
#   $1               - 子模板类型
#                      可选值: command-bash, command-file, description
#   ${@:2}           - 变量键值对 (格式: "key=value")
#
# 输出:
#   渲染后的 JSON 元素字符串(已去除首尾空白)
#
# 返回:
#   0 - 渲染成功
#   1 - 渲染失败
#
# 示例:
#   cmd=$(render_sub_template "command-bash" "command=npm install")
#   desc=$(render_sub_template "description" "description=安装依赖")
#
# 支持的子模板类型:
#   - command-bash:   Bash 命令详情 (参数: command)
#   - command-file:   文件操作详情 (参数: file_path)
#   - description:    操作描述 (参数: description)
# ----------------------------------------------------------------------------
render_sub_template() {
    local sub_type="$1"
    shift

    # 确定模板目录
    local template_dir="$(get_config "FEISHU_TEMPLATE_PATH" "$DEFAULT_TEMPLATE_DIR")"

    # 确定模板文件
    local template_file
    case "$sub_type" in
        "command-bash")
            template_file="${template_dir}/command-detail-bash.json"
            ;;
        "command-file")
            template_file="${template_dir}/command-detail-file.json"
            ;;
        "command-edit")
            template_file="${template_dir}/command-detail-edit.json"
            ;;
        "command-write")
            template_file="${template_dir}/command-detail-write.json"
            ;;
        "description")
            template_file="${template_dir}/description-element.json"
            ;;
        "thinking")
            template_file="${template_dir}/thinking-element.json"
            ;;
        "plan-content")
            template_file="${template_dir}/plan-content.json"
            ;;
        *)
            log_error "Unknown sub template type: $sub_type"
            return 1
            ;;
    esac

    # 验证模板文件
    if ! validate_template "$template_file"; then
        log_error "Invalid sub template file: $template_file"
        return 1
    fi

    # 渲染模板
    local rendered
    rendered=$(render_template "$template_file" "$@")

    if [ -z "$rendered" ]; then
        log_error "Sub template rendering failed: $template_file"
        return 1
    fi

    # 去除外层空白并输出
    echo "$rendered" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

# ----------------------------------------------------------------------------
# render_card_template - 渲染卡片模板(包装函数)
# ----------------------------------------------------------------------------
# 功能: 根据卡片类型选择模板并渲染
#
# 参数:
#   $1               - 卡片类型
#                      可选值: permission, permission-static, notification, buttons
#   ${@:2}           - 变量键值对 (格式: "key=value")
#
# 输出:
#   渲染后的卡片 JSON 字符串
#
# 返回:
#   0 - 渲染成功
#   1 - 渲染失败
#
# 示例:
#   card=$(render_card_template "permission" \
#       "tool_name=Bash" "project_name=test")
#
# 支持的卡片类型:
#   - permission:         权限请求卡片(带按钮)
#   - permission-static:  权限请求卡片(无按钮)
#   - notification:       通用通知卡片
#   - buttons:            交互按钮数组
# ----------------------------------------------------------------------------
render_card_template() {
    local card_type="$1"
    shift

    # 确定模板目录
    local template_dir="$(get_config "FEISHU_TEMPLATE_PATH" "$DEFAULT_TEMPLATE_DIR")"

    # 确定模板文件
    local template_file
    case "$card_type" in
        "permission")
            template_file="${template_dir}/permission-card.json"
            ;;
        "permission-static")
            template_file="${template_dir}/permission-card-static.json"
            ;;
        "notification")
            template_file="${template_dir}/notification-card.json"
            ;;
        "stop")
            template_file="${template_dir}/stop-card.json"
            ;;
        "buttons")
            template_file="${template_dir}/buttons.json"
            ;;
        "buttons-openapi")
            template_file="${template_dir}/buttons-openapi.json"
            ;;
        "ask-question-card")
            template_file="${template_dir}/ask-question-card.json"
            ;;
        *)
            log_error "Unknown card type: $card_type"
            return 1
            ;;
    esac

    # 验证模板文件
    if ! validate_template "$template_file"; then
        log_error "Invalid template file: $template_file"
        return 1
    fi

    # 渲染模板
    local rendered
    rendered=$(render_template "$template_file" "$@")

    if [ -z "$rendered" ]; then
        log_error "Template rendering failed: $template_file"
        return 1
    fi

    # 验证渲染后的 JSON (如果系统有 jq)
    if [ "$JSON_HAS_JQ" = "true" ]; then
        if ! echo "$rendered" | jq empty >/dev/null 2>&1; then
            log_error "Rendered JSON is invalid"
            return 1
        fi
    fi

    echo "$rendered"
}

# =============================================================================
# 辅助函数
# =============================================================================

# ----------------------------------------------------------------------------
# _get_notify_config - 读取运行时通知配置覆盖
# ----------------------------------------------------------------------------
# 功能: 从 runtime/notify_config.json 读取配置
# 输出: JSON 字符串，文件不存在时输出空
# ----------------------------------------------------------------------------
_get_notify_config() {
    local config_file="${RUNTIME_DIR}/notify_config.json"
    if [ ! -f "$config_file" ]; then
        return 0
    fi
    cat "$config_file" 2>/dev/null
}

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
# _build_at_user_tag - 构建 @ 用户标签
# ----------------------------------------------------------------------------
# 功能: 根据运行时通知配置 + 可选 sender_id 构建飞书 @ 用户标签
#
# 参数:
#   $1 - sender_id (可选): 本次 prompt 发送者 user_id，由 hook-router.sh 注入
#
# 输出:
#   飞书 @ 用户标签字符串（含尾部空格），或空字符串
#
# 优先级:
#   1. /notify at off → 空（全局关闭）
#   2. 不在 at 时段内 → 空（全局关闭）
#   3. 传入的 sender_id 非空 → at sender（本次提问的人；协作模式下定位提问者）
#   4. /notify at self/all/<user_id> → 按 config
#   5. 默认 → @ FEISHU_OWNER_ID
# ----------------------------------------------------------------------------
_build_at_user_tag() {
    local sender_id="${1:-}"
    local notify_config_json at_user_config at_start at_end

    # 读取运行时通知配置覆盖
    notify_config_json=$(_get_notify_config)
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
        echo "<at id=${sender_id}></at> "
        return 0
    fi

    # 优先级 4: config self/all/<user_id>
    if [ "$at_user_config" = "self" ]; then
        local owner_id
        owner_id=$(get_config "FEISHU_OWNER_ID" "")
        if [ -n "$owner_id" ]; then
            echo "<at id=${owner_id}></at> "
        else
            echo ""
        fi
        return 0
    fi
    if [ -n "$at_user_config" ]; then
        # all 或 user_id
        echo "<at id=${at_user_config}></at> "
        return 0
    fi

    # 优先级 5: 默认 @ FEISHU_OWNER_ID
    local owner_id
    owner_id=$(get_config "FEISHU_OWNER_ID" "")
    if [ -n "$owner_id" ]; then
        echo "<at id=${owner_id}></at> "
    else
        echo ""
    fi
}

# =============================================================================
# 卡片构建函数
# =============================================================================

_agent_display_name() {
    case "${AGENT_TYPE:-claude}" in
        claude) echo "Claude Code" ;;
        codex) echo "Codex" ;;
        *) echo "${AGENT_TYPE}" ;;
    esac
}

_agent_resume_command() {
    local session_id="$1"
    case "${AGENT_TYPE:-claude}" in
        claude) echo "claude --resume $session_id" ;;
        codex) echo "codex resume $session_id" ;;
        *) echo "${AGENT_TYPE} --resume $session_id" ;;
    esac
}

# ----------------------------------------------------------------------------
# build_permission_card - 构建权限请求卡片
# ----------------------------------------------------------------------------
# 功能: 构建飞书权限请求卡片,支持不同的工具类型
#
# 参数:
#   $1 - tool_name       工具名称 (Bash, Edit, Write, Read 等)
#   $2 - project_name    项目名称
#   $3 - timestamp       时间戳 (格式: 2024-01-01 12:00:00)
#   $4 - command_arg     命令参数
#                          Bash: 命令内容
#                          Edit/Write/Read: 文件路径
#   $5 - description     描述内容 (可选, Markdown 格式)
#   $6 - template_color  卡片颜色
#                          可选: orange, yellow, blue, purple, grey
#   $7 - buttons_json    按钮 JSON 数组 (可选, 不提供则使用静态模板)
#   $8 - session_id      会话标识 (可选)
#
# 输出:
#   飞书卡片 JSON 字符串
#
# 返回:
#   0 - 构建成功
#   1 - 构建失败
#
# 示例:
#   # Bash 工具 (带按钮)
#   buttons=$(build_permission_buttons "http://localhost:8080" "req-123" "$owner_id")
#   card=$(build_permission_card "Bash" "myproject" "2024-01-01 12:00:00" \
#       "npm install" "安装依赖" "orange" "$buttons" "abc12345")
#
#   # Edit 工具 (带按钮)
#   card=$(build_permission_card "Edit" "myproject" "2024-01-01 12:00:00" \
#       "/path/to/file.txt" "修复bug" "orange" "$buttons" "abc12345")
#
#   # 不带按钮 (静态卡片)
#   card=$(build_permission_card "Bash" "myproject" "2024-01-01 12:00:00" \
#       "npm install" "安装依赖" "orange" "" "abc12345")
#
# 工具类型映射:
#   - Bash:    使用 command-bash 子模板
#   - Edit:    使用 command-file 子模板
#   - Write:   使用 command-file 子模板
#   - Read:    使用 command-file 子模板
#   - 其他:    默认使用 command-bash 子模板
# ----------------------------------------------------------------------------
build_permission_card() {
    local tool_name="$1"
    local project_name="$2"
    local timestamp="$3"
    local command_arg="$4"
    local description="$5"
    local template_color="$6"
    local buttons_json="${7:-}"
    local session_id="${8:-unknown}"
    local footer_hint="${9:-}"

    # 选择卡片类型
    local card_type
    if [ -n "$buttons_json" ]; then
        card_type="permission"
    else
        card_type="permission-static"
    fi

    # 根据工具类型渲染命令详情元素
    local command_element=""
    case "$tool_name" in
        "Bash")
            local command_hint=""
            if [ "$EXTRACTED_COMMAND_TRUNCATED" = "1" ]; then
                command_hint="⚠️ 内容过长，已截断"
            fi
            command_element=$(render_sub_template "command-bash" "command=$command_arg" "command_hint=$command_hint")
            ;;
        "Edit")
            if [ -n "$EXTRACTED_DIFF" ]; then
                local edit_file_label="$command_arg"
                if [ "$EXTRACTED_REPLACE_ALL" = "true" ]; then
                    edit_file_label="${command_arg}"$'\n\n'"🔄 全部替换"
                fi
                local diff_old_hint=""
                local diff_new_hint=""
                if [ "$EXTRACTED_DIFF_OLD_TRUNCATED" = "1" ]; then
                    diff_old_hint="⚠️ 内容过长，已截断"
                fi
                if [ "$EXTRACTED_DIFF_NEW_TRUNCATED" = "1" ]; then
                    diff_new_hint="⚠️ 内容过长，已截断"
                fi
                command_element=$(render_sub_template "command-edit" "file_path=$edit_file_label" "diff_old=$EXTRACTED_DIFF_OLD" "diff_new=$EXTRACTED_DIFF_NEW" "diff_old_hint=$diff_old_hint" "diff_new_hint=$diff_new_hint")
            else
                command_element=$(render_sub_template "command-file" "file_path=$command_arg")
            fi
            ;;
        "Write")
            if [ -n "$EXTRACTED_WRITE_CONTENT" ]; then
                local write_content_hint=""
                if [ "$EXTRACTED_WRITE_CONTENT_TRUNCATED" = "1" ]; then
                    write_content_hint="⚠️ 内容过长，已截断"
                fi
                command_element=$(render_sub_template "command-write" "file_path=$command_arg" "write_content=$EXTRACTED_WRITE_CONTENT" "write_content_hint=$write_content_hint")
            else
                command_element=$(render_sub_template "command-file" "file_path=$command_arg")
            fi
            ;;
        "Read")
            command_element=$(render_sub_template "command-file" "file_path=$command_arg")
            ;;
        "ExitPlanMode")
            command_element=$(render_sub_template "plan-content" "plan_content=$command_arg")
            ;;
        *)
            # 未知工具类型,默认使用 Bash 模板
            local default_command_hint=""
            if [ "$EXTRACTED_COMMAND_TRUNCATED" = "1" ]; then
                default_command_hint="⚠️ 内容过长，已截断"
            fi
            command_element=$(render_sub_template "command-bash" "command=$command_arg" "command_hint=$default_command_hint")
            ;;
    esac

    # 渲染描述元素(如果有描述)
    local description_element=""
    if [ -n "$description" ]; then
        description_element=$(render_sub_template "description" "description=$description")
    fi

    # 构建详情元素字符串(用于嵌入到主模板中)
    # 注意：这里必须以逗号结尾，因为模板中 detail_elements 后面还有其他元素
    local detail_elements="      ${command_element},"

    # 添加描述元素
    if [ -n "$description_element" ]; then
        detail_elements="${detail_elements}
      ${description_element},"
    fi

    # 渲染主模板
    local at_user
    at_user=$(_build_at_user_tag)

    # 根据 card_type 设置默认 footer_hint
    local final_footer_hint="$footer_hint"
    if [ -z "$final_footer_hint" ]; then
        if [ -n "$buttons_json" ]; then
            final_footer_hint="请尽快操作以避免 $(_agent_display_name) 超时等待"
        else
            final_footer_hint="回调服务未运行，请返回终端操作"
        fi
    fi

    local resume_command
    resume_command=$(_agent_resume_command "$session_id")

    local card
    if [ -n "$buttons_json" ]; then
        card=$(render_card_template "$card_type" \
            "template_color=$template_color" \
            "tool_name=$tool_name" \
            "project_name=$project_name" \
            "timestamp=$timestamp" \
            "session_id=${session_id:0:8}" \
            "detail_elements=$detail_elements" \
            "buttons_json=$buttons_json" \
            "at_user=$at_user" \
            "footer_hint=$final_footer_hint" \
            "resume_command=$resume_command" \
            "resume_session_id=$session_id")
    else
        card=$(render_card_template "$card_type" \
            "template_color=$template_color" \
            "tool_name=$tool_name" \
            "project_name=$project_name" \
            "timestamp=$timestamp" \
            "session_id=${session_id:0:8}" \
            "detail_elements=$detail_elements" \
            "at_user=$at_user" \
            "footer_hint=$final_footer_hint" \
            "resume_command=$resume_command" \
            "resume_session_id=$session_id")
    fi

    if [ $? -ne 0 ]; then
        return 1
    fi

    echo "$card"
}

# ----------------------------------------------------------------------------
# build_permission_buttons - 构建权限请求按钮
# ----------------------------------------------------------------------------
# 功能: 根据 FEISHU_SEND_MODE 构建飞书权限请求卡片的交互按钮
#
# 参数:
#   $1 - callback_url  回调服务器 URL (用于路由或 webhook 模式)
#   $2 - request_id    请求 ID
#   $3 - owner_id      飞书用户 ID（用于验证操作者身份）
#
# 输出:
#   按钮 JSON 数组字符串
#
# 返回:
#   0 - 构建成功
#   1 - 构建失败
#
# 示例:
#   buttons=$(build_permission_buttons "http://localhost:8080" "req-123" "$owner_id")
#
# 注意:
#   根据 FEISHU_SEND_MODE 选择按钮类型:
#   - webhook: open_url 类型按钮（点击跳转浏览器）
#   - openapi: callback 类型按钮（飞书内直接响应）
#             callback_url 从 BindingStore 获取，不需要在 value 中传递
#   owner_id 用于验证点击按钮的用户是否为本人
# ----------------------------------------------------------------------------
build_permission_buttons() {
    local callback_url="$1"
    local request_id="$2"
    local owner_id="$3"
    local send_mode
    send_mode=$(get_config "FEISHU_SEND_MODE" "webhook")

    # 规范化 callback_url（移除末尾斜杠）
    callback_url=$(echo "$callback_url" | sed 's:/*$::')

    if [ "$send_mode" = "openapi" ]; then
        # OpenAPI 模式：使用 callback 类型按钮
        # callback_url 从 BindingStore 获取，不需要在 value 中传递
        # owner_id 用于验证操作者身份
        render_card_template "buttons-openapi" \
            "request_id=$request_id" \
            "owner_id=$owner_id"
    else
        # Webhook 模式（默认）：使用 open_url 类型按钮
        render_card_template "buttons" \
            "callback_url=$callback_url" \
            "request_id=$request_id"
    fi
}

# ----------------------------------------------------------------------------
# build_notification_card - 构建通用通知卡片
# ----------------------------------------------------------------------------
# 功能: 构建简单的飞书通知卡片(用于非权限请求的通知)
#
# 参数:
#   $1 - title         卡片标题
#   $2 - content       通知内容 (Markdown 格式)
#   $3 - project_name  项目名称
#   $4 - timestamp     时间戳
#
# 输出:
#   飞书卡片 JSON 字符串
#
# 返回:
#   0 - 构建成功
#   1 - 构建失败
#
# 示例:
#   card=$(build_notification_card "Agent 通知" \
#       "**任务暂停，需要人工介入**" "myproject" "2024-01-01 12:00:00")
# ----------------------------------------------------------------------------
build_notification_card() {
    local title="$1"
    local content="$2"
    local project_name="$3"
    local timestamp="$4"

    render_card_template "notification" \
        "title=$title" \
        "content=$content" \
        "project_name=$project_name" \
        "timestamp=$timestamp"
}

# ----------------------------------------------------------------------------
# build_stop_card - 构建 Stop 事件完成卡片
# ----------------------------------------------------------------------------
# 功能: 构建飞书 Stop 事件完成卡片(用于主 Agent 完成响应时的通知)
#
# 参数:
#   $1 - response_elements 响应内容 JSON 片段（多个 markdown 元素，逗号分隔）
#   $2 - project_name      项目名称
#   $3 - timestamp         时间戳
#   $4 - session_id        完整会话标识 (函数内部会截断前 8 字符用于显示)
#   $5 - thinking_content  思考过程内容 (可选, Markdown 格式)
#
# 输出:
#   飞书卡片 JSON 字符串
#
# 返回:
#   0 - 构建成功
#   1 - 构建失败
#
# 示例:
#   elements='{"tag":"markdown","content":"已修复 bug","text_align":"left","text_size":"normal_v2"}'
#   card=$(build_stop_card "$elements" \
#       "myproject" "2024-01-01 12:00:00" "canyon-abc123-xyz" "分析了代码结构...")
# ----------------------------------------------------------------------------
build_stop_card() {
    local response_elements="$1"
    local project_name="$2"
    local timestamp="$3"
    local session_id="${4:-}"
    local thinking_content="${5:-}"

    # 获取 @ 用户配置（stop 卡片优先 at prompt 发送者，由 hook-router.sh 注入）
    local at_user
    at_user=$(_build_at_user_tag "$SENDER_USER_ID")

    # 条件构建 thinking_element
    local thinking_element=""
    if [ -n "$thinking_content" ]; then
        thinking_element=$(render_sub_template "thinking" "thinking_content=$thinking_content")
        # 添加尾逗号，因为模板中 thinking_element 后面还有其他元素
        thinking_element="${thinking_element},"
    fi

    # 截断 session_id 前 8 字符用于显示
    local session_id_short="${session_id:0:8}"
    local resume_command
    resume_command=$(_agent_resume_command "$session_id")

    local agent_display_name
    agent_display_name="$(_agent_display_name)"

    render_card_template "stop" \
        "response_elements=$response_elements" \
        "project_name=$project_name" \
        "timestamp=$timestamp" \
        "session_id=$session_id_short" \
        "at_user=$at_user" \
        "thinking_element=$thinking_element" \
        "resume_command=$resume_command" \
        "resume_session_id=$session_id" \
        "agent_display_name=$agent_display_name"
}

# =============================================================================
# 消息发送函数
# =============================================================================

# ----------------------------------------------------------------------------
# _get_auth_token - 获取存储的 auth_token
# ----------------------------------------------------------------------------
# 功能: 从 runtime/auth_token.json 读取存储的 auth_token
#
# 输出:
#   echos 返回: auth_token 字符串，不存在则返回空字符串
#
# 说明:
#   用于飞书网关注册后的双向认证
# ----------------------------------------------------------------------------
_get_auth_token() {
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

# ----------------------------------------------------------------------------
# _get_bot_open_id - 获取存储的 bot_open_id
# ----------------------------------------------------------------------------
# 功能: 从 runtime/auth_token.json 读取机器人的 open_id
#
# 输出:
#   echos 返回: bot_open_id 字符串，不存在则返回空字符串
#
# 说明:
#   用于富文本消息中 at 机器人等场景
# ----------------------------------------------------------------------------
_get_bot_open_id() {
    # AUTH_TOKEN_FILE 由 core.sh 定义，指向 runtime/auth_token.json

    if [ ! -f "$AUTH_TOKEN_FILE" ]; then
        echo ""
        return 0
    fi

    local token_data
    token_data=$(cat "$AUTH_TOKEN_FILE" 2>/dev/null)
    if [ -z "$token_data" ]; then
        echo ""
        return 0
    fi

    local bot_id
    bot_id=$(json_get "$token_data" "bot_open_id")
    if [ "$bot_id" = "null" ] || [ -z "$bot_id" ]; then
        echo ""
        return 0
    fi
    echo "$bot_id"
}

# ----------------------------------------------------------------------------
# _get_chat_id - 根据 session_id 获取对应的 chat_id
# ----------------------------------------------------------------------------
# 功能: 调用 Callback 后端的 /cb/session/get-chat-id 接口查询 session_id 对应的 chat_id
#
# 参数:
#   $1 - session_id  Agent 会话 ID
#
# 输出:
#   echos 返回: chat_id 字符串，查询失败返回空字符串
#
# 说明:
#   用于确定会话消息发送的目标群聊
#   查询失败时，调用方可使用 FEISHU_CHAT_ID 配置作为兜底
# ----------------------------------------------------------------------------
_get_chat_id() {
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
    response=$(_do_callback_post "/cb/session/get-chat-id" "$request_body")

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
# _ensure_chat - 确保 session 有对应的 chat_id（group 模式下懒创建群聊）
# ----------------------------------------------------------------------------
# 功能: 调用 Callback 后端的 /cb/session/ensure-chat 接口
#       如果 session 已有 chat_id 则直接返回，否则在 group 模式下创建群聊
#
# 参数:
#   $1 - session_id   Agent 会话 ID
#   $2 - project_dir  项目工作目录（用于群聊命名）
#
# 输出:
#   echos 返回: chat_id 字符串，失败返回空字符串
# ----------------------------------------------------------------------------
_ensure_chat() {
    local session_id="$1"
    local project_dir="$2"

    if [ -z "$session_id" ]; then
        echo ""
        return 0
    fi

    local escaped_project_dir
    escaped_project_dir=$(json_escape "$project_dir")

    local response
    local agent_type="${AGENT_TYPE:-claude}"
    response=$(_do_callback_post "/cb/session/ensure-chat" \
        "{\"session_id\":\"$session_id\",\"agent_type\":\"$agent_type\",\"project_dir\":\"$escaped_project_dir\"}")

    local http_code
    http_code=$(echo "$response" | head -n 1)
    response=$(echo "$response" | sed '1d')

    if [ "$http_code" != "200" ]; then
        echo ""
        return 0
    fi

    local chat_id
    chat_id=$(json_get "$response" "chat_id")
    chat_id=$(echo "$chat_id" | sed 's/^"//;s/"$//')

    if [ -n "$chat_id" ] && [ "$chat_id" != "null" ] && [ "$chat_id" != "''" ]; then
        echo "$chat_id"
    else
        echo ""
    fi
}

# ----------------------------------------------------------------------------
# _capture_session_env - 把启动 agent 时的白名单 env 快照上报给 callback
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
_capture_session_env() {
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
    response=$(_do_callback_post "/cb/session/set-env" "$request_body")

    http_code=$(echo "$response" | head -n 1)
    if [ "$http_code" != "200" ]; then
        log "capture_session_env: backend returned $http_code (non-fatal)"
    fi
    return 0
}

# ----------------------------------------------------------------------------
# _resolve_chat_id - 解析 session 对应的 chat_id
# ----------------------------------------------------------------------------
# 功能: 按优先级确定消息发送的目标 chat_id
#       1. 通过 session_id 查询已有的 chat_id
#       2. 调用 ensure-chat（group 模式下由 backend 懒创建群聊）
#       3. 使用配置的 FEISHU_CHAT_ID 兜底
#
# 参数:
#   $1 - session_id   Agent 会话 ID（可选）
#   $2 - project_dir  项目工作目录（创建群聊时用于命名）
#
# 输出:
#   echos 返回: chat_id 字符串，无法确定时返回空字符串
# ----------------------------------------------------------------------------
_resolve_chat_id() {
    local session_id="$1"
    local project_dir="$2"

    local chat_id=""

    # 优先通过 session_id 查询已有的 chat_id
    if [ -n "$session_id" ]; then
        chat_id=$(_get_chat_id "$session_id" "$project_dir")
        if [ "$chat_id" = "$MUTED_SENTINEL" ]; then
            log "Session muted, skipping send: $session_id"
            echo "$MUTED_SENTINEL"
            return 0
        fi
        if [ -n "$chat_id" ]; then
            log "Found chat_id for session: $chat_id"
            echo "$chat_id"
            return 0
        fi
    fi

    # 调用 ensure-chat（group 模式下由 backend 懒创建群聊）
    # 仅 group 模式下调用，非 group 模式跳过避免无用 HTTP 请求
    if [ -n "$session_id" ]; then
        local session_mode
        session_mode=$(get_config "FEISHU_SESSION_MODE" "message")
        if [ "$session_mode" = "group" ]; then
            chat_id=$(_ensure_chat "$session_id" "$project_dir")
            if [ -n "$chat_id" ]; then
                log "Ensured chat for session: $chat_id"
                echo "$chat_id"
                return 0
            fi
        fi
    fi

    # 兜底：使用配置的 FEISHU_CHAT_ID
    chat_id=$(get_config "FEISHU_CHAT_ID" "")
    if [ -n "$chat_id" ]; then
        log "Using configured FEISHU_CHAT_ID: $chat_id"
    fi
    echo "$chat_id"
}

# ----------------------------------------------------------------------------
# _get_last_message_id - 根据 session_id 获取最近一条消息 ID
# ----------------------------------------------------------------------------
# 功能: 调用 Callback 后端的 /cb/session/get-last-message-id 接口查询 session 的最近消息
#
# 参数:
#   $1 - session_id  Agent 会话 ID
#
# 输出:
#   echos 返回: last_message_id 字符串，查询失败返回空字符串
#
# 说明:
#   用于链式回复，后续消息会回复到该消息下
# ----------------------------------------------------------------------------
_get_last_message_id() {
    local session_id="$1"

    if [ -z "$session_id" ]; then
        echo ""
        return 0
    fi

    local response
    response=$(_do_callback_post "/cb/session/get-last-message-id" \
        "{\"session_id\":\"$session_id\"}")

    local http_code
    http_code=$(echo "$response" | head -n 1)
    response=$(echo "$response" | sed '1d')

    if [ "$http_code" != "200" ]; then
        return 0
    fi

    local last_message_id
    last_message_id=$(json_get "$response" "last_message_id")
    # 移除可能的引号
    last_message_id=$(echo "$last_message_id" | sed 's/^"//;s/"$//')

    if [ -n "$last_message_id" ] && [ "$last_message_id" != "null" ] && [ "$last_message_id" != "''" ]; then
        echo "$last_message_id"
    else
        echo ""
    fi
}

# ----------------------------------------------------------------------------
# _check_skip_user_prompt - 检查是否跳过 UserPromptSubmit
# ----------------------------------------------------------------------------
# 功能: 查询 Callback 后端，判断本次 prompt 是否由飞书发起（需要跳过）
#
# 参数:
#   $1 - session_id  Agent 会话 ID
#
# 返回:
#   0 + stdout "true"  - 应跳过
#   0 + stdout "false" - 不跳过
# ----------------------------------------------------------------------------
_check_skip_user_prompt() {
    local session_id="$1"

    if [ -z "$session_id" ]; then
        echo "false"
        return 0
    fi

    local response
    response=$(_do_callback_post "/cb/session/check-skip-user-prompt" \
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
# _do_callback_post - 向 Callback 后端发送 POST 请求
# ----------------------------------------------------------------------------
# 功能: 封装 callback_url 构造和 auth_token，简化 /cb/* 路由的调用
#
# 参数:
#   $1 - path          路由路径（如 /cb/session/set-env）
#   $2 - request_body  请求 JSON 字符串
#   $3 - log_prefix    日志前缀 (可选，默认取 path 去掉前导 /)
#
# 输出/返回: 同 _do_curl_post
# ----------------------------------------------------------------------------
_do_callback_post() {
    local path="$1"
    local request_body="$2"
    local log_prefix="${3:-${path#/}}"

    local callback_url="${CALLBACK_SERVER_URL:-http://localhost:${CALLBACK_SERVER_PORT:-8080}}"
    callback_url=$(echo "$callback_url" | sed 's:/*$::')

    _do_curl_post "${callback_url}${path}" "$request_body" "$log_prefix" "$(_get_auth_token)"
}

# _do_curl_post - 执行 POST 请求的通用函数
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
#   auth_token 用于飞书网关注册后的双向认证
# ----------------------------------------------------------------------------
_do_curl_post() {
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
    --max-time "$FEISHU_HTTP_TIMEOUT" \
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
        --max-time "$FEISHU_HTTP_TIMEOUT" \
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
# _send_via_webhook - 通用的 Webhook 发送函数
# ----------------------------------------------------------------------------
# 功能: 通过飞书 Webhook URL 发送消息
#
# 参数:
#   $1 - request_body  请求 JSON 字符串
#   $2 - target_url    Webhook URL
#   $3 - log_prefix    日志前缀 (可选，默认 "webhook")
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 输出:
#   失败时输出错误信息到 stdout
# ----------------------------------------------------------------------------
_send_via_webhook() {
    local request_body="$1"
    local target_url="$2"
    local log_prefix="${3:-webhook}"

    if [ -z "$target_url" ]; then
        log_error "${log_prefix}: target_url not set"
        echo "Webhook URL 未配置"
        return 1
    fi

    log "Sending via ${log_prefix}..."

    local http_code response
    response=$(_do_curl_post "$target_url" "$request_body" "$log_prefix")
    local curl_status=$?
    http_code=$(echo "$response" | head -n 1)
    response=$(echo "$response" | sed '1d')

    if [ $curl_status -ne 0 ]; then
        log "${log_prefix} failed: http=$http_code, response=$response"
        # response 可能是 curl 错误信息或飞书返回的 JSON
        if [ -n "$response" ]; then
            # 尝试提取 JSON 中的 msg 字段
            local msg
            msg=$(json_get "$response" "msg" 2>/dev/null)
            if [ -n "$msg" ] && [ "$msg" != "null" ]; then
                echo "飞书返回错误: $msg"
            else
                # 使用原始响应（如 curl 错误信息）
                echo "$response"
            fi
        else
            echo "HTTP 请求失败 (http=$http_code)"
        fi
        return 1
    fi

    # 检查业务 code 字段
    local code
    code=$(json_get "$response" "code")
    if [ "$code" != "0" ] && [ "$code" != '"0"' ]; then
        log "${log_prefix} failed: code=$code, response=$response"
        # 提取飞书返回的 msg 字段作为错误信息
        local msg
        msg=$(json_get "$response" "msg")
        if [ -n "$msg" ] && [ "$msg" != "null" ]; then
            echo "飞书返回错误: $msg"
        else
            echo "飞书返回错误码: $code"
        fi
        return 1
    fi

    log "${log_prefix} succeeded"
    return 0
}

# ----------------------------------------------------------------------------
# _send_via_http_endpoint - 通用的 /gw/feishu/send 发送函数
# ----------------------------------------------------------------------------
# 功能: 通过指定服务器的 /gw/feishu/send 接口发送消息
#
# 参数:
#   $1 - request_body  请求 JSON 字符串
#   $2 - target_url    目标服务器 URL（可选，默认使用 CALLBACK_SERVER_URL）
#   $3 - log_prefix    日志前缀 (可选，默认 "http")
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 输出:
#   失败时输出错误信息到 stdout
#
# 说明:
#   兼容 callback 服务和飞书网关的 /gw/feishu/send 接口
#   会自动从 runtime/auth_token.json 读取并传递 auth_token 进行双向认证
# ----------------------------------------------------------------------------
_send_via_http_endpoint() {
    local request_body="$1"
    local target_url="${2:-}"
    local log_prefix="${3:-http}"

    # 构建目标 URL
    if [ -z "$target_url" ]; then
        target_url="${CALLBACK_SERVER_URL:-http://localhost:${CALLBACK_SERVER_PORT:-8080}}"
    fi
    target_url=$(echo "$target_url" | sed 's:/*$::')
    # ws(s):// → http(s)://（FEISHU_GATEWAY_URL 可能是 ws 协议）
    case "$target_url" in
        wss://*) target_url="https://${target_url#wss://}" ;;
        ws://*)  target_url="http://${target_url#ws://}" ;;
    esac
    local api_url="${target_url}/gw/feishu/send"

    log "Sending via ${log_prefix}: $api_url"

    # 获取 auth_token（用于双向认证）
    local auth_token
    auth_token=$(_get_auth_token)
    if [ -n "$auth_token" ]; then
        log "Using auth_token for authentication"
    fi

    local http_code response
    response=$(_do_curl_post "$api_url" "$request_body" "$log_prefix" "$auth_token")
    local curl_status=$?
    http_code=$(echo "$response" | head -n 1)
    response=$(echo "$response" | sed '1d')

    if [ $curl_status -ne 0 ]; then
        log "${log_prefix} failed: http=$http_code, response=$response"
        # response 可能是 curl 错误信息或服务端返回的 JSON
        if [ -n "$response" ]; then
            # 尝试提取 JSON 中的 error 字段
            local err_msg
            err_msg=$(json_get "$response" "error" 2>/dev/null)
            if [ -n "$err_msg" ] && [ "$err_msg" != "null" ]; then
                echo "$err_msg"
            else
                # 使用原始响应（如 curl 错误信息）
                echo "$response"
            fi
        else
            echo "HTTP 请求失败 (http=$http_code)"
        fi
        return 1
    fi

    # 检查 success 字段
    local success
    success=$(json_get "$response" "success" | tr '[:upper:]' '[:lower:]')
    if [ "$success" != "true" ] && [ "$success" != "1" ]; then
        log "${log_prefix} failed: success=$success, response=$response"
        # 提取 error 或 message 字段作为错误信息
        local err_msg
        err_msg=$(json_get "$response" "error")
        if [ -z "$err_msg" ] || [ "$err_msg" = "null" ]; then
            err_msg=$(json_get "$response" "message")
        fi
        if [ -n "$err_msg" ] && [ "$err_msg" != "null" ]; then
            echo "$err_msg"
        else
            echo "服务端返回失败 (success=$success)"
        fi
        return 1
    fi

    log "${log_prefix} succeeded"
    return 0
}

# ----------------------------------------------------------------------------
# _record_dir_usage - 记录目录使用（内部函数）
# ----------------------------------------------------------------------------
# 功能: 调用 Callback 后端的 /cb/directory/record-usage 接口记录目录使用次数
#       后台静默执行，失败不阻塞主流程
#
# 参数:
#   $1 - project_dir  项目目录路径
# ----------------------------------------------------------------------------
_record_dir_usage() {
    local project_dir="$1"

    if [ -z "$project_dir" ]; then
        return 0
    fi

    _do_callback_post "/cb/directory/record-usage" \
        "$(json_build_object "project_dir" "$project_dir")" >/dev/null 2>&1 || true
}

# ----------------------------------------------------------------------------
# preprocess_card_markdown - 卡片 Markdown 预处理
# ----------------------------------------------------------------------------
# 递归遍历飞书卡片 JSON，找到所有 {tag:"markdown", content:"..."} 元素，
# 对 content 执行飞书兼容转换（单次子进程，跳过代码块内容）。
#
# 处理项（均跳过代码块）：
#   1. 图片链接转文本   — ![alt](url) → [图片: alt](url)，飞书不支持会报错
#   2. HTML 标签剥离   — 删除标签，保留标签间的文本内容
#   3. 脚注定义展平    — 脚注定义行转为可见文本
#   4. 标题降级（可选） — # 标题 → 加粗/emoji 格式，由 heading_style 控制
#
# HTML 标签剥离场景：
#   ┌─────────────────────────┬──────────────────┬──────────────────┐
#   │ HTML 标签               │ 转换结果         │ 说明             │
#   ├─────────────────────────┼──────────────────┼──────────────────┤
#   │ <summary>text</summary> │ **text**         │ 保留文本，转加粗 │
#   │ <details>…</details>    │ (删除标签)       │ 保留内部内容     │
#   │ <div>…</div>            │ (删除标签)       │ 保留内部内容     │
#   │ <span>…</span>          │ (删除标签)       │ 保留内部内容     │
#   │ <p>…</p>                │ (删除标签)       │ 保留内部内容     │
#   │ <br>                    │ (不处理)         │ 飞书卡片支持渲染 │
#   │ <hr>                    │ (不处理)         │ 飞书卡片支持渲染 │
#   └─────────────────────────┴──────────────────┴──────────────────┘
#
# 脚注定义替换：
#   [^id]: content → **注 id**: content（直接替换，飞书会吞掉原始脚注定义行）
#   [^id] 引用处不处理（保留原样，由飞书自行渲染）
#
# 标题降级预设（H1 统一使用 **【标题】** 格式）：
#   bar      — H2~H6: ▍ ▎ ▏ ▏▏ ▏▏▏（默认）
#   circle   — H2~H6: 🔵 🔘 ● ○ ◦
#   diamond  — H2~H6: 🔷 🔹 ◆ ◇ ◦
#   original — 不做标题降级
#
# 参数:
#   $1 - card_json      飞书卡片 JSON 字符串
#   $2 - heading_style  标题降级样式（bar/circle/diamond/original，默认 bar）
# 输出：处理后的 card_json
# ----------------------------------------------------------------------------
preprocess_card_markdown() {
    local card_json="$1"
    local heading_style="${2:-bar}"

    if [ "$JSON_HAS_JQ" = "true" ]; then
        echo "$card_json" | jq --arg heading_style "$heading_style" '
            # 标题降级样式表
            def heading_styles:
                if $heading_style == "circle" then
                    [["**【","】**"],["🔵 **","**"],["🔘 **","**"],["● **","**"],["○ **","**"],["◦ **","**"]]
                elif $heading_style == "diamond" then
                    [["**【","】**"],["🔷 **","**"],["🔹 **","**"],["◆ **","**"],["◇ **","**"],["◦ **","**"]]
                elif $heading_style == "bar" then
                    [["**【","】**"],["**▍","**"],["**▎","**"],["**▏","**"],["**▏▏","**"],["**▏▏▏","**"]]
                else null end;

            # 行级转换（图片 → HTML → 脚注 → 标题）
            def transform_line($styles):
                # 图片链接转文本
                gsub("!\\[(?<alt>[^\\]]*)\\]\\((?<url>[^)]+)\\)";
                    if .alt != "" then "[图片: " + .alt + "](" + .url + ")"
                    else "[图片](" + .url + ")" end)
                # HTML: summary 转加粗，其余标签删除
                | gsub("<summary>(?<s>[^<]*)</summary>"; "**\(.s)**")
                | gsub("</?(?:details|div|span|p)[^>]*>"; "")
                # 脚注定义替换 与 标题降级 互斥（test 前置守卫确保 capture 必定匹配）
                | if test("^\\s*\\[\\^(?:[^\\]]+)\\]:\\s.+$") then
                    capture("^(?<indent>\\s*)\\[\\^(?<id>[^\\]]+)\\]:\\s(?<content>.+)$") as $m |
                    "\($m.indent)**注 \($m.id)**: \($m.content)"
                  elif $styles != null and test("^#{1,6}\\s+.+$") then
                    capture("^(?<hashes>#{1,6})\\s+(?<title>.+)$") as $m |
                    (($m.hashes | length) - 1) as $idx |
                    $styles[$idx] as $pair |
                    $pair[0] + $m.title + $pair[1]
                  else . end;

            # 对单个 markdown 文本做行级预处理（跳过代码块）
            def preprocess:
                heading_styles as $styles |
                split("\n") | reduce .[] as $line (
                    {in_code: false, lines: []};
                    if ($line | gsub("^[ \\t]+"; "") | startswith("```")) then
                        .in_code = (.in_code | not) | .lines += [$line]
                    elif .in_code then
                        .lines += [$line]
                    else
                        .lines += [$line | transform_line($styles)]
                    end
                ) | .lines | join("\n");

            # 递归遍历卡片 JSON，处理所有 {tag:"markdown"} 元素的 content
            def walk_md:
                if type == "array" then map(walk_md)
                elif type == "object" then
                    if .tag == "markdown" and .content then
                        .content |= preprocess
                    else to_entries | map(.value |= walk_md) | from_entries end
                else . end;

            walk_md
        ' 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        echo "$card_json" | "$PYTHON3" -c "
import sys, json, re

data = json.load(sys.stdin)
heading_style = sys.argv[1] if len(sys.argv) > 1 else 'bar'
fence = chr(96) * 3  # 即 3 个反引号，代码围栏标记

# ── 正则（预编译） ──────────────────────────────────────────
img_re = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')               # 图片链接
summary_re = re.compile(r'<summary>([^<]*)</summary>')         # HTML summary
html_re = re.compile(r'</?(?:details|div|span|p)[^>]*>')       # HTML 标签
footnote_def_re = re.compile(r'^(\s*)\[\^([^\]]+)\]:\s(.+)$')  # 脚注定义
heading_re = re.compile(r'^(#{1,6})\s+(.+)$')                  # Markdown 标题

# ── 标题降级样式表 ──────────────────────────────────────────
HEADING_STYLES = {
    'circle': [
        ('**\u3010', '\u3011**'),
        ('\U0001f535 **', '**'), ('\U0001f518 **', '**'),
        ('\u25cf **', '**'), ('\u25cb **', '**'), ('\u25e6 **', '**'),
    ],
    'diamond': [
        ('**\u3010', '\u3011**'),
        ('\U0001f537 **', '**'), ('\U0001f539 **', '**'),
        ('\u25c6 **', '**'), ('\u25c7 **', '**'), ('\u25e6 **', '**'),
    ],
    'bar': [
        ('**\u3010', '\u3011**'),
        ('**\u258d', '**'), ('**\u258e', '**'), ('**\u258f', '**'),
        ('**\u258f\u258f', '**'), ('**\u258f\u258f\u258f', '**'),
    ],
}
h_formats = HEADING_STYLES.get(heading_style)

# ── 行级转换 ────────────────────────────────────────────────
def transform_line(line):
    line = img_re.sub(
        lambda m: '[\u56fe\u7247: %s](%s)' % (m.group(1), m.group(2))
                  if m.group(1) else '[\u56fe\u7247](%s)' % m.group(2), line)
    line = summary_re.sub(r'**\1**', line)
    line = html_re.sub('', line)
    m = footnote_def_re.match(line)
    if m:
        return '%s**\u6ce8 %s**: %s' % (m.group(1), m.group(2), m.group(3))
    if h_formats:
        m = heading_re.match(line)
        if m:
            pre, suf = h_formats[len(m.group(1)) - 1]
            line = '%s%s%s' % (pre, m.group(2), suf)
    return line

# ── 文本级处理（代码块保护 → 行级转换） ────────────────────
def process_text(text):
    lines = text.split('\n')
    result = []
    in_code = False
    for line in lines:
        if line.lstrip().startswith(fence):
            in_code = not in_code
            result.append(line)
        elif in_code:
            result.append(line)
        else:
            result.append(transform_line(line))
    return '\n'.join(result)

# ── 递归遍历卡片 JSON，处理所有 markdown 元素 ──────────────
def walk_md(node):
    if isinstance(node, list):
        return [walk_md(x) for x in node]
    if isinstance(node, dict):
        if node.get('tag') == 'markdown' and 'content' in node:
            node['content'] = process_text(node['content'])
            return node
        return {k: walk_md(v) for k, v in node.items()}
    return node

json.dump(walk_md(data), sys.stdout, ensure_ascii=False)
" "$heading_style" 2>/dev/null
    else
        echo "$card_json"
    fi
}

# ----------------------------------------------------------------------------
# send_feishu_card - 发送飞书卡片
# ----------------------------------------------------------------------------
# 功能: 根据 FEISHU_SEND_MODE 发送飞书卡片消息
#
# 参数:
#   $1 - card_json  飞书卡片 JSON 字符串
#   $2 - options    可选参数 JSON 字符串 (可选)
#                  支持的字段:
#                    - webhook_url  Webhook URL (仅 webhook 模式使用)
#                    - session_id   会话标识 (用于继续会话)
#                    - project_dir  项目目录 (用于继续会话)
#                    - callback_url 回调地址 (用于继续会话)
#                    - chat_id      群聊 ID (openapi 模式优先使用，未传则按 session_id 解析)
#                    - reply_to     显式指定回复目标 message_id (openapi 模式；空则 fallback 查 last_message_id)
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 示例:
#   # 简单调用
#   send_feishu_card "$card"
#
#   # 带完整参数
#   send_feishu_card "$card" '{"webhook_url":"xxx","session_id":"yyy","project_dir":"zzz","callback_url":"aaa"}'
#
#   # 仅传需要的参数
#   send_feishu_card "$card" '{"session_id":"yyy","project_dir":"zzz"}'
#
# 发送模式:
#   - webhook: 直接发送到 FEISHU_WEBHOOK_URL
#   - openapi: 通过 {FEISHU_GATEWAY_URL:-$CALLBACK_SERVER_URL}/gw/feishu/send 发送
#
# 说明:
#   - openapi 模式下 FEISHU_GATEWAY_URL 为空时默认使用 CALLBACK_SERVER_URL
#   - 发送失败时，会发送降级文本消息通知用户（包含错误信息）
# ----------------------------------------------------------------------------
send_feishu_card() {
    local card_json="$1"
    local options="${2:-}"

    # 解析可选参数（一次调用获取多个字段，减少进程开销）
    local -a vals=()
    while IFS= read -r _line; do
        vals+=("$_line")
    done <<< "$(json_get_multi "$options" webhook_url session_id project_dir callback_url chat_id reply_to)"
    local webhook_url="${vals[0]:-}"
    local session_id="${vals[1]:-}"
    local project_dir="${vals[2]:-}"
    local callback_url="${vals[3]:-}"
    local chat_id="${vals[4]:-}"
    local reply_to="${vals[5]:-}"
    [ -z "$webhook_url" ] && webhook_url=$(get_config "FEISHU_WEBHOOK_URL" "")

    # Markdown 预处理：遍历卡片中所有 markdown 元素，转换飞书不支持的语法
    local _heading_style
    _heading_style=$(get_config "FEISHU_HEADING_STYLE" "bar")
    local _preprocessed
    _preprocessed=$(preprocess_card_markdown "$card_json" "$_heading_style")
    if [ -n "$_preprocessed" ]; then
        card_json="$_preprocessed"
    fi

    # 记录卡片内容到日志
    log "Sending feishu card:"
    if [ "$JSON_HAS_JQ" = "true" ]; then
        log_raw "$(echo "$card_json" | jq '.' 2>/dev/null)"
    else
        log_raw "$card_json"
    fi

    local send_mode
    send_mode=$(get_config "FEISHU_SEND_MODE" "webhook")

    local result=1
    local error_msg=""

    if [ "$send_mode" = "openapi" ]; then
        # OpenAPI 模式：通过 /gw/feishu/send 发送
        # 目标：FEISHU_GATEWAY_URL 或 CALLBACK_SERVER_URL
        local gateway_url
        gateway_url=$(get_config "FEISHU_GATEWAY_URL" "")

        local target_url="${gateway_url:-$CALLBACK_SERVER_URL}"

        # 构建传递给 _send_feishu_card_http_endpoint 的 options
        local http_options=""
        if [ -n "$session_id" ] || [ -n "$project_dir" ] || [ -n "$callback_url" ] || [ -n "$chat_id" ] || [ -n "$reply_to" ]; then
            http_options=$(json_build_object "session_id" "$session_id" "project_dir" "$project_dir" "callback_url" "$callback_url" "chat_id" "$chat_id" "reply_to" "$reply_to")
        fi

        error_msg=$(_send_feishu_card_http_endpoint "$card_json" "$target_url" "$http_options")
        result=$?
    else
        # Webhook 模式（默认）
        error_msg=$(_send_feishu_card_webhook "$card_json" "$webhook_url")
        result=$?
    fi

    # 失败时发送降级文本消息（send_feishu_text 会根据模式选择发送方式）
    if [ $result -ne 0 ]; then
        local fallback_title="Agent"
        local extracted_title
        extracted_title=$(json_get "$card_json" "card.header.title.content")
        if [ -n "$extracted_title" ] && [ "$extracted_title" != "null" ]; then
            fallback_title="$extracted_title"
        fi

        # 构建包含错误信息的降级文本
        local fallback_text="⚠️ ${fallback_title} 卡片发送失败，请返回终端查看"
        if [ -n "$error_msg" ]; then
            fallback_text="${fallback_text}（错误: ${error_msg}）"
        fi

        send_feishu_text "$fallback_text"
    fi

    # 发送成功且有 project_dir 时，记录目录使用（后台静默执行）
    if [ $result -eq 0 ] && [ -n "$project_dir" ] && [ -n "$CALLBACK_SERVER_URL" ]; then
        _record_dir_usage "$project_dir" &
    fi

    return $result
}

# ----------------------------------------------------------------------------
# _send_feishu_card_webhook - 通过 Webhook 发送飞书卡片（内部函数）
# ----------------------------------------------------------------------------
# 功能: 通过飞书 Webhook URL 发送卡片消息
#
# 参数:
#   $1 - card_json    飞书卡片 JSON 字符串
#   $2 - webhook_url  Webhook URL
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 输出:
#   失败时输出错误信息到 stdout（透传自 _send_via_webhook）
# ----------------------------------------------------------------------------
_send_feishu_card_webhook() {
    local card_json="$1"
    local webhook_url="$2"
    _send_via_webhook "$card_json" "$webhook_url" "webhook-card"
}

# ----------------------------------------------------------------------------
# _send_feishu_card_http_endpoint - 通过 /gw/feishu/send 发送飞书卡片（内部函数）
# ----------------------------------------------------------------------------
# 功能: 通过 /gw/feishu/send 接口发送卡片（OpenAPI 模式内部使用）
#
# 参数:
#   $1 - card_json  飞书卡片 JSON 字符串
#   $2 - target_url 目标服务器 URL（可选）
#                  - 分离部署: 传入 FEISHU_GATEWAY_URL
#                  - 单机部署: 不传，默认使用 CALLBACK_SERVER_URL
#   $3 - options     可选参数 JSON（可选），可包含：
#                   - session_id   Agent 会话 ID（用于继续会话）
#                   - project_dir  项目工作目录（用于继续会话）
#                   - callback_url Callback 后端 URL（用于继续会话）
#                   - chat_id      群聊 ID（优先使用；未传则按 session_id 解析）
#                   - reply_to     显式回复目标 message_id（优先使用；空则 fallback 查 last_message_id）
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 输出:
#   失败时输出错误信息到 stdout
#
# 说明:
#   自动读取 FEISHU_OWNER_ID 配置并作为 owner_id 传递给服务端
#   session_id/project_dir/callback_url 用于支持回复继续会话功能
#   chat_id 优先使用调用方传入值，未传时按 session_id 自动解析
#   reply_to 优先使用调用方传入值，为空时 fallback 查 last_message_id（终端发起场景）
#   如果有 session_id，会自动查询对应的 chat_id，优先使用 chat_id 发送
# ----------------------------------------------------------------------------
_send_feishu_card_http_endpoint() {
    local card_json="$1"
    local target_url="${2:-}"
    local options="${3:-}"

    # 解析可选参数
    local -a vals=()
    while IFS= read -r _line; do
        vals+=("$_line")
    done <<< "$(json_get_multi "$options" session_id project_dir callback_url chat_id reply_to)"
    local session_id="${vals[0]:-}"
    local project_dir="${vals[1]:-}"
    local callback_url="${vals[2]:-}"
    local chat_id="${vals[3]:-}"
    local reply_to="${vals[4]:-}"

    # 提取 card 内容
    local card_content
    card_content=$(json_get_object "$card_json" "card")

    # 读取 owner_id 配置（作为接收者/备用）
    local owner_id
    owner_id=$(get_config "FEISHU_OWNER_ID" "")

    if [ -z "$owner_id" ]; then
        log "Error: FEISHU_OWNER_ID not configured"
        echo "FEISHU_OWNER_ID 未配置"
        return 1
    fi

    # chat_id 由调用方通过 _resolve_chat_id 预解析后透传
    # 未传入时兜底查询（兼容旧调用方式）
    if [ -z "$chat_id" ]; then
        chat_id=$(_resolve_chat_id "$session_id" "$project_dir")
    fi

    # muted session：跳过发送，直接返回成功
    if [ "$chat_id" = "$MUTED_SENTINEL" ]; then
        log "Session muted, skipping card send: $session_id"
        return 0
    fi

    # reply_to 为空时 fallback 到 last_message_id（终端发起场景）
    local reply_to_message_id="$reply_to"
    if [ -z "$reply_to_message_id" ] && [ -n "$session_id" ]; then
        reply_to_message_id=$(_get_last_message_id "$session_id")
        if [ -n "$reply_to_message_id" ]; then
            log "Fallback to last_message_id for session: $reply_to_message_id"
        fi
    fi

    # 构建请求体
    local request_body
    local extra_fields=""

    # 构建额外字段
    if [ -n "$session_id" ] && [ -n "$project_dir" ] && [ -n "$callback_url" ]; then
        local escaped_project_dir=$(json_escape "$project_dir")
        local escaped_callback_url=$(json_escape "$callback_url")
        extra_fields="\"session_id\":\"$session_id\",\"project_dir\":\"$escaped_project_dir\",\"callback_url\":\"$escaped_callback_url\","
    fi

    if [ -n "$reply_to_message_id" ]; then
        extra_fields="${extra_fields}\"reply_to_message_id\":\"$reply_to_message_id\","
    fi

    # 移除末尾的逗号（如果有）
    extra_fields="${extra_fields%,}"

    # 组装请求体
    if [ -n "$extra_fields" ]; then
        request_body="{\"msg_type\":\"interactive\",\"content\":$card_content,\"owner_id\":\"$owner_id\",\"chat_id\":\"$chat_id\",$extra_fields}"
    else
        request_body="{\"msg_type\":\"interactive\",\"content\":$card_content,\"owner_id\":\"$owner_id\",\"chat_id\":\"$chat_id\"}"
    fi

    _send_via_http_endpoint "$request_body" "$target_url" "openapi-card"
}

# ----------------------------------------------------------------------------
# send_feishu_text - 发送飞书文本消息
# ----------------------------------------------------------------------------
# 功能: 发送飞书文本消息，根据配置自动选择发送方式
#
# 参数:
#   $1 - message_text  文本消息内容
#   $2 - webhook_url  Webhook URL (可选, 仅 webhook 模式使用)
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 示例:
#   send_feishu_text "操作失败，请返回终端"
#
# 发送模式:
#   - webhook: 直接发送到 FEISHU_WEBHOOK_URL
#   - openapi: 通过 {FEISHU_GATEWAY_URL:-$CALLBACK_SERVER_URL}/gw/feishu/send 发送
#
# 说明:
#   - openapi 模式自动读取 FEISHU_OWNER_ID 配置
# ----------------------------------------------------------------------------
send_feishu_text() {
    local message_text="$1"
    local webhook_url="${2:-$(get_config "FEISHU_WEBHOOK_URL" "")}"

    local send_mode
    send_mode=$(get_config "FEISHU_SEND_MODE" "webhook")

    if [ "$send_mode" = "openapi" ]; then
        # OpenAPI 模式：通过 /gw/feishu/send 发送
        local gateway_url
        gateway_url=$(get_config "FEISHU_GATEWAY_URL" "")
        local target_url="${gateway_url:-$CALLBACK_SERVER_URL}"

        # 读取 owner_id 配置（作为接收者）
        local owner_id
        owner_id=$(get_config "FEISHU_OWNER_ID" "")

        if [ -z "$owner_id" ]; then
            log "Error: FEISHU_OWNER_ID not configured"
            return 1
        fi

        # 构建 JSON 转义后的文本
        local escaped_text
        escaped_text=$(echo "$message_text" | sed 's/\\/\\\\/g; s/"/\\"/g')

        # 构建文本消息 JSON
        local text_json
        text_json=$(cat <<-EOF
		{
		  "msg_type": "text",
		  "content": {
		    "text": "$escaped_text"
		  },
		  "owner_id": "$owner_id"
		}
		EOF
        )

        log "Sending text message: ${message_text}"
        _send_via_http_endpoint "$text_json" "$target_url" "openapi-text"
    else
        # Webhook 模式（默认）
        _send_via_webhook "$message_text" "$webhook_url" "webhook-text"
    fi
}

# ----------------------------------------------------------------------------
# send_feishu_post - 发送飞书富文本消息（支持 at、链式回复）
# ----------------------------------------------------------------------------
# 功能: 发送富文本(post)消息到飞书，支持 session threading 和 at 机器人
#
# 参数:
#   $1 - message_text  文本消息内容
#   $2 - options       可选参数 JSON，可包含：
#                      - project_dir    项目工作目录
#                      - session_id     Agent 会话 ID（用于链式回复）
#                      - callback_url   Callback 服务地址（可选，透传给网关）
#
# 返回:
#   0 - 发送成功
#   1 - 发送失败
#
# 示例:
#   send_feishu_post "用户消息" '{"project_dir":"/path","session_id":"abc","callback_url":"http://..."}'
#
# 说明:
#   - 仅 openapi 模式可用（需要 FEISHU_OWNER_ID 配置）
#   - 传入 session_id 时自动查询 chat_id 和 last_message_id 实现链式回复
#   - 默认 at 机器人（与 /notify at 的 @ 通知配置无关，此处是 @ bot 自身）
#   - 非 openapi 模式自动降级为 send_feishu_text 纯文本发送
# ----------------------------------------------------------------------------
send_feishu_post() {
    local message_text="$1"
    local options="${2:-}"

    # 解析可选参数
    local -a vals=()
    while IFS= read -r _line; do
        vals+=("$_line")
    done <<< "$(json_get_multi "$options" project_dir session_id callback_url chat_id)"
    local project_dir="${vals[0]:-}"
    local session_id="${vals[1]:-}"
    local callback_url="${vals[2]:-}"
    local chat_id="${vals[3]:-}"

    local send_mode
    send_mode=$(get_config "FEISHU_SEND_MODE" "webhook")

    if [ "$send_mode" != "openapi" ]; then
        # 非 openapi 模式降级为纯文本
        log "send_feishu_post: not in openapi mode, falling back to text"
        send_feishu_text "$message_text"
        return $?
    fi

    # OpenAPI 模式：通过 /gw/feishu/send 发送富文本消息
    local gateway_url
    gateway_url=$(get_config "FEISHU_GATEWAY_URL" "")
    local target_url="${gateway_url:-$CALLBACK_SERVER_URL}"

    # 读取 owner_id 配置（作为接收者）
    local owner_id
    owner_id=$(get_config "FEISHU_OWNER_ID" "")

    if [ -z "$owner_id" ]; then
        log "Error: FEISHU_OWNER_ID not configured"
        return 1
    fi

    # chat_id 由调用方通过 _resolve_chat_id 预解析后透传
    # 未传入时兜底查询（兼容旧调用方式）
    if [ -z "$chat_id" ]; then
        chat_id=$(_resolve_chat_id "$session_id" "$project_dir")
    fi

    # muted session：跳过发送，直接返回成功
    if [ "$chat_id" = "$MUTED_SENTINEL" ]; then
        log "Session muted, skipping post send: $session_id"
        return 0
    fi

    # 查询 last_message_id 用于链式回复
    local reply_to_message_id=""
    if [ -n "$session_id" ]; then
        reply_to_message_id=$(_get_last_message_id "$session_id")
        if [ -n "$reply_to_message_id" ]; then
            log "Found last_message_id for session: $reply_to_message_id"
        fi
    fi

    # 默认 at 机器人
    local at_user_id
    at_user_id=$(_get_bot_open_id)

    # 使用 Python 构建富文本消息 JSON（含 at 标签）
    # 输出结构示例:
    # {
    #   "msg_type": "post",
    #   "content": {
    #     "zh_cn": {
    #       "content": [[
    #         {"tag": "at", "user_id": "ou_xxx"},
    #         {"tag": "text", "text": " "},
    #         {"tag": "text", "text": "消息内容"}
    #       ]]
    #     }
    #   },
    #   "owner_id": "ou_xxx",
    #   "chat_id": "oc_xxx",           // 可选
    #   "session_id": "abc",            // 可选
    #   "project_dir": "/path",         // 可选
    #   "reply_to_message_id": "om_xxx", // 可选
    #   "add_typing": true               // 发送成功后添加 Typing 表情
    # }
    local request_json
    request_json=$(MESSAGE_TEXT="$message_text" \
        OWNER_ID="$owner_id" \
        CHAT_ID="$chat_id" \
        SESSION_ID="$session_id" \
        PROJECT_DIR="$project_dir" \
        REPLY_TO="$reply_to_message_id" \
        AT_USER_ID="$at_user_id" \
        "$PYTHON3" -c '
import json, os

text = os.environ.get("MESSAGE_TEXT", "")
owner_id = os.environ.get("OWNER_ID", "")
chat_id = os.environ.get("CHAT_ID", "")
session_id = os.environ.get("SESSION_ID", "")
project_dir = os.environ.get("PROJECT_DIR", "")
reply_to = os.environ.get("REPLY_TO", "")
at_user_id = os.environ.get("AT_USER_ID", "")

# 构建富文本 content 行
content_line = []
if at_user_id:
    content_line.append({"tag": "at", "user_id": at_user_id})
    content_line.append({"tag": "text", "text": " "})
content_line.append({"tag": "text", "text": text})

post_content = {"zh_cn": {"content": [content_line]}}

req = {
    "msg_type": "post",
    "content": post_content,
    "owner_id": owner_id,
}
if chat_id:
    req["chat_id"] = chat_id
if session_id:
    req["session_id"] = session_id
if project_dir:
    req["project_dir"] = project_dir
if reply_to:
    req["reply_to_message_id"] = reply_to
req["add_typing"] = True

print(json.dumps(req, ensure_ascii=False))
' 2>/dev/null)

    if [ -z "$request_json" ]; then
        log "Error: Failed to build post message JSON"
        return 1
    fi

    log "Sending post message (session=${session_id:-none}, reply_to=${reply_to_message_id:-none}): ${message_text:0:50}"
    _send_via_http_endpoint "$request_json" "$target_url" "openapi-post"
}

# =============================================================================
# AskUserQuestion 卡片构建
# =============================================================================

# ----------------------------------------------------------------------------
# build_ask_question_card - 构建 AskUserQuestion 表单卡片
# ----------------------------------------------------------------------------
# 功能: 根据 questions 数组动态构建飞书表单卡片
#
# 参数:
#   $1 - questions_json   questions 数组 JSON 字符串
#   $2 - project_name     项目名称
#   $3 - timestamp        时间戳
#   $4 - session_id       会话 ID
#   $5 - request_id       请求 ID
#   $6 - owner_id         飞书用户 ID
#
# 输出:
#   卡片 JSON 字符串
#
# 示例:
#   card=$(build_ask_question_card "$questions_json" "myproject" "2024-01-01" "abc123" "req-123" "ou_xxx")
# ----------------------------------------------------------------------------
build_ask_question_card() {
    local questions_json="$1"
    local project_name="$2"
    local timestamp="$3"
    local session_id="$4"
    local request_id="$5"
    local owner_id="$6"

    # 使用 Python 动态构建表单元素（通过环境变量传递 JSON，避免 stdin 和 heredoc 冲突）
    local form_elements
    form_elements=$(QUESTIONS_JSON="$questions_json" "$PYTHON3" << 'PYTHON_SCRIPT'
import json
import sys
import os

try:
    questions = json.loads(os.environ.get('QUESTIONS_JSON', '[]'))
except:
    print('[]')
    sys.exit(1)

elements = []

for i, q in enumerate(questions):
    question_text = q.get('question', '')
    header = q.get('header', '')
    options = q.get('options', [])
    multi_select = q.get('multiSelect', False)

    # 1. 问题标题 (序号 + header + 单选/多选标识 + question)
    type_tag = '多选' if multi_select else '单选'
    # 注意: 空 header 时不能用 **1. **（有空格），飞书 Markdown 会直接显示原始文本而非粗体
    header_part = '**{}. {}**（{}）'.format(i + 1, header, type_tag) if header else '**{}.**（{}）'.format(i + 1, type_tag)
    question_content = header_part + '\n' + question_text

    elements.append({
        'tag': 'markdown',
        'content': question_content,
        'text_align': 'left',
        'text_size': 'normal_v2'
    })

    # 2. 下拉选择
    select_options = []
    for opt in options:
        label = opt.get('label', '')
        desc = opt.get('description', '')
        display_text = label
        if desc:
            display_text = '{} - {}'.format(label, desc)
        select_options.append({
            'text': {'tag': 'plain_text', 'content': display_text},
            'value': label
        })

    select_tag = 'multi_select_static' if multi_select else 'select_static'
    select_element = {
        'tag': select_tag,
        'name': 'q_{}_select'.format(i),
        'placeholder': {'tag': 'plain_text', 'content': '选择回答'},
        'width': 'fill',
        'options': select_options
    }

    elements.append(select_element)

    # 3. 自定义输入框
    custom_placeholder = '或者自定义输入（填写后会覆盖本题选项）' if not multi_select else '可在此补充自定义内容'
    elements.append({
        'tag': 'input',
        'name': 'q_{}_custom'.format(i),
        'input_type': 'text',
        'placeholder': {'tag': 'plain_text', 'content': custom_placeholder},
        'width': 'fill',
        'margin': '4px 0px 12px 0px'
    })

    # 4. 分隔线（最后一个问题不加）
    if i < len(questions) - 1:
        elements.append({
            'tag': 'hr',
            'margin': '8px 0px 8px 0px'
        })

# 输出逗号分隔的 JSON 元素（非 JSON 数组），用于模板 {{ask_question_form_elements}} 文本替换
print(','.join(json.dumps(e, ensure_ascii=False) for e in elements))
PYTHON_SCRIPT
)

    if [ -z "$form_elements" ] || [ "$form_elements" = "[]" ]; then
        log "Error: Failed to build form elements"
        return 1
    fi

    # 构建 @ 用户标签
    local at_user
    at_user=$(_build_at_user_tag)

    # 渲染模板
    local card
    card=$(render_card_template "ask-question-card" \
        "project_name=$project_name" \
        "timestamp=$timestamp" \
        "session_id=${session_id:0:8}" \
        "request_id=$request_id" \
        "owner_id=$owner_id" \
        "ask_question_form_elements=$form_elements" \
        "at_user=$at_user" \
        "resume_command=$(_agent_resume_command "$session_id")" \
        "resume_session_id=$session_id")

    echo "$card"
}
