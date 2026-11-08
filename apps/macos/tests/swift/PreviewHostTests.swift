// Regression cases for moving WorkBuddy, its preview and its capture target independently.
import AppKit
import Testing
@testable import QuietBridgeNative

private func host(_ id: UInt32 = 10, pid: Int32 = 42, visible: Bool = true,
                  bounds: CGRect = CGRect(x: 100, y: 100, width: 1000, height: 700)) -> PreviewHostWindow {
    PreviewHostWindow(windowID: id, pid: pid, bounds: bounds, isOnScreen: visible)
}

@Test func previewHostRemainsBoundWhileItsWindowMovesToAnotherSpace() {
    let original = host()
    let moved = host(visible: false)
    let larger = host(11, bounds: CGRect(x: 0, y: 0, width: 1500, height: 1000))
    #expect(PreviewHostWindow.select([larger, moved], hostPIDs: [42], responsiblePID: 99,
                                     previous: original) == moved)
    #expect(PreviewHostWindow.select([larger], hostPIDs: [42], responsiblePID: 99,
                                     previous: original) == larger)
}

@Test func previewHostDoesNotBecomeTheResponsibleTerminalOrAnUnrelatedFocusedWindow() {
    let hostWindow = host(visible: false)
    let terminal = host(20, pid: 99, bounds: CGRect(x: 0, y: 0, width: 2000, height: 1200))
    #expect(PreviewHostWindow.select([terminal, hostWindow], hostPIDs: [42], responsiblePID: 99,
                                     previous: nil) == hostWindow)
    #expect(PreviewHostWindow.select([terminal], hostPIDs: [42], responsiblePID: 99, previous: nil) == nil)
    #expect(PreviewHostWindow.select([terminal], hostPIDs: [], responsiblePID: 99, previous: nil) == terminal)
}

@Test func previewHostSpaceMovementDoesNotDependOnScreenCoordinatesOrTheCaptureSpace() {
    let before = WindowSpaceMembership(spaceIDs: [2], currentSpaceID: 2)
    let after = WindowSpaceMembership(spaceIDs: [8], currentSpaceID: 8)
    #expect(PreviewHostRoute.decide(host: host(), membership: before, held: true,
                                   source: .window, targetOnCurrentSpace: false) == .host([2]))
    // Identical bounds on a new Space must still migrate the panel to WorkBuddy.
    #expect(PreviewHostRoute.decide(host: host(), membership: after, held: true,
                                   source: .window, targetOnCurrentSpace: false) == .host([8]))
    #expect(!CursorSpaceRoute.matches([2], target: [8]))
}

@Test func leasedPreviewRemainsVisibleWhenTheTargetJoinsWorkBuddysCurrentDesktop() {
    #expect(PreviewHostRoute.decide(host: host(), membership: WindowSpaceMembership(spaceIDs: [2], currentSpaceID: 2),
                                   held: true, source: .window, targetOnCurrentSpace: true) == .host([2]))
    // Switching the user's desktop does not move a background WorkBuddy preview to the user's new Space.
    #expect(PreviewHostRoute.decide(host: host(visible: false), membership: WindowSpaceMembership(spaceIDs: [2], currentSpaceID: 8),
                                   held: true, source: .window, targetOnCurrentSpace: true) == .host([2]))
}

@Test func previewHidesWhenTheHostIsMissingMinimizedOrItsSpacesAreUnknown() {
    let known = WindowSpaceMembership(spaceIDs: [2], currentSpaceID: 2)
    #expect(PreviewHostRoute.decide(host: nil, membership: known, held: true,
                                   source: .desktop, targetOnCurrentSpace: true) == .hidden)
    #expect(PreviewHostRoute.decide(host: host(visible: false), membership: known, held: true,
                                   source: .application, targetOnCurrentSpace: true) == .hidden)
    #expect(PreviewHostRoute.decide(host: host(), membership: nil, held: true,
                                   source: .application, targetOnCurrentSpace: true) == .hidden)
}

