#!/bin/bash

# =============================================================================
# test-permission.sh - hook-router.sh 测试脚本
#
# 用法: ./test-permission.sh [工具类型] [命令/描述]
#
# 示例:
#   ./test-permission.sh                    # 默认测试 Bash 工具
#   ./test-permission.sh bash "rm -rf /tmp"  # 测试 Bash 工具
#   ./test-permission.sh edit "/etc/hosts"   # 测试 Edit 工具
#   ./test-permission.sh write "/tmp/test.txt" "测试内容"  # 测试 Write 工具
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOOK_SCRIPT="${PROJECT_ROOT}/src/hook-router.sh"

# 引入核心库以使用 get_config 函数
if [ -f "${PROJECT_ROOT}/src/lib/core.sh" ]; then
    source "${PROJECT_ROOT}/src/lib/core.sh"
fi

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 默认值
TOOL_TYPE="${1:-bash}"
COMMAND="${2:-echo 'Hello, World!'}"
CONTENT="${3:-}"
DESCRIPTION="${4:-}"
SESSION_ID="test-session-$(date +%s)"
TRANSCRIPT_PATH="${CLAUDE_TRANSCRIPT_PATH:-/tmp/claude-transcript.jsonl}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

# 生成 tool_use_id（模拟字段，实际 PermissionRequest 输入中暂未提供此字段）
# 注意: Claude Code 的 PermissionRequest hook 不提供 tool_use_id，这里仅用于测试标识
generate_tool_use_id() {
    echo "toolu_$(openssl rand -hex 12 2>/dev/null || echo "01ABC123DEF456")"
}

# 打印分隔线
print_separator() {
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# 打印信息
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# JSON 转义函数
json_escape() {
    local str="$1"
    # 转义反斜杠、双引号、换行符
    str="${str//\\/\\\\}"
    str="${str//\"/\\\"}"
    str="${str//$'\n'/\\n}"
    str="${str//$'\r'/\\r}"
    str="${str//$'\t'/\\t}"
    echo "$str"
}

# 生成 Claude Code PermissionRequest hook 标准格式 JSON
generate_input_json() {
    local tool_name="$1"
    local command="$2"
    local content="$3"
    local description="$4"

    # 标准化工具名（首字母大写）
    case "$tool_name" in
        bash) tool_name="Bash" ;;
        edit) tool_name="Edit" ;;
        write) tool_name="Write" ;;
        read) tool_name="Read" ;;
        glob) tool_name="Glob" ;;
        grep) tool_name="Grep" ;;
        ask) tool_name="AskUserQuestion" ;;
        askuserquestion) tool_name="AskUserQuestion" ;;
        skill) tool_name="Skill" ;;
    esac

    local tool_use_id=$(generate_tool_use_id)
    local escaped_command=$(json_escape "$command")
    local escaped_content=$(json_escape "$content")
    local escaped_description=$(json_escape "$description")

    # 构建 tool_input 部分
    local tool_input=""
    case "$tool_name" in
        Bash)
            if [ -n "$description" ]; then
                tool_input="    \"command\": \"$escaped_command\",
    \"description\": \"$escaped_description\",
    \"timeout\": 120000"
            else
                tool_input="    \"command\": \"$escaped_command\",
    \"description\": \"$escaped_command\",
    \"timeout\": 120000"
            fi
            ;;
        Edit)
            tool_input="    \"file_path\": \"$escaped_command\",
    \"old_string\": \"\",
    \"new_string\": \"\",
    \"prompt\": \"修改文件\""
            ;;
        Write)
            tool_input="    \"file_path\": \"$escaped_command\",
    \"content\": \"$escaped_content\""
            ;;
        Read)
            tool_input="    \"file_path\": \"$escaped_command\""
            ;;
        AskUserQuestion)
            # AskUserQuestion 测试：使用内置示例或用户指定的问题
            if [ -n "$COMMAND" ]; then
                # 用户指定了一个问题，构建单个问题
                tool_input="    \"questions\": [
        {
          \"question\": \"$escaped_command\",
          \"header\": \"选择\",
          \"options\": [
            {\"label\": \"选项A\", \"description\": \"第一个选项\"},
            {\"label\": \"选项B\", \"description\": \"第二个选项\"}
          ],
          \"multiSelect\": false
        }
      ]"
            else
                # 使用默认的多问题示例
                tool_input="    \"questions\": [
        {
          \"question\": \"请选择要使用的数据库类型?\",
          \"header\": \"数据库\",
          \"options\": [
            {\"label\": \"PostgreSQL\", \"description\": \"关系型数据库\"},
            {\"label\": \"MongoDB\", \"description\": \"文档数据库\"}
          ],
          \"multiSelect\": false
        },
        {
          \"question\": \"需要启用缓存吗?\",
          \"header\": \"缓存\",
          \"options\": [
            {\"label\": \"是\", \"description\": \"启用 Redis 缓存\"},
            {\"label\": \"否\", \"description\": \"不使用缓存\"}
          ],
          \"multiSelect\": false
        }
      ]"
            fi
            ;;
        Skill)
            # Skill 测试：使用内置示例或用户指定的 skill 名称
            # Skill JSON 格式: { "skill": "skill_name", "args": "optional args" }
            if [ -n "$command" ] && [ "$command" != "echo 'Hello, World!'" ]; then
                # 用户指定了 skill 名称
                if [ -n "$content" ]; then
                    tool_input="    \"skill\": \"$escaped_command\",
    \"args\": \"$escaped_content\""
                else
                    tool_input="    \"skill\": \"$escaped_command\""
                fi
            else
                # 使用默认示例
                tool_input="    \"skill\": \"commit\",
    \"args\": \"-m 'test commit'\""
            fi
            ;;
        *)
            tool_input="    \"prompt\": \"$escaped_command\""
            ;;
    esac

    # 输出完整 JSON（符合 Claude Code PermissionRequest hook 格式）
    cat << EOF
{
  "session_id": "$SESSION_ID",
  "transcript_path": "$TRANSCRIPT_PATH",
  "cwd": "$PROJECT_DIR",
  "permission_mode": "default",
  "hook_event_name": "PermissionRequest",
  "tool_name": "$tool_name",
  "tool_input": {
$tool_input
  },
  "tool_use_id": "$tool_use_id"
}
EOF
}

