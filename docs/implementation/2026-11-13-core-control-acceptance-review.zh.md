# 0.6.26 核心操作现场验收复核

日期：2026-11-13。依据：用户回传的 WorkBuddy 现场报告、现有源码及同轮本机原生元数据日志。本轮仅只读核查运行日志，修改本地测试页和验收文档，未操作真实 Chrome、创建任务/捕获、截图、切桌、安装或重启应用。

## 判定

核心执行及生命周期链路通过；点击后的业务结果读回未完成，预览可见性与用户前台体验未完成验收。不能合并宣称所有核心能力全部通过。

| 项目 | 现场证据 | 判定 |
| --- | --- | --- |
| 运行构建 | App/Helper 0.6.26（109），真实扩展握手 0.4.14 | 新构建生效 |
| 固定目标 | Chrome pid 97718，系统窗口 280997，browserWindowId 57075101，tabId 57075102 | 始终复用同一目标 |
| 分配/映射/共享 | 标题证据到达后 verified/exact；一次 window 共享，trackingWindowId 正确，streamReady=true | 通过；首次标题未到不能据此判为映射缺陷 |
| 填写 | fill performed=true、verified=true，length=10 | 扩展已比较实际输入值；length 单独不足以证明内容 |
| 点击 | performed；静态结果未读回 | 已派发，业务结果待补证 |
| 滚动 | beforeY=0，afterY=240，moved=true；快照坐标变化同为 240px | 通过 |
| 连续导航 | 同绑定 step 1→2→3，新 documentId/epoch，read 读到对应页标题 | 通过 |
| 释放后重绑 | 新任务原窗口/tab 重新绑定、共享及 snapshot 成功 | 无旧占用、无跨任务复核门禁 |
| 目标候选刷新 | 刷新发现原候选 URL 已改变，重新 candidates 后 assign 成功 | 身份复核生效；不是死锁 |
| 清理 | end+unshare，5 秒后 holding/streamReady/streamStarted=false，trackingWindowId=0，stopConfirmed=true；本轮 HTTP 服务退出 | 通过 |

原生应用的控件操作尚未在这份报告中覆盖，不能据此一并验收。

## 观察与验收设计的偏差

`content.js` 的 snapshot 仅收集交互元素及其角色、名称和几何信息，不返回输入框 value，也不保证收集普通静态文本。fill 的 `verified=true` 来自 `__quietbridgeVerifyFill` 对当前元素实际值与请求值的比较，因此本轮填写具有值匹配证据，不能降格为只有长度。

点击结果位于测试页 `<output>`，而现有 `read(mode=content)` 在没有 article 时只采集 p、h1–h3、li、pre 和 blockquote。原提示词却要求用 snapshot 读取输入值和静态结果，超出了工具现有返回范围；这是验收设计错误。未读取到 `<output>` 不能证明点击失败。

本轮按“先做减法”的范围修正本地 fixture：将结果节点改为 p，继续由原按钮处理函数写入。主提示词改为 fill verified 比较 → snapshot 取新引用 → click → snapshot 完成当前任务动作复核 → 新 read 读取结果。保持现有操作、返回 schema 和读取范围，不新增值导出、截图或通用执行入口；无需更新安装包或扩展。

点击后 scroll 被“需先 snapshot 复核”拦截，是本任务内已执行动作的既有复核要求；不同于已修复的旧任务遗留门禁。read 不解除普通输入的该项要求，文档已明确顺序。

## 面板证据

只读 `~/Library/Logs/quietbridge/native-operations.jsonl`，同轮 helper pid 71891 的记录为：

- 00:33:22.230：捕获目标 280997，宿主 pid 10225/window 273960，hostSpaceIds=[366]，面板 Space 尚未确认，迁移校验暂时失败。
- 00:33:22.865：同一宿主与捕获目标，hostSpaceIds=[366]、panelSpaceIds=[366]，panelSpaceVerified=true、placementError=null。

约 635ms 后面板已经通过与宿主一致的 Space 校验。源码将展示宿主和捕获目标分别绑定；面板跟随 WorkBuddy 宿主，即使宿主不在当前 Space，也不会为展示面板切换用户桌面。诊断的 visible 还要求面板属于当前 Space。

因此“panelOnCurrentSpace=false、visible=false”只证明当时当前桌面看不到面板，不能单独证明迁移失败，也不能证明选中的 WorkBuddy 窗口就是用户预期的宿主。报告缺少同次的 hostOnCurrentSpace，未给出用户观察；暂不修改宿主选择和 Space 策略。若后续 hostOnCurrentSpace=true 且面板仍不在当前 Space 或不可见，再沿显示链路定位。

## 最小剩余验收

使用[提示词末尾的最小补测](core-control-acceptance-prompt.zh.md)：在现有 0.6.26 上只补输入与点击结果读回，顺带记录宿主/面板状态，不重跑全部流程，不发布新版本。用户前台体验仍需实际观察，工具未激活窗口并不等于已证明全程没有任何焦点变化。
