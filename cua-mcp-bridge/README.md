# cua-mcp-bridge

把 `workbuddy-cua-native`（macOS 原生 computer-use 辅助程序）包装成 MCP stdio 服务，
让 WorkBuddy 能直接调用桌面操作能力。

```
WorkBuddy ⇄ MCP(stdio/JSON-RPC) ⇄ server.py ⇄ workbuddy-cua-native ⇄ 共享守护进程 ⇄ AX / SkyLight / ScreenCaptureKit
```

辅助程序本身只讲私有 JSON-lines 协议（`protocol.schema.json`），WorkBuddy 只认 MCP，
这一层就是两者之间的单向翻译 + 体积裁剪，Swift 侧零改动。

## 共享范围

应用内的观察、截图及输入默认使用 `application`，通过 ScreenCaptureKit 只共享目标应用在目标显示器上的窗口，不包含其他应用。共享面板会标注“应用共享”。

任务涉及跨应用、Dock 或系统界面时，先调用 `cua_windows(action="share", appHandle=..., windowId=..., source="desktop")`，共享目标窗口所在显示器的当前桌面。后续观察及输入会保留所选范围；仅需要指定窗口时仍可显式使用 `source="window"`。

范围只跟随同一连接及同一应用窗口，结束共享、新任务、更换目标或重连后恢复应用共享。目标位于后台 Space 时，应用共享会明确降为目标窗口预览；桌面共享不会降为窗口冒充成功，需要手动切换到目标桌面后重试。

共享目标与预览面板分别绑定：捕获持续使用任务的原应用/窗口，预览面板跟随固定的 WorkBuddy 宿主窗口。WorkBuddy 换 Space 时，即使屏幕坐标不变，也会把透明的自有预览面板迁到宿主的 Space，并回读验证后再显示；不会激活应用、切换用户桌面或把面板加入所有桌面。手动拖动或调整预览大小后，仍跟随宿主移动并保留相对位置。宿主暂时不可用或迁移无法验证时，隐藏面板并返回诊断状态，不将它留在另一个桌面冒充成功。

目标窗口跨 Space 后仍按原窗口捕获；应用共享在目标不属于当前 Space 时收窄为原窗口流，`mode` 表示请求范围，`captureMode`/`scope` 表示实际捕获范围。显式桌面共享暂不可用时保留绑定并显示等待状态，目标桌面恢复后重建原显示器上的流。有效共享租约期间，目标回到当前桌面也不会再让窗口预览自动消失。

## 固定浏览器目标与后台操作

列出窗口只返回列表，不改变已选目标。缓存目标关闭时返回 `TARGET_LOST`；没有目标且存在多个窗口时返回 `TARGET_REQUIRED`，需要显式选择。已绑定任务仍校验原窗口、进程身份与连接，不会按当前焦点或窗口排序换目标。

Chrome 页面操作优先使用已授权扩展，保持同一任务、浏览器窗口 ID 和标签 ID：先 `cua_browser_events(action="candidates", taskId=...)`，等待扩展刷新后再次查询，用返回的 `browserWindowId/tabId` 调用 `assign`，随后执行 `command(operation="snapshot")` 并查询 `result`。点击使用该快照中的 `ref`；每次动作后再读取快照确认页面变化。`nativeConnection=connected` 仅证明宿主可达，`paired=false` 表示尚未完成标签分配，不应改用重复开窗恢复。

`create_target` 只供等待窗口、尚未绑定的活动任务显式创建专用窗口，采用 `focused=false`。同一任务重复申请会返回原请求或已创建窗口；超时不会生成新请求。扩展把创建标识及结果存入会话存储，worker 重启后只恢复已知结果，结果未知则停止重放。已绑定任务返回 `TARGET_ALREADY_BOUND`，继续使用既有标签。

AX 点击后不再为了恢复前台而激活另一个应用；日志记录动作前后的前台 PID。AX 动作本身仍可能改变焦点，不能据此保证原生操作完全不干扰用户。用户同时操作电脑时应走扩展的固定标签通道；扩展未绑定、权限不足或返回窗口投递错误时报告阻塞，不通过反复切换前台或新建窗口继续。

## 操作诊断日志

界面观察和鼠标操作默认写入 JSONL 日志。普通安装位置为 `~/Library/Logs/workbuddy-cua/bridge-operations.jsonl` 与同目录的 `native-operations.jsonl`；设置 `CUA_CONFIG_DIR` 时位于该目录的 `logs/`。可以用 `CUA_DIAGNOSTICS_DIR` 指定目录，或以 `CUA_OPERATION_LOG=0` 关闭。环境变量需要传给桥接和 helper；更改后重新连接。

