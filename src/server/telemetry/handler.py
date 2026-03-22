"""遥测服务端 HTTP 端点

接收客户端心跳上报，返回最新版本信息。

端点：POST /api/telemetry/heartbeat

功能：
- 记录客户端心跳
- client_id 格式校验（必须是有效的 UUID v4）
- 速率限制（同一 client_id 每 10 分钟只能上报一次）
- IP 限流（同一 IP 每 10 分钟最多 10 次请求）
- 返回最新版本信息（用于版本更新通知）
"""

import logging
from typing import Any, Dict, Optional, Tuple

from services.auth_token import check_global_auth_token
from telemetry.store import TelemetryStore
from telemetry.utils import get_version, is_version_newer, validate_uuid_v4

logger = logging.getLogger(__name__)

# 服务端版本缓存（模块级，避免重复读取）
_SERVER_VERSION: Optional[str] = None


def _get_client_ip(headers: Dict[str, str]) -> str:
    """从请求头获取真实客户端 IP

    直接使用 http_handler.py 注入的 X-Real-IP（已综合 X-Forwarded-For 和 socket 地址）。
    不再独立解析 X-Forwarded-For，避免客户端伪造该头绕过 IP 限流。

    Args:
        headers: HTTP 请求头字典

    Returns:
        客户端 IP 地址
    """
    return headers.get('X-Real-IP', '').strip()


def _get_server_version() -> str:
    """获取服务端版本"""
    global _SERVER_VERSION
    if _SERVER_VERSION is None:
        _SERVER_VERSION = get_version()
    return _SERVER_VERSION


def handle_heartbeat(body: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """处理心跳上报

    Args:
        body: 请求体
        headers: 请求头

    Returns:
        (status_code, response_body)
    """
    client_id = body.get('client_id', '')
    version = body.get('version', 'unknown')
    os_name = body.get('os', 'unknown')
    repo_url = body.get('repo_url', '')
    timestamp = body.get('timestamp')

    # 验证必要字段
    if not client_id:
        logger.warning("[telemetry] Missing client_id")
        return 400, {
            'success': False,
            'error': 'Missing client_id'
        }

    # 验证 client_id 格式（必须是有效的 UUID v4）
    if not validate_uuid_v4(client_id):
        logger.warning("[telemetry] Invalid client_id format: %s", client_id[:20])
        return 400, {
            'success': False,
            'error': 'Invalid client_id format, must be UUID v4'
        }

    # 获取客户端 IP
    client_ip = _get_client_ip(headers)

    # 记录心跳（包含 IP 限流和 client_id 限流）
    store = TelemetryStore.get_instance()
    error_code, error_msg = store.record_heartbeat(client_id, version, os_name, repo_url, timestamp, client_ip)

    if error_code == 'bad_request':
        return 400, {
            'success': False,
            'error': error_msg or 'Bad request'
        }

    if error_code == 'rate_limited':
        logger.debug("[telemetry] Rate limited: %s", client_id[:20])
        return 429, {
            'success': False,
            'error': error_msg or 'Rate limited'
        }

    # 获取服务端最新版本
    server_version = _get_server_version()

    # 使用语义化版本比较
    update_available = False
    if version and server_version and version != 'unknown' and server_version != 'unknown':
        if is_version_newer(server_version, version):
            update_available = True

    logger.debug(
        "[telemetry] Heartbeat recorded: client_id=%s, version=%s, os=%s",
        client_id[:20], version, os_name
    )

    return 200, {
        'success': True,
        'latest_version': server_version,
        'update_available': update_available,
        'message': 'New version available, please run git pull to update' if update_available else ''
    }


def handle_stats(_body: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """获取统计信息（需认证）

    Args:
        _body: 请求体（未使用）
        headers: 请求头

    Returns:
        (status_code, response_body)
    """
    # 验证 X-Auth-Token
    if not check_global_auth_token(headers, '/api/telemetry/stats'):
        return 401, {
            'success': False,
            'error': 'Unauthorized'
        }

    store = TelemetryStore.get_instance()
    stats = store.get_stats()

    return 200, {
        'success': True,
        'data': stats
    }


# 路由注册（供 main.py 使用）
TELEMETRY_ROUTES = {
    '/api/telemetry/heartbeat': handle_heartbeat,
    '/api/telemetry/stats': handle_stats,
}
