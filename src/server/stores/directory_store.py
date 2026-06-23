"""目录相关存储

归属端: Callback 后端
使用方: callback.py, claude.py

管理工作目录的使用历史和静音状态：
  - 使用历史：记录目录使用频率，用于创建新会话时提供常用目录推荐
  - 静音状态：记录被 mute 的目录，终端发起的会话自动继承 mute 状态

飞书网关不应直接调用此 Store，应通过 Callback 后端的 HTTP 接口间接访问。
"""

import os
import time
import logging
from typing import Optional, List, Dict, Any, Tuple

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)

# 使用历史过期时间
DIR_EXPIRE_SECONDS = 30 * 24 * 3600  # 30 天过期

# 决策字段名
_F_MUTED = 'muted_at'
_F_UNMUTED = 'unmuted_at'
_F_RECURSIVE = 'recursive_at'
_DECISION_FIELDS = (_F_MUTED, _F_UNMUTED, _F_RECURSIVE)

# 静音状态标识（list_muted_dirs 返回的 status 字段取值）
# 没有 unmuted_children 状态，因为子孙默认就是不静音的，无需显式声明。
S_MUTED             = 'muted'              # {M}   仅自身静音
S_MUTED_RECURSIVE   = 'muted_recursive'    # {M,R} 自身+子孙静音
S_MUTED_CHILDREN    = 'muted_children'     # {R}   仅子孙静音
S_UNMUTED           = 'unmuted'            # {U}   仅自身加白
S_UNMUTED_RECURSIVE = 'unmuted_recursive'  # {U,R} 自身+子孙加白

# 递归后缀
RECURSIVE_SUFFIX = '/**'


