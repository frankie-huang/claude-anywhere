#!/bin/bash
# =============================================================================
# src/hooks/stop.sh - Claude Code Stop 事件处理脚本
#
# 此脚本由 hook-router.sh 通过 source 调用，不直接执行
#
# 前置条件（由 hook-router.sh 完成）:
#   - $INPUT 变量包含从 stdin 读取的 JSON 数据
#   - 核心库、JSON 解析器、日志系统已初始化
#   - $PROJECT_ROOT, $SRC_DIR, $LIB_DIR 等路径变量已设置
#
# 适用场景:
#   - 主 Agent 完成响应
#   - 发送任务完成通知，包含 Claude 的最终响应内容
#
# 设计原则:
#   - 快速返回，不阻塞 Claude Code
#   - 通知发送在后台异步执行
#   - 任何错误不影响 Claude 正常退出
# =============================================================================

# 检查 stop_hook_active 标志，防止无限循环
STOP_HOOK_ACTIVE=$(json_get "$INPUT" "stop_hook_active")
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    log "Stop hook already active, skipping"
    exit 0
fi

# =============================================================================
# Markdown 预处理
# 对 response_json 中的 texts 做飞书卡片兼容转换（单次子进程，单次 JSON 解析）
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
#   $1 - response_json  含 texts 数组的 JSON
#   $2 - heading_style  标题降级样式（bar/circle/diamond/original）
# 输出：处理后的 response_json
# =============================================================================
preprocess_markdown() {
    local response_json="$1"
    local heading_style="${2:-bar}"

    if [ "$JSON_HAS_JQ" = "true" ]; then
        echo "$response_json" | jq --arg heading_style "$heading_style" '
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
                # 脚注定义替换 与 标题降级 互斥（jq 无 early return，用 if/else 实现）
                | if test("^\\s*\\[\\^(?:[^\\]]+)\\]:\\s.+$") then
                    # 脚注: [^id]: content → **注 id**: content
                    capture("^(?<indent>\\s*)\\[\\^(?<id>[^\\]]+)\\]:\\s(?<content>.+)$") as $m |
                    if $m then "\($m.indent)**注 \($m.id)**: \($m.content)" else . end
                  else
                    # 标题: # title → 加粗/emoji 格式
                    if $styles != null then
                        capture("^(?<hashes>#{1,6})\\s+(?<title>.+)$") as $m |
                        if $m then
                            (($m.hashes | length) - 1) as $idx |
                            $styles[$idx] as $pair |
                            $pair[0] + $m.title + $pair[1]
                        else . end
                    else . end
                  end;

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

            .texts |= map(preprocess)
        ' 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        echo "$response_json" | "$PYTHON3" -c "
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
# 对单行依次执行：图片转文本 → HTML 剥离 → 脚注替换 → 标题降级
# 在主循环中被调用，仅作用于代码块外的行
def transform_line(line):
    # 1. 图片链接转文本: ![alt](url) → [图片: alt](url)
    #    飞书不支持 Markdown 图片语法，会导致卡片渲染报错
    line = img_re.sub(
        lambda m: '[\u56fe\u7247: %s](%s)' % (m.group(1), m.group(2))
                  if m.group(1) else '[\u56fe\u7247](%s)' % m.group(2), line)
    # 2. HTML 标签剥离: <summary> → 加粗，其余标签删除保留内容
    #    飞书不支持 HTML 标签，会原样显示为文本；<br>/<hr> 保留（飞书能渲染）
    line = summary_re.sub(r'**\1**', line)
    line = html_re.sub('', line)
    # 3. 脚注定义替换: [^id]: content → **注 id**: content
    #    飞书会吞掉脚注定义行，替换后内容始终可见
    m = footnote_def_re.match(line)
    if m:
        return '%s**\u6ce8 %s**: %s' % (m.group(1), m.group(2), m.group(3))
    # 4. 标题降级: # title → 加粗/emoji 格式（heading_style=original 时跳过）
    if h_formats:
        m = heading_re.match(line)
        if m:
            pre, suf = h_formats[len(m.group(1)) - 1]
            line = '%s%s%s' % (pre, m.group(2), suf)
    return line

# ── 主处理循环 ──────────────────────────────────────────────
# 单次遍历：代码块保护 → 行级转换
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

data['texts'] = [process_text(t) for t in data.get('texts', [])]
json.dump(data, sys.stdout, ensure_ascii=False)
" "$heading_style" 2>/dev/null
    else
        echo "$response_json"
    fi
}

