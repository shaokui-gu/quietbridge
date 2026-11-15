# QuietBridge MCP service

把 `quietbridge-native`（macOS 原生界面操作辅助程序）包装成 MCP stdio 服务，
让 WorkBuddy 能直接调用桌面操作能力。

```
WorkBuddy ⇄ MCP(stdio/JSON-RPC) ⇄ server.py ⇄ quietbridge-native ⇄ 共享守护进程 ⇄ AX / SkyLight / ScreenCaptureKit
```

辅助程序本身只讲私有 JSON-lines 协议（`protocol.schema.json`），WorkBuddy 只认 MCP，
这一层就是两者之间的单向翻译 + 体积裁剪，Swift 侧零改动。

## 应用运行与退出

先打开 `QuietBridge.app`，菜单栏图标和所有控制能力属于同一个应用运行会话。面板右下角的“退出应用”会结束整个应用，撤销当前会话，取消后台原生操作、停止预览并释放防睡眠租约；浏览器扩展停止观察、解除调试连接并显示“应用未运行”。连接器保留安装，但所有工具返回 `APP_NOT_RUNNING`，不会自行启动后台执行器。点击面板外部或按 Esc 仅收起面板。

重新打开应用会创建新的运行会话，旧任务分配和待执行命令不自动恢复。共享执行器即使遇到应用崩溃，也会检测运行会话失效并退出。锁屏保护中的退出仍沿用先交还系统锁屏的清理流程。

## Agent 调用约定

需要后台操作 macOS 应用界面或 Chrome 已登录标签时，初始化指引优先提示发现并选用 QuietBridge；七项主要入口把适用场景放在工具描述开头。默认连接简介在安装时补齐，已有用户自定义简介保留。工具描述是选择线索，不是宿主的数值优先级或授权依据。可直接使用的宿主偏好文本与选用率验收见 [WorkBuddy 工具偏好](../../products/workbuddy-connector/tool-preference.zh.md)。

MCP `initialize` 返回全局 `instructions`，`tools/list` 的各项描述也包含对应操作要求；错误回执会继续给出相同的恢复方向。桥接版本为 `1.1.0`。PKG 使用固定连接入口，后续覆盖安装或升级保持 stdio，完成当前调用后加载新版并发送工具列表更新通知。旧版直连入口首次迁移仍需要宿主刷新与信任确认。

工具回执的 `structuredContent.recovery` 区分 `user_action_required`、`revalidate_required`、`wait_required`、`outcome_unknown`、`blocked` 和明确停止的 `task_stopped`。可恢复错误不结束业务：快照过期、候选变化和通道启动先按回执对原目标作有界恢复；共享暂未就绪最多两次只读复查；输入回执超时先查询原 commandId 和观察，绝不重放副作用。正常 queued 命令继续查询原结果，不因两次普通轮询未结束就弹框。成功的状态读取也可能附带实际权限或连接阻塞；空候选本身不等于权限故障。

应用未运行、能力关闭、权限缺失、目标占用、无法安全恢复或自动恢复耗尽时，暂停依赖步骤，按 `recovery.userDecision` 让用户选择。立即需要选择的回执是 `suggestedNext=ask_user_choice`；自动恢复回执的 `onExhausted=ask_user_choice` 表示先恢复，耗尽后才展示。Agent 优先调用宿主可用的 `AskUserQuestion` 或等效交互工具，提供“处理后继续 / 保持暂停 / 结束任务”；没有交互工具时在对话中询问并等待，不能默认选择、直接用最终回复宣告业务结束或换工具绕过。等待时停止新输入、释放自己的共享；按具体恢复计划保留原绑定或结束已失效的界面阶段，保留已有业务结果。用户选择继续后重新核实权限、连接、目标、共享与新快照。明确取消或宿主停止的回执为 `taskMustEnd=true`，不再弹继续选择或自动重建。

这些恢复字段是本项目的调用约定，宿主仍负责让 Agent 遵守；MCP 回执本身不能弹出宿主对话框。整个连接器未加载或离线时无法返回回执，需在宿主的常驻 Agent 指令中加入：“指定连接器不可用时，保留业务进度，使用宿主交互工具询问处理后继续、保持暂停或结束；无交互工具则在对话中等待，不改用其他自动化工具绕过。”

界面操作必须**不抢焦点并使用共享窗口能力**：

1. 保持用户的前台应用、键盘窗口、物理鼠标和当前 Space。禁止用 `activate`、`AXRaise`、全局 HID、切换 Space 或先抢焦点再恢复来完成后台任务。无法后台执行时报告阻塞并等待。
2. 按操作对象在 `begin` 指定 `targetSurface`：网页内容用 `web_content`，浏览器自身控件用 `browser_ui`，真实原生对话框用 `native_dialog`，普通应用用 `desktop_app`。自动选择扩展或原生模式；核实权限和真实目标后绑定原窗口，不能因故障改模式绕过限制。
3. 原生任务绑定后先调用 `quietbridge_windows(action="share", appHandle=..., windowId=..., source="window")`，确认 `streamReady=true` 与目标一致，再观察、输入。后续调用自动续租并复用同一捕获流，不逐次 `unshare/share`。共享是画面展示，不提供独立桌面或焦点隔离。
4. 网页先 `bind`、用真实候选 `assign`，再 `verify_mapping` 核实系统窗口与标签的对应关系，随后建立系统窗口连续视频。映射或共享未就绪时，agent 应报告并等待；后台扩展在执行层独立于预览，不能把仍可用的扩展命令或标签截图当作共享已确认，也不能回落前台或原生键鼠。
5. 使用目标窗口最新快照或图像证据；派发后读回核实。点击回执中的 `focusPreserved=false/null` 时暂停后续输入，核实是否为用户主动切换，不重复点击、不自动恢复焦点。`FOCUS_STATE_UNKNOWN`、`FOREGROUND_REQUIRED`、共享或窗口投递错误都不能通过激活绕过。
6. 原生绑定任务拒绝缺少窗口级确认的 `quietbridge_type_text`、`quietbridge_paste`、`quietbridge_key`。写入使用同一快照中可写的 AX 元素；网页用扩展 `fill`。不能结束任务再调用应用级键盘绕过。完成后 `quietbridge_task(action="end", confirm=true)` 释放共享租约。

