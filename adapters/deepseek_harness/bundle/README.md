# QuietBridge × DeepSeek Harness bundle

本目录是 QuietBridge 的 **DSH Host bundle**（`@quietbridge/dsh`）：把 QuietBridge 的桌面能力以
Agent 作用域工具的形式接入 DeepSeek Harness。插件只依赖 DSH 公开的 `ctx` API 与 Node.js 内置模块，
不 import 任何 `@deepseek-ai/*` 或 `@modelcontextprotocol/*` 包（profile 安装的 bundle 无法解析这些包），
因此可以随安装目录整体分发。

## 架构

- `apply(ctx, config)` 在插件加载时订阅 `agent/created` / `agent/disposed` / `session/event` /
  `session/disposed` / `tools/result`，并登记插件级清理 effect。
- 每个 Agent 创建时启动**一个独立的连接器进程**（`python -B server.py`，MCP stdio JSON-RPC），
  环境变量注入可信宿主身份：
  - `QUIETBRIDGE_HOST=deepseek-harness`
  - `QUIETBRIDGE_HOST_SESSION_ID=<agent.session.id>`
  - `QUIETBRIDGE_HOST_AGENT_ID=<agent.id>`（本版本两者相等）
  - `QUIETBRIDGE_HELPER_PATH`、`QUIETBRIDGE_CONFIG_DIR`
- 连接器就绪后读取 `tools/list`，在 `agent.ctx.tools.register(...)` 上按 **MCP 原始工具名**
  （如 `quietbridge_task`，不加 `mcp__` 前缀）注册到该 Agent 作用域；不同 Agent 的工具与连接器互相隔离。
- `index.js` 内实现最小 MCP stdio 客户端：`initialize`（协商并回读 `result.protocolVersion`）→
  `notifications/initialized` → `tools/list`（防御性处理 `nextCursor`）→ `tools/call`；
  newline 分帧；调用超时或 `exec.signal` abort 时发送 `notifications/cancelled`，此后丢弃迟到响应；
  连接器退出后该 Agent 的调用返回结构化错误，**不自动重放输入、不自动重连**，其他 Agent 不受影响。
- `notifications/tools/list_changed` 会重新同步工具集（先取新列表，再替换旧注册；服务器当前声明
  `listChanged: false`，这是防御性处理）。
- 生命周期：从 **持久 session 事件** `turn/start`、`turn/end`、`tool/call`、`tool/result` 生成
  `turn-start` / `turn-end` / `tool-result`，加上 `agent-created` / `agent-disposed`，通过一次性子进程
  `<python> -B <bridge>/adapters/deepseek_harness/lifecycle.py --config-dir <configDir>` 的 stdin
  单 JSON 写入 Python 适配器维护的 lifecycle store。同一 Agent 的事件按顺序串行发送，每个子进程默认
  5 秒超时；任何失败只 `console.warn`，绝不阻塞宿主或触发续跑。
- 清理：Agent 作用域 effect + 插件级 effect + `agent/disposed` 三路幂等清理；插件卸载时关闭所有连接器。
  连接器子进程的 stdin 由 Host 持有，Host 进程退出/管道关闭时 Python 侧读到 EOF 自行退出，避免孤儿进程；
  正常卸载时还会先 `stdin.end()`、SIGTERM，超时后 SIGKILL（每连接最多 2 次 2 秒等待）。

## 配置（cordis.patch.yml）

安装器会把行内 `config` 渲染为具体值；手工使用时也可直接通过环境变量提供：

| 字段 | 环境变量 | 默认 |
| --- | --- | --- |
| `python` | `QUIETBRIDGE_DSH_PYTHON` | 存在 `/usr/bin/python3` 时用它，否则 `python3` |
| `connector` | `QUIETBRIDGE_DSH_CONNECTOR` | 必填 |
| `helper` | `QUIETBRIDGE_DSH_HELPER` | 可选 |
| `configDir` | `QUIETBRIDGE_DSH_CONFIG_DIR` | 必填 |
| `serverName` | `QUIETBRIDGE_DSH_SERVER_NAME` | `quietbridge` |

`index.js` 还接受以下可选配置（安装器不写、按需手填）：

- `requestTimeoutMs`（默认 `70000`）：单次 `tools/call` 预算，与连接器内部 70s worker 预算对齐。
- `startupTimeoutMs`（默认 `min(requestTimeoutMs, 5000)`）：`initialize` 与首次 `tools/list` 的预算。
- `lifecycleTimeoutMs`（默认 `5000`）：每个 lifecycle 子进程的预算。
- `lifecycleScript`：覆盖默认的 `dirname(connector)/adapters/deepseek_harness/lifecycle.py`
  （也可用 `QUIETBRIDGE_DSH_LIFECYCLE`，注意该变量不在模板 `!!js` 中，需手工改 patch 或 config）。
