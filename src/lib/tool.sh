#!/bin/bash
# =============================================================================
# src/lib/tool.sh - 工具详情格式化函数库
#
# 提供工具类型配置和详情提取功能
#
# 函数:
#   get_tool_color()        - 获取工具类型对应的飞书卡片颜色
#   extract_tool_detail()   - 从 tool_input 中提取并格式化工具详情
#   get_tool_value()        - 获取工具的主要参数值
#   get_tool_rule()         - 生成权限规则字符串
#
# 配置:
#   优先使用 config/tools.json，如果不存在则使用内置配置
#
# 使用示例:
#   source "$LIB_DIR/tool.sh"
#   color=$(get_tool_color "Bash")
#   detail=$(extract_tool_detail "$INPUT" "Bash")
# =============================================================================

# =============================================================================
# 配置管理：优先使用外部配置，否则使用内置配置
# =============================================================================

# 尝试加载统一配置管理
USE_EXTERNAL_CONFIG=false

if [ -f "${LIB_DIR}/tool-config.sh" ] && [ -f "${CONFIG_DIR}/tools.json" ]; then
    source "${LIB_DIR}/tool-config.sh"
    if tool_config_init 2>/dev/null; then
        USE_EXTERNAL_CONFIG=true
    fi
fi

# 如果外部配置不可用，使用内置 case 函数作为降级
_builtin_tool_color() {
    case "$1" in
        Bash) echo "orange" ;;
        Edit|Write) echo "yellow" ;;
        Read|Glob|Grep) echo "blue" ;;
        AskUserQuestion|ExitPlanMode) echo "blue" ;;
        Skill) echo "green" ;;
        WebSearch|WebFetch|mcp__4_5v_mcp__analyze_image|mcp__web_reader__webReader) echo "purple" ;;
        *) echo "grey" ;;
    esac
}

_builtin_tool_field() {
    case "$1" in
        Bash) echo "command" ;;
        Edit|Write|Read) echo "file_path" ;;
        Glob|Grep) echo "pattern" ;;
        WebSearch) echo "query" ;;
        WebFetch) echo "url" ;;
        AskUserQuestion) echo "questions" ;;
        ExitPlanMode) echo "plan" ;;
        Skill) echo "skill" ;;
        *) echo "" ;;
    esac
}

# 全局变量用于存储提取的详情
EXTRACTED_COMMAND=""
EXTRACTED_DESCRIPTION=""
EXTRACTED_COLOR=""

