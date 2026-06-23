"""
Feishu Message - 消息发送与通知

提供飞书消息发送、通知、帮助卡片、用户状态卡片等功能：
- _send_session_result_notification: 会话结果通知
- _send_error_notification: 错误通知
- _send_text_message: 文本消息发送
- _send_notice_message: 通知消息
- _add_typing_reaction: typing 表情
- _send_help_card: 帮助卡片
- _build_creating_session_card: 创建中卡片
- _build_user_status_card: 用户状态卡片
- _send_users_status_card: 发送用户状态卡片
"""

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from .utils import (
    _sanitize_user_content, _truncate_path,
    _set_last_message_id_to_callback,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 消息发送
# =============================================================================

def _send_text_message(service, chat_id: str, text: str, reply_to: Optional[str] = None,
                       reply_in_thread: bool = False) -> Tuple[bool, str]:
    """发送文本消息

    Args:
        service: FeishuAPIService 实例
        chat_id: 群聊 ID
        text: 消息内容
        reply_to: 要回复的消息 ID（可选），设置后使用回复 API
        reply_in_thread: 是否收进话题详情

    Returns:
        (success, message_id): 成功时返回 (True, message_id)，失败时返回 (False, '')
    """
    try:
        if reply_to:
            # 使用回复消息 API
            success, message_id = service.reply_text(text, reply_to, reply_in_thread)
        else:
            # 使用发送新消息 API
            success, message_id = service.send_text(text, receive_id=chat_id, receive_id_type='chat_id')

        if success:
            logger.info(f"[feishu] Sent notification to {chat_id}: {_sanitize_user_content(text)}, reply_to={reply_to if reply_to else ''}")
            return True, message_id
        else:
            logger.error(f"[feishu] Failed to send notification: {message_id}")
            return False, ''
    except Exception as e:
        logger.error(f"[feishu] Error sending notification: {e}")
        return False, ''


def _send_notice_message(chat_id: str, text: str, reply_to: Optional[str] = None):
    """发送通知消息（后台线程调用）

    Args:
        chat_id: 群聊 ID
        text: 消息内容
        reply_to: 要回复的消息 ID（可选）
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if service and service.enabled:
        _send_text_message(service, chat_id, text, reply_to=reply_to)


# =============================================================================
# 通知
# =============================================================================

def _send_session_result_notification(chat_id: str, response: dict, project_dir: str,
                                      is_new: bool = False, command: str = '',
                                      agent_type: str = '',
                                      reply_to: Optional[str] = None,
                                      reply_in_thread: bool = False,
                                      binding: Optional[Dict[str, Any]] = None,
                                      add_typing: bool = True):
    """根据会话结果发送飞书通知

    Args:
        chat_id: 群聊 ID
        response: Callback 返回的结果
        project_dir: 项目目录
        is_new: 是否为新建会话（True: 新建会话，False: 继续会话）
        command: 使用的命令（可选）
        agent_type: agent 类型（可选，用于动态显示 agent 名称）
        reply_to: 要回复的消息 ID（可选，用于链式回复）
        reply_in_thread: 是否收进话题详情
        binding: 绑定信息字典（包含 callback_url、auth_token、_owner_id，用于跨网络调用）
        add_typing: 是否给通知消息添加 Typing 表情（后续消息不在当前聊天时应为 False）
    """
    from services.feishu_api import FeishuAPIService
    from stores.message_session_store import MessageSessionStore
    from agents import get_agent_adapter

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.error("[feishu] FeishuAPIService not enabled, skipping notification")
        return

    agent_display = get_agent_adapter(agent_type or None).display_name
    status = response.get('status', '')
    error = response.get('error', '')
    session_id = response.get('session_id', '')

    success = False
    sent_message_id = ''

    if status == 'processing':
        # processing 通知策略：
        #
        # | 场景               | 行为                                      | sent_message_id           |
        # |-------------------|-------------------------------------------|---------------------------|
        # | 新建会话           | 发送文本消息(会话信息)，并给该消息加 Typing 表情 | 新发送的通知消息 ID         |
        # | 继续会话           | 给用户消息添加 Typing 表情(轻量，避免刷屏)      | reply_to(用户发送的消息 ID) |
        # | 继续会话(表情失败时) | 回退发送 ⏳ 文本消息                         | 新发送的通知消息 ID          |
        if is_new:
            # 新建会话 - 发送文本消息，展示会话信息
            message = f"🆕 {agent_display} 会话已创建\n📁 项目: {_truncate_path(project_dir)}"
            if command:
                message += f"\n🔧 命令: `{command}`"
            if session_id:
                message += f"\n🔑 Session: `{session_id}`"
            success, sent_message_id = _send_text_message(service, chat_id, message, reply_to=reply_to,
                                                          reply_in_thread=reply_in_thread)
            # 给发出的通知消息添加 Typing 表情，表示正在处理中
            if success and sent_message_id and add_typing:
                service.add_reaction(sent_message_id, 'Typing')
        else:
            # 继续会话 - 用表情回应代替文本通知，更轻量避免刷屏
            # 继续会话始终在同一聊天中，不存在消息跨聊天的问题，无需 add_typing 控制
            if reply_to:
                reaction_ok, _ = service.add_reaction(reply_to, 'Typing')
                if reaction_ok:
                    logger.info(f"[feishu] Added 'Typing' reaction to message {reply_to}")
                    # 将用户发送的消息作为 last_message_id，维护链式回复
                    sent_message_id = reply_to
                    success = True
                else:
                    logger.warning("[feishu] Failed to add reaction, fallback to text notification")

            # 表情回应失败或无 reply_to 时，回退为文本消息
            if not success:
                message = f"⏳ {agent_display} 正在处理您的问题，请稍候..."
                success, sent_message_id = _send_text_message(service, chat_id, message, reply_to=reply_to,
                                                              reply_in_thread=reply_in_thread)

    elif status == 'completed':
        if response.get('notification_handled'):
            # 通知已由 callback 侧的 on_complete 回调处理（如 /compact），网关无需再发
            return
        # 快速完成
        output = response.get('output', '')
        message = f"✅ {agent_display} 已完成: {_sanitize_user_content(output, 50)}" if output else f"✅ {agent_display} 已完成"
        success, sent_message_id = _send_text_message(service, chat_id, message, reply_to=reply_to,
                                                      reply_in_thread=reply_in_thread)

    elif error:
        # 执行失败
        error_prefix = "新建会话失败" if is_new else f"{agent_display} 执行失败"
        _send_error_notification(chat_id, f"{error_prefix}: {error}", reply_to=reply_to,
                                 session_id=session_id, project_dir=project_dir,
                                 reply_in_thread=reply_in_thread)
        return
    else:
        logger.warning(f"[feishu] Unknown response status: {status}")
        _send_error_notification(chat_id, f"未知的响应状态: {status}", reply_to=reply_to,
                                 session_id=session_id, project_dir=project_dir,
                                 reply_in_thread=reply_in_thread)
        return

    # 发送成功后统一保存消息映射和同步 last_message_id
    # add_typing=False 时说明后续消息在新群聊，不应将当前聊天的消息设为 last_message_id
    if success and sent_message_id and session_id and project_dir:
        msg_store = MessageSessionStore.get_instance()
        if msg_store:
            msg_store.save(sent_message_id, session_id, project_dir)
            logger.info(f"[feishu] Saved notification mapping: {sent_message_id} -> {session_id}")

        if add_typing and binding and binding.get('callback_url') and binding.get('auth_token'):
            _set_last_message_id_to_callback(binding, session_id, sent_message_id)


def _send_error_notification(chat_id: str, error_msg: str, reply_to: Optional[str] = None,
                             session_id: str = '', project_dir: str = '',
                             reply_in_thread: bool = False):
    """发送错误通知到飞书

    注意：错误通知仅保存到 MessageSessionStore（支持用户回复继续会话），
    不同步 last_message_id 到 Callback 后端。这是符合预期的行为——
    错误通知不应成为链式回复的锚点，后续正常通知应继续回复到上一条正常消息。

    Args:
        chat_id: 群聊 ID
        error_msg: 错误消息
        reply_to: 要回复的消息 ID（可选）
        session_id: 会话 ID（可选，用于保存消息映射）
        project_dir: 项目目录（可选，用于保存消息映射）
        reply_in_thread: 是否收进话题详情
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if service and service.enabled:
        success, sent_message_id = _send_text_message(service, chat_id, f"⚠️ {error_msg}", reply_to=reply_to,
                                                      reply_in_thread=reply_in_thread)

        # 保存错误通知消息到 MessageSessionStore
        if success and sent_message_id and session_id and project_dir:
            from stores.message_session_store import MessageSessionStore
            msg_store = MessageSessionStore.get_instance()
            if msg_store:
                msg_store.save(sent_message_id, session_id, project_dir)
                logger.info(f"[feishu] Saved error notification mapping: {sent_message_id} -> {session_id}")


def _add_typing_reaction(card_message_id: str):
    """后台任务：给卡片消息添加 Typing 表情，表示 Agent 正在处理

    Args:
        card_message_id: 卡片消息 ID
    """
    if not card_message_id:
        return
    from services.feishu_api import FeishuAPIService
    service = FeishuAPIService.get_instance()
    if service and service.enabled:
        service.add_reaction(card_message_id, 'Typing')


# =============================================================================
# 帮助卡片
# =============================================================================

def _send_help_card(binding: Optional[Dict[str, Any]], chat_id: str,
                    reply_to: str, commands_dict: Dict[str, Any],
                    slash_help_dict: Dict[str, Any], hint: str = '') -> None:
    """构建并发送帮助指令卡片（后台线程调用）

    Args:
        commands_dict: 网关内置命令字典（格式同 _COMMANDS）。必填，由调用方传入
            （因 _COMMANDS 定义在 __init__.py，此处传参避免循环 import）。
        slash_help_dict: Agent 斜杠命令帮助字典（格式同 _slash_commands_as_help_dict() 返回值）。必填。
        hint: 卡片顶部提示文案（如"未知指令"或"请通过指令使用"等），无提示时省略。
    """
    from services.feishu_api import FeishuAPIService
    from config import FEISHU_OWNER_ID as gateway_owner_id

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.error("[feishu] Failed to send help card: FeishuAPIService not available")
        return

    owner_id = binding.get('_owner_id', '') if binding else ''
    is_admin = owner_id and owner_id == gateway_owner_id

    elements = []

    # 顶部提示文案
    if hint:
        elements.append({
            "tag": "markdown",
            "content": hint
        })
        elements.append({"tag": "hr"})

    # 构建指令帮助：每个命令一个 column_set（命令名 | examples 合并为 markdown）
    def _build_cmd_rows(cmd_dict, admin_filter):
        """生成 column_set 行列表，每个命令一个 column_set"""
        rows = []
        for cmd, (_, admin_only, brief, examples) in cmd_dict.items():
            if admin_only != admin_filter:
                continue
            # 每条 example 作为独立 markdown 元素，飞书自动加间距
            example_elements = []
            for example, desc in examples:
                example_elements.append({"tag": "markdown", "content": f"`{example}`  {desc}"})
            rows.append({
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 2,
                        "vertical_align": "top",
                        "elements": [{"tag": "markdown", "content": f"**`/{cmd}`**\n{brief}"}]
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 5,
                        "vertical_align": "top",
                        "elements": example_elements
                    }
                ]
            })
            rows.append({"tag": "hr"})
        # 去掉最后一个 hr
        if rows and rows[-1].get('tag') == 'hr':
            rows.pop()
        return rows

    def _wrap_section(title, rows):
        """将标题和行列表包裹在灰色背景 column_set 中"""
        return {
            "tag": "column_set",
            "background_style": "grey",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "8px",
                "elements": [{"tag": "markdown", "content": title}] + rows
            }]
        }

    # 使用传入的参数，如果为 None 则使用空字典
    cmds = commands_dict or {}
    slash_help = slash_help_dict or {}

    # 普通指令
    normal_rows = _build_cmd_rows(cmds, False)
    if normal_rows:
        elements.append(_wrap_section("**支持的指令**", normal_rows))

    # Agent 斜杠指令（从各 adapter 的 get_slash_commands() 收集）
    slash_cmd_rows = _build_cmd_rows(slash_help, False)
    if slash_cmd_rows:
        elements.append(_wrap_section("**Agent 指令**（转发给 Agent 执行）", slash_cmd_rows))

    # 管理员指令
    admin_rows = _build_cmd_rows(cmds, True)
    if is_admin and admin_rows:
        elements.append(_wrap_section("**管理员指令**", admin_rows))

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "指令帮助"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": elements
        }
    }

    card_json = json.dumps(card, ensure_ascii=False)
    success = False
    if reply_to:
        success, _ = service.reply_card(card_json, reply_to)
    else:
        success, _ = service.send_card(card_json, receive_id=chat_id, receive_id_type='chat_id')

    if not success:
        logger.error("[feishu] Failed to send help card, fallback to text")
        _send_notice_message(chat_id, "帮助卡片发送失败，请使用 `/help` 或发送未知指令重试", reply_to)


