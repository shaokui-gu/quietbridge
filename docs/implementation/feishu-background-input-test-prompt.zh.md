# 飞书后台点击与填写的最小验证

目的：确认现有 QuietBridge 是否能对飞书桌面搜索界面完成后台点击和 AX 赋值；定位受限步骤，不验证发送，不读取聊天正文。可直接把下方提示词交给 WorkBuddy。

当前已发布 0.6.28（111）可以做输入能力基线测试；本轮新指引和预览防闪烁修正尚未打包，因此不能把该基线测试称为防闪烁验收。飞书用户接口授权不是这项原生测试的前提，不调用飞书 CLI/API。

```text
验证飞书桌面客户端的后台点击与 AX 填写能力，不发送任何消息。只用 QuietBridge，不使用飞书 CLI/API、AppleScript、System Events、CGEvent、Playwright、Chrome 扩展 fill，不重启应用、不追加系统授权。不得 activate/raise、切 Space、移动物理鼠标、使用 type_text/paste/key 或点击任何联系人/会话/发送按钮。搜索输入不是聊天草稿；发现目标是聊天编辑器立即停止该步骤。

先只读核对实际安装/运行版本、quietbridge_permissions(action=status) 和飞书进程/真实窗口。不要沿用历史 PID、windowId 或 snapshotId。若被其他会话占用或有权限阻塞，释放自己的资源并用 Agent 交互工具让用户选择处理后继续/保持暂停/结束，不换通道解决。

分别做两组：A 飞书窗口在当前 Space、WorkBuddy 保持前台；B 同一飞书窗口在另一 Space、WorkBuddy 保持前台。窗口与桌面的准备由用户手动完成，Agent 不移动窗口、不改变前台。通过交互工具说明准备要求并等待用户确认，记录工具返回的 onCurrentSpace/isOnScreen，未知就记未知，不能凭 isOnScreen=false 推断一定处于其他 Space。没有条件完成 B 时记未测。每组结束后先清理自己的阶段，再等待用户准备下一组。

每组只执行以下两项输入测试，正常约 1～2 分钟：

1. 后台点击。复用用户确认的飞书主窗口，begin(targetSurface=desktop_app) → bind(真实 appHandle/windowId,confirm=true) → share(source=window)，确认 streamReady 和 trackingWindowId。取最新 quietbridge_state(details=true)，定位明确标记的“搜索”入口，只点击一次。查询新状态及原进程窗口列表，以搜索界面确实展开或出现新的搜索窗口作为成功证据；dispatched=true/verified=false 自身不能判通过或失败。记录 route、focusPreserved 及前后 PID/键盘窗口字段。焦点改变或无法核实就暂停输入，不自动恢复焦点。点击后界面不变最多两次只读观察，不改坐标再次点击。

2. 后台填写。若搜索已在新系统窗口打开，先 end 主窗口阶段，再 begin(desktop_app) 并 bind 那个真实搜索窗口、share 和取新 state；不能把新窗口塞进原绑定。若点击没有展开搜索，记录点击项未通过，并询问用户是否手动打开一个空搜索框后独立测填写；用户未确认就不继续。这种人工准备必须写进报告，不能算 Agent 点击成功。

只选择上下文明确属于搜索功能、valueWritable=true 的输入控件，记录其 role、原始 value（保留零宽字符/换行转义）、snapshotId、elementIndex。原框里有用户已有查询或无法区分搜索框和聊天编辑器时，不覆盖，暂停询问。对空搜索框仅调用一次 quietbridge_set_value，写 QBTEST_20261113。随后约 0.5 秒和 1.5 秒分别取新 quietbridge_state(details=true)，独立记录实际值是否保持完整测试字符串。AX 写入返回 dispatched 只表示受理，不能当成功。若 AX 值已更新，另核实界面是否进入对应查询状态；以新的 AX 页面状态或用户观察预览为证，不用一个 AX 属性代替业务效果。看不到结果变化就记“值已写入，界面响应未验证”，不要推断 React/onChange 的内部原因。

填写前若曾调用 visual_state 或截图，必须重新取 state 后再使用元素索引，因为视觉观察可能更新快照。本轮默认不额外截图，不以截图促帧；预览闪烁只记现象，不为修显示重复 share/unshare 或重试输入。没有足够观察证据就写未验证。

写入后如测试字符串确实留在搜索框，只用新的同一搜索控件快照恢复记录的原值一次，并读回确认。恢复失败或目标变化就停止清理输入，告知用户手动清空，不用键盘/剪贴板兜底。不清理用户搜索历史、不关闭用户原有窗口。未写入成功且原值未变时无需再恢复。

最后每组 end+幂等 unshare，核对 holding=false/visible=false；captureStop 为 requested 或字段缺失时如实记停止未确认，不当作 completed。TASK_RESUMED 时按回执重新核实原窗口、共享与快照再继续未执行步骤，不重放触发恢复的旧输入。遇到真正能力阻塞先保留测试进度，再提供处理后继续/保持暂停/结束的选择；停止测试时正常释放自己的资源。

报告只需一张对照表，每组记录：实际版本；窗口身份与 Space/onScreen；点击是否使搜索展开及证据；set_value 是否受理；两次 AX 实际值；查询界面是否响应；焦点回执；原值恢复；最终共享释放。标明哪些步骤由用户准备，未经观察的项写未验证，不写“绝无数据变更”或“接收方已确认”等本轮没有证据的结论。不包含联系人姓名、聊天正文或 token。

结论必须限定在本轮控件与条件：A 通过、B 不通过表示当前实现对这两个条件存在差异，不能仅凭两次测试断言平台根因；两组赋值都不生效表示本控件的 AX 写入路径尚未证实可用；点击通过但填写未通过则分别报告。搜索框测试通过也不证明聊天编辑器、发送动作或所有 Electron 应用已支持。
```

验收报告建议：

| 项目 | A 当前 Space、WorkBuddy 前台 | B 另一 Space、WorkBuddy 前台 |
| --- | --- | --- |
| 版本与窗口身份 |  |  |
| Space / isOnScreen |  |  |
| 点击后搜索是否展开 |  |  |
| 搜索窗口是否人工准备 |  |  |
| AX 写入受理 |  |  |
| 0.5s / 1.5s 实际值 |  |  |
| 查询界面响应 |  |  |
| 焦点保持证据 |  |  |
| 恢复原值 / 共享释放 |  |  |
