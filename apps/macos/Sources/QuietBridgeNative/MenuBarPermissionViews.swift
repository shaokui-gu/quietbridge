import AppKit

@MainActor final class MenuBarPermissionModel {
    private let session: ApplicationSession
    private var connection: MenuBarPermissionConnection?
    private var stopped = false
    private var checking = false
    private var queuedPermission: DesktopPermission?
    private(set) var status: DesktopPermissionStatus?
    private(set) var busy = false
    private(set) var error: String?
    var changed: (() -> Void)?

    init(session: ApplicationSession, connection: MenuBarPermissionConnection? = nil) {
        self.session = session; self.connection = connection
    }

    func refresh() { perform() }
    func authorize(_ permission: DesktopPermission) { perform(permission: permission) }

    private func perform(permission: DesktopPermission? = nil) {
        guard !stopped else { return }
        if checking {
            if let permission { queuedPermission = permission }
            return
        }
        checking = true
        busy = permission != nil
        let previousStatus = status, previousError = error
        error = nil
        if busy { changed?() }
        Task { [weak self] in
            guard let self else { return }
            do {
                let client: MenuBarPermissionConnection
                if let connection = self.connection { client = connection }
                else {
                    let executable = try await Task.detached(priority: .utility) {
                        try MenuBarPermissionConnection.helper()
                    }.value
                    guard !self.stopped else { return }
                    client = MenuBarPermissionConnection(executable: executable, session: self.session)
                    self.connection = client
                }
                let result: JSONValue
                if let permission {
                    // Only a deliberate click reaches this branch. Startup and
                    // polling use preflight checks and never request consent.
                    _ = try await client.request("requestPermission", permission: permission, timeout: 60)
                    _ = try await client.request("openPermissionSettings", permission: permission)
                    result = try await client.request()
                } else { result = try await client.request() }
                guard !self.stopped else { return }
                self.status = try DesktopPermissionStatus(result)
                CapabilityControl.shared.updatePermissions(self.status)
            } catch {
                guard !self.stopped else { return }
                self.status = nil
                CapabilityControl.shared.updatePermissions(nil)
                self.error = (error as? NativeFailure)?.message ?? "权限检测失败，请重新检测或重新打开应用。"
            }
            let requested = self.busy
            self.busy = false; self.checking = false
            if requested || self.status != previousStatus || self.error != previousError { self.changed?() }
            if let queued = self.queuedPermission {
                self.queuedPermission = nil
                self.perform(permission: queued)
            }
        }
    }

    func stop() {
        stopped = true
        let client = connection
        connection = nil
        Task { await client?.stop() }
    }
}

@MainActor enum PermissionOnboarding {
    static func consumeFirstLaunch(in defaults: UserDefaults = .standard) -> Bool {
        let key = "quietbridge.permissionOnboardingPresented.v1"
        guard !defaults.bool(forKey: key), !defaults.bool(forKey: QuietBridgeIdentity.previousOnboardingKey) else { return false }
        // Dismissing is a valid choice; a missing-permission toggle can reopen
        // the matching step without showing the welcome window every launch.
        defaults.set(true, forKey: key)
        return true
    }
}

enum PermissionWizardStep: Int, CaseIterable {
    case welcome, accessibility, screenRecording, complete
    var permission: DesktopPermission? {
        switch self {
        case .accessibility: return .accessibility
        case .screenRecording: return .screenRecording
        default: return nil
        }
    }
    init(_ permission: DesktopPermission) { self = permission == .accessibility ? .accessibility : .screenRecording }
    func canContinue(_ status: DesktopPermissionStatus?) -> Bool {
        permission.map { status?.granted($0) == true } ?? true
    }
}

