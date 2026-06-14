#!/bin/bash

# =============================================================================
# test-permission-quick.sh - 权限请求快捷测试脚本
#
# 用法: ./test-permission-quick.sh
#
# 提供交互式菜单选择常见测试场景
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_SCRIPT="${SCRIPT_DIR}/test-permission.sh"

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
RED='\033[0;31m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# 当前项目目录
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PROJECT_NAME="$(basename "$PROJECT_DIR")"

print_header() {
    clear
    echo -e "${MAGENTA}"
    cat << "EOF"
 ╔═══════════════════════════════════════════════════════════════╗
 ║                                                               ║
 ║        Claude Code 权限请求测试 - 快捷菜单                    ║
 ║        Permission Request Test - Quick Menu                   ║
 ║                                                               ║
 ╚═══════════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
    echo -e "${CYAN}项目: ${GREEN}$PROJECT_NAME${NC} ${CYAN}路径: ${GREEN}$PROJECT_DIR${NC}"
    echo ""
}

print_menu() {
    echo -e "${GREEN}请选择测试场景:${NC}"
    echo ""
    echo -e "  ${YELLOW}1.${NC} Bash - 普通命令 (echo 'Hello')"
    echo -e "  ${YELLOW}2.${NC} Bash - 文件删除 (rm -rf /tmp/test)"
    echo -e "  ${YELLOW}3.${NC} Bash - 网络请求 (curl)"
    echo -e "  ${YELLOW}4.${NC} Bash - 包管理器 (npm install)"
    echo -e "  ${YELLOW}5.${NC} Bash - Git 推送 (git push)"
    echo -e "  ${YELLOW}6.${NC} Edit - 编辑系统文件 (/etc/hosts)"
    echo -e "  ${YELLOW}7.${NC} Edit - 编辑配置文件 (.env)"
    echo -e "  ${YELLOW}8.${NC} Write - 写入文件 (/tmp/test.txt)"
    echo -e "  ${YELLOW}9.${NC} Read - 读取敏感文件 (/etc/passwd)"
    echo -e "  ${YELLOW}0.${NC} 自定义测试"
    echo -e "  ${RED}q.${NC} 退出"
    echo ""
}

run_test() {
    local tool="$1"
    local cmd="$2"
    local content="$3"
    local description="$4"

    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}执行测试: ${CYAN}$tool${NC} ${YELLOW}\"$cmd\"${NC}"
    if [ -n "$description" ]; then
        echo -e "${GREEN}描述: ${CYAN}$description${NC}"
    fi
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    if [ -n "$content" ]; then
        "$TEST_SCRIPT" "$tool" "$cmd" "$content" "$description"
    else
        "$TEST_SCRIPT" "$tool" "$cmd" "" "$description"
    fi

    echo ""
    echo -e "${GREEN}按 Enter 键继续...${NC}"
    read
}

custom_test() {
    echo -e "${GREEN}自定义测试${NC}"
    echo ""

    echo -n "工具类型 (bash/edit/write/read) [bash]: "
    read tool
    tool=${tool:-bash}

    echo -n "命令/文件路径: "
    read cmd

    local content=""
    if [ "$tool" = "write" ]; then
        echo -n "文件内容: "
        read content
    fi

    echo -n "描述 (可选): "
    read description

    if [ -n "$content" ]; then
        run_test "$tool" "$cmd" "$content" "$description"
    else
        run_test "$tool" "$cmd" "" "$description"
    fi
}

main() {
    while true; do
        print_header
        print_menu

        echo -n -e "${GREEN}请选择 [0-9/q]: ${NC}"
        read choice
        echo ""

        case "$choice" in
            1)
                run_test "bash" "echo 'Hello, World!'" "" "打印 Hello World"
                ;;
            2)
                run_test "bash" "rm -rf /tmp/test" "" "删除测试目录"
                ;;
            3)
                run_test "bash" "curl -s https://api.example.com" "" "发送 HTTP 请求"
                ;;
            4)
                run_test "bash" "npm install" "" "安装项目依赖"
                ;;
            5)
                run_test "bash" "git push origin main" "" "推送到远程仓库"
                ;;
            6)
                run_test "edit" "/etc/hosts" "" "编辑系统 hosts 文件"
                ;;
            7)
                run_test "edit" "$PROJECT_DIR/.env" "" "编辑环境变量配置"
                ;;
            8)
                run_test "write" "/tmp/test.txt" "这是测试内容\n第二行内容" "写入测试文件"
                ;;
            9)
                run_test "read" "/etc/passwd" "" "读取系统用户信息"
                ;;
            0)
                custom_test
                ;;
            q|Q)
                echo -e "${GREEN}退出测试${NC}"
                exit 0
                ;;
            *)
                echo -e "${RED}无效选择，请重试${NC}"
                sleep 1
                ;;
        esac
    done
}

main
