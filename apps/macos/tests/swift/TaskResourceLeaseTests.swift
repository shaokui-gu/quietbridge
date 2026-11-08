import Foundation
import Testing
@testable import QuietBridgeNative

@Suite @MainActor struct TaskResourceLeaseTests {
    @Test func timerRevokesIdleResourcesWithoutAnotherRequest() async throws {
        var clock = 0.0
        var released: Set<TaskResourceLease.Resource> = []
        let lease = TaskResourceLease(now: { clock }) { _, resources, _ in released = resources }
        let owner = UUID()
        try lease.acquire(owner, resource: .preview)
        try lease.acquire(owner, resource: .protection)
        clock = 121
        try await Task.sleep(for: .milliseconds(900))
        #expect(released == [.preview, .protection])
        #expect(lease.owner == nil)
    }

    @Test func disconnectReleasesOnlyTheOwningSocket() throws {
        let owner = UUID(), other = UUID()
        var released: [(UUID, Set<TaskResourceLease.Resource>, String)] = []
        let lease = TaskResourceLease { released.append(($0, $1, $2)) }
        try lease.acquire(owner, resource: .preview)
        try lease.acquire(owner, resource: .protection)
        lease.disconnect(other)
        #expect(released.isEmpty)
        #expect(!lease.release(other, resource: .preview))
        try lease.acquire(other, resource: .preview)
        #expect(lease.previewCount == 2)
        lease.release(other, resource: .preview)
        lease.disconnect(owner)
        #expect(released.count == 1)
        #expect(released.first?.0 == owner)
        #expect(released.first?.1 == [.preview, .protection])
        #expect(released.first?.2 == "client-disconnected")
        #expect(lease.owner == nil)
        lease.disconnect(owner)
        #expect(released.count == 1)
    }

