"""Session-Chat 映射存储

归属端: Callback 后端
使用方: callback.py, claude.py
对外接口: /cb/session/get-chat-id, /cb/session/get-last-message-id (供 Shell 脚本通过 HTTP 查询)
          /cb/session/set-last-message-id (供飞书网关通过 HTTP 写入)

维护 session_id → chat_id 的映射关系，用于确定消息发送的目标群聊。
飞书网关和 Shell 脚本不应直接调用此 Store，应通过 Callback 后端的 HTTP 接口间接访问。
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class SessionChatStore:
    """管理 session_id -> chat_id 的存储（归属端: Callback 后端）

    维护会话与群聊的映射关系，用于确定消息发送的目标群聊。
    外部通过 /cb/session/get-chat-id, /cb/session/get-last-message-id, /cb/session/set-last-message-id 接口间接访问。

    数据结构::

        {
            "session_id": {
                "chat_id": "oc_xxx",              # 飞书群聊 ID
                "claude_command": "claude",        # 使用的 Claude 命令（可选）
                "last_message_id": "om_xxx",       # 链式回复锚点（可选，由 set_last_message_id 管理）
                "skip_next_user_prompt": true,     # 跳过下一条 UserPromptSubmit（飞书发起时设置）
                "updated_at": 1706745600           # 最近更新时间戳
            }
        }

    写入方式:
        - save(): 创建/更新 chat_id 和 claude_command，自动保留已有的 last_message_id
        - set_last_message_id(): 单独更新 last_message_id，同时刷新 updated_at
    """

    _instance = None  # type: Optional[SessionChatStore]
    _lock = threading.Lock()

    # 过期时间（秒），默认 7 天
    EXPIRE_SECONDS = 7 * 24 * 3600

    def __init__(self, data_dir: str):
        """初始化 SessionChatStore

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'session_chats.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"[session-chat-store] Initialized with data_dir={data_dir}")

    @classmethod
    def initialize(cls, data_dir: str) -> 'SessionChatStore':
        """初始化单例实例

        Args:
            data_dir: 数据存储目录

        Returns:
            SessionChatStore 实例
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['SessionChatStore']:
        """获取单例实例

        Returns:
            SessionChatStore 实例，未初始化返回 None
        """
        return cls._instance

    def save(self, session_id: str, chat_id: str, claude_command: str = '') -> bool:
        """保存 session_id -> chat_id 映射

        Args:
            session_id: Claude 会话 ID
            chat_id: 飞书群聊 ID
            claude_command: 该 session 使用的 Claude 命令（可选）

        Returns:
            是否保存成功
        """
        with self._file_lock:
            try:
                data = self._load()
                entry = {
                    'chat_id': chat_id,
                    'updated_at': int(time.time())
                }
                if claude_command:
                    entry['claude_command'] = claude_command
                # 保留已有的 claude_command（如果本次未指定但已有记录）
                elif session_id in data and 'claude_command' in data[session_id]:
                    entry['claude_command'] = data[session_id]['claude_command']
                # 保留已有的 last_message_id（由 set_last_message_id 单独管理）
                if session_id in data and 'last_message_id' in data[session_id]:
                    entry['last_message_id'] = data[session_id]['last_message_id']
                data[session_id] = entry
                result = self._save(data)
                if result:
                    logger.info(f"[session-chat-store] Saved mapping: {session_id} -> {chat_id}")
                return result
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to save mapping: {e}")
                return False

    def get(self, session_id: str) -> Optional[str]:
        """获取 session_id 对应的 chat_id

        Args:
            session_id: Claude 会话 ID

        Returns:
            群聊 chat_id，不存在或已过期返回 None
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return None

                # 检查过期
                if time.time() - item.get('updated_at', 0) > self.EXPIRE_SECONDS:
                    logger.info(f"[session-chat-store] Mapping expired: {session_id}")
                    del data[session_id]
                    self._save(data)
                    return None

                return item.get('chat_id')
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to get mapping: {e}")
                return None

    def get_command(self, session_id: str) -> Optional[str]:
        """获取 session_id 对应的 claude_command

        Args:
            session_id: Claude 会话 ID

        Returns:
            claude_command 字符串，不存在或已过期返回 None
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return None

                # 检查过期
                if time.time() - item.get('updated_at', 0) > self.EXPIRE_SECONDS:
                    return None

                return item.get('claude_command')
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to get command: {e}")
                return None

    def get_last_message_id(self, session_id: str) -> str:
        """获取 session_id 对应的 last_message_id（最近一条消息 ID）

        Args:
            session_id: Claude 会话 ID

        Returns:
            last_message_id 字符串，不存在或已过期返回空字符串
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return ''

                # 检查过期
                if time.time() - item.get('updated_at', 0) > self.EXPIRE_SECONDS:
                    return ''

                return item.get('last_message_id', '')
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to get last_message_id: {e}")
                return ''

    def set_last_message_id(self, session_id: str, message_id: str) -> bool:
        """更新 session_id 的 last_message_id（最近一条消息 ID）

        每次发送消息成功后调用此方法更新，实现链式回复结构。
        如果 session 不存在，自动创建记录（支持终端直接启动的会话）。

        Args:
            session_id: Claude 会话 ID
            message_id: 最近一条消息 ID

        Returns:
            是否设置成功
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)

                if not item:
                    # session 不存在，自动创建记录（终端直接启动的会话场景）
                    item = {
                        'last_message_id': message_id,
                        'updated_at': int(time.time())
                    }
                    data[session_id] = item
                    result = self._save(data)
                    if result:
                        logger.info(f"[session-chat-store] Created session with last_message_id: {session_id} -> {message_id}")
                    return result

                # 检查过期
                if time.time() - item.get('updated_at', 0) > self.EXPIRE_SECONDS:
                    logger.warning(f"[session-chat-store] Cannot set last_message_id: session expired {session_id}")
                    return False

                # 更新 last_message_id 并刷新过期时间
                item['last_message_id'] = message_id
                item['updated_at'] = int(time.time())
                data[session_id] = item
                result = self._save(data)
                if result:
                    logger.info(f"[session-chat-store] Updated last_message_id: {session_id} -> {message_id}")

                return result
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to set last_message_id: {e}")
                return False

    def set_skip_next_user_prompt(self, session_id: str) -> bool:
        """设置 skip_next_user_prompt 标志

        飞书网关发起会话/继续会话时调用，标记该 session 的下一条
        UserPromptSubmit 事件应被跳过（因为 prompt 已在飞书端展示）。

        Args:
            session_id: Claude 会话 ID

        Returns:
            是否设置成功
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)

                if not item:
                    # session 不存在，创建带标志的记录
                    item = {
                        'skip_next_user_prompt': True,
                        'updated_at': int(time.time())
                    }
                else:
                    item['skip_next_user_prompt'] = True
                    item['updated_at'] = int(time.time())

                data[session_id] = item
                result = self._save(data)
                if result:
                    logger.info(f"[session-chat-store] Set skip_next_user_prompt: {session_id}")
                return result
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to set skip flag: {e}")
                return False

    def check_and_clear_skip_user_prompt(self, session_id: str) -> bool:
        """检查并清除 skip_next_user_prompt 标志（原子操作）

        UserPromptSubmit hook 调用此方法判断是否应跳过。
        如果标志为 True 则清除并返回 True，否则返回 False。

        Args:
            session_id: Claude 会话 ID

        Returns:
            True 表示应跳过（飞书发起的 prompt），False 表示不应跳过
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)

                if not item:
                    return False

                skip = item.get('skip_next_user_prompt', False)
                if skip:
                    del item['skip_next_user_prompt']
                    item['updated_at'] = int(time.time())
                    data[session_id] = item
                    self._save(data)
                    logger.info(f"[session-chat-store] Cleared skip_next_user_prompt: {session_id}")

                return skip
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to check skip flag: {e}")
                return False

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
                    if now - v.get('updated_at', 0) > self.EXPIRE_SECONDS
                ]
                for k in expired:
                    del data[k]
                if expired:
                    self._save(data)
                    logger.info(f"[session-chat-store] Cleaned {len(expired)} expired mappings")
                return len(expired)
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to cleanup: {e}")
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
            logger.warning(f"[session-chat-store] Invalid JSON in {self._file_path}, starting fresh")
            return {}
        except IOError as e:
            logger.error(f"[session-chat-store] Failed to load: {e}")
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
            logger.error(f"[session-chat-store] Failed to save: {e}")
            return False