class DirectoryStore(JsonStore):
    """管理工作目录使用历史和静音状态

    单文件存储 (directories.json)，每条记录的字段独立存在：
    {
        "/path/to/project": {
            "count": 5,              // 使用历史（可选）
            "last_used": 1706745600, // 使用历史（可选）
            "muted_at": 1706745600,  // 自身静音（可选）
            "unmuted_at": ...,       // 自身加白（可选，与 muted_at 互斥）
            "recursive_at": ...      // 子孙跟随当前方向（可选）
        }
    }

    过期清理只移除 count + last_used，保留决策字段（muted_at/unmuted_at/recursive_at）；
    记录变为空 {} 时才整条删除。

    ┌─────────────────────────────────────────────────────────────────┐
    │                      静音数据模型                                │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  决策字段（3 个，加上 count/last_used 使用历史）：                │
    │    muted_at (M)      自身静音（时间戳）                          │
    │    unmuted_at (U)     自身加白（时间戳，与 M 互斥）               │
    │    recursive_at (R)   当前方向覆盖子孙（时间戳）                  │
    │                                                                 │
    │  R 的方向由上下文决定：                                          │
    │    有 M 或无 M/U → R 表示子孙静音                                │
    │    有 U           → R 表示子孙加白                               │
    │                                                                 │
    │  M 与 R 的对称性：                                               │
    │    M = 自身静音、子孙不管                                        │
    │    R = 自身不管、子孙静音                                        │
    │                                                                 │
    │  6 种有效状态：                                                  │
    │    状态      自身    子孙    含义                                 │
    │    {}        默认    默认    无决策                               │
    │    {M}       静音    默认    仅自身静音                           │
    │    {R}       默认    静音    仅子孙静音                           │
    │    {M,R}     静音    静音    递归静音                             │
    │    {U}       加白    默认    仅自身加白                           │
    │    {U,R}     加白    加白    递归加白                             │
    │                                                                 │
    ├─────────────────────────────────────────────────────────────────┤
    │                      匹配算法                                   │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  从目标目录向上遍历（含自身），遇到第一个显式决策即返回：          │
    │    自身维度：M/U 只在本目录生效                                   │
    │    子孙维度：只有 R 对后代生效（有 U 则加白，否则静音）           │
    │                                                                 │
    ├─────────────────────────────────────────────────────────────────┤
    │                    完整状态转移表                                 │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  /mute /p（自身静音）:                                           │
    │    #   起始     结果     规则                                     │
    │    1   {}       {M}      新增                                    │
    │    2   {M}      {M}      幂等                                    │
    │    3   {R}      {M,R}    同向，补 M                              │
    │    4   {M,R}    {M,R}    幂等                                    │
    │    5   {U}      {M}      反向→写入（清除达不到静音目的）          │
    │    6   {U,R}    {M}      更窄反向→清 U+R（翻转保护），写 M       │
    │                                                                 │
    │  /mute /p/**（递归静音）:                                        │
    │    #   起始     结果     规则                                     │
    │    7   {}       {M,R}    新增                                    │
    │    8   {M}      {M,R}    同向，升级                              │
    │    9   {R}      {M,R}    同向，升级                              │
    │    10  {M,R}    {M,R}    幂等                                    │
    │    11  {U}      {M,R}    更甚反向→写入意图                       │
    │    12  {U,R}    {M,R}    全匹配反向但清除达不到静音目的→写入意图  │
    │                                                                 │
    │  /unmute /p（自身加白）:                                         │
    │    #   起始     结果     规则                                     │
    │    13  {}       {U}      新增                                    │
    │    14  {M}      {}       全匹配反向→清除（默认即不静音）          │
    │    15  {R}      {R}      幂等（R 蕴含自身不静音）                 │
    │    16  {M,R}    {R}      更窄→清 M，保留 R                       │
    │    17  {U}      {U}      幂等                                    │
    │    18  {U,R}    {U,R}    幂等                                    │
    │                                                                 │
    │  /unmute /p/**（递归加白）:                                      │
    │    #   起始     结果     规则                                     │
    │    19  {}       {U,R}    新增                                    │
    │    20  {M}      {U,R}    更甚反向→写入意图                       │
    │    21  {R}      {U,R}    更甚反向→写入意图                       │
    │    22  {M,R}    {}       全匹配反向→清除（默认即不静音）          │
    │    23  {U}      {U,R}    同向，升级                              │
    │    24  {U,R}    {U,R}    幂等                                    │
    │                                                                 │
    ├─────────────────────────────────────────────────────────────────┤
    │                     用户提示文案                                  │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  飞书展示格式：目录 {display} {message}                          │
    │  display = `/path`（精确）或 `/path/**`（递归）                  │
    │                                                                 │
    │  /mute /p:                                                      │
    │    #1  已静音，从终端发起的新会话将自动静音。                      │
    │    #2  已处于静音状态。                                           │
    │    #3  已递归静音（含所有子目录），从终端发起的新会话将自动静音。   │
    │    #4  已处于静音状态。                                           │
    │    #5  已静音…… +（原加白规则已覆盖。如需清除……）              │
    │    #6  同 #5                                                     │
    │                                                                 │
    │  /mute /p/**:                                                   │
    │    #7  已递归静音，从终端发起的新会话将自动静音。                  │
    │    #8  同 #7                                                     │
    │    #9  同 #7                                                     │
    │    #10 已处于递归静音状态。                                       │
    │    #11 已递归静音…… +（原加白规则已覆盖。如需清除……）          │
    │    #12 同 #11                                                    │
    │                                                                 │
    │  /unmute /p:                                                    │
    │    #13 已标记为不静音。                                            │
    │    #14 已清除静音规则。                                            │
    │    #15 自身已处于非静音状态。                                     │
    │    #16 已清除自身静音（子目录递归静音保留）。                      │
    │    #17 已处于不静音状态。                                          │
    │    #18 同 #17                                                    │
    │                                                                 │
    │  /unmute /p/**:                                                 │
    │    #19 已标记为不静音（含所有子目录）。                            │
    │    #20 同 #19                                                    │
    │    #21 同 #19                                                    │
    │    #22 已清除递归静音规则。                                        │
    │    #23 同 #19                                                    │
    │    #24 已处于递归不静音状态。                                      │
    │                                                                 │
    ├─────────────────────────────────────────────────────────────────┤
    │                  各状态回到 {} 的路径                             │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │    状态    最短路径                          步数                 │
    │    {M}     /unmute /p                        1                   │
    │    {M,R}   /unmute /p/**                     1                   │
    │    {U}     /mute /p → /unmute /p             2                   │
    │    {R}     /mute /p → /unmute /p/**          2                   │
    │    {U,R}   /mute /p → /unmute /p             2                   │
    │                                                                 │
    ├─────────────────────────────────────────────────────────────────┤
    │                    转移核心原则                                   │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  - /mute 反向永远写入（默认不是静音，清除达不到目的）             │
    │  - /unmute 全匹配反向清除（默认就是不静音，清除即满足）           │
    │  - /unmute 更甚反向写入意图（用户要求比撤销更多）                 │
    │  - 更窄反向只清对应部分，保留其余维度                             │
    │  - 清 U 时若 R 存在，连带清 R（翻转保护：R 默认含义是静音）      │
    │  - 清 M 时保留 R（R 含义不变，无需保护）                         │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘
    """

    STORE_NAME = 'directories'
    LOG_TAG = 'directory-store'

    def _post_init(self):
        self._migrate_legacy_file()  # 旧数据迁移，待旧版本再无流量后可删去

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

    def mute_dir(self, project_dir: str, recursive: bool = False) -> Optional[Dict[str, Any]]:
        """标记目录为静音

        mute_dir 和 unmute_dir 均不校验目录是否存在，允许对已删除或尚未创建
        的路径操作（清除规则 / 预防性加白）。

        局限：对不存在的路径，realpath 无法解析符号链接，若该路径将来创建为
        符号链接，存储的 key 与实际解析路径会不一致。

        Args:
            project_dir: 目标目录（符号链接会自动解析为真实路径，允许尚不存在）
            recursive: True 表示递归静音（自身+子孙），False 表示仅自身

        Returns:
            {'changed': bool, 'message': str} 或 None（失败）
        """
        if not project_dir:
            return None
        project_dir = os.path.realpath(project_dir)
        with self._file_lock:
            try:
                data = self._load()
                entry = data.get(project_dir, {})
                new_entry, msg = self._apply_mute(entry, recursive)
                if new_entry == entry:
                    return {'changed': False, 'message': msg}
                self._set_entry(data, project_dir, new_entry)
                if not self._save(data):
                    return None
                tag = RECURSIVE_SUFFIX if recursive else ''
                logger.info("[directory-store] Muted dir: %s%s", project_dir, tag)
                return {'changed': True, 'message': msg}
            except Exception as e:
                logger.error("[directory-store] Failed to mute dir: %s", e)
                return None

    def unmute_dir(self, project_dir: str, recursive: bool = False) -> Optional[Dict[str, Any]]:
        """取消目录静音 / 加白目录

        路径存在性策略及局限同 mute_dir。

        Args:
            project_dir: 目标目录（符号链接会自动解析为真实路径，允许尚不存在）
            recursive: True 表示递归加白（自身+子孙），False 表示仅自身

        Returns:
            {'changed': bool, 'message': str} 或 None（失败）
        """
        if not project_dir:
            return None
        project_dir = os.path.realpath(project_dir)
        with self._file_lock:
            try:
                data = self._load()
                entry = data.get(project_dir, {})
                new_entry, msg = self._apply_unmute(entry, recursive)
                if new_entry == entry:
                    return {'changed': False, 'message': msg}
                self._set_entry(data, project_dir, new_entry)
                if not self._save(data):
                    return None
                tag = RECURSIVE_SUFFIX if recursive else ''
                logger.info("[directory-store] Unmuted dir: %s%s", project_dir, tag)
                return {'changed': True, 'message': msg}
            except Exception as e:
                logger.error("[directory-store] Failed to unmute dir: %s", e)
                return None

    def is_dir_muted(self, project_dir: str) -> bool:
        """检查目录是否被静音（walk-up 匹配，含递归规则）

        从目标目录向上遍历至根目录，遇到第一个显式决策即返回。
        """
        if not project_dir:
            return False
        project_dir = os.path.realpath(project_dir)
        with self._file_lock:
            try:
                data = self._load()
                return self._walk_up_muted(data, project_dir)
            except Exception as e:
                logger.error("[directory-store] Failed to check muted dir: %s", e)
                return False

    def list_muted_dirs(self) -> List[Dict[str, Any]]:
        """列出所有含决策字段的目录

        Returns:
            [{'project_dir': str, 'muted_at': int, 'unmuted_at': int,
              'recursive_at': int, 'status': str}, ...]
            status 取值：
              'muted'            — {M}    仅自身静音
              'muted_recursive'  — {M,R}  自身+子孙静音
              'muted_children'   — {R}    仅子孙静音（自身不受影响）
              'unmuted'          — {U}    仅自身加白
              'unmuted_recursive'— {U,R}  自身+子孙加白
        """
        with self._file_lock:
            try:
                data = self._load()
                result = []
                for p, info in data.items():
                    if not self._has_mute_decision(info):
                        continue
                    item = {'project_dir': p, 'status': self._describe_mute_status(info)}
                    for f in _DECISION_FIELDS:
                        if f in info:
                            item[f] = info[f]
                    result.append(item)
                return sorted(result, key=lambda x: x['project_dir'])
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
        两者均保留决策字段（muted_at/unmuted_at/recursive_at），记录变为空 {} 时整条删除。

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
    # 内部：静音逻辑（状态转移、匹配算法、决策查询）
    # =========================================================================

    @staticmethod
    def _apply_mute(entry: Dict[str, Any], recursive: bool) -> Tuple[Dict[str, Any], str]:
        """计算 /mute 操作后的新 entry 和反馈文案（不修改原 dict）。

        /mute /p:
          同向（M 或 M+R 或 R 已存在）→ 幂等或补 M
          反向（有 U）→ 清 U（+R 翻转保护），写 M

        /mute /p/**:
          同向 → 幂等或升级（补 M 和/或 R）
          反向 → 写入 {M, R}（清除达不到静音目的，直接写入意图）

        Returns: (new_entry, message)
        """
        out = dict(entry)
        now = int(time.time())
        has_M = _F_MUTED in out
        has_U = _F_UNMUTED in out
        has_R = _F_RECURSIVE in out

        if recursive:
            # /mute /p/** → 目标 {M, R}
            if has_M and has_R:
                return entry, "已处于递归静音状态。"  # 幂等 (#10)
            # 同向升级 (#8, #9) 或反向写入 (#11, #12)
            out.pop(_F_UNMUTED, None)
            if not has_M:
                out[_F_MUTED] = now
            if not has_R:
                out[_F_RECURSIVE] = now
            msg = "已递归静音，从终端发起的新会话将自动静音。"
            if has_U:
                msg += "\n（原加白规则已覆盖。如需清除该规则，请执行 /unmute <path>/**）"
            return out, msg
        else:
            # /mute /p → 目标 {M}（自身静音）
            if has_M:
                return entry, "已处于静音状态。"  # 幂等 (#2, #4)
            if has_U:
                # 反向：清 U，翻转保护清 R (#5, #6)
                out.pop(_F_UNMUTED, None)
                out.pop(_F_RECURSIVE, None)
            # 同向补 M (#1, #3) 或反向写 M (#5, #6)
            out[_F_MUTED] = now
            if _F_RECURSIVE in out:  # 不能用 has_R：has_U 分支可能已 pop R
                # #3: {R} → {M,R}，补上自身后实际变成递归静音
                return out, "已递归静音（含所有子目录），从终端发起的新会话将自动静音。"
            msg = "已静音，从终端发起的新会话将自动静音。"
            if has_U:
                msg += "\n（原加白规则已覆盖。如需清除该规则，请执行 /unmute <path>）"
            return out, msg

    @staticmethod
    def _apply_unmute(entry: Dict[str, Any], recursive: bool) -> Tuple[Dict[str, Any], str]:
        """计算 /unmute 操作后的新 entry 和反馈文案（不修改原 dict）。

        /unmute /p:
          全匹配反向（{M}）→ 清除回 {}
          更窄（{M,R}）→ 清 M 保留 R → {R}
          已满足（无 M，含 R/U/UR）→ 幂等
          无记录 {} → 写 {U}

        /unmute /p/**:
          全匹配反向（{M,R}）→ 清除回 {}
          更甚反向（{M} / {R}）→ 写入 {U, R}
          同向升级（{U}）→ {U, R}
          已满足（{U,R}）→ 幂等
          无记录 {} → 写 {U, R}

        Returns: (new_entry, message)
        """
        out = dict(entry)
        now = int(time.time())
        has_M = _F_MUTED in out
        has_U = _F_UNMUTED in out
        has_R = _F_RECURSIVE in out

        if recursive:
            # /unmute /p/** → 目标"自身+子孙不静音"
            if has_U and has_R:
                return entry, "已处于递归不静音状态。"  # 幂等 (#24)
            if has_M and has_R:
                # 全匹配反向 → 清除 (#22)
                out.pop(_F_MUTED, None)
                out.pop(_F_RECURSIVE, None)
                return out, "已清除递归静音规则。"
            # 更甚反向 (#20, #21) / 同向升级 (#23) / 新增 (#19)
            out.pop(_F_MUTED, None)
            if not has_U:
                out[_F_UNMUTED] = now
            if not has_R:
                out[_F_RECURSIVE] = now
            return out, "已标记为不静音（含所有子目录）。"
        else:
            # /unmute /p → 目标"自身不静音"
            if has_U:
                return entry, "已处于不静音状态。"  # 幂等 (#17, #18)
            if has_R and not has_M:
                return entry, "自身已处于非静音状态。"  # 幂等：R 蕴含自身不静音 (#15)
            if has_M:
                # 反向：清 M，保留 R (#14 → {}, #16 → {R})
                out.pop(_F_MUTED, None)
                if has_R:
                    return out, "已清除自身静音（子目录递归静音保留）。"
                return out, "已清除静音规则。"
            # 无记录 {} → 新增 {U} (#13)
            out[_F_UNMUTED] = now
            return out, "已标记为不静音。"

    @staticmethod
    def _walk_up_muted(data: Dict[str, Any], target: str) -> bool:
        """从 target 向上遍历至根，遇到第一个显式决策即返回。"""
        current = target
        is_self = True
        while True:
            entry = data.get(current, {})
            has_M = _F_MUTED in entry
            has_U = _F_UNMUTED in entry
            has_R = _F_RECURSIVE in entry

            if is_self:
                # 自身维度：M/U 直接生效
                if has_M:
                    return True
                if has_U:
                    return False
                # {R} 或 {} 对自身无决策，继续向上
            else:
                # 子孙维度：只有 R 对后代生效
                if has_R:
                    return not has_U  # U+R → False（递归加白），M+R/R → True（递归静音）

            parent = os.path.dirname(current)
            if parent == current:
                break  # 已到根目录
            current = parent
            is_self = False

        return False

    @staticmethod
    def _has_mute_decision(entry: Dict[str, Any]) -> bool:
        """条目是否含有任何决策字段。"""
        return any(f in entry for f in _DECISION_FIELDS)

    @staticmethod
    def _describe_mute_status(entry: Dict[str, Any]) -> str:
        """根据决策字段生成人可读的状态描述。"""
        has_M = _F_MUTED in entry
        has_U = _F_UNMUTED in entry
        has_R = _F_RECURSIVE in entry
        if has_M and has_R:
            return S_MUTED_RECURSIVE
        if has_M:
            return S_MUTED
        if has_R and not has_U:
            return S_MUTED_CHILDREN
        if has_U and has_R:
            return S_UNMUTED_RECURSIVE
        if has_U:
            return S_UNMUTED
        return 'unknown'

    # =========================================================================
    # 内部：持久化（读写、条目操作、迁移、清理）
    # =========================================================================

    @staticmethod
    def _set_entry(data: Dict[str, Any], path: str, entry: Dict[str, Any]) -> None:
        """设置或删除条目（空条目自动删除）。"""
        if entry:
            data[path] = entry
        elif path in data:
            del data[path]

    def _migrate_legacy_file(self) -> None:
        """将旧版 dir_history.json 迁移为 directories.json（一次性）"""
        legacy_path = os.path.join(self._data_dir, 'dir_history.json')
        if os.path.exists(legacy_path) and not os.path.exists(self._file_path):
            try:
                os.rename(legacy_path, self._file_path)
                logger.info("[directory-store] Migrated %s -> %s", legacy_path, self._file_path)
            except OSError as e:
                logger.warning("[directory-store] Failed to migrate legacy file: %s", e)

    def _filter_stale(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """过滤过期使用历史 + 不存在的目录（仅返回过滤后数据，不持久化）

        注意：原地修改传入的 data 并返回，不创建新 dict。

        过期：超过 30 天未使用 → 移除 count + last_used，保留决策字段。
        不存在：目录路径在磁盘上不存在 → 移除 count + last_used，保留决策字段。
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
