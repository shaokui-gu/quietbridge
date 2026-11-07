// Each connection retains one request through reply delivery; desktop work uses the shared scheduler.
import Foundation
import AppKit
import Dispatch
import Darwin

@MainActor final class RequestCoordinator {
    private let configuration: HelperConfiguration
    private let cancellation: CancellationLatch
    private let worker: AXWorker
    private let visualCapture = VisualStateCapture()
    private let capture: WindowCapture
    private let permissions: SystemPermissions
    private let clipboard = ClipboardPaste()
    private let browserSessions = BrowserSessionManager()
    private let input: JSONLInput
    private let writer: JSONLWriter
    private let channel: SharedSocket
    private let scheduler: RequestScheduler
    private let onClose: @MainActor @Sendable () -> Void
    private let onFatal: @Sendable (NativeFailure, Double) -> Void
    private var deadline: NativeDeadline?
    private var active: UUID?
    private var operation: Task<Void, Never>?
    private var stopping = false
    private var requestControl: NativeRequestControl?
    private var activeRequestID: String?
    private var replying = false
    private var nextFrame: Data?
    private var nextCancelled = false
    private let clientID: UUID
    private let resourceLease: TaskResourceLease

    init(configuration: HelperConfiguration, capture: WindowCapture, channel: SharedSocket,
         cancellation: CancellationLatch, clientID: UUID, resourceLease: TaskResourceLease,
         scheduler: RequestScheduler, writer: JSONLWriter, permissions: SystemPermissions,
         onClose: @escaping @MainActor @Sendable () -> Void,
         onFatal: @escaping @Sendable (NativeFailure, Double) -> Void) {
        self.configuration = configuration
        self.clientID = clientID
        self.resourceLease = resourceLease
        self.capture = capture
        self.permissions = permissions
        self.cancellation = cancellation
        self.channel = channel
        self.scheduler = scheduler
        self.onClose = onClose
        self.onFatal = onFatal
        worker = AXWorker(cancellation: cancellation, axTimeout: configuration.axTimeoutSeconds,
                          textEventInterval: configuration.textEventIntervalSeconds, maxClickHoldMs: configuration.maxClickHoldMs)
        input = JSONLInput(limit: configuration.maxRequestBytes, cancellation: cancellation, channel: channel)
        self.writer = writer
    }
    func start() {
        let latch = cancellation
        input.start(onFrame: { [weak self] frame in
            DispatchQueue.main.async { self?.receive(frame) }
        }, onEOF: { [weak self] in
            latch.cancel()
            DispatchQueue.main.async { self?.shutdown() }
        }, onFailure: { [weak self] failure in
            latch.cancel()
            DispatchQueue.main.async { self?.transportFailed(failure) }
        })
    }
    private func receive(_ data: Data) {
        guard !stopping else { return }
        if let control = try? JSONDecoder().decode(JSONValue.self, from: data).object,
           Set(control.keys) == ["type", "id"], control["type"] == .string("cancel"), let id = control["id"]?.string {
            if id == activeRequestID { cancelRequest(NativeFailure(code: "NATIVE_CANCELLED", message: "Native operation cancelled")) }
            if let nextFrame, (try? NativeRequest(data: nextFrame).id) == id { nextCancelled = true }
            return
        }
        if active != nil, replying, nextFrame == nil { nextFrame = data; return }
        guard active == nil else { transportFailed(NativeFailure(code: "NATIVE_PROTOCOL", message: "Concurrent helper request admission")); return }
        let token = UUID()
        active = token
        let parsed = try? NativeRequest(data: data)
        activeRequestID = parsed?.id
        let operationTrace = parsed.flatMap { request in
            OperationTrace.methods.contains(request.method) ? OperationTrace(requestID: request.id, method: request.method) : nil
        }
        if let parsed { operationTrace?.mark("native.received", fields: OperationTrace.requestFields(parsed.params)) }
        let control = NativeRequestControl()
        requestControl = control
        let trace: ScreenshotTrace?
        if let parsed, parsed.method == "screenshot" {
            let writer = self.writer
            trace = ScreenshotTrace(requestID: parsed.id, report: { writer.diagnostic($0) })
            trace?.mark("helper.received")
        } else { trace = nil }
        let seconds = parsed?.method == "visualState" ? min(5, configuration.timeoutSeconds) : (parsed?.method == "screenshot"
            ? min(configuration.timeoutSeconds, configuration.screenshotTimeoutSeconds) : configuration.timeoutSeconds)
        let grace = configuration.shutdownGraceSeconds
        let axTimeout = Double(configuration.axTimeoutSeconds)
        let onFatal = self.onFatal
        let channel = self.channel
        deadline = NativeDeadline(seconds: seconds) {
            trace?.mark("helper.deadline", errorCode: "NATIVE_TIMEOUT")
            control.cancel(NativeFailure(code: "NATIVE_TIMEOUT", message: "Native operation exceeded its deadline"), grace: grace, axTimeout: axTimeout,
                unresponsive: {
                    onFatal(NativeFailure(code: "NATIVE_UNRESPONSIVE", message: "Native operation did not stop after cancellation"), grace)
                }, blockedReply: { channel.shutdown() })
        }
        operation = Task { [weak self] in
            guard let self else { return }
            defer { control.finish() }
            var requestID: JSONValue = .null
            let response: JSONValue
            do {
                let request = try NativeRequest(data: data)
                requestID = .string(request.id)
                if request.method == "screenshot" { try self.capture.requireAvailable() }
                let result = try await self.scheduler.perform(cancellation: self.cancellation) {
                    control.startOperation()
                    return try await OperationTrace.$current.withValue(operationTrace) {
                        operationTrace?.mark("native.started")
                        return try await self.execute(request, trace: trace)
                    }
                }
                if let failure = control.failure { throw failure }
                var diagnosticResult: [String: JSONValue] = [:]
                for key in ["dispatched", "verified", "route", "axAction", "pointer"] where result[key] != .null {
                    diagnosticResult[key] = result[key]
                }
                operationTrace?.mark("native.completed", fields: diagnosticResult)
                response = .object(["id": requestID, "result": result])
            } catch let error as NativeFailure {
                let failure = control.failure ?? error
                operationTrace?.mark("native.failed", fields: ["errorCode": .string(failure.code)])
                trace?.mark("helper.failed", errorCode: failure.code)
                response = .object(["id": requestID, "error": .object(["code": .string(failure.code), "message": .string(failure.message)])])
            } catch {
                let failure = control.failure ?? NativeFailure(code: requestID == .null ? "INVALID_REQUEST" : "NATIVE_ERROR", message: error.localizedDescription)
                operationTrace?.mark("native.failed", fields: ["errorCode": .string(failure.code)])
                trace?.mark("helper.failed", errorCode: failure.code)
                response = .object(["id": requestID, "error": .object([
                    "code": .string(failure.code), "message": .string(failure.message)])])
            }
            control.finishOperation()
            operationTrace?.flush()
            guard !self.stopping, self.active == token else { return }
            self.replying = true
            do { try await self.writer.write(response, trace: trace) }
            catch let failure as NativeFailure {
                if failure.code == "NATIVE_RESPONSE_LIMIT" {
                    // The bounded writer rejects before emitting any bytes, so one small
                    // error frame can identify the limit before this helper is invalidated.
                    try? await self.writer.write(.object(["id": requestID, "error": .object([
                        "code": .string(failure.code), "message": .string(failure.message),
                    ])]))
                }
                self.transportFailed(failure)
                return
            }
            catch { self.transportFailed(NativeFailure(code: "NATIVE_IO", message: error.localizedDescription)); return }
            guard !self.stopping, self.active == token else { return }
            self.deadline?.cancel()
            self.deadline = nil
            self.active = nil
            self.activeRequestID = nil
            self.requestControl = nil
            self.operation = nil
            self.replying = false
            if let next = self.nextFrame {
                self.nextFrame = nil
                let cancelled = self.nextCancelled
                self.nextCancelled = false
                self.receive(next)
                if cancelled { self.cancelRequest(NativeFailure(code: "NATIVE_CANCELLED", message: "Native operation cancelled")) }
            }
        }
        if let operation { control.attach(operation) }
    }

