# DeepSeek Harness 宿主契约验证（QuietBridge 适配）

更新日期：2026-11-15（北京时间）。适用范围：本机 DeepSeek Harness 0.2.2-alpha.2
（`/Applications/DeepSeek Harness.app/Contents/Resources/app.asar`）。

本文记录 QuietBridge for DeepSeek Harness 适配所需的宿主契约：安装机制、插件 API、
Agent/Session/Turn 身份与事件、工具注册约束、MCP 客户端可用性、依赖解析，以及
本轮未能核实的范围。**结论全部来自本机安装包内代码/文档，或本机 profile 的只读
观测**；未做真实 profile 安装（`plugin_manager install_bundle`）与真实 Agent 端到端
运行，相关条目在“未核实”一节单独列出，不能把本文当作已上线验收。

写入范围：本文件与 `adapters/deepseek_harness/adapter.json`。本轮**没有**修改
`~/.dsh/profiles/desktop` 下任何文件，也没有安装 bundle。

## 0. 结论摘要

| 设计项（本轮方案） | 判定 | 需要调整的点 |
| --- | --- | --- |
| 自包含 Host-only bundle，零 DSH 内部 import | 可行 | manifest 需满足 `dsh.bundle.patch`、locale、icon、exports/files 要求 |
| `agent/created` 时每 Agent 起一个 stdio 连接器 | 可行 | payload 是 `{agent, source, signal?}`；监听器抛错会使 Agent 创建失败；须限时、失败降级、绑定插件级清理 |
| `ctx.tools.register(原始 definition)` 注册每 Agent 工具 | 可行 | `output.schema` 受严格子集限制；`render` 只能输出文本/attachment 图片块；工具名与 `run_code` 约束；注册须在创建期完成 |
| 订阅 `turn/start`、`turn/end` 做生命周期 | 需调整 | 不是 `agent/*` 事件，而是持久 session 事件：`agent.ctx.on('session/event', (session, event) => …)`；payload 是 `{turn, reason}`，**没有** `turnId` 字段 |
| 用 `tools/result` 观察工具结果做回执 | 需调整 | `exec` 里**没有** turn 字段；建议以 session 事件 `tool/call`+`tool/result`（含 turn）为主，`tools/result` 作补充 |
| lifecycle CLI（stdin 单 JSON，失败吞掉） | 可行但降级 | 每事件 spawn Python 会拖慢 turn 结束；须短超时且吞掉失败，建议后续改常驻/批量 |
| 环境变量可信身份（env 注入） | 可行 | 身份只能取 `agent.session.id` / `agent.id`；二者本版本恒等；不要解析 ID 前缀 |
| `services/mcp` 宿主环境上下文路由 | 可行 | 依赖 lifecycle store 的 turn 记录；turn 号是会话内整数，须做复合键 |
| 用 `dsh-mcp-client` profile row 或 programmatic mount | 不可行 | 静态 row 是全局作用域且无法逐 Agent 注入 env；programmatic mount 需内部包且未验证 |
| 原生预览宿主窗口绑定（DSH 窗口证据） | 不可行（本轮） | 未找到任何公开 API 可取 DSH 自身原生窗口/锚点 |
| 签名 helper 的 TCC 归属 | 未核实 | 只做 `helperPath` 配置，不宣称授权可用 |
| 并发子 Agent 验收 | 未验收 | `supportsConcurrentAgents` 保持 `false`，容量沿用 3 |
| 截图等图片结果进入模型上下文 | 不可行（本轮） | 需 attachment 服务 + 模型 image 能力；本轮文本降级并如实标注 |

## 1. 版本与来源

### 1.1 本机 DSH 版本

`app.asar` 是单一包文件，shell 的 `cat`/`ls`、glob 与 ripgrep 都读不了它
（宿主技能 `SKILL.md` 第 38 行明确说明）；必须用 Node 脚本提取。证据均标注
asar 内绝对路径。

| 项 | 值 | 证据 |
| --- | --- | --- |
| 桌面应用包 | `@deepseek-ai/dsh-desktop` 0.2.2-alpha.2 | asar 根 `/package.json`：`name`、`version`、`description: "Electron desktop shell for a bundled dsh runtime and external plugins"` |
| 应用标识 | `com.deepseek.harness` | 同上：`dshDesktopAppId` |
| 更新通道 / 构建 | `nightly` / commit `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`（`dshBuildDirty: false`） | 同上 |
| 运行时包 | `@deepseek-ai/dsh-desktop-runtime` 0.2.2-alpha.2 | `/dsh/package.json`：`name`、`version`；`dependencies` 约 300 个 `@deepseek-ai/*` 包全部为 `0.2.2-alpha.2` |
| Electron 壳入口 | `/lib/main.js`（822 KB） | asar 内文件清单 |
| 内置 CLI/插件核心 | `@deepseek-ai/dsh` 0.2.2-alpha.2 | `/dsh/package.json` 依赖 |
| 本次涉及的关键包 | `dsh-agent`、`dsh-agent-loop`、`dsh-session`、`dsh-tools`、`dsh-mcp-client`、`dsh-plugin-manager`、`dsh-app-boot`、`dsh-shell-env` | 均在 `/dsh/node_modules/@deepseek-ai/<包名>/`，版本 0.2.2-alpha.2 |

Host 插件运行在 Harness 的 Host 进程（Node/Electron 侧），不是浏览器渲染进程；
Host 插件可以直接使用 Node 内置模块。生产实例：profile 已安装的
`dsh-figma-mcp` 同时 `import fs/http/os/path/crypto` 与 `child_process.execFile`
（`~/.dsh/profiles/desktop/node_modules/dsh-figma-mcp/lib/index.js`）。

### 1.2 profile 结构（只读观测）

本机 profile：`/Users/gushaokui/.dsh/profiles/desktop`（`$DSH_PROFILE=desktop`）。

| 文件/目录 | 内容 | 与本适配的关系 |
| --- | --- | --- |
| `package.json` | 私有包 `dsh-profile-desktop`；`dependencies` 仅 3 项：`@deepseek-ai/dsh-cua-bundle@0.2.2-alpha.2`、`@local/cua-native-helper-config (link:…)`、`dsh-figma-mcp (file:…tgz)`；`dsh.profile.bundles` 列出 8 个已选 bundle | `install_bundle` 会写入依赖与 bundle 选择 |
| `cordis.patch.yml` | 用户 patch 层（bundle 层之后应用）：id 覆盖、`disabled`、`insert` 行，支持 `!!js` | 本产品安装后在此追加 `quietbridge-dsh` 行（由安装器渲染，勿手写） |
| `pnpm-workspace.yaml` | `packages: ['.']`；`nodeLinker: hoisted`；`autoInstallPeers: false` | 决定依赖布局；也解释 profile `node_modules` 为扁平布局 |
| `pnpm-lock.yaml` | 已存在锁文件 | `install_bundle` 失败时按锁文件回滚 |
| `node_modules/` | `@deepseek-ai/` 下只有 10 个包：`cosmokit`、`dsh-atomic-write`、`dsh-brand`、`dsh-computer-use`、`dsh-cua`、`dsh-cua-bundle`、`dsh-experimental-cua-macos`、`dsh-storage-domain`、`dsh-typert-protocol`、`schemastery`；另有 `@local/`、`@modelcontextprotocol/`（空）、`@standard-schema/`、`@trycua/`（空）、`@ubjs/`（空）、`dsh-figma-mcp/`、`zod/` | **没有** `@deepseek-ai/dsh-mcp-client`；见 2.8 |
| `.plugin-manager/` | 插件管理器的状态目录 | 只读观测，未修改 |

`cordis.patch.yml` 中的 `dsh-figma-mcp` 行（`insert: [{id: figma-mcp, name: dsh-figma-mcp, config: {mode: local, serverName: figma}}]`）
是“profile 依赖的第三方 Host 插件 + 手写 patch 行”的生产实例，可作为 Host 插件
确实在本 Profile 里运行的旁证。

### 1.3 证据复现方法

```sh
# 列出 asar 内文件（正则）
node /tmp/asar_list.js 'dsh-tools/(README|lib/index)'
# 提取 asar 内单个文件（OUTDIR 必须不同，避免同名覆盖）
OUTDIR=/tmp/x node /tmp/asar_extract.js /dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js
```

