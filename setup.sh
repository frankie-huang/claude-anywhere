#!/bin/bash
# =============================================================================
# setup.sh - Claude Code 飞书通知一键安装脚本
#
# 功能：
#   1. 自动下载源码（如不在源码目录）
#   2. 执行 install.sh 的依赖检测和 hook 配置
#   3. 自动配置 .env 为 OpenAPI 模式并写入飞书凭证
#
# 用法：
#   单机模式：./setup.sh --app-id=cli_xxx --app-secret=xxx [--verification-token=xxx] [--owner-id=xxx] [--port=8080]
#   分离模式：./setup.sh --gateway-url=ws://host:port --owner-id=xxx [--port=8080]
#   更新：    ./setup.sh update
#   ./setup.sh --help
#
# 单机模式 lark-oapi 安装逻辑：
# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  已安装 lark-oapi?                                                          │
# │     └─ 是 ──→ 使用长连接模式 ✓                                              │
# │     └─ 否 ──→ 有 --verification-token?                                      │
# │                 ├─ 是（HTTP 回调模式可选）                                   │
# │                 │    ├─ Python < 3.8 ──→ 提示升级，继续使用 HTTP 回调 ✓      │
# │                 │    └─ Python ≥ 3.8 ──→ 询问是否安装 lark-oapi             │
# │                 │         ├─ 安装成功 ──→ 使用长连接模式 ✓                   │
# │                 │         └─ 不安装/失败 ──→ 使用 HTTP 回调模式 ✓            │
# │                 └─ 否（必须安装 lark-oapi 或提供 token）                     │
# │                      ├─ Python < 3.8 ──→ 提示升级或提供 token，退出 ✗        │
# │                      └─ Python ≥ 3.8 ──→ 询问是否安装 lark-oapi             │
# │                           ├─ 安装成功 ──→ 使用长连接模式 ✓                   │
# │                           └─ 不安装/失败 ──→ 提示提供 token，退出 ✗          │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# 分离模式（--gateway-url）：
#   不涉及 lark-oapi 安装，直接连接网关服务 ✓
#
# 简要说明：
#   有 token 时：lark-oapi 安装是可选的，失败/不安装都可用 HTTP 回调模式
#   无 token 时：必须安装 lark-oapi，否则需提供 token 后重试
#
# 完整文档：参见 docs/deploy/DEPLOYMENT_OPENAPI.md 的「一键安装」章节
# =============================================================================

set -e
set -o pipefail

# =============================================================================
# 颜色定义（需要在使用前定义，不能依赖 install.sh 中的定义）
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# =============================================================================
# 解析命令行参数
# =============================================================================

ACTION=""
APP_ID=""
APP_SECRET=""
VERIFICATION_TOKEN=""
GATEWAY_URL=""
OWNER_ID=""
PORT=""
SHOW_HELP=false

for arg; do
    case "$arg" in
        update)
            ACTION="update"
            ;;
        start|stop|restart|status)
            ACTION="$arg"
            ;;
        --app-id=*)
            APP_ID="${arg#*=}"
            ;;
        --app-secret=*)
            APP_SECRET="${arg#*=}"
            ;;
        --verification-token=*)
            VERIFICATION_TOKEN="${arg#*=}"
            ;;
        --gateway-url=*)
            GATEWAY_URL="${arg#*=}"
            ;;
        --owner-id=*)
            OWNER_ID="${arg#*=}"
            ;;
        --port=*)
            PORT="${arg#*=}"
            ;;
        --help|-h)
            SHOW_HELP=true
            ;;
        *)
            echo "未知参数: $arg"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# =============================================================================
# 帮助信息
# =============================================================================

SCRIPT_NAME="setup.sh"

