// The menu application owns this kernel lock. Closing it revokes every worker's
// session, including when the app crashes or is force-quit.
import Foundation
import Darwin

struct ApplicationSession: Sendable, Equatable {
    // Version 2 requires task resource ownership/idle cleanup. Old helpers only
    // understand a bare UUID and must fail closed after an application upgrade.
    private struct Record: Codable { let version: Int; let sessionId: String }
    static var directory: String { "/tmp/workbuddy-cua-\(getuid())" }
    let directory: String
    let id: String

    static func current(in directory: String = Self.directory) -> ApplicationSession? {
        var directoryInfo = stat()
        guard lstat(directory, &directoryInfo) == 0, directoryInfo.st_mode & S_IFMT == S_IFDIR,
              directoryInfo.st_uid == getuid(), directoryInfo.st_mode & 0o777 == 0o700 else { return nil }
        let fd = open("\(directory)/application.lock", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o777 == 0o600 else { return nil }
        if flock(fd, LOCK_SH | LOCK_NB) == 0 {
            flock(fd, LOCK_UN)
            return nil // A leftover file does not mean the application is running.
        }
        guard errno == EWOULDBLOCK else { return nil }
        var bytes = [UInt8](repeating: 0, count: 256)
        let count = read(fd, &bytes, bytes.count)
        guard count > 0, count < bytes.count,
              let record = try? JSONDecoder().decode(Record.self, from: Data(bytes.prefix(count))),
              record.version == 2, UUID(uuidString: record.sessionId) != nil else { return nil }
        return ApplicationSession(directory: directory, id: record.sessionId)
    }

    static func encoded(_ id: String) throws -> Data {
        try JSONEncoder().encode(Record(version: 2, sessionId: id))
    }

    var isActive: Bool { Self.current(in: directory) == self }
    func requireActive() throws {
        guard isActive else { throw Self.unavailable }
    }
    static var unavailable: NativeFailure {
        NativeFailure(code: "APP_NOT_RUNNING", message: "Workbuddy CUA 已退出，请先打开应用；本次操作未执行")
    }
}

final class ApplicationRuntimeOwner {
    let session: ApplicationSession
    private var descriptor: Int32

    init(directory: String = ApplicationSession.directory) throws {
        if mkdir(directory, 0o700) != 0 && errno != EEXIST { throw SharedSocket.failure("Creating application runtime") }
        var info = stat()
        guard lstat(directory, &info) == 0, info.st_mode & S_IFMT == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o777 == 0o700 else { throw ApplicationSession.unavailable }
        let fd = open("\(directory)/application.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw SharedSocket.failure("Opening application runtime lock") }
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o777 == 0o600,
              flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            throw NativeFailure(code: "APP_ALREADY_RUNNING", message: "Workbuddy CUA is already running or its runtime is unavailable")
        }
        session = ApplicationSession(directory: directory, id: UUID().uuidString)
        let bytes: [UInt8]
        do { bytes = Array(try ApplicationSession.encoded(session.id)) }
        catch { close(fd); throw error }
        guard ftruncate(fd, 0) == 0, bytes.withUnsafeBytes({ write(fd, $0.baseAddress, $0.count) }) == bytes.count else {
            close(fd)
            throw SharedSocket.failure("Writing application runtime session")
        }
        descriptor = fd
    }

    func stop() {
        guard descriptor >= 0 else { return }
        close(descriptor)
        descriptor = -1
    }
    deinit { stop() }
}
