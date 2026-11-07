import AppKit

// AppKit translation of the editable Figma wizard, nodes 79:1008–79:1011.
// Keep the existing permission model and native button target/action behavior.
@MainActor private enum WizardUI {
    @discardableResult static func text(_ parent: NSView, _ value: String, _ frame: NSRect,
                                       size: CGFloat = 13, color: NSColor = MenuBarTheme.text,
                                       weight: NSFont.Weight = .regular, lineHeight: CGFloat? = nil) -> NSTextField {
        let label = PermissionUI.text(value, x: frame.minX, y: frame.minY, width: frame.width,
                                      height: frame.height, size: size, weight: weight, color: color)
        if let lineHeight {
            let paragraph = NSMutableParagraphStyle()
            paragraph.minimumLineHeight = lineHeight; paragraph.maximumLineHeight = lineHeight
            label.attributedStringValue = NSAttributedString(string: value, attributes: [
                .font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: color,
                .paragraphStyle: paragraph,
            ])
        }
        parent.addSubview(label)
        return label
    }
    @discardableResult static func card(_ parent: NSView, _ frame: NSRect,
                                       fill: NSColor = MenuBarTheme.window, radius: CGFloat = 12) -> MenuBarCard {
        let card = MenuBarCard(frame: frame)
        card.fill = fill; card.radius = radius
        parent.addSubview(card)
        return card
    }
    static func icon(_ parent: NSView, _ name: String, _ frame: NSRect, tint: NSColor = MenuBarTheme.accent) {
        let icon = MenuBarPanelView.icon(name, tint: tint)
        icon.frame = frame; parent.addSubview(icon)
    }
    static func mark(_ parent: NSView, _ frame: NSRect, size: CGFloat = 22) {
        let tile = card(parent, frame, fill: MenuBarTheme.accent, radius: 10)
        icon(tile, "status-enabled", NSRect(x: (frame.width - size) / 2, y: (frame.height - size) / 2,
                                          width: size, height: size), tint: .white)
    }
    static func tag(_ parent: NSView, _ value: String, _ frame: NSRect,
                    fill: NSColor = MenuBarTheme.accentSoft, color: NSColor = MenuBarTheme.accent) {
        let pill = card(parent, frame, fill: fill, radius: 13)
        text(pill, value, NSRect(x: 10, y: 5, width: frame.width - 20, height: 18),
             size: 10, color: color, weight: .medium)
    }
}

/// Retains NSButton keyboard and accessibility semantics with the Figma button
/// geometry. Native rounded bezels otherwise ignore the requested 42pt height.
@MainActor private final class WizardButton: NSButton {
    private let primary: Bool
    private let link: Bool
    private var hovered = false
    override var isFlipped: Bool { true }

    init(_ title: String, frame: NSRect, target: AnyObject, action: Selector,
         primary: Bool = false, link: Bool = false) {
        self.primary = primary; self.link = link
        super.init(frame: frame)
        self.title = title; self.target = target; self.action = action
        setButtonType(.momentaryPushIn); isBordered = false; focusRingType = .exterior
        font = .systemFont(ofSize: link ? 11 : 13, weight: link ? .medium : .regular)
        setAccessibilityLabel(title)
    }
    required init?(coder: NSCoder) { nil }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for area in trackingAreas { removeTrackingArea(area) }
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.activeAlways, .mouseEnteredAndExited, .inVisibleRect], owner: self))
    }
    override func mouseEntered(with event: NSEvent) { hovered = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { hovered = false; needsDisplay = true }
    override func highlight(_ flag: Bool) { super.highlight(flag); needsDisplay = true }
    override func draw(_ dirtyRect: NSRect) {
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10)
        if !link {
            let fill = !isEnabled ? MenuBarTheme.window : primary ? MenuBarTheme.accent
                : hovered || isHighlighted ? MenuBarTheme.hover : MenuBarTheme.surface
            fill.setFill(); shape.fill()
            if !primary { MenuBarTheme.separator.setStroke(); shape.lineWidth = 1; shape.stroke() }
            if primary && isEnabled && (hovered || isHighlighted) {
                NSColor.black.withAlphaComponent(0.08).setFill(); shape.fill()
            }
        }
        let color = !isEnabled ? MenuBarTheme.tertiary.withAlphaComponent(0.5)
            : primary ? NSColor.white : link ? MenuBarTheme.accent : MenuBarTheme.text
        let attributes: [NSAttributedString.Key: Any] = [.font: font!, .foregroundColor: color]
        let size = (title as NSString).size(withAttributes: attributes)
        (title as NSString).draw(at: NSPoint(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2),
                                withAttributes: attributes)
    }
    override func drawFocusRingMask() { NSBezierPath(roundedRect: bounds, xRadius: 10, yRadius: 10).fill() }
    override var focusRingMaskBounds: NSRect { bounds }
}

