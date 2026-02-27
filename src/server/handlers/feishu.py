"""
Feishu Handler - 飞书事件处理器

处理飞书相关的 POST 请求：
    - URL 验证（type: url_verification）
    - 消息事件（im.message.receive_v1）
    - 卡片回传交互（card.action.trigger）
    - 发送消息（/gw/feishu/send）
"""

import hmac
import json
import logging
import os
import re
import shlex
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

# setup_logging 由 main.py 启动时将 shared/ 加入 sys.path
from logging_config import setup_logging

from handlers.utils import run_in_background as _run_in_background, post_json as _post_json

logger = logging.getLogger(__name__)

# 飞书 Toast 类型常量
TOAST_SUCCESS = 'success'
TOAST_WARNING = 'warning'
TOAST_ERROR = 'error'
TOAST_INFO = 'info'

# 飞书消息事件日志（独立文件）
_feishu_message_logger = None
_feishu_message_logger_lock = threading.Lock()

# 消息内容清理正则：移除 @_user_1 提及（带或不带尾随空格）
_AT_USER_PATTERN = re.compile(r'@_user_1\s?')


def _sanitize_user_content(content: str, max_len: int = 20) -> str:
    """脱敏用户生成内容

    Args:
        content: 原始内容
        max_len: 保留的最大长度

    Returns:
        脱敏后的内容，格式为 "前N个字符..." (总长度: X)
    """
    if not content:
        return ''
    preview = content[:max_len].replace('\n', '\\n')
    return f"{preview}... (len={len(content)})"


def _truncate_path(path: str, max_len: int = 40) -> str:
    """截断文件路径（从后往前截断，保留重要部分）

    Args:
        path: 文件路径
        max_len: 最大长度

    Returns:
        截断后的路径，如 ".../project/dir" (len=50)，未截断则返回原路径
    """
    if not path:
        return ''
    if len(path) <= max_len:
        return path
    # 保留后 max_len 个字符，前面加 ...
    return f"...{path[-(max_len - 3):]} (len={len(path)})"


def _get_message_logger():
    """获取飞书消息日志记录器（懒加载，线程安全）"""
    global _feishu_message_logger
    if _feishu_message_logger is None:
        with _feishu_message_logger_lock:
            if _feishu_message_logger is None:  # 双重检查
                _feishu_message_logger = setup_logging(
                    'feishu_message', console=False, propagate=False, encoding='utf-8'
                )
                logger.info("Feishu message logging to: %s (daily rotating)",
                            _feishu_message_logger.handlers[0].baseFilename)

    return _feishu_message_logger


def handle_feishu_request(data: dict) -> Tuple[bool, dict]:
    """处理飞书请求

    支持的请求类型：
        - url_verification: URL 验证
        - im.message.receive_v1: 消息接收事件
        - card.action.trigger: 卡片回传交互事件

    Args:
        data: 请求 JSON 数据

    Returns:
        (handled, response): handled 表示是否处理了请求，response 是响应数据
    """
    # URL 验证请求（优先处理，无需验证 token）
    if data.get('type') == 'url_verification':
        return _handle_url_verification(data)

    # 验证 Verification Token（配置了 token 时强制验证）
    if not _verify_token(data):
        logger.warning("[feishu] Invalid verification token")
        return False, {'success': False, 'error': 'Invalid verification token'}

    # 事件订阅（schema 2.0）
    header = data.get('header', {})
    event_type = header.get('event_type', '')

    if event_type == 'im.message.receive_v1':
        _handle_message_event(data)
        return True, {'success': True}

    # 卡片回传交互事件
    if event_type == 'card.action.trigger':
        return _handle_card_action(data)

    # 未处理的飞书事件类型或其他请求
    event_type = data.get('header', {}).get('event_type', '')
    logger.debug(f"[feishu] Unhandled request, event_type={event_type}, data: {json.dumps(data, ensure_ascii=True)}")
    return False, {}


def _verify_token(data: dict) -> bool:
    """验证 Verification Token

    从请求 header 中提取 token 并与配置比对。
    如果未配置 token，则跳过验证（兼容现有部署）。

    Args:
        data: 飞书请求数据

    Returns:
        True: 验证通过或未配置 token
        False: 验证失败
    """
    from config import FEISHU_VERIFICATION_TOKEN

    # 未配置 token，跳过验证
    if not FEISHU_VERIFICATION_TOKEN:
        return True

    # 从 header 提取 token
    header = data.get('header', {})
    token = header.get('token', '')

    if not token:
        logger.warning("[feishu] Request missing token in header")
        return False

    # 验证 token（恒定时间比较，防止时序攻击）
    if not hmac.compare_digest(token, FEISHU_VERIFICATION_TOKEN):
        logger.warning(f"[feishu] Token mismatch")
        return False

    return True


def _handle_url_verification(data: dict) -> Tuple[bool, dict]:
    """处理飞书 URL 验证请求

    飞书在配置事件订阅时会发送验证请求，需要在 1 秒内返回 challenge 值。

    Args:
        data: 请求数据，包含 challenge 字段

    Returns:
        (True, {'challenge': xxx})
    """
    challenge = data.get('challenge', '')
    logger.info(f"[feishu] URL verification, challenge: {challenge[:20]}...")
    return True, {'challenge': challenge}


def _handle_message_event(data: dict):
    """处理飞书消息事件 im.message.receive_v1

    Args:
        data: 飞书事件数据
    """
    header = data.get('header', {})
    event = data.get('event', {})
    message = event.get('message', {})
    sender = event.get('sender', {})

    event_id = header.get('event_id', '')
    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    chat_type = message.get('chat_type', '')  # p2p / group
    message_type = message.get('message_type', '')  # text / image / ...
    content = message.get('content', '{}')
    sender_id = sender.get('sender_id', {}).get('open_id', '')
    parent_id = message.get('parent_id', '')  # 是否是回复消息

    # 解析消息纯文本内容
    try:
        content_obj = json.loads(content)
        text = content_obj.get('text', '')
        # post 类型：从 content 二维数组中提取文本，段落间用 \n 分隔
        if not text and message_type == 'post':
            content_list = content_obj.get('content', [])
            paragraphs = []
            for paragraph in content_list if isinstance(content_list, list) else []:
                if isinstance(paragraph, list):
                    para_text = ''
                    for elem in paragraph:
                        if isinstance(elem, dict) and elem.get('tag') == 'text':
                            elem_text = elem.get('text', '')
                            if elem_text:
                                para_text += elem_text
                    if para_text:
                        paragraphs.append(para_text)
            text = '\n'.join(paragraphs)
    except json.JSONDecodeError:
        text = content

    # 先记录原始数据到日志（所有消息都记录），脱敏用户内容
    msg_logger = _get_message_logger()
    msg_logger.info(json.dumps({
        'event_id': event_id,
        'message_id': message_id,
        'parent_id': parent_id,
        'chat_id': chat_id,
        'chat_type': chat_type,
        'message_type': message_type,
        'sender_id': sender_id,
        'content': _sanitize_user_content(content),
        'text': _sanitize_user_content(text),
        'raw_data': data  # 记录完整的原始数据
    }, ensure_ascii=False))

    logger.info(f"[feishu] Message received: chat_type={chat_type}, message_type={message_type}, parent_id={parent_id if parent_id else ''}, text={_sanitize_user_content(text)}")

    # 清理消息中的 @_user_1 提及（带或不带尾随空格）
    text = _AT_USER_PATTERN.sub('', text)
    # 将清理后的纯文本写入 message['plain_text']，供下游直接使用
    message['plain_text'] = text

    # 检查是否是命令（优先处理，因为命令也可能是回复消息）
    is_command, command, args = _parse_command(text)
    if is_command:
        _handle_command(data, command, args)
        return

    # 检查是否是回复消息（用于继续会话）
    if parent_id:
        _handle_reply_message(data, parent_id)
        return

    # 非回复、非命令的普通消息：发送使用提示
    if text.strip():
        supported = _get_supported_commands()
        hint = "💡 我还不能直接对话哦，请通过以下方式使用：\n\n" \
               "**发起新会话：**\n" \
               "发送 `/new` 指令创建 Claude 会话\n\n" \
               "**继续会话：**\n" \
               "回复 Claude 的消息即可继续对话\n\n" \
               "**支持的指令：**\n" + supported
        _run_in_background(_send_reject_message, (chat_id, hint, message_id))


def _get_supported_commands() -> str:
    """获取支持的命令列表（用于帮助提示）

    Returns:
        命令列表字符串
    """
    items = [f"- `/{cmd}`: {info}" for cmd, (_, info) in _COMMANDS.items()]
    return '\n'.join(items)


def _parse_command(text: str) -> Tuple[bool, str, str]:
    """解析命令

    支持格式：
    - /command arg1 arg2
    - /command --key=value arg

    Args:
        text: 消息文本

    Returns:
        (is_command, command, args):
            - is_command: 是否是命令
            - command: 命令名（不含 /）
            - args: 参数部分（不含命令名）
    """
    stripped = text.strip()
    if not stripped.startswith('/'):
        return False, '', ''

    # 找到第一个空格或结尾，提取命令名
    parts = stripped[1:].split(None, 1)  # 移除 /，然后按空白分割
    if not parts:
        return False, '', ''

    command = parts[0]
    args = parts[1] if len(parts) > 1 else ''
    return True, command, args