@Test func previewFollowsTheHostOntoAnotherDisplayWithTheSameTopRightInset() throws {
    let panel = CGRect(x: 0, y: 0, width: 380, height: 240)
    let placed = try #require(PreviewHostGeometry.frame(for: panel,
        hostBounds: CGRect(x: -1500, y: -100, width: 1000, height: 700),
        mainDisplayHeight: 900, visibleFrame: CGRect(x: -1600, y: 0, width: 1600, height: 1200)))
    #expect(placed.maxX == -516)
    #expect(placed.maxY == 984)
    #expect(placed.size == panel.size)
}

@Test func previewAnchorsToTheHostsDisplayAndKeepsItsCardWithinTheVisibleArea() throws {
    let panel = CGRect(x: 0, y: 0, width: 380, height: 264)
    let visible = CGRect(x: 1600, y: 0, width: 1200, height: 900)
    let placed = try #require(PreviewHostGeometry.frame(for: panel,
        hostBounds: CGRect(x: 2300, y: -100, width: 1000, height: 700),
        mainDisplayHeight: 900, visibleFrame: visible))
    #expect(visible.contains(placed))
    #expect(placed.maxX == visible.maxX)
    #expect(placed.maxY == visible.maxY)
}

@Test func previewTracksEveryIntermediateHostWidthUpToFullScreenWidth() throws {
    let visible = CGRect(x: 0, y: 0, width: 1600, height: 1000)
    let panel = CGRect(x: 400, y: 200, width: 380, height: 240)
    for width in stride(from: CGFloat(800), through: 1600, by: 20) {
        let placed = try #require(PreviewHostGeometry.frame(for: panel,
            hostBounds: CGRect(x: 0, y: 0, width: width, height: 900),
            mainDisplayHeight: 1000, visibleFrame: visible))
        #expect(placed.maxX == width - 16)
        #expect(placed.maxY == 984)
    }
}

@Test func portraitAndOversizedPreviewsAlwaysFitAShrinkingHost() throws {
    let host = CGRect(x: 100, y: 100, width: 340, height: 220)
    let visible = CGRect(x: 0, y: 0, width: 1600, height: 1000)
    for aspect: CGFloat in [0.5625, 1, 2.5] {
        let placed = try #require(PreviewHostGeometry.frame(for: CGRect(x: 0, y: 0, width: 1200, height: 1200 * aspect),
            hostBounds: host, mainDisplayHeight: 1000, visibleFrame: visible))
        #expect(placed.maxX == 424)
        #expect(placed.maxY == 884)
        #expect(placed.width <= 308)
        #expect(placed.height <= 188)
        #expect(abs(placed.height / placed.width - aspect) < 0.00001)
    }
    #expect(PreviewHostGeometry.frame(for: CGRect(x: 0, y: 0, width: 400, height: 250),
        hostBounds: CGRect(x: 2000, y: 0, width: 800, height: 600),
        mainDisplayHeight: 1000, visibleFrame: visible) == nil)
}

@MainActor @Test func followLoopRunsDuringMouseTrackingAndStopsWithItsLease() {
    let loop = PreviewHostFollowLoop()
    var updates = 0
    loop.start { updates += 1 }
    let deadline = Date(timeIntervalSinceNow: 0.15)
    while Date() < deadline { _ = RunLoop.main.run(mode: .eventTracking, before: deadline) }
    #expect(updates >= 2)
    loop.stop()
    let stoppedAt = updates
    _ = RunLoop.main.run(mode: .eventTracking, before: Date(timeIntervalSinceNow: 0.06))
    #expect(updates == stoppedAt)
}

@Test func previewKeepsTheSameCaptureWindowWhenItsSpaceChanges() {
    #expect(PreviewCaptureRoute.decide(source: .application, targetOnCurrentSpace: true) == .capture(.application))
    #expect(PreviewCaptureRoute.decide(source: .application, targetOnCurrentSpace: false) == .capture(.window))
    #expect(PreviewCaptureRoute.decide(source: .application, targetOnCurrentSpace: nil) == .capture(.window))
    #expect(PreviewCaptureRoute.decide(source: .window, targetOnCurrentSpace: false) == .capture(.window))
    #expect(PreviewCaptureRoute.decide(source: .desktop, targetOnCurrentSpace: false) == .waitingForTargetDesktop)
    #expect(PreviewCaptureRoute.decide(source: .desktop, targetOnCurrentSpace: true) == .capture(.desktop))
}

