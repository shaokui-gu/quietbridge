// Full-screen task privacy shield and its system-session lifecycle.
//
// Authorization remains in the separately installed system plug-in. This
// controller coordinates task leases, the desktop cover and manual takeover.
// Real loginwindow behavior and secure-input delivery need host validation.
import AppKit
import CoreGraphics
import Darwin
import Foundation
import IOKit
import IOKit.pwr_mgt

enum TaskPrivacySyntheticEvents {
    static let marker: Int64 = 0x57424355414c4f43
}

enum TaskPrivacyPresentation {
    static func shouldShow(taskActive: Bool, sessionLocked: Bool?) -> Bool {
        taskActive && sessionLocked == false
    }
}

private final class PrivacyDisplayGuard {
    private static let assertionName = "workbuddy-cua: task privacy shield"
    private var assertionID: IOPMAssertionID = 0
    private var failure: NativeFailure?

    func hold() throws {
        guard assertionID == 0 else { return }
        var identifier: IOPMAssertionID = 0
        let result = IOPMAssertionCreateWithName(
            kIOPMAssertPreventUserIdleDisplaySleep as CFString,
            IOPMAssertionLevel(kIOPMAssertionLevelOn),
            Self.assertionName as CFString,
            &identifier)
        guard result == kIOReturnSuccess, identifier != 0 else {
            throw NativeFailure(code: "DISPLAY_ASSERTION_FAILED",
                                message: "无法阻止显示器进入空闲睡眠（IOReturn \(result)）")
        }
        assertionID = identifier
        failure = nil
    }

    func release() {
        guard assertionID != 0 else { return }
        IOPMAssertionRelease(assertionID)
        assertionID = 0
    }

    var isHolding: Bool { assertionID != 0 }
}

/// Ask macOS for its own authentication screen. macOS has removed the old
/// CGSession/SkyLight lock entry points on recent releases, so keep compatible
/// routes in order and verify the resulting session state from the caller's timer.
enum TaskPrivacySystemLock {
    private typealias LockImmediate = @convention(c) () -> Void
    private static let processQueue = DispatchQueue(label: "com.workbuddy.cua.privacy-lock")

    static func request() throws -> String {
        for framework in [
            "/System/Library/PrivateFrameworks/login.framework/login",
            "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
        ] {
            guard let library = dlopen(framework, RTLD_LAZY | RTLD_LOCAL) else { continue }
            if let symbol = dlsym(library, "SACLockScreenImmediate") {
                let lock = unsafeBitCast(symbol, to: LockImmediate.self)
                lock()
                dlclose(library)
                return URL(fileURLWithPath: framework).deletingPathExtension().lastPathComponent
            }
            dlclose(library)
        }

        let legacy = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
        if FileManager.default.isExecutableFile(atPath: legacy) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: legacy)
            process.arguments = ["-suspend"]
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                throw NativeFailure(code: "SYSTEM_LOCK_FAILED",
                                    message: "CGSession 无法锁定系统（\(process.terminationStatus)）")
            }
            return "cgsession"
        }

        // macOS 26 no longer exposes either API. A real Ctrl-Command-Q reaches
        // loginwindow through the session hotkey path. The subsequent display
        // sleep request makes the password/lock transition deterministic when
        // the user's policy requires authentication after sleep.
        guard let source = CGEventSource(stateID: .hidSystemState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: 12, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 12, keyDown: false) else {
            throw NativeFailure(code: "SYSTEM_LOCK_FAILED", message: "无法创建系统锁屏快捷键事件")
        }
        source.userData = TaskPrivacySyntheticEvents.marker
        down.setIntegerValueField(.eventSourceUserData, value: TaskPrivacySyntheticEvents.marker)
        up.setIntegerValueField(.eventSourceUserData, value: TaskPrivacySyntheticEvents.marker)
        down.flags = [.maskControl, .maskCommand]
        up.flags = [.maskControl, .maskCommand]
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
        processQueue.async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
            process.arguments = ["displaysleepnow"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try? process.run()
            process.waitUntilExit()
        }
        return "lock-shortcut+display-sleep"
    }
}

