"""Session-Chat 映射存储

归属端: Callback 后端
使用方: callback.py, claude.py
对外接口: /cb/session/get-chat-id, /cb/session/get-last-message-id (供 Shell 脚本通过 HTTP 查询)
          /cb/session/set-last-message-id (供飞书网关通过 HTTP 写入)
          /cb/session/ensure-chat (供 Shell 脚本确保 session 有 chat_id，group 模式懒创建群聊)
          /cb/session/resolve-group-chat (供飞书网关通过 chat_id 反查 session_id)
          /cb/groups/list, /cb/groups/dissolve (供飞书网关 /groups 命令调用)

维护 session_id → chat_id 的映射关系，用于确定消息发送的目标群聊。
group 模式下额外维护 chat_id → session_id 的反向索引，支持群聊消息路由。
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
                "updated_at": 1706745600,          # 最近更新时间戳
                "group_active": true,              # session 是否是该群聊的当前活跃 session（接管消息路由）（可选）
                "project_dir": "/path/to/project", # 项目目录（可选）
                "dissolved": true                  # 群聊已解散标志（可选）
            }
        }

    写入方式:
        - save(): 创建/更新 chat_id 和 claude_command，自动保留已有的 last_message_id
        - set_last_message_id(): 单独更新 last_message_id，同时刷新 updated_at

    内存反向索引: chat_id -> session_id，加速群聊消息路由查询。
    """

    _instance: Optional['SessionChatStore'] = None
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
        # group 模式反向索引：chat_id -> session_id（其他模式下为空）
        # 重要：仅在 _save() 成功后更新，保证与持久化数据一致
        self._chat_to_session: Dict[str, str] = {}
        self._rebuild_chat_index()
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

    def save(self, session_id: str, chat_id: str,
             group_active: Optional[bool] = None,
             project_dir: str = '', claude_command: str = '') -> bool:
        """保存 session_id -> chat_id 映射

        Args:
            session_id: Claude 会话 ID
            chat_id: 飞书群聊 ID
            group_active: 该 session 是否为该群聊的当前活跃 session（可选，接管消息路由）
                - None: 不改动现有字段（默认；用于只刷新 chat_id/claude_command 的场景）
                - True: 显式激活（同时更新反向索引）
                - False: 显式清除（同时清理反向索引）
            project_dir: 项目目录（可选，空串视为不覆盖）
            claude_command: 该 session 使用的 Claude 命令（可选，空串视为不覆盖）

        Returns:
            是否保存成功
        """
        with self._file_lock:
            try:
                data = self._load()
                old = data.get(session_id, {})
                old_chat_id = old.get('chat_id', '')

                # --- 1) 以旧记录为基础构建 entry，覆盖必要字段 ---
                # merge 方式：自动继承所有旧字段，新增字段无需逐个处理
                entry = dict(old)
                if chat_id:
                    entry['chat_id'] = chat_id
                    # 关联了 chat_id 说明 session 在被使用，清除 dissolved 标志（复活）
                    entry.pop('dissolved', None)
                entry['updated_at'] = int(time.time())
                # 传入非空才覆盖，否则保留旧值
                if claude_command:
                    entry['claude_command'] = claude_command
                if project_dir:
                    entry['project_dir'] = project_dir
                # 三态处理：None=不变，True=激活，False=清除
                if group_active is True:
                    entry['group_active'] = True
                elif group_active is False:
                    entry.pop('group_active', None)
                # chat_id 变更时清除 last_message_id（旧消息链不再适用）
                if chat_id and old_chat_id and old_chat_id != chat_id:
                    entry.pop('last_message_id', None)

                # entry 最终是否作为 chat_id 的活跃路由目标
                # 用 entry 而非参数 group_active 判断：参数三态中 None 表示"不改"，
                # 此时若 old 继承了 group_active=True 且 chat_id 变更，仍需走冲突处理
                # 和索引维护——否则会出现"索引指向新 session 但前任未降级"的裂隙
                will_be_active = bool(entry.get('group_active')) and bool(chat_id)

                # --- 2) 处理 chat_id 冲突：旧 session 解除群聊绑定 ---
                # seq 由 GroupSeqStore 独立管理，无需继承
                if will_be_active:
                    prev_session_id = self._chat_to_session.get(chat_id)
                    if prev_session_id and prev_session_id != session_id:
                        prev = data.get(prev_session_id)
                        if prev and prev.get('group_active'):
                            prev['group_active'] = False
                            data[prev_session_id] = prev
                            logger.info("[session-chat-store] Replacing session %s with %s for chat_id %s",
                                        prev_session_id, session_id, chat_id)

                # --- 3) 持久化 & 更新反向索引 ---
                data[session_id] = entry
                result = self._save(data)
                if result:
                    logger.info(f"[session-chat-store] Saved mapping: {session_id} -> {chat_id}")
                    # 更新反向索引（与冲突处理同步，都以 will_be_active 为准）
                    if will_be_active:
                        # 仍是 active group：chat_id 变更时清理旧索引，再写入新索引
                        if old_chat_id and old_chat_id != chat_id \
                           and self._chat_to_session.get(old_chat_id) == session_id:
                            self._chat_to_session.pop(old_chat_id, None)
                        self._chat_to_session[chat_id] = session_id
                    elif old_chat_id and self._chat_to_session.get(old_chat_id) == session_id:
                        # 降级为非 group 或丢失 chat_id：清理本 session 遗留的索引
                        self._chat_to_session.pop(old_chat_id, None)
                return result
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to save mapping: {e}")
                return False

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取 session 的完整数据（含惰性清理）

        Args:
            session_id: Claude 会话 ID

        Returns:
            完整的 session 数据字典，不存在、已过期或已解散返回 None
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return None

                # 已解散的群聊不可用
                if item.get('dissolved'):
                    return None

                # 检查过期（活跃 group 不受 EXPIRE_SECONDS 限制，由解散机制管理）
                if time.time() - item.get('updated_at', 0) > self.EXPIRE_SECONDS:
                    if not item.get('group_active'):
                        logger.info(f"[session-chat-store] Mapping expired: {session_id}")
                        del data[session_id]
                        self._save(data)
                        return None

                return dict(item)
            except Exception as e:
                logger.error("[session-chat-store] Failed to get_session: %s", e)
                return None

    def get_chat_id(self, session_id: str) -> Optional[str]:
        """获取 session_id 对应的 chat_id

        Args:
            session_id: Claude 会话 ID

        Returns:
            群聊 chat_id，不存在、已过期或已解散返回 None
        """
        item = self.get_session(session_id)
        return item.get('chat_id') if item else None

    def get_command(self, session_id: str) -> Optional[str]:
        """获取 session_id 对应的 claude_command

        Args:
            session_id: Claude 会话 ID

        Returns:
            claude_command 字符串，不存在/已 dissolved/已过期返回 None
        """
        item = self.get_session(session_id)
        return item.get('claude_command') if item else None

    def get_last_message_id(self, session_id: str) -> str:
        """获取 session_id 对应的 last_message_id（最近一条消息 ID）

        Args:
            session_id: Claude 会话 ID

        Returns:
            last_message_id 字符串，不存在/已 dissolved/已过期返回空字符串
        """
        item = self.get_session(session_id)
        return item.get('last_message_id', '') if item else ''

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

        group 条目区分处理：
        - 非 group 条目：超过 EXPIRE_SECONDS 直接删
        - group 且已 dissolved：超过 EXPIRE_SECONDS 直接删
        - group 且未 dissolved：跳过，留给自动解散或手动 /groups dissolve

        Returns:
            清理的条目数量
        """
        with self._file_lock:
            try:
                data = self._load()
                now = time.time()
                expired = []
                for session_id, item in data.items():
                    if now - item.get('updated_at', 0) <= self.EXPIRE_SECONDS:
                        continue
                    if self._is_active_group(item):
                        continue
                    expired.append(session_id)
                if expired:
                    # 先收集需要清理的反向索引，持久化成功后再更新
                    expired_chats = []
                    for session_id in expired:
                        chat_id = data[session_id].get('chat_id', '')
                        if chat_id and self._chat_to_session.get(chat_id) == session_id:
                            expired_chats.append(chat_id)
                        del data[session_id]
                    if self._save(data):
                        for chat_id in expired_chats:
                            self._chat_to_session.pop(chat_id, None)
                        logger.info(f"[session-chat-store] Cleaned {len(expired)} expired mappings")
                return len(expired)
            except Exception as e:
                logger.error(f"[session-chat-store] Failed to cleanup: {e}")
                return 0

    # =========================================================================
    # 反向索引管理
    # =========================================================================

    def _rebuild_chat_index(self) -> None:
        """从持久化数据重建 chat_id -> session_id 反向索引（仅 group 模式条目）

        仅限 __init__ 调用。实例就绪后不要再调用：本方法会先清空索引再重建，
        若 _load() 或中途抛异常，索引会停在空状态；__init__ 场景下索引本就是空的，
        因此无害，但在运行态调用会丢失并发读线程正在依赖的索引内容。
        """
        with self._file_lock:
            try:
                data = self._load()
                self._chat_to_session = {}
                for session_id, item in data.items():
                    if not self._is_active_group(item):
                        continue
                    chat_id = item.get('chat_id', '')
                    if chat_id:
                        self._chat_to_session[chat_id] = session_id
                if self._chat_to_session:
                    logger.info("[session-chat-store] Rebuilt index: %d group chat mappings",
                                len(self._chat_to_session))
            except Exception as e:
                logger.error("[session-chat-store] Failed to rebuild index: %s", e)

    @staticmethod
    def _is_active_group(item: Dict[str, Any]) -> bool:
        """判断是否为活跃（未解散）的 group 条目"""
        return bool(item.get('group_active') and not item.get('dissolved'))

    def get_session_by_chat_id(self, chat_id: str) -> Optional[str]:
        """通过 chat_id 反查当前活跃的 session_id

        反向索引 _chat_to_session 仅维护 group_active=True 且未 dissolved 的 session
        （见 save() 和 mark_dissolved()），所以返回值一定对应该群聊当前接管消息路由
        的活跃会话。同一 chat_id 在 /new 替换场景下历史 session 会被置为
        group_active=False 并从索引中剔除，不会被本方法返回。

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            该群聊当前活跃 session 的 ID，无活跃 session 返回 None
        """
        return self._chat_to_session.get(chat_id)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """返回所有 session 的浅拷贝（不做过期/dissolved 过滤）

        调用方需自行判断 dissolved、group_active、updated_at 等字段。
        适合需要一次性遍历所有 session 的批量场景（如 /groups list 聚合展示），
        避免循环内多次加锁 + 读文件。

        Returns:
            {session_id: {...item}, ...}
        """
        with self._file_lock:
            data = self._load()
        return {sid: dict(item) for sid, item in data.items()}

    def find_by_prefix(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """按 session_id 前缀查找匹配的 session

        不过滤 dissolved/过期——调用方按需决定是否允许（如 /attach 需要复活
        dissolved session）。

        Args:
            prefix: session_id 前缀

        Returns:
            {session_id: data, ...}，无匹配返回空 dict
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

    def get_chat_last_active(self) -> Dict[str, int]:
        """返回 chat_id → max(session.updated_at) 映射

        同一 chat_id 可能对应多条 session 记录（群聊中 /new 替换场景下历史
        session 保留原 chat_id），取 updated_at 最新的作为群聊活跃时间。
        不过滤 group_active——历史 session 的 updated_at 也反映群聊曾经的活跃度。
        已 dissolved 的 session 跳过，避免"死"时间戳干扰。

        调用方需结合 GroupSeqStore 过滤出服务创建的群聊。

        Returns:
            {chat_id: updated_at, ...}，已解散的群聊不包含在内
        """
        # 锁仅保护文件 I/O，遍历在锁外进行
        try:
            with self._file_lock:
                data = self._load()
        except Exception as e:
            logger.error("[session-chat-store] Failed to load in get_chat_last_active: %s", e)
            return {}

        result: Dict[str, int] = {}
        for item in data.values():
            if item.get('dissolved'):
                continue
            chat_id = item.get('chat_id', '')
            if not chat_id:
                continue
            updated_at = item.get('updated_at', 0)
            if chat_id not in result or updated_at > result[chat_id]:
                result[chat_id] = updated_at
        return result

    # =========================================================================
    # group 模式状态管理
    # =========================================================================

    def mark_dissolved(self, chat_id: str) -> bool:
        """按 chat_id 批量标记所有引用该群聊的 session 为已解散

        解散一个群聊意味着所有引用该 chat_id 的 session 都失效（含 group_active=False
        的历史 session）。否则在终端 resume 旧 session 时，hook 会把消息发到已解散的群聊。

        仅记录状态，不调用飞书 API。调用方应先完成飞书侧的群聊解散，
        成功后再调用此方法标记。

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            是否成功
        """
        if not chat_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                now = int(time.time())
                # 标记所有引用同一 chat_id 的 session
                marked = []
                for sid, entry in data.items():
                    if entry.get('chat_id') == chat_id:
                        entry['dissolved'] = True
                        entry['updated_at'] = now
                        marked.append(sid)
                if not marked:
                    return True  # 没有 session 引用，无需标记
                result = self._save(data)
                if result:
                    self._chat_to_session.pop(chat_id, None)
                    logger.info("[session-chat-store] Marked dissolved: chat_id=%s, sessions=%s",
                                chat_id, marked)
                return result
            except Exception as e:
                logger.error("[session-chat-store] Failed to mark_dissolved: %s", e)
                return False

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
