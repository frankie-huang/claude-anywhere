# feishu-command Specification

## Purpose
TBD - created by archiving change add-new-command. Update Purpose after archive.
## Requirements
### Requirement: /new 指令识别

飞书网关 SHALL 能够识别用户发送的 `/new` 指令，并解析工作目录参数、`--cmd` 参数和用户输入的 prompt。

#### Scenario: 识别 /new 指令

- **GIVEN** 飞书网关收到 `im.message.receive_v1` 事件
- **AND** 消息文本内容以 `/new` 开头
- **WHEN** 网关处理该消息
- **THEN** 网关判定为 `/new` 指令
- **AND** 提取指令参数（含 `--dir`、`--cmd`）和 prompt 内容
- **AND** 转发到 Callback 后端 `/cb/claude/new` 接口

#### Scenario: 指令格式解析

- **GIVEN** 用户消息为 `/new --dir=/home/user/project 帮我写一个测试文件`
- **WHEN** 网关解析指令
- **THEN** 提取 `project_dir` 为 `/home/user/project`
- **AND** 提取 `prompt` 为 `帮我写一个测试文件`
- **AND** 保留原始消息的 `message_id` 和 `chat_id`

#### Scenario: 回复模式解析

- **GIVEN** 用户回复某条消息，内容为 `/new 再加个错误处理`
- **AND** 被回复消息的 `parent_id` 在 MessageSessionStore 中有映射
- **WHEN** 网关解析指令
- **THEN** 从 MessageSessionStore 查询 `parent_id` 对应的 `project_dir`
- **AND** 使用查询到的 `project_dir` 作为工作目录
- **AND** 提取 `prompt` 为 `再加个错误处理`

#### Scenario: 回复消息无映射

- **GIVEN** 用户回复某条消息，内容为 `/new 再加个错误处理`
- **AND** 被回复消息的 `parent_id` 在 MessageSessionStore 中无映射
- **WHEN** 网关解析指令
- **THEN** 向飞书发送错误提示：无法获取工作目录，请使用 `/new --dir=/path` 指定
- **AND** 不转发请求到 Callback 后端

#### Scenario: 忽略非指令消息

- **GIVEN** 飞书网关收到消息
- **AND** 消息内容不是 `/new` 开头
- **AND** 消息不包含 `parent_id`
- **WHEN** 网关处理该消息
- **THEN** 忽略该消息
- **AND** 返回成功状态

### Requirement: /new 指令参数格式

`/new` 指令 SHALL 支持 `--dir=` 参数指定工作目录、`--cmd=` 参数指定 Claude Command，同时也支持交互式目录选择。

#### Scenario: 缺少目录参数时发送选择卡片

- **GIVEN** 用户输入 `/new prompt`
- **AND** 没有指定 `--dir=` 参数
- **AND** 不是回复消息
- **WHEN** 解析指令
- **THEN** 发送目录选择卡片
- **AND** 不返回错误提示

#### Scenario: 回复消息无映射时发送选择卡片

- **GIVEN** 用户回复消息，输入 `/new prompt`
- **AND** 被回复消息无项目目录映射
- **WHEN** 解析指令
- **THEN** 发送目录选择卡片
- **AND** 卡片包含原始 prompt 内容

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

### Requirement: 会话创建通知

新建会话成功后，系统 SHALL 发送飞书通知并保存消息映射，支持后续回复继续该会话。

#### Scenario: 发送会话创建通知

- **GIVEN** Callback 后端返回 `{"status": "processing", "session_id": "..."}`
- **WHEN** 飞书网关收到响应
- **THEN** 发送"会话已创建"文本消息到 `chat_id`
- **AND** 消息内容包含 session_id（用于调试）
- **AND** 保存新消息的 `message_id → {session_id, project_dir, callback_url}` 映射

#### Scenario: 后续回复继续新会话

- **GIVEN** 会话已创建
- **AND** 用户回复"会话已创建"通知消息
- **WHEN** 飞书网关收到回复
- **THEN** 查询到 `parent_id` 对应的 session 映射
- **AND** 转发到 `/cb/claude/continue` 接口
- **AND** 使用创建时的 `session_id` 继续会话

### Requirement: 错误处理

系统 SHALL 提供完善的错误处理和友好提示。

#### Scenario: 目录不存在提示

