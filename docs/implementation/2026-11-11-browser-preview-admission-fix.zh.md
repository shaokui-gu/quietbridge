# 0.6.17 网页预览启动准入故障

用户回报任务 `c9c2b740-9d6d-4b22-a755-19668549e400` 在 share 阶段失败，已结束任务并释放，没有导航或截图。此轮仅只读关联既有日志及运行隔离测试，没有重新操作真实 Chrome 或申请共享。

## 根因

不是 `windowId` 未列入白名单。bridge 日志的 `mcp-33211-24` 显示请求已经解析窗口，并发送 `preview hold`、`source=browser_content`。原生随后返回 `INVALID_ARGUMENT`。不带窗口时的错误来自更早的 MCP 参数检查，两者发生在不同层。

0.6.17 新增网页预览时只实现了 RequestCoordinator 处理器，漏更新 NativeProtocol.generated.swift、protocol.schema.json 和 RequestAdmission.swift。新 hold 的 `leaseId` 首先被通用字段白名单拒绝；即使单独放行该字段，`browser_content` 源枚举及后续 browserRenew/browserFrame 动作也仍会被拒绝。这是实现遗漏，不应通过应用共享绕开。

此前 Python 测试模拟 native request，Swift 测试只覆盖了旧系统共享准入及新源枚举；打包核验覆盖组件签名、源码一致性和只读初始化，没有让真实生产器消息经过 Swift 准入，所以未发现该入口故障。0.6.17 不具备已验收的网页预览链路。

## 修复

- 同步原生字段、源和动作枚举，补齐网页 hold、续租、清屏、送帧准入。
- 按动作校验字段集合；系统源不接受网页 leaseId，网页源必须带非空 leaseId。帧身份字段、序号和大小受限，data/capturedAt 必须成对，禁止跨动作附加目标字段。
- 保留原生处理器的连接所有权、活跃租约、身份、帧龄、实际 JPEG 类型及尺寸检查，不因放行协议扩大捕获或输入权限。
- candidates 返回来自有效授权会话真实握手的 capabilities；失效或其他会话返回空列表，便于直接验证 browser_preview。

## 回归验证

- 新增跨语言契约测试：调用实际 Python BrowserPreview 生产器，收集 hold → 清屏 → 续租 → 送帧 → release 五条消息，逐条送入实际 Swift RequestParameters。无需浏览器、原生服务或预览面板。旧准入版本会在首条消息失败。
- 新增合法/非法网页准入覆盖，保留旧系统共享参数覆盖；缺租约、系统源夹带租约、帧时间缺失、分数序号、额外窗口字段及超大数据均拒绝。
- Swift 287 项通过（跳过 sandbox 不可用的 clipboardPaste）；MCP 363 项通过，含候选能力的授权/会话隔离测试。
- Release 编译及新包信息见 [0.6.18 发布说明](../releases/0.6.18.zh.md)。跨语言准入契约在源码测试中执行；实际包另核对源码一致性与组件签名，不将签名有效代替链路测试。

后续真实离屏画面验收已通过：刷新后，同一任务、同一租约内完成初始 36、导航至 37、滚动更新及释放，人工确认画面与关闭状态，begin→end 70.8 秒。详见[验收记录](2026-11-11-browser-preview-acceptance.zh.md)，覆盖边界以该记录为准。
