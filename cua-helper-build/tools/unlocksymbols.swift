// Probes private frameworks for an unlock entry point. Read-only: nothing is
// called, names are only resolved.
import Darwin
import Foundation

let frameworks = [
    "/System/Library/PrivateFrameworks/login.framework/login",
    "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
    "/System/Library/PrivateFrameworks/login.framework/Versions/A/login",
]

let candidates = [
    "SACLockScreenImmediate", "SACUnlockScreen", "SACUnlock", "SACLockScreen",
    "SACScreenSaverStart", "SACScreenSaverStop", "SACScreenSaverLaunchPreferences",
    "SACShieldWindowShowing", "SACDisableScreenSaver",
    "CGSUnlockSession", "CGSSessionUnlock", "CGSUnlockScreen", "CGSExitToLoginWindow",
    "SLSUnlockSession", "SLSUnlockScreen", "SLSScreenLock", "SLSLockSession",
    "SLSForceLogout", "CGSForceLogout",
]

for path in frameworks {
    guard let handle = dlopen(path, RTLD_LAZY) else {
        print("\(path): \(String(cString: dlerror()))")
        continue
    }
    print("\(path): loaded")
    for name in candidates where dlsym(handle, name) != nil {
        print("  FOUND \(name)")
    }
    dlclose(handle)
}
