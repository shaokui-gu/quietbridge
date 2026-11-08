# QuietBridge 整体架构与实施方案

更新日期：2026-11-07（北京时间）。本文件为项目整体方案入口。

## 1. 产品与交付边界

| 对象 | 名称 | 当前状态 |
| --- | --- | --- |
| 原始项目 | `quietbridge` | 本仓库为唯一核心来源 |
| macOS 应用 | **QuietBridge** / `QuietBridge.app` | 已有单任务应用，源码与打包名称已迁移 |
| 原生可执行文件 / Swift 模块 | `quietbridge-native` / `QuietBridgeNative` | 已迁移 |
| 浏览器扩展 | **QuietBridge Browser** | 一份 Chromium MV3 扩展 |
| Workbuddy 产品 | **QuietBridge for Workbuddy** | MCP 接入、升级入口和停止 hooks 已实现 |
| DeepSeek Harness 产品 | **QuietBridge for DeepSeek Harness** | 仅建立适配和产品边界，尚不可安装或发布 |

一台 Mac 共用一个应用、一套系统权限、一份浏览器扩展。两个市场包分别
遵守各宿主规范，复用核心实现。市场元数据目录中的 `product.json` 为内部
描述，不冒充已核验的市场 manifest，也不表示已提交市场。

用户可见名称、源码目录、Swift 模块、原生入口、内部品牌函数及网页注入函数
统一采用 QuietBridge。旧品牌只用于读取既有安装、外部协议与签名兼容，以及
第三方原始许可归属。

## 2. 必须保持的行为

- 后台操作：不激活目标应用，不置前窗口，不移动实体鼠标，不切换用户 Space；
  不采用先抢焦点再恢复的方式。
- 原生界面操作先显式绑定真实目标并建立共享窗口，核实视频首帧和焦点保留；
  共享画面不提供独立桌面或焦点隔离，输入仍须使用后台定向通道。
- 网页输入仅走固定标签的扩展通道；映射、共享、连接或权限不满足时暂停，
  给出用户恢复步骤，不换通道绕过，也不自动重放状态未知的输入。
- 结束、取消、断连或执行故障需停止所属任务并关闭其共享；应用“退出”是
  全局退出，撤销所有任务能力。
- 入口集中于应用控制面板，扩展面板承担浏览器授权及必要连接诊断。
- 新增多任务界面及布局先更新 Figma，再实现应用代码。

## 3. 源码结构与依赖

```text
quietbridge/
├── project.json                    产品命名与能力状态
├── apps/macos/                     App、原生执行与预览
│   └── Sources/QuietBridgeNative/
├── services/mcp/                    公共 MCP、任务、恢复、浏览器消息
│   ├── server.py                    quietbridge_* 工具；接受旧工具别名
│   ├── connector.py                 升级时保留的 stdio 入口
│   ├── lifecycle_store.py           宿主中立的任务回执与撤销存储
│   └── compatibility.py             Python 兼容边界
├── extensions/chromium/             一份扩展及原生消息宿主
├── system/authorization-plugin/     可选系统授权组件
├── adapters/
│   ├── workbuddy/                   MCP 配置合并与生命周期事件适配
│   └── deepseek_harness/            接口待核验、实现待完成
├── products/
│   ├── workbuddy-connector/          独立产品元数据及市场边界
│   └── deepseek-harness-plugin/      独立产品元数据及市场边界
├── packaging/macos/                 共享应用打包、事务部署与恢复
├── tools/release_layout.py          源码安装和 PKG 共用的桥接文件清单
└── docs/architecture/               总体方案及并行运行时细则
```

`adapters` 处理宿主上下文、配置、停止和工具注册差异；不实现第二套 AX、鼠标、
截图或标签绑定。`products` 组合适配器和公共服务，记录版本范围与发布入口。
`services/mcp` 不直接解析 Workbuddy hook payload；它读取公共撤销存储。

打包与源码安装通过 `tools/release_layout.py` 复制相同的核心模块和适配代码。
安装后的桥接资源内保留 `adapters` 包结构；稳定连接目录同时携带 hook 所需
依赖，停止回调不依赖开发者的工作区。

