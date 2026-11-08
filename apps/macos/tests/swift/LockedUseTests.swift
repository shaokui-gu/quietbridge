import Foundation
import Testing
@testable import QuietBridgeNative

@Test func lockedUseGrantIsSingleUseAndShortLived() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(state.consumeGrant(sessionID: 42))
    #expect(!state.consumeGrant(sessionID: 42))
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42), "one lock episode only creates one decision")
}

@Test func manualAuthenticationSuppressesUnlockGrantsWithoutEndingTheTask() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.suppressForManualAuthentication()
    #expect(state.manualAuthenticationRequired)
    #expect(state.status["taskActive"] as? Bool == true)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(!state.consumeGrant(sessionID: 42))
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
    state.noteSessionLocked()
    #expect(state.canCompleteManualHandoff)
    state.noteSessionUnlocked()
    #expect(!state.manualAuthenticationRequired)
    #expect(state.status["taskActive"] as? Bool == true)
    #expect(state.status["lastUnlockMethod"] as? String == "manual")
    state.endTask() // Expire only the unlock grant; the business lease is separate.
    #expect(state.status["taskActive"] as? Bool == false)
    #expect(!state.manualAuthenticationRequired)
    #expect(state.status["lastUnlockMethod"] as? String == "manual")
    try NativePlatform.requireUnlocked(false)
    // Ordinary interface access depends on macOS, not the grant's suppression.
    try NativePlatform.requireUnlocked(false)
}

@Test func endingTaskInvalidatesPendingGrant() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    state.endTask()
    #expect(!state.consumeGrant(sessionID: 42))
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func manualUnlockRestoresTheSameTaskWithoutAnExplicitEnd() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.suppressForManualAuthentication()
    state.observeSession(true)
    state.observeSession(false)
    #expect(!state.manualAuthenticationRequired)
    #expect(state.status["taskActive"] as? Bool == true)
    #expect(state.status["boundSessionID"] as? Int == 42)
    try NativePlatform.requireUnlocked(false)
    // Completing manual authentication does not mint a new automatic grant.
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func explicitTaskReleasePreservesAuthenticationAndAnUnsettledAllow() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(state.consumeGrant(sessionID: 42))
    state.suppressForManualAuthentication()
    state.releaseTask()
    #expect(state.manualAuthenticationRequired)
    #expect(state.automaticUnlockPending)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
    state.noteSessionLocked()
    #expect(!state.canCompleteManualHandoff)
    state.noteSessionUnlocked()
    state.noteSessionUnlocked()
    #expect(!state.automaticUnlockPending)
    #expect(state.manualAuthenticationRequired)
    #expect(state.status["lastUnlockMethod"] as? String == "automatic")
    // Suppression still denies automatic unlock, but never freezes window tools.
    try NativePlatform.requireUnlocked(false)
    state.noteSessionLocked()
    #expect(state.canCompleteManualHandoff)
    state.noteSessionUnlocked()
    #expect(!state.manualAuthenticationRequired)
    #expect(state.status["lastUnlockMethod"] as? String == "manual")
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func inactiveTaskCannotArmGrant() {
    let state = LockedUseAuthorizationState()
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.endTask()
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
}

@Test func replacingAnUnlockLeaseDoesNotBypassManualAuthentication() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.suppressForManualAuthentication()
    state.endTask()
    state.beginTask(sessionID: 42)
    #expect(state.manualAuthenticationRequired)
    state.renewProtection(ready: true)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    state.noteSessionLocked()
    state.noteSessionUnlocked()
    #expect(!state.manualAuthenticationRequired)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    try state.requireAutomaticUnlockAllowed()
}

@Test func invalidGrantDurationIsRejected() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(!state.armExternalLock(seconds: .nan, sessionID: 42))
    #expect(!state.armExternalLock(seconds: .infinity, sessionID: 42))
    #expect(!state.armExternalLock(seconds: -1, sessionID: 42))
}

@Test func grantRequiresLiveProtectionAndRevocationInvalidatesIt() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    state.renewProtection(ready: false)
    #expect(!state.consumeGrant(sessionID: 42))
}

@Test func unconfirmedLockDoesNotClearManualSuppression() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.suppressForManualAuthentication()
    state.noteSessionUnlocked()
    #expect(state.manualAuthenticationRequired)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
}

