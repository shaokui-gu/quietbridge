import AppKit

// Semantic colors from WorkBuddy CUA / Appearance, Light and Dark modes.
enum MenuBarTheme {
    static let window = color(0xF7F8FA, 0x24262B)
    static let surface = color(0xFFFFFF, 0x303238)
    static let hover = color(0xE8EBF0, 0x3B3E46)
    static let text = color(0x202329, 0xF4F5F7)
    static let secondary = color(0x626975, 0xA7AEBB)
    static let tertiary = color(0x69717F, 0x929BAA)
    static let separator = color(0xE2E5EB, 0x454A54)
    static let accent = color(0x006CFF, 0x418DFF)
    static let accentSoft = color(0xE7F0FF, 0x233D62)
    static let success = color(0x187444, 0x61D69A)
    static let warning = color(0x99570D, 0xF0BA65)
    static let off = color(0x858E9B, 0x7F8998)

    private static func color(_ light: UInt32, _ dark: UInt32) -> NSColor {
        NSColor(name: nil) { appearance in
            let value = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((value >> 16) & 255) / 255,
                           green: CGFloat((value >> 8) & 255) / 255,
                           blue: CGFloat(value & 255) / 255, alpha: 1)
        }
    }
}

@MainActor class MenuBarCard: NSView {
    var fill = MenuBarTheme.surface
    var radius: CGFloat = 12
    var bordered = false
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        if radius == 0 { fill.setFill(); bounds.fill(); return }
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: radius, yRadius: radius)
        fill.setFill()
        path.fill()
        if bordered {
            MenuBarTheme.separator.setStroke()
            path.lineWidth = 1
            path.stroke()
        }
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        func invalidate(_ view: NSView) {
            view.needsDisplay = true
            for child in view.subviews { invalidate(child) }
        }
        invalidate(self)
    }
}

@MainActor private final class MenuBarSectionCard: MenuBarCard {
    weak var hoveredRow: MenuBarSwitchRow? {
        didSet { if hoveredRow !== oldValue { needsDisplay = true } }
    }

    override func draw(_ dirtyRect: NSRect) {
        // Repaint the card beneath the old highlight before drawing the new one.
        super.draw(dirtyRect)
        guard let hoveredRow else { return }
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: radius, yRadius: radius).addClip()
        MenuBarTheme.hover.setFill()
        convert(hoveredRow.bounds, from: hoveredRow).fill()
        NSGraphicsContext.restoreGraphicsState()
    }
}

/// An ordinary AppKit switch button with a compact 34 × 20 visual. The whole
/// row is its hit area, so mouse, Space, focus rings and AX share one control.
@MainActor final class MenuBarSwitchRow: NSButton {
    let label: NSTextField
    let detail: NSTextField
    private let icon: NSImageView?
    private let master: Bool
    private let contentInsets: NSEdgeInsets
    override var isFlipped: Bool { true }

    init(key: String, title: String, subtitle: String, iconName: String?, master: Bool = false,
         contentInsets: NSEdgeInsets = .init()) {
        self.master = master
        self.contentInsets = contentInsets
        label = MenuBarPanelView.label(title, size: master ? 14 : 13, weight: .medium)
        detail = MenuBarPanelView.label(subtitle, size: 11, color: MenuBarTheme.secondary)
        icon = iconName.map { MenuBarPanelView.icon($0) }
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier(key)
        setButtonType(.switch)
        self.title = ""
        isBordered = false
        focusRingType = .exterior
        setAccessibilityLabel(title)
        setAccessibilityHelp(subtitle)
        addSubview(label)
        addSubview(detail)
        if let icon { addSubview(icon) }
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(equalToConstant: master ? 65 : 42 + contentInsets.top + contentInsets.bottom).isActive = true
    }

    required init?(coder: NSCoder) { nil }

    override func layout() {
        super.layout()
        let content = contentBounds
        let x: CGFloat = master ? 12 : 30
        let top: CGFloat = master ? 12 : 2.5
        label.frame = NSRect(x: content.minX + (master ? 26 : x), y: content.minY + top, width: content.width - x - 56, height: 20)
        detail.frame = NSRect(x: content.minX + x, y: content.minY + top + (master ? 24 : 20), width: content.width - x - 48, height: 17)
        icon?.frame = NSRect(x: content.minX, y: content.midY - 9, width: 18, height: 18)
    }

