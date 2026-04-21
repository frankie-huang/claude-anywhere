import html
import json
import logging
import os
import shlex
import threading
import urllib.request

from typing import Any, Dict, List, Tuple

from config import CALLBACK_PAGE_CLOSE_DELAY

logger = logging.getLogger(__name__)


def post_json(url, data, auth_token=None, timeout=10):
    """发送 JSON POST 请求（无代理）

    Args:
        url: 请求 URL
        data: 请求数据（dict，会被 JSON 序列化）
        auth_token: 可选的认证令牌（X-Auth-Token header）
        timeout: 超时时间（秒）

    Returns:
        解析后的 JSON 响应（dict）

    Raises:
        urllib.error.HTTPError, urllib.error.URLError, socket.timeout, Exception
    """
    headers = {'Content-Type': 'application/json'}
    if auth_token:
        headers['X-Auth-Token'] = auth_token

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode('utf-8'),
        headers=headers,
        method='POST'
    )

    no_proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(no_proxy_handler)
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def send_feishu_text(chat_id: str, text: str) -> Tuple[bool, str]:
    """从 Callback 侧调用飞书网关，发送文本消息（兼容单机和分离部署）

    优先使用 FeishuAPIService 直接发送（单机模式），
    不可用时通过网关 /gw/feishu/send 转发（分离部署模式）。

    Args:
        chat_id: 飞书群聊 ID
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
                success, result = service.send_text(
                    text,
                    receive_id=chat_id,
                    receive_id_type='chat_id'
                )
                return (success, result)
        except Exception as e:
            logger.warning("[send_feishu_text] FeishuAPIService unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关转发
    try:
        from config import FEISHU_GATEWAY_URL, FEISHU_OWNER_ID
        from services.auth_token_store import AuthTokenStore

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
        resp = post_json(api_url, data, auth_token=auth_token)
        if resp.get('success'):
            return (True, resp.get('data', {}).get('message_id', ''))
        else:
            return (False, resp.get('error', 'unknown'))
    except Exception as e:
        return (False, str(e))


def create_feishu_group(name: str) -> Tuple[bool, str]:
    """从 Callback 侧调用飞书网关，创建飞书群聊（兼容单机和分离部署）

    owner_id 由各模式自行获取：
    - 单机模式：从 FEISHU_OWNER_ID 配置读取
    - 分离部署：网关从 binding 中的 owner_id 获取

    Args:
        name: 群聊名称

    Returns:
        (success, chat_id or error)
    """
    from config import IS_CALLBACK_BACKEND, FEISHU_OWNER_ID

    if not IS_CALLBACK_BACKEND:
        # 单机模式：直接调用网关侧统一入口（创建 + 写归属记录一次完成）
        try:
            from handlers.feishu import create_group_chat_and_record
            return create_group_chat_and_record(name, FEISHU_OWNER_ID)
        except Exception as e:
            logger.warning("[create_feishu_group] create_group_chat_and_record unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关转发
    try:
        from config import FEISHU_GATEWAY_URL
        from services.auth_token_store import AuthTokenStore

        if not FEISHU_GATEWAY_URL:
            return (False, 'no feishu service available')

        store = AuthTokenStore.get_instance()
        auth_token = store.get() if store else ''
        if not auth_token:
            return (False, 'no auth_token available')

        api_url = FEISHU_GATEWAY_URL.rstrip('/') + '/gw/feishu/create-group'
        data = {
            'name': name,
            'owner_id': FEISHU_OWNER_ID
        }
        resp = post_json(api_url, data, auth_token=auth_token)
        if resp.get('success'):
            return (True, resp.get('chat_id', ''))
        else:
            return (False, resp.get('error', 'unknown'))
    except Exception as e:
        return (False, str(e))


def dissolve_feishu_groups(chat_ids: List[str]) -> Dict[str, Any]:
    """从 Callback 侧调用飞书网关解散飞书群聊（兼容单机和分离部署，支持批量）

    归属判断由网关侧完成：非服务创建的群聊会被跳过（skipped_items），不视为失败。

    Args:
        chat_ids: 群聊 ID 列表

    Returns:
        {
            'dissolved_items': List[str],     # 实际解散的 chat_id
            'skipped_items': List[str],       # 非服务创建的 chat_id
            'failed': List[{chat_id, error}]  # API 错误
        }
    """
    if not chat_ids:
        return {'dissolved_items': [], 'skipped_items': [], 'failed': []}

    from config import IS_CALLBACK_BACKEND, FEISHU_OWNER_ID

    if not IS_CALLBACK_BACKEND:
        # 单机模式：直接调用网关侧函数（解散 + 清理归属记录）
        try:
            from handlers.feishu import batch_dissolve_groups
            return batch_dissolve_groups(chat_ids, FEISHU_OWNER_ID)
        except Exception as e:
            logger.warning("[dissolve_feishu_groups] batch_dissolve_groups unavailable: %s", e)

    # 分离部署模式（或单机 fallback）：通过网关批量转发
    try:
        from config import FEISHU_GATEWAY_URL
        from services.auth_token_store import AuthTokenStore

        if not FEISHU_GATEWAY_URL:
            return {'dissolved_items': [], 'skipped_items': [],
                    'failed': [{'chat_id': cid, 'error': 'no feishu service available'} for cid in chat_ids]}

        store = AuthTokenStore.get_instance()
        auth_token = store.get() if store else ''
        if not auth_token:
            return {'dissolved_items': [], 'skipped_items': [],
                    'failed': [{'chat_id': cid, 'error': 'no auth_token available'} for cid in chat_ids]}

        api_url = FEISHU_GATEWAY_URL.rstrip('/') + '/gw/feishu/dissolve-groups'
        resp = post_json(api_url, {'chat_ids': chat_ids, 'owner_id': FEISHU_OWNER_ID}, auth_token=auth_token)
        return {
            'dissolved_items': resp.get('dissolved_items', []),
            'skipped_items': resp.get('skipped_items', []),
            'failed': resp.get('failed', []),
        }
    except Exception as e:
        return {'dissolved_items': [], 'skipped_items': [],
                'failed': [{'chat_id': cid, 'error': str(e)} for cid in chat_ids]}


def send_json(handler, status, data):
    """发送 JSON 响应

    Args:
        handler: HTTP 请求处理器实例（BaseHTTPRequestHandler）
        status: HTTP 状态码
        data: 响应数据（dict，会被 JSON 序列化）
    """
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json.dumps(data).encode())


def build_shell_cmd(shell: str, cmd_str: str) -> List[str]:
    """根据 shell 类型构建命令参数列表，确保能加载配置文件中的别名和环境变量

    ┌──────────────┬───────┬─────────────────────────────────┐
    │ Shell        │ 参数  │ 配置文件                        │
    ├──────────────┼───────┼─────────────────────────────────┤
    │ zsh          │ -ic   │ ~/.zshrc                        │
    │ fish         │ -c    │ ~/.config/fish/config.fish      │
    │ bash         │ -lc   │ ~/.bashrc                       │
    │ dash         │ -lc   │ ~/.profile                      │
    │ 其他 POSIX   │ -lc   │ 对应 shell 的登录配置文件       │
    ├──────────────┼───────┼─────────────────────────────────┤
    │ ❌ pwsh      │ N/A   │ 需用 -NoProfile -Command        │
    │ ❌ csh/tcsh  │ N/A   │ 语法完全不同，暂不支持           │
    └──────────────┴───────┴─────────────────────────────────┘

    login shell 会重新加载 profile，可能导致 PATH 顺序与用户终端不同，
    从而找到不同版本的二进制（如 claude）。为确保一致性，在命令前注入
    当前进程的 PATH，覆盖 login shell profile 设置的 PATH。

    Args:
        shell: shell 路径，如 '/bin/bash'
        cmd_str: 要执行的命令字符串

    Returns:
        命令参数列表，如 ['/bin/bash', '-lc', 'export PATH=...; echo hello']
    """
    shell_name = os.path.basename(shell)

    # 将当前进程的 PATH 注入到 shell 命令中，确保 login shell
    # 加载完 profile（获得别名/函数）后，PATH 仍与服务进程一致
    current_path = os.environ.get('PATH', '')
    if current_path:
        path_prefix = f"export PATH={shlex.quote(current_path)}; "
    else:
        path_prefix = ""

    if shell_name == 'fish':
        # fish 语法不同，用 set -x 替代 export
        fish_prefix = ""
        if current_path:
            fish_paths = ' '.join(shlex.quote(p) for p in current_path.split(':'))
            fish_prefix = f"set -x PATH {fish_paths}; "
        return [shell, '-c', fish_prefix + cmd_str]
    elif shell_name == 'zsh':
        return [shell, '-ic', path_prefix + cmd_str]
    else:
        return [shell, '-lc', path_prefix + cmd_str]


def run_in_background(func, args=()):
    """在后台线程中执行函数

    Args:
        func: 要执行的函数
        args: 位置参数元组
    """
    thread = threading.Thread(target=func, args=args, daemon=True)
    thread.start()


def send_html_response(handler, status, title, message,
                       success=True, close_timeout=None, vscode_uri=None):
    """发送 HTML 响应页面

    Args:
        handler: HTTP 请求处理器实例（BaseHTTPRequestHandler）
        status: HTTP 状态码
        title: 页面标题
        message: 显示消息
        success: 是否成功
        close_timeout: 自动关闭页面超时时间（秒），默认从环境变量 CALLBACK_PAGE_CLOSE_DELAY 读取
        vscode_uri: VSCode URI，配置后会自动跳转到 VSCode
    """
    if close_timeout is None:
        close_timeout = CALLBACK_PAGE_CLOSE_DELAY
    color = '#28a745' if success else '#dc3545'
    icon = '✓' if success else '✗'

    # VSCode 跳转相关的 HTML 和 JS
    vscode_redirect_html = ''
    vscode_redirect_js = ''
    if vscode_uri and success:
        # 判断是本地还是远程，用于显示对应的设置提示
        is_local = vscode_uri.startswith('vscode://file')
        setting_hint = (
            '"security.promptForLocalFileProtocolHandling": false'
            if is_local
            else '"security.promptForRemoteFileProtocolHandling": false'
        )

        vscode_redirect_html = f'''
            <div class="vscode-redirect" id="vscodeRedirect">
                正在跳转到 VSCode...
            </div>
            <div class="vscode-fallback" id="vscodeFallback">
                <p>跳转失败？<a href="{html.escape(vscode_uri)}" class="vscode-link">点击手动打开 VSCode</a></p>
                <p class="vscode-hint">首次使用需在 VSCode settings.json 中添加：<br><code>{setting_hint}</code></p>
            </div>'''
        vscode_redirect_js = f'''
            // VSCode 自动跳转
            const vscodeUri = {json.dumps(vscode_uri)};
            const vscodeRedirect = document.getElementById('vscodeRedirect');
            const vscodeFallback = document.getElementById('vscodeFallback');

            // 500ms 后尝试跳转
            setTimeout(function() {{
                window.location.href = vscodeUri;

                // 2 秒后检测是否仍在页面（跳转失败）
                setTimeout(function() {{
                    if (vscodeRedirect) {{
                        vscodeRedirect.textContent = '跳转失败';
                        vscodeRedirect.style.color = '#dc3545';
                    }}
                    if (vscodeFallback) {{
                        vscodeFallback.style.display = 'block';
                    }}
                }}, 2000);
            }}, 500);'''

    response_html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{html.escape(title)}</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    margin: 0;
                    background: #f5f5f5;
                }}
                .card {{
                    background: white;
                    border-radius: 12px;
                    padding: 40px;
                    text-align: center;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                    max-width: 400px;
                }}
                .icon {{
                    font-size: 48px;
                    color: {color};
                    margin-bottom: 20px;
                }}
                .title {{
                    font-size: 24px;
                    color: #333;
                    margin-bottom: 10px;
                }}
                .message {{
                    color: #666;
                    line-height: 1.6;
                    margin-bottom: 20px;
                }}
                .vscode-redirect {{
                    color: #007acc;
                    font-size: 14px;
                    margin-top: 15px;
                    padding-top: 15px;
                    border-top: 1px solid #eee;
                }}
                .vscode-fallback {{
                    display: none;
                    color: #666;
                    font-size: 14px;
                    margin-top: 10px;
                }}
                .vscode-link {{
                    color: #007acc;
                    text-decoration: none;
                }}
                .vscode-link:hover {{
                    text-decoration: underline;
                }}
                .vscode-hint {{
                    font-size: 12px;
                    color: #999;
                    margin-top: 10px;
                    line-height: 1.4;
                }}
                .vscode-hint code {{
                    display: inline-block;
                    background: #f5f5f5;
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-family: 'Consolas', 'Monaco', monospace;
                    font-size: 11px;
                    color: #d63384;
                }}
                .countdown {{
                    color: #999;
                    font-size: 14px;
                    margin-top: 15px;
                    padding-top: 15px;
                    border-top: 1px solid #eee;
                }}
                .close-hint {{
                    display: none;
                    color: #666;
                    font-size: 14px;
                    margin-top: 10px;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="icon">{icon}</div>
                <div class="title">{html.escape(title)}</div>
                <div class="message">{html.escape(message)}</div>
                {vscode_redirect_html}
                <div class="countdown" id="countdown">
                    页面将在 <span id="seconds">{close_timeout}</span> 秒后自动关闭
                </div>
                <div class="close-hint" id="closeHint">
                    您现在可以关闭此页面
                </div>
            </div>
            <script>
                (function() {{{vscode_redirect_js}
                    const timeout = {close_timeout};
                    let seconds = timeout;
                    const countdownEl = document.getElementById('seconds');
                    const countdownDiv = document.getElementById('countdown');
                    const closeHint = document.getElementById('closeHint');

                    const timer = setInterval(function() {{
                        seconds--;
                        if (countdownEl) {{
                            countdownEl.textContent = seconds;
                        }}

                        if (seconds <= 0) {{
                            clearInterval(timer);
                            if (countdownDiv) {{
                                countdownDiv.style.display = 'none';
                            }}
                            if (closeHint) {{
                                closeHint.style.display = 'block';
                            }}
                            // 尝试关闭窗口（只有脚本打开的窗口才能被关闭）
                            try {{
                                window.close();
                            }} catch (e) {{
                                // 忽略关闭失败（浏览器安全限制）
                            }}
                        }}
                    }}, 1000);
                }})();
            </script>
        </body>
        </html>'''

    handler.send_response(status)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.end_headers()
    handler.wfile.write(response_html.encode('utf-8'))
