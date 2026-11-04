// Draws an agent-owned pointer so a person can see where the helper acts.
// The overlay never takes focus, never becomes key, and never receives events.
import AppKit

/// 真实浮层和共享镜像唯一的坐标、动画和空闲时钟；纯值逻辑不触碰 GUI。
struct CursorFrame: Equatable {
    let windowID: UInt32
    let point: CGPoint
    let rippleProgress: Double?
    /// 到达目标后的呼吸相位（0..1）：让人持续看出指针停在哪里。
    let hoverPhase: Double

    func normalizedPoint(in bounds: CGRect) -> CGPoint? {
        guard bounds.width > 0, bounds.height > 0, bounds.contains(point) else { return nil }
        return CGPoint(x: (point.x - bounds.minX) / bounds.width,
                       y: (point.y - bounds.minY) / bounds.height)
    }
}

struct CursorState {
    static let idleInterval: TimeInterval = 30
    static let glideDuration: TimeInterval = 0.30
    static let rippleDuration: TimeInterval = 0.45
    /// 定位涟漪一圈走完全程的时间；放慢到 2.4 秒，三圈错开约 0.8 秒一波。
    static let rippleCycle: TimeInterval = 2.4
    private var windowID: UInt32?
    private var from: CGPoint = .zero
    private var to: CGPoint = .zero
    private var started: TimeInterval = 0
    private var duration: TimeInterval = 0
    private var lastActivity: TimeInterval = 0
    private var rippleStart: TimeInterval?

    mutating func move(to target: CGPoint, windowID: UInt32, pulse: Bool, now: TimeInterval) {
        let current = frame(at: now)
        if let current, current.windowID == windowID { from = current.point } else { from = target }
        to = target
        self.windowID = windowID
        started = now
        lastActivity = now
        duration = hypot(to.x - from.x, to.y - from.y) >= 0.5 ? Self.glideDuration : 0
        rippleStart = pulse ? now + duration : nil
    }

    func frame(at now: TimeInterval) -> CursorFrame? {
        guard let windowID, now - lastActivity < Self.idleInterval else { return nil }
        let progress = duration == 0 ? 1 : min(1, max(0, (now - started) / duration))
        // easeInOutCubic：起步和落点都收一下，移动过程更像真实指针。
        let eased = progress < 0.5 ? 4 * progress * progress * progress
                                  : 1 - pow(-2 * progress + 2, 3) / 2
        let point = CGPoint(x: from.x + (to.x - from.x) * eased,
                            y: from.y + (to.y - from.y) * eased)
        let ripple = rippleStart.map { (now - $0) / Self.rippleDuration }
        // 涟漪相位用绝对时间推进，移动过程中也持续向外扩散，不会因为改变目标而重置。
        return CursorFrame(windowID: windowID, point: point,
                           rippleProgress: ripple.flatMap { (0..<1).contains($0) ? $0 : nil },
                           hoverPhase: (now / Self.rippleCycle).truncatingRemainder(dividingBy: 1))
    }
}

final class AgentCursor: @unchecked Sendable {
    static let shared = AgentCursor()

    private let size = CGSize(width: 80, height: 80)
    @MainActor private var window: NSPanel?
    @MainActor private var view: AgentCursorView?
    @MainActor private var state = CursorState()
    @MainActor private var timer: Timer?
    @MainActor private var spaces: WindowSpaces?
    @MainActor private var assigned: [UInt64] = []
    @MainActor private var lastValidation: TimeInterval = 0
    @MainActor private var failure: String?

    nonisolated init() {}

    nonisolated func move(to screenPoint: CGPoint, pulse: Bool = false, windowID: UInt32? = nil) {
        Task { @MainActor in
            self.apply(point: screenPoint, pulse: pulse, windowID: windowID)
        }
    }

