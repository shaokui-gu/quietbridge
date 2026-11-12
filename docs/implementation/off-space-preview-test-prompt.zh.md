# 跨 Space 绘制诊断提示词

后续诊断请使用 `off-space-render-probe-test-prompt.zh.md`（0.6.16 / Browser 0.4.7）。下文保留为 0.6.15 基线测试历史流程，不用于新探针对照。

前提：安装 QuietBridge 0.6.15（98）；Codex 已启动本地测试页服务 `http://127.0.0.1:48763/probe?step=0`。若服务未启动、版本不匹配或连接器仍使用旧版本，请报告并等待，不自行安装或重启用户应用。

2026-11-10 复盘后修订：原流程要求共享后结束回复等待用户，正常 FinalStop 会释放共享；用户回复时已不是原流。改为先摆放场景、每次单轮短测试，运行中只发进度消息，完成收尾后再汇总观察。保持停止即释放的产品行为。

下面内容可直接交给 Workbuddy：

```text
请协助定位 QuietBridge 在其他 macOS 桌面操作 Chrome 时页面正文与共享预览不更新的问题。本轮只做诊断，不修改系统、Chrome flags、权限或连接器配置。

核心约束：全程不抢焦点、不激活/raise 浏览器、不切换 Space、不移动物理鼠标；所有桌面切换和窗口摆放由我手动完成。网页操作只用 quietbridge 的 Chrome 扩展，不使用原生输入兜底，不操作已有用户标签。未知结果不得重放。不要把已有旧任务自动接管。

1. 核验 QuietBridge 为 0.6.15（98），先在没有活动任务时查询 permissions 与 browser_events candidates，扩展实际连接握手 clientVersion 为 0.4.6，权限和连接可用。connectionScope=extension_preflight 是全局准备检查；这里 taskChannelReady=false 是正常值，不代表扩展离线。不能用磁盘 manifest 代替实际握手。遇到 APP_NOT_RUNNING、权限不足、连接断开、目标证据不匹配等问题，先向我报告需要处理什么，暂停相关操作，不换其他工具绕过。先只读 apps get Chrome 并确认能取得 appHandle；STALE_APP 最多两次只读重取，仍失败则报告，不推断升级、重启或忽略身份核验。
2. 准备阶段先结束回复，让我摆好测试窗口并指定本次场景（可见、遮挡、其他桌面三选一）和模式（仅基线、基线后一次截图二选一）。准备阶段不建立共享。旧测试标签/窗口 ID 不再作为有效前提，必须重新核实真实候选、PID 代际与 URL。多个 Chrome 实例时优先核实扩展 native host 的父进程归属，不能只选有可见窗口的实例；独立 Playwright profile 不代表装有 QuietBridge 扩展。若目标不存在，报告后等待明确的新建目标指令，不能猜测其他用户标签。明确获准新建后，准备阶段允许创建一个必要的 background_browser / web_content 任务，只执行一次 create_target（测试页 step=30，工具使用 focused=false），只读追踪该请求并记录真实结果；未知结果不重建。创建完结束自己的准备任务、不建立共享，保留专用目标，再结束回复让我手动摆放场景。原生窗口尚不存在时不能要求先 bind 才 create_target；创建回执中的浏览器 ID 也不能当作系统窗口 ID。
3. 我发出“场景已准备，开始”后，按实际 schema 以 targetSurface=web_content 创建自己的新任务，不复活旧任务。新任务候选使用私有通道；若 channel_pending/BROWSER_CHANNEL_STARTING，按 retryAfterMs 最多两次只读复查，不重载扩展或重复新建。全局准备候选不能替代私有 assign 证据。查询真实候选和原生窗口，显式 bind、assign、verify_mapping，再以 source=window 共享。映射未验证、TASK_IN_USE、任务中断、连接变化等情况立即收尾并报告，不结束其他连接。记录 taskId、windowId、browserWindowId、tabId 与开始时间。不要因 currentSpace 的单一布尔值覆盖我的手动场景记录。
4. 本次只测一个场景，连续执行约 45–75 秒，总时长不超过 90 秒。依次导航到 http://127.0.0.1:48763/probe?step=31、step=32、step=33，每次串行执行一次并按工具要求用 DOM read/diagnostics/snapshot 复核 STEP 编号、readyState、URL、epoch、活动目标与时间；未知结果只查询原 command，不重放。每次读回后发进度消息“观察窗口 A/B/C：预期 STEP 31/32/33，现在开始观察约 8 秒”，继续工具执行，不能发最终回复来等我回答。允许使用已可用的本地计时工具作约 8 秒纯等待；计时工具不得控制浏览器、读取页面或充当 UI 兜底。没有合适计时工具时不伪造等待，可缩短本轮并报告。不要用 quietbridge_wait_ready 当纯计时器，它可能获取 visualState、污染无截图基线。最多进行必要的有限状态查询，不通过无限轮询维持任务。
5. 我会在 A/B/C 实际运行时看预览并自行记下时间、STEP、颜色和预览是否可见，待你收尾后统一回复。你不编造人工观察，也不把 DOM 更新当成预览正确。整个基线禁止 screenshot、visual_state、系统截图和 Mission Control；后者也可能影响合成状态。全窗像素变化可能来自标题/工具栏，不证明正文绘制正确。本版另有中央区域 bodyContentChanges/bodyNearUniformSamples 和 sourceFrame 格式、stride、颜色元数据；它们是固定中央裁剪的稀疏统计，不是 DOM 正文边界，也不能单独证明真实画面正确。不要额外截图来获取这些统计，Codex 会从日志读取。
6. 仅当准备阶段已明确选了“基线后一次截图”，才在同一任务和共享保持有效时记录截图前时间，发进度消息，然后对已绑定标签执行一次扩展 screenshot，记录回执时间、再留约 8 秒观察窗口 D。不在中间发最终回复请求批准，不重建共享。前后核实同一任务仍 bound、holding、streamReady；若中断/重建则截图恢复实验无效，不归因给截图。本轮不要求手动切桌：切桌前后对照另行预先约定在同一活动任务内的时间窗口，不能在最终回复后把新一轮当作原流延续。
7. 结束前先记录观察窗口的结束时间，再只结束自己的任务，确认 holding=false、面板/租约已释放；记录 previewCleanup.captureStop 的流编号、stopState、stopConfirmed。released 不等于 stopCapture 已成功返回；requested 是待完成，failed 是停止失败，缺失代表没有足够证据。不无限轮询，不自行重建共享或重启宿主；异步完成由 Codex 通过 captureStopCompleted/captureStopFailed 日志核验。保留测试标签，不改变锁屏保护或其他会话。最后才发最终回复并向我要 A/B/C/D 的观察记录。最终回复触发 FinalStop 属于预期清理，不称为宿主崩溃或重启。收尾返回 TASK_IN_USE 时如实报告归属未确认，不能仅凭此断言共享仍在运行，也不能强制释放。输出工具观察与人工观察分开的时间表；人工记录尚未收到时标为待补。

本地日志由 Codex 关联采集，你不需要读取用户配置、日志全文或任何密钥。
```