- **GIVEN** 用户输入 `/new --dir=/nonexistent/path prompt`
- **WHEN** Callback 后端验证目录
- **THEN** 返回 `{"error": "project directory not found: /nonexistent/path"}`
- **AND** 飞书网关向用户发送错误提示

#### Scenario: 回复消息无目录映射

- **GIVEN** 用户回复非 Claude 消息，输入 `/new prompt`
- **WHEN** 飞书网关查询 MessageSessionStore
- **THEN** 查询结果为空
- **AND** 发送提示："无法获取工作目录，请使用 `/new --dir=/path/to/project` 格式指定"

#### Scenario: 参数格式错误

- **GIVEN** 用户输入 `/new --dirinvalid prompt`
- **WHEN** 飞书网关解析参数
- **THEN** 发送提示："参数格式错误，正确格式：`/new --dir=/path/to/project prompt`"

### Requirement: 双向认证

`/new` 指令请求 SHALL 复用现有的 auth_token 双向认证机制。

#### Scenario: 网关验证请求 Token

- **GIVEN** 用户发送 `/new` 指令
- **WHEN** 飞书网关处理前验证
- **THEN** 从 sender_id 查询对应的 auth_token
- **AND** 验证 token 有效性
- **AND** 验证失败则返回"您尚未注册，无法使用此功能"

#### Scenario: 转发请求携带 Token

- **GIVEN** auth_token 验证通过
- **WHEN** 转发到 Callback 后端
- **THEN** 在请求头中添加 `X-Auth-Token: {auth_token}`

### Requirement: /new 指令缺少目录时发送选择卡片

当用户执行 `/new` 指令但未指定工作目录时，系统 SHALL 发送飞书交互式卡片，卡片包含常用目录下拉菜单（带浏览按钮）和自定义路径输入框（带浏览按钮）。

#### Scenario: 卡片包含目录选择组件

- **GIVEN** 飞书网关收到 `/new` 指令
- **AND** 指令不包含 `--dir=` 参数
- **WHEN** 构建目录选择卡片
- **THEN** 卡片包含 `select_static` 下拉菜单（常用目录，带浏览按钮）
- **AND** 卡片包含 `input` 输入框（name 为 `custom_dir`，带浏览按钮）
- **AND** input 输入框 label 为"自定义路径"
- **AND** input 输入框 placeholder 为"输入完整路径，如 /home/user/project"
- **AND** 卡片包含提示文本说明优先级："选择子目录 > 自定义路径 > 常用目录"

#### Scenario: 目录选择行与浏览按钮并排布局

- **GIVEN** 构建目录选择卡片
- **WHEN** 渲染常用目录和自定义路径输入框
- **THEN** 常用目录行使用 `column_set` 容器包含三列（标签 + 下拉框 + 浏览按钮）
- **AND** 自定义路径行使用 `column_set` 容器包含三列（标签 + 输入框 + 浏览按钮）
- **AND** 浏览按钮 name 为 `browse_dir_select_btn`（常用目录）或 `browse_custom_btn`（自定义路径）
- **AND** 三列在同一行水平排列，中间控件占据主要空间
- **AND** 创建会话按钮独占一行（全宽主按钮）不参与 column_set

#### Scenario: 无历史目录时卡片仍可用

- **GIVEN** 用户无历史使用记录
- **AND** 常用目录列表为空
- **WHEN** 构建目录选择卡片
- **THEN** 下拉菜单不显示或显示为空
- **AND** 自定义路径输入框正常可用
- **AND** 浏览目录按钮正常可用
- **AND** 用户可以通过输入框或浏览来选择目录

### Requirement: 常用目录历史记录

系统 SHALL 记录用户的工作目录使用历史，用于生成常用目录列表。

#### Scenario: 记录目录使用

- **GIVEN** 用户成功创建会话
- **AND** project_dir 为 "/home/user/project"
- **AND** sender_id 为 "ou_xxx"
- **WHEN** 会话创建完成
- **THEN** 记录该目录使用次数 +1
- **AND** 更新 last_used 时间戳

#### Scenario: 获取常用目录列表

- **GIVEN** 用户有以下使用记录：
  - "/home/user/project1": 使用 5 次，最近 1 天前
  - "/home/user/project2": 使用 3 次，最近 2 小时前
  - "/home/user/project3": 使用 3 次，最近 5 天前
