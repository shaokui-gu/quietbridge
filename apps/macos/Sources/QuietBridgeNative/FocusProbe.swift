// Bounded, read-only observation. Never captures key events, text, titles or clipboard contents.
import AppKit
import ApplicationServices
import Foundation

struct FocusProbeOptions: Equatable, Sendable {
    let seconds: Int
    let targetPID: Int32
    let targetWindowID: UInt32

    init(arguments: [String]) throws {
        guard arguments.count == 4, arguments[0] == "--probe-focus",
              let seconds = Int(arguments[1]), (1...120).contains(seconds),
              let pid = Int32(arguments[2]), pid > 0,
              let window = UInt32(arguments[3]), window > 0 else {
            throw RequestParameters.invalid("Use --probe-focus seconds(1...120) targetPID targetWindowID")
        }
        self.seconds = seconds
        targetPID = pid
        targetWindowID = window
    }
}

struct FocusProbeJournal {
    static let limit = 2000
    private(set) var events: [JSONValue] = []
    private(set) var dropped = 0

    mutating func append(kind: String, uptime: Double, pid: Int32? = nil, windowID: UInt32? = nil,
                         elementID: UInt? = nil) -> JSONValue? {
        guard events.count < Self.limit else { dropped += 1; return nil }
        // Deliberately no arbitrary payload: a caller cannot accidentally log input values.
        var fields: [String: JSONValue] = ["event": .string(kind), "uptime": .number(uptime)]
        if let pid { fields["pid"] = .number(Double(pid)) }
        if let windowID { fields["windowId"] = .number(Double(windowID)) }
        if let elementID { fields["elementId"] = .string(String(elementID)) }
        let value = JSONValue.object(fields)
        events.append(value)
        return value
    }
}

private func focusProbeAXCallback(_ observer: AXObserver, _ element: AXUIElement,
                                  _ notification: CFString, _ context: UnsafeMutableRawPointer?) {
    guard let context else { return }
    let probe = Unmanaged<FocusProbe>.fromOpaque(context).takeUnretainedValue()
    var owner: pid_t = 0
    let error = AXUIElementGetPid(element, &owner)
    let pid: Int32? = error == .success ? owner : nil
    let windowID = try? AXAccess(timeout: 0.1, cancellation: CancellationLatch()).windowID(element)
    let elementID = CFHash(element)
    let name = notification as String
    // Extract only Sendable metadata before entering the main actor. The AX source
    // itself is installed exclusively on the main run loop below.
    MainActor.assumeIsolated {
        probe.receivedAX(pid: pid, windowID: windowID, elementID: elementID, notification: name)
    }
}

@MainActor final class FocusProbe {
    private let options: FocusProbeOptions
    private let spaces = WindowSpaces()
    private let access = AXAccess(timeout: 0.1, cancellation: CancellationLatch())
    private var workspaceTokens: [NSObjectProtocol] = []
    private var axObservers: [Int32: AXObserver] = [:]
    private var attemptedPIDs = Set<Int32>()
    private var coverage: [String: JSONValue] = [:]
    private var journal = FocusProbeJournal()
    private var previousSample: JSONValue?
    private var firstSample: JSONValue?
    private var lastSample: JSONValue?
    private var maxSampleGap = 0.0
    private var previousTick: Double?
    private var sampleCount = 0

    init(options: FocusProbeOptions) { self.options = options }

