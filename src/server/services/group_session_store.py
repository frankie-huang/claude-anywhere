"""Group-Session 映射存储

归属端: 飞书网关
使用方:
    - feishu.py: 直接读写（save / remove / touch / find_by_session / get），
      覆盖 /new group / /attach / /groups dissolve / 入站路由活跃刷新等场景
    - SessionFacade.resolve_group_chat: 仅包装 get() 提供给上层做 chat → session
      反查，不持有额外逻辑

维护 (owner_id, chat_id) → 当前活跃 session 信息的映射，用于将群聊入站消息
快速路由到对应 Callback 后端的 session。

为何带 owner_id 维度：gateway 跨 owner 共用此 store，同一个 chat_id 在不同
owner 下可能有不同活跃 session（用户共享群 + /attach 场景），单 chat_id 主键
会让后写覆盖先写，造成路由错乱。

与 MessageSessionStore 对称：
    - MessageSessionStore: message_id → session 信息（用户回复消息继续会话）
    - GroupSessionStore:   (owner_id, chat_id) → session 信息（群聊模式下消息路由）

两者都是 gateway 为了消息路由目的而维护的本地持久化存储，Callback 后端
不直接访问此 Store。
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class GroupSessionStore:
    """管理 (owner_id, chat_id) -> 当前活跃 session 信息的映射

    数据结构（嵌套 owner → chat）:

        {
            "ou_xxx_owner": {
                "oc_yyy_chat": {
                    "session_id": "...",
                    "project_dir": "/path/to/project",   # 入站消息转发到 /cb/claude/continue 时必传
                    "last_active_at": 1706745600,        # 最近一次群聊活动时间（供自动解散判断）
                    "created_at": 1706745600
                }
            }
        }

    last_active_at 刷新时机（任一触发即刷新，防止活跃群被误判空闲而自动解散）：
      - 入站：群聊普通消息路由到 session（touch）、群聊 /reply 命令（touch）
      - 出站：handle_send_message 成功发送到群聊（touch，覆盖终端对话场景）
      - 写入：群内 /new（save）、ensure-chat 建群（save）、/attach 绑定（save）

    每个 (owner_id, chat_id) 只保留一条当前活跃 session 记录。/attach 或 /new
    替换时直接覆盖。/groups dissolve 或自动解散时从本表删除。

    字段对齐 MessageSessionStore：
      - 只存入站转发必需的字段（session_id + project_dir）
      - claude_command 不存——入站 /cb/claude/continue 不是必传；/new 继承
        这个低频场景通过其他路径回源 callback 拿权威值

    内存反向索引: (owner_id, session_id) → chat_id
      - find_by_session 走 O(1) 内存查询，避免每次 _load + 全表遍历
      - 复合 key 含 owner_id：避免不同 owner 下 session_id 极小概率撞车时
        反向索引被静默覆盖
      - 并发模型沿用 group_chat_store._chat_index：写路径在 _file_lock 内、
        _save() 成功后再更新索引；读路径直接查内存，依赖 CPython GIL
        对 dict 单次操作的原子性
    """

    _instance: Optional['GroupSessionStore'] = None
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'group_sessions.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

        # 反向索引：(owner_id, session_id) → chat_id（启动时重建，写路径同步更新）
        self._owner_session_to_chat: Dict[Tuple[str, str], str] = {}
        self._rebuild_index()

        logger.info("[group-session-store] Initialized with data_dir=%s", data_dir)

    @classmethod
    def initialize(cls, data_dir: str) -> 'GroupSessionStore':
        """初始化单例实例"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['GroupSessionStore']:
        """获取单例实例；未初始化返回 None"""
        return cls._instance

    # =========================================================================
    # 读
    # =========================================================================

    def get(self, owner_id: str, chat_id: str) -> Optional[Dict[str, Any]]:
        """获取 (owner_id, chat_id) 当前绑定的 session 信息

        Returns:
            {'session_id', 'project_dir', 'last_active_at', 'created_at'}
            找不到返回 None
        """
        if not owner_id or not chat_id:
            return None
        with self._file_lock:
            try:
                data = self._load()
                return data.get(owner_id, {}).get(chat_id)
            except Exception as e:
                logger.error("[group-session-store] Failed to get: %s", e)
                return None

    def get_by_owner(self, owner_id: str) -> Dict[str, Dict[str, Any]]:
        """获取指定 owner 下所有 chat_id → session 信息的映射（一次磁盘读）

        Returns:
            {chat_id: {'session_id', 'project_dir', 'last_active_at', 'created_at'}, ...}
            owner 不存在返回空 dict
        """
        if not owner_id:
            return {}
        with self._file_lock:
            try:
                data = self._load()
                return dict(data.get(owner_id, {}))
            except Exception as e:
                logger.error("[group-session-store] Failed to get_by_owner: %s", e)
                return {}

    def find_by_session(self, owner_id: str, session_id: str) -> Optional[str]:
        """在指定 owner 范围内反查 session 当前绑定的 chat_id（O(1) 内存索引）

        Returns:
            该 owner 下绑定此 session 的 chat_id；未绑定返回 None
        """
        if not owner_id or not session_id:
            return None
        return self._owner_session_to_chat.get((owner_id, session_id))

    # =========================================================================
    # 写
    # =========================================================================

    def save(self, owner_id: str, chat_id: str, session_id: str,
             project_dir: str = '') -> bool:
        """保存或覆盖 (owner_id, chat_id) 的 session 绑定

        同 (owner_id, chat_id) 下新来的 session 会直接替换旧的（/new 覆盖 /
        /attach 切换）。新记录的 last_active_at 初始化为当前时间。
        """
        if not owner_id or not chat_id or not session_id:
            logger.warning("[group-session-store] save: missing owner_id/chat_id/session_id")
            return False
        with self._file_lock:
            try:
                data = self._load()
                now = int(time.time())
                owner_bucket = data.setdefault(owner_id, {})
                existed = owner_bucket.get(chat_id)
                created_at = existed.get('created_at', now) if existed else now
                # 旧 session_id（若 (owner_id, chat_id) 之前绑过其他 session），
                # 用于持久化成功后清理反向索引中的过期条目
                prev_session_id = existed.get('session_id', '') if existed else ''
                # 清理本 session 的旧 chat 行（仅当该行仍指向本 session，未被其他 session 接管）
                stale_chat_id = self._owner_session_to_chat.get((owner_id, session_id))
                if stale_chat_id and stale_chat_id != chat_id:
                    stale_entry = owner_bucket.get(stale_chat_id)
                    if stale_entry and stale_entry.get('session_id') == session_id:
                        del owner_bucket[stale_chat_id]
                        logger.info("[group-session-store] Reclaimed stale chat row: "
                                    "owner=%s session=%s old_chat=%s -> new_chat=%s",
                                    owner_id, session_id, stale_chat_id, chat_id)
                owner_bucket[chat_id] = {
                    'session_id': session_id,
                    'project_dir': project_dir,
                    'last_active_at': now,
                    'created_at': created_at,
                }
                if not self._save(data):
                    return False
                # 反向索引同步：清旧 + 写新
                if prev_session_id and prev_session_id != session_id:
                    prev_key = (owner_id, prev_session_id)
                    if self._owner_session_to_chat.get(prev_key) == chat_id:
                        self._owner_session_to_chat.pop(prev_key, None)
                self._owner_session_to_chat[(owner_id, session_id)] = chat_id
                logger.info("[group-session-store] Saved: owner=%s chat=%s -> session=%s",
                            owner_id, chat_id, session_id)
                return True
            except Exception as e:
                logger.error("[group-session-store] Failed to save: %s", e)
                return False

    def touch(self, owner_id: str, chat_id: str) -> bool:
        """刷新 (owner_id, chat_id) 的 last_active_at 为当前时间。

        条目不存在时返回 False，不报错。
        """
        if not owner_id or not chat_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                owner_bucket = data.get(owner_id)
                if not owner_bucket:
                    return False
                item = owner_bucket.get(chat_id)
                if not item:
                    return False
                # 同秒去抖：用户连发消息时同秒多次 touch 没必要重复落盘。
                # 自动解散按天判断，秒级精度本就过剩
                now = int(time.time())
                if item.get('last_active_at', 0) >= now:
                    return True
                # item 是 owner_bucket[chat_id] 的引用，原地修改即可
                item['last_active_at'] = now
                return self._save(data)
            except Exception as e:
                logger.error("[group-session-store] Failed to touch: %s", e)
                return False

    def remove(self, owner_id: str, chat_id: str) -> bool:
        """删除 (owner_id, chat_id) 的映射条目（不存在返回 False）"""
        if not owner_id or not chat_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                owner_bucket = data.get(owner_id)
                if not owner_bucket or chat_id not in owner_bucket:
                    return False
                # 记录 session_id 用于持久化成功后清理反向索引
                removed_session_id = owner_bucket[chat_id].get('session_id', '')
                del owner_bucket[chat_id]
                if not owner_bucket:
                    del data[owner_id]
                if not self._save(data):
                    return False
                if removed_session_id:
                    key = (owner_id, removed_session_id)
                    if self._owner_session_to_chat.get(key) == chat_id:
                        self._owner_session_to_chat.pop(key, None)
                logger.info("[group-session-store] Removed: owner=%s chat=%s",
                            owner_id, chat_id)
                return True
            except Exception as e:
                logger.error("[group-session-store] Failed to remove: %s", e)
                return False

    # =========================================================================
    # 内部
    # =========================================================================

    def _rebuild_index(self) -> None:
        """从持久化数据重建 (owner_id, session_id) → chat_id 反向索引。

        仅 __init__ 阶段调用。
        """
        with self._file_lock:
            try:
                data = self._load()
                self._owner_session_to_chat = {}
                for owner_id, owner_bucket in data.items():
                    for chat_id, item in owner_bucket.items():
                        sid = item.get('session_id', '')
                        if sid:
                            self._owner_session_to_chat[(owner_id, sid)] = chat_id
                if self._owner_session_to_chat:
                    logger.info("[group-session-store] Rebuilt index: %d sessions",
                                len(self._owner_session_to_chat))
            except Exception as e:
                logger.error("[group-session-store] Failed to rebuild index: %s", e)

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("[group-session-store] Invalid JSON in %s, starting fresh",
                           self._file_path)
            return {}
        except IOError as e:
            logger.error("[group-session-store] Failed to load: %s", e)
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error("[group-session-store] Failed to save: %s", e)
            return False