def _handle_command(data: dict, command: str, args: str):
    """处理命令

    Args:
        data: 飞书事件数据
        command: 命令名（如 'new'）
        args: 参数部分
    """
    handler = _COMMANDS.get(command)
    if handler:
        handler_func, _ = handler
        handler_func(data, args)
    else:
        logger.info(f"[feishu] Unknown command: /{command}")
        # 发送未知指令提示
        event = data.get('event', {})
        message = event.get('message', {})
        chat_id = message.get('chat_id', '')
        message_id = message.get('message_id', '')
        if chat_id:
            supported = _get_supported_commands()
            _run_in_background(_send_reject_message, (chat_id, f"未知指令：`/{command}`\n\n支持的指令：\n{supported}", message_id))


def _handle_reply_message(data: dict, parent_id: str):
    """处理用户回复消息，继续 Claude 会话

    Args:
        data: 飞书事件数据
        parent_id: 被回复的消息 ID
    """
    from services.message_session_store import MessageSessionStore

    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    # 直接使用上游解析好的 plain_text（已清理 @_user_1）
    prompt = message.get('plain_text', '')

    if not prompt:
        logger.warning(f"[feishu] Reply message has no text content, parent_id={parent_id}")
        _run_in_background(_send_reject_message, (chat_id, "消息内容为空，无法继续会话", message_id))
        return

    logger.info(f"[feishu] Reply message: parent_id={parent_id}, prompt={_sanitize_user_content(prompt)}")

    # 查询映射
    store = MessageSessionStore.get_instance()
    if not store:
        logger.warning("[feishu] MessageSessionStore not initialized")
        _run_in_background(_send_reject_message, (chat_id, "会话存储服务未初始化，请稍后重试或联系管理员", message_id))
        return

    mapping = store.get(parent_id)
    if not mapping:
        logger.info(f"[feishu] No mapping found for parent_id={parent_id}, ignoring")
        _run_in_background(_send_reject_message, (chat_id, "无法找到对应的会话（可能已过期或被清理），请重新发起 /new 指令", message_id))
        return

    # 获取 binding（用于转发请求）
    binding = _get_binding_from_event(event)

    # 在后台线程中转发到 Callback 后端
    _run_in_background(_forward_continue_request, (binding, mapping['session_id'], mapping['project_dir'], prompt, chat_id, message_id))


def _forward_claude_request(callback_url: str, endpoint: str, auth_token: str,
                            payload: dict, chat_id: str,
                            reply_to: Optional[str] = None,
                            reply_in_thread: bool = False):
    """转发 Claude 会话请求到 Callback 后端

    Args:
        callback_url: Callback 后端 URL
        endpoint: API 端点（如 /cb/claude/new, /cb/claude/continue）
        auth_token: 认证令牌
        payload: 请求数据
        chat_id: 群聊 ID（用于错误通知）
        reply_to: 要回复的消息 ID（可选）
        reply_in_thread: 是否收进话题详情
    """
    import urllib.error

    # 从 endpoint 提取 action（如 /cb/claude/new -> new）
    action = endpoint.rstrip('/').split('/')[-1]
    known_actions = ('new', 'continue')
    if action not in known_actions:
        logger.warning(f"[feishu] Unknown endpoint action: {action}, expected one of {known_actions}")
        action = 'continue'  # 默认使用 continue
    is_new = (action == 'new')
    api_url = f"{callback_url.rstrip('/')}{endpoint}"

    logger.info(f"[feishu] Forwarding {action} request to {api_url}")

    try:
        response_data = _post_json(api_url, payload, auth_token=auth_token, timeout=30)
        logger.info(f"[feishu] {action.capitalize()} request response: {response_data}")

        # 先保存用户消息到 MessageSessionStore（不更新 last_message_id）
        if reply_to:
            session_id = response_data.get('session_id', '') or payload.get('session_id', '')
            project_dir = payload.get('project_dir', '')
            if session_id and project_dir:
                from services.message_session_store import MessageSessionStore
                msg_store = MessageSessionStore.get_instance()
                if msg_store:
                    msg_store.save(reply_to, session_id, project_dir)
                    logger.info(f"[feishu] Saved user message mapping: {reply_to} -> {session_id}")

        # 再发送系统通知（内部会更新 last_message_id）
        _send_session_result_notification(chat_id, response_data, payload.get('project_dir', ''),
                                           is_new=is_new,
                                           claude_command=payload.get('claude_command', ''),
                                           reply_to=reply_to,
                                           reply_in_thread=reply_in_thread,
                                           callback_url=callback_url, auth_token=auth_token)

    except urllib.error.HTTPError as e:
        error_detail = _extract_http_error_detail(e)
        action_text = "新建会话失败" if is_new else "继续会话失败"
        error_msg = f"{action_text}: {error_detail}" if error_detail else f"Callback 服务返回错误: HTTP {e.code}"
        logger.error(f"[feishu] {action.capitalize()} request HTTP error: {e.code} {e.reason}")
        # 注意：新建会话时 payload 中没有 session_id，对应的错误通知不会关联会话
        # 这符合预期，因为会话根本没创建成功
        _send_error_notification(chat_id, error_msg, reply_to=reply_to,
                                 session_id=payload.get('session_id', ''),
                                 project_dir=payload.get('project_dir', ''),
                                 reply_in_thread=reply_in_thread)

    except urllib.error.URLError as e:
        logger.error(f"[feishu] {action.capitalize()} request URL error: {e.reason}")
        _send_error_notification(chat_id, f"Callback 服务不可达: {e.reason}", reply_to=reply_to,
                                 session_id=payload.get('session_id', ''),
                                 project_dir=payload.get('project_dir', ''),
                                 reply_in_thread=reply_in_thread)


def _extract_http_error_detail(http_error):
    """从 HTTPError 中提取错误详情

    Args:
        http_error: urllib.error.HTTPError 实例

    Returns:
        错误详情字符串，无法解析返回空字符串
    """
    try:
        error_body = http_error.read().decode('utf-8')
        error_data = json.loads(error_body)
        return error_data.get('error', '')
    except:
        return ''


def _forward_continue_request(binding: dict, session_id: str, project_dir: str,
                              prompt: str, chat_id: str, message_id: str,
                              claude_command: str = ''):
    """转发继续会话请求到 Callback 后端

    Args:
        binding: 绑定信息（包含 auth_token, callback_url 等）
        session_id: 会话 ID
        project_dir: 项目目录
        prompt: 用户回复内容
        chat_id: 群聊 ID
        message_id: 用户消息 ID（用于回复）
        claude_command: 指定使用的 Claude 命令（可选）
    """
    if not binding:
        logger.warning("[feishu] No binding found, cannot continue session")
        _send_error_notification(chat_id, "您尚未注册，无法使用此功能", reply_to=message_id,
                                 session_id=session_id, project_dir=project_dir)
        return

    auth_token = binding.get('auth_token', '')
    callback_url = binding.get('callback_url', '')
    reply_in_thread = binding.get('reply_in_thread', False)

    data = {
        'session_id': session_id,
        'project_dir': project_dir,
        'prompt': prompt,
        'chat_id': chat_id,
        'reply_message_id': message_id
    }
    if claude_command:
        data['claude_command'] = claude_command

    _forward_claude_request(callback_url, '/cb/claude/continue', auth_token,
                            data, chat_id, reply_to=message_id,
                            reply_in_thread=reply_in_thread)