- **WHEN** 查询常用目录（limit=5）
- **THEN** 返回 ["/home/user/project1", "/home/user/project2", "/home/user/project3"]
- **AND** project1 排第一（次数最多）
- **AND** project2 排第二（次数相同，但更近）
- **AND** project3 排第三

#### Scenario: 历史记录自动清理

- **GIVEN** 用户有 25 条不同的目录历史记录
- **AND** 其中 10 条超过 30 天未使用
- **WHEN** 记录新的目录使用
- **THEN** 自动清理超过 30 天的记录
- **AND** 保留最近使用的 20 条记录

### Requirement: 目录选择卡片回调处理

系统 SHALL 处理用户在目录选择卡片中提交的操作，包括创建会话和浏览目录。

#### Scenario: 选择子目录优先级最高

- **GIVEN** 用户在浏览结果下拉框选择了 "/home/user/project/src"
- **AND** 用户同时在自定义路径输入框填写了 "/home/user/project"
- **AND** 用户同时在下拉菜单选择了 "/home/user/old-project"
- **AND** 用户点击"创建会话"按钮
- **WHEN** 处理表单提交
- **THEN** 使用 browse_result 值 "/home/user/project/src" 作为工作目录
- **AND** 忽略 custom_dir 和 directory 的值

#### Scenario: 自定义路径优先于常用目录

- **GIVEN** 用户在自定义路径输入框中填写了 "/home/user/new-project"
- **AND** 用户同时在常用目录下拉菜单选择了 "/home/user/old-project"
- **AND** 用户未选择浏览结果
- **AND** 用户点击"创建会话"按钮
- **WHEN** 处理表单提交
- **THEN** 使用 custom_dir 值 "/home/user/new-project" 作为工作目录
- **AND** 忽略 directory 下拉菜单的值

#### Scenario: 常用目录作为回退

- **GIVEN** 用户未在自定义路径输入框填写内容（custom_dir 为空字符串）
- **AND** 用户未选择浏览结果
- **AND** 用户在常用目录下拉菜单选择了 "/home/user/project"
- **AND** 用户点击"创建会话"按钮
- **WHEN** 处理表单提交
- **THEN** 使用 directory 下拉菜单的值 "/home/user/project" 作为工作目录

#### Scenario: 三者都为空时报错

- **GIVEN** 用户未填写自定义路径
- **AND** 用户未选择常用目录下拉菜单
- **AND** 用户未选择浏览结果
- **AND** 用户点击"创建会话"按钮
- **WHEN** 处理表单提交
- **THEN** 返回 toast 错误提示"请选择或输入一个工作目录"

### Requirement: 卡片动态更新

目录选择卡片 SHALL 支持根据操作结果动态更新显示内容。

#### Scenario: 提交成功后的卡片状态

- **GIVEN** 用户成功创建会话
- **AND** project_dir 为 "/home/user/project"
- **AND** session_id 为 "abc123..."
- **WHEN** 构建成功状态卡片
- **THEN** 卡片标题为"✓ 会话已创建"
- **AND** 内容包含项目目录路径
- **AND** 内容包含完整的 session_id
- **AND** 无任何操作按钮

#### Scenario: 提交失败后的卡片状态

- **GIVEN** 用户选择目录并提交
- **AND** 目录验证失败
- **AND** 错误信息为"目录不存在"
- **WHEN** 构建失败状态卡片
- **THEN** 卡片标题为"✗ 创建失败"
- **AND** 内容显示错误原因
- **AND** 保留下拉菜单和提交按钮
- **AND** 允许用户重新选择

### Requirement: 路径浏览接口

Callback 后端 SHALL 提供 `/cb/directory/browse-dirs` 端点，列出指定路径下的子目录。

#### Scenario: 列出子目录

- **GIVEN** Callback 后端正在运行
- **WHEN** 收到 POST `/cb/directory/browse-dirs` 请求
- **AND** 请求包含 `{"path": "/home/user"}`
- **THEN** 列出 `/home/user` 下的所有子目录（不含文件）
- **AND** 过滤以 `.` 开头的隐藏目录
- **AND** 按目录名字母排序
- **AND** 返回 `{"dirs": ["/home/user/project1", ...], "parent": "/home", "current": "/home/user"}`

#### Scenario: 路径验证

