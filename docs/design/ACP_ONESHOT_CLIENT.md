# ACP Oneshot Client 设计方案

> 状态：**已废弃** — 已改用 `--permission-prompt-tool` MCP 方案，见 [PERMISSION_PROMPT_TOOL.md](./PERMISSION_PROMPT_TOOL.md)
> 前置调研：[INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md](./INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md)

基于 claude-agent-acp 实现「一次性 prompt 调用 + 权限审批」能力，复用现有 permission hook 机制。

---

## 1. 背景与动机

### 现有痛点

在 [交互式 Claude 会话调研](./INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md) 中确定了 `claude -p` (subprocess + headless) 方案，但该模式是非交互式的受限模式：
- 遇到需要权限的工具调用时，直接跳过/拒绝
- 不触发 PermissionRequest hook
- 不会阻塞等待用户决策

这导致无法在自动化/CI/远程场景下完成需要权限审批的任务。

### 目标

提供一个命令行工具，实现：
1. 一次性 prompt 执行（类似 `claude -p`）
2. 遇到权限请求时，通过现有飞书审批系统获取用户决策
3. 支持会话恢复/续接（`--resume`）
4. 对现有 hooks 代码零侵入

---

## 2. 架构概览

```
┌──────────────────────────────────────────────────────────────┐
│                     claude-oneshot (新增)                      │
│                                                               │
│  CLI 入口，负责：                                              │
│  1. 解析命令行参数                                             │
│  2. 管理 claude-agent-acp 子进程                               │
│  3. ACP 协议通信（ndjson over stdin/stdout）                   │
│  4. 权限请求桥接到现有飞书审批系统                               │
│  5. 输出最终结果                                               │
└───────────┬──────────────────────────────┬────────────────────┘
            │ stdin/stdout (ndjson)         │ HTTP / Unix Socket
            ▼                              ▼
┌───────────────────────┐    ┌──────────────────────────────────┐
│  claude-agent-acp     │    │  现有 callback server            │
│  (ACP 服务端子进程)    │    │  /tmp/claude-permission.sock     │
│                       │    │  飞书卡片发送 + 决策回调           │
│  - Claude Agent SDK   │    │                                  │
│  - 工具执行           │    │  （无需任何改动）                  │
│  - 权限请求 → ACP协议  │    │                                  │
└───────────────────────┘    └──────────────────────────────────┘
```

---

## 3. ACP 协议交互流程

### 3.1 完整消息序列

```
claude-oneshot                          claude-agent-acp
     │                                        │
     │──── InitializeRequest ────────────────→│
     │←─── InitializeResponse ───────────────│
     │                                        │
     │──── session/new ──────────────────────→│  (或 session/resume)
     │←─── NewSessionResponse ───────────────│
     │                                        │
     │──── session/prompt ───────────────────→│
     │←─── SessionNotification (chunks) ─────│  (流式输出)
     │←─── SessionNotification (tool_call) ──│
     │                                        │
     │←─── request_permission ───────────────│  ← 权限请求！
     │                                        │
     │  [桥接到飞书审批，等待用户点击]           │
     │                                        │
     │──── permission response ──────────────→│  → 返回决策
     │                                        │
     │←─── SessionNotification (tool_result) ─│
     │←─── SessionNotification (chunks) ─────│
     │←─── PromptResponse (end_turn) ────────│
     │                                        │
     │  [输出结果，退出]                        │
```

### 3.2 关键消息格式

#### InitializeRequest
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-01-01",
    "clientInfo": {
      "name": "claude-oneshot",
      "version": "1.0.0"
    },
    "capabilities": {}
  }
}
```

#### NewSessionRequest
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/new",
  "params": {
    "cwd": "/path/to/project"
  }
}
```

#### 恢复会话
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "unstable/resumeSession",
  "params": {
    "sessionId": "之前的 session_id"
  }
}
```

#### PromptRequest
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "session/prompt",
  "params": {
    "sessionId": "sess_xxx",
    "prompt": [
      {"type": "text", "text": "用户的 prompt 内容"}
    ]
  }
}
```

#### Permission Request (Agent → Client)
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "session/requestPermission",
  "params": {
    "sessionId": "sess_xxx",
    "toolCall": {
      "toolCallId": "tool_001",
      "rawInput": {"command": "npm test", "path": "/project"}
    },
    "options": [
      {"kind": "allow_always", "name": "Always Allow", "optionId": "allow_always"},
      {"kind": "allow_once",   "name": "Allow",        "optionId": "allow_once"},
      {"kind": "reject_once",  "name": "Reject",       "optionId": "reject_once"}
    ]
  }
}
```

#### Permission Response (Client → Agent)
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "outcome": "selected",
    "optionId": "allow_once"
  }
}
```

