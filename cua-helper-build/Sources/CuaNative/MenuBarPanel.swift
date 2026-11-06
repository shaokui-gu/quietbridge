// Resident status item. Preferences live in this process; bridge calls read
// the effective switches over the existing loopback status port.
import AppKit
import Darwin
import Foundation
import Network

@MainActor private final class MenuBarWindow: NSPanel {
    var dismiss: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { dismiss?() }
}

/// Keep both panes in one hierarchy so resizing the window cannot resize the
/// leading pane or reset its controls and tracking areas.
@MainActor final class MenuBarPanelContainer: NSView {
    private let scroll = NSScrollView()
    private let document = MenuBarCard()
    private let content: MenuBarPanelView
    private let browser: NSView
    private var expanded = false
    private var narrow = false
    override var isFlipped: Bool { true }

    init(content: MenuBarPanelView, browser: NSView) {
        self.content = content
        self.browser = browser
        super.init(frame: NSRect(origin: .zero, size: MenuBarPanelView.panelSize))
        wantsLayer = true
        layer?.cornerRadius = 22
        layer?.masksToBounds = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        addSubview(scroll)
        document.fill = MenuBarTheme.window
        document.radius = 22
        document.bordered = true
        scroll.documentView = document
        document.autoresizingMask = []
        content.autoresizingMask = []
        browser.autoresizingMask = []
        content.radius = 0
        content.bordered = false
        document.addSubview(content)
        document.addSubview(browser)
        setBrowserExpanded(false, narrow: false)
    }

    required init?(coder: NSCoder) { nil }

    func setBrowserExpanded(_ expanded: Bool, narrow: Bool) {
        self.expanded = expanded
        self.narrow = narrow
        content.isHidden = expanded && narrow
        browser.isHidden = !expanded
        content.browserButton.selected = expanded
        needsLayout = true
    }

    override func layout() {
        super.layout()
        scroll.hasVerticalScroller = bounds.height < MenuBarPanelView.panelSize.height
        scroll.frame = bounds
        document.frame = NSRect(x: 0, y: 0, width: bounds.width, height: MenuBarPanelView.panelSize.height)
        content.frame = NSRect(origin: .zero, size: MenuBarPanelView.panelSize)
        browser.frame = NSRect(x: expanded && narrow ? 0 : MenuBarPanelView.panelSize.width,
                               y: 0, width: 400, height: MenuBarPanelView.panelSize.height)
    }
}

@MainActor final class MenuBarPanel: NSObject {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private var panel: MenuBarWindow?
    private var content: MenuBarPanelView?
    private var browserContent: MenuBarBrowserView?
    private var container: MenuBarPanelContainer?
    private var browserExpanded = false
    private var listener: NWListener?
    private var localMonitor: Any?
    private var globalMonitor: Any?
    private var refreshTimer: Timer?
    private var pluginState = MenuBarPluginState.missing
    private var checkingPlugin = false

