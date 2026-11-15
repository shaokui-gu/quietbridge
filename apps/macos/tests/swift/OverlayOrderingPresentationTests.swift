import Testing
@testable import QuietBridgeNative

private let presentationTarget = WindowOverlayOrdering.Target(windowID: 10, pid: 42)

private func expectUpdate(_ value: inout OverlayOrderingPresentation, _ status: WindowOverlayOrdering.Status,
                          target: WindowOverlayOrdering.Target, now: Double, visible: Bool) {
    let actual = value.update(status, target: target, now: now)
    #expect(actual == visible)
    #expect(value.artworkVisible == visible)
}

@Test func initialOrderingNeverShowsUnverifiedContent() {
    var presentation = OverlayOrderingPresentation()
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 0, visible: false)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 0.03, visible: false)
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 0.04, visible: true)
}

@Test func shortOrderingCommitsInTheObservedFailureRangeDoNotBlankThePreview() {
    // Representative failure-to-readback durations in the 0.6.28 field log.
    for duration in [0.01142, 0.02018, 0.03085, 0.03954, 0.04954] {
        var presentation = OverlayOrderingPresentation()
        expectUpdate(&presentation, .verified, target: presentationTarget, now: 0, visible: true)
        expectUpdate(&presentation, .repairing, target: presentationTarget, now: 1, visible: true)
        expectUpdate(&presentation, .repairing, target: presentationTarget, now: 1 + duration / 2, visible: true)
        expectUpdate(&presentation, .verified, target: presentationTarget, now: 1 + duration, visible: true)
        #expect(presentation.artworkVisible)
    }
}

@Test func repeatedRepairCallsCannotExtendTheVisibilityBudget() {
    var presentation = OverlayOrderingPresentation()
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 0, visible: true)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 0, visible: true)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 0.099, visible: true)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: OverlayOrderingPresentation.repairInterval, visible: false)
    for time in [0.11, 1.26771, 5.23178] {
        expectUpdate(&presentation, .repairing, target: presentationTarget, now: time, visible: false)
    }
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 6, visible: true)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 7, visible: true)
}

@Test func unknownOrderingAndChangedWindowOrPIDHideImmediately() {
    for changed in [WindowOverlayOrdering.Target(windowID: 11, pid: 42),
                    WindowOverlayOrdering.Target(windowID: 10, pid: 43)] {
        var presentation = OverlayOrderingPresentation()
        expectUpdate(&presentation, .verified, target: presentationTarget, now: 0, visible: true)
        expectUpdate(&presentation, .repairing, target: changed, now: 0.01, visible: false)
    }
    var presentation = OverlayOrderingPresentation()
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 0, visible: true)
    expectUpdate(&presentation, .unavailable, target: presentationTarget, now: 0.01, visible: false)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 0.02, visible: false)
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 0.03, visible: true)
}

@Test func hidingOrChangingSpaceCannotReuseThePreviousOrderingConfirmation() {
    var presentation = OverlayOrderingPresentation()
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 0, visible: true)
    presentation.reset()
    #expect(!presentation.artworkVisible)
    expectUpdate(&presentation, .repairing, target: presentationTarget, now: 0.01, visible: false)
    expectUpdate(&presentation, .verified, target: presentationTarget, now: 0.02, visible: true)
}
