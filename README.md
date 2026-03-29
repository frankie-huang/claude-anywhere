# claude-anywhere

用于实现 Claude Code 的飞书通知机制，支持可交互权限控制、会话继续和远程决策。

## 项目概述

本项目为 Claude Code 提供飞书通知功能，允许用户远程响应权限请求，无需返回终端操作。项目支持两种部署模式：

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **Webhook 模式** | 使用飞书机器人 Webhook，配置简单 | 个人使用、快速开始 |
| **OpenAPI 模式** | 使用飞书开放平台 API，支持飞书内交互回复 | 团队协作、多实例部署 |

### 核心功能

- **可交互权限控制** - 飞书卡片 4 键操作（批准/始终允许/拒绝/中断）
- **飞书回复继续会话** - 直接回复飞书消息在 Claude session 中继续提问（OpenAPI 模式）
- **任务完成通知** - 处理完成后自动发送响应摘要和会话标识
- **优雅降级** - 回调服务不可用时自动降级为仅通知模式
- **权限持久化** - "始终允许"自动写入项目权限规则

### 项目组件

1. **统一 Hook 路由** (`src/hook-router.sh`) - Claude Code Hook 统一入口，事件分发
2. **用户 Prompt 同步脚本** (`src/hooks/user_prompt.sh`) - UserPromptSubmit 事件处理
3. **权限处理脚本** (`src/hooks/permission.sh`) - PermissionRequest 事件处理
4. **通用通知脚本** (`src/hooks/webhook.sh`) - Notification 事件处理
5. **任务完成通知脚本** (`src/hooks/stop.sh`) - Stop 事件处理
6. **回调服务** (`src/server/main.py`) - HTTP 服务接收飞书卡片操作，通过 Unix Socket 传递决策
7. **飞书网关** (`src/server/handlers/feishu.py`) - OpenAPI 模式的飞书 API 网关
8. **飞书卡片模板系统** (`src/templates/feishu/`) - 模块化的卡片模板
9. **MCP 权限审批服务** (`src/server/handlers/permission_mcp.py`) - Headless 模式下桥接权限请求到飞书审批系统

## 项目结构

```
claude-anywhere/
├── setup.sh                    # 一键安装脚本（推荐）
├── install.sh                  # 安装配置脚本（手动安装）
├── .env.example                # 环境变量模板
├── src/                        # 源代码目录
│   ├── hook-router.sh          # Hook 统一入口（配置到 Claude Code）
│   ├── start-server.sh         # 回调服务启动脚本
│   ├── hooks/                  # Hook 事件处理脚本
│   │   ├── user_prompt.sh      # 用户 Prompt 同步（UserPromptSubmit 事件）
│   │   ├── permission.sh       # 权限请求处理（可交互）
│   │   ├── webhook.sh          # 通用通知处理（Notification 事件）
│   │   └── stop.sh             # 任务完成通知处理（Stop 事件）
│   ├── lib/                    # Shell 函数库
│   │   ├── core.sh             # 核心库（路径、环境、日志）
│   │   ├── json.sh             # JSON 解析（jq/python3/grep 降级）
│   │   ├── tool.sh             # 工具详情格式化
│   │   ├── tool-config.sh      # 工具配置加载
│   │   ├── feishu.sh           # 飞书卡片构建和发送
│   │   ├── socket.sh           # Socket 通信函数
│   │   └── vscode-proxy.sh     # VSCode 代理客户端
│   ├── server/                 # Python 回调服务
│   │   ├── main.py             # 主服务入口（ThreadedHTTPServer）
│   │   ├── config.py           # 配置管理
│   │   ├── socket_client.py    # Socket 客户端
│   │   ├── models/             # 数据模型
│   │   ├── services/           # 业务服务
│   │   │   ├── request_manager.py   # 请求管理器
│   │   │   ├── decision_handler.py  # 决策处理器
│   │   │   ├── rule_writer.py       # 权限规则写入
│   │   │   ├── message_session_store.py # Message-Session 映射存储
│   │   │   ├── session_chat_store.py   # Session-Chat 映射存储
│   │   │   ├── binding_store.py     # 群聊绑定存储
│   │   │   ├── dir_history_store.py # 目录历史记录存储
│   │   │   ├── auto_register.py     # 网关注册服务
│   │   │   ├── auth_token.py        # 认证令牌管理
│   │   │   ├── auth_token_store.py  # 认证令牌存储
│   │   │   ├── feishu_api.py        # 飞书 API 封装
│   │   │   └── feishu_longpoll.py   # 飞书 WebSocket 长连接服务
│   │   └── handlers/           # HTTP 处理器
│   │       ├── http_handler.py # HTTP 请求处理器（GET/POST 路由分发）
│   │       ├── callback.py     # 权限回调处理器
│   │       ├── feishu.py       # 飞书事件处理器（OpenAPI 网关）
│   │       ├── claude.py       # Claude 会话继续处理器
│   │       ├── register.py     # 网关注册处理器
│   │       ├── permission_mcp.py # MCP 权限审批服务（headless 模式）
│   │       └── utils.py        # 处理器通用工具函数
│   ├── config/                 # 配置文件
│   │   └── tools.json          # 工具类型配置
│   ├── templates/              # 飞书卡片模板
│   │   └── feishu/             # 飞书卡片模板文件
│   ├── shared/                 # 跨语言共享资源
│   │   ├── protocol.md         # Socket 通信协议规范
│   │   ├── logging.json        # 统一日志配置
│   │   └── logging_config.py   # Python 日志配置模块
│   └── proxy/                  # 本地代理服务
│       └── vscode_ssh_proxy.py # VSCode SSH 反向隧道代理
├── openspec/                   # OpenSpec 规范管理
│   ├── specs/                  # 活跃规范
│   └── changes/                # 历史变更记录
├── docs/                       # 文档
│   ├── AGENTS.md               # Agent 规范说明
│   ├── deploy/                 # 部署相关
│   │   ├── DEPLOYMENT_MODES.md
│   │   ├── DEPLOYMENT_OPENAPI.md
│   │   └── DEPLOYMENT_WEBHOOK.md
│   ├── design/                 # 架构设计
│   │   ├── FEISHU_SESSION_CONTINUE.md
│   │   ├── GATEWAY_AUTH.md
│   │   └── SECURITY_ANALYSIS.md
│   └── reference/              # 参考资料
│       ├── CLAUDE_CODE_HOOKS.md
│       └── CODE_REVIEW.md
├── test/                       # 测试脚本
├── runtime/                    # 运行时数据（session 映射等）
└── log/                        # 日志目录
```

