# Linux.do 长任务回归：证据、修订与未解决项

日期：2026-11-09。现场基线为 QuietBridge 0.6.6 / QuietBridge Browser 0.4.2。
本次源码将扩展更新至 0.4.3，已打包为 QuietBridge 0.6.7（90）；未安装或运行真实网站验收。

## 结论

本轮中断包含两类问题：Chrome 调试访问／输入回执阻塞，以及适合交互的快照不足以承担长篇阅读。
本次补齐分页链接与正文读取，修订受管启动的任务守卫和恢复说明，并增强点击子阶段诊断。
**没有证据证明跨扩展调试拒绝或现场点击超时已被消除；不能把自动测试通过当作 50 个话题／200 条评论已完成。**

## 现场日志相互印证

读取位置为当前用户的 WorkbuddyExtension 会话 browser-events.json 与 QuietBridge bridge-operations.jsonl。
只读取已有日志，未执行网站操作、修改权限、重连连接器或切换用户目标。

| 问题 | 日志证据 | 可作出的结论 |
| --- | --- | --- |
| 导航被拒绝 | requestId=71a081e2774adfbf1bb8ef94d59b1e5e；BROWSER_DEBUGGER_RESTRICTED，阶段 attach，约 141.3ms；toolTraceId=75b40b15-18c4-4a72-9df3-9aae5d2e944e | navigate 的公共前置保护触发 debugger.attach；尚未发送 tabs.update，并非整个扩展连接失效。相邻 snapshot 约 207–322ms 成功。 |
| 点击超时 | requestId=5ddb0d8101d33340755706c6a22d4cbb；BACKGROUND_COMMAND_TIMEOUT，Input.dispatchMouseEvent，约 4102.4ms；toolTraceId=fc47a261-48d3-4e5f-a922-68f1db5d0f5b | 输入回执未知。旧日志未记录 mouseMoved／mousePressed／mouseReleased，不能认定具体哪个事件卡住，也不能认定点击未发生。 |
| 受管恢复被拦截 | TASK_SCOPE_REQUIRED；toolTraceId=88f480eb-01db-4c15-8035-bef26eab50eb | 已绑定任务禁止另起浏览器；此前把独立 profile 描述为直接恢复路径不完整。 |

## 已落地源码

### 分开交互快照与阅读

- snapshot 保留最多 300 个交互元素及原有引用校验，链接新增 href、urlHasQuery、urlHasFragment。
- 新增 quietbridge_browser_events 的 command operation=read：
  - mode=links：当前主文档已渲染链接的名称及 HTTP(S) 目的地址。
  - mode=content：当前主文档 article／role=article 正文；没有文章节点时读取段落、标题、列表等文本块。Discourse 优先读取 .cooked。
  - limit 默认 30，范围 1–100；maxChars 默认 10000，范围 4000–16000；首次 offset=0，续读传相同 mode、readId 和 nextOffset。字符限额约束条目 JSON，不含外层元数据。
  - 单次采集最多 10000 个候选节点、2000 个条目／正文分片、262144 个原始文本字符单位；达到上限返回 sourceTruncated=true。
  - 长正文每 1000 个 Unicode 码点分片，使用 textOffset 拼接；源文本被截断时 textComplete=false。URL+id 相同的分片不能重复计为帖子。article 只是 DOM 类型，text 文本块也不能直接当作评论计数。
  - readId 对应冻结的读取结果及标题；绑定、导航、同文档 URL 改变或新 snapshot 后旧读取失效。
  - read 不附加 debugger、不访问网站 API、不调用任意脚本，不使已有交互 ref 失效，也不能解除输入后的 snapshot 复核要求。
  - 回执再次核对任务、绑定、原始 documentId、navigationEpoch；页面替换时丢弃旧正文。
- href 仅保留 origin+pathname；query／fragment 只标记是否存在。带凭据、非 HTTP(S) 地址不返回。
- 没有查询参数的链接可以作为明确地址证据；存在 query／fragment 的地址不完整，不能将脱敏地址当作等价导航目标。
- 空 innerText 不回退到隐藏子节点的 textContent；隐藏正文容器不读取。
- 已打开页面中的旧版内容观察脚本可在扩展重新注入时替换；需要重新收到有效绑定，不必额外刷新页面。
- 分页能力通过 paged_read 协商；旧扩展收到 EXTENSION_UPGRADE_REQUIRED。

示例（先完成原任务窗口 bind、扩展 assign，并使用真实 taskId）：

    {"action":"command","taskId":"实际任务ID","operation":"read","mode":"links","limit":50,"maxChars":10000}

