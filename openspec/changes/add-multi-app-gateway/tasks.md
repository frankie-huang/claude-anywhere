## 1. 多应用管理基础设施

- [ ] 1.1 新增 `services/app_manager.py`（AppManager）：应用凭据存储与管理
  - 持久化到 `runtime/apps.json`（app_id → app_secret, registered_at）
  - 提供 register_app / get_app / has_app / list_apps 接口
  - app_secret 不在日志中输出
- [ ] 1.2 AppManager 集成 FeishuAPI：每个 app 创建独立的 FeishuAPI 客户端实例，管理 tenant_access_token 缓存
- [ ] 1.3 AppManager 集成 FeishuLongPollClient：每个 app 创建独立的 longpoll 实例
  - 改造 `feishu_longpoll.py` 从全局单例改为支持多实例
  - 每个 longpoll 的事件处理需关联 app_id 用于路由
- [ ] 1.4 网关启动时从 `runtime/apps.json` 加载所有应用，自动恢复 longpoll 连接

## 2. 绑定存储扩展

- [ ] 2.1 BindingStore 支持 (app_id, owner_id) 复合键
  - 多应用绑定键格式：`{app_id}:{owner_id}`
  - 传统绑定（无 app_id）继续用 owner_id 单键
  - 向后兼容：旧格式 bindings.json 正常加载
- [ ] 2.2 WebSocketRegistry 支持多应用上下文
  - 连接关联 app_id，注册键改为复合键（多应用模式）
  - 传统模式连接继续用 owner_id 单键

## 3. 两阶段注册协议

- [ ] 3.1 WS register 消息处理扩展
  - 解析 app_id / app_secret 字段
  - 有 app_id 时：检查 AppManager 是否已注册
  - 已注册 → 使用对应 API 客户端走正常流程
  - 未注册 + 有 app_secret → 验证凭据、注册应用、启动 longpoll、走正常流程
  - 未注册 + 无 app_secret → 返回 `app_not_registered` 错误，不关闭连接
  - 无 app_id → 传统模式（不变）
- [ ] 3.2 HTTP /register 端点同步扩展（与 WS 逻辑对齐）
- [ ] 3.3 Callback WS 客户端适配两阶段注册
  - register 消息携带 app_id（必须）和 app_secret（如配置）
  - 收到 `app_not_registered` 且有 app_secret → 自动重试携带 app_secret
  - 收到 `app_not_registered` 且无 app_secret → 记录错误日志，按退避重试
- [ ] 3.4 授权卡片发送使用对应 app 的 API 客户端（而非网关自身应用）

## 4. 多应用事件路由

- [ ] 4.1 longpoll 事件处理器关联 app_id：每个 app 的 longpoll 事件处理函数知道自己属于哪个 app
- [ ] 4.2 事件路由逻辑：从事件中提取 user_id → 按 (app_id, user_id) 查绑定 → 转发
- [ ] 4.3 网关自身应用事件路由保持不变（owner_id 单键）

## 5. 出站消息代理

- [ ] 5.1 `/gw/feishu/send` 接口适配多应用
  - 从请求的 auth_token/owner_id 反查绑定中的 app_id
  - 使用对应 app 的 FeishuAPI 实例发送消息
- [ ] 5.2 卡片更新（审批后置灰等）使用对应 app 的 API 客户端
- [ ] 5.3 传统模式出站消息不变

## 6. 向后兼容验证

- [ ] 6.1 不携带 app_id 的注册流程完全不变（WS 和 HTTP 模式）
- [ ] 6.2 网关自身应用的 longpoll 和事件处理不受影响
- [ ] 6.3 现有权限审批、消息转发、会话续发功能正常
- [ ] 6.4 持久化数据兼容：旧 bindings.json 可正常加载

## 7. 默认网关地址与模式检测

- [ ] 7.1 config.py 中硬编码中心网关默认地址（如 `wss://gateway.example.com`）
- [ ] 7.2 模式检测逻辑（`FEISHU_SEND_MODE=openapi` 时）：
  - GATEWAY_URL 未设置 → 默认连中心网关
  - GATEWAY_URL = `local` → standalone 本地模式
  - GATEWAY_URL = 自定义地址 → 连指定网关
- [ ] 7.3 Callback WS 客户端 register 消息自动携带 app_id（从 FEISHU_APP_ID 读取）
- [ ] 7.4 迁移提示：standalone 用户升级后若未设 `FEISHU_GATEWAY_URL=local`，启动时给出明确提示

## 8. 配置与文档

- [ ] 8.1 Callback 端新增可选配置项 `FEISHU_APP_ID`（多应用模式必须）、`FEISHU_APP_SECRET`（应用管理员首次注册必须）
- [ ] 8.2 更新 .env.example 和 README 文档
- [ ] 8.3 更新 CHANGELOG
