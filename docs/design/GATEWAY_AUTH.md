# 飞书网关认证与注册机制

## 概述

Callback 后端启动时自动向飞书网关注册，获取 `auth_token` 用于双向认证。

### 部署模式

| 模式 | FEISHU_SEND_MODE | FEISHU_GATEWAY_URL | 网关 |
|------|-----------------|-------------------|------|
| **单机模式** | `openapi` | 默认为 `CALLBACK_SERVER_URL` | 本地服务 |
| **分离模式** | `openapi` | 配置远端地址 | 远端网关 |
| **Webhook 模式** | `webhook`（默认） | 不适用 | 直接调用飞书 Webhook |

> **注意**：双向认证仅在 `FEISHU_SEND_MODE=openapi` 时启用。`webhook` 模式直接使用 `FEISHU_WEBHOOK_URL` 发送消息，无需认证。

## 设计目标

1. **自动注册** - Callback 后端启动时自动获取 auth_token
2. **Token 更新** - 每次重启更新 auth_token
3. **双向认证** - `/gw/feishu/send` 接口验证 X-Auth-Token
4. **用户授权** - 分离模式下新设备需用户确认

## 注册流程

```
┌─────────────────┐         注册请求          ┌─────────────────┐
│  Callback 后端  │ ──────────────────────────>│   飞书网关       │
│   (启动时)       │   {callback_url,           │                 │
│                 │    owner_id}               │                 │
└─────────────────┘                            └─────────────────┘
       │                                                │
       │    立即返回 200                                 │
       │<───────────────────────────────────────────────┘
       │
       │                              ┌─────────────────┐
       │                              │   查询映射表     │
       │                              └─────────────────┘
       │                                    │
       │        ┌───────────────────────────┼───────────────────────────────┐
       │        │                           │                               │
       │   未绑定                       已绑定                             已绑定
       │        │                           │                        callback_url 不同
       │        │                   callback_url 相同                          │
       │        │                           │                               │
       │        ▼                           ▼                               ▼
       │   ┌─────────────────┐      ┌─────────────────┐          ┌─────────────────┐
       │   │调用 check_owner │      │ 回调 callback   │          │ 发飞书卡片       │
       │   │    _id 验证     │      │ /cb/register    │          │ 显示新旧设备     │
       │   └─────────────────┘      │ 通知 auth_token  │          │ 让用户确认       │
       │        │                   └─────────────────┘          └─────────────────┘
       │        ▼                           │                              │
       │   ┌─────────────────┐              │                       用户点"允许/拒绝"
       │   │   发飞书卡片     │              │                              │
       │   │ 请求用户确认     │              │                              ▼
       │   └─────────────────┘              │                    ┌─────────────────┐
       │        │                           │                    │ 回调 callback   │
       │        ▼                           │                    │ /cb/register    │
       │ ┌─────────────────┐                │                    │ 通知 auth_token  │
       │ │用户点"允许/拒绝" │                │                    └─────────────────┘
       │ └─────────────────┘                │                              │
       │        │                           │                              │
       └────────┴───────────────────────────┴──────────────────────────────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │   创建/更新     │
                           │   绑定记录      │
                           └─────────────────┘
```

**处理逻辑说明：**

1. **未绑定**：先调用 `callback_url/cb/check-owner` 验证所属权，验证通过后发送授权卡片
2. **已绑定 + callback_url 相同**：直接更新 auth_token，无需用户确认
3. **已绑定 + callback_url 不同**：发送授权卡片，显示新旧设备对比，让用户选择是否更换
4. **用户拒绝**：如果存在 owner_id + callback_url 精确匹配的绑定则删除（用于解绑）

## 接口定义

### 1. Callback 后端 → 飞书网关：注册接口

**请求**

```
POST /gw/register
Content-Type: application/json

{
  "callback_url": "https://callback.example.com",
  "owner_id": "ou_xxx",
  "reply_in_thread": false,
  "claude_commands": ["claude", "claude --model opus"],
  "default_chat_dir": "/home/user/project"
}
```

**响应**

