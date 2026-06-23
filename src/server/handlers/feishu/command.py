"""
Feishu Command - 命令处理函数

处理飞书消息中的各类命令：
- _parse_command_args: 解析 /new 等命令的 --dir= 和 --cmd= 参数
- _send_new_session_card: 发送新会话卡片
- _handle_new_command: /new 命令处理
- _handle_reply_command: /reply 命令处理
- _handle_groups_command: /groups 命令处理
- _dissolve_groups: 解散群聊逻辑
- _handle_attach_command: /attach 命令处理
- _handle_clear_command: /clear 命令处理
- _handle_users_command: /users 命令处理
"""

import json
import logging
import os
import shlex
import time
import uuid
from typing import Any, Dict, List, Tuple

from utils.concurrency import run_in_background
from services.session_facade import SessionFacade

from .utils import (
    _SESSION_NOT_FOUND_HINT,
    _sanitize_user_content, _should_reply_in_thread,
    _get_binding_from_event,
    _build_agent_commands_from_binding,
    _resolve_agent_command_from_binding,
)
from .message import (
    _send_notice_message,
    _build_user_status_card,
    _send_users_status_card,
)
from .forward import (
    _forward_continue_request,
    _forward_new_request,
    _forward_new_request_for_default_dir,
    _forward_attach_request,
    _fetch_recent_dirs_from_callback,
)
from .card_session import _build_new_session_card
from .group import batch_dissolve_groups, find_idle_group_chats, _send_groups_card

logger = logging.getLogger(__name__)


def _parse_command_args(args: str) -> Tuple[bool, str, str, str]:
    """解析指令参数，提取 --dir=、--cmd= 和 prompt

    支持格式（参数顺序不限）：
    - --dir=/path --cmd=1 prompt
    - --cmd=opus --dir=/path prompt
    - --dir=/path prompt
    - --cmd=opus prompt
    - prompt（回复模式）

    Args:
        args: 参数部分（不含指令名）

    Returns:
        (success, project_dir, cmd_arg, prompt)
    """
    args = args.strip()
    if not args:
        return True, '', '', ''

    # 检查是否有 --dir= 或 --cmd= 参数
    has_named_args = args.startswith('--dir=') or args.startswith('--cmd=')
    if not has_named_args:
        return True, '', '', args

    try:
        parts = shlex.split(args, posix=False)
    except ValueError as e:
        logger.warning(f"[feishu] Failed to parse command args: {e}")
        return False, '', '', ''

    project_dir = ''
    cmd_arg = ''
    prompt_parts = []

    for part in parts:
        if part.startswith('--dir='):
            project_dir = part[6:]
        elif part.startswith('--cmd='):
            cmd_arg = part[6:]
        else:
            prompt_parts.append(part)

    prompt = ' '.join(prompt_parts)
    return True, project_dir, cmd_arg, prompt



