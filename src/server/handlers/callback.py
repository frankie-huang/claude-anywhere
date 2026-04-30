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
- /cb/session/get-info: 按 session_id 返回 session 权威字段（claude_command 等）
- /cb/session/attach: 将指定 session 绑定到目标群聊
- /cb/session/mute: 设置/解除/查询 session 静音状态
- /cb/session/invalidate-chats: 标记所有引用该 chat_id 的记录为 dissolved 状态（gateway 解散群后调用）
- /cb/claude/new: 新建 Claude 会话
- /cb/claude/continue: 继续 Claude 会话
- /cb/claude/recent-dirs: 获取近期工作目录
- /cb/claude/browse-dirs: 浏览子目录
"""

import base64
import json
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
from handlers.utils import send_json, send_html_response, create_feishu_group

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


def _extract_answers_from_form_value(form_value: Dict[str, Any],
                                     questions: list) -> Dict[str, str]:
    """按飞书卡片 form_value 约定（字段命名由 src/lib/feishu.sh 写死）构造 answers。

    Args:
        form_value: 飞书 card.action 回调里的 event.action.form_value 原值，按题
            号索引编码（不含 question 原文）:

                {
                  "q_{i}_select": str | List[str],  # i 为题号（0-based）
                                                     #   单选题 -> str（未选为 ""）
                                                     #   多选题 -> List[str]（未选为 []）
                  "q_{i}_custom": str                # 自定义输入框（未填为 ""）
                }

        questions: AskUserQuestion 的原始 questions 数组（Claude 工具协议），
            每项至少含 `question` 字段用作 answers 的 key；数组顺序与
            form_value 里的索引 i 一致。

    Returns:
        {question_text: answer_str} 字典，按 questions 顺序填充。

        - 多选（select_value 是 list）：合并 select 选项 + custom 输入，
          英文逗号 + 空格分隔，例：``"Python, TypeScript, 自定义内容"``
        - 单选：custom 非空时优先用 custom（覆盖下拉），否则用 select；
          两者都空则为 ``""``

    最终被 Claude 模型消费（通过 hookSpecificOutput.decision.updatedInput 注入
    AskUserQuestion 工具返回值）。

    已知协议约束（不是本函数可改）:
        - Claude Code 的 AskUserQuestion hook 协议要求 answers 的 value 为 str
          （社区验证，见 openspec archive `add-ask-user-question-approval`），
          多选答案按 ", " 拼接，不要改成 List[str] 或其它分隔符。
        - 未验证场景：option label 或用户 custom 输入本身含 ", " 子串时，
          Claude 侧如何解析多选答案未实测；理论上可能产生边界歧义，但也可能
          Claude 会结合原 questions.options 反查消歧。遇到相关问题再补实验。
          待 Claude Code 为 AskUserQuestion 提供独立 hook event 并支持结构化
          answers 后可根治（参考 anthropics/claude-code#12605）。
    """
    answers: Dict[str, str] = {}
    for i, q in enumerate(questions):
        question_text = q.get('question', '')
        select_value = form_value.get(f'q_{i}_select', '')
        custom_value = form_value.get(f'q_{i}_custom', '')

        if isinstance(select_value, list):
            labels = list(select_value)
            if custom_value:
                labels.append(custom_value)
            answer = ', '.join(labels) if labels else ''
        else:
            answer = custom_value if custom_value else (select_value or '')

        answers[question_text] = answer
    return answers


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
            - form_value: 飞书卡片 card.action 回调里的 form_value 原值，其具体
              schema 由 action 决定（action=answer 时的格式见下方分支注释）
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
    form_value = data.get('form_value')

    logger.info("[cb/decision] action=%s, request_id=%s", action, request_id)

    # 验证参数（请求格式错误返回 400）
    if not action or not request_id:
        logger.warning("[cb/decision] Missing params: action=%s, request_id=%s", action, request_id)
        return 400, {
            'success': False,
            'decision': None,
            'message': '无效的请求参数'
        }

    if action == 'answer':
        # action=answer 时 form_value 为 AskUserQuestion 卡片的表单提交值，
        # schema（字段命名由 src/lib/feishu.sh 渲染时写死）:
        #   {
        #     "q_{i}_select": str | List[str],   # i 为题号（0-based）
        #                                         #   单选题 -> str（未选为 ""）
        #                                         #   多选题 -> List[str]（未选为 []）
        #     "q_{i}_custom": str                 # 自定义输入框（未填为 ""）
        #   }
        # 不含 question 原文，只有索引；需要结合 RequestManager 里该 request 的
        # questions_encoded（Claude AskUserQuestion 的原始 questions 数组 base64）
        # 反查 question 原文后构造 answers: {question_text: answer_str}。
        #
        # 其它 action 类型（allow/always/deny/interrupt）目前不使用 form_value；
        # 若未来新增用例，请在对应分支内补充自己的 schema 说明，避免混淆。
        if form_value is None:
            logger.warning("[cb/decision] answer action missing form_value: %s", request_id)
            return 400, {
                'success': False,
                'decision': None,
                'message': '缺少 form_value'
            }
        req_data = RequestManager.get_instance().get_request_data(request_id)
        if not req_data:
            logger.warning("[cb/decision] Request not found: %s", request_id)
            return 200, {
                'success': False,
                'decision': None,
                'message': '请求不存在或已过期'
            }
        questions_encoded = req_data.get('questions_encoded', '')
        if not questions_encoded:
            logger.warning("[cb/decision] No questions_encoded for request: %s", request_id)
            return 200, {
                'success': False,
                'decision': None,
                'message': '问题数据不存在'
            }
        try:
            questions_json = base64.b64decode(questions_encoded.encode()).decode('utf-8')
            questions = json.loads(questions_json)
        except Exception as e:
            logger.error("[cb/decision] Failed to decode questions: %s", e)
            return 200, {
                'success': False,
                'decision': None,
                'message': '问题数据解析失败'
            }
        if not questions:
            return 200, {
                'success': False,
                'decision': None,
                'message': '问题数据不存在'
            }
        answers = _extract_answers_from_form_value(form_value, questions)
        logger.info("[cb/decision] answers built from form_value: %s",
                    json.dumps(answers, ensure_ascii=False))

        success, decision, message = handle_decision(
            request_id, action,
            answers=answers, questions=questions
        )
    else:
        # 其它 action（allow/always/deny/interrupt）：只需 project_dir，无需 answers/questions
        success, decision, message = handle_decision(request_id, action, project_dir)

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


def do_ensure_chat(session_id: str, project_dir: str) -> Tuple[bool, str]:
    """确保 session 有对应的 chat_id（group 模式下创建群聊）

    调用方（各自负责鉴权）:
    - handle_ensure_chat (HTTP /cb/session/ensure-chat): Shell 脚本启动时调用，返回空则 fallback 到 FEISHU_CHAT_ID
    - handle_new_session (claude.py): P2P /new（group 模式无 chat_id）时调用，失败则整个 /new 失败

    流程：先调网关建群，成功后才 save session 记录。失败则不写入任何记录，
    接受网关侧可能已建群的孤儿群（用户可通过 /groups dissolve 清理，
    自动解散也会兜底回收）。

    dissolved 的 session：get_session 过滤返回 None，走建群路径；
    成功后 save(chat_id) 自动清除 dissolved（复活）。

    Args:
        session_id: Claude 会话 ID
        project_dir: 项目工作目录（建群命名用，同时存入 session 字段）

    Returns:
        (ok, chat_id_or_error)
    """
    from services.session_chat_store import SessionChatStore
    from config import FEISHU_SESSION_MODE

    session_store = SessionChatStore.get_instance()
    if not session_store:
        return False, 'Store not initialized'

    # 一次读取 session 数据，判断是否需要建群：
    #   - session 存在且有 chat_id → 直接返回
    #   - session 存在但无 chat_id → 正常态 session，无需建群
    #   - session 不存在或 dissolved → 仅 group 模式才建群
    session_data = session_store.get_session(session_id)
    if session_data:
        chat_id = session_data.get('chat_id', '')
        if chat_id:
            return True, chat_id
        return True, ''
    if FEISHU_SESSION_MODE != 'group':
        return True, ''

    # per-session 锁防止并发创建
    with _ensure_chat_global_lock:
        if session_id not in _ensure_chat_locks:
            _ensure_chat_locks[session_id] = threading.Lock()
        lock = _ensure_chat_locks[session_id]

    with lock:
        # 二次检查（锁内）
        existing = session_store.get_chat_id(session_id)
        if existing:
            return True, existing

        # 创建群聊（网关侧原子完成：建群 + 归属 + seq 分配 + 写 GroupSessionStore 路由）
        # seq 在 group_store.allocate 内部自带 INFO 日志，此处不重复
        ok, result = create_feishu_group(session_id, project_dir)
        if not ok:
            logger.error("[ensure-chat] Failed to create group: %s", result)
            return False, result

        chat_id = result
        # 建群成功才写 session 记录（新建或 dissolved 自动复活）
        session_store.save(session_id, chat_id, project_dir=project_dir)
        logger.info("[ensure-chat] Created group: session=%s, chat_id=%s",
                    session_id, chat_id)

        # 创建成功后清理 per-session 锁
        with _ensure_chat_global_lock:
            _ensure_chat_locks.pop(session_id, None)

        return True, chat_id


def handle_ensure_chat(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """确保 session 有对应的 chat_id（group 模式下懒创建群聊）

    HTTP 路由鉴权后委托 do_ensure_chat，调用方详见其 docstring。
    """
    if not check_global_auth_token(headers, '/cb/session/ensure-chat'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')

    if not session_id:
        return 400, {'error': 'Missing session_id'}

    ok, result = do_ensure_chat(session_id, project_dir)
    if ok:
        return 200, {'chat_id': result}
    else:
        return 500, {'error': 'Failed to create group: %s' % result}


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
            'original_chat_id': str,       # session 绑定前的 chat_id（attached=True 时有值；
                                           # 网关侧自己持有 GroupChatStore，能按此 id 自查 seq）
            'project_dir': str,            # session 的工作目录（供 gateway 本地路由表写入）
        }
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/attach'):
        return 401, {'error': 'Unauthorized'}

    prefix = data.get('session_prefix', '').strip()
    target_chat_id = data.get('chat_id', '').strip()
    if not prefix or not target_chat_id:
        return 400, {'error': 'Missing session_prefix or chat_id'}

    session_store = SessionChatStore.get_instance()
    if not session_store:
        return 500, {'error': 'Store not initialized'}

    # find_by_prefix 含 dissolved（供 attach 复活）
    matches = session_store.find_by_prefix(prefix)
    result = {
        'matched_ids': list(matches.keys()),
        'attached': False,
        'session_id': '',
        'original_chat_id': '',
        'project_dir': '',
    }

    if len(matches) != 1:
        return 200, result

    session_id, session_data = next(iter(matches.items()))
    original_chat_id = session_data.get('chat_id', '')

    # 执行迁移（save 传入非空 chat_id 自动清除 dissolved 复活）
    session_store.save(session_id, target_chat_id)

    result.update({
        'attached': True,
        'session_id': session_id,
        'original_chat_id': original_chat_id,
        # 供 gateway 写本地 GroupSessionStore
        'project_dir': session_data.get('project_dir', ''),
    })

    logger.info("[session-attach] %s: %s -> %s",
                session_id, original_chat_id or '-', target_chat_id)
    return 200, result


def handle_session_mute(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """管理 session 静音状态：mute / unmute / query

    调用方 (飞书网关 feishu.py):
    - /mute 命令: action='mute'
    - /unmute 命令 + 自动解除: action='unmute'
    - 出站拦截（SessionFacade 缓存 miss 时回源）: action='query'

    请求:
        - session_id: 目标 session
        - action: 'mute' | 'unmute' | 'query'

    响应:
        mute   -> {ok: True, changed: bool}  changed=True 表示本次新设置为静音；False 表示幂等（之前已静音）
        unmute -> {ok: True, changed: bool}  changed=True 表示本次实际解除；False 表示幂等（之前就未静音）
        query  -> {ok: True, muted: bool}    当前是否处于静音状态
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/mute'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '').strip()
    action = data.get('action', '').strip()
    if not session_id or action not in ('mute', 'unmute', 'query'):
        return 400, {'error': 'Missing session_id or invalid action'}

    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'error': 'Store not initialized'}

    if action == 'query':
        return 200, {'ok': True, 'muted': store.is_session_muted(session_id)}

    if action == 'mute':
        result = store.mute_session(session_id)
        if result is None:
            return 500, {'error': 'Failed to mute session'}
        return 200, {'ok': True, 'changed': result}

    # unmute —— store 同为 Optional[bool]，changed 语义一致
    result = store.unmute_session(session_id)
    if result is None:
        return 500, {'error': 'Failed to unmute session'}
    return 200, {'ok': True, 'changed': result}


