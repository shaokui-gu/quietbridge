// Streams the acted-on window through ScreenCaptureKit into a non-activating video panel.
// No screenshot polling or synthetic desktop compositing. Anchored to the host application.
import AppKit
import ScreenCaptureKit
import AVFoundation
import CoreMedia

enum PreviewSource: String {
    case window, application, desktop

    var usesDisplay: Bool { self != .window }
    var scope: String {
        switch self {
        case .window: "window"
        case .application: "application_current_space"
        case .desktop: "display_current_space"
        }
    }
    var label: String {
        switch self {
        case .window: "窗口共享"
        case .application: "应用共享"
        case .desktop: "桌面共享"
        }
    }
}

private final class VideoSample: @unchecked Sendable {
    let buffer: CMSampleBuffer
    init(_ buffer: CMSampleBuffer) { self.buffer = buffer }
}

private final class PreviewStreamOutput: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let receive: @Sendable (VideoSample) -> Void
    let failed: @Sendable (String) -> Void
    init(receive: @escaping @Sendable (VideoSample) -> Void, failed: @escaping @Sendable (String) -> Void) {
        self.receive = receive; self.failed = failed
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, buffer.isValid, CMSampleBufferGetImageBuffer(buffer) != nil else { return }
        let info = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]]
        guard (info?.first?[.status] as? NSNumber)?.intValue == SCFrameStatus.complete.rawValue else { return }
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: true) as? [NSMutableDictionary] {
            attachments.first?[kCMSampleAttachmentKey_DisplayImmediately] = true
        }
        receive(VideoSample(buffer))
    }
    func stream(_ stream: SCStream, didStopWithError error: any Error) { failed(error.localizedDescription) }
}

@MainActor
final class SpacePreview {
    static let shared = SpacePreview()

    private let baseWidth: CGFloat = 380
    private let minWidth = PreviewResizeGeometry.minWidth
    private let maxWidth = PreviewResizeGeometry.maxWidth
    /// 面板一旦真正出现就按“界面操作任务”续租：默认十分钟内没有新动作才关闭，
    /// 期间每次动作都会续期。任务结束由 release 立即关闭。
    static let defaultHoldSeconds: TimeInterval = 600
    static let maximumHoldSeconds: TimeInterval = 3600
    private let captureInterval: TimeInterval = 0.6
    private let idleCloseInterval: TimeInterval = 30
    private let titleHeight = PreviewResizeGeometry.titleHeight

    private var panel: PreviewPanel?
    private var view: PreviewView?
    private var timer: Timer?
    private var target: (pid: Int32, windowID: UInt32)?
    private var canvas: CGRect = CGRect(x: 0, y: 0, width: 2560, height: 1440)
    private var width: CGFloat = 380
    private var stream: SCStream?
    private var streamOutput: PreviewStreamOutput?
    private var generation = UUID()
    private var streamStarting = false
    private var streamStarted = false
    private var streamError: String?
    private var lastFrameTime: TimeInterval?
    private var frameCount = 0
    private var userMoved = false
    private var lastHostBounds: CGRect?
    private var visible = false
    private var lastActivity: TimeInterval = 0
    private var hostWindow: PreviewHostWindow?
    private var hostSpaceIDs: [UInt64] = []
    private var panelSpaceIDs: [UInt64] = []
    private var panelSpaceVerified = false
    private var panelPlacementError: String?
    private var workspaceObserver: NSObjectProtocol?
    private var displayObserver: NSObjectProtocol?
    private var lastRouteRecord: JSONValue?
    private let panelTrace = OperationTrace(requestID: "preview-panel", method: "preview")
    private var spaces = WindowSpaces()
    private var holdDeadline: TimeInterval?
    private var holdSeconds: TimeInterval = SpacePreview.defaultHoldSeconds
    /// 桌面共享只捕获目标显示器当前可见的 Space，不捕获后台 Space。
    private var source: PreviewSource = .window
    private var captureSource: PreviewSource?
    private var waitingForTargetDesktop = false
    private var displayID: CGDirectDisplayID?
    /// 租约结束时通知守护进程重新判断是否该退出（没有客户端时才真正退出）。
    var onHoldEnded: (() -> Void)?

