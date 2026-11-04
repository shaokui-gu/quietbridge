// Posts one synthetic Return key at three levels so a lock-screen run can tell
// which (if any) reaches loginwindow. Prints what it attempted.
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

func post(source: CGEventSource?, tap: CGEventTapLocation, keyCode: CGKeyCode, label: String) {
    guard let down = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: false) else {
        print("\(label): event creation failed")
        return
    }
    down.flags = []
    up.flags = []
    // Marker consumed by the privacy guardian so it does not read this as a
    // human takeover.
    down.setIntegerValueField(.eventSourceUserData, value: 0x57424355414c4f43)
    up.setIntegerValueField(.eventSourceUserData, value: 0x57424355414c4f43)
    down.post(tap: tap)
    up.post(tap: tap)
    print("\(label): posted")
}

let hidSource = CGEventSource(stateID: .hidSystemState)
let combinedSource = CGEventSource(stateID: .combinedSessionState)

post(source: hidSource, tap: .cghidEventTap, keyCode: 36, label: "hid-state/cghid")
post(source: combinedSource, tap: .cghidEventTap, keyCode: 36, label: "combined/cghid")
post(source: hidSource, tap: .cgSessionEventTap, keyCode: 36, label: "hid-state/cgsession")

if let loginwindow = NSRunningApplication.runningApplications(
    withBundleIdentifier: "com.apple.loginwindow").first {
    let pid = loginwindow.processIdentifier
    if let down = CGEvent(keyboardEventSource: hidSource, virtualKey: 36, keyDown: true),
       let up = CGEvent(keyboardEventSource: hidSource, virtualKey: 36, keyDown: false) {
        down.postToPid(pid)
        up.postToPid(pid)
        print("postToPid loginwindow(\(pid)): posted")
    }
} else {
    print("loginwindow not found")
}
