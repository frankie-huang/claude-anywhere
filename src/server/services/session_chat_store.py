"""Session-Chat 映射存储

归属端: Callback 后端
使用方: callback.py, claude.py
对外接口:
    - /cb/session/get-chat-id / get-last-message-id / set-last-message-id
    - /cb/session/ensure-chat（group 模式懒创建群聊）
    - /cb/session/get-info（按 session_id 返回权威字段，含 dissolved 状态）
    - /cb/session/mute
    - /cb/session/invalidate-chats（gateway 解散群后调用，标记所有引用该 chat_id 的记录为 dissolved 状态）

维护 session_id → session 语义数据（chat_id、claude_command、muted、dissolved、活跃时间等）。
群聊层数据（chat_id ↔ session_id 反向索引、owner、seq、生命周期）由飞书网关侧
GroupChatStore + GroupSessionStore 独立承担；本 store 只负责 session 自身属性。

dissolved 标记：与 muted 同构的独立布尔字段，默认不存在。
    - 群解散时 gateway 调 /cb/session/invalidate-chats，本 store 设置 dissolved=True
    - get_session 过滤 dissolved 返回 None（软失效），上层落入"session 不存在"分支：
      ensure-chat 走重建、continue 报错引导 /new
    - attach 通过 save(session_id, new_chat_id) 自动复活（非空 chat_id 清除 dissolved）
    - get_session(include_dissolved=True) 可读取 dissolved session（继承属性、校验存在性等只读场景）
    - 不刷新 updated_at（dissolve 不是活跃信号，保留原值让过期机制正常回收）

过期策略：统一 SESSION_EXPIRE_DAYS（默认 30 天），不区分 group/非 group。
gateway 转发 /cb/claude/continue 时 callback 校验 session 是否存在，
已过期则返回错误，gateway 告知用户 /new。
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionChatStore:
    """管理 session_id -> session 语义数据的存储（归属端: Callback 后端）

    数据结构:

        {
            "session_id": {
                "chat_id": "oc_xxx",               # 飞书群聊 ID；空表示需要 ensure-chat 重建
                "claude_command": "claude",        # 使用的 Claude 命令（可选）
                "last_message_id": "om_xxx",       # 链式回复锚点（可选）
                "skip_next_user_prompt": true,     # 跳过下一条 UserPromptSubmit（飞书发起时设置，可选）
                "updated_at": 1706745600,          # 最近更新时间戳
                "project_dir": "/path/to/project", # 项目目录（可选）
                "muted": true,                     # 出站静音标志（可选）
                "dissolved": true                  # 群已解散标志（可选）
            }
        }

    """

    _instance: Optional['SessionChatStore'] = None
    _lock = threading.Lock()

    # 默认过期时间（秒），由 config.SESSION_EXPIRE_DAYS 覆盖
    _expire_seconds: int = 30 * 24 * 3600

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'session_chats.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        logger.info("[session-chat-store] Initialized with data_dir=%s", data_dir)

    @classmethod
    def initialize(cls, data_dir: str, expire_seconds: Optional[int] = None) -> 'SessionChatStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            if expire_seconds is not None:
                cls._instance._expire_seconds = expire_seconds
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['SessionChatStore']:
        return cls._instance

    # =========================================================================
    # 写
    # =========================================================================

    def save(self, session_id: str, chat_id: str,
             project_dir: str = '', claude_command: str = '') -> bool:
        """保存 session 属性（merge 方式，不传的字段保留旧值）

        Args:
            session_id: Claude 会话 ID
            chat_id: 飞书群聊 ID；空串视为不覆盖旧值，非空时自动清除 dissolved 标记（复活）
            project_dir: 项目目录（空串视为不覆盖）
            claude_command: Claude 命令（空串视为不覆盖）

        Returns:
            是否保存成功
        """
        with self._file_lock:
            try:
                data = self._load()
                old = data.get(session_id, {})
                old_chat_id = old.get('chat_id', '')
                entry = dict(old)

                if chat_id:
                    entry['chat_id'] = chat_id
                    # 传入非空 chat_id 等于"session 现在有可用群聊"，自动清除 dissolved
                    entry.pop('dissolved', None)
                entry['updated_at'] = int(time.time())
                if claude_command:
                    entry['claude_command'] = claude_command
                if project_dir:
                    entry['project_dir'] = project_dir
                # chat_id 变更时清掉旧 last_message_id（旧消息链不再适用）
                if chat_id and old_chat_id and old_chat_id != chat_id:
                    entry.pop('last_message_id', None)

                data[session_id] = entry
                result = self._save(data)
                if result:
                    logger.info("[session-chat-store] Saved mapping: %s -> %s",
                                session_id, chat_id or '(unchanged)')
                return result
            except Exception as e:
                logger.error("[session-chat-store] Failed to save mapping: %s", e)
                return False

    def set_last_message_id(self, session_id: str, message_id: str) -> bool:
        """更新 last_message_id（链式回复锚点）

        session 不存在时自动创建（支持终端直接启动的 session）。
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)

                if not item:
                    item = {
                        'last_message_id': message_id,
                        'updated_at': int(time.time())
                    }
                    data[session_id] = item
                    result = self._save(data)
                    if result:
                        logger.info("[session-chat-store] Created session with last_message_id: %s -> %s",
                                    session_id, message_id)
                    return result

                if time.time() - item.get('updated_at', 0) > self._expire_seconds:
                    logger.warning("[session-chat-store] Cannot set last_message_id: session expired %s",
                                   session_id)
                    return False

                item['last_message_id'] = message_id
                item['updated_at'] = int(time.time())
                data[session_id] = item
                result = self._save(data)
                if result:
                    logger.info("[session-chat-store] Updated last_message_id: %s -> %s",
                                session_id, message_id)
                return result
            except Exception as e:
                logger.error("[session-chat-store] Failed to set last_message_id: %s", e)
                return False

    def set_skip_next_user_prompt(self, session_id: str) -> bool:
        """设置 skip_next_user_prompt 标志

        飞书网关发起会话/继续会话时调用，标记该 session 的下一条
        UserPromptSubmit 事件应被跳过（因为 prompt 已在飞书端展示）。
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
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
                    logger.info("[session-chat-store] Set skip_next_user_prompt: %s", session_id)
                return result
            except Exception as e:
                logger.error("[session-chat-store] Failed to set skip flag: %s", e)
                return False

    def check_and_clear_skip_user_prompt(self, session_id: str) -> bool:
        """原子检查并清除 skip_next_user_prompt 标志

        UserPromptSubmit hook 调用此方法判断是否应跳过。
        标志为 True 则清除并返回 True（应跳过），否则返回 False。
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
                    logger.info("[session-chat-store] Cleared skip_next_user_prompt: %s", session_id)
                return skip
            except Exception as e:
                logger.error("[session-chat-store] Failed to check skip flag: %s", e)
                return False

    def mark_dissolved(self, chat_id: str) -> List[str]:
        """按 chat_id 标记所有引用该群的 session 为已解散

        保留 chat_id 字段作为历史信息（debug / 复活溯源）；只设置 dissolved=True。
        被标记的 session 通过 get_session 不可见（软失效），上层自动落入
        "session 不存在"分支：
            - ensure-chat 走重建路径
            - continue 报错"请 /new"
            - attach 通过 save(session_id, new_chat_id) 自动复活

        不刷新 updated_at——dissolve 是"群没了"，不是 session 活跃信号。

        Args:
            chat_id: 已解散的飞书群聊 ID

        Returns:
            被标记的 session_id 列表
        """
        if not chat_id:
            return []
        with self._file_lock:
            try:
                data = self._load()
                marked = []
                for sid, entry in data.items():
                    if entry.get('chat_id') == chat_id and not entry.get('dissolved'):
                        entry['dissolved'] = True
                        marked.append(sid)
                if not marked:
                    return []
                if not self._save(data):
                    return []
                logger.info("[session-chat-store] Marked %d sessions dissolved for chat=%s: %s",
                            len(marked), chat_id, marked)
                return marked
            except Exception as e:
                logger.error("[session-chat-store] Failed to mark_dissolved: %s", e)
                return []

    def delete(self, session_id: str) -> bool:
        """彻底删除 session 记录

        Args:
            session_id: Claude 会话 ID

        Returns:
            是否实际删除（不存在返回 False）
        """
        if not session_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                if session_id not in data:
                    return False
                del data[session_id]
                if not self._save(data):
                    return False
                logger.info("[session-chat-store] Deleted: %s", session_id)
                return True
            except Exception as e:
                logger.error("[session-chat-store] Failed to delete: %s", e)
                return False

    # =========================================================================
    # Mute
    # =========================================================================

    def mute_session(self, session_id: str) -> Optional[bool]:
        """标记 session 为静音（出站消息被 /gw/feishu/send 拦截，
        session 本身继续正常运转，Claude 仍处理用户消息）。

        静音操作不刷新 updated_at，避免干扰群聊自动解散的空闲判断。

        Returns:
            True  = 本次从未静音切到静音
            False = 幂等（之前已静音）
            None  = 失败（session 不存在 / 保存异常）
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    logger.warning("[session-chat-store] mute_session: session not found: %s", session_id)
                    return None
                if item.get('muted'):
                    return False
                item['muted'] = True
                data[session_id] = item
                if not self._save(data):
                    return None
                logger.info("[session-chat-store] Muted: %s", session_id)
                return True
            except Exception as e:
                logger.error("[session-chat-store] Failed to mute_session: %s", e)
                return None

    def unmute_session(self, session_id: str) -> Optional[bool]:
        """清除 session 静音标志。

        Returns:
            True  = 本次从静音切到未静音
            False = 幂等（之前就未静音）
            None  = 失败（session 不存在 / 保存异常；与 mute 对称，
                    避免调用方对"session 缺失"得到矛盾反馈）
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    logger.warning("[session-chat-store] unmute_session: session not found: %s", session_id)
                    return None
                if not item.get('muted'):
                    return False
                del item['muted']
                data[session_id] = item
                if not self._save(data):
                    return None
                logger.info("[session-chat-store] Unmuted: %s", session_id)
                return True
            except Exception as e:
                logger.error("[session-chat-store] Failed to unmute_session: %s", e)
                return None

    def is_session_muted(self, session_id: str) -> bool:
        """检查 session 是否处于静音状态

        muted 与 dissolved 独立：dissolved 表示群聊层失效，muted 表示用户业务层
        意图（是否拦截出站）。dissolved 的 session 若仍被 mute，出站依然按用户
        意图拦截——避免 dissolve 后消息漏到单聊。
        """
        if not session_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False
                return bool(item.get('muted'))
            except Exception as e:
                logger.error("[session-chat-store] Failed to check muted: %s", e)
                return False

    # =========================================================================
    # 读
    # =========================================================================

    def get_session(self, session_id: str,
                    include_dissolved: bool = False) -> Optional[Dict[str, Any]]:
        """获取 session 完整数据（含过期清理）

        不存在 / 过期均返回 None。
        dissolved 默认也返回 None（软失效），传 include_dissolved=True 可读取。
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return None
                if not include_dissolved and item.get('dissolved'):
                    return None
                if time.time() - item.get('updated_at', 0) > self._expire_seconds:
                    logger.info("[session-chat-store] Mapping expired: %s", session_id)
                    del data[session_id]
                    self._save(data)
                    return None
                return dict(item)
            except Exception as e:
                logger.error("[session-chat-store] Failed to get_session: %s", e)
                return None

    def get_chat_id(self, session_id: str) -> Optional[str]:
        """获取 session 的 chat_id。不存在/过期返回 None；chat_id 为空也返回 None。

        过滤 dissolved：dissolved 的 chat_id 指向已解散群，返回会导致消息发往不存在的群。
        """
        item = self.get_session(session_id)
        chat_id = item.get('chat_id') if item else None
        return chat_id or None

    def get_last_message_id(self, session_id: str) -> str:
        """获取 session 的 last_message_id。不存在/过期返回空字符串。

        过滤 dissolved：旧群的 message_id 无法用于链式回复。
        """
        item = self.get_session(session_id)
        return item.get('last_message_id', '') if item else ''

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """返回所有 session 的浅拷贝（不做过滤）"""
        with self._file_lock:
            data = self._load()
        return {sid: dict(item) for sid, item in data.items()}

    def find_by_prefix(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """按 session_id 前缀查找（含 dissolved 用于 attach 复活）

        不过滤过期——调用方按需处理。
        """
        if not prefix:
            return {}
        try:
            with self._file_lock:
                data = self._load()
        except Exception as e:
            logger.error("[session-chat-store] Failed to load in find_by_prefix: %s", e)
            return {}
        return {sid: dict(item) for sid, item in data.items() if sid.startswith(prefix)}

    # =========================================================================
    # 维护
    # =========================================================================

    def cleanup_expired(self) -> int:
        """清理过期数据

        超过 SESSION_EXPIRE_DAYS 的 session 直接删除（统一策略，不区分 group/非 group）。

        Returns:
            清理的条目数量
        """
        with self._file_lock:
            try:
                data = self._load()
                now = time.time()
                expired = [
                    sid for sid, item in data.items()
                    if now - item.get('updated_at', 0) > self._expire_seconds
                ]
                if expired:
                    for sid in expired:
                        del data[sid]
                    if self._save(data):
                        logger.info("[session-chat-store] Cleaned %d expired mappings", len(expired))
                return len(expired)
            except Exception as e:
                logger.error("[session-chat-store] Failed to cleanup: %s", e)
                return 0

    # =========================================================================
    # 底层 I/O
    # =========================================================================

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("[session-chat-store] Invalid JSON in %s, starting fresh",
                           self._file_path)
            return {}
        except IOError as e:
            logger.error("[session-chat-store] Failed to load: %s", e)
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error("[session-chat-store] Failed to save: %s", e)
            return False