    private func emit(_ value: JSONValue) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(value) else { return }
        try? FileHandle.standardOutput.write(contentsOf: data + Data([10]))
    }

    private func event(_ kind: String, pid: Int32? = nil, windowID: UInt32? = nil, elementID: UInt? = nil) {
        if let value = journal.append(kind: kind, uptime: ProcessInfo.processInfo.systemUptime,
                                      pid: pid, windowID: windowID, elementID: elementID) { emit(value) }
    }

    private func attachAX(_ pid: Int32) {
        guard !attemptedPIDs.contains(pid) else { return }
        attemptedPIDs.insert(pid)
        let app = AXUIElementCreateApplication(pid)
        _ = AXUIElementSetMessagingTimeout(app, 0.1)
        var observer: AXObserver?
        let error = AXObserverCreate(pid, focusProbeAXCallback, &observer)
        guard error == .success, let observer else {
            coverage[String(pid)] = .object(["observerError": .number(Double(error.rawValue))])
            return
        }
        var subscribed: [String: JSONValue] = [:]
        for name in ["AXFocusedWindowChanged", "AXFocusedUIElementChanged", "AXMainWindowChanged"] {
            let result = AXObserverAddNotification(observer, app, name as CFString,
                                                  Unmanaged.passUnretained(self).toOpaque())
            subscribed[name] = .number(Double(result.rawValue))
        }
        axObservers[pid] = observer
        coverage[String(pid)] = .object(subscribed)
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
    }

    fileprivate func receivedAX(pid: Int32?, windowID: UInt32?, elementID: UInt, notification: String) {
        event(notification, pid: pid, windowID: windowID, elementID: elementID)
    }

    private func sample() {
        let now = ProcessInfo.processInfo.systemUptime
        if let previousTick { maxSampleGap = max(maxSampleGap, now - previousTick) }
        previousTick = now
        sampleCount += 1
        let frontPID = spaces.frontPID()
        if let frontPID { attachAX(frontPID) }
        let windowID = frontPID.flatMap { try? access.focusedWindowID($0) }
        let focusedElement = frontPID.flatMap { try? access.element(AXUIElementCreateApplication($0), "AXFocusedUIElement") }
        let focusedElementID = focusedElement.map { CFHash($0) }
        let ids = [windowID, options.targetWindowID].compactMap { $0 }
        let memberships = spaces.memberships(windowIDs: ids)
        let mouse = NSEvent.mouseLocation
        // changeCount only; never request clipboard types, strings, data or an event tap.
        let fields: [String: JSONValue] = [
            "frontPid": frontPID.map { .number(Double($0)) } ?? .null,
            "focusedWindowId": windowID.map { .number(Double($0)) } ?? .null,
            "focusedElementId": focusedElementID.map { .string(String($0)) } ?? .null,
            "frontSpaceId": windowID.flatMap { memberships[$0]?.currentSpaceID }.map { .string(String($0)) } ?? .null,
            "targetSpaceId": memberships[options.targetWindowID]?.currentSpaceID.map { .string(String($0)) } ?? .null,
            "mouseX": .number(mouse.x), "mouseY": .number(mouse.y),
            "pasteboardChangeCount": .number(Double(NSPasteboard.general.changeCount))
        ]
        let value = JSONValue.object(fields)
        if firstSample == nil { firstSample = value }
        lastSample = value
        if value != previousSample {
            previousSample = value
            emit(.object(["event": .string("sample"), "uptime": .number(now), "state": value]))
        }
    }

    private func subscribe() {
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didActivateApplicationNotification, NSWorkspace.didDeactivateApplicationNotification,
                     NSWorkspace.activeSpaceDidChangeNotification] {
            workspaceTokens.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                let pid = (note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?.processIdentifier
                let notificationName = name.rawValue
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.event(notificationName, pid: pid)
                    if let pid { self.attachAX(pid) }
                    self.sample()
                }
            })
        }
        attachAX(options.targetPID)
    }

    private func unsubscribe() {
        for token in workspaceTokens { NSWorkspace.shared.notificationCenter.removeObserver(token) }
        workspaceTokens.removeAll()
        for observer in axObservers.values {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
        }
        axObservers.removeAll()
    }

    func run() async -> Int32 {
        do {
            try NativePlatform.requireAccessibility()
            try NativePlatform.requireUnlocked(NativePlatform.sessionLocked())
            _ = try NativePlatform.ownedWindow(options.targetPID, options.targetWindowID)
            subscribe()
            defer { unsubscribe() }
            sample()
            let started = ProcessInfo.processInfo.systemUptime
            emit(.object(["event": .string("ready"), "uptime": .number(started),
                          "targetPid": .number(Double(options.targetPID)),
                          "targetWindowId": .number(Double(options.targetWindowID)),
                          "durationSeconds": .number(Double(options.seconds)),
                          "axCoverage": .object(coverage), "baseline": firstSample ?? .null,
                          "privacy": .string("metadata_only_no_keys_no_text_no_clipboard_contents")]))
            while ProcessInfo.processInfo.systemUptime - started < Double(options.seconds) {
                try await Task.sleep(for: .milliseconds(100))
                sample()
            }
            emit(.object(["event": .string("summary"), "uptime": .number(ProcessInfo.processInfo.systemUptime),
                          "sampleCount": .number(Double(sampleCount)), "maxSampleGapMs": .number(maxSampleGap * 1000),
                          "droppedEvents": .number(Double(journal.dropped)), "axCoverage": .object(coverage),
                          "initial": firstSample ?? .null, "final": lastSample ?? .null,
                          "verdict": .string("observation_only_not_a_pass"),
                          "limitations": .string("AX notification support is app-dependent; mouse/clipboard changes alone do not identify who caused them")]))
            return 0
        } catch {
            let failure = error as? NativeFailure
            emit(.object(["event": .string("error"), "code": .string(failure?.code ?? "PROBE_ERROR"),
                          "message": .string(failure?.message ?? error.localizedDescription)]))
            return 1
        }
    }
}