## 快速开始

### 方式一：一键安装（推荐）

使用 `setup.sh` 脚本自动完成下载源码、依赖检测、Hook 配置和环境变量配置：

```bash
# 单机模式
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --app-id=cli_xxx --app-secret=xxx --owner-id=<用户ID>

# 分离模式（连接远程网关）
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --gateway-url=ws://gateway:8080 --owner-id=<用户ID>
```

安装后使用 `./setup.sh` 管理服务：
```bash
./setup.sh start      # 启动
./setup.sh stop       # 停止
./setup.sh restart    # 重启
./setup.sh status     # 状态
./setup.sh update     # 更新
```

> 详细说明请参考 [OpenAPI 模式部署文档](docs/deploy/DEPLOYMENT_OPENAPI.md) 的「一键安装」章节

### 方式二：手动安装

#### 1. 运行安装脚本

```bash
./install.sh
```

安装脚本会：
- 检测环境依赖（python3, curl 等）
- 配置 Claude Code hook
- 生成环境变量配置模板

#### 2. 配置环境变量

复制并编辑环境变量文件：

```bash
cp .env.example .env
vim .env
```

根据选择的部署模式配置：

| 模式 | 最小配置 | 说明 |
|------|----------|------|
| **Webhook** | `FEISHU_WEBHOOK_URL` | 默认模式，快速开始 |
| **OpenAPI 单机** | `FEISHU_APP_ID` + `FEISHU_APP_SECRET` + `FEISHU_OWNER_ID` | 飞书内响应 |
| **OpenAPI 分离** | `FEISHU_GATEWAY_URL` | 多实例部署 |

> 详细的模式对比、架构设计和配置指南请参考 [部署模式架构文档](docs/deploy/DEPLOYMENT_MODES.md)

#### 3. 启动回调服务

```bash
./src/start-server.sh
```

> **注意**：脚本会自动从 `.env` 文件读取配置，无需手动 `source .env`。

#### 4. 开始使用

启动 Claude Code，权限请求将自动发送到飞书。

## 架构设计

本项目支持两种部署模式：

| 模式 | 说明 | 详细文档 |
|------|------|----------|
| **Webhook 模式** | 使用飞书机器人 Webhook，配置简单，适合个人使用 | [部署文档](docs/deploy/DEPLOYMENT_WEBHOOK.md) |
| **OpenAPI 模式** | 使用飞书开放平台 API，支持飞书内响应、多实例部署 | [部署文档](docs/deploy/DEPLOYMENT_OPENAPI.md) |

**[→ 部署模式选择指南](docs/deploy/DEPLOYMENT_MODES.md)**

### 单机部署架构

单机模式下，callback 后端通过 WS 隧道连接本地网关（同一进程），与分离部署使用相同的通信模式。

```
┌───────────────────────────────────────────────────────────────┐
│                     src/server/ (单一进程)                     │
│                                                               │
│  ┌─────────────────┐              ┌──────────────────────┐   │
│  │ HTTP Server     │   WS 隧道    │ ws_tunnel_client     │   │
│  │ :8080           │◀─────────────│ (callback 角色)       │   │
│  │                 │              │                      │   │
│  │ /ws/tunnel      │─────────────▶│ BACKEND_ROUTES       │   │
│  │ (网关角色)       │   请求/响应   │ (请求处理)           │   │
│  └────────┬────────┘              └──────────────────────┘   │
│           │                                                  │
│           │ Unix Socket (权限请求注册)                        │
│           ▼                                                  │
│  ┌─────────────────┐              ┌──────────────────────┐   │
│  │ RequestManager  │◀─────────────│ src/hooks/           │   │
│  │ (请求管理)       │   Socket     │  permission.sh       │   │
│  └─────────────────┘              └──────────────────────┘   │
└───────────────────────────────────────────────────────────────┘
          │
          │ 飞书卡片通知
          ▼
┌─────────────────┐         ┌─────────────────┐
│  飞书 Webhook/   │◀───────│   用户点击按钮   │
│     OpenAPI     │  卡片    │   (浏览器回调)   │
└─────────────────┘         └─────────────────┘
```

