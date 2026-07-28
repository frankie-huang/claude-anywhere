#!/usr/bin/env python3
"""setup.sh init 交互式初始化脚本

全流程覆盖：.env 配置 → 依赖检测 → Hook 配置 → 服务启动。
由 setup.sh init 子命令调用，不直接执行。

用法:
    python3 setup_init.py <source_dir>

参数:
    source_dir: 项目根目录
"""

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
from typing import List, Optional


# =============================================================================
# Terminal — 终端环境基础设施
# =============================================================================

class Terminal:
    """终端环境管理：初始化、前台进程组控制

    解决两类终端问题：
    1. 子进程抢占前台：依赖检测通过用户的 shell（zsh/bash -ic）
       检测 claude CLI，这些 shell 会启用 job control 并调用 tcsetpgrp() 抢占前台，
       退出后终端的前台进程组可能已不在本进程，导致后续 print() 触发 SIGTTOU 被挂起。
    2. 非终端环境：setup.sh 通过管道调用时 stdin/stdout 可能不是终端，
       需要打开 /dev/tty 确保交互式组件能正常读写。
    """

    @staticmethod
    def init():
        """初始化终端环境（程序启动时调用一次）

        1. 忽略 SIGTTOU/SIGTTIN：防止进程因写/读终端而被挂起
        2. 确保 stdin/stdout 连接到终端
        3. 抢到前台进程组
        """
        # 忽略后台终端操作信号
        for name in ('SIGTTOU', 'SIGTTIN'):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, signal.SIG_IGN)

        # 如果 stdin/stdout 不是 tty，尝试打开 /dev/tty
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            try:
                tty_in = open('/dev/tty', 'r', buffering=1)
                tty_out = open('/dev/tty', 'w', buffering=1)
            except OSError:
                pass
            else:
                if not sys.stdin.isatty():
                    sys.stdin = tty_in
                else:
                    tty_in.close()
                if not sys.stdout.isatty():
                    sys.stdout = tty_out
                else:
                    tty_out.close()

        Terminal.ensure_foreground()

    @staticmethod
    def ensure_foreground():
        """确保当前进程在前台进程组

        依赖检测中通过用户 shell 检测 claude CLI 时，shell 可能抢占前台进程组。
        每次读取按键前调用，防止 termios 操作（setraw/tcsetattr）因非前台而失败。
        """
        if not all(hasattr(os, attr) for attr in ('getpgrp', 'tcgetpgrp', 'tcsetpgrp')):
            return
        try:
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
            if os.tcgetpgrp(fd) != os.getpgrp():
                os.tcsetpgrp(fd, os.getpgrp())
        except OSError:
            pass


# =============================================================================
# EditableBuffer — 可编辑文本缓冲区
# =============================================================================

class EditableBuffer:
    """带光标位置的可编辑字符串，支持插入、删除、左右移动"""

    def __init__(self, text=''):
        self.buf = list(text)
        self.cursor = len(self.buf)

    def insert(self, ch):
        self.buf.insert(self.cursor, ch)
        self.cursor += 1

    def backspace(self):
        if self.cursor > 0:
            self.buf.pop(self.cursor - 1)
            self.cursor -= 1
            return True
        return False

    def move_left(self):
        if self.cursor > 0:
            self.cursor -= 1

    def move_right(self):
        if self.cursor < len(self.buf):
            self.cursor += 1

    def clear(self):
        self.buf.clear()
        self.cursor = 0

    def handle_key(self, key):
        """处理按键，返回 True 表示已处理，False 表示未识别

        支持: left, right, backspace, 可打印字符
        不处理: enter, esc, up, down（由调用方决定语义）
        """
        if key == 'left':
            self.move_left()
        elif key == 'right':
            self.move_right()
        elif key in ('\x7f', '\x08'):
            self.backspace()
        elif len(key) == 1 and key.isprintable():
            self.insert(key)
        else:
            return False
        return True

    @property
    def text(self):
        return ''.join(self.buf)

    def display(self, mask=False):
        """返回带光标标记的显示字符串（光标位置字符反显）"""
        chars = ['*'] * len(self.buf) if mask else list(self.buf)
        before = ''.join(chars[:self.cursor])
        if self.cursor < len(chars):
            cursor_char = f"\033[7m{chars[self.cursor]}\033[0m"
        else:
            cursor_char = "\033[7m \033[0m"
        after = ''.join(chars[self.cursor + 1:]) if self.cursor < len(chars) else ''
        return before + cursor_char + after

    def __bool__(self):
        return bool(self.buf)

    def __len__(self):
        return len(self.buf)


# =============================================================================
# TerminalUI — 终端交互组件
# =============================================================================

