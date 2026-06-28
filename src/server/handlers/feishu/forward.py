"""
Feishu Forward - 请求转发与路由

负责将飞书侧的请求通过 WS 隧道或 HTTP 转发到 Callback 后端：
- _forward_via_ws_or_http: WS/HTTP 隧道核心路由
- _forward_agent_request: Agent 请求转发（统一入口）
- _forward_continue_request: 继续会话转发
- _forward_new_request: 新会话转发
- _forward_new_request_for_default_dir: 默认目录新会话转发
- _forward_attach_request: /attach 转发
- _forward_stop_request: /stop 转发
- _forward_permission_request: 权限决策转发
- _fetch_recent_dirs_from_callback: 获取常用目录
- _fetch_browse_dirs_from_callback: 浏览子目录
"""

import logging
import socket
import time
from typing import Any, Dict, Optional, Tuple

from utils.concurrency import run_in_background
from utils.http_client import post_json

from .utils import (
    TOAST_SUCCESS, TOAST_WARNING, TOAST_ERROR,
    _should_reply_in_thread,
    _extract_http_error_detail, _get_binding_from_event,
)
from .message import (
    _send_session_result_notification,
    _send_error_notification,
    _send_notice_message,
    _add_typing_reaction,
)

logger = logging.getLogger(__name__)


# =============================================================================
# WebSocket 隧道路由分发
# =============================================================================