if [ "$SHOW_HELP" = true ] || [ $# -eq 0 ]; then
    echo "用法: ./$SCRIPT_NAME <部署模式参数> [选项]"
    echo ""
    echo "一键安装 Claude Code 飞书通知（OpenAPI 模式）"
    echo ""
    echo "单机模式（本机配置飞书应用凭证）:"
    echo "  --app-id=ID                 飞书应用 App ID（必填）"
    echo "  --app-secret=SECRET         飞书应用 App Secret（必填）"
    echo "  --verification-token=TOKEN  飞书 Verification Token（HTTP 回调模式需要）"
    echo "                              如已安装 lark-oapi（长连接模式），可不填"
    echo ""
    echo "分离模式（通过网关连接，无需飞书应用凭证）:"
    echo "  --gateway-url=URL           飞书网关地址（必填，如 ws://host:port）"
    echo "  --owner-id=ID               飞书用户 ID（必填）"
    echo ""
    echo "更新:"
    echo "  update                      拉取最新代码并重启服务（需在源码目录执行）"
    echo ""
    echo "服务管理:"
    echo "  start                       启动服务"
    echo "  stop                        停止服务"
    echo "  restart                     重启服务"
    echo "  status                      查看服务运行状态"
    echo ""
    echo "通用可选参数:"
    echo "  --owner-id=ID               飞书用户 ID（分离模式必填，单机模式可稍后配置）"
    echo "  --port=PORT                 服务端口（默认 8080）"
    echo "  --help                      显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  # 单机 - 长连接模式（已安装 lark-oapi）"
    echo "  ./$SCRIPT_NAME --app-id=cli_xxx --app-secret=xxx --owner-id=xxx"
    echo ""
    echo "  # 单机 - HTTP 回调模式（未安装 lark-oapi）"
    echo "  ./$SCRIPT_NAME --app-id=cli_xxx --app-secret=xxx --verification-token=xxx --owner-id=xxx"
    echo ""
    echo "  # 分离模式 - 通过网关"
    echo "  ./$SCRIPT_NAME --gateway-url=ws://gateway:8080 --owner-id=xxx"
    exit 0
fi

# =============================================================================
# 检测源码目录（SETUP_DIR 需要尽早定义，供后续逻辑使用）
# =============================================================================

SETUP_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOADED_SOURCE=false

# 辅助函数：检查目录是否为完整的源码目录
is_valid_source_dir() {
    local dir="$1"
    [ -f "${dir}/.env.example" ] && [ -f "${dir}/src/start-server.sh" ]
}

# 辅助函数：检查是否在源码目录执行，否则退出
require_source_dir() {
    local action="$1"
    if ! is_valid_source_dir "$SETUP_DIR"; then
        echo "错误: $action 需要在源码目录执行"
        echo ""
        echo "请先 cd 到源码目录："
        echo "  cd <源码目录> && ./$SCRIPT_NAME $action"
        exit 1
    fi
}

# 辅助函数：检查是否与其他参数混用
require_no_other_params() {
    local action="$1"
    if [ -n "$APP_ID" ] || [ -n "$APP_SECRET" ] || [ -n "$GATEWAY_URL" ] || [ -n "$OWNER_ID" ] || [ -n "$VERIFICATION_TOKEN" ] || [ -n "$PORT" ]; then
        echo "错误: $action 不能与其他参数一起使用"
        echo "用法: ./$SCRIPT_NAME $action"
        exit 1
    fi
}

# 辅助函数：打印带颜色的消息（在 source install.sh 之前使用）
print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_section() { echo "" && echo -e "${CYAN}━━━ ${BOLD}$1${NC}${CYAN} ━━━${NC}" && echo ""; }

# 辅助函数：询问是/否问题
ask_yes_no() {
    local prompt="$1"
    local default="${2:-N}"
    local hint
    case "$default" in
        y|Y) hint="[Y/n]" ;;
        *)   hint="[y/N]" ;;
    esac
    local choice
    read -p "$prompt $hint: " choice </dev/tty
    case "${choice:-$default}" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# =============================================================================
# update 子命令：校验并执行（必须在源码目录执行，提前退出避免执行后续逻辑）
# =============================================================================

if [ "$ACTION" = "update" ]; then
    require_no_other_params "update"
    require_source_dir "update"

    # 直接在源码目录执行更新
    SOURCE_DIR="$SETUP_DIR"
    set +e
    echo "正在更新..."

    # 拉取最新代码
    if [ -e "${SOURCE_DIR}/.git" ]; then
        echo ""
        GIT_PULL_OUTPUT=$(git -C "$SOURCE_DIR" pull 2>&1)
        GIT_PULL_EXIT=$?
        if [ $GIT_PULL_EXIT -eq 0 ]; then
            echo ""
            echo "代码已更新"
        else
            echo "git pull 失败: $GIT_PULL_OUTPUT"
            exit 1
        fi
    else
        echo "错误: ${SOURCE_DIR} 不是 git 仓库，无法自动更新"
        echo "请手动下载最新代码"
        exit 1
    fi

    # 重启服务
    echo ""
    if "${SOURCE_DIR}/src/start-server.sh" restart; then
        echo ""
        echo "更新完成"
    else
        echo ""
        echo "代码已更新，但服务启动失败，请检查上方错误信息"
        exit 1
    fi

    exit 0
