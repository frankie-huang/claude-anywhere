## 1. 基础设施

- [x] 1.1 `config.py`: 新增 `get_agent_type()` 函数，读取 `AGENT_TYPE` 配置（默认 `claude`）
- [x] 1.2 `config.py`: 新增 `get_codex_commands()` 和 `get_codex_args_template()` 函数，与 Claude 对应函数平行
- [x] 1.3 `.env.example`: 新增 `AGENT_TYPE`、`CODEX_COMMAND`、`CODEX_ARGS_TEMPLATE` 配置项及说明
- [x] 1.4 `agents/__init__.py`: 新增 `get_agent_adapter()` 工厂函数，根据 `AGENT_TYPE` 返回对应 adapter 单例

## 2. Codex 适配器

- [x] 2.1 新建 `agents/codex.py`: 实现 `CodexAdapter` 类
  - `agent_type` → `'codex'`
  - `resolve_command()` → 读取 `CODEX_COMMAND`
  - `get_commands()` → 读取 `CODEX_COMMAND` 列表
  - `build_command_string()` → 构建 `codex exec` / `codex exec resume` 命令
  - `build_debug_command_string()` → 脱敏版本
  - `build_env()` → 默认不修改
- [x] 2.2 验证 `CodexAdapter` 命令构建输出符合预期（手动测试或脚本验证）

## 3. Session ID 捕获

- [x] 3.1 `agents/__init__.py`: `AgentAdapter` 新增 `needs_output_session_id` 属性（默认 `False`；Codex 返回 `True`）
- [x] 3.2 `agents/__init__.py`: `launch_agent()` 增加 session ID 捕获逻辑 — 当 `needs_output_session_id=True` 时，启动后读取 stdout 首行解析 `thread_id`
- [x] 3.3 `agents/codex.py`: 实现 `parse_session_id(line)` 方法，从 `{"type":"thread.started","thread_id":"xxx"}` 解析
- [x] 3.4 `handlers/claude.py`: 更新 `handle_new_session()` 处理 session ID 回填逻辑（Codex 路径下用捕获的 ID 替换临时 ID）
- [x] 3.5 `services/session_chat_store.py`: 新增 `rename_session(old_id, new_id)` 方法，用于 Codex 路径的 session ID 替换

## 4. Handler 层适配

- [x] 4.1 `handlers/claude.py`: 将硬编码的 `ClaudeAdapter()` 替换为 `get_agent_adapter()` 工厂调用
- [x] 4.2 `handlers/claude.py`: 命令校验从 `_claude_adapter.get_commands()` 改为 `get_agent_adapter().get_commands()`
- [x] 4.3 验证 `/cb/claude/new` 和 `/cb/claude/continue` 端点在 Claude 和 Codex 模式下均正常工作

## 5. Hook 配置写入

- [x] 5.1 `setup_init.py`: 提取现有 Claude hook 配置写入逻辑为 `_write_claude_hooks()` 函数
- [x] 5.2 `setup_init.py`: 新增 `_write_codex_hooks()` 函数，生成 `config.toml` 格式的 hook 配置
- [x] 5.3 `setup_init.py`: 根据 `AGENT_TYPE` 调用对应的 hook 配置写入函数
- [x] 5.4 验证生成的 `config.toml` 格式正确且 Codex CLI 能识别

## 6. Hook 脚本兼容

- [x] 6.1 调研 Codex hook 输入 JSON 格式与 Claude 的具体字段差异（`session_id`、`transcript_path`、`cwd`、`tool_name` 等字段是否同名）
- [x] 6.2 `hook-router.sh`: 如有字段名差异，添加规范化逻辑
- [x] 6.3 `hooks/stop.sh`: 新增 Codex JSONL transcript 解析分支（检测 `thread.started` 事件判断格式，提取 `agent_message` 文本）
- [x] 6.4 `hooks/permission.sh`: 验证 Codex 传入的 `tool_name`（如 `shell`、`apply_patch`）能正常处理

## 7. 文档更新

- [x] 7.1 `README.md`: 新增 Codex 支持说明（配置方法、差异说明）
- [x] 7.2 `CHANGELOG.md`: 新增 Codex 支持条目
- [x] 7.3 更新相关设计文档（如 `docs/design/` 下涉及 agent 的文档）

## 8. 端到端验证

- [x] 8.1 Claude 模式回归测试：`AGENT_TYPE=claude`（或未设置），验证 /new、/reply、权限审批、stop 通知全链路
- [x] 8.2 Codex 模式测试：`AGENT_TYPE=codex`，验证 /new（session ID 捕获）、/reply（resume）、权限审批、stop 通知
- [x] 8.3 配置切换测试：切换 `AGENT_TYPE` 后重启，验证行为正确切换
