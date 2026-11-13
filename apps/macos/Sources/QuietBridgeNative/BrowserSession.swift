import AppKit
import Darwin
import Foundation

struct BrowserSessionPlan: Sendable {
    static let applicationPath = "/Applications/Google Chrome.app"
    static let executablePath = applicationPath + "/Contents/MacOS/Google Chrome"
    static let profilesRoot = QuietBridgeIdentity.browserConfiguration
        .appendingPathComponent("browser-sessions", isDirectory: true)
    let sessionID = UUID()
    var profile: String { Self.profilesRoot.appendingPathComponent(sessionID.uuidString, isDirectory: true).path }
    var arguments: [String] {
        ["--user-data-dir=\(profile)", "--no-first-run", "--no-default-browser-check", "about:blank"]
    }
    var json: JSONValue {
        .object([
            "sessionId": .string(sessionID.uuidString), "state": .string("prepared"),
            "profile": .string(profile), "pid": .null, "processIdentity": .null,
            "running": .bool(false), "windows": .array([]), "dispatched": .bool(false), "error": .null,
            "launchSupported": .bool(true), "scope": .string("connection"),
            "setup": .object([
                "profileIsolated": .bool(true), "inheritsLogin": .bool(false),
                "extensionAutoLoaded": .bool(false), "browserChannelReady": .bool(false),
                "nextAction": .string("用户在新 profile 加载 QuietBridge Browser、授权站点并登录；list 核实本会话 PID/窗口后显式绑定，不选择其他 Chrome 窗口。"),
            ]),
            "configuration": .object([
                "applicationPath": .string(Self.applicationPath), "activates": .bool(false),
                "createsNewApplicationInstance": .bool(true), "arguments": .array(arguments.map(JSONValue.string)),
            ]),
            "pendingConfirmation": .array([.string("start 需要 sessionId 和显式 confirm=true；prepare 不授予授权。")]),
            "limitations": .array([.string("请求不激活，但不保证 Chrome 启动不会改变焦点；不指定或切换 Space。prepare 不创建目录或启动进程。独立 profile 不继承扩展和登录态；会话启动不等于网页通道已就绪。")]),
        ])
    }
}

