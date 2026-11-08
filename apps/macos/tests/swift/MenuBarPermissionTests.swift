import AppKit
import Foundation
import Testing
@testable import QuietBridgeNative

struct MenuBarPermissionTests {
    @Test func unknownPartialAndRevokedPermissionsCloseBothUIAndAPI() {
        let control = CapabilityControl()
        for key in ["native_observe", "native_input", "preview", "locked_use"] {
            #expect(control.snapshot()[key] == false)
            #expect(control.preferences()[key] == false)
        }
        #expect(control.snapshot()["browser"] == true)
        #expect(control.snapshot()["app_launch"] == true)
        control.updatePermissions(.init(accessibility: false, screenRecording: true))
        #expect(control.snapshot()["preview"] == true)
        #expect(control.snapshot()["native_input"] == false)
        control.updatePermissions(.init(accessibility: true, screenRecording: true))
        #expect(control.snapshot()["native_input"] == true)
        #expect(control.snapshot()["locked_use"] == false)
        _ = control.set("native_input", false)
        control.updatePermissions(nil)
        #expect(control.snapshot()["preview"] == false)
        control.updatePermissions(.init(accessibility: true, screenRecording: true))
        #expect(control.snapshot()["native_input"] == false)
        #expect(control.snapshot()["native_observe"] == true)
        _ = control.set("master", false)
        #expect(control.snapshot().values.allSatisfy { !$0 })
        #expect(control.preferences()["native_observe"] == true)
    }

    @Test func missingSwitchFindsItsRequiredStepAndStatusUsesActualApplication() throws {
        let status = try DesktopPermissionStatus(.object([
            "accessibility": .bool(false), "screenRecording": .bool(false),
            "launchApplication": .object(["name": .string("Actual Host.app")]),
        ]))
        #expect(status.applicationName == "Actual Host")
        #expect(status.missing(for: "native_input").first == .accessibility)
        #expect(status.missing(for: "preview").first == .screenRecording)
        #expect(status.missing(for: "browser").isEmpty)
        let partial = DesktopPermissionStatus(accessibility: true, screenRecording: false)
        #expect(partial.missing(for: "native_input").first == .screenRecording)
        #expect(throws: NativeFailure.self) { try DesktopPermissionStatus(.object(["accessibility": .bool(true)])) }
    }

