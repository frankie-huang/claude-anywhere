# 测试文档 - claude-anywhere

本文档描述 claude-anywhere 项目的各种测试场景和预期行为。

## 测试环境准备

### 前置条件

1. 安装必要依赖：
```bash
# Ubuntu/Debian
apt-get install python3 curl socat jq

# CentOS/RHEL
yum install python3 curl socat jq
```

2. 配置飞书 Webhook：
```bash
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx"
```

3. 配置 Claude Code Hook（测试项目）：
```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash /root/claude/claude-anywhere/src/hook-router.sh"
          }
        ]
      }
    ]
  }
}
```

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `FEISHU_WEBHOOK_URL` | (必需) | 飞书机器人 Webhook URL |
| `CALLBACK_SERVER_URL` | `http://localhost:8080` | 回调服务外部访问地址 |
| `CALLBACK_SERVER_PORT` | `8080` | 回调服务 HTTP 端口 |
| `PERMISSION_SOCKET_PATH` | `/tmp/claude-permission.sock` | Unix Socket 路径 |
| `PERMISSION_REQUEST_TIMEOUT` | `600` | 服务器端超时秒数（0=禁用） |

---

## 测试场景

### 场景 1: 后端服务未启动

#### 测试步骤

1. 确保回调服务未运行：
```bash
# 检查 Socket 文件不存在
ls -l /tmp/claude-permission.sock
# 输出: ls: cannot access '/tmp/claude-permission.sock': No such file or directory

# 检查 HTTP 端口未监听
lsof -i :8080
# 输出: (空)
```

2. 触发一个权限请求（如执行 Bash 命令）

#### 预期行为

- ✅ 发送**不带交互按钮**的飞书通知卡片
- ✅ 卡片底部显示提示："回调服务未运行，请返回终端操作"
- ✅ 脚本返回退出码 `1` (EXIT_FALLBACK)
- ✅ Claude Code 回退到终端交互模式
- ✅ 用户在终端看到权限请求提示

#### 验证方法

查看日志：
```bash
# 检查脚本日志
tail -20 log/hook/$(date +%Y-%m-%d).log

# 预期看到类似输出：
# [timestamp] Callback service not available
# [timestamp] Running in fallback mode (notification only)
```

---

### 场景 2: 后端服务正常运行，用户正常批准

#### 测试步骤

1. 启动回调服务：
```bash
./src/start-server.sh start
```

2. 验证服务运行：
```bash
# 检查 Socket 文件存在
ls -l /tmp/claude-permission.sock

# 检查 HTTP 端口监听
lsof -i :8080 | grep LISTEN

# 查看 HTTP 状态
curl -s --noproxy '*' -H "X-Auth-Token: $(python3 -c 'import json;print(json.load(open("runtime/auth_token.json"))["auth_token"])')" http://127.0.0.1:8080/status | python3 -m json.tool
```

3. 触发一个权限请求

4. 在飞书中点击"批准运行"按钮

#### 预期行为

- ✅ 发送**带 4 个按钮**的飞书交互卡片
- ✅ 脚本通过 Socket 注册请求并等待
- ✅ 用户点击按钮后，浏览器显示"已批准运行"页面
- ✅ 脚本接收到决策并输出 JSON 给 Claude Code
- ✅ Claude Code 继续执行操作

#### 预期输出格式

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow"
    }
  }
}
```

#### 验证方法

```bash
# 查看回调服务日志
tail -30 log/callback/$(date +%Y-%m-%d).log

# 预期看到类似输出：
# [timestamp] [socket] New connection, fileno=X
# [timestamp] [socket] Request ID: xxx-xxx
# [timestamp] [socket] Request xxx-xxx registered, waiting for user response...
# [timestamp] [resolve] Request xxx-xxx, status=pending, age=X.Xs
# [timestamp] [resolve] Sending X bytes to socket...
# [timestamp] [resolve] Request xxx-xxx resolved: allow
```

---

### 场景 3: 服务端超时（PERMISSION_REQUEST_TIMEOUT=300s），用户在超时前操作

#### 测试步骤

1. 设置默认超时（或确认使用默认值 300s）：
```bash
export PERMISSION_REQUEST_TIMEOUT=300
./src/start-server.sh restart
```

2. 触发一个权限请求

3. 在 300 秒内点击飞书按钮（如 60 秒后点击）

#### 预期行为

- ✅ 用户操作正常完成
- ✅ 脚本输出 allow/deny 决策
- ✅ Claude Code 继续或停止执行

#### 验证方法

```bash
# 查看日志确认操作时间
grep "Request.*resolved" log/callback/$(date +%Y-%m-%d).log

