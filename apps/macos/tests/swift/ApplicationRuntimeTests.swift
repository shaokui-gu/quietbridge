import Foundation
import Darwin
import Testing
@testable import QuietBridgeNative

@Suite struct ApplicationRuntimeTests {
    private func directory() -> String { "/private/tmp/quietbridge-app-\(UUID().uuidString)" }

    @Test func exitRevokesWorkersEvenWhenTheLockFileRemains() throws {
        let path = directory()
        defer { try? FileManager.default.removeItem(atPath: path) }
        let owner = try ApplicationRuntimeOwner(directory: path)
        let session = try #require(ApplicationSession.current(in: path))
        let worker = CancellationLatch(applicationSession: session)
        try worker.check()
        owner.stop()
        #expect(FileManager.default.fileExists(atPath: path + "/application.lock"))
        #expect(ApplicationSession.current(in: path) == nil)
        do { try worker.check(); Issue.record("Worker survived application exit") }
        catch let error as NativeFailure { #expect(error.code == "APP_NOT_RUNNING") }
    }

    @Test func restartingTheAppCannotResumeAnOldWorker() throws {
        let path = directory()
        defer { try? FileManager.default.removeItem(atPath: path) }
        var first: ApplicationRuntimeOwner? = try ApplicationRuntimeOwner(directory: path)
        let old = try #require(ApplicationSession.current(in: path))
        first = nil // Process death releases the same kernel lock automatically.
        let second = try ApplicationRuntimeOwner(directory: path)
        defer { second.stop() }
        #expect(old != second.session)
        #expect(!old.isActive)
        #expect(second.session.isActive)
        withExtendedLifetime(first) {}
    }

    @Test func onlyOneApplicationCanOwnTheRuntime() throws {
        let path = directory()
        defer { try? FileManager.default.removeItem(atPath: path) }
        let owner = try ApplicationRuntimeOwner(directory: path)
        defer { owner.stop() }
        #expect(throws: NativeFailure.self) { try ApplicationRuntimeOwner(directory: path) }
        #expect(owner.session.isActive)
    }

    @Test func versionedSessionPreventsLegacyHelpersFromJoining() throws {
        let path = directory()
        defer { try? FileManager.default.removeItem(atPath: path) }
        let owner = try ApplicationRuntimeOwner(directory: path)
        defer { owner.stop() }
        let data = try Data(contentsOf: URL(fileURLWithPath: path + "/application.lock"))
        #expect(data.count != 36) // Legacy helpers require exactly a bare UUID.
        #expect(UUID(uuidString: String(decoding: data, as: UTF8.self)) == nil)
        #expect(ApplicationSession.current(in: path) == owner.session)
        try Data(owner.session.id.utf8).write(to: URL(fileURLWithPath: path + "/application.lock"))
        #expect(ApplicationSession.current(in: path) == nil)
    }

    @Test func untrustedRuntimePathsCannotGrantCapabilities() throws {
        let path = directory()
        let owner = try ApplicationRuntimeOwner(directory: path)
        defer { owner.stop(); try? FileManager.default.removeItem(atPath: path) }
        #expect(chmod(path + "/application.lock", 0o644) == 0)
        #expect(ApplicationSession.current(in: path) == nil)
        #expect(chmod(path + "/application.lock", 0o600) == 0)
        let alias = path + "-link"
        #expect(symlink(path, alias) == 0)
        defer { unlink(alias) }
        #expect(ApplicationSession.current(in: alias) == nil)
    }
}
