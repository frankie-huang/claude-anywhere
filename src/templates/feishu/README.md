# 飞书卡片模板

本目录包含飞书卡片消息的 JSON 模板,用于权限请求通知和通用通知。

## 模板文件说明

### 主模板

#### permission-card.json
**用途**: 交互式权限请求卡片(带操作按钮)

**变量**:
- `{{at_user}}` - @ 用户标签(由 `FEISHU_AT_USER` 配置控制,默认 @ `FEISHU_OWNER_ID`)
- `{{template_color}}` - 卡片主题颜色(orange/yellow/blue/purple/grey)
- `{{tool_name}}` - 工具名称(Bash/Edit/Write/Read 等)
- `{{project_name}}` - 项目名称
- `{{timestamp}}` - 时间戳
- `{{session_id}}` - 会话标识（session_id）
- `{{detail_elements}}` - 详情元素(JSON 对象,由子模板渲染生成)
- `{{buttons_json}}` - 按钮数组(JSON 字符串)
- `{{footer_hint}}` - 卡片底部提示文本

**使用场景**: 回调服务运行时的权限请求通知

#### permission-card-static.json
**用途**: 静态权限请求卡片(无操作按钮)

**变量**:
- `{{at_user}}` - @ 用户标签(由 `FEISHU_AT_USER` 配置控制,默认 @ `FEISHU_OWNER_ID`)
- `{{template_color}}` - 卡片主题颜色
- `{{tool_name}}` - 工具名称
- `{{project_name}}` - 项目名称
- `{{timestamp}}` - 时间戳
- `{{detail_elements}}` - 详情元素(JSON 对象,由子模板渲染生成)

**使用场景**: 回调服务未运行时的权限请求通知

#### notification-card.json
**用途**: 通用通知卡片

**变量**:
- `{{title}}` - 卡片标题
- `{{content}}` - 通知内容(Markdown 格式)
- `{{project_name}}` - 项目名称
- `{{timestamp}}` - 时间戳

**使用场景**: 非权限请求的通用通知

#### stop-card.json
**用途**: Stop 事件完成通知卡片

**变量**:
- `{{at_user}}` - @ 用户标签（由 `FEISHU_AT_USER` 配置控制，默认 @ `FEISHU_OWNER_ID`）
- `{{project_name}}` - 项目名称
- `{{session_id}}` - 会话标识（session_id）
- `{{timestamp}}` - 时间戳
- `{{thinking_element}}` - 思考过程元素（仅在启用思考且有内容时出现）
- `{{response_elements}}` - 响应内容元素数组（Markdown 格式）
- `{{resume_session_id}}` - 恢复会话的命令 ID

**使用场景**: 主 Agent 完成响应时的任务完成通知

### 子模板

子模板用于构建卡片中的可复用元素,可以组合到主模板中。

#### command-detail-bash.json
**用途**: Bash 命令详情元素

**变量**:
- `{{command}}` - Bash 命令内容
- `{{command_hint}}` - 截断提示（内容过长时显示，为空则不显示）

#### command-detail-file.json
**用途**: 文件操作详情元素(Read，以及 Edit/Write 降级时使用)

**变量**:
- `{{file_path}}` - 文件路径

#### command-detail-edit.json
**用途**: Edit 工具差异对比元素（展示删除/新增内容）

**变量**:
- `{{file_path}}` - 文件路径
- `{{diff_old}}` - 删除内容（代码块内展示）
- `{{diff_new}}` - 新增内容（代码块内展示）
- `{{diff_old_hint}}` - 删除内容截断提示（内容过长时显示，为空则不显示）
- `{{diff_new_hint}}` - 新增内容截断提示（内容过长时显示，为空则不显示）

#### command-detail-write.json
**用途**: Write 工具写入内容展示元素

**变量**:
- `{{file_path}}` - 文件路径
- `{{write_content}}` - 写入内容（代码块内展示）
- `{{write_content_hint}}` - 截断提示（内容过长时显示，为空则不显示）

#### description-element.json
**用途**: 操作描述元素