# 输出示例：
# 2025-01-17 10:05:00.123 [INFO] [socket] Request 123-abc registered, waiting for user response...
# 2025-01-17 10:06:00.456 [INFO] [resolve] Request 123-abc, age=60.3s  # 小于 300s
# 2025-01-17 10:06:00.789 [INFO] [resolve] Request 123-abc resolved: allow
```

---

### 场景 4: 服务端超时（PERMISSION_REQUEST_TIMEOUT=300s），用户超过超时时间操作

#### 测试步骤

1. 设置较短的超时用于测试（如 60 秒）：
```bash
export PERMISSION_REQUEST_TIMEOUT=60
./src/start-server.sh restart
```

2. 触发一个权限请求

3. 等待超过 60 秒后点击飞书按钮

#### 预期行为

**服务端行为**：
- ✅ 60 秒后发送"回退终端"响应到 Socket
- ✅ 关闭 Socket 连接
- ✅ 请求状态标记为 `disconnected`

**脚本行为**：
- ✅ 接收到 `fallback_to_terminal: true` 的响应
- ✅ 返回退出码 `1` (EXIT_FALLBACK)
- ✅ Claude Code 回退到终端交互模式

**飞书按钮行为**：
- ✅ 用户点击按钮后显示："处理失败：连接已断开，Claude 可能已超时或取消"

#### 验证方法

```bash
# 查看回调服务日志
tail -50 log/callback/$(date +%Y-%m-%d).log | grep -A 5 "fallback"

# 预期看到类似输出：
# 2025-01-17 10:05:00.123 [INFO] [socket] Request 123-abc registered, waiting for user response...
# 2025-01-17 10:06:00.456 [INFO] Sent fallback response to 123-abc (age=60s)
# 2025-01-17 10:06:30.789 [WARNING] [resolve] Request 123-abc not found  # 用户已超时
```

---

### 场景 5: Claude hook 超时 < 服务端超时

#### 背景

Claude Code 的 hook 超时时间由 Claude 内部控制（通常较短，约 10-30 秒），而服务端超时默认为 300 秒。

#### 测试步骤

1. 使用默认服务端超时（300s）：
```bash
export PERMISSION_REQUEST_TIMEOUT=300
./src/start-server.sh restart
```

2. 触发一个权限请求

3. **不点击任何按钮**，等待 Claude 超时（约 10-30 秒）

#### 预期行为

**Claude Code 层面**：
- ✅ Claude 在内部超时后取消 hook 进程
- ✅ 显示错误信息或重试提示

**服务端层面**：
- ✅ 清理线程检测到 Socket 连接已断开（Claude 进程被杀死）
- ✅ 请求状态标记为 `disconnected`
- ✅ Socket 连接被清理

**脚本行为**：
- ✅ 进程被 Claude 终止（可能未正常退出）

#### 验证方法

```bash
# 查看清理线程日志
grep "Cleaned up dead connection" log/callback/$(date +%Y-%m-%d).log