def _forward_via_ws_or_http(binding: Dict[str, Any], endpoint: str, payload: Dict[str, Any],
                            timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """通过 WS 或 HTTP 转发请求到 Callback

    根据 callback_url 协议决定转发方式：
    - ws:// 或 wss:// → 通过 WebSocket 隧道转发
    - http:// 或 https:// → 通过 HTTP 请求转发

    从 binding 字典中提取路由信息（owner_id、callback_url、auth_token）。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        endpoint: API 端点（如 /cb/decision, /cb/agent/new）
        payload: 请求数据
        timeout: 请求超时（秒），默认使用各通道的默认超时

    Returns:
        响应数据，失败返回 None
    """
    from services.ws_registry import WebSocketRegistry

    owner_id = binding.get('_owner_id', '')
    callback_url = binding.get('callback_url', '')
    auth_token = binding.get('auth_token', '')

    # 根据 callback_url 协议决定转发方式
    is_ws_mode = callback_url.startswith(('ws://', 'wss://'))

    if is_ws_mode:
        # 尝试通过 WS 转发
        registry = WebSocketRegistry.get_instance()
        if owner_id and registry and registry.is_authenticated(owner_id):
            # 获取该连接的 auth_token 用于本地 handler 验证
            ws_auth_token = registry.get_auth_token(owner_id)
            headers = {'X-Auth-Token': ws_auth_token} if ws_auth_token else {}
            response = registry.send_request(owner_id, endpoint, payload, headers, timeout=timeout)
            if response is not None:
                # WS 隧道返回格式: {status: HTTP码, body: 业务响应}
                # 提取 body 作为真正的业务响应
                return response.get('body', response)
        logger.warning("[feishu] WS tunnel not available for %s", owner_id)
        return None

    # HTTP 模式（ws:// 或 wss:// 是 WS 隧道地址，不能用于 HTTP 请求）
    if callback_url:
        api_url = f"{callback_url.rstrip('/')}{endpoint}"
        http_timeout = int(timeout) if timeout else 10
        logger.debug("[feishu] Using HTTP for %s: %s", owner_id, api_url)
        try:
            return post_json(api_url, payload, headers={'X-Auth-Token': auth_token}, timeout=http_timeout)
        except Exception as e:
            logger.error("[feishu] HTTP request failed: %s", e)
            return None

    logger.warning("[feishu] No callback_url configured for %s", owner_id)
    return None


# =============================================================================
# Agent 请求转发
# =============================================================================

def _forward_agent_request(binding: Dict[str, Any], endpoint: str,
                            payload: Dict[str, Any], chat_id: str,
                            reply_to: Optional[str] = None,
                            reply_in_thread: bool = False) -> str:
    """转发会话请求到 Callback 后端

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        endpoint: API 端点（如 /cb/agent/new, /cb/agent/continue）
        payload: 请求数据
        chat_id: 群聊 ID（用于错误通知）
        reply_to: 要回复的消息 ID（可选）
        reply_in_thread: 是否收进话题详情

    Returns:
        session_id（新建时从响应获取，继续时从 payload 获取），失败时仍返回 payload 中的 session_id
    """
    import urllib.error

    owner_id = binding.get('_owner_id', '')

    # 从 endpoint 提取 action（如 /cb/agent/new -> new）
    action = endpoint.rstrip('/').split('/')[-1]
    known_actions = ('new', 'continue')
    if action not in known_actions:
        logger.warning("[feishu] Unknown endpoint action: %s, expected one of %s", action, known_actions)
        action = 'continue'  # 默认使用 continue
    is_new = (action == 'new')

    # 预设 session_id：continue 时 payload 中已有，new 时成功后从响应覆盖
    session_id = payload.get('session_id', '')

    logger.info("[feishu] Forwarding %s request, owner_id=%s", action, owner_id)

    try:
        # 使用 WS/HTTP 路由分发（保留原 HTTP 模式的 30s 超时）
        response_data = _forward_via_ws_or_http(binding, endpoint, payload, timeout=30)

        if response_data is None:
            raise urllib.error.URLError("No available route (WS or HTTP)")

        logger.info(f"[feishu] {action.capitalize()} request response: {response_data}")

        session_id = response_data.get('session_id', '') or session_id

        # 先保存用户消息到 MessageSessionStore（不更新 last_message_id）
        if reply_to:
            project_dir = payload.get('project_dir', '')
            if session_id and project_dir:
                from stores.message_session_store import MessageSessionStore
                msg_store = MessageSessionStore.get_instance()
                if msg_store:
                    msg_store.save(reply_to, session_id, project_dir)
                    logger.info(f"[feishu] Saved user message mapping: {reply_to} -> {session_id}")

        # 再发送系统通知（内部会更新 last_message_id）
        # add_typing 复用 skip_user_prompt：skip=False 说明后续消息在新群聊，
        # 会导致当前聊天的 Typing 可能无法被移除，所以不加
        _send_session_result_notification(chat_id, response_data, payload.get('project_dir', ''),
                                          is_new=is_new,
                                          command=payload.get('command', ''),
                                          agent_type=response_data.get('agent_type', '') or payload.get('agent_type', ''),
                                          reply_to=reply_to,
                                          reply_in_thread=reply_in_thread,
                                          binding=binding,
                                          add_typing=payload.get('skip_user_prompt', True))

    except urllib.error.HTTPError as e:
        error_detail = _extract_http_error_detail(e)
        action_text = "新建会话失败" if is_new else "继续会话失败"
        error_msg = f"{action_text}: {error_detail}" if error_detail else f"Callback 服务返回错误: HTTP {e.code}"
        logger.error(f"[feishu] {action.capitalize()} request HTTP error: {e.code} {e.reason}")
        # 注意：新建会话时 payload 中没有 session_id，对应的错误通知不会关联会话
        # 这符合预期，因为会话根本没创建成功
        _send_error_notification(chat_id, error_msg, reply_to=reply_to,
                                 session_id=session_id,
                                 project_dir=payload.get('project_dir', ''),
                                 reply_in_thread=reply_in_thread)

    except urllib.error.URLError as e:
        logger.error(f"[feishu] {action.capitalize()} request URL error: {e.reason}")
        _send_error_notification(chat_id, f"Callback 服务不可达: {e.reason}", reply_to=reply_to,
                                 session_id=session_id,
                                 project_dir=payload.get('project_dir', ''),
                                 reply_in_thread=reply_in_thread)

    return session_id


def _forward_continue_request(binding: dict, session_id: str, project_dir: str,
                              prompt: str, chat_id: str, message_id: str,
                              command: str = '', agent_type: str = '') -> str:
    """转发继续会话请求到 Callback 后端

    Args:
        binding: 绑定信息（包含 auth_token, callback_url 等）
        session_id: 会话 ID
        project_dir: 项目目录
        prompt: 用户回复内容
        chat_id: 群聊 ID
        message_id: 用户消息 ID（用于回复）
        command: 指定使用的命令（可选）
        agent_type: agent 类型（可选；handle_continue_session 从 session store 读取，此参数用于通知文案动态展示）

    Returns:
        session_id
    """
    if not binding:
        logger.warning("[feishu] No binding found, cannot continue session")
        _send_error_notification(chat_id, "您尚未注册，无法使用此功能", reply_to=message_id,
                                 session_id=session_id, project_dir=project_dir)
        return session_id

    reply_in_thread = _should_reply_in_thread(binding, project_dir)

    data = {
        'session_id': session_id,
        'project_dir': project_dir,
        'prompt': prompt,
        'chat_id': chat_id,
        'message_id': message_id
    }
    if command:
        data['command'] = command
    if agent_type:
        data['agent_type'] = agent_type

    return _forward_agent_request(binding, '/cb/agent/continue',
                                   data, chat_id, reply_to=message_id,
                                   reply_in_thread=reply_in_thread)


def _forward_new_request(binding: dict, session_id: str, project_dir: str, prompt: str,
                         chat_id: str, message_id: str, chat_type: str = '',
                         command: str = '', agent_type: str = '') -> str:
    """转发新建会话请求到 Callback 后端

    Args:
        binding: 绑定信息（包含 auth_token, callback_url 等）
        session_id: 会话 ID（由调用方生成）
        project_dir: 项目工作目录
        prompt: 用户输入的 prompt
        chat_id: 聊天 ID（P2P 或群聊）
        message_id: 原始消息 ID（用作 reply_to）
        chat_type: 聊天类型（group/p2p），用于 group 模式下的 chat_id 决策
        command: 指定使用的命令（可选）
        agent_type: agent 类型（如 'claude', 'codex'），可选

    Returns:
        session_id，失败时返回空字符串
    """
    if not binding:
        logger.warning("[feishu] No binding found, cannot create session")
        # 注意：此处不关联会话，因为会话尚未创建（用户未注册）
        # 用户回复此错误通知没有意义，应先完成注册
        _send_error_notification(chat_id, "您尚未注册，无法使用此功能", reply_to=message_id)
        return ''

    reply_in_thread = _should_reply_in_thread(binding, project_dir)
    session_mode = binding.get('session_mode', '')

    # 确定目标 chat_id 和 skip_user_prompt
    target_chat_id = chat_id
    skip_user_prompt = True  # 默认跳过（飞书发起的 prompt 已在飞书展示）

    if session_mode == 'group':
        from stores.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        owner_id = binding.get('_owner_id', '')
        if not gs_store or not owner_id:
            run_in_background(_send_notice_message, (chat_id, "存储服务未就绪，请稍后重试", message_id))
            return ''
        if chat_type != 'group':
            # P2P /new（group 模式）：不预建群，让 callback ensure-chat 统一处理
            target_chat_id = ''
            skip_user_prompt = False  # prompt 未在新群展示，由 hook 发送
        elif target_chat_id:
            # 群内 /new：gateway 直接登记当前群的路由表（不调建群 API，群已存在）
            gs_store.save(owner_id, target_chat_id, session_id, project_dir=project_dir)

    data = {
        'project_dir': project_dir,
        'prompt': prompt,
        'chat_id': target_chat_id,
        'message_id': message_id,
        'session_id': session_id,
        'skip_user_prompt': skip_user_prompt
    }
    if command:
        data['command'] = command
    if agent_type:
        data['agent_type'] = agent_type

    # 新建会话时传递 reply_to，让第一条通知回复用户的 /new 消息
    # 后续通知会通过 last_message_id 链式回复
    final_session_id = _forward_agent_request(binding, '/cb/agent/new',
                                               data, chat_id, reply_to=message_id,
                                               reply_in_thread=reply_in_thread)

    # Codex 路径：callback 可能将临时 UUID 替换为从 CLI 输出捕获的真实 session ID，
    # 需同步更新 GroupSessionStore 的路由映射，否则后续 continue 仍查旧 ID
    if final_session_id and final_session_id != session_id:
        from stores.group_session_store import GroupSessionStore
        gs_store = GroupSessionStore.get_instance()
        owner_id = binding.get('_owner_id', '')
        if gs_store and owner_id:
            route_chat_id = gs_store.find_by_session(owner_id, session_id)
            if route_chat_id:
                gs_store.save(owner_id, route_chat_id, final_session_id, project_dir=project_dir)
                logger.info("[feishu] Updated group-session route: %s -> %s (was %s)",
                            route_chat_id, final_session_id, session_id)

    return final_session_id


def _forward_new_request_for_default_dir(binding: Dict[str, Any], session_id: str,
                                         project_dir: str, prompt: str,
                                         chat_id: str, message_id: str, chat_type: str = '',
                                         command: str = '', agent_type: str = '') -> str:
    """转发默认聊天新建会话请求，完成后将 session_id 持久化到 BindingStore

    此函数在后台线程运行，是 _forward_new_request 的包装：
    转发请求后将返回的 session_id 写入 BindingStore。

    Returns:
        session_id
    """
    from stores.binding_store import BindingStore

    session_id = _forward_new_request(binding, session_id, project_dir, prompt, chat_id, message_id, chat_type, command, agent_type)

    owner_id = binding.get('_owner_id', '') if binding else ''
    if session_id and owner_id:
        binding_store = BindingStore.get_instance()
        if binding_store:
            binding_store.update_field(owner_id, 'default_chat_session_id', session_id)
            logger.info(f"[default-chat] Persisted session {session_id} for {owner_id}")

    return session_id


# =============================================================================
# /attach 转发
# =============================================================================

def _forward_attach_request(binding: Dict[str, Any], prefix: str,
                            chat_id: str, message_id: str) -> None:
    """转发 /attach 请求到 Callback 后端并反馈结果

    Callback 响应结构：
        {
            'matched_ids': list[str],
            'attached': bool,
            'session_id': str,
            'original_chat_id': str,
            'project_dir': str,
        }

    dissolve_days 从 gateway 本地 binding 读取，不再经 callback 传递。
    """
    from stores.group_chat_store import GroupChatStore
    from stores.group_session_store import GroupSessionStore

    group_store = GroupChatStore.get_instance()
    gs_store = GroupSessionStore.get_instance()
    if not group_store or not gs_store:
        _send_notice_message(chat_id, "存储服务未就绪，请稍后重试", message_id)
        return

    from services.session_facade import SessionFacade
    resp = SessionFacade.attach(binding, prefix, chat_id)

    if not resp:
        _send_notice_message(chat_id, "Callback 服务不可达", message_id)
        return

    matched_ids = resp.get('matched_ids', [])

    if not matched_ids:
        _send_notice_message(chat_id, f"未找到匹配的 session：`{prefix}`", message_id)
        return

    if len(matched_ids) > 1:
        preview = '、'.join(s[:12] + '…' for s in matched_ids[:3])
        _send_notice_message(chat_id,
                             f"前缀匹配到多个 session（{preview}），请输入更长的前缀",
                             message_id)
        return

    # 唯一匹配已由 callback 侧执行绑定
    if not resp.get('attached'):
        _send_notice_message(chat_id, "绑定失败，请查看日志", message_id)
        return

    session_id = resp.get('session_id', '')
    original_chat_id = resp.get('original_chat_id', '')
    dissolve_days = binding.get('group_dissolve_days', 0) or 0

    # gateway 自查原群 seq（attach 到自己所在群时 original==target，不提示孤儿群）
    original_seq: Optional[int] = None
    if original_chat_id and original_chat_id != chat_id:
        original_seq = group_store.get_seq(original_chat_id)

    session_mode = binding.get('session_mode', '') if binding else ''
    is_group_mode = (session_mode == 'group')
    owner_id = binding.get('_owner_id', '') if binding else ''

    # 同步 gateway 侧 GroupSessionStore（仅 group 模式需要路由表）：
    # save 内部会自动清理本 session 的旧 chat 映射（若仍指向本 session），
    # 不需要外部显式 remove（避免误删已被其他 session 接管的映射）
    if is_group_mode and owner_id:
        gs_store.save(owner_id, chat_id, session_id, project_dir=resp.get('project_dir', ''))

    # 构造反馈
    lines = [f"✅ Session `{session_id[:8]}` 已绑定到当前群聊"]
    if is_group_mode:
        if original_seq is not None:
            hint = f"💡 原群聊 #{original_seq} 已成为孤儿群，可按需通过 `/groups dissolve {original_seq}` 手动解散"
            if dissolve_days > 0:
                hint += f"（空闲超过 {dissolve_days} 天也会被自动解散）"
            lines.append(hint)
    else:
        lines.append("⚠️ 当前会话模式非 group，后续会话通知会发送到本群，"
                     "但在本群直接发送消息（非回复）不会被自动路由到该 session")
    _send_notice_message(chat_id, '\n'.join(lines), message_id)


# =============================================================================
# /stop 转发
# =============================================================================

def _forward_stop_request(binding: dict, session_id: str,
                          chat_id: str, message_id: str) -> None:
    """转发 /stop 请求到 Callback 后端"""
    payload = {'session_id': session_id}
    try:
        response = _forward_via_ws_or_http(binding, '/cb/agent/stop', payload, timeout=10)
    except Exception as e:
        logger.error("[feishu] /stop forward failed: %s", e)
        _send_notice_message(chat_id, "停止请求失败，请稍后重试", message_id)
        return

    if response is None:
        _send_notice_message(chat_id, "Callback 服务不可达，请检查服务状态", message_id)
        return

    stopped = response.get('stopped', False)
    queue_cleared = response.get('queue_cleared', 0)

    if stopped and queue_cleared > 0:
        text = "已停止当前任务，并清空 %d 条排队指令" % queue_cleared
    elif stopped:
        text = "已停止当前任务"
    elif queue_cleared > 0:
        text = "当前没有执行中的任务，已清空 %d 条排队指令" % queue_cleared
    else:
        text = "当前没有执行中的任务"

    _send_notice_message(chat_id, text, message_id)


# =============================================================================
# 权限决策转发
# =============================================================================

def _forward_permission_request(request_id: str, original_data: dict, action_type: str,
                                card_message_id: str = '') -> Tuple[bool, dict]:
    """转发权限请求到 Callback 服务

    调用 callback 服务的纯决策接口，根据返回的决策结果生成 toast。
    优先使用 WS 隧道，fallback 到 HTTP。
    callback_url 从 BindingStore 获取。

    注意：飞书要求在 3 秒内返回响应，timeout 设置为 2 秒预留时间。

    Args:
        request_id: 请求 ID
        original_data: 原始飞书事件数据（用于提取绑定信息和 project_dir）
        action_type: 动作类型 (allow/always/deny/interrupt)
        card_message_id: 卡片消息 ID（用于添加表情）

    Returns:
        (handled, toast_response)
    """
    import urllib.error

    # 提取 project_dir（从原始请求的 value 中获取）
    event = original_data.get('event', {})
    action = event.get('action', {})
    value = action.get('value', {})

    # 获取绑定信息
    binding = _get_binding_from_event(event)
    if not binding:
        logger.warning("[feishu] No binding found for permission request")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '身份验证失败，请重新注册网关'
            }
        }

    owner_id = binding.get('_owner_id', '')

    # 构建请求数据
    request_data = {
        'action': action_type,
        'request_id': request_id
    }

    # 添加可选字段
    if 'project_dir' in value:
        request_data['project_dir'] = value['project_dir']

    logger.info("[feishu] Forwarding permission request: owner_id=%s, action=%s", owner_id, action_type)

    start_time = time.time()

    try:
        # 使用 WS/HTTP 路由分发
        # 飞书要求 3 秒内返回，设置 2 秒超时预留处理时间
        response_data = _forward_via_ws_or_http(binding, '/cb/decision', request_data, timeout=2)

        if response_data is None:
            logger.warning("[feishu] Forward failed: no available route")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '回调服务不可达，请检查服务状态'
                }
            }

        elapsed = (time.time() - start_time) * 1000

        success = response_data.get('success', False)
        decision = response_data.get('decision')
        message = response_data.get('message', '')

        # 根据决策结果生成 toast
        response_body = {}
        if success and decision:
            if decision == 'allow':
                toast_type = TOAST_SUCCESS
            else:  # deny
                toast_type = TOAST_WARNING
            toast_content = message or ('已批准运行' if decision == 'allow' else '已拒绝运行')
            logger.info(f"[feishu] Decision succeeded: decision={decision}, message={message}, elapsed={elapsed:.0f}ms")
            # 决策成功后，异步添加 Typing 表情（拒绝并中断时不需要，因为预期任务会停止）
            if action_type != 'interrupt':
                run_in_background(_add_typing_reaction, (card_message_id,))

            # 尝试在回调响应中返回更新后的卡片（移除按钮，更新状态）
            from .card_action import _get_updated_card_for_response
            updated_card = _get_updated_card_for_response(request_id, action_type)
            if updated_card:
                response_body['card'] = {
                    'type': 'raw',
                    'data': updated_card
                }
                logger.debug(f"[feishu] Returning updated card in response for request: {request_id}")
        else:
            toast_type = TOAST_ERROR
            toast_content = message or '处理失败'
            logger.warning(f"[feishu] Decision failed: message={toast_content}, elapsed={elapsed:.0f}ms")

        response_body['toast'] = {
            'type': toast_type,
            'content': toast_content
        }
        return True, response_body

    except urllib.error.HTTPError as e:
        logger.error(f"[feishu] Forward HTTP error: {e.code} {e.reason}")
        # 401 表示 auth_token 验证失败
        if e.code == 401:
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '身份验证失败，请重新注册网关'
                }
            }
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': f'回调服务错误: HTTP {e.code}'
            }
        }
    except urllib.error.URLError as e:
        logger.error(f"[feishu] Forward URL error: {e.reason}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '回调服务不可达，请检查服务状态'
            }
        }
    except socket.timeout:
        logger.error("[feishu] Forward timeout")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '回调服务响应超时'
            }
        }
    except Exception as e:
        logger.error(f"[feishu] Forward error: {e}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': f'转发失败: {str(e)}'
            }
        }


