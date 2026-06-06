"""Agent 会话处理器

处理用户通过飞书回复消息继续会话的请求，
以及通过 /new 指令发起新会话。支持 Claude 和 Codex 两种 agent。

agent 协议层（命令构建、进程启动/监控）已迁移至 agents/ 模块，
本文件仅保留 HTTP 业务逻辑（参数校验、session store 操作、飞书通知）。
"""

import logging
import os
import uuid
from typing import Callable, Optional, Tuple, Dict, Any

from agents import AgentAdapter, launch_agent, get_agent_adapter
from services.session_chat_store import SessionChatStore

logger = logging.getLogger(__name__)

# 通知消息最大长度
MAX_ERROR_NOTIFICATION_LENGTH = 500
MAX_COMPLETE_OUTPUT_LENGTH = 10000  # 完成通知输出最大长度（/compact、/context 等）


class Response:
    """统一的响应格式"""

    @staticmethod
    def error(msg: str, **extra: Any) -> Tuple[bool, Dict[str, Any]]:
        """错误响应"""
        response = {'error': msg}
        response.update(extra)
        return False, response

    @staticmethod
    def processing() -> Tuple[bool, Dict[str, Any]]:
        """处理中响应"""
        return True, {'status': 'processing'}

    @staticmethod
    def completed(output: str = '') -> Tuple[bool, Dict[str, Any]]:
        """完成响应"""
        return True, {'status': 'completed', 'output': output}

    @staticmethod
    def is_processing(result: Tuple[bool, Dict[str, Any]]) -> bool:
        """判断响应是否为 processing 状态

        Args:
            result: (success, response) 元组

        Returns:
            True 表示成功且状态为 processing
        """
        return result[0] and result[1].get('status') == 'processing'


# =============================================
# 公开接口
# =============================================


def handle_continue_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """处理继续会话的请求

    同步等待一小段时间判断命令是否能正常启动，然后返回结果。

    Args:
        data: 请求数据
            - session_id: 会话 ID (必需)
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 飞书聊天 ID（网关调用时必传）
            - message_id: 用户消息 ID (可选，用于回复式通知)
            - command: 指定使用的命令 (可选)
            - agent_type: agent 类型，如 'claude'/'codex' (可选，未传时从 session store 读取)

    Returns:
        (success, response):
            - success=True, status='processing': 命令正在执行
            - success=True, status='completed': 命令快速完成
            - success=False, error=...: 命令启动/执行失败
    """
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    prompt = data.get('prompt', '')
    chat_id = data.get('chat_id', '') or ''  # 确保 None 转为空字符串
    message_id = data.get('message_id', '') or ''
    command = data.get('command', '') or ''

    # 参数验证
    if not session_id:
        return Response.error('Session not registered or has expired')
    if not project_dir:
        return Response.error('Missing project_dir')
    if not prompt:
        return Response.error('Missing prompt')

    session_store = SessionChatStore.get_instance()
    if not session_store:
        return Response.error('Session store not initialized')

    # 校验 session 是否在 store 中有物理记录（含 dissolved，dissolved 由下方 save 自动复活）
    # 同时缓存 session 数据，避免后续 get_command 重复读盘
    session_data = session_store.get_session(session_id, include_dissolved=True)
    if not session_data:
        return Response.error('Session expired or not found, please /new')

    # 验证项目目录存在
    if not os.path.exists(project_dir):
        return Response.error(f'Project directory not found: {project_dir}')

    # 从 session 记录获取 agent_type，确保 reply 延续创建时的 agent
    agent_type = session_data.get('agent_type', '') or None
    try:
        adapter = get_agent_adapter(agent_type)
    except ValueError as e:
        return Response.error(str(e), agent_type=agent_type or '')

    # 验证 command 合法性（如果指定了的话）
    if command and command not in adapter.get_commands():
        available = ', '.join(adapter.get_commands())
        return Response.error(
            '无效的命令 "%s"，%s 可用命令: %s' % (command, adapter.display_name, available),
            agent_type=adapter.agent_type)

    # 斜杠命令检测：从 prompt 解析，匹配 adapter 声明的命令时走框架路径
    on_complete = _resolve_slash_command_callback(adapter, prompt)

    # Command 优先级: 请求指定 > session 记录 > 默认
    if not command:
        command = session_data.get('command', '')

    actual_cmd = adapter.resolve_command(command)
    logger.info("[%s-continue] Session: %s, Dir: %s, Cmd: %s, Prompt: %s...",
                adapter.agent_type, session_id, project_dir, actual_cmd, prompt[:50])

    # 更新 session 映射：刷新 command 和 chat_id
    # chat_id 可能变化（如用户在不同聊天中通过默认工作目录继续同一 session）
    # chat_id 来自飞书消息事件（P2P / 群聊均必定非空），非空 chat_id 自动清除 dissolved
    session_store.save(session_id, chat_id, command=actual_cmd,
                       agent_type=adapter.agent_type)
    # 用户发送了新消息，自动解除静音
    if session_store.unmute_session(session_id) is True and chat_id:
        _send_unmute_notification(chat_id, session_id, message_id)
    # 飞书发起的 prompt 已在飞书展示，标记跳过
    session_store.set_skip_next_user_prompt(session_id)

    # 通过 agent 适配层启动进程
    result = launch_agent(
        adapter, session_id, project_dir, prompt,
        chat_id, message_id, session_mode='resume',
        command_name=actual_cmd,
        on_complete=on_complete,
        on_error=_send_error_notification)
    return result


