## MODIFIED Requirements

### Requirement: Callback 后端新建会话接口

Callback 后端 SHALL 提供 `/cb/claude/new` 端点，接收并处理新建会话请求，支持 Claude 和 Codex 两种 agent 类型。

#### Scenario: 执行新建会话（Claude）

- **GIVEN** `AGENT_TYPE=claude`
- **AND** 异步线程启动
- **WHEN** 执行 agent 命令
- **THEN** 切换到 `project_dir` 目录
- **AND** 使用指定的 `claude_command`（或默认命令）
- **AND** 通过登录 shell（`bash -lc`）执行
- **AND** 拼接 `--print {prompt} --session-id {session_id}` 参数
- **AND** 捕获输出用于日志

#### Scenario: 执行新建会话（Codex）

- **GIVEN** `AGENT_TYPE=codex`
- **AND** 异步线程启动
- **WHEN** 执行 agent 命令
- **THEN** 切换到 `project_dir` 目录
- **AND** 使用指定的 codex command（或默认命令）
- **AND** 通过登录 shell 执行
- **AND** 拼接 `exec --json --cd {project_dir} {prompt}` 参数（Codex 不支持指定 session ID）
- **AND** 从 stdout 首条 `thread.started` 事件捕获 session ID
- **AND** 用捕获的真实 session ID 替换 store 中的临时 ID

#### Scenario: 参数验证失败

- **GIVEN** 收到 `/cb/claude/new` 请求
- **WHEN** 缺少 `project_dir` 或 `prompt`
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "missing required fields"}`

### Requirement: Claude Command 多命令配置

系统 SHALL 支持配置多个 agent 命令，用户可在飞书卡片中选择，所有命令 SHALL 通过当前 agent adapter 的 `get_commands()` 获取。

#### Scenario: Claude 多命令配置

- **GIVEN** `AGENT_TYPE=claude`
- **AND** `.env` 中 `CLAUDE_COMMAND=["claude", "claude --model opus"]`
- **WHEN** 飞书卡片需要显示命令选择列表
- **THEN** 从 `ClaudeAdapter.get_commands()` 获取列表
- **AND** 展示 `["claude", "claude --model opus"]`

#### Scenario: Codex 多命令配置

- **GIVEN** `AGENT_TYPE=codex`
- **AND** `.env` 中 `CODEX_COMMAND=["codex", "codex --model o3-pro"]`
- **WHEN** 飞书卡片需要显示命令选择列表
- **THEN** 从 `CodexAdapter.get_commands()` 获取列表
- **AND** 展示 `["codex", "codex --model o3-pro"]`

#### Scenario: 默认单命令

- **GIVEN** 命令配置未设置
- **WHEN** 获取命令列表
- **THEN** Claude adapter 返回 `['claude']`
- **AND** Codex adapter 返回 `['codex']`
