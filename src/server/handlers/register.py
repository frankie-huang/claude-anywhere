"""Gateway Register Handler - 网关注册接口处理器

两种注册模式：
    HTTP 模式：Callback 后端需公网可达（网关通过 HTTP 转发请求）
    WebSocket 模式：Callback 后端无需公网可达（主动连接网关建立隧道）

HTTP 注册流程（网关使用）：
    - handle_register_request(): 接收 Callback 后端的注册请求
    - handle_authorization_decision(): 处理用户授权决策（允许/拒绝）
    - handle_register_unbind(): 处理用户解绑操作

WebSocket 注册流程（网关使用）：
    - handle_ws_registration(): 处理 WS 隧道的首次注册
    - handle_ws_rebind_registration(): 处理 WS 隧道的换绑请求
    - handle_ws_authorization_approved(): 处理 WS 模式授权通过
    - handle_ws_authorization_denied(): 处理 WS 模式授权拒绝
    - handle_ws_register_unbind(): 处理 WS 模式用户解绑

Callback 后端使用：
    - handle_register_callback(): 接收网关的 auth_token 通知
    - handle_check_owner_id(): 验证 owner_id 所属权

数据存储服务（从 services 导入）：
    - AuthTokenStore: 存储 auth_token
    - BindingStore: 存储绑定关系
    - WebSocketRegistry: WS 连接注册表
"""

import json
import logging
import socket
import urllib.error
from typing import Tuple, Dict, Any, List, Optional

from services.auth_token_store import AuthTokenStore
from handlers.utils import run_in_background as _run_in_background, post_json as _post_json

logger = logging.getLogger(__name__)

# HTTP 请求超时（秒）
HTTP_TIMEOUT = 10

# 授权卡片中的安全提示文案（HTTP 和 WS 注册流程共用）
_PERMISSION_DESCRIPTION = (
    "**授权后该后端将获得以下权限：**\n"
    "• 接收你发送给机器人的所有消息\n"
    "• 向你发送消息通知（如权限请求、任务状态等）"
)
_SECURITY_WARNING = (
    "**安全风险提示：**\n"
    "• 后端可读取你的对话内容\n"
    "• 后端可主动向你推送消息\n"
    "• 请确认该后端来源可信后再授权"
)
_SECURITY_WARNING_REBIND = (
    "**安全风险提示：**\n"
    "• 旧终端将被断开连接\n"
    "• 新终端可读取你的对话内容\n"
    "• 请确认该请求来源可信后再授权"
)


def get_bot_open_id() -> Optional[str]:
    """从 FeishuAPIService 获取机器人 open_id"""
    from services.feishu_api import FeishuAPIService
    service = FeishuAPIService.get_instance()
    if service:
        return service.bot_open_id
    return None


def handle_register_request(data: dict, client_ip: str = '') -> Tuple[bool, dict]:
    """处理 Callback 后端的注册请求

    网关立即返回成功，后续异步处理：
        - 已绑定：直接调用 Callback 后端的 /cb/register
        - 未绑定：发送飞书授权卡片

    Args:
        data: 请求数据
            - callback_url: Callback 后端 URL（必需）
            - owner_id: 飞书用户 ID（必需）
            - at_bot_only: 群聊 @bot 过滤（可选）
            - session_mode: 会话模式，message/thread/group（可选，默认 message）
            - reply_in_thread: 已废弃，兼容旧客户端（True 映射为 session_mode='thread'）
            - claude_commands: 可用的 Claude 命令列表（可选）
            - default_chat_dir: 默认聊天目录（可选）
            - default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式（可选）
            - group_name_prefix: 群聊名称前缀（可选）
            - group_dissolve_days: 群聊自动解散天数（可选）
        client_ip: 客户端 IP 地址

    Returns:
        (success, response): success 表示是否处理成功，response 是响应数据
    """
    callback_url = data.get('callback_url', '')
    owner_id = data.get('owner_id', '')
    at_bot_only = data.get('at_bot_only')
    claude_commands = data.get('claude_commands', None)
    default_chat_dir = data.get('default_chat_dir', '')
    default_chat_follow_thread = data.get('default_chat_follow_thread', True)
    group_name_prefix = data.get('group_name_prefix')
    group_dissolve_days = data.get('group_dissolve_days')

    # 入口转换：优先使用 session_mode，旧客户端用 reply_in_thread 映射
    session_mode = data.get('session_mode', '')
    if not session_mode or session_mode not in ('message', 'thread', 'group'):
        reply_in_thread = data.get('reply_in_thread', False)
        session_mode = 'thread' if reply_in_thread else 'message'

    # 验证参数
    if not callback_url or not owner_id:
        logger.warning("[register] Missing params: callback_url or owner_id")
        return True, {
            'success': False,
            'error': 'missing required fields: callback_url, owner_id'
        }

    logger.info(f"[register] Registration request: owner_id={owner_id}, callback_url={callback_url}, ip={client_ip}, session_mode={session_mode}, claude_commands={claude_commands}, default_chat_dir={default_chat_dir}")

    # 在后台线程中处理注册逻辑（异步）
    _run_in_background(_process_registration, (callback_url, owner_id, client_ip, at_bot_only, session_mode, claude_commands, default_chat_dir, default_chat_follow_thread, group_name_prefix, group_dissolve_days))

    # 立即返回成功
    return True, {
        'success': True,
        'message': 'Registration request received, processing in background'
    }


