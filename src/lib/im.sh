#!/bin/bash
# =============================================================================
# src/lib/im.sh - IM 平台路由中间层
# =============================================================================
#
# 功能说明:
#   Hook 脚本与具体 IM 平台之间的唯一接口层。
#   Hook 只描述"要做什么"（同步 prompt、发通知、查 chat_id），
#   由本文件按 IM_PLATFORM 分发到对应平台的实现。
#
# 加载顺序:
#   1. callback.sh   - 平台无关的 Callback 后端客户端，所有平台共用
#   2. <platform>.sh - 按 IM_PLATFORM 加载对应平台实现
#
# 统一接口（Hook 脚本只调这些，不感知平台）:
#   im_channel_ready()               - 发送渠道是否就绪
#   im_get_owner_id()                - 当前平台的 owner id
#   get_chat_id()                    - 查询 session 的 chat_id（同时检测 muted）
#   send_user_prompt_notification()  - 同步用户 prompt 到 IM
#   send_permission_notification()   - 发送权限审批通知
#   send_ask_question_notification() - 发送 AskUserQuestion 通知
#   send_stop_notification()         - 发送任务完成通知
#
# 新增平台:
#   1. 新建 src/lib/<platform>.sh，实现 _<platform>_* 系列函数
#   2. 在下方各 case 分支中注册
#   Hook 脚本无需改动。
#
# 说明:
#   统一接口函数接收"业务数据"（非平台产物如卡片 JSON），平台特有的构建与
#   发送逻辑都在各平台的 _<platform>_* 实现内部。部分接口依赖 Hook 脚本中
#   已设置的变量（bash 动态作用域），在各函数注释中列出。
#
# =============================================================================

# 确保 core.sh 已加载（提供 get_config / log / LIB_DIR）
if ! type get_config &> /dev/null; then
    echo "[im.sh] ERROR: core.sh must be sourced before im.sh" >&2
    return 1
fi

# 读取 IM 平台配置（未设置默认 feishu）
IM_PLATFORM=$(get_config "IM_PLATFORM" "feishu")
[ -z "$IM_PLATFORM" ] && IM_PLATFORM="feishu"

# 加载平台无关的 Callback 后端客户端（Hook 脚本和各平台实现都依赖）
if ! type do_callback_post &> /dev/null; then
    source "$LIB_DIR/callback.sh"
fi

# 加载对应平台的实现
case "$IM_PLATFORM" in
    feishu)
        source "$LIB_DIR/feishu.sh"
        ;;
    *)
        # 配置错误：fail-fast 终止 hook，不带着无效平台继续跑
        # log_error 只写日志文件，同时输出到 stderr 才能让用户看到原因
        echo "[im.sh] ERROR: Unknown IM_PLATFORM: $IM_PLATFORM (expected: feishu)" >&2
        log_error "[im.sh] Unknown IM_PLATFORM: $IM_PLATFORM (expected: feishu)"
        exit 1
        ;;
esac


# =============================================================================
# 平台能力查询
# =============================================================================

# ----------------------------------------------------------------------------
# im_channel_ready - 当前平台的发送渠道是否就绪
# ----------------------------------------------------------------------------
# 供 Hook 决定"能否发通知"，不就绪时回退终端交互 / 跳过通知
#
# 返回:
#   0 - 渠道就绪
#   1 - 渠道未配置
# ----------------------------------------------------------------------------
im_channel_ready() {
    case "$IM_PLATFORM" in
        feishu)
            _feishu_channel_ready
            ;;
        *)
            return 1
            ;;
    esac
}

# ----------------------------------------------------------------------------
# im_get_owner_id - 当前平台配置的 owner id
# ----------------------------------------------------------------------------
# 供 Hook 构建卡片按钮的鉴权字段（只有 owner 能点自己的审批按钮）
#
# 输出: owner id 字符串，未配置返回空
# ----------------------------------------------------------------------------
im_get_owner_id() {
    case "$IM_PLATFORM" in
        feishu)
            get_config "FEISHU_OWNER_ID" ""
            ;;
        *)
            echo ""
            ;;
    esac
}


# =============================================================================
# chat_id 查询
# =============================================================================

# ----------------------------------------------------------------------------
# get_chat_id - 查询 session 的 chat_id（同时检测 muted）
# ----------------------------------------------------------------------------
# Hook 端统一入口，一次后端调用同时拿到 muted 状态和 chat_id
#
# 参数:
#   $1 - session_id
#   $2 - project_dir（可选）
#
# 输出:
#   MUTED_SENTINEL - session 已 muted，调用方应跳过发送
#   <chat_id>      - 解析到的 chat_id
#   ""             - 未 muted 且无 chat_id
#
# 分发（两个目标分处不同层，命名约定不同）:
#   feishu → _resolve_chat_id  feishu.sh 平台内部实现（带 _：含 ensure_chat /
#                              FEISHU_CHAT_ID 兜底，hook 不应直调）
#   其他   → query_chat_id     callback.sh 公共工具（无 _：通用后端查询，只查一次）
# ----------------------------------------------------------------------------
get_chat_id() {
    case "$IM_PLATFORM" in
        feishu)
            _resolve_chat_id "$@"
            ;;
        *)
            # 不需要 chat_id 兜底的平台：通用查询即可
            query_chat_id "$@"
            ;;
    esac
}


