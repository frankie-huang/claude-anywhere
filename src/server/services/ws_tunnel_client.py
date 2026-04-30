"""WebSocket 隧道客户端

Callback 后端的 WebSocket 客户端，主动连接飞书网关建立隧道。
支持首次注册和重连两种模式。
"""

import concurrent.futures
import json
import logging
import random
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from services.ws_protocol import (
    ws_client_connect, ws_send_text, ws_recv, ws_send_ping,
    ws_send_pong, ws_send_close, ws_close, cleanup_socket_state,
    OPCODE_TEXT, OPCODE_PING, OPCODE_CLOSE
)
from services.auth_token_store import AuthTokenStore

logger = logging.getLogger(__name__)

# 心跳间隔（秒）
WS_PING_INTERVAL = 30

# 服务端 read timeout（秒），与服务端保持一致，用于检测服务端是否存活
WS_READ_TIMEOUT = 90

# 重连参数
RECONNECT_INITIAL_DELAY = 1.0  # 初始延迟
RECONNECT_MAX_DELAY = 60.0  # 最大延迟
RECONNECT_BACKOFF_FACTOR = 2.0  # 指数退避因子

# 请求处理并发数上限
MAX_REQUEST_WORKERS = 8


class WSTunnelClient:
    """WebSocket 隧道客户端

    运行在 Callback 后端，主动连接飞书网关建立隧道。
    """

    # WS 隧道端点路径
    WS_TUNNEL_PATH = '/ws/tunnel'

    def __init__(self, gateway_url: str, owner_id: str,
                 reply_in_thread: bool = False,
                 at_bot_only: Optional[bool] = None,
                 session_mode: str = '',
                 claude_commands: Optional[List[str]] = None,
                 default_chat_dir: str = '',
                 default_chat_follow_thread: bool = True,
                 group_name_prefix: Optional[str] = None,
                 group_dissolve_days: Optional[int] = None) -> None:
        """
        Args:
            gateway_url: 网关 HTTP base URL（如 http://gateway:8080）
            owner_id: 飞书用户 ID
            reply_in_thread: 是否使用回复话题模式
            at_bot_only: 群聊 @bot 过滤（传递给 gateway BindingStore）
            session_mode: 会话模式（message/thread/group）
            claude_commands: 可用的 Claude 命令列表
            default_chat_dir: 默认聊天目录
            default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
            group_name_prefix: 群聊名称前缀（传递给 gateway BindingStore）
            group_dissolve_days: 群聊自动解散天数（传递给 gateway BindingStore）
        """
        self.gateway_url = gateway_url
        self.owner_id = owner_id
        self.reply_in_thread = reply_in_thread
        self.at_bot_only = at_bot_only
        self.session_mode = session_mode
        self.claude_commands = claude_commands
        self.default_chat_dir = default_chat_dir
        self.default_chat_follow_thread = default_chat_follow_thread
        self.group_name_prefix = group_name_prefix
        self.group_dissolve_days = group_dissolve_days
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.authenticated = False
        self.reconnect_delay = RECONNECT_INITIAL_DELAY
        self.received_shutdown = False

        # 线程控制
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 请求处理线程池：限制并发处理的请求数，防止大量请求耗尽线程资源
        self._request_executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_REQUEST_WORKERS)

    def start(self) -> None:
        """启动客户端（在后台线程中运行）"""
        if self._thread and self._thread.is_alive():
            logger.warning("[ws_client] Already running")
            return

        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("[ws_client] Started")

    def stop(self) -> None:
        """停止客户端"""
        self.running = False
        self._stop_event.set()

        sock = self.sock
        if sock:
            try:
                ws_close(sock)
            except Exception:
                pass

        if self._thread:
            self._thread.join(timeout=5)

        # 关闭线程池（不等待进行中的任务，避免阻塞）
        self._request_executor.shutdown(wait=False)

        logger.info("[ws_client] Stopped")

    def _run_loop(self) -> None:
        """主循环：连接、重连"""
        while self.running and not self._stop_event.is_set():
            try:
                self._connect_and_run()
            except (ConnectionError, socket.error, OSError) as e:
                # 网络/连接相关错误，静默重连
                logger.info("[ws_client] Connection lost: %s", e)
            except ValueError as e:
                # 协议错误（如握手失败、无效响应）
                logger.warning("[ws_client] Protocol error: %s", e)
            except Exception as e:
                # 未预期的错误，记录完整堆栈
                logger.error("[ws_client] Unexpected error: %s", e, exc_info=True)

            if not self.running or self._stop_event.is_set():
                break

            # 计算重连延迟（带 jitter）
            if self.received_shutdown:
                # 计划维护，立即重连
                delay = 0
                self.received_shutdown = False
            else:
                jitter = random.random() * 0.5 + 0.5  # 0.5 ~ 1.0
                delay = self.reconnect_delay * jitter

            logger.info("[ws_client] Reconnecting in %.1fs...", delay)
            self._stop_event.wait(delay)

            # 指数退避
            self.reconnect_delay = min(
                self.reconnect_delay * RECONNECT_BACKOFF_FACTOR,
                RECONNECT_MAX_DELAY
            )

    def _connect_and_run(self) -> None:
        """建立连接并运行消息循环"""
        # 从 HTTP base URL 转为 WS URL：http→ws, https→wss
        _base = self.gateway_url.rstrip('/')
        if _base.startswith('https://'):
            _ws_base = 'wss://' + _base[8:]
        else:
            _ws_base = 'ws://' + _base[7:]
        url = f"{_ws_base}{self.WS_TUNNEL_PATH}?owner_id={self.owner_id}"
        logger.info("[ws_client] Connecting to gateway")

        # 建立 WebSocket 连接
        try:
            self.sock = ws_client_connect(url)
            logger.info("[ws_client] Connected to gateway")
        except (ConnectionError, socket.error, OSError) as e:
            logger.warning("[ws_client] Failed to connect: %s", e)
            raise
        except ValueError as e:
            # 握手失败、URL 格式错误等
            logger.error("[ws_client] Handshake failed: %s", e)
            raise
        except Exception as e:
            logger.error("[ws_client] Unexpected connect error: %s", e, exc_info=True)
            raise

        # 始终发送 register 消息（让网关生成新 token，和 HTTP 模式保持一致）
        register_data = {
            'type': 'register',
            'owner_id': self.owner_id,
            'reply_in_thread': self.reply_in_thread,
            'at_bot_only': self.at_bot_only,
            'session_mode': self.session_mode
        }
        if self.claude_commands:
            register_data['claude_commands'] = self.claude_commands
        if self.default_chat_dir:
            register_data['default_chat_dir'] = self.default_chat_dir
        register_data['default_chat_follow_thread'] = self.default_chat_follow_thread
        register_data['group_name_prefix'] = self.group_name_prefix
        register_data['group_dissolve_days'] = self.group_dissolve_days

        # 携带已持久化的 auth_token，用于网关判断续期/换绑
        token_store = AuthTokenStore.get_instance()
        if token_store:
            existing_token = token_store.get()
            if existing_token:
                register_data['auth_token'] = existing_token

        register_msg = json.dumps(register_data)
        ws_send_text(self.sock, register_msg)
        logger.debug("[ws_client] Sent register message")

        # 启动心跳线程（传入当前 socket 引用，防止重连后旧线程操作新连接）
        current_sock = self.sock
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, args=(current_sock,), daemon=True)
        heartbeat_thread.start()

        # 消息循环
        try:
            while self.running and not self._stop_event.is_set():
                try:
                    self.sock.settimeout(WS_READ_TIMEOUT)
                    opcode, payload = ws_recv(self.sock)
                except socket.timeout:
                    logger.warning("[ws_client] Read timeout, connection lost")
                    break
                except (ConnectionError, socket.error, OSError) as e:
                    logger.info("[ws_client] Connection closed: %s", e)
                    break
                except ValueError as e:
                    # 协议错误（如帧格式错误、帧过大）
                    logger.warning("[ws_client] Protocol error: %s", e)
                    break
                except Exception as e:
                    # 未预期的错误，记录完整堆栈
                    logger.error("[ws_client] Receive error: %s", e, exc_info=True)
                    break

                if opcode == OPCODE_TEXT:
                    try:
                        msg = json.loads(payload.decode('utf-8'))
                        self._handle_message(msg)
                    except json.JSONDecodeError as e:
                        logger.warning("[ws_client] Invalid JSON: %s", e)
                    except UnicodeDecodeError as e:
                        logger.warning("[ws_client] Invalid UTF-8: %s", e)
                    except Exception as e:
                        # 消息处理错误不应断开连接，仅记录
                        logger.error("[ws_client] Message handling error: %s", e, exc_info=True)

                elif opcode == OPCODE_PING:
                    ws_send_pong(self.sock, payload)

                elif opcode == OPCODE_CLOSE:
                    logger.info("[ws_client] Close frame received")
                    break

        finally:
            self.authenticated = False
            if self.sock:
                try:
                    cleanup_socket_state(self.sock)
                except Exception:
                    pass
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _heartbeat_loop(self, conn_sock: socket.socket) -> None:
        """心跳循环

        在连接建立后立即开始发送心跳，包括 pending 状态。
        这样可以防止 nginx 等中间代理因空闲超时关闭连接。

        Args:
            conn_sock: 此心跳线程绑定的 socket 连接
        """
        while self.running and not self._stop_event.is_set():
            self._stop_event.wait(WS_PING_INTERVAL)
            if not self.running or self._stop_event.is_set():
                break

            # 检查当前连接是否还是此线程绑定的连接（防止重连后旧线程操作新连接）
            if self.sock is not conn_sock:
                logger.debug("[ws_client] Connection changed, stopping heartbeat thread")
                break

            try:
                ws_send_ping(conn_sock)
                logger.debug("[ws_client] Ping sent")
            except (ConnectionError, socket.error, OSError) as e:
                logger.debug("[ws_client] Ping failed: %s", e)
                break
            except Exception as e:
                logger.debug("[ws_client] Ping error: %s", e)
                break

    def _handle_message(self, msg: Dict[str, Any]) -> None:
        """处理收到的消息"""
        msg_type = msg.get('type')
        action = msg.get('action')

        # 按类型分发
        if msg_type == 'auth_ok':
            self._handle_auth_ok(msg)
        elif msg_type == 'auth_error':
            self._handle_auth_error(msg)
        elif msg_type == 'request':
            self._handle_request(msg)
        elif msg_type == 'shutdown':
            self._handle_shutdown()
        elif msg_type == 'replaced':
            # replaced: 被新连接替换
            # 原因：换绑后网关已为新客户端生成新 auth_token，
            # 旧客户端携带的 token 不匹配，重连会反复触发换绑授权卡片。
            # 由底部 stop action 统一停止重连
            logger.warning("[ws_client] Connection replaced by another client")
        elif msg_type == 'unbind':
            # unbind: 用户在飞书端执行了解绑操作，清除本地 token 并停止重连
            self._clear_auth_token()
            logger.info("[ws_client] User unbind: %s", msg.get('message', ''))
        elif msg_type == 'pong':
            pass  # 忽略心跳响应
        else:
            logger.debug("[ws_client] Unknown message type: %s", msg_type)
            return

        # 统一处理 stop action
        if action == 'stop':
            logger.info("[ws_client] Received stop action: type=%s", msg_type)
            self._stop_reconnect()

    def _handle_auth_ok(self, msg: Dict[str, Any]) -> None:
        """处理 auth_ok 消息"""
        auth_token = msg.get('auth_token')
        if not auth_token:
            logger.warning("[ws_client] auth_ok missing auth_token")
            return

        # 存储 auth_token（及 bot_open_id）
        bot_open_id = msg.get('bot_open_id', '')
        token_store = AuthTokenStore.get_instance()
        if token_store:
            token_store.save(self.owner_id, auth_token, bot_open_id=bot_open_id)
            logger.info("[ws_client] Stored auth_token for %s", self.owner_id)

        # 发送确认消息（让网关知道我们已收到并存储了 token）
        sock = self.sock
        if sock:
            try:
                ack_msg = json.dumps({'type': 'auth_ok_ack'})
                ws_send_text(sock, ack_msg)
                logger.debug("[ws_client] Sent auth_ok_ack")
            except Exception as e:
                logger.warning("[ws_client] Failed to send auth_ok_ack: %s", e)
                return

        self.authenticated = True
        self.reconnect_delay = RECONNECT_INITIAL_DELAY
        logger.info("[ws_client] Authentication successful")

    def _handle_auth_error(self, msg: Dict[str, Any]) -> None:
        """处理 auth_error 消息

        注意：带 action: stop 的 auth_error 已在 _handle_message 中统一处理 stop action。
        这里处理 connection 断开等可恢复的错误（如 connection inactive）。
        """
        message = msg.get('message', 'unknown error')
        logger.warning("[ws_client] Authentication error: %s", message)
        self.authenticated = False
        # 服务端发完 auth_error 后会关闭 socket，消息循环收到异常后退出并重连

    def _handle_request(self, msg: Dict[str, Any]) -> None:
        """处理来自网关的请求（在线程池中执行，避免阻塞消息循环）"""
        self._request_executor.submit(self._process_request, msg)

    def _process_request(self, msg: Dict[str, Any]) -> None:
        """实际处理请求（在独立线程中运行）

        直接调用 BACKEND_ROUTES 中的纯函数，不经过 HTTP 回环。
        """
        from handlers.callback import BACKEND_ROUTES

        request_id = msg.get('id')
        path = msg.get('path', '')
        headers = msg.get('headers', {})
        body = msg.get('body', {})

        logger.debug("[ws_client] Request: id=%s, path=%s", request_id, path)

        route_handler = BACKEND_ROUTES.get(path)
        if route_handler:
            try:
                status, response = route_handler(body, headers)
                response_msg = {
                    'type': 'response',
                    'id': request_id,
                    'status': status,
                    'body': response
                }
            except Exception as e:
                logger.error("[ws_client] Handler error: %s", e)
                response_msg = {
                    'type': 'error',
                    'id': request_id,
                    'code': 'handler_error',
                    'message': str(e)
                }
        else:
            logger.warning("[ws_client] Unknown path: %s", path)
            response_msg = {
                'type': 'error',
                'id': request_id,
                'code': 'not_found',
                'message': 'Unknown path: ' + path
            }

        # 发送响应（ws_protocol 层已内置 per-socket 发送锁，无需额外加锁）
        sock = self.sock
        if sock:
            try:
                ws_send_text(sock, json.dumps(response_msg))
            except Exception as e:
                logger.error("[ws_client] Failed to send response: %s", e)

    def _stop_reconnect(self) -> None:
        """停止重连循环

        用于需要永久停止的场景（如被替换、解绑、授权超时），
        区别于 stop() 方法（会主动关闭 socket）。
        """
        self.running = False
        self._stop_event.set()

    def _clear_auth_token(self) -> None:
        """清除本地存储的 auth_token"""
        token_store = AuthTokenStore.get_instance()
        if token_store:
            token_store.delete(self.owner_id)
            logger.info("[ws_client] Deleted auth_token for %s", self.owner_id)

    def _handle_shutdown(self) -> None:
        """处理网关关闭通知"""
        logger.info("[ws_client] Gateway is shutting down, will reconnect immediately")
        self.received_shutdown = True

