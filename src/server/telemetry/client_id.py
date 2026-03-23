"""客户端 ID 管理

生成并持久化唯一的客户端标识符，用于遥测统计。

存储位置：runtime/client_id
格式：UUID v4
"""

import logging
import os
import uuid
from typing import Optional

from telemetry.utils import get_project_root

logger = logging.getLogger(__name__)

# client_id 文件路径
_CLIENT_ID_FILE = os.path.join(get_project_root(), 'runtime', 'client_id')

# 缓存的客户端 ID
_cached_client_id: Optional[str] = None


def get_client_id() -> str:
    """获取客户端 ID

    如果不存在则自动生成并持久化。

    Returns:
        客户端 ID（UUID 格式）
    """
    global _cached_client_id

    if _cached_client_id:
        return _cached_client_id

    # 尝试从文件读取
    client_id_file = os.path.abspath(_CLIENT_ID_FILE)
    if os.path.exists(client_id_file):
        try:
            with open(client_id_file, 'r') as f:
                client_id = f.read().strip()
                if client_id:
                    _cached_client_id = client_id
                    logger.debug("[telemetry] Loaded client_id from file: %s", client_id)
                    return client_id
        except Exception as e:
            logger.warning("[telemetry] Failed to read client_id file: %s", e)

    # 生成新的客户端 ID
    client_id = str(uuid.uuid4())
    _cached_client_id = client_id

    # 持久化到文件
    try:
        os.makedirs(os.path.dirname(client_id_file), exist_ok=True)
        with open(client_id_file, 'w') as f:
            f.write(client_id)
        logger.info("[telemetry] Generated and saved client_id: %s", client_id)
    except Exception as e:
        logger.warning("[telemetry] Failed to save client_id file: %s", e)

    return client_id


def is_first_run() -> bool:
    """判断是否为首次运行（client_id 文件不存在）

    Returns:
        True 如果 client_id 文件不存在
    """
    return not os.path.exists(_CLIENT_ID_FILE)
