"""
Feishu Handler - 飞书事件处理器

处理飞书相关的 POST 请求：
    - URL 验证（type: url_verification）
    - 消息事件（im.message.receive_v1）
    - 卡片回传交互（card.action.trigger）
    - 发送消息（/gw/feishu/send）

WebSocket 隧道支持：
    - _forward_via_ws_or_http(): 优先通过 WS 隧道转发请求，失败时 fallback 到 HTTP
    - 适用于 Callback 后端不可公网访问的场景（本地开发、内网部署）
"""

import copy
import hmac
import json
import logging
import os
import re
import shlex
import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

# setup_logging 由 main.py 启动时将 shared/ 加入 sys.path
from logging_config import setup_logging

from handlers.utils import run_in_background as _run_in_background, post_json as _post_json
from services.session_facade import SessionFacade

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


# =============================================================================
# WebSocket 隧道路由分发
# =============================================================================

def _forward_via_ws_or_http(binding: Dict[str, Any], endpoint: str, payload: Dict[str, Any],
                            timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """通过 WS 或 HTTP 转发请求到 Callback

    根据 callback_url 协议决定转发方式：
    - ws:// 或 wss:// → 通过 WebSocket 隧道转发
    - http:// 或 https:// → 通过 HTTP 请求转发

    从 binding 字典中提取路由信息（owner_id、callback_url、auth_token）。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        endpoint: API 端点（如 /cb/decision, /cb/claude/new）
        payload: 请求数据
        timeout: 请求超时（秒），默认使用各通道的默认超时

    Returns:
        响应数据，失败返回 None
    """
    from services.ws_registry import WebSocketRegistry

    owner_id = binding.get('_owner_id', '')
    callback_url = binding.get('callback_url', '')
    auth_token = binding.get('auth_token', '')

    # 根据 callback_url 协议决定转发方式
    is_ws_mode = callback_url.startswith(('ws://', 'wss://'))

    if is_ws_mode:
        # 尝试通过 WS 转发
        registry = WebSocketRegistry.get_instance()
        if owner_id and registry and registry.is_authenticated(owner_id):
            # 获取该连接的 auth_token 用于本地 handler 验证
            ws_auth_token = registry.get_auth_token(owner_id)
            headers = {'X-Auth-Token': ws_auth_token} if ws_auth_token else {}
            response = registry.send_request(owner_id, endpoint, payload, headers, timeout=timeout)
            if response is not None:
                # WS 隧道返回格式: {status: HTTP码, body: 业务响应}
                # 提取 body 作为真正的业务响应
                return response.get('body', response)
        logger.warning("[feishu] WS tunnel not available for %s", owner_id)
        return None

    # HTTP 模式（ws:// 或 wss:// 是 WS 隧道地址，不能用于 HTTP 请求）
    if callback_url:
        api_url = f"{callback_url.rstrip('/')}{endpoint}"
        http_timeout = int(timeout) if timeout else 10
        logger.debug("[feishu] Using HTTP for %s: %s", owner_id, api_url)
        try:
            return _post_json(api_url, payload, auth_token=auth_token, timeout=http_timeout)
        except Exception as e:
            logger.error("[feishu] HTTP request failed: %s", e)
            return None

    logger.warning("[feishu] No callback_url configured for %s", owner_id)
    return None


def _should_reply_in_thread(binding: Dict[str, Any], project_dir: str) -> bool:
    """判断是否应该回复到话题

    当工作目录为该用户的默认聊天目录且未开启话题跟随时，不回复到话题。

    Args:
        binding: 绑定信息
        project_dir: 项目工作目录

    Returns:
        是否回复到话题
    """
    # 优先使用 session_mode 判断
    session_mode = binding.get('session_mode', '')
    if session_mode in ('message', 'thread', 'group'):
        # session_mode 明确设置：thread 模式回复话题，其他模式不回复
        if session_mode == 'thread':
            # 仍需检查 default_chat_dir 覆盖逻辑
            default_chat_dir = binding.get('default_chat_dir', '')
            if project_dir and default_chat_dir and os.path.realpath(project_dir) == os.path.realpath(default_chat_dir):
                if not binding.get('default_chat_follow_thread', True):
                    return False
            return True
        return False

    # 向后兼容：没有 session_mode 时使用 reply_in_thread 判断
    default_chat_dir = binding.get('default_chat_dir', '')
    if project_dir and default_chat_dir and os.path.realpath(project_dir) == os.path.realpath(default_chat_dir):
        # DEFAULT_CHAT_FOLLOW_THREAD=false 时，默认聊天目录的回复强制在主界面显示
        # DEFAULT_CHAT_FOLLOW_THREAD=true（默认）时，使用 reply_in_thread（由全局 FEISHU_REPLY_IN_THREAD 控制）
        if not binding.get('default_chat_follow_thread', True):
            return False
    return binding.get('reply_in_thread', False)


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


def handle_feishu_request(data: dict, skip_token_validation: bool = False) -> Tuple[bool, dict]:
    """处理飞书请求

    支持的请求类型：
        - url_verification: URL 验证
        - im.message.receive_v1: 消息接收事件
        - card.action.trigger: 卡片回传交互事件

    Args:
        data: 请求 JSON 数据
        skip_token_validation: 跳过 token 验证（长连接模式使用）

    Returns:
        (handled, response): handled 表示是否处理了请求，response 是响应数据
    """
    # URL 验证请求（优先处理，无需验证 token）
    if data.get('type') == 'url_verification':
        return _handle_url_verification(data)

    # 验证 Verification Token（HTTP 回调模式需要，长连接模式跳过）
    if not skip_token_validation and not _verify_token(data):
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


def _is_at_bot(message: dict) -> bool:
    """检查消息是否 @ 了机器人

    通过 message.mentions 数组精确匹配：
    1. 优先用 bot_info.app_id 与 FEISHU_APP_ID 比较
    2. 降级用 id.open_id 与 bot_open_id 比较

    Args:
        message: 飞书消息对象（event.message）

    Returns:
        是否 @ 了机器人
    """
    mentions = message.get('mentions', [])
    if not mentions:
        return False

    from config import FEISHU_APP_ID

    # 方法1：通过 bot_info.app_id 精确匹配（最可靠）
    if FEISHU_APP_ID:
        for m in mentions:
            if m.get('mentioned_type') == 'bot':
                bot_info = m.get('bot_info', {})
                if isinstance(bot_info, dict) and bot_info.get('app_id') == FEISHU_APP_ID:
                    return True

    # 方法2：通过 open_id 匹配（降级方案）
    from services.feishu_api import FeishuAPIService
    service = FeishuAPIService.get_instance()
    if service:
        bot_open_id = service.bot_open_id
        if bot_open_id:
            for m in mentions:
                mention_id = m.get('id', {})
                if isinstance(mention_id, dict):
                    if mention_id.get('open_id') == bot_open_id:
                        return True
                elif isinstance(mention_id, str) and mention_id == bot_open_id:
                    return True

    return False


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

    # 检查是否 @ 了机器人（通过 mentions 字段判断）
    message['is_at_bot'] = _is_at_bot(message)
    logger.debug(f"[feishu] is_at_bot={message['is_at_bot']}, mentions_count={len(message.get('mentions', []))}")

    # 获取用户绑定信息（后续处理统一使用）
    binding = _get_binding_from_event(event)

    # 未注册用户：根据 chat_type 和 is_at_bot 决定是否响应
    # - p2p 消息（单聊）：无论是否 @bot，都提示未注册
    # - group 消息（群聊）：只有 @bot 时才提示未注册，否则忽略
    if not binding:
        is_p2p = (chat_type == 'p2p')
        should_respond = is_p2p or message.get('is_at_bot', False)

        if should_respond:
            user_id = sender.get('sender_id', {}).get('user_id', sender_id)
            gateway_ws_url = _get_gateway_ws_url()
            hint = "您（用户 ID：`%s`）尚未注册，无法使用此功能。" % user_id
            if gateway_ws_url:
                hint += "\n\n请在部署了 Claude Code 的系统终端上执行以下命令完成注册：\n" \
                        "```\ncurl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/refs/heads/main/setup.sh | bash -s -- --gateway-url=%s --owner-id=%s\n```" \
                        "\n如果网关地址（`--gateway-url`）非公网可达，请联系管理员获取对外可用的网关地址。" \
                        "\n\n注意：执行命令前，请先申请当前应用的使用权限，否则将无法接收到注册绑定卡片。如未申请，请先申请权限后再执行命令。" % (gateway_ws_url, user_id)
            _run_in_background(_send_notice_message, (chat_id, hint, message_id))
        return

    # 群聊 @bot 过滤：at_bot_only=true 时，群聊中非 @bot 的消息静默忽略
    if chat_type == 'group' and not message.get('is_at_bot', False):
        if binding.get('at_bot_only', False):
            logger.debug("[feishu] Ignored non-@bot message in group: chat=%s msg=%s",
                         chat_id, message_id)
            return

    # 检查是否是命令（优先处理，因为命令也可能是回复消息）
    is_command, command, args = _parse_command(text)
    if is_command:
        _handle_command(data, command, args)
        return

    # 非命令消息：空内容早退（图片/贴图/非文字消息等均 text 为空）
    prompt = text.strip()
    if not prompt:
        hint = "消息内容为空，无法继续会话" if parent_id else "消息内容为空，请发送文字消息与我对话"
        _run_in_background(_send_notice_message, (chat_id, hint, message_id))
        return

    # 路由到已有 session：优先 parent_id，其次 group 模式群聊 chat_id 反查
    route_info = SessionFacade.resolve_from_message(data, binding)
    route_source = route_info['source']

    # 若当前 session 处于静音状态，先自动解除（同步调用；解除失败不影响后续转发）
    _auto_unmute_if_needed(binding, route_info, chat_id, message_id)

    if SessionFacade.RouteSource.is_parent_not_found(route_source):
        _run_in_background(_send_notice_message,
                           (chat_id,
                            _SESSION_NOT_FOUND_HINT + "请稍后重试或重新发起 /new 指令。",
                            message_id))
        return

    if SessionFacade.RouteSource.is_resolved(route_source):
        logger.info("[feishu] Route to session (%s): session_id=%s, prompt=%s",
                    route_source, route_info['session_id'], _sanitize_user_content(prompt))
        # 刷新群聊活跃时间（供自动解散空闲判断）
        if route_source == SessionFacade.RouteSource.GROUP_CHAT:
            from services.group_session_store import GroupSessionStore
            gs_store = GroupSessionStore.get_instance()
            owner_id = binding.get('_owner_id', '')
            if gs_store and owner_id:
                gs_store.touch(owner_id, chat_id)
        # /clear 后首条消息：new_session 标志表示需要启动新 Claude 进程
        if route_info.get('new_session'):
            chat_type = message.get('chat_type', '')
            _run_in_background(_forward_new_request,
                               (binding, route_info['session_id'],
                                route_info['project_dir'], prompt,
                                chat_id, message_id, chat_type))
        else:
            _run_in_background(_forward_continue_request,
                               (binding, route_info['session_id'],
                                route_info['project_dir'], prompt,
                                chat_id, message_id))
        return

    # 未路由到已有 session：走默认聊天目录 / 使用提示
    default_chat_dir = binding.get('default_chat_dir', '')
    # 配置了默认聊天目录时，自动创建/继续会话
    if default_chat_dir:
        _handle_default_chat_message(data, prompt, binding)
        return

    # 已注册但未配置默认目录：发送使用提示
    owner_id = binding.get('_owner_id', '')
    supported = _get_supported_commands(owner_id)
    hint = "💡 我还不能直接对话哦，请通过以下方式使用：\n\n" \
           "**发起新会话：**\n" \
           "发送 `/new` 指令创建 Claude 会话\n\n" \
           "**继续会话：**\n" \
           "回复 Claude 的消息即可继续对话\n\n" \
           "**支持的指令：**\n" + supported
    _run_in_background(_send_notice_message, (chat_id, hint, message_id))


def _get_supported_commands(owner_id: str = '') -> str:
    """获取支持的命令列表（用于帮助提示）

    Args:
        owner_id: 当前请求的用户 ID，用于过滤管理员专属指令

    Returns:
        命令列表字符串
    """
    from config import FEISHU_OWNER_ID as gateway_owner_id

    is_admin = owner_id and owner_id == gateway_owner_id
    items = []
    for cmd, (_, admin_only, info) in _COMMANDS.items():
        if admin_only and not is_admin:
            continue
        items.append(f"- `/{cmd}`: {info}")
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
    from config import FEISHU_OWNER_ID as gateway_owner_id

    # 统一获取事件信息
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    binding = _get_binding_from_event(event)
    owner_id = binding.get('_owner_id', '') if binding else ''

    handler_info = _COMMANDS.get(command)
    if handler_info:
        handler_func, admin_only, _ = handler_info
        # 管理员专属指令需要权限检查
        if admin_only and owner_id != gateway_owner_id:
            if chat_id:
                _run_in_background(_send_notice_message, (chat_id, "此指令仅限管理员使用", message_id))
            return
        handler_func(data, args)
    else:
        logger.info(f"[feishu] Unknown command: /{command}")
        # 发送未知指令提示
        if chat_id:
            supported = _get_supported_commands(owner_id)
            _run_in_background(_send_notice_message, (chat_id, f"未知指令：`/{command}`\n\n支持的指令：\n{supported}", message_id))


def _handle_default_chat_message(data: dict, prompt: str, binding: dict) -> None:
    """处理默认聊天目录下的普通消息

    当用户的 binding 中配置了 default_chat_dir 时，普通消息（非指令、非回复）会：
    - 有活跃默认会话 → 继续该会话
    - 无活跃默认会话 → 在默认目录创建新会话

    Args:
        data: 飞书事件数据
        prompt: 用户消息内容（已清理）
        binding: 用户绑定信息（包含 default_chat_dir）
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')

    default_chat_dir = binding.get('default_chat_dir', '')
    session_id = binding.get('default_chat_session_id', '')
    owner_id = binding.get('_owner_id', '')

    if session_id:
        # 继续活跃的默认会话
        logger.info(f"[default-chat] Continuing session {session_id} for {owner_id}, prompt={_sanitize_user_content(prompt)}")
        _run_in_background(_forward_continue_request, (
            binding, session_id, default_chat_dir,
            prompt, chat_id, message_id
        ))
    else:
        # 创建新的默认会话
        logger.info(f"[default-chat] Creating new session in {default_chat_dir} for {owner_id}, prompt={_sanitize_user_content(prompt)}")
        new_session_id = str(uuid.uuid4())
        _run_in_background(_forward_new_request_for_default_dir, (
            binding, new_session_id, default_chat_dir, prompt, chat_id, message_id
        ))


def _forward_new_request_for_default_dir(binding: Dict[str, Any], session_id: str,
                                         project_dir: str, prompt: str,
                                         chat_id: str, message_id: str, chat_type: str = '',
                                         claude_command: str = '') -> str:
    """转发默认聊天新建会话请求，完成后将 session_id 持久化到 BindingStore

    此函数在后台线程运行，是 _forward_new_request 的包装：
    转发请求后将返回的 session_id 写入 BindingStore。

    Returns:
        session_id
    """
    from services.binding_store import BindingStore

    session_id = _forward_new_request(binding, session_id, project_dir, prompt, chat_id, message_id, chat_type, claude_command)

    owner_id = binding.get('_owner_id', '') if binding else ''
    if session_id and owner_id:
        binding_store = BindingStore.get_instance()
        if binding_store:
            binding_store.update_field(owner_id, 'default_chat_session_id', session_id)
            logger.info(f"[default-chat] Persisted session {session_id} for {owner_id}")

    return session_id


def _forward_claude_request(binding: Dict[str, Any], endpoint: str,
                            payload: Dict[str, Any], chat_id: str,
                            reply_to: Optional[str] = None,
                            reply_in_thread: bool = False) -> str:
    """转发 Claude 会话请求到 Callback 后端

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        endpoint: API 端点（如 /cb/claude/new, /cb/claude/continue）
        payload: 请求数据
        chat_id: 群聊 ID（用于错误通知）
        reply_to: 要回复的消息 ID（可选）
        reply_in_thread: 是否收进话题详情

    Returns:
        session_id（新建时从响应获取，继续时从 payload 获取），失败时仍返回 payload 中的 session_id
    """
    import urllib.error

    owner_id = binding.get('_owner_id', '')

    # 从 endpoint 提取 action（如 /cb/claude/new -> new）
    action = endpoint.rstrip('/').split('/')[-1]
    known_actions = ('new', 'continue')
    if action not in known_actions:
        logger.warning("[feishu] Unknown endpoint action: %s, expected one of %s", action, known_actions)
        action = 'continue'  # 默认使用 continue
    is_new = (action == 'new')

    # 预设 session_id：continue 时 payload 中已有，new 时成功后从响应覆盖
    session_id = payload.get('session_id', '')

    logger.info("[feishu] Forwarding %s request, owner_id=%s", action, owner_id)

    try:
        # 使用 WS/HTTP 路由分发（保留原 HTTP 模式的 30s 超时）
        response_data = _forward_via_ws_or_http(binding, endpoint, payload, timeout=30)

        if response_data is None:
            raise urllib.error.URLError("No available route (WS or HTTP)")

        logger.info(f"[feishu] {action.capitalize()} request response: {response_data}")

        session_id = response_data.get('session_id', '') or session_id

        # 先保存用户消息到 MessageSessionStore（不更新 last_message_id）
        if reply_to:
            project_dir = payload.get('project_dir', '')
            if session_id and project_dir:
                from services.message_session_store import MessageSessionStore
                msg_store = MessageSessionStore.get_instance()
                if msg_store:
                    msg_store.save(reply_to, session_id, project_dir)
                    logger.info(f"[feishu] Saved user message mapping: {reply_to} -> {session_id}")

        # 再发送系统通知（内部会更新 last_message_id）
        # add_typing 复用 skip_user_prompt：skip=False 说明后续消息在新群聊，
        # 会导致当前聊天的 Typing 可能无法被移除，所以不加
        _send_session_result_notification(chat_id, response_data, payload.get('project_dir', ''),
                                          is_new=is_new,
                                          claude_command=payload.get('claude_command', ''),
                                          reply_to=reply_to,
                                          reply_in_thread=reply_in_thread,
                                          binding=binding,
                                          add_typing=payload.get('skip_user_prompt', True))

    except urllib.error.HTTPError as e:
        error_detail = _extract_http_error_detail(e)
        action_text = "新建会话失败" if is_new else "继续会话失败"
        error_msg = f"{action_text}: {error_detail}" if error_detail else f"Callback 服务返回错误: HTTP {e.code}"
        logger.error(f"[feishu] {action.capitalize()} request HTTP error: {e.code} {e.reason}")
        # 注意：新建会话时 payload 中没有 session_id，对应的错误通知不会关联会话
        # 这符合预期，因为会话根本没创建成功
        _send_error_notification(chat_id, error_msg, reply_to=reply_to,
                                 session_id=session_id,
                                 project_dir=payload.get('project_dir', ''),
                                 reply_in_thread=reply_in_thread)

    except urllib.error.URLError as e:
        logger.error(f"[feishu] {action.capitalize()} request URL error: {e.reason}")
        _send_error_notification(chat_id, f"Callback 服务不可达: {e.reason}", reply_to=reply_to,
                                 session_id=session_id,
                                 project_dir=payload.get('project_dir', ''),
                                 reply_in_thread=reply_in_thread)

    return session_id


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
    except Exception:
        return ''


def _forward_continue_request(binding: dict, session_id: str, project_dir: str,
                              prompt: str, chat_id: str, message_id: str,
                              claude_command: str = '') -> str:
    """转发继续会话请求到 Callback 后端

    Args:
        binding: 绑定信息（包含 auth_token, callback_url 等）
        session_id: 会话 ID
        project_dir: 项目目录
        prompt: 用户回复内容
        chat_id: 群聊 ID
        message_id: 用户消息 ID（用于回复）
        claude_command: 指定使用的 Claude 命令（可选）

    Returns:
        session_id
    """
    if not binding:
        logger.warning("[feishu] No binding found, cannot continue session")
        _send_error_notification(chat_id, "您尚未注册，无法使用此功能", reply_to=message_id,
                                 session_id=session_id, project_dir=project_dir)
        return session_id

    reply_in_thread = _should_reply_in_thread(binding, project_dir)

    data = {
        'session_id': session_id,
        'project_dir': project_dir,
        'prompt': prompt,
        'chat_id': chat_id,
        'reply_message_id': message_id
    }
    if claude_command:
        data['claude_command'] = claude_command

    return _forward_claude_request(binding, '/cb/claude/continue',
                                   data, chat_id, reply_to=message_id,
                                   reply_in_thread=reply_in_thread)


def _send_session_result_notification(chat_id: str, response: dict, project_dir: str,
                                      is_new: bool = False, claude_command: str = '',
                                      reply_to: Optional[str] = None,
                                      reply_in_thread: bool = False,
                                      binding: Optional[Dict[str, Any]] = None,
                                      add_typing: bool = True):
    """根据会话结果发送飞书通知

    Args:
        chat_id: 群聊 ID
        response: Callback 返回的结果
        project_dir: 项目目录
        is_new: 是否为新建会话（True: 新建会话，False: 继续会话）
        claude_command: 使用的 Claude 命令（可选，如 'opus', 'sonnet'）
        reply_to: 要回复的消息 ID（可选，用于链式回复）
        reply_in_thread: 是否收进话题详情
        binding: 绑定信息字典（包含 callback_url、auth_token、_owner_id，用于跨网络调用）
        add_typing: 是否给通知消息添加 Typing 表情（后续消息不在当前聊天时应为 False）
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
            if success and sent_message_id and add_typing:
                service.add_reaction(sent_message_id, 'Typing')
        else:
            # 继续会话 - 用表情回应代替文本通知，更轻量避免刷屏
            # 继续会话始终在同一聊天中，不存在消息跨聊天的问题，无需 add_typing 控制
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
    # add_typing=False 时说明后续消息在新群聊，不应将当前聊天的消息设为 last_message_id
    if success and sent_message_id and session_id and project_dir:
        msg_store = MessageSessionStore.get_instance()
        if msg_store:
            msg_store.save(sent_message_id, session_id, project_dir)
            logger.info(f"[feishu] Saved notification mapping: {sent_message_id} -> {session_id}")

        if add_typing and binding and binding.get('callback_url') and binding.get('auth_token'):
            _set_last_message_id_to_callback(binding, session_id, sent_message_id)


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


def _send_notice_message(chat_id: str, text: str, reply_to: Optional[str] = None):
    """发送通知消息（后台线程调用）

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


def _get_gateway_ws_url() -> str:
    """获取网关的 WebSocket 地址，用于注册提示

    从 CALLBACK_SERVER_URL（HTTP）转换为 ws(s):// 格式。

    Returns:
        ws(s):// 格式的网关地址，无法获取时返回空字符串
    """
    from config import CALLBACK_SERVER_URL
    if not CALLBACK_SERVER_URL:
        return ''
    if CALLBACK_SERVER_URL.startswith('https://'):
        return 'wss://' + CALLBACK_SERVER_URL[8:]
    elif CALLBACK_SERVER_URL.startswith('http://'):
        return 'ws://' + CALLBACK_SERVER_URL[7:]
    return ''


def _get_binding_from_event(feishu_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从飞书事件中获取绑定信息

    通过 sender_id 或 operator_id 查询 BindingStore 获取完整绑定信息。
    BindingStore.get() 会自动注入 _owner_id 字段。

    两种场景：
    1. 用户发送消息触发：feishu_event 包含 sender.sender_id
    2. 用户点击按钮触发：feishu_event 包含 operator（operator 本身就是 id 对象）

    Args:
        feishu_event: 飞书事件数据（包含 sender 或 operator 信息）

    Returns:
        绑定信息字典（包含 auth_token, callback_url, _owner_id 等），未找到返回 None。
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

    # 从按钮的 value 中提取 chat_id、message_id 和 chat_type（用户原始消息 ID）
    button_value = action.get('value', {})
    chat_id = button_value.get('chat_id', '')
    message_id = button_value.get('message_id', '')
    chat_type = button_value.get('chat_type', '')

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
        return _handle_browse_directory(trigger_name, recent_dir, custom_dir, chat_id, message_id, chat_type, event, form_values)

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
    new_session_id = str(uuid.uuid4())
    _run_in_background(_forward_new_request, (binding, new_session_id, selected_dir, prompt, chat_id, message_id, chat_type, claude_command))

    return True, response


def _handle_browse_directory(trigger_name: str, recent_dir: str, custom_dir: str,
                             chat_id: str, message_id: str, chat_type: str,
                             feishu_event: dict, form_values: dict) -> Tuple[bool, dict]:
    """处理浏览目录按钮点击

    调用 browse-dirs 接口获取子目录列表，返回更新后的卡片。

    Args:
        trigger_name: 触发的按钮名称 (browse_dir_select_btn, browse_custom_btn 或 browse_result_btn)
        recent_dir: 常用目录下拉框选择的值
        custom_dir: 用户输入的自定义路径
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        chat_type: 聊天类型（group/p2p），透传到重建的卡片
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
    browse_data = _fetch_browse_dirs_from_callback(binding, browse_path)
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
        chat_type=chat_type,
        feishu_event=feishu_event
    )

    return True, {'card': {'type': 'raw', 'data': card}}


