"""
WebSocket 隧道处理器

处理 WebSocket 隧道连接的生命周期：握手、认证、消息循环。
从 http_handler.py 中拆分，专注 WS 隧道逻辑。

线程模型：
    - 每个 WS 连接占用 ThreadedHTTPServer 的一个工作线程，连接期间不释放
    - 消息循环在该工作线程中运行（阻塞在 ws_recv）
    - 对于预期使用场景（每个用户 1-2 个 callback 后端）完全够用
    - 如需支持大规模多租户（成百上千并发 WS），应考虑 asyncio 重构

锁的使用：
    - WebSocketRegistry._connections_lock: 保护已认证连接字典
    - WebSocketRegistry._pending_lock: 保护 pending 连接字典及相关状态
    - ws_protocol._WS_SEND_LOCK_MAP: per-socket 发送锁，在 _send_frame 内部自动加锁

连接状态流转：
    pending (等待授权) → authenticated (已授权，可路由请求) → closed

超时与清理机制（客户端每 30 秒发送心跳 ping）：

    +-------------+-------------------+--------------------------+------------------------+
    | 状态        | 心跳正常          | 心跳停止                 | 额外清理               |
    +=============+===================+==========================+========================+
    | pending     | 最多等待 10 分钟  | read timeout 90s 后退出  | cleanup_expired_pending|
    | (等待授权)  | 由 cleanup 清理   | 或 cleanup 检测到无活动  | 每 30 秒检查一次       |
    +-------------+-------------------+--------------------------+------------------------+
    | authenticated| 连接保持         | read timeout 90s 后退出  | 无                     |
    | (已授权)    |                   | finally 块清理连接       |                        |
    +-------------+-------------------+--------------------------+------------------------+

    常量说明：
    - WS_READ_TIMEOUT = 90 (秒)
    - WS_PENDING_INACTIVE_TIMEOUT = 90 (秒，无活动)
    - WS_PENDING_MAX_WAIT_TIME = 600 (秒，10 分钟)
"""

import json
import logging
import socket
from typing import Any, Dict, List

from handlers.utils import send_json

logger = logging.getLogger(__name__)

# 服务端 read timeout（秒）
WS_READ_TIMEOUT = 90


def handle_ws_tunnel(handler: Any, params: Dict[str, List[str]]) -> None:
    """处理 WebSocket 隧道连接

    认证流程：
    1. 客户端连接时只传递 owner_id
    2. 握手后客户端发送 register 消息
    3. 网关检查绑定记录和 auth_token：
       - 有绑定 + token 匹配：同一终端重连，直接续期
       - 有绑定 + token 不匹配：新终端换绑，发飞书授权卡片
       - 无绑定：首次注册，发飞书授权卡片
    """
    from services.ws_protocol import ws_server_handshake, cleanup_socket_state
    from services.ws_registry import WebSocketRegistry

    owner_id = params.get('owner_id', [None])[0]

    if not owner_id:
        logger.warning("[ws/tunnel] Missing owner_id")
        send_json(handler, 400, {'error': 'Missing owner_id'})
        return

    # 检查是否是 WebSocket 升级请求
    if handler.headers.get('Upgrade', '').lower() != 'websocket':
        logger.warning("[ws/tunnel] Not a WebSocket upgrade request")
        send_json(handler, 400, {'error': 'Expected WebSocket upgrade'})
        return

    registry = WebSocketRegistry.get_instance()
    if registry is None:
        logger.error("[ws/tunnel] WebSocketRegistry not initialized")
        send_json(handler, 500, {'error': 'Server not ready'})
        return

    # 执行握手
    try:
        sock = ws_server_handshake(handler)
    except ValueError as e:
        # ValueError 在发送 101 之前抛出（头部验证失败），可安全返回 HTTP 错误
        logger.error("[ws/tunnel] Handshake failed: %s", e)
        send_json(handler, 400, {'error': str(e)})
        return
    except Exception as e:
        # 其他异常可能在发送 101 过程中抛出，此时 HTTP 响应可能已部分发送
        # 不能再调用 send_json，直接关闭连接
        logger.error("[ws/tunnel] Handshake error: %s", e)
        try:
            handler.connection.close()
        except Exception:
            pass
        return

    # 握手成功后，_WS_CLIENT_MODE_MAP 已写入 id(sock)。
    # _process_tunnel_connection 正常路径会进入 _ws_message_loop（其 finally 负责清理），
    # 但早期退出路径（超时、协议错误、ws_send_auth_ok 失败等）需要此处 finally
    # 兜底清理，防止 pending 条目和协议层状态泄漏。
    try:
        _process_tunnel_connection(sock, handler, owner_id, registry)
    finally:
        # cleanup_connection 是幂等的（通过 sock 身份比较，不会误删已被替换的新连接），
        # 即使 _ws_message_loop 已清理过，此处再调一次也无害
        registry.cleanup_connection(owner_id, sock)
        cleanup_socket_state(sock)
        try:
            sock.close()
        except Exception:
            pass


