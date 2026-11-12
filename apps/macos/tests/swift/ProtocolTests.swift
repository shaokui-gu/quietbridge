// Wire validation and nullable observations do not require desktop access.
import Foundation
import Testing
@testable import QuietBridgeNative

@Test func preservesJSONKindsAndNulls() throws {
    let value = JSONValue.object([
        "missing": .null,
        "flags": .array([.bool(true), .bool(false)]),
        "number": .number(1),
        "text": .string("中文 🧪"),
    ])
    let encoded = try JSONEncoder().encode(value)
    #expect(try JSONDecoder().decode(JSONValue.self, from: encoded) == value)
    #expect(value["absent"] == .null)
    #expect(value["flags"].number == nil)
    #expect(value["number"].string == nil)
}

@Test(arguments: [
    "{}", "[]", "null", "true",
    #"{"id":7,"method":"listApps","params":{}}"#,
    #"{"id":"a","method":null,"params":{}}"#,
    #"{"id":"a","method":"listApps"}"#,
    #"{"id":"a","method":"listApps","params":{},"extra":true}"#,
    #"{"id":"a","id":"b","method":"listApps","params":{}}"#,
    #"{"id":"a","method":"listApps","params":{},"params":{}}"#,
])
func refusesInvalidRequestEnvelopes(_ source: String) {
    #expect(throws: NativeFailure.self) {
        try NativeRequest(data: Data(source.utf8))
    }
}

