// Preview hosts and input targets share ordering mechanics, while retaining separate identities.
import AppKit

enum PreviewHostOrdering {
    typealias Window = WindowOverlayOrdering.Window
    typealias Placement = WindowOverlayOrdering.Placement

    static func plan(host: PreviewHostWindow, panelIDs: [UInt32], panelPID: Int32,
                     windows: [Window]) -> [Placement]? {
        WindowOverlayOrdering.plan(target: .init(windowID: host.windowID, pid: host.pid),
            panelIDs: panelIDs, panelPID: panelPID, windows: windows)
    }

    @MainActor static func restore(panels: [NSPanel], host: PreviewHostWindow) -> Bool {
        WindowOverlayOrdering.restore(panels: panels, target: .init(windowID: host.windowID, pid: host.pid))
    }

    @MainActor static func restoreStatus(panels: [NSPanel], host: PreviewHostWindow) -> WindowOverlayOrdering.Status {
        WindowOverlayOrdering.restoreStatus(panels: panels, target: .init(windowID: host.windowID, pid: host.pid))
    }
}