- **GIVEN** 收到 `/cb/directory/browse-dirs` 请求
- **WHEN** path 不是绝对路径
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "path must be absolute"}`

#### Scenario: 路径不存在

- **GIVEN** 收到 `/cb/directory/browse-dirs` 请求
- **WHEN** path 不存在或无法访问
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "path not found or not accessible"}`

#### Scenario: 默认起始路径

- **GIVEN** 收到 `/cb/directory/browse-dirs` 请求
- **WHEN** 请求未包含 path 字段或 path 为空
- **THEN** 默认使用 `/` 作为浏览起始路径

#### Scenario: 认证保护

- **GIVEN** 收到 `/cb/directory/browse-dirs` 请求
- **WHEN** 请求缺少有效的 `X-Auth-Token`
- **THEN** 返回 `401` 状态码
- **AND** 拒绝请求

### Requirement: 浏览目录卡片回调

飞书网关 SHALL 处理目录选择卡片中"浏览目录"按钮的回调，更新卡片显示子目录列表。

#### Scenario: 处理常用目录浏览按钮点击

- **GIVEN** 用户点击常用目录旁的"浏览"按钮
- **AND** 常用目录下拉框选择了 "/home/user"
- **WHEN** 网关收到 form 提交回调
- **AND** 触发按钮 name 为 `browse_dir_select_btn`
- **THEN** 调用 Callback 后端 `/cb/directory/browse-dirs` 接口，路径为下拉框选中的值
- **AND** 返回更新后的卡片，custom_dir 输入框保持原值（如有）
- **AND** 在下方新增子目录列表（`select_static`，name 为 `browse_result`）

#### Scenario: 常用目录未选择时点击浏览按钮报错

- **GIVEN** 用户点击常用目录旁的"浏览"按钮
- **AND** 常用目录下拉框未选择任何值（directory 为空）
- **WHEN** 网关收到 form 提交回调
- **THEN** 返回 toast 错误提示"请先从常用目录中选择一个目录"
- **AND** 卡片不更新

#### Scenario: 处理自定义路径浏览按钮点击

- **GIVEN** 用户点击自定义路径输入框旁的"浏览"按钮
- **AND** custom_dir 输入框值为 "/home/user"
- **WHEN** 网关收到 form 提交回调
- **AND** 触发按钮 name 为 `browse_custom_btn`
- **THEN** 调用 Callback 后端 `/cb/directory/browse-dirs` 接口
- **AND** 返回更新后的卡片，custom_dir 输入框回填为当前浏览路径
- **AND** 在下方新增子目录列表（`select_static`，name 为 `browse_result`）
- **AND** 保持 prompt 输入框的原值（通过 default_value 回填）
- **AND** 保持 column_set 布局（输入框与浏览按钮并排）

#### Scenario: 自定义路径为空时点击浏览按钮

- **GIVEN** 用户点击自定义路径输入框旁的"浏览"按钮
- **AND** custom_dir 输入框为空
- **WHEN** 网关处理浏览请求
- **THEN** 使用 `/` 作为浏览起始路径
- **AND** 列出根目录下的子目录
- **AND** 返回卡片时 custom_dir 输入框回填为 `/`

#### Scenario: 浏览结果展示

- **GIVEN** browse-dirs 返回子目录列表
- **AND** 子目录为 ["/home/user/project1", "/home/user/project2"]
- **WHEN** 构建更新后的卡片
- **THEN** 卡片新增一个 `select_static` 组件（name 为 `browse_result`）
- **AND** 选项列表包含返回的子目录
- **AND** label 显示"浏览结果 (/home/user)"
- **AND** 用户选择浏览结果中的目录后，该值自动填入（通过下次提交的 form_values 传递）

#### Scenario: 浏览结果为空

- **GIVEN** browse-dirs 返回空列表（当前目录无子目录）
- **WHEN** 构建更新后的卡片
- **THEN** 卡片显示提示文本"该目录下没有子目录"
- **AND** 保持其他表单元素不变

#### Scenario: 浏览起始路径为空时的默认行为

- **GIVEN** 用户点击"浏览"按钮
- **AND** custom_dir 输入框为空
- **WHEN** 网关处理浏览请求
- **THEN** 使用 `/` 作为浏览起始路径
- **AND** 列出根目录下的子目录
- **AND** 返回卡片时 custom_dir 输入框回填为 `/`

