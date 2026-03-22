"""Claude 会话相关处理器

处理用户通过飞书回复消息继续 Claude 会话的请求，
以及通过 /new 指令发起新的 Claude 会话。
"""

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import uuid
from typing import Tuple, Dict, Any

from services.session_chat_store import SessionChatStore
from handlers.utils import build_shell_cmd, run_in_background as _run_in_background

logger = logging.getLogger(__name__)

# 常量定义
STARTUP_TIMEOUT_SECONDS = 30  # 后台启动阶段等待时间（秒），兜住延迟失败
STARTUP_CHECK_SECONDS = 2  # 启动检查等待时间（秒）
MAX_LOG_LENGTH = 500  # 日志最大长度
MAX_NOTIFICATION_LENGTH = 500  # 通知消息最大长度

# MCP 配置
MCP_TOOL_NAME = "mcp__approver__permission_request"


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


def handle_continue_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    处理继续 Claude 会话的请求

    同步等待一小段时间判断命令是否能正常启动，然后返回结果。

    Args:
        data: 请求数据
            - session_id: Claude 会话 ID (必需)
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 群聊 ID (可选)
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
    claude_command = data.get('claude_command', '') or ''

    # 参数验证
    if not session_id:
        return Response.error('Session not registered or has expired')
    if not project_dir:
        return Response.error('Missing project_dir')
    if not prompt:
        return Response.error('Missing prompt')

    # 验证项目目录存在
    if not os.path.exists(project_dir):
        return Response.error(f'Project directory not found: {project_dir}')

    # 验证 claude_command 合法性（如果指定了的话）
    if claude_command:
        from config import get_claude_commands
        if claude_command not in get_claude_commands():
            return Response.error('invalid claude_command')

    # Command 优先级: 请求指定 > SessionChatStore session 记录 > 默认
    if not claude_command:
        chat_store = SessionChatStore.get_instance()
        if chat_store:
            claude_command = chat_store.get_command(session_id) or ''

    actual_cmd = _get_claude_command(claude_command)
    logger.info(f"[claude-continue] Session: {session_id}, Dir: {project_dir}, Cmd: {actual_cmd}, Prompt: {prompt[:50]}...")

    # 如果有 chat_id，保存 session_id -> chat_id + claude_command 映射
    if chat_id:
        store = SessionChatStore.get_instance()
        if store:
            store.save(session_id, chat_id, claude_command=actual_cmd)
            logger.info(f"[claude-continue] Saved chat_id mapping: {session_id} -> {chat_id}")

    # 同步执行并检查（使用 resume 模式）
    result = _execute_and_check(session_id, project_dir, prompt, chat_id,
                                session_mode='resume', claude_command=actual_cmd)

    # 添加 session_id 到响应
    if result[0]:  # success
        response = result[1]
        response['session_id'] = session_id

    return result


def _get_shell() -> str:
    """获取用户默认 shell

    Returns:
        shell 路径，如 '/bin/bash'，默认 '/bin/bash'
    """
    return os.environ.get('SHELL', '/bin/bash')


def _get_mcp_args(project_dir: str, session_id: str) -> str:
    """
    获取 MCP 审批相关的命令行参数

    动态构建 MCP 配置 JSON 字符串，不依赖外部配置文件。
    通过 args 参数将 project_dir 和 session_id 传递给 MCP server。

    Args:
        project_dir: 项目工作目录，传递给 MCP server 用于权限审批上下文
        session_id: Claude 会话 ID，传递给 MCP server 用于权限审批上下文

    Returns:
        MCP 相关参数字符串
        如果 MCP 脚本不存在则返回空字符串
    """
    # 动态定位 MCP 脚本路径（与 claude.py 同目录）
    mcp_script = os.path.join(os.path.dirname(__file__), "permission_mcp.py")

    if not os.path.exists(mcp_script):
        logger.debug(f"[mcp] MCP script not found: {mcp_script}")
        return ""

    # 通过 args 参数传递 cwd 和 session_id，避免环境变量污染
    python_cmd = sys.executable or "python3"
    mcp_config = {
        "mcpServers": {
            "approver": {
                "command": python_cmd,
                "args": [mcp_script, "--cwd", project_dir, "--session-id", session_id]
            }
        }
    }

    config_json = json.dumps(mcp_config)
    logger.info(f"[mcp] Using MCP config: script={mcp_script}, session={session_id}, cwd={project_dir}")
    return f"--permission-prompt-tool {MCP_TOOL_NAME} --mcp-config {shlex.quote(config_json)}"