@MainActor private final class WizardBrandRail: MenuBarCard {
    override func draw(_ dirtyRect: NSRect) {
        let start = NSColor(srgbRed: 17 / 255, green: 23 / 255, blue: 34 / 255, alpha: 1)
        let end = NSColor(srgbRed: 10 / 255, green: 36 / 255, blue: 82 / 255, alpha: 1)
        NSGradient(starting: start, ending: end)?.draw(in: bounds, angle: 30)
        let glow = NSGradient(colorsAndLocations: (NSColor.systemBlue.withAlphaComponent(0.30), 0),
                              (NSColor.systemBlue.withAlphaComponent(0), 1))
        glow?.draw(fromCenter: NSPoint(x: 100, y: 370), radius: 0,
                  toCenter: NSPoint(x: 100, y: 370), radius: 270, options: [])
    }

    init(step: PermissionWizardStep, status: DesktopPermissionStatus?) {
        super.init(frame: NSRect(x: 0, y: 0, width: 276, height: 640))
        radius = 0
        WizardUI.mark(self, NSRect(x: 32, y: 32, width: 34, height: 34), size: 21)
        WizardUI.text(self, "Workbuddy CUA", NSRect(x: 78, y: 35, width: 182, height: 24),
                      size: 14, color: .white, weight: .medium)
        WizardUI.text(self, "DESKTOP COMPANION", NSRect(x: 78, y: 59, width: 180, height: 13),
                      size: 8, color: .white.withAlphaComponent(0.48))
        WizardUI.text(self, "你的桌面，\n由你掌控。", NSRect(x: 32, y: 104, width: 224, height: 76),
                      size: 26, color: .white, weight: .medium, lineHeight: 36)
        WizardUI.text(self, "把日常操作，交给更好的协作。", NSRect(x: 32, y: 187, width: 224, height: 18),
                      size: 10, color: .white.withAlphaComponent(0.6))
        addArtwork(step: step, ready: status?.allGranted == true)

        let titles = ["认识你的助手", "允许操作应用", "允许获取画面", "开始使用"]
        let details = ["了解能力与控制方式", "辅助功能权限", "屏幕录制权限", "完成设置，随时出发"]
        let completed = [step != .welcome, status?.accessibility == true,
                         status?.screenRecording == true, step == .complete && status?.allGranted == true]
        for index in 0..<4 {
            let current = index == step.rawValue
            let row = WizardUI.card(self, NSRect(x: 20, y: 382 + CGFloat(index) * 44, width: 236, height: 42),
                                    fill: current ? MenuBarTheme.accent.withAlphaComponent(0.25) : .clear, radius: 10)
            let dot = WizardUI.card(row, NSRect(x: 12, y: 10, width: 22, height: 22),
                                    fill: completed[index] ? NSColor(srgbRed: 0.09, green: 0.45, blue: 0.27, alpha: 1)
                                        : current ? MenuBarTheme.accent : NSColor.white.withAlphaComponent(0.12), radius: 11)
            let number = WizardUI.text(dot, completed[index] ? "✓" : String(index + 1),
                                        NSRect(x: 0, y: 2, width: 22, height: 19), size: 10, color: .white, weight: .medium)
            number.alignment = .center
            WizardUI.text(row, titles[index], NSRect(x: 46, y: 3, width: 174, height: 19), size: 11,
                          color: .white.withAlphaComponent(current ? 1 : completed[index] ? 0.85 : 0.55), weight: .medium)
            WizardUI.text(row, details[index], NSRect(x: 46, y: 23, width: 174, height: 13), size: 8,
                          color: .white.withAlphaComponent(current ? 0.64 : completed[index] ? 0.48 : 0.32))
        }
        WizardUI.card(self, NSRect(x: 32, y: 579, width: 212, height: 1), fill: .white.withAlphaComponent(0.12), radius: 0)
        WizardUI.icon(self, "lock", NSRect(x: 32, y: 597, width: 14, height: 14), tint: .white.withAlphaComponent(0.5))
        WizardUI.text(self, "授权可随时撤回", NSRect(x: 54, y: 596, width: 184, height: 18), size: 10,
                      color: .white.withAlphaComponent(0.5))
    }
    required init?(coder: NSCoder) { nil }