def handle_new_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """处理新建会话的请求

    Args:
        data: 请求数据
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 飞书聊天 ID，通常非空（来自飞书事件，群聊或 P2P）；
                仅 group 模式下从 P2P 发起 /new 时为空，
                由本函数调 do_ensure_chat 建群后回填
            - message_id: 原始消息 ID (可选，用于飞书网关回复用户消息)
            - command: 指定使用的命令 (可选)
            - agent_type: agent 类型，如 'claude'/'codex' (可选，未传时从 session store 或默认值获取)
            - skip_user_prompt: 是否跳过首条 UserPromptSubmit 通知 (默认 True)；
                group 模式 P2P 建群分支需置 False，让 hook 把首条 prompt 补发到新群

    Returns:
        (success, response):
            - success=True, status='processing': 命令正在执行
            - success=True, status='completed': 命令快速完成
            - success=False, error=...: 命令启动/执行失败
    """
    session_store = SessionChatStore.get_instance()
    if not session_store:
        return Response.error('Session store not initialized')

    project_dir = data.get('project_dir', '') or ''
    prompt = data.get('prompt', '') or ''
    chat_id = data.get('chat_id', '') or ''
    message_id = data.get('message_id', '') or ''
    command = data.get('command', '') or ''
    agent_type = data.get('agent_type', '') or ''
    # session_id：优先使用调用方传入的（网关侧生成），否则自行生成
    session_id = data.get('session_id', '') or str(uuid.uuid4())

    # 参数验证
    if not project_dir:
        return Response.error('Missing project_dir')
    if not prompt:
        return Response.error('Missing prompt')
    # 验证项目目录存在
    if not os.path.exists(project_dir):
        return Response.error(f'Project directory not found: {project_dir}')

    # agent_type / command 优先级：网关传入 > store 已有值（/clear clone 继承）> 默认
    if not agent_type or not command:
        session_data = session_store.get_session(session_id)
        if session_data:
            if not agent_type:
                agent_type = session_data.get('agent_type', '')
            if not command:
                command = session_data.get('command', '')
    try:
        adapter = get_agent_adapter(agent_type or None)
    except ValueError as e:
        return Response.error(str(e), agent_type=agent_type or '')

    # 验证 command 合法性（如果指定了的话）
    if command and command not in adapter.get_commands():
        available = ', '.join(adapter.get_commands())
        return Response.error(
            '无效的命令 "%s"，%s 可用命令: %s' % (command, adapter.display_name, available),
            agent_type=adapter.agent_type)

    # 斜杠命令检测：从 prompt 解析，匹配 adapter 声明的命令时走框架路径
    on_complete = _resolve_slash_command_callback(adapter, prompt)

    actual_cmd = adapter.resolve_command(command)
    logger.info("[%s-new] Session: %s, Dir: %s, Cmd: %s, Prompt: %s...",
                adapter.agent_type, session_id, project_dir, actual_cmd, prompt[:50])

    from config import FEISHU_SESSION_MODE
    is_group_mode = (FEISHU_SESSION_MODE == 'group')
    if is_group_mode and not chat_id:
        # group 模式无 chat_id → 委托给 do_ensure_chat 创建群聊并绑定
        from handlers.callback import do_ensure_chat
        ok, ensure_result = do_ensure_chat(adapter.agent_type, session_id, project_dir)
        if not ok:
            logger.warning("[%s-new] ensure-chat failed for %s: %s",
                           adapter.agent_type, session_id, ensure_result)
            return Response.error(f'Failed to create group chat: {ensure_result}',
                                  agent_type=adapter.agent_type)
        chat_id = ensure_result
        logger.info("[%s-new] ensure-chat created group for %s: %s",
                    adapter.agent_type, session_id, chat_id)

    # 写入 command 等业务属性（与 do_ensure_chat 的"建群"职责解耦：
    # group 分支补写、非 group 分支首次写，统一一处）
    session_store.save(session_id, chat_id, command=actual_cmd,
                       project_dir=project_dir, agent_type=adapter.agent_type)
    logger.info("[%s-new] Saved mapping: %s -> %s",
                adapter.agent_type, session_id, chat_id)

    # 防御性 unmute：新会话通常不会有 mute 状态，此处幂等调用，确保不会意外静音
    if session_store.unmute_session(session_id) is True and chat_id:
        _send_unmute_notification(chat_id, session_id, message_id)

    # 设置 skip_user_prompt 标志
    # 由调用方通过 skip_user_prompt 字段决定，不再根据 chat_id 是否为空判断
    skip_user_prompt = data.get('skip_user_prompt', True)
    if skip_user_prompt:
        session_store.set_skip_next_user_prompt(session_id)

    # 通过 agent 适配层启动进程
    result = launch_agent(
        adapter, session_id, project_dir, prompt,
        chat_id, message_id, session_mode='new',
        command_name=actual_cmd,
        on_complete=on_complete,
        on_error=_send_error_notification)
    return result


