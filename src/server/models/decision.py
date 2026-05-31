"""
Decision Model - Agent Hook 决策常量和工厂方法

参考: https://docs.anthropic.com/en/docs/claude-code/hooks
"""
from typing import Any, Dict


class Decision:
    """决策行为常量和工厂方法"""

    # 决策行为
    ALLOW = 'allow'      # 允许执行
    DENY = 'deny'        # 拒绝执行

    # 决策 JSON 字段名
    FIELD_BEHAVIOR = 'behavior'
    FIELD_MESSAGE = 'message'
    FIELD_INTERRUPT = 'interrupt'
    FIELD_UPDATED_INPUT = 'updated_input'
    FIELD_UPDATED_PERMISSIONS = 'updated_permissions'

    @classmethod
    def allow(cls) -> dict:
        """返回允许决策"""
        return {cls.FIELD_BEHAVIOR: cls.ALLOW}

    @classmethod
    def allow_with_updated_input(cls, updated_input: Dict[str, Any]) -> dict:
        """返回带 updatedInput 的允许决策（用于 AskUserQuestion）

        Args:
            updated_input: 包含 answers 和 questions 的字典

        Returns:
            决策字典
        """
        return {
            cls.FIELD_BEHAVIOR: cls.ALLOW,
            cls.FIELD_UPDATED_INPUT: updated_input
        }

    @classmethod
    def allow_with_updated_permissions(cls, updated_permissions: list) -> dict:
        """返回带 updatedPermissions 的允许决策（用于始终允许）"""
        return {
            cls.FIELD_BEHAVIOR: cls.ALLOW,
            cls.FIELD_UPDATED_PERMISSIONS: updated_permissions
        }

    @classmethod
    def deny(cls, message: str = '', interrupt: bool = False) -> dict:
        """返回拒绝决策

        Args:
            message: 拒绝原因说明
            interrupt: 是否中断 Claude 当前任务

        Returns:
            决策字典
        """
        result = {cls.FIELD_BEHAVIOR: cls.DENY}
        if message:
            result[cls.FIELD_MESSAGE] = message
        if interrupt:
            result[cls.FIELD_INTERRUPT] = True
        return result
