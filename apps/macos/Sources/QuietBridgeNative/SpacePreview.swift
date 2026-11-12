// Streams the acted-on window through ScreenCaptureKit into a non-activating video panel.
// Browser content uses bounded extension frames; native surfaces use ScreenCaptureKit.
import AppKit
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import ImageIO

enum PreviewSource: String {
    case window, application, desktop
    case browserContent = "browser_content"

    var usesDisplay: Bool { self == .application || self == .desktop }
    var scope: String {
        switch self {
        case .browserContent: "web_content"
        case .window: "window"
        case .application: "application_current_space"
        case .desktop: "display_current_space"
        }
    }
    var label: String {
        switch self {
        case .browserContent: "网页内容 · 定时帧（非整窗视频）"
        case .window: "窗口共享"
        case .application: "应用共享"
        case .desktop: "桌面共享"
        }
    }

    func configureFraming(_ configuration: SCStreamConfiguration) {
        guard self == .window else { return }
        // Capture only the selected window, without ScreenCaptureKit's extra
        // window framing or a white backing in its transparent corners.
        configuration.ignoreShadowsSingleWindow = true
        configuration.ignoreGlobalClipSingleWindow = true
        configuration.shouldBeOpaque = false
    }
}

private final class VideoSample: @unchecked Sendable {
    let buffer: CMSampleBuffer
    init(_ buffer: CMSampleBuffer) { self.buffer = buffer }
}

private final class PreviewStreamOutput: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let progress = PreviewFrameProgress()
    let receive: @Sendable (VideoSample) -> Void
    let failed: @Sendable (any Error) -> Void
    init(receive: @escaping @Sendable (VideoSample) -> Void, failed: @escaping @Sendable (any Error) -> Void) {
        self.receive = receive; self.failed = failed
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let info = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]]
        progress.recordCallback(status: (info?.first?[.status] as? NSNumber)?.intValue, now: now)
        guard buffer.isValid, let pixels = CMSampleBufferGetImageBuffer(buffer) else { return }
        guard (info?.first?[.status] as? NSNumber)?.intValue == SCFrameStatus.complete.rawValue else { return }
        progress.sample(pixels, now: now)
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: true) as? [NSMutableDictionary] {
            attachments.first?[kCMSampleAttachmentKey_DisplayImmediately] = true
        }
        receive(VideoSample(buffer))
    }
    func stream(_ stream: SCStream, didStopWithError error: any Error) { failed(error) }
}

@MainActor
final class SpacePreview {
    static let shared = SpacePreview()
    static var enabled = true
    fileprivate var deckID: UUID?

    private let minWidth = PreviewResizeGeometry.minWidth
    private let maxWidth = PreviewResizeGeometry.maxWidth
    /// 面板一旦真正出现就按“界面操作任务”续租：默认十分钟内没有新动作才关闭，
    /// 期间每次动作都会续期。任务结束由 release 立即关闭。
    static let defaultHoldSeconds: TimeInterval = 600
    static let maximumHoldSeconds: TimeInterval = 3600
    private let captureInterval: TimeInterval = 0.6
    private let idleCloseInterval: TimeInterval = 30

    fileprivate var panel: PreviewPanel?
    fileprivate var view: PreviewView?
    private var timer: Timer?
    private let followLoop = PreviewHostFollowLoop()
    private var temporaryPlacement = PreviewTemporaryPlacement()
    fileprivate var target: (pid: Int32, windowID: UInt32)?
    private var canvas: CGRect = CGRect(x: 0, y: 0, width: 2560, height: 1440)
    fileprivate var width: CGFloat = 380
    private var browserLeaseID: String?
    private var browserDocumentKey: String?
    private var browserSequence = 0
    private var stream: SCStream?
    private var streamOutput: PreviewStreamOutput?
    private var streamLifecycle: PreviewCaptureLifecycle?
    private var lastCaptureStop: PreviewCaptureLifecycle?
    private var streamConfiguration: SCStreamConfiguration?
    private var preferredFPS: Int32 = 15
    private var cursorState: CursorState?
    private var cursorTimer: Timer?
    private var generation = UUID()
    // Capture streams can restart while the requested target and scope stay fixed.
    // A first-frame waiter follows those restarts, but never follows a new lease.
    private var leaseGeneration = UUID()
    private var streamStarting = false
    private var streamStarted = false
    private var streamError: String?
    private var lastFrameTime: TimeInterval?
    private var nextProgressLog: TimeInterval = 0
    private var frameCount = 0
    private var visible = false
    private var lastActivity: TimeInterval = 0
    fileprivate var hostWindow: PreviewHostWindow?
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
    var onUserClose: (() -> Void)?

    /// 按任务跟踪目标；显式桌面共享不因目标位于当前 Space 而隐藏。
    func track(pid: Int32, windowID: UInt32, source: PreviewSource? = nil) {
        guard Self.enabled else { return }
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
        if source == .browserContent {
            return isHolding && browserLeaseID != nil && lastFrameTime.map {
                ProcessInfo.processInfo.systemUptime - $0 < 3
            } == true && NativePlatform.sessionLocked() == false && CGPreflightScreenCaptureAccess()
        }
        return streamStarted && !streamStarting && streamError == nil && lastFrameTime != nil
            && stream != nil && view?.video.status != .failed
            && NativePlatform.sessionLocked() == false && CGPreflightScreenCaptureAccess()
    }

