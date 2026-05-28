# 飞书回复继续 Claude 会话方案

> 历史设计文档：本文记录早期 Claude-only 会话继续方案。当前实现已迁移到多 Agent 架构，实际接口以 `/cb/agent/continue` 和 `src/server/handlers/agent.py` 为准。

## 1. 需求概述

当 Claude 触发 Stop 事件后，`stop.sh` 发送一条完成消息到飞书。用户可以通过回复这条消息，在对应的 Claude Session 中继续发起提问。

## 2. 架构背景

本方案基于**分离模式**设计：

| 组件 | 职责 |
|------|------|
| **飞书网关** | 接收飞书事件、发送消息、维护映射表 |
| **Callback 后端** | 运行 Claude，可能有多个，每个对应不同的项目/机器 |

## 3. 核心问题分析

| 问题 | 描述 |
|------|------|
| **消息关联** | 如何知道用户引用回复了哪条消息 |
| **Session 映射** | 如何知道这条消息对应哪个 Claude Session、哪个 Callback 后端 |
| **消息注入** | 如何将用户的提问发送给 Claude 会话 |

## 4. 问题 1：检测用户回复了哪条消息

### 飞书机制

当用户回复一条消息时，`im.message.receive_v1` 事件的消息体中包含 `parent_id` 字段：

```json
{
  "event": {
    "message": {
      "message_id": "om_xxx",        // 当前消息 ID
      "parent_id": "om_yyy",          // 被回复的消息 ID (关键字段)
      "root_id": "om_yyy",            // 会话根消息 ID
      "chat_id": "oc_xxx",
      "content": "{\"text\":\"用户的回复内容\"}"
    }
  }
}
```

### 现有代码

`src/server/handlers/feishu.py` 的 `_handle_message_event` 已处理此事件，但目前只记录日志。

## 5. 问题 2：建立 message_id → Session 的映射

### 映射时机

发送消息时同步注册映射。

### 前提条件

本方案基于 **OpenAPI 模式**，发送消息时会返回 `message_id`。

现有代码 `feishu_api.py` 已经返回 message_id：

```python
message_id = resp.get('data', {}).get('message_id', '')
return True, message_id
```

### 映射存储

| 项目 | 说明 |
|------|------|
| **存储位置** | 飞书网关本地，JSON 文件 `runtime/message_sessions.json` |
| **过期时间** | 默认 7 天 |

### 映射表结构

```json
{
  "om_xxx": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "project_dir": "/home/user/project",
    "callback_url": "http://192.168.1.100:8080",
    "created_at": 1706745600
  }
}
```

| 字段 | 说明 |
|------|------|
| `session_id` | Claude 会话 ID |
| `project_dir` | 项目工作目录 |
| `callback_url` | Callback 后端地址 |
| `created_at` | 创建时间戳，用于过期清理 |

## 6. 问题 3：将提问发送给 Claude 会话

飞书网关收到回复消息后，调用对应 Callback 后端的 `/cb/claude/continue` 接口：

```bash
POST {callback_url}/cb/claude/continue
{
    "session_id": "550e8400-e29b...",
    "project_dir": "/home/user/project",
    "prompt": "用户的回复内容",
    "chat_id": "oc_xxx",
    "reply_message_id": "om_yyy"
}
```

Callback 后端执行：

```bash
cd <project_dir> && claude --print "<prompt>" --resume "<session_id>"
```

Claude 完成后，通过 Hook 事件自行决定发送结果到哪里。

## 7. 完整架构设计

### 7.1 注册映射流程（发送消息时）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Callback 后端 (stop.sh)                              │
│  Claude Stop 事件触发                                                        │
│  调用 send_feishu_card，传递 session_id, project_dir, callback_url          │
└─────────────────────────────────────────────────────────────────────────────┘
                                               │
                                               │ POST /gw/feishu/send
                                               │ {
                                               │   msg_type, content,
                                               │   session_id, project_dir,
                                               │   callback_url
                                               │ }
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              飞书网关                                         │
│  1. 调用飞书 OpenAPI 发送消息                                                 │
│  2. 获取返回的 message_id                                                     │
│  3. 保存映射: message_id → {session_id, project_dir, callback_url}           │
│  4. 返回 message_id 给 Callback                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                               │
                                               ▼
                                    ┌─────────────────────┐
                                    │     飞书消息        │
                                    │  "✅ Claude 完成"   │
                                    └─────────────────────┘