fi

# =============================================================================
# 服务管理子命令：start/stop/restart/status（必须在源码目录执行）
# =============================================================================

case "$ACTION" in
    start|stop|restart|status)
        require_no_other_params "$ACTION"
        require_source_dir "$ACTION"

        # 执行服务管理命令
        "${SETUP_DIR}/src/start-server.sh" "$ACTION"
        exit $?
        ;;
esac

# =============================================================================
# 检测/下载源码目录
# =============================================================================

if is_valid_source_dir "$SETUP_DIR"; then
    # 已在源码目录
    SOURCE_DIR="$SETUP_DIR"
else
    # 需要下载源码
    REPO_URL="https://github.com/frankie-huang/claude-anywhere.git"
    TARGET_DIR="${SETUP_DIR}/claude-anywhere"

    # 检查目标目录是否已存在
    if [ -d "$TARGET_DIR" ]; then
        # 检查目录是否完整
        if is_valid_source_dir "$TARGET_DIR"; then
            # 目录完整，直接使用
            SOURCE_DIR="$TARGET_DIR"
            echo "检测到已有源码目录: $SOURCE_DIR"
        else
            # 目录不完整，提示用户删除
            echo "错误: 目录 ${TARGET_DIR} 已存在但不完整"
            echo "请先删除该目录后重试：rm -rf ${TARGET_DIR}"
            exit 1
        fi
    else
        # 下载源码
        echo "未检测到源码目录，正在下载..."

        if command -v git &>/dev/null; then
            if ! GIT_CLONE_OUTPUT=$(git clone "$REPO_URL" "$TARGET_DIR" 2>&1); then
                echo "git clone 失败: $GIT_CLONE_OUTPUT"
                exit 1
            fi
        elif command -v curl &>/dev/null; then
            TARBALL_URL="https://github.com/frankie-huang/claude-anywhere/archive/refs/heads/main.tar.gz"
            # 检测 tarball 解压的中间目录是否残留
            if [ -d "${SETUP_DIR}/claude-anywhere-main" ]; then
                echo "错误: 检测到上次下载残留目录 ${SETUP_DIR}/claude-anywhere-main"
                echo "请先删除后重试：rm -rf ${SETUP_DIR}/claude-anywhere-main"
                exit 1
            fi
            # 下载并解压（使用管道避免二进制数据存储在变量中）
            if ! curl -fsSL --connect-timeout 30 "$TARBALL_URL" | tar xz -C "$SETUP_DIR" 2>&1; then
                echo "下载或解压源码失败"
                # 清理可能残留的不完整目录
                rm -rf "${SETUP_DIR}/claude-anywhere-main" 2>/dev/null
                exit 1
            fi
            # tar 解压后目录名为 claude-anywhere-main
            if [ -d "${SETUP_DIR}/claude-anywhere-main" ]; then
                mv "${SETUP_DIR}/claude-anywhere-main" "$TARGET_DIR"
            fi
        else
            echo "错误: 需要 git 或 curl 来下载源码"
            exit 1
        fi

        if [ ! -d "$TARGET_DIR" ] || ! is_valid_source_dir "$TARGET_DIR"; then
            echo "错误: 源码下载不完整"
            exit 1
        fi

        SOURCE_DIR="$TARGET_DIR"
        DOWNLOADED_SOURCE=true
        echo "源码已下载到: $SOURCE_DIR"
    fi
fi

# 设置命令前缀（用于提示信息）
# 如果脚本不在源码目录内，提示用户需要先 cd 到源码目录
if [ "$SETUP_DIR" = "$SOURCE_DIR" ]; then
    CMD_PREFIX="./$SCRIPT_NAME"
