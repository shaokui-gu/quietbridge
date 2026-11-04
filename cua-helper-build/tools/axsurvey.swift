// Survey every running application's accessibility subtree size. Used while the
// session is locked to find which process actually owns the lock-screen UI.
import AppKit
import ApplicationServices
import Foundation

let outputPath = "/tmp/axsurvey-\(getuid()).txt"
var lines: [String] = []
lines.append("trusted=\(AXIsProcessTrusted()) uid=\(getuid())")

func attribute(_ element: AXUIElement, _ name: String) -> String {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    guard error == .success else { return "err\(error.rawValue)" }
    if let text = value as? String { return text }
    return value == nil ? "nil" : "\(type(of: value!))"
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success,
          let list = value as? [AXUIElement] else { return [] }
    return list
}

/// Breadth-first count capped at 60 nodes; also reports the first few roles.
func survey(_ root: AXUIElement) -> (Int, [String]) {
    var queue: [(AXUIElement, Int)] = [(root, 0)]
    var roles: [String] = []
    var index = 0
    while index < queue.count && index < 60 {
        let (element, depth) = queue[index]
        index += 1
        if roles.count < 6 {
            roles.append("\(attribute(element, kAXRoleAttribute))/\(attribute(element, kAXSubroleAttribute))")
        }
        guard depth < 5 else { continue }
        for child in children(element) { queue.append((child, depth + 1)) }
    }
    return (index, roles)
}

let apps = NSWorkspace.shared.runningApplications.sorted { $0.processIdentifier < $1.processIdentifier }
for app in apps {
    let pid = app.processIdentifier
    guard pid > 0 else { continue }
    let element = AXUIElementCreateApplication(pid)
    var windowsValue: CFTypeRef?
    let windowError = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &windowsValue)
    let windowCount = (windowsValue as? [AXUIElement])?.count ?? (windowsValue == nil ? -1 : 0)
    let (nodeCount, roles) = survey(element)
    let name = app.localizedName ?? "?"
    let bundle = app.bundleIdentifier ?? "-"
    lines.append("pid=\(pid) nodes=\(nodeCount) wins=\(windowCount) winErr=\(windowError.rawValue) "
                 + "name=\(name) bundle=\(bundle) roles=\(roles.joined(separator: " , "))")
}

let systemWide = AXUIElementCreateSystemWide()
for (label, x, y) in [("center", 700.0, 450.0), ("upper", 700.0, 260.0), ("lower", 700.0, 620.0)] {
    var hit: AXUIElement?
    let error = AXUIElementCopyElementAtPosition(systemWide, Float(x), Float(y), &hit)
    guard error == .success, let hit else {
        lines.append("point \(label) err=\(error.rawValue)")
        continue
    }
    var pid: pid_t = 0
    _ = AXUIElementGetPid(hit, &pid)
    let (nodeCount, roles) = survey(hit)
    lines.append("point \(label) pid=\(pid) nodes=\(nodeCount) "
                 + "role=\(attribute(hit, kAXRoleAttribute)) sub=\(attribute(hit, kAXSubroleAttribute)) "
                 + "roles=\(roles.joined(separator: " , "))")
}

try? lines.joined(separator: "\n").write(toFile: outputPath, atomically: true, encoding: .utf8)
print("written \(outputPath)")
