# Changelog

All notable changes to this project will be documented in this file.

## [Released]

### Changed - 2026-06-24

#### 飞书消息内容解析重构与 @提及 修复

- 新增 `handlers/feishu/content.py`，将消息内容解析（text/post）和 @提及 解析从 `__init__.py` 中拆出，统一入口 `extract_message_text()` + `build_mention_resolution()`
- **修复 @提及 处理 bug**：旧代码 `_AT_USER_PATTERN` 无脑删除 `@_user_1`，无论它是 bot 还是人。现在通过 `build_mention_resolution()` 精确识别：bot 提及删除，人员提及替换为 `@name`（如 `@Frankie`），让 agent 能识别具体是谁
- 增强 post 富文本解析：支持标题、超链接（转 markdown）、代码块（带语言标识）、@提及；修复代码块尾部多余换行
- 消息日志记录从 `__init__.py` 内联逻辑迁移到 `utils._log_message_event()`，日志器懒加载逻辑内聚
- 移除 `_is_at_bot()` 函数和 `_AT_USER_PATTERN` 常量，bot 识别逻辑合并到 `build_mention_resolution()` 中

### Changed - 2026-06-21

#### runtime 目录与 .env 路径支持外置

- `runtime/` 目录路径支持通过 `RUNTIME_DIR` 覆盖（`main.py`，默认仍为项目根下 `runtime/`），便于将运行时状态写到项目外
- `.env` 文件路径支持通过 `CODE_ANYWHERE_ENV_FILE` 覆盖（`config.py` 与 `core.sh` 跨语言对称，默认仍为项目根下 `.env`）；该变量是「读哪个 .env」的引导开关，故只从进程环境读取、不从 .env 自身读，并用项目名前缀避免与其它仓库的通用变量名（如 `ENV_FILE`）撞名
- 两项默认行为不变、生产零副作用，主要服务于让回调服务可在隔离的临时目录中运行（端到端测试隔离的前置能力）

### Added - 2026-06-20

#### `/groups dissolve idle <天数>`：按空闲天数解散群聊

- 新增子命令 `/groups dissolve idle <N>`，直接解散空闲（超过 N 天未活跃）的群聊，避免手动逐个挑序号
- 输入即执行，与其它 `/groups dissolve` 子命令（按序号 / all / 目录）行为一致；`/groups` 卡片已逐目录展示空闲时长，且该指令本身表意明确，故不做二次确认
- 空闲判定（`now - last_active_at >= 天数 * 86400`，无 `last_active_at` 时回退 `created_at`）抽成 `find_idle_group_chats()`，自动解散（`main.py`）与本指令共用
- `find_idle_group_chats()` 设计为纯过滤器：`owner_chats`（群列表）与 `now`（判定时刻）由调用方必传，函数内不自取，避免重复读 group_chat 文件（`JsonStore._load` 无缓存）。定时清理（`_cleanup_group_chats`）外层 `get_all()` 已拿全量、逐 owner 复用 `bucket.values()`，并对全 owner 共用同一 `now` 快照；手动指令（`_dissolve_groups`）顶部本就 `get_chats_by_owner()` 过，直接复用同一份。两条路径都不重复读盘

### Changed - 2026-06-20

#### 日志按月份分子目录归档

- 日志文件在原有「组件/日期.log」基础上增加一层月份目录，变为「组件/YYYY-MM/日期.log」，避免单目录文件数无限增长：
  - `log/hook/2026-06/2026-06-20.log`、`log/callback/2026-06/2026-06-20.log` 等
  - `log/command/` 的 session 日志同样归入月份目录：`log/command/2026-06/2026-06-20_<session>.log`
- 路径模板集中在 `src/shared/logging.json`，新增 `{month}` 占位符（格式 `month_format: "%Y-%m"`），Shell（`core.sh`）与 Python（`logging_config.py`）共用；`DailyRotatingFileHandler` 跨天/跨月时自动切换目录并创建
- `command` 日志从硬编码路径收编进 `file_patterns`：新增 `command/{month}/{date}_{session}.log` 模板与 `{session}` 占位符（运行时值、仅 Shell 端展开），`log_command` 改用模板展开，月/日格式与其它组件一致由配置驱动
- 生成日志路径时保证 month 与 date 同源取值（Shell 端 `date` 单次输出两值、Python 端 `time.localtime()` 快照），避免月末跨午夜临界时日志落入错误月份目录
- `log_command` 对进入文件名的 `session_id` 清洗为安全字符，防止特殊字符破坏 `sed` 替换或构成目录穿越
- 文件名保留完整日期不变（仅新增一层月份目录）；依赖文件名日期的脚本不受影响，按目录层级 glob 的需加一层（如 `hook/*/*.log`）
- 存量旧日志（散在各组件根目录）不自动迁移，由各部署自行处理