@MainActor
final class TaskPrivacyController {
    static let shared = TaskPrivacyController()
    static let minimumSeconds: TimeInterval = 60
    static let maximumSeconds: TimeInterval = 24 * 60 * 60
    static let tickSeconds: TimeInterval = 0.35

    private let sessionState: @MainActor () -> Bool?
    private let requestSystemLock: @MainActor () throws -> String
    private let displayGuard = PrivacyDisplayGuard()
    private let guardian = PrivacyGuardianClient()
    private var handoff = TaskPrivacyHandoffState()
    private var active: Bool { handoff.active }
    private var pausedByUser: Bool { handoff.pausedByUser }
    private var expiresAtUptime: TimeInterval?
    private var ticker: Timer?
    private var screenObserver: NSObjectProtocol?
    private var panels: [PrivacyShieldPanel] = []
    private var shieldVisible = false
    private var observedLocked: Bool?
    private var authenticationLockRequested = false
    private var sawSystemLock = false
    private var lockRequestInFlight = false
    private var lockRequestedAt: TimeInterval?
    private var lastLockRoute: String?
    private var lastError: String?
    private var lastTransition = "inactive"
    private var transitionCount = 0
    private var preparingSince: TimeInterval?
    private var unlockToken: UUID?
    private var unlockAttempt: TaskPrivacyUnlockAttempt?
    private var unlockTask: Task<Void, Never>?
    private var releaseAfterHandoff = false

    nonisolated init(
        sessionState: @escaping @MainActor () -> Bool? = NativePlatform.sessionLocked,
        requestSystemLock: @escaping @MainActor () throws -> String = TaskPrivacySystemLock.request
    ) {
        self.sessionState = sessionState
        self.requestSystemLock = requestSystemLock
    }

    @discardableResult
    func hold(seconds: TimeInterval, lockedUse: Bool = true) -> Bool {
        guard !pausedByUser else { return false }
        let clamped = min(max(seconds, Self.minimumSeconds), Self.maximumSeconds)
        Self.trace("hold \(Int(clamped))s active=\(active) paused=\(pausedByUser) locked=\(String(describing: sessionState()))")
        expiresAtUptime = ProcessInfo.processInfo.systemUptime + clamped
        if handoff.beginTask() {
            PrivacyWallpaper.shared.prepare()
            authenticationLockRequested = false
            sawSystemLock = false
            lockRequestInFlight = false
            lastError = nil
            releaseAfterHandoff = false
            // A switch applies only when this lease starts. Renewals keep the
            // capability decision already attached to the running task.
            if lockedUse {
                LockedUseAuthorizationServer.shared.start()
                LockedUseAuthorizationServer.shared.beginTask()
            }
            startTicker()
            observeScreenChanges()
            transition("waiting-for-system-lock")
        }
        refresh()
        return !pausedByUser
    }

    func release() {
        Self.trace("release active=\(active)")
        // Once we have replaced the user's lock screen, finishing the task
        // must restore the real lock before removing the last desktop cover.
        if handoff.coverRequired {
            releaseAfterHandoff = true
            requestAuthenticationScreen()
            return
        }
        cancelAutomaticUnlock()
        handoff.release()
        stopProtection(completedHandoff: false)
        LockedUseAuthorizationServer.shared.releaseTask()
        transition("inactive")
    }

    private func stopProtection(completedHandoff: Bool) {
        expiresAtUptime = nil
        lockRequestInFlight = false
        ticker?.invalidate()
        ticker = nil
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
        screenObserver = nil
        displayGuard.release()
        PowerGuard.shared.release(reason: "privacy-handoff")
        LockedUseAuthorizationServer.shared.endTask()
        if completedHandoff { guardian.completeHandoff() }
        else { guardian.stop() }
        destroyPanels()
        PrivacyWallpaper.shared.clear()
    }

