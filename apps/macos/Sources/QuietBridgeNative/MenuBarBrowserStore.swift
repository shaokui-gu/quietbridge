import AppKit
import CryptoKit
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

    private static func withStore<T>(at location: URL? = nil, fileName: String = "browser-events.json", lockName: String? = nil,
                                     locking: Bool = true,
                                     _ body: (inout [String: Any], Int32) throws -> T) throws -> T {
        var dir = open("/", O_RDONLY | O_DIRECTORY)
        guard dir >= 0 else { throw StoreError.unavailable }
        defer { close(dir) }
        for part in (location ?? directory).pathComponents where part != "/" {
            let next = openat(dir, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            guard next >= 0 else { throw StoreError.unavailable }
            close(dir); dir = next
        }
        var info = stat()
        let lock = locking ? openat(dir, lockName ?? fileName + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600) : -1
        defer { if lock >= 0 { _ = flock(lock, LOCK_UN); close(lock) } }
        if locking {
            guard lock >= 0, fstat(lock, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
                  info.st_uid == getuid(), info.st_mode & 0o077 == 0 else { throw StoreError.unavailable }
            guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw StoreError.busy }
        }
        let file = openat(dir, fileName, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard file >= 0 else { throw StoreError.unavailable }
        defer { close(file) }
        guard fstat(file, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(),
              info.st_mode & 0o022 == 0, info.st_size <= 1_048_576 else { throw StoreError.unavailable }
        let data = try FileHandle(fileDescriptor: dup(file), closeOnDealloc: true).readToEnd() ?? Data()
        guard var object = try JSONSerialization.jsonObject(with: data) as? [String: Any], object["version"] as? Int == 1 else { throw StoreError.unavailable }
        return try body(&object, dir)
    }

    private static func replace(_ object: [String: Any], fileName: String, directory dir: Int32) throws {
        let raw = try JSONSerialization.data(withJSONObject: object)
        guard raw.count <= 1_048_576 else { throw StoreError.unavailable }
        let temporary = ".quietbridge-panel-\(UUID().uuidString)"
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
              renameat(dir, temporary, dir, fileName) == 0 else { throw StoreError.unavailable }
        _ = fsync(dir)
    }

    static func read() throws -> [String: Any] {
        try withStore { data, _ in data["peer"] as? [String: Any] ?? [:] }
    }

    struct Session {
        let channel: String
        let taskId: String
        let peer: [String: Any]
        let task: [String: Any]
        let host: [String: Any]
        var ownerId: String? { (task["lease"] as? [String: Any])?["ownerId"] as? String }
        var hostSessionId: String? { host["sessionId"] as? String }
        var windowId: Int? { task["windowId"] as? Int }
        var stopping: Bool { host["stoppedAt"] is NSNumber }
        var canStop: Bool { ownerId != nil && hostSessionId != nil && !stopping }
        var label: String {
            let summary = (peer["panelState"] as? [String: Any])?["taskSummary"] as? [String: Any]
            let url = summary?["taskId"] as? String == taskId ? summary?["url"] as? String : nil
            let site = url.flatMap { URL(string: $0)?.host } ?? "等待页面"
            let target = windowId.map { "窗口 \($0)" } ?? "未绑定窗口"
            let holder = hostSessionId.map { "会话 \($0.prefix(8))" } ?? "任务 \(taskId.prefix(8))"
            return "\(target) · \(holder) · \(site)"
        }
    }
    struct Snapshot {
        let rootPeer: [String: Any]
        let sessions: [Session]
    }

    private static func ownerIsLive(_ task: [String: Any], directory dir: Int32) -> Bool {
        guard let lease = task["lease"] as? [String: Any] else { return true } // Legacy display only.
        guard let owner = lease["ownerId"] as? String, UUID(uuidString: owner) != nil else { return false }
        if !["suspended", "stopping"].contains(task["state"] as? String ?? "") {
            let now = Date().timeIntervalSince1970
            let uptime = ProcessInfo.processInfo.systemUptime
            guard let wall = lease["expiresAt"] as? Double, wall > now, wall - now <= 125,
                  let monotonic = lease["expiresAtUptime"] as? Double, monotonic > uptime,
                  monotonic - uptime <= 125 else { return false }
        }
        let fd = openat(dir, "task-owner-" + owner + ".lock", O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o777 == 0o600 else { return false }
        if flock(fd, LOCK_SH | LOCK_NB) == 0 { _ = flock(fd, LOCK_UN); return false }
        guard errno == EWOULDBLOCK else { return false }
        let data = try? FileHandle(fileDescriptor: dup(fd), closeOnDealloc: true).read(upToCount: 64)
        return data.flatMap { String(data: $0, encoding: .ascii) } == owner
    }

    private static func locations(at root: URL) throws -> [(channel: String, taskId: String, directory: URL, task: [String: Any])] {
        let slots = try withStore(at: root, fileName: "session-registry.json") { data, _ in
            data["tasks"] as? [String: [String: Any]] ?? [:]
        }
        return slots.sorted(by: { $0.key < $1.key }).compactMap { key, slot in
            guard key.count == 64, key.allSatisfy({ "abcdef0123456789".contains($0) }),
                  let taskId = slot["taskId"] as? String else { return nil }
            let location = root.appendingPathComponent("sessions").appendingPathComponent(key)
            guard slot["taskFile"] as? String == location.appendingPathComponent("task-windows.json").path,
                  let active = try? withStore(at: location, fileName: "task-windows.json", locking: false, { data, dir -> [String: Any]? in
                      guard data["activeTaskId"] == nil || data["activeTaskId"] as? String == taskId,
                            let task = (data["tasks"] as? [String: [String: Any]])?[taskId],
                            ownerIsLive(task, directory: dir) else { return nil }
                      return task
                  }), let state = active["state"] as? String,
                  !["ended", "interrupted", "invalidated"].contains(state) else { return nil }
            return (key, taskId, location, active)
        }
    }

    private static func matchingHost(_ turns: [String: [String: Any]], channel: String,
                                     taskId: String, ownerId: String?) -> (String, [String: Any])? {
        guard let ownerId, UUID(uuidString: ownerId) != nil else { return nil }
        let matches = turns.filter { _, turn in
            guard let session = turn["sessionId"] as? String else { return false }
            let hash = SHA256.hash(data: Data(("workbuddy\0" + session).utf8)).map { String(format: "%02x", $0) }.joined()
            let receipts = turn["tasks"] as? [String: String] ?? [:]
            return hash == channel && (receipts[taskId] == ownerId ||
                (turn["taskId"] as? String == taskId && turn["ownerId"] as? String == ownerId))
        }
        guard matches.count == 1 else { return nil }
        return matches.first
    }

    static func snapshot(at location: URL? = nil) throws -> Snapshot {
        let root = location ?? directory
        let rootPeer = (try? withStore(at: root) { data, _ in data["peer"] as? [String: Any] ?? [:] }) ?? [:]
        let turns = (try? withStore(at: root.appendingPathComponent("host-lifecycle"), fileName: "state.json", lockName: "state.lock") { data, _ in
            data["turns"] as? [String: [String: Any]] ?? [:]
        }) ?? [:]
        let sessions = ((try? locations(at: root)) ?? []).prefix(3).map { item -> Session in
            // A task can hold a window before assignment, or while the extension
            // is offline. Its stop button must not depend on an extension reply.
            let peer = (try? withStore(at: item.directory) { data, _ in data["peer"] as? [String: Any] ?? [:] }) ?? [:]
            let owner = (item.task["lease"] as? [String: Any])?["ownerId"] as? String
            let host = matchingHost(turns, channel: item.channel, taskId: item.taskId, ownerId: owner)?.1 ?? [:]
            return Session(channel: item.channel, taskId: item.taskId, peer: peer, task: item.task, host: host)
        }
        return Snapshot(rootPeer: rootPeer, sessions: sessions)
    }

    static func stop(_ selected: Session, at location: URL? = nil) throws {
        let root = location ?? directory
        guard let target = try locations(at: root).first(where: {
            $0.channel == selected.channel && $0.taskId == selected.taskId
        }), let owner = selected.ownerId else { throw StoreError.disconnected }
        // Task files publish by atomic replacement. Never wait for a task lock
        // held during native I/O to stop it; revoke the immutable task/owner/run
        // receipt under the host lifecycle lock. A later run has a different
        // receipt and cannot be stopped by this selection. Do not delete leases.
        try withStore(at: target.directory, fileName: "task-windows.json", locking: false) { data, _ in
            guard data["activeTaskId"] as? String == selected.taskId,
                  let task = (data["tasks"] as? [String: [String: Any]])?[selected.taskId],
                  !["ended", "interrupted", "invalidated"].contains(task["state"] as? String ?? ""),
                  (task["lease"] as? [String: Any])?["ownerId"] as? String == owner else { throw StoreError.disconnected }
            try withStore(at: root.appendingPathComponent("host-lifecycle"), fileName: "state.json", lockName: "state.lock") { state, dir in
                var turns = state["turns"] as? [String: [String: Any]] ?? [:]
                guard let (key, original) = matchingHost(turns, channel: selected.channel,
                                                       taskId: selected.taskId, ownerId: owner),
                      original["sessionId"] as? String == selected.hostSessionId,
                      original["requestId"] as? String == selected.host["requestId"] as? String else { throw StoreError.disconnected }
                if original["stoppedAt"] != nil { return }
                var turn = original
                let now = Date().timeIntervalSince1970
                turn["stoppedAt"] = now; turn["updatedAt"] = now
                turn["reason"] = "user_stopped_in_quietbridge"
                turns[key] = turn; state["turns"] = turns
                try replace(state, fileName: "state.json", directory: dir)
            }
        }
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
            try replace(data, fileName: "browser-events.json", directory: dir)
        }
    }

    enum StoreError: LocalizedError {
        case unavailable, disconnected, busy
        var errorDescription: String? {
            switch self {
            case .unavailable: return "无法安全读取任务状态，请稍后重试或检查 QuietBridge 安装状态。"
            case .disconnected: return "任务或宿主归属已变化，请刷新后核对对应会话。"
            case .busy: return "上一项操作尚未返回，请稍候。"
            }
        }
    }
}
