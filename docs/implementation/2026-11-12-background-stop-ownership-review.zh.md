# WorkBuddy 后台强停与窗口占用核查

## 结论

用户确实执行过后台任务强停。2026-11-12 21:23:19 的停止请求针对测试会话 `fac921b5-6c27-4e66-886c-4c9ec6bafe4d` 中的 Bash 后台任务 `nowJx7`，即 48764 端口的本地测试页服务；该进程已被杀掉。实际持有 Chrome 窗口租约的是另一会话 `21a8cc19-38cf-43aa-b659-3d0d8d44f057`，属于定时采集任务的一次补跑。本次没有发现“同一持有者已收到取消但仍然续租”的证据。

两会话的标题均为“浏览Linux.do”，但执行身份、请求、后台任务不同。停止本地测试页服务不会撤销另一个采集会话的窗口绑定。

## 时间线（Asia/Shanghai）

| 时间 | 证据 |
| --- | --- |
| 21:19:21.145 | WorkBuddy 启动定时任务 `97656550-0ff8-4196-9943-029e231a4345` 的运行 `97656550-0ff8-4196-9943-029e231a4345:1794489561145`。运行元数据 `runKind=missed`。 |
| 21:19:22.362 | 该运行关联采集会话 `21a8cc19…`；请求为 `01a1cfe9b502798a996c0467c65b8e77`。 |
| 21:20:10.041 | 测试会话 `fac921b5…` 创建 Bash 后台任务 `nowJx7`，命令为 `python3 -m http.server 48764 --bind 127.0.0.1 --directory /Users/gushaokui/Documents/AI/computer-use/tools/fixtures`。 |
| 21:20:53.925 | 采集会话调用 bind，绑定 Chrome 原生窗口 274431。 |
| 21:23:19.112 | 用户操作到达宿主：`wb:conversations:cancelBackgroundTask`，参数为测试会话 `fac921b5…`。 |
| 21:23:19.126–.127 | `nowJx7` 被标为 `status=killed`，`evidence=manual-stop`；cancelBackgroundTask 回执为 cancelled。 |
| 21:23:19.253 | 同一本地测试页服务收到 ProcessExit，`exitCode=137, killed=true`。 |
| 21:23:52.994 | 测试方绑定 274431 返回 TARGET_IN_USE；此时采集会话尚未结束。 |
| 21:25:58.836 | 采集会话改绑窗口 275157。 |
| 21:29:22.905 | 采集执行自然完成，宿主 `stopReason=completed`。 |
| 21:29:23.377 | QuietBridge 收到该采集会话的 FinalStop，原因为 `host_final_stop_completed`。 |
| 21:29:23.448 | QuietBridge 将采集任务标为 interrupted；距停止记录约 70 毫秒。 |
| 21:29:23.491 | 宿主释放此次自动化运行，`interrupted=false`。 |

## 来源与复核边界

- WorkBuddy daemon：`~/.workbuddy/logs/daemon.log`，本次读取时关键行 1025、1103、2752、2762、3574–3578。
- WorkBuddy 执行日志：`~/.workbuddy/logs/2026-11-12/linuxdo__c22e4ba8cfb86d4c4362b17b1d43c1a6.log`，关键行 18117、25699–25728、33559–33584。
- WorkBuddy 会话记录：`~/.workbuddy/projects/Users-gushaokui-Documents-AI-linuxdo/` 下两份对应 session JSONL，只提取工具名、身份、时间和必要命令。
- QuietBridge 操作日志：`~/Library/Logs/quietbridge/bridge-operations.jsonl`。
- 只读 SQLite 核对：`~/.workbuddy/workbuddy.db` 的 sessions、automations、automation_runs、automation_runtime_state。两会话均为后台自动化会话，标题相同；补跑与先前手动测试是不同运行。

会话列表的 `terminated` 不能单独用作用户取消证据：该采集会话从 21:19:31 的启动阶段列表中就显示这个值，而实际执行一直持续到 21:29。此次结论以明确的取消方法、Bash 退出记录、执行身份、FinalStop 及租约状态交叉确认。

核查时有效目标租约数量为 0。定时采集定义仍为 `ACTIVE`、`deleted_at=null`，计划下一次运行时间为 2026-11-13 01:29:23（以此次只读查询结果为准）。此状态不能说明用户没有删除操作；能确定的是，本次 21:23:19 强停对应本地测试页服务，且采集定义在查询时仍有效。

本次未停止或修改任何真实任务、调度或租约。已补充 Chrome 实验提示词：本地测试页服务延后到目标绑定成功后启动，记录本轮自建服务的后台任务 ID，成功或失败退出时都清理本轮自建服务；复用已有服务时不关闭它。
