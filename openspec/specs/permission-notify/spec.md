# permission-notify Specification

## Purpose
TBD - created by archiving change add-permission-notify. Update Purpose after archive.
## Requirements
### Requirement: Permission Request Notification Script

系统 SHALL 提供 `permission-notify.sh` 脚本，当 Claude Code 发起权限请求时，通过飞书 Webhook 通知用户。

#### Scenario: Bash 命令权限请求通知

- **GIVEN** Claude Code 请求执行 Bash 命令的权限
- **WHEN** PermissionRequest hook 触发并调用 `permission-notify.sh`
- **THEN** 脚本从 stdin 读取 JSON 数据
- **AND** 解析出 `tool_name` 为 "Bash"，`tool_input.command` 为待执行命令
- **AND** 发送飞书卡片消息，包含命令内容

#### Scenario: 文件修改权限请求通知

- **GIVEN** Claude Code 请求修改文件的权限（Edit 或 Write）
- **WHEN** PermissionRequest hook 触发并调用 `permission-notify.sh`
- **THEN** 脚本从 stdin 读取 JSON 数据
- **AND** 解析出 `tool_name` 为 "Edit" 或 "Write"，`tool_input.file_path` 为目标文件
- **AND** 发送飞书卡片消息，包含文件路径信息

#### Scenario: 通用工具权限请求通知

- **GIVEN** Claude Code 请求使用其他工具的权限
- **WHEN** PermissionRequest hook 触发并调用 `permission-notify.sh`
- **THEN** 脚本从 stdin 读取 JSON 数据
- **AND** 发送飞书卡片消息，显示工具名称和基本请求信息

#### Scenario: AskUserQuestion 权限请求

- **GIVEN** Claude Code 触发 AskUserQuestion 权限请求
- **WHEN** PermissionRequest hook 触发
- **THEN** 系统构建带表单的飞书卡片（而非仅通知卡片）
- **AND** 卡片包含问题选项的下拉选择和自定义输入框
- **AND** 系统通过 socket 等待用户回调（而非直接回退终端）

### Requirement: Feishu Card Message Format

通知消息 SHALL 使用飞书交互式卡片格式，包含以下信息：

#### Scenario: 卡片消息内容完整

- **GIVEN** 权限请求事件发生
- **WHEN** 发送飞书通知
- **THEN** 卡片标题显示 "@用户 🙋 请求使用 **{工具名}** 工具"
- **AND** 卡片包含项目名称（从 `CLAUDE_PROJECT_DIR` 或 `cwd` 获取）
- **AND** 卡片包含请求时间戳
- **AND** 卡片包含工具类型（Bash/Edit/Write 等）
- **AND** 卡片包含请求详情（命令内容或文件路径）
- **AND** 卡片包含请求 ID（用于关联回调）
- **AND** 卡片包含操作提示（如 "请尽快操作以避免 Claude 超时"）
- **AND** 卡片包含 4 个操作按钮（批准/始终允许/拒绝/拒绝并中断）

### Requirement: Error Handling

脚本 SHALL 具备容错能力，确保不会阻塞 Claude Code 正常运行。脚本 SHALL 在无 jq 环境下使用原生 Linux 命令（grep、sed、awk）解析 JSON，功能与 jq 解析一致。**脚本 SHALL 通过统一的函数库实现 JSON 解析，避免重复代码。**

#### Scenario: 无 jq 环境下使用原生命令解析

- **GIVEN** 系统未安装 jq
- **WHEN** 脚本接收到 PermissionRequest hook 的 JSON 数据
- **THEN** `lib/json.sh` 中的 `json_get()` 函数自动使用 grep、sed 等原生命令解析 JSON
- **AND** 正确提取 `tool_name` 字段
- **AND** 正确提取 `tool_input` 中的嵌套字段（command、file_path、pattern 等）
- **AND** 通知内容与使用 jq 解析时完全一致

#### Scenario: jq 可用时优先使用

- **GIVEN** 系统已安装 jq
- **WHEN** 脚本接收到 JSON 数据
- **THEN** `lib/json.sh` 中的 `json_init()` 检测到 jq 可用
- **AND** `json_get()` 函数优先使用 jq 解析（更可靠）
- **AND** 不使用原生命令解析

#### Scenario: JSON 解析失败降级处理

- **GIVEN** stdin 输入的 JSON 格式无效或缺少必要字段
- **WHEN** 脚本尝试解析 JSON（无论使用 jq 还是原生命令）
- **THEN** 脚本发送降级通知消息，说明有权限请求但无法解析详情
- **AND** 脚本以退出码 0 结束，不阻塞 Claude Code

#### Scenario: Webhook 请求失败静默处理

