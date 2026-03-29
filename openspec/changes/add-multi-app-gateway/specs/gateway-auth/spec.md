## MODIFIED Requirements

### Requirement: 飞书网关注册接口

飞书网关 MUST 提供 `POST /register` 接口，接收 Callback 后端的注册请求。支持多应用模式的两阶段注册。

#### Scenario: 接收注册请求

- **GIVEN** 飞书网关正在运行
- **WHEN** 收到 POST `/register` 请求
- **AND** 请求 body 包含 `callback_url` 和 `owner_id`
- **THEN** 立即返回 `200` 状态码
- **AND** 返回 `{"status": "accepted", "message": "注册请求已接收，正在处理"}`

#### Scenario: 请求参数缺失

- **GIVEN** 飞书网关正在运行
- **WHEN** 收到 POST `/register` 请求
- **AND** 请求 body 缺少 `callback_url` 或 `owner_id`
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "missing required fields: callback_url, owner_id"}`

#### Scenario: 多应用注册 - 已注册应用的轻量注册

- **GIVEN** app_id=cli_aaa 的凭据已在网关注册
- **WHEN** 收到 POST `/register` 请求，包含 `app_id` 和 `owner_id`（不含 `app_secret`）
- **THEN** 使用已存储凭据的 API 客户端处理注册
- **AND** 后续流程与传统注册一致

#### Scenario: 多应用注册 - 未注册应用被拒绝

- **GIVEN** app_id=cli_aaa 的凭据不在网关中
- **WHEN** 收到 POST `/register` 请求，包含 `app_id` 和 `owner_id`（不含 `app_secret`）
- **THEN** 返回 `403` 状态码
- **AND** 返回 `{"error": "app_not_registered", "message": "app not registered, app_secret required for first registration"}`

#### Scenario: 多应用注册 - 应用管理员首次注册

- **GIVEN** app_id=cli_aaa 的凭据不在网关中
- **WHEN** 收到 POST `/register` 请求，包含 `app_id`、`app_secret`、`owner_id`
- **AND** 凭据验证成功
- **THEN** 持久化应用凭据，启动 longpoll
- **AND** 使用该应用 API 向 owner_id 发送授权卡片

#### Scenario: 异步处理注册

- **GIVEN** 飞书网关返回注册成功响应
- **WHEN** 后续查询映射表
- **THEN** 根据绑定状态决定：
  - 已绑定：直接调用 Callback 后端的 `/cb/register` 接口
  - 未绑定：向用户发送飞书授权卡片

### Requirement: WebSocket 隧道连接与认证

飞书网关 MUST 在现有 HTTP 端口上提供 WebSocket Upgrade 端点（`/ws/tunnel`），通过 register 消息统一认证。网关通过比对客户端携带的 auth_token 区分三种场景：
1. **续期模式**：有绑定 + token 匹配（同一终端重连），直接刷新 token
2. **换绑模式**：有绑定 + token 不匹配/缺失（新终端），需要飞书卡片授权
3. **首次注册模式**：无绑定，需要飞书卡片授权

#### Scenario: WS 握手建立

- **GIVEN** 飞书网关正在运行
- **WHEN** Callback 发送 HTTP GET 请求到 `/ws/tunnel?owner_id=ou_xxx`
- **AND** 请求包含 `Upgrade: websocket` 头
- **THEN** 网关返回 `101 Switching Protocols`
- **AND** 等待客户端发送 register 消息（30s 超时）

#### Scenario: 续期模式 - 同一终端重连（token 匹配）

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** BindingStore 中已有该绑定记录（多应用模式按 app_id + owner_id 查找）
- **WHEN** 客户端发送 register 消息（含 auth_token 与绑定记录匹配）
- **THEN** 网关生成新 auth_token，暂存到 pending_auth_tokens
- **AND** 通过 WS 发送 `{"type":"auth_ok","auth_token":"new_xxx"}`
- **AND** 等待客户端发送 `{"type":"auth_ok_ack"}`
- **AND** 收到 auth_ok_ack 后更新 BindingStore 并注册到 WebSocketRegistry

#### Scenario: 换绑模式 - 新终端（token 不匹配）

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** BindingStore 中已有该绑定记录
- **WHEN** 客户端发送 register 消息（auth_token 与绑定记录不匹配或缺失）
- **THEN** 连接进入 pending 状态
- **AND** 网关向 owner_id 发送飞书换绑授权卡片（使用对应 app 的 API 客户端）

#### Scenario: 首次注册 - 无绑定进入 pending 状态

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** BindingStore 中无该绑定记录
- **WHEN** 客户端发送 register 消息
- **THEN** 连接进入 pending 状态（不注册到 WebSocketRegistry）
- **AND** 网关向 owner_id 发送飞书授权卡片（使用对应 app 的 API 客户端）

#### Scenario: 多应用 WS 注册 - 未注册应用被拒绝

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** register 消息包含 app_id 但该应用未注册
- **AND** register 消息不含 app_secret
- **WHEN** 网关处理 register 消息
- **THEN** 返回 `{"type":"auth_error","code":"app_not_registered","message":"app not registered, app_secret required for first registration"}`
- **AND** 不关闭连接（等待客户端可能的重试）

#### Scenario: 多应用 WS 注册 - 管理员首次注册

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** register 消息包含 app_id + app_secret
- **AND** 该应用未注册
- **WHEN** 网关验证凭据成功
- **THEN** 持久化应用凭据，启动 longpoll
- **AND** 进入正常注册流程（发送授权卡片）

#### Scenario: 授权通过（首次注册和换绑共用）

- **GIVEN** 有一个 pending 状态的 WS 连接（owner_id=ou_xxx）
- **WHEN** 飞书用户在授权卡片上点击"允许"
- **THEN** 网关生成 auth_token（HMAC-SHA256），暂存到 pending_auth_tokens 和 pending_binding_params
- **AND** 通过 WS 发送 `{"type":"auth_ok","auth_token":"xxx"}` 给 Callback
- **AND** 等待客户端发送 `{"type":"auth_ok_ack"}`
- **AND** 收到 auth_ok_ack 后更新 BindingStore，将连接从 pending 升级为已认证

#### Scenario: 授权拒绝

- **GIVEN** 有一个 pending 状态的 WS 连接（owner_id=ou_xxx）
- **WHEN** 飞书用户在授权卡片上点击"拒绝"
- **THEN** 通过 WS 发送 `{"type":"auth_error","message":"authorization denied"}`
- **AND** 直接关闭 socket
- **AND** 从 pending 中移除

#### Scenario: 授权超时

- **GIVEN** 有一个 pending 状态的 WS 连接
- **WHEN** 超过 10 分钟用户未操作授权卡片
- **THEN** 定期清理任务发送 `{"type":"auth_error","action":"stop","message":"authorization timeout"}`
- **AND** 关闭 WS 连接

#### Scenario: 连接替换

- **GIVEN** 某绑定已在 WebSocketRegistry 注册（连接 A）
- **WHEN** 同一绑定的新连接 B 认证成功
- **THEN** 向连接 A 发送 `{"type":"replaced","action":"stop"}` 消息
- **AND** 关闭连接 A
- **AND** 注册连接 B 到 WebSocketRegistry

#### Scenario: 用户解绑

- **GIVEN** 绑定在 BindingStore 中有 WS 模式的记录
- **WHEN** 用户在飞书端执行解绑操作
- **THEN** 删除 BindingStore 中的绑定记录
- **AND** 向该绑定的 WS 连接发送 `{"type":"unbind","action":"stop","message":"user unbind"}`
- **AND** 客户端收到后清除本地 auth_token 并停止重连

#### Scenario: 连接断开时清理

- **GIVEN** 绑定已在 WebSocketRegistry 注册
- **WHEN** WebSocket 连接断开（网络中断、客户端关闭、read timeout 超过 90s）
- **THEN** 从 WebSocketRegistry 移除该绑定的映射

#### Scenario: pending 连接限制

- **GIVEN** 同一绑定已有 5 个 pending 连接
- **WHEN** 第 6 个连接尝试进入 pending 状态
- **THEN** 拒绝新连接，返回 `{"type":"auth_error","message":"too many pending connections"}`

#### Scenario: 授权卡片冷却

- **GIVEN** 同一绑定在 60 秒内已触发过授权卡片发送
- **WHEN** 新的 pending 连接尝试触发卡片发送
- **THEN** 拒绝新连接，返回 `{"type":"auth_error","message":"too many requests"}`

### Requirement: WebSocket 消息协议

飞书网关和 Callback 后端 MUST 使用约定的 JSON 消息格式通信。

#### Scenario: 注册消息格式 - 多应用模式（Callback → Gateway）

- **GIVEN** WS 握手完成后
- **WHEN** 客户端发送多应用模式注册消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "register",
    "owner_id": "ou_xxx",
    "app_id": "cli_aaa",
    "app_secret": "secret_xxx",
    "auth_token": "xxx",
    "reply_in_thread": false,
    "claude_commands": ["opus"],
    "default_chat_dir": "~/work"
  }
  ```