def _build_new_session_card(
    owner_id: str,
    chat_id: str,
    message_id: str,
    chat_type: str = '',
    recent_dirs: List[str] = None,
    selected_recent_dir: str = '',
    custom_dir: str = '',
    browse_data: Optional[Dict[str, Any]] = None,
    prompt: str = '',
    claude_commands: Optional[List[str]] = None,
    claude_command: str = ''
) -> Dict[str, Any]:
    """构建新建会话卡片（统一构建逻辑）

    Args:
        owner_id: 用户 ID
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        chat_type: 聊天类型（group/p2p），卡片提交时透传
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
    for dir_path in (recent_dirs or []):
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
        'message_id': message_id,
        'chat_type': chat_type
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
                              chat_id: str, message_id: str, chat_type: str,
                              feishu_event: dict) -> dict:
    """构建包含浏览结果的目录选择卡片

    Args:
        browse_data: browse-dirs 接口返回的数据 {dirs, parent, current}
        form_values: 原始表单数据（用于回填）
        custom_dir_value: 应该回填到 custom_dir 输入框的值
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        chat_type: 聊天类型（group/p2p），透传到卡片
        feishu_event: 飞书事件数据

    Returns:
        飞书卡片字典
    """
    # 获取绑定信息
    binding = _get_binding_from_event(feishu_event)
    owner_id = binding.get('_owner_id', '') if binding else ''

    # 获取常用目录列表
    recent_dirs = _fetch_recent_dirs_from_callback(binding, limit=20) if binding and binding.get('auth_token') else []

    card = _build_new_session_card(
        owner_id=owner_id, chat_id=chat_id, message_id=message_id,
        chat_type=chat_type,
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

    # 提取卡片消息 ID（用于添加表情）
    context = event.get('context', {})
    card_message_id = context.get('open_message_id', '')

    # AskUserQuestion 表单提交（action=answer）
    if action_type == 'answer':
        return _handle_ask_question_answer(request_id, form_value, data,
                                           card_message_id=card_message_id)

    # 调用 callback_url 的决策接口（callback_url 从 BindingStore 获取）
    return _forward_permission_request(request_id, data, action_type,
                                       card_message_id=card_message_id)


def _add_typing_reaction(card_message_id: str):
    """后台任务：给卡片消息添加 Typing 表情，表示 Claude 正在处理

    Args:
        card_message_id: 卡片消息 ID
    """
    if not card_message_id:
        return
    from services.feishu_api import FeishuAPIService
    service = FeishuAPIService.get_instance()
    if service and service.enabled:
        service.add_reaction(card_message_id, 'Typing')


def _apply_custom_overrides(form_value: Dict[str, Any]) -> Tuple[Dict[str, Any], List[int]]:
    """让单选题的 q_{i}_custom 覆盖 q_{i}_select，返回清理后的 form_value 副本
    和被覆盖的题号列表。

    form_value 字段命名（q_{i}_select / q_{i}_custom）由 src/lib/feishu.sh
    渲染卡片时写死。本函数集中处理该约定：

    - **判定**：单选题同时有非空 q_{i}_select 和 q_{i}_custom 时视为"覆盖"。
      多选题（select 值是 list）不存在覆盖语义；未选中（空串）也不算。
    - **清理**：从 form_value 副本中剥离被覆盖题目的 q_{i}_select 字段，
      使卡片更新时只回显用户最终的 custom 内容。
    - **题号**：0-based 升序列表，供 toast 文案使用（展示时由调用方 +1
      转成"第 X 题"）。

    不修改入参；返回的是新的 dict。

    Returns:
        (cleaned_form_value, overridden_indices)
    """
    cleaned = dict(form_value)  # 浅拷贝
    prefix, suffix = 'q_', '_select'
    overridden = []
    for key, value in cleaned.items():
        # 只看 q_{i}_select 字段；q_{i}_custom 及其它键由各自消费方处理
        if not (key.startswith(prefix) and key.endswith(suffix)):
            continue
        # value 是 list -> 多选题（不存在"custom 覆盖 select"的语义）
        # value 为空串 -> 单选未选中，自然谈不上覆盖
        if isinstance(value, list) or not value:
            continue
        # 防御：过滤形如 q_foo_select 的异常键，保证 idx 是纯数字
        idx_str = key[len(prefix):-len(suffix)]
        if not idx_str.isdigit():
            continue
        idx = int(idx_str)
        # 同题 custom 非空，才算"自定义输入覆盖了下拉"
        if cleaned.get(f'q_{idx}_custom', ''):
            overridden.append(idx)
    overridden.sort()

    # 延迟到识别完成后再剥离 select，避免迭代 dict 时修改导致 RuntimeError
    for idx in overridden:
        cleaned.pop(f'q_{idx}_select', None)

    return cleaned, overridden


def _handle_ask_question_answer(request_id: str, form_value: dict, original_data: dict,
                                card_message_id: str = '') -> Tuple[bool, dict]:
    """处理 AskUserQuestion 表单提交

    从 form_value 中提取用户的选择/输入，构造 answers dict，
    然后调用 callback 服务的决策接口。

    Args:
        request_id: 请求 ID
        form_value: 表单提交数据，包含 q_0_select, q_0_custom 等字段
        original_data: 原始飞书事件数据
        card_message_id: 卡片消息 ID（用于添加表情）

    Returns:
        (handled, toast_response)
    """
    logger.info("[feishu] Handling AskUserQuestion answer: request_id=%s", request_id)
    logger.debug("[feishu] form_value: %s", json.dumps(form_value, ensure_ascii=False, indent=2))

    # 获取绑定信息
    event = original_data.get('event', {})
    binding = _get_binding_from_event(event)
    if not binding:
        logger.warning("[feishu] No binding found for AskUserQuestion request")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '身份验证失败，请重新注册网关'
            }
        }

    # 单选题若 custom 非空则覆盖下拉：清除对应 q_{i}_select 字段并收集题号，
    # 用于卡片更新展示和 toast 文案（多选题不存在覆盖语义）。
    form_value, overridden_questions = _apply_custom_overrides(form_value)

    # 构建请求数据：只透传 form_value，由 callback 端生成 answers/questions
    request_data = {
        'action': 'answer',
        'request_id': request_id,
        'form_value': form_value,
    }

    start_time = time.time()

    try:
        # 使用 WS/HTTP 路由分发
        response_data = _forward_via_ws_or_http(binding, '/cb/decision', request_data, timeout=2)

        if response_data is None:
            logger.warning("[feishu] AskUserQuestion forward failed: no available route")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '回调服务不可达，请检查服务状态'
                }
            }

        elapsed = (time.time() - start_time) * 1000

        success = response_data.get('success', False)
        decision = response_data.get('decision')
        message = response_data.get('message', '')

        response_body = {}
        if success and decision:
            toast_type = TOAST_SUCCESS
            toast_content = message or '已提交回答'
            # 如果有单选题被自定义内容覆盖，在提示中告知用户
            if overridden_questions:
                nums = '、'.join(f'第{idx + 1}题' for idx in overridden_questions)
                toast_content += f'（{nums}的自定义内容已覆盖选项）'
            logger.info("[feishu] AskUserQuestion succeeded: decision=%s, elapsed=%.0fms", decision, elapsed)
            # 决策成功后，异步添加 Typing 表情
            _run_in_background(_add_typing_reaction, (card_message_id,))

            # 尝试在回调响应中返回更新后的卡片
            updated_card = _get_updated_card_for_response(request_id, 'answer', form_value=form_value)
            if updated_card:
                response_body['card'] = {
                    'type': 'raw',
                    'data': updated_card
                }
                logger.debug("[feishu] Returning updated card in response for AskUserQuestion: %s", request_id)
        else:
            toast_type = TOAST_ERROR
            toast_content = message or '提交失败'
            logger.warning("[feishu] AskUserQuestion failed: message=%s, elapsed=%.0fms", toast_content, elapsed)

        response_body['toast'] = {
            'type': toast_type,
            'content': toast_content
        }
        return True, response_body

    except Exception as e:
        logger.error("[feishu] AskUserQuestion error: %s", e)
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': f'提交失败: {str(e)}'
            }
        }


def _forward_permission_request(request_id: str, original_data: dict, action_type: str,
                                card_message_id: str = '') -> Tuple[bool, dict]:
    """转发权限请求到 Callback 服务

    调用 callback 服务的纯决策接口，根据返回的决策结果生成 toast。
    优先使用 WS 隧道，fallback 到 HTTP。
    callback_url 从 BindingStore 获取。

    注意：飞书要求在 3 秒内返回响应，timeout 设置为 2 秒预留时间。

    Args:
        request_id: 请求 ID
        original_data: 原始飞书事件数据（用于提取绑定信息和 project_dir）
        action_type: 动作类型 (allow/always/deny/interrupt)
        card_message_id: 卡片消息 ID（用于添加表情）

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

    owner_id = binding.get('_owner_id', '')

    # 构建请求数据
    request_data = {
        'action': action_type,
        'request_id': request_id
    }

    # 添加可选字段
    if 'project_dir' in value:
        request_data['project_dir'] = value['project_dir']

    logger.info("[feishu] Forwarding permission request: owner_id=%s, action=%s", owner_id, action_type)

    start_time = time.time()

    try:
        # 使用 WS/HTTP 路由分发
        # 飞书要求 3 秒内返回，设置 2 秒超时预留处理时间
        response_data = _forward_via_ws_or_http(binding, '/cb/decision', request_data, timeout=2)

        if response_data is None:
            logger.warning("[feishu] Forward failed: no available route")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '回调服务不可达，请检查服务状态'
                }
            }

        elapsed = (time.time() - start_time) * 1000

        success = response_data.get('success', False)
        decision = response_data.get('decision')
        message = response_data.get('message', '')

        # 根据决策结果生成 toast
        response_body = {}
        if success and decision:
            if decision == 'allow':
                toast_type = TOAST_SUCCESS
            else:  # deny
                toast_type = TOAST_WARNING
            toast_content = message or ('已批准运行' if decision == 'allow' else '已拒绝运行')
            logger.info(f"[feishu] Decision succeeded: decision={decision}, message={message}, elapsed={elapsed:.0f}ms")
            # 决策成功后，异步添加 Typing 表情（拒绝并中断时不需要，因为预期任务会停止）
            if action_type != 'interrupt':
                _run_in_background(_add_typing_reaction, (card_message_id,))

            # 尝试在回调响应中返回更新后的卡片（移除按钮，更新状态）
            updated_card = _get_updated_card_for_response(request_id, action_type)
            if updated_card:
                response_body['card'] = {
                    'type': 'raw',
                    'data': updated_card
                }
                logger.debug(f"[feishu] Returning updated card in response for request: {request_id}")
        else:
            toast_type = TOAST_ERROR
            toast_content = message or '处理失败'
            logger.warning(f"[feishu] Decision failed: message={toast_content}, elapsed={elapsed:.0f}ms")

        response_body['toast'] = {
            'type': toast_type,
            'content': toast_content
        }
        return True, response_body

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
            - mode: "ws" 表示 WebSocket 模式，否则为 HTTP 模式
            - callback_url: Callback 后端 URL（HTTP 模式）
            - owner_id: 飞书用户 ID
            - request_ip: 注册来源 IP（仅 approve_register 需要）
            - request_id: 注册请求 ID（WS 模式需要）
            - at_bot_only: 群聊 @bot 过滤（仅 approve_register 需要）
            - session_mode: 会话模式 message/thread/group（仅 approve_register 需要）
            - claude_commands: 可用的 Claude 命令列表（仅 approve_register 需要）
            - default_chat_dir: 默认聊天目录（仅 approve_register 需要）
            - default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式（仅 approve_register 需要）
            - group_name_prefix: 群聊名称前缀（仅 approve_register 需要）
            - group_dissolve_days: 群聊自动解散天数（仅 approve_register 需要）

    Returns:
        (handled, response) - response 包含 toast 和可选的 card 更新
    """
    from handlers.register import handle_authorization_decision, handle_register_unbind
    from handlers.register import handle_ws_authorization_approved, handle_ws_authorization_denied, handle_ws_register_unbind

    action = value.get('action', '')
    mode = value.get('mode', 'http')  # 默认 HTTP 模式
    callback_url = value.get('callback_url', '')
    owner_id = value.get('owner_id', '')
    request_ip = value.get('request_ip', '')
    request_id = value.get('request_id', '')
    at_bot_only = value.get('at_bot_only')
    session_mode = value.get('session_mode', 'message')
    claude_commands = value.get('claude_commands', None)
    default_chat_dir = value.get('default_chat_dir', '')
    default_chat_follow_thread = value.get('default_chat_follow_thread', True)
    group_name_prefix = value.get('group_name_prefix')
    group_dissolve_days = value.get('group_dissolve_days')

    # WebSocket 模式
    if mode == 'ws':
        if action == 'approve_register':
            logger.info("[feishu] WS registration approved: owner_id=%s", owner_id)
            return True, handle_ws_authorization_approved(
                owner_id, request_id, request_ip,
                at_bot_only=at_bot_only,
                session_mode=session_mode,
                claude_commands=claude_commands,
                default_chat_dir=default_chat_dir,
                default_chat_follow_thread=default_chat_follow_thread,
                group_name_prefix=group_name_prefix,
                group_dissolve_days=group_dissolve_days
            )
        elif action == 'deny_register':
            logger.info("[feishu] WS registration denied: owner_id=%s, request_id=%s", owner_id, request_id)
            return True, handle_ws_authorization_denied(owner_id, request_id)
        elif action == 'unbind_register':
            logger.info("[feishu] WS registration unbound: owner_id=%s", owner_id)
            return True, handle_ws_register_unbind(owner_id)
        else:
            logger.warning("[feishu] Unknown WS register action: %s", action)
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '未知的操作'
                }
            }

    # HTTP 模式
    if action == 'approve_register':
        logger.info("[feishu] Registration approved: owner_id=%s, callback_url=%s, session_mode=%s", owner_id, callback_url, session_mode)
        return True, handle_authorization_decision(
            callback_url, owner_id, request_ip, approved=True,
            at_bot_only=at_bot_only,
            session_mode=session_mode,
            claude_commands=claude_commands,
            default_chat_dir=default_chat_dir,
            default_chat_follow_thread=default_chat_follow_thread,
            group_name_prefix=group_name_prefix,
            group_dissolve_days=group_dissolve_days
        )
    elif action == 'deny_register':
        logger.info("[feishu] Registration denied: owner_id=%s", owner_id)
        return True, handle_authorization_decision(
            callback_url, owner_id, request_ip, approved=False
        )
    elif action == 'unbind_register':
        logger.info("[feishu] Registration unbound: owner_id=%s, callback_url=%s", owner_id, callback_url)
        return True, handle_register_unbind(callback_url, owner_id)
    else:
        logger.warning("[feishu] Unknown register action: %s", action)
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


def _fetch_recent_dirs_from_callback(binding: Dict[str, Any], limit: int = 5) -> list:
    """从 Callback 后端获取近期常用目录列表

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        limit: 最多返回的目录数量

    Returns:
        目录路径列表
    """
    request_data = {
        'limit': limit
    }

    try:
        response_data = _forward_via_ws_or_http(binding, '/cb/claude/recent-dirs', request_data)

        if response_data is None:
            return []

        recent_dirs = response_data.get('dirs', [])
        logger.info(f"[feishu] Fetched {len(recent_dirs)} recent dirs from callback")
        return recent_dirs

    except Exception as e:
        logger.error(f"[feishu] Fetch recent dirs error: {e}")
        return []


def _fetch_browse_dirs_from_callback(binding: Dict[str, Any], path: str) -> dict:
    """从 Callback 后端获取指定路径下的子目录列表

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        path: 要浏览的路径

    Returns:
        包含 dirs, parent, current 的字典，失败时返回空字典
    """
    request_data = {
        'path': path
    }

    try:
        response_data = _forward_via_ws_or_http(binding, '/cb/claude/browse-dirs', request_data)

        if response_data is None:
            return {}

        logger.info(f"[feishu] Fetched browse result: {len(response_data.get('dirs', []))} dirs from {path}")
        return response_data

    except Exception as e:
        logger.error(f"[feishu] Browse dirs error: {e}")
        return {}


def _set_last_message_id_to_callback(binding: Dict[str, Any],
                                     session_id: str, message_id: str) -> bool:
    """通过 Callback 后端设置 session 的 last_message_id

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        session_id: Claude 会话 ID
        message_id: 飞书消息 ID

    Returns:
        是否设置成功
    """
    request_data = {
        'session_id': session_id,
        'message_id': message_id
    }

    try:
        response_data = _forward_via_ws_or_http(binding, '/cb/session/set-last-message-id', request_data)

        if response_data is None:
            return False

        success = response_data.get('success', False)
        if success:
            logger.info(f"[feishu] Set last_message_id via callback: session={session_id}, message_id={message_id}")
        else:
            logger.warning(f"[feishu] Failed to set last_message_id: {response_data.get('error', 'unknown')}")
        return success

    except Exception as e:
        logger.error(f"[feishu] Set last_message_id error: {e}")
        return False


# 会话路由失败时的通用反馈文案（/mute /unmute / 主路由等场景共用）
_SESSION_NOT_FOUND_HINT = "无法找到对应的会话（可能已过期、被清理或服务暂时不可用）。"
_SESSION_UNRESOLVED_HINT = "无法确定目标会话，请在群聊中或回复某条会话消息时使用。"


def _send_new_session_card(binding: dict, owner_id: str, chat_id: str,
                           message_id: str, chat_type: str,
                           project_dir: str, prompt: str,
                           claude_command: str = ''):
    """发送工作目录选择卡片

    Args:
        binding: 绑定信息（包含 auth_token, callback_url, claude_commands 等）
        owner_id: 用户 ID
        chat_id: 群聊 ID
        message_id: 原始消息 ID（用于回复）
        chat_type: 聊天类型（group/p2p），卡片提交时透传
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
        _run_in_background(_send_notice_message, (chat_id, "您尚未注册，无法使用此功能", message_id))
        return

    reply_in_thread = _should_reply_in_thread(binding, project_dir)

    # 从 Callback 后端获取常用目录列表
    recent_dirs = _fetch_recent_dirs_from_callback(binding, limit=20)

    card = _build_new_session_card(
        owner_id=owner_id, chat_id=chat_id, message_id=message_id,
        chat_type=chat_type,
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
    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')

    # 解析指令参数（支持 --dir= 和 --cmd=）
    success, project_dir, cmd_arg, prompt = _parse_command_args(args)
    if not success:
        _run_in_background(_send_notice_message, (chat_id, "参数格式错误，正确格式：`/new --dir=/path/to/project [--cmd=0] prompt`", message_id))
        return

    binding = _get_binding_from_event(event)
    if not binding:
        _run_in_background(_send_notice_message, (chat_id, "您尚未注册，无法使用此功能", message_id))
        return
    owner_id = binding.get('_owner_id', '')
    msg_chat_type = message.get('chat_type', '')

    # 从上下文继承 project_dir 和 claude_command（用户未显式指定时）
    # 优先级：--dir/--cmd 参数 > 消息上下文解析到的旧 session > 默认值
    inherited_dir = ''
    inherited_cmd = ''
    need_inherit = not project_dir or not cmd_arg

    if need_inherit:
        # 两条分支统一走 resolve_from_message：parent_id / group chat 的路由定位
        route_info = SessionFacade.resolve_from_message(data, binding)
        route_source = route_info.get('source', '')
        if SessionFacade.RouteSource.is_resolved(route_source):
            inherited_dir = route_info.get('project_dir', '')
            # claude_command 本地路由 store 不存，回源 callback 拿权威值
            # （/new 低频场景，1 次 RPC 可接受）
            if not cmd_arg:
                inherited_info = SessionFacade.fetch_session_info(
                    binding, route_info.get('session_id', ''))
                inherited_cmd = inherited_info.get('claude_command', '')

    # --cmd 参数优先，继承次之，binding 默认命令兜底
    if not cmd_arg and inherited_cmd:
        claude_command = inherited_cmd
        logger.info(f"[feishu] /new inherited claude_command: {claude_command}")
    else:
        # 有 --cmd 时解析用户指定的命令，否则使用 binding 默认命令
        ok, result = _resolve_claude_command_from_binding(binding, cmd_arg)
        if not ok:
            _run_in_background(_send_notice_message, (chat_id, result, message_id))
            return
        claude_command = result

    # --dir 参数优先，继承次之
    if not project_dir and inherited_dir:
        project_dir = inherited_dir
        logger.info(f"[feishu] /new inherited project_dir: {project_dir}")

    # 没有 --dir 但有 prompt：尝试使用用户的默认聊天目录
    default_chat_dir = binding.get('default_chat_dir', '')
    if not project_dir and prompt and default_chat_dir:
        project_dir = default_chat_dir
        logger.info(f"[default-chat] /new using default dir: {default_chat_dir}")

    # 验证参数：如果没有目录或没有提示词，发送卡片让用户完善
    if not project_dir or not prompt:
        _run_in_background(_send_new_session_card, (binding, owner_id, chat_id, message_id, msg_chat_type, project_dir, prompt, claude_command))
        return

    logger.info(f"[feishu] /new command: dir={project_dir}, cmd={claude_command or '(default)'}, prompt={_sanitize_user_content(prompt)}")

    # 在后台线程中转发到 Callback 后端
    # 如果使用的是默认聊天目录，同时更新活跃默认会话
    new_session_id = str(uuid.uuid4())
    if default_chat_dir and os.path.realpath(project_dir) == os.path.realpath(default_chat_dir):
        _run_in_background(_forward_new_request_for_default_dir, (binding, new_session_id, project_dir, prompt, chat_id, message_id, msg_chat_type, claude_command))
    else:
        _run_in_background(_forward_new_request, (binding, new_session_id, project_dir, prompt, chat_id, message_id, msg_chat_type, claude_command))


def _forward_new_request(binding: dict, session_id: str, project_dir: str, prompt: str,
                         chat_id: str, message_id: str, chat_type: str = '',
                         claude_command: str = '') -> str:
    """转发新建会话请求到 Callback 后端

    Args:
        binding: 绑定信息（包含 auth_token, callback_url 等）
        session_id: 会话 ID（由调用方生成）
        project_dir: 项目工作目录
        prompt: 用户输入的 prompt
        chat_id: 聊天 ID（P2P 或群聊）
        message_id: 原始消息 ID（用作 reply_to）
        chat_type: 聊天类型（group/p2p），用于 group 模式下的 chat_id 决策
        claude_command: 指定使用的 Claude 命令（可选）

    Returns:
        session_id，失败时返回空字符串
    """
    if not binding:
        logger.warning("[feishu] No binding found, cannot create session")
        # 注意：此处不关联会话，因为会话尚未创建（用户未注册）
        # 用户回复此错误通知没有意义，应先完成注册
        _send_error_notification(chat_id, "您尚未注册，无法使用此功能", reply_to=message_id)
        return ''

    reply_in_thread = _should_reply_in_thread(binding, project_dir)
    session_mode = binding.get('session_mode', '')

    # 确定目标 chat_id 和 skip_user_prompt
    target_chat_id = chat_id
    skip_user_prompt = True  # 默认跳过（飞书发起的 prompt 已在飞书展示）

    if session_mode == 'group':
        from services.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        owner_id = binding.get('_owner_id', '')
        if not gs_store or not owner_id:
            _run_in_background(_send_notice_message, (chat_id, "存储服务未就绪，请稍后重试", message_id))
            return ''
        if chat_type != 'group':
            # P2P /new（group 模式）：不预建群，让 callback ensure-chat 统一处理
            target_chat_id = ''
            skip_user_prompt = False  # prompt 未在新群展示，由 hook 发送
        elif target_chat_id:
            # 群内 /new：gateway 直接登记当前群的路由表（不调建群 API，群已存在）
            gs_store.save(owner_id, target_chat_id, session_id, project_dir=project_dir)

    data = {
        'project_dir': project_dir,
        'prompt': prompt,
        'chat_id': target_chat_id,
        'message_id': message_id,
        'session_id': session_id,
        'skip_user_prompt': skip_user_prompt
    }
    if claude_command:
        data['claude_command'] = claude_command

    # 新建会话时传递 reply_to，让第一条通知回复用户的 /new 消息
    # 后续通知会通过 last_message_id 链式回复
    return _forward_claude_request(binding, '/cb/claude/new',
                                   data, chat_id, reply_to=message_id,
                                   reply_in_thread=reply_in_thread)


def create_group_chat_and_record(owner_id: str, session_id: str, project_dir: str,
                                 group_name_prefix: str) -> Tuple[bool, str]:
    """创建飞书群聊并完成网关侧所有数据登记（群聊创建唯一入口）

    所有创建路径都应走此函数，原子完成：
        - 幂等检查：GroupSessionStore 已有该 session_id 的群则直接返回
        - 调飞书 API 建群（name 由 group_name_prefix + project_dir + 时间戳构造）
        - GroupChatStore.allocate（owner + seq）—— seq 落 store 即可，不回传调用方
        - 若提供 session_id/project_dir，同步写 GroupSessionStore（chat → session 路由）

    调用方:
    - handle_create_group() (HTTP): 分离部署 callback 经 HTTP 反向转发
    - handlers.utils.create_feishu_group(): 单机模式 callback 直接调用

    Args:
        owner_id: 归属 owner（飞书用户 ID），用于后续解散权限校验
        session_id: 关联的 session ID（可选）；提供则写 GroupSessionStore
        project_dir: session 工作目录（写 GroupSessionStore 时一并存）
        group_name_prefix: 群名前缀；空串视为无前缀（不再兜底默认值，由调用方决定）

    Returns:
        (success, chat_id_or_error)
    """
    from services.feishu_api import FeishuAPIService
    from services.group_chat_store import GroupChatStore
    from services.group_session_store import GroupSessionStore

    # owner_id 是后续 allocate seq、写 GroupSessionStore、解散归属校验的必需上下文，
    # 缺失则群只能在飞书端裸存、gateway 完全无记录，必须显式拒绝
    if not owner_id:
        return False, 'Missing owner_id'

    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()
    if not group_store or not gs_store:
        return False, 'Store not initialized'

    # 幂等：如果当前 owner 下 GroupSessionStore 已有该 session_id 的群，直接返回
    if session_id:
        existing_chat_id = gs_store.find_by_session(owner_id, session_id)
        if existing_chat_id:
            logger.info("[create-group] Idempotent return: owner=%s session=%s -> chat=%s",
                        owner_id, session_id, existing_chat_id)
            return True, existing_chat_id

    # 由 prefix + project_dir + 时间戳构造群名
    dir_name = os.path.basename(project_dir) if project_dir else ''
    if len(dir_name) > 30:
        dir_name = dir_name[:29] + '\u2026'
    timestamp = time.strftime('%m%d %H:%M:%S')
    if group_name_prefix and dir_name:
        name = f"{group_name_prefix} - {dir_name} - {timestamp}"
    elif group_name_prefix:
        name = f"{group_name_prefix} - {timestamp}"
    elif dir_name:
        name = f"{dir_name} - {timestamp}"
    else:
        name = timestamp

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        return False, 'Feishu API service not available'

    ok, result = service.create_group_chat(name, owner_id)
    if not ok:
        return False, result

    chat_id = result
    seq = group_store.allocate(owner_id, chat_id)
    if not seq:
        logger.warning("[feishu] group_store.allocate failed for owner=%s chat=%s, "
                       "group created on Feishu but not tracked locally", owner_id, chat_id)

    # 同步写 chat → session 路由（如果调用方提供了 session_id）
    if session_id:
        gs_store.save(owner_id, chat_id, session_id, project_dir=project_dir)

    return True, chat_id


def handle_create_group(binding: Dict[str, Any], data: dict) -> Tuple[bool, dict]:
    """处理 /gw/feishu/create-group 请求，创建飞书群聊

    群名由 gateway 根据 binding 中的 group_name_prefix 与请求中的 project_dir
    统一构造，请求方无需也无法自定义。

    Args:
        binding: 绑定信息（由调用方鉴权后传入，包含 owner_id）
        data: 请求 JSON 数据（session_id、project_dir）

    Returns:
        (handled, response)
    """
    owner_id = binding.get('_owner_id', '')
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    group_name_prefix = binding.get('group_name_prefix', '')

    ok, result = create_group_chat_and_record(
        owner_id, session_id, project_dir, group_name_prefix)
    if ok:
        return True, {'success': True, 'chat_id': result}
    else:
        return True, {'success': False, 'error': result}


def batch_dissolve_groups(binding: Dict[str, Any],
                          chat_ids: List[str]) -> Dict[str, Any]:
    """批量解散群聊并清理归属记录（网关侧核心函数）

    只解散 GroupChatStore 中归属于 binding owner 的群聊。
    非服务创建的群聊（不在 store 中或归属其他 owner）直接跳过，不视为失败。

    执行顺序：先通知 callback 标记 dissolved，再调飞书 API 解散。
    这样即使飞书 API 失败，session 被标记 dissolved 但群仍存活——
    用户下次在群内交互时 continue/ensure-chat 会自动复活 session（自愈）。
    反之若先解散再通知，通知失败会导致 session 指向已解散群且无法自愈。

    调用方:
    - _dissolve_groups(): /groups dissolve 命令
    - main.py _cleanup_group_chats(): 定时清理空闲群聊

    Args:
        binding: 绑定信息（包含 _owner_id，用于归属校验和 callback 通知）
        chat_ids: 待解散的群聊 ID 列表

    Returns:
        {
            'dissolved_items': List[str],          # 实际解散的 chat_id
            'skipped_items': List[str],            # 非服务创建或不属于该 owner 的 chat_id
            'failed': List[{'chat_id', 'error'}],  # 真正的 API 错误
        }
    """
    owner_id = binding.get('_owner_id', '')
    if not owner_id:
        logger.warning("[batch-dissolve] owner_id is empty, refusing %d chat(s)", len(chat_ids))
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'owner_id not configured'} for cid in chat_ids],
        }

    from services.feishu_api import FeishuAPIService
    from services.group_chat_store import GroupChatStore

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'Feishu API service not available'} for cid in chat_ids],
        }

    group_store = GroupChatStore.get_instance()
    if not group_store:
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'GroupChatStore not initialized'} for cid in chat_ids],
        }
    my_chats = {item['chat_id'] for item in group_store.get_chats_by_owner(owner_id)}

    # 1) 按归属过滤
    targets = []
    skipped_items = []
    for cid in chat_ids:
        if cid not in my_chats:
            skipped_items.append(cid)
        else:
            targets.append(cid)

    if not targets:
        return {'dissolved_items': [], 'skipped_items': skipped_items, 'failed': []}

    # 2) 先通知 callback 标记 dissolved；失败则中止，等下次清理重试
    try:
        resp = _forward_via_ws_or_http(binding,
                                       '/cb/session/invalidate-chats',
                                       {'chat_ids': targets})
        if not resp or not resp.get('ok'):
            err_msg = (resp or {}).get('error', 'unknown error')
            logger.error("[dissolve] invalidate-chats returned not-ok: %s", err_msg)
            return {
                'dissolved_items': [],
                'skipped_items': skipped_items,
                'failed': [{'chat_id': cid, 'error': 'pre-notify failed: %s' % err_msg} for cid in targets],
            }
    except Exception as e:
        logger.error("[dissolve] invalidate-chats pre-notify failed, aborting: %s", e)
        return {
            'dissolved_items': [],
            'skipped_items': skipped_items,
            'failed': [{'chat_id': cid, 'error': 'pre-notify failed: %s' % e} for cid in targets],
        }

    # 3) 再调飞书 API 解散 + 清 GroupChatStore
    dissolved_items = []
    failed = []
    for cid in targets:
        ok, err = service.dissolve_group_chat(cid)
        if ok:
            group_store.remove(cid)
            dissolved_items.append(cid)
        else:
            failed.append({'chat_id': cid, 'error': err})

    return {'dissolved_items': dissolved_items, 'skipped_items': skipped_items, 'failed': failed}


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
            - add_typing: 发送成功后是否添加 Typing 表情（可选，默认 false）

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
    add_typing = data.get('add_typing', False)

    if not msg_type:
        logger.warning("[feishu] /gw/feishu/send: missing msg_type")
        return True, {'success': False, 'error': 'Missing msg_type'}

    if not owner_id:
        logger.warning("[feishu] /gw/feishu/send: missing owner_id")
        return True, {'success': False, 'error': 'Missing owner_id'}

    # 静音拦截：session 处于静音状态则直接丢弃消息（Claude 依然正常处理用户消息，仅出站静默）
    # 稳态下命中 SessionFacade 内存缓存，零 RPC；重启后首次查询才回源 callback
    if session_id and SessionFacade.is_muted(binding, session_id):
        logger.debug("[feishu] /gw/feishu/send muted: session_id=%s chat_id=%s",
                     session_id, chat_id)
        return True, {'success': True, 'muted': True}

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

    reply_in_thread = _should_reply_in_thread(binding, project_dir)

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

        if isinstance(content, dict):
            card_json = json.dumps(content, ensure_ascii=False)
        else:  # content 是 str（当前调用方不会传入，防御性逻辑；若传入则不缓存避免 parse + dump）
            card_json = content

        if reply_to_message_id:
            success, sent_message_id = service.reply_card(card_json, reply_to_message_id, reply_in_thread)
        else:
            success, sent_message_id = service.send_card(card_json, receive_id, receive_id_type)

        # 仅在卡片实际发送成功后缓存，避免降级为文本消息时误缓存卡片
        # Best-effort 预筛选：通过字符串匹配快速跳过不含回调按钮的通知类卡片
        # 可能误匹配文本中恰好包含 "request_id" 的卡片，但只会多缓存，不影响正确性
        if success and isinstance(content, dict) and '"request_id"' in card_json:
            cached_request_id = _extract_request_id_from_card(content)
            if cached_request_id:
                from services.card_cache import CardCache
                cache = CardCache.get_instance()
                if cache:
                    cache.set(cached_request_id, card_json)
                    logger.debug("[feishu] Cached card for request_id=%s after send", cached_request_id)
        elif not success:
            # 卡片发送失败，降级发送文本错误提示
            error_msg = sent_message_id
            logger.warning(f"[feishu] /gw/feishu/send: send_card failed: {error_msg}, fallback to text")
            fallback_text = f"⚠️ 卡片消息发送失败: {error_msg}"
            if reply_to_message_id:
                success, sent_message_id = service.reply_text(fallback_text, reply_to_message_id, reply_in_thread)
            else:
                success, sent_message_id = service.send_text(fallback_text, receive_id, receive_id_type)

    elif msg_type == 'text':
        text = content if isinstance(content, str) else content.get('text', '')
        if not text:
            logger.warning("[feishu] /gw/feishu/send: missing text content")
            return True, {'success': False, 'error': 'Missing text content'}

        if reply_to_message_id:
            success, sent_message_id = service.reply_text(text, reply_to_message_id, reply_in_thread)
        else:
            success, sent_message_id = service.send_text(text, receive_id, receive_id_type)

    elif msg_type == 'post':
        # 富文本消息：content 应为 {"zh_cn": {"title": "...", "content": [[...]]}}
        if not content or not isinstance(content, dict):
            logger.warning("[feishu] /gw/feishu/send: missing post content")
            return True, {'success': False, 'error': 'Missing post content'}

        if reply_to_message_id:
            success, sent_message_id = service.reply_post(content, reply_to_message_id, reply_in_thread)
        else:
            success, sent_message_id = service.send_post(content, receive_id, receive_id_type)

    else:
        logger.warning(f"[feishu] /gw/feishu/send: unsupported msg_type: {msg_type}")
        return True, {'success': False, 'error': f'Unsupported msg_type: {msg_type}'}

    if not success:
        # sent_message_id 此时实际是错误信息
        logger.error(f"[feishu] /gw/feishu/send: failed, error={sent_message_id}")
        return True, {'success': False, 'error': sent_message_id}

    logger.info(f"[feishu] /gw/feishu/send: message sent to {receive_id} ({receive_id_type}), id={sent_message_id}, reply_to={reply_to_message_id}")

    # 按需添加 Typing 表情（调用方通过 add_typing=true 指定）
    if add_typing and sent_message_id:
        service.add_reaction(sent_message_id, 'Typing')

    # 保存到本地 MessageSessionStore（飞书网关维护）
    if sent_message_id and session_id and project_dir:
        from services.message_session_store import MessageSessionStore
        msg_store = MessageSessionStore.get_instance()
        if msg_store:
            msg_store.save(sent_message_id, session_id, project_dir)

    # 出站到群聊时刷新活跃时间（用户终端对话也会触发 hook 通知到群）
    if success and receive_id_type == 'chat_id' and receive_id and owner_id:
        from services.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        if gs_store:
            gs_store.touch(owner_id, receive_id)

    # 通过 Callback 后端设置 last_message_id
    if sent_message_id and session_id and project_dir and binding:
        if binding.get('callback_url') and binding.get('auth_token'):
            _set_last_message_id_to_callback(binding, session_id, sent_message_id)

    return True, {'success': True, 'message_id': sent_message_id}


