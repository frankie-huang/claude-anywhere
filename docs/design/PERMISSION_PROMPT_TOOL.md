# Permission Prompt Tool 方案设计

> 日期：2026-03-15
> 状态：**已实现**
> 前置调研：[INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md](./INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md)

基于 Claude CLI 原生 `--permission-prompt-tool` 能力，通过 MCP 工具实现非交互式权限审批，对接现有飞书审批系统。

---

## 1. 背景

### 1.1 问题

在 [交互式 Claude 会话调研](./INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md) 中，我们确定了使用 `claude -p` (subprocess + headless) 作为后端会话方案。但该模式下遇到需要权限的工具调用会直接跳过，无法触发审批流程。

### 1.2 发现

Claude CLI 原生提供 `--permission-prompt-tool` 参数，可在 `-p` 模式下将权限决策委托给指定的 MCP 工具。这是官方支持的机制，无需 PTY 代理或 ACP 协议适配。

### 1.3 方案对比

| 方案 | 新增代码量 | 依赖 | 现有代码侵入 | 稳定性 |
|------|-----------|------|-------------|--------|
| PTY 终端代理 | ~500+ 行 | pyte/pexpect | 大 | 低（TUI 兼容性风险） |
| claude-agent-acp ACP 客户端 | ~230 行 | claude-agent-acp npm 包 | 小 | 中（unstable API） |
| **`--permission-prompt-tool` MCP** | **~85 行** | **无额外依赖** | **零** | **高（官方机制）** |

---

## 2. 架构

### 2.1 整体流程

```
claude -p "prompt" \
  --permission-prompt-tool mcp__approver__permission_request \
  --mcp-config approver.json \
  --output-format stream-json

    ↓ Claude 执行任务
    ↓ 遇到需要权限的工具调用
    ↓
┌─────────────────────────────────────────────────────┐
│  权限评估链（Claude CLI 内置）                        │
│                                                      │
│  1. settings.json / --allowedTools 静态规则匹配       │
│     → 命中：直接 allow/deny，不走 MCP                 │
│                                                      │
│  2. 未命中 → 调用 MCP 工具 permission_request            │
│     → MCP server 收到 {tool_name, input}             │
│     → MCP server 转发到网关 callback server           │
│     → callback server 发飞书卡片                      │
│     → 用户点击按钮                                    │
│     → callback server 返回决策                        │
│     → MCP server 返回 {behavior: "allow"/"deny"}     │
│                                                      │
│  3. Claude 根据决策继续或跳过                          │
└─────────────────────────────────────────────────────┘
```

### 2.2 组件关系

```
┌─────────────┐     stdio      ┌──────────────────┐
│ Claude CLI   │◄──────────────►│ approver MCP     │
│ (claude -p)  │    JSON-RPC    │ (permission_mcp) │
└─────────────┘                 └────────┬─────────┘
                                         │ subprocess (stdin/stdout)
                                         │ 从 settings.json 读取 hook 配置
                                         ▼
                                ┌──────────────────┐
                                │ hook script       │
                                │ (permission.sh)   │
                                │                   │
                                │ - 发飞书卡片       │
                                │ - 等 socket 决策   │
                                │ - 返回 allow/deny  │
                                └──────────────────┘
                                         │
                                         ▼
                                    飞书用户审批
```

**关键**：MCP server 是纯转发层，通过 subprocess 调用 settings.json 中配置的 PermissionRequest hook 脚本，完全复用现有审批流程。

---

## 3. MCP Tool 协议

### 3.1 输入（Claude CLI → MCP Tool）

Claude CLI 调用 MCP 工具时传入：

```json
{
  "tool_name": "Bash",
  "input": {
    "command": "npm test",
    "description": "Run tests"
  },
  "tool_use_id": "toolu_01ABC123..."
}
```

> **注意**：`--permission-prompt-tool` MCP 模式下 Claude CLI **会传递 `tool_use_id`**（实测确认）。
> 这与 CLI 交互模式下的 PermissionRequest hook 不同——后者不包含 `tool_use_id`。
> 两者是不同的机制，不能混淆。

