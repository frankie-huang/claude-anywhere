# Changelog

All notable changes to this project will be documented in this file.

## [Released]

### Added - 2026-04-30

#### 新增 FEISHU_AT_BOT_ONLY 配置：群聊 @bot 过滤

- 新增 `FEISHU_AT_BOT_ONLY` per-user 配置项（默认 `false`），控制群聊中是否仅响应 @bot 的消息
- 设为 `true` 时，群聊中非 @bot 的消息（含命令）静默忽略；P2P 单聊不受影响
- 若飞书应用未开通"获取群组中所有消息"权限，效果等同 `true`
- 配置通过注册链路（HTTP / WS / 自动注册）透传至 BindingStore，每个用户独立生效

#### 飞书群聊模式（Group Chat Mode）

新增 `FEISHU_SESSION_MODE` 配置项，支持三种会话消息隔离方式：

- **message**（默认）：普通消息模式，所有消息在同一聊天中
- **thread**：话题模式，消息回复到话题详情中（向后兼容 `FEISHU_REPLY_IN_THREAD=true`）
- **group**：群聊模式，每个 Claude 会话自动创建独立飞书群聊

**群聊生命周期管理：**

- 自动创建群聊：`/new` 或 Shell 脚本启动时通过 `ensure-chat` 懒创建，群名格式 `{前缀} - {目录名} - {时间}`
- 自动解散：空闲超过 `FEISHU_GROUP_DISSOLVE_DAYS` 天的群聊自动解散（每小时检查）
- 手动解散：`/groups dissolve 1 2 3` 按序号解散或 `/groups dissolve all` 全部解散
- 群聊列表：`/groups` 列出当前用户所有活跃群聊（序号、目录、活跃时间）

**新增用户命令：**

- `/attach <session_id 前缀>` — 将 session 绑定到当前群聊（跨群迁移会话）
- `/clear` — 清空当前群聊会话上下文，下次发消息自动创建新 Claude 会话
- `/mute` — 静音当前会话，后续消息不再推送（发任意文字消息自动解除）
- `/unmute` — 手动解除静音
- `/groups` — 管理群聊（列表 / 解散）

**消息路由统一（SessionFacade）：**

- 新增 `SessionFacade` 统一入站消息路由门面：parent_id 回复 → group chat_id 反查 → 默认聊天目录
- group 模式群内消息自动路由到对应 session，无需回复特定消息
- 出站消息静音拦截：`/mute` 后 Claude 继续运行但消息不推送到飞书

**Session 管理增强：**

- `dissolved` 标记：群解散后 session 软失效，`ensure-chat` 自动重建，`/attach` 自动复活
- `muted` 标记：出站消息拦截，稳态下命中内存缓存零 RPC
- `/clear` 通过 session clone 继承旧 session 的 `project_dir` + `claude_command`
- Session 过期时间从 7 天调整为可配置的 `SESSION_EXPIRE_DAYS`（默认 30 天）

**新增存储层：**

- `GroupChatStore`（`group_chats.json`）：群聊归属 + per-owner 序号（seq），网关侧
- `GroupSessionStore`（`group_sessions.json`）：chat_id → 活跃 session 路由表，网关侧
- `TTLCache`（内存）：通用 TTL 缓存工具类

**飞书 API 新增能力：**

- `create_group_chat()`：创建群聊 + 拉入用户（需 `im:chat` 权限）
- `add_chat_members()`：添加群成员
- `dissolve_group_chat()`：解散群聊
- `patch_card()`：更新已发送的卡片消息
- `_is_at_bot()`：通过 mentions 精确检测消息是否 @了机器人

**注册链路升级：**

- 整个注册链路（HTTP / WS / 自动注册）中 `reply_in_thread` 参数升级为 `session_mode`
- 透传 `group_name_prefix` 和 `group_dissolve_days` 到 BindingStore
- 向后兼容旧客户端的 `reply_in_thread` 字段（自动映射为 `session_mode=thread`）

**新增配置项：**

- `FEISHU_SESSION_MODE`：会话模式（message / thread / group）
- `FEISHU_GROUP_NAME_PREFIX`：群聊名称前缀（默认 `Claude`）
- `FEISHU_GROUP_DISSOLVE_DAYS`：群聊空闲自动解散天数（默认 0 = 不自动解散）
- `SESSION_EXPIRE_DAYS`：Session 过期天数（默认 30）

### Fixed - 2026-04-23

#### 修复 AskUserQuestion 回答在分离部署下"请求不存在或已过期"的跨端查询 bug

