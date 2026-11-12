# QuietBridge 0.6.19 长跑能力验收

- 时间：2026-11-12 13:01–13:20（Asia/Shanghai），实际挂机 19 分钟
- 版本：App 0.6.19（102）/ 扩展 0.4.10
- 环境：macOS 26，Chrome pid 97718，测试窗口 253859，标签 57074970，本地 fixture `127.0.0.1:48764`
- 结论：**发现 1 个阻塞级缺陷、2 个稳定性缺陷、2 个资源泄漏**；只读链路基本可用，写入链路不可用。

## 一、能力矩阵

| 能力 | 结果 | 证据 |
|---|---|---|
| `task begin/bind/status/end` | 通过 | 4 个阶段全部正常，回执含 routing、powerGuard、previewCleanup |
| `apps list/get` | 通过 | 按 PID 取 handle 正常；bundleId 仍报 `AMBIGUOUS_APP`（符合设计） |
| `windows list/share/unshare` | 通过（原生）/ 被拒（网页） | 原生 share 成功；网页 share 被 `BROWSER_MAPPING_REQUIRED` 拒绝 |
| `permissions status` | 通过 | accessibility/screenRecording/browserChannel 全 True |
| `situation` | 通过 | 返回 AX、预览生命周期、Space 归属 |
| `state`（AX） | 通过（窗口可见时） | 可见时返回 97 节点完整树；离屏后返回空树 |
| `visual_state` | 通过 | 三次取帧成功，帧龄 103–145ms，画面正确 |
| `browser_events candidates/assign/tabs/status/result` | 通过 | 候选 5 个、assign v2–v5、tabs 一致 |
| `navigate` | 通过 | 3 次，1.7–2.2s，epoch 递增，documentId 更替 |
| `read`（content） | 基本通过 | 9 次中 8 次成功（0.42–0.89s），1 次 `expired` |
| `snapshot` | 通过 | 5 次成功，0.38–3.8s，元素 ref 稳定 |
| `diagnostics` | 通过但慢 | 成功，0.59s → 5.9s，延迟劣化 10 倍 |
| `fill` | **失败** | `BROWSER_LIFECYCLE_TIMEOUT: guard_install 未完成`（4.7s），复测 `expired` |
| `click`（DOM） | **失败** | `expired`（8s TTL 内未执行），复核页面无变化 |
| `verify_mapping` | **失败（阻塞）** | `method=title, reason=no_match, candidates=0` |
| `annotate` | 正确拒绝 | desktop 阶段派发浏览器操作返回 `TASK_ROUTE_MISMATCH`，符合设计 |
| 原生 `click`（坐标） | 通过 | `dispatched=true, focusPreserved=true`，前台 PID 未变 |
| 原生 `scroll` | 派发成功、无法核实 | `foundScrollBar=false, scrollBarMoved=false`，窗口离屏 |
| `browser_session list` | 通过（暴露泄漏） | 返回 3 个同窗口 attached 会话 |
| `render_probe` / `select_tab` / `drag` / `set_value` / `paste` / `type_text` | 未覆盖 | 前两项需浏览器阶段；后四项在 desktop 阶段按设计拒绝（`UNSAFE_WINDOW_DELIVERY`） |

工具调用总计 75 次，成功 68 次（91%）；命令级失败 4 次（2 expired、1 lifecycle timeout、1 stale snapshot）。

## 二、缺陷

### P0：窗口离屏时 `verify_mapping` 失败，网页预览无法建立
- 现象：窗口 253859 在别的 Space 时，`verify_mapping` 返回 `method=title`、`reason=no_match`、`candidates=0`；随后 `share` 被 `BROWSER_MAPPING_REQUIRED` 硬拒绝。
- 对照：窗口可见时同一接口返回 `method=window_active_title`、`candidates=1`、`verified=true`。
- 根因推断：`window_active_title` 证据依赖可读的原生窗口标题；窗口离屏后 AX 树为空，降级到精确标题匹配，而 macOS Chrome 窗口标题恒带 `" - Google Chrome - <profile>"` 后缀，与标签 title 不等，必然 `no_match`。
- 影响：**跨 Space 这一核心场景无法新建预览**；已有共享可以续用，但一旦 end 就再也建不回来。
- 建议：降级路径改为「窗口标题包含标签标题」的前缀/子串匹配；或在离屏时改用 `windowId ↔ browserWindowId` 的进程代际证据，不依赖 AX 标题。

### P1：输入命令偶发丢投递
- `fill` 在 4.7s 报 `guard_install 未完成`，复测 8s `expired`；`click` 同样 `expired`；期间只读 `snapshot`/`read` 正常。
- 复核：AX 输入框 `value=""`、截图无变化，确认未产生副作用。
- 关联证据：`native_host.py` 同时存在 3 个进程；扩展 MV3 service worker 冷启动 + native 连接重建，命令在队列里等超过 8 秒 TTL。
- 建议：命令 TTL 与唤醒预算分离（唤醒期不计入 TTL）；`guard_install` 失败要回滚队列而不是让后续命令排到过期。

### P2：只读命令延迟抖动
- `diagnostics` 从 0.59s 涨到 5.9s，`snapshot` 从 0.38s 涨到 3.8s，同一任务内无代码变更。
- 与 P1 同源：SW 唤醒 + 队列积压。

### P3：browser session 泄漏
- `task end` 后 `browser_session list` 仍返回 3 个 `attached` 会话（`0CFA063A`/`7A32052D`/`56339B91`），全部指向同一 pid 97718 + 窗口 253859。
- 每 `begin`+`bind` 新增一个，`end` 不回收。长期运行会累积到 `TASK_IN_USE`。
- 建议：`task end` 一并释放该 task 创建的 session；`list` 对同 pid+window 去重。

### P4：`appHandle` 随窗口重建失效
- Chrome 重建窗口后，旧 `appHandle` 报 `UNKNOWN_APP_HANDLE: 不在本会话缓存中（可能 helper 重启过）`，需重新 `apps get`。
- 影响：多步流程中途需要额外一次调用，且错误文案把「窗口重建」误归因为「helper 重启」。
- 建议：handle 失效时自动按 pid+processIdentity 重建，或在文案中列出真实原因。

## 三、设计行为（非缺陷，但影响长跑）

- **120 秒无实际界面活动会中断任务**（`TASK_INTERRUPTED`）。本轮在 13:20 因两轮探测间隔累计超 120s 被回收，无法继续挂满 30 分钟。长任务必须每 100 秒内做一次有意义的界面动作。
- 状态查询与心跳不续期，这一点在 11-09 已确认。
- desktop 阶段拒绝 `type_text`/`key`（`UNSAFE_WINDOW_DELIVERY`），网页输入只能走扩展 `fill`——而 `fill` 当前不可用，等于写入路径整体不可用。

## 四、结论

- 可放心使用：导航、正文读取、快照、预览帧、原生坐标点击、任务生命周期。
- 不可用：网页表单填写、网页点击、离屏窗口的预览重建。
- 长跑前必须先修 P0/P1，否则任何超过 2 分钟的后台任务都会在第一次空闲后失效。