# 卡片状态更新时的 header 配置
_CARD_STATUS_CONFIG = {
    'allow': {'template': 'green', 'title_suffix': ' - 已批准'},
    'always': {'template': 'green', 'title_suffix': ' - 已批准（始终允许）'},
    'deny': {'template': 'red', 'title_suffix': ' - 已拒绝'},
    'interrupt': {'template': 'red', 'title_suffix': ' - 已拒绝并中断'},
    'answer': {'template': 'green', 'title_suffix': ' - 已回答'},
}


def _extract_request_id_from_card(card_content: dict) -> Optional[str]:
    """从卡片中提取第一个 callback value.request_id

    用于在卡片发送成功后定位缓存 key。
    同一张审批/问答卡中的回调按钮通常共享同一个 request_id，
    因此取第一个命中的 request_id 即可。
    """
    def _extract_from_element(elem: dict) -> Optional[str]:
        if not isinstance(elem, dict):
            return None

        # 先检查当前元素自身是否挂了 callback behavior
        behaviors = elem.get('behaviors', [])
        if isinstance(behaviors, list):
            for behavior in behaviors:
                if isinstance(behavior, dict) and behavior.get('type') == 'callback':
                    value = behavior.get('value', {})
                    if isinstance(value, dict) and value.get('request_id'):
                        return value['request_id']

        # 递归遍历常见容器节点，查找嵌套按钮上的 callback value.request_id
        for key in ['elements', 'columns']:
            children = elem.get(key, [])
            if isinstance(children, list):
                for child in children:
                    request_id = _extract_from_element(child)
                    if request_id:
                        return request_id

        return None

    body = card_content.get('body', {})
    elements = body.get('elements', [])
    for elem in elements:
        request_id = _extract_from_element(elem)
        if request_id:
            return request_id
    return None


