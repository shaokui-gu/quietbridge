// The task shield is an input boundary; these tests cover visibility and artwork
// without locking the real desktop or sending any synthetic key events.
import AppKit
import Testing
@testable import QuietBridgeNative

@MainActor private final class SessionStatusFixture {
    var locked: Bool? = true
    var lockRequests = 0
}

@Test @MainActor func inactivePrivacyStatusReadsTheLiveSessionWithoutLockingOrStartingProtection() {
    let fixture = SessionStatusFixture()
    let controller = TaskPrivacyController(sessionState: { fixture.locked }, requestSystemLock: {
        fixture.lockRequests += 1
        return "unexpected"
    })
    #expect(controller.status()["systemLocked"] == .bool(true))
    fixture.locked = false
    #expect(controller.status()["systemLocked"] == .bool(false))
    fixture.locked = nil
    #expect(controller.status()["systemLocked"] == .null)
    #expect(controller.status()["phase"] == .string("inactive"))
    #expect(controller.status()["shieldVisible"] == .bool(false))
    #expect(fixture.lockRequests == 0)
}

@Test(arguments: [
    (false, false, false),
    (false, true, false),
    (false, nil, false),
    (true, false, true),
    (true, true, false),
    (true, nil, false),
])
func taskShieldOnlyCoversAnUnlockedActiveSession(
    _ taskActive: Bool, _ sessionLocked: Bool?, _ expected: Bool
) {
    #expect(TaskPrivacyPresentation.shouldShow(
        taskActive: taskActive, sessionLocked: sessionLocked) == expected)
}

@Test @MainActor func taskShieldRendersACompletePixelArtwork() throws {
    let width = 960
    let height = 600
    let bitmap = try #require(NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
    let graphics = try #require(NSGraphicsContext(bitmapImageRep: bitmap))
    NSGraphicsContext.saveGraphicsState()
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSGraphicsContext.current = graphics
    PrivacyShieldView.drawArtwork(
        in: CGRect(x: 0, y: 0, width: width, height: height),
        context: graphics.cgContext,
        screenIndex: 0)
    if let preview = ProcessInfo.processInfo.environment["QUIETBRIDGE_PRIVACY_ARTIFACT"],
       let png = bitmap.representation(using: .png, properties: [:]) {
        try png.write(to: URL(fileURLWithPath: preview), options: .atomic)
    }
    let data = try #require(bitmap.bitmapData)
    var minimum = UInt8.max
    var maximum = UInt8.min
    for index in stride(from: 0, to: width * height * 4, by: 4096) {
        minimum = min(minimum, data[index])
        maximum = max(maximum, data[index])
    }
    #expect(maximum - minimum > 20, "The shield should not be a flat blank window")
}
