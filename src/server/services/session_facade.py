"""SessionFacade — gateway 端 session 能力的统一门面

归属端：飞书网关
使用方：feishu.py

职责：
    对 feishu.py 暴露一组语义化的 session 能力 API，内部隐藏几个子系统：
      - 远端 callback（/cb/session/mute 等）——通过注入的 forward_fn 访问
      - 本地 MessageSessionStore —— parent_id 反查

    设计上预期后续把 feishu.py 里其它 "session 相关" 的能力（group 反查、
    ensure-chat 等）陆续搬到这里。当前已纳入：
      - resolve_from_message：根据飞书消息上下文解析归属 session
      - attach / clone / set_last_message_id / invalidate_chats：session 生命周期 RPC
      - mute / unmute：透传 callback 端的 session 级静音指令
      - mute_dir / unmute_dir：透传 callback 端的目录级静音指令

mute 状态说明：
    权威源与拦截点均在 callback 端（session_chat_store + hook 脚本的 _get_chat_id）。
    网关仅在用户执行 /mute、/unmute 命令时透传到 callback，不缓存、不拦截。
    自动解除静音由 callback 端 handle_continue_session 处理。
    目录级 mute 存储在 DirectoryStore，终端发起的新会话自动继承目录 mute 状态。

初始化：
    应用启动时（在 feishu.py 模块加载末尾）调用一次：
        SessionFacade.configure(forward_fn=_forward_via_ws_or_http)
"""

import logging
from typing import Any, Callable, Dict, List, Optional



logger = logging.getLogger(__name__)


