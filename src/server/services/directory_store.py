"""目录相关存储

归属端: Callback 后端
使用方: callback.py, claude.py

管理工作目录的使用历史和静音状态：
  - 使用历史：记录目录使用频率，用于创建新会话时提供常用目录推荐
  - 静音状态：记录被 mute 的目录，终端发起的会话自动继承 mute 状态

飞书网关不应直接调用此 Store，应通过 Callback 后端的 HTTP 接口间接访问。
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# 使用历史过期时间
DIR_EXPIRE_SECONDS = 30 * 24 * 3600  # 30 天过期


class DirectoryStore:
    """管理工作目录使用历史和静音状态

    单文件存储 (directories.json)，每条记录的字段独立存在：
    {
        "/path/to/project": {
            "count": 5,              // 使用历史（可选）
            "last_used": 1706745600, // 使用历史（可选）
            "muted_at": 1706745600   // 静音状态（可选）
        }
    }

    过期清理只移除 count + last_used，保留 muted_at；
    记录变为空 {} 时才整条删除。
    """

    _instance: Optional['DirectoryStore'] = None
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'directories.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        self._migrate_legacy_file(data_dir)  # 旧数据迁移，待旧版本再无流量后可删去
        logger.info("[directory-store] Initialized with data_dir=%s", data_dir)

    @classmethod
    def initialize(cls, data_dir: str) -> 'DirectoryStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['DirectoryStore']:
        return cls._instance

    # =========================================================================
    # 使用历史
    # =========================================================================

    def record_usage(self, project_dir: str) -> bool:
        """记录目录使用

        Args:
            project_dir: 项目工作目录（符号链接会自动解析为真实路径）

        Returns:
            是否保存成功
        """
        if not project_dir:
            return False

        project_dir = os.path.realpath(project_dir)

        with self._file_lock:
            try:
                data = self._load()
                now = int(time.time())

                # 更新目录使用记录
                entry = data.get(project_dir, {})
                entry['count'] = entry.get('count', 0) + 1
                entry['last_used'] = now
                data[project_dir] = entry

                result = self._save(data)
                if result:
                    logger.info("[directory-store] Recorded usage: %s", project_dir)
                return result
            except Exception as e:
                logger.error("[directory-store] Failed to record usage: %s", e)
                return False

    def get_recent_dirs(self, limit: int = 5, min_count: int = 2) -> List[str]:
        """获取近期常用目录列表

        Args:
            limit: 最多返回的目录数量
            min_count: 最小使用次数阈值，使用次数少于此值的目录不返回

        Returns:
            目录路径列表，按使用频率和时间排序，过滤掉不存在的目录
        """
        with self._file_lock:
            try:
                data = self._load()

                # 内存中过滤过期 + 不存在目录（不持久化，实际清理由 cleanup_expired 执行）
                data = self._filter_stale(data)

                # 只取有使用历史的记录
                history_dirs = {
                    path: info for path, info in data.items()
                    if info.get('count', 0) > 0
                }

                # 过滤掉使用次数少于阈值的目录
                valid_dirs = {
                    path: info for path, info in history_dirs.items()
                    if info.get('count', 0) >= min_count
                }

                # 排序：优先按使用次数降序，次数相同按最近使用时间降序
                sorted_dirs = sorted(
                    valid_dirs.items(),
                    key=lambda x: (x[1]['count'], x[1]['last_used']),
                    reverse=True
                )

                # 返回前 N 个目录路径
                return [dir_path for dir_path, _ in sorted_dirs[:limit]]
            except Exception as e:
                logger.error("[directory-store] Failed to get recent dirs: %s", e)
                return []

    # =========================================================================
    # 目录静音
    # =========================================================================

    def mute_dir(self, project_dir: str) -> Optional[bool]:
        """标记目录为静音（符号链接会自动解析为真实路径）

        Returns:
            True  = 本次新增静音
            False = 幂等（已静音）
            None  = 失败或目录不存在
        """
        if not project_dir:
            return None
        project_dir = os.path.realpath(project_dir)
        if not os.path.isdir(project_dir):
            logger.warning("[directory-store] Cannot mute non-existent dir: %s", project_dir)
            return None
        with self._file_lock:
            try:
                data = self._load()
                entry = data.get(project_dir, {})
                if 'muted_at' in entry:
                    return False
                entry['muted_at'] = int(time.time())
                data[project_dir] = entry
                if not self._save(data):
                    return None
                logger.info("[directory-store] Muted dir: %s", project_dir)
                return True
            except Exception as e:
                logger.error("[directory-store] Failed to mute dir: %s", e)
                return None

    def unmute_dir(self, project_dir: str) -> Optional[bool]:
        """取消目录静音（符号链接会自动解析为真实路径）

        Returns:
            True  = 本次取消静音
            False = 幂等（未静音）
            None  = 失败
        """
        if not project_dir:
            return None
        project_dir = os.path.realpath(project_dir)
        with self._file_lock:
            try:
                data = self._load()
                entry = data.get(project_dir, {})
                if 'muted_at' not in entry:
                    return False
                del entry['muted_at']
                if entry:
                    data[project_dir] = entry
                else:
                    del data[project_dir]
                if not self._save(data):
                    return None
                logger.info("[directory-store] Unmuted dir: %s", project_dir)
                return True
            except Exception as e:
                logger.error("[directory-store] Failed to unmute dir: %s", e)
                return None

    def is_dir_muted(self, project_dir: str) -> bool:
        """检查目录是否被静音（符号链接会自动解析为真实路径）"""
        if not project_dir:
            return False
        project_dir = os.path.realpath(project_dir)
        with self._file_lock:
            try:
                data = self._load()
                return 'muted_at' in data.get(project_dir, {})
            except Exception as e:
                logger.error("[directory-store] Failed to check muted dir: %s", e)
                return False

    def list_muted_dirs(self) -> List[Dict[str, Any]]:
        """列出所有被静音的目录

        Returns:
            [{'project_dir': str, 'muted_at': int}, ...]
        """
        with self._file_lock:
            try:
                data = self._load()
                return sorted(
                    [{'project_dir': p, 'muted_at': info['muted_at']}
                     for p, info in data.items() if 'muted_at' in info],
                    key=lambda x: x['muted_at'],
                    reverse=True
                )
            except Exception as e:
                logger.error("[directory-store] Failed to list muted dirs: %s", e)
                return []

    # =========================================================================
    # 维护
    # =========================================================================

    def cleanup_expired(self) -> int:
        """清理过期使用历史和已不存在的目录（持久化）

        过期：超过 30 天未使用的 count + last_used。
        不存在：目录路径在磁盘上已不存在。
        两者均保留 muted_at，记录变为空 {} 时整条删除。

        Returns:
            清理的条目数量
        """
        with self._file_lock:
            try:
                data = self._load()
                before = len(data)
                data = self._filter_stale(data)
                removed = before - len(data)
                if removed > 0:
                    if not self._save(data):
                        return 0
                    logger.info("[directory-store] cleanup_expired: removed %d entries", removed)
                return removed
            except Exception as e:
                logger.error("[directory-store] Failed to cleanup expired: %s", e)
                return 0

    # =========================================================================
    # 内部方法
    # =========================================================================

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("[directory-store] Invalid JSON in %s, starting fresh",
                           self._file_path)
            return {}
        except IOError as e:
            logger.error("[directory-store] Failed to load %s: %s", self._file_path, e)
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix='.tmp')
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
            return True
        except (IOError, OSError) as e:
            logger.error("[directory-store] Failed to save %s: %s", self._file_path, e)
            return False

    def _migrate_legacy_file(self, data_dir: str) -> None:
        """将旧版 dir_history.json 迁移为 directories.json（一次性）"""
        legacy_path = os.path.join(data_dir, 'dir_history.json')
        if os.path.exists(legacy_path) and not os.path.exists(self._file_path):
            try:
                os.rename(legacy_path, self._file_path)
                logger.info("[directory-store] Migrated %s -> %s", legacy_path, self._file_path)
            except OSError as e:
                logger.warning("[directory-store] Failed to migrate legacy file: %s", e)

    def _filter_stale(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """过滤过期使用历史 + 不存在的目录（仅返回过滤后数据，不持久化）

        注意：原地修改传入的 data 并返回，不创建新 dict。

        过期：超过 30 天未使用 → 移除 count + last_used，保留 muted_at。
        不存在：目录路径在磁盘上不存在 → 移除 count + last_used，保留 muted_at。
        记录变为空 {} 时整条删除。
        """
        now = time.time()
        stale_paths = []
        for path, info in data.items():
            is_expired = info.get('last_used') and now - info['last_used'] > DIR_EXPIRE_SECONDS
            is_ghost = not os.path.isdir(path)
            if is_expired or is_ghost:
                stale_paths.append(path)

        for path in stale_paths:
            data[path].pop('count', None)
            data[path].pop('last_used', None)
            if not data[path]:
                del data[path]
        return data