- 飞书卡片回调在**网关侧**进程处理，但 `_handle_ask_question_answer` 原先直接读本进程的 `RequestManager.get_request_data(request_id)` 获取 `questions_encoded`
- `RequestManager` 只在 **callback 后端**（hooks 通过 Unix Socket 注册请求的进程）中持有数据，分离部署（`IS_CALLBACK_BACKEND=False`）时网关侧本地单例是空的，用户点"提交回答"必然得到"请求不存在或已过期"
- 网关侧改为仅透传 `form_value` + `request_id` 到 `/cb/decision`，questions 解码与 answers 构造全部下沉到 callback 端完成
- `_handle_ask_question_answer` 去掉本地 `RequestManager` 依赖及 `base64`/`questions` 解码逻辑，仅保留 toast 文案与卡片更新（两者都只依赖 form_value，不跨端）；新增私有 `_apply_custom_overrides` 集中处理"单选 custom 覆盖 select"的判定与清理（仅依赖 form_value 的字段命名约定）
- callback 的 `/cb/decision` 在 `action=answer` 分支从 `RequestManager` 本地解码 `questions_encoded`；新增私有 `_extract_answers_from_form_value` 将 form_value 压扁为 `{question_text: answer}` 回给 Claude hook
- `/cb/decision` 同步收紧：`action=answer` 只认 `form_value`，移除 `answers`/`questions` 入参（旧接口已无调用方）

> ⚠️ 升级顺序：**先升级 callback 后端，再升级飞书网关**。`/cb/decision` 已不再接受旧的 `answers`/`questions` 字段，反序升级会导致旧网关提交的 answer 请求在新 callback 上失败。

### Fixed - 2026-04-22

#### 模板渲染改用字面替换，修复含反斜杠或 `&` 的 value 产出非法 JSON