这些说明约束 agent 的选路，并与已有共享、目标和焦点检查配合；它们不构成操作系统的焦点隔离。第三方应用自行激活及用户并发切换仍需用实际回执和观察核实。

## 浏览器扩展与原生操作的选路

选路依据是实际操作对象，不是应用名称或当前前台窗口：

| `targetSurface` | 执行模式／通道 | 操作范围 |
| --- | --- | --- |
| `web_content` | `background_browser`／Chrome 扩展 | 网页链接、表单、正文、滚动、导航、页面按键 |
| `browser_ui` | `desktop`／原生窗口 | 地址栏、工具栏等非网页控件；仅限已观察且能后台投递的控件 |
| `native_dialog` | `desktop`／原生窗口 | 已核实的真实打开／保存对话框；系统授权、安全输入等仍受原有限制 |
| `desktop_app` | `desktop`／原生窗口 | 普通原生应用 |

例如 `quietbridge_task(action="begin", targetSurface="web_content")` 或 `targetSurface="native_dialog"`，不必填写 `interactionMode`。两者同时填写必须一致；`targetSurface` 和模式只能在 `begin` 指定。活动任务的 begin/bind/status/end 回执含 `routing`（目标类别、通道、不抢焦点、共享及目标证据前提、禁止自动故障降级），这不是权限或共享已经就绪的证明。

网页导航优先扩展 `navigate`，不为导航改操作地址栏。网页模态框、登录框、跨域 frame、快照没有元素、AX 空树和 `chrome://` 等受限页面不构成原生对话框证据。扩展断开、权限拒绝、输入超时或结果未知时，保留原目标，按 recovery 通知用户和核实既有结果；不切换到原生网页操作。

真实的跨界面流程（例如网页弹出原生保存框）可以分阶段：核实上一动作结果、保存原任务标签身份 → `end` 自己的网页阶段 → `begin(targetSurface="native_dialog")` 并绑定已观察到的真实窗口、建立共享 → 原生操作并读回 → `end` → 新网页阶段重新核实原窗口和标签，assign／verify_mapping 并重新观察。仅对话框等真实界面切换使用该流程，不能把故障重试包装成新阶段。阶段结束不结束宿主会话保护，不重放结果未知的动作，不接管其他会话。

原生操作继续执行现有窗口归属、最新观察、共享首帧和焦点检查；`type_text/paste/key` 与窗口激活不会因此开放，菜单等无法窗口定向操作时仍需用户处理。同一 Chrome 进程的原生阶段仍按进程互斥，不能影响其他并行会话。声明原生 `targetSurface` 后，桥接在派发前拒绝该阶段的扩展操作（`TASK_ROUTE_MISMATCH`），保留状态读取、候选枚举、已有结果查询和取消；网页模式仍拒绝所有原生输入。旧调用不指定 `targetSurface` 时保持原模式与存储兼容。

完整边界与验收见 [浏览器选路策略](../../docs/architecture/browser-routing.zh.md)。

## 共享范围

接口未显式指定范围时，应用内的观察、截图及输入默认使用 `application`，通过 ScreenCaptureKit 只共享目标应用在目标显示器上的窗口，不包含其他应用。Agent 的固定窗口任务应按上述约定显式建立 `source="window"` 共享，后续调用保留这个范围。

任务涉及跨应用、Dock 或系统界面时，先调用 `quietbridge_windows(action="share", appHandle=..., windowId=..., source="desktop")`，共享目标窗口所在显示器的当前桌面。后续观察及输入会保留所选范围；仅需要指定窗口时仍可显式使用 `source="window"`。

范围只跟随同一连接及同一应用窗口，结束共享、新任务、更换目标或重连后恢复应用共享。目标位于后台 Space 时，应用共享会明确降为目标窗口预览；桌面共享不会降为窗口冒充成功，需要手动切换到目标桌面后重试。

共享目标与预览面板分别绑定：捕获持续使用任务的原应用/窗口，预览面板默认吸附宿主窗口右上角，保留 16pt 内边距。拖动画面可临时避让，松开后保留 30 秒再自动吸附，重复拖动重新计时；临时位置限制在宿主显示器可见范围，宿主移动或缩放立即恢复吸附。移动及拉伸宿主时以 30Hz 独立更新几何位置，鼠标跟踪模式下继续运行；只有左下角支持等比缩放，右上顶点保持固定。宿主变小或源画面较高时自动收小，保持在宿主与显示器可见区域内。正常播放无标题栏，视频铺满卡片；玻璃底层在 macOS 26 使用 NSGlassEffectView，旧系统使用 NSVisualEffectView，统一裁切连续圆角，无额外描边，面板按透明轮廓生成阴影；关闭按钮及缩放提示仅悬浮时显示。WorkBuddy 换 Space 时，即使屏幕坐标不变，也会把自有预览面板迁到宿主的 Space，并回读验证后再显示；不会激活应用、切换用户桌面或把面板加入所有桌面。宿主暂时不可用或迁移无法验证时，隐藏面板并返回诊断状态。

目标窗口跨 Space 后仍按原窗口捕获；应用共享在目标不属于当前 Space 时收窄为原窗口流，`mode` 表示请求范围，`captureMode`/`scope` 表示实际捕获范围。显式桌面共享暂不可用时保留绑定并显示等待状态，目标桌面恢复后重建原显示器上的流。有效共享租约期间，目标回到当前桌面也不会再让窗口预览自动消失。

## 固定浏览器目标与后台操作

菜单栏中的“浏览器任务”展开同一面板，可查看任务、活动和候选下载，停止当前浏览器控制，截取任务页面并添加或导出批注。截图可能包含页面可见内容；候选下载的归属与保存位置仍需在浏览器下载记录中确认。扩展弹窗保留站点授权、连接状态和诊断。升级时需重新加载扩展并更新原生宿主，使应用运行会话与退出行为保持一致。

列出窗口只返回列表，不改变已选目标。缓存目标关闭时返回 `TARGET_LOST`；没有目标且存在多个窗口时返回 `TARGET_REQUIRED`，需要显式选择。已绑定任务仍校验原窗口、进程身份与连接，不会按当前焦点或窗口排序换目标。

