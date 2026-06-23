"""并发工具

后台线程执行等通用并发工具，stdlib-only，无项目依赖。
"""

import threading

from typing import Any, Callable, Tuple


def run_in_background(func: Callable[..., Any], args: Tuple[Any, ...] = ()) -> None:
    """在后台线程中执行函数

    Args:
        func: 要执行的函数
        args: 位置参数元组
    """
    thread = threading.Thread(target=func, args=args, daemon=True)
    thread.start()
