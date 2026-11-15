import Foundation
import Testing
@testable import QuietBridgeNative

@Suite @MainActor struct PreviewReplyTests {
    @Test func activeReplyRetainsSourceProgressAndBothStreamGenerations() {
        let oldID = UUID(), currentID = UUID()
        let old = PreviewCaptureLifecycle(generation: oldID, windowID: 7, source: "window", emit: { _, _ in })
        old.startRequested(); old.startCompleted()
        #expect(old.stopRequested(reason: "reconfigure"))
        let current = PreviewCaptureLifecycle(generation: currentID, windowID: 8, source: "window", emit: { _, _ in })
        current.startRequested(); current.startCompleted()
        let values: [String: Any] = ["streamGeneration": currentID.uuidString, "trackingWindowId": 8,
            "frameProgress": ["bodySamples": 12, "bodyContentChanges": 0,
                              "sourceFrame": ["pixelFormat": "420v", "bodyNearUniform": true]],
            "captureLifecycle": current.snapshot(), "captureStop": old.snapshot()]
        let reply = RequestCoordinator.previewDiagnosticsSnapshot(values, holding: true, holdRemainingSeconds: 30).object
        #expect(reply?["frameProgress"]?.object?["bodySamples"] == .number(12))
        #expect(reply?["frameProgress"]?.object?["sourceFrame"]?.object?["bodyNearUniform"] == .bool(true))
        #expect(reply?["captureLifecycle"]?.object?["streamGeneration"] == .string(currentID.uuidString))
        #expect(reply?["captureStop"]?.object?["streamGeneration"] == .string(oldID.uuidString))
        #expect(reply?["captureStop"]?.object?["stopConfirmed"] == .bool(false))
        #expect(reply?["holding"] == .bool(true))
    }

    @Test func releasedReplyKeepsPendingThenCompletedCaptureStop() {
        let lifecycle = PreviewCaptureLifecycle(generation: UUID(), windowID: 7, source: "window", emit: { _, _ in })
        lifecycle.startRequested(); lifecycle.startCompleted()
        #expect(lifecycle.stopRequested(reason: "release_or_target_change"))
        func reply() -> [String: JSONValue]? {
            RequestCoordinator.previewDiagnosticsSnapshot(["captureStop": lifecycle.snapshot()],
                holding: false, holdRemainingSeconds: nil).object
        }
        #expect(reply()?["holding"] == .bool(false))
        #expect(reply()?["captureLifecycle"] == nil)
        #expect(reply()?["captureStop"]?.object?["stopState"] == .string("requested"))
        #expect(reply()?["captureStop"]?.object?["stopConfirmed"] == .bool(false))
        lifecycle.stopCompleted()
        #expect(reply()?["captureStop"]?.object?["stopState"] == .string("completed"))
        #expect(reply()?["captureStop"]?.object?["stopConfirmed"] == .bool(true))
    }

    @Test func missingOrUnencodableDiagnosticsNeverInventStopConfirmation() {
        let values: [String: Any] = ["captureStop": ["stopElapsedMs": Double.nan], "captureLifecycle": "invalid"]
        let reply = RequestCoordinator.previewDiagnosticsSnapshot(values, holding: false, holdRemainingSeconds: nil).object
        #expect(reply?["captureStop"] == nil)
        #expect(reply?["captureLifecycle"] == nil)
        #expect(reply?["frameProgress"] == nil)
    }

    @Test func hostOrderingEvidenceIsIndependentOfVideoReadinessAndSpaceMembership() {
        let waiting = RequestCoordinator.previewDiagnosticsSnapshot(
            ["panelSpaceVerified": true, "panelOrderVerified": false,
             "panelOrderError": "等待层级回读", "streamReady": true, "visible": false],
            holding: true, holdRemainingSeconds: 30).object
        #expect(waiting?["panelSpaceVerified"] == .bool(true))
        #expect(waiting?["panelOrderVerified"] == .bool(false))
        #expect(waiting?["panelOrderError"] == .string("等待层级回读"))
        #expect(waiting?["streamReady"] == .bool(true))
        #expect(waiting?["visible"] == .bool(false))
        let ready = RequestCoordinator.previewDiagnosticsSnapshot(
            ["panelOrderVerified": true], holding: true, holdRemainingSeconds: 30).object
        #expect(ready?["panelOrderVerified"] == .bool(true))
        #expect(ready?["panelOrderError"] == .null)
        let missing = RequestCoordinator.previewDiagnosticsSnapshot([:], holding: false, holdRemainingSeconds: nil).object
        #expect(missing?["panelOrderVerified"] == .bool(false))
    }

    @Test func pendingOrderingKeepsItsActualPresentationSeparateFromVerification() {
        let reply = RequestCoordinator.previewDiagnosticsSnapshot(
            ["panelOrderState": "repairing", "panelOrderVerified": false,
             "panelArtworkVisible": true, "visible": true, "panelSpaceVerified": true],
            holding: true, holdRemainingSeconds: 30).object
        #expect(reply?["panelOrderState"] == .string("repairing"))
        #expect(reply?["panelOrderVerified"] == .bool(false))
        #expect(reply?["panelArtworkVisible"] == .bool(true))
        #expect(reply?["visible"] == .bool(true))
    }
}

@Test @MainActor func browserContentSourceIsDistinctFromWindowVideo() {
    #expect(PreviewSource.browserContent.rawValue == "browser_content")
    #expect(PreviewSource.browserContent.scope == "web_content")
    #expect(!PreviewSource.browserContent.usesDisplay)
    #expect(PreviewSource.browserContent.label.contains("非整窗视频"))
    let reply = RequestCoordinator.previewDiagnosticsSnapshot(
        ["mode": "browser_content", "captureMode": "browser_content", "scope": "web_content",
         "streamReady": false, "videoFrames": 0], holding: true, holdRemainingSeconds: 60).object
    #expect(reply?["mode"] == .string("browser_content"))
    #expect(reply?["scope"] == .string("web_content"))
    #expect(reply?["streamReady"] == .bool(false))
}
