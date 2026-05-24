# session-continue Specification

## Purpose
TBD - created by archiving change add-session-continue. Update Purpose after archive.
## Requirements
### Requirement: Message-to-Session Mapping

飞书网关 SHALL 维护 message_id 到 session 信息的映射关系，用于将用户回复消息路由到正确的 Callback 后端。

#### Scenario: Stop 事件发送消息时注册映射

- **GIVEN** Claude stop 事件触发
- **AND** Callback 后端调用飞书网关发送完成通知
- **AND** 请求包含 `session_id`、`project_dir`、`callback_url`
- **WHEN** 飞书网关通过 OpenAPI 发送消息成功
- **THEN** 网关获取返回的 `message_id`
- **AND** 网关保存映射: `message_id → {session_id, project_dir, callback_url, created_at}`
- **AND** 映射保存到 `runtime/message_sessions.json`

#### Scenario: 权限请求发送消息时注册映射

- **GIVEN** Claude 权限请求事件触发
- **AND** Callback 后端调用飞书网关发送权限请求通知
- **AND** 请求包含 `session_id`、`project_dir`、`callback_url`
- **WHEN** 飞书网关通过 OpenAPI 发送消息成功
- **THEN** 网关获取返回的 `message_id`
- **AND** 网关保存映射: `message_id → {session_id, project_dir, callback_url, created_at}`
- **AND** 映射保存到 `runtime/message_sessions.json`

#### Scenario: 查询映射关系

- **GIVEN** 用户回复飞书消息
- **AND** 消息包含 `parent_id`
- **WHEN** 飞书网关查询映射
- **THEN** 网关使用 `parent_id` 作为 key 查询
- **AND** 返回对应的 `{session_id, project_dir, callback_url}`
- **OR** 返回 `null`（如果不存在或已过期）

#### Scenario: 映射自动过期

- **GIVEN** 映射已创建超过 7 天
- **WHEN** 查询该映射
- **THEN** 返回 `null`
- **AND** 映射从存储中删除

#### Scenario: 映射持久化

- **GIVEN** 飞书网关重启
- **WHEN** 网关启动
- **THEN** 从 `runtime/message_sessions.json` 加载已有映射
- **AND** 新发送消息的映射追加到文件

### Requirement: 飞书网关回复消息处理

飞书网关 SHALL 能够识别用户回复消息，并将继续会话请求转发到对应的 Callback 后端。`claude_command` 的选择由 Callback 后端负责（从 SessionChatStore 查询）。

#### Scenario: 识别回复消息

- **GIVEN** 飞书网关收到 `im.message.receive_v1` 事件
- **WHEN** 消息包含 `parent_id` 字段
- **THEN** 网关判定为回复消息
- **AND** 查询 `parent_id` 对应的映射

#### Scenario: 转发到 Callback 后端

- **GIVEN** 查询到有效的映射
- **AND** 提取到用户回复内容
- **WHEN** 飞书网关处理回复消息
- **THEN** POST 请求到 `{callback_url}/cb/claude/continue`
- **AND** 请求 body 包含 `session_id`、`project_dir`、`prompt`
- **AND** 可选包含 `chat_id`、`reply_message_id`
- **AND** 如果用户通过 `/reply --cmd=` 指定了 command，携带 `claude_command` 参数

#### Scenario: 忽略未注册消息

- **GIVEN** 飞书网关收到回复消息
- **AND** `parent_id` 查询不到映射
- **WHEN** 网关处理该消息
- **THEN** 忽略该消息
- **AND** 返回成功状态
- **AND** 记录日志

#### Scenario: Callback 不可达处理

- **GIVEN** 查询到有效映射
- **AND** Callback 后端不可达
- **WHEN** 网关尝试转发请求
- **THEN** 记录错误日志
- **AND** 返回错误状态
- **AND** 不影响飞书事件响应

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
- **AND** 拼接 `exec resume --json {session_id} {prompt}` 参数（不含 `--cd`，工作目录由原始会话决定）
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
- **THEN** 使用 `codex --model o3-pro exec resume --json {session_id} {prompt}` 执行

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

### Requirement: 通知参数传递

stop.sh 和 permission.sh SHALL 在发送通知时传递 session 相关参数，支持后续回复继续会话。

#### Scenario: Stop 事件传递 session 参数

