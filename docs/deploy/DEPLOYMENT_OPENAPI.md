# OpenAPI 模式部署文档

本文档详细说明 OpenAPI 模式的架构设计和部署步骤，包括单机部署和分离部署两种方式。

## 目录

- [模式简介](#模式简介)
- [架构概览](#架构概览)
- [组件列表](#组件列表)
- [配置说明](#配置说明)
- [飞书应用配置](#飞书应用配置)
- [部署步骤](#部署步骤)
- [网关注册与双向认证](#网关注册与双向认证)
- [群聊功能](#群聊功能)
- [安全配置](#安全配置)
- [常见问题](#常见问题)

---

## 模式简介

OpenAPI 模式使用飞书开放平台 API 发送消息，支持飞书内直接响应、多实例部署和消息回复继续会话等高级功能。

### 特点

| 特性 | 说明 |
|------|------|
| **飞书应用** | 需要创建飞书开放平台应用 |
| **交互方式** | 按钮直接在飞书内响应 |
| **配置复杂度** | 较复杂（需配置应用凭证） |
| **部署方式** | 单机部署 / 分离部署 |
| **推荐场景** | 团队协作、多机器部署 |

### 支持功能

- ✅ 飞书内直接响应（无需浏览器）
- ✅ 多实例部署
- ✅ 消息回复继续会话
- ✅ 群聊支持
- ✅ 自动注册与双向认证
- ✅ 用户授权控制
- ✅ 长连接事件接收（longpoll 模式，无需公网端点）

### 适用场景

**单机部署**：
- 团队协作使用
- 需要飞书内直接响应
- 需要回复继续会话功能

**分离部署**：
- 多机器/多实例部署
- 共享飞书应用
- 分布式架构

---

## 架构概览

### 单机部署架构

所有服务运行在同一台机器上。

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│  Claude Code    │────────▶│ src/hook-router  │────────▶│  飞书 OpenAPI   │
│                 │  Hook   │  → permission.sh │  Card   │  (access_token) │
└─────────────────┘         └────────┬─────────┘         └─────────────────┘
                                     │
                                     │ Unix Socket
                                     │ (注册请求)
                                     ▼
                            ┌──────────────────┐
                            │   src/server/    │
                            │   HTTP Server    │
                            └────────┬─────────┘
                                     │
                                     │ Feishu Event
                                     │ (card.action.trigger)
                                     ▼
                            ┌──────────────────┐         ┌─────────────────┐
                            │   src/server/    │────────▶│ src/hooks/      │
                            │   (resolve)      │  Socket │  permission.sh  │
                            └──────────────────┘         └────────┬─────────┘
                                                                 │
                                                                 │ Decision JSON
                                                                 ▼
                                                            ┌─────────────────┐
                                                            │  Claude Code    │
                                                            │  (继续/拒绝)     │
                                                            └─────────────────┘
```

### 分离部署架构

飞书网关与 Callback 服务分离，支持多实例。支持两种通信方式：

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
       │                     │                 │              │
       │                     └─────────────────┴──────────────┘
       │                          Callback 主动连接网关，无需公网可达
       │                          网关通过 WS 隧道转发请求到对应 Callback
       ▼
 (独立 FEISHU_OWNER_ID)
```

**WS 隧道优势**：
- 无需公网 IP：本地电脑（如 MacBook）可直接接入
- 自动重连：网络波动后自动恢复连接
- 实时通信：长连接，低延迟
- 安全认证：基于 auth_token 的双向认证

#### HTTP 回调模式

传统模式，网关通过 HTTP 回调 Callback，需要 Callback 公网可达。

```
                                                    ┌─────────────────────┐
                                                    │   Feishu Gateway    │
                                                    │  (飞书应用凭证)      │
                                                    │  FEISHU_APP_ID      │
                                                    │  FEISHU_APP_SECRET  │
                                                    └──────────┬──────────┘
                                                               │
                              ┌─────────────────┬──────────────┼──────────────┐
                              │                 │              │              │
                              ▼                 ▼              ▼              ▼
┌──────────────┐      ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ Claude Code  │─────▶│ Callback A  │   │ Callback B  │   │ Callback C  │
│   实例 A     │      │ :8081       │   │ :8082       │   │ :8083       │
└──────────────┘      └─────────────┘   └─────────────┘   └─────────────┘
       │                     │                 │              │
       │                     └─────────────────┴──────────────┘
       │                               按钮 value 携带 callback_url
       │                               网关自动路由到对应服务
       ▼
 (独立 FEISHU_OWNER_ID)
```

### 数据流向

#### 发送消息流程

```
1. Claude Code 触发权限请求
       ↓
2. hook-router.sh 接收 Hook 事件
       ↓
3. permission.sh 构建飞书卡片
       ↓
4. 通过 Unix Socket 注册请求到回调服务
       ↓
5. 调用飞书 OpenAPI 发送卡片
   - 单机：直接调用 message/send
   - 分离：通过网关调用 /gw/feishu/send
       ↓
6. 飞书群聊显示交互卡片
```

#### 用户操作流程（飞书内响应）

```
1. 用户点击飞书卡片按钮
       ↓
2. 飞书发送 card.action.trigger 事件
   - 单机：直接发送到本机服务
   - 分离：发送到网关
       ↓
3. 事件处理器解析操作
       ↓
4. 通过 Unix Socket 返回决策
       ↓
5. permission.sh 接收决策
       ↓
6. Claude Code 继续或拒绝执行
       ↓
7. 飞书显示操作结果 Toast
```

---

## 组件列表

| 组件 | 路径 | 说明 |
|------|------|------|
| Hook 路由 | `src/hook-router.sh` | Claude Code Hook 统一入口 |
| 权限处理 | `src/hooks/permission.sh` | 构建并发送飞书卡片 |
| 卡片模板 | `src/templates/feishu/buttons-openapi.json` | OpenAPI 模式卡片模板 |
| 回调服务 | `src/server/main.py` | ThreadedHTTPServer，处理飞书事件回调 |
| 飞书事件处理器 | `src/server/handlers/feishu.py` | 处理飞书事件（网关功能） |
| 会话继续 | `src/server/handlers/claude.py` | 处理回复继续会话 |
| Message-Session Store | `src/server/services/message_session_store.py` | 消息 ID 与会话映射 |
| Session-Chat Store | `src/server/services/session_chat_store.py` | 会话 ID 与群聊映射 |
| Auth Token Store | `src/server/services/auth_token_store.py` | 网关注册令牌存储 |
| Binding Store | `src/server/services/binding_store.py` | 网关注册绑定存储（owner_id → callback_url） |
| Dir History Store | `src/server/services/dir_history_store.py` | 目录使用历史 |
| 飞书 API 服务 | `src/server/services/feishu_api.py` | 飞书 OpenAPI 封装 |
| 飞书长连接 | `src/server/services/feishu_longpoll.py` | 飞书 WebSocket 长连接事件接收 |
| 自动注册 | `src/server/services/auto_register.py` | Callback 向网关注册 |
| 认证令牌 | `src/server/services/auth_token.py` | 双向认证令牌管理 |

---

## 配置说明

### 单机部署配置

```bash
# .env 文件
FEISHU_SEND_MODE=openapi

# 必需配置
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxx
FEISHU_OWNER_ID=ou_xxx  # 默认消息接收者（必需，user_id 格式）

# 事件接收模式（可选，默认 auto）
# auto: 有 lark-oapi 则 longpoll，否则 http
# http: 传统 HTTP 回调（需公网端点）
# longpoll: WebSocket 长连接（无需公网端点）
FEISHU_EVENT_MODE=auto

# HTTP 回调模式建议配置（longpoll 模式不需要）
# FEISHU_VERIFICATION_TOKEN=your_verification_token

# 可选配置
CALLBACK_SERVER_URL=http://localhost:8080
CALLBACK_SERVER_PORT=8080
```

### 分离部署配置

#### 网关服务配置

```bash
# 网关 .env
FEISHU_SEND_MODE=openapi
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxx
FEISHU_OWNER_ID=ou_admin_user  # 默认接收者（user_id 格式）
CALLBACK_SERVER_PORT=8080
FEISHU_EVENT_MODE=auto  # auto/http/longpoll（默认 auto）
# FEISHU_VERIFICATION_TOKEN=your_token  # HTTP 回调模式建议配置
```

#### Callback 服务配置

**WS 隧道模式（推荐）**：

```bash
# Callback 实例 .env（本地开发）
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=ws://gateway-server:8080  # ws:// 启用 WS 隧道
CALLBACK_SERVER_URL=http://localhost:8081    # 本地开发无需公网可达
CALLBACK_SERVER_PORT=8081
FEISHU_OWNER_ID=ou_user_a  # 此实例的消息接收者（user_id 格式）
```

**HTTP 回调模式**：

```bash
# Callback 实例 .env（云服务器）
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=http://gateway-server:8080  # http:// HTTP 回调
CALLBACK_SERVER_URL=http://instance-a:8081     # 需要公网可达
CALLBACK_SERVER_PORT=8081
FEISHU_OWNER_ID=ou_user_a  # 此实例的消息接收者（user_id 格式）
```

> **协议头说明**：`FEISHU_GATEWAY_URL` 的协议头决定连接模式
> - `ws://` 或 `wss://` → WS 隧道模式（Callback 无需公网可达，本地开发推荐）
> - `http://` 或 `https://` → HTTP 回调模式（Callback 需公网可达）

> **注意**：Callback 服务不需要 `FEISHU_VERIFICATION_TOKEN`，网关请求通过注册时下发的 `auth_token` 验证。

### 配置清单

| 变量 | 必需 | 默认值 | 说明 |
|------|:----:|:------:|------|
| `FEISHU_SEND_MODE` | 否 | `webhook` | 发送模式（设为 `openapi`） |
| `FEISHU_APP_ID` | **网关** | - | 飞书应用 App ID（单机/网关必需，Callback 不需要） |
| `FEISHU_APP_SECRET` | **网关** | - | 飞书应用 App Secret（单机/网关必需，Callback 不需要） |
| `FEISHU_VERIFICATION_TOKEN` | 否 | - | 飞书验证 Token（HTTP 回调模式建议配置，longpoll 模式不需要） |
| `FEISHU_EVENT_MODE` | 否 | `auto` | 事件接收模式：`auto`/`http`/`longpoll` |
| `FEISHU_OWNER_ID` | **是** | - | 默认消息接收者（OpenAPI 模式必需） |
| `FEISHU_CHAT_ID` | 否 | - | 默认群聊 ID |
| `FEISHU_GATEWAY_URL` | **Callback** | `CALLBACK_SERVER_URL` | 网关地址（分离部署时配置）。协议头决定连接模式：`ws://` 启用 WS 隧道（推荐），`http://` 使用 HTTP 回调。不配置则默认使用 `CALLBACK_SERVER_URL` |
| `CALLBACK_SERVER_URL` | **是** | `http://localhost:8080` | 回调服务外部访问地址（WS 隧道模式下可使用 localhost） |
| `CALLBACK_SERVER_PORT` | 否 | `8080` | 回调服务监听端口 |
| `FEISHU_REPLY_IN_THREAD` | 否 | `false` | 话题内回复模式：回复消息收进话题详情，不刷群聊主界面 |

---

## 飞书应用配置

### 1. 创建应用

1. 访问 [飞书开放平台](https://open.feishu.cn/app)
2. 点击「创建企业自建应用」
3. 填写应用名称和描述

### 2. 配置事件与回调

在「事件与回调」中配置事件订阅：

**事件配置**：添加「接收消息」（`im.message.receive_v1`）事件，并确认以下权限：
- 读取用户发给机器人的单聊消息（添加事件后默认开通）
- 接收群聊中 @机器人消息事件（**需手动点击开通权限**，否则无法接收群聊中 at 机器人的消息）

**回调配置**：添加「卡片回传交互」（`card.action.trigger`）回调

**订阅方式**：根据事件接收模式选择：
- **longpoll 模式**：选择「WebSocket 长连接」方式，无需填写请求地址
- **HTTP 回调模式**：选择「HTTP 回调」方式，请求地址填写 `CALLBACK_SERVER_URL` 的值

> 中间如果提示「尚未开启机器人功能」，点击开启即可。

### 3. 配置权限

在「权限管理」中开通以下权限：

| 权限名称 | 权限代码 |
|----------|----------|
| 以应用的身份发消息 | `im:message:send_as_bot` |
| 获取用户 user ID | `contact:user.employee_id:readonly` |

> 权限可访问的数据范围：与应用的可用范围一致即可。

### 4. 创建版本并发布

完成权限配置后，前往「版本管理与发布」，填写版本号和更新说明，发布应用。

> 以上权限默认免审批。如果要开放给多人使用，需在「可用范围」中添加，会触发审批流程。

### 5. 获取凭证

- `App ID` 和 `App Secret`：在「凭证与基础信息」中获取
- `Verification Token`：在「事件与回调」→「加密策略」中获取（HTTP 回调模式需要，longpoll 模式不需要）

### 6. 获取 OWNER_ID

`FEISHU_OWNER_ID` 是消息接收者的飞书 user_id。获取方式参考 [如何获取 User ID](https://open.feishu.cn/document/faq/trouble-shooting/how-to-obtain-user-id)，也可以通过飞书开放平台的 [获取单个用户信息](https://open.feishu.cn/document/server-docs/contact-v3/user/get) 接口在线调试获取。

---

## 部署步骤

### 一键安装（推荐）

使用 `setup.sh` 可自动完成下载源码、依赖检测、Hook 配置和 .env 配置：

**单机模式**：

```bash
# 已有 lark-oapi（长连接模式，推荐）
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --app-id=cli_xxx --app-secret=xxx --owner-id=<用户ID>

# 未安装 lark-oapi（HTTP 回调模式）
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --app-id=cli_xxx --app-secret=xxx --verification-token=xxx --owner-id=<用户ID>
```

**分离模式**（Callback 服务）：

```bash
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --gateway-url=ws://gateway:8080 --owner-id=<用户ID>
```

**服务管理**：

```bash
./setup.sh start      # 启动服务
./setup.sh stop       # 停止服务
./setup.sh restart    # 重启服务
./setup.sh status     # 查看状态
./setup.sh update     # 更新代码并重启
```

**参数说明**：

| 参数 | 说明 |
|------|------|
| `--app-id` | 飞书应用 App ID（单机模式必填） |
| `--app-secret` | 飞书应用 App Secret（单机模式必填） |
| `--verification-token` | 飞书 Verification Token（HTTP 回调模式需要） |
| `--gateway-url` | 飞书网关地址（分离模式必填） |
| `--owner-id` | 飞书用户 ID（分离模式必填，单机模式可稍后配置） |
| `--port` | 服务端口（默认 8080） |

#### lark-oapi 安装逻辑

单机模式下，脚本会根据环境自动处理 `lark-oapi` 依赖：

```
已安装 lark-oapi?
   └─ 是 ──→ 使用长连接模式 ✓
   └─ 否 ──→ 有 --verification-token?
               ├─ 是（HTTP 回调模式可选）
               │    ├─ Python < 3.8 ──→ 提示升级，继续使用 HTTP 回调 ✓
               │    └─ Python ≥ 3.8 ──→ 询问是否安装
               │         ├─ 安装成功 ──→ 使用长连接模式 ✓
               │         └─ 不安装/失败 ──→ 使用 HTTP 回调模式 ✓
               └─ 否（必须安装或提供 token）
                    ├─ Python < 3.8 ──→ 提示升级或提供 token，退出 ✗
                    └─ Python ≥ 3.8 ──→ 询问是否安装
                         ├─ 安装成功 ──→ 使用长连接模式 ✓
                         └─ 不安装/失败 ──→ 提示提供 token，退出 ✗
```

> **说明**：分离模式（`--gateway-url`）不涉及 lark-oapi 安装，直接连接网关服务。

---

### 单机部署（手动）

```bash
# 1. 在飞书开放平台创建应用，获取凭证
# 记录 APP_ID、APP_SECRET、VERIFICATION_TOKEN

# 2. 运行安装脚本
./install.sh

# 3. 配置环境变量
cp .env.example .env
vim .env
```

**配置示例**：
```bash
FEISHU_SEND_MODE=openapi
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OWNER_ID=ou_xxx  # 默认接收者（user_id 格式）
FEISHU_EVENT_MODE=auto   # longpoll 模式无需公网端点
CALLBACK_SERVER_URL=http://your-server:8080
# FEISHU_VERIFICATION_TOKEN=xxx  # HTTP 回调模式建议配置
```

```bash
# 4. 配置飞书应用事件订阅
# longpoll 模式：选择 WebSocket 长连接方式，无需请求地址
# HTTP 回调模式：请求地址填 http://your-server:8080/feishu/event

# 5. 启动服务
./src/start-server.sh

# 6. 启动 Claude Code
claude
```

### 分离部署（手动）

#### 网关服务部署

```bash
# 在网关服务器上
git clone ... && cd claude-anywhere
./install.sh

# 配置
cat > .env << EOF
FEISHU_SEND_MODE=openapi
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OWNER_ID=ou_admin  # user_id 格式
FEISHU_EVENT_MODE=auto
CALLBACK_SERVER_PORT=8080
EOF

# 启动网关
./src/start-server.sh
```

#### Callback 服务部署

在每个需要运行 Claude Code 的机器上：

**WS 隧道模式（推荐，本地开发）**：

```bash
# 在本地电脑上
git clone ... && cd claude-anywhere
./install.sh

# 配置
cat > .env << EOF
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=ws://gateway-server:8080  # ws:// 启用 WS 隧道
FEISHU_OWNER_ID=ou_user_a                    # user_id 格式
CALLBACK_SERVER_URL=http://localhost:8081    # 本地无需公网可达
CALLBACK_SERVER_PORT=8081
EOF

# 启动服务
./src/start-server.sh
```

**HTTP 回调模式（云服务器）**：

```bash
# 在实例机器上
git clone ... && cd claude-anywhere
./install.sh

# 配置
cat > .env << EOF
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=http://gateway-server:8080
FEISHU_OWNER_ID=ou_user_a                    # user_id 格式
CALLBACK_SERVER_URL=http://this-machine:8081  # 需要公网可达
CALLBACK_SERVER_PORT=8081
EOF

# 启动服务
./src/start-server.sh
```

---

## 网关注册与双向认证

### 功能说明

分离部署模式下，Callback 服务启动时自动向飞书网关注册，获取 `auth_token` 用于后续通信认证。

### 连接模式对比

| 特性 | WS 隧道模式 | HTTP 回调模式 |
|------|:-----------:|:-------------:|
| **网络要求** | Callback 无需公网可达 | Callback 需公网可达 |
| **连接方向** | Callback → 网关（主动） | 网关 → Callback（被动） |
| **适用场景** | 本地开发、多实例 | 云服务器 |
| **断线重连** | 自动重连（指数退避） | 需重新注册 |
| **认证方式** | WS register 消息 + auth_token | HTTP /gw/register + auth_token |

### WS 隧道注册流程

```
Callback                        Gateway                         飞书用户
  │                                │                                │
  │── WS 连接 ws://gateway:8080 ──▶│                                │
  │    /ws/tunnel?owner_id=ou_xxx  │                                │
  │                                │                                │
  │◀─ 101 Switching Protocols ────│                                │
  │                                │                                │
  │── WS: {"type":"register",  ───▶│ 检查绑定状态                   │
  │        "owner_id":"ou_xxx"}    │                                │
  │                                │                                │
  │                                │── 发送飞书授权卡片 ────────────▶│
  │                                │                                │
  │                                │                  用户点击"允许" ─│
  │                                │◀──────────────────────────────│
  │◀─ WS: {"type":"auth_ok",   ───│ 生成 auth_token                │
  │        "auth_token":"xxx"}     │                                │
  │                                │                                │
  │── WS: {"type":"auth_ok_ack"}──▶│ 更新绑定，注册完成              │
```

### HTTP 回调注册流程

```
┌─────────────────┐      注册请求      ┌─────────────────┐
│ Callback 服务   │───────────────────▶│  飞书网关       │
│ (启动时)        │  /gw/register       │                 │
└─────────────────┘                     └────────┬────────┘
                                                │
                                        验证 VERIFICATION_TOKEN
                                                │
                                        生成 auth_token
                                                │
┌─────────────────┘                     ┌────────▼────────┐
│                 │◀────────────────────│  返回 Token     │
│ 存储 auth_token │                     └─────────────────┘
└─────────────────┘
```

### 注册配置

| 变量 | 必需 | 说明 |
|------|:----:|------|
| `CALLBACK_SERVER_URL` | **是** | Callback 服务外部访问地址（WS 模式可使用 localhost） |
| `FEISHU_OWNER_ID` | **是** | 默认消息接收者 |
| `FEISHU_GATEWAY_URL` | **是** | 飞书网关地址（协议头决定连接模式） |

> **注意**：Callback 服务不需要 `FEISHU_VERIFICATION_TOKEN`，网关通过注册时生成的 `auth_token` 验证请求。

### 用户授权控制

新设备首次注册时需要用户确认：

1. 首次注册时，网关发送确认消息到 `FEISHU_OWNER_ID`
2. 用户在飞书中点击「确认授权」按钮
3. 网关记录授权状态，后续请求直接通过

### WS 隧道特性

**自动重连**：
- 断线后自动重连（指数退避：1s → 2s → 4s → 8s，最大 30s）
- 重连时携带本地存储的 `auth_token`，免卡片续期
- 连接被替换（新终端接入）时停止重连

**心跳保活**：
- 客户端每 30 秒发送 WebSocket ping
- 服务端 90 秒无活动判定断连
- 防止 NAT/防火墙因空闲断开连接

> 详细机制请参考 [飞书网关认证与注册机制](GATEWAY_AUTH.md)

---

## 群聊功能

### 功能说明

OpenAPI 模式支持将消息发送到飞书群聊，而非仅限私聊。

### 消息发送目标

消息发送目标的优先级：

```
chat_id 参数 > owner_id
```

- `chat_id 参数`：客户端调用时传入的群聊 ID
- `owner_id`：默认消息接收者

### 配置方式

通过环境变量配置默认群聊：

```bash
# .env 文件
FEISHU_CHAT_ID=oc_xxxxxxxxx  # 群聊 ID

# FEISHU_OWNER_ID 仍需配置（用于网关注册和认证）
FEISHU_OWNER_ID=ou_xxx  # user_id 格式
```

> **说明**：`FEISHU_CHAT_ID` 由客户端 Shell 脚本读取，调用网关时会作为 `chat_id` 参数传递。

### Session 与群聊映射

当用户在群聊中回复消息继续会话时，系统会自动记录 `session_id -> chat_id` 映射：

```
┌─────────────┐    回复消息     ┌─────────────┐
│ 群聊        │───────────────▶│  网关接收    │
│ (chat_id)   │                │  (session)  │
└─────────────┘                └──────┬──────┘
                                      │
                               保存映射关系
                               session_id → chat_id
                                      │
                               ┌──────▼──────┐
                               │  后续消息    │
                               │  发往该群聊  │
                               └─────────────┘
```

这样，后续该 session 的通知消息会自动发送到对应的群聊。

### 多实例配置

每个 Callback 实例可以配置不同的群聊：

| 实例 | FEISHU_OWNER_ID | FEISHU_CHAT_ID | 效果 |
|------|-----------------|----------------|------|
| 实例 A | `ou_user_a` | `oc_group_a` | 消息发往群聊 A |
| 实例 B | `ou_user_b` | `oc_group_b` | 消息发往群聊 B |

---

## 安全配置

### 必需安全措施

| 措施 | 优先级 | 说明 |
|------|:------:|------|
| **Verification Token** | 高 | HTTP 回调模式建议配置 `FEISHU_VERIFICATION_TOKEN`；longpoll 模式不需要（连接已通过 App ID/Secret 认证） |
| **HTTPS** | 高 | 生产环境使用 HTTPS |
| **Callback 白名单** | 高 | 分离部署时必须配置 |

### 推荐 .env 配置

```bash
# 单机部署（longpoll 模式 — 无需 VERIFICATION_TOKEN）
FEISHU_EVENT_MODE=auto  # 或 longpoll

# 单机部署（HTTP 回调模式 — 建议配置 VERIFICATION_TOKEN）
FEISHU_VERIFICATION_TOKEN=your_verification_token

# 分离部署 - Callback（不需要 VERIFICATION_TOKEN）
FEISHU_GATEWAY_URL=http://gateway-server:8080
```

### 安全检查清单

- [ ] HTTP 回调模式是否配置了 `FEISHU_VERIFICATION_TOKEN`
- [ ] 分离部署是否配置了 Callback 白名单
- [ ] `FEISHU_APP_SECRET` 是否妥善保管
- [ ] 生产环境是否使用 HTTPS
- [ ] 服务是否运行在内网/防火墙后

> 完整的安全分析请参考 [通信鉴权与安全分析](SECURITY_ANALYSIS.md)

---

## 常见问题

### Q: 事件订阅 URL 验证失败？

**A:** 此问题仅出现在 HTTP 回调模式下。如果不需要公网端点，可切换到 longpoll 模式（`FEISHU_EVENT_MODE=longpoll`）。

HTTP 回调模式下检查以下几点：
1. 服务是否已启动
2. URL 是否正确（`/feishu/event`）
3. `FEISHU_VERIFICATION_TOKEN` 是否正确
4. 网络是否能从公网访问

### Q: 点击按钮后没有反应？

**A:** 检查以下几点：
1. 飞书开放平台是否订阅了 `card.action.trigger` 事件
2. 服务日志中是否有错误信息
3. `FEISHU_VERIFICATION_TOKEN` 是否正确

### Q: 如何获取 FEISHU_OWNER_ID？

**A:** 有以下几种方式：
1. 在飞书开放平台「用户管理」中查看
2. 发送一条消息到你的飞书机器人，在日志中查看 `open_id`
3. 使用飞书 API `GET /contact/v3/users/:user_id`

### Q: 分离部署时 Callback 注册失败？

**A:** 根据连接模式检查：

**WS 隧道模式**：
1. `FEISHU_GATEWAY_URL` 是否使用 `ws://` 协议头
2. 网关服务是否正常运行
3. 网络是否可以连接到网关（WS 隧道无需公网 IP，但需能访问网关）
4. 检查网关日志中是否有 WS 连接记录

**HTTP 回调模式**：
1. `FEISHU_GATEWAY_URL` 是否使用 `http://` 协议头
2. `CALLBACK_SERVER_URL` 是否可以从网关访问（需要公网可达）
3. 网关服务是否正常运行
4. 检查网关日志中是否有注册请求记录

### Q: WS 隧道模式和 HTTP 回调模式如何选择？

**A:**
- **本地开发**：推荐 WS 隧道模式（`ws://`），本地电脑无需公网 IP
- **云服务器**：两种模式均可，WS 隧道模式连接更稳定
- **需要低延迟**：WS 隧道模式，长连接无握手开销

### Q: 如何配置多个 Claude 命令？

**A:** 在 `.env` 中配置：
```bash
CLAUDE_COMMAND=[claude, claude --setting opus]
```

然后在飞书中：
- `/new --cmd=0 --dir=/path prompt` — 使用第一个命令 `claude`
- `/new --cmd=1 --dir=/path prompt` — 使用第二个命令 `claude --setting opus`
- `/new --cmd=opus --dir=/path prompt` — 子串匹配，命中 `claude --setting opus`

> **说明**：`--cmd` 支持两种匹配方式：
> - **索引**：从 0 开始，`--cmd=0` 表示第一个命令
> - **子串**：大小写敏感，查找命令字符串中是否包含指定子串

### Q: longpoll 模式启动后没有收到事件？

**A:** 检查以下几点：
1. 是否安装了 `lark-oapi` SDK：`pip install lark-oapi`
2. Python 版本是否 >= 3.8（longpoll 模式要求）
3. 飞书应用事件订阅是否选择了「WebSocket 长连接」方式
4. 检查日志 `log/feishu_longpoll/*.log` 中是否有连接成功信息
5. `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 是否正确

### Q: 回复消息继续会话不生效？

**A:** 检查以下几点：
1. 飞书应用是否订阅了 `im.message.receive_v1` 事件
2. 消息是否是「回复」形式（不是新消息）
3. 原消息是否包含 `session_id`

---

## 相关文档

- [Webhook 模式部署文档](DEPLOYMENT_WEBHOOK.md) - Webhook 模式详细说明
- [模式选择指南](DEPLOYMENT_MODES.md) - 两种模式对比与选择
- [飞书消息回复继续会话方案](FEISHU_SESSION_CONTINUE.md) - 回复继续功能详细设计
- [飞书网关认证与注册机制](GATEWAY_AUTH.md) - 网关注册与双向认证详细说明
- [通信鉴权与安全分析](SECURITY_ANALYSIS.md) - 安全风险评估与加固建议
- [Socket 通信协议](../src/shared/protocol.md) - Unix Socket 通信规范