系统签名与授权属于原生应用及可选授权组件，市场包不能自行放宽它们的校验。
控制面板与当前原生实现仍有部分历史宿主预览选择逻辑；该部分需在多宿主阶段
改为经过验证的显式宿主窗口绑定，当前不宣称已经支持 DSH 预览。

## 4. 已落地与目标架构的区别

### 4.1 本次已落地

1. 实际迁移四个原有源码目录，并更新构建、安装、测试和文档引用。
2. App、安装器、权限向导、保护画面、扩展和 MCP 显示名采用 QuietBridge。
3. Swift package/target/main、Objective-C 源文件与内部函数、网页注入函数采用
   QuietBridge 名称；新工具以 `quietbridge_*` 发布；浏览器扩展版本更新为 `0.4.0`。
4. Workbuddy MCP 配置与 hooks 合并逻辑进入 `adapters/workbuddy/configuration.py`；
   事件解析进入 `lifecycle.py`；公共撤销存储保留于 MCP 服务。
5. 建立两个宿主产品目录、DSH 适配前置要求、统一装配清单与兼容层。

### 4.2 当前仍存在的限制

当前核心仍使用单一 `activeTaskId`、原生资源 owner、全局调度槽和
`SpacePreview.shared`。浏览器也未具备完整多会话分路。**整理目录没有将这些
机制变成并行实现**；不同 Agent 同时执行、同时共享，以及 DSH 正式适配均
需完成下面阶段后交付。

## 5. 一个应用、多个宿主、每 Agent 隔离

目标运行链路：宿主适配器 → 会话 Broker → 每执行会话独立桥接与原生 Worker；
浏览器扩展作为共享基础设施，内部按会话和目标分路。

| 层 | 责任与禁止跨越的边界 |
| --- | --- |
| 应用 | 权限、组件版本、面板、全局退出、Broker 受管启动 |
| 宿主适配器 | 传递真实 host/session/agent/turn 身份与取消事件，提供宿主窗口证据 |
| Broker（待实现） | 执行会话注册、进程监督、目标写租约、资源预算、系统资源聚合 |
| 会话桥接 | 私有任务/快照/结果、参数校验、错误恢复，不能使用跨会话默认目标 |
| 原生 Worker（待实现） | 一个执行会话的 AX、定向输入、捕获和预览；卡死可单独回收 |
| 扩展多路运行时（待实现） | 每任务标签、文档、命令去重、CDP、弹窗及清理 |

Broker 不运行可能卡死的 AX 请求，不等待 Worker 视频首帧。隔离粒度为 Agent
执行会话，涵盖同一 Workbuddy 或 DSH 内的多个 Agent，不只分成两个宿主实例。

上下文字段为 `applicationSessionId / hostInstanceId / hostSessionId /
agentInstanceId / executionSessionId / turnId / taskId / leaseId / generation`。
会话标识由可信宿主上下文与受保护 IPC 绑定；不能靠模型自报 Agent 名称、
前台窗口、最后一个 hook 写入者或“最近的任务”猜测归属。

首选每 Agent 独立连接；共享 MCP 连接时，必须核验宿主是否提供可信调用上下文。
Workbuddy 已有停止 hook 不证明其每次并发调用都有足够身份信息；DSH 的 API、
传输与市场格式需对照实际支持的规范验证。有缺口时显式报告不支持的范围。

## 6. 并行执行、共享与资源冲突

- 不同后台应用或独立浏览器窗口可并行。两个 Agent 写同一真实窗口/标签时，
  第二个得到明确资源占用错误，不自动接管、不结束其他人的任务。
- 同一应用多窗口涉及进程级菜单或输入状态，需先实测；未验证时按应用协调。
- 每会话独立捕获流、首帧状态、预览面板与宿主锚点；同宿主多 Agent 的预览
  由布局协调器排列，避免重叠。收起显示与停止共享分别处理。
- 浏览器同窗口多标签不能同时提供各标签的真实窗口视频；并行共享为每任务
  使用专用浏览器窗口。标签隔离不等于账号、Cookie 或 Profile 隔离。
- 防睡眠由 Broker 汇总有效会话。Locked use、系统锁屏、用户接管、权限撤销
  属于共同系统状态；不能承诺每 Agent 有独立锁屏环境。手动解锁只改变解锁方式，
  不暂停、取消或要求重建业务任务；实际停止继续由会话取消及任务租约决定。