- **GIVEN** 飞书 Webhook URL 不可达或返回错误
- **WHEN** `lib/feishu.sh` 中的 `send_feishu_card()` 尝试发送通知
- **THEN** 函数记录错误日志
- **AND** 脚本以退出码 0 结束
- **AND** 不影响 Claude Code 继续运行

### Requirement: Hook Configuration

系统 SHALL 根据 `AGENT_TYPE` 在对应 agent 的配置文件中注册 hook。

#### Scenario: Claude hook 配置

- **GIVEN** `AGENT_TYPE=claude`
- **WHEN** 执行 hook 配置初始化
- **THEN** 在 `~/.claude/settings.json` 中注册 `UserPromptSubmit`、`PermissionRequest`、`Stop` hook
- **AND** hook command 指向 `src/hook-router.sh`

#### Scenario: Codex hook 配置

- **GIVEN** `AGENT_TYPE=codex`
- **WHEN** 执行 hook 配置初始化
- **THEN** 在 `~/.codex/config.toml` 中注册 `UserPromptSubmit`、`PermissionRequest`、`Stop` hook
- **AND** hook command 指向 `src/hook-router.sh`
- **AND** `PermissionRequest` hook 的 timeout 为 `PERMISSION_REQUEST_TIMEOUT + 60` 秒

### Requirement: Interactive Card Buttons

通知消息卡片 SHALL 包含可交互按钮（URL 跳转类型），支持用户直接控制权限请求。按钮行为与 Claude Code 终端权限操作保持一致。

#### Scenario: 卡片包含批准按钮

- **GIVEN** 权限请求卡片发送成功
- **WHEN** 用户查看卡片
- **THEN** 卡片包含"批准运行"按钮
- **AND** 按钮配置为 URL 跳转类型
- **AND** URL 为 `{CALLBACK_SERVER_URL}/allow?id={request_id}`

#### Scenario: 卡片包含始终允许按钮

- **GIVEN** 权限请求卡片发送成功
- **WHEN** 用户查看卡片
- **THEN** 卡片包含"始终允许"按钮
- **AND** 按钮配置为 URL 跳转类型
- **AND** URL 为 `{CALLBACK_SERVER_URL}/always?id={request_id}`

#### Scenario: 卡片包含拒绝按钮

- **GIVEN** 权限请求卡片发送成功
- **WHEN** 用户查看卡片
- **THEN** 卡片包含"拒绝运行"按钮
- **AND** URL 为 `{CALLBACK_SERVER_URL}/deny?id={request_id}`

#### Scenario: 卡片包含拒绝并中断按钮

- **GIVEN** 权限请求卡片发送成功
- **WHEN** 用户查看卡片
- **THEN** 卡片包含"拒绝并中断"按钮
- **AND** URL 为 `{CALLBACK_SERVER_URL}/interrupt?id={request_id}`

### Requirement: HTTP Callback Server

系统 SHALL 提供 HTTP 回调服务，接收飞书卡片按钮的 URL 跳转请求，并将用户决策传递给 permission-notify.sh。

#### Scenario: 服务启动并监听

- **GIVEN** 回调服务配置完成
- **WHEN** 启动回调服务
- **THEN** 服务监听 HTTP 端口（默认 8080）
- **AND** 服务监听 Unix Domain Socket `/tmp/claude-permission.sock`
- **AND** 服务记录启动日志

#### Scenario: 接收批准请求

- **GIVEN** 回调服务正在运行
- **AND** 存在待处理的权限请求 ID
- **WHEN** 用户点击"批准运行"按钮，浏览器访问 `/allow?id={request_id}`
- **THEN** 服务验证请求 ID 有效
- **AND** 将 `allow` 决策发送给对应的等待进程
- **AND** 返回确认页面给浏览器
- **AND** 使请求 ID 失效（防止重复操作）

#### Scenario: 接收始终允许请求

- **GIVEN** 回调服务正在运行
- **AND** 存在待处理的权限请求 ID
- **WHEN** 用户点击"始终允许"按钮，浏览器访问 `/always?id={request_id}`
- **THEN** 服务验证请求 ID 有效
- **AND** 将 `allow` 决策发送给对应的等待进程
- **AND** 根据工具类型生成权限规则（如 `Bash(npm run build)`）
- **AND** 将规则写入项目的 `.claude/settings.local.json`
- **AND** 返回确认页面给浏览器

#### Scenario: 请求 ID 关联

- **GIVEN** 回调服务管理多个待处理的权限请求
- **WHEN** 收到 HTTP 请求
- **THEN** 服务根据请求 ID 定位对应的等待进程
- **AND** 将决策传递给正确的进程

