# Permission Notify 测试脚本

## 快速开始

### 方式一：快捷菜单测试（推荐）

```bash
./test/test-permission-quick.sh
```

提供交互式菜单，预设常见测试场景：
- Bash 普通命令
- Bash 文件操作
- Bash 网络请求
- Bash 包管理器
- Bash Git 操作
- Edit 文件编辑
- Write 文件写入
- Read 文件读取
- 自定义测试

### 方式二：命令行直接测试

```bash
# 基本用法（默认 Bash）
./test/test-permission.sh

# 指定工具和命令
./test/test-permission.sh bash "rm -rf /tmp/test"

# Bash 命令
./test/test-permission.sh bash "npm install"
./test/test-permission.sh bash "git push origin main"

# Edit 文件编辑
./test/test-permission.sh edit "/etc/hosts"
./test/test-permission.sh edit ".env"

# Write 文件写入
./test/test-permission.sh write "/tmp/test.txt" "测试内容"

# Read 文件读取
./test/test-permission.sh read "/etc/passwd"
```

## 测试前准备

### 1. 配置环境变量（可选）

在项目根目录的 `.env` 文件中配置：

```bash
# 飞书 Webhook URL（必需，否则会回退到终端模式）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 回调服务地址（可选，默认 http://localhost:8080）
CALLBACK_SERVER_URL=http://localhost:8080
```

### 2. 启动回调服务（交互模式需要）

```bash
./src/start-server.sh start
```

## 测试模式说明

### 交互模式
- **条件**: 回调服务运行中
- **行为**: 发送带按钮的飞书卡片，等待用户点击决策
- **结果**: 返回 allow/deny 决策

### 降级模式
- **条件**: 回调服务未运行
- **行为**: 发送普通飞书通知卡片
- **结果**: 返回 `EXIT_FALLBACK (1)`，Claude Code 回退到终端交互

## 退出码说明

| 退出码 | 含义 |
|--------|------|
| 0 | `EXIT_HOOK_SUCCESS` - Hook 成功，使用输出决策 |
| 1 | `EXIT_FALLBACK` - 回退到终端交互模式 |
| 2 | `EXIT_HOOK_ERROR` - Hook 执行错误 |

## 示例输出

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[INFO] 测试配置
  工具类型: bash
  命令/路径: rm -rf /tmp/test
  项目目录: /root/claude/claude-anywhere
  Session ID: test-session-1737812345
  时间: 2025-01-25 10:10:45
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[INFO] 生成测试 JSON (符合 Claude Code PermissionRequest hook 格式)...
{
  "session_id": "test-session-1737812345",
  "transcript_path": "/tmp/claude-transcript.jsonl",
  "cwd": "/root/claude/claude-anywhere",
  "permission_mode": "default",
  "hook_event_name": "PermissionRequest",
  "tool_name": "Bash",
  "tool_input": {
    "command": "rm -rf /tmp/test",
    "description": "rm -rf /tmp/test",
    "timeout": 120000
  },
  "tool_use_id": "toolu_abc123..."
}

> 注: `tool_use_id` 是测试脚本生成的模拟字段，目前 PermissionRequest 不提供此字段（未来可能支持）。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[INFO] 执行 permission-notify.sh...
...
```

## 更多测试文档

- **[PROMPTS.md](./PROMPTS.md)** - 权限请求测试指令集，包含各种测试提示词
- **[SCENARIOS.md](./SCENARIOS.md)** - 详细测试场景文档，包含各种测试场景和预期行为
