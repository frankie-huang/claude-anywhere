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
- /cb/decision: 接收飞书网关转发的决策请求
- /cb/session/get-chat-id: 根据 session_id 获取 chat_id
- /cb/session/get-last-message-id: 获取 session 的最近消息 ID
- /cb/session/set-last-message-id: 设置 session 的最近消息 ID
- /cb/session/check-skip-user-prompt: 检查并清除跳过用户 prompt 标志
- /cb/session/ensure-chat: 确保 session 有 chat_id（group 模式懒创建群聊）
- /cb/session/resolve-group-chat: 通过 chat_id 反查 session_id
- /cb/session/attach: 将指定 session 绑定到目标群聊
- /cb/claude/new: 新建 Claude 会话
- /cb/claude/continue: 继续 Claude 会话
- /cb/claude/recent-dirs: 获取近期工作目录
- /cb/claude/browse-dirs: 浏览子目录
- /cb/groups/list: 列出活跃群聊
- /cb/groups/dissolve: 批量解散群聊
"""

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Tuple

from services.auth_token import check_global_auth_token
from services.request_manager import RequestManager
from services.decision_handler import handle_decision
from config import VSCODE_URI_PREFIX, PERMISSION_REQUEST_TIMEOUT
from handlers.register import handle_register_callback, handle_check_owner_id
from handlers.claude import handle_continue_session, handle_new_session
from handlers.utils import send_json, send_html_response, create_feishu_group, dissolve_feishu_groups

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
        'mode': 'socket-based (timeout: %ds)' % PERMISSION_REQUEST_TIMEOUT,
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
        chat_id = store.get_chat_id(session_id) or ''

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
# 群聊管理路由
# =============================================

# ensure-chat 并发锁：防止同一 session 同时创建多个群聊
# 注意：创建失败时故意不清理 per-session 锁。如果失败时也 pop，会出现竞态：
#   线程 B 持有旧锁对象等待中 → A 失败 pop 锁 → C 进来创建新锁 →
#   B 和 C 持有不同锁对象，互斥失效，导致同一 session 重复创建群聊。
# 失败后锁保留在 dict 中，后续线程复用同一把锁，保证互斥正确。
# 只有成功路径（chat_id 已持久化）才清理，因为后续调用在锁外首次检查即命中。
_ensure_chat_locks: Dict[str, threading.Lock] = {}
_ensure_chat_global_lock = threading.Lock()


def handle_ensure_chat(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """确保 session 有对应的 chat_id（group 模式下懒创建群聊）

    调用方:
    - 飞书网关 feishu.py: /new 命令处理后立即调用，创建失败仅 warning，不影响 session 创建
    - Shell 脚本 feishu.sh (_resolve_chat_id): 启动时调用，返回空则 fallback 到 FEISHU_CHAT_ID
    """
    from services.group_seq_store import GroupSeqStore
    from services.session_chat_store import SessionChatStore
    from config import FEISHU_SESSION_MODE, FEISHU_GROUP_NAME_PREFIX

    if not check_global_auth_token(headers, '/cb/session/ensure-chat'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')

    if not session_id:
        return 400, {'error': 'Missing session_id'}

    seq_store = GroupSeqStore.get_instance()
    session_store = SessionChatStore.get_instance()
    if not session_store or not seq_store:
        return 500, {'error': 'Store not initialized'}

    # 已有 chat_id 则直接返回
    existing = session_store.get_chat_id(session_id)
    if existing:
        return 200, {'chat_id': existing}

    # 判断是否需要创建群聊：正常流程中 session 由 handle_new_session 创建，
    # group_active 字段一定存在。session 记录不存在时（存储异常、过期被清理等）
    # 降级为全局配置，相当于将该 session 重新初始化为当前模式。
    session_data = session_store.get_session(session_id)
    group_active = session_data.get('group_active') if session_data else (FEISHU_SESSION_MODE == 'group')
    if not group_active:
        return 200, {'chat_id': ''}

    # per-session 锁防止并发创建
    with _ensure_chat_global_lock:
        if session_id not in _ensure_chat_locks:
            _ensure_chat_locks[session_id] = threading.Lock()
        lock = _ensure_chat_locks[session_id]

    with lock:
        # 二次检查（锁内）
        existing = session_store.get_chat_id(session_id)
        if existing:
            return 200, {'chat_id': existing}

        # 构建群聊名称：{前缀} - {目录名} - {MMdd HH:mm:ss}
        dir_name = os.path.basename(project_dir) if project_dir else ''
        if len(dir_name) > 30:
            dir_name = dir_name[:29] + '\u2026'  # U+2026 "…" 省略号，单字符，截断后总长仍为 30
        timestamp = time.strftime('%m%d %H:%M:%S')
        if dir_name:
            name = f"{FEISHU_GROUP_NAME_PREFIX} - {dir_name} - {timestamp}"
        else:
            name = f"{FEISHU_GROUP_NAME_PREFIX} - {timestamp}"

        # 创建群聊
        ok, result = create_feishu_group(name)
        if not ok:
            logger.error("[ensure-chat] Failed to create group: %s", result)
            return 500, {'error': 'Failed to create group: %s' % result}

        chat_id = result
        # 顺序：allocate seq → save session。
        # 若中间崩溃，留下"有 seq 无 session 引用"的孤儿群：/groups list 可见、
        # _dissolve_idle_groups 按 seq.created_at 兜底回收，不会永久失控。
        # 反过来先 save session 再 allocate 的话，中间崩溃会留下 group_active=True
        # 但 seq 缺失的 session，/groups 命令看不到该群、无法在线管理。
        seq = seq_store.allocate(chat_id)
        session_store.save(session_id, chat_id, group_active=True, project_dir=project_dir)
        logger.info("[ensure-chat] Created group #%d: session=%s, chat_id=%s", seq, session_id, chat_id)

        # 创建成功后清理 per-session 锁：chat_id 已持久化，
        # 后续调用在锁外首次检查即命中，此锁不再被使用
        with _ensure_chat_global_lock:
            _ensure_chat_locks.pop(session_id, None)

        return 200, {'chat_id': chat_id}


def handle_session_attach(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """按前缀查找 session 并在唯一匹配时绑定到目标群聊

    callback 只负责数据层：返回匹配情况和绑定结果，不生成用户提示。
    网关侧根据 matched_ids 和 attached 自行构造反馈消息。

    调用方 (飞书网关 feishu.py):
    - /attach 命令: _handle_attach_command() 用户在群聊中执行

    请求:
        - session_prefix: session_id 前缀
        - chat_id: 目标群聊 ID

    响应:
        {
            'matched_ids': list[str],      # 前缀匹配到的全部 session_id
            'attached': bool,              # 是否执行了绑定（仅唯一匹配时为 True）
            'session_id': str,             # 绑定的 session_id（attached=True 时有值）
            'original_chat_id': str,       # session 绑定前的 chat_id（attached=True 时有值）
            'original_seq': int | None,    # 原群聊的 seq（仅当原群是服务创建时非 None）
            'dissolve_days': int,          # FEISHU_GROUP_DISSOLVE_DAYS 配置值，0 表示未启用自动解散
        }
    """
    from services.session_chat_store import SessionChatStore
    from services.group_seq_store import GroupSeqStore
    from config import FEISHU_GROUP_DISSOLVE_DAYS

    if not check_global_auth_token(headers, '/cb/session/attach'):
        return 401, {'error': 'Unauthorized'}

    prefix = data.get('session_prefix', '').strip()
    target_chat_id = data.get('chat_id', '').strip()
    if not prefix or not target_chat_id:
        return 400, {'error': 'Missing session_prefix or chat_id'}

    session_store = SessionChatStore.get_instance()
    seq_store = GroupSeqStore.get_instance()
    if not session_store or not seq_store:
        return 500, {'error': 'Store not initialized'}

    matches = session_store.find_by_prefix(prefix)
    result = {
        'matched_ids': list(matches.keys()),
        'attached': False,
        'session_id': '',
        'original_chat_id': '',
        'original_seq': None,
        'dissolve_days': FEISHU_GROUP_DISSOLVE_DAYS,
    }

    if len(matches) != 1:
        return 200, result

    session_id, session_data = next(iter(matches.items()))
    original_chat_id = session_data.get('chat_id', '')

    # 执行迁移（save 会自动处理 chat_id 变更、dissolved 复活、反向索引等）
    session_store.save(session_id, target_chat_id, group_active=True)

    # 仅当原群聊不同于目标群聊时才返回 seq，避免提示用户"孤儿群"实际是当前群
    original_seq = None
    if original_chat_id and original_chat_id != target_chat_id:
        original_seq = seq_store.get_seq(original_chat_id)

    result.update({
        'attached': True,
        'session_id': session_id,
        'original_chat_id': original_chat_id,
        'original_seq': original_seq,
    })

    logger.info("[session-attach] %s: %s -> %s (original_seq=%s)",
                session_id, original_chat_id or '-', target_chat_id, result['original_seq'])
    return 200, result


def handle_resolve_group_chat(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """通过 chat_id 反查 session_id

    调用方 (均为飞书网关 feishu.py):
    - 群聊普通消息处理: 通过 chat_id 找到对应 session，转发为 /continue
    - /new 命令处理: 群聊中发起 /new 时，查询该群是否已绑定 session，继承工作目录和命令
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/resolve-group-chat'):
        return 401, {'error': 'Unauthorized'}

    chat_id = data.get('chat_id', '')
    if not chat_id:
        return 400, {'error': 'Missing chat_id'}

    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'error': 'Store not initialized'}

    session_id = store.get_session_by_chat_id(chat_id)
    if not session_id:
        return 200, {'session_id': '', 'project_dir': '', 'claude_command': ''}

    session_data = store.get_session(session_id)
    project_dir = session_data.get('project_dir', '') if session_data else ''
    claude_command = session_data.get('claude_command', '') if session_data else ''
    return 200, {'session_id': session_id, 'project_dir': project_dir, 'claude_command': claude_command}