    private func addArtwork(step: PermissionWizardStep, ready: Bool) {
        // Window chrome is composed from native cards; glyphs reuse the exact
        // Figma exports already embedded in MenuBarAssets.
        let scene = WizardUI.card(self, NSRect(x: 16, y: 222, width: 244, height: 140), fill: .clear, radius: 0)
        scene.setAccessibilityElement(false)
        let rear = WizardUI.card(scene, NSRect(x: 26, y: 3, width: 188, height: 96),
                                  fill: .white.withAlphaComponent(0.10), radius: 10)
        rear.frameRotation = -5
        let front = WizardUI.card(scene, NSRect(x: 8, y: 18, width: 196, height: 106),
                                   fill: NSColor(srgbRed: 0.96, green: 0.97, blue: 0.99, alpha: 1), radius: 12)
        let gray = NSColor(srgbRed: 0.84, green: 0.87, blue: 0.91, alpha: 1)
        WizardUI.card(front, NSRect(x: 0, y: 25, width: 196, height: 1), fill: gray.withAlphaComponent(0.4), radius: 0)
        for index in 0..<3 {
            WizardUI.card(front, NSRect(x: 10 + CGFloat(index) * 9, y: 10, width: 4, height: 4),
                          fill: NSColor.darkGray.withAlphaComponent(0.6), radius: 2)
            WizardUI.card(front, NSRect(x: 10, y: 38 + CGFloat(index) * 15, width: 22, height: 4), fill: gray, radius: 2)
        }
        WizardUI.card(front, NSRect(x: 54, y: 39, width: 76, height: 6), fill: gray, radius: 3)
        WizardUI.card(front, NSRect(x: 54, y: 55, width: 128, height: 5), fill: gray, radius: 2)
        let selected = WizardUI.card(front, NSRect(x: 54, y: 70, width: 128, height: 22),
                                      fill: NSColor(srgbRed: 0.9, green: 0.94, blue: 1, alpha: 1), radius: 5)
        WizardUI.text(selected, step == .screenRecording ? "只看任务中的画面" : "完成今日的工作",
                      NSRect(x: 8, y: 4, width: 116, height: 13), size: 7,
                      color: NSColor(srgbRed: 0, green: 0.42, blue: 1, alpha: 1), weight: .medium)
        let pointer = WizardUI.card(scene, NSRect(x: 173, y: 65, width: 48, height: 48), fill: MenuBarTheme.accent, radius: 14)
        pointer.frameRotation = -10
        if ready && step == .complete {
            let tick = WizardUI.text(pointer, "✓", NSRect(x: 0, y: 4, width: 48, height: 40), size: 27, color: .white, weight: .medium)
            tick.alignment = .center
        } else { WizardUI.icon(pointer, step == .screenRecording ? "eye" : "cursor",
                               NSRect(x: 12, y: 12, width: 24, height: 24), tint: .white) }
        let pill = WizardUI.card(scene, NSRect(x: 66, y: 111, width: 126, height: 25), fill: .white, radius: 12)
        WizardUI.card(pill, NSRect(x: 10, y: 10, width: 5, height: 5),
                      fill: NSColor(srgbRed: 0.09, green: 0.45, blue: 0.27, alpha: 1), radius: 2.5)
        WizardUI.text(pill, ready ? "已准备就绪" : "准备与你协作", NSRect(x: 23, y: 5, width: 96, height: 15),
                      size: 8, color: NSColor.darkGray, weight: .medium)
    }
}

