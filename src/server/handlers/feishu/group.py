"""
群聊管理与网关侧接口

包含：
    - handle_card_action_register: 注册卡片回调处理
    - create_group_chat_and_record: 创建群聊并记录
    - handle_create_group: HTTP 创建群聊端点
    - batch_dissolve_groups: 批量解散群聊
    - find_idle_group_chats: 筛选空闲群聊（供解散流程复用）
    - handle_send_message: HTTP 发送消息端点
    - handle_remove_reaction: HTTP 移除表情端点
    - _send_groups_card: 群聊列表卡片
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Tuple

from .utils import (
    TOAST_ERROR,
    _should_reply_in_thread,
    _set_last_message_id_to_callback,
)
from .message import (
    _send_notice_message,
)
from .card_action import (
    _extract_request_id_from_card,
)

logger = logging.getLogger(__name__)


def handle_card_action_register(value: dict) -> Tuple[bool, dict]:
    """处理注册授权卡片的按钮回调

    Args:
        value: 按钮的 value 数据
            - action: approve_register/deny_register/unbind_register
            - mode: "ws" 表示 WebSocket 模式，否则为 HTTP 模式
            - callback_url: Callback 后端 URL（HTTP 模式）
            - owner_id: 飞书用户 ID
            - request_ip: 注册来源 IP（仅 approve_register 需要）
            - request_id: 注册请求 ID（WS 模式需要）
            - session_mode: 会话模式 message/thread/group（仅 approve_register 需要）
            - default_agent: 默认 agent 类型（仅 approve_register 需要）
            - claude_commands: 可用的 Claude 命令列表（仅 approve_register 需要）
            - codex_commands: 可用的 Codex 命令列表（仅 approve_register 需要）
            - default_chat_dir: 默认聊天目录（仅 approve_register 需要）
            - default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式（仅 approve_register 需要）
            - group_name_prefix: 群聊名称前缀（仅 approve_register 需要）
            - group_dissolve_days: 群聊自动解散天数（仅 approve_register 需要）

    Returns:
        (handled, response) - response 包含 toast 和可选的 card 更新
    """
    from handlers.register import handle_authorization_decision, handle_register_unbind
    from handlers.register import handle_ws_authorization_approved, handle_ws_authorization_denied, handle_ws_register_unbind
    from handlers.register import extract_binding_params

    action = value.get('action', '')
    mode = value.get('mode', 'http')  # 默认 HTTP 模式
    callback_url = value.get('callback_url', '')
    owner_id = value.get('owner_id', '')
    request_ip = value.get('request_ip', '')
    request_id = value.get('request_id', '')
    binding_params = extract_binding_params(value)

    # WebSocket 模式
    if mode == 'ws':
        if action == 'approve_register':
            logger.info("[feishu] WS registration approved: owner_id=%s", owner_id)
            return True, handle_ws_authorization_approved(
                owner_id, request_id, request_ip,
                binding_params=binding_params
            )
        elif action == 'deny_register':
            logger.info("[feishu] WS registration denied: owner_id=%s, request_id=%s", owner_id, request_id)
            return True, handle_ws_authorization_denied(owner_id, request_id)
        elif action == 'unbind_register':
            logger.info("[feishu] WS registration unbound: owner_id=%s", owner_id)
            return True, handle_ws_register_unbind(owner_id)
        else:
            logger.warning("[feishu] Unknown WS register action: %s", action)
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '未知的操作'
                }
            }

    # HTTP 模式
    if action == 'approve_register':
        logger.info("[feishu] Registration approved: owner_id=%s, callback_url=%s, session_mode=%s", owner_id, callback_url, binding_params.get('session_mode'))
        return True, handle_authorization_decision(
            callback_url, owner_id, request_ip, approved=True,
            binding_params=binding_params
        )
    elif action == 'deny_register':
        logger.info("[feishu] Registration denied: owner_id=%s", owner_id)
        return True, handle_authorization_decision(
            callback_url, owner_id, request_ip, approved=False
        )
    elif action == 'unbind_register':
        logger.info("[feishu] Registration unbound: owner_id=%s, callback_url=%s", owner_id, callback_url)
        return True, handle_register_unbind(callback_url, owner_id)
    else:
        logger.warning("[feishu] Unknown register action: %s", action)
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '未知的操作'
            }
        }


def create_group_chat_and_record(owner_id: str, session_id: str, project_dir: str,
                                 group_name_prefix: str) -> Tuple[bool, str]:
    """创建飞书群聊并完成网关侧所有数据登记（群聊创建唯一入口）

    所有创建路径都应走此函数，原子完成：
        - 幂等检查：GroupSessionStore 已有该 session_id 的群则直接返回
        - GroupChatStore.allocate 分配 seq（用于构造群名）
        - 调飞书 API 建群（name = prefix - #seq - dir_name - YYYYMMDD）
        - GroupChatStore.bind 将 chat_id 绑定到 seq
        - 若提供 session_id/project_dir，同步写 GroupSessionStore（chat → session 路由）

    调用方:
    - handle_create_group() (HTTP): 分离部署 callback 经 HTTP 反向转发
    - handlers.outbound.create_feishu_group(): 单机模式 callback 直接调用

    Args:
        owner_id: 归属 owner（飞书用户 ID），用于后续解散权限校验
        session_id: 关联的 session ID（可选）；提供则写 GroupSessionStore
        project_dir: session 工作目录（写 GroupSessionStore 时一并存）
        group_name_prefix: 群名前缀；空串视为无前缀（不再兜底默认值，由调用方决定）

    Returns:
        (success, chat_id_or_error)
    """
    from services.feishu_api import FeishuAPIService
    from stores.group_chat_store import GroupChatStore
    from stores.group_session_store import GroupSessionStore

    # owner_id 是后续 allocate seq、写 GroupSessionStore、解散归属校验的必需上下文，
    # 缺失则群只能在飞书端裸存、gateway 完全无记录，必须显式拒绝
    if not owner_id:
        return False, 'Missing owner_id'

    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()
    if not group_store or not gs_store:
        return False, 'Store not initialized'

    # 幂等：如果当前 owner 下 GroupSessionStore 已有该 session_id 的群，直接返回
    if session_id:
        existing_chat_id = gs_store.find_by_session(owner_id, session_id)
        if existing_chat_id:
            logger.info("[create-group] Idempotent return: owner=%s session=%s -> chat=%s",
                        owner_id, session_id, existing_chat_id)
            return True, existing_chat_id

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        return False, 'Feishu API service not available'

    # 先分配 seq，再构造群名（建群失败则 seq 跳号，可接受）
    seq = group_store.allocate(owner_id)
    if not seq:
        return False, 'Failed to allocate seq for group chat'

    dir_name = os.path.basename(project_dir) if project_dir else ''
    if len(dir_name) > 30:
        dir_name = dir_name[:29] + '\u2026'
    date_str = time.strftime('%Y%m%d')
    parts = []
    if group_name_prefix:
        parts.append(group_name_prefix)
    parts.append('#%d' % seq)
    if dir_name:
        parts.append(dir_name)
    parts.append(date_str)
    name = ' - '.join(parts)

    ok, result = service.create_group_chat(name, owner_id)
    if not ok:
        return False, result

    chat_id = result
    if not group_store.bind(owner_id, seq, chat_id):
        logger.error("[create-group] bind failed: owner=%s seq=%d chat=%s, "
                     "group created on Feishu but not tracked locally",
                     owner_id, seq, chat_id)

    # 同步写 chat → session 路由（如果调用方提供了 session_id）
    if session_id:
        gs_store.save(owner_id, chat_id, session_id, project_dir=project_dir)

    return True, chat_id


def handle_create_group(binding: Dict[str, Any], data: dict) -> Tuple[bool, dict]:
    """处理 /gw/feishu/create-group 请求，创建飞书群聊

    群名由 gateway 根据 binding 中的 group_name_prefix 与请求中的 project_dir
    统一构造，请求方无需也无法自定义。

    Args:
        binding: 绑定信息（由调用方鉴权后传入，包含 owner_id）
        data: 请求 JSON 数据（session_id、project_dir）

    Returns:
        (handled, response)
    """
    owner_id = binding.get('_owner_id', '')
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    group_name_prefix = binding.get('group_name_prefix', '')

    ok, result = create_group_chat_and_record(
        owner_id, session_id, project_dir, group_name_prefix)
    if ok:
        return True, {'success': True, 'chat_id': result}
    else:
        return True, {'success': False, 'error': result}


def batch_dissolve_groups(binding: Dict[str, Any],
                          chat_ids: List[str]) -> Dict[str, Any]:
    """批量解散群聊并清理归属记录（网关侧核心函数）

    只解散 GroupChatStore 中归属于 binding owner 的群聊。
    非服务创建的群聊（不在 store 中或归属其他 owner）直接跳过，不视为失败。

    执行顺序：先通知 callback 标记 dissolved，再调飞书 API 解散。
    这样即使飞书 API 失败，session 被标记 dissolved 但群仍存活——
    用户下次在群内交互时 continue/ensure-chat 会自动复活 session（自愈）。
    反之若先解散再通知，通知失败会导致 session 指向已解散群且无法自愈。

    调用方:
    - _dissolve_groups(): /groups dissolve 命令
    - main.py _cleanup_group_chats(): 定时清理空闲群聊

    Args:
        binding: 绑定信息（包含 _owner_id，用于归属校验和 callback 通知）
        chat_ids: 待解散的群聊 ID 列表

    Returns:
        {
            'dissolved_items': List[str],          # 实际解散的 chat_id
            'skipped_items': List[str],            # 非服务创建或不属于该 owner 的 chat_id
            'failed': List[{'chat_id', 'error'}],  # 真正的 API 错误
        }
    """
    owner_id = binding.get('_owner_id', '')
    if not owner_id:
        logger.warning("[batch-dissolve] owner_id is empty, refusing %d chat(s)", len(chat_ids))
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'owner_id not configured'} for cid in chat_ids],
        }

    from services.feishu_api import FeishuAPIService
    from stores.group_chat_store import GroupChatStore

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'Feishu API service not available'} for cid in chat_ids],
        }

    group_store = GroupChatStore.get_instance()
    if not group_store:
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'GroupChatStore not initialized'} for cid in chat_ids],
        }
    my_chats = {item['chat_id'] for item in group_store.get_chats_by_owner(owner_id)}

    # 1) 按归属过滤
    targets = []
    skipped_items = []
    for cid in chat_ids:
        if cid not in my_chats:
            skipped_items.append(cid)
        else:
            targets.append(cid)

    if not targets:
        return {'dissolved_items': [], 'skipped_items': skipped_items, 'failed': []}

    # 2) 先通知 callback 标记 dissolved；失败则中止，等下次清理重试
    from services.session_facade import SessionFacade
    resp = SessionFacade.invalidate_chats(binding, targets)
    if resp is None:
        return {
            'dissolved_items': [],
            'skipped_items': skipped_items,
            'failed': [{'chat_id': cid, 'error': 'pre-notify failed'} for cid in targets],
        }

    # 3) 再调飞书 API 解散 + 清 GroupChatStore
    dissolved_items = []
    failed = []
    for cid in targets:
        ok, err = service.dissolve_group_chat(cid)
        if ok:
            group_store.remove(cid)
            dissolved_items.append(cid)
        else:
            failed.append({'chat_id': cid, 'error': err})

    return {'dissolved_items': dissolved_items, 'skipped_items': skipped_items, 'failed': failed}


def find_idle_group_chats(owner_id: str, owner_chats: List[Dict[str, Any]],
                          now: int, idle_days: int) -> List[str]:
    """返回该 owner 下空闲（超过 idle_days 天未活跃）的群聊 chat_id 列表。

    空闲判定：now - last_active_at >= idle_days * 86400，无 last_active_at 时
    回退 created_at。自动解散（main.py）与 /groups dissolve idle 共用此函数。

    纯过滤器：owner_chats（群列表）与 now（判定时刻）均由调用方传入，函数不自取，
    避免内部重复读 group_chat 文件、并让批量调用共用同一时刻基准。仅 gs_data
    （session 活跃信息）按 owner 自取。
    """
    from stores.group_session_store import GroupSessionStore

    if idle_days <= 0 or not owner_id:
        return []
    gs_store = GroupSessionStore.get_instance()
    if not gs_store:
        return []

    gs_data = gs_store.get_by_owner(owner_id)
    threshold = idle_days * 86400

    idle_chat_ids: List[str] = []
    for item in owner_chats:
        cid = item.get('chat_id', '')
        if not cid:
            continue
        last_active = (gs_data.get(cid) or {}).get('last_active_at', item.get('created_at', 0))
        if now - last_active >= threshold:
            idle_chat_ids.append(cid)
    return idle_chat_ids


def handle_send_message(binding: Dict[str, Any], data: dict) -> Tuple[bool, dict]:
    """处理 /gw/feishu/send 请求，通过 OpenAPI 发送消息

    Args:
        binding: 绑定信息（由调用方鉴权后传入）
        data: 请求 JSON 数据
            - owner_id: 飞书用户 ID（必需，作为接收者或备用）
            - msg_type: 消息类型 interactive/text/image（必需，暂仅支持 interactive）
            - content: 消息内容（必需）
                - card: 卡片 JSON 对象
                - text: 文本内容
                - image_key: 图片的 key
            - chat_id: 群聊 ID（可选，优先使用）
            - receive_id_type: 接收者类型（可选，默认自动检测）
            - session_id: 会话 ID（可选，用于继续会话）
            - project_dir: 项目工作目录（可选，用于继续会话）
            - reply_to_message_id: 要回复的消息 ID（可选，使用 reply API）
            - add_typing: 发送成功后是否添加 Typing 表情（可选，默认 false）

    Returns:
        (handled, response): handled 始终为 True，response 包含结果

    Note:
        receive_id 优先级：chat_id 参数 > owner_id
        当提供 reply_to_message_id 时，使用 reply API 发送消息到话题流
    """
    from services.feishu_api import FeishuAPIService, detect_receive_id_type

    msg_type = data.get('msg_type')
    content = data.get('content')
    owner_id = data.get('owner_id', '')
    chat_id = data.get('chat_id', '')

    # 提取 session 相关参数
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    reply_to_message_id = data.get('reply_to_message_id', '') or ''
    add_typing = data.get('add_typing', False)

    if not msg_type:
        logger.warning("[feishu] /gw/feishu/send: missing msg_type")
        return True, {'success': False, 'error': 'Missing msg_type'}

    if not owner_id:
        logger.warning("[feishu] /gw/feishu/send: missing owner_id")
        return True, {'success': False, 'error': 'Missing owner_id'}

    # 确定 receive_id 和 receive_id_type
    # 优先级：传入的 chat_id > owner_id
    if chat_id:
        receive_id = chat_id
        receive_id_type = 'chat_id'
    else:
        receive_id = owner_id
        receive_id_type = data.get('receive_id_type', '') or detect_receive_id_type(owner_id)

    service = FeishuAPIService.get_instance()
    if service is None or not service.enabled:
        logger.warning("[feishu] /gw/feishu/send: service not enabled")
        return True, {'success': False, 'error': 'Feishu API service not enabled'}

    reply_in_thread = _should_reply_in_thread(binding, project_dir)

    # 尝试清除 reply_to 消息上的 Typing 表情（新建/继续会话的 processing 阶段可能添加了该表情）
    # 多数场景下消息上并无此表情，remove_reaction 查询到空列表后会直接返回，无副作用
    if reply_to_message_id:
        service.remove_reaction(reply_to_message_id, 'Typing')

    success = False
    sent_message_id = ''

    if msg_type == 'interactive':
        # content 直接是 card 对象
        if not content:
            logger.warning("[feishu] /gw/feishu/send: missing card content")
            return True, {'success': False, 'error': 'Missing card content'}

        if isinstance(content, dict):
            card_json = json.dumps(content, ensure_ascii=False)
        else:  # content 是 str（当前调用方不会传入，防御性逻辑；若传入则不缓存避免 parse + dump）
            card_json = content

        if reply_to_message_id:
            success, sent_message_id = service.reply_card(card_json, reply_to_message_id, reply_in_thread)
        else:
            success, sent_message_id = service.send_card(card_json, receive_id, receive_id_type)

        # 仅在卡片实际发送成功后缓存，避免降级为文本消息时误缓存卡片
        # Best-effort 预筛选：通过字符串匹配快速跳过不含回调按钮的通知类卡片
        # 可能误匹配文本中恰好包含 "request_id" 的卡片，但只会多缓存，不影响正确性
        if success and isinstance(content, dict) and '"request_id"' in card_json:
            cached_request_id = _extract_request_id_from_card(content)
            if cached_request_id:
                from services.card_cache import CardCache
                cache = CardCache.get_instance()
                if cache:
                    cache.set(cached_request_id, card_json)
                    logger.debug("[feishu] Cached card for request_id=%s after send", cached_request_id)
        elif not success:
            # 卡片发送失败，降级发送文本错误提示
            error_msg = sent_message_id
            logger.warning(f"[feishu] /gw/feishu/send: send_card failed: {error_msg}, fallback to text")
            fallback_text = f"\u26a0\ufe0f 卡片消息发送失败: {error_msg}"
            if reply_to_message_id:
                success, sent_message_id = service.reply_text(fallback_text, reply_to_message_id, reply_in_thread)
            else:
                success, sent_message_id = service.send_text(fallback_text, receive_id, receive_id_type)

    elif msg_type == 'text':
        text = content if isinstance(content, str) else content.get('text', '')
        if not text:
            logger.warning("[feishu] /gw/feishu/send: missing text content")
            return True, {'success': False, 'error': 'Missing text content'}

        if reply_to_message_id:
            success, sent_message_id = service.reply_text(text, reply_to_message_id, reply_in_thread)
        else:
            success, sent_message_id = service.send_text(text, receive_id, receive_id_type)

    elif msg_type == 'post':
        # 富文本消息：content 应为 {"zh_cn": {"title": "...", "content": [[...]]}}
        if not content or not isinstance(content, dict):
            logger.warning("[feishu] /gw/feishu/send: missing post content")
            return True, {'success': False, 'error': 'Missing post content'}

        if reply_to_message_id:
            success, sent_message_id = service.reply_post(content, reply_to_message_id, reply_in_thread)
        else:
            success, sent_message_id = service.send_post(content, receive_id, receive_id_type)

    else:
        logger.warning(f"[feishu] /gw/feishu/send: unsupported msg_type: {msg_type}")
        return True, {'success': False, 'error': f'Unsupported msg_type: {msg_type}'}

    if not success:
        # sent_message_id 此时实际是错误信息
        logger.error(f"[feishu] /gw/feishu/send: failed, error={sent_message_id}")
        return True, {'success': False, 'error': sent_message_id}

    logger.info(f"[feishu] /gw/feishu/send: message sent to {receive_id} ({receive_id_type}), id={sent_message_id}, reply_to={reply_to_message_id}")

    # 按需添加 Typing 表情（调用方通过 add_typing=true 指定）
    if add_typing and sent_message_id:
        service.add_reaction(sent_message_id, 'Typing')

    # 保存到本地 MessageSessionStore（飞书网关维护）
    if sent_message_id and session_id and project_dir:
        from stores.message_session_store import MessageSessionStore
        msg_store = MessageSessionStore.get_instance()
        if msg_store:
            msg_store.save(sent_message_id, session_id, project_dir)

    # 出站到群聊时刷新活跃时间（用户终端对话也会触发 hook 通知到群）
    if success and receive_id_type == 'chat_id' and receive_id and owner_id:
        from stores.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        if gs_store:
            gs_store.touch(owner_id, receive_id)

    # 通过 Callback 后端设置 last_message_id
    if sent_message_id and session_id and project_dir and binding:
        if binding.get('callback_url') and binding.get('auth_token'):
            _set_last_message_id_to_callback(binding, session_id, sent_message_id)

    return True, {'success': True, 'message_id': sent_message_id}


def handle_remove_reaction(binding: Dict[str, Any], data: dict) -> Tuple[bool, dict]:
    """处理 /gw/feishu/remove-reaction 请求，移除消息上的表情回应

    Args:
        binding: 绑定信息（由调用方鉴权后传入）
        data: 请求 JSON 数据
            - message_id: 消息 ID（必需）
            - emoji_type: 表情类型，如 "Typing"（必需）

    Returns:
        (handled, response): handled 始终为 True，response 包含结果
    """
    from services.feishu_api import FeishuAPIService

    message_id = data.get('message_id', '') or ''
    emoji_type = data.get('emoji_type', '') or ''

    if not message_id:
        return True, {'success': False, 'error': 'Missing message_id'}
    if not emoji_type:
        return True, {'success': False, 'error': 'Missing emoji_type'}

    service = FeishuAPIService.get_instance()
    if service is None or not service.enabled:
        return True, {'success': False, 'error': 'Feishu API service not enabled'}

    success, deleted_count = service.remove_reaction(message_id, emoji_type)
    if success:
        return True, {'success': True, 'deleted_count': deleted_count}
    else:
        return True, {'success': False, 'error': 'remove_reaction failed'}


def _send_groups_card(binding: Dict[str, Any], chat_id: str, message_id: str) -> None:
    """构建并发送群聊列表卡片（数据全来自 gateway 本地 store，零 RPC）"""
    from services.feishu_api import FeishuAPIService
    from stores.group_chat_store import GroupChatStore
    from stores.group_session_store import GroupSessionStore

    owner_id = binding.get('_owner_id', '')
    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()

    if not group_store or not gs_store:
        _send_notice_message(chat_id, "存储服务未就绪，请稍后重试", message_id)
        return

    owner_chats = group_store.get_chats_by_owner(owner_id) if owner_id else []
    if not owner_chats:
        _send_notice_message(chat_id, "当前没有活跃的群聊会话", message_id)
        return

    # 聚合：group_chat_store（seq + chat_id + created_at）+ group_session_store（project_dir + last_active_at）
    now = int(time.time())
    gs_data = gs_store.get_by_owner(owner_id)

    def _format_ago(seconds: int) -> str:
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return "%d 分钟前" % (seconds // 60)
        if seconds < 86400:
            return "%d 小时前" % (seconds // 3600)
        return "%d 天前" % (seconds // 86400)

    entries = []
    for item in owner_chats:
        cid = item['chat_id']
        seq = item['seq']
        created_at = item.get('created_at', 0)
        gs_item = gs_data.get(cid) or {}
        session_id = gs_item.get('session_id', '')
        project_dir = gs_item.get('project_dir', '')
        last_active_at = gs_item.get('last_active_at', created_at)
        entries.append({
            'chat_id': cid,
            'seq': seq,
            'session_id': session_id,
            'project_dir': project_dir,
            'last_active_at': last_active_at,
        })
    # 按目录分组，每组内按 last_active_at 降序
    groups: Dict[str, List[dict]] = {}  # project_dir → [entry, ...]
    for e in entries:
        groups.setdefault(e['project_dir'], []).append(e)
    # 每组取最近活跃时间，组间按此降序
    # 空目录（未关联目录）始终排最后，其余按最近活跃时间降序
    sorted_dirs = sorted(groups.keys(),
                         key=lambda d: (d != '', max(x['last_active_at'] for x in groups[d])),
                         reverse=True)
    for group in groups.values():
        group.sort(key=lambda x: x['last_active_at'], reverse=True)

    # 用 markdown 生成完整列表，避免 column_set 元素过多超限
    md_parts = ["> **解散群聊：**",
                "> `/groups dissolve all`",
                "> `/groups dissolve idle <天数>`",
                "> `/groups dissolve <序号1> <序号2> ...`",
                "> `/groups dissolve /path`  或  `/groups dissolve /path/**`",
                ""]
    for d in sorted_dirs:
        dir_label = d if d else '(未关联目录)'
        md_parts.append("---")
        md_parts.append(f"\U0001F4C1 **{dir_label}**")
        for e in groups[d]:
            sid = e['session_id'] or '-'
            ago = _format_ago(now - e['last_active_at']) if e['last_active_at'] else ''
            link = "https://applink.feishu.cn/client/chat/open?openChatId=%s" % e['chat_id']
            md_parts.append(f"- **{e['seq']}**  `{sid}`  {ago}  [进入群聊]({link})")

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"由服务创建的群聊（共 {len(entries)} 个）"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": [{
                "tag": "markdown",
                "content": '\n'.join(md_parts)
            }]
        }
    }

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.error("[feishu] Failed to send groups list card: FeishuAPIService not available")
        return

    card_json = json.dumps(card, ensure_ascii=False)
    success = False
    if message_id:
        success, _ = service.reply_card(card_json, message_id)
    else:
        success, _ = service.send_card(card_json, receive_id=chat_id, receive_id_type='chat_id')

    if not success:
        logger.warning("[feishu] Failed to send groups list card, fallback to text")
        _send_notice_message(chat_id, '\n'.join(md_parts), message_id)

