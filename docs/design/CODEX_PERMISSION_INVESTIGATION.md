# Codex 权限审批调研

> 日期：2026-05-16
> 状态：**受限于 Codex 上游**
> 关联文档：[PERMISSION_PROMPT_TOOL.md](./PERMISSION_PROMPT_TOOL.md)（Claude 权限审批方案）

调研 Codex CLI 在非交互（`codex exec`）模式下实现权限审批的可行性，对标 Claude 已有的 MCP 权限审批方案。

---

## 1. 背景

### 1.1 Claude 现状

Claude Code 通过 `--permission-prompt-tool` 参数将权限决策委托给 MCP 工具，在 `--print` 非交互模式下实现了完整的飞书审批流程（详见 [PERMISSION_PROMPT_TOOL.md](./PERMISSION_PROMPT_TOOL.md)）。

### 1.2 Codex 目标

在 Codex `exec` 非交互模式下实现类似的权限审批，让 Codex 遇到需要提权的操作时能通过飞书通知用户并等待审批。

---

## 2. Codex 权限机制调研

### 2.1 审批策略（approval_policy）

Codex 提供以下审批策略：

| 策略 | 行为 |
|------|------|
| `never` | 不请求审批，操作失败就失败 |
| `on-request` | 由模型判断是否请求审批 |
| `untrusted` | 非可信命令时请求审批 |
| `on-failure` | 失败后再请求（已废弃） |

### 2.2 沙箱模式（sandbox_mode）

| 模式 | 行为 |
|------|------|
| `read-only` | 只读沙箱 |
| `workspace-write` | 可读写工作区及白名单目录 |
| `danger-full-access` | 完全不受限 |

### 2.3 exec 模式的关键限制

**`codex exec` 模式下 `approval_policy` 会被强制降级为 `never`。**

原因：exec 模式没有 TTY，无法向用户展示审批提示。Codex 选择静默跳过而非阻塞。

这意味着：
- `PermissionRequest` hook **不会触发**（没有审批可以拦截）
- 越界操作（如写沙箱外文件、访问受限网络）会被**直接拒绝**，而非请求审批
- `-c 'approval_policy="on-request"'` 参数在 exec 模式下**无效**

### 2.4 Hook 支持情况

Codex 支持与 Claude Code 相同的 hook 事件：

| 事件 | exec 模式下触发 | 说明 |
|------|----------------|------|
| `UserPromptSubmit` | 是 | 用户提交 prompt 时 |
| `Stop` | 是 | agent 完成响应时 |
| `PermissionRequest` | **否** | 因 `approval_policy=never` 不触发 |
| `PreToolUse` | 是 | 工具执行前 |
| `PostToolUse` | 是 | 工具执行后 |
| `SessionStart` | 是 | 会话启动时 |

Hook 配置格式（`~/.codex/config.toml`）与 Claude Code 的 `settings.json` 结构一致，只是用 TOML 语法：

```toml
[[hooks.PermissionRequest]]

[[hooks.PermissionRequest.hooks]]
type = "command"
command = "/path/to/hook-router.sh"
timeout = 660
```

### 2.5 MCP 支持情况

Codex 在 exec 模式下**支持 MCP 工具调用**。MCP server 配置在 `~/.codex/config.toml`：

```toml
[mcp_servers.my_server]
command = "python3"
args = ["/path/to/server.py"]
```

但 MCP 工具是模型主动调用的通用工具，不等同于权限审批机制。Codex 没有类似 Claude 的 `--permission-prompt-tool` 参数来将权限决策委托给 MCP。

---

## 3. 方案分析

### 3.1 对标 Claude 方案的差异

```
Claude 权限链路（已实现）：
  claude --print → 遇到需权限操作
    → --permission-prompt-tool → MCP server
      → permission.sh → 飞书卡片 → socket 等待
        → 用户审批 → 返回 allow/deny

Codex 权限链路（不可行）：
  codex exec → 遇到需权限操作
    → approval_policy 被强制降级为 never
      → 操作被沙箱直接拒绝，PermissionRequest hook 不触发
      → 无法进入审批流程
```