#### 拆分 `handlers/utils.py` 杂物文件

- 将 561 行、9 个函数的 `handlers/utils.py` 按职责拆为 5 个文件，消除「什么都往里放」的杂物层：
  - `handlers/responses.py` — HTTP 响应写回（`send_json` / `send_html_response`，需要 HTTP handler）
  - `handlers/outbound.py` — callback 侧飞书出站门面（`reply_feishu_text` / `reply_feishu_markdown` / `remove_feishu_typing` / `create_feishu_group`）
  - `utils/http_client.py` — 通用出站 HTTP（`post_json`）
  - `utils/shell.py` — 子进程命令构建（`build_shell_cmd`）
  - `utils/concurrency.py` — 后台线程执行（`run_in_background`）
- `post_json` 通用化：移除项目特定的 `auth_token` 参数，改为通用 `headers`（合并到默认 `Content-Type` 之上），不再与飞书鉴权耦合
- 给拆出的函数补齐 Python 3.6+ 内联类型注解
- 移除 `register.py` 与 `feishu/*` 中零散的 `as _xxx` 私有 import 别名（约定不统一且部分缺失），统一为裸 import；真正的私有函数不受影响
- 顺带修复 `register.py` 既有的 pyflakes 告警：删除未用的 `List` / `BindingStore` import，去掉 5 处无占位符的 f-string 前缀
- 纯结构重构，外部 import 路径与运行时行为不变

### Changed - 2026-06-19

#### 统一子进程失败错误信息构建

- 抽取 `_build_error_msg()` 收敛 `_check_and_monitor` / `_monitor_startup` / `_monitor_detached` 三处重复的"stderr→stdout→兜底"逻辑
- 顺带修复 `_monitor_startup` 仅取 `stderr`、在 `claude -p` 把错误打到 stdout 时丢失错误信息的缺陷；三处日志统一截断到 `MAX_LOG_LENGTH`，并对 `None` 输入更稳健

#### 抽取 `stores/` 子包

- 将 9 个 store 文件（`json_store` 基类 + 8 个业务 store）从 `services/` 抽到 `src/server/stores/` 子包，让 `services/` 只留真正异构的服务（`feishu_api` / `session_facade` / `ws_*` 等）
- 纯结构搬迁：`git mv` 保留历史，消费者与测试的 `from services.<store>` 统一改为 `from stores.<store>`，不改任何逻辑、不改运行时语义
- `atomic_json.py` 留在 `utils/`（无状态通用工具）；放 `src/server/stores/` 而非 `src/stores/`，保持 server 单一 import 根、零 sys.path 改动

#### 统一抽象 Store 持久化模型

- 新增 `utils/atomic_json.py`（原子 JSON 读写工具）与 `services/json_store.py`（`JsonStore` 单例基类），消除 8 个 store 中逐字重复的 `_load`/`_save`/单例样板
- 8 个 store（message_session / binding / notify_config / directory / auth_token / group_chat / group_session / session_chat）迁移到基类，仅保留 `FILENAME`/`LOG_TAG` 声明与各自业务方法，净减约 500 行
- 一并修复两处一致性缺陷：加载时校验顶层为 dict（文件被篡改成数组/字符串时返回默认值而非崩溃）、写入异常时清理临时 `.tmp` 文件
- per-subclass 单例隔离（`__init_subclass__` 注入独立 `_instance`/`_lock`）；外部 API（`get_instance()` / `initialize()` / 各业务方法签名）不变，不改变运行时语义
- 新增 `tests/test_atomic_json.py`、`tests/test_json_store.py` 覆盖缺陷修复与 per-subclass 隔离不变量

### Changed - 2026-06-16

#### 拆分 `handlers/feishu.py` 上帝文件

- 将 4973 行的 `handlers/feishu.py` 拆分为 `handlers/feishu/` 包（10 个文件），按职责单一拆分，实际代码量均符合 ≤500 行约束
- 模块划分：`__init__.py`（事件路由门面）、`utils.py`（工具）、`forward.py`（请求转发）、`message.py`（消息发送）、`card_session.py` / `card_action.py`（卡片构建与交互）、`command.py` / `mute.py` / `notify.py`（命令处理）、`group.py`（群聊管理）
- 外部 import 路径不变（`from handlers.feishu import ...` 继续可用），公开 API 通过 `__init__.py` re-export 并以 `__all__` 声明
- 纯结构重构，不改变任何业务逻辑

### Added - 2026-06-14

#### 环境变量续聊透传