- **AND** `app_id` 为多应用模式必填字段
- **AND** `app_secret` 仅在应用首次注册时需要，后续可省略
- **AND** `auth_token` 为可选字段（已有 token 时携带）

#### Scenario: 注册消息格式 - 传统模式（Callback → Gateway）

- **GIVEN** WS 握手完成后
- **WHEN** 客户端发送传统模式注册消息（不携带 app_id）
- **THEN** 消息格式为：
  ```json
  {
    "type": "register",
    "owner_id": "ou_xxx",
    "auth_token": "xxx",
    "reply_in_thread": false,
    "claude_commands": ["opus"],
    "default_chat_dir": "~/work"
  }
  ```
- **AND** 使用网关自身应用处理（现有逻辑不变）

#### Scenario: 注册成功消息格式

- **GIVEN** 注册授权通过
- **WHEN** 网关发送注册成功通知
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_ok",
    "auth_token": "timestamp_b64.signature_b64"
  }
  ```

#### Scenario: 应用未注册错误消息格式

- **GIVEN** Callback 携带 app_id 但该应用未在网关注册
- **AND** 未携带 app_secret
- **WHEN** 网关拒绝注册
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_error",
    "code": "app_not_registered",
    "message": "app not registered, app_secret required for first registration"
  }
  ```
