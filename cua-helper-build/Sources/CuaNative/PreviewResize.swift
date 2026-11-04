// Hit-testing and frame maths for the floating share panel.
//
// The panel is a borderless NSPanel, so macOS provides neither resize handles nor
// resize cursors: the regions, the cursor and the resulting frame are all derived
// here. Keeping the maths out of the view makes it testable without a window server.
import AppKit

enum PreviewResizeRegion: CaseIterable, Equatable {
    case none
    case left, right, top, bottom
    case topLeft, topRight, bottomLeft, bottomRight

    var isLeft: Bool { self == .left || self == .topLeft || self == .bottomLeft }
    var isRight: Bool { self == .right || self == .topRight || self == .bottomRight }
    var isTop: Bool { self == .top || self == .topLeft || self == .topRight }
    var isBottom: Bool { self == .bottom || self == .bottomLeft || self == .bottomRight }
    var isCorner: Bool { (isLeft || isRight) && (isTop || isBottom) }

    /// Arrow shown while the pointer sits in this region.
    var cursor: NSCursor {
        switch self {
        case .none:
            return .arrow
        case .left, .right:
            return .resizeLeftRight
        case .top, .bottom:
            return .resizeUpDown
        case .topLeft, .topRight, .bottomLeft, .bottomRight:
            if #available(macOS 15.0, *) {
                let position: NSCursor.FrameResizePosition
                switch self {
                case .topLeft: position = .topLeft
                case .topRight: position = .topRight
                case .bottomLeft: position = .bottomLeft
                default: position = .bottomRight
                }
                return .frameResize(position: position, directions: .all)
            }
            // No public diagonal resize cursor before macOS 15.
            return isLeft || isRight ? .resizeLeftRight : .resizeUpDown
        }
    }
}

struct PreviewResizeGeometry {
    static let minWidth: CGFloat = 200
    static let maxWidth: CGFloat = 1200
    static let titleHeight: CGFloat = 24

    /// Band along the card edge that starts a resize.
    var edgeBand: CGFloat = 8
    /// Square anchored on each rounded corner.
    var cornerSize: CGFloat = 22
    /// Fraction of the width kept for dragging at each end of the title bar.
    var topEdgeSpan: CGFloat = 0.34

    func region(at point: CGPoint, in bounds: CGRect) -> PreviewResizeRegion {
        guard bounds.width > 0, bounds.height > 0 else { return .none }
        guard bounds.insetBy(dx: -1, dy: -1).contains(point) else { return .none }
        // Corners first: they must win over the edge bands so the visible rounded
        // corner answers with a diagonal arrow instead of a one-axis arrow.
        let corner = min(cornerSize, min(bounds.width, bounds.height) / 2)
        let inLeft = point.x <= bounds.minX + corner
        let inRight = point.x >= bounds.maxX - corner
        let inTop = point.y >= bounds.maxY - corner
        let inBottom = point.y <= bounds.minY + corner
        if inLeft && inTop { return .topLeft }
        if inRight && inTop { return .topRight }
        if inLeft && inBottom { return .bottomLeft }
        if inRight && inBottom { return .bottomRight }

        if point.x <= bounds.minX + edgeBand { return .left }
        if point.x >= bounds.maxX - edgeBand { return .right }
        if point.y <= bounds.minY + edgeBand { return .bottom }
        // The middle of the title bar stays a drag handle, so only its ends resize.
        if point.y >= bounds.maxY - edgeBand {
            let span = bounds.width * topEdgeSpan
            if point.x <= bounds.minX + span || point.x >= bounds.maxX - span { return .top }
        }
        return .none
    }

    /// Height follows width so the streamed desktop keeps its aspect ratio.
    func frame(for region: PreviewResizeRegion,
               startFrame: CGRect,
               delta: CGSize,
               aspect: CGFloat) -> CGRect {
        guard region != .none, startFrame.width > 0 else { return startFrame }
        let safeAspect = aspect > 0 ? aspect : 0
        let verticalAsWidth = safeAspect > 0 ? delta.height / safeAspect : 0
        var growth: CGFloat
        if region.isCorner {
            // Diagonal drags are ambiguous once the height is derived from the width:
            // follow whichever axis the pointer moved further so the card does not jitter.
            let horizontal = region.isRight ? delta.width : -delta.width
            let vertical = region.isTop ? verticalAsWidth : -verticalAsWidth
            growth = abs(horizontal) >= abs(vertical) ? horizontal : vertical
        } else if region.isRight {
            growth = delta.width
        } else if region.isLeft {
            growth = -delta.width
        } else if region.isTop {
            growth = verticalAsWidth
        } else {
            growth = -verticalAsWidth
        }
        let width = min(Self.maxWidth, max(Self.minWidth, startFrame.width + growth))
        let height = width * safeAspect + Self.titleHeight
        // Anchor the edge opposite the one being dragged.
        let x = region.isLeft ? startFrame.maxX - width : startFrame.minX
        let y = region.isTop ? startFrame.minY : startFrame.maxY - height
        return CGRect(x: x, y: y, width: width, height: height)
    }
}
