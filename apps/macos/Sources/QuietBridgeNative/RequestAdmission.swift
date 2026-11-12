// Strict model/native request admission; defaults match the existing JSONL operations.
import Foundation

struct RequestParameters: Sendable {
    static let fields = NativeProtocol.fields
    let values: [String: JSONValue]
    init(_ request: NativeRequest) throws {
        guard let values = request.params.object else { throw Self.invalid("params must be an object") }
        guard let allowed = Self.fields[request.method] else { throw NativeFailure(code: "UNSUPPORTED", message: "Unsupported native operation: \(request.method)") }
        guard Set(values.keys).isSubset(of: allowed) else { throw Self.invalid("Unknown operation parameter") }
        for (field, allowed) in NativeProtocol.stringEnums[request.method] ?? [:] {
            guard let value = values[field] else { continue }
            switch value {
            case .string(let text):
                guard allowed.contains(text) else { throw Self.invalid("Unsupported value for \(field)") }
            case .array(let items):
                guard items.allSatisfy({ $0.string.map(allowed.contains) ?? false }) else { throw Self.invalid("Unsupported value for \(field)") }
            default: throw Self.invalid("Invalid enum value for \(field)")
            }
        }
        if request.method == "preview" {
            switch values["action"]?.string {
            case "hold":
                guard ["action", "appHandle", "windowId"].allSatisfy({ values[$0] != nil }),
                      Set(values.keys).isSubset(of: ["action", "appHandle", "windowId", "durationSeconds", "source", "waitForFrame", "leaseId"]) else {
                    throw Self.invalid("hold 需要 appHandle、windowId，可选 durationSeconds 与 source")
                }
                if values["source"] == .string("browser_content") {
                    guard let lease = values["leaseId"]?.string, !lease.isEmpty, lease.utf8.count <= 128 else {
                        throw Self.invalid("网页预览 hold 需要 leaseId")
                    }
                } else if values["leaseId"] != nil {
                    throw Self.invalid("系统预览不接受 leaseId")
                }
            case "browserRenew", "browserFrame":
                let frame = values["action"] == .string("browserFrame")
                let required: Set<String> = frame ? ["action", "leaseId", "documentKey", "sequence"] : ["action", "leaseId", "durationSeconds"]
                let allowed = frame ? required.union(["data", "capturedAt"]) : required
                guard required.isSubset(of: Set(values.keys)), Set(values.keys).isSubset(of: allowed),
                      let lease = values["leaseId"]?.string, !lease.isEmpty, lease.utf8.count <= 128 else {
                    throw Self.invalid("网页预览参数不完整或包含额外字段")
                }
                if frame {
                    guard let key = values["documentKey"]?.string, key.utf8.count <= 2048,
                          let sequence = values["sequence"]?.number, sequence.isFinite,
                          sequence > 0, Int(exactly: sequence) != nil,
                          (values["data"] != nil) == (values["capturedAt"] != nil) else {
                        throw Self.invalid("网页预览帧身份或时间字段无效")
                    }
                    if let raw = values["data"] {
                        guard let data = raw.string, data.utf8.count <= 180000,
                              let captured = values["capturedAt"]?.number, captured.isFinite else {
                            throw Self.invalid("网页预览帧超出限制")
                        }
                    }
                }
            case "release", "status", "renew":
                guard Set(values.keys) == ["action"] else { throw Self.invalid("release/status 仅接受 action") }
            default:
                throw Self.invalid("preview action 无效")
            }
        }
        if request.method == "powerGuard" {
            switch values["action"]?.string {
            case "hold":
                guard Set(values.keys).isSubset(of: ["action", "durationSeconds", "lockedUse"]) else {
                    throw Self.invalid("hold 仅接受可选的 durationSeconds 与 lockedUse")
                }
            case "release", "status", "unlock", "unlockInspect", "lockSynthetic":
                guard Set(values.keys) == ["action"] else {
                    throw Self.invalid("\(values["action"]?.string ?? "action") 仅接受 action")
                }
            default:
                throw Self.invalid("powerGuard action 必须为 hold、release、status、unlock、unlockInspect 或 lockSynthetic")
            }
        }
        if request.method == "browserSession" {
            if values["action"] == .string("start") {
                guard Set(values.keys) == ["action", "sessionId", "confirm"], values["confirm"] == .bool(true),
                      let id = values["sessionId"]?.string, UUID(uuidString: id)?.uuidString == id else {
                    throw Self.invalid("start 必须提供规范 UUID sessionId 和 confirm=true")
                }
            } else if values["action"] == .string("attach") {
                guard Set(values.keys) == ["action", "pid", "windowId", "confirm"], values["confirm"] == .bool(true),
                      let pid = values["pid"]?.number, pid >= 1, pid <= Double(Int32.max), pid.rounded(.towardZero) == pid,
                      let window = values["windowId"]?.number, window >= 1, window <= Double(UInt32.max), window.rounded(.towardZero) == window else {
                    throw Self.invalid("attach 必须且仅提供有效 pid、windowId 和 confirm=true")
                }
            } else {
                guard Set(values.keys) == ["action"], let action = values["action"]?.string,
                      ["prepare", "list"].contains(action) else { throw Self.invalid("prepare/list 仅接受 action") }
            }
        }
        self.values = values
    }
    static func invalid(_ message: String) -> NativeFailure { NativeFailure(code: "INVALID_ARGUMENT", message: message) }
    func contains(_ key: String) -> Bool { values[key] != nil }
    func string(_ key: String) throws -> String {
        guard let value = values[key]?.string else { throw Self.invalid("\(key) must be a string") }
        return value
    }
    func strings(_ key: String) throws -> [String] {
        guard let raw = values[key], case .array(let items) = raw else { throw Self.invalid("\(key) must be a string array") }
        return try items.map { value in
            guard let path = value.string, !path.isEmpty else { throw Self.invalid("\(key) must contain nonempty strings") }
            return path
        }
    }
    func optionalString(_ key: String) throws -> String? { contains(key) ? try string(key) : nil }
    func optionalBoolean(_ key: String) throws -> Bool? {
        guard let raw = values[key] else { return nil }
        guard case .bool(let value) = raw else { throw Self.invalid("\(key) must be a boolean") }
        return value
    }
    func number(_ key: String) throws -> Double {
        guard let value = values[key]?.number, value.isFinite else { throw Self.invalid("\(key) must be finite") }
        return value
    }
    func integer(_ key: String, in bounds: ClosedRange<Int> = Int(Int32.min)...Int(Int32.max)) throws -> Int {
        let value = try number(key)
        guard value.rounded(.towardZero) == value, value >= Double(bounds.lowerBound), value <= Double(bounds.upperBound),
              let integer = Int(exactly: value), bounds.contains(integer) else {
            throw Self.invalid("\(key) must be an integer in \(bounds)")
        }
        return integer
    }
    func windowID() throws -> UInt32 { UInt32(try integer("windowId", in: 1...Int(UInt32.max))) }
    func optionalWindowID() throws -> UInt32? { contains("windowId") ? try windowID() : nil }
    func modifiers() throws -> [String] {
        guard let value = values["modifiers"] else { return [] }
        guard case .array(let items) = value else { throw Self.invalid("modifiers must be an array") }
        return try items.map { item in
            guard let name = item.string else { throw Self.invalid("Unsupported key modifier") }
            return name
        }
    }
    func point(_ x: String = "x", _ y: String = "y") throws -> WindowPoint { WindowPoint(x: try number(x), y: try number(y)) }
}