def _get_updated_card_for_response(request_id: str, action_type: str,
                                   form_value: Optional[dict] = None) -> Optional[dict]:
    """获取更新后的卡片 JSON（用于回调响应中返回）

    Args:
        request_id: 请求 ID（用作卡片缓存 key）
        action_type: 动作类型 (allow/always/deny/interrupt/answer)
        form_value: 表单提交的值（用于回填 AskUserQuestion 卡片的选项和输入）

    Returns:
        更新后的卡片 JSON dict，失败返回 None
    """
    from services.card_cache import CardCache

    cache = CardCache.get_instance()
    if not cache:
        return None

    card_json_str = cache.get(request_id)
    if not card_json_str:
        logger.info("[feishu] Card cache miss for request_id=%s", request_id)
        return None

    try:
        card_info = json.loads(card_json_str)
    except (json.JSONDecodeError, TypeError):
        logger.warning("[feishu] Failed to parse cached card for request_id=%s", request_id)
        return None

    updated_card = _build_updated_card(card_info, action_type, form_value=form_value)
    if updated_card:
        cache.delete(request_id)
    return updated_card


def _build_updated_card(card_content: dict, action_type: str, form_value: Optional[dict] = None) -> Optional[dict]:
    """构建更新后的卡片（禁用按钮，更新 header，回填表单值）

    Args:
        card_content: 原始卡片内容 dict
        action_type: 动作类型 (allow/always/deny/interrupt/answer)
        form_value: 表单提交的值（用于回填选项和输入框）

    Returns:
        更新后的卡片 dict，失败返回 None
    """
    try:
        card = copy.deepcopy(card_content)

        # 更新 header
        config = _CARD_STATUS_CONFIG.get(action_type, {})
        header = card.get('header', {})
        if config.get('template'):
            header['template'] = config['template']
        title = header.get('title', {})
        if title.get('content') and config.get('title_suffix'):
            title['content'] = title['content'] + config['title_suffix']

        # 禁用卡片中的所有按钮，回填表单值
        elements = card.get('body', {}).get('elements', [])
        for elem in elements:
            _apply_submitted_form_state_to_element(elem, form_value)
        return card

    except Exception as e:
        logger.error("[feishu] Failed to build updated card: %s", e)
        return None