class TerminalUI:
    """终端交互 UI 组件：颜色输出、选择器、输入框"""

    # ANSI 颜色
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    RED = '\033[31m'
    BLUE = '\033[34m'
    CYAN = '\033[36m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    NC = '\033[0m'

    # --- 输出方法 ---

    @classmethod
    def print_success(cls, msg):
        print(f"  {cls.GREEN}\u2713{cls.NC} {msg}")

    @classmethod
    def print_warning(cls, msg):
        print(f"  {cls.YELLOW}\u26a0{cls.NC} {msg}")

    @classmethod
    def print_error(cls, msg):
        print(f"  {cls.RED}\u2717{cls.NC} {msg}")

    @classmethod
    def print_info(cls, msg):
        print(f"  {cls.BLUE}\u2139{cls.NC} {msg}")

    @classmethod
    def print_section(cls, title):
        print(f"\n{cls.CYAN}\u2501\u2501\u2501 {cls.BOLD}{title}{cls.NC}{cls.CYAN} \u2501\u2501\u2501{cls.NC}")

    @classmethod
    def print_banner(cls, title, width=50):
        print(f"\n{cls.CYAN}{'=' * width}{cls.NC}")
        print(f"{cls.BOLD}  {title}{cls.NC}")
        print(f"{cls.CYAN}{'=' * width}{cls.NC}")

    @classmethod
    def print_dim(cls, msg):
        print(f"  {cls.DIM}{msg}{cls.NC}")

    @staticmethod
    def print_text(msg=''):
        print(f"  {msg}" if msg else '')

    # --- 内部方法 ---

    @staticmethod
    def _read_key():
        """读取单个按键（支持方向键等特殊键）"""
        import tty
        import termios
        Terminal.ensure_foreground()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                seq = sys.stdin.read(2)
                if seq == '[A':
                    return 'up'
                if seq == '[B':
                    return 'down'
                if seq == '[C':
                    return 'right'
                if seq == '[D':
                    return 'left'
                return 'esc'
            if ch in ('\r', '\n'):
                return 'enter'
            if ch == '\x03':
                raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    @classmethod
    def hidden_cursor(cls):
        """上下文管理器：隐藏终端光标，退出时恢复

        用法:
            with cls.hidden_cursor():
                # 交互循环中使用 EditableBuffer.display() 的反显光标
        """
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            sys.stdout.write('\033[?25l')
            sys.stdout.flush()
            try:
                yield
            finally:
                sys.stdout.write('\033[?25h')
                sys.stdout.flush()

        return _ctx()

    @classmethod
    def _raw_input(cls, mask=False):
        """基于 _read_key 的行输入，支持左右移动光标

        Args:
            mask: True 时回显 *，False 时回显原文
        """
        eb = EditableBuffer()

        def _redraw():
            sys.stdout.write(f"\r\033[4C")  # 回到 "> " 之后
            sys.stdout.write('\033[K')       # 清当前行到行尾（不影响下方）
            sys.stdout.write(eb.display(mask=mask))
            sys.stdout.flush()

        with cls.hidden_cursor():
            _redraw()  # 初始绘制反显光标
            while True:
                key = cls._read_key()
                if key == 'enter':
                    sys.stdout.write(f"\r\033[4C\033[J")
                    if mask:
                        sys.stdout.write('*' * len(eb))
                    else:
                        sys.stdout.write(eb.text)
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return eb.text
                if eb.handle_key(key):
                    _redraw()

    @staticmethod
    def _visible_width(text):
        """计算文本在终端中的可见宽度（去除 ANSI 转义码，CJK 字符算 2 列）"""
        # 去除 ANSI 转义码
        stripped = re.sub(r'\033\[[^m]*m', '', text)
        width = 0
        for ch in stripped:
            cp = ord(ch)
            # CJK 统一表意文字 + 全角符号
            if (0x2E80 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
                    or 0xFE30 <= cp <= 0xFE4F or 0xFF00 <= cp <= 0xFF60
                    or 0x20000 <= cp <= 0x2FA1F):
                width += 2
            else:
                width += 1
        return width

    @classmethod
    def _visual_line_count(cls, text):
        """计算一行文本在终端中实际占用的视觉行数（考虑自动换行）"""
        cols = shutil.get_terminal_size((80, 24)).columns
        w = cls._visible_width(text)
        if w == 0:
            return 1
        return (w + cols - 1) // cols

    @classmethod
    def _render_hint(cls, hint):
        """渲染引用块提示，返回行数"""
        if not hint:
            return 0
        lines = hint.split('\n')
        for line in lines:
            sys.stdout.write(f"  {cls.DIM}\u2503 {line}{cls.NC}\n")
        return len(lines)

    # --- 交互组件 ---

    @classmethod
    def select_option(cls, prompt, options, default=0, hint=''):
        """箭头键选择器

        Args:
            prompt: 提示文字
            options: [(label, description), ...] 选项列表
            default: 默认选中索引
            hint: 可选的引用块提示文字

        Returns:
            选中的索引
        """
        current = default
        sys.stdout.write(f"\n{cls.CYAN}?{cls.NC} {cls.BOLD}{prompt}{cls.NC}\n")
        cls._render_hint(hint)
        sys.stdout.flush()

        with cls.hidden_cursor():
            while True:
                total_lines = 0
                for i, (label, desc) in enumerate(options):
                    if i == current:
                        line = f"  {cls.CYAN}>{cls.NC} {cls.BOLD}{label}{cls.NC}"
                    else:
                        line = f"    {cls.DIM}{label}{cls.NC}"
                    if desc:
                        line += f"  {cls.DIM}{desc}{cls.NC}"
                    sys.stdout.write(line + '\n')
                    total_lines += cls._visual_line_count(line)
                sys.stdout.flush()

                key = cls._read_key()
                if key == 'up' and current > 0:
                    current -= 1
                elif key == 'down' and current < len(options) - 1:
                    current += 1
                elif key == 'enter':
                    sys.stdout.write(f"\033[{total_lines}A\033[J")
                    sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {options[current][0]}\n")
                    sys.stdout.flush()
                    return current

                sys.stdout.write(f"\033[{total_lines}A\033[J")
                sys.stdout.flush()

    @classmethod
    def select_action_or_exit(cls, prompt, hint='', options=None, exit_index=-1):
        """通用操作选择器，支持退出选项

        Args:
            prompt: 提示文字
            hint: 可选的引用块提示文字
            options: [(label, desc), ...] 选项列表
            exit_index: 退出选项索引（默认 -1 即最后一个）

        Returns:
            选中的索引
        """
        options = options or []
        if exit_index < 0:
            exit_index = len(options) + exit_index
        idx = cls.select_option(prompt, options, hint=hint)
        if idx == exit_index:
            cls.print_warning("已取消")
            sys.exit(0)
        return idx

    @classmethod
    def select_multi(cls, prompt, options, default=None, validate=None):
        """多选选择器（空格切换选中，回车确认）

        Args:
            prompt: 提示文字
            options: [(label, description), ...] 选项列表
            default: 默认选中的索引列表，如 [0, 1]
            validate: 校验函数，接收选中索引列表，返回 None 通过，返回字符串为错误提示

        Returns:
            选中的索引列表
        """
        if default is None:
            default = []
        selected = set(default)
        current = 0
        error_msg = ''

        sys.stdout.write(f"\n{cls.CYAN}?{cls.NC} {cls.BOLD}{prompt}{cls.NC}\n")
        sys.stdout.flush()

        with cls.hidden_cursor():
            while True:
                total_lines = 0
                for i, (label, desc) in enumerate(options):
                    check = f"{cls.GREEN}✓{cls.NC}" if i in selected else " "
                    if i == current:
                        line = f"  {cls.CYAN}>{cls.NC} [{check}] {cls.BOLD}{label}{cls.NC}"
                    else:
                        line = f"    [{check}] {cls.DIM}{label}{cls.NC}"
                    if desc:
                        line += f"  {cls.DIM}{desc}{cls.NC}"
                    sys.stdout.write(line + '\n')
                    total_lines += cls._visual_line_count(line)
                if error_msg:
                    err_line = f"  {cls.YELLOW}\u26a0{cls.NC} {error_msg}"
                    sys.stdout.write(err_line + '\n')
                    total_lines += 1
                    error_msg = ''
                sys.stdout.flush()

                key = cls._read_key()
                if key == 'up' and current > 0:
                    current -= 1
                elif key == 'down' and current < len(options) - 1:
                    current += 1
                elif key == ' ':
                    if current in selected:
                        selected.discard(current)
                    else:
                        selected.add(current)
                elif key == 'enter':
                    result = sorted(selected)
                    if validate:
                        error_msg = validate(result) or ''
                    if not error_msg:
                        sys.stdout.write(f"\033[{total_lines}A\033[J")
                        labels = [options[i][0] for i in result]
                        sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {', '.join(labels)}\n")
                        sys.stdout.flush()
                        return result

                sys.stdout.write(f"\033[{total_lines}A\033[J")
                sys.stdout.flush()

    @classmethod
    def input_value(cls, prompt, required=False, secret=False, hint='', validate=None):
        """纯文本输入（无默认值）

        Args:
            prompt: 提示文字
            required: 是否必填
            secret: 是否隐藏输入
            hint: 可选的引用块提示文字
            validate: 校验函数，返回 None 通过，返回字符串为错误提示

        Returns:
            用户输入的值
        """
        suffix = "" if required else f" {cls.DIM}(可选，回车跳过){cls.NC}"

        sys.stdout.write(f"\n{cls.CYAN}?{cls.NC} {cls.BOLD}{prompt}{cls.NC}{suffix}\n")
        if hint:
            cls._render_hint(hint)
        sys.stdout.flush()

        error_msg = ""
        while True:
            sys.stdout.write(f"  {cls.CYAN}>{cls.NC} ")
            if error_msg:
                sys.stdout.write(f"\n  {cls.YELLOW}\u26a0{cls.NC} {error_msg}")
                sys.stdout.write(f"\033[1A\r\033[4C")
            sys.stdout.flush()

            value = cls._raw_input(mask=secret)
            value = value.strip()

            prev_had_error = bool(error_msg)
            error_msg = ""
            if required and not value:
                error_msg = "此项为必填"
            elif value and validate:
                error_msg = validate(value) or ""

            if error_msg:
                sys.stdout.write("\033[1A\033[J")
                continue

            sys.stdout.write("\033[1A\033[J")
            if value:
                result_display = value[:3] + '***' if secret else value
                sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {result_display}\n")
            else:
                sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {cls.DIM}(未配置){cls.NC}\n")
            sys.stdout.flush()
            return value

    @classmethod
    def input_or_keep(cls, prompt, existing='', required=False, secret=False, hint='', validate=None):
        """有已配置值时提供选择 + 内联输入

        Args:
            prompt: 提示文字
            existing: 已配置的值
            required: 是否必填
            secret: 是否隐藏显示
            hint: 可选的引用块提示文字
            validate: 校验函数

        Returns:
            用户最终确认的值
        """
        if not existing:
            return cls.input_value(prompt, required=required, secret=secret, hint=hint, validate=validate)

        display = existing[:3] + '***' if secret else existing
        current = 0
        eb = EditableBuffer()
        error_msg = ""

        sys.stdout.write(f"\n{cls.CYAN}?{cls.NC} {cls.BOLD}{prompt}{cls.NC}\n")
        cls._render_hint(hint)
        sys.stdout.flush()

        def _draw():
            lines = 0
            if current == 0:
                line1 = f"  {cls.CYAN}>{cls.NC} {cls.BOLD}{display}{cls.NC}  {cls.DIM}当前配置{cls.NC}"
                line2 = f"    {cls.DIM}自定义输入...{cls.NC}"
            else:
                line1 = f"    {cls.DIM}{display}{cls.NC}  {cls.DIM}当前配置{cls.NC}"
                line2 = f"  {cls.CYAN}>{cls.NC} {eb.display(mask=secret)}"
            sys.stdout.write(line1 + '\n')
            lines += cls._visual_line_count(line1)
            sys.stdout.write(line2 + '\n')
            lines += cls._visual_line_count(line2)
            if error_msg:
                err_line = f"  {cls.YELLOW}\u26a0{cls.NC} {error_msg}"
                sys.stdout.write(err_line + '\n')
                lines += cls._visual_line_count(err_line)
            sys.stdout.flush()
            return lines

        with cls.hidden_cursor():
            drawn_lines = _draw()

            while True:
                key = cls._read_key()

                if current == 0:
                    error_msg = ""
                    if key == 'down':
                        current = 1
                    elif key == 'enter':
                        sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                        sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {display}\n")
                        sys.stdout.flush()
                        return existing
                else:
                    if key == 'up':
                        eb.clear()
                        current = 0
                        error_msg = ""
                    elif key == 'enter':
                        value = eb.text.strip()
                        if required and not value:
                            error_msg = "此项为必填"
                            eb.clear()
                        elif value and validate:
                            err = validate(value)
                            if err:
                                error_msg = err
                                eb.clear()
                            else:
                                sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                                result_display = value[:3] + '***' if secret and value else value
                                sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {result_display}\n")
                                sys.stdout.flush()
                                return value
                        else:
                            sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                            result_display = value[:3] + '***' if secret and value else value
                            sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {result_display or cls.DIM + '(未配置)' + cls.NC}\n")
                            sys.stdout.flush()
                            return value
                    elif key == 'esc':
                        eb.clear()
                        current = 0
                        error_msg = ""
                    elif eb.handle_key(key):
                        error_msg = ""

                sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                drawn_lines = _draw()

    @classmethod
    def input_list(cls, prompt, existing=None, hint=''):
        """动态列表编辑器

        Args:
            prompt: 提示文字
            existing: 已有项列表
            hint: 可选的引用块提示文字

        Returns:
            编辑后的列表
        """
        items = list(existing) if existing else []
        current = len(items) + 1 if items else len(items)
        eb = EditableBuffer()
        editing = False

        sys.stdout.write(f"\n{cls.CYAN}?{cls.NC} {cls.BOLD}{prompt}{cls.NC}\n")
        cls._render_hint(hint)
        sys.stdout.flush()

        def _total_options():
            return len(items) + 2

        def _draw():
            lines = 0
            for i, item in enumerate(items):
                if i == current and not editing:
                    line = f"  {cls.CYAN}>{cls.NC} {cls.BOLD}{i + 1}. {item}{cls.NC}  {cls.DIM}Delete 删除{cls.NC}"
                else:
                    line = f"    {cls.DIM}{i + 1}. {item}{cls.NC}"
                sys.stdout.write(line + '\n')
                lines += cls._visual_line_count(line)
            add_idx = len(items)
            if current == add_idx:
                if editing:
                    line = f"  {cls.CYAN}>{cls.NC} + {eb.display()}"
                else:
                    line = f"  {cls.CYAN}>{cls.NC} {cls.BOLD}+ 添加新命令...{cls.NC}"
            else:
                line = f"    {cls.DIM}+ 添加新命令...{cls.NC}"
            sys.stdout.write(line + '\n')
            lines += cls._visual_line_count(line)
            done_idx = len(items) + 1
            if current == done_idx:
                line = f"  {cls.CYAN}>{cls.NC} {cls.GREEN}\u2713 完成{cls.NC}"
            else:
                line = f"    {cls.DIM}\u2713 完成{cls.NC}"
            sys.stdout.write(line + '\n')
            lines += cls._visual_line_count(line)
            sys.stdout.flush()
            return lines

        with cls.hidden_cursor():
            drawn_lines = _draw()

            while True:
                key = cls._read_key()

                if editing:
                    if key == 'enter':
                        value = eb.text.strip()
                        if value:
                            items.append(value)
                        eb.clear()
                        editing = False
                        current = len(items)
                    elif key == 'esc':
                        eb.clear()
                        editing = False
                    elif key in ('\x7f', '\x08') and not eb:
                        # 空 buf 退格 → 退出编辑模式
                        editing = False
                    else:
                        eb.handle_key(key)
                else:
                    if key == 'up' and current > 0:
                        current -= 1
                    elif key == 'down' and current < _total_options() - 1:
                        current += 1
                    elif key in ('\x7f', '\x08'):
                        if current < len(items):
                            items.pop(current)
                            if current >= len(items) and current > 0:
                                current = len(items) - 1 if items else 0
                    elif key == 'enter':
                        if current == len(items):
                            editing = True
                        elif current == len(items) + 1:
                            # 完成 — 检查重复
                            seen = set()
                            dupes = []
                            for item in items:
                                if item in seen and item not in dupes:
                                    dupes.append(item)
                                seen.add(item)
                            if dupes:
                                sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                                choice = cls.select_action_or_exit(
                                    f"检测到重复配置: {', '.join(dupes)}",
                                    options=[("自动去重", "保留第一个，移除重复项"),
                                             ("忽略", "保持重复继续"),
                                             ("取消", "退出初始化")])
                                if choice == 0:
                                    items = list(dict.fromkeys(items))
                            sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                            if items:
                                sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {', '.join(items)}\n")
                            else:
                                sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {cls.DIM}(未配置){cls.NC}\n")
                            sys.stdout.flush()
                            return items
                    elif len(key) == 1 and key.isprintable() and current == len(items):
                        editing = True
                        eb.clear()
                        eb.insert(key)

                sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                drawn_lines = _draw()

    @classmethod
    def review_settings(cls, prompt, items, hint=''):
        """预览+按需编辑的配置列表

        展示一组配置项及其当前值，光标默认在"无需修改，继续"上。
        用户可上下键选中配置项，回车后原地编辑。

        Args:
            prompt: 标题
            items: [(key, value, validate_fn_or_None, description?), ...] 配置项列表
                   description 可选，不传则不显示行内说明
            hint: 可选引用块提示

        Returns:
            Dict[str, str] — 编辑后的 {key: value} 字典
        """
        # 可变副本
        values = [item[1] for item in items]
        original_values = list(values)
        n_items = len(items)
        current = n_items  # 默认光标在"继续"上
        editing = False
        eb = EditableBuffer()
        error_msg = ""

        sys.stdout.write(f"\n{cls.CYAN}?{cls.NC} {cls.BOLD}{prompt}{cls.NC}\n")
        cls._render_hint(hint)
        sys.stdout.flush()

        def _draw():
            lines = 0
            for i in range(n_items):
                key = items[i][0]
                val = values[i]
                desc = items[i][3] if len(items[i]) > 3 else ''
                desc_suffix = f"  {cls.DIM}{desc}{cls.NC}" if desc else ''
                if i == current and editing:
                    line = f"  {cls.CYAN}>{cls.NC} {key} = {eb.display()}"
                elif i == current:
                    val_display = val if val else f"{cls.DIM}(未配置){cls.NC}"
                    line = f"  {cls.CYAN}>{cls.NC} {cls.BOLD}{key}{cls.NC} = {val_display}{desc_suffix}"
                else:
                    val_display = val if val else f"(未配置)"
                    line = f"    {cls.DIM}{key} = {val_display}{cls.NC}{desc_suffix}"
                sys.stdout.write(line + '\n')
                lines += cls._visual_line_count(line)
            # "继续" 选项（根据是否有修改切换文案）
            changed = values != original_values
            confirm_text = "确认，继续" if changed else "无需修改，继续"
            if current == n_items:
                line = f"  {cls.CYAN}>{cls.NC} {cls.BOLD}{confirm_text}{cls.NC}"
            else:
                line = f"    {cls.DIM}{confirm_text}{cls.NC}"
            sys.stdout.write(line + '\n')
            lines += cls._visual_line_count(line)
            if error_msg:
                line = f"  {cls.YELLOW}\u26a0{cls.NC} {error_msg}"
                sys.stdout.write(line + '\n')
                lines += cls._visual_line_count(line)
            sys.stdout.flush()
            return lines

        with cls.hidden_cursor():
            drawn_lines = _draw()

            while True:
                key = cls._read_key()

                if editing:
                    if key == 'enter':
                        value = eb.text.strip()
                        validate = items[current][2]
                        if value and validate:
                            err = validate(value)
                            if err:
                                error_msg = err
                                eb.clear()
                            else:
                                values[current] = value
                                editing = False
                                error_msg = ""
                        else:
                            values[current] = value
                            editing = False
                            error_msg = ""
                        eb.clear()
                    elif key == 'esc':
                        editing = False
                        error_msg = ""
                        eb.clear()
                    elif eb.handle_key(key):
                        error_msg = ""
                else:
                    if key == 'up' and current > 0:
                        current -= 1
                    elif key == 'down' and current < n_items:
                        current += 1
                    elif key == 'enter':
                        if current == n_items:
                            # "继续" — 输出最终结果并返回
                            sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                            for i in range(n_items):
                                k = items[i][0]
                                v = values[i]
                                v_display = v if v else f"{cls.DIM}(未配置){cls.NC}"
                                sys.stdout.write(f"  {cls.GREEN}>{cls.NC} {k} = {v_display}\n")
                            sys.stdout.flush()
                            return {items[i][0]: values[i] for i in range(n_items)}
                        else:
                            # 进入编辑模式
                            editing = True
                            error_msg = ""

                sys.stdout.write(f"\033[{drawn_lines}A\033[J")
                drawn_lines = _draw()


# =============================================================================
# EnvManager — .env 文件操作
# =============================================================================

class EnvManager:
    """管理 .env 文件的读写"""

    def __init__(self, env_file, env_example=''):
        self.env_file = env_file
        self.env_example = env_example
        self.content = ''

    def prepare(self):
        """准备 .env 文件：不存在则从 .env.example 复制"""
        if not os.path.exists(self.env_file):
            if self.env_example and os.path.exists(self.env_example):
                shutil.copy2(self.env_example, self.env_file)
                TerminalUI.print_info(f"已从 .env.example 创建 .env")
            else:
                with open(self.env_file, 'w') as f:
                    f.write('')

    def load(self):
        """加载 .env 文件内容"""
        with open(self.env_file, 'r', encoding='utf-8') as f:
            self.content = f.read()

    def save(self):
        """保存 .env 文件"""
        with open(self.env_file, 'w', encoding='utf-8') as f:
            f.write(self.content)

    def get(self, key):
        """读取配置值"""
        match = re.search(r'^' + re.escape(key) + r'=(.*)$', self.content, re.MULTILINE)
        if match:
            return match.group(1).strip().strip('"').strip("'")
        return ''

    def set(self, key, value):
        """更新配置值"""
        pattern = r'^(' + re.escape(key) + r')=.*$'
        replacement = key + '=' + value
        new_content, count = re.subn(pattern, replacement, self.content, flags=re.MULTILINE)
        if count == 0:
            new_content = self.content.rstrip('\n') + '\n\n' + replacement + '\n'
        self.content = new_content


# =============================================================================
# DependencyChecker — 依赖检测
# =============================================================================

class DependencyChecker:
    """检测运行环境依赖"""

    def __init__(self, python_path=''):
        self.python_path = python_path or sys.executable

    def check_all(self, enabled_agents: Optional[List[str]] = None):
        """检测所有依赖，返回是否全部满足"""
        TerminalUI.print_section("检测环境依赖")
        all_ok = True

        # Python 3
        try:
            ver = subprocess.check_output([self.python_path, '--version'],
                                          stderr=subprocess.STDOUT, universal_newlines=True).strip()
            TerminalUI.print_success(f"python3: {ver} ({self.python_path})")
        except Exception:
            TerminalUI.print_error("python3: 未找到")
            all_ok = False

        # 必需依赖
        for dep in ('curl', 'bash'):
            if shutil.which(dep):
                try:
                    ver = subprocess.check_output([dep, '--version'],
                                                  stderr=subprocess.STDOUT, universal_newlines=True).split('\n')[0]
                    TerminalUI.print_success(f"{dep}: {ver}")
                except Exception:
                    TerminalUI.print_success(f"{dep}: 已安装")
            else:
                TerminalUI.print_error(f"{dep}: 未安装")
                all_ok = False

        # 可选依赖
        optional = [('jq', '将使用 Python 作为降级方案'), ('socat', '将使用 Python socket_client')]
        for dep, fallback in optional:
            if shutil.which(dep):
                TerminalUI.print_success(f"{dep}: 已安装 (可选)")
            else:
                TerminalUI.print_info(f"{dep}: 未安装 (可选，{fallback})")

        # Agent CLI 检测
        self._check_agent_commands(enabled_agents or ['claude'])

        if all_ok:
            TerminalUI.print_text()
            TerminalUI.print_success("所有必需依赖已满足")
        else:
            TerminalUI.print_text()
            TerminalUI.print_error("缺少必需依赖，请安装后重试")

        return all_ok

    def run_in_user_shell(self, cmd, timeout=10):
        """通过用户的交互式 shell 执行命令

        与后端 build_shell_cmd (utils/shell.py) 使用相同的 shell 参数：
        zsh/bash 用 -ic，fish 用 -c，其他 POSIX 用 -lc。bash 用 -ic
        （interactive）是为了绕过 ~/.bashrc 的非交互早退保护，让用户自定义
        的函数/别名（如 claude 包装函数）能加载，须与 build_shell_cmd 同步。

        Args:
            cmd: 要执行的命令字符串
            timeout: 超时秒数

        Returns:
            subprocess.CompletedProcess 或 None（执行失败时）
        """
        user_shell = os.environ.get('SHELL', '/bin/bash')
        shell_name = os.path.basename(user_shell)

        if shell_name in ('zsh', 'bash'):
            shell_args = ['-ic']
        elif shell_name == 'fish':
            shell_args = ['-c']
        else:
            shell_args = ['-lc']

        try:
            # start_new_session 避免 bash/zsh -ic 抢占终端前台组、挂起 setup.sh init 交互菜单（SIGTTIN/SIGTTOU）
            return subprocess.run(
                [user_shell] + shell_args + [cmd],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=timeout,
                start_new_session=True)
        except Exception:
            return None

    def _check_agent_commands(self, enabled_agents: List[str]):
        """通过用户 shell 检测已启用 agent 的 CLI"""
        agents = {
            'claude': ('claude', 'https://claude.ai/code'),
            'codex': ('codex', 'https://github.com/openai/codex'),
        }
        for agent_type in enabled_agents:
            cmd, url = agents.get(agent_type, (agent_type, ''))
            result = self.run_in_user_shell(f'command -v {cmd}')
            if result is None:
                TerminalUI.print_warning(f"{cmd}: 检测超时")
                continue
            cmd_path = result.stdout.strip().split('\n')[-1] if result.returncode == 0 else ''
            if cmd_path:
                ver_result = self.run_in_user_shell(f'{cmd} --version')
                version = ver_result.stdout.strip().split('\n')[-1] if ver_result and ver_result.returncode == 0 else ''
                TerminalUI.print_success(f"{cmd}: {version or cmd_path}")
            else:
                TerminalUI.print_warning(f"{cmd}: 未找到")
                if url:
                    TerminalUI.print_dim(f"请确保已安装: {url}")

    def check_supports_print_flag(self, cmd):
        """检测命令是否支持 --print 参数

        通过用户 shell 执行 `cmd --print`，确保能检测到用户的别名/函数。
        根据错误信息判断：
        - stderr 含 "unknown option" 等关键词 → 不支持
        - exit code 127 → 命令未找到
        - 其他非零退出（如缺少参数）→ --print 被识别，返回 True

        Args:
            cmd: 要检测的命令（可以是命令名、路径或别名）

        Returns:
            True 表示支持 --print 参数，False 表示不支持或检测失败
        """
        result = self.run_in_user_shell(cmd + ' --print')
        if result is None:
            return False
        stderr_lower = (result.stderr or '').lower()
        if 'unknown option' in stderr_lower or 'unrecognized option' in stderr_lower \
                or 'invalid option' in stderr_lower or 'unknown flag' in stderr_lower \
                or 'no such option' in stderr_lower or 'unexpected argument' in stderr_lower:
            return False
        if result.returncode == 127:
            return False
        return True

    def check_supports_exec_subcommand(self, cmd):
        """检测命令是否支持 exec 子命令（Codex 非交互模式）

        通过用户 shell 执行 `cmd exec --help`，检查退出码和输出。

        Args:
            cmd: 要检测的命令

        Returns:
            True 表示支持 exec 子命令，False 表示不支持或检测失败
        """
        result = self.run_in_user_shell(cmd + ' exec --help')
        if result is None:
            return False
        if result.returncode == 127:
            return False
        # exec --help 成功返回帮助信息
        if result.returncode == 0:
            return True
        stderr_lower = (result.stderr or '').lower()
        stdout_lower = (result.stdout or '').lower()
        # 无此子命令的常见错误提示
        if 'unknown command' in stderr_lower or 'unknown command' in stdout_lower \
                or 'no such subcommand' in stderr_lower or 'unrecognized' in stderr_lower:
            return False
        return True

    def check_python_version(self, min_version):
        """检查 Python 版本是否 >= min_version (如 '3.8')"""
        major, minor = map(int, min_version.split('.'))
        return sys.version_info >= (major, minor)

    def check_lark_oapi(self):
        """检查 lark-oapi 是否可用"""
        try:
            import importlib.util
            return importlib.util.find_spec('lark_oapi') is not None
        except Exception:
            return False

    def install_lark_oapi(self):
        """安装 lark-oapi"""
        TerminalUI.print_info("正在安装 lark-oapi...")
        result = subprocess.run([self.python_path, '-m', 'pip', 'install', 'lark-oapi'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True)
        if result.returncode == 0:
            TerminalUI.print_success("lark-oapi 安装成功")
            return True
        else:
            TerminalUI.print_error("lark-oapi 安装失败")
            TerminalUI.print_dim(f"手动安装: {self.python_path} -m pip install lark-oapi")
            return False


# =============================================================================
# ClaudeHookConfigurator — settings.json Hook 配置
# =============================================================================

class ClaudeHookConfigurator:
    """管理 Claude Code Hook 配置"""

    def __init__(self, hook_path, settings_file):
        self.hook_path = hook_path
        self.settings_file = settings_file

    def configure(self, server_timeout=600):
        """写入/合并 hook 配置到 settings.json

        Args:
            server_timeout: PERMISSION_REQUEST_TIMEOUT 的值，用于计算 hook timeout
        """
        TerminalUI.print_section("配置 Claude Code Hook")

        if not os.path.exists(self.hook_path):
            TerminalUI.print_error(f"Hook 脚本不存在: {self.hook_path}")
            return False

        # 确保脚本可执行
        os.chmod(self.hook_path, 0o755)

        # 创建 settings 目录
        settings_dir = os.path.dirname(self.settings_file)
        os.makedirs(settings_dir, exist_ok=True)

        # hook timeout = 服务端超时 + 60s
        hook_timeout = server_timeout + 60

        # 期望的 hooks 配置
        desired_hooks = {
            "UserPromptSubmit": [{
                "hooks": [{"type": "command", "command": self.hook_path}]
            }],
            "PermissionRequest": [{
                "hooks": [{"type": "command", "command": self.hook_path, "timeout": hook_timeout}]
            }],
            "Stop": [{
                "hooks": [{"type": "command", "command": self.hook_path}]
            }]
        }

        # 读取现有配置
        config = {}
        try:
            with open(self.settings_file, 'r') as f:
                config = json.load(f)
        except Exception:
            pass

        if 'hooks' not in config:
            config['hooks'] = {}

        # 第一轮：检测状态，直接添加缺失的，展示结果
        TerminalUI.print_dim(f"配置文件: {self.settings_file}")
        changed = False
        status = {}     # event -> 'ok' | 'new' | 'conflict' | 'timeout'
        hook_refs = {}  # event -> 对应的 hook dict 引用（用于 timeout 原地更新）
        for event, desired in desired_hooks.items():
            if event not in config['hooks']:
                status[event] = 'new'
                config['hooks'][event] = desired
                changed = True
                TerminalUI.print_success(f"{event} \u2014 已添加")
            else:
                existing = config['hooks'][event]
                our_hook = None
                real_hook_path = os.path.realpath(self.hook_path)
                for entry in existing:
                    for hook in entry.get('hooks', []):
                        cmd = hook.get('command', '')
                        try:
                            tokens = shlex.split(cmd)
                        except ValueError:
                            tokens = cmd.split()
                        for token in tokens:
                            try:
                                if os.path.realpath(os.path.expanduser(token)) == real_hook_path:
                                    our_hook = hook
                                    break
                            except Exception:
                                pass
                        if our_hook:
                            break
                if our_hook:
                    # 已配置且指向我们的脚本，检查 PermissionRequest 的 timeout
                    if event == 'PermissionRequest' and our_hook.get('timeout', 0) != hook_timeout:
                        status[event] = 'timeout'
                        hook_refs[event] = our_hook
                        TerminalUI.print_warning(
                            f"{event} \u2014 超时配置不一致 "
                            f"(当前 {our_hook.get('timeout', 0)}s, 建议 {hook_timeout}s)")
                    else:
                        status[event] = 'ok'
                        TerminalUI.print_success(f"{event} \u2014 无需变更")
                else:
                    status[event] = 'conflict'
                    TerminalUI.print_warning(f"{event} \u2014 已有其他配置")

        # 第二轮：处理冲突和超时不一致
        for event, desired in desired_hooks.items():
            if status[event] == 'conflict':
                current_json = json.dumps(config['hooks'][event], ensure_ascii=False)
                new_json = json.dumps(desired, ensure_ascii=False)
                idx = TerminalUI.select_action_or_exit(
                    f"{event} 已有其他 hook 配置，是否覆盖？",
                    hint=f"当前: {current_json}\n新增: {new_json}",
                    options=[("覆盖", "替换为当前脚本"),
                             ("跳过", "保留现有配置"),
                             ("取消", "退出初始化")])
                if idx == 0:
                    config['hooks'][event] = desired
                    TerminalUI.print_success(f"已覆盖 {event} hook")
                    changed = True
                else:
                    TerminalUI.print_info(f"已跳过 {event} hook")
            elif status[event] == 'timeout':
                idx = TerminalUI.select_action_or_exit(
                    f"{event} hook 超时需要更新为 {hook_timeout}s，是否更新？",
                    hint=f"服务端超时: {server_timeout}s, Hook 超时建议: {server_timeout} + 60 = {hook_timeout}s",
                    options=[("更新", f"设置为 {hook_timeout}s"),
                             ("跳过", "保留当前超时配置"),
                             ("取消", "退出初始化")])
                if idx == 0:
                    # 只更新 timeout，不覆盖整个 hook 配置
                    hook_refs[event]['timeout'] = hook_timeout
                    TerminalUI.print_success(f"已更新 {event} hook 超时为 {hook_timeout}s")
                    changed = True
                else:
                    TerminalUI.print_info(f"已跳过 {event} hook 超时更新")

        if changed:
            with open(self.settings_file, 'w') as f:
                json.dump(config, f, indent=2)
                f.write('\n')

        return True


# =============================================================================
# CodexHookConfigurator — config.toml Hook 配置
# =============================================================================

class CodexHookConfigurator:
    """管理 Codex CLI Hook 配置（config.toml 格式）"""

    def __init__(self, hook_path, config_file):
        self.hook_path = hook_path
        self.config_file = config_file

    def configure(self, server_timeout=600):
        """写入/合并 hook 配置到 config.toml

        Args:
            server_timeout: PERMISSION_REQUEST_TIMEOUT 的值，用于计算 hook timeout
        """
        TerminalUI.print_section("配置 Codex CLI Hook")

        if not os.path.exists(self.hook_path):
            TerminalUI.print_error(f"Hook 脚本不存在: {self.hook_path}")
            return False

        os.chmod(self.hook_path, 0o755)

        config_dir = os.path.dirname(self.config_file)
        os.makedirs(config_dir, exist_ok=True)

        hook_timeout = server_timeout + 60

        # 读取现有配置（按行保留非 hook 段）
        existing_lines = []
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    existing_lines = f.readlines()
            except Exception:
                pass

        # 移除已有的 [[hooks.*]] 段，保留其他配置
        cleaned_lines = []
        in_hook_section = False
        for line in existing_lines:
            stripped = line.strip()
            if stripped.startswith('[[hooks.'):
                in_hook_section = True
                continue
            if in_hook_section:
                # 检测是否进入新的顶级段（非 hook 相关的缩进行或新的 [ 段头）
                if stripped.startswith('[') or stripped.startswith('[['):
                    in_hook_section = False
                    cleaned_lines.append(line)
                # 跳过 hook 段内的行（包括缩进的子段和空行）
                continue
            cleaned_lines.append(line)

        # 移除尾部多余空行
        while cleaned_lines and cleaned_lines[-1].strip() == '':
            cleaned_lines.pop()

        # 生成 hook 配置 TOML
        hook_toml = self._build_hook_toml(hook_timeout)

        # 合并写入
        with open(self.config_file, 'w') as f:
            if cleaned_lines:
                f.writelines(cleaned_lines)
                f.write('\n\n')
            f.write(hook_toml)

        TerminalUI.print_dim(f"配置文件: {self.config_file}")
        for event in ['UserPromptSubmit', 'PermissionRequest', 'Stop']:
            TerminalUI.print_success(f"{event} — 已配置")

        return True

    def _build_hook_toml(self, hook_timeout):
        """生成 hooks 段的 TOML 文本"""
        lines = []
        for event in ['UserPromptSubmit', 'Stop']:
            lines.append(f'[[hooks.{event}]]')
            lines.append('')
            lines.append(f'  [[hooks.{event}.hooks]]')
            lines.append(f'    type = "command"')
            lines.append(f'    command = "{self.hook_path}"')
            lines.append('')

        # PermissionRequest 带 timeout
        lines.append('[[hooks.PermissionRequest]]')
        lines.append('')
        lines.append('  [[hooks.PermissionRequest.hooks]]')
        lines.append(f'    type = "command"')
        lines.append(f'    command = "{self.hook_path}"')
        lines.append(f'    timeout = {hook_timeout}')
        lines.append('')

        return '\n'.join(lines)


# =============================================================================
# ServiceManager — 服务管理
# =============================================================================

class ServiceManager:
    """管理回调服务的启动/停止"""

    def __init__(self, source_dir):
        self.source_dir = source_dir
        self.start_script = os.path.join(source_dir, 'src', 'start-server.sh')

    def get_state(self):
        """获取服务运行状态，返回 dict"""
        try:
            result = subprocess.run([self.start_script, 'state'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except Exception:
            pass
        return {}

    def restart(self):
        """重启服务"""
        TerminalUI.print_section("启动服务")
        result = subprocess.run([self.start_script, 'restart'],
                                timeout=30)
        if result.returncode == 0:
            TerminalUI.print_success("服务启动成功")
            return True
        else:
            TerminalUI.print_error("服务启动失败，请检查上方错误信息")
            return False


# =============================================================================
# SetupInit — 主编排器
# =============================================================================

class SetupInit:
    """交互式初始化全流程编排"""

    def __init__(self, source_dir):
        self.source_dir = source_dir
        self.env = EnvManager(
            os.path.join(source_dir, '.env'),
            os.path.join(source_dir, '.env.example'))
        self.deps = DependencyChecker()
        self.hook_path = os.path.join(source_dir, 'src', 'hook-router.sh')
        self.enabled_agents = ['claude']
        self.service = ServiceManager(source_dir)
        # 运行状态
        self.deploy_idx = 0
        self.has_lark_oapi = self.deps.check_lark_oapi()

    def run(self):
        """执行完整初始化流程"""
        # 准备 .env
        self.env.prepare()
        self.env.load()

        # 获取服务运行状态
        state = self.service.get_state()
        self.running_port = str(state.get('port', ''))
        self.running_socket = state.get('socket_path', '')
        self.service_running = bool(state.get('pid'))

        # 标题横幅（特殊格式，不走 print_section）
        TerminalUI.print_banner("code-anywhere - 初始化配置")

        # 1-6: 配置 .env
        self._configure_feishu_connection()
        self._configure_owner_id()
        self._configure_callback_service()
        self._configure_session_mode()
        self._configure_agent_commands()
        self._configure_permission()

        # 确认写入
        TerminalUI.print_section("保存配置")
        TerminalUI.select_action_or_exit("是否将以上配置写入文件？", options=[
            ("确认写入", self.env.env_file),
            ("取消", "放弃所有配置并退出"),
        ])
        self.env.save()
        TerminalUI.print_success(f"配置已写入 {self.env.env_file}")

        TerminalUI.print_banner("环境部署")

        # 7: 依赖检测
        if not self.deps.check_all(self.enabled_agents):
            TerminalUI.print_error("依赖检测失败，请先安装缺失的依赖")
            sys.exit(1)

        # 8: lark-oapi 安装引导
        self._install_lark_oapi_if_needed()

        # 9: 持久化 Python 路径
        self.env.load()  # 重新加载（可能被其他步骤修改）
        self.env.set('PYTHON_PATH', sys.executable)
        self.env.save()

        # 10: Hook 配置（hook timeout 根据用户配置的 PERMISSION_REQUEST_TIMEOUT 动态计算）
        try:
            server_timeout = int(self.env.get('PERMISSION_REQUEST_TIMEOUT') or '600')
        except ValueError:
            server_timeout = 600
        for configurator in self._get_hook_configurators():
            configurator.configure(server_timeout=server_timeout)

        # 11: 启动服务
        self.service.restart()

        # 12: 完成提示
        TerminalUI.print_section("初始化完成")
        TerminalUI.print_success(f"{TerminalUI.BOLD}配置已完成{TerminalUI.NC}")
        TerminalUI.print_text()
        TerminalUI.print_text(f"配置文件: {self.env.env_file}")
        TerminalUI.print_text("服务管理: ./setup.sh start|stop|restart|status")
        TerminalUI.print_text()
        TerminalUI.print_dim("可通过 ./setup.sh init 重新配置，或直接编辑 .env 后 ./setup.sh restart")
        TerminalUI.print_text()

    def _get_hook_configurators(self):
        """根据 self.enabled_agents 返回对应的 hook 配置器列表"""
        configurators = []
        if 'claude' in self.enabled_agents:
            configurators.append(ClaudeHookConfigurator(
                self.hook_path,
                os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')))
        if 'codex' in self.enabled_agents:
            configurators.append(CodexHookConfigurator(
                self.hook_path,
                os.path.join(os.path.expanduser('~'), '.codex', 'config.toml')))
        if not configurators:
            configurators.append(ClaudeHookConfigurator(
                self.hook_path,
                os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')))
        return configurators

    # --- .env 配置步骤（从原 main() 迁移，逻辑不变） ---

    def _configure_feishu_connection(self):
        TerminalUI.print_section("飞书连接")
        existing_gateway = self.env.get('FEISHU_GATEWAY_URL')
        default_deploy = 1 if existing_gateway else 0

        self.deploy_idx = TerminalUI.select_option("选择部署模式", [
            ("单机部署", "本机配置飞书应用凭证"),
            ("分离部署", "通过网关连接，无需本机配置凭证"),
        ], default=default_deploy)

        self.env.set('FEISHU_SEND_MODE', 'openapi')

        if self.deploy_idx == 0:
            if self.env.get('FEISHU_GATEWAY_URL'):
                TerminalUI.select_action_or_exit("检测到已有分离部署配置，切换到单机模式需要清除 FEISHU_GATEWAY_URL", options=[
                    ("确认清除", ""), ("取消", "退出初始化")])
                self.env.set('FEISHU_GATEWAY_URL', '')
                TerminalUI.print_info("FEISHU_GATEWAY_URL 将被清除")

            app_id = TerminalUI.input_or_keep("FEISHU_APP_ID", existing=self.env.get('FEISHU_APP_ID'), required=True)
            app_secret = TerminalUI.input_or_keep("FEISHU_APP_SECRET", existing=self.env.get('FEISHU_APP_SECRET'),
                                               required=True, secret=True)
            self.env.set('FEISHU_APP_ID', app_id)
            self.env.set('FEISHU_APP_SECRET', app_secret)

            if not self.has_lark_oapi:
                token = TerminalUI.input_or_keep("FEISHU_VERIFICATION_TOKEN",
                                                 existing=self.env.get('FEISHU_VERIFICATION_TOKEN'), required=True,
                                                 secret=True,
                                                 hint="未检测到 lark-oapi 依赖，需使用 HTTP 回调模式\n从飞书开放平台 -> 事件与回调 -> 加密策略 获取")
                self.env.set('FEISHU_VERIFICATION_TOKEN', token)
            else:
                TerminalUI.print_success("已检测到 lark-oapi，将使用长连接模式（无需 Verification Token）")
        else:
            if self.env.get('FEISHU_APP_ID') or self.env.get('FEISHU_APP_SECRET'):
                TerminalUI.select_action_or_exit("检测到已有单机部署配置，切换到分离模式需要清除应用凭证", options=[
                    ("确认清除", "清除 APP_ID、APP_SECRET、VERIFICATION_TOKEN"), ("取消", "退出初始化")])
                self.env.set('FEISHU_APP_ID', '')
                self.env.set('FEISHU_APP_SECRET', '')
                self.env.set('FEISHU_VERIFICATION_TOKEN', '')
                TerminalUI.print_info("应用凭证配置将被清除")

            def _validate_gateway_url(v):
                if ' ' in v:
                    return "地址中不能包含空格"
                if not (v.startswith('http://') or v.startswith('https://')
                        or v.startswith('ws://') or v.startswith('wss://')):
                    return "请输入 http://、https://、ws:// 或 wss:// 开头的地址"
                return None

            gateway_url = TerminalUI.input_or_keep("FEISHU_GATEWAY_URL", existing=existing_gateway,
                                                   required=True, validate=_validate_gateway_url)
            self.env.set('FEISHU_GATEWAY_URL', gateway_url)

    def _configure_owner_id(self):
        TerminalUI.print_section("用户身份")

        def _validate_owner_id(v):
            if (v.startswith('ou_') or   # open_id
                v.startswith('oc_') or   # chat_id
                v.startswith('on_') or   # union_id
                '@' in v):               # email
                return "必须使用 user_id 格式（不能是 open_id/union_id/chat_id/email）"
            return None

        existing_owner = self.env.get('FEISHU_OWNER_ID')
        owner_hint = "飞书用户 ID（user_id 格式），获取方式见飞书开放平台文档"
        if self.deploy_idx == 0:
            owner_id = TerminalUI.input_or_keep("FEISHU_OWNER_ID", existing=existing_owner,
                                                required=False, hint=owner_hint, validate=_validate_owner_id)
            if not owner_id:
                TerminalUI.print_info("不配置则本服务仅作为纯网关服务")
        else:
            owner_id = TerminalUI.input_or_keep("FEISHU_OWNER_ID", existing=existing_owner,
                                                required=True, hint=owner_hint, validate=_validate_owner_id)
        self.env.set('FEISHU_OWNER_ID', owner_id)

    def _configure_callback_service(self):
        TerminalUI.print_section("回调服务")

        def _validate_port(v):
            try:
                p = int(v)
                if p < 1 or p > 65535:
                    raise ValueError
            except ValueError:
                return "请输入 1-65535 之间的端口号"
            return None

        existing_port = self.env.get('CALLBACK_SERVER_PORT') or '8080'
        while True:
            port = TerminalUI.input_or_keep("CALLBACK_SERVER_PORT", existing=existing_port, required=True,
                                            hint="本机监听端口，服务启动后占用此端口接收回调请求", validate=_validate_port)

            # 端口占用检测
            if self._check_port_in_use(port):
                if self.running_port == port:
                    break
                elif self.service_running and not self.running_port:
                    break
                else:
                    TerminalUI.print_warning(f"端口 {port} 当前已被占用")
                    choice = TerminalUI.select_option("端口已被占用，如何处理？", [
                        ("重新输入端口", "换一个未被占用的端口"),
                        ("继续使用", "启动服务前自行确保该端口可用"),
                    ])
                    if choice == 1:
                        break
                    existing_port = port
            else:
                TerminalUI.print_success(f"端口 {port} 当前可用")
                break
        self.env.set('CALLBACK_SERVER_PORT', port)

        # CALLBACK_SERVER_URL
        # 需要公网地址的场景：
        #   - 单机 + 无 lark-oapi（HTTP 回调模式）：用户可直接拿此地址配置飞书事件订阅
        #   - 分离 + HTTP 模式：网关需要回调 callback 服务
        needs_public_url = False
        if self.deploy_idx == 0 and not self.has_lark_oapi:
            needs_public_url = True
        elif self.deploy_idx == 1:
            actual_gateway = self.env.get('FEISHU_GATEWAY_URL')
            if actual_gateway and not actual_gateway.startswith('ws'):
                needs_public_url = True

        default_url = f"http://localhost:{port}"
        existing_url = self.env.get('CALLBACK_SERVER_URL')

        def _validate_url(v):
            if ' ' in v:
                return "地址中不能包含空格"
            if not v.startswith('http://') and not v.startswith('https://'):
                return "请输入 http:// 或 https:// 开头的地址"
            return None

        if needs_public_url:
            if self.deploy_idx == 0:
                url_hint = ("HTTP 回调模式需要服务地址可被公网访问\n"
                            "请填入公网可达地址，后续请使用该地址配置飞书开放平台的事件订阅地址")
            else:
                url_hint = ("分离部署的 HTTP 连接模式需要服务地址可被网关访问\n"
                            "请填入网关可达的地址")
            url = TerminalUI.input_or_keep("CALLBACK_SERVER_URL", existing=existing_url or default_url, required=True,
                                           hint=url_hint, validate=_validate_url)
            self.env.set('CALLBACK_SERVER_URL', url)
            if 'localhost' in url or '127.0.0.1' in url:
                TerminalUI.print_warning("当前模式需要公网可达地址，localhost 可能无法正常工作")
        elif existing_url and existing_url != default_url:
            url = TerminalUI.input_or_keep("CALLBACK_SERVER_URL", existing=existing_url, required=True,
                                           hint=f"当前模式下 localhost 即可，建议 {default_url}\n保持公网地址也不影响使用，但请确保端口与 CALLBACK_SERVER_PORT ({port}) 一致",
                                           validate=_validate_url)
            self.env.set('CALLBACK_SERVER_URL', url)
        else:
            self.env.set('CALLBACK_SERVER_URL', default_url)
            TerminalUI.print_success(f"CALLBACK_SERVER_URL={default_url}")

        # URL 端口一致性检查
        while True:
            final_url = self.env.get('CALLBACK_SERVER_URL')
            url_port_match = re.search(r':(\d+)(/|$)', final_url)
            if not url_port_match or url_port_match.group(1) == port:
                break
            TerminalUI.print_warning(f"CALLBACK_SERVER_URL 中的端口 ({url_port_match.group(1)}) "
                                     f"与 CALLBACK_SERVER_PORT ({port}) 不一致，可能无法正常工作")
            choice = TerminalUI.select_option("端口不一致，如何处理？", [
                ("重新输入 URL", "修改 CALLBACK_SERVER_URL 中的端口"),
                ("继续使用", "忽略端口不一致"),
            ])
            if choice == 1:
                break
            url = TerminalUI.input_or_keep("CALLBACK_SERVER_URL", existing=final_url, required=True,
                                           validate=_validate_url)
            self.env.set('CALLBACK_SERVER_URL', url)

        # PERMISSION_SOCKET_PATH 冲突检测
        sock_path = self.env.get('PERMISSION_SOCKET_PATH') or '/tmp/claude-permission.sock'
        is_own_socket = (self.running_socket == sock_path) or (self.service_running and not self.running_socket)
        if not is_own_socket and os.path.exists(sock_path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(sock_path)
                s.close()
                while True:
                    new_sock = TerminalUI.input_or_keep("PERMISSION_SOCKET_PATH", existing=sock_path, required=True,
                                                        hint=f"{sock_path} 已被其他服务监听\n同一台机器部署多个服务时需要不同的路径")
                    self.env.set('PERMISSION_SOCKET_PATH', new_sock)
                    if new_sock != sock_path:
                        break
                    choice = TerminalUI.select_action_or_exit("socket 路径未修改，启动后会与已运行的服务互相冲突", options=[
                        ("重新输入", "修改 socket 路径"), ("继续", "忽略冲突继续配置"), ("取消", "退出初始化")])
                    if choice != 0:
                        break
            except (socket.error, OSError):
                pass

    def _configure_session_mode(self):
        TerminalUI.print_section("会话模式")
        modes = ['message', 'thread', 'group']
        existing_mode = self.env.get('FEISHU_SESSION_MODE')
        default_mode = modes.index(existing_mode) if existing_mode in modes else 0

        mode_idx = TerminalUI.select_option("选择会话模式", [
            ("message", "普通模式，所有会话消息在同一个聊天窗口中"),
            ("thread", "话题模式，每个会话收敛到独立话题"),
            ("group", "群聊模式，每个会话自动创建独立群聊"),
        ], default=default_mode)

        selected_mode = modes[mode_idx]
        self.env.set('FEISHU_SESSION_MODE', selected_mode)

        if selected_mode in ('message', 'thread'):
            def _validate_chat_id(v):
                if ' ' in v:
                    return "群聊 ID 中不能包含空格"
                if not v.startswith('oc_'):
                    return "群聊 ID 应以 oc_ 开头"
                return None

            chat_id = TerminalUI.input_or_keep("FEISHU_CHAT_ID", existing=self.env.get('FEISHU_CHAT_ID'), required=False,
                                            hint="指定消息发送到某个群聊（oc_ 开头的群聊 ID）\n不配置则默认发送到用户与机器人的私聊会话中",
                                            validate=_validate_chat_id)
            self.env.set('FEISHU_CHAT_ID', chat_id)
        else:
            TerminalUI.print_warning(f"群聊模式需要飞书应用开通应用身份的 {TerminalUI.BOLD}im:chat{TerminalUI.NC} 权限")
            TerminalUI.print_dim("请确保已在飞书开放平台 -> 应用管理 -> 权限管理 中添加")

            def _validate_days(v):
                try:
                    if int(v) < 0:
                        raise ValueError
                except ValueError:
                    return "请输入非负整数"
                return None

            results = TerminalUI.review_settings("群聊配置", [
                ("FEISHU_GROUP_NAME_PREFIX", self.env.get('FEISHU_GROUP_NAME_PREFIX') or '', None,
                 "群聊名称前缀，完整格式: {前缀} - #{序号} - {目录名} - {YYYYMMDD}"),
                ("FEISHU_GROUP_DISSOLVE_DAYS", self.env.get('FEISHU_GROUP_DISSOLVE_DAYS') or '0', _validate_days,
                 "空闲自动解散天数，0 = 不自动解散"),
            ], hint="自动创建的群聊的命名和生命周期管理")
            self.env.set('FEISHU_GROUP_NAME_PREFIX', results['FEISHU_GROUP_NAME_PREFIX'])
            self.env.set('FEISHU_GROUP_DISSOLVE_DAYS', results['FEISHU_GROUP_DISSOLVE_DAYS'] or '0')

            existing_cowork = (self.env.get('FEISHU_GROUP_ALLOW_COWORK') or '').lower() in ('true', '1', 'yes')
            cowork_idx = TerminalUI.select_option("是否开启群聊协作模式？", [
                ("关闭", "仅会话创建者可在群内对话（默认）"),
                ("开启", "群内所有成员均可参与对话，消耗创建者额度"),
            ], default=1 if existing_cowork else 0)
            self.env.set('FEISHU_GROUP_ALLOW_COWORK', 'true' if cowork_idx == 1 else 'false')

    def _configure_agent_commands(self):
        TerminalUI.print_section("Agent 命令")
        TerminalUI.print_dim("本服务支持 Claude Code 和 Codex 两种 AI 编码代理。")
        TerminalUI.print_dim("请选择要启用的 Agent，然后为每个 Agent 配置命令。")
        TerminalUI.print_text()

        # 读取已有配置
        existing_enabled = self.env.get('ENABLED_AGENTS') or 'claude'
        enabled_list = [a.strip().lower() for a in existing_enabled.split(',') if a.strip()]

        # 选择启用的 Agent
        agent_options = [
            ("Claude Code", "Anthropic Claude CLI"),
            ("Codex", "OpenAI Codex CLI"),
        ]
        all_agents = ['claude', 'codex']
        default_selected = [i for i, a in enumerate(all_agents) if a in enabled_list]
        if not default_selected:
            default_selected = [0]

        selected = TerminalUI.select_multi("选择要启用的 Agent（空格切换，回车确认）",
                                           agent_options, default=default_selected,
                                           validate=lambda s: "请至少选择一个 Agent" if not s else None)

        enabled_agents = [all_agents[i] for i in selected]
        self.env.set('ENABLED_AGENTS', ','.join(enabled_agents))
        self.enabled_agents = enabled_agents

        # 为每个启用的 Agent 配置命令
        agent_configs = [
            ('claude', 'Claude Code', 'CLAUDE_COMMAND', 'claude', 'CLAUDE_ARGS_TEMPLATE'),
            ('codex', 'Codex', 'CODEX_COMMAND', 'codex', 'CODEX_ARGS_TEMPLATE'),
        ]
        for agent_type, display_name, env_key, default_cmd, template_key in agent_configs:
            if agent_type in enabled_agents:
                self._configure_single_agent_command(
                    agent_type, display_name, env_key, default_cmd, template_key)

        # 默认 Agent 选择
        if len(enabled_agents) > 1:
            existing_default = self.env.get('DEFAULT_AGENT') or enabled_agents[0]
            default_idx = enabled_agents.index(existing_default) if existing_default in enabled_agents else 0
            options = [(a.capitalize(), "") for a in enabled_agents]
            idx = TerminalUI.select_option("选择默认 Agent", options, default=default_idx,
                                           hint="用户未指定时使用的 Agent")
            self.env.set('DEFAULT_AGENT', enabled_agents[idx])
        else:
            self.env.set('DEFAULT_AGENT', enabled_agents[0])

    def _configure_single_agent_command(self, agent_type: str, display_name: str,
                                        env_key: str, default_cmd: str, template_key: str):
        """为单个 Agent 配置命令和参数模板

        Args:
            agent_type: agent 类型标识（如 'claude'、'codex'）
            display_name: 显示名称（如 'Claude Code'）
            env_key: 命令配置项（如 'CLAUDE_COMMAND'）
            default_cmd: 默认命令（如 'claude'）
            template_key: 模板配置项（如 'CLAUDE_ARGS_TEMPLATE'）
        """
        TerminalUI.print_text()
        TerminalUI.print_dim(f"── {display_name} 命令配置 ──")
        existing_cmd = self.env.get(env_key)
        has_custom_cmd = existing_cmd and existing_cmd.strip() not in (default_cmd, '')

        if not has_custom_cmd:
            custom_cmd_idx = TerminalUI.select_option(
                f"是否只通过默认的 {default_cmd} 命令启动会话", [
                    ("是", f"只使用默认的 {default_cmd} 命令"),
                    ("否", "使用第三方 CLI、别名或自定义命令"),
                ], hint="如需使用其他命令或配置多个命令切换，请选「否」")
        else:
            custom_cmd_idx = 1

        if custom_cmd_idx == 0:
            self.env.set(env_key, default_cmd)
            self.env.set(template_key, '{cmd} {args}')
            return

        existing_cmds = []
        if existing_cmd:
            stripped = existing_cmd.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                inner = stripped[1:-1]
                existing_cmds = [c.strip().strip('"').strip("'") for c in inner.split(',') if c.strip()]
            else:
                existing_cmds = [stripped]

        cmds = TerminalUI.input_list(env_key, existing=existing_cmds,
                                     hint="如使用第三方 CLI、别名或自定义环境变量封装，请按实际情况配置\n"
                                          "支持多个命令（默认使用第一个，/new 和 /reply 时可切换）")
        if cmds:
            if len(cmds) == 1:
                self.env.set(env_key, cmds[0])
            else:
                self.env.set(env_key, '[' + ', '.join(cmds) + ']')
        else:
            self.env.set(env_key, default_cmd)
            self.env.set(template_key, '{cmd} {args}')
            return

        # 按 agent 类型检测对应的执行模式
        # Claude: --print（非交互输出模式）
        # Codex: exec（非交互执行子命令）
        if agent_type == 'claude':
            check_fn = self.deps.check_supports_print_flag
            flag_name = '--print'
        else:
            check_fn = self.deps.check_supports_exec_subcommand
            flag_name = 'exec'

        TerminalUI.print_text()
        TerminalUI.print_dim("正在检测命令参数支持情况（需加载用户 shell 环境，请稍候）...")
        supported = []
        unsupported = []
        for cmd_item in cmds:
            if check_fn(cmd_item):
                supported.append(cmd_item)
            else:
                unsupported.append(cmd_item)

        if not unsupported:
            TerminalUI.print_success(f"所有命令均支持 {flag_name} 参数，将使用默认参数模板")
        else:
            for s in supported:
                TerminalUI.print_success(f"{s}: 支持 {flag_name}")
            for u in unsupported:
                TerminalUI.print_warning(f"{u}: 不支持 {flag_name}")

        if len(unsupported) == len(cmds):
            needs_template = True
        elif unsupported:
            choice = TerminalUI.select_option("命令参数模式不一致，如何处理？", [
                ("重新输入命令", "修改为参数模式一致的命令列表"),
                ("跳过", "保持当前配置，不配置参数模板（不支持的命令可能无法正常工作）"),
                ("取消", "退出初始化"),
            ])
            if choice == 0:
                self._configure_single_agent_command(
                    agent_type, display_name, env_key, default_cmd, template_key)
                return
            needs_template = False
        else:
            needs_template = False

        if needs_template:
            TerminalUI.print_dim(f"命令不支持 {flag_name} 参数，需要通过模板告诉系统如何传递参数")

            def _validate_template(v):
                if '{cmd}' not in v or '{args}' not in v:
                    return "模板必须同时包含 {cmd} 和 {args}"
                return None

            if agent_type == 'claude':
                example_args = '--print prompt --session-id xxx'
            else:
                example_args = 'exec --json --cd /path prompt'

            existing_template = self.env.get(template_key)
            template = TerminalUI.input_or_keep(
                template_key, existing=existing_template, required=False,
                validate=_validate_template,
                hint=f"命令行模板，默认 {{cmd}} {{args}}，其中:\n"
                     f"  {{cmd}} = {env_key} 的值，{{args}} = {example_args} 等参数\n"
                     f"  默认执行效果: {default_cmd} {example_args}\n"
                     f"仅当第三方 CLI 参数传递方式不同时才需修改")
            self.env.set(template_key, template or '{cmd} {args}')
        else:
            self.env.set(template_key, '{cmd} {args}')

    def _configure_permission(self):
        TerminalUI.print_section("权限请求")

        def _validate_positive_int(v):
            try:
                if int(v) <= 0:
                    raise ValueError
            except ValueError:
                return "请输入正整数"
            return None

        results = TerminalUI.review_settings("权限请求配置", [
            ("PERMISSION_REQUEST_TIMEOUT", self.env.get('PERMISSION_REQUEST_TIMEOUT') or '600', _validate_positive_int,
             "权限请求超时秒数，超时后回退到终端交互"),
        ], hint="Agent 触发权限请求时的超时行为（通知延迟请使用 /notify delay 配置）")
        self.env.set('PERMISSION_REQUEST_TIMEOUT', results['PERMISSION_REQUEST_TIMEOUT'] or '600')

    # --- 辅助方法 ---

    @staticmethod
    def _check_port_in_use(port):
        """检测端口是否被占用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(('127.0.0.1', int(port))) == 0
        except Exception:
            return False

    def _install_lark_oapi_if_needed(self):
        """单机模式 + 无 lark-oapi + 无 token 时引导安装"""
        if self.has_lark_oapi:
            return

        # 重新加载 .env 检查部署模式
        self.env.load()
        gateway = self.env.get('FEISHU_GATEWAY_URL')
        if gateway:
            return  # 分离部署不需要

        token = self.env.get('FEISHU_VERIFICATION_TOKEN')
        if token:
            return  # 有 token，用 HTTP 回调模式

        TerminalUI.print_section("安装 lark-oapi")
        TerminalUI.print_warning("单机模式未配置 Verification Token，需要安装 lark-oapi（长连接模式）")
        TerminalUI.print_text()

        if self.deps.check_python_version('3.8'):
            idx = TerminalUI.select_option("是否现在安装 lark-oapi？", [
                ("安装", "推荐，支持长连接模式，无需公网 IP"),
                ("跳过", "启动后可能无法接收飞书事件"),
            ])
            if idx == 0:
                self.deps.install_lark_oapi()
            else:
                TerminalUI.print_warning("跳过安装，启动后可能无法接收飞书事件")
                TerminalUI.print_dim(f"手动安装: {sys.executable} -m pip install lark-oapi")
        else:
            TerminalUI.print_warning("当前 Python 版本低于 3.8，无法安装 lark-oapi")
            TerminalUI.print_dim("请升级 Python 或在 .env 中配置 FEISHU_VERIFICATION_TOKEN")


# =============================================================================
# 入口
# =============================================================================

def main():
    Terminal.init()

    if len(sys.argv) < 2:
        print("用法: python3 setup_init.py <source_dir>", file=sys.stderr)
        sys.exit(1)

    source_dir = sys.argv[1]
    setup = SetupInit(source_dir)
    setup.run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        TerminalUI.print_text()
        TerminalUI.print_warning("已取消")
        sys.exit(1)
