## ADDED Requirements

### Requirement: 多应用 Longpoll 管理

飞书网关 MUST 支持为多个飞书应用维护独立的 longpoll 长连接，通过 AppManager 统一管理应用生命周期。

#### Scenario: 应用首次注册启动 longpoll

- **GIVEN** 应用管理员通过 Callback 携带 app_id + app_secret + owner_id 注册
- **WHEN** 网关验证 app_secret 有效（成功获取 tenant_access_token）
- **THEN** 网关持久化该应用凭据
- **AND** 为该 app_id 创建并启动 FeishuLongPollClient 实例
- **AND** longpoll 连接成功后开始接收该应用的飞书事件

#### Scenario: 同一应用不重复创建 longpoll

- **GIVEN** app_id=cli_aaa 已有活跃的 longpoll 连接
- **WHEN** 新 Callback 使用相同 app_id 注册
- **THEN** 网关复用已有的 longpoll 连接，不创建新实例

#### Scenario: 网关重启恢复所有 longpoll

- **GIVEN** 网关持久化了 N 个应用的凭据
- **WHEN** 网关重启
- **THEN** 从持久化数据加载所有应用凭据
- **AND** 为每个应用自动启动 longpoll 连接
- **AND** 无需等待 Callback 重连

#### Scenario: 应用凭据更新

- **GIVEN** app_id=cli_aaa 的 app_secret 已变更
- **WHEN** 应用管理员携带新的 app_secret 重新注册
- **THEN** 网关更新持久化的凭据
- **AND** 停止旧的 longpoll 连接
- **AND** 使用新凭据启动新的 longpoll 连接

#### Scenario: 凭据验证失败

- **GIVEN** Callback 携带 app_id + app_secret 注册
- **WHEN** 使用该凭据获取 tenant_access_token 失败
- **THEN** 拒绝注册
- **AND** 返回错误 `{"type": "auth_error", "message": "invalid app credentials"}`
- **AND** 不持久化该凭据

### Requirement: 两阶段注册协议

飞书网关 MUST 支持两阶段注册：先尝试轻量注册（app_id + owner_id），失败后可携带 app_secret 重试。

#### Scenario: 已注册应用的用户注册（轻量模式）

- **GIVEN** app_id=cli_aaa 的凭据已在网关注册
- **WHEN** Callback 携带 app_id + owner_id 发起注册（不含 app_secret）
- **THEN** 网关使用已存储的凭据对应的 API 客户端
- **AND** 向 owner_id 发送授权卡片
- **AND** 后续流程与现有注册一致（授权 → auth_token → 绑定）

#### Scenario: 未注册应用的首次注册被拒绝

- **GIVEN** app_id=cli_aaa 的凭据不在网关中
- **WHEN** Callback 携带 app_id + owner_id 发起注册（不含 app_secret）
- **THEN** 网关拒绝注册
- **AND** 返回错误 `{"type": "auth_error", "code": "app_not_registered", "message": "app not registered, app_secret required for first registration"}`

#### Scenario: 应用管理员首次注册（携带 app_secret）

- **GIVEN** app_id=cli_aaa 的凭据不在网关中
- **WHEN** Callback 携带 app_id + app_secret + owner_id 发起注册
- **AND** 凭据验证成功
- **THEN** 网关持久化应用凭据
- **AND** 启动该应用的 longpoll 连接
- **AND** 向 owner_id 发送授权卡片

#### Scenario: Callback 端自动重试逻辑

- **GIVEN** Callback 配置了 FEISHU_APP_ID + FEISHU_APP_SECRET + FEISHU_OWNER_ID
- **WHEN** 首次注册（app_id + owner_id）收到 `app_not_registered` 错误
- **AND** 本地配置了 app_secret
- **THEN** 自动携带 app_secret 重新发起注册

#### Scenario: 普通用户注册失败

- **GIVEN** Callback 只配置了 FEISHU_APP_ID + FEISHU_OWNER_ID（无 app_secret）
- **WHEN** 首次注册收到 `app_not_registered` 错误
- **THEN** 记录错误日志，提示用户联系应用管理员先完成注册
- **AND** 按退避策略重试（等待管理员注册后即可成功）

