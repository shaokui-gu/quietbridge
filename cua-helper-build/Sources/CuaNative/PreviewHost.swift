// The capture target and the WorkBuddy window hosting its preview are independent.
import CoreGraphics

struct PreviewHostWindow: Equatable {
    let windowID: UInt32
    let pid: Int32
    let bounds: CGRect
    let isOnScreen: Bool

    static func select(_ candidates: [Self], workBuddyPIDs: Set<Int32>, responsiblePID: Int32?,
                       previous: Self?) -> Self? {
        let eligible = candidates.filter {
            $0.windowID != 0 && $0.bounds.width > 300 && $0.bounds.height > 200 &&
            (workBuddyPIDs.isEmpty ? $0.pid == responsiblePID : workBuddyPIDs.contains($0.pid))
        }
        // Keep the same host even while it is moving through an inactive Space.
        if let previous, let retained = eligible.first(where: {
            $0.windowID == previous.windowID && $0.pid == previous.pid
        }) { return retained }
        let visible = eligible.filter(\.isOnScreen)
        return (visible.isEmpty ? eligible : visible).max {
            let left = $0.bounds.width * $0.bounds.height, right = $1.bounds.width * $1.bounds.height
            return left == right ? $0.windowID > $1.windowID : left < right
        }
    }
}

enum PreviewHostRoute: Equatable {
    case hidden
    case host([UInt64])

    static func decide(host: PreviewHostWindow?, membership: WindowSpaceMembership?, held: Bool,
                       source: PreviewSource, targetOnCurrentSpace: Bool?) -> Self {
        guard let host, let membership,
              case .target(let destination) = CursorSpaceRoute.decide(windowID: host.windowID, membership: membership),
              host.isOnScreen || membership.onCurrentSpace == false,
              held || source.usesDisplay || targetOnCurrentSpace == false else { return .hidden }
        return .host(destination)
    }
}

enum PreviewHostGeometry {
    static func frame(for panel: CGRect, hostBounds: CGRect, previousHostBounds: CGRect?,
                      manuallyPositioned: Bool, mainDisplayHeight: CGFloat, visibleFrame: CGRect) -> CGRect {
        let host = CGRect(x: hostBounds.minX, y: mainDisplayHeight - hostBounds.maxY,
                          width: hostBounds.width, height: hostBounds.height)
        var origin = CGPoint(x: host.maxX - 16 - panel.width, y: host.maxY - 16 - panel.height)
        if manuallyPositioned, let previousHostBounds {
            origin = CGPoint(x: panel.minX + hostBounds.minX - previousHostBounds.minX,
                             y: panel.minY - hostBounds.minY + previousHostBounds.minY)
        }
        // Use the host's display, never NSScreen.main (which follows keyboard focus).
        origin.x = min(max(origin.x, visibleFrame.minX), max(visibleFrame.minX, visibleFrame.maxX - panel.width))
        origin.y = min(max(origin.y, visibleFrame.minY), max(visibleFrame.minY, visibleFrame.maxY - panel.height))
        return CGRect(origin: origin, size: panel.size)
    }
}

enum PreviewCaptureRoute: Equatable {
    case capture(PreviewSource)
    case waitingForTargetDesktop

    static func decide(source: PreviewSource, targetOnCurrentSpace: Bool?) -> Self {
        switch source {
        case .window: .capture(.window)
        case .application: .capture(targetOnCurrentSpace == true ? .application : .window)
        case .desktop: targetOnCurrentSpace == true ? .capture(.desktop) : .waitingForTargetDesktop
        }
    }
}
