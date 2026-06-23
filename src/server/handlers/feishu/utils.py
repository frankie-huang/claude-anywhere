"""
Feishu Utils - 飞书处理器通用工具

纯叶子模块，不 import 包内其他模块。提供：
- 常量（Toast 类型、正则、白名单）
- 全局状态（群成员缓存、消息日志器）
- 内容脱敏 / 路径截断
- binding 查找、operator 验证
- agent 命令解析
"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

# setup_logging 由 main.py 启动时将 shared/ 加入 sys.path
from logging_config import setup_logging
from utils.ttl_cache import TTLCache

logger = logging.getLogger(__name__)

# =========================================================================
# 常量
# =========================================================================

# 飞书 Toast 类型常量
TOAST_SUCCESS = 'success'
TOAST_WARNING = 'warning'
TOAST_ERROR = 'error'
TOAST_INFO = 'info'

# 协作者允许执行的命令白名单（其余命令仅 owner 可执行）
_COLLABORATOR_ALLOWED_COMMANDS = {'clear'}

# 会话路由失败时的通用反馈文案（/mute /unmute / 主路由等场景共用）
_SESSION_NOT_FOUND_HINT = "无法找到对应的会话（可能已过期、被清理或服务暂时不可用）。"
_SESSION_UNRESOLVED_HINT = "无法确定目标会话，请在群聊中或回复某条会话消息时使用。"

# =========================================================================
# 全局状态
# =========================================================================

# 飞书消息事件日志（独立文件）
_feishu_message_logger = None
_feishu_message_logger_lock = threading.Lock()

# 群成员数缓存：用于多人群 @bot 过滤判断（TTL 60 秒）
_group_member_cache = TTLCache(ttl=60, max_size=500, name='group_member')


# =========================================================================
# 内容处理
# =========================================================================

def _sanitize_user_content(content: str, max_len: int = 20) -> str:
    """脱敏用户生成内容

    Args:
        content: 原始内容
        max_len: 保留的最大长度

    Returns:
        脱敏后的内容，格式为 "前N个字符..." (总长度: X)
    """
    if not content:
        return ''
    preview = content[:max_len].replace('\n', '\\n')
    return f"{preview}... (len={len(content)})"


def _truncate_path(path: str, max_len: int = 40) -> str:
    """截断文件路径（从后往前截断，保留重要部分）

    Args:
        path: 文件路径
        max_len: 最大长度

    Returns:
        截断后的路径，如 ".../project/dir" (len=50)，未截断则返回原路径
    """
    if not path:
        return ''
    if len(path) <= max_len:
        return path
    # 保留后 max_len 个字符，前面加 ...
    return f"...{path[-(max_len - 3):]} (len={len(path)})"


def _extract_http_error_detail(http_error):
    """从 HTTPError 中提取错误详情

    Args:
        http_error: urllib.error.HTTPError 实例

    Returns:
        错误详情字符串，无法解析返回空字符串
    """
    try:
        error_body = http_error.read().decode('utf-8')
        error_data = json.loads(error_body)
        return error_data.get('error', '')
    except Exception:
        return ''


# =========================================================================
# 日志
# =========================================================================

def _log_message_event(data: Dict[str, Any], text: str) -> None:
    """将入站消息事件记录到独立的飞书消息日志（脱敏用户内容）

    懒加载日志器（线程安全，双重检查），记录事件全量字段 + 解析出的纯文本，
    供审计/调试使用。日志字段从 data 中提取，text 为上游解析后的纯文本。
    """
    global _feishu_message_logger
    if _feishu_message_logger is None:
        with _feishu_message_logger_lock:
            if _feishu_message_logger is None:  # 双重检查
                _feishu_message_logger = setup_logging(
                    'feishu_message', console=False, propagate=False, encoding='utf-8'
                )
                logger.info("Feishu message logging to: %s (daily rotating)",
                            _feishu_message_logger.handlers[0].baseFilename)

    header = data.get('header', {})
    event = data.get('event', {})
    message = event.get('message', {})
    sender_id_obj = event.get('sender', {}).get('sender_id', {})
    content = message.get('content', '{}')

    _feishu_message_logger.info(json.dumps({
        'event_id': header.get('event_id', ''),
        'message_id': message.get('message_id', ''),
        'parent_id': message.get('parent_id', ''),
        'chat_id': message.get('chat_id', ''),
        'chat_type': message.get('chat_type', ''),
        'message_type': message.get('message_type', ''),
        'sender_id': sender_id_obj.get('open_id', ''),
        'content': _sanitize_user_content(content),
        'text': _sanitize_user_content(text),
        'raw_data': data  # 记录完整的原始数据
    }, ensure_ascii=False))


# =========================================================================
# 群聊 / 话题判断
# =========================================================================

def _is_multi_person_group(chat_id: str) -> bool:
    """判断群聊是否为多人群（成员数 > 2）

    用于 @bot 过滤：单人群（owner + bot）不需要 @bot，多人群需要 @bot。
    内部缓存 user_count 60 秒（_group_member_cache），减少 API 调用。
    API 失败时安全降级为 True（要求 @bot），不缓存。
    """
    cached = _group_member_cache.get(chat_id)
    if cached is None:
        from services.feishu_api import FeishuAPIService
        service = FeishuAPIService.get_instance()
        success, data = service.get_chat_info(chat_id)
        if not success:
            logger.warning("[feishu] Failed to get chat info for %s, defaulting to multi-person", chat_id)
            return True
        try:
            cached = int(data.get('user_count', '0'))
        except (ValueError, TypeError):
            logger.warning("[feishu] Invalid user_count for %s, defaulting to multi-person", chat_id)
            return True
        _group_member_cache.put(chat_id, cached)
        logger.debug("[feishu] Group %s user_count=%d", chat_id, cached)

    # user_count 不含 bot；单人群 = 1 个用户 + bot，多人群 = 2+ 个用户
    return cached > 1


def _should_reply_in_thread(binding: Dict[str, Any], project_dir: str) -> bool:
    """判断是否应该回复到话题

    当工作目录为该用户的默认聊天目录且未开启话题跟随时，不回复到话题。

    Args:
        binding: 绑定信息
        project_dir: 项目工作目录

    Returns:
        是否回复到话题
    """
    # 优先使用 session_mode 判断
    session_mode = binding.get('session_mode', '')
    if session_mode in ('message', 'thread', 'group'):
        # session_mode 明确设置：thread 模式回复话题，其他模式不回复
        if session_mode == 'thread':
            # 仍需检查 default_chat_dir 覆盖逻辑
            default_chat_dir = binding.get('default_chat_dir', '')
            if project_dir and default_chat_dir and os.path.realpath(project_dir) == os.path.realpath(default_chat_dir):
                if not binding.get('default_chat_follow_thread', True):
                    return False
            return True
        return False

    # 向后兼容：没有 session_mode 时使用 reply_in_thread 判断
    default_chat_dir = binding.get('default_chat_dir', '')
    if project_dir and default_chat_dir and os.path.realpath(project_dir) == os.path.realpath(default_chat_dir):
        # DEFAULT_CHAT_FOLLOW_THREAD=false 时，默认聊天目录的回复强制在主界面显示
        # DEFAULT_CHAT_FOLLOW_THREAD=true（默认）时，使用 reply_in_thread（由全局 FEISHU_REPLY_IN_THREAD 控制）
        if not binding.get('default_chat_follow_thread', True):
            return False
    return binding.get('reply_in_thread', False)


# =========================================================================
# Operator / Binding 查找
# =========================================================================

def _verify_operator_match(operator: dict, owner_id: str) -> bool:
    """验证 owner_id 是否与 operator 中的某个 ID 匹配

    operator 可能包含 open_id、user_id、union_id 等多个字段，
    逐一匹配即可，兼容不同格式的 owner_id 配置。

    Args:
        operator: 飞书事件中的 operator 对象
        owner_id: 配置的 owner_id

    Returns:
        True 表示匹配成功，False 表示匹配失败
    """
    if not operator or not owner_id:
        return False

    # 逐一匹配 operator 中的所有字段值
    for field_value in operator.values():
        if field_value == owner_id:
            logger.info(f"[feishu] Operator verification passed: owner_id={owner_id} matched in operator")
            return True

    return False


def _get_gateway_ws_url() -> str:
    """获取网关的 WebSocket 地址，用于注册提示

    从 CALLBACK_SERVER_URL（HTTP）转换为 ws(s):// 格式。

    Returns:
        ws(s):// 格式的网关地址，无法获取时返回空字符串
    """
    from config import CALLBACK_SERVER_URL
    if not CALLBACK_SERVER_URL:
        return ''
    if CALLBACK_SERVER_URL.startswith('https://'):
        return 'wss://' + CALLBACK_SERVER_URL[8:]
    elif CALLBACK_SERVER_URL.startswith('http://'):
        return 'ws://' + CALLBACK_SERVER_URL[7:]
    return ''


def _find_cowork_owner(chat_id: str, sender_owner_id: str = '') -> Optional[Dict[str, Any]]:
    """查找 chat_id 所属的、开启了协作模式的 owner 的 binding

    Args:
        chat_id: 群聊 ID
        sender_owner_id: 发送者的 owner_id（传入时跳过 owner 自己）

    Returns:
        owner 的 binding（含 _owner_id），或 None
    """
    from stores.group_session_store import GroupSessionStore
    from stores.binding_store import BindingStore

    gs_store = GroupSessionStore.get_instance()
    binding_store = BindingStore.get_instance()
    if not gs_store or not binding_store:
        return None

    owner_id = gs_store.find_owner_by_chat(chat_id)
    if not owner_id:
        return None
    if sender_owner_id and owner_id == sender_owner_id:
        return None

    owner_binding = binding_store.get(owner_id)
    if not owner_binding or not owner_binding.get('group_allow_cowork', False):
        return None

    return owner_binding


def _get_binding_from_event(feishu_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从飞书事件中获取绑定信息

    通过 sender_id 或 operator_id 查询 BindingStore 获取完整绑定信息。
    BindingStore.get() 会自动注入 _owner_id 字段。

    两种场景：
    1. 用户发送消息触发：feishu_event 包含 sender.sender_id
    2. 用户点击按钮触发：feishu_event 包含 operator（operator 本身就是 id 对象）

    Args:
        feishu_event: 飞书事件数据（包含 sender 或 operator 信息）

    Returns:
        绑定信息字典（包含 auth_token, callback_url, _owner_id 等），未找到返回 None。
    """
    # 协作者模式：上游已注入 owner binding，优先使用
    effective = feishu_event.get('_effective_binding')
    if effective:
        return effective

    from stores.binding_store import BindingStore

    binding_store = BindingStore.get_instance()
    if not binding_store:
        logger.warning("[feishu] BindingStore not initialized")
        return None

    # 场景 1: 从 sender 获取（用户发送消息时）
    sender_id_obj = feishu_event.get('sender', {}).get('sender_id', {})
    if sender_id_obj:
        for field_value in sender_id_obj.values():
            if field_value:
                binding = binding_store.get(field_value)
                if binding:
                    logger.info(f"[feishu] Found binding for sender_id={field_value}")
                    return binding
        logger.warning(f"[feishu] No binding found for sender={sender_id_obj}")

    # 场景 2: 从 operator 获取（用户点击按钮时）
    # operator 本身就是 id 对象 {open_id, user_id, union_id}
    operator = feishu_event.get('operator', {})
    if operator:
        for field_value in operator.values():
            if field_value:
                binding = binding_store.get(field_value)
                if binding:
                    logger.info(f"[feishu] Found binding for operator={field_value}")
                    return binding
        logger.warning(f"[feishu] No binding found for operator={operator}")

    return None