本轮已提取的参考资料：`/tmp/dshdocs/`（宿主插件技能文档）、`/tmp/dsh-mcp/`
（dsh-mcp-client README + lib）、`/tmp/dsh-tools/`（dsh-tools lib）、
`/tmp/dsh-agentlib/`（dsh-agent lib）、`/tmp/dsh-agentlib2/`（dsh-session lib）、
`/tmp/dsh-cordis/`、`/tmp/dsh-session/`，以及本轮新增的 `/tmp/dsh-more/`。
下文“行号”按这些提取副本计（与 asar 内文件一致）。

## 2. 已核实事实

### 2.1 bundle / 插件 manifest 与安装方式

**bundle 的定义**：一个 `package.json` 声明了 `dsh.bundle.patch` 的包；该 YAML patch
向 Loader 插入插件行（`host-plugin.md` 第 3–27 行）。Host-only bundle 不需要依赖、
安装脚本或构建工具，最小 manifest 为：

```json
{
  "name": "@local/my-plugin",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "exports": { ".": "./index.js" },
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

`cordis.patch.yml` 用 Loader patch 语法：

```yaml
- insert:
    - id: my-plugin
      name: '@local/my-plugin'
      config: {}
```

**真实 bundle 证据**（安装包与 profile 双份一致）：`@deepseek-ai/dsh-cua-bundle`
（asar `/dsh/node_modules/@deepseek-ai/dsh-cua-bundle/package.json`；profile
`node_modules/@deepseek-ai/dsh-cua-bundle/package.json`）：

- 顶层 `icon: "./icon.svg"`；
- `exports` 含 `"."`、`"./cordis.patch.yml"`、`"./locale/*.json"`、`"./package.json"`；
- `files` 含 `icon.svg`、`locale/*.json`、`cordis.patch.yml`、`lib/index.js`；
- `dsh.bundle: { listedByDefault: false, patch: "./cordis.patch.yml", platforms: ["darwin"] }`；
- `cordis.patch.yml` 中用 `!!js process.platform !== 'darwin'` 做平台禁用判断。

**展示元数据与图标要求**（`host-plugin.md` 第 29–55 行）：

- `locale/en.json`（及用户语言如 `locale/zh.json`）必须写 `meta.title`、
  `meta.description`；标题用句式大小写；不得留模板文案。
- 图标须原创或有再分发许可；支持 SVG/PNG/JPEG/WebP，上限 256 KiB，必须位于包内，
  拒绝绝对路径、URL、指向包外的符号链接；包根插件也可用 `package.json` 顶层
  `icon`（优先于 `./icon`）。
- 必须导出 `./locale/*.json` 与 `./icon`；`files` 要覆盖 patch、locale、icon 与全部
  运行时文件。**本地目录安装是 link checkout，`files` 不过滤链接目录**，要直接核对目录内容。

**安装方式**（`SKILL.md` 第 8–10 行；`references/host-plugin.md` 第 66–70 行；
`@deepseek-ai/dsh-plugin-manager/README.md` 第 52–86、124–126 行）：

- 只能通过 `plugin_manager` 的 `install_bundle`（目标为**绝对包目录**）完成安装；
  它执行 `pnpm add`（本地目录保存为 `link:<绝对路径>`）、把包加入 profile 的
  `dsh.profile.bundles`、应用 bundle patch 层。不要手写 profile 的 `package.json`
  或 `cordis.patch.yml`，也不要在 profile 目录跑 pnpm。
- 安装前会对**本机路径包**读取其 `package.json`，按其中的 `engines` 与
  `peerDependencies` 做 DSH 版本/peer 前置检查；不满足则在 pnpm 运行前拒绝。
- 失败或取消会从锁文件恢复 `package.json`/`pnpm-lock.yaml`（`pnpm-workspace.yaml`
  例外）；安装成功但激活失败会保留依赖并报 `failed`。
- 应用状态分离：`applied`（已生效）、`overridden`（更高优先层覆盖）、
  `restart-required`（无 HMR 或包目录变化时需要重启）。新 bundle 有 HMR 时可即时
  激活；替换已安装包必须重启才能加载新的 JS 模块代。
- 存在 `engines.dsh` / DSH peer 版本要求；本文建议本产品**不声明**依赖与 peers，
  由 installer 渲染的 bundle 保持零依赖，避免把 profile 依赖面扩大（见 2.8）。

**配置型 bundle 先例**：`references/mcp-bundle.md` 与
`templates/mcp/{package.json,cordis.patch.yml}` 说明“只插一行 `@deepseek-ai/dsh-mcp-client`”
的 bundle 只需唯一名字、版本与 `dsh.bundle.patch`，不需要 Host/Client 入口文件。
本产品需要自己的 Host 入口（`index.js`），因此不是配置型 bundle，但同样必须满足
manifest、locale、icon 与 `files` 清单。

### 2.2 Host 插件导出形式与 ctx API

**导出形式**（`host-plugin.md` 第 57–64 行）：下列两种择一，不可混用：

1. `export function apply(ctx, config) {}`，可选 `export const inject = ['tools']`、
   `export const Config`（Schemastery schema，用于校验该行的 `config`）；
2. 默认导出 service class。

每个资源都必须在 `apply` 内通过 `ctx.effect` 或 `ctx.on` 注册并返回清理函数；插件
卸载、Agent 释放、profile patch 变化会移除对应上下文上的注册（`practices.md` 第 8 行）。
插件加载时会用 `Config` 校验 patch 行里的 `config`，因此 patch 行配置写错会在激活期
直接报错。

**ctx API（已核实于文档与代码）**：

| API | 契约 | 证据 |
| --- | --- | --- |
| `ctx.on(event, listener)` | 事件监听；随所属 context 释放 | `cordis/README.md` 第 44–67 行；`practices.md` 第 8 行 |
| `ctx.effect(() => cleanup)` / generator effect | 资源与清理的所有权边界 | `host-plugin.md` 第 64 行；`dsh-agent/lib/index.js` `register()` 用 `this.ctx.effect(function* …)` |
| `inject` | 声明依赖服务，缺失时不激活插件，而不是抛错 | `practices.md` 第 20 行；官方插件均为 `export const inject = ['tools']`（如 `dsh-figma-mcp/lib/index.js` 第 29 行） |
| `ctx.get(name)` | 可选服务探测（缺失返回 undefined） | `dsh-tools/lib/pipeline.js` 用 `ctx.get('approval')`、`ctx.get('attachments')` |
| `ctx.logger` | Host 日志（warn/error 等） | `dsh-agent/lib/index.js` `emitDisposed()` 中的 `this.ctx.logger.warn` |
| `ctx.plugin(plugin, config)` | 在指定 context 下挂载子插件 | `cordis/README.md` 第 64–67 行 |
| `ctx.serial(event, payload)` | 串行等待监听器；`agent/created` 用它 | `dsh-agent/lib/index.js` `announce()` |
| `ctx.waterfall / ctx.events.dispatch` | 框架内部管线/派发原语，插件不应直接依赖 | `dsh-tools/lib/pipeline.js`、`dsh-session/lib/index.js` `commit()` |

`ctx.tools` 服务的 `inject` 要求：`ToolsService` 自身 `static inject = ['systemPrompt']`
（`dsh-tools/lib/index.js` 第 38 行），所以插件只需声明 `inject = ['tools']` 即可在
`apply` 期同步拿到 `ctx.tools`。不要把 `dsh-tools`、`dsh-agent`、`dsh-mcp-client`
作为 import 依赖（理由见 2.8 与第 4 节 G14）。

**事件作用域规则（已核实，决定“在哪注册监听器能收到什么”）**——`@deepseek-ai/dsh-scope/lib/index.js`
`scopeTarget()`/`createScope()`：

- **未打作用域标签的监听器（注册在根/插件级 ctx）放行所有派发**：
  `const tag = scopeOf(ctx); if (tag === undefined) return true;`
- 打了标签的监听器**只收自己作用域及其后代作用域**的事件：派发时沿目标的
  scope 父链向上走，命中标签才放行；标签在派发目标**之下**则收不到（“事件只向上流”）。
- 因此：① 插件级 `ctx.on('agent/created' / 'agent/disposed' / 'session/event' / 'tools/result')`
  能收到全部 Agent/Session 的事件；② 注册在 `agent.ctx` 的监听器能收到该 Agent 及其
  子作用域（含其 Session）的事件；③ 不要指望父 Agent 的 `agent.ctx` 监听器收到
  子 Agent 的事件——要跨 Agent 观察就用插件级 ctx 或根 ctx。

### 2.3 Agent 身份与生命周期

**`agent/created`（串行创建期事件）**

- 签名：`ctx.on('agent/created', (payload) => …)`，payload 为
  `{ agent, source, signal? }`——**不是**直接传 agent。
  证据：`dsh-agent/lib/index.js` `announce()`：
  `await this.ctx.serial(entry.carrier, "agent/created", { agent: entry.agent, source, ...signal })`。
  生产旁证：本部署已挂载的 `@deepseek-ai/dsh-experimental-agent-team/lib/index.js`
  在 service 的 `[Service.init]` 里就是 `ctx.on("agent/created", ({ agent }) => …)`（第 1474 行）。
  注意事件以 agent 的 scope carrier 为目标派发，插件级/根 context 的监听器能收到全部
  Agent 创建（同机制先例：`dsh-subagent/lib/index.js` 在 service 根 ctx 上
  `ctx.on("agent/disposed", ({ agent }) => …)`；agent-team 亦在根 ctx 上
  `ctx.on("session/event", (session) => …)`）。
- 时序：Agent 与 Session 构造 → `agent.ctx` 作用域建立 → Session 进入 store 并完成
  `session/created` → setup → 发布（`agent/created` 串行 await）→ 启动 loop。
  因此**在 `agent/created` 监听器里 await 连接器 `tools/list` 并注册工具，能保证首轮
  工具可见**；但监听器的 reject 会让 Agent 创建失败（`announce()` 直接 await 串行
  监听器，失败向上抛）。
- 官方限制（`dsh-agent/README.md` 第 207 行）：创建监听器不得 `await agent.whenIdle()`
  或等待自身 owner 的 dispose——那会等待创建完成而自锁；应只完成必要的异步工具/
  prompt 安装后返回。

**`agent/disposed`**

- 签名：`ctx.on('agent/disposed', ({ agent }) => …)`；payload `{ agent }`
  （`dsh-agent/lib/index.js` `emitDisposed()`：`["agent", "agent/disposed", { agent: entry.agent }]`）。
- 释放顺序（`dsh-agent/README.md` 第 124 行）：停 loop → 解 scope（`agent.ctx` 的
  effects 在此释放）→ 注销 agent（发 `agent/disposed`）→ 注销 session。
  因此清理连接器最稳的两个位置是：`agent.ctx.effect` 的 disposer（更早，随 scope 释放），
  以及插件级 `ctx.on('agent/disposed')` 里按 agent 身份过滤（写 `agent-disposed` 生命周期
  事件必须在这里做，因为 `agent.ctx` 监听器此时已随 scope 释放）。

**Agent 对象字段（`dsh-agent/README.md` 第 28–80、122–134 行；`dsh-agent-loop/lib/index.js` `ReactLoopAgent`）**

| 字段/方法 | 含义 |
| --- | --- |
| `agent.id` | Agent 身份；**本版本恒等于 `agent.session.id`**（见下） |
| `agent.session` | 持久 Session：`session.id`、`session.header`、`session.append()`、`session.snapshotEvents()` 等 |
| `agent.ctx` | 该 Agent 的作用域 context；工具、prompt section、事件监听、限制注册于此，随 Agent 释放 |
| `agent.status` | `'idle' | 'running'`（loop 实现按 phase 折算） |
| `agent.inbox` | `followup/steer/inject` 输入队列的结构视图 |
| `agent.model` | `current()`/`select()` 模型选择 |
| `agent.cancel(cause, options?)` | 协作取消（见 2.4） |
| `agent.whenIdle()` | 整个 Agent 静默后 resolve；**不得**在创建监听器里 await |

**身份不变量（关键）**：`dsh-agent/lib/index.js` `enter()`：
`if (agent.id !== agent.session.id) throw new Error('agent id "…" does not match session id "…"')`。
即本版本 **`QUIETBRIDGE_HOST_SESSION_ID` 与 `QUIETBRIDGE_HOST_AGENT_ID` 通常是同一字符串**；
适配器不得假设二者不同，也不得用“agent 与 session 是否不同”区分父子 Agent。

Session id 形态（已核实，但**不可解析**）：

- UI/API 新建会话：`session-${randomUUID()}`（`dsh-api-session-controller/lib/index.js` 第 422 行）；
  子 Agent 子会话同形（同文件第 786 行）。
- 配置驱动的 Agent：`${id}-session-${randomUUID()}`（`dsh-agent-loop/lib/index.js` 第 1350 行）。
- **本会话实测**：`$DSH_SESSION_ID = 63091586-9456-4f00-b1f4-2e9432a1951e`（裸 UUID，无前缀）。
  该变量由 `@deepseek-ai/dsh-shell-env/lib/index.js` 第 89 行在每次 shell 工具执行时
  注入，值就是 `execution.agent.session.header.id`，而 `dsh-session` 中
  `session.id === header.id`。→ **Session id 形态随入口而变，禁止按前缀/后缀解析**。
- `DSH_SESSION_ID` 是 shell 工具的 per-execution 环境变量，**不在** Host 进程 env 中；
  bundle 取身份只能用 `agent.session.id` / `agent.id`。

**agent.ctx 作用域注册语义**（`dsh-agent/README.md` 第 74 行；`dsh-tools/lib/index.js`
`ToolLayer` 与 `register()`）：

- 通过 `agent.ctx` 注册的工具只对该 Agent 可见，且可遮蔽全局同名工具；
- 同名工具有作用域内重复会报错；不同 Agent 各自注册同名工具互不冲突；
- 一个注册在 `agent.ctx` 上的资源有**两个 owner**：插件卸载不会自动清理 `agent.ctx`
  注册，必须同时把 disposer 放进插件自己的 effect（`practices.md` 第 8 行）。

**可选的会话终止信号：`session/disposed`**：`SessionStore` 移除一个已完成
`session/created` 宣告的 Session 时派发 `session/disposed`，载荷是 **session 对象**
（一个参数），派发用 `invokeContainedSessionObservers` 做逐监听器容错
（`dsh-session/lib/index.js` `emitDisposed()` 第 1942–1955 行；`dsh-session/README.zh.md`
第 34 行）。它与 `agent/disposed`（载荷 `{agent}`）是两条不同的边：`agent/disposed` 表示
“这个 Agent 不在了”，`session/disposed` 表示“这个 Session 从 store 移除”。由于本版本
`agent.id === session.id`，生命周期 store 以 `agent/disposed` 作为 sessionEnded 主信号
足够；但若插件只登记了 session（例如某入口只创建 Session 未建 Agent），可额外订阅
`session/disposed` 兜底。归档/fork 等场景也会移除 Session，因此不能把
`session/disposed` 单独当成“用户点了停止”。

### 2.4 Session 持久事件：`turn/start`、`turn/end`（确切 API）

**订阅方式（已核实）**：持久事件从 `session/event` 流出，不是 `agent/*` 事件。

```js
// 在 agent/created 内（agent.ctx 是 Agent 作用域 ctx）
agent.ctx.on('session/event', (session, event) => {
  if (session.id !== agent.session.id) return
  if (event.type === 'turn/start') { /* event.data = { turn } */ }
  if (event.type === 'turn/end')   { /* event.data = { turn, reason } */ }
})
```

证据链：

1. `dsh-session/lib/index.js` `commit()` 在事件入日志后派发
   `[entry.carrier, "session/event", session, event]`，并且事件信封是冻结对象
   `{ type, seq, time, data, ignorable }`。
2. `dsh-agent-loop/lib/index.js` 在 Agent 作用域 ctx 上照此订阅并过滤 session：
   `const stopRecovery = this.ctx.on("session/event", (session, event) => { if (session === this.session) … })`
   （`ReactLoopAgent` 的 `this.ctx` 就是 `createScope(loopCtx, this)` 得到的 agent ctx）。
3. `dsh-subagent/lib/index.js` 对子 Agent 用
   `activation.handle.agent.ctx.on("session/event", (_session, event) => …)`。

补充：独立 `Session.create()/fromRestore()` 实例不发 `session/event`，只有进入
`SessionStore` 且 `session/created` 宣告完成后才发（`dsh-session/README.zh.md` 第 34 行）。
因此插件启动前已有的历史不会被重放——生命周期 store 不应假设能补收启动前事件。

**`turn/start` 载荷**：`data = { turn: number }`。
证据：`dsh-agent-loop/lib/index.js` `turn()` 中
`this.session.append("turn/start", { turn })`（第 731 行）。

**`turn/end` 载荷**：`data = { turn: number, reason }`。证据：同文件第 819 行
`this.session.append("turn/end", { turn, reason: turnEnds })`；崩溃/中断恢复路径由
`dsh-session/lib/index.js` `interruptedTurnClosers()`（第 750–795 行）写入
`reason: { kind: cause.kind }`。

`reason.kind` 的取值（`dsh-agent-loop` 的生成路径全量枚举）：

| `reason.kind` | 产生路径 | 建议映射到 lifecycle |
| --- | --- | --- |
| `completed` | 正常结束（step 返回 completed / 无更多输入） | `completed` |
| `blocked` | `agent/pre-step` 拒绝（该轮未进入任何 step） | `completed`（轮次已收口，非用户取消） |
| `max-tokens` | 达到输出上限被截断收口 | `completed` |
| `aborted` | `agent.cancel(cause)` 导致 signal abort | `cancelled`；细节在 `reason.reason.kind` |
| `error` | 步骤抛错（`reason.error = { message, code }`） | `error` |
| `interrupted` | 崩溃/中断恢复补写（session 侧 closer） | `interrupted` |
| `forked` | fork seed 历史补写（仅构造期） | 忽略（不是真实轮次结束） |

`aborted` 的 `reason.reason.kind` ∈ `user` | `parent` | `disposed` | `hook`
（`hook` 另带 `reason` 文本）。证据：`dsh-agent-loop/lib/index.js` `abortedCancelCause()`
（第 487–503 行）逐个 `case` 复制字段；用户 Stop 按钮即 `agent.cancel({ kind: 'user' })`
（`dsh-agent/lib/index.js` `installTurnArchiveAdmission` 与 `dsh-agent/README.md` 第 126 行）。

**取消路径（已核实）**

- `agent.cancel(cause, options?)` 协作取消；`cause.kind` ∈ `user` | `parent` | `disposed` | `hook`；
  默认清空 inbox（排队与 steer 输入都会丢），`{ keepInbox: true }` 只终止当前轮；
  没有“只终止 step、保留 turn”的 API（`dsh-agent/README.md` 第 208 行）。
- 对工具调用：`exec.signal` 是调用方 signal；取消发生在“调用主体尚未开始时”为
  `TOOL_ABORTED_BEFORE_DISPATCH = 'ABORTED_BEFORE_DISPATCH'`，主体开始之后只能把
  成功结果替换为 `ABORTED`（`dsh-tools/lib/types/contracts.js`；`dsh-tools/README.zh.md`
  第 128 行）。工具批量日志还会为未派发调用补一条
  `content: [{"type":"text","text":"Error: tool call aborted before dispatch"}]`、
  `error.info.code = ABORTED_BEFORE_DISPATCH` 的 `tool/result`（`dsh-tools/lib/types/batch.js`
  `BatchSettlement.skip()`）。
- **注意**：`ABORTED_BEFORE_DISPATCH` 是工具侧结果码，**不是** `turn/end` 的 reason；
  生命周期映射不要把两者混用。

**turn 号是会话内整数，不是全局 turnId**：`turn` 从 1 递增（loop 的 `phase.turn + 1`）。
跨会话/跨 Agent 复用同一数字，生命周期 store 必须以
`宿主 + sessionId(+agentId) + turn` 定位；建议 `turnId = \`${sessionId}:${turn}\``。
本文档及 adapter 描述里不再使用“payload 里有 turnId 字段”的说法。

