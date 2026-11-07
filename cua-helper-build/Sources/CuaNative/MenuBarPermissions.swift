import Foundation
import Darwin
import Security

enum DesktopPermission: String, CaseIterable, Sendable {
    case accessibility, screenRecording
    var title: String { self == .accessibility ? "辅助功能" : "屏幕录制" }
    var explanation: String {
        self == .accessibility
            ? "辅助功能权限用于识别应用控件，并向你指定的应用发送点击、键盘和文字输入。"
            : "屏幕录制权限用于任务截图和实时预览，按任务选择的应用、窗口或桌面范围共享画面。"
    }
    static func required(for key: String) -> [Self] {
        switch key {
        case "native_observe", "native_input", "locked_use": return allCases
        case "preview": return [.screenRecording]
        default: return []
        }
    }
}

struct DesktopPermissionStatus: Sendable, Equatable {
    let accessibility: Bool
    let screenRecording: Bool
    let applicationName: String

    init(accessibility: Bool, screenRecording: Bool, applicationName: String = "Workbuddy CUA") {
        self.accessibility = accessibility; self.screenRecording = screenRecording; self.applicationName = applicationName
    }
    init(_ value: JSONValue) throws {
        guard case .bool(let ax) = value["accessibility"], case .bool(let capture) = value["screenRecording"] else {
            throw NativeFailure(code: "PERMISSION_STATUS_INVALID", message: "权限检测返回不完整，请重新检测。")
        }
        accessibility = ax; screenRecording = capture
        let source = value["launchApplication"]["name"].string ??
            value["helper"]["path"].string.flatMap { path in
                URL(fileURLWithPath: path).pathComponents.last(where: { $0.hasSuffix(".app") })
            } ?? "Workbuddy CUA"
        applicationName = String((source.hasSuffix(".app") ? String(source.dropLast(4)) : source).prefix(100))
    }
    var allGranted: Bool { accessibility && screenRecording }
    func granted(_ permission: DesktopPermission) -> Bool { permission == .accessibility ? accessibility : screenRecording }
    func missing(for key: String) -> [DesktopPermission] { DesktopPermission.required(for: key).filter { !granted($0) } }
}

// The menu retains a stdio connection to the same signed helper executable as
// the MCP bridge. Permission checks and requests therefore run in the actual
// desktop executor, rather than reporting the menu process's unrelated grants.
actor MenuBarPermissionConnection {
    private let executable: URL
    private let session: ApplicationSession
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var sequence = 0

    init(executable: URL, session: ApplicationSession) { self.executable = executable; self.session = session }

    static func helper(near executable: URL? = Bundle.main.executableURL,
                       installation: URL = URL(fileURLWithPath: "/Library/Application Support/workbuddy-cua/installation.plist")) throws -> URL {
        if let data = try? Data(contentsOf: installation),
           let info = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let path = info["helper"] as? String {
            let url = URL(fileURLWithPath: path).standardizedFileURL
            let root = "/Library/Application Support/WorkBuddyCUAHelper/releases/"
            guard url.path.hasPrefix(root), url.path.hasSuffix("/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native"),
                  url.resolvingSymlinksInPath().path == url.path else { throw unavailable }
            var file = stat()
            var parent = url
            while true {
                guard lstat(parent.path, &file) == 0, file.st_uid == 0, file.st_mode & 0o022 == 0 else { throw unavailable }
                if parent.path == "/" { break }
                parent.deleteLastPathComponent()
            }
            let bundle = url.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            var code: SecStaticCode?
            var requirement: SecRequirement?
            guard SecStaticCodeCreateWithPath(bundle as CFURL, [], &code) == errSecSuccess, let code,
                  SecRequirementCreateWithString("anchor apple generic and identifier \"com.workbuddy.cua.helper\" and certificate leaf[subject.OU] = \"62RNP54C47\"" as CFString, [], &requirement) == errSecSuccess,
                  SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSStrictValidate), requirement) == errSecSuccess else { throw unavailable }
            return url
        }
        // Source builds use their own executable and the same shared-service key.
        guard let executable, Bundle.main.bundleIdentifier != "com.workbuddy.cua.menubar",
              FileManager.default.isExecutableFile(atPath: executable.path) else { throw unavailable }
        return executable
    }
    static var unavailable: NativeFailure {
        NativeFailure(code: "PERMISSION_HELPER_UNAVAILABLE", message: "无法连接权限服务，请重新打开 Workbuddy CUA；若仍失败，请重新安装应用。")
    }

    func request(_ method: String = "permissionStatus", permission: DesktopPermission? = nil, timeout: Double = 8) throws -> JSONValue {
        try session.requireActive()
        guard ["permissionStatus", "requestPermission", "openPermissionSettings"].contains(method),
              method == "permissionStatus" || permission != nil else { throw MenuBarPermissionConnection.unavailable }
        do {
            if process?.isRunning != true {
                stop()
                let child = Process(), stdin = Pipe(), stdout = Pipe()
                child.executableURL = executable
                child.arguments = ["--timeout-ms", "60000"]
                child.standardInput = stdin; child.standardOutput = stdout; child.standardError = FileHandle.nullDevice
                try child.run()
                process = child; input = stdin.fileHandleForWriting; output = stdout.fileHandleForReading
            }
            sequence += 1
            let id = "permission-panel-\(sequence)"
            let request = JSONValue.object(["id": .string(id), "method": .string(method),
                "params": .object(permission.map { ["permission": .string($0.rawValue)] } ?? [:])])
            try input?.write(contentsOf: JSONEncoder().encode(request) + Data([10]))
            let deadline = ProcessInfo.processInfo.systemUptime + timeout
            while true {
                try session.requireActive()
                if let newline = buffer.firstIndex(of: 10) {
                    let data = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                    let reply = try JSONDecoder().decode(JSONValue.self, from: data)
                    guard reply["id"].string == id else { throw Self.unavailable }
                    if let code = reply["error"]["code"].string {
                        throw NativeFailure(code: code, message: reply["error"]["message"].string ?? "权限请求失败，请重试。")
                    }
                    guard reply.object?["result"] != nil else { throw Self.unavailable }
                    return reply["result"]
                }
                let remaining = deadline - ProcessInfo.processInfo.systemUptime
                guard remaining > 0, buffer.count < 1_048_576, let output else {
                    throw NativeFailure(code: "PERMISSION_TIMEOUT", message: "权限检测暂未返回，请完成系统对话框后重新检测。")
                }
                var item = pollfd(fd: output.fileDescriptor, events: Int16(POLLIN), revents: 0)
                let ready = poll(&item, 1, Int32(min(remaining, 0.2) * 1000))
                if ready < 0 && errno == EINTR { continue }
                guard ready >= 0 else { throw Self.unavailable }
                if ready == 0 { continue }
                // FileHandle.read(upToCount:) may wait to fill a pipe buffer,
                // bypassing the timeout even after poll reports one short line.
                var bytes = [UInt8](repeating: 0, count: 65536)
                let count = bytes.withUnsafeMutableBytes { Darwin.read(output.fileDescriptor, $0.baseAddress, $0.count) }
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw Self.unavailable }
                buffer.append(contentsOf: bytes.prefix(count))
            }
        } catch {
            stop()
            throw error
        }
    }

    func stop() {
        try? input?.close(); try? output?.close()
        if process?.isRunning == true { process?.terminate() }
        process = nil; input = nil; output = nil; buffer.removeAll()
    }
}
