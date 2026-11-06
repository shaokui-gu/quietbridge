// In-memory capability switches for one menu-bar process.
// Absence of that process means "unchanged": callers keep their existing behavior.
import Foundation

struct CapabilitySwitches: Equatable, Sendable {
    static let keys = ["master", "native_observe", "native_input", "app_launch", "browser", "preview", "operation_log", "locked_use"]
    var master = true
    var nativeObserve = true
    var nativeInput = true
    var appLaunch = true
    var browser = true
    var preview = true
    var operationLog = true
    // Off until the person turns it on. The plug-in is installed separately;
    // this switch only decides whether an agent may use that capability.
    var lockedUse = false

    func enabled(_ key: String) -> Bool {
        guard master || key == "master" else { return false }
        switch key {
        case "master": return master
        case "native_observe": return nativeObserve
        case "native_input": return nativeInput
        case "app_launch": return appLaunch
        case "browser": return browser
        case "preview": return preview
        case "operation_log": return operationLog
        case "locked_use": return lockedUse
        default: return false
        }
    }

    mutating func set(_ key: String, _ value: Bool) -> Bool {
        guard Self.keys.contains(key) else { return false }
        switch key {
        case "master": master = value
        case "native_observe": nativeObserve = value
        case "native_input": nativeInput = value
        case "app_launch": appLaunch = value
        case "browser": browser = value
        case "preview": preview = value
        case "operation_log": operationLog = value
        case "locked_use": lockedUse = value
        default: return false
        }
        return true
    }

    var snapshot: [String: Bool] { Dictionary(uniqueKeysWithValues: Self.keys.map { ($0, enabled($0)) }) }
}

enum CapabilityRouting {
    static func key(method: String, action: String?) -> String? {
        switch method {
        case "getState", "visualState", "screenshot", "listWindows", "listApps", "findApps", "getApp":
            return "native_observe"
        case "click", "hover", "scroll", "drag", "typeText", "paste", "pressKey", "setValue", "selectText", "activate", "show":
            return "native_input"
        case "launch": return "app_launch"
        case "browserSession": return action == "list" || action == "prepare" ? nil : "app_launch"
        case "preview": return action == "hold" ? "preview" : nil
        default: return nil
        }
    }
}

final class CapabilityControl: @unchecked Sendable {
    static let shared = CapabilityControl()
    private let lock = NSLock()
    private var switches = CapabilitySwitches()

    func snapshot() -> [String: Bool] { lock.withLock { switches.snapshot } }
    func set(_ key: String, _ value: Bool) -> [String: Bool]? {
        lock.withLock {
            guard switches.set(key, value) else { return nil }
            return switches.snapshot
        }
    }
}