def _send_session_result_notification(chat_id: str, response: dict, project_dir: str,
                                      is_new: bool = False, claude_command: str = '',
                                      reply_to: Optional[str] = None,
                                      reply_in_thread: bool = False,
                                      callback_url: str = '', auth_token: str = ''):
    """根据会话结果发送飞书通知

    Args:
        chat_id: 群聊 ID
        response: Callback 返回的结果
        project_dir: 项目目录
        is_new: 是否为新建会话（True: 新建会话，False: 继续会话）
        claude_command: 使用的 Claude 命令（可选，如 'opus', 'sonnet'）
        reply_to: 要回复的消息 ID（可选，用于链式回复）
        reply_in_thread: 是否收进话题详情
        callback_url: Callback 后端 URL（用于跨网络设置 last_message_id）
        auth_token: 认证令牌（用于跨网络调用）
    """
    from services.feishu_api import FeishuAPIService
    from services.message_session_store import MessageSessionStore

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.warning("[feishu] FeishuAPIService not enabled, skipping notification")
        return

    status = response.get('status', '')
    error = response.get('error', '')
    session_id = response.get('session_id', '')

    success = False
    sent_message_id = ''

    if status == 'processing':
        # processing 通知策略：
        #
        # | 场景               | 行为                                      | sent_message_id           |
        # |-------------------|-------------------------------------------|---------------------------|
        # | 新建会话           | 发送文本消息(会话信息)，并给该消息加 Typing 表情 | 新发送的通知消息 ID         |
        # | 继续会话           | 给用户消息添加 Typing 表情(轻量，避免刷屏)      | reply_to(用户发送的消息 ID) |
        # | 继续会话(表情失败时) | 回退发送 ⏳ 文本消息                         | 新发送的通知消息 ID          |
        if is_new:
            # 新建会话 - 发送文本消息，展示会话信息
            message = f"🆕 Claude 会话已创建\n📁 项目: {_truncate_path(project_dir)}"
            if claude_command:
                message += f"\n🔧 命令: `{claude_command}`"
            if session_id:
                message += f"\n🔑 Session: `{session_id}`"
            success, sent_message_id = _send_text_message(service, chat_id, message, reply_to=reply_to,
                                                           reply_in_thread=reply_in_thread)
            # 给发出的通知消息添加 Typing 表情，表示正在处理中
            if success and sent_message_id:
                service.add_reaction(sent_message_id, 'Typing')
        else:
            # 继续会话 - 用表情回应代替文本通知，更轻量避免刷屏
            if reply_to:
                reaction_ok, _ = service.add_reaction(reply_to, 'Typing')
                if reaction_ok:
                    logger.info(f"[feishu] Added 'Typing' reaction to message {reply_to}")
                    # 将用户发送的消息作为 last_message_id，维护链式回复
                    sent_message_id = reply_to
                    success = True
                else:
                    logger.warning(f"[feishu] Failed to add reaction, fallback to text notification")

            # 表情回应失败或无 reply_to 时，回退为文本消息
            if not success:
                message = "⏳ Claude 正在处理您的问题，请稍候..."
                success, sent_message_id = _send_text_message(service, chat_id, message, reply_to=reply_to,
                                                               reply_in_thread=reply_in_thread)

    elif status == 'completed':
        # 快速完成
        output = response.get('output', '')
        message = f"✅ Claude 已完成: {_sanitize_user_content(output, 50)}" if output else "✅ Claude 已完成"
        success, sent_message_id = _send_text_message(service, chat_id, message, reply_to=reply_to,
                                                       reply_in_thread=reply_in_thread)

    elif error:
        # 执行失败
        error_prefix = "新建会话失败" if is_new else "Claude 执行失败"
        _send_error_notification(chat_id, f"{error_prefix}: {error}", reply_to=reply_to,
                                 session_id=session_id, project_dir=project_dir,
                                 reply_in_thread=reply_in_thread)
        return
    else:
        logger.warning(f"[feishu] Unknown response status: {status}")
        _send_error_notification(chat_id, f"未知的响应状态: {status}", reply_to=reply_to,
                                 session_id=session_id, project_dir=project_dir,
                                 reply_in_thread=reply_in_thread)
        return

    # 发送成功后统一保存消息映射和同步 last_message_id
    if success and sent_message_id and session_id and project_dir:
        msg_store = MessageSessionStore.get_instance()
        if msg_store:
            msg_store.save(sent_message_id, session_id, project_dir)
            logger.info(f"[feishu] Saved notification mapping: {sent_message_id} -> {session_id}")

        if callback_url and auth_token:
            _set_last_message_id_to_callback(callback_url, auth_token, session_id, sent_message_id)


def _send_error_notification(chat_id: str, error_msg: str, reply_to: Optional[str] = None,
                             session_id: str = '', project_dir: str = '',
                             reply_in_thread: bool = False):
    """发送错误通知到飞书

    注意：错误通知仅保存到 MessageSessionStore（支持用户回复继续会话），
    不同步 last_message_id 到 Callback 后端。这是符合预期的行为——
    错误通知不应成为链式回复的锚点，后续正常通知应继续回复到上一条正常消息。

    Args:
        chat_id: 群聊 ID
        error_msg: 错误消息
        reply_to: 要回复的消息 ID（可选）
        session_id: 会话 ID（可选，用于保存消息映射）
        project_dir: 项目目录（可选，用于保存消息映射）
        reply_in_thread: 是否收进话题详情
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if service and service.enabled:
        success, sent_message_id = _send_text_message(service, chat_id, f"⚠️ {error_msg}", reply_to=reply_to,
                                                       reply_in_thread=reply_in_thread)

        # 保存错误通知消息到 MessageSessionStore
        if success and sent_message_id and session_id and project_dir:
            from services.message_session_store import MessageSessionStore
            msg_store = MessageSessionStore.get_instance()
            if msg_store:
                msg_store.save(sent_message_id, session_id, project_dir)
                logger.info(f"[feishu] Saved error notification mapping: {sent_message_id} -> {session_id}")


def _send_text_message(service, chat_id: str, text: str, reply_to: Optional[str] = None,
                       reply_in_thread: bool = False) -> Tuple[bool, str]:
    """发送文本消息

    Args:
        service: FeishuAPIService 实例
        chat_id: 群聊 ID
        text: 消息内容
        reply_to: 要回复的消息 ID（可选），设置后使用回复 API
        reply_in_thread: 是否收进话题详情

    Returns:
        (success, message_id): 成功时返回 (True, message_id)，失败时返回 (False, '')
    """
    try:
        if reply_to:
            # 使用回复消息 API
            success, message_id = service.reply_text(text, reply_to, reply_in_thread)
        else:
            # 使用发送新消息 API
            success, message_id = service.send_text(text, receive_id=chat_id, receive_id_type='chat_id')

        if success:
            logger.info(f"[feishu] Sent notification to {chat_id}: {_sanitize_user_content(text)}, reply_to={reply_to if reply_to else ''}")
            return True, message_id
        else:
            logger.error(f"[feishu] Failed to send notification: {message_id}")
            return False, ''
    except Exception as e:
        logger.error(f"[feishu] Error sending notification: {e}")
        return False, ''


def _send_reject_message(chat_id: str, text: str, reply_to: Optional[str] = None):
    """发送拒绝消息（后台线程调用）

    Args:
        chat_id: 群聊 ID
        text: 消息内容
        reply_to: 要回复的消息 ID（可选）
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if service and service.enabled:
        _send_text_message(service, chat_id, text, reply_to=reply_to)


def _verify_operator_match(operator: dict, owner_id: str) -> bool:
    """验证 owner_id 是否与 operator 中的某个 ID 匹配

    operator 可能包含 open_id、user_id、union_id 等多个字段，
    逐一匹配即可，兼容不同格式的 owner_id 配置。

    Args:
        operator: 飞书事件中的 operator 对象
        owner_id: 配置的 owner_id

    Returns:
        True 表示匹配成功，False 表示匹配失败
    """
    if not operator or not owner_id:
        return False

    # 逐一匹配 operator 中的所有字段值
    for field_value in operator.values():
        if field_value == owner_id:
            logger.info(f"[feishu] Operator verification passed: owner_id={owner_id} matched in operator")
            return True

    return False


