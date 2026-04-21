"""Group Seq 存储

归属端: Callback 后端
使用方: callback.py

记录服务创建群聊的短序号（chat_id → seq），用于：
- /groups list 展示活跃群聊
- /groups dissolve <seq> 按序号解散
- 自动解散：提供"服务创建"的身份判定，活跃度由 session_chat_store.updated_at 决定

职责单一：只管 callback 侧"服务创建群聊的元数据"，不涉及 session 映射
（session_chat_store 管 session → chat_id 的映射和活跃度）。未被本 store
收录的群聊（老版本遗留、用户手动创建等）不纳入上述三类操作，有意为之。
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class GroupSeqStore:
    """管理服务创建群聊的短序号存储（归属端: Callback 后端）

    数据结构::

        {
            "oc_xxx": {
                "seq": 3,
                "created_at": 1706745600
            }
        }

    内存反向索引: seq -> chat_id，加速按 seq 查询。
    重要：仅在 _save() 成功后更新，保证与持久化数据一致。
    """

    _instance: Optional['GroupSeqStore'] = None
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'group_seqs.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        self._seq_to_chat: Dict[int, str] = {}
        # 缓存当前最大 seq，避免 allocate 时遍历（seq 单调递增，不随 remove 回收）
        self._max_seq: int = 0
        self._rebuild_index()
        logger.info("[group-seq-store] Initialized with data_dir=%s", data_dir)

    def _rebuild_index(self) -> None:
        """从持久化数据重建 seq -> chat_id 反向索引和 _max_seq"""
        with self._file_lock:
            try:
                data = self._load()
                self._seq_to_chat = {}
                self._max_seq = 0
                for chat_id, item in data.items():
                    seq = item.get('seq')
                    if seq is not None:
                        self._seq_to_chat[seq] = chat_id
                        if seq > self._max_seq:
                            self._max_seq = seq
                if self._seq_to_chat:
                    logger.info("[group-seq-store] Rebuilt index: %d entries, max_seq=%d",
                                len(self._seq_to_chat), self._max_seq)
            except Exception as e:
                logger.error("[group-seq-store] Failed to rebuild index: %s", e)

    @classmethod
    def initialize(cls, data_dir: str) -> 'GroupSeqStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['GroupSeqStore']:
        return cls._instance

    def allocate(self, chat_id: str) -> int:
        """为新群聊分配 seq 并保存（已存在则返回已分配的 seq）

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            分配的 seq
        """
        with self._file_lock:
            try:
                data = self._load()
                if chat_id in data:
                    return data[chat_id].get('seq', 0)

                # seq 单调递增，不回收已删除的序号
                allocated = self._max_seq + 1

                data[chat_id] = {
                    'seq': allocated,
                    'created_at': int(time.time()),
                }
                if not self._save(data):
                    # 持久化失败不能返回 allocated：_max_seq 未推进，
                    # 下次 allocate 会把同一个 seq 分给另一个 chat_id
                    return 0
                self._seq_to_chat[allocated] = chat_id
                self._max_seq = allocated
                logger.info("[group-seq-store] Allocated seq=%d for chat_id=%s", allocated, chat_id)
                return allocated
            except Exception as e:
                logger.error("[group-seq-store] Failed to allocate: %s", e)
                return 0

    def remove(self, chat_id: str) -> bool:
        """删除 chat_id 记录（解散后调用）

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            是否成功
        """
        with self._file_lock:
            try:
                data = self._load()
                if chat_id not in data:
                    return True
                seq = data[chat_id].get('seq')
                del data[chat_id]
                result = self._save(data)
                if result:
                    if seq is not None:
                        self._seq_to_chat.pop(seq, None)
                    logger.info("[group-seq-store] Removed: %s (seq=%s)", chat_id, seq)
                return result
            except Exception as e:
                logger.error("[group-seq-store] Failed to remove: %s", e)
                return False

    def get_seq(self, chat_id: str) -> Optional[int]:
        """按 chat_id 查 seq（主键查询）

        Args:
            chat_id: 飞书群聊 ID

        Returns:
            seq，不存在返回 None（即该 chat_id 非服务创建群聊）
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(chat_id)
                return item.get('seq') if item else None
            except Exception as e:
                logger.error("[group-seq-store] Failed to get_seq: %s", e)
                return None

    def get_chat_by_seq(self, seq: int) -> str:
        """通过 seq 反查 chat_id

        Args:
            seq: 群聊短序号

        Returns:
            chat_id，不存在返回空字符串
        """
        return self._seq_to_chat.get(seq, '')

    def get_all(self) -> List[Dict[str, Any]]:
        """获取所有记录，按 seq 升序

        Returns:
            [{'chat_id': str, 'seq': int, 'created_at': int}, ...]
        """
        with self._file_lock:
            try:
                data = self._load()
                result = [
                    {
                        'chat_id': chat_id,
                        'seq': item.get('seq'),
                        'created_at': item.get('created_at', 0),
                    }
                    for chat_id, item in data.items()
                ]
                result.sort(key=lambda x: x['seq'] or 0)
                return result
            except Exception as e:
                logger.error("[group-seq-store] Failed to get_all: %s", e)
                return []

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("[group-seq-store] Invalid JSON, starting fresh")
            return {}
        except IOError as e:
            logger.error("[group-seq-store] Failed to load: %s", e)
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error("[group-seq-store] Failed to save: %s", e)
            return False
