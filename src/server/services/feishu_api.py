"""
Feishu OpenAPI Service - 飞书开放平台 API 服务

功能:
    - TokenManager: access_token 获取与缓存（2小时有效期，提前5分钟刷新）
    - MessageSender: 消息发送（支持卡片和文本）
    - FeishuAPIService: 服务入口（单例模式）
    - detect_receive_id_type(): 根据 ID 前缀自动检测类型
"""

import json
import logging
import threading
import time
from typing import Optional, Tuple, Dict, List, Any

from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import URLError, HTTPError

from config import (
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
)

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

# 飞书 API 基础 URL
FEISHU_API_BASE = 'https://open.feishu.cn/open-apis'

# Token 有效期（秒），飞书默认 2 小时
TOKEN_EXPIRE_SECONDS = 7200

# 提前刷新时间（秒），避免临界点过期
TOKEN_REFRESH_BUFFER = 300  # 5 分钟

# HTTP 请求超时（秒）
HTTP_TIMEOUT = 10


# =============================================================================
# 工具函数
# =============================================================================

def detect_receive_id_type(receive_id: str) -> str:
    """根据 ID 前缀自动检测接收者类型

    Args:
        receive_id: 接收者 ID

    Returns:
        接收者类型: open_id, user_id, chat_id, union_id, email
    """
    if not receive_id:
        return ''

    # open_id: ou_ 开头
    if receive_id.startswith('ou_'):
        return 'open_id'

    # chat_id: oc_ 开头
    if receive_id.startswith('oc_'):
        return 'chat_id'

    # union_id: on_ 开头
    if receive_id.startswith('on_'):
        return 'union_id'

    # email: 包含 @
    if '@' in receive_id:
        return 'email'

    # 默认为 user_id
    return 'user_id'


def _http_request(
    url: str,
    method: str = 'GET',
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = HTTP_TIMEOUT
) -> Tuple[bool, Dict[str, Any]]:
    """发送 HTTP 请求

    Args:
        url: 请求 URL
        method: HTTP 方法
        headers: 请求头
        data: 请求体（bytes）
        timeout: 超时秒数

    Returns:
        (success, response_dict)
    """
    if headers is None:
        headers = {}

    try:
        req = Request(url, data=data, headers=headers, method=method)
        # 创建无代理的 opener（飞书 API 直连）
        no_proxy_handler = ProxyHandler({})
        opener = build_opener(no_proxy_handler)
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
            return True, json.loads(body)
    except HTTPError as e:
        try:
            body = e.read().decode('utf-8')
            error_data = json.loads(body)
        except Exception:
            error_data = {'error': str(e), 'code': e.code}
        logger.error(f"[feishu-api] HTTP error {e.code}: {error_data}")
        return False, error_data
    except URLError as e:
        logger.error(f"[feishu-api] URL error: {e.reason}")
        return False, {'error': str(e.reason)}
    except Exception as e:
        logger.error(f"[feishu-api] Request error: {e}")
        return False, {'error': str(e)}


# =============================================================================
# TokenManager
# =============================================================================

class TokenManager:
    """飞书 access_token 管理器

    功能:
        - 获取 tenant_access_token
        - 自动缓存，2小时有效期，提前5分钟刷新
        - 线程安全
    """

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = ''
        self._expire_time = 0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        """获取有效的 access_token

        Returns:
            access_token，失败返回空字符串
        """
        with self._lock:
            # 检查是否需要刷新
            now = time.time()
            if self._token and now < self._expire_time - TOKEN_REFRESH_BUFFER:
                return self._token

            # 需要刷新
            logger.info("[feishu-api] Refreshing access token...")
            success, token, expire_in = self._fetch_token()
            if success:
                self._token = token
                self._expire_time = now + expire_in
                logger.info(f"[feishu-api] Token refreshed, expires in {expire_in}s")
                return self._token
            else:
                logger.error("[feishu-api] Failed to refresh token")
                return ''

    def _fetch_token(self) -> Tuple[bool, str, int]:
        """从飞书 API 获取 token

        Returns:
            (success, token, expire_in_seconds)
        """
        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        payload = json.dumps({
            'app_id': self._app_id,
            'app_secret': self._app_secret
        }).encode('utf-8')

        success, resp = _http_request(url, method='POST', headers=headers, data=payload)
        if not success:
            return False, '', 0

        code = resp.get('code', -1)
        if code != 0:
            logger.error(f"[feishu-api] Token API error: code={code}, msg={resp.get('msg')}")
            return False, '', 0

        token = resp.get('tenant_access_token', '')
        expire = resp.get('expire', TOKEN_EXPIRE_SECONDS)
        return True, token, expire

    def invalidate(self):
        """使当前 token 失效，强制下次刷新"""
        with self._lock:
            self._token = ''
            self._expire_time = 0


