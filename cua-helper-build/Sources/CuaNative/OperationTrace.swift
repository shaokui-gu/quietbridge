// Geometry and delivery diagnostics only: never record text, AX contents or image bytes.
import Foundation
import Dispatch
import CoreGraphics
import Darwin

struct OperationTrace: Sendable {
    @TaskLocal static var current: OperationTrace?
    static let methods: Set<String> = ["click", "hover", "scroll", "drag", "getState", "screenshot", "visualState", "listWindows", "preview"]
    private static let log = OperationLog()
    let requestID: String
    let method: String
    private let started = ProcessInfo.processInfo.systemUptime

    init(requestID: String, method: String) { self.requestID = requestID; self.method = method }

    static var logPath: String { OperationLog.defaultPath }

    static func requestFields(_ params: JSONValue) -> [String: JSONValue] {
        let numbers: Set<String> = ["windowId", "elementIndex", "x", "y", "fromX", "fromY", "toX", "toY", "clickCount", "holdMs", "deltaLinesX", "deltaLinesY", "durationMs", "steps"]
        let identifiers: Set<String> = ["appHandle", "snapshotId", "mouseButton", "action", "source"]
        var fields: [String: JSONValue] = [:]
        for (key, value) in params.object ?? [:] {
            if numbers.contains(key), let number = value.number, number.isFinite { fields[key] = value }
            if identifiers.contains(key), let text = value.string { fields[key] = .string(String(text.prefix(256))) }
        }
        return fields
    }

    func encoded(_ event: String, fields: [String: JSONValue] = [:]) throws -> Data {
        var record = fields
        record["event"] = .string(event)
        record["requestId"] = .string(String(requestID.prefix(128)))
        record["method"] = .string(method)
        record["pid"] = .number(Double(getpid()))
        record["timestamp"] = .number(Date().timeIntervalSince1970)
        record["uptimeSeconds"] = .number(ProcessInfo.processInfo.systemUptime)
        record["elapsedMs"] = .number((ProcessInfo.processInfo.systemUptime - started) * 1000)
        var data = try JSONEncoder().encode(JSONValue.object(record))
        data.append(10)
        return data
    }

    func mark(_ event: String, fields: [String: JSONValue] = [:]) {
        guard ProcessInfo.processInfo.environment["CUA_OPERATION_LOG"] != "0",
              let data = try? encoded(event, fields: fields) else { return }
        Self.log.enqueue(data)
    }

    func flush() { Self.log.flush() }

    static func display(at point: CGPoint) -> JSONValue {
        var displayID: CGDirectDisplayID = 0
        var count: UInt32 = 0
        guard CGGetDisplaysWithPoint(point, 1, &displayID, &count) == .success, count == 1 else { return .null }
        let bounds = CGDisplayBounds(displayID)
        return .object(["displayId": .number(Double(displayID)),
                        "bounds": WindowBounds(x: bounds.minX, y: bounds.minY, width: bounds.width, height: bounds.height).json,
                        "pixelWidth": .number(Double(CGDisplayPixelsWide(displayID))),
                        "pixelHeight": .number(Double(CGDisplayPixelsHigh(displayID)))])
    }

    static func geometry(route: WindowRoute, point: WindowPoint) -> JSONValue {
        .object(["coordinateSpace": .string("window-points"),
                 "windowId": .number(Double(route.windowID)), "targetPid": .number(Double(route.owner.pid)),
                 "windowBounds": route.bounds.json,
                 "windowPoint": .object(["x": .number(point.x), "y": .number(point.y)]),
                 "screenPoint": .object(["x": .number(route.bounds.x + point.x), "y": .number(route.bounds.y + point.y)])])
    }
}

/// A file lock coordinates helpers sharing this path; failures never change operation results.
final class OperationLog: @unchecked Sendable {
    static var defaultPath: String {
        let env = ProcessInfo.processInfo.environment
        let directory: String
        if let override = env["CUA_DIAGNOSTICS_DIR"], !override.isEmpty { directory = override }
        else if let config = env["CUA_CONFIG_DIR"], !config.isEmpty { directory = config + "/logs" }
        else { directory = NSHomeDirectory() + "/Library/Logs/workbuddy-cua" }
        return directory + "/native-operations.jsonl"
    }

    private let path: String
    private let maxBytes: Int
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "cua.operation-log")

    init(path: String = OperationLog.defaultPath, maxBytes: Int = 5 * 1024 * 1024) {
        self.path = path
        self.maxBytes = maxBytes
    }

    func enqueue(_ data: Data) { queue.async { self.append(data) } }
    func flush() { queue.sync {} }

    func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        guard data.count <= maxBytes else { return }
        let parent = URL(fileURLWithPath: path).deletingLastPathComponent()
        do { try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700]) }
        catch { return }
        let lockFD = open(path + ".lock", O_WRONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0o600)
        guard lockFD >= 0 else { return }
        defer { close(lockFD) }
        var lockInfo = stat()
        guard fstat(lockFD, &lockInfo) == 0, lockInfo.st_mode & S_IFMT == S_IFREG,
              lockInfo.st_uid == geteuid(), lockInfo.st_nlink == 1 else { return }
        guard flock(lockFD, LOCK_EX) == 0 else { return }
        defer { _ = flock(lockFD, LOCK_UN) }
        var descriptor = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { return }
        defer { close(descriptor) }
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == geteuid(), info.st_nlink == 1 else { return }
        if info.st_size + Int64(data.count) > maxBytes {
            guard rename(path, path + ".1") == 0 else { return }
            close(descriptor)
            descriptor = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard descriptor >= 0 else { return }
        }
        data.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(descriptor, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { break }
                offset += count
            }
        }
    }
}