通过 result 读取 commandId 回执，再用返回的 readId／nextOffset 续读。
返回内容只证明 DOM 中已加载并渲染的部分；虚拟列表中未加载或已经卸载的楼层不在其内。

### 受管启动与恢复

- prepare／list 可在活动任务内查询。
- start 可在无活动任务，或 background_browser 尚处于 awaiting_window 且没有 windowId 的阶段执行。保留 confirm=true、连接归属、租约、单次启动和取消保护。
- 已绑定任务不能自动迁移、结束或改绑；失败时保留原目标，通知用户确认恢复方式。
- 受管 profile 明确返回 profileIsolated=true、inheritsLogin=false、extensionAutoLoaded=false、browserChannelReady=false。需用户在新 profile 加载扩展、授权并登录，依据 list 的真实 PID／窗口获取句柄后绑定。
- 不复制 profile／cookie、不停用其他扩展，不把其他既有 Chrome 窗口当作新会话窗口。启动参数请求不激活，但不能保证 Chrome 启动绝不改变焦点；因此不自动启动它作为不抢焦点任务的兜底。
- BROWSER_DEBUGGER_RESTRICTED、BACKGROUND_COMMAND_TIMEOUT、TASK_SCOPE_REQUIRED 均提供 notify_user_and_wait 指引，禁止自动换工具、重放副作用或反复重连。

### 输入超时诊断

- 超时记录方法及事件子阶段，例如 Input.dispatchMouseEvent/mousePressed。
- 任何子阶段未回执即暂停调试输入，不再发送后续事件、不重放原输入；原标签只读 snapshot／read／diagnostics 可用于核实结果。
- 这是诊断与结果未知处理，**不是已经修复 Chrome 不回执的证明**。

## 仍需解决与验证

1. navigate 仍先安装跨导航弹窗保护，因此仍依赖 debugger.attach。不能只删除该保护来绕过 Chrome 拒绝，否则目的页脚本可能打开前台窗口，违反核心约束。当前没有加入这种绕过。
2. 点击仍使用固定标签 CDP 输入。下一次出现超时需要新的子阶段证据；不得凭旧日志猜测修改超时、重复点击或退回系统键鼠。
3. 受管浏览器仍不是带原登录态和扩展的即用逃生路径。真正自动化的独立浏览器准备需要单独设计、用户配置及不抢焦点验收。
4. 读取分页降低了重复回传交互树的开销，但没有测量 50／200 任务的实际 token 消耗或成功率。
5. snapshot 的 elementCount 是 DOM 元素总量；3411 与 300 不同口径，不能说遗漏了 3111 个可操作元素。
6. 匿名 404 本身不能唯一证明登录权限限制；可能也与路径、类别设置或服务端策略有关。本轮未访问该站点核实，不猜接口、不绕过登录。
7. Chrome AX 树为空不等于浏览器通道或录屏权限失效。此任务应使用扩展 DOM 阅读；AX 不是登录网页正文的替代保证。

## 验证记录

- MCP Python 全套：297 项通过。
- Chromium Node：71 项通过，包含 URL 变化续读拒绝与冻结标题测试。
- Swift BrowserSession：17 项通过。
- git diff --check：最终新增测试后再次核对，通过。

新增覆盖：超过 300 个链接的续读、正文冻结分片、Unicode／控制字符、隐藏正文、超长文本截断、旧观察脚本升级、过期文档回执、三种鼠标子阶段超时、读取不清输入复核、旧扩展能力拒绝和未绑定阶段启动。
所有自动测试使用隔离 fixture，未在真实 Linux.do 页面执行操作；没有安装或提交代码。

## 0.6.7 打包验收

- 产物：dist/quietbridge-0.6.7.pkg，2.51 MiB；App／Helper 为 0.6.7（90），扩展为 0.4.3。
- SHA256：a690e30538b381e231048d48209ead2e9e48b0f95f54ab61be7eb8e5d5166a58。
- Release 编译和 macOS 打包 34 项测试通过。
- 实际展开 PKG 后，App／Helper／授权组件签名、标识、团队及证书均核验通过，内嵌 profiles 与 sign/ 提供的材料一致。
- 包内桥接源码、扩展源码及扩展 ZIP 字节一致；MCP 工具清单包含分页读取。
- 独立 HOME 下安装预检、MCP initialize／tools/list（19 工具）及包内原生权限执行器烟测通过，未安装或改变用户权限。
- 临时钥匙串已删除，原搜索列表已恢复。PKG 容器未签名、未公证。
- 机器可读验收记录：dist/quietbridge-0.6.7-verification.json。
