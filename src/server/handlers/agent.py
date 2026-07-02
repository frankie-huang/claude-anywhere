"""Agent 会话处理器

处理用户通过飞书回复消息继续会话的请求，
以及通过 /new 指令发起新会话。支持 Claude 和 Codex 两种 agent。

agent 协议层（命令构建、进程启动/监控）已迁移至 agents/ 模块，
本文件仅保留 HTTP 业务逻辑（参数校验、session store 操作、飞书通知）。
"""

import logging
import os
import signal
import threading
import time
import uuid
from typing import Callable, Optional, Tuple, Dict, Any

from agents import AgentAdapter, launch_agent, get_agent_adapter
from stores.session_chat_store import SessionChatStore
from utils.concurrency import run_in_background

logger = logging.getLogger(__name__)

# 通知消息最大长度
MAX_ERROR_NOTIFICATION_LENGTH = 500
MAX_COMPLETE_OUTPUT_LENGTH = 10000  # 完成通知输出最大长度（/compact、/context 等）

# 指令队列最大容量
_QUEUE_MAX_SIZE = 5

# 启动锁：防止同一 session 并发启动多个 Agent 进程（TOCTOU 竞态保护）
# 锁序约束：_launching_lock → SessionChatStore._file_lock，不可反向获取
_launching_lock = threading.Lock()
_launching_sessions = set()


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


def handle_continue_session(data: Dict[str, Any],
                            from_queue: bool = False) -> Tuple[bool, Dict[str, Any]]:
    """处理继续会话的请求

    同步等待一小段时间判断命令是否能正常启动，然后返回结果。

    Args:
        data: 请求数据
            - session_id: 会话 ID (必需)
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 飞书聊天 ID（网关调用时必传）
            - message_id: 用户消息 ID (可选，用于回复式通知)
            - sender_id: 发送者 user_id (可选，注入子进程 env 后由 stop 卡片优先 at)
            - command: 指定使用的命令 (可选)
            - agent_type: agent 类型，如 'claude'/'codex' (可选，未传时从 session store 读取)
        from_queue: 队列触发时为 True，跳过队列排队检查

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
    sender_id = data.get('sender_id', '') or ''
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

    # ── 忙碌检查：会话有运行中的进程、正在启动、或队列有排队指令时，入队 ──
    with _launching_lock:
        busy, has_pending = session_store.get_session_queue_status(session_id)
        if session_id in _launching_sessions or busy or (not from_queue and has_pending):
            ok, position = session_store.enqueue_prompt(session_id, data,
                                                        max_size=_QUEUE_MAX_SIZE)
            if not ok:
                return Response.error(
                    '指令队列已满（最多 %d 条），请稍后重试' % _QUEUE_MAX_SIZE,
                    session_id=session_id, agent_type=adapter.agent_type)
            logger.info("[%s-continue] Prompt queued at position %d: %s",
                        adapter.agent_type, position, session_id)
            return True, {'status': 'queued', 'queue_position': position,
                          'session_id': session_id, 'agent_type': adapter.agent_type}
        _launching_sessions.add(session_id)

    try:
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

        # ── 包装回调：完成后清 PID + 执行队列下一条 ──
        wrapped_complete = _wrap_with_queue_drain(on_complete)
        wrapped_error = _wrap_with_queue_drain(_send_error_notification)

        # 通过 agent 适配层启动进程
        success, pid, response = launch_agent(
            adapter, session_id, project_dir, prompt,
            chat_id, message_id, sender_id=sender_id,
            session_mode='resume',
            command_name=actual_cmd,
            on_complete=wrapped_complete,
            on_error=wrapped_error)

        # 启动成功且进程仍在运行时，记录 PID
        if success and pid:
            session_store.set_running_pid(session_id, pid)

        return success, response
    finally:
        with _launching_lock:
            _launching_sessions.discard(session_id)
        # NOTE: pid=0 快速完成时 callback 也会 drain，形成双重 drain。
        # 后者被 _launching_sessions 拦截（同步回调）或各 dequeue 一条后
        # 第二条被 _launching_sessions 拦截并 re-enqueue（异步回调）。
        # 代价仅 FIFO 偶尔翻转，可接受；无法从外部区分 callback 是否已调度。
        _process_next_in_queue(session_id)


def handle_new_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """处理新建会话的请求
    NOTE: 无 _launching_sessions 保护，因为 /new 每次生成新 uuid，
    并发 /new 同 session_id 概率极低，不值得增加复杂度。

    Args:
        data: 请求数据
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 飞书聊天 ID，通常非空（来自飞书事件，群聊或 P2P）；
                仅 group 模式下从 P2P 发起 /new 时为空，
                由本函数调 do_ensure_chat 建群后回填
            - message_id: 原始消息 ID (可选，用于飞书网关回复用户消息)
            - sender_id: 发送者 user_id (可选，注入子进程 env 后由 stop 卡片优先 at)
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
    sender_id = data.get('sender_id', '') or ''
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
        # message_id 来自 P2P /new，与新群跨 chat，清空避免被 hook 用作 reply_to
        # 清空后 hook fallback 查 last_message_id（由 hook 同步的 prompt 消息回写）
        message_id = ''
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

    # ── 包装回调：完成后清 PID + 执行队列下一条 ──
    wrapped_complete = _wrap_with_queue_drain(on_complete)
    wrapped_error = _wrap_with_queue_drain(_send_error_notification)

    # 通过 agent 适配层启动进程
    success, pid, response = launch_agent(
        adapter, session_id, project_dir, prompt,
        chat_id, message_id, sender_id=sender_id,
        session_mode='new',
        command_name=actual_cmd,
        on_complete=wrapped_complete,
        on_error=wrapped_error)

    # 启动成功且进程仍在运行时，记录 PID
    if success and pid:
        session_store.set_running_pid(session_id, pid)

    return success, response


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
    from handlers.outbound import remove_feishu_typing, reply_feishu_text

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
    from handlers.outbound import remove_feishu_typing, reply_feishu_text, reply_feishu_markdown

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
    from handlers.outbound import reply_feishu_text

    sid_tag = session_id[:8]
    text = f"已自动解除 session `{sid_tag}` 的静音。"
    success, result = reply_feishu_text(chat_id, message_id, text)
    if success:
        logger.info("Sent unmute notification to %s", chat_id)
    else:
        logger.error("Failed to send unmute notification: %s", result)


