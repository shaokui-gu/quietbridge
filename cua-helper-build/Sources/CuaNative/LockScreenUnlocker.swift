// Drives loginwindow's own credential UI through the accessibility API.
// Physical input is never injected: the privacy guardian treats a hardware
// keystroke as a human takeover and revokes the protection lease, so the only
// act that can keep the grant alive is an AX action performed by this process.
import AppKit
import ApplicationServices
import Darwin
import Foundation

private let unlockAXTimeout: Float = 0.35
private let unlockMaximumNodes = 1500
private let unlockMaximumDepth = 14
private let unlockSubmitDelay: useconds_t = 120_000
private let unlockVerifyDelay: useconds_t = 600_000
private let unlockRevealDelay: useconds_t = 450_000
/// Never a real secret: only present so loginwindow takes its auth path.
private let unlockProbeCredential = "cua-probe-not-a-secret"
/// Below this the lock screen has not painted yet, usually a sleeping display.
private let unlockMinimumNodes = 6
private let unlockWakeDelay: useconds_t = 500_000
private let unlockFailureKeywords = ["错误", "不正确", "失败", "重试", "incorrect", "failed", "try again", "wrong"]

struct LockScreenAXNode {
    let element: AXUIElement
    let depth: Int
    let role: String
    let subrole: String
    let title: String
    let description: String
    let valueLength: Int
    let actions: [String]
}

final class LockScreenUnlocker: @unchecked Sendable {
    static let shared = LockScreenUnlocker()
    /// One grant per locked episode is the authorization rule; the attempt cap
    /// only bounds how many times the UI is poked before giving up.
    static let maximumAttemptsPerEpisode = 3

    private let stateLock = NSLock()
    private var attempts = 0

    private init() {}

    // MARK: - Diagnostics

    /// Read-only projection of loginwindow's accessibility tree. Values are
    /// reported by length only; credential content is never exposed.
    func inspect() throws -> JSONValue {
        guard AXIsProcessTrusted() else {
            throw NativeFailure(code: "PERMISSION_DENIED", message: "需要辅助功能权限")
        }
        let (pid, element) = try loginWindowElement()
        let nodes = walk(element)
        let secureFields = nodes.filter { $0.subrole == "AXSecureTextField" }.count
        let buttons = nodes.filter { $0.role == "AXButton" }.count
        let probes = probeShortcuts(app: element)
        dump(nodes, probes: probes)
        return .object([
            "loginWindowPid": .number(Double(pid)),
            "sessionLocked": .bool(NativePlatform.sessionLocked() == true),
            "nodeCount": .number(Double(nodes.count)),
            "secureFieldCount": .number(Double(secureFields)),
            "buttonCount": .number(Double(buttons)),
            "probes": .array(probes.map(JSONValue.string)),
            "nodes": .array(nodes.prefix(140).map(describe)),
        ])
    }

    /// Targeted lookups that do not depend on walking the whole tree: the
    /// system-wide focused element and the element under a screen point.
    private func probeShortcuts(app: AXUIElement) -> [String] {
        var probes: [String] = []
        probes.append("loginwindow children=\(children(app)?.count ?? -1)"
                      + " windows=\(attributeElements(app, kAXWindowsAttribute).count)")
        let systemWide = AXUIElementCreateSystemWide()
        _ = AXUIElementSetMessagingTimeout(systemWide, unlockAXTimeout)
        var focused: CFTypeRef?
        if AXUIElementCopyAttributeValue(systemWide, kAXFocusedUIElementAttribute as CFString,
                                         &focused) == .success, let focused,
           CFGetTypeID(focused) == AXUIElementGetTypeID() {
            let element = unsafeDowncast(focused, to: AXUIElement.self)
            probes.append("focused role=\(string(element, kAXRoleAttribute))"
                          + " subrole=\(string(element, kAXSubroleAttribute))"
                          + " pid=\(pidOf(element))")
            let subtree = walk(element)
            probes.append("focused subtree=\(subtree.count) secure=\(subtree.filter { $0.subrole == "AXSecureTextField" }.count)")
        } else {
            probes.append("focused unavailable")
        }
        if let screen = NSScreen.main {
            let frame = screen.frame
            let points: [(String, CGFloat, CGFloat)] = [
                ("center", frame.midX, frame.midY),
                ("center+120", frame.midX, frame.midY + 120),
                ("center-120", frame.midX, frame.midY - 120),
            ]
            for (label, x, y) in points {
                var hit: AXUIElement?
                let error = AXUIElementCopyElementAtPosition(systemWide, Float(x), Float(y), &hit)
                if error == .success, let hit {
                    probes.append("point \(label) role=\(string(hit, kAXRoleAttribute))"
                                  + " subrole=\(string(hit, kAXSubroleAttribute))"
                                  + " pid=\(pidOf(hit))")
                } else {
                    probes.append("point \(label) axError=\(error.rawValue)")
                }
            }
        }
        return probes
    }