# 预期看到类似输出：
# 2025-01-17 10:05:00.123 [INFO] [socket] Request 123-abc registered, waiting for user response...
# 2025-01-17 10:05:20.456 [INFO] Cleaned up dead connection: 123-abc (age=20s)  # 远小于 300s
```

---

### 场景 6: Claude hook 超时 > 服务端超时

#### 说明

此场景**理论不可能发生**，因为：
1. Claude 的 hook 超时通常在 10-30 秒范围
2. 服务端超时默认为 300 秒
3. 服务端超时可配置为 0（禁用），但不能小于 Claude 超时

#### 如果强制测试（使用极短的服务端超时）

1. 设置极短的服务端超时（如 5 秒）：
```bash
export PERMISSION_REQUEST_TIMEOUT=5
./src/start-server.sh restart
```

2. 触发权限请求

3. 等待 5 秒

#### 预期行为（同场景 4）

- ✅ 5 秒后服务端发送"回退终端"响应
- ✅ 脚本返回 EXIT_FALLBACK
- ✅ Claude 回退到终端交互模式

---

### 场景 7: 禁用服务端超时（PERMISSION_REQUEST_TIMEOUT=0）

#### 测试步骤

1. 禁用超时：
```bash
export PERMISSION_REQUEST_TIMEOUT=0
./src/start-server.sh restart
```

2. 验证配置：
```bash
curl -s --noproxy '*' -H "X-Auth-Token: $(python3 -c 'import json;print(json.load(open("runtime/auth_token.json"))["auth_token"])')" http://127.0.0.1:8080/status | python3 -m json.tool
# 预期输出包含: "mode": "socket-based (no server-side timeout)"
```

3. 触发权限请求

4. 等待任意长时间后点击按钮

#### 预期行为

- ✅ 服务端**永不超时**
- ✅ 只要 Socket 连接存活（Claude 未超时），用户随时可以操作
- ✅ 实际受限于 Claude 的 hook 超时（通常 10-30 秒）

#### 验证方法

```bash
# 查看日志确认超时禁用
grep "Request Timeout" log/callback/$(date +%Y-%m-%d).log | head -1

# 预期输出：
# 2025-01-17 10:00:00.000 [INFO] Request Timeout: 0s (disabled)
```

---

### 场景 8: 用户点击"拒绝运行"

#### 测试步骤

1. 确保回调服务运行

2. 触发权限请求

3. 点击"拒绝运行"按钮

#### 预期行为

- ✅ 浏览器显示"已拒绝运行"页面
- ✅ 脚本输出 deny 决策

#### 预期输出

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "deny",
      "message": "用户通过飞书拒绝"
    }
  }
}
```

- ✅ Claude 收到 deny，可能尝试其他方式继续工作

---

### 场景 9: 用户点击"拒绝并中断"

#### 测试步骤

1. 确保回调服务运行

2. 触发权限请求

3. 点击"拒绝并中断"按钮

#### 预期行为

- ✅ 浏览器显示"已拒绝并中断"页面
- ✅ 脚本输出 deny 决策（带 interrupt=true）

#### 预期输出

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "deny",
      "message": "用户通过飞书拒绝并中断",
      "interrupt": true
    }
  }
}
```

- ✅ Claude 停止当前任务

---

### 场景 10: 用户点击"始终允许"

#### 测试步骤

1. 确保回调服务运行

2. 触发权限请求（如执行 `npm run build`）

3. 点击"始终允许"按钮

#### 预期行为

- ✅ 浏览器显示"已始终允许"页面
- ✅ 脚本输出 allow 决策
- ✅ 服务端写入权限规则到项目 `.claude/settings.local.json`

#### 验证方法

```bash
# 检查规则文件
cat .claude/settings.local.json

# 预期输出：
{
  "permissions": {
    "allow": [
      "Bash(npm run build)"
    ]
  }
}

# 查看服务日志
grep "Added always-allow rule" log/callback/$(date +%Y-%m-%d).log
```

- ✅ 后续相同操作自动允许，不再询问

---

### 场景 11: 用户重复点击按钮

#### 测试步骤

1. 触发权限请求

2. 点击"批准运行"

3. 再次点击同一请求的按钮（如果卡片仍可操作）

#### 预期行为

- ✅ 第一次点击：显示"已批准运行"，请求标记为 `resolved`
- ✅ 第二次点击：显示"处理失败：请求已被批准，请勿重复操作"

#### 验证方法

```bash
# 查看服务日志
grep -A 2 "Already resolved" log/callback/$(date +%Y-%m-%d).log
```

---

### 场景 12: Socket 连接中断（网络/进程问题）

#### 测试步骤

1. 触发权限请求

2. 在用户未操作前，手动杀掉回调服务：
```bash
./src/start-server.sh stop
```

3. 尝试点击飞书按钮

#### 预期行为

- ✅ Socket 文件被删除
- ✅ 脚本端连接被关闭
- ✅ 点击按钮显示："处理失败：连接已断开"
- ✅ 服务日志显示："Connection closed by client" 或类似错误

#### 验证方法

```bash
# 查看 Socket 客户端日志
tail -30 /tmp/socket-client-debug.log

