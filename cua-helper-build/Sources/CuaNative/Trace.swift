// Append-only forensic log. Every privacy/lock decision writes one line here so
// a run can be reconstructed even when stderr is not captured by the launcher.
import Darwin
import Foundation

enum Trace {
    static let path = "/tmp/cua-trace.log"

    private static let lock = NSLock()

    static func log(_ message: String) {
        let stamp = String(format: "%.3f", ProcessInfo.processInfo.systemUptime)
        let line = "[\(stamp)] \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        lock.lock()
        defer { lock.unlock() }
        let descriptor = open(path, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard descriptor >= 0 else { return }
        _ = data.withUnsafeBytes { Darwin.write(descriptor, $0.baseAddress, $0.count) }
        close(descriptor)
    }
}