def _apply_submitted_form_state_to_element(elem: dict, form_value: Optional[dict] = None):
    """递归更新元素：禁用按钮，将下拉选择和输入框转换为已禁用的 checker 勾选器

    Args:
        elem: 卡片元素 dict
        form_value: 表单提交的值（用于回填选项和输入框）

    支持的表单元素转换：
    - select_static / multi_select_static: 转换为 checker 列表（选中项勾选，全部禁用）
    - input: 有值时转换为已勾选的 checker（"自定义 - xxx"），无值时隐藏
    """
    # 禁用按钮
    if elem.get('tag') == 'button':
        elem['disabled'] = True

    # 回填表单值
    if form_value:
        tag = elem.get('tag', '')
        name = elem.get('name', '')

        if tag in ('select_static', 'multi_select_static') and name:
            value = form_value.get(name, '' if tag == 'select_static' else [])
            options = elem.get('options', [])
            # 确定选中的值集合
            if isinstance(value, list):
                selected_set = set(value)
            else:
                selected_set = {value} if value else set()
            # 将 select 替换为 checker 列表容器
            checkers = []
            for opt in options:
                opt_value = opt.get('value', '')
                opt_text = opt.get('text', {}).get('content', opt_value)
                checkers.append({
                    'tag': 'checker',
                    'name': f'{name}_opt_{opt_value}',
                    'checked': opt_value in selected_set,
                    'disabled': True,
                    'text': {'tag': 'plain_text', 'content': opt_text},
                    'overall_checkable': True,
                    'margin': '4px 0px 0px 0px',
                    'checked_style': {'show_strikethrough': False}
                })
            # 用 column_set 包装 checker 列表替换原 select 元素
            elem.clear()
            elem['tag'] = 'column_set'
            elem['flex_mode'] = 'none'
            elem['columns'] = [{
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'elements': checkers
            }]
        elif tag == 'input' and name:
            value = form_value.get(name, '')
            if value:
                # 有自定义输入：将 input 替换为已勾选的 checker
                elem.clear()
                elem['tag'] = 'checker'
                elem['name'] = name
                elem['checked'] = True
                elem['disabled'] = True
                elem['text'] = {'tag': 'plain_text', 'content': f'自定义 - {value}'}
                elem['overall_checkable'] = True
                elem['margin'] = '4px 0px 0px 0px'
                elem['checked_style'] = {'show_strikethrough': False}
            else:
                # 无自定义输入：隐藏输入框（清空为空 div）
                elem.clear()
                elem['tag'] = 'div'
                elem['text'] = {'tag': 'plain_text', 'content': ''}

    # 递归处理子元素
    for key in ['elements', 'columns']:
        children = elem.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _apply_submitted_form_state_to_element(child, form_value)


