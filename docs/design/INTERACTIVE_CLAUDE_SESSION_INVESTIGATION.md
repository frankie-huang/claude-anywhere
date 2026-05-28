# 交互式 Claude 会话调研报告

> 调研日期：2026-02-18
> 状态：**已定案** - 继续使用 subprocess + headless 模式

## 1. 调研背景

### 1.1 目标

探索在后端 Python 进程中发起**交互式 Claude Code CLI 会话**的可行性，使得用户通过飞书发起/继续会话时，能够获得与在终端直接使用 `claude` 命令相同的交互体验。

### 1.2 核心需求

1. **交互式体验**：支持多轮对话，而非单次命令执行
2. **Hook 机制兼容**：工具权限请求能够通过现有的 Shell Hook 机制触发飞书通知
3. **会话持久化**：支持 `--resume` 恢复之前的会话
4. **生命周期管理**：合理管理进程生命周期，支持超时清理

### 1.3 最终架构

```
飞书消息 → Python 后端 → subprocess.Popen → claude --print --resume/--session-id
                                      ↓
                          PermissionRequest Hook → 飞书审批通知
```

---

## 2. 调研方案总览

| 方案 | 描述 | 交互性 | 复杂度 | 结论 |
|------|------|--------|--------|------|
| **subprocess + Headless** | `subprocess.Popen` + `claude --print` | ⭐⭐ | 低 | ✅ **最终选择** |
| **Claude Agent SDK** | 官方 Python SDK | ⭐⭐⭐ | 中 | ❌ 已尝试并回退 |
| **PTY + 交互模式** | pexpect + `claude` (无 `--print`) | ⭐⭐⭐⭐⭐ | 高 | ❌ TUI 输出解析复杂 |
| **PTY + Headless 模式** | pexpect + `claude --print` | ⭐⭐ | 中 | ❌ 多个问题未解决 |

---

## 3. 方案详细分析

### 3.1 subprocess + Headless 模式（最终选择）✅

#### 3.1.1 方案概述

通过 Python 标准库 `subprocess.Popen` 启动 `claude --print` 子进程，使用登录 shell 包装命令以加载用户环境配置：

```python
import subprocess
import shlex

# 构建命令
cmd_str = f'claude --print {shlex.quote(prompt)} --resume {shlex.quote(session_id)}'

# 通过登录 shell 执行（加载 ~/.bashrc 或 ~/.zshrc）
shell = os.environ.get('SHELL', '/bin/bash')
shell_name = os.path.basename(shell)

if shell_name == 'zsh':
    cmd = [shell, '-ic', cmd_str]
elif shell_name == 'fish':
    cmd = [shell, '-c', cmd_str]
else:
    cmd = [shell, '-lc', cmd_str]

proc = subprocess.Popen(cmd, cwd=project_dir, stdout=PIPE, stderr=PIPE)
```

#### 3.1.2 优点

- **简单可靠**：使用 Python 标准库，无额外依赖
- **Shell Hook 完全兼容**：CLI 进程正常触发 PermissionRequest Hook
- **环境变量加载**：登录 shell 自动加载用户配置（别名、PATH 等）
- **成熟稳定**：已在生产环境验证

#### 3.1.3 缺点

- **单次执行**：每次调用都是独立的进程，不是真正的"交互式会话"
- **文本输出**：只能获取纯文本 stdout/stderr，无法获取结构化信息
- **shell 依赖**：依赖登录 shell 加载环境，不同 shell 有细微差异

#### 3.1.4 当前实现

```python
# src/server/handlers/claude.py

def _launch_claude(session_id, project_dir, prompt, chat_id, message_id, session_mode, claude_command):
    shell = _get_shell()
    claude_cmd = _get_claude_command(claude_command)
    session_flag = '--session-id' if session_mode == 'new' else '--resume'
    mcp_argv = _get_mcp_args(project_dir, session_id)
    args_argv = ['--print', session_flag, session_id] + mcp_argv + ['--', prompt]
    cmd_str = _expand_template(get_claude_args_template(), shlex.split(claude_cmd), args_argv)
    cmd = build_shell_cmd(shell, cmd_str)
    proc = subprocess.Popen(cmd, cwd=project_dir, stdout=PIPE, stderr=PIPE)
    # ... 启动检查和后台监控逻辑
```