Chrome 页面操作优先使用已授权扩展，保持同一任务、浏览器窗口 ID 和标签 ID：先 `quietbridge_browser_events(action="candidates", taskId=...)`，等待扩展刷新后再次查询，用返回的 `browserWindowId/tabId` 调用 `assign`，随后执行 `command(operation="snapshot")` 并查询 `result`。点击使用该快照中的 `ref`；每次动作后再读取快照确认页面变化。`nativeConnection=connected` 仅证明宿主可达，`paired=false` 表示尚未完成标签分配，不应改用重复开窗恢复。

`create_target` 供任务明确需要新建且尚无目标窗口时使用，采用 `focused=false`。故障恢复仅允许一个例外：`task status.targetRecovery.status=recreate_allowed` 证明本任务自己创建并完成映射的 Chrome 专用窗口已关闭，原进程、连接、应用会话与宿主仍有效。Agent 先 end 自己的失效界面阶段，再 begin(recoveryOf=旧taskId,targetSurface=web_content)，在新阶段创建一个替代窗口，重新核实并绑定、分配、映射、共享和观察。恢复链最多自动替换两次；两次后再次关闭只要求用户选择，不推断取消意图。恢复失败也暂停选择，不能丢弃 recoveryOf 绕过预算。用户明确选择重建后，才可开启新一轮已授权阶段。既有用户窗口、原生窗口、仅标签消失或无法证明关闭时不自动重建。

同一任务重复 create_target 会返回原请求或已创建窗口；超时不会生成新请求。重复 begin(recoveryOf=同一旧taskId) 复用仍有效的恢复阶段，不复活已结束阶段。扩展把创建标识及结果存入会话存储，worker 重启后只恢复已知结果，结果未知则停止重放。已绑定任务返回 `TARGET_ALREADY_BOUND`，继续使用既有标签。重建不恢复草稿，也不能证明旧业务输入未生效；提交、支付、发送等未知结果必须先查询或观察。

点击仅在目标应用和目标键盘窗口已经位于前台时使用 AX 动作，后台目标走 PID/窗口定向事件；无法读取点击前的焦点状态时以 `FOCUS_STATE_UNKNOWN` 拒绝。回执记录前后 PID、键盘窗口 ID 和 `focusPreserved`，不自动激活另一个应用恢复焦点。网页任务始终走扩展的固定标签通道；扩展未绑定、共享未核实、权限不足或返回窗口投递错误时报告阻塞，不通过切换前台或新建窗口继续。前后采样相同不证明全程没有短暂切换，仍需结合实际体验核实。

## 操作诊断日志

界面观察和鼠标操作默认写入 JSONL 日志。普通安装位置为 `~/Library/Logs/quietbridge/bridge-operations.jsonl` 与同目录的 `native-operations.jsonl`；设置 `QUIETBRIDGE_CONFIG_DIR` 时位于该目录的 `logs/`。可以用 `QUIETBRIDGE_DIAGNOSTICS_DIR` 指定目录，或以 `QUIETBRIDGE_OPERATION_LOG=0` 关闭。环境变量需要传给桥接和 helper；更改后重新连接。

每次工具调用返回 `structuredContent.diagnostics`，包含 `toolTraceId` 和日志路径。桥接的 `nativeRequestId` 对应原生日志的 `requestId`，可串起请求发送、几何校验、事件创建/投递、结束或错误。点击结果中的 `pointer` 包含窗口边界、窗口内坐标与最终屏幕坐标。原生日志还会记录 CG/AX 边界差异、观察后窗口移动、显示器位置以及实际投递路线。

预览诊断另含 `hostWindowId`、`hostSpaceIds`、`panelSpaceIds`、`panelSpaceVerified`、`panelPlacementError`，用来核对 WorkBuddy 宿主与预览面板是否在同一组 Space；`trackingWindowId` 始终表示独立的捕获目标。原生日志在这些绑定状态变化时记录 `preview.hostRoute`。

坐标点击的 `x/y` 始终是**窗口左上角起算的 macOS point**。截图的 `pixelWidth/pixelHeight` 是像素，日志中的 `pixelsPerPointX/Y` 给出比例：窗口内截图像素位置应分别除以该比例，再作为点击坐标；整个桌面的截图坐标还需减去窗口原点。共享预览面板的显示尺寸也不等于点击坐标尺寸。

`pointer.event.posted` 只说明事件已投递，不证明应用收到或界面发生变化；`verified=false` 会保留。日志不自动重放输入，不记录输入文本、按键内容、页面/窗口标题、AX 正文或截图数据。原生文件写入使用独立队列，避免磁盘写入插入按下与抬起之间。每个文件最多 5 MiB，保留一份轮转文件 `.1`，新建文件权限为 `0600`。

## 注册

Workbuddy 适配器通过 `adapters/workbuddy/configuration.py` 合并
`~/.workbuddy/mcp.json` 和停止 hooks。PKG 使用稳定的 `connector.py` 入口，
原生版本由 `active.json` 指定。新安装的连接名是 `quietbridge`；已有且可确认
属于本产品的旧连接键与稳定路径沿用，保留信任及启用状态。

新工具使用 `quietbridge_*`，旧 `cua_*` 调用映射到同一实现。环境变量优先使用
`QUIETBRIDGE_*`，未配置时兼容旧 `CUA_*`。首次连接仍由用户在宿主中确认信任。
DSH 已接入源码级适配：`adapters/deepseek_harness` 渲染 Host bundle，每 Agent 一个连接器并注入可信
环境身份，宿主上下文路由按 lifecycle store 的 turn 记录放行/拒绝；详见
[DSH 宿主契约](../../docs/architecture/dsh-host-contract.zh.md)。未经真实 profile 安装与端到端验收前
不宣称 DSH 可用或支持多 Agent 并行。整体方案见[整体方案](../../docs/architecture/quietbridge.zh.md)。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `QUIETBRIDGE_HELPER_PATH` | 辅助程序绝对路径，默认指向 `apps/macos/QuietBridge.app/Contents/MacOS/quietbridge-native` |
| `QUIETBRIDGE_READ_ONLY=1` | 只暴露观察类工具（permissions / apps 列表与选择 / windows / state / screenshot），禁输入 |
| `QUIETBRIDGE_ALLOWLIST` | 逗号分隔的 bundleId 或应用名白名单，`quietbridge_apps` 选应用时校验 |

## 工具（19 个）

