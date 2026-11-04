// Opt-in screen-unlock integration for the separately installed Authorization
// Plug-in. The plug-in is still the authorization decision point; this side
// only publishes a short-lived grant after the kernel identifies a valid caller.
import CoreGraphics
import Darwin
import Foundation
import Security
import SystemConfiguration

private let lockedUsePluginIdentifier = "com.workbuddy.cua.AuthorizationPlugin"
private let lockedUseTeamIdentifier = "62RNP54C47"
private let lockedUsePluginPath =
    "/Library/Security/SecurityAgentPlugins/WorkBuddyCUAAuthorizationPlugin.bundle"
private let lockedUseConfigurationPath =
    "/Library/Application Support/WorkBuddyCUAAuthorizationPlugin/installation.plist"
private let lockedUseSocketDirectory = "/tmp/com.workbuddy.cua.\(getuid())"
private let lockedUseSocketPath = lockedUseSocketDirectory + "/LoginAuthorization.sock"

final class LockedUseAuthorizationState: @unchecked Sendable {
    private let lock = NSLock()
    private var taskSessionID: UInt32?
    private var taskActive = false
    private var protectionUntil: TimeInterval = 0
    private var grantUntil: TimeInterval = 0
    private var automaticUnlockPending = false
    private var manualLockObserved = false
    private var suppressUntilManualUnlock = false
    private var currentLockHandled = false
    private var lastGrantReason: String?
    private var consumedCount = 0
    private var rejectedCount = 0

    func beginTask(sessionID: UInt32?) {
        lock.withLock {
            taskSessionID = sessionID
            taskActive = sessionID != nil
            protectionUntil = 0
            currentLockHandled = false
            grantUntil = 0
            lastGrantReason = nil
        }
    }

    func endTask() {
        lock.withLock {
            taskActive = false
            taskSessionID = nil
            protectionUntil = 0
            currentLockHandled = false
            grantUntil = 0
            lastGrantReason = nil
        }
    }

    func renewProtection(ready: Bool, validUntil: TimeInterval? = nil) {
        lock.withLock {
            protectionUntil = ready ? min(ProcessInfo.processInfo.systemUptime + 1,
                                          validUntil ?? .greatestFiniteMagnitude) : 0
            if !ready { grantUntil = 0 }
        }
    }

    /// Called when the user deliberately asks the workbuddy overlay to hand
    /// control back through the macOS login UI.
    func suppressForManualAuthentication() {
        lock.withLock {
            if !suppressUntilManualUnlock { manualLockObserved = false }
            suppressUntilManualUnlock = true
            currentLockHandled = true
            grantUntil = 0
            lastGrantReason = "manual authentication required"
        }
    }

    /// Arms one short-lived decision for an externally caused lock. Returns
    /// false when the current lock episode is already handled or suppressed.
    func armExternalLock(seconds: TimeInterval, sessionID: UInt32) -> Bool {
        lock.withLock {
            guard taskActive, taskSessionID == sessionID, sessionID != 0, protectionUntil > ProcessInfo.processInfo.systemUptime, seconds.isFinite, seconds > 0,
                  !suppressUntilManualUnlock, !currentLockHandled else { return false }
            currentLockHandled = true
            grantUntil = ProcessInfo.processInfo.systemUptime + min(seconds, 12)
            lastGrantReason = "active interface task"
            return true
        }
    }

    var manualAuthenticationRequired: Bool { lock.withLock { suppressUntilManualUnlock } }

    func noteSessionLocked() {
        lock.withLock { if suppressUntilManualUnlock { manualLockObserved = true } }
    }

    func noteSessionUnlocked() {
        lock.withLock {
            if suppressUntilManualUnlock && automaticUnlockPending {
                // An already-consumed Allow cannot be recalled from authd.
                // Its unlock is not evidence of manual authentication.
                automaticUnlockPending = false
                manualLockObserved = false
                return
            }
            guard !suppressUntilManualUnlock || manualLockObserved else { return }
            automaticUnlockPending = false
            manualLockObserved = false
            currentLockHandled = false
            grantUntil = 0
            suppressUntilManualUnlock = false
            lastGrantReason = nil
        }
    }

    func consumeGrant(sessionID: UInt32) -> Bool {
        lock.withLock {
            let now = ProcessInfo.processInfo.systemUptime
            guard taskActive, taskSessionID == sessionID, sessionID != 0, protectionUntil > now, !suppressUntilManualUnlock, grantUntil > now else {
                rejectedCount += 1
                grantUntil = 0
                return false
            }
            grantUntil = 0
            automaticUnlockPending = true
            consumedCount += 1
            return true
        }
    }

