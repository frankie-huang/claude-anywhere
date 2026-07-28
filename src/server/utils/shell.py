"""Shell 命令构建工具

根据 shell 类型构建命令参数列表，stdlib-only，无项目依赖。
"""

import os
import shlex
from typing import List, Optional


def build_shell_cmd(shell: str, cmd_str: str) -> List[str]:
    """根据 shell 类型构建命令参数列表，确保能加载配置文件中的别名和环境变量

    ┌──────────────┬───────┬─────────────────────────────────┐
    │ Shell        │ 参数  │ 配置文件                        │
    ├──────────────┼───────┼─────────────────────────────────┤
    │ zsh          │ -ic   │ ~/.zshrc                        │
    │ bash         │ -ic   │ ~/.bashrc                       │
    │ fish         │ -c    │ ~/.config/fish/config.fish      │
    │ dash         │ -lc   │ ~/.profile                      │
    │ 其他 POSIX   │ -lc   │ 对应 shell 的登录配置文件       │
    ├──────────────┼───────┼─────────────────────────────────┤
    │ ❌ pwsh      │ N/A   │ 需用 -NoProfile -Command        │
    │ ❌ csh/tcsh  │ N/A   │ 语法完全不同，暂不支持           │
    └──────────────┴───────┴─────────────────────────────────┘

    bash 用 -ic 而非 -lc：-lc 是非交互 login shell，会触发 .bashrc 开头的
    非交互早退保护，导致用户自定义函数/别名（如 claude 包装函数）不加载，
    子进程里命令找不到（exit 127）。

    ⚠ 调用约定：用本函数构建的命令启动子进程时必须传 start_new_session=True。
    交互式 shell 有控制终端时会 tcsetpgrp 抢占前台进程组，且命令是真实二进制
    时 bash 会 exec 替换自身（进程名直接变成目标命令），没有退出时机归还前台
    组，父进程后续操作终端即被 SIGTTIN/SIGTTOU 挂起。

    rc/profile 文件可能修改 PATH 顺序，导致找到与用户终端不同版本的二进制
    （如 claude）。为确保一致性，在命令前注入当前进程的 PATH，覆盖
    rc/profile 设置的 PATH。

    Args:
        shell: shell 路径，如 '/bin/bash'
        cmd_str: 要执行的命令字符串

    Returns:
        命令参数列表，如 ['/bin/bash', '-ic', 'export PATH=...; echo hello']
    """
    shell_name = os.path.basename(shell)

    # 将当前进程的 PATH 注入到 shell 命令中，确保 rc/profile
    # 加载完（获得别名/函数）后，PATH 仍与服务进程一致
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
    elif shell_name in ('zsh', 'bash'):
        return [shell, '-ic', path_prefix + cmd_str]
    else:
        return [shell, '-lc', path_prefix + cmd_str]


_SHELL_NOISE_PATTERNS = (
    # bash 无控制终端时成对输出下面两行（zsh 措辞为 "can't set"，取公共子串覆盖）
    #   bash: cannot set terminal process group (-1): Inappropriate ioctl for device
    'set terminal process group',
    #   bash: no job control in this shell
    'no job control in this shell',
)


def strip_shell_noise(text: Optional[str]) -> str:
    """剔除交互式 shell 在无控制终端下输出的 job control 诊断行

    这些行不是错误，只是 shell 在说"我没法做作业控制"，而我们跑完一条命令就
    退出、本就不需要它；它们却占据 stderr 稀释真实失败原因。

    只在「交互式 shell + 无控制终端」同时成立时出现（用户在自己终端执行同样
    命令不会看到）。分配 pty 可从源头消除，但与 start_new_session 脱离控制终端
    的诉求互斥，故只能事后过滤。

    只匹配 shell 自身的已知常态噪音：不含 agent CLI 特有警告，也不做通用错误
    串过滤（'Inappropriate ioctl for device' 也可能来自命令自身，吞掉反丢诊断）。
    """
    if not text:
        return ''
    kept = [ln for ln in text.splitlines()
            if not any(p in ln for p in _SHELL_NOISE_PATTERNS)]
    return '\n'.join(kept).strip()