### 分离部署架构（OpenAPI 模式）

分离部署支持两种通信方式：

| 方式 | 协议 | 适用场景 | Callback 要求 |
|------|------|----------|--------------|
| **WS 隧道模式** | `ws://` / `wss://` | 本地开发、多实例部署（推荐） | 无需公网可达 |
| HTTP 回调模式 | `http://` / `https://` | 云服务器部署 | 需公网可达 |

#### WS 隧道模式（推荐）

Callback 通过 WebSocket 长连接主动接入网关，无需公网 IP，适合本地开发场景。

```
                                                    ┌─────────────────────┐
                                                    │   Feishu Gateway    │
                                                    │  (飞书应用凭证)      │
                                                    │  ws://gateway:8080  │
                                                    └──────────┬──────────┘
                                                               │
                              ┌─────────────────┬──────────────┼──────────────┐
                              │  WS 长连接      │   WS 长连接  │   WS 长连接   │
                              ▼                 ▼              ▼              ▼
┌──────────────┐      ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ Claude Code  │─────▶│ Callback A  │   │ Callback B  │   │ Callback C  │
│  本地 MacBook│      │ (本地电脑)   │   │ (本地电脑)   │   │ (云服务器)   │
└──────────────┘      └─────────────┘   └─────────────┘   └─────────────┘
                              │                 │              │
                              └─────────────────┴──────────────┘
                                 Callback 主动连接网关，无需公网可达
                                 网关通过 WS 隧道转发请求到对应 Callback
```

#### HTTP 回调模式

传统模式，网关通过 HTTP 回调 Callback，需要 Callback 公网可达。

```
                                                    ┌─────────────────────┐
                                                    │   Feishu Gateway    │
                                                    │  (飞书应用凭证)      │
                                                    └──────────┬──────────┘
                                                               │
                              ┌─────────────────┬──────────────┼──────────────┐
                              │                 │              │              │
                              ▼                 ▼              ▼              ▼
┌──────────────┐      ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ Claude Code  │─────▶│ Callback A  │   │ Callback B  │   │ Callback C  │
│   实例 A     │      │ :8081       │   │ :8082       │   │ :8083       │
└──────────────┘      └─────────────┘   └─────────────┘   └─────────────┘
                              │                 │              │
                              └─────────────────┴──────────────┘
                                        按钮 value 携带 callback_url
                                        网关自动路由到对应服务
```

> 完整的部署模式对比、配置清单和选择指南请查看 [部署模式架构文档](docs/deploy/DEPLOYMENT_MODES.md)

### 关键设计点

1. **统一 Hook 路由**: `src/hook-router.sh` 统一处理所有 Hook 事件，根据事件类型分发到对应脚本
2. **stdin 一次读取**: Hook 路由脚本读取 stdin 到 `$INPUT` 变量，子脚本共享此变量
3. **飞书卡片由前端发送**: `permission.sh` 负责构造和发送飞书卡片，回调服务只处理用户决策
4. **Unix Socket 双向通信**: 请求注册和决策返回通过同一个 Socket 连接
5. **长度前缀协议**: 使用 4 字节长度前缀确保数据完整性（详见 `src/shared/protocol.md`）
6. **连接状态驱动**: 请求有效性由 Socket 连接状态决定，支持死连接检测和超时处理
7. **优雅降级**: 回调服务不可用时自动降级为仅通知模式
8. **消息回复关联**: OpenAPI 模式下通过 `message_id` → `session` 映射实现回复继续会话（详见 `docs/design/FEISHU_SESSION_CONTINUE.md`）
9. **自动注册与双向认证**: OpenAPI 模式下 Callback 服务启动时自动向网关注册，通过 `auth_token` 实现双向认证（详见 `docs/design/GATEWAY_AUTH.md`）
10. **WS 隧道模式**: 分离部署时推荐使用 WebSocket 长连接，Callback 主动接入网关，无需公网可达，支持自动重连
11. **飞书长连接事件接收**: 支持 `longpoll` 模式通过 lark-oapi SDK 的 WebSocket 长连接接收飞书事件推送，网关无需公网端点
12. **Headless 权限审批**: 通过 `--permission-prompt-tool` MCP 方案，在 `claude -p` headless 模式下桥接权限请求到飞书审批系统（详见 `docs/design/PERMISSION_PROMPT_TOOL.md`）

## 功能特性

### 权限控制
- **可交互权限控制**: 用户可直接在飞书消息中点击按钮批准/拒绝权限请求
- **四种操作模式**: 批准运行、始终允许、拒绝运行、拒绝并中断
- **权限持久化**: 支持"始终允许"选项，自动写入项目权限规则
- **多工具支持**: 支持 Bash、Edit、Write、Read、Glob、Grep、WebSearch、WebFetch、ExitPlanMode 等 Claude Code 工具

### 会话继续（OpenAPI 模式）
- **飞书回复继续会话**: 用户可回复飞书消息在对应的 Claude session 中继续提问
- **群聊支持**: 支持将消息发送到飞书群聊，Session 自动映射到对应群聊
- **多实例支持**: 分离部署模式下，多个 Claude Code 实例可独立工作

