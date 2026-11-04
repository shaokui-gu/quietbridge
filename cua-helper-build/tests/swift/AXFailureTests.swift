// Optional control values do not hide permission, communication or discovery failures.
import ApplicationServices
import Foundation
import Testing
@testable import CuaNative

@Test func optionalMetadataFailuresDoNotHideStructuralOrPermissionFailures() {
    for name in ["AXValue", "AXTitle", "AXDescription", "AXIdentifier", "AXPlaceholderValue", "AXSubrole",
                 "AXEnabled", "AXPosition", "AXSize", "AXMain", "AXMinimized"] {
        #expect(AXAccess.optionalAttributeUnavailable(.failure, attribute: name))
    }
    for name in ["AXChildren", "AXWindows", "AXFocusedWindow", "AXRole", "AXParent"] {
        #expect(!AXAccess.optionalAttributeUnavailable(.failure, attribute: name))
    }
    for error in [AXError.apiDisabled, .cannotComplete, .invalidUIElement, .illegalArgument] {
        #expect(!AXAccess.optionalAttributeUnavailable(error, attribute: "AXValue"))
        #expect(!AXAccess.actionsUnavailable(error))
    }
    #expect(AXAccess.actionsUnavailable(.failure))
    #expect(AXAccess.actionsUnavailable(.actionUnsupported))
}

@Test func windowResolutionErrorCodesAreNotInterchangeable() {
    #expect(AXError.illegalArgument.rawValue == -25201)
    #expect(AXError.attributeUnsupported.rawValue == -25205)
}

@Test func enhancedAXInitializesBothAttributesOncePerProcessGeneration() throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    let owner = ProcessIdentity(pid: 42, startedSeconds: 1, startedMicroseconds: 0)
    var writes: [String] = []
    for _ in 0..<2 {
        try access.enableAX(identity: owner) { name in writes.append(name); return .success }
    }
    #expect(writes == ["AXManualAccessibility", "AXEnhancedUserInterface"])
    try access.enableAX(identity: ProcessIdentity(pid: 42, startedSeconds: 2, startedMicroseconds: 0)) { name in
        writes.append(name); return .attributeUnsupported
    }
    #expect(writes.count == 4)
}

@Test func unsupportedEnhancedAXAttributesAreNotRetried() throws {
    for code in [AXError.attributeUnsupported, .notImplemented] {
        let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
        let owner = ProcessIdentity(pid: 42, startedSeconds: 1, startedMicroseconds: 0)
        var writes = 0
        for _ in 0..<2 {
            try access.enableAX(identity: owner) { _ in writes += 1; return code }
        }
        #expect(writes == 2)
    }
}

@Test func enhancedAXPreservesFailuresAndDoesNotRepeatSuccessfulWrites() throws {
    for code in [AXError.apiDisabled, .cannotComplete, .invalidUIElement, .illegalArgument, .failure] {
        let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
        let owner = ProcessIdentity(pid: 42, startedSeconds: 1, startedMicroseconds: 0)
        var writes: [String] = []
        do {
            try access.enableAX(identity: owner) { name in
                writes.append(name)
                return name == "AXManualAccessibility" ? .success : code
            }
            Issue.record("增强 AX 错误被忽略")
        } catch let error as NativeFailure {
            #expect(error.accessibilityError == code.rawValue)
            #expect(error.code == (code == .apiDisabled ? "PERMISSION_DENIED" : "NATIVE_ERROR"))
        }
        try access.enableAX(identity: owner) { name in writes.append(name); return .success }
        #expect(writes == ["AXManualAccessibility", "AXEnhancedUserInterface", "AXEnhancedUserInterface"])
    }
}

@Test func hoverResolvesOnlyAnOwnedMatchingAncestorWindow() throws {
    var windowReads: [Int] = []
    func resolve(_ hit: Int?, foreign: Int? = nil, matchedID: UInt32? = 7) throws -> Int? {
        try AXAccess.hoverTarget(hit: hit, ownerPID: 42, windowID: 7,
            equal: { $0 == $1 }, pid: { $0 == foreign ? 99 : 42 },
            role: { $0 == 2 ? "AXWindow" : "AXButton" },
            parent: { $0 == 1 ? 2 : nil },
            resolveWindowID: { windowReads.append($0); return matchedID })
    }
    #expect(try resolve(1) == 1)
    #expect(windowReads == [2])
    windowReads = []
    #expect(try resolve(1, foreign: 1) == nil)
    #expect(try resolve(1, foreign: 2) == nil)
    #expect(windowReads.isEmpty)
    #expect(try resolve(1, matchedID: 8) == nil)
    #expect(try resolve(1, matchedID: nil) == nil)
    #expect(try resolve(nil) == nil)
    #expect(try resolve(3) == nil)
}

