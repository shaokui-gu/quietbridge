import Foundation
import CoreVideo
import ScreenCaptureKit
import Testing
@testable import QuietBridgeNative

@Test func previewFrameProgressSeparatesIncomingFramesFromBackpressure() throws {
    let progress = PreviewFrameProgress()
    progress.recordCallback(status: SCFrameStatus.complete.rawValue, now: 10)
    progress.recordSubmission(accepted: true, now: 10)
    progress.recordCallback(status: SCFrameStatus.complete.rawValue, now: 11)
    progress.recordSubmission(accepted: false, now: 11)
    let result = progress.snapshot(now: 11)
    #expect(result["completeFrames"] as? Int == 2)
    #expect(result["submittedFrames"] as? Int == 1)
    #expect(result["backpressureDroppedFrames"] as? Int == 1)
    #expect(result["lastCallbackAgeMs"] as? Double == 0)
    #expect(result["lastSubmissionAgeMs"] as? Double == 1000)
    #expect(!result.keys.contains("displayedFrames"), "enqueue does not prove presentation")
}

@Test func previewFrameProgressDoesNotTreatStaticOrIdleAsProofOfFreezing() throws {
    let progress = PreviewFrameProgress()
    progress.recordCallback(status: SCFrameStatus.idle.rawValue, now: 10)
    progress.recordCallback(status: SCFrameStatus.suspended.rawValue, now: 11)
    progress.recordCallback(status: nil, now: 12)
    progress.recordFingerprint(123, now: 10)
    progress.recordFingerprint(123, now: 11)
    let before = progress.snapshot(now: 12)
    #expect(before["idleFrames"] as? Int == 1)
    #expect(before["suspendedFrames"] as? Int == 1)
    #expect(before["unknownFrames"] as? Int == 1)
    #expect(before["sampledContentChanges"] as? Int == 0)
    #expect(before["lastSampledContentChangeAgeMs"] is NSNull)
    progress.recordFingerprint(456, now: 12)
    progress.recordFingerprint(nil, now: 12)
    let after = progress.snapshot(now: 13)
    #expect(after["sampledContentChanges"] as? Int == 1)
    #expect(after["sampleFailures"] as? Int == 1)
    #expect(after["lastSampledContentChangeAgeMs"] as? Double == 1000)
    let json = try JSONSerialization.data(withJSONObject: after)
    let text = String(decoding: json, as: UTF8.self)
    #expect(!text.contains("fingerprint"))
    #expect(!text.contains("frozen"))
}

@Test func previewFrameProgressSamplesSyntheticPixelsAtMostOncePerSecond() throws {
    var optional: CVPixelBuffer?
    #expect(CVPixelBufferCreate(kCFAllocatorDefault, 64, 36, kCVPixelFormatType_32BGRA,
                               nil, &optional) == kCVReturnSuccess)
    let buffer = try #require(optional)
    func paint(_ byte: Int32) throws {
        #expect(CVPixelBufferLockBaseAddress(buffer, []) == kCVReturnSuccess)
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let base = try #require(CVPixelBufferGetBaseAddress(buffer))
        memset(base, byte, CVPixelBufferGetBytesPerRow(buffer) * CVPixelBufferGetHeight(buffer))
    }
    let progress = PreviewFrameProgress()
    try paint(0)
    progress.sample(buffer, now: 10)
    try paint(255)
    progress.sample(buffer, now: 10.5)
    #expect(progress.snapshot(now: 10.5)["contentSamples"] as? Int == 1)
    progress.sample(buffer, now: 11)
    let result = progress.snapshot(now: 11)
    #expect(result["contentSamples"] as? Int == 2)
    #expect(result["sampledContentChanges"] as? Int == 1)
    #expect(result["sampleFailures"] as? Int == 0)
    #expect(PreviewFrameProgress().snapshot(now: 11)["contentSamples"] as? Int == 0,
            "new streams must not inherit another stream's sample state")
}
