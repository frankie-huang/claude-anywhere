#!/bin/bash
# =============================================================================
# install.sh - Claude Code 飞书通知安装脚本
#
# 功能：
#   1. 检测环境依赖（python3, curl 等）
#   2. 配置 Claude Code hook
#   3. 生成环境变量配置模板
#
# 用法：
#   ./install.sh              # 交互式安装
#   ./install.sh --check      # 仅检测环境
#   ./install.sh --uninstall  # 卸载 hook 配置
#   ./install.sh --help       # 显示帮助
# =============================================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# 脚本目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_PATH="${SCRIPT_DIR}/src/hook-router.sh"
SETTINGS_FILE="${HOME}/.claude/settings.json"

# =============================================================================
# 辅助函数
# =============================================================================

print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║       Claude Code 飞书通知 - 安装脚本                         ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# 打印分段标题
print_section() {
    echo ""
    echo -e "${CYAN}━━━ ${BOLD}$1${NC}${CYAN} ━━━${NC}"
    echo ""
}

# 打印高亮文本（用于重要路径、命令等）
print_highlight() {
    echo -e "${BOLD}${YELLOW}$1${NC}"
}

# 打印次要信息（灰色）
print_dim() {
    echo -e "${DIM}$1${NC}"
}

# =============================================================================
# 检测依赖
# =============================================================================