# =============================================================================
# 格式化 AskUserQuestion 的 questions 数组
# =============================================================================
# 功能：将 AskUserQuestion 的 questions 数组格式化为可读的文本（包含选项）
# 用法：format_ask_user_questions "json_input"
# 输出：格式化的问题列表（实际换行符分隔）
#
# AskUserQuestion JSON 格式:
# {
#   "tool_input": {
#     "questions": [
#       { "question": "问题内容", "header": "标签", "options": [...], "multiSelect": false }
#     ]
#   }
# }
#
# 输出格式:
# 1. 问题内容
#    - 选项A: 描述
#    - 选项B: 描述
# =============================================================================
format_ask_user_questions() {
    local json_input="$1"

    # 检查是否有 jq 或 python3
    if [ "$JSON_HAS_JQ" = "true" ]; then
        # 使用 jq 解析 questions 数组和选项，输出实际换行符
        echo "$json_input" | jq -r '
            .tool_input.questions // [] |
            to_entries |
            map(
                "\(.key + 1). \(.value.question)" +
                (if (.value.options // []) | length > 0 then
                    "\n" + (.value.options | map("   - " + .label + (if .description then ": " + .description else "" end)) | join("\n"))
                else "" end)
            ) |
            join("\n")
        ' 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        # 使用 python3 解析 questions 数组和选项，输出实际换行符
        echo "$json_input" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    questions = data.get("tool_input", {}).get("questions", [])
    lines = []
    for i, q in enumerate(questions):
        line = "{}. {}".format(i+1, q.get("question", ""))
        options = q.get("options", [])
        if options:
            opt_lines = []
            for o in options:
                opt_text = "   - " + o.get("label", "")
                if o.get("description"):
                    opt_text += ": " + o.get("description")
                opt_lines.append(opt_text)
            line += "\n" + "\n".join(opt_lines)
        lines.append(line)
    print("\n".join(lines))
except:
    print("")
' 2>/dev/null
    else
        # 降级：显示原始 JSON
        echo "AskUserQuestion (详情需要 jq 或 python3)"
    fi
}

# =============================================================================
# 格式化 Skill 调用
# =============================================================================
# 功能：将 Skill 的 skill 和 args 格式化为可读的文本
# 用法：format_skill_call "json_input"
# 输出：格式化的 Skill 调用信息
#
# Skill JSON 格式:
# {
#   "tool_input": {
#     "skill": "skill_name",
#     "args": "optional args"
#   }
# }
#
# 输出格式:
# Skill: /skill_name
#   args (如果有参数)
# =============================================================================
format_skill_call() {
    local json_input="$1"

    # 提取 skill 和 args
    local skill_name
    local skill_args

    skill_name=$(json_get "$json_input" "tool_input.skill")
    skill_args=$(json_get "$json_input" "tool_input.args")

    if [ -n "$skill_name" ]; then
        if [ -n "$skill_args" ]; then
            echo "Skill: /${skill_name}"$'\n'"args: ${skill_args}"
        else
            echo "Skill: /${skill_name}"
        fi
    else
        echo "Skill"
    fi
}

# =============================================================================
# 获取工具类型对应的卡片颜色
# =============================================================================
# 功能：根据工具类型返回对应的飞书卡片模板颜色
# 用法：get_tool_color "tool_name"
# 输出：颜色名称（orange, yellow, blue, purple, grey）
# =============================================================================
get_tool_color() {
    local tool_name="$1"

    if [ "$USE_EXTERNAL_CONFIG" = "true" ]; then
        tool_get_color "$tool_name"
    else
        _builtin_tool_color "$tool_name"
    fi
}

# =============================================================================
# 提取并格式化工具详情
# =============================================================================
# 功能：从 PermissionRequest 的 tool_input 中提取工具详情并格式化为飞书 Markdown
# 用法：extract_tool_detail "json_input" "tool_name"
# 输出：设置全局变量 EXTRACTED_COMMAND（命令内容）
#       设置全局变量 EXTRACTED_DESCRIPTION（描述内容）
#       设置全局变量 EXTRACTED_COLOR（卡片颜色）
# =============================================================================
extract_tool_detail() {
    local json_input="$1"
    local tool_name="$2"

    # 获取颜色
    EXTRACTED_COLOR=$(get_tool_color "$tool_name")

    # 获取字段名
    local field_name=""
    if [ "$USE_EXTERNAL_CONFIG" = "true" ]; then
        field_name=$(tool_get_field "$tool_name")
    else
        field_name=$(_builtin_tool_field "$tool_name")
    fi

    # 初始化
    EXTRACTED_COMMAND=""
    EXTRACTED_DESCRIPTION=""

    # 提取描述（通用）
    EXTRACTED_DESCRIPTION=$(json_get "$json_input" "tool_input.description")

    # 使用统一配置或内置逻辑
    if [ "$USE_EXTERNAL_CONFIG" = "true" ] && [ -n "$field_name" ]; then
        # 检查是否是自定义格式化工具（如 AskUserQuestion）
        local custom_format
        custom_format=$(_tool_config_get "$tool_name" "custom_format" 2>/dev/null)

        if [ "$custom_format" = "true" ]; then
            # 使用自定义格式化函数
            case "$tool_name" in
                "AskUserQuestion")
                    local questions_text
                    questions_text=$(format_ask_user_questions "$json_input")
                    if [ -n "$questions_text" ]; then
                        EXTRACTED_COMMAND="AskUserQuestion:"$'\n'"${questions_text}"
                    else
                        EXTRACTED_COMMAND="AskUserQuestion"
                    fi
                    ;;
                "Skill")
                    EXTRACTED_COMMAND=$(format_skill_call "$json_input")
                    ;;
                "ExitPlanMode")
                    local plan_content
                    plan_content=$(json_get "$json_input" "tool_input.plan")
                    if [ -n "$plan_content" ]; then
                        EXTRACTED_COMMAND="$plan_content"
                    else
                        EXTRACTED_COMMAND="ExitPlanMode"
                    fi
                    ;;
                *)
                    # 未知自定义格式，使用工具名
                    EXTRACTED_COMMAND="$tool_name"
                    ;;
            esac
        else
            # 使用外部配置，直接获取命令内容和描述（不使用 tool_format_detail）
            local field_value
            field_value=$(json_get "$json_input" "tool_input.${field_name}")
            field_value="${field_value:-}"

            # 检查是否需要截断
            local limit_length
            limit_length=$(_tool_config_get "$tool_name" "limit_length" 2>/dev/null)

            if [ -n "$limit_length" ] && [ "$limit_length" -gt 0 ] 2>/dev/null && [ ${#field_value} -gt "$limit_length" ]; then
                local suffix
                suffix=$(_tool_config_get "$tool_name" "truncate_suffix" 2>/dev/null)
                field_value="${field_value:0:$limit_length}${suffix}"
            fi

            # Bash 命令：只提取原始值，转义由模板渲染统一处理

            # 获取模板并替换占位符
            # 注意: Bash 5.2+ 中 ${var//pattern/replacement} 的 replacement 部分
            # & 表示匹配文本，\ 为转义字符，需要预先转义
            local template
            template=$(tool_get_detail_template "$tool_name")
            EXTRACTED_COMMAND="$template"
            local _safe_value="${field_value//\\/\\\\}"
            _safe_value="${_safe_value//&/\\&}"
            EXTRACTED_COMMAND="${EXTRACTED_COMMAND//\{${field_name}\}/${_safe_value}}"
            EXTRACTED_COMMAND="${EXTRACTED_COMMAND//\{tool_name\}/${tool_name}}"
        fi
    else
        # 使用内置逻辑（向后兼容）
        # 注意：现在使用子模板，子模板已包含标签，这里只返回纯值
        case "$tool_name" in
            "Bash")
                local command
                command=$(json_get "$json_input" "tool_input.command")
                command="${command:-N/A}"
                # 只提取原始值，转义由模板渲染统一处理
                EXTRACTED_COMMAND="$command"
                ;;
            "Edit"|"Write")
                local file_path
                file_path=$(json_get "$json_input" "tool_input.file_path")
                file_path="${file_path:-N/A}"
                EXTRACTED_COMMAND="$file_path"
                ;;
            "Read")
                local file_path
                file_path=$(json_get "$json_input" "tool_input.file_path")
                file_path="${file_path:-N/A}"
                EXTRACTED_COMMAND="$file_path"
                ;;
            "AskUserQuestion")
                # 格式化 questions 数组为可读文本
                local questions_text
                questions_text=$(format_ask_user_questions "$json_input")
                if [ -n "$questions_text" ]; then
                    EXTRACTED_COMMAND="AskUserQuestion:"$'\n'"${questions_text}"
                else
                    EXTRACTED_COMMAND="AskUserQuestion"
                fi
                ;;
            "Skill")
                EXTRACTED_COMMAND=$(format_skill_call "$json_input")
                ;;
            "ExitPlanMode")
                local plan_content
                plan_content=$(json_get "$json_input" "tool_input.plan")
                if [ -n "$plan_content" ]; then
                    EXTRACTED_COMMAND="$plan_content"
                else
                    EXTRACTED_COMMAND="ExitPlanMode"
                fi
                ;;
            *)
                EXTRACTED_COMMAND="$tool_name"
                ;;
        esac
    fi
}

