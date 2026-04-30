#!/usr/bin/env python3
"""
共享配置模块

提供统一的配置读取接口，支持从 .env 文件和环境变量读取配置

优先级: .env 文件 > 环境变量 > 默认值
"""

import json
import os
from typing import Optional, List, Tuple

# =============================================================================
# 默认配置值
# =============================================================================
DEFAULT_PERMISSION_REQUEST_TIMEOUT = 600  # 默认 10 分钟
CLIENT_TIMEOUT_BUFFER = 30                # 客户端额外缓冲 30 秒
DEFAULT_CALLBACK_PAGE_CLOSE_DELAY = 3     # 默认 3 秒
DEFAULT_SOCKET_PATH = '/tmp/claude-permission.sock'
DEFAULT_HTTP_PORT = '8080'

# =============================================================================
# .env 文件缓存
# =============================================================================
_env_file_cache: Optional[dict] = None


def _load_env_file() -> dict:
    """加载 .env 文件内容到缓存

    Returns:
        dict: key-value 配置字典
    """
    global _env_file_cache

    if _env_file_cache is not None:
        return _env_file_cache

    _env_file_cache = {}

    # 获取项目根目录 (src/server -> src -> project_root)
    server_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(server_dir)
    project_root = os.path.dirname(src_dir)
    env_path = os.path.join(project_root, '.env')

    if not os.path.exists(env_path):
        return _env_file_cache

    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # 跳过空行和注释
                if not line or line.startswith('#'):
                    continue
                # 解析 key=value
                if '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip()
                    # 去除引号
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    _env_file_cache[key] = value
    except Exception:
        pass

    return _env_file_cache


def get_config(key: str, default: str = '') -> str:
    """获取配置值

    优先级: .env 文件 > 环境变量 > 默认值

    Args:
        key: 配置项名称
        default: 默认值

    Returns:
        配置值
    """
    # 1. 优先从 .env 文件读取
    env_cache = _load_env_file()
    if key in env_cache and env_cache[key]:
        return env_cache[key]

    # 2. 其次从环境变量读取
    env_value = os.environ.get(key, '')
    if env_value:
        return env_value

    # 3. 使用默认值
    return default


def get_config_positive_int(key: str, default: int) -> int:
    """获取正整数配置值

    Args:
        key: 配置项名称
        default: 默认值

    Returns:
        配置值，无效或非正数时返回默认值
    """
    value = get_config(key, '')
    if value and value.isdigit():
        parsed = int(value)
        if parsed > 0:
            return parsed
    return default


def get_request_timeout() -> int:
    """获取请求超时时间

    配置项: PERMISSION_REQUEST_TIMEOUT（秒）
    - 必须为正整数
    - 0 或无效值会使用默认值

    Returns:
        超时秒数
    """
    return get_config_positive_int('PERMISSION_REQUEST_TIMEOUT', DEFAULT_PERMISSION_REQUEST_TIMEOUT)


def get_close_page_timeout() -> int:
    """获取回调页面自动关闭超时时间

    配置项: CALLBACK_PAGE_CLOSE_DELAY（秒）
    - 必须为正整数
    - 0 或无效值会使用默认值（3秒）
    - 建议范围：1-10 秒

    Returns:
        超时秒数
    """
    return get_config_positive_int('CALLBACK_PAGE_CLOSE_DELAY', DEFAULT_CALLBACK_PAGE_CLOSE_DELAY)


def get_claude_commands() -> List[str]:
    """解析 CLAUDE_COMMAND 配置为命令列表

    支持格式:
    - 单命令字符串: "claude" 或 "claude --model opus"
    - 无引号列表: [claude, claude --model opus]
    - JSON 数组: ["claude", "claude --model opus"]
    - 空值/缺失: 默认 ["claude"]

    Returns:
        命令字符串列表，至少包含一个元素
    """
    raw = get_config('CLAUDE_COMMAND', '')
    raw = raw.strip()

    if not raw:
        return ['claude']

    # 列表格式: 以 [ 开头且以 ] 结尾
    if raw.startswith('[') and raw.endswith(']'):
        # 先尝试 JSON 解析
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                result = [str(item).strip() for item in parsed if str(item).strip()]
                return result if result else ['claude']
        except (ValueError, TypeError):
            pass

        # JSON 失败，按逗号分隔（无引号列表格式）
        inner = raw[1:-1]
        items = [item.strip() for item in inner.split(',') if item.strip()]
        return items if items else ['claude']

    # 单命令字符串
    return [raw]


