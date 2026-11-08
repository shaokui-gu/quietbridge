// Exercise the lock handoff without creating windows, posting input, or locking macOS.
import Foundation
import Testing
@testable import QuietBridgeNative

private func expectUnlockAttemptFailure(_ code: String, operation: () throws -> Void) {
    do {
        try operation()
        Issue.record("Expected unlock attempt failure: \(code)")
    } catch let failure as NativeFailure {
        #expect(failure.code == code)
    } catch {
        Issue.record("Unexpected unlock attempt error: \(error)")
    }
}

@Test func lockHandoffWaitsForTheUsersSystemLockBeforeCoveringTheDesktop() throws {
    var state = TaskPrivacyHandoffState()
    #expect(!state.active)
    #expect(!state.coverRequired)
    let inactiveLockObserved = state.observeExternalLock()
    let inactiveUnlock = state.beginAutomaticUnlock()
    #expect(!inactiveLockObserved)
    #expect(inactiveUnlock == nil)

    let taskBegan = state.beginTask()
    #expect(taskBegan)
    #expect(state.phase == .watching)
    #expect(state.active)
    #expect(!state.coverRequired)
    let ordinaryInputTookOver = state.requestTakeover()
    let renewalRestarted = state.beginTask()
    #expect(!ordinaryInputTookOver, "Ordinary input before the initial system lock is not a takeover")
    #expect(!renewalRestarted, "Renewing a task cannot restart the lifecycle")

    let externalLockObserved = state.observeExternalLock()
    #expect(externalLockObserved)
    #expect(state.coverRequired)
    let repeatedLockObserved = state.observeExternalLock()
    #expect(!repeatedLockObserved, "Repeated lock observations cannot create another episode")
    let issuedToken = state.beginAutomaticUnlock()
    let token = try #require(issuedToken)
    #expect(state.automaticUnlockAttempted)
    #expect(state.permitsAutomaticUnlock(token))
    let duplicateUnlock = state.beginAutomaticUnlock()
    #expect(duplicateUnlock == nil)
    let unlockCompleted = state.automaticUnlockCompleted(token)
    #expect(unlockCompleted)
    #expect(state.phase == .shielded)
    #expect(state.coverRequired)
    #expect(!state.permitsAutomaticUnlock(token))
    let lateCompletionAccepted = state.automaticUnlockCompleted(token)
    let subsequentLockObserved = state.observeExternalLock()
    let subsequentUnlock = state.beginAutomaticUnlock()
    #expect(!lateCompletionAccepted, "A late completion must not change the protected state")
    #expect(!subsequentLockObserved)
    #expect(subsequentUnlock == nil, "The same task cannot automatically unlock a second time")
}

@Test func lockHandoffWithdrawsTheCoverOnlyAfterSystemAuthenticationOwnsTheScreen() throws {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()
    let issuedToken = state.beginAutomaticUnlock()
    let token = try #require(issuedToken)
    state.automaticUnlockCompleted(token)

    let takeoverRequested = state.requestTakeover()
    #expect(takeoverRequested)
    #expect(state.phase == .handingBack)
    #expect(state.waitingForManualUnlock)
    #expect(state.active, "The controller must keep observing the handoff until macOS confirms the lock")
    #expect(state.coverRequired)
    let repeatedTakeoverRequested = state.requestTakeover()
    #expect(!repeatedTakeoverRequested, "Repeated clicks and keystrokes must not restart the handoff")

    let unlockedHandoffCompleted = state.completeHandoff(sessionLocked: false, authorizationReady: true)
    #expect(!unlockedHandoffCompleted)
    #expect(state.coverRequired)
    let unknownHandoffCompleted = state.completeHandoff(sessionLocked: nil, authorizationReady: true)
    #expect(!unknownHandoffCompleted)
    #expect(state.coverRequired, "An unknown lock state must not expose the desktop")
    let pendingAuthorizationHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: false)
    #expect(!pendingAuthorizationHandoffCompleted)
    #expect(state.coverRequired, "An already-consumed automatic Allow must settle before removing the cover")

    let confirmedHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(confirmedHandoffCompleted)
    #expect(state.phase == .awaitingManualUnlock)
    #expect(state.waitingForManualUnlock)
    #expect(state.active, "Continue observing until manual unlock; do not end the business task")
    #expect(!state.coverRequired)
    let pausedTaskRestarted = state.beginTask()
    let pausedLockObserved = state.observeExternalLock()
    let pausedUnlock = state.beginAutomaticUnlock()
    let laterHandoffCompleted = state.completeHandoff(sessionLocked: false, authorizationReady: true)
    #expect(!pausedTaskRestarted, "A lease renewal after the user takes over must not cover their desktop again")
    #expect(!pausedLockObserved)
    #expect(pausedUnlock == nil)
    #expect(!laterHandoffCompleted)
    #expect(state.phase == .awaitingManualUnlock)
    let transition1 = state.observeManualUnlock(sessionLocked: false, authorizationPending: false)
    #expect(transition1)
    #expect(state.phase == .manuallyUnlocked)
    #expect(state.active)
    #expect(!state.waitingForManualUnlock)
    #expect(!state.coverRequired)
}