def _get_binding_from_event(feishu_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从飞书事件中获取绑定信息

    通过 sender_id 或 operator_id 查询 BindingStore 获取完整绑定信息。

    两种场景：
    1. 用户发送消息触发：feishu_event 包含 sender.sender_id
    2. 用户点击按钮触发：feishu_event 包含 operator（operator 本身就是 id 对象）

    Args:
        feishu_event: 飞书事件数据（包含 sender 或 operator 信息）

    Returns:
        绑定信息字典（包含 auth_token, callback_url 等），未找到返回 None
    """
    from services.binding_store import BindingStore

    binding_store = BindingStore.get_instance()
    if not binding_store:
        logger.warning("[feishu] BindingStore not initialized")
        return None

    # 场景 1: 从 sender 获取（用户发送消息时）
    sender_id_obj = feishu_event.get('sender', {}).get('sender_id', {})
    if sender_id_obj:
        for field_value in sender_id_obj.values():
            if field_value:
                binding = binding_store.get(field_value)
                if binding:
                    logger.info(f"[feishu] Found binding for sender_id={field_value}")
                    return binding
        logger.warning(f"[feishu] No binding found for sender={sender_id_obj}")

    # 场景 2: 从 operator 获取（用户点击按钮时）
    # operator 本身就是 id 对象 {open_id, user_id, union_id}
    operator = feishu_event.get('operator', {})
    if operator:
        for field_value in operator.values():
            if field_value:
                binding = binding_store.get(field_value)
                if binding:
                    logger.info(f"[feishu] Found binding for operator={field_value}")
                    return binding
        logger.warning(f"[feishu] No binding found for operator={operator}")

    return None


def _resolve_claude_command_from_binding(
    binding: Optional[Dict[str, Any]],
    cmd_arg: str
) -> Tuple[bool, str]:
    """从 binding 中获取 claude_commands 并解析用户输入的命令参数

    Args:
        binding: 绑定信息字典（包含 claude_commands）
        cmd_arg: 用户输入的 --cmd 参数值，可以是:
            - 空字符串：返回默认命令（列表第一个）
            - 数字字符串（索引，从 0 开始）
            - 名称子串（大小写敏感匹配）

    Returns:
        (success, result):
            - success=True, result=匹配到的命令字符串
            - success=False, result=错误提示信息
    """
    if not binding:
        return False, '用户未注册，无法获取命令列表'

    commands = binding.get('claude_commands')
    if not commands:
        return False, '该用户注册信息中没有命令列表，请重新注册'

    if not cmd_arg:
        return True, commands[0]

    # 尝试索引匹配
    if cmd_arg.isdigit():
        idx = int(cmd_arg)
        if 0 <= idx < len(commands):
            return True, commands[idx]
        cmd_list = ', '.join(
            '`[{}] {}`'.format(i, c) for i, c in enumerate(commands)
        )
        return False, '索引 {} 超出范围，可用命令: {}'.format(idx, cmd_list)

    # 名称子串匹配
    for cmd in commands:
        if cmd_arg in cmd:
            return True, cmd

    cmd_list = ', '.join(
        '`[{}] {}`'.format(i, c) for i, c in enumerate(commands)
    )
    return False, '未找到匹配的命令，可用命令: {}'.format(cmd_list)


def _build_creating_session_card(selected_dir: str, prompt: str, claude_command: str = '') -> dict:
    """构建"正在创建会话"状态卡片

    Args:
        selected_dir: 选择的工作目录
        prompt: 用户输入的提示词
        claude_command: 使用的 Claude 命令（可选）

    Returns:
        卡片字典（包含 type 和 data）
    """
    elements = [
        {
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '请稍候，正在启动 Claude...'
            }
        },
        {
            'tag': 'hr'
        },
        {
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': f'📁 工作目录：{selected_dir}'
            }
        }
    ]

    if claude_command:
        elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': f'🔧 命令：{claude_command}'
            }
        })

    elements.append({
        'tag': 'div',
        'text': {
            'tag': 'plain_text',
            'content': f'💬 提示词：{prompt}'
        }
    })

    return {
        'type': 'raw',
        'data': {
            'schema': '2.0',
            'config': {'wide_screen_mode': True},
            'header': {
                'title': {'tag': 'plain_text', 'content': '⏳ 正在创建会话'},
                'template': 'blue'
            },
            'body': {
                'direction': 'vertical',
                'elements': elements
            }
        }
    }


def _handle_new_session_form(card_data: dict, form_values: dict) -> Tuple[bool, dict]:
    """处理新会话表单提交（异步模式）

    支持两种操作：
    1. 点击"浏览"按钮 → 返回更新后的卡片（显示子目录列表）
    2. 点击"创建会话"按钮 → 立即返回"处理中"响应，后台异步执行会话创建

    Args:
        card_data: 完整的飞书卡片事件数据
        form_values: 表单提交的数据（包含 recent_dir, custom_dir, prompt, browse_result）

    Returns:
        (handled, response): handled 始终为 True，response 包含 toast 和卡片更新
    """
    event = card_data.get('event', {})
    action = event.get('action', {})

    # 获取触发按钮名称（飞书 Card 2.0 Form 提交时，按钮名称在 action.name）
    trigger_name = action.get('name', '')
    logger.info(f"[feishu] Form trigger_name: {trigger_name}")

    # 从按钮的 value 中提取 chat_id 和 message_id（用户原始消息 ID）
    button_value = action.get('value', {})
    chat_id = button_value.get('chat_id', '')
    message_id = button_value.get('message_id', '')

    # 从表单数据中提取字段
    recent_dir = form_values.get('recent_dir', '')  # 常用目录下拉选择的值
    custom_dir = form_values.get('custom_dir', '')  # 自定义路径输入框的值
    browse_result = form_values.get('browse_result', '')  # 浏览结果下拉选择的值
    prompt = form_values.get('prompt', '')
    claude_command = form_values.get('claude_command', '')  # Command 选择下拉的值

    # 获取 binding（用于解析默认命令和后续请求）
    binding = _get_binding_from_event(event)

    # 如果没有选择命令，从 binding 获取默认命令
    if not claude_command:
        ok, result = _resolve_claude_command_from_binding(binding, '')
        if not ok:
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': result
                }
            }
        claude_command = result

    logger.info(f"[feishu] Form values: recent_dir={recent_dir}, custom_dir={custom_dir}, browse_result={browse_result}, claude_command={claude_command}, prompt={_sanitize_user_content(prompt)}, trigger={trigger_name}")

    if not chat_id:
        logger.warning("[feishu] No chat_id in button value")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '无法获取群聊信息'
            }
        }

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 1: 点击"浏览"按钮（支持 browse_custom_btn 和 browse_result_btn）│
    # └────────────────────────────────────────────────────────────────┘
    if trigger_name in ('browse_dir_select_btn', 'browse_custom_btn', 'browse_result_btn'):
        return _handle_browse_directory(trigger_name, recent_dir, custom_dir, chat_id, message_id, event, form_values)

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 2: 点击"创建会话"按钮（trigger_name = submit_btn）           │
    # └────────────────────────────────────────────────────────────────┘

    # 按优先级确定目录：browse_result > custom_dir > recent_dir
    # 用户从"选择子目录"中选中的优先级最高，其次才是自定义路径输入框
    selected_dir = browse_result or custom_dir or recent_dir

    if not selected_dir:
        logger.warning("[feishu] No working directory selected in form submission")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '请选择或输入一个工作目录'
            }
        }

    if not prompt:
        logger.warning("[feishu] No prompt provided")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '请输入您的问题'
            }
        }

    # 立即返回"处理中"响应
    response = {
        'toast': {
            'type': TOAST_INFO,
            'content': '正在创建会话...'
        },
        'card': _build_creating_session_card(selected_dir, prompt, claude_command)
    }

    # 在后台线程中异步执行会话创建
    _run_in_background(_forward_new_request, (binding, selected_dir, prompt, chat_id, message_id, claude_command))

    return True, response


def _handle_browse_directory(trigger_name: str, recent_dir: str, custom_dir: str,
                             chat_id: str, message_id: str, feishu_event: dict,
                             form_values: dict) -> Tuple[bool, dict]:
    """处理浏览目录按钮点击

    调用 browse-dirs 接口获取子目录列表，返回更新后的卡片。

    Args:
        trigger_name: 触发的按钮名称 (browse_dir_select_btn, browse_custom_btn 或 browse_result_btn)
        recent_dir: 常用目录下拉框选择的值
        custom_dir: 用户输入的自定义路径
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        feishu_event: 飞书事件数据
        form_values: 表单数据（用于回填）

    Returns:
        (handled, response): handled 始终为 True，response 包含更新后的卡片
    """
    # 获取绑定信息
    binding = _get_binding_from_event(feishu_event)
    if not binding:
        logger.warning("[feishu] No binding found for browse")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '无法获取认证信息'
            }
        }

    auth_token = binding.get('auth_token', '')
    callback_url = binding.get('callback_url', '')

    # 从表单数据中获取 browse_result（用户可能从浏览结果下拉菜单中选择了子目录）
    browse_result = form_values.get('browse_result', '')

    # 根据按钮名称确定浏览路径
    if trigger_name == 'browse_dir_select_btn':
        # 点击常用目录旁边的"浏览"：必须先选择目录
        if not recent_dir:
            logger.warning("[feishu] No recent_dir selected")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '请先从常用目录中选择一个目录'
                }
            }
        browse_path = recent_dir
        logger.info(f"[feishu] Browse recent_dir select: {browse_path}")
    elif trigger_name == 'browse_custom_btn':
        # 点击自定义路径旁边的"浏览"：使用 custom_dir
        browse_path = custom_dir or '/'
        logger.info(f"[feishu] Browse custom path: {browse_path}")
    elif trigger_name == 'browse_result_btn':
        # 点击浏览结果旁边的"浏览"：必须先选择子目录
        if not browse_result:
            logger.warning("[feishu] No browse result selected")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '请先从浏览结果中选择一个子目录'
                }
            }
        browse_path = browse_result
        logger.info(f"[feishu] Browse result path: {browse_path}")
    else:
        # 默认：优先使用 custom_dir（用户主动输入），其次使用 recent_dir
        browse_path = custom_dir or recent_dir or '/'
        logger.info(f"[feishu] Browse default path: {browse_path}")

    # 调用 browse-dirs 接口
    browse_data = _fetch_browse_dirs_from_callback(callback_url, auth_token, browse_path)
    if not browse_data:
        logger.error(f"[feishu] Failed to browse dirs: {browse_path}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '浏览目录失败'
            }
        }

    # 计算应该回填到 custom_dir 输入框的值
    if trigger_name == 'browse_result_btn':
        custom_dir_value = browse_result  # 回填为选中的子目录
    elif trigger_name == 'browse_dir_select_btn':
        # 如果自定义输入框有值，保持不变；否则回填为当前浏览路径
        custom_dir_value = custom_dir if custom_dir else browse_data.get('current', '')
    else:  # browse_custom_btn
        custom_dir_value = browse_data.get('current', '')  # 回填为当前浏览路径

    # 构建更新后的卡片
    card = _build_browse_result_card(
        browse_data=browse_data,
        form_values=form_values,
        custom_dir_value=custom_dir_value,  # 传入计算好的回填值
        chat_id=chat_id,
        message_id=message_id,
        feishu_event=feishu_event
    )

    return True, {'card': {'type': 'raw', 'data': card}}


def _build_new_session_card(
    owner_id: str,
    chat_id: str,
    message_id: str,
    recent_dirs: list,
    selected_recent_dir: str = '',
    custom_dir: str = '',
    browse_data: Optional[dict] = None,
    prompt: str = '',
    claude_commands: list = None,
    claude_command: str = ''
) -> dict:
    """构建新建会话卡片（统一构建逻辑）

    Args:
        owner_id: 用户 ID
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        recent_dirs: 常用目录列表
        selected_recent_dir: 常用目录下拉的选中值（回填用）
        custom_dir: 自定义路径输入框默认值
        browse_data: 浏览结果数据 {dirs, parent, current}，为 None 则不显示浏览结果区域
        prompt: 提示词输入框默认值
        claude_commands: 可用的 Claude 命令列表（从 binding 获取）
        claude_command: 预选的 Claude 命令

    Returns:
        飞书卡片字典
    """
    # 构建常用目录下拉选项（显示：folder_name (/full/path)，value 保持完整路径）
    dir_options = []
    for dir_path in recent_dirs:
        # 提取文件夹名称（路径末尾）
        folder_name = dir_path.rstrip('/').split('/')[-1] if dir_path else ''
        # 格式：folder_name (/full/path/to/folder)
        display_text = f"{folder_name} ({dir_path})" if folder_name else dir_path
        dir_options.append({
            'text': {
                'tag': 'plain_text',
                'content': display_text
            },
            'value': dir_path
        })

    # 回调 value（按钮共用）
    callback_value = {
        'owner_id': owner_id,
        'chat_id': chat_id,
        'message_id': message_id
    }

    # 构建 Form 表单元素
    form_elements = []

    # 区域标题：选择工作目录
    form_elements.append({
        'tag': 'div',
        'text': {
            'tag': 'plain_text',
            'content': '1️⃣ 选择工作目录'
        }
    })

    # 常用目录下拉菜单（如果有），标签和下拉框同行
    if recent_dirs:
        # 决定 initial_option
        if selected_recent_dir and selected_recent_dir in [d['value'] for d in dir_options]:
            initial_option = selected_recent_dir
        else:
            initial_option = dir_options[0]['value'] if dir_options else ''

        form_elements.append({
            'tag': 'column_set',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'vertical_align': 'center',
                    'elements': [
                        {
                            'tag': 'div',
                            'text': {
                                'tag': 'plain_text',
                                'content': '常用目录'
                            }
                        }
                    ]
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 4,
                    'elements': [
                        {
                            'tag': 'select_static',
                            'name': 'recent_dir',
                            'placeholder': {
                                'tag': 'plain_text',
                                'content': '选择工作目录'
                            },
                            'width': 'fill',
                            'options': dir_options,
                            'initial_option': initial_option
                        }
                    ]
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'elements': [
                        {
                            'tag': 'button',
                            'name': 'browse_dir_select_btn',
                            'text': {
                                'tag': 'plain_text',
                                'content': '浏览'
                            },
                            'type': 'default',
                            'width': 'fill',
                            'form_action_type': 'submit',
                            'behaviors': [
                                {
                                    'type': 'callback',
                                    'value': callback_value
                                }
                            ]
                        }
                    ]
                }
            ]
        })

    # 自定义路径标签 + 输入框 + 浏览按钮（同行布局）
    form_elements.append({
        'tag': 'column_set',
        'columns': [
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'vertical_align': 'center',
                'elements': [
                    {
                        'tag': 'div',
                        'text': {
                            'tag': 'plain_text',
                            'content': '自定义路径'
                        }
                    }
                ]
            },
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 4,
                'elements': [
                    {
                        'tag': 'input',
                        'name': 'custom_dir',
                        'placeholder': {
                            'tag': 'plain_text',
                            'content': '输入完整路径，如 /home/user/project'
                        },
                        'width': 'fill',
                        'default_value': custom_dir
                    }
                ]
            },
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'elements': [
                    {
                        'tag': 'button',
                        'name': 'browse_custom_btn',
                        'text': {
                            'tag': 'plain_text',
                            'content': '浏览'
                        },
                        'type': 'default',
                        'width': 'fill',
                        'form_action_type': 'submit',
                        'behaviors': [
                            {
                                'type': 'callback',
                                'value': callback_value
                            }
                        ]
                    }
                ]
            }
        ]
    })

    # 浏览结果区域（仅当 browse_data 非空时显示）
    if browse_data is not None:
        current_path = browse_data.get('current', '')
        browse_dirs = browse_data.get('dirs', [])
        browse_options = []
        for dir_path in browse_dirs:
            display_name = dir_path.rstrip('/').split('/')[-1] if dir_path else ''
            browse_options.append({
                'text': {
                    'tag': 'plain_text',
                    'content': display_name
                },
                'value': dir_path
            })

        if browse_options:
            form_elements.append({
                'tag': 'column_set',
                'columns': [
                    {
                        'tag': 'column',
                        'width': 'weighted',
                        'weight': 1,
                        'vertical_align': 'center',
                        'elements': [
                            {
                                'tag': 'div',
                                'text': {
                                    'tag': 'plain_text',
                                    'content': '选择子目录'
                                }
                            }
                        ]
                    },
                    {
                        'tag': 'column',
                        'width': 'weighted',
                        'weight': 4,
                        'elements': [
                            {
                                'tag': 'select_static',
                                'name': 'browse_result',
                                'placeholder': {
                                    'tag': 'plain_text',
                                    'content': f'选择 {current_path} 的子目录'
                                },
                                'width': 'fill',
                                'options': browse_options
                            }
                        ]
                    },
                    {
                        'tag': 'column',
                        'width': 'weighted',
                        'weight': 1,
                        'elements': [
                            {
                                'tag': 'button',
                                'name': 'browse_result_btn',
                                'text': {
                                    'tag': 'plain_text',
                                    'content': '浏览'
                                },
                                'type': 'default',
                                'width': 'fill',
                                'form_action_type': 'submit',
                                'behaviors': [
                                    {
                                        'type': 'callback',
                                        'value': callback_value
                                    }
                                ]
                            }
                        ]
                    }
                ]
            })
        else:
            form_elements.append({
                'tag': 'div',
                'text': {
                    'tag': 'plain_text',
                    'content': f'📁 {current_path} 下没有子目录'
                }
            })

        # 优先级提示文本（有浏览结果时）
        form_elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '💡 优先级：选择子目录 > 自定义路径 > 常用目录'
            }
        })
    else:
        # 优先级提示文本（初始卡片没有浏览子目录选项）
        form_elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '💡 优先级：自定义路径 > 常用目录'
            }
        })

    # Claude Command 选择（仅当配置了多个命令时显示）
    if claude_commands:
        prompt_step = '3️⃣'

        # 分割线：目录选择区域结束
        form_elements.append({'tag': 'hr'})

        form_elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '2️⃣ 选择 Claude Command'
            }
        })

        cmd_options = []
        for i, cmd in enumerate(claude_commands):
            cmd_options.append({
                'text': {
                    'tag': 'plain_text',
                    'content': f'[{i}] {cmd}'
                },
                'value': cmd
            })

        cmd_select = {
            'tag': 'select_static',
            'name': 'claude_command',
            'placeholder': {
                'tag': 'plain_text',
                'content': '选择 Claude 命令'
            },
            'options': cmd_options,
            'width': 'fill'
        }
        if claude_command and claude_command in claude_commands:
            cmd_select['initial_option'] = claude_command
        else:
            cmd_select['initial_option'] = claude_commands[0]

        form_elements.append({
            'tag': 'column_set',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'vertical_align': 'center',
                    'elements': [
                        {
                            'tag': 'div',
                            'text': {
                                'tag': 'plain_text',
                                'content': '命令'
                            }
                        }
                    ]
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 5,
                    'elements': [cmd_select]
                }
            ]
        })
    else:
        prompt_step = '2️⃣'

    # 分割线：cmd / 目录选择区域结束
    form_elements.append({'tag': 'hr'})

    # Prompt 输入框
    form_elements.append({
        'tag': 'div',
        'text': {
            'tag': 'plain_text',
            'content': prompt_step + ' 输入提示词'
        }
    })

    form_elements.append({
        'tag': 'column_set',
        'columns': [
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'vertical_align': 'center',
                'elements': [
                    {
                        'tag': 'div',
                        'text': {
                            'tag': 'plain_text',
                            'content': '提示词'
                        }
                    }
                ]
            },
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 5,
                'elements': [
                    {
                        'tag': 'input',
                        'name': 'prompt',
                        'input_type': 'multiline_text',
                        'placeholder': {
                            'tag': 'plain_text',
                            'content': '请输入您的问题或任务描述'
                        },
                        'width': 'fill',
                        'default_value': prompt or '',
                        # 不设置 required，避免点击"浏览"按钮时被阻止
                        # 服务端会在创建会话时验证 prompt 是否为空
                    }
                ]
            }
        ]
    })

    # 构建卡片
    card = {
        'schema': '2.0',
        'config': {
            'wide_screen_mode': True
        },
        'header': {
            'title': {
                'tag': 'plain_text',
                'content': '🧠 完善信息以创建会话'
            },
            'template': 'blue'
        },
        'body': {
            'direction': 'vertical',
            'elements': [
                {
                    'tag': 'form',
                    'name': 'dir_prompt_form',
                    'elements': form_elements + [
                        {
                            'tag': 'button',
                            'name': 'submit_btn',
                            'text': {
                                'tag': 'plain_text',
                                'content': '创建会话'
                            },
                            'type': 'primary',
                            'form_action_type': 'submit',
                            'behaviors': [
                                {
                                    'type': 'callback',
                                    'value': callback_value
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

    return card


def _build_browse_result_card(browse_data: dict, form_values: dict, custom_dir_value: str,
                              chat_id: str, message_id: str, feishu_event: dict) -> dict:
    """构建包含浏览结果的目录选择卡片

    Args:
        browse_data: browse-dirs 接口返回的数据 {dirs, parent, current}
        form_values: 原始表单数据（用于回填）
        custom_dir_value: 应该回填到 custom_dir 输入框的值
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        feishu_event: 飞书事件数据

    Returns:
        飞书卡片字典
    """
    # 获取 owner_id
    sender = feishu_event.get('sender', {})
    sender_id_obj = sender.get('sender_id', {})
    owner_id = sender_id_obj.get('open_id', '') or sender_id_obj.get('user_id', '')

    # 获取绑定信息
    binding = _get_binding_from_event(feishu_event)
    auth_token = binding.get('auth_token', '') if binding else ''
    callback_url = binding.get('callback_url', '') if binding else ''

    # 获取常用目录列表
    recent_dirs = _fetch_recent_dirs_from_callback(callback_url, auth_token, limit=5) if auth_token else []

    card = _build_new_session_card(
        owner_id=owner_id, chat_id=chat_id, message_id=message_id,
        recent_dirs=recent_dirs,
        selected_recent_dir=form_values.get('recent_dir', ''),
        custom_dir=custom_dir_value,
        browse_data=browse_data,
        prompt=form_values.get('prompt', ''),
        claude_commands=binding.get('claude_commands') if binding else None,
        claude_command=form_values.get('claude_command', '')
    )

    browse_dirs = browse_data.get('dirs', [])
    logger.info(f"[feishu] Built browse result card with {len(browse_dirs)} dirs")

    # 打印完整卡片 JSON 用于调试
    card_json = json.dumps(card, ensure_ascii=True, indent=2)
    logger.info(f"[feishu] Browse result card JSON:\n{card_json}")

    return card


def _handle_card_action(data: dict) -> Tuple[bool, dict]:
    """处理飞书卡片回传交互事件 card.action.trigger

    当用户点击卡片中的 callback 类型按钮或提交 form 表单时，飞书会发送此事件。
    服务器需要在 3 秒内返回响应，可返回 toast 提示。

    支持的动作类型：
    1. allow/always/deny/interrupt: 权限决策
    2. approve_register/deny_register/unbind_register: 注册授权
    3. Form 表单提交：创建新会话时，选择工作目录 + 填写提示词的表单

    Args:
        data: 飞书事件数据

    Returns:
        (handled, toast_response)
    """
    # 打印完整数据用于调试
    logger.info(f"[feishu] _handle_card_action received data:\n{json.dumps(data, ensure_ascii=True, indent=2)}")

    # 提取事件公共信息
    header = data.get('header', {})
    event = data.get('event', {})
    action = event.get('action', {})
    operator = event.get('operator', {})

    # 记录日志
    event_id = header.get('event_id', '')
    user_id = operator.get('open_id', operator.get('user_id', 'unknown'))
    logger.info(f"[feishu] Card action: event_id={event_id}, user={user_id}")

    # 提取数据：callback 按钮的数据在 value 中，form 表单的数据在 form_value 中
    value = action.get('value', {})
    form_value = action.get('form_value', {})

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 统一身份验证：如果卡片 value 中有 owner_id，必须与 operator 匹配    │
    # │ 适用于：Callback 按钮点击、Form 表单提交                          │
    # └────────────────────────────────────────────────────────────────┘
    owner_id = value.get('owner_id', '')
    if owner_id and not _verify_operator_match(operator, owner_id):
        logger.warning(
            f"[feishu] Operator verification failed: owner_id={owner_id} not found in operator={operator}"
        )
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '只有本人才能执行此操作'
            }
        }

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 1: 新会话表单提交（目录选择 + prompt 输入）                    │
    # │ 识别标志：按钮名称为 submit_btn 或 browse_*_btn                   │
    # └────────────────────────────────────────────────────────────────┘
    trigger_name = action.get('name', '')
    new_session_form_buttons = ('submit_btn', 'browse_dir_select_btn', 'browse_custom_btn', 'browse_result_btn')
    if trigger_name in new_session_form_buttons:
        return _handle_new_session_form(data, form_value)

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 2: Callback 按钮点击（权限决策、注册授权等）                   │
    # │ 提取动作参数：action_type, request_id                            │
    # │ callback_url 从 BindingStore 获取（注册场景除外）                  │
    # └────────────────────────────────────────────────────────────────┘
    action_type = value.get('action', '')  # allow/always/deny/interrupt/approve_register/deny_register
    request_id = value.get('request_id', '')

    logger.info(
        f"[feishu] Card action: action={action_type}, request_id={request_id}"
    )

    # 处理注册授权
    if action_type in ('approve_register', 'deny_register', 'unbind_register'):
        return handle_card_action_register(value)

    # 处理权限决策
    if not action_type or not request_id:
        logger.warning(f"[feishu] Card action missing params: action={action_type}, request_id={request_id}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '无效的回调请求'
            }
        }

    # 调用 callback_url 的决策接口（callback_url 从 BindingStore 获取）
    return _forward_permission_request(request_id, data, action_type)


def _forward_permission_request(request_id: str, original_data: dict, action_type: str) -> Tuple[bool, dict]:
    """转发权限请求到 Callback 服务

    调用 callback 服务的纯决策接口，根据返回的决策结果生成 toast。
    callback_url 从 BindingStore 获取。

    注意：飞书要求在 3 秒内返回响应，timeout 设置为 2 秒预留时间。

    Args:
        request_id: 请求 ID
        original_data: 原始飞书事件数据（用于提取绑定信息和 project_dir）
        action_type: 动作类型 (allow/always/deny/interrupt)

    Returns:
        (handled, toast_response)
    """
    import urllib.error

    # 提取 project_dir（从原始请求的 value 中获取）
    event = original_data.get('event', {})
    action = event.get('action', {})
    value = action.get('value', {})

    # 获取绑定信息
    binding = _get_binding_from_event(event)
    if not binding:
        logger.warning("[feishu] No binding found for permission request")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '身份验证失败，请重新注册网关'
            }
        }

    auth_token = binding.get('auth_token', '')
    callback_url = binding.get('callback_url', '')

    if not callback_url:
        logger.warning("[feishu] No callback_url in binding for permission request")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '回调地址未配置'
            }
        }

    # 构建请求数据
    request_data = {
        'action': action_type,
        'request_id': request_id
    }

    # 添加可选字段
    if 'project_dir' in value:
        request_data['project_dir'] = value['project_dir']

    # 规范化 URL
    callback_url = callback_url.rstrip('/')
    api_url = f"{callback_url}/cb/decision"

    logger.info(f"[feishu] Forwarding to {api_url}: {request_data}")

    start_time = time.time()

    try:
        # 飞书要求 3 秒内返回，设置 2 秒超时预留处理时间
        response_data = _post_json(api_url, request_data, auth_token=auth_token, timeout=2)
        elapsed = (time.time() - start_time) * 1000

        success = response_data.get('success', False)
        decision = response_data.get('decision')
        message = response_data.get('message', '')

        # 根据决策结果生成 toast
        if success and decision:
            if decision == 'allow':
                toast_type = TOAST_SUCCESS
            else:  # deny
                toast_type = TOAST_WARNING
            toast_content = message or ('已批准运行' if decision == 'allow' else '已拒绝运行')
            logger.info(f"[feishu] Decision succeeded: decision={decision}, message={message}, elapsed={elapsed:.0f}ms")
        else:
            toast_type = TOAST_ERROR
            toast_content = message or '处理失败'
            logger.warning(f"[feishu] Decision failed: message={toast_content}, elapsed={elapsed:.0f}ms")

        return True, {
            'toast': {
                'type': toast_type,
                'content': toast_content
            }
        }

    except urllib.error.HTTPError as e:
        logger.error(f"[feishu] Forward HTTP error: {e.code} {e.reason}")
        # 401 表示 auth_token 验证失败
        if e.code == 401:
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '身份验证失败，请重新注册网关'
                }
            }
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': f'回调服务错误: HTTP {e.code}'
            }
        }
    except urllib.error.URLError as e:
        logger.error(f"[feishu] Forward URL error: {e.reason}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '回调服务不可达，请检查服务状态'
            }
        }
    except socket.timeout:
        logger.error("[feishu] Forward timeout")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '回调服务响应超时'
            }
        }
    except Exception as e:
        logger.error(f"[feishu] Forward error: {e}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': f'转发失败: {str(e)}'
            }
        }


def handle_card_action_register(value: dict) -> Tuple[bool, dict]:
    """处理注册授权卡片的按钮回调

    Args:
        value: 按钮的 value 数据
            - action: approve_register/deny_register/unbind_register
            - callback_url: Callback 后端 URL
            - owner_id: 飞书用户 ID
            - request_ip: 注册来源 IP（仅 approve_register 需要）
            - reply_in_thread: 是否使用回复话题模式（仅 approve_register 需要）
            - claude_commands: 可用的 Claude 命令列表（仅 approve_register 需要）

    Returns:
        (handled, response) - response 包含 toast 和可选的 card 更新
    """
    from handlers.register import handle_authorization_decision, handle_register_unbind

    action = value.get('action', '')
    callback_url = value.get('callback_url', '')
    owner_id = value.get('owner_id', '')
    request_ip = value.get('request_ip', '')
    reply_in_thread = value.get('reply_in_thread', False)
    claude_commands = value.get('claude_commands', None)

    if action == 'approve_register':
        logger.info(f"[feishu] Registration approved: owner_id={owner_id}, callback_url={callback_url}, reply_in_thread={reply_in_thread}")
        return True, handle_authorization_decision(
            callback_url, owner_id, request_ip, approved=True, reply_in_thread=reply_in_thread, claude_commands=claude_commands
        )
    elif action == 'deny_register':
        logger.info(f"[feishu] Registration denied: owner_id={owner_id}")
        return True, handle_authorization_decision(
            callback_url, owner_id, request_ip, approved=False
        )
    elif action == 'unbind_register':
        logger.info(f"[feishu] Registration unbound: owner_id={owner_id}, callback_url={callback_url}")
        return True, handle_register_unbind(callback_url, owner_id)
    else:
        logger.warning(f"[feishu] Unknown register action: {action}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '未知的操作'
            }
        }


def _parse_command_args(args: str) -> Tuple[bool, str, str, str]:
    """解析指令参数，提取 --dir=、--cmd= 和 prompt

    支持格式（参数顺序不限）：
    - --dir=/path --cmd=1 prompt
    - --cmd=opus --dir=/path prompt
    - --dir=/path prompt
    - --cmd=opus prompt
    - prompt（回复模式）

    Args:
        args: 参数部分（不含指令名）

    Returns:
        (success, project_dir, cmd_arg, prompt)
    """
    args = args.strip()
    if not args:
        return True, '', '', ''

    # 检查是否有 --dir= 或 --cmd= 参数
    has_named_args = args.startswith('--dir=') or args.startswith('--cmd=')
    if not has_named_args:
        return True, '', '', args

    try:
        parts = shlex.split(args, posix=False)
    except ValueError as e:
        logger.warning(f"[feishu] Failed to parse command args: {e}")
        return False, '', '', ''

    project_dir = ''
    cmd_arg = ''
    prompt_parts = []

    for part in parts:
        if part.startswith('--dir='):
            project_dir = part[6:]
        elif part.startswith('--cmd='):
            cmd_arg = part[6:]
        else:
            prompt_parts.append(part)

    prompt = ' '.join(prompt_parts)
    return True, project_dir, cmd_arg, prompt


def _fetch_recent_dirs_from_callback(callback_url: str, auth_token: str, limit: int = 5) -> list:
    """从 Callback 后端获取近期常用目录列表

    Args:
        callback_url: Callback 后端 URL
        auth_token: 认证令牌
        limit: 最多返回的目录数量

    Returns:
        目录路径列表
    """
    import urllib.error

    if not callback_url:
        logger.warning("[feishu] No callback_url available for fetching recent dirs")
        return []

    api_url = f"{callback_url.rstrip('/')}/cb/claude/recent-dirs"
    request_data = {
        'limit': limit
    }

    try:
        response_data = _post_json(api_url, request_data, auth_token=auth_token, timeout=5)
        recent_dirs = response_data.get('dirs', [])
        logger.info(f"[feishu] Fetched {len(recent_dirs)} recent dirs from callback")
        return recent_dirs

    except urllib.error.HTTPError as e:
        logger.error(f"[feishu] Fetch recent dirs HTTP error: {e.code} {e.reason}")
        return []
    except urllib.error.URLError as e:
        logger.error(f"[feishu] Fetch recent dirs URL error: {e.reason}")
        return []
    except Exception as e:
        logger.error(f"[feishu] Fetch recent dirs error: {e}")
        return []


def _fetch_browse_dirs_from_callback(callback_url: str, auth_token: str, path: str) -> dict:
    """从 Callback 后端获取指定路径下的子目录列表

    Args:
        callback_url: Callback 后端 URL
        auth_token: 认证令牌
        path: 要浏览的路径

    Returns:
        包含 dirs, parent, current 的字典，失败时返回空字典
    """
    import urllib.error

    if not callback_url:
        logger.warning("[feishu] No callback_url available for browsing dirs")
        return {}

    api_url = f"{callback_url.rstrip('/')}/cb/claude/browse-dirs"
    request_data = {
        'path': path
    }

    try:
        response_data = _post_json(api_url, request_data, auth_token=auth_token, timeout=5)
        logger.info(f"[feishu] Fetched browse result: {len(response_data.get('dirs', []))} dirs from {path}")
        return response_data

    except urllib.error.HTTPError as e:
        logger.error(f"[feishu] Browse dirs HTTP error: {e.code} {e.reason}")
        return {}
    except urllib.error.URLError as e:
        logger.error(f"[feishu] Browse dirs URL error: {e.reason}")
        return {}
    except Exception as e:
        logger.error(f"[feishu] Browse dirs error: {e}")
        return {}


def _set_last_message_id_to_callback(callback_url: str, auth_token: str,
                                     session_id: str, message_id: str) -> bool:
    """通过 Callback 后端设置 session 的 last_message_id

    Args:
        callback_url: Callback 后端 URL
        auth_token: 认证令牌
        session_id: Claude 会话 ID
        message_id: 飞书消息 ID

    Returns:
        是否设置成功
    """
    import urllib.error

    if not callback_url:
        logger.warning("[feishu] No callback_url available for setting last_message_id")
        return False

    api_url = f"{callback_url.rstrip('/')}/cb/session/set-last-message-id"
    request_data = {
        'session_id': session_id,
        'message_id': message_id
    }

    try:
        response_data = _post_json(api_url, request_data, auth_token=auth_token, timeout=5)
        success = response_data.get('success', False)
        if success:
            logger.info(f"[feishu] Set last_message_id via callback: session={session_id}, message_id={message_id}")
        else:
            logger.warning(f"[feishu] Failed to set last_message_id: {response_data.get('error', 'unknown')}")
        return success

    except urllib.error.HTTPError as e:
        logger.error(f"[feishu] Set last_message_id HTTP error: {e.code} {e.reason}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"[feishu] Set last_message_id URL error: {e.reason}")
        return False
    except Exception as e:
        logger.error(f"[feishu] Set last_message_id error: {e}")
        return False


def _send_new_session_card(binding: dict, owner_id: str, chat_id: str,
                           message_id: str, project_dir: str, prompt: str,
                           claude_command: str = ''):
    """发送工作目录选择卡片

    Args:
        binding: 绑定信息（包含 auth_token, callback_url, claude_commands 等）
        owner_id: 用户 ID
        chat_id: 群聊 ID
        message_id: 原始消息 ID（用于回复）
        project_dir: 项目目录（用作 custom_dir 输入框的默认值）
        prompt: 用户输入的 prompt（作为 prompt 输入框的默认值）
        claude_command: 预选的 Claude 命令（可选，来自 --cmd 参数）
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.warning("[feishu] FeishuAPIService not enabled, cannot send new session card")
        return

    if not binding:
        logger.warning("[feishu] No binding found, cannot fetch recent dirs")
        _run_in_background(_send_reject_message, (chat_id, "您尚未注册，无法使用此功能", message_id))
        return

    auth_token = binding.get('auth_token', '')
    callback_url = binding.get('callback_url', '')
    reply_in_thread = binding.get('reply_in_thread', False)

    # 从 Callback 后端获取常用目录列表
    recent_dirs = _fetch_recent_dirs_from_callback(callback_url, auth_token, limit=5)

    card = _build_new_session_card(
        owner_id=owner_id, chat_id=chat_id, message_id=message_id,
        recent_dirs=recent_dirs,
        custom_dir=project_dir or '',
        prompt=prompt,
        claude_commands=binding.get('claude_commands'),
        claude_command=claude_command
    )

    # 打印完整卡片 JSON 用于调试
    card_json = json.dumps(card, ensure_ascii=True, indent=2)
    logger.info(f"[feishu] Dir selector card JSON:\n{card_json}")

    if message_id:
        success, sent_message_id = service.reply_card(json.dumps(card, ensure_ascii=False), message_id, reply_in_thread)
    else:
        success, sent_message_id = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=chat_id, receive_id_type='chat_id')

    if success:
        logger.info(f"[feishu] Sent new session card to {chat_id}, card_msg_id={sent_message_id}")
    else:
        logger.error(f"[feishu] Failed to send new session card: {sent_message_id}")