# 预期看到类似输出：
# [timestamp] Connection closed while reading ack
```

---

### 场景 13: 飞书 Webhook 发送失败

#### 测试步骤

1. 设置无效的 Webhook URL：
```bash
export FEISHU_WEBHOOK_URL="https://invalid-url.example.com/hook"
```

2. 启动回调服务

3. 触发权限请求

#### 预期行为

- ✅ 脚本记录错误日志
- ✅ Socket 通信正常进行（如果服务运行）
- ✅ 用户无法收到飞书通知，但可通过终端操作
- ✅ 如果是交互模式，用户需要手动在终端操作（因为收不到通知）

#### 验证方法

```bash
# 查看脚本日志
grep "Failed to send Feishu card" log/hook/$(date +%Y-%m-%d).log
```

---

### 场景 14: 多个并发权限请求

#### 测试步骤

1. 启动回调服务

2. 同时触发多个权限请求（如多个 Claude 实例或快速连续操作）

3. 分别处理各个请求

#### 预期行为

- ✅ 每个请求获得唯一的 request_id
- ✅ 每个请求独立处理，互不干扰
- ✅ 服务日志正确记录各个请求的状态

#### 验证方法

```bash
# 查看服务状态
curl -s --noproxy '*' -H "X-Auth-Token: $(python3 -c 'import json;print(json.load(open("runtime/auth_token.json"))["auth_token"])')" http://127.0.0.1:8080/status | python3 -m json.tool

# 预期输出包含多个 pending 请求：
{
  "status": "ok",
  "pending": 2,
  "resolved": 0,
  "disconnected": 0,
  "requests": {
    "123-abc": {"status": "pending", "age_seconds": 10, "session": "sess_123", "tool": "Bash"},
    "456-def": {"status": "pending", "age_seconds": 5, "session": "sess_456", "tool": "Edit"}
  }
}
```

---

### 场景 15: 回调服务地址配置错误

#### 测试步骤

1. 设置错误的 CALLBACK_SERVER_URL：
```bash
export CALLBACK_SERVER_URL="http://wrong-server:9999"
```

2. 启动回调服务（监听在正确的地址）

3. 触发权限请求

4. 收到飞书卡片后点击按钮

#### 预期行为

- ✅ 飞书卡片按钮链接指向错误地址
- ✅ 点击按钮后浏览器显示"无法连接"或 DNS 错误
- ✅ Claude 端等待超时后回退到终端交互

#### 解决方法

修正 CALLBACK_SERVER_URL 为正确的地址。

---

## 压力测试

### 测试 1: 快速连续请求

#### 步骤

```bash
# 脚本测试快速连续请求
for i in {1..10}; do
  ./test/test-permission.sh bash "echo test$i"
  sleep 0.5
done
```

#### 预期行为

- ✅ 所有请求都能正确注册
- ✅ 服务端日志显示所有请求 ID
- ✅ 无内存泄漏或连接泄漏

---

### 测试 2: 长时间运行稳定性

#### 步骤

1. 启动回调服务

2. 每隔 5 分钟触发一次权限请求

3. 运行 24 小时

#### 预期行为

- ✅ 服务稳定运行，无崩溃
- ✅ 日志文件正常轮转
- ✅ 内存使用稳定

---

## 边界条件测试

### 测试 1: 特殊字符处理

#### 步骤

触发包含特殊字符的命令：

```bash
# Bash 命令包含引号、换行等
echo 'this is a "test" with $(special) chars'

