"""TTL + 上限 FIFO 淘汰的通用内存缓存（进程内、线程安全）

用途：
    需要"本地缓存 + 定期失效 + 内存上限"的场景。无业务语义、无持久化。

语义：
    - TTL：过期条目在 strict_read=True 时读路径返回 miss 并就地删除；
      strict_read=False 时即使过期也返回命中（适用于回调容忍过期的场景）。
    - size 上限：写入时若超过 max_size，按 FIFO（created_at 最老）淘汰。
      覆盖写会 move_to_end，保证"插入顺序 == created_at 顺序"，淘汰 O(1)。
    - 线程安全：内部 RLock。

注意：
    - get 返回 None 与"value 本身为 None"不可区分；使用方避免存 None，
      或在业务层用 sentinel 区分。
    - 不启后台线程；过期条目只在被读到（strict_read=True）或写满时才会清。
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


class TTLCache:
    """TTL + 固定上限 FIFO 淘汰的通用缓存"""

    def __init__(self, ttl: float, max_size: int,
                 strict_read: bool = True, name: str = 'cache') -> None:
        """
        Args:
            ttl: 条目过期秒数
            max_size: 条数硬上限，超出按 FIFO 淘汰最老条目
            strict_read: True = 读时 TTL 过期视为 miss；False = 过期也返回
            name: 日志标签（用于 evict 的 debug 日志）
        """
        self._ttl = ttl
        self._max_size = max_size
        self._strict_read = strict_read
        self._name = name
        self._store: 'OrderedDict[Any, Tuple[Any, float]]' = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: Any) -> Optional[Any]:
        """命中且（strict_read=False 或未过期）返回 value；miss 或严格模式下已过期返回 None"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, created_at = entry
            if self._strict_read and time.time() - created_at >= self._ttl:
                self._store.pop(key, None)
                return None
            return value

    def put(self, key: Any, value: Any) -> None:
        """写入；覆盖时 move_to_end 刷新 created_at；超上限按 FIFO 淘汰"""
        now = time.time()
        with self._lock:
            if key in self._store:
                self._store[key] = (value, now)
                self._store.move_to_end(key)
            else:
                self._store[key] = (value, now)
                while len(self._store) > self._max_size:
                    evicted_key, _ = self._store.popitem(last=False)
                    logger.debug("[%s] evict by size: %s", self._name, evicted_key)

    def pop(self, key: Any, default: Any = None) -> Any:
        """删除并返回 value；不存在返回 default"""
        with self._lock:
            entry = self._store.pop(key, None)
            return entry[0] if entry is not None else default

    def clear(self) -> None:
        """清空整个缓存"""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