@Test func takeoverDuringAutomaticUnlockRejectsItsLateCompletion() throws {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()
    let issuedToken = state.beginAutomaticUnlock()
    let token = try #require(issuedToken)

    let takeoverRequested = state.requestTakeover()
    #expect(takeoverRequested)
    #expect(!state.permitsAutomaticUnlock(token))
    let lateCompletionAccepted = state.automaticUnlockCompleted(token)
    #expect(!lateCompletionAccepted)
    #expect(state.phase == .handingBack)
    #expect(state.coverRequired)
    let resumedUnlock = state.beginAutomaticUnlock()
    #expect(resumedUnlock == nil)
}

@Test func takeoverBeforeAutomaticUnlockStartsNeverIssuesAnUnlockToken() {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()

    let takeoverRequested = state.requestTakeover()
    #expect(takeoverRequested)
    #expect(state.coverRequired)
    #expect(!state.automaticUnlockAttempted)
    let cancelledUnlock = state.beginAutomaticUnlock()
    let handoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(cancelledUnlock == nil)
    #expect(handoffCompleted)
    #expect(!state.coverRequired)
}

@Test func endingATaskInvalidatesUnlockWorkEvenAfterANewTaskStarts() throws {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()
    let oldIssuedToken = state.beginAutomaticUnlock()
    let oldToken = try #require(oldIssuedToken)
    state.release()

    #expect(state.phase == .inactive)
    #expect(!state.active)
    #expect(!state.coverRequired)
    #expect(!state.permitsAutomaticUnlock(oldToken))
    let releasedCompletionAccepted = state.automaticUnlockCompleted(oldToken)
    #expect(!releasedCompletionAccepted)
    #expect(!state.automaticUnlockAttempted)

    let newTaskBegan = state.beginTask()
    #expect(newTaskBegan)
    state.observeExternalLock()
    let newIssuedToken = state.beginAutomaticUnlock()
    let newToken = try #require(newIssuedToken)
    #expect(newToken != oldToken)
    #expect(!state.permitsAutomaticUnlock(oldToken))
    let oldCompletionAccepted = state.automaticUnlockCompleted(oldToken)
    #expect(!oldCompletionAccepted)
    #expect(state.permitsAutomaticUnlock(newToken))
    let newUnlockCompleted = state.automaticUnlockCompleted(newToken)
    #expect(newUnlockCompleted)
}

@Test func explicitTaskEndResetsTheUnlockEpisodeWithoutBeingRequiredForManualUnlock() throws {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()
    let issuedToken = state.beginAutomaticUnlock()
    let token = try #require(issuedToken)
    state.automaticUnlockCompleted(token)
    state.requestTakeover()
    state.completeHandoff(sessionLocked: true, authorizationReady: true)

    let pausedTaskRestarted = state.beginTask()
    #expect(!pausedTaskRestarted)
    state.release()
    #expect(!state.waitingForManualUnlock)
    let newTaskBegan = state.beginTask()
    #expect(newTaskBegan)
    #expect(state.phase == .watching)
    #expect(!state.coverRequired)
    #expect(!state.automaticUnlockAttempted)
    let newLockObserved = state.observeExternalLock()
    let newToken = state.beginAutomaticUnlock()
    #expect(newLockObserved)
    #expect(newToken != nil)
}

@Test func aLockObservationCannotCompleteAHandoffThatWasNeverRequested() throws {
    var state = TaskPrivacyHandoffState()
    let inactiveHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(!inactiveHandoffCompleted)
    state.beginTask()
    let watchingHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(!watchingHandoffCompleted)
    state.observeExternalLock()
    let preparingHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(!preparingHandoffCompleted)
    let issuedToken = state.beginAutomaticUnlock()
    let token = try #require(issuedToken)
    let unlockingHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(!unlockingHandoffCompleted)
    state.automaticUnlockCompleted(token)
    let shieldedHandoffCompleted = state.completeHandoff(sessionLocked: true, authorizationReady: true)
    #expect(!shieldedHandoffCompleted)
    #expect(state.phase == .shielded)
    #expect(state.coverRequired)
}

