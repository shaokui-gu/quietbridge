# 2026-11-11 准备阶段阻塞复核

用户转交 Workbuddy 的准备阶段报告：0.6.14 (97) 已运行，扩展 host_unavailable、本地测试页未监听、旧目标不存在。没有创建任务/共享。Codex 继续只读排查并恢复静态测试服务。

- 本地服务已重新启动，PID 8157，监听 127.0.0.1:48763。使用关闭代理的 urllib opener 确认 HTTP 200 且包含 STEP 0。服务状态保存在 dist/off-space-preview-investigation-20261111/fixture-status.json；未修改 shell 或应用代理配置。
- 11:04–11:05，既有根 browser-events.json 中出现 0.4.6 新心跳；11:05:44 的样本年龄 0.944 秒，enabled/permission=true，application session 与内核锁仍被菜单应用持有的 session 匹配。不声称此前连接错误是误报，也未定位恢复的触发原因。
- 浏览器 native host PID 91789 经 shell 91776 由 Chrome 97718 派生。Chrome 43443 由另一个父进程启动，指定独立 ms-playwright/daemon 用户目录，不能把它仅因有可见窗口就选为当前扩展的目标。本轮未读取该实例的用户页面、cookie 或其他配置。
- Workbuddy 报告的旧窗口/标签已失效；本轮没有创建替代目标。复测应先由 Workbuddy 重新核验连接和目标，明确新建授权后仅创建一个专用测试目标，结束准备任务，再由用户摆放 Space 场景。不能在目标不存在时一边禁止准备任务、一边要求 create_target。
- 无任务创建、共享、截图、网页导航、原生输入、应用重载、开关或权限更改。最终建议：扩展实际连接的实例 + 新专用目标 + 其他 Space + 仅基线。

只读状态快照：dist/off-space-preview-investigation-20261111/preflight-readonly.json。提示词已同步修订：docs/implementation/off-space-preview-test-prompt.zh.md。
