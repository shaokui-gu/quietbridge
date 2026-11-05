// Pure lifecycle shared by the controller and its desktop-free regression tests.
import Foundation

struct TaskPrivacyHandoffState {
    enum Phase: String {
        case inactive, watching
        case preparingUnlock = "preparing-unlock"
        case unlocking, shielded
        case handingBack = "handing-back"
        case paused
    }

    private(set) var phase: Phase = .inactive
    private(set) var automaticUnlockAttempted = false
    private var generation = UUID()

    var active: Bool { phase != .inactive && phase != .paused }
    var pausedByUser: Bool { phase == .handingBack || phase == .paused }
    var coverRequired: Bool {
        [.preparingUnlock, .unlocking, .shielded, .handingBack].contains(phase)
    }

    @discardableResult
    mutating func beginTask() -> Bool {
        guard phase == .inactive else { return false }
        phase = .watching
        automaticUnlockAttempted = false
        generation = UUID()
        return true
    }

    @discardableResult
    mutating func observeExternalLock() -> Bool {
        guard phase == .watching else { return false }
        phase = .preparingUnlock
        return true
    }

    mutating func beginAutomaticUnlock() -> UUID? {
        guard phase == .preparingUnlock, !automaticUnlockAttempted else { return nil }
        automaticUnlockAttempted = true
        phase = .unlocking
        generation = UUID()
        return generation
    }

    func permitsAutomaticUnlock(_ token: UUID) -> Bool {
        phase == .unlocking && generation == token
    }

    @discardableResult
    mutating func automaticUnlockCompleted(_ token: UUID) -> Bool {
        guard permitsAutomaticUnlock(token) else { return false }
        phase = .shielded
        generation = UUID()
        return true
    }

    @discardableResult
    mutating func requestTakeover() -> Bool {
        guard coverRequired, phase != .handingBack else { return false }
        phase = .handingBack
        generation = UUID()
        return true
    }

    @discardableResult
    mutating func completeHandoff(sessionLocked: Bool?, authorizationReady: Bool) -> Bool {
        guard phase == .handingBack, sessionLocked == true, authorizationReady else { return false }
        phase = .paused
        return true
    }

    mutating func release() {
        phase = .inactive
        automaticUnlockAttempted = false
        generation = UUID()
    }
}

/// An episode owns at most one credential submission. Cancellation is checked
/// again inside every AX mutation, including after waits and tree traversal.
final class TaskPrivacyUnlockAttempt: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var submitted = false

    func cancel() { lock.withLock { cancelled = true } }

    func check() throws {
        try lock.withLock {
            if cancelled || Task.isCancelled {
                throw NativeFailure(code: "NATIVE_CANCELLED", message: "锁屏自动解锁已取消")
            }
        }
    }

    func claimSubmission() throws {
        try lock.withLock {
            guard !cancelled, !Task.isCancelled else {
                throw NativeFailure(code: "NATIVE_CANCELLED", message: "锁屏自动解锁已取消")
            }
            guard !submitted else {
                throw NativeFailure(code: "UNLOCK_ATTEMPTS_EXHAUSTED", message: "本次接管已提交过自动解锁")
            }
            submitted = true
        }
    }
}