def handle_register_callback(data: dict) -> Tuple[bool, dict]:
    """处理网关注册回调（网关调用 Callback 后端）

    Callback 后端接收网关的 auth_token 通知并存储。

    Args:
        data: 请求数据
            - owner_id: 飞书用户 ID
            - auth_token: 认证令牌
            - bot_open_id: 机器人 open_id（可选）
            - gateway_version: 网关版本（可选）

    Returns:
        (success, response)
    """
    from config import get_config

    owner_id = data.get('owner_id', '')
    auth_token = data.get('auth_token', '')
    bot_open_id = data.get('bot_open_id', '')
    gateway_version = data.get('gateway_version', '')

    # 获取配置的 owner_id
    config_owner_id = get_config('FEISHU_OWNER_ID', '')

    if not owner_id or not auth_token:
        logger.warning("[cb/register] Missing params: owner_id or auth_token")
        return True, {
            'success': False,
            'error': 'missing required fields: owner_id, auth_token'
        }

    # 验证 owner_id 是否与配置一致
    if config_owner_id and owner_id != config_owner_id:
        logger.warning(
            f"[cb/register] owner_id mismatch: received={owner_id}, config={config_owner_id}"
        )
        return True, {
            'success': False,
            'error': 'owner_id mismatch'
        }

    logger.info(
        f"[cb/register] Storing auth_token for owner_id={owner_id}, "
        f"bot_open_id={bot_open_id}, gateway_version={gateway_version}"
    )

    # 存储 auth_token（及 bot_open_id）
    token_store = AuthTokenStore.get_instance()
    if token_store:
        if token_store.save(owner_id, auth_token, bot_open_id=bot_open_id):
            logger.info(f"[cb/register] Auth token stored successfully")
            return True, {
                'success': True,
                'message': 'Registration successful'
            }
        else:
            return True, {
                'success': False,
                'error': 'Failed to store token'
            }
    else:
        logger.warning("[cb/register] AuthTokenStore not initialized")
        return True, {
            'success': False,
            'error': 'AuthTokenStore not initialized'
        }


def handle_check_owner_id(data: dict) -> Tuple[bool, dict]:
    """处理 owner_id 验证请求（网关调用 Callback 后端）

    网关在发送授权卡片前，先验证 callback_url 是否属于该 owner_id。
    这防止了恶意注册请求。

    Args:
        data: 请求数据
            - owner_id: 飞书用户 ID

    Returns:
        (success, response): response 包含 is_owner 字段
    """
    from config import get_config

    request_owner_id = data.get('owner_id', '')
    config_owner_id = get_config('FEISHU_OWNER_ID', '')

    logger.info(f"[cb/check-owner] Request: owner_id={request_owner_id}")

    # 验证 owner_id 是否与配置一致
    is_owner = bool(request_owner_id and request_owner_id == config_owner_id)

    if is_owner:
        logger.info(f"[cb/check-owner] Verification passed")
    else:
        logger.warning(
            f"[cb/check-owner] Verification failed: request={request_owner_id}, config={config_owner_id}"
        )

    return True, {
        'success': True,
        'is_owner': is_owner
    }


def _process_registration(
    callback_url: str,
    owner_id: str,
    client_ip: str,
    at_bot_only: Optional[bool] = None,
    session_mode: str = 'message',
    claude_commands: Optional[List[str]] = None,
    default_chat_dir: str = '',
    default_chat_follow_thread: bool = True,
    group_name_prefix: Optional[str] = None,
    group_dissolve_days: Optional[int] = None
):
    """处理注册逻辑（后台线程）

    1. 查询绑定关系
    2. 已绑定且 callback_url 相同：直接调用 /cb/register 通知新 token
    3. 已绑定但 callback_url 不同：发送飞书授权卡片让用户确认是否更新
    4. 未绑定：先验证 callback_url 所属，再发送飞书授权卡片

    注意：auth_token 不再提前生成，而是在用户点击允许后实时生成。

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数
    """
    from services.binding_store import BindingStore
    from services.auth_token import generate_auth_token
    from config import FEISHU_APP_SECRET

    # 验证配置
    if not FEISHU_APP_SECRET:
        logger.error("[register] FEISHU_APP_SECRET not configured")
        return

    # 查询现有绑定
    store = BindingStore.get_instance()
    if not store:
        logger.error("[register] BindingStore not initialized")
        return

    binding = store.get(owner_id)

    if binding:
        # 已绑定，检查 callback_url 是否一致
        bound_callback_url = binding.get('callback_url', '')
        if bound_callback_url == callback_url:
            # callback_url 一致：直接更新 token（无需用户确认）
            logger.info(f"[register] Existing binding with same callback_url, updating token")
            auth_token = generate_auth_token(FEISHU_APP_SECRET, owner_id)
            _notify_callback(callback_url, owner_id, auth_token, client_ip)
            store.upsert(
                owner_id, callback_url, auth_token, client_ip,
                at_bot_only,
                session_mode, claude_commands,
                default_chat_dir, default_chat_follow_thread,
                group_name_prefix, group_dissolve_days
            )
        else:
            # callback_url 不同：发送授权卡片让用户确认是否更换设备
            # 不提前生成 auth_token，等用户批准后再生成
            logger.info(
                f"[register] Existing binding with different callback_url: "
                f"old={bound_callback_url}, new={callback_url}"
            )
            _send_authorization_card(
                callback_url, owner_id, client_ip,
                old_callback_url=bound_callback_url,
                at_bot_only=at_bot_only,
                session_mode=session_mode,
                claude_commands=claude_commands,
                default_chat_dir=default_chat_dir,
                default_chat_follow_thread=default_chat_follow_thread,
                group_name_prefix=group_name_prefix,
                group_dissolve_days=group_dissolve_days
            )
    else:
        # 未绑定：先验证 callback_url 是否属于该 owner_id
        logger.info(f"[register] No existing binding, verifying callback_url ownership")
        if not _check_owner_id(callback_url, owner_id):
            logger.warning(
                f"[register] Owner verification failed: callback_url={callback_url}, owner_id={owner_id}. "
                f"This may be a malicious registration request."
            )
            return
        # 验证通过，发送飞书授权卡片
        # 不提前生成 auth_token，等用户批准后再生成
        _send_authorization_card(
            callback_url, owner_id, client_ip,
            at_bot_only=at_bot_only,
            session_mode=session_mode,
            claude_commands=claude_commands,
            default_chat_dir=default_chat_dir,
            default_chat_follow_thread=default_chat_follow_thread,
            group_name_prefix=group_name_prefix,
            group_dissolve_days=group_dissolve_days
        )


