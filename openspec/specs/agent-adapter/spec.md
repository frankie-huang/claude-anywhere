# agent-adapter Specification

## Purpose
TBD - created by archiving change add-codex-support. Update Purpose after archive.
## Requirements
### Requirement: Agent 适配层架构

系统 SHALL 提供 `AgentAdapter` 抽象基类和 `launch_agent()` 共享启动函数，使不同 AI 编码代理（Claude、Codex 等）可以通过统一接口接入系统。

#### Scenario: AgentAdapter 接口定义

- **GIVEN** `src/server/agents/__init__.py` 定义 `AgentAdapter` ABC
- **THEN** 接口 SHALL 包含以下抽象方法/属性：
  - `agent_type` 属性：返回代理标识符（如 `'claude'`、`'codex'`）
  - `display_name` 属性：返回面向用户的产品名（如 `'Claude Code'`、`'Codex'`），用于通知和卡片展示
  - `get_commands()`: 返回可用命令列表
  - `build_command_string(command_name, prompt, session_id, session_mode, project_dir)`: 构建完整 shell 命令
  - `build_debug_command_string(command_name, session_id, session_mode)`: 构建脱敏日志命令
- **AND** 接口 SHALL 包含以下可选覆盖方法（基类提供默认实现）：
  - `needs_output_session_id` 属性：是否需要从进程输出中捕获 session ID（默认 `False`）
  - `session_id_capture_timeout` 属性：从输出捕获 session ID 的超时秒数（默认 `0`）
  - `resolve_command(command_name)`: 解析 agent 命令字符串（默认取 `get_commands()[0]`）
  - `parse_session_id(line)`: 从进程输出行解析 session ID（默认返回 `None`）
  - `build_env(base_env)`: 修改子进程环境变量（默认不修改）

#### Scenario: launch_agent 统一启动

- **GIVEN** 调用方传入 `AgentAdapter` 实例和会话参数
- **WHEN** 调用 `launch_agent(adapter, session_id, project_dir, prompt, ...)`
- **THEN** 系统通过 adapter 构建命令和环境
- **AND** 通过用户 shell 启动子进程
- **AND** 执行启动检查（2 秒内快速完成 / 后台运行）
- **AND** 后台线程监控进程生命周期
- **AND** 失败时通过 `on_error` 回调通知

### Requirement: Agent 类型配置

系统 SHALL 支持通过 `ENABLED_AGENTS` 启用一个或多个 AI 编码代理，并通过 `DEFAULT_AGENT` 指定未显式选择时使用的默认代理。

#### Scenario: 默认启用 Claude

- **GIVEN** `ENABLED_AGENTS` 未设置或为空
- **WHEN** 系统读取 agent 配置
- **THEN** 启用列表为 `['claude']`
- **AND** 默认 agent 为 `claude`

#### Scenario: 同时启用 Claude 和 Codex

- **GIVEN** `ENABLED_AGENTS=claude,codex`
- **AND** `DEFAULT_AGENT=codex`
- **WHEN** 系统读取 agent 配置
- **THEN** 启用列表为 `['claude', 'codex']`
- **AND** 默认 agent 为 `codex`

#### Scenario: 不支持的 agent 类型

- **GIVEN** `ENABLED_AGENTS` 包含不支持的值
- **WHEN** 系统读取 agent 配置
- **THEN** 忽略无效值
- **AND** 如果无有效值则回退为 `['claude']`

### Requirement: Agent 注册与工厂

`agents/__init__.py` SHALL 提供 `get_agent_adapter(agent_type=None)` 工厂函数，按传入的 `agent_type` 返回对应 adapter 单例；未传时使用 `DEFAULT_AGENT`。

#### Scenario: 获取 adapter 实例

- **GIVEN** `agent_type='claude'`
- **WHEN** 调用 `get_agent_adapter('claude')`
- **THEN** 返回 `ClaudeAdapter` 实例
- **AND** 多次调用返回同一实例（单例）

#### Scenario: 获取 Codex adapter

- **GIVEN** `agent_type='codex'`
- **WHEN** 调用 `get_agent_adapter('codex')`
- **THEN** 返回 `CodexAdapter` 实例