    /// 按任务跟踪目标；显式桌面共享不因目标位于当前 Space 而隐藏。
    func track(pid: Int32, windowID: UInt32, source: PreviewSource? = nil) {
        // 未知 Space 不等于异 Space；绑定前确认窗口归属和 host 当前可显示。
        guard NativePlatform.sessionLocked() == false, CGPreflightScreenCaptureAccess(),
              NativePlatform.windows().contains(where: { $0.windowID == windowID && $0.pid == pid }) else {
            stop()
            return
        }
        // 普通动作只续租；目标改变时默认收窄到应用，不继承另一应用的桌面范围。
        let sameTarget = target?.pid == pid && target?.windowID == windowID
        let nextSource = source ?? (sameTarget ? self.source : .application)
        let bounds = Self.windowBounds(windowID)
        let nextDisplay = bounds.flatMap { Self.displayID(containing: CGPoint(x: $0.midX, y: $0.midY)) }
        let changed = !sameTarget || self.source != nextSource
            || (nextSource.usesDisplay && displayID != nextDisplay)
        if changed { teardown() }
        else if streamError != nil, !waitingForTargetDesktop { resetCapture() }
        target = (pid, windowID)
        self.source = nextSource
        let now = ProcessInfo.processInfo.systemUptime
        lastActivity = now
        // 每个动作都续租，避免任务中途因为两次操作间隔而关掉共享面板。
        if holdDeadline != nil { holdDeadline = now + holdSeconds }
        displayID = nextDisplay
        if self.source.usesDisplay {
            if let displayID, let screen = Self.displayBounds(displayID) { canvas = screen }
        } else if let bounds { canvas = bounds }
        start()
    }

    /// Explicitly keep the panel (and the daemon owning it) alive for `seconds`.
    @discardableResult
    func hold(seconds: TimeInterval) -> TimeInterval {
        let clamped = min(max(seconds, 60), SpacePreview.maximumHoldSeconds)
        holdSeconds = clamped
        let now = ProcessInfo.processInfo.systemUptime
        holdDeadline = now + clamped
        lastActivity = now
        if target != nil { start() }
        Self.debug("hold \(clamped)s target=\(String(describing: target))")
        return clamped
    }

    var streamReady: Bool {
        streamStarted && !streamStarting && streamError == nil && lastFrameTime != nil
            && stream != nil && view?.video.status != .failed
            && NativePlatform.sessionLocked() == false && CGPreflightScreenCaptureAccess()
    }

    /// 复用同一条流等待真实首帧，不重启捕获，也不操作目标窗口。
    func waitUntilReady(checkCancellation: () throws -> Void) async throws {
        let token = generation
        var succeeded = false
        defer {
            // 只清理本次等待的失败流，不能关闭重入请求已建立的新共享。
            if !succeeded, generation == token, !waitingForTargetDesktop { stop() }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + 3
        while true {
            try checkCancellation()
            try Task.checkCancellation()
            guard generation == token, target != nil,
                  NativePlatform.sessionLocked() == false, CGPreflightScreenCaptureAccess() else {
                throw NativeFailure(code: "SHARE_NOT_READY", message: "共享目标、屏幕录制授权或解锁状态已不可用")
            }
            if streamReady {
                guard let target,
                      NativePlatform.windows().contains(where: { $0.pid == target.pid && $0.windowID == target.windowID }) else {
                    throw NativeFailure(code: "SHARE_NOT_READY", message: "共享就绪前目标进程或窗口已不可用")
                }
                succeeded = true
                return
            }
            if let streamError {
                throw NativeFailure(code: "SHARE_NOT_READY", message: "共享未就绪：\(streamError)")
            }
            guard ProcessInfo.processInfo.systemUptime < deadline else {
                throw NativeFailure(code: "SHARE_NOT_READY", message: "共享在 3 秒内未完成启动并收到首帧")
            }
            try await Task.sleep(for: .milliseconds(25))
        }
    }

    /// Task finished or the person asked to stop sharing: close immediately.
    func release() {
        Self.debug("release")
        stop()
    }

    var isHolding: Bool {
        guard let holdDeadline else { return false }
        return ProcessInfo.processInfo.systemUptime < holdDeadline
    }

    var holdRemainingSeconds: Double? {
        guard let holdDeadline else { return nil }
        return max(0, holdDeadline - ProcessInfo.processInfo.systemUptime)
    }

    func stop() {
        teardown()
        clearHold()
        hostWindow = nil
        lastHostBounds = nil
        userMoved = false
    }

    /// Stop tracking without ending an active share lease (for example when switching targets).
    private func teardown() {
        timer?.invalidate()
        timer = nil
        if let workspaceObserver { NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver) }
        if let displayObserver { NotificationCenter.default.removeObserver(displayObserver) }
        workspaceObserver = nil
        displayObserver = nil
        target = nil
        captureSource = nil
        displayID = nil
        waitingForTargetDesktop = false
        hostSpaceIDs = []
        panelSpaceIDs = []
        panelSpaceVerified = false
        panelPlacementError = nil
        resetCapture()
        panel?.alphaValue = 0
        panel?.orderOut(nil)
        visible = false
    }

