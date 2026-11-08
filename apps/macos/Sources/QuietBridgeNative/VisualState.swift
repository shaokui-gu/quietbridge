import Foundation
@preconcurrency import ScreenCaptureKit
import CoreMedia
import CoreVideo
import CoreImage

struct VisualFrame: Sendable {
    let image: CapturedPNG
    let capturedAt: Double
    let windowID: UInt32
    let owner: ProcessIdentity
    static var now: Double { CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock())) }
}

typealias VisualStreamStart = @Sendable (CaptureTarget, Double,
    @escaping @Sendable (Result<VisualFrame, NativeFailure>) -> Void) -> @Sendable () -> Void

// 每次观察独占一个流；终态只接受一次，迟到的回调不能恢复已取消的观察。
final class VisualFrameWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<VisualFrame, any Error>?
    private var result: Result<VisualFrame, NativeFailure>?
    private var stop: (@Sendable () -> Void)?
    private var timer: DispatchWorkItem?
    private let target: CaptureTarget
    let requestedAt: Double
    private let clock: @Sendable () -> Double

    init(target: CaptureTarget, clock: @escaping @Sendable () -> Double = { VisualFrame.now }) {
        self.target = target; self.clock = clock; requestedAt = clock()
    }
    func receive(_ value: Result<VisualFrame, NativeFailure>) {
        if case .success(let frame) = value {
            guard frame.windowID == target.window.windowID, frame.owner == target.process else {
                finish(.failure(NativeFailure(code: "WINDOW_CHANGED", message: "视频帧目标或进程代际不匹配"))); return
            }
            let age = clock() - frame.capturedAt
            guard frame.capturedAt >= requestedAt, age >= 0, age <= 2 else { return }
        }
        finish(value)
    }
    func finish(_ value: Result<VisualFrame, NativeFailure>) {
        let pending = lock.withLock { () -> (CheckedContinuation<VisualFrame, any Error>?, (@Sendable () -> Void)?)? in
            guard result == nil else { return nil }
            result = value
            timer?.cancel(); timer = nil
            let pending = (continuation, stop)
            continuation = nil; stop = nil
            return pending
        }
        guard let pending else { return }
        pending.1?()
        pending.0?.resume(with: value.mapError { $0 as any Error })
    }
    func wait(start: VisualStreamStart, timeout: Double = 4) async throws -> VisualFrame {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let existing = lock.withLock { () -> Result<VisualFrame, NativeFailure>? in
                    if let result { return result }
                    self.continuation = continuation
                    return nil
                }
                if let existing { continuation.resume(with: existing.mapError { $0 as any Error }); return }
                let timer = DispatchWorkItem { [weak self] in
                    self?.finish(.failure(NativeFailure(code: "NATIVE_TIMEOUT", message: "等待新complete视频帧超时")))
                }
                lock.withLock { self.timer = timer }
                DispatchQueue.global().asyncAfter(deadline: .now() + min(4, max(0, timeout)), execute: timer)
                let stop = start(target, requestedAt) { [weak self] in self?.receive($0) }
                let finished = lock.withLock { () -> Bool in
                    guard result == nil else { return true }
                    self.stop = stop
                    return false
                }
                if finished { stop() }
            }
        } onCancel: {
            self.finish(.failure(NativeFailure(code: "NATIVE_CANCELLED", message: "视觉观察已取消；丢弃迟到帧")))
        }
    }
}

final class VisualStateCapture: Sendable {
    private let start: VisualStreamStart
    init(start: @escaping VisualStreamStart = VisualStreamSession.start) { self.start = start }
    func capture(_ target: CaptureTarget) async throws -> VisualFrame {
        try await VisualFrameWaiter(target: target).wait(start: start)
    }
}

