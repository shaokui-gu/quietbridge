# Chrome attach 拒绝：其他扩展页面与调试器占用的区分

## 本轮证据

用户回报原生窗口 274431、标签 57075068 的 bind、assign、verify_mapping 和 off-Space 系统窗口共享均成功，navigate 三次在 attach 阶段被拒。snapshot/diagnostics 成功只证明内容脚本通道可用，不能证明 CDP 可附着。

QuietBridge 原实现并非把所有 attach 错误统一包装：`background-browser.mjs` 只有在 Chrome 原文包含 `Cannot access a chrome-extension:// URL of different extension` 时才转换为 BROWSER_DEBUGGER_RESTRICTED。另一调试客户端的占用原文为 `Another debugger is already attached to the … with id: …`。因此此次中文错误足以确认第一种 Chrome 限制路径；不能据此认定是第二个客户端或先前会话遗留附着。

Chromium 官方实现：

- [debugger_api.cc](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/browser/extensions/api/debugger/debugger_api.cc) 的 `kAlreadyAttachedError` 定义独立的已有调试客户端错误。
- 同文件 `ExtensionMayAttachToURL` 拒绝其他扩展的 chrome-extension origin；`ExtensionMayAttachToRenderFrameHost` 遍历目标 frame 树检查权限，因此 HTTP 顶层页面含其他扩展 iframe 时也可能拒绝。

只读检查 Profile 4 的已安装扩展源码发现：

| 扩展 | ID | 有关证据 |
| --- | --- | --- |
| JSON-handle 0.7.4 | `iahnhfdhidomcpggpaimmmahffihkfnj` | content_scripts.js 创建 iframe，src 使用 `chrome.runtime.getURL("JSON-handle/JSON-handle.html?type=iframe")`。无需 debugger 权限也能形成受限扩展 frame。 |
| JSON Viewer 0.18.1 | `gbmdgpbipfallnflgajpaliibnhdgobh` | 存在面向普通网页的内容脚本；本轮未据此确认其造成该 frame。 |
| ChatGPT 1.26.1102.11740 | `hehggadaopoacecdllhhajmbjkdcmajg` | manifest 声明 debugger 权限，但这不证明其正在附着该标签。静态 content_scripts 匹配 chatgpt.com；未把它确定为本轮占用者。 |

当前标签来自 JSON 页面，JSON-handle 是证据更充分的候选。尚未读取该现场标签的实际 frame 树，故不能声称已实机确认由此扩展造成。文件证据只说明其具备注入上述 frame 的实现。

## 实现调整（扩展 0.4.13）

- 外来扩展页面保留 BROWSER_DEBUGGER_RESTRICTED；明确的已有调试客户端错误单独归类 BROWSER_DEBUGGER_IN_USE。
- 回执新增 `debuggerFailure`，携带 Chrome 原文、调用阶段、分类以及可用的扩展 iframe ID。
- attach 被拒且属于外来扩展页面时，在同一任务、标签和文档上执行一次有界只读 DOM 检查：最多查看顶层文档的 128 个 iframe/frame src，返回至多 8 个去重扩展 ID。来源标记为 `top_document_iframe_src`，只证明 DOM 声明的 src，不冒充 Chrome frame 树或调试器所有权。
- 不读 frame 内容，不回传路径、查询参数、图片或用户页面文本；文档变化、脚本失败或超时不采信 frame 证据，仍保留原 Chrome 错误。未知错误保持原有处理。
- MCP 严格验证新增证据字段、长度和类型，拒绝图像、任意 URL 或不匹配的错误类型。坏回执被消费并报告 INVALID_BROWSER_RESULT，不毒化后续握手。
- 恢复指引不再把 debugger 权限当成占用证据，不承诺重载 QuietBridge 就能移除其他扩展 frame，不自动更换标签、关闭扩展、删除 iframe、强制 detach 或绕过跨导航弹窗保护。

## 实机复测方向

由用户将原专用标签手动打开普通 HTML 测试页 `http://127.0.0.1:48764/chrome-off-space-continuity.html?step=1`，然后回到其他桌面，再按原流程验证 step=1→2。本轮只需一次导航。当前帧含其他扩展页面时，QuietBridge 不会绕过保护直接导航。

若普通 HTML 页面仍出现 BROWSER_DEBUGGER_RESTRICTED，记录 `debuggerFailure`；只有证据指向某扩展时，再由用户调整对应扩展的站点访问。BROWSER_DEBUGGER_IN_USE 则核对 DevTools 或实际控制客户端。这两种问题分开处理。

自动化验证：MCP 全量 394 项、扩展全量 118 项通过。覆盖原文及分类保留、同文档扩展 ID 的提取与去重、页面路径/查询不回传、错误与文档变化不覆盖原拒绝、无导航/截图/detach 重试、worker 回执传递、MCP 严格校验及恢复指引。没有操作真实 Chrome、重载扩展或改变用户插件开关。

已打包 QuietBridge 0.6.25（108）及扩展 0.4.13。实际安装包的组件签名、证书/profiles、版本、MCP/适配器源码、扩展目录及 ZIP、新诊断标记和隔离冒烟核验通过，见 `dist/quietbridge-0.6.25-verification.json`。复用本轮未变的已验证原生 Release 程序。临时钥匙串已删除，搜索列表已恢复；未安装或重启应用。PKG 容器未签名、未公证。
