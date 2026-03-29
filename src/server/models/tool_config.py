"""
工具配置统一管理

从 config/tools.json 读取工具类型配置，消除与 Shell 脚本的配置重复
"""

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# 配置文件路径
TOOLS_CONFIG_FILE = Path(__file__).parent.parent.parent / "config" / "tools.json"


class ToolConfig:
    """工具配置数据类"""

    def __init__(
        self,
        name: str,
        display_name: str,
        color: str,
        icon: str,
        input_field: str,
        detail_template: str,
        rule_template: str,
        limit_length: Optional[int] = None,
        truncate_suffix: str = "..."
    ):
        self.name = name
        self.display_name = display_name
        self.color = color
        self.icon = icon
        self.input_field = input_field
        self.detail_template = detail_template
        self.rule_template = rule_template
        self.limit_length = limit_length
        self.truncate_suffix = truncate_suffix

    def format_detail(self, tool_input: Dict[str, Any], description: str = "") -> str:
        """格式化工具详情

        注意：此方法现在只返回纯值，不包含标签。
        标签由飞书卡片的子模板处理。

        Args:
            tool_input: 工具输入参数
            description: 可选的描述信息

        Returns:
            格式化后的详情内容（纯值）
        """
        # 提取字段值
        field_value = tool_input.get(self.input_field, "")

        # 应用长度限制
        if self.limit_length and len(str(field_value)) > self.limit_length:
            field_value = str(field_value)[:self.limit_length] + self.truncate_suffix

        # 基本转义（Bash 命令）
        if self.name == "Bash":
            field_value = field_value.replace("\\", "\\\\").replace('"', '\\"')

        # 替换模板占位符
        result = self.detail_template
        result = result.replace(f"{{{self.input_field}}}", str(field_value))
        result = result.replace("{tool_name}", self.name)

        # 如果有描述，直接附加（不添加标签）
        if description:
            result += f" {description}"

        return result

    def format_rule(self, tool_input: Dict[str, Any]) -> str:
        """格式化权限规则

        Args:
            tool_input: 工具输入参数

        Returns:
            规则字符串（如 Bash(npm install)）
        """
        field_value = tool_input.get(self.input_field, "")

        # 空值时使用 * 作为通配符
        if not field_value:
            field_value = "*"

        result = self.rule_template
        result = result.replace(f"{{{self.input_field}}}", str(field_value))
        result = result.replace("{tool_name}", self.name)

        return result


class ToolConfigManager:
    """工具配置管理器"""

    def __init__(self):
        self._configs: Dict[str, ToolConfig] = {}
        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        try:
            with open(TOOLS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            tools_data = data.get("tools", {})

            for tool_name, config in tools_data.items():
                self._configs[tool_name] = ToolConfig(
                    name=config.get("name", tool_name),
                    display_name=config.get("display_name", tool_name),
                    color=config.get("color", "grey"),
                    icon=config.get("icon", ""),
                    input_field=config.get("input_field", ""),
                    detail_template=config.get("detail_template", ""),
                    rule_template=config.get("rule_template", ""),
                    limit_length=config.get("limit_length"),
                    truncate_suffix=config.get("truncate_suffix", "...")
                )

            logger.info(f"Loaded {len(self._configs)} tool configurations from {TOOLS_CONFIG_FILE}")

        except FileNotFoundError:
            logger.warning(f"Tools config not found: {TOOLS_CONFIG_FILE}, using built-in config")
            self._load_builtin_config()
        except Exception as e:
            logger.error(f"Failed to load tools config: {e}, using built-in config")
            self._load_builtin_config()

    def _load_builtin_config(self):
        """加载内置配置（降级方案）"""
        builtin_tools = {
            "Bash": {
                "name": "Bash",
                "display_name": "命令执行",
                "color": "orange",
                "icon": "terminal",
                "input_field": "command",
                "detail_template": "{command}",
                "limit_length": 5000,
                "rule_template": "Bash({command})",
                "truncate_suffix": "..."
            },
            "Edit": {
                "name": "Edit",
                "display_name": "编辑文件",
                "color": "yellow",
                "icon": "edit",
                "input_field": "file_path",
                "detail_template": "{file_path}",
                "rule_template": "Edit({file_path})"
            },
            "Write": {
                "name": "Write",
                "display_name": "写入文件",
                "color": "yellow",
                "icon": "write",
                "input_field": "file_path",
                "detail_template": "{file_path}",
                "rule_template": "Write({file_path})"
            },
            "Read": {
                "name": "Read",
                "display_name": "读取文件",
                "color": "blue",
                "icon": "read",
                "input_field": "file_path",
                "detail_template": "{file_path}",
                "rule_template": "Read({file_path})"
            },
            "Glob": {
                "name": "Glob",
                "display_name": "文件搜索",
                "color": "blue",
                "icon": "search",
                "input_field": "pattern",
                "detail_template": "{pattern}",
                "rule_template": "Glob({pattern})"
            },
            "Grep": {
                "name": "Grep",
                "display_name": "内容搜索",
                "color": "blue",
                "icon": "search",
                "input_field": "pattern",
                "detail_template": "{pattern}",
                "rule_template": "Grep({pattern})"
            },
            "WebSearch": {
                "name": "WebSearch",
                "display_name": "网络搜索",
                "color": "purple",
                "icon": "web",
                "input_field": "query",
                "detail_template": "{query}",
                "limit_length": 5000,
                "truncate_suffix": "...",
                "rule_template": "WebSearch({query})"
            },
            "WebFetch": {
                "name": "WebFetch",
                "display_name": "获取网页",
                "color": "purple",
                "icon": "web",
                "input_field": "url",
                "detail_template": "{url}",
                "limit_length": 5000,
                "truncate_suffix": "...",
                "rule_template": "WebFetch({url})"
            },
            "Skill": {
                "name": "Skill",
                "display_name": "技能调用",
                "color": "green",
                "icon": "skill",
                "input_field": "skill",
                "detail_template": "",
                "rule_template": "Skill({skill})"
            }
        }

        for tool_name, config in builtin_tools.items():
            self._configs[tool_name] = ToolConfig(**config)

        logger.info(f"Loaded {len(self._configs)} built-in tool configurations")

    def get_config(self, tool_name: str) -> ToolConfig:
        """获取工具配置

        Args:
            tool_name: 工具名称

        Returns:
            工具配置对象，如果不存在则返回默认配置
        """
        if tool_name in self._configs:
            return self._configs[tool_name]

        # 返回默认配置
        return ToolConfig(
            name=tool_name,
            display_name=tool_name,
            color="grey",
            icon="",
            input_field="",
            detail_template="{tool_name}",
            rule_template=tool_name if tool_name.startswith("mcp__") else f"{tool_name}(*)"
        )

    def get_color(self, tool_name: str) -> str:
        """获取工具颜色"""
        return self.get_config(tool_name).color

    def format_rule(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """格式化权限规则"""
        return self.get_config(tool_name).format_rule(tool_input)


# 全局单例
_tool_config_manager: Optional[ToolConfigManager] = None
_tool_config_lock = threading.Lock()


def get_tool_config_manager() -> ToolConfigManager:
    """获取工具配置管理器单例（线程安全）"""
    global _tool_config_manager
    if _tool_config_manager is None:
        with _tool_config_lock:
            if _tool_config_manager is None:  # 双重检查
                _tool_config_manager = ToolConfigManager()
    return _tool_config_manager
