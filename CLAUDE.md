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
| 字符串格式化 | f-string `f"hello {name}"` | `.format()` / `%` 格式化 |

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

### 新增配置项的归属判断

新增配置项时，必须先判断该配置是**全局配置**还是 **per-user 配置**：

- **全局配置**：所有用户共享，只在 `config.py` 中读取（如 `FEISHU_APP_ID`、`CALLBACK_SERVER_PORT`）
- **per-user 配置**：每个用户独立设置，需要通过注册流程写入 `BindingStore`，网关根据用户的 binding 读取（如 `session_mode`、`at_bot_only`、`group_dissolve_days`）

**判断标准**：如果不同用户可能需要不同的值，就是 per-user 配置。

per-user 配置的完整链路（以 `at_bot_only` 为例）：
1. `config.py` — 定义全局默认值
2. `auto_register.py` / `ws_tunnel_client.py` — callback 端注册时传给网关
3. `register.py` — HTTP 注册入口提取并透传
4. `ws_handler.py` — WS 注册入口提取并透传
5. `binding_store.py` — 存储到 binding
6. `feishu.py` — 从 `binding.get('xxx')` 读取（而非全局 config）

**检查方法**：透传完成后，用已有的同类参数（如 `group_dissolve_days`）全局 grep，确认每个出现位置都有对应的新参数。注意不要遗漏 `feishu.py` 中的卡片回调入口（`handle_card_action_register`）。

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

## 提交前检查清单

**每次 commit 前必须执行以下检查：**

- [ ] 如果有功能改动（新功能、Bug 修复、重构、配置变更等） → **必须**更新 `CHANGELOG.md`
- [ ] 如果有配置/目录结构变更 → 检查 README.md 是否需要同步更新

> 纯代码格式、注释修改等不影响功能的变更无需更新 CHANGELOG。

## 常用命令

### 重启后端服务

```bash
./setup.sh restart
```

用于重启 Python 后端服务（callback server），修改代码后需要执行此命令使更改生效。