# =========================================================================
# Agent 命令解析
# =========================================================================

def _build_agent_commands_from_binding(binding):
    """从 binding 构建 agent commands 映射

    Returns:
        如 {'claude': ['claude', 'claude --model opus'], 'codex': ['codex']}
    """
    if not binding:
        return {}
    from config import VALID_AGENTS
    result = {}
    for at in VALID_AGENTS:
        cmds = binding.get('%s_commands' % at)
        if cmds:
            result[at] = cmds
    return result


def _merge_agent_commands(agent_commands: Dict[str, List[str]],
                          default_agent: str) -> List[Tuple[str, str]]:
    """将多 agent 的命令列表合并为有序列表，default_agent 排在前面

    Returns:
        如 [('codex', 'codex'), ('claude', 'claude'), ('claude', 'claude --model opus')]
    """
    from config import VALID_AGENTS
    order = [default_agent] + [a for a in VALID_AGENTS if a != default_agent]
    merged: List[Tuple[str, str]] = []
    for at in order:
        for cmd in agent_commands.get(at, []):
            merged.append((at, cmd))
    return merged


def _resolve_agent_command_from_binding(
    binding: Optional[Dict[str, Any]],
    cmd_arg: str
) -> Tuple[bool, str, str]:
    """从 binding 解析 agent 类型和命令

    Args:
        binding: 绑定信息字典（包含 claude_commands, codex_commands）
        cmd_arg: 用户输入的 --cmd 参数值，可以是:
            - 空字符串：返回默认 agent 的首条命令
            - agent_type::command 格式（如 codex::codex）
            - 数字字符串（索引，从 0 开始，在合并列表中）
            - 名称子串（大小写敏感匹配）

    Returns:
        (success, agent_type, command_or_error):
            - success=True, agent_type=匹配到的 agent 类型, command_or_error=命令字符串
            - success=False, agent_type='', command_or_error=错误提示信息
    """
    if not binding:
        return (False, '', '用户未注册，无法获取命令列表')

    agent_commands = _build_agent_commands_from_binding(binding)
    if not agent_commands:
        return (False, '', '该用户注册信息中没有命令列表，请重新注册')

    default_agent = binding.get('default_agent', 'claude')
    merged = _merge_agent_commands(agent_commands, default_agent)
    available = ', '.join('`[%s] %s::%s`' % (i, at, cmd) for i, (at, cmd) in enumerate(merged))

    if not cmd_arg:
        if default_agent in agent_commands:
            return (True, default_agent, agent_commands[default_agent][0])
        at, cmd = merged[0]
        return (True, at, cmd)

    # agent_type::command 格式（验证 agent_type 和 command 合法性）
    if '::' in cmd_arg:
        parts = cmd_arg.split('::', 1)
        req_at, req_cmd = parts[0], parts[1]
        if req_at in agent_commands and req_cmd in agent_commands[req_at]:
            return (True, req_at, req_cmd)
        return (False, '', '无效的命令 "%s"，可用命令: %s' % (cmd_arg, available))

    # 数字索引
    if cmd_arg.isdigit():
        idx = int(cmd_arg)
        if 0 <= idx < len(merged):
            at, cmd = merged[idx]
            return (True, at, cmd)
        return (False, '', '索引 %s 超出范围，可用命令: %s' % (cmd_arg, available))

    # 子串匹配（精确匹配优先，多个子串匹配时报歧义错误）
    exact = [(at, cmd) for at, cmd in merged if cmd_arg == cmd]
    if len(exact) == 1:
        return (True, exact[0][0], exact[0][1])
    matches = [(at, cmd) for at, cmd in merged if cmd_arg in cmd]
    if len(matches) == 1:
        return (True, matches[0][0], matches[0][1])
    if len(matches) > 1:
        matched_list = ', '.join('`%s::%s`' % (at, cmd) for at, cmd in matches)
        return (False, '', '"%s" 匹配到多个命令: %s，请更精确地指定' % (cmd_arg, matched_list))

    return (False, '', '未找到匹配 "%s" 的命令，可用命令: %s' % (cmd_arg, available))


# =========================================================================
# Callback 通信工具
# =========================================================================

def _set_last_message_id_to_callback(binding: Dict[str, Any],
                                     session_id: str, message_id: str) -> bool:
    """通过 Callback 后端设置 session 的 last_message_id

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        session_id: 会话 ID
        message_id: 飞书消息 ID

    Returns:
        是否设置成功
    """
    from services.session_facade import SessionFacade
    return SessionFacade.set_last_message_id(binding, session_id, message_id)