**变量**:
- `{{description}}` - 描述内容

**渲染结果示例**:
```json
{
  "tag": "div",
  "text": {
    "content": "**描述：** 安装项目依赖",
    "tag": "lark_md"
  }
}
```

#### plan-content.json
**用途**: 方案内容元素（ExitPlanMode 工具）

**变量**:
- `{{plan_content}}` - 方案内容（Markdown 格式）

**结构**: 包含两个元素——**方案：** 标签 + 灰底背景（`grey-50`）的 Markdown 内容区域

#### buttons.json
**用途**: 权限请求卡片的交互按钮（Webhook 模式）

**变量**:
- `{{callback_url}}` - 回调服务器 URL
- `{{request_id}}` - 请求 ID

**按钮类型**: `open_url`（点击后跳转浏览器）

**使用场景**: `FEISHU_SEND_MODE=webhook` 时与 `permission-card.json` 配合使用

#### buttons-openapi.json
**用途**: 权限请求卡片的交互按钮（OpenAPI 模式）

**变量**:
- `{{request_id}}` - 请求 ID
- `{{owner_id}}` - 飞书用户 ID（用于回调时标识操作者）

**按钮类型**: `callback`（点击后飞书内直接响应，显示 toast 提示）

**使用场景**: `FEISHU_SEND_MODE=openapi` 时与 `permission-card.json` 配合使用

**注意**: 使用此模板需在飞书开放平台订阅 `card.action.trigger` 事件

## 子模板使用方法

### 通过 render_sub_template 函数

```bash
# 加载函数库
source src/lib/feishu.sh

# 渲染 Bash 命令元素
cmd_element=$(render_sub_template "command-bash" "command=npm install")

# 渲染描述元素
desc_element=$(render_sub_template "description" "description=安装依赖")

# 组合使用
card=$(build_permission_card "Bash" "myproject" "2024-01-01 12:00:00" \
    "npm install" "安装依赖" "orange" "$buttons")
```

### 手动组合子模板

```bash
# 渲染各个子模板
cmd_element=$(render_sub_template "command-bash" "command=npm install")
desc_element=$(render_sub_template "description" "description=安装依赖")

# 组合到一起（注意：实际使用中建议用 build_permission_card 函数）
detail_elements="${cmd_element},
${desc_element}"
```

## 自定义模板

### 方法 1: 修改默认模板

直接编辑本目录下的模板文件,修改后立即生效。

```bash
# 编辑权限请求卡片模板
vim templates/feishu/permission-card.json
```

### 方法 2: 使用自定义模板路径

通过环境变量指定自定义模板目录:

```bash
export FEISHU_TEMPLATE_PATH=/path/to/custom/templates
```

自定义目录结构需要与默认目录一致:
```
/path/to/custom/templates/
├── permission-card.json           # 主模板:权限请求卡片(带按钮)
├── permission-card-static.json    # 主模板:权限请求卡片(无按钮)
├── notification-card.json         # 主模板:通用通知卡片
├── stop-card.json                 # 主模板:Stop 事件完成通知卡片
├── buttons.json                   # 子模板:交互按钮（Webhook 模式，open_url）
├── buttons-openapi.json           # 子模板:交互按钮（OpenAPI 模式，callback）
├── command-detail-bash.json       # 子模板:Bash命令详情
├── command-detail-file.json       # 子模板:文件操作详情
├── command-detail-edit.json       # 子模板:Edit差异对比详情
├── command-detail-write.json      # 子模板:Write写入内容详情
├── description-element.json       # 子模板:描述元素
├── plan-content.json              # 子模板:方案内容（ExitPlanMode 工具）
└── thinking-element.json          # 子模板:思考过程元素
```

## 模板变量替换规则

模板使用 `{{variable_name}}` 占位符,渲染时会替换为实际值。

### 特殊字符处理