# =============================================
# 斜杠命令辅助
# =============================================


def _resolve_slash_command_callback(adapter: AgentAdapter, prompt: str) -> Optional[Callable]:
    """从 prompt 解析斜杠命令，返回 on_complete 回调或 None

    注意：本函数仅决定通知策略，不影响 prompt 透传。无论是否匹配，
    prompt 都会原样发送给 agent 进程，命令执行由 agent CLI 自行处理。

    如果 prompt 以 / 开头且命令名匹配 adapter 声明的斜杠命令：
    - triggers_stop_hook=False → 返回 _send_complete_notification（框架手动通知）
    - triggers_stop_hook=True  → 返回 None（stop hook 自动通知）
    不匹配时返回 None，prompt 作为普通消息透传给 agent。
    """
    if not prompt.startswith('/'):
        return None
    parts = prompt[1:].split(None, 1)
    if not parts:
        return None
    cmd_info = adapter.get_slash_commands().get(parts[0])
    if cmd_info is None:
        return None
    if not cmd_info.triggers_stop_hook:
        return _send_complete_notification
    return None


# =============================================
# 飞书通知
# =============================================


def _send_error_notification(agent_type: str, chat_id: str, message_id: str,
                             session_id: str, error_msg: str):
    """发送错误通知到飞书

    截断过长的错误消息，防止飞书消息超限。
    同时清理 session 状态（skip_next_user_prompt、last_message_id）。

    Args:
        agent_type: agent 类型标识（用于展示正确产品名）
        chat_id: 群聊 ID
        message_id: 要回复的消息 ID（为空时降级为普通消息）
        session_id: 会话 ID（用于更新 session 状态）
        error_msg: 错误消息
    """
    from handlers.utils import remove_feishu_typing, reply_feishu_text

    try:
        adapter = get_agent_adapter(agent_type or None)
    except ValueError:
        logger.warning("Unknown agent_type for error notification: %s", agent_type)
        adapter = get_agent_adapter()

    # 移除原消息上的 Typing 表情
    remove_feishu_typing(message_id)

    truncated = error_msg[:MAX_ERROR_NOTIFICATION_LENGTH] if len(error_msg) > MAX_ERROR_NOTIFICATION_LENGTH else error_msg
    text = f"❌ {adapter.display_name} 执行异常:\n{truncated}"
    success, sent_id = reply_feishu_text(chat_id, message_id, text)
    if success:
        logger.info("[%s] Sent error notification to %s", adapter.agent_type, chat_id)
    else:
        logger.error("[%s] Failed to send error notification: %s", adapter.agent_type, sent_id)

    # 更新 session 状态（无论通知是否发送成功都需清理，避免状态残留）
    session_store = SessionChatStore.get_instance()
    if session_store and session_id:
        # 清除残留的 skip_next_user_prompt（进程失败不触发 stop hook，无法自然消费）
        session_store.check_and_clear_skip_user_prompt(session_id)
        # 更新 last_message_id
        if success and sent_id:
            session_store.set_last_message_id(session_id, sent_id)
            logger.info("[%s] Updated last_message_id: %s -> %s",
                        adapter.agent_type, session_id, sent_id)


