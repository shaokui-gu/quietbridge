# 跨 Space 诊断的前置阻塞：Chrome 进程路径

用户本轮报告时间为 2026-11-10 17:07–17:09 CST；本机只读复核约 17:27–17:30。未创建任务、捕获画面、重启应用或切换桌面。

## 已核实

- Chrome PID 97718 于 2026-11-09 14:11:13 启动。系统 uptime 不是这个进程的运行时长。
- NSRunningApplication 对该 PID 提供 bundleIdentifier=com.google.Chrome、bundlePath=/Applications/Google Chrome.app、executablePath=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome。
- proc_pidpath 对同一 PID 提供系统临时目录 `X/com.google.Chrome.code_sign_clone/code_sign_clone.…/Google Chrome.app.bundle/Contents/MacOS/Google Chrome`。两个路径经 resolvingSymlinksInPath 后依旧不同。
- ApplicationAuthorization.describe 比较上述两个路径字符串，不比较版本、可执行文件哈希或动态代码签名。该分支确实会抛出用户看到的 `STALE_APP: Application executable changed during selection`。
- BrowserSession.verifyChrome 另外要求 proc_pidpath 精确等于固定安装路径。仅修复应用句柄获取后，浏览器绑定仍会被此处拒绝。
- 17:07:24（1794301644）native/bridge 日志关联 nativeRequestId=mcp-50247-6、toolTraceId=4688a674-4277-4f35-9ede-df46ef3c9701：listWindows 在执行 worker 和 broker 均返回 STALE_APP，工具标记 not_dispatched。该记录证明窗口查询也受阻；当前常驻日志未保留用户所述全部三次 get 的错误正文，不声称逐一印证。

这足以确认当前选择失败来自目录路径与进程路径不一致。Chrome 原地升级是合理背景，但 Last Version 文件和磁盘版本不同，不能单独证明当前进程精确版本或正在映射的全部代码内容。路径带 code_sign_clone 也不是签名可信的充分证据。

## 两个 QuietBridge 目录

本机安装副本及当前用户 release 为 com.quietbridge.app 0.6.12（95）；两个菜单栏可执行文件 SHA256 相同（9b7299926d113fff37548e2be2c1bf96eab800e4b757ee71b89058e7b14f47b0）。系统 release 为 com.quietbridge.helper 0.6.12（95），签名身份不同，其二进制哈希 ca3e6dc7dc291dc1a35bcdd72c153a6008e049fff63bdc9e4b64587de883b2a0 正好对应 helper release 目录。

packaging/macos/build.py 分别签署 menu app 与 helper；deploy.py 的用户 release 目录摘要还包含 extension 内容。因此两处 release 名和签名后二进制哈希不同是预期行为，不能当作来自不同源码构建的证据。

复核时现存：PID 57774 --menubar；其权限代理 PID 57813 --timeout-ms 60000；系统 broker PID 57835 --serve。用户报告的旧会话 worker 当时已不在这份进程快照内。原日志包含 broker 与执行 worker 对同一 requestId 的两层记录，符合既有代理/执行架构，不证明双重派发。

## 处理边界

当前 0.6.12 对这种合法系统路径形态缺少兼容，错误文字又易被理解为已确认可执行文件被替换。不要通过忽略 STALE_APP、仅匹配目录名、bundleId 或跳过 verifyChrome 绕过。

兼容实现须保留 PID+启动代际校验，使用针对运行进程的动态代码身份与预期 Chrome 签名要求核验，并核对应用归属；代码克隆目录不能作为信任来源。应用授权和浏览器绑定两条路径应共用验证，失败则仍阻断。该方案尚未实现/验证，不能声称已修复。

短期由用户选择合适时机保存工作并手动完全退出、重新打开 Chrome，再重新查询权限/连接/原目标，能够检验路径是否恢复一致，但不能保证一次重启解决所有路径形态。不得自动关闭用户标签或重启浏览器。

当前加载的扩展版本应取运行通道握手 clientVersion；磁盘 manifest、历史目录和 ZIP 数量均不能证明 Chrome 当前加载的版本。无须因本次路径错误自动清理历史 release 或重装权限组件。

本轮尚未进入跨 Space 三阶段实验，未产生能解释画面不更新的新捕获统计，不能将此次前置错误当成原画面问题的根因。

## 后续实现：0.6.13（96）

用户询问下一步后，已实现以下兼容，不再要求先重启现有 Chrome：

- `ApplicationExecutableIdentity.resolve` 供应用授权和浏览器 attach/status 共用。普通路径一致的行为保持原样；只为固定安装位置的 Chrome 处理代码克隆。
- 克隆根目录来自 `confstr(_CS_DARWIN_USER_TEMP_DIR)` 的系统用户目录，不读取可伪造的 TMPDIR 环境变量作为信任来源；核验本用户 X/com.google.Chrome.code_sign_clone 的固定相对结构。目录形态本身不授权。
- 动态运行进程通过 `SecCodeCopyGuestWithAttributes` 按 PID 查询，使用 `SecCodeCheckValidity` 核验 Apple generic anchor、com.google.Chrome、Google 团队 EQHXZ8M8AV 和 Developer ID Application 证书 OID；固定磁盘应用也须通过同一要求的严格静态签名校验。签名检查前后再次核验 PID+启动代际。
- 成功后规范化授权 executablePath 为已安装路径，保持应用授权与会话比较稳定；不将瞬时克隆路径写入持久授权身份。签名、路径、bundle 归属或进程代际异常仍然阻断。
- STALE_APP 恢复说明改为最多两次只读重取，仍失败则通知用户等待；文字不再断言应用已经升级/退出，不自动重启或换通道。此为 agent 恢复元数据，不是强制限制所有宿主重试次数的执行器。

验证：应用路径/授权与浏览器会话相关 32 项 Swift 测试通过（包含数据驱动错误目标、签名失败、代际变化、会话永久失效）；另以显式启用的只读集成测试调用生产 ApplicationAuthorization.describe，对未重启的 PID 97718 成功取得规范化应用授权，耗时约 92 毫秒。该集成验证不创建 appHandle 或工具任务，也不共享/截图/操作页面。Swift release 构建通过，MCP 全量 332 项通过。

测试没有关闭 Chrome、切换桌面或绕过签名检查。包内的新代码需覆盖安装后才进入用户连接器；当前真实 Chrome 窗口的工具 get/bind 和跨 Space 绘制实验仍待安装后复测。原先“方案尚未实现”描述为最初只读排查时的状态。