def _check_owner_id(callback_url: str, owner_id: str) -> bool:
    """验证 callback_url 是否属于该 owner_id

    调用 Callback 后端的 /cb/check-owner 接口进行验证。

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID

    Returns:
        True 表示验证通过，False 表示验证失败
    """
    api_url = f"{callback_url.rstrip('/')}/cb/check-owner"

    request_data = {
        'owner_id': owner_id
    }

    logger.info(f"[register] Checking owner_id: {api_url}")

    try:
        response_data = _post_json(api_url, request_data, timeout=HTTP_TIMEOUT)
        is_owner = response_data.get('is_owner', False)
        logger.info(f"[register] Owner check result: {is_owner}")
        return is_owner

    except urllib.error.HTTPError as e:
        logger.error(f"[register] Owner check HTTP error: {e.code} {e.reason}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"[register] Owner check URL error: {e.reason}")
        return False
    except socket.timeout:
        logger.error("[register] Owner check timeout")
        return False
    except Exception as e:
        logger.error(f"[register] Owner check error: {e}")
        return False


def _notify_callback(callback_url: str, owner_id: str, auth_token: str, client_ip: str):
    """通知 Callback 后端 auth_token

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID
        auth_token: 认证令牌
        client_ip: 客户端 IP
    """
    api_url = f"{callback_url.rstrip('/')}/cb/register"

    request_data = {
        'owner_id': owner_id,
        'auth_token': auth_token,
        'gateway_version': '1.0.0'
    }

    # 附带 bot_open_id，供 Callback 端存储到 runtime 文件
    bot_open_id = get_bot_open_id()
    if bot_open_id:
        request_data['bot_open_id'] = bot_open_id

    logger.info(f"[register] Calling {api_url}")

    try:
        response_data = _post_json(api_url, request_data, auth_token=auth_token, timeout=HTTP_TIMEOUT)
        logger.info(f"[register] Callback response: {response_data}")

    except urllib.error.HTTPError as e:
        logger.error(f"[register] Callback HTTP error: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        logger.error(f"[register] Callback URL error: {e.reason}")
    except socket.timeout:
        logger.error("[register] Callback timeout")
    except Exception as e:
        logger.error(f"[register] Callback error: {e}")


def _build_authorization_card(title: str, content: str, approve_value: dict, deny_value: dict) -> dict:
    """构建授权卡片（允许/拒绝按钮）

    HTTP 和 WS 注册流程共用卡片骨架，仅按钮回调 value 不同。

    Args:
        title: 卡片标题
        content: 卡片正文（lark_md 格式）
        approve_value: 允许按钮的 callback value
        deny_value: 拒绝按钮的 callback value

    Returns:
        飞书卡片 JSON 字典
    """
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content}
                },
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_spacing": "8px",
                    "background_style": "default",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "vertical_align": "top",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "允许"},
                                "type": "primary",
                                "behaviors": [{"type": "callback", "value": approve_value}]
                            }]
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "vertical_align": "top",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "拒绝"},
                                "type": "danger",
                                "behaviors": [{"type": "callback", "value": deny_value}]
                            }]
                        }
                    ]
                }
            ]
        }
    }


