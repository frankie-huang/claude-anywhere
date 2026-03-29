# Claude Code Hooks 项目 — 代码审查报告

**审查日期**: 2025-02-15
**审查范围**: 全部源码（13 个 Bash 脚本、25 个 Python 文件、项目结构/配置/文档）

---

## 一、高优先级问题

### 1. ~~[安全] Bash 脚本中 Python 内联代码命令注入风险~~ ✅ 已修复

多处将 Bash 变量直接拼接到 `python3 -c` 的代码字符串中，若变量包含单引号即可注入任意 Python 代码。

| 文件 | 行号 | 拼接变量 |
|------|------|---------|
| `src/lib/vscode-proxy.sh` | 94 | `$project_path` |
| `src/lib/socket.sh` | 239-252 | `$socket_path` |
| `src/lib/json.sh` | 100, 183-186 等 | `$field_path`, `$fields_str` |
| `src/lib/core.sh` | 210-212 | `$config_file` |
| `src/hooks/stop.sh` | 83 | `$transcript_file` |

**修复方案**:
- 有 stdin 可用时：通过 `sys.argv` 传递参数（如 `json.sh` 中的 `"$field_path"`）
- 无 stdin 时：通过前置环境变量传递（如 `_HOOK_PATH="$path" python3 -c "..."`）

---

### 2. ~~[安全] HTML 注入 / XSS 漏洞~~ ✅ 已修复

**文件**: `src/server/handlers/callback.py:64-265`

`send_html_response` 中 `title`、`message`、`vscode_uri` 直接插入 HTML/JavaScript，无任何转义。

**修复方案**: HTML 内容使用 `html.escape()`，JavaScript 字符串使用 `json.dumps()` 输出。

---

### 3. ~~[安全] 路径遍历风险（browse-dirs 端点）~~ ✅ 已修复

**文件**: `src/server/handlers/callback.py:529-599`

`browse-dirs` 接口只检查 `startswith('/')`，未规范化路径。`/etc/../root/.ssh` 这类路径可通过检查，泄露任意目录列表。

**修复方案**: 使用 `os.path.realpath()` 规范化路径，消除 `..`、符号链接等。

---

### 4. ~~[安全] HTTP Content-Length 未校验~~ ✅ 已修复

**文件**: `src/server/handlers/callback.py:383`

Content-Length 未校验范围，负值或超大值可能导致异常或内存耗尽。

**修复方案**: 添加 `MAX_REQUEST_SIZE = 10MB` 常量，校验 Content-Length 范围。

> **注**: Socket 服务端和客户端的大小限制风险较低（本地访问 + 超时机制），接受风险不修复。

---

### 5. [安全] permission.sh 中 JSON 手工拼接 — ⚠️ 接受风险

**文件**: `src/hooks/permission.sh:256`

```bash
decision_json="{\"behavior\": \"deny\", \"message\": \"${message}\", \"interrupt\": true}"
```

`$message` 未经 JSON 转义，含双引号/反斜杠时生成无效 JSON。

**风险分析**: `message` 来自服务端 `decision_handler.py` 的硬编码字符串（如"请求不存在或已过期"），非用户输入，实际注入风险为零。

**决定**: 接受风险，不修复。

---

### 6. ~~[安全] Token 验证缺少恒定时间比较~~ ✅ 已修复

**文件**: `src/server/handlers/feishu.py:165`

```python
if token != FEISHU_VERIFICATION_TOKEN:  # 时序攻击风险
```

项目中 `auth_token.py` 已正确使用 `hmac.compare_digest()`，此处应保持一致。

**修复方案**: 添加 `import hmac`，改用 `hmac.compare_digest()` 进行恒定时间比较。

---

### 7. ~~[兼容性] `dataclass` 不兼容 Python 3.6~~ ✅ 已修复

**文件**: `src/server/models/tool_config.py:11`

`dataclasses` 是 Python 3.7+ 才有的模块，在 3.6 上直接 `ImportError`。