@Test func temporaryMoveExpiresThirtySecondsAfterReleaseEvenWithoutHostMovement() {
    var placement = PreviewTemporaryPlacement()
    let moved = CGRect(x: 200, y: 180, width: 380, height: 240)
    placement.drag(to: moved, host: host())
    // Holding the mouse for over 30s must not snap the preview out of the hand.
    #expect(placement.resolve(host: host(), now: 200) == moved)
    placement.finishDrag(now: 200)
    #expect(placement.resolve(host: host(), now: 229.99) == moved)
    #expect(placement.resolve(host: host(), now: 230) == nil)
    #expect(!placement.isActive)
}

@Test func draggingAgainRestartsTheTemporaryMoveDeadlineButResizingDoesNot() {
    var placement = PreviewTemporaryPlacement()
    let first = CGRect(x: 200, y: 180, width: 380, height: 240)
    let second = first.offsetBy(dx: 100, dy: -50)
    placement.drag(to: first, host: host())
    placement.finishDrag(now: 100)
    placement.drag(to: second, host: host())
    placement.finishDrag(now: 120)
    let resized = PreviewResizeGeometry().frame(for: .bottomLeft, startFrame: second,
        delta: CGSize(width: -100, height: 0), aspect: second.height / second.width)
    placement.resize(to: resized, host: host(), now: 140)
    #expect(placement.resolve(host: host(), now: 149.99) == resized)
    #expect(placement.resolve(host: host(), now: 150) == nil)
}

@Test func anyHostMovementSizeOrIdentityChangeImmediatelyCancelsTheTemporaryPosition() {
    let original = host()
    for changed in [host(bounds: original.bounds.offsetBy(dx: 1, dy: 0)),
                    host(bounds: original.bounds.offsetBy(dx: 0, dy: -1)),
                    host(bounds: CGRect(x: 100, y: 100, width: 1001, height: 700)),
                    host(bounds: CGRect(x: 100, y: 100, width: 1000, height: 701)),
                    host(11), host(pid: 43)] {
        var placement = PreviewTemporaryPlacement()
        placement.drag(to: CGRect(x: 200, y: 180, width: 380, height: 240), host: original)
        placement.finishDrag(now: 100)
        #expect(placement.resolve(host: changed, now: 101) == nil)
        #expect(placement.resolve(host: original, now: 102) == nil)
    }
}

@Test func temporaryMovesStayWithinTheHostDisplayAndPreserveTheResizeTopRight() {
    let visible = CGRect(x: -1600, y: -200, width: 1600, height: 1000)
    let fittedSize = CGSize(width: 380, height: 240)
    for origin in [CGPoint(x: -2000, y: -500), CGPoint(x: -50, y: 700), CGPoint(x: -800, y: 200)] {
        let proposed = CGRect(origin: origin, size: fittedSize)
        let clamped = PreviewHostGeometry.temporaryFrame(proposed, fittedSize: fittedSize, visibleFrame: visible)
        #expect(visible.contains(clamped))
        #expect(clamped.size == fittedSize)
        if visible.contains(proposed) { #expect(clamped == proposed) }
    }
    let original = CGRect(x: -800, y: 200, width: 380, height: 240)
    let shrunk = PreviewHostGeometry.temporaryFrame(original, fittedSize: CGSize(width: 300, height: 190), visibleFrame: visible)
    #expect(shrunk.maxX == original.maxX)
    #expect(shrunk.maxY == original.maxY)
}

@Test func endingThePreviewClearsTemporaryPlacementAndItsCountdown() {
    var placement = PreviewTemporaryPlacement()
    placement.drag(to: CGRect(x: 200, y: 180, width: 380, height: 240), host: host())
    placement.finishDrag(now: 100)
    placement.reset()
    placement.finishDrag(now: 101)
    #expect(!placement.isActive)
    #expect(placement.resolve(host: host(), now: 102) == nil)
    #expect(placement.remainingSeconds(now: 102) == nil)
}