- 新增 `SESSION_ENV_WHITELIST` / `SESSION_ENV_BLACKLIST` 配置，按白名单捕获启动 agent 时的 shell 环境变量，续聊时以 K=V 前缀注入，覆盖登录 shell 全局 export 的同名变量
- 新增 `/cb/session/set-env` 回调路由，hook 进程后台上报 env 快照
- Shell hook 子脚本（permission/stop/user_prompt）的 `source` 统一提升到 `hook-router.sh`，子脚本不再重复引入

### Added - 2026-06-13

#### `/notify` 指令：运行时覆盖通知配置

- 新增 `/notify at self|all|off|<user_id>` 飞书指令，无需改 `.env`/重启即可临时调整通知 @ 行为
- 新增 `/notify at HH:MM-HH:MM` 时段控制，仅在时段内 @；`/notify at always` 清除时段限制
- 新增 `/notify delay <秒>` 运行时覆盖权限通知延迟；`/notify delay default` 恢复默认
- 新增 `/notify status` 统一查看所有通知配置（@ 对象、时段、延迟）
- 新增 `NotifyConfigStore`，运行时覆盖配置持久化到 `runtime/notify_config.json`
- 新增 Callback 路由 `/cb/notify/config`（set_at/set_at_time/clear_at_time/set_permission_delay/clear_permission_delay/query）
- `feishu.sh` 的 `_build_at_user_tag` 优先读取运行时覆盖，支持时段判断（含跨午夜），无覆盖时默认 @ owner
- 废弃 `FEISHU_AT_USER` 环境变量，功能由 `/notify at` 完全替代
- 废弃 `PERMISSION_NOTIFY_DELAY` 环境变量，功能由 `/notify delay` 完全替代（默认值从 60 调整为 0 即立即发送，升级时自动迁移已有配置）

#### `/mute` 支持递归静音和加白

- 新增 `/mute /path/**` 递归静音：静音指定目录及其所有子孙目录
- 新增 `/unmute /path/**` 递归加白：标记目录及其所有子孙目录为不静音
- 新增 `/unmute /path` 加白语义：对非静音目录写入显式加白，可预防性保护目录不被祖先递归静音覆盖
- `mute_dir`/`unmute_dir` 不再校验目录是否存在，允许对已删除或尚未创建的路径操作（清除规则/预防性加白）
- `DirectoryStore` 新增 3 字段数据模型（`muted_at`/`unmuted_at`/`recursive_at`），支持 6 种状态、24 条转移规则和 walk-up 匹配算法
- `/mute list` 卡片改版：中文状态标签（静音·自身、不静音·自身+子目录 等）、按路径层级排序、顶部引用块展示命令帮助
- 新增 38 个单元测试覆盖全部状态转移规则、walk-up 匹配、翻转保护和清除路径

### Changed - 2026-06-10

#### Codex 自动补充 `--skip-git-repo-check` 参数

- Codex 启动命令自动补充 `--skip-git-repo-check`，允许在非 git 目录下使用
- 若用户已在 `CODEX_COMMAND` 中配置该参数则不重复添加
- 补充 session ID 捕获失败时的 stderr 诊断日志

### Changed - 2026-06-09

#### `/groups dissolve` 支持按目录解散群聊

- 新增 `/groups dissolve /path` 精准匹配解散指定目录的群聊
- 新增 `/groups dissolve /path/**` 递归匹配解散指定目录及子目录的群聊
- 使用 `os.path.normpath` 做路径规范化，避免跨机器 symlink 不一致
- 群聊列表卡片优化：标题改为"由服务创建的群聊（共 N 个）"，解散提示置顶，未关联目录排最后，分隔线移至目录标题前

#### 帮助卡片布局优化

- 从每条 example 一个 `column_set` 改为每个命令一个 `column_set`，解决飞书卡片元素超限问题（错误码 11310）

#### 创建群聊时自动设置 owner 为群管理员

- 创建群聊并拉入 owner 后，自动将 owner 设置为群管理员
- 新增 `add_chat_managers()` API 封装，调用飞书 `POST /im/v1/chats/{chat_id}/managers/add_managers`
- 设置管理员失败只记日志警告，不影响群聊创建

### Changed - 2026-06-08

#### 群聊自动 @bot 过滤：按成员数自动判断

- 移除 `at_bot_only` per-user 配置，改为根据群成员数自动判断：单人群（owner + bot）不需要 @bot，多人群（2+ 用户）需要 @bot
- 新增 `feishu_api.get_chat_info()` 获取群信息，内部缓存 `user_count` 60 秒（TTLCache）
- 全链路清理 `at_bot_only`：config、extract_binding_params、binding_store、main、auto_register、.env.example

#### 注册参数透传重构：per-user 配置打包为 binding_params dict

