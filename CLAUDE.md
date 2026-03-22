<!-- OPENSPEC:START -->
# OpenSpec Instructions

These instructions are for AI assistants working in this project.

Always open `@/openspec/AGENTS.md` when the request:
- Mentions planning or proposals (words like proposal, spec, change, plan)
- Introduces new capabilities, breaking changes, architecture shifts, or big performance/security work
- Sounds ambiguous and you need the authoritative spec before coding

Use `@/openspec/AGENTS.md` to learn:
- How to create and apply change proposals
- Spec format and conventions
- Project structure and guidelines

Keep this managed block so 'openspec update' can refresh the instructions.

<!-- OPENSPEC:END -->

## 开发注意事项

### Python 版本要求

本项目 Python 代码需兼容 **Python 3.6+**，编写代码时注意：

| 特性 | Python 3.6+ 兼容写法 | 不要使用 |
|------|---------------------|----------|
| 类型注解 | `from typing import Dict, List, Tuple`<br>`Dict[str, Any]`<br>`Tuple[bool, dict]` | 小写内置泛型 `dict[str, Any]` (3.9+) |
| 空泛型 | `Optional[Dict[str, Any]]` | `Optional[Dict]` (需要完整参数) |
| 联合类型 | `Optional[int]`<br>`Union[int, None]`<br>`Union[str, int]` | `int \| None` (3.10+) |
| subprocess | `stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True` | `capture_output=True, text=True` (3.7+) |
| 运算符 | 普通赋值 `x = foo()` | `:=` walrus (3.8+) |
| 类型注解风格 | 内联注解 `def foo(x: str) -> int:` | `# type:` 注释风格 |

**常见错误示例：**

```python
# ❌ 错误 - Python 3.6 不支持
def foo() -> Optional[Dict]:  # 空泛型
    pass
result = subprocess.run(cmd, capture_output=True, text=True)
if (n := len(data)) > 0:  # walrus
    pass
x: int | None = None  # 联合类型语法
def foo(name, count=0):  # type: (str, int) -> bool  # 注释风格

# ✅ 正确 - Python 3.6 兼容
from typing import Optional, Dict, Any, Union
def foo() -> Optional[Dict[str, Any]]:
    pass
result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
n = len(data)
if n > 0:
    pass
x: Optional[int] = None  # 或 Union[int, None]
def foo(name: str, count: int = 0) -> bool:  # 内联注解
    pass
```

### 跨平台兼容性（macOS + Linux）

所有 Shell 脚本必须同时兼容 macOS 和 Linux。常见兼容性问题：

| 问题 | Linux | macOS | 兼容写法 |
|------|-------|-------|----------|
| 超时命令 | `timeout 1 cmd` | 无 `timeout` | 使用工具内置超时，如 `socat -T 1` |
| `readlink -f` | 支持 | 12.3 前不支持 | `readlink -f ... 2>/dev/null \|\| realpath ... \|\| echo "$HOME"` |
| `stat` 获取大小 | `stat -c%s` | `stat -f%z` | `stat -c%s ... 2>/dev/null \|\| stat -f%z ...` |
| `sed -i` | `sed -i 's/...//'` | `sed -i '' 's/...//'` | 避免使用，或创建临时文件 |
| `grep -P` | 支持 | 不支持 | 使用 `grep -E` 或 awk/sed |
| `/proc/` 路径 | 可用 | 不存在 | 使用 `uuidgen` 替代 `/proc/sys/kernel/random/uuid` |
| 包管理器 | apt/yum | brew | install.sh 中同时支持 |

**原则**：修改 Shell 脚本时，始终考虑两种系统的兼容性。

### 环境变量读取方式

**Bash 脚本中读取配置：**

使用 `src/lib/core.sh` 提供的 `get_config` 函数：

```bash
# 引入 core.sh（如果尚未引入）
if ! type get_config &> /dev/null; then
    source "${BASH_SOURCE[0]%/*}/core.sh"
fi

# 读取配置，第二个参数为默认值
WEBHOOK_URL=$(get_config "FEISHU_WEBHOOK_URL" "")
PORT=$(get_config "CALLBACK_SERVER_PORT" "8080")
```

优先级：`.env` 文件 > 环境变量 > 默认值

**禁止直接使用 `${VAR:-default}` 方式读取**，因为这种方式无法从 `.env` 文件读取。

**Python 中读取配置：**

使用 `server/config.py` 提供的 `get_config` 和 `get_config_int` 函数：

```python
from config import get_config, get_config_int

WEBHOOK_URL = get_config('FEISHU_WEBHOOK_URL', '')
PORT = get_config_int('CALLBACK_SERVER_PORT', 8080)
```

## 常用命令

### 重启后端服务

```bash
./setup.sh restart
```

用于重启 Python 后端服务（callback server），修改代码后需要执行此命令使更改生效。