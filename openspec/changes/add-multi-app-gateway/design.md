## Context

当前飞书网关在分离部署模式下仅支持单个飞书应用（通过 FEISHU_APP_ID/SECRET 环境变量配置）。每个飞书应用需要独立部署网关，增加了运维成本。

目标是实现中心化网关：一个网关服务支持 N 个飞书应用，每个应用下多个用户。用户只需运行轻量 Callback 端即可接入。

### 相关组件

- 飞书网关（Gateway）：中心化部署，管理多个飞书应用的 longpoll 连接
- Callback 后端：轻量端，只处理 Claude hooks 业务逻辑
- 飞书开放平台：每个应用独立的事件推送和 API
- FeishuLongPollClient：基于 lark-oapi SDK 的长连接客户端

## Goals / Non-Goals

**Goals:**
- 一个网关 = N 个飞书应用 = N 套 longpoll 连接
- 应用管理员首次注册携带 app_secret，后续其他用户只需 app_id + owner_id
- 网关全权处理飞书协议（longpoll 接收、SDK 解密、API 发送）
- Callback 端极轻量，只需 owner_id + app_id（+ 首次需 app_secret）
- 兼容现有所有功能和部署模式

**Non-Goals:**
- 不做应用配置的 Web 管理界面（通过 API/注册协议管理）
- 不做跨应用的消息聚合或转发
- 不修改 Callback 端的核心业务逻辑（权限审批、Claude 会话等）

## Decisions

### Decision 1: Per-app Longpoll（网关全权解密）

**选择**: 网关为每个注册的飞书应用创建独立的 FeishuLongPollClient 实例

**原因**:
- SDK longpoll 自动解密事件，网关直接获得明文，可正常路由和处理
- Callback 不需要持有 Encrypt Key，极大简化用户端部署
- 复用现有的 FeishuLongPollClient 和事件处理逻辑，改动最小
- 每应用最多 50 个 longpoll 连接（飞书限制），单连接足够

**替代方案**:
- ~~Per-app HTTP 事件端点 + 加密透传~~: Callback 需要持有 Encrypt Key 自行解密，与自建网关无本质区别，价值不足

### Decision 2: 两阶段注册协议

**选择**: Callback 注册分两阶段，网关区分"应用已注册"和"应用未注册"

**流程**:
1. Callback 启动，先携带 app_id + owner_id 发起注册
2. 网关检查该 app_id 是否已有凭据：
   - **已有**: 正常走授权流程（发卡片 / token 续期）
   - **未有**: 拒绝，返回 `app_not_registered` 错误
3. Callback 收到拒绝后，检查本地是否配置了 app_secret：
   - **有**: 携带 app_id + app_secret + owner_id 重新注册（应用管理员首次注册）
   - **无**: 注册失败，提示用户联系应用管理员先完成注册

**原因**:
- 普通用户不需要也不应该知道 app_secret
- app_secret 只在首次注册时传输一次，之后永久存储在网关
- 应用管理员和普通用户使用相同的 Callback 程序，只是配置不同

### Decision 3: 利用现有纯网关模式

**选择**: 中心网关复用现有的"纯网关"模式检测逻辑，不引入新的配置变量

**现有逻辑**: `FEISHU_SEND_MODE=openapi` + 有 `FEISHU_APP_ID`/`SECRET` + 无 `FEISHU_OWNER_ID` → 纯网关模式

**扩展**: 在纯网关模式基础上，额外支持动态应用注册。网关自身配置的应用作为"默认应用"，与动态注册的应用并存。如果网关不配置 APP_ID/SECRET（只做中心网关），也应支持——所有应用都通过动态注册接入。

**原因**:
- 不增加配置复杂度，现有部署无需修改任何配置
- 纯网关模式已经是"无 owner_id"的状态，天然适合扩展

### Decision 3.5: 默认中心网关地址与模式检测

**选择**: `FEISHU_SEND_MODE=openapi` 时，`FEISHU_GATEWAY_URL` 默认指向中心网关。通过特殊值 `local` 声明 standalone 模式。

**模式检测规则**（仅 `FEISHU_SEND_MODE=openapi` 时适用）:

