"""
Feishu Notify - /notify 命令处理

处理通知相关运行时配置：
- _parse_notify_args: 解析 /notify 子命令参数
- _handle_notify_command: /notify 命令处理入口
- _forward_notify_command: 转发 notify 到 callback
- _format_notify_result: 格式化 notify 结果
"""

import logging
import re
import shlex
from typing import Any, Dict, Tuple

from utils.concurrency import run_in_background

from .utils import _get_binding_from_event
from .message import _send_notice_message
from .forward import _forward_via_ws_or_http

logger = logging.getLogger(__name__)


# =============================================================================
# /notify 命令
# =============================================================================

_TIME_RANGE_RE = re.compile(
    r'^((?:[01]\d|2[0-3]):[0-5]\d|24:00)-((?:[01]\d|2[0-3]):[0-5]\d|24:00)$'
)


def _parse_notify_args(args: str) -> Tuple:
    """解析 /notify 子命令，返回 (action, ...) 元组。

    Returns:
        ('query',)                        — 查询所有通知配置
        ('set_at', at_user)               — 设置 @ 对象
        ('set_at_time', start, end)       — 设置 @ 时段
        ('clear_at_time',)                — 清除时段限制
        ('set_permission_delay', delay)   — 设置权限通知延迟
        ('clear_permission_delay',)       — 清除通知延迟覆盖

    Raises:
        ValueError: 参数格式错误
    """
    try:
        parts = shlex.split(args or '')
    except ValueError:
        raise ValueError('Invalid notify args')

    # /notify 或 /notify status → 查询所有配置
    if not parts or parts[0] == 'status':
        return ('query',)

    sub = parts[0]

    # /notify at ...
    if sub == 'at':
        if len(parts) == 1:
            raise ValueError('Missing at argument')
        if len(parts) != 2:
            raise ValueError('Invalid notify at args')
        value = parts[1]
        if value == 'always':
            return ('clear_at_time',)
        if re.match(r'^\d{2}:\d{2}-\d{2}:\d{2}$', value):
            m = _TIME_RANGE_RE.match(value)
            if not m:
                raise ValueError('Invalid time range format')
            return ('set_at_time', m.group(1), m.group(2))
        return ('set_at', value)

    # /notify delay ...
    if sub == 'delay':
        if len(parts) == 1:
            raise ValueError('Missing delay argument')
        if len(parts) != 2:
            raise ValueError('Invalid notify delay args')
        value = parts[1]
        if value == 'default':
            return ('clear_permission_delay',)
        try:
            delay = int(value)
        except ValueError:
            raise ValueError('Delay must be a non-negative integer')
        if delay < 0 or delay > 86400:
            raise ValueError('Delay must be 0-86400')
        return ('set_permission_delay', delay)

    raise ValueError('Unsupported notify command')


def _handle_notify_command(data: dict, args: str) -> None:
    """处理 /notify 命令：管理通知相关运行时配置。"""
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')

    binding = _get_binding_from_event(event)
    if not binding:
        return

    try:
        parsed = _parse_notify_args(args)
    except ValueError:
        run_in_background(
            _send_notice_message,
            (chat_id,
             "用法：\n"
             "  `/notify status` — 查看当前配置\n"
             "  `/notify at self|all|off|<user_id>` — 设置 @ 对象\n"
             "  `/notify at HH:MM-HH:MM` — 设置 @ 时段\n"
             "  `/notify at always` — 清除时段限制\n"
             "  `/notify delay <秒>` — 设置权限通知延迟\n"
             "  `/notify delay default` — 恢复默认权限通知延迟",
             message_id)
        )
        return

    run_in_background(
        _forward_notify_command,
        (binding, parsed, chat_id, message_id)
    )


# =============================================================================
# /notify 转发
# =============================================================================

def _forward_notify_command(binding: Dict[str, Any], parsed: Tuple,
                            chat_id: str, message_id: str) -> None:
    """转发 /notify 命令到 Callback 后端并反馈结果。"""
    action = parsed[0]
    payload = {'action': action}

    if action == 'set_at':
        payload['at_user'] = parsed[1]
    elif action == 'set_at_time':
        payload['at_start'] = parsed[1]
        payload['at_end'] = parsed[2]
    elif action == 'set_permission_delay':
        payload['delay'] = parsed[1]

    try:
        resp = _forward_via_ws_or_http(binding, '/cb/notify/config', payload)
    except Exception as e:
        logger.error("[feishu] /cb/notify/config error: %s", e)
        resp = None

    if not resp or not resp.get('ok'):
        _send_notice_message(chat_id, "通知配置操作失败，请查看日志。", message_id)
        return

    _send_notice_message(chat_id, _format_notify_result(action, resp), message_id)


def _format_notify_result(action: str, resp: Dict[str, Any]) -> str:
    """格式化 /notify 操作结果的反馈文案。"""
    config = resp.get('config', {})
    at_user = config.get('at_user', '')
    at_start = config.get('at_start', '')
    at_end = config.get('at_end', '')

    if action == 'query':
        parts = []
        # @ 对象
        if not at_user or at_user == 'self':
            parts.append("@ 对象：自己（默认）")
        elif at_user == 'off':
            parts.append("@ 对象：已关闭")
        elif at_user == 'all':
            parts.append("@ 对象：所有人")
        else:
            parts.append("@ 对象：`%s`" % at_user)
        # @ 时段
        if at_start and at_end:
            end_label = "次日 %s" % at_end if at_start > at_end else at_end
            parts.append("@ 时段：%s - %s" % (at_start, end_label))
        else:
            parts.append("@ 时段：全天")
        # 权限通知延迟
        delay = config.get('permission_delay')
        if delay is not None:
            parts.append("权限通知延迟：%d 秒%s" % (delay, "（立即发送）" if delay == 0 else ""))
        else:
            parts.append("权限通知延迟：默认")
        return '\n'.join(parts)

    if action == 'clear_at_time':
        return "已清除通知时段限制。"

    if action == 'set_at_time':
        end_label = "次日 %s" % at_end if at_start > at_end else at_end
        return "已设置通知 @ 时段：%s - %s。" % (at_start, end_label)

    if action == 'set_at':
        if at_user == 'off':
            return "已关闭通知 @。"
        if at_user == 'all':
            return "已设置通知 @ 所有人。"
        if at_user == 'self':
            return "已恢复默认通知 @ 配置（@ 自己）。"
        return "已设置通知 @ `%s`。" % at_user

    if action == 'set_permission_delay':
        delay = config.get('permission_delay', 0)
        return "已设置权限通知延迟：%d 秒%s。" % (delay, "（立即发送）" if delay == 0 else "")

    if action == 'clear_permission_delay':
        return "已恢复默认权限通知延迟。"

    return "未知操作。"