check_dependencies() {
    print_section "检测环境依赖"

    local missing=()
    local optional_missing=()

    # 必需依赖
    local required_deps=("python3" "curl" "bash")
    for dep in "${required_deps[@]}"; do
        if command -v "$dep" &>/dev/null; then
            print_success "$dep: $("$dep" --version 2>&1 | head -1)"
        else
            print_error "$dep: 未安装"
            missing+=("$dep")
        fi
    done

    # 可选依赖 - 格式: "命令:未安装时的说明"
    local optional_deps=(
        "jq:将使用 Python 作为降级方案"
        "socat:将使用 Python socket_client"
    )
    for entry in "${optional_deps[@]}"; do
        local dep="${entry%%:*}"
        local desc="${entry#*:}"
        if command -v "$dep" &>/dev/null; then
            print_success "$dep: 已安装 (可选)"
        else
            print_info "$dep: 未安装 (可选，$desc)"
            optional_missing+=("$dep")
        fi
    done

    echo ""

    if [ ${#missing[@]} -gt 0 ]; then
        print_error "缺少必需依赖: ${missing[*]}"
        echo ""
        echo "请安装缺少的依赖后重试："
        echo "  Ubuntu/Debian: sudo apt install ${missing[*]}"
        echo "  CentOS/RHEL:   sudo yum install ${missing[*]}"
        echo "  macOS:         brew install ${missing[*]}"
        return 1
    fi

    print_success "所有必需依赖已满足"

    if [ ${#optional_missing[@]} -gt 0 ]; then
        print_info "可选依赖 (${optional_missing[*]}) 未安装，但不影响基本功能"
    fi

    return 0
}

# =============================================================================
# 检测超时配置
# =============================================================================

check_timeout_config() {
    print_section "检测超时配置"

    local env_file="${SCRIPT_DIR}/.env"
    local server_timeout=600  # 默认值
    local hook_timeout=0

    # 读取 .env 中的 PERMISSION_REQUEST_TIMEOUT
    if [ -f "$env_file" ]; then
        local env_timeout
        env_timeout=$(grep -E "^PERMISSION_REQUEST_TIMEOUT=" "$env_file" 2>/dev/null | cut -d'=' -f2)
        if [ -n "$env_timeout" ]; then
            server_timeout="$env_timeout"
        fi
    fi

    # 读取 settings.json 中的 PermissionRequest hook timeout
    if [ -f "$SETTINGS_FILE" ]; then
        hook_timeout=$(SETTINGS_FILE="$SETTINGS_FILE" HOOK_PATH="$HOOK_PATH" python3 << 'PYTHON_SCRIPT' 2>/dev/null
import json
import os

settings_file = os.environ.get('SETTINGS_FILE', '')
hook_path = os.environ.get('HOOK_PATH', '')

try:
    with open(settings_file, 'r') as f:
        config = json.load(f)
except:
    print(0)
    exit(0)

hooks = config.get('hooks', {})
perm_hooks = hooks.get('PermissionRequest', [])
max_timeout = 0
for entry in perm_hooks:
    for hook in entry.get('hooks', []):
        if hook.get('command') == hook_path:
            t = hook.get('timeout', 0)
            if t > max_timeout:
                max_timeout = t
print(max_timeout)
PYTHON_SCRIPT
        )
    fi

    # 显示配置信息
    echo "  服务端超时 (PERMISSION_REQUEST_TIMEOUT): ${server_timeout}s"
    echo "  Hook 超时 (settings.json): ${hook_timeout}s"
    echo ""

    # 检查配置是否合理
    # 服务端超时为 0 表示禁用，此时无需检查
    if [ "$server_timeout" -eq 0 ]; then
        print_info "服务端超时已禁用 (PERMISSION_REQUEST_TIMEOUT=0)"
        print_info "Hook 超时将由 Claude Code 默认值控制"
        return 0
    fi

    # Hook 超时为 0 表示未配置或使用默认值
    if [ "$hook_timeout" -eq 0 ]; then
        print_warning "未找到 Hook 超时配置"
        print_info "建议在 settings.json 中配置 PermissionRequest hook timeout"
        print_info "推荐值: $((server_timeout + 60))s (服务端超时 + 60s)"
        return 0
    fi

    # 检查大小关系（相等也有竞态风险）
    if [ "$hook_timeout" -le "$server_timeout" ]; then
        if [ "$hook_timeout" -eq "$server_timeout" ]; then
            print_warning "Hook 超时 == 服务端超时 (${hook_timeout}s)，存在竞态风险"
        else
            print_warning "Hook 超时 (${hook_timeout}s) < 服务端超时 (${server_timeout}s)"
        fi
        echo ""
        print_info "Hook 可能被 Claude Code 强制终止，导致服务端超时响应无法发送"
        print_info "建议修改 settings.json 中的 PermissionRequest timeout 为 $((server_timeout + 60))s 或更大"
        return 0
    fi

    print_success "超时配置合理: Hook 超时 > 服务端超时"
    return 0
}

# =============================================================================
# 配置 Hook
# =============================================================================

configure_hook() {
    print_section "配置 Claude Code Hook"

    # 检查 hook 脚本是否存在
    if [ ! -f "$HOOK_PATH" ]; then
        print_error "Hook 脚本不存在: $HOOK_PATH"
        return 1
    fi

    # 确保脚本可执行
    chmod +x "$HOOK_PATH"

    # 创建 settings 目录
    mkdir -p "$(dirname "$SETTINGS_FILE")"

    # 使用 Python 统一处理配置（新建或合并，仅增量写入缺失的 hook 事件）
    # PermissionRequest timeout 需大于服务端 PERMISSION_REQUEST_TIMEOUT（默认 600 秒）
    # 冲突时通过 /dev/tty 读取用户输入（因 stdin 被 heredoc 占用）
    SETTINGS_FILE="$SETTINGS_FILE" HOOK_PATH="$HOOK_PATH" python3 << 'PYTHON_SCRIPT'
import json
import os

GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
DIM = '\033[2m'
NC = '\033[0m'

settings_file = os.environ.get('SETTINGS_FILE', '')
hook_path = os.environ.get('HOOK_PATH', '')

# 期望写入的 hooks 配置
# 注意: Stop 事件不要配置 async: true
#   - stop.sh 内部已用 `&` 后台执行通知，配置 async 可能导致进程提前终止
#   - 双层 async 会使后台通知进程在父进程退出时被 kill，导致通知不稳定
desired_hooks = {
    "PermissionRequest": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": hook_path,
            "timeout": 660
        }]
    }],
    "Stop": [{
        "hooks": [{
            "type": "command",
            "command": hook_path
        }]
    }]
}

try:
    with open(settings_file, 'r') as f:
        config = json.load(f)
except:
    config = {}

if 'hooks' not in config:
    config['hooks'] = {}

for event, desired in desired_hooks.items():
    if event not in config['hooks']:
        # 事件未配置，直接写入
        config['hooks'][event] = desired
        print(f"{GREEN}✓{NC} {event} 配置已添加")
    else:
        # 事件已存在，检查是否指向我们的目标脚本
        has_our_hook = any(
            hook.get('command') == hook_path
            for entry in config['hooks'][event]
            for hook in entry.get('hooks', [])
        )
        if has_our_hook:
            print(f"{GREEN}✓{NC} {event} 已配置，跳过")
        else:
            print(f"{YELLOW}⚠{NC} {event} 已配置了其他 hook")
            print(f"  当前配置：")
            print(f"{DIM}{json.dumps(config['hooks'][event], indent=2, ensure_ascii=False)}{NC}")
            print(f"  新配置：")
            print(f"{DIM}{json.dumps(desired, indent=2, ensure_ascii=False)}{NC}")
            try:
                with open('/dev/tty', 'r') as tty:
                    print(f"  是否覆盖? [y/N] ", end='', flush=True)
                    answer = tty.readline().strip()
                if answer.lower() == 'y':
                    config['hooks'][event] = desired
                    print(f"{GREEN}✓{NC} {event} 配置已覆盖")
                else:
                    print(f"  跳过 {event}")
            except:
                print(f"  跳过 {event}")

with open(settings_file, 'w') as f:
    json.dump(config, f, indent=2)
PYTHON_SCRIPT

    echo ""
    print_info "配置文件: $(print_highlight "$SETTINGS_FILE")"
    print_info "Hook 脚本: $(print_highlight "$HOOK_PATH")"
}