#### Scenario: 默认 adapter

- **GIVEN** `DEFAULT_AGENT=codex`
- **WHEN** 调用 `get_agent_adapter()`
- **THEN** 返回 `CodexAdapter` 实例

### Requirement: Codex CLI 适配器

`agents/codex.py` SHALL 实现 `AgentAdapter` 接口，封装 Codex CLI 的命令构建逻辑。

#### Scenario: 新建会话命令构建

- **GIVEN** `session_mode` 为 `'new'`
- **WHEN** 调用 `build_command_string(command_name, prompt, session_id, 'new', project_dir)`
- **THEN** 生成命令字符串包含 `codex exec --json --cd {project_dir} {prompt}`
- **AND** 包含 `--json` 标志用于捕获 session ID

#### Scenario: 恢复会话命令构建

- **GIVEN** `session_mode` 为 `'resume'`
- **WHEN** 调用 `build_command_string(command_name, prompt, session_id, 'resume', project_dir)`
- **THEN** 生成命令字符串包含 `codex exec resume --json {session_id} {prompt}`
- **AND** 不包含 `--cd` 参数（resume 模式工作目录由原始会话决定）

#### Scenario: resolve_command 默认值

- **GIVEN** `CODEX_COMMAND` 配置为 `codex`
- **WHEN** 调用 `resolve_command('')`
- **THEN** 返回 `'codex'`

#### Scenario: get_commands 返回列表

- **GIVEN** `CODEX_COMMAND` 配置为 `[codex, codex --model o3-pro]`
- **WHEN** 调用 `get_commands()`
- **THEN** 返回 `['codex', 'codex --model o3-pro']`

#### Scenario: build_env 无特殊处理

- **WHEN** 调用 `build_env(base_env)`
- **THEN** 返回未修改的环境变量

### Requirement: Codex Session ID 捕获

系统 SHALL 支持从 Codex `--json` 输出中捕获自动生成的 session ID。

#### Scenario: 从 thread.started 事件捕获

- **GIVEN** Codex 进程以 `--json` 模式启动
- **WHEN** stdout 输出 JSONL 事件 `{"type":"thread.started","thread_id":"xxx"}`
- **THEN** 系统通过 daemon 线程逐行读取 stdout 放入 Queue，主线程带超时消费
- **AND** 解析 `thread_id` 字段
- **AND** 通过 `SessionChatStore.rename_session()` 将临时 session ID 替换为真实 `thread_id`

#### Scenario: 捕获超时终止进程

- **GIVEN** stdout 在超时时间内未输出有效的 `thread.started` 事件
- **WHEN** 捕获超时
- **THEN** 终止（kill）子进程并等待退出
- **AND** 返回启动失败错误，提示用户检查 CLI 是否正常
- **AND** 记录错误日志

### Requirement: Codex 配置项

系统 SHALL 支持以下 Codex 专属配置项。

#### Scenario: CODEX_COMMAND 配置

- **GIVEN** `.env` 中设置 `CODEX_COMMAND=codex`
- **WHEN** `CodexAdapter.get_commands()` 被调用
- **THEN** 返回 `['codex']`

#### Scenario: CODEX_COMMAND 默认值

- **GIVEN** `CODEX_COMMAND` 未设置
- **WHEN** `CodexAdapter.get_commands()` 被调用
- **THEN** 返回 `['codex']`（默认值）

#### Scenario: CODEX_ARGS_TEMPLATE 配置

- **GIVEN** `.env` 中设置 `CODEX_ARGS_TEMPLATE={cmd} {args}`
- **WHEN** `CodexAdapter.build_command_string()` 被调用
- **THEN** 使用该模板展开命令字符串

### Requirement: Codex Hook 配置写入

`setup_init.py` SHALL 在 `ENABLED_AGENTS` 包含 `codex` 时，将 hook 配置写入独立的 `~/.codex/hooks.json`，采用「追加」语义保留用户已有 hook，并清理旧版写入 `config.toml` 的残留。

#### Scenario: 写入 Codex hook 配置

