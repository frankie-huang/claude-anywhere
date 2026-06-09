# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## 项目概述

code-anywhere 是一个飞书(Lark)通知与交互系统，为 Claude Code 和 Codex 等 AI 编程 Agent 提供：
- 通过飞书卡片按钮审批/拒绝文件操作（替代终端交互）
- 在飞书中回复消息继续 Agent 会话
- 多 Agent 支持（Claude Code + Codex），可扩展
- 支持 Webhook（已废弃）和 OpenAPI 两种飞书连接模式
- 支持单机和分离（WS 隧道/HTTP）部署

## 架构概览

系统分三层：

### Shell Hook 层（轻量，最少依赖）

```
Agent 触发 Hook 事件 → src/hook-router.sh（统一入口）
  ├─ UserPromptSubmit → src/hooks/user_prompt.sh  （用户 prompt 同步到飞书）
  ├─ PermissionRequest → src/hooks/permission.sh   （权限审批，通过 Unix Socket 与 Server 通信）
  └─ Stop             → src/hooks/stop.sh          （任务完成通知）
```

共享库在 `src/lib/`：`core.sh`（路径/配置/日志）、`json.sh`（JSON 解析，jq>python3>grep 降级）、`feishu.sh`（卡片构建与发送）、`socket.sh`（Unix Socket 通信，4 字节长度前缀协议）、`tool.sh`/`tool-config.sh`（工具详情格式化）。

### Python Server 层（回调服务）

`src/server/main.py` 启动 HTTP + Unix Socket 双协议服务器（ThreadedHTTPServer）。

**路由结构**（`handlers/http_handler.py` 分发）：
- `/gw/*` — 网关侧接口（注册、消息发送、群聊创建）
- `/cb/*` — 回调侧接口（决策下发、会话管理、Agent 启动/继续）
- GET `/allow|always|deny|interrupt` — 权限决策回调
- GET `/ws/tunnel` — WebSocket 隧道入口

**核心服务**（`services/` 下单例）：
- `request_manager.py` — 待处理权限请求注册表（request_id → socket 连接）
- `feishu_api.py` — 飞书 API 封装（token 管理、消息发送、敏感信息脱敏）
- `session_facade.py` — 会话操作网关
- `binding_store.py` — owner_id → callback_url 映射（网关侧）
- `ws_tunnel_client.py` — WS 隧道客户端（callback 主动连网关）

### Agent 适配层（策略模式）

`src/server/agents/__init__.py` 定义 `AgentAdapter` 基类，`agents/claude.py` 和 `agents/codex.py` 分别实现。通过 `get_agent_adapter(agent_type)` 工厂获取实例。适配器负责构建命令行、解析斜杠命令、处理权限持久化差异。

### 飞书卡片模板系统

`src/templates/feishu/` 下存放模块化的 JSON 模板，由 `src/lib/feishu.sh` 中的 `build_*_card()` 函数加载并填充变量。主要模板：
- `permission-card.json` — 权限审批卡片（含 4 个决策按钮）
- `stop-card.json` — 任务完成通知卡片
- `notification-card.json` — 通用通知卡片
- `buttons.json` / `buttons-openapi.json` — 按钮组件（Webhook/OpenAPI 各一套）
- `command-detail-*.json` — 工具详情展示组件（bash/edit/write/file）

### 关键数据流：权限审批

```
1. Agent 触发 PermissionRequest → hook-router.sh → permission.sh
2. permission.sh 通过 Unix Socket 发送请求到 Server
3. Server 发 ACK + 推送飞书卡片（延迟 PERMISSION_NOTIFY_DELAY 秒）
4. 用户点飞书按钮 → Server 收到决策 → 通过 Socket 发回 permission.sh
5. permission.sh 返回决策给 Agent（exit code: 0=成功, 1=回退终端, 2=错误）
```

### 关键数据流：会话继续（OpenAPI 模式）

```
1. 用户在飞书回复完成通知消息
2. im.message.receive_v1 事件 → feishu.py → MessageSessionStore 查找 session_id
3. 路由到 agent.py → 使用 AgentAdapter 构建 `claude --resume SESSION_ID` 命令
4. 后台监控 Agent 完成 → 发送新的完成通知
```

## 常用命令

### 服务管理

```bash
./setup.sh init                    # 交互式初始化（生成 .env，配置 hooks）
./setup.sh start                   # 启动服务
./setup.sh stop                    # 停止服务
./setup.sh restart                 # 重启服务（代码修改后需执行）
./setup.sh status                  # 查看服务状态
./setup.sh state                   # JSON 格式输出状态（供脚本使用）
./setup.sh update                  # 拉取最新代码并重启
```

### 安装与卸载