else
    CMD_PREFIX="cd '$SOURCE_DIR' && ./$SCRIPT_NAME"
fi

# =============================================================================
# 加载 install.sh 的函数
# =============================================================================

INSTALL_SCRIPT="${SOURCE_DIR}/install.sh"

if [ ! -f "$INSTALL_SCRIPT" ]; then
    echo "错误: 找不到 install.sh: $INSTALL_SCRIPT"
    exit 1
fi

# source install.sh 中的函数（守卫变量阻止其执行 main）
_SOURCED_BY_SETUP=true source "$INSTALL_SCRIPT"

# source 后重新设置路径（install.sh 的顶层赋值会覆盖）
SCRIPT_DIR="$SOURCE_DIR"
HOOK_PATH="${SCRIPT_DIR}/src/hook-router.sh"
SETTINGS_FILE="${HOME}/.claude/settings.json"

# 辅助函数：从 .env 文件读取配置值（支持空值）
get_env_value() {
    local key="$1"
    local file="$2"
    sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -1
}

# 辅助函数：检查 Python 版本是否 >= 指定版本
check_python_version_ge() {
    local required="$1"
    python3 -c "import sys; exit(0 if sys.version_info >= tuple(map(int, '$required'.split('.'))) else 1)"
}

# 辅助函数：获取当前 Python 版本字符串
get_python_version() {
    python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

# 辅助函数：安装 lark-oapi
install_lark_oapi() {
    print_info "正在安装 lark-oapi..."
    if python3 -m pip install lark-oapi; then
        HAS_LARK_OAPI=true
        print_success "lark-oapi 安装成功"
        return 0
    else
        print_error "安装失败，请手动安装后重试"
        echo "  python3 -m pip install lark-oapi"
        return 1
    fi
}

# 辅助函数：提示需要 --verification-token 并退出
require_verification_token() {
    print_error "HTTP 回调模式需要 --verification-token 参数"
    echo ""
    echo "用法: $CMD_PREFIX --app-id=$APP_ID --app-secret=<App Secret> --verification-token=<Token>"
    exit 1
}

# =============================================================================
# 执行依赖检测
# =============================================================================

# 覆盖 install.sh 的 print_header
print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║       Claude Code 飞书通知 - 一键安装脚本                     ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_header

if ! check_dependencies; then
    print_error "依赖检测失败，请先安装缺失的依赖后重试"
    exit 1
fi

# =============================================================================
# 配置 Hook
# =============================================================================

if ! configure_hook; then
    print_error "Hook 配置失败"
    exit 1
fi

# =============================================================================
# 配置 .env 文件
# =============================================================================

print_section "配置 .env 文件"

ENV_FILE="${SOURCE_DIR}/.env"
ENV_EXAMPLE="${SOURCE_DIR}/.env.example"

# 如果 .env 不存在，从 .env.example 复制
if [ ! -f "$ENV_FILE" ]; then
    if [ ! -f "$ENV_EXAMPLE" ]; then
        print_error ".env.example 模板文件不存在"
        exit 1
    fi
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    print_success ".env 文件已从模板创建"
else
    print_info ".env 文件已存在，将在现有文件上更新配置"
fi

# 检测 .env 中是否存在重复的配置项
DUPLICATE_KEYS=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | cut -d= -f1 | sort | uniq -d)
if [ -n "$DUPLICATE_KEYS" ]; then
    print_error ".env 文件中存在重复的配置项："
    for dup_key in $DUPLICATE_KEYS; do
        echo "  - $dup_key"
    done
    echo ""
    echo "请编辑 $ENV_FILE，移除重复的配置项后重试"
    exit 1
fi

# 如果用户未传 --owner-id，尝试从 .env 中读取已有值
if [ -z "$OWNER_ID" ]; then
    EXISTING_OWNER_ID=$(get_env_value "FEISHU_OWNER_ID" "$ENV_FILE")
    if [ -n "$EXISTING_OWNER_ID" ]; then
        OWNER_ID="$EXISTING_OWNER_ID"
    fi
fi

# 检测 lark-oapi 是否已安装
HAS_LARK_OAPI=false
if python3 -c "import lark_oapi" 2>/dev/null; then
    HAS_LARK_OAPI=true
    print_success "lark-oapi 已安装（支持长连接模式）"
