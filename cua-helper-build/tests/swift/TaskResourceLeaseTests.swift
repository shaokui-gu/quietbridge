import Foundation
import Testing
@testable import CuaNative

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
        #expect(throws: NativeFailure.self) { try lease.acquire(other, resource: .preview) }
        lease.disconnect(owner)
        #expect(released.count == 1)
        #expect(released.first?.0 == owner)
        #expect(released.first?.1 == [.preview, .protection])
        #expect(released.first?.2 == "client-disconnected")
        #expect(lease.owner == nil)
        lease.disconnect(owner)
        #expect(released.count == 1)
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
}