    private func cancelRequest(_ failure: NativeFailure) {
        let grace = configuration.shutdownGraceSeconds
        let onFatal = self.onFatal
        let channel = self.channel
        requestControl?.cancel(failure, grace: grace, axTimeout: Double(configuration.axTimeoutSeconds), unresponsive: {
            onFatal(NativeFailure(code: "NATIVE_UNRESPONSIVE", message: "Native operation did not stop after cancellation"), grace)
        }, blockedReply: { channel.shutdown() })
    }
    private func execute(_ request: NativeRequest, trace: ScreenshotTrace?) async throws -> JSONValue {
        let params = try RequestParameters(request)
        try cancellation.check()
        let taskOperation = ["getState", "visualState", "screenshot", "click", "hover", "scroll", "drag",
                             "setValue", "typeText", "paste", "pressKey", "selectText", "performSecondaryAction"].contains(request.method)
        if taskOperation { try resourceLease.require(clientID); resourceLease.touch(clientID) }
        defer { if taskOperation { resourceLease.touch(clientID) } }
        if request.method == "screenshot" { try capture.requireAvailable() }
        let requiresCatalog = ["getApp", "listApps", "findApps", "typeText", "pressKey"].contains(request.method)
        let catalog = requiresCatalog ? ApplicationCatalog.snapshot() : ApplicationCatalogSnapshot(applications: [], frontPID: nil)
        let result: JSONValue
        if request.method == "browserSession" {
            result = try browserSessions.handle(action: params.string("action"), sessionID: params.optionalString("sessionId"),
                                                confirm: params.optionalBoolean("confirm"),
                                                pid: params.contains("pid") ? Int32(params.integer("pid", in: 1...Int(Int32.max))) : nil,
                                                windowID: params.optionalWindowID(), cancellation: cancellation)
        } else if request.method == "permissionStatus" {
            result = permissions.status()
        } else if request.method == "applicationIcons" {
            var icons: [String: JSONValue] = [:]
            for path in try params.strings("paths") {
                try cancellation.check()
                if let icon = ApplicationCatalog.icon(at: path) { icons[path] = .string(icon) }
            }
            result = .object(icons)
        } else if request.method == "requestPermission" {
            result = try permissions.request(params.string("permission"))
        } else if request.method == "openPermissionSettings" {
            result = try permissions.openSettings(params.string("permission"))
        } else if request.method == "findApps" {
            result = .array(try ApplicationDiscovery.applications(matching: params.string("query"), exact: false,
                limit: params.integer("maxAppMatches", in: 1...1000), cancellation: cancellation).map { $0.json(catalog: catalog) })
        } else if request.method == "preview" {
            result = try await previewControl(params)
        } else if request.method == "powerGuard" {
            result = try await powerControl(params)
        } else {
            var installed: InstalledApplication?
            if request.method == "getApp" {
                let selector = try params.string("selector")
                if !catalog.applications.contains(where: { $0.matches(selector) }) && Int32(selector) == nil {
                    installed = try ApplicationDiscovery.applications(matching: selector, exact: true, limit: 1,
                                                                       cancellation: cancellation).first
                }
            }
            trace?.mark("worker.begin")
            let outcome = try await worker.execute(request.method, params: params, catalog: catalog, installed: installed, trace: trace)
            try cancellation.check()
            trace?.mark("worker.end")
            switch outcome {
            case .result(let value):
                if request.method == "getApp", var fields = value.object {
                    let path = installed?.path ?? catalog.applications.first { Double($0.pid) == value["pid"].number }?.path
                    if let path, let icon = ApplicationCatalog.icon(at: path) { fields["applicationIcon"] = .string(icon) }
                    result = .object(fields)
                } else { result = value }
            case .capture(let target):
                let previewPID = target.process.pid, previewWindow = target.window.windowID
                try cancellation.check()
                try resourceLease.acquire(clientID, resource: .preview)
                SpacePreview.shared.track(pid: previewPID, windowID: previewWindow)
                let image = try await capture.capture(target, cancellation: cancellation, trace: trace)
                result = try await worker.finishCapture(target, image: image, trace: trace)
            case .visual(let target):
                try resourceLease.acquire(clientID, resource: .preview)
                let frame = try await visualCapture.capture(target)
                result = try await worker.finishVisual(target, frame: frame)
                try cancellation.check()
                try resourceLease.require(clientID)
                SpacePreview.shared.track(pid: target.process.pid, windowID: target.window.windowID)
            case .foreground(let plan): result = try await performForeground(plan, requestID: request.id)
            case .hover(let route, let point):
                let latch = cancellation
                try WindowPointerInput.hover(in: route, at: point, checkCancellation: { try latch.check() })
                result = AXWorker.hoverResult(route: "window-pointer", pointerMoved: false)
            case .paste(let plan):
                try await clipboard.perform(text: plan.text, format: plan.format, maxBytes: plan.maxBytes, readTimeout: plan.readTimeout,
                    checkCancellation: { try cancellation.check() }) { requireClipboard in
                    try await worker.performPaste(plan, beforeDispatch: requireClipboard)
                }
                result = AXWorker.actionResult
            case .launch(let plan):
                let pid = try await ApplicationDiscovery.launch(plan.application, cancellation: cancellation)
                result = try await worker.finishLaunch(plan, pid: pid)
            }
        }
        return result
    }