def _process_tunnel_connection(sock: socket.socket, handler: Any, owner_id: str, registry: Any) -> None:
    """处理握手后的隧道连接：接收 register 消息，分发认证路径，进入消息循环

    认证路径：
    - token 匹配：同一终端重连，直接续期（发 auth_ok，等 auth_ok_ack）
    - token 不匹配/缺失：新终端换绑，发飞书授权卡片
    - 无绑定记录：首次注册，发飞书授权卡片

    Args:
        sock: 已完成握手的 WebSocket socket
        handler: HTTP handler（用于获取 client_ip）
        owner_id: 飞书用户 ID
        registry: WebSocketRegistry 实例
    """
    from services.ws_protocol import ws_recv, ws_send_close, OPCODE_TEXT
    from config import FEISHU_APP_SECRET
    from services.binding_store import BindingStore

    client_ip = handler.get_client_ip()
    logger.info("[ws/tunnel] Connection established: owner_id=%s, ip=%s", owner_id, client_ip)

    # 等待首条消息（register），设置较短超时
    sock.settimeout(30)
    try:
        opcode, payload = ws_recv(sock)
    except socket.timeout:
        logger.warning("[ws/tunnel] Timeout waiting for auth message from %s", owner_id)
        ws_send_close(sock, 1002, 'auth timeout')
        return
    except Exception as e:
        logger.warning("[ws/tunnel] Error receiving auth message: %s", e)
        return

    if opcode != OPCODE_TEXT:
        logger.warning("[ws/tunnel] Expected text message, got opcode %d", opcode)
        ws_send_close(sock, 1002, 'expected text message')
        return

    # 解析首条消息
    try:
        msg = json.loads(payload.decode('utf-8'))
    except json.JSONDecodeError as e:
        logger.warning("[ws/tunnel] Invalid JSON in auth message: %s", e)
        ws_send_close(sock, 1002, 'invalid JSON')
        return

    msg_type = msg.get('type')
    msg_owner_id = msg.get('owner_id', owner_id)

    # 验证 owner_id 一致性
    if msg_owner_id != owner_id:
        logger.warning("[ws/tunnel] owner_id mismatch: URL=%s, msg=%s", owner_id, msg_owner_id)
        ws_send_close(sock, 1002, 'owner_id mismatch')
        return

    # === 处理 register 消息 ===
    if msg_type == 'register':
        # 提取 register 消息中的配置参数
        msg_at_bot_only = msg.get('at_bot_only')
        # 入口转换：优先使用 session_mode，旧客户端用 reply_in_thread 映射
        msg_session_mode = msg.get('session_mode', '')
        if not msg_session_mode or msg_session_mode not in ('message', 'thread', 'group'):
            msg_session_mode = 'thread' if msg.get('reply_in_thread', False) else 'message'
        msg_claude_commands = msg.get('claude_commands')
        msg_default_chat_dir = msg.get('default_chat_dir', '')
        msg_default_chat_follow_thread = msg.get('default_chat_follow_thread', True)
        msg_group_name_prefix = msg.get('group_name_prefix')
        msg_group_dissolve_days = msg.get('group_dissolve_days')

        # 提取客户端携带的 auth_token（用于比对是否为同一终端）
        msg_auth_token = msg.get('auth_token', '')

        # 检查是否已有绑定记录
        binding_store = BindingStore.get_instance()
        existing_binding = binding_store.get(owner_id) if binding_store else None

        if existing_binding:
            # 已有绑定，比对 auth_token 判断续期/换绑
            bound_auth_token = existing_binding.get('auth_token', '')

            if msg_auth_token and msg_auth_token == bound_auth_token:
                # auth_token 匹配：同一终端重连，直接续期
                logger.info("[ws/tunnel] auth_token matched, renewing for %s", owner_id)
                from services.auth_token import generate_auth_token

                new_token = generate_auth_token(FEISHU_APP_SECRET, owner_id)

                # 添加到 pending 并暂存 auth_token 和绑定参数，
                # 旧连接保持不动（继续路由请求），auth_ok_ack 收到后由 promote_pending 原子替换
                request_id = registry.add_pending(owner_id, sock, client_ip)
                if not request_id:
                    ws_send_close(sock, 1008, 'too many pending connections')
                    return
                registry.set_pending_auth_token(owner_id, request_id, new_token)
                registry.set_pending_binding_params(owner_id, request_id, {
                    'client_ip': client_ip,
                    'at_bot_only': msg_at_bot_only,
                    'session_mode': msg_session_mode,
                    'claude_commands': msg_claude_commands,
                    'default_chat_dir': msg_default_chat_dir,
                    'default_chat_follow_thread': msg_default_chat_follow_thread,
                    'group_name_prefix': msg_group_name_prefix,
                    'group_dissolve_days': msg_group_dissolve_days
                })

                # 发送新 token，等待消息循环中的 auth_ok_ack 完成认证
                from handlers.register import ws_send_auth_ok
                ws_send_auth_ok(sock, new_token)

                # 进入消息循环（pending 状态，等待 auth_ok_ack）
                _ws_message_loop(sock, owner_id, registry, is_pending=True, request_id=request_id)
                return
            else:
                # auth_token 不匹配或缺失：新终端，触发换绑授权卡片
                if not registry.check_card_cooldown(owner_id):
                    logger.warning("[ws/tunnel] Card cooldown for %s, rejecting", owner_id)
                    ws_send_close(sock, 1008, 'too many requests')
                    return

                old_ip = existing_binding.get('registered_ip', '')
                logger.info(
                    "[ws/tunnel] auth_token mismatch for %s (has_token=%s), triggering rebind. old_ip=%s, new_ip=%s",
                    owner_id, bool(msg_auth_token), old_ip, client_ip
                )

                request_id = registry.add_pending(owner_id, sock, client_ip)
                if not request_id:
                    ws_send_close(sock, 1008, 'too many pending connections')
                    return

                from handlers.register import handle_ws_rebind_registration
                card_sent = handle_ws_rebind_registration(
                    owner_id, request_id, client_ip, old_ip,
                    at_bot_only=msg_at_bot_only,
                    session_mode=msg_session_mode,
                    claude_commands=msg_claude_commands,
                    default_chat_dir=msg_default_chat_dir,
                    default_chat_follow_thread=msg_default_chat_follow_thread,
                    group_name_prefix=msg_group_name_prefix,
                    group_dissolve_days=msg_group_dissolve_days
                )
                if card_sent:
                    registry.set_card_cooldown(owner_id)

                # 进入消息循环（pending 状态，等待用户授权）
                _ws_message_loop(sock, owner_id, registry, is_pending=True, request_id=request_id)
                return

        # 无现有绑定，添加到 pending 并发送飞书授权卡片
        if not registry.check_card_cooldown(owner_id):
            logger.warning("[ws/tunnel] Card cooldown for %s, rejecting", owner_id)
            ws_send_close(sock, 1008, 'too many requests')
            return

        request_id = registry.add_pending(owner_id, sock, client_ip)
        if not request_id:
            ws_send_close(sock, 1008, 'too many pending connections')
            return

        # 触发 WS 注册流程（发送飞书授权卡片）
        from handlers.register import handle_ws_registration
        card_sent = handle_ws_registration(
            owner_id, request_id, client_ip,
            at_bot_only=msg_at_bot_only,
            session_mode=msg_session_mode,
            claude_commands=msg_claude_commands,
            default_chat_dir=msg_default_chat_dir,
            default_chat_follow_thread=msg_default_chat_follow_thread,
            group_name_prefix=msg_group_name_prefix,
            group_dissolve_days=msg_group_dissolve_days
        )
        if card_sent:
            registry.set_card_cooldown(owner_id)

        # 进入消息循环（pending 状态）
        _ws_message_loop(sock, owner_id, registry, is_pending=True, request_id=request_id)
        return

    # 未知消息类型
    logger.warning("[ws/tunnel] Unexpected first message type: %s", msg_type)
    ws_send_close(sock, 1002, 'expected register message')