    @Test func threePreviewsHaveIndependentDeadlinesAndFourthCannotTakeOver() throws {
        var clock = 0.0
        var revoked: [UUID] = []
        let lease = TaskResourceLease(now: { clock }) { id, _, _ in revoked.append(id) }
        let clients = (0..<4).map { _ in UUID() }
        for id in clients.prefix(3) { try lease.acquire(id, resource: .preview) }
        #expect(lease.previewCount == 3)
        do { try lease.acquire(clients[3], resource: .preview); Issue.record("Fourth stream admitted") }
        catch let error as NativeFailure { #expect(error.code == "SESSION_CAPACITY_FULL") }
        clock = 60
        lease.touch(clients[1]); lease.touch(clients[2])
        clock = 121
        lease.expireIfNeeded()
        #expect(revoked == [clients[0]])
        #expect(lease.previewCount == 2)
        try lease.require(clients[1]); try lease.require(clients[2])
        try lease.acquire(clients[3], resource: .preview)
        #expect(lease.previewCount == 3)
        for id in clients { lease.disconnect(id) }
    }

    @Test func expiryCannotBeResurrectedByLateInputOrRenewal() throws {
        var clock = 0.0
        var reasons: [String] = []
        let lease = TaskResourceLease(now: { clock }) { _, _, reason in reasons.append(reason) }
        let old = UUID(), fresh = UUID()
        try lease.acquire(old, resource: .preview)
        clock = 120
        lease.touch(old)
        #expect(reasons == ["idle-timeout"])
        #expect(lease.owner == nil)
        do { try lease.acquire(old, resource: .preview); Issue.record("Revoked socket resumed") }
        catch let error as NativeFailure { #expect(error.code == "TASK_INTERRUPTED") }
        try lease.acquire(fresh, resource: .preview)
        lease.disconnect(old)
        #expect(lease.owner == fresh)
        lease.disconnect(fresh)
    }

    @Test func activityCanSustainAThirtyMinuteTask() throws {
        var clock = 0.0
        var count = 0
        let lease = TaskResourceLease(now: { clock }) { _, _, _ in count += 1 }
        let owner = UUID(), unrelated = UUID()
        try lease.acquire(owner, resource: .protection)
        for _ in 0..<40 {
            clock += 60
            try lease.require(owner)
            lease.touch(owner)
            lease.touch(unrelated)
        }
        #expect(clock > 1800)
        #expect(count == 0)
        clock += 120
        lease.expireIfNeeded()
        #expect(count == 1)
    }

    @Test func unrelatedActivityDoesNotKeepAnIdleTaskAlive() throws {
        var clock = 0.0
        var revoked = false
        let lease = TaskResourceLease(now: { clock }) { _, _, _ in revoked = true }
        let owner = UUID()
        try lease.acquire(owner, resource: .preview)
        clock = 119
        lease.touch(UUID())
        try lease.require(owner) // A check alone is not renewal.
        clock = 120
        lease.expireIfNeeded()
        #expect(revoked)
    }

    @Test func explicitReleaseKeepsOtherResourcesUntilTheirOwnerEnds() throws {
        var released: Set<TaskResourceLease.Resource> = []
        let lease = TaskResourceLease { _, held, _ in released = held }
        let owner = UUID()
        try lease.acquire(owner, resource: .preview)
        try lease.acquire(owner, resource: .protection)
        #expect(lease.release(owner, resource: .preview))
        #expect(lease.owner == owner)
        lease.disconnect(owner)
        #expect(released == [.protection])
        #expect(lease.owner == nil)
    }

    @Test func renewalDoesNotAcquireAbsentResources() throws {
        let lease = TaskResourceLease { _, _, _ in Issue.record("No resources should be held") }
        let client = UUID()
        try lease.require(client)
        lease.touch(client)
        #expect(lease.owner == nil)
        lease.disconnect(client)
    }

    @Test func protectionAndPreviewAreIndependentlyOwned() throws {
        let a = UUID(), b = UUID()
        let lease = TaskResourceLease { _, _, _ in }
        try lease.acquire(a, resource: .preview)
        try lease.acquire(a, resource: .protection)
        try lease.acquire(b, resource: .protection)
        #expect(lease.protectionCount == 2)
        try lease.acquire(b, resource: .preview)
        #expect(lease.previewCount == 2)
        #expect(lease.release(a, resource: .protection))
        #expect(lease.protectionCount == 1)
        #expect(!lease.release(a, resource: .protection))
        #expect(lease.protectionCount == 1)
        lease.disconnect(a)
        #expect(lease.protectionCount == 1)
        #expect(lease.owner == b)
        #expect(lease.release(b, resource: .protection))
        #expect(lease.protectionCount == 0)
    }

    @Test func disconnectAndExpiryPreserveTheOtherParticipant() throws {
        var clock = 0.0
        var remaining: [Int] = []
        var lease: TaskResourceLease!
        lease = TaskResourceLease(now: { clock }) { _, resources, _ in
            if resources.contains(.protection) { remaining.append(lease.protectionCount) }
        }
        let a = UUID(), b = UUID(), c = UUID()
        try lease.acquire(a, resource: .protection)
        try lease.acquire(b, resource: .protection)
        lease.disconnect(a)
        #expect(remaining == [1])
        try lease.acquire(c, resource: .protection)
        clock = 60
        try lease.acquire(b, resource: .protection)
        clock = 120
        lease.expireIfNeeded()
        #expect(remaining == [1, 1])
        #expect(lease.protectionCount == 1)
        clock = 180
        lease.expireIfNeeded()
        #expect(remaining == [1, 1, 0])
        lease = nil
    }

    @Test func simultaneousExpiryReleasesGlobalProtectionOnlyOnce() throws {
        var clock = 0.0
        var finalReleases = 0
        var lease: TaskResourceLease!
        lease = TaskResourceLease(now: { clock }) { _, resources, _ in
            if resources.contains(.protection), lease.protectionCount == 0 { finalReleases += 1 }
        }
        for _ in 0..<4 { try lease.acquire(UUID(), resource: .protection) }
        clock = 120
        lease.expireIfNeeded()
        lease.expireIfNeeded()
        #expect(finalReleases == 1)
        #expect(lease.protectionCount == 0)
        lease = nil
    }

    @Test func protectionRenewalsCannotKeepAnIdlePreviewAlive() throws {
        var clock = 0.0
        var revoked: [Set<TaskResourceLease.Resource>] = []
        let lease = TaskResourceLease(now: { clock }) { _, resources, _ in revoked.append(resources) }
        let client = UUID()
        try lease.acquire(client, resource: .preview)
        try lease.acquire(client, resource: .protection)
        for instant in [30.0, 60, 90, 120, 150] {
            clock = instant
            try lease.acquire(client, resource: .protection)
        }
        #expect(revoked == [[.preview]])
        #expect(lease.protectionCount == 1)
        // Expiring just the preview does not revoke the still-live connection.
        try lease.acquire(client, resource: .preview)
        lease.disconnect(client)
    }
}