#### Scenario: 选择浏览结果后创建会话

- **GIVEN** 用户在浏览结果中选择了 "/home/user/project1"
- **AND** custom_dir 输入框为空
- **AND** 用户点击"创建会话"按钮
- **WHEN** 处理表单提交
- **THEN** 使用 browse_result 的值 "/home/user/project1" 作为工作目录

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

### Requirement: /new 指令 --cmd 参数

`/new` 指令 SHALL 支持 `--cmd` 参数，用于从预配置列表中选择 Claude Command。

#### Scenario: 使用索引选择 Command

- **GIVEN** `CLAUDE_COMMAND=[claude, claude --model opus]`
- **AND** 用户发送 `/new --cmd=1 --dir=/path prompt`
- **WHEN** 网关解析指令
- **THEN** 选择索引 1 对应的命令 `claude --model opus`
- **AND** 使用该命令创建会话

#### Scenario: 使用名称匹配 Command

- **GIVEN** `CLAUDE_COMMAND=[claude, claude --model opus]`
- **AND** 用户发送 `/new --cmd=opus --dir=/path prompt`
- **WHEN** 网关解析指令
- **THEN** 在列表中查找包含 `opus` 子串的命令
- **AND** 匹配到 `claude --model opus`
- **AND** 使用该命令创建会话

#### Scenario: --cmd 与 --dir 同时使用

- **GIVEN** 用户发送 `/new --dir=/path --cmd=1 这是 prompt`
- **WHEN** 网关解析指令
- **THEN** 正确提取 `project_dir` 为 `/path`
- **AND** 正确提取 `cmd` 为 `1`
- **AND** 正确提取 `prompt` 为 `这是 prompt`

#### Scenario: --cmd 索引越界

- **GIVEN** `CLAUDE_COMMAND=[claude]`
- **AND** 用户发送 `/new --cmd=5 --dir=/path prompt`
- **WHEN** 网关解析指令
- **THEN** 返回错误提示，包含可用 Command 列表
- **AND** 不创建会话

#### Scenario: --cmd 名称无匹配

- **GIVEN** `CLAUDE_COMMAND=[claude, claude --model opus]`
- **AND** 用户发送 `/new --cmd=haiku --dir=/path prompt`
- **WHEN** 网关解析指令
- **THEN** 返回错误提示，包含可用 Command 列表
- **AND** 不创建会话

#### Scenario: 仅有单命令时 --cmd 可省略

- **GIVEN** `CLAUDE_COMMAND=[claude]`
- **AND** 用户发送 `/new --dir=/path prompt`（不含 --cmd）
- **WHEN** 网关解析指令
- **THEN** 使用默认命令 `claude`

### Requirement: 新建会话卡片 Command 选择

当配置了 Claude Command 时，新建会话卡片 SHALL 包含 Command 选择下拉框。

#### Scenario: 显示 Command 选择器

- **GIVEN** `CLAUDE_COMMAND` 已配置（单命令或多命令）
- **AND** 用户发送 `/new`（触发卡片）
- **WHEN** 构建目录选择卡片
- **THEN** 卡片包含 `select_static` 下拉框（name 为 `claude_command`）
- **AND** 选项包含所有配置的命令
- **AND** 默认选中第一个命令

#### Scenario: 卡片提交时传递选中的 Command

- **GIVEN** 用户在卡片中选择了 `claude --model opus`
- **AND** 用户点击"创建会话"按钮
- **WHEN** 处理表单提交
- **THEN** 将选中的 `claude_command` 传递到 Callback 后端 `/cb/claude/new`

### Requirement: /reply 指令

系统 SHALL 支持 `/reply` 指令，允许用户在回复消息时指定 Claude Command。

#### Scenario: 基本 /reply 格式

- **GIVEN** 用户回复某条 Claude 会话消息
- **AND** 消息内容为 `/reply 继续帮我完善`
- **WHEN** 网关解析指令
- **THEN** 识别为 `/reply` 命令
- **AND** 提取 `prompt` 为 `继续帮我完善`
- **AND** 从 parent_id 映射中获取 session 信息
- **AND** 使用 session 记录的 Claude Command 继续会话

#### Scenario: /reply 带 --cmd 参数

