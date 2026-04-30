"""BindingStore - owner_id 到 callback_url 的绑定存储

归属端: 飞书网关
使用方: feishu.py, register.py（网关内部逻辑），callback.py 中的网关侧路由（/gw/feishu/send）

维护飞书用户 ID 到 Callback 后端 URL 的映射关系，用于网关注册和双向认证。
Callback 后端的路由和逻辑不应直接调用此 Store。
"""

import json
import os
import tempfile
import threading
import time
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class BindingStore:
    """管理 owner_id -> callback_url + auth_token 的绑定

    绑定结构:
    {
        "ou_xxx": {
            "callback_url": "https://callback.example.com",
            "auth_token": "abc123.def456",
            "reply_in_thread": true,
            "at_bot_only": false,
            "session_mode": "message",
            "claude_commands": ["claude", "claude --model opus"],
            "default_chat_dir": "/home/user/project",
            "default_chat_follow_thread": true,
            "default_chat_session_id": "uuid-xxx",
            "group_name_prefix": "Claude",
            "group_dissolve_days": 7,
            "updated_at": 1706745600,
            "registered_ip": "1.2.3.4"
        }
    }

    注意: get() 方法返回的绑定信息会自动注入 _owner_id 字段。
    """

    _instance = None  # type: Optional[BindingStore]
    _lock = threading.Lock()

    def __init__(self, data_dir: str):
        """初始化 BindingStore

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'bindings.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"[binding-store] Initialized with data_dir={data_dir}")

    @classmethod
    def initialize(cls, data_dir: str) -> 'BindingStore':
        """初始化单例实例

        Args:
            data_dir: 数据存储目录

        Returns:
            BindingStore 实例
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['BindingStore']:
        """获取单例实例

        Returns:
            BindingStore 实例，未初始化返回 None
        """
        return cls._instance

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
        at_bot_only: Optional[bool] = None,
        session_mode: str = 'message',
        claude_commands: Optional[List[str]] = None,
        default_chat_dir: str = '',
        default_chat_follow_thread: bool = True,
        group_name_prefix: Optional[str] = None,
        group_dissolve_days: Optional[int] = None
    ) -> bool:
        """创建或更新绑定

        Args:
            owner_id: 飞书用户 ID
            callback_url: Callback 后端 URL
            auth_token: 认证令牌
            registered_ip: 注册来源 IP
            at_bot_only: 群聊 @bot 过滤（None = 未传，保留旧值；True/False 显式写入）
            session_mode: 会话模式，message/thread/group
            claude_commands: 可用的 Claude 命令列表（从 Callback 后端传递）
            default_chat_dir: 默认聊天目录（从 Callback 后端传递）
            default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
            group_name_prefix: 群聊名称前缀（None = 未传，保留旧值；显式值含 '' 原样写入）
            group_dissolve_days: 群聊自动解散天数（None = 未传，保留旧值；显式值含 0 原样写入）

        Returns:
            是否保存成功
        """
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
                # 处理 claude_commands：过滤空字符串，为空时默认 ["claude"]
                valid_commands = [c for c in (claude_commands or []) if c and c.strip()]
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
                    'claude_commands': valid_commands or ['claude'],
                    'updated_at': int(time.time()),
                    'registered_ip': registered_ip
                }
                # at_bot_only：None = 调用方未传，保留旧值；True/False 显式写入
                if at_bot_only is not None:
                    binding_data['at_bot_only'] = bool(at_bot_only)
                elif existing and 'at_bot_only' in existing:
                    binding_data['at_bot_only'] = existing['at_bot_only']
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

    def _load(self) -> Dict[str, Any]:
        """加载绑定数据

        Returns:
            绑定数据字典
        """
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"[binding-store] Invalid JSON in {self._file_path}, starting fresh")
            return {}
        except IOError as e:
            logger.error(f"[binding-store] Failed to load: {e}")
            return {}

    def _save(self, data: Dict[str, Any]) -> bool:
        """保存绑定数据（原子写入）

        Args:
            data: 绑定数据字典

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
            logger.error(f"[binding-store] Failed to save: {e}")
            return False
