# Codex 场景测试用例

> 日期：2026-05-19
> 关联提交：`f8c9b5e` feat: Codex 全链路对齐 Claude

## 前置条件

1. `.env` 中设置 `AGENT_TYPE=codex`
2. 执行 `./setup.sh restart` 重启后端
3. 确认 `~/.codex/config.toml` 中 hook 已注入（`./setup.sh init`）
4. Codex CLI 已安装且可用（`codex --version`）

---

## 一、新建会话

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 新建会话基本流程 | 飞书发送 `/new 你好` | 会话正常创建，收到 Stop 通知卡片 |
| 2 | Stop 卡片标题 | 观察卡片标题 | 显示 "Codex 处理完成"（非 "Claude Code" 或 "Codex Code"） |
| 3 | Stop 卡片恢复命令 | 观察卡片底部 | 显示 `codex resume <session_id>`（非 `claude --resume`） |
| 4 | Stop 卡片响应内容 | 观察卡片正文 | 包含所有中间消息（非仅最后一条） |
| 5 | Session ID 捕获 | 查看后端日志 | 出现 `Captured session ID: <thread_id>` |
| 6 | Session store 记录 | 查看 `session_chat_store.json` | key 为真实 thread_id（非临时 UUID） |

## 二、继续会话

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 7 | 群聊回复继续会话 | 在群里直接回复消息 | 正常执行，不报 "Session expired or not found" |
| 8 | 多轮对话 | 连续回复 2-3 轮 | 每轮 Stop 卡片都包含当轮完整响应 |
| 9 | 终端恢复命令可用 | 复制卡片中的 `codex resume <id>` 在终端执行 | 进入交互会话，上下文连续 |

## 三、Permission 审批

> **注意**：`codex exec` 模式下 `approval_policy` 被强制降级为 `never`，PermissionRequest hook **不会触发**。以下用例当前预期为 **N/A**，待 Codex 上游支持后启用。

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 10 | Permission 卡片展示 | 触发权限请求 | 卡片正常展示权限请求内容 |
| 11 | Permission 卡片提示语 | 观察卡片底部 | 显示 "请尽快操作以避免 Codex 超时等待" |
| 12 | Permission 卡片恢复命令 | 观察卡片底部 | 显示 `codex resume <session_id>` |
| 13 | 允许操作 | 点击"允许" | Codex 继续执行，无报错 |
| 14 | 拒绝操作 | 点击"拒绝" | Codex 收到 deny，无 `unsupported interrupt:true` 报错 |

## 四、UserPromptSubmit 通知

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 15 | 首条不重复回显 | `/new` 新建会话 | 首条 prompt 不在群里重复显示（skip 标志生效） |
| 16 | 继续会话回显 | 群内回复继续会话 | prompt 正常显示在群聊话题中 |
| 17 | 静音会话 | mute 后触发会话 | UserPromptSubmit 不发送通知 |

## 五、Stop 响应提取

> Stop hook 的 `transcript_path` 始终指向 Codex 持久化文件（`~/.codex/sessions/.../rollout-*.jsonl`），
> 无论 `/new` 还是 resume 都走相同的解析路径。与第一、二组测试重叠，确认卡片内容完整即可。

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 18 | 多条消息完整展示 | `/new` 触发含多步回复的任务 | stop 卡片包含所有中间消息（非仅最后一条） |
| 19 | 多轮对话提取 | resume 后再次触发 | stop 卡片只包含当轮响应（非历史 turn 的内容） |
| 20 | Thinking 提取 | 触发复杂任务（如"分析目录结构"） | 卡片中展示 thinking 折叠区（如 Codex 无 reasoning 输出则 N/A） |
| 21 | 空响应 fallback | Codex 无文本输出 | 卡片显示默认消息 "处理完成，无文本响应"（难触发，可跳过） |

## 六、Hook 基础设施

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 22 | AGENT_TYPE 检测 | 任意会话触发 | 日志显示 `Hook router received event: Stop, agent: codex` |
| 23 | hook 不阻塞 Codex | 观察 Codex CLI 输出 | "Running Stop hook" 立即消失（不长时间停留） |
| 24 | 后台进程不继承 pipe | 同上 | `send_stop_notification_async >/dev/null 2>&1 &` 不阻塞 CLI |

## 七、错误处理

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 25 | 进程启动失败 | 配置错误的 CODEX_COMMAND | 群聊收到 "❌ Codex 执行异常" 通知 |
| 26 | Session ID 捕获超时 | 模拟 Codex 启动慢（>10s） | 日志 warning，会话仍可用（用临时 ID） |

## 八、Group 模式

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 27 | P2P 发 /new 建群 | P2P 聊天中发送 `/new` | 自动建群，群名包含项目信息 |
| 28 | GroupSessionStore 更新 | session rename 后查看 store | 群路由映射指向新 thread_id（非临时 UUID） |
| 29 | 群内继续会话 | 在自动建的群里回复 | 正确路由到 Codex 会话 |

## 九、回归验证（切回 Claude）

| # | 测试项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 30 | 切回 Claude | `.env` 改回 `AGENT_TYPE=claude` + `./setup.sh restart` | Claude 会话一切正常 |
| 31 | Stop 卡片标题 | 新建 Claude 会话 | 显示 "Claude Code 处理完成" |
| 32 | 恢复命令 | 观察卡片底部 | 显示 `claude --resume <session_id>` |
| 33 | 错误通知 | 模拟失败 | 显示 "❌ Claude 执行异常" |

---

## 测试优先级

1. **P0（核心流程）**：#1-6 新建会话 + session ID 捕获
2. **P1（继续会话）**：#7-9 多轮对话 + 恢复命令
3. **P1（hook 基础）**：#22-24 agent 检测 + pipe 不阻塞
4. **P2（回归验证）**：#30-33 切回 Claude 无回归
5. **P2（格式覆盖）**：#18-21 两种 transcript 格式
6. **P3（边缘场景）**：#15-17, #25-29
7. **N/A（待上游）**：#10-14 Permission 审批