**修复方案**: 移除 `@dataclass` 装饰器，改为手动实现 `__init__` 方法。

---

### 8. ~~[数据安全] JSON Store 文件写入非原子操作~~ ✅ 已修复

影响所有 Store 类（`binding_store.py`、`message_session_store.py`、`session_chat_store.py`、`auth_token_store.py`、`dir_history_store.py`）。进程崩溃时文件可能只写入一半，导致全部数据丢失。

**修复方案**: 使用"写临时文件 + `os.replace()`"的原子写入模式，所有 5 个 Store 类的 `_save()` 方法均已更新。

---

### 9. [文档] README 数据模型列表与实际代码严重脱节

README 列出的 `request.py`、`session.py`、`binding.py` **均不存在**，而实际有的 `tool_config.py`、`auth_token_store.py`、`session_chat_store.py`、`dir_history_store.py` 等未在文档中出现。

---

## 二、中优先级问题

### 10. [安全] HTTP 服务监听 0.0.0.0

**文件**: `src/server/main.py:363`

服务器监听所有接口，如无防火墙保护，权限决策接口（`/allow?id=xxx`）可被外部访问。

**建议**: 默认绑定 `127.0.0.1`，通过配置项允许修改。

---

### 11. [安全] auth_token 明文存储在 JSON 文件中

**文件**: `src/server/services/auth_token_store.py`、`src/server/services/binding_store.py`

auth_token 以明文方式存储在 `runtime/auth_token.json`。

**建议**: 设置文件权限为 `0o600`（仅所有者可读写）。

---

### 12. ~~[并发] 多处全局单例懒加载无线程保护~~ ✅ 已修复

| 文件 | 位置 |
|------|------|
| `handlers/feishu.py:70-95` | `_feishu_message_logger` |
| `models/tool_config.py:244-249` | `_tool_config_manager` |
| `services/auth_token_store.py:77,94` | `_token` 类变量读写不一致 |

**修复方案**:
- `_feishu_message_logger` 和 `_tool_config_manager`：添加专用锁 + 双重检查锁定
- `AuthTokenStore._token`：`get()` 方法也使用 `_file_lock` 保护

---

### 13. ~~[逻辑] tool-config.sh 中 `_tool_config_get` 传参错误~~ ✅ 已修复

**文件**: `src/lib/tool-config.sh:100, 137, 157`

```bash
color=$(_tool_config_get "_defaults.unknown_tool.color")
# 函数签名是 _tool_config_get "tool_name" "field_path"，这里只传了一个参数
```

导致构建的查询路径与预期不符。

**修复方案**: 修正为两参数形式，如 `_tool_config_get "_defaults.unknown_tool" "color"`。

---

### 14. ~~[可移植性] 多个 Bash 特性在 macOS 默认环境下不可用~~ ✅ 已修复

| 特性 | 位置 | 要求 |
|------|------|------|
| `readlink -f` | `hook-router.sh:26`, `core.sh:57` | macOS 12.3+ 已原生支持，无兼容性问题 |
| `declare -A` | `tool.sh:39,53` | Bash 4+ |
| `mapfile` | `socket.sh:185`, `feishu.sh:1006,1113` | Bash 4+ |
| `base64 -w 0` | `permission.sh:156` | GNU base64 |

**修复方案**:
- `declare -A` 关联数组改为 `case` 函数（`_builtin_tool_color`、`_builtin_tool_field`）
- `mapfile -t` 改为 `while IFS= read -r` 循环
- `base64 -w 0` 改为 `base64 | tr -d '\n'`
- `readlink -f` 在 macOS 12.3+ 已原生支持，无需修改

---

### 15. ~~[配置] `reload_config()` 不更新已导出的模块级变量~~ 无需修复

**文件**: `src/server/config.py:224-278`

