"""Agent 适配层 — 多 AI 编码代理的统一抽象

提供 AgentAdapter 基类和共享的进程启动/监控逻辑。
各 agent（Claude、Codex 等）通过实现 AgentAdapter 接入系统，
上层业务代码通过 launch_agent() 统一调用。
"""

import json
import logging
import os
import select
import shlex
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# ── 常量 ──
STARTUP_TIMEOUT_SECONDS = 30   # 后台启动阶段等待时间（秒），兜住延迟失败
STARTUP_CHECK_SECONDS = 2      # 启动检查等待时间（秒）
SESSION_ID_CAPTURE_TIMEOUT_SECONDS = 10  # Codex 新会话首行 JSONL 等待时间（秒）
MAX_LOG_LENGTH = 500           # 日志最大长度

# on_error 回调类型: (chat_id, message_id, error_msg) -> None
ErrorCallback = Optional[Callable[[str, str, str], None]]


# =============================================
# AgentAdapter 基类
# =============================================


class AgentAdapter(ABC):
    """AI 编码代理适配器接口

    每个 agent（Claude、Codex 等）实现此接口，封装 CLI 参数构建、
    环境变量调整等 agent 特有的协议细节。
    """

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """代理标识符: 'claude', 'codex', ..."""

    @abstractmethod
    def resolve_command(self, command_name: str = '') -> str:
        """解析 agent 命令字符串

        优先使用传入的 command_name，否则从配置取默认值。

        Args:
            command_name: 指定的命令字符串（可选）

        Returns:
            命令字符串，如 'claude' 或 'claude --model opus'
        """

    @abstractmethod
    def get_commands(self) -> List[str]:
        """获取该 agent 可用的命令列表（用于校验）

        Returns:
            命令字符串列表，至少包含一个元素
        """

    @abstractmethod
    def build_command_string(self, command_name: str, prompt: str,
                             session_id: str, session_mode: str,
                             project_dir: str) -> str:
        """构建完整的 shell 命令字符串

        Args:
            command_name: agent 命令（如 'claude' 或 'claude --model opus'）
            prompt: 用户 prompt
            session_id: 会话 ID
            session_mode: 'new' 或 'resume'
            project_dir: 项目工作目录

        Returns:
            可传给 shell 执行的命令字符串
        """

    @abstractmethod
    def build_debug_command_string(self, command_name: str, session_id: str,
                                   session_mode: str) -> str:
        """构建日志版本的命令字符串（隐藏 prompt 和敏感参数）

        Args:
            command_name: agent 命令
            session_id: 会话 ID
            session_mode: 'new' 或 'resume'

        Returns:
            脱敏后的命令字符串
        """

    @property
    def needs_output_session_id(self) -> bool:
        """是否需要从进程输出中捕获 session ID

        Claude 预先指定 session ID（False）；Codex 自动生成需要从输出捕获（True）。
        """
        return False

    def parse_session_id(self, line: str) -> Optional[str]:
        """从进程输出行解析 session ID

        仅当 needs_output_session_id=True 时被调用。
        默认返回 None，子类按需覆盖。

        Args:
            line: stdout 的一行输出

        Returns:
            session ID 字符串，解析失败时返回 None
        """
        return None

    def build_env(self, base_env: Dict[str, str]) -> Dict[str, str]:
        """修改子进程环境变量

        默认不做修改，子类可覆盖以清除/设置特定变量。

        Args:
            base_env: 当前进程环境变量的副本

        Returns:
            修改后的环境变量字典
        """
        return base_env


# =============================================
# 共享工具函数
# =============================================


def shlex_join(argv: List[str]) -> str:
    """shlex.join 的 Python 3.6 兼容版

    Args:
        argv: 参数列表

    Returns:
        shell-quoted 后的命令字符串
    """
    return ' '.join(shlex.quote(a) for a in argv)