    private var contentBounds: NSRect {
        NSRect(x: contentInsets.left, y: contentInsets.top,
               width: bounds.width - contentInsets.left - contentInsets.right,
               height: bounds.height - contentInsets.top - contentInsets.bottom)
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        isHidden || !bounds.contains(convert(point, from: superview)) ? nil : self
    }

    override func draw(_ dirtyRect: NSRect) {
        let content = contentBounds
        let x = content.maxX - (master ? 24 : 0) - 34
        let track = NSRect(x: x, y: content.midY - 10, width: 34, height: 20)
        (state == .on ? MenuBarTheme.accent : MenuBarTheme.off).setFill()
        NSBezierPath(roundedRect: track, xRadius: 10, yRadius: 10).fill()
        NSColor.white.setFill()
        NSBezierPath(ovalIn: NSRect(x: x + (state == .on ? 16 : 2), y: track.minY + 2, width: 16, height: 16)).fill()
        if master {
            (state == .on ? MenuBarTheme.success : MenuBarTheme.warning).setFill()
            NSBezierPath(ovalIn: NSRect(x: 12, y: 19, width: 6, height: 6)).fill()
        }
    }

    override func drawFocusRingMask() {
        NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
    }
    override var focusRingMaskBounds: NSRect { bounds }

    func update(value: Bool, enabled: Bool) {
        state = value ? .on : .off
        isEnabled = enabled
        alphaValue = enabled ? 1 : 0.4
        needsDisplay = true
    }
}

@MainActor final class MenuBarPluginCard: MenuBarCard {
    var isHovered = false { didSet { if isHovered != oldValue { needsDisplay = true } } }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        if isHovered {
            MenuBarTheme.hover.setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: radius, yRadius: radius).fill()
        }
    }
}

@MainActor final class MenuBarNavigationRow: NSButton {
    private let icon = MenuBarPanelView.icon("browser")
    private let label = MenuBarPanelView.label("浏览器任务", size: 13, weight: .medium)
    private let arrow = NSImageView()
    var isHovered = false { didSet { if isHovered != oldValue { needsDisplay = true } } }
    var selected = false { didSet { needsDisplay = true; updateColors() } }
    override var isFlipped: Bool { true }

    init() {
        super.init(frame: .zero)
        title = ""; isBordered = false; focusRingType = .exterior
        setAccessibilityLabel("浏览器任务")
        setAccessibilityHelp("打开浏览器任务详情")
        arrow.image = NSImage(systemSymbolName: "chevron.right", accessibilityDescription: nil)
        arrow.imageScaling = .scaleProportionallyUpOrDown
        arrow.setAccessibilityElement(false)
        addSubview(icon); addSubview(label); addSubview(arrow)
        updateColors()
    }
    required init?(coder: NSCoder) { nil }
    private func updateColors() {
        icon.contentTintColor = selected ? MenuBarTheme.accent : MenuBarTheme.secondary
        label.textColor = selected ? MenuBarTheme.accent : MenuBarTheme.text
        arrow.contentTintColor = selected ? MenuBarTheme.accent : MenuBarTheme.secondary
    }
    override func layout() {
        super.layout()
        icon.frame = NSRect(x: 12, y: (bounds.height - 18) / 2, width: 18, height: 18)
        label.frame = NSRect(x: 42, y: (bounds.height - 20) / 2, width: max(0, bounds.width - 76), height: 20)
        arrow.frame = NSRect(x: bounds.width - 24, y: (bounds.height - 12) / 2, width: 12, height: 12)
    }
    override func hitTest(_ point: NSPoint) -> NSView? {
        isHidden || !bounds.contains(convert(point, from: superview)) ? nil : self
    }
    override func draw(_ dirtyRect: NSRect) {
        MenuBarTheme.window.setFill()
        bounds.fill()
        if selected || isHovered {
            (selected ? MenuBarTheme.accentSoft : MenuBarTheme.hover).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
        }
    }
    override func drawFocusRingMask() { NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill() }
    override var focusRingMaskBounds: NSRect { bounds }
}

