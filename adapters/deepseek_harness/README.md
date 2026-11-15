# DeepSeek Harness adapter

QuietBridge for DeepSeek Harness 的 Python 适配层。本轮只实现仓库内适配、配置渲染与宿主生命周期回执，**不安装到本机
`~/.dsh`、不启动真实 DSH 子 Agent 端到端验收**。

## 能力

| 文件 | 内容 |
| --- | --- |
| `guidance.py` | `CONNECTOR_DESCRIPTION`：供宿主/插件展示的中文发现文本，明确标注未验收范围。 |
| `configuration.py` | `owned_row`、`bundle_render`、`profile_plan`：把 `bundle/` 模板渲染成可安装目录，并生成 profile 合并计划（纯计划不写盘）。 |
| `lifecycle.py` | `handle_event` + stdin JSON CLI：把 DSH 的 turn/agent/tool 事件写成 `services/mcp/lifecycle_store.py` 的 turns 回执，供核心路由拒绝已停止轮次与记录任务归属。 |
| `bundle/` | 由 DSH 插件侧（队友 dsh-bundle）提供，本目录不含其实现。 |

`bundle_render` 默认 `apply=False`，只校验并返回计划（`bundlePath`、渲染后的 `patch` 文本、`files`、`config`、
`serverName`）；`apply=True` 才事务式写入显式传入的目标目录。目标已存在且不是同一 bundle（按 `package.json` 的
`name` 判断）时抛 `ValueError`，不会覆盖其他组件；同一 bundle 可幂等更新。模板中的 `test/`、`__pycache__`、
`.DS_Store` 不会进入安装产物。

`profile_plan(profile, bundle_path, server_name='quietbridge', config=None)` 返回
`{'packageJson': (path, original|None, rendered), 'patch': (path, original|None, rendered)}`：`dependencies`
增加/原位更新 `link:<bundle 绝对路径>`，`cordis.patch.yml` 追加或原位替换
`- insert:` 下的 `{id: quietbridge-dsh, name: <bundle package name>, config: {...}}`。不写盘、不执行 pnpm、
不安装、不读网络。插件行被其他 `name` 占用或结构无法可靠解析时抛 `ValueError`，不会破坏用户其他行。

## 安装路径与配置项

- 包名以 `bundle/package.json` 的 `name` 为准（当前模板为 `@quietbridge/dsh`），configuration.py 不硬编码。
- bundle 行固定 `id: quietbridge-dsh`；渲染后的 `config` 为：

```json
{
  "python": "/usr/bin/python3",
  "connector": "/绝对路径/services/mcp/server.py",
  "helper": "/绝对路径/QuietBridge Helper.app/Contents/MacOS/helper",
  "configDir": "/绝对路径/QuietBridge 配置目录",
  "serverName": "quietbridge"
}
```

  `connector`、`helper`、`configDir` 必须是绝对路径；`python` 必须是绝对路径或可执行文件名。
  模板里的默认值经 `!!js` 读取 `QUIETBRIDGE_DSH_PYTHON`、`QUIETBRIDGE_DSH_CONNECTOR`、
  `QUIETBRIDGE_DSH_HELPER`、`QUIETBRIDGE_DSH_CONFIG_DIR`、`QUIETBRIDGE_DSH_SERVER_NAME`；
  `bundle_render` 会把它们替换为具体值。
- 仓库安装器负责渲染目录：`python3 install.py --install --dsh-bundle <目录> --dsh-helper <helper>`
  （默认预览；`bundle_render(..., apply=True)` 才写目标目录）。写入 profile 由官方 CLI 完成：
  `dsh plugin --profile <profile> add <目录>`（自动写 link 依赖、`dsh.profile.bundles` 与 patch）。
  `profile_plan()` 只生成等价计划，供需要手工事务化时预览，本仓库不自动写 `~/.dsh`。
- 2026-11-15 已在隔离 `DSH_HOME`/profile 中完成实机验证（bundle 安装、宿主启动、Agent 工具注册、
  turn 生命周期与宿主上下文路由放行/拒绝），见
  [隔离验证记录](../../docs/implementation/2026-11-15-dsh-isolated-verification.zh.md)；
  真实用户 profile、helper 权限/TCC、预览与并发仍未验收。

## 与 services/mcp 的关系

- `bundle/index.js` 为每个 DSH Agent 启动一个连接器进程：
  `python3 -B <connector>`，经 stdio JSON-RPC 暴露原始 `quietbridge_*` 工具（Agent 作用域注册，不加 `mcp__` 前缀）。
  连接器进程沿用现有 `services/mcp/server.py`、输入/截图/授权实现，不复制、不分叉。
- 事件经 `lifecycle.py` 写入 `<configDir>/host-lifecycle/state.json`（复用 `lifecycle_store.store/read/
  task_receipt/host_binding`）。`requestId` 语义为宿主 turn id；turn key 为
  `sha256('deepseek-harness' + '\0' + sessionId + '\0' + agentId + '\0' + turnId)`，写入 `host`、`agentId`
  字段；`agent-disposed` 另写 session 级 hash 墓碑（`sessionEnded`），令迟到回执也保持 stopped。
- 只保存 id、时间戳、任务/租约 id：**不保存**工具参数、返回值或对话内容；`tool-result` 只在
  `quietbridge_task` 的 `begin`/`bind` 动作上解析任务回执，且不会把已确认阶段转给另一会话/Agent。
- 核心侧（task-2）在 `QUIETBRIDGE_HOST=deepseek-harness` + 合法 session/agent id 时启用宿主环境上下文路由，
  session key 纳入 host/agent；无 turn 记录或最新 turn 已 stopped 时拒绝调用；同时最多 3 个会话，沿用现有
  stop/reap 逻辑与 3 会话上限。

## 生命周期 CLI

`python3 -B adapters/deepseek_harness/lifecycle.py --config-dir <configDir>`，stdin 读一个 JSON 对象后即退出。
退出码恒为 0；坏 JSON、超限输入、未知事件静默忽略；存储失败只向 stderr 打一行诊断，绝不阻塞宿主停止或触发续跑。
支持事件：`turn-start`、`turn-end`（reason 归一化 completed/cancelled/interrupted/error，未知→ended）、
`agent-created`（可选，本身不建立运行状态）、`agent-disposed`、`tool-result`。

## 未验证范围与 blocking requirements

- 未做真实 DSH 端到端验收：未在真实 DSH 中安装本 bundle、未启动真实子 Agent、未验证工具注册渲染。
- 原生预览宿主绑定（DSH 窗口证据）未实现：本轮**不宣称** DSH 预览可用，属 Swift/原生侧后续工作。
- DSH 拉起 signed helper 的 TCC 归属未核实；`helper` 目前只是配置值。
- 未声称多 Agent 并行已验收；容量上限沿用 3，超限必须明确拒绝。
- 浏览器扩展共用现有实现，未新增 DSH 专用通道。

## 测试

```sh
env -u PYTHONPATH python3 -m unittest services.mcp.test_dsh_adapter -q
env -u PYTHONPATH python3 -m unittest discover -s services/mcp -p 'test_*.py' -q
python3 -m py_compile adapters/deepseek_harness/{configuration,lifecycle,guidance}.py
```

测试全部使用临时目录，不触碰真实 profile、不联网；`bundle/` 模板缺失时，默认模板测试自动跳过（当前模板已存在）。
