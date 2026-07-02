"""
Feishu Card Action - 卡片交互处理

处理飞书卡片回传交互事件（card.action.trigger）：
- _handle_card_action: 卡片交互总入口
- _handle_new_session_form: 新会话表单提交处理
- _handle_browse_directory: 浏览目录表单处理
- _apply_custom_overrides: 表单自定义覆盖
- _handle_ask_question_answer: 问卷回答处理

卡片状态更新：
- _extract_request_id_from_card: 从卡片提取 request_id
- _get_updated_card_for_response: 获取响应对应的卡片更新
- _build_updated_card: 构建更新后的卡片
- _apply_submitted_form_state_to_element: 应用表单状态到元素
"""

import copy
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from utils.concurrency import run_in_background

from .utils import (
    TOAST_SUCCESS, TOAST_ERROR, TOAST_INFO,
    _sanitize_user_content,
    _verify_operator_match, _get_binding_from_event,
    _resolve_agent_command_from_binding,
)
from .message import (
    _add_typing_reaction,
    _build_creating_session_card,
)
from .forward import (
    _forward_via_ws_or_http,
    _forward_new_request,
    _forward_permission_request,
    _fetch_browse_dirs_from_callback,
)
from .card_session import (
    _build_browse_result_card,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 卡片状态配置
# =============================================================================

_CARD_STATUS_CONFIG = {
    'allow': {'template': 'green', 'title_suffix': ' - 已批准'},
    'always': {'template': 'green', 'title_suffix': ' - 已批准（始终允许）'},
    'deny': {'template': 'red', 'title_suffix': ' - 已拒绝'},
    'interrupt': {'template': 'red', 'title_suffix': ' - 已拒绝并中断'},
    'answer': {'template': 'green', 'title_suffix': ' - 已回答'},
}


# =============================================================================
# 卡片交互处理
# =============================================================================

def _handle_card_action(data: dict) -> Tuple[bool, dict]:
    """处理飞书卡片回传交互事件 card.action.trigger

    当用户点击卡片中的 callback 类型按钮或提交 form 表单时，飞书会发送此事件。
    服务器需要在 3 秒内返回响应，可返回 toast 提示。

    支持的动作类型：
    1. allow/always/deny/interrupt: 权限决策
    2. approve_register/deny_register/unbind_register: 注册授权
    3. Form 表单提交：创建新会话时，选择工作目录 + 填写提示词的表单

    Args:
        data: 飞书事件数据

    Returns:
        (handled, toast_response)
    """
    # 打印完整数据用于调试
    logger.info(f"[feishu] _handle_card_action received data:\n{json.dumps(data, ensure_ascii=True, indent=2)}")

    # 提取事件公共信息
    header = data.get('header', {})
    event = data.get('event', {})
    action = event.get('action', {})
    operator = event.get('operator', {})

    # 记录日志
    event_id = header.get('event_id', '')
    user_id = operator.get('open_id', operator.get('user_id', 'unknown'))
    logger.info(f"[feishu] Card action: event_id={event_id}, user={user_id}")

    # 提取数据：callback 按钮的数据在 value 中，form 表单的数据在 form_value 中
    value = action.get('value', {})
    form_value = action.get('form_value', {})

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 统一身份验证：如果卡片 value 中有 owner_id，必须与 operator 匹配    │
    # │ 适用于：Callback 按钮点击、Form 表单提交                          │
    # └────────────────────────────────────────────────────────────────┘
    owner_id = value.get('owner_id', '')
    if owner_id and not _verify_operator_match(operator, owner_id):
        logger.warning(
            f"[feishu] Operator verification failed: owner_id={owner_id} not found in operator={operator}"
        )
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '只有本人才能执行此操作'
            }
        }

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 1: 新会话表单提交（目录选择 + prompt 输入）                    │
    # │ 识别标志：按钮名称为 submit_btn 或 browse_*_btn                   │
    # └────────────────────────────────────────────────────────────────┘
    trigger_name = action.get('name', '')
    new_session_form_buttons = ('submit_btn', 'browse_dir_select_btn', 'browse_custom_btn', 'browse_result_btn')
    if trigger_name in new_session_form_buttons:
        return _handle_new_session_form(data, form_value)

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 2: Callback 按钮点击（权限决策、注册授权等）                   │
    # │ 提取动作参数：action_type, request_id                            │
    # │ callback_url 从 BindingStore 获取（注册场景除外）                  │
    # └────────────────────────────────────────────────────────────────┘
    action_type = value.get('action', '')  # allow/always/deny/interrupt/approve_register/deny_register
    request_id = value.get('request_id', '')

    logger.info(
        f"[feishu] Card action: action={action_type}, request_id={request_id}"
    )

    # 处理注册授权
    if action_type in ('approve_register', 'deny_register', 'unbind_register'):
        from .group import handle_card_action_register
        return handle_card_action_register(value)

    # 处理权限决策
    if not action_type or not request_id:
        logger.warning(f"[feishu] Card action missing params: action={action_type}, request_id={request_id}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '无效的回调请求'
            }
        }

    # 提取卡片消息 ID（用于添加表情）
    context = event.get('context', {})
    card_message_id = context.get('open_message_id', '')

    # AskUserQuestion 表单提交（action=answer）
    if action_type == 'answer':
        return _handle_ask_question_answer(request_id, form_value, data,
                                           card_message_id=card_message_id)

    # 调用 callback_url 的决策接口（callback_url 从 BindingStore 获取）
    return _forward_permission_request(request_id, data, action_type,
                                       card_message_id=card_message_id)


