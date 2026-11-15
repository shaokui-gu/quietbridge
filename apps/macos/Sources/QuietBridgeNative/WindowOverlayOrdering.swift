// Keep helper-owned overlays immediately above their bound external window.
import AppKit

enum WindowOverlayOrdering {
    enum Status: String {
        case verified, repairing, unavailable
    }

    struct Target: Equatable {
        let windowID: UInt32
        let pid: Int32
    }

    struct Window: Equatable {
        let id: UInt32
        let pid: Int32
        let level: Int
    }

    struct Placement: Equatable {
        let windowID: UInt32
        let relativeTo: UInt32
    }

    /// WindowServer lists front to back; the cards are supplied back to front.
    /// Only the overlays may move. Never raise the target or use relativeTo=0 (global front).
    static func plan(target: Target, panelIDs: [UInt32], panelPID: Int32,
                     windows: [Window]) -> [Placement]? {
        guard target.windowID > 0, target.pid > 0, !panelIDs.isEmpty,
              !panelIDs.contains(0), !panelIDs.contains(target.windowID),
              Set(panelIDs).count == panelIDs.count,
              let targetEntry = windows.first(where: { $0.id == target.windowID }),
              targetEntry.pid == target.pid, targetEntry.level == 0,
              windows.filter({ panelIDs.contains($0.id) }).allSatisfy({
                  $0.pid == panelPID && $0.level == 0
              }) else { return nil }
        let normal = windows.filter { $0.level == 0 }.map(\.id)
        guard let index = normal.firstIndex(of: target.windowID) else { return nil }
        if index >= panelIDs.count,
           Array(normal[(index - panelIDs.count)..<index]) == Array(panelIDs.reversed()) {
            return []
        }
        var previous = target.windowID
        return panelIDs.map { id in
            defer { previous = id }
            return Placement(windowID: id, relativeTo: previous)
        }
    }

    @MainActor static func restore(panels: [NSPanel], target: Target) -> Bool {
        restoreStatus(panels: panels, target: target) == .verified
    }

    @MainActor static func restoreStatus(panels: [NSPanel], target: Target) -> Status {
        guard !panels.isEmpty, panels.allSatisfy({ panel in
            NSApp.windows.contains(where: { $0 === panel }) && panel.level == .normal
                && panel.isVisible && panel.windowNumber > 0
        }), let before = snapshot() else { return .unavailable }
        let ids = panels.compactMap { UInt32(exactly: $0.windowNumber) }
        guard ids.count == panels.count,
              let placements = plan(target: target, panelIDs: ids, panelPID: getpid(), windows: before) else { return .unavailable }
        if placements.isEmpty { return .verified }
        let byID = Dictionary(uniqueKeysWithValues: zip(ids, panels))
        for placement in placements {
            byID[placement.windowID]?.order(.above, relativeTo: Int(placement.relativeTo))
        }
        // An order request is not proof: external apps may have reordered meanwhile.
        guard let after = snapshot(),
              let remaining = plan(target: target, panelIDs: ids, panelPID: getpid(), windows: after) else { return .unavailable }
        return remaining.isEmpty ? .verified : .repairing
    }

    private static func snapshot() -> [Window]? {
        // Ordered-out/off-Space entries in optionAll have no reliable screen order.
        // Revalidate after the target returns to its Space instead of guessing their positions.
        guard let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        return rows.compactMap { row in
            guard let id = row[kCGWindowNumber as String] as? UInt32,
                  let pid = row[kCGWindowOwnerPID as String] as? Int32,
                  let level = row[kCGWindowLayer as String] as? Int else { return nil }
            return Window(id: id, pid: pid, level: level)
        }
    }
}

/// Preserve an already-confirmed picture while AppKit commits a short ordering update.
/// Unknown ownership/target state and any changed anchor still hide immediately.
struct OverlayOrderingPresentation {
    static let repairInterval: TimeInterval = 0.1
    private var target: WindowOverlayOrdering.Target?
    private var confirmed = false
    private var repairStartedAt: TimeInterval?
    private(set) var artworkVisible = false

    mutating func update(_ status: WindowOverlayOrdering.Status, target: WindowOverlayOrdering.Target,
                         now: TimeInterval) -> Bool {
        if self.target != target { reset(); self.target = target }
        switch status {
        case .verified:
            confirmed = true
            repairStartedAt = nil
            artworkVisible = true
        case .repairing:
            if repairStartedAt == nil { repairStartedAt = now }
            artworkVisible = confirmed && now >= repairStartedAt! && now - repairStartedAt! < Self.repairInterval
        case .unavailable:
            reset()
        }
        return artworkVisible
    }

    mutating func reset() {
        target = nil
        confirmed = false
        repairStartedAt = nil
        artworkVisible = false
    }
}
