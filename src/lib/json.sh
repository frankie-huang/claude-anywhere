#!/bin/bash
# =============================================================================
# src/lib/json.sh - JSON 解析函数库
#
# 提供统一的 JSON 解析功能，支持多种解析工具（jq > python3 > grep/sed）
#
# 函数:
#   json_init()              - 初始化 JSON 解析器（检测可用工具）
#   json_get()               - 获取 JSON 字段值
#   json_get_multi()         - 批量获取多个 JSON 字段值
#   json_get_object()        - 获取 JSON 对象
#   json_get_array()         - 获取 JSON 数组
#   json_get_array_value()   - 从数组中提取指定条件的值
#   json_has_field()         - 检查字段是否存在
#   json_build_object()      - 构建 JSON 对象（无 jq 时使用）
#
# 全局变量:
#   JSON_PARSER          - 当前使用的解析器（jq/python3/native）
#   JSON_HAS_JQ          - 是否支持 jq（true/false）
#   JSON_HAS_PYTHON3     - 是否支持 python3（true/false）
#
# 使用示例:
#   source src/lib/json.sh
#   json_init
#   tool_name=$(json_get "$json" "tool_name")
#   command=$(json_get "$json" "tool_input.command")
# =============================================================================

# JSON 解析器类型（初始化时设置）
: "${JSON_PARSER:=}"
: "${JSON_HAS_JQ:=false}"
: "${JSON_HAS_PYTHON3:=false}"

# =============================================================================
# 初始化 JSON 解析器
# =============================================================================
# 功能：检测可用的 JSON 解析工具并设置默认解析器
# 用法：json_init
# 输出：设置全局变量 JSON_PARSER, JSON_HAS_JQ, JSON_HAS_PYTHON3
#
# 优先级：jq > python3 > grep/sed
# =============================================================================
json_init() {
    # 检测 jq（最可靠）
    if command -v jq &> /dev/null; then
        JSON_PARSER="jq"
        JSON_HAS_JQ=true
        export JSON_PARSER JSON_HAS_JQ
        return 0
    fi

    # 检测 python3（比 grep/sed 更可靠）
    if [ -n "$PYTHON3" ]; then
        JSON_PARSER="python3"
        JSON_HAS_PYTHON3=true
        export JSON_PARSER JSON_HAS_PYTHON3
        return 0
    fi

    # 降级到 grep/sed（有已知限制）
    JSON_PARSER="native"
    export JSON_PARSER
    return 0
}

# =============================================================================
# 获取 JSON 字段值
# =============================================================================
# 功能：从 JSON 字符串中提取指定字段的值
# 用法：json_get "json_string" "field_path"
# 参数：
#   json_string  - JSON 字符串
#   field_path   - 字段路径（支持嵌套，如 "tool_input.command"）
# 输出：字段值（字符串），如果字段不存在则输出空字符串
#
# 示例：
#   tool_name=$(json_get "$json" "tool_name")
#   command=$(json_get "$json" "tool_input.command")
# =============================================================================
json_get() {
    local json="$1"
    local field_path="$2"

    # 如果未初始化，先初始化
    if [ -z "$JSON_PARSER" ]; then
        json_init
    fi

    # 空值处理
    if [ -z "$json" ] || [ -z "$field_path" ]; then
        return 1
    fi

    case "$JSON_PARSER" in
        jq)
            # jq 解析（最可靠）
            echo "$json" | jq -r ".$field_path // empty" 2>/dev/null
            ;;
        python3)
            # Python3 解析（安全处理中间键不存在）
            echo "$json" | "$PYTHON3" -c "
import sys, json
d = json.load(sys.stdin)
parts = sys.argv[1].split('.')
result = d
try:
    for part in parts:
        if isinstance(result, dict) and part in result:
            result = result[part]
        else:
            result = ''
            break
    if result is None:
        result = ''
    elif isinstance(result, bool):
        result = 'true' if result else 'false'
    print(str(result))
except Exception:
    print('')