@Test func consumedAutomaticGrantCannotProveManualAuthentication() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(state.consumeGrant(sessionID: 42))
    #expect(state.automaticUnlockPending)
    #expect(state.status["automaticUnlockPending"] as? Bool == true)
    state.suppressForManualAuthentication()
    state.endTask()
    #expect(state.automaticUnlockPending, "Ending the task cannot recall an Allow already held by authd")
    state.noteSessionLocked()
    state.noteSessionLocked()
    #expect(!state.canCompleteManualHandoff, "The old lock may still receive the already-consumed Allow")
    state.noteSessionUnlocked()
    #expect(!state.automaticUnlockPending)
    #expect(!state.canCompleteManualHandoff)
    #expect(state.manualAuthenticationRequired)
    state.noteSessionUnlocked()
    state.noteSessionUnlocked()
    #expect(state.manualAuthenticationRequired)
    #expect(!state.canCompleteManualHandoff)
    state.noteSessionLocked()
    #expect(state.canCompleteManualHandoff, "A fresh lock after the automatic unlock can complete handoff")
    state.noteSessionUnlocked()
    #expect(!state.manualAuthenticationRequired)
    #expect(state.status["lastUnlockMethod"] as? String == "manual")
}

@Test func automaticUnlockEntryRequiresAnActiveProtectedUnconsumedGrant() throws {
    let state = LockedUseAuthorizationState()
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
    state.beginTask(sessionID: 42)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
    state.renewProtection(ready: true)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    try state.requireAutomaticUnlockAllowed()
    #expect(state.consumeGrant(sessionID: 42))
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func automaticUnlockEntryStopsImmediatelyWhenProtectionIsRevoked() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    try state.requireAutomaticUnlockAllowed()
    state.renewProtection(ready: false)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
    state.renewProtection(ready: true)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func automaticUnlockEntryRejectsAnExpiredProtectionLease() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    try state.requireAutomaticUnlockAllowed()
    state.renewProtection(ready: true, validUntil: ProcessInfo.processInfo.systemUptime - 1)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func automaticUnlockEntryRejectsAnExpiredGrant() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 0.01, sessionID: 42))
    Thread.sleep(forTimeInterval: 0.03)
    #expect(throws: NativeFailure.self) { try state.requireAutomaticUnlockAllowed() }
}

@Test func grantIsBoundToTheVerifiedGraphicalSession() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 99))
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(!state.consumeGrant(sessionID: 99))
    #expect(!state.consumeGrant(sessionID: 42), "Mismatch invalidates the pending grant")
}

@Test func unverifiedGraphicalSessionCannotAuthorize() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: nil)
    state.renewProtection(ready: true)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
}

@Test func securityAgentPeerRequiresSameAuditUserAndGraphicalSession() {
    #expect(LockedUsePeerScope.allows(effectiveUID: 92, auditUID: 501, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(!LockedUsePeerScope.allows(effectiveUID: 92, auditUID: 502, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(!LockedUsePeerScope.allows(effectiveUID: 92, auditUID: 501, auditSession: 99,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(LockedUsePeerScope.allows(effectiveUID: 0, auditUID: 501, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
}

@Test func unsetAuditUserStillRequiresKernelUIDAndSessionScope() {
    #expect(LockedUsePeerScope.allows(effectiveUID: 501, auditUID: .max, auditSession: 99,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(LockedUsePeerScope.allows(effectiveUID: 92, auditUID: .max, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(!LockedUsePeerScope.allows(effectiveUID: 92, auditUID: .max, auditSession: 99,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(!LockedUsePeerScope.allows(effectiveUID: 502, auditUID: .max, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
}

@Test func privilegedPeerRequiresConsoleAuditScope() {
    #expect(!LockedUsePeerScope.allows(effectiveUID: 0, auditUID: 501, auditSession: 99,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(!LockedUsePeerScope.allows(effectiveUID: 0, auditUID: 502, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
    #expect(LockedUsePeerScope.allows(effectiveUID: 0, auditUID: .max, auditSession: 42,
        consoleUID: 501, consoleSession: 42, securityAgentUID: 92))
}

@Test func unknownSessionObservationsCannotClearManualAuthenticationOrPermitInput() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.suppressForManualAuthentication()
    state.observeSession(nil)
    state.observeSession(false)
    #expect(state.manualAuthenticationRequired)
    #expect(throws: NativeFailure.self) { try NativePlatform.requireUnlocked(nil) }
    #expect(throws: NativeFailure.self) { try NativePlatform.requireUnlocked(true) }
    state.observeSession(true)
    state.observeSession(nil)
    #expect(state.manualAuthenticationRequired)
    state.observeSession(false)
    #expect(!state.manualAuthenticationRequired)
}

@Test func repeatedUnlockedPollingPreservesTheUnlockOrigin() throws {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.observeSession(true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(state.consumeGrant(sessionID: 42))
    for _ in 0..<4 { state.observeSession(false) }
    #expect(state.status["lastUnlockMethod"] as? String == "automatic")
    state.observeSession(true)
    #expect(state.status["lastUnlockMethod"] as? String == nil)
    for _ in 0..<4 { state.observeSession(false) }
    #expect(state.status["lastUnlockMethod"] as? String == "manual")
}
