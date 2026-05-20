# Codex vs Claude 实现差异

> 日期：2026-05-20
> 状态：持续更新
> 关联文档：[CODEX_PERMISSION_INVESTIGATION.md](./CODEX_PERMISSION_INVESTIGATION.md)

开发 Codex 适配过程中发现的与 Claude 的行为差异汇总，供后续维护参考。

---

## 1. 命令构建

| 项目 | Claude | Codex |
|------|--------|-------|
| 非交互模式 | `--print` | `exec --json` |
| 新建会话 | `claude --print --session-id <id> -- <prompt>` | `codex exec --json --cd <dir> <prompt>` |
| 恢复会话 | `claude --print --resume <id> -- <prompt>` | `codex exec resume --json <id> <prompt>` |
| `--cd` 支持 | 无此参数（通过 cwd 控制） | 新建支持，**resume 不支持**（工作目录由原始会话决定） |
| `--` 分隔符 | 需要（防止 prompt 中的 `--flag` 被误解析） | 不需要 |

**文件**：`agents/claude.py` `build_command_string()`、`agents/codex.py` `build_command_string()`

## 2. Session ID 生命周期

| 项目 | Claude | Codex |
|------|--------|-------|
| 生成方 | 调用方预生成 UUID，通过 `--session-id` 传入 | CLI 自动生成 thread_id |
| 获取方式 | 不需要捕获（调用方已知） | 从 stdout 首条 JSONL 事件 `{"type":"thread.started","thread_id":"xxx"}` 捕获 |
| ID 格式 | UUID v4 | UUID v7（时间序列，前缀相似） |
| Store 处理 | 直接写入 | 需要 rename（临时 UUID → 真实 thread_id）+ adopt_pending_session |

**文件**：`agents/__init__.py` `_capture_session_id()`、`session_chat_store.py` `rename_session()` / `adopt_pending_session()`

**已知限制**：`_readline_with_timeout()` 使用 `select.select` 检测 OS pipe buffer，与 `TextIOWrapper` 内部缓冲混用，理论上可能漏读。实际影响极低，因为 `thread.started` 几乎总是第一行输出。

## 3. 权限审批

| 项目 | Claude | Codex |
|------|--------|-------|
| 审批机制 | `--permission-prompt-tool` MCP 工具 | 无（exec 模式下 `approval_policy` 强制降级为 `never`） |
| PermissionRequest hook | 正常触发 | **不触发** |
| 安全边界 | 用户逐次审批 | 沙箱模式（`workspace-write` / `danger-full-access`） |
| 沙箱配置 | 无 | 用户自行通过 `CODEX_COMMAND` 或 `~/.codex/config.toml` 配置 |
| hook 决策输出 | 支持 `behavior` + `message` + `interrupt` + `updatedInput` + `updatedPermissions` | **仅支持 `behavior` + `message`**，其余字段 fail closed |

**文件**：`permission.sh` `output_decision()`、`docs/design/CODEX_PERMISSION_INVESTIGATION.md`

**上游 Issue**：[#15311](https://github.com/openai/codex/issues/15311)、[#16301](https://github.com/openai/codex/issues/16301)

## 4. Hook 配置

| 项目 | Claude | Codex |
|------|--------|-------|
| 配置文件 | `~/.claude/settings.json` | `~/.codex/config.toml` |
| 格式 | JSON | TOML（`[[hooks.EventName]]` 数组语法） |
| 信任机制 | 自动加载 | 首次运行需 review，hash 存入 `hooks.state` |
| 配置工具 | `HookConfigurator` | `CodexHookConfigurator` |

**文件**：`setup_init.py`

## 5. Transcript 格式与响应提取

### Claude

单一格式：JSONL 数组，每条记录为 user/assistant 消息对象。

### Codex

两种格式，Stop hook 的 `transcript_path` 始终指向持久化文件：

**持久化文件**（`~/.codex/sessions/.../rollout-*.jsonl`）：
```jsonl
{"type":"session_meta","payload":{"id":"xxx",...}}
{"type":"event_msg","payload":{"type":"task_started","turn_id":"..."}}
{"type":"event_msg","payload":{"type":"agent_message","message":"..."}}
{"type":"response_item","payload":{"type":"reasoning","encrypted_content":"..."}}
{"type":"event_msg","payload":{"type":"task_complete","turn_id":"...","last_agent_message":"..."}}
```

**exec stdout**（`codex exec --json` 输出，用于 session ID 捕获，非 hook 输入）：
```jsonl
{"type":"thread.started","thread_id":"xxx"}
{"type":"item.completed","item":{"type":"agent_message","text":"..."}}
```

**文件**：`stop.sh` `extract_codex_response()` / `extract_claude_response()`

### 提取差异

| 项目 | Claude | Codex |
|------|--------|-------|
| 提取函数 | `extract_claude_response` | `extract_codex_response` |
| 定位方式 | 找最近一条用户消息，取其后所有 assistant 消息 | 按 `turn_id` 分片定位目标 turn |
| jq 覆盖范围 | 完整实现 | 仅 exec stdout 格式（持久化格式用 jq 难以维护） |
| 重试间隔 | 0.1s | 1s（持久化文件写入延迟更大） |

## 6. Thinking / Reasoning

| 项目 | Claude | Codex |
|------|--------|-------|
| 字段 | `message.content[].type == "thinking"` | `response_item.payload.type == "reasoning"` |
| 内容 | 明文 | **加密**（`encrypted_content` 字段，`summary` 和 `content` 为空） |
| 卡片展示 | 正常展示折叠区 | 无法展示（无法解密） |

## 7. 进程与 Pipe 行为

| 项目 | Claude | Codex |
|------|--------|-------|
| hook 完成检测 | 等待进程 PID 退出 | 等待 **pipe 关闭**（非 PID 退出） |
| 后台进程处理 | `send_xxx_async &` | 必须 `send_xxx_async >/dev/null 2>&1 &`（切断 pipe 继承） |
| stdin | 无特殊处理 | `stdin=subprocess.DEVNULL`（避免 stdin 继承） |

**影响**：如果后台子进程继承了 hook 的 stdout/stderr pipe，Codex 会一直显示 "Running Stop hook"。

**文件**：`stop.sh`、`user_prompt.sh`

## 8. 环境变量

| 项目 | Claude | Codex |
|------|--------|-------|
| 进程 env 清理 | 清除 `CLAUDECODE` 变量（防止嵌套会话检测） | 无特殊清理 |
| AGENT_TYPE 传递 | hook 由 CLI 直接启动，不一定继承服务端 env | 同左 |
| AGENT_TYPE 检测 | `hook-router.sh` 从 `transcript_path` 路径匹配 `/.claude/` | 从 `transcript_path` 匹配 `/.codex/sessions/` |

**文件**：`hook-router.sh`、`agents/claude.py` `build_env()`

## 9. 显示与通知

| 项目 | Claude | Codex |
|------|--------|-------|
| 产品名 | Claude Code | Codex |
| Stop 卡片标题 | "Claude Code 处理完成" | "Codex 处理完成" |
| 恢复命令 | `claude --resume <session_id>` | `codex resume <session_id>` |
| 超时提示 | "请尽快操作以避免 Claude Code 超时等待" | "请尽快操作以避免 Codex 超时等待" |
| 错误通知 | "❌ Claude 执行异常" | "❌ Codex 执行异常" |

**文件**：`feishu.sh` `_agent_display_name()` / `_agent_resume_command()`、`handlers/feishu.py`、`handlers/claude.py`
