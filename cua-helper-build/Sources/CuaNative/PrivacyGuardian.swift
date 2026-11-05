// A separate copy of this executable owns an additional desktop cover and an
// input listener. Its stdin/stdout pipes are inherited only by the task helper.
import AppKit
import CoreGraphics
import Darwin
import Foundation

/// Must match `TaskPrivacySyntheticEvents.marker`. Kept as a file-scope constant
/// because a C event-tap callback cannot capture context, and referencing the
/// other type's static from inside the callback crashes the Swift optimiser.
private let guardianSyntheticMarker: Int64 = 0x57424355414c4f43

/// The lock gesture belongs to the user until the replacement cover is ready.
/// In particular, releasing Ctrl/Command after locking is not a new takeover.
struct PrivacyGuardianInputPolicy {
    private(set) var armed = false
    private var modifiers: CGEventFlags = []
    private static let modifierMask: CGEventFlags = [
        .maskAlphaShift, .maskShift, .maskControl, .maskAlternate,
        .maskCommand, .maskHelp, .maskSecondaryFn,
    ]

    mutating func arm(flags: CGEventFlags) {
        modifiers = flags.intersection(Self.modifierMask)
        armed = true
    }

    mutating func accepts(flagsChanged: Bool, flags: CGEventFlags,
                          sourcePID: Int64, userData: Int64) -> Bool {
        guard sourcePID == 0, userData != guardianSyntheticMarker else { return false }
        guard flagsChanged else { return true }
        let current = flags.intersection(Self.modifierMask)
        let pressed = current.subtracting(modifiers)
        modifiers = current
        return armed && !pressed.isEmpty
    }
}

/// Once revoked, a queued heartbeat cannot restore the same guardian's lease.
struct PrivacyGuardianLeaseState {
    private(set) var takeoverPending = false
    private(set) var lastReportAt: TimeInterval = 0
    private var announcedReady = false

    func isReady(at now: TimeInterval) -> Bool {
        !takeoverPending && announcedReady && now - lastReportAt < 1
    }

    mutating func receivedHeartbeat(at now: TimeInterval) {
        guard !takeoverPending else { return }
        announcedReady = true
        lastReportAt = now
    }

    @discardableResult
    mutating func revoke() -> Bool {
        let first = !takeoverPending
        takeoverPending = true
        announcedReady = false
        return first
    }
}

/// A guardian cannot inspect authd's outstanding Allow. The parent tells it
/// when that decision has settled; without that proof an old lock is not enough.
struct PrivacyGuardianHandoffState {
    private(set) var revocationAcknowledged = false
    private var automaticUnlockMayBePending = true

    mutating func automaticUnlockSettled() { automaticUnlockMayBePending = false }

    mutating func acknowledgeRevocation(automaticUnlockPending: Bool) {
        revocationAcknowledged = true
        automaticUnlockMayBePending = automaticUnlockPending
    }

    mutating func observeSessionLocked(_ locked: Bool?) {
        if locked == false { automaticUnlockMayBePending = false }
    }

    func mayReleaseAfterParentLoss(sessionLocked: Bool?) -> Bool {
        sessionLocked == true && !automaticUnlockMayBePending
    }
}

@MainActor
final class PrivacyGuardianClient {
    private var process: Process?
    private var commands: FileHandle?
    private var reports: FileHandle?
    private var generation = UUID()
    private var buffered = Data()
    private var lease = PrivacyGuardianLeaseState()
    var ready: Bool { lease.isReady(at: ProcessInfo.processInfo.systemUptime) }
    private(set) var lastError: String?
    var onTakeover: (() -> Void)?

    nonisolated init() {}