    @MainActor private func apply(point: CGPoint, pulse: Bool, windowID: UInt32?) {
        guard point.x.isFinite, point.y.isFinite, let windowID, windowID != 0 else {
            hide(reason: "目标窗口或坐标未知，已隐藏光标")
            return
        }
        let now = ProcessInfo.processInfo.systemUptime
        // 新目标不可沿用旧窗口的动画起点或预览箭头。
        if state.frame(at: now)?.windowID != windowID { hide() }
        state.move(to: point, windowID: windowID, pulse: pulse, now: now)
        guard validateRoute(windowID: windowID, point: point) else { return }
        lastValidation = now
        if let win = NativePlatform.windows().first(where: { $0.windowID == windowID }) {
            SpacePreview.shared.track(pid: win.pid, windowID: windowID)
        }
        render(at: now)
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        }
    }

    @MainActor private func validateRoute(windowID: UInt32, point: CGPoint) -> Bool {
        if spaces == nil { spaces = WindowSpaces() }
        guard let spaces else { return false }
        let before = spaces.memberships(windowIDs: [windowID])[windowID]
        guard case .target(let destination) = CursorSpaceRoute.decide(windowID: windowID, membership: before),
              NativePlatform.windows().contains(where: { $0.windowID == windowID }) else {
            hide(reason: "目标 Space 归属未知或窗口已关闭，已隐藏光标")
            return false
        }
        ensureWindow()
        guard let window else { return false }
        let overlayID = UInt32(exactly: window.windowNumber) ?? 0
        let actual = spaces.memberships(windowIDs: [overlayID])[overlayID]?.spaceIDs
        if assigned != destination || !CursorSpaceRoute.matches(actual, target: destination) || !window.isVisible {
            // alpha=0 时登记窗口并迁移，绝不在用户当前桌面闪出旧光标。
            window.alphaValue = 0
            place(at: point)
            window.orderFrontRegardless()
            guard spaces.assignOverlay(window, to: destination) else {
                hide(reason: "自有光标 Space 迁移未通过回读验证，已隐藏真实浮层及共享镜像")
                return false
            }
            assigned = destination
        }
        let after = spaces.memberships(windowIDs: [windowID, overlayID])
        guard CursorSpaceRoute.decide(windowID: windowID, membership: after[windowID]) == .target(destination),
              CursorSpaceRoute.matches(after[overlayID]?.spaceIDs, target: destination) else {
            hide(reason: "Space 归属在绘制前发生变化，已隐藏光标")
            return false
        }
        failure = nil
        return true
    }

    @MainActor private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        guard let frame = state.frame(at: now) else { hide(); return }
        // 空闲时也复核目标关闭、换 Space 或归属丢失，不让旧指针停留 30 秒。
        if now - lastValidation >= 0.25 {
            guard validateRoute(windowID: frame.windowID, point: frame.point) else { return }
            lastValidation = now
        }
        render(at: now)
    }

    @MainActor private func render(at now: TimeInterval) {
        guard let frame = state.frame(at: now) else { hide(); return }
        place(at: frame.point)
        view?.rippleProgress = frame.rippleProgress
        view?.hoverPhase = frame.hoverPhase
        window?.alphaValue = 1
        SpacePreview.shared.mirrorCursor(frame)
    }

    @MainActor private func hide(reason: String? = nil) {
        window?.alphaValue = 0
        window?.orderOut(nil)
        view?.rippleProgress = nil
        view?.hoverPhase = 0
        timer?.invalidate()
        timer = nil
        state = CursorState()
        assigned = []
        SpacePreview.shared.mirrorCursor(nil)
        if let reason, failure != reason {
            // 不依赖调试开关；失败必须可见，不能把预览镜像冒充真实浮层。
            FileHandle.standardError.write(Data(("[cursor] \(reason)\n").utf8))
        }
        failure = reason
    }

    @MainActor private func ensureWindow() {
        guard window == nil else { return }
        // A background helper launches as .prohibited; accessory keeps no Dock icon
        // while still allowing a non-activating overlay window on screen.
        if NSApp.activationPolicy() != .accessory { NSApp.setActivationPolicy(.accessory) }
        let panel = CursorPanel(contentRect: NSRect(origin: .zero, size: size),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.alphaValue = 0
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = false
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.screenSaverWindow)))
        panel.ignoresMouseEvents = true
        panel.isMovable = false
        panel.hidesOnDeactivate = false
        // Never join every Space: a phantom pointer drifting over whatever desktop the
        // person switched to is worse than no pointer at all.
        panel.collectionBehavior = [.stationary, .fullScreenAuxiliary, .ignoresCycle]
        let content = AgentCursorView(frame: NSRect(origin: .zero, size: size))
        panel.contentView = content
        window = panel
        view = content
    }

    /// Keep the artwork tip on the requested top-left-origin screen point.
    @MainActor private func place(at target: CGPoint) {
        guard let window else { return }
        let tip = WindowGeometry.appKitPoint(target, mainDisplayHeight: NSScreen.screens.first?.frame.maxY ?? 0)
        let origin = NSPoint(x: tip.x - 30, y: tip.y - size.height + 30)
        window.setFrameOrigin(origin)
    }
}