    /// Reconfigure capture without changing either the target identity or the preview host.
    private func resetCapture() {
        generation = UUID()
        streamStarting = false
        streamStarted = false
        streamError = nil
        lastFrameTime = nil
        frameCount = 0
        if let stream { Task { try? await stream.stopCapture() } }
        stream = nil
        streamOutput = nil
        view?.video.flushAndRemoveImage()
        view?.mirrorCursor(nil, canvas: canvas)
    }

    private func clearHold() {
        guard holdDeadline != nil else { return }
        holdDeadline = nil
        onHoldEnded?()
    }

    private func start() {
        ensurePanel()
        guard panel != nil else { return }
        if workspaceObserver == nil {
            workspaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main
            ) { [weak self] _ in Task { @MainActor in self?.tick() } }
        }
        if displayObserver == nil {
            displayObserver = NotificationCenter.default.addObserver(
                forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
            ) { [weak self] _ in Task { @MainActor in self?.tick() } }
        }
        timer?.invalidate()
        let timer = Timer(timeInterval: captureInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        tick()
    }

    private func tick() {
        guard let target else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if let deadline = holdDeadline, now >= deadline {
            Self.debug("hold expired")
            stop()
            return
        }
        guard NativePlatform.sessionLocked() == false, CGPreflightScreenCaptureAccess() else {
            stop()
            return
        }
        guard let targetBounds = Self.windowBounds(target.windowID) else {
            stop()
            return
        }
        let nextDisplay = Self.displayID(containing: CGPoint(x: targetBounds.midX, y: targetBounds.midY))
        if nextDisplay != displayID {
            displayID = nextDisplay
            if captureSource?.usesDisplay == true { resetCapture() }
        }
        let membership = spaces.memberships(windowIDs: [target.windowID])[target.windowID]
        switch PreviewCaptureRoute.decide(source: source, targetOnCurrentSpace: membership?.onCurrentSpace) {
        case .capture(let nextSource):
            if captureSource != nextSource || waitingForTargetDesktop { resetCapture() }
            captureSource = nextSource
            waitingForTargetDesktop = false
            if nextSource.usesDisplay {
                if let displayID, let bounds = Self.displayBounds(displayID) { canvas = bounds }
            } else { canvas = targetBounds }
            if stream == nil, !streamStarting, streamError == nil { beginStream(pid: target.pid, windowID: target.windowID) }
        case .waitingForTargetDesktop:
            if captureSource != nil { resetCapture() }
            captureSource = nil
            waitingForTargetDesktop = true
            let message = "目标桌面不在显示器当前 Space，保留绑定并等待返回"
            streamError = message
            view?.status = message
        }
        let host = resolveHostWindow()
        let hostMembership = host.flatMap { spaces.memberships(windowIDs: [$0.windowID])[$0.windowID] }
        hostSpaceIDs = hostMembership?.spaceIDs ?? []
        let leased = holdDeadline != nil
        var shouldShow = false
        if let panel, let host,
           case .host(let destination) = PreviewHostRoute.decide(host: host, membership: hostMembership,
                held: leased, source: source, targetOnCurrentSpace: membership?.onCurrentSpace) {
            let panelID = UInt32(exactly: panel.windowNumber)
            let actual = panelID.flatMap { spaces.memberships(windowIDs: [$0])[$0]?.spaceIDs }
            if !CursorSpaceRoute.matches(actual, target: destination) { panel.alphaValue = 0 }
            if syncAnchor(panel: panel, host: host) {
                shouldShow = assignPanel(panel, to: destination, host: host)
            } else { panelSpaceVerified = false }
        } else {
            panelSpaceVerified = false
            panelPlacementError = host == nil ? "宿主窗口不可用" : "宿主不可见或 Space 归属未确认"
        }
        recordPanelRoute()
        if shouldShow && !visible {
            // Time spent hidden does not count toward the idle timeout.
            lastActivity = now
        }
        // Safety valve: a preview hidden for many minutes must not track forever,
        // unless a UI task still holds the lease.
        if !shouldShow, holdDeadline == nil, now - lastActivity > idleCloseInterval * 20 {
            stop()
            return
        }
        setVisible(shouldShow)
        if shouldShow, holdDeadline == nil {
            // 面板出现过就默认续租：不再“每个动作显示几秒就关掉”。
            holdSeconds = SpacePreview.defaultHoldSeconds
            holdDeadline = now + holdSeconds
            Self.debug("auto hold \(holdSeconds)s")
        }
        guard shouldShow else { return }
        if holdDeadline == nil, now - lastActivity > idleCloseInterval {
            stop()
            return
        }
    }

