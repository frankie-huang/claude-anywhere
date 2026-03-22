"""
Card Cache Service - 卡片缓存服务

功能：
    - 缓存待回调更新的原始飞书卡片 JSON
    - 基于 request_id 获取卡片内容
    - 使用 TTL 在写入时惰性清理旧缓存，避免缓存长期占用内存

说明：
    - TTL 仅用于内存回收，不作为读取时的强过期限制
    - get() 即使读取到已过期但尚未清理的项，也会正常返回
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class CardCache:
    """缓存待回调更新的原始卡片 JSON"""

    _instance: Optional['CardCache'] = None
    _singleton_lock = threading.Lock()
    TTL_SECONDS = 24 * 60 * 60  # 1 天

    @classmethod
    def initialize(cls):
        """初始化单例实例"""
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
                logger.info("CardCache initialized (ttl=%ss)", cls.TTL_SECONDS)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['CardCache']:
        """获取单例实例"""
        return cls._instance

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def _cleanup_expired_locked(self):
        """清理所有过期缓存（需在持锁状态下调用）"""
        now = time.time()
        expired_keys = []
        for request_id, item in self._cache.items():
            if item.get('expire_at', 0) <= now:
                expired_keys.append(request_id)

        for request_id in expired_keys:
            del self._cache[request_id]

        if expired_keys:
            logger.debug("CardCache cleaned %d expired item(s)", len(expired_keys))

    def set(self, request_id: str, card_json: str):
        """写入卡片缓存"""
        if not request_id or not card_json:
            return

        with self._lock:
            self._cleanup_expired_locked()
            self._cache[request_id] = {
                'card_json': card_json,
                'expire_at': time.time() + self.TTL_SECONDS
            }
        logger.debug("CardCache stored card for request_id=%s", request_id)

    def get(self, request_id: str) -> Optional[str]:
        """读取卡片缓存，未命中返回 None

        注意：这里不检查 TTL，也不触发清理。
        TTL 仅在 set() 时用于惰性回收旧缓存，避免回调时因超时而降级。
        """
        if not request_id:
            return None

        with self._lock:
            item = self._cache.get(request_id)
            if not item:
                return None
            return item.get('card_json')

    def delete(self, request_id: str):
        """删除指定卡片缓存"""
        if not request_id:
            return

        with self._lock:
            if request_id in self._cache:
                del self._cache[request_id]
                logger.debug("CardCache deleted card for request_id=%s", request_id)