def get_session_mode() -> str:
    """获取会话模式

    优先级：
    1. FEISHU_SESSION_MODE 显式配置（message/thread/group）
    2. 向后兼容：FEISHU_REPLY_IN_THREAD=true → 'thread'
    3. 默认：'message'
    """
    mode = get_config('FEISHU_SESSION_MODE', '')
    if mode in ('message', 'thread', 'group'):
        return mode
    # 向后兼容：读取已废弃的 FEISHU_REPLY_IN_THREAD
    reply_in_thread = get_config('FEISHU_REPLY_IN_THREAD', 'false').lower() in ('true', '1', 'yes')
    if reply_in_thread:
        return 'thread'
    return 'message'


def reload_config():
    """重新加载 .env 文件

    当 .env 文件内容变化后调用此函数刷新缓存。
    注意：此函数仅刷新内部缓存，不会更新模块级导出变量
    （如 PERMISSION_REQUEST_TIMEOUT、FEISHU_APP_ID 等）。其他模块通过
    from config import XXX 获取的值仍为模块首次加载时的旧值。
    如需获取最新值，应直接调用 get_config()。
    """
    global _env_file_cache
    _env_file_cache = None
    _load_env_file()


# =============================================================================
# 导出的配置变量 (模块加载时计算)
# =============================================================================

# 服务端超时（用于清理断开连接）
PERMISSION_REQUEST_TIMEOUT = get_request_timeout()

# 客户端超时（比服务端大，确保服务端先触发超时）
CLIENT_TIMEOUT = PERMISSION_REQUEST_TIMEOUT + CLIENT_TIMEOUT_BUFFER

# 回调页面关闭超时
CALLBACK_PAGE_CLOSE_DELAY = get_close_page_timeout()

# VSCode URI 前缀（可选，用于从浏览器页面跳转到 VSCode）
# 示例: vscode://vscode-remote/ssh-remote+myserver 或 vscode://file
VSCODE_URI_PREFIX = get_config('VSCODE_URI_PREFIX', '')

# =============================================================================
# 飞书 OpenAPI 配置
# =============================================================================

