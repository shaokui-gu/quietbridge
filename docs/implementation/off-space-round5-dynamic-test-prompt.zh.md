# Round 5：先改变页面，再比较layout/capture

适用已安装QuietBridge 0.6.16（99）/扩展0.4.7；无需重装。下面整段交给Workbuddy。

```text
上一轮静态STEP33始终可见且无导航，不能用于判断页面更新故障。本轮先触发已知变化，再观察布局查询和一次截图的影响。

全程不抢焦点、不激活/raise、不切Space、不动物理鼠标，不用原生网页输入，不改Chrome flags、权限或连接器，不重启应用。只使用QuietBridge扩展。不要接管旧任务/其他会话。

准备阶段只读核验应用0.6.16（99）、实际扩展握手0.4.7、权限和本地服务可用。重新核实专用STEP33页的真实候选、窗口、PID代际；上轮窗口207258、标签57074808仅是线索。目标不在时报告，不自己换成用户标签或创建新窗口。先结束准备回复，让我手动把这个专用窗口放到其他Space，再等我说“场景已准备，开始”。准备阶段不共享。

开始后创建自己的web_content任务，用私有候选完成bind/assign/verify_mapping，status核验capabilities含render_probe及目标为该窗口活动标签。未知绑定/连接/权限/归属立即报告并收尾，不换工具。记录taskId、所有目标ID及时间。发share之前记录计时起点，share source=window并记录streamGeneration。本轮同一任务、同一流，不重建、不额外拍图。75秒必须开始收尾，包含工具等待总计不超过90秒；时间不足优先跳过后续阶段，不能为了做完而延长。

共享后不重复五个探针阶段，只做以下序列。每条命令单次派发并查询原commandId，未知/超时不重放；result最多两次有间隔复查。不得用quietbridge_wait_ready计时，也不用visual_state、screenshot、系统截图、Mission Control或其它UI工具。单纯等待可用本地计时工具。

A. navigate一次到 http://127.0.0.1:48763/probe?step=34；用read正常核实STEP34、URL、readyState、epoch，解除导航复核。发进度“观察A：DOM已变为34”，约5秒。我会自行记录预览是34、旧33还是无文字色块，不等我中途回答。若DOM尚未就绪则只读核查原命令，不重新导航。
B. render_probe stage=layout一次，记录开始/完成Unix时间与视口；发“观察B：仅查询布局”，约5秒。
C. 在layout完成30秒内render_probe stage=capture一次，记录开始/完成Unix时间及captureDataLength；发“观察C：单次截图”，约5秒。失败、expired或unknown就停止后续并收尾，不补拍、不重新layout重试。
D. 只有距share计时起点尚未满60秒时，navigate一次到 http://127.0.0.1:48763/probe?step=35，再read核实STEP35/URL/epoch，发“观察D：后续导航35”，约5秒。时间不足跳过，不延时。本阶段不截图。navigate自身有调试/弹窗保护前置，不把D结果归因于单个CDP调用。

全程只发进度消息，不发最终回复等待我；FinalStop会清理共享。结束自己的任务，记录holding=false、previewCleanup、captureStop的流ID/状态；requested只是异步待完成，Codex会核日志，不能强制清理别的会话。保留专用标签，不改变锁屏保护。

最后输出简短时序表和全部commandId，向我要A/B/C/D人工观察。DOM正确不等于画面正确。若A就能正常显示34，报告“本轮未复现动态更新失败”，不能声称截图修复。若C正常但D再次滞留，则支持一次采样促成呈现的方向，仍不是单次因果证明。
```