// launcher 不接收调用者提供的路径或参数；迟到完成仍由持有 manager 的回调接收。
@MainActor struct BrowserSessionDependencies {
    typealias Completion = @MainActor @Sendable (Result<Int32, NativeFailure>) -> Void
    var installed: () throws -> InstalledApplication = {
        try BrowserSessionPaths.requireUnlinked(URL(fileURLWithPath: BrowserSessionPlan.executablePath))
        guard let app = InstalledApplication.read(URL(fileURLWithPath: BrowserSessionPlan.applicationPath)),
              app.path == BrowserSessionPlan.applicationPath, app.executablePath == BrowserSessionPlan.executablePath,
              app.bundleID == "com.google.Chrome" else {
            throw NativeFailure(code: "APP_UNAVAILABLE", message: "固定路径未安装预期的 Google Chrome")
        }
        return app
    }
    var authorize: (InstalledApplication, Bool) throws -> JSONValue = ApplicationAuthorization.authorizeInstalled
    var describe: (ProcessIdentity, InstalledApplication) throws -> JSONValue = {
        try ApplicationAuthorization.describe($0, name: $1.name, expectedExecutablePath: $1.executablePath)
    }
    var identity: (Int32) throws -> ProcessIdentity = NativePlatform.identity
    var verify: (ProcessIdentity) throws -> Void = NativePlatform.verify
    var windows: () -> [WindowInfo] = { NativePlatform.windows() }
    var runningBundle: (Int32) -> (identifier: String?, url: URL?) = ApplicationAuthorization.runningBundle
    var executablePath: (Int32) throws -> String = { try NativePlatform.executablePathResult($0).get() }
    var verifyCloneSignature: (ProcessIdentity) throws -> Void = ApplicationExecutableIdentity.verifyChromeSignature
    var existingPIDs: () throws -> Set<Int32> = {
        let count = proc_listallpids(nil, 0)
        guard count > 0 else { throw NativeFailure(code: "NATIVE_ERROR", message: "无法获取启动前进程列表") }
        var pids = [Int32](repeating: 0, count: Int(count) + 4096)
        let capacity = pids.count * MemoryLayout<Int32>.size
        let actual = pids.withUnsafeMutableBytes { proc_listallpids($0.baseAddress, Int32(capacity)) }
        guard actual > 0, Int(actual) < pids.count else { throw NativeFailure(code: "NATIVE_ERROR", message: "启动前进程列表不完整") }
        return Set(pids.prefix(Int(actual)).filter { $0 > 0 })
    }
    var prepareProfile: (BrowserSessionPlan) throws -> Void = { try BrowserSessionPaths.createProfile($0) }
    var validateProfile: (BrowserSessionPlan) throws -> Void = {
        try BrowserSessionPaths.requireUnlinked(URL(fileURLWithPath: $0.profile))
    }
    var launch: (BrowserSessionPlan, @escaping Completion) -> Void = { plan, completion in
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.addsToRecentItems = false
        configuration.createsNewApplicationInstance = true
        configuration.arguments = plan.arguments
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: BrowserSessionPlan.applicationPath), configuration: configuration) { app, error in
            let result: Result<Int32, NativeFailure>
            if let app, app.processIdentifier > 0, !app.isTerminated { result = .success(app.processIdentifier) }
            else { result = .failure(NativeFailure(code: "BROWSER_LAUNCH_FAILED", message: error?.localizedDescription ?? "启动未返回有效进程")) }
            Task { @MainActor in completion(result) }
        }
    }
    var preview: (ProcessIdentity, UInt32) -> Void = { identity, windowID in
        guard (try? NativePlatform.verify(identity)) != nil else { return }
        SpacePreview.shared.track(pid: identity.pid, windowID: windowID)
    }
    var timeout: Duration = .seconds(10)
}

enum BrowserSessionPaths {
    static func requireUnlinked(_ url: URL) throws {
        var current = URL(fileURLWithPath: "/")
        for component in url.standardizedFileURL.pathComponents.dropFirst() {
            current.appendPathComponent(component)
            var info = stat()
            guard lstat(current.path, &info) == 0, (info.st_mode & S_IFMT) != S_IFLNK else {
                throw NativeFailure(code: "UNSAFE_PATH", message: "路径不存在或包含符号链接：\(current.path)")
            }
        }
    }

    static func createProfile(_ plan: BrowserSessionPlan) throws {
        try createDirectory(URL(fileURLWithPath: plan.profile))
    }

    // 逐级通过目录描述符创建，拒绝任意祖先符号链接；最后一级必须是全新 UUID 目录。
    static func createDirectory(_ url: URL) throws {
        var descriptor = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw NativeFailure(code: "UNSAFE_PATH", message: "无法打开 profile 根路径") }
        defer { close(descriptor) }
        let components = url.standardizedFileURL.pathComponents.dropFirst()
        for (index, component) in components.enumerated() {
            let last = index == components.count - 1
            let created = mkdirat(descriptor, component, 0o700)
            guard created == 0 || (!last && errno == EEXIST) else {
                throw NativeFailure(code: "UNSAFE_PATH", message: "profile 路径无法安全创建或已经存在")
            }
            let next = openat(descriptor, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard next >= 0 else { throw NativeFailure(code: "UNSAFE_PATH", message: "profile 路径包含符号链接或非目录") }
            close(descriptor)
            descriptor = next
        }
    }
}

