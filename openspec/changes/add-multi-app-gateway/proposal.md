# Change: 中心化多应用飞书网关

**Status: DEFERRED（暂缓实施）**

### 暂缓原因

1. **安全性与信任矛盾**：中心网关持有所有租户的 app_secret 并通过 longpoll 解密全部消息明文，注重隐私的企业用户不会接受将飞书应用控制权和消息可见性交给第三方。若改为加密透传（网关不解密），Callback 端需要自行处理飞书协议解密，复杂度回到与自建网关相当的水平，中心网关的简化价值被抵消。
2. **投入产出比不足**：涉及 ~1300 行代码改动，深度侵入核心数据结构（BindingStore、WebSocketRegistry 复合键改造），引入 breaking change，而现有架构通过 `setup.sh` 一键部署 + WS 隧道已将自建网关门槛降到较低水平。
3. **额外运维负担**：中心网关需要保证高可用、多租户隔离和 app_secret 安全存储，运营成本不可忽视。
4. **用户群体有限**：愿意将 app_secret 和消息明文交给第三方的用户本身是少数，现有自建方案已能覆盖大部分场景。

### 重新评估条件

- 用户量增长到自建网关成为普遍痛点时
- 找到安全性与易用性的平衡方案（如端到端加密 + 零知识路由）

---

## Why

当前分离部署模式下，每个飞书应用需要独立部署一套网关服务。用户需要自行搭建服务端环境、管理依赖和运维。希望提供一个中心化网关，用户只需运行轻量的 Callback 端，携带飞书应用凭据注册即可接入，无需自行部署网关。

## What Changes

- **中心网关模式**: 网关支持以中心化模式启动，接受多个飞书应用的动态注册
- **默认网关地址**: `FEISHU_SEND_MODE=openapi` 时，`FEISHU_GATEWAY_URL` 默认指向中心网关，无需手动配置；自建网关用户可覆盖为自定义地址
- **Standalone 显式声明**: `FEISHU_GATEWAY_URL=local` 表示 standalone 本地模式（**Breaking Change**: 现有 standalone 用户升级后需新增此配置）
- **多应用 Longpoll 管理**: 网关为每个注册的飞书应用维护独立的 longpoll 长连接，接收并解密事件
- **两阶段注册**: Callback 先尝试 app_id + owner_id 注册；若 app 未注册且配置了 app_secret，自动重试携带凭据完成应用首次注册
- **事件路由**: 网关通过 longpoll 接收事件（SDK 自动解密），按 (app_id, sender_id) 路由到对应 Callback
- **出站消息代理**: Callback 通过网关发送飞书消息（权限卡片、Stop 通知等），网关使用对应 app 的 API 客户端发送
- **向后兼容**: 网关自身配置的应用（FEISHU_APP_ID/SECRET）继续正常工作，与动态注册的应用并存

## Impact

- Affected specs: `gateway-auth`（注册协议扩展）、新增 `multi-app-gateway`（多应用管理与事件路由）
- Affected code:
  - 新增 `src/server/services/app_manager.py`: 多应用生命周期管理（凭据存储、longpoll 管理）
  - `src/server/services/feishu_longpoll.py`: 从单例改为支持多实例
  - `src/server/handlers/register.py`: 两阶段注册流程
  - `src/server/handlers/ws_handler.py`: WS register 消息扩展 app_id/app_secret
  - `src/server/handlers/feishu.py`: 多应用事件路由、出站消息使用对应 app API
  - `src/server/services/binding_store.py`: (app_id, owner_id) 复合键
  - `src/server/services/feishu_api.py`: 多实例 API 客户端
  - `src/server/config.py`: 中心网关模式检测
  - `src/server/main.py`: 启动逻辑适配