def _ws_message_loop(sock: socket.socket, owner_id: str, registry: Any, is_pending: bool = False, request_id: str = '') -> None:
    """WebSocket 消息循环

    Args:
        sock: WebSocket socket
        owner_id: 飞书用户 ID
        registry: WebSocketRegistry 实例
        is_pending: 是否处于 pending 状态
        request_id: 请求 ID（pending 状态时用于更新活动时间）
    """
    from services.ws_protocol import ws_recv, ws_send_pong, ws_send_close, OPCODE_TEXT, OPCODE_PING, OPCODE_CLOSE

    sock.settimeout(WS_READ_TIMEOUT)
    logger.debug("[ws/tunnel] Message loop started for %s, timeout=%ds, pending=%s, request_id=%s", owner_id, WS_READ_TIMEOUT, is_pending, request_id)

    try:
        while True:
            try:
                opcode, payload = ws_recv(sock)
            except socket.timeout:
                logger.info("[ws/tunnel] Connection timed out for %s", owner_id)
                break
            except (ConnectionError, socket.error, OSError) as e:
                logger.info("[ws/tunnel] Connection closed for %s: %s", owner_id, e)
                break
            except ValueError as e:
                # 协议错误（如帧格式错误、帧过大）
                logger.warning("[ws/tunnel] Protocol error for %s: %s", owner_id, e)
                break
            except Exception as e:
                # 未预期的错误，记录完整堆栈
                logger.error("[ws/tunnel] Receive error for %s: %s", owner_id, e, exc_info=True)
                break

            # pending 状态处理
            if is_pending:
                status = registry.check_connection_status(owner_id, request_id, sock)
                if status == 'pending':
                    # 仍在 pending 状态，更新活动时间
                    registry.update_pending_activity(owner_id, request_id)
                elif status == 'authenticated':
                    # 已升级为 authenticated，更新局部变量
                    is_pending = False
                else:
                    # 已被移除（超时/清理），退出循环
                    logger.info("[ws/tunnel] Pending connection removed for %s, request_id=%s", owner_id, request_id)
                    break

            if opcode == OPCODE_TEXT:
                # 处理文本消息
                try:
                    msg = json.loads(payload.decode('utf-8'))
                    _handle_ws_message(sock, owner_id, msg, registry, request_id)
                except json.JSONDecodeError as e:
                    logger.warning("[ws/tunnel] Invalid JSON from %s: %s", owner_id, e)
                except Exception as e:
                    logger.error("[ws/tunnel] Message handling error for %s: %s", owner_id, e)

            elif opcode == OPCODE_PING:
                # 回复 pong
                ws_send_pong(sock, payload)

            elif opcode == OPCODE_CLOSE:
                logger.info("[ws/tunnel] Close frame received from %s", owner_id)
                try:
                    ws_send_close(sock)
                except Exception:
                    pass
                break

            else:
                logger.debug("[ws/tunnel] Ignoring opcode %d from %s", opcode, owner_id)

    finally:
        # 原子清理连接（从 pending 或 authenticated 中移除）
        # 传入 sock 防止误删已被替换的新连接
        registry.cleanup_connection(owner_id, sock)

        # cleanup_socket_state 和 sock.close 由上层 handle_ws_tunnel 的 finally 统一处理
        logger.info("[ws/tunnel] Connection closed for %s", owner_id)


