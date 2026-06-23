"""
Feishu Handler - 飞书事件处理器

处理飞书相关的 POST 请求：
    - URL 验证（type: url_verification）
    - 消息事件（im.message.receive_v1）
    - 卡片回传交互（card.action.trigger）
    - 发送消息（/gw/feishu/send）

WebSocket 隧道支持：
    - _forward_via_ws_or_http(): 优先通过 WS 隧道转发请求，失败时 fallback 到 HTTP
    - 适用于 Callback 后端不可公网访问的场景（本地开发、内网部署）
"""

import hmac
import json
import logging
import uuid
from typing import Dict, List, Tuple

from utils.concurrency import run_in_background
from services.session_facade import SessionFacade

# 本模块实际使用的内部 import
from .utils import (
    _COLLABORATOR_ALLOWED_COMMANDS, _SESSION_NOT_FOUND_HINT,
    _sanitize_user_content, _log_message_event,
    _is_multi_person_group, _get_gateway_ws_url,
    _get_binding_from_event, _find_cowork_owner,
)
from .content import build_mention_resolution, extract_message_text
from .message import _send_notice_message, _send_help_card
from .forward import (
    _forward_via_ws_or_http,
    _forward_continue_request, _forward_new_request,
    _forward_new_request_for_default_dir,
)
from .card_action import _handle_card_action
from .command import (
    _handle_new_command, _handle_reply_command,
    _handle_groups_command, _handle_attach_command,
    _handle_clear_command, _handle_users_command,
)
from .mute import _handle_mute_command, _handle_unmute_command
from .notify import _handle_notify_command

# 对外公开 API（外部通过 from handlers.feishu import ... 使用）
# __all__ 同时让 pyflakes 知道这些 re-export 是有意为之
from .group import (  # noqa: F401
    handle_send_message, handle_create_group, handle_remove_reaction,
    create_group_chat_and_record, batch_dissolve_groups,
    find_idle_group_chats, handle_card_action_register,
)

__all__ = [
    'handle_feishu_request',
    'handle_send_message', 'handle_create_group', 'handle_remove_reaction',
    'create_group_chat_and_record', 'batch_dissolve_groups',
    'find_idle_group_chats', 'handle_card_action_register',
]

logger = logging.getLogger(__name__)


def handle_feishu_request(data: dict, skip_token_validation: bool = False) -> Tuple[bool, dict]:
    """处理飞书请求

    支持的请求类型：
        - url_verification: URL 验证
        - im.message.receive_v1: 消息接收事件
        - card.action.trigger: 卡片回传交互事件

    Args:
        data: 请求 JSON 数据
        skip_token_validation: 跳过 token 验证（长连接模式使用）

    Returns:
        (handled, response): handled 表示是否处理了请求，response 是响应数据
    """
    # URL 验证请求（优先处理，无需验证 token）
    if data.get('type') == 'url_verification':
        return _handle_url_verification(data)

    # 验证 Verification Token（HTTP 回调模式需要，长连接模式跳过）
    if not skip_token_validation and not _verify_token(data):
        logger.warning("[feishu] Invalid verification token")
        return False, {'success': False, 'error': 'Invalid verification token'}

    # 事件订阅（schema 2.0）
    header = data.get('header', {})
    event_type = header.get('event_type', '')

    if event_type == 'im.message.receive_v1':
        _handle_message_event(data)
        return True, {'success': True}

    # 卡片回传交互事件
    if event_type == 'card.action.trigger':
        return _handle_card_action(data)

    # 未处理的飞书事件类型或其他请求
    event_type = data.get('header', {}).get('event_type', '')
    logger.debug(f"[feishu] Unhandled request, event_type={event_type}, data: {json.dumps(data, ensure_ascii=True)}")
    return False, {}


