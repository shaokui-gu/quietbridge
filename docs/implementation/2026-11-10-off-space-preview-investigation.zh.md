# 跨桌面浏览器画面不更新：首次只读排查

日期：2026-11-10，北京时间。用户要求先查原因，并确认问题长期存在、今天上午的测试均可纳入排查。本轮读取用户已有日志与回执，在源码补充诊断统计与隔离测试；未改变捕获或刷新策略、启动浏览器、切换 Space、建立新捕获、重启服务、安装组件或更改权限。

## 可确认的事实

1. 当前安装 App 为 0.6.11（94）；根浏览器通道和最近任务通道记录的扩展版本均为 0.4.6，未见该通道 lifecycleError。
2. 最近 Workbuddy trace `4822/trace_43705521990844ab8c65656da9b6d50c.jsonl` 中，10:59:55–11:12:53 对窗口 162435 有 25 条可提取的 preview 回执；24 条 targetOnCurrentSpace=false，均 streamReady=true。
3. 这 24 条后台 Space 回执的 frameAgeMs 最小 69.03、最大 152.69、中位 106.67 毫秒。它是主线程最近处理 complete 视频样本的年龄，不是测得的帧间隔，也不证明像素内容变化或显示层成功展示。
4. 同一 trace 有 11 条页面 diagnostics，均 readyState=complete，URL 路径随导航变化。这支持 DOM 和导航仍在执行，不能把现象概括成整个浏览器或连接器停止运行。没有拿到同一时刻源视频像素与扩展截图的对照，不能据此证明可视画面也同步更新。
5. 最近保存的 windowEvidence.tabId 与任务 assigned tabId 同为 57074697，browserWindowId 同为 57074696。仅能排除最后一次证据的活动标签错配，不能推断全程始终一致。
6. 用户确认上午测试均存在所述现象，以上任务属于该范围；但没有精确到某次切换 Space 的人工时间标记，无法做逐帧切换前后对照。该任务 11:13:50 已因 host_final_stop_completed 结束，不自动重建它来复现。

脱敏证据摘要保存于 `dist/off-space-preview-investigation-20261110/evidence.json`，不包含页面正文、截图、输入内容或认证信息。

## 代码检查发现

### 捕获不以切换目标桌面为刷新触发条件

`SpacePreview` 用独立窗口 `SCContentFilter(desktopIndependentWindow:)` 和持续 SCStream。后台 Space 的 window 模式没有等待“切到目标桌面”才取帧的分支；只有显式 desktop 共享需要目标位于当前 Space。预览面板跟随宿主，源窗口身份不应随用户焦点改变。

### 明确存在收到帧与显示帧的统计混淆

`SpacePreview.swift:457` 只有 `video.isReadyForMoreMediaData` 才 enqueue，但后面的 frameCount、lastFrameTime 无条件更新。因此显示层背压时可丢帧，同时仍报告 frameAgeMs 很小。streamReady 也只检查曾有帧、没有错误，不核验持续显示进度。

这是已确认的可观测性缺陷；现有证据不足以说明本次实际发生了背压。即使 enqueue 成功，也只是提交显示层，不等于确认已呈现。不能靠改计数直接声称修复画面冻结。

### 尚无源画面变化与提交链路的分层证据

已安装的 0.6.11 常驻日志没有保存 complete/idle/blank/suspended 的分类计数、像素变化计数、enqueue 成功次数和背压丢帧次数。该版本 bridge 的日志白名单还会过滤 frameAgeMs、videoFrames、videoLayerReady 等字段；部分字段只能在 Workbuddy 原始工具回执中找到。

已有独立 StreamProbe 可以在内存里抽样像素变化，但不会自动挂到用户任务；本轮未启动它，避免产生额外捕获及混淆原始现场。

### Chrome 后台渲染是候选原因，尚未定案

受管 BrowserSession 启动参数只有独立 profile、跳过首次运行、跳过默认浏览器检查与 about:blank，没有针对遮挡后台绘制的启动配置。Google 的工具文档说明 `--disable-backgrounding-occluded-windows` 与遮挡窗口后台化有关，并明确仅禁用 renderer 进程降优先级并不等于恢复绘制或定时器。

这只能支持一个待对照实验的方向，不能证明用户当时用了受管实例、Chrome 命中了该机制，或加参数即可解决。既有 Chrome 进程也不能靠给另一次启动附参数就推断已改变行为。本轮未加入参数或重启用户 Chrome。

官方参考：

