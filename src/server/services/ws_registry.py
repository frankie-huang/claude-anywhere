"""WebSocket 连接注册表

管理 WebSocket 长连接的生命周期：
- pending 连接：等待用户授权的连接
- 已认证连接：完成授权，可参与请求路由
- 请求-响应匹配：同步等待响应

超时与清理机制详见 handlers/ws_handler.py 模块文档。
"""

import json
import logging
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from services.ws_protocol import ws_send_text, ws_close

logger = logging.getLogger(__name__)

# pending 连接超时（秒）
WS_PENDING_INACTIVE_TIMEOUT = 90    # 无活动超时（客户端离线）
WS_PENDING_MAX_WAIT_TIME = 600      # 最大授权等待时间（10 分钟）

# WS 隧道请求-响应超时（秒）
WS_REQUEST_TIMEOUT = 10

# 授权卡片发送冷却时间（秒），防止 owner_id 被轰炸
WS_CARD_COOLDOWN = 60

# 每个 owner_id 允许的最大 pending 连接数
WS_MAX_PENDING_PER_OWNER = 5


class WebSocketRegistry:
    """WebSocket 连接注册表（单例）

    管理两种连接状态：
    1. pending: 等待用户授权，不参与请求路由
    2. authenticated: 已授权，参与请求路由
    """

    _instance: Optional['WebSocketRegistry'] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # 已认证连接：owner_id -> (ws_conn, auth_token)
        self._connections: Dict[str, Tuple[socket.socket, str]] = {}
        self._connections_lock = threading.Lock()

        # pending 连接：owner_id -> {request_id: {conn, connect_time, last_activity_time, client_ip}}
        # 支持同一 owner_id 有多个 pending 连接共存
        self._pending: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

        # pending 连接的 auth_token（授权后、auth_ok_ack 前暂存）
        # 格式：owner_id -> {request_id: auth_token}
        self._pending_auth_tokens: Dict[str, Dict[str, str]] = {}

        # pending 连接的绑定参数（auth_ok_ack 收到后用于更新 BindingStore）
        # 格式：owner_id -> {request_id: binding_params}
        self._pending_binding_params: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # 请求-响应匹配
        self._pending_requests: Dict[str, threading.Event] = {}
        self._responses: Dict[str, Dict[str, Any]] = {}
        self._request_lock = threading.Lock()

        # 授权卡片冷却：owner_id -> 上次发卡时间戳
        self._card_cooldown: Dict[str, float] = {}

    @classmethod
    def get_instance(cls) -> Optional['WebSocketRegistry']:
        return cls._instance

    @classmethod
    def initialize(cls) -> 'WebSocketRegistry':
        """初始化单例"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
                logger.info("[ws_registry] Initialized")
            return cls._instance

    # =========================================================================
    # 卡片冷却（防止 owner_id 被授权卡片轰炸）
    # =========================================================================

    def check_card_cooldown(self, owner_id: str) -> bool:
        """检查是否允许发送授权卡片（仅检查，不更新冷却时间）

        同一 owner_id 在 WS_CARD_COOLDOWN 秒内最多触发一次卡片发送。
        卡片成功发送后需调用 set_card_cooldown() 设置冷却时间。

        Returns:
            True 表示允许发送，False 表示冷却中
        """
        now = time.time()
        with self._pending_lock:
            last = self._card_cooldown.get(owner_id, 0)
            return now - last >= WS_CARD_COOLDOWN

    def set_card_cooldown(self, owner_id: str) -> None:
        """设置授权卡片冷却时间（卡片成功发送后调用）"""
        with self._pending_lock:
            self._card_cooldown[owner_id] = time.time()

    # =========================================================================
    # Pending 连接管理（支持同一 owner_id 多个 pending 共存）
    # =========================================================================

    def add_pending(self, owner_id: str, ws_conn: socket.socket, client_ip: str = '') -> str:
        """添加 pending 连接

        支持同一 owner_id 有多个 pending 连接共存，但不超过 WS_MAX_PENDING_PER_OWNER。

        Args:
            owner_id: 飞书用户 ID
            ws_conn: WebSocket 连接的原始 socket
            client_ip: 客户端 IP 地址

        Returns:
            request_id: 本次注册请求的唯一标识（UUID），超过上限时返回空字符串
        """
        now = time.time()
        request_id = str(uuid.uuid4())

        with self._pending_lock:
            # 初始化该 owner_id 的 pending 字典
            if owner_id not in self._pending:
                self._pending[owner_id] = {}
                self._pending_auth_tokens[owner_id] = {}
                self._pending_binding_params[owner_id] = {}

            # 检查 pending 连接数上限
            if len(self._pending[owner_id]) >= WS_MAX_PENDING_PER_OWNER:
                logger.warning(
                    "[ws_registry] Pending limit reached for %s (%d/%d), rejecting",
                    owner_id, len(self._pending[owner_id]), WS_MAX_PENDING_PER_OWNER
                )
                return ''

            # 添加新的 pending 连接
            self._pending[owner_id][request_id] = {
                'conn': ws_conn,
                'connect_time': now,
                'last_activity_time': now,
                'client_ip': client_ip
            }
            logger.info(
                "[ws_registry] Added pending connection for %s, request_id=%s, ip=%s, total=%d",
                owner_id, request_id, client_ip, len(self._pending[owner_id])
            )

        return request_id

    def get_pending(self, owner_id: str, request_id: str) -> Optional[Any]:
        """获取指定的 pending 连接

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID（必需）

        Returns:
            WebSocket 连接对象，如果找不到返回 None
        """
        with self._pending_lock:
            if owner_id not in self._pending:
                return None

            pendings = self._pending[owner_id]
            if request_id in pendings:
                return pendings[request_id]['conn']
            return None

    def update_pending_activity(self, owner_id: str, request_id: str) -> None:
        """更新 pending 连接的最后活动时间

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID（必需）
        """
        now = time.time()
        with self._pending_lock:
            if owner_id not in self._pending:
                return

            pendings = self._pending[owner_id]
            if request_id in pendings:
                pendings[request_id]['last_activity_time'] = now

    def check_connection_status(self, owner_id: str, request_id: str, sock: socket.socket) -> str:
        """检查连接状态（用于 pending 连接的消息循环）

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID
            sock: socket 连接对象（用于判断是否被 promote）

        Returns:
            'pending': 仍在 pending 状态
            'authenticated': 已升级为 authenticated（socket 匹配）
            'removed': 已被移除（超时/清理或被其他连接替换）
        """
        # 先检查是否仍在 pending
        with self._pending_lock:
            if owner_id in self._pending and request_id in self._pending[owner_id]:
                return 'pending'

        # 检查是否被 promote（通过 socket 匹配判断）
        with self._connections_lock:
            if owner_id in self._connections:
                conn, _ = self._connections[owner_id]
                if conn is sock:
                    return 'authenticated'

        return 'removed'

    def remove_pending(self, owner_id: str, request_id: str) -> Optional[socket.socket]:
        """移除指定的 pending 连接

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID

        Returns:
            被移除的连接，如果找不到返回 None
        """
        with self._pending_lock:
            if owner_id not in self._pending:
                return None

            if request_id not in self._pending[owner_id]:
                return None

            conn = self._pending[owner_id].pop(request_id)['conn']
            self._pending_auth_tokens[owner_id].pop(request_id, None)
            self._pending_binding_params[owner_id].pop(request_id, None)
            logger.info(
                "[ws_registry] Removed pending connection for %s, request_id=%s, remaining=%d",
                owner_id, request_id, len(self._pending[owner_id])
            )
            # 如果该 owner_id 没有其他 pending 了，清理空字典
            if not self._pending[owner_id]:
                self._pending.pop(owner_id, None)
                self._pending_auth_tokens.pop(owner_id, None)
                self._pending_binding_params.pop(owner_id, None)
            return conn

    def set_pending_auth_token(self, owner_id: str, request_id: str, auth_token: str) -> None:
        """设置 pending 连接的 auth_token（授权后、auth_ok_ack 前暂存）

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID
            auth_token: 认证令牌
        """
        with self._pending_lock:
            if owner_id not in self._pending_auth_tokens:
                self._pending_auth_tokens[owner_id] = {}
            self._pending_auth_tokens[owner_id][request_id] = auth_token

    def get_pending_auth_token(self, owner_id: str, request_id: str) -> Optional[str]:
        """获取 pending 连接的 auth_token

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID（必需）

        Returns:
            auth_token，如果找不到返回 None
        """
        with self._pending_lock:
            if owner_id not in self._pending_auth_tokens:
                return None
            return self._pending_auth_tokens[owner_id].get(request_id)

    def set_pending_binding_params(self, owner_id: str, request_id: str, params: Dict[str, Any]) -> None:
        """设置 pending 连接的绑定参数（auth_ok_ack 收到后用于更新 BindingStore）

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID
            params: 绑定参数
        """
        with self._pending_lock:
            if owner_id not in self._pending_binding_params:
                self._pending_binding_params[owner_id] = {}
            self._pending_binding_params[owner_id][request_id] = params

    def get_pending_binding_params(self, owner_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        """获取 pending 连接的绑定参数

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID（必需）

        Returns:
            绑定参数字典，如果找不到返回 None
        """
        with self._pending_lock:
            if owner_id not in self._pending_binding_params:
                return None
            return self._pending_binding_params[owner_id].get(request_id)

    def cleanup_connection(self, owner_id: str, ws_conn: socket.socket) -> None:
        """清理指定连接（从 authenticated 或 pending 中移除）

        消息循环结束（连接断开/超时/异常）时调用，确保连接从 registry 中移除。
        通过 ws_conn 身份比较确保只移除对应的连接，防止误删已被替换的新连接。

        同一 owner_id 可能同时存在 authenticated 连接和多个 pending 连接，
        因此两个集合都需要检查。

        Args:
            owner_id: 飞书用户 ID
            ws_conn: 要清理的具体连接
        """
        # 尝试从 authenticated 移除
        with self._connections_lock:
            if owner_id in self._connections:
                conn, _ = self._connections[owner_id]
                if conn is ws_conn:
                    self._connections.pop(owner_id, None)
                    logger.info("[ws_registry] Cleaned up authenticated connection for %s", owner_id)
                    return

        # 尝试从 pending 移除（遍历查找匹配的 socket）
        with self._pending_lock:
            if owner_id in self._pending:
                pendings = self._pending[owner_id]
                # 查找匹配的 request_id
                matched_request_id = None
                for request_id, info in pendings.items():
                    if info['conn'] is ws_conn:
                        matched_request_id = request_id
                        break

                if matched_request_id:
                    pendings.pop(matched_request_id, None)
                    self._pending_auth_tokens[owner_id].pop(matched_request_id, None)
                    self._pending_binding_params[owner_id].pop(matched_request_id, None)
                    logger.info(
                        "[ws_registry] Cleaned up pending connection for %s, request_id=%s, remaining=%d",
                        owner_id, matched_request_id, len(pendings)
                    )
                    # 如果没有其他 pending 了，清理空字典
                    if not pendings:
                        self._pending.pop(owner_id, None)
                        self._pending_auth_tokens.pop(owner_id, None)
                        self._pending_binding_params.pop(owner_id, None)

    def promote_pending(self, owner_id: str, request_id: str, auth_token: str) -> bool:
        """将 pending 连接升级为已认证

        Args:
            owner_id: 飞书用户 ID
            request_id: 请求 ID
            auth_token: 认证令牌

        Returns:
            True 表示升级成功
        """
        # 步骤 1: 在 _pending_lock 下取出并删除 pending 连接及暂存数据
        with self._pending_lock:
            if owner_id not in self._pending:
                logger.warning("[ws_registry] No pending connection to promote for %s", owner_id)
                return False

            pendings = self._pending[owner_id]
            if request_id not in pendings:
                logger.warning(
                    "[ws_registry] No pending connection with request_id=%s for %s",
                    request_id, owner_id
                )
                return False

            pending_conn = pendings.pop(request_id)['conn']
            self._pending_auth_tokens[owner_id].pop(request_id, None)
            self._pending_binding_params[owner_id].pop(request_id, None)

            # 如果没有其他 pending 了，清理空字典
            if not pendings:
                self._pending.pop(owner_id, None)
                self._pending_auth_tokens.pop(owner_id, None)
                self._pending_binding_params.pop(owner_id, None)

        # 步骤 2: 在 _connections_lock 下注册为已认证连接
        # 注意：两把锁不嵌套，避免死锁
        old_conn = None
        with self._connections_lock:
            if owner_id in self._connections:
                old_conn, _ = self._connections[owner_id]
                logger.info("[ws_registry] Replacing old authenticated connection for %s", owner_id)
            self._connections[owner_id] = (pending_conn, auth_token)

        # 步骤 3: 在锁外处理旧连接（发送通知和关闭可能阻塞）
        if old_conn is not None:
            self._send_replaced_notification(old_conn)
            self._close_connection(old_conn)

        logger.info("[ws_registry] Promoted pending connection for %s, request_id=%s", owner_id, request_id)
        return True

    def prepare_authorization(self, owner_id: str, request_id: str,
                              auth_token: str, binding_params: Dict[str, Any],
                              merge: bool = True) -> bool:
        """原子地设置 auth_token、绑定参数，并关闭其他 pending 连接

        将 set_pending_auth_token + set_pending_binding_params + remove_other_pendings
        合并为单次加锁操作，防止在多步操作间隙有新连接加入后被误清除。

        Args:
            owner_id: 飞书用户 ID
            request_id: 要授权的请求 ID
            auth_token: 认证令牌
            binding_params: 绑定参数
            merge: 是否合并已有参数（True，默认）还是覆盖（False）

        Returns:
            True 表示 request_id 对应的 pending 连接仍存在且设置成功，
            False 表示连接已不存在（超时/断开/已被清理）
        """
        conns_to_close = []

        with self._pending_lock:
            # 验证 request_id 对应的 pending 连接仍然存在
            if owner_id not in self._pending or request_id not in self._pending[owner_id]:
                logger.warning(
                    "[ws_registry] prepare_authorization: pending connection gone for %s, request_id=%s",
                    owner_id, request_id
                )
                return False

            # 设置 auth_token
            if owner_id not in self._pending_auth_tokens:
                self._pending_auth_tokens[owner_id] = {}
            self._pending_auth_tokens[owner_id][request_id] = auth_token

            # 设置绑定参数
            if owner_id not in self._pending_binding_params:
                self._pending_binding_params[owner_id] = {}
            if merge and request_id in self._pending_binding_params.get(owner_id, {}):
                # 合并模式：用新值更新已有字段，已有但未传入的字段保持不变
                existing = self._pending_binding_params[owner_id][request_id]
                existing.update(binding_params)
            else:
                # 覆盖模式：直接替换
                self._pending_binding_params[owner_id][request_id] = binding_params

            # 移除其他 pending 连接
            pendings = self._pending[owner_id]
            for rid in [r for r in pendings if r != request_id]:
                conns_to_close.append(pendings.pop(rid)['conn'])
                self._pending_auth_tokens[owner_id].pop(rid, None)
                self._pending_binding_params[owner_id].pop(rid, None)

            if conns_to_close:
                logger.info(
                    "[ws_registry] prepare_authorization: removed %d other pending connections for %s",
                    len(conns_to_close), owner_id
                )

        # 在锁外关闭连接（发送通知和关闭可能阻塞）
        for conn in conns_to_close:
            self._send_replaced_notification(conn)
            self._close_connection(conn)

        return True

    def remove_other_pendings(self, owner_id: str, exclude_request_id: str) -> None:
        """移除指定 owner_id 的其他所有 pending 连接（保留 exclude_request_id）

        注意：此方法的功能已被 prepare_authorization 内联实现（单次加锁完成
        设置 auth_token/binding_params 并移除其他 pending），保留此方法以备
        将来需要独立移除其他 pending 连接的场景。

        用于用户授权一个终端后，关闭其他所有待授权的终端。

        Args:
            owner_id: 飞书用户 ID
            exclude_request_id: 要保留的 request_id
        """
        conns_to_close = []

        with self._pending_lock:
            if owner_id not in self._pending:
                return

            pendings = self._pending[owner_id]
            request_ids_to_remove = [
                rid for rid in pendings.keys() if rid != exclude_request_id
            ]

            for rid in request_ids_to_remove:
                conn = pendings.pop(rid)['conn']
                self._pending_auth_tokens[owner_id].pop(rid, None)
                self._pending_binding_params[owner_id].pop(rid, None)
                conns_to_close.append((rid, conn))

            logger.info(
                "[ws_registry] Removed %d other pending connections for %s",
                len(conns_to_close), owner_id
            )

        # 在锁外关闭连接（发送通知和关闭可能阻塞）
        for rid, conn in conns_to_close:
            try:
                self._send_replaced_notification(conn)
                self._close_connection(conn)
            except Exception as e:
                logger.debug("[ws_registry] Error closing pending connection %s: %s", rid, e)

    # =========================================================================
    # 已认证连接管理
    # =========================================================================

    def register(self, owner_id: str, ws_conn: socket.socket, auth_token: str) -> None:
        """注册已认证连接

        如果已有该 owner_id 的连接，替换旧的（发送 replaced 通知）。

        Args:
            owner_id: 飞书用户 ID
            ws_conn: WebSocket 连接对象
            auth_token: 认证令牌
        """
        old_conn = None
        with self._connections_lock:
            if owner_id in self._connections:
                old_conn, _ = self._connections[owner_id]
                logger.info("[ws_registry] Replacing existing connection for %s", owner_id)

            self._connections[owner_id] = (ws_conn, auth_token)
            logger.info("[ws_registry] Registered authenticated connection for %s", owner_id)

        # 在锁外处理旧连接（发送通知和关闭可能阻塞）
        if old_conn is not None:
            self._send_replaced_notification(old_conn)
            self._close_connection(old_conn)

    def unregister(self, owner_id: str) -> None:
        """注销连接"""
        with self._connections_lock:
            if owner_id in self._connections:
                self._connections.pop(owner_id, None)
                logger.info("[ws_registry] Unregistered connection for %s", owner_id)

    def get(self, owner_id: str) -> Optional[Any]:
        """获取已认证连接"""
        with self._connections_lock:
            if owner_id in self._connections:
                return self._connections[owner_id][0]
        return None

    def get_auth_token(self, owner_id: str) -> Optional[str]:
        """获取已认证连接的 auth_token"""
        with self._connections_lock:
            if owner_id in self._connections:
                return self._connections[owner_id][1]
        return None

    def is_authenticated(self, owner_id: str) -> bool:
        """检查是否有已认证连接"""
        with self._connections_lock:
            return owner_id in self._connections

    # =========================================================================
    # 请求-响应匹配
    # =========================================================================

    def send_request(self, owner_id: str, path: str, body: Dict[str, Any],
                     headers: Optional[Dict[str, str]] = None,
                     timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """发送请求并同步等待响应

        Args:
            owner_id: 飞书用户 ID
            path: 请求路径（如 /cb/decision）
            body: 请求体
            headers: 额外的请求头
            timeout: 等待超时（秒），默认 WS_REQUEST_TIMEOUT

        Returns:
            响应数据，超时或连接不存在返回 None
        """
        if timeout is None:
            timeout = WS_REQUEST_TIMEOUT

        ws_conn = self.get(owner_id)
        if ws_conn is None:
            logger.debug("[ws_registry] No connection for %s", owner_id)
            return None

        # 生成请求 ID
        request_id = str(uuid.uuid4())

        # 构造请求消息
        request_msg = {
            'type': 'request',
            'id': request_id,
            'method': 'POST',
            'path': path,
            'headers': headers or {},
            'body': body
        }

        # 准备等待
        event = threading.Event()
        with self._request_lock:
            self._pending_requests[request_id] = event

        try:
            # 发送请求（ws_protocol 层已内置 per-socket 发送锁，无需额外加锁）
            ws_send_text(ws_conn, json.dumps(request_msg))
            logger.debug("[ws_registry] Sent request %s to %s: %s", request_id, owner_id, path)

            # 等待响应
            if event.wait(timeout=timeout):
                with self._request_lock:
                    response = self._responses.pop(request_id, None)
                return response
            else:
                logger.warning("[ws_registry] Request %s timed out (%.1fs)", request_id, timeout)
                return None

        except Exception as e:
            logger.error("[ws_registry] Failed to send request: %s", e)
            return None
        finally:
            with self._request_lock:
                self._pending_requests.pop(request_id, None)
                self._responses.pop(request_id, None)

    def handle_response(self, response: Dict[str, Any]) -> None:
        """处理收到的响应消息

        Args:
            response: 响应消息字典，包含 id 字段
        """
        request_id = response.get('id')
        if not request_id:
            logger.warning("[ws_registry] Response missing id: %s", response)
            return

        with self._request_lock:
            event = self._pending_requests.get(request_id)
            if event:
                self._responses[request_id] = response
                event.set()
                logger.debug("[ws_registry] Matched response for request %s", request_id)
            else:
                logger.warning("[ws_registry] No pending request for response %s", request_id)

    # =========================================================================
    # 辅助方法
    # =========================================================================

    def _send_replaced_notification(self, ws_conn: socket.socket) -> None:
        """发送连接被替换通知"""
        try:
            msg = json.dumps({'type': 'replaced', 'action': 'stop'})
            ws_send_text(ws_conn, msg)
            logger.debug("[ws_registry] Sent replaced notification")
        except Exception as e:
            logger.debug("[ws_registry] Failed to send replaced notification: %s", e)

    def _close_connection(self, ws_conn: socket.socket) -> None:
        """关闭连接"""
        try:
            ws_close(ws_conn)
        except Exception as e:
            logger.debug("[ws_registry] Failed to close connection: %s", e)

    def get_all_connections(self) -> Dict[str, Any]:
        """获取所有已认证连接（用于广播）"""
        with self._connections_lock:
            return {owner_id: conn for owner_id, (conn, _) in self._connections.items()}

    def get_status(self) -> Dict[str, Any]:
        """获取连接状态摘要（用于 /status 端点）

        Returns:
            包含 authenticated 和 pending 连接数及详情的字典
        """
        now = time.time()

        with self._connections_lock:
            authenticated_ids = list(self._connections.keys())

        with self._pending_lock:
            pending_info = []
            for owner_id, pendings in self._pending.items():
                for request_id, info in pendings.items():
                    pending_info.append({
                        'owner_id': owner_id,
                        'request_id': request_id,
                        'client_ip': info.get('client_ip', ''),
                        'waiting_seconds': int(now - info['connect_time'])
                    })

        return {
            'authenticated_count': len(authenticated_ids),
            'authenticated_owner_ids': authenticated_ids,
            'pending_count': len(pending_info),
            'pending': pending_info
        }

    def cleanup_expired_pending(self) -> int:
        """清理超时的 pending 连接

        双重超时机制：
        - 无活动超过 WS_PENDING_INACTIVE_TIMEOUT（90秒）：客户端离线
        - 等待授权超过 WS_PENDING_MAX_WAIT_TIME（10分钟）：用户可能忘记或恶意

        Returns:
            清理的连接数
        """
        now = time.time()
        expired_conns = []  # [(owner_id, conn, reason), ...]

        # 在锁内收集过期的连接并从字典中移除
        with self._pending_lock:
            # 收集过期的 (owner_id, request_id, reason)
            expired_ids = []
            for owner_id, pendings in self._pending.items():
                for request_id, info in pendings.items():
                    inactive_time = now - info['last_activity_time']
                    wait_time = now - info['connect_time']

                    if inactive_time > WS_PENDING_INACTIVE_TIMEOUT:
                        expired_ids.append((owner_id, request_id, 'inactive'))
                        logger.info("[ws_registry] Pending connection for %s (request_id=%s) inactive for %ds", owner_id, request_id, inactive_time)
                    elif wait_time > WS_PENDING_MAX_WAIT_TIME:
                        expired_ids.append((owner_id, request_id, 'timeout'))
                        logger.info("[ws_registry] Pending connection for %s (request_id=%s) waited %ds without authorization", owner_id, request_id, wait_time)

            for owner_id, request_id, reason in expired_ids:
                pendings = self._pending.get(owner_id)
                if pendings and request_id in pendings:
                    conn = pendings.pop(request_id)['conn']
                    self._pending_auth_tokens.get(owner_id, {}).pop(request_id, None)
                    self._pending_binding_params.get(owner_id, {}).pop(request_id, None)
                    expired_conns.append((owner_id, conn, reason))

                    # 清理空字典
                    if pendings is not None and not pendings:
                        self._pending.pop(owner_id, None)
                        self._pending_auth_tokens.pop(owner_id, None)
                        self._pending_binding_params.pop(owner_id, None)

        # 在锁外发送消息和关闭连接（避免阻塞其他操作）
        for owner_id, conn, reason in expired_conns:
            try:
                if reason == 'inactive':
                    msg = json.dumps({'type': 'auth_error', 'message': 'connection inactive'})
                else:
                    msg = json.dumps({'type': 'auth_error', 'action': 'stop', 'message': 'authorization timeout'})
                ws_send_text(conn, msg)
            except Exception:
                pass
            self._close_connection(conn)

        if expired_conns:
            logger.info("[ws_registry] Cleaned up %d expired pending connections", len(expired_conns))

        # 顺带清理过期的卡片冷却记录
        with self._pending_lock:
            expired_cooldowns = [
                oid for oid, ts in self._card_cooldown.items()
                if now - ts > WS_CARD_COOLDOWN
            ]
            for oid in expired_cooldowns:
                self._card_cooldown.pop(oid, None)

        return len(expired_conns)
