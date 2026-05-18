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
#   - 发送任务完成通知，包含 Agent 的最终响应内容
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
# 从 Codex JSONL transcript 提取响应
# 支持两类格式：
#   1. codex exec --json stdout:
#   {"type":"thread.started","thread_id":"xxx"}
#   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
#   2. ~/.codex/sessions/.../rollout-*.jsonl:
#   {"type":"session_meta","payload":{"id":"xxx",...}}
#   {"type":"event_msg","payload":{"type":"agent_message","phase":"commentary","message":"..."}}
#   {"type":"event_msg","payload":{"type":"task_complete","turn_id":"...","last_agent_message":"..."}}
# 参数:
#   $1 - transcript 文件路径
#   $2 - turn_id (可选，Codex 持久化格式下用于定位目标 turn)
# 返回: {"texts":["..."],"thinking":"","session_id":"xxx"} 或空
# =============================================================================
extract_codex_response() {
    local transcript_file="$1"
    local turn_id="$2"
    local result=""

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    # 1. jq：仅处理 codex exec --json stdout 格式（持久化格式的 turn 分片和 reasoning 提取用 jq 难以维护）
    if [ "$JSON_HAS_JQ" = "true" ]; then
        result=$(jq -s '
(
    [.[] | select(.type == "thread.started") | .thread_id] |
    if length > 0 then .[0] else "" end
) as $sid |
if $sid == "" then null
else
[
    .[] | select(.type == "item.completed" and .item.type == "agent_message") |
    .item.text | gsub("^\\n+|\\n+$"; "") | select(length > 0)
] |
if length == 0 then null
else {texts: ., thinking: "", session_id: $sid}
end
end
' "$transcript_file" 2>/dev/null)
        if [ -n "$result" ] && [ "$result" != "null" ]; then
            echo "$result"
            return 0
        fi
    fi

    # 2. Python：处理所有格式（codex exec --json stdout + 持久化会话），jq 未命中或不可用时走此路径
    if [ "$JSON_HAS_PYTHON3" = "true" ]; then
        "$PYTHON3" -c "
import sys, json

path = sys.argv[1]
target_turn_id = sys.argv[2] if len(sys.argv) > 2 else ''

with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

records = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        records.append(json.loads(line))
    except Exception:
        continue

if not records:
    sys.exit(0)

def payload(record):
    p = record.get('payload')
    return p if isinstance(p, dict) else {}

def append_unique(items, text):
    text = (text or '').strip()
    if text and (not items or items[-1] != text):
        items.append(text)

def collect_text_from_content(content):
    parts = []
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get('type') in ('text', 'output_text'):
                parts.append(item.get('text') or '')
            elif isinstance(item.get('content'), str):
                parts.append(item.get('content'))
    return ''.join(parts)

# codex exec --json stdout format
if any(r.get('type') == 'thread.started' for r in records):
    session_id = ''
    texts = []
    for event in records:
        etype = event.get('type', '')
        if etype == 'thread.started':
            session_id = event.get('thread_id', '') or session_id
        elif etype == 'item.completed':
            item = event.get('item', {})
            if isinstance(item, dict) and item.get('type') == 'agent_message':
                append_unique(texts, item.get('text', ''))
    if texts:
        print(json.dumps({'texts': texts, 'thinking': '', 'session_id': session_id}, ensure_ascii=False))
    sys.exit(0)

# Codex persistent session format
session_id = ''
for r in records:
    if r.get('type') == 'session_meta':
        session_id = payload(r).get('id', '') or session_id
        break

if not target_turn_id:
    for r in reversed(records):
        p = payload(r)
        if p.get('type') == 'task_complete' and p.get('turn_id'):
            target_turn_id = p.get('turn_id')
            break

subset = records
if target_turn_id:
    start_idx = None
    end_idx = None
    for i, r in enumerate(records):
        p = payload(r)
        if p.get('turn_id') != target_turn_id:
            continue
        if start_idx is None and (
            (r.get('type') == 'event_msg' and p.get('type') == 'task_started') or
            r.get('type') == 'turn_context'
        ):
            start_idx = i
        if start_idx is not None and r.get('type') == 'event_msg' and p.get('type') in ('task_complete', 'turn_aborted'):
            end_idx = i
            break
    if start_idx is not None:
        subset = records[start_idx:(end_idx + 1 if end_idx is not None else len(records))]

texts = []
thinkings = []
fallback_final = ''

for r in subset:
    rtype = r.get('type')
    p = payload(r)

    if rtype == 'event_msg':
        ptype = p.get('type')
        if ptype == 'agent_message':
            msg = (p.get('message') or '').strip()
            if msg:
                append_unique(texts, msg)
        elif ptype == 'task_complete':
            fallback_final = p.get('last_agent_message') or fallback_final
        continue

    if rtype != 'response_item':
        continue

    ptype = p.get('type')
    if ptype == 'reasoning':
        summary = p.get('summary')
        if isinstance(summary, list):
            for item in summary:
                if isinstance(item, str):
                    append_unique(thinkings, item)
                elif isinstance(item, dict):
                    append_unique(thinkings, item.get('text') or item.get('summary') or item.get('content') or '')
        elif isinstance(summary, str):
            append_unique(thinkings, summary)
        content = p.get('content')
        if isinstance(content, str):
            append_unique(thinkings, content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    append_unique(thinkings, item.get('text') or item.get('content') or '')
        continue

    # Older/different Codex builds may only write response_item.message.
    if ptype == 'message' and p.get('role') in ('assistant', 'agent'):
        msg = collect_text_from_content(p.get('content')).strip()
        if msg:
            append_unique(texts, msg)
    elif ptype == 'agent_message':
        append_unique(texts, p.get('text') or p.get('message') or '')

append_unique(texts, fallback_final)

if not texts:
    sys.exit(0)

print(json.dumps({
    'texts': texts,
    'thinking': '\n\n'.join(t for t in thinkings if t).strip(),
    'session_id': session_id
}, ensure_ascii=False))
" "$transcript_file" "$turn_id" 2>/dev/null
        return $?
    fi
    return 1
}

# =============================================================================
# 检测 transcript 是否为 Codex 格式
# =============================================================================
is_codex_transcript() {
    local transcript_file="$1"
    [ -f "$transcript_file" ] || return 1
    local first_line
    first_line=$(head -1 "$transcript_file" 2>/dev/null)
    case "$first_line" in
        *'"type"'*'"thread.started"'*) return 0 ;;
        *'"type"'*'"session_meta"'*) return 0 ;;
        *) return 1 ;;
    esac
}

