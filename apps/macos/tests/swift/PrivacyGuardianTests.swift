// Guardian policy tests never launch its process, show a window or post input.
import CoreGraphics
import Testing
@testable import QuietBridgeNative

@Test func guardianDoesNotTreatTheInitialLockGestureReleaseAsTakeover() {
    var policy = PrivacyGuardianInputPolicy()
    let initialGesture = policy.accepts(flagsChanged: true, flags: [.maskControl, .maskCommand],
                                       sourcePID: 0, userData: 0)
    let controlReleased = policy.accepts(flagsChanged: true, flags: [.maskCommand], sourcePID: 0, userData: 0)
    let commandReleased = policy.accepts(flagsChanged: true, flags: [], sourcePID: 0, userData: 0)
    #expect(!initialGesture)
    #expect(!controlReleased)
    #expect(!commandReleased)
    // Input while loginwindow is being prepared must still stop automation.
    let newInteraction = policy.accepts(flagsChanged: false, flags: [], sourcePID: 0, userData: 0)
    #expect(newInteraction)
}

@Test func guardianAcceptsNewModifierPressesAfterTheCoverTakesOver() {
    var policy = PrivacyGuardianInputPolicy()
    policy.arm(flags: [.maskControl, .maskCommand])
    let controlReleased = policy.accepts(flagsChanged: true, flags: [.maskCommand], sourcePID: 0, userData: 0)
    let commandReleased = policy.accepts(flagsChanged: true, flags: [], sourcePID: 0, userData: 0)
    let shiftPressed = policy.accepts(flagsChanged: true, flags: [.maskShift], sourcePID: 0, userData: 0)
    let unchanged = policy.accepts(flagsChanged: true, flags: [.maskShift], sourcePID: 0, userData: 0)
    let shiftReleased = policy.accepts(flagsChanged: true, flags: [], sourcePID: 0, userData: 0)
    #expect(!controlReleased)
    #expect(!commandReleased)
    #expect(shiftPressed)
    #expect(!unchanged)
    #expect(!shiftReleased)
}

@Test func guardianIgnoresSyntheticInputWithoutChangingThePhysicalModifierBaseline() {
    var policy = PrivacyGuardianInputPolicy()
    policy.arm(flags: [])
    let markedInput = policy.accepts(flagsChanged: false, flags: [], sourcePID: 0,
                                     userData: TaskPrivacySyntheticEvents.marker)
    let processInput = policy.accepts(flagsChanged: false, flags: [], sourcePID: 42, userData: 0)
    let processModifier = policy.accepts(flagsChanged: true, flags: [.maskCommand], sourcePID: 42, userData: 0)
    let physicalModifier = policy.accepts(flagsChanged: true, flags: [.maskCommand], sourcePID: 0, userData: 0)
    #expect(!markedInput)
    #expect(!processInput)
    #expect(!processModifier)
    #expect(physicalModifier)
}

@Test func guardianRevocationCannotBeUndoneByAnAlreadyQueuedHeartbeat() {
    var lease = PrivacyGuardianLeaseState()
    lease.receivedHeartbeat(at: 10)
    #expect(lease.isReady(at: 10.5))
    #expect(!lease.isReady(at: 11))
    let firstRevocation = lease.revoke()
    #expect(firstRevocation)
    lease.receivedHeartbeat(at: 12)
    #expect(!lease.isReady(at: 12))
    #expect(lease.lastReportAt == 10)
    let repeatedRevocation = lease.revoke()
    #expect(!repeatedRevocation)
}

@Test func guardianRetainsCoverWhenParentLossMayLeaveAnAllowOutstanding() {
    var handoff = PrivacyGuardianHandoffState()
    handoff.observeSessionLocked(true)
    #expect(!handoff.mayReleaseAfterParentLoss(sessionLocked: true))
    handoff.acknowledgeRevocation(automaticUnlockPending: true)
    handoff.observeSessionLocked(nil)
    #expect(!handoff.mayReleaseAfterParentLoss(sessionLocked: true))
    // The already-consumed Allow takes effect before the replacement lock.
    handoff.observeSessionLocked(false)
    #expect(!handoff.mayReleaseAfterParentLoss(sessionLocked: false))
    handoff.observeSessionLocked(true)
    #expect(handoff.mayReleaseAfterParentLoss(sessionLocked: true))
}

@Test func guardianReleasesOnlyAConfirmedLockAfterSettledAuthorization() {
    var handoff = PrivacyGuardianHandoffState()
    handoff.automaticUnlockSettled()
    #expect(!handoff.mayReleaseAfterParentLoss(sessionLocked: nil))
    #expect(!handoff.mayReleaseAfterParentLoss(sessionLocked: false))
    #expect(handoff.mayReleaseAfterParentLoss(sessionLocked: true))
    // A newer Allow warning takes precedence over the earlier settled state.
    handoff.acknowledgeRevocation(automaticUnlockPending: true)
    #expect(!handoff.mayReleaseAfterParentLoss(sessionLocked: true))
    handoff.acknowledgeRevocation(automaticUnlockPending: false)
    #expect(handoff.revocationAcknowledged)
    #expect(handoff.mayReleaseAfterParentLoss(sessionLocked: true))
}