# 飞书应用凭证
FEISHU_APP_ID = get_config('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = get_config('FEISHU_APP_SECRET', '')

# Verification Token (用于验证飞书事件请求)
FEISHU_VERIFICATION_TOKEN = get_config('FEISHU_VERIFICATION_TOKEN', '')

# 消息接收者不再通过配置指定，由客户端在请求中通过 owner_id 参数提供

# 发送模式: webhook / openapi
FEISHU_SEND_MODE = get_config('FEISHU_SEND_MODE', 'webhook')

# Callback 服务对外访问地址（用于网关注册）
CALLBACK_SERVER_URL = get_config('CALLBACK_SERVER_URL', '')

# 飞书用户 ID（用于网关注册）
# 必须使用 user_id 格式，以确保同一用户在更换应用后仍能正确认证
# 其他 ID 类型（open_id、union_id）在更换应用后会导致认证失败
FEISHU_OWNER_ID = get_config('FEISHU_OWNER_ID', '')
if FEISHU_OWNER_ID:
    # user_id 格式校验：不能是其他已知格式
    if (FEISHU_OWNER_ID.startswith('ou_') or  # open_id
        FEISHU_OWNER_ID.startswith('oc_') or  # chat_id
        FEISHU_OWNER_ID.startswith('on_') or  # union_id
        '@' in FEISHU_OWNER_ID):              # email
        raise ValueError(
            f"配置错误: FEISHU_OWNER_ID 必须使用 user_id 格式，"
            f"当前值: {FEISHU_OWNER_ID} 是其他 ID 类型。"
            f"请在飞书开放平台获取用户的 user_id（纯数字或字母数字组合）。"
        )

# 飞书网关地址（分离部署时配置）
# 支持 http(s):// 和 ws(s):// 两种格式：
#   - ws:// / wss:// → WS 隧道模式（callback 不需要公网可达）
#   - http:// / https:// → HTTP 回调模式（callback 需要公网可达）
# FEISHU_GATEWAY_URL 统一为 HTTP base URL，供 API 调用使用
_FEISHU_GATEWAY_URL_RAW = get_config('FEISHU_GATEWAY_URL', '')

if _FEISHU_GATEWAY_URL_RAW:
    # 有配置 FEISHU_GATEWAY_URL
    _host_part = _FEISHU_GATEWAY_URL_RAW.split('://', 1)[1].split('/')[0]
    if _FEISHU_GATEWAY_URL_RAW.startswith(('ws://', 'wss://')):
        FEISHU_GATEWAY_MODE = 'ws'
        _scheme = 'https' if _FEISHU_GATEWAY_URL_RAW.startswith('wss://') else 'http'
        FEISHU_GATEWAY_URL = f'{_scheme}://{_host_part}'
    else:
        FEISHU_GATEWAY_MODE = 'http'
        FEISHU_GATEWAY_URL = _FEISHU_GATEWAY_URL_RAW.rstrip('/')
elif FEISHU_SEND_MODE == 'openapi':
    # 单机模式：默认使用 WS 隧道连接本地网关（架构统一，与分离部署行为一致）
    FEISHU_GATEWAY_MODE = 'ws'
    FEISHU_GATEWAY_URL = CALLBACK_SERVER_URL
else:
    FEISHU_GATEWAY_MODE = ''
    FEISHU_GATEWAY_URL = ''

# =============================================================================
# OpenAPI 模式下的服务模式判断与冲突检测
# =============================================================================
IS_CALLBACK_BACKEND = False  # 默认值，webhook 模式或未配置

if FEISHU_SEND_MODE == 'openapi':
    # 冲突检测：APP 凭据和网关地址不能同时配置
    if _FEISHU_GATEWAY_URL_RAW and FEISHU_APP_ID and FEISHU_APP_SECRET:
        raise ValueError(
            "配置冲突: FEISHU_APP_ID/FEISHU_APP_SECRET 与 FEISHU_GATEWAY_URL 不能同时配置。\n"
            "  - 单机部署：只需配置 FEISHU_APP_ID + FEISHU_APP_SECRET\n"
            "  - 分离部署：只需配置 FEISHU_GATEWAY_URL（凭据由网关管理）\n"
            "请移除其中一组配置。"
        )

    # 是否为分离部署的 Callback 后端（显式配置了 FEISHU_GATEWAY_URL）
    # - 单机部署（False）：同时充当网关 + Callback 后端，网关地址自动指向本地
    # - 分离部署（True）：纯 Callback 后端，连接远程网关
    IS_CALLBACK_BACKEND = bool(_FEISHU_GATEWAY_URL_RAW)

# 飞书事件接收模式: auto / http / longpoll
# - auto: 自动检测（默认）- 有 lark-oapi 则 longpoll，否则 http
# - http: 传统 HTTP 回调模式（需要公网端点）
# - longpoll: WebSocket 长连接模式（网关主动连接飞书，无需公网端点）
# 注: 一般无需配置，auto 自动选择，不在 .env.example 中暴露
FEISHU_EVENT_MODE = get_config('FEISHU_EVENT_MODE', 'auto')

# 默认聊天目录（配置后普通消息自动创建/继续会话）
# 支持 ~ 开头的路径，自动展开为用户主目录
DEFAULT_CHAT_DIR = os.path.expanduser(get_config('DEFAULT_CHAT_DIR', ''))

# 默认聊天目录话题跟随模式
# True (默认): 跟随 FEISHU_REPLY_IN_THREAD 全局配置
# False: 默认聊天目录的回复始终在主界面显示（不收敛进话题）
DEFAULT_CHAT_FOLLOW_THREAD = get_config('DEFAULT_CHAT_FOLLOW_THREAD', 'true').lower() in ('true', '1', 'yes')

# 群聊 @bot 过滤：群聊中是否仅响应 @bot 的消息
# False (默认): 处理群聊中的所有消息
# True: 群聊中只处理 @bot 的消息，其他消息静默忽略
# 注意：P2P 单聊不受此配置影响，始终响应
FEISHU_AT_BOT_ONLY = get_config('FEISHU_AT_BOT_ONLY', 'false').lower() in ('true', '1', 'yes')

# 话题内回复模式：回复消息时是否收进话题详情（不刷群聊主界面）
# True: 回复消息仅出现在话题详情中，不会冒泡到群聊主界面
# False (默认): 回复消息正常显示在群聊主界面
FEISHU_REPLY_IN_THREAD = get_config('FEISHU_REPLY_IN_THREAD', 'false').lower() in ('true', '1', 'yes')

# 会话模式：message（普通消息）/ thread（话题回复）/ group（独立群聊）
FEISHU_SESSION_MODE = get_session_mode()

# 群聊模式配置
FEISHU_GROUP_NAME_PREFIX = get_config('FEISHU_GROUP_NAME_PREFIX', 'Claude')
FEISHU_GROUP_DISSOLVE_DAYS = get_config_positive_int('FEISHU_GROUP_DISSOLVE_DAYS', 0)

# Session 过期天数（统一，不区分 group/非 group）
SESSION_EXPIRE_DAYS = get_config_positive_int('SESSION_EXPIRE_DAYS', 30)
