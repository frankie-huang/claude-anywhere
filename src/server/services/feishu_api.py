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
import re
import threading
import time
from typing import Optional, Tuple, Dict, List, Any, Pattern

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

# 飞书敏感信息拦截错误码
# 230022: 消息内容包含敏感信息
# 230028: 消息DLP审查未通过（明文电话号码、邮箱等）
SENSITIVE_CONTENT_CODES = frozenset({230022, 230028})


# =============================================================================
# 敏感数据脱敏
# =============================================================================

# 预编译脱敏正则（模块加载时编译一次）
# 注意：身份证必须在手机号之前匹配，避免18位数字串被11位规则局部命中
_SANITIZE_PATTERNS: List[Tuple[Pattern[str], str]] = [
    # 身份证号：18位（6位地区码 + 8位出生日期 + 3位顺序码 + 1位校验码）
    # 保留前6位和后4位，中间8位用*替代
    # 使用非捕获组 (?:...) 包裹日期部分，只捕获前6位和后4位
    (re.compile(r'(?<!\d)(\d{6})(?:(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(\d{3}[\dXx])(?!\d)'),
     r'\1********\2'),
    # 手机号：1[3-9]开头的11位数字，保留前3后4
    (re.compile(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)'),
     r'\1****\2'),
    # 座机号：区号-号码（010-12345678, 0755-1234567），保留区号和后4位
    (re.compile(r'(?<!\d)(0\d{2,3})-\d{3,4}(\d{4})(?!\d)'),
     r'\1-****\2'),
    # 邮箱地址：@ 替换为 [at]，避免被飞书识别为敏感信息
    (re.compile(r'([a-zA-Z0-9._%+\-]+)@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'),
     r'\1[at]\2'),
]


def _sanitize_text(text: str) -> str:
    """对纯文本中的敏感信息进行脱敏处理

    处理规则（按优先级顺序）:
        - 身份证号（18位，含日期校验）: 110101********1234
        - 手机号（11位，1[3-9]开头）: 138****5678
        - 座机号（区号-号码）: 010-****5678
        - 邮箱地址: user[at]example.com
    """
    for pattern, replacement in _SANITIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# 飞书卡片 JSON 中需要脱敏的文本字段名
_CARD_TEXT_KEYS = frozenset({'content', 'text', 'title', 'subtitle', 'value'})

# 飞书卡片 JSON 中应跳过脱敏的字段名（URL、ID、技术字段）
_CARD_SKIP_KEYS = frozenset({
    'url', 'default_url', 'icon', 'icon_key', 'image_key', 'img_key',
    'avatar', 'avatar_key',
    'tag', 'callback_id', 'action', 'name', 'key', 'token',
    'multi_url', 'pc_url', 'ios_url', 'android_url', 'fallback_url',
    'forward_url', 'open_url', 'template_id',
})


