import Foundation
import Testing
@testable import QuietBridgeNative

struct MenuBarBrowserStoreTests {
    private func write(_ value: [String: Any], _ path: URL) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: value).write(to: path)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
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