**其它 session 持久事件对本适配有用**（均来自 `dsh-session/lib/index.js`
`KNOWN_SESSION_EVENT_TYPES`）：`step/start`、`step/end`、`tool/call`、`tool/result`、
`user/message`、`assistant/message`、`request/header`。**禁止**用新 `type` 追加事件
（`practices.md` 第 21 行：live `Session.append()` 无法设置 `ignorable`，会让 Session
重新打开失败）。插件自己的数据应写外部 store（本产品的 lifecycle store 即如此）。

### 2.5 `tools/result` 事件与 `exec.agent`

**订阅签名（已核实）**：`agent.ctx.on('tools/result', (exec, result) => …)`。

- 证据：`dsh-subagent/lib/index.js` 在子 Agent ctx 上
  `childCtx.on("tools/result", function (exec, result) { … })`；
- 派发实现 `dsh-tools/lib/pipeline.js` `notifyResult()`：
  `this.ctx.events.dispatch('emit', [scopeTarget(this.tools, exec.agent), 'tools/result', exec, result])`，
  即按**调用 Agent 的作用域**派发；根 ctx 与祖先 ctx 的监听器也能收到。
- 只观测：监听器返回值被忽略，抛错/拒绝只 `logger.warn`，不会改变调用结果
  （`notifyResult()` 内 `reportFailure`）。