    /// Start, stop or inspect the floating share panel without dispatching input.
    /// Resources belong to this socket and a bounded activity lease.
    private func previewControl(_ params: RequestParameters) async throws -> JSONValue {
        let action = try params.string("action")
        switch action {
        case "hold":
            let handle = try params.string("appHandle")
            let windowID = try params.windowID()
            let seconds = params.contains("durationSeconds")
                ? try params.number("durationSeconds") : SpacePreview.defaultHoldSeconds
            guard seconds >= 60, seconds <= SpacePreview.maximumHoldSeconds else {
                throw RequestParameters.invalid("durationSeconds 必须在 60...\(Int(SpacePreview.maximumHoldSeconds)) 之间")
            }
            let target = try await worker.previewTarget(handle: handle, windowID: windowID)
            try cancellation.check()
            guard NativePlatform.sessionLocked() == false, CGPreflightScreenCaptureAccess() else {
                throw NativeFailure(code: "SHARE_NOT_READY", message: "共享需要已授权屏幕录制且会话处于解锁状态")
            }
            let rawSource = params.contains("source") ? try params.string("source") : "application"
            guard let source = PreviewSource(rawValue: rawSource) else {
                throw RequestParameters.invalid("source 必须为 application、desktop 或 window")
            }
            try resourceLease.acquire(clientID, resource: .preview)
            SpacePreview.shared.track(pid: target.pid, windowID: target.windowID, source: source)
            let granted = SpacePreview.shared.hold(seconds: seconds)
            // Browser CDP input is independent of native video availability.
            // Keep its preview lease without waiting for or requiring a frame.
            if try params.optionalBoolean("waitForFrame") ?? true {
                try await SpacePreview.shared.waitUntilReady { try cancellation.check() }
            }
            return .object(["action": .string("hold"),
                            "windowId": .number(Double(target.windowID)),
                            "pid": .number(Double(target.pid)),
                            "source": .string(source.rawValue),
                            "durationSeconds": .number(granted),
                            "preview": previewDiagnostics()])
        case "release":
            if resourceLease.release(clientID, resource: .preview) { SpacePreview.shared.release() }
            return .object(["action": .string("release"), "preview": previewDiagnostics()])
        case "renew":
            try resourceLease.require(clientID)
            resourceLease.touch(clientID)
            return .object(["action": .string("renew"), "preview": previewDiagnostics()])
        default:
            return .object(["action": .string("status"), "preview": previewDiagnostics()])
        }
    }