" "$field_path" 2>/dev/null
            ;;
        native)
            # grep/sed 解析（有已知限制）
            # 简单实现：仅支持单层字段
            if [[ "$field_path" == *"."* ]]; then
                # 不支持嵌套字段
                echo ""
                return 1
            fi

            # 提取字段值
            echo "$json" | grep -o "\"${field_path}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | \
                sed "s/\"${field_path}\"[[:space:]]*:[[:space:]]*\""'/"/' | sed 's/"$//'
            ;;
        *)
            echo ""
            return 1
            ;;
    esac
}

# =============================================================================
# 批量获取多个 JSON 字段值
# =============================================================================
# 功能：一次调用获取多个字段值，减少进程启动开销
# 用法：json_get_multi "json_string" "field1" "field2" ...
# 参数：
#   json_string  - JSON 字符串
#   field1, field2, ... - 字段路径列表
# 输出：每行一个字段值，顺序与输入字段一致
#
# 示例：
#   local -a vals
#   mapfile -t vals <<< "$(json_get_multi "$json" name age address.city)"
#   local name="${vals[0]:-}" age="${vals[1]:-}" city="${vals[2]:-}"
# =============================================================================
json_get_multi() {
    local json="$1"
    shift
    local fields=("$@")

    # 如果未初始化，先初始化
    if [ -z "$JSON_PARSER" ]; then
        json_init
    fi

    # 空值处理
    if [ -z "$json" ] || [ ${#fields[@]} -eq 0 ]; then
        return 1
    fi

    case "$JSON_PARSER" in
        jq)
            # jq 解析：输出每行一个值
            local query=""
            for field in "${fields[@]}"; do
                if [ -n "$query" ]; then
                    query="$query, "
                fi
                query="$query(.$field // \"\")"
            done
            echo "$json" | jq -r "$query" 2>/dev/null
            ;;
        python3)
            # Python3 解析（一次调用获取所有字段）
            echo "$json" | "$PYTHON3" -c "
import sys, json
d = json.load(sys.stdin)
for f in sys.argv[1:]:
    parts = f.split('.')
    result = d
    try:
        for p in parts:
            if isinstance(result, dict) and p in result:
                result = result[p]
            else:
                result = ''
                break
        if result is None:
            result = ''
        elif isinstance(result, bool):
            result = 'true' if result else 'false'
        print(str(result))
    except Exception:
        print('')
" "${fields[@]}" 2>/dev/null
            ;;
        native)
            # native 不支持批量，逐个调用 json_get
            local field
            for field in "${fields[@]}"; do
                json_get "$json" "$field"
            done
            ;;
        *)
            return 1
            ;;
    esac
}

# =============================================================================
# 内部函数：获取 JSON 复合类型（对象或数组）
# =============================================================================
# 功能：从 JSON 字符串中提取指定路径的复合类型值
# 参数：
#   $1 - json_string   JSON 字符串
#   $2 - field_path    路径（如 "tool_input"、"tool_input.questions"）
#   $3 - fallback      路径不存在时的默认值（"{}" 或 "[]"）
# 输出：JSON 对象/数组字符串
# =============================================================================
_json_get_complex() {
    local json="$1"
    local field_path="$2"
    local fallback="$3"  # "{}" 或 "[]"

    # 如果未初始化，先初始化
    if [ -z "$JSON_PARSER" ]; then
        json_init
    fi

    # 空值处理
    if [ -z "$json" ] || [ -z "$field_path" ]; then
        echo "$fallback"
        return 1
    fi

    case "$JSON_PARSER" in
        jq)
            # jq 解析
            echo "$json" | jq -c ".$field_path // $fallback" 2>/dev/null
            ;;
        python3)
            # Python3 解析（安全处理中间键不存在）
            echo "$json" | "$PYTHON3" -c "
import sys, json
fallback = json.loads(sys.argv[2])
d = json.load(sys.stdin)
parts = sys.argv[1].split('.')
result = d
try:
    for part in parts:
        if isinstance(result, dict) and part in result:
            result = result[part]
        else:
            result = fallback
            break
    print(json.dumps(result))
