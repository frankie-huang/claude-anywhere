"""auth_token 生成与验证模块

使用 HMAC-SHA256 算法生成和验证 auth_token。

Token 格式: base64url(timestamp) + "." + base64url(signature)
- timestamp: Unix 时间戳（秒）
- signature: HMAC-SHA256(FEISHU_APP_SECRET, owner_id + timestamp)
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Python 3.6 兼容的 base64url 编码
def _base64url_encode(data: bytes) -> str:
    """Base64 URL 安全编码（无 padding）

    Args:
        data: 原始字节数据

    Returns:
        编码后的字符串（去除 = padding）
    """
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def _base64url_decode(data: str) -> bytes:
    """Base64 URL 安全解码（支持无 padding）

    Args:
        data: 编码后的字符串

    Returns:
        原始字节数据
    """
    # 添加 padding 以符合标准 base64 格式
    padding = 4 - len(data) % 4
    if padding != 4:
        data += '=' * padding
    return base64.urlsafe_b64decode(data)


def generate_auth_token(app_secret: str, owner_id: str) -> str:
    """生成 auth_token

    Args:
        app_secret: 飞书应用密钥（FEISHU_APP_SECRET）
        owner_id: 飞书用户 ID

    Returns:
        auth_token 字符串，格式为 {timestamp_b64}.{signature_b64}
    """
    timestamp = str(int(time.time()))
    message = owner_id + timestamp

    # 计算 HMAC-SHA256 签名
    signature = hmac.new(
        app_secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()

    # Base64 URL 安全编码
    timestamp_b64 = _base64url_encode(timestamp.encode('utf-8'))
    signature_b64 = _base64url_encode(signature)

    token = f"{timestamp_b64}.{signature_b64}"
    logger.debug(f"[auth_token] Generated token for {owner_id}: {timestamp_b64}.*")

    return token


def verify_auth_token(
    auth_token: str,
    owner_id: str,
    app_secret: str
) -> Tuple[bool, Optional[int]]:
    """验证 auth_token

    Args:
        auth_token: 待验证的 token
        owner_id: 飞书用户 ID
        app_secret: 飞书应用密钥（FEISHU_APP_SECRET）

    Returns:
        (is_valid, timestamp): 验证结果和 token 中的时间戳
        - is_valid: True 表示验证通过
        - timestamp: token 生成时的 Unix 时间戳（秒），验证失败时为 None
    """
    if not auth_token:
        logger.warning("[auth_token] Empty token")
        return False, None

    try:
        parts = auth_token.split('.')
        if len(parts) != 2:
            logger.warning("[auth_token] Invalid token format")
            return False, None

        timestamp_b64, signature_b64 = parts

        # 解码 timestamp
        timestamp_bytes = _base64url_decode(timestamp_b64)
        timestamp = int(timestamp_bytes.decode('utf-8'))

        # 解码收到的签名
        received_signature = _base64url_decode(signature_b64)

        # 重新计算签名
        message = owner_id + str(timestamp)
        expected_signature = hmac.new(
            app_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).digest()

        # 恒定时间比较，防止时序攻击
        is_valid = hmac.compare_digest(received_signature, expected_signature)

        if is_valid:
            logger.debug(f"[auth_token] Token valid for {owner_id}, timestamp={timestamp}")
        else:
            logger.warning(f"[auth_token] Token invalid for {owner_id}")

        return is_valid, timestamp if is_valid else None

    except Exception as e:
        logger.error(f"[auth_token] Verification error: {e}")
        return False, None


# ============================================================================
# HTTP 请求鉴权辅助函数（供 callback.py 等模块复用）
# ============================================================================

def check_global_auth_token(headers: Any, endpoint_name: str) -> bool:
    """纯鉴权检查，返回是否通过

    从 AuthTokenStore 获取存储的 token，与 headers 中的 X-Auth-Token 进行比对。

    Args:
        headers: 请求头（支持 dict 或 HTTPMessage 等 dict-like 对象）
        endpoint_name: 端点名称（用于日志记录）

    Returns:
        True 表示验证通过，False 表示验证失败
    """
    from services.auth_token_store import AuthTokenStore

    client_token = headers.get('X-Auth-Token', '') if headers else ''
    stored_token = ''
    token_store = AuthTokenStore.get_instance()
    if token_store:
        stored_token = token_store.get()

    if not client_token or not hmac.compare_digest(client_token, stored_token):
        logger.warning("[%s] Missing or invalid X-Auth-Token", endpoint_name)
        return False

    return True


def verify_owner_based_auth_token(
    handler,
    data: dict,
    endpoint_name: str
) -> Optional[Dict[str, Any]]:
    """验证基于 owner_id 的 AuthToken（用于需要绑定关系的接口）

    从请求 body 中获取 owner_id，从 BindingStore 查找对应的 auth_token 进行比对。
    验证成功返回 binding，失败则发送 401 响应并返回 None。

    Args:
        handler: BaseHTTPRequestHandler 实例（用于访问 headers 和发送响应）
        data: 请求 body 解析后的字典
        endpoint_name: 端点名称（用于日志记录）

    Returns:
        binding: 验证成功返回 binding 字典，失败返回 None（已发送响应）
    """
    from services.binding_store import BindingStore

    # 从请求 body 中获取 owner_id
    owner_id = data.get('owner_id', '')
    if not owner_id:
        logger.warning(f"[{endpoint_name}] Missing owner_id in request body")
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'success': False, 'error': 'Missing owner_id'}).encode())
        return None

    # 从 header 中获取 auth_token
    client_auth_token = handler.headers.get('X-Auth-Token', '')
    if not client_auth_token:
        logger.warning(f"[{endpoint_name}] Missing X-Auth-Token header")
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'success': False, 'error': 'Missing X-Auth-Token'}).encode())
        return None

    # 从 BindingStore 中查找该 owner_id 对应的 auth_token
    binding_store = BindingStore.get_instance()
    if not binding_store:
        logger.warning(f"[{endpoint_name}] BindingStore not initialized")
        handler.send_response(500)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'success': False, 'error': 'Server not ready'}).encode())
        return None

    binding = binding_store.get(owner_id)
    if not binding:
        logger.warning(f"[{endpoint_name}] No binding found for owner_id={owner_id}")
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'success': False, 'error': 'Owner not registered'}).encode())
        return None

    # 比对 auth_token（恒定时间比较，防止时序攻击）
    stored_auth_token = binding.get('auth_token', '')
    is_valid = hmac.compare_digest(client_auth_token, stored_auth_token)

    if not is_valid:
        logger.warning(f"[{endpoint_name}] Invalid X-Auth-Token for owner_id={owner_id}")
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'success': False, 'error': 'Invalid X-Auth-Token'}).encode())
        return None

    return binding