- **GIVEN** Claude stop 事件触发
- **WHEN** `send_stop_notification_async()` 执行
- **THEN** 读取 `CALLBACK_SERVER_URL` 配置
- **AND** 从 transcript 提取 `SESSION_ID`
- **AND** 获取 `PROJECT_DIR`
- **AND** 调用 `send_feishu_card()` 传递这些参数

#### Scenario: 权限请求传递 session 参数

- **GIVEN** Claude 权限请求事件触发
- **WHEN** 发送飞书权限请求卡片
- **THEN** 读取 `CALLBACK_SERVER_URL` 配置
- **AND** 从请求输入中提取 `SESSION_ID`
- **AND** 获取 `PROJECT_DIR`
- **AND** 调用 `send_feishu_card()` 传递这些参数
- **AND** 交互模式和降级模式都支持

#### Scenario: 飞书网关接收参数

- **GIVEN** Callback 后端调用飞书网关 `/gw/feishu/send`
- **AND** 请求包含 `session_id`、`project_dir`、`callback_url`
- **WHEN** 飞书网关处理请求
- **THEN** 提取这些参数
- **AND** 发送成功后保存映射
- **AND** 返回 `{"success": true, "message_id": "..."}`

#### Scenario: 向后兼容

- **GIVEN** Callback 后端调用飞书网关 `/gw/feishu/send`
- **AND** 请求不包含 `session_id` 等参数
- **WHEN** 飞书网关处理请求
- **THEN** 正常发送消息
- **AND** 跳过映射保存
- **AND** 返回成功

### Requirement: 错误处理与日志

系统 SHALL 提供完善的错误处理和日志记录，便于问题排查。

#### Scenario: 会话恢复失败

- **GIVEN** Callback 后端执行 `claude --resume`
- **AND** 会话已过期或不存在
- **WHEN** Claude 命令执行失败
- **THEN** 记录错误日志
- **AND** 不抛出异常
- **AND** 后续 hook 事件可正常发送错误通知

#### Scenario: 命令执行超时

- **GIVEN** Claude 命令执行超过 10 分钟
- **WHEN** 超时触发
- **THEN** 终止子进程
- **AND** 记录超时日志
- **AND** 不影响 Callback 服务运行

#### Scenario: 映射保存失败

- **GIVEN** 飞书网关发送消息成功
- **AND** 映射保存失败（如磁盘满）
- **WHEN** 保存失败
- **THEN** 记录错误日志
- **AND** 消息仍然成功发送
- **AND** 返回成功状态

### Requirement: 过期清理

MessageSessionStore SHALL 支持定期清理过期的映射数据，防止数据无限增长。

#### Scenario: 定时清理过期数据

- **GIVEN** 飞书网关正在运行
- **WHEN** 触发清理任务（如每小时一次）
- **THEN** 扫描所有映射
- **AND** 删除 `created_at` 超过 7 天的条目
- **AND** 更新存储文件
- **AND** 记录清理数量

#### Scenario: 手动清理

- **GIVEN** 管理员需要手动清理
- **WHEN** 调用 `MessageSessionStore.cleanup_expired()`
- **THEN** 返回清理的条目数量
- **AND** 更新存储文件

### Requirement: SessionChatStore 扩展存储 Claude Command 和 Agent Type

Callback 后端的 SessionChatStore SHALL 扩展支持存储每个 session 最近使用的 agent command（字段名保持 `claude_command` 以保持向后兼容）和 agent 类型（`agent_type` 字段），用于后续回复时自动复用。

#### Scenario: 保存 session 的 agent command 和类型

- **GIVEN** Callback 后端执行 agent 命令（新建或继续会话）
- **AND** 使用了某个命令（如 `claude --model opus` 或 `codex --model o3-pro`）
- **WHEN** 执行成功（进入 processing 状态）
- **THEN** 调用 `SessionChatStore.save()` 保存 `session_id → {chat_id, agent_type, claude_command, updated_at}`
- **AND** `agent_type` 为当前 adapter 的类型标识（`'claude'` 或 `'codex'`）
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

#### Scenario: 旧数据无 agent_type 字段向后兼容

- **GIVEN** SessionChatStore 中的旧记录不包含 `agent_type` 字段
- **WHEN** 查询该 session 的 `agent_type`
- **THEN** 默认视为 `'claude'`

### Requirement: SessionChatStore Session ID 重命名

SessionChatStore SHALL 支持将临时 session ID 替换为真实 ID，用于 Codex 路径中从输出捕获的 session ID 替换预生成的临时 UUID。

#### Scenario: 重命名 session ID