def expand_template(template: str, cmd_argv: List[str],
                    args_argv: List[str]) -> str:
    """根据模板把 cmd 和 args 组装成 shell 命令字符串

    占位符展开规则:
      - 裸占位符 {args}  → 各参数独立 shell-quote 后拼接
      - 引号占位符 "{args}" / '{args}' → 整体打包为一个 shell 参数
      - {cmd} 同理

    Args:
        template: 模板字符串, 如 '{cmd} {args}' 或 '{cmd} -a "{args}"'
        cmd_argv: 命令 argv 列表, 如 ['claude'] 或 ['ccsdk', 'code', '-t', 'claude']
        args_argv: 参数 argv 列表, 如 ['--print', '--resume', 'sid', '--', 'prompt']

    Returns:
        可直接传给 shell 执行的命令字符串
    """
    # posix=False 保留引号字符, 用于区分裸占位符和引号占位符
    tokens = shlex.split(template, posix=False)
    cmd_joined = shlex_join(cmd_argv)
    args_joined = shlex_join(args_argv)

    result = []
    for tok in tokens:
        # 检测是否被成对引号包裹(单或双)
        quoted = len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'")
        inner = tok[1:-1] if quoted else tok

        if inner == '{cmd}':
            # 裸占位符 → 展开为多个独立 argv; 引号占位符 → 合成单个 argv
            result.extend(cmd_argv) if not quoted else result.append(cmd_joined)
        elif inner == '{args}':
            result.extend(args_argv) if not quoted else result.append(args_joined)
        else:
            # 普通 token(如 -a) → 原样 append
            replaced = inner.replace('{cmd}', cmd_joined).replace('{args}', args_joined)
            result.append(replaced)

    return shlex_join(result)


# =============================================
# Agent 工厂
# =============================================

_adapter_instance: Optional[AgentAdapter] = None


def get_agent_adapter() -> AgentAdapter:
    """根据 AGENT_TYPE 配置返回对应的 adapter 单例

    Returns:
        AgentAdapter 实例（ClaudeAdapter 或 CodexAdapter）
    """
    global _adapter_instance
    if _adapter_instance is not None:
        return _adapter_instance

    from config import get_agent_type
    agent_type = get_agent_type()

    if agent_type == 'codex':
        from agents.codex import CodexAdapter
        _adapter_instance = CodexAdapter()
    else:
        from agents.claude import ClaudeAdapter
        _adapter_instance = ClaudeAdapter()

    logger.info("Agent adapter initialized: %s", _adapter_instance.agent_type)
    return _adapter_instance


# =============================================
# 共享启动逻辑
# =============================================


def get_shell() -> str:
    """获取用户默认 shell

    Returns:
        shell 路径，如 '/bin/bash'，默认 '/bin/bash'
    """
    return os.environ.get('SHELL', '/bin/bash')