### 系统特性
- **优雅降级**: 回调服务不可用时自动降级为仅通知模式
- **模板化卡片**: 飞书卡片 2.0 格式，支持模块化模板和自定义扩展
- **统一配置**: Shell 和 Python 共用 `config/tools.json` 工具配置
- **连接状态驱动**: 请求有效性由 Socket 连接状态决定，支持长时间等待
- **自动注册与双向认证**: Callback 服务启动时自动向网关注册（OpenAPI 分离部署）
- **用户授权控制**: 新设备注册需用户在飞书中确认，防止未授权访问
- **WS 隧道模式**: 分离部署支持 WebSocket 长连接，本地开发无需公网可达
- **飞书长连接事件接收**: 支持 longpoll 模式接收飞书事件，无需公网端点（需 lark-oapi SDK）

### 通知功能
- **任务完成通知**: Claude 处理完成后自动发送飞书通知，包含响应摘要
- **延迟发送**: 支持配置延迟时间，避免快速连续请求时的消息轰炸
- **通知 @ 用户**: 支持在通知中 @ 指定用户或所有人

### VSCode 集成
- **浏览器跳转**: 点击按钮后通过 vscode:// 协议跳转
- **自动激活**: 服务端直接激活 VSCode 窗口（推荐）
- **SSH 远程支持**: 通过反向 SSH 隧道唤起本地 VSCode

## 已完成功能

### 核心功能
- ✅ **权限通知延迟发送**: 支持配置延迟时间，避免快速连续请求时的消息轰炸（`PERMISSION_NOTIFY_DELAY`）
- ✅ **飞书卡片模板化**: 支持模块化的飞书卡片模板，便于自定义和扩展
- ✅ **决策页面自动关闭**: 支持定时自动关闭决策页面（`CALLBACK_PAGE_CLOSE_DELAY`）
- ✅ **任务完成通知**: Claude 处理完成后自动发送飞书通知，包含响应摘要和会话标识
- ✅ **Write 内容预览**: Write 工具权限卡片展示写入内容代码块预览
- ✅ **超长内容截断提示**: Permission 请求（Bash/Edit/Write）和 Stop 事件的内容超长时显示截断提示

### OpenAPI 模式
- ✅ **飞书回复继续会话**: 用户可回复飞书消息在对应的 Claude session 中继续提问
- ✅ **自动注册与双向认证**: Callback 服务启动时自动向网关注册，获取 `auth_token` 用于双向认证
- ✅ **用户授权控制**: 分离部署模式下，新设备注册需用户在飞书中确认
- ✅ **群聊支持**: 支持将消息发送到飞书群聊，Session 自动映射到对应群聊
- ✅ **WS 隧道模式**: 分离部署支持 WebSocket 长连接，本地开发无需公网 IP
- ✅ **飞书长连接事件接收**: 支持通过 lark-oapi SDK 的 WebSocket 长连接接收飞书事件，无需公网端点

### VSCode 集成
- ✅ **VSCode 自动跳转**: 点击飞书按钮后从浏览器页面自动跳转到 VSCode（`VSCODE_URI_PREFIX`）
- ✅ **VSCode 自动激活**: 服务端直接激活 VSCode 窗口（`ACTIVATE_VSCODE_ON_CALLBACK`）
- ✅ **SSH 远程代理**: 通过反向 SSH 隧道唤起本地 VSCode（`VSCODE_SSH_PROXY_PORT`）

### 安全与稳定性
- ✅ **连接状态驱动**: 请求有效性由 Socket 连接状态决定，支持死连接检测
- ✅ **请求超时处理**: 支持配置超时时间，超时后自动回退到终端交互
- ✅ **优雅降级**: 回调服务不可用时自动降级为仅通知模式
- ✅ **飞书事件验证**: 支持验证 Token 验证事件来源

### Shell 兼容性
- ✅ **多终端支持**: 支持 bash、zsh、fish 等主流终端的别名加载

## 脚本说明

| 脚本 | 用途 | Hook 类型 |
|------|------|-----------|
| `setup.sh` | 一键安装、服务管理、更新 | - |
| `src/hook-router.sh` | Hook 统一入口（配置到 Claude Code） | 所有 Hook 事件 |
| `src/hooks/user_prompt.sh` | 用户 Prompt 同步到飞书 | UserPromptSubmit |
| `src/hooks/permission.sh` | 权限请求处理（可交互） | PermissionRequest |
| `src/hooks/webhook.sh` | 通用通知（任务暂停等） | Notification |
| `src/hooks/stop.sh` | 任务完成通知（含响应摘要） | Stop |
| `src/server/main.py` | 权限回调服务（HTTP + Socket） | - |
| `src/server/socket_client.py` | Socket 客户端（替代 socat） | - |
| `src/server/handlers/claude.py` | Claude 会话继续处理器 | - |
| `src/server/services/message_session_store.py` | Message-Session 映射存储服务 | - |

## Shell 函数库说明

| 函数库 | 功能 |
|--------|------|
| `src/lib/core.sh` | 核心库：路径管理、环境配置、日志记录 |
| `src/lib/json.sh` | JSON 解析函数（支持 jq/python3/grep+sed 多级降级） |
| `src/lib/tool.sh` | 工具详情格式化 |
| `src/lib/tool-config.sh` | 工具配置加载（读取 config/tools.json） |
| `src/lib/feishu.sh` | 飞书卡片构建和发送（支持 session_id/project_dir/callback_url 参数） |
| `src/lib/socket.sh` | Socket 通信函数（长度前缀协议） |
| `src/lib/vscode-proxy.sh` | VSCode 本地代理客户端，通过反向 SSH 隧道唤起本地 VSCode |