- 时机在流水线最末端：`tools/pre-execute → guard → tools/execute → tools/post-execute
  → finalizeContent → tools/result`，且 `exec` 在派发前 `Object.freeze`。

**payload 字段（已核实）**

`exec`（冻结）：

| 字段 | 说明 |
| --- | --- |
| `name` | 模型可见的公开工具名 |
| `callId` / `rootCallId` | 本次调用/根调用标识（与 session `tool/call` 的 `callId` 对应） |
| `arguments` | 深冻结的已校验参数 |
| `agent` | **调用方 Agent**（仅模型步骤内的调用有；无 agent 的服务调用没有此字段） |
| `token` | 本次执行的内部关联 token |
| `signal` | 调用方 signal（协作取消） |
| `parent` / `toolSet` / `schema` | 子调用/步骤工具集/模式信息（视调用而定） |
| `deferContext()` / `concludeTurn()` | 追加后续上下文 / 声明本轮可结束 |

`result`：`{ isError, error?, value?, content, meta?, additionalContexts?, concludesTurn? }`。
其中 `content` 是 `render` 产出的最终内容（已过 `post-execute`/`finalizeContent`），
`value` 是规范 JSON 值。

**已核实的缺口：`exec` 里没有 turn 信息**。若要让生命周期回执带 turn，有两条路：

1. **推荐——用 session 事件的 `tool/call` / `tool/result`**（都在同一 `session/event`
   流里，带 turn）：
   - `tool/call`：`data = { turn, step, callId, name, arguments }`
     （`dsh-tools/lib/types/batch.js` `BatchSettlement.start()` 第 210 行）；
   - `tool/result`：`data = { turn, step, message, error?, meta? }`，`message` 是
     tool 角色消息（`source.callId` 指回调用），`error?.info` 带结构化错误码
     （同文件 `commit()` 第 218 行；`BatchSettlement.skip()` 给出
     `ABORTED_BEFORE_DISPATCH` 的例子）。
   这样 turn、工具名（从 `tool/call` 配对）、结果都在持久事件层，且与 session 日志一致。
   注意 `tool/result` 可能被 surface 替换（同 type、`surfaceOp: 'replace'`），按最新
   事件记录即可。
2. `tools/result` 观察 + 订阅 `turn/start`/`turn/end` 自维护“当前打开 turn”。优点是能拿到
   `exec.agent`/`exec.arguments`/`exec.token`；缺点是 turn 归属是推导的，边界上存在
   轻微竞态（事件先后由 loop 提交决定），且图片等 `value` 可能很大，转发 lifecycle CLI
   前必须裁剪。

**回执转发的注意点**：`result.value`/`content` 可能包含截图 base64（本仓库 connector
的 `tools/call` 返回 `{"content":[{"type":"image","data":…,"mimeType":"image/png"}]}`，
见 `services/mcp/server.py` 第 2312、2470、2558 行）。生命周期 CLI 走 stdin JSON，
必须只转发名称、参数摘要、turn、结果状态/文本摘要，禁止把整段 base64 塞入。

### 2.6 `ctx.tools.register` 原始 definition 的要求

