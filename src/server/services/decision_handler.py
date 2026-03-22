"""
Decision Handler Service - 决策处理服务

功能：
    - 处理权限决策请求（allow/always/deny/interrupt/answer）
    - 提供统一的纯决策处理逻辑，不包含渲染信息
    - 调用方根据返回的决策结果自行生成响应（HTML/Toast/JSON 等）
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from models.decision import Decision
from services.request_manager import RequestManager
from services.rule_writer import write_always_allow_rule

logger = logging.getLogger(__name__)


def handle_decision(
    request_id: str,
    action: str,
    project_dir: Optional[str] = None,
    answers: Optional[Dict[str, str]] = None,
    questions: Optional[List[Dict[str, Any]]] = None
) -> Tuple[bool, Optional[str], Optional[str]]:
    """处理权限决策

    这是核心决策接口，返回纯决策结果。
    调用方根据返回结果自行生成响应（HTML 页面、Toast、JSON 等）。

    Args:
        request_id: 请求 ID
        action: 动作类型 (allow/always/deny/interrupt/answer)
        project_dir: 项目目录（可选，用于 always 时写入规则）
        answers: AskUserQuestion 的答案字典（仅 action=answer 时使用）
        questions: AskUserQuestion 的原始问题数组（仅 action=answer 时使用）

    Returns:
        (success, decision, message): 三元组
            - success: 是否成功处理
            - decision: 决策类型 ("allow"/"deny")，失败时为 None
            - message: 状态消息或错误信息
    """
    request_manager = RequestManager.get_instance()
    # 验证 request_id
    if not request_id:
        return False, None, '缺少请求 ID'

    # 验证 action
    valid_actions = ('allow', 'always', 'deny', 'interrupt', 'answer')
    if action not in valid_actions:
        return False, None, f'未知的动作类型: {action}'

    # 获取请求数据
    req_data = request_manager.get_request_data(request_id)
    if not req_data:
        return False, None, '请求不存在或已过期'

    # 先检查请求状态：如果已处理，直接返回（避免后续检查 hook 进程导致误判）
    req_status = request_manager.get_request_status(request_id)
    if req_status == request_manager.STATUS_RESOLVED:
        return False, None, '请求已被处理，请勿重复操作'
    if req_status == request_manager.STATUS_DISCONNECTED:
        return False, None, '连接已断开，Claude 可能已继续执行其他操作'

    # 提前保存 extra_data（resolve 后可能被清理）
    extra_data = {
        'project_dir': project_dir or req_data.get('project_dir'),
        'tool_name': req_data.get('tool_name'),
        'tool_input': req_data.get('tool_input', {})
    }

    # 检查 hook 进程是否存活
    #
    # Claude Code 行为细节（实验观察）：
    # - 用户在终端点 "no"（拒绝）时，Claude Code 会立即 kill hook 进程
    # - 用户在终端点 "yes"（批准）时，hook 进程可能不会立即被 kill
    #   （可能是因为 Claude Code 需要先启动工具执行）
    # - 服务端的 cleanup_disconnected 线程每 5 秒检测一次连接状态
    #   如果检测到连接断开会标记为 STATUS_DISCONNECTED
    #
    # 因此：
    # - 如果用户已在终端处理，hook 进程可能已退出（点 no）或还活着（点 yes）
    # - pid 检查只能作为参考，不能完全依赖它来判断用户是否已在终端处理
    # - 更可靠的判断是检查请求状态（STATUS_RESOLVED/STATUS_DISCONNECTED）
    #
    # 未来优化方向：
    # - 如果 Claude Code 在 PermissionRequest hook 输入中提供 tool_use_id，
    #   可以通过检查 transcript 文件中是否存在对应的 tool_result 来精准判断
    #   用户是否已在终端处理（参考 permission.sh 中的 check_tool_result_exists）
    # - 目前非 MCP 模式下 hook 输入中没有 tool_use_id，无法实现精准检测
    #
    hook_pid = req_data.get('hook_pid')
    if hook_pid:
        try:
            os.kill(int(hook_pid), 0)
        except OSError:
            session_id = req_data.get('session_id', 'unknown')
            logger.info(
                f"[decision] Hook process {hook_pid} not alive "
                f"(Session: {session_id}), cannot deliver decision"
            )
            return False, None, '无法传递决策：权限请求已超时或被取消，请返回终端查看当前状态'

    # 构建决策
    if action == 'answer':
        # AskUserQuestion 回答
        if not answers or not isinstance(answers, dict) or not questions or not isinstance(questions, list):
            return False, None, '缺少答案或问题数据'
        updated_input = {
            'answers': answers,
            'questions': questions
        }
        decision = Decision.allow_with_updated_input(updated_input)
        decision_type = 'allow'
    elif action in ('allow', 'always'):
        decision = Decision.allow()
        decision_type = 'allow'
    elif action == 'deny':
        decision = Decision.deny('用户拒绝')
        decision_type = 'deny'
    else:  # interrupt
        decision = Decision.deny('用户拒绝并中断', interrupt=True)
        decision_type = 'deny'

    # 记录日志
    session_id = req_data.get('session_id', 'unknown')
    logger.info(f"[decision] Request {request_id} (Session: {session_id}), action: {action}")

    # 对于 always 操作，先写入规则（在提交决策之前）
    # 这样如果写规则失败，用户可以重试
    if action == 'always':
        rule_success = write_always_allow_rule(
            extra_data.get('project_dir'),
            extra_data.get('tool_name'),
            extra_data.get('tool_input', {})
        )
        if not rule_success:
            logger.error(f"[decision] Failed to write always-allow rule for request {request_id}")
            return False, None, '写入规则失败，请检查项目目录权限后重试'

    # 执行决策
    resolve_success, error_code, error_msg = request_manager.resolve(request_id, decision)

    if not resolve_success:
        # 根据错误码判断错误类型
        if error_code == request_manager.ERR_ALREADY_RESOLVED:
            return False, None, '请求已被处理，请勿重复操作'
        else:
            return False, None, f'处理失败: {error_msg}'

    # 成功响应消息
    action_messages = {
        'allow': '已批准运行',
        'always': '已始终允许，后续相同操作将自动批准',
        'deny': '已拒绝运行',
        'interrupt': '已拒绝并中断',
        'answer': '已提交回答'
    }
    return True, decision_type, action_messages.get(action, '操作成功')