@Test func automaticUnlockAttemptsPermitOnlyOneCredentialSubmission() throws {
    let attempt = TaskPrivacyUnlockAttempt()
    try attempt.check()
    try attempt.claimSubmission()
    // Polling for the result is still allowed after submission.
    try attempt.check()
    expectUnlockAttemptFailure("UNLOCK_ATTEMPTS_EXHAUSTED") { try attempt.claimSubmission() }
}

@Test func cancellationBeforeSubmissionPreventsEveryLaterUnlockMutation() {
    let attempt = TaskPrivacyUnlockAttempt()
    attempt.cancel()
    attempt.cancel()
    expectUnlockAttemptFailure("NATIVE_CANCELLED") { try attempt.check() }
    expectUnlockAttemptFailure("NATIVE_CANCELLED") { try attempt.claimSubmission() }
}

@Test func cancellationAfterSubmissionStopsTheRemainingUnlockSteps() throws {
    let attempt = TaskPrivacyUnlockAttempt()
    try attempt.claimSubmission()
    attempt.cancel()
    expectUnlockAttemptFailure("NATIVE_CANCELLED") { try attempt.check() }
    expectUnlockAttemptFailure("NATIVE_CANCELLED") { try attempt.claimSubmission() }
}

@Test func concurrentUnlockWorkersCannotSubmitTheSameEpisodeTwice() async {
    let attempt = TaskPrivacyUnlockAttempt()
    let outcomes = await withTaskGroup(of: String.self, returning: [String].self) { group in
        for _ in 0..<16 {
            group.addTask {
                do {
                    try attempt.claimSubmission()
                    return "submitted"
                } catch let failure as NativeFailure {
                    return failure.code
                } catch {
                    return "unexpected-error"
                }
            }
        }
        var values: [String] = []
        for await outcome in group { values.append(outcome) }
        return values
    }
    #expect(outcomes.filter { $0 == "submitted" }.count == 1)
    #expect(outcomes.filter { $0 == "UNLOCK_ATTEMPTS_EXHAUSTED" }.count == 15)
}

@Test func cancellingTheUnlockTaskRejectsInputWithoutWaitingForExplicitTokenCancellation() async {
    let attempt = TaskPrivacyUnlockAttempt()
    let (started, signal) = AsyncStream<Void>.makeStream()
    let operation = Task {
        signal.yield(())
        defer { signal.finish() }
        do { try await Task.sleep(for: .seconds(60)) } catch {}
        expectUnlockAttemptFailure("NATIVE_CANCELLED") { try attempt.check() }
        expectUnlockAttemptFailure("NATIVE_CANCELLED") { try attempt.claimSubmission() }
    }
    var iterator = started.makeAsyncIterator()
    let observedStart = await iterator.next()
    #expect(observedStart != nil)
    operation.cancel()
    await operation.value
}

@Test func disabledLockedUseNeverCoversTheDesktopOrRequestsAutomaticUnlock() {
    var state = TaskPrivacyHandoffState()
    let transition2 = state.beginTask(lockedUse: false)
    #expect(transition2)
    let transition3 = state.beginTask(lockedUse: true)
    #expect(!transition3, "Lease renewal cannot change the capability choice")
    let transition4 = state.observeExternalLock()
    #expect(!transition4)
    #expect(!state.coverRequired)
    let transition5 = state.beginAutomaticUnlock()
    #expect(transition5 == nil)
    let transition6 = state.requestTakeover()
    #expect(!transition6)
    #expect(state.active)
}

@Test func aManualUnlockDuringPreparationDoesNotForceAnotherSystemLock() {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()
    let transition7 = state.observeManualUnlock(sessionLocked: false, authorizationPending: false)
    #expect(transition7)
    #expect(state.phase == .manuallyUnlocked)
    #expect(state.active)
    #expect(!state.coverRequired)
    let transition8 = state.beginAutomaticUnlock()
    #expect(transition8 == nil)
    let transition9 = state.beginTask()
    #expect(!transition9, "Continue the same lease without restarting unlock")
}

@Test func manualUnlockDoesNotAcceptUnknownOrStillLockedSessionsOrAPendingAllow() {
    var state = TaskPrivacyHandoffState()
    state.beginTask()
    state.observeExternalLock()
    state.requestTakeover()
    state.completeHandoff(sessionLocked: true, authorizationReady: true)
    let transition10 = state.observeManualUnlock(sessionLocked: nil, authorizationPending: false)
    #expect(!transition10)
    let transition11 = state.observeManualUnlock(sessionLocked: true, authorizationPending: false)
    #expect(!transition11)
    let transition12 = state.observeManualUnlock(sessionLocked: false, authorizationPending: true)
    #expect(!transition12)
    let transition13 = state.observeManualUnlock(sessionLocked: false, authorizationPending: false)
    #expect(transition13)
    #expect(state.phase == .manuallyUnlocked)
    #expect(!state.coverRequired)
}