def _handle_ws_message(sock: socket.socket, owner_id: str, msg: Dict[str, Any], registry: Any, request_id: str = '') -> None:
    """处理 WebSocket 文本消息

    Args:
        sock: WebSocket socket
        owner_id: 飞书用户 ID
        msg: 消息内容
        registry: WebSocketRegistry 实例
        request_id: 请求 ID（用于获取对应的 pending 数据）
    """
    msg_type = msg.get('type')

    if msg_type == 'response':
        # 响应消息，交给 registry 处理
        registry.handle_response(msg)

    elif msg_type == 'error':
        # 错误消息分两种：
        # - 带 id：请求处理失败的响应（如 handler 异常），路由到请求匹配让调用方立即拿到结果
        # - 不带 id：通用错误通知，仅记录日志
        if msg.get('id'):
            registry.handle_response(msg)
        logger.warning("[ws/tunnel] Error from %s: %s", owner_id, msg.get('message', ''))

    elif msg_type == 'auth_ok_ack':
        # 客户端确认收到 auth_ok（首次注册和续期共用路径）
        # 先更新 BindingStore（确保客户端已收到新 token 后再持久化），再 promote
        auth_token = registry.get_pending_auth_token(owner_id, request_id)
        if auth_token:
            # 更新 BindingStore（如果有暂存的绑定参数）
            binding_params = registry.get_pending_binding_params(owner_id, request_id)
            if binding_params:
                from services.binding_store import BindingStore
                store = BindingStore.get_instance()
                if store:
                    store.upsert(
                        owner_id, 'ws://tunnel', auth_token,
                        binding_params.get('client_ip', ''),
                        binding_params.get('at_bot_only'),
                        binding_params.get('session_mode', 'message'),
                        binding_params.get('claude_commands'),
                        binding_params.get('default_chat_dir', ''),
                        binding_params.get('default_chat_follow_thread', True),
                        binding_params.get('group_name_prefix'),
                        binding_params.get('group_dissolve_days')
                    )

            # 原子地从 pending 升级为已认证连接
            if registry.promote_pending(owner_id, request_id, auth_token):
                logger.info("[ws/tunnel] Received auth_ok_ack, connection promoted for %s, request_id=%s", owner_id, request_id)
            else:
                logger.warning("[ws/tunnel] Received auth_ok_ack but promote_pending failed for %s", owner_id)
        else:
            logger.warning("[ws/tunnel] Received auth_ok_ack but no pending auth_token for %s, request_id=%s", owner_id, request_id)

    elif msg_type == 'ping':
        # 应用层 ping（通常用 WS 原生 ping，但保留应用层支持）
        from services.ws_protocol import ws_send_text
        ws_send_text(sock, json.dumps({'type': 'pong'}))

    else:
        logger.debug("[ws/tunnel] Unknown message type '%s' from %s", msg_type, owner_id)


