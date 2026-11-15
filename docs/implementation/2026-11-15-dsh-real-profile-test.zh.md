# QuietBridge × DeepSeek Harness：真实 profile 全能力测试（2026-11-15）

本轮在 **PKG 0.6.29（113）安装成功**后，把部署的 bundle 加入用户真实 `desktop`
profile，并由真实模型驱动、经真实 helper 执行 QuietBridge 的全部工具，核对返回值、
side effect、生命周期回执与失败语义。测试发现并修复了一个真实宿主上的回执缺陷；
浏览器扩展通道因环境未连接而未验收。

## 1. 环境与安装

| 项 | 值 |
| --- | --- |
| QuietBridge（PKG） | 0.6.29（113），`/Applications/QuietBridge.app` |
| helper | `/Library/Application Support/WorkBuddyCUAHelper/releases/9be85928d32dcd70/QuietBridge.app/Contents/MacOS/quietbridge-native`（签名有效） |
| DSH | 0.2.2-alpha.2，profile `desktop`（真实用户 profile） |
| bundle | `~/Library/Application Support/QuietBridge/releases/758fb5fe864a206be5b3/dsh-bundle` |
| 安装命令 | `dsh plugin --profile desktop add <bundle>` |

安装后 `~/.dsh/profiles/desktop/package.json` 写入
`"@quietbridge/dsh": "link:<bundle>"`，`dsh.profile.bundles` 追加 `@quietbridge/dsh`。
运行中的宿主**即时加载**：插件列表出现 `include:quietbridge-dsh`
（`enabled: true`，`fiberPhase: active`），按现存 3 个 Agent 各启动 1 个连接器进程
（3 个连接器的 `QUIETBRIDGE_HOST_SESSION_ID` 互不相同，不是重复进程）。

## 2. 能力矩阵（真实模型 → 真实宿主 → 连接器 → 真实 helper）

测试对象为 TextEdit 的一个临时文档窗口（`windowId=365595`／后一次为 `367114`），
任务为 `desktop_app`，全程未改变用户前台应用（`focusPreserved=True`）。

| 能力 | 结果 |
| --- | --- |
| `quietbridge_permissions` | `accessibility=True`、`screenRecording=True`、`sessionLocked=False`、`responsibleApp=QuietBridge.app` |
| `quietbridge_apps` list/find/get/launch | 13 个运行应用；find 命中 TextEdit；get 返回 appHandle/pid；launch 复用/启动应用（发现 `get` 在应用刚启动瞬间可能先返回“尚未运行”，用 bundleId 重试即得 pid） |
| `quietbridge_task` begin/bind/status/end | 全部成功；bind 返回 processIdentity、powerGuard；end 返回 `previewCleanup.captureStop` 与 `sessionProtection` |
| `quietbridge_windows` list/share/unshare | 列出窗口；share 建立 `window` 连续视频（`streamReady=True`、`trackingWindowId` 正确）；unshare 关闭共享 |
| `quietbridge_state` | AX 树含 `AXTextArea focused valueWritable=true`，`snapshotId` 可用于元素操作 |
| `quietbridge_screenshot` | 1172×976 PNG 落盘 `shots/shot-20261115-231735.png`，像素检查可见写入文本 |
| `quietbridge_visual_state` | `SCStream.complete` 单帧，`frameAgeMs≈153ms`、`streamReady` |
| `quietbridge_situation` | 只读现场：权限、focus、AX（截断提示）、preview 生命周期 |
| `quietbridge_set_value` | 写入 21 行文本，逐字读回 `match`；截图复核 |
| `quietbridge_click` | 元素路径（snapshot+elementIndex）与窗口坐标 (260,300) 均派发，`route=window-pointer`，`focusPreserved=True` |
| `quietbridge_scroll` | `dy=-6` 派发；`foundScrollBar=False`（TextEdit AX 滚动条不可用，如实返回未移动证据） |
| `quietbridge_drag` / `quietbridge_hover` | 均派发成功（`dispatched=True`），焦点保持 |
| `quietbridge_type_text` / `quietbridge_key` / `quietbridge_paste` | **按设计拒绝**：desktop 绑定任务返回 `UNSAFE_WINDOW_DELIVERY`（无窗口级投递确认），未发生应用级键盘或激活绕过 |
| `quietbridge_wait_ready` | 原生窗口无 AXWebArea，返回 `needs_visual_review` + 3 帧视觉材料，不误报 ready |
| `quietbridge_browser_events` | `BROWSER_HOST_UNAVAILABLE`（扩展未连接），带 `recovery.action=connect_extension` 与用户操作指引 |
| `quietbridge_browser_session` | prepare/list 正常；start/attach 未执行（不新建冲突 Chrome 实例） |

## 3. 发现并修复：任务回执参数形态缺陷

