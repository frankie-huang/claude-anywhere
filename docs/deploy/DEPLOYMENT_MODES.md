# 部署模式选择指南

本文档对比 Claude Code 飞书通知项目支持的两种部署模式，帮助您选择合适的部署方式。

## 目录

- [快速安装](#快速安装)
- [模式对比](#模式对比)
- [决策流程](#决策流程)
- [Webhook 模式](#webhook-模式)
- [OpenAPI 模式](#openapi-模式)

---

## 快速安装

### OpenAPI 模式一键安装

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
./setup.sh start      # 启动服务
./setup.sh stop       # 停止服务
./setup.sh restart    # 重启服务
./setup.sh status     # 查看状态
./setup.sh update     # 更新代码并重启
```

---

## 模式对比

| 特性 | Webhook 模式 | OpenAPI 模式 |
|------|:------------:|:------------:|
| **飞书应用** | 无需注册 | 需要创建应用 |
| **交互方式** | 按钮跳转浏览器 | 飞书内直接响应 |
| **配置复杂度** | 简单（2分钟） | 较复杂（15分钟） |
| **多实例部署** | ❌ 不支持 | ✅ 支持 |
| **回复继续会话** | ❌ 不支持 | ✅ 支持 |
| **群聊绑定** | ❌ 不支持 | ✅ 支持 |
| **自动注册与认证** | ❌ 不支持 | ✅ 支持 |
| **本地开发支持** | ❌ 需内网穿透 | ✅ WS 隧道免穿透 |
| **无需公网端点** | ❌ | ✅ longpoll 模式 |
| **VSCode 激活** | ✅ 支持 | ✅ 支持 |

---

## 决策流程

```
                        开始
                          │
                          ▼
              ┌───────────────────────┐
              │ 需要多机器/实例部署？ │
              └───────────┬───────────┘
                    │           │
                   是           否
                    │           │
                    ▼           ▼
         ┌─────────────┐   ┌───────────────────────┐
         │ OpenAPI     │   │ 需要飞书内直接响应？ │
         │ 分离部署    │   └───────────┬───────────┘
         └─────────────┘         │           │
                                是           否
                                 │           │
                                 ▼           ▼
                      ┌─────────────┐   ┌─────────────┐
                      │ OpenAPI     │   │ Webhook     │
                      │ 单机部署    │   │ 快速开始    │
                      └─────────────┘   └─────────────┘
```

---

## Webhook 模式

### 简介

使用飞书群机器人的 Webhook URL 发送消息，配置最简单。

### 适合场景

- ✅ 个人使用
- ✅ 快速体验功能
- ✅ 无需飞书应用权限
- ✅ 单机器部署

### 优点

- 配置简单，2 分钟完成
- 无需飞书应用审核
- 无需配置事件订阅

### 缺点

- 需要打开浏览器操作
- 不支持多实例部署
- 不支持回复继续会话

### 最小配置

```bash
FEISHU_SEND_MODE=webhook
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

### 详细文档

👉 [Webhook 模式部署文档](DEPLOYMENT_WEBHOOK.md)

---

## OpenAPI 模式

### 简介

使用飞书开放平台 API 发送消息，支持高级功能。

### 适合场景

- ✅ 团队协作使用
- ✅ 需要飞书内直接响应
- ✅ 多机器/多实例部署
- ✅ 需要回复继续会话功能

### 部署方式

| 方式 | 说明 | 适用场景 |
|------|------|----------|
| **单机部署** | 所有服务在一台机器 | 团队协作、单实例 |
| **分离部署** | 网关与 Callback 分离 | 多实例、本地开发 |

### 事件接收模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **longpoll** | WebSocket 长连接接收飞书事件（推荐） | 无需公网端点，本地开发 |
| http | 传统 HTTP 回调接收飞书事件 | 已有公网端点 |
| auto | 自动检测（默认） | 有 lark-oapi 则 longpoll，否则 http |

### 分离部署连接模式

| 模式 | 协议 | 说明 | 适用场景 |
|------|------|------|----------|
| **WS 隧道** | `ws://` / `wss://` | Callback 主动连接网关（推荐） | 本地开发、无需公网 IP |
| HTTP 回调 | `http://` / `https://` | 网关回调 Callback | 云服务器、公网可达 |

### 优点

- 飞书内直接操作，无需跳转
- 支持分离部署，多实例共享应用
- 支持消息回复继续会话
- 用户体验更好
- WS 隧道模式支持本地开发（无需公网 IP）
- longpoll 模式无需公网端点接收飞书事件

### 缺点

- 配置复杂，需要飞书应用
- 需要配置事件订阅和权限
- longpoll 模式需要安装 `lark-oapi` SDK（Python >= 3.8）

### 最小配置（单机）

```bash
FEISHU_SEND_MODE=openapi
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OWNER_ID=ou_xxx  # 必需
# FEISHU_EVENT_MODE=auto  # 默认 auto，longpoll 模式无需公网端点
# FEISHU_VERIFICATION_TOKEN=xxx  # HTTP 回调模式建议配置
```

### 最小配置（分离部署 - WS 隧道）

**网关服务**：
```bash
FEISHU_SEND_MODE=openapi
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OWNER_ID=ou_admin
```

**Callback 服务（本地电脑）**：
```bash
FEISHU_SEND_MODE=openapi
FEISHU_GATEWAY_URL=ws://gateway-server:8080  # ws:// 启用 WS 隧道
FEISHU_OWNER_ID=ou_user_a
```

### 详细文档

👉 [OpenAPI 模式部署文档](DEPLOYMENT_OPENAPI.md)

---

## 快速对比

| 问题 | Webhook | OpenAPI |
|------|:-------:|:-------:|
| 只是想快速试试？ | ✅ | |
| 只有一台机器？ | ✅ | ✅ |
| 需要飞书内点按钮？ | | ✅ |
| 有多台机器要部署？ | | ✅ |
| 想在飞书里回复继续对话？ | | ✅ |
| 本地开发（无公网 IP）？ | | ✅ (WS 隧道) |
| 无需公网端点接收事件？ | | ✅ (longpoll) |

---

## 相关文档

### 部署文档
- [Webhook 模式部署文档](DEPLOYMENT_WEBHOOK.md) - Webhook 模式详细说明
- [OpenAPI 模式部署文档](DEPLOYMENT_OPENAPI.md) - OpenAPI 模式详细说明

### 安全文档
- [通信鉴权与安全分析](SECURITY_ANALYSIS.md) - 安全风险评估与加固建议
- [飞书网关认证与注册机制](GATEWAY_AUTH.md) - 网关注册与双向认证

### 功能文档
- [飞书消息回复继续会话方案](FEISHU_SESSION_CONTINUE.md) - 回复继续功能详细设计
- [Socket 通信协议](../src/shared/protocol.md) - Unix Socket 通信规范
- [飞书卡片模板说明](../src/templates/feishu/README.md) - 卡片模板使用指南
