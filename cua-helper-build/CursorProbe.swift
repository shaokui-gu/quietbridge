import AppKit
import CoreGraphics

@main struct CursorProbe {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in
            let pointerBefore = CGEvent(source: nil)!.location
            let frontBefore = NSWorkspace.shared.frontmostApplication?.processIdentifier
            AgentCursor.shared.move(to: CGPoint(x: 500, y: 400))
            try? await Task.sleep(for: .milliseconds(200))
            AgentCursor.shared.move(to: CGPoint(x: 1000, y: 650), pulse: true)
            for _ in 0..<45 {
                try? await Task.sleep(for: .milliseconds(100))
                let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
                let overlay = windows.first { ($0[kCGWindowOwnerPID as String] as? Int32) == getpid() && ($0[kCGWindowLayer as String] as? Int ?? 0) > 0 }
                if let overlay { print("FRAME \(ProcessInfo.processInfo.systemUptime) \(overlay[kCGWindowNumber as String] ?? 0) \(overlay[kCGWindowBounds as String] ?? [:])") }
            }
            try? await Task.sleep(for: .seconds(27))
            let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
            let visible = windows.contains { ($0[kCGWindowOwnerPID as String] as? Int32) == getpid() && ($0[kCGWindowLayer as String] as? Int ?? 0) > 0 }
            print("IDLE_HIDDEN \(!visible)")
            print("POINTER_UNCHANGED \(CGEvent(source: nil)!.location == pointerBefore)")
            print("FOREGROUND_UNCHANGED \(NSWorkspace.shared.frontmostApplication?.processIdentifier == frontBefore)")
            app.terminate(nil)
        }
        app.run()
    }
}
