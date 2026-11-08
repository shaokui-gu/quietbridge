// The preview stays pinned at its top-right corner. Only the bottom-left grip resizes it.
import AppKit

enum PreviewResizeRegion: CaseIterable, Equatable {
    case none, bottomLeft

    var cursor: NSCursor {
        guard self == .bottomLeft else { return .arrow }
        if #available(macOS 15.0, *) {
            return .frameResize(position: .bottomLeft, directions: .all)
        }
        return .resizeLeftRight
    }
}

struct PreviewResizeGeometry {
    static let minWidth: CGFloat = 200
    static let maxWidth: CGFloat = 1200
    var cornerSize: CGFloat = 22

    func region(at point: CGPoint, in bounds: CGRect) -> PreviewResizeRegion {
        guard bounds.width > 0, bounds.height > 0, bounds.contains(point) else { return .none }
        let corner = min(cornerSize, min(bounds.width, bounds.height) / 2)
        return point.x <= bounds.minX + corner && point.y <= bounds.minY + corner ? .bottomLeft : .none
    }

    func frame(for region: PreviewResizeRegion, startFrame: CGRect,
               delta: CGSize, aspect: CGFloat) -> CGRect {
        guard region == .bottomLeft, startFrame.width > 0,
              aspect.isFinite, aspect > 0, delta.width.isFinite, delta.height.isFinite else { return startFrame }
        let horizontal = -delta.width
        let vertical = -delta.height / aspect
        let growth = abs(horizontal) >= abs(vertical) ? horizontal : vertical
        let width = min(Self.maxWidth, max(Self.minWidth, startFrame.width + growth))
        let height = width * aspect
        return CGRect(x: startFrame.maxX - width, y: startFrame.maxY - height,
                      width: width, height: height)
    }
}
