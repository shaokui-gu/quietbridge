# QuietBridge

[English](README.md)

QuietBridge 把 AI Agent 接到 **macOS 后台原生应用与浏览器操作** 上。它保持用户当前前台
应用、键盘焦点、物理鼠标指针和当前 Space 不变：原生操作使用一个显式共享的目标窗口，
浏览器操作使用 Chrome 扩展，且任何失败都不会回落为桌面键鼠。

当前版本：**0.6.29（build 118）**。本机构建包为 `dist/quietbridge-0.6.29.pkg`
（SHA256 `de679aea9de8d62affff2f97de77f24e74f16c06f3d6b9dd3ead12580fad85c7`）。
app、helper 与授权插件使用 Developer ID 签名；外层 PKG 按现行策略不签名、不公证。

## 提供什么

- **一个 macOS 应用**（`QuietBridge.app`，菜单栏常驻）：签名原生 helper、权限管理与可选
  Locked-use 授权插件。
- **一个 Chrome 扩展**（`QuietBridge Browser`，MV3）：固定标签 CDP 输入、文档绑定、
  后台弹窗与分页读取。
- **一个宿主中立的 MCP 服务**（`services/mcp`）：19 个 `quietbridge_*` 工具，覆盖应用、
  窗口、任务、AX 状态、截图、视觉/视频预览、点击/键入/粘贴/滚动/拖拽/悬停、浏览器操作
  与异常恢复。
- **宿主集成**：
  - **Workbuddy**：MCP 连接器 + 可信生命周期 hooks。
  - **DeepSeek Harness 0.2.2-alpha.2**：可安装 bundle（`@quietbridge/dsh`），每 Agent
    一个 MCP 连接器，使用宿主注入的可信 session 身份、宿主轮次路由与 Agent 作用域工具。

## 状态

| 宿主 | 状态 |
| --- | --- |
| Workbuddy | 已实现；与应用、扩展共用同一套统一运行态 |
| DeepSeek Harness | 0.6.29（118）已通过真实 profile 验收：真实 profile 安装 bundle、每 Agent 连接器、19 工具、turn 生命周期、重启恢复、多阶段任务保护与完整浏览器操作均已在真实桌面实测 |

已在真实扩展上验证的浏览器操作包括 `create_target`、`candidates`、`assign`、
`verify_mapping`（窗口活动标题精确匹配）、`snapshot`、`read`（links/content）、
`screenshot`、`diagnostics`、`scroll`、`keypress`、`click`、`navigate`、`tabs`、
`select_tab`、`annotate`、`annotations`、`fill`、`select`；本地表单页实测
`fill + select + click` 端到端通过。

尚未验收、不对外宣称：DSH 原生预览宿主窗口绑定、图片以 attachment 形式投影给模型
（当前返回文本 + 落盘 PNG 路径）、真实 profile 多 Agent 并发与长跑、Workbuddy 真实
多会话/预览视觉验收、PKG 外层签名与公证。

## 统一运行态

所有运行态集中在一个目录：

```
~/Library/Application Support/QuietBridge/
├── connector/                 # 稳定无头 MCP 连接器
├── extension/native-host/     # Chrome 原生消息宿主（只注册一次）
├── releases/<digest>/         # 已验证 app 副本 + 渲染出的 DSH bundle
├── sessions/<key>/            # 按宿主会话隔离的任务通道
├── host-lifecycle/            # 宿主路由使用的 turn/task 回执
├── browser-events.json        # 扩展心跳与候选证据
├── task-windows.json          # 原生任务绑定
└── logs/                      # bridge / native 操作日志
```

Chrome 只注册一个原生宿主（`com.cua.browser_observer`）指向该运行态。旧的
`WorkbuddyExtension` / `QuietBridgeBrowser` 目录只作为**一次性迁移来源**，不再作为活动
host。工具名为 `quietbridge_*`；旧的 `cua_*` 调用与已有连接配置继续作为别名兼容。

## 环境要求

- Apple Silicon、macOS 14+
- Swift 6（构建原生应用）
- Python 3.9+（系统 Python 即可；安装后的 app 使用 `/usr/bin/python3`）
- Node.js（扩展与 bundle 测试）
- Chrome 106+（浏览器任务）
- DeepSeek Harness 0.2.2-alpha.2（DSH 集成）

## 构建与测试

在仓库根目录执行：