    func status() -> JSONValue {
        .object([
            "active": .bool(active),
            "pausedByUser": .bool(pausedByUser),
            "phase": .string(handoff.phase.rawValue),
            "automaticUnlockAttempted": .bool(handoff.automaticUnlockAttempted),
            "guardianReady": .bool(guardian.ready),
            "guardianError": guardian.lastError.map(JSONValue.string) ?? .null,
            "shieldVisible": .bool(shieldVisible),
            "displayCount": .number(Double(panels.count)),
            "systemLocked": observedLocked.map(JSONValue.bool) ?? .null,
            "idleDisplaySleepPrevented": .bool(displayGuard.isHolding),
            "authenticationLockRequested": .bool(authenticationLockRequested),
            "sawSystemLock": .bool(sawSystemLock),
            "lockRequestInFlight": .bool(lockRequestInFlight),
            "lastLockRoute": lastLockRoute.map(JSONValue.string) ?? .null,
            "lastError": lastError.map(JSONValue.string) ?? .null,
            "lastTransition": .string(lastTransition),
            "transitionCount": .number(Double(transitionCount)),
            "lockedUse": LockedUseAuthorizationServer.shared.status,
            "remainingSeconds": expiresAtUptime.map {
                .number(max(0, $0 - ProcessInfo.processInfo.systemUptime).rounded())
            } ?? .number(0),
            "limits": .object([
                "idleDisplaySleep": .string(displayGuard.isHolding ? "prevented" : "not prevented"),
                "idleScreenLock": .string(displayGuard.isHolding ? "prevented" : "not prevented"),
                "manualSystemLock": .string("allowed"),
                "lidCloseLock": .string("not prevented"),
                "touchIDLock": .string("not prevented"),
                "credentials": .string("not requested or stored"),
            ]),
        ])
    }

