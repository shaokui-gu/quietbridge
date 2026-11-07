// Only server mode owns AppKit; launcher and stdio clients connect to its socket.
import AppKit
import Foundation
import Darwin

@MainActor private final class MenuBarApplicationDelegate: NSObject, NSApplicationDelegate {
    let panel = MenuBarPanel()

    func applicationWillTerminate(_ notification: Notification) { panel.shutdown() }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        panel.show()
        return false
    }
}

@main enum CuaNativeMain {
    @MainActor static func main() {
        signal(SIGPIPE, SIG_IGN)
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            if arguments.first == "--privacy-guardian" {
                _ = NSApplication.shared
                if arguments.count == 3, arguments[1] == "--wallpaper-snapshots" {
                    PrivacyWallpaper.shared.useSnapshots(from: URL(fileURLWithPath: arguments[2]))
                }
                PrivacyGuardianProcess().run()
                return
            }
            if arguments.first == "--probe-focus" {
                let options = try FocusProbeOptions(arguments: arguments)
                let app = NSApplication.shared
                app.setActivationPolicy(.prohibited)
                Task { @MainActor in
                    exit(await FocusProbe(options: options).run())
                }
                app.run()
                return
            }
            if arguments.first == "--probe-stream" {
                let app = NSApplication.shared
                app.setActivationPolicy(.prohibited)
                Task { @MainActor in
                    exit(await StreamProbe.run(arguments: arguments))
                }
                app.run()
                return
            }
            if arguments.first == "--menubar" {
                runMenuBar()
                return
            }
            if arguments.first == "--ensure-shared" {
                try SharedHelperLauncher.ensure(SharedHelperOptions(arguments: arguments))
                return
            }
            if arguments.first == "--serve" {
                guard setsid() >= 0 else { throw SharedSocket.failure("Creating shared helper session") }
                let options = try SharedHelperOptions(arguments: arguments)
                let app = NSApplication.shared
                app.setActivationPolicy(.prohibited)
                guard let server = try SharedHelperServer(options: options) else { return }
                try server.start()
                withExtendedLifetime(server) { app.run() }
                return
            }
            // Finder may pass a process serial number and does not guarantee
            // the stdin state used by command-line bridge clients.
            let finderArguments = arguments.allSatisfy { $0.hasPrefix("-psn_") }
            if finderArguments && (Bundle.main.bundleIdentifier == "com.workbuddy.cua.menubar" || Self.launchedWithoutStdioChannel()) {
                runMenuBar()
                return
            }
            let configuration = try HelperConfiguration(arguments: arguments)
            try SharedHelperProxy.run(configuration: configuration)
        } catch {
            let diagnostic = (error as? NativeFailure)?.message ?? error.localizedDescription
            if CommandLine.arguments.dropFirst().first == "--serve" {
                let failure = JSONValue.object(["type": .string("error"), "code": .string((error as? NativeFailure)?.code ?? "NATIVE_STARTUP"),
                                                "message": .string(diagnostic)])
                if let data = try? JSONEncoder().encode(failure) { try? FileHandle.standardOutput.write(contentsOf: data + Data([10])) }
            }
            if let data = "workbuddy-cua-native: \(diagnostic)\n".data(using: .utf8) { try? FileHandle.standardError.write(contentsOf: data) }
            exit(1)
        }
    }

    @MainActor private static func runMenuBar() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = MenuBarApplicationDelegate()
        app.delegate = delegate
        delegate.panel.start()
        withExtendedLifetime(delegate) { app.run() }
    }

    /// The bridge talks to this process over a pipe; Finder and `open` hand it
    /// /dev/null. Both are character/other devices, so only a tty proves that a
    /// person ran it by hand and still wants the usage error.
    static func launchedWithoutStdioChannel(fd: Int32 = STDIN_FILENO) -> Bool {
        var info = stat()
        guard fstat(fd, &info) == 0 else { return false }
        guard (info.st_mode & S_IFMT) != S_IFIFO else { return false }
        return isatty(fd) == 0
    }
}
