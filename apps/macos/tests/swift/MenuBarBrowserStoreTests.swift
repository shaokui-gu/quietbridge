import CryptoKit
import Darwin
import Foundation
import Testing
@testable import QuietBridgeNative

struct MenuBarBrowserStoreTests {
    private func write(_ value: [String: Any], _ path: URL) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: value).write(to: path)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
    }

    private func read(_ path: URL) throws -> [String: Any] {
        try JSONSerialization.jsonObject(with: Data(contentsOf: path)) as! [String: Any]
    }

    private func liveSession(_ name: String, window: Int, root: URL) throws -> (channel: String, taskId: String, owner: String, fd: Int32) {
        let channel = SHA256.hash(data: Data(("workbuddy\0" + name).utf8)).map { String(format: "%02x", $0) }.joined()
        let directory = root.appendingPathComponent("sessions/\(channel)")
        let taskId = UUID().uuidString.lowercased(), owner = UUID().uuidString.lowercased()
        let task: [String: Any] = ["taskId": taskId, "state": "bound", "pid": 88, "windowId": window,
            "interactionMode": "background_browser", "lease": ["ownerId": owner,
                "expiresAt": Date().timeIntervalSince1970 + 120,
                "expiresAtUptime": ProcessInfo.processInfo.systemUptime + 120]]
        try write(["version": 1, "activeTaskId": taskId, "tasks": [taskId: task]], directory.appendingPathComponent("task-windows.json"))
        let fd = open(directory.appendingPathComponent("task-owner-\(owner).lock").path, O_RDWR | O_CREAT | O_EXCL, 0o600)
        #expect(fd >= 0)
        #expect(flock(fd, LOCK_EX | LOCK_NB) == 0)
        let bytes = Array(owner.utf8)
        #expect(bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) } == bytes.count)
        let registryURL = root.appendingPathComponent("session-registry.json")
        var registry = (try? read(registryURL)) ?? ["version": 1]
        var slots = registry["tasks"] as? [String: Any] ?? [:]
        slots[channel] = ["taskId": taskId, "taskFile": directory.appendingPathComponent("task-windows.json").path]
        registry["tasks"] = slots
        try write(registry, registryURL)
        let lifecycleURL = root.appendingPathComponent("host-lifecycle/state.json")
        var lifecycle = (try? read(lifecycleURL)) ?? ["version": 1]
        var turns = lifecycle["turns"] as? [String: Any] ?? [:]
        turns[name] = ["sessionId": name, "requestId": "turn-1", "taskId": taskId, "ownerId": owner,
                       "tasks": [taskId: owner]]
        lifecycle["turns"] = turns
        try write(lifecycle, lifecycleURL)
        return (channel, taskId, owner, fd)
    }

    @Test func offlineOwnerCanBeStoppedWithoutExtensionOrReleasingAnotherSession() throws {
        let root = URL(fileURLWithPath: "/private/tmp").appendingPathComponent("qb-stop-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let a = try liveSession("session-a", window: 274431, root: root)
        let b = try liveSession("session-b", window: 275157, root: root)
        defer { close(a.fd); close(b.fd) }
        let snapshot = try MenuBarBrowserStore.snapshot(at: root)
        let selected = try #require(snapshot.sessions.first { $0.channel == a.channel })
        #expect(selected.peer.isEmpty)
        #expect(selected.label.contains("274431"))
        #expect(selected.hostSessionId == "session-a")
        #expect(selected.canStop)
        let taskURL = root.appendingPathComponent("sessions/\(a.channel)/task-windows.json")
        let before = try Data(contentsOf: taskURL)
        // A native bind/status can hold this lock while blocked in I/O. The
        // person's stop must still reach the lifecycle watchdog immediately.
        let taskLock = open(taskURL.path + ".lock", O_RDWR | O_CREAT, 0o600)
        #expect(taskLock >= 0); #expect(flock(taskLock, LOCK_EX | LOCK_NB) == 0)
        defer { close(taskLock) }
        #expect(try MenuBarBrowserStore.snapshot(at: root).sessions.count == 2)
        try MenuBarBrowserStore.stop(selected, at: root)
        let state = try read(root.appendingPathComponent("host-lifecycle/state.json"))
        let turns = try #require(state["turns"] as? [String: [String: Any]])
        #expect(turns["session-a"]?["reason"] as? String == "user_stopped_in_quietbridge")
        #expect(turns["session-a"]?["stoppedAt"] is Double)
        #expect(turns["session-b"]?["stoppedAt"] == nil)
        #expect(try Data(contentsOf: taskURL) == before, "The worker must close its resources before the lease disappears")
        let stopped = try #require(MenuBarBrowserStore.snapshot(at: root).sessions.first { $0.channel == a.channel })
        #expect(stopped.stopping && !stopped.canStop)
        try MenuBarBrowserStore.stop(selected, at: root) // Idempotent, exact same selected run.
        #expect(try read(root.appendingPathComponent("host-lifecycle/state.json"))["turns"] is [String: Any])
    }

    @Test func stopRejectsChangedTaskOwnerAndNeverBypassesTheLifecycleLock() throws {
        let root = URL(fileURLWithPath: "/private/tmp").appendingPathComponent("qb-stop-race-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let a = try liveSession("session-a", window: 274431, root: root)
        defer { close(a.fd) }
        let selected = try #require(MenuBarBrowserStore.snapshot(at: root).sessions.first)
        let stateURL = root.appendingPathComponent("host-lifecycle/state.json")
        let before = try Data(contentsOf: stateURL)
        let lock = open(root.appendingPathComponent("host-lifecycle/state.lock").path, O_RDWR | O_CREAT, 0o600)
        #expect(lock >= 0); #expect(flock(lock, LOCK_EX | LOCK_NB) == 0)
        #expect(throws: (any Error).self) { try MenuBarBrowserStore.stop(selected, at: root) }
        close(lock)
        #expect(try Data(contentsOf: stateURL) == before)
        let taskURL = root.appendingPathComponent("sessions/\(a.channel)/task-windows.json")
        var store = try read(taskURL)
        var tasks = store["tasks"] as! [String: [String: Any]]
        tasks[a.taskId]?["lease"] = ["ownerId": UUID().uuidString.lowercased()]
        store["tasks"] = tasks
        try write(store, taskURL)
        #expect(throws: (any Error).self) { try MenuBarBrowserStore.stop(selected, at: root) }
        #expect(try Data(contentsOf: stateURL) == before)
    }

    @Test func staleSlotsAndUnprovenHostIdentitiesCannotBeStopped() throws {
        let root = URL(fileURLWithPath: "/private/tmp").appendingPathComponent("qb-stop-stale-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let a = try liveSession("session-a", window: 274431, root: root)
        let stateURL = root.appendingPathComponent("host-lifecycle/state.json")
        var state = try read(stateURL)
        var turns = state["turns"] as! [String: [String: Any]]
        turns["session-a"]?["sessionId"] = "unrelated-session"
        state["turns"] = turns
        try write(state, stateURL)
        let selected = try #require(MenuBarBrowserStore.snapshot(at: root).sessions.first)
        #expect(!selected.canStop)
        #expect(throws: (any Error).self) { try MenuBarBrowserStore.stop(selected, at: root) }
        close(a.fd)
        #expect(try MenuBarBrowserStore.snapshot(at: root).sessions.isEmpty, "A dead owner is not an active window holder")
    }

    @Test func panelCommandsRequireTheExplicitLiveSessionAndNeverFallBack() throws {
        let root = URL(fileURLWithPath: "/private/tmp").appendingPathComponent("qb-panel-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let keys = [String(repeating: "a", count: 64), String(repeating: "b", count: 64)]
        var slots: [String: Any] = [:]
        for key in keys {
            let location = root.appendingPathComponent("sessions/\(key)")
            let task = "task-\(key.prefix(1))"
            slots[key] = ["taskId": task, "taskFile": location.appendingPathComponent("task-windows.json").path]
            try write(["version": 1, "tasks": [task: ["state": "bound"]]], location.appendingPathComponent("task-windows.json"))
            try write(["version": 1, "peer": ["lastSeen": Date().timeIntervalSince1970,
                "enabled": true, "permission": true, "panelState": ["taskSummary": ["taskId": task]]]],
                location.appendingPathComponent("browser-events.json"))
        }
        try write(["version": 1, "tasks": slots], root.appendingPathComponent("session-registry.json"))
        #expect(try MenuBarBrowserStore.snapshot(at: root).sessions.count == 2)
        try MenuBarBrowserStore.submit("stop_task", channel: keys[0], taskId: "task-a", at: root)
        let first = root.appendingPathComponent("sessions/\(keys[0])/browser-events.json")
        let second = root.appendingPathComponent("sessions/\(keys[1])/browser-events.json")
        let a = try JSONSerialization.jsonObject(with: Data(contentsOf: first)) as! [String: Any]
        let b = try JSONSerialization.jsonObject(with: Data(contentsOf: second)) as! [String: Any]
        #expect(((a["peer"] as? [String: Any])?["panelCommand"] as? [String: Any])?["taskId"] as? String == "task-a")
        #expect((b["peer"] as? [String: Any])?["panelCommand"] == nil)
        try write(["version": 1, "tasks": ["task-a": ["state": "ended"]]],
            root.appendingPathComponent("sessions/\(keys[0])/task-windows.json"))
        #expect(try MenuBarBrowserStore.snapshot(at: root).sessions.map(\.channel) == [keys[1]])
        #expect(throws: (any Error).self) {
            try MenuBarBrowserStore.submit("capture_review", channel: keys[0], taskId: "task-a", at: root)
        }
        // A registry entry cannot redirect a channel to another session's files.
        slots[keys[1]] = ["taskId": "task-b", "taskFile": root.appendingPathComponent("sessions/\(keys[0])/task-windows.json").path]
        try write(["version": 1, "tasks": slots], root.appendingPathComponent("session-registry.json"))
        #expect(try MenuBarBrowserStore.snapshot(at: root).sessions.isEmpty)
    }

    @Test func cursorIPCUsesTheOriginalAnimationAndIdleClock() throws {
        var state = CursorState()
        state.move(to: .init(x: 20, y: 40), windowID: 3, pulse: true, now: 100)
        state.move(to: .init(x: 120, y: 90), windowID: 3, pulse: true, now: 101)
        let mirrored = try JSONDecoder().decode(CursorState.self, from: JSONEncoder().encode(state))
        #expect(mirrored.frame(at: 101.15) == state.frame(at: 101.15))
        #expect(mirrored.frame(at: 101.5) == state.frame(at: 101.5))
        #expect(mirrored.frame(at: 132) == nil)
    }
}