# =============================================================================
# 从 Claude transcript 提取响应
# 找到最近一条用户文本消息（排除仅含 tool_result 的 user 消息），
# 收集其后所有 assistant 消息中的 text 和 thinking 内容。
# texts 按 assistant 消息分组，每条 assistant 的文本合并为一个元素。
# 参数:
#   $1 - transcript 文件路径
# 返回: {"texts":["..."],"thinking":"...","session_id":"xxx"} 或空
# =============================================================================
extract_claude_response() {
    local transcript_file="$1"

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    if [ "$JSON_HAS_JQ" = "true" ]; then
        jq -s '
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
' "$transcript_file" 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        "$PYTHON3" -c "
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
" "$transcript_file" 2>/dev/null
    fi
}

# =============================================================================
# 从单个 transcript 文件中提取响应内容（分发 + 重试）
# 自动检测 Codex/Claude 格式，委托给对应的提取函数。
# 参数:
#   $1 - transcript 文件路径
#   $2 - turn_id (可选，Codex 持久化格式下用于定位目标 turn)
#   $3 - 最大重试次数 (可选，默认 5)
# 返回:
#   输出 JSON 字符串 {"texts":["...","..."],"thinking":"...","session_id":"..."}
#   找不到有效内容则返回空
# =============================================================================
extract_response_from_file() {
    local transcript_file="$1"
    local turn_id="$2"
    local max_retries="${3:-5}"
    local retry_count=0
    local result=""
    local extract_fn=""
    local retry_interval=0.1

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    # 按格式选择提取函数、重试间隔和次数
    if is_codex_transcript "$transcript_file"; then
        log "Detected Codex transcript format"
        extract_fn="extract_codex_response"
        retry_interval=1  # Codex 持久化文件写入延迟比 Claude 大
    else
        extract_fn="extract_claude_response"
    fi

    while [ $retry_count -lt $max_retries ]; do
        if [ "$extract_fn" = "extract_codex_response" ]; then
            result=$($extract_fn "$transcript_file" "$turn_id")
        else
            result=$($extract_fn "$transcript_file")
        fi

        if [ -n "$result" ] && [ "$result" != "null" ]; then
            echo "$result"
            return 0
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            log "Retry $retry_count/$max_retries (interval=${retry_interval}s)"
            sleep $retry_interval
        fi
    done

    return 1
}

