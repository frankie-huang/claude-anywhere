# Webhook 模式部署文档

本文档详细说明 Webhook 模式的架构设计和部署步骤。

## 目录

- [模式简介](#模式简介)
- [架构概览](#架构概览)
- [数据流向](#数据流向)
- [组件列表](#组件列表)
- [配置说明](#配置说明)
- [部署步骤](#部署步骤)
- [安全配置](#安全配置)
- [限制说明](#限制说明)
- [常见问题](#常见问题)

---

## 模式简介

Webhook 模式使用飞书群机器人的 Webhook URL 发送消息，是最简单的部署方式。

### 特点

| 特性 | 说明 |
|------|------|
| **飞书应用** | 无需注册，使用机器人 Webhook |
| **交互方式** | 按钮跳转浏览器页面 |
| **配置复杂度** | 简单（仅需 Webhook URL） |
| **部署时间** | 约 2 分钟 |
| **推荐场景** | 个人使用、快速体验 |

### 适用场景

- ✅ 个人使用
- ✅ 快速体验功能
- ✅ 无需飞书应用权限
- ✅ 单机器部署
- ❌ 不支持多实例部署
- ❌ 不支持回复继续会话

---

## 架构概览

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│  Claude Code    │────────▶│ src/hook-router  │────────▶│  飞书 Webhook   │
│                 │  Hook   │  → permission.sh │  Card   │     URL         │
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
                                     │ HTTP Callback
                                     │ (浏览器访问)
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

### 核心组件

1. **Hook 路由** (`src/hook-router.sh`) - Claude Code Hook 统一入口
2. **权限处理** (`src/hooks/permission.sh`) - 构建并发送飞书卡片
3. **回调服务** (`src/server/main.py`) - HTTP 服务器处理浏览器回调
4. **Socket 服务** (`src/server/main.py`) - Unix Socket 服务器

---

## 数据流向

### 发送通知流程

```
1. Claude Code 触发权限请求
       ↓
2. hook-router.sh 接收 Hook 事件
       ↓
3. permission.sh 构建飞书卡片
       ↓
4. 通过 Unix Socket 注册请求到回调服务
       ↓
5. 调用飞书 Webhook URL 发送卡片
       ↓
6. 飞书群聊显示交互卡片
```

### 用户操作流程

```
1. 用户点击飞书卡片按钮
       ↓
2. 浏览器打开 Callback URL
       ↓
3. 回调服务处理请求
       ↓
4. 通过 Unix Socket 返回决策
       ↓
5. permission.sh 接收决策
       ↓
6. Claude Code 继续或拒绝执行
```

---

## 组件列表

| 组件 | 路径 | 说明 |
|------|------|------|
| Hook 路由 | `src/hook-router.sh` | Claude Code Hook 统一入口 |
| 权限处理 | `src/hooks/permission.sh` | 构建并发送飞书卡片 |
| 卡片模板 | `src/templates/feishu/buttons.json` | Webhook 模式卡片模板 |
| 回调服务 | `src/server/main.py` | ThreadedHTTPServer，处理浏览器回调请求 |
| Socket 服务器 | `src/server/main.py` | Unix Socket 服务器，接收 Hook 连接 |
| 请求管理器 | `src/server/services/request_manager.py` | 管理等待中的权限请求 |

---

## 配置说明

### 环境变量

```bash
# .env 文件
FEISHU_SEND_MODE=webhook

# 必需配置
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 可选配置
CALLBACK_SERVER_URL=http://localhost:8080
CALLBACK_SERVER_PORT=8080
PERMISSION_SOCKET_PATH=/tmp/claude-permission.sock
```

### 配置清单

| 变量 | 必需 | 默认值 | 说明 |
|------|:----:|:------:|------|
| `FEISHU_SEND_MODE` | 否 | `webhook` | 发送模式 |
| `FEISHU_WEBHOOK_URL` | **是** | - | 飞书机器人 Webhook URL |
| `CALLBACK_SERVER_URL` | 否 | `http://localhost:8080` | 回调服务外部访问地址 |
| `CALLBACK_SERVER_PORT` | 否 | `8080` | 回调服务监听端口 |
| `PERMISSION_SOCKET_PATH` | 否 | `/tmp/claude-permission.sock` | Unix Socket 路径 |
| `PERMISSION_NOTIFY_DELAY` | 否 | `60` | 权限通知延迟（秒） |
| `PERMISSION_REQUEST_TIMEOUT` | 否 | `600` | 请求超时时间（秒） |
| `CALLBACK_PAGE_CLOSE_DELAY` | 否 | `3` | 决策页面自动关闭延迟（秒） |
| `FEISHU_AT_USER` | 否 | 空 | 通知 @ 配置：空/`all`/`off` |

---

## 部署步骤

### 1. 获取飞书机器人 Webhook URL

1. 打开飞书群聊
2. 点击群设置 → 群机器人 → 添加机器人
3. 选择「自定义机器人」
4. 设置机器人名称（如「Claude 权限通知」）
5. 复制 Webhook URL

> **提示**：Webhook URL 格式为 `https://open.feishu.cn/open-apis/bot/v2/hook/xxx`

### 2. 运行安装脚本

```bash
./install.sh
```

安装脚本会：
- 检测环境依赖（python3, curl 等）
- 配置 Claude Code hook
- 生成环境变量配置模板

### 3. 配置环境变量

```bash
# 复制模板
cp .env.example .env

# 编辑配置
vim .env
```

**最小配置示例**：

```bash
FEISHU_SEND_MODE=webhook
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-url
CALLBACK_SERVER_URL=http://your-server:8080  # 内网穿透时填公网地址
```

### 4. 启动回调服务

```bash
./src/start-server.sh
```

### 5. 启动 Claude Code

```bash
claude
```

### 验证部署

1. 在 Claude Code 中执行一个需要权限的操作
2. 检查飞书群聊是否收到通知卡片
3. 点击卡片按钮，确认能正常响应

---

## 安全配置

### 推荐安全措施

| 措施 | 优先级 | 说明 |
|------|:------:|------|
| **内网部署** | 高 | 服务仅监听 127.0.0.1 |
| **防火墙** | 高 | 限制端口仅本地访问 |
| **速率限制** | 中 | 公网部署时建议添加 Nginx rate limiting |

### 推荐 .env 配置

```bash
# 内网部署推荐配置
CALLBACK_SERVER_URL=http://127.0.0.1:8080
```

### 安全检查清单

- [ ] 服务是否运行在内网/防火墙后
- [ ] CALLBACK_SERVER_URL 是否使用内网地址
- [ ] Socket 文件权限是否为 0666
- [ ] 公网部署时是否添加了速率限制

> **注意**：Webhook 模式的权限决策接口无鉴权，内网部署时风险可忽略。公网部署建议配合速率限制。

---

## 限制说明

| 限制项 | 说明 | 影响 |
|--------|------|------|
| **交互跳转** | 需要打开浏览器，无法在飞书内直接响应 | 用户体验稍差 |
| **多实例** | 不支持，每个实例需要独立的 Webhook | 无法多机器共享 |
| **回复继续** | 不支持消息回复继续会话 | 无法在飞书内继续对话 |

### 升级到 OpenAPI 模式

如需以下功能，请升级到 OpenAPI 模式：
- 飞书内直接响应（无需浏览器）
- 多实例部署
- 消息回复继续会话
- 群聊绑定功能

详见 [OpenAPI 模式部署文档](DEPLOYMENT_OPENAPI.md)

---

## 常见问题

### Q: 点击按钮后没有反应？

**A:** 检查以下几点：
1. 回调服务是否正常运行
2. `CALLBACK_SERVER_URL` 是否正确配置（公网可访问）
3. 浏览器控制台是否有错误信息

### Q: 飞书没有收到通知？

**A:** 检查以下几点：
1. `FEISHU_WEBHOOK_URL` 是否正确
2. 网络是否能访问飞书服务器
3. 查看 `log/hook/*.log` 日志文件

### Q: 如何配置 VSCode 自动跳转？

**A:** 在 `.env` 中添加：
```bash
# 方式一：浏览器跳转
VSCODE_URI_PREFIX=vscode://file

# 方式二：自动激活（推荐）
ACTIVATE_VSCODE_ON_CALLBACK=true
```

### Q: 支持多个飞书群聊吗？

**A:** Webhook 模式不支持多群聊。如需此功能，请使用 [OpenAPI 模式](DEPLOYMENT_OPENAPI.md)。

---

## 相关文档

- [OpenAPI 模式部署文档](DEPLOYMENT_OPENAPI.md) - OpenAPI 模式详细说明
- [模式选择指南](DEPLOYMENT_MODES.md) - 两种模式对比与选择
- [Socket 通信协议](../src/shared/protocol.md) - Unix Socket 通信规范
- [飞书卡片模板说明](../src/templates/feishu/README.md) - 卡片模板使用指南
- [通信鉴权与安全分析](SECURITY_ANALYSIS.md) - 安全风险评估与加固建议