    var status: [String: Any] {
        lock.withLock {
            [
                "boundSessionID": taskSessionID.map { Int($0) } as Any,
                "protectionReady": protectionUntil > ProcessInfo.processInfo.systemUptime,
                "taskActive": taskActive,
                "manualSuppression": suppressUntilManualUnlock,
                "currentLockHandled": currentLockHandled,
                "grantRemainingMilliseconds": max(
                    0, (grantUntil - ProcessInfo.processInfo.systemUptime) * 1000),
                "lastGrantReason": lastGrantReason as Any,
                "consumedCount": consumedCount,
                "rejectedCount": rejectedCount,
            ]
        }
    }
}

enum LockedUsePeerScope {
    static func allows(effectiveUID: UInt32, auditUID: UInt32, auditSession: UInt32,
                       consoleUID: UInt32, consoleSession: UInt32, securityAgentUID: UInt32?) -> Bool {
        // System XPC hosts can have an unset audit user (AU_DEFAUDITID).
        // The effective UID and signed host are still kernel-verifiable.
        guard auditUID == consoleUID || auditUID == UInt32.max else { return false }
        if effectiveUID == consoleUID { return true }
        // Root is admitted only in the console audit session. validateClient also
        // restricts root peers to Apple-signed privileged authorization hosts.
        return (effectiveUID == 0 || securityAgentUID == effectiveUID) && auditSession == consoleSession
    }
}

private enum LockedUseSocketError: Error, CustomStringConvertible {
    case setup(String)
    case invalidPeer(String)

    var description: String {
        switch self {
        case .setup(let message), .invalidPeer(let message): return message
        }
    }
}

/// Private socket. The peer is Apple's authorization host, not the loaded bundle.
/// Host validation does not attest which plug-in is loaded; installation stays gated.
final class LockedUseAuthorizationServer: @unchecked Sendable {
    static let shared = LockedUseAuthorizationServer()

    private let queue = DispatchQueue(label: "com.workbuddy.cua.login-authorization")
    private let state = LockedUseAuthorizationState()
    private var ownershipDescriptor: Int32 = -1
    private var listener: Int32 = -1
    private var source: DispatchSourceRead?
    private var sessionMonitor: DispatchSourceTimer?
    private let lifecycleLock = NSLock()
    private var started = false
    private var lastClientError: String?
    private var lastPeerSession: UInt32?
    private var clientCount = 0

    var isAvailable: Bool { availabilityReason() == "ok" }

    /// Same checks as `isAvailable`, but reports the first failing one so a
    /// denied unlock can be attributed instead of guessed at.
    func availabilityReason() -> String {
        var record = stat()
        guard lstat(lockedUseConfigurationPath, &record) == 0 else {
            return "config-missing(errno \(errno))"
        }
        guard (record.st_mode & S_IFMT) == S_IFREG else { return "config-not-file" }
        guard record.st_uid == 0 else { return "config-owner=\(record.st_uid)" }
        guard record.st_mode & 0o022 == 0 else { return "config-mode=\(String(record.st_mode & 0o777, radix: 8))" }
        guard FileManager.default.fileExists(atPath: lockedUsePluginPath) else { return "plugin-missing" }
        guard let data = FileManager.default.contents(atPath: lockedUseConfigurationPath),
              let values = try? PropertyListSerialization.propertyList(
                from: data, options: [], format: nil) as? [String: Any] else {
            return "config-unreadable"
        }
        var bundle: SecStaticCode?
        var requirement: SecRequirement?
        let expression = "anchor apple generic and identifier \"\(lockedUsePluginIdentifier)\" and certificate leaf[subject.OU] = \"\(lockedUseTeamIdentifier)\""
        let created = SecStaticCodeCreateWithPath(URL(fileURLWithPath: lockedUsePluginPath) as CFURL, [], &bundle)
        guard created == errSecSuccess, let bundle else { return "code-create=\(created)" }
        let requirementStatus = SecRequirementCreateWithString(expression as CFString, [], &requirement)
        guard requirementStatus == errSecSuccess, let requirement else {
            return "requirement=\(requirementStatus)"
        }
        let validity = SecStaticCodeCheckValidity(bundle, [], requirement)
        guard validity == errSecSuccess else { return "signature=\(validity)" }
        let protocolVersion = values["protocolVersion"] as? Int
        let optIn = values["experimentalOptIn"] as? Bool
        let enabled = values["enabled"] as? Bool
        let bundleID = values["bundleIdentifier"] as? String
        let team = values["teamIdentifier"] as? String
        guard protocolVersion == 3 else { return "protocol=\(protocolVersion.map(String.init) ?? "nil")" }
        guard optIn == true else { return "optIn=\(optIn.map(String.init) ?? "nil")" }
        guard enabled == true else { return "enabled=\(enabled.map(String.init) ?? "nil")" }
        guard bundleID == lockedUsePluginIdentifier else { return "bundleID=\(bundleID ?? "nil")" }
        guard team == lockedUseTeamIdentifier else { return "team=\(team ?? "nil")" }
        return "ok"
    }