def _handle_new_command(data: dict, args: str):
    """处理 /new 指令，发起新的 Claude 会话

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /new）
    """
    from services.message_session_store import MessageSessionStore

    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    parent_id = message.get('parent_id', '')

    # 解析指令参数（支持 --dir= 和 --cmd=）
    success, project_dir, cmd_arg, prompt = _parse_command_args(args)
    if not success:
        _run_in_background(_send_reject_message, (chat_id, "参数格式错误，正确格式：`/new --dir=/path/to/project [--cmd=0] prompt`", message_id))
        return

    # 解析 --cmd 参数（从 binding 获取命令列表）
    binding = _get_binding_from_event(event)
    ok, result = _resolve_claude_command_from_binding(binding, cmd_arg)
    if not ok:
        _run_in_background(_send_reject_message, (chat_id, result, message_id))
        return
    claude_command = result

    # 获取 owner_id（用于传递给下游函数）
    sender = event.get('sender', {})
    sender_id_obj = sender.get('sender_id', {})
    owner_id = sender_id_obj.get('open_id', '') or sender_id_obj.get('user_id', '')

    # 如果没有 project_dir，尝试从 parent_id 查询
    if not project_dir and parent_id:
        store = MessageSessionStore.get_instance()
        if store:
            mapping = store.get(parent_id)
            if mapping:
                project_dir = mapping.get('project_dir', '')
        else:
            _run_in_background(_send_reject_message, (chat_id, "服务未就绪，请稍后重试", message_id))
            return

    # 验证参数：如果没有目录或没有提示词，发送卡片让用户完善
    if not project_dir or not prompt:
        _run_in_background(_send_new_session_card, (binding, owner_id, chat_id, message_id, project_dir, prompt, claude_command))
        return

    logger.info(f"[feishu] /new command: dir={project_dir}, cmd={claude_command or '(default)'}, prompt={_sanitize_user_content(prompt)}")

    # 在后台线程中转发到 Callback 后端
    _run_in_background(_forward_new_request, (binding, project_dir, prompt, chat_id, message_id, claude_command))