---

## 4. 权限请求桥接设计

### 4.1 桥接流程

收到 ACP 的 `requestPermission` 后：

```
requestPermission 消息
    ↓
提取 toolCall 信息（tool_name, command, path 等）
    ↓
构造请求数据，调用 callback server HTTP API
    POST /api/permission/request
    {
      "request_id": "<random_32_chars>",
      "tool_name": "Bash",
      "command": "npm test",
      "project": "/path/to/project",
      "session_id": "sess_xxx"
    }
    ↓
callback server:
  - 发送飞书交互卡片
  - 注册 socket 等待
    ↓
用户在飞书点击按钮
    ↓
callback server 返回决策:
  {"action": "allow"} / {"action": "always"} / {"action": "deny"}
    ↓
映射为 ACP 响应:
  allow   → {"optionId": "allow_once"}
  always  → {"optionId": "allow_always"}
  deny    → {"optionId": "reject_once"}
    ↓
返回给 claude-agent-acp
```

### 4.2 与现有系统的对接方式

有两种桥接策略，推荐方案 A：

#### 方案 A：通过 callback server HTTP API（推荐）

直接调用现有 callback server 暴露的 HTTP 接口：
- 发送权限请求：`POST /api/permission/request`
- 等待决策结果：通过 socket 或 HTTP long-poll

**优点**：完全解耦，不依赖 shell 脚本，对现有代码零侵入。
**前提**：callback server 需要暴露一个接收权限请求的 HTTP 端点（如果尚未提供，需新增一个薄接口）。

#### 方案 B：直接复用 shell 函数

在 ACP 客户端中 spawn 调用现有的 `permission.sh`，通过 stdin 传入模拟的 hook 事件 JSON：

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"npm test"},...}' | \
  bash /path/to/hooks/src/hooks/permission.sh
```

**优点**：完全复用现有逻辑，连飞书卡片格式都一致。
**缺点**：依赖 shell 脚本的 stdin 格式不变。

---

## 5. 命令行接口设计

```bash
# 基本用法：一次性执行
claude-oneshot -p "重构 auth 模块"

# 指定工作目录
claude-oneshot -p "修复 bug" --cwd /path/to/project

# 恢复已有会话
claude-oneshot --resume <session_id> -p "继续补充测试"

# 分叉会话
claude-oneshot --fork <session_id> -p "试另一种方案"

# 自动批准所有权限（危险模式）
claude-oneshot -p "部署服务" --auto-approve

# 输出 session_id 供后续复用
claude-oneshot -p "开始任务" --print-session-id

# 指定模型
claude-oneshot -p "简单任务" --model claude-sonnet-4-20250514

# 超时设置（秒）
claude-oneshot -p "长任务" --timeout 3600
```

### 参数列表

| 参数 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `--prompt` | `-p` | Prompt 内容 | 必填 |
| `--cwd` | | 工作目录 | 当前目录 |
| `--resume` | `-r` | 恢复指定会话 | - |
| `--fork` | | 分叉指定会话 | - |
| `--model` | `-m` | 使用的模型 | claude-opus-4-6-1m |
| `--auto-approve` | | 自动批准所有权限 | false |
| `--print-session-id` | | 结束时打印 session_id | false |
| `--timeout` | `-t` | 整体超时时间（秒） | 1800 |
| `--quiet` | `-q` | 只输出最终结果 | false |
| `--verbose` | `-v` | 输出详细日志 | false |

---

## 6. 实现语言选择

### 推荐：Python

理由：
- 与现有 callback server（Python）同栈，维护一致
- subprocess + 管道通信简洁
- json 处理原生支持
- 可直接 import callback server 的模块（如果需要）

### 备选：Node.js / Shell

- Node.js：与 claude-agent-acp 同栈，可直接引用 ACP SDK 的类型定义
- Shell：最轻量，但 ndjson 解析不便

---

## 7. 核心模块划分

```
src/
└── oneshot/
    ├── cli.py              # 命令行参数解析，入口
    ├── acp_client.py       # ACP 协议通信（ndjson 读写、JSON-RPC）
    ├── permission_bridge.py # 权限请求桥接（调 callback server）
    └── output.py           # 结果格式化输出