def _send_complete_notification(agent_type: str, chat_id: str, message_id: str,
                                session_id: str, output: str):
    """发送透传命令完成通知到飞书

    用于无 stop hook 的透传命令（如 /compact），进程成功完成时通知用户。
    负责：移除 Typing 表情、发送完成文案、更新 last_message_id。

    Args:
        agent_type: agent 类型标识
        chat_id: 群聊 ID
        message_id: 要回复的消息 ID（用户发送的消息，也是 Typing 所在消息）
        session_id: 会话 ID（用于更新 last_message_id）
        output: 命令输出内容（有内容时直接展示，为空时发送通用完成文案）
    """
    from handlers.utils import remove_feishu_typing, reply_feishu_text, reply_feishu_markdown

    try:
        adapter = get_agent_adapter(agent_type or None)
    except ValueError:
        adapter = get_agent_adapter()

    log_prefix = '[%s]' % adapter.agent_type

    # 移除原消息上的 Typing 表情
    remove_feishu_typing(message_id)

    # 回复完成文案：有输出时用卡片展示 markdown，无输出时发送纯文本完成提示
    output = output.strip() if output else ''
    if output and len(output) > MAX_COMPLETE_OUTPUT_LENGTH:
        output = output[:MAX_COMPLETE_OUTPUT_LENGTH] + '\n\n...(内容过长，已截断)'
    if output:
        success, sent_id = reply_feishu_markdown(chat_id, message_id, output)
        if not success:
            # 卡片发送失败，降级为纯文本
            logger.warning("%s Markdown card failed, fallback to text: %s", log_prefix, sent_id)
            success, sent_id = reply_feishu_text(chat_id, message_id, output)
    else:
        text = f"✅ {adapter.display_name} 指令已完成"
        success, sent_id = reply_feishu_text(chat_id, message_id, text)
    if success:
        logger.info("%s Sent complete notification to %s", log_prefix, chat_id)
    else:
        logger.error("%s Failed to send complete notification: %s", log_prefix, sent_id)

    # 更新 session 状态（无论通知是否发送成功都需清理，避免状态残留）
    session_store = SessionChatStore.get_instance()
    if session_store and session_id:
        # 清除残留的 skip_next_user_prompt（透传命令不触发 stop hook，无法自然消费）
        session_store.check_and_clear_skip_user_prompt(session_id)
        # 更新 last_message_id
        if success and sent_id:
            session_store.set_last_message_id(session_id, sent_id)
            logger.info("%s Updated last_message_id: %s -> %s",
                        log_prefix, session_id, sent_id)


def _send_unmute_notification(chat_id: str, session_id: str, message_id: str = ''):
    """发送自动解除静音通知到飞书

    Args:
        chat_id: 群聊 ID
        session_id: 会话 ID
        message_id: 要回复的消息 ID（可选）
    """
    from handlers.utils import reply_feishu_text

    sid_tag = session_id[:8]
    text = f"已自动解除 session `{sid_tag}` 的静音。"
    success, result = reply_feishu_text(chat_id, message_id, text)
    if success:
        logger.info("Sent unmute notification to %s", chat_id)
    else:
        logger.error("Failed to send unmute notification: %s", result)