    private func previewDiagnostics() -> JSONValue {
        let values = SpacePreview.shared.diagnostics()
        let targetOnCurrentSpace: JSONValue = (values["targetOnCurrentSpace"] as? Bool).map(JSONValue.bool) ?? .null
        let streamError: JSONValue = (values["streamError"] as? String).map(JSONValue.string) ?? .null
        let frameAge: JSONValue = (values["frameAgeMs"] as? Double).map(JSONValue.number) ?? .null
        let displayID = values["displayId"] as? Int ?? 0
        let fields: [String: JSONValue] = [
            "visible": .bool(values["visible"] as? Bool ?? false),
            "status": .string(values["status"] as? String ?? ""),
            "videoFrames": .number(Double(values["videoFrames"] as? Int ?? 0)),
            "panelFrame": .string(values["panelFrame"] as? String ?? ""),
            "videoLayerReady": .bool(values["videoLayerReady"] as? Bool ?? false),
            "videoLayerFailed": .bool(values["videoLayerFailed"] as? Bool ?? false),
            "holding": .bool(SpacePreview.shared.isHolding),
            "holdRemainingSeconds": SpacePreview.shared.holdRemainingSeconds.map { .number($0) } ?? .null,
            "mode": .string(values["mode"] as? String ?? "window"),
            "scope": .string(values["scope"] as? String ?? "window"),
            "targetOnCurrentSpace": targetOnCurrentSpace,
            "streamStarting": .bool(values["streamStarting"] as? Bool ?? false),
            "streamStarted": .bool(values["streamStarted"] as? Bool ?? false),
            "streamReady": .bool(values["streamReady"] as? Bool ?? false),
            "streamError": streamError,
            "frameAgeMs": frameAge,
            "displayId": displayID == 0 ? .null : .number(Double(displayID)),
            "trackingWindowId": .number(Double(values["trackingWindowId"] as? Int ?? 0)),
            "captureMode": (values["captureMode"] as? String).map(JSONValue.string) ?? .null,
            "waitingForTargetDesktop": .bool(values["waitingForTargetDesktop"] as? Bool ?? false),
            "hostWindowId": (values["hostWindowId"] as? Int).map { .number(Double($0)) } ?? .null,
            "hostPid": (values["hostPid"] as? Int).map { .number(Double($0)) } ?? .null,
            "hostSpaceIds": .array((values["hostSpaceIds"] as? [UInt64] ?? []).map { .number(Double($0)) }),
            "panelSpaceIds": .array((values["panelSpaceIds"] as? [UInt64] ?? []).map { .number(Double($0)) }),
            "hostOnCurrentSpace": (values["hostOnCurrentSpace"] as? Bool).map(JSONValue.bool) ?? .null,
            "panelOnCurrentSpace": (values["panelOnCurrentSpace"] as? Bool).map(JSONValue.bool) ?? .null,
            "panelSpaceVerified": .bool(values["panelSpaceVerified"] as? Bool ?? false),
            "panelPlacementError": (values["panelPlacementError"] as? String).map(JSONValue.string) ?? .null,
        ]
        return .object(fields)
    }

