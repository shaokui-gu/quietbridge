// Declares local user activity exactly like Codex's lock-screen path does.
// If loginwindow reacts by evaluating system.login.screensaver, the
// authorization plug-in gets its chance to allow the unlock.
import Foundation
import IOKit.pwr_mgt

var assertionID: IOPMAssertionID = 0
let status = IOPMAssertionDeclareUserActivity(
    "workbuddy-cua unlock probe" as CFString,
    kIOPMUserActiveLocal,
    &assertionID)
print("IOPMAssertionDeclareUserActivity status=\(status) assertion=\(assertionID)")
if assertionID != 0 {
    Thread.sleep(forTimeInterval: 3)
    IOPMAssertionRelease(assertionID)
    print("released")
}
