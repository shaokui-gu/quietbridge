# DSH 隔离 profile 实机验证（2026-11-15）

补充 [DSH 适配落地记录](2026-11-15-dsh-adaptation.zh.md)：上一轮只完成仓库内源码级适配；
本轮在本机用 **独立 DSH_HOME + 独立 profile** 通过 DeepSeek Harness 官方 CLI 安装渲染出的
bundle，真实启动宿主、创建 Agent 并验证连接器、工具注册、生命周期与宿主上下文路由。

全程**没有读写用户 `~/.dsh`、没有安装到 `desktop` profile、没有启动真实 helper/权限**；
所有临时数据位于 `/private/tmp` 下，验证后清理（可 `--keep` 保留）。

## 1. 验证方法

| 步骤 | 做法 |
| --- | --- |
| 隔离环境 | `DSH_HOME=/private/tmp/dsh-qb-home`；`dsh` CLI 位于 `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh` |
| 建 profile | `dsh --profile qb-test --from-default-profile web --dump-config`（用官方 web 模板，exit 0） |
| 渲染 bundle | `configuration.bundle_render(...)`：connector=仓库 `services/mcp/server.py`，helper=临时目录中**故意不存在**的路径（避免真实权限/UI 副作用），configDir=临时目录 |
| 安装 bundle | `dsh plugin --profile qb-test add /private/tmp/qb-dsh-iso`（真实 pnpm link；exit 0） |
| 测试探针 | 临时 plugin（`@quietbridge/e2e-probe`）：`ctx.agents.create()` 建 Agent，观察工具、用真实 `lifecycle.py` 写 turn，调用一个已注册工具，最后 dispose |
| 启动宿主 | `dsh --profile qb-test --patch <probe overlay> --no-open --port 0`（Electron-as-Node；probe 完成后自行退出） |

`dsh plugin add` 的实测行为（与 `plugin_manager install_bundle` 等价的三件事）：

- profile `package.json` 增加 `"@quietbridge/dsh": "link:/private/tmp/qb-dsh-iso"`；
- 把 `@quietbridge/dsh` 追加进 `dsh.profile.bundles`；
- 应用 bundle 的 `cordis.patch.yml`。

`dsh --profile qb-test --dump-config` 随后在组合树中出现：

```yaml
# == @quietbridge/dsh
- id: quietbridge-dsh
  name: '@quietbridge/dsh'
  config:
    python: /opt/homebrew/opt/python@3.14/bin/python3.14
    connector: /Users/gushaokui/Documents/AI/computer-use/services/mcp/server.py
    helper: /private/tmp/qb-missing-helper
    configDir: /private/tmp/dsh-qb-config
    serverName: quietbridge
```

说明：loader 接受了 bundle manifest、patch 行与本地 link 依赖，无解析错误或告警。

## 2. 实机结果

宿主启动输出（退出码 0）：

```text
dsh web: http://127.0.0.1:52188/?token=…
```

测试探针写入的结果（`/private/tmp/qb-e2e-result.json`）：

| 检查 | 结果 |
| --- | --- |
| Agent 创建 | `created=true`，`agent.id === session.id = qb-e2e-…` |
| 每 Agent 连接器 | 启动成功；连接器以宿主注入身份通过 `server.py` 启动校验（否则不会注册工具） |
| 工具注册 | **19 个 `quietbridge_*` 工具**出现在该 Agent 作用域：`quietbridge_task`、`quietbridge_permissions`、`quietbridge_windows`、`quietbridge_screenshot`、`quietbridge_click`、`quietbridge_browser_events` 等 |
| 工具可见性 | 探针通过 `ctx.tools.get('quietbridge_permissions', agent)` 取到 definition 并可执行 |
| 生命周期 turn | 真实 `agent.followup()` 触发一次 turn；因隔离 profile 无模型凭据，turn 以 error 结束 |
| 清理 | `handle.dispose()` 成功；连接器关闭，无残留进程 |

生命周期 store（`/private/tmp/dsh-qb-config/host-lifecycle/state.json`）实机证据：

| requestId | 关键字段 | 说明 |
| --- | --- | --- |
| `<session>:1` | `stoppedAt` 非空、`reason: "error"` | 真实 session 事件 `turn/start`/`turn/end` → Python lifecycle 写入；观察无模型时的错误收尾 |
| `<session>:90` | 手动 turn-start/turn-end | 探针用真实 CLI 构造的“活动 turn”，用于路由放行测试 |
| `requestId: null` | `sessionEnded: true`、`reason: "host_session_end"` | Agent 释放时由 bundle 写入的墓碑 |

宿主上下文路由（同一实机调用）：

- 活动 turn 内调用 `quietbridge_permissions`：
  `HELPER_MISSING: helper not found at /private/tmp/qb-missing-helper` —— 说明连接器已按
  env 身份放行、进入 worker 并如实返回 helper 缺失，**不是** `SESSION_ROUTING_REQUIRED`。
- 结束该 turn 后再次调用：
  `TASK_CANCELLED: 宿主轮次已停止；本次调用未执行` —— 说明 turn 停止后新输入被拒绝。

## 3. 覆盖与未覆盖

本轮由实机验证覆盖：

- bundle 被 DSH loader 接受（manifest/patch/config/本地 link 依赖）；
- Host 插件 `apply()` 在真实宿主中运行，`agent/created` 收到 `payload.agent`；
- 每 Agent 独立 connector 以宿主注入的 session/agent 身份启动；
- `tools/list` → `agent.ctx.tools.register` 的 19 个工具对 Agent 可见、可执行；
- 真实 session 事件 `turn/start`/`turn/end` 经 bundle 桥接进 Python lifecycle store；
- 宿主上下文路由的放行（活动 turn + env 身份）与拒绝（turn 已停止）；
- Agent 释放时 lifecycle 墓碑写入与连接器清理。

仍未覆盖（不得据此对外宣称）：

- 真实用户 `desktop`/自定义 profile 的安装与验收；helper 由 DSH 拉起时的 TCC 归属；
- 真实 helper 工具执行（本轮故意使用不存在的 helper；原生权限/截图/预览未验证）；
- 模型驱动的真实工具调用回合（隔离 profile 无 API 凭据，turn 以 error 结束）；
- DSH 预览宿主窗口绑定（本轮未实现，也未验证）；
- 并发多个 Agent、容量上限与子 Agent 隔离的真实宿主验收；
- 图片 attachment 投影；HMR/升级路径。

## 4. 复现

手工步骤见上文；仓库脚本可一键复现同一检查（临时 DSH_HOME、临时 profile、渲染 bundle、
测试探针与断言，约 12 秒；默认清理临时目录）：

```sh
env -u DSH_HOME -u PYTHONPATH python3 tools/dsh_isolated_check.py
```

2026-11-15 实跑：`status=passed`，`toolCount=19`，`liveTurnError` 含 `HELPER_MISSING`
且不含 `SESSION_ROUTING_REQUIRED`，`stoppedTurnError` 含 `TASK_CANCELLED`，boot
`exitCode=0`、`timedOut=false`，lifecycle `turnCount=3`（含 `<session>:1`、`<session>:90`
与 `host_session_end` 墓碑），总耗时约 11.6 秒。在 DSH 自己的 shell 中运行时需先
`env -u DSH_HOME`（脚本拒绝继承的真实 home）。