| 工具 | 对应 helper 方法 | 说明 |
| --- | --- | --- |
| `quietbridge_situation` | 有界 `getState` 等只读查询 | 现场状态及共享状态，不启动共享、不抢焦点 |
| `quietbridge_task` | 持久任务与窗口身份核验 | 固定目标和操作模式；结束时释放共享 |
| `quietbridge_browser_session` | `browserSession` | 受管 Chrome 会话与已有窗口绑定，绑定不等于共享就绪 |
| `quietbridge_permissions` | `permissionStatus` + `getCapabilities` | 授权、平台、会话锁和任务隐私层状态；`action=open_settings` 打开系统设置 |
| `quietbridge_apps` | `listApps` / `findApps` / `getApp` / `launchApp` | `action=list\|find\|get\|launch`，`get` 返回 `appHandle` |
| `quietbridge_windows` | `listWindows` / `preview` / `activate` | 窗口列表及共享管理；不抢焦点任务禁止 `activate` |
| `quietbridge_state` | `getState` | 紧凑 AX 树 + `snapshotId`，元素级操作的入口 |
| `quietbridge_wait_ready` | 有界 AX/视频帧观察 | 在同一目标与时间预算内读回，不激活或输入 |
| `quietbridge_visual_state` | `visualState` | 新视频帧观察，复用独立的实时共享预览 |
| `quietbridge_screenshot` | `screenshot` | 返回 PNG + 像素尺寸 + 屏幕 bounds，并落盘到 `shots/` |
| `quietbridge_click` | `click` | 新快照元素或窗口局部坐标；后台窗口定向点击，核对焦点回执 |
| `quietbridge_hover` | `hover` | AX 或窗口定向悬停，不移动物理鼠标 |
| `quietbridge_type_text` | `typeText` | 应用级文本事件；绑定桌面任务拒绝，不能为此激活或解除绑定 |
| `quietbridge_paste` | `paste` | 应用级剪贴板输入；绑定桌面任务拒绝，不能为此激活或解除绑定 |
| `quietbridge_key` | `pressKey` | 应用级按键；绑定桌面任务拒绝，不能用于全局焦点切换 |
| `quietbridge_scroll` | `scroll` | 窗口内滚动，`deltaLinesY>0` 显示上方内容 |
| `quietbridge_drag` | `drag` | 窗口内拖拽，默认 400ms / 15 步 |
| `quietbridge_set_value` | `setValue` | 直接写 AX value，仅对 `valueWritable` 元素有效 |
| `quietbridge_browser_events` | 授权浏览器扩展 | 固定标签的后台观察与输入，先核验映射并建立窗口共享 |

## 四个必须记住的口径

1. **坐标系**：`quietbridge_state` 树里的 bounds 是**屏幕坐标**；`quietbridge_click/scroll/drag` 的 `x/y` 是**窗口左上角起算的 point**（含标题栏）。换算：`窗口坐标 = 屏幕坐标 - 截图 bounds.x/y`。Retina 下 PNG 像素是 point 的 2 倍，别把像素当 point。
2. **快照失效**：每次 `getState` 会升代次，旧 `snapshotId` 再用就是 `STALE_SNAPSHOT`；元素操作必须用同一轮观察拿到的快照。
3. **不自动重试**：输入类请求失败可能已部分生效，桥接层对输入**永不重放**（只对 `listApps`/`getState`/`screenshot` 这类幂等读取重启重试一次）。
4. **焦点与共享约束**：界面操作必须使用已核验的共享窗口和后台定向通道；需要前台、窗口投递失败或共享未就绪时报告阻塞，不能请求激活来兜底。网页只走已绑定扩展。

## 任务隐私保护层

`quietbridge_task action=bind` 成功后，helper 建立任务租约及防睡眠断言，隐私状态进入 `watching`：此时不盖屏，也不启动 guardian，用户仍可触发系统锁屏。

启用下述可选 Locked use 后，交接流程为：

1. 首次观察到系统锁屏，先准备主进程覆盖层与独立 guardian 的第二层覆盖；覆盖及监听就绪后，才签发最长 12 秒、只消费一次的自动解锁票据。
2. AX 可以轮询读取登录界面，但每次自动解锁流程只提交一次非机密探针串。确认系统自动解锁后，显示 QuietBridge 的绿色像素浮层；初次锁屏快捷键的修饰键松开不会被当成新的用户输入。
3. 浮层显示后，任意新按键、鼠标点击或滚轮输入都会撤销自动解锁票据、取消后续登录界面 AX 动作，再请求真正的系统锁屏；这表示用户选择手动解锁，不暂停或取消业务任务。
4. 确认系统锁定且没有已消费、尚未落地的 ALLOW 后，才撤掉两层覆盖，并继续观察手动解锁。系统确认解锁后，原任务和目标绑定保持可用；重新观察原目标、核实共享后继续未执行步骤，不重放结果未知的操作。`manualSuppression` 只禁止自动解锁授权，不是业务任务的暂停开关，也不要求为解锁而 end/begin/bind。

最后一个有效会话参与者停止或会话保护超时，若已经覆盖桌面，也先交还真实系统锁屏再释放覆盖；`watching` 阶段直接结束。界面阶段的 end 不触发此收尾。已交给 authd 的 ALLOW 不能撤回，若其在接管后才实际解锁，必须再锁一次。自有浮层不替代系统安全边界，也不读取或保存真实密码。

2026-11-07 修正了解锁与任务生命周期的边界：关闭 Locked use 不再建立锁屏覆盖；状态读取及解锁监测独立于授权 socket，停止覆盖后仍读取实时系统状态。状态可从 `getCapabilities.sessionPrivacy` / `powerGuard.privacy` 查看。

### 可选 Locked use

仓库内另提供实验性 macOS Authorization Plug-in：

```text
system/authorization-plugin/
```

本项目的系统接入方式如下：

- 修改 `system.login.screensaver`，保留 `use-login-window-ui`；
- 安装 `WorkBuddyCUAAuthorizationPlugin.bundle`；
- 通过 Unix socket 的内核 audit token 校验对端：插件用 SecTask 核对 helper 的签名 identifier/Team ID，helper 验证 Apple 授权宿主；
- 协议仍为 `AUTHORIZE 3` / `ALLOW 3`；只有活动任务、未处于手动认证抑制、保护租约有效且票据未过期或消费时才允许；
- 安装前保存原始 authorization right，支持完整卸载恢复。

该插件默认未安装。安装会修改系统登录策略，必须在受控账户或虚拟机中由管理员显式执行。`quietbridge_permissions` 的状态文本会显示 Locked use 是否可用以及允许/拒绝次数。