**症状**：同一宿主轮次内，第一阶段 `begin→bind→share→end` 成功；第二阶段
`begin` 成功后 `end` 被拒绝：

```
SESSION_ROUTING_REQUIRED: 当前连接保留着会话保护，但新阶段尚未核实属于同一宿主运行
```

**根因**：`services/mcp/server.py` 的 `require_protection_scope()` 用 lifecycle store 的
task receipt 证明“新阶段仍属于同一宿主轮次”。而真实 DSH session 事件
`tool/call.data.arguments` 是**原始 JSON 字符串**（本地会话日志
`session-768f…/session.v4.jsonl.zstd` 可复现），bundle 将字符串原样作为
`toolInput` 发给 lifecycle CLI；`adapters/deepseek_harness/lifecycle.py` 的
`_tool_result()` 要求 `toolInput` 是 dict，于是 begin/bind 回执被整体丢弃
（`host-lifecycle/state.json` 的 turn 中 `tasks` 为空）。第一阶段 begin 时
`SESSION_PROTECTION` 尚不存在，所以不受影响；end 后保护保留，第二阶段 begin 需要
回执比对，就失败在这一步。隔离探针此前用对象参数、并手工调用 lifecycle CLI，
因此没有覆盖到该真实形态。

**修复**：

- `adapters/deepseek_harness/bundle/index.js`：新增 `parseToolArguments()`，在
  `tool/call`、`tool/result` 与 `tools/result` 三条路径统一把 JSON 字符串解析为对象；
  `tools/result` 优先使用已解析的 `exec.arguments`；`__internals` 导出以便测试。
- `adapters/deepseek_harness/lifecycle.py`：`_tool_result()` 同时接受字符串与对象
  `toolInput`，畸形 JSON 安全忽略。
- 测试：bundle Node 测试 +3（字符串参数回执、`tools/result` 优先解析参数、
  `parseToolArguments` 归一化）；Python adapter 测试 +2（字符串参数回执、
  畸形字符串不落盘）。bundle 共 44 项、adapter 相关 48 项通过。

**修复后的真实宿主复验**：同一连接器（旧 bundle JS + 已修复的 installed lifecycle CLI）
`begin` 后 lifecycle store 立即出现该 taskId 回执；随后
`begin→bind→share→end→begin→end` 两个阶段全部通过，`sessionProtection` 由
`retained: false` 变为 `retained: true, scope: host_run`，第二次 end 返回
`captureStop.stopConfirmed=true`。证明修复不依赖重启，也能在既有宿主上即时生效。

### 3.1 宿主重启后会话被旧墓碑误判（114 安装重启后实测）

**症状**：安装 114 并重启 DSH 后，同一会话（`session-768f…`）恢复成功、连接器也已启动，
但所有 QuietBridge 工具立即返回
`TASK_CANCELLED: 宿主会话或 Agent 已结束`。

**根因**：DSH 关闭时 bundle 为每个 Agent 写入 `agent-disposed` 墓碑（`sessionEnded=true`，
session-wide `requestId=null`）；重启后 DSH 用**同一 session id** 恢复会话，lifecycle
store 未清理旧墓碑。新进程的 `turn/start` 命中 `_tombstone()`，被标记为已停止
（`stoppedAt` 甚至早于该 turn 的 `startedAt`），于是该轮全部调用被路由拒绝。

**修复**：lifecycle 的 `agent-created` 不再只是忽略——它证明该 session/agent 已属于
**新的宿主进程**，因此删除该对的 session-wide 墓碑并清除历史 turn 的 `sessionEnded`
（保留已经停止 turn 的 `stoppedAt`）；首个 `turn/start` 随后正常建立新 run 的存活轮次。
`agent/created` 仍不授予 turn，创建本身不能调用工具。

**验证**：

- Python adapter 新增“dispose → agent-created → 新 turn 存活”回归测试；
- 在真实宿主上重放清理后，`quietbridge_permissions`、`quietbridge_apps` 立即恢复；
- 同一轮内再次 `begin→bind→share→end→begin→end` 两阶段全部成功，lifecycle store 的
  turn 中自动写入两个新 taskId 回执（已修复 JS+adapter，非手工回放），第二次 end
  `captureStop.stopConfirmed=true`；
- 115 包安装并再次重启后**无需任何手工修复**：`agent-created` 自动清理旧墓碑，
  lifecycle store 中新 turn 直接为存活状态（`stoppedAt=null`、`sessionEnded=false`），
  `permissions`/`apps` 与两阶段任务再次全部通过，回执自动落盘。

## 4. 浏览器通道：目录分叉定位与统一（116）

现象：Chrome 扩展面板显示“已连接 / 已准备就绪”（最后握手正常），但连接器始终
`browserChannel=False`，`browser_events` 返回 `BROWSER_HOST_UNAVAILABLE`。