    /// 任务租约先监视系统锁屏；首次锁屏自动解锁一次并显示浮层。
    /// 用户接管后等待真正系统锁屏，再释放浮层并暂停当前任务。
    private func powerControl(_ params: RequestParameters) async throws -> JSONValue {
        let action = try params.string("action")
        switch action {
        case "unlock", "unlockInspect":
            try resourceLease.require(clientID)
            // AX traversal blocks for up to the messaging timeout per element;
            // never run it on the main actor that also drives the shield.
            let inspectOnly = action == "unlockInspect"
            let attempt = inspectOnly ? nil : try TaskPrivacyController.shared.currentUnlockAttempt()
            let operation = Task.detached(priority: .userInitiated) { () throws -> JSONValue in
                if inspectOnly { return try LockScreenUnlocker.shared.inspect() }
                guard let attempt else { throw NativeFailure(code: "AUTOMATIC_UNLOCK_NOT_ALLOWED", message: "没有自动解锁任务") }
                return try LockScreenUnlocker.shared.unlock(attempt: attempt)
            }
            let outcome = try await withTaskCancellationHandler {
                try await operation.value
            } onCancel: {
                operation.cancel()
            }
            return .object([
                "action": .string(action),
                "result": outcome,
                "privacy": TaskPrivacyController.shared.status(),
            ])
        case "lockSynthetic":
            try resourceLease.require(clientID)
            // Diagnostic: lock the session with a synthetic shortcut so the
            // guardian's hardware-input takeover stays out of the picture.
            return .object([
                "action": .string("lockSynthetic"),
                "route": .string(try TaskPrivacySyntheticLock.request()),
                "privacy": TaskPrivacyController.shared.status(),
            ])
        case "hold":
            let seconds = params.contains("durationSeconds")
                ? try params.number("durationSeconds") : PowerGuard.defaultSeconds
            guard seconds >= PowerGuard.minimumSeconds, seconds <= PowerGuard.maximumSeconds else {
                throw RequestParameters.invalid("durationSeconds 必须在 \(Int(PowerGuard.minimumSeconds))...\(Int(PowerGuard.maximumSeconds)) 之间")
            }
            let lockedUse = try params.optionalBoolean("lockedUse") ?? true
            try resourceLease.acquire(clientID, resource: .protection)
            guard TaskPrivacyController.shared.hold(seconds: seconds, lockedUse: lockedUse) else {
                resourceLease.release(clientID, resource: .protection)
                throw NativeFailure(code: "MANUAL_AUTHENTICATION_REQUIRED", message: "用户已接管；先结束当前任务，再显式绑定新任务")
            }
            var guardStatus: JSONValue
            do {
                try PowerGuard.shared.hold(seconds: seconds)
                guardStatus = PowerGuard.shared.status()
            } catch let failure as NativeFailure {
                guardStatus = .object([
                    "holding": .bool(false),
                    "error": .string("\(failure.code): \(failure.message)"),
                ])
            }
            return .object([
                "action": .string("hold"),
                "guard": guardStatus,
                "privacy": TaskPrivacyController.shared.status(),
            ])
        case "release":
            let released = resourceLease.release(clientID, resource: .protection)
            if released { TaskPrivacyController.shared.release() }
            return .object([
                "action": .string("release"),
                "guard": released ? PowerGuard.shared.release() : PowerGuard.shared.status(),
                "privacy": TaskPrivacyController.shared.status(),
            ])
        default:
            return .object([
                "action": .string("status"),
                "guard": PowerGuard.shared.status(),
                "privacy": TaskPrivacyController.shared.status(),
            ])
        }
    }

