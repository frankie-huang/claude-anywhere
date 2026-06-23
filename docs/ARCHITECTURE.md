# Claude-Anywhere 架构约束文档

> 本文档是项目的架构权威参考。所有新增代码、重构和 Code Review 必须遵循此处定义的约束。
>
> 最后更新: 2026-06-20

---

## 1. 系统全景

### 1.1 项目定位

Claude-Anywhere 是 Claude Code 的飞书集成扩展，通过 Hook 机制拦截 Claude Code 的权限请求、任务通知、会话停止等事件，将交互能力延伸到飞书消息卡片，实现远程审批和会话继续。

### 1.2 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| Hook 脚本层 | Bash | Claude Code Hook 入口，事件路由与飞书卡片构建 |
| 后端服务层 | Python 3.6+ | HTTP/Socket 服务器，业务逻辑，状态管理 |
| 本地 IPC | Unix Domain Socket | Hook 脚本 ↔ Python 后端的同步通信 |
| 远程通信 | WebSocket / HTTP | 分离部署时 Callback ↔ Gateway 通信 |
| 外部集成 | 飞书 OpenAPI / Webhook | 消息推送与事件接收 |

### 1.3 端到端数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                        Claude Code                               │
│  ┌──────────────────┐ Hook Event (stdin)  ┌──────────────────┐  │
│  │ PermissionRequest ├───────────────────────► hook-router.sh   │  │
│  │ Stop              │                     │  ├─ permission.sh│  │
│  │ UserPromptSubmit  │                     │  ├─ stop.sh      │  │
│  └──────────────────┘                     │  └─ user_prompt  │  │
│       ▲                                     └───────┬──────────┘  │
│       │ exit code + stdout                          │             │
│       │ (allow/deny/fallback)                       │             │
└───────┼─────────────────────────────────────────────┼─────────────┘
        │                                             │
        │  ┌──────────────────────────────────────────┘
        │  │  Unix Socket (IPC)
        │  │  ┌─────────────────┐      ┌──────────────────────┐
        │  │  │  request JSON   │      │                      │
        │  └──►                 ├──────► Python Backend        │
        │     │  ack JSON       │      │  ├─ handlers/        │
        │     │  decision JSON  │◄─────┤  ├─ services/        │
        │     └─────────────────┘      │  ├─ stores/          │
        │                              │  ├─ utils/           │
        │                              │  ├─ models/          │
        │                              │  └─ telemetry/       │
        └──────────────────────────────┘      │
                                              │ HTTP/WS
                                    ┌─────────┴─────────┐
                                    │   飞书 OpenAPI     │
                                    │   ├─ 发送消息卡片   │
                                    │   ├─ 接收事件回调   │
                                    │   └─ 长连接(可选)   │
                                    └───────────────────┘
                                              │
                                    ┌─────────┴─────────┐
                                    │   用户飞书客户端    │
                                    │   点击按钮 → HTTP  │
                                    │   回复消息 → Event │
                                    └───────────────────┘
```

### 1.4 部署模式

系统支持 5 种部署模式，由配置项组合决定：

```
                 ┌──────────────────────────────────────┐
                 │          DeploymentMode               │
                 ├──────────────────────────────────────┤
                 │  WEBHOOK          纯 Webhook 通知    │
                 │  OPENAPI_STANDALONE  单机全功能      │
                 │  OPENAPI_CALLBACK_WS  WS 隧道分离   │
                 │  OPENAPI_CALLBACK_HTTP HTTP 分离     │
                 │  PURE_GATEWAY      纯网关           │
                 └──────────────────────────────────────┘
```

| 模式 | 飞书凭据 | Gateway | 回调服务 | 适用场景 |
|------|---------|---------|---------|---------|
| WEBHOOK | 不需要 | 不需要 | 不需要 | 最简部署，仅单向通知 |
| OPENAPI_STANDALONE | 本地 | 本地 | 本地 | 单机全功能，开发测试 |
| OPENAPI_CALLBACK_WS | 远端网关 | 远端 | 本地 | 内网/NAT 后部署 |
| OPENAPI_CALLBACK_HTTP | 远端网关 | 远端 | 本地(公网) | 多实例分离部署 |
| PURE_GATEWAY | 本地 | 本地 | 不需要 | 纯网关，无 Claude |

**模式判定规则**（目标：启动时显式计算，非运行时条件散落）：

> 注：当前判定逻辑散落在 `main.py`/`config.py` 的 if/elif 中，显式 `DeploymentMode` 枚举尚未实现（见 Roadmap Issue 4）。下方为目标判定顺序。

```
if FEISHU_SEND_MODE == 'webhook':
    → WEBHOOK