# =============================================================================
# 生成环境变量模板
# =============================================================================

generate_env_template() {
    local env_example="${SCRIPT_DIR}/.env.example"
    local env_file="${SCRIPT_DIR}/.env"

    print_section "生成环境变量配置"

    # 检查模板文件是否存在
    if [ ! -f "$env_example" ]; then
        print_error ".env.example 模板文件不存在: $env_example"
        return 1
    fi

    # 检查 .env 是否已存在
    if [ -f "$env_file" ]; then
        print_info ".env 配置文件已存在: $env_file"
        echo ""
        echo "如需重新生成，请先删除现有文件："
        echo "  rm $env_file"
        return 0
    fi

    cp "$env_example" "$env_file"

    print_success ".env 配置文件已生成（从 .env.example 复制）"
    echo ""
    echo "  配置文件: $(print_highlight "$env_file")"
    echo ""
    print_dim "提示：脚本会自动从 .env 文件读取配置，无需手动 source"
    echo ""
    echo "$(print_highlight "快速配置指南：")"
    echo ""
    echo "  编辑 .env 文件，至少配置以下 2 项："
    echo "    1. FEISHU_WEBHOOK_URL     - 飞书 Webhook URL"
    echo "    2. CALLBACK_SERVER_URL    - 回调服务器地址（内网穿透填公网地址）"
    echo ""
    print_dim "  其他配置使用默认值即可，有需要再按需调整"
}

# =============================================================================
# 卸载
# =============================================================================

