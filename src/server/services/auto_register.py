"""Auto Register - Callback 后端启动时自动向飞书网关注册

Callback 后端使用，启动时自动向飞书网关注册获取 auth_token。

OpenAPI 模式下：
  - 分离部署（配置 FEISHU_GATEWAY_URL=http(s)://）：HTTP 回调模式，向远端网关注册
  - 分离部署（配置 FEISHU_GATEWAY_URL=ws(s)://）：WS 隧道模式，无需 HTTP 注册
  - 单机部署（未配置 FEISHU_GATEWAY_URL）：使用 WS 隧道连接本地网关

需配置：CALLBACK_SERVER_URL、FEISHU_OWNER_ID
注册接口：{FEISHU_GATEWAY_URL}/gw/register
"""

import json
import logging
import threading
import urllib.request
import urllib.error
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)

# HTTP 请求超时（秒）
HTTP_TIMEOUT = 10


class AutoRegister:
    """自动注册服务（单例模式）

    Callback 后端启动时自动向飞书网关注册获取 auth_token。
    """

    _instance = None  # type: Optional[AutoRegister]
    _lock = threading.Lock()

    def __init__(self, callback_url: str, owner_id: str, gateway_url: str):
        """初始化自动注册服务

        Args:
            callback_url: Callback 后端 URL
            owner_id: 飞书用户 ID
            gateway_url: 飞书网关地址
        """
        self._callback_url = callback_url
        self._owner_id = owner_id
        self._gateway_url = gateway_url

        # 启用条件：三个参数都存在
        self._enabled = bool(callback_url and owner_id and gateway_url)

        if self._enabled:
            logger.info(
                f"[auto-register] Initialized: owner_id={owner_id}, "
                f"callback_url={callback_url}, gateway={gateway_url}"
            )
        else:
            logger.warning("[auto-register] Disabled (missing configuration)")

    @classmethod
    def initialize(cls, callback_url: str, owner_id: str, gateway_url: str) -> 'AutoRegister':
        """初始化单例实例

        Args:
            callback_url: Callback 后端 URL
            owner_id: 飞书用户 ID
            gateway_url: 飞书网关地址

        Returns:
            AutoRegister 实例
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(callback_url, owner_id, gateway_url)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['AutoRegister']:
        """获取单例实例

        Returns:
            AutoRegister 实例，未初始化返回 None
        """
        return cls._instance

    @property
    def enabled(self) -> bool:
        """服务是否启用"""
        return self._enabled

    def register_in_background(self):
        """在后台线程中执行注册"""
        if not self._enabled:
            logger.warning("[auto-register] Cannot register: service not enabled")
            return

        thread = threading.Thread(target=self._register, daemon=True)
        thread.start()

    def _register(self):
        """执行注册"""
        from config import (
            FEISHU_REPLY_IN_THREAD, FEISHU_AT_BOT_ONLY,
            FEISHU_SESSION_MODE, DEFAULT_CHAT_DIR,
            DEFAULT_CHAT_FOLLOW_THREAD, FEISHU_GROUP_NAME_PREFIX,
            FEISHU_GROUP_DISSOLVE_DAYS, get_claude_commands
        )

        logger.info(
            f"[auto-register] Starting registration in background: "
            f"owner_id={self._owner_id}, callback_url={self._callback_url}, gateway={self._gateway_url}"
        )

        register_url = self._gateway_url.rstrip('/') + '/gw/register'
        success, message = self._do_register(
            self._callback_url,
            self._owner_id,
            register_url,
            reply_in_thread=FEISHU_REPLY_IN_THREAD,
            at_bot_only=FEISHU_AT_BOT_ONLY,
            session_mode=FEISHU_SESSION_MODE,
            claude_commands=get_claude_commands(),
            default_chat_dir=DEFAULT_CHAT_DIR,
            default_chat_follow_thread=DEFAULT_CHAT_FOLLOW_THREAD,
            group_name_prefix=FEISHU_GROUP_NAME_PREFIX,
            group_dissolve_days=FEISHU_GROUP_DISSOLVE_DAYS
        )

        if success:
            logger.info(f"[auto-register] Registration successful: {message}")
        else:
            logger.warning(f"[auto-register] Registration failed: {message}")

    def _do_register(
        self,
        callback_url: str,
        owner_id: str,
        register_url: str,
        reply_in_thread: bool = False,
        at_bot_only: Optional[bool] = None,
        session_mode: str = '',
        claude_commands: Optional[List[str]] = None,
        default_chat_dir: str = '',
        default_chat_follow_thread: bool = True,
        group_name_prefix: Optional[str] = None,
        group_dissolve_days: Optional[int] = None
    ) -> Tuple[bool, str]:
        """向飞书网关注册

        Args:
            callback_url: Callback 后端 URL
            owner_id: 飞书用户 ID
            register_url: 飞书网关注册接口完整 URL（已包含 /gw/register）
            reply_in_thread: 是否使用回复话题模式
            at_bot_only: 群聊 @bot 过滤
            claude_commands: 可用的 Claude 命令列表
            default_chat_dir: 默认聊天目录
            default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
            group_name_prefix: 群聊名称前缀
            group_dissolve_days: 群聊自动解散天数

        Returns:
            (success, message): success 表示是否成功，message 是响应消息或错误信息
        """
        api_url = register_url.rstrip('/')

        request_data = {
            'callback_url': callback_url,
            'owner_id': owner_id,
            'reply_in_thread': reply_in_thread,
            'at_bot_only': at_bot_only,
            'session_mode': session_mode
        }
        # 添加 claude_commands（如果提供）
        if claude_commands:
            request_data['claude_commands'] = claude_commands
        # 添加 default_chat_dir（如果配置）
        if default_chat_dir:
            request_data['default_chat_dir'] = default_chat_dir
        # 添加 default_chat_follow_thread
        request_data['default_chat_follow_thread'] = default_chat_follow_thread
        request_data['group_name_prefix'] = group_name_prefix
        request_data['group_dissolve_days'] = group_dissolve_days

        logger.info(f"[auto-register] Registering to gateway: {api_url}")

        try:
            req = urllib.request.Request(
                api_url,
                data=json.dumps(request_data).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )

            # 创建无代理的 opener
            no_proxy_handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(no_proxy_handler)
            with opener.open(req, timeout=HTTP_TIMEOUT) as response:
                response_data = json.loads(response.read().decode('utf-8'))
                if response_data.get('success'):
                    logger.info(f"[auto-register] Registration accepted")
                    return True, response_data.get('message', 'Accepted')
                else:
                    error = response_data.get('error', 'Unknown error')
                    logger.warning(f"[auto-register] Registration failed: {error}")
                    return False, error

        except urllib.error.HTTPError as e:
            error_msg = f"HTTP {e.code}: {e.reason}"
            logger.error(f"[auto-register] HTTP error: {error_msg}")
            return False, error_msg
        except urllib.error.URLError as e:
            error_msg = f"URL error: {e.reason}"
            logger.error(f"[auto-register] URL error: {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[auto-register] Error: {error_msg}")
            return False, error_msg
