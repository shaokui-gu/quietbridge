# Round 4 新窗口分阶段呈现复核

输入：`/Users/gushaokui/Documents/AI/linuxdo/dist/off-space-preview-investigation-20261111/round4-render-probe-newwin-20261111-1249.md`。
只读既有报告及 bridge/native 日志；没有启动任务、捕获、截图、切桌或修改运行状态。

## 结论

本轮探针成功，但没有复现此前的空白/纯色或动态页面更新故障。用户明确补充：**能看见 STEP 33 和文字，只是一直没变化**。工具时序只有一次 read 和 inspect/attach/enable/layout/capture 五阶段，没有 navigate 或其他页面更新；固定 STEP 33 静态页不变化符合预期。

上一版提示词只在截图后安排可选后续导航，且该步骤因时间阈值跳过，缺少诊断前主动改变页面的刺激。这是流程不足；不能把本轮当成画面问题已修复，也不能判断 attach、enable、layout 或 capture 哪一步能恢复故障。下一轮先导航到新STEP并核实DOM，再做布局/截图短对照。

## 原生源帧与探针

任务 `582fd5cf-f220-4b4d-af0f-592624d545c9`；原生窗口207258；浏览器窗口/标签57074807/57074808；Chrome PID97718。捕获进程为34172，单一流 `FFCEAB7C-A4AB-4C91-A53B-3A1E8C0E41E2`，与Chrome PID区分。

共17份进度样本，同一流；所有样本 targetOnCurrentSpace=false、videoLayerFailed=false。NV12 420v，2560×1327，Y/UV stride2560，709颜色元数据；固定中央稀疏探针1920×830，x320/y331。

| 阶段 | 扩展执行时间（CST） | 关联源样本 | 判读 |
|---|---|---|---|
| 基线、尚未attach | 12:47:53–12:48:20 | 中央亮度spread160、色度49–50；近纯色累计0；bodyChanges0→1 | 首次attach/capture前已有非均匀内容，人工可见33和文字 |
| inspect | 12:48:08.793–08.853 | 状态false/false→false/false | 未自动附加/启用 |
| attach | 12:48:24.318–24.482 | bodyChanges1→3，仍非均匀 | 小变化不能证明恢复或页面更新；没有新STEP刺激 |
| enable | 12:48:36.671–36.795 | bodyChanges3；近纯色0 | 已有内容，没有可证实的空白恢复 |
| layout | 12:48:51.764–51.899 | bodyChanges3；近纯色0 | 视口2560×1201，仅查询布局 |
| capture | 12:49:06.954–07.163 | bodyChanges3→5；近纯色0 | 单次结果长度26656，与本轮静态画面无更新不矛盾 |
| 最后进度 | 12:49:19.548 | complete1291、submitted1290、backpressure1；87个中央样本近纯色累计仍0 | 不支持本轮曾出现之前的中央均匀空白；enqueue不证明显示，人工观察补齐 |

固定稀疏采样不是整页内容识别，不能单凭spread判定数字33；具体数字来自报告DOM与用户观察。此轮没有保存探针图像或源像素。

## 收尾已确认

- 12:47:52.495 capturePreparing；52.629 StartRequested；52.726 StartCompleted。
- 12:49:24.641 task end开始；24.647 StopRequested；24.656 phase_ended。
- **12:49:24.681 StopCompleted，stopConfirmed=true，stopElapsedMs≈33.74。** 报告中的requested为异步完成前的回执，不是清理失败。
- 同一流无重建、无StartFailed/StopFailed/DelegateStopped；未发现本轮中途捕获停止证据。

时限口径：报告从share完成回执约12:47:56.35计，到停止请求约88.30秒；从实际captureStartRequested计约92.02秒。不是任务异常，但下一轮应从发出share前计时，75秒开始收尾，含工具等待不超过90秒，避免回执延迟造成实际共享超时。

## 交付

- `dist/off-space-preview-investigation-20261111/round4-render-probe-progress.json`：原生样本、生命周期及去重探针。
- `dist/off-space-preview-investigation-20261111/round4-command-timeline.json`：固定字段的命令时序。
- `docs/implementation/off-space-round5-dynamic-test-prompt.zh.md`：先触发真实DOM/页面变化的短流程，复用0.6.16/0.4.7，不需要重新打包。
