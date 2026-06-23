"""JsonStore 基类 —— JSON 文件持久化单例的统一抽象

封装各 store 逐字重复的部分：
    - per-subclass 单例（initialize / get_instance，_instance/_lock 每个子类独立）
    - __init__(data_dir)：路径、文件锁、目录创建、_post_init 钩子
    - _load / _save：委托 utils.atomic_json（含 isinstance 校验 + tmp 清理）

子类只需声明 STORE_NAME / LOG_TAG，并保留各自的业务方法。需要在加载后重建
内存索引或执行无条件迁移的子类，覆写 _post_init()。

STORE_NAME 为逻辑名，「.json」由基类拼接；换介质策略见 stores 包 docstring。

注意：
    - 保留 __init__(data_dir) 直接构造路径（测试直接实例化，不走单例）。
    - _instance/_lock 通过 __init_subclass__ 注入到每个子类，避免单例串台。
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

from utils.atomic_json import atomic_load_json, atomic_write_json

logger = logging.getLogger(__name__)


class JsonStore(object):
    """JSON 文件持久化单例基类

    子类示例：
        class MessageSessionStore(JsonStore):
            STORE_NAME = 'message_sessions'
            LOG_TAG = 'message-session-store'
            # 业务方法...
    """

    # 介质中立的逻辑名（如 'auth_token'）；文件实现拼成 <STORE_NAME>.json
    STORE_NAME: Optional[str] = None
    LOG_TAG = 'json-store'

    # 实例属性（在 __init__ 中创建，此处仅声明以提升可发现性，不赋值——
    # 赋值会变成所有实例/子类共享的类属性，_file_lock 会退化成全局锁）
    _data_dir: str
    _file_path: str
    _file_lock: threading.Lock

    def __init_subclass__(cls, **kwargs):
        # 每个子类获得独立的 _instance/_lock，避免多个子类共享基类同一 slot 导致串台
        super().__init_subclass__(**kwargs)
        cls._instance = None
        cls._lock = threading.Lock()

    def __init__(self, data_dir: str):
        if not self.STORE_NAME:
            raise ValueError('%s must define STORE_NAME' % type(self).__name__)
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, self.STORE_NAME + '.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        # 子类钩子：此时 _file_path/_file_lock 已就绪，可安全重建索引或执行迁移
        self._post_init()
        logger.info("[%s] Initialized with data_dir=%s", self.LOG_TAG, data_dir)

    def _post_init(self):
        """子类钩子：加载后重建内存索引 / 执行无条件迁移。默认空实现。"""
        pass

    @classmethod
    def initialize(cls, data_dir: str) -> 'JsonStore':
        """初始化单例实例"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['JsonStore']:
        """获取单例实例，未初始化返回 None

        用 cls.__dict__ 读取本类自己的 _instance，避免继承链上误返回兄弟类实例。
        """
        return cls.__dict__.get('_instance')

    def _load(self) -> Dict[str, Any]:
        return atomic_load_json(self._file_path, default={}, tag=self.LOG_TAG)

    def _save(self, data: Dict[str, Any]) -> bool:
        return atomic_write_json(self._file_path, data,
                                 data_dir=self._data_dir, tag=self.LOG_TAG)