**硬要求（注册期强校验，违反直接 throw）**——`dsh-tools/lib/index.js` `register()`
第 60–105 行：

- `definition.name`：字符串；
- `definition.description`：字符串；
- `definition.parameters`：JSON Schema 对象（**注册期不做受支持子集校验**，但必须
  lossless JSON；`projectToolSchema()` 会重新快照）；
- `definition.output` 必须是对象，且 `output.schema` 存在、`output.render` 是函数
  （`presentationMeta` 可选）；否则
  `TypeError: tool "<name>" must declare output { schema, render, presentationMeta? }`；
- `output.schema` 会立刻过 `assertSupportedJsonSchema(output.schema)`；
- `definition.execute(args, exec)`：唯一执行入口；`exec.signal` 协作取消；
- 可选 `projectContent(exec, result)`、`finalizeContent(exec, result)`、
  `timeoutMs`（仅声明，不强制执行；需 `@deepseek-ai/dsh-tool-call-timeout-policy` 才生效）、
  `isConcurrencySafe(args)`、`deferLoading`、`presentCall/presentResult`；
- `name === 'run_code'` 恒定保留，不可注册或遮蔽；
- 返回 disposer；同名重复在**同一层**报错（全局 vs agent 作用域是不同层）。

**`output.schema` 允许的 JSON Schema 子集**（`dsh-tools/lib/types/json-schema.js`
第 1–12、28–39、144–145、209、233–247 行）：

| 允许 | 说明 |
| --- | --- |
| 任意 JSON 根 | 根不强制 object（工具输出允许任意 JSON） |
| `{}`（纯注解） | 表示不受限 JSON |
| 单个字符串 `type` | `object`/`array`/`string`/`number`/`integer`/`boolean`/`null`；**不支持 type 数组** |
| `properties`、`required` | object 子集 |
| 布尔 `additionalProperties` | 只允许布尔，不允许 schema |
| `items` | array 子集（单个 schema） |
| `enum` / `const` | 只允许类型正确的标量值；两者同时出现时 `const` 必须属于 `enum` |
| `oneOf` | ≥2 个分支、**恰好匹配一个**；不得与 `type/properties/required/additionalProperties/items/enum/const` 同时出现 |
| 注解 | `description`、`title`、`default`、`examples` |

不支持（会以 `JsonSchemaError: unsupported JSON schema: …` 拒绝注册）：
`anyOf`、`allOf`、`not`、`$ref`、`$defs`、`format`、`pattern`、`minLength`/`maxLength`、
`minimum`/`maximum`、`multipleOf`、`uniqueItems`、`type: [...]` 等。

因此“最宽松且必过”的输出 schema 形如（与官方 MCP 客户端一致）：

```js
{ type: 'object',
  properties: { content: { type: 'array', items: {} } },
  required: ['content'],
  additionalProperties: false }
```

（或再加 `structuredContent: {}` 并放进 `required`；官方 `createOutput()` 就是这么做的。）

**`parameters` 的实测差异**：`register()` 只校验 `output.schema`；`parameters` 原样
进入系统提示与模型请求（`projectToolSchema()` 仅做 lossless 快照）。也就是说 MCP
`inputSchema` 里出现 `$ref` 等关键字**不会**在注册期被拒，但会原样发给模型提供方，
风险由提供方决定。本产品建议在连接器侧把参数 schema 规范化到上面的受支持子集，
避免“注册成功但模型请求失败/被忽略”的不一致。

**`render` 的内容词表**（`dsh-tools/lib/pipeline.js` `createSuccessResult()` 第 719–740 行、
`snapshotProjection()` 第 786 行）：

- `render(args, value)` 是**同步**函数，返回值被 `snapshotJsonValue` 做 lossless 快照，
  注册/执行期**不做 content block 结构校验**；
- 但模型内容词表只接受 `{ type: 'text', text }` 与 `{ type: 'image', attachment: <AttachmentRef> }`
  （后者由附件存储产生）。官方 MCP 客户端即：`render` 只产出文本
  （`extractText`），图片经 `projectContent()` + `ctx.get('attachments')` +
  `llm.resolveModelInfo(…).inputModalities.includes('image')` 准入后以
  `{ type: 'image', attachment: ref }` 投影（`dsh-mcp-client/lib/index.js`
  `createOutput()`、`prepareImageProjection()`）。
- **结论**：本轮 bundle 的 `render` 只能输出文本块。MCP 结果里的
  `{type:'image', data, mimeType}` 不能直接塞进 render（会得到无人消费/请求组装失败
  的块）；应转成文本占位（如 `[screenshot: 1280x800 png, 12345 bytes]`）或
  `[image unavailable: …]` 形式。若后续要真正看图，需要：
  Host 侧在 definition 上实现 `projectContent()`，`inject` 可选 `attachments` 与 `llm`，
  按模型 image 能力准入并 `saveImages()` 得到 attachment ref；这需要真实宿主验收
  （附件服务是否在本 profile 挂载、模型是否声明 image 输入均未验证）。

**工具名约束**（模型函数名契约）：`dsh-mcp-client` 的公开名契约是
`mcp__<serverName>__<rawName>`，规范化后必须满足 `[A-Za-z0-9_-]{1,64}`，有损规范化
时追加 12 位 SHA-256 后缀（`dsh-mcp-client/lib/index.js` `publicToolName()` 第 80–110 行；
`mcp-client/README.md`“Tool naming and coexistence”）。自实现映射时建议本产品沿用
`mcp__quietbridge__<raw>` 且总长 ≤64，避免与其它 MCP 命名冲突和提供方截断。

### 2.7 MCP 客户端（`@deepseek-ai/dsh-mcp-client`）

**配置字段**（`dsh-mcp-client/README.md` 配置表；`lib/index.js` `Config` 第 781 行）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `transport` | 必填 | `stdio` 或 `streamable-http` |
| `serverName` | 必填 | 命名空间，`[A-Za-z0-9_-]{1,32}`，同一注册作用域唯一；工具名 `mcp__<serverName>__<rawName>` |
| `command` / `args` / `env` / `cwd` | — | stdio：可执行文件、参数、在“已剥离凭据形状与陈旧 DSH_* 变量”的父 env 之上的显式 env、工作目录 |
| `url` / `headers` | — | streamable-http |
| `toolCallTimeoutMs` | 60000 | 单次调用/资源请求超时 |
| `maxInstructionBytes` | 32768 | 服务器 instructions 上限，超限拒绝连接 |
| `failOnStartupError` | false | 初次连接/工具同步失败时使插件激活失败 |
| `reconnect.enabled` / `initialDelayMs` / `maxDelayMs` / `maxAttempts` | true / 500 / 30000 / 10 | 断线重连策略 |

其它已核实行为：

- 官方实现用 `@modelcontextprotocol/client` SDK；stdio 会**先起一个临时探测进程**
  再起服务进程（README“Startup, updates, and reconnection”之前的总述）。
- 工具发现失败保留上一代工具集；注册冲突整代回滚；`tools/list_changed` 触发重同步。
- 导出的公开面：`apply`、`Config`、`inject`、`name`、`createMcpToolDefinition`
  （`lib/index.js` 第 836 行 `export { Config, apply, createMcpToolDefinition, inject, name }`）。
- 工具定义回调能拿到 `exec.agent` 与 `exec.signal`；图片路径见 2.6。

**为什么不用它的 profile row**：静态 row 是**全局作用域**，一个 `serverName` 对应一条
连接、一份 env，无法为每个 Agent 注入不同的 `QUIETBRIDGE_HOST_SESSION_ID` /
`QUIETBRIDGE_HOST_AGENT_ID`；多条 row 需要预先知道 Agent 存在，且仍共享全局工具命名。
**programmatic mount**（在 `agent.ctx` 下 `ctx.plugin(mcpPlugin, config)`）在 API 上
未被禁止，但：(a) 需要 import 宿主内部包；(b) config 仍是单一 `serverName` 维度、
env 在插件激活时固定，无法自然做到“每次 agent/created 用该 agent 身份新建连接”；
(c) 没有官方文档/测试把它当稳定用法。故本轮不采用。

### 2.8 安装与依赖解析