### Requirement: 多应用事件路由

飞书网关 MUST 将 longpoll 接收到的事件按 (app_id, user_id) 路由到对应的 Callback。

#### Scenario: 消息事件路由

- **GIVEN** app_id=cli_aaa 的 longpoll 收到 im.message.receive_v1 事件
- **AND** 事件中 sender_id=ou_xxx
- **WHEN** 网关查找 (cli_aaa, ou_xxx) 的绑定
- **THEN** 将事件转发给对应 Callback
- **AND** Callback 正常处理（发起 Claude 会话等）

#### Scenario: 卡片回调事件路由

- **GIVEN** app_id=cli_aaa 的 longpoll 收到 card.action.trigger 事件
- **AND** 事件中 operator.open_id=ou_xxx
- **WHEN** 网关查找 (cli_aaa, ou_xxx) 的绑定
- **THEN** 将事件转发给对应 Callback
- **AND** Callback 处理审批决策并返回 toast 响应

#### Scenario: 事件无匹配绑定

- **GIVEN** app_id=cli_aaa 的 longpoll 收到事件
- **AND** sender_id=ou_yyy 在该应用下无绑定
- **WHEN** 网关查找绑定失败
- **THEN** 记录 debug 日志，忽略该事件

#### Scenario: 网关自身应用事件路由不变

- **GIVEN** 网关配置了 FEISHU_APP_ID/SECRET（自身默认应用）
- **WHEN** 该应用的 longpoll 收到事件
- **THEN** 按现有逻辑路由（owner_id 单键查绑定）
- **AND** 不受多应用功能影响

### Requirement: 出站消息代理

飞书网关 MUST 支持 Callback 通过网关发送飞书消息，网关自动使用 Callback 绑定对应的应用 API 客户端。

#### Scenario: Callback 发送权限审批卡片

- **GIVEN** Callback（app_id=cli_aaa, owner_id=ou_xxx）需要发送权限审批卡片
- **WHEN** Callback 请求 `/gw/feishu/send`
- **THEN** 网关从 auth_token/owner_id 查到绑定中的 app_id=cli_aaa
- **AND** 使用 cli_aaa 的 FeishuAPI 实例发送卡片

#### Scenario: Callback 更新卡片（审批后置灰）

- **GIVEN** 用户点击审批按钮后需要更新卡片
- **WHEN** 网关处理卡片更新
- **THEN** 使用对应 app 的 API 客户端更新卡片

#### Scenario: 传统模式的出站消息不变

- **GIVEN** Callback 未携带 app_id 注册（传统模式）
- **WHEN** 发送飞书消息
- **THEN** 使用网关自身应用的 API 客户端（现有逻辑不变）

### Requirement: 多应用绑定存储

飞书网关 MUST 以 (app_id, owner_id) 复合键存储多应用模式的绑定关系，向后兼容现有单键存储。

#### Scenario: 创建多应用绑定

- **GIVEN** Callback 使用 app_id=cli_aaa、owner_id=ou_xxx 注册成功
- **WHEN** 存储绑定
- **THEN** 使用 `cli_aaa:ou_xxx` 作为键
- **AND** 绑定记录包含 app_id 字段

#### Scenario: 同一用户不同应用独立绑定

- **GIVEN** ou_xxx 已在 app_id=cli_aaa 下绑定
- **WHEN** ou_xxx 通过 app_id=cli_bbb 注册
- **THEN** 创建独立的绑定 `cli_bbb:ou_xxx`
- **AND** 两个绑定互不影响

#### Scenario: 传统模式绑定不变

- **GIVEN** Callback 未携带 app_id 注册
- **WHEN** 存储绑定
- **THEN** 使用 owner_id 作为键（现有逻辑不变）

#### Scenario: 应用凭据独立持久化

- **GIVEN** 网关已注册 N 个应用
- **WHEN** 持久化到 runtime/ 目录
- **THEN** 应用凭据与用户绑定分开存储
- **AND** app_secret 不在日志中输出

### Requirement: 默认中心网关地址与模式检测

