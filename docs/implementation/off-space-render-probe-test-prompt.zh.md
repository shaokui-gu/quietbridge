# 离屏呈现分阶段诊断 · 0.6.16（99）/ 扩展 0.4.7

本版用于取证，尚未修复离屏灰色。优先区分布局查询和真正截图，再观察后续导航是否持续更新。普通 navigate/create_target 会有调试连接前置保护，因此不能默认 screenshot 是首次连接。探针也可能影响呈现，不能当成被动检查。

直接交给 Workbuddy：

```text
请用 QuietBridge 做一次离屏预览的短诊断。全程不抢焦点、不激活/raise、不切 Space、不移动物理鼠标、不使用原生网页输入，不重启应用/连接器或修改权限、Chrome flags。不得接管其他会话。所有窗口摆放由我手动完成。

准备阶段：先只读核验应用 0.6.16（99）、实际扩展握手 clientVersion=0.4.7。无任务 candidates 的 connectionScope=extension_preflight、taskChannelReady=false 属正常准备元数据，不能替代任务内 assign。旧扩展或连接不可用时报告并等待。核实已有专用测试页 http://127.0.0.1:48763/probe?step=33 的真实候选与原生窗口、Chrome PID 代际；旧编号只是线索（曾为窗口204047/标签57074801），不得直接复用。目标或服务不存在时报告，不自己操作用户标签或另开窗口。准备阶段不建立共享，结束回复，让我把专用窗口摆到其他 Space，并回复“场景已准备，开始”。

运行阶段：只创建自己的 targetSurface=web_content 任务，使用任务私有候选完成 bind → assign → verify_mapping，任务内 browser_events status 的 capabilities 必须含 render_probe，再 share source=window。映射/权限/通道未就绪、TASK_IN_USE、任务中断或未知目标都停止并收尾，不换工具。记录 taskId、原生windowId、browserWindowId、tabId、时间和 streamGeneration。本轮始终同一任务、同一 SCStream，不能中途 end/rebind/unshare/share。开始前确认 target 标签为共享窗口的活动标签；后台标签的DOM不代表共享窗口内容。

从 share 成功起硬上限90秒，不等待我中途回复。只发进度消息，最后才发最终回复，防止 FinalStop 提前释放共享。未知命令只查询原 commandId，最多两次有间隔复查；不重复派发。计时仅用已有本地等待工具，不用 quietbridge_wait_ready（会观察画面）充当计时器。

1. 对原文档 read 或 diagnostics 核实 STEP/URL/readyState/epoch，不导航、不截图。完成已有动作正常复核，再执行 command operation=render_probe stage=inspect，并查询同一commandId。记录全部固定元数据。发“观察 A：基线”，留约5秒，我自行记下预览内容和时间。
2. 仅当 inspect.attachedAfter=false 才单独执行 stage=attach，记录回执，留约4秒观察 A1；仅当 pageEnabledAfter=false 才单独执行 stage=enable，记录并留约4秒观察 A2。已有连接/已启用时跳过，不重新连接、不自动detach，不称为首次连接对照。inspect只是本连接追踪状态，不证明没有其他调试器。attach/enable属于明确诊断干预。
3. 单独执行 stage=layout，记录时间与视口尺寸，发“观察 B：仅查询布局，尚未截图”，留约6秒。
4. 在 layout 完成30秒内，单独执行一次 stage=capture，记录 startedAt/completedAt/captureDataLength。它只单次采样并丢弃图像，不返回图片。发“观察 C：单次截图探针完成”，再留约6秒。失败、expired、unknown 均不补拍、不重新layout重试；立即按原任务收尾。禁止其他 screenshot、visual_state、系统截图或 Mission Control。
5. 时间仍足够时，只导航一次到 http://127.0.0.1:48763/probe?step=34（若原页已是34则预先报告选择35），用 read/diagnostics 正常核实新STEP/URL/epoch，再发“观察 D：后续导航”，留约6秒。此步骤不再截图，用于区分一次呈现和后续持续更新。navigate 本身可能执行 Page.enable/弹窗保护，所以D只能说明完整导航路径下的后续表现，不能归因于单个CDP调用。若已到75秒则跳过D直接收尾，不延长任务或租约。
6. 只结束自己的任务，记录holding=false、previewCleanup及captureStop的streamGeneration/stopState/stopConfirmed；requested表示等待API完成，不伪称已确认，Codex会读异步日志。缺失字段就是证据不足。保留测试标签，不更改锁屏保护或其他会话。最终回复再向我要A/A1/A2/B/C/D的观察记录。

输出简短时序表，包含阶段、commandId、阶段开始/完成Unix时间、attached/pageEnabled前后状态、performed及实际错误码。不要把DOM正常等同于预览正常，不编造我的人工观察。若基线本来正常或中途流代际改变，标记本轮无法证明灰色恢复的触发点。不得把探针变成高频自动截图“修复”。
```

Codex 采集（只读日志，不启动捕获）：

```sh
python3 -B tools/preview_diagnostics.py analyze --window-id 实际原生窗口ID --task-id 实际任务UUID --since 开始Unix秒 --output dist/off-space-preview-investigation-20261111/render-probe-progress.json
```

输出 renderProbes 按 taskId/commandId 去重；阶段时间来自扩展，timestamp 是结果被查询记录的时间。窗口过滤只作用于原生捕获数据，探针需用本轮已核实任务与窗口绑定关联；时间接近不等于因果。只保留固定状态、数值与标识，不导出页面数据或图像。
