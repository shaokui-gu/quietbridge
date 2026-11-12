// Diagnostics only: never save pixels or expose their fingerprints. Submission
// means enqueue was called, not that the screen presented the sample.
import Foundation
import CoreMedia
import CoreVideo
import ScreenCaptureKit

final class PreviewFrameProgress: @unchecked Sendable {
    private let lock = NSLock()
    private var counts: [String: Int] = [:]
    private var lastCallback: TimeInterval?
    private var lastSubmission: TimeInterval?
    private var lastContentChange: TimeInterval?
    private var nextSample: TimeInterval = 0
    private var previousFingerprint: UInt64?
    private var previousBodyFingerprint: UInt64?
    private var lastBodyContentChange: TimeInterval?
    private var lastSourceSample: TimeInterval?
    private var sourceFrame: [String: Any]?

    func recordCallback(status: Int?, now: TimeInterval) {
        let name: String
        switch status.flatMap(SCFrameStatus.init(rawValue:)) {
        case .complete: name = "completeFrames"
        case .idle: name = "idleFrames"
        case .blank: name = "blankFrames"
        case .suspended: name = "suspendedFrames"
        case .started: name = "startedFrames"
        case .stopped: name = "stoppedFrames"
        default: name = "unknownFrames"
        }
        lock.withLock {
            counts["callbackFrames", default: 0] += 1
            counts[name, default: 0] += 1
            lastCallback = now
        }
    }

    func recordSubmission(accepted: Bool, now: TimeInterval) {
        lock.withLock {
            counts[accepted ? "submittedFrames" : "backpressureDroppedFrames", default: 0] += 1
            if accepted { lastSubmission = now }
        }
    }

    func sample(_ buffer: CVPixelBuffer, now: TimeInterval) {
        let shouldSample = lock.withLock {
            guard now >= nextSample else { return false }
            nextSample = now + 1
            return true
        }
        guard shouldSample else { return }
        guard let sample = PreviewPixelSample.inspect(buffer) else {
            recordFingerprint(nil, now: now)
            lock.withLock {
                counts["bodySampleFailures", default: 0] += 1
                sourceFrame = nil
            }
            return
        }
        recordFingerprint(sample.wholeFingerprint, now: now)
        lock.withLock {
            counts["bodySamples", default: 0] += 1
            if sample.nearUniform { counts["bodyNearUniformSamples", default: 0] += 1 }
            if let previousBodyFingerprint, previousBodyFingerprint != sample.bodyFingerprint {
                counts["bodyContentChanges", default: 0] += 1
                lastBodyContentChange = now
            }
            previousBodyFingerprint = sample.bodyFingerprint
            lastSourceSample = now
            sourceFrame = sample.metadata
        }
    }

    // Internal for synthetic tests; the fingerprint never leaves this instance.
    func recordFingerprint(_ fingerprint: UInt64?, now: TimeInterval) {
        lock.withLock {
            guard let fingerprint else {
                counts["sampleFailures", default: 0] += 1
                return
            }
            counts["contentSamples", default: 0] += 1
            if let previousFingerprint, previousFingerprint != fingerprint {
                counts["sampledContentChanges", default: 0] += 1
                lastContentChange = now
            }
            previousFingerprint = fingerprint
        }
    }

    func snapshot(now: TimeInterval) -> [String: Any] {
        lock.withLock {
            var result: [String: Any] = [:]
            for key in ["callbackFrames", "completeFrames", "idleFrames", "blankFrames", "suspendedFrames",
                        "startedFrames", "stoppedFrames", "unknownFrames", "submittedFrames",
                        "backpressureDroppedFrames", "contentSamples", "sampledContentChanges", "sampleFailures",
                        "bodySamples", "bodyContentChanges", "bodyNearUniformSamples", "bodySampleFailures"] {
                result[key] = counts[key, default: 0]
            }
            for (key, time) in [("lastCallbackAgeMs", lastCallback), ("lastSubmissionAgeMs", lastSubmission),
                                ("lastSampledContentChangeAgeMs", lastContentChange),
                                ("lastBodyContentChangeAgeMs", lastBodyContentChange),
                                ("lastSourceSampleAgeMs", lastSourceSample)] {
                result[key] = time.map { max(0, (now - $0) * 1000) } as Any? ?? NSNull()
            }
            if let sourceFrame { result["sourceFrame"] = sourceFrame }
            return result
        }
    }

}
