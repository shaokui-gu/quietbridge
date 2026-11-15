import AppKit
import Testing
@testable import QuietBridgeNative

private func target(_ id: UInt32 = 7, pid: Int32 = 42, onScreen: Bool = true,
                    bounds: CGRect = CGRect(x: 100, y: 100, width: 400, height: 300)) -> CursorWindowTarget {
    CursorWindowTarget(windowID: id, pid: pid, bounds: bounds, isOnScreen: onScreen)
}

@Test func cursorOrderingUsesTheInputTargetRatherThanThePreviewHost() {
    let actual = target()
    let windows: [WindowOverlayOrdering.Window] = [
        .init(id: 50, pid: 99, level: 0), // Cursor must move down from above an unrelated app.
        .init(id: 80, pid: 70, level: 0),
        .init(id: 10, pid: 43, level: 0), // Preview host is independent of the input target.
        .init(id: 7, pid: 42, level: 0)]
    #expect(WindowOverlayOrdering.plan(target: actual.orderingTarget, panelIDs: [50], panelPID: 99,
        windows: windows) == [.init(windowID: 50, relativeTo: 7)])
    #expect(WindowOverlayOrdering.plan(target: actual.orderingTarget, panelIDs: [50], panelPID: 99,
        windows: [windows[1], windows[2], windows[0], windows[3]]) == [])
}

@Test func cursorWindowIdentityMustSurviveRevalidationButGeometryAndSpaceMayChange() {
    let original = target()
    #expect(CursorWindowTarget.select(target(), windowID: 7, previous: nil) == original)
    #expect(CursorWindowTarget.select(nil, windowID: 7, previous: original) == nil)
    #expect(CursorWindowTarget.select(target(8), windowID: 7, previous: original) == nil)
    #expect(CursorWindowTarget.select(target(pid: 43), windowID: 7, previous: original) == nil)
    #expect(CursorWindowTarget.select(target(pid: 0), windowID: 7, previous: nil) == nil)
    let moved = target(onScreen: false, bounds: CGRect(x: -900, y: -100, width: 400, height: 300))
    #expect(CursorWindowTarget.select(moved, windowID: 7, previous: original) == moved)
}

@Test func cursorArtworkAndRipplesAreClippedAtEveryTargetEdge() {
    let actual = target()
    let inAppKit = CGRect(x: 100, y: 600, width: 400, height: 300)
    for origin in [CGPoint(x: 70, y: 870), CGPoint(x: 470, y: 870),
                   CGPoint(x: 70, y: 570), CGPoint(x: 470, y: 570), CGPoint(x: 200, y: 700)] {
        let overlay = CGRect(origin: origin, size: CGSize(width: 80, height: 80))
        let clip = actual.artworkClip(overlayFrame: overlay, mainDisplayHeight: 1000)
        #expect(clip == overlay.intersection(inAppKit).offsetBy(dx: -origin.x, dy: -origin.y))
        #expect(clip.map { inAppKit.contains($0.offsetBy(dx: origin.x, dy: origin.y)) } == true)
    }
    #expect(actual.artworkClip(overlayFrame: CGRect(x: 0, y: 0, width: 80, height: 80), mainDisplayHeight: 1000) == nil)
}

@Test func cursorArtworkClipSupportsOtherDisplaysAndRefusesUnknownGeometry() {
    let actual = target(bounds: CGRect(x: -1200, y: -200, width: 600, height: 500))
    #expect(actual.artworkClip(overlayFrame: CGRect(x: -1230, y: 1170, width: 80, height: 80),
        mainDisplayHeight: 1000) == CGRect(x: 30, y: 0, width: 50, height: 30))
    #expect(target(bounds: .zero).artworkClip(overlayFrame: .zero, mainDisplayHeight: 1000) == nil)
    #expect(target().artworkClip(overlayFrame: CGRect(x: 100, y: 600, width: 80, height: 80),
        mainDisplayHeight: .nan) == nil)
}

@Test @MainActor func cursorDrawingClipsTheArrowGlowAndRipplesTogether() throws {
    let bitmap = try #require(NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 80, pixelsHigh: 80,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
    let context = try #require(NSGraphicsContext(bitmapImageRep: bitmap))
    let view = AgentCursorView(frame: CGRect(x: 0, y: 0, width: 80, height: 80))
    view.artworkClip = CGRect(x: 30, y: 48, width: 2, height: 2)
    view.rippleProgress = 0.5
    view.hoverPhase = 0.5
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    view.draw(view.bounds)
    NSGraphicsContext.restoreGraphicsState()
    let painted = (0..<80).reduce(0) { sum, y in
        sum + (0..<80).filter { (bitmap.colorAt(x: $0, y: y)?.alphaComponent ?? 0) > 0 }.count
    }
    #expect(painted > 0)
    #expect(painted <= 4)
}
