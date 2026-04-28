"""SessionFacade — gateway 端 session 能力的统一门面

归属端：飞书网关
使用方：feishu.py

职责：
    对 feishu.py 暴露一组语义化的 session 能力 API，内部隐藏几个子系统：
      - 远端 callback（/cb/session/mute 等）——通过注入的 forward_fn 访问
      - 本地 MessageSessionStore —— parent_id 反查
      - 本地进程内缓存 —— mute 状态加速

    设计上预期后续把 feishu.py 里其它 "session 相关" 的能力（group 反查、
    ensure-chat、attach 等）陆续搬到这里。当前已纳入：
      - resolve_from_message：根据飞书消息上下文解析归属 session
      - is_muted / mute / unmute：出站静音状态（缓存 + 写穿透）

mute 状态的一致性模型：
    权威源：callback 端 session_chat_store（持久化到 JSON 文件）
    gateway 缓存：SessionFacade._muted_cache（进程内 dict）
    策略：
      - 写穿透（mute/unmute）——先调 callback，成功后再更新缓存
      - 懒读回源（is_muted）——缓存命中直接返回；miss 去 callback 查一次并回填
      - 故障降级——is_muted 在 callback 调用失败时返回 False（未静音），不污染缓存
    稳态下出站拦截零 RPC；重启后首次查询付一次 RPC。

初始化：
    应用启动时（在 feishu.py 模块加载末尾）调用一次：
        SessionFacade.configure(forward_fn=_forward_via_ws_or_http)
"""

import logging
from typing import Any, Callable, Dict, Optional

from utils.ttl_cache import TTLCache

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

    # ---- mute 内存缓存：session_id -> muted? ----
    # 严格 TTL：读时过期视为 miss；超 size 上限按 FIFO 淘汰。
    _muted_cache: TTLCache = TTLCache(
        ttl=86400.0, max_size=4096,
        strict_read=True, name='session-facade.muted',
    )

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
            {'session_id': str, 'project_dir': str}
            找不到返回空 dict。claude_command 等 session 语义字段不在路由表里，
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
        }

    @classmethod
    def fetch_session_info(cls, binding: Dict[str, Any],
                           session_id: str) -> Dict[str, Any]:
        """按 session_id 从 callback 权威源拿 session 字段（含 claude_command）

        本地路由 store（GroupSessionStore / MessageSessionStore）只存路由必需的
        session_id + project_dir，不存 claude_command 等 session 语义属性。
        需要权威字段的低频场景（/new 继承等）调用此方法，走一次 callback RPC。

        Returns:
            {'project_dir': str, 'claude_command': str, 'chat_id': str, 'dissolved': bool}
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
            'claude_command': resp.get('claude_command', ''),
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
                }

        return {'source': cls.RouteSource.UNRESOLVED, **empty}

    # =========================================================================
    # Mute 状态（缓存 + 写穿透 + 懒读回源）
    # =========================================================================

    @classmethod
    def is_muted(cls, binding: Dict[str, Any], session_id: str) -> bool:
        """查询 session 是否处于静音状态

        命中缓存 → 直接返回；miss → 回源 callback 并回填。
        callback 调用失败或响应字段缺失时降级返回 False（不写入缓存，下次仍会重试）。
        """
        if not session_id:
            return False
        cached = cls._muted_cache.get(session_id)
        if cached is not None:
            return cached

        resp = cls._call_mute_api(binding, session_id, 'query')
        if resp is None or 'muted' not in resp:
            return False  # 故障降级：调用失败或响应不符契约
        muted = bool(resp['muted'])
        cls._muted_cache.put(session_id, muted)
        return muted

    @classmethod
    def mute(cls, binding: Dict[str, Any], session_id: str) -> Optional[bool]:
        """将 session 标记为静音（写穿透 + 幂等短路）

        Returns:
            True  = 本次调用将 session 从未静音切到静音
            False = 幂等：操作前已处于静音
            None  = callback 调用失败（缓存不更新）
        """
        if not session_id:
            return None
        # 缓存已知静音 → 幂等短路，零 RPC
        if cls._muted_cache.get(session_id) is True:
            return False  # 幂等：无状态变化
        resp = cls._call_mute_api(binding, session_id, 'mute')
        if resp is None or 'changed' not in resp:
            return None  # 故障降级：调用失败或响应不符契约（不更新缓存）
        cls._muted_cache.put(session_id, True)
        return bool(resp['changed'])

    @classmethod
    def unmute(cls, binding: Dict[str, Any], session_id: str) -> Optional[bool]:
        """清除 session 静音标志（写穿透 + 幂等短路）

        Returns:
            True  = 本次调用将 session 从静音切到未静音
            False = 幂等：操作前就未静音
            None  = callback 调用失败（缓存不更新）

        注：auto_unmute 钩子在每条非命令消息触发，稳态（缓存已知未静音）下
        短路返回 False，零 RPC。这是在缓存层承担入站路径的优化。
        """
        if not session_id:
            return None
        # 缓存已知未静音 → 幂等短路，零 RPC
        if cls._muted_cache.get(session_id) is False:
            return False  # 幂等：无状态变化
        resp = cls._call_mute_api(binding, session_id, 'unmute')
        if resp is None or 'changed' not in resp:
            return None  # 故障降级：调用失败或响应不符契约（不更新缓存）
        cls._muted_cache.put(session_id, False)
        return bool(resp['changed'])

    @classmethod
    def invalidate_mute_cache(cls, session_id: Optional[str] = None) -> None:
        """清除 mute 状态缓存，下次查询会回源 callback

        常规使用不需要调用：muted 字段只通过 SessionFacade.mute / unmute 写入，
        gateway 缓存自然和 callback 一致。此方法用作保底工具——当知道 callback
        端 mute 状态可能绕过 SessionFacade 被改变时（未来若出现此类场景）主动
        失效缓存，避免旧值长期短路。

        Args:
            session_id: 指定 session 时只清该条；None 时清空整个缓存。
        """
        if session_id is None:
            size = len(cls._muted_cache)
            if size:
                logger.debug("[session-facade] invalidate entire mute cache (size=%d)", size)
            cls._muted_cache.clear()
        else:
            cls._muted_cache.pop(session_id)

    # =========================================================================
    # 内部：callback /cb/session/mute 调用
    # =========================================================================

    @classmethod
    def _call_mute_api(cls, binding: Dict[str, Any], session_id: str,
                       action: str) -> Optional[Dict[str, Any]]:
        """调 /cb/session/mute；action ∈ {mute, unmute, query}。失败返回 None。"""
        if cls._forward_fn is None:
            logger.error("[session-facade] forward_fn not configured")
            return None
        try:
            resp = cls._forward_fn(binding, '/cb/session/mute', {
                'session_id': session_id,
                'action': action,
            })
            if resp and resp.get('ok'):
                return resp
            logger.warning("[session-facade] /cb/session/mute (%s) failed: %s", action, resp)
            return None
        except Exception as e:
            logger.error("[session-facade] /cb/session/mute error: %s", e)
            return None
