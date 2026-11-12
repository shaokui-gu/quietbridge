# 截图后后续导航是否持续更新

**已停止使用**：用户在本轮报告后明确不再执行C，停止同类复测。下文仅保留历史提示词，不作为继续执行或放宽闸门的指令。复核见2026-11-11-round6-render-probe-review.zh.md。

当前0.6.16/扩展0.4.7即可做呈现对照。弹窗守卫修复尚未发布；若遇到旧owner拒绝，通知用户后停止，不自动刷新/换文档/输入。以下提示词供Workbuddy使用。

```text
请只验证一件事：离屏页面经一次截图恢复后，再导航是否又停住。

准备阶段核验当前QuietBridge 0.6.16（99）/真实扩展握手0.4.7、权限与专用测试页。只操作我已授权的专用标签，重新核实ID和PID代际，不使用旧ID猜测。先让我手动把窗口放在其他Space；准备阶段不共享。接到“场景已准备，开始”再执行自己的web_content任务、私有candidates、bind/assign/verify_mapping、source=window共享。若0.4.7弹窗守卫拒绝，报告等待我处理，不绕过守卫或自动换文档。

全程不抢焦点、不activate/raise、不切Space、不动物理鼠标，不使用原生网页输入或其他UI工具；不重启/重载、不改权限/flags。使用同一任务和共享流，75秒开始收尾，含工具等待硬上限90秒；禁止中途最终回复等待我，正常最终回复会触发FinalStop。中途我的消息仅作观察记录，不能据此延长计时。

共享后只跑三个观察窗，均不等待我答复：
A. 单次navigate到 http://127.0.0.1:48763/probe?step=36，read确认STEP36/URL/readyState/epoch，正常完成导航复核；进度消息“A：DOM36”，留约5秒。
B. render_probe layout一次，查询原结果后立即render_probe capture一次，不留layout观察窗；capture距layout完成不超过30秒。记录两个阶段实际startedAt/completedAt及captureDataLength，进度消息“B：一次截图完成”，留约5秒。只有这一次capture，不做其它screenshot/visual_state/系统截图/Mission Control。
C. 尚未到share起点+60秒时，单次navigate到 ?step=37，read核实STEP37/URL/epoch，进度消息“C：DOM37，不再截图”，留约8秒，然后结束自己的任务。时间不足就收尾，不能延时。

每次只查询原commandId，不重派发结果未知的操作。连接/目标/守卫异常立即停止后续并收尾，不换工具或接管其他连接。不要用quietbridge_wait_ready充当计时器。记录所有目标ID、taskId、commandId、时间及同一streamGeneration，结束后记录holding=false与captureStop；缺失或requested如实报告，Codex读异步日志确认。不关闭专用标签，不改锁屏保护。

最后再向我要A/B/C人工记录：36是否出现，截图后是否出现36，随后37是否出现。若A已正常则标记这轮未复现；若B恢复36但C仍是36/纯色则说明一次截图不足以保证后续持续更新。DOM正常不等于画面正常。
```