def _notify_admin_of_registration(
    service: Any,
    requester_id: str,
    client_ip: str,
    callback_url: str,
    old_callback_url: str = ''
):
    """发送注册通知给网关管理员

    如果未配置 FEISHU_OWNER_ID 或管理员就是请求者自己，则不发送通知。

    Args:
        service: 飞书 API 服务实例
        requester_id: 请求注册的用户 ID
        client_ip: 客户端 IP
        callback_url: Callback 后端 URL
        old_callback_url: 旧的 callback_url（有值则表示换绑场景）
    """
    from config import FEISHU_OWNER_ID as gateway_owner_id

    # 未配置管理员或管理员就是请求者自己，不需要通知
    if not gateway_owner_id or gateway_owner_id == requester_id:
        return

    # 构建通知内容（有 old_callback_url 则是换绑场景）
    is_rebind = bool(old_callback_url)
    if is_rebind:
        action_text = "换绑"
        detail = f"**旧设备**: `{old_callback_url}`\n**新设备**: `{callback_url}`"
    else:
        action_text = "注册"
        detail = f"**Callback URL**: `{callback_url}`"

    # 使用 lark_md 格式，@ 注册用户让管理员知道是谁
    content = (
        f"<at id=\"{requester_id}\"></at> 正在请求{action_text}\n\n"
        f"**来源 IP**: `{client_ip}`\n"
        f"{detail}"
    )

    # 构建简单的通知卡片
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"用户{action_text}通知"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content}
                }
            ]
        }
    }

    success, result = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=gateway_owner_id)

    if success:
        logger.info(f"[register] Admin notification sent to {gateway_owner_id}")
    else:
        logger.error(f"[register] Failed to send admin notification: {result}")


def _send_authorization_card(
    callback_url: str,
    owner_id: str,
    client_ip: str,
    old_callback_url: str = '',
    at_bot_only: Optional[bool] = None,
    session_mode: str = 'message',
    claude_commands: Optional[List[str]] = None,
    default_chat_dir: str = '',
    default_chat_follow_thread: bool = True,
    group_name_prefix: Optional[str] = None,
    group_dissolve_days: Optional[int] = None
):
    """发送飞书授权卡片（HTTP 注册模式）

    注意：不在卡片中嵌入 auth_token，而是等用户授权后再实时生成。
    用户点击允许时会验证 operator_id == owner_id，确保本人操作。

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        old_callback_url: 旧的 callback_url（如果有，表示更换设备场景）
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.warning("[register] FeishuAPIService not enabled, cannot send authorization card")
        return

    # 构建卡片内容
    if old_callback_url:
        # 更换设备场景
        title = "Callback 后端更换设备请求"
        content = (
            f"**旧设备**: `{old_callback_url}`\n"
            f"**新设备**: `{callback_url}`\n"
            f"**来源 IP**: `{client_ip}`\n\n"
            f"{_PERMISSION_DESCRIPTION}\n\n"
            f"{_SECURITY_WARNING}\n\n"
            f"是否允许更换到新设备？"
        )
    else:
        # 新设备注册场景
        title = "新的 Callback 后端注册请求"
        content = (
            f"**Callback URL**: `{callback_url}`\n"
            f"**来源 IP**: `{client_ip}`\n\n"
            f"{_PERMISSION_DESCRIPTION}\n\n"
            f"{_SECURITY_WARNING}\n\n"
            f"是否允许该后端绑定？"
        )

    card = _build_authorization_card(
        title, content,
        approve_value={
            "action": "approve_register",
            "callback_url": callback_url,
            "owner_id": owner_id,
            "request_ip": client_ip,
            "old_callback_url": old_callback_url,
            "at_bot_only": at_bot_only,
            "session_mode": session_mode,
            "claude_commands": claude_commands,
            "default_chat_dir": default_chat_dir,
            "default_chat_follow_thread": default_chat_follow_thread,
            "group_name_prefix": group_name_prefix,
            "group_dissolve_days": group_dissolve_days
        },
        deny_value={
            "action": "deny_register",
            "callback_url": callback_url,
            "owner_id": owner_id
        }
    )

    success, result = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=owner_id)

    if success:
        logger.info(f"[register] Authorization card sent to {owner_id}")
    else:
        logger.error(f"[register] Failed to send authorization card: {result}")

    # 通知网关管理员
    _notify_admin_of_registration(
        service=service,
        requester_id=owner_id,
        client_ip=client_ip,
        callback_url=callback_url,
        old_callback_url=old_callback_url
    )


def _build_register_status_card(
    title: str,
    content: str,
    template: str,
    button: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """构建注册授权状态卡片（用于卡片回调响应）

    飞书卡片回调响应格式要求：
    {
      "type": "raw",
      "data": { ... schema 2.0 卡片数据 ... }
    }

    Args:
        title: 卡片标题
        content: 卡片内容（支持 Markdown）
        template: 标题模板颜色（green/red/blue等）
        button: 可选按钮配置（包含 behaviors 格式）

    Returns:
        卡片字典（包含 type 和 data）
    """
    elements = [
        {
            'tag': 'div',
            'text': {
                'tag': 'lark_md',
                'content': content
            }
        }
    ]

    if button:
        elements.append({'tag': 'hr'})
        elements.append({
            'tag': 'column_set',
            'flex_mode': 'none',
            'horizontal_spacing': '8px',
            'background_style': 'default',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'vertical_align': 'top',
                    'elements': [
                        {
                            'tag': button.get('tag', 'button'),
                            'text': button.get('text'),
                            'type': button.get('type'),
                            'behaviors': [
                                {
                                    'type': 'callback',
                                    'value': button.get('value')
                                }
                            ]
                        }
                    ]
                }
            ]
        })

    return {
        'type': 'raw',
        'data': {
            'schema': '2.0',
            'config': {'wide_screen_mode': True},
            'header': {
                'title': {'tag': 'plain_text', 'content': title},
                'template': template
            },
            'body': {
                'direction': 'vertical',
                'elements': elements
            }
        }
    }


def handle_authorization_decision(
    callback_url: str,
    owner_id: str,
    client_ip: str,
    approved: bool,
    at_bot_only: Optional[bool] = None,
    session_mode: str = 'message',
    claude_commands: Optional[List[str]] = None,
    default_chat_dir: str = '',
    default_chat_follow_thread: bool = True,
    group_name_prefix: Optional[str] = None,
    group_dissolve_days: Optional[int] = None
) -> Dict[str, Any]:
    """处理用户授权决策

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        approved: 用户是否批准
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    from services.binding_store import BindingStore
    from services.auth_token import generate_auth_token
    from config import FEISHU_APP_SECRET

    if approved:
        # 验证配置
        if not FEISHU_APP_SECRET:
            logger.error("[register] FEISHU_APP_SECRET not configured")
            return {
                'toast': {
                    'type': 'error',
                    'content': '服务配置错误'
                }
            }

        # 实时生成 auth_token
        auth_token = generate_auth_token(FEISHU_APP_SECRET, owner_id)
        logger.info(f"[register] Authorization approved, generated auth_token")

        # 用户允许：通知 Callback 后端并创建绑定
        store = BindingStore.get_instance()
        if store:
            store.upsert(
                owner_id, callback_url, auth_token, client_ip,
                at_bot_only,
                session_mode, claude_commands,
                default_chat_dir, default_chat_follow_thread,
                group_name_prefix, group_dissolve_days
            )

        # 通知 Callback 后端
        _notify_callback(callback_url, owner_id, auth_token, client_ip)

        # 返回已授权卡片（带解绑按钮）
        content = (
            f"**Callback URL**: `{callback_url}`\n"
            f"**来源 IP**: `{client_ip}`\n\n"
            f"**已授权权限：**\n"
            f"• 接收你发送给机器人的所有消息\n"
            f"• 向你发送消息通知\n\n"
            f"已成功授权该后端"
        )
        return {
            'toast': {
                'type': 'success',
                'content': '已授权绑定'
            },
            'card': _build_register_status_card(
                title='✓ 已授权',
                content=content,
                template='green',
                button={
                    'tag': 'button',
                    'text': {'tag': 'plain_text', 'content': '解绑'},
                    'type': 'danger',
                    'value': {
                        'action': 'unbind_register',
                        'callback_url': callback_url,
                        'owner_id': owner_id
                    }
                }
            )
        }
    else:
        # 用户拒绝：仅删除 owner_id + callback_url 精确匹配的绑定
        toast_content = '已拒绝注册请求'
        store = BindingStore.get_instance()
        if store:
            existing = store.get(owner_id)
            if existing and existing.get('callback_url') == callback_url:
                store.delete(owner_id)
                toast_content = '已拒绝并解除绑定'
                logger.info(f"[register] User denied, deleted binding for {callback_url}: owner_id={owner_id}")

        logger.info(f"[register] User denied registration: owner_id={owner_id}")
        return {
            'toast': {
                'type': 'success' if '解除' in toast_content else 'info',
                'content': toast_content
            },
            'card': _build_register_status_card(
                title='✗ 已拒绝',
                content=f"**Callback URL**: `{callback_url}`\n\n已拒绝该后端的注册请求",
                template='red'
            )
        }


