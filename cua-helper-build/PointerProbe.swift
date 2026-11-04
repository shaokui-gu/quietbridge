import AppKit
import CoreGraphics

@main struct PointerProbe {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in
            let control = ProcessInfo.processInfo.environment["CONTROL"] == "1"
            let pointerBefore = CGEvent(source: nil)!.location
            let frontBefore = NSWorkspace.shared.frontmostApplication?.localizedName ?? "?"
            if !control {
                AgentCursor.shared.move(to: CGPoint(x: 400, y: 300))
                try? await Task.sleep(for: .milliseconds(250))
                AgentCursor.shared.move(to: CGPoint(x: 760, y: 520), pulse: true)
            }
            try? await Task.sleep(for: .milliseconds(850))
            let pointerAfter = CGEvent(source: nil)!.location
            let frontAfter = NSWorkspace.shared.frontmostApplication?.localizedName ?? "?"
            print("POINTER_BEFORE \(Int(pointerBefore.x)),\(Int(pointerBefore.y))")
            print("POINTER_AFTER  \(Int(pointerAfter.x)),\(Int(pointerAfter.y))")
            print("POINTER_UNCHANGED \(pointerBefore == pointerAfter)")
            print("FRONT_BEFORE \(frontBefore)")
            print("FRONT_AFTER  \(frontAfter)")
            print("FOREGROUND_UNCHANGED \(frontBefore == frontAfter)")
            app.terminate(nil)
        }
        app.run()
    }
}
