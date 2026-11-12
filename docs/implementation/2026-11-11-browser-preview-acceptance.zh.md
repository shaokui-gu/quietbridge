# 0.6.18 网页内容预览验收通过

2026-11-11，用户转交 WorkBuddy 的刷新后验收报告：0.6.18（101）及扩展 0.4.9 在同一任务、同一租约内完成初始画面、36→37 导航及一次滚动，三次人工观察均通过。begin→end 报告耗时 70.8 秒。本记录依据用户提供的工具结果与即时人工观察整理，未重新操作浏览器或独立重放验收。

## 目标与运行状态

- taskId：`d580629f-2fc0-4083-98dc-c936dd6c9797`。
- 原生 windowId：`207258`，pid `97718`；browserWindowId：`57074807`；tabId：`57074808`。
- 运行时 connector：`0.6.18 / ready / pid 32258`；bundle：0.6.18（101）。
- 真实握手扩展 0.4.9，capabilities 包含 `browser_preview`，nativeConnection 为 connected。
- bind 为 attached_existing；assignmentVersion 为 4；verify_mapping 返回 verified=true、method=title。

## 通过证据

| 检查 | 工具证据 | 人工观察 |
| --- | --- | --- |
| 建立共享 | share 带 appHandle、windowId、source=window 成功；租约 1800 秒、holding=true；新源 browser_content、实际范围 web_content | 面板显示“网页内容 · 定时帧（非整窗视频）”，数字 36 |
| 同租约导航 | 新 documentId `40B9CEE0…`、navigationEpoch=2；read 确认 STEP 37，导航耗时 1581.9 ms | 面板显示 37，非旧画面、非纯色 |
| 滚动更新 | beforeY=0、afterY=240、moved=true、maxY=2588；耗时 863.7 ms | 画面跟随滚动 |
| 焦点 | 无 activate/raise、切 Space、原生键鼠、screenshot/render_probe；焦点窗口未变 | — |
| 释放 | end 返回 previewCleanup=released，holding=false、visible=false、streamReady=false；幂等 unshare 同样返回已释放 | 面板关闭，5 秒未复活 |

共享范围保持单窗口 207258，未扩大为 application/desktop。15:38:44 begin，约 15:38:56 share 成功，15:39:56 end（报告给出的时间为近似值，精确耗时采用报告的 70.8 秒）。

首次 navigate 因前轮遗留 outcome_unknown 被“上一个动作尚未用 snapshot 复核”闸门阻止；只读 snapshot 一次后解除，随后正常导航。该闸门未被绕过，不归为本轮预览缺陷。

刷新后 documentId 从 `1791E42A…` 变为 `9EF94374…`，报告中 `POPUP_GUARD_LEGACY` 未再出现。0.6.17 的参数拒绝及后续租约/送帧阻断本轮均未复现；通过结论明确限定在刷新后的运行状态。

## 覆盖边界

本轮确认离屏单窗口、单标签的建立共享、初始帧、跨文档导航更新、滚动更新、不抢焦点及结束释放主链路通过。它是周期网页内容帧，不是整窗视频。

未覆盖长时多轮漂移、跨标签切换、高频滚动，以及未刷新旧文档的升级兼容行为；不据此宣称这些场景已通过。既有打包核验中的“未安装”是构建时状态，本轮运行版本证据另行确认实际运行 0.6.18（101）。
