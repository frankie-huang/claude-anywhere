"""
card_session.py - 新建会话相关的卡片构建

包含：
    - _build_new_session_card: 新会话表单卡片构建
    - _build_browse_result_card: 浏览目录结果卡片构建
"""

import json
import logging
from typing import Any, Dict, List, Optional

from .utils import (
    _get_binding_from_event,
    _build_agent_commands_from_binding,
    _merge_agent_commands,
)
from .forward import _fetch_recent_dirs_from_callback

logger = logging.getLogger(__name__)


def _build_new_session_card(
    owner_id: str,
    chat_id: str,
    message_id: str,
    chat_type: str = '',
    recent_dirs: List[str] = None,
    selected_recent_dir: str = '',
    custom_dir: str = '',
    browse_data: Optional[Dict[str, Any]] = None,
    prompt: str = '',
    agent_commands: Optional[Dict[str, List[str]]] = None,
    agent_command: str = '',
    default_agent: str = ''
) -> Dict[str, Any]:
    """构建新建会话卡片（统一构建逻辑）

    Args:
        owner_id: 用户 ID
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        chat_type: 聊天类型（group/p2p），卡片提交时透传
        recent_dirs: 常用目录列表
        selected_recent_dir: 常用目录下拉的选中值（回填用）
        custom_dir: 自定义路径输入框默认值
        browse_data: 浏览结果数据 {dirs, parent, current}，为 None 则不显示浏览结果区域
        prompt: 提示词输入框默认值
        agent_commands: agent 命令映射，如 {'claude': ['claude'], 'codex': ['codex']}
        agent_command: 预选的 agent 命令（agent_type::command 格式）
        default_agent: 默认 agent 类型（如 'claude'）

    Returns:
        飞书卡片字典
    """
    # 构建常用目录下拉选项（显示：folder_name (/full/path)，value 保持完整路径）
    dir_options = []
    for dir_path in (recent_dirs or []):
        # 提取文件夹名称（路径末尾）
        folder_name = dir_path.rstrip('/').split('/')[-1] if dir_path else ''
        # 格式：folder_name (/full/path/to/folder)
        display_text = f"{folder_name} ({dir_path})" if folder_name else dir_path
        dir_options.append({
            'text': {
                'tag': 'plain_text',
                'content': display_text
            },
            'value': dir_path
        })

    # 回调 value（按钮共用）
    callback_value = {
        'owner_id': owner_id,
        'chat_id': chat_id,
        'message_id': message_id,
        'chat_type': chat_type
    }

    # 构建 Form 表单元素
    form_elements = []

    # 区域标题：选择工作目录
    form_elements.append({
        'tag': 'div',
        'text': {
            'tag': 'plain_text',
            'content': '1️⃣ 选择工作目录'
        }
    })

    # 常用目录下拉菜单（如果有），标签和下拉框同行
    if recent_dirs:
        # 决定 initial_option
        if selected_recent_dir and selected_recent_dir in [d['value'] for d in dir_options]:
            initial_option = selected_recent_dir
        else:
            initial_option = dir_options[0]['value'] if dir_options else ''

        form_elements.append({
            'tag': 'column_set',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'vertical_align': 'center',
                    'elements': [
                        {
                            'tag': 'div',
                            'text': {
                                'tag': 'plain_text',
                                'content': '常用目录'
                            }
                        }
                    ]
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 4,
                    'elements': [
                        {
                            'tag': 'select_static',
                            'name': 'recent_dir',
                            'placeholder': {
                                'tag': 'plain_text',
                                'content': '选择工作目录'
                            },
                            'width': 'fill',
                            'options': dir_options,
                            'initial_option': initial_option
                        }
                    ]
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'elements': [
                        {
                            'tag': 'button',
                            'name': 'browse_dir_select_btn',
                            'text': {
                                'tag': 'plain_text',
                                'content': '浏览'
                            },
                            'type': 'default',
                            'width': 'fill',
                            'form_action_type': 'submit',
                            'behaviors': [
                                {
                                    'type': 'callback',
                                    'value': callback_value
                                }
                            ]
                        }
                    ]
                }
            ]
        })

    # 自定义路径标签 + 输入框 + 浏览按钮（同行布局）
    form_elements.append({
        'tag': 'column_set',
        'columns': [
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'vertical_align': 'center',
                'elements': [
                    {
                        'tag': 'div',
                        'text': {
                            'tag': 'plain_text',
                            'content': '自定义路径'
                        }
                    }
                ]
            },
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 4,
                'elements': [
                    {
                        'tag': 'input',
                        'name': 'custom_dir',
                        'placeholder': {
                            'tag': 'plain_text',
                            'content': '输入完整路径，如 /home/user/project'
                        },
                        'width': 'fill',
                        'default_value': custom_dir
                    }
                ]
            },
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'elements': [
                    {
                        'tag': 'button',
                        'name': 'browse_custom_btn',
                        'text': {
                            'tag': 'plain_text',
                            'content': '浏览'
                        },
                        'type': 'default',
                        'width': 'fill',
                        'form_action_type': 'submit',
                        'behaviors': [
                            {
                                'type': 'callback',
                                'value': callback_value
                            }
                        ]
                    }
                ]
            }
        ]
    })

    # 浏览结果区域（仅当 browse_data 非空时显示）
    if browse_data is not None:
        current_path = browse_data.get('current', '')
        browse_dirs = browse_data.get('dirs', [])
        browse_options = []
        for dir_path in browse_dirs:
            display_name = dir_path.rstrip('/').split('/')[-1] if dir_path else ''
            browse_options.append({
                'text': {
                    'tag': 'plain_text',
                    'content': display_name
                },
                'value': dir_path
            })

        if browse_options:
            form_elements.append({
                'tag': 'column_set',
                'columns': [
                    {
                        'tag': 'column',
                        'width': 'weighted',
                        'weight': 1,
                        'vertical_align': 'center',
                        'elements': [
                            {
                                'tag': 'div',
                                'text': {
                                    'tag': 'plain_text',
                                    'content': '选择子目录'
                                }
                            }
                        ]
                    },
                    {
                        'tag': 'column',
                        'width': 'weighted',
                        'weight': 4,
                        'elements': [
                            {
                                'tag': 'select_static',
                                'name': 'browse_result',
                                'placeholder': {
                                    'tag': 'plain_text',
                                    'content': f'选择 {current_path} 的子目录'
                                },
                                'width': 'fill',
                                'options': browse_options
                            }
                        ]
                    },
                    {
                        'tag': 'column',
                        'width': 'weighted',
                        'weight': 1,
                        'elements': [
                            {
                                'tag': 'button',
                                'name': 'browse_result_btn',
                                'text': {
                                    'tag': 'plain_text',
                                    'content': '浏览'
                                },
                                'type': 'default',
                                'width': 'fill',
                                'form_action_type': 'submit',
                                'behaviors': [
                                    {
                                        'type': 'callback',
                                        'value': callback_value
                                    }
                                ]
                            }
                        ]
                    }
                ]
            })
        else:
            form_elements.append({
                'tag': 'div',
                'text': {
                    'tag': 'plain_text',
                    'content': f'📁 {current_path} 下没有子目录'
                }
            })

        # 优先级提示文本（有浏览结果时）
        form_elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '💡 优先级：选择子目录 > 自定义路径 > 常用目录'
            }
        })
    else:
        # 优先级提示文本（初始卡片没有浏览子目录选项）
        form_elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '💡 优先级：自定义路径 > 常用目录'
            }
        })

    # Agent 命令选择（有命令列表就显示，与原 claude_commands 行为一致）
    merged_cmds = _merge_agent_commands(agent_commands or {}, default_agent)

    if merged_cmds:
        prompt_step = '3️⃣'

        # 分割线：目录选择区域结束
        form_elements.append({'tag': 'hr'})

        form_elements.append({
            'tag': 'div',
            'text': {
                'tag': 'plain_text',
                'content': '2️⃣ 选择 Agent 命令'
            }
        })

        cmd_options = []
        for at, cmd in merged_cmds:
            option_value = f'{at}::{cmd}'
            cmd_options.append({
                'text': {
                    'tag': 'plain_text',
                    'content': f'[{at.capitalize()}] {cmd}'
                },
                'value': option_value
            })

        # 确定默认选中项
        if agent_command:
            # 预选值优先（可能是 agent_type::cmd 格式）
            default_option = agent_command if '::' in agent_command else ''
            if not default_option:
                # 尝试从合并列表中匹配
                for at, cmd in merged_cmds:
                    if agent_command == cmd or agent_command in cmd:
                        default_option = f'{at}::{cmd}'
                        break
        else:
            default_option = ''

        if not default_option and merged_cmds:
            at, cmd = merged_cmds[0]
            default_option = f'{at}::{cmd}'

        cmd_select = {
            'tag': 'select_static',
            'name': 'agent_command',
            'placeholder': {
                'tag': 'plain_text',
                'content': '选择命令'
            },
            'options': cmd_options,
            'width': 'fill'
        }
        if default_option:
            cmd_select['initial_option'] = default_option

        form_elements.append({
            'tag': 'column_set',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'vertical_align': 'center',
                    'elements': [
                        {
                            'tag': 'div',
                            'text': {
                                'tag': 'plain_text',
                                'content': '命令'
                            }
                        }
                    ]
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 5,
                    'elements': [cmd_select]
                }
            ]
        })
    else:
        prompt_step = '2️⃣'

    # 分割线：cmd / 目录选择区域结束
    form_elements.append({'tag': 'hr'})

    # Prompt 输入框
    form_elements.append({
        'tag': 'div',
        'text': {
            'tag': 'plain_text',
            'content': prompt_step + ' 输入提示词'
        }
    })

    form_elements.append({
        'tag': 'column_set',
        'columns': [
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 1,
                'vertical_align': 'center',
                'elements': [
                    {
                        'tag': 'div',
                        'text': {
                            'tag': 'plain_text',
                            'content': '提示词'
                        }
                    }
                ]
            },
            {
                'tag': 'column',
                'width': 'weighted',
                'weight': 5,
                'elements': [
                    {
                        'tag': 'input',
                        'name': 'prompt',
                        'input_type': 'multiline_text',
                        'placeholder': {
                            'tag': 'plain_text',
                            'content': '请输入您的问题或任务描述'
                        },
                        'width': 'fill',
                        'default_value': prompt or '',
                        # 不设置 required，避免点击"浏览"按钮时被阻止
                        # 服务端会在创建会话时验证 prompt 是否为空
                    }
                ]
            }
        ]
    })

    # 构建卡片
    card = {
        'schema': '2.0',
        'config': {
            'wide_screen_mode': True
        },
        'header': {
            'title': {
                'tag': 'plain_text',
                'content': '🧠 完善信息以创建会话'
            },
            'template': 'blue'
        },
        'body': {
            'direction': 'vertical',
            'elements': [
                {
                    'tag': 'form',
                    'name': 'dir_prompt_form',
                    'elements': form_elements + [
                        {
                            'tag': 'button',
                            'name': 'submit_btn',
                            'text': {
                                'tag': 'plain_text',
                                'content': '创建会话'
                            },
                            'type': 'primary',
                            'form_action_type': 'submit',
                            'behaviors': [
                                {
                                    'type': 'callback',
                                    'value': callback_value
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

    return card


def _build_browse_result_card(browse_data: dict, form_values: dict, custom_dir_value: str,
                              chat_id: str, message_id: str, chat_type: str,
                              feishu_event: dict) -> dict:
    """构建包含浏览结果的目录选择卡片

    Args:
        browse_data: browse-dirs 接口返回的数据 {dirs, parent, current}
        form_values: 原始表单数据（用于回填）
        custom_dir_value: 应该回填到 custom_dir 输入框的值
        chat_id: 群聊 ID
        message_id: 原始消息 ID
        chat_type: 聊天类型（group/p2p），透传到卡片
        feishu_event: 飞书事件数据

    Returns:
        飞书卡片字典
    """
    # 获取绑定信息
    binding = _get_binding_from_event(feishu_event)
    owner_id = binding.get('_owner_id', '') if binding else ''

    # 获取常用目录列表
    recent_dirs = _fetch_recent_dirs_from_callback(binding, limit=20) if binding and binding.get('auth_token') else []

    card = _build_new_session_card(
        owner_id=owner_id, chat_id=chat_id, message_id=message_id,
        chat_type=chat_type,
        recent_dirs=recent_dirs,
        selected_recent_dir=form_values.get('recent_dir', ''),
        custom_dir=custom_dir_value,
        browse_data=browse_data,
        prompt=form_values.get('prompt', ''),
        agent_commands=_build_agent_commands_from_binding(binding),
        agent_command=form_values.get('agent_command', '') or form_values.get('claude_command', ''),
        default_agent=binding.get('default_agent', 'claude') if binding else ''
    )

    browse_dirs = browse_data.get('dirs', [])
    logger.info(f"[feishu] Built browse result card with {len(browse_dirs)} dirs")

    # 打印完整卡片 JSON 用于调试
    card_json = json.dumps(card, ensure_ascii=True, indent=2)
    logger.info(f"[feishu] Browse result card JSON:\n{card_json}")

    return card