    private func pidOf(_ element: AXUIElement) -> pid_t {
        var pid: pid_t = 0
        _ = AXUIElementGetPid(element, &pid)
        return pid
    }

    /// Full tree, one node per line. Used to discover how a given macOS release
    /// exposes the lock-screen credential UI.
    private func dump(_ nodes: [LockScreenAXNode], probes: [String]) {
        var text = probes.map { "probe " + $0 }.joined(separator: "\n")
        text += "\n--- \(nodes.count) nodes ---\n"
        for node in nodes {
            text += String(repeating: "  ", count: node.depth)
            text += "\(node.role)|\(node.subrole)|t=\(node.title)|d=\(node.description)"
            text += "|v=\(node.valueLength)|a=\(node.actions.joined(separator: ","))\n"
        }
        try? text.write(toFile: "/tmp/cua-ax-dump.txt", atomically: true, encoding: .utf8)
    }

    // MARK: - Unlock

    /// One bounded unlock attempt: focus the credential field, clear its value
    /// and submit. Returns a step log so a failure is attributable.
    func unlock() throws -> JSONValue {
        guard AXIsProcessTrusted() else {
            throw NativeFailure(code: "PERMISSION_DENIED", message: "需要辅助功能权限")
        }
        let locked = NativePlatform.sessionLocked()
        noteSessionState(locked)
        guard locked == true else {
            throw NativeFailure(code: "SESSION_NOT_LOCKED", message: "当前会话未锁定，未执行任何动作")
        }
        let (pid, application) = try loginWindowElement()
        // Stop as soon as the credential element shows up: the lock-screen tree
        // can run to hundreds of decorative nodes.
        var nodes = walk(application, stopWhen: { Self.isCredentialNode($0) })
        var found = nodes.last(where: { Self.isCredentialNode($0) })
        if found == nil {
            nodes = walk(application)
            found = nodes.first(where: { $0.role == "AXTextField" })
        }
        // A sleeping display collapses the tree to a stub: loginwindow only
        // publishes the credential UI once the panel is awake. Declare user
        // activity (which does not authenticate anything) and rescan.
        var wake: String?
        if nodes.count < unlockMinimumNodes, NativePlatform.sessionLocked() == true {
            var assertion: IOPMAssertionID = 0
            let status = IOPMAssertionDeclareUserActivity(
                "CUAAutoUnlock" as CFString, kIOPMUserActiveLocal, &assertion)
            usleep(unlockWakeDelay)
            wake = "status=\(status) assertion=\(assertion)"
            nodes = walk(application, stopWhen: { Self.isCredentialNode($0) })
            found = nodes.last(where: { Self.isCredentialNode($0) })
            if found == nil {
                nodes = walk(application)
                found = nodes.first(where: { $0.role == "AXTextField" })
            }
        }
        var steps: [JSONValue] = [
            .object(["step": .string("scan"), "pid": .number(Double(pid)),
                     "nodes": .number(Double(nodes.count))]),
        ]
        if let wake {
            steps.append(.object(["step": .string("wakeDisplay"), "result": .string(wake),
                                  "nodes": .number(Double(nodes.count))]))
        }

        // The lock screen first shows the user tile with a "show all users"
        // button; the credential field only exists after that is pressed.
        if found == nil, let reveal = Self.revealButton(in: nodes) {
            let press = AXUIElementPerformAction(reveal.element, kAXPressAction as CFString)
            steps.append(.object(["step": .string("reveal"),
                                  "title": .string(reveal.title),
                                  "axError": .number(Double(press.rawValue))]))
            usleep(unlockRevealDelay)
            nodes = walk(application, stopWhen: { Self.isCredentialNode($0) })
            found = nodes.last(where: { Self.isCredentialNode($0) })
            if found == nil {
                nodes = walk(application)
                found = nodes.first(where: { $0.role == "AXTextField" })
            }
            steps.append(.object(["step": .string("rescan"),
                                  "nodes": .number(Double(nodes.count))]))
        }

        // The revealed list shows one button per account; the credential field
        // only appears after the console user's own tile is selected.
        if found == nil, let row = Self.userRow(in: nodes) {
            let press = AXUIElementPerformAction(row.element, kAXPressAction as CFString)
            steps.append(.object(["step": .string("selectUser"),
                                  "description": .string(row.description),
                                  "axError": .number(Double(press.rawValue))]))
            usleep(unlockRevealDelay)
            nodes = walk(application, stopWhen: { Self.isCredentialNode($0) })
            found = nodes.last(where: { Self.isCredentialNode($0) })
            if found == nil {
                nodes = walk(application)
                found = nodes.first(where: { $0.role == "AXTextField" })
            }
            steps.append(.object(["step": .string("rescanUser"),
                                  "nodes": .number(Double(nodes.count))]))
        }

        // The credential UI is built lazily; an empty tree is not a failed
        // attempt and must not consume the episode's submission budget.
        guard let field = found else {
            // Persist the post-reveal tree so the next iteration can target the
            // exact element instead of guessing again.
            dump(nodes, probes: ["unlock-failed", "step-count=\(steps.count)"])
            return finish(steps: steps, verdict: "no-credential-field", unlocked: false)
        }
        guard let attempt = claimAttempt() else {
            throw NativeFailure(code: "UNLOCK_ATTEMPTS_EXHAUSTED",
                                message: "本段锁屏的自动解锁尝试已用尽")
        }
        steps.append(.object(["step": .string("claim"), "attempt": .number(Double(attempt))]))

        var error = AXUIElementSetAttributeValue(field.element, kAXFocusedAttribute as CFString,
                                                 kCFBooleanTrue)
        steps.append(.object(["step": .string("focus"), "axError": .number(Double(error.rawValue))]))
        if error != .success {
            _ = AXUIElementPerformAction(field.element, kAXPressAction as CFString)
        }

        // A non-empty probe value forces loginwindow down its real
        // authentication path. The credential itself is irrelevant: the
        // authorization plug-in answers before any password is checked.
        error = AXUIElementSetAttributeValue(field.element, kAXValueAttribute as CFString,
                                             unlockProbeCredential as CFString)
        steps.append(.object(["step": .string("setValue"), "axError": .number(Double(error.rawValue))]))

        usleep(unlockSubmitDelay)
        let confirm = AXUIElementPerformAction(field.element, kAXConfirmAction as CFString)
        steps.append(.object(["step": .string("confirm"), "axError": .number(Double(confirm.rawValue))]))

        // AXConfirm is a no-op on this field (it reports success but the value
        // survives), and the arrow button only exists once the field holds
        // text, so the tree has to be rescanned before it can be pressed.
        let withValue = walk(application)
        if let button = submitButton(in: withValue) {
            let press = AXUIElementPerformAction(button.element, kAXPressAction as CFString)
            steps.append(.object(["step": .string("pressSubmit"),
                                  "label": .string(button.title + "|" + button.description),
                                  "axError": .number(Double(press.rawValue))]))
        } else {
            steps.append(.object(["step": .string("pressSubmit"),
                                  "label": .string("not-found"),
                                  "nodes": .number(Double(withValue.count))]))
        }

        usleep(unlockVerifyDelay)
        // A rejected credential clears the field and paints a hint. Seeing one
        // proves loginwindow ran a local check instead of consulting the
        // authorization plug-in, which is the difference we need to observe.
        let after = walk(application)
        let hint = after.first { node in
            let haystack = (node.title + " " + node.description + " " + node.subrole).lowercased()
            return unlockFailureKeywords.contains { haystack.contains($0) }
        }
        let fieldAfter = after.first { $0.role == "AXTextField" || $0.subrole == "AXSecureTextField" }
        steps.append(.object([
            "step": .string("submitResult"),
            "nodes": .number(Double(after.count)),
            "fieldValueLength": .number(Double(fieldAfter?.valueLength ?? -1)),
            "hint": .string(hint.map { "\($0.role)|\($0.title)|\($0.description)" } ?? ""),
        ]))
        let unlocked = NativePlatform.sessionLocked() == false
        if unlocked {
            noteSessionState(false)
        } else {
            dump(after, probes: ["post-confirm"])
        }
        return finish(steps: steps, verdict: unlocked ? "unlocked" : "still-locked", unlocked: unlocked)
    }