每次工具调用返回 `structuredContent.diagnostics`，包含 `toolTraceId` 和日志路径。桥接的 `nativeRequestId` 对应原生日志的 `requestId`，可串起请求发送、几何校验、事件创建/投递、结束或错误。点击结果中的 `pointer` 包含窗口边界、窗口内坐标与最终屏幕坐标。原生日志还会记录 CG/AX 边界差异、观察后窗口移动、显示器位置以及实际投递路线。

预览诊断另含 `hostWindowId`、`hostSpaceIds`、`panelSpaceIds`、`panelSpaceVerified`、`panelPlacementError`，用来核对 WorkBuddy 宿主与预览面板是否在同一组 Space；`trackingWindowId` 始终表示独立的捕获目标。原生日志在这些绑定状态变化时记录 `preview.hostRoute`。

坐标点击的 `x/y` 始终是**窗口左上角起算的 macOS point**。截图的 `pixelWidth/pixelHeight` 是像素，日志中的 `pixelsPerPointX/Y` 给出比例：窗口内截图像素位置应分别除以该比例，再作为点击坐标；整个桌面的截图坐标还需减去窗口原点。共享预览面板的显示尺寸也不等于点击坐标尺寸。

`pointer.event.posted` 只说明事件已投递，不证明应用收到或界面发生变化；`verified=false` 会保留。日志不自动重放输入，不记录输入文本、按键内容、页面/窗口标题、AX 正文或截图数据。原生文件写入使用独立队列，避免磁盘写入插入按下与抬起之间。每个文件最多 5 MiB，保留一份轮转文件 `.1`，新建文件权限为 `0600`。

## 注册

已写入 `~/.workbuddy/mcp.json`：

```json
"cua": {
  "command": "/usr/bin/python3",
  "args": ["/Users/gushaokui/Documents/AI/computer-use/cua-mcp-bridge/server.py"],
  "env": { "CUA_HELPER_PATH": "/Users/gushaokui/Documents/AI/computer-use/cua-helper-build/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native" }
}
```

改完配置后 MCP 不会自动生效：到连接器管理页右上角自定义连接器入口，对新服务器点一次 **Trust**。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `CUA_HELPER_PATH` | 辅助程序绝对路径，默认指向 `cua-helper-build/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native` |
| `CUA_READ_ONLY=1` | 只暴露观察类工具（permissions / apps 列表与选择 / windows / state / screenshot），禁输入 |
| `CUA_ALLOWLIST` | 逗号分隔的 bundleId 或应用名白名单，`cua_apps` 选应用时校验 |

## 工具（12 个）

| 工具 | 对应 helper 方法 | 说明 |
| --- | --- | --- |
| `cua_permissions` | `permissionStatus` + `getCapabilities` | 授权、平台、会话锁和任务隐私层状态；`action=open_settings` 打开系统设置 |
| `cua_apps` | `listApps` / `findApps` / `getApp` / `launchApp` | `action=list\|find\|get\|launch`，`get` 返回 `appHandle` |
| `cua_windows` | `listWindows` / `activate` | 窗口列表（含屏幕坐标）或请求激活应用 |
| `cua_state` | `getState` | 紧凑 AX 树 + `snapshotId`，元素级操作的入口 |
| `cua_screenshot` | `screenshot` | 返回 PNG + 像素尺寸 + 屏幕 bounds，并落盘到 `shots/` |
| `cua_click` | `click` | 元素级（`snapshotId`+`elementIndex`）或坐标级（`windowId`+`x/y`） |
| `cua_type_text` | `typeText` | 逐字符 Unicode 事件，适合短文本 |
| `cua_paste` | `paste` | 剪贴板粘贴，长文本/中文首选，会核对输入框 |
| `cua_key` | `pressKey` | `return`/`tab`/`escape`/`f5` 或单字符 + `modifiers` |
| `cua_scroll` | `scroll` | 窗口内滚动，`deltaLinesY>0` 显示上方内容 |
| `cua_drag` | `drag` | 窗口内拖拽，默认 400ms / 15 步 |
| `cua_set_value` | `setValue` | 直接写 AX value，仅对 `valueWritable` 元素有效 |

## 四个必须记住的口径