```

### 7.2 继续会话流程（用户回复时）

```
                                    ┌─────────────────────┐
                                    │     飞书消息        │
                                    │  "✅ Claude 完成"   │
                                    └──────────┬──────────┘
                                               │ 用户回复
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              飞书网关                                         │
│  im.message.receive_v1 事件:                                                 │
│  1. 提取 parent_id (被引用的消息 ID)                                          │
│  2. 查询映射表: parent_id → {session_id, project_dir, callback_url}          │
│  3. 提取用户回复内容                                                          │
│  4. 调用 Callback 后端                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                               │
                                               │ POST {callback_url}/cb/claude/continue
                                               │ {
                                               │   session_id, project_dir,
                                               │   prompt, chat_id,
                                               │   reply_message_id
                                               │ }
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Callback 后端                                        │
│  1. 接收请求                                                                 │
│  2. 执行: cd <project_dir> && claude --print "<prompt>" --resume <session_id> │
│  3. Claude 完成后，Hook 事件自行决定发送结果到哪里                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 8. 需要修改的文件

| 文件 | 修改内容 |
|------|----------|
| `src/hooks/stop.sh` | 传递 `session_id`、`project_dir`、`callback_url` |
| `src/lib/feishu.sh` | 请求体增加 `session_id`、`project_dir`、`callback_url` |
| `src/server/handlers/feishu.py` | 发送成功后保存映射；处理回复消息查映射并调用 Callback |
| `src/server/services/feishu_api.py` | `send_card` 增加参数，返回 message_id 后由 handler 保存映射 |
| `src/server/services/message_session_store.py` | **新增** 管理映射关系 |
| `src/server/handlers/claude.py` | **新增** `/cb/claude/continue` 接口 |
| `src/server/services/claude_invoker.py` | **新增** 封装 Claude 调用 |

## 9. 详细实现说明

### 9.1 注册映射数据流

```
stop.sh
  │
  │ 调用 send_feishu_card($CARD, $WEBHOOK_URL, $SESSION_ID, $PROJECT_DIR, $CALLBACK_URL)
  ▼
send_feishu_card()  [src/lib/feishu.sh]
  │
  │ 调用 _send_feishu_card_openapi(...)
  ▼
_send_feishu_card_openapi()  [src/lib/feishu.sh]
  │
  │ 构建请求体:
  │ {
  │   "msg_type": "interactive",
  │   "content": {...},
  │   "session_id": "xxx",
  │   "project_dir": "/path",
  │   "callback_url": "http://..."
  │ }
  │
  │ POST /gw/feishu/send
  ▼
handle_send_message()  [src/server/handlers/feishu.py]
  │
  │ 提取 session_id, project_dir, callback_url
  │ 调用 service.send_card(...)
  │ 成功后调用 MessageSessionStore.save(message_id, session_id, project_dir, callback_url)
  ▼
完成
```

### 9.2 各文件修改详情

#### 9.2.1 `src/hooks/stop.sh`

修改 `send_stop_notification_async` 函数，传递 session_id、project_dir 和 callback_url：

```bash
# 获取 callback_url (本机 Callback 服务地址)
CALLBACK_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")

# 当前代码
send_feishu_card "$CARD" "$WEBHOOK_URL" >/dev/null 2>&1

# 修改为
send_feishu_card "$CARD" "$WEBHOOK_URL" "$SESSION_ID" "$PROJECT_DIR" "$CALLBACK_URL" >/dev/null 2>&1
```

#### 9.2.2 `src/lib/feishu.sh`

**修改 `send_feishu_card` 函数签名**：

```bash
# 当前签名
send_feishu_card() {
    local card_json="$1"
    local webhook_url="${2:-...}"

# 修改为
send_feishu_card() {
    local card_json="$1"
    local webhook_url="${2:-...}"
    local session_id="${3:-}"
    local project_dir="${4:-}"
    local callback_url="${5:-}"
```

