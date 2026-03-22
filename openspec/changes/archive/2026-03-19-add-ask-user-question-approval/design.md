## Context

Claude Code 的 AskUserQuestion 工具通过 PermissionRequest hook 触发，要求用户从预设选项中选择或自定义输入回答。当前实现直接回退到终端，不支持远程审批。

社区已验证：通过 PermissionRequest hook 返回带 `updatedInput.answers` 的响应，可以绕过终端交互直接传回用户答案。

### 实际数据格式示例

**单个问题 - 单选**：
```json
{
  "session_id": "xxx",
  "hook_event_name": "PermissionRequest",
  "tool_name": "AskUserQuestion",
  "tool_input": {
    "questions": [{
      "question": "你今天想喝什么饮料？",
      "header": "饮料选择",
      "options": [
        {"label": "咖啡", "description": "经典美式或拿铁，提神醒脑"},
        {"label": "奶茶", "description": "珍珠奶茶，甜蜜快乐"},
        {"label": "果汁", "description": "鲜榨橙汁，健康清爽"}
      ],
      "multiSelect": false
    }]
  }
}
```

**单个问题 - 多选**：
```json
{
  "tool_input": {
    "questions": [{
      "question": "你喜欢哪些编程语言？",
      "header": "编程语言",
      "options": [
        {"label": "Python", "description": "简洁优雅，适合数据科学和脚本"},
        {"label": "TypeScript", "description": "类型安全的 JavaScript，前后端通吃"},
        {"label": "Go", "description": "高并发，编译快，部署简单"},
        {"label": "Rust", "description": "内存安全，性能极致"}
      ],
      "multiSelect": true
    }]
  }
}
```

**多个问题 - 单选 + 多选混合**：
```json
{
  "tool_input": {
    "questions": [
      {
        "question": "你更偏好哪种开发模式？",
        "header": "开发模式",
        "options": [
          {"label": "独立开发", "description": "一个人全栈搞定，自由灵活"},
          {"label": "结对编程", "description": "两人协作，实时 review"},
          {"label": "团队协作", "description": "多人分工，流程规范"}
        ],
        "multiSelect": false
      },
      {
        "question": "你常用哪些开发工具？",
        "header": "开发工具",
        "options": [
          {"label": "VS Code", "description": "轻量灵活，插件丰富"},
          {"label": "Vim/Neovim", "description": "终端利器，效率极高"},
          {"label": "JetBrains IDE", "description": "功能全面，智能补全强大"},
          {"label": "Claude Code", "description": "AI 驱动的 CLI 开发助手"}
        ],
        "multiSelect": true
      }
    ]
  }
}
```

## Goals / Non-Goals

- Goals:
  - 在飞书卡片上渲染 AskUserQuestion 的问题和选项，支持用户远程选择/输入
  - 复用现有的 socket 通信和回调架构
  - 支持单选、多选、自定义输入（单选和多选都支持自定义）
- Non-Goals:
  - 不改变非 AskUserQuestion 工具的权限审批流程
  - 不处理 AskUserQuestion 以外的 `updatedInput` 场景

## Decisions

### 1. UI 组件选型

- **单选场景**（`multiSelect: false`）：使用 `select_static` 下拉选择
- **多选场景**（`multiSelect: true`）：使用 `multi_select_static` 多选下拉
- **自定义输入**（单选和多选都支持）：
  - 每个问题下方额外渲染一个 `input` 文本框
  - 单选：自定义输入优先——填写了自定义内容则覆盖下拉选项
  - 多选：自定义输入追加——选中的选项 + 自定义内容合并为答案
- **多问题**：所有问题在同一个 `form` 中渲染，字段名用索引区分（`q_0_select`, `q_0_custom`, `q_1_select`, `q_1_custom`）

Alternatives considered:
- 每个选项一个按钮：选项多时占空间大，不支持多选
- Checkbox 勾选：飞书卡片 checkbox 组件功能有限，不如下拉灵活

### 2. 响应数据格式