```bash
./install.sh                       # 交互式安装
./install.sh --check               # 仅检查依赖（不写入）
./install.sh --uninstall           # 卸载 hooks 并清理
./install.sh --clean-cache         # 清理 Python __pycache__
```

### 测试

```bash
./test/test-permission-quick.sh                        # 交互式菜单测试（推荐）
./test/test-permission.sh                              # 默认 Bash 工具测试
./test/test-permission.sh bash "git push"              # 测试指定 Bash 命令
./test/test-permission.sh edit "/etc/hosts"            # 测试文件编辑
./test/test-permission.sh write "/tmp/f.txt" "content" # 测试文件写入
```

测试前需先启动服务（`./setup.sh start`）。详细测试场景见 `test/SCENARIOS.md`。

### 直接管理服务进程

```bash
./src/start-server.sh start|stop|restart|status|state
```

## 开发工作流

### 修改后生效方式

- **Shell 脚本修改**：立即生效（下次 Hook 触发时使用新代码）
- **Python 代码修改**：需执行 `./setup.sh restart` 重启回调服务
- **`.env` 配置修改**：Shell 部分立即生效，Python 部分需重启服务
- **飞书卡片模板修改**：Shell 侧立即生效（`src/lib/feishu.sh` 每次加载模板文件）

### 日志与调试

日志目录 `log/`，按组件分类，每日自动轮转：

| 目录/文件 | 内容 |
|-----------|------|
| `log/hook/` | Shell Hook 脚本日志（permission、stop、user_prompt） |
| `log/callback/` | HTTP 回调服务日志 |
| `log/command/` | Agent 命令执行日志 |
| `log/feishu_message/` | 飞书消息事件处理日志 |
| `log/feishu_longpoll/` | 飞书 WebSocket 长连接日志 |
| `log/socket_client/` | Socket 客户端通信日志 |
| `log/permission_mcp/` | MCP 权限审批日志 |

调试技巧：
- 权限流程问题：先查 `log/hook/`（Shell 侧），再查 `log/callback/`（Server 侧）
- 飞书消息问题：查 `log/feishu_message/`
- 连接问题：查 `log/socket_client/` 和 `log/feishu_longpoll/`

### Commit 约定

- Commit message 使用**中文**
- 格式：`<type>: <description>`
  - `feat` 新功能 / `fix` 修复 / `refactor` 重构 / `docs` 文档 / `test` 测试
- 示例：`feat: 添加飞书卡片模板系统`

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
- **per-user 配置**：每个用户独立设置，需要通过注册流程写入 `BindingStore`，网关根据用户的 binding 读取（如 `session_mode`、`group_allow_cowork`、`group_dissolve_days`）

**判断标准**：如果不同用户可能需要不同的值，就是 per-user 配置。

per-user 配置的完整链路（以 `group_allow_cowork` 为例），新增只需改 3 处：
1. `register.py:extract_binding_params()` — 从请求数据中提取字段（唯一入口）
2. `binding_store.py:upsert()` — 存储到 binding
3. `config.py` — 定义全局默认值（callback 端注册时使用）

注册时 `main.py` / `auto_register.py` 组装 `binding_params` dict 传给网关，网关侧 `register.py` 和 `ws_handler.py` 通过 `extract_binding_params()` 统一提取，`binding_store.upsert()` 统一存储。读取时 `feishu.py` 从 `binding.get('xxx')` 读取（而非全局 config）。

### 网关侧接口鉴权

网关暴露的飞书相关接口（卡片回调、消息事件等）必须做好 owner 鉴权，确保用户只能操作自己的资源：

- **权限决策**：验证 `owner_id` 与请求中的用户身份一致，防止他人代点审批按钮
- **群聊管理**：群聊创建、解散、成员变更等操作需验证请求者是群聊 owner
- **消息路由**：转发消息到 callback 时需确认目标 session 属于该用户
- **信息查询**：群聊信息、会话状态等查询需限定在 owner 自己的数据范围内

原则：不信任飞书事件中的用户身份，始终与 `BindingStore` 中的 `owner_id` 比对。

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

## 依赖

- Python 3.6+（仅标准库，可选 `lark-oapi` 用于长连接模式）
- Bash 4.0+、curl
- 可选：jq（JSON 解析加速）、socat（Socket 通信）

## 提交前检查清单

**每次 commit 前必须执行以下检查：**

- [ ] 如果有功能改动（新功能、Bug 修复、重构、配置变更等） → **必须**更新 `CHANGELOG.md`
- [ ] 如果有配置/目录结构变更 → 检查 README.md 是否需要同步更新

> 纯代码格式、注释修改等不影响功能的变更无需更新 CHANGELOG。
