"""Agent 适配层 — 多 AI 编码代理的统一抽象

提供 AgentAdapter 基类和共享的进程启动/监控逻辑。
各 agent（Claude、Codex 等）通过实现 AgentAdapter 接入系统，
上层业务代码通过 launch_agent() 统一调用。
"""

import logging
import os
import shlex
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque
from typing import Callable, Dict, List, Optional, Set, Tuple, Any

logger = logging.getLogger(__name__)

# ── 常量 ──
STARTUP_TIMEOUT_SECONDS = 30   # 后台启动阶段等待时间（秒），兜住延迟失败
STARTUP_CHECK_SECONDS = 2      # 启动检查等待时间（秒）
MAX_LOG_LENGTH = 500           # 日志最大长度

# on_error 回调类型: (agent_type, chat_id, message_id, session_id, error_msg) -> None
ErrorCallback = Optional[Callable[[str, str, str, str, str], None]]
# on_complete 回调类型: (agent_type, chat_id, message_id, session_id, output) -> None
CompleteCallback = Optional[Callable[[str, str, str, str, str], None]]


# =============================================
# 斜杠命令元信息
# =============================================


class SlashCommandInfo(object):
    """Agent 斜杠命令元信息

    描述一个 Agent 在 headless 模式下支持的斜杠命令及其属性。
    框架根据 triggers_stop_hook 决定是否需要手动发送完成通知。

    Attributes:
        triggers_stop_hook: 命令执行后 Agent 是否产生输出触发 stop hook。
            True  = 有输出，stop hook 自动通知
            False = 无输出，框架手动发完成通知并清理状态（如 /compact、/context）
        brief: 简短描述（用于 /help 卡片展示）
        examples: 示例列表，每项为 (示例命令, 说明) 元组
    """

    def __init__(self, triggers_stop_hook: bool, brief: str,
                 examples: Optional[List[Tuple[str, str]]] = None) -> None:
        self.triggers_stop_hook = triggers_stop_hook
        self.brief = brief
        self.examples = examples or []


# =============================================
# AgentAdapter 基类
# =============================================


class AgentAdapter(ABC):
    """AI 编码代理适配器接口

    每个 agent（Claude、Codex 等）实现此接口，封装 CLI 参数构建、
    环境变量调整等 agent 特有的协议细节。
    """

    # ── 子类必须实现 ──────────────────────────────

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """代理标识符: 'claude', 'codex', ..."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """面向用户的产品名（如 'Claude Code'、'Codex'）

        每个 adapter 必须显式声明，确保新增 agent 时不会遗漏。

        Returns:
            产品名字符串，用于通知和卡片展示
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

    # ── 子类可选覆盖（基类提供默认实现）──────────────

    @property
    def needs_output_session_id(self) -> bool:
        """是否需要从进程输出中捕获 session ID

        Claude 预先指定 session ID（False）；Codex 覆盖为 True，
        从 stdout JSONL 捕获自动生成的 thread_id。

        Returns:
            True 时 launch_agent 会调用 parse_session_id 读取输出
        """
        return False

    @property
    def session_id_capture_timeout(self) -> int:
        """从进程输出捕获 session ID 的超时秒数

        仅当 needs_output_session_id=True 时有意义。
        Codex 覆盖为 10 秒。

        Returns:
            超时秒数
        """
        return 0

    def resolve_command(self, command_name: str = '') -> str:
        """解析 agent 命令字符串

        优先使用传入的 command_name，否则从配置取默认值。

        Args:
            command_name: 指定的命令字符串（可选）

        Returns:
            命令字符串，如 'claude' 或 'codex --sandbox workspace-write'
        """
        if command_name:
            return command_name
        return self.get_commands()[0]

    def parse_session_id(self, line: str) -> Optional[str]:
        """从进程输出行解析 session ID

        仅当 needs_output_session_id=True 时被调用。
        默认返回 None，Codex 覆盖以解析 thread.started 事件。

        Args:
            line: stdout 的一行输出

        Returns:
            session ID 字符串，解析失败时返回 None
        """
        return None

    def build_env(self, base_env: Dict[str, str]) -> Dict[str, str]:
        """修改子进程环境变量

        默认不做修改。Claude 覆盖以清除 CLAUDECODE 变量
        （防止嵌套会话检测阻止子会话启动）。

        Args:
            base_env: 当前进程环境变量的副本

        Returns:
            修改后的环境变量字典
        """
        return base_env

    def get_slash_commands(self) -> Dict[str, SlashCommandInfo]:
        """声明该 Agent 在 headless 模式下支持的斜杠命令

        子类覆盖此方法来声明自己支持的命令及其属性。
        默认返回空字典，表示不支持任何斜杠命令。

        Returns:
            命令名 -> SlashCommandInfo 的映射，如 {'compact': SlashCommandInfo(...)}
        """
        return {}


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