- **GIVEN** `ENABLED_AGENTS` 包含 `codex`
- **AND** `~/.codex/hooks.json` 不存在
- **WHEN** 执行 `setup_init.py` 初始化
- **THEN** 创建 `~/.codex/hooks.json`，顶层为 `{"hooks": {...}}` 结构
- **AND** 注册 `UserPromptSubmit`、`PermissionRequest`、`Stop` 三个 hook 事件
- **AND** 每个 hook 的 `type` 为 `command`、`command` 指向 `src/hook-router.sh`
- **AND** `PermissionRequest` hook 的 `timeout` 为 `PERMISSION_REQUEST_TIMEOUT + 60` 秒

#### Scenario: 事件已有其他 hook 时追加共存

- **GIVEN** `~/.codex/hooks.json` 中某事件已配置了用户自己的 hook
- **WHEN** 写入 hook 配置
- **THEN** 将本项目 hook 追加进该事件的数组，与已有 hook 共存
- **AND** 不修改也不移除用户已有的 hook 条目
- **AND** 追加是非破坏操作，无需用户确认

#### Scenario: 本项目 hook 已存在且超时一致

- **GIVEN** `~/.codex/hooks.json` 中某事件已配置指向本项目脚本的 hook
- **AND** `PermissionRequest` 的 `timeout` 等于 `PERMISSION_REQUEST_TIMEOUT + 60`
- **WHEN** 写入 hook 配置
- **THEN** 该事件标记为「无需变更」，不重复追加

#### Scenario: PermissionRequest 超时不一致时确认更新

- **GIVEN** `~/.codex/hooks.json` 中 `PermissionRequest` 已指向本项目脚本
- **AND** 其 `timeout` 与 `PERMISSION_REQUEST_TIMEOUT + 60` 不一致
- **WHEN** 写入 hook 配置
- **THEN** 提示当前值与建议值，让用户选择「更新 / 跳过 / 取消」
- **AND** 选择「更新」时仅原地更新 `timeout` 字段，不覆盖整个 hook 条目

#### Scenario: 不修改 config.toml 中的非 hook 配置

- **GIVEN** `~/.codex/config.toml` 已存在且包含 `[mcp_servers]` 等其他段
- **WHEN** 写入 hook 配置
- **THEN** 不向 `config.toml` 写入任何 hook 配置
- **AND** 保留其中所有非 hook 配置段不变

#### Scenario: 清理旧版 config.toml 中的 inline hooks 残留

- **GIVEN** `~/.codex/config.toml` 中存在旧版写入的 `[[hooks.*]]` 段
- **AND** 这些段的 `command` 指向本项目 `hook-router.sh`
- **WHEN** 执行 `setup_init.py` 初始化
- **THEN** 按 `[[hooks.EVENT.hooks]]` 子条目逐个判定，移除命中本项目脚本的子条目
- **AND** 某事件段的子条目被全部移除时，连同 `[[hooks.EVENT]]` 段头一起丢弃
- **AND** 保留其余配置段
- **AND** 提示已清理旧版残留
- **AND** 避免 `config.toml` 与 `hooks.json` 同时定义导致 hook 重复触发

#### Scenario: 同一事件段内本项目与用户 hook 并存

- **GIVEN** `~/.codex/config.toml` 的同一个 `[[hooks.Stop]]` 段下挂有两个 `[[hooks.Stop.hooks]]` 子条目
- **AND** 其一的 `command` 指向本项目脚本，另一个是用户自己的 hook
- **WHEN** 执行 `setup_init.py` 初始化
- **THEN** 仅移除指向本项目脚本的那个子条目
- **AND** 保留 `[[hooks.Stop]]` 段头与用户自己的子条目

#### Scenario: 残留与用户自己的 hook 分处不同事件段

- **GIVEN** `~/.codex/config.toml` 中某个 `[[hooks.*]]` 段属于本项目，另一个属于用户自己
- **WHEN** 执行 `setup_init.py` 初始化
- **THEN** 仅移除本项目 hook 所在的段
- **AND** 保留用户自己的 hook 段不变

#### Scenario: 无从判定归属的 hook 段一律保留

- **GIVEN** `~/.codex/config.toml` 中某 `[[hooks.*]]` 段不含 `command` 行（如非 `command` 类型的 hook，或 `command` 行被注释掉）
- **WHEN** 执行 `setup_init.py` 初始化
- **THEN** 保留该段不变
- **AND** 不因同文件中存在本项目残留而连带移除该段

