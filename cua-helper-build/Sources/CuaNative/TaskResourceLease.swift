// Only the socket owning task resources may renew or release them. Host pings
// and extension heartbeats never renew this independent native deadline.
import Foundation

@MainActor final class TaskResourceLease {
    enum Resource: String, Hashable { case preview, protection }
    static let idleSeconds: TimeInterval = 120
    private(set) var owner: UUID?
    private var resources: Set<Resource> = []
    private var expiresAt: TimeInterval = 0
    private var revoked: Set<UUID> = []
    private var timer: DispatchSourceTimer?
    private let now: () -> TimeInterval
    private let onRevoke: (UUID, Set<Resource>, String) -> Void

    init(now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         onRevoke: @escaping (UUID, Set<Resource>, String) -> Void) {
        self.now = now
        self.onRevoke = onRevoke
    }

    func require(_ client: UUID) throws {
        expireIfNeeded()
        guard !revoked.contains(client) else {
            throw NativeFailure(code: "TASK_INTERRUPTED", message: "任务租约已过期；需创建新任务，不能恢复旧输入")
        }
        guard owner == nil || owner == client else {
            throw NativeFailure(code: "TASK_IN_USE", message: "共享资源属于另一个连接中的任务，未接管")
        }
    }

    func acquire(_ client: UUID, resource: Resource) throws {
        try require(client)
        let added = !resources.contains(resource)
        owner = client
        expiresAt = now() + Self.idleSeconds
        resources.insert(resource)
        if added { record("task.resource_acquired", client: client, held: [resource]) }
        touch(client)
    }

    func touch(_ client: UUID) {
        expireIfNeeded()
        guard owner == client else { return }
        expiresAt = now() + Self.idleSeconds
        if timer == nil {
            let source = DispatchSource.makeTimerSource(queue: .main)
            source.schedule(deadline: .now() + .milliseconds(500), repeating: .milliseconds(500))
            source.setEventHandler { [weak self] in
                MainActor.assumeIsolated { self?.expireIfNeeded() }
            }
            timer = source
            source.resume()
        }
    }

    @discardableResult func release(_ client: UUID, resource: Resource) -> Bool {
        guard owner == client, resources.remove(resource) != nil else { return false }
        record("task.resource_released", client: client, held: [resource])
        if resources.isEmpty { clear() }
        return true
    }

    func disconnect(_ client: UUID) {
        if owner == client { revoke(reason: "client-disconnected") }
        revoked.remove(client)
    }

    func expireIfNeeded() {
        if owner != nil, now() >= expiresAt { revoke(reason: "idle-timeout") }
    }

    private func revoke(reason: String) {
        guard let client = owner else { return }
        let held = resources
        record("task.resources_revoked", client: client, held: held, reason: reason)
        revoked.insert(client)
        clear()
        onRevoke(client, held, reason)
    }

    private func record(_ event: String, client: UUID, held: Set<Resource>, reason: String? = nil) {
        var fields: [String: JSONValue] = ["clientId": .string(client.uuidString),
            "resources": .array(held.map(\.rawValue).sorted().map(JSONValue.string)),
            "idleSeconds": .number(Self.idleSeconds)]
        if let reason { fields["reason"] = .string(reason) }
        OperationTrace(requestID: "task-lease", method: "preview").mark(event, fields: fields)
    }

    private func clear() {
        owner = nil
        resources.removeAll()
        timer?.cancel()
        timer = nil
    }
}