## 服务端组件说明

### 数据模型 (`src/server/models/`)

| 模型 | 功能 |
|------|------|
| `decision.py` | 用户决策数据模型 |
| `tool_config.py` | 工具配置数据模型（从 config/tools.json 读取） |

### 业务服务 (`src/server/services/`)

| 服务 | 功能 |
|------|------|
| `request_manager.py` | 请求管理器（注册、查询、超时处理） |
| `decision_handler.py` | 决策处理器（通过 Socket 返回决策） |
| `rule_writer.py` | 权限规则写入器（"始终允许"功能） |
| `message_session_store.py` | Message-Session 映射存储（message_id → session） |
| `session_chat_store.py` | Session-Chat 映射存储（session_id → chat_id） |
| `binding_store.py` | 网关注册绑定存储（owner_id → callback_url + auth_token） |
| `dir_history_store.py` | 目录历史记录存储（常用工作目录推荐） |
| `auto_register.py` | 网关注册服务（Callback 自动向网关注册） |
| `auth_token.py` | 认证令牌管理（生成、验证、刷新） |
| `auth_token_store.py` | 认证令牌存储（网关注册返回的 token） |
| `feishu_api.py` | 飞书 API 封装（发送消息、上传图片等） |
| `feishu_longpoll.py` | 飞书 WebSocket 长连接服务（lark-oapi SDK） |

### HTTP 处理器 (`src/server/handlers/`)

| 处理器 | 功能 | 端点 |
|--------|------|------|
| `http_handler.py` | HTTP 请求处理器（GET/POST 路由分发入口） | 全部 |
| `callback.py` | 权限回调处理器（接收按钮操作） | `/cb/*` |
| `feishu.py` | 飞书事件处理器（OpenAPI 网关） | `/gw/feishu/*` |
| `claude.py` | Claude 会话继续处理器 | `/cb/claude/continue` |
| `register.py` | 网关注册处理器 | `/gw/register` |
| `permission_mcp.py` | MCP 权限审批服务（headless 模式） | MCP stdio |
| `utils.py` | 处理器通用工具函数 | - |

## VSCode SSH 远程开发代理

通过反向 SSH 隧道，在远程服务器上触发请求后自动唤起本地电脑的 VSCode 窗口。适用于 VSCode 通过 SSH Remote 连接到远程服务器的开发模式。

### 使用方法

1. **在本地电脑启动代理服务**：

```bash
python3 src/proxy/vscode_ssh_proxy.py --vps myserver
```

参数说明：
- `--vps`: SSH 地址（别名如 `myserver`，或 `root@1.2.3.4`）
- `--ssh-port`: SSH 端口（默认 22，使用别名时从 `~/.ssh/config` 读取）
- `--port`: 本地 HTTP 服务端口（默认 9527）
- `--remote-port`: 远程服务器端端口（默认同本地端口）

2. **在远程服务器上配置环境变量**（`.env` 文件）：

```bash
# 启用 VSCode 自动激活
ACTIVATE_VSCODE_ON_CALLBACK=true

# SSH 隧道代理端口（需与启动时的端口一致）
VSCODE_SSH_PROXY_PORT=9527
```

3. **工作流程**：
   - 点击飞书卡片按钮后，远程服务器通过 SSH 隧道向本地代理发送请求
   - 本地代理调用 `code --folder-uri vscode-remote://ssh-remote+myserver/path/to/project`
   - 本地 VSCode 自动打开并激活对应远程项目

### 两种激活模式

| 模式 | 配置 | 行为 |
|------|------|------|
| **SSH 隧道代理激活** | `ACTIVATE_VSCODE_ON_CALLBACK=true` + `VSCODE_SSH_PROXY_PORT=9527` | 通过反向 SSH 隧道唤起本地 VSCode 窗口 |
| **本地命令激活** | `ACTIVATE_VSCODE_ON_CALLBACK=true` | 在当前机器执行 `code .` 激活窗口 |

## 配置方法

### 手动配置（不使用 install.sh）

#### 1. 设置 Webhook URL

```bash
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx"
```

#### 2. 启动回调服务

```bash
./src/start-server.sh
```

#### 3. 配置 Claude Code Hooks

在 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-anywhere/src/hook-router.sh"
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-anywhere/src/hook-router.sh",
            "timeout": 660
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-anywhere/src/hook-router.sh"
          }
        ]
      }
    ]
  }
}
```

> **注意**：PermissionRequest hook 的 `timeout`（秒）建议配置为大于服务端 `PERMISSION_REQUEST_TIMEOUT`（默认 600 秒）的值，确保服务端超时先触发，避免 hook 被 Claude Code 强制终止。上例配置为 660 秒。

### 环境变量

**配置优先级**：`.env` 文件 > 环境变量 > 默认值

脚本会自动从项目根目录的 `.env` 文件读取配置，无需手动 `source`。如果 `.env` 中未配置某项，则从系统环境变量读取；如果仍未配置，则使用默认值。

#### 一、发送模式（必选）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_SEND_MODE` | 发送模式：`webhook` / `openapi` | `webhook` |

**模式对比**：

| 特性 | Webhook 模式 | OpenAPI 模式 |
|------|-------------|-------------|
| 配置复杂度 | 简单 | 需要飞书应用 |
| 按钮交互 | 跳转浏览器 | 飞书内 toast 响应 |
| 多实例支持 | 不支持 | 支持（分离部署） |
| 回复继续会话 | 不支持 | 支持 |

