"""WebSocket 协议最小实现

基于 Python 标准库实现的最小 WebSocket 协议（RFC 6455）。
仅支持：text frame、ping/pong、close frame。不支持 binary frame、分片、扩展。

使用方式：
    # 服务端
    sock = ws_server_handshake(handler)  # 在 do_GET 中调用
    while True:
        opcode, payload = ws_recv(sock)
        if opcode == OPCODE_TEXT:
            ws_send_text(sock, "response")
        elif opcode == OPCODE_PING:
            ws_send_pong(sock, payload)
        elif opcode == OPCODE_CLOSE:
            break

    # 客户端
    sock = ws_client_connect(url)
    ws_send_text(sock, json.dumps({"type": "hello"}))
    opcode, payload = ws_recv(sock)
"""

import base64
import hashlib
import logging
import os
import socket
import ssl
import struct
import threading
from typing import Dict, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# WebSocket opcodes
OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

# Close codes
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_INTERNAL_ERROR = 1011

# 最大帧大小（防止内存耗尽攻击）
MAX_FRAME_SIZE = 10 * 1024 * 1024  # 10MB

# socket ID → 是否为客户端模式的映射（客户端发送帧需要 mask，服务端不需要）
# 使用 id(sock) 作为 key，避免 OS 文件描述符复用导致状态错乱
_WS_CLIENT_MODE_MAP: Dict[int, bool] = {}
_WS_CLIENT_MODE_LOCK = threading.Lock()

# per-socket 发送锁：防止多线程并发写同一个 socket 导致帧交错
# 在 _send_frame 内部自动加锁，上层调用者无需关心
_WS_SEND_LOCK_MAP: Dict[int, threading.Lock] = {}
_WS_SEND_LOCK_MAP_LOCK = threading.Lock()


def _compute_accept_key(key: str) -> str:
    """计算 Sec-WebSocket-Accept 值

    RFC 6455: accept = base64(sha1(key + GUID))
    """
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    sha1 = hashlib.sha1((key + GUID).encode('utf-8')).digest()
    return base64.b64encode(sha1).decode('utf-8')


def _mask_data(data: bytes, mask_key: bytes) -> bytes:
    """使用 mask key 对数据进行 XOR 掩码

    注：逐字节 XOR 对中小 payload 场景足够（KB 级），
    无需 batch XOR 优化（int.from_bytes 按 4 字节块处理）。
    """
    result = bytearray(len(data))
    for i in range(len(data)):
        result[i] = data[i] ^ mask_key[i % 4]
    return bytes(result)


# =============================================================================
# 服务端握手
# =============================================================================

def ws_server_handshake(handler: Any) -> socket.socket:
    """服务端 WebSocket 握手

    在 BaseHTTPRequestHandler.do_GET 中调用。
    验证 Upgrade 请求，发送 101 响应，返回 raw socket。

    Args:
        handler: BaseHTTPRequestHandler 实例

    Returns:
        socket.socket: 已完成握手的原始 socket

    Raises:
        ValueError: 握手失败
    """
    # 检查请求头
    headers = getattr(handler, 'headers', None)
    if headers is None:
        raise ValueError("No headers found")

    if headers.get('Upgrade', '').lower() != 'websocket':
        raise ValueError("Not a WebSocket upgrade request")

    # 检查 Sec-WebSocket-Key
    ws_key = headers.get('Sec-WebSocket-Key', '')
    if not ws_key:
        raise ValueError("Missing Sec-WebSocket-Key")

    # 检查 Sec-WebSocket-Version
    ws_version = headers.get('Sec-WebSocket-Version', '')
    if ws_version != '13':
        raise ValueError(f"Unsupported WebSocket version: {ws_version}")

    # 计算接受密钥
    accept_key = _compute_accept_key(ws_key)

    # 发送 101 响应
    handler.send_response(101)
    handler.send_header('Upgrade', 'websocket')
    handler.send_header('Connection', 'Upgrade')
    handler.send_header('Sec-WebSocket-Accept', accept_key)
    handler.end_headers()

    # 获取底层 socket
    # BaseHTTPRequestHandler 的 socket 在 handler.connection 或通过 request 获取
    if hasattr(handler, 'connection'):
        sock = handler.connection
    elif hasattr(handler, 'request'):
        sock = handler.request
    else:
        raise ValueError("Cannot get underlying socket")

    # 标记为服务端模式（发送帧不需要 mask）
    with _WS_CLIENT_MODE_LOCK:
        _WS_CLIENT_MODE_MAP[id(sock)] = False

    logger.info("[ws] Server handshake completed")
    return sock