def _get_claude_command(claude_command: str = '') -> str:
    """获取 Claude 命令

    优先使用传入的 claude_command，否则从配置列表取默认值。

    Args:
        claude_command: 指定的命令字符串（可选）

    Returns:
        命令字符串，如 'claude' 或 'claude --setting opus'
    """
    if claude_command:
        return claude_command
    from config import get_claude_commands
    return get_claude_commands()[0]


def _execute_and_check(session_id: str, project_dir: str, prompt: str, chat_id: str = '',
                       session_mode: str = 'resume', claude_command: str = '') -> Tuple[bool, Dict[str, Any]]:
    """
    执行命令并检查启动状态

    通过登录 shell 执行命令，支持 shell 配置文件中的别名和环境变量。

    Args:
        session_id: Claude 会话 ID
        project_dir: 项目工作目录
        prompt: 用户的问题
        chat_id: 群聊 ID（用于异常通知）
        session_mode: 会话模式，'resume' 继续会话，'new' 新建会话
        claude_command: 指定使用的 Claude 命令（可选，为空时使用默认）

    Returns:
        (success, response)
    """
    shell = _get_shell()
    claude_cmd = _get_claude_command(claude_command)
    # 使用 shlex.quote 安全处理 prompt 中的特殊字符
    safe_prompt = shlex.quote(prompt)
    safe_session = shlex.quote(session_id)

    # 根据会话模式选择参数
    # 先收集所有 flag 参数，最后用 -- 分隔符追加 prompt
    # 确保 prompt 中的 --flag 不会被 CLI 误解析为参数
    if session_mode == 'new':
        flags = f'{claude_cmd} -p --session-id {safe_session}'
        log_prefix = '[claude-new]'
    else:
        flags = f'{claude_cmd} -p --resume {safe_session}'
        log_prefix = '[claude-continue]'

    mcp_args = _get_mcp_args(project_dir, session_id).strip()
    if mcp_args:
        flags += ' ' + mcp_args

    cmd_str = f'{flags} -- {safe_prompt}'
    log_cmd = f"{flags} -- '<prompt>'"

    cmd = build_shell_cmd(shell, cmd_str)

    # 打印命令（log_cmd 隐藏了 prompt 内容）
    logger.info(f"{log_prefix} Executing: cd {project_dir} && {log_cmd}")

    # 清除 CLAUDECODE 环境变量，避免嵌套会话检测阻止子会话启动
    # 参考：https://code.claude.com/docs/en/headless
    env = os.environ.copy()
    env.pop('CLAUDECODE', None)

    # 启动进程
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=env
        )
    except Exception as e:
        # 启动失败（命令不存在等）
        error_msg = str(e)
        logger.error(f"{log_prefix} Failed to start process: {error_msg}")
        return Response.error(error_msg)

    # 等待一小段时间检查进程状态
    try:
        stdout, stderr = proc.communicate(timeout=STARTUP_CHECK_SECONDS)
        returncode = proc.returncode
        if returncode == 0:
            logger.info(f"{log_prefix} Command completed quickly")
            return Response.completed(stdout[:MAX_LOG_LENGTH * 2] if stdout else '')
        else:
            error_msg = stderr.strip() if stderr.strip() else stdout.strip()
            if not error_msg:
                error_msg = f"命令执行失败，退出码: {returncode}"
            logger.warning(f"{log_prefix} Command failed with exit code {returncode}: {error_msg}")
            return Response.error(error_msg)
    except subprocess.TimeoutExpired:
        # 进程仍在运行，正常启动
        logger.info(f"{log_prefix} Command is running in background")
        # 在后台等待完成
        _run_in_background(_wait_for_completion, (proc, session_id, chat_id))
        return Response.processing()


