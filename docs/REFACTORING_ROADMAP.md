# 重构路线图

> 分阶段重构计划，每个 Issue 独立可交付，互不阻塞（除标注依赖外）。
>
> 创建: 2026-04-03 ｜ 最后更新: 2026-06-20

## 进度总览

| Issue | 目标 | 状态 |
|-------|------|------|
| 1 | 拆分 `handlers/feishu.py` | ✅ 已完成（2026-06-16） |
| 2 | 拆分 `lib/feishu.sh` | ⬜ 待办 |
| 3 | 拆分 `services/feishu_api.py` | ⬜ 待办 |
| 4 | DeploymentMode 状态机 | ⬜ 待办 |
| 5 | RequestManager 职责拆分 | ⬜ 待办 |
| 6 | Handler 依赖注入 | ⬜ 待办 |
| 7 | 结构化日志 context | ⬜ 待办 |
| 8 | Shell/Python 配置统一 | ⬜ 待办 |
| 9 | 核心路径单元测试 | ⬜ 待办 |
| 10 | 端到端集成测试 | ⬜ 待办 |
| 11 | `outbound.py` 迁移到 services 层 | ⬜ 待办 |
| 12 | 拆分 `handlers/register.py`（HTTP/WS/授权卡片） | ⬜ 待办 |
| 13 | 拆分 `handlers/callback.py`（决策/会话/目录/配置） | ⬜ 待办 |

---

## Phase 1: 拆分上帝文件（消除 P0 技术债）

### Issue 1: 拆分 `handlers/feishu.py` ✅ 已完成

**现状**: 单文件混合了飞书事件处理、消息构建、卡片生成、内容脱敏、回调路由。原文件 4973 行。

**实际结构**（拆分为 `handlers/feishu/` 包，10 文件，外部 import 路径不变）:

```
handlers/feishu/
├── __init__.py       → 事件路由门面 + _COMMANDS + re-export 公开 API
├── utils.py          → 工具（binding 查找/内容脱敏/agent 命令）
├── forward.py        → WS/HTTP 隧道转发
├── message.py        → 消息发送 + 帮助/状态等杂项卡片
├── card_session.py   → 新会话表单卡片构建
├── card_action.py    → 卡片交互处理 + 卡片状态更新
├── command.py        → /new /reply /groups /attach /clear /users
├── group.py          → 群聊 CRUD + HTTP 端点 + 群聊列表卡片
├── mute.py           → /mute /unmute + 静音列表卡片
└── notify.py         → /notify
```

**验收结果**:

- [x] 每个文件实际代码量 ≤ 500 行（排除空行/注释/docstring）
- [x] `__init__.py` 只做事件路由分发与命令派发，不构建卡片
- [x] 所有现有功能不变（22 个单元测试通过）
- [x] 无循环引用（依赖规则：`utils.py` 为纯叶子，遇环用局部 import）

> 说明：原计划的 3 个扁平文件不足以满足 ≤500 行约束（原文件实际 4973 行而非预估的 3224），故改为按职责拆为 10 文件的包。

---

### Issue 2: 拆分 `lib/feishu.sh`

**现状**: 单文件混合卡片构建、Webhook/OpenAPI 发送、模板变量替换、内容截断。

**目标结构**:

```
lib/
├── feishu.sh              → 保留: 公共接口 (source 其他文件, ≤100 行)
├── feishu-card.sh         → 新建: 权限/通知卡片 JSON 构建
├── feishu-send.sh         → 新建: Webhook/OpenAPI 发送逻辑
└── feishu-content.sh      → 新建: 内容截断、格式化、脱敏
```

**验收标准**:

- [ ] 每个文件 ≤ 500 行
- [ ] feishu.sh 作为门面，`source` 子文件后暴露原有函数
- [ ] 不修改 hooks/ 层的调用方式

---

### Issue 3: 拆分 `services/feishu_api.py`

**现状**: 混合 HTTP 客户端、Token 生命周期、消息构建与发送、卡片更新。

**目标结构**:

```
services/
├── feishu_api.py          → 保留: 高层消息 API (send_message, update_card, ≤400 行)
├── feishu_http_client.py  → 新建: HTTP 请求 + Token 管理 + 重试
└── feishu_token.py        → 新建: access_token 缓存与刷新
```

**验收标准**:

- [ ] Token 管理逻辑独立，可单独测试
- [ ] feishu_api.py 只暴露业务语义接口（send_message, update_card）

---

### Issue 12: 拆分 `handlers/register.py`