def _verify_token(data: dict) -> bool:
    """验证 Verification Token

    从请求 header 中提取 token 并与配置比对。
    如果未配置 token，则跳过验证（兼容现有部署）。

    Args:
        data: 飞书请求数据

    Returns:
        True: 验证通过或未配置 token
        False: 验证失败
    """
    from config import FEISHU_VERIFICATION_TOKEN

    # 未配置 token，跳过验证
    if not FEISHU_VERIFICATION_TOKEN:
        return True

    # 从 header 提取 token
    header = data.get('header', {})
    token = header.get('token', '')

    if not token:
        logger.warning("[feishu] Request missing token in header")
        return False

    # 验证 token（恒定时间比较，防止时序攻击）
    if not hmac.compare_digest(token, FEISHU_VERIFICATION_TOKEN):
        logger.warning("[feishu] Token mismatch")
        return False

    return True


def _handle_url_verification(data: dict) -> Tuple[bool, dict]:
    """处理飞书 URL 验证请求

    飞书在配置事件订阅时会发送验证请求，需要在 1 秒内返回 challenge 值。

    Args:
        data: 请求数据，包含 challenge 字段

    Returns:
        (True, {'challenge': xxx})
    """
    challenge = data.get('challenge', '')
    logger.info(f"[feishu] URL verification, challenge: {challenge[:20]}...")
    return True, {'challenge': challenge}