- 保留的剪贴板操作必须整机独占并校验所有权；优先使用 AX 设值和扩展填值。

## 7. 停止和故障处理

停止事件以可信 host/session/agent/turn/lease 定位所属资源，顺序为：

1. 撤销新输入资格，移除尚未派发的请求。
2. 取消对应原生 Worker/浏览器等待，拒绝迟到代次，不重放已派发输入。
3. 关闭所属共享与面板，释放目标租约及系统资源参与登记。
4. Worker 无法响应时回收该 Worker；其他会话继续运行。

保留每会话操作租约与断连兜底；心跳、ping 和状态查询不能无限续期。正常路径
以宿主停止事件为起点，目标为两秒内关闭所属预览。系统权限撤销、应用退出、
浏览器或 Broker 崩溃可能影响多路，应准确报告影响范围。

错误回执保留 `errorCode`、派发状态、恢复步骤和是否允许替代工具的策略。
权限未开、应用未运行、连接待恢复时先引导用户处理并等待；不得静默跳过
QuietBridge 或改用前台自动化实现同一输入。

## 8. 命名兼容与安装迁移

| 边界 | 迁移规则 |
| --- | --- |
| 用户显示 | 全部新界面使用 QuietBridge；旧系统授权记录的显示刷新需签名安装实测 |
| App 路径 | 新包安装 `/Applications/QuietBridge.app`；preinstall 识别两个旧 App 名并验证签名后迁移；多副本或符号链接拒绝自动移动 |
| Swift/源码 | `QuietBridgeNative`、`QuietBridgeNativeMain`、`QuietBridgeAuthorizationPlugin.m` 及 QuietBridge 内部函数 |
| 工具 API | 发布 `quietbridge_*`；旧 `cua_*` 在调用边界映射到同一实现，不重复发布一套工具 |
| 环境变量 | 优先 `QUIETBRIDGE_*`，未提供时读取旧 `CUA_*`；冲突时新值胜出 |
| MCP 配置 | 新安装 key 为 `quietbridge`；已确认属于本产品的旧 key 原位保留，保持信任和启用状态；不覆盖其他连接 |
| 稳定 stdio | 已发布固定入口的原始字节按已知摘要保留，继续加载新版核心；已有用户运行目录沿用旧路径；新用户目录为 `~/Library/Application Support/QuietBridge` |
| 扩展运行态 | 既有 `WorkbuddyExtension` 目录沿用原有子目录及注册；新安装使用 `QuietBridgeBrowser` |
| 内核锁 | 原应用会话锁命名空间保留，桥接与原生必须一致，避免出现两个并行的全局 App 生命周期 |
| 签名 | 按用户明确要求改为 `com.quietbridge.app`、`com.quietbridge.helper`、`com.quietbridge.AuthorizationPlugin`；开发者 Team 不变，旧 ID 仅用于识别已安装代码与迁移 |
| 授权插件 | 现有授权数据库机制、部署 bundle basename、socket 和 plist 协议键保留；源码和 bundle 内可执行文件改名，不改 Allow/Deny 策略 |
| 第三方许可 | 保留上游 Cua Driver 的原名称、链接和 MIT 许可，不将它们改成自有品牌 |

这些历史值集中于 Python `compatibility.py` 与 Swift `QuietBridgeIdentity.swift`，
系统授权脚本保留必要的协议字面量。它们是外部兼容标识，不能通过全局字符串
替换删除。本次 Bundle ID 迁移改变 TCC 身份，首次启动须重新检测并按向导授予
辅助功能与屏幕录制；不能继承或伪造旧授权。安装器在固定路径替换前严格核验
既有新旧 App 的签名和 Team，launchd 任务键保留旧值，避免出现重复服务。