fi

# 确定部署模式（单机模式优先）
if [ -n "$APP_ID" ] && [ -n "$APP_SECRET" ] && [ -n "$GATEWAY_URL" ]; then
    print_warning "--gateway-url 与 --app-id/--app-secret 不能同时使用，已忽略 --gateway-url"
    GATEWAY_URL=""
fi

if [ -n "$GATEWAY_URL" ]; then
    DEPLOY_MODE="gateway"
elif [ -n "$APP_ID" ] && [ -n "$APP_SECRET" ]; then
    DEPLOY_MODE="standalone"
else
    print_error "缺少必填参数"
    echo ""
    echo "单机模式: $CMD_PREFIX --app-id=<App ID> --app-secret=<App Secret> [--owner-id=<用户ID>]"
    echo "分离模式: $CMD_PREFIX --gateway-url=<网关地址> --owner-id=<用户ID>"
    exit 1
fi

# 根据部署模式验证必要参数
case "$DEPLOY_MODE" in
    gateway)
        if [ -z "$OWNER_ID" ]; then
            print_error "分离模式下 --owner-id 为必填参数"
            echo ""
            echo "方式一: 在 .env 中配置 FEISHU_OWNER_ID 后重新执行"
            echo "方式二: $CMD_PREFIX --gateway-url=<网关地址> --owner-id=<用户ID>"
            exit 1
        fi
        ;;
    standalone)
        if [ "$HAS_LARK_OAPI" = false ]; then
            if [ -z "$VERIFICATION_TOKEN" ]; then
                # 无 token，必须安装 lark-oapi 或退出
                print_warning "lark-oapi 未安装（长连接模式需要）"
                echo ""

                # 检查 Python 版本是否支持 lark-oapi
                if ! check_python_version_ge "3.8"; then
                    print_error "当前 Python 版本 $(get_python_version) 过低，lark-oapi 需要 Python 3.8+"
                    echo ""
                    echo "请选择："
                    echo "  1) 升级 Python 到 3.8+ 后重新运行此脚本"
                    echo "  2) 使用 HTTP 回调模式（需要 --verification-token 参数）"
                    echo ""
                    read -p "请输入选项 [1/2]: " choice </dev/tty
                    case "$choice" in
                        1)
                            print_info "请升级 Python 后重新运行: $CMD_PREFIX $*"
                            exit 1
                            ;;
                        2)
                            require_verification_token
                            ;;
                        *)
                            print_error "无效选项"
                            exit 1
                            ;;
                    esac
                fi

                echo "请选择："
                echo "  1) 安装 lark-oapi（推荐，支持长连接模式，无需公网 IP）"
                echo "  2) 使用 HTTP 回调模式（需要 --verification-token 参数）"
                echo ""
                read -p "请输入选项 [1/2]: " choice </dev/tty
                case "$choice" in
                    1)
                        echo ""
                        if install_lark_oapi; then
                            : # 安装成功，继续执行
                        else
                            print_error "安装失败"
                            echo ""
                            echo "请选择："
                            echo "  1) 手动安装 lark-oapi 后重新运行此脚本"
                            echo "     python3 -m pip install lark-oapi"
                            echo ""
                            echo "  2) 使用 HTTP 回调模式（需要 --verification-token 参数）"
                            echo "     $CMD_PREFIX --app-id=$APP_ID --app-secret=<App Secret> --verification-token=<Token>"
                            exit 1
                        fi
                        ;;
                    2)
                        require_verification_token
                        ;;
                    *)
                        print_error "无效选项"
                        exit 1
                        ;;
                esac
            else
                # 有 token，提示可选安装
                print_warning "lark-oapi 未安装，将使用 HTTP 回调模式"
                echo ""

                if check_python_version_ge "3.8"; then
                    echo "提示: 安装 lark-oapi 可使用长连接模式（无需公网 IP）"
                    echo "  python3 -m pip install lark-oapi"
                    echo ""
                    if ask_yes_no "是否现在安装？" "N"; then
                        install_lark_oapi || print_warning "安装失败，继续使用 HTTP 回调模式"
                    fi
                else
                    print_info "当前 Python 版本 $(get_python_version)，lark-oapi 需要 Python 3.8+"
                    echo "升级 Python 后可使用长连接模式（无需公网 IP）"
                fi
            fi
        fi
        ;;
