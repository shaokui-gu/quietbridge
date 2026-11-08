// Diagnostic lock route: post the session lock shortcut as a synthetic event.
// The privacy guardian only treats hardware input (eventSourceUnixProcessID ==
// 0) as a human takeover, so a synthetic shortcut keeps the protection lease
// alive long enough to exercise the unlock path end to end.
import CoreGraphics
import Foundation

enum TaskPrivacySyntheticLock {
    static func request() throws -> String {
        guard let source = CGEventSource(stateID: .hidSystemState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: 12, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 12, keyDown: false) else {
            throw NativeFailure(code: "SYNTHETIC_LOCK_FAILED", message: "无法创建锁屏快捷键事件")
        }
        for event in [down, up] {
            event.flags = [.maskControl, .maskCommand]
            event.setIntegerValueField(.eventSourceUserData, value: TaskPrivacySyntheticEvents.marker)
        }
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
        return "ctrl-command-q"
    }
}