def prepend_env_overrides(session_id: str, cmd_str: str,
                          redact: bool = False) -> str:
    """把 session.env_overrides 转成 K=V 前缀拼到命令前

    输出形如 `env -u K1 K2='V2' claude --print --resume ...`。
    POSIX shell 的 K=V 前缀仅作用于其后那条命令，覆盖 .zshenv/.bashrc
    中已 export 的同名变量，让续聊命中用户实际启动 agent 时的 env。

    Args:
        session_id: 会话 ID（用于查 store）
        cmd_str: expand_template 返回的命令串
        redact: True 时把值替换成 *** 用于日志输出

    Returns:
        带前缀的命令串；无 env_overrides 且无黑名单时原样返回
    """
    from stores.session_chat_store import SessionChatStore
    store = SessionChatStore.get_instance()
    env = store.get_env_overrides(session_id) if store else {}

    blacklist = _resolve_env_blacklist(env)

    # 黑名单中的 key 只做 unset，不注入值
    inject_env = {k: v for k, v in env.items() if k not in blacklist}

    if not blacklist and not inject_env:
        return cmd_str

    # 拼装前缀：先 env -u 黑名单，再 K=V 白名单
    parts: List[str] = []
    if blacklist:
        parts.append('env')
        for name in sorted(blacklist):
            parts.extend(['-u', name])

    for k in sorted(inject_env):
        v = '***' if redact else inject_env[k]
        parts.append('%s=%s' % (k, shlex.quote(v)))

    return ' '.join(parts) + ' ' + cmd_str


def _resolve_env_blacklist(env_overrides: Dict[str, str]) -> Set[str]:
    """解析 SESSION_ENV_BLACKLIST 配置，返回需 unset 的变量名集合

    支持精确名和 PREFIX_* 前缀通配。
    通配仅对 env_overrides 中已捕获的 key 展开，避免无谓 env -u。

    Args:
        env_overrides: 当前 session 的 env_overrides

    Returns:
        变量名集合
    """
    from config import get_config
    blacklist = (get_config('SESSION_ENV_BLACKLIST', '') or '').replace(',', ' ').split()
    if not blacklist:
        return set()

    unset_names: Set[str] = set()
    for rule in blacklist:
        if rule.endswith('*'):
            prefix = rule[:-1]
            for key in env_overrides:
                if key.startswith(prefix):
                    unset_names.add(key)
        else:
            unset_names.add(rule)
    return unset_names


# =============================================
# Agent 工厂
# =============================================

_adapters: Dict[str, AgentAdapter] = {}


def get_agent_adapter(agent_type: Optional[str] = None) -> AgentAdapter:
    """根据 agent_type 返回对应的 adapter（字典缓存）

    Args:
        agent_type: agent 类型标识，None 时使用 DEFAULT_AGENT 配置

    Returns:
        AgentAdapter 实例（ClaudeAdapter 或 CodexAdapter）
    """
    if agent_type is None:
        from config import get_default_agent
        agent_type = get_default_agent()
    agent_type = agent_type.strip().lower()

    from config import VALID_AGENTS
    if agent_type not in VALID_AGENTS:
        raise ValueError(f"Unsupported agent_type: {agent_type}")

    if agent_type in _adapters:
        return _adapters[agent_type]

    if agent_type == 'codex':
        from agents.codex import CodexAdapter
        adapter = CodexAdapter()
    elif agent_type == 'claude':
        from agents.claude import ClaudeAdapter
        adapter = ClaudeAdapter()
    else:
        raise ValueError(f"Unsupported agent_type: {agent_type}")

    _adapters[agent_type] = adapter
    logger.info("Agent adapter initialized: %s", adapter.agent_type)
    return adapter