@MainActor final class BrowserSessionManager {
    private final class Session {
        let plan = BrowserSessionPlan()
        var state = "prepared"
        var identity: ProcessIdentity?
        var windowID: UInt32?
        var authorization: JSONValue?
        var application: InstalledApplication?
        var priorPIDs: Set<Int32> = []
        var dispatched = false
        var cancellation: CancellationLatch?
        var failure: NativeFailure?
        var timeoutTask: Task<Void, Never>?
    }
    private var sessions: [Session] = []
    private let sessionState: () -> Bool?
    private let dependencies: BrowserSessionDependencies

    init(sessionState: @escaping () -> Bool? = NativePlatform.sessionLocked,
         dependencies: BrowserSessionDependencies = BrowserSessionDependencies()) {
        self.sessionState = sessionState
        self.dependencies = dependencies
    }

    func handle(action: String, sessionID: String? = nil, confirm: Bool? = nil,
                pid: Int32? = nil, windowID: UInt32? = nil, cancellation: CancellationLatch) throws -> JSONValue {
        guard ["prepare", "list", "start", "attach"].contains(action) else { throw RequestParameters.invalid("action 必须是 prepare、list、start 或 attach") }
        if action == "attach" {
            guard sessionID == nil, let pid, pid > 0, let windowID, windowID > 0, confirm == true else {
                throw RequestParameters.invalid("attach 必须且仅提供 pid、windowId 和 confirm=true")
            }
        } else if action == "start" {
            guard let sessionID, UUID(uuidString: sessionID)?.uuidString == sessionID, confirm == true, pid == nil, windowID == nil else {
                throw RequestParameters.invalid("start 必须提供 prepare 返回的 sessionId 和 confirm=true")
            }
        } else if sessionID != nil || confirm != nil || pid != nil || windowID != nil { throw RequestParameters.invalid("prepare/list 仅接受 action") }
        try cancellation.check()
        try NativePlatform.requireUnlocked(sessionState())
        try cancellation.check()
        if action == "attach", let pid, let windowID {
            let identity = try dependencies.identity(pid)
            guard identity.pid == pid else { throw NativeFailure(code: "BROWSER_PROCESS_MISMATCH", message: "进程身份不匹配请求的 PID") }
            let app = try dependencies.installed()
            try verifyChrome(identity, application: app)
            let authorization = try dependencies.describe(identity, app)
            _ = try NativePlatform.requireOwnedWindow(pid, dependencies.windows().first { $0.windowID == windowID })
            try dependencies.verify(identity)
            try cancellation.check()
            try NativePlatform.requireUnlocked(sessionState())
            // 同一进程代际 + 同一窗口重复绑定必须复用原会话：任务记录里的
            // sessionId 要始终能在 list 里解析到，否则 status 会判定目标失效。
            if let existing = sessions.first(where: {
                $0.state == "attached" && $0.windowID == windowID && $0.identity == Optional(identity)
            }) {
                return try render(existing, cancellation: cancellation)
            }
            let session = Session()
            session.identity = identity
            session.windowID = windowID
            session.application = app
            session.authorization = authorization
            session.state = "attached"
            let result = try render(session, cancellation: cancellation)
            // 进程代际变化（Chrome 重建窗口）时替换旧记录，避免泄漏。
            sessions.removeAll { $0.state == "attached" && $0.identity?.pid == pid && $0.windowID == windowID }
            sessions.append(session)
            return result
        }
        if action == "prepare" {
            let session = Session()
            sessions.append(session)
            return session.plan.json
        }
        if action == "list" {
            // 兜底去重：历史版本可能已累积同窗口的 attached 记录，对外只报最新一条。
            var seen = Set<String>()
            var unique: [Session] = []
            for session in sessions.reversed() where session.state == "attached" {
                let key = "\(session.identity?.pid ?? 0):\(session.windowID ?? 0)"
                if seen.insert(key).inserted { unique.append(session) }
            }
            var visible = sessions.filter { $0.state != "attached" }
            visible.append(contentsOf: unique.reversed())
            return .object(["sessions": .array(try visible.map { try render($0, cancellation: cancellation) }),
                            "scope": .string("connection"), "launchSupported": .bool(true)])
        }
        guard let session = sessions.first(where: { $0.plan.sessionID.uuidString == sessionID }) else {
            throw NativeFailure(code: "UNKNOWN_SESSION", message: "sessionId 不属于当前连接")
        }
        guard session.state == "prepared" else {
            throw NativeFailure(code: "SESSION_ALREADY_STARTED", message: "该会话已尝试启动；禁止重放，包括 pending、超时和失败状态")
        }
        session.state = "authorizing"
        do {
            let app = try dependencies.installed()
            let authorization = try dependencies.authorize(app, confirm == true)
            session.application = app
            session.authorization = authorization
            try cancellation.check()
            try NativePlatform.requireUnlocked(sessionState())
            try dependencies.prepareProfile(session.plan)
            guard try dependencies.installed() == app,
                  try dependencies.authorize(app, confirm == true) == authorization else {
                throw NativeFailure(code: "STALE_APP", message: "Chrome 安装身份在启动前改变")
            }
            session.priorPIDs = try dependencies.existingPIDs()
            try dependencies.validateProfile(session.plan)
            try cancellation.check()
            try NativePlatform.requireUnlocked(sessionState())
            session.state = "pending"
            session.cancellation = cancellation
            session.dispatched = true
            let id = session.plan.sessionID
            session.timeoutTask = Task { [self] in
                do { try await Task.sleep(for: dependencies.timeout) } catch { return }
                markTimedOut(id)
            }
            dependencies.launch(session.plan) { [self] result in complete(id, result: result) }
        } catch {
            session.state = "failed"
            session.failure = (error as? NativeFailure) ?? NativeFailure(code: "BROWSER_LAUNCH_FAILED", message: error.localizedDescription)
            throw error
        }
        return try render(session, cancellation: cancellation)
    }

