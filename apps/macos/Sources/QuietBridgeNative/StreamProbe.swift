// Bounded diagnostic: consumes real video buffers, never captures still images or sends input.
import AppKit
import ScreenCaptureKit
import CoreMedia
import CoreVideo

final class StreamProbe: NSObject, SCStreamOutput, @unchecked Sendable {
    private let lock = NSLock()
    private var statuses: [String: Int] = [:]
    private var hashes = Set<UInt64>()
    private var changed = 0
    private var previous: UInt64?

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]]
        let status = (attachments?.first?[.status] as? NSNumber)?.intValue ?? -1
        lock.withLock { statuses[String(status), default: 0] += 1 }
        guard status == SCFrameStatus.complete.rawValue,
              let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixels) else { return }
        let width = CVPixelBufferGetWidth(pixels), height = CVPixelBufferGetHeight(pixels)
        let stride = CVPixelBufferGetBytesPerRow(pixels)
        var hash: UInt64 = 14695981039346656037
        for y in Swift.stride(from: 0, to: height, by: max(1, height / 90)) {
            for x in Swift.stride(from: 0, to: width, by: max(1, width / 160)) {
                let value = base.load(fromByteOffset: y * stride + x * 4, as: UInt32.self)
                hash = (hash ^ UInt64(value)) &* 1099511628211
            }
        }
        lock.withLock {
            if let previous, previous != hash { changed += 1 }
            previous = hash
            hashes.insert(hash)
        }
    }

    @MainActor static func run(arguments: [String]) async -> Int32 {
        do {
            if arguments.count == 3, ["synthetic-three", "synthetic-deck"].contains(arguments[1]), let seconds = Int(arguments[2]), (6...60).contains(seconds) {
                return try await syntheticThree(seconds: seconds, testDeck: arguments[1] == "synthetic-deck")
            }
            guard (2...4).contains(arguments.count) else {
                throw NativeFailure(code: "INVALID_PARAMS", message: "Use --probe-stream list, or --probe-stream windowID seconds (1...60)")
            }
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            if arguments[1] == "list" {
                let spaces = WindowSpaces().memberships(windowIDs: content.windows.map(\.windowID))
                output(content.windows.filter { $0.windowLayer == 0 }.map { window -> [String: Any] in
                    ["windowID": window.windowID, "pid": window.owningApplication?.processID ?? 0,
                     "app": window.owningApplication?.applicationName ?? "", "title": window.title ?? "",
                     "onScreen": window.isOnScreen, "onCurrentSpace": spaces[window.windowID]?.onCurrentSpace as Any? ?? NSNull(),
                     "width": window.frame.width, "height": window.frame.height]
                })
                return 0
            }
            guard arguments.count >= 3, let id = UInt32(arguments[1]), let seconds = Int(arguments[2]), (1...60).contains(seconds),
                  let window = content.windows.first(where: { $0.windowID == id }) else {
                throw NativeFailure(code: "INVALID_PARAMS", message: "Window missing, or duration outside 1...60 seconds")
            }
            let configuration = SCStreamConfiguration()
            configuration.width = max(1, Int(window.frame.width))
            configuration.height = max(1, Int(window.frame.height))
            configuration.pixelFormat = kCVPixelFormatType_32BGRA
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 15)
            configuration.queueDepth = 3
            configuration.showsCursor = false
            let probe = StreamProbe()
            let stream = SCStream(filter: SCContentFilter(desktopIndependentWindow: window), configuration: configuration, delegate: nil)
            try stream.addStreamOutput(probe, type: .screen, sampleHandlerQueue: DispatchQueue(label: "quietbridge.stream.probe"))
            let showPreview = arguments.count == 4 && arguments[3] == "preview"
            if showPreview { SpacePreview.shared.track(pid: window.owningApplication?.processID ?? 0, windowID: id) }
            let beforePID = NativePlatform.frontPID()
            let beforeMouse = CGEvent(source: nil)?.location ?? .zero
            let start = ProcessInfo.processInfo.systemUptime
            try await stream.startCapture()
            Self.output(["event": "streamStarted", "windowID": id])
            var frontChanges = 0
            var lastPID = beforePID
            for _ in 0..<(seconds * 5) {
                try await Task.sleep(for: .milliseconds(200))
                let pid = NativePlatform.frontPID()
                if pid != lastPID { frontChanges += 1; lastPID = pid }
            }
            try await stream.stopCapture()
            let afterMouse = CGEvent(source: nil)?.location ?? .zero
            let stats: [String: Any] = probe.lock.withLock {
                ["statuses": probe.statuses, "uniqueSampledFrames": probe.hashes.count, "contentChanges": probe.changed]
            }
            var result = stats
            result["windowID"] = id
            result["onScreenAtStart"] = window.isOnScreen
            result["onCurrentSpaceAtStart"] = WindowSpaces().memberships(windowIDs: [id])[id]?.onCurrentSpace as Any? ?? NSNull()
            result["elapsedSeconds"] = ProcessInfo.processInfo.systemUptime - start
            result["frontPIDBefore"] = beforePID as Any? ?? NSNull()
            result["frontPIDAfter"] = NativePlatform.frontPID() as Any? ?? NSNull()
            result["frontChanges"] = frontChanges
            result["mouseBefore"] = [beforeMouse.x, beforeMouse.y]
            result["mouseAfter"] = [afterMouse.x, afterMouse.y]
            if showPreview {
                result["preview"] = SpacePreview.shared.diagnostics()
                SpacePreview.shared.stop()
            }
            output(result)
            return 0
        } catch {
            output(["error": String(describing: error)])
            return 1
        }
    }

    /// Uses only helper-owned animated test windows. Never records user content,
    /// requests consent, changes permissions, moves the mouse or activates an app.
    @MainActor private static func syntheticThree(seconds: Int, testDeck: Bool = false) async throws -> Int32 {
        guard CGPreflightScreenCaptureAccess() else {
            throw NativeFailure(code: "SCREEN_RECORDING_REQUIRED", message: "Three-stream probe requires existing screen capture permission")
        }
        let beforePID = NativePlatform.frontPID()
        let beforeMouse = CGEvent(source: nil)?.location
        // Register the synthetic app for the real getApp/windowIdentity path.
        // Its windows remain nonactivating, and foreground checks cover this.
        NSApp.setActivationPolicy(.regular)
        var windows: [NSPanel] = []
        var labels: [NSTextField] = []
        var streams: [SCStream] = []
        var probes: [StreamProbe] = []
        defer { PreviewDeck.shared.stopAll(); for window in windows { window.orderOut(nil) } }
        for i in 0..<3 {
            let panel = NSPanel(contentRect: CGRect(x: 80 + i * 180, y: 80, width: 160, height: 100),
                styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.title = "QuietBridge synthetic stream \(i + 1)"
            panel.backgroundColor = [.systemBlue, .systemTeal, .systemOrange][i]
            let label = NSTextField(labelWithString: "Stream \(i + 1) · 0")
            label.textColor = .white
            label.font = .monospacedSystemFont(ofSize: 16, weight: .medium)
            label.frame = CGRect(x: 12, y: 35, width: 140, height: 30)
            panel.contentView?.addSubview(label)
            panel.orderFrontRegardless()
            windows.append(panel); labels.append(label)
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        output(["event": "syntheticWindows", "pid": getpid(), "windowIds": windows.map(\.windowNumber)])
        do {
            for (i, panel) in windows.enumerated() {
                guard let window = content.windows.first(where: { $0.windowID == UInt32(panel.windowNumber) }) else {
                    throw NativeFailure(code: "PROBE_WINDOW_MISSING", message: "Synthetic window not enumerated")
                }
                let config = SCStreamConfiguration()
                config.width = 160; config.height = 100
                config.pixelFormat = kCVPixelFormatType_32BGRA
                config.minimumFrameInterval = CMTime(value: 1, timescale: i == 0 ? 15 : 5)
                config.showsCursor = false; config.queueDepth = 3
                let probe = StreamProbe()
                let stream = SCStream(filter: SCContentFilter(desktopIndependentWindow: window), configuration: config, delegate: nil)
                try stream.addStreamOutput(probe, type: .screen, sampleHandlerQueue: DispatchQueue(label: "quietbridge.probe.\(i)"))
                probes.append(probe); streams.append(stream)
                try await stream.startCapture()
            }
            var deckVerified = !testDeck
            if testDeck {
                let ids = (0..<3).map { _ in UUID() }
                let cards = ids.enumerated().map { i, id -> SpacePreview in
                    let card = PreviewDeck.shared.preview(for: id)
                    card.track(pid: getpid(), windowID: UInt32(windows[i].windowNumber), source: .window)
                    _ = card.hold(seconds: 60)
                    return card
                }
                for card in cards { try await card.waitUntilReady {} }
                let initial = cards.map { $0.diagnostics() }
                for id in ids + ids.reversed() {
                    PreviewDeck.shared.select(id)
                    try await Task.sleep(for: .milliseconds(200))
                }
                let states = cards.map { $0.diagnostics() }
                deckVerified = states.enumerated().allSatisfy { i, state in
                    state["streamGeneration"] as? String == initial[i]["streamGeneration"] as? String &&
                    (state["videoFrames"] as? Int ?? 0) > (initial[i]["videoFrames"] as? Int ?? 0)
                } && states.filter { $0["preferredFPS"] as? Int == 15 }.count == 1 &&
                     states.filter { $0["preferredFPS"] as? Int == 5 }.count == 2
                output(["previewDeckVerified": deckVerified, "streamsUnchangedOnSwitch": states.enumerated().allSatisfy {
                    $0.element["streamGeneration"] as? String == initial[$0.offset]["streamGeneration"] as? String },
                    "preferredFPS": states.map { $0["preferredFPS"] ?? 0 },
                    "panelFrames": states.map { $0["panelFrame"] ?? "" }])
                PreviewDeck.shared.stopAll()
            }
            var frontChanged = NativePlatform.frontPID() != beforePID
            var midpoint: [Int] = []
            for tick in 0..<(seconds * 5) {
                for (i, label) in labels.enumerated() { label.stringValue = "Stream \(i + 1) · \(tick)" }
                if tick == seconds * 5 / 2 {
                    midpoint = probes.map { p in p.lock.withLock { p.changed } }
                    try await streams[0].stopCapture()
                    windows[0].orderOut(nil)
                }
                try await Task.sleep(for: .milliseconds(200))
                if NativePlatform.frontPID() != beforePID { frontChanged = true }
            }
            for stream in streams.dropFirst() { try await stream.stopCapture() }
            let counts = probes.map { p in p.lock.withLock { p.changed } }
            let pass = counts.allSatisfy { $0 >= 3 } && counts[1] > midpoint[1] && counts[2] > midpoint[2] && !frontChanged && deckVerified
            output(["syntheticThree": pass, "seconds": seconds, "contentChanges": counts,
                "midpointChanges": midpoint, "stoppedStream": 0, "remainingStreamsAdvanced": counts[1] > midpoint[1] && counts[2] > midpoint[2],
                "frontPIDBefore": beforePID as Any? ?? NSNull(), "frontPIDAfter": NativePlatform.frontPID() as Any? ?? NSNull(),
                "frontChanged": frontChanged, "mouseUnchanged": beforeMouse == CGEvent(source: nil)?.location])
            return pass ? 0 : 1
        } catch {
            for stream in streams { try? await stream.stopCapture() }
            throw error
        }
    }

    private static func output(_ value: Any) {
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
           !data.isEmpty { try? FileHandle.standardOutput.write(contentsOf: data + Data([10])) }
    }
}
