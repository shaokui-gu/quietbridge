# QuietBridge 锁屏集成：第二版实验实现

当前签名 ID 为 `com.quietbridge.AuthorizationPlugin`，仅信任同 Team 的
`com.quietbridge.helper`。历史 mechanism、安装路径和 socket 保持兼容。
旧版用户通过应用管理入口更新插件后须注销并重新登录；升级保留原始规则备份。
该二进制不请求受限 entitlement。可用 `PROVISIONING_PROFILE` 指定匹配新 ID 的
描述文件，构建时核验后嵌入；不使用绑定旧 ID 的文件。

当前代码在任务期间观察用户触发的系统锁屏，通过 Authorization Plug-in 申请一次短时解锁，
确认解锁后显示自有隐私浮层；用户再次输入时停止自动操作，交回真正的系统锁屏。
不收集或保存密码。

2026-11-05 用户已验证 deny-only 版本保留正常全屏锁屏及密码解锁。
随后已切换至自动解锁插件（`denyOnly=false`、`enabled=true`），并部署新版 helper
与桥接程序；权限及插件可用性检查通过，用户确认当前登录会话内完整自动解锁与浮层交接测试通过。
随后用户确认：注销并重新登录后，锁屏变成黑屏与密码弹窗。当前会话的成功不能证明
原生锁屏样式得到保留，remote/stock 授权组合未满足需求。随后已恢复原始规则并卸载旧组合。
根据本机 Codex 安装器与 loginwindow 的静态证据，新候选将 `use-login-window-ui`
直接保留在顶层。用户于 2026-11-05 确认注销重新登录后的界面及密码解锁正常，
已开放显式 `--experimental` 自动解锁安装；旧 remote/stock 结构继续禁用。
新结构拒绝版已于 2026-11-05 安装，事务状态为 `committed`，读回确认顶层原生 UI 标记存在；
新登录会话验证已通过；恢复自动解锁后的安装器与插件测试共 15 项通过。
用户随后确认新结构下完整流程通过：系统锁屏后自动解锁并显示插件浮层，
本地输入后交回系统全屏锁屏，密码解锁正常。helper 状态记录 `consumedCount=1`、
`lastTransition=system-lock-handed-back`、`pausedByUser=true`、`automationPaused=true`，
无待处理自动解锁、无可见浮层、无 Guardian 错误。测试租约已释放，最终实时检查
`sessionLocked=false`，隐私状态为 `inactive`。本次为单机手动验证，不代表零桌面暴露保证。
验证包签名配置 `CUADenyOnly=true`，机制跳过授权服务并返回 Deny；安装记录
`denyOnly=true`、`enabled=false`，不会启用自动解锁。构建使用
`make -B BUILD_DIR=build-deny-only DENY_ONLY=true`，安装使用
`install.py install --experimental --deny-only --bundle <验证包路径>`（需要管理员权限）。
安装器拒绝签名包配置与请求模式不一致的安装。
恢复原始规则并卸载：
`sudo /usr/bin/python3 -B "/Library/Application Support/WorkBuddyCUAAuthorizationPlugin/recovery.py" uninstall`。
下方带日期的本机诊断均为历史记录，其中的成功、失败、版本号和“尚未实现”仅描述当时版本，
不能作为本次交接流程的实测结论。

## 2026-11-07 的职责修正

锁屏授权组件仅处理自动解锁授权与手动认证的区分，不管控业务任务。上方历史记录中的 `pausedByUser`、`automationPaused` 及“需结束旧任务恢复”的行为已修正：手动解锁后原任务和绑定继续可用，先重新观察目标与共享状态。`manualSuppression` 只阻止自动解锁票据；解锁监测和普通工具不依赖授权 socket 成功。已消费 ALLOW 的交接约束保留。

## 授权结构

第二版不把 `use-login-window-ui` 放入 mechanisms。安装器仅接受未定制的原始规则，
生成两个独立的数据库条目：

| 条目 | 内容 |
| --- | --- |
| `system.login.screensaver` | `class=rule`、`k-of-n=1`，直接引用 remote 和 `use-login-window-ui` |
| `com.workbuddy.cua.AuthorizationPlugin.remote` | `class=evaluate-mechanisms`，仅含插件 mechanism，`shared=false` |

原始系统规则仅存入恢复备份，不再创建 stock 分支。`tries=1` 仅用于 remote；
本机 authorizationdb 不持久化 `class=rule` 的 `tries`，安装器按实际可持久化字段严格读回核验。
旧的 remote/stock 间接引用组合导致注销后锁屏样式回归，不可继续安装。
本机 loginwindow 的 `-[LWScreenLock readUseDefaultScreenLockUI]` 包含读取 `rule`、
检查 `use-login-window-ui` 的路径，并有明确日志字符串说明：找不到该规则时使用
SecurityAgent UI。结合本次顶层改为 remote/stock 后的复现，推断顶层 UI 选择没有按预期
递归识别 stock 中的原始规则；具体读取与缓存时机尚未验证。不能把“授权能回退”当成
“原生全屏锁屏界面能保留”。