# =============================================================================
# 状态卡片
# =============================================================================

def _build_creating_session_card(selected_dir: str, prompt: str, command: str = '',
                                 agent_type: str = '') -> dict:
    """构建"正在创建会话"状态卡片

    Args:
        selected_dir: 选择的工作目录
        prompt: 用户输入的提示词
        command: 使用的命令（可选）
        agent_type: agent 类型（如 'claude', 'codex'），用于显示名称

    Returns:
        卡片字典（包含 type 和 data）
    """
    from agents import get_agent_adapter
    agent_display = get_agent_adapter(agent_type or None).display_name

    elements = [
        {
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': f'请稍候，正在启动 {agent_display} 会话...'
            }
        },
        {
            'tag': 'hr'
        },
        {
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': f'📁 工作目录：{selected_dir}'
            }
        }
    ]

    if command:
        elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': f'🔧 命令：{command}'
            }
        })

    elements.append({
        'tag': 'div',
        'text': {
            'tag': 'plain_text',
            'content': f'💬 提示词：{prompt}'
        }
    })

    return {
        'type': 'raw',
        'data': {
            'schema': '2.0',
            'config': {'wide_screen_mode': True},
            'header': {
                'title': {'tag': 'plain_text', 'content': f'⏳ 正在创建 {agent_display} 会话'},
                'template': 'blue'
            },
            'body': {
                'direction': 'vertical',
                'elements': elements
            }
        }
    }


