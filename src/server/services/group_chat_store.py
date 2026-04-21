"""Group Chat 归属存储

归属端: 飞书网关
使用方: feishu.py

记录由服务创建的群聊及其 owner，用于：
- 自动解散：只解散服务创建的群聊，不误杀用户已有群聊
- 手动解散：/groups dissolve 只能解散由服务创建且属于当前 owner 的群聊
- 群聊列表：/groups list 标注当前 owner 可解散的群聊
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, Dict, Any, List, Set

logger = logging.getLogger(__name__)


class GroupChatStore:
    """管理 chat_id -> owner_id 的存储（归属端: 飞书网关）

    只记录由服务创建的群聊。store 中有记录即为服务创建，可被解散；
    无记录即为用户已有群聊，不可被解散。

    数据结构::

        {
            "oc_xxx": {
                "owner_id": "ou_xxx",
                "created_at": 1706745600
            }
        }

    内存索引（启动时从磁盘重建，写入时同步更新）:
    - 正向索引: chat_id -> owner_id，加速 get_owner / is_service_created 查询
    - 反向索引: owner_id -> set(chat_id)，加速按 owner 查询

    为何两条索引都走内存（与 SessionChatStore 的差异）:
    本 Store 没有过期机制——群聊只要不被 dissolve 就长期保留，记录数随时间
    单调增长，若每次查询都 _load() 反序列化整个 JSON，I/O 开销会线性累积。
    SessionChatStore 有 EXPIRE_SECONDS + cleanup_expired 兜底规模上限，所以
    仅为热路径（chat_id -> session_id）加了反向索引、其他读路径仍走 _load()；
    本 Store 则对所有读路径统一走内存索引。

    并发模型:
    - 写路径（save / remove）持 _file_lock，先改磁盘、_save() 成功后再更新内存索引
    - 读路径（get_owner / is_service_created / get_by_owner）直接走内存索引、不加锁，
      依赖 CPython GIL 对 dict/set 单次操作的原子性
    - 维护者注意：读方法内不要调用 _file_lock 保护的方法（如 _load()），
      新增读方法时沿用现有"只读内存索引"模式
    """

    _instance: Optional['GroupChatStore'] = None
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'group_chats.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        # 内存索引：仅在 _save() 成功后更新，保证与持久化数据一致
        self._chat_to_owner: Dict[str, str] = {}           # 正向: chat_id -> owner_id
        self._owner_to_chats: Dict[str, Set[str]] = {}     # 反向: owner_id -> set(chat_id)
        self._rebuild_index()
        logger.info("[group-chat-store] Initialized with data_dir=%s", data_dir)

    # =========================================================================
    # 单例管理
    # =========================================================================

    @classmethod
    def initialize(cls, data_dir: str) -> 'GroupChatStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['GroupChatStore']:
        return cls._instance

    # =========================================================================
    # 写接口
    # =========================================================================

    def save(self, chat_id: str, owner_id: str) -> bool:
        """记录服务创建的群聊

        注意：此 store 仅支持新建，不支持更新。chat_id 已有记录则拒绝并返回 False，
        避免覆盖原有 owner 与 created_at（重复调用属 bug，靠 ERROR 日志暴露）。
        命名沿用 save 以与其他 store（SessionChatStore 等）保持风格一致。

        Args:
            chat_id: 飞书群聊 ID
            owner_id: 创建者的飞书用户 ID

        Returns:
            是否保存成功（参数非法、chat_id 已存在、I/O 失败均返回 False）
        """
        if not chat_id or not owner_id:
            logger.warning("[group-chat-store] Refused save with empty chat_id=%r owner_id=%r",
                           chat_id, owner_id)
            return False
        with self._file_lock:
            try:
                data = self._load()
                if chat_id in data:
                    existing_owner = data[chat_id].get('owner_id', '')
                    logger.error(
                        "[group-chat-store] Refused duplicate save: chat_id=%s existing_owner=%s new_owner=%s",
                        chat_id, existing_owner, owner_id)
                    return False
                data[chat_id] = {
                    'owner_id': owner_id,
                    'created_at': int(time.time())
                }
                result = self._save(data)
                if result:
                    self._index_add(chat_id, owner_id)
                    logger.info("[group-chat-store] Saved: %s -> %s", chat_id, owner_id)
                return result
            except Exception as e:
                logger.error("[group-chat-store] Failed to save: %s", e)
                return False

    def remove(self, chat_id: str) -> bool:
        """删除群聊记录（解散后调用）

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            是否成功
        """
        with self._file_lock:
            try:
                data = self._load()
                if chat_id not in data:
                    # 磁盘无记录但内存索引残留（外部篡改/异常中断）时兜底清理
                    self._index_remove(chat_id)
                    return True
                del data[chat_id]
                result = self._save(data)
                if result:
                    self._index_remove(chat_id)
                    logger.info("[group-chat-store] Removed: %s", chat_id)
                return result
            except Exception as e:
                logger.error("[group-chat-store] Failed to remove: %s", e)
                return False

    # =========================================================================
    # 读接口
    # =========================================================================

    def get_owner(self, chat_id: str) -> Optional[str]:
        """获取群聊的 owner_id（走内存正向索引，O(1)）

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            owner_id，不存在返回 None
        """
        return self._chat_to_owner.get(chat_id)

    def is_service_created(self, chat_id: str) -> bool:
        """判断群聊是否由服务创建（走内存正向索引，O(1)）

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            True 表示服务创建，False 表示非服务创建或不存在
        """
        return chat_id in self._chat_to_owner

    def get_by_owner(self, owner_id: str) -> List[str]:
        """获取指定 owner 的所有群聊 ID

        Args:
            owner_id: 飞书用户 ID

        Returns:
            chat_id 列表
        """
        chats = self._owner_to_chats.get(owner_id)
        if not chats:
            return []
        # 先 copy() 再 list()：set.copy() 走 C 级 set_merge，直接遍历底层 table、
        # 不走 Python 迭代协议，单次 C 调用内 GIL 不释放，能拿到与写线程隔离的独立快照；
        # 随后对快照 list() 就不会碰到并发 add/discard 触发的 "Set changed size during iteration"。
        # 不能简写成 list(chats) —— 那样走迭代协议，并发修改时会抛 RuntimeError。
        return list(chats.copy())

    # =========================================================================
    # 索引维护（内部）
    # =========================================================================

    def _rebuild_index(self):
        """从持久化数据重建内存索引

        仅限 __init__ 调用。实例就绪后不要再调用：本方法会先清空索引再重建，
        若 _load() 或中途抛异常，索引会停在空状态；__init__ 场景下索引本就是空的，
        因此无害，但在运行态调用会丢失并发读线程正在依赖的索引内容。
        """
        with self._file_lock:
            try:
                data = self._load()
                self._chat_to_owner = {}
                self._owner_to_chats = {}
                for chat_id, item in data.items():
                    owner_id = item.get('owner_id', '')
                    if owner_id:
                        self._index_add(chat_id, owner_id)
                if self._chat_to_owner:
                    logger.info("[group-chat-store] Rebuilt index: %d owners, %d chats",
                                len(self._owner_to_chats), len(self._chat_to_owner))
            except Exception as e:
                logger.error("[group-chat-store] Failed to rebuild index: %s", e)

    def _index_add(self, chat_id: str, owner_id: str) -> None:
        """同时更新正反向索引

        - 正向: _chat_to_owner[chat_id] = owner_id
        - 反向: _owner_to_chats[owner_id] 集合加入 chat_id
        """
        self._chat_to_owner[chat_id] = owner_id
        self._owner_to_chats.setdefault(owner_id, set()).add(chat_id)

    def _index_remove(self, chat_id: str) -> Optional[str]:
        """同时从正反向索引移除 chat_id，反向桶空时一并删除

        Returns:
            被移除的 owner_id；若 chat_id 不在索引中返回 None
        """
        owner_id = self._chat_to_owner.pop(chat_id, None)
        if owner_id and owner_id in self._owner_to_chats:
            bucket = self._owner_to_chats[owner_id]
            bucket.discard(chat_id)
            if not bucket:
                del self._owner_to_chats[owner_id]
        return owner_id

    # =========================================================================
    # 底层 I/O（内部）
    # =========================================================================

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("[group-chat-store] Invalid JSON, starting fresh")
            return {}
        except IOError as e:
            logger.error("[group-chat-store] Failed to load: %s", e)
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error("[group-chat-store] Failed to save: %s", e)
            return False
