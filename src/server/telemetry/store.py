"""遥测数据存储

用于遥测服务端，记录活跃客户端、统计用户数据。

功能：
- 记录活跃客户端（client_id → 最后上报时间、版本、OS）
- client_id 速率限制（同一 client_id 每 10 分钟只能上报一次）
- IP 速率限制（同一 IP 每 10 分钟最多 10 次请求）
- 统计在线用户数、版本分布
- 定期清理过期数据（7 天未活跃视为离线）
- 持久化到 JSON 文件（原子写入，重启不丢数据）
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# client_id 速率限制间隔（秒）
RATE_LIMIT_INTERVAL = 600  # 10 分钟

# 客户端时间戳最大允许偏差（秒），超过则使用服务端时间
MAX_TIMESTAMP_DRIFT = 300  # 5 分钟

# IP 速率限制配置
IP_RATE_LIMIT_WINDOW = 600  # 时间窗口：10 分钟（与 client_id 限流对齐）
IP_RATE_LIMIT_MAX = 10  # 同一 IP 在窗口内最多请求次数

# 过期时间（秒）
EXPIRE_INTERVAL = 7 * 24 * 3600  # 7 天

# 清理间隔（秒）
CLEANUP_INTERVAL = 3600  # 1 小时

# 持久化刷盘间隔（秒），避免每次心跳都写磁盘
SAVE_INTERVAL = 60  # 1 分钟

# 持久化文件路径（与 store.py 同目录）
_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_FILE = os.path.join(_DATA_DIR, 'telemetry_clients.json')


class TelemetryStore:
    """遥测数据存储（单例模式）"""

    _instance: Optional['TelemetryStore'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'TelemetryStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        # 加锁保护初始化，防止竞态条件
        with TelemetryStore._lock:
            if self._initialized:
                return
            self._initialized = True

            # IP 速率限制数据（纯内存，无需持久化）
            # 结构：ip -> {'count': 窗口内请求次数, 'window_start': 窗口起始时间戳}
            self._ip_requests: Dict[str, Dict[str, int]] = {}

            # 文件操作锁
            self._file_lock = threading.Lock()

            # 脏标记 + 上次刷盘时间（用于延迟批量写入）
            self._dirty = False
            self._last_save_time = 0

            # 客户端数据：client_id -> {last_seen, version, os, repo_url, first_seen}（持久化到文件）
            self._clients = self._load()

            # 清理线程控制
            self._stop_event = threading.Event()
            self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            self._cleanup_thread.start()
            logger.info("[telemetry] Store initialized, loaded %d clients", len(self._clients))

    @classmethod
    def get_instance(cls) -> 'TelemetryStore':
        """获取单例实例"""
        return cls()

    def stop(self) -> None:
        """停止清理线程，刷盘未保存的数据"""
        self._stop_event.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
        # 退出前刷盘
        with self._file_lock:
            if self._dirty:
                self._save()
                self._dirty = False
        logger.debug("[telemetry] Store stopped")

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """从文件加载客户端数据"""
        if not os.path.exists(_DATA_FILE):
            return {}

        try:
            with open(_DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                logger.warning("[telemetry] Invalid store data format, starting fresh")
                return {}
        except json.JSONDecodeError:
            logger.warning("[telemetry] Invalid JSON in store file, starting fresh")
            return {}
        except (IOError, OSError) as e:
            logger.error("[telemetry] Failed to load store file: %s", e)
            return {}

    def _save(self) -> bool:
        """原子写入客户端数据到文件"""
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(self._clients, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, _DATA_FILE)
            return True
        except (IOError, OSError) as e:
            logger.error("[telemetry] Failed to save store file: %s", e)
            # 清理临时文件
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return False

    def record_heartbeat(
        self,
        client_id: str,
        version: str,
        os_name: str,
        repo_url: str = '',
        timestamp: Optional[int] = None,
        client_ip: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """记录心跳上报

        Args:
            client_id: 客户端 ID
            version: 客户端版本
            os_name: 操作系统
            repo_url: git 远程仓库地址
            timestamp: 上报时间戳（默认当前时间）
            client_ip: 客户端 IP 地址（用于 IP 限流）

        Returns:
            (error_code, error_message):
                error_code=None 表示记录成功
                error_code='bad_request' 表示请求数据异常
                error_code='rate_limited' 表示被速率限制
        """
        with self._file_lock:
            now = int(time.time())

            # 校验客户端时间戳，偏差过大则拒绝（可能是恶意流量或时钟异常）
            if timestamp is not None and abs(now - timestamp) > MAX_TIMESTAMP_DRIFT:
                logger.debug(
                    "[telemetry] Discarded heartbeat: timestamp drift too large "
                    "(client=%d, server=%d, diff=%ds)",
                    timestamp, now, abs(now - timestamp)
                )
                return 'bad_request', "Timestamp drift too large"

            # client_id 速率限制检查（纯读检查，无副作用，优先执行）
            if client_id in self._clients:
                last_seen = self._clients[client_id].get('last_seen', 0)
                if now - last_seen < RATE_LIMIT_INTERVAL:
                    remaining = RATE_LIMIT_INTERVAL - (now - last_seen)
                    logger.debug(
                        "[telemetry] Rate limited: client_id=%s, remaining=%ds",
                        client_id[:20], remaining
                    )
                    return 'rate_limited', "Rate limited, retry after %d seconds" % remaining

            # IP 速率限制检查（有副作用：count+1，放在 client_id 检查之后）
            if client_ip:
                ip_data = self._ip_requests.get(client_ip, {'count': 0, 'window_start': now})

                # 检查是否需要重置窗口
                if now - ip_data['window_start'] >= IP_RATE_LIMIT_WINDOW:
                    ip_data = {'count': 0, 'window_start': now}

                # 检查是否超过限制
                if ip_data['count'] >= IP_RATE_LIMIT_MAX:
                    remaining = IP_RATE_LIMIT_WINDOW - (now - ip_data['window_start'])
                    logger.debug(
                        "[telemetry] IP rate limited: ip=%s, count=%d, remaining=%ds",
                        client_ip, ip_data['count'], remaining
                    )
                    return 'rate_limited', "IP rate limited, retry after %d seconds" % remaining

                # 增加计数
                ip_data['count'] += 1
                self._ip_requests[client_ip] = ip_data

            # 记录数据（last_seen 统一使用服务端时间，避免客户端时钟偏差绕过限流）
            self._clients[client_id] = {
                'last_seen': now,
                'version': version,
                'os': os_name,
                'repo_url': repo_url,
                'first_seen': self._clients.get(client_id, {}).get('first_seen', now),
            }

            # 标记脏数据，按间隔刷盘（避免每次心跳都写磁盘）
            self._dirty = True
            if now - self._last_save_time >= SAVE_INTERVAL:
                self._save()
                self._dirty = False
                self._last_save_time = now

            logger.debug(
                "[telemetry] Recorded heartbeat: client_id=%s, version=%s, os=%s",
                client_id[:20], version, os_name
            )
            return None, None

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息

        Returns:
            统计数据：{
                'total_clients': 总客户端数,
                'active_clients_24h': 24小时内活跃数,
                'active_clients_7d': 7天内活跃数,
                'version_distribution': 版本分布,
                'os_distribution': OS 分布
            }
        """
        with self._file_lock:
            now = int(time.time())
            day_ago = now - 24 * 3600
            week_ago = now - 7 * 24 * 3600

            total = len(self._clients)
            active_24h = 0
            active_7d = 0
            version_dist: Dict[str, int] = {}
            os_dist: Dict[str, int] = {}

            for data in self._clients.values():
                last_seen = data.get('last_seen', 0)

                if last_seen >= day_ago:
                    active_24h += 1
                if last_seen >= week_ago:
                    active_7d += 1

                version = data.get('version', 'unknown')
                version_dist[version] = version_dist.get(version, 0) + 1

                os_name = data.get('os', 'unknown')
                os_dist[os_name] = os_dist.get(os_name, 0) + 1

            return {
                'total_clients': total,
                'active_clients_24h': active_24h,
                'active_clients_7d': active_7d,
                'version_distribution': version_dist,
                'os_distribution': os_dist,
            }

    def _cleanup_loop(self) -> None:
        """定期清理过期数据（启动时立即执行一次，之后按间隔执行）"""
        while not self._stop_event.is_set():
            self._cleanup()
            # 用 wait 替代 sleep，可被 stop_event 中断
            self._stop_event.wait(CLEANUP_INTERVAL)

    def _cleanup(self) -> None:
        """清理过期数据"""
        with self._file_lock:
            now = int(time.time())
            clients_expired = False

            # 清理过期客户端
            expired_clients = [
                client_id for client_id, data in self._clients.items()
                if now - data.get('last_seen', 0) > EXPIRE_INTERVAL
            ]

            for client_id in expired_clients:
                del self._clients[client_id]

            if expired_clients:
                clients_expired = True
                logger.info(
                    "[telemetry] Cleaned up %d expired clients",
                    len(expired_clients)
                )

            # 清理过期的 IP 请求数据（窗口过期超过 5 分钟的）
            expired_ips = [
                ip for ip, data in self._ip_requests.items()
                if now - data.get('window_start', 0) > IP_RATE_LIMIT_WINDOW + 300
            ]

            for ip in expired_ips:
                del self._ip_requests[ip]

            if expired_ips:
                logger.debug(
                    "[telemetry] Cleaned up %d expired IP records",
                    len(expired_ips)
                )

            # 有数据变更（清理过期 或 未刷盘的心跳）则持久化
            if clients_expired or self._dirty:
                self._save()
                self._dirty = False
                self._last_save_time = int(time.time())