- **GIVEN** SessionChatStore 中存在 `old_id` 的记录
- **AND** `new_id` 不存在
- **WHEN** 调用 `rename_session(old_id, new_id)`
- **THEN** 将 `old_id` 的记录迁移到 `new_id`
- **AND** 删除 `old_id` 的记录
- **AND** 返回 `True`

#### Scenario: 新旧 ID 都存在时合并

- **GIVEN** SessionChatStore 中同时存在 `old_id` 和 `new_id` 的记录
- **WHEN** 调用 `rename_session(old_id, new_id)`
- **THEN** 将 `old_id` 的字段合并到 `new_id`（仅补全 `new_id` 缺失的字段）
- **AND** 删除 `old_id` 的记录
- **AND** 返回 `True`

#### Scenario: 旧 ID 不存在

- **GIVEN** SessionChatStore 中不存在 `old_id`
- **WHEN** 调用 `rename_session(old_id, new_id)`
- **THEN** 返回 `False`
- **AND** 记录警告日志

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

### Requirement: 会话消息话题流收敛（链式回复）

系统 SHALL 将同一 Claude 会话的所有飞书消息收敛到一个话题流中，采用链式回复结构。系统维护每个会话的 `last_message_id`，每条新系统通知回复到最近一条消息。

#### Scenario: /new 完整指令场景的链式回复

- **GIVEN** 用户在飞书发送完整的 `/new --dir=/path prompt` 指令
- **WHEN** 飞书网关处理该指令
- **THEN** 将用户发送的 `/new` 消息的 `message_id` 保存到 `MessageSessionStore`
- **AND** 后续系统通知（"会话已创建"等）回复到该 `/new` 消息
- **AND** 发送成功后更新 `last_message_id`

#### Scenario: 终端直接启动场景的链式回复

- **GIVEN** 用户在终端直接启动 Claude Code（非通过飞书 `/new` 指令）
- **AND** 该会话尚无 `last_message_id`
- **WHEN** 首次通过 `/gw/feishu/send` 发送消息（权限请求或完成通知）
- **THEN** 发送新消息（无回复目标）
- **AND** 发送成功后，将返回的 `message_id` 保存为该 session 的 `last_message_id`
- **AND** 后续该会话的消息都使用 reply API 回复到 `last_message_id`

#### Scenario: 链式回复发送系统通知

- **GIVEN** 某 session 已有 `last_message_id`
- **WHEN** 系统需要为该会话发送飞书通知（权限请求、完成通知等）
- **THEN** 使用飞书 reply API (`POST /im/v1/messages/:message_id/reply`) 回复到 `last_message_id`
- **AND** 发送成功后更新 `last_message_id` 为新消息 ID
- **AND** 消息自动出现在话题流中

#### Scenario: 用户消息保存但不更新 last_message_id

- **GIVEN** 用户在话题流中发送消息继续会话
- **WHEN** 飞书网关处理该用户消息
- **THEN** 将用户消息的 `message_id` 保存到 `MessageSessionStore`
- **AND** 不更新 `last_message_id`（保持为最近一条系统消息）
- **AND** 后续系统通知回复到 `last_message_id`（可能是用户消息或系统消息）

#### Scenario: reply 失败时降级为新消息

- **GIVEN** 某 session 已有 `last_message_id`
- **AND** 该消息已被撤回（飞书"删除"仅自己不可见，不影响 reply API）
- **WHEN** reply API 调用失败（错误码 230011）
- **THEN** 降级为使用 send API 发送新消息
- **AND** 记录警告日志
- **AND** 发送成功后更新 `last_message_id`

#### Scenario: 错误通知关联到会话

- **GIVEN** Claude 执行异常（超时、错误等）
- **AND** 存在有效的 `session_id`
- **WHEN** 系统发送错误通知
- **THEN** 错误通知回复到 `last_message_id`（如果存在）
- **AND** 错误通知也收敛到同一话题流中

#### Scenario: 话题内发送消息继续会话

- **GIVEN** 某 session 的消息已收敛到话题流中
- **AND** 话题流中的消息已在 MessageSessionStore 中建立映射
- **WHEN** 用户在话题内直接发送消息（非回复某条具体消息）
- **THEN** 飞书事件的 `parent_id` 为话题根消息或最近一条消息
- **AND** 系统通过 `MessageSessionStore.get(parent_id)` 找到对应的 session
- **AND** 继续该会话

#### Scenario: 话题内回复具体消息继续会话