def _handle_new_session_form(card_data: dict, form_values: dict) -> Tuple[bool, dict]:
    """处理新会话表单提交（异步模式）

    支持两种操作：
    1. 点击"浏览"按钮 → 返回更新后的卡片（显示子目录列表）
    2. 点击"创建会话"按钮 → 立即返回"处理中"响应，后台异步执行会话创建

    Args:
        card_data: 完整的飞书卡片事件数据
        form_values: 表单提交的数据（包含 recent_dir, custom_dir, prompt, browse_result）

    Returns:
        (handled, response): handled 始终为 True，response 包含 toast 和卡片更新
    """
    event = card_data.get('event', {})
    action = event.get('action', {})

    # 获取触发按钮名称（飞书 Card 2.0 Form 提交时，按钮名称在 action.name）
    trigger_name = action.get('name', '')
    logger.info(f"[feishu] Form trigger_name: {trigger_name}")

    # 从按钮的 value 中提取 chat_id、message_id 和 chat_type（用户原始消息 ID）
    button_value = action.get('value', {})
    chat_id = button_value.get('chat_id', '')
    message_id = button_value.get('message_id', '')
    chat_type = button_value.get('chat_type', '')

    # 提取提交者 user_id（注入子进程 env 后由 stop 卡片优先 at）
    sender_id = event.get('operator', {}).get('user_id', '')

    # 从表单数据中提取字段
    recent_dir = form_values.get('recent_dir', '')  # 常用目录下拉选择的值
    custom_dir = form_values.get('custom_dir', '')  # 自定义路径输入框的值
    browse_result = form_values.get('browse_result', '')  # 浏览结果下拉选择的值
    prompt = form_values.get('prompt', '')
    # 兼容新旧卡片：新卡片用 agent_command，旧卡片用 claude_command
    agent_command = form_values.get('agent_command', '') or form_values.get('claude_command', '')

    # 获取 binding（用于解析默认命令和后续请求）
    binding = _get_binding_from_event(event)

    # 解析 agent_type 和 command
    if '::' in agent_command:
        agent_type, command = agent_command.split('::', 1)
    else:
        agent_type = ''
        command = agent_command

    # 如果没有选择命令，从 binding 获取默认命令
    if not command:
        ok, agent_type, result = _resolve_agent_command_from_binding(binding, '')
        if not ok:
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': result
                }
            }
        command = result

    logger.info(f"[feishu] Form values: recent_dir={recent_dir}, custom_dir={custom_dir}, browse_result={browse_result}, agent_type={agent_type}, command={command}, prompt={_sanitize_user_content(prompt)}, trigger={trigger_name}")

    if not chat_id:
        logger.warning("[feishu] No chat_id in button value")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '无法获取群聊信息'
            }
        }

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 1: 点击"浏览"按钮（支持 browse_custom_btn 和 browse_result_btn）│
    # └────────────────────────────────────────────────────────────────┘
    if trigger_name in ('browse_dir_select_btn', 'browse_custom_btn', 'browse_result_btn'):
        return _handle_browse_directory(trigger_name, recent_dir, custom_dir, chat_id, message_id, chat_type, event, form_values)

    # ┌────────────────────────────────────────────────────────────────┐
    # │ 分支 2: 点击"创建会话"按钮（trigger_name = submit_btn）           │
    # └────────────────────────────────────────────────────────────────┘

    # 按优先级确定目录：browse_result > custom_dir > recent_dir
    # 用户从"选择子目录"中选中的优先级最高，其次才是自定义路径输入框
    selected_dir = browse_result or custom_dir or recent_dir

    if not selected_dir:
        logger.warning("[feishu] No working directory selected in form submission")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '请选择或输入一个工作目录'
            }
        }

    if not prompt:
        logger.warning("[feishu] No prompt provided")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '请输入您的问题'
            }
        }

    # 立即返回"处理中"响应
    response = {
        'toast': {
            'type': TOAST_INFO,
            'content': '正在创建会话...'
        },
        'card': _build_creating_session_card(selected_dir, prompt, command, agent_type=agent_type)
    }

    # 在后台线程中异步执行会话创建
    new_session_id = str(uuid.uuid4())
    run_in_background(_forward_new_request, (binding, new_session_id, selected_dir, prompt, chat_id, message_id, sender_id, chat_type, command, agent_type))

    return True, response