1. **坐标系**：`cua_state` 树里的 bounds 是**屏幕坐标**；`cua_click/scroll/drag` 的 `x/y` 是**窗口左上角起算的 point**（含标题栏）。换算：`窗口坐标 = 屏幕坐标 - 截图 bounds.x/y`。Retina 下 PNG 像素是 point 的 2 倍，别把像素当 point。
2. **快照失效**：每次 `getState` 会升代次，旧 `snapshotId` 再用就是 `STALE_SNAPSHOT`；元素操作必须用同一轮观察拿到的快照。
3. **不自动重试**：输入类请求失败可能已部分生效，桥接层对输入**永不重放**（只对 `listApps`/`getState`/`screenshot` 这类幂等读取重启重试一次）。
4. **前台约束**：原生路径的窗口投递错误不能证明可安全点击；需要前台时才显式请求激活。用户同时使用电脑的 Chrome 页面操作优先走已绑定扩展。

## 任务隐私保护层

`cua_task action=bind` 成功后，helper 建立任务租约及防睡眠断言，隐私状态进入 `watching`：此时不盖屏，也不启动 guardian，用户仍可触发系统锁屏。

启用下述可选 Locked use 后，交接流程为：

1. 首次观察到系统锁屏，先准备主进程覆盖层与独立 guardian 的第二层覆盖；覆盖及监听就绪后，才签发最长 12 秒、只消费一次的自动解锁票据。
2. AX 可以轮询读取登录界面，但每次自动解锁流程只提交一次非机密探针串。确认系统自动解锁后，显示 workbuddy 的绿色像素浮层；初次锁屏快捷键的修饰键松开不会被当成新的用户输入。
3. 浮层显示后，任意新按键、鼠标点击或滚轮输入都会撤销票据、取消后续 AX 动作并暂停任务，再请求真正的系统锁屏。
4. 确认系统锁定且没有已消费、尚未落地的 ALLOW 后，才撤掉两层覆盖。用户手动解锁后，同一任务仍暂停，直到显式 `cua_task action=end, confirm=true` 触发原生 `powerGuard release`。显式结束解除任务暂停并撤销任务授权，但手动认证要求和未落地 ALLOW 独立保留；认证完成后才可恢复窗口选择，再 `begin` 并显式 `bind` 重建任务。

任务结束或租约超时，若已经覆盖桌面，也先交还真实系统锁屏再释放覆盖；`watching` 阶段直接结束。已交给 authd 的 ALLOW 不能撤回，若其在接管后才实际解锁，必须再锁一次。自有浮层不替代系统安全边界，也不读取或保存真实密码。

2026-11-05 的本次变更仅涉及代码与离线测试，未安装新版或实测新的系统锁屏/解锁交接流程。状态可从 `getCapabilities.sessionPrivacy` / `powerGuard.privacy` 查看。

### 可选 Locked use

仓库内另提供实验性 macOS Authorization Plug-in：

```text
cua-auth-plugin/
```

本项目的系统接入方式如下：

- 修改 `system.login.screensaver`，保留 `use-login-window-ui`；
- 安装 `WorkBuddyCUAAuthorizationPlugin.bundle`；
- 通过 Unix socket 的内核 audit token 校验对端：插件用 SecTask 核对 helper 的签名 identifier/Team ID，helper 验证 Apple 授权宿主；
- 协议仍为 `AUTHORIZE 3` / `ALLOW 3`；只有活动任务、未被用户暂停、未处于手动认证抑制、保护租约有效且票据未过期或消费时才允许；
- 安装前保存原始 authorization right，支持完整卸载恢复。

该插件默认未安装。安装会修改系统登录策略，必须在受控账户或虚拟机中由管理员显式执行。`cua_permissions` 的状态文本会显示 Locked use 是否可用以及允许/拒绝次数。

## 已知退化路径

- **自绘界面**（游戏、Unity、canvas、部分 Electron 控件）：AX 树为空，`cua_state` 会明确提示，只能截图 + 坐标点击。
- **浏览器**：原生 helper 的 `getCapabilities.browser=false` 不包含独立扩展能力。DOM 操作需要 Chrome 扩展启用、站点授权、宿主连接和显式标签分配均就绪；否则原生 AX/坐标不能充当无干扰的后台网页控制。
- **敏感场景**：常规桌面工具不能操作安全输入框、TCC 系统弹窗或真实系统锁屏（`sessionLocked=true`）。上述可选自动解锁使用独立、受票据约束的登录界面 AX 流程，任务隐私层不存储密码。
- **截图体积**：2116×1394 约 375 KB base64，连续截图会显著吃上下文，按需再截。

## 重编代价：已改成稳定签名（2026-10-30）