private final class VisualStreamSession: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var stopped = false
    private var stream: SCStream?
    private let target: CaptureTarget
    private let requestedAt: Double
    private let receive: @Sendable (Result<VisualFrame, NativeFailure>) -> Void
    private let queue = DispatchQueue(label: "quietbridge.visual.video")
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var encoded = false
    private var expectedWidth = 0
    private var expectedHeight = 0

    init(target: CaptureTarget, requestedAt: Double, receive: @escaping @Sendable (Result<VisualFrame, NativeFailure>) -> Void) {
        self.target = target; self.requestedAt = requestedAt; self.receive = receive
    }
    static func start(_ target: CaptureTarget, _ requestedAt: Double,
                      _ receive: @escaping @Sendable (Result<VisualFrame, NativeFailure>) -> Void) -> @Sendable () -> Void {
        let session = VisualStreamSession(target: target, requestedAt: requestedAt, receive: receive)
        session.begin()
        return { session.stop() }
    }
    private func begin() {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { [self] content, error in
            guard !lock.withLock({ stopped }) else { return }
            do {
                try NativePlatform.verify(target.process)
                try NativePlatform.requireUnlocked(NativePlatform.sessionLocked())
                try NativePlatform.requireScreenRecording()
                guard let content else { throw NativeFailure(code: "NATIVE_ERROR", message: error?.localizedDescription ?? "视频共享列表不可用") }
                guard let window = content.windows.first(where: { $0.windowID == target.window.windowID && $0.owningApplication?.processID == target.process.pid }) else {
                    throw NativeFailure(code: "WINDOW_UNAVAILABLE", message: "目标视频窗口或owner已不可用")
                }
                let expected = target.window.bounds
                let filter = SCContentFilter(desktopIndependentWindow: window)
                guard window.frame == CGRect(x: expected.x, y: expected.y, width: expected.width, height: expected.height),
                      filter.contentRect.width == expected.width, filter.contentRect.height == expected.height else {
                    throw NativeFailure(code: "WINDOW_CHANGED", message: "视频表面与窗口point几何不一致")
                }
                let config = SCStreamConfiguration()
                config.width = max(1, Int((filter.contentRect.width * Double(filter.pointPixelScale)).rounded(.up)))
                config.height = max(1, Int((filter.contentRect.height * Double(filter.pointPixelScale)).rounded(.up)))
                expectedWidth = config.width
                expectedHeight = config.height
                config.pixelFormat = kCVPixelFormatType_32BGRA
                config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
                config.queueDepth = 3
                config.showsCursor = false
                config.ignoreShadowsSingleWindow = true
                config.ignoreGlobalClipSingleWindow = true
                let next = SCStream(filter: filter, configuration: config, delegate: self)
                try next.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
                let accepted = lock.withLock { () -> Bool in
                    guard !stopped else { return false }
                    stream = next
                    return true
                }
                guard accepted else { try? next.removeStreamOutput(self, type: .screen); return }
                next.startCapture { [self] error in
                    if lock.withLock({ stopped }) {
                        next.stopCapture { _ in try? next.removeStreamOutput(self, type: .screen) }
                    } else if let error { receive(.failure(NativeFailure(code: "NATIVE_ERROR", message: error.localizedDescription))) }
                }
            } catch let failure as NativeFailure { receive(.failure(failure)) }
            catch { receive(.failure(NativeFailure(code: "NATIVE_ERROR", message: error.localizedDescription))) }
        }
    }
    private func stop() {
        let previous = lock.withLock { () -> SCStream? in
            stopped = true
            let previous = stream; stream = nil
            return previous
        }
        if let previous {
            previous.stopCapture { [self] _ in try? previous.removeStreamOutput(self, type: .screen) }
        }
    }
    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        receive(.failure(NativeFailure(code: "NATIVE_ERROR", message: error.localizedDescription)))
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, buffer.isValid, !encoded, !lock.withLock({ stopped }) else { return }
        let info = (CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]])?.first
        guard (info?[.status] as? NSNumber)?.intValue == SCFrameStatus.complete.rawValue,
              let ticks = (info?[.displayTime] as? NSNumber)?.uint64Value,
              let pixels = CMSampleBufferGetImageBuffer(buffer) else { return }
        let capturedAt = CMTimeGetSeconds(CMClockMakeHostTimeFromSystemUnits(ticks))
        guard capturedAt >= requestedAt, VisualFrame.now - capturedAt <= 2 else { return }
        do {
            guard CVPixelBufferGetWidth(pixels) == expectedWidth, CVPixelBufferGetHeight(pixels) == expectedHeight else {
                throw NativeFailure(code: "WINDOW_CHANGED", message: "视频像素尺寸与选定窗口表面不一致")
            }
            let image = CIImage(cvPixelBuffer: pixels)
            guard let cg = context.createCGImage(image, from: image.extent) else {
                throw NativeFailure(code: "NATIVE_ERROR", message: "无法读取视频pixel buffer")
            }
            let png = try WindowCapture.encode(cg)
            guard !lock.withLock({ stopped }) else { return }
            encoded = true
            receive(.success(VisualFrame(image: png, capturedAt: capturedAt, windowID: target.window.windowID, owner: target.process)))
        } catch let failure as NativeFailure { receive(.failure(failure)) }
        catch { receive(.failure(NativeFailure(code: "NATIVE_ERROR", message: error.localizedDescription))) }
    }
}
