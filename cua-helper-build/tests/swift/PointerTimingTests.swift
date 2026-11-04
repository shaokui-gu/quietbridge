// 仅测试纯时间及坐标状态，不创建窗口或发送输入。
import Testing
import CoreGraphics
@testable import CuaNative

@Test(arguments: [(1, 240), (399, 20), (400, 20)])
func dragMovementKeepsTheRequestedDuration(_ durationMs: Int, _ steps: Int) {
    let interval = WindowPointerInput.dragStepInterval(durationMs: durationMs, steps: steps)
    #expect(interval > 0)
    #expect(abs(interval * Double(steps) - Double(durationMs) / 1000) < 1e-12)
}

@Test func cursorExpiresAtExactlyThirtySecondsWithoutRenderingExtendingItsLife() {
    var state = CursorState()
    #expect(state.frame(at: 10) == nil)
    state.move(to: CGPoint(x: 100, y: 200), windowID: 7, pulse: false, now: 10)
    #expect(state.frame(at: 10)?.point == CGPoint(x: 100, y: 200))
    for time in [10.1, 20, 39.999] { #expect(state.frame(at: time)?.windowID == 7) }
    #expect(state.frame(at: 40) == nil)
    #expect(state.frame(at: 50) == nil)
}

@Test func repeatedActivityAtTheSamePointRestartsTheThirtySecondDeadline() {
    var state = CursorState()
    let point = CGPoint(x: 100, y: 200)
    state.move(to: point, windowID: 7, pulse: false, now: 0)
    state.move(to: point, windowID: 7, pulse: true, now: 29)
    #expect(state.frame(at: 30)?.point == point)
    #expect(state.frame(at: 58.999) != nil)
    #expect(state.frame(at: 59) == nil)
}

@Test func realCursorAndMirrorUseTheSameGlideAndRippleFrame() throws {
    var state = CursorState()
    let glide = CursorState.glideDuration
    let ripple = CursorState.rippleDuration
    state.move(to: .zero, windowID: 7, pulse: false, now: 0)
    state.move(to: CGPoint(x: 80, y: 40), windowID: 7, pulse: true, now: 1)
    // 起步：仍在起点；中途：在两点的连线上；到点：精确落在目标。
    #expect(state.frame(at: 1)?.point == .zero)
    let moving = try #require(state.frame(at: 1 + glide / 2))
    #expect(moving.point.x > 0 && moving.point.x < 80)
    #expect(abs(moving.point.y - moving.point.x / 2) < 1e-9)
    #expect(moving.rippleProgress == nil)
    let arrived = try #require(state.frame(at: 1 + glide + 1e-9))
    #expect(arrived.point == CGPoint(x: 80, y: 40))
    #expect(try #require(arrived.rippleProgress) < 1e-6)
    let pulsing = try #require(state.frame(at: 1 + glide + ripple / 2))
    #expect(abs(try #require(pulsing.rippleProgress) - 0.5) < 1e-9)
    #expect(state.frame(at: 1 + glide + ripple + 1e-6)?.rippleProgress == nil)
    #expect(state.frame(at: 31) == nil)
}

@Test func ripplePhaseAdvancesContinuouslyEvenAcrossMoves() throws {
    var state = CursorState()
    let cycle = CursorState.rippleCycle
    state.move(to: .zero, windowID: 7, pulse: false, now: 0)
    state.move(to: CGPoint(x: 40, y: 40), windowID: 7, pulse: false, now: 1)
    // 相位按固定周期推进：移动中不重置，一个周期后回到同相位。
    #expect(abs(try #require(state.frame(at: cycle * 0.25)).hoverPhase - 0.25) < 1e-9)
    #expect(abs(try #require(state.frame(at: cycle * 0.75)).hoverPhase - 0.75) < 1e-9)
    #expect(abs(try #require(state.frame(at: cycle * 1.25)).hoverPhase - 0.25) < 1e-9)
    for time in stride(from: 0.0, through: 3.0, by: 0.13) {
        let phase = try #require(state.frame(at: 1 + time)).hoverPhase
        #expect(phase >= 0 && phase < 1)
    }
}

@Test func interruptedGlideStartsAtTheDisplayedPointAndDoesNotReplayAnOldPulse() throws {
    var state = CursorState()
    state.move(to: .zero, windowID: 7, pulse: false, now: 0)
    state.move(to: CGPoint(x: 80, y: 40), windowID: 7, pulse: true, now: 1)
    let previous = try #require(state.frame(at: 1.09))
    state.move(to: CGPoint(x: 20, y: 60), windowID: 7, pulse: false, now: 1.09)
    #expect(state.frame(at: 1.09)?.point == previous.point)
    #expect(state.frame(at: 1.4)?.point == CGPoint(x: 20, y: 60))
    #expect(state.frame(at: 1.4)?.rippleProgress == nil)
}

@Test func changingWindowsOrReturningAfterIdleDoesNotGlideFromTheOldDesktop() {
    var state = CursorState()
    state.move(to: .zero, windowID: 7, pulse: true, now: 0)
    let target = CGPoint(x: -500, y: -200)
    state.move(to: target, windowID: 8, pulse: false, now: 1)
    #expect(state.frame(at: 1)?.point == target)
    #expect(state.frame(at: 1)?.windowID == 8)
    #expect(state.frame(at: 1)?.rippleProgress == nil)
    state.move(to: .zero, windowID: 8, pulse: false, now: 32)
    #expect(state.frame(at: 32)?.point == .zero)
}

@Test func mirrorCoordinatesUseWindowOriginAndNeverClampAnOutsidePointToItsEdge() {
    let frame = CursorFrame(windowID: 7, point: CGPoint(x: -900, y: 250), rippleProgress: nil, hoverPhase: 0)
    #expect(frame.normalizedPoint(in: CGRect(x: -1000, y: 200, width: 400, height: 200)) == CGPoint(x: 0.25, y: 0.25))
    #expect(frame.normalizedPoint(in: .zero) == nil)
    #expect(frame.normalizedPoint(in: CGRect(x: 0, y: 0, width: 400, height: 200)) == nil)
}
