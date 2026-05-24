## MODIFIED Requirements

### Requirement: Callback 后端继续会话接口

Callback 后端 SHALL 提供 `/cb/claude/continue` 端点，接收并处理继续会话请求，支持 Claude 和 Codex 两种 agent 类型。

#### Scenario: 接收继续会话请求

- **GIVEN** Callback 后端正在运行
- **WHEN** 收到 POST `/cb/claude/continue` 请求
- **AND** 请求包含 `session_id`、`project_dir`、`prompt`
- **AND** 请求可选包含 `claude_command`
- **THEN** 后端验证参数完整性
- **AND** 验证 `project_dir` 目录存在
- **AND** 如果 `claude_command` 非空，验证其在当前 agent adapter 的命令列表中
- **AND** 如果 `claude_command` 为空，从 SessionChatStore 查询 session 记录的 command
- **AND** 通过 `launch_agent(adapter, ...)` 启动 agent 进程
- **AND** 立即返回 `{"status": "processing"}`

#### Scenario: 执行继续会话（Claude）

- **GIVEN** `AGENT_TYPE=claude`
- **AND** 异步线程启动
- **WHEN** 执行 agent 命令
- **THEN** 切换到 `project_dir` 目录
- **AND** 使用确定的 `claude_command`（按优先级：请求指定 > session 记录 > 默认）
- **AND** 通过登录 shell（`bash -lc`）执行，支持 shell 配置文件中的别名和环境变量
- **AND** 拼接 `--print {prompt} --resume {session_id}` 参数
- **AND** 捕获输出用于日志

#### Scenario: 执行继续会话（Codex）

- **GIVEN** `AGENT_TYPE=codex`
- **AND** 异步线程启动
- **WHEN** 执行 agent 命令
- **THEN** 切换到 `project_dir` 目录
- **AND** 使用确定的 codex command
- **AND** 通过登录 shell 执行
- **AND** 拼接 `exec resume {session_id} --json --cd {project_dir} {prompt}` 参数
- **AND** 捕获输出用于日志

#### Scenario: 参数验证失败

- **GIVEN** 收到 `/cb/claude/continue` 请求
- **WHEN** 缺少 `session_id`、`project_dir` 或 `prompt`
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "missing required fields"}`

#### Scenario: 项目目录不存在

- **GIVEN** 收到 `/cb/claude/continue` 请求
- **WHEN** `project_dir` 目录不存在
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "project directory not found"}`

#### Scenario: 自定义命令（Claude）

- **GIVEN** `AGENT_TYPE=claude`
- **AND** 环境变量 `CLAUDE_COMMAND` 设置为 `claude-glm`
- **WHEN** 执行继续会话
- **THEN** 使用 `claude-glm --print {prompt} --resume {session_id}` 执行

#### Scenario: 自定义命令（Codex）

- **GIVEN** `AGENT_TYPE=codex`
- **AND** 环境变量 `CODEX_COMMAND` 设置为 `codex --model o3-pro`
- **WHEN** 执行继续会话
- **THEN** 使用 `codex --model o3-pro exec resume {session_id} --json {prompt}` 执行

#### Scenario: 带参数的自定义命令

- **GIVEN** 环境变量 `CLAUDE_COMMAND` 设置为 `claude --model opus`
- **WHEN** 执行继续会话
- **THEN** 使用 `claude --model opus --print {prompt} --resume {session_id}` 执行

#### Scenario: 向后兼容默认命令

- **GIVEN** `AGENT_TYPE` 未设置（默认 claude）
- **AND** 环境变量 `CLAUDE_COMMAND` 未设置
- **WHEN** 执行继续会话
- **THEN** 使用默认命令 `claude --print {prompt} --resume {session_id}` 执行

#### Scenario: 指定的 Command 不在配置列表中

- **GIVEN** 当前 agent adapter 的命令列表为 `['claude', 'claude --model opus']`
- **AND** 请求中 `claude_command` 为 `unknown-cmd`
- **WHEN** 后端验证 command
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "invalid claude_command"}`

### Requirement: SessionChatStore 扩展存储 Claude Command

Callback 后端的 SessionChatStore SHALL 扩展支持存储每个 session 最近使用的 agent command（字段名保持 `claude_command` 以保持向后兼容），用于后续回复时自动复用。

#### Scenario: 保存 session 的 agent command

- **GIVEN** Callback 后端执行 agent 命令（新建或继续会话）
- **AND** 使用了某个命令（如 `claude --model opus` 或 `codex --model o3-pro`）
- **WHEN** 执行成功（进入 processing 状态）
- **THEN** 调用 `SessionChatStore.save()` 保存 `session_id → {chat_id, claude_command, updated_at}`
- **AND** `claude_command` 为实际使用的命令字符串（不论 agent 类型）

#### Scenario: 查询 session 的 agent command

- **GIVEN** Callback 后端收到继续会话请求
- **AND** 请求未指定 `claude_command`
- **WHEN** 后端查询 `SessionChatStore`
- **THEN** 获取该 session 上次使用的命令
- **AND** 传给当前 agent adapter 的 `resolve_command()` 使用

#### Scenario: 旧数据无 claude_command 字段向后兼容

- **GIVEN** SessionChatStore 中的旧记录不包含 `claude_command` 字段
- **WHEN** 查询该 session 的 `claude_command`
- **THEN** 返回 `None`
- **AND** 系统使用当前 agent adapter 的默认命令

### Requirement: Claude Command 选择优先级

Callback 后端 SHALL 按以下优先级确定使用哪个 agent command：请求指定 > SessionChatStore session 记录 > 当前 agent adapter 默认值。

#### Scenario: 请求指定的 command 最优先

- **GIVEN** `/cb/claude/continue` 请求中包含 `claude_command` 参数
- **AND** SessionChatStore 中该 session 记录的 command 为另一个值
- **WHEN** Callback 后端确定使用哪个命令
- **THEN** 使用请求中指定的 `claude_command`
- **AND** 将新的 command 更新到 SessionChatStore

#### Scenario: SessionChatStore session 记录次优先

- **GIVEN** 请求未指定 `claude_command`
- **AND** SessionChatStore 中该 session 记录的 command 为 `claude --model opus`
- **WHEN** Callback 后端确定使用哪个命令
- **THEN** 使用 SessionChatStore 中记录的 `claude --model opus`

#### Scenario: 默认值兜底

- **GIVEN** 请求未指定 `claude_command`
- **AND** SessionChatStore 中该 session 无 command 记录
- **WHEN** Callback 后端确定使用哪个命令
- **THEN** 使用当前 agent adapter 的默认命令（Claude: `CLAUDE_COMMAND` 列表第一个，Codex: `CODEX_COMMAND` 列表第一个）