- **GIVEN** 用户回复某条 Claude 会话消息
- **AND** 消息内容为 `/reply --cmd=opus 用 opus 帮我重构`
- **WHEN** 网关解析指令
- **THEN** 识别为 `/reply` 命令
- **AND** 提取 `cmd` 为 `opus`
- **AND** 提取 `prompt` 为 `用 opus 帮我重构`
- **AND** 使用匹配到的 Claude Command 继续会话

#### Scenario: /reply 非回复消息时报错

- **GIVEN** 用户直接发送 `/reply prompt`（不是回复消息）
- **WHEN** 网关处理该消息
- **THEN** 返回错误提示："`/reply` 指令仅支持在回复消息时使用"

#### Scenario: /reply 回复的消息无映射

- **GIVEN** 用户回复非 Claude 会话消息
- **AND** 消息内容为 `/reply prompt`
- **WHEN** 网关查询 MessageSessionStore
- **THEN** 查询结果为空
- **AND** 返回错误提示："无法找到对应的会话（可能已过期或被清理），请重新发起 /new 指令"

### Requirement: --cmd 参数安全约束

`--cmd` 参数 SHALL 仅允许从预配置列表中选择命令，不允许用户自定义命令内容。

#### Scenario: 拒绝自定义命令

- **GIVEN** 用户发送 `/new --cmd="custom-cmd --flag" --dir=/path prompt`
- **AND** `custom-cmd --flag` 不在配置列表中
- **WHEN** 网关解析 --cmd 参数
- **THEN** 尝试名称匹配
- **AND** 无匹配结果时返回错误提示
- **AND** 不执行任何命令

### Requirement: Default Chat Directory

当配置了 `DEFAULT_CHAT_DIR` 环境变量时，系统 SHALL 支持以下行为：

1. 用户发送普通消息（非指令、非回复）时，自动继续活跃的默认会话；无活跃会话时自动创建新会话
2. 用户发送 `/new prompt`（有 prompt 但无 `--dir`）时，在默认目录创建新会话，该会话成为新的活跃默认会话
3. 服务启动时检测默认目录，不存在则自动创建

未配置 `DEFAULT_CHAT_DIR` 时，系统 SHALL 保持所有现有行为不变。

#### Scenario: 普通消息 - 无活跃默认会话
- **WHEN** `DEFAULT_CHAT_DIR` 已配置
- **AND** 当前无活跃的默认会话
- **AND** 用户发送非指令、非回复的普通文本消息
- **THEN** 系统在 `DEFAULT_CHAT_DIR` 目录下创建新 Claude 会话，消息内容作为 prompt
- **AND** 该会话成为活跃的默认会话

#### Scenario: 普通消息 - 有活跃默认会话
- **WHEN** `DEFAULT_CHAT_DIR` 已配置
- **AND** 存在活跃的默认会话
- **AND** 用户发送非指令、非回复的普通文本消息
- **THEN** 系统继续活跃的默认会话，消息内容作为 prompt

#### Scenario: /new prompt 使用默认目录
- **WHEN** `DEFAULT_CHAT_DIR` 已配置
- **AND** 用户发送 `/new prompt`（有 prompt 但无 `--dir` 参数）
- **THEN** 系统在 `DEFAULT_CHAT_DIR` 目录下创建新 Claude 会话
- **AND** 该会话替换为新的活跃默认会话

#### Scenario: /new --dir 不受影响
- **WHEN** `DEFAULT_CHAT_DIR` 已配置
- **AND** 用户发送 `/new --dir=/other/path prompt`
- **THEN** 系统在指定目录创建新会话（现有行为不变）
- **AND** 不影响活跃的默认会话

#### Scenario: /new 无参数不受影响
- **WHEN** `DEFAULT_CHAT_DIR` 已配置
- **AND** 用户发送 `/new`（无参数）
- **THEN** 系统发送目录选择卡片（现有行为不变）

#### Scenario: 未配置默认目录
- **WHEN** `DEFAULT_CHAT_DIR` 未配置（为空）
- **THEN** 所有行为与现有逻辑一致，普通消息发送使用提示，`/new prompt` 发送目录选择卡片

#### Scenario: 服务启动时目录不存在
- **WHEN** `DEFAULT_CHAT_DIR` 已配置
- **AND** 指定的目录路径不存在
- **THEN** 系统在启动时自动创建该目录（包括中间目录）

