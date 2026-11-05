// Exercise the lock handoff without creating windows, posting input, or locking macOS.
import Foundation
import Testing
@testable import CuaNative

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
    #expect(state.pausedByUser)
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
    #expect(state.phase == .paused)
    #expect(state.pausedByUser)
    #expect(!state.active)
    #expect(!state.coverRequired)
    let pausedTaskRestarted = state.beginTask()
    let pausedLockObserved = state.observeExternalLock()
    let pausedUnlock = state.beginAutomaticUnlock()
    let laterHandoffCompleted = state.completeHandoff(sessionLocked: false, authorizationReady: true)
    #expect(!pausedTaskRestarted, "A lease renewal after the user takes over must not cover their desktop again")
    #expect(!pausedLockObserved)
    #expect(pausedUnlock == nil)
    #expect(!laterHandoffCompleted)
    #expect(state.phase == .paused, "A later manual unlock must leave control with the user")
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

@Test func onlyAnExplicitTaskEndAllowsAUserPausedLifecycleToStartAgain() throws {
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
    #expect(!state.pausedByUser)
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