#### 二、飞书凭证与身份

**Webhook 模式**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook URL | - |

**OpenAPI 模式 — 应用凭证**

> **重要**：需要在飞书开放平台配置事件订阅「卡片回传交互」(`card.action.trigger`)

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_APP_ID` | 飞书应用 App ID（单机/网关必填） | - |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret（单机/网关必填） | - |
| `FEISHU_VERIFICATION_TOKEN` | 飞书验证 Token（HTTP 回调模式建议配置，长连接模式不需要） | - |
| `FEISHU_EVENT_MODE` | 飞书事件接收模式：`auto`（默认）/ `http` / `longpoll` | `auto` |

**OpenAPI 模式 — 事件接收**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_EVENT_MODE` | 事件接收模式：`auto` / `http` / `longpoll` | `auto` |

- **auto**（默认）：自动检测 — 有 `lark-oapi` SDK 且 Python >= 3.8 则使用 longpoll，否则 http
- **http**：传统 HTTP 回调模式，需要公网端点接收飞书事件
- **longpoll**：WebSocket 长连接模式，网关主动连接飞书，无需公网端点

> **longpoll 模式优势**：无需公网 IP 和 HTTPS 证书，适用于本地开发和内网部署。需安装 `lark-oapi` SDK：`python3 -m pip install lark-oapi`

**OpenAPI 模式 — 网关（分离部署）**

配置 `FEISHU_GATEWAY_URL` 后启用分离部署：

- 网关服务配置：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- Callback 服务配置：`FEISHU_GATEWAY_URL`（不配置则默认使用 `CALLBACK_SERVER_URL`，协议头决定连接模式）
- Callback 服务启动时自动向网关注册获取 `auth_token`（用于后续通信验证）

| 变量 | 网关服务 | Callback 服务 |
|------|:-------:|:------------:|
| `FEISHU_GATEWAY_URL` | - | ✓（分离部署必需） |
| `FEISHU_APP_ID` | ✓ | - |
| `FEISHU_APP_SECRET` | ✓ | - |
| `FEISHU_VERIFICATION_TOKEN` | ✓（HTTP 回调模式） | - |
| `FEISHU_EVENT_MODE` | ✓（可选） | - |
| `FEISHU_OWNER_ID` | ✓ | ✓ |
| `FEISHU_CHAT_ID` | ✓ | ✓（客户端读取） |

**连接模式选择**：

| 协议头 | 模式 | 说明 |
|--------|------|------|
| `ws://` / `wss://` | **WS 隧道**（推荐） | Callback 主动连接网关，无需公网可达，本地开发首选 |
| `http://` / `https://` | HTTP 回调 | 网关回调 Callback，需 Callback 公网可达 |

**分离部署配置示例**：

```bash
# === 飞书网关服务 ===
FEISHU_SEND_MODE=openapi
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxx
FEISHU_EVENT_MODE=auto                        # auto/http/longpoll（默认 auto）
# FEISHU_VERIFICATION_TOKEN=your_token        # HTTP 回调模式建议配置，longpoll 模式不需要
CALLBACK_SERVER_PORT=8080
FEISHU_OWNER_ID=ou_admin_user

# === Callback 服务（WS 隧道模式，推荐）===
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=ws://gateway-server:8080  # 使用 ws:// 协议头启用 WS 隧道
CALLBACK_SERVER_URL=http://localhost:8081    # 本地开发无需公网可达
CALLBACK_SERVER_PORT=8081
FEISHU_OWNER_ID=ou_admin_user

# === Callback 服务（HTTP 回调模式）===
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=http://gateway-server:8080  # 使用 http:// 协议头
CALLBACK_SERVER_URL=http://callback-server-a:8081  # 需要公网可达
CALLBACK_SERVER_PORT=8081
FEISHU_OWNER_ID=ou_admin_user
```

> **协议头说明**：`FEISHU_GATEWAY_URL` 的协议头决定连接模式
> - `ws://` 或 `wss://` → WS 隧道模式（Callback 无需公网可达，本地开发推荐）
> - `http://` 或 `https://` → HTTP 回调模式（Callback 需公网可达）

**消息接收者**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_OWNER_ID` | 默认消息接收者（**必须使用 user_id 格式**） | 空 |
| `FEISHU_CHAT_ID` | 默认群聊 ID（由客户端读取，作为参数传递） | 空 |

> `FEISHU_OWNER_ID` 说明：
> - **必须使用 user_id 格式**（纯数字或字母数字组合），其他 ID 类型会导致认证问题
> - 获取方式：[如何获取 User ID](https://open.feishu.cn/document/faq/trouble-shooting/how-to-obtain-user-id)
> - **Webhook 模式**：可选，仅用于通知 @ 用户
> - **OpenAPI 模式**：**必需**，用于确定消息接收者和网关注册

**消息发送优先级**：`chat_id` 参数 > `FEISHU_CHAT_ID` 配置 > `owner_id`

**Session 与群聊映射**：当用户在群聊中回复消息继续会话时，系统会自动记录 `session_id → chat_id` 映射，后续该 session 的通知会自动发送到对应群聊。

#### 三、回调服务

> 分离部署时，仅 Callback 服务需要配置此部分，网关服务不需要

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CALLBACK_SERVER_URL` | 回调服务外部访问地址（所有模式建议配置） | `http://localhost:8080` |
| `CALLBACK_SERVER_PORT` | HTTP 服务端口 | 8080 |
| `PERMISSION_SOCKET_PATH` | Unix Socket 路径 | `/tmp/claude-permission.sock` |