### Requirement: Always Allow Rule Persistence

系统 SHALL 在用户点击"始终允许"时，将权限规则持久化到项目配置。

#### Scenario: 写入 Bash 命令规则

- **GIVEN** 用户点击"始终允许"
- **AND** 权限请求为 Bash 工具，命令为 `npm run build`
- **WHEN** 回调服务处理请求
- **THEN** 服务读取项目的 `.claude/settings.local.json`（不存在则创建）
- **AND** 在 `permissions.allow` 数组中添加 `Bash(npm run build)`
- **AND** 保存文件

#### Scenario: 写入文件操作规则

- **GIVEN** 用户点击"始终允许"
- **AND** 权限请求为 Edit 工具，文件路径为 `/path/to/file.js`
- **WHEN** 回调服务处理请求
- **THEN** 服务在 `permissions.allow` 数组中添加 `Edit(/path/to/file.js)`

### Requirement: Permission Script Integration

permission.sh SHALL 作为 Claude 和 Codex 的共享权限审批脚本，两种 agent 通过不同路径触发但共享同一套审批逻辑。

#### Scenario: Claude 权限审批路径

- **GIVEN** `AGENT_TYPE=claude`
- **WHEN** Claude CLI 遇到需要权限的工具调用
- **THEN** 通过 `--permission-prompt-tool` MCP 工具触发
- **AND** `permission_mcp.py` 调用 `hook-router.sh` → `permission.sh`
- **AND** `permission.sh` 发送飞书审批卡片并等待用户决策

#### Scenario: Codex 权限审批路径

- **GIVEN** `AGENT_TYPE=codex`
- **WHEN** Codex CLI 遇到需要权限的工具调用
- **THEN** 通过原生 `PermissionRequest` hook 直接触发
- **AND** 调用 `hook-router.sh` → `permission.sh`
- **AND** `permission.sh` 发送飞书审批卡片并等待用户决策
- **AND** 不经过 `permission_mcp.py`

#### Scenario: 审批卡片展示兼容

- **GIVEN** 权限请求来自 Codex
- **AND** `tool_name` 为 Codex 工具名（如 `shell`、`apply_patch`）
- **WHEN** `permission.sh` 构建飞书审批卡片
- **THEN** 卡片正常展示 Codex 工具名和工具参数
- **AND** 用户可以正常点击批准/拒绝按钮

### Requirement: Connection-Based Request Validity

系统 SHALL 基于实际连接状态处理请求有效性，而非人为设定的超时时间。请求有效性完全由 Claude Code 与回调服务之间的 socket 连接状态决定。

#### Scenario: 客户端超时导致连接断开

- **GIVEN** 权限请求卡片已发送
- **WHEN** Claude Code hook 等待超时导致 socket 连接断开
- **THEN** permission-notify.sh 因 socat 超时返回 `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "权限请求超时，自动拒绝"}}}`

#### Scenario: 连接断开后按钮点击处理

- **GIVEN** Claude Code 已不再等待（socket 连接已断开）
- **WHEN** 用户之后点击按钮访问 URL
- **THEN** 回调服务返回"连接已断开，Claude 可能已继续执行其他操作"页面

#### Scenario: 长时间等待后点击仍有效

- **GIVEN** 权限请求卡片已发送
- **AND** Claude Code 仍在等待用户响应（socket 连接保持）
- **WHEN** 用户在任意时间点击按钮
- **THEN** 回调服务正常处理请求
- **AND** 将决策发送给 Claude Code

#### Scenario: 请求已被处理后再次点击

- **GIVEN** 权限请求已被用户处理（批准/拒绝）
- **WHEN** 用户再次点击按钮访问 URL
- **THEN** 回调服务返回"请求已被处理"页面
- **AND** 显示之前的处理结果（如"已批准"）

### Requirement: Graceful Degradation

系统 SHALL 在回调服务不可用时降级为仅通知模式。

#### Scenario: 服务不可用时降级

- **GIVEN** 回调服务未启动或 Socket 连接失败
- **WHEN** permission-notify.sh 执行
- **THEN** 脚本检测到服务不可用
- **AND** 脚本使用原有 Webhook 方式发送通知（不含交互按钮）
- **AND** 脚本不返回决策（让用户在终端操作）
- **AND** 脚本以退出码 0 结束

### Requirement: Response Page

回调服务 SHALL 在用户点击按钮后返回确认页面。

#### Scenario: 显示操作成功页面

- **GIVEN** 用户点击按钮且请求 ID 有效
- **WHEN** 回调服务处理完成
- **THEN** 返回 HTML 页面显示"操作成功"
- **AND** 页面显示操作类型（如"已批准运行"）

