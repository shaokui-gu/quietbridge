import Foundation
import Testing
@testable import CuaNative

@Suite struct BrowserSessionTests {
    private func parameters(_ fields: [String: JSONValue]) throws -> RequestParameters {
        let data = try JSONEncoder().encode(JSONValue.object([
            "id": .string("browser-plan"), "method": .string("browserSession"), "params": .object(fields),
        ]))
        return try RequestParameters(NativeRequest(data: data))
    }

    @Test func fixedConfigurationIsOnlyAPreparedPlan() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("cua-helper-config/browser-sessions", isDirectory: true)
        let plan = BrowserSessionPlan()
        let profile = URL(fileURLWithPath: plan.profile)
        #expect(profile.deletingLastPathComponent().path == root.path)
        #expect(UUID(uuidString: profile.lastPathComponent) == plan.sessionID)
        #expect(plan.arguments == ["--user-data-dir=\(plan.profile)", "--no-first-run", "--no-default-browser-check", "about:blank"])
        #expect(plan.json["configuration"]["applicationPath"] == .string("/Applications/Google Chrome.app"))
        #expect(plan.json["configuration"]["activates"] == .bool(false))
        #expect(plan.json["configuration"]["createsNewApplicationInstance"] == .bool(true))
        #expect(plan.json["state"] == .string("prepared"))
        #expect(plan.json["pid"] == .null)
        #expect(plan.json["processIdentity"] == .null)
        #expect(plan.json["running"] == .bool(false))
        #expect(plan.json["windows"] == .array([]))
        #expect(plan.json["dispatched"] == .bool(false))
        #expect(plan.json["launchSupported"] == .bool(true))
        #expect(plan.json["authorization"] == .null)
        #expect(plan.json["pendingConfirmation"] != .null)
        #expect(!FileManager.default.fileExists(atPath: plan.profile))
    }

    @Test @MainActor func prepareAndListOnlyRegisterUniqueConnectionLocalPlans() throws {
        let rootExisted = FileManager.default.fileExists(atPath: BrowserSessionPlan.profilesRoot.path)
        let manager = BrowserSessionManager(sessionState: { false })
        let cancellation = CancellationLatch()
        #expect(try manager.handle(action: "list", cancellation: cancellation)["sessions"] == .array([]))
        let first = try manager.handle(action: parameters(["action": .string("prepare")]).string("action"), cancellation: cancellation)
        let second = try manager.handle(action: "prepare", cancellation: cancellation)
        #expect(first["sessionId"] != second["sessionId"])
        #expect(first["profile"] != second["profile"])
        for _ in 0..<2 {
            #expect(try manager.handle(action: "list", cancellation: cancellation)["sessions"] == .array([first, second]))
        }
        let other = BrowserSessionManager(sessionState: { false })
        #expect(try other.handle(action: "list", cancellation: cancellation)["sessions"] == .array([]))
        #expect(!FileManager.default.fileExists(atPath: try #require(first["profile"].string)))
        #expect(!FileManager.default.fileExists(atPath: try #require(second["profile"].string)))
        #expect(FileManager.default.fileExists(atPath: BrowserSessionPlan.profilesRoot.path) == rootExisted)
    }

    @Test(arguments: ["profile", "applicationPath", "arguments", "sessionId", "appHandle", "activates", "createsNewApplicationInstance", "url", "space"])
    func rejectsEveryCallerControlledConfiguration(_ key: String) {
        #expect(throws: NativeFailure.self) {
            try parameters(["action": .string("prepare"), key: .string("/tmp/not-allowed")])
        }
    }

    @Test(arguments: [JSONValue.null, .bool(true), .number(1), .array([.string("prepare")]), .string("create"), .string("close"), .string("PREPARE"), .string("")])
    func rejectsInvalidActions(_ value: JSONValue) {
        #expect(throws: NativeFailure.self) { try parameters(["action": value]).string("action") }
    }

    @MainActor private final class LauncherFixture {
        var calls = 0
        var profileCalls = 0
        var completion: BrowserSessionDependencies.Completion?
        var locked: Bool? = false
        var denied = false
        var stale = false
        var wrongIdentity = false
        var prior: Set<Int32> = [77]
        let identity = ProcessIdentity(pid: 88, startedSeconds: 123, startedMicroseconds: 456)
        let authorization = JSONValue.object(["id": .string("fixture"), "persistable": .bool(true)])
        func manager(timeout: Duration = .seconds(100)) -> BrowserSessionManager {
            var dependencies = BrowserSessionDependencies()
            dependencies.installed = { InstalledApplication(path: BrowserSessionPlan.applicationPath,
                executablePath: BrowserSessionPlan.executablePath, name: "Google Chrome", bundleID: "com.google.Chrome") }
            dependencies.authorize = { _, confirm in
                guard confirm, !self.denied else { throw NativeFailure(code: "PERMISSION_DENIED", message: "拒绝") }
                return self.authorization
            }
            dependencies.describe = { _, _ in self.wrongIdentity ? .null : self.authorization }
            dependencies.identity = { pid in ProcessIdentity(pid: pid, startedSeconds: 123, startedMicroseconds: 456) }
            dependencies.verify = { _ in if self.stale { throw NativeFailure(code: "STALE_APP", message: "PID 已复用") } }
            dependencies.existingPIDs = { self.prior }
            dependencies.prepareProfile = { _ in self.profileCalls += 1 }
            dependencies.validateProfile = { _ in }
            dependencies.preview = { _, _ in }
            dependencies.windows = { [WindowInfo(windowID: 9, pid: 88, appName: "Chrome", title: "",
                bounds: WindowBounds(x: 0, y: 0, width: 100, height: 100), isOnScreen: false, onCurrentSpace: false),
                WindowInfo(windowID: 10, pid: 77, appName: "Chrome", title: "日常窗口",
                bounds: WindowBounds(x: 0, y: 0, width: 100, height: 100), isOnScreen: true, onCurrentSpace: true)] }
            dependencies.launch = { plan, completion in
                #expect(plan.arguments == ["--user-data-dir=\(plan.profile)", "--no-first-run", "--no-default-browser-check", "about:blank"])
                self.calls += 1
                self.completion = completion
            }
            dependencies.timeout = timeout
            return BrowserSessionManager(sessionState: { self.locked }, dependencies: dependencies)
        }
    }

    @Test @MainActor func launchBusinessChainIsOnceOnlyAndListVerifiesIdentityAndWindows() throws {
        let fixture = LauncherFixture()
        let manager = fixture.manager()
        let latch = CancellationLatch()
        let prepared = try manager.handle(action: "prepare", cancellation: latch)
        let id = try #require(prepared["sessionId"].string)
        let params = try parameters(["action": .string("start"), "sessionId": .string(id), "confirm": .bool(true)])
        let pending = try manager.handle(action: params.string("action"), sessionID: params.string("sessionId"),
            confirm: params.optionalBoolean("confirm"), cancellation: latch)
        #expect(pending["state"] == .string("pending"))
        #expect(fixture.calls == 1)
        #expect(throws: NativeFailure.self) { try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch) }
        fixture.completion?(.success(88))
        let listed = try manager.handle(action: "list", cancellation: latch)
        guard case .array(let sessions) = listed["sessions"] else { Issue.record("缺少会话"); return }
        #expect(sessions[0]["state"] == .string("started"))
        #expect(sessions[0]["processIdentity"]["startedSeconds"] == .string("123"))
        guard case .array(let windows) = sessions[0]["windows"] else { Issue.record("缺少窗口"); return }
        #expect(windows.count == 1)
        #expect(windows[0]["windowId"] == .number(9))
        fixture.stale = true
        guard case .array(let stale) = try manager.handle(action: "list", cancellation: latch)["sessions"] else { return }
        #expect(stale[0]["state"] == .string("stale"))
        #expect(stale[0]["running"] == .bool(false))
        #expect(stale[0]["windows"] == .array([]))
        #expect(fixture.calls == 1)
    }

    @Test @MainActor func deniedLockedCancelledAndUnconfirmedNeverLaunch() throws {
        for mode in ["denied", "locked", "cancelled", "unconfirmed"] {
            let fixture = LauncherFixture()
            let manager = fixture.manager()
            let latch = CancellationLatch()
            let id = try #require(manager.handle(action: "prepare", cancellation: latch)["sessionId"].string)
            fixture.denied = mode == "denied"
            if mode == "locked" { fixture.locked = true }
            if mode == "cancelled" { latch.cancel() }
            #expect(throws: NativeFailure.self) {
                try manager.handle(action: "start", sessionID: id, confirm: mode != "unconfirmed", cancellation: latch)
            }
            #expect(fixture.calls == 0)
            #expect(fixture.profileCalls == 0)
        }
    }

    @Test @MainActor func priorPIDWrongIdentityAndLauncherErrorsAreNeverAdoptedOrRetried() throws {
        for mode in ["existing", "identity", "error"] {
            let fixture = LauncherFixture()
            fixture.wrongIdentity = mode == "identity"
            let manager = fixture.manager()
            let latch = CancellationLatch()
            let id = try #require(manager.handle(action: "prepare", cancellation: latch)["sessionId"].string)
            _ = try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch)
            fixture.completion?(mode == "error" ? .failure(NativeFailure(code: "BROWSER_LAUNCH_FAILED", message: "模拟错误")) : .success(mode == "existing" ? 77 : 88))
            guard case .array(let sessions) = try manager.handle(action: "list", cancellation: latch)["sessions"] else { return }
            #expect(sessions[0]["state"] == .string("failed"))
            #expect(sessions[0]["processIdentity"] == .null)
            #expect(throws: NativeFailure.self) { try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch) }
            #expect(fixture.calls == 1)
        }
    }

    @Test @MainActor func timeoutRetainsLateCompletionAndCannotLaunchTwice() async throws {
        let fixture = LauncherFixture()
        let manager = fixture.manager(timeout: .milliseconds(1))
        let latch = CancellationLatch()
        let id = try #require(manager.handle(action: "prepare", cancellation: latch)["sessionId"].string)
        _ = try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch)
        // Other AppKit tests can occupy MainActor before the timeout task starts.
        // Wait for the transition rather than assuming a 30ms scheduling order.
        for _ in 0..<100 {
            try await Task.sleep(for: .milliseconds(10))
            if case .array(let sessions) = try manager.handle(action: "list", cancellation: latch)["sessions"],
               sessions.first?["state"] == .string("launchUncertain") { break }
        }
        guard case .array(let uncertain) = try manager.handle(action: "list", cancellation: latch)["sessions"] else { return }
        #expect(uncertain[0]["state"] == .string("launchUncertain"))
        #expect(uncertain[0]["dispatched"] == .bool(true))
        #expect(throws: NativeFailure.self) { try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch) }
        fixture.completion?(.success(88))
        guard case .array(let completed) = try manager.handle(action: "list", cancellation: latch)["sessions"] else { return }
        #expect(completed[0]["state"] == .string("started"))
        #expect(fixture.calls == 1)
    }

    @Test @MainActor func cancellationAfterDispatchRetainsOwnershipWithoutAnotherLaunch() throws {
        let fixture = LauncherFixture()
        let manager = fixture.manager()
        let latch = CancellationLatch()
        let id = try #require(manager.handle(action: "prepare", cancellation: latch)["sessionId"].string)
        _ = try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch)
        latch.cancel()
        fixture.completion?(.success(88))
        let fresh = CancellationLatch()
        guard case .array(let sessions) = try manager.handle(action: "list", cancellation: fresh)["sessions"] else { return }
        #expect(sessions[0]["state"] == .string("started"))
        #expect(throws: NativeFailure.self) { try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: fresh) }
        #expect(fixture.calls == 1)
    }

    @Test @MainActor func unsafeProfileStopsTheBusinessChainBeforeLauncher() throws {
        var dependencies = BrowserSessionDependencies()
        let app = InstalledApplication(path: BrowserSessionPlan.applicationPath, executablePath: BrowserSessionPlan.executablePath,
            name: "Google Chrome", bundleID: "com.google.Chrome")
        dependencies.installed = { app }
        dependencies.authorize = { _, _ in .object(["id": .string("fixture")]) }
        dependencies.prepareProfile = { _ in throw NativeFailure(code: "UNSAFE_PATH", message: "模拟符号链接路径") }
        dependencies.launch = { _, _ in Issue.record("不安全路径不得到达 launcher") }
        let manager = BrowserSessionManager(sessionState: { false }, dependencies: dependencies)
        let latch = CancellationLatch()
        let id = try #require(manager.handle(action: "prepare", cancellation: latch)["sessionId"].string)
        #expect(throws: NativeFailure.self) { try manager.handle(action: "start", sessionID: id, confirm: true, cancellation: latch) }
        guard case .array(let sessions) = try manager.handle(action: "list", cancellation: latch)["sessions"] else { return }
        #expect(sessions[0]["state"] == .string("failed"))
        #expect(sessions[0]["dispatched"] == .bool(false))
    }

    @Test func profileAndApplicationPathsRejectSymlinks() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let link = root.appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: root)
        #expect(throws: NativeFailure.self) { try BrowserSessionPaths.requireUnlinked(link) }
        #expect(throws: NativeFailure.self) { try BrowserSessionPaths.createDirectory(link.appendingPathComponent(UUID().uuidString)) }
    }

    @MainActor private final class AttachFixture {
        var identity = ProcessIdentity(pid: 88, startedSeconds: 123, startedMicroseconds: 456)
        var bundleID = "com.google.Chrome"
        var path = BrowserSessionPlan.executablePath
        var bundlePath = BrowserSessionPlan.applicationPath
        var windowPID: Int32 = 88
        var closed = false
        var verifyCalls = 0
        var describeCalls = 0
        let authorization = JSONValue.object(["id": .string("确认记录"), "persistable": .bool(true)])

        func manager() -> BrowserSessionManager {
            var dependencies = BrowserSessionDependencies()
            dependencies.installed = { InstalledApplication(path: BrowserSessionPlan.applicationPath,
                executablePath: BrowserSessionPlan.executablePath, name: "Google Chrome", bundleID: "com.google.Chrome") }
            dependencies.identity = { _ in self.identity }
            dependencies.verify = { identity in
                self.verifyCalls += 1
                guard identity == self.identity else { throw NativeFailure(code: "STALE_APP", message: "进程代际已改变") }
            }
            dependencies.runningBundle = { _ in (self.bundleID, URL(fileURLWithPath: self.bundlePath)) }
            dependencies.executablePath = { _ in self.path }
            dependencies.describe = { _, _ in self.describeCalls += 1; return self.authorization }
            dependencies.windows = {
                let other = WindowInfo(windowID: 11, pid: 88, appName: "Chrome", title: "其他窗口",
                    bounds: WindowBounds(x: 0, y: 0, width: 100, height: 100), isOnScreen: true, onCurrentSpace: true)
                return self.closed ? [other] : [WindowInfo(windowID: 9, pid: self.windowPID, appName: "Chrome", title: "绑定窗口",
                    bounds: other.bounds, isOnScreen: false, onCurrentSpace: false), other]
            }
            dependencies.authorize = { _, _ in Issue.record("attach 不应调用启动授权"); return .null }
            dependencies.prepareProfile = { _ in Issue.record("attach 不得创建 profile") }
            dependencies.validateProfile = { _ in Issue.record("attach 不应读取独立 profile") }
            dependencies.existingPIDs = { Issue.record("attach 不应准备启动"); return [] }
            dependencies.launch = { _, _ in Issue.record("attach 不得启动进程") }
            dependencies.preview = { _, _ in Issue.record("attach 不得操作 GUI") }
            return BrowserSessionManager(sessionState: { false }, dependencies: dependencies)
        }
    }

    @Test @MainActor func attachBindsOnlyTheConfirmedWindowWithoutLaunchOrProfile() throws {
        let fixture = AttachFixture()
        let manager = fixture.manager()
        let latch = CancellationLatch()
        let params = try parameters(["action": .string("attach"), "pid": .number(88), "windowId": .number(9), "confirm": .bool(true)])
        let attached = try manager.handle(action: params.string("action"), confirm: params.optionalBoolean("confirm"),
            pid: Int32(params.integer("pid")), windowID: params.windowID(), cancellation: latch)
        #expect(attached["state"] == .string("attached"))
        #expect(attached["windowId"] == .number(9))
        #expect(attached["pid"] == .number(88))
        #expect(attached["processIdentity"]["startedSeconds"] == .string("123"))
        #expect(attached["authorization"] == fixture.authorization)
        #expect(attached["confirmed"] == .bool(true))
        #expect(attached["profile"] == .null)
        #expect(attached["configuration"] == .null)
        #expect(attached["dispatched"] == .bool(false))
        let before = fixture.verifyCalls
        #expect(try manager.handle(action: "list", cancellation: latch)["sessions"] == .array([attached]))
        #expect(fixture.verifyCalls > before)
        #expect(fixture.describeCalls >= 3)
        guard case .array(let windows) = attached["windows"] else { Issue.record("缺少窗口"); return }
        #expect(windows.count == 1)
        #expect(windows[0]["windowId"] == .number(9))
        #expect(throws: NativeFailure.self) { try manager.handle(action: "start", sessionID: attached["sessionId"].string, confirm: true, cancellation: latch) }
    }

    @Test @MainActor func attachedWindowLossOwnershipChangeAndPIDReuseInvalidatePermanently() throws {
        for mode in ["closed", "owner", "generation", "bundle", "path"] {
            let fixture = AttachFixture()
            let manager = fixture.manager()
            let latch = CancellationLatch()
            _ = try manager.handle(action: "attach", confirm: true, pid: 88, windowID: 9, cancellation: latch)
            switch mode {
            case "closed": fixture.closed = true
            case "owner": fixture.windowPID = 77
            case "generation": fixture.identity = ProcessIdentity(pid: 88, startedSeconds: 124, startedMicroseconds: 456)
            case "bundle": fixture.bundleID = "com.other.App"
            default: fixture.path = "/Applications/Other.app/Contents/MacOS/Other"
            }
            for restored in [false, true] {
                if restored {
                    fixture.closed = false; fixture.windowPID = 88
                    fixture.identity = ProcessIdentity(pid: 88, startedSeconds: 123, startedMicroseconds: 456)
                    fixture.bundleID = "com.google.Chrome"; fixture.path = BrowserSessionPlan.executablePath
                }
                let before = fixture.verifyCalls
                guard case .array(let sessions) = try manager.handle(action: "list", cancellation: latch)["sessions"] else { Issue.record("缺少会话"); return }
                #expect(sessions[0]["state"] == .string("invalidated"))
                #expect(sessions[0]["windowId"] == .number(9))
                #expect(sessions[0]["running"] == .bool(false))
                #expect(sessions[0]["windows"] == .array([]))
                #expect(fixture.verifyCalls > before)
            }
        }
    }

    @Test @MainActor func attachRejectsNonChromeWrongPathMissingWindowAndMissingConfirmation() throws {
        for mode in ["bundle", "path", "bundlePath", "window", "owner", "confirm", "missingConfirm"] {
            let fixture = AttachFixture()
            if mode == "bundle" { fixture.bundleID = "com.other.App" }
            if mode == "path" { fixture.path = "/Applications/Other.app/Contents/MacOS/Other" }
            if mode == "bundlePath" { fixture.bundlePath = "/Applications/Other.app" }
            if mode == "window" { fixture.closed = true }
            if mode == "owner" { fixture.windowPID = 77 }
            let manager = fixture.manager()
            let latch = CancellationLatch()
            #expect(throws: NativeFailure.self) {
                try manager.handle(action: "attach", confirm: mode == "missingConfirm" ? nil : mode != "confirm", pid: 88, windowID: 9, cancellation: latch)
            }
            #expect(try manager.handle(action: "list", cancellation: latch)["sessions"] == .array([]))
        }
    }

    @Test func attachParametersAreStrictAndGeneratedFieldsMatchSchema() throws {
        let valid: [String: JSONValue] = ["action": .string("attach"), "pid": .number(88), "windowId": .number(9), "confirm": .bool(true)]
        _ = try parameters(valid)
        for key in ["pid", "windowId", "confirm"] {
            var missing = valid
            missing.removeValue(forKey: key)
            #expect(throws: NativeFailure.self) { try parameters(missing) }
        }
        for key in ["pid", "windowId"] {
            for value in [JSONValue.bool(true), .string("9"), .number(0), .number(-1), .number(1.5), .number(4294967296)] {
                var invalid = valid; invalid[key] = value
                #expect(throws: NativeFailure.self) { try parameters(invalid) }
            }
        }
        for key in ["confirm", "sessionId", "profile"] {
            var invalid = valid; invalid[key] = .string("错误参数")
            #expect(throws: NativeFailure.self) { try parameters(invalid) }
        }
        for action in ["prepare", "list", "start"] {
            var invalid = valid; invalid["action"] = .string(action)
            #expect(throws: NativeFailure.self) { try parameters(invalid) }
        }
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let schema = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: root.appendingPathComponent("protocol.schema.json")))
        guard case .array(let branches) = schema["oneOf"],
              let branch = branches.first(where: { $0["properties"]["method"]["const"] == .string("browserSession") }),
              let properties = branch["properties"]["params"]["properties"].object,
              case .array(let actions) = properties["action"]?["enum"] else { Issue.record("缺少协议定义"); return }
        #expect(NativeProtocol.fields["browserSession"] == Set(properties.keys))
        #expect(NativeProtocol.stringEnums["browserSession"]?["action"] == Set(actions.compactMap(\.string)))
    }

    @Test func requiresAnExplicitActionAndAdvertisesTheMethod() {
        #expect(throws: NativeFailure.self) { try parameters([:]).string("action") }
        #expect(NativeProtocol.modelMethods.contains("browserSession"))
    }

    @Test @MainActor func lockedUnknownAndCancelledRequestsDoNotRegisterPlans() throws {
        for locked in [true, nil] as [Bool?] {
            let manager = BrowserSessionManager(sessionState: { locked })
            for action in ["prepare", "list"] {
                do {
                    _ = try manager.handle(action: action, cancellation: CancellationLatch())
                    Issue.record("锁屏或未知会话不应允许请求")
                } catch let failure as NativeFailure {
                    #expect(failure.code == (locked == true ? "SESSION_LOCKED" : "SESSION_UNAVAILABLE"))
                }
            }
        }
        let manager = BrowserSessionManager(sessionState: { false })
        let cancelled = CancellationLatch()
        cancelled.cancel()
        for action in ["prepare", "list", "create", "close"] {
            #expect(throws: NativeFailure.self) { try manager.handle(action: action, cancellation: cancelled) }
        }
        #expect(try manager.handle(action: "list", cancellation: CancellationLatch())["sessions"] == .array([]))
    }
}