## 通信与会话绑定

插件按前台控制台用户连接 `/tmp/com.workbuddy.cua.<uid>/LoginAuthorization.sock`。
目录为 0711，socket 为 0666，使匿名系统授权宿主能够到达端点；连接权限本身不授予授权。
服务端使用独占文件锁避免多个 helper 互相替换 socket。

请求为 `AUTHORIZE 3 <uid> <session> <32位随机十六进制nonce>\n`，成功回复为
`ALLOW 3 <相同nonce>\n`。报文有长度和时间限制，处理拆包，使用一次性短时票据。
本次未改变协议 3。当前 remote 机制为 `WorkBuddyCUAAuthorizationPlugin:allow`，不带 `privileged`。

- 插件通过内核 audit token 核对服务端 UID，并用 SecTask 读取 helper 的签名 identifier 与 Team ID；
  机制宿主不再调用 trustd 验证 helper 的信任链。
- helper 通过内核 audit token 校验 Apple SecurityAgent/authorizationhost 的代码有效性，
  并核对请求会话与内核对端会话；票据绑定 helper 验证过的当前控制台图形会话。
- AuthorizationCallbacks.GetSessionId 返回不透明指针，不能强制转换成数字会话编号。
  这里使用 AuthSession API 获得编号，并要求与内核会话匹配。
- 普通授权宿主可能使用独立匿名会话：同控制台 UID 可通过身份检查。`_securityagent` 或 root 宿主必须属于控制台图形会话。审计用户只接受控制台 UID 或未设置值；这些检查仍须同时满足 Apple 宿主验签。
- Apple 宿主验签不能证明它加载了哪个插件，但避免了旧版把宿主可执行文件误认为插件 bundle 的问题。
- 安装记录须由 root 持有、不可被其他用户写入，并显式启用第二版实验协议；插件自身也须通过签名验证。

## 独立隐私守护

任务 `bind` / `hold` 仅进入 `watching`，建立任务租约并观察系统会话；此时不盖屏，也不启动 guardian。
首次观察到用户触发的系统锁屏后，helper 才准备主进程覆盖层，并以同一签名可执行文件的
`--privacy-guardian` 模式启动独立守护进程。两者通过私有继承管道通信；guardian 建立所有显示器的
额外覆盖及输入监听并报告就绪后，helper 才能签发一次最长 12 秒的解锁票据。
自动授权还受最长一秒的保护租约限制，过期、断线或监听失效均停止放行。

AX 流程可以轮询读取尚未准备好的登录界面，但每次自动解锁流程只提交一次非机密探针串，
不读取或输入真实密码。确认系统已经自动解锁后，自有浮层进入可接管状态；触发首次系统锁屏的
修饰键松开不算新的用户输入。之后的新按键、鼠标点击或滚轮输入会立即撤销票据、取消后续 AX
动作并暂停任务，再请求真实系统锁屏。

只有确认真实系统锁屏，且不存在已消费但尚未实际解锁的 ALLOW，才撤掉主进程与 guardian 两层覆盖。
已提交给 authd 的 ALLOW 无法撤回：如果接管发生在它实际解锁之前，需等待该次解锁落地后再次锁屏，
不能把旧锁屏或首次自动解锁当作手动交接完成。锁屏状态未知或请求失败时继续覆盖。
已经提交给系统的 AX 调用也不能强制撤回，取消检查只阻止后续动作。

用户完成手动认证后，当前任务仍保持暂停，不自动恢复输入或浮层，直到显式结束该任务。
显式 `release` 解除任务暂停并撤销任务授权，随后才可选择窗口并显式 `bind` 建立新任务。
手动认证要求独立保留：`release` 和新任务都不能清除尚未完成的手动认证抑制或未落地的 ALLOW；
只有确认真正锁屏后由用户完成认证，才恢复普通窗口选择。
任务结束或租约到期时，若已进入覆盖阶段，同样先交还真实系统锁屏再释放覆盖；
尚处于 `watching` 时直接结束。主进程无响应时，guardian 等待保护租约到期后请求锁屏。

物理输入暂以 HID 事件来源识别；安全输入、锁屏期间的事件可见性、显示器切换、
进程异常退出及遮罩是否出现间隙均待真实验证。此实现不能保证系统级原子覆盖或零桌面暴露。

## 构建与安装边界