elif 无 FEISHU_GATEWAY_URL 且有 APP_ID:
    → OPENAPI_STANDALONE
elif 有 FEISHU_GATEWAY_URL 且协议为 ws/wss:
    → OPENAPI_CALLBACK_WS
elif 有 FEISHU_GATEWAY_URL 且有 CALLBACK_SERVER_URL:
    → OPENAPI_CALLBACK_HTTP
elif 无 FEISHU_OWNER_ID:
    → PURE_GATEWAY
```

---

## 2. 分层架构与模块职责

### 2.1 层级总览

```
src/
├── hook-router.sh              # 入口：统一路由
├── hooks/                      # Hook 处理层（事件处理器）
│   ├── permission.sh           #   权限请求 → 飞书卡片 + Socket 等待
│   ├── stop.sh                 #   会话停止 → 飞书通知 + 会话继续
│   └── user_prompt.sh          #   用户输入 → 飞书转发
├── lib/                        # Shell 基础库（纯函数/工具）
│   ├── core.sh                 #   路径、环境、日志、get_config
│   ├── feishu.sh               #   ⚠️ 卡片构建 + API 调用（2371 行，待拆分）
│   ├── json.sh                 #   JSON 解析（jq/python3/grep 降级）
│   ├── socket.sh               #   Socket IPC 客户端
│   ├── tool.sh                 #   工具信息格式化
│   ├── tool-config.sh          #   tools.json 配置加载
│   └── vscode-proxy.sh         #   VSCode SSH 代理
├── server/                     # Python 后端
│   ├── main.py                 #   服务编排与启动
│   ├── config.py               #   统一配置读取
│   ├── socket_client.py        #   Python Socket IPC 客户端
│   ├── handlers/               #   HTTP 请求处理层
│   │   ├── http_handler.py     #     HTTP 路由分发
│   │   ├── callback.py         #     权限决策回调 (/allow, /deny, ...)
│   │   ├── agent.py            #     会话操作 (/cb/session/new, /continue)
│   │   ├── feishu/             #     飞书事件处理（已拆分为包，10 文件）
│   │   │   ├── __init__.py     #       事件路由门面 + _COMMANDS + re-export
│   │   │   ├── utils.py        #       工具（binding/脱敏/agent 命令）
│   │   │   ├── forward.py      #       WS/HTTP 隧道转发
│   │   │   ├── message.py      #       消息发送 + 杂项卡片
│   │   │   ├── card_session.py #       新会话表单卡片
│   │   │   ├── card_action.py  #       卡片交互 + 状态更新
│   │   │   ├── command.py      #       /new /reply /groups 等命令
│   │   │   ├── group.py        #       群聊 CRUD + HTTP 端点
│   │   │   ├── mute.py         #       /mute /unmute
│   │   │   └── notify.py       #       /notify
│   │   ├── register.py         #     网关注册
│   │   ├── ws_handler.py       #     WebSocket 隧道入口
│   │   ├── permission_mcp.py   #     MCP 权限桥接
│   │   ├── outbound.py         #     飞书出站门面（reply/建群/移除 typing）
│   │   └── responses.py        #     HTTP 响应写回（send_json/send_html）
│   ├── services/               #   业务逻辑层
│   │   ├── request_manager.py  #     权限请求生命周期管理
│   │   ├── decision_handler.py #     决策处理逻辑
│   │   ├── session_facade.py   #     会话操作网关
│   │   ├── codex_rule_writer.py #    Codex execpolicy 规则持久化
│   │   ├── feishu_api.py       #     飞书 OpenAPI 客户端
│   │   ├── feishu_longpoll.py  #     飞书 WebSocket 事件接收
│   │   ├── auto_register.py    #     网关自动注册
│   │   ├── auth_token.py       #     Token 生成/验证
│   │   ├── ws_tunnel_client.py #     WS 隧道客户端
│   │   ├── ws_registry.py      #     WS 连接管理
│   │   └── card_cache.py       #     卡片状态缓存
│   ├── stores/                 #   持久化存储层（JsonStore 单例）
│   │   ├── json_store.py       #     JsonStore 基类（原子 JSON 持久化）
│   │   ├── binding_store.py    #     owner_id → callback_url 绑定
│   │   ├── directory_store.py  #     目录权限存储
│   │   ├── auth_token_store.py #     AuthToken 存储
│   │   ├── message_session_store.py # 消息 → session 映射
│   │   ├── session_chat_store.py #   session → 群聊映射
│   │   ├── group_chat_store.py #     群聊归属 + seq 存储
│   │   ├── group_session_store.py #  群聊 → session 映射
│   │   └── notify_config_store.py #  通知配置存储
│   ├── utils/                  #   通用工具（stdlib-only，无领域耦合）
│   │   ├── atomic_json.py      #     原子 JSON 读写
│   │   ├── ttl_cache.py        #     TTL 缓存
│   │   ├── http_client.py      #     通用出站 HTTP（post_json）
│   │   ├── shell.py            #     子进程命令构建（build_shell_cmd）
│   │   ├── concurrency.py      #     后台线程执行（run_in_background）
│   │   └── ws_protocol.py      #     WS 帧编解码（RFC 6455）
│   ├── models/                 #   数据结构层
│   │   ├── decision.py         #     权限决策模型
│   │   └── tool_config.py      #     工具配置模型
│   └── telemetry/              #   遥测（匿名使用统计）
├── shared/                     # 跨语言共享
│   ├── protocol.md             #   Socket 通信协议规范
│   └── logging_config.py       #   Python 日志配置
├── templates/feishu/           # 飞书卡片 JSON 模板
├── config/tools.json           # 工具元数据配置
└── start-server.sh             # 后端启动脚本
```

### 2.2 依赖方向（强制）

```
hooks/ ──► lib/           Hook 处理器调用基础库
hooks/ ──► server/ (via Socket)   IPC 通信