**修改 `_send_feishu_card_openapi` 函数**：

```bash
_send_feishu_card_openapi() {
    local card_json="$1"
    local session_id="${2:-}"
    local project_dir="${3:-}"
    local callback_url="${4:-}"
    ...

    local request_body
    if [ -n "$session_id" ] && [ -n "$project_dir" ] && [ -n "$callback_url" ]; then
        # 转义特殊字符
        local escaped_project_dir
        escaped_project_dir=$(echo "$project_dir" | sed 's/\\/\\\\/g; s/"/\\"/g')
        local escaped_callback_url
        escaped_callback_url=$(echo "$callback_url" | sed 's/\\/\\\\/g; s/"/\\"/g')

        request_body=$(jq -n \
            --arg msg_type "interactive" \
            --argjson content "$card_content" \
            --arg session_id "$session_id" \
            --arg project_dir "$project_dir" \
            --arg callback_url "$callback_url" \
            '{msg_type: $msg_type, content: $content, session_id: $session_id, project_dir: $project_dir, callback_url: $callback_url}')
    else
        request_body=$(jq -n \
            --arg msg_type "interactive" \
            --argjson content "$card_content" \
            '{msg_type: $msg_type, content: $content}')
    fi
```

#### 9.2.3 `src/server/handlers/feishu.py`

**修改 `handle_send_message` 函数**：

```python
def handle_send_message(data: dict) -> Tuple[bool, dict]:
    msg_type = data.get('msg_type')
    content = data.get('content')

    # 新增：提取 session 信息
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    callback_url = data.get('callback_url', '')

    ...

    if msg_type == 'interactive':
        ...
        success, message_id = service.send_card(card_json, receive_id, receive_id_type)

        # 新增：发送成功后保存映射
        if success and message_id and session_id and project_dir and callback_url:
            from services.message_session_store import MessageSessionStore
            store = MessageSessionStore.get_instance()
            if store:
                store.save(message_id, session_id, project_dir, callback_url)

        return success, {'message_id': message_id}
```

**扩展 `_handle_message_event` 函数处理回复消息**：

```python
def _handle_message_event(event: dict) -> Tuple[bool, dict]:
    message = event.get('message', {})
    parent_id = message.get('parent_id', '')

    # 检查是否是回复消息
    if parent_id:
        return _handle_reply_message(event, parent_id)

    # 原有逻辑...
    return True, {'status': 'ok'}


def _handle_reply_message(event: dict, parent_id: str) -> Tuple[bool, dict]:
    """处理用户回复消息，继续 Claude 会话"""
    from services.message_session_store import MessageSessionStore
    import requests

    # 查询映射
    store = MessageSessionStore.get_instance()
    if not store:
        return False, {'error': 'MessageSessionStore not initialized'}

    mapping = store.get(parent_id)
    if not mapping:
        # 不是回复我们发送的消息，忽略
        return True, {'status': 'ignored', 'reason': 'no mapping found'}

    # 提取回复内容
    message = event.get('message', {})
    content_str = message.get('content', '{}')
    try:
        content = json.loads(content_str)
        prompt = content.get('text', '')
    except json.JSONDecodeError:
        prompt = content_str

    if not prompt:
        return False, {'error': 'empty prompt'}

    # 调用 Callback 后端
    callback_url = mapping['callback_url']
    try:
        resp = requests.post(
            f"{callback_url}/cb/claude/continue",
            json={
                'session_id': mapping['session_id'],
                'project_dir': mapping['project_dir'],
                'prompt': prompt,
                'chat_id': message.get('chat_id', ''),
                'reply_message_id': message.get('message_id', '')
            },
            timeout=5
        )
        return resp.ok, resp.json()
    except Exception as e:
        return False, {'error': str(e)}
```

#### 9.2.4 新增 `src/server/services/message_session_store.py`