def _handle_reply_command(data: dict, args: str):
    """处理 /reply 指令，在回复消息时指定 Claude Command 继续会话

    仅在回复消息时可用。支持 --cmd= 参数。

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /reply）
    """
    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    parent_id = message.get('parent_id', '')
    chat_type = message.get('chat_type', '')

    # /reply 需要回复消息或在 group 模式群聊中使用
    binding = _get_binding_from_event(event)
    session_mode = binding.get('session_mode', '') if binding else ''
    if not parent_id and not (session_mode == 'group' and chat_type == 'group'):
        _run_in_background(_send_notice_message, (chat_id, "`/reply` 指令仅支持在回复消息时使用，或在群聊模式的群聊中直接使用", message_id))
        return

    # 解析参数
    success, project_dir, cmd_arg, prompt = _parse_command_args(args)
    if not success:
        _run_in_background(_send_notice_message, (chat_id, "参数格式错误，正确格式：`/reply [--cmd=0] prompt`", message_id))
        return

    if project_dir:
        _run_in_background(_send_notice_message, (chat_id, "`/reply` 不支持 `--dir` 参数，会话目录由原始 session 决定。请去掉 `--dir` 后重试", message_id))
        return

    if not prompt:
        _run_in_background(_send_notice_message, (chat_id, "请提供问题内容，格式：`/reply [--cmd=0] prompt`", message_id))
        return

    # 解析 --cmd 参数（从 binding 获取命令列表）
    claude_command = ''
    if cmd_arg:
        ok, result = _resolve_claude_command_from_binding(binding, cmd_arg)
        if not ok:
            _run_in_background(_send_notice_message, (chat_id, result, message_id))
            return
        claude_command = result

    # 路由 session（统一走 SessionFacade）
    route_info = SessionFacade.resolve_from_message(data, binding)
    route_source = route_info['source']

    if SessionFacade.RouteSource.is_parent_not_found(route_source):
        _run_in_background(_send_notice_message,
                           (chat_id, _SESSION_NOT_FOUND_HINT + "请重新发起 /new 指令。", message_id))
        return

    if not SessionFacade.RouteSource.is_resolved(route_source):
        _run_in_background(_send_notice_message,
                           (chat_id, "无法找到对应的会话，请重新发起 /new 指令", message_id))
        return

    session_id = route_info['session_id']
    session_project_dir = route_info.get('project_dir', '')

    # 刷新群聊活跃时间（供自动解散空闲判断）
    if route_source == SessionFacade.RouteSource.GROUP_CHAT:
        from services.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        owner_id = binding.get('_owner_id', '') if binding else ''
        if gs_store and owner_id:
            gs_store.touch(owner_id, chat_id)

    logger.info("[feishu] /reply command: session=%s, cmd=%s, prompt=%s",
                session_id, claude_command or '(default)', _sanitize_user_content(prompt))

    # 转发到 Callback 后端
    # /clear 后首条消息：new_session 标志表示需要启动新 Claude 进程
    if route_info.get('new_session'):
        chat_type = message.get('chat_type', '')
        _run_in_background(_forward_new_request,
                           (binding, session_id, session_project_dir, prompt,
                            chat_id, message_id, chat_type, claude_command))
    else:
        _run_in_background(_forward_continue_request, (binding, session_id, session_project_dir, prompt, chat_id, message_id, claude_command))


