"""Message-Session 映射存储

归属端: 飞书网关
使用方: feishu.py（网关内部逻辑），callback.py 中的网关侧路由（/gw/feishu/send）

维护 message_id → session 信息的映射，用于将用户回复消息路由到正确的 Callback 后端。
Callback 后端的路由和逻辑不应直接调用此 Store。
"""

import time
import logging
from typing import Optional, Dict, Any

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)


class MessageSessionStore(JsonStore):
    """管理 message_id -> session 信息的映射

    映射结构:
    {
        "message_id": {
            "session_id": "xxx",
            "project_dir": "/path/to/project",
            "created_at": 1706745600
        }
    }
    """

    STORE_NAME = 'message_sessions'
    LOG_TAG = 'message-session-store'

    # 过期时间（秒），默认 7 天
    EXPIRE_SECONDS = 7 * 24 * 3600

    def save(self, message_id: str, session_id: str, project_dir: str) -> bool:
        """保存映射关系

        Args:
            message_id: 飞书消息 ID
            session_id: 会话 ID
            project_dir: 项目工作目录

        Returns:
            是否保存成功
        """
        with self._file_lock:
            try:
                data = self._load()
                data[message_id] = {
                    'session_id': session_id,
                    'project_dir': project_dir,
                    'created_at': int(time.time())
                }
                result = self._save(data)
                if result:
                    logger.info(f"[message-session-store] Saved mapping: {message_id} -> {session_id}")
                return result
            except Exception as e:
                logger.error(f"[message-session-store] Failed to save mapping: {e}")
                return False

    def get(self, message_id: str) -> Optional[Dict[str, Any]]:
        """获取映射关系

        Args:
            message_id: 飞书消息 ID

        Returns:
            映射信息字典，不存在或已过期返回 None
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(message_id)
                if not item:
                    return None

                # 检查过期
                if time.time() - item.get('created_at', 0) > self.EXPIRE_SECONDS:
                    logger.info(f"[message-session-store] Mapping expired: {message_id}")
                    del data[message_id]
                    self._save(data)
                    return None

                return item
            except Exception as e:
                logger.error(f"[message-session-store] Failed to get mapping: {e}")
                return None

    def cleanup_expired(self) -> int:
        """清理过期数据

        Returns:
            清理的条目数量
        """
        with self._file_lock:
            try:
                data = self._load()
                now = time.time()
                expired = [
                    k for k, v in data.items()
                    if now - v.get('created_at', 0) > self.EXPIRE_SECONDS
                ]
                for k in expired:
                    del data[k]
                if expired:
                    self._save(data)
                    logger.info(f"[message-session-store] Cleaned {len(expired)} expired mappings")
                return len(expired)
            except Exception as e:
                logger.error(f"[message-session-store] Failed to cleanup: {e}")
                return 0
