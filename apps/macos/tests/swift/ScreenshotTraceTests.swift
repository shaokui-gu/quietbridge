import Foundation
import Testing
@testable import QuietBridgeNative

@Test func screenshotDiagnosticsRemainOneJSONLineWithOnlyTimingMetadata() throws {
    let trace = ScreenshotTrace(requestID: "request\nwith\"quotes")
    let data = try trace.encoded("capture-image.begin")
    let line = try #require(String(data: data, encoding: .utf8))
    #expect(line.filter { $0 == "\n" }.count == 1)
    #expect(line.hasPrefix("[quietbridge-screenshot] "))
    let payload = Data(line.dropFirst("[quietbridge-screenshot] ".count).utf8)
    let fields = try #require(JSONSerialization.jsonObject(with: payload) as? [String: Any])
    #expect(Set(fields.keys) == ["requestId", "stage", "elapsedMs"])
    #expect(fields["requestId"] as? String == "request\nwith\"quotes")
    #expect(fields["stage"] as? String == "capture-image.begin")
    #expect(try #require(fields["elapsedMs"] as? Double) >= 0)
}

@Test func screenshotFailureDiagnosticsRetainTheNativeErrorCode() throws {
    let data = try ScreenshotTrace(requestID: "request").encoded("helper.failed", errorCode: "WINDOW_CHANGED")
    let fields = try #require(JSONSerialization.jsonObject(with: data.dropFirst("[quietbridge-screenshot] ".utf8.count)) as? [String: Any])
    #expect(fields["errorCode"] as? String == "WINDOW_CHANGED")
}
