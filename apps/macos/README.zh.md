---
description: "macOS 原生辅助程序的请求协议、输入语义、共享服务和验证命令。"
kind: "package-reference"
---

# QuietBridge 原生辅助程序

[English](README.md) | 中文

## 摘要

该 JSON-lines 辅助程序支持 macOS 14 及以上，使用 Swift 6、AppKit、辅助功能、CoreGraphics 和 ScreenCaptureKit。Windows 支持正在开发中。程序改写了 MIT 许可的 Cua Driver 部分行为，不加载外部驱动。`upstream.json` 记录固定的上游版本、改写的 Swift 文件和本地模块；`LICENSE.quietbridge-driver` 保留 MIT 声明。

## 目录

- [构建与服务](#build-and-service)
- [请求协议](#request-protocol)
- [观察与输入](#observation-and-input)
- [验证](#verification)

-----

<a id="build-and-service"></a>
## 构建与服务

使用 `swift build --package-path apps/macos --configuration release` 构建。二进制位于本目录下的 `.build/release/quietbridge-native`。Host 将 `--ensure-shared` 作为受管启动命令运行，服务进程随后归属于已连接的客户端，最后一条连接完成清理后退出。`.build/` 不提交到仓库。

共享服务在 `/tmp/quietbridge-<uid>` 下使用仅所有者可访问的 Unix socket，以可执行文件的 SHA-256 区分实例。版本锁选出一个进程，另一个真实可执行路径锁排除同一路径的不同构建。首帧为协议版本 1 的 `hello`，包含运行时标识以及该连接的字节、AX、操作、截图、事件间隔和清理限制。`ready` 返回已核实的运行时标识、PID 和实例 ID。每条连接拥有独立的 AXWorker。stdin/stdout 入口是该服务的诊断代理。

使用前先打开 `QuietBridge.app`。菜单应用持有独占内核锁和每次启动生成的会话 ID；执行器必须依赖同一活跃会话。右下角“退出应用”会撤销会话、取消原生操作、停止预览、释放防睡眠租约并结束执行器。应用崩溃同样释放锁，遗留文件不能启用能力；重新打开应用也不能恢复旧执行器会话。桥接与浏览器扩展同步执行这些规则，应用未运行时连接器不会自行启动执行器。

首次启动会在桌面显示独立的四步向导：能力介绍、辅助功能、屏幕录制、完成。界面按照 Figma 使用品牌侧栏和适配浅色、深色的原生内容区，步骤进度与完成状态以实际授权为准。向导先解释用途和申请理由，只有点击“前往授权”才请求权限并打开对应的“隐私与安全性”页面；启动和定时检测只做预检。可以关闭窗口或稍后设置，后续启动不重复弹出欢迎页。“系统权限”右侧分级面板可查看状态和重新进入向导。

权限由实际执行桌面操作的已签名 helper 检测和申请，向导显示系统报告的责任应用名称。菜单进程通过保留的连接复用相同执行器，不用自身的权限替代 helper 的权限。辅助功能与屏幕录制均可用时才能启用原生观察、屏幕操作和锁屏使用；预览需要屏幕录制。权限缺失或无法检测时，相关开关及接口有效值均关闭，开关本身仍可点击以进入缺少权限的说明步骤。授权后保留手动关闭的选择。浏览器权限由扩展单独申请。授权状态每三秒及应用重新激活时刷新；屏幕录制如要求重开应用，应按系统提示重开后再检测。

界面任务 `bind` / `powerGuard hold` 时，helper 建立任务与防睡眠租约，隐私状态进入 `watching`；此时不盖屏，也不启动 guardian。`PreventUserIdleSystemSleep` 阻止系统空闲睡眠，显示器仍可空闲休眠，用户仍可触发真正的系统锁屏；显示器防睡眠断言在准备覆盖层时才建立。

启用 Locked use 后，首次观察到系统锁屏时 helper 才准备自有覆盖层和独立 guardian 的第二层覆盖，等覆盖与监听就绪后，通过可选 Authorization Plug-in 签发最长 12 秒、只消费一次的解锁票据。AX 可轮询读取登录界面，但每次自动解锁流程只提交一次非机密探针串，不读取、保存或输入真实系统密码。确认自动解锁后，自有浮层显示在桌面上并接受用户接管；首次锁屏快捷键的修饰键松开不算新的输入。未安装插件或保护条件不满足时，不签发自动解锁票据。插件通信仍使用协议 3。

浮层显示后，任何新按键、鼠标点击或滚轮输入都会撤销自动解锁票据、取消后续登录界面 AX 动作，再请求真实系统锁屏；这只表示用户选择手动解锁，不暂停或结束业务任务。只有确认系统已锁定，且没有已消费但尚未落地的 ALLOW，才撤掉两层覆盖。已发出的 ALLOW 若随后解锁，仍须再次锁屏才能完成交接。手动解锁完成后，同一任务和绑定继续可用，工具先重新观察目标及共享状态，禁止重放结果未知的输入。任务结束或租约到期仍由任务生命周期处理；锁屏授权不承担任务管控。

`getCapabilities.sessionPrivacy` 与 `powerGuard.privacy` 返回实时 `systemLocked`、`waitingForManualUnlock`、覆盖层和最近失败；授权状态包含 `manualSuppression`、`automaticUnlockPending` 及 `lastUnlockMethod=automatic/manual/null`。手动认证闩锁只影响自动解锁授权；普通工具只检查系统是否真实解锁及自身权限、目标和租约。解锁监测不依赖授权 socket 成功，也不随覆盖层交还而停止。安全交还锁屏后会撤销这一任务持有的自动解锁票据，但业务任务由独立资源租约管理并保持绑定。此前的本机历史验证见授权插件记录；本次代码及离线回归通过，真实锁屏交接仍需安装后验收。自有浮层不替代 macOS 安全边界，不能据此保证系统级原子交接。

浮层背景优先复用当前屏幕的系统壁纸：任务开始时仅截取系统壁纸窗口，不截取桌面合成画面、图标或应用窗口；主浮层与独立 Guardian 共用按显示器 ID 保存的临时图片。任务结束并完成交接后清理缓存。静态图按比例铺满；活动壁纸配置直接指向本地视频或可匹配已下载航拍资源时，静音循环播放该视频，隐藏时暂停。实时绘制的 Macintosh 等壁纸使用任务开始时的静态快照，不复制系统渲染引擎，也不保证动画时间轴同步。读取失败时保留原有绿色背景。2026-11-05 已实测双屏纯壁纸快照和静态叠加预览；视频壁纸分支尚待在用户选中对应壁纸后实测。

AppKit 在主事件循环上运行。阻塞式 socket 接收与握手使用后台队列，服务回调明确声明 `@MainActor @Sendable` 隔离，并弱引用服务对象。关闭时会释放已接收的连接，包括尚未完成握手的连接。阻塞式 AX 和输入使用每条连接的串行 actor，所有连接共享用于完整桌面操作的 FIFO。socket 写出不占用操作槽位。请求取消会等待原生工作停止，不取消连接的生命期控制器。截图超时只结束该请求。未完成的系统捕获保留唯一捕获槽位，直至真实回调到达；期间新截图返回 CAPTURE_BUSY，AX 和输入仍可用。只有无法停止的操作或清理才可能使共享进程失效。代理接受 `--max-request-bytes`、`--max-response-bytes`、`--ax-timeout-ms`、`--timeout-ms`、`--screenshot-timeout-ms`、`--shutdown-grace-ms` 和 `--text-event-interval-ms`；Host 在 hello 中设置相同限制。AX 超时约束单次系统调用。取消可能发生在部分输入之后，结果不确定的输入不自动重放。

任务资源现在归属持有它们的 socket：断开立即取消本连接的原生工作并释放其共享与保护。另有独立的 120 秒实际操作租约，普通状态查询不续期；过期 socket 返回 TASK_INTERRUPTED，不能恢复旧输入。其他连接不能续期或释放该拥有者的资源。保护释放沿用既有系统锁屏交接规则；已投递的输入不回滚。


-----

<a id="request-protocol"></a>
## 请求协议

操作帧为 `{ "id": "opaque request id", "method": "method name", "params": {} }`。对应回复只包含 `result` 或 `error: {code, message}` 中的一项；无效信封返回 `INVALID_REQUEST`，ID 为 null。每条连接同时最多发送一个操作。`{ "type": "cancel", "id": "request id" }` 取消该请求，迟到的取消不会产生额外回复。`protocol.schema.json` 定义操作字段。

截图诊断通过原始连接发送 `type: "diagnostic"` 帧。记录包含 requestId、stage、从接收开始计算的 elapsedMs 和可选的 errorCode，不含窗口标题、AX 文本或图片字节。stdin/stdout 代理在 stderr 上以 `[quietbridge-screenshot]` 前缀转发诊断，操作回复仍写入 stdout。Host 保留有上限的诊断缓冲区，并将其附在截图失败中。

成功的 `activate` 和 `performSecondaryAction` AXRaise 结果在 `{dispatched: true, verified: false}` 之外包含 `foreground`，记录 `requestId`、`operation`、`targetPid`、可空的 `targetWindowId`、`activationRequested`、`raiseRequested` 和即时的 `before`/`after` 观察。`frontPid` 来自 WindowServer，`focusedWindowId` 为目标应用的 AX 焦点窗口，`frontWindowId` 和 `frontWindowPid` 标识最前面的可见普通窗口。未知值为 null。观察依次取得，不具备原子性，也不证明因果关系。工具发起的请求使用日志中的工具调用 ID。失败操作保留原有错误响应，可能已产生部分效果。

| 方法 | 结果 |
| --- | --- |
| `getCapabilities` | `{platform: "macos", permissions: {accessibility, screenRecording}, sessionLocked, methods, coordinateSpace: "window-points", browser: false}`。权限值是布尔型预检结果，不打开系统提示。 |
| `listApps` | 运行中的普通应用数组，元素为 `{pid, name, bundleId}`。 |
| `findApps` | 从标准应用目录或绝对应用路径返回已安装匹配项 `{id, displayName, path, isRunning, pid?}`；发现过程不启动应用。 |
| `getApp` | 按精确名称、bundle ID、十进制 PID 或应用路径选择，返回 `{appHandle, pid, name, bundleId, authorization}`。未运行的选择具有 `pid: null`，等待 Host 授权。名称歧义会失败。 |
| `launchApp` | 授权后的 Host 专用续接操作；不请求激活，等待应用启动完成后将句柄绑定到已核实的进程身份。 |
| `listWindows` | 应用所属普通窗口，包含 `{windowId, pid, appName, title, bounds, isOnScreen, onCurrentSpace}`。Space 归属可为 null。 |
| `getState` | `{appHandle, pid, windowId, focusedWindowId, snapshotId, tree, elements, windows, truncated}`。应用级观察的 `windowId` 为 null。元素包含 index、role、title、value、description、identifier、bounds、actions 和 enabled 状态。元素边界为 null 或屏幕 point 单位的 [x, y, width, height]，窗口输入使用局部 point。这些结构化元素诊断供原生检查保留，模型工具投影会移除它们。不可用的可选描述属性为 null，无法取得的动作列表为空；权限、通信和窗口发现失败仍会拒绝观察。 |
| `hover`, `click`, `setValue`, `selectText`, `typeText`, `paste`, `pressKey`, `scroll`, `drag` | `{dispatched: true, verified: false}` 记录原生分派。需要读取新状态以核实预期的应用变化。 |
| `screenshot` | `{mimeType: "image/png", data, pixelWidth, pixelHeight, bounds, coordinateSpace: "window-points", windowId}`。Retina 显示器上 PNG 像素尺寸可能不同于窗口 point 尺寸。 |

每个面向应用的操作都要求使用同一 helper 的 `getApp` 返回的 `appHandle`。helper 固定 PID 和内核进程启动时间，进程退出或 PID 被复用后使句柄及其全部快照失效。再次调用 `getApp` 可取得替代进程，不会重新激活旧句柄。随机的 helper 命名空间防止已终止 helper 的句柄与替代实例重合。重复获取同一存活进程会共享句柄。启动后检查失败会使待启动句柄失效，并保留原错误；已经启动的进程不会被终止，launchApp 也不能复用失败的句柄。

稳定错误码包括 `INVALID_REQUEST`（信封无效）、`INVALID_ARGUMENT`（参数无效）、`UNSUPPORTED`（操作未知）、`APP_UNAVAILABLE`、`APP_PATH_UNAVAILABLE`、`PROCESS_INSPECTION_FAILED`、`AMBIGUOUS_APP`、`STALE_APP`、`STALE_SNAPSHOT`、`WINDOW_UNAVAILABLE`、`WINDOW_CHANGED`、`PERMISSION_DENIED`、`FOREGROUND_REQUIRED`（悬停目标不是未被遮挡的前台窗口）、`PANEL_NOT_READY`（输入前系统面板接收方尚不可用）及 `NATIVE_ERROR`（其余操作系统失败）。错误不保证已分派的部分输入被回滚，Host 不得自动重试输入。

应用选择向 Host 返回授权记录：区分大小写的 Bundle ID 加上 `macos:bundle-id:` 前缀后计算的 SHA-256 摘要、显示名称、可选的可执行路径和可持久化标记。相同标识的所有安装副本共用授权，包括未签名和临时签名的应用。缺少非空 Bundle ID 时，运行中和未启动应用都对 `macos:executable-path:` 与解析符号链接后的可执行路径计算 SHA-256，仍可保存长期授权。进程路径查询失败时，运行中的有效 Bundle ID 仍可用于授权，记录省略 executablePath；路径可取得时仍须与所选目标一致。两种身份均不可用时，在执行目标操作前返回 APP_PATH_UNAVAILABLE，保留路径查询原因和 errno。PROCESS_INSPECTION_FAILED 表示无法验证进程实例，只有进程检查返回 ESRCH 时才报告进程不存在；两种失败都不触发自动重试。进程号和启动时间只校验存活句柄，不进入已保存身份。Host 从模型结果中移除此记录，并在启动、观察和输入前取得用户许可。此管道协议用于可信 Host 传输，无法隔离另一个已获授权的 shell 进程。

当前仓库使用已纳入版本控制的 `protocol.schema.json` 与 `Sources/QuietBridgeNative/NativeProtocol.generated.swift` 声明原生协议，Swift 测试核验字段和枚举处理。上游 TypeScript 生成器不在本仓库中。模型参数不包含 Host 资源上限和私有启动续接操作。


-----

<a id="observation-and-input"></a>
## 观察与输入

`getState` 可将 AX 遍历限定到应用所属窗口或嵌套 sheet 的 `windowId`。新的刷新或失败的刷新使同一应用/窗口范围的旧快照失效，其他窗口范围仍可使用。应用所属 CG 窗口没有匹配 AX 界面时，getState 返回空 tree/elements、truncated 为 false 的新快照，仍可截图和坐标输入。遍历后重新检查进程生命期、窗口归属、会话未锁定及辅助功能权限。窗口发现中的 APIDisabled 和通信错误仍会失败；不支持或缺失的可选 AX 属性可以产生空观察。不存在或属于其他应用的窗口不会返回空成功结果。遍历中 AX 元素失效会以 `STALE_SNAPSHOT` 拒绝整次读取，不发布部分树。应用变化后可以重新观察，但不重复输入。元素动作要求精确的快照 ID，原生引用不跨 JSON 传递。坐标动作的可选快照 ID 必须属于该窗口或应用级观察。刷新窗口列表会释放已关闭窗口的快照。进程身份、快照代次和窗口归属检查不会锁住桌面，用户或应用仍可同时改变状态。

未指定 holdMs 的元素点击优先使用声明的 AX 动作，否则通过鼠标事件发送。坐标点击、滚动和拖动核实窗口归属和边界后，使用后台 PID/窗口定向输入。事件优先使用 SkyLight，不可用时使用公开的 PID 定向 API；已发送的事件不通过另一 API 重发。坐标是整个窗口左上角起算的 macOS point，包含标题栏。`deltaLinesY` 为正时显示上方内容，为负时显示下方内容；`deltaLinesX` 为正时显示左侧内容，为负时显示右侧内容。每个轴接受 -10 到 10 的整数，至少一个轴非零。helper 直接发送按行计量的滚轮事件。点击和拖动支持左、右、中键及 command/control/option/shift/fn 修饰键，只有左键支持双击。带修饰键的元素点击使用鼠标输入。拖动在一个窗口中完成一次按下、移动、抬起，调用方显式提供 durationMs（1..5000）和 steps（1..240）。移动延迟按小数精度分配到各段，按下和抬起的固定等待及调度延迟另计。不存在隐式改用全局桌面输入的后备路径。

`click.holdMs` 是可选的每次按住时长，单位为毫秒，接受非负整数；0 表示不额外等待。每个连接通过 `maxClickHoldMs` 限制显式按住时长。诊断 helper 的 `--max-click-hold-ms` 选项默认 5000。指定后使用元素中心或给定坐标发送鼠标事件，跳过 AX 动作。省略时保留普通点击时长。整个手势占用请求调度槽位；取消、超时和断开连接会中断等待，在释放槽位前发送对应的松开事件。已接收的事件仍然生效，进程被强制终止时无法保证清理。

截图和鼠标输入在精确窗口的 AX 边界查询前后读取 CG 边界，查询期间移动会返回 WINDOW_CHANGED。有效的 AX/CG 边界向外取整后必须一致；已知不一致会在移动、预备事件或按键发送前返回 WINDOW_CHANGED。helper 保留两个坐标来源，不用 AX 几何替换 CG 几何。缺失或无效的 AX 几何仍表示未知，AX 通信、权限和无效元素错误继续传递。无辅助功能权限的截图保留 CG/SCK 检查。声明的 AX 动作不经过鼠标预检，AX 动作失败也不改用鼠标输入。这些读取不会锁住窗口以阻止后续移动，几何检查不激活或抬起窗口。

`typeText` 和 `pressKey` 面向所选进程当前获得焦点的控件，不指定窗口。组合键先向 PID 发送修饰键变化，再发送主键，并逆序释放修饰键，不使用全局 HID 队列。后台鼠标操作可能改变键盘焦点。KP_0 和 KP_Enter 等数字键盘名称保留物理键码。文本逐个 Unicode 字符输入，不使用剪贴板。LF、CR 和 CRLF 发送一次 Return，制表符发送 Tab。应用保留缩进和焦点导航等正常编辑行为。每次按下和抬起后等待配置的文本事件间隔，分派完成不证明应用已消费全部事件。取消可能中断部分文本或手势，无法撤销已接收事件。很长的文本可能超过 Host 操作期限。继续输入前应核实应用状态。

负责执行的 host/helper 需要 macOS 辅助功能权限以读取 AX 和输入，需要屏幕录制权限以截图。能力预检和权限错误不授予权限。截图会拒绝在读取图片期间移动或缩放的窗口。无法确认归属的系统面板及仅后台辅助类型的应用不可选择。

仅供 Host 使用的权限操作为 `permissionStatus {}`、`requestPermission {permission}` 和 `openPermissionSettings {permission}`，其中 permission 为 `accessibility` 或 `screenRecording`。这些操作不出现在模型工具及 `getCapabilities.methods` 中。权限状态包含观察到的 helper 和父进程路径，这些路径不能确定 TCC 归属的应用。系统授权提示负责指出应用并询问用户决定。helper 使用可选的 libSystem 内部接口 `responsibility_get_pid_responsible_for_pid` 查询责任进程，在报告就绪前记录对应应用的名称和图标。接口通过动态加载解析，函数签名与 [Chromium](https://chromium.googlesource.com/chromium/src/+/refs/tags/134.0.6998.28/base/process/process_info_mac.mm) 使用的一致。符号、进程或应用信息不可用时，归属保持未知，不根据最近的 GUI 祖先推断。权限检查独立于这些信息。

观察通过 `AXUIElementCopyMultipleAttributeValues` 读取每个非布局节点的描述属性和子节点。逐属性错误保留单独读取的规则；不支持批量 API 时使用单独读取。值只在本次观察期间保留，输入读取即时属性。应用状态可能在节点之间变化，批量读取不使整棵树具备原子性。

系统文件面板的键盘路由通过所选应用的 AX 焦点 sheet 解析远端接收方。只接受 Apple 的 Open and Save Panel Service，且其焦点祖先链仍须包含已识别的远端窗口。AXWindow 不可用时，元素鼠标后备路径沿 AX 父节点解析应用所属窗口或 sheet。普通点击保持 AX 动作优先，可视点击标记不需要系统鼠标移动。

请求中的修饰键数组接受 command/control/option/shift/fn。对象 API 在分派前统一组合键别名。

`selectText` 要求新近元素快照和可写 AXSelectedTextRange。它匹配唯一的字面文本，可用紧邻的 prefix/suffix 缩小范围，然后选中文本或将光标放在前后。匹配和返回范围采用 UTF-16 偏移。歧义或缺失文本会失败，helper 不改写内容，并重新核实值和选区。AX 文本包含可用的可写性、禁用状态、选中状态和选区信息；AXAccess 持有索引和原生引用，AXTextFormatter 渲染已观察的值。

`paste` 提供一种纯文本或 HTML 剪贴板格式，调用应用已启用的标准 Paste 菜单命令，并等待剪贴板数据请求。随后仅在所有权标记和修改计数仍匹配时恢复原有格式。Host 通过文件锁串行化不同 helper 的粘贴。maxClipboardBytes 同时限制输入和保留内容，不支持的菜单和过大的数据在分派前失败。后续复制会被保留。取消会尽可能恢复仍持有的剪贴板数据，强制结束不保证恢复，数据请求也不证明目标已完成编辑。 分派完成后，`pasteReadTimeoutMs` 限制等待剪贴板读取的时间。到期返回 `PASTE_NOT_CONSUMED`，关闭延迟数据提供方，并仅在仍持有剪贴板时恢复原内容。该失败不能证明应用以后不会处理已排队的 Paste 命令；决定是否重试前先检查目标内容。

`hover` 接受已观察的元素索引或窗口内坐标，不按下鼠标按钮，也可用于禁用控件。调用结束、AX 读取和截图都不恢复原位置，后续 hover 调用可进入新出现的内容。点击只在目标位置发送按键事件，不先在窗口外点击。单次请求串行执行，不在调用之间保留应用。其他输入或应用变化可能中断交互。

`hover` 优先查找同一窗口内声明了 `AXShowAlternateUI` 和 `AXShowDefaultUI` 的元素或祖先。进入展开区域的子控件时保留父控件界面，离开时恢复一次。AX 动作失败直接返回错误，不尝试其他输入路径。没有可用 AX 路径时，`hover` 核实前台 PID，并通过 AppKit 查询目标点实际接收鼠标输入的窗口后，使用系统输入队列移动可见鼠标。不接收鼠标输入的窗口和透明区域不会阻挡目标。检查失败返回 `FOREGROUND_REQUIRED`，不移动鼠标。结果中的 `hoverRoute` 标明 `accessibility` 或 `system-pointer`，`systemPointerMoved` 标明是否移动系统鼠标。悬停不激活、不抬起窗口，也不恢复焦点。其他输入或窗口变化可能与检查和分派交错。


-----

<a id="verification"></a>
## 验证

从仓库根目录运行 `python3 apps/macos/tests/test_swift.py`。测试使用独立夹具，
覆盖协议、资源所有权、停止清理、后台输入路由、预览状态及面板布局。
它们不替代签名安装、系统权限归属及真实宿主的集成验收。

`apps/macos/tests/test_permission_service.py` 为显式启用的原生服务生命周期
检查，使用自己的应用锁和临时 Unix socket，不请求系统权限。启用前查看该文件
规定的环境变量。

整体架构及并行运行时要求见[总体方案](../../docs/architecture/quietbridge.zh.md)。