`PERMISSION_REQUEST_TIMEOUT`、`FEISHU_APP_ID` 等在模块加载时已固化，调用 `reload_config()` 后其他模块通过 `from config import PERMISSION_REQUEST_TIMEOUT` 获取的仍是旧值。

**说明**: `reload_config()` 当前无调用者，不存在实际风险。已在函数注释中说明此限制。

---

### 16. ~~[质量] `_run_in_background` 函数重复定义三次~~ ✅ 已修复

完全相同的代码分别出现在 `handlers/feishu.py`、`handlers/register.py`、`handlers/claude.py`。

**修复方案**: 提取到 `handlers/utils.py` 作为 `run_in_background()`，三个 handler 文件改为 `from .utils import run_in_background as _run_in_background`。

---

### 17. [质量] Store 类大量模板代码重复

5 个 Store 类的 `_load()`、`_save()`、`initialize()`、`get_instance()` 几乎完全相同。

**建议**: 抽取 `BaseJsonStore` 基类。

---

### 18. [质量] permission.sh 中两种模式函数代码大量重复

**文件**: `src/hooks/permission.sh`

`run_interactive_mode`（114-139 行）与 `run_fallback_mode`（204-228 行）有大量重复逻辑。

**建议**: 抽取公共函数处理共同的准备和发送逻辑。

---

### 19. [配置] `.env.example` 被 `.gitignore` 忽略

**文件**: `.gitignore:90`

`.env.example` 是配置模板，应纳入版本控制。当前新用户无法按 README 指引 `cp .env.example .env`，因为该文件根本不存在于仓库中。

---

### 20. [安全] 日志中记录完整原始数据

**文件**: `src/server/handlers/feishu.py:232-243`

飞书事件的 `raw_data` 完整写入日志，可能包含 token 等敏感信息。

**建议**: 只记录必要的字段，避免记录完整的 raw_data。

---

### 21. [安全] feishu.sh 中多处 JSON 手工拼接

**文件**: `src/lib/feishu.sh:751`、`src/hooks/webhook.sh:77`

`$session_id`、`$WEBHOOK_URL` 等变量未经 JSON 转义直接拼接到 JSON 字符串中。

**建议**: 使用 `json_build_object` 构建请求体。

---

### 22. [质量] feishu.sh 中 sed 转义链重复

**文件**: `src/lib/feishu.sh:154-180`

三处几乎完全相同的 `sed 's/\\/\\\\/g' | sed 's/"/\\"/g' | ...` 转义链。

**建议**: 抽取为 `_json_escape_sed()` 函数。

---

### 23. ~~[安全] Unix Socket 文件删除存在 TOCTOU 竞态~~ ✅ 已修复

**文件**: `src/server/main.py:204-209`

```python
if os.path.exists(SOCKET_PATH):
    os.unlink(SOCKET_PATH)
```

**修复方案**: 直接 `try: os.unlink() except FileNotFoundError: pass`，消除检查与删除之间的时间窗口。

---

## 三、低优先级问题

### 24. [规范] 环境变量读取方式不符规范

**文件**: `src/lib/core.sh:294`

使用 `${DEBUG:-0}` 读取配置，应改用 `get_config "DEBUG" "0"`。

---

### 25. [兼容] Python 2 兼容导入不必要

**文件**: `src/server/services/feishu_api.py:18-22`

`try: from urllib.request ... except: from urllib2 ...` 的 fallback 在 Python 3.6+ 环境下永远不会执行。

---

### 26. [质量] `json_build_object` 不做 JSON 转义

**文件**: `src/lib/json.sh:306-326`

值中含双引号或反斜杠时生成无效 JSON。至少应对 `\` 和 `"` 做基本转义。

---

### 27. [质量] `logging_config.py` 未被使用

**文件**: `src/shared/logging_config.py`

提供了统一日志配置，但 `main.py` 和 `socket_client.py` 各自硬编码配置，未引用此模块。

---

### 28. [质量] HTTP 响应发送代码重复