# =============================================================================
# MessageSender
# =============================================================================

class MessageSender:
    """飞书消息发送器

    功能:
        - 发送卡片消息
        - 发送文本消息
        - 回复卡片消息
        - 回复文本消息
        - 支持指定接收者
    """

    def __init__(self, token_manager: TokenManager):
        self._token_manager = token_manager

    def send_card(
        self,
        card_json: str,
        receive_id: Optional[str] = None,
        receive_id_type: Optional[str] = None
    ) -> Tuple[bool, str]:
        """发送卡片消息（新消息）

        Args:
            card_json: 卡片 JSON 字符串
            receive_id: 接收者 ID（必需）
            receive_id_type: 接收者类型，默认自动检测

        Returns:
            (success, message_id or error)
        """
        # 自动检测类型
        if not receive_id_type:
            receive_id_type = detect_receive_id_type(receive_id)

        if not receive_id:
            return False, "No receive_id specified"

        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        # 解析卡片 JSON
        try:
            card_data = json.loads(card_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid card JSON: {e}"

        # 构建请求
        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}'
        }

        payload = {
            'receive_id': receive_id,
            'msg_type': 'interactive',
            'content': json.dumps(card_data, ensure_ascii=False)
        }

        success, resp = _http_request(
            url,
            method='POST',
            headers=headers,
            data=json.dumps(payload).encode('utf-8')
        )

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Send message failed: code={code}, msg={error_msg}")
            return False, f"API error: {error_msg}"

        message_id = resp.get('data', {}).get('message_id', '')
        logger.info(f"[feishu-api] Message sent: {message_id}")
        return True, message_id

    def reply_card(
        self,
        card_json: str,
        message_id: str,
        reply_in_thread: bool = False
    ) -> Tuple[bool, str]:
        """回复卡片消息

        使用飞书回复消息 API: POST /open-apis/im/v1/messages/:message_id/reply

        Args:
            card_json: 卡片 JSON 字符串
            message_id: 要回复的消息 ID（必需）
            reply_in_thread: 是否收进话题详情（True 时消息仅出现在话题中，不刷群聊主界面）

        Returns:
            (success, new_message_id or error)
        """
        if not message_id:
            return False, "No message_id specified for reply"

        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        # 解析卡片 JSON
        try:
            card_data = json.loads(card_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid card JSON: {e}"

        # 使用回复消息 API
        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply"
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}'
        }

        payload = {
            'msg_type': 'interactive',
            'content': json.dumps(card_data, ensure_ascii=False)
        }
        if reply_in_thread:
            payload['reply_in_thread'] = True

        success, resp = _http_request(
            url,
            method='POST',
            headers=headers,
            data=json.dumps(payload).encode('utf-8')
        )

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Reply message failed: code={code}, msg={error_msg}")
            return False, f"API error: {error_msg}"

        new_message_id = resp.get('data', {}).get('message_id', '')
        logger.info(f"[feishu-api] Message replied: {new_message_id}, parent_id={message_id}, in_thread={reply_in_thread}")
        return True, new_message_id

    def send_text(
        self,
        text: str,
        receive_id: Optional[str] = None,
        receive_id_type: Optional[str] = None
    ) -> Tuple[bool, str]:
        """发送文本消息（新消息）

        Args:
            text: 文本内容
            receive_id: 接收者 ID（必需）
            receive_id_type: 接收者类型，默认自动检测

        Returns:
            (success, message_id or error)
        """
        # 自动检测类型
        if not receive_id_type:
            receive_id_type = detect_receive_id_type(receive_id)

        if not receive_id:
            return False, "No receive_id specified"

        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}'
        }

        payload = {
            'receive_id': receive_id,
            'msg_type': 'text',
            'content': json.dumps({'text': text})
        }

        success, resp = _http_request(
            url,
            method='POST',
            headers=headers,
            data=json.dumps(payload).encode('utf-8')
        )

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Send text failed: code={code}, msg={error_msg}")
            return False, f"API error: {error_msg}"

        message_id = resp.get('data', {}).get('message_id', '')
        logger.info(f"[feishu-api] Text sent: {message_id}")
        return True, message_id

    def reply_text(
        self,
        text: str,
        message_id: str,
        reply_in_thread: bool = False
    ) -> Tuple[bool, str]:
        """回复文本消息

        使用飞书回复消息 API: POST /open-apis/im/v1/messages/:message_id/reply

        Args:
            text: 文本内容
            message_id: 要回复的消息 ID（必需）
            reply_in_thread: 是否收进话题详情（True 时消息仅出现在话题中，不刷群聊主界面）

        Returns:
            (success, new_message_id or error)
        """
        if not message_id:
            return False, "No message_id specified for reply"

        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        # 使用回复消息 API
        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply"
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}'
        }

        payload = {
            'msg_type': 'text',
            'content': json.dumps({'text': text})
        }
        if reply_in_thread:
            payload['reply_in_thread'] = True

        success, resp = _http_request(
            url,
            method='POST',
            headers=headers,
            data=json.dumps(payload).encode('utf-8')
        )

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Reply text failed: code={code}, msg={error_msg}")
            return False, f"API error: {error_msg}"

        new_message_id = resp.get('data', {}).get('message_id', '')
        logger.info(f"[feishu-api] Text replied: {new_message_id}, parent_id={message_id}, in_thread={reply_in_thread}")
        return True, new_message_id

    def add_reaction(
        self,
        message_id: str,
        emoji_type: str
    ) -> Tuple[bool, str]:
        """给消息添加表情回应

        飞书 API: POST /open-apis/im/v1/messages/:message_id/reactions
        所需权限: im:message.reactions:write_only

        Args:
            message_id: 消息 ID
            emoji_type: 表情类型，如 "OK"、"THUMBSUP" 等
                        完整列表见 https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce

        Returns:
            (success, reaction_id or error)
        """
        if not message_id:
            return False, "No message_id specified"

        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions"
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}'
        }

        payload = {
            'reaction_type': {
                'emoji_type': emoji_type
            }
        }

        success, resp = _http_request(
            url,
            method='POST',
            headers=headers,
            data=json.dumps(payload).encode('utf-8')
        )

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Add reaction failed: code={code}, msg={error_msg}")
            return False, f"API error: {error_msg}"

        reaction_id = resp.get('data', {}).get('reaction_id', '')
        logger.info(f"[feishu-api] Reaction added: {reaction_id}, message_id={message_id}, emoji={emoji_type}")
        return True, reaction_id

    def get_reactions(
        self,
        message_id: str,
        emoji_type: str = ''
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """查询消息的表情回应列表

        飞书 API: GET /open-apis/im/v1/messages/:message_id/reactions
        所需权限: im:message.reactions:read

        Args:
            message_id: 消息 ID
            emoji_type: 表情类型过滤，为空则返回所有表情

        Returns:
            (success, reactions_list)，失败时返回空列表
            reactions_list 中每项包含 reaction_id、reaction_type.emoji_type 等
        """
        if not message_id:
            return False, []

        token = self._token_manager.get_token()
        if not token:
            return False, []

        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions"
        if emoji_type:
            from urllib.parse import quote
            url += f"?reaction_type={quote(emoji_type)}"
        headers = {
            'Authorization': f'Bearer {token}'
        }

        success, resp = _http_request(url, method='GET', headers=headers)

        if not success:
            return False, []

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Get reactions failed: code={code}, msg={error_msg}")
            return False, []

        items = resp.get('data', {}).get('items', [])
        return True, items

    def delete_reaction(
        self,
        message_id: str,
        reaction_id: str
    ) -> Tuple[bool, str]:
        """删除消息的表情回应

        飞书 API: DELETE /open-apis/im/v1/messages/:message_id/reactions/:reaction_id
        所需权限: im:message.reactions:write_only（只能删除自己添加的表情）

        Args:
            message_id: 消息 ID
            reaction_id: 表情回应 ID

        Returns:
            (success, error_msg)
        """
        if not message_id or not reaction_id:
            return False, "Missing message_id or reaction_id"

        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions/{reaction_id}"
        headers = {
            'Authorization': f'Bearer {token}'
        }

        success, resp = _http_request(url, method='DELETE', headers=headers)

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        code = resp.get('code', -1)
        if code != 0:
            error_msg = resp.get('msg', 'Unknown error')
            logger.error(f"[feishu-api] Delete reaction failed: code={code}, msg={error_msg}")
            return False, f"API error: {error_msg}"

        logger.info(f"[feishu-api] Reaction deleted: {reaction_id}, message_id={message_id}")
        return True, ''

    def remove_reaction(
        self,
        message_id: str,
        emoji_type: str
    ) -> Tuple[bool, int]:
        """查询并删除消息上指定类型的表情回应（便捷方法）

        先查询消息上的表情列表，过滤出指定类型，再逐个删除。
        只能删除机器人自己添加的表情（飞书 API 限制）。

        Args:
            message_id: 消息 ID
            emoji_type: 要删除的表情类型，如 "Typing"

        Returns:
            (success, deleted_count)
        """
        if not message_id:
            return False, 0

        ok, items = self.get_reactions(message_id, emoji_type)
        if not ok or not items:
            return False, 0

        deleted = 0
        for item in items:
            rid = item.get('reaction_id', '')
            if rid:
                success, _ = self.delete_reaction(message_id, rid)
                if success:
                    deleted += 1

        return deleted > 0, deleted


# =============================================================================
# FeishuAPIService
# =============================================================================

class FeishuAPIService:
    """飞书 API 服务（单例模式）

    使用方式:
        service = FeishuAPIService.get_instance()
        success, result = service.send_card(card_json)
    """

    _instance = None  # type: Optional[FeishuAPIService]
    _lock = threading.Lock()

    def __init__(self, app_id: str, app_secret: str):
        """初始化服务

        Args:
            app_id: 飞书应用 App ID
            app_secret: 飞书应用 App Secret
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._enabled = bool(app_id and app_secret)

        if self._enabled:
            self._token_manager = TokenManager(app_id, app_secret)
            self._message_sender = MessageSender(self._token_manager)
            logger.info("[feishu-api] Service initialized")
        else:
            self._token_manager = None
            self._message_sender = None
            logger.warning("[feishu-api] Service disabled (missing app_id or app_secret)")

    @classmethod
    def initialize(cls, app_id: str = '', app_secret: str = '') -> 'FeishuAPIService':
        """初始化单例实例

        Args:
            app_id: 飞书应用 App ID，默认从配置读取
            app_secret: 飞书应用 App Secret，默认从配置读取

        Returns:
            FeishuAPIService 实例
        """
        with cls._lock:
            if cls._instance is None:
                aid = app_id or FEISHU_APP_ID
                asecret = app_secret or FEISHU_APP_SECRET
                cls._instance = cls(aid, asecret)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['FeishuAPIService']:
        """获取单例实例

        Returns:
            FeishuAPIService 实例，未初始化返回 None
        """
        return cls._instance

    @property
    def enabled(self) -> bool:
        """服务是否启用"""
        return self._enabled

    def send_card(
        self,
        card_json: str,
        receive_id: Optional[str] = None,
        receive_id_type: Optional[str] = None
    ) -> Tuple[bool, str]:
        """发送卡片消息（新消息）

        Args:
            card_json: 卡片 JSON 字符串
            receive_id: 接收者 ID
            receive_id_type: 接收者类型

        Returns:
            (success, message_id or error)
        """
        if not self._enabled:
            return False, "Feishu API service not enabled"

        return self._message_sender.send_card(card_json, receive_id, receive_id_type)

    def reply_card(
        self,
        card_json: str,
        message_id: str,
        reply_in_thread: bool = False
    ) -> Tuple[bool, str]:
        """回复卡片消息

        使用飞书回复消息 API

        Args:
            card_json: 卡片 JSON 字符串
            message_id: 要回复的消息 ID
            reply_in_thread: 是否收进话题详情

        Returns:
            (success, new_message_id or error)
        """
        if not self._enabled:
            return False, "Feishu API service not enabled"

        return self._message_sender.reply_card(card_json, message_id, reply_in_thread)

    def send_text(
        self,
        text: str,
        receive_id: Optional[str] = None,
        receive_id_type: Optional[str] = None
    ) -> Tuple[bool, str]:
        """发送文本消息（新消息）

        Args:
            text: 文本内容
            receive_id: 接收者 ID
            receive_id_type: 接收者类型

        Returns:
            (success, message_id or error)
        """
        if not self._enabled:
            return False, "Feishu API service not enabled"

        return self._message_sender.send_text(text, receive_id, receive_id_type)

    def reply_text(
        self,
        text: str,
        message_id: str,
        reply_in_thread: bool = False
    ) -> Tuple[bool, str]:
        """回复文本消息

        使用飞书回复消息 API

        Args:
            text: 文本内容
            message_id: 要回复的消息 ID
            reply_in_thread: 是否收进话题详情

        Returns:
            (success, new_message_id or error)
        """
        if not self._enabled:
            return False, "Feishu API service not enabled"

        return self._message_sender.reply_text(text, message_id, reply_in_thread)

    def add_reaction(
        self,
        message_id: str,
        emoji_type: str
    ) -> Tuple[bool, str]:
        """给消息添加表情回应

        飞书 API: POST /open-apis/im/v1/messages/:message_id/reactions
        所需权限: im:message.reactions:write_only

        Args:
            message_id: 消息 ID
            emoji_type: 表情类型，如 "OK"、"THUMBSUP" 等
                        完整列表见 https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce

        Returns:
            (success, reaction_id or error)
        """
        if not self._enabled:
            return False, "Feishu API service not enabled"

        return self._message_sender.add_reaction(message_id, emoji_type)

    def remove_reaction(
        self,
        message_id: str,
        emoji_type: str
    ) -> Tuple[bool, int]:
        """查询并删除消息上指定类型的表情回应

        Args:
            message_id: 消息 ID
            emoji_type: 要删除的表情类型，如 "Typing"

        Returns:
            (success, deleted_count)
        """
        if not self._enabled:
            return False, 0

        return self._message_sender.remove_reaction(message_id, emoji_type)