**基础环境**：profile `pnpm-workspace.yaml` 为 `nodeLinker: hoisted`；`node_modules`
是扁平布局，只含 1.2 列出的 10 个 `@deepseek-ai` 包。

**bundle 声明 dependency 是否可行**：可行但不必要。`install_bundle` 走 `pnpm add`，
会把声明的依赖装进 profile（`dsh-plugin-manager/README.md` 第 54、68 行）；安装前
会读本机路径包的 `package.json`，按 `engines` / `peerDependencies` 提前检查
（同文件第 77 行），并在失败/取消时恢复 `package.json` 与 `pnpm-lock.yaml`。
代价是扩大 profile 依赖面、引入 peer 版本门禁与升级耦合；本轮自包含 bundle 保持
**零 dependencies / 零 peerDependencies**。

**`@deepseek-ai/dsh-mcp-client` 是否已在 profile 可解析**：**静态解析不可用**。

- profile `node_modules/@deepseek-ai/` 无此包；
- 在 profile 目录用 Node 实测：
  `require.resolve('@deepseek-ai/dsh-mcp-client/package.json')` → `MODULE_NOT_FOUND`；
  `import('@deepseek-ai/dsh-mcp-client')` → `ERR_MODULE_NOT_FOUND`。

但要注意宿主运行时的“profile resolution”内部层：`dsh-app-boot/lib/worker/profile-resolution-bootstrap.js`
会拦截 Node 的 ESM/CJS 解析，按 name → `{scope, packageDir}` 路由；当目标
`scope === "installation"` 时也给 profile 模块建立路由（第 99、127、448 行附近）。
生产旁证：profile 里已安装的第三方插件 `dsh-figma-mcp` 的 `lib/index.js` 就
`import { createMcpToolDefinition } from '@deepseek-ai/dsh-mcp-client'` 且在该 profile
中运行。也就是说**宿主运行时可能允许 profile 插件 import 安装副本里的内部包**，
但这是安装包内部实现、未写进任何插件文档或稳定契约，且版本升级/替换时的路由
行为（`restart-required`、`package-moved`）不可控。故本轮继续采用“零内部 import +
自实现最小 stdio 客户端”的方案，而不是把可行性建立在它上面。

**与 connector 的协议兼容性（本仓库侧，非 DSH 契约）**：`services/mcp/server.py` 是
手写 MCP stdio 服务，`DEFAULT_PROTOCOL = "2024-11-05"`（第 109 行），`initialize`
**原样回显**客户端 `protocolVersion`（第 3511–3535 行），并声明
`capabilities.tools.listChanged = false`；无第三方 SDK 依赖。因此自实现客户端只需
按 JSON-RPC 2.0 + newline 分帧、`initialize`/`notifications/initialized`/`tools/list`/
`tools/call`/`notifications/cancelled` 与 `notifications/tools/list_changed`（防御性）即可，
版本协商是自洽的。若未来换成官方 MCP server（会真正协商版本），客户端需按 SDK
协商结果行事——属于后续风险。

## 3. 未核实 / 无法核实（本轮不宣称）

1. **未做真实 profile 安装与真实 Agent 端到端运行**：`plugin_manager install_bundle`、
   Profile 重载、tools/list → 首轮可见、turn/end 与 lifecycle store 时序、取消传播均
   只有源码级证据，没有本机实测。验收前不得宣称“已适配可用”。
2. **原生预览宿主窗口证据缺失**：未找到任何公开 API 能让 Host 插件取得 DSH 自身
   （Electron 主窗口/Web UI）的原生窗口句柄、窗口号或预览锚点。证据：插件文档只描述
   Web Client slot 渲染（`ui-plugin.md`、`practices.md` 第 31–38 行）；`/lib/main.js`
   中 `preview` 仅出现在 “previewsystems” 一处，`BrowserWindow` 5 处，均为壳内部实现；
   `dsh-experimental-cua-macos` 的 `windowId` 是**被操作目标应用**的 AX/截图窗口标识
   （`README.zh.md` 第 43、95–113 行），不是宿主窗口。→ 本轮不实现、不宣称 DSH 预览；
   原生侧仍需 Swift 后续工作（历史宿主选择逻辑不能当作 DSH 已支持）。
3. **签名 helper 的 TCC 归属未核实**：连接器由 Host 进程（DSH）经 Python 拉起，helper 再由
   Python 拉起。Accessibility / Screen Recording 授权最终归属 DSH App、DSH Helper、
   QuietBridge helper 还是 Python 解释器，取决于 macOS 责任进程判定，未实测；
   静默截图在无 Screen Recording 时必然失败。本轮只做 `helperPath` 配置。
4. **并发子 Agent 未验收**：本部署已挂 `@deepseek-ai/dsh-experimental-agent-team`
   （`/dsh/package.json` 依赖），当前会话本身存在 lead + 多个 teammate 的多 Agent 共存，
   说明 `agent/created` 会为子 Agent 独立触发；但 QuietBridge 的每 Agent 连接器、
   每 Agent 工具、容量 3 上限、A 停止不影响 B 等行为**没有真实验收**。`supportsConcurrentAgents`
   保持 `false`。
5. **Agent/连接器进程数量与资源**：每次 `agent/created` 一个连接器进程（外加 helper 连接），
   子 Agent 可能很多；本轮未定义/实测上限与回收。建议 bundle 侧对连接器数量设上限并在
   上限时降级为“该 Agent 无 QuietBridge 工具 + 日志”，而不是让宿主 OOM。
6. **HMR / 升级路径未实测**：bundle 重载、连接器旧进程清理、profile 包替换后的
   `restart-required` 行为未验证。
7. **Python 解释器解析**：Electron 从 Finder 启动时 PATH 往往不含 `/usr/local/bin`；本轮
   建议配置项默认绝对路径 `/usr/bin/python3`（本仓库要求 Python 3.9+ 且无第三方依赖），
   但未在真实 Host 进程里实测 PATH 与 TCC。
8. **`agent/created` 期间 await 连接器就绪的耗时/首轮延迟未实测**：串行监听器会推迟
   Agent 首次可用；需要真实测量再决定是否改为“先注册静态 schema、连接器异步补齐”。

## 4. API 缺口与风险清单