```

### 7.1 acp_client.py（~100 行）

```python
class AcpClient:
    """管理 claude-agent-acp 子进程和 ndjson 通信"""

    def start(self, env: dict) -> None:
        """启动 claude-agent-acp 子进程"""

    def send(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 请求，等待同 id 的响应"""

    def read_notifications(self) -> Iterator[dict]:
        """持续读取 ndjson 流中的通知消息"""

    def handle_server_request(self, msg: dict) -> None:
        """处理服务端发来的请求（如 requestPermission）"""

    def close(self) -> None:
        """终止子进程"""
```

### 7.2 permission_bridge.py（~50 行）

```python
class PermissionBridge:
    """将 ACP 权限请求桥接到现有飞书审批系统"""

    def request_permission(self, tool_call: dict, options: list) -> dict:
        """
        1. 构造请求数据
        2. 调用 callback server API 发送飞书卡片
        3. 等待决策结果
        4. 映射为 ACP 响应格式
        """

    def _map_decision(self, action: str) -> str:
        """
        allow   → allow_once
        always  → allow_always
        deny    → reject_once
        """
```

### 7.3 cli.py（~50 行）

```python
def main():
    args = parse_args()

    client = AcpClient()
    bridge = PermissionBridge(callback_server_url=args.callback_url)

    client.start(env={"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]})
    client.initialize()

    if args.resume:
        session = client.resume_session(args.resume)
    else:
        session = client.new_session(cwd=args.cwd)

    client.prompt(session_id=session["sessionId"], text=args.prompt)

    for msg in client.read_notifications():
        if msg.get("method") == "session/requestPermission":
            decision = bridge.request_permission(
                tool_call=msg["params"]["toolCall"],
                options=msg["params"]["options"]
            )
            client.send_response(msg["id"], decision)
        elif is_text_chunk(msg):
            print_chunk(msg)  # 流式输出
        elif is_prompt_response(msg):
            break

    print_session_id(session["sessionId"])
    client.close()
```

---

## 8. 对现有代码的改动评估

| 组件 | 改动 | 说明 |
|------|------|------|
| `src/hooks/permission.sh` | **无** | 不涉及 |
| `src/hooks/webhook.sh` | **无** | 不涉及 |
| `src/hook-router.sh` | **无** | 不涉及 |
| `src/server/main.py` | **无或极小** | 如果已有 HTTP API 可调用则无需改动；若需新增权限请求端点，约 20 行 |
| `src/server/services/` | **无** | 复用现有 request_manager、feishu_api |
| `src/templates/feishu/` | **无** | 复用现有卡片模板 |
| 飞书卡片回调流程 | **无** | callback server 处理不变 |

**新增文件**：
- `src/oneshot/cli.py` (~50 行)
- `src/oneshot/acp_client.py` (~100 行)
- `src/oneshot/permission_bridge.py` (~50 行)
- `src/oneshot/output.py` (~30 行)

**总计**：~230 行新代码，0-20 行现有代码改动。

---

## 9. 依赖

| 依赖 | 说明 | 新增？ |
|------|------|--------|
| `claude-agent-acp` | ACP 服务端，npm 全局安装 | **是** |
| `ANTHROPIC_API_KEY` | Claude API 密钥 | 已有 |
| callback server 运行中 | 飞书卡片发送 + 决策回调 | 已有 |
| Python 3.8+ | 客户端运行环境 | 已有 |

无需额外 Python 包，标准库即可（`subprocess`, `json`, `socket`, `argparse`）。

---

## 10. 风险与注意事项

### 10.1 ACP 协议稳定性
- `unstable_resumeSession` / `unstable_forkSession` 标记为不稳定 API
- `@agentclientprotocol/sdk` 版本 0.15.0，尚未 1.0
- **缓解**：锁定 claude-agent-acp 版本，升级时回归测试

### 10.2 ndjson 协议细节
- 需要正确处理：请求/响应匹配（通过 JSON-RPC id）、通知消息（无 id）、服务端主动请求（有 id 需回复）
- **缓解**：实现中区分三类消息，分别处理

### 10.3 超时处理
- ACP 服务端在等待权限响应时会无限阻塞
- 需要在客户端实现超时机制（默认 30 分钟）
- **缓解**：超时后发送 cancel 请求并退出

### 10.4 进程生命周期
- claude-agent-acp 子进程需要正确清理（SIGTERM）
- 客户端异常退出时避免僵尸进程
- **缓解**：使用 atexit / signal handler 确保清理

---

## 11. 后续扩展

- **批量模式**：从文件读取多个 prompt，顺序执行
- **CI 集成**：作为 CI step 运行，权限审批通过飞书完成
- **webhook 通知**：任务完成后发送飞书通知（复用现有 webhook.sh）
- **日志持久化**：记录每次执行的 session_id、prompt、结果