#### 四、通知与交互行为

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FEISHU_AT_USER` | 通知 @ 用户配置：空=@ `FEISHU_OWNER_ID`，`all`=@ 所有人，`off`=禁用 | 空 |
| `FEISHU_REPLY_IN_THREAD` | 话题内回复模式：回复消息是否收进话题详情（仅 OpenAPI 模式） | `false` |
| `PERMISSION_REQUEST_TIMEOUT` | 服务器端超时秒数（0 禁用） | 600 |
| `PERMISSION_NOTIFY_DELAY` | 权限通知延迟发送秒数 | 60 |
| `CALLBACK_PAGE_CLOSE_DELAY` | 回调页面自动关闭秒数（建议范围 1-10） | 3 |
| `STOP_THINKING_MAX_LENGTH` | Stop 事件思考过程最大长度（字符数，0 禁用） | 10000 |
| `STOP_MESSAGE_MAX_LENGTH` | Stop 事件消息最大长度（字符数） | 10000 |

#### 五、Claude 会话配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEFAULT_CHAT_DIR` | 默认聊天目录，配置后直接发消息即可自动创建/继续会话，详见下文 | 空（不启用） |
| `DEFAULT_CHAT_FOLLOW_THREAD` | 默认聊天目录的话题跟随模式，详见下文 | `true` |
| `CLAUDE_COMMAND` | Claude 命令，支持多命令列表如 `[claude, claude --setting opus]`，详见下文 | `claude` |

**默认聊天目录**

配置 `DEFAULT_CHAT_DIR` 后，无需使用 `/new` 指令，直接发消息即可与 Claude 对话：

```bash
DEFAULT_CHAT_DIR=/home/user/my-project
```

行为：
- 直接发送消息 → 自动在默认目录继续活跃会话（无活跃会话时自动创建）
- `/new prompt` → 在默认目录创建新会话（替换当前活跃会话）
- `/new --dir=/other prompt` → 在指定目录创建（不影响默认会话）
- 服务启动时自动创建不存在的目录

**话题跟随模式**

`DEFAULT_CHAT_FOLLOW_THREAD` 控制默认聊天目录的回复是否收敛进话题：

```bash
# 跟随全局配置（默认）- 若 FEISHU_REPLY_IN_THREAD=true，则回复收敛进话题
DEFAULT_CHAT_FOLLOW_THREAD=true

# 始终在主界面显示 - 回复不收敛进话题，适合即时通讯场景
DEFAULT_CHAT_FOLLOW_THREAD=false
```

| 配置值 | 行为 |
|--------|------|
| `true`（默认） | 跟随 `FEISHU_REPLY_IN_THREAD` 全局配置 |
| `false` | 默认聊天目录的回复始终在群聊主界面显示 |

**多命令配置**

支持配置多个 Claude 命令，在创建/继续会话时选择使用哪个：

```bash
# 单命令（向后兼容）
CLAUDE_COMMAND=claude

# 多命令（列表格式，无需引号）
CLAUDE_COMMAND=[claude, claude --setting opus]
```

配置多命令后：
- `/new` 卡片会显示 Command 选择下拉框
- `/new --cmd=1 --dir=/path prompt` — 按索引选择
- `/new --cmd=opus --dir=/path prompt` — 按名称子串匹配
- `/reply --cmd=opus prompt` — 回复消息时指定 Command（仅在回复消息时可用）
- 每个 session 会记忆最近使用的 Command，后续回复自动复用

#### 六、VSCode 集成（可选，以下两种模式二选一）

| 模式 | 变量 | 说明 | 默认值 |
|------|------|------|--------|
| 浏览器跳转 | `VSCODE_URI_PREFIX` | 通过浏览器 vscode:// 协议跳转（受浏览器策略限制） | 空（不跳转） |
| 自动激活 | `ACTIVATE_VSCODE_ON_CALLBACK` | 服务端直接激活 VSCode 窗口（推荐） | `false` |
| 自动激活 | `VSCODE_SSH_PROXY_PORT` | SSH Remote 场景的代理端口（配合反向隧道） | 空（不启用） |

## 依赖

### 必需依赖
- `python3` - 回调服务 + Socket 客户端
- `curl` - 发送 HTTP 请求
- `bash` - Hook 脚本执行

### 可选依赖
- `jq` - 更好的 JSON 处理（脚本会自动降级使用 Python3 或 grep/sed）
- `socat` - Socket 通信备选方案（有 Python socket_client 作为替代）
- `lark-oapi` - 飞书长连接模式所需（`pip install lark-oapi`，需 Python >= 3.8）

### 安装

```bash
# Ubuntu/Debian
apt-get install python3 curl jq socat

# CentOS/RHEL
yum install python3 curl jq socat

# macOS
brew install python3 curl jq socat
```

### Python 版本要求

本项目 Python 代码兼容 **Python 3.6+**，编写代码时需注意：

