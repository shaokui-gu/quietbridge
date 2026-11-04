// Locks the session through login.framework's SACLockScreenImmediate, which is
// the "lock now" path that always requires authentication (unlike display sleep,
// which honours the user's password-delay setting).
import Darwin
import Foundation

typealias LockImmediate = @convention(c) () -> Void

let path = "/System/Library/PrivateFrameworks/login.framework/login"
guard let handle = dlopen(path, RTLD_LAZY) else {
    print("dlopen failed: \(String(cString: dlerror()))")
    exit(1)
}
guard let symbol = dlsym(handle, "SACLockScreenImmediate") else {
    print("symbol missing")
    exit(1)
}
let lock = unsafeBitCast(symbol, to: LockImmediate.self)
lock()
print("SACLockScreenImmediate called")