    private func startTicker() {
        ticker?.invalidate()
        let timer = Timer(timeInterval: Self.tickSeconds, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        ticker = timer
    }

    private func observeScreenChanges() {
        guard screenObserver == nil else { return }
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.handoff.coverRequired else { return }
                self.createPanels()
                self.orderShield(activate: self.sessionState() == false)
            }
        }
    }

    private func tick() {
        guard active else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if let expiry = expiresAtUptime, now >= expiry {
            lastError = "任务隐私保护租约已到期"
            release()
            return
        }
        if handoff.coverRequired { guardian.pulse() }
        refresh()
    }

    private func refresh() {
        guard active else { return }
        let locked = sessionState()
        observedLocked = locked
        let authorization = LockedUseAuthorizationServer.shared
        if locked == true { authorization.noteSessionLocked() }
        if locked == false { authorization.noteSessionUnlocked() }

        switch handoff.phase {
        case .watching:
            guard locked == true, handoff.observeExternalLock() else { return }
            preparingSince = ProcessInfo.processInfo.systemUptime
            createPanels()
            orderShield(activate: false)
            do { try displayGuard.hold() }
            catch { lastError = error.localizedDescription }
            guardian.onTakeover = { [weak self] in self?.guardianTakeover() }
            guardian.start()
            guard handoff.phase == .preparingUnlock else { return }
            transition("preparing-locked-use")
        case .preparingUnlock:
            if let start = preparingSince, ProcessInfo.processInfo.systemUptime - start >= 4 {
                lastError = "自动解锁未就绪，保留系统锁屏：\(guardian.lastError ?? authorization.availabilityReason())"
                requestAuthenticationScreen()
                return
            }
            guard locked == true else {
                if locked == false { requestAuthenticationScreen() }
                return
            }
            if guardian.ready, authorization.armExternalLock(),
               let token = handoff.beginAutomaticUnlock() {
                transition("automatic-unlock")
                triggerLockedUseUnlock(token: token)
            }
        case .unlocking:
            if locked == false, let token = unlockToken, handoff.automaticUnlockCompleted(token) {
                cancelAutomaticUnlock()
                guardian.armTakeover()
                guard handoff.phase == .shielded else { return }
                armShieldTakeover()
                orderShield(activate: true)
                transition("shield-visible")
            }
        case .shielded:
            if locked == true {
                // A task gets one automatic unlock. A subsequent lock belongs
                // to the user, including another press of the system shortcut.
                requestAuthenticationScreen()
            } else if locked == false {
                orderShield(activate: false)
            }
        case .handingBack:
            if locked == true { authorization.noteSessionLocked() }
            if handoff.completeHandoff(sessionLocked: locked,
                                       authorizationReady: authorization.canCompleteManualHandoff) {
                sawSystemLock = true
                lastError = nil
                stopProtection(completedHandoff: true)
                transition("system-lock-handed-back")
                if releaseAfterHandoff {
                    handoff.release()
                    authorization.releaseTask()
                }
            } else {
                // A consumed ALLOW may still unlock the old lock. Keep both
                // covers until it has landed and a new real lock is observed.
                if locked == false { orderShield(activate: true) }
                requestLockIfNeeded(locked: locked)
            }
        case .inactive, .paused:
            break
        }
    }

    private func guardianTakeover() {
        guard active else { return }
        Self.trace("guardian takeover received")
        requestAuthenticationScreen()
    }

    private func requestAuthenticationScreen() {
        guard handoff.coverRequired else { return }
        if handoff.requestTakeover() {
            let authorization = LockedUseAuthorizationServer.shared
            authorization.suppressForManualAuthentication()
            authorization.renewProtection(ready: false)
            authorization.endTask()
            cancelAutomaticUnlock()
            authenticationLockRequested = true
            expiresAtUptime = nil
            lockRequestedAt = nil
            transition("handing-back-to-system")
        }
        refresh()
    }

    private func requestLockIfNeeded(locked: Bool?) {
        guard locked != true, !lockRequestInFlight else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if let requested = lockRequestedAt, now - requested < 1.5 { return }
        lockRequestInFlight = true
        defer { lockRequestInFlight = false }
        lockRequestedAt = now
        do {
            lastLockRoute = try requestSystemLock()
            Self.trace("system lock requested route=\(lastLockRoute ?? "?")")
            lastError = "等待 macOS 确认系统锁屏，浮层继续覆盖"
        } catch let failure as NativeFailure {
            lastError = failure.message
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// Ask loginwindow to evaluate the screensaver right by driving its own
    /// credential UI through the accessibility API. A synthetic key event is
    /// not an option: loginwindow swallows it while the session is locked, and
    /// the guardian reads it as hardware input and revokes the grant.
    private func triggerLockedUseUnlock(token: UUID) {
        let attempt = TaskPrivacyUnlockAttempt()
        unlockToken = token
        unlockAttempt = attempt
        unlockTask = Task.detached(priority: .userInitiated) { [weak self] in
            var failure: String?
            do {
                // Wait for lazy loginwindow UI, but submit credentials at most
                // once. The token and authorization lease guard every AX write.
                for _ in 0..<20 {
                    try attempt.check()
                    guard NativePlatform.sessionLocked() == true else { break }
                    let result = try LockScreenUnlocker.shared.unlock(attempt: attempt)
                    TaskPrivacyController.trace("AX unlock: \(result)")
                    if result["verdict"] != .string("no-credential-field") { break }
                    try await Task.sleep(for: .milliseconds(350))
                }
                // ALLOW delivery and loginwindow's actual transition are
                // asynchronous. Never send a second submission while waiting.
                for _ in 0..<40 {
                    try attempt.check()
                    if NativePlatform.sessionLocked() == false { break }
                    try await Task.sleep(for: .milliseconds(250))
                }
                try attempt.check()
                if NativePlatform.sessionLocked() != false { failure = "自动解锁未完成，交还系统锁屏" }
            } catch {
                failure = String(describing: error)
            }
            await self?.automaticUnlockFinished(token: token, failure: failure)
        }
    }

    private func automaticUnlockFinished(token: UUID, failure: String?) {
        guard handoff.permitsAutomaticUnlock(token) else { return }
        if sessionState() == false { refresh() }
        else {
            lastError = failure ?? "自动解锁未完成，交还系统锁屏"
            requestAuthenticationScreen()
        }
    }

    private func cancelAutomaticUnlock() {
        unlockAttempt?.cancel()
        unlockTask?.cancel()
        unlockTask = nil
        unlockAttempt = nil
        unlockToken = nil
    }

    /// Diagnostic requests share the controller's current episode and cannot
    /// create a second submission or outlive a user takeover.
    func currentUnlockAttempt() throws -> TaskPrivacyUnlockAttempt {
        guard let token = unlockToken, handoff.permitsAutomaticUnlock(token), let attempt = unlockAttempt else {
            throw NativeFailure(code: "AUTOMATIC_UNLOCK_NOT_ALLOWED", message: "当前没有可用的自动解锁任务")
        }
        return attempt
    }

    private func armShieldTakeover() {
        for panel in panels {
            (panel.contentView as? PrivacyShieldView)?.armTakeover()
        }
    }

    /// Forensic channel: stderr is inherited by the driver that launched us.
    nonisolated static func trace(_ message: String) {
        Trace.log("privacy \(message)")
        FileHandle.standardError.write(Data("[privacy] \(message)\n".utf8))
    }

    private func transition(_ value: String) {
        lastTransition = value
        transitionCount += 1
    }

    private func orderShield(activate: Bool) {
        guard handoff.coverRequired else { return }
        if shieldVisible {
            for panel in panels {
                guard let screen = panel.privacyScreen else { continue }
                if panel.frame != screen.frame { panel.setFrame(screen.frame, display: true) }
            }
        } else {
            if NSApp.activationPolicy() != .accessory { NSApp.setActivationPolicy(.accessory) }
            if panels.isEmpty { createPanels() }
            for panel in panels {
                panel.setFrame(panel.privacyScreen?.frame ?? panel.frame, display: true)
                panel.orderFrontRegardless()
                panel.makeFirstResponder(panel.contentView)
            }
        }
        if activate, sessionState() == false {
            panels.first?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        shieldVisible = true
    }

    private func hideShield() {
        for panel in panels { panel.orderOut(nil) }
        shieldVisible = false
    }

    private func destroyPanels() {
        for panel in panels { panel.orderOut(nil) }
        panels.removeAll()
        shieldVisible = false
    }

    private func createPanels() {
        let previous = panels
        panels = []
        for (index, screen) in NSScreen.screens.enumerated() {
            let panel = PrivacyShieldPanel(
                contentRect: screen.frame,
                styleMask: [.borderless],
                backing: .buffered,
                defer: false)
            panel.privacyScreen = screen
            panel.isOpaque = true
            panel.backgroundColor = PrivacyShieldView.background
            panel.hasShadow = false
            panel.isMovable = false
            panel.hidesOnDeactivate = false
            panel.ignoresMouseEvents = false
            panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
            panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.screenSaverWindow)) + 1)
            panel.contentView = PrivacyShieldView(screenIndex: index) { [weak self] in
                self?.requestAuthenticationScreen()
            }
            if handoff.phase == .shielded { (panel.contentView as? PrivacyShieldView)?.armTakeover() }
            panel.orderFrontRegardless()
            panels.append(panel)
        }
        previous.forEach { $0.orderOut(nil) }
    }
}