# =============================================================================
# 从单个 transcript 文件中提取响应内容（texts 数组 + thinking）
# 参数:
#   $1 - transcript 文件路径
#   $2 - 最大重试次数 (可选，默认 5)
# 返回:
#   输出 JSON 字符串 {"texts":["...","..."],"thinking":"...","session_id":"..."}
#   找不到有效内容则返回空
# 说明:
#   找到最近一条用户文本消息（排除仅含 tool_result 的 user 消息），
#   收集其后所有 assistant 消息中的 text 和 thinking 内容。
#   texts 按 assistant 消息分组，每条 assistant 的文本合并为一个元素。
# =============================================================================
extract_response_from_file() {
    local transcript_file="$1"
    local max_retries="${2:-5}"
    local retry_count=0
    local result=""

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    while [ $retry_count -lt $max_retries ]; do
        if [ "$JSON_HAS_JQ" = "true" ]; then
            result=$(jq -s '
# 找到最近一条含文本的 user 消息的索引
(
    [to_entries[] | select(
        .value.type == "user" and
        (
            (.value.message.content | type == "string" and length > 0) or
            (.value.message.content | type == "array" and (map(select(.type == "text")) | length > 0))
        )
    ) | .key] | if length > 0 then .[-1] else null end
) as $user_idx |
if $user_idx == null then
    null
else
    # 收集 user_idx 之后所有 assistant 消息
    [.[$user_idx + 1:] | .[] | select(.type == "assistant")] |
    {
        texts: [.[] | .message.content // [] | [.[] | select(.type == "text") | .text] | join("") | gsub("^\\n+|\\n+$"; "") | select(length > 0)],
        thinking: ([.[] | .message.content // [] | [.[] | select(.type == "thinking") | .thinking] | join("")] | join("\n\n") | gsub("^\\n+|\\n+$"; "")),
        session_id: ([.[] | .sessionId // empty] | if length > 0 then .[0] else "" end)
    } |
    if (.texts | length) == 0 then null else . end
end
' "$transcript_file" 2>/dev/null)

        elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
            result=$("$PYTHON3" -c "
import sys, json

with open(sys.argv[1], 'r') as f:
    lines = f.readlines()

records = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        records.append(json.loads(line))
    except:
        pass

# 找最近一条含文本的 user 消息索引
user_idx = None
for i in range(len(records) - 1, -1, -1):
    r = records[i]
    if r.get('type') != 'user':
        continue
    content = r.get('message', {}).get('content')
    if isinstance(content, str) and len(content) > 0:
        user_idx = i
        break
    if isinstance(content, list):
        has_text = any(item.get('type') == 'text' for item in content if isinstance(item, dict))
        if has_text:
            user_idx = i
            break

if user_idx is None:
    sys.exit(0)

# 收集 user_idx 之后所有 assistant 消息的 text 和 thinking
# texts 按 assistant 消息分组
stage_texts = []
thinkings = []
session_id = ''
for r in records[user_idx + 1:]:
    if r.get('type') != 'assistant':
        continue
    if not session_id:
        session_id = r.get('sessionId', '')
    content = r.get('message', {}).get('content', [])
    if not isinstance(content, list):
        continue
    msg_parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'text':
            msg_parts.append(item.get('text', ''))
        elif item.get('type') == 'thinking':
            thinkings.append(item.get('thinking', ''))
    msg_text = ''.join(msg_parts).strip()
    if msg_text:
        stage_texts.append(msg_text)

combined_thinking = '\n\n'.join(t for t in thinkings if t).strip()

if not stage_texts:
    sys.exit(0)

print(json.dumps({'texts': stage_texts, 'thinking': combined_thinking, 'session_id': session_id}))
" "$transcript_file" 2>/dev/null)
        fi

        if [ -n "$result" ] && [ "$result" != "null" ]; then
            echo "$result"
            return 0
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 0.1
        fi
    done

    return 1
}

# =============================================================================
# 提取响应内容（带子代理回退）
# 参数:
#   $1 - 主 transcript 文件路径
# 返回:
#   输出 JSON 字符串 {"texts":[...],"thinking":"...","session_id":"..."}
# 说明:
#   1. 先在主 transcript 文件中查找
#   2. 如果找不到，检查 subagents 目录，按修改时间倒序查找
# =============================================================================
extract_response() {
    local transcript_path="$1"

    # 提前检查：路径为空直接返回
    if [ -z "$transcript_path" ]; then
        log "Transcript path is empty"
        return 1
    fi

    local result=""

    # 1. 先在主 transcript 文件中查找
    if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
        log "Searching in main transcript: $transcript_path"
        result=$(extract_response_from_file "$transcript_path" 5)
        if [ -n "$result" ] && [ "$result" != "null" ]; then
            log "Found response in main transcript"
            echo "$result"
            return 0
        fi
    fi

    # 2. 主 transcript 找不到，尝试在 subagents 目录中查找
    local session_dir="${transcript_path%.jsonl}"
    local subagents_dir="$session_dir/subagents"

    if [ -d "$subagents_dir" ]; then
        log "Main transcript has no response, searching in subagents: $subagents_dir"

        # 按修改时间倒序遍历子代理文件（兼容 macOS + Linux）
        local subagent_files=()
        while IFS= read -r f; do
            [ -n "$f" ] && subagent_files+=("$f")
        done < <(ls -t "$subagents_dir"/*.jsonl 2>/dev/null)

        for subagent_file in "${subagent_files[@]}"; do
            if [ -f "$subagent_file" ]; then
                log "Searching in subagent: $(basename "$subagent_file")"
                result=$(extract_response_from_file "$subagent_file" 3)
                if [ -n "$result" ] && [ "$result" != "null" ]; then
                    log "Found response in subagent: $(basename "$subagent_file")"
                    echo "$result"
                    return 0
                fi
            fi
        done
    fi

    log "No response found in transcript or subagents"
    return 1
}

# =============================================================================
# 构建响应元素 JSON 片段（多个 markdown 元素，hr 分隔）
# 参数:
#   $1 - response_json  extract_response 返回的 JSON（含 texts 数组）
#   $2 - max_length     总文本最大长度
# 输出:
#   逗号分隔的 JSON 元素字符串，可直接嵌入飞书卡片 elements 数组
# =============================================================================
build_response_elements() {
    local response_json="$1"
    local max_length="$2"

    if [ "$JSON_HAS_JQ" = "true" ]; then
        echo "$response_json" | jq -r --argjson max_len "$max_length" '
            .texts |
            # 按总长度截断，同时跟踪是否发生截断
            reduce .[] as $t (
                {remaining: $max_len, result: [], truncated: false};
                if .remaining <= 0 then .truncated = true
                elif ($t | length) <= .remaining then
                    .result += [$t] | .remaining -= ($t | length)
                else
                    .result += [$t[:.remaining] + "..."] | .remaining = 0 | .truncated = true
                end
            ) | .truncated as $is_truncated | .result |
            # 构建 markdown 元素，元素间用 hr 分隔
            [to_entries[] |
                (if .key > 0 then
                    [{tag: "hr", margin: "0px 0px 0px 0px"}]
                else [] end) +
                [{
                    tag: "markdown",
                    content: (.value | split("\n") | map(
                        if test("^\\s*```") then sub("^\\s*"; "") else . end
                    ) | join("\n")),
                    text_align: "left",
                    text_size: "normal_v2"
                }]
            ] | flatten |
            # 截断时追加提示元素
            (if $is_truncated then
                . + [{tag: "markdown", content: "<font color='"'"'grey'"'"'>⚠️ 内容过长，已截断</font>", text_align: "left", text_size: "notation", margin: "4px 0px 0px 0px"}]
            else . end) |
            map(tojson) | join(",")
        ' 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        echo "$response_json" | "$PYTHON3" -c "
import sys, json, re
data = json.load(sys.stdin)
texts = data.get('texts', [])
max_len = int(sys.argv[1])
truncated = []
remaining = max_len
is_truncated = False
for text in texts:
    if remaining <= 0:
        is_truncated = True
        break
    if len(text) <= remaining:
        truncated.append(text)
        remaining -= len(text)
    else:
        truncated.append(text[:remaining] + '...')
        is_truncated = True
        remaining = 0
fence = chr(96) * 3  # 即 3 个反引号，代码围栏标记
elements = []
for i, text in enumerate(truncated):
    text = re.sub(r'^[ \t]*' + fence, fence, text, flags=re.MULTILINE)
    if i > 0:
        elements.append(json.dumps({'tag': 'hr', 'margin': '0px 0px 0px 0px'}))
    elements.append(json.dumps({'tag': 'markdown', 'content': text, 'text_align': 'left', 'text_size': 'normal_v2'}))
if is_truncated:
    elements.append(json.dumps({'tag': 'markdown', 'content': \"<font color='grey'>\u26a0\ufe0f 内容过长，已截断</font>\", 'text_align': 'left', 'text_size': 'notation', 'margin': '4px 0px 0px 0px'}))
print(','.join(elements))
" "$max_length" 2>/dev/null
    fi
}

# =============================================================================
# 后台异步发送通知函数
# =============================================================================
send_stop_notification_async() {
    # 捕获当前环境变量供后台使用
    local SEND_MODE=$(get_config "FEISHU_SEND_MODE" "webhook")
    local WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "")
    local STOP_MESSAGE_MAX_LENGTH=$(get_config "STOP_MESSAGE_MAX_LENGTH" "10000")
    local STOP_THINKING_MAX_LENGTH=$(get_config "STOP_THINKING_MAX_LENGTH" "10000")
    local CALLBACK_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")
    local TRANSCRIPT_PATH=$(json_get "$INPUT" "transcript_path")
    local PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(json_get "$INPUT" "cwd")}"
    local PROJECT_NAME=$(basename "${PROJECT_DIR:-$(pwd)}")
    local SESSION_ID=""
    local TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

    # 检查是否有可用的发送渠道（webhook 需要 URL，openapi 模式直接放行）
    if [ "$SEND_MODE" != "openapi" ] && [ -z "$WEBHOOK_URL" ]; then
        return 0
    fi

    # 引入函数库（后台进程需要重新引入）
    source "$LIB_DIR/core.sh" 2>/dev/null || return 0
    source "$LIB_DIR/feishu.sh" 2>/dev/null || return 0
    source "$LIB_DIR/json.sh" 2>/dev/null || return 0
    json_init
    log_init

    log "Stop notification: extracting response from transcript"

    # 提取 Claude 响应内容（texts 数组 + thinking）
    local CLAUDE_THINKING=""
    local RESPONSE_ELEMENTS=""
    local response_json=""

    # 使用 extract_response 函数（支持子代理回退）
    response_json=$(extract_response "$TRANSCRIPT_PATH")

    if [ -n "$response_json" ] && [ "$response_json" != "null" ]; then
        # 从结果中提取 session_id 和 thinking
        SESSION_ID=$(json_get "$response_json" "session_id")
        if [ -z "$SESSION_ID" ]; then
            SESSION_ID="unknown"
        fi

        CLAUDE_THINKING=$(json_get "$response_json" "thinking")

        # 前置解析 chat_id 并检查 mute 状态，muted 时跳过后续所有处理
        local RESOLVED_CHAT_ID
        RESOLVED_CHAT_ID=$(_resolve_chat_id "$SESSION_ID" "$PROJECT_DIR")
        if [ "$RESOLVED_CHAT_ID" = "$MUTED_SENTINEL" ]; then
            log "Session muted, skipping stop notification: $SESSION_ID"
            return 0
        fi

        # Markdown 预处理（图片转换 + HTML 剥离 + 脚注展平 + 标题降级，单次子进程）
        local HEADING_STYLE=$(get_config "FEISHU_HEADING_STYLE" "bar")
        local _processed
        _processed=$(preprocess_markdown "$response_json" "$HEADING_STYLE")
        if [ -n "$_processed" ]; then
            response_json="$_processed"
        fi

        # 构建响应元素 JSON 片段（含截断和代码块格式化）
        RESPONSE_ELEMENTS=$(build_response_elements "$response_json" "$STOP_MESSAGE_MAX_LENGTH")
        log "Built response elements: ${#RESPONSE_ELEMENTS} chars, thinking: ${#CLAUDE_THINKING} chars"
    fi

    # 无响应时使用默认消息
    if [ -z "$RESPONSE_ELEMENTS" ]; then
        RESPONSE_ELEMENTS='{"tag":"markdown","content":"任务已完成，请返回终端查看详细信息。","text_align":"left","text_size":"normal_v2"}'
        log "Using default response"
    fi

    # 处理 thinking：STOP_THINKING_MAX_LENGTH=0 时跳过
    if [ "$STOP_THINKING_MAX_LENGTH" = "0" ]; then
        CLAUDE_THINKING=""
        log "Thinking display disabled (STOP_THINKING_MAX_LENGTH=0)"
    elif [ -n "$CLAUDE_THINKING" ] && [ ${#CLAUDE_THINKING} -gt "$STOP_THINKING_MAX_LENGTH" ]; then
        CLAUDE_THINKING="${CLAUDE_THINKING:0:$STOP_THINKING_MAX_LENGTH}..."
        log "Thinking truncated to ${#CLAUDE_THINKING} chars"
    fi

    # 构建并发送卡片
    local CARD
    CARD=$(build_stop_card "$RESPONSE_ELEMENTS" "$PROJECT_NAME" "$TIMESTAMP" "$SESSION_ID" "$CLAUDE_THINKING" 2>/dev/null)

    if [ -n "$CARD" ]; then
        # 传递 session_id, project_dir, callback_url 支持回复继续会话
        local options
        options=$(json_build_object "webhook_url" "$WEBHOOK_URL" "session_id" "$SESSION_ID" "project_dir" "$PROJECT_DIR" "callback_url" "$CALLBACK_URL" "chat_id" "$RESOLVED_CHAT_ID")
        send_feishu_card "$CARD" "$options" >/dev/null 2>&1
    fi
}

# 启动后台通知发送（不等待，立即返回）
# 注意: Stop hook 配置不要加 async: true，否则双层 async 可能导致此后台进程被提前终止
send_stop_notification_async &

# 立即返回，不阻塞 Claude Code
exit 0