esac

# 分离模式下检测飞书凭证，询问用户是否清除
CLEAR_CREDENTIALS=""
CLEAR_GATEWAY=""
if [ "$DEPLOY_MODE" = "gateway" ]; then
    ENV_APP_ID=$(get_env_value "FEISHU_APP_ID" "$ENV_FILE")
    ENV_APP_SECRET=$(get_env_value "FEISHU_APP_SECRET" "$ENV_FILE")
    ENV_VERIFICATION_TOKEN=$(get_env_value "FEISHU_VERIFICATION_TOKEN" "$ENV_FILE")
    if [ -n "$ENV_APP_ID" ] || [ -n "$ENV_APP_SECRET" ] || [ -n "$ENV_VERIFICATION_TOKEN" ]; then
        echo ""
        print_warning "检测到 .env 中存在飞书应用凭证配置："
        [ -n "$ENV_APP_ID" ] && echo "  - FEISHU_APP_ID=$ENV_APP_ID"
        [ -n "$ENV_APP_SECRET" ] && echo "  - FEISHU_APP_SECRET=******"
        [ -n "$ENV_VERIFICATION_TOKEN" ] && echo "  - FEISHU_VERIFICATION_TOKEN=******"
        echo ""
        print_warning "分离模式下这些配置可能导致冲突"
        if ask_yes_no "是否清除这些凭证？" "N"; then
            CLEAR_CREDENTIALS="true"
            print_info "将清除飞书应用凭证"
        else
            print_info "已跳过清除，如需手动清除请编辑 $ENV_FILE"
        fi
    fi
elif [ "$DEPLOY_MODE" = "standalone" ]; then
    ENV_GATEWAY_URL=$(get_env_value "FEISHU_GATEWAY_URL" "$ENV_FILE")
    if [ -n "$ENV_GATEWAY_URL" ]; then
        echo ""
        print_warning "检测到 .env 中存在网关配置："
        echo "  - FEISHU_GATEWAY_URL=$ENV_GATEWAY_URL"
        echo ""
        print_warning "单机模式下这些配置可能导致冲突"
        if ask_yes_no "是否清除网关配置？" "N"; then
            CLEAR_GATEWAY="true"
            print_info "将清除网关配置"
        else
            print_info "已跳过清除，如需手动清除请编辑 $ENV_FILE"
        fi
    fi
fi

# 使用 Python 更新 .env 文件（避免 sed -i 跨平台问题）
ENV_FILE="$ENV_FILE" \
DEPLOY_MODE="$DEPLOY_MODE" \
APP_ID="$APP_ID" \
APP_SECRET="$APP_SECRET" \
GATEWAY_URL="$GATEWAY_URL" \
OWNER_ID="$OWNER_ID" \
VERIFICATION_TOKEN="$VERIFICATION_TOKEN" \
PORT="$PORT" \
CLEAR_CREDENTIALS="$CLEAR_CREDENTIALS" \
CLEAR_GATEWAY="$CLEAR_GATEWAY" \
python3 << 'PYTHON_SCRIPT'
import os
import re

env_file = os.environ['ENV_FILE']
deploy_mode = os.environ['DEPLOY_MODE']
app_id = os.environ.get('APP_ID', '')
app_secret = os.environ.get('APP_SECRET', '')
gateway_url = os.environ.get('GATEWAY_URL', '')
owner_id = os.environ.get('OWNER_ID', '')
verification_token = os.environ.get('VERIFICATION_TOKEN', '')
port = os.environ.get('PORT', '')
clear_credentials = os.environ.get('CLEAR_CREDENTIALS', '')
clear_gateway = os.environ.get('CLEAR_GATEWAY', '')

with open(env_file, 'r') as f:
    content = f.read()

def update_env_var(content, key, value):
    """Update a KEY=value line in .env content."""
    pattern = r'^(' + re.escape(key) + r')=.*$'
    replacement = key + '=' + value
    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
    if count == 0:
        new_content = content.rstrip('\n') + '\n' + replacement + '\n'
    return new_content

content = update_env_var(content, 'FEISHU_SEND_MODE', 'openapi')

