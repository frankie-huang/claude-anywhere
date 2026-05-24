## Context

项目已完成 `AgentAdapter` 抽象层预整理（`src/server/agents/__init__.py` + `agents/claude.py`），将 Claude CLI 协议逻辑从 `handlers/claude.py` 分离。现在需要在此基础上实现 Codex 适配器。

**核心差异**：

| 维度 | Claude Code | Codex CLI |
|------|------------|-----------|
| 非交互执行 | `claude --print "prompt"` | `codex exec "prompt"` |
| 新建会话 | `--session-id <id>`（外部指定） | 自动生成，从 `--json` 输出 `thread.started` 捕获 |
| 恢复会话 | `--resume <id>` | `codex exec resume <id>` |
| 权限委托 | `--permission-prompt-tool`（MCP 工具） | `PermissionRequest` hook（config.toml） |
| MCP 配置 | 运行时 `--mcp-config` 参数 | `config.toml [mcp_servers]` 预配置 |
| Hook 配置 | `settings.json` (JSON) | `config.toml` (TOML) |
| 输出格式 | text（默认）/ `--output-format stream-json` | text（默认）/ `--json`（JSONL） |
| 会话文件 | `~/.claude/projects/.../*.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| 工具名 | `Bash`, `Edit`, `Write`, `Read` 等 | `shell`, `apply_patch`, `read_file` 等 |
| 指令文件 | `CLAUDE.md` | `AGENTS.md` |
| 环境变量清除 | `CLAUDECODE` | 无需 |

## Goals / Non-Goals

**Goals**:
- 用户通过 `AGENT_TYPE=codex` 即可切换到 Codex 后端
- 飞书侧的使用体验对两个 agent 保持一致（/new、/reply、权限审批卡片）
- 共享代码最大化，agent 特有逻辑隔离在各自的 adapter 中

**Non-Goals**:
- 不在同一实例中混合运行两个 agent（一个 Callback 实例对应一个 agent 类型）
- 不支持 Codex 独有的 sandbox 模式配置（使用默认 `workspace-write`）
- 不实现 Codex `--output-schema` 结构化输出
- 不抽象消息平台（飞书耦合不在此次范围）

## Decisions

### 1. Session ID 捕获策略

**决策**: Codex 新建会话时使用 `--json` 标志，从 stdout 首条 `thread.started` 事件捕获 `thread_id`，作为 session_id 存入 store。

**替代方案**: 
- 环境变量注入 → 放弃，无法唯一标识 session（详见调研讨论）
- 从 session 文件目录扫描 → 放弃，时序不可控

**实现**:
- `CodexAdapter.build_command_string()` 在非交互模式下自动添加 `--json`
- `launch_agent()` 新增 `needs_output_session_id` 流程：启动后读取 stdout 首行，解析 `thread_id`，回填到 session store
- 使用 `pre_session_id`（调用方预生成的 UUID）作为临时 key，捕获到真实 ID 后替换

### 2. 权限审批路径

**决策**: Codex 使用原生 `PermissionRequest` hook，直接调用 `hook-router.sh` → `permission.sh`。不经过 MCP 桥接。

**原因**: Codex 没有 `--permission-prompt-tool` 等效机制，但其 `PermissionRequest` hook 功能完备，可以返回 allow/deny 决策。

**影响**:
- `permission_mcp.py` 仅 Claude 使用，Codex 路径不涉及
- `permission.sh` 核心逻辑完全共享
- Hook 配置写入需要适配 `config.toml` 格式

### 3. Hook 配置双格式

**决策**: `setup_init.py` 根据 `AGENT_TYPE` 决定写入目标：
- Claude → `~/.claude/settings.json` / `.claude/settings.json`
- Codex → `~/.codex/config.toml` / `.codex/config.toml`

**实现**: 新增 `HookConfigurator` 工具类，提供 `write_claude_hooks()` 和 `write_codex_hooks()` 两个方法。

### 4. JSONL Transcript 解析

**决策**: `stop.sh` 中新增 Codex JSONL 解析分支。通过检测 JSONL 首行的 `type` 字段判断格式：
- `type: "user"` / `type: "assistant"` → Claude 格式
- `type: "thread.started"` → Codex 格式

**Codex 提取逻辑**: 
- 找 `type: "item.completed"` 且 `item.type: "agent_message"` 的记录
- 提取 `item.text` 作为最终回复

### 5. 配置架构

```
# .env 新增配置项
AGENT_TYPE=claude              # 或 codex（默认 claude）
CODEX_COMMAND=codex            # Codex CLI 命令（支持列表格式，同 CLAUDE_COMMAND）
CODEX_ARGS_TEMPLATE={cmd} {args}  # Codex 参数模板（同 CLAUDE_ARGS_TEMPLATE）
```

`CLAUDE_COMMAND` 和 `CODEX_COMMAND` 各自独立，不互相干扰。`AGENT_TYPE` 决定使用哪组配置。

### 6. 工具名映射

**决策**: 权限卡片中展示 agent 原生工具名，不做映射。

**原因**: 工具名仅用于飞书卡片展示和 `tools.json` 规则匹配。Codex 的工具名（`shell`, `apply_patch` 等）对用户同样可读。如需在卡片中美化展示，可在飞书卡片层添加显示名映射（不在本次范围）。

## Risks / Trade-offs

| 风险 | 缓解 |
|------|------|
| Codex `PermissionRequest` hook 对 `apply_patch` 等工具不触发（已知 issue） | Codex 自身 sandbox 提供兜底；持续关注上游修复 |
| Codex session ID 捕获有微小时序窗口 | `thread.started` 是首条事件，hook 触发前必然已捕获 |
| `config.toml` 写入可能与用户手动配置冲突 | 仅写入 `[hooks]` 段，不覆盖其他配置；写入前备份 |
| Codex JSONL schema 可能随版本变化 | 解析逻辑做防御性处理，字段缺失时 graceful fallback |

## Open Questions

（无，调研阶段已解决关键问题）
