# Change: 支持 OpenAI Codex CLI 作为第二 Agent

## Why

当前系统仅支持 Claude Code CLI 作为底层 AI 编码代理。为了让用户可以选择不同的 AI 后端（Claude 或 Codex），需要在已有的 `AgentAdapter` 抽象层上实现 Codex 适配器，并调整相关配置、Hook、会话管理和 JSONL 解析逻辑，使两套 agent 可以共存运行。

## What Changes

### 新增能力
- **Codex CLI 适配器** (`agents/codex.py`): 实现 `AgentAdapter` 接口，封装 `codex exec` 命令构建、session ID 捕获、环境变量处理
- **Agent 类型配置**: 新增 `AGENT_TYPE` 配置项，支持 `claude`（默认）和 `codex`；新增 `CODEX_COMMAND` 配置项
- **Codex Hook 配置写入**: `setup_init.py` 支持为 Codex 生成 `config.toml` 格式的 hook 配置
- **Codex 权限审批路径**: Codex 通过原生 `PermissionRequest` hook（而非 MCP 工具）走到 `permission.sh`
- **Codex JSONL 解析**: `stop.sh` 支持从 Codex 格式的 transcript 中提取最终回复

### 修改现有行为
- `handlers/claude.py`: 根据 `AGENT_TYPE` 实例化对应的 adapter（Claude 或 Codex）
- `session_chat_store`: `claude_command` 字段语义泛化为 agent command（字段名保持不变，避免 migration）
- `config.py`: 新增 `AGENT_TYPE`、`CODEX_COMMAND` 配置读取函数
- `.env.example`: 新增 Codex 相关配置项说明
- `setup_init.py`: 根据 agent 类型决定写入 `settings.json`（Claude）或 `config.toml`（Codex）的 hook 配置
- `hook-router.sh`: 兼容 Codex hook 输入 JSON 格式差异（字段名映射）

### 不变
- 飞书通知/卡片层完全不变
- Unix Socket 权限协议不变
- `permission.sh` 核心审批逻辑不变
- `/cb/claude/new`、`/cb/claude/continue` 端点 URL 保持不变（语义上已是通用的 agent 会话端点）

## Impact

- Affected specs: `session-continue`, `feishu-command`, `permission-notify`
- Affected code:
  - `src/server/agents/codex.py` (新增)
  - `src/server/agents/__init__.py` (新增 agent 注册/工厂)
  - `src/server/handlers/claude.py` (adapter 实例化)
  - `src/server/config.py` (新配置项)
  - `src/hook-router.sh` (Codex 输入兼容)
  - `src/hooks/stop.sh` (Codex JSONL 解析)
  - `setup_init.py` (Codex hook 配置写入)
  - `.env.example` (新增配置项)
- New spec: `agent-adapter` (定义 agent 适配层的通用要求)
