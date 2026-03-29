#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Permission approval MCP server.
Bridges Claude CLI --permission-prompt-tool requests to the existing hook system.

Usage:
    python3 permission_mcp.py --cwd <project_dir> --session-id <session_id>

    # 完整调用示例
    claude -p "your prompt" \
        --permission-prompt-tool mcp__approver__permission_request \
        --mcp-config '{"mcpServers":{"approver":{"command":"python3","args":["/path/to/permission_mcp.py","--cwd","/project/dir","--session-id","abc123"]}}}'

MCP 输入（Claude CLI → 本脚本，通过 tools/call）:
    {
        "tool_name": "Bash",                          # 请求权限的工具名
        "input": {"command": "npm test", ...},         # 工具的输入参数
        "tool_use_id": "toolu_01ABC123..."             # 工具调用唯一 ID（可选）
    }

MCP 输出（本脚本 → Claude CLI）:
    允许: {"behavior": "allow", "updatedInput": {<原始 input>}}
    拒绝: {"behavior": "deny", "message": "原因"}
    拒绝并中断: {"behavior": "deny", "message": "原因", "interrupt": true}

    注意: behavior 为 allow 时 updatedInput 是必需的（CLI 会做 schema 校验），
    本脚本会自动补充。输出包装在 MCP tool result 格式中:
    {"content": [{"type": "text", "text": "<上述 JSON 字符串>"}]}

本脚本 → hook 脚本的输入（构造的 hook 事件 JSON）:
    {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test", ...},
        "session_id": "abc123",                        # 来自 --session-id 参数
        "cwd": "/project/dir",                         # 来自 --cwd 参数
        "tool_use_id": "toolu_01ABC123..."             # 来自 MCP 输入（可选）
    }

    与 CLI 原生 PermissionRequest hook 输入的差异:
    ┌─────────────────────┬──────────┬──────────┬──────────────────────────┐
    │ 字段                │ CLI 原生 │ MCP 模式 │ 说明                     │
    ├─────────────────────┼──────────┼──────────┼──────────────────────────┤
    │ session_id          │ ✓        │ ✓        │ MCP 从命令行参数获取     │
    │ cwd                 │ ✓        │ ✓        │ MCP 从命令行参数获取     │
    │ tool_name           │ ✓        │ ✓        │                          │
    │ tool_input          │ ✓        │ ✓        │                          │
    │ tool_use_id         │ ✗        │ ✓        │ MCP 模式下 CLI 会传递    │
    │ hook_event_name     │ ✓        │ ✓        │                          │
    │ transcript_path     │ ✓        │ ✗        │ MCP 模式不需要（见下文） │
    │ permission_mode     │ ✓        │ ✗        │ hook 脚本未使用          │
    │ permission_suggestions │ ✓     │ ✗        │ hook 脚本未使用          │
    └─────────────────────┴──────────┴──────────┴──────────────────────────┘

    transcript_path 缺失的影响:
    - permission.sh 用它在延迟期间检测用户是否已在终端做出决策
    - MCP 模式下设置 MCP_MODE=1，NOTIFY_DELAY=0，延迟检测被跳过
    - 因此 transcript_path 在 MCP 执行路径中不会被用到
    - 该路径由 Claude Code 内部生成，编码规则无文档保证，不宜自行构造

hook 脚本 → 本脚本的输出:
    {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"}
                      或 {"behavior": "deny", "message": "..."}
                      或 {"behavior": "deny", "message": "...", "interrupt": true}
        }
    }