- 注册链路中 10 个 per-user 参数（at_bot_only、session_mode、group_allow_cowork 等）从逐参数透传改为 `binding_params: Dict[str, Any]` 统一传递
- 新增 `extract_binding_params()` 提取函数，作为字段定义的唯一入口
- 涉及 7 个文件，净减少约 350 行代码
- 未来新增 per-user 配置只需改 3 处：提取（extract_binding_params）→ 存储（binding_store.upsert）→ 默认值（config）

### Fixed - 2026-06-07

#### "始终允许"降级优化

- Claude 权限请求无 `permission_suggestions` 时，"始终允许"从报错改为降级为本次允许
- 降级提示仅对 `agent_type == 'claude'` 生效，避免误伤 Codex 等其他 Agent

### Changed - 2026-06-07

#### 协作者支持 Agent 斜杠命令

- 群聊协作者发送 Agent 斜杠命令（如 `/compact`）时跳过 `[来自群成员 xxx]` 前缀，避免 Agent 无法解析

#### Agent 斜杠命令增强

- Claude adapter 新增 `/context`、`/review`、`/simplify`、`/init` 四个斜杠命令
- `CompleteCallback` 新增 `output` 参数，有输出的命令通过 markdown 卡片展示
- 新增 `reply_feishu_markdown()` 工具函数，支持发送 Card JSON 2.0 markdown 卡片（卡片失败时降级纯文本）
- `ErrorCallback` 新增 `session_id` 参数，错误路径也执行 session 状态清理和 Typing 移除
- session 状态清理不再依赖通知发送成功，避免 `skip_next_user_prompt` 残留
- 修复 `_check_and_monitor` 快速失败路径未调用 `on_error` 的 bug
- 修复 `_monitor_detached` 异常路径未调用 `on_error` 的 bug

### Added - 2026-06-06

#### Agent 斜杠命令框架

- 新增 `SlashCommandInfo` 类和 `AgentAdapter.get_slash_commands()` 方法，各 adapter 声明自己支持的斜杠命令
- Claude adapter 声明 `/compact`（`triggers_stop_hook=False`），后续新增命令只需在 adapter 中添加一行
- 网关路由自动识别斜杠命令，跳过内置命令处理，转发给 Agent 执行
- `/help` 卡片新增「Agent 指令」区域，展示各命令及 Agent 归属标注
- 新增 `on_complete` 回调链，无 stop hook 的命令由框架手动发送完成通知并清理状态
- 新增 `/gw/feishu/remove-reaction` 网关接口，`remove_feishu_typing()` 兼容单机和分离部署
- 错误通知路径（`_send_error_notification`）也清理 Typing 表情
- 网关命令与斜杠命令名称冲突保护，避免 adapter 声明覆盖内置命令

### Added - 2026-06-01

#### 群聊协作模式（group_allow_cowork）

- 新增 `FEISHU_GROUP_ALLOW_COWORK` per-user 配置项（默认 false，仅 group 模式可用）
- 开启后群内所有成员（含未注册用户）均可参与对话，消耗 owner 的额度
- 协作者消息自动添加 `[来自群成员 xxx]` 前缀，区分发送者身份
- 协作者可执行 `/clear` 重置会话上下文，其他管理命令仅会话创建者可执行
- GroupSessionStore 新增 `_chat_to_owner` 反向索引，支持 O(1) 协作者路由查找
- 协作者身份在消息入口处统一前置判定，命令和消息路径共用

### Changed - 2026-05-30

#### 多 Agent 权限持久化：按 Agent 类型分流"始终允许"

- **Claude**：改用官方 `updatedPermissions` 机制，权限规则由 Claude CLI 自行应用，不再由服务端写入 `.claude/settings.local.json`
- **Codex**：新增 `codex_rule_writer.py`，将命令解析为 `prefix_rule(...)` 追加到 `$CODEX_HOME/rules/default.rules`
- 删除旧的 `rule_writer.py`（仅适用于 Claude 的服务端写入方案）
- Socket 请求新增 `agent_type` 字段，服务端根据 Agent 类型选择对应的持久化策略
- `permission.sh` 的 `output_decision` 支持 `updatedPermissions` 可选参数，合并原独立函数

#### 文档全面更新：统一多 Agent 表述

- 项目文档中通用场景的 "Claude Code" 统一改为 "Agent CLI" / "Agent"
- README 新增 Codex TOML hook 配置示例、补全目录树、新增环境变量分组
- `CODE_REVIEW.md` 全量更新（2026-05-30 审查，追踪上次 13 项已修复问题）
- 部署文档、设计文档、测试文档同步更新

### Changed - 2026-05-29

#### 多 Agent 并行支持：同一服务同时启用 Claude 和 Codex