## 已知退化路径

- **自绘界面**（游戏、Unity、canvas、部分 Electron 控件）：AX 树为空，`quietbridge_state` 会明确提示，只能截图 + 坐标点击。
- **浏览器**：原生 helper 的 `getCapabilities.browser=false` 不包含独立扩展能力。DOM 操作需要 Chrome 扩展启用、站点授权、宿主连接和显式标签分配均就绪；否则原生 AX/坐标不能充当无干扰的后台网页控制。
- **敏感场景**：常规桌面工具不能操作安全输入框、TCC 系统弹窗或真实系统锁屏（`sessionLocked=true`）。上述可选自动解锁使用独立、受票据约束的登录界面 AX 流程，任务隐私层不存储密码。
- **截图体积**：2116×1394 约 375 KB base64，连续截图会显著吃上下文，按需再截。

## 重编代价：已改成稳定签名（2026-10-30）

`QuietBridge.app` 原本是 ad-hoc 签名。替换 `Contents/MacOS/quietbridge-native` 或 `codesign --deep --sign -` 会让**辅助功能授权立即失效**（实测 `accessibility=False`，屏幕录制保留），必须去系统设置把 `quietbridge` 关掉再打开。

现在改用本机已有的开发者证书签名，designated requirement 绑到证书而不是 cdhash：

```bash
cd "apps/macos"
swift build -c release --disable-sandbox
cp .build/release/quietbridge-native "QuietBridge.app/Contents/MacOS/quietbridge-native"
codesign --force --deep --sign "Apple Development: Shaokui Gu (ATGH8CN43J)" "QuietBridge.app"
```

实测连续两轮「重编 + 替换 + 重签」后 `quietbridge_permissions` 仍为 `accessibility=True`，**不再需要重新授权**。换证书或换机器仍需授权一次。

## 跨桌面预览面板（早期实现记录）

本节以下描述早期合成路径，不代表当前捕获策略。当前原生应用和 Chrome 网页任务都使用 ScreenCaptureKit 连续视频；核心收敛后的扩展源码不再隐式截图促帧。

目标窗口 `isOnScreen=false`（在别的 Space / 另一块屏幕）时，helper 会弹一块 380×271 的悬浮面板（`SpacePreview.swift`，标题「跨桌面预览」），每 0.6s 抓一帧桌面对该 Space 做合成：把属于该 Space 的窗口按原坐标画在**中性灰背景**上。于是面板里没被窗口覆盖的区域会是大片纯灰——那是背景，不是没渲染。

- 目标窗口回到当前桌面时自动隐藏；30s 无操作自动关闭。
- 面板和 80×80 代理光标都是 helper 自己的窗口，**截图（`quietbridge_screenshot`）与屏幕录制都拍不到**，只能肉眼看屏。用 `screencapture -l <windowNumber>` 才能单独拍到它们。
- 面板定位：有真正的宿主 App 时贴宿主窗口右下；**headless 启动（责任进程解析成 helper 自己）时固定贴主屏 `visibleFrame` 右下角，避开 Dock 与菜单栏**。

修过的两个坑（都会让面板「像没出现」）：

1. `track()` 原要求 `onCurrentSpace == false` 且 `spaceIDs` 非空，窗口 Space 归属为 `null` 时直接 `stop()`；现回退用 `isOnScreen` 判断。
2. `hostWindowFrame()` 原会把**面板自己**当成宿主（380×271 通过 `>300×200` 筛选），导致面板每帧以自己为锚点、每 0.6s 向左下滑 16pt，几秒就滑出屏幕。现在责任进程等于自身 pid 时直接按 headless 处理。

## 光标为什么看不到

三件不同的事，别混在一起：

1. **系统鼠标指针不会动**。helper 走 SkyLight / `CGEventPostToPid` 的**窗口定向投递**，事件直接送进目标进程，不经过硬件光标。实测点击后 `CGEvent(source:nil).location` 只反映人手动鼠标的位置，程序从未改写它。
2. **替代指示是 80×80 的代理光标浮层**（`CursorOverlay.swift` 的 `AgentCursor`，`NSPanel` + `screenSaverWindow` 层级）。实测点击后确实出现：`layer=1000 pid=<helper> owner=quietbridge 80x80 alpha=1.0`。三个条件会让人以为"没有光标"：
   - **30 秒无操作自动隐藏**；
   - 只在指针类动作（click/hover/scroll/drag）时刷新位置，`quietbridge_key`/`quietbridge_type_text`/`quietbridge_state` 不画；
   - 它是**独立浮层窗口**，`quietbridge_screenshot` 走 ScreenCaptureKit 窗口捕获，**永远拍不到它**，也拍不到系统光标。
3. **跨桌面会画到看不见的地方**。浮层坐标由窗口所在的窗口服务器空间算出；目标窗口 `isOnScreen=false` 时，代理光标与点击都落在另一个 Space。桥接层现在会在结果里附带这条告警。

以上为早期代理光标的显示行为说明。当前不抢焦点任务禁止为查看光标调用 `activate`、拉动窗口或切换 Space；按当前预览和操作回执核实。

## 自检

```bash
/usr/bin/python3 /tmp/quietbridge_mcp_harness.py   # 本地 MCP 客户端：initialize → tools/list → 12 次调用
```

桥接日志写 stderr（`[quietbridge-bridge]`），stdout 只走 JSON-RPC；helper 的 `[quietbridge-screenshot]` 阶段诊断只保留在环形缓冲里，不刷屏。

## 授权归属

TCC 授权跟着 helper 的**责任进程**走。实测两种形态：

- 由 dsh 宿主拉起：`responsibleApp` 是宿主 App（如 `DeepSeek Harness.app`）；
- 由桥接层直接拉起（无宿主）：责任进程解析为 **`quietbridge` 自己**，授权就记在这个 app 上。

`quietbridge_permissions` 返回的 `launchApplication` 就是当前承载授权的那个 app；显示 `false` 时用 `action=open_settings` 打开面板给它授予辅助功能与屏幕录制。移动 `.app` 位置仍可能导致授权失效。


## 严格后台浏览器模式（扩展 0.3.3）

