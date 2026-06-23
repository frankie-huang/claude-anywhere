"""AuthToken 存储

归属端: Callback 后端
使用方: register.py (注册时保存), auth_token.py (验证时读取)

存储网关注册后返回的全局 auth_token，用于验证来自飞书网关和 Shell 脚本的请求。
飞书网关不应直接调用此 Store，应通过 X-Auth-Token header 传递令牌。
"""

import time
import logging
from typing import Optional

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)


class AuthTokenStore(JsonStore):
    """管理 auth_token 的存储

    用于存储网关注册后返回的 auth_token。
    """

    STORE_NAME = 'auth_token'
    LOG_TAG = 'auth-token-store'

    # token 内存缓存（get() 直接读，不落盘往返），_post_init 写入。
    # 读写都走 self._token 保持每实例独立；勿用 AuthTokenStore._token= 类限定赋值，
    # 否则串到共享类属性。
    _token: Optional[str] = None

    def _post_init(self):
        # 启动时把文件中的 token 加载到内存缓存（空值 → None）。
        # 本地兜底：唯一在启动期饿汉加载且无业务层兜底的 store，脏文件不应阻断启动
        # （token 可重新注册恢复），故吞掉非常规异常降级为无 token。
        try:
            self._token = self._load().get('auth_token', '') or None
        except Exception as e:
            logger.warning("[auth-token-store] Failed to load token: %s", e)
            self._token = None
        if self._token:
            logger.info("[auth-token-store] Loaded token from file")

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
                self._token = auth_token
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
            return self._token or ''

    def delete(self, owner_id: str) -> bool:
        """删除 auth_token

        Args:
            owner_id: 飞书用户 ID

        Returns:
            是否删除成功
        """
        with self._file_lock:
            try:
                # 清空：清内存缓存 + 持久化为空 dict
                self._token = None
                if not self._save({}):
                    return False
                logger.info(f"[auth-token-store] Deleted token for {owner_id}")
                return True
            except Exception as e:
                logger.error(f"[auth-token-store] Failed to delete token: {e}")
                return False
