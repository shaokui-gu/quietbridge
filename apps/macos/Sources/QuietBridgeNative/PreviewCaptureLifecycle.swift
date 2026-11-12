// Capture API completion is separate from immediately hiding the preview panel.
// Each instance owns one immutable generation, so a late stop cannot describe a new stream.
import Foundation

@MainActor final class PreviewCaptureLifecycle {
    private let generation: UUID
    private let windowID: UInt32
    private let source: String
    private let emit: (String, [String: JSONValue]) -> Void
    private let now: () -> TimeInterval
    private var startState = "preparing"
    private var stopState = "not_requested"
    private var stopReason: String?
    private var startAt: TimeInterval?
    private var stopAt: TimeInterval?
    private var startDuration: TimeInterval?
    private var stopDuration: TimeInterval?
    private var startWaiters: [CheckedContinuation<Void, Never>] = []

    init(generation: UUID, windowID: UInt32, source: String,
         now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         emit: ((String, [String: JSONValue]) -> Void)? = nil) {
        self.generation = generation; self.windowID = windowID; self.source = source; self.now = now
        let trace = OperationTrace(requestID: "capture-" + generation.uuidString, method: "preview")
        self.emit = emit ?? { trace.mark($0, fields: $1) }
        record("preview.capturePreparing")
    }

    func startRequested() {
        startState = "requested"; startAt = now()
        record("preview.captureStartRequested")
    }
    func startCompleted() {
        startState = "completed"; startDuration = startAt.map { max(0, now() - $0) }
        record("preview.captureStartCompleted")
        finishStartWaiters()
    }
    func startFailed(_ error: any Error) {
        startState = "failed"; startDuration = startAt.map { max(0, now() - $0) }
        record("preview.captureStartFailed", error: error)
        finishStartWaiters()
    }
    // A reset may arrive while startCapture is suspended. Stop only after it settles,
    // so a late successful start cannot outlive an already completed stop call.
    func waitForStartCompletion() async {
        guard startState == "requested" else { return }
        await withCheckedContinuation { startWaiters.append($0) }
    }
    private func finishStartWaiters() {
        let waiters = startWaiters; startWaiters.removeAll()
        for waiter in waiters { waiter.resume() }
    }
    func delegateStopped(_ error: any Error) { record("preview.captureDelegateStopped", error: error) }

    @discardableResult func stopRequested(reason: String) -> Bool {
        guard stopState == "not_requested" else { return false }
        stopState = "requested"; stopReason = reason; stopAt = now()
        record("preview.captureStopRequested")
        return true
    }
    func stopCompleted() {
        guard stopState == "requested" else { return }
        stopState = "completed"; stopDuration = stopAt.map { max(0, now() - $0) }
        record("preview.captureStopCompleted")
    }
    func stopFailed(_ error: any Error) {
        guard stopState == "requested" else { return }
        stopState = "failed"; stopDuration = stopAt.map { max(0, now() - $0) }
        record("preview.captureStopFailed", error: error)
    }

    func snapshot() -> [String: Any] {
        var result: [String: Any] = ["streamGeneration": generation.uuidString, "trackingWindowId": Int(windowID),
            "source": source, "startState": startState, "stopState": stopState,
            "stopConfirmed": stopState == "completed"]
        if let stopReason { result["stopReason"] = stopReason }
        if let startAt { result["startElapsedMs"] = (startDuration ?? max(0, now() - startAt)) * 1000 }
        if let stopAt { result["stopElapsedMs"] = (stopDuration ?? max(0, now() - stopAt)) * 1000 }
        return result
    }

    private func record(_ event: String, error: (any Error)? = nil) {
        guard let data = try? JSONSerialization.data(withJSONObject: snapshot()),
              let fields = try? JSONDecoder().decode(JSONValue.self, from: data).object else { return }
        var safe = fields
        if let error {
            let platform = error as NSError
            let allowed = ["com.apple.ScreenCaptureKit.SCStreamErrorDomain", "SCStreamErrorDomain",
                           NSOSStatusErrorDomain, NSCocoaErrorDomain, "AVFoundationErrorDomain"]
            safe["platformErrorDomain"] = .string(allowed.contains(platform.domain) ? platform.domain : "other")
            safe["platformErrorCode"] = .number(Double(platform.code))
        }
        emit(event, safe) // Never persist localized error text or page data.
    }
}