**现状**: 1279 行，21 个函数。同一文件混合了三类职责：网关侧 HTTP 注册（`handle_register_request` / `_process_registration` / 授权卡片构建与下发 / 管理员通知）、WS 隧道授权流程（`handle_ws_*` 约 8 个函数）、归属验证与解绑（`_check_owner_id` / `handle_register_unbind`）。

**目标结构**（按注册通道拆分）:

```
handlers/register/
├── __init__.py        → 对外门面，保留现有 import 路径
├── http_register.py   → HTTP 注册请求 + 回调 + 归属验证
├── ws_register.py     → WS 隧道注册/授权/解绑
└── auth_card.py       → 授权卡片构建与下发（含管理员通知）
```

**验收标准**:

- [ ] 每个文件 ≤ 500 代码行
- [ ] 外部 import 路径不变（`from handlers.register import ...`）
- [ ] 网关鉴权逻辑（owner_id 比对）集中、可单测

---

### Issue 13: 拆分 `handlers/callback.py`

**现状**: 1151 行，24 个 handler。回调侧 `/cb/*` 所有端点的总入口，混合了权限决策、目录浏览/记录、会话 attach/mute、目录 mute、通知配置、env 覆盖、VSCode URI 构建等互不相关的功能。

**目标结构**（按端点域拆分）:

```
handlers/callback/
├── __init__.py        → 端点注册门面
├── decision.py        → 权限决策回调 + VSCode URI
├── session.py         → 会话 new/continue/attach/mute/env
├── directory.py       → 目录 recent/browse/record/mute
└── config.py          → 通知配置 + 杂项查询
```

**验收标准**:

- [ ] 每个文件 ≤ 500 代码行
- [ ] `http_handler.py` 的路由分发表改为引用各子模块，行为不变
- [ ] 端点与 owner 鉴权一一对应，便于审计

---

## Phase 2: 架构改善

### Issue 4: 引入 DeploymentMode 显式状态机

**现状**: `main.py` 中 5 种部署模式通过 if/elif 条件散落判断，config.py 有多个 `IS_*` 计算布尔值。

**方案**:

1. 新建 `server/deployment.py`，定义 `DeploymentMode` 枚举
2. 启动时一次性计算模式 + 验证配置完整性
3. `main.py` 改为 `switch` 式启动（一个模式一个启动函数）

**验收标准**:

- [ ] `main.py` 无部署模式相关的 if/elif 条件判断
- [ ] 配置缺失时启动报错明确告知缺什么
- [ ] 日志输出当前部署模式名称

---

### Issue 5: RequestManager 职责拆分

**现状**: RequestManager 同时负责请求注册、Socket 连接生命周期、决策响应发送、超时清理。

**方案**:

```
services/
├── request_registry.py    → 新建: 请求存储 (register, get, remove)
├── request_dispatcher.py  → 新建: Socket I/O (发送决策, 关闭连接)
└── request_manager.py     → 保留: 编排层 (协调 registry + dispatcher)
```

**验收标准**:

- [ ] Registry 可独立单测（纯内存操作）
- [ ] Dispatcher 可通过 mock socket 测试
- [ ] 现有 callback.py 调用方式不变

---

### Issue 6: Handler 依赖注入

**现状**: Handler 通过 `ServiceClass.get_instance()` 获取全局单例。

**方案**:

1. Handler 改为类，构造函数接收依赖
2. `main.py` 中创建实例并注入
3. 现有单例保持向后兼容，标记 `@deprecated`

**验收标准**:

- [ ] 新增的 handler 不使用 `get_instance()`
- [ ] 至少 `callback.py` 改为注入方式作为示范
- [ ] 旧 handler 无需立即改造

---

### Issue 11: `outbound.py` 迁移到 services 层（解耦建群反向依赖）

**现状**: `handlers/outbound.py`（callback 侧飞书出站门面：reply 文本/卡片、移除 Typing、建群）概念上是 service——不吃 HTTP `handler`、不处理入站请求，只在 FeishuAPIService 直发与网关转发之间做部署模式抽象。但其 `create_feishu_group` 的单机路径调用了 `handlers.feishu.create_group_chat_and_record`（建群 + 写归属编排，定义在 `handlers/feishu/group.py`）。

因此它被这条依赖钉在 handlers 层：若直接迁入 services/ 会形成 `services ──► handlers` 反向依赖，违反 ARCHITECTURE 2.2 的强制依赖方向。当前留在 handlers/ 是依赖一致的，但层归属名实不副。

**方案**（分两步，先解依赖再迁移）:

1. 将 `create_group_chat_and_record` 的「建群 + 写群聊归属记录」编排从 `handlers/feishu/group.py` 下沉到 services/（如 `services/group_provision.py`）；`handlers/feishu/group.py` 的 HTTP 端点改为薄封装调该 service
2. `outbound.py` 的单机路径改依赖该 service，`services → handlers` 反向依赖消除
3. 整体迁移 `handlers/outbound.py` → `services/outbound.py`，更新调用方（`agent.py` / `callback.py`）的 import 来源

**验收标准**:

- [ ] `outbound` 不再 import 任何 `handlers.*`，符合 services 层依赖方向
- [ ] `handlers/feishu/group.py` 的建群 HTTP 端点行为不变（改为调 service）
- [ ] `agent.py` / `callback.py` 调用方式不变（仅 import 来源由 `handlers.outbound` 变为 `services.outbound`）

**依赖**: 与 Issue 3（拆分 `feishu_api`）无强依赖；若同期进行，可一并梳理飞书相关 service 的边界

---

## Phase 3: 可观测性

### Issue 7: 结构化日志 context

**现状**: 日志中 request_id/session_id 靠手动拼接 f-string，格式不统一。

**方案**:

1. 新建 `shared/log_context.py`，使用 `contextvars` 传递 request_id/session_id
2. 自定义 Formatter 自动注入 context 字段
3. 逐步替换手动拼接

**验收标准**:

- [ ] 新增代码使用 `log_context.set(request_id=..., session_id=...)`
- [ ] 日志格式: `[{timestamp}] [{level}] [{request_id}] [{session_id}] {message}`
- [ ] 不要求一次性改完所有旧日志

---

### Issue 8: Shell/Python 配置统一

**现状**: `core.sh` 的 `get_config` 和 `config.py` 的 `get_config` 各自解析 `.env`。

**方案**:

1. Python `config.py` 导出 `export_config_json()` → 输出 JSON
2. Shell 新增 `config-bridge.sh`：调用 Python 生成 JSON → 用 json_get 读取
3. 保留 `get_config` 接口不变，底层改为读 JSON cache

**验收标准**:

- [ ] `.env` 只在 Python 端解析一次
- [ ] Shell 端行为不变
- [ ] 新增配置项无需两端同步修改解析逻辑

**依赖**: 无（可独立执行）

---

## Phase 4: 测试与稳定性

### Issue 9: 核心路径单元测试

**范围**:

- `decision_handler.py` — 决策逻辑测试（allow/deny/always/interrupt 各路径）
- `models/decision.py` — 数据结构序列化
- Socket 协议编解码（4-byte 长度前缀）
- `deployment.py` — 模式判定逻辑（Issue 4 完成后）

**验收标准**:

- [ ] 新建 `test/unit/` 目录
- [ ] pytest 可运行
- [ ] 核心决策路径 100% 覆盖

---

### Issue 10: 端到端集成测试

**范围**:

- Socket IPC 完整流程（request → ack → decision）
- 飞书卡片构建（验证 JSON 结构有效）
- 权限规则写入（rule_writer 写入 → 文件内容正确）

**验收标准**:

- [ ] 新建 `test/integration/` 目录
- [ ] 可在 CI 中运行（不依赖外部服务）
- [ ] 至少覆盖权限审批的完整流程

---

## 执行建议

| Phase | 预估工作量 | 风险 | 优先级 | 状态 |
|-------|-----------|------|--------|------|
| Phase 1 (Issue 1-3, 12-13) | 每个 Issue 半天 | 低（纯拆分，不改逻辑） | **最高** | Issue 1 ✅，2/3/12/13 待办 |
| **Phase 4 (Issue 9-10) 测试网** | 共 2 天 | 低 | **最高（提前）** | 待办 |
| Phase 2 (Issue 4-6, 11) | 每个 Issue 1 天 | 中（改接口，需回归测试） | 高 | 待办 |
| Phase 3 (Issue 7-8) | 每个 Issue 半天 | 低（渐进式） | 中 | 待办 |

**原则**: 每个 Issue 独立提交，确保可回滚。Phase 1 剩余 Issue 2/3/12/13 可并行执行。

> **测试优先级上调（2026-06-20）**: 代码量已翻倍且刚完成一连串结构重构（store 抽离、feishu 拆包、utils 重组），核心路径（决策、Socket 协议编解码、JsonStore 持久化）仍缺回归测试网，是当前最大风险敞口。建议把 Issue 9（核心路径单测）提到 Issue 2 之后立即铺设，再继续 Phase 2 的接口改动，让后续重构有测试兜底。