def _handle_users_command(data: dict, args: str):
    """处理 /users 指令，查看已注册用户和在线状态

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /users，当前未使用）
    """
    from config import FEISHU_OWNER_ID as gateway_owner_id
    from services.binding_store import BindingStore
    from services.ws_registry import WebSocketRegistry

    event = data.get('event', {})
    message = event.get('message', {})
    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')

    # 获取数据
    binding_store = BindingStore.get_instance()
    ws_registry = WebSocketRegistry.get_instance()

    bindings = binding_store.get_all() if binding_store else {}
    ws_status = ws_registry.get_status() if ws_registry else {}

    # 构建并发送卡片
    card = _build_user_status_card(bindings, ws_status, gateway_owner_id)
    _run_in_background(_send_users_status_card, (chat_id, card, message_id))


def _build_user_status_card(bindings: dict, ws_status: dict, admin_id: str) -> dict:
    """构建用户状态卡片

    Args:
        bindings: 所有绑定信息
        ws_status: WebSocket 连接状态
        admin_id: 管理员 owner_id

    Returns:
        飞书卡片 JSON 结构
    """
    elements = []

    # 在线用户（已认证连接）
    online_ids = set(ws_status.get('authenticated_owner_ids', []))
    if online_ids:
        content_lines = []
        for oid in sorted(online_ids):
            at_tag = f'<at id="{oid}"></at>'
            marker = " (你)" if oid == admin_id else ""
            content_lines.append(f"• {at_tag}{marker}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**🟢 在线**\n" + "\n".join(content_lines)}
        })

    # 等待授权（pending 连接）
    pending = ws_status.get('pending', [])
    if pending:
        content_lines = []
        for p in pending:
            oid = p.get('owner_id', '')
            at_tag = f'<at id="{oid}"></at>'
            ip = p.get('client_ip', '-')
            wait_sec = p.get('waiting_seconds', 0)
            content_lines.append(f"• {at_tag} - {ip} (等待 {wait_sec}s)")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**🟡 等待授权**\n" + "\n".join(content_lines)}
        })

    # 离线用户（已注册但未在线）
    all_registered = set(bindings.keys())
    offline = all_registered - online_ids
    if offline:
        content_lines = []
        for oid in sorted(offline):
            info = bindings.get(oid, {})
            ts = info.get('updated_at', 0)
            if ts:
                now = int(time.time())
                diff = now - ts
                if diff < 60:
                    time_str = f" ({diff}s 前)"
                elif diff < 3600:
                    time_str = f" ({diff // 60} 分钟前)"
                elif diff < 86400:
                    time_str = f" ({diff // 3600} 小时前)"
                else:
                    time_str = f" ({diff // 86400} 天前)"
            else:
                time_str = ""
            at_tag = f'<at id="{oid}"></at>'
            content_lines.append(f"• {at_tag}{time_str}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**⚫ 离线**\n" + "\n".join(content_lines)}
        })

    # 统计信息
    total_registered = len(bindings)
    total_online = len(online_ids)
    total_pending = len(pending)

    elements.append({
        "tag": "hr"
    })
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"总计: 已注册 {total_registered} 人 | 在线 {total_online} 人 | 等待授权 {total_pending} 人"}
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📊 已注册用户和在线状态"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": elements
        }
    }


def _send_users_status_card(chat_id: str, card: dict, reply_to: str):
    """发送用户状态卡片（后台线程调用）

    Args:
        chat_id: 群聊 ID
        card: 卡片 JSON 结构
        reply_to: 要回复的消息 ID
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        return

    card_json = json.dumps(card, ensure_ascii=False)
    if reply_to:
        success, _ = service.reply_card(card_json, reply_to)
    else:
        success, _ = service.send_card(card_json, receive_id=chat_id, receive_id_type='chat_id')

    if not success:
        logger.warning(f"[feishu] Failed to send user status card to {chat_id}")


def _handle_groups_command(data: dict, args: str) -> None:
    """处理 /groups 命令：列出或解散群聊

    用法：
        /groups              - 列出活跃群聊
        /groups dissolve 1 2 - 按序号解散
        /groups dissolve all - 解散所有群聊
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    binding = _get_binding_from_event(event)

    args = args.strip()

    if args.startswith('dissolve'):
        dissolve_args = args[len('dissolve'):].strip()
        if dissolve_args == 'all':
            payload = {'all': True}
        elif dissolve_args:
            try:
                seqs = [int(x) for x in dissolve_args.split()]
            except ValueError:
                _run_in_background(_send_notice_message,
                                   (chat_id, "格式错误，示例：`/groups dissolve 1 2 3` 或 `/groups dissolve all`", message_id))
                return
            payload = {'seqs': seqs}
        else:
            _run_in_background(_send_notice_message,
                               (chat_id, "请指定要解散的群聊序号，示例：`/groups dissolve 1 2 3` 或 `/groups dissolve all`", message_id))
            return

        _run_in_background(_dissolve_groups, (binding, payload, chat_id, message_id))
    else:
        _run_in_background(_list_groups, (binding, chat_id, message_id))