def get_all_agent_commands() -> Dict[str, List[str]]:
    """获取所有启用 agent 的命令映射

    Returns:
        如 {'claude': ['claude', 'claude --model opus'], 'codex': ['codex']}
    """
    from config import get_enabled_agents
    result: Dict[str, List[str]] = {}
    for at in get_enabled_agents():
        adapter = get_agent_adapter(at)
        result[at] = adapter.get_commands()
    return result


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
                 on_complete: CompleteCallback = None,
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
        on_complete: 成功完成回调 (agent_type, chat_id, message_id, session_id, output) -> None
        on_error: 错误通知回调 (agent_type, chat_id, message_id, session_id, error_msg) -> None

    Returns:
        (success, response): 成功时 response 包含 session_id（Codex 路径可能已 rename 为真实 ID）
    """
    from utils.shell import build_shell_cmd

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
        return False, {'error': error_msg, 'agent_type': agent_type}

    # ── 3. Session ID 捕获（Codex 新建会话时从 stdout 读取真实 ID）──
    did_capture = adapter.needs_output_session_id and session_mode == 'new'
    if did_capture:
        captured_session_id = _capture_session_id(adapter, proc, log_prefix)
        if not captured_session_id:
            # 捕获失败说明 CLI 启动异常，终止进程并报错
            proc.kill()
            proc.wait()
            error_msg = f"{adapter.display_name} 启动异常：未能在 {adapter.session_id_capture_timeout}s 内获取 session ID，请检查 CLI 是否正常"
            logger.error("%s %s", log_prefix, error_msg)
            return False, {'error': error_msg, 'agent_type': agent_type}
        if captured_session_id != session_id:
            new_id = _rename_session_in_store(
                session_id, captured_session_id, log_prefix)
            if new_id == captured_session_id:
                session_id = captured_session_id

    # ── 4. 检查进程状态并进入后台监控 ──
    # Codex 路径已花时间读 session ID，非阻塞检查即可（startup_wait=0）
    # Claude 路径等待 STARTUP_CHECK_SECONDS 秒看是否快速完成
    startup_wait = 0 if did_capture else STARTUP_CHECK_SECONDS
    return _check_and_monitor(
        proc, agent_type, session_id, chat_id, message_id,
        startup_wait=startup_wait,
        on_complete=on_complete, on_error=on_error)


# =============================================
# Session ID 捕获（内部）
# =============================================


def _capture_session_id(adapter: AgentAdapter, proc: subprocess.Popen,
                        log_prefix: str) -> Optional[str]:
    """从进程输出中捕获 session ID（Codex 路径）

    启动 daemon 线程逐行读取 stdout 放入 Queue，主线程带超时消费。
    跳过非 JSON 内容（如代理配置、ANSI 输出），直到找到可解析的
    session ID 或超时。

    使用线程+Queue 而非 select.select，避免 TextIOWrapper 内部缓冲
    导致 select 误报超时的问题。

    Args:
        adapter: agent 适配器（提供 parse_session_id 方法）
        proc: 已启动的子进程
        log_prefix: 日志前缀

    Returns:
        捕获到的 session ID，失败时返回 None
    """
    import time
    from queue import Queue, Empty

    timeout = adapter.session_id_capture_timeout
    deadline = time.monotonic() + timeout

    # daemon 线程逐行读取 stdout，EOF 或异常时放入 None 哨兵
    line_queue = Queue()

    def _reader():
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line_queue.put(line)
        except (OSError, ValueError):
            pass
        line_queue.put(None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("%s Session ID capture timed out after %ss",
                               log_prefix, timeout)
                break
            try:
                line = line_queue.get(timeout=remaining)
            except Empty:
                logger.warning("%s Session ID capture timed out after %ss",
                               log_prefix, timeout)
                break
            if line is None:
                # stdout 已关闭，读取退出码和 stderr 帮助诊断原因
                returncode = proc.poll()
                stderr_output = ''
                # 只在进程已退出时读 stderr，避免阻塞
                if returncode is not None and proc.stderr:
                    try:
                        stderr_output = proc.stderr.read(4096).strip()
                    except Exception:
                        pass
                logger.warning("%s stdout closed before session ID captured, "
                               "exit_code=%s, stderr=%s",
                               log_prefix, returncode,
                               stderr_output[:500] if stderr_output else '(empty)')
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
        from stores.session_chat_store import SessionChatStore
        store = SessionChatStore.get_instance()
        if store and store.rename_session(old_id, new_id):
            logger.info("%s Session ID renamed: %s -> %s",
                        log_prefix, old_id, new_id)
            return new_id
    except Exception as e:
        logger.warning("%s Failed to rename session: %s", log_prefix, e)
    return old_id


def _check_and_monitor(proc: subprocess.Popen, agent_type: str,
                       session_id: str, chat_id: str = '',
                       message_id: str = '',
                       startup_wait: float = 0,
                       on_complete: CompleteCallback = None,
                       on_error: ErrorCallback = None) -> Tuple[bool, Dict[str, Any]]:
    """检查进程状态并决定是否进入后台监控

    统一处理 Claude（等待 startup_wait 秒）和 Codex（session ID 捕获后
    非阻塞检查）两条路径的启动后逻辑。

    成功时 response 包含 session_id，调用方可直接使用（launch_agent
    已在捕获阶段完成 rename，此处的 session_id 即为最终值）。

    Args:
        proc: 子进程对象
        agent_type: agent 类型标识
        session_id: 会话 ID（已经过 rename，为最终值）
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        startup_wait: 等待进程退出的秒数（0 = 非阻塞 poll）
        on_complete: 成功完成回调（用于无 stop hook 的透传命令如 /compact）
        on_error: 错误通知回调

    Returns:
        (success, response): 成功时 response 包含 session_id
    """
    from utils.concurrency import run_in_background
    log_prefix = '[%s]' % agent_type

    # 等待进程退出或超时
    try:
        if startup_wait > 0:
            stdout, stderr = proc.communicate(timeout=startup_wait)
        else:
            # 非阻塞检查：进程可能已退出（Codex 路径，已花时间读 session ID）
            if proc.poll() is None:
                raise subprocess.TimeoutExpired(cmd='', timeout=0)
            stdout = proc.stdout.read() if proc.stdout else ''
            stderr = proc.stderr.read() if proc.stderr else ''
    except subprocess.TimeoutExpired:
        # 进程仍在运行，进入后台监控
        logger.info("%s Command is running in background", log_prefix)
        run_in_background(
            _monitor_startup,
            (proc, agent_type, session_id, chat_id, message_id, on_complete, on_error))
        return True, {'status': 'processing', 'session_id': session_id,
                      'agent_type': agent_type}

    # 进程已退出
    returncode = proc.returncode
    if returncode == 0:
        logger.info("%s Command completed quickly", log_prefix)
        if chat_id and on_complete:
            # 由 on_complete 回调统一处理通知和状态清理（如 /compact），
            # 网关侧无需再发通知
            run_in_background(on_complete,
                              (agent_type, chat_id, message_id, session_id,
                               (stdout or '')))
            return True, {'status': 'completed', 'session_id': session_id,
                          'agent_type': agent_type,
                          'notification_handled': True}
        return True, {'status': 'completed', 'session_id': session_id,
                      'agent_type': agent_type,
                      'output': (stdout or '')[:MAX_LOG_LENGTH * 2]}
    else:
        error_msg = _build_error_msg(agent_type, returncode, stdout, stderr)
        logger.warning("%s Command failed with exit code %s: %s",
                       log_prefix, returncode, error_msg[:MAX_LOG_LENGTH])
        if chat_id and on_error:
            run_in_background(on_error,
                              (agent_type, chat_id, message_id, session_id, error_msg))
        return False, {'error': error_msg, 'agent_type': agent_type}


# =============================================
# 进程监控（共享）
# =============================================


def _monitor_startup(proc: subprocess.Popen, agent_type: str,
                     session_id: str, chat_id: str = '',
                     message_id: str = '',
                     on_complete: CompleteCallback = None,
                     on_error: ErrorCallback = None) -> None:
    """后台短暂等待，捕获启动阶段的延迟失败

    只等待 STARTUP_TIMEOUT_SECONDS 秒。如果进程在此期间失败，
    通过 on_error 回调通知；如果仍在运行，交由 _monitor_detached 持续监控。

    Args:
        proc: 子进程对象
        agent_type: agent 类型标识
        session_id: 会话 ID
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        on_complete: 成功完成回调
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
            if chat_id and on_complete:
                on_complete(agent_type, chat_id, message_id, session_id,
                            stdout or '')
        else:
            error_msg = _build_error_msg(agent_type, proc.returncode, stdout, stderr)
            logger.warning(
                "%s Command failed with exit code %s, session: %s, error: %s",
                log_prefix, proc.returncode, session_id, error_msg[:MAX_LOG_LENGTH])
            if chat_id and on_error:
                on_error(agent_type, chat_id, message_id, session_id, error_msg)
    except subprocess.TimeoutExpired:
        logger.info(
            "%s Process still running after %ss, session: %s "
            "— detaching to background monitor",
            log_prefix, STARTUP_TIMEOUT_SECONDS, session_id)
        threading.Thread(
            target=_monitor_detached,
            args=(proc, agent_type, session_id, chat_id, message_id, on_complete, on_error),
            daemon=True
        ).start()
    except Exception as e:
        logger.error("%s Execution error: %s, session: %s",
                     log_prefix, e, session_id)
        if chat_id and on_error:
            on_error(agent_type, chat_id, message_id, session_id, str(e))


