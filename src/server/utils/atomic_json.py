"""原子 JSON 读写工具（进程内、无状态、不持锁）

用途：
    给 JSON 文件持久化的 store 提供统一的"安全加载 + 原子写入"，消除各 store
    中逐字重复的 _load/_save 模板，并修复两个一致性缺陷：
    1. 加载时校验顶层为 dict，文件被外部篡改成数组/字符串时返回 default 而非崩溃
    2. 写入异常时清理临时 .tmp 文件，避免残留

约束：
    - 纯函数，零状态，不引入任何锁/线程。并发由调用方自行持锁。
    - 仅处理顶层为 dict 的 JSON（本项目所有 store 的数据结构均为 dict）。
"""

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def atomic_load_json(file_path: str, default: Optional[Dict[str, Any]] = None,
                     tag: str = 'json') -> Dict[str, Any]:
    """从文件加载 JSON dict，任何异常或类型不符时返回 default

    Args:
        file_path: JSON 文件路径
        default: 加载失败时返回的默认值（None 时返回新的空 dict）
        tag: 日志前缀标识（如 'binding-store'）

    Returns:
        加载到的 dict；文件不存在/损坏/顶层非 dict 时返回 default
    """
    if default is None:
        default = {}

    if not os.path.exists(file_path):
        return default

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning("[%s] Invalid store data format (not a dict), starting fresh", tag)
        return default
    except json.JSONDecodeError:
        logger.warning("[%s] Invalid JSON in %s, starting fresh", tag, file_path)
        return default
    except (IOError, OSError) as e:
        logger.error("[%s] Failed to load %s: %s", tag, file_path, e)
        return default


def atomic_write_json(file_path: str, data: Dict[str, Any],
                      data_dir: str, tag: str = 'json') -> bool:
    """原子写入 JSON dict 到文件（先写临时文件再 os.replace）

    Args:
        file_path: 目标文件路径
        data: 要写入的数据字典
        data_dir: 临时文件所在目录（须与 file_path 同一文件系统以保证 rename 原子）
        tag: 日志前缀标识

    Returns:
        是否写入成功；失败时清理临时文件
    """
    tmp_path = None
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix='.tmp')
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, file_path)
        return True
    except (IOError, OSError) as e:
        logger.error("[%s] Failed to save %s: %s", tag, file_path, e)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False