- 将单 Agent 架构（`AGENT_TYPE` 二选一）改为多 Agent 并行（`ENABLED_AGENTS` 同时启用）
- 新增 `ENABLED_AGENTS`（逗号分隔）、`DEFAULT_AGENT` 配置项，替代原 `AGENT_TYPE`
- `/new` 卡片下拉菜单合并展示所有已启用 Agent 的命令，用户可自由选择
- 注册链路全链路透传 `default_agent` + 各 agent 命令列表（config → auto_register/ws_tunnel_client → gateway）
- `session_chat_store` 按 session 记录 `agent_type`，启动时 backfill 旧 session 默认值
- `handlers/claude.py` 重命名为 `handlers/agent.py`，per-session 从 store 读取 agent_type
- `setup_init.py` 支持多 Agent 选择 + 逐个命令配置
- 通用接口和用户可见文案去除 Claude 硬编码，`claude_command` 字段重命名为 `command`

### Added - 2026-05-23

#### 支持 OpenAI Codex CLI 作为第二 Agent

- 新增 `agents/codex.py`，实现 `CodexAdapter`（`codex exec` 命令构建、session ID 捕获）
- 新增 `AGENT_TYPE` 配置项，支持 `claude`（默认）和 `codex` 切换
- 新增 `CODEX_COMMAND`、`CODEX_ARGS_TEMPLATE` 配置项
- 新增 `get_agent_adapter()` 工厂函数，根据配置返回对应 adapter
- Codex 新建会话自动从 `--json` 输出捕获 session ID（`thread.started` 事件）
- `permission.sh` 兼容 Codex 权限决策输出格式（`allow`/`block`）
- `stop.sh` 支持 Codex JSONL transcript 解析（`item.completed` + `agent_message`）
- `setup_init.py` 新增 `CodexHookConfigurator`，为 Codex 生成 `config.toml` hook 配置
- `session_chat_store.py` 新增 `rename_session()` 用于 Codex session ID 替换
- `handlers/claude.py` 改用 `get_agent_adapter()` 工厂，不再硬编码 `ClaudeAdapter`

### Changed - 2026-05-11

#### 重构：抽取 Agent 适配层，为多 agent 支持做准备

- 新增 `agents/` 模块，提供 `AgentAdapter` 基类和共享的进程启动/监控逻辑
- 新增 `agents/claude.py`，实现 `ClaudeAdapter`（命令构建、MCP 配置、环境变量）
- `handlers/claude.py` 瘦身为纯 HTTP 业务逻辑，通过 `launch_agent()` 统一调用 agent 层

### Fixed - 2026-05-11

#### Stop hook 竞态修复：用 last_assistant_message 补全缺失的最终答复

- Stop hook 后台读取 transcript 时，最终 assistant text 可能尚未写入文件，导致飞书卡片只展示中间过程而缺失最终答复
- 新增 `supplement_last_message()` 函数，将 Stop 事件提供的 `last_assistant_message` 追加到 texts 末尾
- `send_stop_notification_async()` 顶部注释补充完整调用链文档

### Changed - 2026-05-10

#### Claude 命令配置改进：自动检测 --print 参数支持

- **`run_in_user_shell()`**：从 `_check_claude_command` 抽取为 `DependencyChecker` 公共方法，统一 login shell 执行逻辑
- **`check_supports_print_flag()`**：新增方法，通过 login shell 检测命令是否支持 `--print` 参数
- **`_configure_claude_command()`**：
  - 添加功能说明文案，引导用户判断是否需要自定义命令
  - 自定义命令自动检测 `--print` 支持，全部支持时跳过模板配置
  - 部分支持时提供「重新输入命令」选项，递归调用自然跳过初始问题
  - 仅当命令不支持 `--print` 时才要求配置 `CLAUDE_ARGS_TEMPLATE`
- **参数文档统一**：模板说明中 `-p` 改为 `--print`，与实际参数一致

### Fixed - 2026-05-10

#### 终端被 SIGTTOU 挂起（login shell 抢占前台进程组）

依赖检测通过 `zsh -ic` / `bash -lc` 检测 claude CLI 时，login shell 启用 job control 抢占前台进程组，退出后 Python 进程不在前台，后续 `print()` 触发 SIGTTOU 被挂起。

- **Terminal 类**：忽略 SIGTTOU/SIGTTIN 信号，确保 stdin/stdout 连接终端（打开 `/dev/tty`），每次按键前抢回前台
- **stdin 隔离**：`DependencyChecker` 中 `command -v claude` 和 `claude --version` 调用添加 `stdin=subprocess.DEVNULL`，从源头阻止 login shell 获取终端控制权

### Changed - 2026-05-08

#### 遥测中心迁移支持