| FEISHU_GATEWAY_URL | 行为 |
|---------------------|------|
| 未设置 | **默认连中心网关**（不管有没有 APP_SECRET） |
| 自定义地址（`ws://`/`wss://`/`http://`） | **连指定网关** |
| `local` | **standalone 本地模式**（网关 + callback 同进程） |

**Breaking Change**: 现有 standalone 用户（`SEND_MODE=openapi` + 完整凭据，无 GATEWAY_URL）升级后需要新增一行 `FEISHU_GATEWAY_URL=local`，否则会默认连中心网关。

**原因**:
- 逻辑统一无歧义：不再需要根据凭据完整度猜测用户意图
- 新用户开箱即用：只需配 `FEISHU_APP_ID` + `FEISHU_OWNER_ID` 即可接入中心网关
- 应用管理员同样开箱即用：配 `FEISHU_APP_ID` + `FEISHU_APP_SECRET` + `FEISHU_OWNER_ID` 即可首次注册应用到中心网关
- standalone 是少数进阶场景，显式声明更合理

### Decision 4: 绑定存储 (app_id, owner_id) 复合键

**选择**: 多应用模式下绑定以 (app_id, owner_id) 为键

**存储结构**:
```json
{
  "apps": {
    "cli_aaa": {
      "app_secret": "encrypted_or_plain",
      "registered_at": "2026-03-28T10:00:00Z"
    }
  },
  "bindings": {
    "ou_xxx": { ... },
    "cli_aaa:ou_xxx": { "app_id": "cli_aaa", ... },
    "cli_bbb:ou_yyy": { "app_id": "cli_bbb", ... }
  }
}
```

**原因**:
- 传统模式绑定（无 app_id）继续用 owner_id 单键
- 多应用绑定用 `{app_id}:{owner_id}` 格式键，自然隔离
- 同一用户可在不同应用下有不同的 Callback

### Decision 5: 出站消息路由

**选择**: 网关从 Callback 的绑定信息中获取 app_id，使用对应应用的 API 客户端发送消息

**流程**: Callback 请求 `/gw/feishu/send` → 网关通过 auth_token 识别 owner_id → 查绑定获取 app_id → 用该 app 的 FeishuAPI 实例发送

**原因**:
- Callback 不需要在每个请求中显式传 app_id
- 绑定关系已明确 owner_id 属于哪个 app
- 与现有 `/gw/feishu/send` 接口兼容，Callback 端无需修改

### Decision 6: App Longpoll 生命周期

**选择**:
- 启动：应用管理员首次注册时，网关验证凭据后立即启动 longpoll
- 持久化：网关持久化 app 凭据，重启后自动恢复所有 app 的 longpoll 连接
- 停止：仅在应用被显式删除时停止（不因所有用户断开而停止）

**原因**:
- longpoll 连接成本极低（一个线程 + 一个 WebSocket）
- 用户断开可能是临时的，保持 longpoll 可以接收离线期间的事件
- 网关重启后无需等待 Callback 重连即可恢复事件接收

## Risks / Trade-offs

### Risk 1: app_secret 传输安全
- **风险**: 首次注册时 app_secret 通过 WS 隧道传输
- **缓解**: 建议使用 wss://（TLS 加密），与 app_secret 传给飞书 API 的安全级别一致

### Risk 2: 多 longpoll 连接资源消耗
- **风险**: N 个应用 = N 个后台线程 + N 个 WebSocket 连接
- **缓解**: 每个连接资源极小；飞书限制每应用 50 连接，实际只需 1 个；百级应用完全可承受

### Risk 3: app_secret 持久化安全
- **风险**: 网关需要持久化存储多个 app_secret
- **缓解**: 与现有 auth_token 存储方式一致（runtime/ 目录）；不在日志输出；文件权限控制

### Risk 4: 应用凭据变更
- **风险**: 飞书应用重新生成 app_secret 后，网关存储的旧凭据失效
- **缓解**: 应用管理员重新注册（携带新 app_secret），网关更新存储并重建 longpoll 连接

## Open Questions

1. 是否需要提供应用注销/删除 API？还是只通过飞书指令（如 `/remove-app cli_xxx`）管理？
2. 网关是否需要在应用注册成功后主动通知管理员（如发送飞书消息确认应用已接入）？