    func start() {
        queue.sync { [self] in
            guard !lifecycleLock.withLock({ started }) else { return }
            do {
                try prepareDirectory()
                try listen()
                lifecycleLock.withLock { started = true }
                let monitor = DispatchSource.makeTimerSource(queue: queue)
                monitor.schedule(deadline: .now(), repeating: .milliseconds(350))
                monitor.setEventHandler { [weak self] in
                    guard let self else { return }
                    switch NativePlatform.sessionLocked() {
                    case true: self.state.noteSessionLocked()
                    case false: self.state.noteSessionUnlocked()
                    case nil: break
                    }
                }
                monitor.resume()
                sessionMonitor = monitor
            } catch {
                if listener >= 0 { Darwin.close(listener); listener = -1 }
                if ownershipDescriptor >= 0 { Darwin.close(ownershipDescriptor); ownershipDescriptor = -1 }
                fputs("[locked-use] authorization socket unavailable: \(error)\n", stderr)
            }
        }
    }

    func renewProtection(ready: Bool, validUntil: TimeInterval? = nil) {
        state.renewProtection(ready: ready, validUntil: validUntil)
    }
    func beginTask() { state.beginTask(sessionID: verifiedSessionID()) }
    func endTask() { state.endTask() }
    func suppressForManualAuthentication() { state.suppressForManualAuthentication() }
    func noteSessionLocked() { state.noteSessionLocked() }
    func noteSessionUnlocked() { state.noteSessionUnlocked() }
    var manualAuthenticationRequired: Bool { state.manualAuthenticationRequired }
    func requireAutomationAllowed() throws {
        guard !state.manualAuthenticationRequired else {
            throw NativeFailure(code: "MANUAL_AUTHENTICATION_REQUIRED",
                                message: "用户已接管；请在系统锁屏中手动认证后重新读取状态")
        }
    }

    @discardableResult
    func armExternalLock(seconds: TimeInterval = 12) -> Bool {
        guard isAvailable, lifecycleLock.withLock({ started }),
              let sessionID = verifiedSessionID() else { return false }
        return state.armExternalLock(seconds: seconds, sessionID: sessionID)
    }

    var status: JSONValue {
        var values = state.status
        values["available"] = isAvailable
        lifecycleLock.withLock {
            values["socketStarted"] = started
            if let lastClientError { values["lastClientError"] = lastClientError }
            if let lastPeerSession { values["lastPeerSession"] = Int(lastPeerSession) }
            values["clientCount"] = clientCount
        }
        return JSONValue.object(values.reduce(into: [:]) { result, entry in
            switch entry.value {
            case let value as Bool: result[entry.key] = .bool(value)
            case let value as Int: result[entry.key] = .number(Double(value))
            case let value as Double: result[entry.key] = .number(value)
            case let value as String: result[entry.key] = .string(value)
            default: result[entry.key] = .null
            }
        })
    }