# =============================================================================
# 全局客户端实例
# =============================================================================

_client_instance: Optional['WSTunnelClient'] = None


def start_ws_tunnel_client(gateway_url: str, owner_id: str,
                           reply_in_thread: bool = False,
                           at_bot_only: Optional[bool] = None,
                           session_mode: str = '',
                           claude_commands: Optional[List[str]] = None,
                           default_chat_dir: str = '',
                           default_chat_follow_thread: bool = True,
                           group_name_prefix: Optional[str] = None,
                           group_dissolve_days: Optional[int] = None) -> WSTunnelClient:
    """启动 WebSocket 隧道客户端

    Args:
        gateway_url: 网关 HTTP base URL（如 http://gateway:8080）
        owner_id: 飞书用户 ID
        reply_in_thread: 是否使用回复话题模式
        at_bot_only: 群聊 @bot 过滤（传递给 gateway BindingStore）
        session_mode: 会话模式（message/thread/group）
        claude_commands: 可用的 Claude 命令列表
        default_chat_dir: 默认聊天目录
        default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
        group_name_prefix: 群聊名称前缀（传递给 gateway BindingStore）
        group_dissolve_days: 群聊自动解散天数（传递给 gateway BindingStore）

    Returns:
        客户端实例
    """
    global _client_instance

    if _client_instance and _client_instance.running:
        logger.warning("[ws_client] Client already running")
        return _client_instance

    _client_instance = WSTunnelClient(
        gateway_url, owner_id,
        reply_in_thread=reply_in_thread,
        at_bot_only=at_bot_only,
        session_mode=session_mode,
        claude_commands=claude_commands,
        default_chat_dir=default_chat_dir,
        default_chat_follow_thread=default_chat_follow_thread,
        group_name_prefix=group_name_prefix,
        group_dissolve_days=group_dissolve_days
    )
    _client_instance.start()
    return _client_instance


def stop_ws_tunnel_client() -> None:
    """停止 WebSocket 隧道客户端"""
    global _client_instance

    if _client_instance:
        _client_instance.stop()
        _client_instance = None


def get_ws_tunnel_client() -> Optional[WSTunnelClient]:
    """获取客户端实例"""
    return _client_instance
