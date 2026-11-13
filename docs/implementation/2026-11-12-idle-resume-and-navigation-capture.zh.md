# 等待暂停恢复与 Chrome 导航延迟截图

## 等待用户输入后自动恢复共享

旧链路把 120 秒空闲统一视为终止：任务 interrupted、运行句柄丢弃，恢复指引要求模型通知用户并等待。现场日志包含多次 idle_timeout；这些日志证明空闲中断存在，不能单凭时间推断每次都在等用户输入。

已绑定任务现进入 suspended：立即释放共享，按既有有界规则释放或保留会话保护，原执行 worker 在内存保留原目标及共享范围。下次目标调用先核验进程代际、窗口、应用运行会话、宿主停止记录和跨会话占用；核验通过后重取句柄、续接原目标。恢复回执为 TASK_RESUMED，提供 resumption.nextSteps，明确不要求用户再次提醒重连。状态查询可返回 TASK_SUSPENDED 的恢复指引，但 ping/状态心跳不会自行唤起共享。

恢复调用携带的输入不执行。原生输入必须重新观察后才放行；Chrome 轮换 binding/token，重建原标签的文档绑定，重新验证窗口映射并共享、snapshot。旧引用、旧映射与待发输入不复用，已经可能执行的命令仍保留 mayHaveExecuted。不重试先前副作用。只对同一存活 worker 的空闲暂停恢复；取消、end、unshare、宿主 FinalStop/SessionEnd、应用重启、连接器退出和进程代际变化不自动恢复。原生资源租约先到期时，仅更换本 worker 已撤销的原生 socket，不重启其他会话。

## Chrome 导航后 5 秒截图实验

仅限 Chrome web_content 任务，原生应用和 browser_ui/native_dialog 阶段不适用。预览继续使用 ScreenCaptureKit 系统窗口连续视频。

1. 真实共享回执发布短期窗口 Space 证据。worker 每秒最多一次读取 preview status 刷新证据，不续任务活动或视频租约；证据 4 秒过期。
2. URL 导航提交并确认新文档后安排 5000ms 定时器。初始分配不算 URL 跳转；DOM/load 重复事件不重置定时器。新导航取消旧文档定时器。
3. 到期时复核 task、binding、assignmentVersion、tab、window、document 和 epoch，及扩展权限/启用状态。仅共享仍有效且原生 Space 证据明确为 false 时对原标签截图一次；当前桌面、Space 未知、目标替换、结束或撤权时跳过。
4. 导航立即截图路径已去除。图片在扩展内丢弃，不作为视频帧、不落盘、不发送给模型。原有其他页面动作后的绘制提醒仍存在，因此持续性观察期间不可点击、滚动或主动 screenshot/render_probe。
5. 尝试前持久记住本次文档身份，失败不自动重试。status.navigationCapture 和 browser.navigation_capture 日志只记录身份及计划/开始/完成时间与 captured/failed/skipped；它们不证明窗口视频真的更新。

扩展版本为 0.4.12，需要同时更新 MCP 以提供真实 Space 证据。系统繁忙时可晚于 5 秒，不能早于 5 秒。截图之后只更新一帧还是继续产出一段时间，仍须用户肉眼观察。本次未操作真实浏览器、未安装或切换 Space。测试提示词见 [Chrome 延迟截图实验](chrome-delayed-capture-test-prompt.zh.md)，动态页面为 tools/fixtures/chrome-off-space-continuity.html。

## 自动化验证

恢复回归覆盖空闲释放、重新取句柄、旧输入不派发、新观察闸门、浏览器私有通道恢复、未知动作清理、明确停止、进程代际变化、权限不足和目标不符。导航回归覆盖 5 秒前不截图、延迟后单次截图、后续无截图轮询、快速连跳取消、DOM/load 不延期、当前/未知 Space、结束共享、撤权及无图像回传。测试全部使用隔离文件、模拟 helper/时钟，无真实浏览器输入。

长时活动测试原有的顺序依赖已定位：其他初始化测试会启动真实 watchdog，虚拟时钟测试未使用生产调用持有的 TOOL_LOCK，watchdog 可观察到只替换一半的两个时钟。该测试补齐相同锁，避免测试时钟跨线程污染；没有放宽任务时限。

最终检查：MCP 全量 384 项、扩展全量 114 项、打包回归 47 项全部通过；JavaScript 语法检查与 `git diff --check` 通过。

随后按用户要求生成 0.6.23（106）安装包，内含扩展 0.4.12；原生 Release 构建及 BrowserSession 相关 19 项测试通过。实际 PKG 展开后核验了三个组件的签名、证书/profiles、版本、桥接源码、扩展目录/ZIP、安装脚本，以及隔离 MCP 和原生权限执行器，全部通过。核验报告为 `dist/quietbridge-0.6.23-verification.json`。临时钥匙串已删除、搜索列表已恢复。PKG 容器未签名、未公证；没有安装或重启现有应用，没有真实视频连续性结论。
