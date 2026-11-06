// Resident status item. Its switches live only in this process and are read
// again by each new bridge call; a failed call reads them once more.
import AppKit
import Darwin
import Foundation
import Network

@MainActor final class MenuBarPanel: NSObject, NSPopoverDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private var popover: NSPopover?
    private var rows: [String: NSButton] = [:]
    private var listener: NWListener?

    func start() {
        item.button?.image = NSImage(systemSymbolName: "rectangle.dashed.badge.record", accessibilityDescription: "workbuddy-cua")
        item.button?.target = self
        item.button?.action = #selector(toggle)
        build()
        startStatusServer()
    }

    private func build() {
        let labels = [
            ("master", "总开关"), ("native_observe", "原生观察"), ("native_input", "原生屏幕操作"),
            ("app_launch", "启动应用"), ("browser", "浏览器通道"), ("preview", "预览面板"),
            ("operation_log", "操作日志"), ("locked_use", "锁屏授权插件"),
        ]
        let view = NSStackView()
        view.orientation = .vertical
        view.alignment = .leading
        view.spacing = 8
        view.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        let snapshot = CapabilityControl.shared.snapshot()
        for (key, title) in labels {
            let button = NSButton(checkboxWithTitle: title, target: self, action: #selector(changed(_:)))
            button.identifier = NSUserInterfaceItemIdentifier(key)
            button.state = snapshot[key] == true ? .on : .off
            rows[key] = button
            view.addArrangedSubview(button)
        }
        let quit = NSButton(title: "退出菜单栏", target: self, action: #selector(quitMenuBar))
        view.addArrangedSubview(quit)
        let note = NSTextField(wrappingLabelWithString: "只影响下一次新调用。失败后会重新读取；已开始的调用和租约保持不变。")
        note.preferredMaxLayoutWidth = 250
        note.textColor = .secondaryLabelColor
        view.addArrangedSubview(note)
        view.layoutSubtreeIfNeeded()
        let fitting = view.fittingSize
        view.frame = NSRect(x: 0, y: 0, width: max(fitting.width, 280), height: max(fitting.height, 120))
        let controller = NSViewController()
        controller.view = view
        let popover = NSPopover()
        popover.contentViewController = controller
        // Transient gives the arrow plus click-outside dismissal for free.
        popover.behavior = .transient
        popover.animates = false
        popover.delegate = self
        self.popover = popover
        refreshEnabled()
    }

    @objc private func toggle() {
        guard let popover, let button = item.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    private func pluginInstalled() -> Bool {
        let bundle = "/Library/Security/SecurityAgentPlugins/WorkBuddyCUAAuthorizationPlugin.bundle"
        let receipt = "/Library/Application Support/WorkBuddyCUAAuthorizationPlugin/installation.plist"
        return FileManager.default.fileExists(atPath: bundle) && FileManager.default.fileExists(atPath: receipt)
    }

    private func refreshPluginState() {
        guard let button = rows["locked_use"] else { return }
        let installed = pluginInstalled()
        button.title = installed ? "锁屏授权插件（已安装）" : "锁屏授权插件（未安装）"
        if !installed {
            // Installation is a separate, administrator-authorized step; the
            // panel only reports it and never starts that step on its own.
            write("locked_use", false)
            button.state = .off
        }
        button.isEnabled = installed && CapabilityControl.shared.snapshot()["master"] == true
    }

    private func write(_ key: String, _ value: Bool) {
        _ = CapabilityControl.shared.set(key, value)
    }

    @objc private func changed(_ sender: NSButton) {
        guard let key = sender.identifier?.rawValue else { return }
        if key == "locked_use", sender.state == .on, !pluginInstalled() {
            sender.state = .off
            showPluginMissing()
            return
        }
        _ = CapabilityControl.shared.set(key, sender.state == .on)
        refreshEnabled()
    }

    private func showPluginMissing() {
        let alert = NSAlert()
        alert.messageText = "锁屏授权插件未安装"
        alert.informativeText = "该能力需要先单独安装系统授权插件。安装由你以管理员身份完成，控制面板不会代为安装或修改系统授权设置。"
        alert.addButton(withTitle: "好")
        alert.runModal()
    }

    @objc private func quitMenuBar() { NSApp.terminate(nil) }

    private func refreshEnabled() {
        let enabled = CapabilityControl.shared.snapshot()["master"] == true
        for (key, button) in rows where key != "master" { button.isEnabled = enabled }
        refreshPluginState()
    }

    private func startStatusServer() {
        guard let port = NWEndpoint.Port(rawValue: 47_631) else { return }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: port)
        // No SO_REUSEPORT: a second instance must fail here instead of leaving a
        // duplicate status item with no reachable state behind it.
        parameters.allowLocalEndpointReuse = false
        guard let listener = try? NWListener(using: parameters) else {
            FileHandle.standardError.write(Data("workbuddy-cua: another menu bar instance owns the state port\n".utf8))
            exit(0)
        }
        listener.stateUpdateHandler = { state in
            if case .failed = state { exit(0) }
        }
        listener.newConnectionHandler = { connection in
            connection.start(queue: .global(qos: .utility))
            let snapshot = CapabilityControl.shared.snapshot()
            let body = (try? JSONSerialization.data(withJSONObject: snapshot)) ?? Data("{}".utf8)
            let response = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: \(body.count)\r\n\r\n".utf8) + body
            connection.send(content: response, completion: .contentProcessed { _ in connection.cancel() })
        }
        listener.start(queue: .global(qos: .utility))
        self.listener = listener
    }
}