    private func markTimedOut(_ id: UUID) {
        guard let session = sessions.first(where: { $0.plan.sessionID == id }), session.state == "pending" else { return }
        session.state = "launchUncertain"
        session.failure = NativeFailure(code: "BROWSER_LAUNCH_TIMEOUT", message: "启动已派发但完成回调超时；可能已经启动，继续保留归属并接收迟到回调，禁止再次启动")
        session.timeoutTask = nil
    }

    private func complete(_ id: UUID, result: Result<Int32, NativeFailure>) {
        guard let session = sessions.first(where: { $0.plan.sessionID == id }),
              ["pending", "launchUncertain"].contains(session.state) else { return }
        session.timeoutTask?.cancel()
        session.timeoutTask = nil
        do {
            let pid = try result.get()
            guard pid > 0, !session.priorPIDs.contains(pid), let app = session.application, let approved = session.authorization else {
                throw NativeFailure(code: "BROWSER_PROCESS_MISMATCH", message: "启动返回既有或无效 PID；不接管也不终止任何浏览器")
            }
            let identity = try dependencies.identity(pid)
            guard identity.pid == pid, try dependencies.installed() == app,
                  try dependencies.describe(identity, app) == approved else {
                throw NativeFailure(code: "BROWSER_PROCESS_MISMATCH", message: "启动进程不匹配获准的 Chrome 安装身份；不终止任何浏览器")
            }
            try dependencies.verify(identity)
            session.identity = identity
            session.state = "started"
            session.failure = nil
            if session.cancellation?.isCancelled == false, sessionState() == false,
               let window = dependencies.windows().first(where: { $0.pid == identity.pid && $0.onCurrentSpace == false }) {
                try dependencies.verify(identity)
                dependencies.preview(identity, window.windowID)
            }
        } catch {
            session.state = "failed"
            session.failure = (error as? NativeFailure) ?? NativeFailure(code: "BROWSER_LAUNCH_FAILED", message: error.localizedDescription)
        }
    }