    @Test func connectionKeepsOneProcessAndRejectsRepliesFromAnotherRequest() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let owner = try ApplicationRuntimeOwner(directory: root.appendingPathComponent("runtime").path)
        defer { owner.stop() }
        let helper = root.appendingPathComponent("fake-helper")
        // The fixture is isolated: no desktop helper, TCC request or settings URL.
        let script = """
        #!/bin/sh
        exec /usr/bin/awk '{
            count++; id = $0;
            sub(/.*"id"[ ]*:[ ]*"/, "", id); sub(/".*/, "", id);
            if (count == 3) id = "wrong-id";
            printf "{\\"id\\":\\"%s\\",\\"result\\":{\\"accessibility\\":%s,\\"screenRecording\\":false}}\\n", id, (count == 2 ? "true" : "false");
            fflush();
        }'
        """
        try Data(script.utf8).write(to: helper)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: helper.path)
        let connection = MenuBarPermissionConnection(executable: helper, session: owner.session)
        let first = try DesktopPermissionStatus(try await connection.request())
        let second = try DesktopPermissionStatus(try await connection.request())
        #expect(!first.accessibility)
        #expect(second.accessibility) // A newly spawned fixture would return false.
        do {
            _ = try await connection.request()
            Issue.record("An unrelated permission reply was accepted")
        } catch let failure as NativeFailure { #expect(failure.code == "PERMISSION_HELPER_UNAVAILABLE") }
        await connection.stop()
        owner.stop()
        do {
            _ = try await connection.request()
            Issue.record("A permission request survived application exit")
        } catch let failure as NativeFailure { #expect(failure.code == "APP_NOT_RUNNING") }
    }

    @MainActor @Test func firstRunCanBeDismissedAndDoesNotRepeat() throws {
        let name = "quietbridge-test-" + UUID().uuidString
        let defaults = try #require(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        #expect(PermissionOnboarding.consumeFirstLaunch(in: defaults))
        #expect(!PermissionOnboarding.consumeFirstLaunch(in: defaults))
    }

    @MainActor @Test func wizardRequiresRealGrantsAndOnlyRequestsFromAButton() throws {
        _ = NSApplication.shared
        let view = MenuBarPermissionWizardView()
        var requests: [DesktopPermission] = []
        var advances = 0, rechecks = 0
        view.authorize = { requests.append($0) }
        view.advance = { advances += 1 }
        view.recheck = { rechecks += 1 }
        view.refresh(step: .welcome, status: nil, busy: false, error: nil)
        #expect(view.nextButton.isEnabled)
        for permission in DesktopPermission.allCases {
            view.refresh(step: .init(permission), status: .init(accessibility: false, screenRecording: false), busy: false, error: nil)
            #expect(view.nextButton.isEnabled == false)
            #expect(requests.isEmpty)
            #expect(view.authorizeButton.nextKeyView?.nextKeyView === view.nextButton)
            view.nextButton.performClick(nil)
            #expect(advances == 0)
        }
        view.authorizeButton.performClick(nil)
        #expect(requests == [.screenRecording])
        let check = try #require(view.authorizeButton.nextKeyView as? NSButton)
        check.performClick(nil)
        #expect(rechecks == 1)
        view.refresh(step: .screenRecording, status: .init(accessibility: false, screenRecording: true), busy: false, error: nil)
        #expect(view.nextButton.isEnabled)
        view.nextButton.performClick(nil)
        #expect(advances == 1)
        view.refresh(step: .accessibility, status: .init(accessibility: false, screenRecording: true), busy: false, error: nil)
        #expect(!view.nextButton.isEnabled)
        view.refresh(step: .accessibility, status: .init(accessibility: true, screenRecording: true), busy: true, error: nil)
        #expect(!view.nextButton.isEnabled)
    }

    @MainActor @Test func renderWizardAndPermissionPaneInBothAppearances() throws {
        _ = NSApplication.shared
        let wizard = MenuBarPermissionWizardView()
        let pane = MenuBarPermissionsView(frame: NSRect(x: 0, y: 0, width: 400, height: 750))
        for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            for step in PermissionWizardStep.allCases {
                let granted = step == .complete
                wizard.refresh(step: step, status: .init(accessibility: granted, screenRecording: granted), busy: false, error: nil)
                try render(wizard, appearance: appearance, name: "wizard-\(step.rawValue)-\(name)")
            }
            wizard.refresh(step: .accessibility, status: .init(accessibility: true, screenRecording: false), busy: false, error: nil)
            try render(wizard, appearance: appearance, name: "wizard-granted-\(name)")
            wizard.refresh(step: .complete, status: .init(accessibility: false, screenRecording: true), busy: false, error: nil)
            try render(wizard, appearance: appearance, name: "wizard-partial-\(name)")
            let longName = try DesktopPermissionStatus(.object([
                "accessibility": .bool(false), "screenRecording": .bool(false),
                "launchApplication": .object(["name": .string("QuietBridge Extended Application Name for Permission Settings.app")]),
            ]))
            wizard.refresh(step: .accessibility, status: longName, busy: false,
                           error: "暂时无法连接权限检测服务，请重新检测；如果仍然失败，请重新打开 QuietBridge。")
            try render(wizard, appearance: appearance, name: "wizard-long-name-\(name)")
            pane.refresh(status: .init(accessibility: false, screenRecording: false), busy: false, error: nil)
            try render(pane, appearance: appearance, name: "permissions-\(name)")
        }
    }

    @MainActor private func render(_ view: NSView, appearance: NSAppearance.Name, name: String) throws {
        let window = NSWindow(contentRect: view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false; window.appearance = NSAppearance(named: appearance)
        window.contentView = view
        defer { window.close() }
        view.layoutSubtreeIfNeeded()
        for child in view.subviews { #expect(view.bounds.contains(child.frame)) }
        let bitmap = try #require(view.bitmapImageRepForCachingDisplay(in: view.bounds))
        view.cacheDisplay(in: view.bounds, to: bitmap)
        let png = try #require(bitmap.representation(using: .png, properties: [:]))
        if let directory = ProcessInfo.processInfo.environment["QUIETBRIDGE_PANEL_SNAPSHOTS"] {
            let destination = URL(fileURLWithPath: directory)
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
            try png.write(to: destination.appendingPathComponent(name + ".png"))
        }
    }
}