def _list_groups(binding: Dict[str, Any], chat_id: str, message_id: str) -> None:
    """列出当前 owner 的活跃群聊（数据全来自 gateway 本地 store，零 RPC）"""
    from services.group_chat_store import GroupChatStore
    from services.group_session_store import GroupSessionStore

    owner_id = binding.get('_owner_id', '')
    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()

    if not group_store or not gs_store:
        _send_notice_message(chat_id, "存储服务未就绪，请稍后重试", message_id)
        return

    owner_chats = group_store.get_chats_by_owner(owner_id) if owner_id else []
    if not owner_chats:
        _send_notice_message(chat_id, "当前没有活跃的群聊会话", message_id)
        return

    # 聚合：group_chat_store（seq + chat_id + created_at）+ group_session_store（project_dir + last_active_at）
    now = int(time.time())
    gs_data = gs_store.get_by_owner(owner_id)

    def _format_age(delta: int) -> str:
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return "%d 分钟前" % (delta // 60)
        if delta < 86400:
            return "%d 小时前" % (delta // 3600)
        return "%d 天前" % (delta // 86400)

    entries = []
    for item in owner_chats:
        cid = item['chat_id']
        seq = item['seq']
        created_at = item.get('created_at', 0)
        gs_item = gs_data.get(cid) or {}
        project_dir = gs_item.get('project_dir', '')
        last_active_at = gs_item.get('last_active_at', created_at)
        entries.append({
            'chat_id': cid,
            'seq': seq,
            'project_dir': project_dir,
            'last_active_at': last_active_at,
        })
    # 按 last_active_at 降序（最近活跃的在前）
    entries.sort(key=lambda x: x['last_active_at'], reverse=True)

    lines = ["**活跃群聊列表** (%d 个)\n" % len(entries)]
    for e in entries:
        proj = os.path.basename(e['project_dir']) or '-'
        age = _format_age(now - e['last_active_at']) if e['last_active_at'] else ''
        lines.append("**#%d** | %s | %s" % (e['seq'], proj, age))

    lines.append("\n解散群聊：`/groups dissolve 序号` 或 `/groups dissolve all`")
    _send_notice_message(chat_id, '\n'.join(lines), message_id)


def _dissolve_groups(binding: Dict[str, Any], payload: dict,
                     chat_id: str, message_id: str) -> None:
    """解散当前 owner 的指定群聊（网关主导）

    执行顺序：
        1. gateway 本地定位目标 chat_ids（按 seqs 或 all）
        2. 调 batch_dissolve_groups（先通知 callback 标记 dissolved，再飞书 API 解散 + 清 GroupChatStore）
        3. 清 GroupSessionStore 对应条目
    """
    from services.group_chat_store import GroupChatStore
    from services.group_session_store import GroupSessionStore

    owner_id = binding.get('_owner_id', '')
    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()

    if not group_store or not gs_store:
        _send_notice_message(chat_id, "存储服务未就绪，请稍后重试", message_id)
        return

    # 1) 根据 payload 解出目标 chat_ids（本地查询，零 RPC）
    owner_chats = group_store.get_chats_by_owner(owner_id) if owner_id else []
    seq_to_chat = {item['seq']: item['chat_id'] for item in owner_chats}

    target_chat_ids: List[str] = []
    if payload.get('all'):
        target_chat_ids = [item['chat_id'] for item in owner_chats]
    else:
        # seqs 由 _handle_groups_command 上游 int() 校验，此处防御性跳过非法值
        seqs = payload.get('seqs') or []
        for seq in seqs:
            try:
                cid = seq_to_chat.get(int(seq))
            except (TypeError, ValueError):
                continue
            if cid:
                target_chat_ids.append(cid)

    if not target_chat_ids:
        _send_notice_message(chat_id, "没有找到可解散的群聊", message_id)
        return

    # 2) 调 batch_dissolve_groups（先通知 callback 标记 dissolved，再飞书 API 解散 + 清 GroupChatStore）
    result = batch_dissolve_groups(binding, target_chat_ids)
    dissolved_items = result.get('dissolved_items', [])
    failed = result.get('failed', [])
    # skipped_items 理论上不会出现（我们已按 owner 过滤），但兜底收敛
    skipped_items = result.get('skipped_items', [])

    # 3) 清 GroupSessionStore 对应条目
    if dissolved_items and owner_id:
        for cid in dissolved_items:
            gs_store.remove(owner_id, cid)

    # 4) 组合用户反馈
    parts = []
    if dissolved_items:
        parts.append("已解散 %d 个群聊" % len(dissolved_items))
    if failed:
        parts.append("%d 个解散失败（见服务日志）" % len(failed))
    if skipped_items:
        parts.append("%d 个外部群聊已跳过" % len(skipped_items))
    msg = "，".join(parts) if parts else "没有找到可解散的群聊"

    _send_notice_message(chat_id, msg, message_id)


def _handle_attach_command(data: dict, args: str) -> None:
    """处理 /attach <session_id_prefix> 命令：将 session 绑定到当前群聊

    仅支持在群聊中使用。session_id 前缀至少 8 字符，唯一匹配时执行绑定。
    """
    MIN_PREFIX_LEN = 8

    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    chat_type = message.get('chat_type', '')

    if chat_type != 'group':
        _run_in_background(_send_notice_message,
                           (chat_id, "`/attach` 仅支持在群聊中使用", message_id))
        return

    prefix = args.strip()
    if len(prefix) < MIN_PREFIX_LEN:
        _run_in_background(_send_notice_message,
                           (chat_id, f"用法：`/attach <session_id 前缀>`（至少 {MIN_PREFIX_LEN} 字符）",
                            message_id))
        return

    binding = _get_binding_from_event(event)
    _run_in_background(_forward_attach_request, (binding, prefix, chat_id, message_id))


def _forward_attach_request(binding: Dict[str, Any], prefix: str,
                            chat_id: str, message_id: str) -> None:
    """转发 /attach 请求到 Callback 后端并反馈结果

    Callback 响应结构：
        {
            'matched_ids': list[str],
            'attached': bool,
            'session_id': str,
            'original_chat_id': str,
            'project_dir': str,
        }

    dissolve_days 从 gateway 本地 binding 读取，不再经 callback 传递。
    """
    from services.group_chat_store import GroupChatStore
    from services.group_session_store import GroupSessionStore

    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()
    if not group_store or not gs_store:
        _send_notice_message(chat_id, "存储服务未就绪，请稍后重试", message_id)
        return

    resp = _forward_via_ws_or_http(binding, '/cb/session/attach', {
        'session_prefix': prefix,
        'chat_id': chat_id,
    })

    if not resp:
        _send_notice_message(chat_id, "Callback 服务不可达", message_id)
        return

    matched_ids = resp.get('matched_ids', [])

    if not matched_ids:
        _send_notice_message(chat_id, f"未找到匹配的 session：`{prefix}`", message_id)
        return

    if len(matched_ids) > 1:
        preview = '、'.join(s[:12] + '…' for s in matched_ids[:3])
        _send_notice_message(chat_id,
                             f"前缀匹配到多个 session（{preview}），请输入更长的前缀",
                             message_id)
        return

    # 唯一匹配已由 callback 侧执行绑定
    if not resp.get('attached'):
        _send_notice_message(chat_id, "绑定失败，请查看日志", message_id)
        return

    session_id = resp.get('session_id', '')
    original_chat_id = resp.get('original_chat_id', '')
    dissolve_days = binding.get('group_dissolve_days', 0) or 0

    # gateway 自查原群 seq（attach 到自己所在群时 original==target，不提示孤儿群）
    original_seq: Optional[int] = None
    if original_chat_id and original_chat_id != chat_id:
        original_seq = group_store.get_seq(original_chat_id)

    session_mode = binding.get('session_mode', '') if binding else ''
    is_group_mode = (session_mode == 'group')
    owner_id = binding.get('_owner_id', '') if binding else ''

    # 同步 gateway 侧 GroupSessionStore（仅 group 模式需要路由表）：
    # save 内部会自动清理本 session 的旧 chat 映射（若仍指向本 session），
    # 不需要外部显式 remove（避免误删已被其他 session 接管的映射）
    if is_group_mode and owner_id:
        gs_store.save(owner_id, chat_id, session_id, project_dir=resp.get('project_dir', ''))

    # 构造反馈
    lines = [f"✅ Session `{session_id[:8]}` 已绑定到当前群聊"]
    if is_group_mode:
        if original_seq is not None:
            hint = f"💡 原群聊 #{original_seq} 已成为孤儿群，可按需通过 `/groups dissolve {original_seq}` 手动解散"
            if dissolve_days > 0:
                hint += f"（空闲超过 {dissolve_days} 天也会被自动解散）"
            lines.append(hint)
    else:
        lines.append("⚠️ 当前会话模式非 group，后续会话通知会发送到本群，"
                     "但在本群直接发送消息（非回复）不会被自动路由到该 session")
    _send_notice_message(chat_id, '\n'.join(lines), message_id)


# =============================================================================
# 静音：/mute /unmute 命令 + 自动解除
# =============================================================================
# mute 状态由 SessionFacade 管理：callback 端 session_chat_store 为权威源，
# gateway 端本地缓存（write-through + lazy read-through）。
# handle_send_message 的出站拦截稳态下命中缓存零 RPC。

def _pick_mute_target(route_info: Dict[str, str], chat_id: str,
                      message_id: str) -> str:
    """从已解析好的 route_info 中提取目标 session_id，无法解析时给用户反馈

    仅用于用户主动命令（/mute、/unmute）。被动钩子（auto_unmute）自己内联判断，
    不经过此函数。

    Returns:
        解析成功返回 session_id，失败返回 ''
    """
    source = route_info.get('source', '')
    if SessionFacade.RouteSource.is_resolved(source):
        return route_info.get('session_id', '')
    hint = (_SESSION_NOT_FOUND_HINT
            if SessionFacade.RouteSource.is_parent_not_found(source)
            else _SESSION_UNRESOLVED_HINT)
    _run_in_background(_send_notice_message, (chat_id, hint, message_id))
    return ''


def _handle_mute_command(data: dict, args: str) -> None:
    """处理 /mute 命令：静音当前会话"""
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')

    binding = _get_binding_from_event(event)
    if not binding:
        return

    route_info = SessionFacade.resolve_from_message(data, binding)
    session_id = _pick_mute_target(route_info, chat_id, message_id)
    if not session_id:
        return

    changed = SessionFacade.mute(binding, session_id)
    if changed is None:
        _run_in_background(_send_notice_message,
                           (chat_id, "静音操作失败，请查看日志。", message_id))
        return

    sid_tag = f"session `{session_id[:8]}`"
    text = (f"已静音 {sid_tag}，后续消息将不再推送到此处。发送任意文字消息可自动解除静音。" if changed
            else f"{sid_tag} 已处于静音状态。")
    _run_in_background(_send_notice_message, (chat_id, text, message_id))


def _handle_unmute_command(data: dict, args: str) -> None:
    """处理 /unmute 命令：解除静音"""
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')

    binding = _get_binding_from_event(event)
    if not binding:
        return

    route_info = SessionFacade.resolve_from_message(data, binding)
    session_id = _pick_mute_target(route_info, chat_id, message_id)
    if not session_id:
        return

    changed = SessionFacade.unmute(binding, session_id)
    if changed is None:
        _run_in_background(_send_notice_message,
                           (chat_id, "解除静音失败，请查看日志。", message_id))
        return

    sid_tag = f"session `{session_id[:8]}`"
    text = (f"已解除 {sid_tag} 的静音。" if changed
            else f"{sid_tag} 当前未处于静音状态。")
    _run_in_background(_send_notice_message, (chat_id, text, message_id))


def _auto_unmute_if_needed(binding: Dict[str, Any], route_info: Dict[str, str],
                           chat_id: str, message_id: str) -> None:
    """非命令消息到达入口：若当前 session 处于静音则解除并提示

    使用主路由已解析好的 route_info，避免重复解析。作为被动钩子，对
    PARENT_NOT_FOUND / UNRESOLVED 均静默跳过，不给用户报错。
    """
    if not binding:
        return
    if not SessionFacade.RouteSource.is_resolved(route_info.get('source', '')):
        return
    session_id = route_info.get('session_id', '')
    if not session_id:
        return

    result = SessionFacade.unmute(binding, session_id)
    if result is True:
        _run_in_background(_send_notice_message,
                           (chat_id,
                            f"已自动解除 session `{session_id[:8]}` 的静音。",
                            message_id))
    elif result is None:
        # callback 调用失败：静默跳过用户反馈（被动钩子），但打 warning 留痕便于排障
        logger.warning("[feishu] auto_unmute failed: session_id=%s chat_id=%s",
                       session_id, chat_id)


def _handle_clear_command(data: dict, args: str) -> None:
    """处理 /clear 命令：清空当前群聊会话，预创建新 session

    仅支持 group 模式的群聊中使用。解绑旧 session 并预创建新 session（继承
    project_dir + claude_command），下次发送消息自动启动新 Claude 进程。
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    chat_type = message.get('chat_type', '')

    if chat_type != 'group':
        _run_in_background(_send_notice_message,
                           (chat_id, "`/clear` 仅支持在群聊中使用", message_id))
        return

    binding = _get_binding_from_event(event)
    if not binding:
        _run_in_background(_send_notice_message,
                           (chat_id, "您尚未注册，无法使用此功能", message_id))
        return

    session_mode = binding.get('session_mode', 'message')
    if session_mode != 'group':
        _run_in_background(_send_notice_message,
                           (chat_id, "`/clear` 仅在群聊模式下可用", message_id))
        return

    from services.group_session_store import GroupSessionStore
    gs_store = GroupSessionStore.get_instance()
    owner_id = binding.get('_owner_id', '')

    if not gs_store or not owner_id:
        _run_in_background(_send_notice_message,
                           (chat_id, "存储服务未就绪，请稍后重试", message_id))
        return

    current = gs_store.get(owner_id, chat_id)
    if not current:
        _run_in_background(_send_notice_message,
                           (chat_id, "当前群聊没有活跃的会话", message_id))
        return

    # 幂等：上次 /clear 后还没发消息，不重复 clone
    if current.get('new_session'):
        _run_in_background(_send_notice_message,
                           (chat_id, "会话上下文已清空，下次发送消息将自动创建新 Claude 会话。",
                            message_id))
        return

    old_session_id = current.get('session_id', '')
    new_session_id = str(uuid.uuid4())

    # 1. callback 侧创建新 session（继承旧 session 属性）
    try:
        resp = _forward_via_ws_or_http(binding, '/cb/session/clone', {
            'old_session_id': old_session_id,
            'new_session_id': new_session_id,
            'chat_id': chat_id,
        })
    except Exception as e:
        logger.error("[feishu] /clear clone failed: %s", e)
        _run_in_background(_send_notice_message,
                           (chat_id, "清空会话失败，请稍后重试", message_id))
        return

    if not resp or not resp.get('ok'):
        logger.error("[feishu] /clear clone returned error: %s", resp)
        _run_in_background(_send_notice_message,
                           (chat_id, "清空会话失败，请稍后重试", message_id))
        return

    project_dir = resp.get('project_dir', '')

    # 2. 替换网关侧路由映射（新 session + new_session 标志）
    gs_store.save(owner_id, chat_id, new_session_id,
                  project_dir=project_dir, new_session=True)

    logger.info("[feishu] /clear: owner=%s chat=%s old=%s new=%s",
                owner_id, chat_id, old_session_id[:8], new_session_id[:8])
    _run_in_background(_send_notice_message,
                       (chat_id, "会话上下文已清空，下次发送消息将自动创建新 Claude 会话。",
                        message_id))


# =============================================================================
# 命令映射（放在文件末尾，避免函数未定义的问题）
# =============================================================================

# 支持的命令映射：命令名 -> (处理函数, 是否管理员专属, 帮助文本)
# admin_only 为 True 时，仅管理员可见和执行
_COMMANDS = {
    'new': (_handle_new_command, False, "发起新的 Claude 会话\n格式：`/new --dir=/path/to/project [--cmd=0] prompt` 或回复消息时 `/new prompt`"),
    'reply': (_handle_reply_command, False, "回复消息时指定 Claude Command 继续会话\n格式：`/reply [--cmd=0] prompt`\n仅支持在回复消息时使用"),
    'attach': (_handle_attach_command, False, "将指定 session 绑定到当前群聊\n格式：`/attach <session_id 前缀>`（至少 8 字符）\n仅支持在群聊中使用"),
    'clear': (_handle_clear_command, False, "清空当前群聊会话\n解绑当前会话，下次发消息自动创建新会话\n仅在群聊模式下可用"),
    'mute': (_handle_mute_command, False, "静音当前会话，后续消息不再推送\n格式：`/mute`（发送任意文字消息可自动解除）"),
    'unmute': (_handle_unmute_command, False, "解除当前会话静音\n格式：`/unmute`"),
    'users': (_handle_users_command, True, "查看已注册用户和在线状态"),
    'groups': (_handle_groups_command, False, "管理群聊会话\n`/groups` 列表 | `/groups dissolve 1 2` 解散 | `/groups dissolve all` 全部解散"),
}


# 模块加载末尾：注入 SessionFacade 的下游依赖（避免 services -> handlers 循环 import）
SessionFacade.configure(forward_fn=_forward_via_ws_or_http)