# =============================================================================
# 获取工具的主要参数值
# =============================================================================
# 功能：获取工具的主要参数（如 Bash 的 command，Edit 的 file_path）
# 用法：get_tool_value "json_input" "tool_name"
# 输出：工具的主要参数值
# =============================================================================
get_tool_value() {
    local json_input="$1"
    local tool_name="$2"
    local field_name=""

    if [ "$USE_EXTERNAL_CONFIG" = "true" ]; then
        field_name=$(tool_get_field "$tool_name")
    else
        field_name=$(_builtin_tool_field "$tool_name")
    fi

    if [ -n "$field_name" ]; then
        json_get "$json_input" "tool_input.${field_name}"
    else
        echo ""
    fi
}

# =============================================================================
# 获取权限规则格式
# =============================================================================
# 功能：根据工具类型生成权限规则字符串
# 用法：get_tool_rule "tool_name" "json_input"
# 输出：权限规则字符串（如 Bash(npm install)）
# =============================================================================
get_tool_rule() {
    local tool_name="$1"
    local json_input="$2"

    if [ "$USE_EXTERNAL_CONFIG" = "true" ]; then
        tool_format_rule "$json_input" "$tool_name"
    else
        # 内置逻辑
        case "$tool_name" in
            "Bash")
                local command
                command=$(json_get "$json_input" "tool_input.command")
                if [ -n "$command" ]; then
                    echo "Bash(${command})"
                else
                    echo "Bash(*)"
                fi
                ;;
            "Edit"|"Write"|"Read")
                local file_path
                file_path=$(json_get "$json_input" "tool_input.file_path")
                if [ -n "$file_path" ]; then
                    echo "${tool_name}(${file_path})"
                else
                    echo "${tool_name}(*)"
                fi
                ;;
            "Skill")
                local skill_name
                skill_name=$(json_get "$json_input" "tool_input.skill")
                if [ -n "$skill_name" ]; then
                    echo "Skill(${skill_name})"
                else
                    echo "Skill(*)"
                fi
                ;;
            *)
                echo "${tool_name}(*)"
                ;;
        esac
    fi
}
