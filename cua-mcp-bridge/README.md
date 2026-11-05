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

## 固定浏览器目标与后台操作

列出窗口只返回列表，不改变已选目标。缓存目标关闭时返回 `TARGET_LOST`；没有目标且存在多个窗口时返回 `TARGET_REQUIRED`，需要显式选择。已绑定任务仍校验原窗口、进程身份与连接，不会按当前焦点或窗口排序换目标。

Chrome 页面操作优先使用已授权扩展，保持同一任务、浏览器窗口 ID 和标签 ID：先 `cua_browser_events(action="candidates", taskId=...)`，等待扩展刷新后再次查询，用返回的 `browserWindowId/tabId` 调用 `assign`，随后执行 `command(operation="snapshot")` 并查询 `result`。点击使用该快照中的 `ref`；每次动作后再读取快照确认页面变化。`nativeConnection=connected` 仅证明宿主可达，`paired=false` 表示尚未完成标签分配，不应改用重复开窗恢复。

`create_target` 只供等待窗口、尚未绑定的活动任务显式创建专用窗口，采用 `focused=false`。同一任务重复申请会返回原请求或已创建窗口；超时不会生成新请求。扩展把创建标识及结果存入会话存储，worker 重启后只恢复已知结果，结果未知则停止重放。已绑定任务返回 `TARGET_ALREADY_BOUND`，继续使用既有标签。

AX 点击后不再为了恢复前台而激活另一个应用；日志记录动作前后的前台 PID。AX 动作本身仍可能改变焦点，不能据此保证原生操作完全不干扰用户。用户同时操作电脑时应走扩展的固定标签通道；扩展未绑定、权限不足或返回窗口投递错误时报告阻塞，不通过反复切换前台或新建窗口继续。

## 操作诊断日志

界面观察和鼠标操作默认写入 JSONL 日志。普通安装位置为 `~/Library/Logs/workbuddy-cua/bridge-operations.jsonl` 与同目录的 `native-operations.jsonl`；设置 `CUA_CONFIG_DIR` 时位于该目录的 `logs/`。可以用 `CUA_DIAGNOSTICS_DIR` 指定目录，或以 `CUA_OPERATION_LOG=0` 关闭。环境变量需要传给桥接和 helper；更改后重新连接。

每次工具调用返回 `structuredContent.diagnostics`，包含 `toolTraceId` 和日志路径。桥接的 `nativeRequestId` 对应原生日志的 `requestId`，可串起请求发送、几何校验、事件创建/投递、结束或错误。点击结果中的 `pointer` 包含窗口边界、窗口内坐标与最终屏幕坐标。原生日志还会记录 CG/AX 边界差异、观察后窗口移动、显示器位置以及实际投递路线。

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