`tool_name` 可能的值：`Bash`, `Edit`, `Write`, `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `NotebookEdit`, `mcp__xxx__yyy` 等。

`input` 的结构取决于具体工具，常见的：
- Bash: `{"command": "...", "description": "..."}`
- Edit: `{"file_path": "...", "old_string": "...", "new_string": "..."}`
- Write: `{"file_path": "...", "content": "..."}`

### 3.2 输出（MCP Tool → Claude CLI）

MCP 工具返回标准 MCP tool result，`text` 字段为 JSON 字符串。

> **重要**：以下 schema 已通过实际测试验证（2026-03-15），与官方文档描述有差异。

**Schema 要求：**

| behavior | 必需字段 | 可选字段 |
|----------|---------|---------|
| `allow` | `updatedInput` (record) | - |
| `deny` | `message` (string) | `interrupt` (boolean) |

**允许执行（updatedInput 必需）：**
```json
{
  "content": [{
    "type": "text",
    "text": "{\"behavior\": \"allow\", \"updatedInput\": {\"command\": \"npm test\", \"description\": \"Run tests\"}}"
  }]
}
```

**允许但修改参数：**
```json
{
  "content": [{
    "type": "text",
    "text": "{\"behavior\": \"allow\", \"updatedInput\": {\"command\": \"npm test --safe\", \"description\": \"Run tests safely\"}}"
  }]
}
```

**拒绝执行：**
```json
{
  "content": [{
    "type": "text",
    "text": "{\"behavior\": \"deny\", \"message\": \"用户拒绝了此操作\"}"
  }]
}
```

**拒绝并中断（interrupt 可选）：**
```json
{
  "content": [{
    "type": "text",
    "text": "{\"behavior\": \"deny\", \"message\": \"用户拒绝并中断\", \"interrupt\": true}"
  }]
}
```

> **注意**：如果不提供 `updatedInput`，Claude CLI 会报 schema 验证错误。MCP server 应在返回 allow 时自动补充原始 `tool_input` 作为 `updatedInput`。

### 3.3 JSON-RPC 传输协议

MCP server 通过 stdio 使用 JSON-RPC 2.0 与 Claude CLI 通信。完整生命周期：

**Step 1 — 初始化（request，有 id）：**
```json
// CLI → MCP
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {...}}
// MCP → CLI
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "approver", "version": "1.0.0"}}}
```

**Step 2 — 初始化完成通知（notification，无 id，不得响应）：**
```json
// CLI → MCP（通知没有 id 字段，服务端不能返回响应，否则违反 JSON-RPC 2.0 规范）
{"jsonrpc": "2.0", "method": "notifications/initialized"}
```

**Step 3 — 工具发现（request）：**
```json
// CLI → MCP
{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
// MCP → CLI
{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "permission_request", "inputSchema": {...}}]}}
```

**Step 4 — 权限请求（request，每次需要审批时调用）：**
```json
// CLI → MCP
{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "permission_request", "arguments": {"tool_name": "Bash", "input": {"command": "npm test"}, "tool_use_id": "toolu_01ABC123"}}}
// MCP → CLI
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{\"behavior\": \"allow\", \"updatedInput\": {...}}"}]}}
```

> **实现要点**：
> - 区分 request（有 `id`）和 notification（无 `id`）：notification 不得返回响应
> - `tools/call` 应校验 `params.name` 是否为已注册的工具名
> - MCP 中常见的 notification 有 `notifications/initialized`、`notifications/cancelled` 等

### 3.4 与现有 hook 决策的映射

| 飞书卡片按钮 | callback server 返回 | MCP 响应 |
|-------------|---------------------|----------|
| Allow | `{"action": "allow"}` | `{"behavior": "allow", "updatedInput": <original_input>}` |
| Always Allow | `{"action": "always"}` | `{"behavior": "allow", "updatedInput": <original_input>}` + 写入 permissions.allow |
| Deny | `{"action": "deny"}` | `{"behavior": "deny", "message": "用户拒绝"}` |
| Deny & Interrupt | `{"action": "deny", "interrupt": true}` | `{"behavior": "deny", "message": "用户拒绝并中断", "interrupt": true}` |

> **注意**：
> 1. `--permission-prompt-tool` 不原生支持 "always allow" 语义。可在 MCP server 侧实现：收到 always 时写入 `~/.claude/settings.json` 或 `permissions.allow`，后续同类请求由静态规则直接放行。
> 2. `interrupt` 字段在 MCP 模式下**生效**，Claude 会根据此字段决定是否立即停止尝试其他方案。

---

## 4. stream-json 输出

### 4.1 是否需要

**建议加上**。理由：

| 能力 | text 模式 | stream-json 模式 |
|------|----------|-----------------|
| 实时进度 | ❌ 执行完才输出 | ✅ 逐 token 流式 |
| 工具调用可见 | ❌ 只有最终文本 | ✅ 每个 tool_call 事件 |
| 超时判断 | 只能硬超时 | ✅ 有消息 = 活着 |
| token 用量 | ❌ 不可见 | ✅ usage 事件 |
| 飞书进度更新 | ❌ | ✅ 可实时更新卡片 |
| 错误定位 | 只有 stderr | ✅ 结构化错误事件 |

### 4.2 stream-json 消息格式

每行一个 JSON 对象（ndjson）：

**assistant 文本输出：**
```json
{"type": "assistant", "subtype": "text", "text": "Let me ", "session_id": "..."}
```

**工具调用：**
```json
{"type": "assistant", "subtype": "tool_use", "tool_name": "Bash", "tool_input": {"command": "npm test"}, "session_id": "..."}
```

**工具结果：**
```json
{"type": "tool_result", "tool_name": "Bash", "output": "All tests passed", "session_id": "..."}
```

**最终结果：**
```json
{"type": "result", "result": "完整输出文本", "session_id": "...", "cost_usd": 0.05, "usage": {"input_tokens": 1000, "output_tokens": 500}}
```

### 4.3 在现有系统中的应用

可以在 `src/server/handlers/claude.py` 的 subprocess 处理中，逐行读取 stream-json 输出：

```python
proc = subprocess.Popen(
    [shell, '-lc', f'claude -p {prompt} --resume {session_id} '
                    f'--output-format stream-json '
                    f'--permission-prompt-tool mcp__approver__permission_request'],
    stdout=subprocess.PIPE,
    ...
)

