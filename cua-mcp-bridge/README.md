# cua-mcp-bridge

把 `workbuddy-cua-native`（macOS 原生 computer-use 辅助程序）包装成 MCP stdio 服务，
让 WorkBuddy 能直接调用桌面操作能力。

```
WorkBuddy ⇄ MCP(stdio/JSON-RPC) ⇄ server.py ⇄ workbuddy-cua-native ⇄ 共享守护进程 ⇄ AX / SkyLight / ScreenCaptureKit
```

辅助程序本身只讲私有 JSON-lines 协议（`protocol.schema.json`），WorkBuddy 只认 MCP，
这一层就是两者之间的单向翻译 + 体积裁剪，Swift 侧零改动。

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
4. **前台约束**：被遮挡或非前台窗口会返回 `FOREGROUND_REQUIRED`，先 `cua_windows(action="activate")`。

## 任务隐私保护层

`cua_task action=bind` 成功后，helper 在整个任务租约期间：

- 按住 `PreventUserIdleSystemSleep` 和 `PreventUserIdleDisplaySleep`，阻止空闲睡眠及由显示器睡眠触发的自动锁屏；
- 在所有显示器和所有 Space 的最上层显示 workbuddy 自己的绿色像素保护层；
- 拦截保护层上的按键、点击、滚动和触控板手势，并请求 macOS 显示真实登录锁屏；
- 若合盖、触控 ID 或其它系统路径先锁了电脑，不尝试读取密码或自动解锁；等用户完成系统认证后恢复保护层。

覆盖层只约束用户输入，不替代系统安全边界。macOS 不允许普通应用无条件否决合盖、触控 ID、系统故障或其它受保护锁定路径，这些限制会在 `sessionPrivacy.limits` 中如实返回。任务结束、租约超时或 helper 退出时会撤掉覆盖层并释放两项电源断言。

### 可选 Locked use

仓库内另提供实验性 macOS Authorization Plug-in：

```text
cua-auth-plugin/
```

它使用与官方 Codex Locked use 相同的系统接入点：

- 修改 `system.login.screensaver`，保留 `use-login-window-ui`；
- 安装 `WorkBuddyCUAAuthorizationPlugin.bundle`；
- 通过同用户 Unix socket 校验插件与 helper 的代码签名 identifier/Team ID；
- 只有活动任务、未手动锁屏抑制且票据未消费时才允许；
- 安装前保存原始 authorization right，支持完整卸载恢复。

该插件默认未安装。安装会修改系统登录策略，必须在受控账户或虚拟机中由管理员显式执行。`cua_permissions` 的状态文本会显示 Locked use 是否可用以及允许/拒绝次数。

## 已知退化路径

- **自绘界面**（游戏、Unity、canvas、部分 Electron 控件）：AX 树为空，`cua_state` 会明确提示，只能截图 + 坐标点击。
- **浏览器**：`getCapabilities.browser=false`，没有 DOM/JS 通道，网页操作只能走 AX/坐标，比 Playwright 脆弱。
- **敏感场景**：安全输入框、TCC 系统弹窗、系统真实锁屏（`sessionLocked=true`）不可操作；任务隐私层本身不存储密码。
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
