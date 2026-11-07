// The guard must be observable in the system power state, not just in our own bookkeeping.
import Foundation
import Testing
@testable import CuaNative

@Suite(.serialized)
struct PowerGuardTests {
    private func systemAssertions() throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
        process.arguments = ["-g", "assertions"]
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        // An installed CUA service may legitimately hold its own assertion.
        // Verify only assertions created by this isolated test process.
        let prefix = "pid \(ProcessInfo.processInfo.processIdentifier)("
        return String(decoding: data, as: UTF8.self).split(separator: "\n")
            .filter { $0.trimmingCharacters(in: .whitespaces).hasPrefix(prefix) }
            .joined(separator: "\n")
    }

    @Test func holdsAndReleasesVisiblyInSystemPowerState() throws {
        let power = PowerGuard.shared
        defer { power.release(reason: "test-cleanup") }
        try power.hold(seconds: 120)

        let held = try systemAssertions()
        #expect(held.contains("PreventUserIdleSystemSleep"))
        #expect(held.contains("workbuddy-cua: interface task running"))

        let heldStatus = power.status()
        #expect(heldStatus["holding"] == .bool(true))
        #expect(heldStatus["limits"]["unlock"] == .string("not performed"))

        power.release(reason: "test")

        let released = try systemAssertions()
        #expect(!released.contains("workbuddy-cua: interface task running"))
        #expect(power.status()["holding"] == .bool(false))
    }

    @Test func holdIsIdempotentAndRenewsDeadline() throws {
        let power = PowerGuard.shared
        defer { power.release(reason: "test-cleanup") }
        try power.hold(seconds: 120)
        try power.hold(seconds: 300)
        let remaining = power.status()["remainingSeconds"].number ?? 0
        #expect(remaining > 290 && remaining <= 300)
    }

    @Test func durationIsClampedToSupportedRange() throws {
        let power = PowerGuard.shared
        defer { power.release(reason: "test-cleanup") }
        try power.hold(seconds: 1)
        let remaining = power.status()["remainingSeconds"].number ?? 0
        #expect(remaining > PowerGuard.minimumSeconds - 5 && remaining <= PowerGuard.minimumSeconds)
    }

    @Test func guardNeverClaimsToUnlockOrKeepDisplayOn() throws {
        let power = PowerGuard.shared
        defer { power.release(reason: "test-cleanup") }
        try power.hold(seconds: 120)
        let limits = power.status()["limits"]
        #expect(limits["idleSystemSleep"] == .string("prevented"))
        #expect(limits["displaySleep"] == .string("allowed"))
        #expect(limits["lidCloseSleep"] == .string("not prevented"))
        #expect(limits["guiWhileLocked"] == .string("still refused"))
    }
}