def _sanitize_obj(obj: Any) -> Any:
    """递归脱敏 JSON 对象中的文本字段

    策略：白名单脱敏 + 黑名单跳过 + 其余只递归不脱敏
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in _CARD_SKIP_KEYS:
                # 技术字段，原样保留（不递归子节点）
                result[k] = v
            elif k in _CARD_TEXT_KEYS and isinstance(v, str):
                # 用户可见文本字段，脱敏
                result[k] = _sanitize_text(v)
            else:
                # 其余字段：递归子结构（但不脱敏字符串值本身）
                result[k] = _sanitize_obj(v)
        return result
    elif isinstance(obj, list):
        return [_sanitize_obj(item) for item in obj]
    return obj


def _sanitize_content(content: Any) -> Any:
    """统一脱敏入口，自动处理 dict/str/list"""
    if isinstance(content, dict):
        return _sanitize_obj(content)
    elif isinstance(content, list):
        return [_sanitize_obj(item) for item in content]
    elif isinstance(content, str):
        # 字符串可能是 JSON 或纯文本
        try:
            data = json.loads(content)
            return _sanitize_obj(data)
        except (json.JSONDecodeError, ValueError):
            return _sanitize_text(content)
    return content


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

    def _send_with_retry(
        self,
        url: str,
        msg_type: str,
        content: Any,
        payload_extra: Optional[Dict[str, Any]] = None,
        log_prefix: str = "Message"
    ) -> Tuple[bool, str]:
        """通用发送方法，支持敏感内容自动脱敏重试

        Args:
            url: 请求 URL
            msg_type: 消息类型 (interactive / text)
            content: 消息内容 (dict)
            payload_extra: 额外的 payload 字段
            log_prefix: 日志前缀 (Message / Text)

        Returns:
            (success, message_id or error)
        """
        token = self._token_manager.get_token()
        if not token:
            return False, "Failed to get access token"

        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}'
        }

        # 第一次尝试：使用原始内容
        content_str = json.dumps(content, ensure_ascii=False)
        payload = {'msg_type': msg_type, 'content': content_str}
        if payload_extra:
            payload.update(payload_extra)

        success, resp = _http_request(
            url,
            method='POST',
            headers=headers,
            data=json.dumps(payload).encode('utf-8')
        )
        code = resp.get('code', -1)

        # 第一次发送成功，直接返回
        if success and code == 0:
            message_id = resp.get('data', {}).get('message_id', '')
            logger.info(f"[feishu-api] {log_prefix} sent: {message_id}")
            return True, message_id

        # 如果是敏感信息拦截错误，脱敏后重试一次
        if code in SENSITIVE_CONTENT_CODES:
            logger.info(f"[feishu-api] Sensitive content detected (code={code}, {msg_type}), sanitizing and retrying...")
            sanitized = _sanitize_content(content)
            content_str = json.dumps(sanitized, ensure_ascii=False)
            if content != sanitized:
                logger.debug("[sanitize] before=%s", json.dumps(content, ensure_ascii=False))
                logger.debug("[sanitize] after =%s", content_str)
            payload['content'] = content_str

            success, resp = _http_request(
                url,
                method='POST',
                headers=headers,
                data=json.dumps(payload).encode('utf-8')
            )
            code = resp.get('code', -1)

        if not success:
            return False, str(resp.get('error', 'Unknown error'))

        if code == 0:
            message_id = resp.get('data', {}).get('message_id', '')
            logger.info(f"[feishu-api] {log_prefix} sent after sanitization: {message_id}")
            return True, message_id

        # 返回失败信息
        error_msg = resp.get('msg', 'Unknown error')
        logger.error(f"[feishu-api] {log_prefix} send failed: code={code}, msg={error_msg}")
        return False, f"API error: {error_msg}"

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
        if not receive_id_type:
            receive_id_type = detect_receive_id_type(receive_id)

        if not receive_id:
            return False, "No receive_id specified"

        try:
            card_data = json.loads(card_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid card JSON: {e}"

        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
        return self._send_with_retry(
            url=url,
            msg_type='interactive',
            content=card_data,
            payload_extra={'receive_id': receive_id},
            log_prefix='Card'
        )

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

        try:
            card_data = json.loads(card_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid card JSON: {e}"

        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply"
        payload_extra = {'reply_in_thread': True} if reply_in_thread else None
        return self._send_with_retry(
            url=url,
            msg_type='interactive',
            content=card_data,
            payload_extra=payload_extra,
            log_prefix='Card reply'
        )

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
        if not receive_id_type:
            receive_id_type = detect_receive_id_type(receive_id)

        if not receive_id:
            return False, "No receive_id specified"

        url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
        return self._send_with_retry(
            url=url,
            msg_type='text',
            content={'text': text},
            payload_extra={'receive_id': receive_id},
            log_prefix='Text'
        )

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

        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply"
        payload_extra = {'reply_in_thread': True} if reply_in_thread else None
        return self._send_with_retry(
            url=url,
            msg_type='text',
            content={'text': text},
            payload_extra=payload_extra,
            log_prefix='Text reply'
        )

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

    _instance: Optional['FeishuAPIService'] = None
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