界面阶段完成调用 `quietbridge_task end`：释放本阶段绑定和共享预览，保留周围宿主运行的会话保护。`end` 和 `status` 返回 `sessionProtection`，包括 retained、scope、idleTimeoutSeconds 和 remainingSeconds；没有活动阶段时也可读取。 `end` 另返回 `previewCleanup`，区分已回执释放、原生连接已关闭、清理失败和未确认；不能用 `sessionProtection.retained` 推断共享是否仍在。阶段结束后仍可幂等 `unshare` 清理本连接预览，不释放宿主会话保护、不启动新 helper；其他活动连接的资源仍受所有权检查。MCP 的 `notifications/cancelled` 和 stdin EOF 在独立读取线程中处理，取消当前请求并释放该连接的共享预览、保护与绑定；队列中的输入不再派发。取消可能发生在部分输入之后，已执行结果不会回滚或自动重放。

界面任务由桥接内核文件锁证明连接归属，持有 120 秒无操作租约。实际任务操作续期；权限/任务状态查询、MCP ping 和扩展心跳不续期。该租约到期释放阶段预览并将已绑定目标记为 `suspended`，不结束仍有效的会话保护。等待用户输入后，只要原执行 worker 和 QuietBridge 应用会话仍在，下次原目标调用会自动重取句柄、核验进程代际与窗口，返回 `TASK_RESUMED` 及 `resumption.nextSteps`；Agent 应立即恢复共享、取得新快照并继续已授权工作，无需用户提醒重连。恢复调用不会执行其携带的点击或填写，旧引用不能继续使用。Chrome 自动为原标签生成新绑定，必须重新验证窗口映射、建立共享并获取 snapshot；旧文档、旧映射和待发命令不复用。目标变化、其他会话占用、权限撤销或应用重启仍会阻止恢复。

已核实宿主会话/轮次的保护允许 30 分钟无实际工具活动；同轮次其他业务工具的 PostToolUse 仅记录活动时间，不保存内容。持续实际活动可以运行超过 30 分钟；连接在线不能代替活动。未核实归属时仍按 120 秒兜底。等待时不通过心跳保持共享或无限续租，恢复仅由后续目标调用触发。桥接崩溃时文件锁自动释放，扩展在下次同步撤销分配并取消待发命令和建窗请求；已发出的动作不能撤回。

原生预览与保护分别有 120 秒 socket 资源租约。桥接只在自己的会话租约有效时每 30 秒续原生保护，不延长阶段/预览或伪造宿主活动。各连接独立持有保护，释放、断开和到期仅移除自身；最后一个参与者退出才全局释放防睡眠和锁屏保护。最多三个可信宿主会话分别持有任务和共享预览；卡片切换只改变查看画面，不改变任务目标。原生同进程窗口仍按进程互斥。共享 MCP 缺少可靠逐调用身份时不能混用目标；新阶段需核实与原保护属于同一宿主运行，否则返回 `SESSION_ROUTING_REQUIRED`，不接管或撤销原会话。

等待暂停记录保留 `suspensionReason` 和 `suspendedAt`，状态查询会给出自动恢复步骤，但不会自己唤起共享。`TASK_SUSPENDED`/`TASK_RESUMED` 不要求再次征求用户继续；最多两次自动复查仍受阻时再请求选择。中断记录保留 `interruptionReason` 和 `interruptedAt`；`TASK_CANCELLED` 或 `interruption.explicitStop=true` 表示明确停止，不复活旧任务。其他 `TASK_INTERRUPTED` 表示旧界面阶段不可继续，保留业务进度并请求用户选择；继续时先核实已有结果，再 begin 新阶段并确认目标。end、unshare、取消、FinalStop、SessionEnd 与连接器退出会丢弃原绑定的空闲自动恢复意图；显式 recoveryOf 是独立且受限的窗口关闭恢复链。`TASK_IN_USE` 禁止接管另一活动连接。`TARGET_MISMATCH` 只说明目标不一致，先检查任务和原目标，不能直接判定为旧绑定并自动清理。

窗口冲突 `TARGET_IN_USE` 的 `structuredContent.targetConflict` 返回实际占用任务、窗口、PID、模式、冲突范围和可核实的宿主会话 ID。Agent 应向用户说明这些身份，不能从页面标题猜测占用者或自动结束他人的任务。用户可在 QuietBridge“任务与窗口”核对身份并点击“停止此会话控制”：直接撤销对应宿主本轮，扩展离线或执行者持有任务锁时也能发起；stopping 期间保持目标互斥，资源收尾后释放。原轮次不能重新创建任务抢回窗口，其他独立会话继续执行。此入口不修改 WorkBuddy 定时计划，停止 HTTP 测试服务也不释放另一个采集会话的窗口租约。实现和隔离测试见 [窗口占用停止修复](../../docs/implementation/2026-11-12-window-owner-stop-fix.zh.md)。

macOS PKG 从 0.5.53 起合并注册 Workbuddy 支持的 `PostToolUse`、`FinalStop` 和 `SessionEnd` 命令回调。成功 begin/bind 的回执把本轮宿主会话与确切任务 ID、租约拥有者关联，保留各阶段记录；FinalStop 撤销该轮，SessionEnd 撤销该会话。阶段已 end、没有活动任务时也保留停止关联。即使没有正在运行的 MCP 请求、stdio 保持连接，桥接也在下一次 500ms 检查时关闭自己的原生连接；其他保护参与者继续持有，最后一个退出才交还系统锁屏。执行中的调用同时收到取消信号。回调不保存工具内容、对话正文或宿主日志，不终止 Workbuddy，不重连连接器。异步回执用于停止关联，不能当作逐调用身份路由。

宿主不支持回调或回调未执行时，保留 120 秒无实际操作的租约兜底；不能据此声称立即识别了宿主“停止”。此机制释放任务资源，不退出菜单栏应用、其他会话或用户目标应用，也不关闭用户标签和窗口。

`quietbridge_task begin` 不指定操作对象和模式时默认 `interactionMode=background_browser`。原生点击、悬停、滚动、拖动、键盘、粘贴、设值和窗口激活均被桥接层拒绝；扩展断开或版本能力不足时等待原通道恢复。原生界面在新任务 begin 时用 `targetSurface=browser_ui/native_dialog/desktop_app` 自动选 `desktop`；也兼容旧的显式 `interactionMode=desktop` 调用，不能在已开始的任务内改模式。旧任务缺少模式字段时按后台模式处理。