# 文件路径包含空格
touch "/tmp/test file with spaces.txt"
```

#### 预期行为

- ✅ 特殊字符正确转义
- ✅ JSON 解析成功
- ✅ 飞书卡片正确显示

---

### 测试 2: 超长命令

#### 步骤

触发超长 Bash 命令（>500 字符）

#### 预期行为

- ✅ 命令在飞书卡片中被截断为 500 字符并添加 "..."
- ✅ 日志中记录完整命令
- ✅ Socket 通信正常

---

## 日志分析

### 关键日志模式

#### 正常流程

```
# 脚本日志 (log/hook/YYYY-MM-DD.log)
[timestamp] Tool: Bash, Project: /path/to/project
[timestamp] Request ID: 123-abc
[timestamp] Running in interactive mode
[timestamp] Sending interactive Feishu card
[timestamp] Feishu card sent successfully
[timestamp] Sending request to callback server
[timestamp] Socket client exit code: 0
[timestamp] Received response: {"success":true,"decision":...}
[timestamp] Outputting decision: behavior=allow, ...
[timestamp] Decision output complete

# 服务日志 (log/callback/YYYY-MM-DD.log)
[timestamp] [socket] New connection, fileno=X
[timestamp] [socket] Request ID: 123-abc
[timestamp] [socket] Request 123-abc registered, waiting for user response...
[timestamp] [resolve] Request 123-abc, status=pending, age=XX.Xs
[timestamp] [resolve] Sending XX bytes to socket...
[timestamp] [resolve] Request 123-abc resolved: allow
```

#### 超时流程

```
# 服务日志
[timestamp] [socket] Request 123-abc registered, waiting for user response...
[timestamp] Sent fallback response to 123-abc (age=300s)

# 脚本日志
[timestamp] Received response: {"success":false,"fallback_to_terminal":true,...}
[timestamp] Server timeout, falling back to terminal interaction (card already sent)
```

#### 连接断开

```
# 服务日志
[timestamp] Cleaned up dead connection: 123-abc (age=20s)

# 客户端日志 (/tmp/socket-client-debug.log)
[timestamp] Connection closed while reading ack
```

---

## 故障排查清单

| 问题 | 检查项 | 解决方法 |
|------|--------|----------|
| 收不到飞书通知 | `FEISHU_WEBHOOK_URL` 是否设置 | 检查环境变量 |
| 收不到飞书通知 | Webhook URL 是否正确 | 测试 Webhook URL |
| 收不到飞书通知 | 脚本日志是否有错误 | 查看 `log/hook/*.log` |
| 按钮点击无响应 | 回调服务是否运行 | `lsof -i :8080` |
| 按钮点击无响应 | Socket 文件是否存在 | `ls -l /tmp/claude-permission.sock` |
| 按钮点击无响应 | `CALLBACK_SERVER_URL` 是否正确 | 检查配置 |
| 总是回退到终端 | Socket 连接是否成功 | 查看客户端日志 |
| 总是回退到终端 | 服务是否超时 | 检查 `PERMISSION_REQUEST_TIMEOUT` |
| 规则未写入 | 项目目录是否正确 | 检查 `project_dir` 字段 |
| 规则未写入 | `.claude` 目录权限 | 检查目录权限 |

---

## 测试检查清单

### 基础功能

- [ ] 后端服务未启动时正确降级
- [ ] 后端服务运行时交互模式正常
- [ ] 批准运行功能正常
- [ ] 拒绝运行功能正常
- [ ] 拒绝并中断功能正常
- [ ] 始终允许功能正常且规则写入成功
- [ ] 重复点击正确处理

### 超时处理

- [ ] 服务端超时后正确回退到终端
- [ ] Claude 超时后连接正确清理
- [ ] 禁用超时时功能正常

### 异常处理

- [ ] Socket 连接中断正确处理
- [ ] 飞书 Webhook 失败不影响核心功能
- [ ] 无效请求 ID 正确处理
- [ ] 特殊字符正确转义

### 并发和性能

- [ ] 多个并发请求正确处理
- [ ] 快速连续请求无泄漏
- [ ] 长时间运行稳定

### 日志和调试

- [ ] 所有关键操作有日志记录
- [ ] 错误日志包含足够信息
- [ ] 日志级别合适（不过多/过少）
