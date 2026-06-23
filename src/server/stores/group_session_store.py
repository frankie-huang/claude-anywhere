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

import logging
import time
from typing import Any, Dict, Optional, Tuple

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)


class GroupSessionStore(JsonStore):
    """管理 (owner_id, chat_id) -> 当前活跃 session 信息的映射

    数据结构（嵌套 owner → chat）:

        {
            "ou_xxx_owner": {
                "oc_yyy_chat": {
                    "session_id": "...",
                    "project_dir": "/path/to/project",  # 入站消息转发到 /cb/agent/continue 时必传
                    "new_session": false,               # /clear 后为 true，首条消息触发新建会话后自动清除
                    "last_active_at": 1706745600,       # 最近一次群聊活动时间（供自动解散判断）
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
      - command 不存——入站 /cb/agent/continue 不是必传；/new 继承
        这个低频场景通过其他路径回源 callback 拿权威值

    内存反向索引: (owner_id, session_id) → chat_id
      - find_by_session 走 O(1) 内存查询，避免每次 _load + 全表遍历
      - 复合 key 含 owner_id：避免不同 owner 下 session_id 极小概率撞车时
        反向索引被静默覆盖
      - 并发模型沿用 group_chat_store._chat_index：写路径在 _file_lock 内、
        _save() 成功后再更新索引；读路径直接查内存，依赖 CPython GIL
        对 dict 单次操作的原子性
    """

    STORE_NAME = 'group_sessions'
    LOG_TAG = 'group-session-store'

    def _post_init(self):
        # 反向索引：(owner_id, session_id) → chat_id（启动时重建，写路径同步更新）
        self._owner_session_to_chat: Dict[Tuple[str, str], str] = {}
        # 反向索引：chat_id → owner_id（协作者模式用，启动时重建，写路径同步更新）
        # 同一 chat_id 多 owner 时只存一个：save() 取最后写入，rebuild 取最近活跃。
        # touch() 不同步此索引，运行期与重启后的 owner 选择可能不一致，可接受。
        self._chat_to_owner: Dict[str, str] = {}
        self._rebuild_index()

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

    def find_owner_by_chat(self, chat_id: str) -> Optional[str]:
        """反查 chat_id 归属的 owner_id（O(1) 内存索引，零 I/O）

        协作者模式用：当非 owner 成员在群内发消息时，通过 chat_id 反查到
        owner，进而借用 owner 的 binding 转发消息。

        同一 chat_id 可能被多个 owner /attach（共享群场景），反向索引只存
        最近写入的 owner——协作者进入的是当前活跃的那个 owner 的 session。

        Returns:
            owner_id 或 None
        """
        if not chat_id:
            return None
        return self._chat_to_owner.get(chat_id)

    # =========================================================================
    # 写
    # =========================================================================

    def save(self, owner_id: str, chat_id: str, session_id: str,
             project_dir: str = '', new_session: bool = False) -> bool:
        """保存或覆盖 (owner_id, chat_id) 的 session 绑定

        同 (owner_id, chat_id) 下新来的 session 会直接替换旧的（/new 覆盖 /
        /attach 切换）。新记录的 last_active_at 初始化为当前时间。

        Args:
            new_session: 标记该 session 尚未启动 agent 进程，下次消息应走 /new
                         而非 /continue。/clear 预创建 session 时设为 True，
                         后续 _forward_new_request 中的 save() 不传此参数自动清除。
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
                        if self._chat_to_owner.get(stale_chat_id) == owner_id:
                            self._chat_to_owner.pop(stale_chat_id, None)
                        logger.info("[group-session-store] Reclaimed stale chat row: "
                                    "owner=%s session=%s old_chat=%s -> new_chat=%s",
                                    owner_id, session_id, stale_chat_id, chat_id)
                entry = {
                    'session_id': session_id,
                    'project_dir': project_dir,
                    'last_active_at': now,
                    'created_at': created_at,
                }
                if new_session:
                    entry['new_session'] = True
                owner_bucket[chat_id] = entry
                if not self._save(data):
                    return False
                # 反向索引同步：清旧 + 写新
                if prev_session_id and prev_session_id != session_id:
                    prev_key = (owner_id, prev_session_id)
                    if self._owner_session_to_chat.get(prev_key) == chat_id:
                        self._owner_session_to_chat.pop(prev_key, None)
                self._owner_session_to_chat[(owner_id, session_id)] = chat_id
                self._chat_to_owner[chat_id] = owner_id
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
                if self._chat_to_owner.get(chat_id) == owner_id:
                    self._chat_to_owner.pop(chat_id, None)
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
                self._chat_to_owner = {}
                for owner_id, owner_bucket in data.items():
                    for chat_id, item in owner_bucket.items():
                        sid = item.get('session_id', '')
                        if sid:
                            self._owner_session_to_chat[(owner_id, sid)] = chat_id
                        # 同一 chat_id 多 owner 时，取最近活跃的
                        prev_owner = self._chat_to_owner.get(chat_id)
                        if prev_owner:
                            prev_item = data.get(prev_owner, {}).get(chat_id, {})
                            if item.get('last_active_at', 0) <= prev_item.get('last_active_at', 0):
                                continue
                        self._chat_to_owner[chat_id] = owner_id
                if self._owner_session_to_chat:
                    logger.info("[group-session-store] Rebuilt index: %d sessions",
                                len(self._owner_session_to_chat))
            except Exception as e:
                logger.error("[group-session-store] Failed to rebuild index: %s", e)