- `src/lib/feishu.sh` 的 `render_template` 原使用 awk `gsub` 做占位符替换，第二参数中 `\` 与 `&` 具有特殊语义
- 旧实现仅预转义 `&`，当 value 含反斜杠（如 JSON 转义后的 `\\&`）时会被 `gsub` 解释为 `\&` 输出，生成非法 JSON 转义序列
- 改为 `index` + `substr` 做字面拼接，不经过正则引擎
- 顺带规避 `{{key}}` 中 `{` `}` 在部分 awk 实现下被当作 ERE 量词的风险

### Fixed - 2026-04-02

#### 统一 Python 3 环境检测，修复 setup 与运行时依赖不一致

- 所有 shell 脚本中的裸 `python3` 调用统一替换为 `$PYTHON3` 变量
- 新增 `find_python3()`（install.sh）和 `_init_python3()`（core.sh）按 7 级优先级检测 Python 3：
  `.env PYTHON_PATH` > 项目 `.venv` > 激活 venv > 激活 conda > pyenv > PATH python3 > PATH python
- `setup.sh` 配置时将检测到的 Python 路径持久化到 `.env` 的 `PYTHON_PATH`
- `start-server.sh` 启动时验证 `.env` 中的 `PYTHON_PATH` 是否与检测结果一致，不一致时警告
- `.env` 中 `PYTHON_PATH` 无效时 `setup.sh` 报错退出，提示用户修正或清空
- 新增 `.env.example` 的 `PYTHON_PATH` 配置项
- 新增 `README.md` Python 环境检测文档

### Fixed - 2026-03-31

#### build_shell_cmd 注入当前 PATH 保持一致性

- login shell 重新加载 profile 可能改变 PATH 顺序，导致找到不同版本的二进制
- 在构造 shell 命令时注入当前进程的 PATH，覆盖 profile 设置的值
- fish shell 使用 `set -x PATH` 语法特殊处理，PATH 值均通过 `shlex.quote` 安全转义

### Changed - 2026-03-31

#### 文档与配置清理

- 文档中 `FEISHU_EVENT_MODE` 示例统一标注为注释，明确一般无需配置（auto 自动选择）
- 文档/示例中 `claude --setting opus` 统一修正为 `claude --model opus`
- `PERMISSION_REQUEST_TIMEOUT` 移除"0=禁用"语义，改为正整数校验（无效值回退默认值 600）

### Improved - 2026-03-30

#### install.sh 卸载流程增强与维护命令

- `--uninstall` 从仅移除 hook 配置升级为完整卸载流程：确认提示、停止服务、移除配置、可选清理 runtime/log/.env
- 卸载时遍历所有 hook 事件（不再硬编码事件名），更健壮
- 新增 `--clean-cache` 命令，清理 Python `__pycache__` 缓存目录

### Added - 2026-03-29

#### UserPromptSubmit 事件：终端 Prompt 同步到飞书话题

- 新增 `UserPromptSubmit` hook handler（`src/hooks/user_prompt.sh`），终端发起的 prompt 自动同步到飞书话题
- 飞书发起的 prompt 通过 `skip_next_user_prompt` 标志自动跳过，避免重复通知
- 新增 `send_feishu_post()` 富文本消息发送，支持 session threading 链式回复和 @机器人
- 飞书网关 `/gw/feishu/send` 新增 post 富文本消息类型和 `add_typing` 表情选项
- `stop.sh` 新增 openapi 发送模式直通（不再强制依赖 webhook URL）

#### 注册流程传递 bot_open_id

- 网关注册时通过 `FeishuAPIService.get_bot_info()` 获取机器人 open_id
- HTTP callback 和 WS auth_ok 两条注册路径均附带 bot_open_id
- `AuthTokenStore.save()` 将 bot_open_id 持久化到 runtime 文件，供发消息时 @机器人使用

### Added - 2026-03-28

#### 权限卡片工具内容预览

- Edit 工具权限卡片展示 diff 详情（删除/新增对比）
- Write 工具权限卡片展示写入内容预览

#### 工具内容截断优化

- 提升工具内容截断上限至 5000 字符
- 截断时展示截断提示，告知用户内容被截断
- Stop 通知内容截断时追加截断提示

### Changed - 2026-03-28

- 截断提示从内容拼接改为模板独立渲染

### Added - 2026-03-27

#### /users 管理员指令

- 新增 `/users` 管理员指令，支持查看用户在线状态

### Changed - 2026-03-27

#### 日志目录重构

- 日志文件按组件分子目录存放，便于分类管理
- 日期格式改为 YYYY-MM-DD

### Changed - 2026-03-26

#### 默认聊天目录话题跟随配置 (default-chat-follow-thread)

- 新增 `DEFAULT_CHAT_FOLLOW_THREAD` 配置项（默认 `true`）
- **行为变更**：默认聊天目录的回复现在默认跟随 `FEISHU_REPLY_IN_THREAD` 全局配置
  - 旧版：default chat dir 回复始终在主界面显示（不收敛进话题）
  - 新版（默认）：跟随全局配置，若 `FEISHU_REPLY_IN_THREAD=true` 则收敛进话题
- 设置 `DEFAULT_CHAT_FOLLOW_THREAD=false` 可恢复旧版行为（始终在主界面显示）

#### update 子命令优化

- 改进输出格式，更清晰地展示配置差异
- 增强错误处理，更新失败时提供明确提示

### Changed - 2026-03-25

#### 网关连接逻辑重构

- 添加部署模式标识，区分单机/分离模式
- 优化网关连接错误提示

### Changed - 2026-03-24

#### 运行时文件统一迁移

- 将运行时文件（日志、绑定数据、遥测数据等）统一迁移到 `runtime/` 目录
- 简化项目结构，便于管理和备份

### Added - 2026-03-23

#### 遥测功能

- 新增遥测模块，包含客户端和服务端
- 客户端定期上报心跳（默认每小时），统计活跃用户
- 支持版本更新检测
- 新增 `/api/telemetry/heartbeat` 和 `/api/telemetry/stats` 端点
- 速率限制（client_id + IP 双重限流）
- 支持 `TELEMETRY_ENABLED=false` 关闭

### Fixed - 2026-03-23

- 拒绝并中断时不再添加 Typing 表情（中断操作后任务会停止，不需要显示"正在处理"状态）

### Changed - 2026-03-22

#### 项目重命名

- 项目从 `claude-notify` 重命名为 `claude-anywhere`

#### 安全增强

- `/status` 端点添加 `X-Auth-Token` 认证
- 使用 `--` 分隔符防止 prompt 中的参数被 CLI 误解析

#### UI 优化

- 优化 AskUserQuestion 卡片显示样式

### Added - 2026-03-21

#### 审批/回答回调响应优化

- 审批/回答成功后直接返回更新后的卡片
- 自动禁用按钮、回填表单、更新状态

### Changed - 2026-03-21

- 使用内存缓存替代按钮回调传递卡片 JSON，优化内存占用

### Added - 2026-03-20

#### 权限审批卡片增强

- 权限审批卡片和 AskUserQuestion 卡片底部显示 `claude --resume` 命令
- 卡片审批/回答成功后添加 Typing 表情反馈

#### AskUserQuestion 飞书表单提交

- 支持 AskUserQuestion 通过飞书表单提交回答
- 支持单选、多选、自定义输入远程审批
- 新增 `ask-question-card.json` 模板

### Fixed - 2026-03-17

#### 卡片表格超限处理

- 修正卡片表格超限错误码识别
- 飞书卡片表格超限时自动降级，markdown 表格转代码块重试

#### 安装与绑定逻辑修复

- 安装时检查 claude 命令可用性
- 修复 WS 隧道模式绑定清理逻辑
- claude 会话后台监控改为 30 秒启动检查，超时后 detach 而非 kill

#### 目录使用记录优化

- 将目录使用记录从会话启动时改为通知发送成功后触发，减少无效记录
- 添加公共 `json_escape` 函数，支持 JSON 规范转义
- `get_recent_dirs` 添加 `min_count` 参数过滤低频目录（默认≥2次）
- 常用目录 limit 增加到 20，移除 MAX_DIRS 硬限制

### Added - 2026-03-16

#### 未注册用户提示

- 未注册用户发送消息时提示注册命令
- 改进用户体验

#### MCP 权限审批服务

- 添加 MCP 权限审批服务，支持 headless 模式下飞书权限审批
- 通过 `--permission-prompt-tool` MCP 方案桥接到现有飞书审批系统
- 适用于远程/CI 场景的权限控制

### Added - 2026-03-15

#### 注册通知

- 用户注册/换绑时发送通知给网关管理员

### Added - 2026-03-14

#### 单机模式 WS 隧道

- 单机模式统一使用 WS 隧道通信
- 修复进程管理与异常处理的潜在问题

### Added - 2026-03-13

#### 安装检查与消息发送稳定性

- `install.sh`: 新增超时配置检测，提示 Hook 超时应大于服务端超时
- `feishu.py`: 卡片发送失败时降级发送文本错误提示
- `binding_store.py`: 修复换绑设备时 session_id 未清除的问题
- `feishu_api.py`: 支持更多敏感信息错误码 (230028 DLP审查)

### Fixed - 2026-03-12

- 使用 `os.path.realpath` 规范化 `default_chat_dir` 路径比较

### Added - 2026-03-11

#### 敏感信息脱敏重试

- 消息发送支持敏感信息自动脱敏重试
- 新增脱敏正则：身份证、手机号、座机号、邮箱
- 敏感内容被拦截后自动脱敏重试

#### 一键安装脚本

- 新增 `setup.sh` 支持单机模式和分离模式的自动化安装
- 更新 README 和 QUICKSTART 文档

### Fixed - 2026-03-11

- 修复换绑 HTTP 后仍被旧 WS 连接拦截的问题

### Added - 2026-03-08

#### 飞书 WebSocket 长连接模式

- 新增 `FEISHU_EVENT_MODE` 配置（auto/http/longpoll）
- 新增 `feishu_longpoll.py` 长连接服务
- 通过 lark-oapi SDK 的 ws.Client 建立长连接接收飞书事件推送
- 网关无需公网端点和 HTTPS 证书，适用于本地开发和内网部署场景

#### auth_token 安全增强

- auth_token 生成改用 `FEISHU_APP_SECRET` 作为签名密钥

### Added - 2026-03-07

#### WebSocket 隧道功能

- 添加 WebSocket 隧道功能，支持本地 Claude Code 直连网关
- 本地开发无需公网 IP，通过 WS 隧道与网关通信
- POST 路由处理函数改为纯函数签名，WS 隧道直接调用后端路由

### Added - 2026-03-02

#### 默认聊天目录回复优化

- 默认聊天目录消息直接回复群聊，不回复到话题
- 提升即时通讯场景的用户体验

### Added - 2026-03-01

#### 默认聊天目录功能

- 支持默认聊天目录，普通消息自动创建/继续 Claude 会话
- 新增 `DEFAULT_CHAT_DIR` 配置项
- 精简飞书卡片结构，@ 提醒移至 header
- 精简 `install.sh` 安装脚本，自动生成 `.env` 配置

### Added - 2026-02-28

#### ExitPlanMode 卡片展示

- ExitPlanMode 工具支持方案内容卡片展示
- 灰底 Markdown 格式呈现，保留交互按钮

#### Typing 表情反馈

- processing 通知使用 Typing 表情替代文本消息
- 任务完成自动清除，更轻量的状态反馈

### Added - 2026-02-27

#### 多用户命令列表修复

- 将 `claude_commands` 从全局配置改为 per-user binding 存储
- 修复多用户命令列表混用问题

#### 常用目录显示优化

- 常用目录下拉选项优化显示格式，优先展示文件夹名

#### 日志系统重构

- 统一日志配置，引入 `DailyRotatingFileHandler` 按天自动轮转

### Added - 2026-02-26

#### 飞书通知增强

- 飞书通知完整显示 `session_id`、`prompt` 和 `claude_command`

#### macOS 兼容性修复

- 修复 macOS 兼容性问题

### Added - 2026-02-24

#### 权限延迟检测增强

- 增强权限延迟检测，支持 `transcript tool_result` 精确检测用户决策

### Fixed - 2026-02-23

- 修复 `json.sh` Python 解析器布尔值序列化与异常捕获问题
- 封装 `send_feishu_text()` 修复分离部署模式下飞书消息发送

### Added - 2026-02-22

#### 飞书话题内回复模式

- 新增 `FEISHU_REPLY_IN_THREAD` 配置
- 支持将回复消息收敛到话题详情，不刷群聊主界面

#### 飞书话题流链式回复

- 实现飞书话题流链式回复功能，同一会话的消息在同一话题内回复

### Changed - 2026-02-22

#### 代码重构

- 规范化 API 路由路径，按职责分层命名
- `RequestManager` 改为单例模式，`send_html_response` 移至 utils

### Fixed - 2026-02-22

- 修复文档一致性与代码注释问题
- 清理 Python 代码的 PEP 8 合规问题（无用导入、类型注解风格、函数签名缩进与参数命名）

### Added - 2026-02-21

#### 错误信息透传

- 卡片发送失败时透传具体错误信息到降级通知

#### Stop 通知增强

- 提升 Stop 通知内容长度限制至 10000 字符

### Changed - 2026-02-21

- 拆分 `CallbackHandler` 为 `HttpRequestHandler` + 纯函数路由模块

### Fixed - 2026-02-20

- 健康检查改用 ping/pong 协议，消除服务端空连接 WARNING 日志

### Fixed - 2026-02-19

- 修复 Bash 5.2+ 模板替换中 `&` 和 `\` 被特殊解释的问题

### Added - 2026-02-18

#### 普通消息使用提示

- 为普通消息增加使用提示
- 记录未处理请求日志

### Added - 2026-02-17

#### Skill 和 AskUserQuestion 工具支持

- 新增 Skill 工具支持及权限规则空值通配符处理
- 支持 AskUserQuestion 工具的飞书通知

#### 注册授权卡片增强

- 注册授权卡片增加完整权限说明和安全风险提示

### Changed - 2026-02-17

- 移除按钮和消息映射中的 `callback_url`，统一从 `BindingStore` 获取
- 修复分离部署模式下 `callback_url` 获取问题

### Added - 2026-02-16

- 注册接口支持 `X-Forwarded-For` 获取真实客户端 IP
- Stop 通知卡片显示 `--resume` 指令，标题包含 session-id 便于搜索
- `FEISHU_OWNER_ID` 限制为 user_id 格式，避免换应用后认证失败
- 新建用户使用指南，集中说明飞书端交互方式

### Fixed - 2026-02-16

- MCP 工具权限规则移除参数后缀，匹配 Claude Code 实际格式
- 修复 Bash 兼容性、TOCTOU 竞态，提取公共工具函数

### Changed - 2026-02-16

- 拆分部署文档，修复文档错误和过时引用
- 将文档按用途归类到 deploy/design/reference 子目录

### Fixed - 2026-02-15

- 修复多个安全问题（命令注入、XSS、时序攻击）及兼容性问题
- 优化安全性与健壮性：curl headers 使用数组避免注入、socket 权限收紧
- 修复 `handle_socket_client` 多个异常处理和变量管理问题

### Changed - 2026-02-15

- 将 `AuthTokenStore` 和 `SessionChatStore` 拆分到独立 services 模块
- 重命名 Store 类与文件使命名更准确
- 重命名 Python 文件为符合 PEP 8 规范

### Performance - 2026-02-15

- 优化 JSON 解析性能，新增 `json_get_multi` 批量获取字段
- python3 解析器安全处理中间键不存在

### Added - 2026-02-14

#### Stop 事件通知优化

- 优化 Stop 事件通知，聚合多轮回复并展示思考过程

#### 多命令配置

- 支持 `CLAUDE_COMMAND` 多命令配置
- 新增 `/reply` 指令切换命令继续会话

### Added - 2026-02-12

- `/new` 指令常用目录旁新增浏览按钮
- 移除目录浏览 limit 限制
- 目录历史自动过滤不存在的路径

### Added - 2026-02-11

#### /new 指令卡片优化

- 优化 `/new` 指令卡片布局与交互
- 标签与输入控件同行对齐
- 提示词支持多行输入
- 创建会话卡片显示所选目录和提示词

#### FEISHU_AT_USER 优化

- 空值时默认 @ `FEISHU_OWNER_ID`
- 支持 `off` 禁用

### Fixed - 2026-02-11

- 将 AutoRegister 注册移到 HTTP 服务启动后，修复单机部署竞态条件

### Added - 2026-02-10

- 增强 `/new` 指令目录选择卡片：支持自定义路径输入和动态目录浏览

### Added - 2026-02-09

- vscode-ssh-proxy 添加彩色输出和 autossh 自动重连支持
- 为 `/new` 指令添加工作目录选择卡片，支持从历史目录快速选择

### Added - 2026-02-07

#### 飞书注册卡片升级

- 飞书注册卡片升级 schema 2.0
- 支持点击按钮后动态更新卡片状态
- 新增解绑功能

#### 飞书消息回复能力

- 新增 `reply_text`/`reply_card` API
- 所有通知消息支持回复模式

#### /new 指令

- 添加 `/new` 指令支持飞书发起新 Claude 会话

### Added - 2026-02-06

#### 消息路由机制

- 实现基于 `chat_id` 的消息路由机制
- 支持 `session_id` 与群聊的映射存储和查询

#### 安全增强

- 添加卡片操作者身份验证，确保只有本人才能点击权限按钮
- 增强飞书网关权限验证

#### Shell 兼容性

- 增强 shell 兼容性，支持 zsh、fish 等主流终端的别名加载

### Changed - 2026-02-06

- 使用 `owner_id` 替代 `receive_id` 配置，简化飞书网关鉴权流程

### Added - 2026-02-05

#### 飞书网关双向认证

- 实现飞书网关注册与双向认证机制
- 实现 OpenAPI 模式下前端调用 `/feishu/send` 的双向认证

### Fixed - 2026-02-04

- 新增安全分析与部署模式文档
- 优化 `request_id` 生成降低可预测性

### Added - 2026-02-03

- 新增 `CLAUDE_COMMAND` 环境变量支持自定义 Claude 命令和别名
- 飞书相关网络请求添加无代理配置，避免系统代理干扰

### Changed - 2026-02-03

- 支持飞书 post 类型消息解析，统一提取纯文本并清理 @提及

### Added - 2026-02-02

#### 会话继续功能

- 支持飞书回复继续 Claude 会话
- 会话继续新增飞书错误通知

### Changed - 2026-02-02

- 新增日志脱敏并国际化错误提示
- 统一响应格式并优化代码结构

### Added - 2026-02-01

#### OpenAPI 分离部署

- 支持 OpenAPI 模式下飞书网关与 Callback 服务分离部署

### Fixed - 2026-02-01

- 修复 `send_feishu_text` 函数 JSON 格式错误和返回值缺失

### Changed - 2026-02-01

- 简化飞书日志调用并新增卡片发送日志记录

### Added - 2026-01-31

#### 飞书 OpenAPI 消息发送模式

- 新增飞书 OpenAPI 消息发送模式
- 支持 `webhook`/`openapi`/`both` 三种方式

### Changed - 2026-01-31

- 移除 both 模式，重构飞书发送逻辑抽取公共函数
- 将 `session_slug` 重命名为 `session_id`，统一使用 session_id 前8位作为会话标识

#### 配置项重命名与默认值调整 (config-rename-and-defaults)

- 重命名配置项，提升语义清晰度：
  - `REQUEST_TIMEOUT` → `PERMISSION_REQUEST_TIMEOUT`
  - `CLOSE_PAGE_TIMEOUT` → `CALLBACK_PAGE_CLOSE_DELAY`
- 调整默认值：
  - `PERMISSION_REQUEST_TIMEOUT`: 300s → 600s（10 分钟）
  - Hook timeout: 360s → 660s（匹配服务端超时 + 60s 缓冲）
- 重组 `.env.example` 配置分类，新增配置速查表
- 同步更新 `install.sh`、`README.md`、`QUICKSTART.md` 等文档

### Changed - 2026-01-30

- stop hook 支持从子代理目录提取 assistant 消息

### Added - 2026-01-29

- `FEISHU_AT_ALL` 重构为 `FEISHU_AT_USER`，支持 @ 指定用户

### Changed - 2026-01-29

- 调整 Stop 消息长度默认值为 5000
- 统一飞书 HTTP 超时常量

### Fixed - 2026-01-28

- hook 进程不存在时返回 410 错误，明确告知决策无法送达

### Added - 2026-01-27

#### 权限请求卡片会话信息 (permission-card-session-info)

- 权限请求卡片新增会话信息显示
- 显示 session_id 前 8 位，便于区分不同会话的权限请求
- 更新 `permission-card.json` 和 `permission-card-static.json` 模板
- 注册 hook PID 实时检测终端响应，移除冗余的 socket 存活检查

### Changed - 2026-01-27

- 优化安装脚本输出格式，补充文档配置说明

### Added - 2026-01-26

#### Stop 事件完成通知 (stop-event-notification)

- 新增 `src/hooks/stop.sh` 独立 Stop 事件处理器
- 从 transcript 文件中提取 Claude 最终响应内容
- 支持显示会话标识（session_id 前8位）
- 新增 `STOP_MESSAGE_MAX_LENGTH` 配置（默认 2000 字符）
- 新增 `stop-card.json` 飞书卡片模板
- 任务完成后自动发送飞书通知，包含 Claude 响应摘要
- `src/hooks/webhook.sh` 重构：移除内联飞书卡片，统一使用 `feishu.sh` 函数
- `install.sh` 更新：同时配置 PermissionRequest 和 Stop 两个事件的 Hook

#### 飞书 @ 用户配置 (feishu-at-user-config)

- 新增 `FEISHU_AT_USER` 环境变量配置（默认为空）
- 支持 `all`（@ 所有人）、`ou_xxx`（open_id）、user_id

#### 权限通知延迟发送 (permission-notify-delay)

- 新增 `PERMISSION_NOTIFY_DELAY` 环境变量配置（默认 60 秒）
- 支持在权限请求后延迟指定秒数再发送飞书通知
- 延迟期间用户在终端响应时，自动取消通知发送（Claude Code 会 SIGKILL 终止 hook）
- 延迟期间每秒检测父进程状态，父进程退出时跳过发送
- 用途：避免快速连续请求时的消息轰炸

#### VSCode SSH 远程开发代理

- 新增 VSCode SSH 远程开发代理
- 支持通过反向 SSH 隧道自动唤起本地 VSCode 窗口
- VSCode SSH 代理新增 `--ssh-port` 参数

#### session_id 追踪

- 为前后端通信添加 `session_id` 追踪

### Fixed - 2026-01-26

#### 用户响应时连接状态检测 (socket-state-realtime-check)

- 修复用户点击按钮响应时可能因清理线程延迟而导致响应失败的问题
- 用户点击按钮时实时检测 socket 连接状态，不再依赖后台清理线程的延迟检测
- 确保用户响应时能立即获得准确的连接状态
- 修复 VSCode 代理 HOME 软链接路径不匹配及健康检查正则问题

### Changed - 2026-01-26

- 重构目录结构，统一将源代码迁移至 `src` 目录

### Added - 2026-01-25

- 重组测试文档到 `test/` 目录，新增权限请求测试脚本

### Added - 2026-01-23

#### VSCode 自动跳转 (add-vscode-redirect)

- 点击飞书卡片按钮后，自动跳转到 VSCode 并聚焦到项目目录
- 新增 `VSCODE_URI_PREFIX` 环境变量配置:
  - 支持本地开发: `vscode://file`
  - 支持 SSH Remote: `vscode://vscode-remote/ssh-remote+server`
  - 支持 WSL: `vscode://vscode-remote/wsl+Ubuntu`
