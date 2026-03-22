"""遥测客户端

定期上报心跳到遥测中心，用于：
1. 统计活跃用户数量
2. 检测版本更新

设计原则：
- 硬编码遥测中心地址，可 opt-out
- 异步上报，失败静默
- 不影响用户正常使用
"""

import base64
import json
import logging
import platform
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from config import get_config
from telemetry.client_id import get_client_id
from telemetry.utils import get_version, get_repo_url

logger = logging.getLogger(__name__)

# 遥测中心地址（base64 编码，避免被自动化工具扫描或误改）
TELEMETRY_URL = base64.b64decode(
    'aHR0cHM6Ly9jbGF1ZGUubXlhZmVpLmNuL2FwaS90ZWxlbWV0cnkvaGVhcnRiZWF0'
).decode()

# 上报间隔（秒）
TELEMETRY_INTERVAL = 3600  # 1 小时

# HTTP 超时（秒）
HTTP_TIMEOUT = 5


class TelemetryService:
    """遥测客户端服务（单例模式）"""

    _instance: Optional['TelemetryService'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'TelemetryService':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        # 加锁保护初始化，防止竞态条件
        with TelemetryService._lock:
            if self._initialized:
                return
            self._initialized = True

            self._enabled = False
            self._running = False
            self._thread: Optional[threading.Thread] = None
            self._stop_event = threading.Event()

            # 本地缓存的最新版本（用于检测更新）
            self._cached_latest_version: Optional[str] = None
            self._update_message: Optional[str] = None

            # 预创建无代理的 opener（复用，避免每次心跳创建）
            no_proxy_handler = urllib.request.ProxyHandler({})
            self._opener = urllib.request.build_opener(no_proxy_handler)

    @classmethod
    def initialize(cls) -> 'TelemetryService':
        """初始化单例实例"""
        instance = cls()

        # 检查是否启用遥测（默认启用，'false'/'0' 表示禁用）
        telemetry_enabled = get_config('TELEMETRY_ENABLED', 'true').lower() not in ('false', '0')
        if telemetry_enabled and TELEMETRY_URL:
            instance._enabled = True
            logger.info("[telemetry] Enabled, will report to: %s", TELEMETRY_URL)
        else:
            if not telemetry_enabled:
                logger.info("[telemetry] Disabled by configuration")
            elif not TELEMETRY_URL:
                logger.info("[telemetry] Disabled: no telemetry URL configured")

        return instance

    @classmethod
    def get_instance(cls) -> 'TelemetryService':
        """获取单例实例"""
        return cls()

    @property
    def enabled(self) -> bool:
        """遥测是否启用"""
        return self._enabled

    def start_in_background(self) -> None:
        """在后台线程中启动遥测服务"""
        if not self._enabled:
            logger.debug("[telemetry] Not started: disabled")
            return

        if self._running:
            logger.warning("[telemetry] Already running")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.debug("[telemetry] Started background thread")

    def stop(self) -> None:
        """停止遥测服务"""
        self._running = False
        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

        logger.debug("[telemetry] Stopped")

    def _run_loop(self) -> None:
        """遥测循环"""
        # 首次启动后延迟一段时间再上报（避免启动时集中请求）
        initial_delay = 60  # 1 分钟
        self._stop_event.wait(initial_delay)
        if self._stop_event.is_set():
            return

        while self._running and not self._stop_event.is_set():
            self._report_heartbeat()

            # 等待下一次上报
            self._stop_event.wait(TELEMETRY_INTERVAL)

            if not self._running or self._stop_event.is_set():
                break

    def _report_heartbeat(self) -> None:
        """上报心跳"""
        if not TELEMETRY_URL:
            return

        try:
            client_id = get_client_id()
            version = get_version()
            os_name = platform.system().lower()

            payload = {
                'client_id': client_id,
                'version': version,
                'os': os_name,
                'repo_url': get_repo_url(),
                'timestamp': int(time.time()),
            }

            logger.debug("[telemetry] Reporting heartbeat: %s", payload)

            req = urllib.request.Request(
                TELEMETRY_URL,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )

            with self._opener.open(req, timeout=HTTP_TIMEOUT) as response:
                response_data = json.loads(response.read().decode('utf-8'))
                self._handle_response(response_data)

        except urllib.error.URLError as e:
            logger.debug("[telemetry] Network error: %s", e)
        except Exception as e:
            logger.debug("[telemetry] Error: %s", e)

    def _handle_response(self, data: Dict[str, Any]) -> None:
        """处理遥测响应"""
        if not data.get('success'):
            return

        latest_version = data.get('latest_version')
        update_available = data.get('update_available', False)
        message = data.get('message', '')

        if update_available and latest_version:
            # 避免重复提醒（同一版本只提醒一次）
            if self._cached_latest_version != latest_version:
                self._cached_latest_version = latest_version
                self._update_message = message
                logger.info(
                    "[telemetry] New version available: %s. %s",
                    latest_version, message or "Please update"
                )

    def get_update_info(self) -> Optional[Dict[str, str]]:
        """获取版本更新信息

        Returns:
            如果有更新，返回 {'latest_version': 'x.x.x', 'message': '...'}
            否则返回 None

        TODO: 当前此方法未被调用，版本更新信息仅打印日志。
              后续可考虑：
              1. 在 /status 端点返回更新信息
              2. 通过飞书 OpenAPI 发送消息通知用户
        """
        if self._cached_latest_version:
            return {
                'latest_version': self._cached_latest_version,
                'message': self._update_message or '',
            }
        return None