# =============================================================================
# User prompt 同步（user_prompt.sh 调用）
# =============================================================================

# ----------------------------------------------------------------------------
# send_user_prompt_notification - 同步用户 prompt 到 IM
# ----------------------------------------------------------------------------
# 让用户在 IM 侧也能看到从终端发起的 prompt
#
# 参数:
#   $1 - message_text  prompt 文本（调用方已完成截断）
#
# 依赖变量（由 user_prompt.sh 设置）:
#   SESSION_ID, PROJECT_DIR, RESOLVED_CHAT_ID
# ----------------------------------------------------------------------------
send_user_prompt_notification() {
    local message_text="${1:-}"

    case "$IM_PLATFORM" in
        feishu)
            _feishu_send_user_prompt "$message_text"
            ;;
        *)
            log_error "[im.sh] Unsupported IM platform: $IM_PLATFORM"
            return 1
            ;;
    esac
}


# =============================================================================
# 权限审批相关（permission.sh 调用）
# =============================================================================

# ----------------------------------------------------------------------------
# send_permission_notification - 发送权限审批通知
# ----------------------------------------------------------------------------
# 参数:
#   $1 - custom_footer_hint  自定义底部提示（可选，空则由平台取默认文案）
#   $2 - no_buttons          "true" 表示不生成交互按钮（降级模式）
#
# 依赖变量（由 permission.sh 设置）:
#   TOOL_NAME, PROJECT_NAME, TIMESTAMP, COMMAND_CONTENT, DESCRIPTION,
#   TEMPLATE_COLOR, SESSION_ID, PROJECT_DIR, REQUEST_ID, OWNER_ID, RESOLVED_CHAT_ID
# ----------------------------------------------------------------------------
send_permission_notification() {
    local custom_footer_hint="${1:-}"
    local no_buttons="${2:-false}"

    case "$IM_PLATFORM" in
        feishu)
            _feishu_send_permission_notification "$custom_footer_hint" "$no_buttons"
            ;;
        *)
            log_error "[im.sh] Unsupported IM platform: $IM_PLATFORM"
            return 1
            ;;
    esac
}

# ----------------------------------------------------------------------------
# send_ask_question_notification - 发送 AskUserQuestion 通知
# ----------------------------------------------------------------------------
# 参数:
#   $1 - questions_json  AskUserQuestion 的 questions 数组 JSON
#
# 返回:
#   0 - 卡片构建成功（发送失败由平台内部降级处理，不上报）
#   1 - 卡片构建失败（permission.sh 据此回退终端）
#
# 依赖变量（由 permission.sh 设置）:
#   PROJECT_NAME, TIMESTAMP, SESSION_ID, PROJECT_DIR, REQUEST_ID, OWNER_ID,
#   RESOLVED_CHAT_ID
# ----------------------------------------------------------------------------
send_ask_question_notification() {
    local questions_json="${1:-}"

    case "$IM_PLATFORM" in
        feishu)
            _feishu_send_ask_question "$questions_json"
            ;;
        *)
            log_error "[im.sh] Unsupported IM platform: $IM_PLATFORM"
            return 1
            ;;
    esac
}


# =============================================================================
# 完成通知（stop.sh 调用）
# =============================================================================

# ----------------------------------------------------------------------------
# send_stop_notification - 发送任务完成通知
# ----------------------------------------------------------------------------
# 参数:
#   $1 - response_content  响应正文 {"texts":[...],"truncated":bool}
#                          由 callback.sh 的 build_response_content 产出
#   $2 - thinking          思考过程文本（另一条内容通道，调用方已完成截断，可选）
#
# 依赖变量（由 stop.sh 设置）:
#   PROJECT_NAME, TIMESTAMP, SESSION_ID, PROJECT_DIR, RESOLVED_CHAT_ID
# ----------------------------------------------------------------------------
send_stop_notification() {
    local response_content="${1:-}"
    local thinking="${2:-}"

    case "$IM_PLATFORM" in
        feishu)
            _feishu_send_stop_notification "$response_content" "$thinking"
            ;;
        *)
            log_error "[im.sh] Unsupported IM platform: $IM_PLATFORM"
            return 1
            ;;
    esac
}