- **AND** 不关闭 WS 连接（允许客户端携带 app_secret 重试）

#### Scenario: 客户端确认消息格式

- **GIVEN** 客户端收到 auth_ok 并存储 auth_token
- **WHEN** 客户端发送确认
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_ok_ack"
  }
  ```
- **AND** 网关收到后才更新 BindingStore 并注册连接

#### Scenario: 注册失败消息格式

- **GIVEN** 授权被拒绝或超时
- **WHEN** 网关发送注册失败通知
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_error",
    "message": "authorization denied|authorization timeout|connection inactive"
  }
  ```
- **AND** 部分错误（如 authorization timeout、connection inactive）带有 `action: "stop"` 字段

#### Scenario: 连接被替换消息格式

- **GIVEN** 新连接认证成功，替换旧连接
- **WHEN** 网关通知旧连接被替换
- **THEN** 消息格式为：
  ```json
  {
    "type": "replaced",
    "action": "stop"
  }
  ```

#### Scenario: 用户解绑消息格式

- **GIVEN** 用户在飞书端执行解绑操作
- **WHEN** 网关通知客户端解绑
- **THEN** 消息格式为：
  ```json
  {
    "type": "unbind",
    "action": "stop",
    "message": "user unbind"
  }
  ```

#### Scenario: 请求消息格式

- **GIVEN** 网关需要通过 WS 转发请求到 Callback
- **WHEN** 构造请求消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "request",
    "id": "uuid-for-matching",
    "method": "POST",
    "path": "/cb/decision",
    "headers": {},
    "body": {}
  }
  ```

#### Scenario: 响应消息格式

- **GIVEN** Callback 收到 WS 请求并处理完成
- **WHEN** 构造响应消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "response",
    "id": "uuid-for-matching",
    "status": 200,
    "headers": {},
    "body": {}
  }
  ```
- **AND** `id` 与请求消息的 `id` 一致

#### Scenario: 错误消息格式

- **GIVEN** 请求处理过程中发生错误（超时、handler 异常等）
- **WHEN** 构造错误消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "error",
    "id": "uuid-for-matching",
    "code": "timeout|handler_error|invalid_request",
    "message": "human-readable error description"
  }
  ```

#### Scenario: 心跳保活

- **GIVEN** WebSocket 连接已建立（已认证状态）
- **WHEN** 连接空闲超过心跳间隔（默认 30s）
- **THEN** Callback 客户端发送 WebSocket ping frame
- **AND** 网关响应 pong frame
- **AND** 如果网关连续 90s 未收到任何消息，断开连接
