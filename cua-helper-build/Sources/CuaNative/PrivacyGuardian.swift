// A separate copy of this executable owns an additional desktop cover and an
// input listener. Its stdin/stdout pipes are inherited only by the task helper.
import AppKit
import CoreGraphics
import Darwin
import Foundation

@MainActor
final class PrivacyGuardianClient {
    private var process: Process?
    private var commands: FileHandle?
    private var reports: FileHandle?
    private var generation = UUID()
    private var buffered = Data()
    private var lastReportAt: TimeInterval = 0
    private var announcedReady = false
    var ready: Bool { announcedReady && ProcessInfo.processInfo.systemUptime - lastReportAt < 1 }
    private(set) var lastError: String?
    var onTakeover: (() -> Void)?

    nonisolated init() {}

    func start() {
        guard process == nil else { return }
        generation = UUID()
        let currentGeneration = generation
        let child = Process()
        let input = Pipe(), output = Pipe()
        child.executableURL = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
        child.arguments = ["--privacy-guardian"]
        child.standardInput = input
        child.standardOutput = output
        child.standardError = FileHandle.standardError
        reports = output.fileHandleForReading
        reports?.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            Task { @MainActor [weak self] in
                guard let self, self.generation == currentGeneration else { return }
                self.receive(data)
            }
        }
        child.terminationHandler = { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.generation == currentGeneration else { return }
                self.announcedReady = false
                LockedUseAuthorizationServer.shared.renewProtection(ready: false)
                self.lastError = "隐私守护进程已退出"
                LockedUseAuthorizationServer.shared.suppressForManualAuthentication()
                self.onTakeover?()
            }
        }
        do {
            try child.run()
            input.fileHandleForReading.closeFile()
            output.fileHandleForWriting.closeFile()
            process = child
            commands = input.fileHandleForWriting
        } catch {
            announcedReady = false
            lastError = error.localizedDescription
            reports?.readabilityHandler = nil
            reports = nil
        }
    }

    func pulse() {
        guard process?.isRunning == true else {
            announcedReady = false
            LockedUseAuthorizationServer.shared.renewProtection(ready: false)
            return
        }
        send("PING")
        LockedUseAuthorizationServer.shared.renewProtection(ready: ready, validUntil: lastReportAt + 1)
    }

    func manualUnlockConfirmed() { send("RESUME") }

    func stop() {
        generation = UUID()
        announcedReady = false
        LockedUseAuthorizationServer.shared.renewProtection(ready: false)
        send("STOP")
        try? commands?.close()
        commands = nil
        reports?.readabilityHandler = nil
        try? reports?.close()
        reports = nil
        process?.terminationHandler = nil
        process = nil
        buffered.removeAll()
    }

    private func send(_ command: String) {
        do { try commands?.write(contentsOf: Data((command + "\n").utf8)) }
        catch {
            announcedReady = false
            LockedUseAuthorizationServer.shared.renewProtection(ready: false)
            lastError = error.localizedDescription
        }
    }

    private func receive(_ data: Data) {
        guard !data.isEmpty, buffered.count + data.count <= 4096 else {
            announcedReady = false
            LockedUseAuthorizationServer.shared.renewProtection(ready: false)
            lastError = "隐私守护连接中断"
            return
        }
        buffered.append(data)
        while let newline = buffered.firstIndex(of: 10) {
            let line = String(decoding: buffered[..<newline], as: UTF8.self)
            buffered.removeSubrange(...newline)
            switch line {
            case "READY", "ALIVE":
                announcedReady = true
                lastError = nil
                lastReportAt = ProcessInfo.processInfo.systemUptime
            case "TAKEOVER":
                // Revoke synchronously before acknowledging that the guardian
                // may ask macOS to lock. The helper must not auto-unlock it.
                announcedReady = false
                LockedUseAuthorizationServer.shared.renewProtection(ready: false)
                LockedUseAuthorizationServer.shared.suppressForManualAuthentication()
                onTakeover?()
                send("REVOKED")
            default:
                announcedReady = false
                LockedUseAuthorizationServer.shared.renewProtection(ready: false)
                lastError = line
            }
        }
    }
}

@MainActor
final class PrivacyGuardianProcess {
    private var panels: [NSPanel] = []
    private var timer: Timer?
    private var screenObserver: NSObjectProtocol?
    private var inputBuffer = Data()
    private var tap: CFMachPort?
    private var tapSource: CFRunLoopSource?
    private var lastHeartbeat = ProcessInfo.processInfo.systemUptime
    private var takeoverRequestedAt: TimeInterval?
    private var revoked = false
    private var observedLock = false
    private var failed = false
    private var nextLockAttempt: TimeInterval = 0

