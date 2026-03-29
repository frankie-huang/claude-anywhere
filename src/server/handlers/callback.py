"""
Callback 后端侧路由处理函数

POST 路由处理函数签名统一为 (data, headers) → Tuple[int, Dict[str, Any]]，
返回 (HTTP 状态码, 响应 body)，不依赖 HTTP handler 实例。
HTTP 和 WS 两种模式均可直接调用。

存储器归属: SessionChatStore, AuthTokenStore, DirHistoryStore

GET 路由:
- /status: 获取服务状态（含 WebSocket 连接状态）
- /allow, /always, /deny, /interrupt: 权限决策回调

POST 路由:
- /cb/register: 接收飞书网关通知的 auth_token
- /cb/check-owner: 验证 owner_id 是否属于该 Callback 后端
- /cb/session/get-chat-id: 根据 session_id 获取 chat_id
- /cb/session/get-last-message-id: 获取 session 的最近消息 ID
- /cb/session/set-last-message-id: 设置 session 的最近消息 ID
- /cb/session/check-skip-user-prompt: 检查并清除跳过用户 prompt 标志
- /cb/decision: 接收飞书网关转发的决策请求
- /cb/claude/new: 新建 Claude 会话
- /cb/claude/continue: 继续 Claude 会话
- /cb/claude/recent-dirs: 获取近期工作目录
- /cb/claude/browse-dirs: 浏览子目录
"""

import logging
import os
from typing import Any, Callable, Dict, Tuple

from services.auth_token import check_global_auth_token
from services.request_manager import RequestManager
from services.decision_handler import handle_decision
from config import VSCODE_URI_PREFIX
from handlers.register import handle_register_callback, handle_check_owner_id
from handlers.claude import handle_continue_session, handle_new_session
from handlers.utils import send_json, send_html_response

logger = logging.getLogger(__name__)

# Action 到 HTML 响应的映射
ACTION_HTML_RESPONSES = {
    'allow': {
        'title': '已批准运行',
        'message': '权限请求已批准，请返回终端查看执行结果。'
    },
    'always': {
        'title': '已始终允许',
        'message': '权限请求已批准，并已添加到项目的允许规则中。后续相同操作将自动允许。'
    },
    'deny': {
        'title': '已拒绝运行',
        'message': '权限请求已拒绝。Claude 可能会尝试其他方式继续工作。'
    },
    'interrupt': {
        'title': '已拒绝并中断',
        'message': '权限请求已拒绝，Claude 已停止当前任务。'
    }
}


# =============================================
# GET 路由处理函数（保留 handler 参数，不走 WS 隧道）
# =============================================

def handle_status(handler):
    """获取服务状态统计信息"""
    # 验证 X-Auth-Token（仅支持 Header，避免 URL 泄露）
    if not check_global_auth_token(handler.headers, '/status'):
        send_json(handler, 401, {'error': 'Unauthorized'})
        return

    stats = RequestManager.get_instance().get_stats()
    result = {
        'status': 'ok',
        'mode': 'socket-based (no server-side timeout)',
        **stats
    }

    # 添加 WebSocket 连接状态
    from services.ws_registry import WebSocketRegistry
    registry = WebSocketRegistry.get_instance()
    if registry:
        result['ws'] = registry.get_status()

    send_json(handler, 200, result)


def handle_action(handler, request_id, action):
    """处理权限决策动作（GET /allow, /deny, /always, /interrupt）

    调用纯决策接口，根据返回结果渲染 HTML 响应页面。

    Args:
        handler: HTTP 请求处理器实例
        request_id: 请求 ID
        action: 动作类型 (allow/always/deny/interrupt)
    """
    # 先获取 VSCode URI（在决策之前，因为之后数据可能被清理）
    vscode_uri = _build_vscode_uri(handler, request_id)

    # 调用纯决策接口
    success, decision, message = handle_decision(request_id, action)

    # 根据结果渲染 HTML 响应
    if success:
        response_info = ACTION_HTML_RESPONSES.get(action, {})
        title = response_info.get('title', '操作成功')
        html_message = response_info.get('message', message)
        send_html_response(
            handler, 200, title, html_message,
            success=True,
            vscode_uri=vscode_uri
        )
    else:
        send_html_response(
            handler, 400, '操作失败', message,
            success=False
        )


def _build_vscode_uri(handler, request_id):
    """构建 VSCode URI

    Args:
        handler: HTTP 请求处理器实例
        request_id: 请求 ID

    Returns:
        VSCode URI 或空字符串（未配置时）
    """
    if not VSCODE_URI_PREFIX:
        return ''

    req_data = RequestManager.get_instance().get_request_data(request_id)
    if not req_data:
        return ''

    project_dir = req_data.get('project_dir', '')
    if not project_dir:
        return ''

    return VSCODE_URI_PREFIX + project_dir