def _handle_browse_directory(trigger_name: str, recent_dir: str, custom_dir: str,
                             chat_id: str, message_id: str, chat_type: str,
                             feishu_event: dict, form_values: dict) -> Tuple[bool, dict]:
    """处理浏览目录按钮点击

    调用 browse-dirs 接口获取子目录列表，返回更新后的卡片。

    Args:
        trigger_name: 触发的按钮名称 (browse_dir_select_btn, browse_custom_btn 或 browse_result_btn)
        recent_dir: 常用目录下拉框选择的值
        custom_dir: 用户输入的自定义路径
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        chat_type: 聊天类型（group/p2p），透传到重建的卡片
        feishu_event: 飞书事件数据
        form_values: 表单数据（用于回填）

    Returns:
        (handled, response): handled 始终为 True，response 包含更新后的卡片
    """
    # 获取绑定信息
    binding = _get_binding_from_event(feishu_event)
    if not binding:
        logger.warning("[feishu] No binding found for browse")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '无法获取认证信息'
            }
        }

    # 从表单数据中获取 browse_result（用户可能从浏览结果下拉菜单中选择了子目录）
    browse_result = form_values.get('browse_result', '')

    # 根据按钮名称确定浏览路径
    if trigger_name == 'browse_dir_select_btn':
        # 点击常用目录旁边的"浏览"：必须先选择目录
        if not recent_dir:
            logger.warning("[feishu] No recent_dir selected")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '请先从常用目录中选择一个目录'
                }
            }
        browse_path = recent_dir
        logger.info(f"[feishu] Browse recent_dir select: {browse_path}")
    elif trigger_name == 'browse_custom_btn':
        # 点击自定义路径旁边的"浏览"：使用 custom_dir
        browse_path = custom_dir or '/'
        logger.info(f"[feishu] Browse custom path: {browse_path}")
    elif trigger_name == 'browse_result_btn':
        # 点击浏览结果旁边的"浏览"：必须先选择子目录
        if not browse_result:
            logger.warning("[feishu] No browse result selected")
            return True, {
                'toast': {
                    'type': TOAST_ERROR,
                    'content': '请先从浏览结果中选择一个子目录'
                }
            }
        browse_path = browse_result
        logger.info(f"[feishu] Browse result path: {browse_path}")
    else:
        # 默认：优先使用 custom_dir（用户主动输入），其次使用 recent_dir
        browse_path = custom_dir or recent_dir or '/'
        logger.info(f"[feishu] Browse default path: {browse_path}")

    # 调用 browse-dirs 接口
    browse_data = _fetch_browse_dirs_from_callback(binding, browse_path)
    if not browse_data:
        logger.error(f"[feishu] Failed to browse dirs: {browse_path}")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '浏览目录失败'
            }
        }

    # 计算应该回填到 custom_dir 输入框的值
    if trigger_name == 'browse_result_btn':
        custom_dir_value = browse_result  # 回填为选中的子目录
    elif trigger_name == 'browse_dir_select_btn':
        # 如果自定义输入框有值，保持不变；否则回填为当前浏览路径
        custom_dir_value = custom_dir if custom_dir else browse_data.get('current', '')
    else:  # browse_custom_btn
        custom_dir_value = browse_data.get('current', '')  # 回填为当前浏览路径

    # 构建更新后的卡片
    card = _build_browse_result_card(
        browse_data=browse_data,
        form_values=form_values,
        custom_dir_value=custom_dir_value,  # 传入计算好的回填值
        chat_id=chat_id,
        message_id=message_id,
        chat_type=chat_type,
        feishu_event=feishu_event
    )

    return True, {'card': {'type': 'raw', 'data': card}}