    private func beginStream(pid: Int32, windowID: UInt32) {
        guard !streamStarting else { return }
        streamStarting = true
        streamStarted = false
        streamError = nil
        lastFrameTime = nil
        frameCount = 0
        let token = generation
        guard let source = captureSource else { streamStarting = false; return }
        let wantedDisplay = displayID
        view?.status = "正在连接\(source.label)…"
        Task { @MainActor in
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                guard generation == token else { return }
                guard let window = content.windows.first(where: { $0.windowID == windowID && $0.owningApplication?.processID == pid }) else {
                    throw NativeFailure(code: "WINDOW_UNAVAILABLE", message: "共享窗口已不可用")
                }
                let config = SCStreamConfiguration()
                let filter: SCContentFilter
                // 只共享精确显示器当前 Space，排除 helper 自有预览和浮层以免递归。
                if source.usesDisplay {
                    guard let wantedDisplay,
                          let display = content.displays.first(where: { $0.displayID == wantedDisplay }) else {
                        throw NativeFailure(code: "SHARE_NOT_READY", message: "目标显示器不可用，未切换到其他显示器或窗口")
                    }
                    if source == .application {
                        guard let application = content.applications.first(where: { $0.processID == pid }), pid != getpid() else {
                            throw NativeFailure(code: "SHARE_NOT_READY", message: "无法确认目标应用，未扩大共享范围")
                        }
                        filter = SCContentFilter(display: display, including: [application], exceptingWindows: [])
                    } else {
                        guard spaces.memberships(windowIDs: [windowID])[windowID]?.onCurrentSpace == true else {
                            throw NativeFailure(code: "SHARE_NOT_READY", message: "目标不在显示器当前桌面，请先手动切换到目标桌面")
                        }
                        let ownApplications = content.applications.filter { $0.processID == getpid() }
                        guard !ownApplications.isEmpty else {
                            throw NativeFailure(code: "SHARE_NOT_READY", message: "无法确认 helper 自有浮层排除范围，未启动可能递归的桌面流")
                        }
                        filter = SCContentFilter(display: display, excludingApplications: ownApplications, exceptingWindows: [])
                    }
                    config.width = max(1, display.width)
                    config.height = max(1, display.height)
                } else {
                    filter = SCContentFilter(desktopIndependentWindow: window)
                    config.width = max(1, Int(window.frame.width))
                    config.height = max(1, Int(window.frame.height))
                }
                config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
                config.queueDepth = 3
                config.showsCursor = false
                let output = PreviewStreamOutput(receive: { sample in
                    Task { @MainActor in
                        let owner = SpacePreview.shared
                        guard owner.generation == token, let view = owner.view else { return }
                        if view.video.status == .failed { view.video.flush() }
                        if view.video.isReadyForMoreMediaData { view.video.enqueue(sample.buffer) }
                        owner.frameCount += 1
                        owner.lastFrameTime = ProcessInfo.processInfo.systemUptime
                        view.status = "workbuddy-cua · \(source.label)"
                    }
                }, failed: { message in
                    Task { @MainActor in
                        guard SpacePreview.shared.generation == token else { return }
                        let owner = SpacePreview.shared
                        owner.streamStarted = false
                        owner.streamStarting = false
                        owner.streamError = message
                        owner.lastFrameTime = nil
                        owner.view?.status = "共享已中断：\(message)"
                        owner.view?.video.flushAndRemoveImage()
                    }
                })
                let next = SCStream(filter: filter, configuration: config, delegate: output)
                try next.addStreamOutput(output, type: .screen, sampleHandlerQueue: DispatchQueue(label: "cua.preview.video"))
                stream = next
                streamOutput = output
                try await next.startCapture()
                guard generation == token else { try? await next.stopCapture(); return }
                streamStarting = false
                streamStarted = streamError == nil
                if let panel {
                    let height = width * canvas.height / max(1, canvas.width) + titleHeight
                    panel.setFrame(NSRect(x: panel.frame.minX, y: panel.frame.maxY - height, width: width, height: height), display: true)
                    if let hostWindow { _ = syncAnchor(panel: panel, host: hostWindow) }
                }
            } catch {
                guard generation == token else { return }
                streamStarting = false
                streamStarted = false
                streamError = (error as? NativeFailure)?.message ?? error.localizedDescription
                lastFrameTime = nil
                if let stream { Task { try? await stream.stopCapture() } }
                stream = nil
                streamOutput = nil
                view?.status = "无法共享：\(streamError ?? "未知错误")"
                Self.debug("stream failed: \(error)")
            }
        }
    }

    private func ensurePanel() {
        guard panel == nil else { return }
        if NSApp.activationPolicy() != .accessory { NSApp.setActivationPolicy(.accessory) }
        let initial = NSRect(x: 0, y: 0, width: width, height: width * 0.65 + titleHeight)
        let panel = PreviewPanel(contentRect: initial, styleMask: [.borderless, .nonactivatingPanel],
                                 backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.alphaValue = 0
        panel.backgroundColor = .clear
        panel.hasShadow = true
        // The panel is never key: mouse-moved tracking is what turns the pointer into
        // a resize arrow at the card edges.
        panel.acceptsMouseMovedEvents = true
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.floatingWindow)))
        // Membership is explicitly kept equal to the bound WorkBuddy window below.
        // Never join every Space or activate the host to bring the preview along.
        panel.collectionBehavior = [.stationary, .fullScreenAuxiliary, .ignoresCycle]
        let view = PreviewView(frame: initial)
        view.delegate = self
        panel.contentView = view
        self.panel = panel
        self.view = view
    }

    private func setVisible(_ next: Bool) {
        guard let panel else { return }
        if next {
            if !panel.isVisible { panel.orderFrontRegardless() }
            panel.alphaValue = 1
        } else {
            panel.alphaValue = 0
            if panel.isVisible { panel.orderOut(nil) }
        }
        visible = next
    }

    /// Move only the transparent, helper-owned panel, then verify both memberships again.
    private func assignPanel(_ panel: NSPanel, to destination: [UInt64], host: PreviewHostWindow) -> Bool {
        if panel.windowNumber <= 0 { panel.alphaValue = 0; panel.orderFrontRegardless() }
        guard let panelID = UInt32(exactly: panel.windowNumber), panelID > 0 else {
            panelSpaceVerified = false
            panelPlacementError = "预览面板窗口号不可用"
            return false
        }
        panelSpaceIDs = spaces.memberships(windowIDs: [panelID])[panelID]?.spaceIDs ?? []
        if !CursorSpaceRoute.matches(panelSpaceIDs, target: destination) || !panel.isVisible {
            panel.alphaValue = 0
            panel.orderFrontRegardless()
            guard spaces.assignOverlay(panel, to: destination) else {
                panelSpaceVerified = false
                panelPlacementError = "预览面板迁移未通过 Space 回读验证"
                return false
            }
        }
        let after = spaces.memberships(windowIDs: [host.windowID, panelID])
        panelSpaceIDs = after[panelID]?.spaceIDs ?? []
        guard CursorSpaceRoute.decide(windowID: host.windowID, membership: after[host.windowID]) == .target(destination),
              CursorSpaceRoute.matches(panelSpaceIDs, target: destination) else {
            panelSpaceVerified = false
            panelPlacementError = "宿主或预览面板在迁移期间改变了 Space"
            return false
        }
        panelSpaceVerified = true
        panelPlacementError = nil
        return true
    }

    /// A manual offset survives host movement; dragging never disables Space tracking.
    @discardableResult private func syncAnchor(panel: NSPanel, host: PreviewHostWindow) -> Bool {
        guard let display = Self.displayID(containing: CGPoint(x: host.bounds.midX, y: host.bounds.midY)),
              let screen = NSScreen.screens.first(where: {
                  ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == display
              }) else {
            panelPlacementError = "宿主显示器不可用，未改为跟随当前焦点显示器"
            return false
        }
        if userMoved, host.bounds == lastHostBounds { return true }
        let next = PreviewHostGeometry.frame(for: panel.frame, hostBounds: host.bounds,
            previousHostBounds: lastHostBounds, manuallyPositioned: userMoved,
            mainDisplayHeight: NSScreen.screens.first?.frame.maxY ?? 0, visibleFrame: screen.visibleFrame)
        if panel.frame != next { panel.setFrame(next, display: true) }
        lastHostBounds = host.bounds
        return true
    }

    private func recordPanelRoute() {
        let fields: [String: JSONValue] = [
            "trackingWindowId": target.map { .number(Double($0.windowID)) } ?? .null,
            "hostWindowId": hostWindow.map { .number(Double($0.windowID)) } ?? .null,
            "hostPid": hostWindow.map { .number(Double($0.pid)) } ?? .null,
            "hostSpaceIds": .array(hostSpaceIDs.map { .number(Double($0)) }),
            "panelSpaceIds": .array(panelSpaceIDs.map { .number(Double($0)) }),
            "panelSpaceVerified": .bool(panelSpaceVerified),
            "placementError": panelPlacementError.map(JSONValue.string) ?? .null,
        ]
        let record = JSONValue.object(fields)
        guard record != lastRouteRecord else { return }
        lastRouteRecord = record
        panelTrace.mark("preview.hostRoute", fields: fields)
    }

    /// 共享端只消费真实浮层的同一帧，不拥有另一个位置、动画或空闲计时器。
    func mirrorCursor(_ frame: CursorFrame?) {
        guard let frame, target?.windowID == frame.windowID else {
            view?.mirrorCursor(nil, canvas: canvas)
            return
        }
        view?.mirrorCursor(frame, canvas: canvas)
    }

    func diagnostics() -> [String: Any] {
        let membership = target.flatMap { spaces.memberships(windowIDs: [$0.windowID])[$0.windowID] }
        let hostMembership = hostWindow.flatMap { spaces.memberships(windowIDs: [$0.windowID])[$0.windowID] }
        let panelMembership = panel.flatMap { UInt32(exactly: $0.windowNumber) }
            .flatMap { spaces.memberships(windowIDs: [$0])[$0] }
        return ["videoFrames": frameCount, "visible": visible && panelMembership?.onCurrentSpace == true, "status": view?.status ?? "",
         "scope": captureSource?.scope ?? source.scope,
         "targetOnCurrentSpace": membership?.onCurrentSpace as Any? ?? NSNull(),
         "streamStarting": streamStarting, "streamStarted": streamStarted, "streamReady": streamReady,
         "streamError": streamError as Any? ?? NSNull(),
         "frameAgeMs": lastFrameTime.map { max(0, (ProcessInfo.processInfo.systemUptime - $0) * 1000) } as Any? ?? NSNull(),
         "panelFrame": panel.map { NSStringFromRect($0.frame) } ?? "",
         "videoLayerReady": view?.video.isReadyForMoreMediaData ?? false,
         "videoLayerFailed": view?.video.status == .failed,
         "holding": isHolding, "holdRemainingSeconds": holdRemainingSeconds ?? 0,
         "mode": source.rawValue,
         "captureMode": captureSource?.rawValue as Any? ?? NSNull(),
         "waitingForTargetDesktop": waitingForTargetDesktop,
         "displayId": displayID.map { Int($0) } ?? 0,
         "hostWindowId": hostWindow.map { Int($0.windowID) } as Any? ?? NSNull(),
         "hostPid": hostWindow.map { Int($0.pid) } as Any? ?? NSNull(),
         "hostSpaceIds": hostSpaceIDs,
         "panelSpaceIds": panelSpaceIDs,
         "hostOnCurrentSpace": hostMembership?.onCurrentSpace as Any? ?? NSNull(),
         "panelOnCurrentSpace": panelMembership?.onCurrentSpace as Any? ?? NSNull(),
         "panelSpaceVerified": panelSpaceVerified,
         "panelPlacementError": panelPlacementError as Any? ?? NSNull(),
         "trackingWindowId": target.map { Int($0.windowID) } ?? 0]
    }

    fileprivate func noteUserMoved() { userMoved = true }

    /// Person clicked the panel's close control: end sharing immediately.
    fileprivate func userClosed() {
        Self.debug("user closed preview")
        release()
    }

    /// Picture aspect of the shared window (height / width); drives the panel height.
    fileprivate var canvasAspect: CGFloat {
        canvas.width > 0 && canvas.height > 0 ? canvas.height / canvas.width : 0.62
    }

    /// Corner/edge drag: the view has already resolved the target frame.
    fileprivate func applyResize(to frame: NSRect) {
        guard let panel else { return }
        userMoved = true
        let clamped = NSRect(x: frame.origin.x, y: frame.origin.y,
                             width: min(maxWidth, max(minWidth, frame.width)), height: frame.height)
        width = clamped.width
        panel.setFrame(clamped, display: true)
        if let view { view.window?.invalidateCursorRects(for: view) }
        view?.needsDisplay = true
        Self.debug("resize to \(NSStringFromRect(clamped))")
    }

    /// Opt-in diagnostics: CUA_PREVIEW_DEBUG=1 appends decisions to /tmp/dsh-space-preview.log.
    static func debug(_ message: String) {
        guard ProcessInfo.processInfo.environment["CUA_PREVIEW_DEBUG"] == "1" else { return }
        let path = "/tmp/dsh-space-preview.log"
        guard let data = ("\(Date().timeIntervalSince1970) \(message)\n").data(using: .utf8) else { return }
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: URL(fileURLWithPath: path))
        }
    }

    // MARK: - WindowServer queries

    /// 精确查询包含目标中心的显示器；查询失败或坐标离屏时不回退。
    private static func displayID(containing point: CGPoint) -> CGDirectDisplayID? {
        var count: UInt32 = 0
        guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return nil }
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
        guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return nil }
        return ids.prefix(Int(count)).first { CGDisplayBounds($0).contains(point) }
    }

    private static func displayBounds(_ id: CGDirectDisplayID) -> CGRect? {
        let bounds = CGDisplayBounds(id)
        return bounds.width > 0 && bounds.height > 0 ? bounds : nil
    }

    private static func windowBounds(_ windowID: UInt32) -> CGRect? {
        let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        for entry in list {
            guard (entry[kCGWindowNumber as String] as? UInt32) == windowID,
                  let dict = entry[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: dict as CFDictionary) else { continue }
            return rect
        }
        return nil
    }

    private func resolveHostWindow() -> PreviewHostWindow? {
        let selfPID = getpid()
        let workBuddyPIDs = Set(NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.workbuddy.mac")
            .map(\.processIdentifier))
        let responsible = NativePlatform.responsiblePID(selfPID)
        let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        let candidates: [PreviewHostWindow] = list.compactMap { entry in
            guard let owner = entry[kCGWindowOwnerPID as String] as? Int32, owner != selfPID,
                  workBuddyPIDs.contains(owner) || (workBuddyPIDs.isEmpty && owner == responsible),
                  (entry[kCGWindowLayer as String] as? Int) == 0,
                  let id = entry[kCGWindowNumber as String] as? UInt32,
                  let boundsDict = entry[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: boundsDict as CFDictionary),
                  rect.width > 300, rect.height > 200 else { return nil }
            return PreviewHostWindow(windowID: id, pid: owner, bounds: rect,
                isOnScreen: (entry[kCGWindowIsOnscreen as String] as? Bool) == true)
        }
        let selected = PreviewHostWindow.select(candidates, workBuddyPIDs: workBuddyPIDs,
            responsiblePID: responsible, previous: hostWindow)
        if selected?.windowID != hostWindow?.windowID || selected?.pid != hostWindow?.pid {
            lastHostBounds = nil
            userMoved = false
        }
        hostWindow = selected
        return selected
    }

}