#### Scenario: 显示请求无效页面

- **GIVEN** 用户访问的请求 ID 不存在
- **WHEN** 回调服务处理请求
- **THEN** 返回 HTML 页面显示"请求不存在或已被清理"

#### Scenario: 显示连接已断开页面

- **GIVEN** 用户访问的请求 ID 对应的 socket 连接已断开
- **WHEN** 回调服务处理请求
- **THEN** 返回 HTML 页面显示"连接已断开，Claude 可能已继续执行其他操作"

#### Scenario: 显示重复操作页面

- **GIVEN** 用户访问的请求 ID 已被处理
- **WHEN** 回调服务处理请求
- **THEN** 返回 HTML 页面显示"请求已被{批准/拒绝}，请勿重复操作"

### Requirement: Shell Script Modular Architecture

系统 SHALL 采用模块化架构组织 Shell 脚本代码，将复用逻辑抽离为独立的函数库。

#### Scenario: JSON 解析函数库

- **GIVEN** 系统需要解析 JSON 数据
- **WHEN** 任何 Shell 脚本需要读取 JSON 字段
- **THEN** 脚本引用 `lib/json.sh` 函数库
- **AND** 使用统一的 `json_get()` 函数获取字段值
- **AND** 函数内部自动选择可用的解析工具（jq > python3 > grep/sed）
- **AND** 调用方无需关心具体解析实现

#### Scenario: 飞书卡片函数库

- **GIVEN** 系统需要发送飞书通知
- **WHEN** 需要构建飞书卡片消息
- **THEN** 脚本引用 `lib/feishu.sh` 函数库
- **AND** 使用 `build_permission_card()` 构建权限请求卡片
- **AND** 使用 `send_feishu_card()` 发送卡片
- **AND** 交互式和降级模式复用相同的卡片模板

#### Scenario: 工具详情函数库

- **GIVEN** 系统需要格式化不同工具类型的请求详情
- **WHEN** 需要提取 Bash/Edit/Write/Read 等工具的参数
- **THEN** 脚本引用 `lib/tool.sh` 函数库
- **AND** 使用 `extract_tool_detail()` 获取格式化的详情内容
- **AND** 新增工具类型仅需修改配置数组，无需改动提取逻辑

#### Scenario: 日志函数库

- **GIVEN** 系统需要记录日志
- **WHEN** 任何脚本需要输出日志
- **THEN** 脚本引用 `lib/log.sh` 函数库
- **AND** 使用统一的 `log()` 函数记录日志
- **AND** 日志自动包含时间戳和调用位置

### Requirement: Python Service Modular Architecture

系统 SHALL 采用模块化架构组织 Python 服务代码，实现关注点分离。

#### Scenario: HTTP 处理器模块分离

- **GIVEN** 回调服务需要处理 HTTP 请求
- **WHEN** 用户点击飞书卡片按钮
- **THEN** 请求由 `handlers/callback.py` 模块处理
- **AND** 处理器仅负责路由和参数解析
- **AND** 业务逻辑委托给 services 层

#### Scenario: 请求管理服务模块

- **GIVEN** 系统需要管理待处理的权限请求
- **WHEN** permission-notify.sh 发起请求
- **THEN** 请求由 `services/request_manager.py` 管理
- **AND** 模块负责请求注册、状态跟踪、连接管理
- **AND** 与 HTTP 处理器解耦

#### Scenario: 规则写入服务模块

- **GIVEN** 用户点击"始终允许"按钮
- **WHEN** 系统需要写入权限规则
- **THEN** 规则由 `services/rule_writer.py` 模块处理
- **AND** 模块负责规则格式化和文件写入
- **AND** 与请求管理模块解耦

#### Scenario: 决策模型独立

- **GIVEN** 系统需要构造决策响应
- **WHEN** 需要返回 allow/deny 决策
- **THEN** 使用 `models/decision.py` 中的 Decision 类
- **AND** 类提供 `allow()` 和 `deny()` 工厂方法
- **AND** 决策格式符合 Claude Code Hook 规范

### Requirement: Tool Type Configuration

系统 SHALL 使用配置驱动的方式管理工具类型，支持灵活扩展。

#### Scenario: 工具类型配置集中管理

- **GIVEN** 系统支持多种工具类型（Bash, Edit, Write, Read, Glob, Grep 等）
- **WHEN** 需要处理特定工具的权限请求
- **THEN** 工具类型的颜色、字段映射、规则格式集中配置
- **AND** 新增工具类型仅需添加配置项
- **AND** 无需修改核心处理逻辑

#### Scenario: Shell 脚本工具配置

