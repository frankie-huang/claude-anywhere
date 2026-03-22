# QuickStart - 5 分钟快速上手

本指南帮助你以最快速度跑通 claude-anywhere，让 Claude Code 的权限请求和任务通知发送到飞书。

---

## 选择部署模式

| 模式 | 适合场景 | 需要配置 |
|------|---------|---------|
| **Webhook** | 个人使用，只需接收通知和审批权限 | 飞书 Webhook URL |
| **OpenAPI 单机** | 个人使用，需要在飞书中回复继续会话 | 飞书应用 App ID/Secret |
| **OpenAPI 分离部署** | 团队使用，多实例共享一个网关 | Gateway URL |

---

## OpenAPI 一键安装

```bash
# 单机模式
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --app-id=cli_xxx --app-secret=xxx --owner-id=<用户ID>

# 分离模式（连接远程网关）
curl -fsSL https://raw.githubusercontent.com/frankie-huang/claude-anywhere/main/setup.sh | \
  bash -s -- --gateway-url=ws://gateway:8080 --owner-id=<用户ID>
```

> 详细说明和服务管理命令请参考 [OpenAPI 模式部署文档](docs/deploy/DEPLOYMENT_OPENAPI.md) 的「一键安装」章节

---

## Webhook 模式（手动安装）

最简单的部署方式，只需一个飞书群机器人 Webhook 地址。

### 前提条件

- **Python 3.6+** 和 **curl** 已安装
- **Claude Code** 已安装并可正常使用
- 一个**飞书自定义机器人 Webhook URL**

> 如果还没有 Webhook URL，请在飞书群聊中添加自定义机器人，复制 Webhook 地址即可。

### 安装步骤

**1. 克隆项目**

```bash
cd ~/.claude
git clone <repo-url> hooks
cd hooks
```

**2. 运行安装脚本**

```bash
./install.sh
```

安装脚本会自动完成：
- 检测环境依赖（不会自动安装，缺失时提示命令）
- 将 Hook 注册到 `~/.claude/settings.json`
- 生成 `.env` 配置文件（从 .env.example 复制）

> 如果安装时选择跳过 hooks 配置，可手动将以下内容合并到 `~/.claude/settings.json`：
>
> ```json
> {
>   "hooks": {
>     "PermissionRequest": [
>       {
>         "matcher": "",
>         "hooks": [{ "type": "command", "command": "/path/to/hooks/src/hook-router.sh", "timeout": 660 }]
>       }
>     ],
>     "Stop": [
>       {
>         "hooks": [{ "type": "command", "command": "/path/to/hooks/src/hook-router.sh" }]
>       }
>     ]
>   }
> }
> ```
>
> 将 `/path/to/hooks` 替换为实际安装路径。

**3. 配置环境变量**

编辑 `.env`（安装脚本已自动从 .env.example 复制），只需填写 `FEISHU_WEBHOOK_URL`：

```bash
FEISHU_SEND_MODE=webhook
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的webhook地址
```

> 其他配置保持默认即可运行。

**注意**：所有模式都需要配置 `CALLBACK_SERVER_URL`，确保飞书/浏览器能够访问到。本机使用默认的 `http://localhost:8080` 即可，远程服务器或内网穿透场景需填公网地址。

**4. 启动回调服务**

```bash
./src/start-server.sh
```

看到 `[INFO] Starting callback server on port 8080...` 即为成功。

> 回调服务用于接收飞书按钮点击事件。如果不启动，权限通知仍会发送，但按钮点击将无法响应。

常用命令：

```bash
./src/start-server.sh start    # 启动服务
./src/start-server.sh stop     # 停止服务
./src/start-server.sh restart  # 重启服务
./src/start-server.sh status   # 查看状态
```

**5. 开始使用**

打开新终端运行 `claude`，当需要执行敏感操作时，飞书会收到交互卡片：

| 按钮 | 效果 |
|------|------|
| **批准运行** | 允许本次操作 |
| **始终允许** | 允许并记住规则，下次自动放行 |
| **拒绝运行** | 拒绝本次操作，Claude 可重试 |
| **拒绝并中断** | 拒绝并终止当前任务 |

### 验证安装

```bash
./install.sh --check                                              # 检查环境
cat ~/.claude/settings.json | python3 -m json.tool | grep hook-router  # 确认 Hook 注册
curl -s --noproxy '*' -H "X-Auth-Token: $(python3 -c 'import json;print(json.load(open("runtime/auth_token.json"))["auth_token"])')" http://127.0.0.1:8080/status | python3 -m json.tool  # 确认服务运行
```

### 常见问题

**飞书没收到消息？**

1. 检查 `.env` 中 `FEISHU_WEBHOOK_URL` 是否正确
2. 确认网络：`curl -s https://open.feishu.cn`
3. 查看日志：`cat log/hook_$(date +%Y%m%d).log`

**按钮点击没反应？**

1. 确认回调服务已启动
2. 检查端口：`lsof -i :8080`
3. Webhook 模式按钮会在浏览器打开决策页面，确保浏览器能访问 `CALLBACK_SERVER_URL`

