import Foundation
import Darwin

/// Product naming and the explicit compatibility boundary for existing installs.
enum QuietBridgeIdentity {
    static let displayName = "QuietBridge"
    static let menuIdentifier = "com.quietbridge.app"
    static let helperIdentifier = "com.quietbridge.helper"
    static let pluginIdentifier = "com.quietbridge.AuthorizationPlugin"
    static let legacyPluginIdentifier = "com.workbuddy.cua.AuthorizationPlugin"
    static let teamIdentifier = "62RNP54C47"
    static let pluginDirectory = "/Library/Application Support/WorkBuddyCUAAuthorizationPlugin"
    static let pluginPath = "/Library/Security/SecurityAgentPlugins/WorkBuddyCUAAuthorizationPlugin.bundle"
    static let helperReleases = "/Library/Application Support/WorkBuddyCUAHelper/releases/"
    static var applicationRuntime: String { "/tmp/workbuddy-cua-\(getuid())" }
    static var authorizationSocketDirectory: String { "/tmp/com.workbuddy.cua.\(getuid())" }
    static let previousOnboardingKey = "workbuddyCUA.permissionOnboardingPresented.v1"

    static func environment(_ name: String) -> String? {
        let values = ProcessInfo.processInfo.environment
        return values[name] ?? values[name.replacingOccurrences(of: "QUIETBRIDGE_", with: "CUA_")]
    }

    static func userRuntime(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        let base = home.appendingPathComponent("Library/Application Support")
        let legacy = base.appendingPathComponent("workbuddy-cua")
        return FileManager.default.fileExists(atPath: legacy.path) ? legacy : base.appendingPathComponent("QuietBridge")
    }

    static var browserConfiguration: URL {
        if let path = environment("QUIETBRIDGE_CONFIG_DIR"), !path.isEmpty { return URL(fileURLWithPath: path) }
        let base = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        let legacy = base.appendingPathComponent("WorkbuddyExtension")
        return FileManager.default.fileExists(atPath: legacy.path)
            ? legacy.appendingPathComponent("cua-helper-config")
            : base.appendingPathComponent("QuietBridgeBrowser/quietbridge-config")
    }
}