@MainActor final class MenuBarPanelView: MenuBarCard {
    static let panelSize = NSSize(width: 360, height: 720)
    private(set) var rows: [String: MenuBarSwitchRow] = [:]
    let installButton = NSButton(title: "安装…", target: nil, action: nil)
    let manageButton = NSButton(title: "管理…", target: nil, action: nil)
    let quitButton = NSButton(title: "退出菜单栏", target: nil, action: nil)
    let browserButton = MenuBarNavigationRow()
    private let pluginCard = MenuBarPluginCard()
    private let pluginDetails = NSView()
    private let pluginRow: MenuBarSwitchRow
    private let pluginDescription = label("允许在锁屏状态下继续使用", size: 10, color: MenuBarTheme.secondary)
    private var sectionCards: [MenuBarSectionCard] = []

    init(target: AnyObject, changed: Selector, install: Selector, quit: Selector) {
        pluginRow = MenuBarSwitchRow(key: "locked_use", title: "锁屏授权插件", subtitle: "允许在锁屏状态下继续使用", iconName: "lock")
        super.init(frame: NSRect(origin: .zero, size: Self.panelSize))
        fill = MenuBarTheme.window
        radius = 22
        bordered = true
        setAccessibilityLabel("WorkBuddy 电脑控制助手")
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 20),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -20),
        ])
        func append(_ view: NSView, height: CGFloat) {
            view.translatesAutoresizingMaskIntoConstraints = false
            stack.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            view.heightAnchor.constraint(equalToConstant: height).isActive = true
        }
        let heading = NSView()
        let mark = MenuBarCard(frame: NSRect(x: 0, y: 1.5, width: 40, height: 40))
        mark.fill = MenuBarTheme.accentSoft
        let symbol = Self.icon("status-enabled", tint: MenuBarTheme.accent)
        symbol.frame = NSRect(x: 9, y: 9, width: 18, height: 18)
        mark.addSubview(symbol)
        heading.addSubview(mark)
        let name = Self.label("WorkBuddy", size: 18, weight: .medium)
        name.frame = NSRect(x: 52, y: 17, width: 260, height: 26)
        let subtitle = Self.label("电脑控制助手", size: 11, color: MenuBarTheme.secondary)
        subtitle.frame = NSRect(x: 52, y: 0, width: 260, height: 17)
        heading.addSubview(name)
        heading.addSubview(subtitle)
        append(heading, height: 43)

        let master = MenuBarSwitchRow(key: "master", title: "已启用", subtitle: "允许助手观察和操作这台 Mac", iconName: nil, master: true)
        let masterCard = MenuBarCard()
        masterCard.addSubview(master)
        Self.pin(master, in: masterCard)
        rows["master"] = master
        master.setAccessibilityLabel("总开关")
        append(masterCard, height: 65)

        let controls = [
            ("native_observe", "原生观察", "读取屏幕、窗口和应用", "eye"),
            ("native_input", "屏幕操作", "鼠标、键盘与文字输入", "cursor"),
            ("app_launch", "启动应用", "允许助手打开应用", "app"),
            ("browser", "浏览器通道", "在浏览器中观察与操作", "browser"),
        ]
        let helpers = [
            ("preview", "预览面板", "显示操作中的实时画面", "preview"),
            ("operation_log", "操作日志", "记录助手的操作过程", "log"),
        ]
        append(section("电脑控制", entries: controls), height: 205)
        append(section("辅助功能", entries: helpers), height: 121)

        append(browserButton, height: 54)

        pluginCard.addSubview(pluginDetails)
        pluginDetails.frame = NSRect(x: 12, y: 12, width: 296, height: 37)
        let lock = Self.icon("lock")
        lock.frame = NSRect(x: 0, y: 9.5, width: 18, height: 18)
        let pluginTitle = Self.label("锁屏授权插件", size: 13, weight: .medium)
        pluginTitle.frame = NSRect(x: 30, y: 17, width: 186, height: 20)
        pluginDescription.frame = NSRect(x: 30, y: 0, width: 186, height: 17)
        pluginDetails.addSubview(lock)
        pluginDetails.addSubview(pluginTitle)
        pluginDetails.addSubview(pluginDescription)
        installButton.frame = NSRect(x: 228, y: 6, width: 56, height: 25)
        installButton.bezelStyle = .rounded
        installButton.controlSize = .small
        installButton.font = .systemFont(ofSize: 11, weight: .medium)
        installButton.target = target
        installButton.action = install
        installButton.setAccessibilityHelp("说明系统授权变更并打开插件安装器")
        pluginDetails.addSubview(installButton)
        pluginCard.addSubview(pluginRow)
        manageButton.translatesAutoresizingMaskIntoConstraints = false
        manageButton.bezelStyle = .rounded
        manageButton.controlSize = .small
        manageButton.font = .systemFont(ofSize: 11, weight: .medium)
        manageButton.target = target
        manageButton.action = install
        manageButton.setAccessibilityHelp("查看状态、更新或恢复系统授权并卸载插件")
        pluginCard.addSubview(manageButton)
        NSLayoutConstraint.activate([
            manageButton.trailingAnchor.constraint(equalTo: pluginCard.trailingAnchor, constant: -12),
            manageButton.centerYAnchor.constraint(equalTo: pluginCard.centerYAnchor),
            manageButton.widthAnchor.constraint(equalToConstant: 58),
            manageButton.heightAnchor.constraint(equalToConstant: 25),
            pluginRow.leadingAnchor.constraint(equalTo: pluginCard.leadingAnchor, constant: 8),
            pluginRow.trailingAnchor.constraint(equalTo: manageButton.leadingAnchor, constant: -10),
            pluginRow.centerYAnchor.constraint(equalTo: pluginCard.centerYAnchor),
        ])
        rows["locked_use"] = pluginRow
        append(pluginCard, height: 61)

        append(Self.label("更改对新的调用生效，已开始的操作不受影响。", size: 10, color: MenuBarTheme.secondary), height: 17)
        let separator = MenuBarCard()
        separator.radius = 0
        separator.fill = MenuBarTheme.separator
        append(separator, height: 1)
        let footer = NSView()
        let brand = Self.label("WorkBuddy CUA", size: 10, color: MenuBarTheme.tertiary)
        brand.frame = NSRect(x: 0, y: 0, width: 150, height: 17)
        footer.addSubview(brand)
        quitButton.frame = NSRect(x: 236, y: -3, width: 88, height: 23)
        quitButton.isBordered = false
        quitButton.font = .systemFont(ofSize: 11)
        quitButton.contentTintColor = MenuBarTheme.secondary
        quitButton.image = MenuBarAssets.image("quit", size: 12)
        quitButton.imagePosition = .imageLeading
        quitButton.target = target
        quitButton.action = quit
        footer.addSubview(quitButton)
        append(footer, height: 17)

        for row in rows.values { row.target = target; row.action = changed }
        refresh(preferences: CapabilityControl.shared.preferences(), pluginAvailable: false)
    }

    required init?(coder: NSCoder) { nil }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for area in trackingAreas { removeTrackingArea(area) }
        // One tracking area owns hover for the whole nonactivating panel.
        addTrackingArea(NSTrackingArea(rect: .zero,
            options: [.activeAlways, .mouseMoved, .mouseEnteredAndExited, .inVisibleRect], owner: self))
        refreshHover()
    }

    override func mouseEntered(with event: NSEvent) { updateHover(at: event.locationInWindow) }
    override func mouseMoved(with event: NSEvent) { updateHover(at: event.locationInWindow) }
    override func mouseExited(with event: NSEvent) { updateHover(at: nil) }
    override func viewDidHide() { super.viewDidHide(); updateHover(at: nil) }

    func refreshHover() {
        let location = window.flatMap { $0.isVisible ? $0.mouseLocationOutsideOfEventStream : nil }
        updateHover(at: location)
    }

    private func updateHover(at location: NSPoint?) {
        var target: NSView?
        if let location, !isHiddenOrHasHiddenAncestor,
           bounds.intersection(visibleRect).contains(convert(location, from: nil)) {
            let switches = rows.compactMap { key, row in
                key != "master" && key != "locked_use" && row.isEnabled ? row : nil
            }
            let controls: [NSView] = switches + [browserButton, pluginCard]
            // AppKit can report visibleRect beyond a nonclipping view's bounds.
            // Intersect both so neighboring rows cannot share the hover target.
            target = controls.first { control in
                !control.isHiddenOrHasHiddenAncestor &&
                    control.bounds.intersection(control.visibleRect).contains(control.convert(location, from: nil))
            }
        }
        for card in sectionCards {
            let row = target as? MenuBarSwitchRow
            card.hoveredRow = row?.isDescendant(of: card) == true ? row : nil
        }
        browserButton.isHovered = target === browserButton
        pluginCard.isHovered = target === pluginCard
    }

    func refresh(preferences: [String: Bool], pluginAvailable: Bool) {
        refresh(preferences: preferences, pluginState: pluginAvailable ? .available : .missing)
    }

    func refresh(preferences: [String: Bool], pluginState: MenuBarPluginState) {
        let pluginAvailable = pluginState == .available
        let enabled = preferences["master"] == true
        for (key, row) in rows {
            row.update(value: preferences[key] == true, enabled: key == "master" || (enabled && (key != "locked_use" || pluginAvailable)))
        }
        rows["master"]?.label.stringValue = enabled ? "已启用" : "已暂停"
        rows["master"]?.detail.stringValue = enabled ? "允许助手观察和操作这台 Mac" : "新的调用已暂停，正在执行的操作继续"
        pluginRow.isHidden = !pluginAvailable
        pluginDetails.isHidden = pluginAvailable
        manageButton.isHidden = !pluginAvailable
        pluginDescription.stringValue = pluginState.detail
        pluginDetails.setAccessibilityHelp(pluginState.detail)
        installButton.title = pluginState == .missing ? "安装…" : "管理…"
        installButton.isEnabled = true
        // Keep keyboard navigation in visual order, including the alternate
        // install/switch controls; hidden controls never trap the focus.
        let controls: [NSView] = CapabilitySwitches.keys.compactMap { key in
            key == "locked_use" && !pluginAvailable ? installButton : rows[key]
        } + (pluginAvailable ? [manageButton, browserButton, quitButton] : [browserButton, quitButton])
        for (index, control) in controls.enumerated() {
            control.nextKeyView = controls[(index + 1) % controls.count]
        }
        refreshHover()
    }

    private func section(_ title: String, entries: [(String, String, String, String)]) -> NSView {
        let section = MenuBarCard()
        section.fill = .clear
        let titleView = Self.label(title, size: 11, weight: .medium, color: MenuBarTheme.secondary)
        titleView.frame = NSRect(x: 0, y: 0, width: 320, height: 17)
        section.addSubview(titleView)
        let card = MenuBarSectionCard(frame: NSRect(x: 0, y: 21, width: 320, height: CGFloat(entries.count) * 42 + 16))
        sectionCards.append(card)
        section.addSubview(card)
        let group = NSStackView()
        group.orientation = .vertical
        group.alignment = .leading
        group.spacing = 0
        card.addSubview(group)
        Self.pin(group, in: card)
        for (index, entry) in entries.enumerated() {
            let (key, title, detail, icon) = entry
            let insets = NSEdgeInsets(top: index == 0 ? 8 : 0, left: 8,
                                     bottom: index == entries.count - 1 ? 8 : 0, right: 8)
            let row = MenuBarSwitchRow(key: key, title: title, subtitle: detail, iconName: icon, contentInsets: insets)
            group.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            rows[key] = row
        }
        return section
    }

    static func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor = MenuBarTheme.text) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.lineBreakMode = .byTruncatingTail
        label.setAccessibilityElement(false)
        return label
    }

    static func icon(_ name: String, tint: NSColor = MenuBarTheme.secondary) -> NSImageView {
        let view = NSImageView(image: MenuBarAssets.image(name))
        view.contentTintColor = tint
        view.imageScaling = .scaleProportionallyUpOrDown
        view.setAccessibilityElement(false)
        return view
    }

    private static func pin(_ view: NSView, in parent: NSView, inset: CGFloat = 0) {
        view.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset),
            view.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset),
            view.topAnchor.constraint(equalTo: parent.topAnchor, constant: inset),
            view.bottomAnchor.constraint(equalTo: parent.bottomAnchor, constant: -inset),
        ])
    }
}