浏览器仍需先绑定确切原生窗口，再通过 `quietbridge_browser_events candidates/assign` 分配真实 Chrome 标签 ID。原生窗口预览还需 `verify_mapping` 确认系统窗口与浏览器窗口对应；映射未确认时只使用扩展快照和截图，不猜测新窗口并开始共享。任务归属以固定标签和文档为准，用户焦点变化不会改绑；Chrome 标签被移到其他窗口时会暂停，需移回原任务窗口或重新明确建立任务。浏览器窗口 ID 与系统窗口 ID 不可互换。

候选查询异步刷新，只返回尚在 20 秒有效期内的缓存，并附 `refreshPending`、`candidateAgeMs`、`candidateTtlMs`。0.5.43 的 `assign` 遇到已选择候选过期时，会自动请求扩展重新枚举元数据并等待至多 3 秒；原标签、浏览器窗口、脱敏地址、应用会话及原生任务目标均未变化才分配，随后重新执行原生目标校验。刷新期间释放文件锁，不延长旧证据有效期，不重放网页动作，也不新建窗口。标签关闭、移窗、地址变化、权限撤销或目标失效时拒绝分配；未及时回传时返回 `CANDIDATE_REFRESH_TIMEOUT`，保留原任务供连接恢复后重试。成功结果与 `browser.assigned` 日志附 `candidateRefreshed`、`candidateAgeMs`。
0.5.44 的 `assign` 遇到已选择候选过期时，会自动请求扩展重新枚举元数据并等待至多 3 秒；原标签、浏览器窗口、脱敏地址、应用会话及原生任务目标均未变化才分配，随后重新执行原生目标校验。刷新期间释放文件锁，不延长旧证据有效期，不重放网页动作，也不新建窗口。标签关闭、移窗、地址变化、权限撤销或目标失效时拒绝分配；未及时回传时返回 `CANDIDATE_REFRESH_TIMEOUT`，保留原任务供连接恢复后重试。成功结果与 `browser.assigned` 日志附 `candidateRefreshed`、`candidateAgeMs`。

扩展的 `scroll` 在固定文档中滚动已验证的页面根或快照元素，并回读滚动位置；不依赖隐藏窗口的 CDP 滚轮回执。调用只接受 `direction=down/up` 和可选 `lines`，由桥接层转换为扩展内部滚动量，避免调用方猜测正负号。其他 CDP 命令设置不超过 3 秒的有界回执等待；结果未知时断开并暂停该标签，不重放动作，需重新分配后观察。

导航已受理且落地地址与预期不同时，新的只读观察返回 `verification.reason=navigation_destination_changed` 和 `nextAction=snapshot`，状态也返回 `verificationNextAction`。获取当前文档 snapshot 核实后继续原绑定，不需重新 assign；不凭重定向推定业务成功，不接受旧文档、旧 epoch 或未就绪文档作为导航复核。

新版 snapshot 的 `coverage` 明确返回交互元素数量、300 条上限及实际截断标记；恰好 300 条不自动算截断。`elementCount` 是顶层 DOM 节点数（上限 10000），`elements` 是顶层及可访问同源 frame 中的可见交互元素，两者不必相等。旧扩展缺少 coverage 时不能据此判断完整性，长页面阅读仍使用有分页的 `read`。

执行流程：`command(operation=snapshot)` → `result(commandId)` 取得 `snapshotId` → `command(operation=click/fill/select/scroll, snapshotId=..., ref=...)` → 查询结果 → 新 snapshot 复核。每个命令携带任务分配、tab、documentId、导航 epoch 和过期时间。旧文档、旧快照、过期命令不投递。`performed` 仅代表已分派，不能据此声称业务完成。超时、取消或错误的已投递动作可能已发生；不重放，先观察。

点击和文本输入通过指定标签的 CDP 通道执行，使用页面视口坐标；点击直接发送按下／松开，发送前复核文档和命中元素，不再先等待独立 mouseMoved。滚动和 select 保留文档内 DOM 操作。`keypress(key=Enter/Tab/Escape/ArrowDown/...)` 仅接受有限页面按键，不接受系统或地址栏快捷键。`screenshot` 用该标签的 `Page.captureScreenshot`，不使用桌面截图，不会复核业务动作。DOM 引用覆盖顶层文档及可访问的同源 iframe；跨域 iframe 等未覆盖的网页内容应报告限制，不能回落桌面点击。浏览器自身控件和真实原生对话框按上述独立原生阶段流程操作，受后台投递限制。

扩展弹窗可暂停当前任务的浏览器控制、查看会话内活动记录、截取并批注当前任务页面。暂停不会撤销已发出的动作，恢复需重新分配目标。下载列表只显示调试协议报告的任务标签候选事件，不证明文件最终归属或保存位置；部分浏览器或下载方式可能不产生事件。活动、截图和批注保存在浏览器会话存储中，随会话结束清除。主动快照会包含可交互元素名称，截图可能包含页面内容，诊断可能包含页面错误文本。

受控的 `window.open` 和新窗口链接被约束到原任务窗口，以 `active=false` 创建子标签，结果中回传 `children`。新窗口和子标签先创建为空白页，安装弹窗约束后再导航；创建中途失败时清理本次已创建的子标签，避免遗留未登记目标。`tabs` 查看归属，`select_tab(tabId=...)` 显式切换任务的逻辑目标，保留根标签与任务窗口，不激活实际标签；切换后先等待新文档并取 snapshot。新窗口 POST 表单、依赖 WindowProxy 返回值的弹窗和部分异步导航弹窗不支持自动转送；不得把动作分派当作成功。任务清理时尝试恢复扩展安装的弹窗钩子并释放调试连接；站点权限已撤销时，页面钩子可能要重载页面才清除。此约束不构成对任意网站脚本或浏览器原生界面的全局焦点保证。

预览面板与捕获源分别绑定：预览面板跟随 WorkBuddy 的窗口、显示器和 Space。Chrome 网页任务和其他应用统一使用系统窗口/应用连续视频；系统流可能显示窗口实际选中的标签，不能证明后台任务标签的页面状态，逻辑标签仍以扩展 snapshot/screenshot/read 为准。