- **服务端**：心跳响应新增可选 `redirect_url` 字段，当配置 `TELEMETRY_REDIRECT_URL` 且客户端 `reporting_url` 与之不同时返回
- **客户端**：心跳上报新增 `reporting_url` 字段，告知服务端当前使用的遥测地址；收到 `redirect_url` 后持久化到 `runtime/telemetry_url`，后续请求自动切换到新地址
- **URL 优先级**：持久化迁移地址 > 硬编码默认地址；用户可删除 `runtime/telemetry_url` 恢复默认

### Changed - 2026-05-07

#### claude.py 后台进程监控改进 + 可读性重构

- **后台进程失败通知**：detached 进程（运行超过 30s）失败退出时，通过后台线程排空 pipe 并保留尾部输出，发送飞书错误通知（原来直接关闭 pipe 丢失所有输出）
- **截断逻辑统一**：错误消息截断从各调用方下沉到 `_send_error_notification` 内部，避免遗漏
- **`reply_feishu_text` 参数顺序**：`(chat_id, text, message_id)` → `(chat_id, message_id, text)`，语义更一致
- **函数重命名**：`_execute_and_check` → `_launch_claude`、`_wait_for_completion` → `_monitor_startup`、`_wait_and_notify_on_failure` → `_monitor_detached`
- **函数分组重排**：按 公开接口 → 进程执行与监控 → 命令构建 → 飞书通知 四组排列，公开接口置顶

#### Markdown 预处理下沉到 feishu.sh 统一覆盖所有卡片发送

- **原问题**：Markdown 预处理（图片转文本、HTML 剥离、脚注展平、标题降级）仅在 `stop.sh` 中实现，只覆盖 Stop 事件；Permission、AskQuestion 等卡片未经预处理
- **改动**：将预处理逻辑从 `stop.sh` 迁移到 `feishu.sh` 的 `preprocess_card_markdown()`，在 `send_feishu_card()` 发送前自动递归处理卡片中所有 `{tag:"markdown"}` 元素
- **修复**：迁移时同步修复了 jq 分支 `capture()` 不匹配时产生 `empty` 导致非标题行丢失的问题，改为 `test()` 前置守卫
- **影响**：所有通过 `send_feishu_card()` 发送的卡片自动覆盖，无需各 hook 单独调用

#### setup.sh init 交互式初始化 + server.state 状态持久化

- **`setup.sh init` 交互式初始化**：新增 `init` 子命令，通过 `src/setup_init.py` 引导完成全流程配置（.env → 依赖检测 → lark-oapi 安装 → Hook 配置 → 服务启动）
- **OOP 架构**：`setup_init.py` 包含 7 个类（EditableBuffer、TerminalUI、EnvManager、DependencyChecker、HookConfigurator、ServiceManager、SetupInit），`setup.sh init` 分支简化为检测 Python + 调用 Python 脚本
- **终端交互组件**：箭头键选择器、内联编辑输入、动态列表编辑器、预览+按需编辑的 `review_settings` 组件，支持 CJK 字符宽度计算和超宽行自动换行的正确回退
- **server.state 状态持久化**：`start-server.sh` 从 PID 文件迁移到 JSON 格式的 `runtime/server.state`（含 pid/port/socket_path），新增 `state` 子命令输出运行状态供脚本调用
- **端口/Socket 冲突检测**：init 流程中检测端口占用和 socket 文件冲突，精确区分本服务占用与第三方占用
- **Hook 配置自动化**：`HookConfigurator` 自动写入/合并 settings.json 中的 Hook 配置，支持冲突检测和超时时间动态计算

### Changed - 2026-05-04

#### Hook 事件开关 + 删除 Notification 事件 + .env.example 重整

- **Hook 事件开关**：新增 `HOOK_USER_PROMPT_ENABLED`、`HOOK_PERMISSION_ENABLED`、`HOOK_STOP_ENABLED` 配置项，支持在 `.env` 中关闭对应 Hook 事件
- **删除 Notification 事件**：移除 `src/hooks/webhook.sh` 及 `hook-router.sh` 中的 Notification 路由，该事件已不再使用
- **`.env.example` 重整**：配置项重新归类为 9 个分区，顺序调整为按重要性排列，速查表与配置区域顺序对齐
- **`FEISHU_SEND_MODE` 默认值改为 `openapi`**：Webhook 模式标注为不再维护，推荐使用 OpenAPI 模式

#### 群聊命名规则重构：名称含序号，allocate/bind 两步创建

群名格式从 `{前缀} - {目录名} - {MMdd HH:mm:ss}` 改为 `{前缀} - #{序号} - {目录名} - {YYYYMMDD}`：