| 编号 | 缺口/风险 | 证据 | 影响与处置 |
| --- | --- | --- | --- |
| G1 | `tools/pre-execute` 有意不允许改写 `exec.arguments` | `dsh-tools/README.zh.md` 第 197 行；`pipeline.js` pre-execute 决策只有 allow/deny/ask；改写仍是 proposed Agent Note | Workbuddy 式“工具参数注入一次性路由 token”不可移植；必须走 env 可信身份（本轮设计正确） |
| G2 | `tools/result` 的 `exec` 无 turn 字段 | `pipeline.js` `createExecution()` 组装的 exec 只有 token/callId/name/signal/agent… | 生命周期回执的 turn 需从 session 事件推导（推荐 `tool/call`+`tool/result`） |
| G3 | turn 是会话内整数，无全局 turnId | `dsh-agent-loop` `turn()` 自增；`turn/end` 只带数字 | store 必须按 host+session(+agent)+turn 定位；建议复合 turnId |
| G4 | 图片块需要 attachment 引用，base64 不能直接进 render | `dsh-mcp-client` `prepareImageProjection()`；`createOutput()` | 本轮截图只能文本降级；不要宣称模型能看到 DSH 里的画面 |
| G5 | 无公开的宿主原生窗口/预览锚点 API | 见 3.2 的检索结果 | DSH 预览绑定本轮不可行，留给 Swift 原生侧 |
| G6 | helper TCC 归属未知 | macOS 责任进程语义；未实测 | 只配置；文档与服务端不得声称已授权 |
| G7 | `agent/created` 监听器抛错会使 Agent 创建失败 | `dsh-agent/lib/index.js` `announce()` 直接 await 串行监听器 | 连接器启动/初始化错误必须内部 catch + warn + 降级无工具；不能依赖 failOnStartupError 阻断 Agent |
| G8 | 创建期 await 会推迟首轮 | 同上；创建监听器串行 | 限时（建议 ≤5s）、超时降级；真实耗时待测 |
| G9 | `agent.ctx` 注册的清理有两个 owner | `practices.md` 第 8 行 | 连接器/工具 disposer 必须同时挂插件 effect（按 agent id 索引），防止插件卸载泄漏 |
| G10 | `agent/disposed` 时 agent scope 已解，agent.ctx 监听器已释放 | `dsh-agent/README.md` 第 124 行释放顺序 | 写 `agent-disposed` 生命周期事件要用插件级 `ctx.on('agent/disposed')` 过滤身份；连接器进程回收优先放在 agent ctx effect |
| G11 | 没有会话级“停止/取消”事件给外部插件主动调用；只能观察 | 取消入口是 `agent.cancel`（handle 能力）；插件拿不到 AgentHandle | 本插件只需观察；不要尝试反向取消别人的 Agent（`ctx.agents.get()` 只给裸 Agent，没有 handle 的 cancel 权限语义） |
| G12 | 生命周期 CLI 每事件 spawn Python 的开销 | 方案设计（brief §3） | turn/end 处于用户停止/续跑关键路径；建议短超时 5s、失败吞掉；后续改常驻管道或批量 |
| G13 | `tool/result` 的 `value/content` 可能含大 base64 | `services/mcp/server.py` 第 2312、2470、2558 行返回 image 块 | 转发 lifecycle 前必须裁剪成摘要 |
| G14 | 宿主内部包 import 在 profile 静态不可解析，运行时依赖内部拦截层 | 1.3/2.8 的实测与代码 | 零内部 import；bundle 只用 Node 内置模块 + `ctx` 公开 API |
| G15 | DSH 处于 alpha（0.2.2-alpha.2），契约可能变 | 版本证据 | 文档记录版本与证据；不要把 `dsh.engines` 卡死；安装前复核契约 |
| G16 | `server.py` 返回非标准字段（如 `_quietbridgeRetained`） | `services/mcp/server.py` reply 构造 | 自实现客户端必须忽略未知字段；官方 SDK 会按 spec 校验（未来换官方客户端需清理扩展字段） |
| G17 | 工具名冲突/截断 | mcp-client `publicToolName`、`run_code` 保留 | 若经 MCP 客户端注册，公共名须 `mcp__quietbridge__<raw>`、≤64；本轮 bundle 直接注册 Agent 作用域工具，**采用原始 `quietbridge_*` 名**（wire 名即原始名，且与仓库 `quietbridge_*` 命名要求一致）。不要用 `run_code` |
| G18 | `output.schema` 子集很窄 | `json-schema.js` | 输出 schema 只写 `{type:'object', properties:{content:{type:'array',items:{}}}, required, additionalProperties:false}` 这类形状；不要用 `$ref`/`anyOf` |

## 5. 对本轮自包含 bundle 方案的逐项判定

方案基线（本轮设计简报，未随仓库提交）：Host-only bundle，零 DSH 内部 import；
`agent/created` 时每 Agent 启动独立 stdio JSON-RPC 连接器（env 注入可信身份）；
`tools/list` 的 schema 用 `ctx.tools.register` 原始 definition 注册到该 Agent 作用域；
订阅 session 生命周期事件写 Python lifecycle store；`services/mcp` 增加宿主上下文路由。

| 设计项 | 判定 | 依据 | 必须调整的点 |
| --- | --- | --- | --- |
| 自包含 Host-only bundle，零内部 import | **可行** | 2.1、2.8；官方 Host-only bundle 不需依赖 | manifest：唯一包名、`private`、`type:module`、`exports`（`.`、`./locale/*.json`、`./icon`）、`files` 全覆盖、`dsh.bundle.patch`；补 `locale/en.json`、`locale/zh.json` 的 `meta.title/description` 与原创 `icon.svg`（≤256 KiB） |
| 每 Agent 一个 stdio 连接器 | **可行，需调整** | 2.3、2.7；Host 可用 `node:child_process`（figma 实例） | ① 只能在 `agent/created` 里按 `payload.agent` 启动；② 启动/`initialize`/`tools/list` 必须 try/catch + 超时，失败只 warn 并让该 Agent 无工具，禁止让创建失败；③ 连接器随 `agent.ctx.effect` + 插件级 effect 双清理，`agent/disposed` 兜底；④ `python3` 用绝对路径可配置（建议 `/usr/bin/python3`）；⑤ 连接器数量设上限（建议与容量 3 对齐，超限拒绝而不是无限 spawn）；⑥ 子进程 stdin 关闭/父进程退出时必须自杀，避免孤儿 |
| `ctx.tools.register` 原始 definition、输出宽松 schema | **可行，需调整** | 2.6 | ① `output.schema` 限受支持子集（G18 形状）；② `parameters` 用 MCP `inputSchema` 前先规范化，去掉 `$ref/$defs/anyOf/format/pattern/min*` 与 type 数组；③ `render` 同步、只返回 `{type:'text', text}`（图片文本降级，G4）；④ 工具名沿用原始 `quietbridge_*`（Agent 作用域、`[A-Za-z0-9_-]{1,64}`、避开 `run_code`；若未来改经 MCP 客户端注册，公共名才是 `mcp__quietbridge__<raw>`）；⑤ `execute` 里把 `exec.signal` 接到 `notifications/cancelled`；⑥ 注册动作必须在 `agent/created` 内完成（首轮可见），disposer 双 owner |
| 每 Agent 工具隔离（不做全局注册） | **可行** | 2.3、2.6（agent 作用域注册/遮蔽/清理） | 不要用根 `ctx.tools.register` 注册 quietbridge 工具（会全局可见、跨 Agent 共享）；只在 `agent.ctx` 注册。若某 Agent 连接器失败，不要回退到全局注册 |
| 订阅 `turn/start`、`turn/end` | **需调整** | 2.4 | ① API 是 `agent.ctx.on('session/event', (session, event) => …)`；② 事件类型 `turn/start` / `turn/end`，payload `event.data` 为 `{turn}` / `{turn, reason}`；③ 没有 `turnId` 字段，turnId 用复合键；④ reason 枚举与映射见 2.4 表（aborted→cancelled、error→error、interrupted→interrupted、completed/blocked/max-tokens→completed、forked 忽略）；⑤ 不要等 `agent/status` 轮询（先例：practices.md 第 28 行） |
| 用 `tools/result` 做工具回执 | **需调整** | 2.5 | ① `tools/result` 是 `(exec, result)`，exec 冻结、含 `agent`/`name`/`arguments`；② **没有 turn**：回执要带 turn 就得同时订阅 session 的 `tool/call`/`tool/result`（推荐），或用“当前打开 turn”推导；③ 只转发摘要，裁剪 base64；④ 监听器不要 throw |
| lifecycle CLI（stdin JSON、失败吞掉） | **可行但降级** | brief §3；G12 | 失败/超时只 warn；turn/end 路径短超时；不要因为 CLI 失败影响 Agent；建议后续改常驻/批量；store 增加 `host`/`agentId` 字段，requestId 语义=复合 turnId |
| env 身份契约（QUIETBRIDGE_HOST*/SESSION_ID/AGENT_ID） | **可行** | 2.3；`DSH_SESSION_ID` 来源代码 | ① 只用 `agent.session.id`/`agent.id` 注入，不读 Host 进程 env、不信模型自报；② 两 ID 本版本相同，不得假设不同；③ ID 校验只做宽松 `[A-Za-z0-9_.:-]{1,128}`，不解析前缀（实测有裸 UUID 与 `session-<uuid>` 两种形态）；④ 三者缺一/非法时连接器启动失败（failOnStartupError 语义由 connector 实现） |
| `services/mcp` 宿主上下文路由 | **可行** | brief §4；本仓库 `server.py`/`session_routing.py` 现状 | 依赖 lifecycle store 中该 session+agent 的最新 turn：无记录→拒绝、已 stopped→拒绝、有打开 turn→放行并回填 turnId；参数里出现 `_quietbridgeRoute` 视为非法；token 模式行为不变 |
| profile row（`@deepseek-ai/dsh-mcp-client`） | **不可行** | 2.7 | 全局作用域无法逐 Agent 注入身份；多 row 需预知 Agent；放弃 |
| programmatic mount `dsh-mcp-client` | **不可行（本轮）** | 2.7、2.8、G14 | 需内部包 import + 未验证用法 + 无法自然按 Agent 建连接；不改用 |
| bundle 声明 dependency | **可行但无必要** | 2.8 | 保持零依赖；安装器渲染 bundle 时也不要写入 dependencies |
| 原生预览宿主窗口绑定 | **不可行（本轮）** | G5 / 3.2 | 不实现、不宣称；adapter `blockingRequirements` 保留该项 |
| helper TCC 归属 | **未核实** | 3.3 | 只配置 `helperPath`；产品文案写明未验证 |
| 并发子 Agent | **未验收** | 3.4 | `supportsConcurrentAgents: false`；容量 3；超限明确拒绝 |
| 图片/截图结果进入模型上下文 | **不可行（本轮）** | G4 | 文本降级；若后续要做，见 2.6 的 `projectContent` + attachments 路径，需真实验收 |