Hook 返回格式：
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow",
      "updatedInput": {
        "answers": {
          "问题文本1": "选中的选项label",
          "问题文本2": "自定义输入的内容"
        },
        "questions": [/* 原始 questions 数组原样传回 */]
      }
    }
  }
}
```

- `answers` 的 key 必须精确匹配 `question` 字段文本
- `answers` 的 value 必须精确匹配选中的 `options[].label`（预设选项）或为自定义文本

### 3. 回调数据流

```
permission.sh
  → 提取 questions JSON（json_get_array）
  → 构建 form 卡片（下拉+文本框）+ 发送
  → questions base64 编码后通过 socket 注册请求（携带 questions_encoded）
  → 等待回调响应

用户在飞书选择/输入 → 点击"提交回答"
  → feishu.py 收到 form_value（action=answer）
  → 从 RequestManager 获取原始请求，base64 解码还原 questions
  → 构造 answers dict（单选：自定义输入优先覆盖；多选：自定义输入追加）
  → 调用 /cb/decision 传递 action=answer + answers + raw questions
  → decision_handler 构造 allow + updated_input（含 answers + questions）
  → 通过 socket 返回给 permission.sh
  → permission.sh 输出带 updatedInput 的决策 JSON

用户点击"拒绝回答"/"拒绝并中断"
  → feishu.py 收到 action=deny/interrupt
  → 走通用权限决策路径（_forward_permission_request）
  → 与普通权限审批的 deny/interrupt 处理完全一致
```

### 4. 卡片布局

```
┌─────────────────────────────────────────┐
│  🤔 请回答以下问题                      │
│  项目: my-project · 时间: 14:30         │
├─────────────────────────────────────────┤
│  ■ {header}                             │
│  {question}                             │
│                                         │
│  ┌─ 选择回答 ─────────────────────┐     │
│  │ Option A / Option B            │     │
│  └────────────────────────────────┘     │
│                                         │
│  ┌─ 自定义回答 ──────────────────-┐     │
│  │ (填写后覆盖/追加选项)           │     │
│  └────────────────────────────────┘     │
│  （每个 question 重复上述结构）           │
│                                         │
│  [ 提交回答 ]   [ 拒绝回答 ]   [ 拒绝并中断 ]  │
└─────────────────────────────────────────┘
```

### 5. 按钮行为

卡片底部三个按钮：

| 按钮 | action 值 | 处理路径 | 行为 |
|------|-----------|----------|------|
| 提交回答 | `answer` | `_handle_ask_question_answer` | `allow` + `updatedInput`（含 answers + questions） |
| 拒绝回答 | `deny` | `_forward_permission_request`（通用路径） | `deny` |
| 拒绝并中断 | `interrupt` | `_forward_permission_request`（通用路径） | `deny` + `interrupt: true` |

**交互模式 vs 非交互模式：**
- 按钮行为完全一致
- 区别在于**超时未操作**的兜底行为：
  - 交互模式 → 自动 fallback 到终端
  - 非交互模式 → deny（默认行为）

设计理由：
- 不需要"回退终端"按钮——用户不操作就会超时 fallback
- "拒绝回答"是主动行为，与普通权限审批的"拒绝"语义一致

## Risks / Trade-offs

- **Risk**: `updatedInput.answers` 格式为社区验证，非官方文档明确定义，后续 Claude Code 版本可能变更
  - Mitigation: 保留 fallback 机制（跳过按钮），一旦格式失效可回退
- **Risk**: 多问题场景下表单可能较长
  - Mitigation: 飞书卡片支持滚动，且实际使用中多问题场景较少
- **Trade-off**: 下拉+自定义输入的交互比纯按钮复杂，但覆盖场景更全面

## Open Questions

- Claude Code 未来是否会为 AskUserQuestion 提供独立的 hook event（参考 issue #12605），届时可能需要迁移

## 已解决

- ✅ `multiSelect: true` 时 answers 的值格式：逗号分隔的 label 字符串（如 `"Python, TypeScript, 自定义内容"`），自定义输入追加在末尾

## 已确认

- ✅ `multiSelect: true` 场景真实存在
- ✅ Claude Code 支持多选 + 自定义输入，飞书卡片也应支持
- ✅ 按钮设计：提交回答 / 拒绝回答（deny）/ 拒绝并中断（deny + interrupt），交互/非交互模式行为一致
- ✅ 交互模式超时自动 fallback 终端，不需要额外的"回退终端"按钮