final class PrivacyShieldPanel: NSPanel {
    weak var privacyScreen: NSScreen?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class PrivacyShieldView: NSView {
    static let background = NSColor(srgbRed: 0.29, green: 0.52, blue: 0.25, alpha: 1)

    private let screenIndex: Int
    private let interaction: () -> Void
    private var inputPolicy = PrivacyGuardianInputPolicy()
    private var wallpaperImage: NSImage?
    private var wallpaperVideo: PrivacyWallpaperVideo?
    private var visibilityObservation: NSKeyValueObservation?

    init(screenIndex: Int, interaction: @escaping () -> Void) {
        self.screenIndex = screenIndex
        self.interaction = interaction
        super.init(frame: .zero)
        setAccessibilityElement(true)
        setAccessibilityRole(.window)
        setAccessibilityLabel("workbuddy 正在使用你的 Mac。按任意键或点击，进入系统登录锁屏。")
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard let screen = window?.screen else { return }
        if let url = PrivacyWallpaper.shared.fileURL(for: screen),
           ["mov", "mp4", "m4v"].contains(url.pathExtension.lowercased()) {
            guard wallpaperVideo == nil else { return }
            wallpaperVideo = PrivacyWallpaperVideo(url: url)
            let videoView = NSView(frame: bounds)
            videoView.wantsLayer = true
            videoView.layer = wallpaperVideo?.layer
            videoView.autoresizingMask = [.width, .height]
            addSubview(videoView)
            let artwork = PrivacyWallpaperArtwork(frame: bounds)
            artwork.autoresizingMask = [.width, .height]
            addSubview(artwork)
            visibilityObservation = window?.observe(\.isVisible, options: [.initial, .new]) { [weak self] _, change in
                let visible = change.newValue ?? false
                Task { @MainActor [weak self] in self?.wallpaperVideo?.setVisible(visible) }
            }
        } else {
            wallpaperImage = PrivacyWallpaper.shared.snapshot(for: screen)
                ?? PrivacyWallpaper.shared.fileURL(for: screen).flatMap { NSImage(contentsOf: $0) }
        }
        needsDisplay = true
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        bounds.contains(point) ? self : nil
    }

    required init?(coder: NSCoder) { nil }

    override var isOpaque: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func becomeFirstResponder() -> Bool { true }

    func armTakeover() {
        inputPolicy.arm(flags: CGEventSource.flagsState(.combinedSessionState))
    }

    private func interact(with event: NSEvent) {
        guard let input = event.cgEvent,
              inputPolicy.accepts(flagsChanged: event.type == .flagsChanged, flags: input.flags,
                                  sourcePID: input.getIntegerValueField(.eventSourceUnixProcessID),
                                  userData: input.getIntegerValueField(.eventSourceUserData)) else { return }
        interaction()
    }

    override func keyDown(with event: NSEvent) { interact(with: event) }
    override func flagsChanged(with event: NSEvent) { interact(with: event) }
    override func mouseDown(with event: NSEvent) { interact(with: event) }
    override func rightMouseDown(with event: NSEvent) { interact(with: event) }
    override func otherMouseDown(with event: NSEvent) { interact(with: event) }
    override func scrollWheel(with event: NSEvent) { interact(with: event) }
    override func magnify(with event: NSEvent) { interact(with: event) }
    override func rotate(with event: NSEvent) { interact(with: event) }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        if wallpaperVideo == nil {
            Self.drawArtwork(in: bounds, context: context, screenIndex: screenIndex, wallpaperImage: wallpaperImage)
        } else {
            NSColor.black.setFill()
            context.fill(bounds)
        }
    }