private final class PreviewPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private final class PreviewView: NSView {
    weak var delegate: SpacePreview?
    let video = AVSampleBufferDisplayLayer()
    private let pointerView = AgentCursorView(frame: CGRect(x: 0, y: 0, width: 80, height: 80))
    private var pointerPosition: CGPoint?
    private var cursorCanvas: CGRect = .zero
    func mirrorCursor(_ frame: CursorFrame?, canvas: CGRect) {
        cursorCanvas = canvas
        pointerPosition = frame?.normalizedPoint(in: canvas)
        pointerView.isHidden = pointerPosition == nil
        pointerView.rippleProgress = frame?.rippleProgress
        pointerView.hoverPhase = frame?.hoverPhase ?? 0
        needsLayout = true
    }
    var status = "正在连接窗口共享…" { didSet { if oldValue != status { needsDisplay = true } } }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 12
        layer?.masksToBounds = true
        video.videoGravity = .resizeAspect
        layer?.addSublayer(video)
        pointerView.wantsLayer = true
        pointerView.isHidden = true
        addSubview(pointerView)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout()
        video.frame = CGRect(x: 0, y: 0, width: bounds.width, height: max(0, bounds.height - 24))
        if let pointerPosition, cursorCanvas.width > 0, cursorCanvas.height > 0 {
            // 与 resizeAspect 的实际画面同一缩放及留白，窗口 resize 后也不漂移。
            let scale = min(video.frame.width / cursorCanvas.width, video.frame.height / cursorCanvas.height)
            let picture = CGRect(x: (video.frame.width - cursorCanvas.width * scale) / 2,
                                 y: (video.frame.height - cursorCanvas.height * scale) / 2,
                                 width: cursorCanvas.width * scale, height: cursorCanvas.height * scale)
            // 面板是整屏缩略图，按原始比例指针会小到看不清；给一个最小显示比例，
            // 仍以真实落点为锚点，只放大指针本身。
            let pointerScale = max(scale, 0.85)
            pointerView.frame = CGRect(x: picture.minX + pointerPosition.x * picture.width - 30 * pointerScale,
                                       y: picture.minY + (1 - pointerPosition.y) * picture.height - 50 * pointerScale,
                                       width: 80 * pointerScale, height: 80 * pointerScale)
            pointerView.bounds = CGRect(x: 0, y: 0, width: 80, height: 80)
        }
    }
    private var dragRegion: PreviewResizeRegion = .none
    private var hoverRegion: PreviewResizeRegion = .none
    private var trackingArea: NSTrackingArea?
    private var startFrame: NSRect = .zero
    private var startMouse: NSPoint = .zero
    private let geometry = PreviewResizeGeometry()

    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        let radius: CGFloat = 12
        let titleRect = NSRect(x: 0, y: bounds.height - 24, width: bounds.width, height: 24)

        // Clip everything to one rounded card so the window shadow follows that shape.
        context.saveGState()
        context.addPath(CGPath(roundedRect: bounds, cornerWidth: radius, cornerHeight: radius, transform: nil))
        context.clip()

        context.setFillColor(NSColor(calibratedWhite: 0.08, alpha: 1).cgColor)
        context.fill(bounds)


        context.setFillColor(NSColor(calibratedWhite: 0.10, alpha: 0.90).cgColor)
        context.fill(titleRect)

        let title = status as NSString
        title.draw(in: NSRect(x: 14, y: bounds.height - 19, width: bounds.width - 40, height: 15),
                   withAttributes: [.font: NSFont.systemFont(ofSize: 11, weight: .medium),
                                    .foregroundColor: NSColor(calibratedWhite: 0.92, alpha: 1)])

        // 关闭按钮：共享面板按任务持续显示，必须让人能随时收掉。
        let close = "✕" as NSString
        close.draw(in: NSRect(x: bounds.width - 24, y: bounds.height - 19, width: 14, height: 14),
                   withAttributes: [.font: NSFont.systemFont(ofSize: 11, weight: .semibold),
                                    .foregroundColor: NSColor(calibratedWhite: 0.72, alpha: 1)])

        context.restoreGState()
    }

    // MARK: - Hit testing / drag

    private var closeRegion: NSRect {
        NSRect(x: bounds.width - 30, y: bounds.height - 24, width: 30, height: 24)
    }

    private func region(atWindowPoint pointInWindow: NSPoint) -> PreviewResizeRegion {
        geometry.region(at: convert(pointInWindow, from: nil), in: bounds)
    }

    private func updateCursor(at pointInWindow: NSPoint) {
        let region = region(atWindowPoint: pointInWindow)
        region.cursor.set()
        if region != hoverRegion {
            hoverRegion = region
            SpacePreview.debug("cursor region=\(region) point=\(pointInWindow)")
        }
    }

    /// One area for the whole card: the region maths decides what the pointer means.
    /// This panel never becomes key, so cursor rects alone would not take effect.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingArea { removeTrackingArea(trackingArea) }
        let area = NSTrackingArea(rect: .zero,
                                  options: [.mouseEnteredAndExited, .mouseMoved, .cursorUpdate,
                                            .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
        trackingArea = area
    }

    override func cursorUpdate(with event: NSEvent) {
        updateCursor(at: event.locationInWindow)
    }

    override func mouseEntered(with event: NSEvent) {
        updateCursor(at: event.locationInWindow)
    }

    override func mouseMoved(with event: NSEvent) {
        updateCursor(at: event.locationInWindow)
    }

    override func mouseExited(with event: NSEvent) {
        hoverRegion = .none
        NSCursor.arrow.set()
    }

    override func mouseDown(with event: NSEvent) {
        // The title bar carries a close control; clicking it ends the share lease.
        if closeRegion.contains(convert(event.locationInWindow, from: nil)) {
            delegate?.userClosed()
            return
        }
        let region = region(atWindowPoint: event.locationInWindow)
        guard region != .none, let window else {
            dragRegion = .none
            delegate?.noteUserMoved()
            window?.performDrag(with: event)
            return
        }
        dragRegion = region
        startFrame = window.frame
        startMouse = NSEvent.mouseLocation
        region.cursor.set()
    }

    override func mouseDragged(with event: NSEvent) {
        guard dragRegion != .none, let delegate, let window else { return }
        // Screen-space delta: the view moves under the pointer as the frame changes,
        // so view-local coordinates would feed the previous resize back in.
        let now = NSEvent.mouseLocation
        let delta = CGSize(width: now.x - startMouse.x, height: now.y - startMouse.y)
        let target = geometry.frame(for: dragRegion,
                                    startFrame: startFrame,
                                    delta: delta,
                                    aspect: delegate.canvasAspect)
        delegate.applyResize(to: target)
        dragRegion.cursor.set()
        window.invalidateCursorRects(for: self)
    }

    override func mouseUp(with event: NSEvent) {
        dragRegion = .none
        updateCursor(at: event.locationInWindow)
    }
}
