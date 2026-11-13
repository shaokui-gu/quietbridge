# QuietBridge 核心操作短流程验收

适用于 **QuietBridge 0.6.26（109）/ 扩展 0.4.14**，安装包已于 2026-11-13 生成并核验。需先由用户安装并重新加载扩展，真实握手应为 0.4.14；旧 0.6.25（108）及扩展 0.4.13 不适用。执行验收的 Agent 不自行构建、安装、刷新扩展或重启应用。

2026-11-13 现场反馈后修正：snapshot 仅提供交互元素，不含输入值及普通静态结果；填写使用 fill 的 verified 回执，点击结果使用新的 read。测试页结果已从 output 改成现有 read 覆盖的 p，不涉及安装包或扩展更新。已完成主流程时，仅补测本文末尾的结果读回，无需重跑整轮。

本文“阻塞后清理报告、不建窗”的约束仅用于 0.6.26 核心操作的固定范围验收，不是一般业务异常的结束规则。新的统一恢复与 Agent 选择流程见 [恢复说明](2026-11-13-recoverable-blockers-and-agent-decisions.zh.md)；窗口关闭恢复另按该文的专门验收运行。

可复制以下内容给 WorkBuddy：

```text
验收 QuietBridge 核心控制短流程，重点是已有目标能连续操作、正常释放、再次绑定。保持用户前台应用、键盘焦点、物理鼠标和当前 Space。复用用户确认过的专用 Chrome 窗口与标签；不要新建或关闭浏览器窗口，不移动窗口、不改标题来修复映射。无需制造离屏条件，全程不使用 render_probe 或截图促帧，不安排长跑。Chrome 阶段正常路径约 2 分钟，出现真实阻塞则立即清理自己的任务并报告。

1. 只读核对安装版本 0.6.26（109）、应用运行状态、真实扩展握手 0.4.14 及目标窗口/tab 身份。运行应用或握手仍是旧版本时结束本轮检查，说明新构建尚未进入运行环境，不运行旧截图实验。

2. begin(targetSurface=web_content)，bind 原系统窗口，使用真实 candidates 分配原 browserWindowId/tabId，verify_mapping 后只建立一次 source=window 连续共享。核对 trackingWindowId 和 holding，记录 streamReady；仅浏览器内容操作不以持续新视频帧证明 DOM 成功。同时记录 hostWindowId、hostOnCurrentSpace、hostSpaceIds、panelOnCurrentSpace、panelSpaceIds、panelSpaceVerified、panelPlacementError 和 visible。面板跟随 WorkBuddy 宿主，宿主不在当前 Space 时 visible=false 不单独判为故障；宿主在当前 Space 而面板不可见或持续迁移失败时，单独报告显示问题，不改变宿主或目标。若 share 尚在启动，最多两次只读复查；建立失败时结束并报告。不更换到 application/desktop 范围。

3. 在同一任务和同一绑定内使用普通 HTML 测试页：
   http://127.0.0.1:48764/core-control.html?step=1
   优先复用已存在的静态 fixtures 服务。若不可用且端口未被占用，可用终端启动：
   python3 -m http.server 48764 --bind 127.0.0.1 --directory /Users/gushaokui/Documents/AI/computer-use/tools/fixtures
   记录本轮新建服务的后台任务 ID/进程 ID，结束时清理；既有服务不关闭。服务启动失败就清理报告，不另换端口或窗口。这个 HTTP 服务不持有浏览器窗口租约。

4. 通过扩展 navigate 到 step=1，查询原 commandId 至终态，并用 read 或 diagnostics 核实新文档就绪和地址。snapshot 获取当前元素 ref，在“测试内容”填写 core-check；查询原命令，要求 performed=true、verified=true（扩展会比较实际输入值，length 本身不证明值正确）。随后 snapshot 完成动作复核并取新 ref，点击“确认内容”；查询原命令后，再 snapshot 完成点击复核。用一次新的 read(mode=content，不带旧 readId) 读回“已确认：core-check”，它来自测试页结果段落；snapshot 不承担读取静态结果或输入值的任务。最后以当前快照向下滚动一次，核实 beforeY/afterY 或实际滚动位置变化。每次输入串行完成和复核，不沿用旧 snapshotId/ref；read 不会解除普通输入的 snapshot 复核要求。

5. 同一绑定内依次 navigate 到 ?step=2、?step=3；每次查询原命令并用 read/diagnostics 核实新文档、地址及“第 2/3 页”。不每次重新 assign，不为普通导航额外截图，不通过重建任务解除复核要求。

6. end(confirm=true) 并幂等 unshare，核对 previewCleanup 和 holding=false。随后 begin 一个新网页任务，重新核实并绑定同一窗口、assign 同一 tab、verify_mapping、share、snapshot。确认既无本轮旧占用，也无上个任务遗留“需先复核”的门禁，随即 end+unshare。5 秒后只读确认共享未复活，清理本轮自建 HTTP 服务。

遇到 TARGET_IN_USE、权限错误或 Chrome debugger 拒绝时记录一次原始错误和结构化身份/原因，清理自己的资源后报告。不要接管、停掉别的会话、反复重载、切换原生输入或新建任务重试。若收到 TASK_RESUMED，按回执重新核实原目标、映射、共享并取新快照，再继续尚未执行的步骤；携带在恢复调用里的输入不会自动执行。任何可能已执行但结果未知的输入，先查询既有 commandId 或观察，不能重放。

报告仅列：运行版本与目标身份；绑定/共享；填写/点击/滚动；两次连续导航；释放后重绑；最终清理。操作成功以页面读回为准。面板实际显示和是否抢焦点分别记录用户观察或工具证据，未观察就写未验证，不用 DOM 回执冒充视频已更新。Chrome 在其他 Space 的持续出帧不是本轮判据。
```

