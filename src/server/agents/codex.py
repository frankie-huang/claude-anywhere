"""OpenAI Codex CLI 适配器

实现 AgentAdapter 接口，封装 Codex CLI 的命令构建逻辑。
Codex 使用 `codex exec` 子命令进行非交互执行，通过 `--json`
标志输出 JSONL 事件流，从中捕获自动生成的 session ID。
"""

import json
import logging
import shlex
from typing import Dict, List, Optional

from agents import AgentAdapter

logger = logging.getLogger(__name__)


class CodexAdapter(AgentAdapter):
    """OpenAI Codex CLI 适配器"""

    @property
    def agent_type(self) -> str:
        return 'codex'

    @property
    def needs_output_session_id(self) -> bool:
        """Codex 新建会话时需要从输出中捕获 session ID"""
        return True

    def resolve_command(self, command_name: str = '') -> str:
        """解析 Codex 命令

        优先使用传入的 command_name，否则从配置列表取默认值。
        """
        if command_name:
            return command_name
        return self.get_commands()[0]

    def get_commands(self) -> List[str]:
        """获取 Codex 可用命令列表"""
        from config import get_codex_commands
        return get_codex_commands()

    def build_command_string(self, command_name: str, prompt: str,
                             session_id: str, session_mode: str,
                             project_dir: str) -> str:
        """构建 Codex CLI 完整命令字符串

        新建会话: codex exec --json --cd <dir> <prompt>
        恢复会话: codex exec resume <id> --json --cd <dir> <prompt>
        """
        from config import get_codex_args_template

        cmd = self.resolve_command(command_name)
        template = get_codex_args_template()
        cmd_argv = shlex.split(cmd)

        if session_mode == 'new':
            args_argv = ['exec', '--json', '--cd', project_dir, prompt]
        else:
            args_argv = ['exec', 'resume', session_id,
                         '--json', '--cd', project_dir, prompt]

        return _expand_template(template, cmd_argv, args_argv)

    def build_debug_command_string(self, command_name: str, session_id: str,
                                   session_mode: str) -> str:
        """构建日志版本的 Codex 命令（隐藏 prompt）"""
        from config import get_codex_args_template

        cmd = self.resolve_command(command_name)
        template = get_codex_args_template()
        cmd_argv = shlex.split(cmd)

        if session_mode == 'new':
            debug_args = ['exec', '--json', 'PROMPT']
        else:
            debug_args = ['exec', 'resume', session_id, '--json', 'PROMPT']

        return _expand_template(template, cmd_argv, debug_args)

    def parse_session_id(self, line: str) -> Optional[str]:
        """从 Codex --json 输出行解析 session ID

        Codex 首条事件为 {"type":"thread.started","thread_id":"xxx"}

        Args:
            line: stdout 的一行 JSONL 输出

        Returns:
            thread_id 字符串，解析失败时返回 None
        """
        try:
            event = json.loads(line)
            if event.get('type') == 'thread.started':
                thread_id = event.get('thread_id', '')
                if thread_id:
                    return thread_id
        except (ValueError, TypeError, AttributeError):
            pass
        return None


# =============================================
# Codex 专有工具函数
# =============================================


def _shlex_join(argv: List[str]) -> str:
    """shlex.join 的 Python 3.6 兼容版"""
    return ' '.join(shlex.quote(a) for a in argv)


def _expand_template(template: str, cmd_argv: List[str],
                     args_argv: List[str]) -> str:
    """根据模板把 cmd 和 args 组装成 shell 命令字符串

    语义与 agents.claude._expand_template 相同。
    """
    tokens = shlex.split(template, posix=False)
    cmd_joined = _shlex_join(cmd_argv)
    args_joined = _shlex_join(args_argv)

    result = []
    for tok in tokens:
        quoted = len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'")
        inner = tok[1:-1] if quoted else tok

        if inner == '{cmd}':
            result.extend(cmd_argv) if not quoted else result.append(cmd_joined)
        elif inner == '{args}':
            result.extend(args_argv) if not quoted else result.append(args_joined)
        else:
            replaced = inner.replace('{cmd}', cmd_joined).replace('{args}', args_joined)
            result.append(replaced)

    return _shlex_join(result)