def _handle_message_event(data: dict):
    """处理飞书消息事件 im.message.receive_v1

    Args:
        data: 飞书事件数据
    """
    event = data.get('event', {})
    message = event.get('message', {})
    sender = event.get('sender', {})
    sender_id_obj = sender.get('sender_id', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    chat_type = message.get('chat_type', '')  # p2p / group
    message_type = message.get('message_type', '')  # text / image / ...
    sender_id = sender_id_obj.get('open_id', '')
    user_id = sender_id_obj.get('user_id', sender_id)
    parent_id = message.get('parent_id', '')  # 是否是回复消息

    # 构建 @提及 替换表（bot→删除，人员→@name）并判断是否 @bot
    mention_resolution, is_at_bot = build_mention_resolution(message.get('mentions'))

    # 解析消息纯文本内容（@ 占位符在解析过程中按 resolution 表替换）
    text = extract_message_text(message_type, message.get('content', '{}'), mention_resolution)

    # 先记录原始数据到日志（所有消息都记录），脱敏用户内容
    _log_message_event(data, text)

    logger.info(f"[feishu] Message received: chat_type={chat_type}, message_type={message_type}, parent_id={parent_id if parent_id else ''}, text={_sanitize_user_content(text)}")

    # 将解析后的纯文本写入 message['plain_text']，供下游直接使用
    message['plain_text'] = text

    # @bot 状态写入 message，供下游（多人群过滤等）使用
    message['is_at_bot'] = is_at_bot
    logger.debug(f"[feishu] is_at_bot={is_at_bot}, mentions_count={len(message.get('mentions', []))}")

    # 获取用户绑定信息（后续处理统一使用）
    binding = _get_binding_from_event(event)

    # 协作者模式前置检测：群聊 + 无 binding 或群内无自己 session + owner 开启 cowork
    # 原始 binding 保存到 _original_binding，供 _handle_command 非白名单命令时恢复
    if chat_type == 'group':
        owner_binding = None
        original_binding = None
        if not binding:
            # 未注册用户：尝试借用 owner binding
            owner_binding = _find_cowork_owner(chat_id)
        else:
            # 已注册用户：群内没有自己的 session 时作为协作者
            owner_id = binding.get('_owner_id', '')
            from stores.group_session_store import GroupSessionStore
            gs_store = GroupSessionStore.get_instance()
            has_own_session = gs_store and gs_store.get(owner_id, chat_id) is not None
            if not has_own_session:
                owner_binding = _find_cowork_owner(chat_id, owner_id)
                original_binding = binding
        if owner_binding:
            binding = dict(owner_binding)
            binding['_collaborator_user_id'] = user_id
            binding['_original_binding'] = original_binding
            event['_effective_binding'] = binding

    # 未注册且非协作者：提示注册
    # - 单聊始终提示；群聊仅 @bot 时提示
    if not binding:
        is_p2p = (chat_type == 'p2p')
        should_respond = is_p2p or message.get('is_at_bot', False)
        if should_respond:
            gateway_ws_url = _get_gateway_ws_url()
            hint = "您（用户 ID：`%s`）尚未注册，无法使用此功能。" % user_id
            if gateway_ws_url:
                hint += "\n\n请在部署了 CLI（如 Claude Code、Codex 等）的系统终端上执行以下命令完成注册：\n" \
                        "```\ncurl -fsSL https://raw.githubusercontent.com/frankie-huang/code-anywhere/refs/heads/main/setup.sh | bash -s -- --gateway-url=%s --owner-id=%s\n```" \
                        "\n如果网关地址（`--gateway-url`）非公网可达，请联系管理员获取对外可用的网关地址。" \
                        "\n\n注意：执行命令前，请先申请当前应用的使用权限，否则将无法接收到注册绑定卡片。如未申请，请先申请权限后再执行命令。" % (gateway_ws_url, user_id)
            run_in_background(_send_notice_message, (chat_id, hint, message_id))
        return

    # group 模式下多人群需要 @bot，单人群（owner + bot）不需要
    if chat_type == 'group' and not message.get('is_at_bot', False):
        if binding.get('session_mode') == 'group' and _is_multi_person_group(chat_id):
            logger.debug("[feishu] Ignored non-@bot message in multi-person group: chat=%s msg=%s",
                         chat_id, message_id)
            return

    # 检查是否是命令（优先处理，因为命令也可能是回复消息）
    # Agent 斜杠命令（如 /compact）不由网关处理，作为普通消息转发给 Agent 执行
    is_command, command, args = _parse_command(text)
    is_slash_cmd = is_command and command in _get_slash_commands()
    if is_command and not is_slash_cmd:
        _handle_command(data, command, args)
        return

    # 非命令消息：空内容早退（图片/贴图/非文字消息等均 text 为空）
    prompt = text.strip()
    if not prompt:
        hint = "消息内容为空，无法继续会话" if parent_id else "消息内容为空，请发送文字消息与我对话"
        run_in_background(_send_notice_message, (chat_id, hint, message_id))
        return

    # 路由到已有 session：优先 parent_id，其次 group 模式群聊 chat_id 反查
    route_info = SessionFacade.resolve_from_message(data, binding)
    route_source = route_info['source']

    if SessionFacade.RouteSource.is_parent_not_found(route_source):
        run_in_background(_send_notice_message,
                           (chat_id,
                            _SESSION_NOT_FOUND_HINT + "请稍后重试或重新发起 /new 指令。",
                            message_id))
        return

    if SessionFacade.RouteSource.is_resolved(route_source):
        # 协作者：添加发送者前缀（binding 已替换为 owner 的，转发天然正确）
        # Agent 斜杠命令不加前缀，否则 Agent 无法解析（如 /compact → "[来自群成员 xxx] /compact"）
        collaborator_user_id = binding.get('_collaborator_user_id', '')
        if collaborator_user_id:
            if not is_slash_cmd:
                prompt = "[来自群成员 %s] %s" % (collaborator_user_id, prompt)
            logger.info("[feishu] Collaborator route to session (%s): session_id=%s, collaborator=%s, prompt=%s",
                        route_source, route_info['session_id'], collaborator_user_id, _sanitize_user_content(prompt))
        else:
            logger.info("[feishu] Route to session (%s): session_id=%s, prompt=%s",
                        route_source, route_info['session_id'], _sanitize_user_content(prompt))
        # 刷新群聊活跃时间（供自动解散空闲判断）
        if route_source == SessionFacade.RouteSource.GROUP_CHAT:
            from stores.group_session_store import GroupSessionStore
            gs_store = GroupSessionStore.get_instance()
            owner_id = binding.get('_owner_id', '')
            if gs_store and owner_id:
                gs_store.touch(owner_id, chat_id)
        # /clear 后首条消息：new_session 标志表示需要启动新 Agent 进程
        # agent_type 和 command 不在路由表中，由 callback 侧从 session store 读取
        if route_info.get('new_session'):
            run_in_background(_forward_new_request,
                               (binding, route_info['session_id'],
                                route_info['project_dir'], prompt,
                                chat_id, message_id, chat_type))
        else:
            run_in_background(_forward_continue_request,
                               (binding, route_info['session_id'],
                                route_info['project_dir'], prompt,
                                chat_id, message_id))
        return

    # 协作者未路由到 session：群内无活跃 session，静默忽略
    if binding.get('_collaborator_user_id'):
        logger.debug("[feishu] Collaborator message ignored: no active session in chat=%s", chat_id)
        return

    # 未路由到已有 session：走默认聊天目录 / 使用提示
    default_chat_dir = binding.get('default_chat_dir', '')
    # 配置了默认聊天目录时，自动创建/继续会话
    if default_chat_dir:
        _handle_default_chat_message(data, prompt, binding)
        return

    # 已注册但未配置默认目录：发送帮助卡片
    hint = "💡 我还不能直接对话哦，可选择以下方式使用：\n" \
           "- **发起新会话**\n\n" \
           "发送 `/new` 指令创建新会话\n" \
           "- **继续会话**\n\n" \
           "回复会话消息即可继续对话\n" \
           "- **指定默认目录**\n\n" \
           "在 `.env` 中配置 `DEFAULT_CHAT_DIR` 并重启服务，即可直接发消息对话"
    run_in_background(_send_help_card, (binding, chat_id, message_id,
                                         _COMMANDS, _slash_commands_as_help_dict(), hint))




def _parse_command(text: str) -> Tuple[bool, str, str]:
    """解析命令

    支持格式：
    - /command arg1 arg2
    - /command --key=value arg

    Args:
        text: 消息文本

    Returns:
        (is_command, command, args):
            - is_command: 是否是命令
            - command: 命令名（不含 /）
            - args: 参数部分（不含命令名）
    """
    stripped = text.strip()
    if not stripped.startswith('/'):
        return False, '', ''

    # 找到第一个空格或结尾，提取命令名
    parts = stripped[1:].split(None, 1)  # 移除 /，然后按空白分割
    if not parts:
        return False, '', ''

    command = parts[0]
    args = parts[1] if len(parts) > 1 else ''
    return True, command, args


def _handle_command(data: dict, command: str, args: str):
    """处理命令

    Args:
        data: 飞书事件数据
        command: 命令名（如 'new'）
        args: 参数部分
    """
    from config import FEISHU_OWNER_ID as gateway_owner_id

    # 统一获取事件信息（协作者场景下 _effective_binding 已由 _handle_message_event 前置注入）
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    binding = _get_binding_from_event(event)
    if not binding:
        return

    owner_id = binding.get('_owner_id', '')

    # 协作者命令处理（身份已由 _handle_message_event 前置判定）
    # - 白名单命令（如 /clear）→ 用 owner binding 操作 owner 的 session
    # - 非白名单命令 → 恢复原始 binding 操作自己的 session；未注册用户无原始 binding 则拒绝
    if binding.get('_collaborator_user_id'):
        if command not in _COLLABORATOR_ALLOWED_COMMANDS:
            original_binding = binding.get('_original_binding')
            if original_binding:
                # 已注册用户：恢复自己的 binding，以自己的身份执行命令
                binding = original_binding
                owner_id = binding.get('_owner_id', '')
                event.pop('_effective_binding', None)
            else:
                # 未注册用户：无自己的 binding，拒绝
                run_in_background(_send_notice_message,
                                   (chat_id, "协作者暂不支持此命令，仅会话创建者可执行管理操作。", message_id))
                return

    handler_info = _COMMANDS.get(command)
    if handler_info:
        handler_func, admin_only, _, _ = handler_info
        # 管理员专属指令需要权限检查
        if admin_only and owner_id != gateway_owner_id:
            if chat_id:
                run_in_background(_send_notice_message, (chat_id, "此指令仅限管理员使用", message_id))
            return
        handler_func(data, args)
    else:
        logger.info(f"[feishu] Unknown command: /{command}")
        if chat_id:
            run_in_background(_send_help_card,
                               (binding, chat_id, message_id,
                                _COMMANDS, _slash_commands_as_help_dict(), f"未知指令：`/{command}`"))


def _handle_default_chat_message(data: dict, prompt: str, binding: dict) -> None:
    """处理默认聊天目录下的普通消息

    当用户的 binding 中配置了 default_chat_dir 时，普通消息（非指令、非回复）会：
    - 有活跃默认会话 → 继续该会话
    - 无活跃默认会话 → 在默认目录创建新会话

    Args:
        data: 飞书事件数据
        prompt: 用户消息内容（已清理）
        binding: 用户绑定信息（包含 default_chat_dir）
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    chat_type = message.get('chat_type', '')
    message_id = message.get('message_id', '')

    default_chat_dir = binding.get('default_chat_dir', '')
    session_id = binding.get('default_chat_session_id', '')
    owner_id = binding.get('_owner_id', '')

    if session_id:
        # 继续活跃的默认会话
        logger.info(f"[default-chat] Continuing session {session_id} for {owner_id}, prompt={_sanitize_user_content(prompt)}")
        run_in_background(_forward_continue_request, (
            binding, session_id, default_chat_dir,
            prompt, chat_id, message_id
        ))
    else:
        # 创建新的默认会话
        logger.info(f"[default-chat] Creating new session in {default_chat_dir} for {owner_id}, prompt={_sanitize_user_content(prompt)}")
        new_session_id = str(uuid.uuid4())
        run_in_background(_forward_new_request_for_default_dir, (
            binding, new_session_id, default_chat_dir, prompt, chat_id, message_id, chat_type
        ))


def _handle_help_command(data: dict, args: str) -> None:
    """处理 /help 命令：展示指令帮助卡片"""
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    binding = _get_binding_from_event(event)
    run_in_background(_send_help_card, (binding, chat_id, message_id,
                                         _COMMANDS, _slash_commands_as_help_dict()))


# =============================================================================
# 命令映射（放在文件末尾，避免函数未定义的问题）
# =============================================================================

# 支持的命令映射：命令名 -> (处理函数, 是否管理员专属, 简述, 示例列表)
# admin_only 为 True 时，仅管理员可见和执行
# 格式：(handler, admin_only, brief, examples)
#   brief: 简短说明
#   examples: [(示例, 说明), ...]
_COMMANDS = {
    'new': (_handle_new_command, False, "发起新会话", [
        ("/new", "收到卡片，完善会话信息（指定工作目录，填写提示词等）后发起新会话"),
        ("/new prompt", "- 回复已有会话消息时 - 继承工作目录发起新会话\n- 已配置 `DEFAULT_CHAT_DIR` - 从默认目录发起新会话\n- 未配置 `DEFAULT_CHAT_DIR` - 收到卡片完善会话信息"),
        ("/new --dir=/path prompt", "指定工作目录发起新会话"),
        ("/new --cmd=0 prompt", "指定 Command 发起新会话（需要提前配置 `CLAUDE_COMMAND` 或 `CODEX_COMMAND`）"),
    ]),
    'reply': (_handle_reply_command, False, "回复消息时继续会话", [
        ("/reply --cmd=0 prompt", "指定 Command 继续会话（需要提前配置 `CLAUDE_COMMAND` 或 `CODEX_COMMAND`）"),
    ]),
    'mute': (_handle_mute_command, False, "静音会话或目录", [
        ("/mute", "静音当前会话"),
        ("/mute <session_id>", "静音指定会话，需要指定完整的 session_id"),
        ("/mute /path", "静音指定目录，后续从终端发起的该目录的新会话将不再通知"),
        ("/mute /path/**", "递归静音目录及其所有子孙目录"),
        ("/mute list", "查看静音和加白规则列表"),
    ]),
    'unmute': (_handle_unmute_command, False, "解除静音 / 标记目录为不静音", [
        ("/unmute", "解除当前会话静音"),
        ("/unmute <session_id>", "解除指定会话静音，需要指定完整的 session_id"),
        ("/unmute /path", "解除目录静音，或标记为不静音"),
        ("/unmute /path/**", "解除目录递归静音，或标记目录及其所有子目录为不静音"),
    ]),
    'groups': (_handle_groups_command, False, "【群聊模式】管理群聊会话", [
        ("/groups", "列出所有自动创建的群聊"),
        ("/groups dissolve all", "解散所有自动创建的群聊"),
        ("/groups dissolve idle 7", "解散空闲超过 N 天未活跃的群聊（示例：7 天）"),
        ("/groups dissolve 1 2", "按序号解散群聊，支持批量指定"),
        ("/groups dissolve /path", "解散指定目录的群聊（精准匹配）"),
        ("/groups dissolve /path/**", "解散指定目录及子目录的群聊"),
    ]),
    'attach': (_handle_attach_command, False, "【群聊模式】绑定 session 到当前群聊", [
        ("/attach <session_id>", "`<session_id>` 可以使用前缀（至少 8 个字符）"),
    ]),
    'clear': (_handle_clear_command, False, "【群聊模式】重置当前群聊会话", [
        ("/clear", "解绑会话，下次发消息自动创建新会话"),
    ]),
    'notify': (_handle_notify_command, False, "管理通知配置", [
        ("/notify status", "查看当前通知配置"),
        ("/notify at off", "关闭通知 @"),
        ("/notify at self", "恢复默认通知 @（@ 自己）"),
        ("/notify at all", "通知 @ 所有人"),
        ("/notify at <user_id>", "通知 @ 指定用户"),
        ("/notify at HH:MM-HH:MM", "设置通知 @ 时段（仅在时段内 @）"),
        ("/notify at always", "清除通知时段限制"),
        ("/notify delay <秒>", "设置权限通知延迟秒数"),
        ("/notify delay default", "恢复默认权限通知延迟"),
    ]),
    'users': (_handle_users_command, True, "查看已注册用户和在线状态", [
        ("/users", "列出用户及在线状态"),
    ]),
    'help': (_handle_help_command, False, "查看指令帮助", [
        ("/help", "显示本帮助卡片"),
    ]),
}

# =============================================================================
# Agent 斜杠命令（从各 adapter 动态收集，替代硬编码的透传命令列表）
# =============================================================================

_slash_commands_cache = None
_slash_help_cache = None


def _get_slash_commands():
    """获取所有 Agent 斜杠命令（带缓存）

    首次调用时从各 adapter 的 get_slash_commands() 收集并构建两份缓存：
    - 路由用：命令名 -> SlashCommandInfo 映射
    - Help 用：命令名 -> _build_cmd_rows 格式（含 Agent 归属标注）
    """
    global _slash_commands_cache, _slash_help_cache
    if _slash_commands_cache is None:
        from config import VALID_AGENTS
        from agents import SlashCommandInfo, get_agent_adapter

        commands: Dict[str, SlashCommandInfo] = {}
        cmd_agents: Dict[str, List[str]] = {}
        for agent_type in VALID_AGENTS:
            adapter = get_agent_adapter(agent_type)
            for name, info in adapter.get_slash_commands().items():
                # 与网关内置命令冲突时跳过，避免绕过网关逻辑
                if name in _COMMANDS:
                    logger.warning("Slash command '%s' conflicts with gateway command, skipped", name)
                    continue
                # 多个 adapter 声明同名命令时，brief 和 examples 取首个 adapter 的
                commands.setdefault(name, info)
                cmd_agents.setdefault(name, []).append(adapter.display_name)

        help_dict = {}
        for name, info in commands.items():
            agents_label = ' / '.join(cmd_agents[name])
            brief = f"{info.brief}（{agents_label}）"
            help_dict[name] = (None, False, brief, info.examples)

        _slash_commands_cache = commands
        _slash_help_cache = help_dict
    return _slash_commands_cache


def _slash_commands_as_help_dict():
    """获取斜杠命令的 help 卡片字典（含 Agent 归属标注）

    Returns:
        与 _COMMANDS 相同格式的字典：{name: (None, False, brief, examples)}
    """
    _get_slash_commands()  # 确保缓存已构建
    return _slash_help_cache


# 模块加载末尾：注入 SessionFacade 的下游依赖（避免 services -> handlers 循环 import）
SessionFacade.configure(forward_fn=_forward_via_ws_or_http)