def _send_new_session_card(binding: dict, owner_id: str, chat_id: str,
                           message_id: str, chat_type: str,
                           project_dir: str, prompt: str,
                           agent_command: str = ''):
    """发送工作目录选择卡片

    Args:
        binding: 绑定信息（包含 auth_token, callback_url, claude_commands, codex_commands 等）
        owner_id: 用户 ID
        chat_id: 群聊 ID
        message_id: 原始消息 ID（用于回复）
        chat_type: 聊天类型（group/p2p），卡片提交时透传
        project_dir: 项目目录（用作 custom_dir 输入框的默认值）
        prompt: 用户输入的 prompt（作为 prompt 输入框的默认值）
        agent_command: 预选的 Agent 命令（可选，来自 --cmd 参数）
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.error("[feishu] FeishuAPIService not enabled, cannot send new session card")
        return

    if not binding:
        logger.warning("[feishu] No binding found, cannot fetch recent dirs")
        run_in_background(_send_notice_message, (chat_id, "您尚未注册，无法使用此功能", message_id))
        return

    reply_in_thread = _should_reply_in_thread(binding, project_dir)

    # 从 Callback 后端获取常用目录列表
    recent_dirs = _fetch_recent_dirs_from_callback(binding, limit=20)

    card = _build_new_session_card(
        owner_id=owner_id, chat_id=chat_id, message_id=message_id,
        chat_type=chat_type,
        recent_dirs=recent_dirs,
        custom_dir=project_dir or '',
        prompt=prompt,
        agent_commands=_build_agent_commands_from_binding(binding),
        agent_command=agent_command,
        default_agent=binding.get('default_agent', 'claude')
    )

    # 打印完整卡片 JSON 用于调试
    card_json = json.dumps(card, ensure_ascii=True, indent=2)
    logger.info(f"[feishu] Dir selector card JSON:\n{card_json}")

    if message_id:
        success, sent_message_id = service.reply_card(json.dumps(card, ensure_ascii=False), message_id, reply_in_thread)
    else:
        success, sent_message_id = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=chat_id, receive_id_type='chat_id')

    if success:
        logger.info(f"[feishu] Sent new session card to {chat_id}, card_msg_id={sent_message_id}")
    else:
        logger.error(f"[feishu] Failed to send new session card: {sent_message_id}")
        _send_notice_message(chat_id, "会话卡片发送失败，请稍后重试", message_id)


def _handle_new_command(data: dict, args: str):
    """处理 /new 指令，发起新会话

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /new）
    """
    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')

    # 解析指令参数（支持 --dir= 和 --cmd=）
    success, project_dir, cmd_arg, prompt = _parse_command_args(args)
    if not success:
        run_in_background(_send_notice_message, (chat_id, "参数格式错误，正确格式：`/new --dir=/path/to/project [--cmd=0] prompt`", message_id))
        return

    binding = _get_binding_from_event(event)
    if not binding:
        run_in_background(_send_notice_message, (chat_id, "您尚未注册，无法使用此功能", message_id))
        return
    owner_id = binding.get('_owner_id', '')
    msg_chat_type = message.get('chat_type', '')

    # 从上下文继承 project_dir 和 command（用户未显式指定时）
    # 优先级：--dir/--cmd 参数 > 消息上下文解析到的旧 session > 默认值
    inherited_dir = ''
    inherited_cmd = ''
    inherited_agent_type = ''
    need_inherit = not project_dir or not cmd_arg

    if need_inherit:
        # 两条分支统一走 resolve_from_message：parent_id / group chat 的路由定位
        route_info = SessionFacade.resolve_from_message(data, binding)
        route_source = route_info.get('source', '')
        if SessionFacade.RouteSource.is_resolved(route_source):
            inherited_dir = route_info.get('project_dir', '')
            # command 本地路由 store 不存，回源 callback 拿权威值
            # （/new 低频场景，1 次 RPC 可接受）
            if not cmd_arg:
                inherited_info = SessionFacade.fetch_session_info(
                    binding, route_info.get('session_id', ''))
                inherited_cmd = inherited_info.get('command', '')
                inherited_agent_type = inherited_info.get('agent_type', '')

    # --cmd 参数优先，继承次之，binding 默认命令兜底
    if cmd_arg:
        # 用户显式指定 --cmd，从 binding 命令列表解析
        ok, agent_type, result = _resolve_agent_command_from_binding(binding, cmd_arg)
        if not ok:
            run_in_background(_send_notice_message, (chat_id, result, message_id))
            return
        command = result
    elif inherited_cmd:
        # 继承旧 session 的命令和 agent_type
        command = inherited_cmd
        agent_type = inherited_agent_type
        logger.info(f"[feishu] /new inherited command: {command}, agent_type: {agent_type}")
    else:
        # 无 --cmd 无继承命令，使用默认命令（有继承 agent_type 时用该 agent 的默认命令）
        if inherited_agent_type:
            agent_commands = _build_agent_commands_from_binding(binding)
            cmds = agent_commands.get(inherited_agent_type)
            if cmds:
                agent_type = inherited_agent_type
                command = cmds[0]
            else:
                # 该 agent 未在 binding 中注册，fallback 到全局默认
                ok, agent_type, result = _resolve_agent_command_from_binding(binding, '')
                if not ok:
                    run_in_background(_send_notice_message, (chat_id, result, message_id))
                    return
                command = result
        else:
            ok, agent_type, result = _resolve_agent_command_from_binding(binding, '')
            if not ok:
                run_in_background(_send_notice_message, (chat_id, result, message_id))
                return
            command = result

    # --dir 参数优先，继承次之
    if not project_dir and inherited_dir:
        project_dir = inherited_dir
        logger.info(f"[feishu] /new inherited project_dir: {project_dir}")

    # 没有 --dir 但有 prompt：尝试使用用户的默认聊天目录
    default_chat_dir = binding.get('default_chat_dir', '')
    if not project_dir and prompt and default_chat_dir:
        project_dir = default_chat_dir
        logger.info(f"[default-chat] /new using default dir: {default_chat_dir}")

    # 验证参数：如果没有目录或没有提示词，发送卡片让用户完善
    if not project_dir or not prompt:
        # 拼接 agent_type::command 格式传给卡片，保留用户已选的 agent 类型
        card_agent_cmd = f'{agent_type}::{command}' if agent_type and command else command
        run_in_background(_send_new_session_card, (binding, owner_id, chat_id, message_id, msg_chat_type, project_dir, prompt, card_agent_cmd))
        return

    logger.info(f"[feishu] /new command: dir={project_dir}, agent={agent_type or '(default)'}, cmd={command or '(default)'}, prompt={_sanitize_user_content(prompt)}")

    # 在后台线程中转发到 Callback 后端
    # 如果使用的是默认聊天目录，同时更新活跃默认会话
    new_session_id = str(uuid.uuid4())
    if default_chat_dir and os.path.realpath(project_dir) == os.path.realpath(default_chat_dir):
        run_in_background(_forward_new_request_for_default_dir, (binding, new_session_id, project_dir, prompt, chat_id, message_id, msg_chat_type, command, agent_type))
    else:
        run_in_background(_forward_new_request, (binding, new_session_id, project_dir, prompt, chat_id, message_id, msg_chat_type, command, agent_type))


def _handle_reply_command(data: dict, args: str):
    """处理 /reply 指令，在回复消息时指定 Claude Command 继续会话

    仅在回复消息时可用。支持 --cmd= 参数。

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /reply）
    """
    event = data.get('event', {})
    message = event.get('message', {})

    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')
    parent_id = message.get('parent_id', '')
    chat_type = message.get('chat_type', '')

    # /reply 需要回复消息或在 group 模式群聊中使用
    binding = _get_binding_from_event(event)
    session_mode = binding.get('session_mode', '') if binding else ''
    if not parent_id and not (session_mode == 'group' and chat_type == 'group'):
        run_in_background(_send_notice_message, (chat_id, "`/reply` 指令仅支持在回复消息时使用，或在群聊模式的群聊中直接使用", message_id))
        return

    # 解析参数
    success, project_dir, cmd_arg, prompt = _parse_command_args(args)
    if not success:
        run_in_background(_send_notice_message, (chat_id, "参数格式错误，正确格式：`/reply [--cmd=0] prompt`", message_id))
        return

    if project_dir:
        run_in_background(_send_notice_message, (chat_id, "`/reply` 不支持 `--dir` 参数，会话目录由原始 session 决定。请去掉 `--dir` 后重试", message_id))
        return

    if not prompt:
        run_in_background(_send_notice_message, (chat_id, "请提供问题内容，格式：`/reply [--cmd=0] prompt`", message_id))
        return

    # 解析 --cmd 参数（从 binding 获取命令列表）
    command = ''
    agent_type = ''
    if cmd_arg:
        ok, agent_type, result = _resolve_agent_command_from_binding(binding, cmd_arg)
        if not ok:
            run_in_background(_send_notice_message, (chat_id, result, message_id))
            return
        command = result

    # 路由 session（统一走 SessionFacade）
    route_info = SessionFacade.resolve_from_message(data, binding)
    route_source = route_info['source']

    if SessionFacade.RouteSource.is_parent_not_found(route_source):
        run_in_background(_send_notice_message,
                           (chat_id, _SESSION_NOT_FOUND_HINT + "请重新发起 /new 指令。", message_id))
        return

    if not SessionFacade.RouteSource.is_resolved(route_source):
        run_in_background(_send_notice_message,
                           (chat_id, "无法找到对应的会话，请重新发起 /new 指令", message_id))
        return

    session_id = route_info['session_id']
    session_project_dir = route_info.get('project_dir', '')

    # 刷新群聊活跃时间（供自动解散空闲判断）
    if route_source == SessionFacade.RouteSource.GROUP_CHAT:
        from stores.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        owner_id = binding.get('_owner_id', '') if binding else ''
        if gs_store and owner_id:
            gs_store.touch(owner_id, chat_id)

    logger.info("[feishu] /reply command: session=%s, agent=%s, cmd=%s, prompt=%s",
                session_id, agent_type or '(default)', command or '(default)', _sanitize_user_content(prompt))

    # 转发到 Callback 后端
    # /clear 后首条消息：new_session 标志表示需要启动新 Agent 进程
    if route_info.get('new_session'):
        run_in_background(_forward_new_request,
                           (binding, session_id, session_project_dir, prompt,
                            chat_id, message_id, chat_type, command, agent_type))
    else:
        run_in_background(_forward_continue_request, (binding, session_id, session_project_dir, prompt, chat_id, message_id, command, agent_type))


def _handle_users_command(data: dict, args: str):
    """处理 /users 指令，查看已注册用户和在线状态

    Args:
        data: 飞书事件数据
        args: 参数部分（不含 /users，当前未使用）
    """
    from config import FEISHU_OWNER_ID as gateway_owner_id
    from stores.binding_store import BindingStore
    from services.ws_registry import WebSocketRegistry

    event = data.get('event', {})
    message = event.get('message', {})
    message_id = message.get('message_id', '')
    chat_id = message.get('chat_id', '')

    # 获取数据
    binding_store = BindingStore.get_instance()
    ws_registry = WebSocketRegistry.get_instance()

    bindings = binding_store.get_all() if binding_store else {}
    ws_status = ws_registry.get_status() if ws_registry else {}

    # 构建并发送卡片
    card = _build_user_status_card(bindings, ws_status, gateway_owner_id)
    run_in_background(_send_users_status_card, (chat_id, card, message_id))


def _handle_groups_command(data: dict, args: str) -> None:
    """处理 /groups 命令：列出或解散群聊

    用法：
        /groups                       - 列出活跃群聊
        /groups dissolve 1 2          - 按序号解散
        /groups dissolve all          - 解散所有群聊
        /groups dissolve idle <天数>   - 解散空闲超过 N 天的群聊
        /groups dissolve /path        - 解散指定目录的群聊（精准匹配）
        /groups dissolve /path/**     - 解散指定目录及子目录的群聊
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    binding = _get_binding_from_event(event)

    args = args.strip()

    if args == 'dissolve' or args.startswith('dissolve '):
        dissolve_args = args[len('dissolve'):].strip()
        if dissolve_args == 'all':
            payload = {'all': True}
        elif dissolve_args == 'idle' or dissolve_args.startswith('idle '):
            days_arg = dissolve_args[len('idle'):].strip()
            try:
                idle_days = int(days_arg)
                if idle_days <= 0:
                    raise ValueError
            except ValueError:
                run_in_background(_send_notice_message,
                                   (chat_id, "请指定正整数天数，示例：`/groups dissolve idle 7`", message_id))
                return
            payload = {'idle_days': idle_days}
        elif dissolve_args and dissolve_args.startswith('/'):
            # 目录模式：/path 精准匹配，/path/** 递归匹配子目录
            if dissolve_args.endswith('/**'):
                payload = {'dir': dissolve_args[:-3] or '/', 'recursive': True}
            else:
                payload = {'dir': dissolve_args, 'recursive': False}
        elif dissolve_args:
            try:
                seqs = [int(x) for x in dissolve_args.split()]
            except ValueError:
                run_in_background(_send_notice_message,
                                   (chat_id, "格式错误，示例：`/groups dissolve 1 2 3` 或 `/groups dissolve all`", message_id))
                return
            payload = {'seqs': seqs}
        else:
            run_in_background(_send_notice_message,
                               (chat_id, "请指定要解散的群聊序号，示例：`/groups dissolve 1 2 3` 或 `/groups dissolve all`", message_id))
            return

        run_in_background(_dissolve_groups, (binding, payload, chat_id, message_id))
    else:
        run_in_background(_send_groups_card, (binding, chat_id, message_id))


def _dissolve_groups(binding: Dict[str, Any], payload: dict,
                     chat_id: str, message_id: str) -> None:
    """解散当前 owner 的指定群聊（网关主导）

    执行顺序：
        1. gateway 本地定位目标 chat_ids（按 seqs 或 all）
        2. 调 batch_dissolve_groups（先通知 callback 标记 dissolved，再飞书 API 解散 + 清 GroupChatStore）
        3. 清 GroupSessionStore 对应条目
    """
    from stores.group_chat_store import GroupChatStore
    from stores.group_session_store import GroupSessionStore

    owner_id = binding.get('_owner_id', '')
    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()

    if not group_store or not gs_store:
        _send_notice_message(chat_id, "存储服务未就绪，请稍后重试", message_id)
        return

    # 1) 根据 payload 解出目标 chat_ids（本地查询，零 RPC）
    owner_chats = group_store.get_chats_by_owner(owner_id) if owner_id else []
    seq_to_chat = {item['seq']: item['chat_id'] for item in owner_chats}

    target_chat_ids: List[str] = []
    if payload.get('all'):
        target_chat_ids = [item['chat_id'] for item in owner_chats]
    elif payload.get('idle_days'):
        # idle 模式：按空闲天数筛选群聊（复用上面已加载的 owner_chats，避免重复读盘）
        target_chat_ids = find_idle_group_chats(owner_id, owner_chats=owner_chats,
                                                now=int(time.time()), idle_days=payload['idle_days'])
    elif payload.get('dir') is not None:
        # 目录模式：按 project_dir 匹配群聊
        # 用 normpath 而非 realpath：project_dir 来自 callback/agent 机器，
        # gateway 上解析 symlink 可能与源机器不一致，纯字符串规范化更可靠
        target_dir = os.path.normpath(payload['dir'])
        recursive = payload.get('recursive', False)
        owner_chat_ids = {item['chat_id'] for item in owner_chats}
        dir_prefix = '/' if target_dir == '/' else target_dir + '/'
        gs_data = gs_store.get_by_owner(owner_id) if owner_id else {}
        for cid, gs_item in gs_data.items():
            if cid not in owner_chat_ids:
                continue
            project_dir = gs_item.get('project_dir', '')
            if not project_dir:
                continue
            project_dir = os.path.normpath(project_dir)
            if project_dir == target_dir or (recursive and project_dir.startswith(dir_prefix)):
                target_chat_ids.append(cid)
    else:
        # seqs 由 _handle_groups_command 上游 int() 校验，此处防御性跳过非法值
        seqs = payload.get('seqs') or []
        for seq in seqs:
            try:
                cid = seq_to_chat.get(int(seq))
            except (TypeError, ValueError):
                continue
            if cid:
                target_chat_ids.append(cid)

    if not target_chat_ids:
        _send_notice_message(chat_id, "没有找到可解散的群聊", message_id)
        return

    # 2) 调 batch_dissolve_groups（先通知 callback 标记 dissolved，再飞书 API 解散 + 清 GroupChatStore）
    result = batch_dissolve_groups(binding, target_chat_ids)
    dissolved_items = result.get('dissolved_items', [])
    failed = result.get('failed', [])
    # skipped_items 理论上不会出现（我们已按 owner 过滤），但兜底收敛
    skipped_items = result.get('skipped_items', [])

    # 3) 清 GroupSessionStore 对应条目
    if dissolved_items and owner_id:
        for cid in dissolved_items:
            gs_store.remove(owner_id, cid)

    # 4) 组合用户反馈
    parts = []
    if dissolved_items:
        parts.append("已解散 %d 个群聊" % len(dissolved_items))
    if failed:
        parts.append("%d 个解散失败（见服务日志）" % len(failed))
    if skipped_items:
        parts.append("%d 个外部群聊已跳过" % len(skipped_items))
    msg = "，".join(parts) if parts else "没有找到可解散的群聊"

    _send_notice_message(chat_id, msg, message_id)


def _handle_attach_command(data: dict, args: str) -> None:
    """处理 /attach <session_id_prefix> 命令：将 session 绑定到当前群聊

    仅支持在群聊中使用。session_id 前缀至少 8 字符，唯一匹配时执行绑定。
    """
    MIN_PREFIX_LEN = 8

    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    chat_type = message.get('chat_type', '')

    if chat_type != 'group':
        run_in_background(_send_notice_message,
                           (chat_id, "`/attach` 仅支持在群聊中使用", message_id))
        return

    prefix = args.strip()
    if len(prefix) < MIN_PREFIX_LEN:
        run_in_background(_send_notice_message,
                           (chat_id, f"用法：`/attach <session_id 前缀>`（至少 {MIN_PREFIX_LEN} 字符）",
                            message_id))
        return

    binding = _get_binding_from_event(event)
    run_in_background(_forward_attach_request, (binding, prefix, chat_id, message_id))


def _handle_clear_command(data: dict, args: str) -> None:
    """处理 /clear 命令：清空当前群聊会话，预创建新 session

    仅支持 group 模式的群聊中使用。解绑旧 session 并预创建新 session（继承
    project_dir + command），下次发送消息自动启动新 Agent 进程。
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')
    chat_type = message.get('chat_type', '')

    if chat_type != 'group':
        run_in_background(_send_notice_message,
                           (chat_id, "`/clear` 仅支持在群聊中使用", message_id))
        return

    binding = _get_binding_from_event(event)
    if not binding:
        run_in_background(_send_notice_message,
                           (chat_id, "您尚未注册，无法使用此功能", message_id))
        return

    session_mode = binding.get('session_mode', 'message')
    if session_mode != 'group':
        run_in_background(_send_notice_message,
                           (chat_id, "`/clear` 仅在群聊模式下可用", message_id))
        return

    from stores.group_session_store import GroupSessionStore
    gs_store = GroupSessionStore.get_instance()
    owner_id = binding.get('_owner_id', '')

    if not gs_store or not owner_id:
        run_in_background(_send_notice_message,
                           (chat_id, "存储服务未就绪，请稍后重试", message_id))
        return

    current = gs_store.get(owner_id, chat_id)
    if not current:
        run_in_background(_send_notice_message,
                           (chat_id, "当前群聊没有活跃的会话", message_id))
        return

    # 幂等：上次 /clear 后还没发消息，不重复 clone
    if current.get('new_session'):
        run_in_background(_send_notice_message,
                           (chat_id, "会话上下文已清空，下次发送消息将自动创建新会话。",
                            message_id))
        return

    old_session_id = current.get('session_id', '')
    new_session_id = str(uuid.uuid4())

    # 1. callback 侧创建新 session（继承旧 session 属性）
    from services.session_facade import SessionFacade
    resp = SessionFacade.clone(binding, old_session_id, new_session_id, chat_id)

    if not resp or not resp.get('ok'):
        logger.error("[feishu] /clear clone returned error: %s", resp)
        run_in_background(_send_notice_message,
                           (chat_id, "清空会话失败，请稍后重试", message_id))
        return

    project_dir = resp.get('project_dir', '')

    # 2. 替换网关侧路由映射（新 session + new_session 标志）
    gs_store.save(owner_id, chat_id, new_session_id,
                  project_dir=project_dir, new_session=True)

    logger.info("[feishu] /clear: owner=%s chat=%s old=%s new=%s",
                owner_id, chat_id, old_session_id[:8], new_session_id[:8])
    run_in_background(_send_notice_message,
                       (chat_id, "会话上下文已清空，下次发送消息将自动创建新会话。",
                        message_id))