    func start() {
        item.button?.target = self
        item.button?.action = #selector(toggle)
        item.button?.setAccessibilityLabel("WorkBuddy 电脑控制助手")
        build()
        startStatusServer()
        refresh()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshPluginState()
                if self?.panel?.isVisible == true && self?.browserExpanded == true { self?.browserContent?.refresh() }
            }
        }
    }

    private func build() {
        let view = MenuBarPanelView(target: self, changed: #selector(changed(_:)),
                                    install: #selector(installPlugin), quit: #selector(quitMenuBar))
        view.browserButton.target = self
        view.browserButton.action = #selector(toggleBrowser)
        let browser = MenuBarBrowserView(frame: NSRect(x: 360, y: 0, width: 400, height: MenuBarPanelView.panelSize.height))
        browser.back = { [weak self] in self?.browserExpanded = false; self?.layoutPanel() }
        let panel = MenuBarWindow(contentRect: NSRect(origin: .zero, size: MenuBarPanelView.panelSize),
                                  styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        let container = MenuBarPanelContainer(content: view, browser: browser)
        panel.contentView = container
        panel.title = "WorkBuddy 电脑控制助手"
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.level = .popUpMenu
        panel.collectionBehavior = [.transient, .moveToActiveSpace, .fullScreenAuxiliary]
        panel.isReleasedWhenClosed = false
        panel.acceptsMouseMovedEvents = true
        panel.dismiss = { [weak self] in self?.closePanel() }
        content = view
        browserContent = browser
        self.container = container
        self.panel = panel
    }

    @objc private func toggle() {
        guard let panel, let button = item.button, button.window != nil else { return }
        if panel.isVisible { closePanel(); return }
        show()
    }

    func show() {
        guard let panel, let button = item.button, button.window != nil else { return }
        if panel.isVisible { panel.makeKeyAndOrderFront(nil); return }
        refresh()
        browserExpanded = false
        layoutPanel()
        panel.makeKeyAndOrderFront(nil)
        content?.refreshHover()
        panel.makeFirstResponder(content?.rows["master"])
        button.highlight(true)
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] event in
            MainActor.assumeIsolated {
                guard let self else { return }
                if NSApp.modalWindow == nil && event.window !== self.panel && event.window !== self.item.button?.window { self.closePanel() }
            }
            return event
        }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            Task { @MainActor in self?.closePanel() }
        }
    }

    @objc private func toggleBrowser() {
        browserExpanded.toggle()
        layoutPanel()
        if browserExpanded { browserContent?.refresh() }
    }

    private func layoutPanel() {
        guard let panel, let container, let button = item.button,
              let statusWindow = button.window else { return }
        let anchor = statusWindow.convertToScreen(button.convert(button.bounds, to: nil))
        let visible = (statusWindow.screen ?? NSScreen.main)?.visibleFrame ?? anchor
        let narrow = browserExpanded && visible.width < 776
        let width: CGFloat = browserExpanded ? (narrow ? 400 : 760) : 360
        let height = max(1, min(MenuBarPanelView.panelSize.height, visible.height - 16))
        let preferredX = browserExpanded && !narrow && anchor.maxX + 400 <= visible.maxX - 8
            ? anchor.maxX - 360 : anchor.maxX - width
        let x = max(visible.minX + 8, min(preferredX, visible.maxX - 8 - width))
        container.setBrowserExpanded(browserExpanded, narrow: narrow)
        panel.setContentSize(NSSize(width: width, height: height))
        container.layoutSubtreeIfNeeded()
        panel.setFrameOrigin(NSPoint(x: x, y: max(visible.minY + 8, min(anchor.minY - 8, visible.maxY - 8) - height)))
        content?.refreshHover()
    }

    private func closePanel() {
        panel?.orderOut(nil)
        content?.refreshHover()
        item.button?.highlight(false)
        if let localMonitor { NSEvent.removeMonitor(localMonitor) }
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
        localMonitor = nil
        globalMonitor = nil
    }

    @objc private func changed(_ sender: NSButton) {
        guard let key = sender.identifier?.rawValue else { return }
        if key == "locked_use", sender.state == .on {
            // Never authorize from a stale cached installation result.
            guard LockedUseAuthorizationServer.shared.isAvailable else {
                sender.state = .off
                pluginState = .needsRepair
                _ = CapabilityControl.shared.set(key, false)
                updateControls()
                return
            }
        }
        _ = CapabilityControl.shared.set(key, sender.state == .on)
        updateControls()
    }

    private func refresh() {
        updateControls()
        refreshPluginState()
    }

    private func refreshPluginState() {
        guard !checkingPlugin else { return }
        checkingPlugin = true
        // Reuse the receipt, opt-in, protocol and signature validation without
        // blocking AppKit; recheck after an external install or uninstall.
        Task { [weak self] in
            let state = await Task.detached(priority: .utility) {
                MenuBarPluginState.inspect()
            }.value
            guard let self else { return }
            self.checkingPlugin = false
            self.pluginState = state
            if state != .available { _ = CapabilityControl.shared.set("locked_use", false) }
            self.updateControls()
        }
    }

    private func updateControls() {
        let preferences = CapabilityControl.shared.preferences()
        content?.refresh(preferences: preferences, pluginState: pluginState)
        item.button?.image = MenuBarAssets.image(preferences["master"] == true ? "status-enabled" : "status-paused")
        item.button?.toolTip = preferences["master"] == true ? "WorkBuddy · 已启用" : "WorkBuddy · 已暂停"
    }

    @objc private func installPlugin() {
        let alert = NSAlert()
        alert.messageText = pluginState == .missing ? "安装锁屏授权插件？" : "管理锁屏授权插件"
        alert.informativeText = pluginState == .missing
            ? "插件将加入 macOS 登录授权流程。安装器会保存原始设置并请求管理员授权。安装成功后，还需在这里手动启用能力开关。"
            : "管理器提供查看状态、更新、启用和恢复系统原始授权并卸载的操作。需要恢复的安装会保留原始备份，阻止直接覆盖。"
        alert.addButton(withTitle: "打开管理器…")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        guard let installer = MenuBarPluginInstaller.find(near: Bundle.main.executableURL) else {
            let missing = NSAlert()
            missing.messageText = "插件管理组件缺失"
            missing.informativeText = "请重新安装最新版 WorkBuddy 安装包，以恢复插件管理器。"
            missing.runModal()
            return
        }
        closePanel()
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        NSWorkspace.shared.open([installer], withApplicationAt: URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app"), configuration: configuration) { [weak self] _, error in
            guard let error else { return }
            Task { @MainActor in
                let alert = NSAlert()
                alert.messageText = "无法打开安装器"
                alert.informativeText = error.localizedDescription
                alert.runModal()
                self?.refresh()
            }
        }
    }

    @objc private func quitMenuBar() { NSApp.terminate(nil) }

    private func startStatusServer() {
        guard let port = NWEndpoint.Port(rawValue: 47_631) else { return }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: port)
        parameters.allowLocalEndpointReuse = false
        guard let listener = try? NWListener(using: parameters) else {
            FileHandle.standardError.write(Data("workbuddy-cua: another menu bar instance owns the state port\n".utf8))
            exit(0)
        }
        listener.stateUpdateHandler = { state in if case .failed = state { exit(0) } }
        listener.newConnectionHandler = { connection in
            connection.start(queue: .global(qos: .utility))
            var snapshot: [String: Any] = CapabilityControl.shared.snapshot()
            snapshot["_service"] = [
                "version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
                "build": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development",
                "pid": ProcessInfo.processInfo.processIdentifier,
                "executable": Bundle.main.executableURL?.path ?? CommandLine.arguments[0],
            ]
            let body = (try? JSONSerialization.data(withJSONObject: snapshot)) ?? Data("{}".utf8)
            let response = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: \(body.count)\r\n\r\n".utf8) + body
            connection.send(content: response, completion: .contentProcessed { _ in connection.cancel() })
        }
        listener.start(queue: .global(qos: .utility))
        self.listener = listener
    }
}

enum MenuBarPluginInstaller {
    static func find(near executable: URL?, support: URL = URL(fileURLWithPath: "/Library/Application Support/workbuddy-cua")) -> URL? {
        let packaged = support.appendingPathComponent("plugin/manage-plugin.command")
        if FileManager.default.isExecutableFile(atPath: packaged.path) { return packaged }
        guard var directory = executable?.deletingLastPathComponent() else { return nil }
        for _ in 0..<8 {
            let candidate = directory.appendingPathComponent("cua-auth-plugin/install.command")
            if FileManager.default.isReadableFile(atPath: candidate.path) { return candidate }
            let parent = directory.deletingLastPathComponent()
            if directory == parent { break }
            directory = parent
        }
        return nil
    }
}
