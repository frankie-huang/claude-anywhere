"""Group Chat 归属 + seq 统一存储

归属端: 飞书网关
使用方: feishu.py（群聊生命周期管理的唯一权威数据源）

仅记录由网关侧通过飞书 API 创建的群聊（含 seq 归属），
用户自建群聊不在此 store 中。
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class GroupChatStore:
    """群聊归属 + seq 统一存储

    只记录由网关侧通过飞书 API 创建的群聊，用户自建群聊不在此 store 中。
    store 中有记录即可被解散、出现在 /groups 列表；无记录则跳过。

    用途：
    - 自动解散：按 per-binding dissolve_days 清理空闲群聊（main.py _cleanup_group_chats）
    - 手动解散：/groups dissolve 按序号解散，只解散当前 owner 的群（_dissolve_groups）
    - 群聊列表：/groups 列出当前 owner 所有服务创建的群聊（_list_groups）
    - 归属校验：batch_dissolve_groups 按 owner 过滤，防止误删他人群聊

    数据结构:

        {
            "ou_xxx": {                  # owner_id
                "1": {                   # seq (JSON 字符串键，内部 int)
                    "chat_id": "oc_xxx",
                    "created_at": 1706745600
                },
                ...
            },
            ...
        }

    Seq 语义：
    - per-owner 单调递增，从 1 开始
    - 已 remove 的 seq 不回收（保持序号稳定，便于 /groups dissolve <seq> 引用）；
      _max_seq 在 _rebuild_index 中从磁盘全量重建，无需持久化也不受 remove 影响
    - 不同 owner 的 seq 独立，互不干扰

    内存索引（启动时从磁盘重建，写入时同步更新）：
    - `_chat_index`: chat_id → (owner_id, seq)，加速 O(1) 的归属与 seq 查询
    - `_max_seq`: owner_id → 当前最大 seq，加速 allocate

    并发模型：
    - 写路径（allocate / remove）持 _file_lock，先写磁盘、_save() 成功后再更新索引
    - 读路径（get_owner / is_service_created / get_seq）走内存索引，
      依赖 CPython GIL 对 dict 单次操作的原子性
    - get_chat_by_seq / get_chats_by_owner / get_all 需要遍历 data，走 _file_lock 保护
    """

    _instance: Optional['GroupChatStore'] = None
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'group_chats.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

        # 内存索引：chat_id → (owner_id, seq)
        self._chat_index: Dict[str, Tuple[str, int]] = {}
        # 每个 owner 的当前最大 seq（用于 allocate 时单调递增）
        self._max_seq: Dict[str, int] = {}

        self._rebuild_index()

        logger.info("[group-chat-store] Initialized with data_dir=%s", data_dir)

    # =========================================================================
    # 单例
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

    def allocate(self, owner_id: str, chat_id: str) -> int:
        """为新群聊分配 seq 并记录归属。

        同 chat_id 已存在则返回已分配的 seq，不做重复登记（幂等）。

        Args:
            owner_id: 创建者的飞书用户 ID
            chat_id: 飞书群聊 ID

        Returns:
            分配的 seq；失败返回 0
        """
        if not owner_id or not chat_id:
            logger.warning("[group-chat-store] Refused allocate with empty owner=%r chat=%r",
                           owner_id, chat_id)
            return 0
        with self._file_lock:
            try:
                data = self._load()
                # 幂等：chat_id 已存在则返回原 seq
                existing = self._chat_index.get(chat_id)
                if existing:
                    return existing[1]

                owner_bucket = data.setdefault(owner_id, {})
                allocated = self._max_seq.get(owner_id, 0) + 1
                owner_bucket[str(allocated)] = {
                    'chat_id': chat_id,
                    'created_at': int(time.time()),
                }
                if not self._save(data):
                    return 0
                # 更新索引
                self._chat_index[chat_id] = (owner_id, allocated)
                self._max_seq[owner_id] = allocated
                logger.info("[group-chat-store] Allocated owner=%s seq=%d chat=%s",
                            owner_id, allocated, chat_id)
                return allocated
            except Exception as e:
                logger.error("[group-chat-store] Failed to allocate: %s", e)
                return 0

    def remove(self, chat_id: str) -> bool:
        """删除 chat_id 记录（群聊解散后调用）。

        不存在也返回 True（幂等，调用方收到即"磁盘上该 chat_id 无记录"）。
        """
        if not chat_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                entry = self._chat_index.get(chat_id)
                if not entry:
                    # 磁盘也可能一致 无记录
                    return True
                owner_id, seq = entry
                seq_key = str(seq)
                owner_bucket = data.get(owner_id)
                if owner_bucket and seq_key in owner_bucket:
                    del owner_bucket[seq_key]
                    if not owner_bucket:
                        del data[owner_id]
                    if not self._save(data):
                        return False
                # 索引同步
                self._chat_index.pop(chat_id, None)
                # _max_seq 不回退（保持单调递增）
                logger.info("[group-chat-store] Removed owner=%s seq=%d chat=%s",
                            owner_id, seq, chat_id)
                return True
            except Exception as e:
                logger.error("[group-chat-store] Failed to remove: %s", e)
                return False

    # =========================================================================
    # 读接口（内存索引，O(1)）
    # =========================================================================

    def get_owner(self, chat_id: str) -> Optional[str]:
        """获取群聊的 owner_id。不存在返回 None。"""
        entry = self._chat_index.get(chat_id)
        return entry[0] if entry else None

    def get_seq(self, chat_id: str) -> Optional[int]:
        """获取群聊的 seq。不存在返回 None。"""
        entry = self._chat_index.get(chat_id)
        return entry[1] if entry else None

    def is_service_created(self, chat_id: str) -> bool:
        """判断是否服务创建的群聊（在本 store 有记录即为服务创建）。"""
        return chat_id in self._chat_index

    def get_chat_by_seq(self, owner_id: str, seq: int) -> Optional[str]:
        """按 owner + seq 反查 chat_id。"""
        with self._file_lock:
            try:
                data = self._load()
                return data.get(owner_id, {}).get(str(seq), {}).get('chat_id')
            except Exception as e:
                logger.error("[group-chat-store] get_chat_by_seq error: %s", e)
                return None

    def get_chats_by_owner(self, owner_id: str) -> List[Dict[str, Any]]:
        """按 owner 返回该 owner 的所有群聊列表，按 seq 升序。

        Returns:
            [{'chat_id': str, 'seq': int, 'created_at': int}, ...]
        """
        with self._file_lock:
            try:
                data = self._load()
                owner_bucket = data.get(owner_id, {})
                result = []
                for seq_key, item in owner_bucket.items():
                    try:
                        seq = int(seq_key)
                    except (TypeError, ValueError):
                        continue
                    result.append({
                        'chat_id': item.get('chat_id', ''),
                        'seq': seq,
                        'created_at': item.get('created_at', 0),
                    })
                result.sort(key=lambda x: x['seq'])
                return result
            except Exception as e:
                logger.error("[group-chat-store] Failed to get_chats_by_owner: %s", e)
                return []

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """返回所有记录的原始嵌套结构副本。

        Returns:
            {owner_id: {seq_str: {'chat_id': str, 'created_at': int}, ...}, ...}
        """
        with self._file_lock:
            try:
                return self._load()
            except Exception as e:
                logger.error("[group-chat-store] Failed to get_all: %s", e)
                return {}

    # =========================================================================
    # 索引维护（内部）
    # =========================================================================

    def _rebuild_index(self) -> None:
        """从持久化数据重建内存索引。仅 __init__ 阶段调用。"""
        with self._file_lock:
            try:
                data = self._load()
                self._chat_index = {}
                self._max_seq = {}
                for owner_id, owner_bucket in data.items():
                    for seq_key, item in owner_bucket.items():
                        try:
                            seq = int(seq_key)
                        except (TypeError, ValueError):
                            continue
                        chat_id = item.get('chat_id', '')
                        if not chat_id:
                            continue
                        self._chat_index[chat_id] = (owner_id, seq)
                        if seq > self._max_seq.get(owner_id, 0):
                            self._max_seq[owner_id] = seq
                if self._chat_index:
                    logger.info("[group-chat-store] Rebuilt index: owners=%d chats=%d",
                                len(self._max_seq), len(self._chat_index))
            except Exception as e:
                logger.error("[group-chat-store] Failed to rebuild index: %s", e)

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