class SessionFacade:
    """feishu.py 访问 session 能力的门面（类级单例 + 进程内缓存）"""

    class RouteSource:
        """resolve_from_message 的 source 字段枚举值及常用判定"""
        PARENT = 'parent'                      # parent_id 命中 MessageSessionStore
        GROUP_CHAT = 'group_chat'              # group 模式群聊通过 chat_id 反查命中
        PARENT_NOT_FOUND = 'parent_not_found'  # parent_id 存在但映射查不到（明确失败）
        UNRESOLVED = 'unresolved'              # 其他无法定位 session 的情况

        @classmethod
        def is_resolved(cls, source: str) -> bool:
            """是否成功解析到 session（PARENT 或 GROUP_CHAT）"""
            return source in (cls.PARENT, cls.GROUP_CHAT)

        @classmethod
        def is_parent_not_found(cls, source: str) -> bool:
            """是否属于"parent_id 有效但映射查不到"——用于用户体验层反馈"会话找不到"。"""
            return source == cls.PARENT_NOT_FOUND

        @classmethod
        def is_unresolved(cls, source: str) -> bool:
            """是否无法从消息上下文定位到任何 session（非回复、非 group 群聊等）"""
            return source == cls.UNRESOLVED


    # ---- 注入的下游依赖（feishu.py 启动时 configure 一次）----
    _forward_fn: Optional[Callable[..., Optional[Dict[str, Any]]]] = None

    # =========================================================================
    # 初始化
    # =========================================================================

    @classmethod
    def configure(
        cls,
        forward_fn: Callable[..., Optional[Dict[str, Any]]],
    ) -> None:
        """注入 gateway → callback 的转发函数

        Args:
            forward_fn: (binding, endpoint, payload) -> resp dict
                实际传入 feishu._forward_via_ws_or_http
        """
        cls._forward_fn = forward_fn
        logger.debug("[session-facade] configured")

    # =========================================================================
    # Session 路由
    # =========================================================================

    @classmethod
    def resolve_group_chat(cls, binding: Dict[str, Any], chat_id: str) -> Dict[str, str]:
        """通过 chat_id 反查群聊绑定的 session（纯本地，零 RPC）

        gateway 端 GroupSessionStore 是 (owner_id, chat_id) → session 路由的
        唯一权威源（和 MessageSessionStore 同构），找不到即未绑定。

        owner_id 从 binding['_owner_id'] 取——同一个 chat_id 在不同 owner 下
        可能各自绑定不同 session（用户共享群 + /attach 场景），必须按 owner
        隔离查询。

        Returns:
            {'session_id': str, 'project_dir': str, 'new_session': bool}
            找不到返回空 dict。command 等 session 语义字段不在路由表里，
            需要时调用方走 fetch_session_info 单独回源 callback。
        """
        if not chat_id:
            return {}
        owner_id = binding.get('_owner_id', '') if binding else ''
        if not owner_id:
            return {}
        from services.group_session_store import GroupSessionStore
        local = GroupSessionStore.get_instance()
        if not local:
            return {}
        item = local.get(owner_id, chat_id)
        if not item or not item.get('session_id'):
            return {}
        return {
            'session_id': item['session_id'],
            'project_dir': item.get('project_dir', ''),
            'new_session': bool(item.get('new_session')),
        }

    @classmethod
    def fetch_session_info(cls, binding: Dict[str, Any],
                           session_id: str) -> Dict[str, Any]:
        """按 session_id 从 callback 权威源拿 session 字段（含 command）

        本地路由 store（GroupSessionStore / MessageSessionStore）只存路由必需的
        session_id + project_dir，不存 command 等 session 语义属性。
        需要权威字段的低频场景（/new 继承等）调用此方法，走一次 callback RPC。

        Returns:
            {'project_dir': str, 'command': str, 'agent_type': str, 'chat_id': str, 'dissolved': bool}
            失败或 session 不存在返回空 dict（所有字段为空的等价）
        """
        if not session_id or cls._forward_fn is None:
            return {}
        try:
            resp = cls._forward_fn(binding, '/cb/session/get-info',
                                   {'session_id': session_id})
        except Exception as e:
            logger.warning("[session-facade] fetch_session_info error: %s", e)
            return {}
        if not resp:
            return {}
        return {
            'project_dir': resp.get('project_dir', ''),
            'command': resp.get('command', ''),
            'agent_type': resp.get('agent_type', ''),
            'chat_id': resp.get('chat_id', ''),
            'dissolved': resp.get('dissolved', False),
        }

    @classmethod
    def resolve_from_message(cls, data: dict, binding: Dict[str, Any]) -> Dict[str, str]:
        """按飞书消息上下文解析该消息归属的 session

        优先级：
        1. 有 parent_id：通过 MessageSessionStore 反查 parent 消息所属 session
           查不到视为**明确失败**（source=PARENT_NOT_FOUND），不 fallback 到 chat_id，
           避免"引用旧会话的消息却操作到 active session"的错误。
        2. 无 parent_id + group 模式群聊：通过 chat_id 反查当前活跃 session
        3. 其他场景：无法确定（source=UNRESOLVED）

        调用方按 source 决策（参见 RouteSource）：
        - 用户主动命令（/mute 等）：PARENT_NOT_FOUND 给"未找到会话"反馈；
          UNRESOLVED 给"无法确定目标"
        - 被动钩子（auto_unmute 等）：PARENT_NOT_FOUND / UNRESOLVED 均静默跳过

        Returns:
            {
                'source':      SessionFacade.RouteSource.*  (str 字面量),
                'session_id':  str,  # source in {PARENT, GROUP_CHAT} 时非空,
                'project_dir': str,
            }
        """
        from services.message_session_store import MessageSessionStore

        event = data.get('event', {})
        message = event.get('message', {})
        chat_id = message.get('chat_id', '')
        parent_id = message.get('parent_id', '')
        chat_type = message.get('chat_type', '')
        session_mode = binding.get('session_mode', '')

        empty = {'session_id': '', 'project_dir': ''}

        if parent_id:
            msg_store = MessageSessionStore.get_instance()
            if msg_store is None:
                # store 未就绪：不能 fallback 到 UNRESOLVED，否则带 parent_id 的 reply
                # 会落入默认目录分支被当作"新消息"开新 session，违背用户意图。
                # 统一归入 PARENT_NOT_FOUND 走 reject 分支，并打告警留痕。
                logger.warning("[session-facade] MessageSessionStore not initialized; "
                               "cannot resolve parent_id=%s", parent_id)
                return {'source': cls.RouteSource.PARENT_NOT_FOUND, **empty}
            mapping = msg_store.get(parent_id)
            if mapping and mapping.get('session_id'):
                return {
                    'source': cls.RouteSource.PARENT,
                    'session_id': mapping['session_id'],
                    'project_dir': mapping.get('project_dir', ''),
                }
            return {'source': cls.RouteSource.PARENT_NOT_FOUND, **empty}

        if session_mode == 'group' and chat_type == 'group' and chat_id:
            resp = cls.resolve_group_chat(binding, chat_id)
            session_id = resp.get('session_id', '')
            if session_id:
                return {
                    'source': cls.RouteSource.GROUP_CHAT,
                    'session_id': session_id,
                    'project_dir': resp.get('project_dir', ''),
                    'new_session': resp.get('new_session', False),
                }

        return {'source': cls.RouteSource.UNRESOLVED, **empty}

    # =========================================================================
    # Session 生命周期（透传 callback RPC）
    # =========================================================================

    @classmethod
    def attach(cls, binding: Dict[str, Any],
               session_prefix: str, chat_id: str) -> Optional[Dict[str, Any]]:
        """请求 callback 按前缀匹配并绑定 session

        Args:
            binding: 绑定信息字典
            session_prefix: session_id 前缀（至少 8 字符）
            chat_id: 目标群聊 ID

        Returns:
            callback 响应 dict（含 matched_ids, attached, session_id 等），
            失败返回 None
        """
        if cls._forward_fn is None:
            return None
        try:
            return cls._forward_fn(binding, '/cb/session/attach', {
                'session_prefix': session_prefix,
                'chat_id': chat_id,
            })
        except Exception as e:
            logger.error("[session-facade] attach error: %s", e)
            return None

    @classmethod
    def clone(cls, binding: Dict[str, Any], old_session_id: str,
              new_session_id: str, chat_id: str) -> Optional[Dict[str, Any]]:
        """请求 callback 克隆 session（用于 /clear 命令）

        Args:
            binding: 绑定信息字典
            old_session_id: 被克隆的原 session ID
            new_session_id: 新 session ID
            chat_id: 群聊 ID

        Returns:
            callback 响应 dict（含 ok, project_dir），失败返回 None
        """
        if cls._forward_fn is None:
            return None
        try:
            return cls._forward_fn(binding, '/cb/session/clone', {
                'old_session_id': old_session_id,
                'new_session_id': new_session_id,
                'chat_id': chat_id,
            })
        except Exception as e:
            logger.error("[session-facade] clone error: %s", e)
            return None

    @classmethod
    def set_last_message_id(cls, binding: Dict[str, Any],
                            session_id: str, message_id: str) -> bool:
        """通过 callback 设置 session 的 last_message_id

        Args:
            binding: 绑定信息字典
            session_id: 会话 ID
            message_id: 飞书消息 ID

        Returns:
            是否设置成功
        """
        if not session_id or not message_id or cls._forward_fn is None:
            return False
        try:
            resp = cls._forward_fn(binding, '/cb/session/set-last-message-id', {
                'session_id': session_id,
                'message_id': message_id,
            })
            if resp is None:
                return False
            success = resp.get('success', False)
            if success:
                logger.info("[session-facade] set_last_message_id: session=%s, message_id=%s",
                            session_id, message_id)
            else:
                logger.warning("[session-facade] set_last_message_id failed: %s",
                               resp.get('error', 'unknown'))
            return success
        except Exception as e:
            logger.error("[session-facade] set_last_message_id error: %s", e)
            return False

    @classmethod
    def invalidate_chats(cls, binding: Dict[str, Any],
                         chat_ids: List[str]) -> Optional[Dict[str, Any]]:
        """通知 callback 标记指定群聊的 session 为 dissolved

        在实际解散飞书群聊之前调用，确保 callback 端先标记状态。

        Args:
            binding: 绑定信息字典
            chat_ids: 待标记的群聊 ID 列表

        Returns:
            callback 响应 dict（含 ok），失败返回 None
        """
        if cls._forward_fn is None:
            return None
        try:
            resp = cls._forward_fn(binding, '/cb/session/invalidate-chats', {
                'chat_ids': chat_ids,
            })
            if not resp or not resp.get('ok'):
                err_msg = (resp or {}).get('error', 'unknown error')
                logger.warning("[session-facade] invalidate_chats failed: %s", err_msg)
                return None
            return resp
        except Exception as e:
            logger.error("[session-facade] invalidate_chats error: %s", e)
            return None

    # =========================================================================
    # Mute 状态（透传 callback，网关不缓存）
    # =========================================================================

    @classmethod
    def mute(cls, binding: Dict[str, Any], session_id: str) -> Optional[bool]:
        """将 session 标记为静音

        Returns:
            True  = 本次调用将 session 从未静音切到静音
            False = 幂等：操作前已处于静音
            None  = callback 调用失败
        """
        if not session_id:
            return None
        resp = cls._call_session_mute_api(binding, 'mute', session_id)
        if resp is None or 'changed' not in resp:
            return None
        return bool(resp['changed'])

    @classmethod
    def unmute(cls, binding: Dict[str, Any], session_id: str) -> Optional[bool]:
        """清除 session 静音标志

        Returns:
            True  = 本次调用将 session 从静音切到未静音
            False = 幂等：操作前就未静音
            None  = callback 调用失败
        """
        if not session_id:
            return None
        resp = cls._call_session_mute_api(binding, 'unmute', session_id)
        if resp is None or 'changed' not in resp:
            return None
        return bool(resp['changed'])

    @classmethod
    def mute_dir(cls, binding: Dict[str, Any], project_dir: str,
                 recursive: bool = False) -> Optional[Dict[str, Any]]:
        """将目录标记为静音

        Args:
            binding: 用户绑定信息
            project_dir: 目标目录
            recursive: True 表示递归静音（自身+子孙）

        Returns:
            {'changed': bool, 'message': str} 或 None（callback 调用失败）
        """
        if not project_dir:
            return None
        resp = cls._call_dir_mute_api(binding, 'mute', project_dir, recursive=recursive)
        if resp is None or 'changed' not in resp:
            return None
        return {'changed': bool(resp['changed']), 'message': resp.get('message', '')}

    @classmethod
    def unmute_dir(cls, binding: Dict[str, Any], project_dir: str,
                   recursive: bool = False) -> Optional[Dict[str, Any]]:
        """取消目录静音 / 加白目录

        Args:
            binding: 用户绑定信息
            project_dir: 目标目录
            recursive: True 表示递归加白（自身+子孙）

        Returns:
            {'changed': bool, 'message': str} 或 None（callback 调用失败）
        """
        if not project_dir:
            return None
        resp = cls._call_dir_mute_api(binding, 'unmute', project_dir, recursive=recursive)
        if resp is None or 'changed' not in resp:
            return None
        return {'changed': bool(resp['changed']), 'message': resp.get('message', '')}

    @classmethod
    def list_muted(cls, binding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """列出所有已静音的 session 和目录

        Returns:
            {'sessions': [{session_id, project_dir, chat_id, muted_at}, ...],
             'dirs': [{project_dir, muted_at}, ...]}
            失败返回 None
        """
        resp_s = cls._call_session_mute_api(binding, 'list')
        resp_d = cls._call_dir_mute_api(binding, 'list')
        if resp_s is None and resp_d is None:
            return None
        return {
            'sessions': resp_s.get('sessions', []) if resp_s else [],
            'dirs': resp_d.get('dirs', []) if resp_d else [],
        }

    # =========================================================================
    # 内部：callback mute API 调用
    # =========================================================================

    @classmethod
    def _call_session_mute_api(cls, binding: Dict[str, Any], action: str,
                               session_id: str = '') -> Optional[Dict[str, Any]]:
        """调 /cb/session/mute；action ∈ {mute, unmute, query, list}。失败返回 None。"""
        if cls._forward_fn is None:
            logger.error("[session-facade] forward_fn not configured")
            return None
        try:
            payload = {'action': action}
            if session_id:
                payload['session_id'] = session_id
            resp = cls._forward_fn(binding, '/cb/session/mute', payload)
            if resp and resp.get('ok'):
                return resp
            logger.warning("[session-facade] /cb/session/mute (%s) failed: %s", action, resp)
            return None
        except Exception as e:
            logger.error("[session-facade] /cb/session/mute error: %s", e)
            return None

    @classmethod
    def _call_dir_mute_api(cls, binding: Dict[str, Any], action: str,
                           project_dir: str = '',
                           recursive: bool = False) -> Optional[Dict[str, Any]]:
        """调 /cb/directory/mute；action ∈ {mute, unmute, query, list}。失败返回 None。"""
        if cls._forward_fn is None:
            logger.error("[session-facade] forward_fn not configured")
            return None
        try:
            payload = {'action': action}
            if project_dir:
                payload['project_dir'] = project_dir
            if recursive:
                payload['recursive'] = True
            resp = cls._forward_fn(binding, '/cb/directory/mute', payload)
            if resp and resp.get('ok'):
                return resp
            logger.warning("[session-facade] /cb/directory/mute (%s) failed: %s", action, resp)
            return None
        except Exception as e:
            logger.error("[session-facade] /cb/directory/mute error: %s", e)
            return None