def _apply_custom_overrides(form_value: Dict[str, Any]) -> Tuple[Dict[str, Any], List[int]]:
    """让单选题的 q_{i}_custom 覆盖 q_{i}_select，返回清理后的 form_value 副本
    和被覆盖的题号列表。

    form_value 字段命名（q_{i}_select / q_{i}_custom）由 src/lib/feishu.sh
    渲染卡片时写死。本函数集中处理该约定：

    - **判定**：单选题同时有非空 q_{i}_select 和 q_{i}_custom 时视为"覆盖"。
      多选题（select 值是 list）不存在覆盖语义；未选中（空串）也不算。
    - **清理**：从 form_value 副本中剥离被覆盖题目的 q_{i}_select 字段，
      使卡片更新时只回显用户最终的 custom 内容。
    - **题号**：0-based 升序列表，供 toast 文案使用（展示时由调用方 +1
      转成"第 X 题"）。

    不修改入参；返回的是新的 dict。

    Returns:
        (cleaned_form_value, overridden_indices)
    """
    cleaned = dict(form_value)  # 浅拷贝
    prefix, suffix = 'q_', '_select'
    overridden = []
    for key, value in cleaned.items():
        # 只看 q_{i}_select 字段；q_{i}_custom 及其它键由各自消费方处理
        if not (key.startswith(prefix) and key.endswith(suffix)):
            continue
        # value 是 list -> 多选题（不存在"custom 覆盖 select"的语义）
        # value 为空串 -> 单选未选中，自然谈不上覆盖
        if isinstance(value, list) or not value:
            continue
        # 防御：过滤形如 q_foo_select 的异常键，保证 idx 是纯数字
        idx_str = key[len(prefix):-len(suffix)]
        if not idx_str.isdigit():
            continue
        idx = int(idx_str)
        # 同题 custom 非空，才算"自定义输入覆盖了下拉"
        if cleaned.get(f'q_{idx}_custom', ''):
            overridden.append(idx)
    overridden.sort()

    # 延迟到识别完成后再剥离 select，避免迭代 dict 时修改导致 RuntimeError
    for idx in overridden:
        cleaned.pop(f'q_{idx}_select', None)

    return cleaned, overridden