@MainActor final class MenuBarPermissionWizardView: MenuBarCard {
    private(set) var step = PermissionWizardStep.welcome
    private(set) var nextButton = NSButton()
    private(set) var authorizeButton = NSButton()
    var advance: (() -> Void)?
    var retreat: (() -> Void)?
    var authorize: ((DesktopPermission) -> Void)?
    var recheck: (() -> Void)?

    init() {
        super.init(frame: NSRect(x: 0, y: 0, width: 900, height: 640))
        fill = MenuBarTheme.surface; radius = 0
    }
    required init?(coder: NSCoder) { nil }

    func refresh(step: PermissionWizardStep, status: DesktopPermissionStatus?, busy: Bool, error: String?) {
        self.step = step
        for view in subviews { view.removeFromSuperview() }
        addSubview(WizardBrandRail(step: step, status: status))
        let main = WizardUI.card(self, NSRect(x: 276, y: 0, width: 624, height: 552), fill: MenuBarTheme.surface, radius: 0)
        let footer = WizardUI.card(self, NSRect(x: 276, y: 552, width: 624, height: 88), fill: MenuBarTheme.surface, radius: 0)
        WizardUI.card(footer, NSRect(x: 48, y: 0, width: 528, height: 1), fill: MenuBarTheme.separator, radius: 0)
        let note = WizardUI.text(footer, error ?? (step == .welcome ? "可稍后继续设置" : step == .complete
            ? "你的选择，始终由你决定" : "可随时关闭向导，稍后继续"), NSRect(x: 48, y: 7, width: 528, height: 28),
            size: 10, color: error == nil ? MenuBarTheme.tertiary : MenuBarTheme.warning)
        note.maximumNumberOfLines = 2; note.toolTip = error
        let back = WizardButton(step == .welcome ? "稍后设置" : "返回",
                                frame: NSRect(x: 48, y: 38, width: step == .welcome ? 108 : 92, height: 42),
                                target: self, action: #selector(goBack))
        footer.addSubview(back)
        nextButton = WizardButton((step == .welcome ? "开始设置" : step == .complete ? "开始使用" : "下一步") + "  →",
                                  frame: NSRect(x: 440, y: 38, width: 136, height: 42), target: self,
                                  action: #selector(next), primary: true)
        nextButton.isEnabled = step.canContinue(status) && (step.permission == nil || !busy)
        nextButton.keyEquivalent = nextButton.isEnabled ? "\r" : ""
        footer.addSubview(nextButton)
        if step == .welcome { welcome(in: main) }
        else if let permission = step.permission { authorization(in: main, permission: permission, status: status, busy: busy) }
        else { completion(in: main, status: status) }
        back.nextKeyView = step.permission == nil ? nextButton : authorizeButton
        nextButton.nextKeyView = back
    }

    private func welcome(in main: NSView) {
        WizardUI.tag(main, "为你的 Mac，准备一位新搭档", NSRect(x: 48, y: 36, width: 194, height: 26))
        WizardUI.text(main, "让助手成为你的\n桌面搭档", NSRect(x: 48, y: 86, width: 528, height: 88),
                      size: 30, weight: .medium, lineHeight: 42)
        WizardUI.text(main, "观察应用、完成操作、处理浏览器任务。\n先花一点时间，设置属于你的控制方式。",
                      NSRect(x: 48, y: 184, width: 528, height: 48), color: MenuBarTheme.secondary, lineHeight: 22)
        for (index, item) in [
            ("eye", "看懂你的工作现场", "识别应用窗口与界面，了解当前任务。"),
            ("cursor", "把想法变成操作", "点击、填写、滚动，让重复工作更轻松。"),
            ("browser", "让浏览器加入协作", "在指定标签页中完成任务、查看执行结果。"),
        ].enumerated() {
            let row = WizardUI.card(main, NSRect(x: 48, y: 254 + CGFloat(index) * 72, width: 528, height: 62))
            let tile = WizardUI.card(row, NSRect(x: 14, y: 13, width: 36, height: 36), fill: MenuBarTheme.surface, radius: 10)
            WizardUI.icon(tile, item.0, NSRect(x: 8, y: 8, width: 20, height: 20))
            WizardUI.text(row, item.1, NSRect(x: 66, y: 10, width: 430, height: 22), weight: .medium)
            WizardUI.text(row, item.2, NSRect(x: 66, y: 35, width: 430, height: 18), size: 11, color: MenuBarTheme.secondary)
        }
        let note = WizardUI.card(main, NSRect(x: 48, y: 486, width: 528, height: 42), fill: MenuBarTheme.accentSoft, radius: 10)
        WizardUI.icon(note, "lock", NSRect(x: 14, y: 13, width: 16, height: 16))
        WizardUI.text(note, "系统授权前，相关能力保持关闭。控制权始终在你。",
                      NSRect(x: 40, y: 12, width: 474, height: 20), size: 11, color: MenuBarTheme.accent)
    }

    private func authorization(in main: NSView, permission: DesktopPermission, status: DesktopPermissionStatus?, busy: Bool) {
        let ax = permission == .accessibility, granted = status?.granted(permission) == true
        WizardUI.tag(main, permission.title, NSRect(x: 48, y: 36, width: 80, height: 26))
        let progress = WizardUI.text(main, ax ? "02 / 04" : "03 / 04", NSRect(x: 488, y: 41, width: 88, height: 18),
                                    size: 11, color: MenuBarTheme.tertiary, weight: .medium)
        progress.alignment = .right
        WizardUI.text(main, ax ? "让助手读懂你的界面" : "让助手看见任务画面", NSRect(x: 48, y: 86, width: 528, height: 44),
                      size: 28, weight: .medium)
        WizardUI.text(main, ax ? "识别应用控件，执行点击、键盘和文字输入。\n每次任务都使用你在控制面板中允许的能力。"
            : "用于任务截图与实时预览。\n按任务选择的应用、窗口或桌面范围共享画面。",
            NSRect(x: 48, y: 144, width: 528, height: 48), color: MenuBarTheme.secondary, lineHeight: 22)
        let setting = WizardUI.card(main, NSRect(x: 48, y: 210, width: 528, height: 188), radius: 16)
        WizardUI.icon(setting, "lock", NSRect(x: 20, y: 19, width: 16, height: 16), tint: MenuBarTheme.secondary)
        WizardUI.text(setting, "隐私与安全性  /  \(permission.title)", NSRect(x: 46, y: 17, width: 356, height: 20),
                      size: 11, color: MenuBarTheme.secondary, weight: .medium)
        WizardUI.tag(setting, granted ? "已授权" : "待授权", NSRect(x: 438, y: 14, width: 70, height: 26),
                     fill: MenuBarTheme.surface, color: granted ? MenuBarTheme.success : MenuBarTheme.warning)
        let app = WizardUI.card(setting, NSRect(x: 16, y: 56, width: 496, height: 66), fill: MenuBarTheme.surface, radius: 10)
        WizardUI.mark(app, NSRect(x: 14, y: 14, width: 36, height: 36))
        let name = WizardUI.text(app, status?.applicationName ?? "Workbuddy CUA", NSRect(x: 64, y: 11, width: 352, height: 24),
                                 size: 14, weight: .medium)
        name.maximumNumberOfLines = 1; name.lineBreakMode = .byTruncatingTail; name.toolTip = status?.applicationName
        WizardUI.text(app, ax ? "允许读取与操作应用" : "允许任务截图与实时预览", NSRect(x: 64, y: 38, width: 352, height: 16),
                      size: 10, color: MenuBarTheme.secondary)
        // A decorative preview of the system page. It cannot change TCC state.
        let toggle = WizardUI.card(app, NSRect(x: 440, y: 23, width: 38, height: 22),
                                    fill: granted ? MenuBarTheme.accent : MenuBarTheme.separator, radius: 11)
        toggle.setAccessibilityElement(false)
        WizardUI.card(toggle, NSRect(x: granted ? 18 : 2, y: 2, width: 18, height: 18), fill: .white, radius: 9)
        WizardUI.text(setting, "系统设置界面示意", NSRect(x: 20, y: 145, width: 260, height: 18), size: 10, color: MenuBarTheme.tertiary)
        authorizeButton = WizardButton(granted ? "查看设置  ↗" : "前往授权  ↗", frame: NSRect(x: 368, y: 132, width: 140, height: 42),
                                       target: self, action: #selector(request), primary: true)
        authorizeButton.isEnabled = !busy
        authorizeButton.keyEquivalent = !granted && !busy ? "\r" : ""
        setting.addSubview(authorizeButton)
        for (index, pair) in [
            (ax ? "在列表中允许系统显示的应用" : "在列表中开启屏幕录制权限",
             ax ? "授权对象以系统对话框和实际检测结果为准。" : "如系统提示重新打开应用，请按提示操作。"),
            (ax ? "返回向导，状态会自动更新" : "重新打开后，返回这里检测",
             granted ? "此项权限已开启；你可以继续下一步。" : ax ? "尚未授权的原生观察与屏幕操作保持关闭。" : "尚未授权时，预览与需要画面的能力保持关闭。"),
        ].enumerated() {
            let y: CGFloat = 420 + CGFloat(index) * 56
            WizardUI.text(main, String(index + 1), NSRect(x: 48, y: y + 2, width: 24, height: 22),
                          size: 12, color: MenuBarTheme.accent, weight: .medium)
            WizardUI.text(main, pair.0, NSRect(x: 78, y: y, width: 498, height: 22), size: 12, weight: .medium)
            WizardUI.text(main, pair.1, NSRect(x: 78, y: y + 25, width: index == 0 ? 498 : 386, height: 20),
                          size: 11, color: MenuBarTheme.secondary)
        }
        let check = WizardButton(busy ? "正在检测…" : "重新检测", frame: NSRect(x: 492, y: 494, width: 88, height: 30),
                                 target: self, action: #selector(recheckStatus), link: true)
        check.isEnabled = !busy; main.addSubview(check)
        authorizeButton.nextKeyView = check; check.nextKeyView = nextButton
    }

    private func completion(in main: NSView, status: DesktopPermissionStatus?) {
        let ready = status?.allGranted == true
        WizardUI.tag(main, "设置完成", NSRect(x: 48, y: 36, width: 80, height: 26), fill: MenuBarTheme.window,
                     color: ready ? MenuBarTheme.success : MenuBarTheme.secondary)
        let progress = WizardUI.text(main, "04 / 04", NSRect(x: 488, y: 41, width: 88, height: 18),
                                    size: 11, color: MenuBarTheme.tertiary, weight: .medium)
        progress.alignment = .right
        WizardUI.text(main, ready ? "一切就绪，\n把下一步交给助手" : "本次设置已完成，\n随时继续你的工作",
                      NSRect(x: 48, y: 86, width: 408, height: 88), size: 30, weight: .medium, lineHeight: 42)
        WizardUI.card(main, NSRect(x: 470, y: 89, width: 96, height: 96), fill: MenuBarTheme.accentSoft, radius: 48)
        let dot = WizardUI.card(main, NSRect(x: 486, y: 105, width: 64, height: 64), fill: MenuBarTheme.accent, radius: 32)
        let tick = WizardUI.text(dot, ready ? "✓" : "→", NSRect(x: 0, y: 9, width: 64, height: 50), size: 36, color: .white, weight: .medium)
        tick.alignment = .center
        WizardUI.card(main, NSRect(x: 555, y: 99, width: 6, height: 6), fill: MenuBarTheme.accent.withAlphaComponent(0.65), radius: 3)
        WizardUI.card(main, NSRect(x: 463, y: 173, width: 5, height: 5), fill: MenuBarTheme.success.withAlphaComponent(0.65), radius: 2.5)
        WizardUI.text(main, ready ? "所需系统权限已开启。\n现在可以在控制面板选择允许助手使用的能力。"
            : "已完成本次权限设置。尚未授权的能力继续保持关闭。\n稍后打开对应开关，就能继续授权。",
            NSRect(x: 48, y: 184, width: 528, height: 48), color: MenuBarTheme.secondary, lineHeight: 22)
        for (index, permission) in DesktopPermission.allCases.enumerated() {
            let granted = status?.granted(permission) == true
            let row = WizardUI.card(main, NSRect(x: 48, y: 258 + CGFloat(index) * 78, width: 528, height: 66))
            let tile = WizardUI.card(row, NSRect(x: 14, y: 15, width: 36, height: 36), fill: MenuBarTheme.surface, radius: 10)
            WizardUI.icon(tile, permission == .accessibility ? "cursor" : "eye", NSRect(x: 8, y: 8, width: 20, height: 20))
            WizardUI.text(row, permission.title, NSRect(x: 66, y: 10, width: 340, height: 22), weight: .medium)
            WizardUI.text(row, permission == .accessibility ? "读取界面并完成鼠标、键盘与文字输入" : "获取任务画面，并在预览面板中实时查看",
                          NSRect(x: 66, y: 35, width: 340, height: 18), size: 11, color: MenuBarTheme.secondary)
            WizardUI.text(row, granted ? "✓  已授权" : "未授权", NSRect(x: 430, y: 23, width: 86, height: 20), size: 11,
                          color: granted ? MenuBarTheme.success : MenuBarTheme.warning, weight: .medium)
        }
        let tip = WizardUI.card(main, NSRect(x: 48, y: 432, width: 528, height: 94), fill: MenuBarTheme.accentSoft)
        WizardUI.mark(tip, NSRect(x: 16, y: 20, width: 42, height: 42), size: 25)
        WizardUI.text(tip, "点击菜单栏图标，随时掌控能力", NSRect(x: 74, y: 16, width: 432, height: 24), weight: .medium)
        WizardUI.text(tip, "关闭能力、查看任务、管理权限，都在这里。", NSRect(x: 74, y: 44, width: 432, height: 18), size: 11, color: MenuBarTheme.secondary)
        WizardUI.text(tip, "系统权限也可以随时撤回。", NSRect(x: 74, y: 66, width: 432, height: 16), size: 10, color: MenuBarTheme.secondary)
    }

    @objc private func next() { advance?() }
    @objc private func goBack() { retreat?() }
    @objc private func request() { if let permission = step.permission { authorize?(permission) } }
    @objc private func recheckStatus() { recheck?() }
}
