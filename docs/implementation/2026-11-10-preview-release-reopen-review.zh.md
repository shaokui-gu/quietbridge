# STEP 20 释放后重开实验复核

用户追加了 19:38–19:41 的测试对话：希望判断共享连接是否造成离屏窗口纯色。本轮只读既有日志、相关 trace 和源码，未操作浏览器、创建捕获、切桌或关闭其他会话。

## 本机时间线

| 时间 CST | 原生日志/桥接回执 |
|---|---|
| 19:38:37.507 | 获取 preview 资源，client 2D92194A…；SCStream generation=480FE851… |
| 19:38:42.436 | 原任务 8039bc1b… 排入一次 navigate；19:38:51.537 排入 read |
| 19:38:54.441 | 第一条流最后一个进度样本：complete=249、submitted=248、背压=0、sampledContentChanges=4、layerFailed=false |
| 19:38:57.581 | task.phase_ended，previewCleanup.status=released、holding=false、streamReady=false。19:38:54 是调用附近时间，不能当作回执完成时间 |
| 19:39:15.958 | 新任务 begin 已返回；随后 bind / assign / mapping |
| 19:39:33.254 | 同一 native client 再获取 preview 资源；generation=B6282754…，证实新建了流 |
| 19:39:39.318 | 第二条流 complete=87、submitted=87、背压=0、layerFailed=false、panelVisible=true；targetOnCurrentSpace=false |
| 19:39:42.502 | 新任务 7341b91f… 的 host-lifecycle 标记 FinalStop completed |
| 19:39:42.553 | 原生资源 client-disconnected 撤销 |

原生日志中的显式资源 release 使用另一事件名，不能因筛选列表没有 resources_revoked 就认为首次 end 未释放；桥接 end 回执和新 acquire 已表明状态发生切换。

从 end 回执到新 acquire 约 35.67 秒，用户提到的 10 秒是其中纯等待的一段，其余时间还包含新任务准备。两次共享使用同一 helper client 和 broker PID 98745，只更换任务与捕获流；这是重建共享，不是断开全部 MCP、native host 或浏览器调试连接。

用户只提供到分钟的纯色反馈，不能定位在第二条流实际活动的约 9 秒内还是 FinalStop 之后。Workbuddy 随后的“holding=True 等你收尾”也已过时：本机 19:39:42 就撤销了该资源。不得因此猜测另有资源残留或强制清理其他会话。

## 结论的有效范围

如果用户看到纯色的时刻确在 19:39:33–19:39:42，共享重建没有稳定恢复图像这一点成立，且两条流末次采样均无背压丢帧/显示层失败。

这不能否定“捕获参与问题”的假设：

1. 共享关闭期间没有同一 Space 条件下的源画面对照，无法证明源窗口是否恢复过。
2. 重新建立捕获本身可能再次触发问题；重开后纯色与“窗口一直纯色”两种解释都符合现有记录。
3. 重新打开后仍复用 helper 连接，未测试全部通道断开；不能把释放 preview 等同于所有连接离线。
4. 源码 SpacePreview.resetCapture 用 `Task { try? await stream.stopCapture() }` 异步请求停止，随即清空 stream/计数/显示层。released 回执没有等到 stopCapture 完成，且错误被忽略；现有日志不能证明停止 API 已成功完成。缺少完成回执是可观测性缺口，不是证明旧流泄漏。
5. 导航后的 DOM complete 不证明可视表面已绘制；正确的扩展截图也可能主动触发一次合成。两者都不能完全排除 Chrome 原先未绘制正文。

因此“假设不成立”“与连接无关”“加长到 60 秒仍纯色就能定性”均超出现有证据。“关闭期间手动切桌看一眼”也不是严格分离方法，因为切桌本身就是已知可能恢复绘制的变量。

## 后续最小取证

优先补齐 capture start/stop 请求、完成、失败和旧/新 stream generation 的生命周期证据；停止完成前不得把 released 解释为捕获已确认退出。该状态观察应保持异步，不为等待停止阻塞用户的立即关面板操作。

随后在专用固定页、同一活动任务里，拿到首次扩展截图之前的窗口源帧证据：正文区域的像素格式、尺寸/stride、颜色元数据、局部纯色/变化统计，必要时由用户授权保存一帧；对照显示层画面。原始源帧也为纯色则沿 Chrome 可视表面/WindowServer 捕获查，源帧正文正常而预览纯色则沿像素转换/显示层查。源帧取证和扩展截图的顺序必须明确，不能截图后倒推截图前的画面。

用户交互可优先评估 Workbuddy 的 AskUserQuestion 是否真正阻塞当前运行而不发送 FinalStop；仍须从日志核验，不假定其保活。人工回答应在有界租约内；任务中断后不拿下一轮反馈当作原流状态，不取消现有停止清理策略。

本轮未修改生产捕获策略或新增截图，未把本次对照当成根因定案。
