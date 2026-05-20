"""Claude Code CLI 适配器

实现 AgentAdapter 接口，封装 Claude CLI 的命令构建、
MCP 权限审批配置、参数模板展开等协议细节。
"""

import json
import logging
import os
import shlex
import sys
from typing import Dict, List

from agents import AgentAdapter, expand_template

logger = logging.getLogger(__name__)

# MCP 配置
MCP_TOOL_NAME = "mcp__approver__permission_request"


class ClaudeAdapter(AgentAdapter):
    """Claude Code CLI 适配器"""

    @property
    def agent_type(self) -> str:
        return 'claude'

    @property
    def display_name(self) -> str:
        return 'Claude Code'

    def get_commands(self) -> List[str]:
        """获取 Claude 可用命令列表"""
        from config import get_claude_commands
        return get_claude_commands()

    def build_command_string(self, command_name: str, prompt: str,
                             session_id: str, session_mode: str,
                             project_dir: str) -> str:
        """构建 Claude CLI 完整命令字符串

        组装参数列表，通过模板展开生成可执行的 shell 命令。
        """
        from config import get_claude_args_template

        cmd = self.resolve_command(command_name)
        template = get_claude_args_template()
        cmd_argv = shlex.split(cmd)

        session_flag = '--session-id' if session_mode == 'new' else '--resume'
        mcp_argv = _get_mcp_args(project_dir, session_id)
        # -- 分隔符确保 prompt 中的 --flag 不会被 CLI 误解析为参数
        args_argv = ['--print', session_flag, session_id] + mcp_argv + ['--', prompt]

        return expand_template(template, cmd_argv, args_argv)

    def build_debug_command_string(self, command_name: str, session_id: str,
                                   session_mode: str) -> str:
        """构建日志版本的 Claude 命令（隐藏 prompt 和 MCP 参数）"""
        from config import get_claude_args_template

        cmd = self.resolve_command(command_name)
        template = get_claude_args_template()
        cmd_argv = shlex.split(cmd)

        session_flag = '--session-id' if session_mode == 'new' else '--resume'
        debug_args = ['--print', session_flag, session_id, '--', 'PROMPT']

        return expand_template(template, cmd_argv, debug_args)

    def build_env(self, base_env: Dict[str, str]) -> Dict[str, str]:
        """清除 CLAUDECODE 环境变量，避免嵌套会话检测阻止子会话启动

        参考: https://code.claude.com/docs/en/headless
        """
        base_env.pop('CLAUDECODE', None)
        return base_env


# =============================================
# Claude 专有工具函数
# =============================================


def _get_mcp_args(project_dir: str, session_id: str) -> List[str]:
    """获取 MCP 审批相关的命令行参数

    动态构建 MCP 配置 JSON 字符串，不依赖外部配置文件。
    通过 args 参数将 project_dir 和 session_id 传递给 MCP server。

    Args:
        project_dir: 项目工作目录，传递给 MCP server 用于权限审批上下文
        session_id: Claude 会话 ID，传递给 MCP server 用于权限审批上下文

    Returns:
        MCP 相关参数 argv 列表(未经 shell quote), MCP 脚本缺失时返回空列表
    """
    # 动态定位 MCP 脚本路径（与 handlers/claude.py 同级目录下的 permission_mcp.py）
    handlers_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'handlers')
    mcp_script = os.path.join(handlers_dir, "permission_mcp.py")

    if not os.path.exists(mcp_script):
        logger.debug("[mcp] MCP script not found: %s", mcp_script)
        return []

    # sys.executable 返回启动本进程的 Python 解释器路径，
    # 确保 MCP 子进程与服务使用同一 Python 环境
    python_cmd = sys.executable or "python3"
    mcp_config = {
        "mcpServers": {
            "approver": {
                "command": python_cmd,
                "args": [mcp_script, "--cwd", project_dir,
                         "--session-id", session_id]
            }
        }
    }

    config_json = json.dumps(mcp_config)
    logger.info("[mcp] Using MCP config: script=%s, session=%s, cwd=%s",
                mcp_script, session_id, project_dir)
    return ['--permission-prompt-tool', MCP_TOOL_NAME, '--mcp-config', config_json]