    // MARK: - Session bookkeeping

    private func noteSessionState(_ locked: Bool?) {
        stateLock.lock()
        defer { stateLock.unlock() }
        if locked == false { attempts = 0 }
    }

    private func claimAttempt() -> Int? {
        stateLock.lock()
        defer { stateLock.unlock() }
        guard attempts < Self.maximumAttemptsPerEpisode else { return nil }
        attempts += 1
        return attempts
    }

    // MARK: - Target resolution

    private func loginWindowElement() throws -> (pid_t, AXUIElement) {
        guard let pid = loginWindowPID() else {
            throw NativeFailure(code: "LOGINWINDOW_UNAVAILABLE", message: "找不到 loginwindow 进程")
        }
        let element = AXUIElementCreateApplication(pid)
        _ = AXUIElementSetMessagingTimeout(element, unlockAXTimeout)
        var value: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &value)
        guard error == .success else {
            throw NativeFailure(code: "LOGINWINDOW_UNREACHABLE",
                                message: "无法读取 loginwindow 辅助功能树 (AXError \(error.rawValue))")
        }
        return (pid, element)
    }

    private func loginWindowPID() -> pid_t? {
        if let app = NSRunningApplication.runningApplications(
            withBundleIdentifier: "com.apple.loginwindow").first, app.processIdentifier > 0 {
            return app.processIdentifier
        }
        for app in NSWorkspace.shared.runningApplications {
            if app.bundleIdentifier == "com.apple.loginwindow" ||
                app.executableURL?.lastPathComponent == "loginwindow" {
                return app.processIdentifier
            }
        }
        return nil
    }

    // MARK: - Tree projection

    private func walk(_ root: AXUIElement, limit: Int = unlockMaximumNodes,
                      stopWhen: ((LockScreenAXNode) -> Bool)? = nil) -> [LockScreenAXNode] {
        var queue: [(AXUIElement, Int)] = [(root, 0)]
        var nodes: [LockScreenAXNode] = []
        var index = 0
        while index < queue.count && nodes.count < limit {
            let (element, depth) = queue[index]
            index += 1
            _ = AXUIElementSetMessagingTimeout(element, unlockAXTimeout)
            let node = LockScreenAXNode(
                element: element,
                depth: depth,
                role: string(element, kAXRoleAttribute),
                subrole: string(element, kAXSubroleAttribute),
                title: bounded(string(element, kAXTitleAttribute)),
                description: bounded(string(element, kAXDescriptionAttribute)),
                valueLength: valueLength(element),
                actions: actionNames(element))
            nodes.append(node)
            if let stopWhen, stopWhen(node) { break }
            guard depth < unlockMaximumDepth else { continue }
            var next = children(element) ?? []
            // Some system apps publish windows only through AXWindows, leaving
            // AXChildren empty on the application element.
            if next.isEmpty, depth == 0 {
                next = attributeElements(element, kAXWindowsAttribute)
                if next.isEmpty { next = attributeElements(element, kAXFocusedWindowAttribute) }
            }
            for child in next { queue.append((child, depth + 1)) }
        }
        return nodes
    }

    private func attributeElements(_ element: AXUIElement, _ attribute: String) -> [AXUIElement] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let value else { return [] }
        if let list = value as? [AXUIElement] { return list }
        if CFGetTypeID(value) == AXUIElementGetTypeID() {
            return [unsafeDowncast(value, to: AXUIElement.self)]
        }
        return []
    }

    private func children(_ element: AXUIElement) -> [AXUIElement]? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success,
              let list = value as? [AXUIElement] else { return nil }
        return list
    }

    private func actionNames(_ element: AXUIElement) -> [String] {
        var value: CFArray?
        guard AXUIElementCopyActionNames(element, &value) == .success,
              let names = value as? [String] else { return [] }
        return names.map { bounded($0) }
    }

    private func string(_ element: AXUIElement, _ attribute: String) -> String {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let text = value as? String else { return "" }
        return text
    }

    private func valueLength(_ element: AXUIElement) -> Int {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value) == .success else {
            return -1
        }
        if let text = value as? String { return text.count }
        return -1
    }

    private func bounded(_ text: String) -> String {
        String(text.prefix(48))
    }

    // MARK: - Selection

    private static func isCredentialNode(_ node: LockScreenAXNode) -> Bool {
        if node.subrole == "AXSecureTextField" { return true }
        let keywords = ["密码", "password", "passwort", "パスワード", "contraseña", "mot de passe"]
        let haystack = (node.title + " " + node.description).lowercased()
        if node.role == "AXTextField" || node.role == "AXTextArea",
           keywords.contains(where: { haystack.contains($0) }) { return true }
        if node.role == "AXTextField", node.actions.contains("AXConfirm") { return true }
        return false
    }

    /// Picks the lock screen tile that belongs to the console user. Matching is
    /// name-only so power buttons can never be pressed by accident.
    private static func userRow(in nodes: [LockScreenAXNode]) -> LockScreenAXNode? {
        let names = [NSUserName(), NSFullUserName()]
            .map { $0.lowercased() }
            .filter { !$0.isEmpty }
        guard !names.isEmpty else { return nil }
        return nodes.first { node in
            guard node.role == "AXButton" else { return false }
            let haystack = (node.title + " " + node.description).lowercased()
            return names.contains { haystack.contains($0) }
        }
    }

    private static func revealButton(in nodes: [LockScreenAXNode]) -> LockScreenAXNode? {
        let keywords = ["显示所有用户", "show all users", "其他用户", "other user",
                        "切换用户", "switch user"]
        return nodes.first { node in
            guard node.role == "AXButton" else { return false }
            let haystack = (node.title + " " + node.description).lowercased()
            return keywords.contains { haystack.contains($0.lowercased()) }
        }
    }

    private func submitButton(in nodes: [LockScreenAXNode]) -> LockScreenAXNode? {
        let buttons = nodes.filter { $0.role == "AXButton" }
        guard !buttons.isEmpty else { return nil }
        // The submit arrow carries an empty title and description "login"; the
        // input-source switch is also an AXButton, so never fall back to
        // "the first button".
        let keywords = ["login", "log in", "unlock", "解锁", "登录", "submit", "确定"]
        if let labelled = buttons.first(where: { button in
            let haystack = (button.title + " " + button.description).lowercased()
            return keywords.contains { haystack.contains($0) }
        }) { return labelled }
        return buttons.first(where: { $0.subrole == "AXDefaultButton" })
    }

    // MARK: - Reporting

    private func describe(_ node: LockScreenAXNode) -> JSONValue {
        .object([
            "depth": .number(Double(node.depth)),
            "role": .string(node.role),
            "subrole": .string(node.subrole),
            "title": .string(node.title),
            "description": .string(node.description),
            "valueLength": .number(Double(node.valueLength)),
            "actions": .array(node.actions.map(JSONValue.string)),
        ])
    }

    private func finish(steps: [JSONValue], verdict: String, unlocked: Bool) -> JSONValue {
        .object([
            "verdict": .string(verdict),
            "unlocked": .bool(unlocked),
            "sessionLocked": .bool(NativePlatform.sessionLocked() == true),
            "steps": .array(steps),
        ])
    }
}
