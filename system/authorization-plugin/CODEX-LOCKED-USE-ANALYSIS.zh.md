# Codex Computer Use / Locked use 本机实现分析

核对日期：2026-11-05（Asia/Shanghai）。

结论：本机 Codex 安装工具将 `use-login-window-ui` 直接放在
`system.login.screensaver` 的顶层 `rule` 数组中。WorkBuddy 已回滚的实现则引用
remote 和 stock，再由 stock 间接引用它。这是与本次注销后锁屏界面回归最直接相关的差异。
下一版候选应首先保留顶层的原生 UI 标记，不能再以“保留 stock 副本”等同于保留原生 UI。
本分析未安装或运行 Codex 的授权安装器，也未重新启用 WorkBuddy 自动解锁。

## 证据范围

- 官方行为：[OpenAI Computer Use 文档](https://developers.openai.com/codex/app/computer-use/)。
- 本机桌面应用：`/Applications/ChatGPT.app`，版本 `26.1027.61615`。
- 本机服务：`~/.codex/computer-use/Codex Computer Use.app`，版本 `26.1022.1001641`，build `1001641`。
- 随应用分发的 `@oai/sky` JavaScript 包，版本 `0.7.13`。
- Swift / Objective-C 原生程序仅通过包结构、字符串、导入符号及局部反汇编核对，未获得完整源代码。

下文将可读 JS 和明确的机器码常量作为直接证据；Swift 类型与属性名只用于还原组件职责，
不据此宣称已经确认完整控制流、所有验签条件或精确超时值。

## 1. Computer Use 调用与应用授权

可读 JS 的入口是 `@oai/sky/.../src/sky.js`：在受信任的 Node REPL 中通过
`nodeRepl.rpc("sky", ...)` 进入宿主服务。macOS 客户端再连接本机原生 Computer Use 服务。

`targets/mac/computer-use-policy.js` 的 `withComputerUsePolicy` 明确执行：

1. 要求 `app` 是普通数据属性，将输入固定为不可变对象，避免确认前后换目标。
2. `getAppPolicy` 将请求解析为应用的 bundle identifier、路径、风险与组织策略结果。
3. 组织策略拒绝或禁止时终止；允许进入用户确认的目标才发起 `createElicitation`。
4. 根据策略允许保存本次会话或永久授权，确认后使用解析出的应用路径执行动作。

“组织策略允许”在这里不等于“用户已同意”。系统的辅助功能 / 屏幕录制权限也不等于应用授权。

`targets/mac/native-pipe.js` 显示当前 JS 路径使用带 4 字节小端长度前缀的 JSON-RPC，
最大帧为 8 MiB。默认端点位于 OpenAI 的 Group Containers 目录中的 `IPC/computeruse.sock`。
请求携带 API 版本、截止时间与 `codexTurnMetadata`；缺省元数据来自宿主提供的
`x-codex-turn-metadata`。本机原生包还包含 `SkyComputerUseClient` 和 XPC transport，
不能把所有版本与入口都描述成同一种传输。

服务中可见 `ComputerUseIPCSenderAuthorization`、`senderContextResolver`、
`prepareForAuthenticatedRequest`，以及 `untrusted_parent`、`relay_without_trusted_ancestor`
等失败原因。它另外检查调用进程与受信任进程链，不能只靠 JSON 中自报的线程 ID。
本轮未还原其完整接受列表。

## 2. Locked use 的任务约束与保护

官方文档确认：用户主动启用 Locked use 后安装 Apple Authorization Plug-in；
自动解锁限于活动、受信任的 Computer Use turn，授权窗口短暂且只针对当前尝试。
临时解锁时覆盖所有显示器；检测本地输入后重新锁屏，并暂停自动解锁直到手动解锁。

本机服务含以下对应组件与状态：

| 组件 | 可见证据与职责 |
| --- | --- |
| `LockScreenAutoUnlockCoordinator` | `activeThreadIDs`、`autoUnlockedThreadIDs`、`attemptsInCurrentLockedEpisode`、`maxAttemptsPerLockedEpisode` |
| `LockScreenLoginAuthorizationBroker` | 维护 `activeAttempt` |
| `LockScreenLoginAuthorizationAttempt` | `consumed`、`revoked`、`timedOut` 三种结束状态，以及等待完成的接口 |
| `SystemLockScreenController` | AX interactor、轮询 / 提交 / 验证延时、authorization broker factory |
| `SystemLockScreenMonitor` | 同时观察 locked / unlocked / lock UI shown 通知，并等待锁屏 UI 稳定 |
| `LockScreenGuardianCoordinator` | pending unlock、浮层 assertion、relock settle observation |
| `SystemLockScreenPhysicalInputMonitor` | 物理输入监控，含 ignored process ID |

协调器有 `suppressedUntilManualUnlock`、`waitingForRelock`、`waitingForManualUnlock` 状态。
服务错误也分别说明“无法自动解锁”“物理输入后暂停”“请求无法关联 ChatGPT thread”。
这些证据支持按任务约束解锁，并在本地输入后等待手动认证的设计。

它有每次锁屏 episode 的尝试上限，但本轮没有确定具体数值；不能将它描述成与
WorkBuddy 的“一次锁屏只提交一次 AX 请求”完全相同。

## 3. 独立 Guardian 与浮层交接

服务包内嵌独立的 `QuietBridgeLockScreenGuardian.app`。主服务可见：

- `beginUnlockGuard(threadID:connection:)`；
- Mach bootstrap rendezvous 与 XPC 连接建立；
- Guardian 连接结束导致受保护解锁失败的错误；
- `lockScreenGuardianDetectedPhysicalInput` 回调。

XPC 协议还暴露 `retainAutoUnlockedLeaseForThreadID:` 与
`releaseAutoUnlockedLeaseForThreadID:`。这说明保护与自动解锁按线程租约管理，
并有专门的守护进程生命周期。

浮层包含壁纸监控、头像缓存、屏幕观察，显示 “ChatGPT is Using Your Mac” 和
“Press any key or click to unlock”。原生服务的锁屏稳定监测监听
`com.apple.screenLockUIIsShown` 及 `com.apple.sessionagent.screenLockUIIsShown`，
同时监听 locked / unlocked。结合 `relockOverlaySettlingAssertion`，推断它会等
真实锁屏界面接管后再结束覆盖；本轮未还原其完整退出与故障处理分支。

## 4. 授权插件与身份核对

插件标识为 `com.openai.sky.QuietBridgeService.AuthorizationPlugin`，机制名为
`CodexComputerUseAuthorizationPlugin:allow`，该常量没有 `,privileged` 后缀。

插件机制连接固定 Unix socket：

```text
/tmp/com.openai.sky.QuietBridgeService/LockScreenLoginAuthorization.sock
```

导入符号和反汇编确认，它从 socket 取得对端 audit token，再调用
`SecTaskCreateWithAuditToken`、`SecTaskCopySigningIdentifier` 与
`SecTaskCopyTeamIdentifier`，比较预期的服务标识 `com.openai.sky.QuietBridgeService`
和 Team ID `2DC432GLL2`。没有找到依赖插件宿主调用 `SecCodeCheckValidity` 的路径。

该版本的插件连接后读取服务的授权结果，最终回调 Allow 或 Deny。活动任务、尝试消费和
撤销由服务中的 broker / coordinator 管理。WorkBuddy 的协议 3 nonce / 会话报文
是自己的实现，不能说是从此版本 Codex 插件照搬。

`QuietBridgeLockScreenGuardian_Parent.coderequirement` 与
`SkyComputerUseClient_Parent.coderequirement` 都指定 OpenAI Team ID。
这些资源表明它对父进程身份有要求，但单个资源不能证明运行时只检查 Team ID。

## 5. 安装器的关键差异：原生 UI 规则保持在顶层

本机安装器机器码可还原出以下核心结构（省略原始条目继承的其他字段）：

```text
system.login.screensaver:
    class: rule
    k-of-n: 1
    rule:
        - com.openai.sky.QuietBridgeService.AuthorizationPlugin.remote
        - use-login-window-ui
    tries: 1
    删除 mechanisms 字段

com.openai.sky.QuietBridgeService.AuthorizationPlugin.remote:
    class: evaluate-mechanisms
    mechanisms:
        - CodexComputerUseAuthorizationPlugin:allow
    tries: 1
    version: 1
```

取证位置（仅对上述 build 的 arm64 文件有效）：

- 安装器 `0x100004df4–0x100004fc4`：基于传入字典设置 class、k-of-n、rule、tries，并删除 mechanisms。
- `0x100004ec8–0x100004ed0`：初始化静态规则数组 `0x100010118`。
- 该数组 count=2；两个元素分别引用 remote 与 `use-login-window-ui` 字符串。
- `__TEXT,__cstring` 的 `0x100007b80` 为 remote，`0x100007bc0` 为 `use-login-window-ui`。
- `0x100004150–0x1000042f0`：构造 remote 机制规则。

我们已回滚的结构为：

```text
system.login.screensaver.rule = [WorkBuddy.remote, WorkBuddy.stock]
WorkBuddy.stock.rule = [use-login-window-ui]
```

本机 `/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow`
包含 `-[LWScreenLock readUseDefaultScreenLockUI]`，以及找不到 `use-login-window-ui`
时转用 SecurityAgent UI 的明确诊断字符串。由此结合用户注销后的实际复现，
最有依据的解释是：UI 选择没有通过 stock 间接识别原生规则。
登录会话中曾成功不等于新会话读取配置后仍会选择相同 UI；本轮未验证精确缓存时机。

因此此前认为 remote/stock 与 Codex 等价的判断不成立。需要直接保留顶层原生 UI 标记，
而不是继续改变 SecurityAgent 宿主、修签名或调整浮层来解决这个回归。

## 6. 对 WorkBuddy 的后续修改建议与验证门槛

1. 候选授权拓扑改为顶层 `[WorkBuddy.remote, use-login-window-ui]`；原始完整规则继续只作为恢复备份。
2. 离线测试断言顶层直接包含原生 UI 规则，防止再引入 stock 包装。
3. 在新登录会话先验证无任务时的全屏锁屏、密码与取消流程，再验证任务中自动解锁与 Guardian 交接。
4. 进一步核对任务结束、Guardian 断连、已消费 ALLOW 尚未落地和本地输入竞争，保持默认拒绝与手动认证抑制。

上述授权结构现已实现，离线测试覆盖顶层原生 UI 标记、事务恢复及卸载还原。
仅开放拒绝自动解锁的验证安装；自动解锁入口继续禁用，需在新登录会话确认原生全屏锁屏。
首次安装时发现本机 authorizationdb 丢弃顶层 `class=rule` 的 `tries` 字段，
安装器因严格读回校验自动恢复原始入口。修正版只在 remote 分支设置 `tries=1`，
保留其余字段的严格核验；14 项安装器及插件测试通过。
修正版拒绝包已安装并完成规则读回，`denyOnly=true`、`enabled=false`、事务 `committed`。
这只能证明部署和规则结构正确，注销后的锁屏样式仍待用户实测。

后续验证：2026-11-05 用户确认注销重新登录后的界面正常、密码解锁正常。
据此开放新授权结构的自动解锁安装，15 项安装器及插件测试通过。
随后用户确认新结构下自动解锁、浮层、本地输入接管和系统密码解锁完整测试通过。
helper 状态佐证：单次消费 `consumedCount=1`，交接结束为 `system-lock-handed-back`，
任务因用户接管暂停、浮层已移除、无待处理自动解锁或 Guardian 错误。
测试租约已释放，最终实时会话查询为已解锁；本次手动验证不证明零桌面暴露。

## 二进制指纹

- 安装工具 SHA-256：`e7fc5e1376de56863b54f8a3a3ca18e9c57b0039c1a8297dbfa501f4f759439b`。
- 授权插件 SHA-256：`9b537c0513f2089f00d4309315ae1ea0cb63f008c85cd2ed70517215595ff626`。

这些指纹用于限定分析适用版本，不代表已经通过真实锁屏测试。
