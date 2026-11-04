import Foundation
import Testing
@testable import CuaNative

private let visualTarget = CaptureTarget(appHandle: "fixture", process: ProcessIdentity(pid: 42, startedSeconds: 1, startedMicroseconds: 0),
    window: WindowInfo(windowID: 7, pid: 42, appName: "fixture", title: "fixture",
                       bounds: WindowBounds(x: 0, y: 0, width: 1, height: 1), isOnScreen: true, onCurrentSpace: true))
private func visualFrame(time: Double = 10, window: UInt32 = 7, owner: ProcessIdentity = visualTarget.process) -> VisualFrame {
    VisualFrame(image: CapturedPNG(bytes: Data([1]), width: 1, height: 1), capturedAt: time, windowID: window, owner: owner)
}
private final class VisualFixture: @unchecked Sendable {
    private let lock = NSLock()
    private var stops = 0
    var stopCount: Int { lock.withLock { stops } }
    func stop() { lock.withLock { stops += 1 } }
}

@Test func visualWaitsForNewFrameAndStopsOnce() async throws {
    let fixture = VisualFixture()
    let waiter = VisualFrameWaiter(target: visualTarget, clock: { 10 })
    let frame = try await waiter.wait(start: { _, _, receive in
        receive(.success(visualFrame(time: 9)))
        receive(.success(visualFrame()))
        receive(.success(visualFrame(time: 8)))
        return { fixture.stop() }
    })
    #expect(frame.capturedAt == 10)
    #expect(fixture.stopCount == 1)
}

@Test func visualTimeoutStopsWithoutFrames() async throws {
    let fixture = VisualFixture()
    do {
        _ = try await VisualFrameWaiter(target: visualTarget).wait(start: { _, _, _ in { fixture.stop() } }, timeout: 0.01)
        Issue.record("无帧不应成功")
    } catch let failure as NativeFailure { #expect(failure.code == "NATIVE_TIMEOUT") }
    #expect(fixture.stopCount == 1)
}

@Test func visualPropagatesStreamErrorsAndStops() async throws {
    let fixture = VisualFixture()
    do {
        _ = try await VisualFrameWaiter(target: visualTarget).wait(start: { _, _, receive in
            receive(.failure(NativeFailure(code: "NATIVE_ERROR", message: "视频流错误")))
            return { fixture.stop() }
        })
        Issue.record("错误不应成功")
    } catch let failure as NativeFailure { #expect(failure.code == "NATIVE_ERROR") }
    #expect(fixture.stopCount == 1)
}

@Test(arguments: [false, true]) func visualRejectsWrongTargetOrOwner(_ wrongOwner: Bool) async throws {
    let fixture = VisualFixture()
    do {
        _ = try await VisualFrameWaiter(target: visualTarget, clock: { 10 }).wait(start: { _, _, receive in
            receive(.success(visualFrame(window: wrongOwner ? 7 : 8,
                owner: wrongOwner ? ProcessIdentity(pid: 42, startedSeconds: 2, startedMicroseconds: 0) : visualTarget.process)))
            return { fixture.stop() }
        })
        Issue.record("旧target不应成功")
    } catch let failure as NativeFailure { #expect(failure.code == "WINDOW_CHANGED") }
    #expect(fixture.stopCount == 1)
}

@Test func visualCancellationDiscardsLateFrameAndStops() async throws {
    let fixture = VisualFixture()
    let (events, starts) = AsyncStream<Bool>.makeStream()
    let waiter = VisualFrameWaiter(target: visualTarget, clock: { 10 })
    let task = Task { try await waiter.wait(start: { _, _, _ in
        starts.yield(true)
        return { fixture.stop() }
    }) }
    var iterator = events.makeAsyncIterator()
    #expect(await iterator.next() == true)
    task.cancel()
    do { _ = try await task.value; Issue.record("取消不应成功") }
    catch let failure as NativeFailure { #expect(failure.code == "NATIVE_CANCELLED") }
    waiter.receive(.success(visualFrame()))
    #expect(fixture.stopCount == 1)
    starts.finish()
}

@Test func visualExpiredAndFutureFramesCannotCompleteObservation() async throws {
    let fixture = VisualFixture()
    do {
        _ = try await VisualFrameWaiter(target: visualTarget, clock: { 10 }).wait(start: { _, _, receive in
            receive(.success(visualFrame(time: 7)))
            receive(.success(visualFrame(time: 11)))
            return { fixture.stop() }
        }, timeout: 0.01)
        Issue.record("过期或未来帧不应完成观察")
    } catch let failure as NativeFailure { #expect(failure.code == "NATIVE_TIMEOUT") }
    #expect(fixture.stopCount == 1)
}

@Test func visualProtocolAdmitsOnlyObservationFields() throws {
    let params = try RequestParameters(NativeRequest(data: Data(#"{"id":"v","method":"visualState","params":{"appHandle":"fixture","windowId":7}}"#.utf8)))
    #expect(try params.windowID() == 7)
    #expect(NativeProtocol.modelMethods.contains("visualState"))
    #expect(throws: NativeFailure.self) {
        try RequestParameters(NativeRequest(data: Data(#"{"id":"v","method":"visualState","params":{"appHandle":"fixture","windowId":7,"click":true}}"#.utf8)))
    }
}