# =============================================================================
# 客户端握手
# =============================================================================

def ws_client_connect(url: str, user_agent: str = "Python-WS-Client/1.0",
                      extra_headers: Optional[Dict[str, str]] = None) -> socket.socket:
    """客户端 WebSocket 握手

    发送 HTTP Upgrade 请求，验证响应，返回连接 socket。

    Args:
        url: WebSocket URL，格式 ws://host:port/path?query
        user_agent: User-Agent 请求头的值（调用方可传入自己的标识）
        extra_headers: 额外的 HTTP 请求头

    Returns:
        socket.socket: 已完成握手的 socket

    Raises:
        ValueError: URL 格式错误或握手失败
        ConnectionError: 连接失败
    """
    # 解析 URL
    if url.startswith('ws://'):
        host_port = url[5:]
        use_ssl = False
    elif url.startswith('wss://'):
        host_port = url[6:]
        use_ssl = True
    else:
        raise ValueError(f"Invalid WebSocket URL: {url}")

    # 分离 host:port 和 path?query
    # 先按 '/' 分离（path 起始），再处理无 path 但有 query 的情况
    if '/' in host_port:
        host_port_part, path = host_port.split('/', 1)
        path = '/' + path
    elif '?' in host_port:
        # 无 path 但有 query：ws://host:port?key=val
        host_port_part, query = host_port.split('?', 1)
        path = '/?' + query
    else:
        host_port_part = host_port
        path = '/'

    # 解析 host 和 port
    if ':' in host_port_part:
        host, port_str = host_port_part.split(':', 1)
        port = int(port_str)
    else:
        host = host_port_part
        port = 443 if use_ssl else 80

    # 生成 WebSocket Key（RFC 6455 要求标准 base64 编码）
    ws_key = base64.b64encode(os.urandom(16)).decode('utf-8')

    # 构造 HTTP 请求
    request_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {ws_key}",
        "Sec-WebSocket-Version: 13",
        f"User-Agent: {user_agent}",
    ]

    # 添加额外头
    if extra_headers:
        for key, value in extra_headers.items():
            request_lines.append(f"{key}: {value}")

    request_lines.append("")  # 空行
    request_lines.append("")
    request = "\r\n".join(request_lines).encode('utf-8')

    # 建立 TCP 连接
    # 注：当前仅支持 IPv4，暂不支持 IPv6 地址（如 ws://[::1]:8080/）
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30)  # 连接超时

    try:
        sock.connect((host, port))
    except socket.error as e:
        sock.close()
        raise ConnectionError(f"Failed to connect to {host}:{port}: {e}")

    # 如果是 wss://，包装 SSL
    if use_ssl:
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=host)

    # 发送握手请求
    sock.sendall(request)

    # 接收响应（限制最大 16KB，防止恶意服务器耗尽内存）
    max_response_size = 16384
    response = b''
    while b'\r\n\r\n' not in response:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise ConnectionError("Connection closed during handshake")
        response += chunk
        if len(response) > max_response_size:
            sock.close()
            raise ValueError(f"Handshake response too large (>{max_response_size} bytes)")

    # 解析响应
    header_end = response.find(b'\r\n\r\n')
    header_part = response[:header_end].decode('utf-8')

    lines = header_part.split('\r\n')
    status_line = lines[0]

    # 检查状态码（101 Switching Protocols）
    # 格式: HTTP/x.x 101 <reason>
    parts = status_line.split()
    if len(parts) < 2 or parts[1] != '101':
        sock.close()
        raise ValueError(f"WebSocket handshake failed: {status_line}")

    # 检查响应头
    headers_lower = {}
    for line in lines[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers_lower[key.strip().lower()] = value.strip()

    # 验证 Sec-WebSocket-Accept
    expected_accept = _compute_accept_key(ws_key)
    actual_accept = headers_lower.get('sec-websocket-accept', '')

    if actual_accept != expected_accept:
        sock.close()
        raise ValueError(f"Invalid Sec-WebSocket-Accept: {actual_accept}")

    # 标记为客户端模式（发送帧需要 mask）
    with _WS_CLIENT_MODE_LOCK:
        _WS_CLIENT_MODE_MAP[id(sock)] = True

    logger.info("[ws] Client handshake completed: %s:%s", host, port)
    return sock


# =============================================================================
# 帧发送
# =============================================================================

def ws_send_text(sock: socket.socket, data: str) -> None:
    """发送 text frame

    客户端模式下会自动 mask。
    """
    _send_frame(sock, OPCODE_TEXT, data.encode('utf-8'))


def ws_send_ping(sock: socket.socket, data: bytes = b'') -> None:
    """发送 ping frame"""
    _send_frame(sock, OPCODE_PING, data)


def ws_send_pong(sock: socket.socket, data: bytes = b'') -> None:
    """发送 pong frame"""
    _send_frame(sock, OPCODE_PONG, data)


def ws_send_close(sock: socket.socket, code: int = CLOSE_NORMAL, reason: str = '') -> None:
    """发送 close frame

    Args:
        sock: WebSocket socket
        code: 关闭码
        reason: 关闭原因
    """
    payload = struct.pack('!H', code) + reason.encode('utf-8')
    _send_frame(sock, OPCODE_CLOSE, payload)


def _get_send_lock(sock: socket.socket) -> threading.Lock:
    """获取 socket 对应的发送锁（按需创建）"""
    sid = id(sock)
    with _WS_SEND_LOCK_MAP_LOCK:
        lock = _WS_SEND_LOCK_MAP.get(sid)
        if lock is None:
            lock = threading.Lock()
            _WS_SEND_LOCK_MAP[sid] = lock
        return lock


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """发送 WebSocket 帧

    内部自动加 per-socket 发送锁，上层调用者无需额外加锁。

    Args:
        sock: socket
        opcode: 操作码
        payload: 负载数据
    """
    # 从字典获取是否为客户端模式（未知 socket 默认当客户端处理，加 mask 更安全）
    with _WS_CLIENT_MODE_LOCK:
        is_client = _WS_CLIENT_MODE_MAP.get(id(sock), True)

    fin = 0x80  # FIN=1, 单帧消息
    rsv = 0x00  # 无扩展

    first_byte = fin | rsv | opcode
    payload_len = len(payload)

    # 构造帧头
    if payload_len <= 125:
        header = bytes([first_byte, payload_len])
    elif payload_len <= 65535:
        header = bytes([first_byte, 126]) + struct.pack('!H', payload_len)
    else:
        header = bytes([first_byte, 127]) + struct.pack('!Q', payload_len)

    # 客户端需要 mask
    if is_client:
        mask_key = os.urandom(4)
        masked_payload = _mask_data(payload, mask_key)
        # 设置 MASK 位（second byte 的最高位）
        # 注意：MASK 位与 payload length indicator 不冲突：
        #   - length <= 125: header[1] = length, | 0x80 后高位为 MASK 标志
        #   - length 126: header[1] = 126, | 0x80 = 254, 接收方 254 & 0x7F = 126（正确）
        #   - length 127: header[1] = 127, | 0x80 = 255, 接收方 255 & 0x7F = 127（正确）
        # 扩展长度字节 header[2:] 不受影响
        header = bytes([header[0], header[1] | 0x80]) + header[2:]
        frame = header + mask_key + masked_payload
    else:
        frame = header + payload

    with _get_send_lock(sock):
        sock.sendall(frame)


# =============================================================================
# 帧接收
# =============================================================================

def ws_recv(sock: socket.socket, timeout: Optional[float] = None) -> Tuple[int, bytes]:
    """接收一帧

    Args:
        sock: WebSocket socket
        timeout: 接收超时（秒），None 表示不设置

    Returns:
        (opcode, payload): 操作码和负载数据

    Raises:
        socket.timeout: 接收超时
        ConnectionError: 连接关闭
        ValueError: 协议错误
    """
    if timeout is not None:
        old_timeout = sock.gettimeout()
        sock.settimeout(timeout)
    else:
        old_timeout = None

    try:
        # 读取前两个字节
        first_two = _recv_exact(sock, 2)
        first_byte = first_two[0]
        second_byte = first_two[1]

        opcode = first_byte & 0x0F
        # fin = (first_byte & 0x80) >> 7  # 暂不使用
        # rsv = (first_byte & 0x70) >> 4  # 暂不处理扩展

        masked = (second_byte & 0x80) >> 7
        payload_len = second_byte & 0x7F

        # 读取扩展长度
        if payload_len == 126:
            ext_len = _recv_exact(sock, 2)
            payload_len = struct.unpack('!H', ext_len)[0]
        elif payload_len == 127:
            ext_len = _recv_exact(sock, 8)
            payload_len = struct.unpack('!Q', ext_len)[0]

        # 检查帧大小限制
        if payload_len > MAX_FRAME_SIZE:
            raise ValueError(f"Frame too large: {payload_len} bytes (max {MAX_FRAME_SIZE})")

        # 读取 mask key（服务端接收的客户端帧有 mask）
        mask_key = b''
        if masked:
            mask_key = _recv_exact(sock, 4)

        # 读取 payload
        payload = _recv_exact(sock, payload_len)

        # 解 mask
        if masked:
            payload = _mask_data(payload, mask_key)

        return opcode, payload

    finally:
        if timeout is not None:
            sock.settimeout(old_timeout)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    """精确接收指定字节数

    注：bytes 拼接对中小 payload 场景足够（KB 级，通常 1-2 次 recv），
    无需 bytearray + memoryview 优化。
    """
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError(f"Connection closed, received {len(data)}/{length} bytes")
        data += chunk
    return data


# =============================================================================
# 高级 API
# =============================================================================

def ws_recv_text(sock: socket.socket, timeout: Optional[float] = None) -> Optional[str]:
    """接收一条文本消息

    自动处理 ping/pong 和 close。如果收到 close frame，返回 None。

    Args:
        sock: WebSocket socket
        timeout: 接收超时

    Returns:
        文本消息或 None（连接关闭）
    """
    while True:
        try:
            opcode, payload = ws_recv(sock, timeout)
        except socket.timeout:
            raise
        except ConnectionError:
            return None

        if opcode == OPCODE_TEXT:
            return payload.decode('utf-8')
        elif opcode == OPCODE_PING:
            # 自动回复 pong
            ws_send_pong(sock, payload)
        elif opcode == OPCODE_CLOSE:
            # 回复 close 并返回
            try:
                ws_send_close(sock)
            except Exception:
                pass
            return None
        elif opcode == OPCODE_PONG:
            # 忽略 pong
            pass
        elif opcode == OPCODE_CONT or opcode == OPCODE_BINARY:
            # 不支持续帧和 binary，发送 close 并返回 None
            logger.warning("[ws] Received unsupported opcode: %d, closing connection", opcode)
            ws_send_close(sock, CLOSE_UNSUPPORTED, "unsupported opcode")
            return None
        else:
            logger.warning("[ws] Unknown opcode: %d", opcode)


def ws_close(sock: socket.socket, code: int = CLOSE_NORMAL, reason: str = '') -> None:
    """优雅关闭 WebSocket 连接

    发送 close frame 并等待对方响应（最多等待 2 秒）。
    """
    try:
        ws_send_close(sock, code, reason)

        # 等待对方 close frame
        sock.settimeout(2.0)
        try:
            ws_recv(sock)
            # 不管收到什么，都关闭连接
        except socket.timeout:
            pass
        except Exception:
            pass
    except Exception as e:
        logger.debug("[ws] Error during close: %s", e)
    finally:
        # 清理协议层状态，防止内存泄漏
        cleanup_socket_state(sock)
        try:
            sock.close()
        except Exception:
            pass


def cleanup_socket_state(sock: socket.socket) -> None:
    """清理 socket 的协议层状态（client mode 映射和发送锁）"""
    sid = id(sock)
    try:
        with _WS_CLIENT_MODE_LOCK:
            _WS_CLIENT_MODE_MAP.pop(sid, None)
    except Exception:
        pass
    try:
        with _WS_SEND_LOCK_MAP_LOCK:
            _WS_SEND_LOCK_MAP.pop(sid, None)
    except Exception:
        pass
