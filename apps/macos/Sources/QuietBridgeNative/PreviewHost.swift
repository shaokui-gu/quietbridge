// The capture target and the WorkBuddy window hosting its preview are independent.
import AppKit

@MainActor
final class PreviewHostFollowLoop {
    private var timer: Timer?

    func start(_ update: @escaping @MainActor @Sendable () -> Void) {
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 1.0 / 30, repeats: true) { _ in
            // Execute inside the tracking run loop, without an async main-queue hop.
            MainActor.assumeIsolated { update() }
        }
        timer.tolerance = 0.002
        RunLoop.main.add(timer, forMode: .common)
        RunLoop.main.add(timer, forMode: .eventTracking)
        self.timer = timer
    }

    func stop() { timer?.invalidate(); timer = nil }
}

struct PreviewHostWindow: Equatable {
    let windowID: UInt32
    let pid: Int32
    let bounds: CGRect
    let isOnScreen: Bool

    static func select(_ candidates: [Self], hostPIDs: Set<Int32>, responsiblePID: Int32?,
                       previous: Self?) -> Self? {
        let eligible = candidates.filter {
            $0.windowID != 0 && $0.bounds.width > 300 && $0.bounds.height > 200 &&
            (hostPIDs.isEmpty ? $0.pid == responsiblePID : hostPIDs.contains($0.pid))
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
    static let inset: CGFloat = 16

    static func frame(for panel: CGRect, hostBounds: CGRect,
                      mainDisplayHeight: CGFloat, visibleFrame: CGRect) -> CGRect? {
        guard panel.width.isFinite, panel.height.isFinite, panel.width > 0, panel.height > 0 else { return nil }
        let host = CGRect(x: hostBounds.minX, y: mainDisplayHeight - hostBounds.maxY,
                          width: hostBounds.width, height: hostBounds.height)
        // Fit the picture inside both the host and its display. A tall source or
        // a shrinking host may require going below the usual minimum drag width.
        let available = host.insetBy(dx: inset, dy: inset).intersection(visibleFrame)
        guard !available.isNull, available.width > 0, available.height > 0 else { return nil }
        let aspect = panel.height / panel.width
        let width = min(panel.width, available.width, available.height / aspect)
        let size = CGSize(width: width, height: width * aspect)
        return CGRect(x: available.maxX - size.width, y: available.maxY - size.height,
                      width: size.width, height: size.height)
    }

    /// Temporary moves can leave the host's bounds, but stay on its display.
    /// A resize retains the temporary top-right while respecting the fitted size.
    static func temporaryFrame(_ frame: CGRect, fittedSize: CGSize, visibleFrame: CGRect) -> CGRect {
        CGRect(x: min(max(frame.maxX - fittedSize.width, visibleFrame.minX), visibleFrame.maxX - fittedSize.width),
               y: min(max(frame.maxY - fittedSize.height, visibleFrame.minY), visibleFrame.maxY - fittedSize.height),
               width: fittedSize.width, height: fittedSize.height)
    }
}

/// Uses monotonic time. Host identity or geometry changes invalidate the move,
/// even when the slow host resolver has already refreshed its cached window.
struct PreviewTemporaryPlacement {
    static let duration: TimeInterval = 30
    private var frame: CGRect?
    private var host: PreviewHostWindow?
    private var deadline: TimeInterval?
    private var dragging = false

    var isActive: Bool { frame != nil }

    mutating func drag(to frame: CGRect, host: PreviewHostWindow) {
        self.frame = frame
        self.host = host
        deadline = nil
        dragging = true
    }

    mutating func finishDrag(now: TimeInterval) {
        guard dragging else { return }
        dragging = false
        deadline = now + Self.duration
    }

    mutating func resolve(host: PreviewHostWindow, now: TimeInterval) -> CGRect? {
        guard let frame else { return nil }
        guard let bound = self.host, bound.pid == host.pid, bound.windowID == host.windowID,
              bound.bounds == host.bounds, dragging || now < (deadline ?? 0) else {
            reset()
            return nil
        }
        return frame
    }

    mutating func resize(to frame: CGRect, host: PreviewHostWindow, now: TimeInterval) {
        if resolve(host: host, now: now) != nil { self.frame = frame }
    }

    func remainingSeconds(now: TimeInterval) -> TimeInterval? {
        deadline.map { max(0, $0 - now) }
    }

    mutating func reset() {
        frame = nil
        host = nil
        deadline = nil
        dragging = false
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
