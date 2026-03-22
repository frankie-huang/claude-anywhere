# Spec: 项目结构规范

## Purpose
定义 claude-anywhere 项目的目录结构组织规范，确保跨语言组件（Shell 和 Python）的代码组织清晰、易于维护。

## Requirements

### Requirement: 根目录简洁性
根目录 SHALL 只保留入口级文件、必要的配置文件和顶级目录。

#### Scenario: 检查根目录文件数量
- Given 项目根目录
- When 列出根目录下的非目录文件
- Then 只包含以下文件：
  - README.md（主文档）
  - CHANGELOG.md（变更日志）
  - LICENSE（许可证）
  - CLAUDE.md（AI 助手指令）
  - .gitignore（Git 忽略配置）
  - .env.example（环境变量模板）
  - install.sh（安装脚本）

#### Scenario: 检查根目录的目录数量
- Given 项目根目录
- When 列出根目录下的目录
- Then 只包含以下目录：
  - src/（源代码）
  - docs/（文档）
  - log/（日志，运行时生成）
  - openspec/（规范）
  - test/（测试）

### Requirement: 源代码目录组织
所有源代码 MUST 统一放置在 `src/` 目录下。

#### Scenario: 源代码入口脚本位置
- Given 需要查找项目入口脚本
- When 查看 `src/` 目录
- Then 入口脚本位于 `src/` 一级目录：
  - `src/hook-router.sh`（统一 Hook 入口）
  - `src/start-server.sh`（服务启动入口）

#### Scenario: 源代码子目录组织
- Given `src/` 目录
- When 列出其子目录
- Then 包含以下组织结构：
  - `src/hooks/`（Hook 处理逻辑）
  - `src/lib/`（Shell 函数库）
  - `src/server/`（Python 回调服务）
  - `src/shared/`（跨语言共享资源）
  - `src/config/`（配置文件）
  - `src/templates/`（模板文件）
  - `src/proxy/`（VSCode 代理）

### Requirement: Hook 统一路由机制
Claude Code Hook MUST 使用统一的路由脚本作为入口，根据事件类型分发到对应处理脚本。

#### Scenario: Hook 入口配置
- Given 需要配置 Claude Code Hook
- When 配置 hook 路径
- Then 所有事件类型都使用同一个入口 `src/hook-router.sh`

#### Scenario: Hook 路由分发
- Given `src/hook-router.sh` 接收到 PermissionRequest 事件
- When 解析事件类型
- Then 路由到 `src/hooks/permission.sh` 处理

#### Scenario: stdin 数据传递
- Given Hook 路由脚本读取 stdin 数据
- When 分发到子脚本
- Then 子脚本通过 `$INPUT` 变量获取数据（不再从 stdin 读取）

#### Scenario: Hook 脚本独立执行
- Given `src/hook-router.sh` 脚本
- When 从任意目录执行该脚本
- Then 脚本能正确找到其依赖的 `src/lib/` 和 `src/hooks/` 目录

### Requirement: 服务端目录组织
回调服务器代码 MUST 统一放置在 `src/server/` 目录。

#### Scenario: 服务端启动脚本
- Given 需要启动回调服务器
- When 执行启动命令
- Then 使用 `src/start-server.sh` 脚本

#### Scenario: 服务端主程序
- Given `src/server/` 目录
- When 查找主程序入口
- Then 主程序为 `src/server/main.py`

### Requirement: 共享资源目录
跨语言共享的协议和配置 SHALL 放置在 `src/shared/` 目录。

#### Scenario: Socket 协议规范
- Given 需要了解 Socket 通信协议
- When 查找协议文档
- Then 协议规范位于 `src/shared/protocol.md`

#### Scenario: 日志配置
- Given Shell 脚本或 Python 程序需要配置日志
- When 读取日志配置
- Then 统一日志配置位于 `src/shared/logging.json`

### Requirement: Shell 库目录命名
Shell 函数库 MUST 放置在 `src/lib/` 目录。