#### Scenario: 保留 Codex 自身维护的 hooks 状态段

- **GIVEN** `~/.codex/config.toml` 中存在 Codex 自己写入的单括号 `[hooks.state]` 段（记录 hook 信任状态的 `trusted_hash`）
- **WHEN** 清理旧版 inline hooks 残留
- **THEN** 保留 `[hooks.state]` 及其下所有条目
- **AND** 不论该段位于 `[[hooks.*]]` 定义段之前、之后还是之间，结果一致

#### Scenario: 识别数组写法的 command

- **GIVEN** 旧版残留的 `command` 写成数组形式 `["bash", "-c", "<hook-router.sh 路径>"]`
- **WHEN** 执行 `setup_init.py` 初始化
- **THEN** 识别出该段指向本项目脚本并移除

#### Scenario: 提示 Codex hook 信任审查

- **GIVEN** 本次初始化实际写入了 `hooks.json`
- **WHEN** 写入完成
- **THEN** 提示用户重启 Codex 并在 `/hooks` 中信任本项目 hook
- **AND** 说明非托管 hook 默认不会自动运行

### Requirement: Codex 权限审批路径

Codex SHALL 通过原生 `PermissionRequest` hook 实现权限审批，共享 `permission.sh` 核心逻辑。

#### Scenario: Codex 权限请求流程

- **GIVEN** Codex 进程遇到需要权限的工具调用
- **WHEN** Codex 触发 `PermissionRequest` hook
- **THEN** 调用 `hook-router.sh` → `permission.sh`
- **AND** `permission.sh` 发送飞书审批卡片
- **AND** 等待用户决策
- **AND** 返回 allow/deny 决策

#### Scenario: Codex 不使用 MCP 桥接

- **GIVEN** 当前 agent 为 `codex`
- **WHEN** `CodexAdapter.build_command_string()` 被调用
- **THEN** 命令字符串中不包含 `--permission-prompt-tool` 和 `--mcp-config` 参数

### Requirement: Codex JSONL Transcript 解析

`stop.sh` SHALL 支持从 Codex 格式的 JSONL transcript 中提取最终回复文本。

#### Scenario: 检测 Codex JSONL 格式

- **GIVEN** `transcript_path` 指向的 JSONL 文件
- **WHEN** 首条记录包含 `"type": "thread.started"`
- **THEN** 使用 Codex 解析逻辑

#### Scenario: 提取 Codex 最终回复

- **GIVEN** Codex JSONL 文件包含多条事件
- **WHEN** 解析 `type: "item.completed"` 且 `item.type: "agent_message"` 的记录
- **THEN** 提取最后一条 `agent_message` 的 `item.text` 作为最终回复

#### Scenario: Codex JSONL 无 agent_message

- **GIVEN** Codex JSONL 文件中没有 `agent_message` 类型的 item
- **WHEN** 解析回复
- **THEN** 返回空字符串
- **AND** 不触发错误

### Requirement: Hook 输入 JSON 兼容

`hook-router.sh` SHALL 兼容 Claude 和 Codex 两种 hook 输入 JSON 格式。

#### Scenario: Claude hook 输入

- **GIVEN** Hook 由 Claude CLI 触发
- **WHEN** hook-router.sh 接收 stdin JSON
- **THEN** JSON 包含 `session_id`、`transcript_path`、`cwd`、`tool_name` 等字段
- **AND** 正常分发到对应 hook 脚本

#### Scenario: Codex hook 输入

- **GIVEN** Hook 由 Codex CLI 触发
- **WHEN** hook-router.sh 接收 stdin JSON
- **THEN** JSON 字段与 Claude 格式一致（`session_id`、`transcript_path`、`cwd` 等）
- **AND** hook-router.sh 从 `transcript_path` 推断 AGENT_TYPE（`/.codex/sessions/` → codex，`/.claude/` → claude）
- **AND** 导出 `AGENT_TYPE` 环境变量供下游 hook 脚本使用
- **AND** 正常分发到对应 hook 脚本
