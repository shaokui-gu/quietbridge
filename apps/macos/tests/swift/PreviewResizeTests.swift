// The sole resize grip keeps the preview's top-right corner fixed.
import AppKit
import ScreenCaptureKit
import Testing
@testable import QuietBridgeNative

private let bounds = CGRect(x: 0, y: 0, width: 400, height: 240)
private let geometry = PreviewResizeGeometry()
private let startFrame = CGRect(x: 100, y: 100, width: 400, height: 240)
private let aspect: CGFloat = 0.6

@Test func onlyTheBottomLeftCornerAcceptsResizeGestures() {
    #expect(geometry.region(at: CGPoint(x: 1, y: 1), in: bounds) == .bottomLeft)
    for point in [CGPoint(x: 1, y: 239), CGPoint(x: 399, y: 239), CGPoint(x: 399, y: 1),
                  CGPoint(x: 3, y: 120), CGPoint(x: 397, y: 120), CGPoint(x: 200, y: 3),
                  CGPoint(x: 200, y: 239), CGPoint(x: 200, y: 120), CGPoint(x: -1, y: 1)] {
        #expect(geometry.region(at: point, in: bounds) == .none)
    }
}

@Test(arguments: [CGSize(width: -100, height: 0), CGSize(width: 0, height: -60), CGSize(width: -20, height: -60)])
func bottomLeftResizePreservesTheTopRightAndPictureAspect(_ delta: CGSize) {
    let frame = geometry.frame(for: .bottomLeft, startFrame: startFrame, delta: delta, aspect: aspect)
    #expect(frame.width == 500)
    #expect(frame.maxX == startFrame.maxX)
    #expect(frame.maxY == startFrame.maxY)
    #expect(frame.height == frame.width * aspect)
}

@Test(arguments: [CGFloat(5000), CGFloat(-5000)])
func widthStaysWithinTheSupportedDragRange(_ delta: CGFloat) {
    let frame = geometry.frame(for: .bottomLeft, startFrame: startFrame,
                               delta: CGSize(width: delta, height: 0), aspect: aspect)
    #expect(frame.width >= PreviewResizeGeometry.minWidth)
    #expect(frame.width <= PreviewResizeGeometry.maxWidth)
    #expect(frame.maxX == startFrame.maxX)
    #expect(frame.maxY == startFrame.maxY)
}

@Test func nonResizeGesturesNeverMoveThePinnedCard() {
    #expect(geometry.frame(for: .none, startFrame: startFrame, delta: CGSize(width: 120, height: 90), aspect: aspect) == startFrame)
}

@MainActor @Test func fullFrameVideoUsesNativeGlassAndKeepsCloseControlsAccessible() throws {
    _ = NSApplication.shared
    let view = PreviewView(frame: bounds)
    let window = NSWindow(contentRect: CGRect(x: 0, y: 0, width: 500, height: 340),
                          styleMask: .borderless, backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    defer { window.close() }
    let host = NSView(frame: CGRect(x: 0, y: 0, width: 500, height: 340))
    window.contentView = host
    host.wantsLayer = true
    host.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
    view.frame.origin = CGPoint(x: 50, y: 50)
    host.addSubview(view)
    view.layoutSubtreeIfNeeded()
    #expect(view.video.frame == bounds)
    #expect(view.backdropView?.frame == bounds)
    // Both the native backdrop and the GPU-backed picture are inside the same
    // outer clip, rather than relying on the glass contentView to clip video.
    #expect(view.layer?.masksToBounds == true)
    #expect(view.layer?.cornerRadius == PreviewView.cornerRadius)
    #expect(view.video.masksToBounds)
    #expect(view.video.cornerRadius == PreviewView.cornerRadius)
    #expect(view.video.borderWidth == 0)
    #expect(view.layer?.borderWidth == 0)
    #expect(view.backdropView?.superview === view)
    #expect(view.video.superlayer?.delegate as? NSView !== view.backdropView)
    // A solid picture makes leaked glass borders and square video corners
    // visible in the offscreen render, without capturing a person's desktop.
    view.video.backgroundColor = NSColor(srgbRed: 0.10, green: 0.30, blue: 0.70, alpha: 1).cgColor
    if #available(macOS 26.0, *) { #expect(view.backdropView is NSGlassEffectView) }
    else { #expect(view.backdropView is NSVisualEffectView) }
    #expect(view.closeButton.isHiddenOrHasHiddenAncestor)
    view.setHovered(true)
    #expect(!view.closeButton.isHiddenOrHasHiddenAncestor)
    #expect(view.closeButton.accessibilityLabel() == "关闭共享预览")
    #expect(view.hitTest(CGPoint(x: 55, y: 55)) === view)
    let closeCenter = view.closeButton.convert(CGPoint(x: 14, y: 14), to: host)
    #expect(view.hitTest(closeCenter) === view.closeButton)
    host.layoutSubtreeIfNeeded()
    let bitmap = try #require(host.bitmapImageRepForCachingDisplay(in: host.bounds))
    host.cacheDisplay(in: host.bounds, to: bitmap)
    let png = try #require(bitmap.representation(using: .png, properties: [:]))
    try png.write(to: URL(fileURLWithPath: "/private/tmp/quietbridge-preview-glass.png"))
}

@Test func windowPreviewExcludesCaptureFramingWithoutChangingDisplayCapture() {
    let configuration = SCStreamConfiguration()
    configuration.ignoreShadowsSingleWindow = false
    configuration.ignoreGlobalClipSingleWindow = false
    configuration.shouldBeOpaque = true
    PreviewSource.window.configureFraming(configuration)
    #expect(configuration.ignoreShadowsSingleWindow)
    #expect(configuration.ignoreGlobalClipSingleWindow)
    #expect(!configuration.shouldBeOpaque)
    for source in [PreviewSource.application, .desktop] {
        let display = SCStreamConfiguration()
        display.ignoreShadowsSingleWindow = false
        display.shouldBeOpaque = true
        source.configureFraming(display)
        #expect(!display.ignoreShadowsSingleWindow)
        #expect(display.shouldBeOpaque)
    }
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
