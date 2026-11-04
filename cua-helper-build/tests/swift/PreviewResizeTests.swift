// The share panel is borderless, so resize regions and frames are computed by us.
// These checks pin the two reported defects: no resize arrow at the rounded corners,
// and the card growing away from the pointer while dragging a corner.
import Testing
import CoreGraphics
import Foundation
@testable import CuaNative

private let bounds = CGRect(x: 0, y: 0, width: 400, height: 264)
private let geometry = PreviewResizeGeometry()

@Test(arguments: [
    (CGPoint(x: 1, y: 263), PreviewResizeRegion.topLeft),
    (CGPoint(x: 399, y: 263), PreviewResizeRegion.topRight),
    (CGPoint(x: 1, y: 1), PreviewResizeRegion.bottomLeft),
    (CGPoint(x: 399, y: 1), PreviewResizeRegion.bottomRight),
])
func roundedCornersAreResizeTargets(_ point: CGPoint, _ expected: PreviewResizeRegion) {
    #expect(geometry.region(at: point, in: bounds) == expected)
}

@Test func cardEdgesResizeAndTheMiddleStaysADragHandle() {
    #expect(geometry.region(at: CGPoint(x: 3, y: 132), in: bounds) == .left)
    #expect(geometry.region(at: CGPoint(x: 397, y: 132), in: bounds) == .right)
    #expect(geometry.region(at: CGPoint(x: 200, y: 3), in: bounds) == .bottom)
    // Title bar ends resize, its middle keeps dragging the panel.
    #expect(geometry.region(at: CGPoint(x: 60, y: 262), in: bounds) == .top)
    #expect(geometry.region(at: CGPoint(x: 340, y: 262), in: bounds) == .top)
    #expect(geometry.region(at: CGPoint(x: 200, y: 262), in: bounds) == .none)
    #expect(geometry.region(at: CGPoint(x: 200, y: 132), in: bounds) == .none)
    #expect(geometry.region(at: CGPoint(x: -5, y: 132), in: bounds) == .none)
}

private let startFrame = CGRect(x: 100, y: 100, width: 400, height: 264)
private let aspect: CGFloat = 0.6

private func resized(_ region: PreviewResizeRegion, dx: CGFloat, dy: CGFloat) -> CGRect {
    geometry.frame(for: region, startFrame: startFrame,
                   delta: CGSize(width: dx, height: dy), aspect: aspect)
}

@Test func draggingTheRightEdgeKeepsTheLeftEdgeAndGrowsRight() {
    let rect = resized(.right, dx: 100, dy: 0)
    #expect(rect.width == 500)
    #expect(rect.minX == startFrame.minX)
    #expect(rect.maxX == startFrame.minX + 500)
    // Height follows the width; the top stays put.
    #expect(rect.height == 500 * aspect + PreviewResizeGeometry.titleHeight)
    #expect(rect.maxY == startFrame.maxY)
}

@Test func draggingTheLeftEdgeKeepsTheRightEdge() {
    let rect = resized(.left, dx: -100, dy: 0)
    #expect(rect.width == 500)
    #expect(rect.maxX == startFrame.maxX)
    #expect(rect.minX == startFrame.maxX - 500)
}

@Test func draggingTheTopEdgeKeepsTheBottomAndGrowsUpward() {
    let rect = resized(.top, dx: 0, dy: 60)
    #expect(rect.width == 500)
    #expect(rect.minY == startFrame.minY)
    #expect(rect.height > startFrame.height)
}

@Test func draggingTheBottomEdgeKeepsTheTop() {
    let rect = resized(.bottom, dx: 0, dy: -60)
    #expect(rect.width == 500)
    #expect(rect.maxY == startFrame.maxY)
}

@Test func cornerDragFollowsTheAxisThePointerMovedFurthest() {
    let horizontal = resized(.topLeft, dx: -30, dy: 4)
    #expect(horizontal.width == 430)
    #expect(horizontal.maxX == startFrame.maxX)

    let vertical = resized(.topLeft, dx: -4, dy: 60)
    #expect(vertical.width == 500)
    #expect(vertical.maxX == startFrame.maxX)
}

@Test(arguments: [CGFloat(5000), CGFloat(-5000)])
func widthStaysWithinTheSupportedRange(_ delta: CGFloat) {
    let rect = resized(.right, dx: delta, dy: 0)
    #expect(rect.width >= PreviewResizeGeometry.minWidth)
    #expect(rect.width <= PreviewResizeGeometry.maxWidth)
}

@Test func nonResizeRegionsNeverMoveTheCard() {
    #expect(resized(.none, dx: 120, dy: 90) == startFrame)
}

@Test @MainActor func previewLeaseAloneDoesNotClaimStreamReadiness() async {
    let preview = SpacePreview()
    #expect(preview.hold(seconds: 120) == 120)
    let diagnostics = preview.diagnostics()
    #expect(diagnostics["holding"] as? Bool == true)
    #expect(diagnostics["streamStarting"] as? Bool == false)
    #expect(diagnostics["streamStarted"] as? Bool == false)
    #expect(diagnostics["streamReady"] as? Bool == false)
    #expect(diagnostics["frameAgeMs"] is NSNull)
    #expect(diagnostics["targetOnCurrentSpace"] is NSNull)
    do {
        try await preview.waitUntilReady {}
        Issue.record("没有目标或首帧的租约不应成功")
    } catch let failure as NativeFailure {
        #expect(failure.code == "SHARE_NOT_READY")
    } catch {
        Issue.record("意外错误：\(error)")
    }
    #expect(preview.isHolding == false)
    #expect(preview.streamReady == false)
    preview.release()
}