### 3.2 可选方案评估

| 方案 | 可行性 | 说明 |
|------|--------|------|
| PermissionRequest hook | **不可行** | exec 模式下不触发 |
| MCP 工具模拟审批 | **不可靠** | 模型需要主动调用，无法保证在危险操作前调用 |
| `danger-full-access` | 可行但不安全 | 跳过所有限制，无审批 |
| **`workspace-write` 沙箱** | **当前最佳** | 工作区内可写，越界拦截，无审批 |
| 等待上游支持 | 待定 | 见下方 issue 跟踪 |

### 3.3 PreToolUse hook 方案

`PreToolUse` 在 exec 模式下**会触发**，理论上可以在工具执行前通过 hook 拦截并请求审批。但存在局限：

- 只能**拒绝**（exit code 2），不能修改参数后放行
- 拒绝后操作失败，模型可能反复重试
- 无法实现 "allow + 修改参数" 的灵活审批

因此不作为主方案，但可作为未来增强的补充（如拦截特定高危命令）。

---

## 4. 当前实现

### 4.1 沙箱策略

沙箱模式不由适配器硬编码，而是交给用户自行配置。用户可通过以下方式设置：

- **`~/.codex/config.toml`**（推荐）：全局设置 `sandbox_mode = "workspace-write"`
- **`CODEX_COMMAND`**：在命令中带上参数，如 `codex --sandbox workspace-write`

适配器只负责拼接 `exec --json` 等协议必需参数：

```bash
# 新建会话
codex exec --json --cd /project/dir "prompt"

# 恢复会话
codex exec resume --json <session_id> "prompt"
```

### 4.2 行为对比

| 操作 | Claude（有审批） | Codex（沙箱模式） |
|------|-----------------|-------------------|
| 读取项目文件 | 允许 | 允许 |
| 编辑项目文件 | 审批后允许 | 允许（workspace 内） |
| 执行 shell 命令 | 审批后允许 | 允许（workspace 内） |
| 写沙箱外文件 | 审批后允许 | **拒绝** |
| 访问网络 | 审批后允许 | **拒绝** |
| 删除系统文件 | 审批后允许/拒绝 | **拒绝** |

### 4.3 Hook 配置

虽然 `PermissionRequest` 在 exec 模式下不触发，仍在 `~/.codex/config.toml` 中配置了三个 hook（`UserPromptSubmit`、`Stop`、`PermissionRequest`），为后续 Codex 支持 exec 模式权限请求做准备。

---

## 5. 上游 Issue 跟踪

以下 Codex 上游 issue 与本需求直接相关：

| Issue | 标题 | 状态 | 说明 |
|-------|------|------|------|
| [#15311](https://github.com/openai/codex/issues/15311) | Add blocking PermissionRequest hook for external approval UIs | Open | 请求 exec 模式下支持阻塞式 PermissionRequest hook，允许外部系统（IDE、桌面覆盖层、CI/CD）处理审批 |
| [#16301](https://github.com/openai/codex/issues/16301) | hooks: add permission request event for parity with claude code's auto-approve flow | Open | 明确要求与 Claude Code 的审批流程对齐 |

**当这些 issue 被实现后**，可通过以下方式启用完整审批：
1. exec 模式下 `PermissionRequest` hook 能正常触发
2. hook 脚本（`hook-router.sh` → `permission.sh`）发送飞书卡片等待审批
3. 与 Claude 共享同一套审批基础设施

---

## 6. 后续计划

- **当前**：使用 `workspace-write` 沙箱，提供基本安全保障
- **短期**：关注 Codex #15311 / #16301 进展，一旦支持 exec 模式 PermissionRequest hook，立即启用
- **可选增强**：通过 `PreToolUse` hook 拦截特定高危命令（如 `rm -rf`、`git push --force`）
