# 2026-11-10 验收复盘与修复

## 范围与结论

本次对照用户提供的 `qb_log.txt`、`quietbridge_acceptance_report.md`，以及本机 bridge/native 操作日志、Workbuddy 工具回执和源码。原始报告保持原样。修改仅在源码和测试中，未打包、安装、重启连接器、操作用户网页或更改权限开关。

现场安装为 QuietBridge **0.6.10（93）／扩展 0.4.5**。本次源码扩展版本更新为 **0.4.6**，尚未部署。现有证据支持桌面计算器、不抢焦点点击、网页填写和滚动、同一绑定的 20 轮导航通过；后台网页 click 是安装版本上的明确阻断，完整长时间稳定性验收尚未完成。

## 报告逐项复核

| 项目 | 复核结论 | 本次处理 |
| --- | --- | --- |
| D1 后台 click 超时 | 原始回执卡在 `Input.dispatchMouseEvent/mouseMoved` 预备悬停，尚未发送按下事件；不能据此认定 press/release 也失败。 | 移除独立预备悬停；保留点击前坐标命中、标签、文档和 epoch 核验，再发 CDP 按下／松开。未知结果仍停止输入、不重放、不回落原生键鼠。 |
| D2 重定向复核 | 两次地址变化后可以用 snapshot 解除等待，不是永久死锁；实际问题是没有提示正确的下一步。 | 当前绑定的新文档已就绪、只读观察证实落地地址变化时，明确返回 `navigation_destination_changed`、`nextAction=snapshot`，继续原绑定。不自动接受任意重定向。 |
| D3 阶段 end 后保护保留 | `scope=host_run` 的保护为同一宿主运行的后续界面阶段保留，符合此前需求与工具说明；阶段结束不等于宿主会话结束。 | 保留现有设计，增加共享清理回执，区分共享释放与会话保护。 |
| D4 end 后 unshare 被拒 | 已确认守卫误拦清理操作；trace `bbf77274-9c73-4a78-b9d6-831c2dc32853`。 | 允许阶段结束后幂等清理本连接共享，仍禁止释放其他活动连接资源；原生连接关闭时不启动 helper。 |
| D5 宿主正常退出 | 根 sync、channels discover 的单次消息会产生短生命周期宿主；stdin EOF 正常退出。约 117 次/分钟、code=0 与此路径相符，不能当作崩溃证据。 | 默认不记录正常启动／退出；stderr、非零退出仍记录。`QUIETBRIDGE_NATIVE_HOST_DEBUG=1` 可显式开启完整生命周期诊断。未改传输架构或删除历史日志。 |
| D6 输入后 snapshot | click/fill/scroll 后重新观察是现有安全约束；普通导航已经支持新文档 screenshot/read/diagnostics 轻量复核。 | 保留规则，不把轻量导航复核扩大为任意未知输入的成功判断。 |
| D7 快照计数 | `elementCount` 为顶层 DOM 节点总数，`elements` 为可见交互元素，两者无需相等；旧回执缺少覆盖说明。 | 新增 coverage 数量、上限、范围、截断标记；发现第 301 个合格元素才算截断。tabs 缺 title/url 仍是元数据限制，可用当前 read/diagnostics 核对，未补入可能过期的标题。 |
| D8 旧快照错误码 | 此处是原生 task_guard 的快照检查误报；扩展旧 snapshotId 本来就报过期。 | 原生快照缺失或被新快照替换时返回 `STALE_SNAPSHOT`；真正应用／窗口不匹配仍返回 `TARGET_MISMATCH`，绑定不变。 |

另：fill 使用 CDP `Input.insertText`，不是报告所称的 CDP DOM 直接写入；滚动使用固定文档内 DOM 滚动并回读位置。

## 掉线与时长的证据边界

北京时间 **2026-11-09 20:40:30 至 2026-11-10 10:05:43** 的 bridge 操作记录中，核对到 241 条 `tool.started`、60 条 `browser.command_queued`，未发现 `BROWSER_HOST_UNAVAILABLE`。报告引用的 19:54／20:04 是此前故障轮次，不能证明 0.6.10 仍在 10–20 秒内掉线。

这也不等于已证明新版长期稳定：约 13 小时 25 分是墙钟跨度，含中断，不能当作连续运行时长。非零退出、新的崩溃报告仍须按具体时间、会话、toolTraceId/nativeRequestId 核对，不能拿历史日志累计次数当当前故障率。

## 租约与会话保护

- 界面阶段连续 120 秒无实际界面工具活动会触发 idle_timeout；权限／任务状态、ping、扩展心跳不续期。本轮三次 idle 中断均有记录。
- 阶段 end 请求释放共享与绑定；新增 `previewCleanup` 返回 `released`、`connection_closed`、`failed` 或 `unconfirmed`。只有原生回执 holding/visible 均为 false 才报告 released；失败保留错误码，不伪装成功。
- 已核实宿主归属的 host_run 保护可以跨阶段保留，最长允许 30 分钟无实际工具活动；FinalStop/SessionEnd、取消、断连或退出负责结束对应保护。未核实归属采用 120 秒兜底。
- `PreventUserIdleSystemSleep` 不阻止合盖睡眠。只看电源断言仍在，不能判断共享视频或预览仍在。
- 我此前给出的“两分钟完全无工具调用后原任务继续”预期与 120 秒回收冲突，应改为“预期回收，核实旧结果后显式 begin/bind 新阶段”，不为通过测试擅自延长 TTL。

## 验证结果

- Chromium Node 全套：**90 项通过**，覆盖 press/release 超时、点击前文档变化、无 mouseMoved、快照 300／301 边界、旧观察器替换及三个通道隔离。
- MCP Python 全套：**318 项通过**，覆盖重定向提示、原绑定恢复、旧快照、阶段后 unshare、关闭连接不启动 helper、清理失败如实返回、coverage 校验和宿主日志行为。
- macOS 打包／部署脚本：**47 项通过**；`git diff --check` 通过。
- 独立临时 profile 的真实 headless Chrome、本地 HTTP fixture：**连续 20 次点击全部收到 trusted click 事件，40 条按下／松开命令，0 条显式 mouseMoved**。没有使用用户 profile 或访问用户网站。
- 此 Chrome 验证使用 CDP 适配夹具，不是已安装扩展、真实后台／隐藏窗口及不抢焦点的端到端验收，不能宣称实机 click 已彻底修复。

## 安装后重点复测

1. 用户继续操作其他应用时，对专用后台标签连续点击至少 20 次，每次只发一次并观察；记录前台应用、物理鼠标和目标身份，确认无抢焦点、无闪烁、无重放。
2. 同一绑定重复导航，覆盖精确地址、尾斜杠、站内重定向、真实失败页；落地地址变化时按提示 snapshot 后继续，不为恢复改变 assignmentVersion。
3. 连续 35 分钟做有意义的观察／导航，每 30–45 秒有实际界面工具活动；另开独立用例空闲超过 120 秒，确认资源回收且旧任务不能继续输入，不靠状态查询虚假保活。
4. 桌面阶段 end 后核对 previewCleanup，再调用 unshare；共享应关闭，同一宿主会话保护可以保留。随后人工停止宿主会话，从外部核实该会话预览、捕获流、租约、保护释放；共用进程仍服务其他会话时不要求它退出。
5. 三会话并行与人工停止仍需独立实测，停止一项不得影响其他会话。正常 end 不能替代真实人工停止测试。
