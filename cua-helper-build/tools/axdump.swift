// Standalone probe: dump loginwindow's accessibility tree without setting a
// messaging timeout, so a timeout-driven failure cannot be mistaken for an
// empty tree. Writes to /tmp/axdump-<uid>.txt.
import AppKit
import ApplicationServices
import Foundation

let outputPath = "/tmp/axdump-\(getuid()).txt"
var lines: [String] = []
lines.append("trusted=\(AXIsProcessTrusted()) uid=\(getuid()) euid=\(geteuid())")

func describe(_ element: AXUIElement, depth: Int) -> String {
    func attribute(_ name: String) -> String {
        var value: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
        if error != .success { return "err\(error.rawValue)" }
        if let text = value as? String { return text.isEmpty ? "\"\"" : text }
        if let number = value as? NSNumber { return number.stringValue }
        return value == nil ? "nil" : "\(type(of: value!))"
    }
    var actionsDescription = "-"
    var names: CFArray?
    if AXUIElementCopyActionNames(element, &names) == .success, let names {
        actionsDescription = (names as? [String])?.joined(separator: ",") ?? ""
    }
    var identifier = ""
    var idValue: CFTypeRef?
    if AXUIElementCopyAttributeValue(element, "AXIdentifier" as CFString, &idValue) == .success,
       let text = idValue as? String { identifier = text }
    let indent = String(repeating: "  ", count: depth)
    return "\(indent)[\(depth)] role=\(attribute(kAXRoleAttribute))"
        + " sub=\(attribute(kAXSubroleAttribute))"
        + " id=\(identifier)"
        + " title=\(attribute(kAXTitleAttribute))"
        + " desc=\(attribute(kAXDescriptionAttribute))"
        + " acts=\(actionsDescription)"
}

func dump(_ root: AXUIElement, label: String) {
    lines.append("=== \(label) ===")
    var queue: [(AXUIElement, Int)] = [(root, 0)]
    var index = 0
    while index < queue.count && index < 120 {
        let (element, depth) = queue[index]
        index += 1
        lines.append(describe(element, depth: depth))
        guard depth < 4 else { continue }
        var value: CFTypeRef?
        var children: [AXUIElement] = []
        if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success,
           let list = value as? [AXUIElement] {
            children = list
        }
        if children.isEmpty, depth == 0 {
            for attribute in [kAXWindowsAttribute, kAXFocusedWindowAttribute] {
                var extra: CFTypeRef?
                if AXUIElementCopyAttributeValue(element, attribute as CFString, &extra) == .success {
                    if let list = extra as? [AXUIElement] { children.append(contentsOf: list) }
                    else if let one = extra, CFGetTypeID(one) == AXUIElementGetTypeID() {
                        children.append(unsafeDowncast(one, to: AXUIElement.self))
                    }
                }
            }
        }
        for child in children { queue.append((child, depth + 1)) }
    }
    lines.append("--- \(label) node count=\(index) ---")
}

guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.loginwindow").first else {
    lines.append("loginwindow not found")
    try? lines.joined(separator: "\n").write(toFile: outputPath, atomically: true, encoding: .utf8)
    exit(1)
}
lines.append("loginwindow pid=\(app.processIdentifier)")
dump(AXUIElementCreateApplication(app.processIdentifier), label: "app-tree")

let systemWide = AXUIElementCreateSystemWide()
var focused: CFTypeRef?
let focusError = AXUIElementCopyAttributeValue(systemWide, kAXFocusedUIElementAttribute as CFString, &focused)
lines.append("systemwide focused err=\(focusError.rawValue)")
if focusError == .success, let focused, CFGetTypeID(focused) == AXUIElementGetTypeID() {
    dump(unsafeDowncast(focused, to: AXUIElement.self), label: "focused-tree")
}
var hit: AXUIElement?
let hitError = AXUIElementCopyElementAtPosition(systemWide, 700, 450, &hit)
lines.append("elementAtPosition err=\(hitError.rawValue)")
if hitError == .success, let hit { dump(hit, label: "point-tree") }

try? lines.joined(separator: "\n").write(toFile: outputPath, atomically: true, encoding: .utf8)
print("written \(outputPath)")