# =============================================================================
# 目录查询
# =============================================================================

def _fetch_recent_dirs_from_callback(binding: Dict[str, Any], limit: int = 5) -> list:
    """从 Callback 后端获取近期常用目录列表

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        limit: 最多返回的目录数量

    Returns:
        目录路径列表
    """
    request_data = {
        'limit': limit
    }

    try:
        response_data = _forward_via_ws_or_http(binding, '/cb/directory/recent-dirs', request_data)

        if response_data is None:
            return []

        recent_dirs = response_data.get('dirs', [])
        logger.info(f"[feishu] Fetched {len(recent_dirs)} recent dirs from callback")
        return recent_dirs

    except Exception as e:
        logger.error(f"[feishu] Fetch recent dirs error: {e}")
        return []


def _fetch_browse_dirs_from_callback(binding: Dict[str, Any], path: str) -> dict:
    """从 Callback 后端获取指定路径下的子目录列表

    优先使用 WS 隧道，fallback 到 HTTP。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        path: 要浏览的路径

    Returns:
        包含 dirs, parent, current 的字典，失败时返回空字典
    """
    request_data = {
        'path': path
    }

    try:
        response_data = _forward_via_ws_or_http(binding, '/cb/directory/browse-dirs', request_data)

        if response_data is None:
            return {}

        logger.info(f"[feishu] Fetched browse result: {len(response_data.get('dirs', []))} dirs from {path}")
        return response_data

    except Exception as e:
        logger.error(f"[feishu] Browse dirs error: {e}")
        return {}