    private func performForeground(_ plan: ForegroundPlan, requestID: String) async throws -> JSONValue {
        let before = try await worker.foregroundObservation(plan)
        let raiseRequested = plan.isRaise && AXWorker.shouldRaise(plan, before: before)
        var activationRequested = false
        if !plan.isRaise || raiseRequested {
            if before.frontPID != plan.identity.pid {
                try cancellation.check()
                try ApplicationCatalog.activate(plan.identity)
                activationRequested = true
            }
        }
        if raiseRequested { try await worker.raise(plan) }
        let after = try await worker.foregroundObservation(plan)
        return .object(["dispatched": .bool(true), "verified": .bool(false), "foreground": .object([
            "requestId": .string(requestID), "operation": .string(plan.isRaise ? "AXRaise" : "activate"),
            "targetPid": .number(Double(plan.identity.pid)), "targetWindowId": plan.targetWindowID.map { .number(Double($0)) } ?? .null,
            "before": before.json, "after": after.json, "activationRequested": .bool(activationRequested), "raiseRequested": .bool(raiseRequested),
        ])])
    }
    private func transportFailed(_ failure: NativeFailure) {
        if let bytes = "\(failure.code): \(failure.message)\n".data(using: .utf8) { try? FileHandle.standardError.write(contentsOf: bytes) }
        shutdown()
    }
    func close() { shutdown() }

    func cancelTaskResources() {
        cancelRequest(NativeFailure(code: "TASK_INTERRUPTED", message: "任务连接断开或空闲租约过期"))
    }

    private func shutdown() {
        guard !stopping else { return }
        stopping = true
        cancellation.cancel()
        deadline?.cancel()
        deadline = nil
        cancelRequest(NativeFailure(code: "NATIVE_CANCELLED", message: "Native client disconnected"))
        input.stop()
        do { try clipboard.cancel() }
        catch { fputs("Clipboard restoration failed during shutdown: \(error.localizedDescription)\n", stderr) }
        active = nil
        let pending = operation
        operation = nil
        pending?.cancel()
        Task { [self] in
            await pending?.value
            deadline?.cancel()
            deadline = nil
            await worker.shutdown()
            onClose()
        }
    }
}
