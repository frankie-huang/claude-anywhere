"""HTTP 客户端工具

通用的出站 HTTP 请求工具，stdlib-only，无项目依赖。
"""

import json
import urllib.request

from typing import Any, Dict, Optional


def post_json(url: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None,
              timeout: int = 10) -> Dict[str, Any]:
    """发送 JSON POST 请求（无代理）

    Args:
        url: 请求 URL
        data: 请求数据（dict，会被 JSON 序列化）
        headers: 可选的额外请求头（dict），会合并到默认头之上，可覆盖 Content-Type
        timeout: 超时时间（秒）

    Returns:
        解析后的 JSON 响应（dict）

    Raises:
        urllib.error.HTTPError, urllib.error.URLError, socket.timeout, Exception
    """
    final_headers = {'Content-Type': 'application/json'}
    if headers:
        final_headers.update(headers)

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode('utf-8'),
        headers=final_headers,
        method='POST'
    )

    no_proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(no_proxy_handler)
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))