def handle_invalidate_chats(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """批量标记一组 chat_id 关联的 session 为 dissolved

    调用方 (飞书网关 feishu.py):
    - /groups dissolve 成功解散群后通知 callback
    - 自动解散群后同样通知

    语义：标记所有引用该 chat_id 的 session 为 dissolved=True，
    保留 chat_id 字段作为历史信息。get_session 过滤 dissolved 后返回 None，
    使 session 软失效——下次 ensure-chat 自动走重建路径，continue 直接报错
    引导用户 /new。

    请求:
        - chat_ids: List[str]，被解散的群聊 ID 列表

    响应:
        {ok: True, invalidated: int}  invalidated 是被标记 dissolved 的 session 总数
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/invalidate-chats'):
        return 401, {'error': 'Unauthorized'}

    chat_ids = data.get('chat_ids') or []
    if not isinstance(chat_ids, list):
        return 400, {'error': 'chat_ids must be a list'}

    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'error': 'Store not initialized'}

    total = 0
    for cid in chat_ids:
        if not cid:
            continue
        total += len(store.mark_dissolved(cid))
    return 200, {'ok': True, 'invalidated': total}


def handle_get_session_info(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """按 session_id 返回 session 的权威字段

    调用方 (飞书网关 feishu.py):
    - /new 继承场景: 从消息上下文拿到 session_id 后，回源取 claude_command 等属性

    请求:
        - session_id: Claude 会话 ID

    响应:
        {project_dir: str, claude_command: str, chat_id: str, dissolved: bool}
        session 不存在或已过期返回全空（非错误）
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/get-info'):
        return 401, {'error': 'Unauthorized'}

    session_id = data.get('session_id', '').strip()
    if not session_id:
        return 400, {'error': 'Missing session_id'}

    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'error': 'Store not initialized'}

    # include_dissolved=True：只读属性继承场景，dissolved 不应阻断
    item = store.get_session(session_id, include_dissolved=True)
    if not item:
        return 200, {'project_dir': '', 'claude_command': '', 'chat_id': '', 'dissolved': False}
    return 200, {
        'project_dir': item.get('project_dir', ''),
        'claude_command': item.get('claude_command', ''),
        'chat_id': item.get('chat_id', ''),
        'dissolved': bool(item.get('dissolved')),
    }


