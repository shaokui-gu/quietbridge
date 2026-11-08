import AppKit
import Darwin
import Foundation

// Uses the native host's lock and atomic replacement protocol.
enum MenuBarBrowserStore {
    private static var directory: URL {
        let installed = QuietBridgeIdentity.browserConfiguration
        if FileManager.default.fileExists(atPath: installed.path) { return installed }
        var root = Bundle.main.executableURL?.deletingLastPathComponent() ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        for _ in 0..<10 {
            let candidate = root.appendingPathComponent("quietbridge-config")
            if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
            let parent = root.deletingLastPathComponent()
            if parent == root { break }
            root = parent
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("quietbridge-config")
    }

    private static func withStore<T>(at location: URL? = nil, fileName: String = "browser-events.json",
                                     _ body: (inout [String: Any], Int32) throws -> T) throws -> T {
        var dir = open("/", O_RDONLY | O_DIRECTORY)
        guard dir >= 0 else { throw StoreError.unavailable }
        defer { close(dir) }
        for part in (location ?? directory).pathComponents where part != "/" {
            let next = openat(dir, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            guard next >= 0 else { throw StoreError.unavailable }
            close(dir); dir = next
        }
        let lock = openat(dir, fileName + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600)
        guard lock >= 0 else { throw StoreError.unavailable }
        defer { close(lock) }
        var info = stat()
        guard fstat(lock, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid() else { throw StoreError.unavailable }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw StoreError.busy }
        defer { flock(lock, LOCK_UN) }
        let file = openat(dir, fileName, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard file >= 0 else { throw StoreError.unavailable }
        defer { close(file) }
        guard fstat(file, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(), info.st_size <= 1_048_576 else { throw StoreError.unavailable }
        let data = try FileHandle(fileDescriptor: dup(file), closeOnDealloc: true).readToEnd() ?? Data()
        guard var object = try JSONSerialization.jsonObject(with: data) as? [String: Any], object["version"] as? Int == 1 else { throw StoreError.unavailable }
        return try body(&object, dir)
    }

    static func read() throws -> [String: Any] {
        try withStore { data, _ in data["peer"] as? [String: Any] ?? [:] }
    }

    struct Session {
        let channel: String
        let taskId: String
        let peer: [String: Any]
        var label: String {
            let summary = (peer["panelState"] as? [String: Any])?["taskSummary"] as? [String: Any]
            let site = (summary?["url"] as? String).flatMap { URL(string: $0)?.host } ?? "等待页面"
            return "\(taskId.prefix(6)) · \(site)"
        }
    }
    struct Snapshot {
        let rootPeer: [String: Any]
        let sessions: [Session]
    }

    private static func locations(at root: URL) throws -> [(channel: String, taskId: String, directory: URL)] {
        let slots = try withStore(at: root, fileName: "session-registry.json") { data, _ in
            data["tasks"] as? [String: [String: Any]] ?? [:]
        }
        return slots.sorted(by: { $0.key < $1.key }).prefix(3).compactMap { key, slot in
            guard key.count == 64, key.allSatisfy({ "abcdef0123456789".contains($0) }),
                  let taskId = slot["taskId"] as? String else { return nil }
            let location = root.appendingPathComponent("sessions").appendingPathComponent(key)
            guard slot["taskFile"] as? String == location.appendingPathComponent("task-windows.json").path,
                  let active = try? withStore(at: location, fileName: "task-windows.json", { data, _ in
                      (data["tasks"] as? [String: [String: Any]])?[taskId]
                  }), let state = active["state"] as? String,
                  !["ended", "interrupted", "invalidated"].contains(state) else { return nil }
            return (key, taskId, location)
        }
    }

    static func snapshot(at location: URL? = nil) throws -> Snapshot {
        let root = location ?? directory
        let rootPeer = (try? withStore(at: root) { data, _ in data["peer"] as? [String: Any] ?? [:] }) ?? [:]
        let sessions = ((try? locations(at: root)) ?? []).compactMap { item -> Session? in
            guard let peer = try? withStore(at: item.directory, { data, _ in data["peer"] as? [String: Any] ?? [:] }),
                  let summary = (peer["panelState"] as? [String: Any])?["taskSummary"] as? [String: Any],
                  summary["taskId"] as? String == item.taskId else { return nil }
            return Session(channel: item.channel, taskId: item.taskId, peer: peer)
        }
        return Snapshot(rootPeer: rootPeer, sessions: sessions)
    }

    static func submit(_ operation: String, channel: String, taskId: String, arguments: [String: Any] = [:],
                       at location: URL? = nil) throws {
        guard let target = try locations(at: location ?? directory).first(where: { $0.channel == channel && $0.taskId == taskId })
        else { throw StoreError.disconnected }
        try withStore(at: target.directory) { data, dir in
            var peer = data["peer"] as? [String: Any] ?? [:]
            guard let state = peer["panelState"] as? [String: Any],
                  let summary = state["taskSummary"] as? [String: Any], summary["taskId"] as? String == taskId,
                  let seen = peer["lastSeen"] as? TimeInterval, Date().timeIntervalSince1970 - seen < 15,
                  peer["enabled"] as? Bool == true, peer["permission"] as? Bool == true else { throw StoreError.disconnected }
            if let command = peer["panelCommand"] as? [String: Any],
               let expires = command["expiresAt"] as? TimeInterval, expires > Date().timeIntervalSince1970 { throw StoreError.busy }
            var command: [String: Any] = ["requestId": UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                                          "taskId": taskId, "operation": operation, "arguments": arguments,
                                          "expiresAt": Date().timeIntervalSince1970 + 20]
            if let version = summary["assignmentVersion"] {
                command["assignmentVersion"] = version
                command["documentId"] = summary["documentId"] ?? NSNull()
            }
            peer["panelCommand"] = command
            peer["panelResult"] = NSNull()
            data["peer"] = peer
            let raw = try JSONSerialization.data(withJSONObject: data)
            guard raw.count <= 1_048_576 else { throw StoreError.unavailable }
            let temporary = ".browser-panel-\(UUID().uuidString)"
            let fd = openat(dir, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { throw StoreError.unavailable }
            defer { close(fd); unlinkat(dir, temporary, 0) }
            let written = raw.withUnsafeBytes { bytes in
                guard let base = bytes.baseAddress else { return 0 }
                var offset = 0
                while offset < bytes.count {
                    let count = write(fd, base.advanced(by: offset), bytes.count - offset)
                    if count <= 0 { break }; offset += count
                }
                return offset
            }
            guard written == raw.count, fsync(fd) == 0,
                  renameat(dir, temporary, dir, "browser-events.json") == 0 else { throw StoreError.unavailable }
            _ = fsync(dir)
        }
    }

    enum StoreError: LocalizedError {
        case unavailable, disconnected, busy
        var errorDescription: String? {
            switch self {
            case .unavailable: return "无法读取浏览器连接，请检查扩展和原生宿主安装状态。"
            case .disconnected: return "浏览器扩展未连接或任务已变化。"
            case .busy: return "上一项操作尚未返回，请稍候。"
            }
        }
    }
}