def _forward_new_request(binding: dict, project_dir: str, prompt: str,
                         chat_id: str, message_id: str, claude_command: str = ''):
    """转发新建会话请求到 Callback 后端

    Args:
        binding: 绑定信息（包含 auth_token, callback_url 等）
        project_dir: 项目工作目录
        prompt: 用户输入的 prompt
        chat_id: 群聊 ID
        message_id: 原始消息 ID（用作 reply_to）
        claude_command: 指定使用的 Claude 命令（可选）
    """
    if not binding:
        logger.warning("[feishu] No binding found, cannot create session")
        # 注意：此处不关联会话，因为会话尚未创建（用户未注册）
        # 用户回复此错误通知没有意义，应先完成注册
        _send_error_notification(chat_id, "您尚未注册，无法使用此功能", reply_to=message_id)
        return

    auth_token = binding.get('auth_token', '')
    callback_url = binding.get('callback_url', '')
    reply_in_thread = binding.get('reply_in_thread', False)

    data = {
        'project_dir': project_dir,
        'prompt': prompt,
        'chat_id': chat_id,
        'message_id': message_id
    }
    if claude_command:
        data['claude_command'] = claude_command

    # 新建会话时传递 reply_to，让第一条通知回复用户的 /new 消息
    # 后续通知会通过 last_message_id 链式回复
    _forward_claude_request(callback_url, '/cb/claude/new', auth_token,
                            data, chat_id, reply_to=message_id,
                            reply_in_thread=reply_in_thread)