- **双引号(`"`)**: 自动转义为 `\"`
- **反斜杠(`\`)**: 自动转义为 `\\`
- **换行符**: 保留原样
- **变量值包含特殊字符**: 自动进行 JSON 安全转义

### JSON 数组注入

对于 `buttons.json` 等返回 JSON 数组的模板,变量替换后仍为有效的 JSON。

### 按钮模板选择

`build_permission_buttons()` 函数根据 `FEISHU_SEND_MODE` 自动选择按钮模板：

| `FEISHU_SEND_MODE` | 使用的模板 | 按钮类型 | 用户体验 |
|-------------------|-----------|---------|---------|
| `webhook` | `buttons.json` | `open_url` | 点击跳转浏览器显示结果 |
| `openapi` | `buttons-openapi.json` | `callback` | 点击飞书内直接显示 toast |

示例:
```bash
# 先渲染按钮模板
buttons=$(render_template "buttons.json" \
    "callback_url=http://localhost:8080" \
    "request_id=123-456")

# 再将按钮数组注入卡片模板
card=$(render_template "permission-card.json" \
    "buttons_json=$buttons" \
    "tool_name=Bash" \
    ...)
```

## 故障排查

### 模板文件未找到

**错误日志**: `Template not found: /path/to/template.json`

**解决方案**:
1. 检查模板文件路径是否正确
2. 确认 `FEISHU_TEMPLATE_PATH` 环境变量设置
3. 验证文件权限

### 模板 JSON 格式错误

**错误日志**: `Invalid JSON in template: ...`

**解决方案**:
1. 使用 `jq` 或 Python 验证 JSON 格式:
   ```bash
   jq empty templates/feishu/permission-card.json
   # 或
   python3 -m json.tool templates/feishu/permission-card.json
   ```
2. 检查 JSON 语法(括号匹配、逗号位置等)
3. 确保所有变量占位符格式正确

### 变量替换后 JSON 无效

**错误日志**: `Rendered JSON is invalid: ...`

**解决方案**:
1. 检查变量值是否包含未转义的特殊字符
2. 查看 `DEBUG=1` 时的完整输出
3. 手动验证渲染后的 JSON 格式

### 卡片发送失败

**错误日志**: `Failed to send Feishu card: ...`

**降级处理**:
- 系统会自动发送降级文本通知
- 检查 `FEISHU_WEBHOOK_URL` 环境变量
- 验证网络连接和 Webhook URL 有效性

## 调试技巧

### 启用调试模式

```bash
export DEBUG=1
# 运行权限请求,查看渲染后的完整 JSON
```

### 手动测试模板

```bash
# 加载函数库
source src/lib/feishu.sh

# 方法1: 使用 build_permission_card (推荐)
buttons=$(build_permission_buttons "http://localhost:8080" "123-456")
card=$(build_permission_card "Bash" "myproject" "2024-01-01 12:00:00" \
    "npm install" "安装依赖" "orange" "$buttons")

# 方法2: 使用 render_card_template
card=$(render_card_template "permission" \
    "template_color=orange" \
    "tool_name=Bash" \
    "project_name=myproject" \
    "timestamp=2024-01-01 12:00:00" \
    "detail_elements={\"tag\":\"div\",\"text\":{\"content\":\"**命令：**\\nnpm install\",\"tag\":\"lark_md\"}}" \
    'buttons_json=[{"tag":"button","text":{"content":"批准","tag":"plain_text"}}]')

# 方法3: 测试子模板
cmd_element=$(render_sub_template "command-bash" "command=npm install")
echo "$cmd_element" | jq .

desc_element=$(render_sub_template "description" "description=安装依赖")
echo "$desc_element" | jq .

# 验证 JSON
echo "$card" | jq .
```

## 版本管理

模板文件的修改建议通过 Git 进行版本管理:

```bash
# 查看模板变更
git diff templates/feishu/

# 创建自定义模板分支
git checkout -b custom-feishu-cards
# 编辑模板...
git commit -am "Customize Feishu card templates"
```

## 相关文档

- [飞书官方文档 - 卡片消息](https://open.feishu.cn/document/ukTMukTMukTM/uEjNwUjLxYDM14SM2ATN)
- [项目 README](../../README.md)
- [Shell 函数库说明](../../src/lib/feishu.sh)