- **GIVEN** permission-notify.sh 需要处理工具详情
- **WHEN** 提取工具参数用于显示
- **THEN** 使用 `lib/tool.sh` 中的配置数组
- **AND** `TOOL_COLORS` 映射工具类型到卡片颜色
- **AND** `TOOL_FIELDS` 映射工具类型到参数字段名

#### Scenario: Python 服务工具配置

- **GIVEN** 回调服务需要生成权限规则
- **WHEN** 用户点击"始终允许"
- **THEN** 使用 `RULE_FORMATTERS` 字典生成规则字符串
- **AND** 新增工具类型仅需添加格式化函数

### Requirement: VSCode Auto Redirect Configuration

系统 SHALL 支持通过环境变量配置 VSCode 自动跳转功能。

#### Scenario: 配置 VSCode Remote 前缀

- **GIVEN** 用户在 `.env` 文件中配置 `VSCODE_URI_PREFIX`
- **WHEN** 回调服务启动
- **THEN** 服务读取该配置
- **AND** 配置值为 VSCode URI 前缀（如 `vscode://vscode-remote/ssh-remote+server`）
- **AND** 不包含项目路径部分

#### Scenario: 未配置时禁用跳转

- **GIVEN** 用户未配置 `VSCODE_URI_PREFIX` 环境变量
- **WHEN** 用户点击飞书卡片按钮并处理完成
- **THEN** 响应页面显示操作结果
- **AND** 不执行 VSCode 跳转
- **AND** 保持当前的自动关闭页面行为

### Requirement: Notification Delay

系统 SHALL 支持配置权限通知延迟发送，避免快速连续请求时的消息轰炸。

#### Scenario: 配置延迟时间

- **GIVEN** 用户在 `.env` 文件中配置 `PERMISSION_NOTIFY_DELAY=3`
- **WHEN** permission-notify.sh 收到权限请求
- **THEN** 脚本等待 3 秒后再发送飞书通知
- **AND** 默认值为 0（立即发送）

#### Scenario: 延迟期间用户终端响应

- **GIVEN** 用户配置了通知延迟（如 3 秒）
- **AND** 权限请求正在延迟等待中
- **WHEN** 用户在终端选择 yes/no 响应权限请求
- **THEN** Claude Code 发送 SIGKILL 终止 hook 进程
- **AND** 飞书通知不会发送

#### Scenario: 延迟期间父进程退出

- **GIVEN** 用户配置了通知延迟
- **AND** 权限请求正在延迟等待中
- **WHEN** 用户通过 Ctrl+C 中断 Claude Code
- **THEN** 脚本检测到父进程退出
- **AND** 脚本跳过发送飞书通知
- **AND** 脚本以退出码 1 结束

#### Scenario: 延迟完成后正常发送

- **GIVEN** 用户配置了通知延迟（如 3 秒）
- **AND** 权限请求正在延迟等待中
- **WHEN** 延迟时间结束且用户未响应
- **THEN** 脚本正常发送飞书通知
- **AND** 继续后续的交互流程

### Requirement: Response Page VSCode Redirect

回调服务响应页面 SHALL 在配置启用时自动跳转到 VSCode。

#### Scenario: 操作成功后跳转 VSCode

- **GIVEN** 用户已配置 `VSCODE_URI_PREFIX`
- **AND** 权限请求包含有效的项目路径（`project_dir`）
- **WHEN** 用户点击飞书卡片按钮（批准/拒绝/始终允许/拒绝并中断）
- **AND** 回调服务成功处理决策
- **THEN** 响应页面显示操作成功信息
- **AND** 页面在短暂延迟（约 500ms）后自动跳转到 VSCode
- **AND** VSCode URI 格式为 `{VSCODE_URI_PREFIX}{project_dir}`

#### Scenario: 跳转后聚焦终端

- **GIVEN** VSCode 跳转成功执行
- **WHEN** VSCode 打开项目
- **THEN** 用户可以在 VSCode 中查看 Claude Code 终端的执行结果
- **AND** 终端保持之前的聚焦状态（依赖用户的 VSCode 布局设置）

#### Scenario: 操作失败时不跳转

- **GIVEN** 用户已配置 `VSCODE_URI_PREFIX`
- **WHEN** 回调服务处理决策失败（请求无效、连接断开等）
- **THEN** 响应页面显示错误信息
- **AND** 不执行 VSCode 跳转

#### Scenario: 页面显示跳转提示

- **GIVEN** 用户已配置 `VSCODE_URI_PREFIX`
- **AND** 决策处理成功
- **WHEN** 响应页面加载
- **THEN** 页面显示"正在跳转到 VSCode..."的提示信息
- **AND** 用户可以看到跳转即将发生