def handle_register_unbind(callback_url: str, owner_id: str) -> Dict[str, Any]:
    """处理用户解绑操作（HTTP 模式）

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    from services.binding_store import BindingStore

    # 删除绑定记录
    store = BindingStore.get_instance()
    if store:
        existing = store.get(owner_id)
        if existing and existing.get('callback_url') == callback_url:
            store.delete(owner_id)
            logger.info("[register] User unbound: owner_id=%s, callback_url=%s", owner_id, callback_url)
        else:
            logger.info("[register] User unbind (no existing binding): owner_id=%s, callback_url=%s", owner_id, callback_url)

    # 返回已解绑卡片（无按钮）
    return {
        'toast': {
            'type': 'info',
            'content': '已解绑'
        },
        'card': _build_register_status_card(
            title='✗ 已解绑',
            content=(
                f"**Callback URL**: `{callback_url}`\n\n"
                f"已解除绑定，该后端将：\n"
                f"• 无法再接收你的消息\n"
                f"• 无法再向你发送通知"
            ),
            template='grey'
        )
    }


# =============================================================================
# WebSocket 注册流程
# =============================================================================

def _send_ws_authorization_card(owner_id: str, request_id: str,
                                client_ip: str, title: str, content: str,
                                at_bot_only: Optional[bool] = None,
                                session_mode: str = 'message',
                                claude_commands: Optional[List[str]] = None,
                                default_chat_dir: str = '',
                                default_chat_follow_thread: bool = True,
                                old_ip: str = '',
                                group_name_prefix: Optional[str] = None,
                                group_dissolve_days: Optional[int] = None) -> bool:
    """发送 WS 模式授权卡片（注册/换绑共用）

    每个终端独立发送自己的卡片，包含允许/拒绝按钮。
    当用户允许其中一个终端时，其他 pending 连接会被自动关闭。

    Args:
        owner_id: 飞书用户 ID
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        client_ip: 客户端 IP
        title: 卡片标题
        content: 卡片正文（lark_md 格式）
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        old_ip: 旧终端 IP（有值则表示换绑场景）
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数

    Returns:
        True 表示卡片发送成功
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.warning("[ws_register] FeishuAPIService not enabled, cannot send authorization card")
        return False

    card = _build_authorization_card(
        title, content,
        approve_value={
            "action": "approve_register",
            "mode": "ws",
            "owner_id": owner_id,
            "request_id": request_id,
            "request_ip": client_ip,
            "at_bot_only": at_bot_only,
            "session_mode": session_mode,
            "claude_commands": claude_commands,
            "default_chat_dir": default_chat_dir,
            "default_chat_follow_thread": default_chat_follow_thread,
            "group_name_prefix": group_name_prefix,
            "group_dissolve_days": group_dissolve_days
        },
        deny_value={
            "action": "deny_register",
            "mode": "ws",
            "owner_id": owner_id,
            "request_id": request_id
        }
    )

    success, result = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=owner_id)

    if success:
        logger.info("[ws_register] Authorization card sent to %s: %s", owner_id, title)
    else:
        logger.error("[ws_register] Failed to send authorization card: %s", result)

    # 通知网关管理员
    _notify_admin_of_registration(
        service=service,
        requester_id=owner_id,
        client_ip=client_ip,
        callback_url=f'WS 隧道 ({client_ip})',
        old_callback_url=f'WS 隧道 ({old_ip})' if old_ip else ''
    )

    return success


