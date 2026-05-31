# Claude Permission Socket Protocol v1

## 概述

基于 Unix Domain Socket 的请求-响应协议，用于 Hook 脚本与回调服务器通信。

## 连接参数

| 参数 | 值 |
|------|------|
| Socket 类型 | SOCK_STREAM |
| 默认路径 | /tmp/claude-permission.sock |
| 环境变量 | PERMISSION_SOCKET_PATH |

## 消息序列

```
Client (permission-notify.sh)           Server (callback server)
         |                                        |
         |------ Request JSON ------------------>|
         |                                        |
         |<----- Ack JSON (无长度前缀) ----------|
         |                                        |
         |       [等待用户通过 HTTP 响应]         |
         |                                        |
         |<----- Decision (4B长度+JSON) ---------|
         |                                        |
```

## 消息格式

### 1. 请求消息 (Client → Server)

- **格式**: 原始 JSON, UTF-8 编码
- **长度前缀**: 无
- **发送方式**: 一次性发送完整 JSON

**字段说明**:

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| request_id | string | 是 | 唯一请求标识 (32 位随机字符) |
| project_dir | string | 是 | 项目目录路径 |
| raw_input_encoded | string | 是 | Base64 编码的原始 PermissionRequest 输入（包含 session_id、tool_name、tool_input） |
| agent_type | string | 否 | Agent 类型，`claude` 或 `codex`，用于 always 规则持久化分流 |

**raw_input_encoded 解码后的字段**:

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| session_id | string | 否 | Claude Code 会话 ID，用于追踪请求来源 |
| tool_name | string | 是 | 工具名称（如 Bash、Write 等） |
| tool_input | object | 是 | 工具输入参数 |
| permission_suggestions | array | 否 | Claude 提供的权限规则建议；Claude always 决策会作为 `updatedPermissions` 返回 |

**示例**:
```json
{
  "request_id": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
  "project_dir": "/home/user/myproject",
  "raw_input_encoded": "eyJzZXNzaW9uX2lkIjoic2Vzc18xMjMiLCJ0b29sX25hbWUiOiJCYXNoIiwidG9vbF9pbnB1dCI6eyJjb21tYW5kIjoiZWNobyBoZWxsbyJ9fQ==",
  "agent_type": "claude"
}
```

其中 `raw_input_encoded` 解码后为：
```json
{
  "session_id": "sess_123",
  "tool_name": "Bash",
  "tool_input": {"command": "echo hello"}
}
```

### 2. 确认消息 (Server → Client)

- **格式**: 原始 JSON, UTF-8 编码
- **长度前缀**: 无
- **时机**: 请求注册成功后立即发送

**JSON 字段说明**:

| 字段 | 类型 | 说明 |
|------|------|------|
| success | boolean | 请求是否成功注册 |
| message | string | 状态消息 |
| session_id | string | 会话 ID（来自请求的 raw_input_encoded） |

**示例**:
```json
{
  "success": true,
  "message": "Request registered",
  "session_id": "sess_123"
}
```

### 3. 决策消息 (Server → Client)

- **格式**: 4字节大端序长度 + JSON 数据
- **长度字段**: uint32, big-endian
- **时机**: 用户通过 HTTP 回调做出决策后发送

**消息结构**:
```
+------------------+------------------+
| Length (4 bytes) |   JSON Payload   |
|   big-endian     |    (UTF-8)       |
+------------------+------------------+
```

**JSON 字段说明**:

| 字段 | 类型 | 说明 |
|------|------|------|
| success | boolean | 请求是否成功处理 |
| session_id | string | 会话 ID（用于追踪请求来源） |
| decision.behavior | string | "allow" 或 "deny" |
| decision.message | string | 决策原因说明 |
| decision.interrupt | boolean | 是否中断 Claude (仅 deny 时有效) |
| fallback_to_terminal | boolean | 是否回退到终端交互 (可选) |
| error | string | 错误类型（仅失败时有值） |
| message | string | 错误/状态消息（仅失败时有值） |

**成功响应示例**:
```json
{
  "success": true,
  "session_id": "sess_123",
  "decision": {
    "behavior": "allow"
  }
}
```

**拒绝响应示例**:
```json
{
  "success": true,
  "session_id": "sess_123",
  "decision": {
    "behavior": "deny",
    "message": "用户通过飞书拒绝",
    "interrupt": false
  }
}
```

**超时/回退响应示例**:
```json
{
  "success": false,
  "fallback_to_terminal": true,
  "error": "server_timeout",
  "session_id": "sess_123",
  "message": "服务器超时（300秒），请在终端操作"
}
```

## 超时配置

| 配置项 | 默认值 | 环境变量 | 说明 |
|--------|--------|----------|------|
| 服务端超时 | 600s | PERMISSION_REQUEST_TIMEOUT | 等待用户决策的最大时间 |
| 客户端超时 | 630s | - | 服务端超时 + 30s 缓冲 |

客户端超时设计为比服务端大，确保服务端先触发超时并发送回退响应。

## 错误处理

### 连接错误
- 服务端未运行: 客户端检测 socket 文件不存在，回退到降级模式
- 连接被拒绝: 返回 `{"success": false, "error": "connection_refused"}`

### 通信错误
- 读取长度失败: 连接意外关闭
- 响应不完整: 数据传输中断
- 客户端超时: 超过 330 秒无响应

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-01-18 | 初始版本 |
| v1.1 | 2026-01-26 | 新增 session_id 字段用于请求追踪，所有日志输出包含 session_id |
| v1.2 | 2026-02-04 | request_id 格式从 {timestamp}-{uuid8} 改为 32 位随机字符，提升不可预测性 |