@MainActor enum PermissionUI {
    static func text(_ text: String, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat,
                     size: CGFloat = 13, weight: NSFont.Weight = .regular,
                     color: NSColor = MenuBarTheme.text) -> NSTextField {
        let view = MenuBarPanelView.label(text, size: size, weight: weight, color: color)
        view.frame = NSRect(x: x, y: y, width: width, height: height)
        view.lineBreakMode = .byWordWrapping
        view.maximumNumberOfLines = 0
        view.cell?.wraps = true
        view.setAccessibilityElement(true)
        return view
    }
    static func button(_ title: String, x: CGFloat, y: CGFloat, width: CGFloat = 112,
                       height: CGFloat = 34, target: AnyObject, action: Selector, primary: Bool = false) -> NSButton {
        let button = NSButton(title: title, target: target, action: action)
        button.frame = NSRect(x: x, y: y, width: width, height: height)
        button.bezelStyle = .rounded
        button.font = .systemFont(ofSize: 12, weight: .medium)
        if primary {
            button.bezelColor = MenuBarTheme.accent; button.contentTintColor = .white
            button.keyEquivalent = "\r"
        }
        return button
    }
    static func card(x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat) -> MenuBarCard {
        let card = MenuBarCard(frame: NSRect(x: x, y: y, width: width, height: height))
        card.fill = MenuBarTheme.surface; card.bordered = true
        return card
    }
}

@MainActor final class MenuBarPermissionWizard: NSObject, NSWindowDelegate {
    let view = MenuBarPermissionWizardView()
    private(set) var step = PermissionWizardStep.welcome
    private let model: MenuBarPermissionModel
    private var window: NSWindow?
    private var completing = false
    var finished: (() -> Void)?
    var dismissed: (() -> Void)?
    var isVisible: Bool { window?.isVisible == true }