def handle_ws_registration(owner_id: str, request_id: str,
                           client_ip: str,
                           at_bot_only: Optional[bool] = None,
                           session_mode: str = 'message',
                           claude_commands: Optional[List[str]] = None,
                           default_chat_dir: str = '',
                           default_chat_follow_thread: bool = True,
                           group_name_prefix: Optional[str] = None,
                           group_dissolve_days: Optional[int] = None) -> bool:
    """处理 WebSocket 隧道的注册请求

    发送飞书授权卡片，用户授权后通过 WS 通道下发 auth_token。
    每个终端独立发送自己的卡片，允许其中一个时自动关闭其他 pending 连接。

    Args:
        owner_id: 飞书用户 ID
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        client_ip: 客户端 IP
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数

    Returns:
        True 表示卡片发送成功
    """
    title = "WebSocket 隧道连接请求"
    content = (
        f"**来源 IP**: `{client_ip}`\n\n"
        f"有本地 Callback 后端尝试通过 WebSocket 隧道连接。\n\n"
        f"{_PERMISSION_DESCRIPTION}\n\n"
        f"{_SECURITY_WARNING}\n\n"
        f"是否允许该连接？"
    )
    return _send_ws_authorization_card(owner_id, request_id, client_ip, title, content,
                                       at_bot_only=at_bot_only,
                                       session_mode=session_mode,
                                       claude_commands=claude_commands,
                                       default_chat_dir=default_chat_dir,
                                       default_chat_follow_thread=default_chat_follow_thread,
                                       group_name_prefix=group_name_prefix,
                                       group_dissolve_days=group_dissolve_days)


def handle_ws_rebind_registration(owner_id: str, request_id: str,
                                   client_ip: str, old_ip: str,
                                   at_bot_only: Optional[bool] = None,
                                   session_mode: str = 'message',
                                   claude_commands: Optional[List[str]] = None,
                                   default_chat_dir: str = '',
                                   default_chat_follow_thread: bool = True,
                                   group_name_prefix: Optional[str] = None,
                                   group_dissolve_days: Optional[int] = None) -> bool:
    """处理 WS 模式的换绑注册请求（新终端替换旧终端）

    发送飞书授权卡片，展示旧终端 IP 和新终端 IP，用户授权后走现有
    handle_ws_authorization_approved() 逻辑。
    每个终端独立发送自己的卡片，允许其中一个时自动关闭其他 pending 连接。

    Args:
        owner_id: 飞书用户 ID
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        client_ip: 新终端 IP
        old_ip: 旧终端 IP
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数

    Returns:
        True 表示卡片发送成功
    """
    title = "WebSocket 隧道换绑请求"
    content = (
        f"**旧终端 IP**: `{old_ip or '未知'}`\n"
        f"**新终端 IP**: `{client_ip}`\n\n"
        f"有新终端尝试通过 WebSocket 隧道连接，将替换当前已绑定的终端。\n\n"
        f"{_PERMISSION_DESCRIPTION}\n\n"
        f"{_SECURITY_WARNING_REBIND}\n\n"
        f"是否允许换绑到新终端？"
    )
    return _send_ws_authorization_card(owner_id, request_id, client_ip, title, content,
                                       at_bot_only=at_bot_only,
                                       session_mode=session_mode,
                                       claude_commands=claude_commands,
                                       default_chat_dir=default_chat_dir,
                                       default_chat_follow_thread=default_chat_follow_thread,
                                       old_ip=old_ip,
                                       group_name_prefix=group_name_prefix,
                                       group_dissolve_days=group_dissolve_days)