- 响应页面增强:
  - 显示"正在跳转到 VSCode..."提示
  - 跳转失败时显示手动打开链接和 VSCode 设置提示
  - 根据本地/远程自动显示对应的配置项

#### 统一配置读取

- 新增统一配置读取模块，支持自动从 `.env` 文件加载配置

### Fixed - 2026-01-23

- 修复倒计时显示和工具配置路径问题

### Changed - 2026-01-23

- 抽取项目路径管理逻辑到统一的 `project.sh` 库

### Added - 2026-01-22

- 添加权限请求 command 日志功能，按日期和会话 ID 保存命令历史

### Fixed - 2026-01-22

- 修复命令中 `&` 符号在飞书卡片显示异常的问题

### Added - 2026-01-21

- 权限请求卡片支持 @所有人 以触发消息横幅

### Added - 2026-01-20

#### 飞书卡片回传交互 (card-callback-handler)

- 新增飞书卡片按钮回传交互支持，用户点击按钮后飞书内直接显示 toast 提示
- 新增 `buttons-openapi.json` 模板，使用 `callback` 类型按钮
- `build_permission_buttons()` 根据 `FEISHU_SEND_MODE` 自动选择按钮类型
  - `webhook` 模式：使用 `open_url` 类型按钮（点击跳转浏览器）
  - `openapi` 模式：使用 `callback` 类型按钮（飞书内直接响应）