```python
"""Message-Session 映射存储"""

import json
import os
import threading
import time
from typing import Optional, Dict

class MessageSessionStore:
    """管理 message_id -> session 信息的映射"""

    _instance = None
    _lock = threading.Lock()

    # 过期时间（秒），默认 7 天
    EXPIRE_SECONDS = 7 * 24 * 3600

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, 'message_sessions.json')
        self._file_lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

    @classmethod
    def initialize(cls, data_dir: str) -> 'MessageSessionStore':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['MessageSessionStore']:
        return cls._instance

    def save(self, message_id: str, session_id: str, project_dir: str, callback_url: str) -> bool:
        """保存映射关系"""
        with self._file_lock:
            data = self._load()
            data[message_id] = {
                'session_id': session_id,
                'project_dir': project_dir,
                'callback_url': callback_url,
                'created_at': int(time.time())
            }
            return self._save(data)

    def get(self, message_id: str) -> Optional[Dict]:
        """获取映射关系，返回 None 表示不存在或已过期"""
        with self._file_lock:
            data = self._load()
            item = data.get(message_id)
            if not item:
                return None

            # 检查过期
            if time.time() - item.get('created_at', 0) > self.EXPIRE_SECONDS:
                del data[message_id]
                self._save(data)
                return None

            return item

    def cleanup_expired(self) -> int:
        """清理过期数据，返回清理数量"""
        with self._file_lock:
            data = self._load()
            now = time.time()
            expired = [
                k for k, v in data.items()
                if now - v.get('created_at', 0) > self.EXPIRE_SECONDS
            ]
            for k in expired:
                del data[k]
            if expired:
                self._save(data)
            return len(expired)

    def _load(self) -> Dict:
        if not os.path.exists(self._file_path):
            return {}
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self, data: Dict) -> bool:
        try:
            with open(self._file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except IOError:
            return False
```

#### 9.2.5 新增 `src/server/handlers/claude.py`

```python
"""Claude 会话相关接口"""

import subprocess
import threading
from typing import Tuple

def handle_continue_session(data: dict) -> Tuple[bool, dict]:
    """
    处理继续 Claude 会话的请求

    请求体:
    {
        "session_id": "xxx",
        "project_dir": "/path/to/project",
        "prompt": "用户的问题",
        "chat_id": "oc_xxx",           # 可选
        "reply_message_id": "om_xxx"   # 可选
    }
    """
    session_id = data.get('session_id', '')
    project_dir = data.get('project_dir', '')
    prompt = data.get('prompt', '')

    if not session_id or not project_dir or not prompt:
        return False, {'error': 'missing required fields'}

    # 异步执行 Claude，避免超时
    thread = threading.Thread(
        target=_run_claude_async,
        args=(session_id, project_dir, prompt),
        daemon=True
    )
    thread.start()

    return True, {'status': 'processing'}


def _run_claude_async(session_id: str, project_dir: str, prompt: str):
    """异步执行 Claude 命令"""
    try:
        cmd = ['claude', '--print', prompt, '--resume', session_id]
        subprocess.run(
            cmd,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=600  # 10 分钟超时
        )
    except Exception as e:
        # Claude 完成后会触发 Hook，由 Hook 发送结果
        # 这里只记录错误
        print(f"Claude execution error: {e}")
```

#### 9.2.6 路由注册

在 `src/server/app.py` 中注册新路由：

```python
from handlers.claude import handle_continue_session

@app.route('/cb/claude/continue', methods=['POST'])
def claude_continue():
    data = request.get_json() or {}
    success, result = handle_continue_session(data)
    status_code = 200 if success else 400
    return jsonify(result), status_code
```

#### 9.2.7 错误通知机制

当 Claude 命令执行异常时，Callback 后端会通过飞书发送错误通知（前提是请求中包含 `chat_id` 参数）：

| 场景 | 触发条件 | 通知内容 |
|------|----------|----------|
| **命令执行失败** | 进程退出码非 0 | stderr 内容（最多 500 字） |
| **执行超时** | 超过 10 分钟未完成 | "执行超时（超过 10 分钟）" |
| **其他异常** | 启动失败、执行错误等 | 异常消息（最多 500 字） |

**实现要点**：

- 异步等待进程完成，超时时间为 10 分钟
- 只在异常情况下发送通知，正常完成不通知
- 通知通过 `FeishuAPIService.send_text()` 发送到群聊
- 延迟导入 `FeishuAPIService` 以避免循环依赖

## 10. 潜在问题与解决方案