except Exception:
    print(sys.argv[2])
" "$field_path" "$fallback" 2>/dev/null
            ;;
        native)
            # native 仅支持单层对象提取，数组直接返回 fallback
            if [ "$fallback" != "{}" ]; then
                echo "$fallback"
                return 1
            fi
            # grep/sed 解析（简化版，仅支持单层对象）
            if [[ "$field_path" == *"."* ]]; then
                echo "{}"
                return 1
            fi

            # 提取对象内容（简化版，不完美）
            local value
            value=$(echo "$json" | grep -o "\"${field_path}\"[[:space:]]*:[[:space:]]*{[^}]*}" | head -1 | \
                sed "s/\"${field_path}\"[[:space:]]*:[[:space:]]*//")
            if [ -n "$value" ]; then
                echo "$value"
            else
                echo "{}"
            fi
            ;;
        *)
            echo "$fallback"
            return 1
            ;;
    esac
}

# =============================================================================
# 获取 JSON 对象
# =============================================================================
# 功能：从 JSON 字符串中提取指定的对象部分
# 用法：json_get_object "json_string" "field_path"
# 参数：
#   json_string  - JSON 字符串
#   field_path   - 对象路径（如 "tool_input"）
# 输出：JSON 对象字符串，路径不存在时返回 {}
#
# 示例：
#   tool_input=$(json_get_object "$json" "tool_input")
# =============================================================================
json_get_object() {
    _json_get_complex "$1" "$2" "{}"
}

# =============================================================================
# 获取 JSON 数组
# =============================================================================
# 功能：从 JSON 字符串中提取指定路径的数组
# 用法：json_get_array "json_string" "field_path"
# 参数：
#   json_string  - JSON 字符串
#   field_path   - 数组路径（如 "tool_input.questions"）
# 输出：JSON 数组字符串，路径不存在时返回 []
#
# 示例：
#   questions=$(json_get_array "$json" "tool_input.questions")
# =============================================================================
json_get_array() {
    _json_get_complex "$1" "$2" "[]"
}

# =============================================================================
# JSON 字符串转义
# =============================================================================
# 功能：对字符串进行 JSON 规范要求的转义
# 用法：json_escape "string"
# 参数：
#   $1 - 需要转义的字符串
# 输出：转义后的字符串（不含外层引号）
#
# 示例：
#   json_escape 'hello "world"'   # 输出：hello \"world\"
#   json_escape 'line1\nline2'    # 输出：line1\\nline2
# =============================================================================
json_escape() {
    printf '%s' "$1" | awk '
    BEGIN { ORS="" }
    {
        if (NR > 1) printf "\\n"   # 行间换行 -> \n
        gsub(/\\/, "\\\\")         # \ -> \\
        gsub(/"/, "\\\"")          # " -> \"
        gsub(/\t/, "\\t")          # tab -> \t
        gsub(/\r/, "\\r")          # CR -> \r
        gsub(/\010/, "\\b")        # BS -> \b
        gsub(/\014/, "\\f")        # FF -> \f
        printf "%s", $0
    }'
}

