"""
共享日志配置模块

从 shared/logging.json 读取统一日志配置
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

# 默认配置
DEFAULT_CONFIG = {
    "log_dir_relative": "log",
    "date_format": "%Y-%m-%d",
    "datetime_format": "%Y-%m-%d %H:%M:%S",
    "file_patterns": {
        "hook": "hook/{date}.log",
        "server": "callback/{date}.log",
        "socket_client": "socket_client/{date}.log",
        "feishu_message": "feishu_message/{date}.log",
        "feishu_longpoll": "feishu_longpoll/{date}.log"
    },
    "formats": {
        "python": "[%(process)d] %(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        "python_datefmt": "%Y-%m-%d %H:%M:%S",
        "feishu_message": "%(asctime)s.%(msecs)03d %(message)s"
    },
    "levels": {
        "default": "DEBUG",
        "feishu_message": "INFO"
    }
}

# 配置文件路径
CONFIG_FILE = Path(__file__).parent / "logging.json"


def get_logging_config() -> dict:
    """获取日志配置

    优先从 shared/logging.json 读取，如果不存在则使用默认配置

    Returns:
        日志配置字典
    """
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 合并默认配置（确保所有必需字段存在）
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                    elif isinstance(value, dict) and isinstance(config.get(key), dict):
                        for k, v in value.items():
                            if k not in config[key]:
                                config[key][k] = v
                return config
        except Exception:
            pass
    return DEFAULT_CONFIG


class DailyRotatingFileHandler(logging.FileHandler):
    """按天自动轮转的日志文件处理器

    每次写入日志时检查当前日期，如果日期变化则自动切换到新的日志文件。
    适用于长期运行的服务进程。
    """

    def __init__(self, log_dir, filename_pattern, date_format="%Y-%m-%d", **kwargs):
        # type: (str, str, str, **Any) -> None
        """初始化按天轮转处理器

        Args:
            log_dir: 日志目录路径
            filename_pattern: 文件名模式，包含 {date} 占位符
            date_format: 日期格式，默认 %Y-%m-%d
            **kwargs: 传递给 FileHandler 的其他参数
        """
        self._log_dir = log_dir
        self._filename_pattern = filename_pattern
        self._date_format = date_format
        self._current_date = time.strftime(date_format)
        filename = filename_pattern.replace("{date}", self._current_date)
        filepath = os.path.join(log_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        super(DailyRotatingFileHandler, self).__init__(filepath, **kwargs)

    def emit(self, record):
        """写入日志前检查日期是否变化，如需要则切换文件"""
        today = time.strftime(self._date_format)
        if today != self._current_date:
            self.acquire()
            try:
                # 双重检查：获取锁后再次确认日期确实变化
                if today != self._current_date:
                    self._current_date = today
                    # 关闭旧文件
                    if self.stream:
                        self.stream.close()
                        self.stream = None  # type: ignore[assignment]
                    # 切换到新文件
                    filename = self._filename_pattern.replace("{date}", today)
                    self.baseFilename = os.path.join(self._log_dir, filename)
                    os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
                    self.stream = self._open()
            finally:
                self.release()
        super(DailyRotatingFileHandler, self).emit(record)


def setup_logging(component, logger=None, console=True, propagate=True, encoding=None,
                  configure_root=False):
    # type: (str, Optional[logging.Logger], bool, bool, Optional[str], bool) -> logging.Logger
    """设置日志记录器

    根据 logging.json / DEFAULT_CONFIG 中的配置，为指定组件创建带按天轮转的 logger。
    支持组件级别的 format、level 覆盖。

    Args:
        component: 组件名称 (server, socket_client, feishu_message 等)
        logger: 可选的现有 logger，如果为 None 则创建新的
        console: 是否添加控制台处理器（默认 True）
        propagate: 是否传播到父 logger（默认 True）
        encoding: 日志文件编码（默认 None，即系统默认）
        configure_root: 是否同时将 handlers 配置到 root logger（默认 False）。
            主入口模块应设为 True，确保所有子模块的 logger 继承到统一的 handlers。

    Returns:
        配置好的 logger
    """
    config = get_logging_config()

    # 获取或创建 logger
    if logger is None:
        logger = logging.getLogger(component)

    # 设置日志级别
    levels = config.get("levels", {})
    level_name = levels.get(component, levels.get("default", "DEBUG"))
    level = getattr(logging, level_name.upper(), logging.DEBUG)
    logger.setLevel(level)
    logger.propagate = propagate

    # 日志格式：优先使用组件专属格式，其次使用通用 python 格式
    formats = config.get("formats", {})
    log_format = formats.get(component, formats.get("python", DEFAULT_CONFIG["formats"]["python"]))
    date_format = formats.get("python_datefmt", DEFAULT_CONFIG["formats"]["python_datefmt"])

    formatter = logging.Formatter(log_format, datefmt=date_format)

    # 按天轮转文件处理器
    project_root = str(Path(__file__).parent.parent.parent)
    log_dir = os.path.join(project_root, config.get("log_dir_relative", "log"))
    os.makedirs(log_dir, exist_ok=True)
    file_patterns = config.get("file_patterns", {})
    pattern = file_patterns.get(component, "{component}/{{date}}.log".format(component=component))
    file_handler = DailyRotatingFileHandler(
        log_dir, pattern,
        date_format=config.get("date_format", "%Y-%m-%d"),
        encoding=encoding
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 控制台处理器
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # 将 handlers 同步到 root logger，确保子模块 logger 继承
    if configure_root:
        root = logging.getLogger()
        root.setLevel(level)
        for handler in logger.handlers:
            root.addHandler(handler)
        # 组件自身不再向 root 传播，避免同一条消息被写两次
        logger.propagate = False

    return logger