def launch_agent(adapter: AgentAdapter, session_id: str, project_dir: str,
                 prompt: str, chat_id: str = '', message_id: str = '',
                 session_mode: str = 'resume', command_name: str = '',
                 on_error: ErrorCallback = None) -> Tuple[bool, Dict[str, Any]]:
    """构建命令、启动 agent 进程并检查初始状态

    通过登录 shell 执行命令，支持 shell 配置文件中的别名和环境变量。

    Args:
        adapter: agent 适配器实例
        session_id: 会话 ID
        project_dir: 项目工作目录
        prompt: 用户的问题
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        session_mode: 会话模式，'resume' 继续会话，'new' 新建会话
        command_name: 指定使用的命令（可选，为空时使用默认）
        on_error: 错误通知回调 (chat_id, message_id, error_msg) -> None

    Returns:
        (success, response): response 中可能包含 captured_session_id（Codex 新建会话时）
    """
    from handlers.utils import build_shell_cmd, run_in_background

    agent_type = adapter.agent_type
    log_prefix = '[%s-%s]' % (agent_type, 'new' if session_mode == 'new' else 'continue')

    # ── 1. 构建命令 ──
    shell = get_shell()
    cmd_str = adapter.build_command_string(
        command_name, prompt, session_id, session_mode, project_dir)
    cmd = build_shell_cmd(shell, cmd_str)

    # 日志版本: 不含敏感参数, prompt 用占位符替代
    log_cmd_str = adapter.build_debug_command_string(
        command_name, session_id, session_mode)
    debug_shell_cmd = build_shell_cmd(shell, log_cmd_str)
    if len(debug_shell_cmd) >= 3:
        logger.info("%s Copyable: cd %s && %s %s %s",
                    log_prefix, project_dir,
                    debug_shell_cmd[0], debug_shell_cmd[1],
                    shlex.quote(debug_shell_cmd[2]))
    logger.info("%s shell=%s, Executing: cd %s && %s",
                log_prefix, shell, project_dir, log_cmd_str)

    # ── 2. 启动进程 ──
    env = adapter.build_env(os.environ.copy())
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=env
        )
    except Exception as e:
        error_msg = str(e)
        logger.error("%s Failed to start process: %s", log_prefix, error_msg)
        return False, {'error': error_msg}

    # ── 3. Session ID 捕获 + rename（Codex 新建会话路径）──
    captured_session_id: Optional[str] = None
    if adapter.needs_output_session_id and session_mode == 'new':
        captured_session_id = _capture_session_id(adapter, proc, log_prefix)
        if captured_session_id and captured_session_id != session_id:
            new_id = _rename_session_in_store(
                session_id, captured_session_id, log_prefix)
            if new_id == captured_session_id:
                session_id = captured_session_id
            else:
                # rename 失败，store 中数据仍在 old_id 下，
                # 不能把 captured_id 返回给上层，否则后续 /continue 查不到 mapping
                captured_session_id = None

        return _finish_launch(
            proc, session_id, agent_type, chat_id, message_id,
            on_error, captured_session_id)

    # ── 3b. 标准启动检查（Claude 路径）──
    try:
        stdout, stderr = proc.communicate(timeout=STARTUP_CHECK_SECONDS)
        returncode = proc.returncode
        if returncode == 0:
            logger.info("%s Command completed quickly", log_prefix)
            return True, {'status': 'completed',
                          'output': stdout[:MAX_LOG_LENGTH * 2] if stdout else ''}
        else:
            error_msg = stderr.strip() if stderr.strip() else stdout.strip()
            if not error_msg:
                error_msg = f"命令执行失败，退出码: {returncode}"
            logger.warning("%s Command failed with exit code %s: %s",
                           log_prefix, returncode, error_msg)
            return False, {'error': error_msg}
    except subprocess.TimeoutExpired:
        # 进程仍在运行，正常启动
        logger.info("%s Command is running in background", log_prefix)
        run_in_background(
            _monitor_startup,
            (proc, session_id, agent_type, chat_id, message_id, on_error))
        return True, {'status': 'processing'}


# =============================================
# Session ID 捕获（内部）
# =============================================


def _capture_session_id(adapter: AgentAdapter, proc: subprocess.Popen,
                        log_prefix: str) -> Optional[str]:
    """从进程输出中捕获 session ID（Codex 路径）

    循环读取 stdout 行，跳过非 JSON 内容（如代理配置、ANSI 输出），
    直到找到可解析的 session ID 或超时。

    Args:
        adapter: agent 适配器（提供 parse_session_id 方法）
        proc: 已启动的子进程
        log_prefix: 日志前缀

    Returns:
        捕获到的 session ID，失败时返回 None
    """
    import time
    deadline = time.monotonic() + SESSION_ID_CAPTURE_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("%s Session ID capture timed out after %ss",
                               log_prefix, SESSION_ID_CAPTURE_TIMEOUT_SECONDS)
                break
            line = _readline_with_timeout(proc.stdout, remaining)
            if not line:
                # 超时或进程退出
                if proc.poll() is not None:
                    logger.warning("%s Process exited before session ID captured",
                                   log_prefix)
                break
            stripped = line.strip()
            if not stripped or not stripped.startswith('{'):
                # 跳过非 JSON 行（如代理配置信息、ANSI 输出）
                logger.debug("%s Skipping non-JSON output: %s",
                             log_prefix, stripped[:100])
                continue
            captured = adapter.parse_session_id(stripped)
            if captured:
                logger.info("%s Captured session ID: %s", log_prefix, captured)
                return captured
            # JSON 行但不是 session ID 事件，继续读
            logger.debug("%s Skipping non-session-id JSON: %s",
                         log_prefix, stripped[:100])
    except Exception as e:
        logger.warning("%s Error reading session ID: %s", log_prefix, e)
    return None