if deploy_mode == 'standalone':
    content = update_env_var(content, 'FEISHU_APP_ID', app_id)
    content = update_env_var(content, 'FEISHU_APP_SECRET', app_secret)
    if verification_token:
        content = update_env_var(content, 'FEISHU_VERIFICATION_TOKEN', verification_token)
    # 单机模式下清除网关配置
    if clear_gateway:
        content = update_env_var(content, 'FEISHU_GATEWAY_URL', '')
else:
    content = update_env_var(content, 'FEISHU_GATEWAY_URL', gateway_url)
    # 分离模式下清除飞书应用凭证
    if clear_credentials:
        content = update_env_var(content, 'FEISHU_APP_ID', '')
        content = update_env_var(content, 'FEISHU_APP_SECRET', '')
        content = update_env_var(content, 'FEISHU_VERIFICATION_TOKEN', '')

if owner_id:
    content = update_env_var(content, 'FEISHU_OWNER_ID', owner_id)

if port:
    # 读取旧的端口值
    old_port_match = re.search(r'^CALLBACK_SERVER_PORT=(.*)$', content, re.MULTILINE)
    old_port = old_port_match.group(1).strip() if old_port_match else '8080'

    content = update_env_var(content, 'CALLBACK_SERVER_PORT', port)

    # 仅当 CALLBACK_SERVER_URL 以旧端口结尾时才更新端口
    current_url_match = re.search(r'^CALLBACK_SERVER_URL=(.*)$', content, re.MULTILINE)
    if current_url_match:
        current_url = current_url_match.group(1).strip()
        if current_url.endswith(':' + old_port) or current_url.endswith(':' + old_port + '/'):
            new_url = current_url.replace(':' + old_port, ':' + port)
            content = update_env_var(content, 'CALLBACK_SERVER_URL', new_url)

with open(env_file, 'w') as f:
    f.write(content)
PYTHON_SCRIPT

print_success "FEISHU_SEND_MODE=openapi"
if [ "$DEPLOY_MODE" = "standalone" ]; then
    print_success "FEISHU_APP_ID=$APP_ID"
    print_success "FEISHU_APP_SECRET=******"
    if [ -n "$VERIFICATION_TOKEN" ]; then
        print_success "FEISHU_VERIFICATION_TOKEN=******"
    fi
    # 如果用户选择清除网关配置，显示清除结果
    if [ "$CLEAR_GATEWAY" = "true" ]; then
        print_success "已清除 FEISHU_GATEWAY_URL"
    fi
else
    print_success "FEISHU_GATEWAY_URL=$GATEWAY_URL"
    # 如果用户选择清除凭证，显示清除结果
    if [ "$CLEAR_CREDENTIALS" = "true" ]; then
        print_success "已清除 FEISHU_APP_ID & FEISHU_APP_SECRET & FEISHU_VERIFICATION_TOKEN"
    fi
    # 非 ws/wss 协议时，CALLBACK_SERVER_URL 需要公网可访问
    case "$GATEWAY_URL" in
        ws://*|wss://*)
            ;;
        *)
            CALLBACK_URL=$(get_env_value "CALLBACK_SERVER_URL" "$ENV_FILE")
            if [ -z "$CALLBACK_URL" ] || echo "$CALLBACK_URL" | grep -qE '(^|//)(localhost|127\.[0-9]+\.[0-9]+\.[0-9]+|0\.0\.0\.0|\[::1?\])([:/]|$)'; then
                echo ""
                print_error "网关地址使用 HTTP 协议，CALLBACK_SERVER_URL 必须为公网可访问地址"
                if [ -n "$CALLBACK_URL" ]; then
                    print_error "当前值: $CALLBACK_URL"
                fi
                echo ""
                echo "请在 .env 中将 CALLBACK_SERVER_URL 配置为公网地址后重新执行"
                echo "或改用 WebSocket 协议的网关地址（ws:// 或 wss://）"
                exit 1
            fi
            ;;
    esac
fi
if [ -n "$OWNER_ID" ]; then
    print_success "FEISHU_OWNER_ID=$OWNER_ID"
fi
if [ -n "$PORT" ]; then
    print_success "CALLBACK_SERVER_PORT=$PORT"
fi

# =============================================================================
# 启动服务
# =============================================================================