- **GIVEN** 某 session 的消息已收敛到话题流中
- **WHEN** 用户在话题内回复某条具体的系统消息或用户消息
- **THEN** 飞书事件的 `parent_id` 为被回复的消息 ID
- **AND** 系统通过 `MessageSessionStore.get(parent_id)` 找到对应的 session
- **AND** 继续该会话

#### Scenario: Webhook 模式不支持话题流

- **GIVEN** 系统配置为 `FEISHU_SEND_MODE=webhook`
- **WHEN** 系统发送飞书消息
- **THEN** 忽略 `reply_to_message_id` 参数
- **AND** 使用 Webhook 发送新消息
- **AND** 行为与变更前完全一致

### Requirement: SessionChatStore 扩展存储 last_message_id

SessionChatStore SHALL 扩展支持存储每个 session 的 `last_message_id`，用于链式回复。

#### Scenario: 保存 last_message_id

- **GIVEN** 系统为某 session 发送消息成功
- **WHEN** 系统更新 `last_message_id`
- **THEN** 调用 `SessionChatStore.set_last_message_id(session_id, message_id)`
- **AND** 覆盖该 session 的 `last_message_id`（链式更新）
- **AND** 持久化到 `runtime/session_chats.json`

#### Scenario: 查询 last_message_id

- **GIVEN** 系统需要为某 session 发送飞书消息
- **WHEN** 查询 `SessionChatStore.get_last_message_id(session_id)`
- **THEN** 返回该 session 的 `last_message_id`
- **OR** 返回空字符串（如果未设置）

#### Scenario: 旧数据向后兼容

- **GIVEN** SessionChatStore 中的旧记录不包含 `last_message_id` 字段
- **WHEN** 查询该 session 的 `last_message_id`
- **THEN** 返回空字符串
- **AND** 系统使用默认行为（发送新消息）

### Requirement: /gw/feishu/send 接口支持 reply 模式

飞书网关的 `/gw/feishu/send` 端点 SHALL 支持 `reply_to_message_id` 可选参数，启用时使用 reply API 发送消息。

#### Scenario: 携带 reply_to_message_id 发送卡片

- **GIVEN** 收到 POST `/gw/feishu/send` 请求
- **AND** 请求包含 `reply_to_message_id` 字段（非空）
- **AND** `msg_type` 为 `interactive`
- **WHEN** 飞书网关处理请求
- **THEN** 使用 `FeishuAPIService.reply_card()` 回复到 `reply_to_message_id`
- **AND** 返回 `{"success": true, "message_id": "..."}`

#### Scenario: 携带 reply_to_message_id 发送文本

- **GIVEN** 收到 POST `/gw/feishu/send` 请求
- **AND** 请求包含 `reply_to_message_id` 字段（非空）
- **AND** `msg_type` 为 `text`
- **WHEN** 飞书网关处理请求
- **THEN** 使用 `FeishuAPIService.reply_text()` 回复到 `reply_to_message_id`

#### Scenario: reply 失败降级为 send

- **GIVEN** 收到 POST `/gw/feishu/send` 请求
- **AND** 请求包含 `reply_to_message_id`
- **WHEN** reply API 调用失败
- **THEN** 降级为使用 `send_card()` / `send_text()` 发送新消息
- **AND** 记录警告日志

#### Scenario: 发送成功后更新 last_message_id

- **GIVEN** 收到 POST `/gw/feishu/send` 请求
- **AND** 请求包含有效的 `session_id` 和 `project_dir`
- **WHEN** 消息发送成功
- **THEN** 调用 `set_last_message_id(session_id, message_id)` 更新链式回复状态

#### Scenario: 向后兼容

- **GIVEN** 收到 POST `/gw/feishu/send` 请求
- **AND** 请求不包含 `reply_to_message_id` 字段
- **WHEN** 飞书网关处理请求
- **THEN** 行为与变更前完全一致
- **AND** 使用 `send_card()` / `send_text()` 发送新消息

### Requirement: Shell 脚本传递 last_message_id

Hook 脚本 SHALL 在发送飞书通知时查询并传递 `last_message_id`，使消息收敛到话题流中。

#### Scenario: 查询 last_message_id

- **GIVEN** Hook 脚本需要发送飞书通知
- **AND** 已获取到 `session_id`
- **WHEN** 脚本调用 `_get_last_message_id(session_id)`
- **THEN** 函数调用 `POST /cb/session/get-last-message-id` 接口
- **AND** 返回 `last_message_id`（可能为空）