- 新增 `src/server/services/decision_handler.py` 统一决策处理逻辑
- `RequestManager.resolve()` 返回值增加错误码，避免字符串匹配判断
- 新增飞书事件 `card.action.trigger` 处理支持
- 配置文档补充飞书事件订阅配置步骤

### Changed - 2026-01-20

- 飞书卡片模板统一升级至 2.0 格式
- 完善文档并升级通用通知卡片格式

### Fixed - 2026-01-20

- 为拒绝运行和拒绝并中断按钮添加跳转链接
- 修复权限请求卡片 JSON 格式错误

### Added - 2026-01-19

#### 回调页面自动关闭

- 实现回调页面自动关闭功能
- 支持环境变量配置超时时间

#### 飞书卡片模板化 (extract-feishu-card-templates)

- 将飞书卡片的 JSON 构造逻辑从 Shell 脚本抽离为独立的模板文件
- 新增 `templates/feishu/` 目录,存放卡片模板:
  - `permission-card.json` - 权限请求卡片(交互模式)
  - `permission-card-static.json` - 权限请求卡片(静态模式)
  - `notification-card.json` - 通用通知卡片
  - `buttons.json` - 交互按钮配置
  - `README.md` - 模板使用说明和变量清单
- 新增模板渲染函数:
  - `validate_template()` - 验证模板文件 JSON 格式
  - `render_template()` - 核心模板渲染函数(支持变量替换和 JSON 转义)
  - `render_card_template()` - 卡片模板渲染包装函数