    private func verifyChrome(_ identity: ProcessIdentity, application app: InstalledApplication) throws {
        try dependencies.verify(identity)
        let bundle = dependencies.runningBundle(identity.pid)
        let path = try dependencies.executablePath(identity.pid)
        guard app.bundleID == "com.google.Chrome", app.path == BrowserSessionPlan.applicationPath,
              app.executablePath == BrowserSessionPlan.executablePath,
              bundle.identifier == "com.google.Chrome",
              bundle.url?.resolvingSymlinksInPath().path == app.path else {
            throw NativeFailure(code: "BROWSER_PROCESS_MISMATCH", message: "绑定目标必须是固定真实路径的 Google Chrome 主进程")
        }
        do {
            _ = try ApplicationExecutableIdentity.resolve(identity, observedPath: path, expectedPath: app.executablePath,
                bundleIdentifier: bundle.identifier, bundleURL: bundle.url,
                verifyProcess: dependencies.verify, verifySignature: dependencies.verifyCloneSignature)
        } catch let error as NativeFailure where error.code == "STALE_APP" {
            throw NativeFailure(code: "BROWSER_PROCESS_MISMATCH", message: "无法核验固定安装路径对应的 Chrome 进程；代码克隆需要有效的运行进程与安装签名，不会改绑其他浏览器")
        }
        try dependencies.verify(identity)
    }

    private func render(_ session: Session, cancellation: CancellationLatch) throws -> JSONValue {
        try cancellation.check()
        try NativePlatform.requireUnlocked(sessionState())
        var fields = session.plan.json.object!
        var windows: [WindowInfo] = []
        if let identity = session.identity {
            do {
                try dependencies.verify(identity)
                guard let app = session.application, try dependencies.installed() == app,
                      try dependencies.describe(identity, app) == session.authorization else {
                    throw NativeFailure(code: "BROWSER_PROCESS_MISMATCH", message: "会话进程身份已改变")
                }
                if let windowID = session.windowID {
                    try verifyChrome(identity, application: app)
                    guard session.state != "invalidated" else {
                        throw NativeFailure(code: "WINDOW_UNAVAILABLE", message: "绑定已经失效；不会自动重新绑定")
                    }
                    windows = [try NativePlatform.requireOwnedWindow(identity.pid, dependencies.windows().first { $0.windowID == windowID })]
                } else {
                    windows = dependencies.windows().filter { $0.pid == identity.pid }
                }
                try dependencies.verify(identity)
            } catch {
                session.state = session.windowID == nil ? "stale" : "invalidated"
                session.failure = (error as? NativeFailure) ?? NativeFailure(code: "STALE_APP", message: error.localizedDescription)
                windows = []
            }
            fields["pid"] = .number(Double(identity.pid))
            fields["processIdentity"] = .object(["pid": .number(Double(identity.pid)),
                "startedSeconds": .string(String(identity.startedSeconds)), "startedMicroseconds": .string(String(identity.startedMicroseconds))])
        }
        try cancellation.check()
        try NativePlatform.requireUnlocked(sessionState())
        fields["state"] = .string(session.state)
        if let windowID = session.windowID {
            fields["mode"] = .string("attached")
            fields["windowId"] = .number(Double(windowID))
            fields["profile"] = .null
            fields["configuration"] = .null
            fields["pendingConfirmation"] = .array([])
            fields["authorization"] = session.authorization
            fields["confirmed"] = .bool(true)
            fields["limitations"] = .array([.string("共享既有 Chrome 数据，仅绑定指定进程代际与窗口；不启动、不写 profile、不激活、不 raise、不切换 Space。")])
        }
        fields["running"] = .bool(session.state == "started" || session.state == "attached")
        fields["dispatched"] = .bool(session.dispatched)
        fields["windows"] = .array(windows.map(\.json))
        fields["error"] = session.failure.map { .object(["code": .string($0.code), "message": .string($0.message)]) } ?? .null
        return .object(fields)
    }
}