#### Scenario: 跳转失败时显示手动链接

- **GIVEN** 用户已配置 `VSCODE_URI_PREFIX`
- **AND** 决策处理成功
- **AND** 页面尝试跳转到 VSCode
- **WHEN** 跳转超时（页面仍未离开，如 2 秒后）
- **THEN** 页面显示"跳转失败"的错误提示
- **AND** 页面显示可点击的 VSCode URI 链接
- **AND** 用户可以手动点击链接打开 VSCode

### Requirement: OpenAPI Message Sending Mode

系统 SHALL 支持通过飞书 OpenAPI 发送消息，作为 Webhook 的替代或补充方式。**分离部署下，消息发送统一通过飞书网关。**

#### Scenario: Webhook 模式（默认）

- **GIVEN** 用户配置 `FEISHU_SEND_MODE=webhook` 或未配置该变量
- **AND** 未配置 `FEISHU_GATEWAY_URL`
- **WHEN** 系统需要发送飞书消息
- **THEN** 使用 `FEISHU_WEBHOOK_URL` 通过 Webhook 发送
- **AND** 不需要配置应用凭证

#### Scenario: OpenAPI 模式（单机）

- **GIVEN** 用户配置 `FEISHU_SEND_MODE=openapi`
- **AND** 未配置 `FEISHU_GATEWAY_URL`
- **AND** 配置了 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`
- **AND** 配置了 `FEISHU_OWNER_ID`
- **WHEN** 系统需要发送飞书消息
- **THEN** 通过飞书 OpenAPI 发送消息到指定接收者
- **AND** 自动管理 access_token（2小时有效期，提前5分钟刷新）

#### Scenario: 分离部署

- **GIVEN** 用户配置了 `FEISHU_GATEWAY_URL`
- **WHEN** 系统需要发送飞书消息
- **THEN** POST 请求到 `${FEISHU_GATEWAY_URL}/gw/feishu/send`
- **AND** 网关负责实际的消息发送
- **AND** Callback 服务无需配置飞书凭证

#### Scenario: 接收者类型自动检测

- **GIVEN** 用户配置了 `FEISHU_OWNER_ID`
- **WHEN** 系统需要确定接收者类型
- **THEN** 根据 ID 前缀自动检测：`ou_` 为 open_id，`oc_` 为 chat_id，`on_` 为 union_id，包含 `@` 为 email，其他为 user_id

### Requirement: Feishu Send HTTP Endpoint

回调服务 SHALL 提供 `/gw/feishu/send` HTTP 端点，支持通过 OpenAPI 发送飞书消息。

#### Scenario: 发送卡片消息

- **GIVEN** 回调服务正在运行
- **AND** 飞书 OpenAPI 服务已初始化
- **WHEN** 收到 POST `/gw/feishu/send` 请求，body 包含 `{"msg_type": "interactive", "content": {...}}`
- **THEN** 通过 OpenAPI 发送卡片消息到配置的接收者
- **AND** 返回 `{"success": true, "message_id": "..."}` 或 `{"success": false, "error": "..."}`

#### Scenario: 发送文本消息

- **GIVEN** 回调服务正在运行
- **AND** 飞书 OpenAPI 服务已初始化
- **WHEN** 收到 POST `/gw/feishu/send` 请求，body 包含 `{"msg_type": "text", "content": "..."}`
- **THEN** 通过 OpenAPI 发送文本消息到配置的接收者

#### Scenario: 服务未启用时拒绝请求

- **GIVEN** 飞书 OpenAPI 服务未初始化（缺少凭证配置）
- **WHEN** 收到 POST `/gw/feishu/send` 请求
- **THEN** 返回 `{"success": false, "error": "Feishu API service not enabled"}`

### Requirement: 飞书网关分离部署支持

系统 SHALL 支持将飞书网关与 Callback 服务分离部署，通过 `FEISHU_GATEWAY_URL` 配置启用分离部署。

#### Scenario: 检测分离部署

- **GIVEN** 用户配置了 `FEISHU_GATEWAY_URL` 环境变量
- **WHEN** permission.sh 初始化
- **THEN** 系统启用分离部署
- **AND** 消息发送使用 `${FEISHU_GATEWAY_URL}/gw/feishu/send`
- **AND** 按钮 value 中包含 `callback_url: ${CALLBACK_SERVER_URL}`

#### Scenario: 未配置时使用单机模式

- **GIVEN** 用户未配置 `FEISHU_GATEWAY_URL`
- **WHEN** permission.sh 初始化
- **THEN** 系统使用原有的单机模式逻辑
- **AND** 根据 `FEISHU_SEND_MODE` 决定消息发送方式

### Requirement: 卡片按钮携带 Callback URL

飞书卡片按钮的 value MUST 包含 `callback_url` 字段，用于飞书网关路由决策请求。

#### Scenario: 按钮 value 包含 callback_url

- **GIVEN** 系统需要发送权限请求卡片
- **WHEN** 构建按钮 JSON
- **THEN** 每个按钮的 `value` 包含 `callback_url` 字段
- **AND** `callback_url` 值为 `${CALLBACK_SERVER_URL}`

#### Scenario: 向后兼容

- **GIVEN** 飞书网关未配置或使用旧版本
- **WHEN** 网关直接处理 card.action.trigger 事件
- **THEN** 网关可以忽略 `callback_url` 字段
- **AND** 直接使用本地决策处理逻辑

### Requirement: 多消息响应内容聚合

Stop 事件通知 SHALL 提取最近一条用户文本消息之后所有 assistant 消息的文本内容，而非仅最后一条 assistant 消息。

#### Scenario: 用户消息后有多条 assistant text 消息

- **GIVEN** JSONL transcript 中最近一条用户文本消息之后存在多条 `type=="assistant"` 消息
- **AND** 每条 assistant 消息的 `message.content` 中包含 `type=="text"` 块
- **WHEN** Stop 事件提取响应内容
- **THEN** 系统收集所有这些 text 块的文本内容
- **AND** 使用双换行符 `\n\n` 拼接为完整响应

#### Scenario: 识别用户文本消息

- **GIVEN** JSONL transcript 包含多种类型的 user 消息
- **WHEN** 系统查找最近一条用户文本消息
- **THEN** 系统从末尾向前遍历，找到第一条满足以下条件的 `type=="user"` 消息：
  - `message.content` 为非空字符串，或
  - `message.content` 数组中包含 `type=="text"` 的元素（排除仅含 `tool_result` 的消息）

#### Scenario: 无用户文本消息时回退

- **GIVEN** JSONL transcript 中不存在符合条件的用户文本消息
- **WHEN** Stop 事件提取响应内容
- **THEN** 系统从所有 assistant 消息中收集 text 内容
- **OR** 回退到 subagents 目录查找

### Requirement: 思考过程提取与展示

Stop 事件通知 SHALL 支持提取并展示 Claude 的思考过程（thinking），使用飞书折叠面板组件呈现。

#### Scenario: 提取 thinking 内容

- **GIVEN** assistant 消息的 `message.content` 中包含 `type=="thinking"` 块
- **WHEN** Stop 事件提取响应内容
- **THEN** 系统同时收集 thinking 块的 `thinking` 字段文本
- **AND** 多条 thinking 使用双换行符 `\n\n` 拼接

#### Scenario: 飞书卡片展示 thinking

- **GIVEN** 提取到非空的 thinking 内容
- **WHEN** 构建 Stop 事件飞书卡片
- **THEN** 卡片在"答复"区域之前展示思考过程
- **AND** 使用飞书 `collapsible_panel` 组件
- **AND** 面板默认折叠（`expanded: false`）
- **AND** 面板标题为"思考过程"

#### Scenario: 无 thinking 内容时不展示

- **GIVEN** 未提取到 thinking 内容（thinking 为空字符串）
- **WHEN** 构建 Stop 事件飞书卡片
- **THEN** 卡片不包含思考过程区域
- **AND** 卡片布局与当前版本完全一致

#### Scenario: thinking 内容截断

- **GIVEN** thinking 内容长度超过 `STOP_THINKING_MAX_LENGTH` 配置值
- **WHEN** 构建通知内容
- **THEN** thinking 内容截断到配置长度
- **AND** 末尾添加 "..."

#### Scenario: 禁用 thinking 展示

- **GIVEN** `STOP_THINKING_MAX_LENGTH` 配置为 `0`
- **WHEN** Stop 事件提取响应内容
- **THEN** 跳过 thinking 内容提取
- **AND** 卡片不包含思考过程区域

### Requirement: STOP_THINKING_MAX_LENGTH 配置

系统 SHALL 支持 `STOP_THINKING_MAX_LENGTH` 环境变量配置 thinking 内容的最大展示长度。

#### Scenario: 默认值

- **GIVEN** 未配置 `STOP_THINKING_MAX_LENGTH`
- **WHEN** 系统读取配置
- **THEN** 使用默认值 `5000` 字符

#### Scenario: 自定义值

- **GIVEN** `.env` 文件中配置 `STOP_THINKING_MAX_LENGTH=1000`
- **WHEN** 系统读取配置
- **THEN** 使用配置值 `1000` 作为最大长度

### Requirement: AskUserQuestion 飞书卡片审批

当 Claude Code 触发 AskUserQuestion 权限请求时，系统 SHALL 构建带表单的飞书卡片，支持用户在飞书中远程选择/输入答案，并将答案通过 `updatedInput.answers` 返回给 Claude Code。

#### Scenario: 单选问题渲染为下拉选择

- **GIVEN** AskUserQuestion 的 `tool_input.questions` 包含一个 `multiSelect: false` 的问题
- **AND** 该问题有多个 `options`
- **WHEN** 系统构建飞书卡片
- **THEN** 卡片包含一个 `form` 表单
- **AND** 该问题渲染为 `select_static` 下拉组件
- **AND** 下拉选项包含所有 `options[].label`（展示为 `label - description`）
- **AND** 同时渲染一个 `input` 文本框（placeholder: "或者自定义输入（填写后会覆盖本题选项）"）

#### Scenario: 多选问题渲染为多选下拉

- **GIVEN** AskUserQuestion 的 `tool_input.questions` 包含一个 `multiSelect: true` 的问题
- **WHEN** 系统构建飞书卡片
- **THEN** 该问题渲染为 `multi_select_static` 多选下拉组件
- **AND** 同时渲染一个 `input` 文本框（placeholder: "可在此补充自定义内容"）

#### Scenario: 多问题在同一表单中渲染

- **GIVEN** AskUserQuestion 的 `questions` 数组包含多个问题
- **WHEN** 系统构建飞书卡片
- **THEN** 所有问题在同一个 `form` 中按顺序渲染
- **AND** 每个问题的表单字段名使用索引区分（`q_0_select`, `q_0_custom`, `q_1_select`, `q_1_custom`）
- **AND** 表单底部有"提交回答"、"拒绝回答"和"拒绝并中断"三个按钮

#### Scenario: 用户选择预设选项并提交

- **GIVEN** 用户在飞书卡片下拉中选择了一个预设选项且未填写自定义输入
- **WHEN** 用户点击"提交回答"按钮
- **THEN** 回调服务器从 `form_value` 中提取选择
- **AND** 构造 `answers` dict，key 为 question 文本，value 为选中的 option label
- **AND** permission.sh 输出带 `updatedInput` 的决策 JSON
- **AND** Claude Code 收到答案并继续执行

#### Scenario: 用户选择自定义输入并提交

- **GIVEN** 用户在文本框中输入了自定义内容（无论是否选择了下拉选项）
- **WHEN** 用户点击"提交回答"按钮
- **THEN** 单选时：自定义输入优先覆盖下拉选项值
- **AND** 多选时：自定义输入追加到选中的选项列表中
- **AND** permission.sh 输出带自定义答案的 `updatedInput` 决策 JSON

#### Scenario: 用户点击拒绝回答按钮

- **GIVEN** 用户不想回答这个问题
- **WHEN** 用户点击"拒绝回答"按钮
- **THEN** 回调服务器返回 deny 决策
- **AND** permission.sh 输出 `{"behavior": "deny"}`
- **AND** Claude Code 收到拒绝，AskUserQuestion 返回空答案

#### Scenario: 用户点击拒绝并中断按钮

- **GIVEN** 用户不想回答这个问题并想中断当前会话
- **WHEN** 用户点击"拒绝并中断"按钮
- **THEN** 回调服务器返回 deny + interrupt 决策
- **AND** permission.sh 输出 `{"behavior": "deny", "message": "用户拒绝并中断", "interrupt": true}`
- **AND** Claude Code 收到拒绝并中断当前会话

#### Scenario: 用户未操作超时（交互模式）

- **GIVEN** 用户在交互模式下运行 Claude Code
- **AND** 飞书卡片已发送但用户未点击任何按钮
- **WHEN** 等待超时
- **THEN** permission.sh 以 `EXIT_FALLBACK` 退出
- **AND** Claude Code 回退到终端交互模式

#### Scenario: 用户未操作超时（MCP 非交互模式）

- **GIVEN** 用户在非交互模式下运行 Claude Code（`claude --print`）
- **AND** 飞书卡片已发送但用户未点击任何按钮
- **WHEN** 等待超时
- **THEN** permission.sh 返回 deny 决策（默认行为）
- **AND** Claude Code 收到空答案

#### Scenario: updatedInput 响应格式

- **GIVEN** 回调服务器已构造好 answers dict
- **WHEN** permission.sh 输出决策
- **THEN** 输出的 JSON 格式为：
  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "PermissionRequest",
      "decision": {
        "behavior": "allow",
        "updatedInput": {
          "answers": {"问题文本": "选中的label或自定义文本"},
          "questions": [/* 原始 questions 数组 */]
        }
      }
    }
  }
  ```

