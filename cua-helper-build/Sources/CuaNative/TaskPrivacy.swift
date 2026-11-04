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
    private var active = false
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
    private var externalUnlockTriggered = false
    /// 用户通过系统界面亲自解锁后置位：交出控制权，不再重新覆盖，也不因后续按键反复回锁。
    private var pausedByUser = false

    nonisolated init(
        sessionState: @escaping @MainActor () -> Bool? = NativePlatform.sessionLocked,
        requestSystemLock: @escaping @MainActor () throws -> String = TaskPrivacySystemLock.request
    ) {
        self.sessionState = sessionState
        self.requestSystemLock = requestSystemLock
    }

    func hold(seconds: TimeInterval) {
        if pausedByUser { return }                  // 已交给用户，不再重新覆盖
        let clamped = min(max(seconds, Self.minimumSeconds), Self.maximumSeconds)
        Self.trace("hold \(Int(clamped))s active=\(active) paused=\(pausedByUser) locked=\(String(describing: sessionState()))")
        expiresAtUptime = ProcessInfo.processInfo.systemUptime + clamped
        if !active {
            active = true
            authenticationLockRequested = false
            sawSystemLock = false
            lockRequestInFlight = false
            lastError = nil
            externalUnlockTriggered = false
            LockedUseAuthorizationServer.shared.start()
            LockedUseAuthorizationServer.shared.beginTask()
            guardian.onTakeover = { [weak self] in self?.guardianTakeover() }
            guardian.start()
            expireLockRequestIfUnverified(now: ProcessInfo.processInfo.systemUptime)
            startTicker()
            observeScreenChanges()
            transition("task-active")
        }
        do {
            try displayGuard.hold()
        } catch let failure as NativeFailure {
            lastError = failure.message
        } catch {
            lastError = error.localizedDescription
        }
        refresh()
    }

    func release() {
        Self.trace("release active=\(active)")
        guard active else { hideShield(); pausedByUser = false; return }
        active = false
        pausedByUser = false
        expiresAtUptime = nil
        authenticationLockRequested = false
        sawSystemLock = false
        lockRequestInFlight = false
        lockRequestedAt = nil
        ticker?.invalidate()
        ticker = nil
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
        screenObserver = nil
        displayGuard.release()
        LockedUseAuthorizationServer.shared.endTask()
        guardian.stop()
        destroyPanels()
        lastTransition = "inactive"
        transitionCount += 1
    }

    func status() -> JSONValue {
        .object([
            "active": .bool(active),
            "pausedByUser": .bool(pausedByUser),
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
                guard let self, self.active, self.shieldVisible else { return }
                self.destroyPanels()
                self.showShield()
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
        guardian.pulse()
        expireLockRequestIfUnverified(now: now)
        refresh()
    }

    private func expireLockRequestIfUnverified(now: TimeInterval) {
        guard authenticationLockRequested, !sawSystemLock, !lockRequestInFlight,
              let requested = lockRequestedAt, now - requested >= 2.5 else { return }
        lockRequestedAt = now
        lastError = "系统尚未确认锁屏；保护层继续覆盖，新的输入会重试"
        transition("lock-unconfirmed")
    }

    private func refresh() {
        guard active, !pausedByUser else { hideShield(); return }
        let locked = sessionState()
        observedLocked = locked
        if locked == true {
            LockedUseAuthorizationServer.shared.noteSessionLocked()
            // Leave the desktop's cover ordered in before any authorization
            // grant. loginwindow remains the system-owned screen above it.
            // This reduces the polling gap, but is not an independent guardian.
            if panels.isEmpty { createPanels() }
            for panel in panels { panel.orderFrontRegardless() }
            if authenticationLockRequested { sawSystemLock = true }
            if authenticationLockRequested {
                Self.trace("locked while authRequested → suppress lease")
                LockedUseAuthorizationServer.shared.suppressForManualAuthentication()
            } else if !externalUnlockTriggered {
                Self.trace("locked externally → arm external unlock")
                if LockedUseAuthorizationServer.shared.armExternalLock() {
                    externalUnlockTriggered = true
                    Self.trace("external lock armed → AX unlock")
                    triggerLockedUseUnlock()
                } else {
                    Self.trace("armExternalLock refused")
                }
            }
            if authenticationLockRequested, lastTransition != "system-locked" {
                transition("system-locked")
            }
            return
        }
        if locked == false {
            LockedUseAuthorizationServer.shared.noteSessionUnlocked()
            externalUnlockTriggered = false
            if authenticationLockRequested, sawSystemLock {
                Self.trace("unlocked after system lock → hand back to user")
                // 用户已经在系统界面完成认证：把控制权交回去。
                // 旧逻辑在这里 showShield() + requestAuthenticationScreen() 立刻回锁，
                // 造成「解锁后按任意键又被锁上」的死循环。
                authenticationLockRequested = false
                sawSystemLock = false
                lockRequestedAt = nil
                lastError = nil
                guardian.manualUnlockConfirmed()
                pauseForUserTakeover()
                return
            }
            showShield()
            return
        }
        // Unknown does not prove that the desktop is exposed. Keep the existing
        // shield in place rather than removing the only input boundary.
        if shieldVisible { return }
        hideShield()
        lastError = "无法验证当前 macOS 会话状态；保护层等待会话恢复"
    }

    /// 交出控制权：撤掉覆盖层、释放电源断言、结束票据分支，并保持暂停直到任务真正结束。
    /// 之后 `hold()` 直接返回，避免下一次动作又把屏幕盖上。
    private func pauseForUserTakeover() {
        guard active else { return }
        Self.trace("pause for user takeover")
        active = false
        pausedByUser = true
        expiresAtUptime = nil
        ticker?.invalidate()
        ticker = nil
        displayGuard.release()
        destroyPanels()
        guardian.stop()
        LockedUseAuthorizationServer.shared.endTask()
        transition("user-took-over")
    }

    private func guardianTakeover() {
        guard active else { return }
        Self.trace("guardian takeover received")
        requestAuthenticationScreen()
        authenticationLockRequested = true
        if sessionState() == true { sawSystemLock = true }
        lockRequestedAt = ProcessInfo.processInfo.systemUptime
    }

    private func requestAuthenticationScreen() {
        guard active, shieldVisible, !lockRequestInFlight, !sawSystemLock else { return }
        lockRequestInFlight = true
        authenticationLockRequested = true
        LockedUseAuthorizationServer.shared.suppressForManualAuthentication()
        lockRequestedAt = ProcessInfo.processInfo.systemUptime
        do {
            lastLockRoute = try requestSystemLock()
            Self.trace("system lock requested route=\(lastLockRoute ?? "?")")
            lastError = nil
        } catch let failure as NativeFailure {
            lastError = failure.message
            authenticationLockRequested = false
            lockRequestedAt = nil
        } catch {
            lastError = error.localizedDescription
            authenticationLockRequested = false
            lockRequestedAt = nil
        }
        lockRequestInFlight = false
        transition("authentication-requested")
        refresh()
    }

    /// Ask loginwindow to evaluate the screensaver right by driving its own
    /// credential UI through the accessibility API. A synthetic key event is
    /// not an option: loginwindow swallows it while the session is locked, and
    /// the guardian reads it as hardware input and revokes the grant.
    private func triggerLockedUseUnlock() {
        Task.detached(priority: .userInitiated) {
            var tries = 0
            // loginwindow builds its credential UI a moment after the lock, so
            // poll for it instead of spending the submission budget on an empty
            // tree. The unlocker still caps real submissions at three.
            while tries < 20 {
                tries += 1
                guard NativePlatform.sessionLocked() == true else { return }
                do {
                    let result = try LockScreenUnlocker.shared.unlock()
                    TaskPrivacyController.trace("AX unlock try \(tries): \(result)")
                } catch {
                    TaskPrivacyController.trace("AX unlock try \(tries) failed: \(error)")
                }
                if NativePlatform.sessionLocked() == false { return }
                usleep(600_000)
            }
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

    private func showShield() {
        guard active, sessionState() == false else { hideShield(); return }
        if shieldVisible {
            for panel in panels {
                guard let screen = panel.privacyScreen else { continue }
                if panel.frame != screen.frame { panel.setFrame(screen.frame, display: true) }
            }
            return
        }
        if NSApp.activationPolicy() != .accessory { NSApp.setActivationPolicy(.accessory) }
        if panels.isEmpty { createPanels() }
        for panel in panels {
            panel.setFrame(panel.privacyScreen?.frame ?? panel.frame, display: true)
            panel.orderFrontRegardless()
            panel.makeFirstResponder(panel.contentView)
        }
        panels.first?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        shieldVisible = true
        if lastTransition != "shield-visible" { transition("shield-visible") }
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
        panels.forEach { $0.orderOut(nil) }
        panels.removeAll()
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
            panels.append(panel)
        }
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

    init(screenIndex: Int, interaction: @escaping () -> Void) {
        self.screenIndex = screenIndex
        self.interaction = interaction
        super.init(frame: .zero)
        setAccessibilityElement(true)
        setAccessibilityRole(.window)
        setAccessibilityLabel("workbuddy 正在使用你的 Mac。按任意键或点击，进入系统登录锁屏。")
    }

    required init?(coder: NSCoder) { nil }

    override var isOpaque: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func becomeFirstResponder() -> Bool { true }

    private func interact(with event: NSEvent) {
        guard event.cgEvent?.getIntegerValueField(.eventSourceUserData) != TaskPrivacySyntheticEvents.marker else { return }
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
        Self.drawArtwork(in: bounds, context: context, screenIndex: screenIndex)
    }

    static func drawArtwork(in bounds: CGRect, context: CGContext, screenIndex: Int) {
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

        let center = CGPoint(x: bounds.midX, y: bounds.midY + bounds.height * 0.035)
        drawMark(at: center, scale: max(0.82, min(1.12, bounds.height / 900)), context: context)

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
        let titleY = center.y - max(56, bounds.height * 0.09)
        title.draw(in: CGRect(x: bounds.midX - bounds.width * 0.4, y: titleY,
                              width: bounds.width * 0.8, height: bounds.height * 0.05))
        subtitle.draw(in: CGRect(x: bounds.midX - bounds.width * 0.4, y: titleY - bounds.height * 0.038,
                                 width: bounds.width * 0.8, height: bounds.height * 0.035))
        instruction.draw(in: CGRect(x: bounds.midX - bounds.width * 0.4, y: titleY - bounds.height * 0.075,
                                    width: bounds.width * 0.8, height: bounds.height * 0.035))
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
