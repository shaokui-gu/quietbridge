// AX traversal/input and still capture live in a disposable process per client.
// The broker owns only preview streams, privacy participation and IPC admission.
import Foundation
import AppKit
import Darwin

final class WorkerChannel: @unchecked Sendable {
    let socket: SharedSocket
    let process: Process
    let configuration: HelperConfiguration
    private let queue = DispatchQueue(label: "quietbridge.execution-worker")
    private let onPreviewCursor: @Sendable (JSONValue) -> Void

    init(configuration: HelperConfiguration, onPreviewCursor: @escaping @Sendable (JSONValue) -> Void) throws {
        self.configuration = configuration
        self.onPreviewCursor = onPreviewCursor
        var descriptors: [Int32] = [0, 0]
        guard socketpair(AF_UNIX, SOCK_STREAM, 0, &descriptors) == 0 else {
            throw SharedSocket.failure("Creating execution worker channel")
        }
        socket = SharedSocket(descriptor: descriptors[0])
        let child = FileHandle(fileDescriptor: descriptors[1], closeOnDealloc: true)
        process = Process()
        guard let executable = NativePlatform.executablePath(getpid()) else {
            throw SharedSocket.failure("Resolving execution worker")
        }
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = ["--execution-worker",
            "--max-request-bytes", String(configuration.maxRequestBytes),
            "--max-response-bytes", String(configuration.maxResponseBytes),
            "--ax-timeout-ms", String(configuration.axTimeoutSeconds * 1000),
            "--text-event-interval-ms", String(configuration.textEventIntervalSeconds * 1000),
            "--max-click-hold-ms", String(configuration.maxClickHoldMs),
            "--timeout-ms", String(configuration.timeoutSeconds * 1000),
            "--screenshot-timeout-ms", String(configuration.screenshotTimeoutSeconds * 1000),
            "--shutdown-grace-ms", String(configuration.shutdownGraceSeconds * 1000)]
        process.standardInput = child
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.standardError
        try process.run()
        try child.close()
    }

    func execute(_ request: NativeRequest) async throws -> JSONValue {
        let data = try JSONEncoder().encode(JSONValue.object([
            "id": .string(request.id), "method": .string(request.method), "params": request.params]))
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                queue.async { [self] in
                    do {
                        try socket.write(data + Data([10]), timeout: configuration.timeoutSeconds)
                        let deadline = ProcessInfo.processInfo.systemUptime + configuration.timeoutSeconds + 1
                        var buffer = Data()
                        while true {
                            guard ProcessInfo.processInfo.systemUptime < deadline else {
                                throw NativeFailure(code: "NATIVE_TIMEOUT", message: "本会话原生执行进程超时；其他会话不受影响")
                            }
                            var item = pollfd(fd: socket.descriptor, events: Int16(POLLIN), revents: 0)
                            let ready = poll(&item, 1, 100)
                            if ready < 0 && errno == EINTR { continue }
                            if ready == 0 { continue }
                            guard ready > 0 else { throw SharedSocket.failure("Reading execution worker") }
                            var bytes = [UInt8](repeating: 0, count: 65536)
                            let count = bytes.withUnsafeMutableBytes { Darwin.read(socket.descriptor, $0.baseAddress, $0.count) }
                            if count < 0 && errno == EINTR { continue }
                            guard count > 0 else {
                                throw NativeFailure(code: "NATIVE_WORKER_EXITED", message: "本会话原生执行进程已退出；先核实既有结果，不重放输入")
                            }
                            buffer.append(contentsOf: bytes.prefix(count))
                            guard buffer.count <= configuration.maxResponseBytes else {
                                throw NativeFailure(code: "NATIVE_RESPONSE_LIMIT", message: "Execution worker response exceeded limit")
                            }
                            while let newline = buffer.firstIndex(of: 10) {
                                let frame = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                                let reply = try JSONDecoder().decode(JSONValue.self, from: frame)
                                if reply["type"] == .string("diagnostic") { continue }
                                guard reply["id"] == .string(request.id) else {
                                    throw NativeFailure(code: "NATIVE_PROTOCOL", message: "Execution worker response ID mismatch")
                                }
                                if let code = reply["error"]["code"].string {
                                    throw NativeFailure(code: code, message: reply["error"]["message"].string ?? code)
                                }
                        if reply.object?["previewCursor"] != nil { onPreviewCursor(reply["previewCursor"]) }
                        continuation.resume(returning: reply["result"])
                                return
                            }
                        }
                    } catch {
                        if let failure = error as? NativeFailure, failure.code == "NATIVE_IO" {
                            continuation.resume(throwing: NativeFailure(code: "NATIVE_WORKER_EXITED",
                                message: "本会话原生执行通道已断开；先核实既有结果，不重放输入"))
                        } else { continuation.resume(throwing: error) }
                    }
                }
            }
        } onCancel: { [self] in stop() }
    }

    func stop() {
        socket.shutdown()
        // EOF first lets cooperative input/clipboard cleanup run. A blocked AX
        // thread still has a strict kill deadline and cannot block the broker.
        let process = self.process
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + configuration.shutdownGraceSeconds) {
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
    }
    deinit { stop() }
}

@MainActor enum ExecutionWorkerMain {
    static func run(configuration: HelperConfiguration) {
        SpacePreview.enabled = false
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let channel = SharedSocket(descriptor: STDIN_FILENO)
        let cancellation = CancellationLatch()
        let coordinator = RequestCoordinator(configuration: configuration, capture: WindowCapture(),
            channel: channel, cancellation: cancellation, clientID: UUID(),
            resourceLease: TaskResourceLease { _, _, _ in }, scheduler: RequestScheduler(),
            writer: JSONLWriter(limit: configuration.maxResponseBytes, channel: channel,
                                writeTimeout: configuration.shutdownGraceSeconds),
            permissions: SystemPermissions(), executionWorker: true,
            onClose: { exit(0) }, onFatal: { _, _ in _exit(1) })
        coordinator.start()
        withExtendedLifetime(coordinator) { app.run() }
    }
}
