# DSH 适配落地记录（2026-11-15）

依据：[整体架构](../architecture/quietbridge.zh.md)、
[DSH 宿主契约](../architecture/dsh-host-contract.zh.md)、
[多 Agent 并行方案](../architecture/multi-agent-runtime.zh.md)。
本轮用户确认范围：**仓库内完成适配**，不安装到本机 `~/.dsh` profile、不改宿主配置、
不做真实 DSH 端到端验收。

## 1. 交付内容

| 位置 | 内容 |
| --- | --- |
| `docs/architecture/dsh-host-contract.zh.md` | DSH 0.2.2-alpha.2 宿主契约验证：bundle/插件 manifest 与安装、Host `ctx` API、Agent/Session/Turn 身份与事件、`ctx.tools.register` 硬约束、`tools/result` 缺口、MCP 客户端可用性、依赖解析、8 项未核实范围、G1–G18 缺口、逐项可行性判定 |
| `adapters/deepseek_harness/adapter.json` | 机器可读状态：`implemented-source-unverified-live`、每 Agent stdio 传输、可信 env 身份、剩余 blocking requirements |
| `adapters/deepseek_harness/configuration.py` | `owned_row`、`bundle_render`（默认纯预览、`apply=True` 事务写入、外来目标拒绝、幂等）、`profile_plan`（profile `package.json` + `cordis.patch.yml` 的纯计划，不写盘） |
| `adapters/deepseek_harness/lifecycle.py` | turn/agent/tool 事件写入 `services/mcp/lifecycle_store.py`；只存 id/时间戳/任务租约，不存参数、结果正文；坏输入静默 rc=0 |
| `adapters/deepseek_harness/guidance.py`、`README.md`、`__init__.py` | 发现文案、能力与限制、未验证范围 |
| `adapters/deepseek_harness/bundle/` | 可安装 DSH Host bundle（`@quietbridge/dsh`）：`index.js`（零 DSH 内部 import 的最小 MCP stdio 客户端 + 每 Agent 连接器 + Agent 作用域工具注册 + session 生命周期桥接 + 双 owner 清理）、`package.json`、`cordis.patch.yml`、`locale/en|zh.json`、`icon.svg`、`README.md`、8 组 `node --test` 测试与 fixtures |
| `services/mcp/session_registry.py` | `session_key(host, session, agent)`：单参旧调用保持 WorkBuddy 历史字节值不变；DSH 用 host/session/agent 三元组隔离 |
| `services/mcp/session_routing.py` | 新增严格 `host_context()` / `host_probe()`；DSH 模式按 env 身份 + lifecycle store 最新 turn 放行；拒绝 `_quietbridgeRoute`；已停止 turn / 已结束 Agent → `TASK_CANCELLED`；token 模式逐字不变 |
| `services/mcp/server.py` | 启动即校验宿主上下文（非法 fail-closed）、host 模式走 `SessionRouter`、host worker 不再写 WorkBuddy 重复回执、host 模式 `tools/list` 不广告 `_quietbridgeRoute` |
| `install.py` | `--dsh-bundle DIR`（+ `--dsh-connector/--dsh-helper`）渲染可安装 bundle；默认预览、`--install` 才写目标目录；**不写 `~/.dsh`** |
| `tools/release_layout.py`、`packaging/macos/{build,deploy}.py` | 桥接分发包含 bundle 运行时文件（js/json/yml/svg），安装产物排除 `test/` |
| `products/deepseek-harness-plugin/*`、`project.json`、仓库 `README.md`、三份架构/服务文档 | 产品状态、安装路径、限制与架构状态同步更新 |
| `services/mcp/test_dsh_adapter.py`、`test_dsh_host_routing.py`、`test_installer.py` | 适配器/路由/安装器测试（新增 38 + 20 + 3 个用例） |

## 2. 运行链路（源码级）

