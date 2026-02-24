import html
import json
import logging
import threading
import urllib.request

from typing import Tuple

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
    """从 Callback 侧发送文本消息到飞书（兼容单机和分离部署）

    优先使用 FeishuAPIService 直接发送（单机模式），
    不可用时通过网关 /gw/feishu/send 转发（分离部署模式）。

    Args:
        chat_id: 飞书群聊 ID
        text: 消息文本

    Returns:
        (success, message_id or error)
    """
    # 方式 1：直接通过 FeishuAPIService 发送（单机模式）
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

    # 方式 2：通过网关转发（分离部署模式）
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