- 重构卡片构建函数使用模板:
  - `build_permission_card()` - 权限请求卡片
  - `build_notification_card()` - 通用通知卡片
  - `build_permission_buttons()` - 交互按钮
- 支持环境变量 `FEISHU_TEMPLATE_PATH` 自定义模板目录
- 移除硬编码的 JSON 字符串拼接逻辑

**优势**:
- 维护便利:直接编辑 JSON 模板即可更新卡片样式,无需修改代码
- 版本管理:模板独立存储,可轻松回滚或升级
- 扩展性强:新增卡片类型只需添加新模板文件
- 向后兼容:保持现有 API 接口不变

### Added - 2026-01-18

#### 降级文本通知

- 飞书卡片发送失败时自动发送降级文本通知
- 修复飞书卡片 Bash 命令转义问题

#### 模块化重构

- 项目结构模块化重构，修复 Socket 服务检测逻辑
- 模块化重构权限通知功能，统一工具配置，消除代码重复
- 添加客户端超时兜底机制，抽取共享超时配置模块
- 优化 hooks 配置合并逻辑，保留现有其他配置

### Changed - 2026-01-18

- 优化环境变量配置提示，自动检测 shell 类型并提示配置文件
- 移除冗余的 server_stdout.log

### Fixed - 2026-01-17

