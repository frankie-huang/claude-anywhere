"""Session-Chat 映射存储

归属端: Callback 后端
使用方: callback.py, agent.py
对外接口:
    - /cb/session/get-chat-id / get-last-message-id / set-last-message-id
    - /cb/session/ensure-chat（group 模式懒创建群聊）
    - /cb/session/get-info（按 session_id 返回权威字段，含 dissolved 状态）
    - /cb/session/mute
    - /cb/session/invalidate-chats（gateway 解散群后调用，标记所有引用该 chat_id 的记录为 dissolved 状态）

维护 session_id → session 语义数据（chat_id、command、dissolved、muted_at、活跃时间等）。
群聊层数据（chat_id ↔ session_id 反向索引、owner、seq、生命周期）由飞书网关侧
GroupChatStore + GroupSessionStore 独立承担；本 store 只负责 session 自身属性。

dissolved 标记：独立布尔字段，默认不存在。
    - 群解散时 gateway 调 /cb/session/invalidate-chats，本 store 设置 dissolved=True
    - get_session 过滤 dissolved 返回 None（软失效），上层落入"session 不存在"分支：
      ensure-chat 走重建、continue 报错引导 /new
    - attach 通过 save(session_id, new_chat_id) 自动复活（非空 chat_id 清除 dissolved）
    - get_session(include_dissolved=True) 可读取 dissolved session（继承属性、校验存在性等只读场景）
    - 不刷新 updated_at（dissolve 不是活跃信号，保留原值让过期机制正常回收）

过期策略：统一 SESSION_EXPIRE_DAYS（默认 30 天），不区分 group/非 group。
gateway 转发 /cb/agent/continue 时 callback 校验 session 是否存在，
已过期则返回错误，gateway 告知用户 /new。
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)


class SessionChatStore(JsonStore):
    """管理 session_id -> session 语义数据的存储（归属端: Callback 后端）

    数据结构:

        {
            "session_id": {
                "chat_id": "oc_xxx",               # 飞书群聊 ID；空表示需要 ensure-chat 重建
                "agent_type": "claude",            # agent 类型: 'claude' / 'codex'（旧数据可能缺失，默认视为 claude）
                "command": "claude",               # 使用的命令（可选）
                "last_message_id": "om_xxx",       # 链式回复锚点（可选）
                "skip_next_user_prompt": true,     # 跳过下一条 UserPromptSubmit（飞书发起时设置，可选）
                "updated_at": 1706745600,          # 最近更新时间戳
                "project_dir": "/path/to/project", # 项目目录（可选）
                "muted_at": 1706745600,            # 出站静音时间戳（可选）
                "dissolved": true,                 # 群已解散标志（可选）
                "env_overrides": {                 # hook 快照的白名单 env（可选，续聊注入，值可为空串）
                    "ANTHROPIC_BASE_URL": "https://x.com",
                    "NO_PROXY": ""
                },
                "running_pid": 12345,              # 当前运行中的 Agent 进程 PID（可选，0/缺失 = 空闲）
                "pending_prompts": [               # 排队中的指令（可选，容量由上游控制，默认 5）
                    {"session_id": "...", "project_dir": "...", "prompt": "...",
                     "chat_id": "...", "message_id": "...", "command": "..."}
                ],
                "stopped": true                    # /stop 触发标志（可选，抑制错误通知）
            }
        }

    """

    STORE_NAME = 'session_chats'
    LOG_TAG = 'session-chat-store'

    # 默认过期时间（秒），由 config.SESSION_EXPIRE_DAYS 覆盖
    _expire_seconds: int = 30 * 24 * 3600

    @classmethod
    def initialize(cls, data_dir: str, expire_seconds: Optional[int] = None) -> 'SessionChatStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
                cls._instance.cleanup_runtime_state()
            if expire_seconds is not None:
                cls._instance._expire_seconds = expire_seconds
            return cls._instance

    def backfill_agent_type(self, default: str = 'claude') -> int:
        """为缺少 agent_type 的旧 session 记录补写默认值

        启动时调用一次，处理从旧版本升级的历史数据。

        Returns:
            补写的记录数
        """
        with self._file_lock:
            try:
                data = self._load()
                count = 0
                for entry in data.values():
                    if isinstance(entry, dict) and not entry.get('agent_type'):
                        entry['agent_type'] = default
                        count += 1
                if count > 0:
                    self._save(data)
                    logger.info("[session-chat-store] Backfilled agent_type='%s' for %d sessions",
                                default, count)
                return count
            except Exception as e:
                logger.error("[session-chat-store] Failed to backfill agent_type: %s", e)
                return 0

    def migrate_claude_command(self) -> int:
        """将旧字段 claude_command 迁移为 command

        启动时调用一次，处理从旧版本升级的历史数据。

        Returns:
            迁移的记录数
        """
        with self._file_lock:
            try:
                data = self._load()
                count = 0
                for entry in data.values():
                    if isinstance(entry, dict) and 'claude_command' in entry:
                        entry['command'] = entry.pop('claude_command')
                        count += 1
                if count > 0:
                    self._save(data)
                    logger.info("[session-chat-store] Migrated claude_command -> command for %d sessions",
                                count)
                return count
            except Exception as e:
                logger.error("[session-chat-store] Failed to migrate claude_command: %s", e)
                return 0

    def cleanup_runtime_state(self) -> int:
        """清理进程运行时状态（running_pid / pending_prompts / stopped）

        启动时调用一次。服务重启后这些字段已失效：
        - running_pid 指向的进程已不存在
        - pending_prompts 中的指令无人 drain，会阻塞新消息入队
        - stopped 残留会导致下次正常完成时误吞通知

        Returns:
            清理的记录数
        """
        with self._file_lock:
            try:
                data = self._load()
                count = 0
                for entry in data.values():
                    if not isinstance(entry, dict):
                        continue
                    changed = False
                    for key in ('running_pid', 'pending_prompts', 'stopped'):
                        if key in entry:
                            del entry[key]
                            changed = True
                    if changed:
                        count += 1
                if count > 0:
                    self._save(data)
                    logger.info("[session-chat-store] Cleaned runtime state from %d sessions",
                                count)
                return count
            except Exception as e:
                logger.error("[session-chat-store] Failed to cleanup runtime state: %s", e)
                return 0

    # =========================================================================
    # 写
    # =========================================================================

    def save(self, session_id: str, chat_id: str,
             project_dir: str = '', agent_type: str = '',
             command: str = '') -> bool:
        """保存 session 属性（merge 方式，不传的字段保留旧值）

        Args:
            session_id: 会话 ID
            chat_id: 飞书群聊 ID；空串视为不覆盖旧值，非空时自动清除 dissolved 标记（复活）
            project_dir: 项目目录（空串视为不覆盖）
            agent_type: agent 类型标识（'claude'/'codex'，空串视为不覆盖）
            command: 命令字符串（空串视为不覆盖）

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
                if command:
                    entry['command'] = command
                # agent_type：传了才写，空串不动（旧 session 缺失时在读取侧 fallback 到默认值）
                if agent_type:
                    entry['agent_type'] = agent_type
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

    def rename_session(self, old_id: str, new_id: str) -> bool:
        """重命名 session ID（将 old_id 的数据迁移到 new_id）

        用于 Codex 路径：临时 ID 替换为从输出捕获的真实 session ID。
        如果 new_id 已存在，将 old_id 的数据合并到 new_id（old_id 字段补全 new_id 缺失值）。

        Args:
            old_id: 旧 session ID
            new_id: 新 session ID

        Returns:
            是否重命名成功
        """
        with self._file_lock:
            try:
                data = self._load()
                if old_id not in data:
                    logger.warning("[session-chat-store] rename: old_id %s not found", old_id)
                    return False
                if new_id in data:
                    old_entry = data.pop(old_id)
                    new_entry = dict(data[new_id])
                    for key, value in old_entry.items():
                        if key not in new_entry or new_entry.get(key) is None:
                            new_entry[key] = value
                    new_entry['updated_at'] = int(time.time())
                    data[new_id] = new_entry
                    result = self._save(data)
                    if result:
                        logger.info("[session-chat-store] Merged session rename: %s -> %s",
                                    old_id, new_id)
                    return result
                data[new_id] = data.pop(old_id)
                result = self._save(data)
                if result:
                    logger.info("[session-chat-store] Renamed session: %s -> %s", old_id, new_id)
                return result
            except Exception as e:
                logger.error("[session-chat-store] Failed to rename session: %s", e)
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

    def set_env_overrides(self, session_id: str, env: Dict[str, str]) -> bool:
        """记录该 session 启动时的白名单 env 快照

        续聊时由 AgentAdapter 取出，以 K=V 前缀注入续聊命令，覆盖登录 shell
        中 .zshenv/.bashrc 全局 export 的同名变量。

        每次 hook 触发都会调用（幂等覆盖）。不会为不存在的 session 创建
        占位记录——否则 do_ensure_chat 误判为"session 存在"跳过建群。

        Args:
            session_id: 会话 ID
            env: 已过白名单过滤的 env 字典

        Returns:
            是否成功写入；session 不存在时返回 False
        """
        if not session_id or not isinstance(env, dict):
            return False
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False
                filtered = {str(k): str(v) for k, v in env.items() if k}
                existing = item.get('env_overrides') or {}
                if filtered == existing:
                    return True
                if not filtered:
                    item.pop('env_overrides', None)
                else:
                    item['env_overrides'] = filtered
                item['updated_at'] = int(time.time())
                data[session_id] = item
                if not self._save(data):
                    return False
                logger.info("[session-chat-store] Set env_overrides: session=%s, keys=%s",
                            session_id, sorted(filtered.keys()))
                return True
            except Exception as e:
                logger.error("[session-chat-store] Failed to set env_overrides: %s", e)
                return False

    def get_env_overrides(self, session_id: str) -> Dict[str, str]:
        """读取 session 的 env_overrides 快照

        Returns:
            env 字典；不存在/过期/dissolved 返回空 dict
        """
        item = self.get_session(session_id)
        if not item:
            return {}
        env = item.get('env_overrides') or {}
        if not isinstance(env, dict):
            return {}
        return env

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
            session_id: 会话 ID

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
                if item.get('muted_at'):
                    return False
                item['muted_at'] = int(time.time())
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
                if not item.get('muted_at'):
                    return False
                del item['muted_at']
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

        过滤 expired（过期记录视为不存在），不过滤 dissolved：
        dissolved 表示群聊层失效，muted 表示用户业务层意图（是否拦截出站）。
        dissolved 的 session 若仍被 mute，出站依然按用户意图拦截——
        避免 dissolve 后消息漏到单聊。
        """
        if not session_id:
            return False
        # include_dissolved=True：dissolved 不影响 mute 意图
        item = self.get_session(session_id, include_dissolved=True)
        if not item:
            return False
        return bool(item.get('muted_at'))

    def list_muted_sessions(self) -> List[Dict[str, Any]]:
        """列出所有处于静音状态的 session

        Returns:
            [{'session_id': str, 'project_dir': str, 'chat_id': str, 'muted_at': int}, ...]
            不过滤 expired（cleanup_expired 保留有 muted_at 的记录，用户可通过列表手动解除）。
            包含 dissolved（dissolved 不影响 mute 状态）。
        """
        with self._file_lock:
            try:
                data = self._load()
                result = []
                for session_id, item in data.items():
                    if not item.get('muted_at'):
                        continue
                    result.append({
                        'session_id': session_id,
                        'project_dir': item.get('project_dir', ''),
                        'chat_id': item.get('chat_id', ''),
                        'muted_at': item.get('muted_at', 0),
                    })
                return sorted(result, key=lambda x: x['muted_at'], reverse=True)
            except Exception as e:
                logger.error("[session-chat-store] Failed to list muted sessions: %s", e)
                return []

    # =========================================================================
    # 读
    # =========================================================================

    def get_session(self, session_id: str,
                    include_dissolved: bool = False) -> Optional[Dict[str, Any]]:
        """获取 session 完整数据

        不存在 / 过期均返回 None，不主动删除（清理由 cleanup_expired 统一处理）。
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
                    return None
                return dict(item)
            except Exception as e:
                logger.error("[session-chat-store] Failed to get_session: %s", e)
                return None

    def get_active_chat_id(self, session_id: str) -> str:
        """获取可用的发送目标 chat_id

        过滤 expired + dissolved：过期或已解散群的 chat_id 不可用。
        不存在/过期/dissolved/chat_id 为空 均返回空字符串。
        """
        item = self.get_session(session_id)
        return item.get('chat_id', '') if item else ''

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

        超过 SESSION_EXPIRE_DAYS 且无 muted_at 的 session 删除。
        有 muted_at 的记录保留——静音是用户主动意图，过期不应绕过静音。

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
                    and not item.get('muted_at')
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
    # 进程 PID 管理
    # =========================================================================

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """检查进程组是否存活

        Popen 用 start_new_session=True，子进程为新进程组 leader（PID == PGID），
        故用 os.killpg 检测整组而非单一 wrapper shell——否则 wrapper 崩溃但
        agent 子进程仍在跑时会误判为空闲。

        NOTE: 无法防 PID 复用，但 Agent 进程生命周期通常远长于 PID 回绕窗口，
        且误判仅导致 is_session_busy 多等一轮，实际无影响。
        """
        try:
            os.killpg(pid, 0)
            return True
        except OSError:
            return False

    def _resolve_running_pid(self, data: dict, session_id: str) -> int:
        """在已持有 _file_lock 的上下文中检查 PID 存活，死进程自动清除

        调用方必须持有 _file_lock 且已 _load() 过 data。
        """
        item = data.get(session_id)
        if not item:
            return 0
        pid = item.get('running_pid', 0)
        if not pid:
            return 0
        if self._is_pid_alive(pid):
            return pid
        item.pop('running_pid', None)
        data[session_id] = item
        self._save(data)
        logger.info("[session-chat-store] Auto-cleared dead PID %d: %s",
                    pid, session_id)
        return 0

    def set_running_pid(self, session_id: str, pid: int) -> bool:
        """记录 session 当前运行的 Agent 进程 PID"""
        if not session_id or not pid:
            return False
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False
                item['running_pid'] = pid
                item.pop('stopped', None)
                data[session_id] = item
                return self._save(data)
            except Exception as e:
                logger.error("[session-chat-store] Failed to set_running_pid: %s", e)
                return False

    def clear_running_pid(self, session_id: str) -> bool:
        """清除运行 PID（进程完成时调用）"""
        if not session_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False
                if 'running_pid' not in item:
                    return True
                del item['running_pid']
                data[session_id] = item
                return self._save(data)
            except Exception as e:
                logger.error("[session-chat-store] Failed to clear_running_pid: %s", e)
                return False

    def get_running_pid(self, session_id: str) -> int:
        """获取运行 PID，若进程已死则自动清除并返回 0"""
        if not session_id:
            return 0
        with self._file_lock:
            try:
                data = self._load()
                return self._resolve_running_pid(data, session_id)
            except Exception as e:
                logger.error("[session-chat-store] Failed to get_running_pid: %s", e)
                return 0

    def is_session_busy(self, session_id: str) -> bool:
        """判断 session 是否有存活的 Agent 进程"""
        return self.get_running_pid(session_id) > 0

    # =========================================================================
    # 指令队列
    # =========================================================================

    def get_session_queue_status(self, session_id: str) -> Tuple[bool, bool]:
        """一次读盘返回 (busy, has_pending)，减少临界区 I/O

        Returns:
            (busy, has_pending):
                busy — 是否有存活的 Agent 进程（死进程自动清除）
                has_pending — 是否有排队中的指令
        """
        if not session_id:
            return False, False
        with self._file_lock:
            try:
                data = self._load()
                pid = self._resolve_running_pid(data, session_id)
                item = data.get(session_id)
                has_pending = bool(item.get('pending_prompts')) if item else False
                return pid > 0, has_pending
            except Exception as e:
                logger.error("[session-chat-store] Failed to get_session_queue_status: %s", e)
                return False, False

    def enqueue_prompt(self, session_id: str, item_data: dict,
                       max_size: int = 5) -> Tuple[bool, int]:
        """追加排队指令

        Args:
            session_id: 会话 ID
            item_data: handle_continue_session 的原始请求数据
            max_size: 队列最大容量

        Returns:
            (成功, 队列位置)。队列满时返回 (False, -1)
        """
        if not session_id:
            return False, -1
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False, -1
                queue = item.get('pending_prompts') or []
                if len(queue) >= max_size:
                    return False, -1
                queue.append(item_data)
                item['pending_prompts'] = queue
                data[session_id] = item
                if not self._save(data):
                    return False, -1
                position = len(queue)
                logger.info("[session-chat-store] Enqueued prompt at position %d: %s",
                            position, session_id)
                return True, position
            except Exception as e:
                logger.error("[session-chat-store] Failed to enqueue_prompt: %s", e)
                return False, -1

    def dequeue_prompt(self, session_id: str) -> Optional[Dict[str, Any]]:
        """弹出队首指令，无可用指令返回 None"""
        if not session_id:
            return None
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return None
                queue = item.get('pending_prompts') or []
                if not queue:
                    return None
                entry = queue.pop(0)
                item['pending_prompts'] = queue
                data[session_id] = item
                self._save(data)
                logger.info("[session-chat-store] Dequeued prompt: %s (remaining: %d)",
                            session_id, len(queue))
                return entry
            except Exception as e:
                logger.error("[session-chat-store] Failed to dequeue_prompt: %s", e)
                return None

    def clear_pending_prompts(self, session_id: str) -> int:
        """清空排队指令，返回被清除的数量"""
        if not session_id:
            return 0
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return 0
                queue = item.get('pending_prompts') or []
                count = len(queue)
                if count == 0:
                    return 0
                item['pending_prompts'] = []
                data[session_id] = item
                self._save(data)
                logger.info("[session-chat-store] Cleared %d pending prompts: %s",
                            count, session_id)
                return count
            except Exception as e:
                logger.error("[session-chat-store] Failed to clear_pending_prompts: %s", e)
                return 0

    # =========================================================================
    # Stop 标志
    # =========================================================================

    def set_stopped_flag(self, session_id: str) -> bool:
        """设置 stopped 标志（/stop 触发时调用，抑制监控线程的错误通知）"""
        if not session_id:
            return False
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False
                item['stopped'] = True
                data[session_id] = item
                return self._save(data)
            except Exception as e:
                logger.error("[session-chat-store] Failed to set_stopped_flag: %s", e)
                return False

    def check_and_clear_stopped_flag(self, session_id: str) -> bool:
        """原子检查并清除 stopped 标志

        Returns:
            True = 标志存在且已清除（/stop 触发的终止），False = 标志不存在
        """
        with self._file_lock:
            try:
                data = self._load()
                item = data.get(session_id)
                if not item:
                    return False
                if not item.get('stopped'):
                    return False
                del item['stopped']
                data[session_id] = item
                self._save(data)
                logger.info("[session-chat-store] Cleared stopped flag: %s", session_id)
                return True
            except Exception as e:
                logger.error("[session-chat-store] Failed to check stopped flag: %s", e)
                return False