| 特性 | Python 3.6+ 兼容写法 | 不要使用 |
|------|---------------------|----------|
| 类型注解 | `from typing import Dict, List`<br>`Dict[str, Any]` | 小写内置泛型 `dict[str, Any]` (3.9+) |
| 联合类型 | `Optional[int]`<br>`Union[str, int]` | `int \| None` (3.10+) |
| subprocess | `universal_newlines=True` | `text=True` (3.7+) |
| 运算符 | 普通赋值 `x = foo()` | `:=` walrus (3.8+) |

## 使用流程

### 权限交互流程

1. Claude Code 发起权限请求
2. `src/hooks/permission.sh` 发送飞书交互卡片（4 个按钮）：
   - **批准运行** - 允许这一次执行
   - **始终允许** - 允许并写入规则
   - **拒绝运行** - 拒绝，Claude 可继续尝试其他方式
   - **拒绝并中断** - 拒绝并停止当前任务
3. 用户点击按钮，决策通过 Unix Socket 返回给 Claude Code

### 降级模式

回调服务不可用时：
- 发送不带交互按钮的通知卡片
- 用户需在终端手动确认

### 飞书回复继续会话（OpenAPI 模式）

1. Claude 完成响应后，飞书通知包含 `session_id`
2. 用户直接回复飞书消息
3. 网关通过 `message_id` 找到对应的 session
4. 消息注入到 Claude session，继续对话

> 详细原理请参考 [飞书消息回复继续会话方案](docs/design/FEISHU_SESSION_CONTINUE.md)

### 飞书指令

| 指令 | 说明 |
|------|------|
| `/new` | 发起新会话，弹出目录选择卡片 |
| `/new --dir=/path prompt` | 直接指定目录和提示词创建会话 |
| `/new --cmd=1 --dir=/path prompt` | 指定 Claude Command（按索引或名称子串） |
| `/reply --cmd=opus prompt` | 回复消息时指定 Command 继续会话 |

- `/reply` 仅在回复消息时可用，用于临时切换 Command 继续会话
- 未指定 `--cmd` 时，使用 session 记忆的 Command 或默认命令

## 日志

日志文件位于 `log/` 目录，按组件分子目录存放，按天自动轮转：

| 文件模式 | 说明 |
|----------|------|
| `hook/YYYY-MM-DD.log` | Hook 脚本日志 |
| `callback/YYYY-MM-DD.log` | 回调服务日志 |
| `socket_client/YYYY-MM-DD.log` | Socket 客户端日志 |
| `feishu_message/YYYY-MM-DD.log` | 飞书消息日志 |
| `feishu_longpoll/YYYY-MM-DD.log` | 飞书长连接日志 |
| `permission_mcp/YYYY-MM-DD.log` | MCP 权限审批日志 |
| `command/YYYY-MM-DD_{session}.log` | 命令日志（按 session 分文件） |

日志配置统一定义在 `shared/logging.json`。

## 文档

### 部署指南
- [部署模式选择指南](docs/deploy/DEPLOYMENT_MODES.md) - Webhook 与 OpenAPI 模式对比与选择
- [Webhook 模式部署](docs/deploy/DEPLOYMENT_WEBHOOK.md) - Webhook 模式详细部署文档
- [OpenAPI 模式部署](docs/deploy/DEPLOYMENT_OPENAPI.md) - OpenAPI 模式详细部署文档

### 架构设计
- [飞书网关认证与注册机制](docs/design/GATEWAY_AUTH.md) - Callback 后端自动注册与双向认证机制
- [飞书消息回复继续会话方案](docs/design/FEISHU_SESSION_CONTINUE.md) - 飞书回复继续 Claude 会话的设计与实现
- [飞书话题内回复模式](docs/design/FEISHU_THREAD_REPLY.md) - 同一会话消息收敛到话题流的链式回复设计
- [交互式 Claude 会话调研](docs/design/INTERACTIVE_CLAUDE_SESSION_INVESTIGATION.md) - 交互式 CLI 会话方案调研报告
- [Headless 权限审批 MCP 方案](docs/design/PERMISSION_PROMPT_TOOL.md) - `--permission-prompt-tool` MCP 权限审批设计
- [ACP Oneshot Client 方案（已废弃）](docs/design/ACP_ONESHOT_CLIENT.md) - 基于 ACP 的早期调研方案
- [通信鉴权与安全分析](docs/design/SECURITY_ANALYSIS.md) - 通信安全风险评估与加固建议
- [Socket 通信协议](src/shared/protocol.md) - Unix Socket 通信规范

### 参考资料
- [用户使用指南](docs/reference/USER_GUIDE.md) - 从用户视角的完整使用说明（指令、卡片交互、错误处理）
- [Claude Code Hooks 事件调研](docs/reference/CLAUDE_CODE_HOOKS.md) - 所有 hooks 事件类型、触发时机和配置方式
- [代码审查记录](docs/reference/CODE_REVIEW.md) - 代码审查与改进记录
- [飞书卡片模板说明](src/templates/feishu/README.md) - 卡片模板使用指南

### OpenSpec
- [Agent 规范](openspec/AGENTS.md) - OpenSpec Agent 使用说明
- [OpenSpec 规范](openspec/specs/) - 项目变更规范管理

### 测试文档
- [测试文档](test/README.md) - 测试脚本使用说明
- [测试指令集](test/PROMPTS.md) - 权限请求测试指令
- [测试场景](test/SCENARIOS.md) - 详细测试场景文档