# =============================================
# POST 路由处理函数 — 纯函数签名: (data, headers) → (status, body)
# =============================================


def handle_register_callback_route(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """接收飞书网关通知的 auth_token（网关 → Callback）"""
    handled, response = handle_register_callback(data)
    status = 200 if response.get('success') else 400
    return status, response


def handle_check_owner_id_route(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """验证 owner_id 是否属于该 Callback 后端"""
    handled, response = handle_check_owner_id(data)
    return 200, response


def handle_claude_new(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """新建 Claude 会话（飞书网关调用）"""
    if not check_global_auth_token(headers, '/cb/claude/new'):
        return 401, {'error': 'Unauthorized'}

    success, response = handle_new_session(data)
    status = 200 if success else 400
    return status, response


def handle_claude_continue(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """继续 Claude 会话（飞书网关调用）"""
    if not check_global_auth_token(headers, '/cb/claude/continue'):
        return 401, {'error': 'Unauthorized'}

    success, response = handle_continue_session(data)
    status = 200 if success else 400
    return status, response


def handle_get_chat_id(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """根据 session_id 获取对应的 chat_id（客户端调用）"""
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/get-chat-id'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '')
    if not session_id:
        return 400, {'chat_id': None}

    store = SessionChatStore.get_instance()
    chat_id = ''
    if store:
        chat_id = store.get(session_id) or ''

    return 200, {'chat_id': chat_id}


def handle_get_last_message_id(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """根据 session_id 获取对应的 last_message_id（客户端调用）"""
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/get-last-message-id'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '')
    if not session_id:
        return 400, {'last_message_id': ''}

    store = SessionChatStore.get_instance()
    last_message_id = ''
    if store:
        last_message_id = store.get_last_message_id(session_id)

    return 200, {'last_message_id': last_message_id}


def handle_set_last_message_id(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """设置 session 的 last_message_id（飞书网关调用）"""
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/set-last-message-id'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '')
    message_id = data.get('message_id', '')

    if not session_id or not message_id:
        return 400, {'success': False, 'error': 'Missing required parameters'}

    store = SessionChatStore.get_instance()
    if store:
        success = store.set_last_message_id(session_id, message_id)
        if success:
            logger.info("[callback] Set last_message_id: session=%s, message_id=%s", session_id, message_id)
            return 200, {'success': True}
        else:
            logger.warning("[callback] Failed to set last_message_id: session=%s, message_id=%s", session_id, message_id)
            return 500, {'success': False, 'error': 'Failed to set last_message_id'}
    else:
        return 500, {'success': False, 'error': 'SessionChatStore not initialized'}


def handle_record_dir_usage(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """记录目录使用（供 feishu.sh 调用）"""
    if not check_global_auth_token(headers, '/cb/claude/record-dir-usage'):
        return 401, {'error': 'Unauthorized'}

    project_dir = data.get('project_dir', '')
    if not project_dir:
        return 400, {'error': 'Missing project_dir'}

    from services.dir_history_store import DirHistoryStore
    store = DirHistoryStore.get_instance()
    if store:
        store.record_usage(project_dir)
        return 200, {'success': True}
    return 500, {'error': 'DirHistoryStore not initialized'}


def handle_check_skip_user_prompt(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """检查并清除 session 的 skip_next_user_prompt 标志

    UserPromptSubmit hook 调用此接口判断是否应跳过该 prompt 的飞书通知。
    飞书发起的会话在启动时会设置此标志，避免重复发送用户已在飞书输入的 prompt。
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/check-skip-user-prompt'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '')
    if not session_id:
        return 400, {'skip': False}

    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'skip': False, 'error': 'SessionChatStore not initialized'}

    skip = store.check_and_clear_skip_user_prompt(session_id)
    return 200, {'skip': skip}


def handle_callback_decision(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """处理纯决策请求（供飞书网关或其他服务调用）

    此接口返回纯决策结果，不包含 toast 格式。
    调用方根据返回的 success 和 decision 自行生成响应。

    HTTP 状态码语义：
        - 200: 接口正常处理（业务成功/失败通过 success 字段区分）
        - 400: 请求格式错误（缺少必要参数）
        - 401: 身份验证失败（auth_token 无效）

    Args:
        data: 请求数据
            - action: 动作类型 (allow/always/deny/interrupt/answer)
            - request_id: 请求 ID
            - project_dir: 项目目录（可选，用于 always 写入规则）
            - answers: AskUserQuestion 的答案字典（仅 action=answer 时使用）
            - questions: AskUserQuestion 的原始问题数组（仅 action=answer 时使用）
        headers: 请求头字典
    """
    # 验证 auth_token（飞书网关调用）
    if not check_global_auth_token(headers, '/cb/decision'):
        return 401, {
            'success': False,
            'decision': None,
            'message': 'Unauthorized'
        }

    action = data.get('action', '')
    request_id = data.get('request_id', '')
    project_dir = data.get('project_dir', '')
    answers = data.get('answers')
    questions = data.get('questions')

    logger.info("[cb/decision] action=%s, request_id=%s", action, request_id)

    # 验证参数（请求格式错误返回 400）
    if not action or not request_id:
        logger.warning("[cb/decision] Missing params: action=%s, request_id=%s", action, request_id)
        return 400, {
            'success': False,
            'decision': None,
            'message': '无效的请求参数'
        }

    # 调用纯决策接口
    success, decision, message = handle_decision(
        request_id, action, project_dir,
        answers=answers, questions=questions
    )

    logger.info(
        "[cb/decision] result: request_id=%s, success=%s, decision=%s, message=%s",
        request_id, success, decision, message
    )

    # 返回 JSON 响应（业务成功/失败统一返回 200，通过 success 字段区分）
    return 200, {
        'success': success,
        'decision': decision,
        'message': message
    }


def handle_recent_dirs(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """获取近期常用工作目录列表"""
    if not check_global_auth_token(headers, '/cb/claude/recent-dirs'):
        return 401, {'error': 'Unauthorized'}

    try:
        limit = int(data.get('limit', 5))
    except (TypeError, ValueError):
        limit = 5

    from services.dir_history_store import DirHistoryStore
    store = DirHistoryStore.get_instance()
    recent_dirs = store.get_recent_dirs(limit) if store else []

    return 200, {'dirs': recent_dirs}


def handle_browse_dirs(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """浏览指定路径下的子目录"""
    if not check_global_auth_token(headers, '/cb/claude/browse-dirs'):
        return 401, {'error': 'Unauthorized'}

    # 解析参数
    request_path = data.get('path', '')

    # 默认起始路径为根目录
    if not request_path:
        request_path = '/'

    # 规范化路径（消除 .. 、符号链接等）
    request_path = os.path.realpath(request_path)

    # 验证路径必须是绝对路径
    if not request_path.startswith('/'):
        logger.warning("[browse-dirs] Path must be absolute: %s", request_path)
        return 400, {'error': 'path must be absolute'}

    # 验证路径存在且可访问
    if not os.path.isdir(request_path):
        logger.warning("[browse-dirs] Path not found or not accessible: %s", request_path)
        return 400, {'error': 'path not found or not accessible'}

    try:
        # 获取父目录路径（去除末尾斜杠，但保留根目录的 /）
        current_path = request_path.rstrip('/') if request_path != '/' else '/'
        parent_path = os.path.dirname(current_path) if current_path != '/' else ''

        # 列出子目录，过滤隐藏目录和文件
        dirs = []
        try:
            entries = os.listdir(request_path)
            for entry in entries:
                # 跳过隐藏目录（以 . 开头）
                if entry.startswith('.'):
                    continue
                # 只保留目录
                full_path = os.path.join(request_path, entry)
                if os.path.isdir(full_path):
                    dirs.append(full_path)
        except PermissionError:
            logger.warning("[browse-dirs] Permission denied: %s", request_path)
            dirs = []

        # 按目录名字母排序
        dirs.sort()

        return 200, {
            'dirs': dirs,
            'parent': parent_path,
            'current': current_path
        }
    except Exception as e:
        logger.error("[browse-dirs] Error listing directory: %s", e)
        return 500, {'error': 'internal server error'}


# =============================================
# POST 路由表 — 纯函数签名: (data, headers) → (status, body)
# =============================================

# 类型别名：POST 路由处理函数类型
PostRouteHandler = Callable[[Dict[str, Any], Dict[str, str]], Tuple[int, Dict[str, Any]]]

BACKEND_ROUTES: Dict[str, PostRouteHandler] = {
    '/cb/register': handle_register_callback_route,
    '/cb/check-owner': handle_check_owner_id_route,
    '/cb/session/get-chat-id': handle_get_chat_id,
    '/cb/session/get-last-message-id': handle_get_last_message_id,
    '/cb/session/set-last-message-id': handle_set_last_message_id,
    '/cb/session/check-skip-user-prompt': handle_check_skip_user_prompt,
    '/cb/decision': handle_callback_decision,
    '/cb/claude/new': handle_claude_new,
    '/cb/claude/continue': handle_claude_continue,
    '/cb/claude/record-dir-usage': handle_record_dir_usage,
    '/cb/claude/recent-dirs': handle_recent_dirs,
    '/cb/claude/browse-dirs': handle_browse_dirs,
}

# =============================================
# 遥测路由（从 telemetry.handler 导入）
# =============================================
from telemetry.handler import TELEMETRY_ROUTES  # noqa: E402
BACKEND_ROUTES.update(TELEMETRY_ROUTES)