for line in proc.stdout:
    event = json.loads(line)
    if event["type"] == "assistant" and event["subtype"] == "tool_use":
        # 可选：更新飞书卡片显示当前正在执行什么工具
        update_feishu_progress(chat_id, f"正在执行: {event['tool_name']}")
    elif event["type"] == "result":
        send_feishu_result(chat_id, event["result"])
```

**这部分是增量优化，不影响核心方案，可后续迭代。**

---

## 5. 实现详情

### 5.1 MCP Server（新增文件）

`src/server/handlers/permission_mcp.py`（核心逻辑 ~85 行）：

**设计思路**：MCP server 通过命令行参数接收 `--cwd` 和 `--session-id`，从 settings.json 动态读取 PermissionRequest hook 配置，构造模拟的 hook 事件 JSON 通过 subprocess 调用 hook 脚本，完全复用现有审批流程。此脚本由 `claude.py` 的 `_get_mcp_args()` 动态加载。

**核心流程**：

```
启动参数: python3 permission_mcp.py --cwd <project_dir> --session-id <session_id>
                    │
                    ▼
              main() 循环读取 stdin
                    │
           ┌────────┴─────────┐
           │ 有 id？(request)  │ 无 id？(notification)
           │     ▼             │     ▼
           │ handle_request()  │  跳过，不响应
           │     │             │
           └─────┤             └──────────────────
                 ▼
         tools/call → call_hook_router()
                 │
                 ├── 1. 从 settings.json 读取 hook 配置（项目级 > 全局级）
                 ├── 2. 构造 PermissionRequest hook 事件 JSON
                 ├── 3. subprocess 调用 hook 脚本（设置 MCP_MODE=1 跳过延迟）
                 └── 4. 解析 hook 输出，返回 {behavior, updatedInput/message}