```json
{
  "status": "accepted",
  "message": "注册请求已接收，正在处理"
}
```

> 注：网关立即返回 200，后续处理（用户确认/回调）异步进行。

### 2. 飞书网关 → Callback 后端：验证 owner_id 所属

在发送授权卡片前，网关先验证 callback_url 是否属于该 owner_id。

**请求**

```
POST {callback_url}/cb/check-owner
Content-Type: application/json

{
  "owner_id": "ou_xxx"
}
```

**响应**

```json
{
  "success": true,
  "is_owner": true
}
```

### 3. 飞书网关 → Callback 后端：注册回调接口

**请求**

```
POST {callback_url}/cb/register
X-Auth-Token: {auth_token}
Content-Type: application/json

{
  "owner_id": "ou_xxx",
  "auth_token": "{timestamp}.{signature}",
  "gateway_version": "1.0.0"
}
```

**响应**

```json
{
  "status": "ok",
  "message": "注册成功"
}
```

### 4. Callback 后端 → 飞书网关：发送消息接口

Callback 后端通过此接口发送飞书消息（卡片或文本），需要携带有效的 auth_token。

**请求**

```
POST /gw/feishu/send
X-Auth-Token: {auth_token}
Content-Type: application/json

{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {
        "tag": "plain_text",
        "content": "消息标题"
      }
    },
    "elements": [...]
  },
  "receive_id": "ou_xxx",
  "receive_id_type": "open_id",
  "session_id": "optional-session-id",
  "project_dir": "/path/to/project",
  "callback_url": "https://callback.example.com"
}
```

**响应**

成功（200）：

```json
{
  "success": true,
  "message_id": "om_xxx"
}
```

失败（400）：

```json
{
  "success": false,
  "error": "错误描述"
}
```

认证失败（401）：

```json
{
  "success": false,
  "error": "Missing X-Auth-Token"
}
```

或

```json
{
  "success": false,
  "error": "Invalid X-Auth-Token"
}
```

**认证流程**

1. 前端脚本自动从 `runtime/auth_token.json` 读取存储的 auth_token
2. 将 auth_token 添加到 `X-Auth-Token` header
3. 后端验证 token 格式和签名（HMAC-SHA256）
4. 验证通过后处理消息发送逻辑

### 5. 飞书网关 → 飞书用户：授权卡片

#### 场景一：新设备注册

当收到新的注册请求且未绑定时，网关向用户发送确认卡片：

```json
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {
        "tag": "plain_text",
        "content": "新的 Callback 后端注册请求"
      },
      "template": "blue"
    },
    "elements": [
      {
        "tag": "div",
        "text": {
          "tag": "lark_md",
          "content": "**来源 IP**: `1.2.3.4`\n**Callback URL**: `https://callback.example.com`\n\n是否允许该后端接收你的飞书消息？"
        }
      },
      {
        "tag": "action",
        "actions": [
          {
            "tag": "button",
            "text": {
              "tag": "plain_text",
              "content": "允许"
            },
            "type": "primary",
            "value": {
              "action": "approve_register",
              "callback_url": "https://callback.example.com",
              "owner_id": "ou_xxx",
              "request_ip": "1.2.3.4",
              "old_callback_url": "",
              "reply_in_thread": false
            }
          },
          {
            "tag": "button",
            "text": {
              "tag": "plain_text",
              "content": "拒绝"
            },
            "value": {
              "action": "deny_register",
              "callback_url": "https://callback.example.com",
              "owner_id": "ou_xxx"
            }
          }
        ]
      }
    ]
  }
}
```

#### 场景二：更换设备

当用户已有绑定，但新请求来自不同的 callback_url 时：

```json
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {
        "tag": "plain_text",
        "content": "Callback 后端更换设备请求"
      },
      "template": "blue"
    },
    "elements": [
      {
        "tag": "div",
        "text": {
          "tag": "lark_md",
          "content": "**旧设备**: `https://old-callback.example.com`\n**新设备**: `https://new-callback.example.com`\n**来源 IP**: `1.2.3.4`\n\n是否允许更换到新设备？"
        }
      },
      {
        "tag": "action",
        "actions": [
          {
            "tag": "button",
            "text": {
              "tag": "plain_text",
              "content": "允许"
            },
            "type": "primary",
            "value": {
              "action": "approve_register",
              "callback_url": "https://new-callback.example.com",
              "owner_id": "ou_xxx",
              "request_ip": "1.2.3.4",
              "old_callback_url": "https://old-callback.example.com",
              "reply_in_thread": false
            }
          },
          {
            "tag": "button",
            "text": {
              "tag": "plain_text",
              "content": "拒绝"
            },
            "value": {
              "action": "deny_register",
              "callback_url": "https://new-callback.example.com",
              "owner_id": "ou_xxx"
            }
          }
        ]
      }
    ]
  }
}
```

## auth_token 生成与验证

### 格式

```
auth_token = base64url(timestamp) + "." + base64url(signature)
```

### 签名计算

```python
import hmac
import hashlib
import base64
import time