@Test func hoverAncestryDoesNotSwallowFailuresOrCycles() throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    for code in [AXError.apiDisabled, .cannotComplete, .invalidUIElement, .illegalArgument] {
        do {
            _ = try AXAccess.hoverTarget(hit: 1, ownerPID: 42, windowID: 7,
                equal: { $0 == $1 }, pid: { _ in 42 }, role: { _ in "AXWindow" },
                parent: { _ in nil }, resolveWindowID: { _ in throw access.failure(code, "resolve window ID") })
            Issue.record("窗口解析错误被忽略")
        } catch let error as NativeFailure { #expect(error.accessibilityError == code.rawValue) }
    }
    #expect(throws: NativeFailure.self) {
        try AXAccess.hoverTarget(hit: 1, ownerPID: 42, windowID: 7,
            equal: { $0 == $1 }, pid: { _ in 42 }, role: { _ in "AXGroup" },
            parent: { $0 }, resolveWindowID: { _ in Issue.record("不应解析非窗口节点"); return nil })
    }
}

private func wrappedAXError(_ code: AXError) throws -> AXValue {
    var error = code
    return try #require(AXValueCreate(.axError, &error))
}

@Test func batchValuesRetainScalarArrayAndGeometryTypes() throws {
    var point = CGPoint(x: 20, y: 40)
    let position = try #require(AXValueCreate(.cgPoint, &point))
    let values: [CFTypeRef] = ["Title" as CFString, kCFBooleanTrue, NSNumber(value: 7), [] as CFArray, position]
    for value in values {
        let decoded = try #require(try AXAccess.batchAttribute(value, name: "AXValue"))
        #expect(CFGetTypeID(decoded) == CFGetTypeID(value))
        #expect(CFEqual(decoded, value))
    }
    #expect(try AXAccess.batchAttribute(NSNull(), name: "AXValue") == nil)
}

@Test func batchUnavailableValuesKeepIndividualAttributeRules() throws {
    for error in [AXError.attributeUnsupported, .noValue, .notImplemented] {
        #expect(try AXAccess.batchAttribute(wrappedAXError(error), name: "AXChildren") == nil)
    }
    #expect(try AXAccess.batchAttribute(wrappedAXError(.failure), name: "AXTitle") == nil)
    #expect(throws: NativeFailure.self) {
        try AXAccess.batchAttribute(wrappedAXError(.failure), name: "AXChildren")
    }
}

@Test func batchFailuresPreservePermissionCommunicationAndStaleElementErrors() throws {
    for code in [AXError.apiDisabled, .cannotComplete, .invalidUIElement, .illegalArgument] {
        let value = try wrappedAXError(code)
        do {
            _ = try AXAccess.batchAttribute(value, name: "AXTitle")
            Issue.record("Batch attribute accepted an accessibility failure")
        } catch let error as NativeFailure {
            #expect(error.code == (code == .apiDisabled ? "PERMISSION_DENIED" : "NATIVE_ERROR"))
            #expect(error.accessibilityError == code.rawValue)
            #expect(error.message.contains("AXTitle"))
            #expect(error.message.contains(String(code.rawValue)))
        }
    }
}

@Test func invalidElementsInvalidateObservationsWithoutWeakeningInputOrOtherErrors() {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    let stale = access.failure(.invalidUIElement, "read AXRole")
    #expect(stale.code == "NATIVE_ERROR")
    #expect(AXWorker.observationFailure(stale).code == "STALE_SNAPSHOT")
    for code in [AXError.apiDisabled, .cannotComplete, .failure, .illegalArgument] {
        let original = access.failure(code, "read AXRole")
        let observed = AXWorker.observationFailure(original)
        #expect(observed.code == original.code)
        #expect(observed.message == original.message)
        #expect(observed.accessibilityError == code.rawValue)
    }
    let other = NativeFailure(code: "NATIVE_ERROR", message: "Unrelated failure with -25202 in text")
    #expect(AXWorker.observationFailure(other).code == other.code)
    #expect(AXWorker.observationFailure(other).message == other.message)
}

