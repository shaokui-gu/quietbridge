import AppKit
import Testing
@testable import CuaNative

@MainActor private final class PanelActionTarget: NSObject {
    var lastKey: String?
    @objc func changed(_ sender: NSButton) { lastKey = sender.identifier?.rawValue }
    @objc func install() {}
    @objc func quit() {}
}

@MainActor struct MenuBarPanelTests {
    @Test func templateAssetsRetainTransparentBackgrounds() throws {
        for name in ["status-enabled", "status-paused", "status-attention", "eye", "cursor", "app", "browser", "preview", "log", "lock", "quit"] {
            let image = MenuBarAssets.image(name)
            #expect(image.isTemplate)
            let bitmap = try #require(image.representations.first as? NSBitmapImageRep)
            #expect(bitmap.pixelsWide >= (name == "quit" ? 36 : 54))
            #expect(bitmap.colorAt(x: 0, y: 0)?.alphaComponent == 0)
        }
    }

    private func makeView(_ target: PanelActionTarget) -> MenuBarPanelView {
        _ = NSApplication.shared
        return MenuBarPanelView(target: target, changed: #selector(PanelActionTarget.changed(_:)),
                                install: #selector(PanelActionTarget.install), quit: #selector(PanelActionTarget.quit))
    }

    @Test func controlsRetainPreferencesAndOnlyExposeAvailablePlugin() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        var switches = CapabilitySwitches()
        _ = switches.set("operation_log", false)
        view.refresh(preferences: switches.preferences, pluginAvailable: false)
        #expect(view.rows["locked_use"]?.isHidden == true)
        #expect(view.installButton.isHiddenOrHasHiddenAncestor == false)
        let input = try #require(view.rows["native_input"])
        input.performClick(nil)
        #expect(input.state == .off)
        #expect(target.lastKey == "native_input")
        _ = switches.set("master", false)
        view.refresh(preferences: switches.preferences, pluginAvailable: false)
        #expect(input.state == .on)
        #expect(input.isEnabled == false)
        #expect(view.rows["operation_log"]?.state == .off)
        #expect(view.installButton.isEnabled == true)
        #expect(view.rows["master"]?.isEnabled == true)
        #expect(view.rows["master"]?.label.stringValue == "已暂停")
        _ = switches.set("master", true)
        view.refresh(preferences: switches.preferences, pluginAvailable: true)
        #expect(view.rows["locked_use"]?.isHidden == false)
        #expect(view.rows["locked_use"]?.isEnabled == true)
        #expect(view.installButton.isHiddenOrHasHiddenAncestor == true)
        #expect(view.rows["operation_log"]?.nextKeyView === view.browserButton)
        #expect(view.browserButton.nextKeyView === view.permissionsButton)
        #expect(view.permissionsButton.nextKeyView === view.rows["locked_use"])
        #expect(input.accessibilityLabel() == "屏幕操作")
        #expect(view.manageButton.isHidden == false)
    }