print_section "启动回调服务"

SERVER_OK=true
if ! "${SOURCE_DIR}/src/start-server.sh" restart; then
    SERVER_OK=false
fi

# =============================================================================
# 完成提示
# =============================================================================

print_section "安装完成"

if [ "$SERVER_OK" = true ]; then
    echo -e "${GREEN}${BOLD}✓ 所有组件已就绪${NC}"
else
    echo -e "${YELLOW}${BOLD}⚠ 安装完成，但服务启动失败${NC}"
fi
echo ""
echo -e "${BOLD}配置结果：${NC}"
echo "  发送模式: OpenAPI"
if [ "$DEPLOY_MODE" = "standalone" ]; then
    echo "  部署模式: 单机"
    echo "  App ID:   $APP_ID"
else
    echo "  部署模式: 分离（网关）"
    echo "  网关地址: $GATEWAY_URL"
fi
if [ -n "$OWNER_ID" ]; then
    echo "  Owner ID: $OWNER_ID"
fi
if [ -n "$PORT" ]; then
    echo "  服务端口: $PORT"
fi
echo "  配置文件: $ENV_FILE"
echo ""
echo -e "${BOLD}下一步操作：${NC}"
echo ""

# 构造重启命令提示（根据脚本所在目录是否在源码目录决定是否加 cd 前缀）
if [ "$SETUP_DIR" = "$SOURCE_DIR" ]; then
    RESTART_CMD="./src/start-server.sh restart"
else
    RESTART_CMD="cd '$SOURCE_DIR' && ./src/start-server.sh restart"
fi

STEP=1
if [ "$SERVER_OK" = false ]; then
    echo "  ${STEP}. 检查上方错误信息，修复配置后重启服务："
    echo -e "     ${YELLOW}${RESTART_CMD}${NC}"
    echo ""
    STEP=$((STEP + 1))
fi

if [ -z "$OWNER_ID" ]; then
    echo "  ${STEP}. 配置飞书用户 ID（单机模式必填，网关服务可跳过）："
    echo -e "     编辑 ${YELLOW}$ENV_FILE${NC}，设置 FEISHU_OWNER_ID"
    echo -e "     修改后执行 ${YELLOW}${RESTART_CMD}${NC} 重启生效"
    echo ""
    STEP=$((STEP + 1))
fi

if [ "$DEPLOY_MODE" = "standalone" ]; then
    # 读取 .env 中的 FEISHU_EVENT_MODE
    EVENT_MODE=$(get_env_value "FEISHU_EVENT_MODE" "$ENV_FILE")
    EVENT_MODE="${EVENT_MODE:-auto}"
    echo "  ${STEP}. 在飞书开放平台配置事件与回调："
    echo ""
    if [ "$HAS_LARK_OAPI" = true ] && { [ "$EVENT_MODE" = "auto" ] || [ "$EVENT_MODE" = "longpoll" ]; }; then
        echo "     a) 事件订阅 & 卡片回调均选择「使用长连接接收事件」"
    else
        SERVER_PORT=$(get_env_value "CALLBACK_SERVER_PORT" "$ENV_FILE")
        SERVER_PORT="${SERVER_PORT:-8080}"
        echo "     a) 事件订阅 & 卡片回调均选择「将事件发送至开发者服务器」"
        echo "        请求地址填写本机公网 IP + 端口，如 http://<公网IP>:${SERVER_PORT}"
    fi
    echo ""
    echo "     b) 订阅回调事件："
    echo "        卡片回传交互 (card.action.trigger)"
    echo ""
    echo "     c) 开通应用权限："
    echo "        contact:user.employee_id:readonly"
    echo "        im:message.group_at_msg:readonly"
    echo "        im:message.p2p_msg:readonly"
    echo "        im:message.reactions:read"
    echo "        im:message.reactions:write_only"
    echo "        im:message:send_as_bot"
    echo ""
    echo "     d) 创建版本并发布应用后配置才生效"
    echo ""
    echo "     配置完成后重启服务："
    echo -e "        ${YELLOW}${RESTART_CMD}${NC}"
    echo ""
    STEP=$((STEP + 1))
fi

echo "  ${STEP}. 使用 Claude Code，通知将发送到飞书"
