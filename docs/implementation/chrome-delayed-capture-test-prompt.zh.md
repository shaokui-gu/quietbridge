# Chrome 其他桌面：导航后 5 秒单次截图实验

> 历史实验，已停止使用。2026-11-12 的核心能力收敛已从源码移除导航后自动截图、截图促帧及相应状态字段。下面仅保留原实验记录，不再执行。后续安装包含收敛改动的构建后，改用[核心操作验收提示词](core-control-acceptance-prompt.zh.md)。

需要安装 QuietBridge 0.6.25（108），并刷新 Chrome 扩展至 0.4.13；只更新扩展不足以获得原生 Space 证据。可将以下内容发给 WorkBuddy。

```text
测试 QuietBridge 的 Chrome 离屏导航单次截图实验，保持当前前台应用、物理鼠标、键盘焦点和 Space。仅操作已确认的 Chrome 专用窗口，不涉及原生应用。全程最多 90 秒。

1. 只读核对运行应用为 0.6.25（108）、连接器已加载相应 MCP，真实扩展握手版本为 0.4.13。原专用标签应由用户先手动打开普通 HTML 动态测试页 step=1，再回到其他桌面；不要从 JSON 查看器页面开始。若当前已是普通 HTML 则直接复用。需要本地页面时，先完成第 2 步目标绑定和共享，再在终端启动：
   python3 -m http.server 48764 --bind 127.0.0.1 --directory /Users/gushaokui/Documents/AI/computer-use/tools/fixtures
   页面为 http://127.0.0.1:48764/chrome-off-space-continuity.html?step=1 。仅在端口没有现有服务时启动，并记录本轮新建服务的后台任务 ID/进程 ID。这个静态服务会让 WorkBuddy 显示仍有后台任务，但不持有 Chrome 窗口租约；所有成功、失败和提前退出路径都必须清理本轮自建服务，复用的既有服务不关闭。

2. 用 web_content 任务绑定原 Chrome 窗口，assign 原 tab，verify_mapping 后建立 source=window 系统窗口连续共享。确认 trackingWindowId 正确、targetOnCurrentSpace=false。TARGET_IN_USE 时停止本项实验并清理自己的任务，报告 targetConflict 的窗口、任务和宿主会话身份；由用户在 QuietBridge“任务与窗口”核对并停止对应会话，Agent 不自行接管。不要创建本地服务，也不从窗口标题推断占用者。Space 为 true 或未知时停止本项实验并说明，不激活窗口、不切桌、不用原生键鼠制造条件。

3. 通过扩展 navigate 将 step=1 改成 step=2（或对既有测试页做一次等价跳转）。导航后不要主动截图、render_probe、点击或滚动。新版扩展会在新文档确认后等 5 秒，仅截图一次并丢弃；你不要代为再截图。

   若导航报错，停止依赖实验并清理自己的任务/共享，不新建任务重试。BROWSER_DEBUGGER_RESTRICTED 表示其他扩展页面限制，记录 debuggerFailure 的 chromeMessage、stage、foreignFrameExtensionIds 和 frameEvidence；BROWSER_DEBUGGER_IN_USE 才表示已有调试客户端。不仅凭 debugger 权限归咎某扩展，不让重载 QuietBridge 成为连续重试，不删除 iframe、不绕过跨导航弹窗保护。

4. 随后静置观察 30 秒，只可读取浏览器 status。请用户直接观察面板：大约第 5 秒画面是否更新；第 10、20、30 秒，时钟/圆点是否还在变化，若停止约持续多久。不要每个时点都阻塞等待用户确认，统一收集一次描述。没有用户观察时明确标记未验证，不能用 DOM 时间、截图回执或 videoFrames 数量代替肉眼结论。

5. 读取 status.navigationCapture，记录 documentId、scheduledAt、startedAt、completedAt、status；startedAt-scheduledAt 应至少约 5 秒，status=captured 才表示这次截图成功。将截图时刻与用户观察对照，区分：只刷新一帧、持续几秒后停止、整个 30 秒都在更新、截图失败/被跳过。也可对照 browser.navigation_capture 元数据日志，但不能再调用任何截图。

6. 结束任务并 unshare，确认共享关闭且 5 秒不复活；按记录的后台任务 ID/进程 ID 停止本轮自建的静态服务，并确认退出。报告运行版本、窗口/tab、延迟截图时刻、人工持续性观察和全部清理证据。没有证据不要声称已经修复离屏视频。
```