---

### 3.2 Claude Agent SDK 方案（已回退）❌

#### 3.2.1 方案概述

使用 Anthropic 官方提供的 `claude-agent-sdk` Python 包，直接在代码中调用 `query()` 函数发起 Claude 会话。

```python
from claude_agent_sdk import query, ClaudeAgentOptions

async for message in query(
    prompt="帮我重构这段代码",
    options=ClaudeAgentOptions(
        cwd="/path/to/project",
        resume="session-id-here",
    )
):
    if isinstance(message, ResultMessage):
        print(message.result)
```

#### 3.2.2 尝试过程

**阶段 1：基本集成**

将 `subprocess.Popen` 替换为 SDK `query()` 调用，权限控制仍依赖 Shell Hook：

```python
# openspec/changes/replace-subprocess-with-sdk/design.md
options = ClaudeAgentOptions(
    cwd=project_dir,
    cli_path=cli_path,
    setting_sources=['user', 'project', 'local'],
    # 不设置 permission_mode，保持默认让 hook 正常触发
)
if session_mode == 'resume':
    options.resume = session_id
else:
    options.extra_args = {'session-id': session_id}
```

**阶段 1 验证结果**：❌ Shell Hook 未触发，权限请求被直接拒绝

**阶段 2：尝试 `can_use_tool` 回调**

尝试使用 SDK 提供的 `can_use_tool` 参数，在 Python 层面实现权限回调：

```python
async def can_use_tool_callback(request: PermissionRequest) -> bool:
    # 通过飞书发送审批请求，等待用户响应
    # ...
    return user_approved

options = ClaudeAgentOptions(
    cwd=project_dir,
    can_use_tool=can_use_tool_callback,  # Python 回调
)
```

**阶段 2 遇到的问题**：

1. **"can_use_tool callback requires streaming mode"**
   - 修复：将 prompt 从字符串改为 AsyncIterable 格式
   ```python
   async def prompt_generator():
       yield {"type": "user", "message": {"role": "user", "content": prompt}}
   ```

2. **"can_use_tool callback cannot be used with permission_prompt_tool_name"**
   - 修复：移除 `permission_prompt_tool_name` 参数（SDK 会自动设置）

3. **回调从未被调用** ❌
   - 即使设置 `setting_sources=[]` 禁止加载 Shell Hook，`can_use_tool` 回调也从未被触发
   - 日志显示 `can_use_tool` 配置为 True，但实际调用时直接拒绝权限请求

**阶段 3：尝试 `hooks` 参数**

尝试使用 SDK 的 `hooks` 参数监听 `PermissionRequest` 事件：

```python
from claude_agent_sdk import PermissionRequest

async def on_permission_request(event: PermissionRequest):
    # 处理权限请求
    pass

options = ClaudeAgentOptions(
    hooks={
        "PermissionRequest": on_permission_request,
    }
)
```

**阶段 3 验证结果**：❌ 回调从未被调用

**阶段 4：尝试 `permission_prompt_tool_name`**

尝试使用 `permission_prompt_tool_name` 让 Claude 调用一个自定义工具来请求权限：

```python
options = ClaudeAgentOptions(
    permission_prompt_tool_name="request_permission",
    tools=[{
        "name": "request_permission",
        "description": "Request user permission for tool use",
        "input_schema": {...}
    }]
)
```

**阶段 4 验证结果**：❌ 与 `can_use_tool` 冲突，无法同时使用

#### 3.2.3 根本原因分析

经过深入测试，发现 SDK 的权限机制设计存在以下限制：