```

**关键实现细节**：

1. **通知处理**：`notifications/initialized` 返回 `None`，`main()` 循环中检测到无 `id` 或返回 `None` 时跳过响应（JSON-RPC 2.0 规范要求通知不得有响应）
2. **工具名校验**：`tools/call` 中校验 `params.name == "permission_request"`，防止未注册工具名被静默处理
3. **allow 补全**：`behavior: "allow"` 时自动补充 `updatedInput`（CLI 实际上要求此字段）
4. **Python 3.6 兼容**：使用 `typing` 模块、`subprocess.PIPE` + `universal_newlines=True`
5. **Hook 配置读取**：从 `settings.json` 的 `hooks.PermissionRequest` 中读取第一个 `type: "command"` 的 hook

### 5.2 MCP 配置（内联 JSON）

`claude.py` 中的 `_get_mcp_args(project_dir, session_id)` 动态构建内联 MCP 配置，通过 `args` 将上下文传递给 MCP server：

```python
def _get_mcp_args(project_dir: str, session_id: str) -> str:
    import json

    # 动态定位 MCP 脚本路径（与 claude.py 同目录）
    mcp_script = os.path.join(os.path.dirname(__file__), "permission_mcp.py")

    if not os.path.exists(mcp_script):
        return ""

    # 通过 args 参数传递 cwd 和 session_id，避免环境变量污染
    mcp_config = {
        "mcpServers": {
            "approver": {
                "command": "python3",
                "args": [mcp_script, "--cwd", project_dir, "--session-id", session_id]
            }
        }
    }

    config_json = json.dumps(mcp_config)
    return f"--permission-prompt-tool {MCP_TOOL_NAME} --mcp-config {shlex.quote(config_json)}"
```

**优点：**
- 不依赖外部配置文件，不生成临时文件
- 路径自动计算，无需环境变量
- 通过命令行参数传递上下文（project_dir、session_id），每个 MCP server 实例独立隔离

### 5.3 调用方式

网关服务（`claude.py`）会通过 `_get_mcp_args()` 自动构建内联 JSON 配置，用户无需手动指定。等效的命令行形式：

```bash
# 基本用法（内联 MCP 配置，由 claude.py 自动生成）
claude -p "重构 auth 模块" \
  --session-id <session_id> \
  --permission-prompt-tool mcp__approver__permission_request \
  --mcp-config '{"mcpServers":{"approver":{"command":"python3","args":["/path/to/permission_mcp.py","--cwd","/project/dir","--session-id","<session_id>"]}}}'

# 恢复会话
claude -p "继续补充测试" \
  --resume <session_id> \
  --permission-prompt-tool mcp__approver__permission_request \
  --mcp-config '...'

# 静态规则 + MCP 审批混合使用
claude -p "修复 bug" \
  --allowedTools "Read,Grep,Glob" \
  --permission-prompt-tool mcp__approver__permission_request \
  --mcp-config '...'
```

---

## 6. Callback Server 适配

**无需任何改动**。

MCP server 直接调用 `permission.sh`，完全复用现有流程：
- `permission.sh` 发送飞书卡片
- callback server 处理用户决策回调
- 通过 socket 返回决策给 `permission.sh`
- MCP server 从 `permission.sh` 的 stdout 读取决策

整个审批链路与交互模式下 Claude CLI 触发 PermissionRequest hook 完全一致。

---

## 7. 分发与安装

### 7.1 自动检测

网关服务（`src/server/handlers/claude.py`）的 `_get_mcp_args()` 会自动检测 MCP 脚本是否存在：

```python
# 检测同目录下的 permission_mcp.py
mcp_script = os.path.join(os.path.dirname(__file__), "permission_mcp.py")
if os.path.exists(mcp_script):
    # 自动添加 --permission-prompt-tool 和 --mcp-config 参数
