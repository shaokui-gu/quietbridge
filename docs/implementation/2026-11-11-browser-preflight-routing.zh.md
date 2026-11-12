# 浏览器准备检查与私有通道启动互相等待

## 证据

2026-11-11，用户转交 Workbuddy 11:25–11:26 的调用：0.6.14 (97) 生效，原生 Chrome 扩展宿主仍在，但无任务 candidates=host_unavailable、permissions.browserChannel=false。Codex 同时读取根 browser-events.json，0.4.6 心跳持续新鲜、enabled/permission=true，与菜单应用有效 session 一致。进程存在本身不证明每条私有路径可用，也没有证据表明第三方抢占。

源代码与实际部署的 browser_events/session_registry/session_routing 均一致。调用通过 SessionRouter 到带 QUIETBRIDGE_SESSION_KEY 的 worker；browser_events.EVENT_FILE 因而解析到 sessions/<key>/browser-events.json。service-worker.js 从 session_registry.channels 发现私有通道，但后者仅返回 reserve 过且租约有效的任务槽。准备检查没有 begin，不存在活动任务槽，私有心跳不会刷新；旧任务留下的私有心跳也早已过期。

使用同一真实配置根、同一现有会话 key，在两个互相独立的只读 Python 进程内执行生产 browser_channel_state：已安装 0.6.14=False，修复源码=True。没有调用原生 helper、创建任务、发浏览器命令或重启进程。结果保存于 dist/off-space-preview-investigation-20261111/preflight-path-comparison.json。

## 修复边界

- permissions.browserChannel 读取根通道当前应用会话的心跳与授权。应用未运行、session 不匹配、心跳过期/来自未来、关闭或未授权均不通过。
- 没有活动任务的无 taskId candidates 仅向根通道请求候选元数据，返回真实 clientVersion、connectionScope=extension_preflight、taskChannelReady=false。只设置候选刷新标志，不剪裁其他任务记录、不分配标签、不占任务槽、不建立捕获。
- 有活动任务或显式 taskId 的请求仍使用私有通道，不将根候选当作私有 assign 证据。工厂显式将 task_file 旁的 event_file 传给执行/状态函数，避免可选任务路径与默认事件路径错位。
- 私有通道第一次 sync 最多等 3 秒；全局在线但私有尚无新握手时返回 channel_pending/BROWSER_CHANNEL_STARTING。恢复建议最多两次只读检查，持续异常再通知用户；不要求重载扩展、不重建任务、不重复 create_target。
- 已收到本次应用会话的私有握手、但权限被拒绝时，仍报告授权拒绝，不伪装成正在启动。取消/任务更换中止等待，不派发创建动作。

扩展仍是 0.4.6；本次只改 MCP 准备检查和恢复说明，不修改原生输入或捕获策略。跨 Space 画面问题仍待诊断，不能声称已修复。