原生应用另用一个已确认的专用测试窗口运行独立短流程：`begin(targetSurface=desktop_app) → bind → share(source=window)`，首帧就绪后获取 AX 快照，对已观察的无害控件执行一次点击；如有可写测试字段，再用新快照的 `valueWritable` 元素设值并读回。最后 `end+unshare`，确认焦点回执和释放。同样不激活、不用应用级键盘兜底，不因没有适合的测试控件而临时操作用户数据。原生阶段不 assign Chrome 标签，不使用任何 Chrome 专属策略。

## 已完成主流程后的最小补测

```text
只补测 QuietBridge 0.6.26 / 扩展 0.4.14 的填写与点击结果读回，先只读确认运行版本。保持用户前台应用、焦点、物理鼠标及 Space，不重跑长流程、不安装升级、不截图。复用先前确认的专用窗口/tab：begin(targetSurface=web_content)、bind、candidates、assign、verify_mapping、share(source=window)，核实共享目标和 holding，不扩大捕获范围。

复用既有 fixtures 服务；仅在 48764 端口未占用且没有可用服务时，可后台启动 python3 -m http.server 48764 --bind 127.0.0.1 --directory /Users/gushaokui/Documents/AI/computer-use/tools/fixtures 。记录本轮服务的后台任务 ID/进程 ID，所有退出路径都停止自己的服务；既有服务不关闭。

navigate 到 http://127.0.0.1:48764/core-control.html?step=1，用新的 read 确认出现“尚未确认”；如果读不到就清理并报告，不继续点击。snapshot → fill core-check → 查原 commandId，确认 performed=true、verified=true → 新 snapshot → click“确认内容”→ 查原 commandId → 新 snapshot → 新 read(mode=content，不带旧 readId)，必须读到“已确认：core-check”。不要求 snapshot 返回输入值或静态结果。

仅记录一次预览的 hostWindowId、hostOnCurrentSpace、hostSpaceIds、panelOnCurrentSpace、panelSpaceIds、panelSpaceVerified、panelPlacementError、visible，不移动/激活宿主，也不为显示验证切换 Space。最终 end+unshare，清理本轮自建服务，报告结果读回、面板状态和清理证据。遇到占用、权限、Chrome 拒绝或共享建立失败，清理自己的任务/共享/服务后报告，不接管、不重载、不新建任务重试、不换原生输入；结果未知先查既有 commandId 或观察，不重放输入。
```
