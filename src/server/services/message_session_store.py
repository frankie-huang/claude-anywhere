"""Message-Session 映射存储

归属端: 飞书网关
使用方: feishu.py（网关内部逻辑），callback.py 中的网关侧路由（/gw/feishu/send）

维护 message_id → session 信息的映射，用于将用户回复消息路由到正确的 Callback 后端。
Callback 后端的路由和逻辑不应直接调用此 Store。
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class MessageSessionStore:
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

    _instance = None  # type: Optional[MessageSessionStore]
    _lock = threading.Lock()

    # 过期时间（秒），默认 7 天
    EXPIRE_SECONDS = 7 * 24 * 3600

    def __init__(self, data_dir: str):
        """初始化 MessageSessionStore

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'message_sessions.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"[message-session-store] Initialized with data_dir={data_dir}")

    @classmethod
    def initialize(cls, data_dir: str) -> 'MessageSessionStore':
        """初始化单例实例

        Args:
            data_dir: 数据存储目录

        Returns:
            MessageSessionStore 实例
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['MessageSessionStore']:
        """获取单例实例

        Returns:
            MessageSessionStore 实例，未初始化返回 None
        """
        return cls._instance

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

    def _load(self) -> Dict[str, Any]:
        """加载映射数据

        Returns:
            映射数据字典
        """
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"[message-session-store] Invalid JSON in {self._file_path}, starting fresh")
            return {}
        except IOError as e:
            logger.error(f"[message-session-store] Failed to load: {e}")
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        """保存映射数据（原子写入）

        Args:
            data: 映射数据字典

        Returns:
            是否保存成功
        """
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error(f"[message-session-store] Failed to save: {e}")
            return False