# =============================================================================
# 提取响应内容（带子代理回退）
# 参数:
#   $1 - 主 transcript 文件路径
#   $2 - turn_id (可选，Codex 持久化格式下用于定位目标 turn)
# 返回:
#   输出 JSON 字符串 {"texts":[...],"thinking":"...","session_id":"..."}
# 说明:
#   1. 先在主 transcript 文件中查找
#   2. 如果找不到，检查 subagents 目录，按修改时间倒序查找
# =============================================================================
extract_response() {
    local transcript_path="$1"
    local turn_id="$2"

    # 提前检查：路径为空直接返回
    if [ -z "$transcript_path" ]; then
        log "Transcript path is empty"
        return 1
    fi

    local result=""

    # 1. 先在主 transcript 文件中查找
    if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
        log "Searching in main transcript: $transcript_path"
        result=$(extract_response_from_file "$transcript_path" "$turn_id" 5)
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
                result=$(extract_response_from_file "$subagent_file" "$turn_id" 3)
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
# 用 last_assistant_message 补全 response_json 的 texts 数组
# 参数:
#   $1 - response_json           extract_response 返回的 JSON（可为空）
#   $2 - last_assistant_message  Stop 事件提供的最终答复文本（可为空）
#   $3 - session_id              会话 ID（transcript 无数据时用于构造 fallback）
# 返回:
#   更新后的 response_json（stdout），补全失败时返回原值
# 说明:
#   Stop hook 后台执行时 transcript 可能尚未写入最终 assistant text，
#   但中间过程（thinking、早期 text）已落盘。本函数将 last_assistant_message
#   追加到 texts 末尾（如尚未包含）。transcript 无数据时构造最小 fallback。
# =============================================================================
supplement_last_message() {
    local response_json="$1"
    local last_msg="$2"
    local fallback_session_id="${3:-unknown}"

    if [ -z "$last_msg" ]; then
        echo "$response_json"
        return 0
    fi

    if [ -n "$response_json" ] && [ "$response_json" != "null" ]; then
        # transcript 有数据，追加最终答复（如 texts 末尾不是它）
        # 通过 stdin 管道 + 分隔符传入两个输入：
        #   - 避免 argv 传递长消息 (MAX_ARG_STRLEN=128KB)
        #   - 避免 local wrapper= + json_escape 方案，因为 json_escape 未覆盖所有控制字符，
        #     特殊字符会导致构造的 wrapper JSON 畸形而解析失败；分隔符方案原样传递无丢失
        local merged_json=""
        local SEP='__SUPPLEMENT_SEP_b7e2f4__'
        if [ "$JSON_HAS_JQ" = "true" ]; then
            merged_json=$(printf '%s\n%s\n%s' "$response_json" "$SEP" "$last_msg" \
                | jq -R -s --arg sep "$SEP" '
                    split($sep + "\n") as $raw_parts |
                    # jq split 无 maxsplit，需手动 re-join 以对齐 Python split(...,1)
                    (if ($raw_parts | length) > 2 then
                        [$raw_parts[0], ($raw_parts[1:] | join($sep + "\n"))]
                    else $raw_parts end) as $parts |
                    ($parts[0] | fromjson) as $resp |
                    $parts[1] as $msg |
                    if ($msg == null) then $resp
                    elif (.texts | length) == 0 or (.texts[-1] != $msg) then
                        .texts += [$msg]
                    else . end
                ' 2>/dev/null)
        elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
            merged_json=$(printf '%s\n%s\n%s' "$response_json" "$SEP" "$last_msg" \
                | SEP="$SEP" "$PYTHON3" -c "
import sys, json, os
parts = sys.stdin.read().split(os.environ['SEP'] + chr(10), 1)
data = json.loads(parts[0])
msg = parts[1] if len(parts) > 1 else ''
if not data.get('texts') or data['texts'][-1] != msg:
    data.setdefault('texts', []).append(msg)
print(json.dumps(data, ensure_ascii=False))
" 2>/dev/null)
        fi
        if [ -n "$merged_json" ] && [ "$merged_json" != "null" ]; then
            log "Supplemented response with last_assistant_message"
            echo "$merged_json"
        else
            echo "$response_json"
        fi
    else
        # transcript 无数据，用 last_assistant_message 构造最小 response
        # 注：此路径几乎不可达（需 extract_response 彻底失败且 last_msg 非空），
        # json_escape 的控制字符风险在此无实际影响，无需走 stdin 管道
        local result="{\"texts\":[\"$(json_escape "$last_msg")\"],\"thinking\":\"\",\"session_id\":\"$fallback_session_id\"}"
        log "Constructed response from last_assistant_message (transcript unavailable)"
        echo "$result"
    fi
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
#
# 调用链：
#   send_stop_notification_async()
#     ├─ extract_response(transcript_path)
#     │    └─ extract_response_from_file(transcript, 5 retries)
#     │         └─ (fallback) extract_response_from_file(subagent_file, 3 retries)
#     ├─ supplement_last_message(response_json, hook_input)
#     ├─ build_response_elements(response_json, max_length)
#     └─ build_stop_card(elements, project, timestamp, session_id, thinking)
# =============================================================================
send_stop_notification_async() {
    # 捕获当前环境变量供后台使用
    local SEND_MODE=$(get_config "FEISHU_SEND_MODE" "webhook")
    local WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "")
    local STOP_MESSAGE_MAX_LENGTH=$(get_config "STOP_MESSAGE_MAX_LENGTH" "10000")
    local STOP_THINKING_MAX_LENGTH=$(get_config "STOP_THINKING_MAX_LENGTH" "10000")
    local CALLBACK_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")
    local INPUT_SESSION_ID=$(json_get "$INPUT" "session_id")
    local INPUT_TURN_ID=$(json_get "$INPUT" "turn_id")
    local TRANSCRIPT_PATH=$(json_get "$INPUT" "transcript_path")
    local LAST_ASSISTANT_MSG=$(json_get "$INPUT" "last_assistant_message")
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

    # 提取响应内容（texts 数组 + thinking）
    local THINKING=""
    local RESPONSE_ELEMENTS=""
    local response_json=""

    # 使用 extract_response 函数（支持子代理回退）
    response_json=$(extract_response "$TRANSCRIPT_PATH" "$INPUT_TURN_ID")

    # 用 Stop 事件提供的 last_assistant_message 补全竞态缺失的最终答复
    response_json=$(supplement_last_message "$response_json" "$LAST_ASSISTANT_MSG" "$INPUT_SESSION_ID")

    if [ -n "$response_json" ] && [ "$response_json" != "null" ]; then
        # 从结果中提取 session_id 和 thinking
        SESSION_ID=$(json_get "$response_json" "session_id")
        if [ -z "$SESSION_ID" ]; then
            SESSION_ID="unknown"
        fi

        THINKING=$(json_get "$response_json" "thinking")

        # 前置解析 chat_id 并检查 mute 状态，muted 时跳过后续所有处理
        local RESOLVED_CHAT_ID
        RESOLVED_CHAT_ID=$(_resolve_chat_id "$SESSION_ID" "$PROJECT_DIR")
        if [ "$RESOLVED_CHAT_ID" = "$MUTED_SENTINEL" ]; then
            log "Session muted, skipping stop notification: $SESSION_ID"
            return 0
        fi

        # 构建响应元素 JSON 片段（含截断和代码块格式化）
        # 注: Markdown 预处理已下沉到 feishu.sh 的 send_feishu_card() 中统一处理
        RESPONSE_ELEMENTS=$(build_response_elements "$response_json" "$STOP_MESSAGE_MAX_LENGTH")
        log "Built response elements: ${#RESPONSE_ELEMENTS} chars, thinking: ${#THINKING} chars"
    fi

    # 无响应时使用默认消息
    if [ -z "$RESPONSE_ELEMENTS" ]; then
        RESPONSE_ELEMENTS='{"tag":"markdown","content":"任务已完成，请返回终端查看详细信息。","text_align":"left","text_size":"normal_v2"}'
        log "Using default response"
    fi

    # 处理 thinking：STOP_THINKING_MAX_LENGTH=0 时跳过
    if [ "$STOP_THINKING_MAX_LENGTH" = "0" ]; then
        THINKING=""
        log "Thinking display disabled (STOP_THINKING_MAX_LENGTH=0)"
    elif [ -n "$THINKING" ] && [ ${#THINKING} -gt "$STOP_THINKING_MAX_LENGTH" ]; then
        THINKING="${THINKING:0:$STOP_THINKING_MAX_LENGTH}..."
        log "Thinking truncated to ${#THINKING} chars"
    fi

    # 构建并发送卡片
    local CARD
    CARD=$(build_stop_card "$RESPONSE_ELEMENTS" "$PROJECT_NAME" "$TIMESTAMP" "$SESSION_ID" "$THINKING" 2>/dev/null)

    if [ -n "$CARD" ]; then
        # 传递 session_id, project_dir, callback_url 支持回复继续会话
        local options
        options=$(json_build_object "webhook_url" "$WEBHOOK_URL" "session_id" "$SESSION_ID" "project_dir" "$PROJECT_DIR" "callback_url" "$CALLBACK_URL" "chat_id" "$RESOLVED_CHAT_ID")
        send_feishu_card "$CARD" "$options" >/dev/null 2>&1
    fi
}

# 后台发送；单独 & 不够——宿主以 pipe 关闭（非 PID 退出）判断 hook 结束，
# 子进程继承 stdout/stderr 会导致 pipe 未关闭，需要 >/dev/null 2>&1 切断
# 注意: Stop hook 配置不要加 async: true，否则双层 async 可能导致此后台进程被提前终止
send_stop_notification_async >/dev/null 2>&1 &

# 立即返回，不阻塞 Claude Code
exit 0