def _monitor_detached(proc: subprocess.Popen, agent_type: str,
                      session_id: str, chat_id: str = '',
                      message_id: str = '',
                      on_complete: CompleteCallback = None,
                      on_error: ErrorCallback = None) -> None:
    """后台持续读取 pipe 并等待子进程退出，失败时通过 on_error 通知

    通过后台线程排空 stdout/stderr 防止 buffer 满阻塞，
    同时保留尾部输出用于错误通知。

    Args:
        proc: 子进程对象（stdout/stderr 仍然打开）
        agent_type: agent 类型标识
        session_id: 会话 ID
        chat_id: 群聊 ID（用于异常通知）
        message_id: 用户消息 ID（用于回复式通知）
        on_complete: 成功完成回调
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
            if stdout_tail:
                logger.debug("%s stdout: %s...", log_prefix,
                             stdout_tail[:MAX_LOG_LENGTH])
            if chat_id and on_complete:
                on_complete(agent_type, chat_id, message_id, session_id,
                            stdout_tail or '')
        else:
            # 注意：用户通过飞书点击"拒绝并中断"时，hook 返回 interrupt: true，
            # Claude Code 会以 exit code=1 + "Execution error" 退出，也会走到这里。
            # 这属于用户主动中断的预期行为，但当前无法与真正的执行错误区分，
            # 因此统一作为错误通知。如需区分可在 resolve interrupt 时标记 session 状态。
            error_msg = _build_error_msg(agent_type, returncode, stdout_tail, stderr_tail)
            logger.warning(
                "%s Detached process failed (code=%s), session: %s, output: %s",
                log_prefix, returncode, session_id, error_msg[:MAX_LOG_LENGTH])
            if chat_id and on_error:
                on_error(agent_type, chat_id, message_id, session_id, error_msg)
    except Exception as e:
        logger.error("%s Error waiting for detached process: %s, session: %s",
                     log_prefix, e, session_id)
        if chat_id and on_error:
            on_error(agent_type, chat_id, message_id, session_id, str(e))


# =============================================
# 内部工具
# =============================================


def _build_error_msg(agent_type: str, returncode: int,
                     stdout: Optional[str], stderr: Optional[str]) -> str:
    """统一构建子进程失败时的错误信息

    claude -p 等 CLI 失败时常把错误打到 stdout 而非 stderr，
    故优先取 stderr，其次 stdout，两者都为空时用兜底文案。
    """
    error_output = (stderr or '').strip() or (stdout or '').strip()
    return error_output or f"{agent_type} 进程异常退出 (code={returncode})"


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
