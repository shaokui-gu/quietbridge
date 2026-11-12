# 会话通道停止同步：20:04 轮诊断与修复

日期：2026-11-09。现场安装为 QuietBridge 0.6.9（92）、扩展 0.4.4；本轮源码扩展版本更新为 0.4.5。后续已按用户要求打包为 QuietBridge 0.6.10（93），未安装。

## 现场证据

已安装 App、Chrome Profile 4 扩展目录、native host 与桥接源码均核对过；关键组件与本轮修改前的工作区一致。

| 证据 | 结论 |
| --- | --- |
| 根目录 browser-events.json 持续更新至 20:08 以后，会话目录的 peer.lastSeen 停在 20:05:17.766 | 会话通道停止同步，不能据此认定整个扩展或 native host 崩溃。 |
| 20:04:45 assign 成功；20:04:47 缺少标题；20:04:52 snapshot 入队；20:05:07 报 BROWSER_HOST_UNAVAILABLE | 分配后存在同步停滞。缺少标题与命令排队属于同一时间段。 |
| snapshot 06ab9aa077cf85f76fa0d69a80fa924c 最终 canceled，delivered=false、mayHaveExecuted=false | 该次 snapshot 没有投递执行，不能归因于快照读取页面。 |
| 第二轮没有导航或输入仍失去候选回传；新任务可能继承 storage.session 中上一任务的观察器与 managedTabs | 无页面命令也会经过旧任务清理流程。 |
| 20:04 附近未发现新的 Chrome/Python/QuietBridge 原生崩溃报告；当天 10:40 的 Chrome 报告属于旧事件 | 本轮没有确认进程崩溃的证据。未取得现场 Chrome API 堆栈，不能确定具体卡在哪个 API。 |

关联任务：8d16a2af-0af8-4d81-99e8-76fbf3966c09、970368aa-2e97-4f53-9033-032a5ee9eb43。

## 已确认的代码缺陷

每个会话把 sync、导航与内容事件放在同一串行队列，sync 完成后才安排下一次同步。观察器加载／配置、旧观察器停止、弹窗约束清理和标签元数据查询中存在无截止时间的 Chrome API 等待。

用真实 startChannel/channelChrome 入口与合成 Chrome API 测试：让观察器加载不回执，或让上一任务的页面清理不回执，修改前两个测试均失败，表现为根通道继续同步、会话通道停止。这证明了可以解释现场现象的代码路径；不是实机崩溃复现，也没有证明这就是现场唯一诱因。

## 修改

- 观察器加载、配置各最多等待 3 秒；被动元数据查询和清理各最多等待 1.2 秒。使用真实 native sync 续传，不延长服务端 20 秒心跳判定、不另刷虚假心跳。
- 配置观察器也使用 injectImmediately，避免默认等待 document_idle。相同绑定／文档／epoch 的初始化最多尝试两次；失败后保留原目标，等待显式恢复或新文档，不重复页面输入。
- 标签标题不再被无界注入等待永久阻断。窗口标题证据仍需真实回传与匹配；初始化期间立即 verify_mapping 仍可能需要等待，不伪造映射成功。
- 旧观察器及多个受管标签并行清理；先清本地观察状态和所有权，再有界等待。调试 detach 独立于页面清理发出，避免页面无回执阻止释放。Chrome detach 同时移除该调试会话的 preload。
- 同一 worker 中尚未完成的 detach 阻止该标签重新 attach/reset。页面清理脚本包含执行期限；观察器停止还核对 bindingId，迟到配置超过期限不执行，迟到 Promise 结果不能恢复旧绑定。
- 清理超时会留下诊断；若清理发生在另一操作的失败收尾中，保留原操作的错误，不用清理错误覆盖它。Chrome 不提供撤销已提交脚本的通用接口，因此超时不等于已确认所有页面 hook 均清理完毕。
- 新增 observerStatus（初始化状态、绑定／文档／epoch、尝试次数、阶段、固定错误码与时间）和 lifecycleError（最后一次生命周期错误及时间）。它们不包含页面正文、完整地址或任意异常文本。
- 通道真实同步成功但观察初始化失败时，状态仍为 connected，同时 waitingReason=observer_not_ready；命令在未就绪时明确返回 BROWSER_OBSERVER_NOT_READY。生命周期错误使用 BROWSER_LIFECYCLE_TIMEOUT／BROWSER_LIFECYCLE_FAILED，不冒充 host_unavailable。
- MCP 接收并校验这些字段，只用与当前绑定／文档相符的观察状态。Agent 最多做两次只读状态复查，持续失败则通知用户并等待，不改绑、不切换输入通道。

## 验证

- Chromium Node 全套：88 项通过。
- MCP Python 全套：311 项通过。
- 新增覆盖：加载不回执、配置不回执与有限重试、旧任务清理不回执、仅候选枚举失败、活动标签元数据失败、三个真实 worker 实例的隔离与停止、迟到回执、旧观察器清理、detach 与页面清理独立、未完成 detach 阻止复用、保留原操作失败、MCP 状态及恢复提示。
- 所有新增执行测试均使用隔离合成数据，没有对用户网页执行导航、点击或输入，也未修改用户权限、开关、连接器或安装文件。

本轮不宣称所有浏览器断线原因已解决，也没有进行 30 分钟实机验收。后续需要随新 App 一并交付扩展和 MCP 协议变化，先复测仅 begin/bind/candidates 的连接稳定性，再进行页面操作；观察是否仍丢失会话心跳，以及新增诊断指出的具体阶段。

## 后续打包

dist/quietbridge-0.6.10.pkg（2.53 MiB）已生成。实际展开后的 App／Helper／授权组件签名、证书及 profiles，桥接源码、扩展 0.4.5 目录与 ZIP、升级元数据均通过核验；隔离安装预检、工具清单及包内原生权限执行器检查通过。临时钥匙串已删除，原搜索列表已恢复。PKG 容器未签名、未公证。

验收记录：dist/quietbridge-0.6.10-verification.json。