- [GoogleChrome chrome-launcher：Task throttling](https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md)
- [Chromium：Mac Window Occlusion API Use](https://new.chromium.org/developers/design-documents/mac-occlusion/)（设计文档，不能单独作为本机当前版本行为证明）
- [Apple：SCFrameStatus.idle](https://developer.apple.com/documentation/screencapturekit/scframestatus/idle)：画面未变化时可无新帧；静态页面没有像素变化不能直接判冻结。

## 当前判断与下一次取证

已经缩小到浏览器可视表面／WindowServer 捕获／预览显示链路；最近证据不支持“后台桌面完全停止发帧”或“所有导航都没执行”的简单结论。仍不能在源画面重复、显示层背压及窗口合成恢复之间给出唯一根因。

### 用户补充：原浏览器也停在地址更新、正文未绘制的画面

用户进一步确认：未切换到目标 Space 时，源浏览器地址已更新，但正文尚未显示；预览与当时源浏览器的显示一致。这是人工观察证据，显著降低“只有预览显示层卡住”的优先级。结合 DOM 正常与 complete 样本仍到达，当前首要假设变为 Chrome 对非当前 Space 窗口的可见性判定影响页面绘制／合成，而系统窗口捕获继续返回已有表面。尚不能仅靠这些观察划分 Chrome renderer、GPU 合成与 WindowServer 的具体责任。

Chromium 的 Mac occlusion 设计文档明确把其他桌面列为不可见性来源；公开主分支 `WebContentsImpl::UpdateWebContentsVisibility` 也仍包含针对 OCCLUDED 状态的禁用后台化开关。但主分支不等于本机 Chrome 157.0.8103.40 的精确实现，OCCLUDED 与 HIDDEN 也不能混同：该分支中的开关只将 OCCLUDED 改为 VISIBLE，不能保证所有后台标签、最小化或其他 Space 情况都获益。ScreenCaptureKit 的原生窗口共享也不能当作 Chrome 已启用内部 tab capture 的证据。

新增源码参考：[Chromium WebContentsImpl](https://chromium.googlesource.com/chromium/src/+/main/content/browser/web_contents/web_contents_impl.cc)。下一步优先在独立受管实例对照有无后台绘制配置，并同步查看源像素变化；不直接修改或重启用户已有 Chrome，也不以激活窗口或切换 Space 作为刷新措施。本轮只更新判断和排查记录，未加入实验启动参数。

下一次应对专用可控页面，关联同一 task、tab、window 和导航 epoch：

1. 页面 DOM 中可验证的变化标记与当前活动标签身份。
2. SCStream 帧状态、采样像素变化、流代际及帧龄，区分没有帧与重复旧内容；只记录计数，不记录图像或正文。
3. 显示层可接收状态、实际提交计数、背压丢帧计数及状态转换，避免把收到样本当成显示成功。
4. 由用户明确记录手动切换目标 Space 的时刻，对照切换前后各层变化。采集程序不自行切桌面、激活、raise 或移动物理鼠标，也不以截图轮播冒充视频预览。

## 本轮已补充的诊断（已打包 0.6.12，尚未安装）

- `PreviewFrameProgress.swift` 区分各类捕获回调、实际 enqueue 提交与显示层背压丢帧，并分别记录最近回调和最近提交的年龄。提交不等于已呈现。
- complete 帧每秒最多进行一次稀疏像素抽样，只输出样本数和变化次数。图像与指纹均不写入日志；稀疏抽样可能漏掉很小的局部变化，静态内容不自动判为冻结。
- 每条活动捕获流最多每 5 秒写入一次 `preview.streamProgress`，关联流代际、窗口与当前 Space 状态；工具诊断日志保留经过白名单和有限数值校验的统计。
- 保留原来的捕获、enqueue、丢帧和旧 frameAgeMs 行为，避免在诊断前改变现场刷新策略。
- 合入浏览器选路说明后 MCP 全量 331 项测试通过；Swift 预览与操作日志相关 34 项、本地诊断工具 5 项测试通过。全量测试曾暴露测试替身污染：模拟所有 `threading.Event.wait` 会推进其他后台线程的假时钟，已改为只模拟当前请求的取消等待，不改变生产等待行为。

下一轮需运行含上述诊断的构建，才能以源内容变化和显示提交的对照进一步定因；当前安装的 0.6.11 不含这些统计，不能声称已修复。

诊断包已生成：`dist/quietbridge-0.6.12.pkg`（95），扩展保持 0.4.6。包内签名、证书、profiles、版本、源码、隔离 MCP 与原生权限执行器检查通过，见 `dist/quietbridge-0.6.12-verification.json`。没有安装或重启应用。固定静态测试页服务已在 `127.0.0.1:48763` 启动并通过 HTTP 检查，不打开浏览器；用户安装并手动摆放窗口后按 `docs/implementation/off-space-preview-test-prompt.zh.md` 取证。基线时间和日志读取位置存于 `dist/off-space-preview-investigation-20261110/diagnostic-baseline.json`。仍不把本轮新增统计当作根因修复。

## 另外发现的独立问题

同一 trace 有 5 次 click 回执为 INVALID_BROWSER_RESULT，不能当作点击完全未执行。源码最终回执包含点击坐标，坐标可能带小数，但 Python validate_action_result 目前仅接受 bool/int/短字符串。隔离验证：x=100 被接受，x=100.5 被拒绝，错误为“观测值类型或长度错误”。

这证明了一个独立的回执类型缺陷，但未拿到被丢弃的现场原始坐标，不能断言 5 次都由它触发；也不能用它解释连续导航时的视频不更新。本轮按“先查原因”的范围记录，未顺带修改输入逻辑或重放失败点击。