- **群名含序号**：先分配 seq 再建群，群名中包含 `#seq`，便于识别
- **GroupChatStore.allocate/bind 拆分**：`allocate(owner_id)` 分配 seq 并持久化占位，`bind(owner_id, seq, chat_id)` 绑定实际群聊
- **allocate 失败提前返回**：存储不可用时不再继续建群，避免产生孤儿群
- **bind 失败记 warning**：群已创建但未追踪时记录日志便于排查
- **启动时清理占位记录**：`_rebuild_index` 清理未绑定的空 chat_id 记录，清理前计入 `_max_seq` 防止 seq 回退
- **读接口过滤占位记录**：`get_chats_by_owner`、`get_chat_by_seq` 跳过空 chat_id 记录

#### groups 卡片重构：按目录分组 + 进入群聊链接

`/groups` 列表从纯文本改为飞书卡片，按目录分组展示，新增群聊跳转链接：

- **卡片化展示**：从 `_send_notice_message` 改为飞书卡片，失败时降级为文本
- **按目录分组**：群聊按 `project_dir` 分组，最近活跃的目录排前面，目录行带 📁 图标
- **进入群聊链接**：每个群聊条目附带 applink 跳转链接，点击可在飞书客户端打开群聊
- **展示 session_id**：每个群聊条目显示关联的 session ID
- **解散命令支持批量**：文案更新为 `/groups dissolve <序号1> <序号2> ...`

#### mute list 卡片重构：按目录分组 + column_set 背景色分块

飞书卡片元素上限约 50 个，column_set 按钮布局在记录较多时触发 230099 错误，重构为 markdown + column_set 背景色方案：

- **按目录分组展示**：静音会话按 `project_dir` 分组，最近静音的目录排前面，目录行带 📁 图标
- **column_set 灰色背景分块**：「已静音的目录」和「已静音的会话」各用 column_set grey 背景包裹，视觉层级清晰
- **`/mute <session_id>` / `/unmute <session_id>`**：直接按 session ID 静音/解除静音，替代卡片内按钮交互
- **移除 `handle_mute_list_unmute`**：卡片交互按钮删除，unmute 统一走命令方式
- **失败提示优化**：session-id 静音/解除失败时提示确认 ID 是否完整且正确

### Added - 2026-05-04

#### `/help` 指令帮助卡片与指令体系重构

- **`/help` 指令**：发送帮助卡片展示所有可用指令及示例
- **帮助卡片**：column_set 三列布局（指令+示例+说明），首行显示指令名后续留空，管理员指令单独分区
- **指令元数据重构**：`_COMMANDS` 从 `(handler, admin_only, help_text)` 改为 `(handler, admin_only, brief, examples)`，每个指令拆为简述 + 示例列表
- **统一触发**：未知指令、未配置默认目录时均发送帮助卡片（替代原纯文本）
- **文案补充**：未配置默认目录的提示新增 `DEFAULT_CHAT_DIR` 配置说明

### Added - 2026-05-03

#### `/mute list` 静音列表卡片

通过飞书卡片展示所有已静音的会话和目录，支持卡片内点击解除静音：

- **`/mute list` 命令**：飞书卡片分「已静音的目录」和「已静音的会话」两个区块，按 `muted_at` 降序排列
- **卡片交互**：每条记录带「解除」按钮，点击回调执行 unmute，返回 toast 反馈
- **SessionChatStore**：新增 `list_muted_sessions()` 方法；`muted` 字段改为 `muted_at` 时间戳，与目录 mute 对齐
- **SessionFacade**：新增 `list_muted` 透传方法；`_call_session_mute_api` / `_call_dir_mute_api` 参数改为 `action` 在前

#### 目录级静音（mute directory）与 SessionChatStore 重构

支持 mute 整个工作目录，终端发起的新会话自动继承目录 mute 状态；同时重构 SessionChatStore 读取方法，提供更清晰的业务抽象：

- **目录级 mute**：`/mute /path/to/dir` 静音指定目录，`/unmute /path/to/dir` 取消；终端新会话首次调用 `get-chat-id` 时自动检查并继承目录 mute 状态
- **DirectoryStore 增强**：新增 `mute_dir`/`unmute_dir`/`is_dir_muted`/`list_muted_dirs` 方法；符号链接自动解析为真实路径存储；mute 前校验目录存在性
- **定期清理**：新增 `cleanup_expired` 公共方法，清理过期使用历史和已不存在的目录条目，由 `_cleanup_expired_data` 每小时定期执行
- **SessionChatStore 重构**：`get_chat_id` 改为 `get_active_chat_id`（过滤 dissolved + expired），`is_session_muted` 仅过滤 expired（mute 是 session 维度，与群解散无关）
- **handle_get_chat_id 增强**：store 未初始化提前返回 500；目录 mute 继承仅对真正不存在的新 session 生效（dissolved session 不触发）；save/mute 失败均打日志并返回 `muted: false`
- **SessionFacade 透传**：新增 `mute_dir`/`unmute_dir` 方法，通过 `/cb/directory/mute` 路由转发