历史安装记录中的构建产物为 helper 0.4.3（23）、Authorization Plug-in 0.3.2（7），不代表本次变更已部署。签名配置使用本机
`Developer ID Application: shaokui gu (62RNP54C47)`，Team ID 为 `62RNP54C47`。

```bash
cd system/authorization-plugin
make
python3 install.py status
python3 install.py plan --output /绝对路径/新的候选目录
```

普通 `install` 仍会在任何系统操作前拒绝。第二版仅提供显式实验安装入口：

```bash
sudo python3 install.py install --experimental
```

`install.command` 通过 `prepare_install.py` 将签名组件暂存到临时目录，再申请管理员认证，发布系统可读的 helper、安装授权插件，并调用项目根安装器更新 WorkBuddy 配置。
helper 代码发布在 `/Library/Application Support/WorkBuddyCUAHelper/releases/`，以便 `_securityagent` 能读取签名；不发布用户数据或私钥。授权插件卸载不会移除这个基础 helper。

安装器先独占保存原规则和事务记录，再安装已验签 bundle，先写两个依赖分支，
最后修改系统入口。每次写入均读回核对；只接受已观察到的精确 Apple 创建者标识与签名要求；其他无法解释的规范化均视为失败。
`class=rule` 入口不写入本机数据库不保留的 shared/tries 字段，remote 分支仍为 shared=false。
失败时先停用授权并恢复系统入口，保留 bundle、原规则及事务记录供恢复，
不会在系统可能仍引用插件时删掉 bundle。重复安装不会覆盖备份。

卸载会检查第二版规则是否已被其他进程修改，确认恢复原入口后才移除分支和 bundle：

```bash
sudo python3 install.py uninstall
```

## 本机恢复

用户已确认 SSH 可登录；验证前还需在 SSH 会话中确认 sudo 可用并保留连接。
新增管理员账户共用同一授权规则，不能单独保证恢复。

若正常卸载因规则冲突而停止，可人工审查备份后从 SSH 恢复系统入口：

```bash
sudo sh -c 'security authorizationdb write system.login.screensaver < "/Library/Application Support/WorkBuddyCUAAuthorizationPlugin/system.login.screensaver.original.plist"'
security authorizationdb read system.login.screensaver
```

这条手工命令仅恢复系统入口。必须核对实际读回内容；它不会自动清理插件分支、bundle 或事务记录。

## 历史诊断：2026-11-02 本机验证记录

以下记录保留当时的诊断结论，不代表 2026-11-05 交接流程的验证结果。

- 系统密码回落：用户确认可以正常解锁。
- 非锁屏通信：修复 Darwin 接入 socket 继承 O_NONBLOCK 导致的提前断连后，连续五次双向验签与请求响应通过，均拒绝授权且消费次数为零。
- 真实锁屏：自动解锁未成功；锁屏宿主验证 helper 返回 `CSSMERR_TP_NOT_TRUSTED`（-2147409622）。不能将普通用户环境的验签成功当作锁屏宿主信任成功。
- 物理输入触发真实锁屏已观察到；完整自动解锁、遮罩连续覆盖和 WorkBuddy 任务继续执行未验证。
- 本次诊断授权已释放。普通通信通过不代表自动解锁可用，也不代表无需触发系统认证即可唤起插件。

本机随后将签名链中的 Apple WWDR G3 公共中间证书补入系统钥匙串（SHA-256：`DCF21878C77F4198E4B4614F03D696D89C66C66008D4244E1B99161AAC91601F`）。没有导出私钥或设置额外信任覆盖；此项不随授权插件卸载而移除。是否解决锁屏宿主信任问题仍需以实测结果为准。

补齐中间证书后的真实锁屏复测仍返回相同的 `CSSMERR_TP_NOT_TRUSTED`，自动授权消费次数为零。中间证书缺失不是完整根因。诊断脚本现会在没有自动授权的情况下观察到解锁时立即释放任务，避免手动认证后继续遮罩造成重复锁屏。

## 历史诊断：2026-11-03 签名错误诊断准备

插件 0.3.2（6）使用 `SecCodeCheckValidityWithErrors` 代替无详细错误对象的调用，
保留原有签名要求与拒绝条件。错误日志区分代码对象查询、要求编译、动态有效性验证和签名信息读取，
记录有界的错误说明、最多四层底层错误，以及架构、路径或要求语法等指定字段。
不输出整个错误字典、证书内容、audit token 或授权上下文。

当前没有增加 App ID 能力、App Groups、Keychain Sharing 或其他签名权限，也没有改动授权规则。
更换 Bundle ID 不能证明签名信任问题已被排除；如后续切换 ID 或团队，须同步检查插件的 helper 身份要求、
helper 身份、安装器中的签名检查，以及实际需要的描述文件。

