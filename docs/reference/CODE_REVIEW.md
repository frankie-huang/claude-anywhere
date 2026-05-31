# code-anywhere 项目 — 代码审查报告

**审查日期**: 2026-05-30
**审查范围**: 全部源码（12 个 Bash 脚本、49 个 Python 文件、5 个 Dashboard Python 文件、项目结构/配置/文档）
**代码规模**: ~15,000 行（Bash ~5,500 行, Python ~9,500 行）

---

## 一、上次审查（2025-02-15）问题追踪

### 已修复 ✅（13 项）

| 原编号 | 问题 | 状态 |
|--------|------|------|
| #1 | Bash 脚本 Python 内联代码命令注入 | ✅ 已通过 `sys.argv` / 环境变量传参修复 |
| #2 | HTML 注入 / XSS 漏洞 | ✅ 已使用 `html.escape()` + `json.dumps()` 修复 |
| #3 | 路径遍历风险（browse-dirs） | ✅ 已使用 `os.path.realpath()` 修复 |
| #4 | HTTP Content-Length 未校验 | ✅ 已添加 `MAX_REQUEST_SIZE` 校验 |
| #6 | Token 验证缺少恒定时间比较 | ✅ 已改用 `hmac.compare_digest()` |
| #7 | `dataclass` 不兼容 Python 3.6 | ✅ 已改为手动 `__init__` |
| #8 | JSON Store 文件写入非原子操作 | ✅ 已改为"临时文件 + `os.replace()`"原子写入 |
| #12 | 全局单例懒加载无线程保护 | ✅ 已添加双重检查锁定 |
| #13 | `tool-config.sh` `_tool_config_get` 传参错误 | ✅ 已修正为两参数形式 |
| #14 | Bash 4+ 特性在 macOS 下不可用 | ✅ `mapfile` → `while read`、`declare -A` → `case` 函数、`base64 -w 0` → `tr -d '\n'` |
| #16 | `_run_in_background` 函数重复定义三次 | ✅ 已提取到 `handlers/utils.py` |
| #19 | `.env.example` 被 `.gitignore` 忽略 | ✅ 已从 `.gitignore` 移除 |
| #23 | Unix Socket 文件删除 TOCTOU 竞态 | ✅ 已改为 `try: os.unlink() except FileNotFoundError: pass` |

### 无需修复 / 接受风险（3 项）

| 原编号 | 问题 | 说明 |
|--------|------|------|
| #5 | `permission.sh` JSON 手工拼接 | ⚠️ 接受风险：`message` 来自服务端硬编码字符串，非用户输入 |
| #15 | `reload_config()` 不更新已导出变量 | 无调用者，无实际风险 |
| #25 | Python 2 兼容导入 | ✅ 已移除（`feishu_api.py` 不再有 `from urllib2` fallback） |

### 改善但仍有残留（2 项）

| 原编号 | 问题 | 当前状态 |
|--------|------|----------|
| #21 | `feishu.sh` JSON 手工拼接 | 部分改用 `json_build_object`，但仍有手工拼接残留 |
| #27 | `logging_config.py` 未被使用 | ✅ 现已被 `main.py`、`socket_client.py`、`feishu.py`、`permission_mcp.py` 引用 |

---

## 二、高优先级问题

### 1. [安全] HTTP 服务监听 0.0.0.0 — 待修复（原 #10）

**文件**: `src/server/main.py:555`

```python
server = ThreadedHTTPServer(('0.0.0.0', HTTP_PORT), HttpRequestHandler)
```

服务器监听所有接口。如无防火墙保护，权限决策接口（`/allow?id=xxx`）、注册接口等可被外部访问。

**建议**: 默认绑定 `127.0.0.1`，通过 `CALLBACK_SERVER_BIND` 配置项允许修改。

---

### 2. [安全] auth_token 明文存储 — 待修复（原 #11）

**文件**: `src/server/services/auth_token_store.py`、`src/server/services/binding_store.py`

auth_token 以明文方式存储在 `runtime/auth_token.json`、`runtime/bindings.json`。

**建议**: 设置文件权限为 `0o600`（仅所有者可读写），在所有 Store 的 `_save()` 方法中添加：
```python
os.chmod(tmp_file.name, 0o600)
```

---

### 3. [安全] 日志中记录完整原始数据 — 待修复（原 #20）

**文件**: `src/server/handlers/feishu.py:388`

```python
'raw_data': data  # 记录完整的原始数据
```

飞书事件的 `raw_data` 完整写入日志，可能包含 token、用户消息等敏感信息。

