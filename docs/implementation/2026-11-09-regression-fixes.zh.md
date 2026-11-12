# 2026-11-09 综合测试问题处理

基线：QuietBridge 0.6.5（88），扩展 0.4.1。后续修订已打包为 QuietBridge 0.6.6（89），包含扩展 0.4.2；未在本机安装。

## 运行证据

- bridge 日志中 quietbridge_state 两次返回 BRIDGE_ERROR，toolTraceId 分别为
  b1588228-ef40-4e0d-8927-a9cfa6d86ecf、85dea368-38b9-4111-ba23-84756336e124。
- 对应源码的空 AX 树分支引用了未定义的 by_id。非空树不会进入该分支；修改 maxDepth/details 不会解决。
- 浏览器会话的保留回执中，click、navigate、fill 多次返回
  Cannot access a chrome-extension:// URL of different extension。
  相邻 snapshot 回执仍成功，因此不能据此判定整个浏览器通道失效。
  例：navigate 请求 da33b8d123e5f724fbb148c64c321f15、fill 请求 72eab6fabbe58841edf59c8d52d1a43c。
- 原日志未记录失败的具体 CDP 方法，不能据此认定是哪一个外部扩展注入了页面。

## 已修改的代码

1. AX 空树：从同一应用的窗口列表读取已观察窗口的元数据；仅显式 isOnScreen=false 才提示屏下状态。
   缺少可见性字段不再被当成 false；不激活窗口、不截图兜底。
2. situation：普通 content 文本与 structuredContent 同时提供窗口、权限、焦点、有界 AX、共享状态和建议。
   AX 最多 50 元素、6 层、24000 字符。preview 仍只查询 status，不自行开始共享或截图。
3. candidates：无可用缓存时释放存储锁，最多等 3 秒接收扩展元数据。
   有新结果即可在同一次调用返回；真实空列表与尚待刷新分开报告，后者带 retryAfterMs。
   等待可取消、核对原任务，不创建窗口、不重放导航或输入。
4. 后台标签窗口核验：扩展只读查询目标 browserWindowId 的活动标签标题，并携带 bindingId。
   服务端只接受当前绑定、同一浏览器窗口、20 秒内的证据，再与原生窗口标题唯一匹配。
   不选择标签、不激活窗口；标题相同的多个窗口仍拒绝核验。
5. 映射结果明确区分“窗口归属正确”和“任务标签可见”。窗口共享显示活动标签；后台任务内容用其 snapshot/screenshot 复核。
6. 调试受限：捕获 Chrome 的跨扩展拒绝，返回 BROWSER_DEBUGGER_RESTRICTED 和失败阶段
   （attach、Page.enable、Page.addScriptToEvaluateOnNewDocument 或 Input.*）。
   异步 result 将命令错误码传到 MCP 恢复指引和诊断日志；代理须通知用户并暂停依赖步骤。

## Chrome 限制及剩余问题

Chromium 官方 debugger_api.cc 的 ExtensionMayAttachToRenderFrameHost 会验证目标的子框架。
DebuggerSendCommandFunction 通过 InitClientHost/InitAgentHost 再次执行目标权限检查。
目标含其他扩展源页面时，Chrome 可能拒绝原先已连接的 debugger 命令；HTTP 顶层页面可正常快照并不证明 CDP 仍有权限。

源码核对地址：
https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/browser/extensions/api/debugger/debugger_api.cc

本轮修订识别并解释该阻塞，不声称已消除外部扩展冲突或恢复该现场下的点击。
不得为此移除页面框架、停用用户其他扩展、扩大系统输入权限、激活浏览器、改用原生键鼠或自动重复输入。
下一轮需读取新增错误阶段，用户确认后检查相应扩展注入，或明确选择仅加载 QuietBridge 的独立受管配置进行对照测试。
原任务、标签和其他会话不能被自动替换或结束。

## 验收要求

源码自动验证已完成：MCP Python 290 项、浏览器扩展 Node 61 项、macOS 打包 34 项全部通过；git diff --check 通过。
测试使用隔离 fixture，不代表已在用户当前浏览器中消除跨扩展冲突。未安装新版、未重启用户应用、未修改权限或其他扩展。

打包验收：dist/quietbridge-0.6.6.pkg（2.50 MiB）。实际展开包中的 App、Helper、授权组件签名、所供证书与 profiles 均匹配；桥接源码、扩展 0.4.2 和扩展 ZIP 一致。
独立 HOME 下安装预检、MCP initialize/tools/list（19 工具）及打包原生权限服务烟测通过。临时钥匙串已删除，搜索列表已恢复。
PKG 容器未签名、未公证。详细记录：dist/quietbridge-0.6.6-verification.json。

- 空 AX 树返回正常观察和可见性说明；situation 在仅展示 content 的客户端里也能读到实际 AX/preview 状态。
- 首次候选在 3 秒内回传时无需代理再次调用；超过时诚实返回 pending，而非无标签或权限失败。
- assign 非活动标签后，唯一活动标签标题可核实其所属窗口；同时回报 targetTabActiveAtVerification=false。
- 跨扩展阻塞返回明确阶段、recovery.status=user_action_required；不换工具、不重放，不误报网页操作成功。
- 浏览器原有 CDP 输入、文档绑定、弹窗约束与三会话隔离测试继续通过。
- browser_session 受管启动本轮未做实际界面测试，不将未测试项目计为失败或通过。