    static func drawArtwork(in bounds: CGRect, context: CGContext, screenIndex: Int,
                            wallpaperImage: NSImage? = nil, drawBackdrop: Bool = true) {
        if drawBackdrop, let wallpaperImage {
            let size = wallpaperImage.size
            if size.width > 0, size.height > 0 {
                let scale = max(bounds.width / size.width, bounds.height / size.height)
                wallpaperImage.draw(in: CGRect(x: bounds.midX - size.width * scale / 2,
                                               y: bounds.midY - size.height * scale / 2,
                                               width: size.width * scale, height: size.height * scale))
            }
        } else if drawBackdrop {
        let sky = NSColor(srgbRed: 0.45, green: 0.66, blue: 0.37, alpha: 1)
        let earth = NSColor(srgbRed: 0.24, green: 0.47, blue: 0.21, alpha: 1)
        if let gradient = CGGradient(
            colorsSpace: CGColorSpaceCreateDeviceRGB(),
            colors: [sky.cgColor, earth.cgColor] as CFArray,
            locations: [0, 1]
        ) {
            context.drawLinearGradient(
                gradient,
                start: CGPoint(x: bounds.minX, y: bounds.maxY),
                end: CGPoint(x: bounds.maxX, y: bounds.minY),
                options: [])
        } else {
            background.setFill()
            context.fill(bounds)
        }

        let pixel = NSColor(srgbRed: 0.13, green: 0.32, blue: 0.12, alpha: 0.17)
        drawDiagonalGrid(in: CGRect(x: -bounds.width * 0.10, y: bounds.height * 0.56,
                                    width: bounds.width * 0.36, height: bounds.height * 0.57),
                         angle: .pi / 4, rows: 18, columns: 22,
                         spacing: max(8, bounds.height / 98), color: pixel, context: context)
        drawDiagonalGrid(in: CGRect(x: bounds.width * 0.78, y: -bounds.height * 0.05,
                                    width: bounds.width * 0.34, height: bounds.height * 0.72),
                         angle: -.pi / 4, rows: 24, columns: 16,
                         spacing: max(8, bounds.height / 94), color: pixel, context: context)
        drawMap(in: CGPoint(x: bounds.width * 0.24, y: bounds.height * 0.29),
                radius: min(bounds.width, bounds.height) * 0.15,
                pixel: max(5, bounds.height / 126), context: context)
        }
        if wallpaperImage != nil || !drawBackdrop {
            context.setFillColor(NSColor.black.withAlphaComponent(0.18).cgColor)
            context.fill(bounds)
        }

        let center = CGPoint(x: bounds.midX, y: bounds.midY + bounds.height * 0.035)
        let markScale = max(0.82, min(1.12, bounds.height / 900))
        drawMark(at: center, scale: markScale, context: context)

        let title = NSMutableAttributedString(
            string: "workbuddy is using your Mac",
            attributes: [
                .font: NSFont.systemFont(ofSize: max(17, bounds.height / 45), weight: .medium),
                .foregroundColor: NSColor.white.withAlphaComponent(0.93),
            ])
        let subtitle = NSMutableAttributedString(
            string: "任务运行中 · 输入由 workbuddy 保护",
            attributes: [
                .font: NSFont.systemFont(ofSize: max(11.5, bounds.height / 76), weight: .regular),
                .foregroundColor: NSColor.white.withAlphaComponent(0.68),
            ])
        let instruction = NSMutableAttributedString(
            string: "按任意键或点击，进入系统登录锁屏",
            attributes: [
                .font: NSFont.systemFont(ofSize: max(10.5, bounds.height / 93), weight: .regular),
                .foregroundColor: NSColor.white.withAlphaComponent(0.55),
            ])
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        title.addAttribute(.paragraphStyle, value: paragraph,
                           range: NSRange(location: 0, length: title.length))
        subtitle.addAttribute(.paragraphStyle, value: paragraph,
                              range: NSRange(location: 0, length: subtitle.length))
        instruction.addAttribute(.paragraphStyle, value: paragraph,
                                 range: NSRange(location: 0, length: instruction.length))
        // Anchor text below the actual logo bounds. Percentage-based text boxes
        // grew upward into the minimum-size logo on shorter displays.
        let textWidth = bounds.width * 0.8
        var textTop = center.y - 38 * markScale - max(24, bounds.height * 0.028)
        for (text, gap) in [(title, CGFloat(12)), (subtitle, CGFloat(14)),
                            (instruction, CGFloat(0))] {
            let measured = text.boundingRect(
                with: CGSize(width: textWidth, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading])
            let height = ceil(measured.height) + 2
            text.draw(in: CGRect(x: bounds.midX - textWidth / 2, y: textTop - height,
                                 width: textWidth, height: height))
            textTop -= height + gap
        }
    }

