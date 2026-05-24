## MODIFIED Requirements

### Requirement: Permission Script Integration

permission.sh SHALL 作为 Claude 和 Codex 的共享权限审批脚本，两种 agent 通过不同路径触发但共享同一套审批逻辑。

#### Scenario: Claude 权限审批路径

- **GIVEN** `AGENT_TYPE=claude`
- **WHEN** Claude CLI 遇到需要权限的工具调用
- **THEN** 通过 `--permission-prompt-tool` MCP 工具触发
- **AND** `permission_mcp.py` 调用 `hook-router.sh` → `permission.sh`
- **AND** `permission.sh` 发送飞书审批卡片并等待用户决策

#### Scenario: Codex 权限审批路径

- **GIVEN** `AGENT_TYPE=codex`
- **WHEN** Codex CLI 遇到需要权限的工具调用
- **THEN** 通过原生 `PermissionRequest` hook 直接触发
- **AND** 调用 `hook-router.sh` → `permission.sh`
- **AND** `permission.sh` 发送飞书审批卡片并等待用户决策
- **AND** 不经过 `permission_mcp.py`

#### Scenario: 审批卡片展示兼容

- **GIVEN** 权限请求来自 Codex
- **AND** `tool_name` 为 Codex 工具名（如 `shell`、`apply_patch`）
- **WHEN** `permission.sh` 构建飞书审批卡片
- **THEN** 卡片正常展示 Codex 工具名和工具参数
- **AND** 用户可以正常点击批准/拒绝按钮

### Requirement: Hook Configuration

系统 SHALL 根据 `AGENT_TYPE` 在对应 agent 的配置文件中注册 hook。

#### Scenario: Claude hook 配置

- **GIVEN** `AGENT_TYPE=claude`
- **WHEN** 执行 hook 配置初始化
- **THEN** 在 `~/.claude/settings.json` 中注册 `UserPromptSubmit`、`PermissionRequest`、`Stop` hook
- **AND** hook command 指向 `src/hook-router.sh`

#### Scenario: Codex hook 配置

- **GIVEN** `AGENT_TYPE=codex`
- **WHEN** 执行 hook 配置初始化
- **THEN** 在 `~/.codex/config.toml` 中注册 `UserPromptSubmit`、`PermissionRequest`、`Stop` hook
- **AND** hook command 指向 `src/hook-router.sh`
- **AND** `PermissionRequest` hook 的 timeout 为 `PERMISSION_REQUEST_TIMEOUT + 60` 秒