# =============================================
# 指令队列
# =============================================


def _wrap_with_queue_drain(original_callback: Optional[Callable]) -> Callable:
    """包装回调：完成后清 PID + 检查 stopped 标志 + 执行队列下一条"""

    def wrapper(agent_type: str, chat_id: str, message_id: str,
                session_id: str, output_or_error: str):
        store = SessionChatStore.get_instance()

        # /stop 触发的终止：跳过原始回调（不发错误通知），只做清理，不 drain 队列
        stopped = store and store.check_and_clear_stopped_flag(session_id)
        if stopped:
            logger.info("[queue] Stopped flag detected, skipping notification: %s",
                        session_id)
            store.check_and_clear_skip_user_prompt(session_id)
            from handlers.outbound import remove_feishu_typing  # 延迟导入避免循环依赖
            remove_feishu_typing(message_id)
        elif original_callback:
            original_callback(agent_type, chat_id, message_id, session_id, output_or_error)

        # 清除 running_pid
        if store:
            store.clear_running_pid(session_id)

        # stopped 时不 drain 队列（/stop 已清空队列）
        if not stopped:
            _process_next_in_queue(session_id)

    return wrapper


def _process_next_in_queue(session_id: str):
    """从队列弹出下一条指令并执行

    前置检查防止误 drain：
    - _launching_sessions：启动流程未结束时跳过
    - is_session_busy：进程仍在运行时跳过
    """
    with _launching_lock:
        if session_id in _launching_sessions:
            return
    store = SessionChatStore.get_instance()
    if not store:
        return
    if store.is_session_busy(session_id):
        return
    next_item = store.dequeue_prompt(session_id)
    if next_item is None:
        return
    logger.info("[queue] Processing next queued prompt: %s", session_id)
    run_in_background(_execute_queued_prompt, (session_id, next_item))


def _execute_queued_prompt(session_id: str, data: Dict[str, Any]):
    """执行队列中的指令，失败时通知用户并继续 drain

    handle_continue_session 在参数校验阶段失败时（session 过期、目录不存在等），
    会在进入 try/finally 之前提前 return，此时 finally 不会执行，队列不会继续 drain。
    本函数捕获这类失败，发送错误通知并手动触发队列 drain。
    """
    store = SessionChatStore.get_instance()
    if not store:
        logger.warning("[queue] SessionChatStore not initialized, aborting queued prompt: %s",
                       session_id)
        return

    if store.check_and_clear_stopped_flag(session_id):
        logger.info("[queue] Stopped flag detected, discarding queued prompt: %s",
                    session_id)
        return

    # 给用户原始消息加 Typing（进度反馈）
    # last_message_id 不必再写：reply_to 由 launch_agent 注入的 CODE_ANYWHERE_MESSAGE_ID 提供
    message_id = data.get('message_id', '') or ''
    if message_id:
        from handlers.outbound import add_feishu_typing
        add_feishu_typing(message_id)

    success, response = handle_continue_session(data, from_queue=True)
    if not success:
        chat_id = data.get('chat_id', '') or ''
        agent_type = data.get('agent_type', '') or ''
        error_msg = response.get('error', '排队指令执行失败')
        if chat_id:
            _send_error_notification(
                agent_type, chat_id, message_id, session_id, error_msg)
        _process_next_in_queue(session_id)


# =============================================
# /stop 处理
# =============================================


def handle_stop_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """停止会话中正在运行的 Agent 进程并清空队列

    Args:
        data: 请求数据
            - session_id: 会话 ID (必需)

    Returns:
        (success, response):
            - stopped: 是否终止了进程
            - queue_cleared: 清空的排队指令数量
    """
    session_id = data.get('session_id', '')
    if not session_id:
        return False, {'error': 'Missing session_id'}

    session_store = SessionChatStore.get_instance()
    if not session_store:
        return False, {'error': 'Session store not initialized'}

    # 无论有无运行进程，都先设置 stopped 标志：
    # 防止并发的 wrapper drain 在 clear_pending_prompts 之前 dequeue 出指令后仍然执行
    session_store.set_stopped_flag(session_id)

    pid = session_store.get_running_pid(session_id)
    stopped = False
    if pid:
        stopped = _kill_process(pid)
        session_store.clear_running_pid(session_id)
        logger.info("[stop] Terminated process PID=%d for session %s: %s",
                    pid, session_id, 'success' if stopped else 'already dead')

    queue_cleared = session_store.clear_pending_prompts(session_id)

    return True, {'stopped': stopped, 'queue_cleared': queue_cleared,
                  'session_id': session_id}


def _kill_process(pid: int) -> bool:
    """终止进程组：SIGTERM → 等 5s → SIGKILL

    用 os.killpg（依赖 Popen 的 start_new_session=True）杀整个进程组，
    否则 SIGTERM 只杀 wrapper shell，agent 子进程变孤儿。

    NOTE: 同步阻塞最多 5s，在飞书 10s 网关超时内，且提供即时反馈。
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.killpg(pid, 0)
        except OSError:
            return True
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except OSError:
        return True