| 机制 | 设计用途 | 远程审批可行性 |
|------|----------|----------------|
| `can_use_tool` | 预配置权限规则（如允许特定工具） | ❌ 不支持动态等待 |
| `hooks.PermissionRequest` | 本地交互式使用 | ❌ CLI 不发送事件 |
| `permission_prompt_tool_name` | 自定义权限工具 | ❌ 与 can_use_tool 冲突 |
| Shell Hook (`settings.json`) | CLI 直接运行时触发 | ✅ 仅 subprocess 方案可用 |

**核心问题**：SDK 内部启动 CLI 时，权限请求不会通过控制协议（stdin/stdout）传递给调用方。SDK 的 `can_use_tool` 和 `hooks` 机制设计用于：
- **无头自动化**：预先配置允许/拒绝规则
- **本地交互**：用户在终端直接响应

对于**远程审批**场景（用户通过飞书响应），SDK 不提供合适的机制。

#### 3.2.4 回退原因

1. **权限机制不兼容**：SDK 的 `can_use_tool` 和 `hooks` 参数无法用于远程权限审批
2. **Shell Hook 是唯一可行方案**：只有通过 subprocess 直接运行 CLI，才能触发 `settings.json` 中配置的 Shell Hook
3. **额外依赖无意义**：如果只是用 SDK 启动 CLI 而不使用其权限回调，SDK 只是"更复杂的 subprocess"
4. **Python 版本要求**：SDK 要求 Python 3.10+，而 subprocess 方案兼容 3.6+

**决策记录**：
> SDK 的权限机制（can_use_tool、hooks）不适用于远程审批场景。对于飞书权限通知需求，必须依赖 Shell Hook 机制，而 Shell Hook 只能在 CLI 直接运行时触发。因此，subprocess 方案是目前唯一可行的选择。

#### 3.2.4 回退操作

```bash
# 归档 SDK 提案
mv openspec/changes/replace-subprocess-with-sdk openspec/archive/

# 恢复 subprocess 版本代码
git checkout b53de28 -- src/server/handlers/claude.py
```

---

### 3.3 PTY + 交互模式方案（已放弃）❌

#### 3.3.1 方案概述

使用 `pexpect` 库在 PTY（伪终端）中启动真实的 `claude` CLI 交互进程，追求"真正的交互式体验"。

#### 3.3.2 遇到的问题

**问题 1：嵌套会话检测**

```
Error: Claude Code cannot be launched inside another Claude Code session.
To bypass this check, unset the CLAUDECODE environment variable.
```

**问题 2：TUI 输出解析复杂**

交互模式的输出包含大量 ANSI escape 序列和 TUI 控制字符，程序化解析非常困难。

**问题 3：响应完成检测困难**

交互模式下，难以可靠地判断 Claude 是否已完成响应并等待用户输入。

#### 3.3.3 结论

交互模式是为人类终端设计的，不适合程序化调用。

---

### 3.4 PTY + Headless 模式方案（已放弃）❌

#### 3.4.1 遇到的问题

**问题 1：stream-json 需要 --verbose**

```
Error: When using --print, --output-format=stream-json requires --verbose
```

**问题 2：嵌套会话检测**

与交互模式相同，需要移除 `CLAUDECODE` 环境变量。

**问题 3：本质上仍是单次执行**

Headless 模式每次执行完就退出，与 subprocess 方案相比没有实质性改进，反而增加了 pexpect 依赖。

---

## 4. 问题总结

### 4.1 核心矛盾

| 需求 | 现实限制 |
|------|----------|
| 真正的交互式体验（TUI） | TUI 输出包含大量控制字符，解析困难 |
| 在 Claude Code 环境中运行 | 嵌套会话检测阻止子会话启动 |
| 使用现有 Shell Hook 机制 | SDK 和 Hook 是两套独立系统 |
| Headless 模式简单可靠 | 本质是单次执行，不是交互会话 |