```sh
swift build --package-path apps/macos --configuration release
python3 apps/macos/tests/test_swift.py

python3 -m unittest discover -s services/mcp -p 'test_*.py' -q          # 495 项
python3 -m unittest discover -s packaging/macos -p 'test_*.py' -q      # 57 项
node --test extensions/chromium/*.test.mjs                             # 117 项
node --test adapters/deepseek_harness/bundle/test/*.test.mjs           # 44 项

# 可选：临时 DSH_HOME/profile，绝不读写 ~/.dsh
env -u DSH_HOME -u PYTHONPATH python3 tools/dsh_isolated_check.py --agents 3
```

构建共享安装包：

```sh
python3 packaging/macos/build.py --version 0.6.29 --build 118 \
  --identity 'Developer ID Application: …' --profiles-dir sign
```

产物为 `dist/quietbridge-0.6.29.pkg`。构建本身不代表外层 PKG 已签名；签名与公证是独立
发布步骤。

## 安装

1. 双击安装 `dist/quietbridge-0.6.29.pkg`；
2. 按提示给 QuietBridge 授予 **辅助功能** 与 **屏幕录制** 权限；
3. 在 Chrome 加载/启用 **QuietBridge Browser** 扩展，并给需要 Agent 操作的站点授权；
4. DeepSeek Harness：把部署好的 bundle 加入 profile（安装包 postinstall 日志会打印
   精确路径）：

```sh
REL=$(ls -dt "$HOME/Library/Application Support/QuietBridge/releases"/* | head -1)
"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  plugin --profile <profile> add "$REL/dsh-bundle"
```

5. 重启 DeepSeek Harness；首次安装后也重启 Chrome，让原生宿主注册生效。

如只想渲染 bundle、不写 DSH profile：

```sh
python3 install.py --install --dsh-bundle /path/to/rendered-bundle \
  --dsh-helper '/Applications/QuietBridge.app/Contents/MacOS/quietbridge-native'
```

## 仓库结构

| 目录 | 职责 |
| --- | --- |
| `apps/macos` | 原生应用、菜单面板、权限、原生执行器与预览 |
| `services/mcp` | 宿主中立的 MCP 工具、任务状态、浏览器消息与恢复 |
| `extensions/chromium` | QuietBridge Browser：固定标签 CDP 操作与观察 |
| `system/authorization-plugin` | 可选的 Locked-use 签名授权插件 |
| `adapters/workbuddy` | Workbuddy MCP 设置与可信生命周期 hooks |
| `adapters/deepseek_harness` | DSH 契约、每 Agent 连接器适配与可安装 bundle |
| `products` | 各宿主产品元数据与发布边界 |
| `packaging/macos` | 共享 PKG 组装、用户文件事务化部署与恢复 |
| `tools` | 发布布局、DSH 隔离检查与开发工具 |
| `docs` | 架构、宿主契约、实现记录与发布说明 |

`project.json` 定义产品名与实现状态；仓库 checkout 所在目录任意，不依赖开发者本机绝对
路径即可构建。

## 文档

- [总体架构](docs/architecture/quietbridge.zh.md)、[多 Agent 运行时](docs/architecture/multi-agent-runtime.zh.md)
- [DeepSeek Harness 宿主契约](docs/architecture/dsh-host-contract.zh.md)
- [DSH 隔离实机验证](docs/implementation/2026-11-15-dsh-isolated-verification.zh.md)
- [DSH 真实 profile 测试记录](docs/implementation/2026-11-15-dsh-real-profile-test.zh.md)
- [DSH PKG 部署](docs/implementation/2026-11-15-dsh-pkg-deploy.zh.md)
- [0.6.29 发布说明](docs/releases/0.6.29.zh.md)

## 兼容性

新工具名为 `quietbridge_*`，旧的 `cua_*` 调用继续作为别名接受；已有自有连接键、环境
配置与稳定连接器路径保持兼容。Bundle 标识现为 `com.quietbridge.app`、
`com.quietbridge.helper` 和 `com.quietbridge.AuthorizationPlugin`，Developer ID team
不变；新标识需要重新检查并授予 macOS 权限。已有授权插件需通过 app 管理器显式更新并
重新登录；主 PKG 不会激活或改写系统授权策略。

## 第三方归属

第三方归属信息保留在 `apps/macos/upstream.json` 与 `apps/macos/LICENSE.cua-driver`。
