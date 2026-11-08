// Task preview and session protection have separate native deadlines. Only
// actual task activity touches both; a protection renewal cannot renew preview.
import Foundation

@MainActor final class TaskResourceLease {
    enum Resource: String, Hashable { case preview, protection }
    static let idleSeconds: TimeInterval = 120
    private struct Entry {
        var resources: Set<Resource> = []
        var expiresAt: [Resource: TimeInterval] = [:]
    }
    private var entries: [UUID: Entry] = [:]
    static let maximumPreviews = 3
    var previewCount: Int { entries.values.filter { $0.resources.contains(.preview) }.count }
    var owner: UUID? {
        entries.first(where: { $0.value.resources.contains(.preview) })?.key
            ?? (entries.count == 1 ? entries.first?.key : nil)
    }
    var protectionCount: Int { entries.values.filter { $0.resources.contains(.protection) }.count }
    private var revoked: Set<UUID> = []
    private var timer: DispatchSourceTimer?
    private var expiring = false
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
    }

    func acquire(_ client: UUID, resource: Resource) throws {
        expireIfNeeded()
        if resource == .preview { try require(client) }
        if resource == .preview && !(entries[client]?.resources.contains(.preview) ?? false) && previewCount >= Self.maximumPreviews {
            throw NativeFailure(code: "SESSION_CAPACITY_FULL", message: "最多同时共享 3 个会话；未接管现有共享")
        }
        guard !revoked.contains(client) else {
            throw NativeFailure(code: "TASK_INTERRUPTED", message: "任务租约已过期；需创建新连接，不能恢复旧输入")
        }
        var entry = entries[client] ?? Entry()
        let added = !entry.resources.contains(resource)
        entry.expiresAt[resource] = now() + Self.idleSeconds
        entry.resources.insert(resource)
        entries[client] = entry
        if added { record("task.resource_acquired", client: client, held: [resource]) }
        startTimer()
    }

    func touch(_ client: UUID) {
        expireIfNeeded()
        guard var entry = entries[client] else { return }
        for resource in entry.resources { entry.expiresAt[resource] = now() + Self.idleSeconds }
        entries[client] = entry
        startTimer()
    }

    private func startTimer() {
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
        guard var entry = entries[client], entry.resources.remove(resource) != nil else { return false }
        entry.expiresAt.removeValue(forKey: resource)
        entries[client] = entry.resources.isEmpty ? nil : entry
        record("task.resource_released", client: client, held: [resource])
        stopTimerIfEmpty()
        return true
    }

    func disconnect(_ client: UUID) {
        if let entry = entries.removeValue(forKey: client) {
            record("task.resources_revoked", client: client, held: entry.resources, reason: "client-disconnected")
            stopTimerIfEmpty()
            onRevoke(client, entry.resources, "client-disconnected")
        }
        revoked.remove(client)
    }

    func expireIfNeeded() {
        guard !expiring else { return }
        expiring = true
        defer { expiring = false }
        // Notify after each removal: only the final protection participant
        // observes a zero count, even when several expire in the same tick.
        for client in Array(entries.keys) {
            guard var entry = entries[client] else { continue }
            let expired = Set(entry.resources.filter { now() >= (entry.expiresAt[$0] ?? 0) })
            guard !expired.isEmpty else { continue }
            entry.resources.subtract(expired)
            for resource in expired { entry.expiresAt.removeValue(forKey: resource) }
            entries[client] = entry.resources.isEmpty ? nil : entry
            if entry.resources.isEmpty { revoked.insert(client) }
            record("task.resources_revoked", client: client, held: expired, reason: "idle-timeout")
            onRevoke(client, expired, "idle-timeout")
        }
        stopTimerIfEmpty()
    }

    private func record(_ event: String, client: UUID, held: Set<Resource>, reason: String? = nil) {
        var fields: [String: JSONValue] = ["clientId": .string(client.uuidString),
            "resources": .array(held.map(\.rawValue).sorted().map(JSONValue.string)),
            "idleSeconds": .number(Self.idleSeconds),
            "protectionParticipants": .number(Double(protectionCount))]
        if let reason { fields["reason"] = .string(reason) }
        OperationTrace(requestID: "task-lease", method: "preview").mark(event, fields: fields)
    }

    private func stopTimerIfEmpty() {
        guard entries.isEmpty else { return }
        timer?.cancel()
        timer = nil
    }
}
