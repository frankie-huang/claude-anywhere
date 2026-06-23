"""Callback 侧飞书出站发送辅助

从 Callback 侧主动向飞书发送消息/卡片、移除表情、创建群聊，
统一抽象「单机直发（FeishuAPIService）vs 分离部署（网关转发）」两种部署模式。
"""

import json
import logging
from typing import Tuple

from utils.http_client import post_json

logger = logging.getLogger(__name__)


def reply_feishu_text(chat_id: str, message_id: str, text: str) -> Tuple[bool, str]:
    """从 Callback 侧调用飞书网关，发送/回复文本消息（兼容单机和分离部署）

    优先使用 FeishuAPIService 直接发送（单机模式），
    不可用时通过网关 /gw/feishu/send 转发（分离部署模式）。
    有 message_id 时回复该消息，无则降级为向 chat_id 发送。

    Args:
        chat_id: 飞书群聊 ID
        message_id: 要回复的消息 ID（为空时降级为 send_text）
        text: 消息文本

    Returns:
        (success, message_id or error)
    """
    from config import IS_CALLBACK_BACKEND

    if not IS_CALLBACK_BACKEND:
        # 单机模式：直接通过 FeishuAPIService 发送
        try:
            from services.feishu_api import FeishuAPIService
            service = FeishuAPIService.get_instance()
            if service and service.enabled:
                if message_id:
                    return service.reply_text(text, message_id)
                else:
                    return service.send_text(
                        text,
                        receive_id=chat_id,
                        receive_id_type='chat_id'
                    )
        except Exception as e:
            logger.warning("[reply_feishu_text] FeishuAPIService unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关转发
    try:
        from config import FEISHU_GATEWAY_URL, FEISHU_OWNER_ID
        from stores.auth_token_store import AuthTokenStore

        if not FEISHU_GATEWAY_URL:
            return (False, 'no feishu service available')

        store = AuthTokenStore.get_instance()
        auth_token = store.get() if store else ''
        if not auth_token:
            return (False, 'no auth_token available')

        api_url = FEISHU_GATEWAY_URL.rstrip('/') + '/gw/feishu/send'
        data = {
            'msg_type': 'text',
            'content': text,
            'owner_id': FEISHU_OWNER_ID,
            'chat_id': chat_id
        }
        if message_id:
            data['reply_to_message_id'] = message_id
        resp = post_json(api_url, data, headers={'X-Auth-Token': auth_token})
        if resp.get('success'):
            return (True, resp.get('data', {}).get('message_id', ''))
        else:
            return (False, resp.get('error', 'unknown'))
    except Exception as e:
        return (False, str(e))


def reply_feishu_markdown(chat_id: str, message_id: str, markdown: str) -> Tuple[bool, str]:
    """从 Callback 侧发送 markdown 卡片消息（兼容单机和分离部署）

    单机模式下通过 FeishuAPIService 直接发送卡片，
    分离部署模式下通过网关 /gw/feishu/send 转发。
    有 message_id 时回复该消息，无则降级为向 chat_id 发送。

    Args:
        chat_id: 飞书群聊 ID
        message_id: 要回复的消息 ID（为空时降级为 send_card）
        markdown: markdown 文本内容

    Returns:
        (success, message_id or error)
    """
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "direction": "vertical",
            "elements": [{"tag": "markdown", "content": markdown}]
        }
    }

    from config import IS_CALLBACK_BACKEND

    if not IS_CALLBACK_BACKEND:
        # 单机模式：直接通过 FeishuAPIService 发送
        try:
            from services.feishu_api import FeishuAPIService
            service = FeishuAPIService.get_instance()
            if service and service.enabled:
                card_json = json.dumps(card, ensure_ascii=False)
                if message_id:
                    return service.reply_card(card_json, message_id)
                else:
                    return service.send_card(
                        card_json,
                        receive_id=chat_id,
                        receive_id_type='chat_id'
                    )
        except Exception as e:
            logger.warning("[reply_feishu_markdown] FeishuAPIService unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关转发
    try:
        from config import FEISHU_GATEWAY_URL, FEISHU_OWNER_ID
        from stores.auth_token_store import AuthTokenStore

        if not FEISHU_GATEWAY_URL:
            return (False, 'no feishu service available')

        store = AuthTokenStore.get_instance()
        auth_token = store.get() if store else ''
        if not auth_token:
            return (False, 'no auth_token available')

        api_url = FEISHU_GATEWAY_URL.rstrip('/') + '/gw/feishu/send'
        data = {
            'msg_type': 'interactive',
            'content': card,
            'owner_id': FEISHU_OWNER_ID,
            'chat_id': chat_id
        }
        if message_id:
            data['reply_to_message_id'] = message_id
        resp = post_json(api_url, data, headers={'X-Auth-Token': auth_token})
        if resp.get('success'):
            return (True, resp.get('data', {}).get('message_id', ''))
        else:
            return (False, resp.get('error', 'unknown'))
    except Exception as e:
        return (False, str(e))


def remove_feishu_typing(message_id: str) -> None:
    """从 Callback 侧移除消息上的 Typing 表情（兼容单机和分离部署）

    单机模式下通过 FeishuAPIService 直接移除，
    分离部署模式下通过网关 /gw/feishu/remove-reaction 转发。

    Args:
        message_id: 要移除 Typing 的消息 ID
    """
    if not message_id:
        return
    from config import IS_CALLBACK_BACKEND

    if not IS_CALLBACK_BACKEND:
        # 单机模式：直接通过 FeishuAPIService 移除
        try:
            from services.feishu_api import FeishuAPIService
            service = FeishuAPIService.get_instance()
            if service and service.enabled:
                service.remove_reaction(message_id, 'Typing')
                return
        except Exception as e:
            logger.warning("[remove_feishu_typing] FeishuAPIService unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关转发
    try:
        from config import FEISHU_GATEWAY_URL, FEISHU_OWNER_ID
        from stores.auth_token_store import AuthTokenStore

        if not FEISHU_GATEWAY_URL:
            return

        store = AuthTokenStore.get_instance()
        auth_token = store.get() if store else ''
        if not auth_token:
            return

        api_url = FEISHU_GATEWAY_URL.rstrip('/') + '/gw/feishu/remove-reaction'
        post_json(api_url, {
            'owner_id': FEISHU_OWNER_ID,
            'message_id': message_id,
            'emoji_type': 'Typing',
        }, headers={'X-Auth-Token': auth_token})
    except Exception as e:
        logger.warning("[remove_feishu_typing] gateway fallback failed: %s", e)


def create_feishu_group(session_id: str, project_dir: str) -> Tuple[bool, str]:
    """从 Callback 侧调用飞书网关，创建飞书群聊（兼容单机和分离部署）

    群名统一由 gateway 根据 prefix + project_dir + 时间戳构造，调用方无需关心。

    Args:
        session_id: 关联的 session ID
        project_dir: 项目目录

    Returns:
        (success, chat_id_or_error)
    """
    from config import IS_CALLBACK_BACKEND, FEISHU_OWNER_ID, FEISHU_GROUP_NAME_PREFIX

    if not IS_CALLBACK_BACKEND:
        # 单机模式：直接调用网关侧统一入口（创建 + 写归属记录一次完成）
        try:
            from handlers.feishu import create_group_chat_and_record
            return create_group_chat_and_record(FEISHU_OWNER_ID, session_id, project_dir, FEISHU_GROUP_NAME_PREFIX)
        except Exception as e:
            logger.warning("[create_feishu_group] create_group_chat_and_record unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关转发
    try:
        from config import FEISHU_GATEWAY_URL
        from stores.auth_token_store import AuthTokenStore

        if not FEISHU_GATEWAY_URL:
            return (False, 'no feishu service available')

        store = AuthTokenStore.get_instance()
        auth_token = store.get() if store else ''
        if not auth_token:
            return (False, 'no auth_token available')

        api_url = FEISHU_GATEWAY_URL.rstrip('/') + '/gw/feishu/create-group'
        data = {
            'owner_id': FEISHU_OWNER_ID,
            'session_id': session_id,
            'project_dir': project_dir,
        }
        resp = post_json(api_url, data, headers={'X-Auth-Token': auth_token})
        if resp.get('success'):
            return (True, resp.get('chat_id', ''))
        else:
            return (False, resp.get('error', 'unknown'))
    except Exception as e:
        return (False, str(e))