- `maxConnectors`（默认 `3`，上限 32）：同时存活的连接器数量上限，与连接器服务端“最多 3 个
  会话”的容量预算对齐。达到上限的新 Agent 不启动连接器、不注册工具，只记录警告——绝不无限
  spawn Python 进程；已释放/崩溃的连接器会立即归还名额。

`python` 是 CPython 时自动附加 `-B`；其他解释器按直接可执行文件处理（便于测试用 Node 假连接器）。

配置缺 `connector`/`configDir` 或类型非法时插件激活失败（`apply` 抛 `TypeError`），不静默降级为
猜测归因；连接器启动失败只影响该 Agent 的工具可见性，不失败 Agent 创建（`agent/created` 监听器内
全部异常被吞掉并记录警告）。

## 工具与结果映射

- 工具名沿用 MCP `tools/list` 的原始名（`quietbridge_*`，不加 `mcp__` 前缀——这些是 Agent 作用域
  工具，不是 profile 级 MCP 客户端；连接器 `tools/call` 需要原始 wire 名，lifecycle 也同时接受原始名）。
  不符合 DSH 名称约束（`[A-Za-z0-9_-]`、≤64 字符）的工具会被跳过。
- `parameters` 会把 MCP inputSchema 降级到 DSH 支持的 JSON Schema 子集：去掉 `$schema`/`$defs`，
  内联本地 `$ref`，`anyOf`→`oneOf`、对象 `allOf` 合并、类型数组折叠、元组 `items`→`{}`，丢弃
  `format`/`minimum`/`pattern` 等不支持关键字。
- `output.schema` 使用受支持的宽松对象 schema；`render` 同步返回 `[{type:'text', text}]`。
- MCP 内容块（text/image/audio/resource_link/resource）全部归一化为可读文本：图片、音频、嵌入资源
  只输出元数据说明，**不把 base64 或二进制内容放进模型上下文 / 值 / lifecycle store**。
- MCP `isError` 结果会作为工具失败抛出，模型能看到错误文本；连接器崩溃、超时、取消也会转为带
  `CONNECTOR_*` / `MCP_TOOL_ERROR` 前缀的可读错误。

## 安装

本 bundle **不自行安装到 profile**，也不写 `~/.dsh`。由仓库安装器
（`adapters/deepseek_harness/configuration.py` 的 `bundle_render` + `profile_plan`）渲染
`cordis.patch.yml` 并写入 profile 依赖与 patch 行。开发期可直接把本目录作为
`plugin_manager install_bundle` 的 target（需人工确认），但本轮未做真实安装验收。

## 测试

```sh
node --check adapters/deepseek_harness/bundle/index.js
node --test adapters/deepseek_harness/bundle/test/*.test.mjs
```

测试使用 `test/fixtures/fake-connector.mjs`（Node 实现的假 MCP stdio 服务器）与
`test/fixtures/fake-lifecycle.mjs`（记录 stdin 的假 lifecycle 脚本），覆盖：工具名/schema 降级、
`tools/call` 往返与内容归一化、取消→`notifications/cancelled` 与迟到响应丢弃、超时、
连接器崩溃隔离与不重放、turn/tool 生命周期事件映射与去重、`tools/list_changed` 重同步、
插件卸载/Agent 释放清理。**不依赖真实 DSH、真实 helper 或网络。** 另有一组 `integration.test.mjs`
在检测到本机 `python3` 与仓库 `adapters/deepseek_harness/lifecycle.py` 时，用真实 lifecycle CLI 验证
stdin 协议、turn/任务回执写入与 `agent-disposed` tombstone；缺失时自动 skip，其余测试不受影响。

## 已知限制（本轮未验证/未实现）

- 不安装到本机 profile，未做真实 DSH 端到端验收；`agents` 服务枚举仅用于 profile 重载时接管已有 Agent。
- 原生预览的宿主窗口绑定（DSH 窗口证据）未实现，本轮不宣称 DSH 预览可用。
- DSH 拉起签名 helper 的 TCC 归属未核实；`helperPath` 只做配置透传。
- 图片内容按文本降级（本轮不输出 image content block）；需要图片时由后续版本接入 attachment 投影。
- 连接器退出后不自动重连、不自动重放输入；由模型/用户核实既有结果后重新建立阶段。
- lifecycle 写入是 best-effort：脚本缺失/超时/失败只丢生命周期簿记，不影响工具调用与轮次。
- `tool-result` 优先取 session 事件 `tool/call`+`tool/result`（含 turn）；`tools/result` 仅作无 turn
  场景的补充回执，且按 `callId` 去重。