本轮诊断版已编译和验签；尚未据此完成新的真实锁屏验证，自动解锁仍未验证成功。

### 历史诊断：2026-11-03 签名切换

两端嵌入对应的 Developer ID 描述文件，证书 SHA-1 为
`7CA7D67E84FB6EA56483B9829440FDD8EC90D27D`。Bundle ID 不变；两端验签固定的 Team ID 同步更新。
本次仅完成构建、严格验签、部署和连接握手，未重新锁屏测试，也未完成公证。
此前真实锁屏诊断中，授权宿主报 `Failed to talk to trustd after 4 attempts`，
随后动态验签返回 `CSSMERR_TP_NOT_TRUSTED`（-2147409622）。更换签名是否解决该宿主环境问题尚未验证。

新 Developer ID 签名真实锁屏复测（2026-11-03）：五次未锁屏传输探针通过，真实 `_securityagent` 宿主（EUID 92）仍报无法连接 trustd，动态验签返回 -2147409622，自动票据消费为 0，观察到密码回退解锁。测试中亦出现人工认证抑制状态，因此本轮不能验证完整自动流程；但已复现相同验签阻塞。安装记录的 Team ID 已同步修正。证据见 output/locked-use-v2-test/developerid-signature-20261103.log 和 developerid-cycle-20261103.json。

### 历史诊断：2026-11-03 特权宿主迁移

Apple 官方文档支持通过 `,privileged` 将无界面的机制加载到 root 授权宿主。
Security 开源 `OSX/sec/ipc/client.c` 的 `securityd_in_system_context` 和 `trustd_connection`
区分用户级 trustd.agent 与系统级 trustd。本机日志确认普通锁屏宿主请求
`com.apple.trustd.agent` 时查找失败；迁移后 root 宿主请求 `com.apple.trustd` 并通过验签。

当时 remote 机制为 `WorkBuddyCUAAuthorizationPlugin:allow,privileged`。
helper 仅接受 Apple 签名、精确标识为 authorizationhost 或 authorizationhosthelper.arm64、
并属于控制台 audit session 的 root 对端。其他任务、保护状态、票据和人工抑制限制继续生效。
迁移工具 `migrate_privileged.py` 备份安装状态和规则，仅修改 remote 分支，失败时回滚；
主规则和 stock 密码分支保持原样。

14 项 Swift 授权状态/范围测试和 7 项安装策略测试通过。
真实锁屏复测：5 次传输探针通过；真实授权宿主完成双向验签并收到 DENY；
未再出现 -2147409622。自动票据消费仍为 0，插件调用前已进入人工认证抑制，
最终通过系统密码回退解锁。不能据此声称完整自动解锁流程已实现。
证据：`output/locked-use-v2-test/privileged-cycle-20261103.json` 和 `privileged-host-20261103.log`。

官方资料：
- https://developer.apple.com/documentation/security/extending-authorization-services-with-plug-ins
- https://github.com/apple-oss-distributions/Security/blob/main/OSX/sec/ipc/client.c

### 历史诊断：2026-11-03 无输入时序观察

`no-input-cycle-20261103.json`：锁屏前 5 次探针通过；锁屏后持续观察约 20 秒，
保护状态为 true、人工认证抑制为 false，但 clientCount 一直保持 5、consumedCount 为 0。
12 秒票据自然到期，无自动解锁。当前模拟 Shift 仅可视为唤醒尝试，不能再描述为
已能触发登录窗口授权。此前人工输入后才出现插件请求，并不能证明插件能自主启动解锁。

Apple 开源锁屏 QA 将流程描述为 loginwindow 收集凭据、Authorization 评估凭据。
单独调用 AuthorizationCopyRights 即使得到 Allow，也不能据此认定 loginwindow 或用户 keybag 已解锁。
来源：https://github.com/apple-oss-distributions/Security/blob/main/OSX/authd/QA/features/macos/screen-saver.feature

2026-11-03追加Return诊断：build24将单次唤醒键改为Return；最终无输入复测中保护持续有效、人工抑制为false，但锁屏后20秒无新增插件请求、票据自然过期。已恢复build23。当前Shift和本次Return实现均未证明能自主启动loginwindow认证。原因可能涉及事件过滤、UI时序或空提交处理，尚未区分；完整无人值守解锁仍未实现。

2026-11-03延时提交诊断：build25在唤醒两秒后、票据与保护有效且无人接管时尝试Return。无输入观察20秒仍无新增插件请求，未自动解锁。已恢复build23并撤回实验延时提交代码。日志仅在诊断结束后的手动提交时出现loginPressed/checkAuth；尚未验证能自主启动loginwindow认证的接口。详见output/locked-use-v2-test/authorization-trigger-analysis-20261103.md。
