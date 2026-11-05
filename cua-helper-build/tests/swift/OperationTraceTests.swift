import Foundation
import Testing
@testable import CuaNative

@Test func operationTraceRecordsOnlyAllowedRequestMetadata() throws {
    let fields = OperationTrace.requestFields(.object([
        "windowId": .number(7), "x": .number(20), "y": .number(30), "snapshotId": .string("snapshot-1"),
        "text": .string("synthetic-secret"), "value": .string("synthetic-secret"),
        "tree": .string("synthetic-secret"), "data": .string("synthetic-secret")]))
    let data = try OperationTrace(requestID: "request\n1", method: "click").encoded("native.received", fields: fields)
    let line = try #require(String(data: data, encoding: .utf8))
    #expect(!line.contains("synthetic-secret"))
    #expect(line.filter { $0 == "\n" }.count == 1)
    let record = try JSONDecoder().decode(JSONValue.self, from: data)
    #expect(record["windowId"] == .number(7))
    #expect(record["requestId"] == .string("request\n1"))
    #expect(record["method"] == .string("click"))
}

@Test func operationGeometryKeepsWindowPointsAndNegativeDisplayOriginsSeparate() {
    let route = WindowRoute(owner: ProcessIdentity(pid: 88, startedSeconds: 1, startedMicroseconds: 0),
                            windowID: 7, bounds: WindowBounds(x: -900, y: -100, width: 400, height: 200))
    let geometry = OperationTrace.geometry(route: route, point: WindowPoint(x: 20, y: 30))
    #expect(geometry["windowPoint"]["x"] == .number(20))
    #expect(geometry["screenPoint"]["x"] == .number(-880))
    #expect(geometry["screenPoint"]["y"] == .number(-70))
    #expect(geometry["coordinateSpace"] == .string("window-points"))
}

@Test func operationTracesRemainScopedToTheirConcurrentRequests() async {
    let values = await withTaskGroup(of: String?.self, returning: [String?].self) { group in
        for id in ["first", "second"] {
            group.addTask {
                await OperationTrace.$current.withValue(OperationTrace(requestID: id, method: "click")) {
                    await Task.yield()
                    return OperationTrace.current?.requestID
                }
            }
        }
        var values: [String?] = []
        for await value in group { values.append(value) }
        return values
    }
    #expect(Set(values.compactMap { $0 }) == ["first", "second"])
    #expect(OperationTrace.current == nil)
}

@Test func operationLogRotatesWholeLinesAndFlushesQueuedRecords() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let path = root.appendingPathComponent("operations.jsonl")
    let log = OperationLog(path: path.path, maxBytes: 25)
    for index in 0..<10 { log.enqueue(Data("{\"index\":\(index)}\n".utf8)) }
    log.flush()
    for file in [path, URL(fileURLWithPath: path.path + ".1")] {
        let data = try Data(contentsOf: file)
        #expect(data.count <= 25)
        for line in data.split(separator: 10) { _ = try JSONDecoder().decode(JSONValue.self, from: Data(line)) }
    }
    #expect(try String(contentsOf: path, encoding: .utf8).contains("\"index\":9"))
    let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
    #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
}

@Test func operationLogFileFailuresDoNotThrow() throws {
    let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try Data("fixture".utf8).write(to: file)
    defer { try? FileManager.default.removeItem(at: file) }
    let log = OperationLog(path: file.appendingPathComponent("operations.jsonl").path)
    log.enqueue(Data("{}\n".utf8))
    log.flush()
    #expect(try String(contentsOf: file, encoding: .utf8) == "fixture")
}
