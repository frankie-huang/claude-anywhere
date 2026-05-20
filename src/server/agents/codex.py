"""OpenAI Codex CLI 适配器

实现 AgentAdapter 接口，封装 Codex CLI 的命令构建逻辑。
Codex 使用 `codex exec` 子命令进行非交互执行，通过 `--json`
标志输出 JSONL 事件流，从中捕获自动生成的 session ID。
"""

import json
import logging
import shlex
from typing import Dict, List, Optional

from agents import AgentAdapter, expand_template

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

    @property
    def session_id_capture_timeout(self) -> int:
        """Codex 首条 JSONL 事件等待时间"""
        return 10

    def get_commands(self) -> List[str]:
        """获取 Codex 可用命令列表"""
        from config import get_codex_commands
        return get_codex_commands()

    def build_command_string(self, command_name: str, prompt: str,
                             session_id: str, session_mode: str,
                             project_dir: str) -> str:
        """构建 Codex CLI 完整命令字符串

        新建会话: codex exec --json --cd <dir> <prompt>
        恢复会话: codex exec resume --json <id> <prompt>

        沙箱模式由用户通过 CODEX_COMMAND 或 ~/.codex/config.toml 自行配置。
        """
        from config import get_codex_args_template

        cmd = self.resolve_command(command_name)
        template = get_codex_args_template()
        cmd_argv = shlex.split(cmd)

        if session_mode == 'new':
            args_argv = ['exec', '--json', '--cd', project_dir, prompt]
        else:
            # resume 不支持 --cd，工作目录由原始会话决定
            args_argv = ['exec', 'resume', '--json', session_id, prompt]

        return expand_template(template, cmd_argv, args_argv)

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
            debug_args = ['exec', 'resume', '--json', session_id, 'PROMPT']

        return expand_template(template, cmd_argv, debug_args)

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