def handle_send_message(binding: Dict[str, Any], data: dict) -> Tuple[bool, dict]:
    """处理 /gw/feishu/send 请求，通过 OpenAPI 发送消息

    Args:
        binding: 绑定信息（由调用方鉴权后传入）
        data: 请求 JSON 数据
            - owner_id: 飞书用户 ID（必需，作为接收者或备用）
            - msg_type: 消息类型 interactive/text/image（必需，暂仅支持 interactive）
            - content: 消息内容（必需）
                - card: 卡片 JSON 对象
                - text: 文本内容
                - image_key: 图片的 key
            - chat_id: 群聊 ID（可选，优先使用）
            - receive_id_type: 接收者类型（可选，默认自动检测）
            - session_id: Claude 会话 ID（可选，用于继续会话）
            - project_dir: 项目工作目录（可选，用于继续会话）
            - reply_to_message_id: 要回复的消息 ID（可选，使用 reply API）

    Returns:
        (handled, response): handled 始终为 True，response 包含结果

    Note:
        receive_id 优先级：chat_id 参数 > owner_id
        当提供 reply_to_message_id 时，使用 reply API 发送消息到话题流
    """
    from services.feishu_api import FeishuAPIService, detect_receive_id_type

    msg_type = data.get('msg_type')
    content = data.get('content')
    owner_id = data.get('owner_id', '')
    chat_id = data.get('chat_id', '')

    # 提取 session 相关参数
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    reply_to_message_id = data.get('reply_to_message_id', '') or ''

    if not msg_type:
        logger.warning("[feishu] /gw/feishu/send: missing msg_type")
        return True, {'success': False, 'error': 'Missing msg_type'}

    if not owner_id:
        logger.warning("[feishu] /gw/feishu/send: missing owner_id")
        return True, {'success': False, 'error': 'Missing owner_id'}

    # 确定 receive_id 和 receive_id_type
    # 优先级：传入的 chat_id > owner_id
    if chat_id:
        receive_id = chat_id
        receive_id_type = 'chat_id'
    else:
        receive_id = owner_id
        receive_id_type = data.get('receive_id_type', '') or detect_receive_id_type(owner_id)

    service = FeishuAPIService.get_instance()
    if service is None or not service.enabled:
        logger.warning("[feishu] /gw/feishu/send: service not enabled")
        return True, {'success': False, 'error': 'Feishu API service not enabled'}

    reply_in_thread = binding.get('reply_in_thread', False)

    # 尝试清除 reply_to 消息上的 Typing 表情（新建/继续会话的 processing 阶段可能添加了该表情）
    # 多数场景下消息上并无此表情，remove_reaction 查询到空列表后会直接返回，无副作用
    if reply_to_message_id:
        service.remove_reaction(reply_to_message_id, 'Typing')

    success = False
    sent_message_id = ''

    if msg_type == 'interactive':
        # content 直接是 card 对象
        if not content:
            logger.warning("[feishu] /gw/feishu/send: missing card content")
            return True, {'success': False, 'error': 'Missing card content'}

        # 如果是 dict 类型，转为 JSON 字符串
        card_json = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

        if reply_to_message_id:
            success, sent_message_id = service.reply_card(card_json, reply_to_message_id, reply_in_thread)
            if not success:
                logger.warning(f"[feishu] /gw/feishu/send: reply_card failed, fallback to send: {sent_message_id}")
                success, sent_message_id = service.send_card(card_json, receive_id, receive_id_type)
        else:
            success, sent_message_id = service.send_card(card_json, receive_id, receive_id_type)

    elif msg_type == 'text':
        text = content if isinstance(content, str) else content.get('text', '')
        if not text:
            logger.warning("[feishu] /gw/feishu/send: missing text content")
            return True, {'success': False, 'error': 'Missing text content'}

        if reply_to_message_id:
            success, sent_message_id = service.reply_text(text, reply_to_message_id, reply_in_thread)
            if not success:
                logger.warning(f"[feishu] /gw/feishu/send: reply_text failed, fallback to send: {sent_message_id}")
                success, sent_message_id = service.send_text(text, receive_id, receive_id_type)
        else:
            success, sent_message_id = service.send_text(text, receive_id, receive_id_type)

    else:
        logger.warning(f"[feishu] /gw/feishu/send: unsupported msg_type: {msg_type}")
        return True, {'success': False, 'error': f'Unsupported msg_type: {msg_type}'}

    if not success:
        # sent_message_id 此时实际是错误信息
        logger.error(f"[feishu] /gw/feishu/send: failed, error={sent_message_id}")
        return True, {'success': False, 'error': sent_message_id}

    logger.info(f"[feishu] /gw/feishu/send: message sent to {receive_id} ({receive_id_type}), id={sent_message_id}, reply_to={reply_to_message_id}")

    # 保存到本地 MessageSessionStore（飞书网关维护）
    if sent_message_id and session_id and project_dir:
        from services.message_session_store import MessageSessionStore
        msg_store = MessageSessionStore.get_instance()
        if msg_store:
            msg_store.save(sent_message_id, session_id, project_dir)

    # 通过 Callback 后端设置 last_message_id
    if sent_message_id and session_id and project_dir and binding:
        callback_url = binding.get('callback_url', '')
        auth_token_from_binding = binding.get('auth_token', '')
        if callback_url and auth_token_from_binding:
            _set_last_message_id_to_callback(callback_url, auth_token_from_binding,
                                              session_id, sent_message_id)

    return True, {'success': True, 'message_id': sent_message_id}