旧描述文件绑定旧 Bundle ID，不嵌入新的组件。用户已补充三个匹配通用 ID 的
QuietBridge 描述文件，最终包分别嵌入 App、Helper 和授权插件，并核验对应
ID、Team、有效期与签名证书。当前无受限 entitlement，也支持不附带描述文件的
标准 Developer ID 构建；后续如需受限能力，须核验新 ID 的授权范围。
旧授权插件须经应用管理入口显式升级并注销后重新登录。升级兼容已提交的旧
安装记录，写入新的插件身份，保留原始授权备份及既有规则；主 PKG 不自动
启用或更改 authorizationdb，未升级时锁屏能力保持不可用并显示更新提示。

PKG 装配先构建公共代码，再组装同一 App 和扩展。用户文件修改保持事务备份、
并发编辑检查和回滚；原生插件管理仍走明确的用户授权流程。源码、静态验证
和单元测试通过后，发布阶段另做签名、安装、覆盖升级及卸载验证。

## 9. 分阶段落地与验收门槛

| 阶段 | 交付 | 验收要求 |
| --- | --- | --- |
| 0：品牌与源码边界 | 本次目录迁移、名称、适配抽离、统一装配与文档 | 核心/安装/扩展测试、Swift 编译及符号检查通过 |
| 1：宿主契约验证 | Workbuddy/DSH 真实 Agent 身份、取消和宿主窗口证据 | 并发主/子 Agent 每次调用可唯一归属；API 缺口明确记录 |
| 2：隔离运行时 | Broker、每 Agent Worker、私有状态、目标写租约和停止代次 | A 停止、AX 卡死或 Worker 崩溃不撤销 B；旧结果不能复活任务 |
| 3：扩展分路 | 多任务协议、独立标签队列、结果与弹窗归属 | 并行网页任务不串页，结束一项不解除另一项 CDP |
| 4：多共享界面 | 先 Figma 再实现会话面板、多预览、单项停止与全局退出 | 四路预览可辨认、不重叠，独立收起和停止 |
| 5：系统协调及产品包 | 防睡眠/Locked use 聚合、版本协商、两个市场包 | 两宿主共用一个 App；移除一个宿主接入不卸载公共运行时 |
| 6：真实端到端验收 | Workbuddy 两 Agent + DSH 两 Agent | 四路并行任务及共享持续至少 30 分钟，停止/崩溃/权限变化均按所属范围清理 |

每阶段记录组件版本、测试命令、日志 traceId、执行时间与未覆盖范围。独立测试
不能替代真实宿主取消、签名升级、权限归属和不抢焦点验收。

完整的资源模型、故障注入及验收用例见
[多 Agent 并行运行时细则](multi-agent-runtime.zh.md)。

## 10. 本次整理验证记录

| 检查 | 结果 |
| --- | --- |
| MCP 与升级兼容测试 | 253 项通过，包含 19 个旧工具别名进入同一恢复策略 |
| 部署与安装器测试 | 34 项通过，包含旧连接键/停用状态、浏览器运行路径、旧 hooks、重复安装、通用身份清理及描述文件校验 |
| 授权安装及旧二进制名称兼容测试 | 20 项通过，包含旧签名身份安装记录迁移和拒绝外来记录 |
| 扩展 Node 测试 | 58 项通过，包含旧 worker 的页面注入别名与清理 |
| Swift 原生及面板测试 | 252 项通过 |
| Objective-C 源码检查 | 新源文件通过 Clang 严格语法检查 |
| 旧固定连接入口 | 两个已发布入口原样运行，均可加载 MCP 1.3.0、列出 19 个新工具且不创建任务状态 |
| 安装装配与脚本 | 脱离源码目录的 preflight 运行成功且未创建用户文件；安装器名称及组件名为 QuietBridge；shell 语法通过 |

2026-11-07 已生成 QuietBridge 0.6.0（build 83）PKG，使用用户提供的 Developer ID
证书和三个新的 QuietBridge 描述文件。对最终包解包核验：App、Helper 和授权插件
的通用 ID、Team、描述文件及严格签名检查均通过；脱离源码目录的隔离安装预检
与 MCP 1.3.0/19 个工具握手通过，未启动 helper 或写入用户状态。
证据保存在 `dist/quietbridge-0.6.0-verification.json`。临时签名钥匙串已恢复并删除。

应用及组件已签名，PKG 外层未签名、未公证。本次未执行真实安装、系统权限变更、
宿主重启或多 Agent 端到端运行；这些仍需分别验收。