def _build_user_status_card(bindings: dict, ws_status: dict, admin_id: str) -> dict:
    """构建用户状态卡片

    Args:
        bindings: 所有绑定信息
        ws_status: WebSocket 连接状态
        admin_id: 管理员 owner_id

    Returns:
        飞书卡片 JSON 结构
    """
    elements = []

    # 在线用户（已认证连接）
    online_ids = set(ws_status.get('authenticated_owner_ids', []))
    if online_ids:
        content_lines = []
        for oid in sorted(online_ids):
            at_tag = f'<at id="{oid}"></at>'
            marker = " (你)" if oid == admin_id else ""
            content_lines.append(f"• {at_tag}{marker}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**🟢 在线**\n" + "\n".join(content_lines)}
        })

    # 等待授权（pending 连接）
    pending = ws_status.get('pending', [])
    if pending:
        content_lines = []
        for p in pending:
            oid = p.get('owner_id', '')
            at_tag = f'<at id="{oid}"></at>'
            ip = p.get('client_ip', '-')
            wait_sec = p.get('waiting_seconds', 0)
            content_lines.append(f"• {at_tag} - {ip} (等待 {wait_sec}s)")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**🟡 等待授权**\n" + "\n".join(content_lines)}
        })

    # 离线用户（已注册但未在线）
    all_registered = set(bindings.keys())
    offline = all_registered - online_ids
    if offline:
        content_lines = []
        for oid in sorted(offline):
            info = bindings.get(oid, {})
            ts = info.get('updated_at', 0)
            if ts:
                now = int(time.time())
                diff = now - ts
                if diff < 60:
                    time_str = f" ({diff}s 前)"
                elif diff < 3600:
                    time_str = f" ({diff // 60} 分钟前)"
                elif diff < 86400:
                    time_str = f" ({diff // 3600} 小时前)"
                else:
                    time_str = f" ({diff // 86400} 天前)"
            else:
                time_str = ""
            at_tag = f'<at id="{oid}"></at>'
            content_lines.append(f"• {at_tag}{time_str}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**⚫ 离线**\n" + "\n".join(content_lines)}
        })

    # 统计信息
    total_registered = len(bindings)
    total_online = len(online_ids)
    total_pending = len(pending)

    elements.append({
        "tag": "hr"
    })
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"总计: 已注册 {total_registered} 人 | 在线 {total_online} 人 | 等待授权 {total_pending} 人"}
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📊 已注册用户和在线状态"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": elements
        }
    }


def _send_users_status_card(chat_id: str, card: dict, reply_to: str):
    """发送用户状态卡片（后台线程调用）

    Args:
        chat_id: 群聊 ID
        card: 卡片 JSON 结构
        reply_to: 要回复的消息 ID
    """
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.error("[feishu] Feishu API service not available, cannot send user status card")
        return

    card_json = json.dumps(card, ensure_ascii=False)
    if reply_to:
        success, _ = service.reply_card(card_json, reply_to)
    else:
        success, _ = service.send_card(card_json, receive_id=chat_id, receive_id_type='chat_id')

    if not success:
        logger.error("[feishu] Failed to send user status card, fallback to text")
        _send_notice_message(chat_id, "用户状态卡片发送失败，请稍后重试", reply_to)
