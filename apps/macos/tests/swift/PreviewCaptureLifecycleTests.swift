import Foundation
import Testing
@testable import QuietBridgeNative

@Suite @MainActor struct PreviewCaptureLifecycleTests {
    @Test(arguments: [false, true]) func stopWaitsForPendingStartWithoutDelayingPanelRelease(startFails: Bool) async {
        let lifecycle = PreviewCaptureLifecycle(generation: UUID(), windowID: 7, source: "window", emit: { _, _ in })
        lifecycle.startRequested()
        #expect(lifecycle.stopRequested(reason: "release_or_target_change"))
        var invokedStop = false
        let stop = Task { @MainActor in
            await lifecycle.waitForStartCompletion()
            invokedStop = true
            lifecycle.stopCompleted()
        }
        await Task.yield()
        #expect(!invokedStop)
        #expect(lifecycle.snapshot()["stopConfirmed"] as? Bool == false)
        if startFails { lifecycle.startFailed(NSError(domain: NSCocoaErrorDomain, code: 1)) }
        else { lifecycle.startCompleted() }
        await stop.value
        #expect(invokedStop)
        #expect(lifecycle.snapshot()["stopConfirmed"] as? Bool == true)
    }

    @Test func closingPanelDoesNotConfirmCaptureStopUntilCompletion() throws {
        var time = 10.0
        var records: [(String, [String: JSONValue])] = []
        let generation = UUID()
        let lifecycle = PreviewCaptureLifecycle(generation: generation, windowID: 7, source: "window",
            now: { time }, emit: { records.append(($0, $1)) })
        lifecycle.startRequested(); time = 11; lifecycle.startCompleted()
        #expect(lifecycle.stopRequested(reason: "release_or_target_change"))
        time = 13
        #expect(lifecycle.snapshot()["stopState"] as? String == "requested")
        #expect(lifecycle.snapshot()["stopConfirmed"] as? Bool == false)
        #expect(lifecycle.snapshot()["stopElapsedMs"] as? Double == 2000)
        #expect(!lifecycle.stopRequested(reason: "superseded_start"))
        lifecycle.stopCompleted()
        time = 20
        #expect(lifecycle.snapshot()["stopConfirmed"] as? Bool == true)
        #expect(lifecycle.snapshot()["stopElapsedMs"] as? Double == 2000)
        lifecycle.stopCompleted()
        #expect(records.filter { $0.0 == "preview.captureStopCompleted" }.count == 1)
        #expect(records.last?.1["streamGeneration"] == .string(generation.uuidString))
    }

    @Test func failedStopDoesNotBecomeConfirmedOrExposeRawError() throws {
        var records: [(String, [String: JSONValue])] = []
        let lifecycle = PreviewCaptureLifecycle(generation: UUID(), windowID: 8, source: "window",
            emit: { records.append(($0, $1)) })
        lifecycle.startRequested()
        #expect(lifecycle.stopRequested(reason: "start_failed"))
        let error = NSError(domain: "private-content", code: -9,
                            userInfo: [NSLocalizedDescriptionKey: "private-content"])
        lifecycle.startFailed(error); lifecycle.stopFailed(error)
        lifecycle.stopCompleted() // A late or duplicate callback must not rewrite failure as success.
        #expect(lifecycle.snapshot()["startState"] as? String == "failed")
        #expect(lifecycle.snapshot()["stopState"] as? String == "failed")
        #expect(lifecycle.snapshot()["stopConfirmed"] as? Bool == false)
        let data = try JSONEncoder().encode(JSONValue.array(records.map { .object($0.1) }))
        #expect(!String(decoding: data, as: UTF8.self).contains("private-content"))
        #expect(records.last?.1["platformErrorDomain"] == .string("other"))
        #expect(records.last?.1["platformErrorCode"] == .number(-9))
    }

    @Test func lateOldStopCannotConfirmNewStream() throws {
        var records: [(String, [String: JSONValue])] = []
        let oldID = UUID(), newID = UUID()
        let old = PreviewCaptureLifecycle(generation: oldID, windowID: 7, source: "window",
            emit: { records.append(($0, $1)) })
        old.startRequested()
        #expect(old.stopRequested(reason: "reconfigure"))
        let next = PreviewCaptureLifecycle(generation: newID, windowID: 9, source: "window",
            emit: { records.append(($0, $1)) })
        next.startRequested(); next.startCompleted()
        old.startCompleted() // startCapture may return after a reset requested stop.
        old.stopCompleted()
        #expect(records.last?.1["streamGeneration"] == .string(oldID.uuidString))
        #expect(records.last?.1["trackingWindowId"] == .number(7))
        #expect(next.snapshot()["streamGeneration"] as? String == newID.uuidString)
        #expect(next.snapshot()["stopConfirmed"] as? Bool == false)
    }
}