**文件**: `src/server/handlers/callback.py`

`do_POST` 每个路由分支都重复 `send_response/send_header/end_headers/wfile.write`。

**建议**: 提取 `send_json_response()` 辅助方法。

---

### 29. [风格] 变量命名和 SCRIPT_DIR 获取方式不统一

- 全局变量命名：`TOOLS_CONFIG_CACHE` vs `_ENV_FILE_CACHE`，风格不一致
- 路径解析：`hook-router.sh` 用 `readlink -f`，`start-server.sh` 用 `cd && pwd`（`readlink -f` 在 macOS 12.3+ 已原生支持，两种写法均可，但建议统一）

---

### 30. [类型] 多处类型注解不够精确

| 文件 | 问题 |
|------|------|
| `config.py:26` | `Optional[dict]` → 应为 `Optional[Dict[str, str]]` |
| `decision_handler.py:26` | 返回值类型应为 `Tuple[bool, Optional[str], Optional[str]]` |
| `callback.py:59` | `request_manager: RequestManager = None` → 应为 `Optional[RequestManager]` |

---

### 31. [质量] 裸 `except:` 吞掉所有异常

**文件**: `src/server/handlers/feishu.py:457`

```python
except:
    return ''
```

**建议**: 改为 `except Exception:`，避免捕获 `SystemExit`、`KeyboardInterrupt`。

---

### 32. [测试] 测试覆盖严重不足

仅有 2 个手动交互式测试脚本（均针对 PermissionRequest），缺少：

- Stop 事件测试
- Notification 事件测试
- Python 服务端单元测试
- Socket 通信测试
- JSON 解析降级测试

无自动化测试框架，无法集成 CI/CD。

---

### 33. [文档] README 多处与实际代码不一致

| 问题 | 说明 |
|------|------|
| 目录结构图遗漏文件 | `auth_token_store.py`、`session_chat_store.py`、`dir_history_store.py`、`tool_config.py`、`logging_config.py` 未列出 |
| `shared/logging.json` 描述错误 | 实际文件是 `logging_config.py`（Python 文件），非 JSON |
| 日志文件模式不完整 | 缺少 `log/feishu_message/`、`log/command/` 目录说明 |

---

### 34. [配置] `.gitignore` 规则冗余

- `*.sock` 在第 49 行和第 55 行重复
- `/tmp/claude-permission.sock` 已被 `*.sock` 覆盖

---

### 35. [运维] 根目录文件管理不规范

- `autossh-tunnel.sh`、`autossh-tunnel.conf` 未提交也未忽略
- `TODO.md`、`docs/QUICKSTART.md` 处于游离状态
- `nohup.out` 残留表明服务启动方式不够规范

---

## 统计汇总

| 严重程度 | 数量 | 已修复 | 接受风险/无需修复 | 待修复 | 主要类别 |
|---------|------|--------|------------------|--------|---------|
| **高** | 9 | 7 | 1 | 1 | 命令注入、XSS、路径遍历、数据安全、兼容性 |
| **中** | 14 | 5 | 1 | 8 | 监听配置、并发安全、逻辑错误、代码重复、文档脱节、TOCTOU |
| **低** | 12 | 0 | 0 | 12 | 规范合规、类型注解、测试覆盖、代码风格 |

> 注：高优先级 #5 (JSON手工拼接) 经评估后接受风险不修复；中优先级 #15 (`reload_config`) 经评估无需修复。

## 建议修复优先级

1. **立即修复**: 安全类问题（#2-#6, #10-#11, #20-#21）
2. **尽快修复**: 数据安全 (#8 原子写入)、兼容性 (#7 dataclass)、逻辑错误 (#13)
3. **计划修复**: 文档同步 (#9, #19, #33)、代码去重 (#16-#18, #22)、并发保护 (#12)
4. **持续改进**: 测试覆盖 (#32)、类型注解 (#30)、代码规范 (#24-#29, #31, #34-#35)