    private static func drawDiagonalGrid(
        in rect: CGRect, angle: CGFloat, rows: Int, columns: Int, spacing: CGFloat,
        color: NSColor, context: CGContext
    ) {
        context.saveGState()
        context.translateBy(x: rect.midX, y: rect.midY)
        context.rotate(by: angle)
        context.setFillColor(color.cgColor)
        for row in 0..<rows {
            for column in 0..<columns {
                guard (row * 7 + column * 11) % 5 != 0 else { continue }
                let x = (CGFloat(column) - CGFloat(columns - 1) / 2) * spacing
                let y = (CGFloat(row) - CGFloat(rows - 1) / 2) * spacing
                let width = spacing * ((row + column) % 4 == 0 ? 0.88 : 0.58)
                context.fill(CGRect(x: x, y: y, width: width, height: width))
            }
        }
        context.restoreGState()
    }

    private static func drawMap(
        in center: CGPoint, radius: CGFloat, pixel: CGFloat, context: CGContext
    ) {
        context.saveGState()
        context.setFillColor(NSColor(srgbRed: 0.13, green: 0.33, blue: 0.12, alpha: 0.30).cgColor)
        let rings = 9
        for ring in 0..<rings {
            let ringRadius = radius * CGFloat(ring + 1) / CGFloat(rings)
            let count = max(8, 10 + ring * 4)
            for index in 0..<count {
                guard (index + ring * 3) % 4 != 0 else { continue }
                let theta = CGFloat(index) / CGFloat(count) * 2 * .pi
                let point = CGPoint(x: center.x + cos(theta) * ringRadius,
                                    y: center.y + sin(theta) * ringRadius * 0.72)
                let size = pixel * (ring % 2 == 0 ? 1.0 : 0.72)
                context.fill(CGRect(x: point.x - size / 2, y: point.y - size / 2,
                                    width: size, height: size))
            }
        }
        context.restoreGState()
    }

