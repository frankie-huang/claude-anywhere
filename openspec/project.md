# Project Context

## Purpose
claude-anywhere 是一个为 Claude Code 提供飞书通知机制的工具，支持可交互权限控制。当 Claude Code 发起权限请求时，通过飞书 Webhook 发送通知卡片，用户可以直接在飞书中点击按钮批准或拒绝请求，无需返回终端操作。

## Tech Stack
- **Shell Script**: Hook 脚本（Bash）
- **Python 3**: HTTP 回调服务器
- **Unix Socket**: 进程间通信
- **Feishu Card 2.0**: 飞书交互式卡片格式
- **Claude Code Hooks**: 权限请求和通知机制

## Project Conventions

### Code Style

#### Shell 脚本
- 使用 4 空格缩进
- 函数命名使用 `snake_case`，如 `send_feishu_card()`
- 变量命名使用 `UPPER_CASE`，如 `FEISHU_WEBHOOK_URL`
- 函数库统一放在 `shell-lib/` 目录
- 所有函数库必须提供 `source` 后的初始化检查（如 `json_init`）

#### Python 代码
- 遵循 PEP 8 规范
- 使用类型注解（type hints）
- 模块化分层架构：handlers → services → models
- 配置统一由 `config.py` 管理

### Architecture Patterns

#### 模块化 Shell 架构
- **函数库复用**: Shell 脚本抽离公共逻辑到 `shell-lib/*.sh`
- **配置驱动**: 工具类型配置集中在 `config/tools.json`，Shell 和 Python 共用
- **多级降级**: JSON 解析支持 jq → python3 → grep/sed 三级降级

#### 分层 Python 架构
- **handlers**: HTTP 请求处理（路由和参数解析）
- **services**: 业务逻辑（请求管理、规则写入等）
- **models**: 数据模型（Decision、ToolConfig 等）
- 关注点分离，各层解耦

#### 通信协议
- Unix Socket 双向通信，4 字节长度前缀协议
- 连接状态驱动请求有效性（非超时驱动）
- 协议规范文档化在 `shared/protocol.md`

### Testing Strategy

#### 测试方法
- 通过手动测试验证主要功能流程
- 使用环境变量模拟测试场景
- 关键测试场景：Socket 通信、权限流程、降级模式、飞书卡片格式

#### 测试环境变量
- `TEST_MODE`: 启用测试模式
- `MOCK_FEISHU`: 模拟飞书响应（避免真实发送）
- `TEST_SOCKET_PATH`: 使用测试专用 Socket 路径

### Git Workflow

#### 分支策略
- `main`: 主分支，保持稳定可发布状态
- 功能开发基于 `main` 创建分支
- 使用 OpenSpec 变更提案管理重大功能

#### Commit 约定
- Commit message 使用中文
- 格式: `<type>: <description>`
  - `feat`: 新功能
  - `fix`: 修复 bug
  - `refactor`: 重构
  - `docs`: 文档更新
  - `test`: 测试相关
- 示例: `feat: 添加飞书卡片模板系统`

#### OpenSpec 工作流
- 重大功能先创建变更提案 (`openspec change new`)
- 编写 proposal.md、design.md、tasks.md
- 实现完成后归档变更 (`openspec archive`)
- 归档后自动更新主规范

## Domain Context

### Claude Code Hooks 机制
Claude Code 支持 Hooks 机制，在特定事件触发时调用外部脚本：
- **PermissionRequest**: 权限请求时触发，输入包含 tool_name 和 tool_input
- **Notification**: 通用通知（任务暂停等）
- **Stop**: 任务停止时触发

Hook 脚本通过 stdout 返回决策 JSON：
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow|deny",
      "message": "可选消息",
      "interrupt": false
    }
  }
}
```

### 飞书卡片 2.0 格式
飞书交互式卡片使用 JSON 格式，核心元素：
- `schema`: 版本号（2.0）
- `config`: 全局配置（update_multi、style）
- `body.elements`: 卡片内容（column_set、markdown、div 等）
- `actions`: 操作按钮（使用 action_btn 元素）

详见 `openspec/specs/feishu-card-format/spec.md`

## Important Constraints

### Hook 超时限制
- PermissionRequest hook 默认超时 60 秒
- 脚本需在超时前返回决策，否则 Claude Code 继续执行
- 实现时使用 55 秒内部超时（预留缓冲）

### 环境依赖最小化
- 降级模式仅需 `curl`
- 可交互模式推荐 `python3` 和 `socat`
- `jq` 为可选依赖（脚本自动降级）

### 错误处理不阻塞
- 任何错误都不能阻塞 Claude Code 正常运行
- Webhook 失败、Socket 断开等场景都需优雅降级
- 脚本必须以退出码 0 结束

### 兼容性要求
- 支持 Linux 和 macOS
- Python 3.6+ 兼容
- Bash 4.0+ 兼容

## External Dependencies

### 飞书 Webhook API
- **用途**: 发送卡片通知
- **限制**: 需配置有效的 Webhook URL
- **文档**: https://open.feishu.cn/document/common-capabilities/message-card/message-cards-content

### Claude Code Hooks
- **用途**: 权限请求和通知触发
- **配置**: `~/.claude/settings.json`
- **文档**: Claude Code 官方文档

### Unix Domain Socket
- **用途**: permission-notify.sh 与回调服务通信
- **路径**: `/tmp/claude-permission.sock`（可配置）
- **协议**: 长度前缀二进制协议（详见 `shared/protocol.md`）