```
DSH Host 进程
  ├─ bundle/index.js apply()
  │    ├─ agent/created → spawn  python3 -B <bridge>/server.py
  │    │                   env: QUIETBRIDGE_HOST=deepseek-harness
  │    │                        QUIETBRIDGE_HOST_SESSION_ID=<agent.session.id>
  │    │                        QUIETBRIDGE_HOST_AGENT_ID=<agent.id>
  │    │                        QUIETBRIDGE_HELPER_PATH / QUIETBRIDGE_CONFIG_DIR
  │    ├─ initialize → tools/list → agent.ctx.tools.register(quietbridge_* 原始 schema)
  │    └─ session/event（turn/start、turn/end、tool/call、tool/result）
  │         + agent/disposed → lifecycle.py --config-dir <configDir>（stdin 单 JSON，best effort）
  └─ 连接器 server.py
       ├─ 启动时读取 host env，HOST_ROUTING 生效
       ├─ consume(): lifecycle store 中该 session+agent 的最新 turn 是否仍活动
       └─ SessionRouter: 每 Agent 一个 SessionProcess（上限 3），沿用现有 helper/权限/恢复实现
```

设计取舍：

- **不使用 `dsh-mcp-client` profile row / programmatic mount**：静态 row 是全局作用域，
  无法为每个 Agent 注入不同可信身份；programmatic mount 需要 import 宿主内部包，
  本机 profile 静态不可解析（实测 `ERR_MODULE_NOT_FOUND`），属未验证内部机制。
  因此 bundle 自实现最小 stdio MCP 客户端（仅 Node 内置模块）。
- **身份只来自宿主事件**：`agent.id`/`agent.session.id`（本版本恒等），不读
  `process.env.DSH_SESSION_ID`、不从模型参数/前台窗口/最近 hook 猜测。
- **工具名沿用 `quietbridge_*`**：这些是 Agent 作用域工具，不是 profile 级 MCP 客户端注册，
  wire 名即原始名；与仓库 `quietbridge_*` 发布命名一致。若未来改经 MCP 客户端注册，
  公共名需 `mcp__quietbridge__<raw>`（契约文档 G17）。
- **`tools/pre-execute` 不能改写参数**（DSH 官方限制）：Workbuddy 的一次性 token 注入
  在 DSH 不可用，改为“每 Agent 独立连接 + env 可信身份 + lifecycle turn 判定”。
- **图片降级**：DSH 图片内容需要 attachment 服务与模型 image 能力，本轮 `render` 只输出
  文本；截图/图像结果以元数据文本呈现，不把 base64 写入模型上下文、工具值或 lifecycle store。
- **预览不宣称**：未找到公开 API 取得 DSH 宿主窗口/预览锚点；原生预览宿主绑定是后续
  Swift/原生侧工作，本轮不实现、不宣称 DSH 预览可用。

## 3. 验证记录（2026-11-15，本机）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| MCP 全量（含新增适配器/路由/安装器测试） | `env -u PYTHONPATH python3 -m unittest discover -s services/mcp -p 'test_*.py' -q` | **481 项通过** |
| DSH 适配器测试 | `env -u PYTHONPATH python3 -m unittest discover -s services/mcp -p 'test_dsh_adapter.py' -q` | 38 项通过 |
| DSH 宿主路由测试 | `env -u PYTHONPATH python3 -m unittest discover -s services/mcp -p 'test_dsh_host_routing.py' -q` | 20 项通过 |
| Bundle Node 测试 | `node --test adapters/deepseek_harness/bundle/test/*.test.mjs` | **40 项通过**（含真实 `lifecycle.py` 协议集成，缺 python3 自动 skip） |
| 打包回归 | `env -u PYTHONPATH python3 -m unittest discover -s packaging/macos -p 'test_*.py' -q` | 47 项通过 |
| 浏览器扩展回归 | `node --test extensions/chromium/*.test.mjs` | 117 项通过 |
| Python 语法 | `python3 -m py_compile`（全部改动 .py） | 通过 |
| Bundle 语法 | `node --check adapters/deepseek_harness/bundle/index.js` | 通过 |
| JSON/补丁渲染 | `python3 install.py --install --dsh-bundle /private/tmp/qb-dsh-final --dsh-helper /Applications/QuietBridge.app/Contents/MacOS/quietbridge-native` | 渲染 7 个文件；`config` 为具体绝对路径；`!!js` 默认值被替换；`profileChanged=false` |
| 幂等/外来目标 | `test_installer.py` + `test_dsh_adapter.py` | 重复渲染幂等、外来 `package.json` 拒绝、home 下不出现 `.dsh` |

