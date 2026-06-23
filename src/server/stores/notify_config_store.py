"""通知配置存储

归属端: Callback 后端
使用方: callback.py

管理运行时通知配置覆盖（如 @ 谁、@ 时段），持久化到 runtime/notify_config.json。
用户通过飞书 /notify 命令修改，Shell 侧 feishu.sh 在构建卡片时读取。

飞书网关不应直接调用此 Store，应通过 Callback 后端的 HTTP 接口间接访问。
"""

import time
import logging
from typing import Dict, Any

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)


class NotifyConfigStore(JsonStore):
    """管理运行时通知配置覆盖（单例 + 文件持久化）

    存储格式 (notify_config.json):
    {
        "at_user": "all",           // @ 谁: self/all/<user_id>/off
        "at_start": "08:00",        // @ 时段起始（可选）
        "at_end": "22:00",          // @ 时段结束（可选）
        "permission_delay": 60,     // 权限通知延迟秒数（可选）
        "updated_at": 1749582880
    }

    - at_user 不存在时等同于 self（@ owner）
    - at_start/at_end 不存在时等同于 always（全天 @）
    - permission_delay 不存在时默认 0（立即发送）
    - 文件不存在时等同于全部默认
    """

    STORE_NAME = 'notify_config'
    LOG_TAG = 'notify-config-store'

    def _post_init(self):
        self._migrate_permission_delay()

    def get_config(self) -> Dict[str, Any]:
        """读取完整配置。文件不存在或解析失败返回 {}。"""
        with self._file_lock:
            return self._load()

    def set_at_user(self, at_user: str) -> bool:
        """设置 at_user（merge，保留 at_start/at_end）。

        Args:
            at_user: self/all/<user_id>/off

        Returns:
            是否保存成功
        """
        value = (at_user or '').strip()
        if not value:
            return False
        with self._file_lock:
            data = self._load()
            data['at_user'] = value
            data['updated_at'] = int(time.time())
            return self._save(data)

    def set_time_range(self, at_start: str, at_end: str) -> bool:
        """设置 @ 时段（merge，保留 at_user）。

        Args:
            at_start: 起始时间，格式 HH:MM
            at_end: 结束时间，格式 HH:MM

        Returns:
            是否保存成功
        """
        at_start = (at_start or '').strip()
        at_end = (at_end or '').strip()
        if not at_start or not at_end:
            return False
        with self._file_lock:
            data = self._load()
            data['at_start'] = at_start
            data['at_end'] = at_end
            data['updated_at'] = int(time.time())
            return self._save(data)

    def clear_time_range(self) -> bool:
        """清除 @ 时段（保留 at_user）。

        Returns:
            是否保存成功
        """
        with self._file_lock:
            data = self._load()
            data.pop('at_start', None)
            data.pop('at_end', None)
            data['updated_at'] = int(time.time())
            return self._save(data)

    def set_permission_delay(self, delay: int) -> bool:
        """设置权限通知延迟秒数（merge，保留其他字段）。

        Args:
            delay: 延迟秒数（非负整数）

        Returns:
            是否保存成功
        """
        if delay < 0:
            return False
        with self._file_lock:
            data = self._load()
            data['permission_delay'] = delay
            data['updated_at'] = int(time.time())
            return self._save(data)

    def clear_permission_delay(self) -> bool:
        """清除权限通知延迟覆盖（保留其他字段）。

        Returns:
            是否保存成功
        """
        with self._file_lock:
            data = self._load()
            data.pop('permission_delay', None)
            data['updated_at'] = int(time.time())
            return self._save(data)

    # =========================================================================
    # 迁移
    # =========================================================================

    def _migrate_permission_delay(self) -> None:
        """将 .env 的 PERMISSION_NOTIFY_DELAY 迁移到 notify_config.json（一次性）。

        仅当 json 文件中没有 permission_delay 字段时执行，写入后不再重复。
        """
        data = self._load()
        if 'permission_delay' in data:
            return
        try:
            from config import get_config
            value = get_config('PERMISSION_NOTIFY_DELAY', '0')
            delay = int(value)
        except (ValueError, ImportError):
            delay = 0
        if delay < 0:
            delay = 0
        data['permission_delay'] = delay
        data['updated_at'] = int(time.time())
        if self._save(data):
            logger.info("[notify-config-store] Migrated PERMISSION_NOTIFY_DELAY=%d", delay)
        else:
            logger.warning("[notify-config-store] Failed to migrate PERMISSION_NOTIFY_DELAY")
