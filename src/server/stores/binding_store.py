"""BindingStore - owner_id 到 callback_url 的绑定存储

归属端: 飞书网关
使用方: feishu.py, register.py（网关内部逻辑），callback.py 中的网关侧路由（/gw/feishu/send）

维护飞书用户 ID 到 Callback 后端 URL 的映射关系，用于网关注册和双向认证。
Callback 后端的路由和逻辑不应直接调用此 Store。
"""

import os
import time
import logging
from typing import Optional, Dict, Any

from stores.json_store import JsonStore

logger = logging.getLogger(__name__)


class BindingStore(JsonStore):
    """管理 owner_id -> callback_url + auth_token 的绑定

    绑定结构:
    {
        "ou_xxx": {
            "callback_url": "https://callback.example.com",
            "auth_token": "abc123.def456",
            "reply_in_thread": true,
            "session_mode": "message",
            "default_agent": "claude",
            "claude_commands": ["claude", "claude --model opus"],
            "codex_commands": ["codex"],
            "default_chat_dir": "/home/user/project",
            "default_chat_follow_thread": true,
            "default_chat_session_id": "uuid-xxx",
            "group_name_prefix": "Agent",
            "group_dissolve_days": 7,
            "updated_at": 1706745600,
            "registered_ip": "1.2.3.4"
        }
    }

    注意: get() 方法返回的绑定信息会自动注入 _owner_id 字段。
    """

    STORE_NAME = 'bindings'
    LOG_TAG = 'binding-store'

    def get(self, owner_id: str) -> Optional[Dict[str, Any]]:
        """获取绑定信息

        Args:
            owner_id: 飞书用户 ID

        Returns:
            绑定信息字典（自动注入 _owner_id 字段），不存在返回 None
        """
        with self._file_lock:
            try:
                data = self._load()
                binding = data.get(owner_id)
                if binding:
                    result = dict(binding)
                    result['_owner_id'] = owner_id
                    return result
                return None
            except Exception as e:
                logger.error(f"[binding-store] Failed to get binding: {e}")
                return None

    def upsert(
        self,
        owner_id: str,
        callback_url: str,
        auth_token: str,
        registered_ip: str = '',
        binding_params: Optional[Dict[str, Any]] = None
    ) -> bool:
        """创建或更新绑定

        Args:
            owner_id: 飞书用户 ID
            callback_url: Callback 后端 URL
            auth_token: 认证令牌
            registered_ip: 注册来源 IP
            binding_params: per-user 配置参数 dict，字段说明：
                session_mode: 会话模式 message/thread/group（默认 message）
                default_agent: 默认 agent 标识，如 'claude'/'codex'（非空才写入）
                claude_commands: 可用的 Claude 命令列表（非空才写入）
                codex_commands: 可用的 Codex 命令列表（非空才写入）
                default_chat_dir: 默认聊天目录（非空写入，变更时清除关联 session_id）
                default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
                group_name_prefix: 群聊名称前缀（None=保留旧值；含 '' 显式写入）
                group_dissolve_days: 群聊自动解散天数（None=保留旧值；含 0 显式写入）
                group_allow_cowork: 群聊协作者模式（None=保留旧值；True/False 显式写入）

        Returns:
            是否保存成功
        """
        if binding_params is None:
            binding_params = {}
        # 从 binding_params 中提取各字段
        session_mode = binding_params.get('session_mode', 'message')
        default_agent = binding_params.get('default_agent', '')
        claude_commands = binding_params.get('claude_commands')
        codex_commands = binding_params.get('codex_commands')
        default_chat_dir = binding_params.get('default_chat_dir', '')
        default_chat_follow_thread = binding_params.get('default_chat_follow_thread', True)
        group_name_prefix = binding_params.get('group_name_prefix')
        group_dissolve_days = binding_params.get('group_dissolve_days')
        group_allow_cowork = binding_params.get('group_allow_cowork')
        with self._file_lock:
            try:
                data = self._load()
                existing = data.get(owner_id)
                # 清除其他用户对同一 callback_url 的旧绑定
                # WS 隧道模式下 callback_url 是共享占位符（ws://tunnel），不能清理
                is_ws = callback_url.startswith(('ws://', 'wss://'))
                stale_owners = [
                    oid for oid, info in data.items()
                    if oid != owner_id and info.get('callback_url') == callback_url
                ] if not is_ws else []
                for oid in stale_owners:
                    del data[oid]
                    logger.info(
                        f"[binding-store] Removed stale binding: {oid} -> {callback_url}"
                    )
                # 处理 claude_commands：过滤空字符串
                valid_claude_commands = [c for c in (claude_commands or []) if c and c.strip()]
                # 处理 codex_commands：过滤空字符串
                valid_codex_commands = [c for c in (codex_commands or []) if c and c.strip()]
                # 校验 session_mode（入口处已做 reply_in_thread → session_mode 转换，
                # 此处为防御性校验，防止未来新调用方传入非法值）
                if session_mode not in ('message', 'thread', 'group'):
                    session_mode = 'message'
                binding_data = {
                    'callback_url': callback_url,
                    'auth_token': auth_token,
                    'reply_in_thread': (session_mode == 'thread'),  # 兼容旧版读取
                    'session_mode': session_mode,
                    'default_chat_follow_thread': default_chat_follow_thread,
                    'updated_at': int(time.time()),
                    'registered_ip': registered_ip
                }
                # default_agent：非空才写入
                if default_agent:
                    binding_data['default_agent'] = default_agent
                # claude_commands：非空才写入
                if valid_claude_commands:
                    binding_data['claude_commands'] = valid_claude_commands
                # codex_commands：非空才写入
                if valid_codex_commands:
                    binding_data['codex_commands'] = valid_codex_commands
                # 群聊配置：None = 调用方未传，保留旧值；其他值（含 ''、0）显式写入
                if group_name_prefix is not None:
                    binding_data['group_name_prefix'] = group_name_prefix
                elif existing and 'group_name_prefix' in existing:
                    binding_data['group_name_prefix'] = existing['group_name_prefix']
                if group_dissolve_days is not None:
                    try:
                        group_dissolve_days = int(group_dissolve_days)
                    except (TypeError, ValueError):
                        group_dissolve_days = 0
                    binding_data['group_dissolve_days'] = group_dissolve_days
                elif existing and 'group_dissolve_days' in existing:
                    binding_data['group_dissolve_days'] = existing['group_dissolve_days']
                # group_allow_cowork：None = 调用方未传，保留旧值；True/False 显式写入
                if group_allow_cowork is not None:
                    binding_data['group_allow_cowork'] = bool(group_allow_cowork)
                elif existing and 'group_allow_cowork' in existing:
                    binding_data['group_allow_cowork'] = existing['group_allow_cowork']
                # 处理 default_chat_dir 及关联的 default_chat_session_id：
                # - 传入非空值且与旧值相同：保留两者
                # - 传入非空值且与旧值不同：更新目录，清除旧 session_id（已失效）
                # - 传入空值：清除两者
                # - callback_url 改变时（更换设备），即使目录相同也清除 session_id
                if default_chat_dir:
                    binding_data['default_chat_dir'] = default_chat_dir
                    old_dir = existing.get('default_chat_dir', '') if existing else ''
                    old_callback = existing.get('callback_url', '') if existing else ''
                    # 注意：old_dir 为空时，os.path.realpath('') 会返回 cwd，导致错误比较
                    # 更换设备（callback_url 改变）时，session_id 已失效，需要清除
                    # WS 模式：callback_url 都是 ws://tunnel，同值即视为同设备；
                    #   无法精确区分不同机器，但不用 registered_ip 判断，
                    #   因为同一台机器换网络环境 IP 就会变。
                    #   目录相同就保留 session_id，换机器但路径碰巧相同时，
                    #   callback 端发现 session 无效应该自行重建。
                    # 协议变化（HTTP↔WS）视为换设备，清除 session_id。
                    callback_unchanged = old_callback == callback_url
                    if (old_dir and os.path.realpath(default_chat_dir) == os.path.realpath(old_dir)
                            and existing and 'default_chat_session_id' in existing
                            and callback_unchanged):
                        binding_data['default_chat_session_id'] = existing['default_chat_session_id']
                data[owner_id] = binding_data
                result = self._save(data)
                if result:
                    if existing:
                        logger.info(
                            f"[binding-store] Updated binding: {owner_id} -> {callback_url}"
                        )
                    else:
                        logger.info(
                            f"[binding-store] Created binding: {owner_id} -> {callback_url}"
                        )
                return result
            except Exception as e:
                logger.error(f"[binding-store] Failed to upsert binding: {e}")
                return False

    def update_field(self, owner_id: str, field: str, value: Any) -> bool:
        """更新绑定中的单个字段

        仅在绑定已存在时更新，不会创建新绑定。

        Args:
            owner_id: 飞书用户 ID
            field: 字段名
            value: 字段值

        Returns:
            是否更新成功
        """
        with self._file_lock:
            try:
                data = self._load()
                if owner_id not in data:
                    logger.warning(f"[binding-store] Cannot update field '{field}': binding not found for {owner_id}")
                    return False
                data[owner_id][field] = value
                return self._save(data)
            except Exception as e:
                logger.error(f"[binding-store] Failed to update field '{field}': {e}")
                return False

    def delete(self, owner_id: str) -> bool:
        """删除绑定

        Args:
            owner_id: 飞书用户 ID

        Returns:
            是否删除成功
        """
        with self._file_lock:
            try:
                data = self._load()
                if owner_id not in data:
                    return True
                del data[owner_id]
                result = self._save(data)
                if result:
                    logger.info(f"[binding-store] Deleted binding: {owner_id}")
                return result
            except Exception as e:
                logger.error(f"[binding-store] Failed to delete binding: {e}")
                return False

    def get_all(self) -> Dict[str, Any]:
        """获取所有绑定信息（用于管理员查看）

        Returns:
            所有绑定数据字典的副本
        """
        with self._file_lock:
            try:
                return dict(self._load())
            except Exception as e:
                logger.error(f"[binding-store] Failed to get all bindings: {e}")
                return {}