#### Scenario: Shell 核心库位置
- Given 需要引用核心函数（路径、环境、日志）
- When 查找核心库文件
- Then 文件位于 `src/lib/core.sh`

#### Scenario: Shell 专用库位置
- Given 需要查找 JSON 解析函数
- When 查找 JSON 库文件
- Then 文件位于 `src/lib/json.sh`

### Requirement: 路径自适应机制
所有脚本 MUST 使用统一的路径解析机制，支持从任意位置调用。

#### Scenario: 软链接调用支持
- Given Hook 脚本被软链接到 `/usr/local/bin/`
- When 通过软链接执行脚本
- Then 脚本能正确找到项目根目录和所有依赖

#### Scenario: 路径变量统一定义
- Given Shell 脚本需要引用项目路径
- When 查看路径定义
- Then 路径变量（PROJECT_ROOT, SRC_DIR, LIB_DIR 等）在 `src/lib/core.sh` 中统一定义

### Requirement: 安装脚本
项目 SHALL 提供 `install.sh` 脚本简化用户配置。

#### Scenario: 安装脚本检测依赖
- Given 执行 `./install.sh --check`
- When 系统缺少必要依赖（如 python3）
- Then 脚本报告缺少的依赖列表

#### Scenario: 安装脚本配置 hook
- Given 执行 `./install.sh`
- When 用户确认配置
- Then 脚本将 `src/hook-router.sh` 路径写入 Claude Code 配置

### Requirement: 日志配置统一
Shell 和 Python 组件 MUST 使用相同的日志输出目录。

#### Scenario: 日志目录一致性
- Given Hook 脚本和服务端程序运行
- When 查看各组件的日志输出目录
- Then 所有日志输出到项目根目录下的 `log/` 目录

### Requirement: 协议文档化
Socket 通信协议 MUST 有明确的文档说明。

#### Scenario: 协议实现引用
- Given `src/lib/socket.sh` 中的协议实现代码
- When 查看代码注释
- Then 注释中引用 `src/shared/protocol.md` 作为协议规范

## 目录结构参考

```
claude-anywhere/
├── README.md
├── CHANGELOG.md
├── LICENSE
├── CLAUDE.md
├── .gitignore
├── .env.example
├── install.sh                    # 安装配置脚本
│
├── src/                          # 源代码目录
│   ├── hook-router.sh            # 统一 Hook 入口（配置到 Claude Code）
│   ├── start-server.sh           # 服务启动入口
│   │
│   ├── hooks/                    # Hook 处理逻辑
│   │   ├── permission.sh         # 权限请求处理
│   │   └── webhook.sh            # 通用通知处理
│   │
│   ├── lib/                      # Shell 函数库
│   │   ├── core.sh               # 核心库（路径、环境、日志）
│   │   ├── json.sh
│   │   ├── feishu.sh
│   │   ├── socket.sh
│   │   ├── tool.sh
│   │   ├── tool-config.sh
│   │   └── vscode-proxy.sh
│   │
│   ├── server/                   # Python 回调服务
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── socket_client.py
│   │   ├── handlers/
│   │   ├── models/
│   │   └── services/
│   │
│   ├── shared/                   # 跨语言共享资源
│   │   ├── protocol.md
│   │   ├── logging.json
│   │   └── logging_config.py
│   │
│   ├── config/                   # 配置文件
│   │   └── tools.json
│   │
│   ├── templates/                # 飞书卡片模板
│   │   └── feishu/
│   │
│   └── proxy/                    # VSCode 代理
│       └── vscode_ssh_proxy.py
│
├── docs/                         # 文档
│   ├── AGENTS.md
│   ├── deploy/                   # 部署相关
│   ├── design/                   # 架构设计
│   └── reference/                # 参考资料
│
├── log/                          # 日志目录（运行时生成）
│
├── openspec/                     # OpenSpec 规范
│
└── test/                         # 测试脚本
```