    private static func drawMark(at center: CGPoint, scale: CGFloat, context: CGContext) {
        let radius = 38 * scale
        context.saveGState()
        context.setShadow(offset: .zero, blur: 24,
                          color: NSColor(srgbRed: 0.08, green: 0.25, blue: 0.07, alpha: 0.28).cgColor)
        context.setFillColor(NSColor.white.withAlphaComponent(0.92).cgColor)
        context.fillEllipse(in: CGRect(x: center.x - radius, y: center.y - radius,
                                       width: radius * 2, height: radius * 2))
        context.restoreGState()

        let innerRadius = radius * 0.72
        let colors = [
            NSColor(srgbRed: 0.19, green: 0.49, blue: 0.33, alpha: 1).cgColor,
            NSColor(srgbRed: 0.11, green: 0.25, blue: 0.20, alpha: 1).cgColor,
        ] as CFArray
        if let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                     colors: colors, locations: [0, 1]) {
            context.saveGState()
            context.addEllipse(in: CGRect(x: center.x - innerRadius, y: center.y - innerRadius,
                                          width: innerRadius * 2, height: innerRadius * 2))
            context.clip()
            context.drawLinearGradient(gradient,
                                       start: CGPoint(x: center.x - innerRadius, y: center.y + innerRadius),
                                       end: CGPoint(x: center.x + innerRadius, y: center.y - innerRadius),
                                       options: [])
            context.restoreGState()
        }

        let text = NSAttributedString(
            string: "w",
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 30 * scale, weight: .semibold),
                .foregroundColor: NSColor.white.withAlphaComponent(0.92),
            ])
        let size = text.size()
        text.draw(at: CGPoint(x: center.x - size.width / 2, y: center.y - size.height / 2 + 1))
    }
}

private final class PrivacyWallpaperArtwork: NSView {
    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        PrivacyShieldView.drawArtwork(in: bounds, context: context, screenIndex: 0, drawBackdrop: false)
    }
}