def handle_groups_list(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """列出活跃群聊

    调用方 (飞书网关 feishu.py):
    - /groups 命令: _list_groups() 展示当前活跃群聊列表

    数据源：
    - GroupSeqStore：服务创建群聊的清单和 seq
    - SessionChatStore：每个 chat_id 对应 session 的 project_dir 和最近活跃时间
    """
    from services.session_chat_store import SessionChatStore
    from services.group_seq_store import GroupSeqStore

    if not check_global_auth_token(headers, '/cb/groups/list'):
        return 401, {'error': 'Unauthorized'}

    seq_store = GroupSeqStore.get_instance()
    session_store = SessionChatStore.get_instance()
    if not seq_store or not session_store:
        return 500, {'error': 'Store not initialized'}

    chat_last_active = session_store.get_chat_last_active()
    # 一次性拿全量 session，后续按 session_id 索引，避免循环内反复加锁读文件
    sessions = session_store.get_all()

    groups = []
    for entry in seq_store.get_all():
        chat_id = entry['chat_id']
        # 查 active session 的 project_dir（仅用于展示）
        project_dir = ''
        session_id = session_store.get_session_by_chat_id(chat_id)
        # 有 session_id 关联，说明该群聊有活跃会话；否则为孤儿群——可能是该群聊的
        # 最近会话已被 attach 到其他群聊（group_active 转移）、session 记录已过期清理等
        if session_id:
            session_data = sessions.get(session_id)
            if session_data:
                project_dir = session_data.get('project_dir', '')
        # 孤儿群（无活跃 session 关联）用 created_at 兜底
        updated_at = chat_last_active.get(chat_id, entry.get('created_at', 0))
        groups.append({
            'chat_id': chat_id,
            'group_seq': entry['seq'],
            'project_dir': project_dir,
            'updated_at': updated_at,
        })
    # 按 updated_at 降序（最近活跃的在前）
    groups.sort(key=lambda x: x['updated_at'], reverse=True)
    return 200, {'groups': groups}


def dissolve_groups_by_targets(chat_ids: List[str]) -> Dict[str, Any]:
    """批量解散群聊并清理相关 session 状态

    调用方:
    - handle_groups_dissolve(): /groups dissolve 命令，按序号或全部解散群聊
    - _dissolve_idle_groups() (main.py): 自动解散空闲群聊

    归属判断（是否服务创建）由网关侧 batch_dissolve_groups 完成，
    callback 侧只负责 session 映射清理。

    Args:
        chat_ids: 待解散的群聊 ID 列表

    Returns:
        {'dissolved_count': int, 'dissolved_items': list, 'skipped_count': int,
         'failed': list (optional)}
    """
    if not chat_ids:
        return {'dissolved_count': 0, 'dissolved_items': [], 'skipped_count': 0}

    from services.session_chat_store import SessionChatStore
    from services.group_seq_store import GroupSeqStore

    session_store = SessionChatStore.get_instance()
    seq_store = GroupSeqStore.get_instance()
    if not session_store or not seq_store:
        return {
            'dissolved_count': 0,
            'dissolved_items': [],
            'skipped_count': 0,
            'failed': [{'chat_id': cid, 'error': 'Store not initialized'}
                       for cid in chat_ids],
        }

    # 批量解散（网关内部按归属判断，非服务群聊会进入 skipped_items）
    resp = dissolve_feishu_groups(chat_ids)
    dissolved_items = resp['dissolved_items']
    skipped_items = resp.get('skipped_items', [])
    failed_items = resp.get('failed', [])

    # 按 chat_id 批量标记所有相关 session 为 dissolved，并清理 GroupSeqStore 记录
    for chat_id in dissolved_items:
        session_store.mark_dissolved(chat_id)
        seq_store.remove(chat_id)

    # 脏 seq 清理：skipped = 网关 GroupChatStore 无该群归属（已解散/从未归属），
    # callback 侧保留 seq 会被 /groups list、自动解散反复扫到
    for chat_id in skipped_items:
        if seq_store.remove(chat_id):
            logger.info("[groups-dissolve] Purged dangling seq for chat_id=%s (gateway reported skipped)", chat_id)

    logger.info("[groups-dissolve] Dissolved %d groups (skipped %d, failed %d)",
                len(dissolved_items), len(skipped_items), len(failed_items))
    result = {
        'dissolved_count': len(dissolved_items),
        'dissolved_items': dissolved_items,
        'skipped_count': len(skipped_items),
    }
    if failed_items:
        result['failed'] = failed_items
    return result


def handle_groups_dissolve(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """按 seqs 或 all 查询解散群聊（HTTP 路由 POST /cb/groups/dissolve）

    调用方 (飞书网关 feishu.py):
    - /groups dissolve 命令: _dissolve_groups() 按序号或全部解散群聊

    请求 data:
    - all: bool，true 表示解散全部活跃群聊
    - seqs: List[int]，按 group_seq 序号解散（all 为 false 时必需）

    响应 body:
    - dissolved_count: int，成功解散数
    - dissolved_items: List[str]，成功解散的 chat_id 列表
    - skipped_count: int，跳过的外部群聊数
    - failed: List[{seq, chat_id, error}]，失败明细（可选）
    """
    from services.session_chat_store import SessionChatStore
    from services.group_seq_store import GroupSeqStore

    if not check_global_auth_token(headers, '/cb/groups/dissolve'):
        return 401, {'error': 'Unauthorized'}

    session_store = SessionChatStore.get_instance()
    seq_store = GroupSeqStore.get_instance()
    if not session_store or not seq_store:
        return 500, {'error': 'Store not initialized'}

    # 确定目标 chat_id 列表，同时构建 chat_id → seq 快照用于 failed 回填
    # 快照必须在 dissolve 前建立：dissolve 成功会 remove seq，事后回查拿不到
    if data.get('all'):
        entries = seq_store.get_all()
        chat_ids = [entry['chat_id'] for entry in entries]
        chat_to_seq = {entry['chat_id']: entry['seq'] for entry in entries}
    else:
        seqs = data.get('seqs', [])
        if not seqs:
            return 400, {'error': 'Missing seqs or all'}
        # 走内存索引，零 I/O；同时 dict 自动去重，避免用户输入重复 seq 时下游幂等被重复触发
        chat_to_seq = {}
        for s in seqs:
            cid = seq_store.get_chat_by_seq(s)
            if cid:
                chat_to_seq[cid] = s
        chat_ids = list(chat_to_seq.keys())

    result = dissolve_groups_by_targets(chat_ids)

    # 回填 seq：网关侧返回的 failed 只带 chat_id，seq 是 callback 的业务概念，
    # 在响应层按 chat_id 反查补齐，避免用户侧失败反馈丢失群聊序号。
    if result.get('failed'):
        for item in result['failed']:
            item['seq'] = chat_to_seq.get(item.get('chat_id', ''), 0)
    return 200, result


# =============================================
# POST 路由表 — 纯函数签名: (data, headers) → (status, body)
# =============================================

# 类型别名：POST 路由处理函数类型
PostRouteHandler = Callable[[Dict[str, Any], Dict[str, str]], Tuple[int, Dict[str, Any]]]

BACKEND_ROUTES: Dict[str, PostRouteHandler] = {
    '/cb/register': handle_register_callback_route,
    '/cb/check-owner': handle_check_owner_id_route,
    '/cb/decision': handle_callback_decision,
    '/cb/session/get-chat-id': handle_get_chat_id,
    '/cb/session/get-last-message-id': handle_get_last_message_id,
    '/cb/session/set-last-message-id': handle_set_last_message_id,
    '/cb/session/check-skip-user-prompt': handle_check_skip_user_prompt,
    '/cb/session/ensure-chat': handle_ensure_chat,
    '/cb/session/resolve-group-chat': handle_resolve_group_chat,
    '/cb/session/attach': handle_session_attach,
    '/cb/claude/new': handle_claude_new,
    '/cb/claude/continue': handle_claude_continue,
    '/cb/claude/record-dir-usage': handle_record_dir_usage,
    '/cb/claude/recent-dirs': handle_recent_dirs,
    '/cb/claude/browse-dirs': handle_browse_dirs,
    '/cb/groups/list': handle_groups_list,
    '/cb/groups/dissolve': handle_groups_dissolve,
}

# =============================================
# 遥测路由（从 telemetry.handler 导入）
# =============================================
from telemetry.handler import TELEMETRY_ROUTES  # noqa: E402
BACKEND_ROUTES.update(TELEMETRY_ROUTES)
