"""
Request Manager Service - 请求管理服务

功能：
    - 管理待处理的权限请求
    - 保存每个请求的 Socket 连接
    - 处理用户决策并通过 Socket 返回
    - 清理断开连接和超时的请求
"""

import json
import logging
import socket
import threading
import time
import traceback
from typing import Tuple, Optional

from config import PERMISSION_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class RequestManager:
    """管理待处理的权限请求"""

    # 单例
    _instance = None  # type: Optional[RequestManager]
    _singleton_lock = threading.Lock()

    # 请求状态常量
    STATUS_PENDING = 'pending'          # 等待用户响应
    STATUS_RESOLVED = 'resolved'        # 已处理（用户已决策）
    STATUS_DISCONNECTED = 'disconnected' # 连接已断开

    # 错误码常量
    ERR_NOT_FOUND = 'not_found'         # 请求不存在
    ERR_ALREADY_RESOLVED = 'already_resolved'  # 请求已被处理
    ERR_DISCONNECTED = 'disconnected'   # 连接已断开
    ERR_UNKNOWN = 'unknown'             # 未知错误

    @classmethod
    def initialize(cls):
        """初始化单例实例"""
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
                logger.info("RequestManager initialized")
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['RequestManager']:
        """获取单例实例"""
        return cls._instance

    def __init__(self):
        self._requests = {}  # request_id -> {conn, data, timestamp, status, resolved_decision}
        self._lock = threading.Lock()

    def register(self, request_id: str, conn: socket.socket, data: dict):
        """注册新的权限请求

        Args:
            request_id: 请求唯一标识
            conn: Unix Socket 连接（用于后续返回决策）
            data: 请求数据（包含 session_id, tool_name, tool_input, project_dir 等）
        """
        with self._lock:
            self._requests[request_id] = {
                'conn': conn,
                'data': data,
                'timestamp': time.time(),
                'status': self.STATUS_PENDING,
                'resolved_decision': None  # 记录已处理的决策类型
            }
            session_id = data.get('session_id', 'unknown')
            logger.info(f"Registered request: {request_id}, Session: {session_id}")

    def _close_connection(self, conn: socket.socket):
        """安全关闭 socket 连接"""
        try:
            conn.close()
        except Exception:
            pass

    def resolve(self, request_id: str, decision: dict) -> Tuple[bool, str, str]:
        """处理用户决策，返回 (是否成功, 错误码, 错误信息)

        Args:
            request_id: 请求 ID
            decision: 决策内容（behavior, message, interrupt）

        Returns:
            (成功标志, 错误码, 错误信息)
            错误码: '' / ERR_NOT_FOUND / ERR_ALREADY_RESOLVED / ERR_DISCONNECTED

        处理流程：
            1. 检查请求是否存在且状态有效
            2. 通过 Socket 连接发送决策给 permission-notify.sh
            3. 标记请求为已解决并关闭连接
        """
        from models.decision import Decision

        with self._lock:
            if request_id not in self._requests:
                logger.warning(f"[resolve] Request {request_id} not found")
                return False, self.ERR_NOT_FOUND, "请求不存在或已被清理"

            req = self._requests[request_id]
            age = time.time() - req['timestamp']
            session_id = req['data'].get('session_id', 'unknown')
            logger.info(f"[resolve] Request {request_id}, Session: {session_id}, status={req['status']}, age={age:.1f}s")

            # 检查请求状态
            if req['status'] == self.STATUS_RESOLVED:
                resolved_type = req.get('resolved_decision', '处理')
                logger.info(f"[resolve] Already resolved: {resolved_type}")
                return False, self.ERR_ALREADY_RESOLVED, f"请求已被{resolved_type}，请勿重复操作"

            if req['status'] == self.STATUS_DISCONNECTED:
                logger.info(f"[resolve] Already disconnected")
                return False, self.ERR_DISCONNECTED, "连接已断开，Agent 可能已继续执行其他操作"

            # 发送决策给等待的进程
            try:
                conn = req['conn']
                logger.debug(f"[resolve] Checking socket connection (fileno={conn.fileno()})")

                # 确保连接处于阻塞模式
                conn.settimeout(None)

                response = json.dumps({
                    'success': True,
                    'decision': decision,
                    'session_id': req['data'].get('session_id', 'unknown'),
                    'tool_name': req['data'].get('tool_name'),
                    'tool_input': req['data'].get('tool_input'),
                    'project_dir': req['data'].get('project_dir')
                })
                response_bytes = response.encode('utf-8')
                logger.info(f"[resolve] Sending {len(response_bytes)} bytes to socket (fileno={conn.fileno()})...")

                # 使用长度前缀协议：4 字节长度 + 数据
                length_prefix = len(response_bytes).to_bytes(4, 'big')
                total_data = length_prefix + response_bytes
                logger.debug(f"[resolve] Total data size: {len(total_data)} bytes (4 header + {len(response_bytes)} payload)")

                conn.sendall(total_data)
                logger.info(f"[resolve] Data sent successfully")

                # 成功：标记为已解决（不关闭连接，让客户端关闭）
                behavior = decision.get(Decision.FIELD_BEHAVIOR, 'unknown')
                req['status'] = self.STATUS_RESOLVED
                req['resolved_decision'] = '批准' if behavior == Decision.ALLOW else '拒绝'
                session_id = req['data'].get('session_id', 'unknown')
                logger.info(f"[resolve] Request {request_id} (Session: {session_id}) resolved: {behavior}")
                return True, '', ''

            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                error_type = type(e).__name__
                logger.error(f"[resolve] {error_type} for {request_id}: {e}")
                req['status'] = self.STATUS_DISCONNECTED
                self._close_connection(req['conn'])
                return False, self.ERR_DISCONNECTED, f"连接已断开（{error_type}），Agent 可能已超时或取消"

            except Exception as e:
                logger.error(f"[resolve] Unexpected error: {type(e).__name__}: {e}")
                logger.error(f"[resolve] Traceback:\n{traceback.format_exc()}")
                return False, self.ERR_UNKNOWN, f"发送决策失败: {str(e)}"

    def get_request_data(self, request_id: str) -> Optional[dict]:
        """获取请求数据（用于始终允许时写入规则）"""
        with self._lock:
            if request_id in self._requests:
                return self._requests[request_id]['data']
            return None

    def get_request_status(self, request_id: str) -> Optional[str]:
        """获取请求状态

        Returns:
            请求状态 (STATUS_PENDING/STATUS_RESOLVED/STATUS_DISCONNECTED)，
            如果请求不存在返回 None
        """
        with self._lock:
            if request_id in self._requests:
                return self._requests[request_id]['status']
            return None

    def _send_fallback_response(self, request_id: str, req: dict, conn, age: float):
        """发送"回退终端"响应，让 Claude 回退到终端交互模式"""
        session_id = req['data'].get('session_id', 'unknown')
        try:
            response = json.dumps({
                'success': False,
                'fallback_to_terminal': True,
                'error': 'server_timeout',
                'session_id': session_id,
                'message': f'服务器超时（{age:.0f}秒），请在终端操作'
            })
            response_bytes = response.encode('utf-8')
            length_prefix = len(response_bytes).to_bytes(4, 'big')
            total_data = length_prefix + response_bytes

            conn.settimeout(5)  # 设置发送超时
            conn.sendall(total_data)
            logger.info(f"Sent fallback response to {request_id} (Session: {session_id}, age={age:.0f}s)")
        except Exception as e:
            logger.warning(f"Failed to send fallback response to {request_id}: {e}")
        finally:
            req['status'] = self.STATUS_DISCONNECTED
            self._close_connection(conn)

    def _is_connection_alive(self, conn) -> bool:
        """检测 socket 连接是否仍然存活"""
        try:
            # 检查 socket 是否有效
            if conn.fileno() == -1:
                return False
            # 使用非阻塞 peek 检测连接状态
            conn.setblocking(False)
            try:
                # MSG_PEEK 不会消费数据，只是查看
                data = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                # 如果返回空，说明对方已关闭连接
                if data == b'':
                    return False
            except BlockingIOError:
                # 没有数据可读，但连接仍然存活
                pass
            except (ConnectionResetError, BrokenPipeError, OSError):
                return False
            finally:
                conn.setblocking(True)
            return True
        except Exception:
            return False

    def cleanup_disconnected(self, max_age: int = None):
        """清理长时间未处理的请求和断开的连接

        功能：
            - 检测并清理已断开的 Socket 连接
            - 超时请求发送"回退终端"响应

        Args:
            max_age: 超时秒数（正整数），None 表示使用 PERMISSION_REQUEST_TIMEOUT 配置
        """
        if not max_age or max_age <= 0:
            max_age = PERMISSION_REQUEST_TIMEOUT

        with self._lock:
            now = time.time()
            for rid, req in list(self._requests.items()):
                if req['status'] == self.STATUS_PENDING:
                    age = now - req['timestamp']
                    conn = req['conn']

                    # 检查连接是否已断开
                    if not self._is_connection_alive(conn):
                        session_id = req['data'].get('session_id', 'unknown')
                        req['status'] = self.STATUS_DISCONNECTED
                        self._close_connection(conn)
                        logger.info(f"Cleaned up dead connection: {rid} (Session: {session_id}, age={age:.0f}s)")
                        continue

                    # 检查是否超时
                    if age > max_age:
                        # 发送"回退终端"响应，而不是直接关闭连接
                        self._send_fallback_response(rid, req, conn, age)

                # 已处理或已断开的请求，从内存中移除
                elif req['status'] in (self.STATUS_RESOLVED, self.STATUS_DISCONNECTED):
                    if now - req['timestamp'] > 60:  # 保留 60 秒供调试
                        del self._requests[rid]
                        logger.debug(f"Removed old request: {rid}")

    def get_stats(self) -> dict:
        """获取当前请求统计信息"""
        with self._lock:
            now = time.time()
            stats = {
                'pending': 0,
                'resolved': 0,
                'disconnected': 0,
                'requests': {}
            }
            for rid, req in self._requests.items():
                stats[req['status']] = stats.get(req['status'], 0) + 1
                stats['requests'][rid] = {
                    'status': req['status'],
                    'age_seconds': int(now - req['timestamp']),
                    'session': req['data'].get('session_id', 'unknown'),
                    'tool': req['data'].get('tool_name', 'Unknown')
                }
            return stats