| 问题 | 影响 | 解决方案 |
|------|------|----------|
| **会话已过期/删除** | 恢复失败 | Claude 会返回错误，Hook 可以发送友好提示 |
| **长时间运行** | 飞书回调超时 | 异步执行 Claude，先返回 "processing" |
| **多人回复不同消息** | 不同 Session 并发 | 每次回复创建独立调用，不同 Session 互不影响 |
| **项目目录不存在** | 执行失败 | 执行前检查目录，返回错误提示 |
| **Callback 不可达** | 无法继续会话 | 飞书网关记录错误日志，可考虑重试机制 |

## 11. 已知问题

### 同一 Session 并发调用

**问题描述：**

当多人同时回复**同一条**飞书消息（即同一个 `message_id`，对应同一个 `session_id`）时，会同时发起多个 `claude --resume <session_id>` 调用。

**潜在影响：**

| 影响 | 描述 |
|------|------|
| **竞态条件** | 多个进程同时读写 `~/.claude/sessions/<session_id>.json` |
| **状态覆盖** | 后完成的请求会覆盖先完成请求的 Session 状态 |
| **对话历史混乱** | 可能导致对话历史丢失或交错 |
| **文件损坏** | 极端情况下可能导致 Session JSON 文件损坏 |

**当前状态：** 暂未处理，允许并发执行

**未来解决方案（待实现）：**

| 方案 | 复杂度 | 说明 |
|------|--------|------|
| **文件锁** | 低 | 执行前创建 `runtime/session_<id>.lock`，存在则拒绝或等待 |
| **内存锁** | 中 | 在 `MessageSessionStore` 中维护"正在使用的 Session"集合 |
| **任务队列** | 高 | 引入队列系统（如 Redis），严格排队处理 |

## 12. 配置项

### 12.1 Callback 服务地址

本方案复用现有配置 `CALLBACK_SERVER_URL`（已在权限通知场景中使用）：

```bash
# Callback 服务对外可访问的地址
# 用于权限按钮回调、会话继续回调等
CALLBACK_SERVER_URL=http://localhost:8080
```

在 `stop.sh` 中读取此配置传递给飞书网关：

```bash
CALLBACK_URL=$(get_config "CALLBACK_SERVER_URL" "http://localhost:8080")
send_feishu_card "$CARD" "$WEBHOOK_URL" "$SESSION_ID" "$PROJECT_DIR" "$CALLBACK_URL"
```

### 12.2 Claude 命令配置

通过 `CLAUDE_COMMAND` 环境变量可以自定义使用的 Claude 命令。

**默认值**：`claude`

**配置示例**：

```bash
# .env 文件中配置
# 使用默认 claude 命令
CLAUDE_COMMAND=claude

# 使用其他变体命令
CLAUDE_COMMAND=claude-glm

# 带参数的命令（需要用引号包裹）
CLAUDE_COMMAND="claude --model opus"
```

**支持 Shell 别名：**

命令通过登录 Shell（`bash -lc`）执行，会自动加载 Shell 配置文件（如 `~/.bashrc`），因此支持使用定义在配置文件中的别名：

```bash
# ~/.bashrc 中定义别名
alias claude-glm='claude --model opus'

# .env 中直接使用别名
CLAUDE_COMMAND=claude-glm
```

**适用场景**：

- 使用其他 Claude 变体（如 `claude-glm`）
- 指定模型参数（如 `--model opus`）
- 使用 Shell 配置文件中定义的别名
- 使用自定义脚本路径

**注意**：

- 配置带参数的命令时需要用引号包裹
- 命令会在项目工作目录下执行
- 继续会话和新建会话功能都会使用此配置

## 13. 实现优先级建议

### 第一阶段：映射注册

- 新增 `message_session_store.py`
- 修改 `stop.sh` 传递 session_id、project_dir、callback_url
- 修改 `feishu.sh` 在请求体中添加这些字段
- 修改 `feishu.py` 发送成功后保存映射

### 第二阶段：消息处理

- 扩展 `_handle_message_event` 识别回复消息
- 新增 `/cb/claude/continue` 接口
- 实现 Claude 异步调用

### 第三阶段：优化体验

- 错误处理和友好提示
- 过期清理机制
- 监控和日志
