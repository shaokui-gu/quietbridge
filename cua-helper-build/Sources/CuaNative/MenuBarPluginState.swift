import Foundation

enum MenuBarPluginState: Sendable, Equatable {
    case missing, available, disabled, needsRepair

    static func classify(reason: String, hasArtifacts: Bool) -> Self {
        if reason == "ok" { return .available }
        if reason == "disabled" { return .disabled }
        if reason == "config-missing(errno 2)" && !hasArtifacts { return .missing }
        return .needsRepair
    }

    static func inspect() -> Self {
        let reason = LockedUseAuthorizationServer.shared.availabilityReason()
        let artifacts = [
            "/Library/Application Support/WorkBuddyCUAAuthorizationPlugin",
            "/Library/Security/SecurityAgentPlugins/WorkBuddyCUAAuthorizationPlugin.bundle",
        ].contains { FileManager.default.fileExists(atPath: $0) }
        return classify(reason: reason, hasArtifacts: artifacts)
    }

    var detail: String {
        switch self {
        case .missing: return "允许在锁屏状态下继续使用"
        case .available: return "允许在锁屏状态下继续使用"
        case .disabled: return "插件已停用，可在管理中启用"
        case .needsRepair: return "需恢复，请先在管理中查看状态"
        }
    }
}
