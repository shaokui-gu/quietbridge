import Testing
@testable import QuietBridgeNative

@Test func setValueReadbackKeepsDispatchSeparateFromBusinessVerification() {
    for (observed, status) in [("QBTEST", "match"), ("\u{200B}\nQBTEST\u{200B}\n", "mismatch"),
                               (nil, "unavailable")] as [(String?, String)] {
        #expect(AXWorker.setValueResult(expected: "QBTEST", observed: observed) == .object([
            "dispatched": .bool(true), "verified": .bool(false), "valueReadback": .string(status)
        ]))
    }
}