---

## OpenAPI 模式（手动安装，进阶）

适合需要飞书内直接响应、回复继续会话、多实例部署的场景。

### 前置准备

**1. 创建飞书应用**

前往 [飞书开放平台](https://open.feishu.cn/app)，创建「企业自建应用」。

**2. 配置事件与回调**

在「事件与回调」中，**事件配置**和**回调配置**都需要填写订阅方式，请求地址填写 `CALLBACK_SERVER_URL` 的值：

- **事件配置**：添加「接收消息」（`im.message.receive_v1`）事件，并确认以下权限：
  - 读取用户发给机器人的单聊消息（添加事件后默认开通）
  - 接收群聊中 @机器人消息事件（**需手动点击开通权限**，否则无法接收群聊中 at 机器人的消息）
- **回调配置**：添加「卡片回传交互」（`card.action.trigger`）回调

> 中间如果提示「尚未开启机器人功能」，点击开启即可。

**3. 配置权限**

在「权限管理」中开通以下权限：

| 权限名称 | 权限代码 |
|----------|----------|
| 以应用的身份发消息 | `im:message:send_as_bot` |
| 获取用户 user ID | `contact:user.employee_id:readonly` |

> 权限可访问的数据范围：与应用的可用范围一致即可。

**4. 创建版本并发布**

完成权限配置后，前往「版本管理与发布」，填写版本号和更新说明，发布应用。

> 以上权限默认免审批。如果要开放给多人使用，需在「可用范围」中添加，会触发审批流程。

**5. 获取凭证**

- `App ID` 和 `App Secret`：在「凭证与基础信息」中获取
- `Verification Token`：在「事件与回调」→「加密策略」中获取

**6. 获取 OWNER_ID**

`FEISHU_OWNER_ID` 是消息接收者的飞书 user_id。获取方式参考 [如何获取 User ID](https://open.feishu.cn/document/faq/trouble-shooting/how-to-obtain-user-id)，也可以通过飞书开放平台的 [获取单个用户信息](https://open.feishu.cn/document/server-docs/contact-v3/user/get) 接口在线调试获取。

### 配置

编辑 `.env`：

```bash
FEISHU_SEND_MODE=openapi

# 飞书应用凭证
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxx
FEISHU_VERIFICATION_TOKEN=your_verification_token

# 消息接收者
FEISHU_OWNER_ID=12345678

# 回调服务（需要飞书能访问到此地址）
CALLBACK_SERVER_URL=http://your-server:8080
```

### 启动

```bash
./src/start-server.sh
```

> **首次启动注意**：机器人会发送一条注册消息，需要在飞书中点击绑定后才能正常工作。

> 更多部署模式和高级配置，请参阅 [部署模式架构文档](docs/deploy/DEPLOYMENT_MODES.md)。

---

## 可选：VSCode 自动激活

点击飞书按钮后自动激活 VSCode 窗口，无需手动切换。

### 本地开发

在 `.env` 中添加：

```bash
ACTIVATE_VSCODE_ON_CALLBACK=true
```

点击飞书按钮后，服务端会执行 `code .` 激活当前目录的 VSCode 窗口。

### SSH 远程开发

通过反向 SSH 隧道，在远程服务器触发请求后自动唤起本地 VSCode。

**1. 本地电脑启动代理**

```bash
python3 src/proxy/vscode_ssh_proxy.py --vps myserver
```

参数说明：
- `--vps`：SSH 地址（别名如 `myserver`，或 `root@1.2.3.4`）
- `--port`：本地 HTTP 服务端口（默认 9527）
- `--remote-port`：远程服务器端口（默认等于 `--port`，一般无需指定）

**2. 远程服务器配置 `.env`**

```bash
ACTIVATE_VSCODE_ON_CALLBACK=true
VSCODE_SSH_PROXY_PORT=9527    # 需与本地代理端口一致
```

**工作流程**：点击飞书按钮 → 远程服务器通过 SSH 隧道请求本地代理 → 本地代理调用 `code --folder-uri` → 本地 VSCode 自动打开远程项目。

---

## 配置速查

| 场景 | 模式 | 最小配置 |
|------|------|----------|
| 个人使用，快速体验 | Webhook | `FEISHU_WEBHOOK_URL` |
| 飞书内响应，回复继续会话 | OpenAPI | App ID + Secret + OWNER_ID |
| 多人/多实例部署 | OpenAPI 分离 | 上述 + `FEISHU_GATEWAY_URL` |

## 更多文档

- [部署模式架构](docs/deploy/DEPLOYMENT_MODES.md) - Webhook vs OpenAPI 详细对比
- [OpenAPI 部署](docs/deploy/DEPLOYMENT_OPENAPI.md) - OpenAPI 模式完整指南
- [会话继续功能](docs/design/FEISHU_SESSION_CONTINUE.md) - 飞书回复继续 Claude 会话
- [安全分析](docs/design/SECURITY_ANALYSIS.md) - 安全性说明
- [用户使用指南](docs/reference/USER_GUIDE.md) - 飞书端使用说明（指令、卡片交互、错误处理）