def _rename_session_in_store(old_id: str, new_id: str,
                             log_prefix: str) -> str:
    """在 store 中将临时 session ID 替换为真实 ID

    Args:
        old_id: 临时 session ID
        new_id: 从输出中捕获的真实 session ID
        log_prefix: 日志前缀

    Returns:
        最终使用的 session ID（rename 成功返回 new_id，失败返回 old_id）
    """
    try:
        from services.session_chat_store import SessionChatStore
        store = SessionChatStore.get_instance()
        if store and store.rename_session(old_id, new_id):
            logger.info("%s Session ID renamed: %s -> %s",
                        log_prefix, old_id, new_id)
            return new_id
    except Exception as e:
        logger.warning("%s Failed to rename session: %s", log_prefix, e)
    return old_id


def _finish_launch(proc: subprocess.Popen, session_id: str, agent_type: str,
                   chat_id: str, message_id: str, on_error: ErrorCallback,
                   captured_session_id: Optional[str]) -> Tuple[bool, Dict[str, Any]]:
    """Codex 路径：session ID 捕获后，检查进程状态并进入后台监控

    Args:
        proc: 子进程对象
        session_id: 会话 ID（可能已 rename 为真实 ID）
        agent_type: agent 类型标识
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        on_error: 错误通知回调
        captured_session_id: 从输出捕获的真实 session ID（可能为 None）

    Returns:
        (success, response)
    """
    from handlers.utils import run_in_background
    log_prefix = '[%s]' % agent_type

    if proc.poll() is not None:
        # 罕见：进程在首行输出后立即退出
        returncode = proc.returncode
        if returncode == 0:
            logger.info("%s Command completed quickly", log_prefix)
            result = (True, {'status': 'completed', 'output': ''})
        else:
            stderr_out = proc.stderr.read() if proc.stderr else ''
            error_msg = stderr_out.strip() or f"命令执行失败，退出码: {returncode}"
            logger.warning("%s Command failed: %s", log_prefix, error_msg)
            result = False, {'error': error_msg}
        if captured_session_id and result[0]:
            result[1]['captured_session_id'] = captured_session_id
        return result

    logger.info("%s Command is running in background", log_prefix)
    run_in_background(
        _monitor_startup,
        (proc, session_id, agent_type, chat_id, message_id, on_error))
    result_dict = {'status': 'processing'}
    if captured_session_id:
        result_dict['captured_session_id'] = captured_session_id
    return True, result_dict


# =============================================
# 进程监控（共享）
# =============================================


def _monitor_startup(proc: subprocess.Popen, session_id: str,
                     agent_type: str, chat_id: str = '',
                     message_id: str = '',
                     on_error: ErrorCallback = None) -> None:
    """后台短暂等待，捕获启动阶段的延迟失败

    只等待 STARTUP_TIMEOUT_SECONDS 秒。如果进程在此期间失败，
    通过 on_error 回调通知；如果仍在运行，交由 _monitor_detached 持续监控。

    Args:
        proc: 子进程对象
        session_id: 会话 ID
        agent_type: agent 类型标识
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        on_error: 错误通知回调
    """
    log_prefix = '[%s]' % agent_type
    try:
        stdout, stderr = proc.communicate(timeout=STARTUP_TIMEOUT_SECONDS)
        if proc.returncode == 0:
            logger.info("%s Command completed successfully, session: %s",
                        log_prefix, session_id)
            if stdout:
                logger.debug("%s stdout: %s...", log_prefix,
                             stdout[:MAX_LOG_LENGTH])
            _handle_late_session_id(agent_type, session_id, stdout)
        else:
            error_summary = (stderr.strip()[:MAX_LOG_LENGTH]
                             if stderr.strip() else '(无错误输出)')
            logger.warning(
                "%s Command failed with exit code %s, session: %s, error: %s",
                log_prefix, proc.returncode, session_id, error_summary)
            if chat_id and stderr and on_error:
                on_error(chat_id, message_id, stderr.strip())
    except subprocess.TimeoutExpired:
        logger.info(
            "%s Process still running after %ss, session: %s "
            "— detaching to background monitor",
            log_prefix, STARTUP_TIMEOUT_SECONDS, session_id)
        threading.Thread(
            target=_monitor_detached,
            args=(proc, session_id, agent_type, chat_id, message_id, on_error),
            daemon=True
        ).start()
    except Exception as e:
        logger.error("%s Execution error: %s, session: %s",
                     log_prefix, e, session_id)
        if chat_id and on_error:
            on_error(chat_id, message_id, str(e))