0.6.26（扩展 0.4.14）移除动作后的截图促帧、导航后 5 秒单次截图及相关原生 Space 状态轮询。普通观察、导航、点击、填写和滚动均不隐式截图，预览只使用系统窗口连续视频；旧扩展回传的实验报告和 screenshot-mailbox 帧会被丢弃，不能重启旧截图租约。显式 screenshot 仍是独立观察能力；render_probe 仅在明确诊断时调用，不能进入日常操作前置流程。Chrome 在其他 Space 的视频呈现仍受平台渲染行为影响，不以截图模拟连续视频。后台网页控制不等待视频首帧，身份或权限失败也不切换到前台输入。

旧协议的预览邮箱模块保留供识别和清理遗留状态，握手无条件撤销旧截图租约和帧。生产流程不再执行绘制提醒截图。当前改动及其边界见[核心能力收敛](../../docs/implementation/2026-11-12-core-stability-convergence.zh.md)；后续安装新版后使用[核心操作验收提示词](../../docs/implementation/core-control-acceptance-prompt.zh.md)。

升级需重新加载扩展 0.3.2；从尚未授权 debugger 的版本升级时，在 Chrome 提示后由本人确认该权限。该权限用于固定标签的浏览器调试协议；缺权限时不尝试其他输入通道。重新连接 WorkBuddy MCP 后开始新任务。已在实际 Chrome 上验证新建窗口、候选分配、页面快照、填写、滚动、截图及任务结束后的窗口保留；不同 macOS Space 和特殊站点弹窗仍须单独验证。

0.3.2 使用已授权的 CDP `Page.getFrameTree` 核验新建标签的 `about:blank` 主文档，等待至多 1 秒；HTTP(S) 站点权限下 `tabs.get` 可能始终不返回空白页 URL，因此不再以该字段作为就绪条件，也不增加 `tabs` 权限。期间持续校验原窗口、标签和应用运行会话，安装弹窗约束后再次核验主框架及文档身份。准备失败不导航，并释放调试连接；导航下发前保存记录，结果未知时不自动重放。`create_target` 的既有结果及 `status.createdTarget` 返回 `navigationState`（`issued/failed/unknown/not_requested`）和可选 `navigationError`。`issued` 仅表示浏览器已受理导航，仍须刷新 HTTP(S) 候选再分配；空白页不会作为普通网页候选，也不能用系统窗口 ID 代替扩展窗口 ID。

`desktop` 任务通过原生 `windowIdentity` 核验指定应用句柄、进程代际与窗口归属；不再使用 Chrome 会话的 attach 校验。后台浏览器任务继续使用 Chrome 固定路径和进程身份验证。目标应用结束或窗口消失后，任务失效并释放隐私层与防睡眠租约。

0.5.51 升级边界：安装新版菜单应用不代表宿主已替换内存中的旧 stdio 进程。安装器会核对运行中的旧 bridge，发现后报告 `host_refresh_required`，即使 mcp.json 已指向固定入口也不会冒充自动切换成功。旧 helper 经签名、归属、版本及进程路径核验后退出；应用会话记录使用 version=2，旧版只读裸 UUID 的组件无法再次接入。原生租约使用独立 Dispatch 计时，并记录 `task.resource_acquired/released`、`task.resources_revoked` 与撤销原因；桥接记录 `task.interrupted`。

0.5.52（build 80）：升级了旧连接迁移提示。后续测试确认，工具调用间隙的停止不一定传到 MCP：Workbuddy 在 16:23:21 取消会话，直到 16:24:44 回收连接后预览才释放；这不满足立即关闭要求，0.5.53 为此补上宿主回调。Workbuddy 若仍缓存旧启动命令，普通连接器重连可能再次启动旧脚本；首次迁移请先保存工作，再完整退出并重新打开 Workbuddy，使其重新读取固定入口配置。

服务 initialize 信息和所有 tools/list 条目现在通过 MCP 标准 `icons` 返回同一枚 QuietBridge Logo（内嵌 64×64 PNG，不依赖外部网址或本地文件 URL）。支持该字段的 Agent 客户端可展示此图标；实际工具调用卡片的展示由客户端决定，MCP 服务不能强制替换宿主的通用工具图标。


## 扩展准备检查与会话隔离

0.6.15 起，未建立任务也可以检查浏览器通道：`permissions.browserChannel` 只表示当前菜单应用会话下的全局扩展心跳、版本与授权有效；无活动任务的 `browser_events candidates` 返回 `connectionScope=extension_preflight`、真实握手 `clientVersion` 与候选元数据。`taskChannelReady=false` 在此阶段是正常值，不表示需要重载扩展。这条查询不占三任务槽、不创建任务或共享、不改变其他会话的绑定和命令。

begin 后仍使用原会话私有通道。扩展发现新通道并第一次 sync 前，候选查询/首次 create_target 最多等待 3 秒；若全局扩展在线但私有通道仍未就绪，返回 `channel_pending` / `BROWSER_CHANNEL_STARTING`，按 retryAfterMs 最多两次只读检查，持续失败再报告。不能把全局候选复制为私有 assign 证据，不能降级原生输入、重载应用或重复新建目标。真正的全局离线、授权撤销、应用会话变化仍按已有恢复规则阻断。


### 分阶段呈现诊断（0.6.16 / Browser 0.4.7）

`quietbridge_browser_events action=command operation=render_probe stage=inspect|attach|enable|layout|capture` 仅用于明确约定的诊断。每次只派发一个阶段，使用原 commandId 查询结果；没有任意 CDP 方法入口。`inspect` 不连接，`attach` 不启用 Page，`enable` 不查布局，`layout` 不截图；`capture` 只消耗当前文档 30 秒内的 layout 缓存执行一次截图，丢弃图像，只返回 base64 字符长度。未知或失败结果不自动重放。探针不能代替动作/导航的正常复核，也不解除 pendingVerify。

结果包含 stage、startedAt/completedAt（Unix 秒）、performed、attachedBefore/After、pageEnabledBefore/After；layout 多返回 viewportWidth/Height，capture 多返回 captureDataLength。已有连接的阶段 performed=false，不能说成刚刚建立连接。成功结果经固定字段和状态转移校验；bridge 日志 browser.render_probe_result 只保存此元数据，按 taskId/commandId 关联，result 重复轮询可能产生重复日志，分析工具会去重。不保存图像、页面内容、URL 或任意错误文本到探针日志。

普通 navigate 的 run 前置保护也会 attach/enable；不能把首次截图直接视为首次建立调试连接。扩展截图与 render_probe 仍可能影响后台呈现，均不是被动测量或自动修复策略。