@Test func batchRejectsSuccessEncodedAsAnAttributeError() throws {
    let value = try wrappedAXError(.success)
    #expect(throws: NativeFailure.self) {
        try AXAccess.batchAttribute(value, name: "AXTitle")
    }
}

@Test func nodeRoleAndMetadataUseTheSameBatchWithoutFallbackReads() throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    let observation = try access.nodeObservation(attributes: ["AXRole": "AXWindow" as CFString, "AXTitle": "Window" as CFString]) { name in
        Issue.record("Unexpected fallback read: \(name)")
        return nil
    }
    let (role, attribute) = (observation.role, observation.attribute)
    #expect(role == "AXWindow")
    #expect(CFEqual(try #require(try attribute("AXTitle")), "Window" as CFString))
}

@Test(arguments: ["AXGroup", "AXScrollArea"])
func containersRetainTheirOwnMetadata(role: String) throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    let observation = try access.nodeObservation(attributes: ["AXRole": role as CFString, "AXTitle": "Results" as CFString]) { _ in nil }
    #expect(observation.role == role)
    #expect(CFEqual(try #require(try observation.attribute("AXTitle")), "Results" as CFString))
}

@Test(arguments: ["AXGroup", "AXScrollArea"])
func consumedContainerMetadataPreservesStaleAndCommunicationErrors(role: String) throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    for error in [AXError.invalidUIElement, .cannotComplete] {
        let observation = try access.nodeObservation(attributes: ["AXRole": role as CFString, "AXTitle": try wrappedAXError(error)]) { _ in nil }
        #expect(throws: NativeFailure.self) { try observation.attribute("AXTitle") }
    }
}

@Test func ordinaryNodeFallbackRetainsIndividualAttributeReads() throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    var reads: [String] = []
    let observation = try access.nodeObservation(attributes: nil) { name in
        reads.append(name)
        return name == "AXRole" ? "AXButton" as CFString : "Title" as CFString
    }
    let (role, attribute) = (observation.role, observation.attribute)
    #expect(role == "AXButton")
    #expect(reads == ["AXRole"])
    #expect(CFEqual(try #require(try attribute("AXTitle")), "Title" as CFString))
    #expect(reads == ["AXRole", "AXTitle"])
}

@Test func nodeRoleAbsenceAndTypeErrorsRetainTheirMeaning() throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    for missing in [NSNull() as CFTypeRef, try wrappedAXError(.noValue)] {
        let observation = try access.nodeObservation(attributes: ["AXRole": missing]) { _ in nil }
        #expect(observation.role == "AXUnknown")
    }
    do {
        _ = try access.nodeObservation(attributes: ["AXRole": kCFBooleanTrue]) { _ in nil }
        Issue.record("Non-string role was accepted")
    } catch let error as NativeFailure {
        #expect(error.code == "NATIVE_ERROR")
        #expect(error.message == "Accessibility AXRole is not a string")
    }
}

@Test func nodeRoleAndContainerChildrenRetainStructuralFailures() throws {
    let access = AXAccess(timeout: 1, cancellation: CancellationLatch())
    for name in ["AXRole", "AXChildren"] {
        for code in [AXError.invalidUIElement, .cannotComplete, .apiDisabled, .failure] {
            var attributes: [String: CFTypeRef] = ["AXRole": "AXGroup" as CFString, "AXChildren": [] as CFArray]
            attributes[name] = try wrappedAXError(code)
            do {
                let observation = try access.nodeObservation(attributes: attributes) { _ in nil }
                _ = try observation.attribute("AXChildren")
                Issue.record("Structural node error was ignored")
            } catch let error as NativeFailure {
                #expect(error.accessibilityError == code.rawValue)
                #expect(error.code == (code == .apiDisabled ? "PERMISSION_DENIED" : "NATIVE_ERROR"))
                if code == .invalidUIElement { #expect(AXWorker.observationFailure(error).code == "STALE_SNAPSHOT") }
            }
        }
    }
    #expect(throws: NativeFailure.self) {
        try access.nodeObservation(attributes: ["AXRole": "AXGroup" as CFString, "AXChildren": try wrappedAXError(.illegalArgument)]) { _ in nil }.attribute("AXChildren")
    }
}