根因：Chrome 的 `com.cua.browser_observer` 原生宿主注册仍指向旧
`WorkbuddyExtension/browser-observer-extension`，状态写入
`WorkbuddyExtension/cua-helper-config`；而 DSH bundle 的 `configDir` 是
`~/Library/Application Support/QuietBridge`。扩展心跳与连接器读取的是两个不同目录，
所以扩展明明在线，连接器却认为宿主不可用。

按“只保留一个 QuietBridge 运行态、不为浏览器装第二套扩展/原生宿主”修复：

- `compatibility.browser_layout()` 统一到应用运行态根：host 位于
  `~/Library/Application Support/QuietBridge/extension/native-host/`，bridge 模块与
  `browser-events.json` / `task-windows.json` 位于 QuietBridge 根；
- Chrome 原生宿主注册改为 QuietBridge 自己的 launcher，旧 WorkbuddyExtension 不再注册
  第二个 host；`install_native_host.py` 与 `deploy.py` 将旧
  `WorkbuddyExtension/cua-helper-config`、`QuietBridgeBrowser/quietbridge-config` 作为
  迁移来源，首次部署时复制任务状态到 QuietBridge 根（不覆盖已有文件）；
- DSH bundle `configDir` 保持 QuietBridge 根，与原生宿主、任务状态共用同一目录。

Workbuddy 与 DSH 共用这一扩展/宿主，所以统一也覆盖 Workbuddy：其 `.workbuddy/mcp.json`
的 `quietbridge` 连接器 env 为空时会按新布局默认读 QuietBridge 运行态；旧
`WorkbuddyExtension` 仅迁移、不再注册。本机仍存有历史遗留 hooks 指向
`workbuddy-cua/connector/host_lifecycle.py --config-dir .../WorkbuddyExtension/cua-helper-config`，
它们不再影响新宿主，但会继续写旧目录。117 已把 Workbuddy 配置统一：部署会重写
`.workbuddy/mcp.json` 的 `QUIETBRIDGE_CONFIG_DIR` 为 QuietBridge 运行态、删除旧
`CUA_CONFIG_DIR`，并把 hooks 迁移为唯一一套 `adapters/workbuddy/lifecycle.py`（识别并移除
旧 `host_lifecycle.py` hooks）。本机已实机应用同一规则（原文件备份 `.bak-20261116-002244`，
移除 7 条旧 hooks）。

连接器级实测（使用 115 release bridge + 真实扩展，仅把 configDir 指向统一状态目录）：
`browserChannel=True`、`clientVersion=0.4.14`、`globalPermission=true`，candidates 返回
6 个真实 Chrome 标签（URL 可见），`browser_session list` 正常。完整页面操作
（`snapshot/read/click/fill/select/navigate/screenshot/scroll/keypress/diagnostics`、
`assign`、`verify_mapping`、`tabs/select_tab`）仍需安装 116 并重启 DSH/Chrome 后，
经宿主实际通道验收；在此之前不宣称 DSH 网页自动化已完成。

## 5. 回归与产物

| 检查 | 结果 |
| --- | --- |
| MCP 全量 | 494 项通过（新增 3 项：回执参数形态 ×2、重启墓碑 ×1） |
| DSH bundle Node | 44 项通过（新增 3 项） |
| 打包回归 | 57 项通过 |
| 浏览器扩展 | 117 项通过 |
| DSH 隔离实机 | `--agents 3` passed（19 工具、turn、路由、容量） |
| PKG | 0.6.29（116）重新打包并离线核验，见发布说明 |

验证证据：

- 真实会话日志：`~/.dsh/sessions/…/session-768f…/session.v4.jsonl.zstd`（本地只读）；
- lifecycle store：`~/Library/Application Support/QuietBridge/host-lifecycle/state.json`
  （修复后含多阶段 task receipts）；
- 截图：`~/Library/Application Support/QuietBridge/shots/shot-20261115-231735.png`、
  `visual-2783b8ko.png`；
- 打包核验：`dist/quietbridge-0.6.29-verification.json`。

## 6. 剩余限制

- 真实 Chrome 扩展通道已连通（116），但完整页面操作/标签分配与 mapping 需安装 116 并重启 DSH/Chrome 后验收；
- DSH 原生预览宿主窗口绑定未实现；图片结果仍以文本 + 落盘路径返回；
- 真实 profile 的并发 Agent、长跑与 Workbuddy 预览视觉验收未做；
- PKG 外层未签名/未公证（沿用现行发布策略）。

相关文档：[宿主契约](../architecture/dsh-host-contract.zh.md#71-真实用户-profile-全能力测试2026-11-15-晚)、
[隔离 profile 实机验证](2026-11-15-dsh-isolated-verification.zh.md)、
[发布说明 0.6.29](../releases/0.6.29.zh.md)。