"""
import sys
import os
import json
import subprocess
import argparse

from typing import Dict, Any, Tuple, Optional

# 加入模块搜索路径（本文件作为独立进程运行，需手动设置）
# 本文件位于 src/server/handlers/，向上两级到 src/server（用于 from handlers.utils）
# 再向上一级到 src（用于 from shared.logging_config）
_server_dir = os.path.dirname(os.path.dirname(__file__))
_src_dir = os.path.dirname(_server_dir)
sys.path.insert(0, _server_dir)
sys.path.insert(0, _src_dir)

from handlers.utils import build_shell_cmd  # noqa: E402

try:
    from shared.logging_config import setup_logging  # noqa: E402
    logger = setup_logging('permission_mcp', console=False, propagate=False)
except ImportError:
    # 目录结构不匹配时回退到标准 logging，写入项目 log 目录
    import logging
    _log_dir = os.path.join(os.path.dirname(_src_dir), 'log', 'permission_mcp')
    os.makedirs(_log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        filename=os.path.join(_log_dir, 'fallback.log'),
        format='[%(process)d] %(asctime)s [%(levelname)s] %(message)s'
    )
    logger = logging.getLogger('permission_mcp')

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 600


class PermissionMCPServer:
    """MCP server that bridges permission requests to the existing hook system."""

    def __init__(self, session_id: str, project_cwd: str):
        self.session_id = session_id
        self.project_cwd = project_cwd

    def get_permission_hook_config(self) -> Optional[Tuple[str, int]]:
        """
        从 settings.json 读取 PermissionRequest hook 配置。

        查找顺序：
            1. 项目级: <project_cwd>/.claude/settings.local.json
            2. 全局级: ~/.claude/settings.json

        Returns:
            (hook_command, timeout): hook 命令和超时时间（秒），未找到配置时返回 None
        """
        # 配置文件路径列表（按优先级）
        config_paths = []
        if self.project_cwd:
            config_paths.append(os.path.join(self.project_cwd, ".claude", "settings.local.json"))
            config_paths.append(os.path.join(self.project_cwd, ".claude", "settings.json"))
        config_paths.append(os.path.expanduser("~/.claude/settings.local.json"))
        config_paths.append(os.path.expanduser("~/.claude/settings.json"))

        for config_path in config_paths:
            if not os.path.isfile(config_path):
                continue

            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                # 解析 hooks.PermissionRequest[0].hooks[0]
                hooks_config = config.get("hooks", {})
                perm_hooks = hooks_config.get("PermissionRequest", [])

                if perm_hooks and isinstance(perm_hooks, list):
                    # 遍历所有 matcher 配置，找到第一个有 hooks 的
                    # 注意：此处不做 matcher 条件过滤（如 tool_name 匹配），
                    # 仅取第一个包含有效 command hook 的配置项。
                    # 当前场景下 PermissionRequest 只配置了一个 hook，如需支持
                    # 多 matcher 按条件分发，需扩展此逻辑。
                    for matcher_config in perm_hooks:
                        hooks_list = matcher_config.get("hooks", [])
                        if hooks_list and isinstance(hooks_list, list):
                            first_hook = hooks_list[0]
                            if isinstance(first_hook, dict):
                                # 只处理 type 为 command 的 hook
                                hook_type = first_hook.get("type", "command")
                                if hook_type != "command":
                                    continue
                                cmd = first_hook.get("command")
                                if not (cmd and isinstance(cmd, str)):
                                    continue  # 没有 command 字段，跳过
                                hook_command = cmd
                                t = first_hook.get("timeout")
                                timeout = int(t) if isinstance(t, (int, float)) and t > 0 else DEFAULT_TIMEOUT
                                logger.info("Loaded hook config from %s: cmd=%s, timeout=%d",
                                            config_path, hook_command, timeout)
                                return hook_command, timeout

            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to read config %s: %s", config_path, e)
                continue

        logger.info("No PermissionRequest hook configured in settings")
        return None

    def call_hook_router(self, tool_name: str, tool_input: Dict[str, Any], tool_use_id: str) -> Dict[str, Any]:
        """
        Invoke hook script with simulated PermissionRequest hook event.
        Returns the decision from the hook system.

        Args:
            tool_name: Name of the tool requesting permission
            tool_input: Input parameters for the tool
            tool_use_id: Unique ID for this tool use (provided by Claude CLI in MCP mode)
        """
        # 从 settings 读取 hook 配置（脚本路径和超时时间）
        hook_config = self.get_permission_hook_config()
        if hook_config is None:
            # 没有配置 PermissionRequest hook，返回 deny
            return {"behavior": "deny", "message": "No PermissionRequest hook configured"}

        hook_command, timeout = hook_config

        # 构造模拟的 PermissionRequest hook 事件 JSON
        hook_event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": self.session_id,
            "cwd": self.project_cwd,
            "tool_use_id": tool_use_id,
        }

        # 设置 MCP_MODE 环境变量，让 permission.sh 跳过延迟等待
        env = os.environ.copy()
        env["MCP_MODE"] = "1"

        # 调试日志（hook_event 可能包含敏感的 tool_input，仅在 debug 级别记录完整内容）
        logger.info("Calling hook: tool=%s, tool_use_id=%s", tool_name, tool_use_id)
        logger.debug("Hook event detail: %s", json.dumps(hook_event))

        shell = os.environ.get('SHELL', '/bin/bash')
        cmd = build_shell_cmd(shell, hook_command)

        try:
            # Python 3.6 兼容：使用 stdout/stderr PIPE 和 universal_newlines
            result = subprocess.run(
                cmd,
                input=json.dumps(hook_event),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
                cwd=self.project_cwd,
                env=env
            )

            # 调试日志
            logger.info("Return code: %d", result.returncode)
            logger.debug("Stdout: %s", result.stdout)
            logger.debug("Stderr: %s", result.stderr)

            # hook-router.sh 成功执行
            if result.returncode == 0 and result.stdout.strip():
                return parse_hook_output(result.stdout.strip())
            else:
                # 非 0 返回码通常表示回退到终端交互
                # 在 MCP 模式下，我们应该返回 deny
                error_msg = result.stderr.strip() if result.stderr else "Permission request fallback"
                return {"behavior": "deny", "message": error_msg}

        except subprocess.TimeoutExpired:
            return {"behavior": "deny", "message": "Permission request timed out"}
        except Exception as e:
            return {"behavior": "deny", "message": str(e)}

    def handle_request(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle a JSON-RPC request from Claude CLI.

        Returns:
            dict for normal result, None for unknown method (caller sends JSON-RPC error)
        """
        method = request.get("method")

        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "approver", "version": "1.0.0"}
            }

        if method == "notifications/initialized":
            # MCP 协议：initialize 完成后客户端发送此通知，属于 JSON-RPC notification（无 id），
            # 服务端不应返回响应，否则违反 JSON-RPC 2.0 规范，可能导致客户端异常
            return None

        if method == "tools/list":
            return {
                "tools": [{
                    "name": "permission_request",
                    "description": "Handle permission approval for tool execution via Feishu interactive card",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "description": "Name of the tool requesting permission"
                            },
                            "input": {
                                "type": "object",
                                "description": "Input parameters for the tool"
                            },
                            "tool_use_id": {
                                "type": "string",
                                "description": "Unique identifier for this tool use"
                            }
                        },
                        "required": ["tool_name", "input"]
                    }
                }]
            }

        if method == "tools/call":
            params = request.get("params", {})
            # 校验工具名，防止 CLI 调用了未注册的工具名时静默处理
            if params.get("name") != "permission_request":
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"behavior": "deny", "message": "Unknown tool: {}".format(params.get("name"))})
                    }],
                    "isError": True
                }
            args = params.get("arguments", {})
            tool_name = args.get("tool_name", "unknown")
            tool_input = args.get("input", {})
            tool_use_id = args.get("tool_use_id", "")

            decision = self.call_hook_router(tool_name, tool_input, tool_use_id)

            # 确保 decision 格式符合 Claude CLI 要求
            # - behavior: "allow" 时需要提供 updatedInput
            # - behavior: "deny" 时需要提供 message
            # - interrupt 字段会被原样传递，Claude CLI 支持
            if decision.get("behavior") == "allow" and "updatedInput" not in decision:
                decision["updatedInput"] = tool_input

            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(decision)
                }]
            }

        # 未知方法：返回 None，由 run() 判断是否需要响应
        # 如果有 id（是 request），run() 会发送 error 响应
        return None

    def run(self) -> None:
        """MCP stdio transport: read JSON-RPC from stdin, write to stdout."""
        logger.info("Session ID: %s, CWD: %s", self.session_id, self.project_cwd)

        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON request: %s", line[:100])
                continue

            method = request.get("method", "")
            logger.debug("Request: %s", json.dumps(request))

            result = self.handle_request(request)

            # JSON-RPC 2.0 规范：通知（notification）没有 id 字段，服务端不得返回响应。
            # MCP 中典型的通知有 notifications/initialized、notifications/cancelled 等。
            if "id" not in request:
                logger.info("Notification handled: %s", method)
                continue

            # handle_request 返回 None 表示未知方法，返回 JSON-RPC error
            if result is None:
                response = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32601, "message": "Method not found: {}".format(method)}
                }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": result
                }

            logger.debug("Response: %s", json.dumps(response))

            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def parse_hook_output(output: str) -> Dict[str, Any]:
    """Parse hook output and extract decision."""
    if not output or not output.strip():
        return {"behavior": "deny", "message": "Empty hook output"}

    try:
        data = json.loads(output.strip())
        hook_output = data.get("hookSpecificOutput", {})
        decision = hook_output.get("decision", {})
        if decision:
            return decision
        return {"behavior": "deny", "message": "Missing decision in hook output"}
    except (json.JSONDecodeError, ValueError) as e:
        return {"behavior": "deny", "message": "Invalid JSON: {}".format(str(e))}


def parse_args() -> Tuple[str, str]:
    """解析命令行参数，返回 (session_id, cwd)"""
    parser = argparse.ArgumentParser(description='Permission approval MCP server')
    parser.add_argument('--cwd', dest='cwd', required=True,
                        help='Project working directory for permission context')
    parser.add_argument('--session-id', dest='session_id', required=True,
                        help='Claude session ID for permission context')
    args = parser.parse_args()
    return args.session_id, args.cwd


def main() -> None:
    session_id, project_cwd = parse_args()
    server = PermissionMCPServer(session_id, project_cwd)
    server.run()


if __name__ == "__main__":
    main()