    @Test func damagedAndDisabledPluginsExposeManagement() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        #expect(MenuBarPluginState.classify(reason: "ok", hasArtifacts: true) == .available)
        #expect(MenuBarPluginState.classify(reason: "disabled", hasArtifacts: true) == .disabled)
        #expect(MenuBarPluginState.classify(reason: "config-missing(errno 2)", hasArtifacts: false) == .missing)
        #expect(MenuBarPluginState.classify(reason: "config-missing(errno 2)", hasArtifacts: true) == .needsRepair)
        #expect(MenuBarPluginState.classify(reason: "signature=-67050", hasArtifacts: true) == .needsRepair)
        for state in [MenuBarPluginState.disabled, .needsRepair] {
            view.refresh(preferences: CapabilitySwitches().preferences, pluginState: state)
            #expect(view.installButton.title == "管理…")
            #expect(view.rows["locked_use"]?.isEnabled == false)
            #expect(view.rows["locked_use"]?.isHidden == true)
        }
    }

    @Test func packagedInstallerIsFoundWithoutSourceCheckout() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let installer = root.appendingPathComponent("plugin/manage-plugin.command")
        try FileManager.default.createDirectory(at: installer.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("#!/bin/bash\n".utf8).write(to: installer)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: installer.path)
        #expect(MenuBarPluginInstaller.find(near: nil, support: root) == installer)
    }

    @Test func paneResizingKeepsPluginControlsSeparate() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        let browser = MenuBarBrowserView(frame: NSRect(x: 360, y: 0, width: 400, height: 720))
        let container = MenuBarPanelContainer(content: view, browser: browser)
        let window = NSPanel(contentRect: container.bounds, styleMask: [.borderless, .nonactivatingPanel],
                             backing: .buffered, defer: false)
        window.contentView = container
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .aqua)
        defer { window.close() }
        view.refresh(preferences: CapabilitySwitches().preferences, pluginAvailable: true)
        let plugin = try #require(view.rows["locked_use"])
        let card = try #require(plugin.superview)
        for (name, expanded, narrow, size) in [
            ("expanded", true, false, NSSize(width: 760, height: 720)),
            ("collapsed", false, false, NSSize(width: 360, height: 720)),
            ("expanded-again", true, false, NSSize(width: 760, height: 720)),
            ("narrow", true, true, NSSize(width: 400, height: 540)),
            ("short-collapsed", false, false, NSSize(width: 360, height: 540)),
        ] {
            container.setBrowserExpanded(expanded, narrow: narrow)
            window.setContentSize(size)
            container.layoutSubtreeIfNeeded()
            #expect(view.frame.size == MenuBarPanelView.panelSize)
            #expect(card.bounds.width == 320)
            #expect(view.browserButton.bounds.width == 320)
            #expect(plugin.frame.maxX + 10 <= view.manageButton.frame.minX)
            #expect(card.bounds.contains(view.manageButton.frame))
            #expect(view.isHidden == (expanded && narrow))
            #expect(browser.isHidden == !expanded)
            #expect(browser.frame.minX == (expanded && narrow ? 0 : 360))
            if !narrow && size.height == 720 {
                let managePoint = NSPoint(x: view.manageButton.frame.midX, y: view.manageButton.frame.midY)
                #expect(card.hitTest(card.convert(managePoint, to: card.superview)) === view.manageButton)
            }
            try saveSnapshot(container, name: name)
        }
    }

    @Test func permissionDetailReplacesBrowserWithinTheSamePanel() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        let browser = NSView(), permissions = MenuBarPermissionsView(frame: .zero)
        let container = MenuBarPanelContainer(content: view, browser: browser, permissions: permissions)
        container.frame.size = NSSize(width: 760, height: 750)
        let control = CapabilityControl()
        view.refresh(preferences: control.preferences(), pluginState: .available, permissions: nil)
        #expect(view.rows["native_input"]?.state == .off)
        #expect(view.rows["native_input"]?.isEnabled == true)
        #expect(view.rows["preview"]?.state == .off)
        #expect(view.permissionsButton.badge.stringValue == "待授权")
        container.setDetail(.browser, narrow: false)
        container.layoutSubtreeIfNeeded()
        #expect(view.browserButton.selected)
        #expect(permissions.isHidden)
        container.setDetail(.permissions, narrow: false)
        container.layoutSubtreeIfNeeded()
        #expect(!view.browserButton.selected)
        #expect(view.permissionsButton.selected)
        #expect(browser.isHidden)
        #expect(!permissions.isHidden)
        #expect(permissions.frame == NSRect(x: 360, y: 0, width: 400, height: 750))
        container.setDetail(.permissions, narrow: true)
        container.layoutSubtreeIfNeeded()
        #expect(view.isHidden)
        #expect(permissions.frame.minX == 0)
        container.setDetail(nil, narrow: false)
        #expect(!view.isHidden)
        #expect(!view.permissionsButton.selected)
        #expect(permissions.isHidden)
    }

    @Test func browserSetupUsesPackagedFilesAndStaysInPanel() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        #expect(MenuBarExtensionSetup.directory(home: root, near: nil) == nil)
        let directory = root.appendingPathComponent("Library/Application Support/workbuddy-cua/extension/extension-stage")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: directory.appendingPathComponent("manifest.json"))
        #expect(MenuBarExtensionSetup.directory(home: root, near: nil)?.path == directory.path)
        let target = PanelActionTarget()
        let view = makeView(target)
        let browser = MenuBarBrowserView(frame: NSRect(x: 360, y: 0, width: 400, height: 720))
        let container = MenuBarPanelContainer(content: view, browser: browser)
        let window = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 760, height: 720),
                             styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.contentView = container
        window.appearance = NSAppearance(named: .aqua)
        window.isReleasedWhenClosed = false
        defer { window.close() }
        container.setBrowserExpanded(true, narrow: false)
        container.layoutSubtreeIfNeeded()
        #expect(browser.showingSetup == false)
        browser.setupButton.performClick(nil)
        #expect(browser.showingSetup)
        #expect(browser.setupButton.title == "返回任务")
        #expect(browser.bounds.contains(browser.setupButton.frame))
        func descendants(_ node: NSView) -> [NSView] {
            node.subviews.flatMap { [$0] + descendants($0) }
        }
        let controls = descendants(browser).compactMap { $0 as? NSButton }
        #expect(controls.contains { $0.title == "打开扩展管理" })
        #expect(controls.contains { $0.title == "复制路径" })
        #expect(controls.contains { $0.title == "显示文件夹" })
        try saveSnapshot(container, name: "extension-setup")
        browser.setupButton.performClick(nil)
        #expect(browser.showingSetup == false)
        #expect(browser.setupButton.title == "安装扩展…")
    }

    @Test func hoverFeedbackWorksWithoutKeyboardFocus() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        let window = NSPanel(contentRect: view.bounds, styleMask: [.borderless, .nonactivatingPanel],
                             backing: .buffered, defer: false)
        window.contentView = view
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .aqua)
        defer { window.close() }
        view.layoutSubtreeIfNeeded()
        #expect(!window.isKeyWindow)
        let row = try #require(view.rows["native_input"])
        let plugin = try #require(view.rows["locked_use"]?.superview)
        view.updateTrackingAreas()
        #expect(view.trackingAreas.contains { $0.options.contains([.activeAlways, .mouseMoved]) })
        // Include the unavailable plug-in state: its management action still
        // needs hover feedback even when the capability switch is hidden.
        for (name, control) in [("row-hover", row as NSView), ("browser-hover", view.browserButton), ("plugin-hover", plugin)] {
            let before = try snapshot(view)
            try movePointer(over: control, in: view)
            #expect(try snapshot(view) != before)
            try saveSnapshot(view, name: name)
            view.refreshHover()
            #expect(try snapshot(view) == before)
        }
    }

    @Test func highlightReachesBothEdgesAndOnlyFollowsCurrentRow() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        let window = NSPanel(contentRect: view.bounds, styleMask: [.borderless, .nonactivatingPanel],
                             backing: .buffered, defer: false)
        window.contentView = view
        window.isReleasedWhenClosed = false
        defer { window.close() }
        let keys = ["native_observe", "native_input", "app_launch", "browser", "preview", "operation_log"]
        let rows = try keys.map { try #require(view.rows[$0]) }
        for (appearanceName, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            window.appearance = NSAppearance(named: appearance)
            view.refreshHover()
            view.layoutSubtreeIfNeeded()
            let baseline = try bitmap(view)
            for (index, row) in rows.enumerated() {
                #expect(row.bounds.width == 320)
                // Move along the outer edges as well as across section boundaries.
                try movePointer(over: row, in: view, x: index.isMultiple(of: 2) ? 2 : row.bounds.width - 2)
                let hovered = try bitmap(view)
                for candidate in rows {
                    let frame = view.convert(candidate.bounds, from: candidate)
                    for x in [frame.minX + 2, frame.maxX - 2] {
                        let point = NSPoint(x: x, y: frame.midY)
                        let before = try pixel(in: baseline, at: point, bounds: view.bounds)
                        let after = try pixel(in: hovered, at: point, bounds: view.bounds)
                        #expect((before != after) == (candidate === row))
                    }
                }
                try saveSnapshot(view, name: "edge-hover-" + appearanceName + "-" + keys[index])
            }
            view.refreshHover()
            let cleared = try bitmap(view)
            for row in rows {
                let frame = view.convert(row.bounds, from: row)
                let point = NSPoint(x: frame.minX + 2, y: frame.midY)
                #expect(try pixel(in: cleared, at: point, bounds: view.bounds) == pixel(in: baseline, at: point, bounds: view.bounds))
            }
        }
    }

    private func movePointer(over control: NSView, in panel: MenuBarPanelView, x: CGFloat? = nil) throws {
        let location = control.convert(NSPoint(x: x ?? control.bounds.midX, y: control.bounds.midY), to: nil)
        let event = try #require(NSEvent.mouseEvent(with: .mouseMoved, location: location,
            modifierFlags: [], timestamp: 0, windowNumber: panel.window?.windowNumber ?? 0,
            context: nil, eventNumber: 0, clickCount: 0, pressure: 0))
        panel.mouseMoved(with: event)
    }

    private func bitmap(_ view: NSView) throws -> NSBitmapImageRep {
        let bitmap = try #require(view.bitmapImageRepForCachingDisplay(in: view.bounds))
        view.cacheDisplay(in: view.bounds, to: bitmap)
        return bitmap
    }

    private func pixel(in bitmap: NSBitmapImageRep, at point: NSPoint, bounds: NSRect) throws -> NSColor {
        let x = Int((point.x - bounds.minX) / bounds.width * CGFloat(bitmap.pixelsWide))
        let y = Int((point.y - bounds.minY) / bounds.height * CGFloat(bitmap.pixelsHigh))
        return try #require(bitmap.colorAt(x: x, y: y))
    }

    private func snapshot(_ view: NSView) throws -> Data {
        let bitmap = try bitmap(view)
        return try #require(bitmap.representation(using: .png, properties: [:]))
    }

    private func saveSnapshot(_ view: NSView, name: String) throws {
        guard let directory = ProcessInfo.processInfo.environment["CUA_PANEL_SNAPSHOTS"] else { return }
        let destination = URL(fileURLWithPath: directory, isDirectory: true)
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        try snapshot(view).write(to: destination.appendingPathComponent(name + ".png"))
    }

    @Test func renderAllAppearances() throws {
        let target = PanelActionTarget()
        let view = makeView(target)
        let window = NSWindow(contentRect: view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        window.contentView = view
        window.isReleasedWhenClosed = false
        defer { window.close() }
        for (name, appearance, paused, installed) in [
            ("light", NSAppearance.Name.aqua, false, false),
            ("dark", .darkAqua, false, false),
            ("paused-light", .aqua, true, false),
            ("paused-dark", .darkAqua, true, false),
            ("installed", .aqua, false, true),
        ] {
            window.appearance = NSAppearance(named: appearance)
            var switches = CapabilitySwitches()
            _ = switches.set("operation_log", false)
            _ = switches.set("master", !paused)
            view.refresh(preferences: switches.preferences, pluginAvailable: installed)
            view.layoutSubtreeIfNeeded()
            let row = try #require(view.rows["native_input"])
            #expect(row.frame.height == 42)
            let rect = view.convert(row.bounds, from: row)
            #expect(view.bounds.contains(rect))
            #expect(rect.width > 280)
            let bitmap = try #require(NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 720, pixelsHigh: 1308,
                                                       bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                                       isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
            bitmap.size = view.bounds.size
            view.cacheDisplay(in: view.bounds, to: bitmap)
            let png = try #require(bitmap.representation(using: .png, properties: [:]))
            #expect(png.count > 1000)
            if let directory = ProcessInfo.processInfo.environment["CUA_PANEL_SNAPSHOTS"] {
                let destination = URL(fileURLWithPath: directory, isDirectory: true)
                try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
                try png.write(to: destination.appendingPathComponent(name + ".png"))
            }
        }
    }
}