def _monitor_detached(proc: subprocess.Popen, session_id: str,
                      agent_type: str, chat_id: str = '',
                      message_id: str = '',
                      on_error: ErrorCallback = None) -> None:
    """后台持续读取 pipe 并等待子进程退出，失败时通过 on_error 通知

    通过后台线程排空 stdout/stderr 防止 buffer 满阻塞，
    同时保留尾部输出用于错误通知。

    Args:
        proc: 子进程对象（stdout/stderr 仍然打开）
        session_id: 会话 ID
        agent_type: agent 类型标识
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        on_error: 错误通知回调
    """
    log_prefix = '[%s]' % agent_type
    stderr_tail = ''
    stdout_tail = ''
    try:
        def drain_stderr():
            nonlocal stderr_tail
            stderr_tail = _drain_pipe(proc.stderr)

        def drain_stdout():
            nonlocal stdout_tail
            stdout_tail = _drain_pipe(proc.stdout)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        returncode = proc.wait()
        stderr_thread.join(timeout=5)
        stdout_thread.join(timeout=5)

        if returncode == 0:
            logger.info("%s Detached process completed successfully, session: %s",
                        log_prefix, session_id)
            _handle_late_session_id(agent_type, session_id, stdout_tail)
        else:
            error_output = stderr_tail or stdout_tail
            logger.warning(
                "%s Detached process failed (code=%s), session: %s, output: %s",
                log_prefix, returncode, session_id,
                error_output[:MAX_LOG_LENGTH])
            if chat_id and on_error:
                error_msg = error_output or f"{agent_type} 进程异常退出 (code={returncode})"
                on_error(chat_id, message_id, error_msg)
    except Exception as e:
        logger.error("%s Error waiting for detached process: %s, session: %s",
                     log_prefix, e, session_id)


# =============================================
# 内部工具
# =============================================


def _drain_pipe(pipe: Any, tail_lines: int = 20) -> str:
    """读取并丢弃 pipe 内容，保留最后 N 行用于错误诊断

    防止 pipe buffer 满导致子进程阻塞，同时保留尾部用于错误诊断。

    Args:
        pipe: 可读的文件对象（proc.stdout 或 proc.stderr）
        tail_lines: 保留的尾部行数

    Returns:
        最后 N 行的文本内容
    """
    if pipe is None:
        return ''
    tail = deque(maxlen=tail_lines)
    try:
        for line in pipe:
            tail.append(line)
    except (ValueError, OSError):
        pass  # pipe 已关闭
    return ''.join(tail).strip()


def _readline_with_timeout(pipe: Any, timeout: float) -> str:
    """带超时的管道单行读取，防止阻塞请求线程

    已知限制: select.select 检测的是 OS pipe buffer，而 universal_newlines=True
    的 TextIOWrapper 内部有 BufferedReader。若多行数据在同一次 read() 中被拉入
    Python buffer，后续 select 会因 OS buffer 已空而误报超时。实际影响极低，因为
    Codex 的 thread.started 事件几乎总是第一行输出。如需彻底解决可改用线程+Queue。

    Args:
        pipe: 可读的文件对象
        timeout: 超时秒数

    Returns:
        读取到的一行文本，超时或错误时返回空字符串
    """
    if pipe is None:
        return ''
    try:
        readable, _, _ = select.select([pipe], [], [], timeout)
        if not readable:
            return ''
        return pipe.readline()
    except (OSError, ValueError):
        return ''


def _handle_late_session_id(agent_type: str, session_id: str,
                            stdout_text: str) -> None:
    """从已排空的 stdout 中补捕 Codex session ID（启动超时后的兜底）

    Args:
        agent_type: agent 类型标识，非需要输出捕获的 agent 时直接返回
        session_id: 当前 session ID（可能是临时 ID）
        stdout_text: 已排空的 stdout 文本内容
    """
    if not stdout_text:
        return
    adapter = get_agent_adapter()
    if not adapter.needs_output_session_id:
        return
    for line in stdout_text.splitlines():
        captured_id = adapter.parse_session_id(line.strip())
        if captured_id and captured_id != session_id:
            _rename_session_in_store(session_id, captured_id,
                                     '[%s]' % agent_type)
            return
