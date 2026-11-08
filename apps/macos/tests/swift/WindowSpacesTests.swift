// Value-only membership decisions; these tests do not instantiate WindowSpaces or query WindowServer.
import Testing
@testable import QuietBridgeNative

@Test func cursorRoutesToTheTargetsActualSpacesInsteadOfTheVisibleDesktop() {
    #expect(CursorSpaceRoute.decide(windowID: 7, membership: WindowSpaceMembership(spaceIDs: [8], currentSpaceID: 2)) == .target([8]))
    #expect(CursorSpaceRoute.decide(windowID: 7, membership: WindowSpaceMembership(spaceIDs: [2], currentSpaceID: 2)) == .target([2]))
    #expect(CursorSpaceRoute.decide(windowID: 7, membership: WindowSpaceMembership(spaceIDs: [8, 2, 8], currentSpaceID: 2)) == .target([2, 8]))
}

@Test func cursorHidesForEveryUnknownOrInvalidSpaceRoute() {
    let known = WindowSpaceMembership(spaceIDs: [8], currentSpaceID: 2)
    #expect(CursorSpaceRoute.decide(windowID: nil, membership: known) == .hidden)
    #expect(CursorSpaceRoute.decide(windowID: 0, membership: known) == .hidden)
    #expect(CursorSpaceRoute.decide(windowID: 7, membership: nil) == .hidden)
    for membership in [WindowSpaceMembership(spaceIDs: nil, currentSpaceID: 2),
                       WindowSpaceMembership(spaceIDs: [], currentSpaceID: 2),
                       WindowSpaceMembership(spaceIDs: [8], currentSpaceID: nil),
                       WindowSpaceMembership(spaceIDs: [8], currentSpaceID: 0),
                       WindowSpaceMembership(spaceIDs: [0, 8], currentSpaceID: 2)] {
        #expect(CursorSpaceRoute.decide(windowID: 7, membership: membership) == .hidden)
    }
}

@Test func cursorAssignmentRequiresAnExactReadbackWithoutExtraDesktops() {
    #expect(CursorSpaceRoute.matches([8], target: [8]))
    #expect(CursorSpaceRoute.matches([8, 2], target: [2, 8]))
    #expect(!CursorSpaceRoute.matches([2, 8], target: [8]))
    #expect(!CursorSpaceRoute.matches([2], target: [8]))
    #expect(!CursorSpaceRoute.matches(nil, target: [8]))
    #expect(!CursorSpaceRoute.matches([], target: []))
    #expect(!CursorSpaceRoute.matches([0], target: [0]))
}

@Test func checksEverySpaceAssignedToTheWindow() {
    let present = WindowSpaceMembership(spaceIDs: [2, 4, 8], currentSpaceID: 4)
    let absent = WindowSpaceMembership(spaceIDs: [2, 4, 8], currentSpaceID: 3)
    #expect(present.onCurrentSpace == true)
    #expect(absent.onCurrentSpace == false)
}

@Test func unavailableMembershipDoesNotBecomeAnotherSpace() {
    #expect(WindowSpaceMembership(spaceIDs: nil, currentSpaceID: 4).onCurrentSpace == nil)
    #expect(WindowSpaceMembership(spaceIDs: [4], currentSpaceID: nil).onCurrentSpace == nil)
    #expect(WindowSpaceMembership(spaceIDs: [], currentSpaceID: 4).onCurrentSpace == nil)
    #expect(WindowSpaceMembership(spaceIDs: [4], currentSpaceID: 0).onCurrentSpace == nil)
}

@Test func usesEachWindowsAssociatedDisplaySpace() {
    let primaryDisplay = WindowSpaceMembership(spaceIDs: [2], currentSpaceID: 2)
    let secondaryDisplay = WindowSpaceMembership(spaceIDs: [4], currentSpaceID: 4)
    let parkedOnSecondaryDisplay = WindowSpaceMembership(spaceIDs: [5], currentSpaceID: 4)
    #expect(primaryDisplay.onCurrentSpace == true)
    #expect(secondaryDisplay.onCurrentSpace == true)
    #expect(parkedOnSecondaryDisplay.onCurrentSpace == false)
}