def ws_send_auth_ok(sock: 'socket.socket', auth_token: str) -> None:
    """通过 WebSocket 发送 auth_ok 消息（含 bot_open_id）

    WS 模式首次授权和续期重连共用此函数，确保 auth_ok 消息格式一致。
    """
    from services.ws_protocol import ws_send_text
    auth_ok_data = {
        'type': 'auth_ok',
        'auth_token': auth_token
    }
    bot_open_id = get_bot_open_id()
    if bot_open_id:
        auth_ok_data['bot_open_id'] = bot_open_id
    msg = json.dumps(auth_ok_data)
    ws_send_text(sock, msg)


def handle_ws_authorization_approved(owner_id: str, request_id: str,
                                     client_ip: str,
                                     at_bot_only: Optional[bool] = None,
                                     session_mode: str = 'message',
                                     claude_commands: Optional[List[str]] = None,
                                     default_chat_dir: str = '',
                                     default_chat_follow_thread: bool = True,
                                     group_name_prefix: Optional[str] = None,
                                     group_dissolve_days: Optional[int] = None) -> Dict[str, Any]:
    """处理 WS 模式授权通过

    生成 auth_token，存入 BindingStore，通过 WS 下发给客户端。

    Args:
        owner_id: 飞书用户 ID
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        client_ip: 客户端 IP
        at_bot_only: 群聊 @bot 过滤
        session_mode: 会话模式，message/thread/group
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀
        group_dissolve_days: 群聊自动解散天数

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    if not request_id:
        logger.warning("[ws_register] Authorization approved but request_id is empty for %s", owner_id)
        return {
            'toast': {'type': 'error', 'content': '请求参数异常'},
        }

    from services.binding_store import BindingStore
    from services.auth_token import generate_auth_token
    from services.ws_registry import WebSocketRegistry
    from config import FEISHU_APP_SECRET

    if not FEISHU_APP_SECRET:
        logger.error("[ws_register] FEISHU_APP_SECRET not configured")
        return {
            'toast': {
                'type': 'error',
                'content': '服务配置错误'
            }
        }

    # 生成 auth_token
    auth_token = generate_auth_token(FEISHU_APP_SECRET, owner_id)
    logger.info("[ws_register] Authorization approved, generated auth_token for %s", owner_id)

    # 通过 WS 下发 auth_token
    # BindingStore 更新延迟到消息循环收到 auth_ok_ack 后执行，
    # 确保客户端已确认收到新 token 后再持久化（与续期路径一致）
    registry = WebSocketRegistry.get_instance()
    if not registry:
        logger.error("[ws_register] WebSocketRegistry not initialized")
        return {
            'toast': {'type': 'error', 'content': '服务内部错误'},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content='服务内部错误，请稍后重试。',
                template='red'
            )
        }

    pending_conn = registry.get_pending(owner_id, request_id)
    if not pending_conn:
        logger.warning("[ws_register] No pending connection for %s, request_id=%s", owner_id, request_id)
        return {
            'toast': {'type': 'error', 'content': '连接已断开或不存在'},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content=(
                    f"**来源 IP**: `{client_ip}`\n\n"
                    f"该终端的连接已断开或不存在。\n\n"
                    f"可能已被其他操作处理，请刷新后重试。"
                ),
                template='yellow'
            )
        }

    try:
        # 原子地设置 auth_token、绑定参数，并关闭其他 pending 连接
        # 防止客户端极快回复 auth_ok_ack 时消息循环找不到 token
        # 同时验证 pending 连接仍然存在（防止 get_pending 与此处之间的竞态)
        # 注意：session_mode 等参数已在注册时存入 pending_binding_params
        # 此处传入的参数会与已有参数合并（不覆盖已有值）
        if not registry.prepare_authorization(owner_id, request_id, auth_token, {
            'client_ip': client_ip,
            'at_bot_only': at_bot_only,
            'session_mode': session_mode,
            'claude_commands': claude_commands,
            'default_chat_dir': default_chat_dir,
            'default_chat_follow_thread': default_chat_follow_thread,
            'group_name_prefix': group_name_prefix,
            'group_dissolve_days': group_dissolve_days
        }):
            logger.warning("[ws_register] prepare_authorization failed: connection gone for %s, request_id=%s", owner_id, request_id)
            return {
                'toast': {'type': 'error', 'content': '连接已断开或不存在'},
                'card': _build_register_status_card(
                    title='⚠️ 授权失败',
                    content=(
                        f"**来源 IP**: `{client_ip}`\n\n"
                        f"该终端的连接已断开或不存在。\n\n"
                        f"可能已被其他操作处理，请刷新后重试。"
                    ),
                    template='yellow'
                )
            }

        ws_send_auth_ok(pending_conn, auth_token)
        logger.info("[ws_register] Sent auth_ok to %s via WS, request_id=%s, waiting for auth_ok_ack", owner_id, request_id)
    except Exception as e:
        logger.error("[ws_register] Failed to send auth_ok: %s", e)
        return {
            'toast': {'type': 'error', 'content': '发送授权信息失败'},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content=(
                    f"**来源 IP**: `{client_ip}`\n\n"
                    f"向客户端发送授权信息失败。\n\n"
                    f"请重新启动后端服务后重试。"
                ),
                template='yellow'
            )
        }

    # 返回已授权卡片（带解绑按钮）
    content = (
        f"**来源 IP**: `{client_ip}`\n\n"
        f"**已授权权限：**\n"
        f"• 接收你发送给机器人的所有消息\n"
        f"• 向你发送消息通知\n\n"
        f"已成功授权 WebSocket 隧道连接"
    )
    return {
        'toast': {
            'type': 'success',
            'content': '已授权绑定'
        },
        'card': _build_register_status_card(
            title='✓ 已授权',
            content=content,
            template='green',
            button={
                'tag': 'button',
                'text': {'tag': 'plain_text', 'content': '解绑'},
                'type': 'danger',
                'value': {
                    'action': 'unbind_register',
                    'mode': 'ws',
                    'callback_url': 'ws://tunnel',
                    'owner_id': owner_id
                }
            }
        )
    }


def handle_ws_authorization_denied(owner_id: str, request_id: str) -> Dict[str, Any]:
    """处理 WS 模式授权拒绝（单个终端）

    通过 WS 发送 auth_error 消息，关闭指定的 pending 连接。

    注意：即使 pending 连接已断开（如超时），仍返回"已拒绝"而非"连接已断开"。
    原因：用户点击拒绝表示不想绑定该终端，无论连接状态如何，"不绑定"这个
    结果都达成了，无需区分内部状态。这与授权通过场景不同——授权通过时
    如果连接断开，用户的期望（绑定成功）无法达成，需要告知失败。

    Args:
        owner_id: 飞书用户 ID
        request_id: 请求 ID（用于指定拒绝哪个终端）

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    if not request_id:
        logger.warning("[ws_register] Authorization denied but request_id is empty for %s", owner_id)
        return {
            'toast': {'type': 'error', 'content': '请求参数异常'},
        }

    from services.ws_registry import WebSocketRegistry

    # 通过 WS 发送 auth_error，然后直接关闭 socket
    # 注意：不能调用 ws_close()，因为消息循环线程正在同一个 socket 上 recv()，
    # 跨线程操作 socket 不安全。直接 close() 会让消息循环收到异常后退出。
    registry = WebSocketRegistry.get_instance()
    if registry:
        pending_conn = registry.get_pending(owner_id, request_id)
        if pending_conn:
            try:
                from services.ws_protocol import ws_send_text
                msg = json.dumps({
                    'type': 'auth_error',
                    'action': 'stop',
                    'message': 'authorization denied'
                })
                ws_send_text(pending_conn, msg)
            except Exception as e:
                logger.debug("[ws_register] Error sending auth_error: %s", e)
            # 直接关闭 socket，消息循环会因 ConnectionError 退出并清理
            try:
                pending_conn.close()
            except Exception:
                pass

        registry.remove_pending(owner_id, request_id)

    logger.info("[ws_register] Authorization denied for %s, request_id=%s", owner_id, request_id)

    return {
        'toast': {
            'type': 'info',
            'content': '已拒绝该终端的连接请求'
        },
        'card': _build_register_status_card(
            title='✗ 已拒绝',
            content="已拒绝该终端的 WebSocket 隧道连接请求",
            template='red'
        )
    }


