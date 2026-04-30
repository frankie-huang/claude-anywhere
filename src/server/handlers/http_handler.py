"""
HTTP Handler - HTTP 请求处理器

处理权限回调请求（GET）和 POST 路由分发

GET 路由：
    - /ws/tunnel: WebSocket 隧道入口点
    - /status: 服务状态
    - /allow, /always, /deny, /interrupt: 权限决策回调

POST 路由：
    - /gw/*: 飞书网关侧路由
        - /gw/register: Callback 后端注册
        - /gw/feishu/send: 发送飞书消息
        - /gw/feishu/create-group: 创建飞书群聊
    - /cb/*: Callback 后端侧路由（通过路由表分发）
"""

import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from services.auth_token import verify_owner_based_auth_token
from handlers.feishu import (handle_feishu_request, handle_send_message,
                             handle_create_group)
from handlers.register import handle_register_request
from handlers.utils import send_json, send_html_response
from handlers.ws_handler import handle_ws_tunnel
from handlers.callback import (
    handle_status,
    handle_action,
    BACKEND_ROUTES,
)

# 单次请求最大大小限制
MAX_REQUEST_SIZE = 10 * 1024 * 1024  # 10MB

logger = logging.getLogger(__name__)


class HttpRequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器 - 路由分发入口

    职责：解析请求、路由分发。
    业务逻辑委托给对应的 handler 模块：
        - Callback 后端路由 → handlers.callback
        - 飞书网关路由 → handlers.register / handlers.feishu
    """

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    # 可信代理 IP 列表：只有来自这些地址的请求才信任 X-Forwarded-For
    # 对应部署架构：Nginx (127.0.0.1/::1) → Python Server
    #
    # Nginx 必须用 $remote_addr 覆写（非追加），防止客户端伪造：
    #   proxy_set_header X-Forwarded-For $remote_addr;
    # 注意：不要用 $proxy_add_x_forwarded_for，它会保留客户端传入的伪造值
    _TRUSTED_PROXIES = frozenset(['127.0.0.1', '::1'])

    def get_client_ip(self) -> str:
        """获取真实客户端 IP

        安全策略：仅当请求来自可信代理时才信任 X-Forwarded-For，
        防止外部客户端直连公网端口时伪造该头绕过 IP 限流。

        Returns:
            客户端 IP 地址
        """
        socket_ip = self.client_address[0] if self.client_address else ''

        # 仅信任来自本机代理（Nginx）的 X-Forwarded-For
        if socket_ip in self._TRUSTED_PROXIES:
            forwarded_for = self.headers.get('X-Forwarded-For', '')
            if forwarded_for:
                # 取第一个 IP（Nginx 应配置为 $remote_addr 覆写，不是追加）
                client_ip = forwarded_for.split(',')[0].strip()
                if client_ip:
                    return client_ip

        # 非可信代理 或 无 X-Forwarded-For：直接用 socket 地址（不可伪造）
        return socket_ip

    # GET 路由: action → 路由处理函数
    ACTION_ROUTES = frozenset(['allow', 'always', 'deny', 'interrupt'])

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ===== WebSocket 隧道路由 =====
        if path == '/ws/tunnel':
            handle_ws_tunnel(self, params)
            return

        # ===== Callback 后端侧路由 =====
        if path == '/status':
            handle_status(self)
            return

        # /allow, /always, /deny, /interrupt - 权限决策回调
        action = path.lstrip('/')
        if action in self.ACTION_ROUTES:
            request_id = params.get('id', [None])[0]
            handle_action(self, request_id, action)
            return

        send_html_response(self, 404, '未找到', '请求的页面不存在。', False)

    def do_POST(self):
        """处理 POST 请求

        路由分发逻辑：
        1. 飞书网关侧路由（/gw/register, /gw/feishu/send）
        2. Callback 后端侧路由（路由表匹配）
        3. 飞书事件回调兜底（URL验证、消息事件、卡片回传交互）
        """
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get('Content-Length', 0))

        # 验证 Content-Length 范围
        if content_length <= 0 or content_length > MAX_REQUEST_SIZE:
            logger.warning("[POST] Invalid Content-Length: %d", content_length)
            send_json(self, 400, {'error': 'Empty request body' if content_length <= 0 else 'Request body too large'})
            return

        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.warning("[POST] Invalid JSON: %s", e)
            send_json(self, 400, {'error': 'Invalid JSON'})
            return

        # ===== 飞书网关侧路由 =====
        if path == '/gw/register':
            client_ip = self.get_client_ip()
            handled, response = handle_register_request(data, client_ip)
            send_json(self, 200 if response.get('success') else 400, response)
            return

        if path == '/gw/feishu/send':
            binding = verify_owner_based_auth_token(self, data, '/gw/feishu/send')
            if binding is None:
                return  # 验证失败，已发送响应
            handled, response = handle_send_message(binding, data)
            send_json(self, 200 if response.get('success') else 400, response)
            return

        if path == '/gw/feishu/create-group':
            binding = verify_owner_based_auth_token(self, data, '/gw/feishu/create-group')
            if binding is None:
                return
            handled, response = handle_create_group(binding, data)
            send_json(self, 200 if response.get('success') else 400, response)
            return


        # ===== Callback 后端侧路由 =====
        route_handler = BACKEND_ROUTES.get(path)
        if route_handler:
            # 将 HTTPMessage 转为纯字符串字典，确保类型安全
            headers = {k: str(v) for k, v in self.headers.items()}
            # 注入真实客户端 IP（供遥测等需要 IP 的路由使用）
            headers['X-Real-IP'] = self.get_client_ip()
            status, response = route_handler(data, headers)
            send_json(self, status, response)
            return

        # ===== 飞书事件回调（兜底：URL验证、消息事件、卡片回传交互）=====
        handled, response = handle_feishu_request(data)
        if handled:
            send_json(self, 200, response)
            return

        # 未知的 POST 请求
        logger.warning("[POST] Unknown request, type: %s", data.get('type', 'none'))
        send_json(self, 400, {'error': 'Unknown request type'})

