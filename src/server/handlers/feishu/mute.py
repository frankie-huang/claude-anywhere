"""
Mute / Unmute - /mute /unmute 命令处理 + 静音列表卡片

从 __init__.py 拆分，保持原有逻辑不变。
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from utils.concurrency import run_in_background
from services.session_facade import SessionFacade

from .utils import (
    _SESSION_NOT_FOUND_HINT, _SESSION_UNRESOLVED_HINT,
    _get_binding_from_event,
)
from .message import _send_notice_message

logger = logging.getLogger(__name__)


# =============================================================================
# 静音：/mute /unmute 命令 + 自动解除
# =============================================================================
# mute 状态由 SessionFacade 管理：callback 端 session_chat_store 为权威源，
# gateway 端本地缓存（write-through + lazy read-through）。
# handle_send_message 的出站拦截稳态下命中缓存零 RPC。

def _pick_mute_target(route_info: Dict[str, str], chat_id: str,
                      message_id: str) -> str:
    """从已解析好的 route_info 中提取目标 session_id，无法解析时给用户反馈

    仅用于用户主动命令（/mute、/unmute）。被动钩子（auto_unmute）自己内联判断，
    不经过此函数。

    Returns:
        解析成功返回 session_id，失败返回 ''
    """
    source = route_info.get('source', '')
    if SessionFacade.RouteSource.is_resolved(source):
        return route_info.get('session_id', '')
    hint = (_SESSION_NOT_FOUND_HINT
            if SessionFacade.RouteSource.is_parent_not_found(source)
            else _SESSION_UNRESOLVED_HINT)
    run_in_background(_send_notice_message, (chat_id, hint, message_id))
    return ''


def _handle_mute_command(data: dict, args: str) -> None:
    """处理 /mute 命令：静音会话、目录或查看静音列表

    用法：
        /mute                — 静音当前会话
        /mute <session_id>   — 静音指定会话，需要指定完整的 session_id
        /mute /path          — 静音指定目录（以 / 开头的参数视为目录）
        /mute /path/**       — 递归静音指定目录及其所有子孙目录
        /mute list           — 查看所有静音和加白规则
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')

    binding = _get_binding_from_event(event)
    if not binding:
        return

    trimmed = args.strip()

    # /mute list → 查看静音列表
    if trimmed == 'list':
        run_in_background(_send_mute_list_card, (binding, chat_id, message_id))
        return

    # 参数以 / 开头 → 目录 mute
    if trimmed.startswith('/'):
        recursive = trimmed.endswith('/**')
        dir_path = trimmed[:-3] if recursive else trimmed
        if not dir_path:
            dir_path = '/'
        result = SessionFacade.mute_dir(binding, dir_path, recursive=recursive)
        if result is None:
            run_in_background(_send_notice_message,
                               (chat_id, "目录静音操作失败，请查看日志。", message_id))
            return
        display = f"`{dir_path.rstrip('/')}/**`" if recursive else f"`{dir_path}`"
        text = f"目录 {display} {result['message']}"
        run_in_background(_send_notice_message, (chat_id, text, message_id))
        return

    # 非空参数（非 list、非 / 开头）→ 视为 session-id 直接 mute
    if trimmed:
        changed = SessionFacade.mute(binding, trimmed)
        if changed is None:
            run_in_background(_send_notice_message,
                               (chat_id, "静音操作失败，请确认 session ID 是否完整且正确。", message_id))
            return
        text = (f"已静音 session `{trimmed[:8]}`，后续消息将不再推送到此处。"
                if changed else f"session `{trimmed[:8]}` 已处于静音状态。")
        run_in_background(_send_notice_message, (chat_id, text, message_id))
        return

    # 无参数 → 静音当前会话
    route_info = SessionFacade.resolve_from_message(data, binding)
    session_id = _pick_mute_target(route_info, chat_id, message_id)
    if not session_id:
        return

    changed = SessionFacade.mute(binding, session_id)
    if changed is None:
        run_in_background(_send_notice_message,
                           (chat_id, "静音操作失败，请查看日志。", message_id))
        return

    sid_tag = f"session `{session_id[:8]}`"
    text = (f"已静音 {sid_tag}，后续消息将不再推送到此处。发送消息继续会话时会自动解除静音，也可通过 /unmute 手动解除。" if changed
            else f"{sid_tag} 已处于静音状态。")
    run_in_background(_send_notice_message, (chat_id, text, message_id))


def _handle_unmute_command(data: dict, args: str) -> None:
    """处理 /unmute 命令：解除静音 / 标记目录为不静音

    用法：
        /unmute                — 解除当前会话静音
        /unmute <session_id>   — 解除指定会话静音，需要指定完整的 session_id
        /unmute /path          — 解除目录静音，或标记为不静音
        /unmute /path/**       — 解除目录递归静音，或标记目录及其所有子目录为不静音
    """
    event = data.get('event', {})
    message = event.get('message', {})
    chat_id = message.get('chat_id', '')
    message_id = message.get('message_id', '')

    binding = _get_binding_from_event(event)
    if not binding:
        return

    trimmed = args.strip()

    # 参数以 / 开头 → 目录 unmute
    if trimmed.startswith('/'):
        recursive = trimmed.endswith('/**')
        dir_path = trimmed[:-3] if recursive else trimmed
        if not dir_path:
            dir_path = '/'
        result = SessionFacade.unmute_dir(binding, dir_path, recursive=recursive)
        if result is None:
            run_in_background(_send_notice_message,
                               (chat_id, "目录加白操作失败，请查看日志。", message_id))
            return
        display = f"`{dir_path.rstrip('/')}/**`" if recursive else f"`{dir_path}`"
        text = f"目录 {display} {result['message']}"
        run_in_background(_send_notice_message, (chat_id, text, message_id))
        return

    # 非空参数（非 / 开头）→ 视为 session-id 直接 unmute
    if trimmed:
        changed = SessionFacade.unmute(binding, trimmed)
        if changed is None:
            run_in_background(_send_notice_message,
                               (chat_id, "解除静音失败，请确认 session ID 是否完整且正确。", message_id))
            return
        text = (f"已解除 session `{trimmed[:8]}` 的静音。"
                if changed else f"session `{trimmed[:8]}` 当前未处于静音状态。")
        run_in_background(_send_notice_message, (chat_id, text, message_id))
        return

    # 无参数 → 解除当前会话静音
    route_info = SessionFacade.resolve_from_message(data, binding)
    session_id = _pick_mute_target(route_info, chat_id, message_id)
    if not session_id:
        return

    changed = SessionFacade.unmute(binding, session_id)
    if changed is None:
        run_in_background(_send_notice_message,
                           (chat_id, "解除静音失败，请查看日志。", message_id))
        return

    sid_tag = f"session `{session_id[:8]}`"
    text = (f"已解除 {sid_tag} 的静音。" if changed
            else f"{sid_tag} 当前未处于静音状态。")
    run_in_background(_send_notice_message, (chat_id, text, message_id))


def _send_mute_list_card(binding: Dict[str, Any], chat_id: str,
                         reply_to: str) -> None:
    """构建并发送静音列表卡片（后台线程调用）"""
    from services.feishu_api import FeishuAPIService

    result = SessionFacade.list_muted(binding)
    if result is None:
        _send_notice_message(chat_id, "查询静音列表失败，请查看日志。", reply_to)
        return

    sessions = result.get('sessions', [])
    dirs = result.get('dirs', [])
    if not sessions and not dirs:
        _send_notice_message(chat_id, "当前没有任何静音和加白规则。", reply_to)
        return

    # 构建卡片元素列表，目录块和会话块用 column_set 背景色区分
    elements = [{"tag": "markdown", "content": (
        "> **目录：**`/mute /path`  `/unmute /path`  加 `/**` 含子目录\n"
        "> **会话：**`/mute` `/unmute`（在会话内），后接 session_id 可指定会话"
    )}]
    if dirs:
        dir_parts = [f"**目录规则 ({len(dirs)})**", ""]
        from stores.directory_store import (
            S_MUTED, S_MUTED_RECURSIVE, S_MUTED_CHILDREN,
            S_UNMUTED, S_UNMUTED_RECURSIVE,
        )
        _status_labels = {
            S_MUTED: '静音·自身',
            S_MUTED_RECURSIVE: '静音·自身+子目录',
            S_MUTED_CHILDREN: '静音·仅子目录',
            S_UNMUTED: '不静音·自身',
            S_UNMUTED_RECURSIVE: '不静音·自身+子目录',
        }
        for d in dirs:
            ts = max(d.get('muted_at', 0), d.get('unmuted_at', 0), d.get('recursive_at', 0))
            date_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d') if ts else ''
            label = _status_labels.get(d.get('status', ''), d.get('status', ''))
            dir_parts.append(f"- `{d['project_dir']}`  [{label}]  {date_str}")
        elements.append({
            "tag": "column_set",
            "background_style": "grey",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "8px",
                "elements": [{"tag": "markdown", "content": '\n'.join(dir_parts)}]
            }]
        })
    if sessions:
        session_parts = [f"**已静音的会话 ({len(sessions)})**", ""]
        # 按目录分组，最近静音的目录排前面
        session_groups: Dict[str, List[dict]] = {}  # project_dir → [session, ...]
        for s in sessions:
            session_groups.setdefault(s.get('project_dir', ''), []).append(s)
        sorted_dirs = sorted(session_groups.keys(),
                             key=lambda d: max(x.get('muted_at', 0) for x in session_groups[d]),
                             reverse=True)
        for d in sorted_dirs:
            dir_label = d if d else '(未关联目录)'
            session_parts.append(f"\U0001F4C1 **{dir_label}**")
            for s in session_groups[d]:
                muted_date = datetime.fromtimestamp(s.get('muted_at', 0)).strftime('%Y-%m-%d')
                session_parts.append(f"- `{s['session_id']}`  {muted_date}")
            session_parts.append("---")
        elements.append({
            "tag": "column_set",
            "background_style": "grey",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "8px",
                "elements": [{"tag": "markdown", "content": '\n'.join(session_parts)}]
            }]
        })

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "静音和加白规则"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": elements
        }
    }

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.error("[feishu] Failed to send mute list card: FeishuAPIService not available")
        return

    card_json = json.dumps(card, ensure_ascii=False)
    success = False
    if reply_to:
        success, _ = service.reply_card(card_json, reply_to)
    else:
        success, _ = service.send_card(card_json, receive_id=chat_id, receive_id_type='chat_id')

    if not success:
        logger.error("[feishu] Failed to send mute list card, fallback to text")
        _send_notice_message(chat_id, "静音列表卡片发送失败，请使用 `/unmute` 命令操作", reply_to)