`workbuddy-cua.app` 原本是 ad-hoc 签名。替换 `Contents/MacOS/workbuddy-cua-native` 或 `codesign --deep --sign -` 会让**辅助功能授权立即失效**（实测 `accessibility=False`，屏幕录制保留），必须去系统设置把 `workbuddy-cua` 关掉再打开。

现在改用本机已有的开发者证书签名，designated requirement 绑到证书而不是 cdhash：

```bash
cd "cua-helper-build"
swift build -c release --disable-sandbox
cp .build/release/workbuddy-cua-native "workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native"
codesign --force --deep --sign "Apple Development: Shaokui Gu (ATGH8CN43J)" "workbuddy-cua.app"
```

实测连续两轮「重编 + 替换 + 重签」后 `cua_permissions` 仍为 `accessibility=True`，**不再需要重新授权**。换证书或换机器仍需授权一次。

## 跨桌面预览面板

目标窗口 `isOnScreen=false`（在别的 Space / 另一块屏幕）时，helper 会弹一块 380×271 的悬浮面板（`SpacePreview.swift`，标题「跨桌面预览」），每 0.6s 抓一帧桌面对该 Space 做合成：把属于该 Space 的窗口按原坐标画在**中性灰背景**上。于是面板里没被窗口覆盖的区域会是大片纯灰——那是背景，不是没渲染。

- 目标窗口回到当前桌面时自动隐藏；30s 无操作自动关闭。
- 面板和 80×80 代理光标都是 helper 自己的窗口，**截图（`cua_screenshot`）与屏幕录制都拍不到**，只能肉眼看屏。用 `screencapture -l <windowNumber>` 才能单独拍到它们。
- 面板定位：有真正的宿主 App 时贴宿主窗口右下；**headless 启动（责任进程解析成 helper 自己）时固定贴主屏 `visibleFrame` 右下角，避开 Dock 与菜单栏**。

修过的两个坑（都会让面板「像没出现」）：

1. `track()` 原要求 `onCurrentSpace == false` 且 `spaceIDs` 非空，窗口 Space 归属为 `null` 时直接 `stop()`；现回退用 `isOnScreen` 判断。
2. `hostWindowFrame()` 原会把**面板自己**当成宿主（380×271 通过 `>300×200` 筛选），导致面板每帧以自己为锚点、每 0.6s 向左下滑 16pt，几秒就滑出屏幕。现在责任进程等于自身 pid 时直接按 headless 处理。

## 光标为什么看不到

三件不同的事，别混在一起：

1. **系统鼠标指针不会动**。helper 走 SkyLight / `CGEventPostToPid` 的**窗口定向投递**，事件直接送进目标进程，不经过硬件光标。实测点击后 `CGEvent(source:nil).location` 只反映人手动鼠标的位置，程序从未改写它。
2. **替代指示是 80×80 的代理光标浮层**（`CursorOverlay.swift` 的 `AgentCursor`，`NSPanel` + `screenSaverWindow` 层级）。实测点击后确实出现：`layer=1000 pid=<helper> owner=workbuddy-cua 80x80 alpha=1.0`。三个条件会让人以为"没有光标"：
   - **30 秒无操作自动隐藏**；
   - 只在指针类动作（click/hover/scroll/drag）时刷新位置，`cua_key`/`cua_type_text`/`cua_state` 不画；
   - 它是**独立浮层窗口**，`cua_screenshot` 走 ScreenCaptureKit 窗口捕获，**永远拍不到它**，也拍不到系统光标。
3. **跨桌面会画到看不见的地方**。浮层坐标由窗口所在的窗口服务器空间算出；目标窗口 `isOnScreen=false` 时，代理光标与点击都落在另一个 Space。桥接层现在会在结果里附带这条告警。

想在屏幕上看到光标：先 `cua_windows(action="activate")` 把窗口拉到当前桌面，再做坐标点击，并在这 30 秒内看屏幕——别去看截图。

## 自检

```bash
/usr/bin/python3 /tmp/cua_mcp_harness.py   # 本地 MCP 客户端：initialize → tools/list → 12 次调用
```

桥接日志写 stderr（`[cua-bridge]`），stdout 只走 JSON-RPC；helper 的 `[cua-screenshot]` 阶段诊断只保留在环形缓冲里，不刷屏。

## 授权归属

TCC 授权跟着 helper 的**责任进程**走。实测两种形态：

- 由 dsh 宿主拉起：`responsibleApp` 是宿主 App（如 `DeepSeek Harness.app`）；
- 由桥接层直接拉起（无宿主）：责任进程解析为 **`workbuddy-cua` 自己**，授权就记在这个 app 上。

