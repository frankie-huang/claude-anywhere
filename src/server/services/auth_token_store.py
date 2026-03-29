"""AuthToken 存储

归属端: Callback 后端
使用方: register.py (注册时保存), auth_token.py (验证时读取)

存储网关注册后返回的全局 auth_token，用于验证来自飞书网关和 Shell 脚本的请求。
飞书网关不应直接调用此 Store，应通过 X-Auth-Token header 传递令牌。
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class AuthTokenStore:
    """管理 auth_token 的存储

    用于存储网关注册后返回的 auth_token。
    """

    _instance = None  # type: Optional[AuthTokenStore]
    _lock = threading.Lock()
    _token = None  # type: Optional[str]

    def __init__(self, data_dir: str):
        """初始化 AuthTokenStore

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'auth_token.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"[auth-token-store] Initialized with data_dir={data_dir}")

        # 启动时尝试加载已有的 token
        self._load()

    @classmethod
    def initialize(cls, data_dir: str) -> 'AuthTokenStore':
        """初始化单例实例

        Args:
            data_dir: 数据存储目录

        Returns:
            AuthTokenStore 实例
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['AuthTokenStore']:
        """获取单例实例

        Returns:
            AuthTokenStore 实例，未初始化返回 None
        """
        return cls._instance

    def save(self, owner_id: str, auth_token: str, bot_open_id: str = '') -> bool:
        """保存 auth_token

        Args:
            owner_id: 飞书用户 ID
            auth_token: 认证令牌
            bot_open_id: 机器人 open_id（可选，注册时由网关传入，用于消息中 @机器人）

        Returns:
            是否保存成功
        """
        with self._file_lock:
            try:
                AuthTokenStore._token = auth_token
                data = {
                    'owner_id': owner_id,
                    'auth_token': auth_token,
                    'updated_at': int(time.time())
                }
                # 每次都写入 bot_open_id（即使是空值），避免切换网关后残留旧机器人 ID
                data['bot_open_id'] = bot_open_id
                return self._save(data)
            except Exception as e:
                logger.error(f"[auth-token-store] Failed to save token: {e}")
                return False

    def get(self) -> str:
        """获取存储的 auth_token（线程安全）

        Returns:
            存储的 auth_token，不存在返回空字符串
        """
        with self._file_lock:
            return AuthTokenStore._token or ''

    def delete(self, owner_id: str) -> bool:
        """删除 auth_token

        Args:
            owner_id: 飞书用户 ID

        Returns:
            是否删除成功
        """
        with self._file_lock:
            try:
                # 清除内存中的 token
                AuthTokenStore._token = None

                # 删除文件
                if os.path.exists(self._file_path):
                    os.remove(self._file_path)
                    logger.info(f"[auth-token-store] Deleted token for {owner_id}")

                return True
            except Exception as e:
                logger.error(f"[auth-token-store] Failed to delete token: {e}")
                return False

    def _load(self):
        """从文件加载 token"""
        if not os.path.exists(self._file_path):
            return

        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                AuthTokenStore._token = data.get('auth_token', '')
                if AuthTokenStore._token:
                    logger.info(f"[auth-token-store] Loaded token from file")
        except Exception as e:
            logger.warning(f"[auth-token-store] Failed to load token: {e}")

    def _save(self, data: Dict[str, Any]) -> bool:
        """保存数据到文件（原子写入）

        Args:
            data: 数据字典

        Returns:
            是否保存成功
        """
        try:
            # 写入临时文件
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # 原子重命名
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error(f"[auth-token-store] Failed to save: {e}")
            return False