# =============================================================================
# 构建 JSON 对象（无 jq 时使用）
# =============================================================================
# 功能：构建简单的 JSON 对象（用于没有 jq 的环境）
# 用法：json_build_object "key1" "value1" "key2" "value2" ...
# 参数：
#   key1, value1, key2, value2, ... - 键值对（必须成对出现）
# 输出：JSON 对象字符串
#
# 示例：
#   json_obj=$(json_build_object "request_id" "123" "project_dir" "/path")
#   # 输出：{"request_id":"123","project_dir":"/path"}
#
# 注意：这是简化实现，不支持嵌套对象；值会自动通过 json_escape 转义
# =============================================================================
json_build_object() {
    local result="{"
    local first=true

    while [ $# -ge 2 ]; do
        local key="$1"
        local value="$2"
        shift 2

        if [ "$first" = true ]; then
            first=false
        else
            result="${result},"
        fi

        result="${result}\"${key}\":\"$(json_escape "$value")\""
    done

    result="${result}}"
    echo "$result"
}

# =============================================================================
# 从数组中提取指定条件的值
# =============================================================================
# 功能：从 JSON 数组中提取指定 type 字段的值
# 用法：json_get_array_value "json_string" "array_path" "type_filter" "value_field"
# 参数：
#   json_string  - JSON 字符串
#   array_path   - 数组路径（如 "message.content"）
#   type_filter  - 过滤条件（如 "text" 匹配 .type=="text"）
#   value_field  - 要提取的值字段（如 "text"）
# 输出：匹配项的值（多个用换行符连接）
#
# 示例：
#   # 从 {"message":{"content":[{"type":"text","text":"hello"}]}} 提取 "hello"
#   text=$(json_get_array_value "$json" "message.content" "text" "text")
# =============================================================================
json_get_array_value() {
    local json="$1"
    local array_path="$2"
    local type_filter="$3"
    local value_field="$4"

    # 如果未初始化，先初始化
    if [ -z "$JSON_PARSER" ]; then
        json_init
    fi

    # 空值处理
    if [ -z "$json" ] || [ -z "$array_path" ]; then
        return 1
    fi

    case "$JSON_PARSER" in
        jq)
            # jq 解析：遍历数组，过滤 type，提取 value_field
            # 注意：不要用 tr '\n' '∂' 拼接，会在末尾多出一个分隔符
            # 旧写法（有尾部分隔符 bug）：
            #   echo "$json" | jq -r ".$array_path[]? | select(.type==\"$type_filter\") | .$value_field" 2>/dev/null | tr '\n' '∂'
            echo "$json" | jq -r "[.$array_path[]? | select(.type==\"$type_filter\") | .$value_field] | join(\"∂\")" 2>/dev/null
            ;;
        python3)
            # Python3 解析
            echo "$json" | "$PYTHON3" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    array_path, type_filter, val_field = sys.argv[1], sys.argv[2], sys.argv[3]
    parts = array_path.split('.')
    obj = data
    for part in parts:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            obj = []
            break
    if isinstance(obj, list):
        values = []
        for item in obj:
            if item.get('type') == type_filter:
                v = item.get(val_field, '')
                if isinstance(v, bool):
                    v = 'true' if v else 'false'
                values.append(str(v) if v is not None else '')
        print('∂'.join(values))
except Exception:
    print('')
" "$array_path" "$type_filter" "$value_field" 2>/dev/null
            ;;
        *)
            # 不支持
            return 1
            ;;
    esac
}

# =============================================================================
# 检查字段是否存在
# =============================================================================
# 功能：检查 JSON 中是否存在指定字段
# 用法：json_has_field "json_string" "field_path"
# 参数：
#   json_string  - JSON 字符串
#   field_path   - 字段路径
# 输出：0 表示存在，1 表示不存在
# =============================================================================
json_has_field() {
    local json="$1"
    local field_path="$2"

    # 如果未初始化，先初始化
    if [ -z "$JSON_PARSER" ]; then
        json_init
    fi

    # 空值处理
    if [ -z "$json" ] || [ -z "$field_path" ]; then
        return 1
    fi

    case "$JSON_PARSER" in
        jq)
            # jq 解析
            local value
            value=$(echo "$json" | jq -r ".$field_path // empty" 2>/dev/null)
            [ -n "$value" ]
            ;;
        python3)
            # Python3 解析（安全处理中间键不存在）
            local value
            value=$(echo "$json" | "$PYTHON3" -c "
import sys, json
d = json.load(sys.stdin)
parts = sys.argv[1].split('.')
result = d
try:
    for part in parts:
        if isinstance(result, dict) and part in result:
            result = result[part]
        else:
            result = None
            break
    print('1' if result is not None else '')
except Exception:
    print('')
" "$field_path" 2>/dev/null)
            [ -n "$value" ]
            ;;
        native)
            # grep/sed 解析
            echo "$json" | grep -q "\"${field_path}\"[[:space:]]*:"
            ;;
        *)
            return 1
            ;;
    esac
}
