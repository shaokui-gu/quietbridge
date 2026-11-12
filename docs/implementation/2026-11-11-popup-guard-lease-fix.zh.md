# 页面弹窗守卫的跨任务交接

问题与证据见2026-11-11-round5-render-probe-review.zh.md。修改范围为Chrome扩展守卫、当前文档安装/释放、通道退出，以及MCP用户引导；不修改离屏画面策略。

扩展源码版本0.4.8。guard协议2包含owner/taskId、leaseId/bindingId、claimedAt。只有经BackgroundBrowser.attach/check的worker显式安装才携带有时限claim；preload无claim，不能覆盖其他租约。相同租约幂等，不同租约只在新claim有效且比现有claim新时替换，旧链接不转移。释放要求owner和lease均相等；旧清理不能删同task的新binding。page API不是授权来源，页面可篡改，真正权限和目标身份仍由worker/native核验。

真实0.4.7页面闭包没有可读owner，不能安全推导所有旧监听。遇到未知旧协议时报POPUP_GUARD_LEGACY，通知用户刷新该专用标签一次；不伪称热修复、不自动刷新。可识别的新协议残留可无导航交接。

测试104项Node、354项MCP全量通过。新增覆盖：旧队列不泄漏、监听不叠加、旧preload不能接管、同task旧binding清理无效、过期/迟到claim无效、失去worker状态后同文档新任务可导航、preload随绑定更新、通道退出不等renderer且不影响其他通道、守卫未确认时不派发输入、旧协议要求用户操作且禁用替代工具/重放。

未运行真实UI操作；未生成新PKG。0.6.16包仍包含0.4.7，不能拿原PKG当作已含此修复。后续打包需递增App版本/构建号并包含0.4.8。