### 4.2 为什么 subprocess 方案是最佳选择

1. **简单可靠**：使用 Python 标准库，无额外依赖
2. **Hook 兼容**：CLI 进程正常触发 Shell Hook，权限机制完整保留
3. **环境加载**：登录 shell 自动加载用户配置
4. **已验证**：在生产环境稳定运行

---

## 5. 未来展望

### 5.1 可能的增强方向

1. **流式输出到飞书**：通过 WebSocket 或长轮询，将 Claude 的输出实时推送到飞书
2. **增强 AskUserQuestion 支持**：处理 Claude 的澄清性提问
3. **优化输出格式**：使用 `--output-format stream-json` 获取结构化输出

### 5.2 等待官方支持

关注 Claude Code 的更新，未来可能提供：
- 更好的嵌入式会话支持
- 官方的交互式 API
- 更完善的 SDK 权限回调机制

---

## 6. 文件变更记录

| 文件 | 状态 | 说明 |
|------|------|------|
| `src/server/handlers/claude.py` | 恢复 | 回退到 subprocess 版本 |
| `src/server/services/sdk_permission.py` | 删除 | SDK 权限管理服务（已验证不可行） |
| `openspec/changes/replace-subprocess-with-sdk/` | 归档 | SDK 提案已移至 archive 目录 |
| `src/server/services/pty_session.py` | 删除 | PTY 方案代码已删除 |

---

## 7. 参考资料

- [Claude Agent SDK Documentation](https://platform.claude.com/docs/en/agent-sdk)
- [Handle approvals and user input](https://platform.claude.com/docs/en/agent-sdk/user-input)
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- [pexpect documentation](https://pexpect.readthedocs.io/)
- [Interactive Shell PTY Feature Request - GitHub Issue #9881](https://github.com/anthropics/claude-code/issues/9881)

---

## 8. 结论

经过充分调研和**实际代码测试验证**，**最终决定继续使用 subprocess + Headless 模式方案**。

### 8.1 测试验证总结

| SDK 方案尝试 | 结果 | 原因 |
|-------------|------|------|
| 基本集成 + Shell Hook | ❌ | Hook 未触发，权限直接被拒绝 |
| `can_use_tool` 回调 | ❌ | 回调从未被调用 |
| `hooks.PermissionRequest` | ❌ | 事件从未触发 |
| `permission_prompt_tool_name` | ❌ | 与 can_use_tool 冲突 |
| `setting_sources=[]` 禁用 Hook | ❌ | 仍然无法触发 SDK 权限回调 |

### 8.2 方案对比

| 对比项 | subprocess + Headless | Claude Agent SDK | PTY 方案 |
|--------|----------------------|------------------|----------|
| 实现复杂度 | ⭐ 低 | ⭐⭐ 中 | ⭐⭐⭐ 高 |
| 依赖数量 | ⭐ 无额外依赖 | ⭐⭐ 新增 SDK | ⭐⭐ 新增 pexpect |
| **远程权限审批** | ✅ Shell Hook 可用 | ❌ 权限机制不兼容 | ⚠️ 需要额外处理 |
| 可靠性 | ✅ 已验证 | ❌ 经测试不可行 | ❌ 多个问题 |
| 交互性 | ❌ 单次执行 | ❌ 单次执行 | ⚠️ 理论上支持 |

### 8.3 核心认知

1. **SDK 权限机制的限制**：`can_use_tool` 和 `hooks` 设计用于本地预配置或交互式使用，不支持远程异步审批
2. **Shell Hook 是唯一可行方案**：只有 subprocess 直接运行 CLI 才能触发 `settings.json` 中的 Hook
3. **Headless 模式的合理性**：配合 `--resume` 参数可以实现多轮对话的上下文延续，虽然不是"真正的交互式"，但满足实际需求

**最终结论**：在 Claude Code 提供更好的远程权限审批机制之前，subprocess + Shell Hook 是唯一可行的方案。
