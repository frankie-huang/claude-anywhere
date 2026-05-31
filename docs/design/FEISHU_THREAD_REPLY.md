# 飞书话题流链式回复设计

## 概述

本设计实现同一 Agent 会话的所有飞书消息收敛到一个话题流中，采用链式回复结构。

### 核心概念

- **last_message_id**：会话中最近一条系统消息的 ID，用于后续消息的回复目标
- **链式回复**：每条新消息回复到上一条消息，形成链式话题结构
- **MessageSessionStore**：message_id → session 映射，用于用户回复时找到对应会话
- **话题内回复**：回复消息收进话题详情，不刷群聊主界面（通过 binding 级别配置）

### 配置项

| 配置项 | 归属端 | 默认值 | 说明 |
|--------|--------|--------|------|
| `FEISHU_REPLY_IN_THREAD` | Callback 后端 → 飞书网关 | `false` | 回复消息是否收进话题详情 |

配置流程：
1. Callback 后端在 `.env` 中配置 `FEISHU_REPLY_IN_THREAD=true`
2. 注册时，Callback 后端将此配置发送给飞书网关
3. 飞书网关存储到 binding 中（每个用户/binding 独立配置）
4. 发送消息时，从 binding 中读取配置决定是否使用话题内回复

效果：
- `false`：回复消息正常显示在群聊主界面
- `true`：回复消息仅出现在话题详情中，不会冒泡到群聊主界面

**注意**：此配置仅在 `FEISHU_SEND_MODE=openapi` 时生效，Webhook 模式不支持回复 API。

### 存储架构

| 存储服务 | 维护方 | 访问方式 |
|---------|--------|---------|
| `SessionChatStore` (last_message_id) | Callback 后端 | HTTP 接口间接访问 |
| `MessageSessionStore` | 飞书网关 | 直接访问 |

### 关键接口

| 接口 | 端 | 用途 |
|-----|---|------|
| `/cb/session/get-last-message-id` | Callback 后端 | Shell 脚本查询 last_message_id |
| `/cb/session/set-last-message-id` | Callback 后端 | 飞书网关跨网络写入 last_message_id |

---

## 消息发送链路

### Shell 脚本链路（主要）

```
permission.sh/stop.sh
    ↓ 查询 /cb/session/get-last-message-id
    ↓ 传递 reply_to_message_id
/gw/feishu/send
    ↓ 发送成功
    ↓ 调用 /cb/session/set-last-message-id
更新 SessionChatStore
```

### 飞书网关链路

```
飞书事件 (/new, 用户回复)
    ↓
_send_session_result_notification
    ↓ 发送成功
    ↓ _set_last_message_id_to_callback
更新 SessionChatStore (通过 HTTP)
```

---

## 场景详解

### 场景 1: 飞书 /new 新建会话（长时间执行）

用户通过飞书发起新会话，会话执行时间较长（>2秒），涉及权限请求。

```
用户: /new --dir=/path 请帮我重构这个模块
  │
  ├─► 系统: 🆕 Agent 会话已创建                    [回复 /new 消息]
  │         📁 项目: /path
  │         🔑 Session: `abc12345...`
  │         ↓ last_message_id = 系统消息1
  │
  ├─► 系统: 🔐 权限请求: Bash                       [回复 last_message_id，链式]
  │         允许执行 npm install?
  │         [允许] [拒绝]
  │         ↓ last_message_id = 权限消息
  │
  ├─► (用户点击允许)
  │
  └─► 系统: ✅ Agent 已完成: 重构完成...           [回复 last_message_id，链式]
          ↓ last_message_id = 完成消息
```

**消息流：**
1. 飞书网关收到 `/new` 指令，调用 callback 后端创建会话
2. callback 后端返回 `status='processing'`
3. 飞书网关发送"会话已创建"，回复 `/new` 消息，更新 `last_message_id`
4. 会话执行中触发权限请求，`permission.sh` 查询 `last_message_id`
5. `permission.sh` 发送权限卡片，回复 `last_message_id`，更新 `last_message_id`
6. 会话完成，`stop.sh` 查询 `last_message_id`
7. `stop.sh` 发送完成通知，回复 `last_message_id`

---

### 场景 2: 飞书 /new 新建会话（快速完成）

用户通过飞书发起新会话，会话在 2 秒内快速完成，无需权限请求。

```
用户: /new --dir=/path 列出当前目录
  │
  ├─► 系统: ✅ Agent 已完成: src/, lib/, ...      [回复 /new 消息，文本通知]
  │         ↓ last_message_id = 文本通知
  │
  └─► 系统: ┌─────────────────────────────┐       [回复 last_message_id，卡片通知]
          │ 任务已完成                    │
          │ 📁 项目: path                 │
          │ 📋 响应内容: ...              │
          └─────────────────────────────┘
          ↓ last_message_id = 卡片通知
```

**消息流：**
1. 飞书网关收到 `/new` 指令，调用 callback 后端创建会话
2. callback 后端同步执行（2秒内完成），返回 `status='completed'`
3. 飞书网关发送**文本通知**"已完成"，回复 `/new` 消息，更新 `last_message_id`
4. 会话结束，触发 `stop.sh`
5. `stop.sh` 查询 `last_message_id`，发送**卡片通知**，回复 `last_message_id`，更新 `last_message_id`

**说明：** 会话完成时会有两条通知——飞书网关发送的简单文本通知和 `stop.sh` 发送的详细卡片通知。两者形成链式回复。