    /// 复用同一条流等待真实首帧，不重启捕获，也不操作目标窗口。
    func waitUntilReady(checkCancellation: () throws -> Void) async throws {
        let token = leaseGeneration
        var succeeded = false
        defer {
            // 只清理本次等待的失败流，不能关闭重入请求已建立的新共享。
            if !succeeded, leaseGeneration == token, !waitingForTargetDesktop { stop() }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + 3
        func failure(_ reason: String, _ message: String) -> NativeFailure {
            OperationTrace.current?.mark("preview.wait_failed", fields: ["reason": .string(reason)])
            return NativeFailure(code: "SHARE_NOT_READY", message: message)
        }
        while true {
            try checkCancellation()
            try Task.checkCancellation()
            guard leaseGeneration == token, target != nil else {
                throw failure("lease_changed", "共享目标已停止或改变")
            }
            guard NativePlatform.sessionLocked() == false, CGPreflightScreenCaptureAccess() else {
                throw failure("access_unavailable", "共享的屏幕录制授权或解锁状态已不可用")
            }
            if streamReady {
                guard let target,
                      NativePlatform.windows().contains(where: { $0.pid == target.pid && $0.windowID == target.windowID }) else {
                    throw failure("target_unavailable", "共享就绪前目标进程或窗口已不可用")
                }
                succeeded = true
                return
            }
            if let streamError {
                throw failure("stream_error", "共享未就绪：\(streamError)")
            }
            guard ProcessInfo.processInfo.systemUptime < deadline else {
                throw failure("first_frame_timeout", "共享在 3 秒内未完成启动并收到首帧")
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
    }

    /// Stop tracking without ending an active share lease (for example when switching targets).
    private func teardown() {
        cursorTimer?.invalidate(); cursorTimer = nil; cursorState = nil
        leaseGeneration = UUID()
        timer?.invalidate()
        timer = nil
        followLoop.stop()
        temporaryPlacement.reset()
        view?.cancelInteraction()
        if let workspaceObserver { NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver) }
        if let displayObserver { NotificationCenter.default.removeObserver(displayObserver) }
        workspaceObserver = nil
        displayObserver = nil
        target = nil
        browserLeaseID = nil
        browserDocumentKey = nil
        browserSequence = 0
        view?.browserImage.contents = nil
        view?.browserLabel.isHidden = true
        view?.video.isHidden = false
        captureSource = nil
        displayID = nil
        waitingForTargetDesktop = false
        hostSpaceIDs = []
        panelSpaceIDs = []
        panelSpaceVerified = false
        panelPlacementError = nil
        resetCapture(reason: "release_or_target_change")
        panel?.alphaValue = 0
        panel?.orderOut(nil)
        visible = false
    }

    /// Reconfigure capture without changing either the target identity or the preview host.
    private func resetCapture(reason: String = "reconfigure") {
        if let stream, let lifecycle = streamLifecycle {
            requestCaptureStop(stream, output: streamOutput, lifecycle: lifecycle, reason: reason)
        }
        generation = UUID()
        nextProgressLog = 0
        streamStarting = false
        streamStarted = false
        streamError = nil
        lastFrameTime = nil
        frameCount = 0
        stream = nil
        streamOutput = nil
        streamLifecycle = nil
        streamConfiguration = nil
        view?.video.flushAndRemoveImage()
        view?.mirrorCursor(nil, canvas: canvas)
    }

    func configureBrowserLease(_ leaseID: String) {
        guard source == .browserContent, isHolding else { return }
        browserLeaseID = leaseID
        browserDocumentKey = nil
        browserSequence = 0
        view?.video.isHidden = true
        view?.browserImage.contents = nil
        view?.browserLabel.isHidden = false
        view?.browserLabel.stringValue = "网页内容 · 定时帧（非整窗视频）"
    }

    func renewBrowserLease(_ leaseID: String, seconds: TimeInterval) throws {
        guard source == .browserContent, isHolding, browserLeaseID == leaseID else {
            throw NativeFailure(code: "STALE_TARGET", message: "网页预览租约已失效")
        }
        hold(seconds: seconds)
    }

    func acceptBrowserFrame(leaseID: String, documentKey: String, sequence: Int, data: String?, capturedAt: Double?) throws {
        guard source == .browserContent, isHolding, browserLeaseID == leaseID,
              sequence > browserSequence, target != nil,
              NativePlatform.sessionLocked() == false, CGPreflightScreenCaptureAccess() else {
            throw NativeFailure(code: "STALE_TARGET", message: "网页预览租约已失效")
        }
        browserSequence = sequence
        if browserDocumentKey != documentKey || data == nil {
            browserDocumentKey = documentKey
            lastFrameTime = nil
            view?.browserImage.contents = nil
        }
        guard let data else { return }
        guard let capturedAt, capturedAt.isFinite,
              Date().timeIntervalSince1970 - capturedAt >= -0.25,
              Date().timeIntervalSince1970 - capturedAt <= 3,
              data.utf8.count <= 180000, let bytes = Data(base64Encoded: data),
              let imageSource = CGImageSourceCreateWithData(bytes as CFData, nil),
              CGImageSourceGetType(imageSource) as String? == "public.jpeg",
              let properties = CGImageSourceCopyPropertiesAtIndex(imageSource, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= 2000, height <= 1500,
              let image = CGImageSourceCreateImageAtIndex(imageSource, 0, nil) else {
            throw RequestParameters.invalid("网页预览帧无效或超出限制")
        }
        canvas = CGRect(x: 0, y: 0, width: width, height: height)
        view?.browserImage.contents = image
        frameCount += 1
        lastFrameTime = ProcessInfo.processInfo.systemUptime - max(0, Date().timeIntervalSince1970 - capturedAt)
        view?.status = "网页内容 · 定时帧（非整窗视频）"
    }

    private func requestCaptureStop(_ stream: SCStream, output: PreviewStreamOutput?,
                                    lifecycle: PreviewCaptureLifecycle, reason: String) {
        guard lifecycle.stopRequested(reason: reason) else { return }
        lastCaptureStop = lifecycle
        // Keep the old stream/delegate alive until the API returns. Closing the panel stays immediate.
        Task { @MainActor in
            await lifecycle.waitForStartCompletion()
            do { try await stream.stopCapture(); lifecycle.stopCompleted() }
            catch { lifecycle.stopFailed(error) }
            withExtendedLifetime(output) {}
        }
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
            MainActor.assumeIsolated { self?.tick() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        RunLoop.main.add(timer, forMode: .eventTracking)
        followLoop.start { [weak self] in self?.followHostFrame() }
        tick()
    }

    /// Lightweight geometry-only updates; capture/Space validation stays on tick().
    private func followHostFrame() {
        guard visible, panelSpaceVerified, let panel, let bound = hostWindow else { return }
        guard let updated = Self.readHostWindow(bound), updated.isOnScreen else {
            setVisible(false)
            return
        }
        guard updated.bounds != bound.bounds || temporaryPlacement.isActive else { return }
        hostWindow = updated
        if !syncAnchor(panel: panel, host: updated) { setVisible(false) }
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
        recordStreamProgress(now: now, targetOnCurrentSpace: membership?.onCurrentSpace)
        if source == .browserContent {
            captureSource = .browserContent
            if !streamReady { view?.browserImage.contents = nil }
        } else {
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
        let lifecycle = PreviewCaptureLifecycle(generation: token, windowID: windowID, source: source.rawValue)
        streamLifecycle = lifecycle
        let wantedDisplay = displayID
        view?.status = "正在连接\(source.label)…"
        Task { @MainActor in
            var createdStream: SCStream?
            var createdOutput: PreviewStreamOutput?
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
                config.minimumFrameInterval = CMTime(value: 1, timescale: preferredFPS)
                config.queueDepth = 3
                config.showsCursor = false
                source.configureFraming(config)
                let output = PreviewStreamOutput(receive: { [weak self] sample in
                    Task { @MainActor [weak self] in
                        guard let owner = self else { return }
                        guard owner.generation == token, let view = owner.view else { return }
                        if view.video.status == .failed { view.video.flush() }
                        let accepted = view.video.isReadyForMoreMediaData
                        if accepted { view.video.enqueue(sample.buffer) }
                        owner.streamOutput?.progress.recordSubmission(accepted: accepted, now: ProcessInfo.processInfo.systemUptime)
                        owner.frameCount += 1
                        owner.lastFrameTime = ProcessInfo.processInfo.systemUptime
                        view.status = "quietbridge · \(source.label)"
                    }
                }, failed: { [weak self] error in
                    Task { @MainActor [weak self] in
                        lifecycle.delegateStopped(error)
                        guard let owner = self, owner.generation == token else { return }
                        owner.streamStarted = false
                        owner.streamStarting = false
                        owner.streamError = error.localizedDescription
                        owner.lastFrameTime = nil
                        owner.view?.status = "共享已中断：\(error.localizedDescription)"
                        owner.view?.video.flushAndRemoveImage()
                    }
                })
                let next = SCStream(filter: filter, configuration: config, delegate: output)
                createdStream = next; createdOutput = output
                try next.addStreamOutput(output, type: .screen, sampleHandlerQueue: DispatchQueue(label: "quietbridge.preview.video"))
                stream = next
                streamConfiguration = config
                streamOutput = output
                lifecycle.startRequested()
                try await next.startCapture()
                lifecycle.startCompleted()
                guard generation == token else {
                    requestCaptureStop(next, output: output, lifecycle: lifecycle, reason: "superseded_start")
                    return
                }
                streamStarting = false
                streamStarted = streamError == nil
                if let panel {
                    let height = width * canvas.height / max(1, canvas.width)
                    panel.setFrame(NSRect(x: panel.frame.minX, y: panel.frame.maxY - height, width: width, height: height), display: true)
                    if let hostWindow { _ = syncAnchor(panel: panel, host: hostWindow) }
                }
            } catch {
                lifecycle.startFailed(error)
                if let createdStream {
                    requestCaptureStop(createdStream, output: createdOutput, lifecycle: lifecycle, reason: "start_failed")
                }
                guard generation == token else { return }
                streamStarting = false
                streamStarted = false
                streamError = (error as? NativeFailure)?.message ?? error.localizedDescription
                lastFrameTime = nil
                stream = nil
                streamOutput = nil
                streamLifecycle = nil
                view?.status = "无法共享：\(streamError ?? "未知错误")"
                Self.debug("stream failed: \(error)")
            }
        }
    }

    private func ensurePanel() {
        guard panel == nil else { return }
        if NSApp.activationPolicy() != .accessory { NSApp.setActivationPolicy(.accessory) }
        let initial = NSRect(x: 0, y: 0, width: width, height: width * 0.65)
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
            temporaryPlacement.reset()
            view?.cancelInteraction()
            panel.alphaValue = 0
            if panel.isVisible { panel.orderOut(nil) }
        }
        visible = next
        if next, deckID != nil { PreviewDeck.shared.refresh() }
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

    /// Resolve temporary placement, otherwise keep the preview pinned top-right.
    @discardableResult private func syncAnchor(panel: NSPanel, host: PreviewHostWindow) -> Bool {
        if deckID != nil { return PreviewDeck.shared.place(from: self, host: host) }
        guard let display = Self.displayID(containing: CGPoint(x: host.bounds.midX, y: host.bounds.midY)),
              let screen = NSScreen.screens.first(where: {
                  ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == display
              }) else {
            panelPlacementError = "宿主显示器不可用，未改为跟随当前焦点显示器"
            return false
        }
        let desired = CGRect(x: 0, y: 0, width: width, height: width * canvasAspect)
        guard let anchored = PreviewHostGeometry.frame(for: desired, hostBounds: host.bounds,
            mainDisplayHeight: NSScreen.screens.first?.frame.maxY ?? 0, visibleFrame: screen.visibleFrame) else {
            panelPlacementError = "宿主没有可容纳共享预览的可见区域"
            return false
        }
        let next: CGRect
        let wasTemporary = temporaryPlacement.isActive
        if let temporary = temporaryPlacement.resolve(host: host, now: ProcessInfo.processInfo.systemUptime) {
            next = PreviewHostGeometry.temporaryFrame(temporary, fittedSize: anchored.size, visibleFrame: screen.visibleFrame)
        } else { next = anchored }
        if wasTemporary && !temporaryPlacement.isActive { view?.cancelInteraction() }
        if panel.frame != next {
            panel.setFrame(next, display: true)
            panel.invalidateShadow()
        }
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

    func acceptCursorState(_ value: JSONValue) {
        cursorTimer?.invalidate(); cursorTimer = nil
        cursorState = (try? JSONEncoder().encode(value)).flatMap { try? JSONDecoder().decode(CursorState.self, from: $0) }
        guard let frame = cursorState?.frame(at: ProcessInfo.processInfo.systemUptime), target?.windowID == frame.windowID else {
            cursorState = nil; mirrorCursor(nil); return
        }
        mirrorCursor(frame)
        cursorTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let frame = self.cursorState?.frame(at: ProcessInfo.processInfo.systemUptime)
                self.mirrorCursor(frame)
                if frame == nil { self.cursorTimer?.invalidate(); self.cursorTimer = nil; self.cursorState = nil }
            }
        }
    }

    func diagnostics() -> [String: Any] {
        let now = ProcessInfo.processInfo.systemUptime
        let membership = target.flatMap { spaces.memberships(windowIDs: [$0.windowID])[$0.windowID] }
        let hostMembership = hostWindow.flatMap { spaces.memberships(windowIDs: [$0.windowID])[$0.windowID] }
        let panelMembership = panel.flatMap { UInt32(exactly: $0.windowNumber) }
            .flatMap { spaces.memberships(windowIDs: [$0])[$0] }
        var result: [String: Any] = ["videoFrames": frameCount, "visible": visible && panelMembership?.onCurrentSpace == true, "status": view?.status ?? "",
         "streamGeneration": generation.uuidString, "preferredFPS": Int(preferredFPS),
         "scope": captureSource?.scope ?? source.scope,
         "targetOnCurrentSpace": membership?.onCurrentSpace as Any? ?? NSNull(),
         "streamStarting": streamStarting, "streamStarted": streamStarted, "streamReady": streamReady,
         "streamError": streamError as Any? ?? NSNull(),
         "frameAgeMs": lastFrameTime.map { max(0, (ProcessInfo.processInfo.systemUptime - $0) * 1000) } as Any? ?? NSNull(),
         "panelFrame": panel.map { NSStringFromRect($0.frame) } ?? "",
         "videoLayerReady": view?.video.isReadyForMoreMediaData ?? false,
         "videoLayerFailed": view?.video.status == .failed,
         "holding": isHolding, "holdRemainingSeconds": holdRemainingSeconds ?? 0,
         "temporarilyMoved": temporaryPlacement.isActive,
         "temporaryPositionRemainingSeconds": temporaryPlacement.remainingSeconds(now: now) as Any? ?? NSNull(),
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
        result["frameProgress"] = streamOutput?.progress.snapshot(now: now) ?? PreviewFrameProgress().snapshot(now: now)
        if let streamLifecycle { result["captureLifecycle"] = streamLifecycle.snapshot() }
        if let lastCaptureStop { result["captureStop"] = lastCaptureStop.snapshot() }
        return result
    }

    private func recordStreamProgress(now: TimeInterval, targetOnCurrentSpace: Bool?) {
        guard stream != nil, let output = streamOutput, now >= nextProgressLog else { return }
        nextProgressLog = now + 5
        // Only counters, ages and fixed format/range metadata; no image, fingerprint,
        // page title, URL, user input or raw platform error is logged.
        guard let data = try? JSONSerialization.data(withJSONObject: output.progress.snapshot(now: now)),
              let progress = try? JSONDecoder().decode(JSONValue.self, from: data) else { return }
        panelTrace.mark("preview.streamProgress", fields: [
            "trackingWindowId": target.map { .number(Double($0.windowID)) } ?? .null,
            "streamGeneration": .string(generation.uuidString),
            "targetOnCurrentSpace": targetOnCurrentSpace.map(JSONValue.bool) ?? .null,
            "panelVisible": .bool(visible),
            "videoLayerReady": .bool(view?.video.isReadyForMoreMediaData ?? false),
            "videoLayerFailed": .bool(view?.video.status == .failed),
            "frameProgress": progress,
        ])
    }

    /// Person clicked the panel's close control: end sharing immediately.
    fileprivate func userClosed() {
        Self.debug("user closed preview")
        release()
        onUserClose?()
    }

    fileprivate func selectCard() {
        if let deckID { PreviewDeck.shared.select(deckID) }
    }

    fileprivate func setFront(_ front: Bool, label: String, count: Int) {
        view?.sessionTab.title = label
        view?.sessionTab.isHidden = count < 2 || front
        view?.isBackCard = !front
        let next: Int32 = front ? 15 : 5
        guard preferredFPS != next else { return }
        preferredFPS = next
        guard let stream, let configuration = streamConfiguration else { return }
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: next)
        let token = generation
        Task { [weak self] in
            do { try await stream.updateConfiguration(configuration) }
            catch {
                guard let self, self.generation == token else { return }
                self.streamError = "帧率调整失败：\(error.localizedDescription)"
            }
        }
    }

    /// Picture aspect of the shared window (height / width); drives the panel height.
    fileprivate var canvasAspect: CGFloat {
        canvas.width > 0 && canvas.height > 0 ? canvas.height / canvas.width : 0.62
    }

    fileprivate var moveHost: PreviewHostWindow? { hostWindow }

    /// Move only our nonactivating panel. Never move/activate the host or source.
    @discardableResult fileprivate func applyMove(to frame: NSRect, from initialHost: PreviewHostWindow) -> Bool {
        if deckID != nil { return PreviewDeck.shared.move(from: self, frame: frame, host: initialHost) }
        guard visible, panelSpaceVerified, let panel, let bound = hostWindow,
              let current = Self.readHostWindow(bound), current.isOnScreen else { return false }
        hostWindow = current
        guard current.pid == initialHost.pid, current.windowID == initialHost.windowID,
              current.bounds == initialHost.bounds else {
            temporaryPlacement.reset()
            if !syncAnchor(panel: panel, host: current) { setVisible(false) }
            return false
        }
        temporaryPlacement.drag(to: frame, host: current)
        if !syncAnchor(panel: panel, host: current) { setVisible(false); return false }
        return true
    }

    fileprivate func finishMove() {
        if deckID != nil { PreviewDeck.shared.finishMove(); return }
        temporaryPlacement.finishDrag(now: ProcessInfo.processInfo.systemUptime)
    }

    /// Corner/edge drag: the view has already resolved the target frame.
    fileprivate func applyResize(to frame: NSRect) {
        if deckID != nil { PreviewDeck.shared.resize(from: self, frame: frame); return }
        guard let panel else { return }
        width = min(maxWidth, max(minWidth, frame.width))
        if let hostWindow {
            temporaryPlacement.resize(to: frame, host: hostWindow, now: ProcessInfo.processInfo.systemUptime)
            if !syncAnchor(panel: panel, host: hostWindow) { setVisible(false) }
            width = panel.frame.width
        }
        if let view { view.window?.invalidateCursorRects(for: view) }
        view?.needsDisplay = true
        Self.debug("resize to \(NSStringFromRect(panel.frame))")
    }

    /// Opt-in diagnostics: QUIETBRIDGE_PREVIEW_DEBUG=1 appends decisions to /tmp/quietbridge-space-preview.log.
    static func debug(_ message: String) {
        guard QuietBridgeIdentity.environment("QUIETBRIDGE_PREVIEW_DEBUG") == "1" else { return }
        let path = "/tmp/quietbridge-space-preview.log"
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

    fileprivate static func readHostWindow(_ bound: PreviewHostWindow) -> PreviewHostWindow? {
        let list = CGWindowListCopyWindowInfo(.optionIncludingWindow, bound.windowID) as? [[String: Any]] ?? []
        guard let entry = list.first(where: { ($0[kCGWindowNumber as String] as? UInt32) == bound.windowID }),
              (entry[kCGWindowOwnerPID as String] as? Int32) == bound.pid,
              let dictionary = entry[kCGWindowBounds as String] as? [String: Any],
              let bounds = CGRect(dictionaryRepresentation: dictionary as CFDictionary) else { return nil }
        return PreviewHostWindow(windowID: bound.windowID, pid: bound.pid, bounds: bounds,
                                 isOnScreen: (entry[kCGWindowIsOnscreen as String] as? Bool) == true)
    }

    private func resolveHostWindow() -> PreviewHostWindow? {
        let selfPID = getpid()
        let hostPIDs = Set(NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.workbuddy.mac")
            .map(\.processIdentifier))
        let responsible = NativePlatform.responsiblePID(selfPID)
        let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        let candidates: [PreviewHostWindow] = list.compactMap { entry in
            guard let owner = entry[kCGWindowOwnerPID as String] as? Int32, owner != selfPID,
                  hostPIDs.contains(owner) || (hostPIDs.isEmpty && owner == responsible),
                  (entry[kCGWindowLayer as String] as? Int) == 0,
                  let id = entry[kCGWindowNumber as String] as? UInt32,
                  let boundsDict = entry[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: boundsDict as CFDictionary),
                  rect.width > 300, rect.height > 200 else { return nil }
            return PreviewHostWindow(windowID: id, pid: owner, bounds: rect,
                isOnScreen: (entry[kCGWindowIsOnscreen as String] as? Bool) == true)
        }
        let selected = PreviewHostWindow.select(candidates, hostPIDs: hostPIDs,
            responsiblePID: responsible, previous: hostWindow)
        hostWindow = selected
        return selected
    }

}

/// Display selection is deliberately separate from each client's capture and AX
/// identities. No API here can activate a target or alter a task's binding.
@MainActor final class PreviewDeck {
    static let shared = PreviewDeck()
    private var previews: [UUID: SpacePreview] = [:]
    private var order: [UUID] = []
    private(set) var selected: UUID?
    private struct Group {
        var selected: UUID?
        var placement = PreviewTemporaryPlacement()
        var width: CGFloat = 380
    }
    private var groups: [String: Group] = [:]
    private func hostKey(_ host: PreviewHostWindow) -> String { "\(host.pid):\(host.windowID)" }
    private var layingOut = false
    var onHoldEnded: (() -> Void)?

    var isHolding: Bool { previews.values.contains { $0.isHolding } }
    var count: Int { previews.values.filter { $0.target != nil }.count }

    func preview(for client: UUID) -> SpacePreview {
        if let existing = previews[client] { return existing }
        let preview = SpacePreview()
        preview.deckID = client
        preview.onHoldEnded = { [weak self] in
            self?.relayout()
            self?.onHoldEnded?()
        }
        previews[client] = preview
        order.append(client)
        return preview
    }

    func remove(_ client: UUID) {
        let preview = previews.removeValue(forKey: client)
        order.removeAll { $0 == client }
        if selected == client { selected = nil }
        preview?.stop()
        relayout()
    }

    func stopAll() {
        let old = previews.values
        previews = [:]; order = []; selected = nil
        groups = [:]
        for preview in old { preview.stop() }
    }

    func select(_ client: UUID) {
        guard previews[client]?.target != nil else { return }
        selected = client
        if let host = previews[client]?.hostWindow { groups[hostKey(host), default: Group()].selected = client }
        relayout()
    }

    private func relayout() {
        var visited = Set<String>()
        for preview in order.compactMap({ previews[$0] }) where preview.target != nil {
            guard let host = preview.hostWindow, visited.insert(hostKey(host)).inserted else { continue }
            _ = place(from: preview, host: host)
        }
    }

    fileprivate func refresh() { relayout() }

    @discardableResult fileprivate func place(from preview: SpacePreview, host: PreviewHostWindow) -> Bool {
        guard !layingOut else { return true }
        layingOut = true
        defer { layingOut = false }
        let groupKey = hostKey(host)
        var group = groups[groupKey, default: Group()]
        let active = order.filter { key in
            guard let p = previews[key], p.target != nil, p.panel != nil else { return false }
            return (p === preview && p.hostWindow == nil) || (p.hostWindow?.pid == host.pid && p.hostWindow?.windowID == host.windowID)
        }
        guard !active.isEmpty else { return false }
        if group.selected == nil || !active.contains(group.selected!) { group.selected = active.first }
        guard let selected = group.selected, let front = previews[selected] else { return false }
        self.selected = selected
        let ranked = [selected] + active.filter { $0 != selected }
        let tail = CGFloat(ranked.count - 1)
        let desired = CGRect(x: 0, y: 0, width: group.width + tail * 24,
                             height: group.width * front.canvasAspect + tail * 40)
        guard let screen = NSScreen.screens.first(where: { screen in
            let y = (NSScreen.screens.first?.frame.maxY ?? 0) - host.bounds.midY
            return screen.frame.contains(CGPoint(x: host.bounds.midX, y: y))
        }), let anchor = PreviewHostGeometry.frame(for: desired, hostBounds: host.bounds,
            mainDisplayHeight: NSScreen.screens.first?.frame.maxY ?? 0, visibleFrame: screen.visibleFrame) else { return false }
        let envelope: CGRect
        if let temporary = group.placement.resolve(host: host, now: ProcessInfo.processInfo.systemUptime) {
            envelope = PreviewHostGeometry.temporaryFrame(temporary, fittedSize: anchor.size, visibleFrame: screen.visibleFrame)
        } else { envelope = anchor }
        let scale = min(envelope.width / desired.width, envelope.height / desired.height)
        groups[groupKey] = group
        let width = group.width * scale, height = group.width * front.canvasAspect * scale
        var previous: Int = 0
        for (rank, key) in ranked.enumerated().reversed() {
            guard let p = previews[key], let panel = p.panel else { continue }
            let frame = CGRect(x: envelope.maxX - width - CGFloat(rank) * 24 * scale,
                y: envelope.maxY - height - CGFloat(rank) * 40 * scale, width: width, height: height)
            if panel.frame != frame { panel.setFrame(frame, display: true); panel.invalidateShadow() }
            p.width = width
            let app = p.target.flatMap { NSRunningApplication(processIdentifier: $0.pid)?.localizedName } ?? "共享窗口"
            let ordinal = (active.firstIndex(of: key) ?? 0) + 1
            p.setFront(rank == 0, label: "● \(app) · 会话 \(ordinal)  ↗", count: ranked.count)
            if panel.isVisible {
                if previous > 0 { panel.order(.above, relativeTo: previous) }
                previous = panel.windowNumber
            }
        }
        return true
    }

    fileprivate func move(from preview: SpacePreview, frame: CGRect, host: PreviewHostWindow) -> Bool {
        guard let bound = preview.moveHost, let current = SpacePreview.readHostWindow(bound),
              current.pid == host.pid, current.windowID == host.windowID,
              current.bounds == host.bounds, let panel = preview.panel else {
            groups[hostKey(host), default: Group()].placement.reset(); relayout(); return false
        }
        let active = previews.values.filter { $0.target != nil && $0.hostWindow?.windowID == host.windowID }
        let envelope = active.compactMap { $0.panel?.frame }.reduce(CGRect.null) { $0.union($1) }
        groups[hostKey(host), default: Group()].placement.drag(to: envelope.offsetBy(dx: frame.minX - panel.frame.minX, dy: frame.minY - panel.frame.minY), host: host)
        return place(from: preview, host: host)
    }

    fileprivate func finishMove() {
        for key in groups.keys { groups[key]?.placement.finishDrag(now: ProcessInfo.processInfo.systemUptime) }
    }

    fileprivate func resize(from preview: SpacePreview, frame: CGRect) {
        guard let host = preview.hostWindow else { return }
        groups[hostKey(host), default: Group()].width = min(PreviewResizeGeometry.maxWidth, max(PreviewResizeGeometry.minWidth, frame.width))
        relayout()
    }
}

fileprivate final class PreviewPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class PreviewView: NSView {
    let sessionTab = NSButton()
    var isBackCard = false
    private var closeArmedUntil: TimeInterval = 0
    static let cornerRadius: CGFloat = 12
    weak var delegate: SpacePreview?
    let video = AVSampleBufferDisplayLayer()
    let browserImage = CALayer()
    let browserLabel = NSTextField(labelWithString: "网页内容 · 定时帧（非整窗视频）")
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
    var status = "正在连接窗口共享…"
    private let surface = NSView()
    private(set) var backdropView: NSView?
    private var closeBackdrop: NSView?
    let closeButton = NSButton()
    private let grip = CAShapeLayer()

    func setHovered(_ next: Bool) {
        closeBackdrop?.isHidden = !next
        grip.opacity = next ? 1 : 0
    }

    @objc private func closePreview() {
        let now = ProcessInfo.processInfo.systemUptime
        if now < closeArmedUntil {
            closeArmedUntil = 0
            delegate?.userClosed()
            return
        }
        closeArmedUntil = now + 4
        closeButton.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: "确认结束当前会话共享")
        closeButton.toolTip = "再次点击，仅结束当前会话共享"
        closeButton.setAccessibilityLabel("确认结束当前会话共享；其他会话继续")
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
            guard let self, ProcessInfo.processInfo.systemUptime >= self.closeArmedUntil else { return }
            self.closeButton.image = NSImage(systemSymbolName: "xmark", accessibilityDescription: "结束当前共享")
            self.closeButton.toolTip = "结束当前共享"
            self.closeButton.setAccessibilityLabel("结束当前会话共享")
        }
    }
    @objc private func selectPreview() { delegate?.selectCard() }

    private static func glass(content: NSView, radius: CGFloat, withinWindow: Bool = false) -> NSView {
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView()
            glass.style = .regular
            glass.cornerRadius = radius
            glass.contentView = content
            return glass
        }
        let frost = NSVisualEffectView()
        frost.material = .hudWindow
        frost.blendingMode = withinWindow ? .withinWindow : .behindWindow
        frost.state = .active
        frost.wantsLayer = true
        frost.layer?.cornerRadius = radius
        frost.layer?.masksToBounds = true
        frost.addSubview(content)
        content.autoresizingMask = [.width, .height]
        return frost
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        // Clip the whole composited window, including the glass backdrop. The
        // transparent NSPanel supplies the outer shadow from this rounded alpha
        // shape; an unmasked root layer can leave a rectangular frame around it.
        layer?.backgroundColor = NSColor.clear.cgColor
        layer?.cornerRadius = Self.cornerRadius
        layer?.cornerCurve = .continuous
        layer?.masksToBounds = true
        surface.wantsLayer = true
        surface.layer?.backgroundColor = NSColor.clear.cgColor
        surface.layer?.cornerRadius = Self.cornerRadius
        surface.layer?.cornerCurve = .continuous
        surface.layer?.masksToBounds = true
        // Keep the video above the glass, outside its contentView, so its native
        // refractive edge cannot draw an extra outline over the shared picture.
        let backdrop = Self.glass(content: NSView(), radius: Self.cornerRadius)
        backdropView = backdrop
        addSubview(backdrop)
        addSubview(surface)
        video.videoGravity = .resizeAspect
        video.isOpaque = false
        video.backgroundColor = NSColor.clear.cgColor
        // AVSampleBufferDisplayLayer has its own video surface. Clip it directly
        // as well, so arriving frames keep their rounded corners on the GPU path.
        video.cornerRadius = Self.cornerRadius
        video.cornerCurve = .continuous
        video.masksToBounds = true
        video.actions = ["position": NSNull(), "bounds": NSNull()]
        surface.layer?.addSublayer(video)
        browserImage.contentsGravity = .resizeAspect
        browserImage.masksToBounds = true
        browserImage.cornerRadius = Self.cornerRadius
        browserImage.actions = ["contents": NSNull(), "bounds": NSNull(), "position": NSNull()]
        surface.layer?.addSublayer(browserImage)
        browserLabel.font = .systemFont(ofSize: 10)
        browserLabel.textColor = .white
        browserLabel.backgroundColor = NSColor.black.withAlphaComponent(0.7)
        browserLabel.drawsBackground = true
        browserLabel.isHidden = true
        surface.addSubview(browserLabel)
        grip.fillColor = nil
        grip.strokeColor = NSColor.white.withAlphaComponent(0.85).cgColor
        grip.lineWidth = 1.5
        grip.lineCap = .round
        grip.shadowColor = NSColor.black.cgColor
        grip.shadowOpacity = 0.7
        grip.shadowRadius = 2
        surface.layer?.addSublayer(grip)
        closeButton.image = NSImage(systemSymbolName: "xmark", accessibilityDescription: "关闭共享预览")
        closeButton.imagePosition = .imageOnly
        closeButton.isBordered = false
        closeButton.contentTintColor = .labelColor
        closeButton.wantsLayer = true
        closeButton.layer?.cornerRadius = 14
        closeButton.target = self
        closeButton.action = #selector(closePreview)
        closeButton.setAccessibilityLabel("关闭共享预览")
        let closeGlass = Self.glass(content: closeButton, radius: 14, withinWindow: true)
        closeBackdrop = closeGlass
        surface.addSubview(closeGlass)
        sessionTab.title = "持续共享"
        sessionTab.bezelStyle = .inline
        sessionTab.font = .systemFont(ofSize: 11, weight: .medium)
        sessionTab.isBordered = false
        sessionTab.contentTintColor = .labelColor
        sessionTab.target = self
        sessionTab.action = #selector(selectPreview)
        sessionTab.setAccessibilityLabel("切换共享预览；不改变操作目标")
        sessionTab.isHidden = true
        surface.addSubview(sessionTab)
        setHovered(false)
        pointerView.wantsLayer = true
        pointerView.isHidden = true
        surface.addSubview(pointerView)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout()
        backdropView?.frame = bounds
        surface.frame = bounds
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        video.frame = surface.bounds
        browserImage.frame = surface.bounds
        browserLabel.frame = CGRect(x: 8, y: max(8, surface.bounds.height - 28),
                                    width: min(260, max(0, surface.bounds.width - 54)), height: 18)
        grip.frame = surface.bounds
        let handle = CGMutablePath()
        handle.move(to: CGPoint(x: 8, y: 20)); handle.addLine(to: CGPoint(x: 8, y: 8)); handle.addLine(to: CGPoint(x: 20, y: 8))
        handle.move(to: CGPoint(x: 12, y: 20)); handle.addLine(to: CGPoint(x: 12, y: 12)); handle.addLine(to: CGPoint(x: 20, y: 12))
        grip.path = handle
        CATransaction.commit()
        closeBackdrop?.frame = CGRect(x: max(0, bounds.width - 38), y: max(0, bounds.height - 38), width: 28, height: 28)
        closeButton.frame = CGRect(x: 0, y: 0, width: 28, height: 28)
        sessionTab.frame = CGRect(x: 12, y: 4, width: min(240, max(0, bounds.width - 24)), height: 28)
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
    private var moveStartHost: PreviewHostWindow?
    private var didMove = false
    private let geometry = PreviewResizeGeometry()

    func cancelInteraction() {
        dragRegion = .none
        moveStartHost = nil
        didMove = false
    }

    override var isOpaque: Bool { false }

    // MARK: - Hit testing / bottom-left resize

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        // Decorative glass/video never consumes a move or resize gesture.
        if closeBackdrop?.isHidden == false,
           closeButton.bounds.contains(closeButton.convert(point, from: superview)) { return closeButton }
        if !sessionTab.isHidden, sessionTab.bounds.contains(sessionTab.convert(point, from: superview)) { return sessionTab }
        return hit === closeButton ? closeButton : self
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
        setHovered(true)
        updateCursor(at: event.locationInWindow)
    }

    override func mouseMoved(with event: NSEvent) {
        setHovered(true)
        updateCursor(at: event.locationInWindow)
    }

    override func mouseExited(with event: NSEvent) {
        if dragRegion == .none { setHovered(false) }
        hoverRegion = .none
        NSCursor.arrow.set()
    }

    override func mouseDown(with event: NSEvent) {
        if isBackCard { delegate?.selectCard(); return }
        let region = region(atWindowPoint: event.locationInWindow)
        guard let window else { return }
        dragRegion = region
        moveStartHost = region == .none ? delegate?.moveHost : nil
        didMove = false
        startFrame = window.frame
        startMouse = NSEvent.mouseLocation
        region.cursor.set()
    }

    override func mouseDragged(with event: NSEvent) {
        guard let delegate, let window else { return }
        // Screen-space delta: the view moves under the pointer as the frame changes,
        // so view-local coordinates would feed the previous resize back in.
        let now = NSEvent.mouseLocation
        let delta = CGSize(width: now.x - startMouse.x, height: now.y - startMouse.y)
        if dragRegion == .none {
            guard let initialHost = moveStartHost,
                  didMove || max(abs(delta.width), abs(delta.height)) >= 3 else { return }
            let moved = startFrame.offsetBy(dx: delta.width, dy: delta.height)
            guard delegate.applyMove(to: moved, from: initialHost) else {
                moveStartHost = nil
                return
            }
            didMove = true
            NSCursor.closedHand.set()
            return
        }
        let target = geometry.frame(for: dragRegion,
                                    startFrame: startFrame,
                                    delta: delta,
                                    aspect: delegate.canvasAspect)
        delegate.applyResize(to: target)
        dragRegion.cursor.set()
        window.invalidateCursorRects(for: self)
    }

    override func mouseUp(with event: NSEvent) {
        if didMove { delegate?.finishMove() }
        moveStartHost = nil
        didMove = false
        dragRegion = .none
        setHovered(bounds.contains(convert(event.locationInWindow, from: nil)))
        updateCursor(at: event.locationInWindow)
    }
}