- 修复始终允许写入 `None(*)` 的 bug
- 添加 Read 工具卡片适配
- 修复重复发送卡片问题
- 优化日志打印目录

### Added - 2026-01-16

- 优化飞书卡片的显示问题
- 更新超时机制

### Changed - 2026-01-16

- 将飞书卡片发送逻辑从后端迁移至前端脚本

### Fixed - 2026-01-16

- 修复 socket 连接过早关闭导致决策响应不完整的问题
- 后端未启动时回退终端而非拒绝
- 任何服务错误都回退终端而非拒绝，只有用户明确拒绝才 deny
- 超时回退终端 & 死连接检测 & 日志时间戳修复
- 修复依赖检测的 bug

### Added - 2026-01-15

#### 可交互权限控制 (add-interactive-permission-control)

- 实现飞书卡片按钮交互，用户可直接在飞书中批准/拒绝权限请求
- 新增回调服务 (`callback-server/server.py`)，通过 HTTP 接收按钮操作
- 使用 Unix Domain Socket 实现进程间通信
- 支持四种操作：
  - 批准运行 (allow)
  - 始终允许 (allow + 持久化规则)
  - 拒绝运行 (deny)
  - 拒绝并中断 (deny + interrupt)
- "始终允许"功能自动写入权限规则到 `.claude/settings.local.json`
- 实现降级模式：回调服务不可用时仅发送通知