# 显示使用帮助
show_help() {
    cat << EOF
${GREEN}权限请求测试脚本${NC}

${YELLOW}用法:${NC}
  $0 [工具类型] [命令/路径] [内容] [描述]

${YELLOW}支持的工具类型:${NC}
  bash    - Bash 命令执行 (默认)
  edit    - 文件编辑
  write   - 文件写入
  read    - 文件读取
  glob    - 文件模式匹配
  grep    - 内容搜索
  ask     - AskUserQuestion 用户提问
  skill   - Skill 技能调用

${YELLOW}示例:${NC}
  $0                                    # 默认 Bash 测试
  $0 bash "rm -rf /tmp/test"           # 危险命令测试
  $0 bash "npm install" "构建项目"      # 带描述的 Bash 测试
  $0 edit "/etc/hosts"                 # 编辑系统文件测试
  $0 write "/tmp/test.txt" "测试内容"   # 写入文件测试
  $0 read "/etc/passwd"                # 读取敏感文件测试
  $0 ask                               # AskUserQuestion 默认测试（多问题）
  $0 ask "你喜欢哪种编程语言?"          # AskUserQuestion 单问题测试
  $0 skill                             # Skill 默认测试（commit）
  $0 skill "commit" "-m 'test'"        # Skill 带参数测试

${YELLOW}环境变量:${NC}
  CLAUDE_PROJECT_DIR    - 项目目录 (默认: 当前目录)
  CLAUDE_TRANSCRIPT_PATH - 对话记录文件路径 (默认: /tmp/claude-transcript.jsonl)
  FEISHU_WEBHOOK_URL    - 飞书 Webhook URL
  CALLBACK_SERVER_URL   - 回调服务地址 (默认: http://localhost:8080)

${YELLOW}JSON 格式说明:${NC}
  输出符合 Claude Code PermissionRequest hook 标准格式：
  - session_id, transcript_path, cwd, permission_mode
  - hook_event_name, tool_name, tool_input (嵌套)
  - tool_use_id (模拟字段，目前 PermissionRequest 不提供，未来可能支持)

EOF
}

# 主流程
main() {
    print_separator

    # 检查参数
    if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
        show_help
        exit 0
    fi

    print_info "测试配置"
    echo -e "  工具类型: ${YELLOW}$TOOL_TYPE${NC}"
    echo -e "  命令/路径: ${YELLOW}$COMMAND${NC}"
    if [ -n "$CONTENT" ]; then
        echo -e "  内容: ${YELLOW}$CONTENT${NC}"
    fi
    if [ -n "$DESCRIPTION" ]; then
        echo -e "  描述: ${YELLOW}$DESCRIPTION${NC}"
    fi
    echo -e "  项目目录: ${YELLOW}$PROJECT_DIR${NC}"
    echo -e "  Session ID: ${YELLOW}$SESSION_ID${NC}"
    echo -e "  时间: ${YELLOW}$TIMESTAMP${NC}"
    print_separator

    # 检查 hook 脚本是否存在
    if [ ! -f "$HOOK_SCRIPT" ]; then
        print_error "找不到 hook 脚本: $HOOK_SCRIPT"
        exit 1
    fi

    # 确保脚本可执行
    chmod +x "$HOOK_SCRIPT"

    # 生成 JSON 输入
    print_info "生成测试 JSON (符合 Claude Code PermissionRequest hook 格式)..."
    INPUT_JSON=$(generate_input_json "$TOOL_TYPE" "$COMMAND" "$CONTENT" "$DESCRIPTION")
    echo -e "${BLUE}$INPUT_JSON${NC}"
    print_separator

    # 检查环境变量（使用 get_config 从 .env 文件读取）
    FEISHU_WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "$FEISHU_WEBHOOK_URL")
    CALLBACK_SERVER_URL=$(get_config "CALLBACK_SERVER_URL" "$CALLBACK_SERVER_URL")

    if [ -z "$FEISHU_WEBHOOK_URL" ]; then
        print_warning "FEISHU_WEBHOOK_URL 未设置，将回退到终端交互模式"
    fi

    if [ -z "$CALLBACK_SERVER_URL" ]; then
        export CALLBACK_SERVER_URL="http://localhost:8080"
        print_info "设置 CALLBACK_SERVER_URL=$CALLBACK_SERVER_URL"
    fi

    # 执行测试
    print_info "执行 hook-router.sh..."
    print_separator

    echo "$INPUT_JSON" | "$HOOK_SCRIPT"
    EXIT_CODE=$?

    print_separator
    print_info "脚本退出码: $EXIT_CODE"

    case $EXIT_CODE in
        0)
            echo -e "  ${GREEN}EXIT_HOOK_SUCCESS (0)${NC} - Hook 成功执行，使用输出的决策"
            ;;
        1)
            echo -e "  ${YELLOW}EXIT_FALLBACK (1)${NC} - 回退到终端交互模式"
            ;;
        2)
            echo -e "  ${RED}EXIT_HOOK_ERROR (2)${NC} - Hook 执行错误"
            ;;
        *)
            echo -e "  ${RED}未知退出码: $EXIT_CODE${NC}"
            ;;
    esac

    print_separator
}

# 运行主流程
main "$@"
