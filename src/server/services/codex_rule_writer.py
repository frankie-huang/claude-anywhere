"""Codex execpolicy rule writer.

Codex does not support Claude's PermissionRequest ``updatedPermissions`` field.
For "always allow" decisions we persist an execpolicy prefix rule and then
return a normal allow decision to the hook.
"""

import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _resolve_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(raw).expanduser()


def _extract_command_tokens(tool_name: str, tool_input: Dict[str, Any]) -> List[str]:
    """Extract Codex execpolicy command tokens from a permission request."""
    cmd = tool_input.get("cmd")
    if isinstance(cmd, list) and all(isinstance(item, str) for item in cmd):
        return [item for item in cmd if item]

    command = tool_input.get("command")
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        return [item for item in command if item]
    if isinstance(command, str) and command.strip():
        return shlex.split(command)

    # Some Codex hook payloads name the shell tool directly and put the argv in
    # args/argv. Keep this permissive because the upstream schema is still moving.
    for key in ("args", "argv"):
        value = tool_input.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [item for item in value if item]

    if tool_name and tool_name not in ("unknown", "shell", "Bash"):
        logger.warning("Cannot derive Codex prefix rule for non-shell tool: %s", tool_name)
    return []


def format_codex_prefix_rule(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Return a Starlark prefix_rule line for Codex, or None if unsupported."""
    try:
        tokens = _extract_command_tokens(tool_name, tool_input)
    except ValueError as exc:
        logger.warning("Failed to parse Codex command for prefix rule: %s", exc)
        return None

    if not tokens:
        return None

    pattern = json.dumps(tokens, ensure_ascii=False)
    return f'prefix_rule(pattern={pattern}, decision="allow")'


def write_codex_always_allow_rule(
    tool_name: str,
    tool_input: Dict[str, Any]
) -> bool:
    """Append a Codex prefix_rule to $CODEX_HOME/rules/default.rules."""
    rule = format_codex_prefix_rule(tool_name, tool_input)
    if not rule:
        logger.error("Cannot write Codex always-allow rule: unsupported request")
        return False

    rules_file = _resolve_codex_home() / "rules" / "default.rules"

    try:
        rules_file.parent.mkdir(parents=True, exist_ok=True)
        raw = rules_file.read_text(encoding="utf-8") if rules_file.exists() else ""
        if rule in raw.splitlines():
            logger.info("Codex always-allow rule already exists: %s", rule)
            return True

        with open(rules_file, "a", encoding="utf-8") as f:
            # 确保不会拼接到上一行末尾（防止文件缺少末尾换行）
            if raw and not raw.endswith("\n"):
                f.write("\n")
            f.write(rule + "\n")

        logger.info("Added Codex always-allow rule to %s: %s", rules_file, rule)
        return True
    except Exception as exc:
        logger.error("Failed to write Codex always-allow rule: %s", exc)
        return False
