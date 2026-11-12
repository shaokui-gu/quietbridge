# 浏览器扩展与原生窗口的操作选路

日期：2026-11-10。本轮只处理现有 Chrome 扩展与原生窗口两条通道。

## 选路依据

按“要操作什么控件”选择，不按“它属于 Chrome”一概选择扩展或原生。Agent 在 `quietbridge_task begin` 声明 `targetSurface`，桥接选定现有执行模式，并在回执中返回 `routing`。原生声明是操作意图，不证明控件已被观察、能够后台操作或能覆盖任意浏览器功能。

| 操作对象 | `targetSurface` | 模式与操作工具 |
| --- | --- | --- |
| 网页正文、链接、表单、导航、网页滚动和页面按键 | `web_content` | `background_browser`，`quietbridge_browser_events` |
| 地址栏、浏览器工具栏等非网页控件 | `browser_ui` | `desktop`，已有原生观察／窗口输入工具 |
| 真实原生打开、保存等对话框 | `native_dialog` | `desktop`，已有原生观察／窗口输入工具 |
| 普通应用 | `desktop_app` | `desktop`，已有原生工具 |

浏览器设置页、跨域 iframe、网页 JavaScript 模态框、网页登录页与验证码，不因为扩展未覆盖或未返回元素就变成原生界面。不能为绕过站点、调试或权限限制使用桌面操作。网页导航用扩展 `navigate`，不通过地址栏和快捷键替代。

## 派发与前提

- 扩展：应用和扩展可用、站点授权、任务已绑定真实窗口、候选标签分配有效；网页输入使用原标签、文档和快照引用。共享前先 verify_mapping；原生预览状态与页面命令状态分开核实，不把命令可执行当成预览正常。
- 原生：核实真实应用进程与窗口、绑定该窗口、共享首帧已就绪、同目标最新观察证据，再按已有 AX／窗口定向投递能力操作。不能把目标换成当前前台窗口或通过激活促成操作。
- `browser_ui` 不开放全局菜单、应用级键盘、粘贴、激活或 AXRaise。原生接口不支持某个控件时报告限制；例如无法后台完成地址栏提交，就不能用 Command+L/Return 绕过。
- `native_dialog` 必须是已观察到的原生界面，窗口／sheet 归属应按实际原生结果绑定。无法可靠定位、共享或后台投递时由用户操作；TCC、安全输入等限制继续有效。
- 后台页面或预览不更新时，不能激活、置前、切 Space 或假定“收到帧”等于页面已绘制；报告画面证据不足。
- 同一 Chrome 进程的原生阶段仍按 PID 互斥。与其他会话冲突时返回 TARGET_IN_USE，等待而非结束他人任务或强行操作工具栏。

## 两种“换通道”的区别

合法的是实际工作流跨越不同界面，典型例子是网页动作后产生原生保存框。先核实上一动作的结果，保存原标签身份，结束自己的网页阶段，再为真实原生窗口建立新阶段；完成后重新开始网页阶段，核实原标签和窗口、重新 assign／verify_mapping、获取新观察。快照和坐标不跨阶段复用。阶段结束只释放本阶段资源，宿主会话保护保持原生命周期。

不合法的是扩展断开、权限拒绝、受限页面、缺少 ref、超时或执行结果未知后，结束网页阶段并重新声明 desktop，以原生点击继续同一个网页动作。这属于故障绕过，不是界面切换。应通知用户恢复原能力，先只读观察或查询旧命令，不重复输入。

## API 与兼容

```json
{"action":"begin","targetSurface":"web_content"}
```

```json
{"action":"begin","targetSurface":"native_dialog"}
```

`interactionMode` 可省略；显式提供时必须与 `targetSurface` 一致。冲突在创建或替换任务前拒绝。`bind/status/end` 不接受模式或操作对象变更。

`routing` 包括 channel、targetSurface、scopeSource、focusPolicy、automaticFallbackAllowed、requiresWindowShare、requiresTargetEvidence 和 extensionTabRequired。它是选路及前提说明，不是已授权、已连接、已共享或动作成功的证明。旧调用不填 `targetSurface` 时兼容原模式与持久记录格式；旧任务缺失模式时仍按后台网页处理。

显式原生对象的任务在桥接层拒绝扩展 assign、command、create_target、select_tab、verify_mapping、tabs 和 annotate；允许 status/candidates/result/cancel/annotations 用于检查和收尾。网页模式继续在桥接层禁止原生输入。桥接不能仅凭 `targetSurface` 文本证明原生坐标落点确实是浏览器工具栏；Agent 仍须使用最新观察和目标核验，不能据声明声称操作具备焦点隔离。

## 验收

隔离测试覆盖操作对象自动选路、模式冲突不破坏活动任务、非法对象无派发、对象不可中途更改、网页拒绝原生输入、显式原生阶段拒绝扩展操作且允许结果查询／取消、原生阶段不开放应用级键盘／激活、共享未就绪不点击、阶段生命周期中选路一致，以及旧调用兼容。实际浏览器工具栏和原生 sheet 的后台行为仍需安装后人工验收，不能用模拟回执宣称全部可用。