### Changed - 2026-05-03

#### Session mute 字段改为时间戳 & 清理逻辑优化

- **muted → muted_at**：session 静音字段从布尔值改为时间戳，与 DirectoryStore 的 muted_at 模式对齐
- **get_session 去主动清理**：过期 session 不再在读取时删除，仅返回 None，清理由 cleanup_expired 统一处理
- **cleanup_expired 保留静音记录**：有 muted_at 标记的 session 即使过期也不删除，避免绕过用户静音意图

#### 目录相关路由与 Store 重命名

将目录相关接口从 `/cb/claude/*` 独立为 `/cb/directory/*` 命名空间，Store 类同步重命名以保持语义一致：

- **路由重命名**：`/cb/claude/record-dir-usage` → `/cb/directory/record-usage`，`/cb/claude/recent-dirs` → `/cb/directory/recent-dirs`，`/cb/claude/browse-dirs` → `/cb/directory/browse-dirs`
- **Store 重命名**：`DirHistoryStore` → `DirectoryStore`，文件 `dir_history_store.py` → `directory_store.py`
- **数据文件迁移**：`dir_history.json` → `directories.json`，首次启动时自动迁移旧文件

### Changed - 2026-05-02

#### mute 架构重构：职责统一归 callback 端

mute 状态的检查、拦截、自动解除统一由 callback 端管理，网关侧简化为纯指令透传：

- **自动解除静音**：从网关 `_auto_unmute_if_needed` 移至 callback 端 `handle_continue_session` / `handle_new_session`，解除后回复用户消息通知
- **网关简化**：移除 `SessionFacade._muted_cache`、`is_muted()`、`invalidate_mute_cache()`；移除 `handle_send_message` 中的 mute 拦截
- **回复式通知**：`send_feishu_text` 改为 `reply_feishu_text`，支持回复指定消息（错误通知和解除静音通知均回复到用户消息上）
- **字段统一**：网关→callback 的 `reply_message_id` 统一为 `message_id`
- **文案优化**：`/mute` 提示更新为"发送消息继续会话时会自动解除静音，也可通过 /unmute 手动解除"

### Added - 2026-05-02

#### muted session Hook 层前置拦截与 chat_id 透传

muted session 的出站拦截提前到 Hook 脚本层，避免不必要的卡片构建和重复 HTTP 请求：

- **callback API 增强**：`/cb/session/get-chat-id` 响应新增 `muted` 字段，`_get_chat_id` 通过 `MUTED_SENTINEL` 哨兵值向上游传播
- **hook 前置检查**：stop/permission/user_prompt hook 前置调用 `_resolve_chat_id`，muted 时直接短路，跳过 Markdown 处理、卡片构建等后续工作
- **chat_id 透传**：hook 预解析的 `RESOLVED_CHAT_ID` 通过 options 透传到发送函数，整个链路 `_get_chat_id` 只调一次

### Added - 2026-05-01

#### Markdown 预处理：飞书卡片兼容转换

Claude 响应发送到飞书前自动做 Markdown 兼容转换（单次子进程，跳过代码块）：

- **图片链接转文本**：`![alt](url)` → `[图片: alt](url)`，避免飞书卡片渲染报错
- **HTML 标签剥离**：`<summary>` 转加粗，其余标签删除保留内容（`<br>`/`<hr>` 保留）
- **脚注定义展平**：`[^id]: content` → `**注 id**: content`，飞书会吞掉原始脚注定义行
- **标题降级**（可选）：`#` 标题 → 加粗/emoji 格式，由 `FEISHU_HEADING_STYLE` 控制
  - `bar`（默认）：H1 **【标题】**，H2~H6 竖线粗细递减
  - `circle`：H1 **【标题】**，H2~H6 蓝色圆形递减
  - `diamond`：H1 **【标题】**，H2~H6 蓝色菱形递减
  - `original`：不做标题降级，其余三项预处理仍生效

### Added - 2026-04-30

#### 新增 CLAUDE_ARGS_TEMPLATE 配置：CLI 包装器参数模板

- 新增 `CLAUDE_ARGS_TEMPLATE` 配置项（默认 `{cmd} {args}`），支持自定义 Claude 命令行参数的拼接方式
- 用于兼容第三方 CLI wrapper 的参数语法（如需要用 `-a` 将所有参数打包为单个字符串传入的场景）
- 支持裸占位符（`{args}` 展开为独立参数）和引号占位符（`"{args}"` 打包为单个 shell 参数）
- 重构命令构建逻辑：从字符串拼接改为 argv 列表 + 模板展开，shell quoting 统一由 `_expand_template` 处理

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