    init(model: MenuBarPermissionModel) {
        self.model = model
        super.init()
        view.advance = { [weak self] in self?.advance() }
        view.retreat = { [weak self] in self?.back() }
        view.authorize = { [weak model] in model?.authorize($0) }
        view.recheck = { [weak model] in model?.refresh() }
    }
    func show(permission: DesktopPermission? = nil) {
        step = permission.map(PermissionWizardStep.init) ?? .welcome
        if window == nil {
            let window = NSWindow(contentRect: view.bounds, styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "QuietBridge · 首次使用与权限设置"
            window.contentView = view; window.isReleasedWhenClosed = false
            window.delegate = self; window.center()
            self.window = window
        }
        refresh(); bringForward(); model.refresh()
    }
    func bringForward() { NSApp.activate(ignoringOtherApps: true); window?.makeKeyAndOrderFront(nil) }
    func refresh() { view.refresh(step: step, status: model.status, busy: model.busy, error: model.error) }
    func close() { window?.close() }
    func windowWillClose(_ notification: Notification) { if !completing { dismissed?() } }
    private func advance() {
        guard step.canContinue(model.status), step.permission == nil || !model.busy else { return }
        if step == .complete {
            completing = true; close(); finished?(); completing = false
            return
        }
        step = PermissionWizardStep(rawValue: step.rawValue + 1) ?? .complete
        refresh()
    }
    private func back() {
        if step == .welcome { close(); return }
        step = PermissionWizardStep(rawValue: step.rawValue - 1) ?? .welcome
        refresh()
    }
}

@MainActor final class MenuBarPermissionsView: MenuBarCard {
    var back: (() -> Void)?
    var authorize: ((DesktopPermission) -> Void)?
    var recheck: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        radius = 0; fill = MenuBarTheme.surface
        refresh(status: nil, busy: false, error: nil)
    }
    required init?(coder: NSCoder) { nil }
    func refresh(status: DesktopPermissionStatus?, busy: Bool, error: String?) {
        for view in subviews { view.removeFromSuperview() }
        let line = MenuBarCard(frame: NSRect(x: 0, y: 0, width: 1, height: bounds.height))
        line.fill = MenuBarTheme.separator; line.radius = 0; addSubview(line)
        addSubview(PermissionUI.text("系统权限", x: 24, y: 24, width: 240, height: 24, size: 16, weight: .medium))
        addSubview(PermissionUI.button("返回", x: 308, y: 20, width: 68, height: 30, target: self, action: #selector(goBack)))
        let intro = PermissionUI.card(x: 24, y: 72, width: 352, height: 95)
        intro.fill = MenuBarTheme.accentSoft; intro.bordered = false
        intro.addSubview(PermissionUI.text("由你决定助手可以做什么", x: 16, y: 14, width: 320, height: 22, weight: .medium))
        intro.addSubview(PermissionUI.text("未授权的能力保持关闭。点击“去授权”，在桌面向导中了解用途并前往系统设置。", x: 16, y: 42,
                                         width: 320, height: 40, size: 12, color: MenuBarTheme.secondary))
        addSubview(intro)
        for (index, permission) in DesktopPermission.allCases.enumerated() {
            let granted = status?.granted(permission) == true
            let card = PermissionUI.card(x: 24, y: CGFloat(187 + index * 133), width: 352, height: 121)
            card.fill = MenuBarTheme.window; card.bordered = false
            let icon = MenuBarPanelView.icon(permission == .accessibility ? "cursor" : "eye")
            icon.frame = NSRect(x: 16, y: 17, width: 18, height: 18); card.addSubview(icon)
            card.addSubview(PermissionUI.text(permission.title, x: 46, y: 15, width: 160, height: 22, weight: .medium))
            card.addSubview(PermissionUI.text(granted ? "已授权" : status == nil ? "待检测" : "待授权", x: 276, y: 18,
                width: 64, height: 18, size: 11, color: granted ? MenuBarTheme.success : MenuBarTheme.warning))
            card.addSubview(PermissionUI.text(permission == .accessibility ? "识别应用控件，执行鼠标、键盘与文字输入。"
                : "获取任务截图，并在预览面板中显示实时画面。", x: 16, y: 45, width: 320, height: 32,
                size: 11, color: MenuBarTheme.secondary))
            let button = PermissionUI.button(granted ? "查看设置" : "去授权", x: 252, y: 83, width: 84, height: 28,
                                            target: self, action: #selector(openGuide(_:)))
            button.identifier = NSUserInterfaceItemIdentifier(permission.rawValue)
            button.isEnabled = !busy; card.addSubview(button); addSubview(card)
        }
        addSubview(PermissionUI.text("系统设置中的授权对象：\n\(status?.applicationName ?? "检测中…")", x: 24, y: 463,
                                    width: 352, height: 44, size: 11, color: MenuBarTheme.secondary))
        addSubview(PermissionUI.text(error ?? "授权状态会自动更新。如屏幕录制授权后系统要求重新打开应用，请重新打开 QuietBridge 后再检测。",
            x: 24, y: 519, width: 352, height: 56, size: 11,
            color: error == nil ? MenuBarTheme.secondary : MenuBarTheme.warning))
        let browser = PermissionUI.card(x: 24, y: 599, width: 352, height: 72)
        browser.fill = MenuBarTheme.window; browser.bordered = false
        browser.addSubview(PermissionUI.text("浏览器权限", x: 16, y: 12, width: 320, height: 20, size: 12, weight: .medium))
        browser.addSubview(PermissionUI.text("标签、站点和调试权限由浏览器扩展单独申请。", x: 16, y: 37,
                                            width: 320, height: 24, size: 11, color: MenuBarTheme.secondary))
        addSubview(browser)
        let check = PermissionUI.button(busy ? "正在检测…" : "重新检测", x: 24, y: 695,
                                       target: self, action: #selector(check))
        check.isEnabled = !busy; addSubview(check)
    }
    @objc private func goBack() { back?() }
    @objc private func check() { recheck?() }
    @objc private func openGuide(_ sender: NSButton) {
        if let key = sender.identifier?.rawValue, let permission = DesktopPermission(rawValue: key) { authorize?(permission) }
    }
}