def handle_ws_register_unbind(owner_id: str) -> Dict[str, Any]:
    """处理 WS 模式用户解绑操作

    关闭 WebSocket 连接并删除绑定记录。

    Args:
        owner_id: 飞书用户 ID

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    from services.binding_store import BindingStore
    from services.ws_registry import WebSocketRegistry

    # 关闭 WebSocket 连接
    # 注意：不能调用 ws_close()，因为消息循环线程正在同一个 socket 上 recv()，
    # ws_close() 会发送 close frame 后阻塞等待响应（最多 2 秒），跨线程不安全。
    # 直接 close() 会让消息循环收到异常后退出并清理。
    registry = WebSocketRegistry.get_instance()
    if registry:
        ws_conn = registry.get(owner_id)
        if ws_conn:
            try:
                from services.ws_protocol import ws_send_text
                msg = json.dumps({'type': 'unbind', 'action': 'stop', 'message': 'user unbind'})
                ws_send_text(ws_conn, msg)
            except Exception as e:
                logger.debug("[ws_register] Error sending unbind: %s", e)
            try:
                ws_conn.close()
            except Exception:
                pass
        registry.unregister(owner_id)

    # 删除绑定记录
    store = BindingStore.get_instance()
    if store:
        existing = store.get(owner_id)
        if existing and existing.get('callback_url', '').startswith('ws'):
            store.delete(owner_id)
            logger.info("[ws_register] WS user unbound: owner_id=%s", owner_id)
        else:
            logger.info("[ws_register] WS user unbind (no existing binding): owner_id=%s", owner_id)

    return {
        'toast': {
            'type': 'info',
            'content': '已解绑'
        },
        'card': _build_register_status_card(
            title='✗ 已解绑',
            content=(
                "已解除 WebSocket 隧道绑定，该后端将：\n"
                "• 无法再接收你的消息\n"
                "• 无法再向你发送通知"
            ),
            template='grey'
        )
    }