**建议**: 只记录必要字段（event_type、event_id、sender），不记录完整 raw_data。

---

## 三、中优先级问题

### 4. [质量] feishu.py 文件过大（4565 行） — 新发现

**文件**: `src/server/handlers/feishu.py` (4565 行)

单文件 4500+ 行，包含消息处理、卡片构建、Markdown 格式化、会话管理、群聊逻辑等多个职责，维护难度高。

**建议**: 按职责拆分为子模块：
- `feishu_message.py` — 消息接收与分发
- `feishu_card.py` — 卡片构建与回调处理
- `feishu_session.py` — 会话管理逻辑

---

### 5. [质量] Store 类模板代码重复（7 个 Store） — 待修复（原 #17，问题加剧）

现有 7 个 Store 类，`_load()`、`_save()`、`initialize()`、`get_instance()` 几乎完全相同：

| Store | 文件 |
|-------|------|
| BindingStore | `services/binding_store.py` |
| MessageSessionStore | `services/message_session_store.py` |
| SessionChatStore | `services/session_chat_store.py` |
| AuthTokenStore | `services/auth_token_store.py` |
| DirectoryStore | `services/directory_store.py` |
| GroupChatStore | `services/group_chat_store.py` |
| GroupSessionStore | `services/group_session_store.py` |

**建议**: 抽取 `BaseJsonStore` 基类，将 `_load()`、`_save()`、`get_instance()` 等公共逻辑统一管理。

---

### 6. [质量] permission.sh 两种模式代码重复 — 待修复（原 #18，已改善）

**文件**: `src/hooks/permission.sh`

`run_interactive_mode`（362 行）与 `run_fallback_mode`（517 行）仍有重复逻辑。已通过 `prepare_common_vars`（324 行）抽取部分公共变量准备，但发送流程仍有大量重复。

**建议**: 进一步抽取公共的通知发送函数。

---

### 7. [质量] feishu.sh JSON 手工拼接残留 — 待修复（原 #21，部分改善）

**文件**: `src/lib/feishu.sh`

部分场景已改用 `json_build_object`（如 1423、1700 行），但仍有其他位置直接拼接 JSON 字符串。

**建议**: 统一使用 `json_build_object` 或 `json_escape` 构建请求体。

---

### 8. [质量] WebSocket 隧道客户端关闭不完整 — 新发现

**文件**: `src/server/services/ws_tunnel_client.py:128`

`_request_executor` (ThreadPoolExecutor) 在 `stop()` 中使用 `wait=False` 关闭，放弃了正在处理的请求。如果 `stop()` 未被调用（如异常中断），线程池线程将永不释放。

**建议**: 使用 `shutdown(wait=True)` 并设置合理超时，或添加 `__del__` 安全网。

---

### 9. [质量] WebSocket Registry `get_status()` 快照不一致 — 新发现

**文件**: `src/server/services/ws_registry.py`

`get_status()` 分别读取 `_connections` 和 `_pending` 字典，两次读取之间状态可能变化，导致返回的 `authenticated_count` 与 `pending_count` 不一致。

**建议**: 获取两个字典的快照时使用一致的锁顺序：
```python
with self._connections_lock:
    auth_snapshot = dict(self._connections)
with self._pending_lock:
    pending_snapshot = dict(self._pending)
```

> 注：虽然两次加锁之间仍有间隙，但对于状态展示用途，这种程度的一致性已经足够。

---

## 四、低优先级问题

### 10. [规范] 裸 `except:` 吞掉所有异常 — 待修复（原 #31，新增位置）

3 处嵌入式 Python 代码使用裸 `except:`：

| 文件 | 行号 | 上下文 |
|------|------|--------|
| `src/hooks/stop.sh` | 308 | `json.loads(line)` 解析 |
| `src/lib/feishu.sh` | 2137 | `json.loads()` 解析 questions |
| `src/lib/tool.sh` | 128 | 工具描述格式化 |

**建议**: 改为 `except Exception:` 或更具体的 `except (json.JSONDecodeError, ValueError):`。

---

### 11. [规范] 环境变量读取不符规范 — 待修复（原 #24）

**文件**: `src/lib/core.sh:442`

```bash
if [ "${DEBUG:-0}" != "1" ]; then
```

应改用 `get_config "DEBUG" "0"`，以支持从 `.env` 文件读取。

---

### 12. [质量] `json_build_object` 不做 JSON 转义 — 待修复（原 #26）

**文件**: `src/lib/json.sh`