---

### 场景 3: 终端直接启动会话

用户在终端直接运行 Agent CLI（非通过飞书），后续有权限请求和完成通知。

```
终端: $ claude
  │
  ├─► 系统: 🔐 权限请求: Bash                       [无回复目标，发送新消息]
  │         允许执行 npm install?
  │         [允许] [拒绝]
  │         ↓ last_message_id = 权限消息（自动创建 session 记录）
  │
  └─► 系统: ✅ Agent 已完成: ...                   [回复 last_message_id，链式]
          ↓ last_message_id = 完成消息
```

**消息流：**
1. 终端启动 Claude，SessionChatStore 中无该 session 记录
2. 触发权限请求，`permission.sh` 查询 `last_message_id`（为空）
3. `permission.sh` 发送权限卡片（无回复目标），调用 `/cb/session/set-last-message-id`
4. `/cb/session/set-last-message-id` **自动创建** session 记录，设置 `last_message_id`
5. 会话完成，`stop.sh` 查询 `last_message_id`（已存在）
6. `stop.sh` 发送完成通知，回复 `last_message_id`

---

### 场景 4: 继续会话（用户回复）

用户在话题流中回复系统消息，继续会话。

```
用户: /new --dir=/path 请帮我重构
  │
  ├─► 系统: 🆕 Agent 会话已创建                    [回复 /new 消息]
  │         ↓ last_message_id = 系统消息1
  │
  └─► 系统: ✅ Agent 已完成: 重构完成              [回复 last_message_id，链式]
          ↓ last_message_id = 完成消息

用户: 帮我也重构一下测试文件                       [回复 完成消息]
  │
  ├─► 系统: ⏳ Agent 正在处理您的问题...           [回复用户消息]
  │         ↓ last_message_id = 处理消息
  │
  └─► 系统: ✅ Agent 已完成: 测试已重构            [回复 last_message_id，链式]
          ↓ last_message_id = 完成消息2
```

**消息流：**
1. 用户回复"完成消息"，飞书网关通过 MessageSessionStore 找到 session
2. 飞书网关调用 callback 后端继续会话
3. callback 后端返回 `status='processing'`
4. 飞书网关发送"正在处理"，**回复用户消息**（非链式，合理的设计）
5. 会话完成，`stop.sh` 查询 `last_message_id`（"正在处理"消息）
6. `stop.sh` 发送完成通知，回复 `last_message_id`（链式）

**设计说明：** 飞书网关内部发送的通知回复用户消息而非 `last_message_id`，这是合理的设计——用户能立即看到对自己输入的响应。后续 Shell 脚本发送的通知继续使用链式回复。

---

### 场景 5: 错误通知

会话执行过程中发生错误。

```
用户: /new --dir=/path 请帮我重构
  │
  ├─► 系统: 🆕 Agent 会话已创建                    [回复 /new 消息]
  │         ↓ last_message_id = 系统消息1
  │
  └─► 系统: ⚠️ Agent 执行异常: 命令超时             [回复用户消息]
          ↓ 错误通知保存到 MessageSessionStore
          ↓ last_message_id 不更新（保持为系统消息1）
```

**消息流：**
1. 飞书网关发送"会话已创建"，更新 `last_message_id`
2. 会话执行超时，`claude.py` 发送错误通知
3. 错误通知回复用户消息（如果有），**不更新 `last_message_id`**

**设计说明：** 错误通知不同步 `last_message_id`，后续正常通知继续回复到上一条正常消息。

---

### 场景 6: 多个权限请求

会话执行过程中触发多个权限请求。

```
用户: /new --dir=/path 请帮我部署
  │
  ├─► 系统: 🆕 Agent 会话已创建                    [回复 /new 消息]
  │         ↓ last_message_id = 系统消息1
  │
  ├─► 系统: 🔐 权限请求: Bash #1                    [回复 last_message_id]
  │         ↓ last_message_id = 权限消息1
  │
  ├─► 系统: 🔐 权限请求: Bash #2                    [回复 last_message_id]
  │         ↓ last_message_id = 权限消息2
  │
  └─► 系统: ✅ Agent 已完成: 部署完成              [回复 last_message_id]
          ↓ last_message_id = 完成消息
```

**消息流：**
1. 每个权限请求发送时，查询当前的 `last_message_id`
2. 权限卡片回复 `last_message_id`，发送成功后更新 `last_message_id`
3. 下一个权限请求回复更新后的 `last_message_id`
4. 形成链式结构

---

### 场景 7: Webhook 模式

系统配置为 Webhook 模式，不使用 OpenAPI。

```
用户: /new --dir=/path 请帮我重构
  │
  ├─► 系统: 🆕 Agent 会话已创建                    [发送新消息，无回复]
  │
  └─► 系统: ✅ Agent 已完成: 重构完成              [发送新消息，无回复]
```

**说明：** Webhook 模式不支持 reply API，所有消息作为新消息发送，不形成话题流。

---

## 异常处理

### 消息被撤回

如果用户撤回了链中的某条消息，reply API 会失败（错误码 230011）。此时降级为 send 发送新消息，消息能正常送达但会脱离话题流。

**注意：** 飞书"删除"（仅自己不可见）不影响 reply API，只有"撤回"（所有人不可见）才会导致 reply 失败。

### 终端启动会话

终端直接启动的会话，SessionChatStore 中无记录。首次发送通知时，`/cb/session/set-last-message-id` 会自动创建 session 记录。