```

**无需用户手动配置**，只要 `permission_mcp.py` 与 `claude.py` 同目录存在，网关服务调用 `claude -p` 时会自动添加 MCP 审批参数。

### 7.2 手动使用（可选）

用户也可以手动使用 wrapper 脚本：

```bash
~/.claude/hooks/bin/claude-approve -p "your prompt"
```

---

## 8. 改动总结

| 组件 | 改动 | 文件 |
|------|------|------|
| MCP server | **新增** ~85 行核心逻辑 | `src/server/handlers/permission_mcp.py` |
| claude.py | **修改** ~40 行 | `_get_mcp_args(project_dir, session_id)` 动态生成内联 MCP 配置 |
| permission.sh | **修改** +6 行 | 添加 `MCP_MODE` 检测，跳过延迟 |
| callback server | **无** | - |
| hook-router.sh | **无** | - |
| 飞书卡片模板 | **无** | - |
| 飞书回调流程 | **无** | - |

---

## 9. 与现有 hook 机制的关系

两套机制**互补共存**，不冲突：

```
┌──────────────────────────────────────────────────────────┐
│                  Claude CLI 权限处理                       │
│                                                           │
│  交互模式（claude）                                        │
│  └─→ PermissionRequest Hook → hook-router.sh              │
│      └─→ permission.sh → 飞书卡片 → socket 等待           │
│                                                           │
│  非交互模式（claude -p）                                   │
│  └─→ --permission-prompt-tool → MCP server                │
│      └─→ permission.sh → 飞书卡片 → socket 等待           │
│                                                           │
│  两条路径共享：                                             │
│  ├── permission.sh（审批流程）                             │
│  ├── callback server（决策处理）                            │
│  ├── 飞书卡片模板（UI 展示）                                │
│  └── permissions.allow（Always Allow 持久化）              │
└──────────────────────────────────────────────────────────┘
```

---

## 10. 风险与注意事项

### 10.1 `--permission-prompt-tool` Schema 验证

**已验证（2026-03-15）**：

通过 mock 测试确认了 MCP 工具返回值的实际 schema 要求：

| behavior | 必需字段 | 可选字段 |
|----------|---------|---------|
| `allow` | `updatedInput` (record) | - |
| `deny` | `message` (string) | `interrupt` (boolean) |

**关键发现**：
1. `updatedInput` 在 `behavior: "allow"` 时是**必需的**（官方文档未说明）
2. `interrupt` 字段在 MCP 模式下**生效**，Claude 会据此决定是否中断

**注意事项**：
- 官方文档极少，以上 schema 通过实际测试验证
- 建议在 MCP server 中对 allow 响应自动补充 `updatedInput`

### 10.2 MCP server 超时
- 飞书审批可能耗时较长（用户不在手边）
- MCP stdio 协议无内置心跳机制
- **缓解**：callback server 端设合理超时（默认 10 分钟），超时返回 deny

### 10.3 Always Allow 实现
- `--permission-prompt-tool` 不原生支持 always allow 语义
- MCP server 需自行写入 permissions.allow 文件
- **缓解**：复用现有 rule_writer.py 逻辑

### 10.4 并发安全
- 多个 `claude -p` 实例可能同时触发 MCP server
- 每个 `claude -p` 会启动独立的 MCP server 子进程，天然隔离
- callback server 端需处理并发请求（已有 request_manager 支持）

---

## 11. 后续迭代

- **P0**：✅ 核心流程打通（MCP server 调用 permission.sh）
- **P0**：✅ Schema 验证（`updatedInput` 必需，`interrupt` 支持）
- **P1**：stream-json 输出集成（实时进度更新飞书卡片）
- **P1**：`claude-approve` wrapper 脚本（手动调用场景）
- **P2**：Always Allow 支持（MCP 侧写入规则文件）
- **P2**：`updatedInput` 能力（审批时修改参数）
- **P3**：批量部署方案（settings.json 自动配置）