handlers/ ──► services/   HTTP 处理调用业务逻辑
handlers/ ──► models/     使用数据结构
handlers/ ──► stores/     访问持久化
services/ ──► models/     使用数据结构
services/ ──► stores/     访问持久化
handlers/ ──► utils/      复用通用工具（HTTP/shell/并发等）
services/ ──► utils/      复用通用工具
stores/   ──► utils/      复用原子 JSON 等通用工具

✗ services/ ──► handlers/     禁止反向引用
✗ models/   ──► services/     禁止反向引用
✗ stores/   ──► services/     禁止反向引用（store 是叶子）
✗ lib/      ──► hooks/        禁止反向引用
```

### 2.3 已知技术债

> 行数为 `wc -l` 原始值（2026-06-20 实测）；约束按代码行 ≤500（去空行/注释），原始行数 >650 基本可判定超标。Roadmap Issue 编号见 docs/REFACTORING_ROADMAP.md。

| 文件 | 行数 | 问题 | 优先级 | 状态 |
|------|------|------|--------|------|
| `lib/feishu.sh` | 2371 | 混合卡片构建/API 调用/模板/脱敏 | P0 | 待拆（Issue 2） |
| `services/feishu_api.py` | 1536 | 混合 HTTP 客户端/Token 管理/消息发送 | P1 | 待拆（Issue 3） |
| `handlers/register.py` | 1279 | 网关注册(HTTP) + WS 隧道授权 + 授权卡片构建 + 归属验证混合 | P1 | 待拆（Issue 12） |
| `handlers/callback.py` | 1151 | 回调侧所有 `/cb/*` 端点总入口（24 个 handler），决策/目录/会话/静音/通知配置混合 | P1 | 待拆（Issue 13） |
| `agents/__init__.py` | 851 | AgentAdapter 基类 + 工厂 + 共享启动/会话捕获 + env/模板工具混合在包入口 | P2 | 待评估 |
| `services/ws_registry.py` | 778 | pending 连接生命周期 + 授权预备 + 卡片冷却 + token/binding 暂存混合 | P2 | 待评估 |
| `handlers/feishu/card_action.py` | 728 | 卡片交互 + 状态更新；Issue 1 拆分后又涨过 500 | P2 | 待评估 |
| `main.py` | 682 | 初始化序列 + Socket 处理 + 部署条件判断 | P1 | 待拆（Issue 4） |
| `stores/directory_store.py` | 641 | store 内含目录静音决策/向上遍历/遗留迁移等较重逻辑 | P3 | 待评估 |
| `stores/session_chat_store.py` | 632 | store 承载会话生命周期大量业务方法（env 覆盖/静音/迁移/解散） | P3 | 待评估 |
| 全局单例 | 8 处 `get_instance` | 单例难以测试（stores 已统一 `JsonStore` 基类，定义点收敛） | P2 | 待治理（Issue 6） |
| `handlers/feishu.py` | ~~3224~~ | 混合事件处理/消息构建/回调路由 | P0 | ✅ 已拆分为 `feishu/` 包（Issue 1） |

---

## 3. 开发约束

> 以下约束适用于所有新增和修改的代码。Code Review 时必须逐项检查。

### 3.1 文件与函数

| 规则 | 约束 | 原因 |
|------|------|------|
| **单文件行数** | ≤ 500 行（不含空行和注释） | 超过 500 行的文件难以在一屏内理解全貌 |
| **单函数行数** | ≤ 50 行 | 长函数意味着职责不单一 |
| **单一职责** | 每个文件/函数只做一件事 | 混合职责导致修改扩散 |
| **新文件位置** | 必须放入对应层级目录 | handler 不能放 services/，反之亦然 |

### 3.2 依赖与耦合

| 规则 | 约束 | 原因 |
|------|------|------|
| **依赖方向** | 只能上层调下层（见 2.2） | 防止循环依赖 |
| **禁止新增全局单例** | 新代码使用构造函数注入 | 全局状态难以测试和推理 |
| **跨层通信** | Shell ↔ Python 仅通过 Socket 协议 | 维持语言边界清晰 |
| **外部 API** | 所有飞书 API 调用集中在 `feishu_api.py` | 统一错误处理和 Token 管理 |

### 3.3 配置管理

| 规则 | 约束 |
|------|------|
| Shell 配置 | 必须通过 `get_config` (core.sh) 读取 |
| Python 配置 | 必须通过 `get_config` / `get_config_int` (config.py) 读取 |
| **禁止** | 直接使用 `${VAR:-default}` 或 `os.environ.get()` |
| 优先级 | `.env` 文件 > 环境变量 > 默认值 |
| 新增配置项 | 必须同步更新 `.env.example` |

### 3.4 错误处理

| 层 | 规则 |
|---|---|
| Shell Hook | 必须返回有意义的 exit code：0=allow, 1=fallback, 2+=deny |
| Shell 函数 | 返回值表示成功/失败，stderr 输出错误信息 |
| Python Handler | 捕获异常后返回 HTTP 错误码 + JSON 错误体 |
| Python Service | 抛出 typed exception，不吞异常 |
| 飞书 API | 所有调用必须处理超时、Token 过期、频率限制 |

### 3.5 日志

| 规则 | 约束 |
|------|------|
| 必须包含 context | request_id、session_id（可用时） |
| Python | 使用 `logging` 模块，通过 `setup_logging()` 配置 |
| Shell | 使用 `log()` / `log_error()` 函数 (core.sh) |
| **禁止** | `print()` 用于日志输出 |
| 敏感信息 | 禁止日志中出现 Token、Secret、用户手机号等 |

### 3.6 Socket 协议

| 规则 | 约束 |
|------|------|
| 协议规范 | 以 `shared/protocol.md` 为唯一权威参考 |
| 编解码 | Shell 通过 `socket.sh`，Python 通过 `request_manager.py` |
| 变更流程 | 修改协议必须同步更新 protocol.md + Shell + Python |

### 3.7 飞书卡片

| 规则 | 约束 |
|------|------|
| 模板文件 | 放置在 `src/templates/feishu/` |
| 构建逻辑 | 放置在 `lib/feishu.sh`（Shell）或对应 Python 模块 |
| **禁止** | 在 handler/hook 中内联构建卡片 JSON |
| 内容脱敏 | 所有用户可见内容必须经过脱敏处理 |

### 3.8 兼容性

| 维度 | 约束 | 参考 |
|------|------|------|
| Python | 3.6+ 兼容 | 见 CLAUDE.md 类型注解表 |
| Shell | macOS + Linux | 见 CLAUDE.md 跨平台表 |
| 依赖 | 核心功能零外部依赖 | jq/lark-oapi 等为可选增强 |
| 降级 | 每个可选依赖必须有降级路径 | json_init 的三级降级为范例 |

---

## 4. Code Review Checklist

每次 Review（人类或 AI）使用此清单：

### 结构

- [ ] 文件行数 ≤ 500（不含空行注释）？
- [ ] 函数行数 ≤ 50？
- [ ] 文件放在正确的层级目录？
- [ ] 依赖方向正确（上层调下层，无反向）？

### 耦合

- [ ] 无新增 `get_instance()` 全局单例？
- [ ] 无跨层直接引用（handler 不直接操作 store）？
- [ ] 飞书 API 调用集中在 `feishu_api.py`？

### 质量

- [ ] 日志包含 request_id/session_id context？
- [ ] 无 `print()` 用于日志？
- [ ] 配置通过 `get_config` 读取？
- [ ] 错误有合理处理，不吞异常？
- [ ] 无敏感信息泄露（Token、Secret、手机号）？

### 兼容性

- [ ] Python 3.6 兼容（类型注解用 `typing` 模块）？
- [ ] macOS/Linux Shell 兼容？
- [ ] 可选依赖有降级路径？

### 变更管理

- [ ] 功能改动更新了 CHANGELOG.md？
- [ ] 配置变更更新了 .env.example？
- [ ] Socket 协议变更更新了 protocol.md？

---

## 5. 新增模块指南

当你需要添加新功能时，按以下流程确定代码放置位置：

```
新功能是什么？
│
├─ 处理 Claude Code Hook 事件？
│  └─ 放 src/hooks/ 新建 {event}.sh
│
├─ 处理 HTTP 请求/回调？
│  └─ 放 src/server/handlers/ 新建 {feature}.py
│
├─ 包含业务逻辑（非 HTTP 相关）？
│  └─ 放 src/server/services/ 新建 {feature}.py
│
├─ 定义数据结构？
│  └─ 放 src/server/models/ 新建 {model}.py
│
├─ 需要持久化存储？
│  └─ 放 src/server/stores/{feature}_store.py
│  └─ 继承 JsonStore 基类：JSON 文件 + 原子写 + lazy expiry
│
├─ 通用工具（stdlib-only，无领域耦合）？
│  └─ 放 src/server/utils/ 新建 {feature}.py
│
├─ Shell 可复用工具函数？
│  └─ 放 src/lib/ 新建 {feature}.sh
│
├─ 飞书卡片模板？
│  └─ 放 src/templates/feishu/ 新建 {card}.json
│
└─ 跨 Shell/Python 的共享定义？
   └─ 放 src/shared/
```

**新模块必须包含**：
1. 文件头部 docstring 说明模块职责（一句话）
2. 明确的公开接口（Python 用 `__all__`，Shell 用注释标记）
3. 不超过 500 行

---

## 附录 A: 术语表

| 术语 | 含义 |
|------|------|
| Hook | Claude Code 的事件拦截机制，通过 settings.json 配置 |
| Permission Request | Claude Code 请求执行危险操作时触发的审批流程 |
| Decision | 用户对权限请求的决策（allow/deny/always/interrupt） |
| Session | Claude Code 的一次交互会话，有唯一 session_id |
| Gateway | 持有飞书凭据的中心服务，转发请求到 Callback 后端 |
| Callback Backend | 运行在用户机器上的服务，处理权限请求和会话管理 |
| WS Tunnel | Callback 主动连接 Gateway 的 WebSocket 隧道 |
| Longpoll | 飞书 SDK 提供的长连接事件接收模式 |
| Card Cache | 已发送飞书卡片的缓存，用于后续更新卡片状态 |