def generate_auth_token(feishu_verification_token: str, owner_id: str) -> str:
    timestamp = str(int(time.time()))
    message = owner_id + timestamp
    signature = hmac.new(
        feishu_verification_token.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    return (
        base64.urlsafe_b64encode(timestamp.encode()).decode().rstrip('='),
        base64.urlsafe_b64encode(signature).decode().rstrip('=')
    )

# 生成
timestamp_b64, signature_b64 = generate_auth_token(VERIFICATION_TOKEN, owner_id)
auth_token = f"{timestamp_b64}.{signature_b64}"
```

### 网关验证

```python
def verify_auth_token(auth_token: str, owner_id: str) -> bool:
    try:
        parts = auth_token.split('.')
        if len(parts) != 2:
            return False

        timestamp_b64, signature_b64 = parts
        timestamp = base64.urlsafe_b64decode(timestamp_b64 + '==').decode()
        received_signature = base64.urlsafe_b64decode(signature_b64 + '==')

        # 重新计算签名
        message = owner_id + timestamp
        expected_signature = hmac.new(
            FEISHU_VERIFICATION_TOKEN.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()

        # 恒定时间比较
        return hmac.compare_digest(received_signature, expected_signature)
    except:
        return False
```

## 映射表结构

```
owner_id   | callback_url              | auth_token        | reply_in_thread | claude_commands       | default_chat_dir      | default_chat_session_id | updated_at        | registered_ip
-----------|---------------------------|-------------------|-----------------|-----------------------|-----------------------|-------------------------|-------------------|---------------
ou_xxx     | https://callback.example  | abc123.def456     | false           | ["claude", "opus"]    | /home/user/project    | session-uuid-123       | 2025-02-05 10:30:00| 1.2.3.4
```

- 一个用户（owner_id）同时只能有一个活跃的 callback_url 绑定
- 绑定由 `owner_id + callback_url` 共同确定
- 已绑定 + 相同 callback_url：直接更新 token，无需用户确认
- 已绑定 + 不同 callback_url：发送卡片让用户选择是否更换设备
- 用户拒绝且存在精确匹配的绑定：删除绑定（可用于解绑）

### 话题回复模式 (reply_in_thread)

- `reply_in_thread`：控制飞书通知是否回复到话题详情
- **特殊规则**：当工作目录 `project_dir` 等于用户的 `default_chat_dir` 时，强制 `reply_in_thread=False`（直接回复到群聊主界面）
- 其他情况使用 `reply_in_thread` 配置值

### 默认聊天目录 (default_chat_dir)

- 用户配置的默认工作目录，配置后可直接发消息创建/继续会话
- 由 Callback 后端注册时传递（从 `DEFAULT_CHAT_DIR` 环境变量读取）
- 分离部署场景下不同用户可有不同的默认目录

## 安全保证

| 威胁 | 防护机制 |
|------|----------|
| 第三方伪造 auth_token | 需要 FEISHU_VERIFICATION_TOKEN 才能生成有效签名 |
| callback 后端伪造其他用户的 token | 签名包含 owner_id，只能生成自己的 token |
| 重放攻击 | 虽然不设过期，但每次重启会更新 token，旧的自动失效 |
| 未授权注册 | 新注册需要用户在飞书中确认 |
| 恶意注册请求 | 发送授权卡片前先调用 `/cb/check-owner` 验证 callback_url 所属 |

## 后续请求认证

### 飞书网关 → Callback 后端

网关转发消息时在请求头中携带 auth_token：

```
POST {callback_url}/cb/agent/continue
X-Auth-Token: {auth_token}
Content-Type: application/json

{...}
```

### Callback 后端 → 飞书网关（/gw/feishu/send）

Callback 后端调用 `/gw/feishu/send` 发送消息时，**前端脚本自动**从 `runtime/auth_token.json` 读取 auth_token 并添加到请求头：

```
POST {gateway_url}/gw/feishu/send
X-Auth-Token: {auth_token}
Content-Type: application/json

{
  "msg_type": "interactive",
  "card": {...}
}
```

#### 前端自动读取机制

```bash
# feishu.sh 中的实现
_get_auth_token() {
    local auth_token_file="${PROJECT_ROOT}/runtime/auth_token.json"
    # 读取 auth_token 字段
    json_get "$(cat "$auth_token_file")" "auth_token"
}

# 调用 /gw/feishu/send 时自动附加 token
_do_curl_post "$url" "$body" "$log_prefix" "$auth_token"
```

### Callback 后端验证（必需）

`/gw/feishu/send` 接口**强制要求**验证 `X-Auth-Token` header：

| 验证项 | 说明 |
|--------|------|
| Header 存在性 | `X-Auth-Token` header 必须存在 |
| Token 格式 | 必须符合 `{timestamp_b64}.{signature_b64}` 格式 |
| 签名有效性 | 使用 `FEISHU_VERIFICATION_TOKEN` 验证 HMAC-SHA256 签名 |
| Owner 匹配 | 签名中的 owner_id 必须与 `FEISHU_OWNER_ID` 一致 |

验证失败时返回 401 Unauthorized：

```json
{
  "success": false,
  "error": "Invalid X-Auth-Token"
}
```

## 配置

### 单机模式（OpenAPI）

```bash
# 发送模式（必需，启用双向认证）
FEISHU_SEND_MODE=openapi

# 回调服务对外可访问的地址
CALLBACK_SERVER_URL=http://localhost:8080

# 飞书用户 ID（必需）
FEISHU_OWNER_ID=ou_xxx

# 飞书验证 Token（必需，用于生成和验证 auth_token 签名）
FEISHU_VERIFICATION_TOKEN=your_secret_token

# 飞书应用凭证（OpenAPI 模式必需）
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

# 飞书网关地址（可选，单机模式默认使用 CALLBACK_SERVER_URL）
# FEISHU_GATEWAY_URL=http://localhost:8080
```

### 分离模式（OpenAPI）

**Callback 后端：**

```bash
# 发送模式（必需）
FEISHU_SEND_MODE=openapi

# 回调服务对外可访问的地址
CALLBACK_SERVER_URL=https://callback.example.com

# 飞书用户 ID（必需）
FEISHU_OWNER_ID=ou_xxx

# 飞书网关地址（必需）
FEISHU_GATEWAY_URL=https://gateway.example.com

# 注意：Callback 后端不需要 FEISHU_VERIFICATION_TOKEN
# 网关请求通过注册时下发的 auth_token 验证
```

**飞书网关：**

```bash
# 飞书验证 Token（必需，用于验证飞书事件和生成 auth_token）
FEISHU_VERIFICATION_TOKEN=your_secret_token

# 飞书应用凭证（必需）
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

### Webhook 模式（无需认证）

```bash
# 发送模式（默认）
FEISHU_SEND_MODE=webhook

# 飞书 Webhook URL（必需）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

> **说明**：Webhook 模式直接调用飞书 Webhook URL 发送消息，不经过 `/gw/feishu/send` 接口，无需配置认证相关参数。