`cua_permissions` 返回的 `launchApplication` 就是当前承载授权的那个 app；显示 `false` 时用 `action=open_settings` 打开面板给它授予辅助功能与屏幕录制。移动 `.app` 位置仍可能导致授权失效。


## 严格后台浏览器模式（扩展 0.3.0）

`cua_task begin` 默认 `interactionMode=background_browser`。原生点击、悬停、滚动、拖动、键盘、粘贴、设值和窗口激活均被桥接层拒绝；扩展断开或版本能力不足时等待原通道恢复。普通桌面应用操作可在新任务 begin 时明确指定 `interactionMode=desktop`，不能在已开始的任务内改模式。旧任务缺少模式字段时按后台模式处理。

浏览器仍需先绑定确切原生窗口，再通过 `cua_browser_events candidates/assign` 分配真实 Chrome 标签 ID。原生窗口预览还需 `verify_mapping` 确认系统窗口与浏览器窗口对应；映射未确认时只使用扩展快照和截图，不猜测新窗口并开始共享。任务归属以固定标签和文档为准，用户焦点变化不会改绑；Chrome 标签被移到其他窗口时会暂停，需移回原任务窗口或重新明确建立任务。浏览器窗口 ID 与系统窗口 ID 不可互换。

执行流程：`command(operation=snapshot)` → `result(commandId)` 取得 `snapshotId` → `command(operation=click/fill/select/scroll, snapshotId=..., ref=...)` → 查询结果 → 新 snapshot 复核。每个命令携带任务分配、tab、documentId、导航 epoch 和过期时间。旧文档、旧快照、过期命令不投递。`performed` 仅代表已分派，不能据此声称业务完成。超时、取消或错误的已投递动作可能已发生；不重放，先观察。

点击、输入和滚动通过指定标签的 CDP 通道执行，使用页面视口坐标；select 保留文档内 DOM 操作。`keypress(key=Enter/Tab/Escape/ArrowDown/...)` 仅接受有限页面按键，不接受系统或地址栏快捷键。`screenshot` 用该标签的 `Page.captureScreenshot`，不使用桌面截图，不会复核业务动作。DOM 引用覆盖顶层文档及可访问的同源 iframe；跨域 iframe、浏览器工具栏和原生系统对话框需要另外的交互流程，不能回落桌面点击。

扩展弹窗可暂停当前任务的浏览器控制、查看会话内活动记录、截取并批注当前任务页面。暂停不会撤销已发出的动作，恢复需重新分配目标。下载列表只显示调试协议报告的任务标签候选事件，不证明文件最终归属或保存位置；部分浏览器或下载方式可能不产生事件。活动、截图和批注保存在浏览器会话存储中，随会话结束清除。主动快照会包含可交互元素名称，截图可能包含页面内容，诊断可能包含页面错误文本。

受控的 `window.open` 和新窗口链接被约束到原任务窗口，以 `active=false` 创建子标签，结果中回传 `children`。新窗口和子标签先创建为空白页，安装弹窗约束后再导航；创建中途失败时清理本次已创建的子标签，避免遗留未登记目标。`tabs` 查看归属，`select_tab(tabId=...)` 显式切换任务的逻辑目标，保留根标签与任务窗口，不激活实际标签；切换后先等待新文档并取 snapshot。新窗口 POST 表单、依赖 WindowProxy 返回值的弹窗和部分异步导航弹窗不支持自动转送；不得把动作分派当作成功。任务清理时尝试恢复扩展安装的弹窗钩子并释放调试连接；站点权限已撤销时，页面钩子可能要重载页面才清除。此约束不构成对任意网站脚本或浏览器原生界面的全局焦点保证。

预览面板与捕获源分别绑定：捕获源保持任务窗口，预览面板跟随 WorkBuddy 的窗口、显示器和 Space。原生预览不可用会记录 `browser.preview_waiting`，后台输入不等待原生视频首帧。原生面板展示窗口视频；如果人在该窗口选了其他标签，视频会显示那个标签，不能用它证明后台逻辑标签的页面状态。确认任务页面使用扩展的 `snapshot/screenshot`。日志记录命令、标签、文档、导航版本、选路和延迟，不记录输入文本、截图数据或网页正文。

升级需重新加载扩展 0.3.0，并在 Chrome 提示时由本人确认新增的 debugger 权限。该权限用于固定标签的浏览器调试协议；缺权限时不尝试其他输入通道。重新连接 WorkBuddy MCP 后开始新任务。当前测试为隔离的自动化回归；实际 Chrome 焦点、不同 macOS Space 和特殊站点弹窗仍须实机验证。