uninstall() {
    print_section "卸载 Claude Code Hook"

    if [ ! -f "$SETTINGS_FILE" ]; then
        print_info "配置文件不存在，无需卸载"
        return 0
    fi

    # 使用 Python 精确移除指向本项目脚本的 hook（保留用户的其他 hook）
    # 退出码: 0=有变更已写入, 1=错误, 2=无需操作
    local py_exit=0
    SETTINGS_FILE="$SETTINGS_FILE" HOOK_PATH="$HOOK_PATH" python3 << 'PYTHON_SCRIPT' || py_exit=$?
import json
import os

GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
NC = '\033[0m'

settings_file = os.environ.get('SETTINGS_FILE', '')
hook_path = os.environ.get('HOOK_PATH', '')

try:
    with open(settings_file, 'r') as f:
        config = json.load(f)
except:
    print(f"{YELLOW}⚠{NC} 无法读取配置文件")
    exit(1)

if 'hooks' not in config:
    print(f"{GREEN}✓{NC} 未找到 hooks 配置，无需卸载")
    exit(2)

events_to_check = ['PermissionRequest', 'Stop']
removed = []

for event in events_to_check:
    if event not in config['hooks']:
        continue

    entries = config['hooks'][event]
    # 过滤掉包含本项目脚本的条目，保留其他 hook
    remaining = []
    for entry in entries:
        hooks = entry.get('hooks', [])
        filtered_hooks = [h for h in hooks if h.get('command') != hook_path]
        if filtered_hooks:
            entry['hooks'] = filtered_hooks
            remaining.append(entry)
        elif not hooks:
            # 没有 hooks 字段的条目，保留
            remaining.append(entry)

    if len(remaining) < len(entries):
        removed.append(event)
        if remaining:
            config['hooks'][event] = remaining
            print(f"{GREEN}✓{NC} {event} 已移除本项目 hook（保留了其他 hook）")
        else:
            del config['hooks'][event]
            print(f"{GREEN}✓{NC} {event} 已移除")
    else:
        print(f"{YELLOW}⚠{NC} {event} 未找到本项目的 hook 配置")

if not removed:
    print(f"{GREEN}✓{NC} 未找到需要移除的配置")
    exit(2)

# 有变更时才写入文件
if 'hooks' in config and len(config['hooks']) == 0:
    del config['hooks']
with open(settings_file, 'w') as f:
    json.dump(config, f, indent=2)
PYTHON_SCRIPT

    if [ $py_exit -eq 1 ]; then
        return 1
    elif [ $py_exit -eq 0 ]; then
        print_success "卸载完成"
    fi
}

# =============================================================================
# 显示帮助
# =============================================================================

show_help() {
    echo "用法: $0 [选项]"
    echo ""
    echo "选项:"
    echo "  (无参数)     交互式安装，依次执行以下操作："
    echo "                 1. 检测环境依赖 (不会自动安装，缺失时提示命令)"
    echo "                 2. 配置 hook → 写入 ~/.claude/settings.json"
    echo "                 3. 生成配置 → 复制 .env.example 为 .env"
    echo ""
    echo "  --check      仅检测环境依赖，不做任何写入"
    echo ""
    echo "  --uninstall  从 ~/.claude/settings.json 移除本项目的 hook 配置"
    echo "               仅移除本项目的 hook，保留用户的其他配置"
    echo ""
    echo "  --help       显示此帮助信息"
}

# =============================================================================
# 主函数
# =============================================================================

main() {
    case "${1:-}" in
        --check)
            print_header
            print_dim "仅检测环境依赖，不做任何写入操作"
            check_dependencies
            check_timeout_config
            ;;
        --uninstall)
            print_header
            print_dim "将从 ${SETTINGS_FILE} 移除本项目的 hook 配置（保留其他配置）"
            uninstall
            ;;
        --help|-h)
            show_help
            ;;
        "")
            print_header
            if check_dependencies; then
                configure_hook
                generate_env_template

                print_section "安装完成"

                echo -e "${GREEN}${BOLD}✓ 所有组件已就绪${NC}"
                echo ""
                echo -e "${BOLD}下一步操作：${NC}"
                echo ""
                echo "  1. 编辑配置文件："
                echo -e "     ${YELLOW}vim .env${NC}"
                echo ""
                echo "  2. 启动回调服务："
                echo -e "     ${YELLOW}./src/start-server.sh${NC}"
                echo ""
                echo "  3. 使用 Claude Code，权限请求将发送到飞书"
                echo ""
                print_dim "可选：VSCode SSH 代理 - python3 src/proxy/vscode_ssh_proxy.py --vps <host>"
            fi
            ;;
        *)
            print_error "未知选项: $1"
            show_help
            exit 1
            ;;
    esac
}

if [ "${_SOURCED_BY_SETUP:-}" != "true" ]; then
    main "$@"
fi