@Test func retainsTheRequestIDForMethodLevelParameterErrors() throws {
    let request = try NativeRequest(data: Data(#"{"id":"call-7","method":"listApps","params":[]}"#.utf8))
    #expect(request.id == "call-7")
    #expect(request.method == "listApps")
    #expect(request.params == .array([]))
}

@Test func emitsUnknownWindowSpaceAsExplicitNull() throws {
    let window = WindowInfo(
        windowID: 7, pid: 42, appName: "Fixture", title: "Canvas",
        bounds: WindowBounds(x: -100, y: 40, width: 320, height: 200),
        isOnScreen: true, onCurrentSpace: nil
    )
    let encoded = try JSONEncoder().encode(window.json)
    let object = try #require(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
    #expect(object["onCurrentSpace"] is NSNull)
    #expect(window.json["windowId"] == .number(7))
    #expect(window.json["bounds"]["x"] == .number(-100))
    #expect(window.json["isOnScreen"] == .bool(true))
}

@Test func separatesUnsupportedMethodsFromInvalidParameters() throws {
    let unsupported = try NativeRequest(data: Data(#"{"id":"call","method":"absent","params":{}}"#.utf8))
    do {
        _ = try RequestParameters(unsupported)
        Issue.record("Unsupported operation was admitted")
    } catch let error as NativeFailure {
        #expect(error.code == "UNSUPPORTED")
    }
    for source in [
        #"{"id":"call","method":"listApps","params":[]}"#,
        #"{"id":"call","method":"listApps","params":{"launch":true}}"#,
    ] {
        do {
            _ = try RequestParameters(NativeRequest(data: Data(source.utf8)))
            Issue.record("Invalid operation parameters were admitted")
        } catch let error as NativeFailure {
            #expect(error.code == "INVALID_ARGUMENT")
        }
    }
}

@Test(arguments: ["application", "window", "desktop"])
func admitsPreviewLeaseWithoutGrantingInput(_ source: String) throws {
    #expect(NativeProtocol.modelMethods.contains("preview"))
    let hold = try RequestParameters(NativeRequest(data: Data("{\"id\":\"p\",\"method\":\"preview\",\"params\":{\"action\":\"hold\",\"appHandle\":\"app\",\"windowId\":7,\"durationSeconds\":900,\"source\":\"\(source)\"}}".utf8)))
    #expect(try hold.string("action") == "hold")
    #expect(try hold.windowID() == 7)
    #expect(try hold.number("durationSeconds") == 900)
    #expect(try hold.string("source") == source)
    // release/status carry no target: the task may end after the app quit.
    for action in ["release", "status", "renew"] {
        let params = try RequestParameters(NativeRequest(data: Data("{\"id\":\"p\",\"method\":\"preview\",\"params\":{\"action\":\"\(action)\"}}".utf8)))
        #expect(try params.string("action") == action)
    }
    for invalid in [
        #"{"id":"p","method":"preview","params":{"action":"hold","appHandle":"app"}}"#,
        #"{"id":"p","method":"preview","params":{"action":"release","windowId":7}}"#,
        #"{"id":"p","method":"preview","params":{"action":"show"}}"#,
        #"{"id":"p","method":"preview","params":{"action":"hold","appHandle":"app","windowId":7,"force":true}}"#,
        #"{"id":"p","method":"preview","params":{"action":"hold","appHandle":"app","windowId":7,"source":"screen"}}"#,
    ] {
        do {
            _ = try RequestParameters(NativeRequest(data: Data(invalid.utf8)))
            Issue.record("Invalid preview request was admitted: \(invalid)")
        } catch let error as NativeFailure {
            #expect(error.code == "INVALID_ARGUMENT")
        }
    }
}

@Test(arguments: ["true", "1.5", "0", "4294967296", "\"7\""])
func rejectsInvalidWindowIDs(_ value: String) throws {
    let request = try NativeRequest(data: Data("{\"id\":\"call\",\"method\":\"screenshot\",\"params\":{\"windowId\":\(value)}}".utf8))
    let params = try RequestParameters(request)
    #expect(throws: NativeFailure.self) { try params.windowID() }
}

@Test func rejectsRoundedIntegerOverflowWithoutTrapping() throws {
    let request = try NativeRequest(data: Data(#"{"id":"call","method":"click","params":{"elementIndex":9223372036854775808}}"#.utf8))
    let params = try RequestParameters(request)
    #expect(throws: NativeFailure.self) { try params.integer("elementIndex", in: 0...Int.max) }
}

@Test func nestedSeparatorsAndEscapedQuotesDoNotChangeEnvelopeFieldCount() throws {
    let request = try NativeRequest(data: Data(#"{"id":"call","method":"typeText","params":{"text":"comma, quote\" braces{}[] slash\\","nested":[1,{"x":2,"y":3}]}}"#.utf8))
    #expect(request.id == "call")
    #expect(request.params["nested"] == .array([.number(1), .object(["x": .number(2), "y": .number(3)])]))
}

@Test func resolvesTextEventIntervalFromHelperArguments() throws {
    #expect(try HelperConfiguration(arguments: []).textEventIntervalSeconds == 0.00025)
    #expect(try HelperConfiguration(arguments: ["--text-event-interval-ms", "0.5"]).textEventIntervalSeconds == 0.0005)
    #expect(try HelperConfiguration(arguments: ["--text-event-interval-ms", "0"]).textEventIntervalSeconds == 0)
    #expect(try HelperConfiguration(arguments: ["--text-event-interval-ms", "1000"]).textEventIntervalSeconds == 1)
    for invalid in ["-1", "1000.1", "nan", "inf", "absent"] {
        #expect(throws: NativeFailure.self) {
            try HelperConfiguration(arguments: ["--text-event-interval-ms", invalid])
        }
    }
}

@Test func disclosureFlagsRejectNonBooleanWireValues() throws {
    for source in ["true", "false"] {
        let params = try RequestParameters(NativeRequest(data: Data("{\"id\":\"read\",\"method\":\"getState\",\"params\":{\"details\":\(source)}}".utf8)))
        #expect(try params.optionalBoolean("details") == (source == "true"))
        #expect(try params.optionalBoolean("includeMenus") == nil)
    }
    for source in ["1", "null", "\"true\""] {
        let params = try RequestParameters(NativeRequest(data: Data("{\"id\":\"read\",\"method\":\"getState\",\"params\":{\"includeMenus\":\(source)}}".utf8)))
        #expect(throws: NativeFailure.self) { try params.optionalBoolean("includeMenus") }
    }
}

@Test(arguments: ["null", "true", "[1]", "[\"\"]"])
func rejectsMalformedApplicationIconPaths(_ value: String) throws {
    let request = try NativeRequest(data: Data("{\"id\":\"icons\",\"method\":\"applicationIcons\",\"params\":{\"paths\":\(value)}}".utf8))
    #expect(throws: NativeFailure.self) { try RequestParameters(request).strings("paths") }
}

@Test func admitsBrowserPreviewProducerMessagesAndRejectsMixedScopes() throws {
    func admit(_ values: [String: JSONValue]) throws -> RequestParameters {
        let encoded = try JSONEncoder().encode(JSONValue.object([
            "id": .string("preview-contract"), "method": .string("preview"), "params": .object(values)]))
        return try RequestParameters(NativeRequest(data: encoded))
    }
    let hold: [String: JSONValue] = ["action": .string("hold"), "appHandle": .string("app"),
        "windowId": .number(207258), "source": .string("browser_content"), "leaseId": .string("lease"),
        "durationSeconds": .number(600), "waitForFrame": .bool(false)]
    let renew: [String: JSONValue] = ["action": .string("browserRenew"), "leaseId": .string("lease"),
        "durationSeconds": .number(600)]
    let clear: [String: JSONValue] = ["action": .string("browserFrame"), "leaseId": .string("lease"),
        "documentKey": .string("document"), "sequence": .number(1)]
    var frame = clear
    frame.merge(["sequence": .number(2), "data": .string("ZmFrZQ=="), "capturedAt": .number(1794380125)]) { _, new in new }
    for message in [hold, renew, clear, frame] { _ = try admit(message) }
    var noLease = hold; noLease.removeValue(forKey: "leaseId")
    var systemLease = hold; systemLease["source"] = .string("window")
    var partial = frame; partial.removeValue(forKey: "capturedAt")
    var fractional = clear; fractional["sequence"] = .number(1.5)
    var crossScope = renew; crossScope["windowId"] = .number(8)
    var oversized = frame; oversized["data"] = .string(String(repeating: "a", count: 180001))
    for message in [noLease, systemLease, partial, fractional, crossScope, oversized] {
        #expect(throws: NativeFailure.self) { try admit(message) }
    }
}

// Exercise the real Python producer against Swift admission, without starting
// the helper, a browser, a capture or the preview UI.
@Test func pythonBrowserPreviewProducerPassesNativeAdmission() throws {
    var root = URL(fileURLWithPath: #filePath)
    for _ in 0..<5 { root.deleteLastPathComponent() }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = ["python3", "-B", "-c", """
import json,sys
sys.path.insert(0,sys.argv[1])
from test_browser_preview import BrowserPreviewTests
case=BrowserPreviewTests()
case.setUp()
try:
    lease=case.start()
    case.start()
    case.data['peer']['previewFrame']=case.frame(lease)
    case.preview.tick()
    case.preview.stop()
    print(json.dumps([dict(id='contract',method=c.args[0],params=c.args[1]) for c in case.request.call_args_list]))
finally:
    case.doCleanups()
""", root.appendingPathComponent("services/mcp").path]
    let output = Pipe(), errors = Pipe()
    process.standardOutput = output
    process.standardError = errors
    try process.run()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    #expect(process.terminationStatus == 0)
    let messages = try JSONDecoder().decode([JSONValue].self, from: data)
    #expect(messages.count == 5)
    for message in messages {
        let request = try NativeRequest(data: JSONEncoder().encode(message))
        _ = try RequestParameters(request)
    }
}
