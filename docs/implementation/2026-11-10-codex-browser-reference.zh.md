# Codex 浏览器实现对 QuietBridge 的参考

日期：2026-11-10。范围：官方资料与本机已安装桌面客户端静态检查；未启动浏览器、连接调试端口、读取网站数据或改变应用配置。

## 使用的浏览器

官方 Browser 文档明确区分：桌面内置浏览器（`@Browser`）与通过扩展使用已有浏览器配置的通道（例如 `@Chrome`）。内置浏览器使用独立 profile，不自动复用用户 Chrome 登录态。云端 Work 浏览器是另一种环境，不能与本机内置浏览器混同。

本机 `/Applications/ChatGPT.app` 的 bundle ID 为 `com.openai.codex`，版本为 `26.1103.60933`。以下实现结论仅针对这个构建，不代表所有 Codex 版本。

## 本机静态证据

安装包 `Contents/Resources/app.asar` 中：

- `package.json` 包含 `@oai/cua-repl`、`browser-api`、`browser-backend-common` 等依赖；主入口加载 Electron。
- `/.vite/build/early-bootstrap-815-e030c49be4b9c405.js` 区分 `control-in-app-browser`、`control-chrome` 和 `computer-use`，符合多通道而非单一系统鼠标通道的结构。
- 内置浏览器页面记录 `isBrowserUseActive`、`isBeingCaptured()` 等状态，并调用 `webContents.setBackgroundThrottling(...)`。对符合该路径条件的页面，正在执行 Browser Use 或正在捕获时，不按普通不可见页面开启后台节流。不能推断所有窗口、网页和外部 Chrome 都受该策略管理。
- 存在内置浏览器 CDP 执行适配与 capture surface 管理；部分页面截图路径直接调用 `webContents.capturePage()`。这证明存在浏览器级控制／取图能力，不证明所有截图均走同一实现。
- 当前提供给本会话的 CUA 接口也区分 `iab`、Chrome、Edge 等 browser surface，并提供可见或隐藏的内置浏览器 tab 创建方式。`visible: false` 仅说明界面不显示，不能等同于启动 Chrome 的 `--headless` 模式。

## 能确认与不能确认

能确认的是：本机内置浏览器是 Electron/Chromium 体系的应用内页面，Agent 通过浏览器控制层使用页面；宿主可以管理后台节流、页面截图与呈现。用户已有 Chrome 则是另一条扩展通道。

不能从上述证据确认：所有操作都仅走 CDP、预览一律使用 `Page.startScreencast`、具体帧率／编码／传输协议、后台页面永不冻结、所有 Chrome 扩展任务都不抢焦点。没有证据表明桌面内置浏览器必须是独立无头 Chrome 进程。

## 对 QuietBridge 的意义

目前 QuietBridge 通过 ScreenCaptureKit 捕获既有 Chrome 窗口，不拥有 Chrome 内部 `webContents`，也就不能直接套用宿主的 `setBackgroundThrottling(false)`。原生窗口捕获仍有帧，不代表页面内容仍在绘制。

下一步的设计应把“页面控制”“后台绘制保障”“预览画面输出”作为同一浏览器会话的能力进行管理，并与原生应用的系统共享分开实现：

1. 若 Workbuddy 开放了受支持的会话入口，验证宿主内置浏览器适配；不能仅凭其内部接口存在就认定第三方可稳定调用。
2. 通用独立模式可验证受管无头 Chrome + 页面画面流，绑定浏览器实例与 target，不依赖 CGWindowID。
3. 预览界面可复用，画面源应可替换为系统窗口流或浏览器页面流。画面更新不经过模型，模型仅在观察时取图。
4. 对网页绘制、连续画面、断线恢复和三会话并行做验证；不用激活、raise、切换 Space 或系统鼠标作为恢复手段。

以上是架构参考，未在本轮更改 QuietBridge 的功能或打包。

## 官方来源

- [Browser](https://learn.chatgpt.com/docs/browser)：桌面内置浏览器、独立 profile、Chrome 扩展、开发者模式 CDP，以及云端浏览器的区别。
- [Using your computer and browser](https://developers.openai.com/training/walkthroughs/using-your-computer-and-browser)：Computer、Browser 和 Chrome 的入口与用途。

本机实现细节来自已安装包的静态代码检查，官方文档未逐项公开这些内部实现，也未保证其是第三方可调用接口。