当 `FEISHU_SEND_MODE=openapi` 时，Callback 端 MUST 根据 `FEISHU_GATEWAY_URL` 的值决定部署模式：未设置则默认连中心网关，`local` 表示 standalone，其他值表示自建网关。

#### Scenario: 普通用户开箱即用（无 GATEWAY_URL）

- **GIVEN** `FEISHU_SEND_MODE=openapi`
- **AND** 配置了 `FEISHU_APP_ID` + `FEISHU_OWNER_ID`
- **AND** 未配置 `FEISHU_GATEWAY_URL`
- **WHEN** Callback 启动
- **THEN** `FEISHU_GATEWAY_URL` 默认为中心网关地址
- **AND** 以多应用轻量模式连接中心网关（app_id + owner_id）

#### Scenario: 应用管理员首次接入（有 APP_SECRET，无 GATEWAY_URL）

- **GIVEN** `FEISHU_SEND_MODE=openapi`
- **AND** 配置了 `FEISHU_APP_ID` + `FEISHU_APP_SECRET` + `FEISHU_OWNER_ID`
- **AND** 未配置 `FEISHU_GATEWAY_URL`
- **WHEN** Callback 启动
- **THEN** `FEISHU_GATEWAY_URL` 默认为中心网关地址
- **AND** 以管理员模式连接中心网关（携带 app_secret 注册应用）

#### Scenario: Standalone 模式（GATEWAY_URL=local）

- **GIVEN** `FEISHU_SEND_MODE=openapi`
- **AND** 配置了 `FEISHU_APP_ID` + `FEISHU_APP_SECRET` + `FEISHU_OWNER_ID`
- **AND** `FEISHU_GATEWAY_URL=local`
- **WHEN** Callback 启动
- **THEN** 以 standalone 模式运行（网关 + callback 同进程）
- **AND** 不连接任何远程网关

#### Scenario: 自建网关用户显式指定地址

- **GIVEN** `FEISHU_SEND_MODE=openapi`
- **AND** `FEISHU_GATEWAY_URL=wss://my-gateway.com`
- **WHEN** Callback 启动
- **THEN** 连接到用户指定的网关地址
- **AND** 不使用中心网关默认地址

#### Scenario: Webhook 模式不受影响

- **GIVEN** `FEISHU_SEND_MODE=webhook`
- **WHEN** Callback 启动
- **THEN** 走 webhook 模式
- **AND** 忽略 `FEISHU_GATEWAY_URL` 配置

### Requirement: 多应用场景下的功能兼容

多应用注册模式 MUST 兼容现有所有 Callback 功能。

#### Scenario: 消息转发发起 Claude 会话

- **GIVEN** 用户通过飞书应用 A 发送消息
- **WHEN** 网关的 app A longpoll 收到事件并路由到 Callback
- **THEN** Callback 正常发起 Claude 会话

#### Scenario: 权限审批按钮点击

- **GIVEN** 用户在飞书中点击权限审批卡片的按钮
- **WHEN** 卡片回调事件通过对应 app 的 longpoll 到达网关
- **THEN** 网关路由到对应 Callback
- **AND** Callback 处理审批决策
- **AND** 网关使用对应 app 的 API 返回 toast 响应

#### Scenario: Stop/Notification 事件通知

- **GIVEN** Callback 需要发送 Stop 或 Notification 通知卡片
- **WHEN** 通过网关 `/gw/feishu/send` 发送
- **THEN** 网关使用对应 app 的 API 客户端发送到正确的用户

#### Scenario: 会话续发（reply 消息）

- **GIVEN** 用户在飞书中回复某条消息以继续会话
- **WHEN** 网关收到 reply 消息事件
- **THEN** 按 (app_id, sender_id) 路由到对应 Callback
- **AND** Callback 正常处理会话续发

#### Scenario: 网关自身应用功能完全不受影响

- **GIVEN** 网关配置了 FEISHU_APP_ID/SECRET 和通过传统方式注册的 Callback
- **WHEN** 网关自身应用收到事件
- **THEN** 按现有逻辑处理，不受多应用功能影响
