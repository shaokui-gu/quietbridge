import AppKit
import Darwin
import Foundation
import Testing
@testable import QuietBridgeNative

@Suite struct ApplicationExecutableIdentityTests {
    private let root = URL(fileURLWithPath: "/private/tmp/identity-fixture/X/com.google.Chrome.code_sign_clone")
    private var clone: String { root.appendingPathComponent("code_sign_clone.Abc123/Google Chrome.app.bundle/Contents/MacOS/Google Chrome").path }
    private let identity = ProcessIdentity(pid: 123, startedSeconds: 1, startedMicroseconds: 2)

    @Test func verifiedCloneKeepsCanonicalConsentAndChecksGenerationAroundSignature() throws {
        var calls: [String] = []
        let path = try ApplicationExecutableIdentity.resolve(identity, observedPath: clone,
            expectedPath: BrowserSessionPlan.executablePath, bundleIdentifier: "com.google.Chrome",
            bundleURL: URL(fileURLWithPath: BrowserSessionPlan.applicationPath), cloneRoot: root,
            verifyProcess: { value in #expect(value == identity); calls.append("generation") },
            verifySignature: { value in #expect(value == identity); calls.append("signature") })
        #expect(path == BrowserSessionPlan.executablePath)
        #expect(calls == ["generation", "signature", "generation"])
    }

    @Test(arguments: ["arbitraryPath", "siblingRoot", "helper", "bundleID", "bundlePath", "expectedPath", "missingRoot"])
    func lookalikeOrWrongTargetsAreRejectedBeforeSignature(_ mode: String) {
        var path = clone
        var bundleID = "com.google.Chrome"
        var bundlePath = BrowserSessionPlan.applicationPath
        var expected = BrowserSessionPlan.executablePath
        if mode == "arbitraryPath" { path = "/tmp/Google Chrome.app/Contents/MacOS/Google Chrome" }
        if mode == "siblingRoot" { path = root.path + "-other/code_sign_clone.Abc123/Google Chrome.app.bundle/Contents/MacOS/Google Chrome" }
        if mode == "helper" { path += " Helper" }
        if mode == "bundleID" { bundleID = "com.other.App" }
        if mode == "bundlePath" { bundlePath = "/Applications/Other.app" }
        if mode == "expectedPath" { expected = "/Applications/Other.app/Contents/MacOS/Other" }
        #expect(throws: NativeFailure.self) {
            try ApplicationExecutableIdentity.resolve(identity, observedPath: path, expectedPath: expected,
                bundleIdentifier: bundleID, bundleURL: URL(fileURLWithPath: bundlePath),
                cloneRoot: mode == "missingRoot" ? nil : root,
                verifyProcess: { _ in Issue.record("wrong target reached process validation") },
                verifySignature: { _ in Issue.record("wrong target reached signature validation") })
        }
    }

    @Test(arguments: ["signature", "before", "after"])
    func signatureFailureOrProcessReplacementRejectsClone(_ failure: String) {
        var checks = 0, signatures = 0
        #expect(throws: NativeFailure.self) {
            try ApplicationExecutableIdentity.resolve(identity, observedPath: clone,
                expectedPath: BrowserSessionPlan.executablePath, bundleIdentifier: "com.google.Chrome",
                bundleURL: URL(fileURLWithPath: BrowserSessionPlan.applicationPath), cloneRoot: root,
                verifyProcess: { _ in
                    checks += 1
                    if (failure == "before" && checks == 1) || (failure == "after" && checks == 2) {
                        throw NativeFailure(code: "STALE_APP", message: "PID reused")
                    }
                }, verifySignature: { _ in
                    signatures += 1
                    if failure == "signature" { throw ApplicationExecutableIdentity.mismatch }
                })
        }
        #expect(signatures == (failure == "before" ? 0 : 1))
        #expect(checks == (failure == "after" ? 2 : 1))
    }

    @Test func ordinaryExactPathsPreserveExistingUnsignedApplicationSupport() throws {
        #expect(try ApplicationExecutableIdentity.resolve(identity, observedPath: "/bin/cat", expectedPath: "/bin/cat",
            bundleIdentifier: nil, bundleURL: nil,
            verifyProcess: { _ in Issue.record("caller already checks generation for exact paths") },
            verifySignature: { _ in Issue.record("ordinary applications do not require Google signing") }) == "/bin/cat")
    }

    @Test func realNonChromeProcessCannotPassGoogleRequirement() throws {
        let current = try NativePlatform.identity(getpid())
        #expect(throws: NativeFailure.self) { try ApplicationExecutableIdentity.verifyChromeSignature(current) }
    }

    @Test func cloneAuthorizationMatchesInstalledConsentWithoutPersistingClonePath() throws {
        let current = try NativePlatform.identity(getpid())
        let actualRoot = try #require(ApplicationExecutableIdentity.systemChromeCloneRoot())
        let fakeClone = actualRoot.appendingPathComponent("code_sign_clone.Test123/Google Chrome.app.bundle/Contents/MacOS/Google Chrome").path
        let bundle: (Int32) -> (identifier: String?, url: URL?) = { _ in
            ("com.google.Chrome", URL(fileURLWithPath: BrowserSessionPlan.applicationPath))
        }
        let normal = try ApplicationAuthorization.describe(current, name: "Chrome", expectedExecutablePath: BrowserSessionPlan.executablePath,
            readBundle: bundle, readPath: { _ in .success(BrowserSessionPlan.executablePath) })
        var signatures = 0
        let cloned = try ApplicationAuthorization.describe(current, name: "Chrome", expectedExecutablePath: BrowserSessionPlan.executablePath,
            readBundle: bundle, readPath: { _ in .success(fakeClone) }, verifyCloneSignature: { _ in signatures += 1 })
        #expect(cloned == normal)
        #expect(signatures == 1)
        #expect(cloned["executablePath"] == .string(BrowserSessionPlan.executablePath))
    }

    // Explicit opt-in, read-only integration probe. Never launches Chrome or performs UI actions.
    @Test(.enabled(if: ProcessInfo.processInfo.environment["QUIETBRIDGE_CHROME_IDENTITY_PROBE_PID"] != nil))
    @MainActor func installedChromeLiveIdentityProbe() throws {
        let raw = try #require(ProcessInfo.processInfo.environment["QUIETBRIDGE_CHROME_IDENTITY_PROBE_PID"])
        let pid = try #require(Int32(raw))
        let current = try NativePlatform.identity(pid)
        let bundle = ApplicationAuthorization.runningBundle(pid)
        let observed = try NativePlatform.executablePathResult(pid).get()
        #expect(bundle.identifier == "com.google.Chrome")
        #expect(observed != BrowserSessionPlan.executablePath)
        #expect(ApplicationExecutableIdentity.isChromeClonePath(URL(fileURLWithPath: observed).resolvingSymlinksInPath().path,
            root: ApplicationExecutableIdentity.systemChromeCloneRoot()))
        let authorization = try ApplicationAuthorization.describe(current, name: "Google Chrome", expectedExecutablePath: BrowserSessionPlan.executablePath)
        #expect(authorization["executablePath"] == .string(BrowserSessionPlan.executablePath))
        try NativePlatform.verify(current)
    }
}