    private func prepareDirectory() throws {
        var info = stat()
        if lstat(lockedUseSocketDirectory, &info) == 0 {
            guard (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == getuid() else {
                throw LockedUseSocketError.setup("授权 socket 目录归属或类型不安全")
            }
            guard chmod(lockedUseSocketDirectory, 0o711) == 0 else {
                throw LockedUseSocketError.setup("无法保护授权 socket 目录")
            }
        } else {
            guard mkdir(lockedUseSocketDirectory, 0o711) == 0 else {
                throw LockedUseSocketError.setup("无法创建授权 socket 目录")
            }
        }
        // Hold exclusive ownership before unlinking a stale endpoint. A second
        // helper must never replace a socket served by the live helper.
        guard chmod(lockedUseSocketDirectory, 0o711) == 0 else {
            throw LockedUseSocketError.setup("无法设置授权目录访问权限")
        }
        let ownerPath = lockedUseSocketDirectory + "/owner.lock"
        ownershipDescriptor = open(ownerPath, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard ownershipDescriptor >= 0,
              fstat(ownershipDescriptor, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(),
              info.st_nlink == 1, flock(ownershipDescriptor, LOCK_EX | LOCK_NB) == 0 else {
            throw LockedUseSocketError.setup("授权 socket 已被占用或所有权文件不安全")
        }
        if lstat(lockedUseSocketPath, &info) == 0 {
            guard info.st_uid == getuid(), (info.st_mode & S_IFMT) == S_IFSOCK else {
                throw LockedUseSocketError.setup("已有授权 socket 路径不安全")
            }
            guard unlink(lockedUseSocketPath) == 0 else {
                throw LockedUseSocketError.setup("无法清理旧授权 socket")
            }
        }
    }

    private func listen() throws {
        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listener >= 0 else {
            throw LockedUseSocketError.setup("socket() errno \(errno)")
        }
        _ = fcntl(listener, F_SETFD, FD_CLOEXEC)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let path = lockedUseSocketPath.utf8CString
        guard path.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw LockedUseSocketError.setup("授权 socket 路径过长")
        }
        withUnsafeMutableBytes(of: &address.sun_path) { destination in
            path.withUnsafeBytes { source in
                destination.copyBytes(from: source.prefix(destination.count))
            }
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(listener, $0, size)
            }
        }
        guard bound == 0, chmod(lockedUseSocketPath, 0o666) == 0,
              Darwin.listen(listener, 8) == 0 else {
            throw LockedUseSocketError.setup("无法监听授权 socket，errno \(errno)")
        }
        _ = fcntl(listener, F_SETFL, O_NONBLOCK)
        let source = DispatchSource.makeReadSource(fileDescriptor: listener, queue: queue)
        source.setEventHandler { [weak self] in self?.acceptPending() }
        source.setCancelHandler { [listener] in Darwin.close(listener) }
        source.resume()
        self.source = source
    }

    private func acceptPending() {
        while listener >= 0 {
            let client = accept(listener, nil, nil)
            if client < 0 {
                if errno == EINTR { continue }
                if errno == EAGAIN || errno == EWOULDBLOCK { return }
                fputs("[locked-use] accept errno \(errno)\n", stderr)
                return
            }
            _ = fcntl(client, F_SETFD, FD_CLOEXEC)
            // Darwin accept inherits O_NONBLOCK from the listening socket.
            // The bounded request reader below uses SO_RCVTIMEO and must wait
            // for the peer to finish its signature check before sending data.
            let flags = fcntl(client, F_GETFL)
            guard flags >= 0, fcntl(client, F_SETFL, flags & ~O_NONBLOCK) == 0 else {
                Darwin.close(client)
                continue
            }
            handle(client)
        }
    }

    private func handle(_ descriptor: Int32) {
        defer { Darwin.close(descriptor) }
        do {
            let peerSession = try validateClient(descriptor)
            lifecycleLock.withLock {
                lastClientError = nil
                lastPeerSession = peerSession
                clientCount += 1
            }
            var noSignal: Int32 = 1
            setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            var timeout = timeval(tv_sec: 1, tv_usec: 0)
            setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))

            var bytes = [UInt8]()
            let deadline = ProcessInfo.processInfo.systemUptime + 1
            while bytes.count < 128 && ProcessInfo.processInfo.systemUptime < deadline {
                var byte: UInt8 = 0
                let count = Darwin.read(descriptor, &byte, 1)
                if count < 0 && errno == EINTR { continue }
                guard count == 1 else { throw LockedUseSocketError.invalidPeer("授权请求不完整") }
                bytes.append(byte)
                if byte == 10 { break }
            }
            let fields = String(decoding: bytes, as: UTF8.self).split(separator: " ", omittingEmptySubsequences: false)
            guard fields.count == 5, fields[0] == "AUTHORIZE", fields[1] == "3",
                  UInt32(fields[2]) == getuid(),
                  let session = UInt32(fields[3]), peerSession == session, let targetSession = verifiedSessionID(),
                  fields[4].last == "\n" else {
                throw LockedUseSocketError.invalidPeer("授权请求用户或会话不匹配")
            }
            let nonce = String(fields[4].dropLast())
            guard nonce.count == 32, nonce.utf8.allSatisfy({
                (48...57).contains($0) || (97...102).contains($0)
            }) else { throw LockedUseSocketError.invalidPeer("授权请求 nonce 无效") }
            // authd can invoke the plug-in before the main-thread lock poll.
            // Arm only after all caller/console checks and an actual locked
            // session observation; the state still enforces one grant per episode.
            var allowed = false
            let availability = availabilityReason()
            let lockedNow = NativePlatform.sessionLocked()
            var armed = false
            if availability == "ok", lockedNow == true {
                armed = state.armExternalLock(seconds: 12, sessionID: targetSession)
                allowed = state.consumeGrant(sessionID: targetSession)
            }
            let reply = (allowed ? "ALLOW 3 " : "DENY 3 ") + nonce + "\n"
            Trace.log("authz session=\(targetSession) available=\(availability)"
                      + " locked=\(String(describing: lockedNow)) armed=\(armed)"
                      + " allowed=\(allowed) state=\(state.status)")
            _ = reply.withCString { Darwin.write(descriptor, $0, strlen($0)) }

        } catch {
            lifecycleLock.withLock { lastClientError = String(describing: error) }
            Trace.log("authz rejected: \(error)")
            fputs("[locked-use] rejected authorization client: \(error)\n", stderr)
            _ = "DENY\n".withCString { Darwin.write(descriptor, $0, strlen($0)) }
        }
    }

    private func verifiedSessionID() -> UInt32? {
        var consoleUID: uid_t = 0
        var consoleGID: gid_t = 0
        guard SCDynamicStoreCopyConsoleUser(nil, &consoleUID, &consoleGID) != nil,
              consoleUID == getuid() else { return nil }
        var identifier: SecuritySessionId = 0
        var attributes = SessionAttributeBits(rawValue: 0)
        guard SessionGetInfo(callerSecuritySession, &identifier, &attributes) == errSecSuccess,
              identifier != noSecuritySession,
              attributes.contains(.sessionHasGraphicAccess), !attributes.contains(.sessionIsRemote) else { return nil }
        return identifier
    }

    private func validateClient(_ descriptor: Int32) throws -> UInt32 {
        var token = audit_token_t()
        var tokenLength = socklen_t(MemoryLayout<audit_token_t>.size)
        guard getsockopt(descriptor, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &tokenLength) == 0,
              tokenLength == MemoryLayout<audit_token_t>.size else {
            throw LockedUseSocketError.invalidPeer("无法读取授权客户端 audit token (errno \(errno), length \(tokenLength))")
        }
        var account = passwd()
        var accountResult: UnsafeMutablePointer<passwd>?
        var accountBuffer = [CChar](repeating: 0, count: 16384)
        let accountStatus = getpwnam_r("_securityagent", &account, &accountBuffer,
                                       accountBuffer.count, &accountResult)
        let agentUID: UInt32? = accountStatus == 0 && accountResult != nil ? account.pw_uid : nil
        guard let graphicalSession = verifiedSessionID(),
              LockedUsePeerScope.allows(effectiveUID: token.val.1, auditUID: token.val.0,
                  auditSession: token.val.6, consoleUID: getuid(), consoleSession: graphicalSession,
                  securityAgentUID: agentUID) else {
            throw LockedUseSocketError.invalidPeer("授权客户端身份与控制台不匹配 (euid \(token.val.1), auid \(token.val.0), session \(token.val.6))")
        }
        let attributes = [
            kSecGuestAttributeAudit as String:
                Data(bytes: &token, count: MemoryLayout<audit_token_t>.size),
        ] as CFDictionary
        var code: SecCode?
        let guestStatus = SecCodeCopyGuestWithAttributes(nil, attributes, [], &code)
        guard guestStatus == errSecSuccess, let code else {
            throw LockedUseSocketError.invalidPeer("无法验证授权客户端签名 (status \(guestStatus))")
        }
        let expression: String
        if token.val.1 == 0 {
            expression = "anchor apple and (identifier \"com.apple.authorizationhost\" or identifier \"com.apple.authorizationhosthelper.arm64\")"
        } else {
            expression = "anchor apple and (identifier \"com.apple.SecurityAgent\" or identifier \"com.apple.authorizationhost\" or identifier \"com.apple.SecurityAgentHelper.arm64\" or identifier \"com.apple.authorizationhosthelper.arm64\")"
        }
        var requirement: SecRequirement?
        guard SecRequirementCreateWithString(expression as CFString, [], &requirement) == errSecSuccess,
              let requirement,
              SecCodeCheckValidity(code, [], requirement) == errSecSuccess else {
            throw LockedUseSocketError.invalidPeer("授权客户端不是有效的 Apple 授权宿主")
        }
        return token.val.6 // kernel audit session ID; never trust the request alone
    }
}
