// Regression cases for moving WorkBuddy, its preview and its capture target independently.
import CoreGraphics
import Testing
@testable import CuaNative

private func host(_ id: UInt32 = 10, pid: Int32 = 42, visible: Bool = true,
                  bounds: CGRect = CGRect(x: 100, y: 100, width: 1000, height: 700)) -> PreviewHostWindow {
    PreviewHostWindow(windowID: id, pid: pid, bounds: bounds, isOnScreen: visible)
}

@Test func previewHostRemainsBoundWhileItsWindowMovesToAnotherSpace() {
    let original = host()
    let moved = host(visible: false)
    let larger = host(11, bounds: CGRect(x: 0, y: 0, width: 1500, height: 1000))
    #expect(PreviewHostWindow.select([larger, moved], workBuddyPIDs: [42], responsiblePID: 99,
                                     previous: original) == moved)
    #expect(PreviewHostWindow.select([larger], workBuddyPIDs: [42], responsiblePID: 99,
                                     previous: original) == larger)
}

@Test func previewHostDoesNotBecomeTheResponsibleTerminalOrAnUnrelatedFocusedWindow() {
    let workBuddy = host(visible: false)
    let terminal = host(20, pid: 99, bounds: CGRect(x: 0, y: 0, width: 2000, height: 1200))
    #expect(PreviewHostWindow.select([terminal, workBuddy], workBuddyPIDs: [42], responsiblePID: 99,
                                     previous: nil) == workBuddy)
    #expect(PreviewHostWindow.select([terminal], workBuddyPIDs: [42], responsiblePID: 99, previous: nil) == nil)
    #expect(PreviewHostWindow.select([terminal], workBuddyPIDs: [], responsiblePID: 99, previous: nil) == terminal)
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

@Test func manuallyDraggedPreviewStillFollowsTheHostOntoAnotherDisplay() {
    let old = CGRect(x: 100, y: 100, width: 1000, height: 700)
    let new = old.offsetBy(dx: -1600, dy: -200)
    let panel = CGRect(x: 400, y: 200, width: 380, height: 264)
    let placed = PreviewHostGeometry.frame(for: panel, hostBounds: new, previousHostBounds: old,
        manuallyPositioned: true, mainDisplayHeight: 900,
        visibleFrame: CGRect(x: -1600, y: 0, width: 1600, height: 1200))
    #expect(placed.origin == CGPoint(x: -1200, y: 400))
    #expect(placed.size == panel.size)
}

@Test func previewAnchorsToTheHostsDisplayAndKeepsItsCardWithinTheVisibleArea() {
    let panel = CGRect(x: 0, y: 0, width: 380, height: 264)
    let visible = CGRect(x: 1600, y: 0, width: 1200, height: 900)
    let placed = PreviewHostGeometry.frame(for: panel,
        hostBounds: CGRect(x: 2300, y: -100, width: 1000, height: 700), previousHostBounds: nil,
        manuallyPositioned: false, mainDisplayHeight: 900, visibleFrame: visible)
    #expect(visible.contains(placed))
    #expect(placed.maxX == visible.maxX)
    #expect(placed.maxY == visible.maxY)
}

@Test func previewKeepsTheSameCaptureWindowWhenItsSpaceChanges() {
    #expect(PreviewCaptureRoute.decide(source: .application, targetOnCurrentSpace: true) == .capture(.application))
    #expect(PreviewCaptureRoute.decide(source: .application, targetOnCurrentSpace: false) == .capture(.window))
    #expect(PreviewCaptureRoute.decide(source: .application, targetOnCurrentSpace: nil) == .capture(.window))
    #expect(PreviewCaptureRoute.decide(source: .window, targetOnCurrentSpace: false) == .capture(.window))
    #expect(PreviewCaptureRoute.decide(source: .desktop, targetOnCurrentSpace: false) == .waitingForTargetDesktop)
    #expect(PreviewCaptureRoute.decide(source: .desktop, targetOnCurrentSpace: true) == .capture(.desktop))
}