def _handle_ask_question_answer(request_id: str, form_value: dict, original_data: dict,
                                card_message_id: str = '') -> Tuple[bool, dict]:
    """处理 AskUserQuestion 表单提交

    从 form_value 中提取用户的选择/输入，构造 answers dict，
    然后调用 callback 服务的决策接口。

    Args:
        request_id: 请求 ID
        form_value: 表单提交数据，包含 q_0_select, q_0_custom 等字段
        original_data: 原始飞书事件数据
        card_message_id: 卡片消息 ID（用于添加表情）

    Returns:
        (handled, toast_response)
    """
    logger.info("[feishu] Handling AskUserQuestion answer: request_id=%s", request_id)
    logger.debug("[feishu] form_value: %s", json.dumps(form_value, ensure_ascii=False, indent=2))

    # 获取绑定信息
    event = original_data.get('event', {})
    binding = _get_binding_from_event(event)
    if not binding:
        logger.warning("[feishu] No binding found for AskUserQuestion request")
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': '身份验证失败，请重新注册网关'
            }
        }

    # 单选题若 custom 非空则覆盖下拉：清除对应 q_{i}_select 字段并收集题号，
    # 用于卡片更新展示和 toast 文案（多选题不存在覆盖语义）。
    form_value, overridden_questions = _apply_custom_overrides(form_value)

    # 构建请求数据：只透传 form_value，由 callback 端生成 answers/questions
    request_data = {
        'action': 'answer',
        'request_id': request_id,
        'form_value': form_value,
    }

    start_time = time.time()

    try:
        # 使用 WS/HTTP 路由分发
        response_data = _forward_via_ws_or_http(binding, '/cb/decision', request_data, timeout=2)

        if response_data is None:
            logger.warning("[feishu] AskUserQuestion forward failed: no available route")
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

        response_body = {}
        if success and decision:
            toast_type = TOAST_SUCCESS
            toast_content = message or '已提交回答'
            # 如果有单选题被自定义内容覆盖，在提示中告知用户
            if overridden_questions:
                nums = '、'.join(f'第{idx + 1}题' for idx in overridden_questions)
                toast_content += f'（{nums}的自定义内容已覆盖选项）'
            logger.info("[feishu] AskUserQuestion succeeded: decision=%s, elapsed=%.0fms", decision, elapsed)
            # 决策成功后，异步添加 Typing 表情
            run_in_background(_add_typing_reaction, (card_message_id,))

            # 尝试在回调响应中返回更新后的卡片
            updated_card = _get_updated_card_for_response(request_id, 'answer', form_value=form_value)
            if updated_card:
                response_body['card'] = {
                    'type': 'raw',
                    'data': updated_card
                }
                logger.debug("[feishu] Returning updated card in response for AskUserQuestion: %s", request_id)
        else:
            toast_type = TOAST_ERROR
            toast_content = message or '提交失败'
            logger.warning("[feishu] AskUserQuestion failed: message=%s, elapsed=%.0fms", toast_content, elapsed)

        response_body['toast'] = {
            'type': toast_type,
            'content': toast_content
        }
        return True, response_body

    except Exception as e:
        logger.error("[feishu] AskUserQuestion error: %s", e)
        return True, {
            'toast': {
                'type': TOAST_ERROR,
                'content': f'提交失败: {str(e)}'
            }
        }


# =============================================================================
# 卡片状态更新
# =============================================================================

def _extract_request_id_from_card(card_content: dict) -> Optional[str]:
    """从卡片中提取第一个 callback value.request_id

    用于在卡片发送成功后定位缓存 key。
    同一张审批/问答卡中的回调按钮通常共享同一个 request_id，
    因此取第一个命中的 request_id 即可。
    """
    def _extract_from_element(elem: dict) -> Optional[str]:
        if not isinstance(elem, dict):
            return None

        # 先检查当前元素自身是否挂了 callback behavior
        behaviors = elem.get('behaviors', [])
        if isinstance(behaviors, list):
            for behavior in behaviors:
                if isinstance(behavior, dict) and behavior.get('type') == 'callback':
                    value = behavior.get('value', {})
                    if isinstance(value, dict) and value.get('request_id'):
                        return value['request_id']

        # 递归遍历常见容器节点，查找嵌套按钮上的 callback value.request_id
        for key in ['elements', 'columns']:
            children = elem.get(key, [])
            if isinstance(children, list):
                for child in children:
                    request_id = _extract_from_element(child)
                    if request_id:
                        return request_id

        return None

    body = card_content.get('body', {})
    elements = body.get('elements', [])
    for elem in elements:
        request_id = _extract_from_element(elem)
        if request_id:
            return request_id
    return None


def _get_updated_card_for_response(request_id: str, action_type: str,
                                   form_value: Optional[dict] = None) -> Optional[dict]:
    """获取更新后的卡片 JSON（用于回调响应中返回）

    Args:
        request_id: 请求 ID（用作卡片缓存 key）
        action_type: 动作类型 (allow/always/deny/interrupt/answer)
        form_value: 表单提交的值（用于回填 AskUserQuestion 卡片的选项和输入）

    Returns:
        更新后的卡片 JSON dict，失败返回 None
    """
    from services.card_cache import CardCache

    cache = CardCache.get_instance()
    if not cache:
        return None

    card_json_str = cache.get(request_id)
    if not card_json_str:
        logger.info("[feishu] Card cache miss for request_id=%s", request_id)
        return None

    try:
        card_info = json.loads(card_json_str)
    except (json.JSONDecodeError, TypeError):
        logger.warning("[feishu] Failed to parse cached card for request_id=%s", request_id)
        return None

    updated_card = _build_updated_card(card_info, action_type, form_value=form_value)
    if updated_card:
        cache.delete(request_id)
    return updated_card