#### 移除服务器端超时 (remove-server-timeout)

- 移除人为的服务器端 TTL 超时机制
- 请求有效性完全由 socket 连接状态决定
- 改进错误提示，区分"已被处理"和"连接已断开"
- 清理机制改为基于连接状态而非时间

#### 移除 jq 依赖 (remove-jq-dependency)

- 实现使用 grep/sed/awk 等原生命令解析 JSON
- 优先使用 jq（如可用），否则回退到原生命令
- 降低系统依赖要求

### Fixed

- 修复 socket 通信中的 half-close 问题
- 改进 base64 编码传输以处理特殊字符
- 修复管道数据传递问题（从 heredoc 改为管道）

### Technical - 2026-01-15

#### 服务器端统一超时控制 (server-side-timeout-control)

- **移除客户端超时**:
  - `socket-client.py` 不再设置超时，改为无限等待服务器响应
  - 移除 `PERMISSION_TIMEOUT` 环境变量
  - 客户端等待服务器主动关闭连接

- **服务器端可配置超时**:
  - 新增 `PERMISSION_REQUEST_TIMEOUT` 环境变量（默认: 300 秒）
  - 设为 0 可完全禁用超时清理
  - 清理线程定期检查并关闭超时的 pending 请求
  - 超时后主动关闭 socket 连接，客户端收到后返回 deny

- **优势**:
  - 服务器端统一管理超时策略
  - 避免客户端和服务器超时不一致问题
  - 更灵活的配置（禁用/调整超时时间）

#### 增强调试和日志功能 (enhance-debug-logging)

- **日志系统增强**:
  - 添加文件日志输出 (`log/callback_YYYYMMDD.log`)
  - 毫秒级时间戳格式
  - DEBUG 级别详细日志
  - Socket 客户端独立调试日志 (`/tmp/socket-client-debug.log`)

- **Socket 通信改进**:
  - 服务器端接收超时设置（5 秒），避免永久阻塞
  - 详细的时间跟踪（总耗时、等待耗时、读取耗时）
  - Socket 连接状态检查和日志记录
  - 长度前缀协议的详细传输日志

- **错误处理增强**:
  - 添加异常 traceback 输出
  - 区分不同类型的连接错误
  - 更精确的错误信息（包括耗时信息）

- **数据传递修复**:
  - 将 heredoc (`<<<`) 改为管道 (`echo |`) 传递
  - 添加进程退出码记录
  - stderr 重定向到日志以便调试

### Technical Details

**项目架构**:
```
Claude Code → PermissionRequest hook
    ↓
permission-notify.sh
    ↓ (Unix Socket)
callback-server (Python HTTP)
    ↓ (HTTP POST)
飞书 Webhook
    ↓ (用户点击按钮)
callback-server (接收回调)
    ↓ (Unix Socket)
permission-notify.sh
    ↓ (JSON output)
Claude Code
```

**环境变量**:
- `FEISHU_WEBHOOK_URL` - 飞书 Webhook URL（必需）
- `CALLBACK_SERVER_URL` - 回调服务外部访问地址（默认: http://localhost:8080）
- `CALLBACK_SERVER_PORT` - HTTP 服务端口（默认: 8080）
- `PERMISSION_SOCKET_PATH` - Unix Socket 路径（默认: /tmp/claude-permission.sock）
- `PERMISSION_REQUEST_TIMEOUT` - 服务器端超时秒数（需为正整数，默认: 600）

**依赖**:
- 可交互模式: socat, python3, curl
- 降级模式: curl (可选 jq)