未运行 Swift 构建/测试：本轮未改动 `apps/macos` 或 Swift 源码；原生预览绑定未实现（见限制）。

## 4. 未验证范围与阻塞项

- **用户真实 profile 未安装**：没有修改 `~/.dsh/profiles/*`。同日已在隔离
  `DSH_HOME/profile` 完成 bundle 安装、宿主启动、Agent 工具注册、turn 生命周期与
  宿主上下文路由放行/拒绝的真实验证，见
  [隔离 profile 实机验证](2026-11-15-dsh-isolated-verification.zh.md)；真实 helper
  权限/TCC、模型驱动的工具调用、预览、并发与升级路径仍未验收。
- **原生 DSH 预览宿主绑定未实现**：不宣称 DSH 预览可用；原生侧仍是历史宿主选择逻辑。
- **签名 helper 的 TCC 归属未核实**：`helperPath` 只是配置透传；DSH 进程拉起 helper 时
  辅助功能/屏幕录制授权归属需实测。
- **多 Agent 并发未验收**：`supportsConcurrentAgents=false`，容量沿用 3，第四路明确拒绝；
  真实 DSH 子 Agent 并行、父子 Agent 隔离仍需端到端验证。
- **HMR/升级/孤儿回收**：仅按 stdin EOF、SIGTERM/SIGKILL 与双 owner 清理设计，未真实宿主实测。
- **生命周期写入开销**：每次事件 spawn 一次 `lifecycle.py`（约数十毫秒，best effort，
  5 秒超时吞掉）；turn/end 路径异步，不阻塞宿主，后续可改常驻或批量。
- **图片能力**：仅文本降级；attachment 投影是后续工作。
- **profile 合并**：`profile_plan` 只生成计划，未接入真实 profile 事务；实际安装仍须
  通过 DSH 自己的 `install_bundle` 流程并由用户显式确认。

## 5. 后续建议

1. 在隔离 profile 中执行一次 `plugin_manager install_bundle`（渲染产物），验证首轮工具可见性、
   turn/end 后拒绝、用户停止取消、Agent 释放清理；记录 trace。
2. 原生侧增加“显式宿主窗口绑定”能力后，再实现 DSH 预览归属；在此之前控制面板与文档保持
   不宣称。
3. 真实 DSH 并发主/子 Agent 验收（两路起步），确认连接器容量、目标租约与停止隔离。
4. 视需要把 lifecycle 写入改为常驻进程或批量合并，减少每事件进程启动开销。
5. 评估 `attachment` 投影以恢复截图等图片型工具结果。

## 6. 补充（同日）：隔离 profile 实机验证

在隔离 `DSH_HOME` 中用官方 `dsh plugin add` 安装渲染 bundle，并真实启动宿主：
每 Agent 独立连接器按宿主身份启动，19 个 `quietbridge_*` 工具注册到 Agent 作用域；
真实 `turn/start`/`turn/end` 事件写入 lifecycle store；活动 turn 内工具调用被放行并
返回 `HELPER_MISSING`（测试 helper 故意缺失），turn 结束后调用返回 `TASK_CANCELLED`；
Agent 释放写入 `sessionEnded` 墓碑并清理连接器。完整证据、限制与复现方式见
[DSH 隔离 profile 实机验证](2026-11-15-dsh-isolated-verification.zh.md)，
仓库脚本 `env -u DSH_HOME -u PYTHONPATH python3 tools/dsh_isolated_check.py` 可一键复现（约 12 秒）。

## 7. 补充（同日）：接入 PKG 部署

`packaging/macos/deploy.py` 现在会从已验证 release 内模板渲染 DSH bundle 到
`releases/<digest>/dsh-bundle`，与其它用户文件同一事务提交/回滚；渲染失败只降级记录，
不阻断主安装。验证与限制见 [DSH bundle 接入 PKG 用户部署](2026-11-15-dsh-pkg-deploy.zh.md)。
多 Agent 并发隔离（2-4 Agent、容量降级）也已在该隔离环境中补验，见
[隔离 profile 实机验证 §5](2026-11-15-dsh-isolated-verification.zh.md#5-多-agent-并发隔离验证补充)。