def _handle_reply_command(data: dict, args: str):
    """处理 /reply 指令，在回复消息时指定 Claude Command 继续会话

    仅在回复消息时可用。支持 --cmd= 参数。

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /reply）
    """
    from services.message_session_store import MessageSessionStore

    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    parent_id = message.get('parent_id', '')

    # /reply 仅在回复消息时可用
    if not parent_id:
        _run_in_background(_send_reject_message, (chat_id, "`/reply` 指令仅支持在回复消息时使用", message_id))
        return

    # 解析参数
    success, project_dir, cmd_arg, prompt = _parse_command_args(args)
    if not success:
        _run_in_background(_send_reject_message, (chat_id, "参数格式错误，正确格式：`/reply [--cmd=0] prompt`", message_id))
        return

    if project_dir:
        _run_in_background(_send_reject_message, (chat_id, "`/reply` 不支持 `--dir` 参数，会话目录由原始 session 决定。请去掉 `--dir` 后重试", message_id))
        return

    if not prompt:
        _run_in_background(_send_reject_message, (chat_id, "请提供问题内容，格式：`/reply [--cmd=0] prompt`", message_id))
        return

    # 解析 --cmd 参数（从 binding 获取命令列表）
    binding = _get_binding_from_event(event)
    claude_command = ''
    if cmd_arg:
        ok, result = _resolve_claude_command_from_binding(binding, cmd_arg)
        if not ok:
            _run_in_background(_send_reject_message, (chat_id, result, message_id))
            return
        claude_command = result

    # 查询映射
    store = MessageSessionStore.get_instance()
    if not store:
        _run_in_background(_send_reject_message, (chat_id, "会话存储服务未初始化，请稍后重试或联系管理员", message_id))
        return

    mapping = store.get(parent_id)
    if not mapping:
        _run_in_background(_send_reject_message, (chat_id, "无法找到对应的会话（可能已过期或被清理），请重新发起 /new 指令", message_id))
        return

    logger.info(f"[feishu] /reply command: session={mapping.get('session_id', '')}, cmd={claude_command or '(default)'}, prompt={_sanitize_user_content(prompt)}")

    # 转发到 Callback 后端
    _run_in_background(_forward_continue_request, (binding, mapping['session_id'], mapping['project_dir'], prompt, chat_id, message_id, claude_command))


# =============================================================================
# 命令映射（放在文件末尾，避免函数未定义的问题）
# =============================================================================

# 支持的命令映射：命令名 -> (处理函数, 帮助文本)
_COMMANDS = {
    'new': (_handle_new_command, "发起新的 Claude 会话\n格式：`/new --dir=/path/to/project [--cmd=0] prompt` 或回复消息时 `/new prompt`"),
    'reply': (_handle_reply_command, "回复消息时指定 Claude Command 继续会话\n格式：`/reply [--cmd=0] prompt`\n仅支持在回复消息时使用"),
}
