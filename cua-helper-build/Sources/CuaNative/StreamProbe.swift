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
            try stream.addStreamOutput(probe, type: .screen, sampleHandlerQueue: DispatchQueue(label: "cua.stream.probe"))
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

    private static func output(_ value: Any) {
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
           !data.isEmpty { try? FileHandle.standardOutput.write(contentsOf: data + Data([10])) }
    }
}