def handle_session_clone(data: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """以旧 session 为模板创建新 session 记录（/clear 用）

    从旧 session 继承 project_dir + claude_command，创建新 session 记录绑定到 chat_id。
    旧 session 不受影响（不标记 dissolved，不杀进程）。

    调用方 (飞书网关 feishu.py):
    - /clear 命令: 预创建新 session，等待下一条消息启动 Claude 进程

    请求:
        - old_session_id: 旧 Claude 会话 ID（用于读取继承属性）
        - new_session_id: 新 Claude 会话 ID（由网关生成）
        - chat_id: 飞书群聊 ID

    响应:
        {ok: true, project_dir: str}
        旧 session 不存在时仍创建新记录（继承属性为空）
    """
    from services.session_chat_store import SessionChatStore

    if not check_global_auth_token(headers, '/cb/session/clone'):
        return 401, {'error': 'Unauthorized'}

    old_session_id = data.get('old_session_id', '').strip()
    new_session_id = data.get('new_session_id', '').strip()
    chat_id = data.get('chat_id', '').strip()

    if not new_session_id:
        return 400, {'error': 'Missing new_session_id'}
    if not chat_id:
        return 400, {'error': 'Missing chat_id'}

    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'error': 'Store not initialized'}

    # 从旧 session 读取继承属性（允许 dissolved，只读不改）
    project_dir = ''
    claude_command = ''
    if old_session_id:
        old_item = store.get_session(old_session_id, include_dissolved=True)
        if old_item:
            project_dir = old_item.get('project_dir', '')
            claude_command = old_item.get('claude_command', '')

    # 创建新 session 记录
    ok = store.save(new_session_id, chat_id,
                    project_dir=project_dir, claude_command=claude_command)
    if not ok:
        return 500, {'error': 'Failed to save new session'}

    logger.info("[session-clone] Cloned %s -> %s (chat=%s, dir=%s, cmd=%s)",
                old_session_id, new_session_id, chat_id, project_dir, claude_command)
    return 200, {'ok': True, 'project_dir': project_dir}


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
    '/cb/session/get-info': handle_get_session_info,
    '/cb/session/attach': handle_session_attach,
    '/cb/session/mute': handle_session_mute,
    '/cb/session/clone': handle_session_clone,
    '/cb/session/invalidate-chats': handle_invalidate_chats,
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
