"""Shell 命令构建工具

根据 shell 类型构建命令参数列表，stdlib-only，无项目依赖。
"""

import os
import shlex
from typing import List


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
