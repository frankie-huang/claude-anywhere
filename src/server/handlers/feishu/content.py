"""
Feishu Content - 入站消息内容解析

负责将飞书各类消息的 content（JSON 字符串）解析为纯文本，供下游路由/转发使用。
包含 @提及 解析：识别 bot/人员，构建替换表，在解析过程中完成占位符替换。

当前支持的 message_type：
- text: 直接取 text 字段，替换 @_user_X 占位符
- post: 富文本，提取标题、普通文本/md/超链接/代码块、@提及（img/media/emotion/hr 等忽略）

其他类型（image/file/audio/video 等）无文本内容，返回空字符串。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# text 类型中 @_user_N 占位符的匹配模式（带或不带尾随空格）
_AT_PLACEHOLDER_PATTERN = re.compile(r'@_user_\d+\s?')


# =========================================================================
# @提及 解析
# =========================================================================

def build_mention_resolution(mentions: Optional[List[Dict[str, Any]]]) -> Tuple[Dict[str, str], bool]:
    """从飞书消息的 mentions 数组构建 @提及 替换表，并判断是否 @bot

    遍历 mentions，对每个 key（如 @_user_1）：
    - bot 提及（FEISHU_APP_ID 或 bot_open_id 匹配）→ 替换为 ''（删除）
    - 人员提及 → 替换为 '@name(user_id)'（姓名+ID，与协作者前缀对齐）

    Args:
        mentions: 飞书消息 event.message.mentions 数组（可能为 None 或空）

    Returns:
        (resolution, is_at_bot):
            resolution: key → 替换文本的映射，如 {'@_user_1': '', '@_user_2': '@张三(abc123)'}
            is_at_bot: 是否包含 @bot 提及
    """
    if not mentions:
        return {}, False

    from config import FEISHU_APP_ID
    from services.feishu_api import FeishuAPIService

    bot_open_id = ''
    service = FeishuAPIService.get_instance()
    if service:
        bot_open_id = service.bot_open_id or ''

    resolution: Dict[str, str] = {}
    is_at_bot = False

    for m in mentions:
        if not isinstance(m, dict):
            continue
        key = m.get('key', '')
        if not key:
            continue

        # 判断是否本服务的 bot（区别于群内其他 bot）
        # 不依赖 mentioned_type（2026-04 前飞书不下发，缺失时整体跳过会漏判 @bot）
        # if m.get('mentioned_type') == 'bot':
        is_our_bot = False
        # 方法1: bot_info.app_id 精确匹配（飞书 2026-04 短暂下发过，当前死代码）
        bot_info = m.get('bot_info', {})
        if isinstance(bot_info, dict) and FEISHU_APP_ID and bot_info.get('app_id') == FEISHU_APP_ID:
            is_our_bot = True
        # 方法2: open_id 匹配（当前实际生效）
        elif bot_open_id:
            mention_id = m.get('id', {})
            if isinstance(mention_id, dict) and mention_id.get('open_id') == bot_open_id:
                is_our_bot = True
            elif isinstance(mention_id, str) and mention_id == bot_open_id:
                is_our_bot = True

        if is_our_bot:
            resolution[key] = ''
            is_at_bot = True
        else:
            # 人员或其他 bot 提及：用 @name(user_id) 显示
            # name 让 agent 识别具体是谁，user_id 与协作者前缀对齐（[来自群成员 user_id]）
            name = m.get('name', '')
            mention_id = m.get('id', {})
            uid = mention_id.get('user_id', '') if isinstance(mention_id, dict) else ''
            if name and uid:
                resolution[key] = '@%s(%s)' % (name, uid)
            elif name:
                resolution[key] = '@%s' % name
            else:
                resolution[key] = key

    return resolution, is_at_bot


# =========================================================================
# 消息内容提取
# =========================================================================

def extract_message_text(message_type: str, content: str,
                         mention_resolution: Optional[Dict[str, str]] = None) -> str:
    """从飞书消息 content 中提取纯文本

    Args:
        message_type: 消息类型（text / post / image / ...）
        content: 消息 content 字段（JSON 字符串）
        mention_resolution: @提及 替换表（key→替换文本），由 build_mention_resolution 构建。
            key 如 '@_user_1'，value 为替换后的文本（人员→'@name(user_id)'，bot→''）。
            未传入时不做 @ 替换。

    Returns:
        提取出的纯文本；非文本类消息（图片/文件等）返回空字符串；
        content 非合法 JSON 时原样返回。
    """
    try:
        content_obj = json.loads(content)
    except json.JSONDecodeError:
        return content
    if not isinstance(content_obj, dict):
        return ''

    text = content_obj.get('text', '')

    if text and mention_resolution:
        # text 类型：替换 @_user_X 占位符（按 resolution 表逐个替换）
        text = _apply_mention_resolution(text, mention_resolution)

    # post 类型：遍历二维数组提取各元素文本
    if not text and message_type == 'post':
        text = _extract_post_text(content_obj, mention_resolution)

    return text


def _apply_mention_resolution(text: str,
                              mention_resolution: Dict[str, str]) -> str:
    """在文本中替换 @_user_X 占位符

    按 resolution 表逐个精确替换（带或不带尾随空格），
    未在表中的占位符保留原样（安全降级）。
    """
    def _replacer(match):
        placeholder = match.group(0).rstrip()  # '@_user_1' (去尾部空格用于查表)
        if placeholder in mention_resolution:
            replacement = mention_resolution[placeholder]
            # 替换为空时同时吃掉尾随空格；替换为内容时保留空格分隔
            if not replacement:
                return ''
            return replacement + (' ' if match.group(0) != placeholder else '')
        return match.group(0)  # 不在表中，保留原样

    return _AT_PLACEHOLDER_PATTERN.sub(_replacer, text)


def _extract_post_text(content_obj: Dict[str, Any],
                       mention_resolution: Optional[Dict[str, str]] = None) -> str:
    """提取 post 富文本的纯文本（标题 + 普通文本/md/超链接/代码块/@提及）"""
    paragraphs: List[str] = []
    # 接收侧 post 顶层含 title 字段，作为首段并入正文（发送侧才带 zh_cn 语言层）
    title = content_obj.get('title', '')
    if title:
        paragraphs.append(title)
    content_list = content_obj.get('content', [])
    for paragraph in content_list if isinstance(content_list, list) else []:
        if not isinstance(paragraph, list):
            continue
        para_parts: List[str] = []
        for elem in paragraph:
            if not isinstance(elem, dict):
                continue
            tag = elem.get('tag')
            if tag in ('text', 'md'):
                para_parts.append(elem.get('text', ''))
            elif tag == 'a':
                # 超链接转为 markdown 形式，文本缺失时退化为 href
                href = elem.get('href', '')
                link_text = elem.get('text', '') or href
                para_parts.append(f'[{link_text}]({href})' if href else link_text)
            elif tag == 'code_block':
                # 代码块用 ``` 包裹，附带语言标识；text 自带结尾换行需剥除，避免尾部空行
                para_parts.append(f"```{elem.get('language', '')}\n{elem.get('text', '').rstrip(chr(10))}\n```")
            elif tag == 'at':
                # @提及：通过 resolution 表查替换文本，查不到退回 user_name
                at_key = elem.get('user_id', '')
                if mention_resolution and at_key in mention_resolution:
                    label = mention_resolution[at_key]
                else:
                    label = '@%s' % elem.get('user_name', '') if elem.get('user_name') else ''
                if label:
                    para_parts.append(label)
            # img/media/emotion/hr 等无文本内容，不提取
        para_text = ''.join(para_parts)
        if para_text:
            paragraphs.append(para_text)
    return '\n'.join(paragraphs)