    func run() {
        signal(SIGPIPE, SIG_IGN)
        NSApp.setActivationPolicy(.accessory)
        coverDisplays()
        if !startInputListener() {
            report("ERROR input monitoring unavailable")
            failed = true
        }
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.coverDisplays()
                if let self, !self.failed, self.takeoverRequestedAt == nil {
                    self.report(self.panels.isEmpty ? "ERROR no display cover" : "READY")
                }
            }
        }
        FileHandle.standardInput.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            Task { @MainActor [weak self] in self?.receive(data) }
        }
        let timer = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        if !failed, !panels.isEmpty { report("READY") }
        NSApp.run()
    }

    private func coverDisplays() {
        let previous = panels
        panels = NSScreen.screens.map { screen in
            let panel = PrivacyShieldPanel(contentRect: screen.frame, styleMask: .borderless,
                                           backing: .buffered, defer: false)
            panel.isOpaque = true
            panel.backgroundColor = PrivacyShieldView.background
            panel.hasShadow = false
            panel.hidesOnDeactivate = false
            panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
            panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.screenSaverWindow)) + 2)
            panel.contentView = PrivacyShieldView(screenIndex: 0) { [weak self] in self?.takeover() }
            panel.orderFrontRegardless()
            return panel
        }
        // Order replacements before removing the old display cover.
        previous.forEach { $0.orderOut(nil) }
    }

    private func startInputListener() -> Bool {
        let mask = [CGEventType.keyDown, .flagsChanged, .leftMouseDown, .rightMouseDown,
                    .otherMouseDown, .scrollWheel].reduce(CGEventMask(0)) { $0 | (1 << $1.rawValue) }
        let callback: CGEventTapCallBack = { _, type, event, context in
            guard let context else { return Unmanaged.passUnretained(event) }
            let guardian = Unmanaged<PrivacyGuardianProcess>.fromOpaque(context).takeUnretainedValue()
            // This tap is attached to the main run loop. Failure to receive
            // input (for example during secure input) still requires real validation.
            MainActor.assumeIsolated {
                if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                    guardian.failed = true
                    guardian.report("ERROR input listener disabled")
                    guardian.takeover()
                } else if event.getIntegerValueField(.eventSourceUnixProcessID) == 0 {
                    guardian.takeover()
                }
            }
            return Unmanaged.passUnretained(event)
        }
        guard let tap = CGEvent.tapCreate(tap: .cghidEventTap, place: .headInsertEventTap,
                                         options: .listenOnly, eventsOfInterest: mask,
                                         callback: callback, userInfo: Unmanaged.passUnretained(self).toOpaque()),
              let source = CFMachPortCreateRunLoopSource(nil, tap, 0) else { return false }
        self.tap = tap
        tapSource = source
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        return CGEvent.tapIsEnabled(tap: tap)
    }

    private func receive(_ data: Data) {
        guard !data.isEmpty, inputBuffer.count + data.count <= 4096 else {
            failed = true
            takeover()
            return
        }
        inputBuffer.append(data)
        while let newline = inputBuffer.firstIndex(of: 10) {
            let line = String(decoding: inputBuffer[..<newline], as: UTF8.self)
            inputBuffer.removeSubrange(...newline)
            switch line {
            case "PING":
                lastHeartbeat = ProcessInfo.processInfo.systemUptime
                if !failed, takeoverRequestedAt == nil, !panels.isEmpty { report("ALIVE") }
            case "RESUME":
                if observedLock, !failed, NativePlatform.sessionLocked() == false {
                    takeoverRequestedAt = nil
                    observedLock = false
                    revoked = false
                    nextLockAttempt = 0
                    report("READY")
                }
            case "REVOKED": revoked = true
            case "STOP":
                if takeoverRequestedAt != nil {
                    // An interrupted task must not cancel a user-requested lock.
                    revoked = true
                    failed = true
                    continue
                }
                panels.forEach { $0.orderOut(nil) }
                exit(0)
            default:
                failed = true
                takeover()
            }
        }
    }

    private func takeover() {
        guard takeoverRequestedAt == nil else { return }
        takeoverRequestedAt = ProcessInfo.processInfo.systemUptime
        report("TAKEOVER")
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastHeartbeat > 1.5 { failed = true; takeover() }
        guard let requested = takeoverRequestedAt else { return }
        // ACK revokes immediately. On helper failure, its protection lease
        // expires after one second, before this fallback lock request.
        guard revoked || now - requested > 1.5 else { return }
        if NativePlatform.sessionLocked() == true {
            observedLock = true
            if failed { exit(0) }
            // Remain until the manual unlock, then allow a fresh input episode.
            return
        }
        if observedLock, !failed {
            // A false lock result is not proof of manual authentication. Do
            // not clear suppression here; parent confirms the locked->unlocked transition.
            return
        }
        if now >= nextLockAttempt {
            nextLockAttempt = now + 1
            do { _ = try TaskPrivacySystemLock.request() }
            catch { report("ERROR system lock request failed") }
        }
    }

    private func report(_ value: String) {
        try? FileHandle.standardOutput.write(contentsOf: Data((value + "\n").utf8))
    }
}