### 5.1 必须调整的关键点（给 bundle 实现者的硬约束）

1. `agent/created` 的 payload 是 `{ agent, source, signal? }`；监听器必须返回而不让异常外泄。
2. 工具注册只能发生在 `agent.ctx`，且在 `agent/created` 内 await 完成；`inject = ['tools']`。
3. `output.schema` 用受支持子集（禁止 `anyOf`/`$ref`/type 数组等）；`render` 只返回文本块。
4. 工具名 `[A-Za-z0-9_-]{1,64}`、前缀 `mcp__quietbridge__`，避开 `run_code`。
5. turn 事件订阅用 `session/event`，payload `{turn}`/`{turn, reason}`；turnId 自组复合键。
6. 工具回执的 turn 取自 session 的 `tool/call`/`tool/result`，或自行跟踪打开的 turn；
   `tools/result` 仅作补充观察。
7. 身份只取 `agent.session.id`/`agent.id`，二者恒等；不解析 ID 前缀，不读宿主进程 env。
8. 连接器失败一律降级（该 Agent 无工具 + warn），绝不阻断 Agent 创建或 turn。
9. 生命周期 CLI 失败必须吞掉；turn/end 路径不得被它阻塞。
10. 不 import 任何 `@deepseek-ai/*`；只用 Node 内置模块与 `ctx` 公开 API。

## 6. 与 `adapters/deepseek_harness/adapter.json` 的对应关系

- `status: "implemented-source-unverified-live"`：源码级适配完成（bundle、adapter、
  routing 与测试），但第 3 节的宿主验收项（真实安装、真实 Agent、并发、预览、TCC）
  均未完成。仓库其它元数据（`project.json`、`products/deepseek-harness-plugin/product.json`）
  使用较短状态词 `implemented-source`，语义一致；本文件用更长的词明确“未做真实宿主验收”。
- `hostVersionVerified: "0.2.2-alpha.2"`：本文所有契约结论核对的 DSH 版本；升级 DSH 后
  需按附录 A 重新核对。
- `contractEvidence: "docs/architecture/dsh-host-contract.zh.md"`：指向本文件。
- `serverName: "quietbridge"`：连接器的 MCP server 名与工具命名空间（工具名
  `mcp__quietbridge__<raw>`）。
- `configuration: "DSH profile cordis.patch.yml bundle row; installed only via plugin_manager install_bundle"`
  ：配置落在 profile 的 patch 行（bundle 渲染/安装器生成），不手写 profile 文件。
- `transport: "per-agent-stdio-jsonrpc"`：每 Agent 一个 `python3 -B server.py` 子进程，
  stdio 上跑 JSON-RPC 2.0（非 Workbuddy 的静态 MCP row，也不是 profile 级 MCP 客户端）。
- `trustedContext: ["QUIETBRIDGE_HOST", "QUIETBRIDGE_HOST_SESSION_ID", "QUIETBRIDGE_HOST_AGENT_ID"]`
  对应 2.3 的身份不变量与 2.8 的注入方式。
- `lifecycleEvents` 对应 2.4/2.5 的事件：`agent/created`、`agent/disposed`、
  `turn/start`、`turn/end`、`tool/result`（后者指 session 持久事件或同名观测事件，
  bundle 内部须按 2.5 的推荐方式取 turn）。
- `supportsConcurrentAgents: false`、`blockingRequirements` 覆盖 G5/G6/G12/3.1–3.4。

## 附录 A：引用文件索引

| asar 内路径（`/dsh` 前缀） | 本文件用途 |
| --- | --- |
| `/package.json`、`/dsh/package.json` | 版本、构建、依赖清单 |
| `/lib/main.js` | Electron 壳；宿主窗口/预览检索 |
| `/dsh/node_modules/@deepseek-ai/dsh-agent/README.md`、`lib/index.js` | Agent API、`agent/created`/`agent/disposed` payload、id 不变量、创建监听器限制 |
| `/dsh/node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js` | turn 提交、aborted cause、`session/event` 订阅先例 |
| `/dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js` | 事件作用域规则（untagged 全局放行、tagged 自身上行） |
| `/dsh/node_modules/@deepseek-ai/dsh-session/README.zh.md`、`lib/index.js` | `session/event` 派发、事件信封、`turn/end` closer、事件类型清单 |
| `/dsh/node_modules/@deepseek-ai/dsh-tools/README.zh.md`、`lib/index.js`、`lib/pipeline.js`、`lib/types/{json-schema,contracts,batch}.js` | 注册校验、schema 子集、`tools/result`、取消码、`tool/call`+`tool/result` 持久载荷 |
| `/dsh/node_modules/@deepseek-ai/dsh-mcp-client/README.md`、`lib/index.js` | 配置字段、命名契约、图片投影、导出面 |
| `/dsh/node_modules/@deepseek-ai/dsh-plugin-manager/README.md` | `install_bundle`、pnpm、bundle selection、回滚、重启语义 |
| `/dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/worker/profile-resolution-bootstrap.js` | profile resolution 拦截层 |
| `/dsh/node_modules/@deepseek-ai/dsh-agent-preset/skills/cordis-plugin-development/SKILL.md` 与 `references/{host-plugin,mcp-bundle,practices,ui-plugin,verification,user-actions}.md`、`templates/mcp/*` | bundle manifest、locale/icon、安装、Host 导出、Agent ctx 清理实践、MCP 配置型 bundle 模板 |
| `/dsh/node_modules/@deepseek-ai/dsh-api-session-controller/lib/index.js` | UI/子 Agent 会话 id 形态 |
| `/dsh/node_modules/@deepseek-ai/dsh-shell-env/lib/index.js` | `DSH_SESSION_ID` 来源 |
| `/dsh/node_modules/@deepseek-ai/dsh-subagent/lib/index.js` | `agent/disposed`、`session/event`、`tools/result` 的插件侧订阅先例 |
| `/dsh/node_modules/@deepseek-ai/dsh-experimental-agent-team/lib/index.js` | 本部署已加载；根 ctx 上 `agent/created`（第 1474 行）与 `session/event` 订阅的生产先例 |
| `/dsh/node_modules/@deepseek-ai/dsh-experimental-cua-macos/README.zh.md` | 目标应用 `windowId` 语义（非宿主窗口） |

非 asar（本仓库/本机 profile，只读）：

- `~/.dsh/profiles/desktop/{package.json,cordis.patch.yml,pnpm-workspace.yaml,pnpm-lock.yaml,node_modules}`；
- `services/mcp/server.py`（现有 connector 的 MCP 实现与协议版本、图片块）；
- `services/mcp/{lifecycle_store,session_routing,session_registry}.py`（本轮改动的服务端）；
- `adapters/deepseek_harness/`（本产品适配器与 bundle）。

## 附录 B：本轮验证命令

```sh
# 版本与包清单
node /tmp/asar_list.js '^/dsh/package\.json$|^/package\.json$'
OUTDIR=/tmp/v-root node /tmp/asar_extract.js /package.json && cat /tmp/v-root/package.json

# 关键契约代码（示例）
OUTDIR=/tmp/v-agent node /tmp/asar_extract.js /dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js
grep -n "agent/created\|agent/disposed\|agent.id !== agent.session.id" /tmp/v-agent/index.js

# profile 只读观测
ls ~/.dsh/profiles/desktop/node_modules/@deepseek-ai
cd ~/.dsh/profiles/desktop && node -e "import('@deepseek-ai/dsh-mcp-client').catch(e=>console.log(e.code))"   # 预期 ERR_MODULE_NOT_FOUND

# adapter.json 合法性
python3 -c "import json;json.load(open('adapters/deepseek_harness/adapter.json'))"
```