## 本地采集命令（由 Codex 执行）

```sh
python3 -B tools/preview_diagnostics.py serve --port 48763
python3 -B tools/preview_diagnostics.py mark visible --output dist/off-space-preview-investigation-20261110/stages.jsonl
python3 -B tools/preview_diagnostics.py analyze --window-id 实际窗口ID --since 本轮开始的Unix秒 --output dist/off-space-preview-investigation-20261110/progress.json
```

mark 支持 visible、covered、off_space、before_screenshot、after_screenshot、before_switch、after_switch、end。可按用户消息及时写入标记；用户消息时间存在误差，不冒充逐帧同步。分析工具读取 native-operations.jsonl 及 .1，按进程、窗口和流代际分组；正常静态样本不判冻结，enqueue 也不代表已经呈现。

优先判读：已确认 DOM/大色块变化但源像素变化计数停滞，且切回 Space 后恢复，进一步查 Chrome 绘制或窗口合成；源像素持续变化而提交停滞/背压增长，进一步查预览链路。两者都正常仍需核验实际展示和目标身份。首次截图是否改变现象单独记录。基线完成前不加 Chrome 启动参数，不重启已有浏览器。

## 0.6.14 诊断字段

- `captureLifecycle` 对应当前流，`captureStop` 对应最近请求停止的流，两者可能不是同一代际。按 `streamGeneration`、原生宿主 PID 和窗口 ID 关联，迟到的旧流停止回执不能当成新流停止。
- 原生日志新增 `preview.capturePreparing`、`captureStartRequested/Completed/Failed`、`captureStopRequested/Completed/Failed` 和 `captureDelegateStopped`。请求停止的时间包括可能等待 startCapture 返回的时间；只有 StopCompleted 证明该次 stopCapture API 成功返回，不保证所有系统内部资源已经回收。
- `frameProgress.sourceFrame` 只描述最近成功取样的输入缓冲区。`lastSourceSampleAgeMs` 表示样本年龄；BGRA 记录 alpha 范围，NV12 分别观察亮度和色度差异，避免只看亮度漏掉变化。`bodyNearUniform` 是固定稀疏网格的近似判断，不表示整帧每个像素都一致。
- 未支持的像素格式计入 sampleFailures/bodySampleFailures 并清空旧格式信息，不把旧信息当作新帧。本版不保存源帧或像素指纹，不增加捕获、不修改焦点与 Space，也不视为画面问题已修复。

## 2026-11-11 准备阶段复核

11:03 恢复本地测试页服务，绕过 HTTP_PROXY 直连 `/probe?step=0` 为 200。11:04–11:05 对既有状态文件的只读检查发现 0.4.6 新心跳，enabled/permission=true，心跳年龄不足 1 秒，且与当前菜单应用的有效 session 匹配；这说明检查时根通道有新握手，不追溯否定此前 host_unavailable，也不替代 Workbuddy 自己的下一次工具核验。

原生浏览器宿主 PID 91789 的父链为 shell 91776 → Chrome 97718；Chrome 43443 使用独立 `ms-playwright/daemon/...` 用户目录，不是本次心跳的来源。因此不能根据“只有 43443 有真实窗口”就选它绑定。PID 仅为本次快照，后续每轮仍须重新核实，不固定猜测进程。

旧窗口 187636/标签 57074733 已由 Workbuddy 报告不存在，尚未创建替代目标。本轮 Codex 只恢复本地静态服务并读取状态/进程归属，未建立任务、共享、截图、导航或重载扩展。

## 0.6.15 准备阶段修复

0.6.14 的会话 worker 在任务开始前读取私有 browser-events.json，而扩展仅为 registry 内的活动任务发现私有通道。结果是根心跳新鲜、该会话准备检查却 host_unavailable，按先检查再 begin 的流程无法前进。本版无任务时读取全局扩展心跳与候选元数据，不创建任务或预览、不占执行槽；任务开始后仍由私有通道完成候选、绑定和命令。首次握手等待有界，真实权限拒绝不会被标记为启动中。

扩展仍为 0.4.6，此次修复不依赖用户重载扩展。安装后先在 Workbuddy 复查无任务 candidates 的 clientVersion/connectionScope；不要把本次修复当作离屏画面问题的修复。
