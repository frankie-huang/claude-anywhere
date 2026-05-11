"""Agent 会话处理器

处理用户通过飞书回复消息继续会话的请求，
以及通过 /new 指令发起新会话。支持 Claude 和 Codex 两种 agent。

agent 协议层（命令构建、进程启动/监控）已迁移至 agents/ 模块，
本文件仅保留 HTTP 业务逻辑（参数校验、session store 操作、飞书通知）。
"""

import logging
import os
import uuid
from typing import Tuple, Dict, Any

from agents import launch_agent, get_agent_adapter
from services.session_chat_store import SessionChatStore

logger = logging.getLogger(__name__)

# 通知消息最大长度
MAX_NOTIFICATION_LENGTH = 500


def _get_adapter():
    """获取当前 agent adapter（延迟初始化，避免模块加载时读配置）"""
    return get_agent_adapter()


class Response:
    """统一的响应格式"""

    @staticmethod
    def error(msg: str) -> Tuple[bool, Dict[str, Any]]:
        """错误响应"""
        return False, {'error': msg}

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
    """
    处理继续 Claude 会话的请求

    同步等待一小段时间判断命令是否能正常启动，然后返回结果。

    Args:
        data: 请求数据
            - session_id: Claude 会话 ID (必需)
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 飞书聊天 ID（网关调用时必传，飞书事件必定携带；非空时触发 dissolved 自动复活）
            - message_id: 用户消息 ID (可选，用于回复式通知)
            - claude_command: 指定使用的 Claude 命令 (可选)

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
    claude_command = data.get('claude_command', '') or ''

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

    # 验证 claude_command 合法性（如果指定了的话）
    adapter = _get_adapter()
    if claude_command:
        if claude_command not in adapter.get_commands():
            return Response.error('invalid claude_command')

    # Command 优先级: 请求指定 > session 记录 > 默认
    if not claude_command:
        claude_command = session_data.get('claude_command', '')

    actual_cmd = adapter.resolve_command(claude_command)
    logger.info(f"[continue] Session: {session_id}, Dir: {project_dir}, Cmd: {actual_cmd}, Prompt: {prompt[:50]}...")

    # 更新 session 映射：刷新 claude_command 和 chat_id
    # chat_id 可能变化（如用户在不同聊天中通过默认工作目录继续同一 session）
    # chat_id 来自飞书消息事件（P2P / 群聊均必定非空），非空 chat_id 自动清除 dissolved
    session_store.save(session_id, chat_id, claude_command=actual_cmd)
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
        on_error=_send_error_notification)

    # 添加 session_id 到响应
    if result[0]:  # success
        response = result[1]
        response['session_id'] = session_id

    return result


def handle_new_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    处理新建 Claude 会话的请求

    使用 --session-id 参数发起新会话。

    Args:
        data: 请求数据
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 飞书聊天 ID，通常非空（来自飞书事件，群聊或 P2P）；
                仅 group 模式下从 P2P 发起 /new 时为空，
                由本函数调 do_ensure_chat 建群后回填
            - message_id: 原始消息 ID (可选，用于飞书网关回复用户消息)
            - claude_command: 指定使用的 Claude 命令 (可选)
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

    project_dir = data.get('project_dir', '')
    prompt = data.get('prompt', '')
    chat_id = data.get('chat_id', '') or ''  # 确保 None 转为空字符串
    message_id = data.get('message_id', '') or ''
    claude_command = data.get('claude_command', '') or ''
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

    # 确定实际命令：网关传入 > store 已有值（/clear clone 继承）> 默认
    adapter = _get_adapter()
    if claude_command:
        # 验证 claude_command 合法性（如果指定了的话）
        if claude_command not in adapter.get_commands():
            return Response.error('invalid claude_command')
    else:
        session_data = session_store.get_session(session_id)
        claude_command = (session_data or {}).get('claude_command', '')
    actual_cmd = adapter.resolve_command(claude_command)
    logger.info(f"[new] Session: {session_id}, Dir: {project_dir}, Cmd: {actual_cmd}, Prompt: {prompt[:50]}...")

    from config import FEISHU_SESSION_MODE
    is_group_mode = (FEISHU_SESSION_MODE == 'group')
    if is_group_mode and not chat_id:
        # group 模式无 chat_id → 委托给 do_ensure_chat 创建群聊并绑定
        from handlers.callback import do_ensure_chat
        ok, ensure_result = do_ensure_chat(session_id, project_dir)
        if not ok:
            logger.warning("[claude-new] ensure-chat failed for %s: %s",
                           session_id, ensure_result)
            return Response.error(f'Failed to create group chat: {ensure_result}')
        chat_id = ensure_result
        logger.info("[claude-new] ensure-chat created group for %s: %s", session_id, chat_id)

    # 写入 claude_command 等业务属性（与 do_ensure_chat 的"建群"职责解耦：
    # group 分支补写、非 group 分支首次写，统一一处）
    session_store.save(session_id, chat_id, claude_command=actual_cmd, project_dir=project_dir)
    logger.info(f"[claude-new] Saved mapping: {session_id} -> {chat_id}")

    # 防御性 unmute：新会话通常不会有 mute 状态，此处幂等调用，确保不会意外静音
    if session_store.unmute_session(session_id) is True and chat_id:
        _send_unmute_notification(chat_id, session_id, message_id)

    # 设置 skip_user_prompt 标志
    # 由调用方通过 skip_user_prompt 字段决定，不再根据 chat_id 是否为空判断
    skip_user_prompt = data.get('skip_user_prompt', True)
    if skip_user_prompt:
        session_store.set_skip_next_user_prompt(session_id)

    # 通过 agent 适配层启动进程
    adapter = _get_adapter()
    result = launch_agent(
        adapter, session_id, project_dir, prompt,
        chat_id, message_id, session_mode='new',
        command_name=actual_cmd,
        on_error=_send_error_notification)
    if result[0]:  # success
        response = result[1]
        # Codex 路径：用从输出捕获的真实 session ID 替换临时 ID
        captured_id = response.pop('captured_session_id', None)
        if captured_id and captured_id != session_id:
            if session_store.rename_session(session_id, captured_id):
                logger.info("[new] Session ID replaced: %s -> %s",
                            session_id, captured_id)
                session_id = captured_id
            else:
                logger.warning("[new] Failed to rename session %s -> %s, keeping original",
                               session_id, captured_id)
        response['session_id'] = session_id

    return result


# =============================================
# 飞书通知
# =============================================


def _send_error_notification(chat_id: str, message_id: str, error_msg: str):
    """发送错误通知到飞书

    截断过长的错误消息，防止飞书消息超限。

    Args:
        chat_id: 群聊 ID
        message_id: 要回复的消息 ID（为空时降级为普通消息）
        error_msg: 错误消息
    """
    from handlers.utils import reply_feishu_text

    truncated = error_msg[:MAX_NOTIFICATION_LENGTH] if len(error_msg) > MAX_NOTIFICATION_LENGTH else error_msg
    text = f"❌ Claude 执行异常:\n{truncated}"
    success, result = reply_feishu_text(chat_id, message_id, text)
    if success:
        logger.info(f"[claude] Sent error notification to {chat_id}")
    else:
        logger.error(f"[claude] Failed to send error notification: {result}")


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
        logger.info(f"[claude] Sent unmute notification to {chat_id}")
    else:
        logger.error(f"[claude] Failed to send unmute notification: {result}")
