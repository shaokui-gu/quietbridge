# 网页内容预览源修复

依据 [Round 6 复核与修复范围](2026-11-11-round6-render-probe-review.zh.md) 实现。此次只修改源码和运行自动化测试，未执行新的离屏 A/B/C 取证、真实页面截图、安装、重启、激活窗口或切换 Space。

## 行为与范围

后台网页任务在固定标签分配、原生窗口映射确认后，默认共享及 `source=window` 改用扩展主动获取的网页视口帧。内部源为 `browser_content`、范围为 `web_content`，面板和工具回复标注“网页内容 · 定时帧（非整窗视频）”。不再以离屏 ScreenCaptureKit 持续出帧作为网页内容正确的保证，也不采用“截图一次后继续假定系统视频正常”的策略。

原生任务继续使用系统捕获。网页任务显式指定 `application/desktop` 时也使用原系统捕获，后续网页操作保留该范围；工具栏、原生对话框及跨应用界面仍走对应原生任务与系统共享路径。没有改变物理焦点、选择实际标签或切换桌面的恢复逻辑。

## 数据流与隔离

1. MCP 的 `BrowserPreview` 为已验证映射的任务建立原生面板租约，并向扩展发布最长 3 秒的生产租约。相同绑定续租，不为每次操作重建预览。
2. 扩展同步在没有待执行页面命令时，最高每秒启动一次获取：检查固定标签和文档、必要时有界连接 debugger、查询视口、获取一次 JPEG。布局长边约束为 1000×750，quality=45，超过 180000 个 base64 字符丢弃，不在本轮重试。
3. 获取前后检查 taskId、bindingId、assignmentVersion、browserWindowId、tabId、documentId、navigationEpoch 和有效期。调试附加、文档查询、协议调用均有超时；失败不重放输入。
4. 预览帧走独立同步字段，与命令结果和成功验证分离。MCP 每 0.5 秒尝试消费最新帧；忙于工具调用时跳过，由帧龄限制淘汰旧帧。原生端复核租约、单调序号、JPEG 类型、尺寸上限 2000×1500 和 3 秒帧龄，再交给独立图片层显示，不创建该源的 SCStream。
5. 导航未就绪、文档变化时立即清空旧文档画面。帧超过 3 秒不再展示。结束、取消、重分配、权限撤销、连接失效或租约到期时停止生产并释放；旧租约晚到帧不能恢复面板。邮箱读写失败仍尝试原生释放，hold 回执失败也清理已请求资源。

预览流量和轮询不计作用户的实际工具活动，不延长任务空闲期限。原生资源仍遵循既有连接所有权和锁屏/权限检查。预览不可用不会将后台命令改派到前台输入。

## 内容存储与打包

复用现有扩展—原生宿主—MCP JSON 邮箱，仅保存最新一帧。图像会短暂落盘到当前用户可读写的 `browser-events.json`，消费或清理后移除；没有宣称纯内存或崩溃后立即擦盘。记录接近总大小上限时优先丢弃预览帧，避免挤占命令结果。操作日志不保存该帧或页面正文。

扩展版本升至 0.4.9，新增 `browser_preview` 能力。构建、部署及发布布局均包含 `browser_preview.py`。需要匹配的扩展、MCP 和原生端；旧扩展缺少能力时返回网页预览不可用，不假报新源已启动。

主要入口：`services/mcp/browser_preview.py`、`services/mcp/browser_events.py`、`services/mcp/server.py`、`extensions/chromium/channel-worker.mjs`、`extensions/chromium/background-browser.mjs`、`SpacePreview.swift` 与 `RequestCoordinator.swift`。

## 自动化验证

- Node：108 项通过，含固定身份/尺寸边界、晚到帧、过期与撤销、同步邮箱、命令优先及停止生产。
- MCP：362 项通过，含导航清屏、身份与帧龄过滤、租约退出、断线不重建、邮箱失败释放、hold 失败清理、默认网页源及显式系统共享范围保留。
- 打包/部署：47 项通过。
- Swift：285 项通过（`swift test --skip clipboardPaste`）；原完整运行中 8 个 `clipboardPaste` 测试因沙箱系统剪贴板不可用报 `CLIPBOARD_UNAVAILABLE`，不是完整套件通过。此轮没有修改剪贴板路径。
- `git diff --check` 通过。

测试日志：`/tmp/quietbridge-node-preview-final.log`、`/tmp/quietbridge-python-preview-final.log`、`/tmp/quietbridge-package-preview.log`、`/tmp/quietbridge-swift-preview-final.log`。

## 验收边界

源码实现与上述自动化验证已完成，已构建并核验 0.6.17（100）安装包（扩展 0.4.9），尚未安装或验收真实离屏画面。因此未宣称生产环境已恢复，也未补写已停止的同流截图后再导航持续性实验。后续发布验收应直接核对新源标识、任务页导航后的定时帧更新、结束后的关闭及不抢焦点，不能将定时帧称为高帧率整窗视频。