    func start() {
        guard process == nil else { return }
        generation = UUID()
        lease = PrivacyGuardianLeaseState()
        buffered.removeAll()
        lastError = nil
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
                self.revoke(error: "隐私守护进程已退出")
            }
        }
        do {
            try child.run()
            input.fileHandleForReading.closeFile()
            output.fileHandleForWriting.closeFile()
            process = child
            commands = input.fileHandleForWriting
        } catch {
            reports?.readabilityHandler = nil
            reports = nil
            revoke(error: error.localizedDescription)
        }
    }

    func pulse() {
        guard process?.isRunning == true else {
            revoke(error: "隐私守护进程未运行")
            return
        }
        send("PING")
        LockedUseAuthorizationServer.shared.renewProtection(ready: ready, validUntil: lease.lastReportAt + 1)
    }

    func armTakeover() {
        guard !lease.takeoverPending else { return }
        send("ARM_TAKEOVER")
    }

    /// Called only after the parent has observed the final system lock and
    /// proved that no previously consumed Allow can unlock it again.
    func completeHandoff() {
        close(command: "HANDOFF")
    }

    func stop() {
        let pending = LockedUseAuthorizationServer.shared.automaticUnlockPending
        close(command: pending ? "STOP_PENDING" : "STOP")
    }

    private func close(command: String) {
        generation = UUID()
        lease.revoke()
        LockedUseAuthorizationServer.shared.renewProtection(ready: false)
        send(command)
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
            revoke(error: error.localizedDescription)
        }
    }

    private func revoke(error: String? = nil) {
        let first = lease.revoke()
        if let error { lastError = error }
        LockedUseAuthorizationServer.shared.renewProtection(ready: false)
        LockedUseAuthorizationServer.shared.suppressForManualAuthentication()
        if first { onTakeover?() }
    }

    private func receive(_ data: Data) {
        guard !data.isEmpty, buffered.count + data.count <= 4096 else {
            revoke(error: "隐私守护连接中断")
            return
        }
        buffered.append(data)
        while let newline = buffered.firstIndex(of: 10) {
            let line = String(decoding: buffered[..<newline], as: UTF8.self)
            buffered.removeSubrange(...newline)
            switch line {
            case "READY", "ALIVE":
                guard !lease.takeoverPending else { continue }
                lease.receivedHeartbeat(at: ProcessInfo.processInfo.systemUptime)
                lastError = nil
            case "TAKEOVER":
                // Revoke synchronously before acknowledging that the guardian
                // may ask macOS to lock. The helper must not auto-unlock it.
                revoke()
                send(LockedUseAuthorizationServer.shared.automaticUnlockPending ? "REVOKED_PENDING" : "REVOKED")
            default:
                revoke(error: line)
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
    private var inputPolicy = PrivacyGuardianInputPolicy()
    private var handoff = PrivacyGuardianHandoffState()
    private var lockedSince: TimeInterval?
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
            panel.contentView = PrivacyShieldView(screenIndex: 0) { [weak self] in self?.viewInput() }
            if inputPolicy.armed { (panel.contentView as? PrivacyShieldView)?.armTakeover() }
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
                    guardian.takeover("tap-disabled type=\(type.rawValue)")
                } else if guardian.inputPolicy.accepts(
                    flagsChanged: type == .flagsChanged, flags: event.flags,
                    sourcePID: event.getIntegerValueField(.eventSourceUnixProcessID),
                    userData: event.getIntegerValueField(.eventSourceUserData)) {
                    guardian.takeover("hid-input type=\(type.rawValue)")
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

    private func viewInput() {
        guard let event = NSApp.currentEvent, let cgEvent = event.cgEvent,
              inputPolicy.accepts(
                flagsChanged: event.type == .flagsChanged, flags: cgEvent.flags,
                sourcePID: cgEvent.getIntegerValueField(.eventSourceUnixProcessID),
                userData: cgEvent.getIntegerValueField(.eventSourceUserData)) else { return }
        takeover("cover-input type=\(event.type.rawValue)")
    }

    private func receive(_ data: Data) {
        guard !data.isEmpty, inputBuffer.count + data.count <= 4096 else {
            failed = true
            takeover("command-stream-error")
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
            case "ARM_TAKEOVER":
                guard takeoverRequestedAt == nil else { continue }
                inputPolicy.arm(flags: CGEventSource.flagsState(.combinedSessionState))
                panels.forEach { ($0.contentView as? PrivacyShieldView)?.armTakeover() }
                handoff.automaticUnlockSettled()
            case "REVOKED", "REVOKED_PENDING":
                handoff.acknowledgeRevocation(automaticUnlockPending: line == "REVOKED_PENDING")
            case "HANDOFF":
                // The parent has revoked grants and checked for an outstanding
                // Allow. Recheck the system before removing this final cover.
                handoff.acknowledgeRevocation(automaticUnlockPending: false)
                if NativePlatform.sessionLocked() == true { finish() }
                failed = true
                takeover("handoff-lock-not-confirmed")
            case "STOP", "STOP_PENDING":
                let pending = line == "STOP_PENDING"
                if takeoverRequestedAt == nil && !pending { finish() }
                // A cancelled task cannot discard a requested system lock or
                // a cover that may still be needed for an in-flight Allow.
                handoff.acknowledgeRevocation(automaticUnlockPending: pending)
                failed = true
                takeover("parent-stopped")
            default:
                failed = true
                takeover("unknown-command \(line.prefix(24))")
            }
        }
    }

    private func takeover(_ reason: String = "unspecified") {
        guard takeoverRequestedAt == nil else { return }
        takeoverRequestedAt = ProcessInfo.processInfo.systemUptime
        Self.trace("TAKEOVER reason=\(reason) locked=\(String(describing: NativePlatform.sessionLocked()))")
        report("TAKEOVER")
    }

    /// Forensic channel: stderr is inherited by the helper, so the driver sees it.
    nonisolated static func trace(_ message: String) {
        Trace.log("guardian \(message)")
        FileHandle.standardError.write(Data("[guardian] \(message)\n".utf8))
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastHeartbeat > 1.5 { failed = true; takeover("heartbeat-timeout") }
        guard let requested = takeoverRequestedAt else { return }
        // ACK revokes immediately. On helper failure, its protection lease
        // expires after one second, before this fallback lock request.
        guard handoff.revocationAcknowledged || now - requested > 1.5 else { return }
        let locked = NativePlatform.sessionLocked()
        handoff.observeSessionLocked(locked)
        if locked == true {
            if lockedSince == nil { lockedSince = now }
            if failed, handoff.mayReleaseAfterParentLoss(sessionLocked: locked),
               let lockedSince, now - lockedSince >= 0.75 { finish() }
            // With a live parent, HANDOFF is the only completion signal. With
            // no parent and unknown pending Allow, retain cover until that
            // unlock is observed and a subsequent lock is stable.
            return
        }
        lockedSince = nil
        if now >= nextLockAttempt {
            nextLockAttempt = now + 1
            Self.trace("requesting system lock (guardian fallback)")
            do { _ = try TaskPrivacySystemLock.request() }
            catch { report("ERROR system lock request failed") }
        }
    }

    private func report(_ value: String) {
        try? FileHandle.standardOutput.write(contentsOf: Data((value + "\n").utf8))
    }

    private func finish() -> Never {
        panels.forEach { $0.orderOut(nil) }
        exit(0)
    }
}
