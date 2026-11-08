import Foundation
import Testing
@testable import QuietBridgeNative

@Suite("FocusProbe")
struct FocusProbeTests {
    @Test func acceptsOnlyBoundedExplicitTargets() throws {
        let options = try FocusProbeOptions(arguments: ["--probe-focus", "25", "42", "900"])
        #expect(options.seconds == 25)
        #expect(options.targetPID == 42)
        #expect(options.targetWindowID == 900)
        for args in [
            ["--probe-focus"], ["--probe-focus", "0", "42", "900"],
            ["--probe-focus", "121", "42", "900"], ["--probe-focus", "25", "0", "900"],
            ["--probe-focus", "25", "42", "0"], ["--probe-focus", "25", "42", "900", "text"],
            ["--probe-other", "25", "42", "900"]
        ] {
            #expect(throws: NativeFailure.self) { try FocusProbeOptions(arguments: args) }
        }
    }

    @Test func journalCannotContainInputValuesOrWindowTitles() throws {
        var journal = FocusProbeJournal()
        let item = journal.append(kind: "AXFocusedWindowChanged", uptime: 10, pid: 42,
                                  windowID: 900, elementID: 123)
        let object = try #require(item?.object)
        #expect(Set(object.keys) == Set(["event", "uptime", "pid", "windowId", "elementId"]))
        #expect(object["elementId"] == .string("123"))
        #expect(object["text"] == nil)
        #expect(object["value"] == nil)
        #expect(object["title"] == nil)
    }

    @Test func journalHasAnExplicitCapacityAndReportsDrops() {
        var journal = FocusProbeJournal()
        for index in 0..<FocusProbeJournal.limit {
            #expect(journal.append(kind: "AXFocusedWindowChanged", uptime: Double(index)) != nil)
        }
        #expect(journal.append(kind: "overflow", uptime: 3000) == nil)
        #expect(journal.events.count == FocusProbeJournal.limit)
        #expect(journal.dropped == 1)
    }

    @Test func clickEvidenceDoesNotMistakeRestorationForVerification() {
        let result = AXWorker.clickResult(route: "accessibility", action: "AXPress",
            frontBefore: 10, frontAfterAction: 10, restorationAttempted: true, restorationAccepted: true,
            frontWindowBefore: 900, frontWindowAfterAction: 901)
        #expect(result["route"] == .string("accessibility"))
        #expect(result["axAction"] == .string("AXPress"))
        #expect(result["dispatched"] == .bool(true))
        #expect(result["verified"] == .bool(false))
        #expect(result["focusRestorationAttempted"] == .bool(true))
        #expect(result["frontPidBefore"] == .number(10))
        #expect(result["frontPidAfterAction"] == .number(10))
        #expect(result["frontWindowIdBefore"] == .number(900))
        #expect(result["frontWindowIdAfterAction"] == .number(901))
        #expect(result["focusPreserved"] == .bool(false))
        let pointer = AXWorker.clickResult(route: "window-pointer")
        #expect(pointer["frontPidBefore"] == .null)
        #expect(pointer["focusRestorationAccepted"] == .null)
        #expect(pointer["focusPreserved"] == .null)
        #expect(pointer["verified"] == .bool(false))
    }

    @Test func backgroundTargetsNeverUseAccessibilityClickActions() {
        #expect(AXWorker.shouldUseAccessibilityClick(frontPID: 10, targetPID: 10,
                                                     frontWindowID: 900, targetWindowID: 900))
        #expect(!AXWorker.shouldUseAccessibilityClick(frontPID: 10, targetPID: 10,
                                                      frontWindowID: 901, targetWindowID: 900))
        #expect(!AXWorker.shouldUseAccessibilityClick(frontPID: 20, targetPID: 10,
                                                      frontWindowID: 900, targetWindowID: 900))
        #expect(!AXWorker.shouldUseAccessibilityClick(frontPID: 10, targetPID: 10,
                                                      frontWindowID: nil, targetWindowID: 900))
        #expect(AXWorker.foregroundUnchanged(beforePID: 10, beforeWindow: 900,
                                             afterPID: 10, afterWindow: 900) == true)
        #expect(AXWorker.foregroundUnchanged(beforePID: 10, beforeWindow: 900,
                                             afterPID: 10, afterWindow: 901) == false)
        #expect(AXWorker.foregroundUnchanged(beforePID: 10, beforeWindow: 900,
                                             afterPID: 20, afterWindow: 900) == false)
        #expect(AXWorker.foregroundUnchanged(beforePID: 10, beforeWindow: nil,
                                             afterPID: 10, afterWindow: 900) == nil)
    }
}