def _wait_for_completion(proc: subprocess.Popen, session_id: str, chat_id: str = ''):
    """
    在后台短暂等待，捕获启动阶段的延迟失败

    只等待 STARTUP_TIMEOUT_SECONDS 秒。如果进程在此期间失败，发送通知；
    如果仍在运行，说明 claude 已正常启动，不再监控。

    Args:
        proc: 子进程对象
        session_id: 会话 ID
        chat_id: 群聊 ID（用于异常通知）
    """
    try:
        stdout, stderr = proc.communicate(timeout=STARTUP_TIMEOUT_SECONDS)
        if proc.returncode == 0:
            logger.info(f"[claude] Command completed successfully, session: {session_id}")
            if stdout:
                logger.debug(f"[claude] stdout: {stdout[:MAX_LOG_LENGTH]}...")
        else:
            # 启动阶段失败，记录日志
            error_summary = stderr.strip()[:MAX_LOG_LENGTH] if stderr.strip() else '(无错误输出)'
            logger.warning(f"[claude] Command failed with exit code {proc.returncode}, session: {session_id}, error: {error_summary}")

            # 如果有 chat_id 且有 stderr，才发送飞书通知
            if chat_id and stderr:
                _send_error_notification(chat_id, stderr.strip()[:MAX_LOG_LENGTH])
    except subprocess.TimeoutExpired:
        # 进程仍在运行，说明 claude 已正常启动，放手让它自己跑
        logger.info(f"[claude] Process still running after {STARTUP_TIMEOUT_SECONDS}s, session: {session_id} — detaching")
        # 关闭 pipe，避免 buffer 满导致子进程阻塞
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        # 启动守护线程回收子进程，防止 zombie
        threading.Thread(target=proc.wait, daemon=True).start()
    except Exception as e:
        logger.error(f"[claude] Execution error: {e}, session: {session_id}")
        if chat_id:
            _send_error_notification(chat_id, str(e)[:MAX_NOTIFICATION_LENGTH])


def _send_error_notification(chat_id: str, error_msg: str):
    """发送错误通知到飞书

    Args:
        chat_id: 群聊 ID
        error_msg: 错误消息
    """
    from handlers.utils import send_feishu_text

    text = "❌ Claude 执行异常:\n%s" % error_msg
    success, result = send_feishu_text(chat_id, text)
    if success:
        logger.info("[claude] Sent error notification to %s", chat_id)
    else:
        logger.error("[claude] Failed to send error notification: %s", result)


def handle_new_session(data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    处理新建 Claude 会话的请求

    使用 --session-id 参数发起新会话。

    Args:
        data: 请求数据
            - project_dir: 项目工作目录 (必需)
            - prompt: 用户的问题 (必需)
            - chat_id: 群聊 ID (可选)
            - message_id: 原始消息 ID (可选，用于飞书网关回复用户消息)
            - claude_command: 指定使用的 Claude 命令 (可选)

    Returns:
        (success, response):
            - success=True, status='processing': 命令正在执行
            - success=True, status='completed': 命令快速完成
            - success=False, error=...: 命令启动/执行失败
    """
    project_dir = data.get('project_dir', '')
    prompt = data.get('prompt', '')
    chat_id = data.get('chat_id', '') or ''  # 确保 None 转为空字符串
    claude_command = data.get('claude_command', '') or ''

    # 参数验证
    if not project_dir:
        return Response.error('Missing project_dir')
    if not prompt:
        return Response.error('Missing prompt')

    # 验证项目目录存在
    if not os.path.exists(project_dir):
        return Response.error(f'Project directory not found: {project_dir}')

    # 验证 claude_command 合法性（如果指定了的话）
    if claude_command:
        from config import get_claude_commands
        if claude_command not in get_claude_commands():
            return Response.error('invalid claude_command')

    # 生成 UUID session_id
    session_id = str(uuid.uuid4())

    actual_cmd = _get_claude_command(claude_command)
    logger.info(f"[claude-new] Session: {session_id}, Dir: {project_dir}, Cmd: {actual_cmd}, Prompt: {prompt[:50]}...")

    # 保存 session_id -> chat_id + claude_command 映射
    if chat_id:
        store = SessionChatStore.get_instance()
        if store:
            store.save(session_id, chat_id, claude_command=actual_cmd)
            logger.info(f"[claude-new] Saved chat_id mapping: {session_id} -> {chat_id}")

    # 同步执行并检查（使用 new 模式）
    result = _execute_and_check(session_id, project_dir, prompt, chat_id,
                                session_mode='new', claude_command=actual_cmd)
    if result[0]:  # success
        # 添加 session_id 到响应
        response = result[1]
        response['session_id'] = session_id

    return result
