// Keeps the Mac awake for the duration of an interface task so the task survives the
// screen locking. Locking alone never stops a process; idle system sleep does — so this
// holds a PreventUserIdleSystemSleep assertion while a task lease is active.
//
// Deliberately narrow: it does not unlock the Mac, does not keep the display awake and
// cannot stop a lid-close sleep (Apple deprecated PreventSystemSleep as unsupported).
// GUI work while locked stays refused by NativePlatform.requireUnlocked.
import Foundation
import IOKit
import IOKit.pwr_mgt

final class PowerGuard: @unchecked Sendable {
    static let shared = PowerGuard()
    /// A task that is stepped away from should stop holding power after a bounded time.
    static let defaultSeconds: TimeInterval = 1800
    static let minimumSeconds: TimeInterval = 60
    static let maximumSeconds: TimeInterval = 24 * 60 * 60
    private static let assertionName = "workbuddy-cua: interface task running"

    private let queue = DispatchQueue(label: "com.workbuddy.cua.power-guard")
    private var assertionID: IOPMAssertionID = 0
    private var expiresAtUptime: TimeInterval?
    private var wallAnchor = Date()
    private var uptimeAnchor: TimeInterval = 0
    private var sleptSeconds: TimeInterval = 0
    private var expiryTimer: DispatchSourceTimer?
    private var expiryCount = 0
    private var lastRelease: String?

    private init() {}

    /// Creates or extends the assertion. Idempotent: a second hold reuses the same ID.
    func hold(seconds: TimeInterval) throws {
        let clamped = min(max(seconds, Self.minimumSeconds), Self.maximumSeconds)
        try queue.sync {
            let now = ProcessInfo.processInfo.systemUptime
            if assertionID == 0 {
                var identifier: IOPMAssertionID = 0
                let result = IOPMAssertionCreateWithName(
                    kIOPMAssertPreventUserIdleSystemSleep as CFString,
                    IOPMAssertionLevel(kIOPMAssertionLevelOn),
                    Self.assertionName as CFString,
                    &identifier)
                guard result == kIOReturnSuccess, identifier != 0 else {
                    throw NativeFailure(code: "POWER_ASSERTION_FAILED",
                                        message: "无法创建防睡眠断言（IOReturn \(result)）")
                }
                assertionID = identifier
                wallAnchor = Date()
                uptimeAnchor = now
                sleptSeconds = 0
            } else {
                accumulateSleep(now: now)
            }
            expiresAtUptime = now + clamped
            lastRelease = nil
            scheduleExpiry(seconds: clamped)
        }
    }

    /// Releases the assertion immediately. Safe to call when nothing is held.
    @discardableResult
    func release(reason: String = "requested") -> JSONValue {
        queue.sync {
            let now = ProcessInfo.processInfo.systemUptime
            if assertionID != 0 { accumulateSleep(now: now) }
            releaseLocked(reason: reason)
            return snapshot(now: now)
        }
    }

    func status() -> JSONValue {
        queue.sync {
            let now = ProcessInfo.processInfo.systemUptime
            if assertionID != 0 { accumulateSleep(now: now) }
            return snapshot(now: now)
        }
    }

    // MARK: - queue-isolated state

    private func snapshot(now: TimeInterval) -> JSONValue {
        let remaining = expiresAtUptime.map { max(0, $0 - now) } ?? 0
        return .object([
            "holding": .bool(assertionID != 0 && remaining > 0),
            "assertionType": .string("PreventUserIdleSystemSleep"),
            "remainingSeconds": .number(remaining.rounded()),
            "maximumSeconds": .number(Self.maximumSeconds),
            "expiredCount": .number(Double(expiryCount)),
            "sleptWhileHoldingSeconds": .number(sleptSeconds.rounded()),
            "lastRelease": lastRelease.map(JSONValue.string) ?? .null,
            "limits": .object([
                "idleSystemSleep": .string("prevented"),
                "displaySleep": .string("allowed"),
                "lidCloseSleep": .string("not prevented"),
                "unlock": .string("not performed"),
                "guiWhileLocked": .string("still refused"),
            ]),
        ])
    }

    private func releaseLocked(reason: String) {
        expiryTimer?.cancel()
        expiryTimer = nil
        expiresAtUptime = nil
        lastRelease = reason
        guard assertionID != 0 else { return }
        IOPMAssertionRelease(assertionID)
        assertionID = 0
    }

    /// `systemUptime` does not advance while the machine is asleep, so a wall-clock gap
    /// wider than the uptime gap means the machine slept despite the assertion
    /// (lid closed, or a forced sleep). Recorded instead of silently claiming success.
    private func accumulateSleep(now: TimeInterval) {
        let wallElapsed = Date().timeIntervalSince(wallAnchor)
        let uptimeElapsed = now - uptimeAnchor
        if wallElapsed > uptimeElapsed + 5 { sleptSeconds += wallElapsed - uptimeElapsed }
        wallAnchor = Date()
        uptimeAnchor = now
    }

    private func scheduleExpiry(seconds: TimeInterval) {
        expiryTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + seconds)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let now = ProcessInfo.processInfo.systemUptime
            guard let deadline = self.expiresAtUptime, now >= deadline else { return }
            self.accumulateSleep(now: now)
            self.expiryCount += 1
            self.releaseLocked(reason: "expired")
        }
        timer.resume()
        expiryTimer = timer
    }
}