private final class CursorPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class AgentCursorView: NSView {
    var rippleProgress: Double? { didSet { needsDisplay = true } }
    /// 0..1 呼吸相位，驱动定位光晕与悬停环。
    var hoverPhase: Double = 0 { didSet { needsDisplay = true } }

    override var isOpaque: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        let tip = CGPoint(x: 30, y: bounds.height - 30)
        let phase = hoverPhase.truncatingRemainder(dividingBy: 1)
        let accent = NSColor.controlAccentColor

        // 柔光：让指针在任何背景上都有存在感，但不抢内容。
        context.saveGState()
        if let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                     colors: [accent.withAlphaComponent(0.20).cgColor,
                                              accent.withAlphaComponent(0).cgColor] as CFArray,
                                     locations: [0, 1]) {
            context.drawRadialGradient(gradient, startCenter: tip, startRadius: 1,
                                       endCenter: tip, endRadius: 26, options: [])
        }
        context.restoreGState()

        // 向外扩散的涟漪：三条错开的圆环持续从指针处扩散并淡出。
        for index in 0..<3 {
            let progress = (phase + Double(index) / 3.0).truncatingRemainder(dividingBy: 1)
            let radius = 7 + 22 * progress
            let alpha = 0.55 * pow(1 - progress, 1.4)
            context.saveGState()
            context.setStrokeColor(accent.withAlphaComponent(alpha).cgColor)
            context.setLineWidth(2.2 * (1 - 0.5 * progress))
            context.strokeEllipse(in: CGRect(x: tip.x - radius, y: tip.y - radius,
                                             width: radius * 2, height: radius * 2))
            context.restoreGState()
        }

        // 点击时额外一圈更亮的涟漪。
        if let progress = rippleProgress {
            let radius = 4 + 26 * progress
            let alpha = 0.70 * (1 - progress)
            context.saveGState()
            context.setStrokeColor(accent.withAlphaComponent(alpha).cgColor)
            context.setLineWidth(3.0 * (1 - 0.5 * progress))
            context.strokeEllipse(in: CGRect(x: tip.x - radius, y: tip.y - radius,
                                             width: radius * 2, height: radius * 2))
            context.restoreGState()
        }

        let arrow = CGMutablePath()
        arrow.move(to: tip)
        arrow.addLine(to: CGPoint(x: tip.x, y: tip.y - 17))
        arrow.addLine(to: CGPoint(x: tip.x + 4.6, y: tip.y - 12.4))
        arrow.addLine(to: CGPoint(x: tip.x + 12, y: tip.y - 12.4))
        arrow.closeSubpath()

        context.saveGState()
        // 投影让白色箭头在浅色桌面上也不糊掉。
        context.setShadow(offset: CGSize(width: 0, height: -1.5), blur: 4,
                          color: NSColor.black.withAlphaComponent(0.55).cgColor)
        context.addPath(arrow)
        context.setFillColor(NSColor.white.cgColor)
        context.setStrokeColor(NSColor.black.withAlphaComponent(0.88).cgColor)
        context.setLineWidth(1.5)
        context.setLineJoin(.round)
        context.drawPath(using: .fillStroke)
        context.restoreGState()
    }
}
