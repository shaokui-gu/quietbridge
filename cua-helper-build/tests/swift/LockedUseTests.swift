import Foundation
import Testing
@testable import CuaNative

@Test func lockedUseGrantIsSingleUseAndShortLived() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(state.consumeGrant(sessionID: 42))
    #expect(!state.consumeGrant(sessionID: 42))
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42), "one lock episode only creates one decision")
}

@Test func manualAuthenticationSuppressesAutomaticUnlockUntilSessionReturns() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.suppressForManualAuthentication()
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    #expect(!state.consumeGrant(sessionID: 42))
    state.noteSessionLocked()
    state.noteSessionUnlocked()
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
}

@Test func endingTaskInvalidatesPendingGrant() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
    state.endTask()
    #expect(!state.consumeGrant(sessionID: 42))
}

@Test func inactiveTaskCannotArmGrant() {
    let state = LockedUseAuthorizationState()
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.endTask()
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
}

@Test func replacingTaskDoesNotClearManualSuppression() {
    let state = LockedUseAuthorizationState()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    state.suppressForManualAuthentication()
    state.endTask()
    state.beginTask(sessionID: 42)
    state.renewProtection(ready: true)
    #expect(!state.armExternalLock(seconds: 5, sessionID: 42))
    state.noteSessionLocked()
    state.noteSessionUnlocked()
    #expect(state.armExternalLock(seconds: 5, sessionID: 42))
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
    state.suppressForManualAuthentication()
    state.noteSessionLocked()
    state.noteSessionUnlocked()
    #expect(state.manualAuthenticationRequired)
    state.noteSessionUnlocked()
    #expect(state.manualAuthenticationRequired)
    state.noteSessionLocked()
    state.noteSessionUnlocked()
    #expect(!state.manualAuthenticationRequired)
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