#### Scenario: 传递 last_message_id 到 /gw/feishu/send

- **GIVEN** 脚本已查询到 `last_message_id`
- **AND** `last_message_id` 非空
- **WHEN** 调用 `send_feishu_card()` 发送卡片
- **THEN** 将 `last_message_id` 作为 `reply_to_message_id` 传递给 `/gw/feishu/send`
- **AND** 消息将作为话题回复发送

#### Scenario: last_message_id 为空时正常发送

- **GIVEN** 脚本查询 `last_message_id` 结果为空
- **WHEN** 调用 `send_feishu_card()` 发送卡片
- **THEN** 不传递 `reply_to_message_id`
- **AND** `/gw/feishu/send` 发送新消息
- **AND** 如果请求包含 `session_id`，发送成功后自动设置 `last_message_id`

### Requirement: Callback 后端提供 last_message_id 查询接口

Callback 后端 SHALL 提供 `POST /cb/session/get-last-message-id` 端点，供 Shell 脚本查询 session 的 last_message_id。

#### Scenario: 查询存在的 last_message_id

- **GIVEN** Callback 后端正在运行
- **AND** SessionChatStore 中某 session 已有 `last_message_id`
- **WHEN** 收到 `POST /cb/session/get-last-message-id` 请求，body 为 `{"session_id": "xxx"}`
- **THEN** 返回 `{"last_message_id": "om_xxx"}`

#### Scenario: 查询不存在的 last_message_id

- **GIVEN** SessionChatStore 中某 session 无 `last_message_id`
- **WHEN** 收到查询请求
- **THEN** 返回 `{"last_message_id": ""}`

#### Scenario: session_id 不存在

- **GIVEN** SessionChatStore 中无该 session_id
- **WHEN** 收到查询请求
- **THEN** 返回 `{"last_message_id": ""}`

#### Scenario: 缺少 session_id 参数

- **GIVEN** 收到 `POST /cb/session/get-last-message-id` 请求
- **AND** 请求 body 中 `session_id` 为空或缺失
- **WHEN** Callback 后端处理请求
- **THEN** 返回 HTTP 400，body 为 `{"last_message_id": ""}`
- **AND** 调用方应将非 200 响应统一处理为空字符串

### Requirement: Callback 后端提供 last_message_id 写入接口

Callback 后端 SHALL 提供 `POST /cb/session/set-last-message-id` 端点，供飞书网关在发送消息成功后同步 `last_message_id`。

在分离部署模式下，SessionChatStore 归属 Callback 后端，飞书网关不能直接调用 `set_last_message_id()`，需通过此 HTTP 接口间接写入。

#### Scenario: 设置 last_message_id 成功

- **GIVEN** Callback 后端正在运行
- **AND** SessionChatStore 中该 session 存在且未过期
- **WHEN** 收到 `POST /cb/session/set-last-message-id` 请求，body 为 `{"session_id": "xxx", "message_id": "om_xxx"}`
- **AND** 请求携带有效的 `X-Auth-Token` header
- **THEN** 更新该 session 的 `last_message_id` 并刷新 `updated_at`
- **AND** 返回 `{"success": true}`

#### Scenario: session 不存在时自动创建

- **GIVEN** SessionChatStore 中无该 session_id（终端直接启动的会话场景）
- **WHEN** 收到 `POST /cb/session/set-last-message-id` 请求
- **THEN** 自动创建该 session 的记录，包含 `last_message_id` 和 `updated_at`
- **AND** 返回 `{"success": true}`

#### Scenario: session 已过期

- **GIVEN** SessionChatStore 中该 session 已过期（超过 7 天未更新）
- **WHEN** 收到设置请求
- **THEN** 返回 HTTP 500，body 为 `{"success": false, "error": "Failed to set last_message_id"}`

#### Scenario: 缺少必要参数

- **GIVEN** 收到 `POST /cb/session/set-last-message-id` 请求
- **AND** `session_id` 或 `message_id` 为空
- **WHEN** Callback 后端处理请求
- **THEN** 返回 HTTP 400，body 为 `{"success": false, "error": "Missing required parameters"}`

#### Scenario: 鉴权失败

- **GIVEN** 收到 `POST /cb/session/set-last-message-id` 请求
- **AND** 请求未携带或携带无效的 `X-Auth-Token` header
- **WHEN** Callback 后端处理请求
- **THEN** 返回 HTTP 401，body 为 `{"error": "Unauthorized"}`