def _build_updated_card(card_content: dict, action_type: str, form_value: Optional[dict] = None) -> Optional[dict]:
    """构建更新后的卡片（禁用按钮，更新 header，回填表单值）

    Args:
        card_content: 原始卡片内容 dict
        action_type: 动作类型 (allow/always/deny/interrupt/answer)
        form_value: 表单提交的值（用于回填选项和输入框）

    Returns:
        更新后的卡片 dict，失败返回 None
    """
    try:
        card = copy.deepcopy(card_content)

        # 更新 header
        config = _CARD_STATUS_CONFIG.get(action_type, {})
        header = card.get('header', {})
        if config.get('template'):
            header['template'] = config['template']
        title = header.get('title', {})
        if title.get('content') and config.get('title_suffix'):
            title['content'] = title['content'] + config['title_suffix']

        # 禁用卡片中的所有按钮，回填表单值
        elements = card.get('body', {}).get('elements', [])
        for elem in elements:
            _apply_submitted_form_state_to_element(elem, form_value)
        return card

    except Exception as e:
        logger.error("[feishu] Failed to build updated card: %s", e)
        return None


def _apply_submitted_form_state_to_element(elem: dict, form_value: Optional[dict] = None):
    """递归更新元素：禁用按钮，将下拉选择和输入框转换为已禁用的 checker 勾选器

    Args:
        elem: 卡片元素 dict
        form_value: 表单提交的值（用于回填选项和输入框）

    支持的表单元素转换：
    - select_static / multi_select_static: 转换为 checker 列表（选中项勾选，全部禁用）
    - input: 有值时转换为已勾选的 checker（"自定义 - xxx"），无值时隐藏
    """
    # 禁用按钮
    if elem.get('tag') == 'button':
        elem['disabled'] = True

    # 回填表单值
    if form_value:
        tag = elem.get('tag', '')
        name = elem.get('name', '')

        if tag in ('select_static', 'multi_select_static') and name:
            value = form_value.get(name, '' if tag == 'select_static' else [])
            options = elem.get('options', [])
            # 确定选中的值集合
            if isinstance(value, list):
                selected_set = set(value)
            else:
                selected_set = {value} if value else set()
            # 将 select 替换为 checker 列表容器
            checkers = []
            for opt in options:
                opt_value = opt.get('value', '')
                opt_text = opt.get('text', {}).get('content', opt_value)
                checkers.append({
                    'tag': 'checker',
                    'name': f'{name}_opt_{opt_value}',
                    'checked': opt_value in selected_set,
                    'disabled': True,
                    'text': {'tag': 'plain_text', 'content': opt_text},
                    'overall_checkable': True,
                    'margin': '4px 0px 0px 0px',
                    'checked_style': {'show_strikethrough': False}
                })
            # 用 column_set 包装 checker 列表替换原 select 元素
            elem.clear()
            elem['tag'] = 'column_set'
            elem['flex_mode'] = 'none'
            elem['columns'] = [{
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'elements': checkers
            }]
        elif tag == 'input' and name:
            value = form_value.get(name, '')
            if value:
                # 有自定义输入：将 input 替换为已勾选的 checker
                elem.clear()
                elem['tag'] = 'checker'
                elem['name'] = name
                elem['checked'] = True
                elem['disabled'] = True
                elem['text'] = {'tag': 'plain_text', 'content': f'自定义 - {value}'}
                elem['overall_checkable'] = True
                elem['margin'] = '4px 0px 0px 0px'
                elem['checked_style'] = {'show_strikethrough': False}
            else:
                # 无自定义输入：隐藏输入框（清空为空 div）
                elem.clear()
                elem['tag'] = 'div'
                elem['text'] = {'tag': 'plain_text', 'content': ''}

    # 递归处理子元素
    for key in ['elements', 'columns']:
        children = elem.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _apply_submitted_form_state_to_element(child, form_value)
