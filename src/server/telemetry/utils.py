"""遥测工具函数

提供版本号获取、项目根目录定位、版本解析比较等通用功能。
"""

import logging
import os
import re
import subprocess
import uuid
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 缓存
_version_cache: Optional[str] = None
_repo_url_cache: Optional[str] = None

# 预编译的版本号正则（git describe 格式）
_VERSION_PATTERN = re.compile(
    r'v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(\d+)-g[0-9a-f]+)?$'
)

# 提取 git remote URL 中的 user/repo 路径
# 匹配：https://host/path, ssh://host/path, git@host:path
_REPO_PATH_PATTERN = re.compile(
    r'(?:(?:https?|ssh|git)://[^/]+/|[^@]+@[^:]+:)(.+?)(?:\.git)?$'
)


def get_project_root() -> str:
    """获取项目根目录

    从本文件位置向上推导：telemetry -> server -> src -> project_root

    Returns:
        项目根目录绝对路径
    """
    telemetry_dir = os.path.dirname(os.path.abspath(__file__))
    server_dir = os.path.dirname(telemetry_dir)
    src_dir = os.path.dirname(server_dir)
    return os.path.dirname(src_dir)


def get_repo_url() -> str:
    """获取 git 远程仓库地址（仅路径部分，保护隐私）

    通过 git remote get-url origin 获取后，只保留路径部分：
    - https://github.com/user/repo.git → user/repo
    - git@github.com:user/repo.git → user/repo
    - /path/to/local/repo → (空字符串，本地仓库不上报)

    Returns:
        仓库路径（如 "user/repo"）。获取失败或本地仓库返回空字符串
    """
    global _repo_url_cache

    if _repo_url_cache is not None:
        return _repo_url_cache

    project_root = get_project_root()

    try:
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=project_root
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            if url:
                # 提取路径部分（保护隐私，去掉域名和协议）
                # https://github.com/user/repo.git -> user/repo
                # ssh://git@github.com:2222/user/repo.git -> user/repo
                # git@github.com:user/repo.git -> user/repo
                # /local/path/repo -> ''（本地仓库不上报）
                match = _REPO_PATH_PATTERN.match(url)
                _repo_url_cache = match.group(1) if match else ''
                return _repo_url_cache
    except Exception:
        pass

    _repo_url_cache = ''
    return _repo_url_cache


def get_version() -> str:
    """获取项目版本号

    通过 git describe 获取版本标识，格式如：
    - v1.0.0（恰好在 tag 上）
    - v1.0.0-3-gabcdef（tag 之后第 3 个 commit）

    Returns:
        版本号字符串。获取失败返回 'unknown'
    """
    global _version_cache

    if _version_cache is not None:
        return _version_cache

    project_root = get_project_root()

    try:
        result = subprocess.run(
            ['git', 'describe', '--tags', '--always'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=project_root
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            if version:
                _version_cache = version
                return _version_cache
    except Exception:
        pass

    _version_cache = 'unknown'
    return _version_cache


def validate_uuid_v4(client_id: str) -> bool:
    """验证是否为有效的 UUID v4 格式

    Args:
        client_id: 待验证的客户端 ID

    Returns:
        True 如果是有效的 UUID v4 格式
    """
    try:
        parsed = uuid.UUID(client_id, version=4)
        # 确保字符串形式一致（避免类似格式绕过）
        return str(parsed) == client_id.lower()
    except (ValueError, AttributeError):
        return False


def parse_version(version_str: str) -> Optional[Tuple[int, ...]]:
    """解析版本字符串

    支持 git describe 格式：
    - 'v1.0.0' / '1.0.0' → (1, 0, 0, 0)
    - 'v1.0.0-3-gabcdef' → (1, 0, 0, 3)  （tag 之后第 3 个 commit）
    - '1' / 'v1.2' → (1, 0, 0, 0) / (1, 2, 0, 0)  （省略部分自动补零）

    Returns:
        (major, minor, patch, commits_ahead) 元组，解析失败返回 None
    """
    match = _VERSION_PATTERN.match(version_str.strip())
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    patch = int(match.group(3)) if match.group(3) else 0
    ahead = int(match.group(4)) if match.group(4) else 0

    return (major, minor, patch, ahead)


def is_version_newer(new_version: str, old_version: str) -> bool:
    """比较两个版本号，判断 new_version 是否比 old_version 新"""
    new_parsed = parse_version(new_version)
    old_parsed = parse_version(old_version)

    if new_parsed is None or old_parsed is None:
        # 解析失败时保守返回 False（不提示更新），避免字符串比较误判
        # 例如 "9.0.0" > "10.0.0" 在字符串比较中为 True（逐字符 ASCII 比较）
        return False

    return new_parsed > old_parsed