值中含双引号或反斜杠时生成无效 JSON。建议至少对 `\` 和 `"` 做基本转义。

---

### 13. [质量] HTTP 响应发送代码重复 — 待修复（原 #28）

**文件**: `src/server/handlers/callback.py`

`do_POST` 每个路由分支都重复 `send_response/send_header/end_headers/wfile.write`。

**建议**: 提取 `send_json_response()` 辅助方法。

---

### 14. [风格] 变量命名和路径解析方式不统一 — 待修复（原 #29）

- 全局变量命名：`TOOLS_CONFIG_CACHE` vs `_ENV_FILE_CACHE`，风格不一致
- 函数命名：内部辅助函数有的使用 `_` 前缀（如 `_tool_config_get`），有的没有（如 `prepare_common_vars`）

---

### 15. [类型] 多处类型注解不够精确 — 待修复（原 #30）

新增文件（`ws_registry.py`、`ws_protocol.py`、`session_facade.py` 等）的类型注解总体较好，但部分旧文件仍有 `Optional[dict]`（应为 `Optional[Dict[str, Any]]`）等不精确注解。

---

### 16. [测试] 测试覆盖严重不足 — 待修复（原 #32）

仍仅有 2 个手动测试脚本（`test/test-permission.sh`、`test/test-permission-quick.sh`），均针对 PermissionRequest。新增的 WebSocket 注册、多 Agent 支持、群聊会话、遥测等模块均无测试。

缺少：
- WebSocket 协议测试
- Store 类单元测试
- Agent 分发逻辑测试
- 飞书消息处理测试
- 自动化测试框架 / CI 集成

---

### 17. [配置] `.gitignore` 规则冗余 — 待修复（原 #34）

`*.sock` 在第 50 行和第 56 行重复出现。

---

### 18. [质量] BindingStore 配置值静默降级 — 新发现

**文件**: `src/server/services/binding_store.py`

`group_dissolve_days` 等数值配置在转换失败时静默降为 `0`，不产生日志告警。用户可能误输入 `"abc"` 而不自知。

**建议**: 转换失败时记录 `logger.warning()`，帮助用户排查配置错误。

---

## 五、统计汇总

### 本次审查总览

| 严重程度 | 总数 | 新发现 | 旧项延续 | 主要类别 |
|---------|------|--------|---------|---------|
| **高** | 3 | 0 | 3 | 网络监听、敏感数据存储/日志 |
| **中** | 6 | 3 | 3 | 代码规模、模板重复、WebSocket 资源管理 |
| **低** | 9 | 1 | 8 | 规范合规、类型注解、测试覆盖、代码风格 |

### 与上次审查对比

| 指标 | 2025-02-15 | 2026-05-30 | 变化 |
|------|-----------|-----------|------|
| 总问题数 | 35 | 18 | -17 |
| 高优先级 | 9 | 3 | -6（修复 7，接受 1，降级 1） |
| 中优先级 | 14 | 6 | -8（修复 5，无需修复 1，新增 3） |
| 低优先级 | 12 | 9 | -3（修复 2，新增 1） |
| Python 文件数 | ~25 | 49 (+5 dashboard) | +29 |
| Bash 脚本数 | 13 | 12 | -1 |
| 安全漏洞 | 7 (5已修复) | 0 新增 | 显著改善 |

### 代码健康趋势

**改善方面**：
- 所有已知安全漏洞已修复（命令注入、XSS、路径遍历、TOCTOU）
- 原子文件写入已全面应用于所有 Store
- 线程安全防护已加强（双重检查锁定、`hmac.compare_digest`）
- 日志配置已统一使用 `logging_config.py`
- macOS/Linux 兼容性问题已基本解决

**需关注方面**：
- 代码规模增长迅速（Python 文件数翻倍），部分文件过大（feishu.py 4565 行）
- Store 类增至 7 个，模板代码重复问题加剧
- 测试覆盖仍严重不足，新模块均无自动化测试
- 3 个高优先级旧项长期未修复（0.0.0.0 监听、auth_token 明文、raw_data 日志）

## 六、建议修复优先级

1. **尽快修复**: 安全类（#1 监听地址、#2 文件权限、#3 日志脱敏）
2. **计划修复**: 代码质量（#4 拆分 feishu.py、#5 BaseJsonStore 基类）
3. **持续改进**: 测试覆盖（#16）、规范统一（#10-#14）
4. **长期优化**: 代码重复消除（#6, #7, #13）、WebSocket 资源管理（#8, #9）
